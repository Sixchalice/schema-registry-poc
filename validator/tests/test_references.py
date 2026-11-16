from __future__ import annotations

import pytest

from schema_validator.models import ReferenceInput
from schema_validator.references import (
    Resolution,
    cross_check,
    normalise_rows,
    pinned_warning,
    resolve,
)
from schema_validator.rules import _effective_ref_string
from schema_validator.sr_client import SchemaRegistryUnavailable, SrReference
from tests.fake_sr import FakeSchemaRegistry

MAIN = "a.Main"
B1 = '{"type": "record", "name": "B", "namespace": "a", "fields": []}'
B2 = '{"type": "record", "name": "B", "namespace": "a", "fields": [], "doc": "v2"}'
TOO_DEEP_S0 = "References of 'a.S0' are nested more than 20 levels deep."


@pytest.fixture
def sr() -> FakeSchemaRegistry:
    return FakeSchemaRegistry()


def row(schema_name: str, ref_string: str | None = None) -> ReferenceInput:
    return ReferenceInput(schema_name, ref_string)


def get_version_calls(sr: FakeSchemaRegistry) -> list[tuple]:
    return [c for c in sr.calls if c[0] == "get_version"]


def add_chain(sr: FakeSchemaRegistry, length: int) -> None:
    """a.S0 -> a.S1 -> ... -> a.S{length-1}, each referenced at version 1."""
    for i in reversed(range(length)):
        refs = [SrReference(f"a.S{i + 1}", f"a.S{i + 1}", 1)] if i + 1 < length else []
        sr.add(f"a.S{i}", f"S{i}", references=refs)


# normalise_rows (D6)


def test_normalise_avro_default() -> None:
    assert normalise_rows("AVRO", [row("a.B")]) == ([ReferenceInput("a.B", "a.B")], [])


def test_normalise_avro_keeps_given() -> None:
    assert normalise_rows("AVRO", [row("a.B", "a.B")]) == ([ReferenceInput("a.B", "a.B")], [])


def test_normalise_protobuf_derives() -> None:
    rows, errors = normalise_rows(
        "PROTOBUF", [row("com.acme.Address"), row("com.acme.Money", "whatever.proto")]
    )
    assert [r.ref_string for r in rows] == ["com/acme/Address.proto", "com/acme/Money.proto"]
    assert errors == []


def test_normalise_json_requires_ref_string() -> None:
    rows, errors = normalise_rows("JSON", [row("a.B"), row("a.C", "c.json")])
    assert rows == [ReferenceInput("a.C", "c.json")]
    assert errors == ["Reference to 'a.B' needs the text used in the schema (the $ref value)."]


@pytest.mark.parametrize("ref_string", [None, ""])
def test_normalise_avro_agrees_with_stage_a(ref_string: str | None) -> None:
    given = row("a.B", ref_string)
    rows, errors = normalise_rows("AVRO", [given])
    assert errors == []
    assert rows[0].ref_string == _effective_ref_string("AVRO", given) == "a.B"


def test_normalise_json_empty_ref_string_agrees_with_stage_a() -> None:
    given = row("a.B", "")
    rows, errors = normalise_rows("JSON", [given])
    assert errors == []
    assert rows[0].ref_string == _effective_ref_string("JSON", given) == ""


# resolve (D1, D2, D3, depth)


def test_resolve_happy(sr: FakeSchemaRegistry) -> None:
    sr.add("a.B", B1)
    sr.add("a.B", B2)
    result = resolve(MAIN, "AVRO", [row("a.B", "a.B")], sr)
    assert result == Resolution(
        resolved=[SrReference("a.B", "a.B", 2)], referenced={"a.B": B2}, errors=[]
    )


def test_not_registered(sr: FakeSchemaRegistry) -> None:
    result = resolve(MAIN, "AVRO", [row("a.B", "a.B")], sr)
    assert result.errors == ["Referenced schema 'a.B' is not registered in Schema Registry yet."]
    assert result.resolved == []


def test_wrong_format(sr: FakeSchemaRegistry) -> None:
    sr.add("a.B", "{}", "JSON")
    result = resolve(MAIN, "AVRO", [row("a.B", "a.B")], sr)
    assert result.errors == [
        "'a.B' is a JSON schema; a AVRO schema can only reference AVRO schemas."
    ]
    assert result.resolved == []


def test_wrong_format_row_is_not_walked(sr: FakeSchemaRegistry) -> None:
    sr.add("a.B", "{}", "JSON", [SrReference("c.json", "a.C", 1)])
    result = resolve(MAIN, "AVRO", [row("a.B", "a.B")], sr)
    assert get_version_calls(sr) == []
    assert result.referenced == {}


def test_transitive_order(sr: FakeSchemaRegistry) -> None:
    sr.add("a.C", "C")
    sr.add("a.B", "B", references=[SrReference("c.ref", "a.C", 1)])
    result = resolve(MAIN, "AVRO", [row("a.B", "a.B")], sr)
    assert list(result.referenced) == ["c.ref", "a.B"]
    assert result.referenced == {"c.ref": "C", "a.B": "B"}
    assert result.errors == []


def test_direct_cycle(sr: FakeSchemaRegistry) -> None:
    sr.add("a.B", "B", references=[SrReference("a.Main", MAIN, 1)])
    result = resolve(MAIN, "AVRO", [row("a.B", "a.B")], sr)
    assert result.errors == ["Reference cycle: 'a.B' already depends on 'a.Main'."]


def test_transitive_cycle(sr: FakeSchemaRegistry) -> None:
    sr.add("a.C", "C", references=[SrReference("a.Main", MAIN, 1)])
    sr.add("a.B", "B", references=[SrReference("a.C", "a.C", 1)])
    result = resolve(MAIN, "AVRO", [row("a.B", "a.B")], sr)
    assert result.errors == ["Reference cycle: 'a.B' already depends on 'a.Main'."]


def test_cycle_is_reported_once_per_row(sr: FakeSchemaRegistry) -> None:
    sr.add("a.C", "C", references=[SrReference("a.Main", MAIN, 1)])
    sr.add("a.B", "B", references=[
        SrReference("a.C", "a.C", 1), SrReference("a.Main", MAIN, 1),
    ])
    result = resolve(MAIN, "AVRO", [row("a.B", "a.B")], sr)
    assert result.errors == ["Reference cycle: 'a.B' already depends on 'a.Main'."]


def test_row_for_name_itself_is_a_cycle(sr: FakeSchemaRegistry) -> None:
    sr.add(MAIN, "M")
    result = resolve(MAIN, "AVRO", [row(MAIN, MAIN)], sr)
    assert result.errors == ["Reference cycle: 'a.Main' already depends on 'a.Main'."]


def test_shared_dependency_reaching_name_is_a_cycle_for_every_row(
    sr: FakeSchemaRegistry,
) -> None:
    sr.add("a.D", "D", references=[SrReference("a.Main", MAIN, 1)])
    sr.add("a.B", "B", references=[SrReference("a.D", "a.D", 1)])
    sr.add("a.C", "C", references=[SrReference("a.D", "a.D", 1)])
    result = resolve(MAIN, "AVRO", [row("a.B", "a.B"), row("a.C", "a.C")], sr)
    assert result.errors == [
        "Reference cycle: 'a.B' already depends on 'a.Main'.",
        "Reference cycle: 'a.C' already depends on 'a.Main'.",
    ]
    assert get_version_calls(sr) == [("get_version", "a.D", 1)]


def test_diamond_is_not_a_cycle(sr: FakeSchemaRegistry) -> None:
    sr.add("a.D", "D")
    sr.add("a.B", "B", references=[SrReference("a.D", "a.D", 1)])
    sr.add("a.C", "C", references=[SrReference("a.D", "a.D", 1)])
    result = resolve(MAIN, "AVRO", [row("a.B", "a.B"), row("a.C", "a.C")], sr)
    assert result.errors == []
    assert sr.calls.count(("get_version", "a.D", 1)) == 1
    assert list(result.referenced.values()).count("D") == 1
    assert list(result.referenced) == ["a.D", "a.B", "a.C"]


def test_diamond_within_one_row_keeps_dependencies_first(sr: FakeSchemaRegistry) -> None:
    sr.add("a.E", "E")
    sr.add("a.C", "C", references=[SrReference("a.E", "a.E", 1)])
    sr.add("a.D", "D", references=[SrReference("a.E", "a.E", 1)])
    sr.add("a.B", "B", references=[SrReference("a.C", "a.C", 1), SrReference("a.D", "a.D", 1)])
    result = resolve(MAIN, "AVRO", [row("a.B", "a.B")], sr)
    assert result.errors == []
    assert list(result.referenced) == ["a.E", "a.C", "a.D", "a.B"]
    assert sr.calls.count(("get_version", "a.E", 1)) == 1


def test_too_deep(sr: FakeSchemaRegistry) -> None:
    add_chain(sr, 22)
    result = resolve(MAIN, "AVRO", [row("a.S0", "a.S0")], sr)
    assert result.errors == [TOO_DEEP_S0]


def test_twenty_levels_is_allowed(sr: FakeSchemaRegistry) -> None:
    add_chain(sr, 21)  # a.S20 is 20 levels below a.S0
    result = resolve(MAIN, "AVRO", [row("a.S0", "a.S0")], sr)
    assert result.errors == []
    assert list(result.referenced) == [f"a.S{i}" for i in reversed(range(21))]


def test_depth_counts_the_longest_path(sr: FakeSchemaRegistry) -> None:
    # a.X is reached first at level 1, then again at level 20 through a.S1..a.S19;
    # its dependency a.Y then sits at level 21.
    sr.add("a.Y", "Y")
    sr.add("a.X", "X", references=[SrReference("a.Y", "a.Y", 1)])
    for i in reversed(range(1, 20)):
        nxt = SrReference(f"a.S{i + 1}", f"a.S{i + 1}", 1) if i < 19 else SrReference("a.X", "a.X", 1)
        sr.add(f"a.S{i}", f"S{i}", references=[nxt])
    sr.add("a.S0", "S0", references=[SrReference("a.X", "a.X", 1), SrReference("a.S1", "a.S1", 1)])
    result = resolve(MAIN, "AVRO", [row("a.S0", "a.S0")], sr)
    assert result.errors == [TOO_DEEP_S0]
    assert sr.calls.count(("get_version", "a.X", 1)) == 1


def test_cycle_not_involving_name_terminates(sr: FakeSchemaRegistry) -> None:
    # Cannot happen in a real registry; the registry's data is not trusted.
    sr.add("a.S0", "S0", references=[SrReference("a.B", "a.B", 1)])
    sr.add("a.B", "B", references=[SrReference("a.S0", "a.S0", 1)])
    result = resolve(MAIN, "AVRO", [row("a.S0", "a.S0")], sr)
    assert result.errors == [TOO_DEEP_S0]
    assert get_version_calls(sr) == [("get_version", "a.B", 1)]


def test_schema_referencing_itself_terminates(sr: FakeSchemaRegistry) -> None:
    sr.add("a.S0", "S0", references=[SrReference("a.S0", "a.S0", 1)])
    result = resolve(MAIN, "AVRO", [row("a.S0", "a.S0")], sr)
    assert result.errors == [TOO_DEEP_S0]
    assert get_version_calls(sr) == []


def test_nested_not_registered(sr: FakeSchemaRegistry) -> None:
    sr.add("a.B", "B", references=[SrReference("a.C", "a.C", 5)])
    result = resolve(MAIN, "AVRO", [row("a.B", "a.B")], sr)
    assert result.errors == ["Referenced schema 'a.C' is not registered in Schema Registry yet."]
    assert result.resolved == [SrReference("a.B", "a.B", 1)]


def test_same_missing_dependency_is_reported_once(sr: FakeSchemaRegistry) -> None:
    sr.add("a.B", "B", references=[SrReference("a.C", "a.C", 1)])
    sr.add("a.D", "D", references=[SrReference("a.C", "a.C", 1)])
    result = resolve(MAIN, "AVRO", [row("a.B", "a.B"), row("a.D", "a.D")], sr)
    assert result.errors == ["Referenced schema 'a.C' is not registered in Schema Registry yet."]


def test_same_reference_name_for_different_schemas_keeps_the_first(
    sr: FakeSchemaRegistry,
) -> None:
    sr.add("a.C", "C")
    sr.add("a.E", "E")
    sr.add("a.B", "B", references=[SrReference("shared", "a.C", 1)])
    sr.add("a.D", "D", references=[SrReference("shared", "a.E", 1)])
    result = resolve(MAIN, "AVRO", [row("a.B", "a.B"), row("a.D", "a.D")], sr)
    assert result.referenced == {"shared": "C", "a.B": "B", "a.D": "D"}
    assert result.errors == []


def test_sr_failure_propagates(sr: FakeSchemaRegistry) -> None:
    sr.add("a.B", "B")
    sr.fail_with = SchemaRegistryUnavailable("x")
    with pytest.raises(SchemaRegistryUnavailable):
        resolve(MAIN, "AVRO", [row("a.B", "a.B")], sr)


# cross_check (D4, D5)


def test_cross_check_ok() -> None:
    assert cross_check("AVRO", [row("a.B", "a.B")], {"a.B"}) == []


def test_missing_row() -> None:
    assert cross_check("AVRO", [], {"a.B"}) == [
        "Your schema uses 'a.B' but no reference is selected for it."
    ]


def test_missing_rows_are_sorted() -> None:
    assert cross_check("AVRO", [], {"a.C", "a.B"}) == [
        "Your schema uses 'a.B' but no reference is selected for it.",
        "Your schema uses 'a.C' but no reference is selected for it.",
    ]


def test_unused_row() -> None:
    assert cross_check("AVRO", [row("a.B", "a.B")], set()) == [
        "'a.B' is selected as a reference but is not used in the schema."
    ]


def test_unused_row_protobuf() -> None:
    rows = [row("com.acme.Address", "com/acme/Address.proto")]
    assert cross_check("PROTOBUF", rows, {"acme/address.proto"}) == [
        "Your schema uses 'acme/address.proto' but no reference is selected for it.",
        "'com.acme.Address' is selected as a reference but the schema does not import it. "
        "Write the import as: import \"com/acme/Address.proto\";",
    ]


def test_json_fragment_already_stripped() -> None:
    assert cross_check("JSON", [row("a.B", "address.json")], {"address.json"}) == []


# pinned_warning


def test_pinned_warning() -> None:
    resolved = [SrReference("x", "com.acme.Address", 3), SrReference("y", "com.acme.Money", 1)]
    assert pinned_warning(resolved) == (
        "References will be pinned to: com.acme.Address v3, com.acme.Money v1. "
        "They stay on these versions until this schema is saved again."
    )
