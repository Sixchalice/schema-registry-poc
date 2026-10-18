"""NGINX auth_request service: HTTP Basic (Service name + password) -> RS256 JWT for Schema Registry.

NGINX sends every client request's headers (no body) to /check. On success we answer 200 with
`X-SR-Token: <JWT>`; NGINX sends it upstream as `Authorization: Bearer <JWT>`, replacing whatever the
client sent. 401 goes back to the client; any other status (e.g. 503 when the DB is down and the Service
isn't cached) becomes a 500 from NGINX. Either way the request never reaches Schema Registry.
"""
import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time

import bcrypt
import jwt
import pymssql
from cryptography.hazmat.primitives import serialization
from fastapi import FastAPI, Request, Response

log = logging.getLogger("auth")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

ISSUER = os.environ.get("JWT_ISSUER", "sr-auth-proxy")
AUDIENCE = os.environ.get("JWT_AUDIENCE", "schema-registry")
TTL = int(os.environ.get("JWT_TTL_SECONDS", "300"))
CACHE_TTL = int(os.environ.get("CACHE_TTL_SECONDS", "600"))
KID = os.environ.get("JWT_KID", "sr-auth-proxy-1")
DB = dict(server=os.environ.get("DB_HOST", "mssql"), user=os.environ.get("DB_USER", "sr_jwt_auth"),
          password=os.environ["DB_PASSWORD"], database=os.environ.get("DB_NAME", "platform"),
          login_timeout=3, timeout=5)

with open(os.environ.get("JWT_PRIVATE_KEY", "/keys/private.pem"), "rb") as f:
    PRIVATE_KEY = serialization.load_pem_private_key(f.read(), password=None)
_pub = PRIVATE_KEY.public_key().public_numbers()
_b64 = lambda n: base64.urlsafe_b64encode(n.to_bytes((n.bit_length() + 7) // 8, "big")).rstrip(b"=").decode()
JWKS = {"keys": [{"kty": "RSA", "use": "sig", "alg": "RS256", "kid": KID, "n": _b64(_pub.n), "e": _b64(_pub.e)}]}

# Successful checks only: name -> (HMAC of the password, expiry). Lets already-verified Services keep
# working while the DB is down. The HMAC key is per process, so the cache never holds the password.
_CACHE_KEY = secrets.token_bytes(32)
_cache: dict[str, tuple[bytes, float]] = {}
_lock = threading.Lock()
stats = {"db_lookups": 0, "cache_hits": 0, "denied": 0, "db_errors": 0}

app = FastAPI()


def _digest(password: str) -> bytes:
    return hmac.new(_CACHE_KEY, password.encode(), hashlib.sha256).digest()


def _db_hash(name: str) -> str | None:
    with pymssql.connect(**DB) as conn, conn.cursor() as cur:
        cur.execute("SELECT password_hash FROM sr_services WHERE name = %s", (name,))
        row = cur.fetchone()
        return row[0] if row else None


def _verify(name: str, password: str) -> str:
    """Returns 'ok', 'denied' or 'unavailable'."""
    d = _digest(password)
    with _lock:
        hit = _cache.get(name)
    if hit and hit[1] > time.time() and hmac.compare_digest(hit[0], d):
        stats["cache_hits"] += 1
        return "ok"
    try:
        stats["db_lookups"] += 1
        stored = _db_hash(name)
    except Exception as e:  # DB down: only cached Services get through
        stats["db_errors"] += 1
        log.warning("DB lookup failed for %s: %s", name, e)
        return "unavailable"
    if stored and bcrypt.checkpw(password.encode(), stored.encode()):
        with _lock:
            _cache[name] = (d, time.time() + CACHE_TTL)
        return "ok"
    return "denied"


def _deny(status: int, reason: str) -> Response:
    stats["denied"] += 1
    return Response(status_code=status, content=json.dumps({"error_code": status, "message": reason}),
                    media_type="application/json",
                    headers={"WWW-Authenticate": 'Basic realm="schema-registry"', "X-Auth-Service": reason})


@app.get("/jwks.json")
def jwks():
    return JWKS


@app.get("/healthz")
def healthz():
    return {"ok": True, "cached": len(_cache), **stats}


@app.api_route("/check", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
def check(request: Request):
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("basic "):
        return _deny(401, "basic-credentials-required")
    try:
        name, _, password = base64.b64decode(auth[6:]).decode().partition(":")
    except Exception:
        return _deny(401, "malformed-basic-credentials")
    result = _verify(name, password)
    if result == "unavailable":
        return _deny(503, "credential-store-unavailable")
    if result != "ok":
        log.info("denied %s %s %s", name, request.headers.get("x-original-method"),
                 request.headers.get("x-original-uri"))
        return _deny(401, "invalid-credentials")
    now = int(time.time())
    token = jwt.encode({"sub": name, "iss": ISSUER, "aud": AUDIENCE, "iat": now, "exp": now + TTL},
                       PRIVATE_KEY, algorithm="RS256", headers={"kid": KID})
    return Response(status_code=200, headers={"X-SR-Token": token, "X-Auth-Service": "ok"})
