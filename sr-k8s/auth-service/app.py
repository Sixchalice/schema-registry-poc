"""Auth service for NGINX auth_request: cluster + HTTP Basic -> RS256 JWT for Schema Registry.

NGINX sends every request's headers to GET /check, with the cluster (first URL path segment) in X-Cluster.
If a ServiceWallets row has that Name and Password and its Cluster has that Name, we answer 200 with
`X-SR-Token: <JWT sub=<cluster>.<name>>`, otherwise 401. Schema Registry fetches the public key from /jwks.json.

Both tables (ServiceWallets joined with Cluster) are kept in memory and reloaded every RELOAD_SECONDS.
"""
import base64
import contextlib
import logging
import os
import threading
import time

import jwt
import pymssql
from cryptography.hazmat.primitives import serialization
from fastapi import FastAPI, Request, Response

log = logging.getLogger("auth")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

ISSUER = os.environ.get("JWT_ISSUER", "sr-auth")
AUDIENCE = os.environ.get("JWT_AUDIENCE", "schema-registry")
TTL = int(os.environ.get("JWT_TTL_SECONDS", "300"))
KID = os.environ.get("JWT_KID", "sr-auth-1")
RELOAD_SECONDS = int(os.environ.get("RELOAD_SECONDS", "60"))
DB = dict(server=os.environ.get("DB_HOST", "mssql"), port=os.environ.get("DB_PORT", "1433"),
          user=os.environ.get("DB_USER", "sr_auth"), password=os.environ.get("DB_PASSWORD", ""),
          database=os.environ.get("DB_NAME", "platform"))

# Every replica must sign with the same key, so it comes from a file (a Kubernetes Secret), not generated here
with open(os.environ.get("JWT_PRIVATE_KEY", "/keys/private.pem"), "rb") as f:
    PRIVATE_KEY = serialization.load_pem_private_key(f.read(), password=None)
_pub = PRIVATE_KEY.public_key().public_numbers()
_b64 = lambda n: base64.urlsafe_b64encode(n.to_bytes((n.bit_length() + 7) // 8, "big")).rstrip(b"=").decode()
JWKS = {"keys": [{"kty": "RSA", "use": "sig", "alg": "RS256", "kid": KID, "n": _b64(_pub.n), "e": _b64(_pub.e)}]}

users: dict[tuple[str, str], str] = {}  # (Cluster.Name, ServiceWallets.Name) -> Password


def load_users() -> None:
    global users
    with pymssql.connect(**DB) as conn, conn.cursor() as cur:
        cur.execute("SELECT c.Name, s.Name, s.Password FROM ServiceWallets s JOIN Cluster c ON c.ID = s.ClusterID")
        users = {(cluster, name): password for cluster, name, password in cur.fetchall()}
    log.info("loaded %d ServiceWallets", len(users))


def reload_loop() -> None:
    while True:
        time.sleep(RELOAD_SECONDS)
        try:
            load_users()
        except Exception as e:  # keep the last good copy
            log.warning("reload failed: %s", e)


@contextlib.asynccontextmanager
async def lifespan(_app):
    load_users()  # fails startup if the DB can't be read
    threading.Thread(target=reload_loop, daemon=True).start()
    yield


app = FastAPI(lifespan=lifespan)


@app.get("/healthz")
def healthz():
    return {"ok": True, "services": len(users)}


@app.get("/jwks.json")
def jwks():
    return JWKS


@app.get("/check")
def check(request: Request):
    cluster = request.headers.get("x-cluster", "")
    name, password = "", ""
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("basic "):
        try:
            name, _, password = base64.b64decode(auth[6:]).decode().partition(":")
        except Exception:
            pass
    if not cluster or users.get((cluster, name)) != password:
        log.info("denied cluster=%s name=%s", cluster, name)
        return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="schema-registry"'})
    now = int(time.time())
    token = jwt.encode({"sub": f"{cluster}.{name}", "iss": ISSUER, "aud": AUDIENCE, "iat": now, "exp": now + TTL},
                       PRIVATE_KEY, algorithm="RS256", headers={"kid": KID})
    return Response(status_code=200, headers={"X-SR-Token": token})
