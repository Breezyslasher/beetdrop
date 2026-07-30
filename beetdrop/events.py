"""Server-Sent Events broadcasting.

Each subscriber gets its own asyncio queue; publish fans out to all of
them. Publishing never blocks: a subscriber that stopped draining (dead
connection, saturated queue) is dropped rather than allowed to stall the
worker.
"""

from __future__ import annotations

import asyncio
import json

QUEUE_LIMIT = 256


class Broadcaster:
    def __init__(self):
        self._subscribers: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_LIMIT)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    def publish(self, event: dict) -> None:
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                self._subscribers.discard(queue)


def sse_format(event: dict) -> str:
    return "event: job\ndata: %s\n\n" % json.dumps(event, default=str)
