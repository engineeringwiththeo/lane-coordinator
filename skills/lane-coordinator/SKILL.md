---
name: lane-coordinator
description: Run a repo as an Opus coordinator with parallel worker "lanes", one ticket per lane. It schedules lanes from a dependency DAG of the backlog, picks each lane's model from a routing table, has the coordinator review and merge every PR, logs a cost/quality ledger and tunes routing each sprint. Use when coordinating multiple worker agents on a backlog, spawning lane, CI-watcher or helper sub-agents, deciding what each lane works on next or which model a ticket gets, finding blockers or dependency cycles, closing story issues after merge, logging agent metrics, or running the end-of-sprint routing review. Also use to set this up in a new project ("set up lanes", "add the coordinator pattern here").
---

# Lane coordinator

You are the **coordinator**. You plan, brief, review and approve. Workers do the tickets. Per-project state lives outside the repo, in the project's Claude folder:

```
STATE=~/.claude/projects/<slug>/metrics     # slug = absolute repo path with every non-alphanumeric char → "-"
  config.json        project settings (integration branch, work-queue source, labels, risk stacks)
  routing.json       live model table
  ledger.jsonl       one row per worker session and per merged ticket
  sprints/<YYYY-Www>.md
~/.claude/projects/<slug>/memory/review-patterns.md   recurring PR defects (linked from every brief)
```

Find STATE with `python3 -I ${CLAUDE_SKILL_DIR}/scripts/state_dir.py`. If it prints `missing`, run **Setup** first.

## Setup (once per project)

1. `bash ${CLAUDE_SKILL_DIR}/scripts/init.sh [repo_path]` creates STATE from the skill's `templates/` and never overwrites existing files.
2. Fill in `config.json` from the repo: integration branch, the `queue` section (where stories and their dependencies live: see `references/queue.md`), and the risky stacks this codebase has. Then run the work queue once and fix any parse problems it reports.
3. Add a pointer to `MEMORY.md`: `- [Review patterns](review-patterns.md) — recurring PR defects; link from every worker brief`.

## The loop

**1. Pick work.** Run `python3 -I ${CLAUDE_SKILL_DIR}/scripts/work_queue.py --lanes <active lanes> --models`, or `config.queue_cmd` if the project has its own scheduler. It builds the dependency DAG (stories, ADRs, issues, and PRs into the integration branch) and gives each lane the top of its ranked ready queue. The ranking is by how much not-done work an item unblocks, then priority, then critical-path length, then smaller first. An idle lane borrows the best unassigned or paused-lane item. Act on the rest of the report too:
- **Paused lane owns a blocker:** open that lane, or reassign the story.
- **Merged but issue still open:** close it per step 5.
- **Unparsed dependencies:** these are treated as satisfied, so check each one by hand.
- **Cycles** and **parse problems:** fix them in the backlog.

The script is read-only on GitHub. Each lane gets one story.

**2. Route.** Work out the ticket type `size:kind:stack` (see `references/metrics.md`), then run `python3 -I ${CLAUDE_SKILL_DIR}/scripts/route.py <type> [--issue N]`. Pass the model it prints as `model` on the Agent call. The script applies overrides, stack floors, size defaults and the 10% exploration sample, and prints `explore=true|false`.

**3. Brief.** Every worker brief includes:
- the ticket, acceptance criteria and branch name off `config.integration_branch`;
- "Before opening a PR, check every item in `<memory>/review-patterns.md`";
- "Use helper sub-agents on `<helper model>` for search, test runs and log reading";
- "Report `#<pr> ready (head <sha>)`. Never merge."

**4. Review and merge yourself.** A CI-watcher agent (`ci_watcher` model) watches CI, triages failures it already knows about, and reports `#N ready (head <sha>)`. It never merges. You review that head and merge it yourself (`gh pr merge`). Sub-agent merges get flagged as "Merge Without Review", even when the approval is pinned to a SHA. Count each round where you send a PR back for a real defect. Known pins and infra flakes don't count.

**5. Close stories.** The integration branch usually isn't the default branch, so `Closes #` won't fire. When a story's **last** PR merges ("part A" or "part 1" alone doesn't count), run `gh issue close <n> --comment "Done: <story> merged into <branch> in #<pr>."`. Close tech-debt issues only when the PR actually resolves them.

**6. Log.** When a worker finishes, append a `session` row; when its PR merges, append a `ticket` row (schemas in `references/metrics.md`). For a session's cost, run `python3 -I ${CLAUDE_SKILL_DIR}/scripts/usage.py <agent transcript .jsonl or task output> [--since --until]`.

**7. Learn.** If a review comment comes up a second time, add it to `review-patterns.md` with the PR numbers.

## Sprint end (Sunday)

Run `python3 -I ${CLAUDE_SKILL_DIR}/scripts/sprint.py` and check what it suggests. It also flags estimate misses and tickets whose `reverted_7d` hasn't been set yet. Fill those in (was the PR reverted, or did a later PR fix a defect it introduced?), then run it again. If you accept the suggestions, add `--apply` to write the overrides to `routing.json`. Write `sprints/<YYYY-Www>.md` with the numbers behind every change and the estimate notes for the planner.

## Defaults

Opus coordinates, plans, designs, reviews and merges. Sonnet watches CI and takes L tickets plus anything touching sec, db or money. Haiku takes S and M tickets and helpers. The point is cost: long-running Opus lanes are expensive, mostly in cache reads and writes, so every downgrade needs evidence from the ledger. Change a project's table only through the sprint rules or an explicit owner decision.
