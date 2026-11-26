# lane-coordinator user guide

This guide follows a fictional project, **acme**, through a coordinator's week. Every output below is real: `examples/demo.py` builds acme on an in-memory GitHub, runs the plugin's commands, and pastes their output here. CI fails if this page drifts from what the code prints.

In Claude Code you rarely type these commands yourself. You ask the coordinator ("roadmap status", "plan the lanes", "review #120"), and it runs them. They're shown so you know what's happening and can run them yourself. Each is `python3 -I ${CLAUDE_SKILL_DIR}/scripts/<script>`; the guide drops that prefix.

**Roles:**
- **Coordinator (session 00):** opened at the repo's main checkout. It plans, decides ADRs with you, reviews, merges, releases and closes tickets.
- **Worker sessions (01, 02, …):** each opened in a git worktree named `acme-NN`. Each claims one ticket at a time and carries it to a reviewable PR.

**acme's starting point:**
- three milestones;
- four epics, one lane each;
- three ADRs, one of which (the billing provider) the owner hasn't decided yet;
- last sprint shipped sign-up in `v0.1.0`;
- the landing page is on staging in `v0.2.0-rc.1`.

---

## 1. The roadmap

`docs/roadmap.md` is the plan. It lists the milestones in delivery order, each with a due date, goal and exit criteria, and the epics under each milestone, with a lane and any ADR they depend on.

```markdown
{{roadmap_file}}
```

`roadmap.py sync` shows what GitHub needs in order to match the file. `roadmap.py sync --apply` does it.

```text
$ roadmap.py sync
{{sync_plan}}
```

Run it again after editing the file. It only moves, updates or creates: it never deletes, and it reports anything on GitHub that isn't on the roadmap as **drift**.

## 2. Breaking epics into stories and tasks

```text
{{create_cmds}}
```

Each new ticket copies its parent's epic, feature, milestone and lane tags. A task becomes a GitHub sub-issue of its story, and a story becomes a sub-issue of its epic. Every ticket starts as `status:todo`:

```text
$ tracker.py task "Avatar crop and resize" --story 9 --size S
{{create_result}}
```

A story that has tasks becomes a **container**: sessions claim its tasks, never the story itself.

## 3. Sessions claim work

On Monday morning, each worker session asks for its next ticket. Its session number comes from its worktree folder name (`acme-01` is session 01).

```text
$ tracker.py next                       # in ~/code/acme-01
{{next_01}}

$ tracker.py next                       # in ~/code/acme-02
{{next_02}}

$ tracker.py next                       # in ~/code/acme-03
{{next_03}}
```

Lane 03 gets nothing because everything in billing waits on **ADR-0002**, which is still Proposed. It can't borrow from the other lanes either, because they're all active; that rule keeps lanes from editing each other's files. Section 5 is where the coordinator deals with it.

A session moves its own ticket through `in-progress → testing → merging`. `blocked` requires a note:

```text
$ tracker.py status 10 blocked --note "waits on storage credentials from ops"
{{status_blocked}}
```

The tracker refuses any move a session isn't allowed to make. Only the coordinator merges:

```text
$ tracker.py status 6 merged            # from session 01
{{status_refused}}
```

Every move leaves a comment on the issue, which serves as both the audit trail and the claim lock. Here is sign-up's history, from claim to production. The HTML comments are the claim markers that make claims race-safe:

```text
{{trail}}
```

## 4. Planning: the dependency graph

`work_queue.py` builds the dependency graph from the tracker's tickets, ADRs and milestones, then ranks what's ready for each lane. With `--models`, it also says which model each lane's next item should use.

```text
$ work_queue.py --lanes 01,02,03,04 --models
{{queue}}
```

Some things to read off this report:
- **The top blocker** is `#12 Plans table`, and it's blocked by an ADR.
- **Lane 02's next item:** with the storage task blocked, lane 02's next item is the new crop task.
- **Milestones:** the table shows how much work is left in each milestone and how long its longest chain is.

## 5. Decisions: ADRs

```text
$ adr.py list
{{adr_list}}

$ adr.py pending
{{adr_pending}}
```

`pending` ranks the undecided ADRs by how much work waits on them, directly and downstream. This is the coordinator's list of decisions to get from the owner. Once the owner decides, the coordinator runs `adr.py status 0002 accepted --note "…"`, and the billing tickets become ready on the next plan.

When briefing a session, the coordinator includes the ADRs the ticket, its story and its epic name:

```text
$ adr.py for-ticket 13
{{adr_for_ticket}}
```

A warning here means the ticket isn't ready to claim, which is why the planner keeps it blocked.

## 6. The board

```text
$ tracker.py board
{{board}}
```

The storage task has been blocked since Friday with no update. It shows as a **stale claim** for the coordinator to chase or release (`tracker.py release 10`).

## 7. Review

Before reviewing the diff, the coordinator runs the ADR gate:

```text
$ adr.py check --pr 120
{{adr_check_ok}}

$ adr.py check --pr 121
{{adr_check_bad}}
```

PR 121 tried to accept the billing provider itself. Only the coordinator decides ADRs, with the owner, so the check fails (exit code 1). The coordinator sends the PR back with `tracker.py status N in-progress --note "…"`.

When a PR is clean, the coordinator merges it and runs `tracker.py --session 00 status N merged --pr P`. The session then runs `tracker.py cleanup` and claims its next ticket.

## 8. Releases

Releases go from `integration` to `main`. Then a release-candidate tag goes to staging, and that same commit is tagged for production.

```text
$ release.py plan
{{release_plan}}
```

`release.py promote` opens the release PR. Its body links tickets with `Refs`, so GitHub doesn't close them itself:

```markdown
{{promote_body}}
```

The coordinator merges the PR with a merge commit (never squash), then tags it for staging:

```text
$ release.py staging --pr 19
{{staging}}
```

Once staging has been checked, the same commit is tagged for production:

```text
$ release.py prod
{{prod}}
```

The landing page has since been promoted as `v0.2.0` and is on staging:

```text
$ release.py status
{{release_status}}
```

## 9. Roadmap status: everything in one view

```text
$ roadmap.py status
{{roadmap_status}}
```

How to read it:
- **M1 is 🔴 at risk.** At the current pace of about 2 tickets a week across 4 lanes, it lands two days after its due date. Part of the reason is that the M1 profile badge waits on M2's Plans table, which in turn waits on the billing decision.
- **M2 and M3 are ⛔ waiting on ADR-0002.** Nothing left in them can start until the owner decides. M3's pricing page needs the Plans table too.
- **The coordinator's move:** chase ADR-0002 first, since it unblocks all three milestones, or move the badge to M2 in `docs/roadmap.md` and re-sync.

## 10. Models and sprint end

`route.py` picks the model for a ticket type (`size:kind:stack`). Here, money and security work never goes below Sonnet:

```text
{{route}}
```

At sprint end, `sprint.py` reads the ledger and suggests routing changes:

```text
$ sprint.py --today 2027-01-10
{{sprint}}
```

The five large UI tickets run on Haiku as the 10% exploration sample all passed review the first time, at about a fifth of the cost. So the suggestion is to move `L:feature:ui` down to Haiku. `--apply` writes that into `routing.json`.

---

## Command reference

| Script | Commands |
|---|---|
| `roadmap.py` | `init`, `check`, `sync [--apply]`, `status [--json]` |
| `tracker.py` | `setup`, `milestone`, `epic`, `story`, `task`, `claim`, `next [--milestone]`, `status`, `release`, `mine`, `board`, `deployed`, `cleanup` |
| `work_queue.py` | `[--lanes] [--milestone] [--models] [--json]` |
| `adr.py` | `init`, `new`, `status`, `list`, `pending`, `for-ticket`, `check --pr [--coordinator]` |
| `release.py` | `plan`, `promote`, `staging --pr`, `prod [--done]`, `status` |
| `route.py` / `sprint.py` / `usage.py` | model routing, sprint-end tuning, transcript cost |

Details are in `skills/lane-coordinator/references/`: `roadmap.md`, `tracker.md`, `queue.md`, `adr.md`, `release.md` and `metrics.md`.
