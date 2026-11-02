"""FastAPI entrypoint: JSON API, SSE run stream, and the static frontend.

One container serves the API and the UI from the same origin. That is a
deployment decision, not laziness: a single service means one URL, no CORS
story, and nothing to go wrong during a live demo.
"""
from __future__ import annotations

import asyncio
import json
import hashlib
import logging
import re
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import ClassVar

from fastapi import Body, FastAPI, HTTPException, Request, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, field_validator, model_validator

from . import (
    config as config_mod, db, explain, invoice_pdf, normalize, pipeline, rules,
    seed, state,
)
from .config import (
    DATA_DIR,
    EXTRACTION_EFFORT,
    EXTRACTION_MODE,
    FRONTEND_DIR,
    PROVIDERS,
    SAMPLE_INVOICE_DIR,
    STAGE_DELAY_MS,
    active_model,
    active_provider,
    provider_key_present,
)
from .events import broker
from .models import PROVIDER_LABELS, RunResult, RunSummary

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s - %(message)s"
)
log = logging.getLogger("api")

MAX_UPLOAD_BYTES = 20 * 1024 * 1024   # the API accepts 32 MB; stay well inside it

app = FastAPI(title="PS-1 Invoice Processing", version="1.0.0")

app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

# Hold references to in-flight pipeline tasks so they are not garbage collected
# mid-run (asyncio only keeps weak references to tasks).
_tasks: set[asyncio.Task] = set()

# Summaries are deterministic for a finished run, so generate each one once.
# Keeps a reviewer clicking between tabs from re-billing the same tokens.
explain_cache: dict[str, dict] = {}


@app.on_event("startup")
def _startup() -> None:
    db.init_db()
    seed.seed_masters_if_empty()
    state.load_from_db()
    log.info(
        "ready - %d vendors, %d POs, %d runs, model=%s",
        len(db.list_vendors()), len(db.list_pos()), db.count_runs(), active_model(),
    )


# --------------------------------------------------------------------------- #
# Health / meta
# --------------------------------------------------------------------------- #

def _provider_options() -> list[dict]:
    """Every provider the UI can offer, and whether it is usable right now.

    A provider with no key is listed but disabled rather than hidden: the point
    of the control is to show the seam exists, and silently dropping an option
    would just raise the question of where it went.
    """
    return [
        {
            "id": name,
            "label": PROVIDER_LABELS.get(name, name),
            "available": provider_key_present(name),
            "active": name == active_provider(),
            # Groq serves text-only models, so a scanned PDF has nothing for it
            # to read. The UI says so before someone picks it and is surprised.
            "reads_images": name != "groq",
        }
        for name in PROVIDERS
    ]


@app.post("/api/provider")
def set_provider(payload: dict = Body(...)) -> dict:
    """Switch which model provider stage 2 calls.

    Process-local and not persisted: this is a demo control that makes the
    provider seam visible, not a deployment setting. Runs already in the
    database keep whatever read them - the switch applies to the next run, and
    each run's stage 2 records the model that actually ran.
    """
    name = str(payload.get("provider", "")).strip().lower()
    try:
        active = config_mod.set_active_provider(name)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not provider_key_present(active):
        log.warning("switched to %s, which has no API key configured", active)
    log.info("extraction provider switched to %s (%s)", active, active_model())
    return {
        "provider": active,
        "model": active_model(),
        "providers": _provider_options(),
        "key_present": provider_key_present(active),
    }


@app.get("/api/health")
def health() -> dict:
    return {
        "ok": True,
        "model": active_model(),
        "provider": active_provider(),
        "providers": _provider_options(),
        "effort": EXTRACTION_EFFORT,
        "extraction_mode": EXTRACTION_MODE,
        "api_key_present": provider_key_present(active_provider()),
        "runs": db.count_runs(),
        "vendors": len(db.list_vendors()),
        "purchase_orders": len(db.list_pos()),
        "stage_delay_ms": STAGE_DELAY_MS,
        "time": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


@app.get("/api/rules")
def get_rules() -> dict:
    """The rule set as data, with the thresholds currently in force spliced in.

    The UI renders this verbatim, so the documented rules and the applied rules
    cannot drift apart.
    """
    return {
        "rules": rules.catalog_with_thresholds(state.current()),
        "config": state.current().to_dict(),
        "decision_logic": (
            "Worst severity wins. Any REJECT-severity failure rejects; otherwise any "
            "FLAG-severity failure flags; otherwise approve."
        ),
    }


@app.get("/api/config")
def get_config() -> dict:
    return {"config": state.current().to_dict(), "defaults": rules.RuleConfig().to_dict()}


class ConfigPatch(BaseModel):
    po_tolerance_pct: float | None = None
    po_over_tolerance_reject_pct: float | None = None
    duplicate_window_days: int | None = None
    confidence_floor: float | None = None
    arithmetic_tolerance: float | None = None
    stale_invoice_days: int | None = None
    po_match_min_score: float | None = None
    vendor_match_min_score: float | None = None


@app.patch("/api/config")
def patch_config(patch: ConfigPatch) -> dict:
    """Change a threshold at runtime. Re-run an invoice afterwards and the
    decision changes - which is the point: the rules are configuration, not
    hardcoded output."""
    config, changed = state.update(patch.model_dump(exclude_none=True))
    return {"config": config.to_dict(), "changed": changed}


@app.post("/api/config/reset")
def reset_config() -> dict:
    return {"config": state.reset().to_dict(), "changed": ["reset to defaults"]}


# --------------------------------------------------------------------------- #
# Procurement masters
# --------------------------------------------------------------------------- #

PO_NUMBER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9/-]{2,23}$")


class NewPO(BaseModel):
    """A purchase order raised through the UI.

    Validated here rather than in the handler so a bad request is a 422 with
    the offending field named, and so the same rules apply to anything else
    that posts to this endpoint. Only INR is accepted: every threshold, every
    rule message and the whole PO ledger are single-currency (see README >
    Assumptions), and accepting a second currency here would produce a PO that
    silently compares wrong.
    """

    po_number: str
    vendor_id: str
    description: str = ""
    po_value: float
    issued_date: str = ""
    expiry_date: str | None = None
    currency: str = "INR"
    status: str = "open"

    @field_validator("po_number")
    @classmethod
    def _po_number(cls, value: str) -> str:
        cleaned = value.strip().upper()
        if not PO_NUMBER_RE.match(cleaned):
            raise ValueError(
                "A PO number is 3 to 24 characters of letters, digits, dashes or slashes - "
                "for example PO-9001."
            )
        return cleaned

    @field_validator("vendor_id")
    @classmethod
    def _vendor(cls, value: str) -> str:
        cleaned = value.strip().upper()
        if not cleaned:
            raise ValueError("Choose the vendor this PO is raised on.")
        return cleaned

    @field_validator("description")
    @classmethod
    def _description(cls, value: str) -> str:
        return value.strip()[:120]

    # A floor, not just a positivity check. The generated test invoices bill a
    # fixed fraction of the PO value plus a small absolute amount, so that two
    # POs of the same value do not produce byte-identical invoices that R7
    # correctly reports as near duplicates (see `invoice_pdf.make_test_invoice`).
    # That absolute amount is a rounding error against a real purchase order and
    # dominates a trivial one: below about 80 rupees it pushes the 7.5%-over
    # variant past the reject threshold, so the invoice advertised as FLAG comes
    # back REJECT. 1,000 is the floor with comfortable margin, and
    # `tests/test_po_entry.py` asserts the bands hold from exactly here.
    MIN_PO_VALUE: ClassVar[float] = 1_000.0

    @field_validator("po_value")
    @classmethod
    def _value(cls, value: float) -> float:
        if not value or value <= 0:
            raise ValueError("The PO value has to be a positive amount in rupees.")
        if value < NewPO.MIN_PO_VALUE:
            raise ValueError(
                f"A purchase order here starts at INR {NewPO.MIN_PO_VALUE:,.0f}. Below that the "
                f"generated test invoices cannot land in the bands they advertise."
            )
        if value > 1e10:
            raise ValueError("That is over 1,000 crore; check the figure.")
        return round(float(value), 2)

    @field_validator("currency")
    @classmethod
    def _currency(cls, value: str) -> str:
        cleaned = (value or "INR").strip().upper()
        if cleaned != "INR":
            raise ValueError("This build is INR-only; see README > Assumptions.")
        return cleaned

    @field_validator("status")
    @classmethod
    def _status(cls, value: str) -> str:
        cleaned = (value or "open").strip().lower()
        if cleaned not in ("open", "closed"):
            raise ValueError("A PO is either open or closed.")
        return cleaned

    @field_validator("issued_date", "expiry_date")
    @classmethod
    def _date(cls, value: str | None) -> str | None:
        if value in (None, ""):
            return value
        try:
            return date.fromisoformat(value.strip()).isoformat()
        except ValueError as exc:
            raise ValueError(f"'{value}' is not a date in YYYY-MM-DD form.") from exc

    @model_validator(mode="after")
    def _window(self) -> "NewPO":
        # An expiry before the issue date gives the PO an empty validity window,
        # and stage 5 scores an invoice date against that window - so every
        # invoice against this PO would score badly for a reason the person who
        # typed it would never guess.
        if not self.issued_date:
            self.issued_date = date.today().isoformat()
        if self.expiry_date and self.expiry_date < self.issued_date:
            raise ValueError("The PO cannot expire before it was issued.")
        return self


class TestInvoiceRequest(BaseModel):
    variant: str = "match"


# 15 characters: state code, PAN, entity number, a literal Z, checksum.
GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][0-9A-Z]Z[0-9A-Z]$")


class NewVendor(BaseModel):
    """A supplier added through the UI, so a PO can be raised against someone
    who is not in the seeded master.

    Added as `approved`, because an added-and-blocked vendor is a PO nobody
    could ever pay - the blocked case already exists in the seeded data
    (Tri-City Electricals) precisely so it can be demonstrated. The id is
    generated rather than asked for: nobody should have to invent an
    identifier to add a supplier.
    """

    name: str
    aliases: str = ""
    gstin: str | None = None
    payment_terms_days: int = 30

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        cleaned = " ".join((value or "").split())
        if len(cleaned) < 3:
            raise ValueError("Give the vendor its registered name.")
        # Stage 4 matches on the canonical form - lower-cased, punctuation
        # stripped, entity suffixes removed. A name that is nothing *but*
        # entity suffix canonicalises to nothing, so the vendor could never be
        # resolved, and two such names would be indistinguishable from each
        # other as well.
        if not normalize.canonical_vendor_name(cleaned):
            raise ValueError(
                f"'{cleaned}' is only a company suffix. Give the trading name as well."
            )
        return cleaned[:120]

    @field_validator("gstin")
    @classmethod
    def _gstin(cls, value: str | None) -> str | None:
        cleaned = (value or "").strip().upper()
        if not cleaned:
            return None
        if not GSTIN_RE.match(cleaned):
            # Stage 4 resolves on GSTIN first and treats an exact match as
            # settled identity, so a malformed one is worse than none at all.
            raise ValueError(
                "A GSTIN is 15 characters, like 27AABCS1234A1Z5. Leave it blank if you "
                "do not have it - the vendor then resolves by name."
            )
        return cleaned

    @field_validator("payment_terms_days")
    @classmethod
    def _terms(cls, value: int) -> int:
        if not 0 <= int(value) <= 365:
            raise ValueError("Payment terms are between 0 and 365 days.")
        return int(value)

    def alias_list(self) -> list[str]:
        """Aliases arrive as one comma-separated field, because that is how a
        person types them. They matter: stage 4 fuzzy-matches the printed name
        against the registered name *and* every alias, so "Sharma Steel" on the
        invoice resolves to "Sharma Steel Works Private Limited"."""
        return [a for a in (" ".join(part.split()) for part in self.aliases.split(",")) if a][:8]


@app.get("/api/vendors")
def get_vendors() -> list[dict]:
    return db.list_vendors()


@app.post("/api/vendors", status_code=201)
def create_vendor(payload: NewVendor) -> dict:
    """Add an approved vendor, so a PO can be raised against a supplier who is
    not in the seeded master.

    Refuses a name or GSTIN that already resolves to an existing vendor. Two
    vendors the matcher cannot tell apart is the one state this master must
    never be in: stage 4 would score them within a hair of each other and the
    winner would be whichever row came back first.
    """
    name = payload.name
    gstin = payload.gstin
    existing = db.list_vendors()

    if gstin:
        clash = next((v for v in existing if (v.get("gstin") or "").upper() == gstin), None)
        if clash:
            raise HTTPException(
                409, f"{clash['name']} ({clash['vendor_id']}) already holds GSTIN {gstin}.")

    # Every name this vendor could be recognised by, not just its registered
    # one: stage 4 scores the printed name against the registered name *and*
    # every alias, so an alias that collides with an existing vendor produces
    # two candidates within a hair of each other and the winner is decided by
    # sort stability. That is the one state this master must never be in.
    aliases = payload.alias_list()
    incoming = {
        normalize.canonical_vendor_name(value): value
        for value in [name, *aliases]
        if normalize.canonical_vendor_name(value)
    }
    for vendor in existing:
        for known in [vendor["name"], *vendor.get("aliases", [])]:
            canonical = normalize.canonical_vendor_name(known)
            if canonical and canonical in incoming:
                offending = incoming[canonical]
                raise HTTPException(
                    409,
                    f"'{offending}' already resolves to {vendor['name']} "
                    f"({vendor['vendor_id']}). Add it as an alias of that vendor rather "
                    f"than as a second one.",
                )

    try:
        created = db.insert_vendor(
            name=name, aliases=aliases, gstin=gstin,
            payment_terms_days=payload.payment_terms_days,
        )
    except sqlite3.IntegrityError as exc:          # lost a race for the id
        raise HTTPException(
            409, "Another vendor was added at the same moment. Try again.") from exc
    log.info("vendor added in the UI: %s %s", created["vendor_id"], created["name"])
    return created


@app.get("/api/pos")
def get_pos() -> list[dict]:
    return db.list_pos()


@app.get("/api/pos/variants")
def po_test_variants() -> list[dict]:
    """The test invoices that can be generated against a PO, and what each one
    is for. Declared before the `{po_number}` route so the literal path wins."""
    return [
        {"id": key, "label": spec["label"], "expect": spec["expect"],
         "why": spec["why"], "ratio": spec["target_ratio"]}
        for key, spec in invoice_pdf.VARIANTS.items()
    ]


@app.post("/api/pos", status_code=201)
def create_po(payload: NewPO) -> dict:
    """Raise a purchase order from the UI.

    Writes `source='user'`, which is what keeps it distinguishable from the
    twelve seeded demo POs everywhere it is shown. The vendor is chosen from
    the approved master rather than typed, because a PO against a vendor the
    system does not know can never pay an invoice - R3 would reject the vendor
    and R4 would find no PO - so free text would let someone create a PO that
    silently cannot work.
    """
    vendors = {v["vendor_id"]: v for v in db.list_vendors()}
    vendor = vendors.get(payload.vendor_id)
    if vendor is None:
        raise HTTPException(
            400,
            f"'{payload.vendor_id}' is not on the vendor master. A purchase order has to "
            f"name an approved vendor, or no invoice against it could ever be paid.",
        )
    if vendor["status"] != "approved":
        raise HTTPException(
            400,
            f"{vendor['name']} is on the master but {vendor['status']}. An invoice against "
            f"this PO would be rejected by R3 whatever else it got right.",
        )
    if db.get_po(payload.po_number) is not None:
        raise HTTPException(409, f"{payload.po_number} already exists.")

    try:
        created = db.insert_po(
            po_number=payload.po_number, vendor_id=payload.vendor_id,
            description=payload.description, po_value=payload.po_value,
            issued_date=payload.issued_date, expiry_date=payload.expiry_date,
            currency=payload.currency, status=payload.status, source="user",
        )
    except sqlite3.IntegrityError as exc:          # lost a race with another client
        raise HTTPException(409, f"{payload.po_number} already exists.") from exc

    log.info("PO raised in the UI: %s for %s, value %.2f",
             created["po_number"], created["vendor_id"], created["po_value"])
    return created


@app.get("/api/pos/{po_number}")
def get_po(po_number: str) -> dict:
    po = db.get_po(po_number)
    if po is None:
        raise HTTPException(404, f"PO {po_number} not found")
    return {**po, "consumption": db.po_consumption_detail(po_number)}


@app.delete("/api/pos/{po_number}")
def delete_po(po_number: str) -> dict:
    """Remove a PO raised in the UI, and whatever was billed against it.

    Seeded POs are refused: the prepared scenarios advertise verdicts that
    depend on them, so deleting one would quietly break a demo elsewhere in the
    app. Reset the demo data instead.
    """
    po = db.get_po(po_number)
    if po is None:
        raise HTTPException(404, f"PO {po_number} not found")
    if po.get("source") != "user":
        raise HTTPException(
            400,
            f"{po_number} is seeded demo data, not yours. The prepared scenarios are scored "
            f"against it; use 'Reset purchase orders' to restore the demo state instead.",
        )
    # Refuse while anything is still running against it. `po_consumption`
    # holds a foreign key on `pos(po_number)` with enforcement on, so deleting
    # the PO under an in-flight run turns that run's decision stage into an
    # IntegrityError the user sees as a raw database message.
    busy = db.running_runs_for_po(po_number) + db.running_runs_for_prefix(
        f"test-invoice-{po_number}-")
    if busy:
        raise HTTPException(
            409,
            f"{po_number} has a run still in progress ({busy[0]}). Wait for it to finish, "
            f"then delete it.",
        )

    cleared = db.delete_runs_by_filename_prefix(f"test-invoice-{po_number}-")
    db.delete_po(po_number)
    log.info("PO %s deleted, %d generated test run(s) cleared", po_number, cleared)
    return {"deleted": po_number, "test_runs_cleared": cleared}


@app.post("/api/pos/{po_number}/test-invoice")
async def run_test_invoice(po_number: str, payload: TestInvoiceRequest) -> dict:
    """Generate an invoice for this PO and put it through the real pipeline.

    This is the other half of raising a PO: a PO with no invoice against it
    demonstrates nothing. The PDF is written here (`invoice_pdf.py`), with a
    real text layer, and then goes through exactly the same code path as an
    upload - `pipeline.start` does not know and cannot tell where the bytes
    came from.

    Restricted to user-raised POs on purpose. Generating one against a seeded
    PO would book consumption the prepared scenarios are scored against, and
    `tools/check_scenarios.py` would start failing for a reason nobody would
    connect to a button in the UI.
    """
    po = db.get_po(po_number)
    if po is None:
        raise HTTPException(404, f"PO {po_number} not found")
    if po.get("source") != "user":
        raise HTTPException(
            400,
            f"{po_number} is one of the seeded demo POs. Generated invoices are only offered "
            f"for POs you raised, so the prepared scenarios keep the budgets they are scored "
            f"against. Raise a PO of your own and try it there.",
        )
    vendor = next((v for v in db.list_vendors() if v["vendor_id"] == po["vendor_id"]), None)
    if vendor is None:
        raise HTTPException(409, f"{po_number} names vendor {po['vendor_id']}, who is not on the master.")

    try:
        generated = invoice_pdf.make_test_invoice(
            po=po, vendor=vendor, variant=payload.variant,
            sequence=int(datetime.now().strftime("%H%M%S")),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    # One at a time per PO. The three variant buttons share one filename
    # prefix, so starting a second while the first is still in the pipeline
    # would clear the first's rows out from under it - and the first would go
    # on to book consumption against a run that no longer exists, leaving a
    # charge against this PO that nothing can find to undo.
    prefix = f"test-invoice-{po_number}-"
    in_flight = db.running_runs_for_prefix(prefix)
    if in_flight:
        raise HTTPException(
            409,
            f"A generated invoice for {po_number} is still being processed "
            f"({in_flight[0]}). Wait for it to finish, then try again.",
        )

    # Then clear this button's own finished history - see
    # `db.delete_runs_by_filename_prefix` for why that is the honest thing to
    # do rather than a way of hiding a duplicate.
    cleared = db.delete_runs_by_filename_prefix(prefix)

    run_id = pipeline.new_run_id()
    _schedule(run_id, generated["filename"], generated["pdf"], replay=False)
    meta = {k: v for k, v in generated.items() if k != "pdf"}
    return {"run_id": run_id, "cleared_prior_runs": cleared, "po_number": po_number, **meta}


# --------------------------------------------------------------------------- #
# Runs
# --------------------------------------------------------------------------- #

def _schedule(run_id: str, filename: str, data: bytes, *, replay: bool) -> None:
    """Register the run synchronously, then run the stages in the background.

    The two-step matters: `start()` writes the run row and its pending stages
    before this function returns, so the client that just got a run id can
    immediately fetch it or attach to its event stream without racing.
    """
    run = pipeline.start(run_id, filename, data, state.current(), replay=replay)
    task = asyncio.create_task(run.run())
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def _validate_pdf(filename: str, data: bytes) -> None:
    if not data:
        raise HTTPException(400, "Empty file.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            413, f"File is {len(data) / 1e6:.1f} MB; the limit is {MAX_UPLOAD_BYTES / 1e6:.0f} MB."
        )
    if not data.startswith(b"%PDF"):
        raise HTTPException(
            400,
            f"'{filename}' is not a PDF. This pipeline reads PDFs natively; "
            f"images and Office files are out of scope.",
        )


@app.post("/api/runs")
async def create_run(file: UploadFile = File(...)) -> dict:
    """Accept an invoice PDF and start the pipeline.

    Returns immediately with a run id; the client then opens the SSE stream to
    watch the stages. The upload response deliberately does not wait for the
    decision - the whole point of the run view is that you see the work happen.
    """
    data = await file.read()
    filename = file.filename or "upload.pdf"
    _validate_pdf(filename, data)

    run_id = pipeline.new_run_id()
    _schedule(run_id, filename, data, replay=False)
    return {"run_id": run_id, "filename": filename, "bytes": len(data)}


@app.post("/api/runs/{run_id}/replay")
async def replay_run(run_id: str) -> dict:
    """Re-run an existing run from its stored PDF, reusing the same run id.

    This is how to demonstrate a threshold change: edit the PO tolerance, replay
    the same invoice, watch the decision flip. Because the run id is reused, the
    duplicate rule correctly treats it as a replay rather than a resubmission,
    and any PO consumption the previous attempt booked is reversed first.
    """
    existing = db.get_run(run_id)
    if existing is None:
        raise HTTPException(404, f"run {run_id} not found")
    path = pipeline.blob_path(existing.file_sha256)
    if not path.exists():
        raise HTTPException(410, "The stored PDF for this run is no longer on disk.")
    _schedule(run_id, existing.filename, path.read_bytes(), replay=True)
    return {"run_id": run_id, "filename": existing.filename, "replay": True}


@app.get("/api/runs")
def list_runs(decision: str | None = None) -> list[RunSummary]:
    runs = db.list_runs()
    if decision:
        wanted = decision.upper()
        runs = [r for r in runs if (r.decision or "").upper() == wanted]
    return runs


@app.get("/api/runs/{run_id}")
def get_run(run_id: str) -> RunResult:
    run = db.get_run(run_id)
    if run is None:
        raise HTTPException(404, f"run {run_id} not found")
    return run


@app.delete("/api/runs/{run_id}")
def delete_run(run_id: str) -> dict:
    if not db.delete_run(run_id):
        raise HTTPException(404, f"run {run_id} not found")
    return {"ok": True, "run_id": run_id}


@app.post("/api/runs/{run_id}/explain")
async def explain_run(run_id: str) -> dict:
    """A short plain-English summary of what is wrong with a flagged invoice.

    This is the one place a model is asked to *describe* rather than extract -
    and it still decides nothing. The verdict and the failing rules are already
    fixed by the time this runs; the model is given them and asked to say what
    they mean in two sentences for someone who does not want to read nine rule
    rows. If it is unavailable the UI falls back to the deterministic summary,
    because an explanation is a convenience and the decision is not.
    """
    run = db.get_run(run_id)
    if run is None:
        raise HTTPException(404, f"run {run_id} not found")
    if run.decision is None:
        raise HTTPException(409, "This run has not reached a decision yet.")

    cached = explain_cache.get(run_id)
    if cached:
        return cached

    try:
        summary = await asyncio.to_thread(explain.summarise, run)
    except Exception as exc:
        log.warning("explain failed for %s: %s", run_id, exc)
        raise HTTPException(503, f"Could not generate a summary: {exc}") from exc

    explain_cache[run_id] = summary
    return summary


@app.get("/api/runs/{run_id}/file")
def get_run_file(run_id: str, download: bool = False) -> FileResponse:
    """The stored PDF.

    Served **inline** by default so the UI can embed it beside the extracted
    fields. Passing `filename=` to FileResponse would set
    `Content-Disposition: attachment`, which makes a browser download the file
    rather than render it - fine for a download link, fatal for an <iframe>.
    `?download=1` asks for that behaviour explicitly.
    """
    run = db.get_run(run_id)
    if run is None:
        raise HTTPException(404, f"run {run_id} not found")
    path = pipeline.blob_path(run.file_sha256)
    if not path.exists():
        raise HTTPException(404, "stored PDF not found")
    if download:
        return FileResponse(str(path), media_type="application/pdf", filename=run.filename)
    safe = run.filename.replace('"', "")
    return FileResponse(
        str(path),
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{safe}"'},
    )


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


@app.get("/api/runs/{run_id}/events")
async def run_events(run_id: str, request: Request) -> StreamingResponse:
    """Server-sent events for the live run view.

    Subscribes *before* reading the snapshot so an event that fires in between
    is queued rather than lost. The client applies stage updates by sequence
    number, so a replayed-then-streamed duplicate is harmless.
    """
    async def generator():
        queue = broker.subscribe(run_id)
        try:
            run = db.get_run(run_id)
            if run is None:
                yield _sse("error", {"message": f"run {run_id} not found"})
                return

            yield _sse("snapshot", {
                "run_id": run_id,
                "filename": run.filename,
                "status": run.status,
                "stages": [s.model_dump() for s in run.stages],
                "decision": run.decision.model_dump() if run.decision else None,
            })

            if run.status != "running":
                yield _sse("done", {"run_id": run_id, "status": run.status})
                return

            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    # Comment frame: keeps proxies from closing an idle stream.
                    yield ": keepalive\n\n"
                    if await request.is_disconnected():
                        return
                    continue

                yield _sse(event.get("type", "message"), event)
                if event.get("type") == "done":
                    return
        finally:
            broker.unsubscribe(run_id, queue)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # Tell nginx-style proxies (Render's included) not to buffer, or the
            # stage events arrive all at once at the end.
            "X-Accel-Buffering": "no",
        },
    )


# --------------------------------------------------------------------------- #
# Bundled sample invoices - one-click runs, so a demo never fumbles a file picker
# --------------------------------------------------------------------------- #

@app.get("/api/samples")
def list_samples() -> list[dict]:
    return seed.describe_samples()


@app.get("/api/thumbs/{name}")
def get_thumb(name: str) -> FileResponse:
    """A rendered PNG of one invoice layout, for the Layouts gallery."""
    safe = Path(name).name
    path = DATA_DIR / "thumbs" / safe
    if path.suffix.lower() != ".png" or not path.exists():
        raise HTTPException(404, f"thumbnail '{safe}' not found")
    return FileResponse(str(path), media_type="image/png")


@app.get("/api/samples/{name}/file")
def get_sample_file(name: str, download: bool = False) -> FileResponse:
    """A bundled sample PDF.

    Inline by default so the Layouts tab can show it; `?download=1` hands it
    over as a file, so anyone on the deployed URL can get a real invoice to
    upload without cloning the repository.
    """
    safe = Path(name).name
    path = SAMPLE_INVOICE_DIR / safe
    if path.suffix.lower() != ".pdf" or not path.exists():
        raise HTTPException(404, f"sample '{safe}' not found")
    if download:
        return FileResponse(str(path), media_type="application/pdf", filename=safe)
    return FileResponse(
        str(path),
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{safe}"'},
    )


@app.post("/api/samples/{name}/run")
async def run_sample(name: str) -> dict:
    safe = Path(name).name
    path = SAMPLE_INVOICE_DIR / safe
    if path.suffix.lower() != ".pdf" or not path.exists():
        raise HTTPException(404, f"sample '{safe}' not found")
    pdf_bytes = path.read_bytes()
    # A prepared scenario advertises the verdict it earns on a clean slate, so
    # give it one: drop this file's own earlier runs (and the PO budget they
    # booked) before running. Without this, every scenario returns REJECT on a
    # second click - R7 and R6 reporting correctly on a promise the label had
    # already broken. Only the identical file is cleared, so scenarios that
    # depend on a *different* file being in history are untouched, and an
    # arbitrary upload still gets the full duplicate check.
    meta = next((x for x in seed.describe_samples() if x["file"] == safe), {})

    cleared = db.delete_runs_by_hash(hashlib.sha256(pdf_bytes).hexdigest())
    # Some scenarios are only meaningful relative to another. `resets` names the
    # files whose history would stop this one reaching its advertised verdict;
    # `requires` names the one that has to be in history first for it to mean
    # anything at all. Both are declared in the sample catalogue rather than
    # inferred, because the relation is not symmetric.
    for other in meta.get("resets", []):
        other_path = SAMPLE_INVOICE_DIR / Path(other).name
        if other_path.exists():
            cleared += db.delete_runs_by_hash(
                hashlib.sha256(other_path.read_bytes()).hexdigest()
            )

    prerequisite = meta.get("requires")
    if prerequisite:
        pre_path = SAMPLE_INVOICE_DIR / Path(prerequisite).name
        if pre_path.exists():
            pre_bytes = pre_path.read_bytes()
            pre_hash = hashlib.sha256(pre_bytes).hexdigest()
            if db.find_run_by_hash(pre_hash) is None:
                pre_id = pipeline.new_run_id()
                pre = pipeline.start(
                    pre_id, Path(prerequisite).name, pre_bytes, state.current(), replay=False,
                )
                # Awaited, not scheduled: the dependent run's verdict depends on
                # this one already being in history.
                await pre.run()

    run_id = pipeline.new_run_id()
    _schedule(run_id, safe, pdf_bytes, replay=False)
    return {"run_id": run_id, "filename": safe, "cleared_prior_runs": cleared}


# --------------------------------------------------------------------------- #
# Dashboard + admin
# --------------------------------------------------------------------------- #

@app.get("/api/stats")
def stats() -> dict:
    return db.dashboard_stats()


@app.get("/api/admin/telemetry")
def telemetry() -> dict:
    return {
        **db.telemetry_summary(),
        "model": active_model(),
        "effort": EXTRACTION_EFFORT,
        "stage_delay_ms": STAGE_DELAY_MS,
        "stage_delay_note": (
            "Artificial per-stage pacing so the live run view is watchable. It is added "
            "latency, not work - set STAGE_DELAY_MS=0 for true pipeline timings."
        ),
    }


class ReseedRequest(BaseModel):
    clear_runs: bool = False


@app.post("/api/admin/reseed")
def reseed(req: ReseedRequest) -> dict:
    """Restore the procurement masters to their seeded state.

    Useful between demo takes: approving an invoice books consumption against a
    PO, which changes what the next run of the same invoice decides.
    """
    summary = seed.reseed(clear_runs=req.clear_runs)
    return summary


# --------------------------------------------------------------------------- #
# Static frontend - mounted last so /api routes win
# --------------------------------------------------------------------------- #

if FRONTEND_DIR.exists():

    @app.get("/")
    def root() -> FileResponse:
        return FileResponse(str(FRONTEND_DIR / "index.html"))

    @app.get("/{path:path}")
    def spa(path: str) -> FileResponse:
        target = FRONTEND_DIR / path
        if target.is_file():
            return FileResponse(str(target))
        return FileResponse(str(FRONTEND_DIR / "index.html"))
