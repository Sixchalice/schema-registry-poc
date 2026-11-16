# acl-api: REST API for sr-acl-cli

A small FastAPI service for managing Confluent Schema Registry ACLs over HTTP. Each request runs
the real `sr-acl-cli` (cp-schema-registry 7.9.0) as a subprocess, so validation and the add/remove
merge rules are the CLI's own. The service needs network access to **Kafka only**, not to Schema
Registry. The CLI reads and writes the ACL topic (`_schemas_acl`), and the registry's ACL
authorizer picks the records up from there.

Contents:
[Quick start](#run) ·
[Configuration](#configuration) ·
[API](#api) ·
[How it works](#how-it-works) ·
[Write batching](#write-batching-in-detail) ·
[Capacity](#capacity) ·
[Known limitations](#known-limitations) ·
[Code layout](#code-layout) ·
[Development](#development)

This directory is self-contained, so you can copy just `acl-api/` to an air-gapped host:

| Path | What |
|---|---|
| `acl_api/` | the service ([layout](#code-layout)) |
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
| `ACL_API_WRITE_BATCH_WINDOW` | `0.05` | seconds to collect concurrent writes before each CLI call, so a burst merges ([details](#the-batch-window)) |
| `ACL_API_LOG_LEVEL` | `INFO` | |

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

The body means exactly what the CLI's arguments mean:
- **Every combination is granted.** Each principal gets each operation on each subject. A topic
  `T` stands for the two subjects `T-key` and `T-value`.
- **Global operations** (`GLOBAL_*`) apply to the principals and ignore subjects.
- **`*`** means "all", in any list. A list that contains `*` is reduced to just `*`.

POST/DELETE return the resulting operations of every record the command touched, read back with
`--list` after the write. Errors are `{"error": "..."}`:
- **400:** the CLI rejected the arguments (its own message) or the body is invalid.
- **401:** bad credentials.
- **502:** the CLI failed (e.g. Kafka unreachable).
- **503:** the write queue is full (retry after `Retry-After`, 5 s).
- **504:** the CLI timed out.

## How it works

### The big picture

```
 client ──HTTP──▶ api/        auth, body validation, error → status code
                    │
                    ▼
               AclService     use cases: list, add, remove
             ┌──────┴──────────────┐
      reads  ▼                     ▼  writes
        Coalescer             WriteBatcher ──uses──▶ merge_changes()  (domain/batching.py)
     (one shared read)     (one write at a time,
             │               merged in rounds)
             ▼                     ▼
      SrAclCli --list      SrAclCli --add / --remove
             └──────────┬──────────┘
                        ▼
               sr-acl-cli (a fresh JVM per call)
                        │  reads to the end, then writes
                        ▼
             Kafka topic _schemas_acl ──▶ Schema Registry's ACL authorizer
```

The CLI has no server side. It is a Java program that reads the whole ACL topic, changes the
records in memory and writes the changed records back. Schema Registry consumes the same topic, so
a new ACL takes effect in the registry within moments of the CLI finishing. The registry is never
called.

### Why run the real CLI instead of reimplementing it

- **Same behavior as the CLI.** Validation, operation names and aliases, and the add/remove merge
  rules are the CLI's own, so the API can't drift from it after an upgrade.
- **The cost** is that every call is a fresh JVM: about 1 s and 2 CPU-seconds just to start, and a
  write takes ~1.5 s in total. The design below exists to keep that cost under control.

### Reads: coalesced (`services/coalescer.py`)

`GET /acls` needs a `--list` run (~2.2 s). If 100 clients asked at once, 100 JVMs would start.
Instead, the `Coalescer` runs at most one `--list` at a time and shares its result:
- **No run in progress:** a new caller starts one.
- **A run is in progress:** the caller waits for the *next* run, not the current one. The current
  run started before the caller asked, so it could miss a write the caller has just seen succeed.
- **The next run** answers everyone who queued while the previous one was running.

So N concurrent readers cause at most two runs, and nobody gets data older than their own request.
Reads stay at ~2.3 s whatever the load (tested to 500 concurrent clients).

### Writes: one at a time (`services/write_batcher.py`)

Each write is a **read-modify-write** of the ACL topic. Two runs that overlap would lose an update:

```
CLI run A reads:  orders-value → {svc-1: [READ]}
CLI run B reads:  orders-value → {svc-1: [READ]}
A writes:         orders-value → {svc-1: [READ], svc-2: [READ]}
B writes:         orders-value → {svc-1: [READ], svc-3: [READ]}   ← svc-2's grant is lost
```

So exactly one write CLI runs at any moment. This is also why the service runs **one uvicorn
worker** and **one replica**. The guarantee only holds inside one process.

Serializing writes is correct, but slow: before batching, 20 requests arriving together meant 20
CLI runs back to back, ~32 s, and the last client waited for the 19 ahead of it. Batching fixes
that for the common case.

### Write batching: the idea

One CLI run already accepts several principals, subjects and operations, and grants every
combination. So these 20 requests:

```
POST /acls  {"principals":["svc-1"],  "subjects":["orders-value"], "operations":["SUBJECT_READ"]}
POST /acls  {"principals":["svc-2"],  "subjects":["orders-value"], "operations":["SUBJECT_READ"]}
...
POST /acls  {"principals":["svc-20"], "subjects":["orders-value"], "operations":["SUBJECT_READ"]}
```

can become **one** CLI run that writes exactly the same ACLs:

```
sr-acl-cli --add --principal=svc-1 --principal=svc-2 ... --principal=svc-20 \
           --subject=orders-value --operation=SUBJECT_READ
```

All 20 clients get their answer after ~3.7 s instead of up to 32 s.

### Write batching: the mechanics

1. **Queue.** A request arrives and goes into a waiting list. It isn't run right away.
2. **Window.** The writer waits 50 ms (`ACL_API_WRITE_BATCH_WINDOW`), so the rest of a burst can
   arrive.
3. **Round.** It takes *everything* in the list and groups the requests that can share one run
   (`merge_changes()`, rules below).
4. **Run.** It runs the CLI once per group, still one run at a time.
5. **Answer.** Each original caller gets its own outcome when its group's run finishes. Then it
   does its own read-back (through the Coalescer) and gets back only the records *its* request
   touched.
6. **Repeat.** Requests that arrived during the round are in the list for the next round. While
   one CLI run is busy (~1.5 s), the next round is filling up.

```
time ─────────────────────────────────────────────────────────────────▶
requests:  ▲▲▲▲▲▲▲▲ (20 arrive)            ▲▲▲ (3 arrive during the run)
writer:    [50 ms][ CLI --add, 20 principals ][50 ms][ CLI --add, 3 principals ]
replies:                                   ▼×20                              ▼×3
```

## Write batching in detail

### Merge rules (`domain/batching.py`)

`merge_changes()` is a pure function: a list of requests in, a list of CLI calls out, each saying
which original requests it covers. It does two passes:

| Pass | Requests that have the same… | …become one call with the union of their… | Example |
|---|---|---|---|
| 1 | subjects, topics and operations | principals | 20 principals → `SUBJECT_READ` on `orders-value` |
| 2 | principals and operations | subjects and topics | `svc-a` → `SUBJECT_READ` on 5 different subjects |

**Why this never changes what is written.** If two requests differ in only *one* dimension, the
combinations of their union are exactly the union of their combinations:
`{a, b} × {s} = ({a} × {s}) ∪ ({b} × {s})`. Pass 2 runs on the output of pass 1, so a full grid
also collapses into one call: `{a, b} × {s1, s2}` sent as four separate requests becomes one call.
A randomized test (`tests/test_batching.py`) checks this on hundreds of random request mixes.

Operation names are compared case-insensitively, like the CLI does.

### What is never merged, and why

| Case | Why not |
|---|---|
| Requests that differ in two dimensions, e.g. a different principal **and** a different subject | `{a, b} × {s1, s2}` would also grant `a`→`s2` and `b`→`s1`, which nobody asked for |
| Requests that use `*` (principal, subject or topic) | the CLI reduces a list containing `*` to just `*`, so the other values would be silently dropped |
| An add next to a remove | their order matters: `add p`, `remove p`, `add p` must end *with* the ACL. Only **consecutive** requests of the same action are merged, so arrival order between adds and removes is kept |
| In pass 2, requests with no subjects and no topics | they are global-only or invalid; merging an invalid one (subject operation, no subject) with a valid one would make it wrongly succeed |

Merging *within* a run of adds (or of removes) may reorder them. That's safe because adds don't
depend on each other's order (they only add operations), and the same holds for removes.

### When a merged call is rejected

If the CLI rejects a merged call (exit code 1 plus its usage table), the service can't tell which
request caused it. So it retries every request in that call **on its own**. The valid ones succeed,
and only the invalid one gets the 400 with the CLI's message. Other failures (Kafka unreachable,
timeout) are given to every request in the call, because retrying them one by one wouldn't help.

### Back-pressure and disconnects

- **Queue limit:** `ACL_API_MAX_PENDING_WRITES` (200) counts running + queued writes. Past it, a new
  write gets an immediate `503` with `Retry-After: 5`, instead of waiting until the client times out.
- **Client goes away:** the write still runs in its turn (`asyncio.shield`). A half-applied
  burst would be worse than a reply nobody reads.

### The batch window

`ACL_API_WRITE_BATCH_WINDOW` (default 50 ms) is a pause before every round.
- **What it costs:** it adds 50 ms to every write, even a lone one. That's small next to the
  ~1.5 s CLI run.
- **What it buys:** a burst whose requests arrive a few ms apart lands in one round instead of
  "the first request alone, then everyone else".
- **Setting it to `0`** still merges everything that queued while a CLI run was busy. Only the very
  first request of a burst would go alone.

### Why not run unrelated writes in parallel instead

Writes for different (principal, subject) pairs don't conflict, so they could run side by side.
That was deliberately **not** built:
- The expected burst shape is *many principals → the same subject*, which batching already turns
  into one call.
- Parallel CLI runs need one JVM each (~2 CPU-seconds per run), and still need per-key locking to be safe.
  Build it only if the burst shape changes.

## Capacity

Measured on one container capped at 2 CPUs / 2 GB. The full numbers, method and baseline are in
[STRESS_RESULTS.md](STRESS_RESULTS.md).

| What | Result |
|---|---|
| Reads | ~2.3 s at any load, tested to 500 concurrent clients (210 req/s, 0 errors) |
| Burst: 20 / 50 / 100 principals → one subject | ~3.7 s for all of them (1 CLI call). Before batching: 32 s for 20 |
| Burst: 200 / 500 | ~4.8 s / ~10 s. At 500, 300 requests got a 503 first, then succeeded on retry |
| Sustained writes that merge | ~25 writes/s at 100 concurrent writers |
| Writes that share nothing | ~0.7/s (one CLI call each, as before) |
| Footprint | at most 2 JVMs at once (one read, one write), peak ~300 MiB and 2 CPUs |

**Where the ~3.7 s per write comes from:** 50 ms window + one CLI write (~1.5 s) + the read-back
(~2.2 s, possibly waiting out a `--list` already in progress). The read-back is what lets the
response show the resulting ACLs.

## Known limitations

- **One replica, one worker.** Writes are serialized only inside one process. Two replicas (or the
  service plus someone running `sr-acl-cli` by hand) writing the same subject at the same moment can
  lose an update, exactly like two concurrent CLI runs.
- **A shared rejection costs N+1 CLI calls.** Requests merged in pass 1 have the same subjects and
  operations, so the usual rejection reasons (misspelled operation, subject with a global-only
  operation, subject operation without a subject) apply to *all* of them. 100 such requests make
  1 failed merged call + 100 failed single calls, about 150 s with the single writer blocked.
  That's no worse than before batching, but batching doesn't help here. **Possible fix:** reject
  those three cases in the API (400) before queueing; `resolve_operations()` already has what's
  needed. The cost is copying part of the CLI's validation into Python.
- **A bug inside a round would hang its requests.** `_drain` only catches errors from the CLI call.
  If `merge_changes()` ever raised, the requests in that round would never get an answer and the
  pending count would never go down. Eventually every write would get 503. It's a tested pure
  function, so this is unlikely. **Possible fix:** catch errors for the whole round and fail its
  requests.
- **Bursts above 200** hit the queue limit and depend on clients retrying. Raising
  `ACL_API_MAX_PENDING_WRITES` (e.g. to 1000) should bring 500 at once close to ~4 s, since they
  merge into one call anyway.
- **Latency floor ~3.7 s** per write, mostly the read-back. Returning `202 Accepted` without it
  would save ~1–2 s, but it changes the API (no resulting ACLs in the response).
- **Operation aliases aren't merged with each other** (`GLOBAL_SUBJECTS_READ` and `GLOBAL_READ`
  stay separate calls). That only means a missed merge, never a wrong write.

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

**How the CLI's failures are classified** (`cli/sr_acl_cli.py`). The CLI exits with 1 both for
bad arguments and for crashes. If stderr contains its usage table (a line starting `Option ` or
`Usage:`), the text before it is the rejection reason (→ 400). Otherwise it's a crash (→ 502), and
the first non-stack-frame line becomes the message. Arguments are passed as `--name=value`, so a
value starting with `-` can't be read as an option. Request values with control characters are
refused up front, because they would break parsing of the `--list` output.

## Development

```bash
pip install -r requirements-dev.txt && python -m pytest       # Python 3.12
# or without a local Python:
docker run --rm -v "$PWD:/w:ro" -w /w python:3.12-slim sh -c "pip install -q -r requirements-dev.txt && python -m pytest -q -p no:cacheprovider"
```

The tests use an in-memory fake backend (`tests/conftest.py`), plus fake executables for
`SrAclCli`, so they don't need Kafka. The load test needs a running stack:
`python3 stress/stress.py burst --levels 20,100,500 --timeout 300` (other modes: `read`, `write`,
`mixed`; see the script's docstring).

**Updating dependencies:** change `requirements.txt`, then re-download the wheels for both platforms:

```bash
for p in manylinux2014_x86_64 manylinux2014_aarch64; do
  docker run --rm -v "$PWD:/w" -w /w python:3.12-slim pip download -r requirements.txt -d wheels \
    --only-binary=:all: --platform $p --python-version 3.12 --implementation cp
done
```

Remove the wheels of old versions afterwards.
