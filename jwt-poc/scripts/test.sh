#!/usr/bin/env bash
# Checks 1-13 (POC_REPORT.md) -> results/jwt-results.tsv/.md, results/jwt-principal-log.txt.
# pytest runs in the `tests` container in phases; this script stops/starts SQL Server and the auth service
# between them and adds the rows that need container logs (4b, 12b, 13b). Needs ./scripts/up.sh first.
source "$(dirname "$0")/lib.sh"

RES=results/jwt-results.tsv
MARKERS=results/markers.tsv
mkdir -p results
: > "$RES"; : > "$MARKERS"
START=$(date -u +%Y-%m-%dT%H:%M:%SZ)
FAILED=0

pytest_phase() { dc --profile tests run --rm -T tests -m "$1" test_poc.py || FAILED=1; }
rec() { # id title expected actual [notes]  -- PASS iff actual == expected
  local v=FAIL; [[ "$4" == "$3" ]] && v=PASS || FAILED=1
  printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$1" "$2" "$3" "$4" "$v" "${5:-}" >> "$RES"
}
marker() { awk -F'\t' -v id="$1" '$1 == id { print $2 }' "$MARKERS" | tail -1; }
sr_logs() { dc logs --no-log-prefix --since "$START" sr-jwt-1 sr-jwt-2 2>&1; }
hits()    { grep -c -- "$1" || true; }
upstream_of() { # marker -> Envoy access log upstream= for that request ("-" = never reached SR); waits for the log flush
  local line t0=$SECONDS
  until line=$(dc logs --no-log-prefix envoy | grep "^ACCESS .*$1") || (( SECONDS - t0 >= 15 )); do sleep 1; done
  sed -E 's/.*upstream=([^ ]*).*/\1/' <<< "$line" | tail -1
}

log "Phase main: checks 1-10, 13"
pytest_phase main

m=$(marker 4)
rec 4b "wrong-password request never reached SR (marker in SR request logs | Envoy upstream)" "0|-" \
  "$(sr_logs | hits "$m")|$(upstream_of "$m")" "$m"

m=$(marker 13)
leader=$(dc logs --no-log-prefix --since "$START" sr-jwt-1 | grep 'POST /subjects/jwt-forward-test-value/versions' | grep -c ' - platform \[' || true)
follower=$(dc logs --no-log-prefix --since "$START" sr-jwt-2 | grep 'POST /subjects/jwt-forward-test-value/versions' | grep -c ' - platform \[' || true)
rec 13b "forwarded registration logged as principal platform on follower | leader" "1|1" "$follower|$leader" \
  "request-log lines with user=platform; svc-reader's attempt was denied on the follower, never forwarded"

mssql_wait() { until [[ "$(mssql_q "SELECT state_desc FROM sys.databases WHERE name = 'platform'" master 2>/dev/null)" == ONLINE ]]; do sleep 1; done; }
proxy_code() { curl -s -o /dev/null -w '%{http_code}' -u "$1" "$PROXY/subjects"; }
wait_code() { # creds want-code timeout-s -> seconds until Envoy answered want-code, or "timeout"
  local t0=$SECONDS
  until [[ "$(proxy_code "$1")" == "$2" ]]; do (( SECONDS - t0 >= $3 )) && { echo timeout; return; }; sleep 1; done
  echo $(( SECONDS - t0 ))
}
within() { [[ "$1" != timeout ]] && (( $1 <= $2 )) && echo yes || echo "no ($1)"; }

log "Check 15: a Service added to / removed from sr_services takes effect on the next reload (<= 60 s)"
NEW=svc-new:svc-new-secret
hash=$(dc run --rm -T --no-deps --entrypoint python auth-service -c 'import bcrypt,sys; print(bcrypt.hashpw(sys.argv[1].encode(), bcrypt.gensalt()).decode())' "${NEW#*:}")
mssql_q "DELETE FROM sr_services WHERE name = 'svc-new'; INSERT INTO sr_services VALUES ('svc-new', '$hash')"
added=$(wait_code "$NEW" 403 75)   # authenticated; no ACLs -> 403 from SR
mssql_q "DELETE FROM sr_services WHERE name = 'svc-new'"
removed=$(wait_code "$NEW" 401 75)
rec 15 "svc-new inserted -> authenticated (403 from SR, no ACLs) within 65 s | deleted -> 401 within 65 s (no restart)" "yes|yes" \
  "$(within "$added" 65)|$(within "$removed" 65)" "seconds: added $added, removed $removed (reload every 60 s)"

log "Check 11: stop SQL Server; the auth service keeps its last snapshot"
base stop mssql >/dev/null
pytest_phase db_down

log "Check 11b: the auth service refuses to start while SQL Server is down"
dc restart auth-service >/dev/null 2>&1 || true
sleep 5
state=$(docker inspect -f '{{.State.Status}}/{{.State.ExitCode}}' "$(dc ps -aq auth-service)")
reason=$(dc logs --no-log-prefix --since 30s auth-service 2>&1 | grep -m1 -oE 'OperationalError.{0,60}|Application startup failed.*' || true)
rec 11b "auth service restarted while SQL Server is down -> exits (no start without credentials)" exited/3 "$state" "$reason"

log "Starting SQL Server, then the auth service again"
base up -d --wait mssql >/dev/null
mssql_wait
dc up -d --wait auth-service >/dev/null

log "Check 12: stop the auth service"
dc stop auth-service >/dev/null
pytest_phase auth_down
m=$(marker 12)
rec 12b "with auth service down the request never reached SR (marker in SR request logs | Envoy upstream)" "0|-" \
  "$(sr_logs | hits "$m")|$(upstream_of "$m")" "$m"
dc up -d --wait auth-service >/dev/null
t0=$SECONDS
until [[ "$(curl -s -o /dev/null -w '%{http_code}' -u "$READER" "$PROXY/subjects/orders-value/versions/latest")" == 200 ]]; do
  (( SECONDS - t0 >= 30 )) && break; sleep 1
done
back=$(( SECONDS - t0 ))
rec 12c "auth service started again: svc-reader via Envoy gets 200 within 15 s" yes "$(within "$back" 15)" \
  "$back s after the container was healthy (STRICT_DNS cluster, re-resolved every 5 s)"

log "Principal log excerpts -> results/jwt-principal-log.txt"
{
  echo "# sr-jwt-1 / sr-jwt-2, this run (since $START)"
  dc logs --since "$START" sr-jwt-1 sr-jwt-2 2>&1 \
    | grep -E 'The principal is|Processing new Jwt login|denied operation|rest-utils.requests' \
    | grep -v '"GET /subjects HTTP/1.1" 401' | head -80 || true
  echo; echo "# startup: authentication handler configuration"
  dc logs sr-jwt-1 2>&1 | grep -E 'IDP issuer|IDP Oauth is enabled' | tail -2 || true
  echo; echo "# envoy access log, this run"
  dc logs --no-log-prefix --since "$START" envoy | grep ACCESS | head -60 || true
  echo; echo "# auth service, this run"
  dc logs --no-log-prefix --since "$START" auth-service 2>&1 | grep -E 'loaded|reload failed|startup failed' | head -20 || true
} > results/jwt-principal-log.txt

python3 - "$RES" results/jwt-results.md <<'EOF'
import sys
rows = [l.rstrip("\n").split("\t") for l in open(sys.argv[1]) if l.strip()]
key = lambda r: (int("".join(c for c in r[0] if c.isdigit())), r[0])
rows.sort(key=key)
esc = lambda s: s.replace("|", "\\|")
out = ["| # | Check | Expected | Actual | Result | Notes |", "|---|---|---|---|---|---|"]
out += ["| " + " | ".join(esc(c) for c in (r + [""] * 6)[:6]) + " |" for r in rows]
passed = sum(r[4] == "PASS" for r in rows)
open(sys.argv[2], "w").write(f"{passed}/{len(rows)} rows passed\n\n" + "\n".join(out) + "\n")
print(f"{passed}/{len(rows)} rows passed")
EOF
exit $FAILED
