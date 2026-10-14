#!/usr/bin/env bash
# Seed: SQL Server users, Schema Registry ACLs (sr-acl-cli), schemas registered by platform-admin.
source "$(dirname "$0")/lib.sh"

log "SQL Server: platform principals and tenant Services"
mssql_q "
IF NOT EXISTS (SELECT 1 FROM clusters WHERE cluster_id = 'c17') INSERT INTO clusters VALUES ('c17');
IF NOT EXISTS (SELECT 1 FROM platform_principals WHERE username = '$ADMIN_USER') INSERT INTO platform_principals VALUES ('$ADMIN_USER', '$ADMIN_PASS');
IF NOT EXISTS (SELECT 1 FROM services WHERE cluster_id = 'c17' AND scram_user = 'svc-a') INSERT INTO services VALUES ('c17', 'svc-a', '$A_PASS');
IF NOT EXISTS (SELECT 1 FROM services WHERE cluster_id = 'c17' AND scram_user = 'svc-b')
  INSERT INTO services VALUES ('c17', 'svc-b', 'MD5:' + LOWER(CONVERT(varchar(32), HASHBYTES('MD5', '$B_PASS'), 2)));
" >/dev/null
mssql_q "SELECT username + ' | ' + password FROM sr_auth_users ORDER BY 1" | sed 's/^/    /'
# Schema Registry picks up the new rows on its next full reload (every 60 s)
log "Waiting for Schema Registry to load the new users"
for _ in $(seq 75); do [[ "$(http_code "$ADMIN_USER:$ADMIN_PASS" GET "$SR/subjects")" != 401 ]] && break; sleep 1; done

log "Schema Registry ACLs via sr-acl-cli"
sracl --add -s '*' -p "$ADMIN_USER" -o '*'                >/dev/null
sracl --add -t orders -p "$A_USER" -o SUBJECT_READ        >/dev/null
sracl --list | sed 's/^/    /'

log "Registering schemas as $ADMIN_USER"
code=$(http_code "$ADMIN_USER:$ADMIN_PASS" POST "$SR/subjects/orders-value/versions" "$(schema_body client/src/main/resources/order.avsc)")
echo "    orders-value -> HTTP $code $(cat "$LAST_BODY")"
# Subject for RecordNameStrategy on topic 'payments' = fully-qualified record name
code=$(http_code "$ADMIN_USER:$ADMIN_PASS" POST "$SR/subjects/com.acme.payments.Payment/versions" "$(schema_body client/src/main/resources/payment.avsc)")
echo "    com.acme.payments.Payment -> HTTP $code $(cat "$LAST_BODY")"
