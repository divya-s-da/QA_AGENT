# Deploying QA Navigator (v2) to Azure

The product is the v2 job server (`react_qa_agent.v2.server`): one container that serves the UI/API and
spawns a run (several headless-Chromium browsers) per test. Put it behind Azure's HTTPS ingress.

## Build the image

```
docker build -f v2/Dockerfile -t qa-navigator .
```
The image installs Chromium with `playwright install --with-deps chromium`, runs as a non-root user, and starts
the server on `0.0.0.0:8080` with `--runs /data/runs`. Secrets are never baked in; pass them at run time.

## Required configuration (Azure app settings / env)

| Variable | Purpose |
|---|---|
| `V2_API_TOKEN` | Operator token (16+ chars). It logs in as `operator` and sees every run. Keep it secret. |
| `AIROUTER_API_KEY` | The model key. Add `AIROUTER_API_KEY_2` (or `AIROUTER_API_KEYS=k1,k2`) for more capacity. |
| `V2_OPEN_SIGNUP` | Optional. `1` lets anyone register. Default: the operator creates accounts (first user bootstraps). |
| `V2_MAX_CONCURRENT_CALLS` | Optional. In-flight model calls per key (default 6). |

Do **not** set `V2_ALLOW_PRIVATE_HOSTS` in production — leaving it unset keeps the SSRF guard on (a run cannot
reach internal/metadata addresses).

## Persistence (important)

Mount a persistent volume at **`/data`**. Everything that must survive a restart lives there automatically:

- `/data/runs` — each run's folder and reports (the CMD already points `--runs` here).
- `/data/accounts.db` — user accounts (next to the runs dir by default).
- `/data/memory/` — the "new since last run" store.

Without a persistent `/data`, accounts and cross-run memory reset on every container restart.

## Resources

Each run opens several headless Chromium browsers. Give the container at least **2 vCPU and 4 GB RAM**. The
browsers already launch with `--no-sandbox --disable-dev-shm-usage`, so Azure's small `/dev/shm` is not a problem.

## Run a single instance (for now)

The job server holds its queue and running processes in memory and manages the browser subprocesses itself.
**Deploy ONE replica** (scale to 1); do not autoscale to multiple replicas, because a second replica would not
know about runs started on the first. A shared, externalised queue is a later change if horizontal scale is needed.

## HTTPS and health

- Azure (Container Apps or App Service for Containers) terminates TLS at the ingress; the container serves plain
  HTTP on 8080. The UI already sends the login token in a header, not the URL.
- Health check: `GET /` returns the UI with HTTP 200.

## First use

1. Open the app URL. It shows a login screen.
2. The **first** account you create is the bootstrap admin. After that, the operator token (or `V2_OPEN_SIGNUP=1`)
   controls who can register.
3. Each user sees only their own runs, reports, and history.

## Known limits (from the system-design review)

- Single instance only (above).
- The runs list reads each run on every poll — fine for a team, add pagination before large history.
- Safe-write logs every write but does not yet auto-delete created test data.
