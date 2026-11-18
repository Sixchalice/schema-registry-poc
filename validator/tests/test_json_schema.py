from __future__ import annotations

import json
import time
from typing import Any

import pytest

from schema_validator import ValidationRequest, ValidationResult, messages, validate
from schema_validator.formats._depth import json_depth
from schema_validator.formats.json_schema import analyse
from schema_validator.models import DEFAULT_JSON_DRAFTS, FormatAnalysis, ValidatorConfig
from tests.fake_sr import FakeSchemaRegistry

cfg = ValidatorConfig()
D7 = "http://json-schema.org/draft-07/schema#"

TITLE_MISSING = 'JSON schema needs a "title"; it becomes the schema name.'
TITLE_FORMAT = (
    'The "title" must be a dotted name made of letters, digits and underscores, '
    'for example "com.team.domain.Order".'
)
TOP_LEVEL = 'The top level of a JSON schema must have "type": "object".'


def run(d: Any) -> FormatAnalysis:
    return analyse(json.dumps(d), {}, cfg)


def schema(**extra: Any) -> dict[str, Any]:
    return {"$schema": D7, "title": "com.acme.Order", "type": "object", **extra}


def test_valid() -> None:
    result = run(
        {"$schema": D7, "title": "com.acme.Order", "type": "object", "properties": {"id": {"type": "string"}}}
    )
    assert result == FormatAnalysis(expected_name="com.acme.Order", external_refs=set(), errors=[])


def test_not_json() -> None:
    result = analyse("{not json", {}, cfg)
    assert len(result.errors) == 1
    assert result.errors[0].startswith("Schema is not valid JSON: ")
    assert result.external_refs is None
    assert result.expected_name is None


def test_top_level_array() -> None:
    result = run([1])
    assert result.errors == [TOP_LEVEL]
    assert result.external_refs is None
    assert result.expected_name is None


def test_missing_title() -> None:
    result = run({"$schema": D7, "type": "object"})
    assert result.errors == [TITLE_MISSING]
    assert result.expected_name is None


@pytest.mark.parametrize(
    "title",
    [
        "Order",
        "com.acme.My Order",
        "com/acme.Order",
        "com.acme:Order",
        "com.*.Order",
        ":.ctx:com.acme.Order",
        "com..Order",
        "1com.acme.Order",
    ],
)
def test_bad_titles(title: str) -> None:
    result = run({"$schema": D7, "title": title, "type": "object"})
    assert result.errors == [TITLE_FORMAT]
    assert result.expected_name is None


def test_type_not_object() -> None:
    result = run(schema(type="array"))
    assert result.errors == [TOP_LEVEL]


def test_unsupported_draft() -> None:
    result = run(schema(**{"$schema": "http://json-schema.org/draft-03/schema#"}))
    assert result.errors == ["Unsupported JSON Schema draft 'http://json-schema.org/draft-03/schema#'."]


@pytest.mark.parametrize(
    "draft", ["http://json-schema.org/draft-07/schema#", "http://json-schema.org/draft-07/schema"]
)
def test_draft_with_and_without_hash(draft: str) -> None:
    assert run(schema(**{"$schema": draft})).errors == []


@pytest.mark.parametrize("draft", DEFAULT_JSON_DRAFTS)
def test_all_allowed_drafts(draft: str) -> None:
    assert len(DEFAULT_JSON_DRAFTS) == 5
    assert run(schema(**{"$schema": draft})).errors == []


def test_invalid_schema() -> None:
    result = run(schema(properties="nope"))
    assert len(result.errors) == 1
    assert result.errors[0].startswith("Invalid JSON schema: ")
    assert "'nope' is not of type 'object'" in result.errors[0]


def test_missing_schema_keyword_is_an_error() -> None:
    # Audit issue 3: the Python serializer reads it as 2020-12, Confluent as draft-07.
    d: dict[str, Any] = {
        "title": "a.B",
        "type": "object",
        "properties": {"t": {"type": "array", "items": [{"type": "string"}]}},
    }
    assert messages.JSON_SCHEMA_MISSING in run(d).errors
    assert run(d).errors == [messages.JSON_SCHEMA_MISSING]  # the draft-07 meta-schema check still ran

    d["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    errors = run(d).errors
    assert len(errors) == 1
    assert errors[0].startswith("Invalid JSON schema: ")


def test_internal_refs_ignored() -> None:
    result = run(
        schema(
            definitions={"Z": {"type": "string"}},
            **{"$defs": {"Z": {"type": "string"}}},
            properties={
                "a": {"$ref": "#/definitions/Z"},
                "b": {"$ref": "#/$defs/Z"},
                "c": {"$ref": "#"},
            },
        )
    )
    assert result.external_refs == set()
    assert result.errors == []


def test_external_refs() -> None:
    result = run(
        schema(
            properties={
                "a": {"$ref": "address.json"},
                "b": {"$ref": "address.json#/definitions/Zip"},
                "c": {"$ref": "https://x.io/money.json#"},
            }
        )
    )
    assert result.external_refs == {"address.json", "https://x.io/money.json"}
    assert result.errors == []


def test_ref_in_nested_positions() -> None:
    result = run(
        schema(
            properties={
                "list": {"type": "array", "items": {"$ref": "item.json"}},
                "combo": {"allOf": [{"$ref": "all.json"}]},
            },
            definitions={"X": {"type": "object", "properties": {"y": {"$ref": "deep.json"}}}},
        )
    )
    assert result.external_refs == {"item.json", "all.json", "deep.json"}
    assert result.errors == []


def test_property_named_ref() -> None:
    result = run(schema(properties={"$ref": {"type": "string"}}))
    assert result.external_refs == set()
    assert result.errors == []


def test_ref_inside_enum_or_default_ignored() -> None:
    result = run(
        schema(
            properties={
                "a": {"type": "object", "default": {"$ref": "x.json"}},
                "b": {"enum": [{"$ref": "y.json"}]},
            }
        )
    )
    assert result.external_refs == set()
    assert result.errors == []


def test_ref_under_dependencies_is_collected() -> None:
    # The keys of "dependencies" are property names, so "default" here is not the keyword.
    result = run(schema(dependencies={"default": {"$ref": "x.json"}}))
    assert result.external_refs == {"x.json"}
    assert result.errors == []


def test_non_string_ref_is_not_a_reference() -> None:
    result = run(schema(properties={"a": {"$ref": 5}}))
    assert result.external_refs == set()
    assert len(result.errors) == 1
    assert result.errors[0].startswith("Invalid JSON schema: ")


def test_non_string_schema_keyword() -> None:
    result = run(schema(**{"$schema": 7}))
    assert result.errors == ["Unsupported JSON Schema draft '7'."]


def test_non_string_title() -> None:
    # The meta-schema check also rejects the title, so there are two messages.
    result = run({"$schema": D7, "title": 5, "type": "object"})
    assert result.errors == [TITLE_MISSING, "Invalid JSON schema: 5 is not of type 'string' (at /title)"]
    assert result.expected_name is None


def test_json_depth() -> None:
    assert json_depth("x", 5) == 0
    assert json_depth({"a": [1]}, 5) == 2
    assert json_depth({"type": "string"}, 5) == 1
    ten: Any = 1
    for _ in range(10):
        ten = [ten]
    assert json_depth(ten, 3) > 3
    assert json_depth(ten, 10) == 10


def nested(depth: int) -> str:
    return "[" * depth + "]" * depth


TOO_DEEP = ["Schema is nested more than 64 levels deep, counting every JSON object and array."]


def test_depth_at_limit_passes_the_depth_check() -> None:
    result = analyse(nested(64), {}, cfg)
    assert messages.SCHEMA_TOO_DEEP.format(max=64) not in result.errors


def test_depth_above_limit_is_an_error() -> None:
    result = analyse(nested(65), {}, cfg)
    assert result.errors == TOO_DEEP
    assert result.external_refs is None


def test_parser_recursion_is_too_deep() -> None:
    # json.loads raises RecursionError, not ValueError, at about 1000 levels.
    result = analyse(nested(5000), {}, cfg)
    assert result.errors == TOO_DEEP
    assert result.external_refs is None


def test_configured_limit_is_used() -> None:
    result = analyse(nested(6), {}, ValidatorConfig(max_nesting_depth=5))
    assert result.errors == ["Schema is nested more than 5 levels deep, counting every JSON object and array."]


def test_javatype_allowed() -> None:
    result = run(schema(javaType="com.acme.Order"))
    assert result.errors == []
    assert result.expected_name == "com.acme.Order"


def test_several_errors() -> None:
    result = run({"$schema": D7, "type": "array"})
    assert result.errors == [TITLE_MISSING, TOP_LEVEL]


def test_empty_ref_is_internal() -> None:
    # "" is a same-document reference, like "#".
    assert run(schema(properties={"a": {"$ref": ""}})).external_refs == set()


def test_property_named_like_data_keyword_is_walked() -> None:
    result = run(schema(properties={"default": {"$ref": "x.json"}, "enum": {"$ref": "y.json"}}))
    assert result.external_refs == {"x.json", "y.json"}


def refs_of(d: Any) -> set[str] | None:
    return run(d).external_refs


def test_relative_ref_under_id_resolves_to_absolute() -> None:
    # J2
    result = run(schema(**{"$id": "https://acme.example/schemas/order.json",
                           "properties": {"a": {"$ref": "address.json"}}}))
    assert result.external_refs == {"https://acme.example/schemas/address.json"}


def test_ref_to_own_id_with_fragment_is_internal() -> None:
    # J5b
    result = run(schema(**{"$id": "https://acme.example/o5b.json",
                           "definitions": {"x": {"type": "string"}},
                           "properties": {"x": {"$ref": "https://acme.example/o5b.json#/definitions/x"}}}))
    assert result.external_refs == set()


def test_fragment_only_root_id_keeps_the_empty_base() -> None:
    result = run(schema(**{"$id": "#foo", "properties": {"a": {"$ref": "#/definitions/x"},
                                                         "b": {"$ref": "address.json"}}}))
    assert result.external_refs == {"address.json"}


def test_non_string_id_is_ignored() -> None:
    result = run(schema(**{"$id": 5, "properties": {"a": {"$ref": "a.json"}}}))
    assert result.external_refs == {"a.json"}


def test_draft04_uses_id() -> None:
    result = run(schema(**{"$schema": "http://json-schema.org/draft-04/schema#",
                           "id": "https://x/dir/root.json", "$id": "https://ignored/",
                           "properties": {"a": {"$ref": "a.json"}}}))
    assert result.external_refs == {"https://x/dir/a.json"}


def test_https_draft07_rejected() -> None:
    draft = "https://json-schema.org/draft-07/schema#"
    assert run(schema(**{"$schema": draft})).errors == [f"Unsupported JSON Schema draft '{draft}'."]


def test_json_too_deep_still_reported_above_the_configured_limit() -> None:
    # Depth 661: 330 nested "properties" levels, deeper than jsonschema's check_schema can follow.
    node: dict[str, Any] = {"type": "string"}
    for _ in range(330):
        node = {"properties": {"p": node}}
    text = json.dumps(schema(**node))
    result = analyse(text, {}, ValidatorConfig(max_nesting_depth=10_000))
    assert result.errors == ["JSON schema is nested too deeply."]


def test_malformed_id_is_an_error() -> None:
    bad = "Invalid JSON schema: the $id 'http://[x' is not a valid URI"
    root = run(schema(**{"$id": "http://[x", "properties": {"a": {"$ref": "a.json"}}}))
    assert root.errors == [bad] and root.external_refs is None
    draft04 = run(schema(**{"$schema": "http://json-schema.org/draft-04/schema#", "id": "http://[x"}))
    assert draft04.errors == [bad.replace("$id", "id")] and draft04.external_refs is None


def test_malformed_ref_is_an_error() -> None:
    result = run(schema(properties={"a": {"$ref": "http://[x"}}))
    assert result.errors == ["Invalid JSON schema: the $ref 'http://[x' is not a valid URI"]
    assert result.external_refs is None


ROOT = "https://acme.example/o.json"
A = "https://acme.example/a.json"
DRAFT_07 = D7
DRAFT_04 = "http://json-schema.org/draft-04/schema#"
DRAFT_06 = "http://json-schema.org/draft-06/schema#"
DRAFT_2020 = "https://json-schema.org/draft/2020-12/schema"


def nested_id(paths: str, kw: str = "$id") -> str:
    return messages.JSON_NESTED_ID.format(kw=kw, paths=paths)


# Shapes whose nested "$id" the Python client does not register, or reads differently: all rule A now.
NESTED_ID_SHAPES = {
    "defs-in-draft-07": (schema(**{"$id": ROOT, "$defs": {"A": {"$id": A, "type": "string"}},
                                   "properties": {"p": {"$ref": A}}}), "/$defs/A"),
    "unknown-keyword": (schema(**{"$id": ROOT, "x-defs": {"A": {"$id": A, "type": "string"}},
                                  "properties": {"p": {"$ref": A}}}), "/x-defs/A"),
    "id-beside-ref-draft-07": (schema(**{"$id": ROOT, "properties": {
        "p": {"$id": "https://acme.example/sub/", "$ref": "a.json"}}}), "/properties/p"),
    "id-beside-ref-2020-12": (schema(**{"$schema": DRAFT_2020, "$id": ROOT, "properties": {
        "p": {"$id": "https://acme.example/sub/", "$ref": "a.json"}}}), "/properties/p"),
    "id-with-fragment": (schema(**{"$id": ROOT, "definitions": {"x": {"$id": A + "#frag"}},
                                   "properties": {"p": {"$ref": A}}}), "/definitions/x"),
    "id-under-nested-schema": (schema(**{"$schema": DRAFT_2020, "$id": ROOT, "properties": {
        "s": {"$schema": D7, "properties": {"t": {"$id": "t.json"}}}}}), "/properties/s/properties/t"),
}


@pytest.mark.parametrize("shape", NESTED_ID_SHAPES)
def test_nested_id_shapes_are_rule_errors(shape: str) -> None:
    d, path = NESTED_ID_SHAPES[shape]
    result = run(d)
    assert nested_id(f"'{path}'") in result.errors
    assert result.external_refs is None


@pytest.mark.parametrize("shape", NESTED_ID_SHAPES)
def test_nested_id_shapes_fail_validate_without_the_registry(shape: str) -> None:
    request = ValidationRequest(name="com.acme.Order", schema_type="JSON",
                                schema=json.dumps(NESTED_ID_SHAPES[shape][0]), compatibility="BACKWARD",
                                references=[], previous_name=None, existing_names=[])
    sr = FakeSchemaRegistry()
    result = validate(request, sr, cfg)
    assert result.validationType == "Error" and not result.valid
    assert sr.calls == []


def test_nested_schema_keyword_is_an_error() -> None:
    # Audit issue 2: the registry reads "$schema" by the draft above it; the Python client by its own.
    result = run(schema(**{"$schema": DRAFT_2020, "$id": ROOT,
                           "properties": {"p": {"$schema": D7, "properties": {
                               "t": {"$id": "https://acme.example/sub/", "$ref": "a.json"}}}}}))
    assert result.errors == [nested_id("'/properties/p/properties/t'"),
                             messages.JSON_NESTED_SCHEMA.format(paths="'/properties/p'")]
    assert result.external_refs is None


def test_nested_id_under_unknown_keyword_is_an_error() -> None:
    result = run(schema(**{"$id": ROOT, "x-defs": {"A": {"$id": A, "type": "string"}}}))
    assert result.errors == [nested_id("'/x-defs/A'")]


def test_long_nested_id_chain_is_one_error_and_fast() -> None:
    # 2,000 siblings and a 100-level chain: a deep chain cannot even be parsed, so this is the widest case.
    siblings = {f"d{n}": {"$id": f"d{n}/", "x": 1} for n in range(2000)}
    chain: dict[str, Any] = {"$ref": "leaf.json"}
    for n in range(100):
        chain = {"$id": f"c{n}/", "x": chain}
    d = schema(**{"x-defs": siblings, "x-chain": chain})
    total = 2000 + 100
    start = time.perf_counter()
    result = analyse(json.dumps(d), {}, ValidatorConfig(max_nesting_depth=200))
    assert time.perf_counter() - start < 1
    assert len(result.errors) == 1
    assert result.errors[0].startswith("Only the top level of a JSON schema may have \"$id\". Remove it from '/")
    assert f"' and {total - 3} more." in result.errors[0]
    assert result.external_refs is None


def test_more_than_three_paths_are_summarised() -> None:
    d = schema(**{"x-defs": {f"n{i}": {"$id": f"n{i}/"} for i in (4, 1, 3, 0, 2)}})
    assert run(d).errors == [nested_id("'/x-defs/n0', '/x-defs/n1', '/x-defs/n2' and 2 more")]


def test_exactly_three_paths_have_no_summary() -> None:
    d = schema(**{"x-defs": {f"n{i}": {"$id": f"n{i}/"} for i in range(3)}})
    assert run(d).errors == [nested_id("'/x-defs/n0', '/x-defs/n1', '/x-defs/n2'")]


def test_exactly_four_paths_are_summarised_as_one_more() -> None:
    d = schema(**{"x-defs": {f"n{i}": {"$id": f"n{i}/"} for i in range(4)}})
    assert run(d).errors == [nested_id("'/x-defs/n0', '/x-defs/n1', '/x-defs/n2' and 1 more")]


def test_paths_are_json_pointers() -> None:
    d = schema(**{"x-defs": {"a/b": {"$id": "x/"}, "c~d": {"$id": "y/"}}, "allOf": [{"$id": "z/"}]})
    assert run(d).errors == [nested_id("'/allOf/0', '/x-defs/a~1b', '/x-defs/c~0d'")]


@pytest.mark.parametrize("draft", [DRAFT_04, DRAFT_06, DRAFT_07])
def test_root_id_beside_root_ref_up_to_draft07_is_an_error(draft: str) -> None:
    keyword = "id" if draft == DRAFT_04 else "$id"
    d = schema(**{"$schema": draft, keyword: ROOT, "$ref": "#/definitions/O", "definitions": {"O": {"type": "object"}}})
    assert run(d).errors == [messages.JSON_ROOT_ID_WITH_REF.format(kw=keyword)]
    assert run(d).external_refs is None


def test_root_id_beside_root_ref_in_2020_12_is_fine() -> None:
    d = schema(**{"$schema": DRAFT_2020, "$id": ROOT, "$ref": "#/definitions/O", "definitions": {"O": {"type": "object"}}})
    assert run(d).errors == []


def test_keyword_names_as_property_names_or_data_are_not_flagged() -> None:
    result = run(schema(**{
        "properties": {"$id": {"type": "string"}, "id": {"type": "string"}, "$schema": {"type": "string"}},
        "default": {"$id": "x", "$schema": "y"}, "const": {"$id": "x", "$schema": "y"},
        "enum": [{"$id": "x", "$schema": "y"}], "examples": [{"$id": "x", "$schema": "y"}],
    }))
    assert result.errors == []
    assert result.external_refs == set()


def test_only_the_drafts_own_id_keyword_is_flagged() -> None:
    nested = {"properties": {"p": {"type": "string", "$id": "x/"}}}
    assert run(schema(**{"$schema": DRAFT_04, **nested})).errors == []
    assert run(schema(**{"$schema": DRAFT_04, "properties": {"p": {"id": "x/"}}})).errors == [
        nested_id("'/properties/p'", "id")]
    assert run(schema(**{"properties": {"p": {"id": "x/"}}})).errors == []


def test_rule_errors_skip_reference_checks() -> None:
    nested = run(schema(properties={"p": {"$id": "x/"}, "q": {"$ref": "other.json"}}))
    assert nested.external_refs is None
    missing = run({"title": "a.B", "type": "object", "properties": {"q": {"$ref": "other.json"}}})
    assert missing.errors == [messages.JSON_SCHEMA_MISSING]
    assert missing.external_refs == {"other.json"}


def test_rules_with_an_unusable_schema_keyword_use_dollar_id() -> None:
    # JSON_DRAFT is reported; the id keyword is "$id" and rule D is skipped.
    d = schema(**{"$schema": 7, "$id": ROOT, "$ref": "#/x", "properties": {"p": {"$id": "x/"}}})
    assert run(d).errors == ["Unsupported JSON Schema draft '7'.", nested_id("'/properties/p'")]


def test_ref_under_id_and_absolute_ids() -> None:
    # A ref to the root's own document is internal, with or without a fragment; an absolute root resolves.
    result = run(schema(**{"$id": ROOT + "#", "properties": {"a": {"$ref": ROOT}, "b": {"$ref": "#/x"},
                                                           "c": {"$ref": "b.json#/y"}, "d": {"$ref": ""}}}))
    assert result.external_refs == {"https://acme.example/b.json"}


def test_example_data_is_not_walked() -> None:
    # OpenAPI-style "example" holds data, like "examples".
    result = run(schema(**{"$schema": DRAFT_04, "properties": {"user": {
        "type": "object", "properties": {"id": {"type": "integer"}},
        "example": {"id": 1, "$schema": "x", "$id": "y"}}}}))
    assert result.errors == []


def test_root_id_with_a_fragment_is_an_error() -> None:
    frag = ROOT + "#frag"
    expected = messages.JSON_ROOT_ID_FRAGMENT.format(kw="$id", value=frag)
    result = run(schema(**{"$id": frag, "properties": {"a": {"$ref": "a.json"}}}))
    assert result.errors == [expected]
    assert result.external_refs is None
    draft04 = run(schema(**{"$schema": DRAFT_04, "id": frag}))
    assert draft04.errors == [expected.replace('"$id"', '"id"')]
    assert draft04.external_refs is None


def test_root_id_fragment_rule_leaves_other_ids_alone() -> None:
    assert run(schema(**{"$id": "#foo"})).errors == []
    assert run(schema(**{"$id": ROOT + "#"})).errors == []
    assert run(schema(**{"$id": "http://[x#f"})).errors == [
        "Invalid JSON schema: the $id 'http://[x#f' is not a valid URI"]


def test_root_id_fragment_in_2020_12_is_the_meta_schema_error() -> None:
    result = run(schema(**{"$schema": "https://json-schema.org/draft/2020-12/schema", "$id": ROOT + "#frag"}))
    assert len(result.errors) == 1 and result.errors[0].startswith("Invalid JSON schema: ")


def test_root_id_fragment_comes_after_rule_d() -> None:
    result = run(schema(**{"$id": ROOT + "#frag", "$ref": "#/definitions/x"}))
    assert result.errors == [
        messages.JSON_ROOT_ID_WITH_REF.format(kw="$id"),
        messages.JSON_ROOT_ID_FRAGMENT.format(kw="$id", value=ROOT + "#frag"),
    ]


def _items_chain(draft: str, depth: int) -> str:
    node: dict[str, Any] = {"type": "string"}
    for _ in range(depth - 1):
        node = {"items": node}
    text = json.dumps(schema(**{"$schema": draft, **node}))
    assert json_depth(json.loads(text), 1000) == depth
    return text


@pytest.mark.parametrize("draft", ["https://json-schema.org/draft/2019-09/schema",
                                   "https://json-schema.org/draft/2020-12/schema"])
def test_schema_shaped_at_limit_items(draft: str) -> None:
    # The tightest shape: check_schema overflows an "items" chain from depth 100 (2019-09) or 124
    # (2020-12) on a shallow stack, at about 10 frames per level. At the limit of 64 that leaves about
    # 360 frames for the producer's own stack, so the chain passes cleanly.
    result = analyse(_items_chain(draft, 64), {}, cfg)
    assert result.errors == []
    assert result.external_refs == set()


def test_items_chain_at_the_old_limit_is_too_deep() -> None:
    # Depth 100, the earlier default, is where 2019-09 "items" overflows check_schema: now refused first.
    result = analyse(_items_chain("https://json-schema.org/draft/2019-09/schema", 100), {}, cfg)
    assert result.errors == TOO_DEEP


def test_long_schema_error_is_shortened() -> None:
    schema = {"$schema": D7, "title": "a.B", "type": "x" * 400000}
    result = analyse(json.dumps(schema), {}, cfg)
    invalid = [e for e in result.errors if e.startswith("Invalid JSON schema: ")]
    assert len(invalid) == 1
    assert len(invalid[0]) <= len("Invalid JSON schema: ") + 1000
    assert invalid[0].endswith("…")


def test_long_uri_error_is_shortened() -> None:
    schema = {"$schema": D7, "title": "a.B", "type": "object", "$id": "http://[" + "x" * 5000}
    result = analyse(json.dumps(schema), {}, cfg)
    assert all(len(e) <= len("Invalid JSON schema: ") + 1000 for e in result.errors)


# --- $ref values that point inside the schema (audit 2 F9) -------------------------------

R8 = "https://acme.example/r8.json"


def ref_schema(ref: str, with_id: bool = True, **extra: Any) -> dict[str, Any]:
    d = schema(definitions={"s": {"type": "string"}}, properties={"a": {"$ref": ref}}, **extra)
    if with_id:
        d["$id"] = R8
    return d


A_PATH = "'/properties/a/$ref'"
A_DIFFERS = messages.JSON_REF_CLIENTS_DIFFER.format(paths=A_PATH)


@pytest.mark.parametrize("with_id", [True, False])
def test_dangling_pointer_is_an_error(with_id: bool) -> None:
    result = run(ref_schema("#/definitions/missing", with_id))
    assert result.errors == [messages.JSON_REF_NOT_FOUND.format(paths=A_PATH)]
    assert result.external_refs == set()


def test_list_index_out_of_range_is_an_error() -> None:
    result = run(ref_schema("#/definitions/s/0"))
    assert result.errors == [messages.JSON_REF_NOT_FOUND.format(paths=A_PATH)]


@pytest.mark.parametrize("ref", [R8 + "#/definitions/s", "r8.json#/definitions/s"])
def test_self_url_is_an_error(ref: str) -> None:
    result = run(ref_schema(ref))
    assert result.errors == [messages.JSON_REF_SELF_URL.format(kw="$id", paths=A_PATH)]
    assert result.external_refs == set()


def test_self_url_under_draft_04_names_id() -> None:
    d = ref_schema("r8.json#/definitions/s", with_id=False)
    d["$schema"] = "http://json-schema.org/draft-04/schema#"
    d["id"] = R8
    assert run(d).errors == [messages.JSON_REF_SELF_URL.format(kw="id", paths=A_PATH)]


def test_self_url_to_a_missing_place_is_one_error() -> None:
    result = run(ref_schema(R8 + "#/definitions/missing"))
    assert result.errors == [messages.JSON_REF_SELF_URL.format(kw="$id", paths=A_PATH)]


@pytest.mark.parametrize("ref", ["#/defin%69tions/s", "#/definitions/a~1b", "#/definitions/t~0", "#/allOf/1/definitions/x"])
def test_escaped_pointer_resolves(ref: str) -> None:
    d = ref_schema(ref)
    d["definitions"].update({"a/b": {}, "t~": {}})
    d["allOf"] = [{}, {"definitions": {"x": {}}}]
    assert run(d).errors == []


# "\uff11" is a full-width 1: an index is ASCII digits only.
@pytest.mark.parametrize("ref", ["#/allOf/-1", "#/allOf/x", "#/allOf/2", "#/allOf/\uff11", "#/definitions/s/type/x"])
def test_bad_list_index_or_scalar_is_not_found(ref: str) -> None:
    d = ref_schema(ref)
    d["allOf"] = [{}, {}]
    assert run(d).errors == [messages.JSON_REF_NOT_FOUND.format(paths=A_PATH)]


def test_ref_in_default_is_not_checked() -> None:
    d = ref_schema("#/definitions/s", default={"$ref": "#/nope"})
    assert run(d).errors == []


@pytest.mark.parametrize("ref", ["#", ""])
def test_empty_fragment_is_the_root(ref: str) -> None:
    assert run(ref_schema(ref)).errors == []


def test_anchor_fragment_is_checked_only_by_python() -> None:
    # The registry reads an unknown anchor as the root; the Python client cannot follow it.
    assert run(ref_schema("#foo")).errors == [A_DIFFERS]


def test_many_dangling_refs_are_listed_three_and_more() -> None:
    d = schema(properties={f"p{i}": {"$ref": "#/nope"} for i in range(5)})
    paths = "'/properties/p0/$ref', '/properties/p1/$ref', '/properties/p2/$ref' and 2 more"
    assert run(d).errors == [messages.JSON_REF_NOT_FOUND.format(paths=paths)]


D2019 = "https://json-schema.org/draft/2019-09/schema"
D2020 = "https://json-schema.org/draft/2020-12/schema"


def defs_ref_schema(draft: str, ref: str, **extra: Any) -> dict[str, Any]:
    return {"$schema": draft, "$id": R8, "title": "com.acme.Order", "type": "object",
            "$defs": {"s": {"type": "string"}}, "properties": {"a": {"$ref": ref}}, **extra}


@pytest.mark.parametrize("draft", [D2019, D2020])
@pytest.mark.parametrize("ref", [R8 + "#/$defs/s", "r8.json#/$defs/s", R8 + "#", "r8.json#", R8 + "#/%24defs/s"])
def test_self_url_is_internal_under_2019_and_2020(draft: str, ref: str) -> None:
    # The registry accepts these under 2019-09 and 2020-12 (checked on :18081); it rejects them under 04 to 07.
    result = run(defs_ref_schema(draft, ref))
    assert result.errors == []
    assert result.external_refs == set()


@pytest.mark.parametrize("draft", [D2019, D2020])
@pytest.mark.parametrize("ref", [R8 + "#/$defs/missing", "r8.json#/$defs/missing", R8 + "#/$defs/s/0"])
def test_self_url_to_a_missing_place_under_2019_and_2020_is_not_found(draft: str, ref: str) -> None:
    assert run(defs_ref_schema(draft, ref)).errors == [messages.JSON_REF_NOT_FOUND.format(paths=A_PATH)]


@pytest.mark.parametrize("draft", [D2019, D2020])
@pytest.mark.parametrize("anchor, expected", [("foo", []), ("bar", [A_DIFFERS])])
def test_self_url_anchor_under_2019_and_2020_is_checked_only_by_python(
    draft: str, anchor: str, expected: list[str],
) -> None:
    # Same rule as "#foo": the Python client must find the anchor.
    d = defs_ref_schema(draft, R8 + "#foo")
    d["$defs"]["s"]["$anchor"] = anchor
    assert run(d).errors == expected


@pytest.mark.parametrize("draft", ["http://json-schema.org/draft-06/schema#", D7])
def test_self_url_under_draft_06_and_07_is_an_error(draft: str) -> None:
    d = ref_schema(R8 + "#/definitions/s")
    d["$schema"] = draft
    assert run(d).errors == [messages.JSON_REF_SELF_URL.format(kw="$id", paths=A_PATH)]


def test_self_url_without_schema_is_an_error() -> None:
    # The registry reads a schema with no "$schema" as draft-07, which rejects a self-URL.
    d = ref_schema(R8 + "#/definitions/s")
    del d["$schema"]
    assert run(d).errors == [
        messages.JSON_SCHEMA_MISSING, messages.JSON_REF_SELF_URL.format(kw="$id", paths=A_PATH),
    ]


def test_self_url_and_dangling_give_two_messages() -> None:
    d = schema(properties={"a": {"$ref": "r8.json#/x"}, "b": {"$ref": "#/nope"}}, **{"$id": R8})
    assert run(d).errors == [
        messages.JSON_REF_SELF_URL.format(kw="$id", paths="'/properties/a/$ref'"),
        messages.JSON_REF_NOT_FOUND.format(paths="'/properties/b/$ref'"),
    ]


def test_external_ref_is_not_checked_for_a_pointer() -> None:
    result = run(ref_schema("other.json#/nope"))
    assert result.errors == []
    assert result.external_refs == {"https://acme.example/other.json"}


# --- Pointer tokens resolve the way the registry does (audit 2 F9, review) ---------------

def test_bare_own_url_is_not_a_self_url() -> None:
    # The registry accepts the document's own URL with no "#"; with a "#" it rejects it.
    assert run(ref_schema(R8)).errors == []
    assert run(ref_schema("r8.json")).errors == []
    assert run(ref_schema(R8 + "#")).errors == [messages.JSON_REF_SELF_URL.format(kw="$id", paths=A_PATH)]


@pytest.mark.parametrize("ref", ["#/definitions/s/", "#/definitions/s//", "#/definitions/", "#/"])
def test_empty_tokens_at_the_end_differ(ref: str) -> None:
    # The registry ignores them; the Python client looks for a "" key, or finds a different place.
    d = ref_schema(ref)
    d["definitions"][""] = {"x": {}}
    assert run(d).errors == [A_DIFFERS]


def test_empty_token_in_the_middle_with_an_empty_key_passes() -> None:
    d = ref_schema("#/definitions//x")
    d["definitions"][""] = {"x": {}}
    assert run(d).errors == []


@pytest.mark.parametrize("ref", ["#/definitions/zz/", "#//definitions/s", "#/definitions//x"])
def test_empty_token_in_the_middle_needs_an_empty_key(ref: str) -> None:
    assert run(ref_schema(ref)).errors == [messages.JSON_REF_NOT_FOUND.format(paths=A_PATH)]


NOT_FOUND = messages.JSON_REF_NOT_FOUND.format(paths=A_PATH)


@pytest.mark.parametrize("ref, defs, expected", [
    # The registry splits, then decodes; the Python client decodes, then splits.
    ("#/definitions/a%2Fb", {"a/b": {}}, [A_DIFFERS]),
    ("#/definitions/a%2Fb", {"a": {"b": {}}}, [NOT_FOUND]),
    # The registry reads "+" as a space; the Python client does not.
    ("#/definitions/a+b", {"a b": {}}, [A_DIFFERS]),
    ("#/definitions/a+b", {"a+b": {}}, [NOT_FOUND]),
    ("#/definitions/a%2Bb", {"a+b": {}}, []),
    ("#/definitions/a%2Bb", {"a b": {}}, [NOT_FOUND]),
    ("#/definitions/a%20b", {"a b": {}}, []),
    ("#/definitions/%C3%A9", {"é": {}}, []),
    ("#/definitions/é", {"é": {}}, []),
    ("#/definitions/a%7E1b", {"a/b": {}}, []),
])
def test_tokens_are_split_then_decoded(ref: str, defs: dict[str, Any], expected: list[str]) -> None:
    d = ref_schema(ref)
    d["definitions"] = defs
    assert run(d).errors == expected


@pytest.mark.parametrize("ref", ["#/definitions/%zz", "#/definitions/%", "#/definitions/%2f", "#/definitions/%e9", "#/definitions/%E9"])
def test_unpinned_encodings_are_checked_only_by_python(ref: str) -> None:
    # The registry reading skips them; the Python client finds nothing there.
    assert run(ref_schema(ref)).errors == [A_DIFFERS]


@pytest.mark.parametrize("ref, key", [("#/definitions/%zz", "%zz"), ("#/definitions/%E9", "\ufffd")])
def test_unpinned_encoding_the_python_client_follows_passes(ref: str, key: str) -> None:
    d = ref_schema(ref)
    d["definitions"][key] = {}
    assert run(d).errors == []


# --- A "$ref" must resolve the same for the registry and Python (audit 3 I1, M7) ---------

X_PATH = "'/properties/x/$ref'"
X_DIFFERS = [messages.JSON_REF_CLIENTS_DIFFER.format(paths=X_PATH)]
ADDRESS = json.dumps({"$schema": D7, "type": "object", "properties": {"street": {"type": "string"}}})


def x_ref(ref: str, draft: str = D7, **extra: Any) -> dict[str, Any]:
    return {"$schema": draft, "title": "com.acme.Order", "type": "object", "properties": {"x": {"$ref": ref}}, **extra}


def run_with(d: dict[str, Any], referenced: dict[str, str]) -> FormatAnalysis:
    return analyse(json.dumps(d), referenced, cfg)


def test_plus_in_pointer_differs() -> None:
    # The registry reads "+" as a space; Python's client does not.
    assert run(x_ref("#/definitions/a+b", definitions={"a b": {"type": "string"}})).errors == X_DIFFERS


def test_trailing_slash_differs() -> None:
    # The registry ignores the empty last token; Python's client looks for a "" key.
    assert run(x_ref("#/definitions/A/", definitions={"A": {"type": "string"}})).errors == X_DIFFERS


def test_empty_token_target_differs() -> None:
    # Both resolve, to different places: the registry to "definitions", Python to definitions[""].
    assert run(x_ref("#/definitions/", definitions={"": {"type": "string"}})).errors == X_DIFFERS


def test_missing_anchor_differs() -> None:
    # The registry resolves an unknown anchor to the root; Python's client cannot follow it.
    assert run(x_ref("#nope", definitions={"s": {"type": "string"}})).errors == X_DIFFERS


@pytest.mark.parametrize("draft", [D2019, D2020])
def test_existing_anchor_passes(draft: str) -> None:
    # A draft-07 anchor is a nested "$id", which JSON_NESTED_ID rejects, so "$anchor" is used.
    assert run(x_ref("#nope", draft, **{"$defs": {"s": {"$anchor": "nope", "type": "string"}}})).errors == []


def test_missing_anchor_in_a_reference_differs() -> None:
    result = run_with(x_ref("address.json#nope"), {"address.json": ADDRESS})
    assert result.errors == X_DIFFERS
    assert result.external_refs == {"address.json"}
    assert run_with(x_ref("address.json#/properties/street"), {"address.json": ADDRESS}).errors == []


def test_leading_zero_index_passes() -> None:
    # The registry, Python's client and Java all read "01" as index 1.
    assert run(x_ref("#/allOf/01", allOf=[{}, {"type": "object"}])).errors == []


@pytest.mark.parametrize("draft, ref, extra, referenced", [
    (D7, "#/definitions/Item", {"definitions": {"Item": {"type": "string"}}}, {}),
    (D2019, "#/$defs/Item", {"$defs": {"Item": {"type": "string"}}}, {}),
    (D2020, "#/$defs/Item", {"$defs": {"Item": {"type": "string"}}}, {}),
    (D7, "#/definitions/a~1b", {"definitions": {"a/b": {}}}, {}),
    (D7, "#/definitions/t~0", {"definitions": {"t~": {}}}, {}),
    (D7, "#/defin%69tions/s", {"definitions": {"s": {}}}, {}),
    (D2020, R8 + "#/$defs/Item", {"$id": R8, "$defs": {"Item": {"type": "string"}}}, {}),
    (D7, "address.json#/properties/street", {}, {"address.json": ADDRESS}),
])
def test_ordinary_refs_still_pass(draft: str, ref: str, extra: dict[str, Any], referenced: dict[str, str]) -> None:
    assert run_with(x_ref(ref, draft, **extra), referenced).errors == []


def test_reference_fragments_are_read_once_per_reference() -> None:
    d = schema(properties={
        "x": {"$ref": "address.json#/properties/street"}, "y": {"$ref": "address.json#nope"},
        "z": {"$ref": "address.json"}})
    paths = "'/properties/y/$ref'"
    assert run_with(d, {"address.json": ADDRESS}).errors == [messages.JSON_REF_CLIENTS_DIFFER.format(paths=paths)]


@pytest.mark.parametrize("text", ["{not json", "[" * 100_000])
def test_reference_text_that_is_not_json_is_skipped(text: str) -> None:
    assert run_with(x_ref("address.json#nope"), {"address.json": text}).errors == []


def test_reference_without_a_schema_keyword_is_read_as_draft_07() -> None:
    text = json.dumps({"definitions": {"n": {"$id": "#nope"}}})
    assert run_with(x_ref("address.json#nope"), {"address.json": text}).errors == []


def test_malformed_subschema_the_python_client_cannot_walk_differs() -> None:
    # Python's referencing raises a TypeError, not Unresolvable, on a definition that is a number.
    errors = run(x_ref("#/definitions/a", definitions={"a": 5})).errors
    assert errors[0].startswith("Invalid JSON schema: ")
    assert errors[1:] == X_DIFFERS


def test_refs_that_differ_are_listed_once_after_the_others() -> None:
    d = schema(**{"$id": R8}, properties={
        "a": {"$ref": "r8.json#/definitions/s"}, "b": {"$ref": "#/nope"}, "c": {"$ref": "#nope"},
        "d": {"$ref": "#/definitions/s/"}}, definitions={"s": {}})
    assert run(d).errors == [
        messages.JSON_REF_SELF_URL.format(kw="$id", paths="'/properties/a/$ref'"),
        messages.JSON_REF_NOT_FOUND.format(paths="'/properties/b/$ref'"),
        messages.JSON_REF_CLIENTS_DIFFER.format(paths="'/properties/c/$ref', '/properties/d/$ref'"),
    ]


def _anchors_and_refs(count: int, ref_prefix: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """`count` anchors under "$defs", one malformed subschema, and three "$ref"s to each anchor."""
    defs: dict[str, Any] = {f"d{i}": {"$anchor": f"a{i}", "type": "string"} for i in range(count)}
    defs["zz"] = {"items": 5}
    props = {f"p{i}": {"$ref": f"{ref_prefix}#a{i % count}"} for i in range(3 * count)}
    return defs, props


MANY_DIFFER = messages.JSON_REF_CLIENTS_DIFFER.format(
    paths="'/properties/p0/$ref', '/properties/p1/$ref', '/properties/p10/$ref' and 2997 more")


def test_anchors_in_a_schema_that_cannot_be_crawled_are_fast() -> None:
    # A failed crawl is not repeated for every anchor lookup (it took about 50 s at 2,000 anchors).
    defs, props = _anchors_and_refs(1000, "")
    d = {"$schema": D2020, "title": "com.acme.Order", "type": "object", "$defs": defs, "properties": props}
    start = time.perf_counter()
    errors = run(d).errors
    assert time.perf_counter() - start < 1
    assert errors[0].startswith("Invalid JSON schema: ")
    assert errors[1:] == [MANY_DIFFER]


def test_anchors_in_a_reference_that_cannot_be_crawled_are_fast() -> None:
    defs, props = _anchors_and_refs(1000, "address.json")
    text = json.dumps({"$schema": D2020, "type": "object", "$defs": defs, "properties": {"street": {}}})
    props["street"] = {"$ref": "address.json#/properties/street"}
    start = time.perf_counter()
    errors = run_with(schema(properties=props), {"address.json": text}).errors
    assert time.perf_counter() - start < 1
    # The pointer into the malformed reference still resolves; only the anchors cannot be followed.
    assert errors == [MANY_DIFFER]


FORMAT_PATH = "'/properties/e/format'"


def test_format_warns() -> None:
    result = run(schema(properties={"e": {"type": "string", "format": "email"}}))
    assert result.errors == []
    assert result.warnings == [messages.JSON_FORMAT_NOT_CHECKED.format(paths=FORMAT_PATH)]


def test_many_formats_are_listed_three_and_more() -> None:
    result = run(schema(properties={name: {"type": "string", "format": "email"} for name in "abcde"}))
    assert result.warnings == [messages.JSON_FORMAT_NOT_CHECKED.format(
        paths="'/properties/a/format', '/properties/b/format', '/properties/c/format' and 2 more")]


def test_format_as_data_or_property_name_has_no_warning() -> None:
    result = run(schema(
        properties={"format": {"type": "string"}, "p": {"default": {"format": "email"}}},
        patternProperties={"format": {"type": "string"}},
        enum=[{"format": "x"}], examples=[{"format": "x"}], example={"format": "x"}, const={"format": "x"},
    ))
    assert result.errors == []
    assert result.warnings == []


def test_format_that_is_not_a_string_has_no_warning() -> None:
    assert run(schema(properties={"e": {"format": 1}})).warnings == []


def test_no_format_warning_with_errors() -> None:
    result = run(schema(properties={"e": {"format": "email", "$id": "x"}}))
    assert result.errors and result.warnings == []


def test_metaschema_error_shows_json_and_location() -> None:
    result = run(schema(properties={"x": {"type": "string", "required": True}}))
    assert len(result.errors) == 1
    assert "true is not of type 'array'" in result.errors[0]
    assert "(at /properties/x/required)" in result.errors[0]
    assert result.errors[0].startswith(messages.JSON_INVALID.split("{err}")[0])


def test_metaschema_error_names_the_top_level_keyword() -> None:
    result = run(schema(required=True))
    assert any("(at /required)" in e for e in result.errors)
    result = run(schema(type=5))
    assert any("(at /type)" in e for e in result.errors)
