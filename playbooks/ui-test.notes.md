# ui-test — ideas only (not vendored)

Source: `browserbase/skills`, skill `ui-test`. That repo has **no license file**, so we do not copy
its text. This file is our own summary of the ideas, written from the public description. Ideas and
facts are free to use; the wording here is ours. If browserbase adds a permissive license, vendor the
real file the same way as the others.

## The ideas worth using

1. **Assigned numbered lists, not open exploration.** Each tester gets a fixed, numbered list of
   tests to run, not a free "go find bugs" brief. This matches our task board: a tester claims a
   task, not a page. We already do this.

2. **The slowest agent is the bottleneck.** Split the work evenly across the testers, so one tester
   is not still running while the rest are idle. Planner note: balance task weight, not task count.
   This is the same lesson as our 6-slot limit — depth is lost when the board is unbalanced.

3. **Adversarial patterns** — a checklist of ways a control breaks. Use for the tester teams:
   - Forms: empty submit, wrong types, too-long input, special characters, double submit, submit
     while a field is still loading.
   - Modals: open then navigate away, open two at once, close with the keyboard, focus trapped inside.
   - Navigation: back and forward after an action, deep link to a logged-in page, reload mid-flow.
   - Keyboard access: tab order, focus visible, every control reachable and operable without a mouse.

4. **Usability heuristics** — for the UI/UX team, to judge what a rule cannot measure:
   - Nielsen's 10 (visibility of system status, match to the real world, user control and freedom,
     consistency and standards, error prevention, recognition over recall, flexibility, minimal
     design, help users with errors, help and documentation).
   - Laws of UX (for example Fitts, Hick, Jakob, Miller) as named lenses, not hard rules.

## How to apply

- Testers: add the adversarial checklist to the tester prompt as extra edge cases per control.
- Planner: balance the task weight across testers.
- UI/UX: use the heuristics as the judgement lens for a finding a measured rule cannot catch.
