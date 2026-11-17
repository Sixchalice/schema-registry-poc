from __future__ import annotations

import json
from typing import Any

import pytest

from schema_validator import ValidationRequest, ValidationResult, messages, validate
from schema_validator.formats.json_schema import analyse
from schema_validator.models import DEFAULT_JSON_DRAFTS, FormatAnalysis, ValidatorConfig
from tests.fake_sr import FakeSchemaRegistry

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
    sub = {"s": {"$id": "#foo", "properties": {"a": {"$ref": "a.json"}}}}
    result = run(schema(**{"$schema": "http://json-schema.org/draft-07/schema#", "properties": sub}))
    assert result.external_refs == {"a.json"}
    # Without $schema the 2020-12 reading (the Python client's) also applies: urljoin("#foo", "a.json").
    assert run(schema(properties=sub)).external_refs == {"a.json", "/a.json"}


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
    nested = run(schema(properties={"s": {"$id": "http://[x"}}))
    assert nested.errors == [bad] and nested.external_refs is None
    draft04 = run(schema(**{"$schema": "http://json-schema.org/draft-04/schema#", "id": "http://[x"}))
    assert draft04.errors == [bad.replace("$id", "id")] and draft04.external_refs is None


def test_malformed_ref_is_an_error() -> None:
    result = run(schema(properties={"a": {"$ref": "http://[x"}}))
    assert result.errors == ["Invalid JSON schema: the $ref 'http://[x' is not a valid URI"]
    assert result.external_refs is None


ROOT = "https://acme.example/o.json"
A = "https://acme.example/a.json"
DRAFT_07 = "http://json-schema.org/draft-07/schema#"
# Audit-fix review F-I1: an $id the Python client (`referencing`) does not register is not internal.
UNREGISTERED_ID_SHAPES = {
    "defs-in-draft-07": schema(**{"$schema": DRAFT_07, "$id": ROOT,
                                  "$defs": {"A": {"$id": A, "type": "string"}},
                                  "properties": {"p": {"$ref": A}}}),
    "unknown-keyword": schema(**{"$id": ROOT, "x-defs": {"A": {"$id": A, "type": "string"}},
                                 "properties": {"p": {"$ref": A}}}),
    "id-beside-ref-draft-07": schema(**{"$schema": DRAFT_07, "$id": ROOT,
                                        "properties": {"p": {"$id": "https://acme.example/sub/", "$ref": "a.json"}}}),
}


@pytest.mark.parametrize("shape", UNREGISTERED_ID_SHAPES)
def test_id_the_client_does_not_register_is_external(shape: str) -> None:
    result = run(UNREGISTERED_ID_SHAPES[shape])
    assert result.errors == []
    assert result.external_refs == {A}


@pytest.mark.parametrize("shape", UNREGISTERED_ID_SHAPES)
def test_id_the_client_does_not_register_needs_a_reference(shape: str) -> None:
    request = ValidationRequest(name="com.acme.Order", schema_type="JSON",
                                schema=json.dumps(UNREGISTERED_ID_SHAPES[shape]), compatibility="BACKWARD",
                                references=[], previous_name=None, existing_names=[])
    sr = FakeSchemaRegistry()
    assert validate(request, sr, cfg) == ValidationResult(False, "Error", messages.REF_MISSING_ROW.format(x=A))
    assert sr.calls == []


def test_id_beside_ref_moves_the_base_from_2019_09() -> None:
    result = run(schema(**{"$schema": "https://json-schema.org/draft/2020-12/schema", "$id": ROOT,
                           "properties": {"p": {"$id": "https://acme.example/sub/", "$ref": "a.json"}}}))
    assert result.external_refs == {"https://acme.example/sub/a.json"}


def test_without_schema_keyword_both_readings_need_a_reference() -> None:
    # Confluent reads such a schema as draft-07; the Python client's validator class is 2020-12.
    result = run(schema(**{"$id": ROOT, "properties": {"p": {"$id": "https://acme.example/sub/", "$ref": "a.json"}}}))
    assert result.external_refs == {A, "https://acme.example/sub/a.json"}
    assert run(schema(**{"$id": ROOT, "$defs": {"A": {"$id": A}}, "properties": {"p": {"$ref": A}}})).external_refs \
        == {A}


def test_base_under_an_unknown_keyword_depends_on_how_the_ref_is_reached() -> None:
    # Through "#/x-defs/A" the client applies q's $id; through a pointer straight to r it does not.
    result = run(schema(**{"$id": "https://x/root.json",
                           "x-defs": {"A": {"properties": {"q": {"$id": "https://y/",
                                                                 "properties": {"r": {"$ref": "b.json"}}}}}}}))
    assert result.external_refs == {"https://x/b.json", "https://y/b.json"}


def test_same_document_ref_under_an_unregistered_id_is_external() -> None:
    result = run(schema(**{"$schema": DRAFT_07, "$id": ROOT,
                           "x-defs": {"A": {"properties": {"q": {"$id": "https://y/", "$ref": "#/definitions/z"},
                                                           "r": {"$id": "https://z/",
                                                                 "items": {"$ref": "#/definitions/z"}}}}}}))
    # q's $id is ignored beside its $ref, so its ref stays in the document; r's $id is not registered.
    assert result.external_refs == {"https://z/"}


def test_id_under_a_nested_schema_keyword_is_not_internal() -> None:
    # referencing reads a subschema with its own $schema by that draft, which can drop the $id.
    result = run(schema(**{"$schema": "https://json-schema.org/draft/2020-12/schema", "$id": ROOT,
                           "properties": {"s": {"$schema": DRAFT_07, "$id": A, "$ref": "#/$defs/z",
                                                "properties": {"t": {"$id": "t.json"}}},
                                          "u": {"$ref": A}, "v": {"$ref": "https://acme.example/t.json"}},
                           "$defs": {"z": {"type": "string"}}}))
    assert result.external_refs == {A, "https://acme.example/t.json"}


def test_id_with_a_fragment_is_registered_with_it() -> None:
    # Draft-07 keeps the fragment of a subschema $id, and lookups drop the fragment of the $ref.
    result = run(schema(**{"$schema": DRAFT_07, "$id": ROOT,
                           "definitions": {"x": {"$id": A + "#frag", "type": "string"}},
                           "properties": {"p": {"$ref": A}}}))
    assert result.external_refs == {A}


def test_keyword_of_the_wrong_type_for_one_reading_does_not_raise() -> None:
    # Without $schema, "$defs": 5 is fine in draft-07 and wrong in 2020-12.
    assert run(schema(**{"$defs": 5, "properties": {"p": {"$ref": "a.json"}}})) == FormatAnalysis(
        "com.acme.Order", {"a.json"}, [])


def test_id_in_data_or_as_a_property_name_does_not_move_the_base() -> None:
    result = run(schema(**{
        "$id": "https://x/root.json",
        "properties": {"$id": {"type": "string"}, "a": {"$ref": "a.json"}},
        "default": {"$id": "https://other/"}, "const": {"$id": "https://other/"},
        "enum": [{"$id": "https://other/"}], "examples": [{"$id": "https://other/"}],
    }))
    assert result.external_refs == {"https://x/a.json"}
