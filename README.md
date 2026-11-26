# lane-coordinator

A Claude Code plugin for running a repository as **one coordinator plus parallel worker lanes**, with one ticket per lane.

- **DAG scheduler.** `work_queue.py` builds the dependency graph of your backlog (GitHub issues with `Depends on: #N` lines, or a markdown backlog), marks what's done from merged PRs, and gives each lane the ready item that unblocks the most work. Idle lanes borrow work, and the report flags blockers, cycles and merged-but-open issues.
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

Then, in any repo, ask Claude to "set up lanes here". The setup creates per-project state in `~/.claude/projects/<repo-slug>/metrics/` (outside your repo):

| File | Purpose |
|---|---|
| `config.json` | integration branch, work-queue source and labels, risk stacks |
| `routing.json` | live model table (roles, stack floors, size defaults, overrides) |
| `ledger.jsonl` | session and ticket rows |
| `sprints/YYYY-Www.md` | weekly routing reports |

## Scripts

All scripts are in `skills/lane-coordinator/scripts/` and use only the Python 3 standard library.

| Script | Use |
|---|---|
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
