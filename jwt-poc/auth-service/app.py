"""NGINX auth_request service: HTTP Basic (Service name + password) -> RS256 JWT for Schema Registry.

NGINX sends every client request's headers (no body) to /check. On success we answer 200 with
`X-SR-Token: <JWT>`; NGINX sends it upstream as `Authorization: Bearer <JWT>`, replacing whatever the
client sent. 401 goes back to the client and the request never reaches Schema Registry.
Passwords are checked against an in-memory snapshot of sr_services, reloaded every RELOAD_SECONDS.
"""
import base64
import contextlib
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
RELOAD_SECONDS = int(os.environ.get("RELOAD_SECONDS", "60"))
KID = os.environ.get("JWT_KID", "sr-auth-proxy-1")
DB = dict(server=os.environ.get("DB_HOST", "mssql"), user=os.environ.get("DB_USER", "sr_jwt_auth"),
          password=os.environ["DB_PASSWORD"], database=os.environ.get("DB_NAME", "platform"),
          login_timeout=3, timeout=5)

with open(os.environ.get("JWT_PRIVATE_KEY", "/keys/private.pem"), "rb") as f:
    PRIVATE_KEY = serialization.load_pem_private_key(f.read(), password=None)
_pub = PRIVATE_KEY.public_key().public_numbers()
_b64 = lambda n: base64.urlsafe_b64encode(n.to_bytes((n.bit_length() + 7) // 8, "big")).rstrip(b"=").decode()
JWKS = {"keys": [{"kty": "RSA", "use": "sig", "alg": "RS256", "kid": KID, "n": _b64(_pub.n), "e": _b64(_pub.e)}]}

# Credentials snapshot: the whole sr_services table, loaded at startup and then reloaded every RELOAD_SECONDS
# by one background thread (one connection, one SELECT). Requests only read the snapshot, never the DB.
# If a reload fails the last good snapshot stays in use; if the first load fails the service doesn't start.
_users: dict[str, str] = {}              # name -> bcrypt hash; replaced as a whole on each reload
_loaded_at = 0.0
# bcrypt costs ~0.2 s per check, so remember passwords already verified against a given stored hash:
# (name, hash) -> HMAC(password). A new hash in the snapshot (password change) no longer matches.
# The HMAC key is per process, so this never holds a password.
_VERIFIED_KEY = secrets.token_bytes(32)
_verified: dict[tuple[str, str], bytes] = {}
_lock = threading.Lock()
stats = {"db_loads": 0, "db_errors": 0, "bcrypt_checks": 0, "verified_hits": 0, "denied": 0}


def _digest(password: str) -> bytes:
    return hmac.new(_VERIFIED_KEY, password.encode(), hashlib.sha256).digest()


def _load() -> None:
    global _users, _loaded_at
    with pymssql.connect(**DB) as conn, conn.cursor() as cur:
        cur.execute("SELECT name, password_hash FROM sr_services")
        users = {name: h for name, h in cur.fetchall()}
    with _lock:
        _users, _loaded_at = users, time.time()
        for key in [k for k in _verified if users.get(k[0]) != k[1]]:   # changed or removed Services
            del _verified[key]
    stats["db_loads"] += 1
    log.info("loaded %d Services from sr_services", len(users))


def _reload_loop() -> None:
    while True:
        time.sleep(RELOAD_SECONDS)
        try:
            _load()
        except Exception as e:  # keep serving the last good snapshot
            stats["db_errors"] += 1
            log.warning("reload failed, keeping the snapshot from %.0f s ago: %s", time.time() - _loaded_at, e)


@contextlib.asynccontextmanager
async def _lifespan(_app):
    _load()  # raises if SQL Server is unreachable -> uvicorn exits: no start without credentials
    threading.Thread(target=_reload_loop, name="sr-services-reload", daemon=True).start()
    yield


app = FastAPI(lifespan=_lifespan)


def _verify(name: str, password: str) -> bool:
    with _lock:
        stored = _users.get(name)
    if stored is None:
        return False
    d = _digest(password)
    if hmac.compare_digest(_verified.get((name, stored), b""), d):
        stats["verified_hits"] += 1
        return True
    stats["bcrypt_checks"] += 1
    if not bcrypt.checkpw(password.encode(), stored.encode()):
        return False
    with _lock:
        if _users.get(name) == stored:
            _verified[(name, stored)] = d
    return True


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
    return {"ok": True, "services": len(_users), "snapshot_age_s": round(time.time() - _loaded_at, 1), **stats}


@app.api_route("/check", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
def check(request: Request):
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("basic "):
        return _deny(401, "basic-credentials-required")
    try:
        name, _, password = base64.b64decode(auth[6:]).decode().partition(":")
    except Exception:
        return _deny(401, "malformed-basic-credentials")
    if not _verify(name, password):
        log.info("denied %s %s %s", name, request.headers.get("x-original-method"),
                 request.headers.get("x-original-uri"))
        return _deny(401, "invalid-credentials")
    now = int(time.time())
    token = jwt.encode({"sub": name, "iss": ISSUER, "aud": AUDIENCE, "iat": now, "exp": now + TTL},
                       PRIVATE_KEY, algorithm="RS256", headers={"kid": KID})
    return Response(status_code=200, headers={"X-SR-Token": token, "X-Auth-Service": "ok"})
