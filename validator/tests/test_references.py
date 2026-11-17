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


def row(schema_name: str, ref_string: str | None = None, version: int = 1) -> ReferenceInput:
    return ReferenceInput(schema_name, ref_string, version)


def get_version_calls(sr: FakeSchemaRegistry) -> list[tuple]:
    return [c for c in sr.calls if c[0] == "get_version"]


def add_chain(sr: FakeSchemaRegistry, length: int) -> None:
    """a.S0 -> a.S1 -> ... -> a.S{length-1}, each referenced at version 1."""
    for i in reversed(range(length)):
        refs = [SrReference(f"a.S{i + 1}", f"a.S{i + 1}", 1)] if i + 1 < length else []
        sr.add(f"a.S{i}", f"S{i}", references=refs)


# normalise_rows (D6)


def test_normalise_avro_default() -> None:
    assert normalise_rows("AVRO", [row("a.B")]) == ([ReferenceInput("a.B", "a.B", 1)], [])


def test_normalise_avro_keeps_given() -> None:
    assert normalise_rows("AVRO", [row("a.B", "a.B")]) == ([ReferenceInput("a.B", "a.B", 1)], [])


def test_normalise_protobuf_derives() -> None:
    rows, errors = normalise_rows(
        "PROTOBUF", [row("com.acme.Address"), row("com.acme.Money", "whatever.proto")]
    )
    assert [r.ref_string for r in rows] == ["com/acme/Address.proto", "com/acme/Money.proto"]
    assert errors == []


def test_normalise_json_requires_ref_string() -> None:
    rows, errors = normalise_rows("JSON", [row("a.B"), row("a.C", "c.json")])
    assert rows == [ReferenceInput("a.C", "c.json", 1)]
    assert errors == ["Reference to 'a.B' needs the text used in the schema (the $ref value)."]


@pytest.mark.parametrize("ref_string", [None, ""])
def test_normalise_avro_agrees_with_stage_a(ref_string: str | None) -> None:
    given = row("a.B", ref_string)
    rows, errors = normalise_rows("AVRO", [given])
    assert errors == []
    assert rows[0].ref_string == _effective_ref_string("AVRO", given) == "a.B"


@pytest.mark.parametrize("ref_string", [None, ""])
def test_normalise_json_missing_ref_string_agrees_with_stage_a(ref_string: str | None) -> None:
    # Stage A does not compare the row (None); stage D gives D6 and leaves it out.
    given = row("a.B", ref_string)
    rows, errors = normalise_rows("JSON", [given])
    assert _effective_ref_string("JSON", given) is None
    assert rows == []
    assert errors == ["Reference to 'a.B' needs the text used in the schema (the $ref value)."]


# resolve (D1, D2, D3, depth)


def test_resolve_happy(sr: FakeSchemaRegistry) -> None:
    sr.add("a.B", B1)
    sr.add("a.B", B2)
    result = resolve(MAIN, "AVRO", [row("a.B", "a.B", 2)], sr)
    assert result == Resolution(
        resolved=[SrReference("a.B", "a.B", 2)], referenced={"a.B": B2}, errors=[],
        provided_by={"a.B": "a.B"}, latest={"a.B": 2},
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
    assert result.errors == [
        "The reference name 'shared' is used for more than one Schema: 'a.C', 'a.E'. "
        "Each Schema needs its own reference name."
    ]


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
    latest = {"com.acme.Address": 3, "com.acme.Money": 1}
    assert pinned_warning(resolved, latest) == (
        "References will be pinned to: com.acme.Address v3, com.acme.Money v1."
    )


# D7: one version per subject, one Schema per reference name

def _money_setup(sr: FakeSchemaRegistry) -> None:
    sr.add("a.Money", "Money1")
    sr.add("a.Money", "Money2")


def test_version_conflict_direct_and_transitive(sr: FakeSchemaRegistry) -> None:
    _money_setup(sr)
    sr.add("a.Line", "Line", references=[SrReference("a.Money", "a.Money", 1)])
    result = resolve(MAIN, "AVRO", [row("a.Line", "a.Line"), row("a.Money", "a.Money", 2)], sr)
    assert result.errors == [
        "'a.Money' is used at more than one version: v1 through 'a.Line', v2 directly. "
        "A schema can use only one version of it. Select the same version for 'a.Money' as the "
        "Schemas it comes through, or update those Schemas to v2: select v2 for 'a.Money' in "
        "their references and save them."
    ]


def test_conflict_between_two_transitive_paths(sr: FakeSchemaRegistry) -> None:
    _money_setup(sr)
    sr.add("a.B", "B", references=[SrReference("a.Money", "a.Money", 1)])
    sr.add("a.C", "C", references=[SrReference("a.Money", "a.Money", 2)])
    result = resolve(MAIN, "AVRO", [row("a.B", "a.B"), row("a.C", "a.C")], sr)
    assert result.errors == [
        "'a.Money' is used at more than one version: v1 through 'a.B', v2 through 'a.C'. "
        "A schema can use only one version of it. Update the Schemas that use an older version "
        "to v2: select v2 for 'a.Money' in their references and save them. Then save this schema."
    ]


def test_diamond_same_version_is_not_a_conflict(sr: FakeSchemaRegistry) -> None:
    sr.add("a.Money", "Money1")
    sr.add("a.B", "B", references=[SrReference("a.Money", "a.Money", 1)])
    sr.add("a.C", "C", references=[SrReference("a.Money", "a.Money", 1)])
    result = resolve(MAIN, "AVRO", [row("a.B", "a.B"), row("a.C", "a.C")], sr)
    assert result.errors == []


def test_same_version_directly_and_through_another_parent(sr: FakeSchemaRegistry) -> None:
    _money_setup(sr)
    sr.add("a.B", "B", references=[SrReference("a.Money", "a.Money", 1)])
    sr.add("a.C", "C", references=[SrReference("a.Money", "a.Money", 2)])
    sr.add("a.D", "D", references=[SrReference("a.Money", "a.Money", 2)])
    result = resolve(
        MAIN, "AVRO",
        [row("a.B", "a.B"), row("a.C", "a.C"), row("a.D", "a.D"), row("a.Money", "a.Money", 2)], sr)
    assert result.errors == [
        "'a.Money' is used at more than one version: v1 through 'a.B', "
        "v2 directly and through 'a.C' and 'a.D'. "
        "A schema can use only one version of it. Select the same version for 'a.Money' as the "
        "Schemas it comes through, or update those Schemas to v2: select v2 for 'a.Money' in "
        "their references and save them."
    ]


def test_name_conflict(sr: FakeSchemaRegistry) -> None:
    sr.add("a.Other", "Other", schema_type="JSON")
    sr.add("a.Address", "Address", schema_type="JSON",
           references=[SrReference("address.json", "a.Other", 1)])
    result = resolve(MAIN, "JSON", [row("a.Address", "address.json")], sr)
    assert result.errors == [
        "The reference name 'address.json' is used for more than one Schema: "
        "'a.Address', 'a.Other'. Each Schema needs its own reference name."
    ]


def test_conflict_recorded_on_a_skipped_revisit(sr: FakeSchemaRegistry) -> None:
    # Top -> Mid -> Money v1 is walked first, so Top -> Money v1 is the skipped revisit
    # that must still be recorded.
    _money_setup(sr)
    sr.add("a.Mid", "Mid", references=[SrReference("a.Money", "a.Money", 1)])
    sr.add("a.Top", "Top", references=[SrReference("a.Mid", "a.Mid", 1),
                                       SrReference("a.Money", "a.Money", 1)])
    sr.add("a.Other", "Other", references=[SrReference("a.Money", "a.Money", 2)])
    result = resolve(MAIN, "AVRO", [row("a.Top", "a.Top"), row("a.Other", "a.Other")], sr)
    assert len(result.errors) == 1
    assert "v1 through 'a.Mid' and 'a.Top', v2 through 'a.Other'" in result.errors[0]


# The version in the row

def test_pinned_older_version_is_used(sr: FakeSchemaRegistry) -> None:
    sr.add("a.Money", "Money1")
    sr.add("a.Money", "Money2")
    result = resolve(MAIN, "AVRO", [row("a.Money", "a.Money", 1)], sr)
    assert result.resolved == [SrReference("a.Money", "a.Money", 1)]
    assert result.referenced == {"a.Money": "Money1"}
    assert result.latest == {"a.Money": 2}
    assert result.errors == []


def test_row_at_latest_fetches_only_latest(sr: FakeSchemaRegistry) -> None:
    sr.add("a.Money", "Money1")
    sr.add("a.Money", "Money2")
    resolve(MAIN, "AVRO", [row("a.Money", "a.Money", 2)], sr)
    assert sr.calls == [("get_latest", "a.Money")]


def test_version_above_latest_is_not_found(sr: FakeSchemaRegistry) -> None:
    for text in ("M1", "M2", "M3"):
        sr.add("a.Money", text)
    result = resolve(MAIN, "AVRO", [row("a.Money", "a.Money", 7)], sr)
    assert result.errors == ["'a.Money' has no version 7 (the latest is 3)."]
    assert result.resolved == []


def test_walk_follows_the_pinned_versions_references(sr: FakeSchemaRegistry) -> None:
    sr.add("a.Money", "Money1")
    sr.add("a.Money", "Money2")
    sr.add("a.Line", "Line1", references=[SrReference("a.Money", "a.Money", 1)])
    sr.add("a.Line", "Line2", references=[SrReference("a.Money", "a.Money", 2)])
    result = resolve(MAIN, "AVRO", [row("a.Line", "a.Line", 1)], sr)
    assert result.errors == []
    assert result.referenced == {"a.Money": "Money1", "a.Line": "Line1"}


def test_protobuf_row_keeps_its_version() -> None:
    rows, _ = normalise_rows("PROTOBUF", [ReferenceInput("a.B", None, 3)])
    assert rows == [ReferenceInput("a.B", "a/B.proto", 3)]


def test_pinned_warning_shows_newer_versions() -> None:
    resolved = [SrReference("a.M", "a.M", 1), SrReference("a.A", "a.A", 3)]
    assert pinned_warning(resolved, {"a.M": 3, "a.A": 3}) == (
        "References will be pinned to: a.M v1 (latest is v3), a.A v3. "
        "To update one, select a newer version for it."
    )


def test_pinned_warning_all_latest_has_no_update_hint() -> None:
    assert pinned_warning([SrReference("a.A", "a.A", 3)], {"a.A": 3}) == (
        "References will be pinned to: a.A v3."
    )


def test_version_conflict_without_own_row_says_update_them(sr: FakeSchemaRegistry) -> None:
    _money_setup(sr)
    sr.add("a.B", "B", references=[SrReference("a.Money", "a.Money", 1)])
    sr.add("a.C", "C", references=[SrReference("a.Money", "a.Money", 2)])
    result = resolve(MAIN, "AVRO", [row("a.B", "a.B"), row("a.C", "a.C")], sr)
    assert result.errors == [
        "'a.Money' is used at more than one version: v1 through 'a.B', v2 through 'a.C'. "
        "A schema can use only one version of it. Update the Schemas that use an older version "
        "to v2: select v2 for 'a.Money' in their references and save them. Then save this schema."
    ]


def test_version_conflict_with_own_row_suggests_selecting(sr: FakeSchemaRegistry) -> None:
    _money_setup(sr)
    sr.add("a.Line", "Line", references=[SrReference("a.Money", "a.Money", 1)])
    result = resolve(MAIN, "AVRO", [row("a.Line", "a.Line", 1), row("a.Money", "a.Money", 2)], sr)
    assert result.errors == [
        "'a.Money' is used at more than one version: v1 through 'a.Line', v2 directly. "
        "A schema can use only one version of it. Select the same version for 'a.Money' as the "
        "Schemas it comes through, or update those Schemas to v2: select v2 for 'a.Money' in "
        "their references and save them."
    ]


def test_version_conflict_with_an_older_own_row_suggests_the_newer_version(sr: FakeSchemaRegistry) -> None:
    _money_setup(sr)
    sr.add("a.Line", "Line", references=[SrReference("a.Money", "a.Money", 2)])
    result = resolve(MAIN, "AVRO", [row("a.Line", "a.Line", 1), row("a.Money", "a.Money", 1)], sr)
    assert result.errors == [
        "'a.Money' is used at more than one version: v1 directly, v2 through 'a.Line'. "
        "A schema can use only one version of it. Select v2 for 'a.Money', the version the "
        "Schemas it comes through use."
    ]


def test_wrong_format_checks_the_pinned_version(sr: FakeSchemaRegistry) -> None:
    # v1 is AVRO, the latest (v2) is JSON: the row pinned to v1 is fine for AVRO, and one pinned
    # to v2 is not.
    sr.add("a.B", B1)
    sr.add("a.B", "{}", "JSON")
    ok = resolve(MAIN, "AVRO", [row("a.B", "a.B", 1)], sr)
    assert ok.errors == [] and ok.resolved == [SrReference("a.B", "a.B", 1)]
    bad = resolve(MAIN, "AVRO", [row("a.B", "a.B", 2)], sr)
    assert bad.errors == ["'a.B' is a JSON schema; a AVRO schema can only reference AVRO schemas."]
    assert bad.resolved == []


def test_wrong_format_of_an_older_pinned_version(sr: FakeSchemaRegistry) -> None:
    sr.add("a.B", "{}", "JSON")
    sr.add("a.B", B1)
    bad = resolve(MAIN, "AVRO", [row("a.B", "a.B", 1)], sr)
    assert bad.errors == ["'a.B' is a JSON schema; a AVRO schema can only reference AVRO schemas."]


def test_resolve_row_without_a_version_fails_closed(sr: FakeSchemaRegistry) -> None:
    sr.add("a.B", B1)
    with pytest.raises(ValueError, match="reference row without a version"):
        resolve(MAIN, "AVRO", [ReferenceInput("a.B", "a.B", None)], sr)
