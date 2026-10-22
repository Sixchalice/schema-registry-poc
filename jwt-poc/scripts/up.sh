#!/usr/bin/env bash
# Start the bearer-token stack on top of the repo's Kafka + SQL Server, then seed it:
# keys, sr_services rows (bcrypt), SR ACLs (sr-acl-cli), and two schemas registered as `platform` via Envoy.
source "$(dirname "$0")/lib.sh"

if ! base ps --status running --services 2>/dev/null | grep -qx kafka; then
  log "Main stack's Kafka is not running: starting it with ../scripts/up.sh would also start its SR;"
  log "starting only kafka + mssql (they must have been bootstrapped by ../scripts/up.sh before)"
  base up -d --wait kafka mssql
fi
base up -d --wait mssql >/dev/null
until [[ "$(mssql_q "SELECT state_desc FROM sys.databases WHERE name = 'platform'" master 2>/dev/null)" == ONLINE ]]; do sleep 1; done

if [[ ! -f keys/private.pem ]]; then
  log "Generating the RS256 key pair (keys/, gitignored)"
  mkdir -p keys
  openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -out keys/private.pem 2>/dev/null
  openssl pkey -in keys/private.pem -pubout -out keys/public.pem
  chmod 644 keys/private.pem   # read by the auth-service and tests containers (POC only)
fi

log "Building auth-service and tests images"
dc build -q auth-service
dc --profile tests build -q tests

log "SQL Server: sr_services table + read-only login sr_jwt_auth"
base exec -T mssql /opt/mssql-tools18/bin/sqlcmd -C -S localhost -U sa -P "$MSSQL_SA_PASS" -b -i /dev/stdin >/dev/null < mssql/init.sql

log "SQL Server: upserting Services with bcrypt hashes"
sql=""
for cred in "$READER" "$OTHER" "$PLATFORM"; do
  name=${cred%%:*}
  hash=$(dc run --rm -T --no-deps --entrypoint python auth-service -c 'import bcrypt,sys; print(bcrypt.hashpw(sys.argv[1].encode(), bcrypt.gensalt()).decode())' "${cred#*:}")
  sql+="MERGE sr_services AS t USING (SELECT '$name' AS name, '$hash' AS h) AS s ON t.name = s.name
        WHEN MATCHED THEN UPDATE SET password_hash = s.h WHEN NOT MATCHED THEN INSERT VALUES (s.name, s.h);"
done
mssql_q "$sql"
mssql_q "SELECT name + ' | ' + password_hash FROM sr_services ORDER BY 1" | sed 's/^/    /'

log "Starting auth-service, sr-jwt-1 (leader), sr-jwt-2 (follower), envoy"
dc up -d --wait auth-service sr-jwt-1 sr-jwt-2 envoy

log "Schema Registry ACLs (sr-acl-cli on sr-jwt-1)"
sracl --add -s '*' -p platform -o '*' >/dev/null
sracl --add -s orders-value -p svc-reader -o SUBJECT_READ >/dev/null
sracl --list | sed 's/^/    /'

log "Registering schemas as platform via Envoy"
for s in orders-value payments-value; do
  for _ in $(seq 30); do   # ACLs reach SR through the _schemas_jwt_acl topic; retry until they apply
    code=$(curl -s -o /tmp/srjwt.out -w '%{http_code}' -u "$PLATFORM" -X POST \
      -H 'Content-Type: application/vnd.schemaregistry.v1+json' --data "$(schema_body schemas/$s.avsc)" \
      "$PROXY/subjects/$s/versions")
    [[ $code == 200 ]] && break; sleep 1
  done
  echo "    $s -> HTTP $code $(cat /tmp/srjwt.out)"
done
