"""The `validate` pipeline against a registry with HTTP Basic auth and ACLs (spec §7, E4).

The registry is `schema-registry-secured` in integration/docker-compose.yml. Its users are in
integration/secured/users.properties and their ACLs in the service's start command:
`validator` has SUBJECT_READ, SUBJECT_WRITE and SUBJECT_COMPATIBILITY_READ on every subject,
`no-write` lacks SUBJECT_WRITE, `no-acl` has nothing, `admin` has everything.
"""
from __future__ import annotations

import json
import os

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

SECURED_SR_URL = os.environ.get("SECURED_SR_URL", "http://localhost:18082").rstrip("/")
_HEADERS = {"Content-Type": "application/vnd.schemaregistry.v1+json"}
E4 = ValidationResult(False, "Error", messages.SR_UNAUTHORIZED)


def _unreachable_reason() -> str | None:
    try:
        status = requests.get(f"{SECURED_SR_URL}/subjects", timeout=5).status_code
    except requests.RequestException as e:
        return f"the secured Schema Registry at {SECURED_SR_URL} is not reachable ({type(e).__name__})"
    return None if status == 401 else f"{SECURED_SR_URL} answered {status} without credentials, expected 401"


_REASON = _unreachable_reason()
pytestmark = pytest.mark.skipif(_REASON is not None, reason=str(_REASON))


def avro(name: str, namespace: str, fields: list[dict[str, object]]) -> str:
    return json.dumps({"type": "record", "name": name, "namespace": namespace, "fields": fields})


def run(request: ValidationRequest, username: str | None, password: str | None) -> ValidationResult:
    config = ValidatorConfig(sr_url=SECURED_SR_URL, sr_username=username, sr_password=password)
    return validate(request, HttpSchemaRegistryClient.from_config(config), config)


def create(name: str, schema: str, references: list[ReferenceInput] | None = None,
           previous_name: str | None = None) -> ValidationRequest:
    return ValidationRequest(
        name=name, schema_type="AVRO", schema=schema, compatibility="BACKWARD",
        references=references or [], previous_name=previous_name, existing_names=[],
    )


def register_as_admin(subject: str, schema: str, references: list[dict[str, object]] | None = None) -> None:
    response = requests.post(
        f"{SECURED_SR_URL}/subjects/{subject}/versions", headers=_HEADERS, auth=("admin", "admin-secret"),
        data=json.dumps({"schema": schema, "schemaType": "AVRO", "references": references or []}), timeout=20,
    )
    assert response.status_code == 200, response.text


ORDER_FIELDS: list[dict[str, object]] = [{"name": "id", "type": "string"}]


@pytest.mark.parametrize("username, password", [
    pytest.param(None, None, id="no-credentials"),
    pytest.param("validator", "wrong-password", id="wrong-password"),
    pytest.param("nobody", "nobody-secret", id="unknown-user"),
])
def test_401_is_e4(ns: str, username: str | None, password: str | None) -> None:
    result = run(create(f"{ns}.Order", avro("Order", ns, ORDER_FIELDS)), username, password)
    assert result == E4


def test_user_without_acls_is_e4(ns: str) -> None:
    result = run(create(f"{ns}.Order", avro("Order", ns, ORDER_FIELDS)), "no-acl", "no-acl-secret")
    assert result == E4


def test_403_on_a_reference_lookup_is_e4(ns: str) -> None:
    # The first call for this request is the lookup of the referenced subject.
    schema = avro("Order", ns, [{"name": "addr", "type": f"{ns}.Address"}])
    request = create(f"{ns}.Order", schema, [ReferenceInput(f"{ns}.Address")])
    assert run(request, "no-acl", "no-acl-secret") == E4


def test_compatibility_check_needs_subject_write(ns: str) -> None:
    # SUBJECT_READ and SUBJECT_COMPATIBILITY_READ are not enough: the registry asks for
    # Write on the subject for POST /compatibility, although the call stores nothing.
    name = f"{ns}.Order"
    response = requests.post(
        f"{SECURED_SR_URL}/compatibility/subjects/{name}/versions?verbose=true", headers=_HEADERS,
        auth=("no-write", "no-write-secret"), data=json.dumps({"schema": avro("Order", ns, ORDER_FIELDS)}),
        timeout=20,
    )
    assert response.status_code == 403
    assert response.json() == {"error_code": 40301, "message": f"User is denied operation Write on Subject: {name}"}
    assert run(create(name, avro("Order", ns, ORDER_FIELDS)), "no-write", "no-write-secret") == E4


def test_level_lookup_needs_subject_compatibility_read(ns: str) -> None:
    response = requests.get(f"{SECURED_SR_URL}/config/{ns}.Order?defaultToGlobal=true",
                            auth=("no-acl", "no-acl-secret"), timeout=20)
    assert response.status_code == 403
    assert response.json()["message"] == f"User is denied operation ReadCompatibility on Subject: {ns}.Order"


def test_validator_user_create(ns: str) -> None:
    result = run(create(f"{ns}.Order", avro("Order", ns, ORDER_FIELDS)), "validator", "validator-secret")
    assert result == ValidationResult(True, "Ok", "")
    # The compatibility check stored nothing, although it needs SUBJECT_WRITE.
    listed = requests.get(f"{SECURED_SR_URL}/subjects", auth=("admin", "admin-secret"), timeout=20).json()
    assert f"{ns}.Order" not in listed


def test_validator_user_edit(ns: str) -> None:
    name = f"{ns}.Order"
    register_as_admin(name, avro("Order", ns, ORDER_FIELDS))
    compatible = avro("Order", ns, ORDER_FIELDS + [{"name": "note", "type": "string", "default": ""}])
    assert run(create(name, compatible, previous_name=name), "validator", "validator-secret") == \
        ValidationResult(True, "Ok", "")
    incompatible = avro("Order", ns, ORDER_FIELDS + [{"name": "zip", "type": "string"}])
    result = run(create(name, incompatible, previous_name=name), "validator", "validator-secret")
    assert (result.valid, result.validationType) == (False, "Error")
    assert result.message.startswith("Not BACKWARD-compatible with version 1: The field 'zip' ")


def test_validator_user_with_a_reference(ns: str) -> None:
    register_as_admin(f"{ns}.Address", avro("Address", ns, [{"name": "city", "type": "string"}]))
    schema = avro("Order", ns, [{"name": "addr", "type": f"{ns}.Address"}])
    result = run(create(f"{ns}.Order", schema, [ReferenceInput(f"{ns}.Address")]), "validator", "validator-secret")
    assert result == ValidationResult(True, "Warning", messages.PINNED_WARNING.format(list=f"{ns}.Address v1"))


@pytest.mark.parametrize("version", ["latest", "1"])
def test_version_lookups_need_subject_read(ns: str, version: str) -> None:
    response = requests.get(f"{SECURED_SR_URL}/subjects/{ns}.Order/versions/{version}",
                            auth=("no-acl", "no-acl-secret"), timeout=20)
    assert response.status_code == 403
    assert response.json() == {"error_code": 40301, "message": f"User is denied operation Read on Subject: {ns}.Order"}


@pytest.mark.parametrize("version", ["latest", "1"])
def test_missing_subject_is_404_for_the_validator_user(ns: str, version: str) -> None:
    # Not 403: with its ACLs on '*' the validator can tell "not registered" from "not allowed".
    response = requests.get(f"{SECURED_SR_URL}/subjects/{ns}.Order/versions/{version}",
                            auth=("validator", "validator-secret"), timeout=20)
    assert (response.status_code, response.json()["error_code"]) == (404, 40401)


def test_validator_user_with_a_transitive_reference(ns: str) -> None:
    # Address references Country, so the validator also calls /versions/{v} for Country.
    register_as_admin(f"{ns}.Country", avro("Country", ns, [{"name": "code", "type": "string"}]))
    register_as_admin(f"{ns}.Address", avro("Address", ns, [{"name": "country", "type": f"{ns}.Country"}]),
                      [{"name": f"{ns}.Country", "subject": f"{ns}.Country", "version": 1}])
    schema = avro("Order", ns, [{"name": "addr", "type": f"{ns}.Address"}])
    request = create(f"{ns}.Order", schema, [ReferenceInput(f"{ns}.Address")])
    client = HttpSchemaRegistryClient(SECURED_SR_URL, "validator", "validator-secret")
    calls: list[str] = []
    original = client.get_version
    client.get_version = lambda subject, version: (calls.append(f"{subject} v{version}"), original(subject, version))[1]  # type: ignore[method-assign]
    result = validate(request, client, ValidatorConfig(sr_url=SECURED_SR_URL))
    assert result == ValidationResult(True, "Warning", messages.PINNED_WARNING.format(list=f"{ns}.Address v1"))
    assert calls == [f"{ns}.Country v1"]



def test_schema_lookup_by_validator_user(ns: str) -> None:
    name = f"{ns}.Order"
    register_as_admin(name, avro("Order", ns, ORDER_FIELDS))
    client = HttpSchemaRegistryClient(SECURED_SR_URL, "validator", "validator-secret")

    assert client.is_registered(name, avro("Order", ns, ORDER_FIELDS), "AVRO", []) is True
    assert client.is_registered(name, avro("Order", ns, ORDER_FIELDS + [{"name": "z", "type": "string"}]),
                                "AVRO", []) is False


def test_level_and_schema_change_by_validator_user(ns: str) -> None:
    name = f"{ns}.Order"
    register_as_admin(name, avro("Order", ns, ORDER_FIELDS))
    put = requests.put(f"{SECURED_SR_URL}/config/{name}", headers=_HEADERS, auth=("admin", "admin-secret"),
                       data=json.dumps({"compatibility": "NONE"}), timeout=20)
    assert put.status_code == 200, put.text
    changed = avro("Order", ns, ORDER_FIELDS + [{"name": "zip", "type": "string"}])
    request = ValidationRequest(name=name, schema_type="AVRO", schema=changed, compatibility="FULL",
                                references=[], previous_name=name, existing_names=[])

    result = run(request, "validator", "validator-secret")

    assert result == ValidationResult(False, "Error", messages.LEVEL_AND_SCHEMA_CHANGED.format(
        sr_level="NONE", compatibility="FULL"))


def test_mode_lookup_by_validator_user(ns: str) -> None:
    client = HttpSchemaRegistryClient(SECURED_SR_URL, "validator", "validator-secret")

    assert client.get_mode(f"{ns}.Order") == "READWRITE"
