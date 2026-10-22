"""Sample Avro client for the bearer-token Schema Registry behind Envoy.

What the client configures, and what it doesn't:
  - Schema Registry: Envoy's URL plus HTTP Basic (Service name + password from sr_services). Nothing about tokens:
    Envoy has the auth service check the Basic credentials and mint the JWT on every request, and replaces the
    client's Authorization header with it. The client never sees, caches or refreshes a token.
  - Kafka: SASL/SCRAM straight to the brokers. Envoy and the auth service are not involved; Kafka ACLs apply.
  - Schemas are registered by `platform` (scripts/up.sh). A producer only looks up the latest registered version,
    so it needs SUBJECT_READ on the subject, not SUBJECT_WRITE.

Usage (from the host, against the running jwt-poc stack; pip install 'confluent-kafka[avro,schemaregistry]'):
  python client/client.py produce            # one Order to `orders`
  python client/client.py consume            # read `orders` from the start, print what this registry can decode
Inside the compose network set SR_URL=http://envoy:10000 KAFKA_BOOTSTRAP=kafka:29092.
"""
import os
import sys
import uuid

from confluent_kafka import Consumer, KafkaError, Producer
from confluent_kafka.schema_registry import SchemaRegistryClient
from confluent_kafka.schema_registry.avro import AvroDeserializer, AvroSerializer
from confluent_kafka.schema_registry.error import SchemaRegistryError
from confluent_kafka.serialization import MessageField, SerializationContext

TOPIC = os.environ.get("TOPIC", "orders")

# ---------- Schema Registry, through Envoy ----------
SR_CONF = {
    "url": os.environ.get("SR_URL", "http://localhost:10000"),     # Envoy, never an SR node (they have no host port)
    "basic.auth.user.info": os.environ.get("SR_USER_INFO", "svc-reader:svc-reader-secret"),  # name:password
    # Envoy answers 503 while the auth service is unreachable; the client retries 5xx/429 with backoff.
    "max.retries": 3,
    "retries.wait.ms": 1000,
    "timeout": 10,                   # seconds per HTTP request (httpx); Envoy's own route timeout is 60 s
}

# ---------- Kafka, direct ----------
KAFKA_CONF = {
    "bootstrap.servers": os.environ.get("KAFKA_BOOTSTRAP", "localhost:9092"),
    "security.protocol": "SASL_PLAINTEXT",
    "sasl.mechanism": "SCRAM-SHA-256",
    "sasl.username": os.environ.get("KAFKA_USER", "svc-a"),
    "sasl.password": os.environ.get("KAFKA_PASSWORD", "svc-a-secret"),
}
GROUP_ID = os.environ.get("GROUP_ID", "c17.svc-a.jwt-client")       # must match the Kafka ACL prefix c17.svc-a

sr = SchemaRegistryClient(SR_CONF)   # caches schemas by id and subject: repeat records don't hit Envoy again
ctx = SerializationContext(TOPIC, MessageField.VALUE)


def produce() -> None:
    # schema_str=None + use.latest.version: serialize with whatever `platform` registered for <topic>-value
    ser = AvroSerializer(sr, schema_str=None, conf={"auto.register.schemas": False, "use.latest.version": True})
    order = {"id": f"client-{uuid.uuid4().hex[:8]}", "amount": 12.5}
    try:
        value = ser(order, ctx)
    except SchemaRegistryError as e:   # 401 bad credentials, 403 no ACL on the subject, 503 auth service down
        sys.exit(f"Schema Registry refused {TOPIC}-value: HTTP {e.http_status_code} {e.error_message}")

    p = Producer(KAFKA_CONF)
    p.produce(TOPIC, value=value, on_delivery=lambda err, msg: print(
        f"delivery failed: {err}" if err else f"produced {order} to {msg.topic()}[{msg.partition()}]@{msg.offset()}"))
    p.flush(15)


def consume() -> None:
    de = AvroDeserializer(sr)   # looks up each writer schema by the id in the record (GET /schemas/ids/<id>)
    c = Consumer({**KAFKA_CONF, "group.id": GROUP_ID, "auto.offset.reset": "earliest", "enable.auto.commit": False})
    c.subscribe([TOPIC])
    try:
        while (msg := c.poll(5)) is not None:
            if msg.error():
                if msg.error().code() != KafkaError._PARTITION_EOF:
                    print(f"consumer error: {msg.error()}")
                continue
            try:
                print(f"{msg.partition()}@{msg.offset()}: {de(msg.value(), ctx)}")
            except SchemaRegistryError as e:
                # `orders` also holds records from the repo's main registry: their ids don't exist in this one
                print(f"{msg.partition()}@{msg.offset()}: not decodable here (HTTP {e.http_status_code})")
    finally:
        c.close()


if __name__ == "__main__":
    {"produce": produce, "consume": consume}[sys.argv[1] if len(sys.argv) > 1 else "produce"]()
