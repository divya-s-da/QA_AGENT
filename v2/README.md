# QA Agent v2

The design is the v2 system design brief (2026-11-03): a crawler, a planner, three tester teams
that work in parallel from one task board, a judge, a checker and a reporter. The build order has
8 steps; this folder grows one step at a time.

| Step | Part | State |
|---|---|---|
| 1 | Baseline of today's agent on ShopLite | **done** (aiRouter key, 2026-11-03) |
| 2 | Crawler, task board, checker (plain code) | **built** |
| 3 | Function testers in parallel, proof rule, loop guards | **built** |
| 4 | Planner | **built** |
| 5 | Journey team, UI and UX team | **built** |
| 6 | Judge, always on | **built** |
| 7 | Human report, user manual on request | **built** |
| 8 | Containers for each run, speed | **partly**: Docker image + job server; speed is the open item |

## Step 2: what is here

- `sitemap.py`: pages, interactive elements (with stable IDs) and links. `require_element()` rejects an
  ID that is not on the map.
- `crawler.py`: its own headless browser, read-only through `tools/write_guard.decide`, breadth first over
  same-origin links, logged out and then logged in (`tools/site_login`). Basic checks on every page as it
  loads: load, JavaScript errors, broken files, title, and (after the crawl) every link.
- `board.py`: the SQLite task board. Claims, claim expiry, the proof rule, reuse, follow-ups, a judge view
  without the tester's verdict, and an event log.
- `checker.py`: compares the map with the board, pins gaps as tasks, round limit, then NOT TESTED with a reason.
- `results.py`: results JSON for `benchmark/score.py`, and `summary.md`.
- `run.py`: the command line.

```bash
# from the folder above the repo
python -m react_qa_agent.v2.run --url http://127.0.0.1:8765 --login --out /tmp/v2run
# or against ShopLite, scored:
python react_qa_agent/benchmark/run_benchmark.py --engine v2 --bugs all --label v2-step2
```

## ShopLite result of step 2 (2026-11-03)

Plain code, no model call, about 11 seconds:

- Bug build: 5 of 22 bugs caught (B13 broken link, B15 broken image, B19 empty title, B20 JavaScript
  error, B21 empty href). T0 catch rate 0%: the money, login and permission bugs need the tester teams.
- Clean build: 0 FAIL of 146 verdicts.
- First page checked after about 1 second.
- The crawl changes no data (tested).

The scorer does not count a basic-check PASS as a false pass: "the page loads" says nothing about a
feature (see `benchmark/score.py`).

## Step 3: function testers

- `tester.py`: one thread per tester, its own browser and login, a fresh model context for each task. Tools:
  click, fill, select, check, press, goto (map pages only), observe, finish. Code rules: element IDs must be on
  the map and on the page; loop guard (same action 3 times on an unchanged page, 18 steps, 150 s); after a save the tester must reload and confirm; code takes
  the screenshot, URL and server responses; PASS with a 5xx becomes FAIL; a blocked write becomes BLOCKED;
  the login form may write to its own endpoint (paths from the crawler's login). Each task saves its model
  conversation in `evidence/`.
- `llm.py`: one model per team through aiRouter (testers DeepSeek-V4-Flash; judge, UI and planner Qwen3.8).
- The checker tests a form as one task, and the same control on many pages (header buttons, "Add to cart")
  as one task that covers all copies.

## ShopLite: today's agent and v2 (2026-11-03, final)

Hand-checked (the scorer links by keywords and over-counts). Writes on (the ShopLite charter allows them).

| | Today's agent (run_pipeline) | v2 plain code only | v2 full (full run 3) |
|---|---|---|---|
| Real bugs caught (bug build) | 1 of 22 | 8 of 22 | **14 of 22** |
| T0 bugs (money, login, permissions, deletes) | 0 of 5 | 0 of 5 | 1 of 5 (B02) |
| Real false passes | 0 | 0 | 0 |
| False alarms (clean build) | 6 of 69 (9%) | 0 of 233 | 2 of 247 in run 3 (both from a shared account; fixed) -> **0 of 236** in the final run |
| Results BLOCKED | 60 of 67 | 4 | 4 |
| Time | 13 min | about 1 min | 23 min (limit 25) |

The final run (bug and clean build at the same time, half the model throughput each) confirmed the clean
build (0 FAIL of 236) but finished only one function task on the bug build: throughput, not quality, is
the limit. Full run 3 (one run alone) is the representative bug-build number.

## Known limits of step 2

- Links made only by JavaScript click handlers are not followed (single-page apps).
- SSO, CAPTCHA and OTP logins are not supported.
- External links and downloads are not checked.
- Links that look like they change data (log out, delete) are not followed; they stay gaps for the testers.
- No tester team exists yet, so each button, field and form ends as NOT TESTED with the reason
  "no tester team yet".

## Run it

One run from the command line (folder above the repo):

```bash
python -m react_qa_agent.v2.run --url https://app.example.com --secrets login.env \
    [--allow-writes] [--docs product_docs/] [--files upload_samples/] [--manual] [--minutes 25]
```

`login.env` holds `SITE_EMAIL=...` and `SITE_PASSWORD=...`; code types them, no model sees them.
The run folder gets `report.html` (open this), `report.docx`, `report.txt` (for an AI that fixes bugs),
`report.json`, `results.json`, `site_map.json`, `board.db`, and `evidence/` and `screens/`.

## Host it on a VM

```bash
cd react_qa_agent
docker build -f v2/Dockerfile -t qa-agent-v2 .
docker run -d --restart unless-stopped -p 8080:8080 --shm-size=1g \
  -e AI_ROUTER_API=<key> -e V2_API_TOKEN=<a long random token> \
  -v qa_runs:/data/runs --name qa-agent-v2 qa-agent-v2
```

Open `http://<vm>:8080/?token=<token>` to start runs, or use the API (see `v2/server.py`). Put the VM
behind HTTPS (a reverse proxy) before you give the address to a client: the token travels in the URL.
Each run starts about 9 browsers (4 for the crawl, then the testers); give the VM 4 CPU and 8 GB of memory
for one run at a time (`--parallel 1`, the default).
