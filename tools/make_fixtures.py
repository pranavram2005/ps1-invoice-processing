"""Build the offline extraction fixtures.

    python tools/make_fixtures.py

What these are, precisely, because it matters: they are the **true field values**
of the generated sample invoices, taken from the generator's own definitions
(`data/invoices/ground_truth.json`), with each provenance quote lifted verbatim
out of the PDF's real text layer. They are not recorded model output and they do
not pretend to be.

Two things they are good for:

1. **Testing the deterministic half of the system in isolation.** Rules,
   matching, duplicate detection, confidence arithmetic and the whole UI can be
   exercised without an API call and without model variance - so when a rule
   test fails, it failed because of the rule.
2. **A demo that survives a dead API key.** `EXTRACTION_MODE=offline` runs the
   nine bundled invoices with no network at all.

What they are *not* good for: judging extraction quality. A fixture run proves
nothing about whether the model can read an invoice - only a live run does
that. The UI labels offline runs so the distinction is never lost.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.config import DATA_DIR, SAMPLE_INVOICE_DIR    # noqa: E402
from backend.provenance import probe_pdf                     # noqa: E402

GROUND_TRUTH = SAMPLE_INVOICE_DIR / "ground_truth.json"
FIXTURES = DATA_DIR / "fixtures.json"

# Model confidence a competent reader would plausibly report. Lower for the
# image-only page, which is also where the real penalty is applied by
# provenance.compute_confidence.
CONFIDENCE_TEXT = 0.94
CONFIDENCE_SCANNED = 0.82

PROVENANCE_FIELDS = [
    "vendor_name", "vendor_gstin", "invoice_number", "invoice_date",
    "currency", "po_reference", "subtotal", "tax_amount", "total_amount",
]


def inr(value: float) -> str:
    """Indian digit grouping, matching the generator's formatter."""
    whole, decimals = f"{abs(value):.2f}".split(".")
    if len(whole) > 3:
        whole = re.sub(r"\B(?=(\d{2})+(?!\d))", ",", whole[:-3]) + "," + whole[-3:]
    return ("-" if value < 0 else "") + whole + "." + decimals


def flat(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def find_quote(page_text: str, needle: str, before: int = 3, after: int = 2) -> str | None:
    """Return a short verbatim window of the page around `needle`.

    Taken straight out of the extracted text so the provenance verifier - which
    does a fuzzy match against that same text - will confirm it. A fixture whose
    quotes could not be verified would make the confidence numbers meaningless.
    """
    if not needle:
        return None
    haystack = flat(page_text)
    index = haystack.lower().find(flat(needle).lower())
    if index < 0:
        return None

    head = haystack[:index].split()
    tail = haystack[index + len(flat(needle)):].split()
    parts = head[-before:] + [haystack[index:index + len(flat(needle))]] + tail[:after]
    return " ".join(parts).strip()


def build_fixture(entry: dict, page_text: str) -> dict:
    """Assemble one extraction payload in exactly the shape the live path returns."""
    scanned = entry["scanned"]

    candidates: dict[str, str | None] = {
        "vendor_name": entry["vendor_name"],
        "vendor_gstin": entry["vendor_gstin"],
        "invoice_number": entry["invoice_number"],
        "invoice_date": entry["invoice_date"],
        "currency": "INR",
        "po_reference": entry["po_reference"],
        "subtotal": inr(entry["subtotal"]) if entry["subtotal"] is not None else None,
        "tax_amount": inr(entry["tax_amount"]) if entry["tax_amount"] is not None else None,
        "total_amount": inr(entry["total_amount"]),
    }

    provenance: list[dict] = []
    for field in PROVENANCE_FIELDS:
        needle = candidates.get(field)
        if not needle:
            continue
        if scanned:
            # No text layer to quote from, so record what the value looks like
            # on the page. These can never be verified, which is the point.
            quote = f"{field.replace('_', ' ')}: {needle}"
        else:
            quote = find_quote(page_text, needle)
            if not quote:
                continue
        provenance.append({"field": field, "page": 1, "quote": quote})

    notes = None
    if scanned:
        notes = (
            "Read from a scanned page image - there is no digital text on this document. "
            "Figures were cross-checked against the totals block."
        )
    elif entry["total_is_misstated"]:
        notes = (
            "The printed grand total does not equal the line items plus tax. Reported exactly "
            "as printed."
        )

    return {
        "vendor_name": entry["vendor_name"],
        "vendor_gstin": entry["vendor_gstin"],
        "invoice_number": entry["invoice_number"],
        "invoice_date": entry["invoice_date"],
        "invoice_date_iso": entry["invoice_date_iso"],
        "currency": "INR",
        "po_reference": entry["po_reference"],
        "line_items": entry["line_items"],
        "subtotal": entry["subtotal"],
        "tax_amount": entry["tax_amount"],
        "total_amount": entry["total_amount"],
        "notes": notes,
        "model_confidence": CONFIDENCE_SCANNED if scanned else CONFIDENCE_TEXT,
        "provenance": provenance,
    }


def main() -> int:
    if not GROUND_TRUTH.exists():
        print(f"missing {GROUND_TRUTH}\nrun: node tools/invoice_gen/generate.js")
        return 2

    truth = json.loads(GROUND_TRUTH.read_text(encoding="utf-8"))
    fixtures: dict[str, dict] = {}
    unverifiable = 0

    for entry in truth["invoices"]:
        path = SAMPLE_INVOICE_DIR / entry["file"]
        if not path.exists():
            print(f"  skip (no pdf): {entry['file']}")
            continue

        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        probe = probe_pdf(data)
        page_text = probe.pages_text[0] if probe.pages_text else ""

        fixture = build_fixture(entry, page_text)
        quoted = len(fixture["provenance"])
        expected_quotable = sum(
            1 for f in PROVENANCE_FIELDS
            if f == "currency" or entry.get(f) is not None
            or (f in ("subtotal", "tax_amount", "total_amount") and entry.get(f) is not None)
        )
        if not entry["scanned"] and quoted < 5:
            unverifiable += 1
            print(f"  WARNING only {quoted} quotes found in {entry['file']}")

        fixtures[digest] = {
            "file": entry["file"],
            "sha256_short": digest[:16],
            "pdf_kind": probe.kind,
            "expect": entry["expect"],
            "extraction": fixture,
        }
        print(
            f"  {entry['file']:<44} {probe.kind:8} {quoted} provenance quote(s)"
            f"{' (unverifiable - image only)' if entry['scanned'] else ''}"
        )

    payload = {
        "note": (
            "Offline extraction fixtures: the true field values of the bundled sample invoices, "
            "with provenance quotes taken verbatim from each PDF's text layer. These are "
            "ground truth, NOT recorded model output - they exist to test the deterministic "
            "half of the pipeline and to let the demo run without an API key. They say nothing "
            "about extraction accuracy."
        ),
        "source": "tools/make_fixtures.py from data/invoices/ground_truth.json",
        "keyed_by": "sha256 of the PDF bytes",
        "fixtures": fixtures,
    }
    FIXTURES.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"\nwrote {len(fixtures)} fixture(s) to {FIXTURES}")
    return 1 if unverifiable else 0


if __name__ == "__main__":
    sys.exit(main())
