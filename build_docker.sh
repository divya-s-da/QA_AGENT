#!/bin/sh
set -eu
# Builds the qa-agent image; use `./build_docker.sh up` to also start the stack.
docker build -t qa-agent:latest .
if [ "${1:-}" = "up" ]; then
  docker compose up -d
  echo "qa-agent running at http://localhost:8000"
else
  echo "Image built. Run it with: docker compose up -d"
fi
