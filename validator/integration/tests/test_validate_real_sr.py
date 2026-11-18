"""The full `validate` pipeline with the real HTTP client against the real registry (spec §10a item 6)."""
from __future__ import annotations

import json
from collections.abc import Callable, Sequence

import pytest
import requests

from schema_validator import (
    HttpSchemaRegistryClient,
    ReferenceInput,
    ValidationRequest,
    ValidationResult,
    ValidatorConfig,
    messages,
    validate,
)
from schema_validator.models import proto_import_path

FORMATS = ("AVRO", "JSON", "PROTOBUF")

# (status code, parsed JSON body or raw text), from the conftest fixtures
Response = tuple[int, object]
Register = Callable[..., Response]


@pytest.fixture
def client(config: ValidatorConfig) -> HttpSchemaRegistryClient:
    """The validator's own client, built the way the README shows."""
    return HttpSchemaRegistryClient.from_config(config)


class RecordingClient:
    """Passes every call to the real client and records the method names."""

    def __init__(self, inner: HttpSchemaRegistryClient) -> None:
        self.inner = inner
        self.calls: list[str] = []

    def __getattr__(self, method: str) -> object:
        self.calls.append(method)
        return getattr(self.inner, method)


def request(name: str, schema_type: str, schema: str, references: Sequence[ReferenceInput] = (),
            previous_name: str | None = None, compatibility: str = "BACKWARD") -> ValidationRequest:
    return ValidationRequest(
        name=name, schema_type=schema_type, schema=schema, compatibility=compatibility,
        references=list(references), previous_name=previous_name, existing_names=[],
    )


def order(schema_type: str, ns: str, name: str = "Order") -> str:
    if schema_type == "AVRO":
        return json.dumps({"type": "record", "name": name, "namespace": ns,
                           "fields": [{"name": "id", "type": "string"}]})
    if schema_type == "JSON":
        return json.dumps({"$schema": "http://json-schema.org/draft-07/schema#", "title": f"{ns}.{name}",
                           "type": "object", "properties": {"id": {"type": "string"}}})
    return f'syntax = "proto3";\npackage {ns};\nmessage {name} {{ string id = 1; }}\n'


def incompatible_order(schema_type: str, ns: str) -> str:
    if schema_type == "AVRO":
        return json.dumps({"type": "record", "name": "Order", "namespace": ns,
                           "fields": [{"name": "id", "type": "string"}, {"name": "zip", "type": "string"}]})
    if schema_type == "JSON":
        return json.dumps({"$schema": "http://json-schema.org/draft-07/schema#", "title": f"{ns}.Order",
                           "type": "object", "properties": {"id": {"type": "integer"}}})
    return f'syntax = "proto3";\npackage {ns};\nmessage Order {{ int32 id = 1; }}\n'


BROKEN = {
    "AVRO": ("{not json", messages.NOT_JSON.split("{")[0]),
    "JSON": ("{not json", messages.NOT_JSON.split("{")[0]),
    "PROTOBUF": ('syntax = "proto3";\npackage x.y;\nmessage Order { strin id = 1 }\n',
                 messages.PROTO_INVALID.split("{")[0]),
}


def assert_error(result: ValidationResult) -> None:
    assert result.valid is False and result.validationType == "Error", result


@pytest.mark.parametrize("schema_type", FORMATS)
def test_create_ok(
    client: HttpSchemaRegistryClient,
    config: ValidatorConfig,
    register: Register,
    ns: str,
    schema_type: str,
) -> None:
    schema = order(schema_type, ns)
    req = request(f"{ns}.Order", schema_type, schema)

    assert validate(req, client, config) == ValidationResult(True, "Ok", "")

    status, body = register(req.name, schema, schema_type)
    assert status == 200, body


def test_stage_a_failure(client: HttpSchemaRegistryClient, config: ValidatorConfig, ns: str) -> None:
    result = validate(request(f"{ns}.Order", "AVRO", ""), client, config)

    assert result == ValidationResult(False, "Error", messages.SCHEMA_EMPTY)


@pytest.mark.parametrize("schema_type", FORMATS)
def test_stage_b_failure(
    client: HttpSchemaRegistryClient,
    config: ValidatorConfig,
    ns: str,
    schema_type: str,
) -> None:
    schema, prefix = BROKEN[schema_type]
    recording = RecordingClient(client)

    result = validate(request(f"{ns}.Order", schema_type, schema), recording, config)

    assert_error(result)
    assert result.message.startswith(prefix), result.message
    assert recording.calls == []


def test_stage_c_failure(client: HttpSchemaRegistryClient, config: ValidatorConfig, ns: str) -> None:
    result = validate(request(f"{ns}.Wrong", "AVRO", order("AVRO", ns)), client, config)

    assert result == ValidationResult(
        False, "Error", messages.NAME_MISMATCH.format(expected_name=f"{ns}.Order"))


def test_stage_d_failure(client: HttpSchemaRegistryClient, config: ValidatorConfig, ns: str) -> None:
    customer = json.dumps({"type": "record", "name": "Customer", "namespace": ns,
                           "fields": [{"name": "addr", "type": f"{ns}.Address"}]})

    result = validate(request(f"{ns}.Customer", "AVRO", customer, [ReferenceInput(f"{ns}.Address", None, 1)]),
                      client, config)

    assert result == ValidationResult(
        False, "Error", messages.REF_NOT_REGISTERED.format(s=f"{ns}.Address"))


def test_version_conflict_real_sr(
    client: HttpSchemaRegistryClient, config: ValidatorConfig, register: Register, ns: str,
) -> None:
    # Audit F1: Line pinned Money v1, Money is now v2, and Order uses both.
    money, line = f"{ns}.Money", f"{ns}.Line"
    money1 = {"type": "record", "name": "Money", "namespace": ns, "fields": [{"name": "amount", "type": "long"}]}
    money2 = {**money1, "fields": money1["fields"] + [{"name": "currency", "type": "string", "default": "EUR"}]}
    assert register(money, json.dumps(money1))[0] == 200
    assert register(line, json.dumps({"type": "record", "name": "Line", "namespace": ns,
                                      "fields": [{"name": "price", "type": money}]}),
                    "AVRO", [{"name": money, "subject": money, "version": 1}])[0] == 200
    assert register(money, json.dumps(money2))[0] == 200
    order = json.dumps({"type": "record", "name": "Order", "namespace": ns, "fields": [
        {"name": "line", "type": line}, {"name": "total", "type": money}]})

    result = validate(request(f"{ns}.Order", "AVRO", order,
                              [ReferenceInput(line, None, 1), ReferenceInput(money, None, 2)]), client, config)

    assert result == ValidationResult(False, "Error", messages.REF_VERSION_CONFLICT_DIRECT.format(
        s=money, uses=f"v1 through '{line}', v2 directly", chosen=2))


def test_stage_e_invalid(client: HttpSchemaRegistryClient, config: ValidatorConfig, ns: str) -> None:
    # fastavro accepts a duplicate field name; the registry does not.
    schema = json.dumps({"type": "record", "name": "Order", "namespace": ns,
                         "fields": [{"name": "a", "type": "int"}, {"name": "a", "type": "int"}]})

    result = validate(request(f"{ns}.Order", "AVRO", schema), client, config)

    assert_error(result)
    assert result.message.startswith("Schema Registry rejected the schema: "), result.message
    assert "Duplicate field a" in result.message


@pytest.mark.parametrize("schema_type", FORMATS)
def test_stage_e_incompatible(
    client: HttpSchemaRegistryClient,
    config: ValidatorConfig,
    register: Register,
    ns: str,
    schema_type: str,
) -> None:
    name = f"{ns}.Order"
    assert register(name, order(schema_type, ns), schema_type)[0] == 200

    result = validate(request(name, schema_type, incompatible_order(schema_type, ns), previous_name=name),
                      client, config)

    assert_error(result)
    assert result.message.startswith("Not BACKWARD-compatible with version 1: "), result.message
    assert "\n" not in result.message
    assert "oldSchema" not in result.message


def address(schema_type: str, ns: str) -> str:
    if schema_type == "JSON":
        return json.dumps({"$schema": "http://json-schema.org/draft-07/schema#", "title": f"{ns}.Address",
                           "type": "object", "properties": {"city": {"type": "string"}},
                           "definitions": {"Zip": {"type": "string"}}})
    if schema_type == "AVRO":
        return json.dumps({"type": "record", "name": "Address", "namespace": ns,
                           "fields": [{"name": "city", "type": "string"}]})
    return f'syntax = "proto3";\npackage {ns};\nmessage Address {{ string city = 1; }}\n'


def customer(schema_type: str, ns: str) -> tuple[str, ReferenceInput]:
    """A Customer that uses Address, and the reference row a user would enter for it."""
    subject = f"{ns}.Address"
    if schema_type == "AVRO":
        text = json.dumps({"type": "record", "name": "Customer", "namespace": ns,
                           "fields": [{"name": "addr", "type": subject}]})
        return text, ReferenceInput(subject, None, 1)
    if schema_type == "JSON":
        # With and without a #fragment: both match the row without the fragment.
        text = json.dumps({"$schema": "http://json-schema.org/draft-07/schema#", "title": f"{ns}.Customer",
                           "type": "object",
                           "properties": {"addr": {"$ref": "address.json"},
                                          "zip": {"$ref": "address.json#/definitions/Zip"}}})
        return text, ReferenceInput(subject, "address.json", 1)
    text = (f'syntax = "proto3";\npackage {ns};\nimport "{proto_import_path(subject)}";\n'
            f'message Customer {{ Address addr = 1; }}\n')
    return text, ReferenceInput(subject, None, 1)


@pytest.mark.parametrize("schema_type", FORMATS)
def test_references_end_to_end(
    client: HttpSchemaRegistryClient,
    config: ValidatorConfig,
    register: Register,
    ns: str,
    schema_type: str,
) -> None:
    subject = f"{ns}.Address"
    assert register(subject, address(schema_type, ns), schema_type)[0] == 200
    text, row = customer(schema_type, ns)

    result = validate(request(f"{ns}.Customer", schema_type, text, [row]), client, config)

    assert result == ValidationResult(True, "Warning", messages.PINNED_WARNING.format(list=f"{subject} v1"))
    ref_name = proto_import_path(subject) if schema_type == "PROTOBUF" else (row.ref_string or subject)
    status, body = register(f"{ns}.Customer", text, schema_type,
                            [{"name": ref_name, "subject": subject, "version": 1}])
    assert status == 200, body


@pytest.mark.parametrize("uses_address", [True, False], ids=["both", "nested-only"])
def test_avro_name_nested_in_a_reference(
    client: HttpSchemaRegistryClient,
    config: ValidatorConfig,
    register: Register,
    ns: str,
    uses_address: bool,
) -> None:
    # The enum Country is defined inside Address. The registry accepts its use through the
    # Address reference; the validator does not, because the Python serializer cannot load it
    # (test_python_clients.py::test_python_client_cannot_use_a_type_nested_in_a_reference).
    subject = f"{ns}.Address"
    address_text = json.dumps({"type": "record", "name": "Address", "namespace": ns, "fields": [
        {"name": "city", "type": "string"},
        {"name": "country", "type": {"type": "enum", "name": "Country", "symbols": ["NL", "US"]}}]})
    assert register(subject, address_text)[0] == 200
    fields = [{"name": "country", "type": f"{ns}.Country"}]
    if uses_address:
        fields.insert(0, {"name": "addr", "type": subject})
    text = json.dumps({"type": "record", "name": "Customer", "namespace": ns, "fields": fields})

    result = validate(request(f"{ns}.Customer", "AVRO", text, [ReferenceInput(subject, None, 1)]), client, config)

    assert result == ValidationResult(
        False, "Error", messages.AVRO_NESTED_IN_REFERENCE.format(name=f"{ns}.Country", schema=subject))
    status, body = register(f"{ns}.Customer", text, "AVRO", [{"name": subject, "subject": subject, "version": 1}])
    assert status == 200, body


def test_level_warning_on_edit(
    client: HttpSchemaRegistryClient,
    config: ValidatorConfig,
    register: Register,
    set_subject_level: Callable[[str, str], Response],
    ns: str,
) -> None:
    name = f"{ns}.Order"
    assert register(name, order("AVRO", ns))[0] == 200
    assert set_subject_level(name, "FULL")[0] == 200

    result = validate(request(name, "AVRO", order("AVRO", ns), previous_name=name, compatibility="BACKWARD"),
                      client, config)

    assert result == ValidationResult(
        True, "Warning", messages.LEVEL_WARNING.format(sr_level="FULL", compatibility="BACKWARD"))


def test_level_and_schema_change_together_is_an_error(
    client: HttpSchemaRegistryClient,
    config: ValidatorConfig,
    register: Register,
    set_subject_level: Callable[[str, str], Response],
    ns: str,
) -> None:
    # The audit's C3: under NONE any edit passes, but under FULL this one is incompatible.
    name = f"{ns}.Order"
    assert set_subject_level(name, "NONE")[0] == 200
    assert register(name, order("AVRO", ns))[0] == 200

    result = validate(request(name, "AVRO", incompatible_order("AVRO", ns), previous_name=name,
                              compatibility="FULL"), client, config)

    assert result == ValidationResult(False, "Error", messages.LEVEL_AND_SCHEMA_CHANGED.format(
        sr_level="NONE", compatibility="FULL"))


def test_level_change_with_unchanged_schema_warns(
    client: HttpSchemaRegistryClient,
    config: ValidatorConfig,
    register: Register,
    set_subject_level: Callable[[str, str], Response],
    ns: str,
) -> None:
    name = f"{ns}.Order"
    assert set_subject_level(name, "NONE")[0] == 200
    assert register(name, order("AVRO", ns))[0] == 200

    result = validate(request(name, "AVRO", order("AVRO", ns), previous_name=name, compatibility="FULL"),
                      client, config)

    assert result == ValidationResult(
        True, "Warning", messages.LEVEL_WARNING.format(sr_level="NONE", compatibility="FULL"))


def test_edit_identical_to_an_older_version_is_an_error(
    client: HttpSchemaRegistryClient,
    config: ValidatorConfig,
    register: Register,
    ns: str,
) -> None:
    name = f"{ns}.Order"
    v1 = order("AVRO", ns)
    v2 = json.dumps({"type": "record", "name": "Order", "namespace": ns, "fields": [
        {"name": "id", "type": "string"}, {"name": "note", "type": ["null", "string"], "default": None}]})
    assert register(name, v1)[0] == 200
    assert register(name, v2)[0] == 200

    result = validate(request(name, "AVRO", v1, previous_name=name), client, config)

    assert result == ValidationResult(False, "Error", messages.SCHEMA_IS_OLDER_VERSION.format(
        n=1, name=name, latest=2))


def test_edit_identical_to_the_latest_warns_unchanged(
    client: HttpSchemaRegistryClient,
    config: ValidatorConfig,
    register: Register,
    ns: str,
) -> None:
    name = f"{ns}.Order"
    assert register(name, order("AVRO", ns))[0] == 200

    result = validate(request(name, "AVRO", order("AVRO", ns), previous_name=name), client, config)

    assert result == ValidationResult(True, "Warning", messages.SCHEMA_UNCHANGED.format(n=1, name=name))


def test_format_change_on_edit_is_an_error(
    client: HttpSchemaRegistryClient,
    config: ValidatorConfig,
    register: Register,
    set_subject_level: Callable[[str, str], Response],
    ns: str,
) -> None:
    # Under NONE the registry would accept a JSON schema as the next version of an Avro subject.
    name = f"{ns}.Order"
    assert set_subject_level(name, "NONE")[0] == 200
    assert register(name, order("AVRO", ns))[0] == 200

    result = validate(request(name, "JSON", order("JSON", ns), previous_name=name, compatibility="NONE"),
                      client, config)

    assert result == ValidationResult(False, "Error", messages.FORMAT_CHANGED.format(
        name=name, registered="AVRO", requested="JSON"))


def test_level_change_gap_is_closed(
    client: HttpSchemaRegistryClient,
    config: ValidatorConfig,
    register: Register,
    set_subject_level: Callable[[str, str], Response],
    ns: str,
) -> None:
    # Same text, a newer reference version and a level change: the new Money v2 would be registered
    # under the old level, so it counts as a schema change.
    money, name = f"{ns}.Money", f"{ns}.Order"
    money1 = {"type": "record", "name": "Money", "namespace": ns, "fields": [{"name": "amount", "type": "long"}]}
    money2 = {**money1, "fields": [{"name": "value", "type": "long"}]}
    text = json.dumps({"type": "record", "name": "Order", "namespace": ns,
                       "fields": [{"name": "total", "type": money}]})
    assert set_subject_level(money, "NONE")[0] == 200
    assert register(money, json.dumps(money1))[0] == 200
    assert set_subject_level(name, "NONE")[0] == 200
    assert register(name, text, "AVRO", [{"name": money, "subject": money, "version": 1}])[0] == 200
    assert register(money, json.dumps(money2))[0] == 200

    same = validate(request(name, "AVRO", text, [ReferenceInput(money, None, 1)],
                            previous_name=name, compatibility="FULL"), client, config)
    newer = validate(request(name, "AVRO", text, [ReferenceInput(money, None, 2)],
                             previous_name=name, compatibility="FULL"), client, config)

    assert same.validationType == "Warning"
    assert messages.LEVEL_WARNING.format(sr_level="NONE", compatibility="FULL") in same.message
    assert newer == ValidationResult(False, "Error", messages.LEVEL_AND_SCHEMA_CHANGED.format(
        sr_level="NONE", compatibility="FULL"))


def test_level_change_with_reformatted_unchanged_schema(
    client: HttpSchemaRegistryClient,
    config: ValidatorConfig,
    register: Register,
    set_subject_level: Callable[[str, str], Response],
    ns: str,
) -> None:
    name = f"{ns}.Order"
    assert set_subject_level(name, "NONE")[0] == 200
    assert register(name, order("AVRO", ns))[0] == 200
    parsed = json.loads(order("AVRO", ns))
    reformatted = json.dumps(dict(reversed(parsed.items())), indent=4)
    assert reformatted != order("AVRO", ns)

    result = validate(request(name, "AVRO", reformatted, previous_name=name, compatibility="FULL"),
                      client, config)

    # The registry compares parsed schemas, so a reformatted text still counts as registered.
    assert result == ValidationResult(
        True, "Warning", messages.LEVEL_WARNING.format(sr_level="NONE", compatibility="FULL"))


def test_readonly_subject_rejected(
    client: HttpSchemaRegistryClient,
    config: ValidatorConfig,
    set_subject_mode: Callable[[str, str], Response],
    ns: str,
) -> None:
    name = f"{ns}.Order"
    assert set_subject_mode(name, "READONLY")[0] == 200

    result = validate(request(name, "AVRO", order("AVRO", ns)), client, config)

    assert result == ValidationResult(False, "Error", messages.SR_MODE.format(name=name, mode="READONLY"))


def test_unreachable(ns: str) -> None:
    config = ValidatorConfig(sr_url="http://localhost:1", sr_timeout_seconds=1)

    result = validate(request(f"{ns}.Order", "AVRO", order("AVRO", ns)),
                      HttpSchemaRegistryClient.from_config(config), config)

    assert result == ValidationResult(False, "Error", messages.SR_UNREACHABLE)


def _fixed(name: str, **extra: object) -> dict[str, object]:
    return {"type": "fixed", "name": name, "size": 4, **extra}


@pytest.mark.parametrize("build, registry_accepts, expected", [
    pytest.param(lambda ns: [{"name": "a", "type": _fixed("Inner", namespace="")}, {"name": "b", "type": "Inner"}],
                 True, lambda ns: messages.AVRO_NESTED_NAMESPACE.format(name="Inner"), id="empty-namespace"),
    pytest.param(lambda ns: [{"name": "a", "type": _fixed("Inner", namespace=None)}, {"name": "b", "type": "Inner"}],
                 True, lambda ns: messages.AVRO_NESTED_NAMESPACE.format(name="Inner"), id="null-namespace"),
    pytest.param(lambda ns: [{"name": "a", "type": _fixed("Inner")}, {"name": "b", "type": {"type": f"{ns}.Inner"}}],
                 True, lambda ns: messages.AVRO_INVALID.format(
                     err=f"write the type '{ns}.Inner' as a plain string, not as {{\"type\": \"{ns}.Inner\"}}"),
                 id="wrapped-name"),
    pytest.param(lambda ns: [{"name": "a", "type": "Later"}, {"name": "b", "type": _fixed("Later")}],
                 False, lambda ns: messages.AVRO_INVALID.format(err=f"type '{ns}.Later' is used before it is defined"),
                 id="forward-use"),
])
def test_avro_rules_the_registry_does_not_have(
    client: HttpSchemaRegistryClient, config: ValidatorConfig, sr: HttpSchemaRegistryClient, ns: str,
    build: Callable[[str], list[dict[str, object]]], registry_accepts: bool, expected: Callable[[str], str],
) -> None:
    # What the registry itself says about each shape, and that the validator stops before asking it.
    text = json.dumps({"type": "record", "name": "Order", "namespace": ns, "fields": build(ns)})
    assert sr.check_compatibility(f"{ns}.Order", text, "AVRO", ()).is_compatible is registry_accepts
    recording = RecordingClient(client)
    result = validate(request(f"{ns}.Order", "AVRO", text), recording, config)  # type: ignore[arg-type]
    assert result == ValidationResult(False, "Error", expected(ns))
    assert recording.calls == []



def test_unknown_version_is_not_found(
    client: HttpSchemaRegistryClient, config: ValidatorConfig, register: Register, ns: str,
) -> None:
    subject = f"{ns}.Address"
    assert register(subject, address("AVRO", ns))[0] == 200
    text, _ = customer("AVRO", ns)

    result = validate(request(f"{ns}.Customer", "AVRO", text, [ReferenceInput(subject, None, 9)]),
                      client, config)

    assert result == ValidationResult(
        False, "Error", messages.REF_VERSION_NOT_FOUND.format(s=subject, v=9, latest=1))


def test_soft_deleted_version_is_not_found(
    client: HttpSchemaRegistryClient, config: ValidatorConfig, register: Register, ns: str,
) -> None:
    subject = f"{ns}.Address"
    assert register(subject, address("AVRO", ns))[0] == 200
    v2 = json.dumps({"type": "record", "name": "Address", "namespace": ns, "fields": [
        {"name": "city", "type": "string"}, {"name": "zip", "type": "string", "default": ""}]})
    assert register(subject, v2)[0] == 200
    deleted = requests.delete(f"{config.sr_url}/subjects/{subject}/versions/1", timeout=20)
    assert deleted.status_code == 200, deleted.text
    text, _ = customer("AVRO", ns)

    result = validate(request(f"{ns}.Customer", "AVRO", text, [ReferenceInput(subject, None, 1)]),
                      client, config)

    assert result == ValidationResult(
        False, "Error", messages.REF_VERSION_NOT_FOUND.format(s=subject, v=1, latest=2))


def test_top_level_enum_warns(
    client: HttpSchemaRegistryClient,
    config: ValidatorConfig,
    ns: str,
) -> None:
    name = f"{ns}.Color"
    enum = json.dumps({"type": "enum", "name": "Color", "namespace": ns, "symbols": ["RED"]})

    result = validate(request(name, "AVRO", enum), client, config)

    assert result == ValidationResult(True, "Warning", messages.AVRO_NOT_A_MESSAGE.format(kind="enum"))
