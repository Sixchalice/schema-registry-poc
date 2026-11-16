"""Shares one expensive read among all concurrent callers, without serving stale results."""

import asyncio
from collections.abc import Awaitable, Callable
from functools import partial


class Coalescer[T]:
    """Runs `fetch` at most once at a time.

    A caller always gets the result of a run that started *after* it called: if a run is in
    progress, the caller queues for the next one, and that next run answers everyone who queued
    meanwhile. So N concurrent callers cause at most two runs, and none sees data older than its call.
    """

    def __init__(self, fetch: Callable[[], Awaitable[T]]):
        self._fetch = fetch
        self._running: asyncio.Task[T] | None = None
        self._queued: asyncio.Future[T] | None = None

    async def get(self) -> T:
        if self._running is None:
            result = self._start()
        elif self._queued is None:
            result = self._queued = asyncio.get_running_loop().create_future()
        else:
            result = self._queued
        # shield: a caller that goes away must not cancel a run that others are waiting for
        return await asyncio.shield(result)

    def _start(self) -> asyncio.Task[T]:
        self._running = asyncio.create_task(self._fetch())
        self._running.add_done_callback(self._on_run_done)
        return self._running

    def _on_run_done(self, run: asyncio.Task[T]) -> None:
        if not run.cancelled():
            run.exception()  # marks the exception retrieved, even if every caller went away
        self._running = None
        if self._queued is not None:
            waiting, self._queued = self._queued, None
            self._start().add_done_callback(partial(_copy_outcome, target=waiting))


def _copy_outcome[T](source: asyncio.Task[T], target: asyncio.Future[T]) -> None:
    if source.cancelled():
        target.cancel()
    elif source.exception() is not None:
        target.set_exception(source.exception())
    else:
        target.set_result(source.result())
