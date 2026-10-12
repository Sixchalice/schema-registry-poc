#!/usr/bin/env bash
# Seed: Postgres users, Schema Registry ACLs (sr-acl-cli), schemas registered by platform-admin.
source "$(dirname "$0")/lib.sh"

log "Postgres: platform principals and tenant Services"
psql_q "
INSERT INTO clusters VALUES ('c17') ON CONFLICT DO NOTHING;
INSERT INTO platform_principals VALUES ('$ADMIN_USER', '$ADMIN_PASS') ON CONFLICT DO NOTHING;
-- svc-a: plaintext password; svc-b: Jetty 'MD5:' form, to show both credential formats work
INSERT INTO services VALUES ('c17', 'svc-a', '$A_PASS') ON CONFLICT DO NOTHING;
INSERT INTO services VALUES ('c17', 'svc-b', 'MD5:' || md5('$B_PASS')) ON CONFLICT DO NOTHING;
" >/dev/null
psql_q "SELECT username, password FROM sr_auth_users ORDER BY 1" | sed 's/^/    /'

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
