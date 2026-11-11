# schema-registry

Config for `confluentinc/cp-schema-registry:7.9.0` with the Confluent Security Plugin (already in the image).
Schema Registry does not see passwords: it gets a JWT from NGINX, checks it against the auth service's
`/jwks.json`, uses the token's `sub` (`<cluster>.<name>`, e.g. `c17.alice`) as the principal, and checks its ACLs.

## schema-registry.properties

Change the Kafka section (`kafkastore.*`) to match your cluster. The SR user needs the usual Kafka ACLs on
`_schemas`, `_schemas_acl`, `_confluent-license` and the consumer group `schema-registry`.

Each pod also needs `host.name` set to an address the other SR pods can reach (followers forward writes to the
leader there). In Kubernetes, mount the file from a ConfigMap and append the pod IP at startup:

```yaml
env:
  - name: POD_IP
    valueFrom: { fieldRef: { fieldPath: status.podIP } }
command: ["bash", "-c"]
args:
  - |
    { cat /config/schema-registry.properties; echo; echo host.name=$POD_IP; } > /tmp/sr.properties
    exec schema-registry-start /tmp/sr.properties
volumeMounts:
  - { name: config, mountPath: /config }
```

Expose port 8081 as a Kubernetes Service named `schema-registry` (NGINX uses that name). The auth service must be
up first: Schema Registry fetches `http://auth-service:8080/jwks.json`.

A request without a valid JWT gets 401. Through NGINX, a valid user without an ACL gets 403.

## ACLs

ACLs are managed with `sr-acl-cli`, which is in the same image. Run it inside an SR pod, pointing at the config
the pod actually runs with:

```bash
kubectl exec -it <sr-pod> -- bash
sr-acl-cli --config /tmp/sr.properties --add -p platform.admin -s '*' -o '*'      # the CI that registers schemas
sr-acl-cli --config /tmp/sr.properties --add -p c17.alice -s orders-value -o SUBJECT_READ    # enough for serializers
sr-acl-cli --config /tmp/sr.properties --list
```

`SUBJECT_READ` is all a producer or consumer needs. Listing subjects (`GET /subjects`) is different: without
`GLOBAL_READ` it is a 403, and with it the list only shows subjects the principal has `SUBJECT_COMPATIBILITY_READ` on:

```bash
sr-acl-cli --config /tmp/sr.properties --add -p c17.alice -o GLOBAL_READ
sr-acl-cli --config /tmp/sr.properties --add -p c17.alice -s orders-value -o SUBJECT_COMPATIBILITY_READ
```

The principal is always `<Cluster.Name>.<ServiceWallets.Name>`.
