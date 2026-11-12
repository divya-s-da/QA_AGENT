# Product Plan: Supervised AI QA

Status: draft v2, 2026-11-01. Owner: TBD.

This is a **product**, not an internal tool. It must work for any company
and any web app. Our own 2 testers are the first users (dogfood). Their
feedback and their measured hours prove the product before outside
customers use it.

## 1. Product promise

> Each QA tester goes from about 8 hours of manual testing a day to about
> 1 hour of supervision, and the product **never shows a green result it
> cannot prove**.

- The **agent** does the volume work: it writes tests, runs them, explores
  the app, triages failures, collects proof and writes the documents.
- The **tester (supervisor)** owns truth, risk and accountability. They
  approve what "correct" means, review everything the agent is not sure
  about, witness the critical tests, and sign off the release.

Research (the Perplexity summary, arXiv 2605.01471) shows that AI QA
agents fail at judgment, intent and proof, and that the most dangerous
failure is a silent false pass. Most tools hide this. Our product makes it
the core feature.

### 1.1 Why a customer picks us

| Differentiator | What it means for the customer |
|---|---|
| Honest verdicts | `UNCERTAIN` and `HEALED-PASS` are shown, never hidden inside PASS. Every PASS has machine proof |
| Built-in supervisor workflow | A review queue, approvals and sign-off are part of the product, not an afterthought |
| Safe on production | The write guard blocks POST/PUT/PATCH/DELETE in read-only runs. Customers can point it at prod |
| Business proof | Critical flows are checked through the API or DB, not only by a success message on screen |
| No lock-in | Approved tests export as standard Playwright scripts |
| Measured trust | Each customer sees the false-pass rate, the catch rate and the hours saved |

### 1.2 Market (not verified in depth)

Competitors include QA Wolf (managed service), Momentic, testRigor, mabl,
Testsigma, Functionize, and open-source testzeus-hercules and
vostride/agent-qa. Octomind may have shut down in 2026 (not confirmed).
Public pricing is unclear: reports range from about $125/month self-serve
to about $30K/year contracts. A real competitor study is task 0.6.

## 2. Product rules (the agent must always follow these)

1. **Expectations are locked.** Each expected result has a source ID: a
   business rule (`BR-012`), a spec, WCAG, or HTTP semantics. Only a
   supervisor can change an expected result. The agent can only propose a
   diff.
2. **The agent may change HOW it reaches a target, never WHAT it expects.**
   Locator repairs are logged and labelled `HEALED-PASS`. Adding, removing
   or skipping a step, or changing an expected value, needs approval.
3. **Only proven passes are green.** A PASS needs machine evidence for each
   condition. "No error seen" is not a pass.
4. **Uncertain is a verdict.** `UNCERTAIN` never counts as a pass.
5. **The agent does not decide that a failure is flaky, by design, or not a
   bug.** It proposes. A supervisor decides.
6. **No check disappears without a record.** Checks at the end = checks at
   the start + checks added. A mismatch fails the run.
7. **Tier-0 flows need a backend proof** (order exists, amount correct,
   permission refused) when the customer gives API or DB access.
8. **Production is read-only.** Writes only on staging, with seeded test
   accounts, when the project allows it.
9. **Every run has a budget:** max actions and turns for each check, a
   dollar cap for each run, a wall-clock limit. On exhaustion the verdict
   is `BLOCKED`, never PASS.
10. **Customer data stays isolated.** No run can see another customer's
    browser, credentials, evidence or results.

## 3. Verdict model

| Verdict | Meaning | Green? | To review queue? |
|---|---|---|---|
| `PASS` | Every condition met with machine evidence | Yes | Sampled only |
| `HEALED-PASS` | Passed, but by a different element or path than the saved one | No, until approved | Yes |
| `FAIL` | Evidence contradicts the expected result | No | Yes, with repro bundle |
| `UNCERTAIN` | Evidence incomplete, or the verifier disagrees with the navigator | No | Yes |
| `BLOCKED` | Could not run: environment, data, budget, tool. Reason named | No | If it repeats |
| `FLAKY-CANDIDATE` | Result changed between re-runs | No | Yes |
| `OBSERVATION` | Looks wrong but has no traceable expectation | — | Yes, as a suggestion |

Each `FAIL` gets a proposed root cause (`app_bug`, `test_bug`,
`environment`, `data`, `agent_error`, `flake`, `unknown`) with evidence.
A supervisor confirms it.

## 4. The supervisor's day (target)

| Task | Time |
|---|---|
| Clear the review queue for their area | 35 min |
| Approve new tests and changes to expected results | 10 min |
| Spot-check a random sample of PASS results | 10 min |
| Read the daily summary | 5 min |

Per release: witness Tier-0 flows from video and backend proof (30-60
min), then sign off (15 min). Per new app: approve the charter (about 1
day).

These are targets, not facts. Task 0.3 measures the real baseline with
our 2 testers before we promise any number to customers.

## 5. Where the current code stands

The current repo (`react_qa_agent`) is a good **prototype** of the verdict
core. It is not yet a product. Review of 2026-11-01:

### 5.1 Keep (product value)

- Write guard (`tools/write_guard.py`): fail-closed, a real differentiator.
- Charter as the source of expected behaviour (`tools/charter.py`).
- Verifier split, evidence-by-tool, coverage contract, invariants,
  injected-bug benchmark idea, budget and stall control.
- Credentials never reach the model (`tools/site_login.py`).

### 5.2 Defects that cause false passes (fix first)

All in `run_pipeline.py`:

1. Verifier output replaces the whole verdict list; checks without a
   `VERDICT:` line disappear (around line 905).
2. Verifier lines are paired with checks by position, not by
   `CHECK <index>` (around line 186). Prompt and message also ask for
   different formats.
3. Checks without `expected_source` keep PASS/FAIL instead of becoming
   observations (around line 907).
4. Without `--verify`, the navigator grades its own work (line 430).
5. The flake gate sets `DISCARDED` by itself, and re-runs only the first
   3 FAILs and no PASS results (around line 999).
6. `BudgetController.record_step()` is never called, so only the wall
   clock limits a run (line 848).

Found during Phase 0 (2026-11-01):

7. Black-box mode (no `--repo-url`) tests only 4 fixed routes: `/`,
   `/login`, `/register`, `/cart` (`FALLBACK_ROUTES` in
   `tools/deterministic_mapper.py`). Every other page of the app is never
   a screen. A general product needs real route discovery.
8. A run that aborts (charter or LLM check) still exits with code 0, so
   CI would show it as green.
9. The final `journal_step` uses `p`, `f`, `b` before they are defined; the
   `try/except` hides the error, so the journal never gets its end entry.
10. The navigator prompt used an undefined name `standard_file`, so every
    run crashed on its first screen with a NameError (found 2026-11-03 by the
    first baseline attempt; fixed: the prompt now asks for the check's own
    `standard_file`).

### 5.3 Blockers for a product

| Area | Now | Needed |
|---|---|---|
| Isolation | One shared browser on CDP 9222 for everything; one run at a time | One isolated browser (container) per run; parallel runs |
| Tenancy | `user_id = "pipeline_user"`, no accounts | Organisations, projects, users, roles |
| Secrets | `SITE_EMAIL`/`SITE_PASSWORD` in the server `.env` | Per-project secret vault, encrypted, never in logs |
| LLM | Locked to aiRouter.ch with DeepSeek/Qwen | Provider choice per customer (incl. bring-your-own key, region); a different family for the verifier |
| App support | Mapper is React-specific and needs a repo | Black-box first: any web stack from a URL; repo access optional |
| Foundation | `google-adk==2.5.0` with a monkey-patched SSE server | Decide in task 0.4: keep ADK or own a small runner on Playwright + LiteLLM |
| Storage | Evidence in `~/.react_qa_screenshots`, DOCX in repo root | Object storage, retention rules, delete on request, PII masking in screenshots |
| Interface | CLI + DOCX | Web app, REST API, CI integration, exports |
| Tests | 2 test files | Unit + benchmark tests in CI for every verdict rule |
| License | No LICENSE file | Decide (proprietary / open core) |

### 5.4 License warning

- testzeus-hercules is **AGPL-3.0**. If we put its code in a hosted
  product, we must publish our full source. Use ideas only.
- vostride/agent-qa is **FSL-1.1-ALv2**. It forbids use in a competing
  commercial product for 2 years after each release. Use ideas only.

## 6. Product architecture (target)

```
                    ┌────────────── WEB APP ──────────────┐
 Supervisors ─────► │ review queue · approvals · reports  │
                    │ charter editor · dashboards · admin │
                    └──────────────────┬──────────────────┘
 CI (GitHub Actions,                   │ REST API
 GitLab) ─────────────────────────────►│
                    ┌──────────────────▼──────────────────┐
                    │ CONTROL PLANE                       │
                    │ orgs · projects · suites · charters │
                    │ run scheduler · budgets · audit log │
                    │ secret vault · approvals ledger     │
                    └───────┬──────────────────┬──────────┘
                            │ run jobs         │ results
                    ┌───────▼────────┐  ┌──────▼──────────┐
                    │ RUN WORKERS    │  │ EVIDENCE STORE  │
                    │ 1 container +  │─►│ traces, HAR,    │
                    │ 1 browser/run  │  │ video, DOM,     │
                    │ write guard    │  │ screenshots     │
                    │ navigator      │  └─────────────────┘
                    │ assertion eng. │
                    │ verifier       │──► LLM gateway (provider per org)
                    └────────────────┘
 Integrations: Jira / Linear / GitHub Issues · Slack · Playwright export
```

### 6.1 Core components

**A. Charter v2.** Business rules (ID, text, source, risk tier), risk
tiers (T0 money/auth/permissions/deletion/compliance, T1 core, T2
cosmetic), roles with login states, state recipes (`overdue_invoice`),
backend oracles (API calls or SQL that prove an outcome), environments,
known issues, by-design. The agent drafts rules from PRDs, tickets and a
site crawl; drafted rules stay `proposed` until approved.

**B. Test authoring.** Tests are YAML steps with explicit assertions and
stated values. A drafted test is kept only if it:

1. runs to a clear verdict,
2. gives the same verdict in 3 runs,
3. has at least one falsifiable assertion with a concrete value, and
4. keeps every value stated in the rule.

Each test stores a hash of its expectations; a changed hash without an
approval record fails the run.

**C. Execution modes.** *Replay* (saved actions from approved runs, the
release gate, strict unique locator match), *Agentic* (new tests and
repairs; best result `HEALED-PASS`), *Exploratory* (hypothesis-driven
with a budget; candidate findings only).

**D. Assertion engine (code, not prompt).** Text, URL, count, attribute,
ARIA state, network status, console errors, visual baseline, API/DB
post-conditions. Results go to a ledger. A ledger FAIL cannot be turned
into PASS by any model. LLM-only judgments are labelled `ai-judged`.

**E. Action record.** Before each action: intended element (role +
accessible name + expected next state). After: compare with what was
clicked and what followed. Mismatch → `HEALED-PASS` or `UNCERTAIN`.

**F. Verifier.** Different model family. Sees the expectation and machine
evidence only, never the navigator's verdict or narrative. Disagreement →
`UNCERTAIN`.

**G. Triage.** Deterministic root-cause rules first, then the LLM.
Duplicate clustering. Repro bundle: steps, trace, HAR, video, one-line
replay command. Bug tickets are drafted, then filed after approval.

**H. Specialist checks** (findings for review, never risk acceptance):
axe-core + keyboard checks, security headers/cookies + role permission
matrix + passive scan on staging, Web Vitals and API latency, OpenAPI
contract checks.

**I. Documents.** JUnit XML with strict exit codes, release report
(PDF/DOCX), traceability matrix (rule → tests → result → evidence),
defect log, coverage map, sign-off record.

**J. Trust metrics, shown to each customer.** Injected-bug catch rate,
false-pass rate from sample audits, false-fail rate, escaped defects,
queue size, supervisor minutes per day, cost per run.

### 6.2 Enterprise requirements (before outside customers)

SSO, roles (admin / supervisor / viewer), audit log of every approval,
data retention and deletion, PII masking, no training on customer data,
data region choice, a third-party pentest of our own product, and a path
to SOC 2.

## 7. Phases

Each phase has an exit test. Do not start the next phase until it passes.

### Phase 0: Foundations (decisions and baselines)

- 0.1 Commit the current work on a branch; add CI with the existing tests.
- 0.2 Build the **benchmark**: a test web app with 20+ planted bugs, 5 of
  them T0, several designed to cause false passes (wrong button with a
  success toast; "saved" shown but nothing saved). Run the current agent;
  record catch rate and false-pass rate.
- 0.3 **Time study:** the 2 testers log their hours by activity for 2
  weeks (test design, execution, regression, bug reports, retests,
  documents, meetings). This is the real baseline for the 8-to-1 promise.
- 0.4 **Architecture spike:** ADK vs our own runner on Playwright +
  LiteLLM. Pick one, with a short written reason.
- 0.5 License decision for our code.
- 0.6 Competitor study: features, pricing, how each handles false passes.

**Exit:** baseline numbers (benchmark + tester hours) and written
decisions for 0.4 and 0.5.

### Phase 1: Honest verdict core

- Fix the 6 defects in section 5.2.
- Add `UNCERTAIN`, `OBSERVATION`, `FLAKY-CANDIDATE`; the check-count
  invariant (rule 6); the verifier on by default; budgets for each check.
- One regression test per fix; the benchmark in CI.

**Exit:** every planted false-pass bug gives FAIL or UNCERTAIN, never PASS.

### Phase 2: Real proof

- Assertion engine and ledger (D), action record and `HEALED-PASS` (E),
  verifier on machine evidence (F).
- Trace, HAR and video for each check.
- Backend oracles for T0 flows.

**Exit:** false-pass rate on the benchmark is 0 for T0 and < 2% overall.

### Phase 3: Platform core

- Control plane: orgs, projects, suites, runs, REST API, audit log.
- Run workers: one container and one browser per run; parallel runs;
  the write guard inside each worker.
- Secret vault; evidence store with retention.
- LLM gateway with provider choice per org.
- Black-box onboarding for any web stack from a URL; repo optional.

**Exit:** 3 different apps (React, a server-rendered app, a SPA from
another stack) run in parallel with no shared state, and the benchmark
results are unchanged.

### Phase 4: Supervisor app

- Web app: review queue (evidence first, proposal second), approve /
  reject / reclassify, expectation diffs and approvals, time logging.
- Charter editor and onboarding wizard.
- Reports and documents (I); CI integration (GitHub Actions first);
  Jira / Linear / GitHub Issues; Slack summary.

**Exit:** a tester clears a full benchmark queue in < 35 minutes.

### Phase 5: Dogfood with our 2 testers

- Each tester owns one product area.
- Shadow mode: they keep their manual process and also use the product,
  for at least 3 releases. Compare the findings.
- Weekly feedback session; fix the top 3 problems each week.
- Then step-down: one area at a time moves to the 1-hour model.

**Exit (for each area):** no T0 escape in 3 releases; the product finds
at least 95% of what the tester found manually; sample-audit false-pass
rate < 1%; tester time inside the section 4 target, measured against
the 0.3 baseline.

### Phase 6: Authoring, state and replay

- Rule drafting from PRDs/tickets with approval; keep-gates (B).
- Setup/teardown hooks, state recipes, role login states.
- Replay cache; environment preflight; visual baselines.
- Export of approved tests to Playwright.

**Exit:** a new app goes from sign-up to a first approved T0 suite in
< 1 day; the same build gives the same verdicts in 5 replay runs.

### Phase 7: Exploratory and specialist checks

- Exploratory mode (C); specialist checks (H).

**Exit:** exploratory mode finds at least half of the planted bugs that
the scripted tests miss.

### Phase 8: Design partners and launch

- Enterprise requirements (6.2).
- 2-3 outside design-partner companies, each with a named supervisor.
- Pricing and packaging decision based on the cost-per-run data.
- Public trust metrics page from the benchmark.

**Exit:** each design partner meets the Phase 5 exit test on their own
app, and the pentest has no open high findings.

Phases 6 and 7 can start in parallel with Phase 5 once Phase 4 is done.

## 8. Risks

| Risk | Control |
|---|---|
| Supervisors approve without reading | Sample audits; evidence shown before the proposal; planted trap items |
| Queue overload hides real bugs | Clustering and dedupe; T0 first; queue size is a tracked metric |
| Model change breaks behaviour | Pin model versions; benchmark gate before any model change |
| Customer staging data drifts | Seeded data per run; teardown hooks; isolated accounts |
| Cost per run above price | Replay mode needs no LLM for normal regression; budgets per run |
| Cross-customer data leak | Container per run; vault per project; isolation tests in CI; pentest |
| Agent damages a customer system | Write guard fail-closed; prod read-only; destructive mode only by explicit project setting |
| Testers lose product knowledge | Free hours go to exploratory sessions; rotate areas |
| License contamination from AGPL/FSL code | Ideas only; code review check for copied code |

## 9. Open decisions

1. Which of our own apps do the 2 testers test now? These are the dogfood
   apps.
2. Do we have staging with API or database access for them?
3. Which CI and bug tracker do we use internally?
4. Hosting: SaaS only, or also self-hosted for enterprise?
5. Web only at launch, or also mobile? (Recommendation: web only.)
6. Who owns the product (product decisions, pricing)?

## References

- Perplexity summary of AI QA failure points (user-provided, 2026-11-01).
- test-zeus-ai/testzeus-hercules (AGPL-3.0): Gherkin input, assertion
  ledger that overrides the LLM, observer invariants, API/SQL/security
  agents.
- vostride/agent-qa (FSL-1.1-ALv2): met/not-met/unverified conditions,
  guards that can only lower a verdict, `healed` status, keep-gates,
  action replay cache, root-cause classes.
- Competitor pricing: secondary sources only (qodex.ai, mojoauth.com,
  bugbug.io comparison pages), not verified.
