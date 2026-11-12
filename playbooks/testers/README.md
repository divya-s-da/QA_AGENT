# Tester playbooks (function and journey teams)

The testers run their assigned tasks and record evidence. These playbooks shape how they explore
and what they call a bug.

## Load into the tester prompt

- **dogfood** (`_vendor/dogfood/SKILL.md` + `references/issue-taxonomy.md`): the exploratory flow
  (open, act, observe, record) and the issue taxonomy. Use the taxonomy as the finding categories so
  every tester labels a bug the same way.
- **ui-test ideas** (`../ui-test.notes.md`): the adversarial patterns for forms, modals, navigation,
  and keyboard access. Use them as a checklist of ways a control can break.

## What to take, what to skip

- Take: the taxonomy labels, the adversarial checks, the "record full repro evidence" rule.
- Skip: the `agent-browser` CLI calls in dogfood. We use our own browser layer (Playwright).
- Note: dogfood is Apache-2.0. Keep `_vendor/dogfood/LICENSE` if you move the text.
