from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from schema_validator import messages
from schema_validator.models import ReferenceInput, ValidationRequest, ValidatorConfig
from schema_validator.rules import InputCheck, check_input, check_name

CONFIG = ValidatorConfig()


def req(**overrides: Any) -> ValidationRequest:
    base = ValidationRequest(
        name="com.acme.Order",
        schema_type="AVRO",
        schema='{"type": "record", "name": "Order", "namespace": "com.acme", "fields": []}',
        compatibility="BACKWARD",
        references=[],
        previous_name=None,
        existing_names=[],
    )
    return replace(base, **overrides)


# Stage A


def test_valid_input() -> None:
    assert check_input(req(), CONFIG) == InputCheck([], False, True)


def test_unsupported_format() -> None:
    result = check_input(req(schema_type="XML"), CONFIG)
    assert result.errors == ["Unsupported schema format 'XML'. Use AVRO, JSON or PROTOBUF."]
    assert result.fatal is True


def test_empty_schema() -> None:
    result = check_input(req(schema="  \n"), CONFIG)
    assert result.errors == ["Schema is empty."]
    assert result.fatal is True


def test_schema_too_large() -> None:
    result = check_input(req(schema="é" * 6), replace(CONFIG, max_schema_bytes=10))
    assert result.errors == ["Schema is too large (12 bytes, limit 10)."]
    assert result.fatal is True


def test_unknown_compatibility() -> None:
    result = check_input(req(compatibility="STRICT"), CONFIG)
    assert result.errors == ["Unknown compatibility level 'STRICT'."]
    assert result.fatal is False


@pytest.mark.parametrize("name", ["", "a b", "a\tb", ":.ctx:a.B"])
def test_invalid_names(name: str) -> None:
    result = check_input(req(name=name), CONFIG)
    assert result.errors == ["Schema name is invalid."]
    assert result.fatal is False


def test_duplicate_reference_subject() -> None:
    refs = [ReferenceInput("a.B"), ReferenceInput("a.B")]
    result = check_input(req(references=refs), CONFIG)
    assert result.errors == ["Reference 'a.B' is listed twice."]
    assert result.references_ok is False
    assert result.fatal is False


def test_duplicate_ref_string() -> None:
    refs = [ReferenceInput("a.B", "x.json"), ReferenceInput("a.C", "x.json")]
    result = check_input(req(schema_type="JSON", references=refs), CONFIG)
    assert result.errors == ["Reference 'x.json' is listed twice."]
    assert result.references_ok is False


def test_self_reference() -> None:
    result = check_input(req(references=[ReferenceInput("com.acme.Order")]), CONFIG)
    assert result.errors == ["A schema cannot reference itself."]
    assert result.references_ok is False


def test_avro_ref_string_defaults_to_schema_name() -> None:
    # a.C's own text 'a.B' is a custom ref_string (L6) and also repeats a.B's name.
    refs = [ReferenceInput("a.B"), ReferenceInput("a.C", "a.B")]
    result = check_input(req(references=refs), CONFIG)
    assert result.errors == [
        "Reference 'a.B' is listed twice.",
        "For an Avro reference to 'a.C', leave the reference text empty or write 'a.C'.",
    ]
    assert result.references_ok is False


def test_avro_custom_ref_string_rejected() -> None:
    refs = [ReferenceInput("a.Address", "address.avsc"), ReferenceInput("a.Money", "m")]
    result = check_input(req(references=refs), CONFIG)
    assert result.errors == [
        messages.AVRO_REF_STRING.format(s="a.Address"), messages.AVRO_REF_STRING.format(s="a.Money")]
    assert result.references_ok is False
    assert result.fatal is False


@pytest.mark.parametrize("ref_string", [None, "", "a.Address"])
def test_avro_ref_string_equal_to_name_allowed(ref_string: str | None) -> None:
    result = check_input(req(references=[ReferenceInput("a.Address", ref_string)]), CONFIG)
    assert result.errors == []
    assert result.references_ok is True


def test_lone_surrogate_schema() -> None:
    result = check_input(req(schema="\ud800"), CONFIG)
    assert result.errors == [messages.SCHEMA_NOT_TEXT]
    assert result.fatal is True


def test_avro_same_row_twice_reports_once() -> None:
    refs = [ReferenceInput("a.B"), ReferenceInput("a.B")]
    result = check_input(req(references=refs), CONFIG)
    assert result.errors == ["Reference 'a.B' is listed twice."]


def test_protobuf_ref_string_is_ignored() -> None:
    refs = [ReferenceInput("a.B", "x.proto"), ReferenceInput("a.C", "x.proto")]
    result = check_input(req(schema_type="PROTOBUF", references=refs), CONFIG)
    assert result.errors == []
    assert result.references_ok is True


def test_json_rows_without_ref_string_are_not_duplicates() -> None:
    refs = [ReferenceInput("a.B"), ReferenceInput("a.C")]
    result = check_input(req(schema_type="JSON", references=refs), CONFIG)
    assert result.errors == []
    assert result.references_ok is True


def test_json_rows_with_empty_ref_string_are_not_duplicates() -> None:
    # "" means "not filled in", like None: stage D reports D6 for each row.
    refs = [ReferenceInput("a.B", ""), ReferenceInput("a.C", "")]
    result = check_input(req(schema_type="JSON", references=refs), CONFIG)
    assert result.errors == []
    assert result.references_ok is True


def test_value_repeated_three_times_reports_once() -> None:
    refs = [ReferenceInput("a.B", "b.json"), ReferenceInput("a.B", "c.json"),
            ReferenceInput("a.B", "d.json")]
    result = check_input(req(schema_type="JSON", references=refs), CONFIG)
    assert result.errors == ["Reference 'a.B' is listed twice."]


def test_unsupported_format_still_reports_references() -> None:
    refs = [ReferenceInput("a.B"), ReferenceInput("a.B")]
    result = check_input(req(schema_type="XML", references=refs), CONFIG)
    assert result.errors == [
        "Unsupported schema format 'XML'. Use AVRO, JSON or PROTOBUF.",
        "Reference 'a.B' is listed twice.",
    ]
    assert result.fatal is True
    assert result.references_ok is False


def test_collects_all_non_fatal() -> None:
    result = check_input(req(compatibility="STRICT", name="a b"), CONFIG)
    assert result.errors == [
        "Unknown compatibility level 'STRICT'.",
        "Schema name is invalid.",
    ]
    assert result.fatal is False


# Stage C


def test_name_matches() -> None:
    assert check_name(req(), "com.acme.Order") == []


def test_name_mismatch() -> None:
    assert check_name(req(name="com.acme.Orders"), "com.acme.Order") == [
        "The name must match the schema. It should be 'com.acme.Order'."
    ]


def test_name_differs_only_by_case() -> None:
    assert check_name(req(name="com.acme.order"), "com.acme.Order") == [
        "The name must match the schema. It should be 'com.acme.Order'."
    ]


def test_duplicate_case_insensitive() -> None:
    assert check_name(req(existing_names=["COM.ACME.ORDER"]), "com.acme.Order") == [
        "A Schema named 'com.acme.Order' already exists "
        "(names are compared case-insensitively)."
    ]


def test_edit_ignores_own_name_in_existing_names() -> None:
    request = req(previous_name="a.B", name="a.B", existing_names=["a.B"])
    assert check_name(request, "a.B") == []


def test_edit_still_catches_other_case_variant() -> None:
    request = req(previous_name="a.B", name="a.B", existing_names=["A.b"])
    assert check_name(request, "a.B") == [messages.NAME_EXISTS.format(name="a.B")]


def test_rename_on_edit() -> None:
    request = req(previous_name="com.acme.Old", name="com.acme.New")
    assert check_name(request, "com.acme.Old") == [
        "A schema's name cannot change after creation (was 'com.acme.Old'). "
        "Create a new Schema instead.",
        "The name must match the schema. It should be 'com.acme.Old'.",
    ]


def test_fullname_changed_on_edit() -> None:
    request = req(previous_name="com.acme.Order", name="com.acme.Order")
    assert check_name(request, "com.acme.Order2") == [
        "The schema's full name changed from 'com.acme.Order' to 'com.acme.Order2'. "
        "A schema's name cannot change; revert the namespace/name or create a new Schema."
    ]


def test_fullname_changed_with_duplicate_name() -> None:
    request = req(previous_name="com.acme.Order", existing_names=["Com.Acme.Order"])
    assert check_name(request, "com.acme.Order2") == [
        "The schema's full name changed from 'com.acme.Order' to 'com.acme.Order2'. "
        "A schema's name cannot change; revert the namespace/name or create a new Schema.",
        "A Schema named 'com.acme.Order' already exists "
        "(names are compared case-insensitively).",
    ]


def test_rename_on_edit_without_expected_name() -> None:
    request = req(previous_name="com.acme.Old", name="com.acme.New")
    assert check_name(request, None) == [
        "A schema's name cannot change after creation (was 'com.acme.Old'). "
        "Create a new Schema instead."
    ]


def test_no_expected_name() -> None:
    assert check_name(req(), None) == []
