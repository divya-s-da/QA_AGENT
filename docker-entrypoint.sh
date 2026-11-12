#!/bin/sh
set -eu

# Entrypoint for the qa-agent image: keepalive patch, Chromium CDP, ADK web.
exec python /app/react_qa_agent/docker_entry.py
