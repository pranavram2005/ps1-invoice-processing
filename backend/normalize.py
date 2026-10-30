"""Stage 3: turn the model's reading of the document into canonical values.

Everything here is deterministic. The model reports what is printed; this module
decides what that means. Dates become ISO-8601, currency becomes an ISO 4217
code, vendor names get a canonical form for matching, and the line items get
summed so rule R2 has something to reconcile against.

The one interesting judgment is date parsing. Indian invoices are day-first, so
"03/04/2026" is 3 April, not 4 March - getting that backwards would silently
corrupt the staleness and duplicate-window rules. The code parses the printed
string itself with day-first precedence and only falls back to the model's own
ISO reading for formats it does not recognise, so the common case never depends
on the model getting it right.
"""
from __future__ import annotations

import re
from datetime import date, datetime

from .models import NormalizedInvoice, RawExtraction

# Day-first formats come before month-first on purpose.
DATE_FORMATS = [
    "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y",
    "%Y-%m-%d", "%Y/%m/%d",
    "%d %b %Y", "%d %B %Y", "%d-%b-%Y", "%d-%B-%Y",
    "%b %d, %Y", "%B %d, %Y", "%b %d %Y", "%B %d %Y",
    "%d/%m/%y", "%d-%m-%y",
]

CURRENCY_ALIASES = {
    "₹": "INR", "RS": "INR", "RS.": "INR", "INR": "INR", "RUPEES": "INR",
    "$": "USD", "USD": "USD", "US$": "USD",
    "€": "EUR", "EUR": "EUR",
    "£": "GBP", "GBP": "GBP",
}

# Suffixes that carry no identity - stripped before fuzzy vendor matching so
# "Sharma Steel Pvt. Ltd." and "Sharma Steel Private Limited" collapse together.
COMPANY_NOISE = [
    "private limited", "pvt limited", "pvt ltd", "pvt. ltd.", "p ltd",
    "limited", "ltd", "llp", "inc", "incorporated", "corporation", "corp",
    "company", "co", "and sons", "& sons", "enterprises", "enterprise",
]


def parse_date(raw: str | None, model_iso: str | None = None) -> tuple[str | None, str | None]:
    """Return (iso_date, raw_as_seen). Day-first precedence; model ISO as fallback."""
    if raw:
        cleaned = re.sub(r"\s+", " ", str(raw)).strip().strip(",")
        cleaned = re.sub(r"(?i)^(date|dated|invoice date)\s*[:\-]?\s*", "", cleaned).strip()
        # Drop ordinal suffixes: "14th Mar 2026" -> "14 Mar 2026"
        candidate = re.sub(r"(?i)(\d{1,2})(st|nd|rd|th)\b", r"\1", cleaned)
        for fmt in DATE_FORMATS:
            try:
                return datetime.strptime(candidate, fmt).date().isoformat(), str(raw).strip()
            except ValueError:
                continue

    if model_iso:
        try:
            return date.fromisoformat(str(model_iso).strip()[:10]).isoformat(), (
                str(raw).strip() if raw else str(model_iso).strip()
            )
        except ValueError:
            pass

    return None, (str(raw).strip() if raw else None)


def normalize_currency(raw: str | None) -> str:
    """Default to INR: this deployment is single-currency (see README)."""
    if not raw:
        return "INR"
    key = str(raw).strip().upper()
    if key in CURRENCY_ALIASES:
        return CURRENCY_ALIASES[key]
    for symbol, code in CURRENCY_ALIASES.items():
        if symbol in key:
            return code
    return key[:3] if len(key) >= 3 else "INR"


def canonical_vendor_name(raw: str | None) -> str | None:
    """Lower-cased, punctuation-free, entity-suffix-free form used for matching.

    The display name stays untouched - this form exists only so the fuzzy
    matcher is comparing trading names rather than legal boilerplate.
    """
    if not raw:
        return None
    text = str(raw).lower()
    text = re.sub(r"[^\w\s&]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    for noise in sorted(COMPANY_NOISE, key=len, reverse=True):
        text = re.sub(rf"\b{re.escape(noise)}\b", " ", text)
    return re.sub(r"\s+", " ", text).strip() or None


def normalize_po_reference(raw: str | None) -> str | None:
    """Uppercase, alphanumerics only: 'po-4471', 'PO 4471' and 'PO/4471' all
    become 'PO4471' so a reference match is not defeated by punctuation."""
    if not raw:
        return None
    return re.sub(r"[^A-Z0-9]", "", str(raw).upper()) or None


def normalize(extraction: RawExtraction, *, model_iso: str | None = None) -> NormalizedInvoice:
    iso_date, raw_date = parse_date(extraction.invoice_date, model_iso)

    line_totals = [
        item.line_total for item in extraction.line_items if item.line_total is not None
    ]
    # Fall back to quantity x unit price for any line that printed those but no
    # line total - a vendor layout choice, not missing information.
    for item in extraction.line_items:
        if item.line_total is None and item.quantity is not None and item.unit_price is not None:
            line_totals.append(item.quantity * item.unit_price)

    items_sum = round(sum(line_totals), 2) if line_totals else None

    return NormalizedInvoice(
        vendor_name_raw=extraction.vendor_name,
        vendor_name_canonical=canonical_vendor_name(extraction.vendor_name),
        vendor_gstin=(extraction.vendor_gstin or "").strip().upper() or None,
        invoice_number=(extraction.invoice_number or "").strip() or None,
        invoice_date=iso_date,
        invoice_date_raw=raw_date,
        currency=normalize_currency(extraction.currency),
        po_reference=(extraction.po_reference or "").strip() or None,
        line_items=extraction.line_items,
        line_items_sum=items_sum,
        subtotal=extraction.subtotal,
        tax_amount=extraction.tax_amount,
        total_amount=extraction.total_amount,
        notes=extraction.notes,
    )
