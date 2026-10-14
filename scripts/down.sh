#!/usr/bin/env bash
# Stop everything and delete all volumes (Kafka data, SQL Server data).
source "$(dirname "$0")/lib.sh"
dc --profile bench --profile client --profile dr down -v --remove-orphans
