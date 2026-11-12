# Judge playbooks

The judge decides PASS / FAIL / UNCERTAIN from evidence only, with a different model family. These
playbooks give it clear rules for what counts as a bug.

## Load into the judge prompt

- **exploratory-testing** (`_vendor/exploratory-testing/SKILL.md` + `references/heuristics-and-automation.md`):
  the HICCUPS and FEW HICCUPS heuristics. Use them as the test for "is this a defect?" — a result
  that breaks History, Image, Comparable product, Claims, User expectations, Product, or Standards
  is a bug. This keeps the judge's reasons consistent.
- **payment-testing** (`_vendor/payment-testing/SKILL.md` + its `references/`): the correct behaviour
  of a checkout against sandbox cards (Stripe test cards, 3DS, webhooks, multi-PSP). Use it to judge
  whether a payment result is right, not only whether the page looked right.

## What to take, what to skip

- Take: the HICCUPS rules, the payment-correctness rules.
- Skip: any step that sends a real payment. Use sandbox cards only, and only when writes are allowed.
