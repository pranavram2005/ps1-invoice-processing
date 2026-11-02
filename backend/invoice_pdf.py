"""Generate a real invoice PDF for a purchase order, in pure Python.

Why this exists. A purchase order raised in the UI has no invoice to check
against it, so the most interesting half of the system - the seven stages, the
nine rules, the decision - could not be demonstrated on a PO the user had just
created. This module closes that loop: it writes a genuine single-page PDF with
a real text layer, which then goes through exactly the same `POST /api/runs`
code path as an uploaded file. Nothing in the pipeline knows these bytes were
generated.

Three constraints shaped it:

* **No new dependency.** reportlab and friends would have to go into the image;
  the Dockerfile is deliberately a thin Python container. A text-layer PDF is a
  few hundred bytes of PDF syntax over the base-14 Helvetica, so it is written
  here by hand.
* **A real text layer.** Stage 3 verifies every quoted value against the PDF's
  own text layer (`provenance.py`). A rasterised page would demonstrate
  something else - the scanned-document path, which the bundled Kaveri sample
  already covers.
* **The figures have to reconcile.** R2 adds the line items and tax back up
  against the stated total, so the arithmetic is derived rather than typed: the
  line items are built first and the subtotal, tax and total follow from them.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

# A4 at 72 dpi, which is what PDF user space is.
PAGE_W, PAGE_H = 595, 842
GST_RATE = 0.18
BUYER = "Harbourline Industries India Private Limited"

# What each variant is for. `target_ratio` is the fraction of the PO value the
# invoice bills, and it is the single number that decides the verdict: R5
# passes anything at or under the PO value plus tolerance, flags up to the
# reject threshold, and rejects past it.
VARIANTS: dict[str, dict] = {
    "match": {
        "label": "Bills 60% of the PO",
        "target_ratio": 0.60,
        "expect": "APPROVE",
        "why": (
            "A clean part-bill against the purchase order. The vendor is on the approved "
            "list, the invoice names the PO, the figures reconcile and the amount is well "
            "inside the PO value - so all nine checks pass and it is approved."
        ),
    },
    "over": {
        "label": "Bills 7.5% more than the PO",
        "target_ratio": 1.075,
        "expect": "FLAG",
        "why": (
            "Identical in every other respect, but billed over the purchase order. It "
            "clears the 2% tolerance and stays under the 10% reject threshold, so R5 "
            "flags it for an approver rather than rejecting it outright."
        ),
    },
    "reject": {
        "label": "Bills 35% more than the PO",
        "target_ratio": 1.35,
        "expect": "REJECT",
        "why": (
            "Billed far past the purchase order. R5 is beyond the 10% reject threshold, "
            "so the decision is a rejection with the variance stated - nobody has to work "
            "out by how much."
        ),
    },
}


# --------------------------------------------------------------------------- #
# Money
# --------------------------------------------------------------------------- #

def inr(value: float) -> str:
    """Indian digit grouping: 2,41,451.60, not 241,451.60."""
    whole, _, frac = f"{abs(value):.2f}".partition(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        parts: list[str] = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        whole = ",".join(parts + [tail])
    sign = "-" if value < 0 else ""
    return f"{sign}{whole}.{frac}"


@dataclass
class Figures:
    """The numbers on the invoice, derived so that they reconcile."""
    lines: list[dict]
    subtotal: float
    cgst: float
    sgst: float
    tax: float
    total: float


def compose_figures(target_total: float, description: str) -> Figures:
    """Build line items that add up to about `target_total` including tax.

    Derived in the order a real invoice is: quantities and rates first, then the
    taxable value, then the tax, then the total. Typing a round total and
    back-filling the items is what produces an invoice that does not add up,
    which is a case this project already has a sample for and not the one this
    button is demonstrating.
    """
    taxable_target = target_total / (1 + GST_RATE)

    # Two lines rather than one: a single line makes "line items + tax = total"
    # read as a tautology, and an AP reviewer reads a split bill differently
    # from a lump sum.
    split = [
        (0.65, 12, f"{description} - supply"),
        (0.35, 4, f"{description} - site works and handling"),
    ]

    lines: list[dict] = []
    allocated = 0.0
    for i, (share, qty, text) in enumerate(split):
        want = taxable_target - allocated if i == len(split) - 1 else taxable_target * share
        rate = round(want / qty, 2)
        amount = round(rate * qty, 2)
        lines.append({"no": i + 1, "description": text, "qty": qty,
                      "rate": rate, "amount": amount})
        allocated += amount

    subtotal = round(sum(line["amount"] for line in lines), 2)
    cgst = round(subtotal * GST_RATE / 2, 2)
    sgst = round(subtotal * GST_RATE / 2, 2)
    tax = round(cgst + sgst, 2)
    return Figures(lines, subtotal, cgst, sgst, tax, round(subtotal + tax, 2))


# --------------------------------------------------------------------------- #
# A minimal PDF writer
# --------------------------------------------------------------------------- #

def _pdf_escape(text: str) -> str:
    out = text.replace("\\", "\\\\")
    return out.replace("(", "\\(").replace(")", "\\)")


class Page:
    """Accumulates PDF content-stream operators for one page.

    Coordinates are given from the top of the page, because that is how a
    layout is read; the y axis is flipped on the way in.
    """

    def __init__(self) -> None:
        self.ops: list[str] = []

    def text(self, x: float, y: float, value: str, size: float = 9.5,
             bold: bool = False, gray: float = 0.0) -> None:
        if value == "":
            return
        font = "F2" if bold else "F1"
        self.ops.append(
            f"BT {gray:.2f} g /{font} {size:g} Tf 1 0 0 1 {x:g} {PAGE_H - y:g} Tm "
            f"({_pdf_escape(value)}) Tj ET"
        )

    def right(self, x_right: float, y: float, value: str, size: float = 9.5,
              bold: bool = False, gray: float = 0.0) -> None:
        """Right-align using an approximate advance width.

        Money in a column that is not right-aligned is unreadable. There are no
        font metrics here, and half an em per character is within a glyph of
        correct over digits and separators - close enough for a column edge.
        """
        width = size * 0.5 * len(value)
        self.text(x_right - width, y, value, size, bold, gray)

    def rule(self, x1: float, y: float, x2: float, width: float = 0.5,
             gray: float = 0.75) -> None:
        self.ops.append(
            f"{gray:.2f} G {width:g} w {x1:g} {PAGE_H - y:g} m {x2:g} {PAGE_H - y:g} l S"
        )

    def band(self, x: float, y: float, w: float, h: float, gray: float = 0.93) -> None:
        self.ops.append(f"{gray:.2f} g {x:g} {PAGE_H - y - h:g} {w:g} {h:g} re f")

    def stream(self) -> bytes:
        return "\n".join(self.ops).encode("latin-1", "replace")


def build_pdf(page: Page) -> bytes:
    """Assemble one page into a PDF 1.4 file with a cross-reference table."""
    content = page.stream()
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {PAGE_W} {PAGE_H}] "
            f"/Resources << /Font << /F1 5 0 R /F2 6 0 R >> >> /Contents 4 0 R >>"
        ).encode("latin-1"),
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>",
    ]

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: list[int] = []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode("latin-1") + body + b"\nendobj\n"

    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode("latin-1")
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode("latin-1")
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_at}\n%%EOF\n"
    ).encode("latin-1")
    return bytes(out)


# --------------------------------------------------------------------------- #
# The invoice itself
# --------------------------------------------------------------------------- #

def render_invoice(
    *, vendor: dict, po: dict, invoice_number: str, invoice_date: date,
    figures: Figures, note: str,
) -> bytes:
    """Lay out one tax invoice.

    Deliberately plain: this document exists to be read correctly, not to test
    the extractor's patience. The five bundled samples are the ones that hide
    the same facts in five different places.
    """
    p = Page()
    left, right_edge = 56.0, 539.0

    # ---- supplier
    p.text(left, 72, vendor["name"], size=16, bold=True)
    p.text(left, 90, "SUPPLIER TAX INVOICE", size=7.5, gray=0.45)
    p.text(left, 108, f"GSTIN {vendor.get('gstin') or '-'}", size=9, gray=0.3)
    p.text(left, 122, f"Vendor code {vendor['vendor_id']} on the buyer's approved vendor master",
           size=8.5, gray=0.45)

    p.right(right_edge, 76, "TAX INVOICE", size=13, bold=True)
    p.right(right_edge, 94, f"Invoice No. {invoice_number}", size=9.5)
    p.right(right_edge, 108, f"Invoice Date {invoice_date.strftime('%d/%m/%Y')}", size=9.5)
    p.right(right_edge, 122, f"Purchase Order {po['po_number']}", size=9.5, bold=True)

    p.rule(left, 140, right_edge)

    # ---- buyer, and the PO this is billed against
    p.text(left, 162, "B I L L E D   T O", size=7.5, gray=0.45)
    p.text(left, 180, BUYER, size=10.5, bold=True)
    p.text(left, 196, "Plot 14, Phase II, Peenya Industrial Area", size=9, gray=0.25)
    p.text(left, 210, "Bengaluru 560058, Karnataka", size=9, gray=0.25)
    p.text(left, 224, "GSTIN 29AABCH4455L1ZQ", size=9, gray=0.25)

    p.text(330, 162, "A G A I N S T   P U R C H A S E   O R D E R", size=7.5, gray=0.45)
    p.text(330, 180, po["po_number"], size=10.5, bold=True)
    p.text(330, 196, (po.get("description") or "")[:46], size=9, gray=0.25)
    p.text(330, 210, f"PO value INR {inr(float(po['po_value']))}", size=9, gray=0.25)
    p.text(330, 224, f"Valid {po.get('issued_date') or '-'} to {po.get('expiry_date') or '-'}",
           size=9, gray=0.25)

    # ---- line items
    y = 262.0
    p.band(left, y - 12, right_edge - left, 18)
    p.text(left + 4, y, "#", size=8, bold=True)
    p.text(left + 26, y, "DESCRIPTION", size=8, bold=True)
    p.right(360, y, "QTY", size=8, bold=True)
    p.right(440, y, "RATE", size=8, bold=True)
    p.right(right_edge - 4, y, "AMOUNT", size=8, bold=True)

    y += 24
    for line in figures.lines:
        p.text(left + 4, y, str(line["no"]), size=9.5)
        p.text(left + 26, y, line["description"][:52], size=9.5)
        p.right(360, y, str(line["qty"]), size=9.5)
        p.right(440, y, inr(line["rate"]), size=9.5)
        p.right(right_edge - 4, y, inr(line["amount"]), size=9.5)
        y += 20

    p.rule(left, y - 6, right_edge)

    # ---- totals
    y += 16
    for label, value in (
        ("Taxable value", figures.subtotal),
        ("CGST @ 9%", figures.cgst),
        ("SGST @ 9%", figures.sgst),
    ):
        p.right(440, y, label, size=9.5)
        p.right(right_edge - 4, y, inr(value), size=9.5)
        y += 18

    p.rule(300, y - 4, right_edge, width=0.8, gray=0.4)
    y += 16
    p.right(440, y, "Total payable", size=11, bold=True)
    p.right(right_edge - 4, y, f"INR {inr(figures.total)}", size=11, bold=True)

    # ---- footer
    y += 44
    p.text(left, y, "Payment within 30 days of invoice date. Goods once sold are not returnable.",
           size=9, gray=0.3)
    p.text(left, y + 16, f"For {vendor['name']}", size=9, gray=0.3)
    p.text(left, y + 30, "Authorised Signatory", size=9, gray=0.3)
    p.rule(left, PAGE_H - 92, right_edge)
    p.text(left, PAGE_H - 74, note, size=7.5, gray=0.5)
    return build_pdf(p)


def make_test_invoice(
    *, po: dict, vendor: dict, variant: str = "match", today: date | None = None,
    sequence: int = 1,
) -> dict:
    """A PDF plus everything the caller needs to describe what it is.

    The invoice number carries the sequence, so pressing the button twice
    produces two different files rather than a byte-identical resubmission that
    R7 would correctly reject. The near-duplicate and prior-consumption history
    is cleared by the API instead, which is the honest way back to a clean
    slate.
    """
    if variant not in VARIANTS:
        raise ValueError(f"unknown variant '{variant}'; expected one of {', '.join(VARIANTS)}")
    spec = VARIANTS[variant]
    issued = today or date.today()

    po_value = float(po["po_value"])

    # Two generated invoices must not look like a re-issue of each other.
    # R7's near-duplicate check is same vendor, same amount (within a rupee),
    # inside the date window - and billing the same 60% of two POs that happen
    # to hold the same value, for the same vendor, on the same day is exactly
    # that. It would be a correct reading of a history this button created, and
    # it would land on whichever variant was run second.
    #
    # So each invoice gets its own small, deterministic amount. Proportional to
    # the PO so a small PO cannot be pushed out of its variant's band, floored
    # at two rupees so it always clears the one-rupee tolerance, and salted
    # with the PO number so two POs generated in the same second still differ.
    # It moves the billed fraction by at most a tenth of a percent.
    salt = (sequence + sum(ord(ch) for ch in po["po_number"])) % 97
    nudge = max(2.0, po_value * salt / 100_000.0)

    figures = compose_figures(
        po_value * spec["target_ratio"] + nudge,
        (po.get("description") or "Goods and services").split(" - ")[0],
    )

    stem = "".join(ch for ch in po["po_number"] if ch.isalnum())
    invoice_number = f"{stem}/{issued.strftime('%Y')}/{sequence:03d}"
    pdf = render_invoice(
        vendor=vendor, po=po, invoice_number=invoice_number, invoice_date=issued,
        figures=figures, note=(
            "Generated by the invoice-processing demo to exercise the pipeline against a "
            "purchase order raised in the UI. Not a real commercial document."
        ),
    )
    variance_pct = (figures.total - po_value) / po_value * 100.0 if po_value else 0.0
    return {
        "pdf": pdf,
        "filename": f"test-invoice-{po['po_number']}-{variant}.pdf",
        "invoice_number": invoice_number,
        "invoice_date": issued.isoformat(),
        "variant": variant,
        "label": spec["label"],
        "expect": spec["expect"],
        "why": spec["why"],
        "total": figures.total,
        "subtotal": figures.subtotal,
        "tax": figures.tax,
        "po_value": po_value,
        "variance_pct": round(variance_pct, 2),
    }
