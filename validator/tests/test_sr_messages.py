from __future__ import annotations

import time
from typing import Any

from schema_validator.sr_messages import Incompatibility, invalid_schema_reason, parse_incompatibility

# Real Schema Registry 7.9.0 output.
AVRO = ["{errorType:'READER_FIELD_MISSING_DEFAULT_VALUE', description:'The field 'zip' at path '/fields/1' in the new schema has no default value and is missing in the old schema', additionalInfo:'zip'}",
        "{oldSchemaVersion: 1}",
        "{oldSchema: '{\"type\":\"record\",\"name\":\"Address\",\"namespace\":\"p\",\"fields\":[{\"name\":\"city\",\"type\":\"string\"}]}'}",
        "{validateFields: 'false', compatibility: 'BACKWARD'}"]
JSON_ = ["{errorType:\"TYPE_CHANGED\", description:\"A type at path '#/properties/city' is different between the new schema and the old schema'}",
         "{oldSchemaVersion: 3}", "{oldSchema: '{\"title\":\"p.JAddress\"}'}",
         "{validateFields: 'false', compatibility: 'FULL_TRANSITIVE'}"]
PROTO = ["{errorType:\"FIELD_SCALAR_KIND_CHANGED\", description:\"The kind of a SCALAR field at path '#/PAddress/1' in the new schema does not match its kind in the old schema\"}",
         "{oldSchemaVersion: 1}", "{oldSchema: 'syntax = \"proto3\";\npackage p;\n\nmessage PAddress {\n  string city = 1;\n}\n'}",
         "{validateFields: 'false', compatibility: 'BACKWARD'}"]
TYPE_CHANGE = ["Incompatible because of different schema type", "{validateFields: 'false', compatibility: 'BACKWARD'}"]
INVALID = "Invalid schema {subject=p.Order,version=0,id=-1,schemaType=AVRO,references=[],metadata=null,ruleSet=null,schema={\"type\": \"record\", \"doc\": \"details: not this\", \"name\": \"Order\"},schemaTags=null} with refs [] of type AVRO, details: \"strin\" is not a defined name."


def test_avro() -> None:
    assert parse_incompatibility(AVRO) == Incompatibility(("The field 'zip' at path '/fields/1' in the new schema has no default value and is missing in the old schema",), 1, "BACKWARD")


def test_json_mismatched_quotes() -> None:
    assert parse_incompatibility(JSON_) == Incompatibility(("A type at path '#/properties/city' is different between the new schema and the old schema",), 3, "FULL_TRANSITIVE")


def test_protobuf() -> None:
    assert parse_incompatibility(PROTO) == Incompatibility(("The kind of a SCALAR field at path '#/PAddress/1' in the new schema does not match its kind in the old schema",), 1, "BACKWARD")


def test_plain_message_kept() -> None:
    assert parse_incompatibility(TYPE_CHANGE) == Incompatibility(("Incompatible because of different schema type",), None, "BACKWARD")


def test_several_errors_keep_order() -> None:
    second = "{errorType:'READER_FIELD_MISSING_DEFAULT_VALUE', description:'The field 'street' at path '/fields/2' in the new schema has no default value and is missing in the old schema', additionalInfo:'street'}"
    result = parse_incompatibility([AVRO[0], second, *AVRO[1:]])
    assert result.descriptions == (
        "The field 'zip' at path '/fields/1' in the new schema has no default value and is missing in the old schema",
        "The field 'street' at path '/fields/2' in the new schema has no default value and is missing in the old schema",
    )
    assert (result.old_version, result.level) == (1, "BACKWARD")


def test_extracted_descriptions_come_before_unrecognised_messages() -> None:
    result = parse_incompatibility(["Incompatible because of different schema type", AVRO[0]])
    assert result.descriptions == (
        "The field 'zip' at path '/fields/1' in the new schema has no default value and is missing in the old schema",
        "Incompatible because of different schema type",
    )


def test_empty() -> None:
    assert parse_incompatibility([]) == Incompatibility((), None, None)


def test_old_schema_never_leaks() -> None:
    assert all("oldSchema" not in d and "record" not in d for d in parse_incompatibility(AVRO).descriptions)


def test_invalid_reason_takes_last_details() -> None:
    assert invalid_schema_reason(INVALID) == '"strin" is not a defined name.'


def test_invalid_reason_json() -> None:
    assert invalid_schema_reason("Invalid schema {subject=x,schema={},schemaTags=null}, details: Invalid JSON Schema") == "Invalid JSON Schema"


def test_invalid_reason_without_details() -> None:
    assert invalid_schema_reason("Invalid schema something") == "Invalid schema something"


def test_not_an_invalid_schema_message() -> None:
    assert invalid_schema_reason(AVRO[0]) is None


# Robustness: the text comes from a remote service, so nothing may raise.


def test_invalid_reason_empty_string() -> None:
    assert invalid_schema_reason("") is None


def test_invalid_reason_prefix_without_trailing_space() -> None:
    assert invalid_schema_reason("Invalid schema") is None


def test_invalid_reason_empty_details_falls_back_to_whole_message() -> None:
    msg = "Invalid schema {subject=x}, details: "
    assert invalid_schema_reason(msg) == msg


def test_invalid_reason_non_string() -> None:
    bad: Any = None
    assert invalid_schema_reason(bad) is None
    bad = 42
    assert invalid_schema_reason(bad) is None


def test_invalid_reason_unbalanced_braces_and_quotes() -> None:
    assert invalid_schema_reason("Invalid schema {subject=\"x,schema={{{', details: boom") == "boom"


def test_error_type_without_closing_brace_kept_unchanged() -> None:
    msg = "{errorType:'X', description:'half a message"
    assert parse_incompatibility([msg]).descriptions == (msg,)


def test_error_type_without_description_kept_unchanged() -> None:
    msg = "{errorType:'X', additionalInfo:'y'}"
    assert parse_incompatibility([msg]).descriptions == (msg,)


def test_error_type_with_empty_description_kept_unchanged() -> None:
    msg = "{errorType:'X', description:''}"
    assert parse_incompatibility([msg]).descriptions == (msg,)


def test_error_type_with_single_quote_description_kept_unchanged() -> None:
    msg = "{errorType:'X', description:'}"
    assert parse_incompatibility([msg]).descriptions == (msg,)


def test_description_without_quotes() -> None:
    assert parse_incompatibility(["{errorType:X, description:plain text}"]).descriptions == ("plain text",)


def test_description_cut_at_last_additional_info() -> None:
    msg = "{errorType:'X', description:'a, additionalInfo: inside', additionalInfo:'z'}"
    assert parse_incompatibility([msg]).descriptions == ("a, additionalInfo: inside",)


def test_half_matching_shapes_kept_unchanged() -> None:
    msgs = ["{oldSchemaVersion: x}", "{oldSchemaVersion: 1", "{validateFields: 'false', compatibility: 'backward'}",
            "{compatibility: 'BACKWARD'}"]
    assert parse_incompatibility(msgs) == Incompatibility(tuple(msgs), None, None)


def test_old_schema_dropped_even_when_malformed() -> None:
    assert parse_incompatibility(["{oldSchema: '{\"type\":\"record\""]) == Incompatibility((), None, None)


def test_old_schema_with_leading_whitespace_dropped() -> None:
    msgs = [" {oldSchema: '{\"a\":1}'}", "{oldSchemaVersion: 1}"]
    assert parse_incompatibility(msgs) == Incompatibility((), 1, None)


def test_huge_old_schema_version_does_not_raise() -> None:
    msg = "{oldSchemaVersion: " + "9" * 10_000 + "}"
    result = parse_incompatibility([msg])
    assert result.old_version is None
    assert result.descriptions == (msg,)


def test_non_string_items_skipped() -> None:
    items: Any = [None, 7, {"oldSchema": "x"}, b"bytes", *AVRO]
    assert parse_incompatibility(items) == parse_incompatibility(AVRO)


def test_messages_not_a_list() -> None:
    bad: Any = None
    assert parse_incompatibility(bad) == Incompatibility((), None, None)
    bad = 5
    assert parse_incompatibility(bad) == Incompatibility((), None, None)


def test_messages_given_as_one_string() -> None:
    bad: Any = "Incompatible because of different schema type"
    assert parse_incompatibility(bad).descriptions == ("Incompatible because of different schema type",)


def test_empty_string_message_dropped() -> None:
    assert parse_incompatibility(["", "   "]) == Incompatibility((), None, None)


def test_long_input_is_fast() -> None:
    n = 200_000
    msgs = [
        "{errorType:" + ", description:" * n,
        "{validateFields: " + "compatibility: '" * n + "A" * n,
        "{oldSchemaVersion: " + "1" * n,
        "{errorType:'X', description:'" + "'" * n + "}",
        "Invalid schema " + "details: " * n,
    ]
    start = time.monotonic()
    parse_incompatibility(msgs)
    invalid_schema_reason(msgs[-1])
    assert time.monotonic() - start < 2.0
