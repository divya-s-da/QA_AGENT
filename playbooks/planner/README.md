# Planner playbooks

The planner detects the site type, ranks risk, and seeds the task board. These playbooks feed that.

## Load into the planner prompt

- **risk-based-testing** (`_vendor/risk-based-testing/SKILL.md`): rank features by risk, so the
  high-risk ones get tasks first. Pairs with our catalog risk (T0/T1/T2).
- **exploratory-testing** (`_vendor/exploratory-testing/SKILL.md` + `references/session-templates.md`):
  write a test charter per journey (a goal, an area, a time box). Use the charter shape for the
  task-board task text.
- **test-exploratory** (`_vendor/test-exploratory/SKILL.md`): compare guest and logged-in behaviour
  in separate sessions. Add this as a standard edge case the planner always seeds when a login exists.

## What to take, what to skip

- Take: the charter format, the risk ranking, the guest-vs-logged-in edge case.
- Skip: any tool-install or framework steps. We drive the browser ourselves.
