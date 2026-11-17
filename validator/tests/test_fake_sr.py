import pytest

from schema_validator.models import DEFAULT_BUILTIN_PROTO_IMPORTS, DEFAULT_JSON_DRAFTS
from schema_validator.sr_client import (
    CompatibilityVerdict,
    SchemaRegistryUnavailable,
    SrReference,
)
from tests.fake_sr import FakeSchemaRegistry


def test_add_returns_increasing_versions():
    sr = FakeSchemaRegistry()
    assert sr.add("s", "a") == 1
    assert sr.add("s", "b") == 2


def test_get_latest_returns_last_version_with_type_and_references():
    sr = FakeSchemaRegistry()
    ref = SrReference("x.proto", "x", 1)
    sr.add("s", "a")
    sr.add("s", "b", "PROTOBUF", [ref])
    latest = sr.get_latest("s")
    assert (latest.version, latest.schema, latest.schema_type) == (2, "b", "PROTOBUF")
    assert latest.references == (ref,)


def test_missing_returns_none():
    sr = FakeSchemaRegistry()
    sr.add("s", "a")
    assert sr.get_latest("missing") is None
    assert sr.get_version("s", 9) is None
    assert sr.get_version("s", 1).schema == "a"


def test_compatibility_level_defaults_to_backward():
    sr = FakeSchemaRegistry()
    assert sr.get_compatibility_level("s") == "BACKWARD"
    sr.levels["s"] = "FULL"
    assert sr.get_compatibility_level("s") == "FULL"


def test_check_compatibility_returns_verdict_and_records_call():
    sr = FakeSchemaRegistry()
    assert sr.check_compatibility("s", "t", "AVRO", []) == CompatibilityVerdict(True)
    sr.verdict = CompatibilityVerdict(False, ("bad",))
    ref = SrReference("n", "s2", 3)
    assert sr.check_compatibility("s", "t", "AVRO", [ref]).messages == ("bad",)
    assert sr.calls[-1] == ("check_compatibility", "s", "t", "AVRO", (ref,))


def test_fail_with_raises_from_every_method():
    sr = FakeSchemaRegistry()
    sr.fail_with = SchemaRegistryUnavailable("x")
    for call in (
        lambda: sr.get_latest("s"),
        lambda: sr.get_version("s", 1),
        lambda: sr.get_compatibility_level("s"),
        lambda: sr.check_compatibility("s", "t", "AVRO", []),
    ):
        with pytest.raises(SchemaRegistryUnavailable):
            call()


def test_default_constants_sizes():
    assert len(DEFAULT_BUILTIN_PROTO_IMPORTS) == 29
    assert len(DEFAULT_JSON_DRAFTS) == 6
