---
name: lane-coordinator
description: Run a repo as an Opus coordinator with parallel worker sessions ("lanes"), one ticket each. It tracks milestone → epic → story → task as GitHub issues with a status workflow (todo, in-progress, testing, blocked, merging, merged, staging, deployed, done) and per-session claim tags (<project>-NN). It schedules lanes from a dependency DAG of the backlog, picks each lane's model from a routing table, has the coordinator review, merge and close every ticket, ships periodic feature releases (integration → main, a release-candidate tag for staging, the same commit tagged for production), logs a cost/quality ledger and tunes routing each sprint. Use when coordinating multiple worker sessions on a backlog; creating epics, stories or tasks; claiming, releasing or moving a ticket's status; cleaning up a worktree after merge; planning, promoting or tagging a release for staging or production; deciding what each lane works on next or which model a ticket gets; finding blockers, stale claims or dependency cycles; logging agent metrics; or running the end-of-sprint routing review. Also use to set this up in a new project ("set up lanes", "add the coordinator pattern here").
---

# Lane coordinator

One **coordinator** session (session 00) plans, briefs, reviews, merges and closes. Worker **sessions** (01, 02, …) each claim one ticket at a time and carry it to a reviewable PR. Per-project state lives outside the repo, in the project's Claude folder:

```
STATE=~/.claude/projects/<slug>/metrics     # slug = absolute repo path with every non-alphanumeric char → "-"
  config.json        integration branch, queue (work source), tracker (labels, milestones), release, risk stacks
  routing.json       live model table
  ledger.jsonl       one row per worker session and per merged ticket
  sprints/<YYYY-Www>.md
~/.claude/projects/<slug>/memory/review-patterns.md   recurring PR defects (linked from every brief)
```

Find STATE with `python3 -I ${CLAUDE_SKILL_DIR}/scripts/state_dir.py`. If it prints `missing`, run **Setup** first. Below, `TR`, `WQ` and `REL` mean `python3 -I ${CLAUDE_SKILL_DIR}/scripts/` + `tracker.py`, `work_queue.py` and `release.py`.

## Setup (once per project)

1. `bash ${CLAUDE_SKILL_DIR}/scripts/init.sh [repo_path]` creates STATE from the skill's `templates/` and never overwrites existing files.
2. Fill in `config.json`:
   - **`integration_branch`**
   - **`queue`:** where stories and their dependencies live. See `references/queue.md`.
   - **`tracker`:** project name, and whether milestones are GitHub milestones or labels. See `references/tracker.md`.
   - **`release`:** main branch, version bump, and optional deploy hooks for staging and production. See `references/release.md`.
   - the risky stacks this codebase has.
3. Run `TR setup` to create the `status:*`, `type:*` and `<project>-NN` labels. Then run `WQ` once and fix any parse problems it reports.
4. Add a pointer to `MEMORY.md`: `- [Review patterns](review-patterns.md) — recurring PR defects; link from every worker brief`.

## Tickets and workflow

**Hierarchy:**
- **Milestone:** a GitHub milestone, or a `milestone:<name>` label.
- **Epic:** an issue labelled `type:epic` and `epic:<key>`.
- **Story:** an issue labelled `type:story`, linked as a sub-issue of its epic.
- **Task:** an issue labelled `type:task`, linked as a sub-issue of its story.

Create them with `TR epic`, `TR story` and `TR task`. A child copies its parent's `epic:`, `feature:`, milestone and `lane:` tags. A story that has tasks is a container: sessions claim its tasks, and the story counts as done when they all are.

**Workflow** (one `status:*` label per issue; `TR status N <state>` checks each move, `--force` overrides):

```
todo → in-progress → testing → merging ──(coordinator)──→ merged → staging → deployed → done (issue closed)
            ↑   ↘ blocked (needs --note) ↙   ↑               (rc tag)   (prod tag)
            └──────── sent back ─────────────┘          cancelled (coordinator; closes as not planned)
```

- **Sessions** move their own ticket from todo up to merging.
- **Only the coordinator** sets merged, staging, deployed, done and cancelled. `REL` makes the staging and deployed moves.
- **`lane:NN`** is the queue a ticket is planned for. **`<project>-NN`** (for example `myhoa2-03`) is the session that has claimed it right now.

## Worker session protocol (put this in every brief)

1. **Claim:** `TR next` claims the best ready ticket for your lane from the DAG, borrowing if your lane is empty. Or the coordinator names one: `TR claim N`. Your session number comes from the worktree folder (`<project>-NN`) or `$LANE_SESSION`. You hold one active ticket at a time.
2. **Branch:** branch from `origin/<integration_branch>`. Before opening a PR, check every item in `<memory>/review-patterns.md`.
3. **Test:** `TR status N testing` while running the full test suites. If they fail, go back with `TR status N in-progress --note "…"`.
4. **Blocked:** `TR status N blocked --note "waits on #M"`. Then tell the coordinator.
5. **PR ready:** `TR status N merging --pr P`, then report `#P ready (head <sha>)`. **Never merge.**
6. **After the coordinator sets merged:** `TR cleanup` deletes the merged branch and resets this worktree to `origin/<integration_branch>`. Add `--remove` for a per-ticket worktree. It refuses on uncommitted work or an unmerged branch. Then `TR next`.
7. **Helpers:** use helper sub-agents on the `helper` model for search, test runs and log reading.

## The coordinator loop

**1. Plan.** Run `WQ --lanes <active lanes> --models`, adding `--milestone M` to focus on one milestone. It builds the dependency DAG straight from the tracker (stories, tasks as children of their stories, status labels, claims, epics, milestones), plus ADRs and PRs into the integration branch, and ranks each lane's ready queue. The ranking is by milestone (a blocker inherits the earliest milestone it unblocks), then how much not-done work an item unblocks, then priority, then critical-path length, then smaller first. The **Milestones** table shows progress and the longest chain left in each. Act on the rest of the report too:
- **Paused lane owns a blocker:** open that lane, or reassign the story.
- **Merged but issue still open:** set its status per step 5.
- **Unparsed dependencies:** these are treated as satisfied, so check each one by hand.
- **Cycles** and **parse problems:** fix them in the backlog.

**2. Check the board.** `TR board [--milestone M]` shows:
- status counts per milestone and epic;
- every live claim;
- **stale claims** (no update within `tracker.stale_hours`): chase that session, or `TR release N`;
- **needs fixing:** two status labels, two claimants, `in-progress` with no session, closed-but-active.

**3. Route.** Work out the ticket type `size:kind:stack` (see `references/metrics.md`), then run `python3 -I ${CLAUDE_SKILL_DIR}/scripts/route.py <type> [--issue N]`. Pass the model it prints as `model` when you start the session's agent. It applies overrides, stack floors, size defaults and the 10% exploration sample.

**4. Review and merge yourself.** A CI-watcher agent (`ci_watcher` model) watches CI and reports `#N ready (head <sha>)`. It never merges. You review that head:
- **Defect:** send it back with `TR status N in-progress --note "…"` (or `testing`), and count a review round.
- **Clean:** merge it (`gh pr merge`), then `TR --session 00 status N merged --pr P`. That releases the session's claim. The session then runs `TR cleanup`.

Sub-agent merges get flagged as "Merge Without Review". Known pins and infra flakes don't count as review rounds.

**5. Release** (periodically, at the owner's cadence; see `references/release.md`):
1. **Plan:** `REL plan` shows the next version and the merged tickets.
2. **Promote:** `REL promote` opens the "Release vX.Y.Z" PR from integration to main.
3. **Merge:** review it and merge with a **merge commit** (`gh pr merge N --merge`, never squash).
4. **Staging:** `REL staging --pr N` tags `vX.Y.Z-rc.K`, publishes a prerelease and moves its tickets to staging.
5. **Verify staging.** On failure, fix forward and promote again, which gives the next rc.
6. **Production:** `REL prod` tags that same commit `vX.Y.Z` and moves the tickets to deployed.
7. **Check:** `REL status` shows what's in each environment.

**6. Close.** You close issues; sessions never do. After reviewing a shipped ticket, run `TR status N done --note "…"`, or `REL prod --done` once the owner has signed off on the whole release.
- **No release flow:** go straight from merged to done.
- **Parts:** a story whose PR title says "part A" or similar isn't done until its last part merges.

**7. Log.** When a session finishes a ticket, append a `session` row. When its PR merges, append a `ticket` row (schemas in `references/metrics.md`). For a session's cost, run `python3 -I ${CLAUDE_SKILL_DIR}/scripts/usage.py <transcript .jsonl> [--since --until]`.

**8. Learn.** If a review comment comes up a second time, add it to `review-patterns.md` with the PR numbers.

## Sprint end (Sunday)

Run `python3 -I ${CLAUDE_SKILL_DIR}/scripts/sprint.py` and check what it suggests. It also flags estimate misses and tickets whose `reverted_7d` hasn't been set yet. Fill those in (was the PR reverted, or did a later PR fix a defect it introduced?), then run it again. If you accept the suggestions, add `--apply` to write the overrides to `routing.json`. Write `sprints/<YYYY-Www>.md` with the numbers behind every change and the estimate notes for the planner.

## Defaults

Opus coordinates, plans, designs, reviews, merges and closes. Sonnet watches CI and takes L tickets plus anything touching sec, db or money. Haiku takes S and M tickets and helpers. The point is cost: long-running Opus lanes are expensive, mostly in cache reads and writes, so every downgrade needs evidence from the ledger. Change a project's table only through the sprint rules or an explicit owner decision.
