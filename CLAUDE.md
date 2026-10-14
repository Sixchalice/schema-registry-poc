# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Docker Compose proof of concept for Confluent Schema Registry 7.9 security. It covers:
- HTTP Basic auth checked by a custom JAAS login module (`auth/`) that keeps the whole user table from SQL Server in memory (README F7). Jetty's stock `JDBCLoginModule` against the same SQL Server is kept only as the test 8 baseline (F4).
- Authorization by the Confluent Security Plugin in ACL mode (`SchemaRegistryAclAuthorizer`), with ACLs managed via `sr-acl-cli`.

The database is SQL Server because that is the user's real platform database.

The deliverable is the findings, which are in `README.md`. Its numbers and conclusions must stay consistent with `results/`.

## Commands

```bash
./scripts/up.sh      # build SR image + auth/ jar, start Kafka+SQL Server, create DB, SCRAM users, Kafka ACLs, topics, then SR
./scripts/seed.sh    # SQL Server users (waits until SR has loaded them), SR ACLs (sr-acl-cli), schemas registered as platform-admin
./scripts/test.sh    # all tests 1-10 (~3 min) -> results/results.md, results.tsv, details.log, burst-*.txt
./scripts/dr-test.sh # DR with an exporter, README §12 (~2 min) -> results/dr-results.md/.tsv; removes the DR registry after
./scripts/down.sh    # remove everything incl. volumes
```

**Running tests:**
- **No single-test selector:** `test.sh` always runs everything. To check one behavior, use curl or the CLI directly against the running stack (examples below).
- **Clean full verification:** `down.sh && up.sh && seed.sh && test.sh`.

**Common one-off operations:**
```bash
# Build the Java client (Maven runs inside the maven container; ~/.m2 is cached in .cache/m2)
docker compose run --rm --entrypoint mvn client -q -B package
# Run it: produce|consume <topic> <cluster> <scramUser> <password> <topic|record>
docker compose --profile client run --rm -T client produce orders c17 svc-a svc-a-secret topic
# Rebuild the login module, then restart SR (the jar is mounted from auth/target)
docker compose run --rm -T -v "$PWD/auth:/auth" -w /auth --entrypoint mvn client -q -B package
docker compose up -d --force-recreate schema-registry

# SQL Server shell (no host port; -I is needed for XML methods)
docker compose exec mssql /opt/mssql-tools18/bin/sqlcmd -C -I -S localhost -U sa -P Platform-secret1 -d platform

# ACLs: always use /tmp/sr.properties (the effective config, see below)
docker compose exec -T schema-registry sr-acl-cli --config /tmp/sr.properties --list

# Restart SR with extra properties appended (';'-separated); plain `up -d` restores the base config
SR_EXTRA_PROPS='schema.linking.rbac.enable=false' docker compose up -d schema-registry

# Inspect Confluent internals: copy jars out of the image, then use javap
cid=$(docker create confluentinc/cp-schema-registry:7.9.0); docker cp $cid:/usr/share/java/confluent-security/schema-registry <dir>; docker rm $cid
```

## Architecture (things that span several files)

**The bootstrap is split by dependency order.** Schema Registry won't start without its Kafka ACLs, so `up.sh` does all Kafka-side setup (SCRAM users, Kafka ACLs, topics `orders`/`payments`) *before* starting SR. `up.sh` also creates the SQL Server database from `mssql/init.sql` (once; it checks `sys.databases`). `seed.sh` only does things that need a running SR: SQL Server users, SR ACLs and schema registration. New users reach SR on its next 60 s reload, so `seed.sh` waits for `platform-admin` to stop getting 401 before registering schemas.

**Kafka (`docker-compose.yml` `kafka.command`).** It replaces the image's `run` script so the KRaft format step can bootstrap the inter-broker SCRAM user (`--add-scram`, user `broker`).
- **Super users:** `broker` and `ANONYMOUS`. The PLAINTEXT controller listener is bound to localhost only.
- **Admin tooling:** use `kafka/admin.properties` inside the kafka container.

**Schema Registry config.**
- **Effective config:** the container entrypoint copies `schema-registry/sr.properties`, appends `SR_EXTRA_PROPS`, and writes the result to `/tmp/sr.properties`. That file is the effective config, so `sr-acl-cli` must point at it. Later duplicate keys win.
- **Mounts:** `schema-registry/` is mounted read-only at `/etc/schema-registry/poc`.
- **Image:** `schema-registry/Dockerfile` only adds the SQL Server JDBC driver. The Security Plugin is already in the base image. The login module jar is mounted from `auth/target` and put on the classpath via the `CLASSPATH` env var (`schema-registry-run-class` appends to it).

**Authentication chain.**
- **Realm:** `authentication.realm=SchemaRegistryRealm` must match the entry name in `schema-registry/jaas.conf` (`poc.auth.SnapshotLoginModule`). The JAAS file is passed via `SCHEMA_REGISTRY_OPTS`.
- **What the module reads:** the whole view `sr_auth_users` in `mssql/init.sql` (never the base tables), as the read-only login `sr_auth`. The SR username is built in the view as `cluster_id + '.' + scram_user`.
- **Refresh:** a background thread reloads the whole view every 60 s (`reloadSeconds`): one connection, one query. There is no change detection (no Change Tracking); that's the DBA's recommendation, keep it that way.
- **State is static** in `UserSnapshot`: JAAS creates a new module instance per login, and the refresh thread starts on the first login, not at SR startup.
- **Startup check:** each SR container command runs `poc.auth.Preflight` (same JAAS entry, one full load) before `schema-registry-start` and exits 1 on failure. This replaces a resource extension, which the user explicitly didn't want.
- **Credential formats:** stored credentials must be plaintext, `MD5:<hex>` or `CRYPT:` (Jetty 9.4 `Credential`).
- **Load measurement:** test 8 counts connections and queries with the Extended Events sessions `sr_auth_activity` / `sr_jdbc_activity` (one DB login per module). Don't use the plan cache for connection counts; it only sees cached plans (test.sh uses it just to count full loads). The server-wide `Logins/sec` counter includes the healthcheck.

**Identity mapping.** The Kafka SCRAM user is `svc-a`; the SR principal is `c17.svc-a`; both use the same password. The Java client (`client/src/main/java/poc/Client.java`) derives the SR login from the cluster and SCRAM user arguments. The svc-b consumer group must match the Kafka ACL prefix `c17.svc-x*`.

**Benchmark twins (profile `bench`, test 8 only).** Both join the same SR group as non-leaders (`leader.eligibility=false`) and read the same `_schemas`.
- `schema-registry-noplugin` (port 8082): same in-memory login, plugin OFF (`sr-noplugin.properties`).
- `schema-registry-jdbc` (port 8083): plugin ON, Jetty's stock `JDBCLoginModule` against the same SQL Server (`jaas-jdbc.conf`, login `sr_jdbc`, views `sr_auth_users` + `sr_auth_roles`).

**DR registry (profile `dr`, `dr-test.sh` only).** `schema-registry-dr` (port 8084) is a separate registry cluster on the same Kafka: `sr.properties` with `sr-dr.properties` appended (own `schema.registry.group.id` and `_schemas_dr*` / `_exporter_dr_*` topics, covered by the existing prefix Kafka ACLs). Its ACLs are separate: use `docker compose exec -T schema-registry-dr sr-acl-cli --config /tmp/sr.properties`. The script's cleanup (also on failure, via `trap`) deletes exporters, `dr-test-*` subjects, the primary's IMPORT mode, the DR container and its topics; `KEEP_DR=1` keeps the DR registry running.

**`scripts/test.sh` mechanics.**
- **Recording:** `rec`/`check` append rows to `results/results.tsv`, and `results.md` is generated from that at the end.
- **Cleanup:** each test removes the ACLs and exporters it creates, so the script is re-runnable.
- **Restarts:** test 5 restarts SR twice: `schema.linking.rbac.enable=false`, then the shipped config (`true`). Tests 8 and 10 stop SQL Server; test 10 also restarts SR with it down (via plain `docker restart`, since compose would start `mssql` as a dependency). Use `mssql_up` to bring it back: it waits until the `platform` database is ONLINE, which comes after the healthcheck passes.
- **README tables:** README §8 holds hand-written per-test tables copied from a run. If results change, update README §1, §7 and §8 too.

## Gotchas and deliberate choices

- **`lib.sh` is bash-only** (it uses `BASH_SOURCE`). Don't `source` it from zsh. macOS `date` lacks `%N`, so `now_ms` uses perl.
- **SQL Server is x86-only,** so on this Apple Silicon machine it runs under Rosetta (starts in seconds, ~1.1 GB). It has no host port; use `mssql_q` in `lib.sh`.
- **Timings vary by run on this machine.** Compare numbers within one run only. Test 8 warms each registry up before its bursts because the bench twins start cold; without it plugin OFF looked slower than ON.
- **Exporters need `kafkastore.update.handlers=io.confluent.schema.exporter.storage.SchemaExporterUpdateHandler`.** Without it SR crashes at startup with an NPE in `SchemaExporterResourceExtension.register`.
- **`schema.linking.rbac.enable=true` in `sr.properties` is intentional.** With the default `false`, `/exporters` is completely unauthorized in ACL mode (finding F1). Keep it `true`.
- **Test 5 is permission checks only** (agreed scope). Its exporters point back at this SR without credentials and are deleted immediately. Real exporting to a destination registry lives in `dr-test.sh`, kept out of `test.sh` on purpose.
- **DR is active-passive with exporters** (README §12); cold standby via Cluster Linking was rejected. The exporter's user needs `SUBJECT_DELETE` and `GLOBAL_COMPATIBILITY_READ` beyond the three ops Confluent documents, or the exporter goes `PAUSED` (trace only in `GET /exporters/<name>/status`).
- **Subject listing filters on a different operation.** The `GET /subjects` filter uses `SUBJECT_COMPATIBILITY_READ`, not `SUBJECT_READ`. `sr-acl-cli --topic T` only covers `T-key`/`T-value`. Subject ACLs treat `*` anywhere as a glob.
- **The license is the single-node developer license** (log line `License for single cluster, single node`), not the 30-day trial.
- **Kafka ACLs for the SR principal in `up.sh` are deliberately broad prefix grants,** not a verified minimum.
