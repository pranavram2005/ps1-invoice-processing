"""SQLite persistence.

Six tables, one concern each:

* `vendors`, `pos`, `po_consumption` - the procurement masters. These stand in
  for an ERP; every read goes through a function in this module, so swapping
  SQLite for a real procurement API touches this file and nothing else.
* `runs` + `run_stages` - the audit trail. `run_stages` is not a log of the UI;
  it *is* the UI. The live run view streams these rows as they are written and
  the dashboard replays the same rows from disk, so what you watch happen and
  what you can audit later are the same record.
* `llm_events` - token/latency/cost telemetry for the Admin tab.
* `app_config` - the live rule thresholds, so a change made in the Admin tab
  survives a restart.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterator

from .config import DB_PATH, price_for
from .models import RunResult, RunSummary, StageRecord

SCHEMA = """
CREATE TABLE IF NOT EXISTS vendors (
    vendor_id          TEXT PRIMARY KEY,
    name               TEXT NOT NULL,
    aliases_json       TEXT NOT NULL DEFAULT '[]',
    gstin              TEXT,
    status             TEXT NOT NULL DEFAULT 'approved',   -- approved | blocked
    payment_terms_days INTEGER NOT NULL DEFAULT 30,
    source             TEXT NOT NULL DEFAULT 'seed',       -- seed | user
    created_at         TEXT
);

-- `source` separates the twelve seeded demo POs from ones a user raised in
-- the UI, so the two can never be confused on screen. Same convention as
-- `po_consumption.source`.
CREATE TABLE IF NOT EXISTS pos (
    po_number    TEXT PRIMARY KEY,
    vendor_id    TEXT NOT NULL,
    description  TEXT,
    po_value     REAL NOT NULL,
    currency     TEXT NOT NULL DEFAULT 'INR',
    issued_date  TEXT NOT NULL,
    expiry_date  TEXT,
    status       TEXT NOT NULL DEFAULT 'open',
    source       TEXT NOT NULL DEFAULT 'seed',   -- seed | user
    created_at   TEXT,
    FOREIGN KEY (vendor_id) REFERENCES vendors(vendor_id)
);
CREATE INDEX IF NOT EXISTS idx_pos_vendor ON pos(vendor_id);

-- Everything billed against a PO. Seeded rows (source='history') represent
-- invoices paid before this system existed; rows written by a run
-- (source='run') are what make the pipeline stateful across submissions.
CREATE TABLE IF NOT EXISTS po_consumption (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    po_number      TEXT NOT NULL,
    run_id         TEXT,
    invoice_number TEXT,
    amount         REAL NOT NULL,
    booked_at      TEXT NOT NULL,
    source         TEXT NOT NULL DEFAULT 'run',
    FOREIGN KEY (po_number) REFERENCES pos(po_number)
);
CREATE INDEX IF NOT EXISTS idx_consumption_po ON po_consumption(po_number);

CREATE TABLE IF NOT EXISTS runs (
    run_id         TEXT PRIMARY KEY,
    filename       TEXT NOT NULL,
    file_sha256    TEXT NOT NULL,
    file_bytes     INTEGER,
    pdf_kind       TEXT,
    page_count     INTEGER,
    status         TEXT NOT NULL,
    decision       TEXT,
    confidence     REAL,
    vendor_id      TEXT,
    vendor_name    TEXT,
    invoice_number TEXT,
    invoice_date   TEXT,
    currency       TEXT,
    total_amount   REAL,
    matched_po     TEXT,
    po_match_score REAL,
    failed_rules   TEXT NOT NULL DEFAULT '[]',
    result_json    TEXT NOT NULL DEFAULT '{}',
    started_at     TEXT NOT NULL,
    finished_at    TEXT,
    duration_ms    INTEGER,
    cached         INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_runs_hash ON runs(file_sha256);
CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started_at);
CREATE INDEX IF NOT EXISTS idx_runs_vendor ON runs(vendor_id);

CREATE TABLE IF NOT EXISTS run_stages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       TEXT NOT NULL,
    seq          INTEGER NOT NULL,
    stage        TEXT NOT NULL,
    label        TEXT NOT NULL,
    status       TEXT NOT NULL,
    started_at   TEXT,
    finished_at  TEXT,
    duration_ms  INTEGER,
    summary      TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '{}',
    error        TEXT,
    UNIQUE (run_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_stages_run ON run_stages(run_id);

CREATE TABLE IF NOT EXISTS llm_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        TEXT,
    model         TEXT NOT NULL,
    latency_ms    INTEGER NOT NULL,
    input_tokens  INTEGER,
    output_tokens INTEGER,
    cached        INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS app_config (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


# Columns added after the first release. `CREATE TABLE IF NOT EXISTS` is a
# no-op on a table that already exists, so a new column has to be ALTERed in:
# the repository ships a populated `data/invoices.db`, and on Render that file
# is used as-is. Without this the feature would work locally and break there.
MIGRATIONS: list[tuple[str, str, str]] = [
    ("pos", "source", "ALTER TABLE pos ADD COLUMN source TEXT NOT NULL DEFAULT 'seed'"),
    ("pos", "created_at", "ALTER TABLE pos ADD COLUMN created_at TEXT"),
    ("vendors", "source", "ALTER TABLE vendors ADD COLUMN source TEXT NOT NULL DEFAULT 'seed'"),
    ("vendors", "created_at", "ALTER TABLE vendors ADD COLUMN created_at TEXT"),
]


def _migrate(conn: sqlite3.Connection) -> list[str]:
    applied: list[str] = []
    for table, column, ddl in MIGRATIONS:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if not cols or column in cols:
            continue
        conn.execute(ddl)
        applied.append(f"{table}.{column}")
    return applied


def init_db(path: Path = DB_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(DB_PATH, timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Procurement masters (the ERP seam)
# --------------------------------------------------------------------------- #

def upsert_vendor(
    vendor_id: str, name: str, aliases: list[str], gstin: str | None,
    status: str = "approved", payment_terms_days: int = 30, source: str = "seed",
) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO vendors (vendor_id, name, aliases_json, gstin, status,
                                 payment_terms_days, source, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(vendor_id) DO UPDATE SET
                name=excluded.name, aliases_json=excluded.aliases_json,
                gstin=excluded.gstin, status=excluded.status,
                payment_terms_days=excluded.payment_terms_days,
                source=excluded.source
            """,
            (vendor_id, name, json.dumps(aliases), gstin, status, payment_terms_days,
             source, _now()),
        )


def _next_vendor_id(conn: sqlite3.Connection) -> str:
    """The next free id in the user's own range, on an open connection.

    The seeded master occupies V-1001 to V-1006, so a vendor added here starts
    at V-2001: the id itself says which half of the master it came from, and
    nobody has to invent an identifier to add a supplier.

    Takes the connection rather than opening its own, so the read and the
    insert that follows it are one transaction. Two callers arriving together
    would otherwise both read the same maximum, and the second would fail on
    the primary key.
    """
    rows = conn.execute("SELECT vendor_id FROM vendors WHERE vendor_id LIKE 'V-2%'").fetchall()
    used = [int(r[0][2:]) for r in rows if r[0][2:].isdigit()]
    return f"V-{max(used, default=2000) + 1}"


def insert_vendor(
    name: str, aliases: list[str], gstin: str | None, *,
    payment_terms_days: int = 30, vendor_id: str | None = None,
) -> dict:
    """Add an approved vendor. Never an upsert, for the same reason
    `insert_po` is not: silently rewriting a seeded vendor would make the runs
    already matched against it unexplainable."""
    with connect() as conn:
        new_id = vendor_id or _next_vendor_id(conn)
        conn.execute(
            """
            INSERT INTO vendors (vendor_id, name, aliases_json, gstin, status,
                                 payment_terms_days, source, created_at)
            VALUES (?, ?, ?, ?, 'approved', ?, 'user', ?)
            """,
            (new_id, name, json.dumps(aliases), gstin, payment_terms_days, _now()),
        )
    created = next((v for v in list_vendors() if v["vendor_id"] == new_id), None)
    assert created is not None
    return created


def list_vendors() -> list[dict]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM vendors ORDER BY name").fetchall()
    return [
        {
            "vendor_id": r["vendor_id"],
            "name": r["name"],
            "aliases": json.loads(r["aliases_json"]),
            "gstin": r["gstin"],
            "status": r["status"],
            "payment_terms_days": r["payment_terms_days"],
            "source": (r["source"] if "source" in r.keys() else "seed") or "seed",
        }
        for r in rows
    ]


def upsert_po(
    po_number: str, vendor_id: str, description: str, po_value: float,
    issued_date: str, expiry_date: str | None, currency: str = "INR",
    status: str = "open", source: str = "seed", created_at: str | None = None,
) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO pos (po_number, vendor_id, description, po_value, currency,
                             issued_date, expiry_date, status, source, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(po_number) DO UPDATE SET
                vendor_id=excluded.vendor_id, description=excluded.description,
                po_value=excluded.po_value, currency=excluded.currency,
                issued_date=excluded.issued_date, expiry_date=excluded.expiry_date,
                status=excluded.status, source=excluded.source
            """,
            (po_number, vendor_id, description, po_value, currency,
             issued_date, expiry_date, status, source, created_at or _now()),
        )


def insert_po(
    po_number: str, vendor_id: str, description: str, po_value: float,
    issued_date: str, expiry_date: str | None, currency: str = "INR",
    status: str = "open", source: str = "user",
) -> dict:
    """Create a PO that does not exist yet, or raise.

    Deliberately not `upsert_po`: raising a PO through the UI must never
    silently rewrite one of the seeded masters, because an invoice already
    matched against the old value would then be unexplainable. The caller turns
    the IntegrityError into a 409.
    """
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO pos (po_number, vendor_id, description, po_value, currency,
                             issued_date, expiry_date, status, source, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (po_number, vendor_id, description, po_value, currency,
             issued_date, expiry_date, status, source, _now()),
        )
    created = get_po(po_number)
    assert created is not None
    return created


def delete_po(po_number: str) -> bool:
    """Remove a PO and whatever was billed against it.

    Only ever called for a user-raised PO (the API enforces that). The
    consumption rows go too: leaving them would keep a ledger pointing at a PO
    that no longer exists, and they only exist because of runs against this PO
    in the first place.
    """
    with connect() as conn:
        conn.execute("DELETE FROM po_consumption WHERE po_number = ?", (po_number,))
        cur = conn.execute("DELETE FROM pos WHERE po_number = ?", (po_number,))
        return cur.rowcount > 0


def list_pos(vendor_id: str | None = None) -> list[dict]:
    """POs with their consumption ledger folded in."""
    sql = """
        SELECT p.*, v.name AS vendor_name,
               COALESCE((SELECT SUM(amount) FROM po_consumption c
                         WHERE c.po_number = p.po_number), 0) AS already_billed
        FROM pos p
        LEFT JOIN vendors v ON v.vendor_id = p.vendor_id
    """
    params: tuple = ()
    if vendor_id:
        sql += " WHERE p.vendor_id = ?"
        params = (vendor_id,)
    sql += " ORDER BY p.po_number"
    with connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    out: list[dict] = []
    for r in rows:
        billed = float(r["already_billed"] or 0.0)
        out.append({
            "po_number": r["po_number"],
            "source": (r["source"] if "source" in r.keys() else "seed") or "seed",
            "created_at": r["created_at"] if "created_at" in r.keys() else None,
            "vendor_id": r["vendor_id"],
            "vendor_name": r["vendor_name"],
            "description": r["description"],
            "po_value": float(r["po_value"]),
            "currency": r["currency"],
            "issued_date": r["issued_date"],
            "expiry_date": r["expiry_date"],
            "status": r["status"],
            "already_billed": billed,
            "remaining": float(r["po_value"]) - billed,
        })
    return out


def get_po(po_number: str) -> dict | None:
    matches = [p for p in list_pos() if p["po_number"] == po_number]
    return matches[0] if matches else None


def po_consumption_detail(po_number: str) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM po_consumption WHERE po_number = ? ORDER BY booked_at",
            (po_number,),
        ).fetchall()
    return [dict(r) for r in rows]


def book_consumption(
    po_number: str, amount: float, *, run_id: str | None = None,
    invoice_number: str | None = None, source: str = "run",
    booked_at: str | None = None,
) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO po_consumption (po_number, run_id, invoice_number, amount, booked_at, source)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (po_number, run_id, invoice_number, amount, booked_at or _now(), source),
        )


def unbook_run(run_id: str) -> None:
    """Remove a run's consumption rows. Called before a re-run of the same run
    id so replays do not double-count against a PO."""
    with connect() as conn:
        conn.execute("DELETE FROM po_consumption WHERE run_id = ?", (run_id,))


# --------------------------------------------------------------------------- #
# Runs + stages
# --------------------------------------------------------------------------- #

def create_run(
    run_id: str, filename: str, file_sha256: str, file_bytes: int,
    stages: list[StageRecord],
) -> None:
    started = _now()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO runs (run_id, filename, file_sha256, file_bytes, status, started_at)
            VALUES (?, ?, ?, ?, 'running', ?)
            -- A replay restarts the run under the same id, so every derived
            -- column is cleared. Leaving the previous `result_json` in place
            -- would make the API serve the *old* decision for as long as the
            -- replay takes, because the stored blob carries its own status.
            ON CONFLICT(run_id) DO UPDATE SET
                filename=excluded.filename, file_sha256=excluded.file_sha256,
                file_bytes=excluded.file_bytes, status='running',
                started_at=excluded.started_at, finished_at=NULL, duration_ms=NULL,
                decision=NULL, confidence=NULL, failed_rules='[]',
                result_json='{}', pdf_kind=NULL, page_count=NULL,
                vendor_id=NULL, vendor_name=NULL, invoice_number=NULL,
                invoice_date=NULL, currency=NULL, total_amount=NULL,
                matched_po=NULL, po_match_score=NULL, cached=0
            """,
            (run_id, filename, file_sha256, file_bytes, started),
        )
        conn.execute("DELETE FROM run_stages WHERE run_id = ?", (run_id,))
        conn.executemany(
            """
            INSERT INTO run_stages (run_id, seq, stage, label, status, summary, payload_json)
            VALUES (?, ?, ?, ?, ?, ?, '{}')
            """,
            [(run_id, s.seq, s.stage, s.label, s.status, s.summary) for s in stages],
        )


def save_stage(run_id: str, stage: StageRecord) -> None:
    with connect() as conn:
        conn.execute(
            """
            UPDATE run_stages SET
                status=?, started_at=?, finished_at=?, duration_ms=?,
                summary=?, payload_json=?, error=?
            WHERE run_id=? AND seq=?
            """,
            (
                stage.status, stage.started_at, stage.finished_at, stage.duration_ms,
                stage.summary, json.dumps(stage.payload, default=str), stage.error,
                run_id, stage.seq,
            ),
        )


def load_stages(run_id: str) -> list[StageRecord]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM run_stages WHERE run_id = ? ORDER BY seq", (run_id,)
        ).fetchall()
    return [
        StageRecord(
            seq=r["seq"], stage=r["stage"], label=r["label"], status=r["status"],
            started_at=r["started_at"], finished_at=r["finished_at"],
            duration_ms=r["duration_ms"], summary=r["summary"],
            payload=json.loads(r["payload_json"] or "{}"), error=r["error"],
        )
        for r in rows
    ]


def finish_run(result: RunResult) -> None:
    inv = result.normalized
    dec = result.decision
    with connect() as conn:
        conn.execute(
            """
            UPDATE runs SET
                pdf_kind=?, page_count=?, status=?, decision=?, confidence=?,
                vendor_id=?, vendor_name=?, invoice_number=?, invoice_date=?,
                currency=?, total_amount=?, matched_po=?, po_match_score=?,
                failed_rules=?, result_json=?, finished_at=?, duration_ms=?, cached=?
            WHERE run_id=?
            """,
            (
                result.pdf_kind, result.page_count, result.status,
                dec.decision if dec else None,
                dec.confidence if dec else None,
                result.vendor.vendor_id if result.vendor else None,
                (result.vendor.vendor_name if result.vendor and result.vendor.vendor_name
                 else (inv.vendor_name_raw if inv else None)),
                inv.invoice_number if inv else None,
                inv.invoice_date if inv else None,
                inv.currency if inv else None,
                inv.total_amount if inv else None,
                result.po.po_number if result.po else None,
                result.po.score if result.po else None,
                json.dumps(dec.failed_rules if dec else []),
                result.model_dump_json(),
                result.finished_at, result.duration_ms, 1 if result.cached else 0,
                result.run_id,
            ),
        )


def get_run(run_id: str) -> RunResult | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    if row is None:
        return None
    blob = row["result_json"] or "{}"
    if blob and blob != "{}":
        result = RunResult.model_validate_json(blob)
    else:
        # Still running - rebuild the shell from the row so the UI has something.
        result = RunResult(
            run_id=row["run_id"], filename=row["filename"],
            file_sha256=row["file_sha256"], file_bytes=row["file_bytes"] or 0,
            status=row["status"], started_at=row["started_at"],
        )
    # The row is authoritative for status: a stored blob is a snapshot of a
    # finished attempt and would otherwise keep reporting "complete" through a
    # replay of the same run id.
    result.status = row["status"]
    result.stages = load_stages(run_id)
    return result


def list_runs(limit: int = 500) -> list[RunSummary]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM runs ORDER BY started_at DESC, rowid DESC LIMIT ?", (limit,)
        ).fetchall()
    return [
        RunSummary(
            run_id=r["run_id"], filename=r["filename"], status=r["status"],
            started_at=r["started_at"], duration_ms=r["duration_ms"],
            vendor_name=r["vendor_name"], invoice_number=r["invoice_number"],
            invoice_date=r["invoice_date"], currency=r["currency"],
            total_amount=r["total_amount"], matched_po=r["matched_po"],
            po_match_score=r["po_match_score"], decision=r["decision"],
            confidence=r["confidence"], pdf_kind=r["pdf_kind"],
            failed_rules=json.loads(r["failed_rules"] or "[]"),
            cached=bool(r["cached"]),
        )
        for r in rows
    ]


def count_runs() -> int:
    with connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]


def delete_run(run_id: str) -> bool:
    with connect() as conn:
        conn.execute("DELETE FROM po_consumption WHERE run_id = ?", (run_id,))
        conn.execute("DELETE FROM run_stages WHERE run_id = ?", (run_id,))
        cur = conn.execute("DELETE FROM runs WHERE run_id = ?", (run_id,))
        return cur.rowcount > 0


# --------------------------------------------------------------------------- #
# Duplicate detection (R7)
# --------------------------------------------------------------------------- #

def delete_runs_by_hash(file_sha256: str) -> int:
    """Forget every completed run of one exact file, and un-book what it spent.

    The prepared scenarios each advertise an expected verdict, and that verdict
    is the one the invoice earns on its own merits. Once a file is in history
    it can no longer earn it: R7 sees the earlier submission and R6 sees the PO
    budget that submission consumed, so a scenario labelled APPROVE returns
    REJECT - correct behaviour reported against a promise the UI had already
    broken.

    Re-running a prepared scenario therefore clears that scenario's own prior
    runs first. Only the identical file is touched, so the cases that depend on
    *another* file being in history still work: the near-duplicate still finds
    the Nalanda original, and the split-PO case still reads the seeded prior
    billing. An arbitrary upload is never cleared - a real duplicate must still
    be caught.
    """
    with connect() as conn:
        rows = conn.execute(
            "SELECT run_id FROM runs WHERE file_sha256 = ?", (file_sha256,)
        ).fetchall()
        ids = [r[0] for r in rows]
        for run_id in ids:
            conn.execute("DELETE FROM po_consumption WHERE run_id = ?", (run_id,))
            conn.execute("DELETE FROM run_stages WHERE run_id = ?", (run_id,))
            conn.execute("DELETE FROM runs WHERE run_id = ?", (run_id,))
        return len(ids)


def delete_runs_by_filename_prefix(prefix: str) -> int:
    """Forget every run of the generated test invoices for one PO.

    Same reasoning as `delete_runs_by_hash`, one level up. A generated test
    invoice carries a fresh invoice number each time, so it is never a
    byte-identical duplicate - but it *is* a same-vendor, same-amount invoice
    on the same day, which is exactly what R7's near-duplicate check is for,
    and the previous attempt has already booked consumption that R6 would then
    read. Both are correct readings of a history the button itself created, so
    the button clears its own history first and the demonstrated verdict is the
    one the invoice earns on a clean slate.

    Scoped to the `test-invoice-<PO>-` prefix, so an uploaded invoice is never
    touched and a real duplicate is still caught.
    """
    # `_` and `%` are LIKE wildcards, and this prefix carries a PO number - so
    # this is only a prefix match because `main.PO_NUMBER_RE` rejects both. If
    # that pattern is ever widened, escape them here.
    #
    # Runs still in flight are left alone. Deleting one mid-pipeline does not
    # stop it: it goes on to stage 7 and books its consumption against a
    # `run_id` that no longer has a row, and nothing afterwards can find that
    # ledger entry to undo it. The caller refuses rather than racing.
    with connect() as conn:
        rows = conn.execute(
            "SELECT run_id FROM runs WHERE filename LIKE ? AND status != 'running'",
            (f"{prefix}%",),
        ).fetchall()
        ids = [r[0] for r in rows]
        for run_id in ids:
            conn.execute("DELETE FROM po_consumption WHERE run_id = ?", (run_id,))
            conn.execute("DELETE FROM run_stages WHERE run_id = ?", (run_id,))
            conn.execute("DELETE FROM runs WHERE run_id = ?", (run_id,))
    return len(ids)


def running_runs_for_prefix(prefix: str) -> list[str]:
    """Runs of the generated test invoices for one PO that have not finished.

    Same prefix convention, and the same wildcard note, as
    `delete_runs_by_filename_prefix`.
    """
    with connect() as conn:
        rows = conn.execute(
            "SELECT run_id FROM runs WHERE filename LIKE ? AND status = 'running'",
            (f"{prefix}%",),
        ).fetchall()
    return [r[0] for r in rows]


def running_runs_for_po(po_number: str) -> list[str]:
    """Any unfinished run that has already matched this PO.

    A run that is still going may yet book consumption against the PO, and
    `po_consumption.po_number` is a foreign key with enforcement on - so
    deleting the PO underneath it turns the run's own decision stage into an
    IntegrityError the user sees as a raw database message.
    """
    with connect() as conn:
        rows = conn.execute(
            "SELECT run_id FROM runs WHERE status = 'running' AND matched_po = ?",
            (po_number,),
        ).fetchall()
    return [r[0] for r in rows]


def find_run_by_hash(file_sha256: str, exclude_run_id: str | None = None) -> dict | None:
    """Exact duplicate: the same bytes were submitted before."""
    sql = (
        "SELECT run_id, filename, started_at, decision, invoice_number, total_amount "
        "FROM runs WHERE file_sha256 = ? AND status = 'complete'"
    )
    params: list = [file_sha256]
    if exclude_run_id:
        sql += " AND run_id != ?"
        params.append(exclude_run_id)
    sql += " ORDER BY started_at LIMIT 1"
    with connect() as conn:
        row = conn.execute(sql, params).fetchone()
    return dict(row) if row else None


def find_extraction_by_hash(file_sha256: str, exclude_run_id: str | None = None) -> dict | None:
    """Content-addressed extraction cache.

    The same bytes always extract to the same fields, so a second submission of
    a file we have already read costs zero LLM calls. This is what makes a live
    demo instant and idempotent, and it keeps a re-run from burning quota.

    Note this is *not* the duplicate check - rule R7 does that separately and
    draws the opposite conclusion from the same fact. Reusing the extraction is
    an efficiency; recognising the resubmission is a control.
    """
    sql = (
        "SELECT run_id, result_json FROM runs "
        "WHERE file_sha256 = ? AND status = 'complete' AND result_json != '{}'"
    )
    params: list = [file_sha256]
    if exclude_run_id:
        sql += " AND run_id != ?"
        params.append(exclude_run_id)
    sql += " ORDER BY started_at LIMIT 1"
    with connect() as conn:
        row = conn.execute(sql, params).fetchone()
    if not row:
        return None
    try:
        payload = json.loads(row["result_json"])
    except json.JSONDecodeError:
        return None
    if not payload.get("extraction"):
        return None
    return {
        "run_id": row["run_id"],
        "extraction": payload["extraction"],
        # The *true* origin of these values, which is what a caller needs to
        # decide whether reusing them is honest - not merely that they came from
        # the cache. A cache hit on a cache hit reports what it inherited, so a
        # fixture cannot be laundered into looking like real extraction by being
        # copied twice.
        "origin": payload.get("extraction_origin")
        or payload.get("extraction_source")
        or "live",
    }


def find_near_duplicate(
    *, vendor_id: str | None, total_amount: float | None, invoice_date: str | None,
    window_days: int, amount_tolerance: float, exclude_run_id: str | None = None,
) -> dict | None:
    """Near duplicate: same vendor, same amount, inside the date window, but a
    *different* invoice number. This is the case a hash check cannot see - a
    vendor re-issuing the same bill under a new number."""
    if not vendor_id or total_amount is None or not invoice_date:
        return None
    try:
        centre = datetime.fromisoformat(invoice_date).date()
    except ValueError:
        return None
    lo = (centre - timedelta(days=window_days)).isoformat()
    hi = (centre + timedelta(days=window_days)).isoformat()

    sql = """
        SELECT run_id, filename, invoice_number, invoice_date, total_amount, started_at
        FROM runs
        WHERE vendor_id = ? AND status = 'complete'
          AND total_amount IS NOT NULL
          AND ABS(total_amount - ?) <= ?
          AND invoice_date BETWEEN ? AND ?
    """
    params: list = [vendor_id, total_amount, amount_tolerance, lo, hi]
    if exclude_run_id:
        sql += " AND run_id != ?"
        params.append(exclude_run_id)
    sql += " ORDER BY started_at LIMIT 1"
    with connect() as conn:
        row = conn.execute(sql, params).fetchone()
    return dict(row) if row else None


# --------------------------------------------------------------------------- #
# Telemetry
# --------------------------------------------------------------------------- #

def record_llm_event(
    *, run_id: str | None, model: str, latency_ms: int,
    input_tokens: int | None, output_tokens: int | None, cached: bool,
) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO llm_events (run_id, model, latency_ms, input_tokens,
                                    output_tokens, cached, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (run_id, model, latency_ms, input_tokens, output_tokens,
             1 if cached else 0, _now()),
        )


def telemetry_summary() -> dict:
    with connect() as conn:
        totals = conn.execute(
            """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN cached=1 THEN 1 ELSE 0 END) AS cached_total,
                   COALESCE(SUM(input_tokens), 0)  AS input_tokens,
                   COALESCE(SUM(output_tokens), 0) AS output_tokens
            FROM llm_events
            """
        ).fetchone()
        per_model = conn.execute(
            """
            SELECT model,
                   COUNT(*) AS calls,
                   COALESCE(SUM(input_tokens), 0)  AS input_tokens,
                   COALESCE(SUM(output_tokens), 0) AS output_tokens
            FROM llm_events WHERE cached = 0 GROUP BY model
            """
        ).fetchall()
        latencies = [
            r["latency_ms"] for r in conn.execute(
                "SELECT latency_ms FROM llm_events WHERE cached=0 ORDER BY latency_ms"
            ).fetchall()
        ]

    def pct(vals: list[int], p: float) -> int | None:
        if not vals:
            return None
        k = max(0, min(len(vals) - 1, int(round(p * (len(vals) - 1)))))
        return vals[k]

    cost = 0.0
    models: list[dict] = []
    for r in per_model:
        in_rate, out_rate = price_for(r["model"])
        model_cost = (r["input_tokens"] / 1e6) * in_rate + (r["output_tokens"] / 1e6) * out_rate
        cost += model_cost
        models.append({
            "model": r["model"], "calls": r["calls"],
            "input_tokens": r["input_tokens"], "output_tokens": r["output_tokens"],
            "input_rate_per_mtok": in_rate, "output_rate_per_mtok": out_rate,
            "estimated_cost_usd": round(model_cost, 4),
        })

    total = totals["total"] or 0
    cached_total = totals["cached_total"] or 0
    return {
        "total_llm_calls": total,
        "cache_hits": cached_total,
        "cache_hit_rate": round(cached_total / total, 3) if total else 0.0,
        "input_tokens": totals["input_tokens"] or 0,
        "output_tokens": totals["output_tokens"] or 0,
        "estimated_cost_usd": round(cost, 4),
        "p50_latency_ms": pct(latencies, 0.50),
        "p95_latency_ms": pct(latencies, 0.95),
        "by_model": models,
    }


def dashboard_stats() -> dict:
    """The stat tiles. `hours_saved` is the one number a non-technical buyer
    actually cares about, so its assumption is stated inline and echoed in the
    UI rather than buried."""
    minutes_per_manual_invoice = 9.0   # AP clerk: open PDF, look up PO, key it in, file it
    with connect() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN decision='APPROVE' THEN 1 ELSE 0 END) AS approved,
                   SUM(CASE WHEN decision='FLAG'    THEN 1 ELSE 0 END) AS flagged,
                   SUM(CASE WHEN decision='REJECT'  THEN 1 ELSE 0 END) AS rejected,
                   AVG(duration_ms) AS avg_ms,
                   COALESCE(SUM(CASE WHEN decision='APPROVE' THEN total_amount ELSE 0 END), 0)
                       AS approved_value
            FROM runs WHERE status='complete'
            """
        ).fetchone()

    total = row["total"] or 0
    approved = row["approved"] or 0
    return {
        "total_runs": total,
        "approved": approved,
        "flagged": row["flagged"] or 0,
        "rejected": row["rejected"] or 0,
        "auto_approved_pct": round(approved / total * 100, 1) if total else 0.0,
        "avg_duration_ms": int(row["avg_ms"]) if row["avg_ms"] else None,
        "approved_value": round(float(row["approved_value"] or 0.0), 2),
        "hours_saved": round(approved * minutes_per_manual_invoice / 60.0, 1),
        "hours_saved_basis": (
            f"{minutes_per_manual_invoice:.0f} minutes of manual handling per "
            f"auto-approved invoice, counted only for invoices a human never had to open"
        ),
    }


# --------------------------------------------------------------------------- #
# Live config
# --------------------------------------------------------------------------- #

def load_config_overrides() -> dict:
    with connect() as conn:
        row = conn.execute("SELECT value FROM app_config WHERE key='rule_config'").fetchone()
    return json.loads(row["value"]) if row else {}


def save_config_overrides(values: dict) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO app_config (key, value) VALUES ('rule_config', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (json.dumps(values),),
        )


if __name__ == "__main__":
    init_db()
    print(f"initialised db at {DB_PATH}")
