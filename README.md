# Schema Registry security POC: ACL mode with database-backed login

This folder is a local proof of concept (POC) of the Schema Registry security design planned for the "Kafka topic as a service" platform. It runs on a laptop with Docker Compose. One command starts everything, one seeds it, one runs every test, and one deletes it.

The POC answers one question: **does the planned design actually work?** The design has three parts:

- **Login:** tenants log in to Schema Registry over plain HTTP with HTTP Basic auth. The username is `<cluster>.<scram-user>` (for example `c17.svc-payments`) and the password is the Service's Kafka SCRAM password. Schema Registry checks the password against the platform's PostgreSQL database, using Jetty's JDBC login module.
- **Permissions:** the Confluent Schema Registry Security Plugin enforces them in **ACL mode** (`SchemaRegistryAclAuthorizer`). ACLs are managed with `sr-acl-cli`. There is no RBAC, MDS or LDAP.
- **Who does what:** only the platform (`platform-admin`) registers schemas. Apps are read-only (`auto.register.schemas=false`). A Service with read or write on topic T gets `SUBJECT_READ` on T's subjects.

Everything below was run on **2026-10-12**, starting from a clean environment (down → up → seed → test). The results were **60 PASS, 10 FAIL, 10 INFO**. All 10 FAILs are real problems with the design, not broken tests.

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
12. [Open questions for Confluent support](#12-open-questions-for-confluent-support)

---

## 1. Summary: answers to the 9 questions

| # | Question | Answer | Works as designed? |
|---|---|---|---|
| 1 | Does database login work? | Yes. A missing password, wrong password or unknown user gets **401**. A correct password gets in. Passwords stored as plaintext or as Jetty's `MD5:` format both work. | ✅ Yes |
| 2 | Are permissions enforced? | Yes. svc-a can read `orders-value`; svc-b gets **403**. svc-a can't register schemas, delete subjects or change compatibility (403). platform-admin can do all of these. | ✅ Yes |
| 3 | `GET /schemas/ids/{id}` | Matches the docs. It's allowed only if the caller can read at least one subject that uses the ID. svc-a: 200 for its own ID, 403 for another subject's ID. svc-b: 403. | ✅ Yes |
| 4 | What does `GET /subjects` return to svc-b? | **403**, not an empty or filtered list. Listing needs the global `GLOBAL_READ` permission. With `GLOBAL_READ` the list is filtered, but on `SUBJECT_COMPATIBILITY_READ` rather than `SUBJECT_READ`. | ⚠️ Different from what you'd expect ([F3](#f3)) |
| 5 | Can tenants manage exporters? | **With the default config: yes, all of them. That's a security hole.** Any logged-in tenant, even one with zero ACLs, can list, read, create, pause and delete exporters, including the platform's own. Setting `schema.linking.rbac.enable=true` fixes it: tenants get 403 and platform-admin keeps access. | ❌ Not by default ([F1](#f1)) |
| 6 | Do real Java clients work? | (a) TopicNameStrategy: **yes**. (b) RecordNameStrategy set by config only: **no**, the producer gets 403. The topic ACL doesn't cover the record-name subject. Adding a pattern ACL on the record namespace fixes it. A consumer running as svc-b fails with 403, as intended. | ⚠️ (a) yes, (b) not with topic-based ACLs ([F2](#f2)) |
| 7 | How fast do ACL changes take effect? | About **0.9 s** after `sr-acl-cli` starts. That's before the CLI exits; the CLI itself takes about 1.5 s, mostly JVM startup. Adds and removes are equally fast. | ✅ Yes ([F6](#f6)) |
| 8 | Database load | **Every request goes to the database; nothing is cached.** Each request runs 2 SQL queries and opens **2 new Postgres connections** (no pooling). 1,000 requests caused 2,000 connections. Response times were p50 6.6 ms and p99 9.3 ms one at a time, and about 470 req/s with 10 in parallel. The plugin adds no measurable latency. While Postgres is down, every client gets 401. | ⚠️ Works, but needs a plan ([F4](#f4)) |
| 9 | Can ACLs be managed from Java without the CLI? | Possible but unsupported. The CLI runs `io.confluent.kafka.schemaregistry.security.tools.SchemaRegistryAclCommand`. You can call its `main()`, but on any error it shuts down the calling program unless you override `Exit`. Alternatively, write the plain-JSON ACL records to the `_schemas_acl` Kafka topic yourself (undocumented format). | ⚠️ Feasible, not supported ([§9](#9-managing-acls-without-the-cli-test-9)) |

Two more findings that weren't among the questions:
- **Password storage and transport** ([F5](#f5)). Jetty can only check passwords stored as plaintext, unsalted MD5 or old DES crypt. And over plain HTTP, every request carries the tenant's **Kafka** SCRAM password in readable form.
- **License** ([§6](#6-licensing)). The POC runs under Confluent's free single-broker developer license. Production needs your Enterprise key.

---

## 2. What must change in the design

The recommendations below are ordered by importance.

1. **Set `schema.linking.rbac.enable=true`** on every Schema Registry that has exporters enabled, or don't enable exporters. Without it, any tenant can copy all schemas out of the registry and delete your Schema Linking exporters ([F1](#f1)). Ask Confluent to confirm this setting is supported in ACL mode (Q1).
2. **Pick TopicNameStrategy as the platform standard.** The rule "topic permission ⇒ `SUBJECT_READ` on T's subjects" only works with it. If teams need RecordNameStrategy, treat record-name subjects as separately granted resources, for example with a namespace pattern ACL per Service ([F2](#f2)).
3. **Plan for database load from authentication.** There are 2 connections per request with no cache. Options: a pgbouncer or read replica dedicated to Schema Registry, or a small caching login module. Also decide what should happen when the database is down; today clients see 401 ([F4](#f4)).
4. **Don't send Kafka passwords over plain HTTP.** Either enable TLS on the Schema Registry listener, or give each Service a separate Schema Registry password. Also decide how passwords are stored: plaintext, unsalted MD5 and DES crypt (8-character limit) are the only formats Jetty accepts ([F5](#f5)).
5. **Grant `SUBJECT_READ` only, unless an app needs to list subjects.** Read-only apps work with it. Tools and UIs that list subjects also need `GLOBAL_READ` plus `SUBJECT_COMPATIBILITY_READ` ([F3](#f3)).
6. **Run all ACL changes through one writer in the platform.** Each ACL record holds the complete permission set, so two writers changing the same entry at once can lose an update ([§9](#9-managing-acls-without-the-cli-test-9)).
7. **Put your Enterprise license key in `confluent.license`** and get the license topic and Kafka ACLs confirmed by Confluent (Q6).

---

## 3. Background: how a request is checked

### The path of one request

```
client (serializer / curl)
   │  HTTP Basic: "c17.svc-a:<password>"
   ▼
Schema Registry (Jetty web server)
   │ 1. AUTHENTICATION: Jetty JAAS realm "SchemaRegistryRealm"
   │    → JDBCLoginModule opens a Postgres connection and runs
   │        select password from sr_auth_users where username=?
   │    → compares the password (plaintext / MD5: / CRYPT:)
   │    → opens another connection and runs
   │        select role from sr_auth_roles where username=?
   │    wrong/unknown → 401
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
| **Exporter / Schema Linking** | A job running inside Schema Registry that continuously copies schemas to another Schema Registry at a URL given in its config. It is managed through `/exporters` endpoints. |
| **`schema.linking.rbac.enable`** | A Security Plugin setting. When `true`, the `/exporters` endpoints are permission-checked. Default `false`. |
| **401 vs 403** | 401 = authentication failed (who are you?). 403 = authenticated, but not allowed. |

---

## 4. What was built

```
                          ┌───────────────────────────── docker network ─────────────────────────────┐
 host :8081 ──────────────► schema-registry (cp-schema-registry 7.9.0 + Postgres JDBC driver)         │
                          │    • HTTP Basic, JAAS realm → Jetty JDBCLoginModule ──────► postgres:16    │
                          │    • Security Plugin, ACL authorizer          (platform DB, sr_auth_* views)│
                          │    • exporters (Schema Linking) enabled                                    │
                          │    • storage + ACLs in Kafka: _schemas, _schemas_acl                       │
                          │                         │ SASL_PLAINTEXT / SCRAM-SHA-256                   │
 host :9092 ──────────────► kafka (cp-server 7.9.0, single KRaft node, ACLs enabled)                   │
                          │                                                                          │
 host :8082 ──────────────► schema-registry-noplugin  (test 8 only: same login, plugin OFF)           │
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

### The platform database (Postgres)

`postgres/init.sql` models the platform with three tables: `clusters`, `services` (cluster + SCRAM user + password) and `platform_principals` (platform-admin). Jetty never reads those tables directly. It reads two views:

- `sr_auth_users(username, password)`: `cluster_id || '.' || scram_user` for every Service, plus the platform principals.
- `sr_auth_roles(username, role)`: every user gets the role `sr-user`. Schema Registry requires a role; actual permissions come from ACLs.

Schema Registry connects as `sr_auth`, a login that can only `SELECT` from those two views. `pg_stat_statements` and connection logging are enabled so the tests can count queries and connections.

### Files

| Path | What it is |
|---|---|
| `docker-compose.yml` | All services. `schema-registry-noplugin` only starts with profile `bench`, `client` only with profile `client`. |
| `schema-registry/Dockerfile` | `cp-schema-registry:7.9.0` plus the PostgreSQL JDBC driver 42.7.4. **This is the only addition**; the Security Plugin is already in the image. |
| `schema-registry/sr.properties` | Schema Registry config: login, plugin, ACL authorizer, Kafka connection, exporters. Every line is commented. |
| `schema-registry/sr-noplugin.properties` | The same, without the plugin. Used only for the test 8 comparison. |
| `schema-registry/jaas.conf` | The login realm: Jetty `JDBCLoginModule` pointing at Postgres. |
| `postgres/init.sql` | Tables, views, the `sr_auth` login, `pg_stat_statements`. |
| `kafka/` | The broker's SCRAM login file and an admin client config. |
| `client/` | Java producer/consumer (Maven): `auto.register.schemas=false`, choice of TopicNameStrategy or RecordNameStrategy. |
| `scripts/up.sh` | Builds the image, starts Kafka and Postgres, creates SCRAM users, Kafka ACLs and topics, then starts Schema Registry. |
| `scripts/seed.sh` | Inserts users into Postgres, creates Schema Registry ACLs with `sr-acl-cli`, registers the schemas as platform-admin. |
| `scripts/test.sh` | Runs tests 1–9 and writes `results/`. |
| `scripts/down.sh` | Stops everything and deletes all data. |
| `results/results.md` | The latest results table (regenerated on every test run). |
| `results/details.log` | Full client output from the latest run. |
| `results/burst-*.txt` | Raw status code and latency of every request in the test 8 bursts. |
| `results/schemas_acl-dump.txt` | The raw contents of the `_schemas_acl` topic. |

---

## 5. How to run it and explore by hand

**Requirements:** Docker Desktop with about 4 GB free memory, plus `curl`, `python3` and `perl` (all on macOS by default). Ports 8081, 8082 and 9092 must be free. Postgres has no host port.

```bash
./scripts/up.sh      # ~1-2 min the first time (image build); ends with "Schema Registry is up"
./scripts/seed.sh    # a few seconds
./scripts/test.sh    # ~2 min; prints PASS/FAIL/INFO per check, writes results/results.md
./scripts/down.sh    # removes containers and all data
```

`test.sh` can be run repeatedly. It cleans up the ACLs and exporters it creates. It restarts Schema Registry twice during test 5 (with the exporter setting off, then on), so don't be surprised by short pauses.

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

# Add a tenant: the Postgres row is all that's needed to log in (403 = logged in, no ACLs yet)
docker compose exec postgres psql -U platform -d platform \
  -c "INSERT INTO services VALUES ('c17','svc-c','svc-c-secret')"
curl -u c17.svc-c:svc-c-secret http://localhost:8081/subjects/orders-value/versions/latest   # 403
# Deleting the row locks the user out on the next request (401): there is no cache

# Watch the queries Schema Registry sends to Postgres
docker compose logs -f postgres | grep sr_auth

# Run the Java client yourself (build happens inside test.sh, or: docker compose run --rm --entrypoint mvn client -q package)
docker compose --profile client run --rm client produce orders c17 svc-a svc-a-secret topic
docker compose --profile client run --rm client consume orders c17 svc-a svc-a-secret topic
docker compose --profile client run --rm client consume orders c17 svc-b svc-b-secret topic   # fails: 403
```

Client arguments: `produce|consume <topic> <cluster> <scram-user> <password> <topic|record>`. The Kafka login is `<scram-user>`, the Schema Registry login is `<cluster>.<scram-user>`, and both use the same password. `record` makes the client use RecordNameStrategy, set purely by configuration.

---

## 6. Licensing

- **Plugin:** the Security Plugin is **already included** in `confluentinc/cp-schema-registry:7.9.0` (in `/usr/share/java/confluent-security/schema-registry/`, which is on the classpath), and so is `sr-acl-cli`. Nothing had to be installed except the Postgres JDBC driver.
- **What the POC runs on:** no license key is configured. At startup Schema Registry logs `License for single cluster, single node`, meaning it runs under Confluent's **free developer license for single-broker clusters**, not the 30-day trial. It has no expiry while the cluster has one broker.
- **Production:** your clusters have multiple brokers, so they need the **Enterprise license key** in `confluent.license`, plus Kafka ACLs letting the Schema Registry user use the license topic.
- **Not confirmed:** the docs name the license topic `_confluent-command`, but the plugin's built-in default is `_confluent-license`. This POC sets `_confluent-license` and grants Kafka ACLs on both. The developer license never used either topic, so which one matters couldn't be confirmed (question 6 in §12).

---

## 7. Findings in detail

<a id="f1"></a>
### F1. Exporters are unprotected by default (security issue)

**What was found.** The docs say "Schema Registry ACL Authorizer does not support schema exporters". That reads like "exporters are denied". In fact, with the default `schema.linking.rbac.enable=false`, the plugin **doesn't check the `/exporters` endpoints at all**. In tests 5a1–5a9 both tenants did all of the following:
- svc-a, which only has `SUBJECT_READ` on `orders`, and svc-b, which has no ACLs, could list all exporters.
- Both could read the config of the platform's exporter.
- Both could create exporters of their own.
- Both could pause the platform's exporter, and svc-b deleted it.

**Why it matters.** An exporter runs inside Schema Registry. It reads schemas straight from storage, without permission checks, and sends them to whatever Schema Registry URL its creator puts in its config. So any tenant can copy every schema in the registry to a place of their choosing. They can also stop or delete the platform's own Schema Linking.

**The fix that works.** With `schema.linking.rbac.enable=true`, every `/exporters` call needs the `EXPORTER_ACCESS` operation. The ACL authorizer grants it only to users who hold **all five** SUBJECT operations on subject `*`. Here that's platform-admin. In tests 5b0–5bx both tenants got 403 on every exporter call and platform-admin could still create, pause and delete. **`schema-registry/sr.properties` ships with this set to `true`.**

**Caveat.** This setting is documented for RBAC, not for ACL mode, so ask Confluent to confirm it's supported (question 1).

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
### F4. Every request goes to the database; no cache, no connection pool

**Measurements (test 8):**

| Request type | SQL queries | New Postgres connections |
|---|---|---|
| correct password | 2 (user + role) | **2** |
| wrong password / unknown user | 1 | 1 |
| no credentials | 0 | 0 |
| 20 requests on one reused HTTP connection | 40 | 40 (no reuse at all) |
| plugin turned off | same as plugin on | same as plugin on |

**Why.** Jetty's `JDBCLoginModule` opens a brand-new database connection (`DriverManager.getConnection`) for each of its two queries and keeps nothing between requests.

**Load tests (1,000 requests, measured from the Mac through Docker Desktop, so compare the numbers with each other, not with production):**

| Run | p50 | p99 | Throughput | Postgres connections |
|---|---|---|---|---|
| plugin ON, 1 at a time | 6.6 ms | 9.3 ms | 76 req/s | 2,000 |
| plugin OFF, 1 at a time | 6.8 ms | 10.1 ms | 74 req/s | 2,000 |
| plugin ON, 10 parallel | 10.2 ms | 17.7 ms | 466 req/s | 2,000 |
| plugin OFF, 10 parallel | 10.4 ms | 21.7 ms | 459 req/s | 2,000 |
| plugin ON, 50 parallel | 37.5 ms | 135.7 ms | 496 req/s | 2,000 |

All 5,000 requests succeeded.

**What it means:**
- **The plugin costs nothing measurable.** The database login is the expensive part.
- **About 500 requests per second means about 1,000 new connections per second** against the platform database.
- **Failed logins also hit the database,** so anyone who can reach Schema Registry can generate database load with bad passwords.
- **When Postgres is down,** every client gets **401 Unauthorized**, not a 5xx error (test 8l). To an app it looks like a wrong password, and nothing works until the database is back (test 8m).
- **The upside of no cache:** password changes and removals take effect on the **very next request**. A new row in `services` can log in immediately; deleting it gets 401 on the next call (checked by hand).
- **Real traffic is much lower than these bursts,** because serializers cache schema lookups. A redeploy of many apps, or a storm of restarts, still produces bursts like these.

**Options to evaluate:**
- A pgbouncer or read replica dedicated to Schema Registry authentication.
- A small custom login module with a short-lived cache.
- Jetty's `DataSourceLoginModule` with a pooled data source. It exists in this Jetty version but is untested with Schema Registry.

<a id="f5"></a>
### F5. How passwords are stored and sent

- **Storage.** Jetty 9.4 compares the password against the database value in only three formats:
  - plaintext;
  - `MD5:<hex>` (unsalted MD5);
  - `CRYPT:<…>` (old Unix DES crypt, where **only the first 8 characters of the password count**).

  The POC uses plaintext for svc-a and `MD5:` for svc-b; both work (tests 1d and 1e). Kafka's own salted SCRAM password data can't be reused, so the platform database must keep the password in one of these three forms.
- **Transport.** Over plain HTTP, every request carries `Authorization: Basic base64("c17.svc-a:<password>")`. That's readable by anyone who can see the traffic, and the password is the tenant's **Kafka** SCRAM password. Sniffing Schema Registry traffic therefore yields Kafka credentials.
- **Options:** enable TLS on the Schema Registry listener, or issue a separate Schema Registry password per Service so a leak doesn't expose Kafka.

<a id="f6"></a>
### F6. ACL changes take effect within about a second

**Measured (test 7, three adds and three removes):**
- An `sr-acl-cli --add` or `--remove` became effective **0.91–0.94 s** after the command started.
- The command itself took about **1.47 s**, so the change was live about half a second **before** the CLI exited.
- Most of the CLI time is Java startup and connecting to Kafka.

**How it works.** The CLI writes a record to the `_schemas_acl` topic. Every Schema Registry node reads that topic continuously and updates its in-memory permission table. With several Schema Registry nodes, each one picks up the change independently in the same way.

<a id="f7"></a>
### F7. Smaller observations

- **Unknown vs forbidden schema IDs.** An unknown schema ID returns 404, while an existing ID the caller can't read returns 403. A tenant can therefore probe which IDs exist, but not their content (test 3f).
- **KEK/DEK operations.** In the plugin's ACL authorizer, the method that checks encryption-key (KEK/DEK) operations always says "allowed" (`authorizeKekOperation`). This only matters if client-side field-level encryption (the DEK registry) is ever enabled. It's off here and wasn't tested (question 7).
- **Exporters need an undocumented-in-practice setting.** Enabling them requires `kafkastore.update.handlers=io.confluent.schema.exporter.storage.SchemaExporterUpdateHandler`. Without it, Schema Registry crashes at startup with a `NullPointerException`. This was hit while building the POC.

---

## 8. Test results, test by test

Each row: what was checked, what the design needs (Expected), what happened (Actual), and the verdict.
- **Response codes:** 200/204 = allowed, 401 = not logged in, 403 = logged in but not allowed, 404 = not found.
- **Freshness:** `results/results.md` holds the same table and is regenerated on every run. The tables below are the snapshot from 2026-10-12.

### Test 1: Authentication

Each request goes to `GET /subjects/orders-value/versions/latest` with different credentials. Test 1e uses svc-b, which has no ACLs: getting **403** rather than 401 proves its password (stored as `MD5:`) was accepted.

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
| 3f | svc-b `GET /schemas/ids/99999` (doesn't exist) | 403 or 404 | 404 (reveals existence, see F7) | **INFO** |

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

Run twice: first with the Confluent default `schema.linking.rbac.enable=false` (rows 5a), then with the shipped config `true` (rows 5b). Each time, platform-admin first creates a "platform" exporter, then each tenant tries to list exporters, create one, read the platform exporter's config, and pause it. svc-b also tries to delete it. Expected = what the design needs (tenants denied).

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
| 7a1 | `--add` #1 | live before CLI exits | 1467 ms / 941 ms | **PASS** |
| 7r1 | `--remove` #1 | live before CLI exits | 1484 ms / 930 ms | **PASS** |
| 7a2 | `--add` #2 | live before CLI exits | 1465 ms / 926 ms | **PASS** |
| 7r2 | `--remove` #2 | live before CLI exits | 1470 ms / 920 ms | **PASS** |
| 7a3 | `--add` #3 | live before CLI exits | 1443 ms / 907 ms | **PASS** |
| 7r3 | `--remove` #3 | live before CLI exits | 1478 ms / 912 ms | **PASS** |

### Test 8: Database load

**How it's measured:**
- **Queries** are counted with `pg_stat_statements`, filtered to the `sr_auth` database user.
- **Connections** are counted from Postgres's connection log.
- **Per-request rows (8a–8f)** use 20 requests each.
- **Bursts (8g–8k)** are 1,000 `GET /subjects/orders-value/versions/latest` requests as svc-a, either against the normal Schema Registry (plugin ON, port 8081) or the twin without the plugin (plugin OFF, port 8082).
- **8l/8m** stop Postgres, make a request, then start it again.

| # | Check | Expected | Actual | Result |
|---|---|---|---|---|
| 8a | Correct password, plugin ON, per request | – | 1 user query, 1 role query, **2 new DB connections** | **INFO** |
| 8b | Wrong password, per request | – | 1 query, 1 connection | **INFO** |
| 8c | Unknown user, per request | – | 1 query, 1 connection | **INFO** |
| 8d | No credentials, per request | – | nothing | **INFO** |
| 8e | Correct password, plugin OFF, per request | – | 1 + 1 queries, 2 connections (same as ON) | **INFO** |
| 8f | 20 requests on one reused HTTP connection | – | 20 + 20 queries, **40 connections** (no caching) | **INFO** |
| 8g | Burst, plugin ON, 1 at a time | all 200 | 1000×200; p50 6.6 ms, p99 9.3 ms, max 19.0 ms, 76 req/s; 2,000 DB connections | **PASS** |
| 8h | Burst, plugin OFF, 1 at a time | all 200 | 1000×200; p50 6.8 ms, p99 10.1 ms, max 14.2 ms, 74 req/s; 2,000 DB connections | **PASS** |
| 8i | Burst, plugin ON, 10 parallel | all 200 | 1000×200; p50 10.2 ms, p99 17.7 ms, max 24.2 ms, 466 req/s; 2,000 DB connections | **PASS** |
| 8j | Burst, plugin OFF, 10 parallel | all 200 | 1000×200; p50 10.4 ms, p99 21.7 ms, max 29.5 ms, 459 req/s; 2,000 DB connections | **PASS** |
| 8k | Burst, plugin ON, 50 parallel | all 200 | 1000×200; p50 37.5 ms, p99 135.7 ms, max 159.9 ms, 496 req/s; 2,000 DB connections | **PASS** |
| 8l | Correct password while Postgres is down | – | **401 Unauthorized** | **INFO** |
| 8m | Correct password after Postgres is back | 200 | 200 | **PASS** |

### Test 9: sr-acl-cli internals

| # | Check | Expected | Actual | Result |
|---|---|---|---|---|
| 9a | Java class the CLI runs | `…tools.SchemaRegistryAclCommand` | `io.confluent.kafka.schemaregistry.security.tools.SchemaRegistryAclCommand` | **PASS** |
| 9b | Format of ACL records on `_schemas_acl` | – | plain JSON key/value; 17 subject + 5 global records in the topic history (`results/schemas_acl-dump.txt`) | **INFO** |

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

- **Writing:** a normal Kafka producer in the platform could write these. Schema Registry picks them up within about a second (F6).
- **Each record holds the complete set of operations** for one (user, subject) pair. Every change is therefore "read the current set, modify it, write it back". Two writers changing the same pair at the same time can lose an update, so serialize ACL changes in the platform.
- **The format is undocumented.** Ask Confluent whether it's stable before relying on it (question 4).

---

## 10. Configuration: what was verified and what was guessed

**How names were verified.** Every property name was checked in the compiled jars inside the `cp-schema-registry:7.9.0` image (with `javap`) and against Confluent's 7.9 documentation. The image contains **Jetty 9.4.57.v20241219**, including `jetty-jaas`, which provides the JDBC login module.

### Verified

| Setting | Where it was verified / notes |
|---|---|
| `authentication.method=BASIC`, `authentication.realm=SchemaRegistryRealm`, `authentication.roles=sr-user` | `io.confluent.rest.RestConfig` (rest-utils 7.9.0). The realm name must match the entry name in `jaas.conf`. |
| JAAS entry `org.eclipse.jetty.jaas.spi.JDBCLoginModule` with `dbDriver`, `dbUrl`, `dbUserName`, `dbPassword`, `userTable`, `userField`, `credentialField`, `userRoleTable`, `userRoleUserField`, `userRoleRoleField` | `JDBCLoginModule` / `AbstractDatabaseLoginModule` in Jetty 9.4.57. They generate `select <credentialField> from <userTable> where <userField>=?` and `select <userRoleRoleField> from <userRoleTable> where <userRoleUserField>=?`. |
| JAAS file location: `-Djava.security.auth.login.config=…` via `SCHEMA_REGISTRY_OPTS` | Standard JVM mechanism. Set in `docker-compose.yml`. |
| `resource.extension.class=io.confluent.kafka.schemaregistry.security.SchemaRegistrySecurityResourceExtension` | docs + jar |
| `confluent.schema.registry.authorizer.class=io.confluent.kafka.schemaregistry.security.authorizer.schemaregistryacl.SchemaRegistryAclAuthorizer` | docs + jar |
| `confluent.schema.registry.auth.mechanism=JETTY_AUTH` | The setting accepts `SSL` (default) and `JETTY_AUTH` (enum `io.confluent.common.security.auth.RestAuthType`). The docs only show `SSL`. |
| `confluent.schema.registry.acl.topic=_schemas_acl` | Default is `<kafkastore.topic>_acl`; set explicitly. docs + jar |
| `schema.linking.rbac.enable=true` | Default `false` (`SecureSchemaRegistryConfig`); its effect was read in `AuthorizationFilter` and confirmed by test 5. |
| Exporters: add `io.confluent.schema.exporter.SchemaExporterResourceExtension` to `resource.extension.class`, plus `kafkastore.update.handlers=io.confluent.schema.exporter.storage.SchemaExporterUpdateHandler` and `password.encoder.secret` | Without the update handler, Schema Registry crashes at startup (F7). |
| `kafkastore.bootstrap.servers`, `kafkastore.security.protocol=SASL_PLAINTEXT`, `kafkastore.sasl.mechanism=SCRAM-SHA-256`, `kafkastore.sasl.jaas.config` | Standard. `sr-acl-cli` reads the same file and uses these settings to reach Kafka. |
| Postgres JDBC driver | Not in the image. Added in `schema-registry/Dockerfile` (`org.postgresql:postgresql:42.7.4`). |

### Guessed or not minimized: check before production

- **Kafka ACLs for the Schema Registry user are deliberately broad**, not a verified minimum:
  - prefix grants on topics `_schemas*` (covers `_schemas` and `_schemas_acl`) and `_exporter*`, with Read, Write, Describe, DescribeConfigs and Create;
  - both license topics;
  - consumer groups `schema-registry*`, `sr-*`, `confluent-license*` and `_confluent-license*`;
  - cluster Describe and DescribeConfigs.

  The real minimum is question 8.
- **License topic:** `confluent.license.topic=_confluent-license` and `confluent.license.topic.replication.factor=1` were never exercised (see §6).
- **`authentication.roles=sr-user`:** every user gets this role via the `sr_auth_roles` view. It only says "this user exists"; all real permissions come from ACLs.
- **Not tried:** `confluent.schema.registry.anonymous.principal` (left unset), the combined `RbacAndAclAuthorizer`, and Jetty's `CRYPT:` password format.
- **Kafka broker:** the controller listener is PLAINTEXT but bound to localhost inside the container. Fine for a POC, not a production pattern.

---

## 11. Limitations of this POC

- **Single node:** one Kafka broker and one Schema Registry node. Multi-node Schema Registry (leader forwarding, several nodes reading `_schemas_acl`) wasn't tested, although ACL propagation works per node in the same way.
- **Latency numbers** go through Docker Desktop port forwarding on a laptop. Use them to compare plugin on vs off, not as production estimates.
- **The exporter test (5) checks permissions only.** It doesn't run a real export to another registry.
- **svc-b's Kafka read on `orders` is artificial** (see §4); it isolates the Schema Registry check.
- **The platform tables are a simplified stand-in** for the real platform database. The important part is the two views Jetty reads.
- **Licensing ran on the single-broker developer license.** Behavior with an Enterprise key wasn't tested.

---

## 12. Open questions for Confluent support

1. **Exporters in ACL mode.** With `SchemaRegistryAclAuthorizer` and the default `schema.linking.rbac.enable=false`, the `/exporters` endpoints aren't authorized at all. Any authenticated user can create, read, pause and delete exporters. Is that intended? Is `schema.linking.rbac.enable=true` supported with the ACL authorizer? Is the resulting rule intended: `EXPORTER_ACCESS` only for users holding every SUBJECT operation on `*`?
2. **Subject listing.** `GET /subjects` (with `GLOBAL_READ`) filters on `SUBJECT_COMPATIBILITY_READ`, while `GET /schemas` filters on `SUBJECT_READ`. Is that intended? Which operations should a read-only tenant get?
3. **ACL patterns.** Are subject patterns with `*` in any position (e.g. `com.acme.payments.*`) supported and stable, or only the documented prefix forms like `svc*`? Is there a recommended model for RecordNameStrategy subjects with many tenants?
4. **ACL storage format.** Is the JSON format of `_schemas_acl` stable and supported for direct writes? Is there a supported Java or REST API for managing ACLs instead of `sr-acl-cli`?
5. **Database login.** Is Jetty's JAAS `JDBCLoginModule` supported with `confluent.schema.registry.auth.mechanism=JETTY_AUTH`? It opens 2 database connections per request with no caching. Is there a supported way to cache logins or pool connections, for example `DataSourceLoginModule`?
6. **License topic.** For the Schema Registry Security Plugin on 7.9, which license topic is used: `_confluent-license` (the code's default) or `_confluent-command` (the docs)? What Kafka ACLs does the Schema Registry user need once the license key is set?
7. **Encryption keys.** `authorizeKekOperation` always returns "allowed" in the ACL authorizer. Does that mean CSFLE / DEK registry endpoints are unprotected in ACL mode?
8. **Minimal Kafka ACLs.** What is the minimal set of Kafka ACLs (topics and consumer groups) the Schema Registry user needs with the Security Plugin and exporters enabled?
