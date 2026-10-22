#!/usr/bin/env bash
# Start Kafka + SQL Server, bootstrap Kafka-side security (SCRAM users, Kafka ACLs, topics), then Schema Registry.
source "$(dirname "$0")/lib.sh"

log "Building Schema Registry image (adds the SQL Server JDBC driver) and the ACL REST service"
dc build -q schema-registry acl-service

log "Building the snapshot login module (auth/ -> auth/target/sr-auth-snapshot.jar)"
dc run --rm -T -v "$ROOT/auth:/auth" -w /auth --entrypoint mvn client -q -B package

log "Starting Kafka (cp-server 7.9, KRaft, SASL_PLAINTEXT/SCRAM-SHA-256, ACLs) and SQL Server"
dc up -d --wait kafka mssql

if [[ "$(mssql_q "SELECT COUNT(*) FROM sys.databases WHERE name = 'platform'" master)" == 0 ]]; then
  log "SQL Server: creating the platform database (mssql/init.sql)"
  dc exec -T mssql /opt/mssql-tools18/bin/sqlcmd -C -S localhost -U sa -P "$MSSQL_SA_PASS" -b -i /etc/mssql-poc/init.sql >/dev/null
fi

ADMIN_CFG=/etc/kafka/poc/admin.properties
BS=kafka:29092

log "Creating SCRAM-SHA-256 users"
for u in "schema-registry:schema-registry-secret" "platform-admin:platform-admin-secret" "svc-a:svc-a-secret" "svc-b:svc-b-secret"; do
  kexec kafka-configs --bootstrap-server $BS --command-config $ADMIN_CFG --alter \
    --entity-type users --entity-name "${u%%:*}" --add-config "SCRAM-SHA-256=[password=${u#*:}]" >/dev/null
done

acl() { kexec kafka-acls --bootstrap-server $BS --command-config $ADMIN_CFG --add --force "$@" >/dev/null; }

log "Kafka ACLs for the Schema Registry principal"
# _schemas and _schemas_acl (prefix), exporter topics, license topics
for t in _schemas _exporter _confluent-license _confluent-command; do
  acl --allow-principal User:schema-registry --resource-pattern-type prefixed --topic "$t" \
    --operation Read --operation Write --operation Describe --operation DescribeConfigs --operation Create
done
# SR leader-election group, kafkastore reader, ACL reader, license consumer groups
for g in schema-registry sr- confluent-license _confluent-license; do
  acl --allow-principal User:schema-registry --resource-pattern-type prefixed --group "$g" --operation Read --operation Describe
done
acl --allow-principal User:schema-registry --cluster --operation Describe --operation DescribeConfigs

log "Tenant topics + Kafka ACLs (svc-a: read/write orders,payments; svc-b: read orders only)"
for t in orders payments; do
  kexec kafka-topics --bootstrap-server $BS --command-config $ADMIN_CFG --create --if-not-exists --topic $t --partitions 1 --replication-factor 1 >/dev/null
  acl --allow-principal User:svc-a --topic $t --operation Read --operation Write --operation Describe
done
acl --allow-principal User:svc-a --resource-pattern-type prefixed --group c17.svc-a --operation Read
# svc-b may read the Kafka topic so test 6 isolates the Schema Registry denial (synthetic; see README)
acl --allow-principal User:svc-b --topic orders --operation Read --operation Describe
acl --allow-principal User:svc-b --resource-pattern-type prefixed --group c17.svc-b --operation Read

log "Starting Schema Registry (security plugin, ACL authorizer, in-memory login from SQL Server)"
dc up -d schema-registry acl-service
wait_for_http "$SR/subjects" 401 180
wait_for_http "http://localhost:8085/health" 200 60
log "Schema Registry is up (unauthenticated request -> 401). Next: scripts/seed.sh"
