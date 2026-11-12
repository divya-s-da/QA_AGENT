# QA-Tester Agent — Docker Deployment

Runs the deep-QA navigator agent (the same one used for DoTrack testing) in a
container: headless Chromium with CDP, Playwright MCP, and the ADK web chat UI.
End users open the UI in a browser, give the agent a site URL to test, and
receive a DOCX report with screenshots.

## Prerequisites

- Docker (Docker Desktop on Windows/WSL2, or any Linux host with Docker Engine)
- API keys for the LLM backend (aiRouter or opencode-go — see `.env.example`)

## Build

```sh
docker build -t qa-agent:latest .
```

or use the wrapper: `.\build_docker.ps1` (Windows) / `./build_docker.sh` (Linux).
The image bundles Python 3.14, all pip dependencies, Chromium (Playwright) and
Node + the Playwright MCP server. API keys are NEVER baked into the image.

> Note: if `./build_docker.sh` says "Permission denied" (exec bit not preserved
> on some filesystems), run it as `sh build_docker.sh` instead.

## Run

```sh
docker run -d --name qa-agent \
  -p 8000:8000 \
  -e AIROUTER_API_KEY=your-key-here \
  -e SITE_EMAIL=user@example.com \
  -e SITE_PASSWORD=your-password \
  -e SITE_PASSWORD_FALLBACK=alternate-password \
  -v qa-evidence:/root/.react_qa_screenshots \
  -v qa-state:/app/react_qa_agent/state \
  qa-agent:latest
```

Or with compose (reads the same variables from your environment):

```sh
docker compose up -d
```

## Using it

1. Open `http://localhost:8000` — this is the ADK web UI.
2. Pick the `navigator` agent (the `report_generator` agent also exists).
3. Give it a site URL or a feature to test. The agent will browse the target
   site only (off-site navigation is blocked by the site guard), take
   screenshots, record evidence, and finish with `build_docx_report()`.
4. The DOCX report + all screenshots are saved under the `qa-evidence` volume
   (`/root/.react_qa_screenshots` in the container). They survive restarts.

REST API is also available (`POST /run`, etc.) for programmatic use.

## Environment variables

| Variable | Required | Purpose |
|---|---|---|
| `AIROUTER_API_KEY` | one of the keys | aiRouter.ch flat-rate LLM backend |
| `OPENCODE_GO_API_KEY` | one of the keys | fallback LLM backend (used if aiRouter key absent) |
| `SITE_EMAIL` / `SITE_PASSWORD` / `SITE_PASSWORD_FALLBACK` | yes | target-site login credentials used by the navigator |
| `ALLOW_ORIGINS` | no | comma-separated CORS origins (default: localhost:8000) |
| `PLAYWRIGHT_MCP_CMD` | no | override the npx command for the Playwright MCP server (default: `npx`) |
| `CHROME_PATH` | no | override the Chromium binary path (default: discovered in the image) |
| `TEST_FILE_PATH` | no | file path used by the upload test case (empty = skip uploads) |

## Data persistence

- `qa-evidence` volume → screenshots, DOCX reports (`~/.react_qa_screenshots`)
- `qa-state` volume → pipeline run state (`state/`)
- ADK session storage lives under the agents dir; mount `/app/react_qa_agent/adk_agents` as a named volume if session history must survive rebuilds (note: that replaces the code — rebuild the image before mounting it).

## Security notes

- The ADK server runs in dev mode (`adk web`) because the agent directory is
  nested (`navigator/`, `report/`), which `adk api_server` does not support.
  Dev mode binds to `0.0.0.0` when exposed — do NOT publish port 8000 directly
  to the public internet. Put it behind a reverse proxy with basic auth, or
  restrict the port to trusted networks/VPN.
- API keys are runtime-only environment variables. The build context excludes
  `.env` (see `.dockerignore`).
- The container runs Chromium with `--no-sandbox` (root-in-container
  requirement). Treat the container as untrusted input boundary.

## Troubleshooting

- **Browser won't start** — check `docker logs qa-agent` for "browser up on 9222".
  Verify `CHROME_PATH` if you overrode it.
- **SSE connection drops on long runs** — the entrypoint applies the ADK
  SSE-keepalive patch idempotently; logs show "applied SSE keepalive patch" or
  "already present". If you see a WARNING, the installed ADK version changed —
  the patch pattern needs updating to match.
- **Debug the browser** — expose port 9222 and connect with any CDP client
  (e.g. Chrome DevTools at `http://localhost:9222`).
- **Report volume content** — `docker run --rm -v qa-evidence:/data alpine ls /data`
