"""Verify the live run stream.

    python tools/check_sse.py [base_url]

Starts a run, immediately attaches to its SSE stream, and asserts the browser
would see what the run view needs: a snapshot, every one of the seven stages
transitioning to a terminal state, the finish event, and a close.

Also exercises the late-subscriber path, which matters more than it sounds -
a reviewer reloading the page mid-run must not get an empty timeline.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8077"
SAMPLE = "INV-SSW-2211_sharma_split_po.pdf"
EXPECTED_STAGES = 7


def post(path: str) -> dict:
    request = urllib.request.Request(BASE + path, method="POST", data=b"")
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read())


def read_events(run_id: str, timeout: float = 45.0) -> list[tuple[str, dict]]:
    """Minimal SSE reader: accumulate event/data pairs until `done`."""
    events: list[tuple[str, dict]] = []
    deadline = time.time() + timeout
    url = f"{BASE}/api/runs/{run_id}/events"
    with urllib.request.urlopen(url, timeout=timeout) as stream:
        name: str | None = None
        for raw in stream:
            if time.time() > deadline:
                break
            line = raw.decode("utf-8").rstrip("\n")
            if line.startswith("event:"):
                name = line.split(":", 1)[1].strip()
            elif line.startswith("data:") and name:
                try:
                    events.append((name, json.loads(line.split(":", 1)[1].strip())))
                except json.JSONDecodeError:
                    events.append((name, {}))
                if name == "done":
                    break
                name = None
    return events


def main() -> int:
    print(f"base: {BASE}")
    started = post(f"/api/samples/{SAMPLE}/run")
    run_id = started["run_id"]
    print(f"started {run_id}")

    events = read_events(run_id)
    kinds: dict[str, int] = {}
    for name, _ in events:
        kinds[name] = kinds.get(name, 0) + 1
    print(f"received {len(events)} event(s): {kinds}")

    problems: list[str] = []

    if not any(name == "snapshot" for name, _ in events):
        problems.append("no snapshot event - a late subscriber would see an empty timeline")
    if not any(name == "done" for name, _ in events):
        problems.append("stream never closed with 'done'")

    # Which stages reached a terminal state over the stream (or in the snapshot
    # for a stage that had already finished before we attached)?
    terminal: dict[int, str] = {}
    for name, payload in events:
        if name == "stage":
            stage = payload.get("stage", {})
            if stage.get("status") in ("done", "failed", "skipped"):
                terminal[stage["seq"]] = stage["status"]
        elif name == "snapshot":
            for stage in payload.get("stages", []):
                if stage.get("status") in ("done", "failed", "skipped"):
                    terminal[stage["seq"]] = stage["status"]

    live_stage_events = kinds.get("stage", 0)
    print(f"stages observed terminal: {len(terminal)}/{EXPECTED_STAGES}; "
          f"live stage events: {live_stage_events}")
    if len(terminal) < EXPECTED_STAGES:
        missing = sorted(set(range(1, EXPECTED_STAGES + 1)) - set(terminal))
        problems.append(f"stages never reached a terminal state: {missing}")
    if live_stage_events == 0:
        problems.append(
            "no live stage events - the stream only replayed the snapshot, so the run view "
            "would not animate (is STAGE_DELAY_MS too low to observe?)"
        )

    finished = [p for name, p in events if name == "run_finished"]
    if finished:
        decision = (finished[0].get("decision") or {}).get("decision")
        print(f"run_finished: status={finished[0].get('status')} decision={decision}")
    else:
        problems.append("no run_finished event")

    # Late subscriber: attach to the now-complete run and expect snapshot + done.
    late = read_events(run_id, timeout=15.0)
    late_kinds = [name for name, _ in late]
    print(f"late subscriber saw: {late_kinds}")
    if "snapshot" not in late_kinds or "done" not in late_kinds:
        problems.append("a late subscriber to a finished run did not get snapshot + done")

    if problems:
        print(f"\n{len(problems)} problem(s):")
        for line in problems:
            print(f"  - {line}")
        return 1

    print("\nSSE stream behaves correctly: snapshot, live stage transitions, finish, close")
    return 0


if __name__ == "__main__":
    sys.exit(main())
