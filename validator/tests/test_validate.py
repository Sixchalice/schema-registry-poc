from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from schema_validator import (
    ReferenceInput,
    ValidationRequest,
    ValidationResult,
    ValidatorConfig,
    messages,
    validate,
)
from schema_validator.formats import ANALYSERS
from schema_validator.models import FormatAnalysis
from schema_validator.sr_client import (
    CompatibilityVerdict,
    SchemaRegistryError,
    SchemaRegistryRejected,
    SchemaRegistryUnauthorized,
    SchemaRegistryUnavailable,
    SrReference,
)
from tests.fake_sr import FakeSchemaRegistry
from tests.test_sr_messages import AVRO, INVALID, TYPE_CHANGE

SCHEMAS = Path(__file__).parent / "schemas"
FORMATS = ("AVRO", "JSON", "PROTOBUF")
_FILES = {"AVRO": ("avro", "avsc"), "JSON": ("json", "json"), "PROTOBUF": ("proto", "proto")}
# How each format writes the reference to com.acme.Address in customer.*.
ADDRESS_REF = {"AVRO": "com.acme.Address", "JSON": "address.json", "PROTOBUF": "com/acme/Address.proto"}

cfg = ValidatorConfig()

E3 = "Could not reach Schema Registry to validate. Try again."
E4 = "The validator is not allowed to query Schema Registry. Contact the platform team."
PINNED_ADDRESS = (
    "References will be pinned to: com.acme.Address v1. "
    "They stay on these versions until this schema is saved again."
)
D1_ADDRESS = "Referenced schema 'com.acme.Address' is not registered in Schema Registry yet."
D5_ADDRESS = "'com.acme.Address' is selected as a reference but is not used in the schema."


def load(schema_type: str, file: str) -> str:
    folder, ext = _FILES[schema_type]
    return (SCHEMAS / folder / f"{file}.{ext}").read_text()


def req(schema_type: str, file: str | None, name: str, /, **overrides: Any) -> ValidationRequest:
    fields: dict[str, Any] = {
        "name": name,
        "schema_type": schema_type,
        "schema": load(schema_type, file) if file is not None else "",
        "compatibility": "BACKWARD",
        "references": [],
        "previous_name": None,
        "existing_names": [],
    }
    fields.update(overrides)
    return ValidationRequest(**fields)


def address_row(schema_type: str) -> ReferenceInput:
    return ReferenceInput("com.acme.Address", "address.json" if schema_type == "JSON" else None)


def compat_calls(sr: FakeSchemaRegistry) -> list[tuple]:
    return [c for c in sr.calls if c[0] == "check_compatibility"]


def call_names(sr: FakeSchemaRegistry) -> list[str]:
    return [c[0] for c in sr.calls]


def raiser(error: BaseException) -> Callable[..., Any]:
    def fail(*args: Any, **kwargs: Any) -> Any:
        raise error
    return fail


@pytest.fixture
def sr() -> FakeSchemaRegistry:
    return FakeSchemaRegistry()


@pytest.fixture(scope="module")
def produced() -> list[ValidationResult]:
    """Every result `run` returned in this module."""
    return []


@pytest.fixture
def run(produced: list[ValidationResult]) -> Callable[[ValidationRequest, Any], ValidationResult]:
    def call(request: ValidationRequest, sr: Any) -> ValidationResult:
        result = validate(request, sr, cfg)
        produced.append(result)
        return result
    return call


# --- Happy path and stage A ---------------------------------------------------------


@pytest.mark.parametrize("schema_type", FORMATS)
def test_ok(schema_type: str, sr: FakeSchemaRegistry, run: Callable) -> None:
    request = req(schema_type, "order", "com.acme.Order")
    assert run(request, sr) == ValidationResult(True, "Ok", "")
    assert compat_calls(sr) == [("check_compatibility", "com.acme.Order", request.schema, schema_type, ())]


def test_fatal_stops_early(sr: FakeSchemaRegistry, run: Callable) -> None:
    result = run(req("AVRO", "order", "com.acme.Order", schema_type="XML"), sr)
    assert result == ValidationResult(False, "Error", messages.UNSUPPORTED_FORMAT.format(x="XML"))
    assert sr.calls == []


def test_fatal_keeps_every_stage_a_error(sr: FakeSchemaRegistry, run: Callable) -> None:
    request = req(
        "AVRO", None, " bad", schema_type="XML", compatibility="SIDEWAYS",
        references=[ReferenceInput("x"), ReferenceInput("x")],
    )
    result = run(request, sr)
    assert result.message.split("\n") == [
        messages.UNSUPPORTED_FORMAT.format(x="XML"),
        messages.SCHEMA_EMPTY,
        messages.UNKNOWN_COMPATIBILITY.format(x="SIDEWAYS"),
        messages.NAME_INVALID,
        messages.REFERENCE_DUPLICATE.format(x="x"),
    ]
    assert sr.calls == []


# --- Stage C --------------------------------------------------------------------------


@pytest.mark.parametrize("schema_type", FORMATS)
def test_name_mismatch_reports_expected_name(schema_type: str, sr: FakeSchemaRegistry, run: Callable) -> None:
    result = run(req(schema_type, "order", "com.acme.Wrong"), sr)
    assert result == ValidationResult(
        False, "Error", "The name must match the schema. It should be 'com.acme.Order'."
    )
    assert compat_calls(sr) == []


def test_avro_empty_name_part_is_not_passed(sr: FakeSchemaRegistry, run: Callable) -> None:
    schema = json.dumps({"type": "record", "name": ".Order", "fields": [{"name": "id", "type": "string"}]})
    result = run(req("AVRO", None, ".Order", schema=schema), sr)
    assert result == ValidationResult(False, "Error", messages.AVRO_NAMESPACE)
    assert compat_calls(sr) == []


def test_duplicate_name(sr: FakeSchemaRegistry, run: Callable) -> None:
    result = run(req("AVRO", "order", "com.acme.Order", existing_names=["com.acme.order"]), sr)
    assert result == ValidationResult(False, "Error", messages.NAME_EXISTS.format(name="com.acme.Order"))


def test_fullname_changed_on_edit(sr: FakeSchemaRegistry, run: Callable) -> None:
    result = run(req("AVRO", "order", "com.acme.Old", previous_name="com.acme.Old"), sr)
    assert result == ValidationResult(False, "Error", messages.FULLNAME_CHANGED.format(
        previous_name="com.acme.Old", expected_name="com.acme.Order"))


# --- Stage D --------------------------------------------------------------------------


@pytest.mark.parametrize("schema_type", FORMATS)
def test_references_happy(schema_type: str, sr: FakeSchemaRegistry, run: Callable) -> None:
    sr.add("com.acme.Address", load(schema_type, "address"), schema_type)
    request = req(schema_type, "customer", "com.acme.Customer", references=[address_row(schema_type)])
    assert run(request, sr) == ValidationResult(True, "Warning", PINNED_ADDRESS)
    assert compat_calls(sr) == [(
        "check_compatibility", "com.acme.Customer", request.schema, schema_type,
        (SrReference(ADDRESS_REF[schema_type], "com.acme.Address", 1),),
    )]


@pytest.mark.parametrize("schema_type", FORMATS)
def test_missing_row(schema_type: str, sr: FakeSchemaRegistry, run: Callable) -> None:
    result = run(req(schema_type, "customer", "com.acme.Customer"), sr)
    assert result.validationType == "Error"
    assert result.message == messages.REF_MISSING_ROW.format(x=ADDRESS_REF[schema_type])
    assert result.message.endswith("but no reference is selected for it.")
    assert "Invalid" not in result.message
    assert compat_calls(sr) == []


def test_unused_row(sr: FakeSchemaRegistry, run: Callable) -> None:
    sr.add("com.acme.Address", load("AVRO", "address"))
    result = run(req("AVRO", "order", "com.acme.Order", references=[address_row("AVRO")]), sr)
    assert result == ValidationResult(False, "Error", D5_ADDRESS)
    assert compat_calls(sr) == []


def test_reference_not_registered(sr: FakeSchemaRegistry, run: Callable) -> None:
    result = run(req("AVRO", "customer", "com.acme.Customer", references=[address_row("AVRO")]), sr)
    assert result == ValidationResult(False, "Error", D1_ADDRESS)


def test_reference_wrong_format(sr: FakeSchemaRegistry, run: Callable) -> None:
    sr.add("com.acme.Address", load("JSON", "address"), "JSON")
    result = run(req("AVRO", "customer", "com.acme.Customer", references=[address_row("AVRO")]), sr)
    assert result.validationType == "Error"
    assert messages.REF_WRONG_FORMAT.format(
        s="com.acme.Address", their_type="JSON", my_type="AVRO") in result.message.split("\n")


def test_reference_cycle(sr: FakeSchemaRegistry, run: Callable) -> None:
    sr.add("com.acme.Address", load("AVRO", "address"), "AVRO",
           [SrReference("com.acme.Customer", "com.acme.Customer", 1)])
    result = run(req("AVRO", "customer", "com.acme.Customer", references=[address_row("AVRO")]), sr)
    assert result.validationType == "Error"
    assert "Reference cycle: 'com.acme.Address' already depends on 'com.acme.Customer'." in result.message
    assert compat_calls(sr) == []


def test_json_row_without_ref_string(sr: FakeSchemaRegistry, run: Callable) -> None:
    sr.add("com.acme.Address", load("JSON", "address"), "JSON")
    request = req("JSON", "customer", "com.acme.Customer", references=[ReferenceInput("com.acme.Address")])
    result = run(request, sr)
    assert result.validationType == "Error"
    assert messages.REF_STRING_REQUIRED.format(s="com.acme.Address") in result.message.split("\n")


def test_json_row_with_empty_ref_string(sr: FakeSchemaRegistry, run: Callable) -> None:
    sr.add("com.acme.Address", load("JSON", "address"), "JSON")
    request = req("JSON", "customer", "com.acme.Customer", references=[ReferenceInput("com.acme.Address", "")])
    result = run(request, sr)
    assert result.validationType == "Error"
    lines = result.message.split("\n")
    assert messages.REF_STRING_REQUIRED.format(s="com.acme.Address") in lines
    assert messages.REF_UNUSED.format(x="") not in lines
    assert compat_calls(sr) == []


def test_protobuf_wrong_import_path(sr: FakeSchemaRegistry, run: Callable) -> None:
    sr.add("com.acme.Address", load("PROTOBUF", "address"), "PROTOBUF")
    schema = load("PROTOBUF", "customer").replace('"com/acme/Address.proto"', '"acme/address.proto"')
    request = req("PROTOBUF", None, "com.acme.Customer", schema=schema,
                  references=[address_row("PROTOBUF")])
    result = run(request, sr)
    assert result.validationType == "Error"
    assert 'Write the import as: import "com/acme/Address.proto";' in result.message
    assert compat_calls(sr) == []


def test_protobuf_syntax_error_has_no_d5(sr: FakeSchemaRegistry, run: Callable) -> None:
    # The import line without its ";": protoc fails, so the import is not taken as used.
    sr.add("com.acme.Address", load("PROTOBUF", "address"), "PROTOBUF")
    schema = load("PROTOBUF", "customer").replace('import "com/acme/Address.proto";', 'import "com/acme/Address.proto"')
    assert schema != load("PROTOBUF", "customer")
    request = req("PROTOBUF", None, "com.acme.Customer", schema=schema, references=[address_row("PROTOBUF")])
    result = run(request, sr)
    assert result.validationType == "Error"
    assert len(result.message.split("\n")) == 1
    assert result.message.startswith("Invalid Protobuf schema: ")
    assert compat_calls(sr) == []


def test_protobuf_transitive(sr: FakeSchemaRegistry, run: Callable) -> None:
    sr.add("com.acme.Address", load("PROTOBUF", "address"), "PROTOBUF")
    mid = (
        'syntax = "proto3";\npackage com.acme;\nimport "com/acme/Address.proto";\n'
        "message Mid {\n  com.acme.Address address = 1;\n}\n"
    )
    sr.add("com.acme.Mid", mid, "PROTOBUF", [SrReference("com/acme/Address.proto", "com.acme.Address", 1)])
    schema = (
        'syntax = "proto3";\npackage com.acme;\nimport "com/acme/Mid.proto";\n'
        "message Top {\n  com.acme.Mid mid = 1;\n}\n"
    )
    request = req("PROTOBUF", None, "com.acme.Top", schema=schema, references=[ReferenceInput("com.acme.Mid")])
    result = run(request, sr)
    assert result.validationType == "Warning", result.message


def test_avro_transitive(sr: FakeSchemaRegistry, run: Callable) -> None:
    sr.add("com.acme.Address", load("AVRO", "address"))
    mid = json.dumps({"type": "record", "name": "Mid", "namespace": "com.acme",
                      "fields": [{"name": "address", "type": "com.acme.Address"}]})
    sr.add("com.acme.Mid", mid, "AVRO", [SrReference("com.acme.Address", "com.acme.Address", 1)])
    schema = json.dumps({"type": "record", "name": "Top", "namespace": "com.acme",
                         "fields": [{"name": "mid", "type": "com.acme.Mid"}]})
    request = req("AVRO", None, "com.acme.Top", schema=schema, references=[ReferenceInput("com.acme.Mid")])
    result = run(request, sr)
    assert result.validationType == "Warning", result.message


ADDRESS_WITH_COUNTRY = json.dumps({"type": "record", "name": "Address", "namespace": "com.acme", "fields": [
    {"name": "street", "type": "string"},
    {"name": "country", "type": {"type": "enum", "name": "Country", "symbols": ["NL", "US"]}}]})


@pytest.mark.parametrize("fields", [
    pytest.param([{"name": "address", "type": "com.acme.Address"}, {"name": "country", "type": "com.acme.Country"}],
                 id="both"),
    pytest.param([{"name": "country", "type": "com.acme.Country"}], id="nested-only"),
])
def test_avro_name_nested_in_a_reference(fields: list[dict[str, Any]], sr: FakeSchemaRegistry, run: Callable) -> None:
    sr.add("com.acme.Address", ADDRESS_WITH_COUNTRY)
    schema = json.dumps({"type": "record", "name": "Customer", "namespace": "com.acme", "fields": fields})
    request = req("AVRO", None, "com.acme.Customer", schema=schema, references=[address_row("AVRO")])
    assert run(request, sr) == ValidationResult(True, "Warning", PINNED_ADDRESS)
    assert compat_calls(sr) == [("check_compatibility", "com.acme.Customer", schema, "AVRO",
                                 (SrReference("com.acme.Address", "com.acme.Address", 1),))]


def test_avro_nested_null_namespace_type_is_an_error(sr: FakeSchemaRegistry, run: Callable) -> None:
    # fastavro stops at the bare use of Inner and never sees the bad default: this must not pass.
    schema = json.dumps({"type": "record", "name": "Order", "namespace": "com.acme", "fields": [
        {"name": "inner", "type": {"type": "record", "name": "Inner", "namespace": "", "fields": []}},
        {"name": "again", "type": "Inner"},
        {"name": "bad", "type": "string", "default": 5}]})
    result = run(req("AVRO", None, "com.acme.Order", schema=schema), sr)
    assert (result.valid, result.validationType) == (False, "Error")
    assert messages.AVRO_NESTED_NAMESPACE.format(name="Inner") in result.message
    assert compat_calls(sr) == []


def test_avro_null_namespace_type_nested_in_a_reference_is_an_error(sr: FakeSchemaRegistry, run: Callable) -> None:
    sr.add("com.acme.Address", json.dumps({"type": "record", "name": "Address", "namespace": "com.acme", "fields": [
        {"name": "country", "type": {"type": "enum", "name": "Country", "namespace": "", "symbols": ["NL"]}}]}))
    schema = json.dumps({"type": "record", "name": "Customer", "namespace": "com.acme", "fields": [
        {"name": "address", "type": "com.acme.Address"},
        {"name": "country", "type": "Country"},
        {"name": "bad", "type": "string", "default": 5}]})
    request = req("AVRO", None, "com.acme.Customer", schema=schema, references=[address_row("AVRO")])
    result = run(request, sr)
    assert result == ValidationResult(False, "Error", messages.REF_MISSING_ROW.format(x="com.acme.Country"))
    assert compat_calls(sr) == []


def test_duplicate_rows_skip_stage_d(sr: FakeSchemaRegistry, run: Callable) -> None:
    # order.avsc uses neither row: a D5 message would show that the cross-check ran.
    sr.add("com.acme.Address", load("AVRO", "address"))
    rows = [ReferenceInput("com.acme.Address"), ReferenceInput("com.acme.Address")]
    result = run(req("AVRO", "order", "com.acme.Order", references=rows), sr)
    assert result == ValidationResult(False, "Error", messages.REFERENCE_DUPLICATE.format(x="com.acme.Address"))
    assert "get_latest" not in call_names(sr)


def test_duplicate_rows_skip_row_normalisation(sr: FakeSchemaRegistry, run: Callable) -> None:
    # JSON rows without a ref_string would get D6 if the rows were normalised.
    rows = [ReferenceInput("com.acme.Address"), ReferenceInput("com.acme.Address")]
    result = run(req("JSON", "order", "com.acme.Order", references=rows), sr)
    assert result == ValidationResult(False, "Error", messages.REFERENCE_DUPLICATE.format(x="com.acme.Address"))
    assert sr.calls == []


def test_partial_resolution_skips_stage_e_and_warnings(sr: FakeSchemaRegistry, run: Callable) -> None:
    # One row resolves, the other does not: `referenced` is partial and must not reach SR.
    sr.add("com.acme.Address", load("AVRO", "address"))
    schema = json.dumps({"type": "record", "name": "Customer", "namespace": "com.acme", "fields": [
        {"name": "address", "type": "com.acme.Address"}, {"name": "money", "type": "com.acme.Money"}]})
    rows = [ReferenceInput("com.acme.Address"), ReferenceInput("com.acme.Money")]
    request = req("AVRO", None, "com.acme.Customer", schema=schema, references=rows,
                  previous_name="com.acme.Customer", compatibility="FULL")
    result = run(request, sr)
    assert result == ValidationResult(
        False, "Error", messages.REF_NOT_REGISTERED.format(s="com.acme.Money"))
    assert compat_calls(sr) == []
    assert "get_compatibility_level" not in call_names(sr)


# --- Stage E --------------------------------------------------------------------------


def test_incompatible(sr: FakeSchemaRegistry, run: Callable) -> None:
    sr.verdict = CompatibilityVerdict(False, tuple(AVRO))
    result = run(req("AVRO", "order", "com.acme.Order"), sr)
    assert result == ValidationResult(False, "Error", (
        "Not BACKWARD-compatible with version 1: The field 'zip' at path '/fields/1' "
        "in the new schema has no default value and is missing in the old schema"))


def test_incompatible_without_version(sr: FakeSchemaRegistry, run: Callable) -> None:
    sr.verdict = CompatibilityVerdict(False, tuple(TYPE_CHANGE))
    result = run(req("AVRO", "order", "com.acme.Order"), sr)
    assert result.message == (
        "Not BACKWARD-compatible with the existing versions: Incompatible because of different schema type")


def test_incompatible_uses_request_level_when_sr_gives_none(sr: FakeSchemaRegistry, run: Callable) -> None:
    sr.verdict = CompatibilityVerdict(False, ("Incompatible because of different schema type",))
    result = run(req("AVRO", "order", "com.acme.Order", compatibility="FULL"), sr)
    assert result.message == (
        "Not FULL-compatible with the existing versions: Incompatible because of different schema type")


def test_incompatible_without_descriptions_with_version(sr: FakeSchemaRegistry, run: Callable) -> None:
    sr.verdict = CompatibilityVerdict(False, ("{oldSchemaVersion: 2}", AVRO[3]))
    result = run(req("AVRO", "order", "com.acme.Order"), sr)
    assert result == ValidationResult(False, "Error", "Not BACKWARD-compatible with version 2")


def test_incompatible_without_descriptions_without_version(sr: FakeSchemaRegistry, run: Callable) -> None:
    sr.verdict = CompatibilityVerdict(False, (AVRO[3],))
    result = run(req("AVRO", "order", "com.acme.Order"), sr)
    assert result == ValidationResult(False, "Error", "Not BACKWARD-compatible with the existing versions")


def test_incompatible_without_any_message(sr: FakeSchemaRegistry, run: Callable) -> None:
    sr.verdict = CompatibilityVerdict(False, ())
    result = run(req("AVRO", "order", "com.acme.Order", compatibility="FORWARD"), sr)
    assert result == ValidationResult(False, "Error", "Not FORWARD-compatible with the existing versions")


def test_sr_says_invalid(sr: FakeSchemaRegistry, run: Callable) -> None:
    sr.verdict = CompatibilityVerdict(False, (INVALID,))
    result = run(req("AVRO", "order", "com.acme.Order"), sr)
    assert result == ValidationResult(
        False, "Error", 'Schema Registry rejected the schema: "strin" is not a defined name.')


def test_sr_says_invalid_takes_the_first_invalid_message(sr: FakeSchemaRegistry, run: Callable) -> None:
    sr.verdict = CompatibilityVerdict(False, (AVRO[0], "Invalid schema x, details: first",
                                              "Invalid schema y, details: second"))
    result = run(req("AVRO", "order", "com.acme.Order"), sr)
    assert result.message == "Schema Registry rejected the schema: first"


@pytest.mark.parametrize("message", [
    'Invalid schema {subject=com.acme.Order,schema={"type":"record"}} with refs [] of type AVRO',
    'Invalid schema {subject=com.acme.Order,schema={"type":"record"}} with refs [] of type AVRO, details: ',
])
def test_sr_says_invalid_without_details(message: str, sr: FakeSchemaRegistry, run: Callable) -> None:
    sr.verdict = CompatibilityVerdict(False, (message,))
    result = run(req("AVRO", "order", "com.acme.Order"), sr)
    assert result == ValidationResult(False, "Error", messages.SR_REJECTED_NO_REASON)
    assert "{subject=" not in result.message


@pytest.mark.parametrize("message", [
    'Invalid schema {subject=com.acme.Order,schema={"type":"record"}}',
    'Invalid schema {subject=com.acme.Order,schema={"type":"record"}}, details:',
])
def test_sr_rejected_exception_without_details(message: str, sr: FakeSchemaRegistry, run: Callable) -> None:
    sr.fail_with = SchemaRegistryRejected(message)
    result = run(req("AVRO", "order", "com.acme.Order"), sr)
    assert result == ValidationResult(False, "Error", messages.SR_REJECTED_NO_REASON)
    assert "{subject=" not in result.message


def test_sr_rejected_exception(sr: FakeSchemaRegistry, run: Callable) -> None:
    sr.fail_with = SchemaRegistryRejected("Invalid schema {..}, details: boom")
    result = run(req("AVRO", "order", "com.acme.Order"), sr)
    assert result == ValidationResult(False, "Error", "Schema Registry rejected the schema: boom")


def test_sr_rejected_exception_without_invalid_prefix(sr: FakeSchemaRegistry, run: Callable) -> None:
    sr.fail_with = SchemaRegistryRejected("Subject name is too long")
    result = run(req("AVRO", "order", "com.acme.Order"), sr)
    assert result.message == "Schema Registry rejected the schema: Subject name is too long"


def test_sr_unreachable(sr: FakeSchemaRegistry, run: Callable) -> None:
    sr.fail_with = SchemaRegistryUnavailable("x")
    result = run(req("AVRO", "order", "com.acme.Order"), sr)
    assert result == ValidationResult(False, "Error", E3)


def test_sr_unreachable_during_references(sr: FakeSchemaRegistry, run: Callable) -> None:
    sr.fail_with = SchemaRegistryUnavailable("x")
    result = run(req("AVRO", "customer", "com.acme.Customer", references=[address_row("AVRO")]), sr)
    assert result.validationType == "Error"
    assert result.message.count(E3) == 1
    assert D1_ADDRESS not in result.message
    assert compat_calls(sr) == []


def test_sr_failure_during_references_still_runs_stages_b_to_d(sr: FakeSchemaRegistry, run: Callable) -> None:
    sr.fail_with = SchemaRegistryUnavailable("x")
    request = req("AVRO", "order", "com.acme.Wrong", references=[address_row("AVRO")])
    result = run(request, sr)
    assert result.message.split("\n") == [
        messages.NAME_MISMATCH.format(expected_name="com.acme.Order"), D5_ADDRESS, E3]


@pytest.mark.parametrize("error, expected", [
    (SchemaRegistryUnauthorized("x"), E4),
    (SchemaRegistryRejected("Invalid schema {..}, details: no such subject"),
     "Schema Registry rejected the schema: no such subject"),
    (SchemaRegistryRejected("plain"), "Schema Registry rejected the schema: plain"),
])
def test_sr_failure_during_references_maps_like_stage_e(
    error: Exception, expected: str, sr: FakeSchemaRegistry, run: Callable
) -> None:
    sr.fail_with = error
    result = run(req("AVRO", "customer", "com.acme.Customer", references=[address_row("AVRO")]), sr)
    assert result == ValidationResult(False, "Error", expected)
    assert compat_calls(sr) == []


def test_sr_unauthorized(sr: FakeSchemaRegistry, run: Callable) -> None:
    sr.fail_with = SchemaRegistryUnauthorized("x")
    result = run(req("AVRO", "order", "com.acme.Order"), sr)
    assert result == ValidationResult(False, "Error", E4)


# --- Warnings -------------------------------------------------------------------------


def test_level_changed_on_edit(sr: FakeSchemaRegistry, run: Callable) -> None:
    sr.levels["com.acme.Order"] = "BACKWARD"
    request = req("AVRO", "order", "com.acme.Order", previous_name="com.acme.Order", compatibility="FULL")
    assert run(request, sr) == ValidationResult(True, "Warning", (
        "Compatibility was checked using the current level 'BACKWARD', not the new level 'FULL'."))
    assert ("get_compatibility_level", "com.acme.Order") in sr.calls


def test_level_unchanged_on_edit(sr: FakeSchemaRegistry, run: Callable) -> None:
    sr.levels["com.acme.Order"] = "FULL"
    request = req("AVRO", "order", "com.acme.Order", previous_name="com.acme.Order", compatibility="FULL")
    assert run(request, sr) == ValidationResult(True, "Ok", "")


def test_no_level_call_on_create(sr: FakeSchemaRegistry, run: Callable) -> None:
    run(req("AVRO", "order", "com.acme.Order", compatibility="FULL"), sr)
    assert "get_compatibility_level" not in call_names(sr)


def test_pinned_and_level_warnings_together(sr: FakeSchemaRegistry, run: Callable) -> None:
    sr.add("com.acme.Address", load("AVRO", "address"))
    sr.levels["com.acme.Customer"] = "BACKWARD"
    request = req("AVRO", "customer", "com.acme.Customer", references=[address_row("AVRO")],
                  previous_name="com.acme.Customer", compatibility="FULL")
    result = run(request, sr)
    assert result == ValidationResult(True, "Warning", PINNED_ADDRESS + "\n" + (
        "Compatibility was checked using the current level 'BACKWARD', not the new level 'FULL'."))


def test_no_level_call_when_there_are_errors(sr: FakeSchemaRegistry, run: Callable) -> None:
    sr.verdict = CompatibilityVerdict(False, tuple(AVRO))
    request = req("AVRO", "order", "com.acme.Order", previous_name="com.acme.Order", compatibility="FULL")
    assert run(request, sr).validationType == "Error"
    assert "get_compatibility_level" not in call_names(sr)


@pytest.mark.parametrize("error, expected", [
    (SchemaRegistryUnavailable("x"), E3),
    (SchemaRegistryUnauthorized("x"), E4),
    (SchemaRegistryRejected("Invalid schema {..}, details: bad subject"),
     "Schema Registry rejected the schema: bad subject"),
])
def test_level_lookup_failure_is_an_error(
    error: Exception, expected: str, sr: FakeSchemaRegistry, run: Callable, monkeypatch: pytest.MonkeyPatch
) -> None:
    sr.add("com.acme.Address", load("AVRO", "address"))
    monkeypatch.setattr(sr, "get_compatibility_level", raiser(error))
    request = req("AVRO", "customer", "com.acme.Customer", references=[address_row("AVRO")],
                  previous_name="com.acme.Customer")
    assert run(request, sr) == ValidationResult(False, "Error", expected)


# --- Result assembly and robustness ---------------------------------------------------


def test_errors_joined_in_stage_order(sr: FakeSchemaRegistry, run: Callable) -> None:
    sr.add("com.acme.Address", load("AVRO", "address"))
    request = req("AVRO", "order", "com.acme.Wrong", compatibility="SIDEWAYS",
                  references=[address_row("AVRO")])
    result = run(request, sr)
    assert result.message.split("\n") == [
        messages.UNKNOWN_COMPATIBILITY.format(x="SIDEWAYS"),
        messages.NAME_MISMATCH.format(expected_name="com.acme.Order"),
        D5_ADDRESS,
    ]


def test_local_errors_skip_sr_verdict(sr: FakeSchemaRegistry, run: Callable) -> None:
    schema = json.dumps({"type": "record", "name": "Order", "namespace": "com.acme",
                         "fields": [{"name": "id", "type": "string", "default": 5}]})
    result = run(req("AVRO", None, "com.acme.Order", schema=schema), sr)
    assert result.validationType == "Error"
    assert result.message.startswith("Invalid Avro schema: ")
    assert compat_calls(sr) == []


def test_never_raises(sr: FakeSchemaRegistry, run: Callable, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(ANALYSERS, "AVRO", raiser(RuntimeError("boom")))
    assert run(req("AVRO", "order", "com.acme.Order"), sr) == ValidationResult(
        False, "Error", "Internal validator error: boom")


def test_recursion_error_does_not_raise(
    sr: FakeSchemaRegistry, run: Callable, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(ANALYSERS, "JSON", raiser(RecursionError("too deep")))
    assert run(req("JSON", "order", "com.acme.Order"), sr) == ValidationResult(
        False, "Error", "Internal validator error: too deep")


def test_deeply_nested_schema_does_not_raise(sr: FakeSchemaRegistry, run: Callable) -> None:
    deep = '{"type": "object", "title": "com.acme.Order", "properties": {"a": ' * 5000
    schema = deep + '{"type": "string"}' + "}}" * 5000
    for schema_type in FORMATS:
        result = run(req(schema_type, None, "com.acme.Order", schema=schema), sr)
        assert result.validationType == "Error"


@pytest.mark.parametrize("error", [ValueError("odd"), SchemaRegistryError("odd")])
def test_unexpected_registry_exception_is_internal(
    error: Exception, sr: FakeSchemaRegistry, run: Callable
) -> None:
    sr.fail_with = error
    assert run(req("AVRO", "order", "com.acme.Order"), sr) == ValidationResult(
        False, "Error", "Internal validator error: odd")


def test_unexpected_exception_from_resolve_is_internal(sr: FakeSchemaRegistry, run: Callable) -> None:
    sr.fail_with = KeyError("odd")
    result = run(req("AVRO", "customer", "com.acme.Customer", references=[address_row("AVRO")]), sr)
    assert result == ValidationResult(False, "Error", "Internal validator error: 'odd'")


def test_wrong_input_types_do_not_raise(sr: FakeSchemaRegistry, run: Callable) -> None:
    for request in (req("AVRO", "order", "com.acme.Order", schema=None),
                    req("AVRO", "order", "com.acme.Order", references=None)):
        result = run(request, sr)
        assert result.valid is False
        assert result.validationType == "Error"


def test_failing_request_field_does_not_raise(sr: FakeSchemaRegistry, run: Callable) -> None:
    class Broken:
        name = "com.acme.Order"
        schema_type = "AVRO"

        @property
        def schema(self) -> str:
            raise RuntimeError("cannot read")

    assert run(Broken(), sr) == ValidationResult(False, "Error", "Internal validator error: cannot read")


def test_exception_with_failing_str_does_not_raise(
    sr: FakeSchemaRegistry, run: Callable, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Unprintable(Exception):
        def __str__(self) -> str:
            raise RuntimeError("no text")

    monkeypatch.setitem(ANALYSERS, "AVRO", raiser(Unprintable()))
    result = run(req("AVRO", "order", "com.acme.Order"), sr)
    assert result == ValidationResult(False, "Error", "Internal validator error: Unprintable")


@pytest.mark.parametrize("analysis, rows", [
    (FormatAnalysis(None, set(), []), []),
    # The shape the Protobuf analyser returns for a missing import of the schema itself.
    (FormatAnalysis(None, {"com/acme/Address.proto"}, []), [address_row("PROTOBUF")]),
    (FormatAnalysis("com.acme.Order", None, []), []),
])
def test_unanalysed_schema_without_errors_is_not_passed(
    analysis: FormatAnalysis, rows: list[ReferenceInput], sr: FakeSchemaRegistry, run: Callable,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sr.add("com.acme.Address", load("PROTOBUF", "address"), "PROTOBUF")
    monkeypatch.setitem(ANALYSERS, "PROTOBUF", lambda *args: analysis)
    request = req("PROTOBUF", "customer", "com.acme.Order", references=rows)
    result = run(request, sr)
    assert result.validationType == "Error"
    assert result.message.startswith("Internal validator error: ")
    assert compat_calls(sr) == []


def test_analysers_cover_every_format() -> None:
    assert set(ANALYSERS) == set(FORMATS)


# Keep last: checks every result the tests above produced, and three of its own.
def test_result_invariant(sr: FakeSchemaRegistry, run: Callable, produced: list[ValidationResult]) -> None:
    sr.add("com.acme.Address", load("AVRO", "address"))
    run(req("AVRO", "order", "com.acme.Order"), sr)
    run(req("AVRO", "customer", "com.acme.Customer", references=[address_row("AVRO")]), sr)
    run(req("AVRO", "order", "com.acme.Wrong"), sr)
    assert {r.validationType for r in produced} == {"Ok", "Warning", "Error"}
    for result in produced:
        assert result.valid == (result.validationType != "Error"), result
        assert (result.message == "") == (result.validationType == "Ok"), result
