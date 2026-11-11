# Schema Registry behind NGINX + auth service

```
client --Basic, http://sr-proxy:8080/<cluster>/...--> nginx --/_auth--> auth-service (SQL Server: ServiceWallets + Cluster)
                                                        |
                                                        +--/... , Authorization: Bearer <JWT>--> schema-registry
```

1. The client calls `http://sr-proxy:8080/c17/subjects` with HTTP Basic `alice:<password>`.
2. NGINX takes `c17` from the path and asks the auth service. The auth service looks up `alice` in cluster `c17`
   in `ServiceWallets` (kept in memory) and, if the password matches, returns a JWT with `sub=c17.alice`.
3. NGINX forwards `/subjects` to Schema Registry with `Authorization: Bearer <JWT>`.
4. Schema Registry checks the JWT with the auth service's public key, takes `c17.alice` as the principal and applies
   its ACLs.

| Directory | What | README |
|---|---|---|
| `auth-service/` | Python service, Dockerfile, SQL for the table | [auth-service/README.md](auth-service/README.md) |
| `nginx/` | NGINX image (Dockerfile + config + amd64 base image tarball for air-gapped builds) | [nginx/README.md](nginx/README.md) |
| `schema-registry/` | Schema Registry config + ACL commands | [schema-registry/README.md](schema-registry/README.md) |
| `client/` | Python test client | [client/README.md](client/README.md) |

Order to bring it up: SQL table, auth service, Schema Registry, NGINX, then the ACLs, then test with the client.
Names used across the configs (change them together): Kubernetes Services `auth-service:8080`,
`schema-registry:8081`, `sr-proxy:8080`; JWT issuer `sr-auth`, audience `schema-registry`.
