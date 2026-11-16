# acl-api stress test results

**Summary:**
- **Reads:** one container serves any number of concurrent reads, at a flat **~2.3 s** latency.
  Tested up to 500 concurrent clients (210 req/s, 0 errors).
- **Writes:** run at **~0.7/s** whatever the concurrency, because each one is a JVM run of the real
  CLI and they are serialized to avoid lost updates. Past 20 queued writes, the service returns `503`
  at once instead of letting clients time out.
- **Footprint:** under every load, memory stayed under ~300 MiB and CPU under 2 cores. There were no
  OOMs, restarts, lost updates or leftover ACLs.

## Setup

| | |
|---|---|
| Host | Apple Silicon Mac, Docker Desktop VM with 12 CPUs / 8 GB |
| Container | `acl-api:7.9.0`, **capped at 2 CPUs / 2 GB** (`cpus: 2`, `mem_limit: 2g`) |
| Kafka | the POC's single cp-server broker, in the same Docker VM (it shares the CPUs) |
| ACL data | the POC seed state: 2 principals, 4 ACL records |
| Load generator | `stress/stress.py` on the same host. N threads send requests back to back on keep-alive connections, with a 120 s client timeout. |
| Test date | 2026-11-16 |

**What one request costs:** each `sr-acl-cli` run is a fresh JVM. It starts, connects to Kafka,
reads `_schemas_acl` from the beginning, and then prints or writes. That takes **~1.1 s and ~2
CPU-seconds**, which dominates everything else. The Python layer adds a few milliseconds.

## Two designs

1. **Naive:** one CLI run per request, with no limit.
2. **Shipped:** the number of CLI runs is bounded.
   - **Reads are coalesced:** at most one `--list` runs at a time. Every request that arrives while
     it runs is answered by the *next* run, which starts after those requests arrived, so a response
     never predates its request.
   - **Writes are serialized:** one `--add`/`--remove` at a time, and at most
     `max_pending_writes` (20) waiting. More than that get `503` + `Retry-After: 5`.
   - **Read-back:** after a write, the response is read back with a coalesced `--list`.
   - So at most **2 JVMs** run at once (one list, one write), whatever the load.

## Results: naive design (baseline)

| Concurrent reads | Requests | OK/s | p50 | p95 | Errors |
|---|---|---|---|---|---|
| 1 | 14 | 0.88 | 1.14 s | 1.16 s | 0 |
| 5 | 25 | 1.49 | 3.31 s | 3.84 s | 0 |
| 10 | 20 | 1.08 | 8.85 s | 10.04 s | 0 |
| 25 | 25 | 0.88 | 26.32 s | 28.31 s | 0 |

CPU was pinned at the 2-core cap from 5 clients on. Memory reached **1.998 GiB of 2 GiB** at 25
clients, about 80 MB per concurrent JVM. 50 clients would have been OOM-killed, so that level wasn't
run. Throughput never got past ~1.5 req/s, and latency grew linearly with concurrency.

## Results: shipped design

### Reads (`stress.py read`, 15–20 s per level)

| Concurrent clients | Requests | OK/s | p50 | p95 | max | Errors |
|---|---|---|---|---|---|---|
| 1 | 14 | 0.89 | 1.12 s | 1.17 s | 1.17 s | 0 |
| 5 | 36 | 2.14 | 2.25 s | 2.28 s | 2.29 s | 0 |
| 10 | 71 | 4.22 | 2.25 s | 2.27 s | 2.28 s | 0 |
| 25 | 176 | 10.42 | 2.24 s | 2.28 s | 2.29 s | 0 |
| 50 | 351 | 20.88 | 2.24 s | 2.27 s | 2.27 s | 0 |
| 100 | 701 | 41.34 | 2.25 s | 2.33 s | 2.34 s | 0 |
| 250 | 2250 | 104.70 | 2.38 s | 2.49 s | 2.52 s | 0 |
| 500 | 4500 | 209.96 | 2.36 s | 2.45 s | 2.48 s | 0 |

Peak usage was 97–118 % CPU, 196 MiB of memory and 58 PIDs. Latency is one in-flight run plus your
own (≤ 2 × 1.1 s) at any load, and throughput is simply `clients / 2.3 s`. 500 concurrent clients was
the highest level tried. The limit beyond that is open sockets in uvicorn, not the CLI.

### Writes (`stress.py write`, 30 s per level)

Each client loops POST, then DELETE, on a subject of its own. A POST answered 200 whose read-back
doesn't show the added operation would count as **lost**: there were none.

| Concurrent clients | Writes OK/s (POST+DELETE) | POST p50 / p95 | DELETE p50 / p95 | 503s |
|---|---|---|---|---|
| 1 | 0.44 | 2.29 s / 2.31 s | 2.26 s / 2.31 s | 0 |
| 5 | 0.66 | 7.34 s / 8.02 s | 7.46 s / 7.97 s | 0 |
| 20 | 0.66 | 17.16 s / 30.66 s | 30.05 s / 30.50 s | 0 |
| 40 | 0.72 | — (most got an immediate 503) | — | yes, by design |

Peak usage was 202–205 % CPU and ~300 MiB. With one client, a write costs a CLI write plus a
read-back (~2.3 s). Under load, the read-back overlaps the next write, so throughput tops out at
**~0.7 writes/s**, about 1.5 s per CLI write. Queued latency is about 1.5 s × the writes ahead of you.

At 40 clients, more than 20 were waiting, so the rest got `503` within milliseconds. The first run
left 14 ACLs behind. That came from the test client, not the service: a POST succeeded, its cleanup
DELETE got 503, and the client didn't retry. The client now retries DELETE on 503, and the re-run
left **0**.

### Mixed (`stress.py mixed`, 1 writer per 6 clients, 30 s per level)

| Clients | Reads OK/s | Read p50 / p95 | Writes OK/s | Write p50 / p95 | Errors |
|---|---|---|---|---|---|
| 12 (10 R + 2 W) | 4.02 | 2.26 s / 2.38 s | 0.58 | 3.4 s / 4.4 s | 0 |
| 60 (50 R + 10 W) | 11.49 | 2.54 s / 2.71 s | 0.76 | 12.4 s / 14.8 s | 0 |

Writes barely slow reads down (p95 2.3 s → 2.7 s), because the two use separate JVM slots.

## How many requests can one container handle at once?

| | Answer |
|---|---|
| Concurrent reads | No practical limit from the design. 500 tested, 0 errors, ~2.4 s each. |
| Read throughput | ~0.43 req/s per waiting client. 210 req/s at 500 clients. |
| Concurrent writes | 1 runs at a time. Up to 20 queued (`ACL_API_MAX_PENDING_WRITES`), the rest get 503. |
| Write throughput | ~0.7/s (~40/min, ~2,500/hour) |
| Resources to give it | 2 CPUs and 512 MiB is comfortable (peak was 2 cores, ~300 MiB) |

**Scaling out** adds read capacity, but not write capacity. Writes from different replicas aren't
serialized against each other, so two of them changing the same (principal, subject) at the same
moment can lose an update, the same as two concurrent CLI runs. Run **one replica**, or send writes
to a single one.

**Ways to get more write throughput, if ever needed (not done):**
- **Batch queued writes into one CLI call.** The CLI accepts several `-p/-s/-o`, but it applies their
  cross product, so only requests with the same shape can be merged.
- **Return 202 without the read-back.** That saves ~1 s of latency, but not throughput.

## Re-check after the refactor into the `acl_api/` package

The same container and limits, with a shorter run (15–20 s per level):

| Load | Before refactor | After refactor |
|---|---|---|
| reads, 1 client | 0.89 req/s, p95 1.17 s | 0.87 req/s, p95 1.20 s |
| reads, 100 clients | 41.3 req/s, p95 2.33 s | 43.1 req/s, p95 2.37 s |
| reads, 500 clients | 210 req/s, p95 2.45 s | 212 req/s, p95 2.41 s |
| writes, 5 clients | 0.66 writes/s, p95 8.0 s | 0.64 writes/s, p95 8.3 s |
| writes, 40 clients | 0.72 writes/s + fast 503s, 0 left over | 0.72 writes/s + fast 503s, 0 left over |
| mixed, 50 R + 10 W | 11.5 reads/s, 0.76 writes/s | 12.3 reads/s, 0.76 writes/s |
| peak memory | ~300 MiB | ~283 MiB |

Same behavior. The differences are within run-to-run noise.

## Re-running

```bash
python3 stress/stress.py read  --levels 1,5,10,25,50,100,250,500 --duration 20
python3 stress/stress.py write --levels 1,5,20,40 --duration 30
python3 stress/stress.py mixed --levels 12,60 --duration 30
# --url http://host:8087 --auth user:password for another deployment
```

The write and mixed modes use a throwaway principal `stress-<timestamp>` and remove everything they
add. At the end they print how many of its ACLs are left, which should be 0. Like any CLI removal,
removed records stay on `_schemas_acl` with empty operation sets.
