"""Differential run: the validator's verdict against what Schema Registry really does.

For many generated schemas (valid seeds and random mutations of them) this calls
`validate` as a create, then tries to register the same text under the same name, and
sorts every case into one of:

    agree-pass       validate passed, registration succeeded
    agree-reject     validate failed, registration failed
    own-rule         validate failed on a rule the registry does not have (namespace,
                     name, title, references...), registration succeeded. Expected.
    stage-e-only     validate failed only in stage E, yet registration succeeded. A bug.
    local-stricter   validate failed in a local parser (fastavro, jsonschema, protoc),
                     registration succeeded. Look at each: a known difference or a bug.
    FALSE-PASS       validate passed, registration failed. A bug.
    INTERNAL         validate raised, or reported an internal error. A bug.

Run it against the throwaway stack only; it registers a few thousand subjects:

    docker compose -p validator-it -f integration/docker-compose.yml up -d
    .venv/bin/python integration/differential.py --cases 500 --seed 1 --out /tmp/diff.tsv

The same --seed gives the same schemas (under a new namespace), so a finding can be rerun.
"""
from __future__ import annotations

import argparse
import copy
import dataclasses
import json
import random
import sys
import urllib.parse
import uuid
from collections import Counter
from collections.abc import Callable
from typing import Any

import requests

from schema_validator import (
    HttpSchemaRegistryClient,
    ReferenceInput,
    ValidationRequest,
    ValidatorConfig,
    validate,
)
from schema_validator.formats import ANALYSERS
from schema_validator.models import proto_import_path

_HEADERS = {"Content-Type": "application/vnd.schemaregistry.v1+json"}

# Message prefixes of the local parsers. Any other error line is one of the validator's own rules.
_LOCAL_PARSER = ("Invalid Avro schema: ", "Invalid JSON schema: ", "Invalid Protobuf schema: ",
                 "Schema is not valid JSON: ")
_STAGE_E = ("Schema Registry rejected the schema", "Not ", "Could not reach", "The validator is not allowed")
_INTERNAL = "Internal validator error: "
_MISMATCH = "The name must match the schema. It should be '"


# --- Seeds. NS is replaced by the case's namespace, ADDR by the registered Address's. ---

AVRO_SEEDS: list[tuple[dict[str, Any], bool]] = [
    ({"type": "record", "name": "Order", "namespace": "NS", "fields": [
        {"name": "id", "type": "string"},
        {"name": "qty", "type": "int", "default": 1},
        {"name": "note", "type": ["null", "string"], "default": None},
        {"name": "status", "type": {"type": "enum", "name": "Status", "symbols": ["NEW", "PAID"]}, "default": "NEW"},
        {"name": "again", "type": "Status"},
        {"name": "hash", "type": {"type": "fixed", "name": "Md5", "size": 16}},
        {"name": "tags", "type": {"type": "array", "items": "string"}, "default": []},
        {"name": "attrs", "type": {"type": "map", "values": "long"}},
        {"name": "at", "type": {"type": "long", "logicalType": "timestamp-millis"}},
        {"name": "price", "type": {"type": "bytes", "logicalType": "decimal", "precision": 9, "scale": 2}},
        {"name": "line", "type": {"type": "record", "name": "Line", "fields": [
            {"name": "sku", "type": "string"}, {"name": "status", "type": "NS.Status"}]}},
        {"name": "parent", "type": ["null", "Order"], "default": None},
    ]}, False),
    ({"type": "record", "name": "Customer", "namespace": "NS", "fields": [
        {"name": "id", "type": "string"},
        {"name": "address", "type": "ADDR.Address"},
        {"name": "others", "type": {"type": "array", "items": "ADDR.Address"}},
    ]}, True),
]

JSON_SEEDS: list[tuple[dict[str, Any], bool]] = [
    ({"$schema": "http://json-schema.org/draft-07/schema#", "title": "NS.Order", "type": "object",
      "properties": {
          "id": {"type": "string", "minLength": 1},
          "qty": {"type": "integer", "minimum": 0},
          "status": {"enum": ["NEW", "PAID"]},
          "tags": {"type": "array", "items": {"type": "string"}, "uniqueItems": True},
          "line": {"$ref": "#/definitions/line"},
          "when": {"type": "string", "format": "date-time"},
          "extra": {"oneOf": [{"type": "null"}, {"type": "object", "additionalProperties": {"type": "number"}}]},
      },
      "required": ["id"], "additionalProperties": False,
      "definitions": {"line": {"type": "object", "properties": {"sku": {"type": "string", "pattern": "^[A-Z]+$"}}}}},
     False),
    ({"$schema": "https://json-schema.org/draft/2020-12/schema", "title": "NS.Customer", "type": "object",
      "properties": {"id": {"type": "string"}, "address": {"$ref": "address.json"},
                     "city": {"$ref": "address.json#/properties/city"}}},
     True),
]

PROTO_SEEDS: list[tuple[str, bool]] = [
    ('''syntax = "proto3";
package NS;
import "google/protobuf/timestamp.proto";
option java_package = "org.example.gen";
message Order {
  string id = 1;
  int32 qty = 2;
  repeated string tags = 3;
  map<string, int64> attrs = 4;
  google.protobuf.Timestamp at = 5;
  Status status = 6;
  Line line = 7;
  oneof pay { string card = 8; string iban = 9; }
  optional string note = 10;
  enum Status { NEW = 0; PAID = 1; }
  message Line { string sku = 1; Status status = 2; }
  reserved 20, 30 to 40;
}
''', False),
    ('''syntax = "proto3";
package NS;
import "ADDRPATH";
message Customer {
  string id = 1;
  ADDR.Address address = 2;
  repeated ADDR.Address others = 3;
}
''', True),
]

ADDRESS = {
    "AVRO": json.dumps({"type": "record", "name": "Address", "namespace": "ADDR", "fields": [
        {"name": "city", "type": "string"},
        {"name": "country", "type": {"type": "enum", "name": "Country", "symbols": ["NL", "US"]}}]}),
    "JSON": json.dumps({"$schema": "http://json-schema.org/draft-07/schema#", "title": "ADDR.Address",
                        "type": "object", "properties": {"city": {"type": "string"}}}),
    "PROTOBUF": 'syntax = "proto3";\npackage ADDR;\nmessage Address { string city = 1; }\n',
}

# --- Mutations of a JSON tree (Avro and JSON Schema) ------------------------------------

_SCALARS: list[Any] = [None, 5, -1, 1.5, True, "", "string", "int", "long", "null", "bytes", "record", "enum",
                       "Nope", "NS.Nope", ".X", "com..X", "X.", "a b", "T-6", "9x", "üni", [], {}, ["null"],
                       "object", "array", "number", "strin", "#/definitions/line", "#/nope", "other.json", "#"]
_INSERTS: dict[str, list[tuple[str, Any]]] = {
    "AVRO": [("namespace", ""), ("namespace", "other.ns"), ("namespace", "bad..ns"), ("default", 5), ("default", None),
             ("default", "x"), ("default", {}), ("aliases", ["Old"]), ("aliases", "Old"), ("doc", "text"), ("doc", 5),
             ("order", "ascending"), ("order", "up"), ("logicalType", "uuid"), ("logicalType", "decimal"),
             ("logicalType", "date"), ("logicalType", "nope"), ("size", -1), ("symbols", []), ("symbols", ["A", "A"]),
             ("symbols", ["1A"]), ("name", "NS.Other"), ("name", "Order"), ("type", ["null", "string"]),
             ("type", ["string", "string"]), ("fields", []), ("precision", 0), ("scale", 99), ("custom", {"a": 1})],
    "JSON": [("$ref", "#/definitions/line"), ("$ref", "#/nope"), ("$ref", "other.json"), ("$ref", ""), ("$ref", 5),
             ("type", "strin"), ("type", ["string", "null"]), ("type", 5), ("required", "id"), ("required", ["zz"]),
             ("required", ["id", "id"]), ("minimum", "a"), ("minLength", -1), ("pattern", "["), ("enum", []),
             ("enum", "x"), ("format", "nope"), ("items", []), ("items", 5), ("additionalProperties", 5),
             ("properties", []), ("definitions", 5), ("$defs", {"x": {"type": "string"}}), ("const", None),
             ("title", "No Dots"), ("title", "a.b"), ("title", 5), ("$schema", "http://json-schema.org/draft-03/schema#"),
             ("$schema", "http://json-schema.org/draft-04/schema#"), ("$schema", "https://example.com/x"),
             ("$id", "http://example.com/order.json"), ("allOf", []), ("anyOf", [{"type": "string"}]), ("not", 5),
             ("dependencies", {"id": ["qty"]}), ("if", {"type": "object"}), ("default", {"id": 5})],
}


def _paths(node: Any, path: tuple = ()) -> list[tuple]:
    """Every path in the tree, parents before children."""
    found = [path]
    if isinstance(node, dict):
        for key, value in node.items():
            found.extend(_paths(value, path + (key,)))
    elif isinstance(node, list):
        for i, value in enumerate(node):
            found.extend(_paths(value, path + (i,)))
    return found


def _get(node: Any, path: tuple) -> Any:
    for step in path:
        node = node[step]
    return node


def _set(root: Any, path: tuple, value: Any) -> Any:
    if not path:
        return value
    _get(root, path[:-1])[path[-1]] = value
    return root


def mutate_tree(root: Any, schema_type: str, rng: random.Random) -> Any:
    root = copy.deepcopy(root)
    paths = _paths(root)
    path = rng.choice(paths)
    target = _get(root, path)
    op = rng.choice(["delete", "scalar", "insert", "insert", "wrap", "dup", "swap", "retype"])
    if op == "delete" and path:
        del _get(root, path[:-1])[path[-1]]
    elif op == "scalar":
        root = _set(root, path, rng.choice(_SCALARS))
    elif op == "insert":
        dicts = [p for p in paths if isinstance(_get(root, p), dict)]
        if dicts:
            key, value = rng.choice(_INSERTS[schema_type])
            _get(root, rng.choice(dicts))[key] = copy.deepcopy(value)
    elif op == "wrap" and path:
        wrapped = rng.choice([
            ["null", target], [target, "null"], [target, target], {"type": "array", "items": target},
            {"type": "map", "values": target}, {"type": target}, [[target]], {"oneOf": [target]},
        ])
        root = _set(root, path, wrapped)
    elif op in ("dup", "swap"):
        lists = [p for p in paths if isinstance(_get(root, p), list) and len(_get(root, p)) > 0]
        if lists:
            items = _get(root, rng.choice(lists))
            if op == "dup":
                items.append(copy.deepcopy(rng.choice(items)))
            else:
                rng.shuffle(items)
    elif op == "retype":
        typed = [p for p in paths if p and p[-1] == "type" and isinstance(_get(root, p), str)]
        if typed:
            pool = {"AVRO": ["string", "int", "long", "double", "boolean", "bytes", "null", "float", "Status", "Line",
                             "Md5", "Order", "NS.Line", "ADDR.Address", "Address", "Country", "record", "enum",
                             "fixed", "array", "map"],
                    "JSON": ["string", "integer", "number", "boolean", "object", "array", "null"]}[schema_type]
            root = _set(root, rng.choice(typed), rng.choice(pool))
    return root


# --- Mutations of Protobuf text ------------------------------------------------------------

_PROTO_REPLACE = [("string", "strin"), ("= 1;", "= 0;"), ("= 2;", "= 1;"), ("proto3", "proto2"), ("proto3", "proto4"),
                  ("NEW = 0", "NEW = 1"), ("int32", "sint64"), ("repeated ", ""), ("optional ", "required "),
                  ("map<string, int64>", "map<float, int64>"), ("Status status", "Nope status"), (";", "", ),
                  ("message Order", "message order"), ("package NS;", ""), ("package NS;", "package NS.sub;"),
                  ("google.protobuf.Timestamp", ".google.protobuf.Timestamp"), ("Order", "Ord-er"),
                  ('import "google/protobuf/timestamp.proto";', ""), ("ADDR.Address", "Address"),
                  ("timestamp.proto", "duration.proto"), ("reserved 20", "reserved 1"), ("{", "{ {")]
_PROTO_ADD = ['import "google/type/date.proto";', 'import "nope/missing.proto";', 'import public "google/protobuf/any.proto";',
              'import "confluent/meta.proto";', 'import "../x.proto";', "message Second { string a = 1; }",
              "enum Top { A = 0; }", "enum Bad { A = 1; }", "service S { rpc Get (Order) returns (Order); }",
              'option java_multiple_files = true;', "extend Order { string z = 100; }", "// a comment", "}", "\x00"]
_PROTO_FIELD = ["google.type.Date d = 50;", "string id = 51;", "string dup = 1;", "bytes b = 19000;",
                "string big = 536870912;", "Order self = 52;", "repeated Line lines = 53;", "NS.Order.Line q = 54;",
                "google.protobuf.Any any = 55;", "map<string, Line> m = 56;", "string json_name = 57 [json_name = \"id\"];",
                "int32 d2 = 58 [deprecated = true];", "group G = 59 { }", "string Id = 60;"]


def mutate_proto(text: str, rng: random.Random) -> str:
    lines = text.split("\n")
    op = rng.choice(["replace", "replace", "delete", "dup", "add", "field", "field"])
    if op == "replace":
        old, new = rng.choice(_PROTO_REPLACE)
        return text.replace(old, new, 1)
    if op == "delete":
        del lines[rng.randrange(len(lines))]
    elif op == "dup":
        i = rng.randrange(len(lines))
        lines.insert(i, lines[i])
    elif op == "add":
        lines.insert(rng.randrange(2, len(lines) + 1), rng.choice(_PROTO_ADD))
    else:
        opening = [i for i, line in enumerate(lines) if line.rstrip().endswith("{")]
        if opening:
            lines.insert(rng.choice(opening) + 1, "  " + rng.choice(_PROTO_FIELD))
    return "\n".join(lines)


# --- One case ----------------------------------------------------------------------------

def _place(text: str, ns: str, addr: str) -> str:
    return text.replace("ADDRPATH", proto_import_path(f"{addr}.Address")).replace("ADDR", addr).replace("NS", ns)


def build_case(schema_type: str, ns: str, addr: str, rng: random.Random) -> tuple[str, list[ReferenceInput]]:
    """A schema text and the reference rows a user would select for the unmutated seed."""
    mutations = rng.choice([0, 1, 1, 1, 2, 2, 3])
    if schema_type == "PROTOBUF":
        text, uses_address = rng.choice(PROTO_SEEDS)
        for _ in range(mutations):
            text = mutate_proto(text, rng)
    else:
        tree, uses_address = rng.choice(AVRO_SEEDS if schema_type == "AVRO" else JSON_SEEDS)
        for _ in range(mutations):
            tree = mutate_tree(tree, schema_type, rng)
        text = json.dumps(tree)
    rows = []
    # Now and then the row is left out, or selected although the seed does not use it.
    if uses_address != (rng.random() < 0.1):
        rows = [ReferenceInput(f"{addr}.Address", "address.json" if schema_type == "JSON" else None, 1)]
    return _place(text, ns, addr), rows


def register(sr_url: str, subject: str, text: str, schema_type: str, rows: list[ReferenceInput]) -> tuple[int, str]:
    references = []
    for row in rows:
        name = {"AVRO": row.schema_name, "PROTOBUF": proto_import_path(row.schema_name)}.get(schema_type) \
            or row.ref_string
        references.append({"name": name, "subject": row.schema_name, "version": -1})
    body: dict[str, Any] = {"schema": text, "schemaType": schema_type}
    if references:
        body["references"] = references
    response = requests.post(f"{sr_url}/subjects/{urllib.parse.quote(subject, safe='')}/versions",
                             headers=_HEADERS, data=json.dumps(body), timeout=30)
    return response.status_code, response.text


def classify(valid: bool, message: str, registered: bool) -> str:
    lines = [line for line in message.split("\n") if line]
    if any(line.startswith(_INTERNAL) for line in lines):
        return "INTERNAL"
    if valid:
        return "agree-pass" if registered else "FALSE-PASS"
    if not registered:
        return "agree-reject"
    if any(line.startswith(_LOCAL_PARSER) for line in lines):
        return "local-stricter"
    if all(line.startswith(_STAGE_E) for line in lines):
        return "stage-e-only"
    return "own-rule"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--sr-url", default="http://localhost:18081")
    parser.add_argument("--cases", type=int, default=300, help="cases per format")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--out", default="differential.tsv", help="one row per case that is not an agreement")
    args = parser.parse_args()

    sr_url = args.sr_url.rstrip("/")
    config = ValidatorConfig(sr_url=sr_url)
    client = HttpSchemaRegistryClient.from_config(config)
    run_id = "d" + uuid.uuid4().hex[:8]
    counts: Counter[tuple[str, str]] = Counter()
    rows_out: list[str] = ["category\tformat\tcase\tvalidator\tregistry\tschema"]

    for schema_type in ("AVRO", "JSON", "PROTOBUF"):
        rng = random.Random(f"{args.seed}-{schema_type}")
        addr = f"{run_id}.shared{schema_type.lower()}"
        status, body = register(sr_url, f"{addr}.Address", ADDRESS[schema_type].replace("ADDR", addr), schema_type, [])
        if status != 200:
            print(f"could not register the shared Address ({status}): {body}", file=sys.stderr)
            return 2
        for i in range(args.cases):
            ns = f"{run_id}.c{i}"
            text, rows = build_case(schema_type, ns, addr, rng)
            # The name a user would type: the one the schema itself gives, when it gives one.
            try:
                derived = ANALYSERS[schema_type](text, {}, config).expected_name
            except Exception:
                derived = None
            name = derived or f"{ns}.Unnamed"
            request = ValidationRequest(name=name, schema_type=schema_type, schema=text, compatibility="BACKWARD",
                                        references=rows, previous_name=None, existing_names=[])
            try:
                result = validate(request, client, config)
                # With references the name is only known after they are loaded: take it from C3.
                if result.message.startswith(_MISMATCH) and result.message.endswith("'."):
                    name = result.message[len(_MISMATCH):-2]
                    request = dataclasses.replace(request, name=name)
                    result = validate(request, client, config)
                valid, message = result.valid, result.message
            except Exception as e:  # validate must never raise
                valid, message = False, f"{_INTERNAL}raised {type(e).__name__}: {e}"
            status, body = register(sr_url, name, text, schema_type, rows)
            category = classify(valid, message, status == 200)
            counts[(schema_type, category)] += 1
            if not category.startswith("agree"):
                flat: Callable[[str], str] = lambda s: s.replace("\t", " ").replace("\n", " | ")
                registry = "registered" if status == 200 else f"{status} {flat(body)[:300]}"
                rows_out.append("\t".join([category, schema_type, f"seed={args.seed} i={i}",
                                           flat(message)[:400], registry, flat(text)]))

    with open(args.out, "w", encoding="utf-8") as out:
        out.write("\n".join(rows_out) + "\n")
    categories = ["agree-pass", "agree-reject", "own-rule", "local-stricter", "stage-e-only", "FALSE-PASS", "INTERNAL"]
    print(f"{'format':10}" + "".join(f"{c:>16}" for c in categories))
    for schema_type in ("AVRO", "JSON", "PROTOBUF"):
        print(f"{schema_type:10}" + "".join(f"{counts[(schema_type, c)]:>16}" for c in categories))
    print(f"details: {args.out}")
    bugs = sum(n for (_, c), n in counts.items() if c in ("FALSE-PASS", "INTERNAL", "stage-e-only"))
    return 1 if bugs else 0


if __name__ == "__main__":
    sys.exit(main())
