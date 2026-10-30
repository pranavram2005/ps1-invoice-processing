"""Run a single sample invoice through the pipeline from the command line.

    python tools/run_one.py INV-MER-0881_meridian_happy.pdf

Prints every stage summary and the decision with its reasons. Useful for
debugging a scenario without a browser, and for checking a prompt change
against one invoice before spending a full seeding run on all nine.
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import db, pipeline, seed, state     # noqa: E402
from backend.config import SAMPLE_INVOICE_DIR     # noqa: E402


async def main(name: str) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s - %(message)s")
    db.init_db()
    seed.seed_masters_if_empty()
    state.load_from_db()

    path = SAMPLE_INVOICE_DIR / name
    if not path.exists():
        print(f"not found: {path}")
        print("available:")
        for candidate in sorted(SAMPLE_INVOICE_DIR.glob("*.pdf")):
            print(f"  {candidate.name}")
        return 2

    run_id = pipeline.new_run_id()
    result = await pipeline.execute(run_id, name, path.read_bytes(), state.current())

    print(f"\n=== {name} -> {run_id} ({result.duration_ms} ms) ===")
    for stage in result.stages:
        marker = {"done": "ok", "failed": "!!", "skipped": "--"}.get(stage.status, "??")
        print(f" {marker} {stage.seq}. {stage.label:34} {stage.summary}")

    if result.error:
        print(f"\nFAILED: {result.error}")
        return 1

    print("\n--- rules ---")
    for rule in result.rules:
        mark = "skip" if rule.skipped else ("pass" if rule.passed else rule.severity)
        print(f" {rule.rule_id} {mark:6} {rule.title}")
        if not rule.passed and not rule.skipped:
            print(f"        expected: {rule.expected}")
            print(f"        actual:   {rule.actual}")

    decision = result.decision
    if decision:
        print(f"\n=== {decision.decision}  (confidence {decision.confidence:.2f}) ===")
        for i, reason in enumerate(decision.reasons):
            print(f"  {i}. {reason}")
    return 0


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "INV-MER-0881_meridian_happy.pdf"
    sys.exit(asyncio.run(main(target)))
