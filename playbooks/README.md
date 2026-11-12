# Playbooks

Open-source QA playbooks to load into the team system prompts of the v2 agent.

These are **prompt text**, not installed skills. Our agent is Python + LiteLLM, not Claude Code,
so we do not "install" a SKILL.md. Instead, each team's system prompt loads the parts of a
playbook that team needs. Load only what the team needs, not the whole file.

## Layout

- `_vendor/<skill>/` — a verbatim copy of each permissively licensed skill, with its `LICENSE`
  and a `SOURCE.md` (repo URL, commit, license). Do not edit these; update by re-copying.
- `<team>/README.md` — for each team (planner, testers, uiux, judge, reporter): which vendored
  skills that team loads, and which parts.
- `ui-test.notes.md` — our own summary of the ideas in `browserbase/skills` (ui-test). That repo
  has **no license file**, so we do not copy its text. We use the ideas only.

## Skill → team map

| Skill | Source | License | Teams | Use it for |
|---|---|---|---|---|
| dogfood | vercel-labs/agent-browser | Apache-2.0 | testers, reporter | exploratory flow; `references/issue-taxonomy.md` = finding categories; report template |
| exploratory-testing | petrkindlmann/qa-skills | MIT | planner, judge | test charters; HICCUPS / FEW HICCUPS bug rules; session-based management |
| risk-based-testing | petrkindlmann/qa-skills | MIT | planner | rank what to test first by risk |
| payment-testing | petrkindlmann/qa-skills | MIT | judge | checkout tests against payment-provider sandboxes (Stripe test cards, 3DS, webhooks) |
| exploratory-web-testing | randykaskuser/qa-agent-skills | MIT | reporter | URL-only → observed-behaviour spec → base for the user manual |
| test-exploratory | kensaurus/cursor-kenji | MIT | planner | guest vs logged-in comparison with separate sessions = a standard edge case |
| ui-test (ideas only) | browserbase/skills | **no license** | planner, testers, uiux | assigned numbered lists; balance work; adversarial patterns; UX heuristics. See `ui-test.notes.md`. |

## License rule

- MIT and Apache-2.0 text is copied here with its license and a `SOURCE.md`. Keep both if you move it.
- `browserbase/skills` has no license. We did not copy it. We wrote our own notes from its ideas.
  If browserbase adds a permissive license later, we can vendor it the same way.

## How a team loads a playbook

1. Open the team's `README.md` here.
2. Read which vendored files it lists and which sections matter.
3. Put those sections into that team's system prompt. Trim to what fits the token budget.

Tools (not playbooks) from the same research — ZAP baseline, Katana, axe-core, Big List of Naughty
Strings, Midscene, WebJudge, Allure — are tracked in `docs/V2_WORKLOG.md`, not here.
