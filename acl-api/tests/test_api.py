import pytest

from acl_api.errors import CliFailed, CliRejectedArguments, CliTimedOut, WriteQueueFull


def test_health_needs_no_auth(client):
    assert client.get("/health").json() == {"status": "UP"}


def test_deep_health_reads_through_the_backend(client, backend):
    backend.error = CliFailed("sr-acl-cli failed: kafka down")
    response = client.get("/health?deep=true")
    assert (response.status_code, response.json()) == (502, {"error": "sr-acl-cli failed: kafka down"})


@pytest.mark.parametrize("credentials", [None, ("admin", "wrong"), ("other", "secret")])
def test_bad_credentials_get_401(client, credentials):
    response = client.get("/acls", auth=credentials)

    assert response.status_code == 401
    assert response.json() == {"error": "unauthorized"}
    assert response.headers["WWW-Authenticate"].startswith("Basic")


def test_add_without_content_type_then_list(client, auth):
    body = '{"principals":["c17.svc-a"],"topics":["orders"],"operations":["SUBJECT_READ"]}'

    added = client.post("/acls", content=body, auth=auth)  # no Content-Type, like `curl -d`
    listed = client.get("/acls?principal=c17.svc-a", auth=auth)

    assert added.status_code == 200
    assert added.json()["subjectAcls"] == [
        {"principal": "c17.svc-a", "subject": "orders-key", "operations": ["SUBJECT_READ"]},
        {"principal": "c17.svc-a", "subject": "orders-value", "operations": ["SUBJECT_READ"]},
    ]
    assert listed.json() == added.json()


def test_remove(client, auth):
    body = {"principals": ["p"], "subjects": ["s"], "operations": ["SUBJECT_READ"]}
    client.post("/acls", json=body, auth=auth)

    removed = client.request("DELETE", "/acls", json=body, auth=auth)

    assert removed.json()["subjectAcls"] == [{"principal": "p", "subject": "s", "operations": []}]


def test_invalid_body_gets_400_with_the_field(client, auth):
    response = client.post("/acls", json={"principals": [], "operations": ["X"], "extra": 1}, auth=auth)

    assert response.status_code == 400
    assert "principals" in response.json()["error"]
    assert "extra" in response.json()["error"]


def test_malformed_json_gets_400(client, auth):
    assert client.post("/acls", content="not json", auth=auth).status_code == 400


@pytest.mark.parametrize("error, status", [
    (CliRejectedArguments("Invalid operation specified : NOPE"), 400),
    (CliFailed("sr-acl-cli failed: boom"), 502),
    (WriteQueueFull("too many pending writes"), 503),
    (CliTimedOut("sr-acl-cli did not finish within 60s"), 504),
])
def test_service_errors_map_to_status_codes(client, backend, auth, error, status):
    backend.error = error

    response = client.post("/acls", json={"principals": ["p"], "operations": ["GLOBAL_READ"]}, auth=auth)

    assert (response.status_code, response.json()) == (status, {"error": str(error)})
    if status == 503:
        assert response.headers["Retry-After"] == "5"


def test_operations_catalog(client, auth):
    catalog = client.get("/operations", auth=auth).json()

    assert catalog["subject"][0] == "SUBJECT_READ"
    assert catalog["global"] == ["GLOBAL_COMPATIBILITY_READ", "GLOBAL_COMPATIBILITY_WRITE", "GLOBAL_READ"]


def test_unknown_route_uses_the_error_body(client):
    assert client.get("/nope").json() == {"error": "Not Found"}
