# PoC: NGINX + auth service + JWT in front of Schema Registry (ACL authorizer)

**Answer to the critical question: yes.** Confluent's ACL authorizer (`SchemaRegistryAclAuthorizer`, CP 7.9.0)
accepts a principal that came from a bearer token. The security plugin's `AuthenticationHandler` validates
the RS256 JWT against the JWKS. The `sub` claim then becomes the principal (`The principal is svc-reader`),
and the ACL authorizer allows or denies on that name, exactly as it does for a Basic-auth user. No custom
plugin was needed. The config from the brief worked as given, including `confluent.schema.registry.auth.mechanism=JETTY_AUTH`.

All 15 checks pass (20 result rows; 4b, 11b, 12b, 12c and 13b are log-based or extra rows). Source run: `results/jwt-results.md`.

Checks 14 and 15 were added, and check 11 was changed, when the auth service moved from a per-request DB lookup with a
10-minute cache of successes to a **whole-table snapshot reloaded every 60 s**. That's the same model as the main repo's
SnapshotLoginModule.

## Design as built

```
client --Basic svc:pw--> nginx:10000 --auth_request /_auth (headers only)--> auth-service:8080/check
                          |                          bcrypt vs in-memory snapshot of sr_services (no DB access)
                          |                          200 + X-SR-Token: <RS256 JWT>  |  401
                          |                          (background: SELECT name, password_hash FROM sr_services every 60 s)
                          '--Authorization: Bearer $sr_token--> sr-jwt-1 (leader) / sr-jwt-2 (follower), round-robin
                                                                validates JWT via http://auth-service:8080/jwks.json
```

- **Layout:** everything lives in `jwt-poc/`, which is its own compose project (`sr-jwt-poc`). It joins the main stack's
  network and reuses its **Kafka** (SCRAM user `schema-registry`, covered by the existing `_schemas*` and
  `schema-registry*` prefix ACLs) and its **SQL Server** `platform` database.
- **What it doesn't touch:** the main stack's Schema Registry, scripts and topics.
- **Its own registry cluster:** group `schema-registry-jwt`, topics `_schemas_jwt` and `_schemas_jwt_acl`, two nodes,
  **no host ports**. Only NGINX is published.
- **Auth service:** `auth-service/app.py` (FastAPI + PyJWT + bcrypt + pymssql).
  - **Database access:** reads `sr_services(name, password_hash)` as the read-only login `sr_jwt_auth`.
  - **Signing key:** the RSA key pair is generated once by `scripts/up.sh` into `keys/` (gitignored).
  - **Token:** RS256, `kid=sr-auth-proxy-1`. Claims are `sub`, `iss=sr-auth-proxy`, `aud=schema-registry`, `iat` and `exp=now+300`.
  - **Credentials in memory:** the whole table is loaded at startup and reloaded every 60 s by one thread (one
    connection, one `SELECT`). Requests only read the snapshot.
  - **Reload failure:** if a reload fails, the last good snapshot stays. If the **first** load fails, the service exits.
  - **Skipping bcrypt:** a password already verified against a given stored hash is remembered as HMAC(password), keyed on
    (name, hash), so bcrypt runs only once. A changed hash (new password) invalidates that entry.
- **Test data:**
  - `svc-reader` has `SUBJECT_READ` on `orders-value`.
  - `svc-other` has no ACLs.
  - `platform` has every operation on `*`.
  - `platform` registered `orders-value` (id 1) and `payments-value` (id 2) through NGINX.

Run: `./scripts/up.sh && ./scripts/test.sh` (test run ~4 min, mostly waiting for reloads). Remove: `./scripts/down.sh`.

## Results

| # | Check | Expected | Actual | Result |
|---|---|---|---|---|
| 1 | svc-reader GET `orders-value` latest via NGINX | 200 | 200 | PASS |
| 2 | svc-reader GET `payments-value` latest | 403 from SR (40301) | 403/40301 `User is denied operation Read on Subject: payments-value` | PASS |
| 3 | svc-other GET `orders-value` latest | 403 | 403/40301 | PASS |
| 4 | Wrong password | 401 from auth service | 401, `X-Auth-Service: invalid-credentials`, `WWW-Authenticate: Basic` | PASS |
| 4b | …never reached SR | marker absent from SR logs, NGINX `upstream=-` | 0 hits, `upstream=-` | PASS |
| 5 | svc-reader GET `/schemas/ids/1` (orders) \| `/schemas/ids/2` (payments) | 200 \| 403 | 200 \| 403 | PASS |
| 6 | POST `orders-value/versions`: svc-reader \| platform | 403 \| 200 | 403 \| 200 (`{"id":1}`) | PASS |
| 7 | Forged `platform` token (other key, same `kid`): a) as Bearer via NGINX, b) valid Basic + 2nd `Authorization: Bearer` header, c) valid Basic + client-sent `X-SR-Token`, d) Bearer direct to SR | 401 \| rejected \| 403 \| 401 | 401 \| **400** \| 403 \| 401 | PASS (see differences, item 3) |
| 8 | Direct to `sr-jwt-1:8081` from inside the network: no auth, forged key, expired, wrong `aud`, plus wrong `iss`, `alg:none`, Basic `platform:…` | all 401; valid token control 200 | all 401; control 200 | PASS |
| 9 | JWKS over plain HTTP | accepted, or record that HTTPS is needed | accepted: `http://` JWKS URL works, no HTTPS requirement | PASS |
| 10 | Python confluent-kafka 2.8.0 Avro producer + consumer, `basic.auth.user.info=svc-reader:…`, `auto.register.schemas=false`, `use.latest.version=true`, topic `orders` | record round-trips | round-trips; schema id in record = 1 | PASS |
| 11 | SQL Server stopped, after a reload has failed: svc-reader \| svc-other \| svc-reader wrong pw | 200 \| 403 \| 401 (snapshot still used) | 200 \| 403 \| 401; snapshot age 60.8 s | PASS |
| 11b | Auth service restarted while SQL Server is down | exits (no start without credentials) | `exited/3`, `OperationalError (20009 ... Unable to connect)` | PASS |
| 12 | Auth service stopped | NGINX rejects (fails closed) | 500 from NGINX | PASS |
| 12b | …never reached SR | 0 hits, `upstream=-` | 0 hits, `upstream=-` | PASS |
| 12c | Auth service restarted → works again | 200 within 15 s | 200 after 4 s (see differences, item 2) | PASS |
| 13 | Registration sent straight to follower `sr-jwt-2`: as platform \| as svc-reader; leader has it | 200 \| 403 \| yes | 200 \| 403 \| yes | PASS |
| 13b | Forwarded request carries the token | leader logs it as `platform` | leader: `172.19.0.6 - platform "POST …?normalize=false" 200 "Java/17.0.13"` | PASS |
| 14 | 30 requests via NGINX: DB queries \| bcrypt checks | ≤ 1 (periodic reload) \| 0 | 0 \| 0 (30 snapshot + verified-password hits) | PASS |
| 15 | `svc-new` inserted into `sr_services` \| then deleted, no restart | authenticated within 65 s \| 401 within 65 s | after 22 s (403 from SR: no ACLs) \| after 60 s | PASS |

## Schema Registry config that worked

`schema-registry/sr-jwt.properties` (the container appends `host.name`, plus `leader.eligibility=false` on `sr-jwt-2`):

```properties
listeners=http://0.0.0.0:8081

kafkastore.bootstrap.servers=SASL_PLAINTEXT://kafka:29092
kafkastore.security.protocol=SASL_PLAINTEXT
kafkastore.sasl.mechanism=SCRAM-SHA-256
kafkastore.sasl.jaas.config=org.apache.kafka.common.security.scram.ScramLoginModule required username="schema-registry" password="schema-registry-secret";
kafkastore.topic=_schemas_jwt
kafkastore.topic.replication.factor=1
schema.registry.group.id=schema-registry-jwt

# No authentication.method (default NONE): rest-utils must not install its own BASIC handler.
rest.servlet.initializor.classes=io.confluent.common.security.jetty.initializer.AuthenticationHandler
oauthbearer.jwks.endpoint.url=http://auth-service:8080/jwks.json
oauthbearer.expected.issuer=sr-auth-proxy
oauthbearer.expected.audience=schema-registry
oauthbearer.sub.claim.name=sub

resource.extension.class=io.confluent.kafka.schemaregistry.security.SchemaRegistrySecurityResourceExtension
confluent.schema.registry.authorizer.class=io.confluent.kafka.schemaregistry.security.authorizer.schemaregistryacl.SchemaRegistryAclAuthorizer
confluent.schema.registry.auth.mechanism=JETTY_AUTH
confluent.schema.registry.acl.topic=_schemas_jwt_acl

confluent.license.topic=_confluent-license
confluent.license.topic.replication.factor=1
```

**How the values were confirmed:**
- **The OAuth path:** `javap` on `confluent-security-plugins-common-7.9.0.jar` shows that `AuthenticationHandler` reads these
  `oauthbearer.*` keys. `AuthUtils.createHttpLoginService` turns on "IDP OAuth" when both
  `oauthbearer.expected.issuer` and `oauthbearer.jwks.endpoint.url` are set.
- **The JWKS URL:** its doc string says "The URL can be HTTP(S)-based".
- **`JETTY_AUTH`:** SR started and authorized on the Jetty principal, so the value is correct.

The NGINX side (`nginx/nginx.conf`):

```nginx
location / {
  auth_request /_auth;
  auth_request_set $sr_token $upstream_http_x_sr_token;
  auth_request_set $auth_reason $upstream_http_x_auth_service;
  add_header X-Auth-Service $auth_reason always;
  proxy_set_header Authorization "Bearer $sr_token";   # replaces the client's Authorization
  proxy_pass http://schema_registry;                   # sr-jwt-1, sr-jwt-2 (resolve, round-robin)
}
location = /_auth {
  internal;
  proxy_pass http://auth_service/check;
  proxy_pass_request_body off;
  proxy_set_header Content-Length "";
  proxy_set_header X-Original-URI $request_uri;
  proxy_set_header X-Original-Method $request_method;
}
```

## Log excerpts: the principal Schema Registry resolved

The full excerpt from the run is in `results/jwt-principal-log.txt`.

Startup (`sr-jwt-1`):
```
DEBUG Configured IDP issuer: sr-auth-proxy, IDP key url: http://auth-service:8080/jwks.json (io.confluent.common.security.jetty.initializer.AuthenticationHandler)
DEBUG Confluent OAuth enabled: false, IDP Oauth is enabled: true (io.confluent.common.security.jetty.initializer.AuthenticationHandler)
INFO License for single cluster, single node (io.confluent.license.LicenseManager)
```

Check 1 (allowed) and checks 2 and 3 (denied by the ACL authorizer, on the JWT principal):
```
DEBUG AUTHORIZATION header present, processing HTTP authorization (io.confluent.common.security.jetty.CompositeAuthenticator)
DEBUG Processing new Jwt login request. (io.confluent.common.security.jetty.JwtLoginService)
DEBUG The principal is svc-reader for qtp2102368942-34 (io.confluent.common.security.auth.AuthenticationFilter)
INFO 172.19.0.7 - svc-reader [...] "GET /subjects/orders-value/versions/latest HTTP/1.1" 200 224 (io.confluent.rest-utils.requests)

DEBUG Subject Pattern operation SUBJECT_READ search FAILED for user svc-reader and subject payments-value (...SchemaRegistryAclAuthorizer)
INFO 172.19.0.7 - svc-reader [...] "GET /subjects/payments-value/versions/latest HTTP/1.0" 403 89 (io.confluent.rest-utils.requests)

DEBUG The principal is svc-other for qtp2102368942-40 (io.confluent.common.security.auth.AuthenticationFilter)
DEBUG Subject operation SUBJECT_READ search FAILED for user svc-other and subject orders-value (...SchemaRegistryAclAuthorizer)
INFO 172.19.0.7 - svc-other [...] "GET /subjects/orders-value/versions/latest HTTP/1.1" 403 87 (io.confluent.rest-utils.requests)
```

Check 13 (follower `sr-jwt-2` → leader `sr-jwt-1`, token forwarded):
```
sr-jwt-2  DEBUG The principal is platform for qtp2102368942-34 (io.confluent.common.security.auth.AuthenticationFilter)
sr-jwt-2  INFO 172.19.0.8 - platform [...] "POST /subjects/jwt-forward-test-value/versions HTTP/1.1" 200 8 "python-requests/2.32.3"
sr-jwt-1  INFO 172.19.0.6 - platform [...] "POST /subjects/jwt-forward-test-value/versions?normalize=false HTTP/1.1" 200 8 "Java/17.0.13"
sr-jwt-2  DEBUG Subject Pattern operation SUBJECT_WRITE search FAILED for user svc-reader and subject jwt-forward-test-value (...SchemaRegistryAclAuthorizer)
```

Check 8 (rejections from the token validator):
```
DEBUG Failed to process token JWT rejected due to invalid signature. ... {"alg":"RS256","kid":"sr-auth-proxy-1","typ":"JWT"}
JwtVerificationException: Failed to validate authentication token : InvalidJwtException ... [[JWT aud claim has unexpected value ...
```

Auth service: check 15 (svc-new added, then removed), then check 11 (SQL Server stopped), then SQL Server back:
```
18:23:37 INFO loaded 4 Services from sr_services
18:24:37 INFO loaded 3 Services from sr_services
18:25:37 WARNING reload failed, keeping the snapshot from 60 s ago: (20009, b'DB-Lib error message 20009 ... Unable to connect: Adaptive Server is unavailable or does not exist (mssql)
18:25:49 INFO loaded 3 Services from sr_services
```

NGINX access log (`upstream=-` means the request never reached Schema Registry):
```
ACCESS GET /subjects/orders-value/versions/latest status=200 upstream=172.19.0.6:8081 user=svc-reader auth=ok
ACCESS GET /subjects/orders-value/versions/latest status=401 upstream=- user=svc-reader auth=invalid-credentials
```

## Where behaviour differed from the design

1. **Nothing had to change on the Schema Registry side.** `rest.servlet.initializor.classes=...AuthenticationHandler`
   and `auth.mechanism=JETTY_AUTH` are correct as written. The one thing we deliberately did was to leave
   `authentication.method` unset (NONE). We didn't test what happens with `BASIC` set alongside the handler.
2. **Outages surface as 500.** NGINX passes only 2xx, 401 and 403 from the auth sub-request to the client; anything
   else becomes **500**.
   - **Auth service down (check 12):** the client gets 500. It fails closed. If clients should see 503,
     add an `error_page 500 =503` mapping.
   - **SQL Server down:** clients aren't affected, because the snapshot is kept (check 11). They get 500 only when an auth
     service has to *start* without the DB (check 11b).
   - **Recovery:** after the auth service container comes back, NGINX returned 500 ("no live upstreams") for a few seconds
     (4 s in check 12c). With `server … resolve`, NGINX removes a server whose name stops resolving: Docker DNS drops the name
     of a stopped container. NGINX re-resolves every `valid=5s`. With static addresses this doesn't apply.
3. **Duplicate `Authorization` headers get 400 from NGINX itself (check 7b).** NGINX rejects them before
   `auth_request` runs (`auth=-` in the access log). The brief's expected outcome was overwrite or reject, and the result is reject.
   - **Single header:** when the client sends one `Authorization: Bearer <forged>`, the auth service answers 401 `basic-credentials-required`.
   - **Client-sent `X-SR-Token`:** ignored, because `$sr_token` comes from the sub-request's response (7c: still svc-reader, 403).
4. **Wording of the 403 message:** a denied `/schemas/ids/<id>` returns `User is denied operation on this server.`
   rather than naming the subject.
5. **A Service with no ACLs at all gets 403 on `GET /subjects`** (check 15), not 200 with an empty list. The subject list
   is filtered on `SUBJECT_COMPATIBILITY_READ`, so svc-reader (only `SUBJECT_READ`) also gets 403 there.
6. **SR does not accept Basic directly.** The handler is `OAuthOrBasicAuthenticator`, but no Basic login service is
   configured without MDS, so `Basic platform:…` sent straight to SR gets 401 (check 8).
7. **A startup warning to note:** `WARN Skipping impersonation identity validation as client auth is not set.
   Configure 'ssl.client.authentication'`. It concerns mTLS impersonation tokens and doesn't affect this design.
8. **The license covers one node.** The cluster ran 2 nodes on the developer license, which logs
   `License for single cluster, single node`. Production would need a proper license for a multi-node cluster.
9. **What the network boundary does and doesn't do.** Not publishing SR ports stops access from the host only.
   Anything on the Docker network can still reach `sr-jwt-*:8081` directly (check 8). What protects SR there is token
   validation, not reachability: a caller needs a token signed by the auth service's key. In production, also
   restrict the network path to NGINX.
10. **Process notes:**
   - The PoC was first built with Envoy `ext_authz`. Checks 1–3 passed with Envoy too, then the proxy was switched to
     NGINX for team fit. All the results above are from NGINX.
   - The confluent-kafka consumer printed one harmless `GroupCoordinator: SASL authentication error ...
     Broker transport failure` line when it closed. The consumer uses `assign()` and the record was read correctly.

## Not covered

- HTTPS anywhere: NGINX, the JWKS endpoint and SR are all plain HTTP. HTTPS is not required by SR (check 9).
- What happens if the auth service (JWKS) is down when SR starts.
- Key rotation. SR refetches the JWKS when it sees an unknown `kid`, according to the config doc; this wasn't tested.
- Load or latency. Every request runs an auth sub-request, which is answered from memory; bcrypt runs once per Service, password and process.
