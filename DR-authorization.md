# DR for Schema Registry: schemas, ACLs and RBAC

**Requirement:** active-passive DR. A DR Schema Registry runs all the time and serves reads, and only the primary accepts registrations. A cold standby that starts only at failover is not acceptable (README §12).

**Short answer:**
- Schemas reach DR through **Schema Linking (exporters)**.
- **ACLs don't reach DR on their own.** Neither ACL mode nor RBAC copies permissions to a DR registry.
- The platform's single ACL writer applies every change to **both** registries, and a periodic check compares the two sides.

## 1. Schemas: Schema Linking, because Cluster Linking can't feed a running registry

**Cluster Linking** mirrors `_schemas` to the DR Kafka cluster byte for byte. A mirror topic is **read-only**: Kafka rejects every write to it until the mirror is promoted.

**A Schema Registry can't start on a read-only topic.** At startup every node, even one with `leader.eligibility=false`, writes a no-op record to its store topic. It uses that write to find the end of the log before it serves anything. On a mirror, startup fails (tested 2026-10-14, README §12):

```
StoreException: Failed to write Noop record to kafka store.
InvalidRequestException: Cannot append records to read-only mirror topic '_schemas'
```

With Cluster Linking, the DR registry can only start **after** the mirrors are promoted, which makes it a cold standby. That fails the requirement.

**Schema Linking (exporters)** copies schemas over HTTP into a separate DR registry. That registry has its own writable `_schemas` and runs in `IMPORT` mode. It runs and serves reads all the time, so it meets the requirement. Confluent documents and recommends this for Schema Registry DR.

## 2. ACLs (`_schemas_acl`): neither linking method copies them

**Exporters copy schemas only.** ACLs are not part of Schema Linking.

**Cluster Linking fails on `_schemas_acl` for the same reason as on `_schemas`.** The ACL authorizer has its own copy of the same startup pattern. When `SchemaRegistryAclAuthorizer.configure()` runs, it calls `waitUntilKafkaReaderReachesLastOffset`, which calls `SchemaRegistryAclAuthorizerUtils.getLatestOffset`. That method writes a `NoopKey` record to the ACL topic and fails startup if the write fails:

```
"Sending Noop record to schema registry ACL topic to find last offset."
"Failed to write Noop record to schema registry ACL topic."
```

This comes from reading the bytecode of `confluent-schema-registry-security-plugin-7.9.0.jar` with javap. It hasn't been run against a mirror. A DR registry with `confluent.schema.registry.acl.topic` pointing at a mirror would fail at startup just like the `_schemas` case. Mirroring only `_schemas_acl` while exporters feed `_schemas` is therefore ruled out.

**What Confluent documents for ACLs:** the only Confluent design that mentions the ACL topic is the older [multi-datacenter architecture](https://docs.confluent.io/platform/current/schema-registry/multidc.html):
- **One registry cluster stretched over both sites.** All nodes connect to the **primary** Kafka cluster. DC B nodes have `leader.eligibility=false`, serve reads and forward writes to the leader in DC A.
- **Replicator copies `_schemas` and `_schemas_acl` to DR Kafka.** The docs say that when ACLs are enabled, "the schemas ACL topic must also be replicated".
- **At failover** you point DC B at the DR Kafka cluster and make it leader-eligible.

In that design the DR nodes never read the copied topics in steady state. That's why read-only copies (Replicator, or even Cluster Linking mirrors) are fine there. The costs:
- the DR registry depends on the primary Kafka cluster over the WAN;
- a DR node that restarts during an outage stays down until it's reconfigured;
- Confluent now recommends Schema Linking over Replicator.

This design is untested in this POC.

The [Schema Linking docs](https://docs.confluent.io/platform/current/schema-registry/schema-linking-cp.html) say nothing about ACLs.

## 3. RBAC mode: the topic problem goes away, the copying problem doesn't

| | ACL mode (this POC) | RBAC mode |
|---|---|---|
| Where permissions live | `_schemas_acl`, written by `sr-acl-cli`, read and written by every SR node | Metadata Service (MDS) on Confluent Server brokers; SR only queries it |
| Copied to DR by Schema Linking | No | No |
| Copied by Cluster Linking or Replicator | Topic can be copied, but SR can't run on a mirror (§2) | No. Confluent: role bindings are not synced; create them on both clusters ahead of time ([Cloud DR](https://docs.confluent.io/cloud/current/multi-cloud/cluster-linking/dr-failover.html), [Replicator](https://docs.confluent.io/platform/current/multi-dc-deployments/replicator/)) |
| DR needs its own grants | Yes | Yes. Role bindings are scoped to an SR cluster ID, and the DR registry has a different one |
| `schema.linking.rbac.enable` | Documented for RBAC only; support in ACL mode needs confirming (Q1) | Documented |
| Authentication | Custom SQL Server login module (works) | MDS authenticates via LDAP (or OAuth in newer releases); whether the SQL Server login module still fits is **unchecked** |

**Where MDS runs makes a difference.** With one central MDS ([Cluster Registry](https://docs.confluent.io/platform/7.8/security/cluster-registry.html)), all bindings are written in one place, but MDS itself has to survive a site loss. That means a cluster stretched over both sites, and there is no Confluent runbook for MDS failover. With one MDS per site, you write to both sides, exactly as in ACL mode.

RBAC fixes Q1 and removes `_schemas_acl`. It doesn't remove the need to write every permission to two places, and it brings in MDS and LDAP or OAuth. **It isn't worth switching for DR alone.**

## 4. Recommendation

1. **Keep:** Schema Linking for schemas, ACL mode, and `schema.linking.rbac.enable=true`. Get Q1 confirmed by Confluent before going live.
2. **Write ACLs to both sides:** the platform's single ACL writer (README recommendation 5) applies every change to the primary **and** the DR registry, and retries on failure until both sides succeed.
3. **Check for drift:** a periodic job compares `sr-acl-cli --list` (or the `_schemas_acl` contents) on both sides and alerts on any difference.
4. **At failover:** the ACL writer switches to the DR registry as its primary target. At failback, it replays to the old primary anything written during the outage. The drift check confirms the two match before the exporter is reversed.
5. **Don't use:** Cluster Linking for `_schemas` or `_schemas_acl` with a running DR registry. It can't work because of the startup no-op write.
6. **Optional fallback to test:** the stretched multi-DC design (§2). Consider it only if writing to both sides proves unreliable in practice.
