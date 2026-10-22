#!/usr/bin/env bash
# Tests for the ACL REST service (acl-service/, :8085). Separate from test.sh; run up.sh + seed.sh first.
# Checks auth, add/remove semantics against sr-acl-cli, validation, that Schema Registry enforces what
# the service writes, CLI -> service visibility, serialized concurrent writes and a service restart.
# Writes results/acl-service-results.md and .tsv. ACLs it creates are removed at the end (as with the
# CLI, removal leaves records with empty operation sets on _schemas_acl).
source "$(dirname "$0")/lib.sh"

mkdir -p results
RES=results/acl-service-results.tsv; : > "$RES"
RUN_ID=$(date +%s)
ACL_SVC=http://localhost:8085
SVC_CREDS=acl-admin:acl-admin-secret
P=acl-test-$RUN_ID            # principal used by the service tests
P_CLI=acl-test-cli-$RUN_ID    # principal written with sr-acl-cli
T=acltest$RUN_ID              # topic -> subjects $T-key, $T-value
S=acltest-$RUN_ID-s           # plain subject
S_RACE=acltest-$RUN_ID-race   # subject for the concurrency test

svc() { http_code "${4:-$SVC_CREDS}" "$1" "$ACL_SVC$2" "${3:-}"; } # method path [body] [creds]
req() { # principals subjects topics operations (comma-separated, "" = omit) -> JSON body
  python3 -c 'import json,sys
keys = ["principals","subjects","topics","operations"]
print(json.dumps({k: v.split(",") for k, v in zip(keys, sys.argv[1:]) if v}))' "$@"
}
ops() { # principal [subject] -> operations from GET /acls (comma-joined, "-" if no ACL)
  svc GET "/acls?principal=$1" >/dev/null
  python3 -c 'import json,sys
d = json.load(open(sys.argv[1])); p, s = sys.argv[2], sys.argv[3]
rows = [a for a in d["globalAcls"]] if not s else [a for a in d["subjectAcls"] if a["subject"] == s]
print(",".join(rows[0]["operations"]) if rows else "-")' "$LAST_BODY" "$1" "${2:-}"
}
body_ops() { # subject|"" -> operations of the first row for it in the last response body
  python3 -c 'import json,sys
d = json.load(open(sys.argv[1])); s = sys.argv[2]
rows = d["globalAcls"] if not s else [a for a in d["subjectAcls"] if a["subject"] == s]
print(",".join(rows[0]["operations"]) if rows else "-")' "$LAST_BODY" "$1"
}
normalized_service() { # all ACLs from the service, one sorted line per (principal, subject|GLOBAL)
  svc GET /acls >/dev/null
  python3 -c 'import json,sys
d = json.load(open(sys.argv[1]))
rows = ["%s|%s|%s" % (a["principal"], a["subject"], ",".join(sorted(a["operations"]))) for a in d["subjectAcls"]]
rows += ["%s|GLOBAL|%s" % (a["principal"], ",".join(sorted(a["operations"]))) for a in d["globalAcls"]]
print(" ".join(sorted(rows)))' "$LAST_BODY"
}
normalized_cli() { # same, parsed from sr-acl-cli --list (empty operation sets left out, as the service does)
  sracl --list 2>/dev/null | python3 -c 'import re,sys
lines = [l.strip() for l in sys.stdin if l.strip().startswith("{")]
rows = []
if lines:
  for p, inner in re.findall(r"([^{},=\s]+)=\{([^{}]*)\}", lines[0]):
    for s, o in re.findall(r"([^=,\s]+)=\[([^\]]*)\]", inner):
      if o.strip(): rows.append("%s|%s|%s" % (p, s, ",".join(sorted(x.strip() for x in o.split(",")))))
if len(lines) > 1:
  for p, o in re.findall(r"([^{},=\s]+)=\[([^\]]*)\]", lines[1]):
    if o.strip(): rows.append("%s|GLOBAL|%s" % (p, ",".join(sorted(x.strip() for x in o.split(",")))))
print(" ".join(sorted(rows)))'
}
wait_ops() { # timeout-ms principal subject want -> elapsed ms, or "timeout"
  local t0; t0=$(now_ms)
  until [[ "$(ops "$2" "$3")" == "$4" ]]; do (( $(now_ms) - t0 > $1 )) && { echo timeout; return; }; sleep 0.1; done
  echo $(( $(now_ms) - t0 ))
}

cleanup() {
  svc DELETE /acls "$(req "$P" "$T-key,$T-value,$S,$S_RACE" "" '*')" >/dev/null || true
  svc DELETE /acls "$(req "$P,$P_CLI" "" "" GLOBAL_COMPATIBILITY_READ,GLOBAL_COMPATIBILITY_WRITE,GLOBAL_READ)" >/dev/null || true
  svc DELETE /acls "$(req "$B_USER" "" orders SUBJECT_READ)" >/dev/null || true
}
trap cleanup EXIT

wait_for_http "$ACL_SVC/health" 200 60

# ---------------------------------------------------------------- auth and routing
log "A: auth and routing"
check a1 "GET /health without credentials" 200 "$(svc GET /health "" -)"
check a2 "GET /acls without credentials" 401 "$(svc GET /acls "" -)"
check a3 "GET /acls with a wrong password" 401 "$(svc GET /acls "" acl-admin:wrong)"
check a4 "GET /acls as a Schema Registry user (not the service admin)" 401 "$(svc GET /acls "" "$ADMIN")"
check a5 "unknown route" 404 "$(svc GET /nope)"
check a6 "unsupported method on /acls" 404 "$(svc PUT /acls '{}')"
svc GET /operations >/dev/null
check a7 "GET /operations lists subject and global operations" \
  "SUBJECT_READ,SUBJECT_WRITE,SUBJECT_DELETE,SUBJECT_COMPATIBILITY_READ,SUBJECT_COMPATIBILITY_WRITE GLOBAL_COMPATIBILITY_READ,GLOBAL_COMPATIBILITY_WRITE,GLOBAL_READ" \
  "$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(",".join(d["subject"]), ",".join(d["global"]))' "$LAST_BODY")"

# ---------------------------------------------------------------- list matches the CLI
log "A: GET /acls matches sr-acl-cli --list"
check a8 "service and CLI list the same ACLs" "$(normalized_cli)" "$(normalized_service)"

# ---------------------------------------------------------------- add/remove semantics
log "A: add/remove semantics (same as sr-acl-cli --add/--remove)"
check a9 "add SUBJECT_READ on topic $T" 200 "$(svc POST /acls "$(req "$P" "" "$T" SUBJECT_READ)")" "$(body)"
check a10 "topic $T means subjects $T-key and $T-value" "SUBJECT_READ SUBJECT_READ" "$(ops "$P" "$T-key") $(ops "$P" "$T-value")"
check a11 "CLI sees the service's ACLs" "$(normalized_service)" "$(normalized_cli)"
svc POST /acls "$(req "$P" "$S" "" SUBJECT_READ)" >/dev/null
check a12 "add merges into the existing set" "200 SUBJECT_READ,SUBJECT_WRITE" \
  "$(svc POST /acls "$(req "$P" "$S" "" SUBJECT_WRITE)") $(body_ops "$S")"
check a13 "add is idempotent" "200 SUBJECT_READ,SUBJECT_WRITE" \
  "$(svc POST /acls "$(req "$P" "$S" "" SUBJECT_WRITE)") $(body_ops "$S")"
check a14 "remove takes out only the given operation" "200 SUBJECT_READ" \
  "$(svc DELETE /acls "$(req "$P" "$S" "" SUBJECT_WRITE)") $(body_ops "$S")"
check a15 "operation names are case-insensitive" "200 SUBJECT_READ,SUBJECT_DELETE" \
  "$(svc POST /acls "$(req "$P" "$S" "" subject_delete)") $(body_ops "$S")"
check a16 "global operation, alias GLOBAL_SUBJECTS_READ -> GLOBAL_READ" "200 GLOBAL_READ" \
  "$(svc POST /acls "$(req "$P" "" "" GLOBAL_SUBJECTS_READ)") $(ops "$P")"
check a17 "'*' operations on a subject grant every subject and global operation" \
  "200 SUBJECT_READ,SUBJECT_WRITE,SUBJECT_DELETE,SUBJECT_COMPATIBILITY_READ,SUBJECT_COMPATIBILITY_WRITE GLOBAL_COMPATIBILITY_READ,GLOBAL_COMPATIBILITY_WRITE,GLOBAL_READ" \
  "$(svc POST /acls "$(req "$P" "$S" "" '*')") $(ops "$P" "$S") $(ops "$P")"
svc GET "/acls?principal=$P&subject=$S" >/dev/null
check a18 "filter by principal and subject (no global rows)" "1 0" \
  "$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(len(d["subjectAcls"]), len(d["globalAcls"]))' "$LAST_BODY")"
check a19 "removing every operation hides the ACL from the list" "200 -" \
  "$(svc DELETE /acls "$(req "$P" "$S" "" '*')") $(ops "$P" "$S")"
check a20 "remove on a principal without ACLs is a no-op" "200 -" \
  "$(svc DELETE /acls "$(req "nobody-$RUN_ID" "$S" "" SUBJECT_READ)") $(ops "nobody-$RUN_ID" "$S")"

# ---------------------------------------------------------------- validation (sr-acl-cli's rules)
log "A: validation"
check a21 "no principals" 400 "$(svc POST /acls "$(req "" "$S" "" SUBJECT_READ)")" "$(body)"
check a22 "no operations" 400 "$(svc POST /acls "$(req "$P" "$S" "" "")")" "$(body)"
check a23 "subject operation without subject or topic" 400 "$(svc POST /acls "$(req "$P" "" "" SUBJECT_READ)")" "$(body)"
check a24 "global-only operation with a subject" 400 "$(svc POST /acls "$(req "$P" "$S" "" GLOBAL_READ)")" "$(body)"
check a25 "unknown operation" 400 "$(svc POST /acls "$(req "$P" "$S" "" NOPE)")" "$(body)"
check a26 "operation the CLI can't set (EXPORTER_ACCESS)" 400 "$(svc POST /acls "$(req "$P" "$S" "" EXPORTER_ACCESS)")" "$(body)"
check a27 "blank principal" 400 "$(svc POST /acls '{"principals":[" "],"subjects":["x"],"operations":["SUBJECT_READ"]}')" "$(body)"
check a28 "unknown JSON field" 400 "$(svc POST /acls '{"principals":["x"],"subject":["x"],"operations":["SUBJECT_READ"]}')" "$(body)"
check a29 "malformed JSON" 400 "$(svc POST /acls 'not json')" "$(body)"
check a30 "rejected requests wrote nothing" "-" "$(ops "$P" "$S")"

# ---------------------------------------------------------------- enforcement by Schema Registry
log "A: Schema Registry enforces what the service writes"
URL="$SR/subjects/orders-value/versions/latest"
check a31 "precondition: $B_USER can't read orders-value" 403 "$(get "$B" "$URL")" "$(body)"
check a32 "grant $B_USER SUBJECT_READ on topic orders" 200 "$(svc POST /acls "$(req "$B_USER" "" orders SUBJECT_READ)")" "$(body)"
ms=$(wait_code "$B" "$URL" 200 10000)
rec a33 "$B_USER can read orders-value" "200 within 10000 ms" "$ms ms" "$(within "$ms" 10000)"
check a34 "revoke it" 200 "$(svc DELETE /acls "$(req "$B_USER" "" orders SUBJECT_READ)")" "$(body)"
ms=$(wait_code "$B" "$URL" 403 10000)
rec a35 "$B_USER is denied again" "403 within 10000 ms" "$ms ms" "$(within "$ms" 10000)"

# ---------------------------------------------------------------- CLI -> service
log "A: changes made with sr-acl-cli show up in the service"
sracl --add -p "$P_CLI" -o GLOBAL_READ >/dev/null 2>&1
ms=$(wait_ops 10000 "$P_CLI" "" GLOBAL_READ)
rec a36 "CLI --add visible in GET /acls" "<= 10000 ms" "$ms ms" "$(within "$ms" 10000)"
check a37 "service add merges with the CLI's grant" "200 GLOBAL_COMPATIBILITY_READ,GLOBAL_READ" \
  "$(svc POST /acls "$(req "$P_CLI" "" "" GLOBAL_COMPATIBILITY_READ)") $(ops "$P_CLI")"
sracl --remove -p "$P_CLI" -o GLOBAL_READ:GLOBAL_COMPATIBILITY_READ >/dev/null 2>&1
ms=$(wait_ops 10000 "$P_CLI" "" -)
rec a38 "CLI --remove visible in GET /acls" "<= 10000 ms" "$ms ms" "$(within "$ms" 10000)"

# ---------------------------------------------------------------- concurrency
log "A: concurrent adds on one key are serialized (no lost update)"
pids=()
for op in SUBJECT_READ SUBJECT_WRITE SUBJECT_DELETE SUBJECT_COMPATIBILITY_READ SUBJECT_COMPATIBILITY_WRITE; do
  curl -s -o /dev/null -u "$SVC_CREDS" -X POST --data "$(req "$P" "$S_RACE" "" "$op")" "$ACL_SVC/acls" & pids+=($!)
done
wait "${pids[@]}"
check a39 "5 parallel single-operation adds -> all 5 operations" \
  "SUBJECT_READ,SUBJECT_WRITE,SUBJECT_DELETE,SUBJECT_COMPATIBILITY_READ,SUBJECT_COMPATIBILITY_WRITE" "$(ops "$P" "$S_RACE")"

# ---------------------------------------------------------------- restart
log "A: ACLs survive a service restart (state is the Kafka topic)"
before=$(normalized_service)
dc restart acl-service >/dev/null 2>&1
t0=$(now_ms); wait_for_http "$ACL_SVC/health" 200 60 >/dev/null
rec a40 "service healthy again after restart" "<= 60000 ms" "$(( $(now_ms) - t0 )) ms" PASS
check a41 "same ACLs after restart" "$before" "$(normalized_service)"

# ---------------------------------------------------------------- report
write_md results/acl-service-results.md
echo
log "Summary: $(cut -f5 "$RES" | sort | uniq -c | tr '\n' ' ')"
log "Table written to results/acl-service-results.md"
