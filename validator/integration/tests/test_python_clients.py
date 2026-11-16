"""Real confluent-kafka producers look up the subject `validate` accepts (spec §10a items 2 and 3).

Each case: `validate` accepts the name; the serializer (RecordNameStrategy,
auto.register.schemas=False, use.latest.version=True) fails with
"Subject '<name>' not found" until that name is registered; after registration it
serializes and the bytes are produced to Kafka. Referenced schemas are registered
first, because a serializer built from a schema with references fetches them.
"""
from __future__ import annotations

import importlib
import json
import re
import subprocess
import sys
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from types import ModuleType

import pytest
from confluent_kafka import KafkaError, Message, Producer
from confluent_kafka.schema_registry import (
    Schema,
    SchemaReference,
    SchemaRegistryClient,
    SchemaRegistryError,
    record_subject_name_strategy,
)
from confluent_kafka.schema_registry.avro import AvroSerializer
from confluent_kafka.schema_registry.json_schema import JSONSerializer
from confluent_kafka.schema_registry.protobuf import ProtobufSerializer
from confluent_kafka.serialization import MessageField, SerializationContext

from schema_validator import (
    HttpSchemaRegistryClient,
    ReferenceInput,
    ValidationRequest,
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
