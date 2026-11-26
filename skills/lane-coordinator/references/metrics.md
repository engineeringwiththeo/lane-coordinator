# Metrics, routing and sprint rules

The coordinator owns these files. Workers never edit them.

## Ticket type key: `size:kind:stack`

- **size:** S | M | L (from the backlog estimate).
- **kind:** feature | bug | debt | test | docs | seed.
- **stack:** the highest-risk surface touched, in `config.stack_order` (default `sec` > `db` > `money` > `api` > `ui` > `docs`).
  - `sec` covers auth, permissions, privacy and RLS; `db` covers migrations; `money` covers ledger, billing and payments.

Examples: `M:feature:db`, `S:bug:ui`, `L:feature:sec`.

## Routing resolution (`route.py`)

1. If `overrides[type]` exists, use it.
2. Otherwise use the more expensive of `size_default[size]` and `stack_floor[stack]` (if that stack has a floor).
3. Exploration: for S and M tickets with `issue % 10 == 0`, drop one tier (haiku is the floor) and log `explore: true`.

Tiers, cheapest first: haiku < sonnet < opus.

## Ledger rows (`ledger.jsonl`)

```json
{"kind":"session","date":"YYYY-MM-DD","agent":"<task id>","role":"lane|merger|planner|helper","model":"claude-sonnet-5-5","tickets":["E40-S14"],"in":0,"out":0,"cache_read":0,"cache_write":0,"cost_usd":0.0,"minutes":0,"tests":{"passed":0,"failed":0}}
{"kind":"ticket","date":"YYYY-MM-DD","ticket":"E40-S14","issue":1062,"pr":1288,"type":"M:feature:db","model":"claude-sonnet-5-5","explore":false,"review_rounds":1,"first_pass":false,"escalations":0,"reverted_7d":null,"est_size":"M","actual_minutes":0,"cost_usd":0.0,"notes":""}
```

- **review_rounds:** how many times the coordinator or CI sent the PR back for a real defect. Known pins and infra flakes don't count.
- **first_pass:** `review_rounds == 0`.
- **escalations:** the worker couldn't proceed and the coordinator had to decide or rescue (design question, stuck, wrong approach redone). Answering an owner-decision question isn't an escalation.
- **reverted_7d:** leave it `null` until the sprint-end check. Set it `true` if the PR was reverted, or a later PR fixed a defect it introduced, within 7 days.
- **cost_usd:** if a lane did several tickets in one session, split the cost by minutes or leave it `null` with a note.

## Sprint-end rules (sprint = Mon to Sun, window = last 28 days)

For each ticket type:
- **Move down a tier** if it has at least 5 tickets on the cheaper tier, those tickets' first_pass rate is at least 80%, none were reverted, and (when costs are known) they cost less per ticket.
- **Move up a tier** if it has at least 5 tickets on the current tier and either 2 of the last 5 escalated or the first_pass rate is below 50%.
- Record every change, with its numbers, in `sprints/<week>.md`, and write it to `routing.json` `overrides`.

## Feedback loops

- A review comment made twice or more goes into `review-patterns.md`, and every brief links that file.
- An estimate miss (actual minutes over 2× or under 0.5× the size's median) goes to the planner as a note in the sprint report. Re-sizing waits for the next planning pass.

## Cost note

`usage.py` prices at Anthropic first-party API list rates and dedupes repeated usage by message id. A subscription bills differently, so use it to compare models, not to reconcile an invoice. Update `PRICES` when list prices change.
