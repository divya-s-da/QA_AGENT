# QA Agent v2: crawler, planner, tester teams, judge, report, job server.
# Build (from the repo folder; the image puts the code under /app/react_qa_agent):
#   docker build -f v2/Dockerfile -t qa-agent-v2 .
# Run:
#   docker run -d -p 8080:8080 -e AI_ROUTER_API=... -e V2_API_TOKEN=<16+ chars> \
#              -v qa_runs:/data/runs --shm-size=1g --name qa-agent-v2 qa-agent-v2
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/app \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app
COPY v2/requirements.txt /tmp/v2-requirements.txt
# libatomic1 is needed by Playwright's bundled Node before it can install Chromium; python:slim lacks it.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libatomic1 \
    && pip install --no-cache-dir -r /tmp/v2-requirements.txt \
    && playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

# Code only. Secrets never go in the image: .env is ignored; keys come from -e at run time.
COPY __init__.py react_qa_agent/__init__.py
COPY v2 react_qa_agent/v2
COPY tools react_qa_agent/tools

RUN useradd -m qa && mkdir -p /data/runs && chown -R qa /data /app
USER qa
EXPOSE 8080
CMD ["python", "-m", "react_qa_agent.v2.server", "--host", "0.0.0.0", "--port", "8080", "--runs", "/data/runs"]
