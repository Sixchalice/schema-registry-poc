# acl-api: REST API for sr-acl-cli

A small FastAPI service. Each request runs the real `sr-acl-cli` (cp-schema-registry 7.9.0) as a
subprocess, so validation and the add/remove merge rules are the CLI's own. The service needs network
access to Kafka only, not to Schema Registry: the CLI reads and writes the ACL topic (`_schemas_acl`),
and the registry's ACL authorizer picks the records up from there.

This directory is self-contained, so you can copy just `acl-api/` to an air-gapped host:

| Path | What |
|---|---|
| `app.py`, `requirements.txt` | the service |
| `wheels/` | Python wheels for linux amd64 + arm64 (the build runs `pip install --no-index`) |
| `sr-acl-cli/` | the CLI scripts and the 4 jar directories on its classpath (277 MB) |
| `tools-log4j.properties` | CLI logging to stderr (stdout carries only CLI output) |
| `config/sr-acl-cli.properties` | **edit this**: Kafka connection + ACL topic, same values as your registry's |
| `Dockerfile`, `docker-compose.yml`, `.env.example` | build and run |
| `save-base-images.sh` | run *before* moving: saves the 2 base images to `images/base-images.tar` |
| `stress/stress.py` | load test (stdlib only) |

## Run

```bash
./save-base-images.sh linux/amd64        # on a machine with internet, before copying
# --- on the air-gapped host ---
docker load -i images/base-images.tar
cp .env.example .env                     # set ACL_SERVICE_PASSWORD
vi config/sr-acl-cli.properties          # your Kafka bootstrap servers / SASL settings
docker compose up -d --build
```

If your internal registry already mirrors the base images, skip the tar and pass
`--build-arg PYTHON_IMAGE=... --build-arg JRE_IMAGE=...` instead.

## API

HTTP Basic auth (`ACL_SERVICE_USER` / `ACL_SERVICE_PASSWORD`) on everything except `/health`.
Whoever has the credential can grant anything.

```
GET    /health[?deep=true]          liveness; deep=true also runs --list (checks Kafka)
GET    /operations                  operation names
GET    /acls[?principal=&subject=]  current ACLs (exact-match filters)
POST   /acls                        --add
DELETE /acls                        --remove
body: {"principals":["c17.svc-a"], "subjects":["orders-value"], "topics":["orders"], "operations":["SUBJECT_READ"]}
```

```bash
curl -u acl-admin:$PW -X POST localhost:8087/acls \
  -d '{"principals":["c17.svc-a"],"topics":["orders"],"operations":["SUBJECT_READ"]}'
```

POST/DELETE return the resulting operations of every record the command touched, read back with
`--list` after the write. Errors are `{"error": "..."}`:
- **400:** the CLI rejected the arguments (its own message) or the body is invalid.
- **401:** bad credentials.
- **502:** the CLI failed (e.g. Kafka unreachable).
- **503:** the write queue is full (retry after `Retry-After`).
- **504:** the CLI timed out.

## Capacity

Measured on one container capped at 2 CPUs / 2 GB. The full numbers, method and baseline are in
[STRESS_RESULTS.md](STRESS_RESULTS.md).
- **Reads:** concurrent reads are coalesced into one CLI run. Latency is ~2.3 s at any load, tested
  up to 500 concurrent clients (210 req/s, 0 errors).
- **Writes:** serialized, ~0.7 writes/s. Past 20 queued writes, the service returns `503` + `Retry-After`.
- **Footprint:** at most 2 JVMs at once, peak ~300 MiB and 2 CPUs.
- **Replicas:** run one, or send all writes to one (writes aren't serialized across replicas).
