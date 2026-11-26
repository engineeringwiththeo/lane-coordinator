# Roadmap

<!--
The plan above everything else. Milestones are listed in delivery order (that order ranks work in the planner).
Under each "## Milestone" heading put Due/Goal/Exit lines. Each "### key — Title" under it is an epic: it becomes
(or matches) the GitHub issue labelled type:epic + epic:<key>. An epic may carry Lane: and Depends on: lines; any
other text under it becomes the epic issue's description.
Change the plan here, then run `roadmap.py sync` to see the GitHub changes, and `roadmap.py sync --apply` to make
them. Stories and tasks are created under the epics with tracker.py.
-->

## M1 — Foundation
Due: 2027-01-31
Goal: A member can sign up, sign in and see their account.
Exit: Sign-up to first sign-in works end to end in staging; ADR-0001 accepted.

### auth — Accounts and sign-in
Lane: 01
Depends on: ADR-0001
Email and password sign-up, sign-in, password reset and sessions.

## M2 — Billing
Due: 2027-03-15
Goal: Members can pay and download receipts.
Exit: A real card payment succeeds in staging.

### billing — Plans, payments and receipts
Lane: 02
