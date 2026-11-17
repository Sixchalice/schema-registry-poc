"""Schema Registry client protocol, value types and errors."""
from __future__ import annotations

import json
import time
import urllib.parse
from collections.abc import Iterator, Sequence
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
    """Timeout, connection error, 5xx, unreadable body, or a 4xx not from Schema Registry (E3)."""


class SchemaRegistryUnauthorized(SchemaRegistryError):
    """401 or 403 (E4)."""


class SchemaRegistryRejected(SchemaRegistryError):
    """Other 4xx with an error_code (E1).

    str(e) is the body's `message` field, or "" when it has none; the raw body is
    never kept, since it may echo request headers.
    """

    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.code = code


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
# Schema Registry's 404 error codes: subject not found, version not found.
_NOT_FOUND_CODES = (40401, 40402)

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
        return self._get_version("/subjects/{s}/versions/latest", subject)

    def get_version(self, subject: str, version: int) -> SchemaVersion | None:
        return self._get_version(f"/subjects/{{s}}/versions/{version}", subject)

    def get_compatibility_level(self, subject: str) -> str:
        path = "/config/{s}?defaultToGlobal=true"
        body = self._call("GET", path, subject)
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
        path = "/compatibility/subjects/{s}/versions?verbose=true"
        body = self._call("POST", path, subject, request)
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

    def _get_version(self, path: str, subject: str) -> SchemaVersion | None:
        body = self._call("GET", path, subject, not_found_ok=True)
        if body is _NOT_FOUND:
            return None
        version = _parse_version(body)
        if version is None:
            raise _bad_body("GET", path)
        return version

    def _call(self, method: str, path: str, subject: str, request: object = None,
              not_found_ok: bool = False) -> object:
        """Send the request and return the parsed 2xx body, or _NOT_FOUND for a
        Schema Registry "subject/version not found" 404 when `not_found_ok`.

        `path` holds a `{s}` placeholder for the URL-encoded subject; diagnostics
        show the placeholder, not the subject. Raises a SchemaRegistryError for
        every other outcome.
        """
        where = f"{method} {path}"
        started = time.monotonic()
        response = None
        try:
            try:
                # Inside the guard: quoting raises UnicodeEncodeError on a lone surrogate.
                url = self._url + path.replace("{s}", _quote(subject))
                response = self._session.request(
                    method,
                    url,
                    headers={"Content-Type": _CONTENT_TYPE},
                    auth=self._auth,
                    timeout=self._timeout,
                    data=None if request is None else json.dumps(request),
                    allow_redirects=False,
                    stream=True,
                )
                # `timeout` bounds connect and each read, not the whole response: a
                # server that trickles bytes would hold the call open, so check the
                # clock after every piece of the body.
                chunks = []
                for chunk in _pieces(response):
                    chunks.append(chunk)
                    if time.monotonic() > started + self._timeout:
                        raise SchemaRegistryUnavailable(
                            f"{where}: response took longer than the timeout")
                raw = b"".join(chunks)
            except (requests.RequestException, ValueError) as e:
                # ValueError covers UnicodeEncodeError from requests' Latin-1 Basic header
                # (its repr holds the password) and urllib3's timeout <= 0 check.
                raise SchemaRegistryUnavailable(f"{where}: {type(e).__name__}") from None
            status = response.status_code
            if status in (401, 403):
                raise SchemaRegistryUnauthorized(f"{where}: HTTP {status}")
            if status >= 500:
                raise SchemaRegistryUnavailable(f"{where}: HTTP {status}")
            if 400 <= status < 500:
                body = _parse(raw)
                code = _error_code(body)
                if status == 404 and code not in _NOT_FOUND_CODES:
                    # Not Schema Registry's own "not found": a wrong URL or a proxy, on every call.
                    raise SchemaRegistryUnavailable(f"{where}: HTTP 404 without a not-found error_code")
                if status == 404 and not_found_ok:
                    return _NOT_FOUND
                if code is None:
                    # Schema Registry always sends an error_code: this answer is from something else.
                    raise SchemaRegistryUnavailable(f"{where}: HTTP {status} without an error_code")
                raise SchemaRegistryRejected(_error_message(body), code)
            if not 200 <= status < 300:
                raise SchemaRegistryUnavailable(f"{where}: HTTP {status}")
            body = _parse(raw)
            if body is _UNPARSEABLE:
                raise _bad_body(method, path)
            return body
        finally:
            if response is not None:
                response.close()


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _quote(subject: str) -> str:
    return urllib.parse.quote(subject, safe="")


def _bad_body(method: str, path: str) -> SchemaRegistryUnavailable:
    return SchemaRegistryUnavailable(f"{method} {path}: unexpected response body")


_UNPARSEABLE = object()
_READ_SIZE = 65536


def _pieces(response: requests.Response) -> Iterator[bytes]:
    """The body in pieces, each handed over as soon as it arrives.

    urllib3 2.x `read1` returns what one underlying read gives, so a server that
    drips bytes yields small pieces and the caller's deadline check runs between
    them. `iter_content` waits for a full chunk (or the end of the body) before it
    returns, so it only works piece by piece at chunk_size=1; that is the fallback
    for urllib3 1.x, which has no `read1`.
    """
    read1 = getattr(response.raw, "read1", None)
    if read1 is None:
        yield from response.iter_content(chunk_size=1)
        return
    while True:
        piece = read1(_READ_SIZE, decode_content=True)
        if not piece:
            return
        yield piece


def _parse(raw: bytes) -> object:
    """The JSON in `raw`, or _UNPARSEABLE."""
    try:
        return json.loads(raw)
    except (ValueError, RecursionError):
        return _UNPARSEABLE


def _error_code(body: object) -> int | None:
    """The integer `error_code` of a Schema Registry error body, if it has one."""
    code = body.get("error_code") if isinstance(body, dict) else None
    return code if _is_int(code) else None


def _error_message(body: object) -> str:
    """The `message` of a Schema Registry error body, or "" when it has none."""
    message = body.get("message") if isinstance(body, dict) else None
    return message if isinstance(message, str) else ""


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
