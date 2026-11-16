"""Schema Registry client protocol, value types and errors."""
from __future__ import annotations

import json
import urllib.parse
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

import requests

from schema_validator.models import ValidatorConfig


@dataclass(frozen=True)
class SrReference:
    name: str
    subject: str
    version: int


@dataclass(frozen=True)
class SchemaVersion:
    subject: str
    version: int
    schema_type: str
    schema: str
    references: tuple[SrReference, ...] = ()


@dataclass(frozen=True)
class CompatibilityVerdict:
    is_compatible: bool
    messages: tuple[str, ...] = ()


class SchemaRegistryError(Exception):
    """Base class for Schema Registry failures."""


class SchemaRegistryUnavailable(SchemaRegistryError):
    """Timeout, connection error, 5xx or unreadable body (E3)."""


class SchemaRegistryUnauthorized(SchemaRegistryError):
    """401 or 403 (E4)."""


class SchemaRegistryRejected(SchemaRegistryError):
    """Other 4xx with an error_code; str(e) is SR's message (E1)."""


class SchemaRegistryClient(Protocol):
    def get_latest(self, subject: str) -> SchemaVersion | None:
        """Latest version of the subject, or None on 404."""
        ...

    def get_version(self, subject: str, version: int) -> SchemaVersion | None:
        """A specific version, or None on 404."""
        ...

    def get_compatibility_level(self, subject: str) -> str: ...

    def check_compatibility(
        self,
        subject: str,
        schema: str,
        schema_type: str,
        references: Sequence[SrReference],
    ) -> CompatibilityVerdict: ...


_CONTENT_TYPE = "application/vnd.schemaregistry.v1+json"

# Returned by _call for a 404 when the caller asked for it; distinct from a JSON `null` body.
_NOT_FOUND = object()

# Exception texts below are diagnostics, not user-facing messages: stage E maps
# each exception class to its constant in messages.py. They name the method and
# path only, never the base URL (it may carry user info), a header or a body.


class HttpSchemaRegistryClient:
    """SchemaRegistryClient over Schema Registry's REST API (spec section 7).

    Every response field is treated as untrusted: a body of the wrong shape is
    SchemaRegistryUnavailable, never a TypeError or KeyError.
    """

    def __init__(self, url: str, username: str | None = None, password: str | None = None,
                 timeout_seconds: float = 10, session: requests.Session | None = None) -> None:
        self._url = url.rstrip("/")
        self._auth = (username, password or "") if username else None
        self._timeout = timeout_seconds
        self._session = session if session is not None else requests.Session()

    @classmethod
    def from_config(cls, config: ValidatorConfig) -> HttpSchemaRegistryClient:
        return cls(config.sr_url, config.sr_username, config.sr_password, config.sr_timeout_seconds)

    def get_latest(self, subject: str) -> SchemaVersion | None:
        return self._get_version(f"/subjects/{_quote(subject)}/versions/latest")

    def get_version(self, subject: str, version: int) -> SchemaVersion | None:
        return self._get_version(f"/subjects/{_quote(subject)}/versions/{version}")

    def get_compatibility_level(self, subject: str) -> str:
        path = f"/config/{_quote(subject)}?defaultToGlobal=true"
        body = self._call("GET", path)
        level = body.get("compatibilityLevel") if isinstance(body, dict) else None
        if not isinstance(level, str):
            raise _bad_body("GET", path)
        return level

    def check_compatibility(
        self,
        subject: str,
        schema: str,
        schema_type: str,
        references: Sequence[SrReference],
    ) -> CompatibilityVerdict:
        request: dict[str, object] = {"schema": schema, "schemaType": schema_type}
        if references:
            request["references"] = [
                {"name": r.name, "subject": r.subject, "version": r.version} for r in references
            ]
        path = f"/compatibility/subjects/{_quote(subject)}/versions?verbose=true"
        body = self._call("POST", path, request)
        if not isinstance(body, dict):
            raise _bad_body("POST", path)
        is_compatible = body.get("is_compatible")
        messages = body.get("messages")
        if messages is None:
            messages = []
        if (not isinstance(is_compatible, bool) or not isinstance(messages, list)
                or not all(isinstance(m, str) for m in messages)):
            raise _bad_body("POST", path)
        return CompatibilityVerdict(is_compatible, tuple(messages))

    def _get_version(self, path: str) -> SchemaVersion | None:
        body = self._call("GET", path, not_found_ok=True)
        if body is _NOT_FOUND:
            return None
        version = _parse_version(body)
        if version is None:
            raise _bad_body("GET", path)
        return version

    def _call(self, method: str, path: str, request: object = None,
              not_found_ok: bool = False) -> object:
        """Send the request and return the parsed 2xx body, or _NOT_FOUND for a 404
        when `not_found_ok`. Raises a SchemaRegistryError for every other outcome."""
        where = f"{method} {path}"
        try:
            response = self._session.request(
                method,
                self._url + path,
                headers={"Content-Type": _CONTENT_TYPE},
                auth=self._auth,
                timeout=self._timeout,
                data=None if request is None else json.dumps(request),
            )
        except requests.RequestException as e:
            raise SchemaRegistryUnavailable(f"{where}: {type(e).__name__}") from None
        status = response.status_code
        if status in (401, 403):
            raise SchemaRegistryUnauthorized(f"{where}: HTTP {status}")
        if status >= 500:
            raise SchemaRegistryUnavailable(f"{where}: HTTP {status}")
        if 400 <= status < 500:
            if status == 404 and not_found_ok:
                return _NOT_FOUND
            raise SchemaRegistryRejected(_error_message(response))
        if not 200 <= status < 300:
            raise SchemaRegistryUnavailable(f"{where}: HTTP {status}")
        try:
            return response.json()
        except (ValueError, RecursionError):
            raise _bad_body(method, path) from None


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _quote(subject: str) -> str:
    return urllib.parse.quote(subject, safe="")


def _bad_body(method: str, path: str) -> SchemaRegistryUnavailable:
    return SchemaRegistryUnavailable(f"{method} {path}: unexpected response body")


def _error_message(response: requests.Response) -> str:
    """The `message` of a Schema Registry error body, else the raw text."""
    try:
        body = response.json()
    except (ValueError, RecursionError):
        body = None
    if isinstance(body, dict):
        message = body.get("message")
        if isinstance(message, str) and message:
            return message
    return response.text or f"HTTP {response.status_code}"


def _parse_version(body: object) -> SchemaVersion | None:
    """A SchemaVersion from a /versions body, or None if the body has the wrong shape."""
    if not isinstance(body, dict):
        return None
    subject = body.get("subject")
    version = body.get("version")
    schema = body.get("schema")
    schema_type = body.get("schemaType", "AVRO")
    raw_refs = body.get("references", [])
    if (not isinstance(subject, str) or not _is_int(version) or not isinstance(schema, str)
            or not isinstance(schema_type, str) or not isinstance(raw_refs, list)
            or ("id" in body and not _is_int(body["id"]))):
        return None
    refs = []
    for ref in raw_refs:
        if not isinstance(ref, dict):
            return None
        name, ref_subject, ref_version = ref.get("name"), ref.get("subject"), ref.get("version")
        if not isinstance(name, str) or not isinstance(ref_subject, str) or not _is_int(ref_version):
            return None
        refs.append(SrReference(name, ref_subject, ref_version))
    return SchemaVersion(subject, version, schema_type, schema, tuple(refs))
