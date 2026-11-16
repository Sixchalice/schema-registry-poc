#!/usr/bin/env bash
# Run on a machine WITH internet before moving acl-api/ to the air-gapped system: saves the two base
# images the Dockerfile builds from into images/base-images.tar (~350 MB).
# On the air-gapped side:  docker load -i images/base-images.tar && docker compose up -d --build
# Usage: ./save-base-images.sh [platform]     (default linux/amd64; use linux/arm64 for ARM hosts)
set -euo pipefail
cd "$(dirname "$0")"
platform=${1:-linux/amd64}
images=(python:3.12-slim eclipse-temurin:17-jre)
for img in "${images[@]}"; do docker pull --platform "$platform" "$img"; done
mkdir -p images
docker save --platform "$platform" -o images/base-images.tar "${images[@]}"
ls -lh images/base-images.tar
