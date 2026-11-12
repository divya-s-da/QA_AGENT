# Reporter playbooks

The reporter writes the "safe to demo?" report and, on request, the user manual. These playbooks
shape both.

## Load into the reporter prompt

- **dogfood** (`_vendor/dogfood/SKILL.md` + `templates/dogfood-report-template.md`): a report shape
  with full reproduction evidence per finding. Our `v2/report.py` already does most of this; use the
  template to check nothing is missing (steps, screenshot, expected vs actual).
- **exploratory-web-testing** (`_vendor/exploratory-web-testing/SKILL.md` +
  `references/observed-spec-format.md` + `references/exploration-method.md`): turn a URL and the
  observed behaviour into a spec. This is the base for the on-request user manual: the passed
  journeys become the "how to use it" steps.

## What to take, what to skip

- Take: the report evidence checklist, the observed-behaviour spec format.
- Skip: the Qase CSV export (we removed it; we do not use Qase).
