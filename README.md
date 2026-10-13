# Schema Registry security POC: ACL mode with database-backed login

This folder is a local proof of concept (POC) of the Schema Registry security design planned for the "Kafka topic as a service" platform. It runs on a laptop with Docker Compose. One command starts everything, one seeds it, one runs every test, and one deletes it.

The POC answers one question: **does the planned design actually work?** The design has three parts:

- **Login:** tenants log in to Schema Registry over plain HTTP with HTTP Basic auth. The username is `<cluster>.<scram-user>` (for example `c17.svc-payments`) and the password is the Service's Kafka SCRAM password. Schema Registry checks the password against the platform's **SQL Server** database through a small custom login module (`auth/`) that keeps the whole user table in memory ([F7](#f7)). Jetty's stock JDBC login module was measured as the alternative ([F4](#f4)).
- **Permissions:** the Confluent Schema Registry Security Plugin enforces them in **ACL mode** (`SchemaRegistryAclAuthorizer`). ACLs are managed with `sr-acl-cli`. There is no RBAC, MDS or LDAP.
- **Who does what:** only the platform (`platform-admin`) registers schemas. Apps are read-only (`auto.register.schemas=false`). A Service with read or write on topic T gets `SUBJECT_READ` on T's subjects.

Everything below was run on **2026-10-13**, starting from a clean environment (down → up → seed → test). The results were **68 PASS, 10 FAIL, 10 INFO**. All 10 FAILs are real problems with the design, not broken tests.

---

## Contents

1. [Summary: answers to the 9 questions](#1-summary-answers-to-the-9-questions)
2. [What must change in the design](#2-what-must-change-in-the-design)
3. [Background: how a request is checked](#3-background-how-a-request-is-checked)
4. [What was built](#4-what-was-built)
5. [How to run it and explore by hand](#5-how-to-run-it-and-explore-by-hand)
6. [Licensing](#6-licensing)
7. [Findings in detail](#7-findings-in-detail)
8. [Test results, test by test](#8-test-results-test-by-test)
9. [Managing ACLs without the CLI (test 9)](#9-managing-acls-without-the-cli-test-9)
10. [Configuration: what was verified and what was guessed](#10-configuration-what-was-verified-and-what-was-guessed)
11. [Limitations of this POC](#11-limitations-of-this-poc)
12. [Disaster recovery with Cluster Linking instead of exporters (design, not tested)](#12-disaster-recovery-with-cluster-linking-instead-of-exporters-design-not-tested)
13. [Open questions for Confluent support](#13-open-questions-for-confluent-support)

---

## 1. Summary: answers to the 9 questions

| # | Question | Answer | Works as designed? |
|---|---|---|---|
| 1 | Does database login work? | Yes, from an in-memory copy of the SQL Server user table. A missing password, wrong password or unknown user gets **401**. A correct password gets in. Passwords stored as plaintext or as Jetty's `MD5:` format both work. | ✅ Yes |
| 2 | Are permissions enforced? | Yes. svc-a can read `orders-value`; svc-b gets **403**. svc-a can't register schemas, delete subjects or change compatibility (403). platform-admin can do all of these. | ✅ Yes |
| 3 | `GET /schemas/ids/{id}` | Matches the docs. It's allowed only if the caller can read at least one subject that uses the ID. svc-a: 200 for its own ID, 403 for another subject's ID. svc-b: 403. | ✅ Yes |
| 4 | What does `GET /subjects` return to svc-b? | **403**, not an empty or filtered list. Listing needs the global `GLOBAL_READ` permission. With `GLOBAL_READ` the list is filtered, but on `SUBJECT_COMPATIBILITY_READ` rather than `SUBJECT_READ`. | ⚠️ Different from what you'd expect ([F3](#f3)) |
| 5 | Can tenants manage exporters? | **With the default config: yes, all of them. That's a security hole.** Any logged-in tenant, even one with zero ACLs, can list, read, create, pause and delete exporters, including the platform's own. Setting `schema.linking.rbac.enable=true` fixes it: tenants get 403 and platform-admin keeps access. **Decision: production doesn't enable exporters at all; DR uses Cluster Linking** ([§12](#12-disaster-recovery-with-cluster-linking-instead-of-exporters-design-not-tested)). | ❌ Not by default; avoided by not using exporters ([F1](#f1)) |
| 6 | Do real Java clients work? | (a) TopicNameStrategy: **yes**. (b) RecordNameStrategy set by config only: **no**, the producer gets 403. The topic ACL doesn't cover the record-name subject. Adding a pattern ACL on the record namespace fixes it. A consumer running as svc-b fails with 403, as intended. | ⚠️ (a) yes, (b) not with topic-based ACLs ([F2](#f2)) |
| 7 | How fast do ACL changes take effect? | About **1.3 s** after `sr-acl-cli` starts. That's before the CLI exits; the CLI itself takes about 1.8 s, mostly JVM startup. Adds and removes are equally fast. | ✅ Yes ([F5](#f5)) |
| 8 | Database load | **With the in-memory login module, requests cause no database work.** The module polls SQL Server once every 5 s per node. At 10 parallel clients: p50 2.1 ms, 598 req/s. Jetty's stock `JDBCLoginModule` opens **2 new connections and runs 2 queries per request**: 1,000 requests caused 2,000 connections, with p50 36.6 ms and 192 req/s. The plugin adds no measurable latency in either case. | ✅ Yes, with the in-memory module ([F4](#f4), [F7](#f7)) |
| 9 | Can ACLs be managed from Java without the CLI? | Possible but unsupported. The CLI runs `io.confluent.kafka.schemaregistry.security.tools.SchemaRegistryAclCommand`. You can call its `main()`, but on any error it shuts down the calling program unless you override `Exit`. Alternatively, write the plain-JSON ACL records to the `_schemas_acl` Kafka topic yourself (undocumented format). | ⚠️ Feasible, not supported ([§9](#9-managing-acls-without-the-cli-test-9)) |

Two more findings that weren't among the questions:
- **Login keeps working through SQL Server outages** ([F7](#f7), test 10). New users, password changes and deletions take effect within 5 s. While SQL Server is down, logins keep working from the last snapshot. A registry that starts while SQL Server is down refuses to start.
- **License** ([§6](#6-licensing)). The POC runs under Confluent's free single-broker developer license. Production needs your Enterprise key.

---

## 2. What must change in the design

The recommendations below are ordered by importance.

1. **Don't enable exporters (Schema Linking) in production. Use Cluster Linking for disaster recovery instead.** With exporters enabled and the default `schema.linking.rbac.enable=false`, any tenant can copy all schemas out of the registry and delete your exporters ([F1](#f1)). Without the exporter extension there is no `/exporters` endpoint to attack. Mirroring `_schemas` and `_schemas_acl` to the DR cluster also copies the ACLs, which exporters never do ([§12](#12-disaster-recovery-with-cluster-linking-instead-of-exporters-design-not-tested)). If exporters are ever enabled, set `schema.linking.rbac.enable=true` and ask Confluent to confirm it's supported in ACL mode (Q1).
2. **Pick TopicNameStrategy as the platform standard.** The rule "topic permission ⇒ `SUBJECT_READ` on T's subjects" only works with it. If teams need RecordNameStrategy, treat record-name subjects as separately granted resources, for example with a namespace pattern ACL per Service ([F2](#f2)).
3. **Use the in-memory login module (`auth/`), not Jetty's `JDBCLoginModule`** ([F7](#f7)). The stock module opens 2 database connections per request with no cache, and every client gets 401 while the database is down ([F4](#f4)). The in-memory module does no database work per request and keeps working through an outage. In return, changes take up to 5 s to apply, and a revoked user keeps access for as long as SQL Server stays down. Decide whether that's acceptable, and alert on snapshot age.
4. **Grant `SUBJECT_READ` only, unless an app needs to list subjects.** Read-only apps work with it. Tools and UIs that list subjects also need `GLOBAL_READ` plus `SUBJECT_COMPATIBILITY_READ` ([F3](#f3)).
5. **Run all ACL changes through one writer in the platform.** Each ACL record holds the complete permission set, so two writers changing the same entry at once can lose an update ([§9](#9-managing-acls-without-the-cli-test-9)).
6. **Put your Enterprise license key in `confluent.license`** and get the license topic and Kafka ACLs confirmed by Confluent (Q6).

---

## 3. Background: how a request is checked

### The path of one request

```
client (serializer / curl)
   │  HTTP Basic: "c17.svc-a:<password>"
   ▼
Schema Registry (Jetty web server)
   │ 1. AUTHENTICATION: Jetty JAAS realm "SchemaRegistryRealm"
   │    → SnapshotLoginModule looks the user up in its in-memory copy
   │      of the SQL Server view sr_auth_users (no database call)
   │    → Jetty compares the password (plaintext / MD5: / CRYPT:)
   │    wrong/unknown → 401
   │    (a background thread keeps the copy current: every 5 s it asks SQL Server
   │     "did anything change?" and reloads the whole view if so)
   │
   │ 2. AUTHORIZATION: Security Plugin (SchemaRegistryAclAuthorizer)
   │    → maps the endpoint to an operation (e.g. GET /subjects/X/versions → SUBJECT_READ on X)
   │    → looks up the principal "c17.svc-a" in its in-memory ACL table
   │    not allowed → 403
   ▼
request is served (200)
```

The in-memory ACL table is filled from the Kafka topic `_schemas_acl`. `sr-acl-cli` writes records to that topic, and every Schema Registry node reads it continuously. That's why ACL changes appear within about a second.

### Vocabulary

| Term | Meaning |
|---|---|
| **Subject** | A named, versioned slot that holds schemas. Clients look schemas up by subject. |
| **Subject name strategy** | How a client derives the subject name from a topic and a record. **TopicNameStrategy** (the default) gives `<topic>-key` / `<topic>-value`, e.g. `orders-value`. **RecordNameStrategy** gives the record's full name, e.g. `com.acme.payments.Payment`, which is the same on every topic. **TopicRecordNameStrategy** gives `<topic>-<record full name>`. |
| **SUBJECT_\* operations** | Per-subject permissions: `SUBJECT_READ`, `SUBJECT_WRITE` (register), `SUBJECT_DELETE`, `SUBJECT_COMPATIBILITY_READ`, `SUBJECT_COMPATIBILITY_WRITE`. |
| **GLOBAL_\* operations** | Permissions not tied to a subject: `GLOBAL_READ` (list subjects and schemas, schema types), `GLOBAL_COMPATIBILITY_READ` / `_WRITE` (the global compatibility setting). |
| **`sr-acl-cli`** | Confluent's command-line tool for ACLs. `--topic T` grants on `T-key` and `T-value`; `--subject S` grants on one subject. A `*` in a subject works as a wildcard (`'*'` means all subjects; `'com.acme.*'` is a pattern). |
| **Exporter / Schema Linking** | A job running inside Schema Registry that continuously copies schemas to another Schema Registry at a URL given in its config. It is managed through `/exporters` endpoints. **Not used in the production design** ([§12](#12-disaster-recovery-with-cluster-linking-instead-of-exporters-design-not-tested)); enabled in the POC only for test 5. |
| **Cluster Linking** | A Confluent Server feature that mirrors Kafka topics byte for byte (same offsets) to another Kafka cluster. The production design uses it to copy `_schemas` and `_schemas_acl` to the DR cluster ([§12](#12-disaster-recovery-with-cluster-linking-instead-of-exporters-design-not-tested)). |
| **`schema.linking.rbac.enable`** | A Security Plugin setting. When `true`, the `/exporters` endpoints are permission-checked. Default `false`. Has no effect when exporters aren't enabled. |
| **401 vs 403** | 401 = authentication failed (who are you?). 403 = authenticated, but not allowed. |

---

## 4. What was built

```
                          ┌───────────────────────────── docker network ─────────────────────────────┐
 host :8081 ──────────────► schema-registry (cp-schema-registry 7.9.0 + SQL Server JDBC driver)       │
                          │    • HTTP Basic, JAAS realm → poc.auth.SnapshotLoginModule (auth/)         │
                          │      users in memory ◄── poll every 5 s ── mssql (SQL Server 2022,         │
                          │    • Security Plugin, ACL authorizer          platform DB, sr_auth_* views) │
                          │    • exporters (Schema Linking) enabled, for test 5 only                   │
                          │    • storage + ACLs in Kafka: _schemas, _schemas_acl                       │
                          │                         │ SASL_PLAINTEXT / SCRAM-SHA-256                   │
 host :9092 ──────────────► kafka (cp-server 7.9.0, single KRaft node, ACLs enabled)                   │
                          │                                                                          │
 host :8082 ──────────────► schema-registry-noplugin  (test 8 only: same login, plugin OFF)           │
 host :8083 ──────────────► schema-registry-jdbc      (test 8 only: plugin ON, Jetty JDBCLoginModule)  │
                          │ client (Java Avro producer/consumer, run on demand by the tests)           │
                          └──────────────────────────────────────────────────────────────────────────┘
```

### Users and permissions after seeding

| Kafka SCRAM user | Schema Registry login | Password | Kafka permissions | Schema Registry ACLs |
|---|---|---|---|---|
| `schema-registry` | – (Schema Registry's own Kafka user) | `schema-registry-secret` | its internal topics (`_schemas*`, `_exporter*`, license topics), its consumer groups, cluster Describe | – |
| `platform-admin` | `platform-admin` | `platform-admin-secret` | none needed | everything: `-s '*' -o '*'` |
| `svc-a` | `c17.svc-a` (password stored in plain text) | `svc-a-secret` | read/write `orders` and `payments`; consumer groups `c17.svc-a*` | `SUBJECT_READ` via `--topic orders` (= `orders-key`, `orders-value`) |
| `svc-b` | `c17.svc-b` (password stored as Jetty `MD5:`) | `svc-b-secret` | read `orders`; consumer groups `c17.svc-b*` | none |

svc-b is deliberately allowed to read the Kafka topic `orders` but has no Schema Registry ACL. That way the svc-b consumer test fails at Schema Registry, not at Kafka. Under the real platform policy, a Kafka read permission would also earn `SUBJECT_READ`.

Schemas registered by platform-admin during seeding:
- `orders-value` (record `com.acme.orders.Order`, schema ID 1), the TopicNameStrategy subject for topic `orders`.
- `com.acme.payments.Payment` (schema ID 2), the RecordNameStrategy subject used on topic `payments`.

### The platform database (SQL Server)

`mssql/init.sql` models the platform with three tables: `clusters`, `services` (cluster + SCRAM user + password) and `platform_principals` (platform-admin). Schema Registry never reads those tables directly. It reads views:

- `sr_auth_users(username, password)`: `cluster_id + '.' + scram_user` for every Service, plus the platform principals. The in-memory module reads the whole view; Jetty's `JDBCLoginModule` reads one row per login.
- `sr_auth_roles(username, role)`: every user gets the role `sr-user`. Only `JDBCLoginModule` reads it; the in-memory module assigns `sr-user` itself. Schema Registry requires a role; actual permissions come from ACLs.

On top of that:
- **Change Tracking** is on for the database and for the base tables `services` and `platform_principals`. `SELECT CHANGE_TRACKING_CURRENT_VERSION()` returns a number that goes up on every insert, update or delete, so the module can detect changes without reading rows.
- **Two read-only logins:** `sr_auth` (the in-memory module, `SELECT` on `sr_auth_users` only; reading the change-tracking version needs no extra permission) and `sr_jdbc` (the `JDBCLoginModule` baseline). Separate logins keep their database work apart in the measurements.
- **Two Extended Events sessions** (`sr_auth_activity`, `sr_jdbc_activity`) count each login's connections and queries for test 8.

### Files

| Path | What it is |
|---|---|
| `docker-compose.yml` | All services. `schema-registry-noplugin` and `schema-registry-jdbc` only start with profile `bench`, `client` only with profile `client`. |
| `schema-registry/Dockerfile` | `cp-schema-registry:7.9.0` plus the SQL Server JDBC driver 12.8.1. **This is the only addition to the image**; the Security Plugin is already in it. The login module jar is mounted from `auth/target`. |
| `schema-registry/sr.properties` | Schema Registry config: login, plugin, ACL authorizer, Kafka connection, exporters (POC only, for test 5; production drops them, see §12). Every line is commented. |
| `schema-registry/sr-noplugin.properties` | The same, without the plugin. Used only for the test 8 comparison. |
| `schema-registry/jaas.conf` | The login realm: `poc.auth.SnapshotLoginModule` pointing at SQL Server, with poll intervals. |
| `schema-registry/jaas-jdbc.conf` | The test 8 baseline realm: Jetty `JDBCLoginModule` pointing at the same SQL Server. |
| `auth/` | The in-memory login module (Maven, no runtime dependencies): `SnapshotLoginModule`, `UserSnapshot` (the in-memory table and its refresh thread), `Preflight` (the startup check). Built by `up.sh` into `auth/target/`, which is mounted into the containers. |
| `mssql/init.sql` | Tables, views, Change Tracking, the `sr_auth` and `sr_jdbc` logins, the Extended Events counters. Run once by `up.sh`. |
| `kafka/` | The broker's SCRAM login file and an admin client config. |
| `client/` | Java producer/consumer (Maven): `auto.register.schemas=false`, choice of TopicNameStrategy or RecordNameStrategy. |
| `scripts/up.sh` | Builds the image and the login module, starts Kafka and SQL Server, creates the database, SCRAM users, Kafka ACLs and topics, then starts Schema Registry. |
| `scripts/seed.sh` | Inserts users into SQL Server, waits until Schema Registry has loaded them, creates Schema Registry ACLs with `sr-acl-cli`, registers the schemas as platform-admin. |
| `scripts/test.sh` | Runs tests 1–10 and writes `results/`. |
| `scripts/down.sh` | Stops everything and deletes all data. |
| `results/results.md` | The latest results table (regenerated on every test run). |
| `results/details.log` | Full client output from the latest run. |
| `results/burst-*.txt` | Raw status code and latency of every request in the test 8 bursts. |
| `results/schemas_acl-dump.txt` | The raw contents of the `_schemas_acl` topic. |

---

## 5. How to run it and explore by hand

**Requirements:** Docker Desktop with about 6 GB free memory, plus `curl`, `python3` and `perl` (all on macOS by default). Ports 8081, 8082, 8083 and 9092 must be free. SQL Server has no host port. SQL Server only exists for x86, so on Apple Silicon it runs under Rosetta emulation (it starts in a few seconds and uses about 1.1 GB).

```bash
./scripts/up.sh      # ~2-3 min the first time (image build, SQL Server download); ends with "Schema Registry is up"
./scripts/seed.sh    # a few seconds
./scripts/test.sh    # ~3 min; prints PASS/FAIL/INFO per check, writes results/results.md
./scripts/down.sh    # removes containers and all data
```

`test.sh` can be run repeatedly. It cleans up the ACLs and exporters it creates. It restarts Schema Registry twice during test 5 (with the exporter setting off, then on) and once in test 10, and stops SQL Server twice (tests 8 and 10), so don't be surprised by short pauses.

**Reading the test output:**
- **PASS:** behaved as the design needs.
- **FAIL:** behaved differently from what the design needs. Each FAIL is a finding.
- **INFO:** a measurement or observation with no right or wrong answer.

### Trying things by hand

```bash
# Login and permissions
curl -i http://localhost:8081/subjects                                       # 401: no credentials
curl -u c17.svc-a:svc-a-secret http://localhost:8081/subjects/orders-value/versions/latest   # 200
curl -u c17.svc-b:svc-b-secret http://localhost:8081/subjects/orders-value/versions/latest   # 403
curl -u platform-admin:platform-admin-secret http://localhost:8081/subjects                  # full list

# ACLs (sr-acl-cli runs inside the Schema Registry container)
docker compose exec schema-registry sr-acl-cli --config /tmp/sr.properties --list
docker compose exec schema-registry sr-acl-cli --config /tmp/sr.properties --add    -t orders -p c17.svc-b -o SUBJECT_READ
docker compose exec schema-registry sr-acl-cli --config /tmp/sr.properties --remove -t orders -p c17.svc-b -o SUBJECT_READ

# Add a tenant: the SQL Server row is all that's needed to log in (403 = logged in, no ACLs yet)
docker compose exec mssql /opt/mssql-tools18/bin/sqlcmd -C -S localhost -U sa -P Platform-secret1 -d platform \
  -Q "INSERT INTO services VALUES ('c17','svc-c','svc-c-secret')"
curl -u c17.svc-c:svc-c-secret http://localhost:8081/subjects/orders-value/versions/latest   # 401, then 403 within 5 s
# Deleting the row locks the user out within 5 s (401)

# Watch the login module reload its snapshot
docker compose logs -f schema-registry | grep poc.auth

# Run the Java client yourself (build happens inside test.sh, or: docker compose run --rm --entrypoint mvn client -q package)
docker compose --profile client run --rm client produce orders c17 svc-a svc-a-secret topic
docker compose --profile client run --rm client consume orders c17 svc-a svc-a-secret topic
docker compose --profile client run --rm client consume orders c17 svc-b svc-b-secret topic   # fails: 403
```

Client arguments: `produce|consume <topic> <cluster> <scram-user> <password> <topic|record>`. The Kafka login is `<scram-user>`, the Schema Registry login is `<cluster>.<scram-user>`, and both use the same password. `record` makes the client use RecordNameStrategy, set purely by configuration.

---

## 6. Licensing

- **Plugin:** the Security Plugin is **already included** in `confluentinc/cp-schema-registry:7.9.0` (in `/usr/share/java/confluent-security/schema-registry/`, which is on the classpath), and so is `sr-acl-cli`. Nothing had to be installed except the SQL Server JDBC driver (and the POC's own login module).
- **What the POC runs on:** no license key is configured. At startup Schema Registry logs `License for single cluster, single node`, meaning it runs under Confluent's **free developer license for single-broker clusters**, not the 30-day trial. It has no expiry while the cluster has one broker.
- **Production:** your clusters have multiple brokers, so they need the **Enterprise license key** in `confluent.license`, plus Kafka ACLs letting the Schema Registry user use the license topic.
- **Not confirmed:** the docs name the license topic `_confluent-command`, but the plugin's built-in default is `_confluent-license`. This POC sets `_confluent-license` and grants Kafka ACLs on both. The developer license never used either topic, so which one matters couldn't be confirmed (question 6 in §13).

---

## 7. Findings in detail

<a id="f1"></a>
### F1. Exporters are unprotected by default (security issue)

**Decision.** The production design **doesn't enable exporters**. Disaster recovery uses Cluster Linking instead ([§12](#12-disaster-recovery-with-cluster-linking-instead-of-exporters-design-not-tested)). This finding is the main reason: with no exporter extension loaded, there is no `/exporters` endpoint and nothing below applies. The POC still enables exporters only so that test 5 can demonstrate the problem.

**What was found.** The docs say "Schema Registry ACL Authorizer does not support schema exporters". That reads like "exporters are denied". In fact, with the default `schema.linking.rbac.enable=false`, the plugin **doesn't check the `/exporters` endpoints at all**. In tests 5a1–5a9 both tenants did all of the following:
- svc-a, which only has `SUBJECT_READ` on `orders`, and svc-b, which has no ACLs, could list all exporters.
- Both could read the config of the platform's exporter.
- Both could create exporters of their own.
- Both could pause the platform's exporter, and svc-b deleted it.

**Why it matters.** An exporter runs inside Schema Registry. It reads schemas straight from storage, without permission checks, and sends them to whatever Schema Registry URL its creator puts in its config. So any tenant can copy every schema in the registry to a place of their choosing. They can also stop or delete the platform's own Schema Linking.

**If exporters are ever enabled.** With `schema.linking.rbac.enable=true`, every `/exporters` call needs the `EXPORTER_ACCESS` operation. The ACL authorizer grants it only to users who hold **all five** SUBJECT operations on subject `*`. Here that's platform-admin. In tests 5b0–5bx both tenants got 403 on every exporter call and platform-admin could still create, pause and delete. **`schema-registry/sr.properties` ships with this set to `true`.**

**Caveat.** This setting is documented for RBAC, not for ACL mode, so ask Confluent to confirm it's supported (question 1). This is no longer blocking, since production doesn't use exporters.

<a id="f2"></a>
### F2. "Topic permission ⇒ subject permission" only works with TopicNameStrategy

**What was found.** `sr-acl-cli --topic payments` creates ACLs for exactly two subjects: `payments-key` and `payments-value`. With RecordNameStrategy, the client looks up the subject `com.acme.payments.Payment` instead. No topic ACL covers that, so the producer failed with 403 (test 6d). A pattern ACL on the record namespace fixed it: `sr-acl-cli --add -s 'com.acme.payments.*' -p c17.svc-a -o SUBJECT_READ` (tests 6e and 6f).

**How patterns behave.** Read from the plugin's code: any `*` in an ACL subject is a wildcard, and every other character, including `.`, is literal.

**Why it's a design problem, not just a config tweak:**
- Record names aren't tied to topics, so the platform can't derive "T's subjects" from the topic alone. It would need to know which record namespaces each topic carries.
- Two tenants using the same record name on different topics share the **same** subject, and with it the same schema history.
- TopicRecordNameStrategy (`<topic>-<record>`) isn't covered by `--topic` either. A pattern `-s 'orders-*'` would also match a different topic named `orders-archive`.

**Recommendation.** Make TopicNameStrategy the platform standard. If RecordNameStrategy is allowed, grant record-name subjects explicitly as their own resources.

<a id="f3"></a>
### F3. Subject listing behaves differently from what you'd expect

- **`GET /subjects`** returns **403** for both tenants (tests 4a and 4b), not an empty or filtered list. Listing is a global operation that needs `GLOBAL_READ`.
- **With `GLOBAL_READ` added,** the list is filtered per subject, but the filter checks **`SUBJECT_COMPATIBILITY_READ`**, not `SUBJECT_READ`. So svc-a, which can read `orders-value`, got `[]` (test 4d, the one FAIL outside test 5). After `SUBJECT_COMPATIBILITY_READ` on `orders-value` was added, it got `["orders-value"]` (test 4d3).
- **The same applies to** `/schemas/ids/{id}/subjects` and `/schemas/ids/{id}/versions` (tests 3d and 4d4).
- **`GET /schemas`** (list all schemas) with `GLOBAL_READ` *is* filtered on `SUBJECT_READ` (test 4d2).
- **`/schemas/types`** also needs `GLOBAL_READ` (tests 4g and 4h).

**What it means.** Normal Avro serializers and deserializers never call these endpoints, so read-only apps work with `SUBJECT_READ` alone (test 6). Tools, UIs and scripts that list subjects need `GLOBAL_READ` plus `SUBJECT_COMPATIBILITY_READ`.

<a id="f4"></a>
### F4. Jetty's JDBC login module hits the database on every request

Jetty ships a `JDBCLoginModule` that checks every login against a database table. Test 8 runs it against the same SQL Server on a benchmark-only registry (`schema-registry-jdbc`, port 8083).

**Measurements (test 8):**

| Request type | SQL queries | New SQL Server connections |
|---|---|---|
| correct password | 2 (user + role) | **2** |
| wrong password / unknown user | 1 | 1 |
| no credentials | 0 | 0 |
| 20 requests on one reused HTTP connection | 40 | 40 (no reuse at all) |

**Why.** `JDBCLoginModule` opens a brand-new database connection (`DriverManager.getConnection`) for each of its two queries and keeps nothing between requests.

**What it means:**
- **Every request per second costs two new database connections per second.** At 500 req/s that's 1,000 new connections per second against the platform database.
- **Failed logins also hit the database,** so anyone who can reach Schema Registry can generate database load with bad passwords.
- **It's slow.** Each request waits for two connection setups. With 10 parallel clients it reached p50 36.6 ms and 192 req/s, against p50 2.1 ms and 598 req/s for the in-memory module (F7).
- **When SQL Server is down,** every client gets **401 Unauthorized**, not a 5xx error (test 8m). To an app it looks like a wrong password. Logins work again as soon as the database is back (test 8n).
- **The upside of no cache:** password changes and removals take effect on the very next request.

**Alternatives considered:**
- **A login module that keeps the whole user table in memory. Built, tested and used by the POC: [F7](#f7).**
- A connection pooler or read replica dedicated to Schema Registry authentication. This removes the connection churn, but not the queries or the dependency on the database being up.
- Jetty's `DataSourceLoginModule` with a pooled data source. It exists in this Jetty version but is untested with Schema Registry.

<a id="f5"></a>
### F5. ACL changes take effect within about a second

**Measured (test 7, three adds and three removes):**
- An `sr-acl-cli --add` or `--remove` became effective **1.27–1.35 s** after the command started.
- The command itself took about **1.8 s**, so the change was live about half a second **before** the CLI exited.
- Most of the CLI time is Java startup and connecting to Kafka.

**How it works.** The CLI writes a record to the `_schemas_acl` topic. Every Schema Registry node reads that topic continuously and updates its in-memory permission table. With several Schema Registry nodes, each one picks up the change independently in the same way.

<a id="f6"></a>
### F6. Smaller observations

- **Unknown vs forbidden schema IDs.** An unknown schema ID returns 404, while an existing ID the caller can't read returns 403. A tenant can therefore probe which IDs exist, but not their content (test 3f).
- **KEK/DEK operations.** In the plugin's ACL authorizer, the method that checks encryption-key (KEK/DEK) operations always says "allowed" (`authorizeKekOperation`). This only matters if client-side field-level encryption (the DEK registry) is ever enabled. It's off here and wasn't tested (question 7).
- **Exporters need an undocumented-in-practice setting** (only relevant if exporters are enabled; production doesn't enable them). Enabling them requires `kafkastore.update.handlers=io.confluent.schema.exporter.storage.SchemaExporterUpdateHandler`. Without it, Schema Registry crashes at startup with a `NullPointerException`. This was hit while building the POC.

<a id="f7"></a>
### F7. In-memory login from SQL Server keeps the database out of the request path

The POC's login, built in `auth/`. Its realm is `schema-registry/jaas.conf`, used by `schema-registry` and its plugin-OFF twin.

**How it works:**
- **One table in memory.** The module loads the whole `sr_auth_users` view (username, stored credential) into a map. A login is a map lookup plus Jetty's normal password check (plaintext, `MD5:`, `CRYPT:`), with no database work. For 10,000 users the map takes a few MB.
- **Changes are found by polling.** A background thread runs `SELECT CHANGE_TRACKING_CURRENT_VERSION()` every 5 s. This returns one number and reads no rows. When the number changes, the thread reloads the whole table. Every 5 minutes it reloads regardless. SQL Server has no `LISTEN/NOTIFY`, and Query Notifications only work from .NET, so polling a cheap version number is the practical choice.
- **Swapped, never edited.** Each reload builds a new map and replaces the old one in a single step, so a request never sees a half-loaded table. A reload that returns 0 users while the current snapshot has users is rejected, since it's far more likely a broken view than "everyone was deleted".
- **Database down: keep the last snapshot.** Failed polls are logged with the snapshot's age, and logins carry on.
- **Fails at startup instead of at the first request.** Jetty only creates a login module on the first login, so the module can't check the database at startup itself. The container first runs `poc.auth.Preflight`, which reads the same JAAS entry and does one full load. If that fails, the container exits and Schema Registry never starts. No Schema Registry extension is involved.

**Measured (tests 8 and 10), next to Jetty's `JDBCLoginModule` on the same SQL Server:**

| | `JDBCLoginModule` | In-memory module |
|---|---|---|
| DB work per request (valid, wrong password or unknown user) | 1–2 queries, 1–2 new connections | **none** (300 requests: 1 query, 1 connection, both the background poll) |
| DB work in the background | none | 1 connection + 1 version query per 5 s per node, plus a full load per change or every 5 min |
| 1,000 requests, 1 at a time | p50 20.9 ms, p99 32.2 ms | **p50 1.8 ms, p99 4.3 ms** |
| 1,000 requests, 10 parallel | p50 36.6 ms, p99 73.8 ms, 192 req/s | **p50 2.1 ms, p99 6.7 ms, 598 req/s** |
| New user can log in | next request | after **2.0 s** |
| Changed password works / old one rejected | next request | after **4.7 s** |
| Deleted user locked out | next request | after **4.7 s** |
| Valid login while the database is down | **401** | **200** (wrong password still 401) |
| Starting a registry while the database is down | starts; every login gets 401 | **refuses to start** (exit code 1) |

**The plugin's own cost is now measurable, and it's zero.** With the database out of the request path, plugin ON and OFF both handled 1,000 requests at 10 parallel in 598 req/s (p50 2.1 vs 2.6 ms; tests 8j, 8k).

**What to decide before production:**
- **Up to 5 s delay for changes** (new Services, password rotations, removals). With Kafka the new SCRAM password works immediately, so for up to 5 s Schema Registry rejects a password that Kafka accepts. Provisioning can wait for it, or the poll interval can be shortened, since it's cheap.
- **Revocation stops during an outage.** While SQL Server is down, a deleted user keeps working on nodes that already loaded it. Alert on snapshot age (the module logs it on every failed poll). If an unbounded window isn't acceptable, add a maximum age after which the module rejects all logins.
- **It's your code in the login path.** It uses Jetty 9.4's JAAS classes (`AbstractLoginModule`, `UserInfo`), which change between Jetty major versions. Check it against the Jetty in every Schema Registry upgrade.
- **Each node polls on its own.** Database load grows with the number of Schema Registry nodes, not with request rate.
- **Credential formats don't change.** The stored password must still be plaintext, `MD5:` or `CRYPT:`, because Jetty does the comparison.

---

## 8. Test results, test by test

Each row: what was checked, what the design needs (Expected), what happened (Actual), and the verdict.
- **Response codes:** 200/204 = allowed, 401 = not logged in, 403 = logged in but not allowed, 404 = not found.
- **Freshness:** `results/results.md` holds the same table and is regenerated on every run. The tables below are the snapshot from 2026-10-13.

### Test 1: Authentication

Each request goes to `GET /subjects/orders-value/versions/latest` with different credentials, checked by the in-memory login module. Test 1e uses svc-b, which has no ACLs: getting **403** rather than 401 proves its password (stored as `MD5:`) was accepted.

| # | Check | Expected | Actual | Result |
|---|---|---|---|---|
| 1a | No credentials | 401 | 401 | **PASS** |
| 1b | c17.svc-a, wrong password | 401 | 401 | **PASS** |
| 1c | Unknown user | 401 | 401 | **PASS** |
| 1d | c17.svc-a, correct password (plaintext in DB) | 200 | 200 | **PASS** |
| 1e | c17.svc-b, correct password (stored as `MD5:`) | 403 | 403 | **PASS** |

### Test 2: Authorization

svc-a only has `SUBJECT_READ` on `orders-*`. Test 2f is the call a serializer makes with `auto.register.schemas=false`: it asks "does this exact schema exist under this subject, and what's its ID?". Read permission is enough for that. platform-admin's writes use a scratch subject, so `orders-value` stays untouched.

| # | Check | Expected | Actual | Result |
|---|---|---|---|---|
| 2a | svc-a reads `orders-value` | 200 | 200 | **PASS** |
| 2b | svc-b reads `orders-value` | 403 | 403 ("User is denied operation Read on Subject: orders-value") | **PASS** |
| 2c | svc-a registers a new version of `orders-value` | 403 | 403 ("…denied operation Write…") | **PASS** |
| 2d | svc-a deletes subject `orders-value` | 403 | 403 ("…denied operation Delete…") | **PASS** |
| 2e | svc-a changes compatibility of `orders-value` | 403 | 403 | **PASS** |
| 2f | svc-a looks up its schema (`POST /subjects/orders-value`) | 200 | 200 | **PASS** |
| 2g | platform-admin registers a schema | 200 | 200 | **PASS** |
| 2h | platform-admin soft-deletes the subject | 200 | 200 | **PASS** |
| 2i | platform-admin permanently deletes the subject | 200 | 200 | **PASS** |

### Test 3: Schema by ID

Schema ID 1 belongs to `orders-value` (svc-a can read it). ID 2 belongs only to `com.acme.payments.Payment` (svc-a can't read it).

| # | Check | Expected | Actual | Result |
|---|---|---|---|---|
| 3a | svc-a `GET /schemas/ids/1` | 200 | 200 | **PASS** |
| 3b | svc-b `GET /schemas/ids/1` | 403 | 403 | **PASS** |
| 3c | svc-a `GET /schemas/ids/2` | 403 | 403 | **PASS** |
| 3d | svc-a `GET /schemas/ids/1/subjects` | 200 `[orders-value]` | 403 (needs `GLOBAL_READ`, see test 4) | **INFO** |
| 3e | svc-b `GET /schemas/ids/1/versions` | 403 | 403 | **PASS** |
| 3f | svc-b `GET /schemas/ids/99999` (doesn't exist) | 403 or 404 | 404 (reveals existence, see F6) | **INFO** |

### Test 4: Subject listing

First without global permissions. Then `GLOBAL_READ` is temporarily granted to both tenants, and in 4d3/4d4 also `SUBJECT_COMPATIBILITY_READ` on `orders-value` to svc-a. Everything is removed afterwards. See [F3](#f3).

| # | Check | Expected | Actual | Result |
|---|---|---|---|---|
| 4a | svc-b `GET /subjects` (no ACLs) | 403 | 403 | **PASS** |
| 4b | svc-a `GET /subjects` (only `SUBJECT_READ`) | 403 | 403: denied outright, not filtered | **PASS** |
| 4c | platform-admin `GET /subjects` | 200 | 200 `["com.acme.payments.Payment","orders-value"]` | **PASS** |
| 4d | svc-a `GET /subjects` with `GLOBAL_READ` | 200 `["orders-value"]` | **200 `[]`**: filter ignores `SUBJECT_READ` | **FAIL** |
| 4d2 | svc-a `GET /schemas` with `GLOBAL_READ` | only `orders-value` | only `orders-value` (filtered on `SUBJECT_READ`) | **PASS** |
| 4d3 | svc-a `GET /subjects` after adding `SUBJECT_COMPATIBILITY_READ` | 200 `["orders-value"]` | 200 `["orders-value"]` | **PASS** |
| 4d4 | svc-a `GET /schemas/ids/1/subjects` with both | 200 `["orders-value"]` | 200 `["orders-value"]` | **PASS** |
| 4e | svc-b `GET /subjects` with `GLOBAL_READ` only | 200 `[]` | 200 `[]` | **PASS** |
| 4f | svc-a `GET /config` (global compatibility) | 403 | 403 (needs `GLOBAL_COMPATIBILITY_READ`) | **PASS** |
| 4g | svc-a `GET /schemas/types` with `GLOBAL_READ` | 200 | 200 | **PASS** |
| 4h | svc-b `GET /schemas/types` without `GLOBAL_READ` | 403 | 403 | **PASS** |

### Test 5: Exporters (Schema Linking)

Production doesn't enable exporters ([§12](#12-disaster-recovery-with-cluster-linking-instead-of-exporters-design-not-tested)); this test documents why (F1). Run twice: first with the Confluent default `schema.linking.rbac.enable=false` (rows 5a), then with the shipped config `true` (rows 5b). Each time, platform-admin first creates a "platform" exporter, then each tenant tries to list exporters, create one, read the platform exporter's config, and pause it. svc-b also tries to delete it. Expected = what the design needs (tenants denied).

These are permission checks only. The test exporters point back at this same Schema Registry **without credentials**, so they can't copy anything, and they are paused and deleted right away.

| # | Check | Expected | Actual | Result |
|---|---|---|---|---|
| 5a0 | [false] platform-admin creates exporter | 200 | 200 | **PASS** |
| 5a1 | [false] svc-a lists exporters | 403 | **200** (sees the platform exporter) | **FAIL** |
| 5a2 | [false] svc-a creates an exporter | 403 | **200** | **FAIL** |
| 5a3 | [false] svc-a reads platform exporter config | 403 | **200** | **FAIL** |
| 5a4 | [false] svc-a pauses platform exporter | 403 | **200** | **FAIL** |
| 5a5 | [false] svc-b lists exporters | 403 | **200** | **FAIL** |
| 5a6 | [false] svc-b creates an exporter | 403 | **200** | **FAIL** |
| 5a7 | [false] svc-b reads platform exporter config | 403 | **200** | **FAIL** |
| 5a8 | [false] svc-b pauses platform exporter | 403 | **200** | **FAIL** |
| 5a9 | [false] svc-b deletes platform exporter | 403 | **204 (deleted)** | **FAIL** |
| 5ax | [false] platform-admin pauses + deletes | 204 | 204 | **PASS** |
| 5b0 | [true] platform-admin creates exporter | 200 | 200 | **PASS** |
| 5b1 | [true] svc-a lists exporters | 403 | 403 | **PASS** |
| 5b2 | [true] svc-a creates an exporter | 403 | 403 | **PASS** |
| 5b3 | [true] svc-a reads platform exporter config | 403 | 403 | **PASS** |
| 5b4 | [true] svc-a pauses platform exporter | 403 | 403 | **PASS** |
| 5b5 | [true] svc-b lists exporters | 403 | 403 | **PASS** |
| 5b6 | [true] svc-b creates an exporter | 403 | 403 | **PASS** |
| 5b7 | [true] svc-b reads platform exporter config | 403 | 403 | **PASS** |
| 5b8 | [true] svc-b pauses platform exporter | 403 | 403 | **PASS** |
| 5b9 | [true] svc-b deletes platform exporter | 403 | 403 | **PASS** |
| 5bx | [true] platform-admin pauses + deletes | 204 | 204 | **PASS** |

### Test 6: Real Java clients

A Java Avro producer/consumer (`client/`) with `auto.register.schemas=false`, logging in to Kafka and Schema Registry with the tenant's credentials. "Expected FAIL" rows pass when the client fails the way it should.

For 6d, svc-a gets `--topic payments` (the topic-based rule from the design), then produces with RecordNameStrategy. For 6e/6f a pattern ACL on `com.acme.payments.*` is added. Both are removed afterwards.

| # | Check | Expected | Actual | Result |
|---|---|---|---|---|
| 6a | svc-a produces to `orders` (TopicNameStrategy) | OK | OK | **PASS** |
| 6b | svc-a consumes from `orders` | OK | OK: deserialized `com.acme.orders.Order` | **PASS** |
| 6c | svc-b consumes from `orders` | FAIL | FAIL: `RecordDeserializationException`, Schema Registry 403 | **PASS** |
| 6d | svc-a produces to `payments` with RecordNameStrategy, only `--topic payments` ACL | FAIL | FAIL: "denied operation Read on Subject: com.acme.payments.Payment" | **PASS** (confirms F2) |
| 6e | Same, after adding `-s 'com.acme.payments.*'` | OK | OK | **PASS** |
| 6f | svc-a consumes from `payments` (RecordNameStrategy) | OK | OK: deserialized `com.acme.payments.Payment` | **PASS** |

### Test 7: ACL propagation

A background loop polls svc-b's access to `orders-value` every ~20 ms while `sr-acl-cli` grants and then revokes `SUBJECT_READ`. Each run shows "CLI total time / time until the change was live", both measured from the moment the CLI started.

| # | Check | Expected | Actual (CLI time / live after) | Result |
|---|---|---|---|---|
| 7a1 | `--add` #1 | live before CLI exits | 1884 ms / 1348 ms | **PASS** |
| 7r1 | `--remove` #1 | live before CLI exits | 1833 ms / 1286 ms | **PASS** |
| 7a2 | `--add` #2 | live before CLI exits | 1807 ms / 1273 ms | **PASS** |
| 7r2 | `--remove` #2 | live before CLI exits | 1824 ms / 1288 ms | **PASS** |
| 7a3 | `--add` #3 | live before CLI exits | 1865 ms / 1328 ms | **PASS** |
| 7r3 | `--remove` #3 | live before CLI exits | 1838 ms / 1302 ms | **PASS** |

### Test 8: Database load

Compares the two login modules on the same SQL Server. See [F4](#f4) and [F7](#f7).

**How it's measured:**
- **Queries and connections** are counted on the SQL Server side with the Extended Events sessions in `mssql/init.sql`. Each module has its own database login (`sr_jdbc`, `sr_auth`), so their counts don't mix.
- **8a–8e** send requests to `schema-registry-jdbc` (port 8083, Jetty `JDBCLoginModule`), 20 per row. **8f** sends 300 requests to `schema-registry` (port 8081, in-memory module).
- **Bursts (8g–8l)** are 1,000 `GET /subjects/orders-value/versions/latest` requests as svc-a, after a warm-up of 300 requests per registry. "Plugin OFF" is `schema-registry-noplugin` (port 8082, in-memory module). At concurrency 1, throughput is limited by the test client, which starts a new `curl` process per request. Compare latencies there.
- **8m/8n** stop SQL Server, make a request, then start it again.

| # | Check | Expected | Actual | Result |
|---|---|---|---|---|
| 8a | `JDBCLoginModule`, correct password, per request | – | 2 queries, **2 new DB connections** | **INFO** |
| 8b | `JDBCLoginModule`, wrong password, per request | – | 1 query, 1 connection | **INFO** |
| 8c | `JDBCLoginModule`, unknown user, per request | – | 1 query, 1 connection | **INFO** |
| 8d | `JDBCLoginModule`, no credentials, per request | – | nothing | **INFO** |
| 8e | `JDBCLoginModule`, 20 requests on one reused HTTP connection | – | 40 queries, **40 connections** (no caching) | **INFO** |
| 8f | In-memory module, 300 requests (100 valid, 100 wrong password, 100 unknown user) | no per-request DB work | 1 query, 1 connection (the 5 s poll), 0 full loads, in 2 s | **INFO** |
| 8g | Burst, `JDBCLoginModule`, 1 at a time | all 200 | 1000×200; p50 20.9 ms, p99 32.2 ms, max 82.9 ms, 32 req/s; 2,000 queries, 2,000 DB connections | **PASS** |
| 8h | Burst, in-memory, 1 at a time | all 200 | 1000×200; p50 1.8 ms, p99 4.3 ms, max 18.3 ms, 95 req/s; 4 queries, 4 DB connections (polls) | **PASS** |
| 8i | Burst, `JDBCLoginModule`, 10 parallel | all 200 | 1000×200; p50 36.6 ms, p99 73.8 ms, max 85.3 ms, 192 req/s; 2,000 queries, 2,000 DB connections | **PASS** |
| 8j | Burst, in-memory, 10 parallel | all 200 | 1000×200; p50 2.1 ms, p99 6.7 ms, max 13.3 ms, 598 req/s; 1 query, 1 DB connection | **PASS** |
| 8k | Burst, in-memory, plugin OFF, 10 parallel | all 200 | 1000×200; p50 2.6 ms, p99 6.5 ms, max 9.0 ms, 598 req/s; 0 queries | **PASS** |
| 8l | Burst, in-memory, 50 parallel | all 200 | 1000×200; p50 2.5 ms, p99 18.7 ms, max 23.9 ms, 666 req/s; 1 query, 1 DB connection | **PASS** |
| 8m | `JDBCLoginModule`, correct password while SQL Server is down | – | **401 Unauthorized** | **INFO** |
| 8n | `JDBCLoginModule`, correct password after SQL Server is back | 200 within 30 s | 200, 110 ms after the database was online | **PASS** |

### Test 9: sr-acl-cli internals

| # | Check | Expected | Actual | Result |
|---|---|---|---|---|
| 9a | Java class the CLI runs | `…tools.SchemaRegistryAclCommand` | `io.confluent.kafka.schemaregistry.security.tools.SchemaRegistryAclCommand` | **PASS** |
| 9b | Format of ACL records on `_schemas_acl` | – | plain JSON key/value; 17 subject + 5 global records in the topic history (`results/schemas_acl-dump.txt`) | **INFO** |

### Test 10: In-memory login: freshness and SQL Server outages

All requests go to `schema-registry` (port 8081), `GET /subjects/orders-value/versions/latest`. See [F7](#f7).
- **10a–10c** change a row in SQL Server, then poll every 100 ms until the response changes. 403 means "logged in, no ACL", which is what a new Service without ACLs gets.
- **10d–10g** stop SQL Server, check the running registry, restart the registry container, then start SQL Server and the registry again.

| # | Check | Expected | Actual | Result |
|---|---|---|---|---|
| 10a | New user inserted → can log in | ≤ 6 s | 1971 ms | **PASS** |
| 10b | Password changed → new password works | ≤ 6 s | 4680 ms; old password then gets 401 | **PASS** |
| 10c | User deleted → locked out | ≤ 6 s | 4668 ms | **PASS** |
| 10d | Correct password while SQL Server is down | 200 | 200 | **PASS** |
| 10e | Wrong password while SQL Server is down | 401 | 401 | **PASS** |
| 10f | Restart the registry while SQL Server is down | exits, code 1 | exited, code 1: `auth preflight FAILED: … The TCP/IP connection to the host mssql … failed` | **PASS** |
| 10g | SQL Server back: start the registry, correct password | 200 | 200 | **PASS** |

---

## 9. Managing ACLs without the CLI (test 9)

**What the CLI is.** `bin/sr-acl-cli` is a shell script that runs:

```
security-plugins-run-class io.confluent.kafka.schemaregistry.security.tools.SchemaRegistryAclCommand "$@"
```

The classpath is built from the jar folders `confluent-security/schema-registry`, `confluent-common`, `rest-utils` and `schema-registry`.

**Calling it from the platform's Java code** (assessed by reading the compiled class; nothing was built): feasible, but not a supported API.
- **Entry point:** the only public entry point is `main(String[] args)`. You pass the same flags as on the command line, including `--config <Schema Registry properties file>`. The methods that do the work (`addAcl`, `removeAcl`, `processCommand`) aren't public.
- **Cost per call:** each call creates a Kafka producer and a reader, reads the **whole** `_schemas_acl` topic, writes the change, then shuts down. That's about one second per call, so batch changes.
- **It can kill your program.** On any error it calls Kafka's `Exit.exit(1)`, which ends the whole JVM. Inside the platform service you'd have to install `org.apache.kafka.common.utils.Exit.setExitProcedure(...)` first, to turn that into an exception.
- **Dependencies:** you'd ship Confluent's plugin and Schema Registry jars with the platform, and keep their version in step with Schema Registry.

**The simpler alternative: write the records yourself.** The ACL store is a Kafka topic with plain JSON records:

```
key:   {"keytype":"SUBJECT_ACL","magic":0,"principal":"c17.svc-a","subject":"orders-value"}
value: {"magic":0,"principal":"c17.svc-a","subject":"orders-value","allowedoperations":["SUBJECT_READ"]}

key:   {"keytype":"GLOBAL_ACL","magic":0,"principal":"platform-admin"}
value: {"magic":0,"principal":"platform-admin","allowedoperations":["GLOBAL_COMPATIBILITY_READ","GLOBAL_COMPATIBILITY_WRITE","GLOBAL_READ"]}
```

- **Writing:** a normal Kafka producer in the platform could write these. Schema Registry picks them up within about a second (F5).
- **Each record holds the complete set of operations** for one (user, subject) pair. Every change is therefore "read the current set, modify it, write it back". Two writers changing the same pair at the same time can lose an update, so serialize ACL changes in the platform.
- **The format is undocumented.** Ask Confluent whether it's stable before relying on it (question 4).

---

## 10. Configuration: what was verified and what was guessed

**How names were verified.** Every property name was checked in the compiled jars inside the `cp-schema-registry:7.9.0` image (with `javap`) and against Confluent's 7.9 documentation. The image contains **Jetty 9.4.57.v20241219**, including `jetty-jaas`, which provides the login-module base classes and the JDBC login module.

### Verified

| Setting | Where it was verified / notes |
|---|---|
| `authentication.method=BASIC`, `authentication.realm=SchemaRegistryRealm`, `authentication.roles=sr-user` | `io.confluent.rest.RestConfig` (rest-utils 7.9.0). The realm name must match the entry name in `jaas.conf`. |
| Test 8 baseline (`jaas-jdbc.conf`): JAAS entry `org.eclipse.jetty.jaas.spi.JDBCLoginModule` with `dbDriver`, `dbUrl`, `dbUserName`, `dbPassword`, `userTable`, `userField`, `credentialField`, `userRoleTable`, `userRoleUserField`, `userRoleRoleField` | `JDBCLoginModule` / `AbstractDatabaseLoginModule` in Jetty 9.4.57. They generate `select <credentialField> from <userTable> where <userField>=?` and `select <userRoleRoleField> from <userRoleTable> where <userRoleUserField>=?`. |
| JAAS file location: `-Djava.security.auth.login.config=…` via `SCHEMA_REGISTRY_OPTS` | Standard JVM mechanism. Set in `docker-compose.yml`. |
| `resource.extension.class=io.confluent.kafka.schemaregistry.security.SchemaRegistrySecurityResourceExtension` | docs + jar |
| `confluent.schema.registry.authorizer.class=io.confluent.kafka.schemaregistry.security.authorizer.schemaregistryacl.SchemaRegistryAclAuthorizer` | docs + jar |
| `confluent.schema.registry.auth.mechanism=JETTY_AUTH` | The setting accepts `SSL` (default) and `JETTY_AUTH` (enum `io.confluent.common.security.auth.RestAuthType`). The docs only show `SSL`. |
| `confluent.schema.registry.acl.topic=_schemas_acl` | Default is `<kafkastore.topic>_acl`; set explicitly. docs + jar |
| `schema.linking.rbac.enable=true` | POC only; no effect once exporters are removed (§12). Default `false` (`SecureSchemaRegistryConfig`); its effect was read in `AuthorizationFilter` and confirmed by test 5. |
| Exporters: add `io.confluent.schema.exporter.SchemaExporterResourceExtension` to `resource.extension.class`, plus `kafkastore.update.handlers=io.confluent.schema.exporter.storage.SchemaExporterUpdateHandler` and `password.encoder.secret` | **POC only, for test 5; not in the production config (§12).** Without the update handler, Schema Registry crashes at startup (F6). |
| `kafkastore.bootstrap.servers`, `kafkastore.security.protocol=SASL_PLAINTEXT`, `kafkastore.sasl.mechanism=SCRAM-SHA-256`, `kafkastore.sasl.jaas.config` | Standard. `sr-acl-cli` reads the same file and uses these settings to reach Kafka. |
| Custom login module on the classpath: `CLASSPATH=/opt/poc-auth/sr-auth-snapshot.jar` | `schema-registry-run-class` appends to an existing `CLASSPATH`, so the jar loads in the same classloader as `jetty-jaas`. Confirmed by test 10. |
| `poc.auth.SnapshotLoginModule` extending Jetty's `AbstractLoginModule`, returning `UserInfo(name, Credential, roles)` | Signatures read with `javap` from `jetty-jaas-9.4.57`. `AbstractLoginModule.login()` calls `getUserInfo`, then `checkCredential`, then `fetchRoles`, which is a no-op once roles are passed to the constructor. JAAS creates a new module per login, so the snapshot is static. |
| SQL Server JDBC driver | Not in the image. Added in `schema-registry/Dockerfile` (`com.microsoft.sqlserver:mssql-jdbc:12.8.1.jre11`). |
| `CHANGE_TRACKING_CURRENT_VERSION()` as `sr_auth` | Works with only `SELECT` on the view. `VIEW CHANGE TRACKING` would only be needed for `CHANGETABLE()`, which the module doesn't use. |

### Guessed or not minimized: check before production

- **Kafka ACLs for the Schema Registry user are deliberately broad**, not a verified minimum:
  - prefix grants on topics `_schemas*` (covers `_schemas` and `_schemas_acl`) and `_exporter*` (the `_exporter*` grant is POC only; drop it in production, §12), with Read, Write, Describe, DescribeConfigs and Create;
  - both license topics;
  - consumer groups `schema-registry*`, `sr-*`, `confluent-license*` and `_confluent-license*`;
  - cluster Describe and DescribeConfigs.

  The real minimum is question 8.
- **License topic:** `confluent.license.topic=_confluent-license` and `confluent.license.topic.replication.factor=1` were never exercised (see §6).
- **`authentication.roles=sr-user`:** every user gets this role (from the module's `role` option, or the `sr_auth_roles` view for `JDBCLoginModule`). It only says "this user exists"; all real permissions come from ACLs.
- **Not tried:** `confluent.schema.registry.anonymous.principal` (left unset), the combined `RbacAndAclAuthorizer`, and Jetty's `CRYPT:` password format.
- **Kafka broker:** the controller listener is PLAINTEXT but bound to localhost inside the container. Fine for a POC, not a production pattern.
- **SQL Server TLS:** `jaas.conf` and `jaas-jdbc.conf` use `encrypt=true;trustServerCertificate=true`, which encrypts without checking the server certificate. Production should validate it.
- **Snapshot intervals** (`changePollSeconds=5`, `fullReloadSeconds=300`) are the values discussed, not tuned. The periodic 5-minute reload wasn't waited for in the tests; only change-triggered reloads were.

---

## 11. Limitations of this POC

- **Single node:** one Kafka broker and one Schema Registry node (plus the two test 8 registries, which join as non-leaders). Multi-node Schema Registry (leader forwarding, several nodes reading `_schemas_acl`) wasn't tested, although ACL propagation works per node in the same way.
- **Latency numbers** go through Docker Desktop port forwarding on a laptop. Use them to compare plugin on vs off, not as production estimates.
- **The exporter test (5) checks permissions only.** It doesn't run a real export to another registry.
- **DR with Cluster Linking (§12) wasn't tested.** There is one Kafka cluster and no cluster link.
- **svc-b's Kafka read on `orders` is artificial** (see §4); it isolates the Schema Registry check.
- **The platform tables are a simplified stand-in** for the real platform database. The important part is the views the login modules read.
- **SQL Server runs under emulation** on the Apple Silicon test machine, and with 4 users, not 10,000. Load time for a large table wasn't measured.
- **Licensing ran on the single-broker developer license.** Behavior with an Enterprise key wasn't tested.

---

## 12. Disaster recovery with Cluster Linking instead of exporters (design, not tested)

**Decision.** Production Schema Registries don't load the exporter extension. Schemas and ACLs reach the DR site through Confluent Cluster Linking, which mirrors Schema Registry's Kafka topics. This section is a design. **None of it was run in this POC** (§11).

### Why Cluster Linking and not exporters

- **No `/exporters` endpoint.** Without `SchemaExporterResourceExtension`, F1 can't happen: tenants have nothing to list, create, pause or delete.
- **ACLs are copied too.** All permissions live in `_schemas_acl` (§3). Mirroring that topic gives the DR registry the same permission table. Exporters copy schemas only, never ACLs, so with them the platform would have to replay every ACL change to the DR registry itself.
- **Same schema IDs.** A mirror topic is a byte-for-byte copy with the same offsets. The DR registry rebuilds the same subjects, versions and IDs from `_schemas`, so records in mirrored data topics still resolve to the right schemas.

### Setup

```
 primary Kafka cluster                         DR Kafka cluster (Confluent Server)
 ┌───────────────────────────┐   cluster link  ┌──────────────────────────────────┐
 │ _schemas                  │ ──────────────► │ _schemas      (mirror, read-only) │
 │ _schemas_acl              │ ──────────────► │ _schemas_acl  (mirror, read-only) │
 │ data topics (orders, …)   │ ──────────────► │ data topics   (mirrors)           │
 └───────────────────────────┘                 └──────────────────────────────────┘
        ▲                                                  ▲
 primary Schema Registry                            DR Schema Registry
 (leader.eligibility=true,                          (leader.eligibility=false while the
  takes writes)                                      link is active: reads only)
```

- **Mirror only `_schemas` and `_schemas_acl`.** Don't mirror the license topic: the DR registry writes its own. With no exporters there are no `_exporter_*` topics.
- **The DR registry is read-only until failover.** Mirror topics can't be written to, so the DR nodes run with `leader.eligibility=false`. Reads (deserializers, `GET /schemas/ids/{id}`) work. Registrations and `sr-acl-cli` changes fail, which keeps a single writer for both (recommendation 5). The benchmark twin `schema-registry-noplugin` already runs this way against `_schemas` here, but on the same cluster, not on a mirror.
- **Production config without exporters.** Remove `io.confluent.schema.exporter.SchemaExporterResourceExtension` from `resource.extension.class`, and remove `kafkastore.update.handlers`, `password.encoder.secret`, `exporter.config.topic`, `exporter.state.topic` and the `_exporter*` Kafka ACLs. `schema.linking.rbac.enable` then has no effect; keeping it `true` costs nothing if exporters are ever turned back on.

### Failover

1. Promote (or fail over) the `_schemas` and `_schemas_acl` mirror topics on the DR cluster, so they become writable.
2. Restart the DR registries with `leader.eligibility=true` so one of them becomes leader and accepts registrations.
3. Point `sr-acl-cli` (or the platform's ACL writer) at the DR cluster's config.

Failing back is the same in reverse: link the DR topics back to the primary, then promote them there.

### Risks and things the DR site also needs

- **Replication lag (RPO).** Mirroring is asynchronous, and nothing orders `_schemas` against the data topics. A schema registered just before the disaster can be missing in DR while data that uses its ID got there. Those records won't deserialize after failover. Because only the platform registers schemas, this window is small and observable (compare the latest schema ID on both sides).
- **The rest of the login chain.** The DR registry needs a reachable copy of the SQL Server auth database (§3, step 1). Without it the DR registry refuses to start (F7).
- **Kafka users and ACLs on the DR cluster.** Cluster Linking can sync Kafka ACLs, but not SCRAM credentials. Every Service's SCRAM user, and Schema Registry's own user and Kafka ACLs, must already exist on the DR cluster.
- **Licensing.** Cluster Linking needs Confluent Server on the destination cluster and is a licensed feature.
- **Not verified:** that a registry with no eligible leader starts and serves reads cleanly from a mirror topic it can't write to, and that the Security Plugin builds its ACL table from a mirrored `_schemas_acl`. Both should be tested before relying on this design (question 9).

### How this POC could test it

The broker image is already `cp-server:7.9.0`. Add a second Kafka cluster, a cluster link mirroring `_schemas`, `_schemas_acl` and `orders`, and a DR registry with `leader.eligibility=false`. Then check: schema IDs match on both sides, svc-a/svc-b get the same 200/403 in DR, writes are refused before promotion, and work after promotion and restart.

---

## 13. Open questions for Confluent support

1. **Exporters in ACL mode** (not blocking: production doesn't enable exporters, §12). With `SchemaRegistryAclAuthorizer` and the default `schema.linking.rbac.enable=false`, the `/exporters` endpoints aren't authorized at all. Any authenticated user can create, read, pause and delete exporters. Is that intended? Is `schema.linking.rbac.enable=true` supported with the ACL authorizer? Is the resulting rule intended: `EXPORTER_ACCESS` only for users holding every SUBJECT operation on `*`?
2. **Subject listing.** `GET /subjects` (with `GLOBAL_READ`) filters on `SUBJECT_COMPATIBILITY_READ`, while `GET /schemas` filters on `SUBJECT_READ`. Is that intended? Which operations should a read-only tenant get?
3. **ACL patterns.** Are subject patterns with `*` in any position (e.g. `com.acme.payments.*`) supported and stable, or only the documented prefix forms like `svc*`? Is there a recommended model for RecordNameStrategy subjects with many tenants?
4. **ACL storage format.** Is the JSON format of `_schemas_acl` stable and supported for direct writes? Is there a supported Java or REST API for managing ACLs instead of `sr-acl-cli`?
5. **Database login.** Is a custom JAAS login module (like the POC's in-memory module, built on Jetty's `AbstractLoginModule`) supported with `confluent.schema.registry.auth.mechanism=JETTY_AUTH`? Jetty's own `JDBCLoginModule` opens 2 database connections per request with no caching. Is there a supported way to cache logins instead?
6. **License topic.** For the Schema Registry Security Plugin on 7.9, which license topic is used: `_confluent-license` (the code's default) or `_confluent-command` (the docs)? What Kafka ACLs does the Schema Registry user need once the license key is set?
7. **Encryption keys.** `authorizeKekOperation` always returns "allowed" in the ACL authorizer. Does that mean CSFLE / DEK registry endpoints are unprotected in ACL mode?
8. **Minimal Kafka ACLs.** What is the minimal set of Kafka ACLs (topics and consumer groups) the Schema Registry user needs with the Security Plugin enabled and exporters disabled, both on the primary and on a DR registry reading Cluster Linking mirrors?
9. **Disaster recovery with Cluster Linking.** Is running a DR Schema Registry (`leader.eligibility=false`) against Cluster Linking mirrors of `_schemas` and `_schemas_acl` supported with the Security Plugin in ACL mode, and is the failover in §12 (promote mirrors, restart with `leader.eligibility=true`) the recommended procedure? Confluent's documented DR path for schemas is Schema Linking.
