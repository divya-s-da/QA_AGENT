# Learnings from Explorbot (testomatio/explorbot) — mapped to our v2

Explorbot is Elastic License 2.0. We reuse the **ideas**, not the code or prompts. This file maps the
learnings to what our v2 already does, names the real gaps, and decides what to adopt and when.

Legend: **HAVE** = already in v2 · **PARTIAL** = some of it · **GAP** = not yet · priority P1 (do soon) /
P2 (worth it) / P3 (later or needs the founder).

## 1. Architecture (code decides strategy, AI decides tactics)
- HAVE: fixed order crawl → plan → test → judge in code; the model only clicks/types/judges.
- HAVE: a separate judge model family; the judge reviews but never acts.
- PARTIAL: model tiers. We have per-team models (`llm.TEAM_MODELS`), but not an explicit cheap/smart/vision
  split where the smart model sees only short summaries.
- GAP (P2): a "decision model" for closed yes/no questions with a probability, acting only above a threshold.
  Our judge already uses a confidence floor (0.6); raising key gates to ~0.7 is cheap.
- GAP (P3): a dedicated blocker-handler agent that gets {asked, where stopped, what tried} and never sees the
  same blocker twice. Today a blocked task becomes BLOCKED and a human reads it.

## 2. Crawler and page understanding
- HAVE: we skip logout/delete/destroy links; we crawl logged-in and logged-out; we reuse the login session.
- PARTIAL: page state key. We key pages by route + auth. Explorbot adds h1/h2 + open-modal name for a finer
  key used by cache, memory and loop detection.
- GAP (P2): tag every interactive element with a stable index (`eidx`) that links HTML ↔ ARIA ↔ screenshot
  label. This would make the tester's element references steadier (see section 7).
- GAP (P2): collapse template pages — treat URLs that differ only by IDs/UUIDs as one; skip a page whose ARIA
  role structure (text removed) is ≥90% like one already seen. Saves slots on big sites.
- GAP (P3): deep research (click up to 10 expandables, record what appeared, restore state) and 6 h/1 h caches.

## 3. Planner
- HAVE: the planner cannot invent elements (`planner.py` strips element IDs not on the page); it skips Log out
  and header/footer controls; it ranks the demo path and high-risk areas first; the catalog pins signature bugs.
- GAP (P1, cheap): grounding words in the planner prompt — "never invent a route name, a success message or a
  validation text; no record IDs; do not plan sign-in / sign-up / password-recovery as test targets."
- GAP (P2): rotate planning styles per round — normal, "curious" (plan for the untouched controls), "psycho"
  (one scenario that gives every control a bad value: empty, 10k chars, unicode, SQL, script tags). The psycho
  style is a ready plan for our BLNS fuzz tool when we wire it.
- HAVE (partial): dedupe. We have reuse-before-retest in the board; we do not yet dedupe by ≥90% page similarity.

## 4. Testers
- HAVE: a loop guard (same state 3 times stops); we compact to the latest ARIA/UI; we record actions.
- PARTIAL: locator ladder. We locate elements, but not the explicit most-stable-first ladder (ARIA/text →
  text-in-container → ARIA-in-container → CSS/XPath → coordinates) with "save what worked".
- GAP (P1, strong): after every action, capture a diff (URL, ARIA, HTML, network, console) and treat **a click
  with no change at all as FAILED**. We capture network/console but do not assert "no change = fail".
- GAP (P1, strong): an ambiguous locator must click **nothing** and return the numbered matches for the agent to
  pick by index — never a hidden tie-breaker. (Explorbot's tie-breaker toggled a switch 4 times and still passed.)
- GAP (P2): first attempt gets the ARIA tree only; add full HTML only after a failure (smaller, cheaper calls).
- HAVE: "no error seen is not a pass" is already in the tester prompt.

## 5. Loops and stalls
- HAVE: same-state-3-times loop guard; a per-task step cap (MAX_STEPS); claims expire and return to the board.
- GAP (P2): a stall counter (same URL + state hash, no successful action) that escalates: 2 stalls → supervisor
  review, 3 stalls → stop and go to final review (not an automatic fail).
- GAP (P2): "4 failures in a row on the same API endpoint = stop"; "do not re-visit a page seen ≥5 times while
  others have <5". Cheap guards against wasting slots.

## 6. Judge and verdicts  (our core promise lives here)
- HAVE: four-ish outcomes (PASS/FAIL/UNCERTAIN/BLOCKED); evidence only; PASS needs proof; a success message the
  server contradicts is a FAIL; absence of evidence is UNCERTAIN; a 5xx during a PASS task flips to FAIL (code).
- HAVE: "if the judge errors, a PASS is not shown" — our combine() turns no-judge-answer on a PASS into UNCERTAIN.
- GAP (P1, ADOPTED NOW): "reaching a page or opening a form is never a PASS; an action (create/save/send/submit/
  order/delete/update) passes only with persisted evidence — a matching successful write (a non-GET with a 2xx
  status) or the data still present after a reload. A success message with no such write is a success trap: FAIL."
  This is the B01 signature bug. Added to the judge system prompt (the judge already sees the server responses).
- GAP (P2): a "contradiction" outcome that quotes both the screenshot and the log when they disagree (today we
  FAIL and explain, without a distinct label).
- GAP (P2): provenance — "the entity cited as proof must appear in this run's own actions; verifying something
  that was already true before the test is rejected." Needs the ledger in section 7.

## 7. Hallucination guards
- HAVE: the proof rule (no screenshot/URL/server → the result is rejected by the board).
- HAVE (schema): the read-only write guard blocks non-GET at the tool layer, not only in the prompt.
- GAP (P1): a write ledger — count successful writes (non-GET, 2xx) in the run; a "done/created" claim with zero
  successful writes is rejected; a claimed ID must appear in a 2xx response. Feeds the section-6 persisted rule.
- GAP (P2): element refs valid for one snapshot only, must match exactly one element, stripped before diffing
  (renumbering alone created phantom changes). Pairs with the `eidx` idea in section 2.
- HAVE: credentials come only from the live session; secrets are typed, never in prompts.

## 8. Speed and cost
- HAVE: one global LLM gate (6 slots) with a queue; prompt caching via a stable system prompt and tool list.
- GAP (P1, cheap): on a request failure, send the provider's error reason back to the model instead of a blind
  retry (blind retries waste generations). Check our llm.chat retry path.
- GAP (P2): turn vision off for the whole session after its first failure; split a truncated researcher output
  into one request per section.
- HAVE: a hard time cutoff with teardown reserved; the report is always written.

## 9. Memory (between runs)
- GAP (P3): knowledge (we write) vs experience (the agent earns), keyed by URL pattern/state hash, one folder
  per site, injected as a table of contents the agent fetches on demand. This is the repeat-run speed-up; it
  pairs with "save passing flows as replayable tests". A founder-scope item.

## 10. Report and user manual
- HAVE: our report groups by section (critical / other / needs-human / not-tested), headlines the verdict not a
  count, and the manual turns passed journeys into steps.
- GAP (P2): group findings by **root cause** (3 failures on the same control = 1 defect) and split Defects / UX /
  Execution issues explicitly. Matches the dogfood taxonomy we vendored.
- GAP (P3): a screencast per test with the step as the chapter title.

## 11. Where we deliberately differ (keep these)
- We run parallel teams on the 6-slot queue; Explorbot runs one browser.
- We keep the write guard and read-only default for customer sites; Explorbot assumes a disposable staging env.
- We keep the crawler-map vs task-board checker so nothing is silently untested; Explorbot does not guarantee
  coverage.

## Adopt order (proposal)
- **P1 now**: (6) persisted-evidence / success-trap judge rule [DONE]; (3) planner grounding words;
  (4) "no change = FAILED" and "ambiguous locator clicks nothing"; (7) write ledger; (8) return the error to the
  model instead of a blind retry.
- **P2 next**: stall counter + per-endpoint and per-page visit caps; locator ladder with memory; template-page
  collapse; report grouping by root cause; decision-model 0.7 gate.
- **P3 / founder**: blocker-handler agent; cross-run memory + replayable tests; screencasts; model tiers with
  summary-only smart models.
