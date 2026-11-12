# UI/UX team playbooks

The UI/UX team checks layout, design, and usability. Our `v2/design.py` already measures layout and
runs axe-core. These playbooks add the human-judgement checks on top.

## Load into the UI/UX prompt

- **ui-test ideas** (`../ui-test.notes.md`): the usability heuristics (Laws of UX, Nielsen's 10).
  Use them to judge a screen a tester cannot measure with a rule (clarity, feedback, consistency).
- **dogfood** (`_vendor/dogfood/SKILL.md`): the visual and UX parts of its issue taxonomy, so a
  design finding is labelled the same way as a function finding.

## What to take, what to skip

- Take: the heuristic list, the visual-issue categories.
- Skip: anything that needs a tool we do not run.
- Remember: a design finding must still carry proof (a screenshot plus the rule or heuristic it breaks).
