from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

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
    result = check_input(req(references=refs), CONFIG)
    assert result.errors == ["Reference 'x.json' is listed twice."]
    assert result.references_ok is False


def test_self_reference() -> None:
    result = check_input(req(references=[ReferenceInput("com.acme.Order")]), CONFIG)
    assert result.errors == ["A schema cannot reference itself."]
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


def test_no_expected_name() -> None:
    assert check_name(req(), None) == []
