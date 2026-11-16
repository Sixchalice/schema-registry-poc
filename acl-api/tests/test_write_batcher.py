import asyncio

import pytest

from acl_api.domain.models import AclChange
from acl_api.errors import CliFailed, CliRejectedArguments, WriteQueueFull
from acl_api.services.write_batcher import WriteAction, WriteBatcher
from tests.conftest import FakeBackend, run

ADD, REMOVE = WriteAction.ADD, WriteAction.REMOVE


def grant(principal: str, subject: str = "orders-value") -> AclChange:
    return AclChange(principals=[principal], subjects=[subject], operations=["SUBJECT_READ"])


def batcher(backend: FakeBackend, max_pending: int = 100, window: float = 0.01) -> WriteBatcher:
    return WriteBatcher(backend.add, backend.remove, max_pending, window)


def submit_all(writer: WriteBatcher, writes: list[tuple[WriteAction, AclChange]]):
    async def scenario():
        return await asyncio.gather(*(writer.submit(action, c) for action, c in writes), return_exceptions=True)
    return run(scenario())


def test_burst_of_principals_on_one_subject_is_one_cli_call():
    backend = FakeBackend()

    results = submit_all(batcher(backend), [(ADD, grant(f"p{i}")) for i in range(20)])

    assert results == [None] * 20
    assert len(backend.writes) == 1
    assert backend.writes[0][1].principals == [f"p{i}" for i in range(20)]
    assert set(backend.subject_operations) == {f"p{i}" for i in range(20)}


def test_writes_arriving_during_a_call_are_merged_into_the_next_one():
    backend = FakeBackend(write_delay=0.05)

    async def scenario():
        writer = batcher(backend, window=0)
        first = asyncio.create_task(writer.submit(ADD, grant("first")))
        await asyncio.sleep(0.01)  # the first call is running now
        await asyncio.gather(*(writer.submit(ADD, grant(f"p{i}")) for i in range(30)), first)

    run(scenario())

    assert [len(c.principals) for _, c in backend.writes] == [1, 30]


def test_backend_writes_never_overlap():
    backend = FakeBackend(write_delay=0.01)
    unmergeable = [(ADD, grant(f"p{i}", f"s{i}")) for i in range(10)]  # nothing in common

    submit_all(batcher(backend), unmergeable)

    assert len(backend.writes) == 10
    assert backend.max_active_writes == 1


def test_add_and_remove_keep_their_order():
    backend = FakeBackend()

    submit_all(batcher(backend), [(ADD, grant("p")), (REMOVE, grant("p")), (ADD, grant("p")), (ADD, grant("q"))])

    assert [action for action, _ in backend.writes] == ["add", "remove", "add"]
    assert backend.subject_operations["p"]["orders-value"] == ["SUBJECT_READ"]


def test_a_rejected_request_does_not_fail_the_others_in_its_batch():
    backend = FakeBackend()
    backend.reject = lambda change: "bad" in change.principals

    results = submit_all(batcher(backend), [(ADD, grant("good1")), (ADD, grant("bad")), (ADD, grant("good2"))])

    assert results[0] is None and results[2] is None
    assert isinstance(results[1], CliRejectedArguments)
    assert set(backend.subject_operations) == {"good1", "good2"}


def test_a_backend_failure_reaches_every_request_in_the_batch():
    backend = FakeBackend()
    backend.error = CliFailed("kafka down")

    results = submit_all(batcher(backend), [(ADD, grant(f"p{i}")) for i in range(3)])

    assert all(isinstance(result, CliFailed) for result in results)


def test_full_queue_rejects_new_writes_until_there_is_room():
    backend = FakeBackend(write_delay=0.05)

    async def scenario():
        writer = batcher(backend, max_pending=2, window=0)
        first = [asyncio.create_task(writer.submit(ADD, grant(f"p{i}"))) for i in range(2)]
        await asyncio.sleep(0)
        with pytest.raises(WriteQueueFull):
            await writer.submit(ADD, grant("too-many"))
        await asyncio.gather(*first)
        await writer.submit(ADD, grant("room-again"))

    run(scenario())
