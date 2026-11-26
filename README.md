# lane-coordinator

A Claude Code plugin for running a repository as **one coordinator plus parallel worker lanes**, with one ticket per lane.

- **Ticket tracking.** Milestone → epic → story → task as GitHub issues (stories and tasks are linked as sub-issues). Each ticket has one `status:*` label: todo, in-progress, testing, blocked, merging, merged, staging, deployed, done or cancelled. Sessions claim tickets with a `<project>-NN` tag; claims are race-safe and a session holds one ticket at a time. Only the coordinator merges and closes, and a session cleans up its worktree after its ticket merges.
- **ADRs.** The coordinator owns the architecture decisions in `docs/adr`. Tickets wait on `ADR-NNNN` until it is accepted, and `adr.py pending` ranks proposed ADRs by the work they block. Briefs list each ticket's ADRs (`for-ticket`), and `adr.py check --pr` fails a PR that relies on an undecided ADR or changes an ADR's status. Decisions change by superseding, never by rewriting.
- **Releases.** Periodically, the coordinator promotes `integration` into `main` with a "Release vX.Y.Z" PR whose notes come from the merged tickets. It then tags the merge commit `vX.Y.Z-rc.N` for staging, and once staging passes, tags the same commit `vX.Y.Z` for production. Tickets move merged → staging → deployed. Deploys run from optional hooks or from a CI workflow triggered by the release.
- **DAG scheduler.** `work_queue.py` builds the dependency graph straight from the ticket tracker (stories, tasks, status labels, claims, epics, milestones), or from a markdown backlog, marks what's done from merged PRs, and gives each lane the ready item for the earliest milestone that unblocks the most work. A blocker is promoted to the milestone it blocks. Idle lanes borrow work, and the report flags blockers, cycles and merged-but-open issues.
- **Model routing.** Each ticket gets a model from a per-project table, keyed on `size:kind:stack` (for example `M:feature:db`). Risky stacks (auth, migrations, money) have a model floor.
- **Gated merges.** A CI-watcher agent reports `#N ready (head <sha>)`. The coordinator reviews that head and merges it itself, so sub-agents never merge.
- **Ledger.** Every worker session and merged ticket is logged to `ledger.jsonl`: cost, review rounds, escalations and reverts.
- **Sprint tuning.** `sprint.py` suggests moving a ticket type down a tier when the cheaper model has a proven record (≥5 tickets, ≥80% first pass, no reverts, cheaper), and up a tier when it struggles. 10% of S and M tickets run one tier cheaper to gather that evidence.
- **Review patterns.** Defects found twice go into a shared `review-patterns.md` that every worker brief links.

## Install

```
/plugin marketplace add engineeringwiththeo/lane-coordinator
/plugin install lane-coordinator@engineeringwiththeo
```

Then, in any repo, ask Claude to "set up lanes here". It runs `tracker.py setup` to create the labels. The setup creates per-project state in `~/.claude/projects/<repo-slug>/metrics/` (outside your repo):

| File | Purpose |
|---|---|
| `config.json` | integration branch, work-queue source, tracker settings, risk stacks |
| `routing.json` | live model table (roles, stack floors, size defaults, overrides) |
| `ledger.jsonl` | session and ticket rows |
| `sprints/YYYY-Www.md` | weekly routing reports |

## Scripts

All scripts are in `skills/lane-coordinator/scripts/` and use only the Python 3 standard library.

| Script | Use |
|---|---|
| `tracker.py <setup\|epic\|story\|task\|claim\|next\|status\|release\|mine\|board\|deployed\|cleanup>` | Create and move tickets, claim work, see the board. See [references/tracker.md](skills/lane-coordinator/references/tracker.md). |
| `adr.py <init\|new\|status\|list\|pending\|for-ticket\|check --pr N>` | Write and decide ADRs, rank the decisions blocking work, brief sessions and gate review. See [references/adr.md](skills/lane-coordinator/references/adr.md). |
| `release.py <plan\|promote\|staging --pr N\|prod\|status>` | Feature releases: promotion PR, rc tag for staging, final tag for production. See [references/release.md](skills/lane-coordinator/references/release.md). |
| `work_queue.py [--lanes 01,02] [--models] [--json out]` | Next item per lane, ranked ready queues, blockers and cycles. See [references/queue.md](skills/lane-coordinator/references/queue.md). |
| `init.sh [repo]` | Set up state for a repo. Never overwrites. |
| `route.py <size:kind:stack> [--issue N]` | Print the model for a ticket. `--role helper` prints a role's model. |
| `sprint.py [--apply]` | Sprint-end tier suggestions, estimate misses and unchecked reverts. |
| `usage.py <transcript.jsonl>` | Token and API-equivalent cost of an agent transcript. |

Cost figures use Anthropic API list prices and are for comparing models, not for reconciling a bill. Update `PRICES` in `usage.py` when prices change.

## Tests

```
python3 -I -m unittest discover -s tests
```

## Requirements

Claude Code, `python3`, `git`, and `gh` (for closing issues and merging).

## License

MIT
