"""The single writer: queues writes and runs them as few, non-overlapping CLI calls."""

import asyncio
import itertools
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum

from acl_api.domain.batching import merge_changes
from acl_api.domain.models import AclChange
from acl_api.errors import CliRejectedArguments, WriteQueueFull

log = logging.getLogger(__name__)

Write = Callable[[AclChange], Awaitable[None]]


class WriteAction(StrEnum):
    ADD = "add"
    REMOVE = "remove"


@dataclass(eq=False)
class _QueuedWrite:
    action: WriteAction
    change: AclChange
    outcome: asyncio.Future[None]


class WriteBatcher:
    """Runs one CLI write at a time, because overlapping read-merge-write runs would lose updates.

    Writes that arrive while a call is running wait in a queue. Each round takes the whole queue
    and merges it into as few calls as possible (domain.batching), so a burst of N similar requests
    costs one or two CLI calls instead of N. `batch_window` is a short pause before each round, so
    that a burst arriving at once lands in the same round.

    If a merged call is rejected, its requests are retried one by one, so only the invalid one fails.
    More than `max_pending` unfinished writes make new ones fail fast with WriteQueueFull.
    """

    def __init__(self, add: Write, remove: Write, max_pending: int, batch_window: float):
        self._writers = {WriteAction.ADD: add, WriteAction.REMOVE: remove}
        self._max_pending = max_pending
        self._batch_window = batch_window
        self._queue: list[_QueuedWrite] = []
        self._pending = 0
        self._worker: asyncio.Task | None = None

    async def submit(self, action: WriteAction, change: AclChange) -> None:
        """Returns once the change is written; raises what the write raised."""
        if self._pending >= self._max_pending:
            raise WriteQueueFull(f"too many pending writes ({self._pending}), retry later")
        write = _QueuedWrite(action, change, asyncio.get_running_loop().create_future())
        self._queue.append(write)
        self._pending += 1
        if self._worker is None:
            self._worker = asyncio.create_task(self._drain())
        # shield: a caller that goes away doesn't cancel the write; it still runs in its turn
        await asyncio.shield(write.outcome)

    async def _drain(self) -> None:
        try:
            while self._queue:
                await asyncio.sleep(self._batch_window)
                this_round, self._queue = self._queue, []
                # Only consecutive writes of the same action may be merged (see domain.batching).
                for _, run in itertools.groupby(this_round, key=lambda write: write.action):
                    await self._write_run(list(run))
        finally:
            self._worker = None

    async def _write_run(self, run: list[_QueuedWrite]) -> None:
        for merged in merge_changes([write.change for write in run]):
            members = [run[position] for position in merged.members]
            await self._write_batch(run[0].action, merged.change, members)

    async def _write_batch(self, action: WriteAction, change: AclChange, members: list[_QueuedWrite]) -> None:
        try:
            await self._writers[action](change)
        except CliRejectedArguments as error:
            if len(members) == 1:
                self._settle(members, error)
                return
            log.info("merged %s of %d requests rejected (%s); retrying them one by one",
                     action, len(members), error)
            for member in members:
                await self._write_batch(action, member.change, [member])
        except Exception as error:  # every waiting caller must get an outcome
            self._settle(members, error)
        else:
            if len(members) > 1:
                log.info("%s: %d requests in one CLI call", action, len(members))
            self._settle(members)

    def _settle(self, members: list[_QueuedWrite], error: Exception | None = None) -> None:
        for member in members:
            self._pending -= 1
            if member.outcome.done():
                continue
            if error is None:
                member.outcome.set_result(None)
            else:
                member.outcome.set_exception(error)
