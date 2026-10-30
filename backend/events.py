"""In-process pub/sub for the live run view.

One queue per connected browser, keyed by run id. The pipeline publishes a
stage transition; every subscriber on that run gets it. Deliberately
in-process: this is a single-container deployment, and a Redis hop would add an
operational dependency to buy nothing.

Two behaviours matter for the UI not to lie:

* **Late subscribers get a replay.** `GET /api/runs/{id}/events` sends the
  current state of all seven stages from the database before attaching the live
  stream, so a browser that connects mid-run (or after a reload) renders the
  correct state rather than an empty timeline.
* **Finished runs still answer.** If the run is already complete when a client
  subscribes, it gets the snapshot and an immediate `done`, instead of hanging
  on a stream that will never produce another event.
"""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict

log = logging.getLogger("events")

QUEUE_MAXSIZE = 256


class EventBroker:
    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue]] = defaultdict(set)
        self._finished: set[str] = set()

    def subscribe(self, run_id: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_MAXSIZE)
        self._subscribers[run_id].add(queue)
        return queue

    def unsubscribe(self, run_id: str, queue: asyncio.Queue) -> None:
        self._subscribers.get(run_id, set()).discard(queue)
        if not self._subscribers.get(run_id):
            self._subscribers.pop(run_id, None)

    def publish(self, run_id: str, event: dict) -> None:
        """Fan an event out to every subscriber. Never blocks and never raises:
        a wedged browser tab must not be able to stall the pipeline."""
        for queue in list(self._subscribers.get(run_id, set())):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                log.warning("dropping event for %s - subscriber queue full", run_id)

    def mark_finished(self, run_id: str) -> None:
        self._finished.add(run_id)
        self.publish(run_id, {"type": "done", "run_id": run_id})

    def is_finished(self, run_id: str) -> bool:
        return run_id in self._finished

    def subscriber_count(self, run_id: str) -> int:
        return len(self._subscribers.get(run_id, set()))


broker = EventBroker()
