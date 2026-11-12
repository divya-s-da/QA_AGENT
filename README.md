# QA Tester Agent

Deterministic QA pipeline for testing live React deployments against automated test contracts. Clones the app's repository, maps its screens, generates accessibility/UX/error-handling contracts, then uses a browser agent (Playwright MCP over a shared CDP browser) to test each screen and produce an honest DOCX report with screenshots.

## Architecture

The pipeline runs 4 deterministic steps with Python control flow (no LLM orchestration):

1. **Mapper** (`tools/deterministic_mapper.py`) — clones the repo and maps its screens (route, component) without an LLM.
2. **Contract generator** (`prompts/contract.py`) — builds test contracts (checks per screen) from YAML standards in `standards/`.
3. **Navigator + Judge** (`run_pipeline.py`) — for each screen, a navigator agent drives a shared headless browser (CDP port 9222, `tools/browser_server.py`), records per-check evidence, takes screenshots; then a judge agent issues PASS/FAIL/BLOCKED verdicts. Login is done once up front; the shared browser session persists login state across screens.
4. **Report generator** (`prompts/report.py` + `tools/docx_report.py`) — compiles an honest DOCX report (scorecard, per-screen results, screenshots). HONESTY RULES forbid inventing results.

### Reliability features
- **Rate limiting + retry** (`utils/throttle.py`) — spaces LLM calls and retries rate limits, empty responses, and network hiccups.
- **Fresh session per screen** — only the current screen's contract is sent, so context stays small.
- **Evidence via tools** (`tools/evidence_tools.py`) — navigator/judge record results to session state with `record_evidence`/`record_verdict` instead of relying on final JSON output.
- **Screenshot fallback** — if no verdict is recorded but screenshots exist, they are counted as BLOCKED evidence so nothing is silently lost.

## Setup

```bash
pip install -r requirements.txt
playwright install chromium
cp .env.example .env     # then fill in AIROUTER_API_KEY + site credentials
```

## Usage

```bash
python -m react_qa_agent.run_pipeline \
  --repo-url "https://github.com/owner/app" \
  --site-url "http://example.com/login" \
  --charter charters/my-client.yaml \
  --max-screens 10        # optional, default 10
```

Screenshots are written to `~/.react_qa_screenshots/`; the DOCX report is written next to the pipeline module.

## Safety for client and production sites

- **Read-only by default, enforced in the browser.** `tools/write_guard.py` aborts every
  POST/PUT/PATCH/DELETE before it leaves the machine. GraphQL queries and URLs on the
  allowlist still pass. Browser tools refuse to run if the guard is down (fail closed).
  Not covered: WebSocket frames and service-worker requests.
- **The charter decides the mode.** `--charter` is required. Writes are allowed only when it
  sets `destructive_actions_allowed: true`; `write_allowlist` lists URL patterns that may
  write anyway (copy `charters/example.yaml`). The ADK web path uses `QA_WRITE_MODE` and
  `QA_WRITE_ALLOWLIST` from `.env` instead.
- **Credentials never reach the model.** The agent calls `site_login()`, which reads
  `SITE_EMAIL` / `SITE_PASSWORD` from `.env` and fills the form itself. There are no default
  credentials. Typed password values are masked in `get_interactive_elements`.
- In read-only runs, a blocked write is recorded BLOCKED (via `get_blocked_writes()`), not
  FAIL, and the signup test is skipped.

## Configuration

All settings live in `config.py`, overridable via `.env`:

| Variable | Purpose |
| --- | --- |
| `AIROUTER_API_KEY` | API key for the LLM via aiRouter.ch (`DeepSeek-V4-Flash` for reasoning, `Qwen3.8` for visual judgement) |
| `OPENCODE_GO_API_KEY` | Fallback backend, used only when `AIROUTER_API_KEY` is unset |
| `SITE_EMAIL` | Login email for the target site |
| `SITE_PASSWORD` | Login password (fallback: `SITE_PASSWORD_FALLBACK`) |
| `TEST_FILE_PATH` | Optional file used for upload-testing checks (empty = skip uploads) |
