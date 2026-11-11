# auth-service

Small FastAPI service that turns HTTP Basic credentials into a JWT for Schema Registry. NGINX calls it on every
request; Schema Registry calls it for the public key.

| Endpoint | Used by | Does |
|---|---|---|
| `GET /check` | NGINX | Reads `X-Cluster` and the Basic header. 200 with `X-SR-Token: <JWT>` if a `ServiceWallets` row has that `Name` and `Password` and its `Cluster.Name` is the `X-Cluster` value, else 401 |
| `GET /jwks.json` | Schema Registry | Public key to verify the JWTs |
| `GET /healthz` | Kubernetes probes | `{"ok": true, "services": <rows loaded>}` |

The JWT is RS256, `sub=<cluster>.<name>` (this is the Schema Registry principal), `iss=sr-auth`,
`aud=schema-registry`, valid 5 minutes.

`ServiceWallets` joined with `Cluster` is loaded whole at startup and reloaded every 60 s, so a new or changed row
works within a minute. If the first load fails the service exits; if a later reload fails it keeps the last copy.

## Database

Run `init.sql` once in the database. It creates the two tables, a read-only login `sr_auth` and example rows.

| Table | Columns |
|---|---|
| `Cluster` | `ID` (int, PK), `Name` (unique; the URL path segment, e.g. `c17`) |
| `ServiceWallets` | `ID` (uniqueidentifier, PK, `NEWID()`), `Name`, `Password` (plain text), `ClusterID` (FK to `Cluster.ID`); `(ClusterID, Name)` unique |

The Schema Registry principal is `<Cluster.Name>.<ServiceWallets.Name>`, e.g. `c17.alice`. Add a Service:

```sql
INSERT INTO ServiceWallets (Name, Password, ClusterID)
VALUES ('bob', 'bob-secret', (SELECT ID FROM Cluster WHERE Name = 'c17'));
```

## Build

```bash
docker build -t auth-service .
```

## Run

It needs an RSA private key. All replicas must use the **same** key, so create it once and put it in a Secret:

```bash
openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -out private.pem
kubectl create secret generic auth-service-key --from-file=private.pem
```

Mount the Secret at `/keys` (the file is then `/keys/private.pem`). Listens on port 8080; expose it as a
Kubernetes Service named `auth-service` (NGINX and Schema Registry use that name).

| Env var | Default | |
|---|---|---|
| `DB_HOST` | `mssql` | SQL Server host |
| `DB_PORT` | `1433` | |
| `DB_NAME` | `platform` | database with `Cluster` and `ServiceWallets` |
| `DB_USER` | `sr_auth` | |
| `DB_PASSWORD` | (empty) | `sr-auth-secret` in `init.sql` |
| `JWT_PRIVATE_KEY` | `/keys/private.pem` | |
| `RELOAD_SECONDS` | `60` | |
| `JWT_TTL_SECONDS` | `300` | |
| `JWT_ISSUER` / `JWT_AUDIENCE` | `sr-auth` / `schema-registry` | must match `oauthbearer.expected.*` in the Schema Registry config |

Run it locally:

```bash
docker run --rm -p 8080:8080 -v "$PWD/private.pem:/keys/private.pem:ro" \
  -e DB_HOST=<sql-server> -e DB_PASSWORD=sr-auth-secret auth-service
curl -i -H 'X-Cluster: c17' -u alice:alice-c17-secret localhost:8080/check   # 200 + X-SR-Token
```
