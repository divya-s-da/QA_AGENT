# QA Agent v2: work log

Started 2026-11-03 16:00 IST. Demo deadline 2026-11-04 16:00 IST. Target: done by 14:00, 2 h buffer.
Scope now: the main agent only. UI/UX, hosting (VM) and log formats come later.

## Plan (time boxes)

| Box (IST) | Step | Goal |
|---|---|---|
| 16:00-18:00 | 6 Judge | always on, other model family, evidence only, by task ID |
| 18:00-21:00 | 4 Planner | product type, guessed demo path, risk ranking, edge-case tasks |
| 21:00-23:00 | 7 Reporter | verdict line first; critical bugs, not tested, human look, demo path, coverage; DOCX + TXT + JSON + HTML |
| 23:00-02:00 | 5 Journeys + UI/UX | flows across pages; layout and mobile with a vision model |
| 02:00-05:00 | Benchmark | full ShopLite runs (bug + clean), fix what they show |
| 05:00-08:00 | Product shape | one command, packaging for the VM, docs |
| 08:00-14:00 | Buffer | fixes, final runs, this log |

## Starting point (2026-11-03 16:00)

Branch `v2/step2-crawler-board`, commit 48de6f1. Steps 2-3 built. ShopLite: today's agent 1/22 bugs,
9% false alarms; v2 10/22 (hand-checked), 2% false alarms (3 FAIL, cause fixed), 1 real false pass (B02).

## Log

### 16:00-16:10 Step 6, the judge: done
- Built `v2/judge.py`: testers pin results as DONE, two judge threads (Qwen3.8, other family than the
  DeepSeek testers) decide from the evidence and the screenshot, never from the tester's verdict.
- Stuck: on the B02 evidence the judge first gave "no answer". Cause: Qwen3.8 reasons at length and hit the
  2,000-token cap, so its tool arguments were cut off. Tackled: cap 5,000, "one short reason per check".
  Then it found the wrong total ("2 x $20 + ... miscalculated") that the tester had passed.
- Trade-off: a judgment takes about 40 s (reasoning model + image). Two judge threads keep up with three
  testers; a faster judge (Qwen3.6) is possible if the judge becomes the bottleneck.
- Rule in code: "no judge answer" or "low confidence" never shows PASS; tester FAIL vs judge PASS -> human.
- Working: 7 judge tests pass; real call on the last run's evidence: FAIL, FAIL, FAIL (all correct).

### 16:10-16:13 Measured page hygiene in plain code
- Added basic checks: form fields without a name, skipped heading levels, sideways scroll at 1280px.
  Catches B16, B17, B18 with no model.
- Trade-off: the clean ShopLite home goes h1 -> h3 (product cards). That is common and not a WCAG failure,
  so a one-level skip is a note in the PASS line; a skip of two or more levels is a FAIL.
- Scorer fix: keywords no longer match URL paths ("/orders" linked an overflow FAIL to the order-delete bug).
- Working: plain code alone 8 of 22 bugs, 0 false alarms on the clean build (233 results).

### 16:13-16:21 Steps 4, 5, 7: planner, journey and UI/UX teams, report
- Planner (`v2/planner.py`): overview call (Qwen3.8) -> product type, demo journeys, risk per page; page calls
  -> edge-case tasks whose element IDs code checks against the map. Journey and look tasks from the demo path.
- Stuck: the first real plan took 314 s and made 52 tasks (one fifth of the run, more than 25 minutes of
  work), with "Log out" planned on every page and 4 identical product pages planned 4 times.
  Tackled: page calls on the fast model (DeepSeek), one page per URL template, max 4 tasks per page and 30
  in all, drop tasks that use the same controls; the planner runs in the background so testers start at once.
- Trade-off: a cap of 30 planned tasks means low-risk features rely on the checker's gap round.
- The plan itself was good: it found the admin-permission test (B03), page 2 (B09) and order delete (B04).
- Teams: journey testers (30 steps, back/reload), UI/UX testers (Qwen3.8 sees screenshots; a `look` tool
  also measures overlap, sideways scroll, tiny text, broken images on desktop and phone).
- Report (`v2/report.py`): verdict line first, then critical bugs with steps + screenshots, not tested and
  why, human look, demo path, coverage, full detail; HTML, DOCX, TXT, JSON, manual.html on request.
  A fourth verdict, NOT CONFIRMED, when nothing failed but demo-path work was not tested.
- Working: 90 tests pass. A full run with all teams comes next.

### 16:21-17:00 Real sites, crawl speed, a thread bug, the API limit, the job server
- Tested on real public test sites (read-only, plain code): the-internet.herokuapp.com, saucedemo.com.
- Stuck 1: saucedemo showed only its login page. Cause: the static host answers HTTP 404 for app routes
  (/inventory.html) while the app renders the page, and product links are `href="#"` script links.
  Tackled: a 404 that renders a real page counts as a page; up to 10 script links per page are clicked
  (read-only) to find routes; hash routes kept. Now 6 pages found after login, no false alarm.
- Trade-off: a `#`/no-href link whose click opens nothing is left to the testers, not a FAIL (it is often a
  menu toggle); only `href=""` is a FAIL.
- Stuck 2: the crawl was slow (36 pages in 3.9 min). Tackled: 4 crawl browsers in parallel -> 79 pages in
  about 1.5 min. Downloads (100 files on one page) were reported as "did not load": now not a failure.
- Stuck 3: in a full run a judge thread died (`sqlite3.InterfaceError`) and testers hit `IndexError`.
  Cause: one SQLite connection shared by threads, reads not under the lock. Fixed + stress test.
- Stuck 4: the full run's function testers all hit their time limit. Cause: aiRouter queues calls beyond
  about 4 at once (measured: 4 at once 2-5 s; 8 at once, one call 79 s), and the run sent up to 8.
  Tackled: at most 4 model calls in flight, 45 s timeout + retry, smaller planned tasks (2-3 edge cases).
- Trade-off: the cap makes each worker wait for a slot; total throughput is the same, but no call stalls.
- Added the inputs of the brief: `--files` (upload tool, model sees file names only), `--secrets` file.
- Added `v2/server.py` for the VM: standard library only, token, queue, run status from the board,
  report download, login file mode 600 and deleted after the run, `board.db` not served. Tested end to end.
- Working: 91 tests pass in the worktree branch `v2/spa-crawl` (merged after the running benchmark).

### 17:00-18:15 Report polish, context size, packaging
- Report: "critical" now means what a client would see in the demo (tester/journey failures on the demo
  path or a high-risk feature; broken pages, files, links on the demo path). Before, an invisible console
  error made a run "NOT SAFE TO DEMO". Plain titles for basic checks, cropped screenshots that open on click.
- Testers: tool results older than the last three are cut to their first lines; old screenshots dropped.
  Calls at step 15 no longer carry the whole history (speed and cost). Journeys get 420 s.
- My mistake: a wait loop (`while pgrep -f "run_benchmark..."`) matched its own command line and waited
  for itself; about 30 minutes passed before I saw that the benchmark had ended. Now I use background
  tasks that notify when they end.
- Packaging: the package import no longer loads ADK (lazy `root_agent`; `adk web` still works). v2 imports
  in 0.07 s. `v2/Dockerfile` (v2 requirements only, non-root, runs in /data/runs), deploy notes in
  `v2/README.md`. Server tests (token, URL check, login file 600, no secrets/board download, traversal).
- Trade-off: the job server is standard library only (no framework) and its token travels in the URL for
  report links; it must sit behind HTTPS before a client gets the address.

### 18:15-19:15 Full run 2: what it showed, and the fixes
- Full run 2 (all fixes so far): clean build 0 FAIL of 246 results (no false alarm). Bug build: hand check
  10 real catches, but the testers added almost nothing: only 7 of 23 function tasks finished in 25 minutes;
  the checker never got its round; 198 items NOT TESTED (each with the reason "time limit").
- Stuck 1: calls averaged 16 s although a test call took 2-5 s. Found: the provider caches the start of a
  conversation (DeepSeek: an identical prefix took 2.3 s, a new one 20 s). My context shortening edited old
  messages at every step, so the prefix changed every time and the cache never helped. Tackled: shorten
  only past 45,000 characters. Also: several tool calls per turn allowed; 6 calls in flight.
- Stuck 2: one tester deleted order #1000, and the journey that needed it then failed ("no orders yet").
  This is the shared-test-data problem the brief warns about. Tackled: tasks that destroy shared data
  (delete, cancel, clear, log out) are claimed last. Full fix needs one test account per tester (open).
- Stuck 3: the planner guessed "register -> home page, logged in"; ShopLite shows "Account created, log in".
  The judge agreed with the FAIL. On a client site this is a false alarm. Tackled: the planner writes only
  what a user sees; the judge marks "different but reasonable" UNCERTAIN, never FAIL.
- Scorer: it now links a bug to the one check about it. Before, words of other checks in the same result
  linked wrong bugs (5 of 15 links in full run 2 were wrong). My hand check stays the authority.
- Environment: four Microsoft 365 app processes (`ms-365-electron`) run at 85% CPU each (load 12). Not
  mine, not stopped. They slow the runs and made two tests fail only under that load.

### 19:15-20:10 Full run 3, packaging check, finishing tonight
- Full run 3 (bug build): 14 of 22 bugs caught (hand-checked), 0 false passes. The testers found B02 (cart
  quantity 6, total for 1: a T0 money bug), B06 search, B08 the "Saved" trap, B09 page 2, B10 price sort,
  B12 dead Reviews tab; plain code found B13, B15-B21. Today's agent: 1 of 22.
- Full run 3 (clean build): 2 FAIL of 247. Both came from testers sharing one account: one tester saved a
  very long profile name, and the header that another tester looked at broke; a cart changed under a
  journey. Tackled: one account per tester (SITE_EMAIL_2 ... in the secrets file, also in the job server);
  ShopLite gets four more test users for the benchmark.
- The user asked to finish today, not tomorrow. My 14:00 target came from the time boxes I set at 16:00,
  not from an estimate of the remaining work; most steps took far less time than their box. Re-planned:
  runs in parallel instead of in sequence, hand-check only what changed.
- Docker image built (2.33 GB) and tested: API, a run inside the container, report download, non-root user.
- The same self-matching wait loop as before happened once more in a background command; this time I saw
  it at once and waited for the result files instead.

### 20:10-20:35 Final runs, real site with testers, done
- Final run, bug and clean build in parallel (3 model calls each): clean build **0 FAIL of 236** (the
  account-per-tester fix removed run 3's two false alarms). Bug build: 9 real catches, because running two
  builds at once halved each one's throughput (one function task finished). Trade-off accepted to finish
  tonight; full run 3 (14 of 22, run alone) is the representative number.
- Real site with testers (saucedemo.com, read-only, 12 min): login, planner ("e-commerce demo store"), a
  passed checkout journey (tester and judge agree), honest verdict NOT CONFIRMED (12 demo-path items not
  tested in 12 minutes). No crash.
- Fix: a UI "look" task chose the logged-out /cart (only a redirect to login); it now uses the logged-in
  page when there is one.

### 20:35 After the hand-over: time limits raised, waits shortened
- On request: the default run limit is 60 minutes (command line, job server, start page); journeys get
  600 s, UI checks 300 s. The wait after each tester action is 2 s at most (was 3 s; pages that poll the
  server always hit the limit). Phase-wise runs and Jev as a compactor: in Reminders, to discuss.

## Is it working now?

Yes, end to end: crawl -> basic checks -> planner -> function, journey and UI testers in parallel ->
judge -> checker -> report (HTML, DOCX, TXT, JSON, manual) -> job server -> Docker image. 97 tests pass.
ShopLite: 14 of 22 real bugs, 0 false passes, 0 false alarms on the clean build (today's agent: 1 of 22,
9% false alarms).

## Open items, in order of value

1. Speed: about a third of the planned tasks finish in 25 minutes. The model provider handles about 6
   calls at once. Options: a second provider or key for more parallel calls, a faster tester model, fewer
   steps per task.
2. T0 bugs: 1 of 5. Placing an order (B01), admin access (B03), cancel-delete (B04), logout session (B05)
   need longer journeys and a check of server state after the action.
3. Login with SSO, CAPTCHA or one-time codes is not supported.
4. UI: the job server has a plain start page; the real UI comes next (inspirations from you).
5. Hosting: the Docker image is built and tested locally; put the VM behind HTTPS before a client uses it.
6. Branch `v2/step2-crawler-board` is local, not pushed.

## 2026-11-03 23:00 Coverage: design rules, a bug catalog, more parallel calls

The client asked for wider bug coverage on any kind of product. Three changes, all plain code (no model
slot), built and tested:

1. **Design checks (`v2/design.py`, rules in `docs/V2_DESIGN_RULES.yaml`).** On the main pages it measures
   the rendered page: things that cover a button or overlap text, cut-off text, sideways scroll, unreadable
   contrast, tiny text, broken or stretched images, placeholder or raw-error text, and the axe-core
   accessibility rules (critical and serious only). axe-core 4.14.0 (MPL-2.0) is vendored in `v2/vendor/`.
   It runs in its own browser at the same time as the testers. T2 polish is not a FAIL. Accessibility
   findings that repeat on the nav or footer are reported one time. The vision model keeps only the
   judgment calls (hierarchy, a confusing flow). Tests: `tests/test_v2_design.py`.
2. **Bug catalog (`v2/catalog.py`, data in `docs/V2_BUG_CATALOG.yaml`).** 27 site types (shop, bank,
   admin, CRM, HR, search, LLM/RAG, booking, LMS, health, ...), 292 bugs with severity, black-box steps
   and the correct behaviour. The planner detects the site type from the page words and seeds the
   applicable critical bugs (T0/T1, task-based) as tester tasks, so the testers always check the
   known-dangerous features. It drops bugs that this run cannot test (two accounts, writes, no login). This
   is where the missed severe bugs now get a check: the logout-session bug (GEN-AUTH-001), the success trap
   (GEN-FORM-001) and client-side price/role (GEN-MONEY-002, GEN-ACCESS-003) are always on the list for a
   site with a login. Tests: `tests/test_v2_catalog.py`. The code-only catalog bugs (headers, cookie flags,
   exposed files, leaked keys) are not seeded yet; they are a separate plain-code job (see below).
3. **The 60-minute run** (started 22:38) gives the testers time to reach the deep bugs. In it the journey
   testers reached the logout-session bug and the profile-save success trap, which the 25-minute run missed.

### Not built yet in this session
- The code-only critical-bug probes (one GET for exposed files, a scan of the page JavaScript for live
  keys, access-by-role and session checks in plain code). An automatic safety filter stopped the file
  twice while it was written, so it was removed. Another session can build it from `docs/V2_BUG_CATALOG.yaml`
  (the `check: code` bugs). The catalog's task-based versions of these still run through the testers.

## 2026-11-04 00:30 Two 60-minute runs, the product UI, and the catalog lesson

- **60-min run, no new code (hand-checked): 19 of 22, T0 4 of 5, 0 real false green.** The scorer links by
  keyword and mislinked, so read the hand check: B01 (order 500) is caught by the code 5xx override; B06
  (search) is caught by a tester FAIL. Missed: B03 (admin role), B07 (register invalid email), B22
  (/product/999 stack trace). This is the best run so far. More time lets the journeys reach the deep bugs.
- **60-min run WITH the catalog (24 seeds): scorer 15 of 22, T0 1 of 5 - worse.** Hand check: only 3 of 20
  catalog tasks finished. The 24 extra risk-3 tasks flooded the board and starved the deep journeys of the
  6 model slots, so B01/B02/B04 were never reached. The catalog did catch B07 (its invalid-email seed) and
  the design checks caught B15-B21, so the mechanism works; the problem is slot starvation.
- **Fix:** `catalog.select` now seeds 10 focused checks (was 24) and pins the four signature bugs
  (GEN-FORM-001 success trap, GEN-AUTH-001 logout session, GEN-ACCESS-003 admin access, GEN-MONEY-002
  client price) so they always run first. The real unlock is more model slots (a founder item).
- **Product UI** (`v2/ui.html`, served by `v2/server.py` at `/?token=`): a terminal-window theme inspired
  by the Typesafe console - start a run, a live runs table, and a report panel (verdict, counts, PASS/FAIL
  bars, summary, links). Preview artifact: https://claude.ai/artifact/LaKUzJoPh7Bepnig9YBuo6 .
- **UI 2026-11-04:** brand color changed to dark rich green (header gradient #052a1c->#1c9460, green
  accents); the runs table becomes stacked labelled cards below 620px wide (phone). Commit 931ab46.
  Local run: from /home/divya, `V2_API_TOKEN=<16+ hex> ~/qa-venv/bin/python -m react_qa_agent.v2.server
  --port <p>`; open `/?token=<token>`. Screenshots: use the headless-shell at
  `~/.cache/ms-playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell`
  with `--no-sandbox` (the full chromium-1248 chrome core-dumps here).

## Open-source building blocks (from the client's repo list, 2026-11-04)

Playbooks are vendored under `playbooks/` (commit 2b2bcd6), sorted by team, with licenses. See
`playbooks/README.md`. Only permissive text was copied (dogfood Apache-2.0; exploratory-testing,
risk-based-testing, payment-testing, exploratory-web-testing, test-exploratory all MIT).
`browserbase/skills` (ui-test) has **no license**, so we wrote our own idea summary, not a copy.

Tools to add to the pipeline (code, not playbooks) - BACKLOG, not yet wired:
- **OWASP ZAP baseline** (passive only): `docker run --rm -v $(pwd):/zap/wrk zaproxy/zap-stable
  zap-baseline.py -t <URL> -j -J zap.json`. NEVER run `zap-full-scan.py` on a customer site (active attack).
- **Katana** (projectdiscovery, MIT): headless crawl that finds endpoints inside JS files (single-page apps).
- **axe-core** (already vendored in `v2/vendor/`): accessibility on live pages.
- **Big List of Naughty Strings** (minimaxir, MIT): `blns.json` as form inputs (fuzz).
- **Midscene.js** (web-infra-dev, MIT): screenshot-only element finding for the UI/UX team; runs on
  Qwen3.x / UI-TARS, self-hostable.
- **WebJudge** (OSU-NLP-Group/Online-Mind2Web): judge method = key screenshots + action history -> verdict.
- **Allure Report** (Apache-2.0): static HTML report; could sit under our "safe to demo?" summary.
- **Impeccable detect** (`npx impeccable detect <URL> --json`): detector only; test if it works behind login.
- Browser layer alternatives to Playwright: Stagehand (MIT), agent-browser CLI (vercel-labs, works from
  the accessibility tree, claims up to 97% fewer tokens).

## Reminders: discuss later (with the founder)

1. **Phase-wise runs with a report after each layer.** Phase 1 (crawl + basic checks) -> report 1 in about a
   minute; phase 2 (high-risk planned tasks, journeys) -> report 2; phase 3 (medium/low tasks, checker gaps)
   -> final report. The client gets a first answer fast and the run can go on as long as needed.
   - The reports themselves need no model: code builds them from the task board.
   - What needs a good **compactor** is the hand-over between phases: a short, correct summary of what is
     known (findings, the guessed demo path, what failed and why) for the next phase's planner and testers.
     Qwen 3.8 and DeepSeek V4 Flash are not good compactors (they drop or bend facts when they shorten).
   - Candidate: **Jev** (OpenRouter `typesafe/jev-router`, already wired in ~/jev-ultrafast) on top, only
     for compaction. It costs money per call: decide with the founder (budget per run, which phases).
2. **A second model key or provider** for more calls at once: the provider handles about 6 at once, and that
   is the main limit on how many planned tasks finish (see Open items, 1).
3. **Default time limit is 60 minutes for now** (the brief's target is 25). Go back to 25 when speed allows.

## 2026-11-04 - Product UI rebuild (Sentry-inspired) + live chat

- Rewrote v2/ui.html as a multi-page app (hash router): Overview, Sessions, Session detail, New test, Settings.
- Design: left sidebar (dark green), top bar with env pill, status badges, verdict pills, stat tiles, data table.
- Fonts: Inter (Google Fonts) with a system fallback. No display/"AI" fonts.
- Brand: dark rich green (--brand #0f3d2c, --accent #167a48).
- Session detail: per-session timer (elapsed + left), status, Stop button, live log, findings tally and list,
  live browser screenshot, download menu (HTML, PDF, TXT, DOCX, JSON), and a chat box.
- Chat box: recommendation chips + free text. Each note posts to a new endpoint.
- Server (v2/server.py): new POST /api/runs/<id>/note -> appends to live_notes.txt and chat.jsonl;
  GET /api/runs/<id> now returns "chat" (history) and a longer log_tail (40 lines).
- Tester (v2/tester.py): _live_notes() reads live_notes.txt fresh before each task and adds it to the task
  prompt as "OWNER LIVE NOTES", so chat guidance changes the tasks that come next.
- Verified: Python parses; UI renders in headless Chrome (overview, sessions, session detail, new test).
- Pending: full test suite run (held back for low memory); write-guard post_data crash fix; safe-write mode.
- The user will send a reference design to copy, and a VM, later.

## 2026-11-09/10 - Audit fixes (safe-write, crash, trustworthy score)

Done and tested (121/121 tests pass):
- C3 post_data crash: v2/crawler.py and v2/tester.py now wrap request.post_data in try/except before
  write_guard.decide (the shared tools/write_guard.py _handle already had this; the two v2 copies did not).
- C2 safe-write mode: tools/write_guard.py now has 3 modes (read-only | safe | allow).
  safe = allow create-shaped POST, block DELETE, PUT/PATCH, and destructive/financial POSTs
  (delete/cancel/refund/checkout/pay/...). Every write let through is recorded in a ledger (writes_made()).
  Wired through v2/run.py (--safe-writes, mode=), v2/server.py (safe_writes field + --safe-writes),
  v2/ui.html (3-way Write mode radio), tester prompt (write_guard.mode_help()). writes.json saved per run.
  6 new tests in tests/test_write_guard.py (SafeWriteTests). Backward compatible: configure(allow_writes=...) still works.
- C6 trustworthy score: benchmark/score.py adds a per-link confidence (high = >=2 distinct keywords,
  low = single generic keyword) and reports caught vs high-confidence-caught, for T0 too. New Confidence
  column. Confirms the audit's point: the trap false_pass links (B01/B05/B14) show as 'low'.

Still open (near-term, need a live browser run to validate - do with the 2-hour benchmark when memory is free):
- C4/C5 two browser sessions + post-action server-state assertions (the T0 lifter).
- Auto-cleanup of safe-write created data (v1 logs every write; it does not yet auto-delete - generic
  auto-delete is risky, so cleanup should only remove resources with a server-confirmed create handle).
- Report HTML section for the write ledger (ledger is in writes.json + results.json today).

NOTE: a running v2/server.py must be RESTARTED to pick up the safe_writes handling (Python has no hot reload);
ui.html is read fresh each request so the new Write-mode control shows on refresh.

## 2026-11-11 - Daily-use features (new-only, multi-key, actionable report)

All tested: 127/127 pass.
- Multi-key LLM pool (v2/llm.py): keys from AIROUTER_API_KEYS (comma), AIROUTER_API_KEY, and AIROUTER_API_KEY_2/_3...
  Per-key BoundedSemaphore of V2_MAX_CONCURRENT_CALLS; round-robin takes a free slot from any key, so total
  concurrency = keys x per-key. One key behaves exactly as before. Ready for the second key (no code change needed).
- Cross-run memory / "new since last run" (NEW v2/memory.py): per-site JSON store keyed by host. Classifies each
  finding as new vs recurring by route + salient-word overlap (Jaccard >= 0.55 and >= 2 shared words). Fail-safe:
  unclear match = NEW (never hide a real new bug). Updates the store ONCE per run (final report only), so interim
  reports do not age findings. 6 new tests (tests/test_v2_memory.py).
- Report shows NEW (v2/report.py): "Since the last run: N new, M seen before" line; NEW / seen-before tags on each
  finding; new findings sorted first (HTML and TXT). report.json carries memory_on/new_count/recurring_count/new_findings.
- UI + server surface the new count (v2/server.py GET returns the counts from report.json; v2/ui.html session page
  shows a "N new - M seen before" chip). The report files carry NEW regardless of the server.

Still open:
- Feature 4 (steady T0): two browser sessions + post-action server-state assertions - needs a live browser run to
  validate; pair with the 2-hour benchmark when memory is free / a second key is in.
- Feature 2 (near-zero false positives): served today by the proof rule + FAIL-vs-UNCERTAIN split; the real lever is
  a live run on a real app to measure and tune.
- Safe-write auto-cleanup of created data (logs every write today; no auto-delete yet).

NOTE: restart a running v2/server.py to activate the live "new" chip (GET change); ui.html shows on refresh.

## 2026-11-11 (cont.) - Accounts, scoped testing, and a system-design review

System-design review done (background agent): core engine (board/judge/tester/report) is well-designed; issues
are in the server (multi-user), cross-run state, failure recovery, throughput. Addressed the mandatory items
for safe multi-user below. Full findings summarized to the user.

Scoped testing (tested, 4 tests):
- Crawler scope: --scope-mode site|section|page. section = the URL's path and under it; page = only the start
  page (no link following). v2/crawler.py _in_scope() + page_only; run.py derives scope from the URL; server +
  ui.html "Test scope" select.

Per-user accounts (tested, 10 tests + live smoke):
- v2/accounts.py: SQLite users + scrypt password hashing + in-memory sessions with expiry (stdlib only).
- v2/server.py: identity via Authorization: Bearer (operator token = sees all; user session = own runs only).
  Endpoints /api/login, /api/logout, /api/me, /api/register (operator-gated, or first-user bootstrap, or
  V2_OPEN_SIGNUP=1). Every run stamped with an owner; all()/get()/file-route/stop/note authorized by owner.
  Live smoke confirmed: alice sees her run, bob sees 0, bob cannot open alice's report (404), operator sees all.
- SSRF guard: server.ssrf_reason() blocks loopback/link-local/private/reserved target hosts (override with
  V2_ALLOW_PRIVATE_HOSTS=1 for local benchmarks). 5 tests.
- Owner-scoped, locked memory (v2/memory.py): key = owner + host; an flock across load->classify->save fixes the
  lost-update race the review found. run.py --owner -> SiteMemory(url, owner).
- Boot reconcile (v2/server.py): on start, mark stranded running/queued jobs stopped and delete leftover
  secrets.env (fixes orphaned-"running" and plaintext-login-after-crash from the review).
- UI: a login screen (session token in a header, not the URL); the token no longer injected into the page; file
  links use the session token scoped to the owner. Avatar shows the user and signs out.
- Accounts DB path override: V2_ACCOUNTS_DB.

Still open (from the review, not yet done): "Stop" still builds no final report (download 404 after a stop);
run()' is a long god-function; the runs-list does heavy work per poll (no pagination); unstructured logging.
Feature 4 (two-session + state assertions for steady T0) still needs a live browser run to validate.

## 2026-11-11 (cont.) - Stop now writes a final report

Fixes review item #9 ("Stop produces no report; download 404s").
- Robust stop signal: the server drops a STOP sentinel file in the run folder; run.py's main loop checks it each
  second (does not rely on a signal, which a library was swallowing). SIGTERM/SIGINT still set the flag as a backup.
- On stop, the run builds a FAST final report (skips the model summary and the PDF) and marks it stopped.
- Fixed a related hang: a ThreadPoolExecutor (planner) atexit-join could block process exit when a model call was
  stuck. The report/results are written first, so main() now os._exit() to exit promptly.
- Server stop(): write STOP -> wait up to 30 s for the run to write its report and exit -> force-kill the group as
  a backstop -> reap any leftover browser (killpg on the saved pgid).
- Verified live against the benchmark: STOP during the crawl -> exit code 2, report.html written, PDF skipped,
  "stop requested" logged, process exited 7 s after STOP.
