"""Runs writes one at a time, with a bounded number waiting."""

import asyncio
from collections.abc import Awaitable, Callable

from acl_api.errors import WriteQueueFull


class WriteQueue:
    """Serializes writes so that two read-merge-write CLI runs never overlap (that would lose an
    update). Beyond `max_pending` writes (running + waiting), new ones fail fast with WriteQueueFull
    instead of waiting until their clients time out."""

    def __init__(self, max_pending: int):
        self._max_pending = max_pending
        self._pending = 0
        self._lock = asyncio.Lock()

    async def run(self, write: Callable[[], Awaitable[None]]) -> None:
        if self._pending >= self._max_pending:
            raise WriteQueueFull(f"too many pending writes ({self._pending}), retry later")
        self._pending += 1
        # shield: if the caller goes away, the write still finishes before the next one starts
        await asyncio.shield(asyncio.create_task(self._run_in_turn(write)))

    async def _run_in_turn(self, write: Callable[[], Awaitable[None]]) -> None:
        try:
            async with self._lock:
                await write()
        finally:
            self._pending -= 1
