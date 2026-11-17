from __future__ import annotations

import json
import time
from typing import Any

import pytest

from schema_validator import ValidationRequest, ValidationResult, messages, validate
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
    assert result.errors == [TITLE_MISSING, "Invalid JSON schema: 5 is not of type 'string'"]
    assert result.expected_name is None


def test_deeply_nested_json_is_not_json() -> None:
    # json.loads raises RecursionError, not ValueError, past about 1000 levels.
    result = analyse("[" * 3000 + "]" * 3000, {}, cfg)
    assert len(result.errors) == 1
    assert result.errors[0].startswith("Schema is not valid JSON: ")
    assert result.external_refs is None


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


def test_deep_schema_gives_too_deep() -> None:
    node: dict[str, Any] = {"type": "string"}
    for _ in range(200):
        node = {"type": "object", "properties": {"p": node}}
    result = run(schema(properties={"p": node}))
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
    result = run(d)
    assert time.perf_counter() - start < 1
    assert len(result.errors) == 1
    assert result.errors[0].startswith("Only the top level of a JSON schema may have \"$id\". Remove it from '/")
    assert f"' and {total - 3} more." in result.errors[0]
    assert result.external_refs is None


def test_more_than_three_paths_are_summarised() -> None:
    d = schema(**{"x-defs": {f"n{i}": {"$id": f"n{i}/"} for i in (4, 1, 3, 0, 2)}})
    assert run(d).errors == [nested_id("'/x-defs/n0', '/x-defs/n1', '/x-defs/n2' and 2 more")]


def test_paths_are_json_pointers() -> None:
    d = schema(**{"x-defs": {"a/b": {"$id": "x/"}, "c~d": {"$id": "y/"}}, "allOf": [{"$id": "z/"}]})
    assert run(d).errors == [nested_id("'/allOf/0', '/x-defs/a~1b', '/x-defs/c~0d'")]


def test_root_id_beside_root_ref_in_draft07_is_an_error() -> None:
    d = schema(**{"$id": ROOT, "$ref": "#/definitions/O", "definitions": {"O": {"type": "object"}}})
    assert run(d).errors == [messages.JSON_ROOT_ID_WITH_REF.format(kw="$id")]
    assert run(d).external_refs is None
    assert run({**d, "$schema": DRAFT_2020}).errors == []
    d04 = {**d, "$schema": DRAFT_04, "id": ROOT}
    assert run(d04).errors == [messages.JSON_ROOT_ID_WITH_REF.format(kw="id")]


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
