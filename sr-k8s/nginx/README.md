# nginx

The entry point clients use. Clients put their Kafka cluster as the first path segment of the registry URL:
`schema.registry.url=http://sr-proxy:8080/c17`.

For every request NGINX:
1. Takes the cluster from the first path segment (`/c17/subjects` -> `c17`). There is no list of valid clusters:
   `/subjects` is read as cluster `subjects` and fails at the auth service (401). Only `/` gives 404.
2. Calls the auth service (`GET /check` with `X-Cluster: c17` and the client's Basic header). 401 goes straight
   back to the client.
3. On 200, forwards the request to Schema Registry **without** the `/c17` prefix and with
   `Authorization: Bearer <JWT>` instead of the client's Basic header.

`GET /healthz` answers 200 without calling anything (for Kubernetes probes).

## Files

- `default.conf.template`: the config. The official NGINX image renders it into `/etc/nginx/conf.d/default.conf`
  at startup, filling in `${AUTH_SERVICE}` and `${SCHEMA_REGISTRY}` from the environment.
- `Dockerfile`: `nginx:1.27.5-alpine` + the template.
- `image/nginx_1.27.5-alpine_amd64.tar.gz`: the official `nginx:1.27.5-alpine` base image (linux/amd64, from
  `docker save`), for machines with no access to Docker Hub. `image/SHA256SUMS` has its checksum.

## Build

With Docker Hub access:

```bash
docker build -t sr-proxy .
```

Air-gapped: load the base image first, then build (the `FROM` is found locally, nothing is downloaded):

```bash
(cd image && sha256sum -c SHA256SUMS)
docker load -i image/nginx_1.27.5-alpine_amd64.tar.gz     # -> nginx:1.27.5-alpine
docker build --platform linux/amd64 -t sr-proxy .
```

If your build pulls base images from an internal registry instead, push the loaded image there and change the
`FROM` line to that name:

```bash
docker tag nginx:1.27.5-alpine <registry>/nginx:1.27.5-alpine && docker push <registry>/nginx:1.27.5-alpine
```

## Run

Listens on port 8080. Expose it as a Kubernetes Service (the examples use `sr-proxy`).

| Env var | Default | |
|---|---|---|
| `AUTH_SERVICE` | `auth-service:8080` | host:port of the auth service |
| `SCHEMA_REGISTRY` | `schema-registry:8081` | host:port of Schema Registry. Use one Kubernetes Service over all SR pods; writes that hit a follower are forwarded to the leader by SR itself |

Both names are resolved once when NGINX starts, so the Kubernetes Services must exist before the pod starts
(their ClusterIPs don't change, so that's enough).
