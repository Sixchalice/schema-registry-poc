# client

A Python test client using `confluent-kafka`'s `SchemaRegistryClient`, the same client the Avro/Protobuf/JSON
serializers use. It only talks to Schema Registry (through NGINX), not to Kafka.

```bash
pip install -r requirements.txt

export SR_URL=http://sr-proxy:8080/c17   # NGINX + your cluster
export SR_USER=alice SR_PASSWORD=alice-c17-secret

python client.py subjects                          # needs GLOBAL_READ (see schema-registry/README.md), else 403
python client.py latest orders-value               # latest schema
python client.py register orders-value orders-value.avsc
```

What to expect:

| Case | Result |
|---|---|
| Right name, password and cluster, ACL exists | the data |
| Wrong password, unknown name, or name in another cluster | `HTTP 401` |
| Authenticated but no ACL for that subject/operation | `HTTP 403` |

To register schemas, use the principal that has the write ACL, e.g.
`SR_URL=http://sr-proxy:8080/platform SR_USER=admin SR_PASSWORD=platform-secret`.

From your laptop, reach NGINX with `kubectl port-forward svc/sr-proxy 8080:8080` and use
`SR_URL=http://localhost:8080/c17`.
