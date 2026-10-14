#!/usr/bin/env bash
# Runs tests 1-10 against the running stack (scripts/up.sh + scripts/seed.sh first).
# Writes results/results.md (table), results/results.tsv, results/details.log, results/burst-*.txt
source "$(dirname "$0")/lib.sh"

mkdir -p results
RES=results/results.tsv
DETAIL=results/details.log
: > "$RES"; : > "$DETAIL"
RUN_ID=$(date +%s)

# ---------------------------------------------------------------- helpers (rec, check, get, body, wait_code, within: lib.sh)
restart_sr() { # extra-props
  log "Restarting Schema Registry with SR_EXTRA_PROPS='${1:-}'"
  SR_EXTRA_PROPS="${1:-}" dc up -d schema-registry >/dev/null 2>&1
  sleep 3; wait_for_http "$SR/subjects" 401 180
  until [[ "$(get "$ADMIN" "$SR/subjects")" == 200 ]]; do sleep 1; done   # ACL cache loaded
}

client() { # args... -> prints the RESULT line
  dc --profile client run --rm -T -e JAVA_TOOL_OPTIONS=-Dorg.slf4j.simpleLogger.defaultLogLevel=warn client "$@" \
    2>>"$DETAIL" | tee -a "$DETAIL" | grep '^RESULT' | tail -1 || true
}
ok_or_fail() { [[ "$1" == "RESULT OK"* ]] && echo OK || echo FAIL; }

ms_stats() { # xe-session -> "<logins> <queries>" counted by an Extended Events session from mssql/init.sql
  mssql_q "SELECT COALESCE(x.d.value('(//Event[@name=\"login\"]/@count)[1]', 'bigint'), 0),
      COALESCE(x.d.value('(//Event[@name=\"sql_batch_completed\"]/@count)[1]', 'bigint'), 0)
    + COALESCE(x.d.value('(//Event[@name=\"rpc_completed\"]/@count)[1]', 'bigint'), 0)
    FROM (SELECT CAST(t.target_data AS xml) AS d FROM sys.dm_xe_session_targets t
          JOIN sys.dm_xe_sessions s ON s.address = t.event_session_address WHERE s.name = '$1') x" master | tr -s ' \t' ' '
}
full_loads() { # how often the snapshot module has read the whole user view (plan cache)
  mssql_q "SELECT COALESCE(SUM(qs.execution_count),0) FROM sys.dm_exec_query_stats qs CROSS APPLY sys.dm_exec_sql_text(qs.sql_handle) t
    WHERE t.text = 'SELECT username, password FROM sr_auth_users'" master
}

lat_stats() { # file-of-"code seconds"-lines wall-ms -> one-line summary
  python3 - "$1" "$2" <<'PY'
import sys, collections
rows=[l.split() for l in open(sys.argv[1]) if l.strip()]
codes=collections.Counter(r[0] for r in rows); lat=sorted(float(r[1])*1000 for r in rows)
pct=lambda p: lat[min(len(lat)-1, int(round(p/100*len(lat)))-1)]
wall=int(sys.argv[2])/1000
print(f"codes {dict(codes)}, p50 {pct(50):.1f}ms, p99 {pct(99):.1f}ms, max {lat[-1]:.1f}ms, {len(rows)/wall:.0f} req/s")
PY
}

ORDERS_LATEST="$SR/subjects/orders-value/versions/latest"

[[ -f client/target/client.jar ]] || { log "Building Java client"; dc --profile client run --rm --entrypoint mvn client -q -B package >/dev/null; }

# Start from the shipped configuration
restart_sr ""

# ======================================================================== 1
log "Test 1: Authentication (in-memory snapshot of SQL Server)"
check 1a "No credentials"                    401 "$(get - "$ORDERS_LATEST")"
check 1b "c17.svc-a, wrong password"         401 "$(get "$A_USER:wrong" "$ORDERS_LATEST")"
check 1c "Unknown user"                      401 "$(get "c17.nobody:x" "$ORDERS_LATEST")"
check 1d "c17.svc-a, correct password (plaintext in DB)" 200 "$(get "$A" "$ORDERS_LATEST")"
check 1e "c17.svc-b, correct password (stored as Jetty MD5:)" 403 "$(get "$B" "$ORDERS_LATEST")" \
  "403 (authz) rather than 401 => authentication succeeded"

# ======================================================================== 2
log "Test 2: Authorization"
check 2a "c17.svc-a GET orders-value latest" 200 "$(get "$A" "$ORDERS_LATEST")"
check 2b "c17.svc-b GET orders-value latest" 403 "$(get "$B" "$ORDERS_LATEST")" "$(body)"
ORDER_V2=$(python3 -c 'import json;s=json.load(open("client/src/main/resources/order.avsc"));s["fields"].append({"name":"note","type":["null","string"],"default":None});print(json.dumps({"schema":json.dumps(s)}))')
check 2c "c17.svc-a register new version of orders-value" 403 "$(http_code "$A" POST "$SR/subjects/orders-value/versions" "$ORDER_V2")" "$(body)"
check 2d "c17.svc-a DELETE subject orders-value" 403 "$(http_code "$A" DELETE "$SR/subjects/orders-value")" "$(body)"
check 2e "c17.svc-a PUT compatibility on orders-value" 403 "$(http_code "$A" PUT "$SR/config/orders-value" '{"compatibility":"NONE"}')"
check 2f "c17.svc-a schema lookup POST /subjects/orders-value (used when auto.register=false)" 200 \
  "$(http_code "$A" POST "$SR/subjects/orders-value" "$(schema_body client/src/main/resources/order.avsc)")"
check 2g "platform-admin register schema (scratch subject)" 200 \
  "$(http_code "$ADMIN" POST "$SR/subjects/admin-scratch-value/versions" "$(schema_body client/src/main/resources/order.avsc)")"
check 2h "platform-admin soft-delete subject" 200 "$(http_code "$ADMIN" DELETE "$SR/subjects/admin-scratch-value")"
check 2i "platform-admin hard-delete subject" 200 "$(http_code "$ADMIN" DELETE "$SR/subjects/admin-scratch-value?permanent=true")"

# ======================================================================== 3
log "Test 3: Schema by ID"
get "$ADMIN" "$ORDERS_LATEST" >/dev/null; ORDERS_ID=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["id"])' "$LAST_BODY")
get "$ADMIN" "$SR/subjects/com.acme.payments.Payment/versions/latest" >/dev/null; PAY_ID=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["id"])' "$LAST_BODY")
check 3a "c17.svc-a GET /schemas/ids/$ORDERS_ID (orders-value)" 200 "$(get "$A" "$SR/schemas/ids/$ORDERS_ID")"
check 3b "c17.svc-b GET /schemas/ids/$ORDERS_ID" 403 "$(get "$B" "$SR/schemas/ids/$ORDERS_ID")" "$(body)"
check 3c "c17.svc-a GET /schemas/ids/$PAY_ID (only used by a subject it has no ACL on)" 403 "$(get "$A" "$SR/schemas/ids/$PAY_ID")"
code=$(get "$A" "$SR/schemas/ids/$ORDERS_ID/subjects"); rec 3d "c17.svc-a GET /schemas/ids/$ORDERS_ID/subjects" "200 [orders-value]" "$code $(body)" INFO "global endpoint: needs GLOBAL_READ (see test 4)"
code=$(get "$B" "$SR/schemas/ids/$ORDERS_ID/versions"); rec 3e "c17.svc-b GET /schemas/ids/$ORDERS_ID/versions" "403" "$code $(body)" "$([[ $code == 403 ]] && echo PASS || echo FAIL)"
code=$(get "$B" "$SR/schemas/ids/99999"); rec 3f "c17.svc-b GET non-existent id 99999" "403 or 404" "$code $(body)" INFO \
  "404 for unknown IDs vs 403 for existing ones reveals whether an ID exists (minor)"

# ======================================================================== 4
log "Test 4: Subject listing"
check 4a "c17.svc-b GET /subjects (no ACLs)" 403 "$(get "$B" "$SR/subjects")" "$(body); /subjects needs GLOBAL_READ"
check 4b "c17.svc-a GET /subjects (SUBJECT_READ on orders-*)" 403 "$(get "$A" "$SR/subjects")" "denied outright, not filtered, without GLOBAL_READ"
code=$(get "$ADMIN" "$SR/subjects"); check 4c "platform-admin GET /subjects" 200 "$code" "$(body)"
sracl --add -p "$A_USER" -o GLOBAL_READ >/dev/null; sracl --add -p "$B_USER" -o GLOBAL_READ >/dev/null; sleep 2
code=$(get "$A" "$SR/subjects"); check 4d "c17.svc-a GET /subjects with GLOBAL_READ added" '200 ["orders-value"]' "$code $(body)" "filter does NOT use SUBJECT_READ"
code=$(get "$A" "$SR/schemas");  check 4d2 "c17.svc-a GET /schemas with GLOBAL_READ added (filtered list)" '200 orders-value only' "$code $(python3 -c 'import json,sys;print(" ".join(sorted({x["subject"] for x in json.load(open(sys.argv[1]))})) + " only")' "$LAST_BODY")" "this list IS filtered by SUBJECT_READ"
sracl --add -s orders-value -p "$A_USER" -o SUBJECT_COMPATIBILITY_READ >/dev/null; sleep 2
code=$(get "$A" "$SR/subjects"); check 4d3 "c17.svc-a GET /subjects after adding SUBJECT_COMPATIBILITY_READ on orders-value" '200 ["orders-value"]' "$code $(body)" "/subjects filter requires SUBJECT_COMPATIBILITY_READ"
code=$(get "$A" "$SR/schemas/ids/$ORDERS_ID/subjects"); check 4d4 "c17.svc-a GET /schemas/ids/$ORDERS_ID/subjects (GLOBAL_READ + SUBJECT_COMPATIBILITY_READ)" '200 ["orders-value"]' "$code $(body)"
sracl --remove -s orders-value -p "$A_USER" -o SUBJECT_COMPATIBILITY_READ >/dev/null
code=$(get "$B" "$SR/subjects"); check 4e "c17.svc-b GET /subjects with GLOBAL_READ added" '200 []' "$code $(body)"
code=$(get "$A" "$SR/config");   rec 4f "c17.svc-a GET /config (global compatibility)" "403" "$code $(body)" "$([[ $code == 403 ]] && echo PASS || echo FAIL)" "needs GLOBAL_COMPATIBILITY_READ"
code=$(get "$A" "$SR/schemas/types"); rec 4g "c17.svc-a GET /schemas/types (with GLOBAL_READ)" "200" "$code $(body)" "$([[ $code == 200 ]] && echo PASS || echo FAIL)"
sracl --remove -p "$A_USER" -o GLOBAL_READ >/dev/null; sracl --remove -p "$B_USER" -o GLOBAL_READ >/dev/null; sleep 2
code=$(get "$B" "$SR/schemas/types"); rec 4h "c17.svc-b GET /schemas/types (no GLOBAL_READ)" "403" "$code" "$([[ $code == 403 ]] && echo PASS || echo FAIL)" "also gated by GLOBAL_READ"

# ======================================================================== 5
# Permission checks only. Exporters created here point back at this registry WITHOUT credentials
# (they cannot write anything) and are paused + deleted immediately.
log "Test 5: Exporter (Schema Linking) endpoint permissions"
exp_body() { echo "{\"name\":\"$1\",\"contextType\":\"CUSTOM\",\"context\":\"poc\",\"subjects\":[\"orders-value\"],\"config\":{\"schema.registry.url\":\"http://schema-registry:8081\"}}"; }
admin_cleanup_exporters() {
  get "$ADMIN" "$SR/exporters" >/dev/null
  for e in $(python3 -c 'import json,sys;print(" ".join(json.load(open(sys.argv[1]))))' "$LAST_BODY" 2>/dev/null); do
    http_code "$ADMIN" PUT "$SR/exporters/$e/pause" >/dev/null; http_code "$ADMIN" DELETE "$SR/exporters/$e" >/dev/null
  done
}
exporter_matrix() { # phase-label rbac-flag expected-tenant-code
  local ph=$1 exp=$3 n=0
  admin_cleanup_exporters
  local target="platform-link-$RUN_ID-$ph"
  check "5${ph}0" "[$2] platform-admin create exporter" 200 "$(http_code "$ADMIN" POST "$SR/exporters" "$(exp_body "$target")")" "$(body)"
  for who in A B; do
    local cred=${!who} name; [[ $who == A ]] && name=$A_USER || name=$B_USER
    n=$((n+1)); check "5${ph}$n" "[$2] $name GET /exporters" "$exp" "$(get "$cred" "$SR/exporters")" "$(body)"
    n=$((n+1)); check "5${ph}$n" "[$2] $name POST /exporters (create)" "$exp" "$(http_code "$cred" POST "$SR/exporters" "$(exp_body "tenant-$name-$RUN_ID")")"
    n=$((n+1)); check "5${ph}$n" "[$2] $name GET platform exporter config" "$exp" "$(get "$cred" "$SR/exporters/$target/config")"
    n=$((n+1)); check "5${ph}$n" "[$2] $name PUT pause on platform exporter" "$exp" "$(http_code "$cred" PUT "$SR/exporters/$target/pause")"
  done
  check "5${ph}9" "[$2] c17.svc-b DELETE platform exporter" "$([[ $exp == 403 ]] && echo 403 || echo 204)" "$(http_code "$B" DELETE "$SR/exporters/$target")"
  admin_cleanup_exporters
  check "5${ph}x" "[$2] platform-admin pause+delete exporter" "204" "$(http_code "$ADMIN" POST "$SR/exporters" "$(exp_body "$target-2")" >/dev/null; http_code "$ADMIN" PUT "$SR/exporters/$target-2/pause" >/dev/null; http_code "$ADMIN" DELETE "$SR/exporters/$target-2")"
}
# A: Confluent default (schema.linking.rbac.enable=false). Secure expectation for tenants = 403.
restart_sr "schema.linking.rbac.enable=false"
exporter_matrix a "rbac.enable=false" 403
# B: shipped config (schema.linking.rbac.enable=true)
restart_sr ""
exporter_matrix b "rbac.enable=true" 403

# ======================================================================== 6
log "Test 6: Real Java clients (auto.register.schemas=false)"
r=$(client produce orders c17 svc-a "$A_PASS" topic);   check 6a "svc-a produce 'orders', TopicNameStrategy" OK "$(ok_or_fail "$r")" "$r"
r=$(client consume orders c17 svc-a "$A_PASS" topic);   check 6b "svc-a consume 'orders'" OK "$(ok_or_fail "$r")" "$r"
r=$(client consume orders c17 svc-b "$B_PASS" topic);   check 6c "svc-b consume 'orders' (Kafka read allowed, no SR ACL)" FAIL "$(ok_or_fail "$r")" "$r"
sracl --add -t payments -p "$A_USER" -o SUBJECT_READ >/dev/null; sleep 2
r=$(client produce payments c17 svc-a "$A_PASS" record); check 6d "svc-a produce 'payments', RecordNameStrategy, ACL only via --topic payments" FAIL "$(ok_or_fail "$r")" \
  "--topic grants payments-key/-value only; subject is com.acme.payments.Payment. $r"
sracl --add -s 'com.acme.payments.*' -p "$A_USER" -o SUBJECT_READ >/dev/null; sleep 2
r=$(client produce payments c17 svc-a "$A_PASS" record); check 6e "same, after sr-acl-cli -s 'com.acme.payments.*' (prefix ACL)" OK "$(ok_or_fail "$r")" "$r"
r=$(client consume payments c17 svc-a "$A_PASS" record); check 6f "svc-a consume 'payments' (RecordNameStrategy)" OK "$(ok_or_fail "$r")" "$r"
sracl --remove -s 'com.acme.payments.*' -p "$A_USER" -o SUBJECT_READ >/dev/null
sracl --remove -t payments -p "$A_USER" -o SUBJECT_READ >/dev/null

# ======================================================================== 7
log "Test 7: ACL propagation (sr-acl-cli -> effective in SR)"
# A background poller (every ~20ms) records when svc-b's access to orders-value flips.
measure_acl() { # add|remove want-code
  local flip=$(mktemp)
  ( while :; do c=$(curl -s -o /dev/null -w '%{http_code}' -u "$B" "$ORDERS_LATEST"); [[ $c == "$2" ]] && { now_ms > "$flip"; exit; }; done ) &
  local poller=$! t0 t1; t0=$(now_ms)
  sracl --$1 -s orders-value -p "$B_USER" -o SUBJECT_READ >/dev/null
  t1=$(now_ms)
  for _ in $(seq 300); do [[ -s $flip ]] && break; sleep 0.1; done
  kill $poller 2>/dev/null || true; wait $poller 2>/dev/null || true
  local t2; t2=$(cat "$flip" 2>/dev/null || echo -1)
  echo "$((t1-t0)) $((t2-t0)) $((t2-t1))"
}
for i in 1 2 3; do
  read -r cli eff post <<<"$(measure_acl add 200)"
  rec "7a$i" "--add #$i: CLI wall time / effective after CLI start (ms)" "effective before CLI returns" "$cli / $eff" "$([[ $eff -ge 0 && $post -le 0 ]] && echo PASS || echo FAIL)"
  read -r cli eff post <<<"$(measure_acl remove 403)"
  rec "7r$i" "--remove #$i: CLI wall time / effective after CLI start (ms)" "effective before CLI returns" "$cli / $eff" "$([[ $eff -ge 0 && $post -le 0 ]] && echo PASS || echo FAIL)"
done

# ======================================================================== 8
log "Test 8: Database load (Jetty's JDBCLoginModule vs the in-memory snapshot, both on SQL Server)"
dc --profile bench up -d schema-registry-noplugin schema-registry-jdbc >/dev/null 2>&1
wait_for_http "$SR_NOPLUGIN/subjects" 401 180; wait_for_http "$SR_JDBC/subjects" 401 180
until [[ "$(get "$ADMIN" "$SR_JDBC/subjects")" == 200 ]]; do sleep 1; done   # ACL cache loaded
JDBC_LATEST="$SR_JDBC/subjects/orders-value/versions/latest"
NOPLUGIN_LATEST="$SR_NOPLUGIN/subjects/orders-value/versions/latest"
per_request() { # id label creds url n  (JDBCLoginModule, login sr_jdbc)
  local s0 s1; s0=($(ms_stats sr_jdbc_activity))
  for _ in $(seq "$5"); do curl -s -o /dev/null ${3:+-u "$3"} "$4"; done
  s1=($(ms_stats sr_jdbc_activity))
  local q=$(( s1[1]-s0[1] )) c=$(( s1[0]-s0[0] ))
  rec "$1" "$2: DB work per request (n=$5)" "-" "$(python3 -c "print(f'queries {$q/$5:.2f}, new DB connections {$c/$5:.2f}')")" INFO
}
per_request 8a "JDBCLoginModule, valid creds" "$A" "$JDBC_LATEST" 20
per_request 8b "JDBCLoginModule, wrong password" "$A_USER:wrong" "$JDBC_LATEST" 20
per_request 8c "JDBCLoginModule, unknown user" "c17.nobody:x" "$JDBC_LATEST" 20
per_request 8d "JDBCLoginModule, no credentials" "" "$JDBC_LATEST" 20
# Keep-alive: 20 requests over one HTTP connection
s0=($(ms_stats sr_jdbc_activity))
curl -s -u "$A" $(for _ in $(seq 20); do printf -- '-o /dev/null %s ' "$JDBC_LATEST"; done)
s1=($(ms_stats sr_jdbc_activity))
rec 8e "JDBCLoginModule, 20 requests on ONE keep-alive HTTP connection" "-" "queries $(( s1[1]-s0[1] )), new DB connections $(( s1[0]-s0[0] ))" INFO
# Snapshot: valid, wrong-password and unknown-user requests
s0=($(ms_stats sr_auth_activity)); f0=$(full_loads); t0=$(now_ms)
for _ in $(seq 100); do
  curl -s -o /dev/null -u "$A" "$ORDERS_LATEST"; curl -s -o /dev/null -u "$A_USER:wrong" "$ORDERS_LATEST"; curl -s -o /dev/null -u "c17.nobody:x" "$ORDERS_LATEST"
done
t1=$(now_ms); s1=($(ms_stats sr_auth_activity)); f1=$(full_loads)
rec 8f "snapshot, 300 requests (100 valid, 100 wrong password, 100 unknown user)" "no per-request DB work" \
  "queries $(( s1[1]-s0[1] )), new DB connections $(( s1[0]-s0[0] )), full loads $(( f1-f0 )) in $(( (t1-t0)/1000 ))s" INFO \
  "only the background reload: 1 connection + 1 full load per 60s per node"

burst() { # id label url n conc xe-session
  local out="results/burst-$1.txt" s0 s1 t0 t1
  s0=($(ms_stats "$6")); t0=$(now_ms)
  seq "$4" | xargs -P "$5" -I{} curl -s -o /dev/null -w '%{http_code} %{time_total}\n' -u "$A" "$3" > "$out"
  t1=$(now_ms); s1=($(ms_stats "$6"))
  rec "$1" "$2 (n=$4, concurrency=$5)" "all 200" "$(lat_stats "$out" $((t1-t0))); DB queries $(( s1[1]-s0[1] )), DB conns $(( s1[0]-s0[0] ))" \
    "$([[ $(grep -vc '^200 ' "$out") == 0 ]] && echo PASS || echo FAIL)"
}
# Warm-up (not measured): JIT-compile each registry, the bench twins have only just started
for url in "$JDBC_LATEST" "$ORDERS_LATEST" "$NOPLUGIN_LATEST"; do seq 300 | xargs -P 10 -I{} curl -s -o /dev/null -u "$A" "$url"; done
burst 8g "burst, JDBCLoginModule"   "$JDBC_LATEST"     1000 1  sr_jdbc_activity
burst 8h "burst, snapshot"          "$ORDERS_LATEST"   1000 1  sr_auth_activity
burst 8i "burst, JDBCLoginModule"   "$JDBC_LATEST"     1000 10 sr_jdbc_activity
burst 8j "burst, snapshot"          "$ORDERS_LATEST"   1000 10 sr_auth_activity
burst 8k "burst, snapshot, plugin OFF" "$NOPLUGIN_LATEST" 1000 10 sr_auth_activity
burst 8l "burst, snapshot"          "$ORDERS_LATEST"   1000 50 sr_auth_activity
# Availability of the stock module while the database is down (the snapshot's behavior is test 10)
dc stop mssql >/dev/null 2>&1
code=$(get "$A" "$JDBC_LATEST"); rec 8m "JDBCLoginModule, valid creds while SQL Server is DOWN" "-" "$code $(body)" INFO "every request needs the DB"
mssql_up
ms=$(wait_code "$A" "$JDBC_LATEST" 200 30000)
rec 8n "JDBCLoginModule, valid creds after SQL Server is back" "200 within 30 s" "$ms ms after the database is ONLINE" "$(within "$ms" 30000)"
dc --profile bench stop schema-registry-noplugin schema-registry-jdbc >/dev/null 2>&1

# ======================================================================== 9
log "Test 9: sr-acl-cli internals"
cls=$(dc exec -T schema-registry grep -o 'io\.confluent[A-Za-z.]*SchemaRegistryAclCommand' /usr/bin/sr-acl-cli | head -1)
check 9a "Java class run by sr-acl-cli" io.confluent.kafka.schemaregistry.security.tools.SchemaRegistryAclCommand "$cls"
dc exec -T kafka kafka-console-consumer --bootstrap-server kafka:29092 --consumer.config /etc/kafka/poc/admin.properties \
  --topic _schemas_acl --from-beginning --property print.key=true --property key.separator=' | ' --timeout-ms 5000 \
  > results/schemas_acl-dump.txt 2>/dev/null || true
rec 9b "ACL record format on _schemas_acl" "-" "JSON key/value, $(grep -c SUBJECT_ACL results/schemas_acl-dump.txt) subject + $(grep -c GLOBAL_ACL results/schemas_acl-dump.txt) global records (results/schemas_acl-dump.txt)" INFO

# ======================================================================== 10
log "Test 10: In-memory login snapshot: freshness and SQL Server outages"
# Changes in SQL Server reach the snapshot on the next full reload (every 60s)
NEW="c17.svc-new:svc-new-secret"
mssql_q "INSERT INTO services VALUES ('c17', 'svc-new', 'svc-new-secret')"
ms=$(wait_code "$NEW" "$ORDERS_LATEST" 403 75000)
rec 10a "new user inserted in SQL Server -> accepted (403 = authenticated, no ACL)" "<= 61000 ms" "$ms ms" "$(within "$ms" 61000)"
mssql_q "UPDATE services SET scram_password = 'svc-new-rotated' WHERE scram_user = 'svc-new'"
ms=$(wait_code "c17.svc-new:svc-new-rotated" "$ORDERS_LATEST" 403 75000)
rec 10b "password changed -> new password accepted" "<= 61000 ms" "$ms ms" "$(within "$ms" 61000)" \
  "old password now: $(get "$NEW" "$ORDERS_LATEST")"
mssql_q "DELETE FROM services WHERE scram_user = 'svc-new'"
ms=$(wait_code "c17.svc-new:svc-new-rotated" "$ORDERS_LATEST" 401 75000)
rec 10c "user deleted -> rejected" "<= 61000 ms" "$ms ms" "$(within "$ms" 61000)"

# SQL Server down: the running registry keeps working; a restarting one refuses to start
dc stop mssql >/dev/null 2>&1
sleep 61   # at least one failed background reload
check 10d "valid creds while SQL Server is DOWN" 200 "$(get "$A" "$ORDERS_LATEST")"
check 10e "wrong password while SQL Server is DOWN" 401 "$(get "$A_USER:wrong" "$ORDERS_LATEST")"
cid=$(dc ps -aq schema-registry)
docker restart "$cid" >/dev/null   # plain docker: compose would start mssql as a dependency
until [[ "$(docker inspect -f '{{.State.Status}}' "$cid")" == exited ]]; do sleep 1; done
rec 10f "restart Schema Registry while SQL Server is DOWN" "exits, code 1" \
  "$(docker inspect -f '{{.State.Status}}, code {{.State.ExitCode}}' "$cid")" \
  "$([[ "$(docker inspect -f '{{.State.ExitCode}}' "$cid")" == 1 ]] && echo PASS || echo FAIL)" \
  "$(docker logs "$cid" 2>&1 | grep -o 'auth preflight FAILED.*' | tail -1 | cut -c1-120)"
mssql_up
restart_sr ""
check 10g "after SQL Server is back: start + valid creds" 200 "$(get "$A" "$ORDERS_LATEST")"

# ---------------------------------------------------------------- report
write_md results/results.md
echo
log "Summary: $(cut -f5 "$RES" | sort | uniq -c | tr '\n' ' ')"
log "Table written to results/results.md"
