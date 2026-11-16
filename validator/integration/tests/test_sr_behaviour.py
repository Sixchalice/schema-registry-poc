"""What Schema Registry 7.9.0 itself does (spec §10a items 1, 4, 5 and 7).

If a future registry version behaves differently, these tests fail and the plan is revisited.
"""
from __future__ import annotations

import json
from collections.abc import Callable

import pytest

from schema_validator import HttpSchemaRegistryClient, ValidatorConfig
from schema_validator.formats import protobuf
from schema_validator.models import DEFAULT_BUILTIN_PROTO_IMPORTS
from schema_validator.sr_client import CompatibilityVerdict
from schema_validator.sr_messages import invalid_schema_reason, parse_incompatibility

FORMATS = ("AVRO", "JSON", "PROTOBUF")

# (status code, parsed JSON body or raw text), from the conftest fixtures
Response = tuple[int, object]
Register = Callable[..., Response]


def valid_schema(schema_type: str, ns: str, name: str = "Order") -> str:
    if schema_type == "AVRO":
        return json.dumps({"type": "record", "name": name, "namespace": ns,
                           "fields": [{"name": "id", "type": "string"}]})
    if schema_type == "JSON":
        return json.dumps({"title": f"{ns}.{name}", "type": "object",
                           "properties": {"id": {"type": "string"}}})
    return f'syntax = "proto3";\npackage {ns};\nmessage {name} {{ string id = 1; }}\n'


def invalid_schema(schema_type: str, ns: str) -> str:
    """Well-formed text that the registry's parser rejects."""
    if schema_type == "AVRO":  # unknown type
        return json.dumps({"type": "record", "name": "Order", "namespace": ns,
                           "fields": [{"name": "id", "type": "strin"}]})
    if schema_type == "JSON":  # not a JSON Schema keyword value
        return json.dumps({"title": f"{ns}.Order", "type": "objekt", "properties": {"id": {"type": 5}}})
    return f'syntax = "proto3";\npackage {ns};\nmessage Order {{ strin id = 1 }}\n'


def incompatible_edit(schema_type: str, ns: str) -> str:
    """Not BACKWARD-compatible with `valid_schema(schema_type, ns)`."""
    if schema_type == "AVRO":  # new required field
        return json.dumps({"type": "record", "name": "Order", "namespace": ns,
                           "fields": [{"name": "id", "type": "string"}, {"name": "zip", "type": "string"}]})
    if schema_type == "JSON":  # type change
        return json.dumps({"title": f"{ns}.Order", "type": "object",
                           "properties": {"id": {"type": "integer"}}})
    return f'syntax = "proto3";\npackage {ns};\nmessage Order {{ int32 id = 1; }}\n'


def address(schema_type: str, ns: str) -> str:
    return valid_schema(schema_type, ns, "Address")


def _ref(name: str, subject: str, version: int = 1) -> list[dict[str, object]]:
    return [{"name": name, "subject": subject, "version": version}]


# Item 1

@pytest.mark.parametrize("schema_type", FORMATS)
def test_new_subject_valid(sr: HttpSchemaRegistryClient, ns: str, schema_type: str) -> None:
    verdict = sr.check_compatibility(f"{ns}.Order", valid_schema(schema_type, ns), schema_type, ())
    assert verdict == CompatibilityVerdict(True, ())


@pytest.mark.parametrize("schema_type", FORMATS)
def test_new_subject_invalid(sr: HttpSchemaRegistryClient, ns: str, schema_type: str) -> None:
    verdict = sr.check_compatibility(f"{ns}.Order", invalid_schema(schema_type, ns), schema_type, ())
    assert verdict.is_compatible is False
    assert verdict.messages[0].startswith("Invalid schema ")
    assert "details: " in verdict.messages[0]
    assert invalid_schema_reason(verdict.messages[0]) is not None


# Item 7

@pytest.mark.parametrize("schema_type", FORMATS)
def test_incompatible_messages_parse(
    sr: HttpSchemaRegistryClient,
    register: Register,
    ns: str,
    schema_type: str,
) -> None:
    subject = f"{ns}.Order"
    assert register(subject, valid_schema(schema_type, ns), schema_type)[0] == 200

    verdict = sr.check_compatibility(subject, incompatible_edit(schema_type, ns), schema_type, ())

    assert verdict.is_compatible is False
    parsed = parse_incompatibility(verdict.messages)
    assert len(parsed.descriptions) >= 1
    assert parsed.old_version == 1
    assert parsed.level == "BACKWARD"
    assert not any("oldSchema" in d for d in parsed.descriptions)


# Item 4: the registry does not enforce the validator's own rules

@pytest.mark.parametrize("schema_type", FORMATS)
def test_sr_accepts_unused_reference_row(register: Register, ns: str, schema_type: str) -> None:
    assert register(f"{ns}.Address", address(schema_type, ns), schema_type)[0] == 200
    ref_name = {"AVRO": f"{ns}.Address", "JSON": "address.json", "PROTOBUF": f"{ns}/Address.proto"}[schema_type]

    status, body = register(f"{ns}.Order", valid_schema(schema_type, ns), schema_type,
                            _ref(ref_name, f"{ns}.Address"))

    assert status == 200, body


def test_sr_accepts_protobuf_reference_name_mismatch(register: Register, ns: str) -> None:
    assert register(f"{ns}.Address", address("PROTOBUF", ns), "PROTOBUF")[0] == 200
    order = (f'syntax = "proto3";\npackage {ns};\nimport "acme/address.proto";\n'
             f'message Order {{ {ns}.Address a = 1; }}\n')

    status, body = register(f"{ns}.Order", order, "PROTOBUF", _ref("acme/other.proto", f"{ns}.Address"))

    assert status == 200, body


def test_sr_accepts_protobuf_cycle(register: Register, ns: str) -> None:
    assert register(f"{ns}.Address", address("PROTOBUF", ns), "PROTOBUF")[0] == 200
    customer = (f'syntax = "proto3";\npackage {ns};\nimport "{ns}/Address.proto";\n'
                f'message Customer {{ Address a = 1; }}\n')
    status, body = register(f"{ns}.Customer", customer, "PROTOBUF", _ref(f"{ns}/Address.proto", f"{ns}.Address"))
    assert status == 200, body
    address_v2 = (f'syntax = "proto3";\npackage {ns};\nimport "{ns}/Customer.proto";\n'
                  f'message Address {{ string id = 1; Customer c = 2; }}\n')

    status, body = register(f"{ns}.Address", address_v2, "PROTOBUF", _ref(f"{ns}/Customer.proto", f"{ns}.Customer"))

    assert status == 200, body


def test_sr_accepts_json_title_with_space(register: Register, ns: str) -> None:
    schema = json.dumps({"title": f"{ns}.My Order", "type": "object"})

    status, body = register(f"{ns}.My Order", schema, "JSON")

    assert status == 200, body


# Item 5: built-in Protobuf imports

# One type per built-in import path (from the original probe).
_BUILTIN_TYPES = {
    "google/protobuf/any.proto": "google.protobuf.Any",
    "google/protobuf/api.proto": "google.protobuf.Api",
    "google/protobuf/descriptor.proto": "google.protobuf.FileOptions",
    "google/protobuf/duration.proto": "google.protobuf.Duration",
    "google/protobuf/empty.proto": "google.protobuf.Empty",
    "google/protobuf/field_mask.proto": "google.protobuf.FieldMask",
    "google/protobuf/source_context.proto": "google.protobuf.SourceContext",
    "google/protobuf/struct.proto": "google.protobuf.Struct",
    "google/protobuf/timestamp.proto": "google.protobuf.Timestamp",
    "google/protobuf/type.proto": "google.protobuf.Type",
    "google/protobuf/wrappers.proto": "google.protobuf.StringValue",
    "google/type/calendar_period.proto": "google.type.CalendarPeriod",
    "google/type/color.proto": "google.type.Color",
    "google/type/date.proto": "google.type.Date",
    "google/type/datetime.proto": "google.type.DateTime",
    "google/type/dayofweek.proto": "google.type.DayOfWeek",
    "google/type/decimal.proto": "google.type.Decimal",
    "google/type/expr.proto": "google.type.Expr",
    "google/type/fraction.proto": "google.type.Fraction",
    "google/type/interval.proto": "google.type.Interval",
    "google/type/latlng.proto": "google.type.LatLng",
    "google/type/money.proto": "google.type.Money",
    "google/type/month.proto": "google.type.Month",
    "google/type/phone_number.proto": "google.type.PhoneNumber",
    "google/type/postal_address.proto": "google.type.PostalAddress",
    "google/type/quaternion.proto": "google.type.Quaternion",
    "google/type/timeofday.proto": "google.type.TimeOfDay",
    "confluent/meta.proto": "confluent.Meta",
    "confluent/type/decimal.proto": "confluent.type.Decimal",
}


def _importing(ns: str, path: str, type_name: str | None) -> str:
    field = f"{type_name} f = 1;" if type_name else "string f = 1;"
    return f'syntax = "proto3";\npackage {ns};\nimport "{path}";\nmessage Order {{ {field} }}\n'


@pytest.mark.parametrize("path", sorted(DEFAULT_BUILTIN_PROTO_IMPORTS))
def test_builtin_imports(register: Register, ns: str, path: str) -> None:
    text = _importing(ns, path, _BUILTIN_TYPES.get(path))

    status, body = register(f"{ns}.Order", text, "PROTOBUF")

    assert status == 200, body
    # It also compiles locally with the bundled _proto directory, and needs no reference row.
    analysis = protobuf.analyse(text, {}, ValidatorConfig())
    assert analysis.errors == []
    assert analysis.external_refs == set()


@pytest.mark.parametrize(("path", "type_name"), [
    ("google/type/localized_text.proto", "google.type.LocalizedText"),
    ("google/rpc/status.proto", "google.rpc.Status"),
])
def test_non_builtin_imports_rejected(register: Register, ns: str, path: str, type_name: str) -> None:
    status, body = register(f"{ns}.Order", _importing(ns, path, type_name), "PROTOBUF")

    assert status == 422, body


# Lookups the validator relies on (spec §7)

def test_level_endpoint_for_missing_subject(sr: HttpSchemaRegistryClient, ns: str) -> None:
    assert sr.get_compatibility_level(f"{ns}.none") == "BACKWARD"


def test_get_latest_missing(
    sr: HttpSchemaRegistryClient,
    raw_get: Callable[[str], Response],
    ns: str,
) -> None:
    # The client returns None only for a 404 carrying error_code 40401 or 40402.
    status, body = raw_get(f"/subjects/{ns}.none/versions/latest")
    assert status == 404
    assert isinstance(body, dict) and body.get("error_code") == 40401

    assert sr.get_latest(f"{ns}.none") is None


def test_get_version_missing(
    sr: HttpSchemaRegistryClient,
    register: Register,
    raw_get: Callable[[str], Response],
    ns: str,
) -> None:
    subject = f"{ns}.Order"
    assert register(subject, valid_schema("AVRO", ns))[0] == 200

    status, body = raw_get(f"/subjects/{subject}/versions/99")
    assert status == 404
    assert isinstance(body, dict) and body.get("error_code") == 40402

    assert sr.get_version(subject, 99) is None
