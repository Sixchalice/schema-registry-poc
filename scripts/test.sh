#!/usr/bin/env bash
# Runs tests 1-9 against the running stack (scripts/up.sh + scripts/seed.sh first).
# Writes results/results.md (table), results/results.tsv, results/details.log, results/burst-*.txt
source "$(dirname "$0")/lib.sh"

mkdir -p results
RES=results/results.tsv
DETAIL=results/details.log
: > "$RES"; : > "$DETAIL"
RUN_ID=$(date +%s)

# ---------------------------------------------------------------- helpers
rec() { # id title expected actual verdict [notes]
  printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$1" "$2" "$3" "$4" "$5" "${6:-}" >> "$RES"
  local c='\033[1;32m'; [[ "$5" == FAIL ]] && c='\033[1;31m'; [[ "$5" == INFO ]] && c='\033[1;33m'
  printf "  ${c}%-4s\033[0m %-4s %s | expected: %s | actual: %s\n" "$5" "$1" "$2" "$3" "$4"
}
check() { # id title expected actual [notes] -- PASS iff actual == expected
  local v=FAIL; [[ "$3" == "$4" ]] && v=PASS
  rec "$1" "$2" "$3" "$4" "$v" "${5:-}"
}
get()  { http_code "$1" GET "$2"; }
body() { tr -d '\n' < "$LAST_BODY" | tr '\t' ' ' | cut -c1-150; }

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

pg_auth_queries() { # -> "<user-queries> <role-queries>" run by the sr_auth DB role
  psql_q "SELECT coalesce(sum(calls) FILTER (WHERE query ILIKE '%from sr_auth_users%'),0) || ' ' ||
                 coalesce(sum(calls) FILTER (WHERE query ILIKE '%from sr_auth_roles%'),0)
          FROM pg_stat_statements WHERE userid = (SELECT oid FROM pg_roles WHERE rolname='sr_auth')"
}
pg_auth_conns() { dc logs --no-log-prefix postgres 2>&1 | grep -c 'connection authorized: user=sr_auth' || true; }

ADMIN="$ADMIN_USER:$ADMIN_PASS"; A="$A_USER:$A_PASS"; B="$B_USER:$B_PASS"
ORDERS_LATEST="$SR/subjects/orders-value/versions/latest"

[[ -f client/target/client.jar ]] || { log "Building Java client"; dc --profile client run --rm --entrypoint mvn client -q -B package >/dev/null; }

# Start from the shipped configuration
restart_sr ""

# ======================================================================== 1
log "Test 1: Authentication (Jetty JDBCLoginModule -> Postgres)"
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
log "Test 8: Database load"
dc --profile bench up -d schema-registry-noplugin >/dev/null 2>&1; wait_for_http "$SR_NOPLUGIN/subjects" 401 180
per_request() { # id label creds url n
  local q0 c0 q1 c1; q0=($(pg_auth_queries)); c0=$(pg_auth_conns)
  for _ in $(seq "$5"); do curl -s -o /dev/null ${3:+-u "$3"} "$4"; done
  sleep 1; q1=($(pg_auth_queries)); c1=$(pg_auth_conns)
  local uq=$(( q1[0]-q0[0] )) rq=$(( q1[1]-q0[1] )) cn=$(( c1-c0 ))
  rec "$1" "$2: DB work per request (n=$5)" "-" "$(python3 -c "print(f'user-queries {$uq/$5:.2f}, role-queries {$rq/$5:.2f}, new DB connections {$cn/$5:.2f}')")" INFO
}
per_request 8a "valid creds (svc-a), plugin ON" "$A" "$ORDERS_LATEST" 20
per_request 8b "wrong password" "$A_USER:wrong" "$ORDERS_LATEST" 20
per_request 8c "unknown user" "c17.nobody:x" "$ORDERS_LATEST" 20
per_request 8d "no credentials" "" "$ORDERS_LATEST" 20
per_request 8e "valid creds, plugin OFF" "$A" "$SR_NOPLUGIN/subjects/orders-value/versions/latest" 20
# Keep-alive: 20 requests over one HTTP connection
q0=($(pg_auth_queries)); c0=$(pg_auth_conns)
curl -s -u "$A" $(for _ in $(seq 20); do printf -- '-o /dev/null %s ' "$ORDERS_LATEST"; done); sleep 1
q1=($(pg_auth_queries)); c1=$(pg_auth_conns)
rec 8f "valid creds, 20 requests on ONE keep-alive HTTP connection" "-" "user-queries $(( q1[0]-q0[0] )), role-queries $(( q1[1]-q0[1] )), new DB connections $(( c1-c0 ))" INFO

burst() { # id label url n conc
  local out="results/burst-$1.txt" q0 c0 q1 c1 t0 t1
  q0=($(pg_auth_queries)); c0=$(pg_auth_conns); t0=$(now_ms)
  seq "$4" | xargs -P "$5" -I{} curl -s -o /dev/null -w '%{http_code} %{time_total}\n' -u "$A" "$3" > "$out"
  t1=$(now_ms); sleep 1; q1=($(pg_auth_queries)); c1=$(pg_auth_conns)
  local stats; stats=$(python3 - "$out" $((t1-t0)) <<'PY'
import sys, collections
rows=[l.split() for l in open(sys.argv[1]) if l.strip()]
codes=collections.Counter(r[0] for r in rows); lat=sorted(float(r[1])*1000 for r in rows)
pct=lambda p: lat[min(len(lat)-1, int(round(p/100*len(lat)))-1)]
wall=int(sys.argv[2])/1000
print(f"codes {dict(codes)}, p50 {pct(50):.1f}ms, p99 {pct(99):.1f}ms, max {lat[-1]:.1f}ms, {len(rows)/wall:.0f} req/s")
PY
)
  rec "$1" "$2 (n=$4, concurrency=$5)" "all 200" "$stats; DB user-q $(( q1[0]-q0[0] )), role-q $(( q1[1]-q0[1] )), DB conns $(( c1-c0 ))" \
    "$([[ $(grep -vc '^200 ' "$out") == 0 ]] && echo PASS || echo FAIL)"
}
burst 8g "burst, plugin ON"  "$ORDERS_LATEST" 1000 1
burst 8h "burst, plugin OFF" "$SR_NOPLUGIN/subjects/orders-value/versions/latest" 1000 1
burst 8i "burst, plugin ON"  "$ORDERS_LATEST" 1000 10
burst 8j "burst, plugin OFF" "$SR_NOPLUGIN/subjects/orders-value/versions/latest" 1000 10
burst 8k "burst, plugin ON"  "$ORDERS_LATEST" 1000 50
# Availability: what clients see while the auth database is down
dc stop postgres >/dev/null 2>&1
code=$(get "$A" "$ORDERS_LATEST"); rec 8l "valid creds while Postgres is DOWN" "-" "$code $(body)" INFO "every request needs the DB"
dc start postgres >/dev/null 2>&1; until dc exec -T postgres pg_isready -U platform >/dev/null 2>&1; do sleep 1; done; sleep 1
check 8m "valid creds after Postgres is back" 200 "$(get "$A" "$ORDERS_LATEST")"
dc --profile bench stop schema-registry-noplugin >/dev/null 2>&1

# ======================================================================== 9
log "Test 9: sr-acl-cli internals"
cls=$(dc exec -T schema-registry grep -o 'io\.confluent[A-Za-z.]*SchemaRegistryAclCommand' /usr/bin/sr-acl-cli | head -1)
check 9a "Java class run by sr-acl-cli" io.confluent.kafka.schemaregistry.security.tools.SchemaRegistryAclCommand "$cls"
dc exec -T kafka kafka-console-consumer --bootstrap-server kafka:29092 --consumer.config /etc/kafka/poc/admin.properties \
  --topic _schemas_acl --from-beginning --property print.key=true --property key.separator=' | ' --timeout-ms 5000 \
  > results/schemas_acl-dump.txt 2>/dev/null || true
rec 9b "ACL record format on _schemas_acl" "-" "JSON key/value, $(grep -c SUBJECT_ACL results/schemas_acl-dump.txt) subject + $(grep -c GLOBAL_ACL results/schemas_acl-dump.txt) global records (results/schemas_acl-dump.txt)" INFO

# ---------------------------------------------------------------- report
{
  echo "| # | Test | Expected | Actual | Result | Notes |"
  echo "|---|------|----------|--------|--------|-------|"
  awk -F'\t' '{ for (i=1;i<=6;i++) gsub(/\|/, "\\|", $i); printf "| %s | %s | %s | %s | **%s** | %s |\n", $1,$2,$3,$4,$5,$6 }' "$RES"
} > results/results.md
echo
log "Summary: $(cut -f5 "$RES" | sort | uniq -c | tr '\n' ' ')"
log "Table written to results/results.md"
