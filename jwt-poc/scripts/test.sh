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

log "Phase main: checks 1-10, 13"
pytest_phase main

m=$(marker 4)
rec 4b "wrong-password request never reached SR (marker in SR request logs | NGINX upstream)" "0|-" \
  "$(sr_logs | hits "$m")|$(dc logs --no-log-prefix nginx | grep -- "$m" | sed -E 's/.*upstream=([^ ]*).*/\1/' | tail -1)" "$m"

m=$(marker 13)
leader=$(dc logs --no-log-prefix --since "$START" sr-jwt-1 | grep 'POST /subjects/jwt-forward-test-value/versions' | grep -c ' - platform \[' || true)
follower=$(dc logs --no-log-prefix --since "$START" sr-jwt-2 | grep 'POST /subjects/jwt-forward-test-value/versions' | grep -c ' - platform \[' || true)
rec 13b "forwarded registration logged as principal platform on follower | leader" "1|1" "$follower|$leader" \
  "request-log lines with user=platform; svc-reader's attempt was denied on the follower, never forwarded"

log "Check 11: restart auth service (empty cache), warm svc-reader only, stop SQL Server"
dc restart auth-service >/dev/null && dc up -d --wait auth-service >/dev/null
pytest_phase warm
base stop mssql >/dev/null
pytest_phase db_down
log "Starting SQL Server again"
base up -d --wait mssql >/dev/null
until [[ "$(mssql_q "SELECT state_desc FROM sys.databases WHERE name = 'platform'" master 2>/dev/null)" == ONLINE ]]; do sleep 1; done

log "Check 12: stop the auth service"
dc stop auth-service >/dev/null
pytest_phase auth_down
m=$(marker 12)
rec 12b "with auth service down the request never reached SR (marker in SR request logs | NGINX upstream)" "0|-" \
  "$(sr_logs | hits "$m")|$(dc logs --no-log-prefix nginx | grep -- "$m" | sed -E 's/.*upstream=([^ ]*).*/\1/' | tail -1)" "$m"
dc up -d --wait auth-service >/dev/null
code=$(curl -s -o /dev/null -w '%{http_code}' -u "$READER" "$PROXY/subjects/orders-value/versions/latest")
rec 12c "auth service started again: svc-reader via NGINX recovers" 200 "$code" "NGINX re-resolves auth-service (resolver valid=5s)"

log "Principal log excerpts -> results/jwt-principal-log.txt"
{
  echo "# sr-jwt-1 / sr-jwt-2, this run (since $START)"
  dc logs --since "$START" sr-jwt-1 sr-jwt-2 2>&1 \
    | grep -E 'The principal is|Processing new Jwt login|denied operation|rest-utils.requests' \
    | grep -v '"GET /subjects HTTP/1.1" 401' | head -80
  echo; echo "# startup: authentication handler configuration"
  dc logs sr-jwt-1 2>&1 | grep -E 'IDP issuer|IDP Oauth is enabled' | tail -2
  echo; echo "# nginx access log, this run"
  dc logs --no-log-prefix --since "$START" nginx | grep ACCESS | head -60
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
