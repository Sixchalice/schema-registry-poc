# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Docker Compose proof of concept for Confluent Schema Registry 7.9 security. It covers:
- HTTP Basic auth validated by Jetty's JAAS `JDBCLoginModule` against PostgreSQL.
- Authorization by the Confluent Security Plugin in ACL mode (`SchemaRegistryAclAuthorizer`), with ACLs managed via `sr-acl-cli`.

The deliverable is the findings, which are in `README.md`. Its numbers and conclusions must stay consistent with `results/`.

## Commands

```bash
./scripts/up.sh      # build SR image, start Kafka+Postgres, SCRAM users, Kafka ACLs, topics, then SR
./scripts/seed.sh    # Postgres users, SR ACLs (sr-acl-cli), schemas registered as platform-admin
./scripts/test.sh    # all tests 1-9 (~2 min) -> results/results.md, results.tsv, details.log, burst-*.txt
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

# ACLs: always use /tmp/sr.properties (the effective config, see below)
docker compose exec -T schema-registry sr-acl-cli --config /tmp/sr.properties --list

# Restart SR with extra properties appended (';'-separated); plain `up -d` restores the base config
SR_EXTRA_PROPS='schema.linking.rbac.enable=false' docker compose up -d schema-registry

# Inspect Confluent internals: copy jars out of the image, then use javap
cid=$(docker create confluentinc/cp-schema-registry:7.9.0); docker cp $cid:/usr/share/java/confluent-security/schema-registry <dir>; docker rm $cid
```

## Architecture (things that span several files)

**The bootstrap is split by dependency order.** Schema Registry won't start without its Kafka ACLs, so `up.sh` does all Kafka-side setup (SCRAM users, Kafka ACLs, topics `orders`/`payments`) *before* starting SR. `seed.sh` only does things that need a running SR: Postgres users, SR ACLs and schema registration.

**Kafka (`docker-compose.yml` `kafka.command`).** It replaces the image's `run` script so the KRaft format step can bootstrap the inter-broker SCRAM user (`--add-scram`, user `broker`).
- **Super users:** `broker` and `ANONYMOUS`. The PLAINTEXT controller listener is bound to localhost only.
- **Admin tooling:** use `kafka/admin.properties` inside the kafka container.

**Schema Registry config.**
- **Effective config:** the container entrypoint copies `schema-registry/sr.properties`, appends `SR_EXTRA_PROPS`, and writes the result to `/tmp/sr.properties`. That file is the effective config, so `sr-acl-cli` must point at it. Later duplicate keys win.
- **Mounts:** `schema-registry/` is mounted read-only at `/etc/schema-registry/poc`.
- **Image:** `schema-registry/Dockerfile` only adds the Postgres JDBC driver. The Security Plugin is already in the base image.

**Authentication chain.**
- **Realm:** `authentication.realm=SchemaRegistryRealm` must match the entry name in `schema-registry/jaas.conf`. The JAAS file is passed via `SCHEMA_REGISTRY_OPTS`.
- **What Jetty reads:** the views `sr_auth_users` / `sr_auth_roles` in `postgres/init.sql`, never the base tables. The SR username is built in the view as `cluster_id || '.' || scram_user`. Jetty connects as the read-only `sr_auth` role.
- **Credential formats:** stored credentials must be plaintext, `MD5:<hex>` or `CRYPT:` (Jetty 9.4 `Credential`).
- **Load measurement:** test 8 counts queries via `pg_stat_statements` filtered to `userid = sr_auth`, and counts connections from Postgres `log_connections` lines.

**Identity mapping.** The Kafka SCRAM user is `svc-a`; the SR principal is `c17.svc-a`; both use the same password. The Java client (`client/src/main/java/poc/Client.java`) derives the SR login from the cluster and SCRAM user arguments. The svc-b consumer group must match the Kafka ACL prefix `c17.svc-x*`.

**Benchmark twin.** `schema-registry-noplugin` (profile `bench`, port 8082) uses the same JAAS/DB auth without the plugin. It joins the same SR group as a non-leader (`leader.eligibility=false`) and reads the same `_schemas`.

**`scripts/test.sh` mechanics.**
- **Recording:** `rec`/`check` append rows to `results/results.tsv`, and `results.md` is generated from that at the end.
- **Cleanup:** each test removes the ACLs and exporters it creates, so the script is re-runnable.
- **Restarts:** test 5 restarts SR twice: `schema.linking.rbac.enable=false`, then the shipped config (`true`).
- **README tables:** README §8 holds hand-written per-test tables copied from a run. If results change, update README §1, §7 and §8 too.

## Gotchas and deliberate choices

- **`lib.sh` is bash-only** (it uses `BASH_SOURCE`). Don't `source` it from zsh. macOS `date` lacks `%N`, so `now_ms` uses perl.
- **Postgres has no host port** because 5432 is taken on this machine. Use `docker compose exec postgres psql -U platform -d platform`.
- **Exporters need `kafkastore.update.handlers=io.confluent.schema.exporter.storage.SchemaExporterUpdateHandler`.** Without it SR crashes at startup with an NPE in `SchemaExporterResourceExtension.register`.
- **`schema.linking.rbac.enable=true` in `sr.properties` is intentional.** With the default `false`, `/exporters` is completely unauthorized in ACL mode (finding F1). Keep it `true`.
- **Test 5 is permission checks only** (agreed scope). Its exporters point back at this SR without credentials and are deleted immediately. Don't add a destination registry or any data-copying demo.
- **Subject listing filters on a different operation.** The `GET /subjects` filter uses `SUBJECT_COMPATIBILITY_READ`, not `SUBJECT_READ`. `sr-acl-cli --topic T` only covers `T-key`/`T-value`. Subject ACLs treat `*` anywhere as a glob.
- **The license is the single-node developer license** (log line `License for single cluster, single node`), not the 30-day trial.
- **Kafka ACLs for the SR principal in `up.sh` are deliberately broad prefix grants,** not a verified minimum.
