"""Tiny helper for the probe scripts: raw calls to the throwaway Schema Registry."""
import json
import os
import urllib.parse

import requests

SR = os.environ.get("SR_URL", "http://localhost:18081")
H = {"Content-Type": "application/vnd.schemaregistry.v1+json"}


def q(subject: str) -> str:
    return urllib.parse.quote(subject, safe="")


def call(method: str, path: str, body=None):
    r = requests.request(method, SR + path, headers=H, data=None if body is None else json.dumps(body), timeout=20)
    try:
        payload = r.json()
    except ValueError:
        payload = r.text
    return r.status_code, payload


def show(label: str, res) -> None:
    status, payload = res
    text = json.dumps(payload) if not isinstance(payload, str) else payload
    if len(text) > 420:
        text = text[:420] + " ...[cut]"
    print(f"{label:<58} -> {status} {text}")


def body(schema, schema_type="AVRO", references=None):
    b = {"schema": schema if isinstance(schema, str) else json.dumps(schema)}
    if schema_type != "AVRO":
        b["schemaType"] = schema_type
    if references:
        b["references"] = references
    return b


def register(subject, schema, schema_type="AVRO", references=None):
    return call("POST", f"/subjects/{q(subject)}/versions", body(schema, schema_type, references))


def compat(subject, schema, schema_type="AVRO", references=None, version=None):
    path = f"/compatibility/subjects/{q(subject)}/versions"
    if version is not None:
        path += f"/{version}"
    return call("POST", path + "?verbose=true", body(schema, schema_type, references))
