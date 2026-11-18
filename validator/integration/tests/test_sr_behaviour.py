"""What Schema Registry 7.9.0 itself does (spec §10a items 1, 4, 5 and 7).

If a future registry version behaves differently, these tests fail and the plan is revisited.
"""
from __future__ import annotations

import json
import socket
import threading
import urllib.parse
from collections.abc import Callable

import pytest
import requests

from schema_validator import HttpSchemaRegistryClient, ValidationRequest, ValidationResult, ValidatorConfig, validate
from schema_validator import messages
from schema_validator.formats import protobuf
from schema_validator.models import DEFAULT_BUILTIN_PROTO_IMPORTS
from schema_validator.sr_client import CompatibilityVerdict, SchemaRegistryUnavailable, SrReference
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


def test_404_from_a_wrong_base_url_is_not_not_found(
    register: Register,
    raw_get: Callable[[str], Response],
    config: ValidatorConfig,
    ns: str,
) -> None:
    # A wrong base URL also gives 404, but without a "not found" code. Taking it for
    # "not registered" would make every create pass and every reference fail D1.
    subject = f"{ns}.Order"
    assert register(subject, valid_schema("AVRO", ns))[0] == 200
    for path in (f"/no-such-prefix/subjects/{subject}/versions/latest",
                 f"/no-such-prefix/subjects/{subject}/versions/1"):
        status, body = raw_get(path)
        assert status == 404
        assert isinstance(body, dict) and body.get("error_code") not in (40401, 40402), body

    wrong = HttpSchemaRegistryClient(config.sr_url + "/no-such-prefix")
    with pytest.raises(SchemaRegistryUnavailable):
        wrong.get_latest(f"{ns}.none")
    with pytest.raises(SchemaRegistryUnavailable):
        wrong.get_version(subject, 1)

    create = ValidationRequest(
        name=f"{ns}.Fresh", schema_type="AVRO", schema=valid_schema("AVRO", ns, "Fresh"),
        compatibility="BACKWARD", references=[], previous_name=None, existing_names=[],
    )
    assert validate(create, wrong, config) == ValidationResult(False, "Error", messages.SR_UNREACHABLE)


def test_stalled_body_is_unreachable() -> None:
    """A registry that sends part of a body and stalls is E3, not an internal error (no Docker needed)."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    done = threading.Event()

    def serve() -> None:
        conn, _ = server.accept()
        with conn:
            conn.recv(65536)
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                         b"Content-Length: 100\r\n\r\n{\"abc\"")
            done.wait(10)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        client = HttpSchemaRegistryClient(f"http://127.0.0.1:{server.getsockname()[1]}", timeout_seconds=1)
        request = ValidationRequest(
            name="stalled.Fresh", schema_type="AVRO", schema=valid_schema("AVRO", "stalled", "Fresh"),
            compatibility="BACKWARD", references=[], previous_name=None, existing_names=[],
        )
        result = validate(request, client, ValidatorConfig())
    finally:
        done.set()
        server.close()
        thread.join(5)
    assert result == ValidationResult(False, "Error", messages.SR_UNREACHABLE)
    assert "127.0.0.1" not in result.message


# --- Spec §10 rows 5a, 9, 10 and 11 (first answered by one-off probe scripts) -----------

_JSON_HEADERS = {"Content-Type": "application/vnd.schemaregistry.v1+json"}


def _avro(name: str, ns: str, fields: list[dict[str, object]]) -> str:
    return json.dumps({"type": "record", "name": name, "namespace": ns, "fields": fields})


@pytest.mark.parametrize("draft", [
    "http://json-schema.org/draft-03/schema#",
    "http://json-schema.org/draft-07/schema#",
    "https://json-schema.org/draft/2020-12/schema",
    "https://example.com/not-a-draft",
])
def test_sr_accepts_any_json_schema_draft(register: Register, ns: str, draft: str) -> None:
    # Row 5a: the allowed drafts are the validator's policy, not the registry's.
    text = json.dumps({"$schema": draft, "title": f"{ns}.Order", "type": "object",
                       "properties": {"id": {"type": "string"}}})
    status, body = register(f"{ns}.Order", text, "JSON")
    assert status == 200, body


def test_sr_subject_names_are_case_sensitive(register: Register, raw_get: Callable[[str], Response], ns: str) -> None:
    # Row 9: X.Case and X.case are two subjects, so the platform compares names itself (C2).
    assert register(f"{ns}.Case", _avro("Case", ns, [{"name": "a", "type": "string"}]))[0] == 200
    assert register(f"{ns}.case", _avro("case", ns, [{"name": "b", "type": "int"}]))[0] == 200
    status, subjects = raw_get(f"/subjects?subjectPrefix={ns}.")
    assert status == 200
    assert {f"{ns}.Case", f"{ns}.case"} <= set(subjects)  # type: ignore[arg-type]
    assert raw_get(f"/subjects/{ns}.Case/versions")[1] == [1]
    assert raw_get(f"/subjects/{ns}.case/versions")[1] == [1]


@pytest.mark.parametrize("name", ["with space.Order", "ünï.Order", "a/b.Order", "9.Order", "x" * 300])
def test_sr_accepts_odd_subject_names(register: Register, raw_get: Callable[[str], Response], ns: str,
                                      name: str) -> None:
    # Row 9: the registry accepts anything; A4 and C3 are the validator's own.
    subject = f"{ns}.{name}"
    assert register(subject, _avro("Order", ns, [{"name": "id", "type": "string"}]))[0] == 200
    assert raw_get(f"/subjects/{urllib.parse.quote(subject, safe='')}/versions")[1] == [1]


def test_sr_context_prefix_lands_in_another_context(
    register: Register, raw_get: Callable[[str], Response], ns: str
) -> None:
    # Row 9: a name starting with ":.<ctx>:" is not a subject of the default context (A4 forbids ':').
    context = f".{ns}"
    status, body = register(f":{context}:{ns}.Order", _avro("Order", ns, [{"name": "id", "type": "string"}]))
    assert status == 200, body
    assert context in raw_get("/contexts")[1]  # type: ignore[operator]
    assert raw_get(f"/subjects/{ns}.Order/versions")[0] == 404


def test_sr_refuses_to_delete_a_referenced_subject(
    register: Register, raw_get: Callable[[str], Response], config: ValidatorConfig, ns: str
) -> None:
    # Row 10: the delete fails with 422 (42206) and referencedby lists the schema ids.
    address, customer = f"{ns}.Address", f"{ns}.Customer"
    assert register(address, _avro("Address", ns, [{"name": "city", "type": "string"}]))[0] == 200
    status, body = register(customer, _avro("Customer", ns, [{"name": "addr", "type": address}]), "AVRO",
                            [{"name": address, "subject": address, "version": 1}])
    assert status == 200, body
    response = requests.delete(f"{config.sr_url}/subjects/{address}", headers=_JSON_HEADERS, timeout=20)
    assert (response.status_code, response.json()["error_code"]) == (422, 42206)
    assert raw_get(f"/subjects/{address}/versions/1/referencedby") == (200, [body["id"]])  # type: ignore[index]


def test_soft_delete_drops_the_history_for_compatibility(
    register: Register, sr: HttpSchemaRegistryClient, config: ValidatorConfig, ns: str
) -> None:
    # Row 10: after a soft delete an incompatible schema registers as the next version, and
    # the validator sees the subject as not registered.
    name = f"{ns}.Order"
    v1 = _avro("Order", ns, [{"name": "id", "type": "string"}])
    incompatible = _avro("Order", ns, [{"name": "id", "type": "string"}, {"name": "zip", "type": "string"}])
    assert register(name, v1)[0] == 200
    assert register(name, incompatible)[0] == 409
    assert sr.check_compatibility(name, incompatible, "AVRO", ()).is_compatible is False

    response = requests.delete(f"{config.sr_url}/subjects/{name}", headers=_JSON_HEADERS, timeout=20)
    assert (response.status_code, response.json()) == (200, [1])

    assert sr.get_latest(name) is None
    assert sr.check_compatibility(name, incompatible, "AVRO", ()).is_compatible is True
    status, _ = register(name, incompatible)
    assert status == 200
    latest = sr.get_latest(name)
    assert latest is not None and latest.version == 2


def test_sr_stores_the_latest_version_for_reference_version_minus_one(
    register: Register, sr: HttpSchemaRegistryClient, ns: str
) -> None:
    # Row 11: -1 is accepted at registration and stored as the concrete latest version.
    address, customer = f"{ns}.Address", f"{ns}.Customer"
    assert register(address, _avro("Address", ns, [{"name": "city", "type": "string"}]))[0] == 200
    assert register(address, _avro("Address", ns, [{"name": "city", "type": "string"},
                                                   {"name": "zip", "type": "string", "default": ""}]))[0] == 200
    status, body = register(customer, _avro("Customer", ns, [{"name": "addr", "type": address}]), "AVRO",
                            [{"name": address, "subject": address, "version": -1}])
    assert status == 200, body
    latest = sr.get_latest(customer)
    assert latest is not None
    assert [(r.name, r.subject, r.version) for r in latest.references] == [(address, address, 2)]


def test_lookup_is_reference_order_sensitive(
    register: Register, config: ValidatorConfig, ns: str
) -> None:
    # The registry's lookup (POST /subjects/{s}) compares the references in order: the same schema with
    # its references reversed is not found, so registering it would add an identical new version.
    one, two, main = f"{ns}.One", f"{ns}.Two", f"{ns}.Main"
    assert register(one, _avro("One", ns, [{"name": "a", "type": "string"}]))[0] == 200
    assert register(two, _avro("Two", ns, [{"name": "b", "type": "string"}]))[0] == 200
    schema = _avro("Main", ns, [{"name": "x", "type": one}, {"name": "y", "type": two}])
    refs = [{"name": one, "subject": one, "version": 1}, {"name": two, "subject": two, "version": 1}]
    status, body = register(main, schema, "AVRO", refs)
    assert status == 200, body

    def lookup(references: list[dict[str, object]]) -> requests.Response:
        return requests.post(
            f"{config.sr_url}/subjects/{main}", headers=_JSON_HEADERS, timeout=20,
            json={"schema": schema, "schemaType": "AVRO", "references": references})

    assert lookup(refs).status_code == 200
    reversed_lookup = lookup(list(reversed(refs)))
    assert (reversed_lookup.status_code, reversed_lookup.json()["error_code"]) == (404, 40403)


def test_registering_an_older_versions_text_reuses_its_id(
    register: Register, sr: HttpSchemaRegistryClient, raw_get: Callable[[str], Response], ns: str
) -> None:
    # F1: the registry does not add a version for text it already holds; the producers get v1's id back.
    name = f"{ns}.Order"
    v1 = _avro("Order", ns, [{"name": "id", "type": "string"}])
    v2 = _avro("Order", ns, [{"name": "id", "type": "string"},
                             {"name": "note", "type": ["null", "string"], "default": None}])
    status1, body1 = register(name, v1)
    assert status1 == 200, body1
    assert register(name, v2)[0] == 200
    status, again = register(name, v1)
    assert status == 200, again
    assert again == body1  # type: ignore[comparison-overlap]
    assert raw_get(f"/subjects/{name}/versions") == (200, [1, 2])
    assert sr.registered_version(name, v1, "AVRO", []) == 1


def test_lookup_with_another_reference_version_is_not_found(
    register: Register, sr: HttpSchemaRegistryClient, ns: str
) -> None:
    address, customer = f"{ns}.Address", f"{ns}.Customer"
    assert register(address, _avro("Address", ns, [{"name": "city", "type": "string"}]))[0] == 200
    assert register(address, _avro("Address", ns, [{"name": "city", "type": "string"},
                                                   {"name": "zip", "type": "string", "default": ""}]))[0] == 200
    text = _avro("Customer", ns, [{"name": "addr", "type": address}])
    status, body = register(customer, text, "AVRO", [{"name": address, "subject": address, "version": 1}])
    assert status == 200, body
    assert sr.registered_version(customer, text, "AVRO", [SrReference(address, address, 1)]) == 1
    assert sr.registered_version(customer, text, "AVRO", [SrReference(address, address, 2)]) is None


# Mode lookups (spec §7, E5)

def test_mode_default_to_global(raw_get: Callable[[str], Response], ns: str) -> None:
    status, body = raw_get(f"/mode/{ns}.none?defaultToGlobal=true")

    assert (status, body) == (200, {"mode": "READWRITE"})


def test_mode_readonly_subject(
    register: Register,
    set_subject_mode: Callable[[str, str], Response],
    ns: str,
) -> None:
    subject = f"{ns}.Order"
    assert set_subject_mode(subject, "READONLY")[0] == 200

    status, body = register(subject, valid_schema("AVRO", ns))

    assert status == 422, body
    assert isinstance(body, dict) and body.get("error_code") == 42205


# --- Audit 2 F9: "$ref" values that point inside the schema ------------------------------

_DRAFT_07_REFS = [
    ("#/definitions/s", True, {"s": {"type": "string"}}, True),
    ("#/defin%69tions/s", True, {"s": {"type": "string"}}, True),
    ("https://acme.example/r8.json#/definitions/s", True, {"s": {"type": "string"}}, False),
    ("r8.json#/definitions/s", True, {"s": {"type": "string"}}, False),
    ("#/definitions/missing", True, {"s": {"type": "string"}}, False),
    ("#/definitions/missing", False, {"s": {"type": "string"}}, False),
    ("#/definitions/s/0", True, {"s": {"type": "string"}}, False),
    ("#/definitions/a~1b", True, {"a/b": {}}, True),
    ("#/definitions/t~0", True, {"t~": {}}, True),
    ("#/definitions/a%2Fb", True, {"a/b": {}}, True),
    ("#/definitions/a%2Fb", True, {"a": {"b": {}}}, False),
    ("#/definitions/a+b", True, {"a b": {}}, True),
    ("#/definitions/a+b", True, {"a+b": {}}, False),
    ("#/definitions/a%2Bb", True, {"a+b": {}}, True),
    ("#/definitions/a%2Bb", True, {"a b": {}}, False),
    ("#/definitions/s/", True, {"s": {"type": "string"}}, True),
    ("#/definitions/s//", True, {"s": {"type": "string"}}, True),
    ("#/definitions/", True, {"s": {"type": "string"}}, True),
    ("#/definitions/zz/", True, {"s": {"type": "string"}}, False),
    ("#/definitions//x", True, {"": {"x": {}}}, True),
    ("#/definitions//x", True, {"s": {"type": "string"}}, False),
    ("https://acme.example/r8.json", True, {"s": {"type": "string"}}, True),
    ("https://acme.example/r8.json#", True, {"s": {"type": "string"}}, False),
    ("r8.json", True, {"s": {"type": "string"}}, True),
    ("#/allOf/01", True, {"s": {"type": "string"}}, True),
]
_D2019 = "https://json-schema.org/draft/2019-09/schema"
_D2020 = "https://json-schema.org/draft/2020-12/schema"
# Under 2019-09 and 2020-12 the registry reads a "$ref" written with the schema's own URL as a "#" ref.
_LATER_DRAFT_REFS = [
    (draft, ref, True, {"s": {"type": "string"}}, accepted)
    for draft in (_D2019, _D2020)
    for ref, accepted in [
        ("https://acme.example/r8.json#/$defs/s", True),
        ("r8.json#/$defs/s", True),
        ("https://acme.example/r8.json#", True),
        ("https://acme.example/r8.json#/%24defs/s", True),
        ("https://acme.example/r8.json#/$defs/missing", False),
        ("r8.json#/$defs/missing", False),
    ]
]


@pytest.mark.parametrize("draft, ref, with_id, definitions, accepted", [
    ("http://json-schema.org/draft-07/schema#", *row) for row in _DRAFT_07_REFS
] + _LATER_DRAFT_REFS)
def test_json_internal_refs(
    sr: HttpSchemaRegistryClient, ns: str, draft: str, ref: str, with_id: bool, definitions: dict[str, object],
    accepted: bool,
) -> None:
    defs = "definitions" if "draft-07" in draft else "$defs"
    # Every schema has a two-element "allOf", for the list index cases.
    d = {"$schema": draft, "title": f"{ns}.Order", "type": "object", "allOf": [{}, {"type": "object"}],
         defs: definitions, "properties": {"a": {"$ref": ref}}}
    if with_id:
        d["$id"] = "https://acme.example/r8.json"
    verdict = sr.check_compatibility(f"{ns}.Order", json.dumps(d), "JSON", ())
    assert verdict.is_compatible is accepted, verdict


# These two shapes (30-level int32 fields, Avro 1e6 numbers) register at the default max_schema_bytes (100,000);
# the registry keeps a re-printed copy in one Kafka record of about 1 MB (audit 5 C3). Not every shape fits:
# a deeply nested allow_alias enum of short values is refused above about 89,998 bytes (see VERIFICATION.md)

def _largest_protobuf(ns: str, levels: int = 30) -> str:
    """Compact, `levels` nested messages (protoc compiles at most 31), int32 fields up to 99,000-100,000 bytes."""
    def build(fields: int) -> str:
        body = "".join(f"int32 f{i}={i};" for i in range(1, fields + 1))
        inner = body
        for level in range(levels, 1, -1):
            inner = f"message M{level}{{{inner}}}"
        return f'syntax="proto3";package {ns};message T{{{inner}}}'

    fields = 1
    while len(build(fields + 1).encode()) < 100_000:
        fields += 1
    text = build(fields)
    assert 99_000 <= len(text.encode()) < 100_000
    return text


def _largest_avro(ns: str) -> str:
    """A record with a custom property holding 1e6 numbers (the registry prints each as 1000000.0)."""
    def build(count: int) -> str:
        return (f'{{"type":"record","name":"A","namespace":"{ns}","fields":[{{"name":"id","type":"string"}}],'
                f'"x":[{",".join(["1e6"] * count)}]}}')

    count = 1
    while len(build(count + 1).encode()) < 100_000:
        count += 1
    text = build(count)
    assert 99_000 <= len(text.encode()) < 100_000
    return text


def test_largest_default_schemas_register(register: Register, ns: str) -> None:
    status, body = register(f"{ns}.T", _largest_protobuf(ns), "PROTOBUF")
    assert status == 200, body

    status, body = register(f"{ns}.A", _largest_avro(ns), "AVRO")
    assert status == 200, body
