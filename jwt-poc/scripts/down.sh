#!/usr/bin/env bash
# Remove this stack's containers and its Kafka topics. Leaves the repo's main stack, sr_services and keys/.
source "$(dirname "$0")/lib.sh"
dc --profile tests down --remove-orphans
if base ps --status running --services 2>/dev/null | grep -qx kafka; then
  for t in _schemas_jwt _schemas_jwt_acl; do
    base exec -T kafka kafka-topics --bootstrap-server kafka:29092 --command-config /etc/kafka/poc/admin.properties \
      --delete --if-exists --topic "$t" >/dev/null || true
  done
fi
