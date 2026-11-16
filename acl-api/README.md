# acl-api: REST API for sr-acl-cli

A small FastAPI service. Each request runs the real `sr-acl-cli` (cp-schema-registry 7.9.0) as a
subprocess, so validation and the add/remove merge rules are the CLI's own. The service needs network
access to Kafka only, not to Schema Registry: the CLI reads and writes the ACL topic (`_schemas_acl`),
and the registry's ACL authorizer picks the records up from there.

This directory is self-contained, so you can copy just `acl-api/` to an air-gapped host:

| Path | What |
|---|---|
| `acl_api/` | the service (layout below) |
| `requirements.txt`, `wheels/` | runtime dependencies, as wheels for linux amd64 + arm64 (the build runs `pip install --no-index`) |
| `sr-acl-cli/` | the CLI scripts and the 4 jar directories on its classpath (277 MB, not in git: `./fetch-cli-jars.sh`) |
| `tools-log4j.properties` | CLI logging to stderr (stdout carries only CLI output) |
| `config/sr-acl-cli.properties` | **edit this**: Kafka connection + ACL topic, same values as your registry's |
| `Dockerfile`, `docker-compose.yml`, `.env.example` | build and run |
| `save-base-images.sh` | run *before* moving: saves the 2 base images to `images/base-images.tar` |
| `tests/`, `requirements-dev.txt`, `pyproject.toml` | unit and API tests |
| `stress/stress.py`, `STRESS_RESULTS.md` | load test (stdlib only) and its results |

## Run

```bash
./fetch-cli-jars.sh                      # only after a fresh git clone (jars aren't in git)
./save-base-images.sh linux/amd64        # on a machine with internet, before copying
# --- on the air-gapped host ---
docker load -i images/base-images.tar
cp .env.example .env                     # set ACL_API_PASSWORD
vi config/sr-acl-cli.properties          # your Kafka bootstrap servers / SASL settings
docker compose up -d --build
```

If your internal registry already mirrors the base images, skip the tar and pass
`--build-arg PYTHON_IMAGE=... --build-arg JRE_IMAGE=...` instead.

## Configuration

These are environment variables, read and validated at startup by `acl_api/settings.py`
(pydantic-settings). An invalid value stops the service with a message naming the field.

| Variable | Default | |
|---|---|---|
| `ACL_API_PASSWORD` | — (required) | HTTP Basic password |
| `ACL_API_USER` | `acl-admin` | HTTP Basic user |
| `ACL_API_CLI_PATH` | `/opt/sr-acl-cli/bin/sr-acl-cli` | must exist and be executable |
| `ACL_API_CLI_CONFIG` | `/etc/acl-api/sr-acl-cli.properties` | must exist |
| `ACL_API_CLI_TIMEOUT` | `60` | seconds before a CLI run is killed (→ 504) |
| `ACL_API_MAX_PENDING_WRITES` | `200` | unfinished writes (running + queued); more get 503 |
| `ACL_API_WRITE_BATCH_WINDOW` | `0.05` | seconds to collect concurrent writes before each CLI call, so a burst merges |
| `ACL_API_LOG_LEVEL` | `INFO` | |

## Code layout

```
acl_api/
  main.py               create_app(): wires settings, backend, service, routes
  settings.py           Settings (pydantic-settings, ACL_API_*)
  errors.py             errors below the HTTP layer
  domain/
    operations.py       Operation enum, resolve_operations() (case, alias, '*')
    models.py           AclChange (request), AclSnapshot (ACL state), AclListing/SubjectAcl/GlobalAcl (responses)
    batching.py         merge_changes(): which changes can share one CLI call (pure rules)
  cli/
    sr_acl_cli.py       SrAclCli: runs the CLI, builds its arguments, classifies its failures
    list_output.py      parses `--list` output (Java Map.toString) into an AclSnapshot
  services/
    acl_service.py      AclService (use cases) and AclBackend, the protocol SrAclCli implements
    coalescer.py        Coalescer: one shared read at a time, never stale
    write_batcher.py    WriteBatcher: the single writer; queues writes, merges them, one CLI call at a time
  api/
    routes.py           endpoints
    dependencies.py     auth, service lookup, body parsing
    error_handlers.py   error -> status code + {"error": ...}
    schemas.py          HTTP-only bodies (Health, OperationCatalog, ErrorBody)
```

Dependencies point inward: `api` → `services` → `domain`. `services` only knows the `AclBackend`
protocol, and `cli` implements it, so tests (and any future backend) plug in through
`create_app(settings, backend)`.

## Development

```bash
pip install -r requirements-dev.txt && python -m pytest       # Python 3.12
# or without a local Python:
docker run --rm -v "$PWD:/w:ro" -w /w python:3.12-slim sh -c "pip install -q -r requirements-dev.txt && python -m pytest -q -p no:cacheprovider"
```

**Updating dependencies:** change `requirements.txt`, then re-download the wheels for both platforms:

```bash
for p in manylinux2014_x86_64 manylinux2014_aarch64; do
  docker run --rm -v "$PWD:/w" -w /w python:3.12-slim pip download -r requirements.txt -d wheels \
    --only-binary=:all: --platform $p --python-version 3.12 --implementation cp
done
```

Remove the wheels of old versions afterwards.

## API

HTTP Basic auth (`ACL_API_USER` / `ACL_API_PASSWORD`) on everything except `/health`. Whoever has
the credential can grant anything. Interactive docs are at `/docs`.

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
- **Writes:** queued and run one CLI call at a time (no lost updates). Each call takes the whole
  queue and merges it into as few calls as possible:
  - **Same subject, topic and operations:** requests from different principals become one call
    with all of them, e.g. 20 principals asking for `SUBJECT_READ` on one subject.
  - **Same principal and operations:** requests for different subjects become one call.
  - **Never merged:** requests using `*`, and an add next to a remove.
  - **Rejected merged call:** its requests are retried one by one, so only the invalid one gets the 400.
- **Bursts:** 20, 50 or 100 principals on one subject at once all finish in ~3.7 s (before: 32 s
  for 20). Sustained mergeable writes reach ~25 writes/s at 100 concurrent writers.
- **Writes that share nothing** (different principal *and* subject or operations) still need a CLI
  call each: ~0.7/s.
- **Queue limit:** past 200 unfinished writes, the service returns `503` + `Retry-After`.
- **Footprint:** at most 2 JVMs at once, peak ~300 MiB and 2 CPUs.
- **Replicas:** run one, or send all writes to one (writes aren't serialized across replicas).
