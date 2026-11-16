"""The API's use cases: list, add and remove ACLs."""

import logging
from typing import Protocol

from acl_api.domain.models import AclChange, AclListing, AclSnapshot
from acl_api.services.coalescer import Coalescer
from acl_api.services.write_batcher import WriteAction, WriteBatcher

log = logging.getLogger(__name__)


class AclBackend(Protocol):
    """Where ACLs are read and written. Implemented by cli.sr_acl_cli.SrAclCli."""

    async def list_acls(self) -> AclSnapshot: ...

    async def add(self, change: AclChange) -> None: ...

    async def remove(self, change: AclChange) -> None: ...


class AclService:
    """Reads are coalesced (one backend read at a time, shared by all concurrent callers). Writes go
    through one WriteBatcher (one backend write at a time, concurrent requests merged). So at most
    one read and one write reach the backend at once, whatever the load."""

    def __init__(self, backend: AclBackend, max_pending_writes: int, write_batch_window: float = 0.0):
        self._snapshots = Coalescer(backend.list_acls)
        self._writes = WriteBatcher(backend.add, backend.remove, max_pending_writes, write_batch_window)

    async def list_acls(self, principal: str | None = None, subject: str | None = None) -> AclListing:
        snapshot = await self._snapshots.get()
        return snapshot.listing(principal, subject)

    async def check_backend(self) -> None:
        await self._snapshots.get()

    async def add(self, change: AclChange, actor: str) -> AclListing:
        return await self._apply(WriteAction.ADD, change, actor)

    async def remove(self, change: AclChange, actor: str) -> AclListing:
        return await self._apply(WriteAction.REMOVE, change, actor)

    async def _apply(self, action: WriteAction, change: AclChange, actor: str) -> AclListing:
        await self._writes.submit(action, change)
        log.info("%s by %s: %s", action, actor, change.model_dump_json())
        snapshot = await self._snapshots.get()  # a read that starts after the write finished
        return snapshot.records_touched_by(change)
