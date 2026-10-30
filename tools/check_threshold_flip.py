"""Verify the live threshold-change move end to end.

    python tools/check_threshold_flip.py [base_url]

This is the most load-bearing claim the demo makes: that the rules are real
configuration rather than a dressed-up recording. The proof is mechanical -
raise the PO tolerance past the invoice's variance, replay the *same* file, and
the decision has to flip from FLAG to APPROVE. Then put the threshold back and
confirm it flips again.

If this ever stops working, the demo should not be given.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8077"
TARGET = "INV-SSW-2241_sharma_over_tolerance.pdf"      # 7.50% over its PO


def call(path: str, method: str = "GET", payload: dict | None = None):
    data = json.dumps(payload).encode() if payload is not None else (
        None if method == "GET" else b""
    )
    request = urllib.request.Request(BASE + path, method=method, data=data)
    if payload is not None:
        request.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())


def wait_for_run(run_id: str, timeout: float = 60.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        run = call(f"/api/runs/{run_id}")
        if run["status"] != "running":
            return run
        time.sleep(0.4)
    raise TimeoutError(f"run {run_id} did not finish within {timeout}s")


def find_run(filename: str) -> dict:
    matches = [r for r in call("/api/runs") if r["filename"] == filename]
    if not matches:
        raise SystemExit(
            f"no run found for {filename} - run: python -m backend.seed --runs --clear"
        )
    return matches[0]


def replay(run_id: str) -> dict:
    call(f"/api/runs/{run_id}/replay", method="POST")
    return wait_for_run(run_id)


def verdict(run: dict) -> str | None:
    return (run.get("decision") or {}).get("decision")


def describe(run: dict) -> str:
    decision = run.get("decision") or {}
    failed = ",".join(decision.get("failed_rules", [])) or "-"
    return f"{decision.get('decision')} (failed: {failed})"


def r5_line(run: dict) -> str:
    for rule in run.get("rules", []):
        if rule["rule_id"] == "R5":
            state = "pass" if rule["passed"] else rule["severity"]
            return f"R5 {state}: {rule['actual']}"
    return "R5 not evaluated"


def main() -> int:
    original = call("/api/config")["config"]["po_tolerance_pct"]
    print(f"base: {BASE}\ntarget: {TARGET}\nstarting po_tolerance_pct = {original}\n")

    run_id = find_run(TARGET)["run_id"]
    problems: list[str] = []

    try:
        before = replay(run_id)
        print(f"1. at {original:.1f}% tolerance -> {describe(before)}")
        print(f"   {r5_line(before)}")
        if verdict(before) != "FLAG":
            problems.append(f"expected FLAG at {original}% tolerance, got {describe(before)}")

        changed = call("/api/config", "PATCH", {"po_tolerance_pct": 10.0})
        print(f"\n2. PATCH /api/config -> changed {changed['changed']}, "
              f"po_tolerance_pct now {changed['config']['po_tolerance_pct']}")

        after = replay(run_id)
        print(f"\n3. at 10.0% tolerance -> {describe(after)}")
        print(f"   {r5_line(after)}")
        if verdict(after) != "APPROVE":
            problems.append(f"expected APPROVE at 10% tolerance, got {describe(after)}")
    finally:
        call("/api/config", "PATCH", {"po_tolerance_pct": original})
        restored = replay(run_id)
        print(f"\n4. restored to {original:.1f}% -> {describe(restored)}")
        if verdict(restored) != "FLAG":
            problems.append(f"expected FLAG after restoring tolerance, got {describe(restored)}")

    if problems:
        print(f"\n{len(problems)} problem(s):")
        for line in problems:
            print(f"  - {line}")
        return 1

    print("\nthreshold change flips the decision and flips back - the rules are live config")
    return 0


if __name__ == "__main__":
    sys.exit(main())
