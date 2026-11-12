# QA Agent v2: demo guide

## What it does, in one sentence

Give it a website address and a test login; it answers "is this product safe to
show?", with proof for every result and a list of what it could not test, and why. The default limit
is 60 minutes for now (the target is 25; see the Reminders in docs/V2_WORKLOG.md).

## Coverage on any kind of product

- It knows 27 kinds of web product (shop, bank, admin tool, CRM, HR tracker, search engine, AI/RAG
  assistant, booking, learning platform, health portal and more; see docs/V2_BUG_CATALOG.yaml). It finds
  the kind from the pages and then always checks the known-dangerous features for that kind: a success
  message with nothing saved, a session still valid after log out, a price or a role changed on the
  client, access to another user's data, and so on.
- Its design checks measure every page: things that cover a button or overlap, cut-off text, sideways
  scroll, unreadable contrast, broken images, placeholder text, and accessibility (axe-core). The design
  of each product differs, so these rules check what is always wrong, not taste.

## Start a run

Command line (from the folder above the repo):

```bash
python -m react_qa_agent.v2.run --url https://app.example.com --secrets login.env --minutes 60
# login.env:  SITE_EMAIL=...  SITE_PASSWORD=...   (more accounts: SITE_EMAIL_2=... SITE_PASSWORD_2=...)
# add --allow-writes only on a test environment; --docs <folder> for product documents;
# --files <folder> for upload fields; --manual for a user guide
```

Or the job server (the VM): `V2_API_TOKEN=<token> python -m react_qa_agent.v2.server --port 8080`, then open
`http://<host>:8080/?token=<token>`, fill in the URL and the login, press Start.

## What to show

1. `report.html` in the run folder: the verdict line at the top (NOT SAFE / SAFE IF YOU AVOID ... /
   NOT CONFIRMED / SAFE), then the critical bugs with steps to reproduce and a screenshot each, then what was
   not tested and why, then what needs a human look, the guessed demo path, coverage by page and team.
2. A critical bug card: the steps were recorded by the tester; the verdict was decided by a judge from another
   model family that never saw the tester's verdict.
3. `report.docx` (for people) and `report.txt` (for an AI that fixes bugs).

## Honest numbers (ShopLite benchmark: 22 planted bugs, 5 of them "success traps")

| | Today's agent | QA Agent v2 |
|---|---|---|
| Real bugs caught (checked by hand) | 1 of 22 | 14 of 22 |
| False passes (said "works" on a broken feature) | 0 | 0 |
| False alarms on the clean build | 9% (6 of 69) | 0% (0 of 236, final run) |
| BLOCKED results | 60 of 67 | 4 |
| First page checked | - | about 1 second |

## What it does not do yet (say it before the client asks)

- In 25 minutes on ShopLite the testers finished about a third of the planned tasks (now the limit is 60); the rest is listed as
  NOT TESTED with the reason "time limit". High-risk work goes first. The model provider is the limit
  (about 6 calls at once).
- 4 of the 5 most severe ShopLite bugs (placing an order, admin access, cancel-delete, logout session) are
  not caught reliably yet; they need longer journeys and more time.
- Logins with SSO, CAPTCHA or one-time codes are not supported.
- One test account is shared by all testers unless more are given; destructive tasks run last to protect
  shared data.
- The UI is a plain start page; the real UI comes next.
