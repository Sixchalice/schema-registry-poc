"""Shared setup for the tests against the Docker stack in integration/docker-compose.yml.

Start the stack with `docker compose -p validator-it -f integration/docker-compose.yml up -d`.
"""
from __future__ import annotations

import json
import os
import urllib.parse
import uuid
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path

import pytest
import requests

from schema_validator import HttpSchemaRegistryClient, ValidatorConfig

SR_URL = os.environ.get("SR_URL", "http://localhost:18081").rstrip("/")
KAFKA_BOOTSTRAP = os.environ.get("KAFKA_BOOTSTRAP", "localhost:19092")
_HEADERS = {"Content-Type": "application/vnd.schemaregistry.v1+json"}
_HERE = Path(__file__).resolve().parent

# (status code, parsed JSON body or the raw text)
Response = tuple[int, object]
Register = Callable[..., Response]


def _sr_unreachable_reason() -> str | None:
    try:
        requests.get(f"{SR_URL}/subjects", timeout=5).raise_for_status()
    except requests.RequestException as e:
        return f"Schema Registry at {SR_URL} is not reachable ({type(e).__name__}); start integration/docker-compose.yml"
    return None


@pytest.hookimpl(tryfirst=True)  # before `-m` deselection runs
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    ours = [item for item in items if _HERE in Path(item.path).resolve().parents]
    if not ours:
        return
    reason = _sr_unreachable_reason()
    for item in ours:
        item.add_marker(pytest.mark.integration)
        if reason is not None:
            item.add_marker(pytest.mark.skip(reason=reason))


def sr_call(method: str, path: str, body: object = None) -> Response:
    """A raw call to the test registry; `path` must already be URL-encoded."""
    response = requests.request(
        method, SR_URL + path, headers=_HEADERS,
        data=None if body is None else json.dumps(body), timeout=20,
    )
    try:
        return response.status_code, response.json()
    except ValueError:
        return response.status_code, response.text


def quote(subject: str) -> str:
    return urllib.parse.quote(subject, safe="")


def _register(subject: str, schema: str, schema_type: str = "AVRO",
              references: Sequence[dict[str, object]] | None = None) -> Response:
    body: dict[str, object] = {"schema": schema, "schemaType": schema_type}
    if references:
        body["references"] = list(references)
    return sr_call("POST", f"/subjects/{quote(subject)}/versions", body)


@pytest.fixture(autouse=True)
def _clean_validator_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """No SCHEMA_VALIDATOR_* variable from the shell reaches ValidatorConfig (or a subprocess)."""
    for name in list(os.environ):
        if name.startswith("SCHEMA_VALIDATOR_"):
            monkeypatch.delenv(name)


@pytest.fixture
def ns() -> str:
    """A namespace no earlier run has used, so subjects never collide."""
    return "it" + uuid.uuid4().hex[:10]


@pytest.fixture
def sr() -> HttpSchemaRegistryClient:
    return HttpSchemaRegistryClient(SR_URL)


@pytest.fixture
def register() -> Register:
    """register(subject, schema_text, schema_type="AVRO", references=None) -> (status, body)."""
    return _register


@pytest.fixture
def raw_get() -> Callable[[str], Response]:
    """raw_get(path) -> (status, body); `path` must already be URL-encoded."""
    return lambda path: sr_call("GET", path)


@pytest.fixture
def set_subject_level() -> Callable[[str, str], Response]:
    """PUT /config/{subject} with the given compatibility level."""
    def put(subject: str, level: str) -> Response:
        return sr_call("PUT", f"/config/{quote(subject)}", {"compatibility": level})
    return put


@pytest.fixture
def set_subject_mode() -> Iterator[Callable[[str, str], Response]]:
    """PUT /mode/{subject}; every subject set this way gets its own mode removed afterwards."""
    touched: list[str] = []

    def put(subject: str, mode: str) -> Response:
        touched.append(subject)
        return sr_call("PUT", f"/mode/{quote(subject)}", {"mode": mode})

    yield put
    for subject in touched:
        sr_call("DELETE", f"/mode/{quote(subject)}")


@pytest.fixture(scope="session")
def kafka_bootstrap() -> str:
    return KAFKA_BOOTSTRAP


@pytest.fixture
def config() -> ValidatorConfig:
    """The validator's configuration for the test registry (no authentication)."""
    return ValidatorConfig(sr_url=SR_URL)
