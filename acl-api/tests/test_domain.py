import pytest
from pydantic import ValidationError

from acl_api.domain.models import AclChange, AclSnapshot
from acl_api.domain.operations import Operation, resolve_operations


@pytest.mark.parametrize("names, expected", [
    (["subject_read"], {Operation.SUBJECT_READ}),
    (["GLOBAL_SUBJECTS_READ"], {Operation.GLOBAL_READ}),
    (["SUBJECT_READ", "*"], set(Operation)),
    (["NOPE"], set()),
])
def test_resolve_operations(names, expected):
    assert resolve_operations(names) == expected


def test_topics_expand_and_wildcards_collapse():
    change = AclChange(principals=["a", "a"], subjects=["s"], topics=["orders"], operations=["SUBJECT_READ"])
    assert change.affected_principals() == ["a"]
    assert change.affected_subjects() == ["s", "orders-key", "orders-value"]

    wildcard = AclChange(principals=["a", "*"], topics=["*"], operations=["SUBJECT_READ"])
    assert wildcard.affected_principals() == ["*"]
    assert wildcard.affected_subjects() == ["*"]


@pytest.mark.parametrize("body", [
    {"principals": [], "operations": ["GLOBAL_READ"]},
    {"principals": ["p"], "operations": []},
    {"principals": [" "], "operations": ["GLOBAL_READ"]},
    {"principals": ["p\nq"], "operations": ["GLOBAL_READ"]},
    {"principals": ["p"], "operations": ["GLOBAL_READ"], "extra": 1},
])
def test_invalid_changes_are_rejected(body):
    with pytest.raises(ValidationError):
        AclChange.model_validate(body)


SNAPSHOT = AclSnapshot(
    subject_operations={"b": {"s2": ["SUBJECT_READ"], "gone": []}, "a": {"s1": ["SUBJECT_WRITE"]}},
    global_operations={"a": ["GLOBAL_READ"], "b": []},
)


def test_listing_is_sorted_and_skips_empty_records():
    listing = SNAPSHOT.listing()

    assert [(acl.principal, acl.subject) for acl in listing.subject_acls] == [("a", "s1"), ("b", "s2")]
    assert [acl.principal for acl in listing.global_acls] == ["a"]


def test_listing_filters():
    assert [acl.subject for acl in SNAPSHOT.listing(principal="b").subject_acls] == ["s2"]
    by_subject = SNAPSHOT.listing(subject="s1")
    assert [acl.principal for acl in by_subject.subject_acls] == ["a"]
    assert by_subject.global_acls == []


def test_listing_serializes_in_camel_case():
    assert set(SNAPSHOT.listing().model_dump(by_alias=True)) == {"subjectAcls", "globalAcls"}
