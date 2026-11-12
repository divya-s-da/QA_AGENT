# QA-Tester agent (DoTrack-style deep QA): ADK web UI + Playwright Chromium + MCP
# Build:  docker build -t qa-agent:latest .
# Run:    docker run -p 8000:8000 -e AIROUTER_API_KEY=... qa-agent:latest
FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    HOME=/root

# Node for the Playwright MCP server (launched via npx by the navigator agent)
RUN apt-get update && apt-get install -y --no-install-recommends \
        nodejs npm \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first (cache-friendly layer)
COPY requirements.txt /app/react_qa_agent/requirements.txt
# Debian's metadata is sometimes signed with a future validity window; prime
# the apt lists with permissive flags so playwright's internal apt-get reuses
# the cached lists instead of re-resolving and hard-failing on "not valid yet".
RUN apt-get -o Acquire::Check-Valid-Until=false -o Acquire::AllowInsecureRepositories=true update \
    && pip install --no-cache-dir -r /app/react_qa_agent/requirements.txt \
    && playwright install --with-deps chromium \
    && npm install -g @playwright/mcp@0.0.79

# Chromium binary location for browser_server._find_chrome
RUN CHROME_BIN=$(find /root/.cache/ms-playwright -name chrome -path "*chrome-linux*" | sort | tail -1) \
    && echo "CHROME_PATH=$CHROME_BIN" \
    && test -n "$CHROME_BIN"

# Agent code (secrets stay OUT: .env is dockerignored, keys come from -e at runtime)
COPY . /app/react_qa_agent

# COPY preserves source permissions; the entrypoint must be executable.
RUN chmod +x /app/react_qa_agent/docker-entrypoint.sh /app/react_qa_agent/build_docker.sh

WORKDIR /app/react_qa_agent

EXPOSE 8000 9222

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000', timeout=4)" || exit 1

ENTRYPOINT ["/app/react_qa_agent/docker-entrypoint.sh"]
