import asyncio

from acl_api.domain.models import AclChange
from acl_api.errors import CliFailed
from acl_api.services.acl_service import AclService
from acl_api.services.coalescer import Coalescer
from tests.conftest import FakeBackend, run


class CountingFetch:
    def __init__(self, delay: float = 0.05, error: Exception | None = None):
        self.calls = 0
        self.delay = delay
        self.error = error

    async def __call__(self) -> int:
        self.calls += 1
        call = self.calls
        await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return call


def test_concurrent_reads_share_at_most_two_runs():
    fetch = CountingFetch()

    async def scenario():
        coalescer = Coalescer(fetch)
        return await asyncio.gather(*(coalescer.get() for _ in range(50)))

    results = run(scenario())

    # The first caller starts run 1; the other 49 called while it was running, so they share run 2.
    assert fetch.calls == 2
    assert results == [1] + [2] * 49


def test_caller_arriving_during_a_run_gets_the_next_run():
    fetch = CountingFetch()

    async def scenario():
        coalescer = Coalescer(fetch)
        first = asyncio.create_task(coalescer.get())
        await asyncio.sleep(0.01)  # the first run is now in progress
        late = await asyncio.gather(*(coalescer.get() for _ in range(10)))
        return await first, late

    first, late = run(scenario())

    assert first == 1
    assert set(late) == {2}  # one shared follow-up run, started after they called
    assert fetch.calls == 2


def test_errors_reach_every_waiting_caller():
    fetch = CountingFetch(error=CliFailed("kafka down"))

    async def scenario():
        coalescer = Coalescer(fetch)
        return await asyncio.gather(*(coalescer.get() for _ in range(5)), return_exceptions=True)

    assert all(isinstance(result, CliFailed) for result in run(scenario()))


def test_add_returns_touched_records_read_after_the_write():
    backend = FakeBackend()
    service = AclService(backend, max_pending_writes=5)
    change = AclChange(principals=["p"], topics=["orders"], operations=["subject_read", "GLOBAL_READ"])

    listing = run(service.add(change, actor="tester"))

    assert [(a.subject, a.operations) for a in listing.subject_acls] == [
        ("orders-key", ["SUBJECT_READ"]), ("orders-value", ["SUBJECT_READ"])]
    assert [(a.principal, a.operations) for a in listing.global_acls] == [("p", ["GLOBAL_READ"])]


def test_remove_reports_emptied_records():
    backend = FakeBackend()
    service = AclService(backend, max_pending_writes=5)
    change = AclChange(principals=["p"], subjects=["s"], operations=["SUBJECT_READ"])
    run(service.add(change, actor="tester"))

    listing = run(service.remove(change, actor="tester"))

    assert listing.subject_acls[0].operations == []
    assert run(service.list_acls()).subject_acls == []
