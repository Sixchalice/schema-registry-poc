"""The API's use cases: list, add and remove ACLs."""

import logging
from collections.abc import Awaitable, Callable
from typing import Protocol

from acl_api.domain.models import AclChange, AclListing, AclSnapshot
from acl_api.services.coalescer import Coalescer
from acl_api.services.write_queue import WriteQueue

log = logging.getLogger(__name__)


class AclBackend(Protocol):
    """Where ACLs are read and written. Implemented by cli.sr_acl_cli.SrAclCli."""

    async def list_acls(self) -> AclSnapshot: ...

    async def add(self, change: AclChange) -> None: ...

    async def remove(self, change: AclChange) -> None: ...


class AclService:
    """Reads are coalesced (one backend read at a time, shared by all concurrent callers) and writes
    are queued (one at a time), so at most one read and one write reach the backend at once."""

    def __init__(self, backend: AclBackend, max_pending_writes: int):
        self._backend = backend
        self._snapshots = Coalescer(backend.list_acls)
        self._writes = WriteQueue(max_pending_writes)

    async def list_acls(self, principal: str | None = None, subject: str | None = None) -> AclListing:
        snapshot = await self._snapshots.get()
        return snapshot.listing(principal, subject)

    async def check_backend(self) -> None:
        await self._snapshots.get()

    async def add(self, change: AclChange, actor: str) -> AclListing:
        return await self._apply("add", self._backend.add, change, actor)

    async def remove(self, change: AclChange, actor: str) -> AclListing:
        return await self._apply("remove", self._backend.remove, change, actor)

    async def _apply(self, action: str, write: Callable[[AclChange], Awaitable[None]],
                     change: AclChange, actor: str) -> AclListing:
        await self._writes.run(lambda: write(change))
        log.info("%s by %s: %s", action, actor, change.model_dump_json())
        snapshot = await self._snapshots.get()  # a read that starts after the write finished
        return snapshot.records_touched_by(change)
