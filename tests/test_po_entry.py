"""Raising a purchase order, and the invoice generated to check it.

No LLM, no network, no server. Three things are worth asserting here, and all
three are things that would break silently in a demo rather than loudly in
development:

* the generated PDF is a real PDF with a readable text layer, because stage 3
  verifies quotes against that layer and a broken writer would look like a
  model failure;
* each variant's amount lands inside the band that earns its advertised
  verdict, because the UI states that verdict before the run;
* the `pos.source` column is ALTERed into a database that already exists,
  because the repository ships a populated one and Render uses it as-is.

Run:  python -m pytest tests/ -q
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import date
from io import BytesIO
from pathlib import Path

import pytest
from pypdf import PdfReader

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import db, invoice_pdf  # noqa: E402
from backend.main import NewPO, NewVendor  # noqa: E402
from backend.rules import RuleConfig  # noqa: E402

TODAY = date(2026, 11, 2)

PO = {
    "po_number": "PO-9001",
    "vendor_id": "V-1001",
    "description": "Handrail fabrication - block C",
    "po_value": 250_000.0,
    "currency": "INR",
    "issued_date": "2026-11-01",
    "expiry_date": "2027-02-01",
}
VENDOR = {
    "vendor_id": "V-1001",
    "name": "Sharma Steel Works Private Limited",
    "gstin": "27AABCS1234A1Z5",
}


def generated(variant: str = "match", *, po: dict | None = None, sequence: int = 120000) -> dict:
    return invoice_pdf.make_test_invoice(
        po=po or PO, vendor=VENDOR, variant=variant, sequence=sequence, today=TODAY,
    )


# --------------------------------------------------------------------------- #
# The PDF
# --------------------------------------------------------------------------- #

def test_generated_file_is_a_pdf_one_page_deep():
    out = generated()
    assert out["pdf"].startswith(b"%PDF-")
    assert out["pdf"].rstrip().endswith(b"%%EOF")
    assert len(PdfReader(BytesIO(out["pdf"])).pages) == 1


def test_text_layer_carries_what_the_rules_need_to_read():
    """Everything the pipeline has to find has to be *in* the text layer.

    If this fails, extraction degrades to the scanned-document path and R9
    starts flagging a document that should sail through - which in a demo looks
    like the model got worse.
    """
    out = generated()
    text = PdfReader(BytesIO(out["pdf"])).pages[0].extract_text()
    assert VENDOR["name"] in text
    assert VENDOR["gstin"] in text
    assert PO["po_number"] in text
    assert out["invoice_number"] in text
    assert "02/11/2026" in text                      # the printed invoice date
    assert invoice_pdf.inr(out["total"]) in text     # the printed total
    assert "TAX INVOICE" in text


def test_the_invoice_adds_up_to_within_a_rupee():
    """R2 adds the line items and the tax back up against the stated total."""
    config = RuleConfig()
    for variant in invoice_pdf.VARIANTS:
        figures = invoice_pdf.compose_figures(250_000.0, "Handrail fabrication")
        computed = sum(line["amount"] for line in figures.lines) + figures.tax
        assert abs(computed - figures.total) <= config.arithmetic_tolerance, variant


# --------------------------------------------------------------------------- #
# The variants, against the thresholds that judge them
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("variant", sorted(invoice_pdf.VARIANTS))
def test_each_variant_lands_in_the_band_that_earns_its_verdict(variant):
    config = RuleConfig()
    out = generated(variant)
    variance = out["variance_pct"]
    expect = out["expect"]

    if expect == "APPROVE":
        assert variance <= config.po_tolerance_pct
    elif expect == "FLAG":
        assert config.po_tolerance_pct < variance <= config.po_over_tolerance_reject_pct
    else:
        assert variance > config.po_over_tolerance_reject_pct


@pytest.mark.parametrize("po_value", [1_000.0, 50_000.0, 250_000.0, 10_000_000.0])
def test_the_bands_hold_at_every_plausible_PO_size(po_value):
    """The per-invoice nudge is proportional, so a small PO cannot be pushed
    out of its variant's band by it."""
    config = RuleConfig()
    for variant, spec in invoice_pdf.VARIANTS.items():
        out = generated(variant, po={**PO, "po_value": po_value})
        if spec["expect"] == "APPROVE":
            assert out["variance_pct"] <= config.po_tolerance_pct, (variant, po_value)
        elif spec["expect"] == "FLAG":
            assert config.po_tolerance_pct < out["variance_pct"] <= config.po_over_tolerance_reject_pct
        else:
            assert out["variance_pct"] > config.po_over_tolerance_reject_pct


def test_two_POs_of_the_same_value_do_not_bill_the_same_amount():
    """Otherwise the second one is a same-vendor, same-amount, same-day invoice
    and R7 correctly reports a near duplicate the demo never intended."""
    a = generated(po={**PO, "po_number": "PO-9101"})
    b = generated(po={**PO, "po_number": "PO-9102"})
    assert abs(a["total"] - b["total"]) > RuleConfig().arithmetic_tolerance


def test_an_unknown_variant_is_refused_rather_than_guessed():
    with pytest.raises(ValueError):
        generated("whatever")


# --------------------------------------------------------------------------- #
# Validation of the form that raises the PO
# --------------------------------------------------------------------------- #

def test_po_number_and_vendor_are_normalised():
    po = NewPO(po_number=" po-9001 ", vendor_id=" v-1001 ", po_value=1000)
    assert po.po_number == "PO-9001"
    assert po.vendor_id == "V-1001"


def test_issued_date_defaults_to_today_rather_than_being_required():
    assert NewPO(po_number="PO-9001", vendor_id="V-1001", po_value=1000).issued_date \
        == date.today().isoformat()


@pytest.mark.parametrize("bad", [
    {"po_number": "x!"},                                  # not a PO number
    {"po_number": "AB"},                                  # too short
    {"po_value": 0},                                      # no value
    {"po_value": -5},                                     # negative
    {"po_value": 999},                                    # under the floor
    {"currency": "USD"},                                  # this build is INR-only
    {"status": "paused"},                                 # not a PO status
    {"issued_date": "02/11/2026"},                        # not ISO
    {"issued_date": "2026-11-02", "expiry_date": "2026-10-01"},   # expires first
])
def test_a_PO_that_could_never_pay_an_invoice_is_refused(bad):
    payload = {"po_number": "PO-9001", "vendor_id": "V-1001", "po_value": 1000, **bad}
    with pytest.raises(Exception):
        NewPO(**payload)


def test_a_long_description_is_trimmed_not_rejected():
    po = NewPO(po_number="PO-9001", vendor_id="V-1001", po_value=1000, description="x" * 400)
    assert len(po.description) == 120


# --------------------------------------------------------------------------- #
# The migration
# --------------------------------------------------------------------------- #

def test_source_is_added_to_a_pos_table_that_already_exists(tmp_path):
    """The shipped database has a `pos` table without this column, and
    CREATE TABLE IF NOT EXISTS will not add one."""
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE pos (
                po_number TEXT PRIMARY KEY, vendor_id TEXT NOT NULL, description TEXT,
                po_value REAL NOT NULL, currency TEXT NOT NULL DEFAULT 'INR',
                issued_date TEXT NOT NULL, expiry_date TEXT,
                status TEXT NOT NULL DEFAULT 'open'
            )
            """
        )
        conn.execute(
            "INSERT INTO pos VALUES ('PO-4471','V-1001','steel',1000.0,'INR','2026-08-01',NULL,'open')"
        )

    with sqlite3.connect(path) as conn:
        applied = db._migrate(conn)
        assert "pos.source" in applied
        row = conn.execute("SELECT source FROM pos WHERE po_number='PO-4471'").fetchone()
        assert row[0] == "seed"            # an existing PO is demo data, not yours
        # and running it again is a no-op rather than an error
        assert db._migrate(conn) == []




# --------------------------------------------------------------------------- #
# Adding a vendor
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("gstin", [
    "27AABCS1234A1Z5",      # the seeded Sharma Steel GSTIN
    "29AACFM5678B1Z3",
    "07AAHCO7890E1Z1",
])
def test_a_real_gstin_is_accepted(gstin):
    """Every GSTIN already in the seeded master has to pass the check that
    guards new ones, or the form refuses data the system itself ships."""
    assert NewVendor(name="Some Vendor Pvt Ltd", gstin=gstin).gstin == gstin


@pytest.mark.parametrize("bad", ["27AABCS1234A1Z", "ABC", "27AABCS1234A1Z55", "notagstin12345"])
def test_a_malformed_gstin_is_refused(bad):
    """Stage 4 treats an exact GSTIN match as settled identity, so a malformed
    one is worse than none at all."""
    with pytest.raises(Exception):
        NewVendor(name="Some Vendor Pvt Ltd", gstin=bad)


def test_a_blank_gstin_is_allowed_and_becomes_none():
    assert NewVendor(name="Some Vendor Pvt Ltd", gstin="  ").gstin is None


def test_aliases_are_split_trimmed_and_capped():
    vendor = NewVendor(name="Vectra Engineering Private Limited",
                       aliases="Vectra Engineering,  Vectra Engg , , Vectra")
    assert vendor.alias_list() == ["Vectra Engineering", "Vectra Engg", "Vectra"]


def test_a_vendor_needs_a_name_and_sane_terms():
    with pytest.raises(Exception):
        NewVendor(name="ab")
    with pytest.raises(Exception):
        NewVendor(name="Some Vendor Pvt Ltd", payment_terms_days=400)




# --------------------------------------------------------------------------- #
# Findings from the code review of 2026-11-03
# --------------------------------------------------------------------------- #

def test_a_PO_below_the_floor_is_refused():
    """The generated invoices add a small absolute amount so that two POs of
    the same value do not produce near-duplicate invoices. On a trivial PO that
    amount dominates: below about 80 rupees the 7.5%-over variant lands past
    the reject threshold and the invoice advertised as FLAG comes back REJECT.
    The floor is what keeps the advertised bands honest."""
    with pytest.raises(Exception):
        NewPO(po_number="PO-9001", vendor_id="V-1001", po_value=NewPO.MIN_PO_VALUE - 1)
    assert NewPO(po_number="PO-9001", vendor_id="V-1001",
                 po_value=NewPO.MIN_PO_VALUE).po_value == NewPO.MIN_PO_VALUE


def test_the_bands_hold_at_the_smallest_PO_the_form_accepts():
    config = RuleConfig()
    for variant, spec in invoice_pdf.VARIANTS.items():
        out = generated(variant, po={**PO, "po_value": NewPO.MIN_PO_VALUE})
        if spec["expect"] == "APPROVE":
            assert out["variance_pct"] <= config.po_tolerance_pct, variant
        elif spec["expect"] == "FLAG":
            assert config.po_tolerance_pct < out["variance_pct"] <= config.po_over_tolerance_reject_pct, variant
        else:
            assert out["variance_pct"] > config.po_over_tolerance_reject_pct, variant


def test_a_name_that_is_only_a_company_suffix_is_refused():
    """It canonicalises to nothing, so stage 4 could never resolve it - and two
    such names would be indistinguishable from each other as well."""
    for bad in ("Private Limited", "Pvt Ltd", "LLP"):
        with pytest.raises(Exception):
            NewVendor(name=bad)


def test_a_vendors_own_aliases_are_checked_for_clashes(monkeypatch):
    """Stage 4 scores the printed name against the registered name *and* every
    alias, so an alias colliding with an existing vendor produces two
    candidates a hair apart and the winner is decided by sort stability."""
    from fastapi.testclient import TestClient
    from backend import main as main_mod

    existing = [{"vendor_id": "V-1001", "name": "Sharma Steel Works Private Limited",
                 "aliases": ["Sharma Steel"], "gstin": None, "status": "approved",
                 "payment_terms_days": 30, "source": "seed"}]
    monkeypatch.setattr(main_mod.db, "list_vendors", lambda: existing)

    with TestClient(main_mod.app) as client:
        clash = client.post("/api/vendors", json={
            "name": "Acme Fabricators", "aliases": "Sharma Steel"})
        assert clash.status_code == 409
        assert "Sharma Steel" in clash.json()["detail"]


def test_an_in_flight_run_is_not_deleted_out_from_under_itself(tmp_path, monkeypatch):
    """Deleting a run mid-pipeline does not stop it: it goes on to book
    consumption against a run_id with no row, and nothing afterwards can find
    that ledger entry to undo it."""
    path = tmp_path / "runs.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init_db(path)
    now = "2026-11-03T10:00:00+05:30"
    with db.connect() as conn:
        for run_id, name, status in (
            ("RUN-A", "test-invoice-PO-9001-match.pdf", "complete"),
            ("RUN-B", "test-invoice-PO-9001-over.pdf", "running"),
            ("RUN-C", "INV-MER-0881_meridian_happy.pdf", "complete"),
        ):
            conn.execute(
                "INSERT INTO runs (run_id, filename, file_sha256, status, started_at) "
                "VALUES (?, ?, 'x', ?, ?)", (run_id, name, status, now))

    assert db.running_runs_for_prefix("test-invoice-PO-9001-") == ["RUN-B"]
    assert db.delete_runs_by_filename_prefix("test-invoice-PO-9001-") == 1

    with db.connect() as conn:
        left = {r[0] for r in conn.execute("SELECT run_id FROM runs")}
    assert left == {"RUN-B", "RUN-C"}      # the running one and the unrelated one




# --------------------------------------------------------------------------- #
# Where a deployed copy keeps what you enter
# --------------------------------------------------------------------------- #

def test_state_dir_moves_the_database_and_the_blobs_together(tmp_path, monkeypatch):
    """`STATE_DIR` is the single switch, and it has to win over a `DB_PATH`
    left in a local `.env` - honouring both independently is how the database
    ends up back in the container while the blobs go to the volume, which
    silently loses a purchase order at the next restart."""
    monkeypatch.setenv("STATE_DIR", str(tmp_path / "vol"))
    monkeypatch.setenv("DB_PATH", "data/invoices.db")       # the trap
    import importlib
    from backend import config as config_mod
    cfg = importlib.reload(config_mod)
    try:
        assert cfg.DB_PATH == tmp_path / "vol" / "invoices.db"
        assert cfg.UPLOAD_DIR == tmp_path / "vol" / "uploads"
    finally:
        monkeypatch.undo()
        importlib.reload(config_mod)


def test_an_empty_volume_is_seeded_once_and_never_overwritten(tmp_path, monkeypatch):
    monkeypatch.setenv("STATE_DIR", str(tmp_path / "vol"))
    import importlib
    from backend import config as config_mod
    cfg = importlib.reload(config_mod)
    try:
        first = cfg.bootstrap_state()
        assert first["db"] is True                   # the seeded database lands
        assert first["uploads"] > 0                  # and the stored PDFs with it
        assert cfg.DB_PATH.exists()

        # Something the deployed copy wrote afterwards.
        marker = cfg.DB_PATH.read_bytes()
        cfg.DB_PATH.write_bytes(marker + b"\n-- raised on the live site")

        second = cfg.bootstrap_state()
        assert second == {"db": False, "uploads": 0}  # a no-op on the second boot
        assert cfg.DB_PATH.read_bytes().endswith(b"-- raised on the live site")
    finally:
        monkeypatch.undo()
        importlib.reload(config_mod)


def test_without_state_dir_nothing_moves_and_bootstrap_does_nothing():
    """Every local checkout, and the free-tier deployment, take this path."""
    from backend import config as config_mod
    assert config_mod.DB_PATH.parent == config_mod.DATA_DIR
    assert config_mod.bootstrap_state() == {"db": False, "uploads": 0}


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
