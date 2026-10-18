"""Checks 1-13 of the bearer-token PoC. Runs inside the `tests` container on the compose network.

PROXY (NGINX) is the only published entry point; LEADER/FOLLOWER are the Schema Registry nodes, reachable only from
inside the network (check 8, 13). Checks that need log evidence (4, 12, 13) write a marker to MARKERS; the
wrapper (scripts/test.sh) greps the container logs for it afterwards.
"""
import base64
import http.client
import json
import time
import uuid

import jwt
import pytest
import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

PROXY = "http://nginx:10000"
LEADER = "http://sr-jwt-1:8081"
FOLLOWER = "http://sr-jwt-2:8081"
AUTH = "http://auth-service:8080"
MARKERS = "/results/markers.tsv"
CT = {"Content-Type": "application/vnd.schemaregistry.v1+json"}

READER = ("svc-reader", "svc-reader-secret")
OTHER = ("svc-other", "svc-other-secret")
PLATFORM = ("platform", "platform-secret")

with open("/keys/private.pem", "rb") as f:
    REAL_KEY = serialization.load_pem_private_key(f.read(), password=None)
FORGED_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
KID = "sr-auth-proxy-1"


def mark(cid, note=""):
    m = f"probe-{cid}-{uuid.uuid4().hex[:12]}"
    with open(MARKERS, "a") as f:
        f.write(f"{cid}\t{m}\t{note}\n")
    return m


def mint(key=REAL_KEY, sub="platform", iss="sr-auth-proxy", aud="schema-registry", exp_in=300, kid=KID):
    now = int(time.time())
    return jwt.encode({"sub": sub, "iss": iss, "aud": aud, "iat": now, "exp": now + exp_in},
                      key, algorithm="RS256", headers={"kid": kid})


def bearer(t):
    return {"Authorization": f"Bearer {t}"}


def issued_token(creds):
    """A real token from the auth service, exactly as NGINX would get it."""
    r = requests.get(f"{AUTH}/check", auth=creds, timeout=5)
    assert r.status_code == 200, r.text
    return r.headers["X-SR-Token"]


def schema_of(path):
    with open(path) as f:
        return json.dumps(json.load(f))


def code(r):
    return r.status_code


# ------------------------------------------------------------------------------------------- main

@pytest.mark.main
def test_01_reader_reads_orders(check):
    r = requests.get(f"{PROXY}/subjects/orders-value/versions/latest", auth=READER)
    check("1", "svc-reader GET orders-value latest via NGINX", 200, code(r), r.text[:80])


@pytest.mark.main
def test_02_reader_denied_payments(check):
    r = requests.get(f"{PROXY}/subjects/payments-value/versions/latest", auth=READER)
    check("2", "svc-reader GET payments-value latest via NGINX", "403/40301", f"{code(r)}/{r.json().get('error_code')}",
          r.json().get("message", ""))


@pytest.mark.main
def test_03_other_denied_orders(check):
    r = requests.get(f"{PROXY}/subjects/orders-value/versions/latest", auth=OTHER)
    check("3", "svc-other GET orders-value latest via NGINX", "403/40301", f"{code(r)}/{r.json().get('error_code')}",
          r.json().get("message", ""))


@pytest.mark.main
def test_04_wrong_password(check):
    m = mark("4", "wrong password; must not appear in any SR request log")
    r = requests.get(f"{PROXY}/subjects/orders-value/versions/latest?{m}=1", auth=("svc-reader", "wrong"))
    check("4", "wrong password -> 401 (auth service reason via NGINX)", "401/invalid-credentials",
          f"{code(r)}/{r.headers.get('X-Auth-Service')}", "SR-side evidence: row 4b")


@pytest.mark.main
def test_05_schema_ids(check):
    ids = {s: requests.get(f"{PROXY}/subjects/{s}/versions/latest", auth=PLATFORM).json()["id"]
           for s in ("orders-value", "payments-value")}
    a = requests.get(f"{PROXY}/schemas/ids/{ids['orders-value']}", auth=READER)
    b = requests.get(f"{PROXY}/schemas/ids/{ids['payments-value']}", auth=READER)
    check("5", "svc-reader GET /schemas/ids/<orders id> | <payments id>", "200|403", f"{code(a)}|{code(b)}",
          f"orders id={ids['orders-value']}, payments id={ids['payments-value']}; {b.text[:90]}")


@pytest.mark.main
def test_06_register(check):
    body = json.dumps({"schema": schema_of("/schemas/orders-value.avsc")})  # same schema: platform gets 200 + same id
    a = requests.post(f"{PROXY}/subjects/orders-value/versions", auth=READER, data=body, headers=CT)
    b = requests.post(f"{PROXY}/subjects/orders-value/versions", auth=PLATFORM, data=body, headers=CT)
    check("6", "POST orders-value versions: svc-reader | platform", "403|200", f"{code(a)}|{code(b)}",
          f"platform -> {b.text}")


@pytest.mark.main
def test_07_forged_token_via_nginx(check):
    forged = mint(FORGED_KEY, sub="platform")   # same kid as the real key, different key
    # a) forged bearer only: no Basic credentials -> auth service refuses (401), nothing goes upstream
    a = requests.get(f"{PROXY}/subjects/payments-value/versions/latest", headers=bearer(forged))
    # b) valid Basic for svc-reader AND a second Authorization header with the forged platform token.
    #    NGINX refuses a duplicate Authorization header outright (400, before auth_request runs).
    c = http.client.HTTPConnection("nginx", 10000, timeout=5)
    c.putrequest("GET", "/subjects/payments-value/versions/latest")
    c.putheader("Authorization", "Basic " + base64.b64encode(b"svc-reader:svc-reader-secret").decode())
    c.putheader("Authorization", f"Bearer {forged}")
    c.endheaders()
    rb = c.getresponse()
    b_title = rb.read().decode().split("<title>")[-1].split("</title>")[0]
    # c) valid Basic for svc-reader + the forged platform token in X-SR-Token: NGINX takes $sr_token from the
    #    auth sub-request's response, never from the client, so SR sees svc-reader and denies payments-value.
    e = requests.get(f"{PROXY}/subjects/payments-value/versions/latest", auth=READER, headers={"X-SR-Token": forged})
    # d) forged token signed by a different key, sent straight to SR (what would arrive if NGINX passed it on)
    d = requests.get(f"{LEADER}/subjects/payments-value/versions/latest", headers=bearer(forged))
    check("7", "forged platform token: Bearer via NGINX | + valid Basic (2 Authorization headers) | "
               "valid Basic + client X-SR-Token | Bearer direct to SR", "401|400|403|401",
          f"{code(a)}|{rb.status}|{code(e)}|{code(d)}",
          f"a: {a.headers.get('X-Auth-Service')}; b: NGINX '{b_title}'; c: {e.text[:70]}")


@pytest.mark.main
def test_08_direct_calls_rejected(check):
    url = f"{LEADER}/subjects/orders-value/versions/latest"
    cases = {
        "no auth": {},
        "forged key": bearer(mint(FORGED_KEY)),
        "expired": bearer(mint(exp_in=-600)),
        "wrong aud": bearer(mint(aud="not-schema-registry")),
        "wrong iss": bearer(mint(iss="someone-else")),
        "alg none": bearer(jwt.encode({"sub": "platform", "iss": "sr-auth-proxy", "aud": "schema-registry",
                                       "exp": int(time.time()) + 300}, None, algorithm="none")),
        "Basic platform": {"Authorization": "Basic " + base64.b64encode(b"platform:platform-secret").decode()},
    }
    got = {k: code(requests.get(url, headers=h)) for k, h in cases.items()}
    control = code(requests.get(url, headers=bearer(issued_token(READER))))
    check("8", "direct to sr-jwt-1: " + ", ".join(cases) + " | control: valid token",
          " ".join(["401"] * len(cases)) + " | 200",
          " ".join(str(got[k]) for k in cases) + f" | {control}")


@pytest.mark.main
def test_09_jwks_over_http(check):
    # SR is configured with oauthbearer.jwks.endpoint.url=http://... ; a real token validated => accepted.
    r = requests.get(f"{LEADER}/subjects/orders-value/versions/latest", headers=bearer(issued_token(READER)))
    check("9", "JWKS over plain HTTP accepted (token from http://auth-service:8080/jwks.json validates)", 200,
          code(r), "no HTTPS requirement; see SR log 'IDP key url: http://...'")


@pytest.mark.main
def test_10_avro_end_to_end(check):
    from confluent_kafka import Consumer, Producer, TopicPartition
    from confluent_kafka.schema_registry import SchemaRegistryClient
    from confluent_kafka.schema_registry.avro import AvroDeserializer, AvroSerializer
    from confluent_kafka.serialization import MessageField, SerializationContext

    sr = SchemaRegistryClient({"url": PROXY, "basic.auth.user.info": "svc-reader:svc-reader-secret"})
    ser = AvroSerializer(sr, schema_str=None, conf={"auto.register.schemas": False, "use.latest.version": True})
    de = AvroDeserializer(sr)
    kafka = {"bootstrap.servers": "kafka:29092", "security.protocol": "SASL_PLAINTEXT",
             "sasl.mechanism": "SCRAM-SHA-256", "sasl.username": "svc-a", "sasl.password": "svc-a-secret"}
    ctx = SerializationContext("orders", MessageField.VALUE)
    order = {"id": f"jwt-{uuid.uuid4().hex[:8]}", "amount": 12.5}

    p = Producer(kafka)
    delivered = {}
    p.produce("orders", value=ser(order, ctx), on_delivery=lambda e, m: delivered.update(err=e, msg=m))
    p.flush(15)
    assert delivered.get("err") is None and "msg" in delivered, delivered
    tp = delivered["msg"].partition(), delivered["msg"].offset()
    schema_id = int.from_bytes(delivered["msg"].value()[1:5], "big")

    # Read back exactly that record: `orders` also holds records from the repo's main registry (other ids)
    c = Consumer({**kafka, "group.id": "c17.svc-a.jwt-poc", "enable.auto.commit": False})
    c.assign([TopicPartition("orders", tp[0], tp[1])])
    msg = c.poll(15)
    c.close()
    got = de(msg.value(), ctx) if msg is not None and msg.error() is None else None
    check("10", "confluent-kafka Avro produce+consume on orders (SR as svc-reader via NGINX)", order, got,
          f"schema id in record={schema_id}, partition/offset={tp}")


@pytest.mark.main
def test_13_follower_forwards_to_leader(check):
    m = mark("13", "registration sent to follower sr-jwt-2; leader log must show it as platform")
    subject = "jwt-forward-test-value"
    schema = json.dumps({"type": "record", "name": "Fwd", "namespace": "poc", "doc": m,
                         "fields": [{"name": "x", "type": "string"}]})
    tok = issued_token(PLATFORM)
    r = requests.post(f"{FOLLOWER}/subjects/{subject}/versions", headers={**CT, **bearer(tok)},
                      data=json.dumps({"schema": schema}))
    denied = requests.post(f"{FOLLOWER}/subjects/{subject}/versions",
                           headers={**CT, **bearer(issued_token(READER))}, data=json.dumps({"schema": schema}))
    seen = requests.get(f"{LEADER}/subjects/{subject}/versions/latest", headers=bearer(tok)).json().get("id")
    for perm in ("", "?permanent=true"):   # cleanup, re-runnable
        requests.delete(f"{LEADER}/subjects/{subject}{perm}", headers=bearer(tok))
    check("13", "POST to follower sr-jwt-2 as platform | as svc-reader; leader has it", "200|403|True",
          f"{code(r)}|{code(denied)}|{seen == r.json().get('id')}", f"{r.text}; marker {m}; leader-log evidence: row 13b")


# ------------------------------------------------------------------------- DB outage (check 11)

@pytest.mark.warm
def test_11_warm_cache():
    r = requests.get(f"{PROXY}/subjects/orders-value/versions/latest", auth=READER)
    assert r.status_code == 200, r.text


@pytest.mark.db_down
def test_11_db_down(check):
    a = requests.get(f"{PROXY}/subjects/orders-value/versions/latest", auth=READER)
    b = requests.get(f"{PROXY}/subjects/orders-value/versions/latest", auth=OTHER)
    c = requests.get(f"{PROXY}/subjects/orders-value/versions/latest", auth=("svc-reader", "wrong"))
    # auth service answers 503 for anything it can't verify without the DB; auth_request turns non-401/403 into 500
    check("11", "SQL Server stopped: cached svc-reader | uncached svc-other | svc-reader wrong pw", "200|500|500",
          f"{code(a)}|{code(b)}|{code(c)}", f"auth reason: {b.headers.get('X-Auth-Service')}")


# ---------------------------------------------------------------------- auth outage (check 12)

@pytest.mark.auth_down
def test_12_auth_down(check):
    m = mark("12", "auth service stopped; must not appear in any SR request log")
    r = requests.get(f"{PROXY}/subjects/orders-value/versions/latest?{m}=1", auth=READER)
    check("12", "auth service stopped: svc-reader via NGINX (fails closed, 500 from NGINX)", "500/nginx",
          f"{code(r)}/{'nginx' if 'nginx' in r.text else r.text[:60]}", "SR-side evidence: row 12b")
