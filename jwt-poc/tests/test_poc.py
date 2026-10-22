"""Checks 1-13 of the bearer-token PoC. Runs inside the `tests` container on the compose network.

PROXY (Envoy) is the only published entry point; LEADER/FOLLOWER are the Schema Registry nodes, reachable only from
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

PROXY = "http://envoy:10000"
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
    """A real token from the auth service, exactly as Envoy would get it."""
    r = requests.get(f"{AUTH}/check/subjects", auth=creds, timeout=5)
    assert r.status_code == 200, r.text
    return r.headers["Authorization"].removeprefix("Bearer ")


def schema_of(path):
    with open(path) as f:
        return json.dumps(json.load(f))


def code(r):
    return r.status_code


# ------------------------------------------------------------------------------------------- main

@pytest.mark.main
def test_14_no_db_query_per_request(check):
    for creds in (READER, OTHER, PLATFORM):   # first use of each password may cost one bcrypt check
        requests.get(f"{PROXY}/subjects", auth=creds)
    before = requests.get(f"{AUTH}/healthz", timeout=5).json()
    for i in range(30):
        requests.get(f"{PROXY}/subjects/orders-value/versions/latest", auth=(READER, OTHER, PLATFORM)[i % 3])
    after = requests.get(f"{AUTH}/healthz", timeout=5).json()
    loads = after["db_loads"] - before["db_loads"]
    check("14", "30 requests via Envoy: DB queries (<= 1 periodic reload in the window) | bcrypt checks",
          "True|0", f"{loads <= 1}|{after['bcrypt_checks'] - before['bcrypt_checks']}",
          f"db_loads +{loads}, verified_hits +{after['verified_hits'] - before['verified_hits']}")


@pytest.mark.main
def test_01_reader_reads_orders(check):
    r = requests.get(f"{PROXY}/subjects/orders-value/versions/latest", auth=READER)
    check("1", "svc-reader GET orders-value latest via Envoy", 200, code(r), r.text[:80])


@pytest.mark.main
def test_02_reader_denied_payments(check):
    r = requests.get(f"{PROXY}/subjects/payments-value/versions/latest", auth=READER)
    check("2", "svc-reader GET payments-value latest via Envoy", "403/40301", f"{code(r)}/{r.json().get('error_code')}",
          r.json().get("message", ""))


@pytest.mark.main
def test_03_other_denied_orders(check):
    r = requests.get(f"{PROXY}/subjects/orders-value/versions/latest", auth=OTHER)
    check("3", "svc-other GET orders-value latest via Envoy", "403/40301", f"{code(r)}/{r.json().get('error_code')}",
          r.json().get("message", ""))


@pytest.mark.main
def test_04_wrong_password(check):
    m = mark("4", "wrong password; must not appear in any SR request log")
    r = requests.get(f"{PROXY}/subjects/orders-value/versions/latest?{m}=1", auth=("svc-reader", "wrong"))
    check("4", "wrong password -> 401 (auth service reason via Envoy)", "401/invalid-credentials",
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
def test_07_forged_token_via_proxy(check):
    forged = mint(FORGED_KEY, sub="platform")   # same kid as the real key, different key
    # a) forged bearer only: no Basic credentials -> auth service refuses (401), nothing goes upstream
    a = requests.get(f"{PROXY}/subjects/payments-value/versions/latest", headers=bearer(forged))
    # b) valid Basic for svc-reader AND a second Authorization header with the forged platform token.
    #    Envoy joins the two into one header; the auth service accepts the Basic part and Envoy replaces the whole
    #    header with svc-reader's JWT, so SR sees svc-reader and denies payments-value (the forged token is dropped).
    c = http.client.HTTPConnection("envoy", 10000, timeout=5)
    c.putrequest("GET", "/subjects/payments-value/versions/latest")
    c.putheader("Authorization", "Basic " + base64.b64encode(b"svc-reader:svc-reader-secret").decode())
    c.putheader("Authorization", f"Bearer {forged}")
    c.endheaders()
    rb = c.getresponse()
    b_body = rb.read().decode()[:70]
    # c) valid Basic for svc-reader + the forged platform token in X-SR-Token: Envoy takes Authorization from the
    #    auth service's response, never from the client, so SR sees svc-reader and denies payments-value.
    e = requests.get(f"{PROXY}/subjects/payments-value/versions/latest", auth=READER, headers={"X-SR-Token": forged})
    # d) forged token signed by a different key, sent straight to SR (what would arrive if Envoy passed it on)
    d = requests.get(f"{LEADER}/subjects/payments-value/versions/latest", headers=bearer(forged))
    check("7", "forged platform token: Bearer via Envoy | + valid Basic (2 Authorization headers) | "
               "valid Basic + client X-SR-Token | Bearer direct to SR", "401|403|403|401",
          f"{code(a)}|{rb.status}|{code(e)}|{code(d)}",
          f"a: {a.headers.get('X-Auth-Service')}; b: {b_body}; c: {e.text[:70]}")


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
    check("10", "confluent-kafka Avro produce+consume on orders (SR as svc-reader via Envoy)", order, got,
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

def health():
    return requests.get(f"{AUTH}/healthz", timeout=5).json()


@pytest.mark.db_down
def test_11_db_down(check):
    # Wait for a reload to fail, so the answers below come from the snapshot kept after a failed reload
    errors, deadline = health()["db_errors"], time.time() + 90
    while health()["db_errors"] == errors and time.time() < deadline:
        time.sleep(1)
    h = health()
    a = requests.get(f"{PROXY}/subjects/orders-value/versions/latest", auth=READER)
    b = requests.get(f"{PROXY}/subjects/orders-value/versions/latest", auth=OTHER)
    c = requests.get(f"{PROXY}/subjects/orders-value/versions/latest", auth=("svc-reader", "wrong"))
    check("11", "SQL Server stopped, a reload has failed: svc-reader | svc-other | svc-reader wrong pw",
          "True|200|403|401", f"{h['db_errors'] > errors}|{code(a)}|{code(b)}|{code(c)}",
          f"snapshot age {h['snapshot_age_s']} s; svc-other authenticates and is denied by the ACL, as normal")


# ---------------------------------------------------------------------- auth outage (check 12)

@pytest.mark.auth_down
def test_12_auth_down(check):
    m = mark("12", "auth service stopped; must not appear in any SR request log")
    r = requests.get(f"{PROXY}/subjects/orders-value/versions/latest?{m}=1", auth=READER)
    check("12", "auth service stopped: svc-reader via Envoy (fails closed, 503 from Envoy)", "503/envoy",
          f"{code(r)}/{r.headers.get('server')}", "SR-side evidence: row 12b")
