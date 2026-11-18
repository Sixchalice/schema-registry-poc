from __future__ import annotations

import json
import socket
import threading
import time
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
    HttpSchemaRegistryClient,
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
PINNED_ADDRESS = "References will be pinned to: com.acme.Address v1."
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
    return ReferenceInput("com.acme.Address", "address.json" if schema_type == "JSON" else None, 1)


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
        references=[ReferenceInput("x", None, 1), ReferenceInput("x", None, 1)],
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


def test_version_conflict_is_an_error(sr: FakeSchemaRegistry, run: Callable) -> None:
    money = lambda extra: json.dumps({"type": "record", "name": "Money", "namespace": "a", "fields": [
        {"name": "amount", "type": "long"}] + extra})
    sr.add("a.Money", money([]))
    sr.add("a.Money", money([{"name": "currency", "type": "string", "default": "EUR"}]))
    sr.add("a.Line", json.dumps({"type": "record", "name": "Line", "namespace": "a",
                                 "fields": [{"name": "price", "type": "a.Money"}]}),
           "AVRO", [SrReference("a.Money", "a.Money", 1)])
    schema = json.dumps({"type": "record", "name": "Order", "namespace": "a", "fields": [
        {"name": "line", "type": "a.Line"}, {"name": "total", "type": "a.Money"}]})
    result = run(req("AVRO", None, "a.Order", schema=schema,
                     references=[ReferenceInput("a.Line", None, 1), ReferenceInput("a.Money", None, 2)]), sr)
    assert not result.valid
    assert result.validationType == "Error"
    assert result.message == messages.REF_VERSION_CONFLICT_DIRECT.format(
        s="a.Money", uses="v1 through 'a.Line', v2 directly", chosen=2)
    assert compat_calls(sr) == []


def test_compatibility_check_gets_the_chosen_version(sr: FakeSchemaRegistry, run: Callable) -> None:
    sr.add("com.acme.Address", load("AVRO", "address"))
    sr.add("com.acme.Address", load("AVRO", "address"))
    request = req("AVRO", "customer", "com.acme.Customer", references=[address_row("AVRO")])
    result = run(request, sr)
    assert result == ValidationResult(
        True, "Warning",
        "References will be pinned to: com.acme.Address v1 (latest is v2). "
        "To update one, select a newer version for it.")
    assert compat_calls(sr)[0][4] == (SrReference("com.acme.Address", "com.acme.Address", 1),)


def test_avro_custom_ref_string_rejected(sr: FakeSchemaRegistry, run: Callable) -> None:
    sr.add("a.Address", json.dumps({"type": "record", "name": "Address", "namespace": "a",
                                    "fields": [{"name": "street", "type": "string"}]}))
    schema = json.dumps({"type": "record", "name": "Order", "namespace": "a", "fields": [
        {"name": "to", "type": "a.Address"}]})
    result = run(req("AVRO", None, "a.Order", schema=schema,
                     references=[ReferenceInput("a.Address", "address.avsc", 1)]), sr)
    assert result == ValidationResult(False, "Error", messages.AVRO_REF_STRING.format(s="a.Address"))
    assert compat_calls(sr) == []


def test_json_row_without_ref_string(sr: FakeSchemaRegistry, run: Callable) -> None:
    sr.add("com.acme.Address", load("JSON", "address"), "JSON")
    request = req("JSON", "customer", "com.acme.Customer", references=[ReferenceInput("com.acme.Address", None, 1)])
    result = run(request, sr)
    assert result.validationType == "Error"
    assert messages.REF_STRING_REQUIRED.format(s="com.acme.Address") in result.message.split("\n")


def test_json_row_with_empty_ref_string(sr: FakeSchemaRegistry, run: Callable) -> None:
    sr.add("com.acme.Address", load("JSON", "address"), "JSON")
    request = req("JSON", "customer", "com.acme.Customer", references=[ReferenceInput("com.acme.Address", "", 1)])
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
    request = req("PROTOBUF", None, "com.acme.Top", schema=schema, references=[ReferenceInput("com.acme.Mid", None, 1)])
    result = run(request, sr)
    assert result.validationType == "Warning", result.message


def test_avro_transitive(sr: FakeSchemaRegistry, run: Callable) -> None:
    sr.add("com.acme.Address", load("AVRO", "address"))
    mid = json.dumps({"type": "record", "name": "Mid", "namespace": "com.acme",
                      "fields": [{"name": "address", "type": "com.acme.Address"}]})
    sr.add("com.acme.Mid", mid, "AVRO", [SrReference("com.acme.Address", "com.acme.Address", 1)])
    schema = json.dumps({"type": "record", "name": "Top", "namespace": "com.acme",
                         "fields": [{"name": "mid", "type": "com.acme.Mid"}]})
    request = req("AVRO", None, "com.acme.Top", schema=schema, references=[ReferenceInput("com.acme.Mid", None, 1)])
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
    # One message: no D4 for Country and no D5 for the Address row.
    sr.add("com.acme.Address", ADDRESS_WITH_COUNTRY)
    schema = json.dumps({"type": "record", "name": "Customer", "namespace": "com.acme", "fields": fields})
    request = req("AVRO", None, "com.acme.Customer", schema=schema, references=[address_row("AVRO")])
    assert run(request, sr) == ValidationResult(
        False, "Error", messages.AVRO_NESTED_IN_REFERENCE.format(name="com.acme.Country", schema="com.acme.Address"))
    assert compat_calls(sr) == []


def test_avro_nested_null_namespace_type_is_an_error(sr: FakeSchemaRegistry, run: Callable) -> None:
    # fastavro stops at the bare use of Inner and never sees the bad default: this must not pass.
    schema = json.dumps({"type": "record", "name": "Order", "namespace": "com.acme", "fields": [
        {"name": "inner", "type": {"type": "record", "name": "Inner", "namespace": "", "fields": []}},
        {"name": "again", "type": "Inner"},
        {"name": "bad", "type": "string", "default": 5}]})
    result = run(req("AVRO", None, "com.acme.Order", schema=schema), sr)
    assert (result.valid, result.validationType) == (False, "Error")
    assert result == ValidationResult(False, "Error", messages.AVRO_NESTED_NAMESPACE.format(name="Inner"))
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


CUSTOMER_BARE_COUNTRY = json.dumps({"type": "record", "name": "Customer", "namespace": "com.acme", "fields": [
    {"name": "c", "type": "Country"},
    {"name": "bad", "type": "string", "default": 5}]})


@pytest.mark.parametrize("subject, text, row", [
    pytest.param(
        "com.acme.Country", json.dumps({"type": "enum", "name": "Country", "symbols": ["NL"]}),
        ReferenceInput("com.acme.Country", None, 1), id="subject-holds-a-null-namespace-type"),
])
def test_avro_row_that_does_not_define_its_name_is_an_error(
    subject: str, text: str, row: ReferenceInput, sr: FakeSchemaRegistry, run: Callable
) -> None:
    # The row's name matches the used type, so D4 is silent; but its text does not define
    # com.acme.Country, fastavro stops there and never sees the bad default.
    sr.add(subject, text)
    request = req("AVRO", None, "com.acme.Customer", schema=CUSTOMER_BARE_COUNTRY, references=[row])
    result = run(request, sr)
    assert result == ValidationResult(
        False, "Error",
        "Invalid Avro schema: type 'com.acme.Country' is not defined by the reference selected for it")
    assert compat_calls(sr) == []


def _transitive_avro(sr: FakeSchemaRegistry) -> None:
    """Address (the row) references Country, and Country defines a nested enum Code."""
    sr.add("com.acme.Country", json.dumps({"type": "record", "name": "Country", "namespace": "com.acme", "fields": [
        {"name": "code", "type": {"type": "enum", "name": "Code", "symbols": ["NL", "US"]}}]}))
    sr.add("com.acme.Address", json.dumps({"type": "record", "name": "Address", "namespace": "com.acme", "fields": [
        {"name": "country", "type": "com.acme.Country"}]}),
        "AVRO", [SrReference("com.acme.Country", "com.acme.Country", 1)])


@pytest.mark.parametrize("fields", [
    pytest.param([{"name": "c", "type": "com.acme.Country"}], id="transitive-schema-only"),
    pytest.param([{"name": "a", "type": "com.acme.Address"}, {"name": "c", "type": "com.acme.Country"}],
                 id="row-and-transitive-schema"),
    pytest.param([{"name": "a", "type": "Address"}, {"name": "c", "type": "Country"}], id="unqualified"),
])
def test_avro_name_from_a_transitive_reference_is_provided_by_the_row(
    fields: list[dict[str, Any]], sr: FakeSchemaRegistry, run: Callable
) -> None:
    # The registry loads Country together with Address, so the row for Address covers it:
    # no "no reference is selected" for a schema the user never chose, and the row is used.
    _transitive_avro(sr)
    schema = json.dumps({"type": "record", "name": "Customer", "namespace": "com.acme", "fields": fields})
    request = req("AVRO", None, "com.acme.Customer", schema=schema, references=[address_row("AVRO")])
    assert run(request, sr) == ValidationResult(True, "Warning", PINNED_ADDRESS)
    assert compat_calls(sr) == [("check_compatibility", "com.acme.Customer", schema, "AVRO",
                                 (SrReference("com.acme.Address", "com.acme.Address", 1),))]


@pytest.mark.parametrize("fields", [
    pytest.param([{"name": "code", "type": "com.acme.Code"}], id="qualified"),
    pytest.param([{"name": "a", "type": "Address"}, {"name": "code", "type": "Code"}], id="unqualified"),
])
def test_avro_name_nested_in_a_transitive_reference_cannot_be_used(
    fields: list[dict[str, Any]], sr: FakeSchemaRegistry, run: Callable
) -> None:
    # Code is nested in Country, which only Address references. The message names Country.
    _transitive_avro(sr)
    schema = json.dumps({"type": "record", "name": "Customer", "namespace": "com.acme", "fields": fields})
    request = req("AVRO", None, "com.acme.Customer", schema=schema, references=[address_row("AVRO")])
    assert run(request, sr) == ValidationResult(
        False, "Error", messages.AVRO_NESTED_IN_REFERENCE.format(name="com.acme.Code", schema="com.acme.Country"))
    assert compat_calls(sr) == []


def test_avro_transitive_name_with_its_own_row_uses_that_row(sr: FakeSchemaRegistry, run: Callable) -> None:
    # Both rows are selected and each is used by its own name: no D5 for either.
    _transitive_avro(sr)
    schema = json.dumps({"type": "record", "name": "Customer", "namespace": "com.acme", "fields": [
        {"name": "a", "type": "com.acme.Address"}, {"name": "c", "type": "com.acme.Country"}]})
    request = req("AVRO", None, "com.acme.Customer", schema=schema,
                  references=[address_row("AVRO"), ReferenceInput("com.acme.Country", None, 1)])
    result = run(request, sr)
    assert (result.valid, result.validationType) == (True, "Warning"), result.message


def test_avro_row_used_only_through_another_rows_transitive_name_is_unused(
    sr: FakeSchemaRegistry, run: Callable
) -> None:
    # Country has its own row, so Address (which only brings Country along) is not used.
    _transitive_avro(sr)
    schema = json.dumps({"type": "record", "name": "Customer", "namespace": "com.acme", "fields": [
        {"name": "c", "type": "com.acme.Country"}]})
    request = req("AVRO", None, "com.acme.Customer", schema=schema,
                  references=[address_row("AVRO"), ReferenceInput("com.acme.Country", None, 1)])
    assert run(request, sr) == ValidationResult(False, "Error", D5_ADDRESS)


def test_avro_name_not_in_any_reference_is_still_missing(sr: FakeSchemaRegistry, run: Callable) -> None:
    _transitive_avro(sr)
    schema = json.dumps({"type": "record", "name": "Customer", "namespace": "com.acme", "fields": [
        {"name": "a", "type": "com.acme.Address"}, {"name": "m", "type": "com.acme.Money"}]})
    request = req("AVRO", None, "com.acme.Customer", schema=schema, references=[address_row("AVRO")])
    assert run(request, sr) == ValidationResult(False, "Error", messages.REF_MISSING_ROW.format(x="com.acme.Money"))


def test_duplicate_rows_skip_stage_d(sr: FakeSchemaRegistry, run: Callable) -> None:
    # order.avsc uses neither row: a D5 message would show that the cross-check ran.
    sr.add("com.acme.Address", load("AVRO", "address"))
    rows = [ReferenceInput("com.acme.Address", None, 1), ReferenceInput("com.acme.Address", None, 1)]
    result = run(req("AVRO", "order", "com.acme.Order", references=rows), sr)
    assert result == ValidationResult(False, "Error", messages.REFERENCE_DUPLICATE.format(x="com.acme.Address"))
    assert "get_latest" not in call_names(sr)


def test_duplicate_rows_skip_row_normalisation(sr: FakeSchemaRegistry, run: Callable) -> None:
    # JSON rows without a ref_string would get D6 if the rows were normalised.
    rows = [ReferenceInput("com.acme.Address", None, 1), ReferenceInput("com.acme.Address", None, 1)]
    result = run(req("JSON", "order", "com.acme.Order", references=rows), sr)
    assert result == ValidationResult(False, "Error", messages.REFERENCE_DUPLICATE.format(x="com.acme.Address"))
    assert sr.calls == []


def test_partial_resolution_skips_stage_e_and_warnings(sr: FakeSchemaRegistry, run: Callable) -> None:
    # One row resolves, the other does not: `referenced` is partial and must not reach SR.
    sr.add("com.acme.Address", load("AVRO", "address"))
    schema = json.dumps({"type": "record", "name": "Customer", "namespace": "com.acme", "fields": [
        {"name": "address", "type": "com.acme.Address"}, {"name": "money", "type": "com.acme.Money"}]})
    rows = [ReferenceInput("com.acme.Address", None, 1), ReferenceInput("com.acme.Money", None, 1)]
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
    sr.fail_with = SchemaRegistryRejected(message, 42201)
    result = run(req("AVRO", "order", "com.acme.Order"), sr)
    assert result == ValidationResult(False, "Error", messages.SR_REJECTED_NO_REASON)
    assert "{subject=" not in result.message


def test_sr_rejected_exception(sr: FakeSchemaRegistry, run: Callable) -> None:
    sr.fail_with = SchemaRegistryRejected("Invalid schema {..}, details: boom", 42201)
    result = run(req("AVRO", "order", "com.acme.Order"), sr)
    assert result == ValidationResult(False, "Error", "Schema Registry rejected the schema: boom")


def test_text_not_in_registry_7_9_is_not_shown(sr: FakeSchemaRegistry, run: Callable) -> None:
    sr.fail_with = SchemaRegistryRejected("Either the input schema or one its references is invalid", 42201)
    result = run(req("AVRO", "order", "com.acme.Order"), sr)
    assert result.message == messages.SR_REJECTED_CODE.format(code=42201)


def test_sr_rejected_exception_without_invalid_prefix(sr: FakeSchemaRegistry, run: Callable) -> None:
    sr.fail_with = SchemaRegistryRejected("Subject name is too long", 42201)
    result = run(req("AVRO", "order", "com.acme.Order"), sr)
    assert result.message == messages.SR_REJECTED_CODE.format(code=42201)


def test_unknown_plain_message_is_not_shown(sr: FakeSchemaRegistry, run: Callable) -> None:
    sr.fail_with = SchemaRegistryRejected("Internal host sr-0.internal:8081 refused; token=abc", 42299)
    result = run(req("AVRO", "order", "com.acme.Order"), sr)
    assert result.message == messages.SR_REJECTED_CODE.format(code=42299)
    assert "sr-0" not in result.message


@pytest.mark.parametrize("text, code", [
    ("Subject 'com.acme.Order' not found.", 40401),
    ("Version 3 not found.", 40402),
])
def test_known_not_found_texts_are_shown(text: str, code: int, sr: FakeSchemaRegistry, run: Callable) -> None:
    sr.fail_with = SchemaRegistryRejected(text, code)
    result = run(req("AVRO", "order", "com.acme.Order"), sr)
    assert result.message == messages.SR_REJECTED.format(reason=text)


@pytest.mark.parametrize("text, code", [
    ("Subject 'x' not found.", 42201),
    ("Subject 'x' not found.", 40402),
    ("Version 3 not found.", 40401),
    ("Subject 'x' not found. host=sr-0", 40401),
    ("Version 3 not found. host=sr-0", 40402),
])
def test_not_found_text_with_the_wrong_code_or_extra_words_is_not_shown(
    text: str, code: int, sr: FakeSchemaRegistry, run: Callable
) -> None:
    sr.fail_with = SchemaRegistryRejected(text, code)
    result = run(req("AVRO", "order", "com.acme.Order"), sr)
    assert result.message == messages.SR_REJECTED_CODE.format(code=code)


def test_sr_rejected_raw_body_is_never_shown(sr: FakeSchemaRegistry, run: Callable) -> None:
    sr.fail_with = SchemaRegistryRejected("", 400)
    result = run(req("AVRO", "order", "com.acme.Order"), sr)
    assert result == ValidationResult(False, "Error", "Schema Registry rejected the schema (error code 400).")


@pytest.mark.parametrize("code, expected", [
    (400, messages.SR_REJECTED_CODE.format(code=400)),
    (422, messages.SR_REJECTED_CODE.format(code=422)),
    (39999, messages.SR_REJECTED_CODE.format(code=39999)),
    (50001, messages.SR_REJECTED_CODE.format(code=50001)),
    (40000, messages.SR_REJECTED_CODE.format(code=40000)),
    (42201, messages.SR_REJECTED_CODE.format(code=42201)),
    (49999, messages.SR_REJECTED_CODE.format(code=49999)),
    (409, messages.SR_REJECTED_CODE.format(code=409)),
])
def test_plain_words_are_never_shown(
    code: int, expected: str, sr: FakeSchemaRegistry, run: Callable
) -> None:
    sr.fail_with = SchemaRegistryRejected("plain words", code)
    assert run(req("AVRO", "order", "com.acme.Order"), sr) == ValidationResult(False, "Error", expected)


def test_sr_rejected_details_reason_shown_whatever_the_code(sr: FakeSchemaRegistry, run: Callable) -> None:
    sr.fail_with = SchemaRegistryRejected("Invalid schema {..}, details: boom", 400)
    assert run(req("AVRO", "order", "com.acme.Order"), sr).message == "Schema Registry rejected the schema: boom"


def test_sr_rejected_structured_message_not_echoed(sr: FakeSchemaRegistry, run: Callable) -> None:
    sr.fail_with = SchemaRegistryRejected('Bad request {"headers":{"Authorization":"Basic xyz"}}', 42201)
    result = run(req("AVRO", "order", "com.acme.Order"), sr)
    assert result == ValidationResult(False, "Error", messages.SR_REJECTED_CODE.format(code=42201))
    assert "Authorization" not in result.message


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
    (SchemaRegistryRejected("Invalid schema {..}, details: no such subject", 42201),
     "Schema Registry rejected the schema: no such subject"),
    (SchemaRegistryRejected("plain", 42201), messages.SR_REJECTED_CODE.format(code=42201)),
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


# --- E5: mode -------------------------------------------------------------------------


E5 = "Schema Registry does not accept new versions of 'com.acme.Order' right now (mode {mode}). Contact the platform team."


def test_readonly_subject_is_an_error(sr: FakeSchemaRegistry, run: Callable) -> None:
    sr.modes["com.acme.Order"] = "READONLY"
    result = run(req("AVRO", "order", "com.acme.Order"), sr)
    assert result == ValidationResult(False, "Error", E5.format(mode="READONLY"))


def test_mode_inherited_from_global_blocks(sr: FakeSchemaRegistry, run: Callable) -> None:
    sr.global_mode = "READONLY"
    result = run(req("AVRO", "order", "com.acme.Order", previous_name="com.acme.Order"), sr)
    assert result == ValidationResult(False, "Error", E5.format(mode="READONLY"))


def test_import_mode_is_an_error(sr: FakeSchemaRegistry, run: Callable) -> None:
    sr.modes["com.acme.Order"] = "IMPORT"
    result = run(req("AVRO", "order", "com.acme.Order"), sr)
    assert result == ValidationResult(False, "Error", E5.format(mode="IMPORT"))


def test_incompatible_and_readonly_gives_only_the_incompatibility(sr: FakeSchemaRegistry, run: Callable) -> None:
    sr.verdict = CompatibilityVerdict(False, tuple(AVRO))
    sr.modes["com.acme.Order"] = "READONLY"
    result = run(req("AVRO", "order", "com.acme.Order"), sr)
    assert result == ValidationResult(False, "Error", (
        "Not BACKWARD-compatible with version 1: The field 'zip' at path '/fields/1' "
        "in the new schema has no default value and is missing in the old schema"))
    assert "get_mode" not in call_names(sr)


def test_mode_failure_maps_like_other_stage_e_calls(sr: FakeSchemaRegistry, run: Callable) -> None:
    def fail(subject: str) -> str:
        raise SchemaRegistryUnavailable("x")
    sr.get_mode = fail  # type: ignore[method-assign]
    result = run(req("AVRO", "order", "com.acme.Order"), sr)
    assert result == ValidationResult(False, "Error", messages.SR_UNREACHABLE)


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


def test_same_level_unchanged_behaviour(sr: FakeSchemaRegistry, run: Callable) -> None:
    sr.add("com.acme.Order", load("AVRO", "order"))
    sr.levels["com.acme.Order"] = "FULL"
    sr.verdict = CompatibilityVerdict(False, tuple(AVRO))
    request = req("AVRO", "order", "com.acme.Order", previous_name="com.acme.Order", compatibility="FULL")
    assert run(request, sr).validationType == "Error"
    names = call_names(sr)
    assert names.count("get_latest") == 1 and names.count("registered_version") == 1
    assert names.index("registered_version") < names.index("get_compatibility_level") < names.index("check_compatibility")


def test_level_and_schema_change_together_is_an_error(sr: FakeSchemaRegistry, run: Callable) -> None:
    sr.add("com.acme.Order", '{"type":"record","name":"Order","namespace":"com.acme","fields":[]}')
    sr.levels["com.acme.Order"] = "NONE"
    request = req("AVRO", "order", "com.acme.Order", previous_name="com.acme.Order", compatibility="FULL")
    assert run(request, sr) == ValidationResult(False, "Error", messages.LEVEL_AND_SCHEMA_CHANGED.format(
        sr_level="NONE", compatibility="FULL"))
    assert "check_compatibility" not in call_names(sr)


def test_level_change_with_unchanged_schema_warns(sr: FakeSchemaRegistry, run: Callable) -> None:
    sr.add("com.acme.Order", load("AVRO", "order"))
    sr.levels["com.acme.Order"] = "NONE"
    request = req("AVRO", "order", "com.acme.Order", previous_name="com.acme.Order", compatibility="FULL")
    assert run(request, sr) == ValidationResult(True, "Warning", messages.LEVEL_WARNING.format(
        sr_level="NONE", compatibility="FULL"))
    assert "check_compatibility" in call_names(sr)


def test_level_change_without_versions_is_not_blocked(sr: FakeSchemaRegistry, run: Callable) -> None:
    sr.levels["com.acme.Order"] = "NONE"
    request = req("AVRO", "order", "com.acme.Order", previous_name="com.acme.Order", compatibility="FULL")
    assert run(request, sr).validationType == "Warning"
    assert "registered_version" not in call_names(sr)
    assert "check_compatibility" in call_names(sr)


def test_lookup_uses_the_selected_versions(sr: FakeSchemaRegistry, run: Callable) -> None:
    sr.add("com.acme.Address", load("AVRO", "address"))
    sr.add("com.acme.Address", load("AVRO", "address"))
    chosen = SrReference("com.acme.Address", "com.acme.Address", 2)
    sr.add("com.acme.Customer", load("AVRO", "customer"), references=[chosen])
    sr.levels["com.acme.Customer"] = "NONE"
    request = req("AVRO", "customer", "com.acme.Customer", references=[ReferenceInput("com.acme.Address", None, 2)],
                  previous_name="com.acme.Customer", compatibility="FULL")
    result = run(request, sr)
    assert result.validationType == "Warning"
    lookup = next(c for c in sr.calls if c[0] == "registered_version")
    assert lookup[4] == (chosen,)


def _customer_on_address_v1(sr: FakeSchemaRegistry) -> None:
    sr.add("com.acme.Address", load("AVRO", "address"))
    sr.add("com.acme.Address", load("AVRO", "address"))
    sr.add("com.acme.Customer", load("AVRO", "customer"),
           references=[SrReference("com.acme.Address", "com.acme.Address", 1)])
    sr.levels["com.acme.Customer"] = "NONE"


def test_level_change_with_a_newer_reference_version_is_an_error(sr: FakeSchemaRegistry, run: Callable) -> None:
    _customer_on_address_v1(sr)
    request = req("AVRO", "customer", "com.acme.Customer", references=[ReferenceInput("com.acme.Address", None, 2)],
                  previous_name="com.acme.Customer", compatibility="FULL")
    assert run(request, sr) == ValidationResult(False, "Error", messages.LEVEL_AND_SCHEMA_CHANGED.format(
        sr_level="NONE", compatibility="FULL"))
    assert "check_compatibility" not in call_names(sr)


def test_level_change_with_the_same_reference_version_warns(sr: FakeSchemaRegistry, run: Callable) -> None:
    _customer_on_address_v1(sr)
    request = req("AVRO", "customer", "com.acme.Customer", references=[ReferenceInput("com.acme.Address", None, 1)],
                  previous_name="com.acme.Customer", compatibility="FULL")
    result = run(request, sr)
    assert result.validationType == "Warning"
    assert messages.LEVEL_WARNING.format(sr_level="NONE", compatibility="FULL") in result.message
    assert "registered_version" in call_names(sr)
    assert "check_compatibility" in call_names(sr)


def test_level_change_when_the_request_adds_a_reference_is_an_error(sr: FakeSchemaRegistry, run: Callable) -> None:
    # The latest registered version has no references; the request now has a row: the schema changed.
    sr.add("com.acme.Address", load("AVRO", "address"))
    sr.add("com.acme.Customer", load("AVRO", "customer"))
    sr.levels["com.acme.Customer"] = "NONE"
    request = req("AVRO", "customer", "com.acme.Customer", references=[ReferenceInput("com.acme.Address", None, 1)],
                  previous_name="com.acme.Customer", compatibility="FULL")
    assert run(request, sr) == ValidationResult(False, "Error", messages.LEVEL_AND_SCHEMA_CHANGED.format(
        sr_level="NONE", compatibility="FULL"))
    assert "check_compatibility" not in call_names(sr)


def test_level_change_with_repointed_reference_is_an_error(sr: FakeSchemaRegistry, run: Callable) -> None:
    # Same text and ref_string, but the row now points at another Schema: the schema changed (review F-I2).
    sr.add("com.acme.Address", load("JSON", "address"), "JSON")
    sr.add("com.acme.Address2", load("JSON", "address").replace("com.acme.Address", "com.acme.Address2"), "JSON")
    sr.add("com.acme.Customer", load("JSON", "customer"), "JSON",
           references=[SrReference("address.json", "com.acme.Address", 1)])
    sr.levels["com.acme.Customer"] = "NONE"
    request = req("JSON", "customer", "com.acme.Customer",
                  references=[ReferenceInput("com.acme.Address2", "address.json", 1)],
                  previous_name="com.acme.Customer", compatibility="FULL")
    assert run(request, sr) == ValidationResult(False, "Error", messages.LEVEL_AND_SCHEMA_CHANGED.format(
        sr_level="NONE", compatibility="FULL"))
    assert "check_compatibility" not in call_names(sr)


def test_level_change_with_the_same_reference_subjects_warns(sr: FakeSchemaRegistry, run: Callable) -> None:
    sr.add("com.acme.Address", load("JSON", "address"), "JSON")
    sr.add("com.acme.Customer", load("JSON", "customer"), "JSON",
           references=[SrReference("address.json", "com.acme.Address", 1)])
    sr.levels["com.acme.Customer"] = "NONE"
    request = req("JSON", "customer", "com.acme.Customer", references=[address_row("JSON")],
                  previous_name="com.acme.Customer", compatibility="FULL")
    result = run(request, sr)
    assert result == ValidationResult(True, "Warning", PINNED_ADDRESS + "\n" + messages.LEVEL_WARNING.format(
        sr_level="NONE", compatibility="FULL"))


def test_no_registry_call_when_stages_a_to_d_have_errors(sr: FakeSchemaRegistry, run: Callable) -> None:
    sr.add("com.acme.Order", load("AVRO", "order"))
    sr.levels["com.acme.Order"] = "NONE"
    request = req("AVRO", "order", "com.acme.Wrong", previous_name="com.acme.Wrong", compatibility="FULL")
    assert run(request, sr).validationType == "Error"
    assert sr.calls == []


@pytest.mark.parametrize("method", ["get_latest", "registered_version"])
@pytest.mark.parametrize("error, expected", [
    (SchemaRegistryUnavailable("x"), E3),
    (SchemaRegistryUnauthorized("x"), E4),
    (SchemaRegistryRejected("Invalid schema {..}, details: bad subject", 42201),
     "Schema Registry rejected the schema: bad subject"),
])
def test_level_change_lookup_failure_is_an_error(
    method: str, error: Exception, expected: str,
    sr: FakeSchemaRegistry, run: Callable, monkeypatch: pytest.MonkeyPatch
) -> None:
    sr.add("com.acme.Order", load("AVRO", "order"))
    sr.levels["com.acme.Order"] = "NONE"
    monkeypatch.setattr(sr, method, raiser(error))
    request = req("AVRO", "order", "com.acme.Order", previous_name="com.acme.Order", compatibility="FULL")
    assert run(request, sr) == ValidationResult(False, "Error", expected)
    assert "check_compatibility" not in call_names(sr)


@pytest.mark.parametrize("error, expected", [
    (SchemaRegistryUnavailable("x"), E3),
    (SchemaRegistryUnauthorized("x"), E4),
    (SchemaRegistryRejected("Invalid schema {..}, details: bad subject", 42201),
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


def test_level_change_with_reordered_references_says_keep_the_order(sr: FakeSchemaRegistry, run: Callable) -> None:
    # Same triples, other order: the registry's lookup is order-sensitive, so a save would register a new version.
    one = '{"type":"record","name":"One","namespace":"a","fields":[]}'
    two = '{"type":"record","name":"Two","namespace":"a","fields":[]}'
    main = ('{"type":"record","name":"Main","namespace":"a","fields":'
            '[{"name":"x","type":"a.One"},{"name":"y","type":"a.Two"}]}')
    sr.add("a.One", one)
    sr.add("a.Two", two)
    sr.add("a.Main", main, references=[SrReference("a.One", "a.One", 1), SrReference("a.Two", "a.Two", 1)])
    sr.levels["a.Main"] = "NONE"
    request = ValidationRequest(
        name="a.Main", schema_type="AVRO", schema=main, compatibility="FULL",
        references=[ReferenceInput("a.Two", None, 1), ReferenceInput("a.One", None, 1)],
        previous_name="a.Main", existing_names=[])
    assert run(request, sr) == ValidationResult(False, "Error", messages.LEVEL_REFERENCE_ORDER.format(
        order="a.One v1, a.Two v1"))
    assert "check_compatibility" not in call_names(sr)


# Stage E on an edit: the format, an older version, and the lookup (audit 2 F1, F3).
ORDER = "com.acme.Order"
ORDER_V1 = load("AVRO", "order")
ORDER_V2 = json.dumps({**json.loads(ORDER_V1), "fields": json.loads(ORDER_V1)["fields"] + [
    {"name": "note", "type": ["null", "string"], "default": None}]})


def _order_with_two_versions(sr: FakeSchemaRegistry) -> None:
    sr.add(ORDER, ORDER_V1)
    sr.add(ORDER, ORDER_V2)


def test_edit_identical_to_an_older_version_is_an_error(sr: FakeSchemaRegistry, run: Callable) -> None:
    _order_with_two_versions(sr)
    request = req("AVRO", "order", ORDER, previous_name=ORDER)
    assert run(request, sr) == ValidationResult(False, "Error", messages.SCHEMA_IS_OLDER_VERSION.format(
        n=1, name=ORDER, latest=2))
    assert "check_compatibility" not in call_names(sr)


def test_older_version_with_a_level_change_is_the_older_version_error(sr: FakeSchemaRegistry, run: Callable) -> None:
    _order_with_two_versions(sr)
    sr.levels[ORDER] = "NONE"
    request = req("AVRO", "order", ORDER, previous_name=ORDER, compatibility="FULL")
    assert run(request, sr) == ValidationResult(False, "Error", messages.SCHEMA_IS_OLDER_VERSION.format(
        n=1, name=ORDER, latest=2))
    assert "get_compatibility_level" not in call_names(sr)


def test_edit_identical_to_the_latest_warns_unchanged(sr: FakeSchemaRegistry, run: Callable) -> None:
    sr.add(ORDER, ORDER_V1)
    sr.levels[ORDER] = "FULL"
    request = req("AVRO", "order", ORDER, previous_name=ORDER, compatibility="FULL")
    result = run(request, sr)
    assert result.validationType == "Warning"
    assert messages.SCHEMA_UNCHANGED.format(n=1, name=ORDER) in result.message
    assert messages.LEVEL_WARNING.format(sr_level="FULL", compatibility="FULL") not in result.message


def test_unchanged_warning_follows_the_pinned_warning(sr: FakeSchemaRegistry, run: Callable) -> None:
    sr.add("com.acme.Address", load("AVRO", "address"))
    sr.add("com.acme.Customer", load("AVRO", "customer"),
           references=[SrReference("com.acme.Address", "com.acme.Address", 1)])
    request = req("AVRO", "customer", "com.acme.Customer", references=[address_row("AVRO")],
                  previous_name="com.acme.Customer")
    assert run(request, sr) == ValidationResult(True, "Warning", PINNED_ADDRESS + "\n" + messages.SCHEMA_UNCHANGED.format(
        n=1, name="com.acme.Customer"))


def test_format_change_on_edit_is_an_error(sr: FakeSchemaRegistry, run: Callable) -> None:
    sr.add(ORDER, ORDER_V1)
    sr.levels[ORDER] = "NONE"
    schema = json.dumps({"$schema": "http://json-schema.org/draft-07/schema#", "title": ORDER, "type": "object"})
    request = ValidationRequest(name=ORDER, schema_type="JSON", schema=schema, compatibility="NONE",
                                references=[], previous_name=ORDER, existing_names=[])
    assert run(request, sr) == ValidationResult(False, "Error", messages.FORMAT_CHANGED.format(
        name=ORDER, registered="AVRO", requested="JSON"))
    assert "registered_version" not in call_names(sr)
    assert "get_compatibility_level" not in call_names(sr)


def test_edit_reads_latest_and_looks_up_once(sr: FakeSchemaRegistry, run: Callable) -> None:
    sr.add(ORDER, ORDER_V2)
    request = req("AVRO", "order", ORDER, previous_name=ORDER)
    run(request, sr)
    names = call_names(sr)
    assert names.count("get_latest") == 1 and names.count("registered_version") == 1
    assert names.index("get_latest") < names.index("registered_version") < names.index("check_compatibility")


def test_create_makes_no_lookup(sr: FakeSchemaRegistry, run: Callable) -> None:
    run(req("AVRO", "order", ORDER), sr)
    assert ("get_latest", ORDER) not in sr.calls
    assert "registered_version" not in call_names(sr)


def test_older_version_lookup_failure_is_unreachable(sr: FakeSchemaRegistry, run: Callable) -> None:
    sr.add(ORDER, ORDER_V2)
    sr.fail_only("registered_version", SchemaRegistryUnavailable("x"))
    request = req("AVRO", "order", ORDER, previous_name=ORDER)
    assert run(request, sr) == ValidationResult(False, "Error", messages.SR_UNREACHABLE)


def test_kind_warning_comes_first(sr: FakeSchemaRegistry, run: Callable) -> None:
    color = json.dumps({"type": "enum", "name": "Color", "namespace": "t", "symbols": ["RED"]})
    sr.add("t.Color", color)
    sr.levels["t.Color"] = "BACKWARD"
    request = req("AVRO", None, "t.Color", schema=color, previous_name="t.Color")
    assert run(request, sr) == ValidationResult(True, "Warning", messages.AVRO_NOT_A_MESSAGE.format(
        kind="enum") + "\n" + messages.SCHEMA_UNCHANGED.format(n=1, name="t.Color"))


# One deadline for the whole validation (audit 2 F6).

def _edit_request() -> ValidationRequest:
    return req("AVRO", "order", ORDER, previous_name=ORDER)


def test_total_deadline_returns_unreachable(sr: FakeSchemaRegistry) -> None:
    sr.add(ORDER, ORDER_V2)
    sr.delay_seconds = 0.2
    started = time.monotonic()
    result = validate(_edit_request(), sr, ValidatorConfig(total_timeout_seconds=0.3))
    assert result == ValidationResult(False, "Error", messages.SR_UNREACHABLE)
    assert time.monotonic() - started < 0.8


def test_fast_validation_is_unchanged_by_the_deadline(sr: FakeSchemaRegistry) -> None:
    sr.add(ORDER, ORDER_V2)
    request = _edit_request()
    assert (validate(request, sr, ValidatorConfig(total_timeout_seconds=0.3))
            == validate(request, sr, cfg))


def test_exception_is_still_internal_error_under_the_deadline(sr: FakeSchemaRegistry) -> None:
    sr.add(ORDER, ORDER_V2)
    sr.fail_only("get_latest", RuntimeError("boom"))
    result = validate(_edit_request(), sr, ValidatorConfig(total_timeout_seconds=0.3))
    assert result == ValidationResult(False, "Error", messages.INTERNAL_ERROR.format(err="boom"))


def test_header_drip_is_bounded() -> None:
    stop = threading.Event()

    def drip(conn: socket.socket) -> None:
        with conn:
            conn.settimeout(0.5)
            try:
                conn.recv(65536)
                for byte in b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n" * 10:
                    if stop.is_set():
                        return
                    conn.sendall(bytes([byte]))
                    time.sleep(0.2)
            except OSError:
                return

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(0.2)

    def accept() -> None:
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
            except OSError:
                continue
            threading.Thread(target=drip, args=(conn,), daemon=True).start()

    acceptor = threading.Thread(target=accept, daemon=True)
    acceptor.start()
    try:
        client = HttpSchemaRegistryClient(
            f"http://127.0.0.1:{listener.getsockname()[1]}", "u", "p", timeout_seconds=5)
        started = time.monotonic()
        result = validate(req("AVRO", "order", ORDER, previous_name=ORDER), client,
                          ValidatorConfig(total_timeout_seconds=1))
        elapsed = time.monotonic() - started
    finally:
        stop.set()
        acceptor.join()
        listener.close()
    assert result == ValidationResult(False, "Error", messages.SR_UNREACHABLE)
    assert elapsed < 1.5


def test_thread_start_failure_is_internal_error(sr: FakeSchemaRegistry, monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(self: threading.Thread) -> None:
        raise RuntimeError("can't start new thread")
    monkeypatch.setattr(threading.Thread, "start", refuse)
    result = validate(_edit_request(), sr, cfg)
    assert result == ValidationResult(
        False, "Error", messages.INTERNAL_ERROR.format(err="can't start new thread"))


@pytest.mark.parametrize("bad", [float("nan"), "30"])
def test_unusable_total_timeout_is_internal_error(bad: Any, sr: FakeSchemaRegistry) -> None:
    result = validate(_edit_request(), sr, ValidatorConfig(total_timeout_seconds=bad))
    assert result.validationType == "Error"
    assert result.message.startswith("Internal validator error: ")


def test_abandoned_worker_stops_calling_the_registry(sr: FakeSchemaRegistry) -> None:
    sr.add(ORDER, ORDER_V2)
    sr.delay_seconds = 0.2
    validate(_edit_request(), sr, ValidatorConfig(total_timeout_seconds=0.3))
    at_return = len(sr.calls)
    time.sleep(0.6)
    assert len(sr.calls) <= at_return + 1
