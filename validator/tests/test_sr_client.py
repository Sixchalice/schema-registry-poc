import json
from typing import Any

import pytest
import requests

from schema_validator.models import ValidatorConfig
from schema_validator.sr_client import (
    CompatibilityVerdict,
    HttpSchemaRegistryClient,
    SchemaRegistryError,
    SchemaRegistryRejected,
    SchemaRegistryUnauthorized,
    SchemaRegistryUnavailable,
    SchemaVersion,
    SrReference,
)

BASE = "http://sr:8081"
PASSWORD = "s3cret-Pa55"
CONTENT_TYPE = "application/vnd.schemaregistry.v1+json"


class StubResponse:
    def __init__(self, status_code: int, body: Any = None, text: str | None = None) -> None:
        self.status_code = status_code
        self.text = text if text is not None else ("" if body is None else json.dumps(body))

    def json(self) -> Any:
        try:
            return json.loads(self.text)
        except json.JSONDecodeError as e:
            raise ValueError(str(e)) from e


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


def test_rejected_uses_raw_text_without_json_message():
    session = StubSession(StubResponse(400, text="Bad request from proxy"))
    with pytest.raises(SchemaRegistryRejected) as e:
        client_for(session).check_compatibility("a.B", "S", "AVRO", [])
    assert str(e.value) == "Bad request from proxy"


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
    json.dumps(["Invalid schema X"]),
    json.dumps("Invalid schema X"),
    json.dumps({"error_code": 42201, "message": 5}),
    json.dumps({"error_code": 42201, "message": None}),
    json.dumps({"error_code": 42201, "message": ["Invalid schema X"]}),
    json.dumps({"error_code": 42201}),
    "<html>bad request</html>",
])
def test_odd_4xx_body_is_still_rejected_with_raw_text(text):
    with pytest.raises(SchemaRegistryRejected) as e:
        client_for(StubSession(StubResponse(422, text=text))).check_compatibility("a.B", "S", "AVRO", [])
    assert str(e.value) == text


def test_empty_4xx_body_is_rejected_with_status():
    with pytest.raises(SchemaRegistryRejected) as e:
        client_for(StubSession(StubResponse(422, text=""))).check_compatibility("a.B", "S", "AVRO", [])
    assert str(e.value) == "HTTP 422"


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


@pytest.mark.parametrize("user,password", [
    ("u", "пароль-密码"),
    ("пользователь", PASSWORD),
    ("u", "pass\udc80word"),
], ids=["cyrillic-cjk-password", "cyrillic-username", "surrogate-password"])
def test_non_latin1_credentials_are_unavailable_without_leaking(no_sockets, user, password):
    # requests encodes the Basic header as Latin-1 inside session.request and raises
    # UnicodeEncodeError, which is not a RequestException and whose repr holds the password.
    client = HttpSchemaRegistryClient("http://127.0.0.1:9", username=user, password=password)
    for call in call_all(client):
        _assert_unavailable_or_other_clean_error(call, user, password, SchemaRegistryUnavailable)


def test_negative_timeout_is_unavailable(no_sockets):
    # urllib3 rejects a timeout <= 0 with a ValueError before connecting.
    client = HttpSchemaRegistryClient("http://127.0.0.1:9", timeout_seconds=-1)
    for call in call_all(client):
        with pytest.raises(SchemaRegistryUnavailable):
            call()


def test_lone_surrogate_subject_is_unavailable():
    # urllib.parse.quote encodes the subject as strict UTF-8.
    session = StubSession(StubResponse(200, AVRO_BODY))
    client = client_for(session)
    calls = [
        lambda: client.get_latest("a\udc80b"),
        lambda: client.get_version("a\udc80b", 1),
        lambda: client.get_compatibility_level("a\udc80b"),
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
