"""PDF text-layer probing and provenance verification.

Why this module exists
----------------------
The brief asks for a decision with everything that happened in between visible.
For extraction, the strongest version of that is field-level provenance: for
every value, which page it came from and the exact text it was read from.

The Claude API has a `citations` feature that returns exactly this. It cannot be
used here: citations are **incompatible with structured outputs** - enabling both
on one request returns a 400 - and this pipeline needs a schema-guaranteed
extraction far more than it needs server-formatted citations (see DECISIONS.md).

So provenance is part of the extraction schema: the model reports the page and
the verbatim snippet it read each field from. On its own that would just be a
second thing the model asserts. This module turns the assertion into evidence by
checking each quoted snippet against the PDF's own text layer with a fuzzy
match. The UI then shows, per field, "verified on page 1" or "could not verify".

That check also yields the scanned-document behaviour for free. An image-only
PDF has no text layer, so nothing can be verified - `verified` stays None
(unknown, not false), confidence takes a documented penalty, and R9 flags the
invoice for mandatory human review. Being less confident is the correct response
to a worse input; nothing has to fail for that to happen.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from io import BytesIO

from pypdf import PdfReader
from rapidfuzz import fuzz

from .models import ConfidenceBreakdown, Provenance

log = logging.getLogger("provenance")

# A quoted snippet counts as verified at or above this fuzzy score. Set below
# 100 on purpose: PDF text extraction mangles whitespace, ligatures and currency
# glyphs, so an exact string match would report false negatives on documents
# that are in fact perfectly readable.
VERIFY_THRESHOLD = 88.0

# Below this many extractable characters per page the document has no usable
# text layer and is treated as scanned.
MIN_CHARS_PER_PAGE = 24


@dataclass
class PdfProbe:
    page_count: int = 0
    pages_text: list[str] = field(default_factory=list)
    total_chars: int = 0
    kind: str = "text"            # "text" | "scanned"
    note: str = ""
    error: str | None = None

    @property
    def has_text_layer(self) -> bool:
        return self.kind == "text"


def _normalise(text: str) -> str:
    """Collapse whitespace and drop currency/punctuation noise so a quote and
    the extracted page text are compared on their words and digits."""
    text = text.replace("₹", "Rs").replace("\xa0", " ")
    text = re.sub(r"[^\w\s.,/:%-]", " ", text)
    return re.sub(r"\s+", " ", text).strip().lower()


def probe_pdf(data: bytes) -> PdfProbe:
    """Stage 1's text-layer probe: how many pages, and is there text to read?"""
    try:
        reader = PdfReader(BytesIO(data))
    except Exception as exc:                      # malformed/encrypted PDF
        log.warning("could not open PDF: %s", exc)
        return PdfProbe(kind="scanned", error=str(exc),
                        note="PDF could not be parsed for a text layer; treating as image-only.")

    pages: list[str] = []
    for page in reader.pages:
        try:
            pages.append(page.extract_text() or "")
        except Exception as exc:
            log.warning("text extraction failed on a page: %s", exc)
            pages.append("")

    total = sum(len(p.strip()) for p in pages)
    count = len(pages)
    kind = "text" if count and total >= MIN_CHARS_PER_PAGE * count else "scanned"
    if kind == "text":
        note = f"Text layer present: {total:,} extractable characters across {count} page(s)."
    else:
        note = (
            f"No usable text layer ({total} characters across {count} page(s)) - this is an "
            f"image-only PDF. Extraction will run on the page images instead, and no quoted "
            f"value can be verified against the file."
        )
    return PdfProbe(page_count=count, pages_text=pages, total_chars=total, kind=kind, note=note)


def verify_provenance(items: list[Provenance], probe: PdfProbe) -> list[Provenance]:
    """Check each quoted snippet against the PDF text layer.

    Checks the page the model claimed first; if the quote is not there but is
    found on another page, the entry is still verified and the page is corrected
    - a wrong page number on a correct quote is a citation error, not an
    extraction error, and conflating the two would hide real extraction quality.
    """
    if not probe.has_text_layer:
        for item in items:
            item.verified = None
            item.match_score = None
        return items

    normalised_pages = [_normalise(p) for p in probe.pages_text]

    for item in items:
        quote = (item.quote or "").strip()
        if len(quote) < 3:
            item.verified = None
            item.match_score = None
            continue

        needle = _normalise(quote)
        best_score, best_page = 0.0, None

        claimed = item.page
        order = []
        if claimed and 1 <= claimed <= len(normalised_pages):
            order.append(claimed)
        order += [p for p in range(1, len(normalised_pages) + 1) if p not in order]

        for page_no in order:
            score = float(fuzz.partial_ratio(needle, normalised_pages[page_no - 1]))
            if score > best_score:
                best_score, best_page = score, page_no
            if score >= VERIFY_THRESHOLD and page_no == claimed:
                break

        item.match_score = round(best_score / 100.0, 3)
        item.verified = best_score >= VERIFY_THRESHOLD
        if item.verified and best_page and best_page != item.page:
            item.page = best_page

    return items


# Fields whose provenance actually matters for a payment decision. Verifying
# that the model correctly quoted a line-item description is not interesting;
# verifying the total, the vendor and the PO reference is the whole point.
DECISION_CRITICAL_FIELDS = {
    "vendor_name", "invoice_number", "invoice_date", "total_amount",
    "po_reference", "tax_amount", "subtotal",
}


def compute_confidence(
    *, model_confidence: float, items: list[Provenance], probe: PdfProbe,
) -> ConfidenceBreakdown:
    """Blend the model's self-reported confidence with how much of what it
    claimed we were able to confirm.

    Deliberately simple and fully shown in the UI. A confidence number a
    reviewer cannot decompose is worse than no confidence number at all.
    """
    model_confidence = max(0.0, min(1.0, float(model_confidence or 0.0)))
    critical = [i for i in items if i.field in DECISION_CRITICAL_FIELDS]
    checkable = [i for i in critical if i.verified is not None]
    verified = [i for i in checkable if i.verified]

    if not probe.has_text_layer:
        # No text layer: the model read page images, and nothing it quoted can
        # be confirmed against the file. A flat 0.7 multiplier is a judgment
        # call, not a measurement - it is set so that a confident model
        # (0.95) on a scanned document lands at 0.665, under the 0.75 floor,
        # and therefore always reaches a human.
        final = round(model_confidence * 0.70, 3)
        return ConfidenceBreakdown(
            model_confidence=round(model_confidence, 3),
            provenance_fields_total=len(critical),
            provenance_fields_verified=0,
            provenance_verified_ratio=None,
            scanned_penalty_applied=True,
            final=final,
            explanation=(
                f"model reported {model_confidence:.2f}, reduced by the 0.70 scanned-document "
                f"factor because no text layer exists to verify any value against"
            ),
        )

    if not checkable:
        final = round(model_confidence * 0.85, 3)
        return ConfidenceBreakdown(
            model_confidence=round(model_confidence, 3),
            provenance_fields_total=len(critical),
            provenance_fields_verified=0,
            provenance_verified_ratio=None,
            final=final,
            explanation=(
                f"model reported {model_confidence:.2f}; no field quoted a snippet that could "
                f"be checked, so an 0.85 factor applies"
            ),
        )

    ratio = len(verified) / len(checkable)
    final = round(0.5 * model_confidence + 0.5 * ratio, 3)
    return ConfidenceBreakdown(
        model_confidence=round(model_confidence, 3),
        provenance_fields_total=len(critical),
        provenance_fields_verified=len(verified),
        provenance_verified_ratio=round(ratio, 3),
        final=final,
        explanation=(
            f"half the model's own {model_confidence:.2f}, half the "
            f"{len(verified)}/{len(checkable)} decision-critical fields whose quoted text was "
            f"found in the document"
        ),
    )
