"""Test client: talks to Schema Registry through NGINX with HTTP Basic, like a real serializer would.

  python client.py subjects                      # list the subjects you may see
  python client.py latest  <subject>             # latest schema of a subject
  python client.py register <subject> <file>     # register an Avro schema from a file

Settings (environment):
  SR_URL       NGINX URL + your cluster, e.g. http://sr-proxy:8080/c17
  SR_USER      Service name, e.g. alice   (the principal in Schema Registry is <cluster>.<name>, e.g. c17.alice)
  SR_PASSWORD  its password
"""
import os
import sys

from confluent_kafka.schema_registry import Schema, SchemaRegistryClient
from confluent_kafka.schema_registry.error import SchemaRegistryError

sr = SchemaRegistryClient({
    "url": os.environ.get("SR_URL", "http://localhost:8080/c17"),
    "basic.auth.user.info": f"{os.environ.get('SR_USER', 'alice')}:{os.environ.get('SR_PASSWORD', 'alice-c17-secret')}",
})


def main(cmd: str, *args: str) -> None:
    if cmd == "subjects":
        for s in sr.get_subjects():
            print(s)
    elif cmd == "latest":
        v = sr.get_latest_version(args[0])
        print(f"{v.subject} version {v.version} id {v.schema_id}: {v.schema.schema_str}")
    elif cmd == "register":
        with open(args[1]) as f:
            schema_id = sr.register_schema(args[0], Schema(f.read(), "AVRO"))
        print(f"registered {args[0]}: id {schema_id}")
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    try:
        main(*sys.argv[1:])
    except SchemaRegistryError as e:   # 401 wrong name/password/cluster, 403 no ACL
        sys.exit(f"HTTP {e.http_status_code}: {e.error_message}")
