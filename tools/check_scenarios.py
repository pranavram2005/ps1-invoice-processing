"""Assert that each sample invoice fails for the *right* reason.

    python tools/check_scenarios.py

Checking the decision alone is not enough. An invoice can reach FLAG by
accident - a misparsed date, a vendor that did not resolve - and look like a
pass. This asserts the exact set of rules each scenario is supposed to trip, so
a regression that gets the right answer for the wrong reason still fails.

Run `python -m backend.seed --runs --clear` first.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import db    # noqa: E402

# filename -> (expected decision, exact set of rules expected to fail)
EXPECTED: dict[str, tuple[str, set[str]]] = {
    "INV-MER-0881_meridian_happy.pdf": ("APPROVE", set()),
    "INV-SSW-2211_sharma_split_po.pdf": ("REJECT", {"R6"}),
    "INV-NL-118_nalanda_original.pdf": ("APPROVE", set()),
    "INV-NL-127_nalanda_reissue.pdf": ("FLAG", {"R7"}),
    "INV-KAV-7731_kaveri_scanned.pdf": ("FLAG", {"R9"}),
    "INV-ORB-5540_orbit_no_po.pdf": ("FLAG", {"R4"}),
    "INV-SSW-2240_sharma_within_tolerance.pdf": ("APPROVE", set()),
    "INV-SSW-2241_sharma_over_tolerance.pdf": ("FLAG", {"R5"}),
    "INV-MER-0902_meridian_arithmetic.pdf": ("FLAG", {"R2"}),
    # R5's reject severity, which nothing else reaches: 18% over, past the 10%
    # threshold rather than inside the flag band.
    "INV-ORB-5561_orbit_over_reject.pdf": ("REJECT", {"R5"}),
    # Both R3 cases also trip R4, and that is not incidental: a vendor who is
    # blocked or unrecognised has no open PO to match against. Asserting the
    # pair keeps the two apart - what differs is the stage-4 payload, where the
    # blocked vendor matches confidently on GSTIN and the unknown one not at all.
    "INV-TCE-3310_tricity_blocked_vendor.pdf": ("REJECT", {"R3", "R4"}),
    "INV-VEC-8120_vectra_unknown_vendor.pdf": ("REJECT", {"R3", "R4"}),
    "INV-NL-094_nalanda_stale_date.pdf": ("FLAG", {"R8"}),
    "INV-MER-NONUM_meridian_missing_number.pdf": ("FLAG", {"R1"}),
}


def main() -> int:
    runs = {r.filename: r for r in db.list_runs()}
    failures: list[str] = []
    print(f"{'file':<44} {'decision':<9} {'failed rules':<16} verdict")
    print("-" * 86)

    for filename, (want_decision, want_rules) in EXPECTED.items():
        run = runs.get(filename)
        if run is None:
            failures.append(f"{filename}: no run found")
            print(f"{filename:<44} {'-':<9} {'-':<16} MISSING")
            continue

        got_rules = set(run.failed_rules)
        ok = run.decision == want_decision and got_rules == want_rules
        if not ok:
            failures.append(
                f"{filename}: wanted {want_decision} via {sorted(want_rules) or 'no failures'}, "
                f"got {run.decision} via {sorted(got_rules) or 'no failures'}"
            )
        print(
            f"{filename:<44} {str(run.decision):<9} "
            f"{','.join(sorted(got_rules)) or '-':<16} {'ok' if ok else 'MISMATCH'}"
        )

    extra = set(runs) - set(EXPECTED)
    if extra:
        print(f"\nnote: {len(extra)} run(s) not covered by this check: {sorted(extra)}")

    if failures:
        print(f"\n{len(failures)} problem(s):")
        for line in failures:
            print(f"  - {line}")
        return 1

    print(f"\nall {len(EXPECTED)} scenarios reached the intended decision via the intended rules")
    return 0


if __name__ == "__main__":
    sys.exit(main())
