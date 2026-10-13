# Shared helpers. Source from the other scripts.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

SR=http://localhost:8081
SR_NOPLUGIN=http://localhost:8082
SR_JDBC=http://localhost:8083
MSSQL_SA_PASS=Platform-secret1

# Credentials (POC only). Schema Registry user = <cluster>.<scram-user>, same password as SCRAM.
ADMIN_USER=platform-admin;  ADMIN_PASS=platform-admin-secret
A_USER=c17.svc-a;           A_PASS=svc-a-secret
B_USER=c17.svc-b;           B_PASS=svc-b-secret

dc()      { docker compose "$@"; }
kexec()   { docker compose exec -T kafka "$@"; }
mssql_q() { docker compose exec -T mssql /opt/mssql-tools18/bin/sqlcmd -C -I -S localhost -U sa -P "$MSSQL_SA_PASS" -d "${2:-platform}" -b -h -1 -W -Q "SET NOCOUNT ON; $1"; }
sracl()   { docker compose exec -T schema-registry sr-acl-cli --config /tmp/sr.properties "$@"; }
mssql_up() { # start SQL Server and wait until the platform database is ONLINE (not just the server)
  docker compose up -d --wait mssql >/dev/null 2>&1
  until [[ "$(mssql_q "SELECT state_desc FROM sys.databases WHERE name = 'platform'" master 2>/dev/null)" == ONLINE ]]; do sleep 1; done
}
now_ms()  { perl -MTime::HiRes=time -e 'printf "%d\n", time*1000'; }
log()     { printf '\033[1;34m==> %s\033[0m\n' "$*"; }

# http_code <user:pass|-> <method> <url> [json-body]  -> prints status code; body saved to $LAST_BODY
LAST_BODY="$(mktemp -t srpoc.XXXX)"
http_code() {
  local creds=$1 method=$2 url=$3 body=${4:-}
  local args=(-s -o "$LAST_BODY" -w '%{http_code}' -X "$method" -H 'Content-Type: application/vnd.schemaregistry.v1+json')
  [[ "$creds" != "-" ]] && args+=(-u "$creds")
  [[ -n "$body" ]] && args+=(--data "$body")
  curl "${args[@]}" "$url"
}

wait_for_http() { # url expected-code timeout-s
  local url=$1 want=$2 t=${3:-120} i=0
  until [[ "$(curl -s -o /dev/null -w '%{http_code}' "$url" || true)" == "$want" ]]; do
    (( i++ >= t )) && { echo "timeout waiting for $url -> $want"; return 1; }
    sleep 1
  done
}

# Build a {"schema": "..."} registration body from an .avsc file
schema_body() { python3 -c 'import json,sys; print(json.dumps({"schema": json.dumps(json.load(open(sys.argv[1])))}))' "$1"; }
