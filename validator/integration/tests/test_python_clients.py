"""Real confluent-kafka producers look up the subject `validate` accepts (spec §10a items 2 and 3).

Each case: `validate` accepts the name; the serializer (RecordNameStrategy,
auto.register.schemas=False, use.latest.version=True) fails with
"Subject '<name>' not found" until that name is registered; after registration it
serializes and the bytes are produced to Kafka. Referenced schemas are registered
first, because a serializer built from a schema with references fetches them.
"""
from __future__ import annotations

import importlib
import io
import json
import re
import subprocess
import sys
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from types import ModuleType

import fastavro
import pytest
from fastavro.schema import SchemaParseException
from confluent_kafka import KafkaError, Message, Producer
from confluent_kafka.schema_registry import (
    Schema,
    SchemaReference,
    SchemaRegistryClient,
    SchemaRegistryError,
    record_subject_name_strategy,
)
from confluent_kafka.schema_registry.avro import AvroDeserializer, AvroSerializer
from confluent_kafka.schema_registry.json_schema import JSONSerializer
from confluent_kafka.schema_registry.protobuf import ProtobufSerializer
from confluent_kafka.serialization import MessageField, SerializationContext
from jsonschema.validators import Draft202012Validator, validator_for

from schema_validator import (
    HttpSchemaRegistryClient,
    ReferenceInput,
    ValidationRequest,
    ValidationResult,
    ValidatorConfig,
    validate,
)
from schema_validator.models import proto_import_path

TOPIC = "validator-it-clients"
CONF = {
    "auto.register.schemas": False,
    "use.latest.version": True,
    "subject.name.strategy": record_subject_name_strategy,
}
PROTO_CONF = CONF | {"use.deprecated.format": False}
CTX = SerializationContext(TOPIC, MessageField.VALUE)
DRAFT_07 = "http://json-schema.org/draft-07/schema#"

Serializer = Callable[[object, SerializationContext], bytes | None]

# (status code, parsed JSON body or raw text), from the conftest fixtures
Response = tuple[int, object]
Register = Callable[..., Response]


@pytest.fixture
def ck_client(config: ValidatorConfig) -> SchemaRegistryClient:
    return SchemaRegistryClient({"url": config.sr_url})


@pytest.fixture(scope="module")
def producer(kafka_bootstrap: str) -> Iterator[Producer]:
    p = Producer({"bootstrap.servers": kafka_bootstrap})
    yield p
    p.flush(10)


def produce(producer: Producer, data: bytes) -> None:
    """Produce one message and wait for the broker to acknowledge it."""
    errors: list[KafkaError | None] = []

    def on_delivery(err: KafkaError | None, _msg: Message) -> None:
        errors.append(err)

    producer.produce(TOPIC, value=data, on_delivery=on_delivery)
    producer.flush(15)
    assert errors == [None]


def accepted_name(name: str, schema_type: str, schema: str, config: ValidatorConfig,
                  references: Sequence[ReferenceInput] = ()) -> str:
    """Validate a create; the request's name is the one `validate` accepts."""
    request = ValidationRequest(
        name=name, schema_type=schema_type, schema=schema, compatibility="BACKWARD",
        references=list(references), previous_name=None, existing_names=[],
    )
    result = validate(request, HttpSchemaRegistryClient.from_config(config), config)
    assert result.valid, result.message
    return name


def check_client(
    serializer: Serializer,
    value: object,
    name: str,
    register_main: Callable[[], None],
    producer: Producer,
) -> None:
    """Before registration the serializer looks up `name`; after it, it serializes and produces."""
    with pytest.raises(SchemaRegistryError, match=re.escape(f"Subject '{name}' not found")):
        serializer(value, CTX)

    register_main()

    data = serializer(value, CTX)
    assert isinstance(data, bytes) and data
    produce(producer, data)


def registrar(
    register: Register,
    subject: str,
    schema: str,
    schema_type: str,
    references: Sequence[dict[str, object]] | None = None,
) -> Callable[[], None]:
    def run() -> None:
        status, body = register(subject, schema, schema_type, references)
        assert status == 200, body
    return run


# AVRO

def test_avro_simple(
    ck_client: SchemaRegistryClient,
    register: Register,
    producer: Producer,
    config: ValidatorConfig,
    ns: str,
) -> None:
    schema = json.dumps({"type": "record", "name": "Order", "namespace": ns,
                         "fields": [{"name": "id", "type": "string"}]})
    name = accepted_name(f"{ns}.Order", "AVRO", schema, config)

    check_client(AvroSerializer(ck_client, schema, conf=CONF), {"id": "1"}, name,
                 registrar(register, name, schema, "AVRO"), producer)


def test_avro_dotted_name_wins_over_namespace(
    ck_client: SchemaRegistryClient,
    register: Register,
    producer: Producer,
    config: ValidatorConfig,
    ns: str,
) -> None:
    schema = json.dumps({"type": "record", "name": f"{ns}.real.Dotted", "namespace": "ignored.ns",
                         "fields": [{"name": "id", "type": "string"}]})
    name = accepted_name(f"{ns}.real.Dotted", "AVRO", schema, config)

    check_client(AvroSerializer(ck_client, schema, conf=CONF), {"id": "1"}, name,
                 registrar(register, name, schema, "AVRO"), producer)


def test_avro_with_reference(
    ck_client: SchemaRegistryClient,
    register: Register,
    producer: Producer,
    config: ValidatorConfig,
    ns: str,
) -> None:
    address = f"{ns}.Address"
    registrar(register, address, json.dumps({"type": "record", "name": "Address", "namespace": ns,
                                             "fields": [{"name": "city", "type": "string"}]}), "AVRO")()
    schema = json.dumps({"type": "record", "name": "Customer", "namespace": ns,
                         "fields": [{"name": "addr", "type": address}]})
    name = accepted_name(f"{ns}.Customer", "AVRO", schema, config, [ReferenceInput(address)])
    refs = [{"name": address, "subject": address, "version": 1}]

    serializer = AvroSerializer(ck_client, Schema(schema, "AVRO", [SchemaReference(**refs[0])]), conf=CONF)
    check_client(serializer, {"addr": {"city": "TLV"}}, name,
                 registrar(register, name, schema, "AVRO", refs), producer)


def test_avro_shared_enum(
    ck_client: SchemaRegistryClient, register: Register, producer: Producer,
    config: ValidatorConfig, ns: str,
) -> None:
    # An enum is a Schema of its own; a record references it by name (audit F5).
    color = f"{ns}.Color"
    color_schema = json.dumps({"type": "enum", "name": "Color", "namespace": ns, "symbols": ["RED", "GREEN"]})
    accepted_name(color, "AVRO", color_schema, config)
    registrar(register, color, color_schema, "AVRO")()

    schema = json.dumps({"type": "record", "name": "Paint", "namespace": ns,
                         "fields": [{"name": "color", "type": color}]})
    name = accepted_name(f"{ns}.Paint", "AVRO", schema, config, [ReferenceInput(color)])
    refs = [{"name": color, "subject": color, "version": 1}]

    serializer = AvroSerializer(ck_client, Schema(schema, "AVRO", [SchemaReference(**refs[0])]), conf=CONF)
    check_client(serializer, {"color": "GREEN"}, name,
                 registrar(register, name, schema, "AVRO", refs), producer)
    data = serializer({"color": "GREEN"}, CTX)
    assert isinstance(data, bytes)
    assert AvroDeserializer(ck_client)(data, CTX) == {"color": "GREEN"}


def test_avro_name_from_a_transitive_reference(
    ck_client: SchemaRegistryClient,
    register: Register,
    producer: Producer,
    config: ValidatorConfig,
    ns: str,
) -> None:
    # Address references Country. Customer selects only Address and uses Country directly:
    # the registry and the Python client load Country together with Address.
    country, address = f"{ns}.Country", f"{ns}.Address"
    registrar(register, country, json.dumps({"type": "record", "name": "Country", "namespace": ns, "fields": [
        {"name": "code", "type": {"type": "enum", "name": "Code", "symbols": ["NL", "US"]}}]}), "AVRO")()
    registrar(register, address, json.dumps({"type": "record", "name": "Address", "namespace": ns, "fields": [
        {"name": "country", "type": country}]}), "AVRO", [{"name": country, "subject": country, "version": 1}])()
    schema = json.dumps({"type": "record", "name": "Customer", "namespace": ns, "fields": [
        {"name": "addr", "type": address}, {"name": "extra", "type": country}]})
    name = accepted_name(f"{ns}.Customer", "AVRO", schema, config, [ReferenceInput(address)])
    refs = [{"name": address, "subject": address, "version": 1}]

    serializer = AvroSerializer(ck_client, Schema(schema, "AVRO", [SchemaReference(**refs[0])]), conf=CONF)
    value = {"addr": {"country": {"code": "NL"}}, "extra": {"code": "US"}}
    check_client(serializer, value, name, registrar(register, name, schema, "AVRO", refs), producer)


def test_two_versions_of_one_type_corrupt_data(
    ck_client: SchemaRegistryClient, register: Register, ns: str,
) -> None:
    # Audit F1: Line pinned Money v1, Order uses Line and Money v2 (latest). The registry
    # accepts it, but Python and Java resolve the one name Money differently, so one side
    # decodes the other's bytes wrongly. This is why `validate` rejects the shape
    # (REF_VERSION_CONFLICT). Here: the Python serializer writes Money v2 in both places,
    # a reader that resolves Money v1 everywhere (what Java does) misreads `total`.
    money, line, order_name = f"{ns}.Money", f"{ns}.Line", f"{ns}.Order"
    money1 = {"type": "record", "name": "Money", "namespace": ns, "fields": [{"name": "amount", "type": "long"}]}
    money2 = {**money1, "fields": money1["fields"] + [{"name": "currency", "type": "string", "default": "EUR"}]}
    line_schema = {"type": "record", "name": "Line", "namespace": ns, "fields": [{"name": "price", "type": money}]}
    order = {"type": "record", "name": "Order", "namespace": ns, "fields": [
        {"name": "line", "type": line}, {"name": "total", "type": money}]}
    registrar(register, money, json.dumps(money1), "AVRO")()
    registrar(register, line, json.dumps(line_schema), "AVRO", [{"name": money, "subject": money, "version": 1}])()
    registrar(register, money, json.dumps(money2), "AVRO")()
    refs = [{"name": line, "subject": line, "version": 1}, {"name": money, "subject": money, "version": 2}]
    registrar(register, order_name, json.dumps(order), "AVRO", refs)()

    serializer = AvroSerializer(
        ck_client, Schema(json.dumps(order), "AVRO", [SchemaReference(**r) for r in refs]), conf=CONF)
    value = {"line": {"price": {"amount": 5, "currency": "USD"}}, "total": {"amount": 7, "currency": "USD"}}
    data = serializer(value, CTX)
    assert isinstance(data, bytes)

    # Magic byte and 4-byte schema id come first; the rest is the Avro body.
    java_view = fastavro.parse_schema({**order, "fields": [
        {"name": "line", "type": {**line_schema, "fields": [{"name": "price", "type": money1}]}},
        {"name": "total", "type": money}]})
    assert data[5:] == bytes.fromhex("0a06555344" "0e06555344")
    decoded = fastavro.schemaless_reader(io.BytesIO(data[5:]), java_view)
    # Money v1 has only `amount`, so `total` starts at the currency's length byte 0x06 (zigzag 3).
    assert decoded == {"line": {"price": {"amount": 5}}, "total": {"amount": 3}}

    # The same bytes read with Money v2 everywhere give the original values back,
    # so the version difference is the cause.
    v2_view = fastavro.parse_schema({**order, "fields": [
        {"name": "line", "type": {**line_schema, "fields": [{"name": "price", "type": money2}]}},
        {"name": "total", "type": money}]})
    assert fastavro.schemaless_reader(io.BytesIO(data[5:]), v2_view) == value


@pytest.mark.parametrize("fields", [
    pytest.param(lambda ns: [{"name": "a", "type": f"{ns}.Address"}, {"name": "c", "type": f"{ns}.Country"}],
                 id="address-then-nested"),
    pytest.param(lambda ns: [{"name": "c", "type": f"{ns}.Country"}], id="nested-only"),
])
def test_python_client_cannot_use_a_type_nested_in_a_reference(
    ck_client: SchemaRegistryClient, register: Register, ns: str,
    fields: Callable[[str], list[dict[str, object]]],
) -> None:
    # The enum Country is defined inside the referenced Address. The registry
    # accepts a schema that uses <ns>.Country through the Address reference; the Python
    # serializer cannot load it, which is why `validate` rejects this shape
    # (AVRO_NESTED_IN_REFERENCE). If a client release fixes this, the test fails: revisit.
    address = f"{ns}.Address"
    registrar(register, address, json.dumps({"type": "record", "name": "Address", "namespace": ns, "fields": [
        {"name": "city", "type": "string"},
        {"name": "country", "type": {"type": "enum", "name": "Country", "symbols": ["NL", "US"]}}]}), "AVRO")()
    schema = json.dumps({"type": "record", "name": "Customer", "namespace": ns, "fields": fields(ns)})
    refs = [{"name": address, "subject": address, "version": 1}]
    registrar(register, f"{ns}.Customer", schema, "AVRO", refs)()

    value = {"a": {"city": "TLV", "country": "NL"}, "c": "US"}
    with pytest.raises((TypeError, SchemaParseException)):
        serializer = AvroSerializer(ck_client, Schema(schema, "AVRO", [SchemaReference(**refs[0])]), conf=CONF)
        serializer(value, CTX)


# JSON

def test_json_simple(
    ck_client: SchemaRegistryClient,
    register: Register,
    producer: Producer,
    config: ValidatorConfig,
    ns: str,
) -> None:
    schema = json.dumps({"$schema": DRAFT_07, "title": f"{ns}.Order", "type": "object",
                         "properties": {"id": {"type": "string"}}, "required": ["id"]})
    name = accepted_name(f"{ns}.Order", "JSON", schema, config)

    check_client(JSONSerializer(schema, ck_client, conf=CONF), {"id": "1"}, name,
                 registrar(register, name, schema, "JSON"), producer)


def test_json_with_external_ref(
    ck_client: SchemaRegistryClient,
    register: Register,
    producer: Producer,
    config: ValidatorConfig,
    ns: str,
) -> None:
    address = f"{ns}.Address"
    registrar(register, address, json.dumps({
        "$schema": DRAFT_07, "title": address, "type": "object",
        "properties": {"city": {"type": "string"}}, "required": ["city"],
        "definitions": {"Zip": {"type": "string"}},
    }), "JSON")()
    # One $ref with a #fragment and one without; the row has the value without the fragment.
    schema = json.dumps({"$schema": DRAFT_07, "title": f"{ns}.Customer", "type": "object",
                         "properties": {"addr": {"$ref": "address.json"},
                                        "zip": {"$ref": "address.json#/definitions/Zip"}},
                         "required": ["addr"]})
    name = accepted_name(f"{ns}.Customer", "JSON", schema, config, [ReferenceInput(address, "address.json")])
    refs = [{"name": "address.json", "subject": address, "version": 1}]

    serializer = JSONSerializer(Schema(schema, "JSON", [SchemaReference(**refs[0])]), ck_client, conf=CONF)
    check_client(serializer, {"addr": {"city": "TLV"}, "zip": "12345"}, name,
                 registrar(register, name, schema, "JSON", refs), producer)


def validated(name: str, schema: str, config: ValidatorConfig, references: Sequence[ReferenceInput]) -> ValidationResult:
    request = ValidationRequest(
        name=name, schema_type="JSON", schema=schema, compatibility="BACKWARD",
        references=list(references), previous_name=None, existing_names=[],
    )
    return validate(request, HttpSchemaRegistryClient.from_config(config), config)


def register_address(register: Register, ns: str) -> str:
    address = f"{ns}.Address"
    registrar(register, address, json.dumps({
        "$schema": DRAFT_07, "title": address, "type": "object",
        "properties": {"street": {"type": "string"}}, "required": ["street"],
    }), "JSON")()
    return address


def id_schema(ns: str, title: str) -> str:
    return json.dumps({"$schema": DRAFT_07, "$id": "https://acme.example/schemas/order.json",
                       "title": f"{ns}.{title}", "type": "object",
                       "properties": {"a": {"$ref": "address.json"}}, "required": ["a"]})


ABSOLUTE = "https://acme.example/schemas/address.json"


def test_json_id_relative_ref_is_rejected(
    ck_client: SchemaRegistryClient, register: Register, producer: Producer, config: ValidatorConfig, ns: str,
) -> None:
    # Under a root $id the Python serializer resolves "address.json" against it, so a reference
    # named "address.json" cannot be found. The registry itself accepts the schema.
    address = register_address(register, ns)
    schema = id_schema(ns, "O2")
    result = validated(f"{ns}.O2", schema, config, [ReferenceInput(address, "address.json")])
    assert result.validationType == "Error" and ABSOLUTE in result.message

    refs = [{"name": "address.json", "subject": address, "version": 1}]
    registrar(register, f"{ns}.O2", schema, "JSON", refs)()
    serializer = JSONSerializer(Schema(schema, "JSON", [SchemaReference(**refs[0])]), ck_client, conf=CONF)
    with pytest.raises(Exception, match="Unresolvable"):
        serializer({"a": {"street": "x"}}, CTX)


def test_json_id_absolute_ref_string_works(
    ck_client: SchemaRegistryClient, register: Register, producer: Producer, config: ValidatorConfig, ns: str,
) -> None:
    address = register_address(register, ns)
    schema = id_schema(ns, "O3")
    result = validated(f"{ns}.O3", schema, config, [ReferenceInput(address, ABSOLUTE)])
    assert result.validationType == "Warning", result.message

    refs = [{"name": ABSOLUTE, "subject": address, "version": 1}]
    serializer = JSONSerializer(Schema(schema, "JSON", [SchemaReference(**refs[0])]), ck_client, conf=CONF)
    check_client(serializer, {"a": {"street": "x"}}, f"{ns}.O3",
                 registrar(register, f"{ns}.O3", schema, "JSON", refs), producer)
    with pytest.raises(Exception, match="(?i)valid|integer|string"):
        serializer({"a": {"street": 1}}, CTX)


def test_json_internal_id_ref_works(
    ck_client: SchemaRegistryClient, register: Register, producer: Producer, config: ValidatorConfig, ns: str,
) -> None:
    schema = json.dumps({"$schema": DRAFT_07, "$id": "https://acme.example/o5.json", "title": f"{ns}.O5",
                         "type": "object",
                         "definitions": {"x": {"$id": "https://acme.example/x.json", "type": "string"}},
                         "properties": {"x": {"$ref": "https://acme.example/x.json"}}})
    result = validated(f"{ns}.O5", schema, config, [])
    assert result.validationType == "Ok", result.message
    check_client(JSONSerializer(schema, ck_client, conf=CONF), {"x": "ok"}, f"{ns}.O5",
                 registrar(register, f"{ns}.O5", schema, "JSON"), producer)


def test_python_client_reads_https_draft07_as_2020_12(config: ValidatorConfig, ns: str) -> None:
    # jsonschema does not know the https spelling of draft-07: it validates with 2020-12 and warns
    # that this will become an error. That is why the validator rejects it (JSON_DRAFT).
    draft = "https://json-schema.org/draft-07/schema#"
    with pytest.warns(DeprecationWarning, match="metaschema specified by \\$schema was not found"):
        assert validator_for({"$schema": draft}) is Draft202012Validator
    schema = json.dumps({"$schema": draft, "title": f"{ns}.H7", "type": "object"})
    result = validated(f"{ns}.H7", schema, config, [])
    assert not result.valid
    assert result.message == f"Unsupported JSON Schema draft '{draft}'."


# PROTOBUF

def compile_proto(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    files: dict[str, str],
) -> dict[str, ModuleType]:
    """Generate Python classes with protoc; returns the module of each file, by path."""
    for path, text in files.items():
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    subprocess.run([sys.executable, "-m", "grpc_tools.protoc", f"-I{tmp_path}", f"--python_out={tmp_path}",
                    *files], check=True, capture_output=True)
    monkeypatch.syspath_prepend(str(tmp_path))
    return {path: importlib.import_module(path.removesuffix(".proto").replace("/", ".") + "_pb2")
            for path in files}


def test_protobuf_simple(
    ck_client: SchemaRegistryClient,
    register: Register,
    producer: Producer,
    config: ValidatorConfig,
    ns: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schema = f'syntax = "proto3";\npackage {ns};\nmessage Order {{ string id = 1; }}\n'
    name = accepted_name(f"{ns}.Order", "PROTOBUF", schema, config)
    order = compile_proto(tmp_path, monkeypatch, {f"{ns}/Order.proto": schema})[f"{ns}/Order.proto"].Order

    check_client(ProtobufSerializer(order, ck_client, PROTO_CONF), order(id="1"), name,
                 registrar(register, name, schema, "PROTOBUF"), producer)


def test_protobuf_builtin_import(
    ck_client: SchemaRegistryClient,
    register: Register,
    producer: Producer,
    config: ValidatorConfig,
    ns: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schema = (f'syntax = "proto3";\npackage {ns};\nimport "google/protobuf/timestamp.proto";\n'
              f'message Stamped {{ google.protobuf.Timestamp at = 1; }}\n')
    name = accepted_name(f"{ns}.Stamped", "PROTOBUF", schema, config)
    stamped = compile_proto(tmp_path, monkeypatch, {f"{ns}/Stamped.proto": schema})[f"{ns}/Stamped.proto"].Stamped

    check_client(ProtobufSerializer(stamped, ck_client, PROTO_CONF), stamped(), name,
                 registrar(register, name, schema, "PROTOBUF"), producer)


def test_protobuf_import_of_another_schema(
    ck_client: SchemaRegistryClient,
    register: Register,
    producer: Producer,
    config: ValidatorConfig,
    ns: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    address = f"{ns}.Address"
    address_path = proto_import_path(address)  # <ns>/Address.proto
    address_text = f'syntax = "proto3";\npackage {ns};\nmessage Address {{ string city = 1; }}\n'
    registrar(register, address, address_text, "PROTOBUF")()
    schema = (f'syntax = "proto3";\npackage {ns};\nimport "{address_path}";\n'
              f'message Customer {{ Address addr = 1; }}\n')
    name = accepted_name(f"{ns}.Customer", "PROTOBUF", schema, config, [ReferenceInput(address)])
    modules = compile_proto(tmp_path, monkeypatch, {address_path: address_text, f"{ns}/Customer.proto": schema})
    customer = modules[f"{ns}/Customer.proto"].Customer
    value = customer(addr=modules[address_path].Address(city="TLV"))
    refs = [{"name": address_path, "subject": address, "version": 1}]

    check_client(ProtobufSerializer(customer, ck_client, PROTO_CONF), value, name,
                 registrar(register, name, schema, "PROTOBUF", refs), producer)


def test_protobuf_types_nested_in_an_imported_schema(
    ck_client: SchemaRegistryClient,
    register: Register,
    producer: Producer,
    config: ValidatorConfig,
    ns: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Unlike Avro, a message and an enum nested inside the imported Address can be used
    # from another schema: `validate` accepts it, the registry registers it and the
    # Python serializer produces with it.
    address = f"{ns}.Address"
    address_path = proto_import_path(address)
    address_text = (f'syntax = "proto3";\npackage {ns};\n'
                    f'message Address {{\n  string city = 1;\n  Geo geo = 2;\n  Kind kind = 3;\n'
                    f'  message Geo {{ double lat = 1; }}\n  enum Kind {{ HOME = 0; WORK = 1; }}\n}}\n')
    registrar(register, address, address_text, "PROTOBUF")()
    schema = (f'syntax = "proto3";\npackage {ns};\nimport "{address_path}";\n'
              f'message Customer {{\n  Address.Geo geo = 1;\n  {ns}.Address.Kind kind = 2;\n'
              f'  repeated Address.Geo more = 3;\n}}\n')
    name = accepted_name(f"{ns}.Customer", "PROTOBUF", schema, config, [ReferenceInput(address)])
    modules = compile_proto(tmp_path, monkeypatch, {address_path: address_text, f"{ns}/Customer.proto": schema})
    customer = modules[f"{ns}/Customer.proto"].Customer
    addr = modules[address_path].Address
    value = customer(geo=addr.Geo(lat=1.5), kind=addr.WORK, more=[addr.Geo(lat=2.0)])
    refs = [{"name": address_path, "subject": address, "version": 1}]

    check_client(ProtobufSerializer(customer, ck_client, PROTO_CONF), value, name,
                 registrar(register, name, schema, "PROTOBUF", refs), producer)


def test_json_definitions_of_a_referenced_schema_two_levels_deep(
    ck_client: SchemaRegistryClient,
    register: Register,
    producer: Producer,
    config: ValidatorConfig,
    ns: str,
) -> None:
    # Unlike Avro, a definition nested inside the referenced schema can be used through a
    # $ref with a fragment, also when that definition itself points at another definition.
    address = f"{ns}.Address"
    registrar(register, address, json.dumps({
        "$schema": DRAFT_07, "title": address, "type": "object", "properties": {"city": {"type": "string"}},
        "definitions": {"Geo": {"type": "object", "properties": {"zip": {"$ref": "#/definitions/Zip"}},
                                "required": ["zip"]},
                        "Zip": {"type": "string", "minLength": 5}},
    }), "JSON")()
    schema = json.dumps({"$schema": DRAFT_07, "title": f"{ns}.Customer", "type": "object",
                         "properties": {"geo": {"$ref": "address.json#/definitions/Geo"}}, "required": ["geo"]})
    name = accepted_name(f"{ns}.Customer", "JSON", schema, config, [ReferenceInput(address, "address.json")])
    refs = [{"name": "address.json", "subject": address, "version": 1}]

    serializer = JSONSerializer(Schema(schema, "JSON", [SchemaReference(**refs[0])]), ck_client, conf=CONF)
    check_client(serializer, {"geo": {"zip": "12345"}}, name, registrar(register, name, schema, "JSON", refs), producer)
    # The nested definition is really applied: a value that breaks Zip's minLength is refused.
    with pytest.raises(Exception, match="(?i)valid|short"):
        serializer({"geo": {"zip": "1"}}, CTX)

