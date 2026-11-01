"""Rule-engine tests. No LLM, no network, no database.

These exist to prove the claim the whole design rests on: the decision is a
pure function of the extracted data. Every scenario in the demo script has a
test here, so a change to a threshold that breaks a scenario breaks a test.

Run:  python tests/test_rules.py      (or: python -m pytest tests/ -q)
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.models import (  # noqa: E402
    ConfidenceBreakdown,
    LineItem,
    NormalizedInvoice,
    POCandidate,
    POMatch,
    VendorMatch,
)
from backend.rules import (  # noqa: E402
    RuleConfig,
    RuleContext,
    catalog_with_thresholds,
    decide,
    evaluate,
)

TODAY = date(2026, 10, 31)


# --------------------------------------------------------------------------- #
# Builders — a clean, fully-passing invoice, which each test then spoils in
# exactly one way. That keeps every assertion about one rule.
# --------------------------------------------------------------------------- #

def invoice(**over) -> NormalizedInvoice:
    base = dict(
        vendor_name_raw="Acme Steel Pvt Ltd",
        vendor_name_canonical="acme steel pvt ltd",
        vendor_gstin="27AAACA1234A1Z5",
        invoice_number="ACM-2026-0412",
        invoice_date=(TODAY - timedelta(days=5)).isoformat(),
        invoice_date_raw="26/10/2026",
        currency="INR",
        po_reference="PO-4471",
        line_items=[
            LineItem(description="MS Plate 10mm", quantity=20,
                     unit_price=4000, line_total=80000),
            LineItem(description="MS Plate 12mm", quantity=10,
                     unit_price=5000, line_total=50000),
        ],
        line_items_sum=130000.0,
        subtotal=130000.0,
        tax_amount=23400.0,
        total_amount=153400.0,
    )
    base.update(over)
    return NormalizedInvoice(**base)


def single_line(amount: float, **over) -> NormalizedInvoice:
    """An invoice for exactly `amount`, so PO-tolerance maths stays readable."""
    return invoice(
        line_items=[LineItem(description="Lot", quantity=1,
                             unit_price=amount, line_total=amount)],
        line_items_sum=amount, subtotal=amount, tax_amount=0.0,
        total_amount=amount, **over)


def vendor(**over) -> VendorMatch:
    base = dict(vendor_id="V001", vendor_name="Acme Steel Pvt Ltd",
                score=1.0, status="matched", considered=[])
    base.update(over)
    return VendorMatch(**base)


def po_match(po_value=1000000.0, already_billed=0.0, score=0.98, **over) -> POMatch:
    cand = POCandidate(
        po_number="PO-4471", vendor_id="V001", description="MS plates Q3",
        po_value=po_value, already_billed=already_billed,
        remaining=po_value - already_billed, score=score,
        score_breakdown={"reference": 1.0},
    )
    base = dict(matched=True, po_number="PO-4471", score=score,
                strategy="explicit_reference", candidates=[cand])
    base.update(over)
    return POMatch(**base)


def confidence(model_conf=0.96, scanned=False, verified=9, total=9) -> ConfidenceBreakdown:
    ratio = (verified / total) if total else None
    final = model_conf * (0.6 if scanned else 1.0)
    return ConfidenceBreakdown(
        model_confidence=model_conf,
        provenance_fields_total=total,
        provenance_fields_verified=verified,
        provenance_verified_ratio=ratio,
        scanned_penalty_applied=scanned,
        final=round(final, 3),
        explanation=("no text layer, nothing verifiable" if scanned
                     else f"{verified}/{total} fields verified against the text layer"),
    )


def run(inv=None, ven=None, po=None, conf=None, cfg=None, **ctx_kw):
    inv = inv if inv is not None else invoice()
    ven = ven if ven is not None else vendor()
    po = po if po is not None else po_match()
    conf = conf if conf is not None else confidence()
    matched = po.candidates[0] if po.candidates else None
    ctx = RuleContext(
        invoice=inv, vendor=ven, po=po, confidence=conf,
        config=cfg or RuleConfig(),
        po_value=ctx_kw.pop("po_value", matched.po_value if matched else None),
        po_already_billed=ctx_kw.pop(
            "po_already_billed", matched.already_billed if matched else 0.0),
        today=TODAY,
        **ctx_kw,
    )
    results = evaluate(ctx)
    return results, decide(results, conf)


def by_id(results):
    return {r.rule_id: r for r in results}


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #

def test_clean_invoice_is_approved():
    results, dec = run()
    assert dec.decision == "APPROVE", [r.message for r in results if not r.passed]
    assert dec.failed_rules == []
    assert dec.requires_human is False
    assert len(results) == 9


def test_every_rule_always_reports():
    """A rule whose precondition is missing must say so, not disappear."""
    results, _ = run(inv=invoice(total_amount=None, line_items=[], line_items_sum=None),
                     po=POMatch(matched=False, candidates=[]))
    assert len(results) == 9
    r = by_id(results)
    assert r["R2"].skipped and r["R5"].skipped and r["R6"].skipped


# --------------------------------------------------------------------------- #
# R1 required fields
# --------------------------------------------------------------------------- #

def test_missing_total_rejects():
    results, dec = run(inv=invoice(total_amount=None))
    assert by_id(results)["R1"].severity == "REJECT"
    assert dec.decision == "REJECT"


def test_missing_invoice_number_only_flags():
    results, dec = run(inv=invoice(invoice_number=None))
    assert by_id(results)["R1"].severity == "FLAG"
    assert dec.decision == "FLAG"


# --------------------------------------------------------------------------- #
# R2 arithmetic
# --------------------------------------------------------------------------- #

def test_arithmetic_mismatch_flags():
    results, dec = run(inv=invoice(total_amount=163400.0))   # 10,000 out
    r = by_id(results)["R2"]
    assert not r.passed and r.severity == "FLAG"
    assert dec.decision == "FLAG"


def test_one_rupee_rounding_passes():
    results, dec = run(inv=invoice(total_amount=153401.0))
    assert by_id(results)["R2"].passed
    assert dec.decision == "APPROVE"


# --------------------------------------------------------------------------- #
# R3 approved vendor
# --------------------------------------------------------------------------- #

def test_unknown_vendor_rejects():
    v = VendorMatch(status="unknown", score=0.0,
                    considered=[{"vendor_id": "V002",
                                 "name": "Acme Steels & Alloys", "score": 0.71}])
    results, dec = run(inv=invoice(vendor_name_raw="Acme Steelworks LLP"), ven=v)
    r = by_id(results)["R3"]
    assert not r.passed and r.severity == "REJECT"
    assert dec.decision == "REJECT"


def test_blocked_vendor_rejects():
    results, dec = run(ven=vendor(status="blocked"))
    assert by_id(results)["R3"].severity == "REJECT"
    assert dec.decision == "REJECT"


# --------------------------------------------------------------------------- #
# Edge case 4 — missing / ambiguous PO reference  (R4)
# --------------------------------------------------------------------------- #

def test_ambiguous_po_flags_with_ranked_candidates():
    cands = [
        POCandidate(po_number="PO-4471", vendor_id="V001", po_value=1000000,
                    already_billed=0, remaining=1000000, score=0.62),
        POCandidate(po_number="PO-4492", vendor_id="V001", po_value=900000,
                    already_billed=0, remaining=900000, score=0.58),
        POCandidate(po_number="PO-4510", vendor_id="V001", po_value=1200000,
                    already_billed=0, remaining=1200000, score=0.41),
    ]
    po = POMatch(matched=False, score=0.62, strategy="vendor_amount_window",
                 candidates=cands)
    results, dec = run(inv=invoice(po_reference=None), po=po,
                       po_value=None, po_already_billed=0.0)
    r = by_id(results)["R4"]
    assert not r.passed and r.severity == "FLAG"
    # The escalation must carry the work already done, not just "I gave up".
    assert "PO-4471" in r.message and "PO-4492" in r.message
    assert dec.decision == "FLAG"
    assert by_id(results)["R5"].skipped and by_id(results)["R6"].skipped


# --------------------------------------------------------------------------- #
# Edge case 5 — tolerance boundary pair  (R5)
# --------------------------------------------------------------------------- #

def test_18_pct_over_approves_and_states_variance():
    """1.8% over a 1,00,000 PO — inside the 2% tolerance."""
    results, dec = run(inv=single_line(101800.0), po=po_match(po_value=100000.0))
    r = by_id(results)["R5"]
    assert r.passed
    assert "+1.80%" in r.actual
    assert dec.decision == "APPROVE"


def test_75_pct_over_flags():
    """7.5% over: past tolerance, inside the reject threshold.

    This is the test that caught R5/R6 overlapping — R6 was firing a REJECT on
    the same comparison and making this band unreachable.
    """
    results, dec = run(inv=single_line(107500.0), po=po_match(po_value=100000.0))
    r = by_id(results)["R5"]
    assert not r.passed and r.severity == "FLAG"
    assert by_id(results)["R6"].skipped, "R6 must abstain with no prior billing"
    assert dec.decision == "FLAG"


def test_over_reject_threshold_rejects():
    results, dec = run(inv=single_line(125000.0), po=po_match(po_value=100000.0))
    assert by_id(results)["R5"].severity == "REJECT"
    assert dec.decision == "REJECT"


def test_under_po_value_passes():
    """Partial delivery is normal and must not flag."""
    results, dec = run(po=po_match(po_value=1000000.0))
    r = by_id(results)["R5"]
    assert r.passed and "under the PO value" in r.message
    assert dec.decision == "APPROVE"


def test_tolerance_is_config_not_hardcoded():
    """The live demo move: widen the tolerance, the decision flips."""
    inv, po = single_line(107500.0), po_match(po_value=100000.0)
    _, strict = run(inv=inv, po=po)
    _, loose = run(inv=inv, po=po, cfg=RuleConfig(po_tolerance_pct=10.0))
    assert strict.decision == "FLAG"
    assert loose.decision == "APPROVE"


def test_config_patch_reports_what_changed():
    cfg = RuleConfig()
    changed = cfg.apply({"po_tolerance_pct": 5.0, "duplicate_window_days": 30})
    assert changed == ["po_tolerance_pct"]      # 30 was already the value
    assert cfg.po_tolerance_pct == 5.0


def test_config_patch_ignores_junk():
    cfg = RuleConfig()
    assert cfg.apply({"nonsense": 1, "po_tolerance_pct": "abc"}) == []
    assert cfg.po_tolerance_pct == 2.0


# --------------------------------------------------------------------------- #
# Edge case 1 — split invoice against one PO  (R6)
# --------------------------------------------------------------------------- #

def test_split_invoice_over_consumes_po_and_rejects():
    """6L against a 10L PO that already has 5L billed.

    Each invoice alone passes R5 — 6L is well under 10L. Only the stateful
    cumulative check sees the problem.
    """
    results, dec = run(inv=single_line(600000.0),
                       po=po_match(po_value=1000000.0, already_billed=500000.0))
    r = by_id(results)

    assert r["R5"].passed, "each invoice alone looks fine against the PO"
    assert not r["R6"].passed and r["R6"].severity == "REJECT"
    # Indian grouping, currency marked: 11,00,000 not 1,100,000.
    assert "₹11,00,000.00" in r["R6"].actual   # projected cumulative
    assert "₹5,00,000.00" in r["R6"].actual    # already billed
    assert dec.decision == "REJECT"


def test_cumulative_within_po_passes():
    results, dec = run(inv=single_line(400000.0),
                       po=po_match(po_value=1000000.0, already_billed=500000.0))
    r = by_id(results)["R6"]
    assert r.passed and "headroom" in r.message
    assert dec.decision == "APPROVE"


# --------------------------------------------------------------------------- #
# Edge case 2 — duplicates  (R7)
# --------------------------------------------------------------------------- #

def test_exact_duplicate_rejects():
    results, dec = run(exact_duplicate_run_id="RUN_20261020T101500")
    r = by_id(results)["R7"]
    assert not r.passed and r.severity == "REJECT"
    assert dec.decision == "REJECT"


def test_near_duplicate_reissue_flags_and_links_original():
    """Same vendor, same amount, different invoice number — hashing misses this."""
    results, dec = run(
        inv=invoice(invoice_number="ACM-2026-0419"),
        near_duplicate_run_id="RUN_20261025T090000",
        near_duplicate_detail="ACM-2026-0412 for the same 1,53,400.00 on 2026-10-25",
    )
    r = by_id(results)["R7"]
    assert not r.passed and r.severity == "FLAG"
    assert "RUN_20261025T090000" in r.message       # the finding points at the original
    # The explanation moved out of the finding and into the note, so the banner
    # stays scannable. It must still be carried somewhere.
    assert "hash check would have missed" in (r.note or "")
    assert dec.decision == "FLAG"


# --------------------------------------------------------------------------- #
# R8 date plausibility
# --------------------------------------------------------------------------- #

def test_future_date_flags():
    results, dec = run(inv=invoice(
        invoice_date=(TODAY + timedelta(days=3)).isoformat()))
    assert by_id(results)["R8"].severity == "FLAG"
    assert dec.decision == "FLAG"


def test_stale_invoice_flags():
    results, dec = run(inv=invoice(
        invoice_date=(TODAY - timedelta(days=200)).isoformat()))
    r = by_id(results)["R8"]
    assert not r.passed and "200 days old" in r.message
    assert dec.decision == "FLAG"


def test_unparseable_date_skips_not_crashes():
    results, _ = run(inv=invoice(invoice_date="not-a-date"))
    assert by_id(results)["R8"].skipped


# --------------------------------------------------------------------------- #
# Edge case 3 — scanned image-only PDF  (R9)
# --------------------------------------------------------------------------- #

def test_low_confidence_scan_flags_even_when_everything_else_passes():
    """The point: degrade to human review, don't fail and don't guess."""
    conf = confidence(model_conf=0.82, scanned=True, verified=0, total=9)
    results, dec = run(conf=conf, pdf_kind="scanned")
    r = by_id(results)

    others = [x for x in results if x.rule_id != "R9"]
    assert all(x.passed for x in others), "only R9 should fail"
    assert not r["R9"].passed and r["R9"].severity == "FLAG"
    assert "no text layer" in r["R9"].message      # explains *why* confidence fell
    assert "be less confident, not to fail" in r["R9"].message
    assert dec.decision == "FLAG"
    assert dec.requires_human is True


def test_clean_scan_above_floor_still_approves():
    """A scanned document is not automatically flagged — only a low-confidence one."""
    conf = confidence(model_conf=0.98, scanned=False, verified=9, total=9)
    results, dec = run(conf=conf, pdf_kind="scanned")
    assert by_id(results)["R9"].passed
    assert dec.decision == "APPROVE"


# --------------------------------------------------------------------------- #
# Rollup + the rules-as-data contract
# --------------------------------------------------------------------------- #

def test_reject_beats_flag():
    """Worst severity wins, regardless of how many flags there are."""
    results, dec = run(
        inv=invoice(total_amount=163400.0),                      # R2 flag
        conf=confidence(model_conf=0.50),                        # R9 flag
        po=po_match(po_value=1000000.0, already_billed=900000.0),  # R6 reject
    )
    sev = {r.rule_id: r.severity for r in results if not r.passed}
    assert sev["R2"] == "FLAG" and sev["R9"] == "FLAG" and sev["R6"] == "REJECT"
    assert dec.decision == "REJECT"
    assert set(dec.failed_rules) == {"R2", "R6", "R9"}


def test_decision_is_deterministic():
    first = run()[1]
    for _ in range(20):
        assert run()[1].model_dump() == first.model_dump()


def test_every_rule_is_declared_and_served():
    """No rule may exist in code without appearing in the served rule set."""
    cfg = RuleConfig()
    served = catalog_with_thresholds(cfg)
    declared = {r["id"] for r in served}
    executed = {r.rule_id for r in run()[0]}
    assert declared == executed == {f"R{i}" for i in range(1, 10)}
    # Every threshold a rule names must exist in the config it is served with.
    live = cfg.to_dict()
    for spec in served:
        for key in spec["thresholds"]:
            assert key in live, f"{spec['id']} references unknown threshold {key}"
        assert spec["active_thresholds"] == {k: live[k] for k in spec["thresholds"]}


def test_decision_reasons_name_the_failing_rules():
    _, dec = run(inv=single_line(600000.0),
                 po=po_match(po_value=1000000.0, already_billed=500000.0))
    assert dec.reasons[0].startswith("Rejected:")
    assert any("R6" in r for r in dec.reasons[1:])


if __name__ == "__main__":
    import traceback

    fns = {k: v for k, v in sorted(globals().items()) if k.startswith("test_")}
    failed = 0
    for name, fn in fns.items():
        try:
            fn()
            print(f"  PASS  {name}")
        except Exception:
            failed += 1
            print(f"  FAIL  {name}")
            traceback.print_exc()
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
