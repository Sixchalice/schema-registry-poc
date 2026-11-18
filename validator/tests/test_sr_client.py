import gzip
import json
import socket
import threading
import time
from contextlib import nullcontext as _nullcontext
from types import SimpleNamespace
from typing import Any

import pytest
import requests
import urllib3.connection
import urllib3.exceptions

from schema_validator import sr_client
from schema_validator.models import ValidatorConfig
from schema_validator.sr_client import (
    CompatibilityVerdict,
    HttpSchemaRegistryClient,
    SchemaRegistryConfigError,
    SchemaRegistryError,
    SchemaRegistryRejected,
    SchemaRegistryUnauthorized,
    SchemaRegistryUnavailable,
    SchemaVersion,
    SrReference,
)
from tests.local_server import LocalServer, drip, read_request, respond, send_drip

BASE = "http://sr:8081"
PASSWORD = "s3cret-Pa55"
CONTENT_TYPE = "application/vnd.schemaregistry.v1+json"


class StubRaw:
    """urllib3 2.x raw response: read1 returns one piece, then b"" at the end."""

    def __init__(self, data: bytes, piece: int = 7) -> None:
        self._all = data
        self._data = data
        self._piece = piece
        self.sizes: list[int | None] = []
        self.decode: list[object] = []

    def read1(self, amt: int | None = None, decode_content: bool | None = None) -> bytes:
        self.sizes.append(amt)
        self.decode.append(decode_content)
        out, self._data = self._data[:self._piece], self._data[self._piece:]
        if not out:
            self._data = self._all  # a stub response may be read by several calls
        return out


class StubResponse:
    def __init__(self, status_code: int, body: Any = None, text: str | None = None) -> None:
        self.status_code = status_code
        self.text = text if text is not None else ("" if body is None else json.dumps(body))
        self.closed = False
        self.raw = StubRaw(self.text.encode())

    def iter_content(self, chunk_size: int | None = None):
        yield self.text.encode()

    def close(self) -> None:
        self.closed = True


class StubSession:
    def __init__(self, response: StubResponse | None = None, raises: Exception | None = None) -> None:
        self.response = response
        self.raises = raises
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> StubResponse:
        self.calls.append((method, url, kwargs))
        if self.raises is not None:
            raise self.raises
        assert self.response is not None
        return self.response

    def mount(self, prefix: str, adapter: Any) -> None:
        """The client mounts its adapter on a session it builds (test_from_config builds this stub)."""

    @property
    def last(self) -> tuple[str, str, dict[str, Any]]:
        return self.calls[-1]


def client_for(session: StubSession, **kwargs: Any) -> HttpSchemaRegistryClient:
    return HttpSchemaRegistryClient(BASE, session=session, **kwargs)  # type: ignore[arg-type]


VERSION_BODY = {
    "subject": "a.B", "version": 3, "id": 7, "schemaType": "JSON", "schema": "{}",
    "references": [{"name": "x.json", "subject": "a.C", "version": 2}],
}
AVRO_BODY = {"subject": "a.B", "version": 1, "id": 4, "schema": '"string"'}
REFS = [SrReference("n", "s", 1)]


def call_all(client: HttpSchemaRegistryClient) -> list[Any]:
    """Each of the four methods, as zero-argument callables."""
    return [
        lambda: client.get_latest("a.B"),
        lambda: client.get_version("a.B", 2),
        lambda: client.get_compatibility_level("a.B"),
        lambda: client.get_mode("a.B"),
        lambda: client.check_compatibility("a.B", "S", "PROTOBUF", REFS),
    ]


# ---- the brief's table -------------------------------------------------------

def test_get_latest():
    session = StubSession(StubResponse(200, VERSION_BODY))
    result = client_for(session).get_latest("a.B")
    assert result == SchemaVersion("a.B", 3, "JSON", "{}", (SrReference("x.json", "a.C", 2),))
    method, url, _ = session.last
    assert method == "GET"
    assert url.endswith("/subjects/a.B/versions/latest")


def test_avro_has_no_schema_type():
    session = StubSession(StubResponse(200, AVRO_BODY))
    result = client_for(session).get_latest("a.B")
    assert result is not None
    assert result.schema_type == "AVRO"
    assert result.references == ()


def test_get_latest_404():
    session = StubSession(StubResponse(404, {"error_code": 40401, "message": "Subject 'x' not found."}))
    assert client_for(session).get_latest("x") is None


def test_get_version_url_and_404():
    session = StubSession(StubResponse(404, {"error_code": 40402, "message": "Version 2 not found."}))
    assert client_for(session).get_version("a.B", 2) is None
    method, url, _ = session.last
    assert method == "GET"
    assert url.endswith("/subjects/a.B/versions/2")


def test_get_version_returns_version():
    session = StubSession(StubResponse(200, {**VERSION_BODY, "version": 2}))
    result = client_for(session).get_version("a.B", 2)
    assert result is not None and result.version == 2


def test_subject_is_url_encoded():
    session = StubSession(StubResponse(200, AVRO_BODY))
    client = client_for(session)
    client.get_latest("com/acme my.Order")
    assert "com%2Facme%20my.Order" in session.last[1]
    session.response = StubResponse(200, {"compatibilityLevel": "FULL"})
    client.get_compatibility_level("com/acme my.Order")
    assert "/config/com%2Facme%20my.Order?" in session.last[1]
    session.response = StubResponse(200, {"is_compatible": True, "messages": []})
    client.check_compatibility("com/acme my.Order", "S", "AVRO", [])
    assert "/compatibility/subjects/com%2Facme%20my.Order/versions" in session.last[1]


def test_get_mode():
    session = StubSession(StubResponse(200, {"mode": "READONLY"}))
    assert client_for(session).get_mode("a.B") == "READONLY"
    method, url, _ = session.last
    assert method == "GET"
    assert url.endswith("/mode/a.B?defaultToGlobal=true")


@pytest.mark.parametrize("body", [
    ["READONLY"], "READONLY", None, {}, {"mode": 1}, {"mode": None}, {"mode": ["READONLY"]},
])
def test_get_mode_bad_body(body):
    with pytest.raises(SchemaRegistryUnavailable):
        client_for(StubSession(StubResponse(200, body))).get_mode("a.B")


def test_level():
    session = StubSession(StubResponse(200, {"compatibilityLevel": "FULL"}))
    assert client_for(session).get_compatibility_level("a.B") == "FULL"
    method, url, _ = session.last
    assert method == "GET"
    assert url.endswith("/config/a.B?defaultToGlobal=true")


def test_check_compatibility_request():
    session = StubSession(StubResponse(200, {"is_compatible": True, "messages": []}))
    verdict = client_for(session).check_compatibility("a.B", "S", "PROTOBUF", REFS)
    assert verdict == CompatibilityVerdict(True, ())
    method, url, kwargs = session.last
    assert method == "POST"
    assert url.endswith("/compatibility/subjects/a.B/versions?verbose=true")
    assert json.loads(kwargs["data"]) == {
        "schema": "S", "schemaType": "PROTOBUF",
        "references": [{"name": "n", "subject": "s", "version": 1}],
    }
    assert kwargs["headers"] == {"Content-Type": CONTENT_TYPE}
    assert kwargs["timeout"] == 10


def test_no_references_key_when_empty():
    session = StubSession(StubResponse(200, {"is_compatible": True, "messages": []}))
    client_for(session).check_compatibility("a.B", "S", "AVRO", [])
    assert "references" not in json.loads(session.last[2]["data"])


def test_registered_version_request_and_200():
    session = StubSession(StubResponse(200, {"subject": "a.B", "id": 1, "version": 2, "schema": "S"}))
    assert client_for(session).registered_version("a.B", "S", "PROTOBUF", REFS) == 2
    method, url, kwargs = session.last
    assert method == "POST"
    assert url.endswith("/subjects/a.B")
    assert json.loads(kwargs["data"]) == {
        "schema": "S", "schemaType": "PROTOBUF",
        "references": [{"name": "n", "subject": "s", "version": 1}],
    }


@pytest.mark.parametrize("code", [40401, 40403])
def test_registered_version_404_is_none(code):
    session = StubSession(StubResponse(404, {"error_code": code, "message": "not found"}))
    assert client_for(session).registered_version("a.B", "S", "AVRO", []) is None


def test_registered_version_404_other_code_is_unavailable():
    session = StubSession(StubResponse(404, {"error_code": 40402, "message": "x"}))
    with pytest.raises(SchemaRegistryUnavailable):
        client_for(session).registered_version("a.B", "S", "AVRO", [])


def test_403_on_lookup_is_unauthorized():
    with pytest.raises(SchemaRegistryUnauthorized):
        client_for(StubSession(StubResponse(403, {"error_code": 40301, "message": "x"}))).registered_version(
            "a.B", "S", "AVRO", [])


def test_registered_version_reads_the_version_field():
    body = {"subject": "a.B", "id": 7, "version": 3, "schema": "S"}
    assert client_for(StubSession(StubResponse(200, body))).registered_version("a.B", "S", "AVRO", []) == 3


@pytest.mark.parametrize("body", [[], "x", None, 3, {}, {"version": "3"}, {"version": True}])
def test_registered_version_bad_body(body):
    with pytest.raises(SchemaRegistryUnavailable):
        client_for(StubSession(StubResponse(200, body))).registered_version("a.B", "S", "AVRO", [])


def test_other_calls_still_reject_40403():
    body = {"error_code": 40403, "message": "x"}
    for call in call_all(client_for(StubSession(StubResponse(404, body)))):
        with pytest.raises(SchemaRegistryUnavailable):
            call()


def test_incompatible():
    session = StubSession(StubResponse(200, {"is_compatible": False, "messages": ["m1", "m2"]}))
    verdict = client_for(session).check_compatibility("a.B", "S", "AVRO", [])
    assert verdict == CompatibilityVerdict(False, ("m1", "m2"))


def test_messages_missing_or_null_means_empty():
    session = StubSession(StubResponse(200, {"is_compatible": True}))
    client = client_for(session)
    assert client.check_compatibility("a.B", "S", "AVRO", []) == CompatibilityVerdict(True, ())
    session.response = StubResponse(200, {"is_compatible": True, "messages": None})
    assert client.check_compatibility("a.B", "S", "AVRO", []) == CompatibilityVerdict(True, ())


def test_basic_auth():
    session = StubSession(StubResponse(200, {"compatibilityLevel": "FULL"}))
    client_for(session, username="u", password="p").get_compatibility_level("a.B")
    assert session.last[2]["auth"] == ("u", "p")
    client_for(session).get_compatibility_level("a.B")
    assert session.last[2]["auth"] is None


def test_get_requests_send_header_timeout_and_no_body():
    session = StubSession(StubResponse(200, AVRO_BODY))
    client_for(session, timeout_seconds=2.5).get_latest("a.B")
    kwargs = session.last[2]
    assert kwargs["headers"] == {"Content-Type": CONTENT_TYPE}
    assert kwargs["timeout"] == 2.5
    assert kwargs["data"] is None


def test_trailing_slash_removed():
    session = StubSession(StubResponse(200, AVRO_BODY))
    HttpSchemaRegistryClient(BASE + "/", session=session).get_latest("a.B")  # type: ignore[arg-type]
    assert session.last[1] == BASE + "/subjects/a.B/versions/latest"


@pytest.mark.parametrize("status", [401, 403])
def test_unauthorized(status):
    for call in call_all(client_for(StubSession(StubResponse(status, {"error_code": status, "message": "no"})))):
        with pytest.raises(SchemaRegistryUnauthorized):
            call()


def test_rejected_42201():
    session = StubSession(StubResponse(422, {"error_code": 42201, "message": "Invalid schema X"}))
    with pytest.raises(SchemaRegistryRejected) as e:
        client_for(session).check_compatibility("a.B", "S", "AVRO", [])
    assert str(e.value) == "Invalid schema X"


@pytest.mark.parametrize("body", [{"error_code": 42201}, {"error_code": 42201, "message": ""},
                                  {"error_code": 42201, "message": None}, {"error_code": 42201, "message": 5}])
def test_rejected_without_a_message_has_an_empty_message(body):
    session = StubSession(StubResponse(422, body))
    with pytest.raises(SchemaRegistryRejected) as e:
        client_for(session).check_compatibility("a.B", "S", "AVRO", [])
    assert str(e.value) == ""
    assert e.value.code == 42201


def test_4xx_without_error_code_is_unavailable():
    session = StubSession(StubResponse(400, text="Bad request from proxy"))
    with pytest.raises(SchemaRegistryUnavailable) as e:
        client_for(session).check_compatibility("a.B", "S", "AVRO", [])
    assert "proxy" not in str(e.value)


def test_404_on_level_and_compatibility_is_rejected():
    body = {"error_code": 40401, "message": "Subject 'a.B' not found."}
    client = client_for(StubSession(StubResponse(404, body)))
    for call in call_all(client)[2:]:
        with pytest.raises(SchemaRegistryRejected) as e:
            call()
        assert str(e.value) == "Subject 'a.B' not found."


@pytest.mark.parametrize("status", [500, 503])
def test_server_error(status):
    for call in call_all(client_for(StubSession(StubResponse(status, {"error_code": 50001, "message": "x"})))):
        with pytest.raises(SchemaRegistryUnavailable):
            call()


@pytest.mark.parametrize("exc", [requests.Timeout("timed out"), requests.ConnectionError("refused")])
def test_timeout_and_connection_error(exc):
    for call in call_all(client_for(StubSession(raises=exc))):
        with pytest.raises(SchemaRegistryUnavailable):
            call()


def test_html_body():
    for call in call_all(client_for(StubSession(StubResponse(200, text="<html>proxy</html>")))):
        with pytest.raises(SchemaRegistryUnavailable):
            call()


def test_json_without_expected_field():
    client = client_for(StubSession(StubResponse(200, {})))
    with pytest.raises(SchemaRegistryUnavailable):
        client.check_compatibility("a.B", "S", "AVRO", [])
    with pytest.raises(SchemaRegistryUnavailable):
        client.get_compatibility_level("a.B")


def test_from_config(monkeypatch):
    session = StubSession(StubResponse(200, AVRO_BODY))
    monkeypatch.setattr(requests, "Session", lambda: session)
    config = ValidatorConfig(sr_url="http://sr:8081/", sr_username="u", sr_password="p", sr_timeout_seconds=3)
    HttpSchemaRegistryClient.from_config(config).get_latest("a.B")
    _, url, kwargs = session.last
    assert url == "http://sr:8081/subjects/a.B/versions/latest"
    assert kwargs["auth"] == ("u", "p")
    assert kwargs["timeout"] == 3
    small = config.model_copy(update={"max_response_bytes": 10})
    with pytest.raises(SchemaRegistryUnavailable, match="response larger than the limit"):
        HttpSchemaRegistryClient.from_config(small).get_latest("a.B")


# ---- untrusted 200 bodies of the wrong shape ---------------------------------

@pytest.mark.parametrize("body", [
    [VERSION_BODY],
    "a string",
    42,
    None,
    {k: v for k, v in VERSION_BODY.items() if k != "schema"},
    {k: v for k, v in VERSION_BODY.items() if k != "version"},
    {k: v for k, v in VERSION_BODY.items() if k != "subject"},
    {**VERSION_BODY, "schema": {"type": "record"}},
    {**VERSION_BODY, "schema": None},
    {**VERSION_BODY, "subject": 5},
    {**VERSION_BODY, "version": "3"},
    {**VERSION_BODY, "version": 3.0},
    {**VERSION_BODY, "version": True},
    {**VERSION_BODY, "id": "7"},
    {**VERSION_BODY, "id": None},
    {**VERSION_BODY, "schemaType": 1},
    {**VERSION_BODY, "schemaType": None},
    {**VERSION_BODY, "references": {"name": "x"}},
    {**VERSION_BODY, "references": "x.json"},
    {**VERSION_BODY, "references": None},
    {**VERSION_BODY, "references": ["x.json"]},
    {**VERSION_BODY, "references": [None]},
    {**VERSION_BODY, "references": [{"name": "x.json", "subject": "a.C"}]},
    {**VERSION_BODY, "references": [{"name": 1, "subject": "a.C", "version": 2}]},
    {**VERSION_BODY, "references": [{"name": "x.json", "subject": None, "version": 2}]},
    {**VERSION_BODY, "references": [{"name": "x.json", "subject": "a.C", "version": "2"}]},
    {**VERSION_BODY, "references": [{"name": "x.json", "subject": "a.C", "version": False}]},
])
def test_wrong_shape_version_body_is_unavailable(body):
    client = client_for(StubSession(StubResponse(200, body)))
    with pytest.raises(SchemaRegistryUnavailable):
        client.get_latest("a.B")
    with pytest.raises(SchemaRegistryUnavailable):
        client.get_version("a.B", 3)


@pytest.mark.parametrize("body", [
    ["FULL"], "FULL", None, {"compatibilityLevel": 1}, {"compatibilityLevel": None},
    {"compatibilityLevel": ["FULL"]},
])
def test_wrong_shape_level_body_is_unavailable(body):
    with pytest.raises(SchemaRegistryUnavailable):
        client_for(StubSession(StubResponse(200, body))).get_compatibility_level("a.B")


@pytest.mark.parametrize("body", [
    [True], "true", True, None,
    {"messages": []},
    {"is_compatible": "true", "messages": []},
    {"is_compatible": 1, "messages": []},
    {"is_compatible": None, "messages": []},
    {"is_compatible": False, "messages": "m1"},
    {"is_compatible": False, "messages": {"m": 1}},
    {"is_compatible": False, "messages": [1, 2]},
    {"is_compatible": False, "messages": ["m1", None]},
    {"is_compatible": False, "messages": [{"m": 1}]},
])
def test_wrong_shape_compatibility_body_is_unavailable(body):
    with pytest.raises(SchemaRegistryUnavailable):
        client_for(StubSession(StubResponse(200, body))).check_compatibility("a.B", "S", "AVRO", [])


def test_json_null_200_body_is_unavailable_not_404():
    for call in call_all(client_for(StubSession(StubResponse(200, text="null")))):
        with pytest.raises(SchemaRegistryUnavailable):
            call()


def test_empty_200_body_is_unavailable():
    for call in call_all(client_for(StubSession(StubResponse(200, text="")))):
        with pytest.raises(SchemaRegistryUnavailable):
            call()


def test_deeply_nested_json_is_unavailable():
    text = "[" * 100_000 + "]" * 100_000
    for call in call_all(client_for(StubSession(StubResponse(200, text=text)))):
        with pytest.raises(SchemaRegistryUnavailable):
            call()


def test_unexpected_status_is_unavailable():
    for call in call_all(client_for(StubSession(StubResponse(302, text="")))):
        with pytest.raises(SchemaRegistryUnavailable):
            call()


# ---- untrusted error bodies --------------------------------------------------

@pytest.mark.parametrize("text", [
    json.dumps({"error_code": 42201, "message": 5}),
    json.dumps({"error_code": 42201, "message": None}),
    json.dumps({"error_code": 42201, "message": ["Invalid schema X"]}),
    json.dumps({"error_code": 42201}),
])
def test_odd_4xx_body_with_error_code_is_still_rejected_without_raw_text(text):
    with pytest.raises(SchemaRegistryRejected) as e:
        client_for(StubSession(StubResponse(422, text=text))).check_compatibility("a.B", "S", "AVRO", [])
    assert str(e.value) == ""
    assert e.value.code == 42201


@pytest.mark.parametrize("text", [
    json.dumps(["Invalid schema X"]),
    json.dumps("Invalid schema X"),
    json.dumps({"message": "Invalid schema X"}),
    json.dumps({"error_code": "42201", "message": "Invalid schema X"}),
    json.dumps({"error_code": True, "message": "Invalid schema X"}),
    json.dumps({"error_code": None, "message": "Invalid schema X"}),
    "<html>bad request</html>",
])
def test_odd_4xx_body_without_error_code_is_unavailable(text):
    with pytest.raises(SchemaRegistryUnavailable) as e:
        client_for(StubSession(StubResponse(422, text=text))).check_compatibility("a.B", "S", "AVRO", [])
    assert "Invalid schema" not in str(e.value)


def test_empty_4xx_body_is_unavailable():
    for call in call_all(client_for(StubSession(StubResponse(422, text="")))):
        with pytest.raises(SchemaRegistryUnavailable):
            call()


@pytest.mark.parametrize("status, text", [
    (407, "<html>Proxy auth required for user bob</html>"),
    (429, ""),
])
def test_proxy_4xx_without_error_code_is_unavailable(status, text):
    for call in call_all(client_for(StubSession(StubResponse(status, text=text)))):
        with pytest.raises(SchemaRegistryUnavailable) as e:
            call()
        assert "bob" not in str(e.value)


@pytest.mark.parametrize("status,exc_class", [
    (401, SchemaRegistryUnauthorized), (403, SchemaRegistryUnauthorized),
    (500, SchemaRegistryUnavailable), (503, SchemaRegistryUnavailable),
])
@pytest.mark.parametrize("text", [json.dumps([1]), json.dumps({"message": 5}), "<html>x</html>", ""])
def test_odd_error_body_keeps_exception_class(status, exc_class, text):
    for call in call_all(client_for(StubSession(StubResponse(status, text=text)))):
        with pytest.raises(exc_class):
            call()


@pytest.mark.parametrize("code", [40401, 40402])
def test_sr_not_found_codes_are_none_on_both_gets(code):
    client = client_for(StubSession(StubResponse(404, {"error_code": code, "message": "not found"})))
    assert client.get_latest("a.B") is None
    assert client.get_version("a.B", 1) is None


# 404 bodies without Schema Registry's "subject/version not found" codes. The second
# JSON one is what Schema Registry 7.9.0 returns for a path it does not serve, e.g.
# when sr_url has a wrong prefix (observed in integration/tests/test_sr_behaviour.py).
_OTHER_404_BODIES = [
    "<html>not found</html>",
    "",
    json.dumps({"message": "Subject 'a.B' not found."}),
    json.dumps({"error_code": 40403, "message": "Schema not found"}),
    json.dumps({"error_code": 404, "message": "HTTP 404 Not Found"}),
    json.dumps({"error_code": "40401", "message": "not found"}),
    json.dumps({"error_code": None}),
    json.dumps([{"error_code": 40401}]),
    json.dumps(40401),
]


@pytest.mark.parametrize("text", _OTHER_404_BODIES)
def test_other_404_on_gets_is_unavailable(text):
    client = client_for(StubSession(StubResponse(404, text=text)))
    with pytest.raises(SchemaRegistryUnavailable):
        client.get_latest("a.B")
    with pytest.raises(SchemaRegistryUnavailable):
        client.get_version("a.B", 1)


@pytest.mark.parametrize("text", _OTHER_404_BODIES)
def test_other_404_on_level_and_compatibility_is_unavailable(text):
    client = client_for(StubSession(StubResponse(404, text=text)))
    for call in call_all(client)[2:]:
        with pytest.raises(SchemaRegistryUnavailable):
            call()


# ---- secrets ------------------------------------------------------------------

def _basic_header(user: str, password: str) -> str:
    import base64
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


@pytest.mark.parametrize("session", [
    StubSession(raises=requests.ConnectionError(f"http://u:{PASSWORD}@sr:8081 refused")),
    StubSession(raises=requests.Timeout(f"Authorization: {_basic_header('u', PASSWORD)}")),
    StubSession(StubResponse(401, text="unauthorized")),
    StubSession(StubResponse(403, text="forbidden")),
    StubSession(StubResponse(503, text="unavailable")),
    StubSession(StubResponse(200, text="<html>proxy</html>")),
    StubSession(StubResponse(200, {})),
    StubSession(StubResponse(422, {"error_code": 42201, "message": "Invalid schema X"})),
], ids=["conn", "timeout", "401", "403", "503", "html", "shape", "422"])
def test_errors_never_contain_the_password(session):
    client = client_for(session, username="u", password=PASSWORD)
    for call in call_all(client):
        _assert_unavailable_or_other_clean_error(call, "u", PASSWORD, SchemaRegistryError)


def _assert_unavailable_or_other_clean_error(call: Any, user: str, password: str,
                                             expected: type[SchemaRegistryError]) -> None:
    """call() raises `expected`, and nothing in it or its chain shows a secret."""
    try:
        call()
    except expected as e:
        texts = [str(e), repr(e), repr(e.args)]
        # The cause chain is printed in tracebacks, so it must not leak either.
        assert e.__cause__ is None
        assert e.__context__ is None or e.__suppress_context__
        secrets = [password, "Authorization"]
        try:
            secrets.append(_basic_header(user, password))
        except UnicodeEncodeError:
            pass
        for text in texts:
            for secret in secrets:
                assert secret not in text, (secret, text)
    else:
        pytest.fail("expected an error")


@pytest.fixture
def no_sockets(monkeypatch):
    """Real requests.Session tests below must fail before any connection is opened."""
    import socket

    def refuse(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("unit test tried to open a network connection")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


CONFIG_ERROR_TEXT = "Schema Registry username and password must be Latin-1 text"


@pytest.mark.parametrize("user,password", [
    ("u", "pässwörd€"),
    ("ü€", PASSWORD),
    ("u", "пароль-密码"),
    ("u", "pass\udc80word"),
], ids=["euro-password", "euro-username", "cyrillic-cjk-password", "surrogate-password"])
def test_non_latin1_credentials_are_a_config_error(user, password):
    # requests encodes the Basic header as Latin-1 and would fail on every call; fail at construction.
    with pytest.raises(SchemaRegistryConfigError) as info:
        HttpSchemaRegistryClient("http://x", user, password)
    assert str(info.value) == CONFIG_ERROR_TEXT
    assert isinstance(info.value, ValueError)
    assert info.value.__cause__ is None
    assert password not in repr(info.value)


def test_non_latin1_password_from_config_is_a_config_error():
    config = ValidatorConfig(sr_url="http://x", sr_username="u", sr_password="pässwörd€")
    with pytest.raises(SchemaRegistryConfigError):
        HttpSchemaRegistryClient.from_config(config)


def test_latin1_password_is_accepted():
    HttpSchemaRegistryClient("http://x", "u", "pässwörd")


def test_lone_surrogate_subject_is_unavailable():
    # urllib.parse.quote encodes the subject as strict UTF-8.
    session = StubSession(StubResponse(200, AVRO_BODY))
    client = client_for(session)
    calls = [
        lambda: client.get_latest("a\udc80b"),
        lambda: client.get_version("a\udc80b", 1),
        lambda: client.get_compatibility_level("a\udc80b"),
        lambda: client.get_mode("a\udc80b"),
        lambda: client.check_compatibility("a\udc80b", "S", "AVRO", []),
    ]
    for call in calls:
        with pytest.raises(SchemaRegistryUnavailable):
            call()
    assert session.calls == []


def test_request_body_is_ascii_json_even_for_surrogates_and_non_ascii():
    # json.dumps escapes everything outside ASCII (ensure_ascii), so building the
    # body cannot fail on a lone surrogate and the wire bytes are plain ASCII.
    session = StubSession(StubResponse(200, {"is_compatible": True, "messages": []}))
    schema = "a\udc80b \u00e9 \u5bc6"
    refs = [SrReference("n\udc80", "s\u00e9", 1)]
    client_for(session).check_compatibility("a.B", schema, "AVRO", refs)
    data = session.last[2]["data"]
    assert data.isascii()
    assert json.loads(data) == {
        "schema": schema, "schemaType": "AVRO",
        "references": [{"name": "n\udc80", "subject": "s\u00e9", "version": 1}],
    }


def test_repr_does_not_contain_the_password():
    client = HttpSchemaRegistryClient(BASE, username="u", password=PASSWORD)
    assert PASSWORD not in repr(client)
    assert PASSWORD not in str(client)


def test_config_repr_does_not_contain_the_password():
    config = ValidatorConfig(sr_url=BASE, sr_username="u", sr_password="s3cret")
    assert "s3cret" not in repr(config)
    assert "s3cret" not in str(config)
    assert config.sr_password == "s3cret"


# ---- deadline, redirects, raw bodies (audit L1-L3) ---------------------------

class SlowRaw:
    def read1(self, amt: int | None = None, decode_content: bool | None = None) -> bytes:
        return b" "


class SlowResponse(StubResponse):
    def __init__(self) -> None:
        super().__init__(200, VERSION_BODY)
        self.raw = SlowRaw()


class NoRead1Response(StubResponse):
    """urllib3 1.x: `raw` has no read1, only iter_content is used."""

    def __init__(self, status: int, body: Any = None) -> None:
        super().__init__(status, body)
        self.raw = object()
        self.chunk_sizes: list[int | None] = []

    def iter_content(self, chunk_size: int | None = None):
        self.chunk_sizes.append(chunk_size)
        yield from (bytes([b]) for b in self.text.encode())


def test_slow_body_hits_deadline(monkeypatch):
    clock = iter(range(100))
    monkeypatch.setattr("schema_validator.sr_client.time.monotonic", lambda: float(next(clock)))
    response = SlowResponse()
    with pytest.raises(SchemaRegistryUnavailable) as e:
        client_for(StubSession(response), timeout_seconds=2).get_latest("a.B")
    assert "longer than the timeout" in str(e.value)
    assert "a.B" not in str(e.value)
    assert response.closed


def test_redirects_not_followed():
    session = StubSession(StubResponse(302, text=""))
    with pytest.raises(SchemaRegistryUnavailable):
        client_for(session).get_latest("a.B")
    kwargs = session.last[2]
    assert kwargs["allow_redirects"] is False
    assert kwargs["stream"] is True


def test_raw_body_never_in_rejected():
    session = StubSession(StubResponse(400, {"error_code": 400, "detail": "Authorization: Basic xyz"}))
    with pytest.raises(SchemaRegistryRejected) as e:
        client_for(session).check_compatibility("a.B", "S", "AVRO", [])
    assert str(e.value) == ""
    assert e.value.code == 400


def test_response_is_closed_on_every_outcome():
    for response in (StubResponse(200, AVRO_BODY), StubResponse(500, text=""), StubResponse(401, text="")):
        with pytest.raises(SchemaRegistryError) if response.status_code != 200 else _nullcontext():
            client_for(StubSession(response)).get_latest("a.B")
        assert response.closed


def test_body_is_read_in_pieces_with_read1():
    response = StubResponse(200, VERSION_BODY)
    result = client_for(StubSession(response)).get_latest("a.B")
    assert result is not None and result.version == 3
    assert set(response.raw.sizes) == {65536}
    assert set(response.raw.decode) == {True}


def test_fallback_to_iter_content_without_read1():
    response = NoRead1Response(200, VERSION_BODY)
    result = client_for(StubSession(response)).get_latest("a.B")
    assert result is not None and result.version == 3
    assert response.chunk_sizes == [1]


def test_fallback_hits_deadline_too(monkeypatch):
    clock = iter(range(1000))
    monkeypatch.setattr("schema_validator.sr_client.time.monotonic", lambda: float(next(clock)))
    with pytest.raises(SchemaRegistryUnavailable):
        client_for(StubSession(NoRead1Response(200, VERSION_BODY)), timeout_seconds=2).get_latest("a.B")


@pytest.mark.parametrize("error", [
    urllib3.exceptions.ReadTimeoutError(None, "http://10.9.8.7:8081/x", "Read timed out."),
    urllib3.exceptions.ProtocolError("Connection broken: 10.9.8.7"),
    urllib3.exceptions.DecodeError("bad gzip from 10.9.8.7"),
    ConnectionResetError("reset by 10.9.8.7"),
])
def test_error_while_reading_the_body_is_unavailable(error):
    response = StubResponse(200, VERSION_BODY)

    def read1(amt=None, decode_content=None):
        raise error

    response.raw.read1 = read1
    with pytest.raises(SchemaRegistryUnavailable) as e:
        client_for(StubSession(response)).get_latest("a.B")
    assert str(e.value) == f"GET /subjects/{{s}}/versions/latest: {type(error).__name__}"
    assert "10.9.8.7" not in str(e.value)
    assert response.closed


def test_deadline_passed_before_the_body_is_unavailable(monkeypatch):
    clock = iter([0.0, 5.0, 5.0, 5.0])
    monkeypatch.setattr("schema_validator.sr_client.time.monotonic", lambda: next(clock))
    response = StubResponse(200, text="")  # nothing to read: only the check after request() can catch it
    with pytest.raises(SchemaRegistryUnavailable) as e:
        client_for(StubSession(response), timeout_seconds=2).get_latest("a.B")
    assert "longer than the timeout" in str(e.value)


# ---- response cap, a total time per call, startup checks (audit 3 I3, M1, M2) --------

def count_pieces(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """The sizes of the decoded pieces the client reads, in order."""
    sizes: list[int] = []
    real = sr_client._pieces

    def counted(response: Any) -> Any:
        for piece in real(response):
            sizes.append(len(piece))
            yield piece

    monkeypatch.setattr(sr_client, "_pieces", counted)
    return sizes


def endless(conn: socket.socket, server: LocalServer) -> None:
    read_request(conn, server)
    conn.settimeout(5)
    conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n")
    chunk = b"%x\r\n%s\r\n" % (8192, b" " * 8192)
    while not server.stopping.is_set():
        conn.sendall(chunk)


def test_endless_body_is_capped(monkeypatch):
    sizes = count_pieces(monkeypatch)
    with LocalServer(endless) as server:
        client = HttpSchemaRegistryClient(server.url, max_response_bytes=100_000)
        started = time.monotonic()
        with pytest.raises(SchemaRegistryUnavailable) as e:
            client.get_latest("a.B")
        elapsed = time.monotonic() - started
        assert server.peer_closed.wait(2)
    assert str(e.value) == "GET /subjects/{s}/versions/latest: response larger than the limit"
    assert elapsed < 2
    assert 100_000 < sum(sizes) < 100_000 + 65536


def test_gzip_bomb_is_capped(monkeypatch):
    sizes = count_pieces(monkeypatch)
    bomb = gzip.compress(bytes(50_000_000), compresslevel=9)  # about 50 KB on the wire
    with LocalServer(respond("200 OK", bomb, "Content-Encoding: gzip\r\n")) as server:
        client = HttpSchemaRegistryClient(server.url, max_response_bytes=1_000_000)
        with pytest.raises(SchemaRegistryUnavailable, match="response larger than the limit"):
            client.get_latest("a.B")
    # urllib3 2.x read1 decodes at most one piece (64 KB) at a time, so the cap holds after decoding.
    assert 1_000_000 < sum(sizes) < 1_000_000 + 65536
    assert max(sizes) <= 65536


def test_response_near_the_schema_limit_loads():
    # Review focus 3: the largest schema a registry may hold, escaped inside its version body,
    # fits the default limit with room to spare.
    fields: list[dict[str, str]] = []
    schema = ""
    while len(schema) < 900_000:
        fields.extend({"name": f"f{len(fields) + i}", "type": "string", "doc": "\"quoted\""} for i in range(1000))
        schema = json.dumps({"type": "record", "name": "Big", "namespace": "a", "fields": fields})
    body = json.dumps({"subject": "a.B", "version": 1, "id": 1, "schema": schema}).encode()
    assert len(body) > 1_000_000
    with LocalServer(respond("200 OK", body)) as server:
        result = HttpSchemaRegistryClient(server.url).get_latest("a.B")
    assert result is not None and result.schema == schema


def test_header_drip_is_cut_at_the_call_timeout():
    with LocalServer(drip(b"HTTP/1.1 200 OK\r\nX-A: ")) as server:
        client = HttpSchemaRegistryClient(server.url, timeout_seconds=1)
        started = time.monotonic()
        with pytest.raises(SchemaRegistryUnavailable):
            client.get_latest("a.B")
        elapsed = time.monotonic() - started
        assert server.peer_closed.wait(1)
    assert elapsed < 1.6


def test_watchdog_closes_only_its_own_call():
    # Review focus 4: one client, two threads. A's watchdog fires while B's call is in flight.
    def by_subject(conn: socket.socket, server: LocalServer) -> None:
        request = read_request(conn, server)
        if b"slow" in request:
            send_drip(conn, server, b"HTTP/1.1 200 OK\r\nX-A: ")
            return
        server.stopping.wait(2.0)
        body = json.dumps(AVRO_BODY).encode()
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n%s" % (len(body), body))

    outcomes: dict[str, object] = {}

    def run(name: str, subject: str, client: HttpSchemaRegistryClient) -> None:
        try:
            outcomes[name] = client.get_latest(subject)
        except SchemaRegistryError as e:
            outcomes[name] = e

    with LocalServer(by_subject) as server:
        client = HttpSchemaRegistryClient(server.url, timeout_seconds=3)
        a = threading.Thread(target=run, args=("A", "slow.S", client))
        b = threading.Thread(target=run, args=("B", "a.B", client))
        a.start()
        # B starts at 1.5 s and is answered at 3.5 s: A's watchdog fires at 3.0 s, during B's
        # call, and B's own fires at 4.5 s, 1 s after B is answered.
        time.sleep(1.5)
        b.start()
        a.join(6)
        b.join(6)
        assert not a.is_alive() and not b.is_alive()
    assert isinstance(outcomes["A"], SchemaRegistryUnavailable)
    assert outcomes["B"] == SchemaVersion("a.B", 1, "AVRO", '"string"', ())


def test_watchdog_never_reaches_a_connection_back_in_the_pool(monkeypatch):
    # The timer may fire just as a call ends, after its connection went back to the pool
    # and before the timer is cancelled. Here it fires exactly then: the next call must
    # still reuse that connection (the watchdog left it alone).
    class FiresOnCancel:
        def __init__(self, interval: float, function: Any) -> None:
            self.function = function
            self.daemon = False

        def start(self) -> None:
            pass

        def cancel(self) -> None:
            self.function()

    monkeypatch.setattr(sr_client.threading, "Timer", FiresOnCancel)
    with LocalServer(respond("200 OK", json.dumps(AVRO_BODY).encode())) as server:
        client = HttpSchemaRegistryClient(server.url)
        assert client.get_latest("a.B") is not None
        assert client.get_latest("a.B") is not None
        assert server.connections == 1


URL_ERROR_TEXT = "Schema Registry URL must be http:// or https:// with a host"


@pytest.mark.parametrize("url", ["", "localhost:18081", "ftp://x", "http://", "http://[::1",
                                 "http://host:abc", "http://host:99999", "http://ho st:8081"])
def test_bad_url_is_a_config_error(url):
    with pytest.raises(SchemaRegistryConfigError) as info:
        HttpSchemaRegistryClient(url)
    assert str(info.value) == URL_ERROR_TEXT
    assert info.value.__cause__ is None


def test_default_config_url_is_a_config_error():
    # ValidatorConfig's sr_url defaults to "": the platform must set it before building the client.
    with pytest.raises(SchemaRegistryConfigError, match="URL"):
        HttpSchemaRegistryClient.from_config(ValidatorConfig())


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), None, "10"])
def test_bad_timeout_is_a_config_error(timeout):
    with pytest.raises(SchemaRegistryConfigError) as info:
        HttpSchemaRegistryClient(BASE, timeout_seconds=timeout)
    assert str(info.value) == "Schema Registry timeout must be a number of seconds above 0"


@pytest.mark.parametrize("limit", [0, -1, None, 1.5])
def test_bad_response_limit_is_a_config_error(limit):
    with pytest.raises(SchemaRegistryConfigError) as info:
        HttpSchemaRegistryClient(BASE, max_response_bytes=limit)
    assert str(info.value) == "Schema Registry response limit must be a whole number of bytes, at least 1"


def test_https_url_is_accepted(no_sockets):
    HttpSchemaRegistryClient("https://sr.example:8081")


def test_watchdog_ignores_a_socket_closed_meanwhile():
    # The call may close its socket just as the timer fires: shutdown then fails, harmlessly.
    sock = socket.socket()
    sock.close()
    watch = sr_client._CallWatch(1)
    watch.record(SimpleNamespace(sock=sock))  # type: ignore[arg-type]
    watch._abort()


def _call_in_thread(client: HttpSchemaRegistryClient) -> tuple[threading.Thread, dict[str, object]]:
    outcome: dict[str, object] = {}

    def go() -> None:
        started = time.monotonic()
        try:
            client.get_latest("a.B")
        except SchemaRegistryError as e:
            outcome["error"] = e
        outcome["elapsed"] = time.monotonic() - started

    thread = threading.Thread(target=go, daemon=True)
    thread.start()
    return thread, outcome


def test_watchdog_covers_a_slow_connect_then_a_header_drip(monkeypatch):
    # Final review, Important: the timer fires while the socket is still being made (slow
    # DNS or TCP connect); the header drip that follows must not outlive the call's timeout.
    plain_new_conn = urllib3.connection.HTTPConnection._new_conn

    def slow_new_conn(self: urllib3.connection.HTTPConnection) -> socket.socket:
        time.sleep(1.2)
        return plain_new_conn(self)

    monkeypatch.setattr(urllib3.connection.HTTPConnection, "_new_conn", slow_new_conn)
    with LocalServer(drip(b"HTTP/1.1 200 OK\r\nX-A: ")) as server:
        thread, outcome = _call_in_thread(HttpSchemaRegistryClient(server.url, timeout_seconds=1))
        thread.join(3)
        assert not thread.is_alive()
    assert isinstance(outcome["error"], SchemaRegistryUnavailable)
    assert outcome["elapsed"] < 1.6


def test_watchdog_covers_https_connecting_before_the_request(monkeypatch):
    # HTTPSConnectionPool._validate_conn runs connect() (TCP and the TLS handshake) before
    # request(). The handshake is stood in for by a slow connect that ends in a plain socket,
    # so the drip that follows is read over it; no real TLS is needed for the ordering.
    def slow_tls_connect(self: urllib3.connection.HTTPSConnection) -> None:
        time.sleep(1.2)
        self.sock = socket.create_connection((self.host, self.port))
        self.is_verified = True

    monkeypatch.setattr(urllib3.connection.HTTPSConnection, "connect", slow_tls_connect)
    with LocalServer(drip(b"HTTP/1.1 200 OK\r\nX-A: ")) as server:
        url = server.url.replace("http://", "https://")
        thread, outcome = _call_in_thread(HttpSchemaRegistryClient(url, timeout_seconds=1))
        thread.join(3)
        assert not thread.is_alive()
    assert isinstance(outcome["error"], SchemaRegistryUnavailable)
    assert outcome["elapsed"] < 1.6


def test_an_expired_watch_refuses_a_connection():
    # Once the timer has fired, no connection may start (or go on with) a request in the call.
    watch = sr_client._CallWatch(1)
    watch._abort()
    with pytest.raises(OSError):
        watch.record(SimpleNamespace(sock=None))  # type: ignore[arg-type]
