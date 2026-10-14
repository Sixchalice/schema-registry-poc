#!/usr/bin/env bash
# Active-passive DR with Schema Linking (README §12). Separate from test.sh; run up.sh + seed.sh first.
# Starts schema-registry-dr (profile dr, :8084), exports the primary into it, then checks schema IDs,
# permissions on DR, IMPORT mode, failover and failback. Writes results/dr-results.md and .tsv.
# Everything it creates is removed at the end (KEEP_DR=1 leaves the DR registry running).
source "$(dirname "$0")/lib.sh"

mkdir -p results
RES=results/dr-results.tsv; : > "$RES"
RUN_ID=$(date +%s)
BS=kafka:29092; ADMIN_CFG=/etc/kafka/poc/admin.properties
DR_TOPICS="_schemas_dr _schemas_dr_acl _exporter_dr_configs _exporter_dr_states"

# The exporter's own login on the receiving registry. Confluent's docs ("ACLs for the schema exporter
# running user") list SUBJECT_WRITE, SUBJECT_COMPATIBILITY_WRITE and SUBJECT_COMPATIBILITY_READ. In ACL
# mode the exporter also needs SUBJECT_DELETE (it replays subject deletions) and the global
# GLOBAL_COMPATIBILITY_READ (it reads GET /mode first). Without either it stops in state PAUSED.
EXP_USER=sr-dr-exporter; EXP_PASS=sr-dr-exporter-secret; EXP="$EXP_USER:$EXP_PASS"
EXP_OPS=SUBJECT_WRITE:SUBJECT_COMPATIBILITY_WRITE:SUBJECT_COMPATIBILITY_READ:SUBJECT_DELETE
EXP_GLOBAL_OPS=GLOBAL_COMPATIBILITY_READ
SUBJ=dr-test-$RUN_ID-value              # registered on the primary
FO_SUBJ=dr-test-$RUN_ID-failover-value  # registered on DR after failover

sracl_dr() { dc exec -T schema-registry-dr sr-acl-cli --config /tmp/sr.properties "$@"; }
put()  { http_code "$1" PUT "$2" "${3:-}"; }
post() { http_code "$1" POST "$2" "$3"; }
avro() { # record-name -> registration body for a new, unique Avro schema
  python3 -c 'import json,sys; print(json.dumps({"schema": json.dumps({"type":"record","name":sys.argv[1],"fields":[{"name":"id","type":"string"}]})}))' "$1"
}
exp_body() { # name target-url user:pass -> exporter into the target's default context
  python3 -c 'import json,sys; print(json.dumps({"name":sys.argv[1],"contextType":"NONE","subjects":["*"],"config":{
    "schema.registry.url":sys.argv[2],"basic.auth.credentials.source":"USER_INFO","basic.auth.user.info":sys.argv[3]}}))' "$@"
}
fingerprint() { # registry -> sorted "subject/version=id" of all live schemas
  get "$ADMIN" "$1/schemas" >/dev/null
  python3 -c 'import json,sys
try: print(" ".join(sorted("%s/%s=%s" % (s["subject"], s["version"], s["id"]) for s in json.load(open(sys.argv[1])))))
except Exception: print("error")' "$LAST_BODY"
}
id_of() { # registry subject -> latest schema id (empty if none)
  get "$ADMIN" "$1/subjects/$2/versions/latest" >/dev/null
  python3 -c 'import json,sys
try: print(json.load(open(sys.argv[1])).get("id", ""))
except Exception: print("")' "$LAST_BODY"
}
max_id() { fingerprint "$1" | tr ' ' '\n' | sed 's/.*=//' | sort -n | tail -1; }
same_schemas() { [[ "$(fingerprint "$SR")" == "$(fingerprint "$SR_DR")" ]]; }
has_id()       { [[ "$(id_of "$1" "$2")" == "$3" ]]; }
compat_is()    { get "$ADMIN" "$1/config/$2" >/dev/null; grep -q "\"$3\"" "$LAST_BODY"; }
wait_for() { # timeout-ms predicate... -> elapsed ms, or "timeout"
  local t0; t0=$(now_ms)
  until "${@:2}"; do (( $(now_ms) - t0 > $1 )) && { echo timeout; return; }; sleep 0.5; done
  echo $(( $(now_ms) - t0 ))
}

remove_dr() {
  dc --profile dr rm -sf schema-registry-dr >/dev/null 2>&1
  for t in $DR_TOPICS; do
    kexec kafka-topics --bootstrap-server $BS --command-config $ADMIN_CFG --delete --if-exists --topic "$t" >/dev/null 2>&1
  done
  while kexec kafka-topics --bootstrap-server $BS --command-config $ADMIN_CFG --list 2>/dev/null | grep -qE '^_(schemas|exporter)_dr'; do sleep 1; done
}
cleanup() { # undo everything on the primary; remove the DR registry and its topics
  set +e
  log "Cleanup"
  for u in "$SR" "$SR_DR"; do for e in dr dr-back; do
    put "$ADMIN" "$u/exporters/$e/pause" >/dev/null; http_code "$ADMIN" DELETE "$u/exporters/$e" >/dev/null
  done; done
  put "$ADMIN" "$SR/mode" '{"mode":"READWRITE"}' >/dev/null
  get "$ADMIN" "$SR/subjects" >/dev/null
  for s in $(python3 -c 'import json,sys; print(" ".join(s for s in json.load(open(sys.argv[1])) if s.startswith("dr-test-")))' "$LAST_BODY" 2>/dev/null); do
    http_code "$ADMIN" DELETE "$SR/subjects/$s" >/dev/null; http_code "$ADMIN" DELETE "$SR/subjects/$s?permanent=true" >/dev/null
  done
  sracl --remove -s '*' -p "$EXP_USER" -o "$EXP_OPS" >/dev/null 2>&1
  sracl --remove -p "$EXP_USER" -o "$EXP_GLOBAL_OPS" >/dev/null 2>&1
  [[ -n "${KEEP_DR:-}" ]] || { remove_dr; mssql_q "DELETE FROM platform_principals WHERE username = '$EXP_USER'" >/dev/null; }
}

[[ "$(get "$ADMIN" "$SR/subjects")" == 200 ]] || { echo "Primary registry not ready: run scripts/up.sh and scripts/seed.sh first"; exit 1; }
log "Removing leftovers of earlier runs"
KEEP_DR= cleanup; set -e
trap cleanup EXIT

# ---------------------------------------------------------------- setup
log "SQL Server: exporter user $EXP_USER (the DR registry loads it at startup)"
mssql_q "IF NOT EXISTS (SELECT 1 FROM platform_principals WHERE username = '$EXP_USER') INSERT INTO platform_principals VALUES ('$EXP_USER', '$EXP_PASS')" >/dev/null
log "Starting schema-registry-dr (:8084, own group and topics, same image/login/plugin)"
dc --profile dr up -d schema-registry-dr >/dev/null 2>&1
wait_for_http "$SR_DR/subjects" 401 180
log "DR ACLs (applied separately: exporters don't copy ACLs)"
sracl_dr --add -s '*' -p "$ADMIN_USER" -o '*'      >/dev/null
sracl_dr --add -t orders -p "$A_USER" -o SUBJECT_READ >/dev/null
sracl_dr --add -s '*' -p "$EXP_USER" -o "$EXP_OPS"  >/dev/null
sracl_dr --add -p "$EXP_USER" -o "$EXP_GLOBAL_OPS"  >/dev/null
until [[ "$(get "$ADMIN" "$SR_DR/subjects")" == 200 ]]; do sleep 1; done

# ---------------------------------------------------------------- export
log "D: export primary -> DR (IMPORT mode, contextType NONE)"
check d1 "platform-admin sets DR to IMPORT" 200 "$(put "$ADMIN" "$SR_DR/mode" '{"mode":"IMPORT"}')" "$(body)"
check d2 "platform-admin creates exporter 'dr' on primary" 200 "$(post "$ADMIN" "$SR/exporters" "$(exp_body dr http://schema-registry-dr:8081 "$EXP")")" "$(body)"
ms=$(wait_for 90000 same_schemas)
n=$(fingerprint "$SR" | wc -w | tr -d ' ')
get "$ADMIN" "$SR/exporters/dr/status" >/dev/null
rec d3 "all primary schemas on DR with the same subject/version/ID" "<= 90000 ms" "$ms ms" "$(within "$ms" 90000)" \
  "$n schemas; exporter status $(body)"

log "D: permissions on DR"
OID=$(id_of "$SR" orders-value)
check d4 "c17.svc-a GET orders-value on DR" 200 "$(get "$A" "$SR_DR/subjects/orders-value/versions/latest")"
check d5 "c17.svc-a GET /schemas/ids/$OID on DR" 200 "$(get "$A" "$SR_DR/schemas/ids/$OID")"
check d6 "c17.svc-b GET orders-value on DR" 403 "$(get "$B" "$SR_DR/subjects/orders-value/versions/latest")"
check d7 "c17.svc-a registers on DR" 403 "$(post "$A" "$SR_DR/subjects/orders-value/versions" "$(avro DrTenant$RUN_ID)")"
check d8 "platform-admin registers on DR while IMPORT" 422 \
  "$(post "$ADMIN" "$SR_DR/subjects/dr-test-$RUN_ID-import-value/versions" "$(avro DrImport$RUN_ID)")" "$(body)"

log "D: changes on the primary"
check d9 "platform-admin registers $SUBJ on primary" 200 "$(post "$ADMIN" "$SR/subjects/$SUBJ/versions" "$(avro DrNew$RUN_ID)")"
NEW_ID=$(id_of "$SR" "$SUBJ")
ms=$(wait_for 60000 has_id "$SR_DR" "$SUBJ" "$NEW_ID")
rec d10 "new schema reaches DR with the same ID" "<= 60000 ms" "$ms ms" "$(within "$ms" 60000)" "id $NEW_ID"
put "$ADMIN" "$SR/config/$SUBJ" '{"compatibility":"NONE"}' >/dev/null
ms=$(wait_for 20000 compat_is "$SR_DR" "$SUBJ" NONE)
rec d11 "per-subject compatibility NONE reaches DR" "exported?" \
  "$([[ $ms == timeout ]] && echo "not after 20 s" || echo "$ms ms")" INFO "DR GET /config/$SUBJ: $(get "$ADMIN" "$SR_DR/config/$SUBJ"; echo -n ' '; body)"
sracl --add -s "$SUBJ" -p "$B_USER" -o SUBJECT_READ >/dev/null
ms=$(wait_code "$B" "$SR/subjects/$SUBJ/versions/latest" 200 10000)
check d12 "ACL added on primary: c17.svc-b reads $SUBJ there" 200 "$([[ $ms == timeout ]] && echo timeout || echo 200)"
sleep 5
check d13 "same ACL on DR: c17.svc-b reads $SUBJ there" 403 "$(get "$B" "$SR_DR/subjects/$SUBJ/versions/latest")" "ACLs are not exported"
sracl --remove -s "$SUBJ" -p "$B_USER" -o SUBJECT_READ >/dev/null

# ---------------------------------------------------------------- failover
log "D: failover (primary assumed lost)"
check d14 "pause exporter 'dr'" 200 "$(put "$ADMIN" "$SR/exporters/dr/pause")" "$(body)"
check d15 "DR to READWRITE" 200 "$(put "$ADMIN" "$SR_DR/mode" '{"mode":"READWRITE"}')" "$(body)"
check d16 "platform-admin registers $FO_SUBJ on DR" 200 "$(post "$ADMIN" "$SR_DR/subjects/$FO_SUBJ/versions" "$(avro DrFailover$RUN_ID)")" "$(body)"
FO_ID=$(id_of "$SR_DR" "$FO_SUBJ")
check d17 "the new DR ID is unused on the primary" 404 "$(get "$ADMIN" "$SR/schemas/ids/$FO_ID")" \
  "DR assigned $FO_ID; primary max ID $(max_id "$SR")"

# ---------------------------------------------------------------- failback
log "D: failback (Confluent's Schema Linking DR steps)"
sracl --add -s '*' -p "$EXP_USER" -o "$EXP_OPS" >/dev/null   # the exporter user now writes into the primary
sracl --add -p "$EXP_USER" -o "$EXP_GLOBAL_OPS" >/dev/null
log "Waiting for the primary to load $EXP_USER (next 60 s reload)"
until [[ "$(get "$EXP" "$SR/subjects/orders-value/versions/latest")" != 401 ]]; do sleep 1; done
check d18 "primary to IMPORT (force)" 200 "$(put "$ADMIN" "$SR/mode?force=true" '{"mode":"IMPORT"}')" "$(body)"
check d19 "platform-admin creates exporter 'dr-back' on DR" 200 "$(post "$ADMIN" "$SR_DR/exporters" "$(exp_body dr-back http://schema-registry:8081 "$EXP")")" "$(body)"
ms=$(wait_for 60000 has_id "$SR" "$FO_SUBJ" "$FO_ID")
rec d20 "schema registered during the outage reaches the primary, same ID" "<= 60000 ms" "$ms ms" "$(within "$ms" 60000)" "id $FO_ID"
codes="$(put "$ADMIN" "$SR_DR/exporters/dr-back/pause") $(http_code "$ADMIN" DELETE "$SR_DR/exporters/dr-back")"
codes+=" $(put "$ADMIN" "$SR/mode" '{"mode":"READWRITE"}') $(put "$ADMIN" "$SR_DR/mode?force=true" '{"mode":"IMPORT"}')"
codes+=" $(put "$ADMIN" "$SR/exporters/dr/resume")"
check d21 "swap back: pause+delete dr-back, primary READWRITE, DR IMPORT, resume dr" "200 204 200 200 200" "$codes" "$(body)"
check d22 "platform-admin registers on primary after failback" 200 "$(post "$ADMIN" "$SR/subjects/$SUBJ/versions" "$(avro DrBack$RUN_ID)")" "$(body)"
ms=$(wait_for 60000 same_schemas)
rec d23 "primary and DR hold identical schemas again" "<= 60000 ms" "$ms ms" "$(within "$ms" 60000)"

# ---------------------------------------------------------------- report
write_md results/dr-results.md
echo
log "Summary: $(cut -f5 "$RES" | sort | uniq -c | tr '\n' ' ')"
log "Table written to results/dr-results.md"
