"""The deterministic rule set - the part that decides.

Design principle for the whole project: **the model extracts, the code
decides.** Extraction is fuzzy and needs a model. A payment decision has to be
explainable, repeatable and defensible to a finance team, so it is plain Python
over plain thresholds. Nothing in this file asks an LLM anything.

Two consequences worth noting:

* Every rule returns the same shape - `{rule_id, passed, severity, expected,
  actual, message}` - so the UI can render all nine identically and a reviewer
  can read off exactly what tripped.
* The thresholds live in one `RuleConfig` and are editable at runtime via
  `PATCH /api/config`. Change the PO tolerance from 2% to 5%, re-run the same
  invoice, and the decision flips. The rules are real, not a dressed-up
  recording.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from datetime import date, datetime, timedelta

from .models import (
    ConfidenceBreakdown,
    Decision,
    NormalizedInvoice,
    POMatch,
    RuleResult,
    VendorMatch,
)


# --------------------------------------------------------------------------- #
# Configuration - the only numbers in the system
# --------------------------------------------------------------------------- #

@dataclass
class RuleConfig:
    """Thresholds. Plausible mid-market AP values; not derived from a real
    policy document (see README > Assumptions)."""

    po_tolerance_pct: float = 2.0
    po_over_tolerance_reject_pct: float = 10.0
    duplicate_window_days: int = 30
    confidence_floor: float = 0.75
    arithmetic_tolerance: float = 1.0        # currency units, i.e. +/- INR 1
    stale_invoice_days: int = 180
    po_match_min_score: float = 0.80
    vendor_match_min_score: float = 0.85

    def to_dict(self) -> dict:
        return asdict(self)

    def apply(self, patch: dict) -> list[str]:
        """Apply a partial update, coercing to the declared field types.

        Returns the names of the fields that actually changed, so the API can
        report it and the UI can show "tolerance 2% -> 5%".
        """
        types = {f.name: f.type for f in fields(self)}
        changed: list[str] = []
        for key, value in patch.items():
            if key not in types or value is None:
                continue
            caster = int if types[key] == "int" else float
            try:
                new = caster(value)
            except (TypeError, ValueError):
                continue
            if new != getattr(self, key):
                setattr(self, key, new)
                changed.append(key)
        return changed


# --------------------------------------------------------------------------- #
# Catalogue - rendered by the UI, served at /api/rules
# --------------------------------------------------------------------------- #

RULE_CATALOG: list[dict] = [
    {
        "id": "R1",
        "title": "Required fields present",
        "checks": "Invoice number, invoice date, vendor name and total are all present.",
        "severity": "REJECT if vendor or total is missing, otherwise FLAG",
        "thresholds": [],
    },
    {
        "id": "R2",
        "title": "Invoice arithmetic is internally consistent",
        "checks": "Line-item total plus tax equals the stated invoice total.",
        "severity": "FLAG",
        "thresholds": ["arithmetic_tolerance"],
    },
    {
        "id": "R3",
        "title": "Vendor is on the approved list",
        "checks": "The extracted vendor name resolves to an approved vendor in the master.",
        "severity": "REJECT",
        "thresholds": ["vendor_match_min_score"],
    },
    {
        "id": "R4",
        "title": "A purchase order was matched with confidence",
        "checks": "A PO was identified with a match score at or above the floor.",
        "severity": "FLAG",
        "thresholds": ["po_match_min_score"],
    },
    {
        "id": "R5",
        "title": "Invoice total is within PO tolerance",
        "checks": "Invoice total does not exceed the matched PO value beyond tolerance.",
        "severity": "FLAG up to the reject threshold, then REJECT",
        "thresholds": ["po_tolerance_pct", "po_over_tolerance_reject_pct"],
    },
    {
        "id": "R6",
        "title": "Cumulative billing does not over-consume the PO",
        "checks": "Everything already billed against the PO, plus this invoice, stays within the PO value.",
        "severity": "REJECT",
        "thresholds": ["po_tolerance_pct"],
    },
    {
        "id": "R7",
        "title": "Not a duplicate submission",
        "checks": "No byte-identical prior submission, and no same-vendor same-amount invoice inside the duplicate window.",
        "severity": "REJECT on an exact duplicate, FLAG on a near duplicate",
        "thresholds": ["duplicate_window_days", "arithmetic_tolerance"],
    },
    {
        "id": "R8",
        "title": "Invoice date is plausible",
        "checks": "Invoice date is not in the future and not older than the staleness limit.",
        "severity": "FLAG",
        "thresholds": ["stale_invoice_days"],
    },
    {
        "id": "R9",
        "title": "Extraction confidence clears the floor",
        "checks": "Verified field provenance and model confidence combine above the floor; scanned documents are penalised.",
        "severity": "FLAG - mandatory human review",
        "thresholds": ["confidence_floor"],
    },
]


# --------------------------------------------------------------------------- #
# Evaluation context
# --------------------------------------------------------------------------- #

@dataclass
class RuleContext:
    invoice: NormalizedInvoice
    vendor: VendorMatch
    po: POMatch
    confidence: ConfidenceBreakdown
    config: RuleConfig
    pdf_kind: str | None = None
    # Duplicate detection inputs, resolved by the pipeline against the DB.
    exact_duplicate_run_id: str | None = None
    near_duplicate_run_id: str | None = None
    near_duplicate_detail: str = ""
    # PO ledger for the matched PO.
    po_value: float | None = None
    po_already_billed: float = 0.0
    today: date | None = None

    def now(self) -> date:
        return self.today or date.today()


def _money(value: float | None) -> str:
    """Indian digit grouping with the currency marked.

    A bare "500,000.00" in a decision reason makes the reader supply the
    currency themselves, and western grouping reads wrong to anyone working
    in lakhs. Everything here is INR (see the assumptions in README).
    """
    if value is None:
        return "-"
    whole, _, frac = f"{abs(value):.2f}".partition(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        whole = ",".join(parts + [tail])
    sign = "-" if value < 0 else ""
    return f"{sign}₹{whole}.{frac}"


def _pass(rule: dict, expected: str, actual: str, message: str) -> RuleResult:
    return RuleResult(
        rule_id=rule["id"], title=rule["title"], passed=True, severity="PASS",
        expected=expected, actual=actual, message=message,
    )


def _fail(rule: dict, severity: str, expected: str, actual: str, message: str,
          note: str | None = None) -> RuleResult:
    """`message` is the finding - one sentence, with the numbers that decided it.
    `note` is optional commentary, shown under the finding rather than inside it."""
    return RuleResult(
        rule_id=rule["id"], title=rule["title"], passed=False, severity=severity,  # type: ignore[arg-type]
        expected=expected, actual=actual, message=message, note=note,
    )


def _skip(rule: dict, reason: str) -> RuleResult:
    return RuleResult(
        rule_id=rule["id"], title=rule["title"], passed=True, severity="PASS",
        expected="-", actual="-", message=reason, skipped=True,
    )


BY_ID = {r["id"]: r for r in RULE_CATALOG}


# --------------------------------------------------------------------------- #
# The nine rules
# --------------------------------------------------------------------------- #

def r1_required_fields(ctx: RuleContext) -> RuleResult:
    rule = BY_ID["R1"]
    inv = ctx.invoice
    required = {
        "vendor name": inv.vendor_name_raw,
        "invoice number": inv.invoice_number,
        "invoice date": inv.invoice_date,
        "total": inv.total_amount,
    }
    missing = [name for name, val in required.items() if val in (None, "")]
    if not missing:
        return _pass(rule, "all four fields present", "all four present",
                     "Invoice number, date, vendor and total were all read from the document.")

    critical = [m for m in missing if m in ("vendor name", "total")]
    severity = "REJECT" if critical else "FLAG"
    msg = f"Could not read {', '.join(missing)} from the document."
    if critical:
        msg += " A payment decision cannot be made without the vendor and the amount."
    return _fail(rule, severity, "all four fields present", f"missing: {', '.join(missing)}", msg)


def r2_arithmetic(ctx: RuleContext) -> RuleResult:
    rule = BY_ID["R2"]
    inv = ctx.invoice
    tol = ctx.config.arithmetic_tolerance
    if inv.total_amount is None or inv.line_items_sum is None:
        return _skip(rule, "No line items or no stated total to reconcile - nothing to check.")

    computed = inv.line_items_sum + (inv.tax_amount or 0.0)
    delta = computed - inv.total_amount
    expected = f"line items + tax = stated total (+/- {_money(tol)})"
    actual = (
        f"{_money(inv.line_items_sum)} + {_money(inv.tax_amount or 0.0)} "
        f"= {_money(computed)} vs stated {_money(inv.total_amount)}"
    )
    if abs(delta) <= tol:
        return _pass(rule, expected, actual, "Invoice adds up.")
    return _fail(
        rule, "FLAG", expected, actual,
        f"Arithmetic mismatch of {_money(abs(delta))}: the line items and tax sum to "
        f"{_money(computed)} but the invoice states {_money(inv.total_amount)}.",
    )


def r3_approved_vendor(ctx: RuleContext) -> RuleResult:
    rule = BY_ID["R3"]
    v = ctx.vendor
    floor = ctx.config.vendor_match_min_score
    expected = f"vendor resolves to an approved vendor at score >= {floor:.2f}"
    if v.status == "matched":
        return _pass(rule, expected, f"{v.vendor_name} ({v.vendor_id}) at {v.score:.2f}",
                     f"Vendor resolved to {v.vendor_name} on the approved list.")
    if v.status == "blocked":
        return _fail(rule, "REJECT", expected, f"{v.vendor_name} is blocked",
                     f"{v.vendor_name} is on the vendor master but blocked for payment.")
    best = f"best candidate scored {v.score:.2f}" if v.score else "no candidate"
    return _fail(
        rule, "REJECT", expected, best,
        f"'{ctx.invoice.vendor_name_raw or 'unknown'}' does not resolve to an approved vendor "
        f"({best}). Paying an unapproved vendor is the failure mode this rule exists to stop.",
    )


def r4_po_matched(ctx: RuleContext) -> RuleResult:
    rule = BY_ID["R4"]
    floor = ctx.config.po_match_min_score
    po = ctx.po
    expected = f"a PO matched at score >= {floor:.2f}"
    if po.matched:
        return _pass(rule, expected, f"{po.po_number} at {po.score:.2f} ({po.strategy})",
                     f"Matched {po.po_number} with score {po.score:.2f} via {po.strategy.replace('_', ' ')}.")
    if po.candidates:
        ranked = ", ".join(f"{c.po_number} ({c.score:.2f})" for c in po.candidates[:3])
        return _fail(
            rule, "FLAG", expected, f"best {po.candidates[0].score:.2f}, below floor",
            f"No PO cleared the {floor:.2f} match floor. Ranked candidates for a human to pick: {ranked}.",
        )
    return _fail(rule, "FLAG", expected, "no candidate POs",
                 "No purchase order could be associated with this invoice.")


def r5_po_tolerance(ctx: RuleContext) -> RuleResult:
    rule = BY_ID["R5"]
    cfg = ctx.config
    if not ctx.po.matched or ctx.po_value is None or ctx.invoice.total_amount is None:
        return _skip(rule, "No matched PO to compare the total against - R4 already flagged this.")

    total = ctx.invoice.total_amount
    po_value = ctx.po_value
    variance_pct = (total - po_value) / po_value * 100.0 if po_value else 0.0
    expected = f"invoice total within {cfg.po_tolerance_pct:.1f}% of PO value {_money(po_value)}"
    actual = f"{_money(total)} = {variance_pct:+.2f}% vs PO"

    if variance_pct <= cfg.po_tolerance_pct:
        note = (
            f"Invoice is {abs(variance_pct):.2f}% under the PO value - partial billing is normal."
            if variance_pct < 0 else
            f"Invoice is {variance_pct:.2f}% over the PO value, inside the "
            f"{cfg.po_tolerance_pct:.1f}% tolerance."
        )
        return _pass(rule, expected, actual, note)

    if variance_pct <= cfg.po_over_tolerance_reject_pct:
        return _fail(
            rule, "FLAG", expected, actual,
            f"Invoice exceeds {ctx.po.po_number} by {variance_pct:.2f}%, over the "
            f"{cfg.po_tolerance_pct:.1f}% tolerance but within the "
            f"{cfg.po_over_tolerance_reject_pct:.1f}% reject threshold - needs an approver.",
        )
    return _fail(
        rule, "REJECT", expected, actual,
        f"Invoice exceeds {ctx.po.po_number} by {variance_pct:.2f}%, beyond the "
        f"{cfg.po_over_tolerance_reject_pct:.1f}% reject threshold.",
    )


def r6_cumulative_consumption(ctx: RuleContext) -> RuleResult:
    rule = BY_ID["R6"]
    cfg = ctx.config
    if not ctx.po.matched or ctx.po_value is None or ctx.invoice.total_amount is None:
        return _skip(rule, "No matched PO, so there is no consumption ledger to check.")

    # Nothing has been billed against this PO yet, so "cumulative" is just this
    # invoice - which R5 already judged, and judged with the right severity. If
    # this rule also fired here it would upgrade every R5 flag into a rejection
    # and make R5's tolerance band unreachable. R6 exists to catch what
    # per-document logic cannot see; with no prior billing there is nothing
    # cross-document to see.
    if ctx.po_already_billed <= 0:
        return _skip(
            rule,
            "Nothing billed against this PO yet - R5 already judged this invoice's variance "
            "on its own, so there is no cross-invoice total to add up.",
        )

    total = ctx.invoice.total_amount
    billed = ctx.po_already_billed
    allowed = ctx.po_value * (1 + cfg.po_tolerance_pct / 100.0)
    cumulative = billed + total
    expected = f"already billed + this invoice <= {_money(allowed)} (PO + {cfg.po_tolerance_pct:.1f}%)"
    actual = f"{_money(billed)} + {_money(total)} = {_money(cumulative)}"

    if cumulative <= allowed:
        headroom = allowed - cumulative
        return _pass(rule, expected, actual,
                     f"{_money(headroom)} of headroom remains on {ctx.po.po_number} after this invoice.")
    return _fail(
        rule, "REJECT", expected, actual,
        f"{ctx.po.po_number} is already {_money(billed)} spent of {_money(ctx.po_value)}. "
        f"This invoice would take it to {_money(cumulative)} - over by "
        f"{_money(cumulative - allowed)}.",
        note="Each invoice looks acceptable on its own; only the running total catches this.",
    )


def r7_not_duplicate(ctx: RuleContext) -> RuleResult:
    rule = BY_ID["R7"]
    window = ctx.config.duplicate_window_days
    expected = f"no identical prior submission and no near duplicate within {window} days"
    if ctx.exact_duplicate_run_id:
        return _fail(
            rule, "REJECT", expected, f"byte-identical to run {ctx.exact_duplicate_run_id}",
            f"Byte-identical to run {ctx.exact_duplicate_run_id}, already processed.",
            note="Paying this again would be a duplicate payment.",
        )
    if ctx.near_duplicate_run_id:
        return _fail(
            rule, "FLAG", expected, ctx.near_duplicate_detail or "near duplicate found",
            f"Looks like run {ctx.near_duplicate_run_id}: {ctx.near_duplicate_detail}.",
            note="The invoice number differs, so a file-hash check would have missed this.",
        )
    return _pass(rule, expected, "no duplicate found", "No prior submission matches this invoice.")


def r8_date_plausible(ctx: RuleContext) -> RuleResult:
    rule = BY_ID["R8"]
    cfg = ctx.config
    iso = ctx.invoice.invoice_date
    expected = f"invoice date not in the future and not older than {cfg.stale_invoice_days} days"
    if not iso:
        return _skip(rule, "No parseable invoice date - R1 already reported this.")
    try:
        d = date.fromisoformat(iso)
    except ValueError:
        return _skip(rule, f"Invoice date '{iso}' is not a valid date - R1 covers it.")

    today = ctx.now()
    if d > today:
        return _fail(rule, "FLAG", expected, f"{iso} is {(d - today).days} days in the future",
                     f"Invoice is dated {iso}, which is in the future.")
    age = (today - d).days
    if age > cfg.stale_invoice_days:
        return _fail(rule, "FLAG", expected, f"{iso} is {age} days old",
                     f"Invoice is {age} days old, beyond the {cfg.stale_invoice_days}-day staleness limit.")
    return _pass(rule, expected, f"{iso} ({age} days old)", f"Invoice date {iso} is plausible.")


def r9_confidence_floor(ctx: RuleContext) -> RuleResult:
    rule = BY_ID["R9"]
    floor = ctx.config.confidence_floor
    conf = ctx.confidence
    expected = f"extraction confidence >= {floor:.2f}"
    actual = f"{conf.final:.2f} ({conf.explanation})"
    if conf.final >= floor:
        return _pass(rule, expected, actual,
                     f"Extraction confidence {conf.final:.2f} clears the {floor:.2f} floor.")
    reason = (
        "This document has no text layer, so none of the extracted values could be verified "
        "against the file itself - only against the page image."
        if conf.scanned_penalty_applied else
        "Too few extracted values could be traced back to text in the document."
    )
    return _fail(
        rule, "FLAG", expected, actual,
        f"Extraction confidence {conf.final:.2f} is below the {floor:.2f} floor. {reason} "
        f"The correct behaviour here is to be less confident, not to fail - a human must review.",
    )


RULES = [
    r1_required_fields, r2_arithmetic, r3_approved_vendor, r4_po_matched,
    r5_po_tolerance, r6_cumulative_consumption, r7_not_duplicate,
    r8_date_plausible, r9_confidence_floor,
]


def evaluate(ctx: RuleContext) -> list[RuleResult]:
    """Run all nine rules in order. Each is independent; none short-circuits,
    because the audit trail should show every check that was made."""
    return [rule(ctx) for rule in RULES]


# --------------------------------------------------------------------------- #
# Decision
# --------------------------------------------------------------------------- #

def decide(results: list[RuleResult], confidence: ConfidenceBreakdown) -> Decision:
    """Worst severity wins. Deliberately trivial - all the judgment is in the
    rules, and the rules are all visible."""
    failed = [r for r in results if not r.passed]
    rejects = [r for r in failed if r.severity == "REJECT"]
    flags = [r for r in failed if r.severity == "FLAG"]
    # A rule that had nothing to check (no PO to compare against, no date to
    # parse) is reported as skipped rather than counted as a pass, so the
    # headline never overstates how much was verified.
    applied = [r for r in results if not r.skipped]
    skipped = len(results) - len(applied)
    skipped_note = f" {skipped} rule(s) had nothing to check." if skipped else ""

    if rejects:
        decision = "REJECT"
        headline = (
            f"Rejected: {len(rejects)} blocking check"
            f"{'s' if len(rejects) != 1 else ''} failed."
        )
    elif flags:
        decision = "FLAG"
        headline = (
            f"Flagged for review: {len(flags)} check"
            f"{'s' if len(flags) != 1 else ''} need a human."
        )
    else:
        decision = "APPROVE"
        headline = f"Approved: all {len(applied)} applicable checks passed.{skipped_note}"

    # The finding, not the rule's own title. "R6 - Cumulative billing does not
    # over-consume the PO: <message>" restates the rule name the reader can
    # already see next to it and pushes the actual numbers off the first line.
    reasons = [headline]
    for r in rejects + flags:
        reasons.append(f"{r.rule_id}: {r.message}")
    if decision == "APPROVE":
        reasons.append(
            f"Extraction confidence {confidence.final:.2f}; {confidence.explanation}."
        )

    return Decision(
        decision=decision,  # type: ignore[arg-type]
        confidence=round(confidence.final, 3),
        reasons=reasons,
        failed_rules=[r.rule_id for r in rejects + flags],
        requires_human=decision != "APPROVE",
    )


def catalog_with_thresholds(config: RuleConfig) -> list[dict]:
    """The catalogue with the live threshold values spliced in, so the UI can
    never drift from what the engine actually applied."""
    values = config.to_dict()
    out: list[dict] = []
    for rule in RULE_CATALOG:
        out.append({
            **rule,
            "active_thresholds": {k: values[k] for k in rule["thresholds"]},
        })
    return out
