from __future__ import annotations

import json
from typing import Any

import pytest

from schema_validator.formats.json_schema import analyse
from schema_validator.models import DEFAULT_JSON_DRAFTS, FormatAnalysis, ValidatorConfig

cfg = ValidatorConfig()

TITLE_MISSING = 'JSON schema needs a "title"; it becomes the schema name.'
TITLE_FORMAT = (
    'The "title" must be a dotted name made of letters, digits and underscores, '
    'for example "com.team.domain.Order".'
)
TOP_LEVEL = 'The top level of a JSON schema must have "type": "object".'


def run(d: Any) -> FormatAnalysis:
    return analyse(json.dumps(d), {}, cfg)


def schema(**extra: Any) -> dict[str, Any]:
    return {"title": "com.acme.Order", "type": "object", **extra}


def test_valid() -> None:
    result = run(
        {"title": "com.acme.Order", "type": "object", "properties": {"id": {"type": "string"}}}
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
    result = run({"type": "object"})
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
    result = run({"title": title, "type": "object"})
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


def test_no_schema_keyword_uses_draft7() -> None:
    d: dict[str, Any] = {
        "title": "a.B",
        "type": "object",
        "properties": {"x": {"type": "array", "items": [{"type": "string"}]}},
    }
    assert run(d).errors == []

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
    result = run({"title": 5, "type": "object"})
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
    result = run({"type": "array"})
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


def test_ref_to_internal_id_is_internal() -> None:
    # J5
    result = run(schema(**{"$id": "https://acme.example/o5.json",
                           "definitions": {"x": {"$id": "https://acme.example/x.json", "type": "string"}},
                           "properties": {"x": {"$ref": "https://acme.example/x.json"}}}))
    assert result.external_refs == set()


def test_ref_to_own_id_with_fragment_is_internal() -> None:
    # J5b
    result = run(schema(**{"$id": "https://acme.example/o5b.json",
                           "definitions": {"x": {"type": "string"}},
                           "properties": {"x": {"$ref": "https://acme.example/o5b.json#/definitions/x"}}}))
    assert result.external_refs == set()


def test_nested_id_moves_the_base() -> None:
    result = run(schema(properties={"s": {"$id": "https://x/sub/", "properties": {"a": {"$ref": "a.json"}}}}))
    assert result.external_refs == {"https://x/sub/a.json"}


def test_fragment_id_keeps_the_base() -> None:
    result = run(schema(**{"$id": "https://x/root.json",
                           "properties": {"s": {"$id": "#foo", "properties": {"a": {"$ref": "a.json"}}}}}))
    assert result.external_refs == {"https://x/a.json"}


def test_fragment_id_without_root_id_keeps_the_empty_base() -> None:
    result = run(schema(properties={"s": {"$id": "#foo", "properties": {"a": {"$ref": "a.json"}}}}))
    assert result.external_refs == {"a.json"}


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
