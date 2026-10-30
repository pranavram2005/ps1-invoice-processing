"""Stages 4 and 5: resolve the vendor, then find the purchase order.

Both are scoring problems with an explicit floor, and both show their work:
every candidate considered comes back with its score and a component breakdown,
so the UI can render *why* a PO won rather than just announcing a winner.

One policy decision is encoded here and worth stating plainly. If the invoice
does not name a PO, the system can still infer one from vendor, amount and
date - but an inferred PO is capped at a score below the match floor, so it can
never auto-approve. The system escalates with the work already done (ranked
candidates, scores, reasons) instead of guessing. See
`INFERRED_PO_SCORE_CEILING`.
"""
from __future__ import annotations

from datetime import date, timedelta

from rapidfuzz import fuzz

from .models import NormalizedInvoice, POCandidate, POMatch, VendorMatch
from .normalize import canonical_vendor_name, normalize_po_reference
from .rules import RuleConfig

# A PO that the invoice never named, inferred from vendor + amount + date, is
# capped here. The match floor is 0.80, so this ceiling guarantees an inferred
# PO always lands in front of a human. Guessing which PO to bill is exactly the
# decision an AP system should not make silently.
INFERRED_PO_SCORE_CEILING = 0.78


# --------------------------------------------------------------------------- #
# Stage 4 - vendor
# --------------------------------------------------------------------------- #

def resolve_vendor(
    invoice: NormalizedInvoice, vendors: list[dict], config: RuleConfig,
) -> VendorMatch:
    """GSTIN is an exact identifier, so it wins outright when present on both
    sides. Otherwise fall back to fuzzy name matching over the registered name
    and every known alias."""
    if not vendors:
        return VendorMatch(status="unknown", score=0.0)

    gstin = (invoice.vendor_gstin or "").strip().upper()
    if gstin:
        for vendor in vendors:
            if (vendor.get("gstin") or "").strip().upper() == gstin:
                return VendorMatch(
                    vendor_id=vendor["vendor_id"], vendor_name=vendor["name"], score=1.0,
                    status="blocked" if vendor["status"] != "approved" else "matched",
                    considered=[{
                        "vendor_id": vendor["vendor_id"], "name": vendor["name"],
                        "score": 1.0, "via": "GSTIN exact match",
                    }],
                )

    target = invoice.vendor_name_canonical or canonical_vendor_name(invoice.vendor_name_raw)
    if not target:
        return VendorMatch(status="unknown", score=0.0)

    considered: list[dict] = []
    for vendor in vendors:
        names = [vendor["name"], *vendor.get("aliases", [])]
        best, via = 0.0, vendor["name"]
        for name in names:
            canonical = canonical_vendor_name(name) or ""
            score = max(
                fuzz.WRatio(target, canonical),
                fuzz.token_set_ratio(target, canonical),
            ) / 100.0
            if score > best:
                best, via = score, name
        considered.append({
            "vendor_id": vendor["vendor_id"], "name": vendor["name"],
            "score": round(best, 3), "via": via, "status": vendor["status"],
        })

    considered.sort(key=lambda c: c["score"], reverse=True)
    top = considered[0]
    status = "unknown"
    if top["score"] >= config.vendor_match_min_score:
        status = "blocked" if top.get("status") != "approved" else "matched"

    return VendorMatch(
        vendor_id=top["vendor_id"] if status != "unknown" else None,
        vendor_name=top["name"] if status != "unknown" else None,
        score=round(top["score"], 3),
        status=status,  # type: ignore[arg-type]
        considered=considered[:5],
    )


# --------------------------------------------------------------------------- #
# Stage 5 - purchase order
# --------------------------------------------------------------------------- #

def _reference_score(invoice_ref: str | None, po_number: str) -> tuple[float, str]:
    if not invoice_ref:
        return 0.0, "invoice names no PO"
    a = normalize_po_reference(invoice_ref) or ""
    b = normalize_po_reference(po_number) or ""
    if not a or not b:
        return 0.0, "no comparable reference"
    if a == b:
        return 1.0, f"reference '{invoice_ref}' matches exactly"
    if b in a or a in b:
        return 0.92, f"reference '{invoice_ref}' contains {po_number}"
    ratio = fuzz.token_set_ratio(a, b) / 100.0
    if ratio >= 0.75:
        return round(ratio * 0.9, 3), f"reference '{invoice_ref}' is a near match for {po_number}"
    return 0.0, f"reference '{invoice_ref}' does not match {po_number}"


def _amount_score(total: float | None, po: dict) -> tuple[float, str]:
    """How well does this invoice amount fit what is left on the PO?

    Scored against the *remaining* balance rather than the PO value, because a
    vendor billing out the balance of a part-consumed PO is the common case and
    should score as a good fit.
    """
    if total is None or not po["po_value"]:
        return 0.0, "no amount to compare"
    remaining = max(po["remaining"], 0.0)
    reference = remaining if remaining > 0 else po["po_value"]
    diff_ratio = abs(total - reference) / max(po["po_value"], 1.0)
    score = max(0.0, 1.0 - diff_ratio)
    return round(score, 3), (
        f"{total:,.0f} against {reference:,.0f} available on a {po['po_value']:,.0f} PO"
    )


def _date_score(invoice_date: str | None, po: dict) -> tuple[float, str]:
    if not invoice_date:
        return 0.3, "no invoice date to compare"
    try:
        d = date.fromisoformat(invoice_date)
    except ValueError:
        return 0.3, "invoice date unparseable"
    try:
        issued = date.fromisoformat(po["issued_date"]) if po.get("issued_date") else None
        expiry = date.fromisoformat(po["expiry_date"]) if po.get("expiry_date") else None
    except ValueError:
        return 0.5, "PO dates unparseable"

    if issued and d < issued - timedelta(days=1):
        return 0.0, f"invoice predates the PO ({po['issued_date']})"
    if expiry and d > expiry:
        overdue = (d - expiry).days
        if overdue <= 30:
            return 0.5, f"invoice is {overdue} days past PO expiry"
        return 0.1, f"invoice is {overdue} days past PO expiry"
    return 1.0, "invoice date falls inside the PO validity window"


def match_po(
    invoice: NormalizedInvoice, vendor: VendorMatch, pos: list[dict], config: RuleConfig,
) -> POMatch:
    """Score every plausible PO and return the ranked list with the winner."""
    pool = [p for p in pos if p["status"] == "open"]
    if vendor.vendor_id:
        scoped = [p for p in pool if p["vendor_id"] == vendor.vendor_id]
        # If the vendor resolved but has no open POs, fall back to the whole
        # pool so the UI can show that the search happened and found nothing
        # for this vendor, rather than an empty panel.
        pool = scoped or pool

    if not pool:
        return POMatch(matched=False, strategy="none", note="No open purchase orders to match against.")

    has_reference = bool(invoice.po_reference)
    candidates: list[POCandidate] = []

    for po in pool:
        ref_score, ref_note = _reference_score(invoice.po_reference, po["po_number"])
        amt_score, amt_note = _amount_score(invoice.total_amount, po)
        date_score, date_note = _date_score(invoice.invoice_date, po)

        if ref_score >= 1.0:
            # The invoice names this PO exactly, so its identity is settled.
            # Deliberately NOT blended with the amount: scoring identity down
            # because the amount looks wrong would conflate two questions. "Which
            # PO is this?" is this stage's job; "is this amount acceptable
            # against that PO?" belongs to R5 and R6, which judge it explicitly
            # and with the right severity. Blending them also produced a
            # perverse result - a part-consumed PO made a perfectly valid
            # follow-up invoice match *worse*.
            total_score = 1.0
            breakdown = {
                "reference": 1.0,
                "amount": round(amt_score, 3),
                "date": round(date_score, 3),
            }
            weights = (
                "an exact PO reference settles identity on its own; amount and date are shown "
                "for information and are judged by R5 and R6"
            )
            reason = f"{ref_note}; {amt_note}; {date_note}"
        elif has_reference:
            # A near-miss reference is still mostly about the reference, but now
            # the amount and date carry some corroborating weight.
            total_score = 0.80 * ref_score + 0.12 * amt_score + 0.08 * date_score
            breakdown = {
                "reference": round(ref_score, 3),
                "amount": round(amt_score, 3),
                "date": round(date_score, 3),
            }
            weights = "reference 0.80, amount 0.12, date 0.08"
            reason = f"{ref_note}; {amt_note}; {date_note}"
        else:
            raw = 0.60 * amt_score + 0.40 * date_score
            total_score = min(raw, INFERRED_PO_SCORE_CEILING)
            breakdown = {
                "amount": round(amt_score, 3),
                "date": round(date_score, 3),
                "uncapped": round(raw, 3),
            }
            weights = f"amount 0.60, date 0.40, then capped at {INFERRED_PO_SCORE_CEILING}"
            reason = (
                f"inferred from vendor, amount and date ({amt_note}; {date_note}); "
                f"capped at {INFERRED_PO_SCORE_CEILING} because the invoice names no PO"
            )

        candidates.append(POCandidate(
            po_number=po["po_number"], vendor_id=po["vendor_id"],
            description=po.get("description"), po_value=po["po_value"],
            already_billed=po["already_billed"], remaining=po["remaining"],
            issued_date=po.get("issued_date"), expiry_date=po.get("expiry_date"),
            score=round(total_score, 3), score_breakdown=breakdown,
            weights=weights, reason=reason,
        ))

    candidates.sort(key=lambda c: c.score, reverse=True)
    best = candidates[0]
    matched = best.score >= config.po_match_min_score

    if has_reference:
        strategy = (
            "explicit_reference" if best.score_breakdown.get("reference") == 1.0
            else "fuzzy_reference"
        )
    else:
        strategy = "vendor_amount_window"

    if matched:
        note = f"{best.po_number} matched at {best.score:.2f} via {strategy.replace('_', ' ')}."
    elif has_reference:
        note = (
            f"The invoice names '{invoice.po_reference}', but no open PO matched it well enough "
            f"(best {best.po_number} at {best.score:.2f}, floor {config.po_match_min_score:.2f})."
        )
    else:
        note = (
            f"The invoice names no PO. {len(candidates)} candidate(s) were ranked by amount and "
            f"date; the best scored {best.score:.2f}, under the {config.po_match_min_score:.2f} "
            f"floor by design, so a human picks."
        )

    # Report the strategy that was attempted whether or not it cleared the
    # floor - "we tried to infer it and got this far" is more useful to a
    # reviewer than "none".
    return POMatch(
        matched=matched,
        po_number=best.po_number if matched else None,
        score=best.score,
        strategy=strategy,  # type: ignore[arg-type]
        candidates=candidates[:5],
        note=note,
    )
