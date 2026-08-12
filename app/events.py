"""Fan-out bus for pushing alerts to every open browser at once.

Server-Sent Events rather than WebSockets on purpose: the traffic is one-way
(server -> browser), SSE reconnects automatically after a phone sleeps or a
tunnel drops, and it survives proxies that mangle WebSocket upgrades. Nothing
here needs the client to talk back — the watchlist edits go over plain REST.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

log = logging.getLogger(__name__)

QUEUE_SIZE = 64


class EventBus:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[str]] = set()
        self._lock = asyncio.Lock()

    async def subscribe(self) -> asyncio.Queue[str]:
        q: asyncio.Queue[str] = asyncio.Queue(maxsize=QUEUE_SIZE)
        async with self._lock:
            self._subscribers.add(q)
        return q

    async def unsubscribe(self, q: asyncio.Queue[str]) -> None:
        async with self._lock:
            self._subscribers.discard(q)

    @property
    def listeners(self) -> int:
        return len(self._subscribers)

    async def publish(self, event: str, data: Any) -> None:
        payload = data if isinstance(data, str) else json.dumps(data, default=str)
        frame = f"event: {event}\ndata: {payload}\n\n"
        dead: list[asyncio.Queue[str]] = []
        for q in list(self._subscribers):
            try:
                q.put_nowait(frame)
            except asyncio.QueueFull:
                # A browser that stopped draining (backgrounded tab, dead
                # tunnel) gets dropped rather than backing up the scanner.
                dead.append(q)
        if dead:
            async with self._lock:
                for q in dead:
                    self._subscribers.discard(q)
            log.info("dropped %d stalled SSE subscriber(s)", len(dead))


bus = EventBus()
