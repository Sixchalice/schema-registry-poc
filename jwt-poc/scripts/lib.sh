# Shared helpers for jwt-poc/scripts. Bash only (BASH_SOURCE).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO="$(cd "$ROOT/.." && pwd)"
cd "$ROOT"

PROXY=http://localhost:10000
MSSQL_SA_PASS=Platform-secret1

# Services in sr_services (POC passwords)
READER=svc-reader:svc-reader-secret
OTHER=svc-other:svc-other-secret
PLATFORM=platform:platform-secret

dc()      { docker compose "$@"; }                       # this stack (sr-jwt-poc)
base()    { docker compose -f "$REPO/docker-compose.yml" "$@"; }   # the repo's main stack (kafka, mssql)
mssql_q() { base exec -T mssql /opt/mssql-tools18/bin/sqlcmd -C -I -S localhost -U sa -P "$MSSQL_SA_PASS" -d "${2:-platform}" -b -h -1 -W -Q "SET NOCOUNT ON; $1"; }
sracl()   { dc exec -T sr-jwt-1 sr-acl-cli --config /tmp/sr.properties "$@"; }
log()     { printf '\033[1;34m==> %s\033[0m\n' "$*"; }

# Build a {"schema": "..."} registration body from an .avsc file
schema_body() { python3 -c 'import json,sys; print(json.dumps({"schema": json.dumps(json.load(open(sys.argv[1])))}))' "$1"; }
