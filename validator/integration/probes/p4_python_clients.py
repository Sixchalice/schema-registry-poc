"""Item 2 (Python half): real confluent-kafka serializers with RecordNameStrategy,
auto.register.schemas=False, use.latest.version=True.

For each case: (1) serialize before anything is registered and read the subject the client asked
for out of the error, (2) register under the validator's expected name, (3) serialize, produce,
deserialize."""
import importlib
import json
import subprocess
import sys
import tempfile
import time

from confluent_kafka import Producer
from confluent_kafka.schema_registry import (Schema, SchemaReference, SchemaRegistryClient,
                                             record_subject_name_strategy)
from confluent_kafka.schema_registry.avro import AvroDeserializer, AvroSerializer
from confluent_kafka.schema_registry.json_schema import JSONDeserializer, JSONSerializer
from confluent_kafka.schema_registry.protobuf import ProtobufDeserializer, ProtobufSerializer
from confluent_kafka.serialization import MessageField, SerializationContext

from sr import SR, register

RUN = str(int(time.time()))
N = f"p4r{RUN}"
TOPIC = "p4-any-topic"
client = SchemaRegistryClient({"url": SR})
producer = Producer({"bootstrap.servers": "localhost:19092"})
CONF = {"auto.register.schemas": False, "use.latest.version": True,
        "subject.name.strategy": record_subject_name_strategy}
ctx = SerializationContext(TOPIC, MessageField.VALUE)


def short(e):
    return str(e).replace("\n", " ")[:230]


def attempt(label, make_serializer, value):
    try:
        data = make_serializer()(value, ctx)
        return data, f"OK ({len(data)} bytes)"
    except Exception as e:  # noqa: BLE001
        return None, f"{type(e).__name__}: {short(e)}"


def run_case(label, expected_name, make_serializer, value, registrations, make_deserializer=None):
    print(f"\n[{label}] validator expected_name = {expected_name}")
    _, before = attempt(label, make_serializer, value)
    print(f"  before registering : {before}")
    for subject, schema, stype, refs in registrations:
        st, payload = register(subject, schema, stype, refs)
        if st != 200:
            print(f"  REGISTER FAILED {subject}: {st} {json.dumps(payload)[-200:]}")
    data, after = attempt(label, make_serializer, value)
    print(f"  after registering  : {after}")
    if data is not None:
        producer.produce(TOPIC, value=data)
        producer.flush(10)
        if make_deserializer:
            try:
                print(f"  deserialized       : {short(make_deserializer()(data, ctx))}")
            except Exception as e:  # noqa: BLE001
                print(f"  DESERIALIZE FAILED : {type(e).__name__}: {short(e)}")


# ---------- AVRO ----------
a1 = {"type": "record", "name": "Order", "namespace": N, "fields": [{"name": "id", "type": "string"}]}
run_case("AVRO simple", f"{N}.Order",
         lambda: AvroSerializer(client, json.dumps(a1), conf=CONF), {"id": "1"},
         [(f"{N}.Order", a1, "AVRO", None)], lambda: AvroDeserializer(client))

a2 = {"type": "record", "name": f"{N}.real.Dotted", "namespace": "ignored.ns", "fields": [{"name": "id", "type": "string"}]}
run_case("AVRO dotted name + conflicting namespace", f"{N}.real.Dotted",
         lambda: AvroSerializer(client, json.dumps(a2), conf=CONF), {"id": "1"},
         [(f"{N}.real.Dotted", a2, "AVRO", None)], lambda: AvroDeserializer(client))

addr = {"type": "record", "name": "Address", "namespace": N, "fields": [{"name": "city", "type": "string"}]}
cust = {"type": "record", "name": "Customer", "namespace": N, "fields": [{"name": "addr", "type": f"{N}.Address"}]}
cust_refs = [{"name": f"{N}.Address", "subject": f"{N}.Address", "version": 1}]
run_case("AVRO with reference (client passes Schema + references)", f"{N}.Customer",
         lambda: AvroSerializer(client, Schema(json.dumps(cust), "AVRO", [SchemaReference(**cust_refs[0])]), conf=CONF),
         {"addr": {"city": "TLV"}},
         [(f"{N}.Address", addr, "AVRO", None), (f"{N}.Customer", cust, "AVRO", cust_refs)],
         lambda: AvroDeserializer(client))
_, res = attempt("", lambda: AvroSerializer(client, json.dumps(cust), conf=CONF), {"addr": {"city": "TLV"}})
print(f"  same, client passes plain text without references: {res}")

# whitespace / field-order difference between the client's text and the registered one
a1_reformatted = json.dumps({"namespace": N, "fields": [{"type": "string", "name": "id"}], "name": "Order", "type": "record"}, indent=4)
_, res = attempt("", lambda: AvroSerializer(client, a1_reformatted, conf=CONF), {"id": "1"})
print(f"\n[AVRO reformatted text, use.latest.version=True]  {res}")
_, res = attempt("", lambda: AvroSerializer(client, a1_reformatted, conf=CONF | {"use.latest.version": False}), {"id": "1"})
print(f"[AVRO reformatted text, use.latest.version=False] {res}")

# ---------- JSON ----------
j1 = {"$schema": "http://json-schema.org/draft-07/schema#", "title": f"{N}.JOrder", "type": "object",
      "properties": {"id": {"type": "string"}}, "required": ["id"]}
run_case("JSON simple", f"{N}.JOrder",
         lambda: JSONSerializer(json.dumps(j1), client, conf=CONF), {"id": "1"},
         [(f"{N}.JOrder", j1, "JSON", None)], lambda: JSONDeserializer(json.dumps(j1), schema_registry_client=client))

jaddr = {"$schema": "http://json-schema.org/draft-07/schema#", "title": f"{N}.JAddress", "type": "object",
         "properties": {"city": {"type": "string"}}, "required": ["city"]}
jcust = {"$schema": "http://json-schema.org/draft-07/schema#", "title": f"{N}.JCustomer", "type": "object",
         "properties": {"addr": {"$ref": "address.json"}}, "required": ["addr"]}
jrefs = [{"name": "address.json", "subject": f"{N}.JAddress", "version": 1}]
run_case("JSON with external $ref", f"{N}.JCustomer",
         lambda: JSONSerializer(Schema(json.dumps(jcust), "JSON", [SchemaReference(**jrefs[0])]), client, conf=CONF),
         {"addr": {"city": "TLV"}},
         [(f"{N}.JAddress", jaddr, "JSON", None), (f"{N}.JCustomer", jcust, "JSON", jrefs)],
         lambda: JSONDeserializer(Schema(json.dumps(jcust), "JSON", [SchemaReference(**jrefs[0])]), schema_registry_client=client))
_, res = attempt("", lambda: JSONSerializer(Schema(json.dumps(jcust), "JSON", [SchemaReference(**jrefs[0])]), client, conf=CONF), {"addr": {"city": 5}})
print(f"  same, invalid payload (city is a number) is rejected by the client: {res}")
_, res = attempt("", lambda: JSONSerializer(json.dumps(jcust), client, conf=CONF), {"addr": {"city": "TLV"}})
print(f"  same, client passes plain text without references: {res}")

j_notitle = {"type": "object", "properties": {"id": {"type": "string"}}}
_, res = attempt("", lambda: JSONSerializer(json.dumps(j_notitle), client, conf=CONF), {"id": "1"})
print(f"\n[JSON without title] {res}")

# ---------- PROTOBUF ----------
tmp = tempfile.mkdtemp(prefix="p4proto")
pkg_dir = f"{tmp}/{N}"
subprocess.run(["mkdir", "-p", pkg_dir], check=True)
protos = {
    "simple.proto": f'syntax = "proto3";\npackage {N};\nmessage POrder {{ string id = 1; }}\n',
    "address.proto": f'syntax = "proto3";\npackage {N};\nmessage PAddress {{ string city = 1; }}\n',
    "customer.proto": f'syntax = "proto3";\npackage {N};\nimport "{N}/address.proto";\nmessage PCustomer {{ PAddress addr = 1; }}\n',
    "stamped.proto": f'syntax = "proto3";\npackage {N};\nimport "google/protobuf/timestamp.proto";\nmessage PStamped {{ google.protobuf.Timestamp at = 1; }}\n',
    "nested.proto": f'syntax = "proto3";\npackage {N};\nmessage POuter {{ message PInner {{ string x = 1; }} PInner inner = 1; }}\n',
}
for fn, text in protos.items():
    open(f"{pkg_dir}/{fn}", "w").write(text)
subprocess.run([sys.executable, "-m", "grpc_tools.protoc", f"-I{tmp}", f"--python_out={tmp}"] + [f"{N}/{fn}" for fn in protos], check=True)
sys.path.insert(0, tmp)
mod = {fn: importlib.import_module(f"{N}.{fn[:-6]}_pb2") for fn in protos}
PCONF = CONF | {"use.deprecated.format": False}

POrder = mod["simple.proto"].POrder
run_case("PROTOBUF simple", f"{N}.POrder", lambda: ProtobufSerializer(POrder, client, PCONF), POrder(id="1"),
         [(f"{N}.POrder", protos["simple.proto"], "PROTOBUF", None)],
         lambda: ProtobufDeserializer(POrder, {"use.deprecated.format": False}))

PStamped = mod["stamped.proto"].PStamped
run_case("PROTOBUF importing google/protobuf/timestamp.proto", f"{N}.PStamped",
         lambda: ProtobufSerializer(PStamped, client, PCONF), PStamped(),
         [(f"{N}.PStamped", protos["stamped.proto"], "PROTOBUF", None)],
         lambda: ProtobufDeserializer(PStamped, {"use.deprecated.format": False}))

PCustomer = mod["customer.proto"].PCustomer
PAddress = mod["address.proto"].PAddress
prefs = [{"name": f"{N}/address.proto", "subject": f"{N}.PAddress", "version": 1}]
run_case("PROTOBUF with import of another Schema", f"{N}.PCustomer",
         lambda: ProtobufSerializer(PCustomer, client, PCONF), PCustomer(addr=PAddress(city="TLV")),
         [(f"{N}.PAddress", protos["address.proto"], "PROTOBUF", None),
          (f"{N}.PCustomer", protos["customer.proto"], "PROTOBUF", prefs)],
         lambda: ProtobufDeserializer(PCustomer, {"use.deprecated.format": False}))

POuter = mod["nested.proto"].POuter
run_case("PROTOBUF nested message serialized on its own (POuter.PInner)", f"{N}.POuter",
         lambda: ProtobufSerializer(POuter.PInner, client, PCONF), POuter.PInner(x="1"),
         [(f"{N}.POuter", protos["nested.proto"], "PROTOBUF", None)], None)
run_case("PROTOBUF outer message of the same file", f"{N}.POuter",
         lambda: ProtobufSerializer(POuter, client, PCONF), POuter(), [], None)

import confluent_kafka  # noqa: E402
print("\nconfluent-kafka", confluent_kafka.version(), "librdkafka", confluent_kafka.libversion())
