# NGINX vs Envoy in front of Schema Registry

Both proxies were run in this PoC in the same role, and each passed the same 20 checks (`scripts/test.sh`).
This page compares them for that role: the edge proxy that checks every request with the auth service and sends
a JWT on to Schema Registry.

- **NGINX version:** the commit `9093537`, `nginx/nginx.conf`, `nginx:1.27.5-alpine`.
- **Envoy version:** the current working tree, `envoy/envoy.yaml`, `envoyproxy/envoy:v1.39.4`.

## TL;DR

- **Security is the same.** In both, every request gets one auth call, the client's `Authorization` header is
  always replaced, the proxy fails closed, and a forged token never reaches SR.
- **NGINX is shorter to configure and already familiar to the team.** It's enough for this design as built.
- **Envoy is easier to observe and more flexible.** You get built-in Prometheus stats, active health checks, a
  configurable failure status, the auth service's own error bodies passed to the client, and gRPC or
  edge-JWT options for later. In return it's more config, and a new tool for the team to run.
- **Recommendation:** use **NGINX** unless the team wants Envoy's metrics or health checks in production, or already
  runs Envoy somewhere. The PoC needs none of Envoy's extras. See [Choosing](#choosing).

## The flow is the same

```
client --Basic svc:pw--> PROXY :10000 --check (headers only)--> auth-service:8080
                            |                                     bcrypt vs in-memory snapshot of sr_services
                            |<-- 200 + JWT  |  401 + reason ------|
                            |
                            +--Authorization: Bearer <JWT>--> sr-jwt-1 / sr-jwt-2 (round-robin)
                                                              SR validates the JWT, the ACL authorizer decides
```

| Step | NGINX | Envoy |
|---|---|---|
| Auth call mechanism | `auth_request` subrequest to an internal location | `ext_authz` HTTP filter |
| URL the auth service gets | `/check` (fixed); the original path is sent in `X-Original-URI` | `/check` + the original path, e.g. `/check/subjects/orders-value/versions/latest` |
| Method the auth service gets | The PoC sends the original in `X-Original-Method` and doesn't rely on the subrequest's own method | The original method |
| Body | Not sent (`proxy_pass_request_body off`) | Not sent (it would be only with `with_request_body`) |
| How the JWT gets upstream | The auth service returns `X-SR-Token: <jwt>`, then `auth_request_set` + `proxy_set_header Authorization "Bearer $sr_token"` | The auth service returns `Authorization: Bearer <jwt>`, and `allowed_upstream_headers` sets it, overwriting the client's header |
| Load balancing to SR | `upstream` block, round-robin | `STRICT_DNS` cluster, `ROUND_ROBIN` |

### The auth service is not proxy-neutral

The auth service's contract differs for each proxy (`auth-service/app.py`):

| | NGINX | Envoy |
|---|---|---|
| Route | `@app.api_route("/check")` | `@app.api_route("/check{path:path}")` |
| Success response | `X-SR-Token: <jwt>` | `Authorization: Bearer <jwt>` (`X-SR-Token` kept as well) |
| Where it reads the original request for logs | `X-Original-Method`, `X-Original-URI` headers | `request.method` and the path suffix |

The reason is that Envoy can't build a header value such as `"Bearer " + header` from the auth service's
response. It can only copy headers, so the auth service has to return the final `Authorization` value itself.
NGINX can build one with variables.

A single auth service could serve both proxies if `/check{path:path}` returns both headers. The current code
already does, apart from the NGINX-specific log headers.

## Configs

### NGINX (`nginx/nginx.conf`, 57 lines)

```nginx
worker_processes 1;
events { worker_connections 1024; }

http {
  # Docker's DNS, re-resolved so restarted containers (new IPs) are found again
  resolver 127.0.0.11 valid=5s ipv6=off;

  # upstream = SR node that served it ("-" = never reached SR); auth = auth service's X-Auth-Service reason
  log_format sr 'ACCESS $request_method $request_uri status=$status upstream=$upstream_addr '
                'user=$remote_user auth=$auth_reason';
  access_log /dev/stdout sr;
  error_log /dev/stderr warn;

  upstream auth_service {
    zone auth_service 64k;
    server auth-service:8080 resolve;
  }
  upstream schema_registry {
    zone schema_registry 64k;
    server sr-jwt-1:8081 resolve;
    server sr-jwt-2:8081 resolve;
  }

  server {
    listen 10000;
    client_max_body_size 20m;

    location / {
      auth_request /_auth;
      auth_request_set $sr_token $upstream_http_x_sr_token;
      auth_request_set $auth_reason $upstream_http_x_auth_service;
      add_header X-Auth-Service $auth_reason always;

      # proxy_set_header replaces the client's Authorization header (all copies of it)
      proxy_set_header Authorization "Bearer $sr_token";
      proxy_set_header Host $host;
      proxy_pass http://schema_registry;
    }

    location = /_auth {
      internal;
      proxy_pass http://auth_service/check;
      proxy_pass_request_body off;               # credentials are in the headers; don't send the schema body
      proxy_set_header Content-Length "";
      proxy_set_header X-Original-URI $request_uri;
      proxy_set_header X-Original-Method $request_method;
      proxy_connect_timeout 2s;
      proxy_read_timeout 5s;
    }
  }
}
```

### Envoy (`envoy/envoy.yaml`, 80 lines)

```yaml
admin:
  address: { socket_address: { address: 127.0.0.1, port_value: 9901 } }   # inside the container only

static_resources:
  listeners:
    - name: sr
      address: { socket_address: { address: 0.0.0.0, port_value: 10000 } }
      filter_chains:
        - filters:
            - name: envoy.filters.network.http_connection_manager
              typed_config:
                "@type": type.googleapis.com/envoy.extensions.filters.network.http_connection_manager.v3.HttpConnectionManager
                stat_prefix: sr
                access_log:
                  - name: envoy.access_loggers.stdout
                    typed_config:
                      "@type": type.googleapis.com/envoy.extensions.access_loggers.stream.v3.StdoutAccessLog
                      log_format:
                        text_format_source:
                          inline_string: "ACCESS %REQ(:METHOD)% %REQ(:PATH)% status=%RESPONSE_CODE% upstream=%UPSTREAM_HOST% auth=%RESP(X-AUTH-SERVICE)% details=%RESPONSE_CODE_DETAILS%\n"
                route_config:
                  virtual_hosts:
                    - name: sr
                      domains: ["*"]
                      routes:
                        - match: { prefix: "/" }
                          route: { cluster: schema_registry, timeout: 60s }
                http_filters:
                  - name: envoy.filters.http.ext_authz
                    typed_config:
                      "@type": type.googleapis.com/envoy.extensions.filters.http.ext_authz.v3.ExtAuthz
                      transport_api_version: V3
                      failure_mode_allow: false
                      status_on_error: { code: ServiceUnavailable }
                      http_service:
                        server_uri: { uri: "http://auth-service:8080", cluster: auth_service, timeout: 5s }
                        path_prefix: /check
                        authorization_response:
                          # Set (not appended): replaces every Authorization header the client sent
                          allowed_upstream_headers:
                            patterns: [{ exact: authorization, ignore_case: true }]
                          allowed_client_headers_on_success:
                            patterns: [{ exact: x-auth-service, ignore_case: true }]
                  - name: envoy.filters.http.router
                    typed_config:
                      "@type": type.googleapis.com/envoy.extensions.filters.http.router.v3.Router

  clusters:
    - name: auth_service
      type: STRICT_DNS
      dns_lookup_family: V4_ONLY
      dns_refresh_rate: 5s
      connect_timeout: 2s
      load_assignment:
        cluster_name: auth_service
        endpoints:
          - lb_endpoints:
              - endpoint: { address: { socket_address: { address: auth-service, port_value: 8080 } } }
    - name: schema_registry
      type: STRICT_DNS
      dns_lookup_family: V4_ONLY
      dns_refresh_rate: 5s
      connect_timeout: 2s
      lb_policy: ROUND_ROBIN
      load_assignment:
        cluster_name: schema_registry
        endpoints:
          - lb_endpoints:
              - endpoint: { address: { socket_address: { address: sr-jwt-1, port_value: 8081 } } }
              - endpoint: { address: { socket_address: { address: sr-jwt-2, port_value: 8081 } } }
```

Envoy also needs a command-line flag in `docker-compose.yml`:
`--file-flush-interval-msec 1000`. Without it, access-log lines appear up to 10 s late. See
[Operations](#operations).

### Key lines mapped across

| Concern | NGINX | Envoy |
|---|---|---|
| Call the auth service | `auth_request /_auth;` + `location = /_auth { internal; proxy_pass … }` | `envoy.filters.http.ext_authz` + `http_service.server_uri` |
| Don't send the body | `proxy_pass_request_body off;` + `Content-Length ""` | the default |
| Put the JWT upstream | `auth_request_set` + `proxy_set_header Authorization "Bearer $sr_token"` | `allowed_upstream_headers: authorization` |
| Show the auth reason to the client | `add_header X-Auth-Service $auth_reason always;` | Denied: all auth service headers pass by default. Allowed: `allowed_client_headers_on_success` |
| Fail closed | built in, can't be turned off | `failure_mode_allow: false` (the default) |
| Status when the auth service is down | always 500 | `status_on_error` (default 403; set to 503 here) |
| Auth call timeouts | `proxy_connect_timeout 2s; proxy_read_timeout 5s;` | `connect_timeout: 2s` on the cluster, `timeout: 5s` on `server_uri` |
| Find restarted containers again | `resolver … valid=5s` + `server … resolve` (open-source NGINX ≥ 1.27.3) | `type: STRICT_DNS` + `dns_refresh_rate: 5s` |
| Access log | `log_format` with `$upstream_addr`, `$remote_user` | `text_format_source` with `%UPSTREAM_HOST%`, `%RESPONSE_CODE_DETAILS%` |

## Test results compared

All 20 rows passed with both proxies. These rows differ:

| Check | NGINX | Envoy | Notes |
|---|---|---|---|
| 4: wrong password | 401, `WWW-Authenticate`, `X-Auth-Service: invalid-credentials` | the same, plus the auth service's JSON body `{"error_code": 401, "message": "invalid-credentials"}` | NGINX sends its own error page and copies only `WWW-Authenticate` from the auth response; `X-Auth-Service` comes from `add_header`. Envoy passes on the auth service's whole denial response. |
| 7b: valid Basic for svc-reader + a second `Authorization: Bearer <forged platform token>` | **400**, rejected by NGINX before `auth_request` runs | **403** from SR as svc-reader | Envoy joins the two headers into one. The auth service accepts the Basic part, and Envoy then overwrites the whole header with svc-reader's JWT, so the forged token is dropped. Both are safe; NGINX's result is stricter and easier to explain. |
| 12: auth service stopped | **500** (not configurable) | **503** (configured) | Both fail closed and never reach SR (row 12b). 503 tells clients and monitoring that a dependency is down; 500 looks like a proxy bug. |
| 12c: recovery after the auth service restarts | 4 s | 1 s | One run each, and timings vary on this machine. NGINX drops a server whose name stops resolving (stopped container) and re-adds it on the next 5 s resolve. Envoy keeps the last address when resolution fails. |
| 4b / 12b log evidence | worked as written | needed changes to the flush flag and `test.sh` | Envoy buffers access logs and flushes every 10 s by default. The test now waits for the line. |

Rows 1–3, 5, 6, 8–11 and 13–15 gave the same results with both proxies.

## Operations

| | NGINX | Envoy |
|---|---|---|
| Config format | NGINX directives, compact | YAML/protobuf (v3 API): verbose, but typed and validated at startup |
| Config changes | `nginx -s reload`: graceful, no dropped connections | A static file needs a restart; hot restart exists but is awkward in containers. Changes at runtime come from xDS (a control plane). |
| Access logs | Written as they happen | Buffered: flushed every 10 s by default (`--file-flush-interval-msec`) |
| Why a request was rejected | `auth=` / `status=` in the log; `auth request unexpected status` in the error log | `%RESPONSE_CODE_DETAILS%` gives `ext_authz_denied`, `ext_authz_error` or `via_upstream` directly in the access log |
| Metrics | `stub_status` (connection counts only) in open-source NGINX. Per-upstream metrics need an exporter or NGINX Plus. | Admin `/stats/prometheus`: ext_authz ok/denied/error counters, per-cluster and per-host latency, retries, and more, with no extra component |
| Active health checks | NGINX Plus only. Open-source NGINX only marks a server as failed when real requests to it fail (`max_fails`, `fail_timeout`). | Built in, per cluster (`health_checks`) |
| Retries, circuit breaking, outlier detection | Basic (`proxy_next_upstream`) | Built in and configurable per route or cluster |
| Caching auth results | Possible: `proxy_cache` on the `/_auth` location, keyed on `Authorization` | Not built into ext_authz. Not needed here, because the auth service answers from memory. |
| Auth protocol | HTTP only | HTTP or gRPC (`envoy.service.auth.v3.Authorization`) |
| JWT validation at the edge | NGINX Plus only (`auth_jwt`) | Built in (`jwt_authn`). Not needed here, because SR validates the token. |
| Image on disk (this machine) | 76.8 MB (`nginx:1.27.5-alpine`) | 266 MB (`envoyproxy/envoy:v1.39.4`); about 24 MiB RAM measured while running the tests |
| Team | Already used by the team | New to the team |

## Choosing

**Choose NGINX if:**
- the team runs and debugs NGINX already, which is the case today
- the design stays as built: HTTP Basic → auth service → JWT, with nothing else at the edge
- graceful reloads and a short config matter more than built-in metrics

On prem, add a Prometheus exporter, or rely on the auth service's `/healthz` and SR metrics for visibility.

**Choose Envoy if:**
- you want edge metrics (auth ok/denied/error rates, per-SR-node latency) without extra components
- you want active health checks on the SR nodes and the auth service without NGINX Plus
- a clear 503 when the auth service is down matters to clients or alerting
- the platform already runs Envoy (a service mesh, Contour, Gateway API), or a gRPC auth service or edge JWT
  validation is planned

**Either way:**
- Keep the auth service answering from its in-memory snapshot. Every request makes one auth call with either proxy.
- Keep SR ports reachable only from the proxy and the other SR nodes. The proxy is a convenience; the real check is SR
  validating the JWT (check 8).
- Pin the image version and keep the PoC's tests as the acceptance suite for whichever proxy is chosen.

## Switching between them in this repo

- **Envoy:** the current working tree. `./scripts/up.sh && ./scripts/test.sh`.
- **NGINX:** commit `9093537`. `nginx/nginx.conf` and the matching `app.py`, tests and `results/` are there.

The `README.md` and `POC_REPORT.md` in the working tree still describe NGINX.
