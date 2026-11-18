"""Schema Registry client protocol, value types and errors."""
from __future__ import annotations

import json
import math
import socket
import threading
import time
import urllib.parse
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import requests
import requests.adapters
import urllib3.connection
import urllib3.connectionpool
import urllib3.exceptions

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


class SchemaRegistryConfigError(ValueError):
    """The client's configuration cannot work (raised at construction, not by validate)."""


class SchemaRegistryClient(Protocol):
    def get_latest(self, subject: str) -> SchemaVersion | None:
        """Latest version of the subject, or None on 404."""
        ...

    def get_version(self, subject: str, version: int) -> SchemaVersion | None:
        """A specific version, or None on 404."""
        ...

    def get_compatibility_level(self, subject: str) -> str: ...

    def get_mode(self, subject: str) -> str:
        """The subject's mode, or the global mode when it has none of its own."""
        ...

    def check_compatibility(
        self,
        subject: str,
        schema: str,
        schema_type: str,
        references: Sequence[SrReference],
    ) -> CompatibilityVerdict: ...

    def registered_version(
        self,
        subject: str,
        schema: str,
        schema_type: str,
        references: Sequence[SrReference],
    ) -> int | None:
        """The version of the subject that holds this schema with these references, or None."""
        ...


_CONTENT_TYPE = "application/vnd.schemaregistry.v1+json"

# Returned by _call for a 404 when the caller asked for it; distinct from a JSON `null` body.
_NOT_FOUND = object()
# Schema Registry's 404 error codes: subject not found, version not found.
_NOT_FOUND_CODES = (40401, 40402)
# For the schema lookup (POST /subjects/{s}): subject not found, schema not found.
_LOOKUP_NOT_FOUND_CODES = (40401, 40403)

# Exception texts below are diagnostics, not user-facing messages: stage E maps
# each exception class to its constant in messages.py. They name the method and
# path only, never the base URL (it may carry user info), a header or a body.


class HttpSchemaRegistryClient:
    """SchemaRegistryClient over Schema Registry's REST API (spec section 7).

    Every response field is treated as untrusted: a body of the wrong shape is
    SchemaRegistryUnavailable, never a TypeError or KeyError.
    """

    def __init__(self, url: str, username: str | None = None, password: str | None = None,
                 timeout_seconds: float = 10, session: requests.Session | None = None,
                 max_response_bytes: int = 5_000_000) -> None:
        """Raises SchemaRegistryConfigError for a value no call could work with.

        `timeout_seconds` bounds each call as a whole, headers included: the client's own
        session shuts the call's socket down when it runs out (_CallWatch). A `session`
        passed in gets no such watchdog; its calls are bounded only by the per-read timeout
        and the clock checks between body pieces.
        """
        try:
            parts = urllib.parse.urlsplit(url)
            parts.port  # raises ValueError for a port that is not a number from 0 to 65535
        except ValueError:
            parts = None
        if (parts is None or parts.scheme not in ("http", "https") or not parts.hostname
                or any(c in "?#;" or c.isspace() or ord(c) < 32 for c in url)):
            raise SchemaRegistryConfigError("Schema Registry URL must be http:// or https:// with a host")
        if not (_is_number(timeout_seconds) and math.isfinite(timeout_seconds) and timeout_seconds > 0):
            raise SchemaRegistryConfigError("Schema Registry timeout must be a number of seconds above 0")
        if not _is_int(max_response_bytes) or max_response_bytes < 1:
            raise SchemaRegistryConfigError(
                "Schema Registry response limit must be a whole number of bytes, at least 1")
        for credential in (username, password):
            try:
                (credential or "").encode("latin-1")
            except UnicodeEncodeError:
                # requests sends Basic credentials as Latin-1; no call could succeed.
                raise SchemaRegistryConfigError(
                    "Schema Registry username and password must be Latin-1 text") from None
        self._url = url.rstrip("/")
        self._auth = (username, password or "") if username else None
        self._timeout = timeout_seconds
        self._max_response_bytes = max_response_bytes
        # Each thread's current _CallWatch, for the client's own session; None for a caller's.
        self._calls: threading.local | None = None
        if session is None:
            self._calls = threading.local()
            session = _watched_session(self._calls)
        self._session = session

    @classmethod
    def from_config(cls, config: ValidatorConfig) -> HttpSchemaRegistryClient:
        return cls(config.sr_url, config.sr_username, config.sr_password, config.sr_timeout_seconds,
                   max_response_bytes=config.max_response_bytes)

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

    def get_mode(self, subject: str) -> str:
        path = "/mode/{s}?defaultToGlobal=true"
        body = self._call("GET", path, subject)
        mode = body.get("mode") if isinstance(body, dict) else None
        if not isinstance(mode, str):
            raise _bad_body("GET", path)
        return mode

    def check_compatibility(
        self,
        subject: str,
        schema: str,
        schema_type: str,
        references: Sequence[SrReference],
    ) -> CompatibilityVerdict:
        request = _schema_request(schema, schema_type, references)
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

    def registered_version(
        self,
        subject: str,
        schema: str,
        schema_type: str,
        references: Sequence[SrReference],
    ) -> int | None:
        request = _schema_request(schema, schema_type, references)
        path = "/subjects/{s}"
        body = self._call("POST", path, subject, request, not_found_ok=True,
                          not_found_codes=_LOOKUP_NOT_FOUND_CODES)
        if body is _NOT_FOUND:
            return None
        version = body.get("version") if isinstance(body, dict) else None
        if not isinstance(version, int) or isinstance(version, bool):
            raise _bad_body("POST", path)
        return version

    def _get_version(self, path: str, subject: str) -> SchemaVersion | None:
        body = self._call("GET", path, subject, not_found_ok=True)
        if body is _NOT_FOUND:
            return None
        version = _parse_version(body)
        if version is None:
            raise _bad_body("GET", path)
        return version

    def _call(self, method: str, path: str, subject: str, request: object = None,
              not_found_ok: bool = False,
              not_found_codes: tuple[int, ...] = _NOT_FOUND_CODES) -> object:
        """Send the request and return the parsed 2xx body, or _NOT_FOUND for a
        Schema Registry "subject/version not found" 404 when `not_found_ok`.

        `path` holds a `{s}` placeholder for the URL-encoded subject; diagnostics
        show the placeholder, not the subject. Raises a SchemaRegistryError for
        every other outcome.
        """
        where = f"{method} {path}"
        started = time.monotonic()
        watch = None
        if self._calls is not None:
            watch = _CallWatch(self._timeout)
            self._calls.watch = watch
            watch.start()
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
                # `timeout` bounds connect and each read, not the whole response. The
                # watchdog bounds the whole call on the client's own session; for a
                # caller's session (and a body sent without a length, read after
                # http.client has let go of the socket) the clock is checked when the
                # headers are in, which also covers an empty body, and after every piece.
                _check_deadline(started, self._timeout, where)
                # The decoded size is counted too: neither an endless body nor a gzip
                # bomb is held in memory.
                chunks = []
                size = 0
                for chunk in _pieces(response):
                    size += len(chunk)
                    if size > self._max_response_bytes:
                        raise SchemaRegistryUnavailable(f"{where}: response larger than the limit")
                    chunks.append(chunk)
                    _check_deadline(started, self._timeout, where)
                raw = b"".join(chunks)
            except (requests.RequestException, urllib3.exceptions.HTTPError, OSError, ValueError) as e:
                # urllib3 errors and OSError come from reading the body (stream=True, so
                # requests does not wrap them), or from a socket the watchdog shut down.
                # ValueError covers value checks in urllib3 and http.client (the URL and
                # credentials are checked at construction).
                raise SchemaRegistryUnavailable(f"{where}: {type(e).__name__}") from None
            status = response.status_code
            if status in (401, 403):
                raise SchemaRegistryUnauthorized(f"{where}: HTTP {status}")
            if status >= 500:
                raise SchemaRegistryUnavailable(f"{where}: HTTP {status}")
            if 400 <= status < 500:
                body = _parse(raw)
                code = _error_code(body)
                if status == 404 and code not in not_found_codes:
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
            if watch is not None:
                watch.stop()
            if response is not None:
                response.close()


def _schema_request(schema: str, schema_type: str, references: Sequence[SrReference]) -> dict[str, object]:
    request: dict[str, object] = {"schema": schema, "schemaType": schema_type}
    if references:
        request["references"] = [
            {"name": r.name, "subject": r.subject, "version": r.version} for r in references
        ]
    return request


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _quote(subject: str) -> str:
    return urllib.parse.quote(subject, safe="")


def _bad_body(method: str, path: str) -> SchemaRegistryUnavailable:
    return SchemaRegistryUnavailable(f"{method} {path}: unexpected response body")


_UNPARSEABLE = object()
_READ_SIZE = 65536


def _check_deadline(started: float, timeout: float, where: str) -> None:
    if time.monotonic() > started + timeout:
        raise SchemaRegistryUnavailable(f"{where}: response took longer than the timeout")


class _CallWatch:
    """The watchdog of one call: once the call has run `timeout` seconds, it shuts down the
    socket of the connection the call is using, so a blocked read ends (audit 3 M1).

    The connection is recorded as it starts connecting and as each request starts on it,
    and forgotten when it goes back to the pool or the call ends, all under one lock, so
    the watchdog never reaches a connection that another call may be using. The timer
    fires once, possibly before the call has a socket (a slow DNS lookup, TCP connect or
    TLS handshake), so it also marks the watch expired: from then on, recording a
    connection or finishing a connect raises, and the call ends.
    """

    def __init__(self, timeout: float) -> None:
        self._lock = threading.Lock()
        self._connection: urllib3.connection.HTTPConnection | None = None
        self._expired = False
        self._timer = threading.Timer(timeout, self._abort)
        self._timer.daemon = True

    def start(self) -> None:
        self._timer.start()

    def record(self, connection: urllib3.connection.HTTPConnection) -> None:
        with self._lock:
            if self._expired:
                raise _CallExpired()
            self._connection = connection

    def connected(self) -> None:
        """Called after a connect returns: the connect ran past the timer, so the call ends."""
        with self._lock:
            if self._expired:
                raise _CallExpired()

    def release(self, connection: object) -> None:
        with self._lock:
            if self._connection is connection:
                self._connection = None

    def stop(self) -> None:
        self._timer.cancel()
        with self._lock:
            self._connection = None

    def _abort(self) -> None:
        with self._lock:
            self._expired = True
            sock = self._connection.sock if self._connection is not None else None
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass  # already closed


class _CallExpired(OSError):
    """The call's timer fired; urllib3 and requests turn this into a connection error."""

    def __init__(self) -> None:
        super().__init__("the call took longer than the timeout")


def _watched_session(calls: threading.local) -> requests.Session:
    """A Session whose connections report to `calls.watch`, the calling thread's _CallWatch.

    A connection records itself as it starts connecting and as each request starts on
    it, and its pool releases it from the watch when it is put back. Every connect,
    request and release happens inside a call, in the calling thread, after that call has
    set `calls.watch`. Connect is recorded too because HTTPSConnectionPool connects
    (TCP and the TLS handshake) before the request starts; for plain HTTP, http.client
    connects from inside the request.
    """

    class Recording:
        def connect(self) -> None:
            calls.watch.record(self)
            super().connect()  # type: ignore[misc]
            calls.watch.connected()

        def request(self, *args: Any, **kwargs: Any) -> None:
            calls.watch.record(self)
            super().request(*args, **kwargs)  # type: ignore[misc]

    class Connection(Recording, urllib3.connection.HTTPConnection):
        pass

    class TlsConnection(Recording, urllib3.connection.HTTPSConnection):
        pass

    class Releasing:
        # _put_conn is a private urllib3 API; verified on the pinned urllib3 2.8.0.
        def _put_conn(self, conn: urllib3.connection.HTTPConnection | None) -> None:
            calls.watch.release(conn)
            super()._put_conn(conn)  # type: ignore[misc]

    class Pool(Releasing, urllib3.connectionpool.HTTPConnectionPool):
        ConnectionCls = Connection

    class TlsPool(Releasing, urllib3.connectionpool.HTTPSConnectionPool):
        ConnectionCls = TlsConnection

    class Adapter(requests.adapters.HTTPAdapter):
        def init_poolmanager(self, *args: Any, **kwargs: Any) -> None:
            super().init_poolmanager(*args, **kwargs)
            self.poolmanager.pool_classes_by_scheme = {"http": Pool, "https": TlsPool}

    session = requests.Session()
    adapter = Adapter()
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


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
