"""Seed the procurement masters, and optionally run the sample invoices.

Usage:
    python -m backend.seed                  # masters only, if empty
    python -m backend.seed --force          # reset masters to seeded state
    python -m backend.seed --runs           # also process every sample invoice
    python -m backend.seed --runs --clear   # wipe run history first, then process

The masters stand in for a procurement system. Two things in here are load
bearing for the edge cases rather than decoration:

* `PO_HISTORY` gives PO-4471 five lakh of prior billing. That is what makes the
  split-invoice case work: the incoming invoice looks perfectly reasonable
  against the PO on its own, and only the running total reveals the problem.
* Orbit Facility Services holds two open POs of quite different sizes, so the
  missing-PO-reference case has something genuinely ambiguous to rank.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from . import db, pipeline, state
from .config import SAMPLE_INVOICE_DIR

log = logging.getLogger("seed")

# --------------------------------------------------------------------------- #
# Approved vendor master
# --------------------------------------------------------------------------- #

VENDORS: list[dict] = [
    {
        "vendor_id": "V-1001", "name": "Sharma Steel Works Private Limited",
        "aliases": ["Sharma Steel Works", "Sharma Steel", "SSW Steel"],
        "gstin": "27AABCS1234A1Z5",
    },
    {
        "vendor_id": "V-1002", "name": "Meridian Office Supplies LLP",
        "aliases": ["Meridian Office Supplies", "Meridian Supplies", "Meridian Office"],
        "gstin": "29AACFM5678B1Z3",
    },
    {
        "vendor_id": "V-1003", "name": "Nalanda Print and Packaging",
        "aliases": ["Nalanda Print & Packaging", "Nalanda Printers", "Nalanda Packaging"],
        "gstin": "33AAFCN9012C1Z7",
    },
    {
        "vendor_id": "V-1004", "name": "Kaveri Logistics Private Limited",
        "aliases": ["Kaveri Logistics", "Kaveri Transport", "Kaveri Freight"],
        "gstin": "36AAGCK3456D1Z9",
    },
    {
        "vendor_id": "V-1005", "name": "Orbit Facility Services",
        "aliases": ["Orbit Facilities", "Orbit FM", "Orbit Facility Management"],
        "gstin": "07AAHCO7890E1Z1",
    },
    {
        # On the master but blocked - a vendor under dispute. An invoice from a
        # blocked vendor is rejected by R3 even though the name resolves
        # perfectly, which is a different failure from an unknown vendor.
        "vendor_id": "V-1006", "name": "Tri-City Electricals",
        "aliases": ["TriCity Electricals", "Tri City Electrical"],
        "gstin": "24AAJCT2345F1Z4", "status": "blocked",
    },
]

# --------------------------------------------------------------------------- #
# Purchase order master
# --------------------------------------------------------------------------- #

POS: list[dict] = [
    {"po_number": "PO-4471", "vendor_id": "V-1001", "description": "Structural steel supply - Phase 2",
     "po_value": 1_000_000.0, "issued_date": "2026-08-01", "expiry_date": "2026-12-31"},
    {"po_number": "PO-4472", "vendor_id": "V-1002", "description": "Office consumables - Q3",
     "po_value": 250_000.0, "issued_date": "2026-07-15", "expiry_date": "2026-11-30"},
    # An annual print programme billed in instalments. Sized so two legitimate
    # part-bills both fit inside it - which is what lets the near-duplicate case
    # be caught by the duplicate rule alone, rather than incidentally by PO
    # over-consumption.
    {"po_number": "PO-4473", "vendor_id": "V-1003", "description": "Brochure printing - annual programme",
     "po_value": 400_000.0, "issued_date": "2026-09-01", "expiry_date": "2027-01-31"},
    {"po_number": "PO-4474", "vendor_id": "V-1004", "description": "Inbound freight - October",
     "po_value": 320_000.0, "issued_date": "2026-09-20", "expiry_date": "2026-12-20"},
    {"po_number": "PO-4475", "vendor_id": "V-1005", "description": "Housekeeping services Oct-Dec",
     "po_value": 450_000.0, "issued_date": "2026-09-25", "expiry_date": "2026-12-31"},
    {"po_number": "PO-4476", "vendor_id": "V-1002", "description": "Workstation furniture refresh",
     "po_value": 600_000.0, "issued_date": "2026-08-10", "expiry_date": "2026-12-31"},
    {"po_number": "PO-4477", "vendor_id": "V-1001", "description": "Fabrication services - mezzanine",
     "po_value": 150_000.0, "issued_date": "2026-09-05", "expiry_date": "2026-12-31"},
    {"po_number": "PO-4478", "vendor_id": "V-1003", "description": "Corrugated packaging boxes",
     "po_value": 95_000.0, "issued_date": "2026-10-01", "expiry_date": "2027-02-28"},
    {"po_number": "PO-4479", "vendor_id": "V-1004", "description": "Last-mile delivery - metro",
     "po_value": 210_000.0, "issued_date": "2026-10-05", "expiry_date": "2027-01-05"},
    {"po_number": "PO-4480", "vendor_id": "V-1005", "description": "Pantry and cafeteria services",
     "po_value": 130_000.0, "issued_date": "2026-10-10", "expiry_date": "2027-01-10"},
    {"po_number": "PO-4481", "vendor_id": "V-1002", "description": "IT peripherals - laptops and docks",
     "po_value": 275_000.0, "issued_date": "2026-10-12", "expiry_date": "2027-02-12"},
    {"po_number": "PO-4482", "vendor_id": "V-1001", "description": "MS plates and sections",
     "po_value": 500_000.0, "issued_date": "2026-10-15", "expiry_date": "2027-01-15"},
]

# Invoices paid before this system existed. The PO-4471 row is the one the
# split-invoice edge case depends on.
PO_HISTORY: list[dict] = [
    {"po_number": "PO-4471", "invoice_number": "SSW/2026/2184", "amount": 500_000.0,
     "booked_at": "2026-09-12T11:00:00+05:30"},
    {"po_number": "PO-4476", "invoice_number": "MER/2026/0771", "amount": 180_000.0,
     "booked_at": "2026-09-02T10:30:00+05:30"},
    {"po_number": "PO-4479", "invoice_number": "KAV/2026/7702", "amount": 45_000.0,
     "booked_at": "2026-10-18T16:20:00+05:30"},
]


# --------------------------------------------------------------------------- #
# Sample invoices - metadata for the one-click demo buttons
# --------------------------------------------------------------------------- #

SAMPLES: list[dict] = [
    {
        "file": "INV-MER-0881_meridian_happy.pdf",
        "title": "Clean invoice, matched PO",
        "scenario": "happy_path",
        "expected": "APPROVE",
        "why": "Approved vendor, PO named, total inside tolerance.",
        "order": 1,
    },
    {
        "file": "INV-SSW-2211_sharma_split_po.pdf",
        "title": "Split invoice against one PO",
        "scenario": "split_po",
        "expected": "REJECT",
        "why": "Fine on its own; the PO's running total is what catches it (R6).",
        "order": 2,
    },
    {
        "file": "INV-NL-118_nalanda_original.pdf",
        "title": "Nalanda original (run this before the re-issue)",
        "scenario": "duplicate_setup",
        "expected": "APPROVE",
        "why": "The legitimate first submission, so the re-issue has something to match.",
        "order": 3,
        # This invoice and the re-issue below near-match each other, and the
        # relation is symmetric: with the re-issue already in history, running
        # the original would flag against it and never show the APPROVE it
        # advertises. Re-running the original therefore resets the pair.
        "resets": ["INV-NL-127_nalanda_reissue.pdf"],
    },
    {
        "file": "INV-NL-127_nalanda_reissue.pdf",
        "title": "Near-duplicate re-issue",
        "scenario": "near_duplicate",
        "expected": "FLAG",
        "why": "Same vendor, amount and date, new invoice number. A hash check misses it (R7).",
        "order": 4,
        # The reverse does not hold: this scenario only means anything when the
        # original is already in history, so running it must leave that alone.
        # It is run first if it is missing.
        "requires": "INV-NL-118_nalanda_original.pdf",
    },
    {
        "file": "INV-KAV-7731_kaveri_scanned.pdf",
        "title": "Scanned, image-only PDF",
        "scenario": "scanned",
        "expected": "FLAG",
        "why": "No text layer, so nothing can be verified. Confidence drops and R9 asks for a human.",
        "order": 5,
    },
    {
        "file": "INV-ORB-5540_orbit_no_po.pdf",
        "title": "No PO reference anywhere",
        "scenario": "missing_po",
        "expected": "FLAG",
        "why": "No PO printed. Falls back to vendor, amount and date, then escalates (R4).",
        "order": 6,
    },
    {
        "file": "INV-SSW-2240_sharma_within_tolerance.pdf",
        "title": "Tolerance boundary: 1.8% over",
        "scenario": "tolerance_inside",
        "expected": "APPROVE",
        "why": "1.8% over the PO, inside the 2% tolerance.",
        "order": 7,
    },
    {
        "file": "INV-SSW-2241_sharma_over_tolerance.pdf",
        "title": "Tolerance boundary: 7.5% over",
        "scenario": "tolerance_outside",
        "expected": "FLAG",
        "why": "7.5% over: past tolerance, short of rejection (R5).",
        "order": 8,
    },
    {
        "file": "INV-MER-0902_meridian_arithmetic.pdf",
        "title": "Invoice that does not add up",
        "scenario": "arithmetic",
        "expected": "FLAG",
        "why": "Line items plus tax disagree with the printed total (R2).",
        "order": 9,
    },
    {
        "file": "INV-ORB-5561_orbit_over_reject.pdf",
        "title": "Tolerance boundary, 18% over",
        "scenario": "over_reject",
        "expected": "REJECT",
        "why": "18% over the PO, past the reject threshold (R5).",
        "order": 10,
    },
    {
        "file": "INV-TCE-3310_tricity_blocked_vendor.pdf",
        "title": "Vendor on the master but blocked",
        "scenario": "blocked_vendor",
        "expected": "REJECT",
        "why": "Known vendor, blocked for payment (R3).",
        "order": 11,
    },
    {
        "file": "INV-VEC-8120_vectra_unknown_vendor.pdf",
        "title": "Vendor not on the master at all",
        "scenario": "unknown_vendor",
        "expected": "REJECT",
        "why": "Vendor not on the approved list at all (R3).",
        "order": 12,
    },
    {
        "file": "INV-NL-094_nalanda_stale_date.pdf",
        "title": "Invoice surfaced months late",
        "scenario": "stale_date",
        "expected": "FLAG",
        "why": "Dated beyond the 180-day staleness limit (R8).",
        "order": 13,
    },
    {
        "file": "INV-MER-NONUM_meridian_missing_number.pdf",
        "title": "No invoice number printed",
        "scenario": "missing_number",
        "expected": "FLAG",
        "why": "No invoice number printed (R1).",
        "order": 14,
    },
]


def describe_samples() -> list[dict]:
    """Only advertise samples that are actually on disk."""
    out: list[dict] = []
    for sample in sorted(SAMPLES, key=lambda s: s["order"]):
        path = SAMPLE_INVOICE_DIR / sample["file"]
        if path.exists():
            out.append({**sample, "bytes": path.stat().st_size})
    return out


# --------------------------------------------------------------------------- #
# Seeding
# --------------------------------------------------------------------------- #

def seed_masters(force: bool = False) -> dict:
    db.init_db()
    existing = len(db.list_vendors())
    if existing and not force:
        return {"skipped": True, "vendors": existing, "purchase_orders": len(db.list_pos())}

    for vendor in VENDORS:
        db.upsert_vendor(
            vendor["vendor_id"], vendor["name"], vendor["aliases"], vendor.get("gstin"),
            status=vendor.get("status", "approved"),
            payment_terms_days=vendor.get("payment_terms_days", 30),
        )
    for po in POS:
        db.upsert_po(
            po["po_number"], po["vendor_id"], po["description"], po["po_value"],
            po["issued_date"], po["expiry_date"],
        )

    # Rebuild history rows idempotently: drop the previous 'history' set first
    # so re-seeding does not double the prior billing.
    with db.connect() as conn:
        conn.execute("DELETE FROM po_consumption WHERE source = 'history'")
    for row in PO_HISTORY:
        db.book_consumption(
            row["po_number"], row["amount"], run_id=None,
            invoice_number=row["invoice_number"], source="history",
            booked_at=row["booked_at"],
        )

    return {
        "skipped": False,
        "vendors": len(db.list_vendors()),
        "purchase_orders": len(db.list_pos()),
        "history_rows": len(PO_HISTORY),
    }


def seed_masters_if_empty() -> dict:
    return seed_masters(force=False)


def reseed(clear_runs: bool = False) -> dict:
    """Restore masters (and optionally wipe run history) between demo takes."""
    if clear_runs:
        with db.connect() as conn:
            conn.execute("DELETE FROM run_stages")
            conn.execute("DELETE FROM runs")
            conn.execute("DELETE FROM llm_events")
            conn.execute("DELETE FROM po_consumption")
    else:
        with db.connect() as conn:
            conn.execute("DELETE FROM po_consumption WHERE source = 'run'")

    summary = seed_masters(force=True)
    return {**summary, "runs_cleared": clear_runs, "runs": db.count_runs()}


async def _process_samples() -> list[tuple[str, str]]:
    """Run every sample invoice through the real pipeline, in the order the
    scenarios depend on - the Nalanda original has to land before its re-issue
    can be recognised as a near duplicate."""
    outcomes: list[tuple[str, str]] = []
    for sample in sorted(SAMPLES, key=lambda s: s["order"]):
        path = SAMPLE_INVOICE_DIR / sample["file"]
        if not path.exists():
            log.warning("sample missing, skipping: %s", sample["file"])
            continue
        run_id = pipeline.new_run_id()
        result = await pipeline.execute(
            run_id, sample["file"], path.read_bytes(), state.current()
        )
        got = result.decision.decision if result.decision else f"FAILED ({result.error})"
        flag = "ok " if got == sample["expected"] else "DIFF"
        log.info("%s %-44s expected %-7s got %s", flag, sample["file"], sample["expected"], got)
        outcomes.append((sample["file"], got))
    return outcomes


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s - %(message)s"
    )
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="re-seed masters even if present")
    parser.add_argument("--runs", action="store_true", help="also process the sample invoices")
    parser.add_argument("--clear", action="store_true", help="wipe run history before processing")
    args = parser.parse_args()

    db.init_db()
    if args.clear:
        info = reseed(clear_runs=True)
        log.info("cleared run history; masters re-seeded: %s", info)
    else:
        info = seed_masters(force=args.force)
        log.info("masters: %s", info)

    state.load_from_db()

    if args.runs:
        outcomes = asyncio.run(_process_samples())
        log.info("processed %d sample invoice(s)", len(outcomes))
    return 0


if __name__ == "__main__":
    sys.exit(main())
