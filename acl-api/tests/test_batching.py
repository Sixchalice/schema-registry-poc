import random

from acl_api.domain.batching import merge_changes
from acl_api.domain.models import AclChange
from acl_api.domain.operations import resolve_operations


def change(principals, subjects=(), topics=(), operations=("SUBJECT_READ",)) -> AclChange:
    return AclChange(principals=list(principals), subjects=list(subjects), topics=list(topics),
                     operations=list(operations))


def records(c: AclChange) -> set[tuple]:
    """What the CLI writes for a change: (principal, subject | None, operation) triples."""
    ops = resolve_operations(c.operations)
    subject_ops = {op for op in ops if op.is_subject_operation}
    global_ops = ops - subject_ops
    return ({(p, s, op) for p in c.affected_principals() for s in c.affected_subjects() for op in subject_ops}
            | {(p, None, op) for p in c.affected_principals() for op in global_ops})


def test_many_principals_on_one_subject_become_one_call():
    changes = [change([f"p{i}"], subjects=["orders-value"]) for i in range(20)]

    merged = merge_changes(changes)

    assert len(merged) == 1
    assert merged[0].change.principals == [f"p{i}" for i in range(20)]
    assert merged[0].change.subjects == ["orders-value"]
    assert merged[0].members == tuple(range(20))


def test_one_principal_on_many_subjects_becomes_one_call():
    changes = [change(["p"], subjects=[f"s{i}"]) for i in range(5)] + [change(["p"], topics=["orders"])]

    merged = merge_changes(changes)

    assert len(merged) == 1
    assert merged[0].change.subjects == [f"s{i}" for i in range(5)]
    assert merged[0].change.topics == ["orders"]


def test_grid_of_principals_and_subjects_becomes_one_call():
    changes = [change([p], subjects=[s]) for p in ("a", "b") for s in ("s1", "s2")]

    merged = merge_changes(changes)

    assert len(merged) == 1
    assert records(merged[0].change) == set().union(*map(records, changes))


def test_different_operations_are_not_merged():
    merged = merge_changes([change(["a"], ["s"], operations=["SUBJECT_READ"]),
                            change(["b"], ["s"], operations=["SUBJECT_WRITE"])])
    assert len(merged) == 2


def test_operation_names_are_compared_case_insensitively():
    merged = merge_changes([change(["a"], ["s"], operations=["subject_read"]),
                            change(["b"], ["s"], operations=["SUBJECT_READ"])])
    assert len(merged) == 1


def test_wildcards_are_never_merged():
    changes = [change(["*"], ["s"]), change(["a"], ["s"]), change(["a"], ["*"]), change(["b"], topics=["*"])]
    assert [m.members for m in merge_changes(changes)] == [(0,), (1,), (2,), (3,)]


def test_global_only_changes_merge_principals_but_not_with_subject_changes():
    global_a = change(["a"], operations=["GLOBAL_READ"])
    global_b = change(["b"], operations=["GLOBAL_READ"])
    invalid_no_subject = change(["c"], operations=["SUBJECT_READ"])
    with_subject = change(["c"], ["s"], operations=["SUBJECT_READ"])

    merged = merge_changes([global_a, global_b, invalid_no_subject, with_subject])

    assert [m.members for m in merged] == [(0, 1), (2,), (3,)]


def test_merging_never_changes_what_is_written():
    rng = random.Random(7)
    principals, subjects, topics = ["a", "b", "c", "*"], ["s1", "s2", "*"], ["t1", "t2"]
    operations = ["SUBJECT_READ", "SUBJECT_WRITE", "GLOBAL_READ", "subject_read"]
    for _ in range(300):
        changes = [
            change(rng.sample(principals, rng.randint(1, 2)), rng.sample(subjects, rng.randint(0, 2)),
                   rng.sample(topics, rng.randint(0, 1)), rng.sample(operations, rng.randint(1, 2)))
            for _ in range(rng.randint(1, 12))
        ]

        merged = merge_changes(changes)

        assert sorted(m for batch in merged for m in batch.members) == list(range(len(changes)))
        for batch in merged:
            assert records(batch.change) == set().union(*(records(changes[m]) for m in batch.members))
