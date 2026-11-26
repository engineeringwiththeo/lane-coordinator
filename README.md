# lane-coordinator

A Claude Code plugin for running a repository as **one coordinator plus parallel worker lanes**, with one ticket per lane.

- **Roadmap.** `docs/roadmap.md` lists the milestones in delivery order, with due dates, goals and exit criteria, and the epics under them. `roadmap.py sync` turns it into GitHub milestones and epic issues. Roadmap order ranks the planner, and `roadmap.py status` reports per milestone: progress, epics, the ADRs holding it up, what's on staging and in production, and a forecast date that's flagged when it lands after the due date.
- **Ticket tracking.** Milestone → epic → story → task as GitHub issues (stories and tasks are linked as sub-issues). Each ticket has one `status:*` label: todo, in-progress, testing, blocked, merging, merged, staging, deployed, done or cancelled. Sessions claim tickets with a `<project>-NN` tag; claims are race-safe and a session holds one ticket at a time. Only the coordinator merges and closes, and a session cleans up its worktree after its ticket merges.
- **ADRs.** The coordinator owns the architecture decisions in `docs/adr`. Tickets wait on `ADR-NNNN` until it is accepted, and `adr.py pending` ranks proposed ADRs by the work they block. Briefs list each ticket's ADRs (`for-ticket`), and `adr.py check --pr` fails a PR that relies on an undecided ADR or changes an ADR's status. Decisions change by superseding, never by rewriting.
- **Releases.** Periodically, the coordinator promotes `integration` into `main` with a "Release vX.Y.Z" PR whose notes come from the merged tickets. It then tags the merge commit `vX.Y.Z-rc.N` for staging, and once staging passes, tags the same commit `vX.Y.Z` for production. Tickets move merged → staging → deployed. Deploys run from optional hooks or from a CI workflow triggered by the release.
- **DAG scheduler.** `work_queue.py` builds the dependency graph straight from the ticket tracker (stories, tasks, status labels, claims, epics, milestones), or from a markdown backlog, marks what's done from merged PRs, and gives each lane the ready item for the earliest milestone that unblocks the most work. A blocker is promoted to the milestone it blocks. Idle lanes borrow work, and the report flags blockers, cycles and merged-but-open issues.
- **Model routing.** Each ticket gets a model from a per-project table, keyed on `size:kind:stack` (for example `M:feature:db`). Risky stacks (auth, migrations, money) have a model floor.
- **Gated merges.** A CI-watcher agent reports `#N ready (head <sha>)`. The coordinator reviews that head and merges it itself, so sub-agents never merge.
- **Ledger.** Every worker session and merged ticket is logged to `ledger.jsonl`: cost, review rounds, escalations and reverts.
- **Sprint tuning.** `sprint.py` suggests moving a ticket type down a tier when the cheaper model has a proven record (≥5 tickets, ≥80% first pass, no reverts, cheaper), and up a tier when it struggles. 10% of S and M tickets run one tier cheaper to gather that evidence.
- **Review patterns.** Defects found twice go into a shared `review-patterns.md` that every worker brief links.

**New here? Read the [user guide](docs/USER_GUIDE.md).** It follows a demo project through a coordinator's week with the real output of every command.

## Install

Plugins installed at user scope are shared by the terminal CLI, the Claude Desktop app (Code tab) and the VS Code extension, so install it once from any of them:

| Where | How |
|---|---|
| Terminal | `claude plugin marketplace add engineeringwiththeo/lane-coordinator`, then `claude plugin install lane-coordinator@engineeringwiththeo`, or `/plugin` inside `claude` |
| VS Code extension | type `/plugins` in the prompt box. In **Marketplaces**, add `engineeringwiththeo/lane-coordinator`; in **Plugins**, install `lane-coordinator` |
| Claude Desktop (Code tab) | **+** next to the prompt → **Add plugins** → add the marketplace and install |

Update with `claude plugin update lane-coordinator@engineeringwiththeo`, or from the same plugin screens.

The terminal `claude` command doesn't come with Claude Desktop or the VS Code extension. To get it, use one of:

- `curl -fsSL https://claude.ai/install.sh | bash`: the native installer, which updates itself;
- `brew install --cask claude-code`: Homebrew, stable channel; use `claude-code@latest` for every release. Update with `brew upgrade`;
- `npm install -g @anthropic-ai/claude-code@latest`: needs Node 22+.

## Using it in a project

The skill loads by itself when you talk about lanes, tickets, the roadmap, ADRs or releases. You can also call it directly as **`/lane-coordinator:lane-coordinator`**.

**1. Set up (once), in a Claude Code session opened at the repo's main checkout.** This session is the coordinator, session 00:

```
/lane-coordinator:lane-coordinator set up lanes here
```

Claude then:
- runs `init.sh` to create the per-project state;
- fills in `config.json` with you: the integration branch, the tracker's project name and milestone mode, and the release settings;
- runs `tracker.py setup` to create the labels and `adr.py init` to create `docs/adr`;
- writes `docs/roadmap.md` with you, then runs `roadmap.py sync --apply`;
- breaks the first milestone's epics into stories.

Release automation needs separate `integration` and `main` branches.

**2. Open worker sessions.** Each lane is a git worktree named `<project>-NN`, which is how a session knows its number. Alternatively, set `LANE_SESSION=NN`.

```
git worktree add -b lane/03 ../<project>-03 origin/integration
```

Open Claude Code in that folder and ask it to "pick up the next ticket". It follows the worker protocol: `tracker.py next` → testing → merging → report ready → `cleanup` after the coordinator merges.

**3. Run the coordinator loop** from session 00. Ask for things like:
- "roadmap status"
- "plan the lanes"
- "what decisions are blocking work?"
- "review #123"
- "cut a release"
- "sprint end"

The setup creates per-project state in `~/.claude/projects/<repo-slug>/metrics/`, outside your repo:

| File | Purpose |
|---|---|
| `config.json` | integration branch, work-queue source, tracker, ADR, roadmap and release settings, risk stacks |
| `routing.json` | live model table (roles, stack floors, size defaults, overrides) |
| `ledger.jsonl` | session and ticket rows |
| `sprints/YYYY-Www.md` | weekly routing reports |

In the repo itself, it adds `docs/roadmap.md` and `docs/adr/`.

## Scripts

All scripts are in `skills/lane-coordinator/scripts/` and use only the Python 3 standard library.

| Script | Use |
|---|---|
| `roadmap.py <init\|check\|sync [--apply]\|status>` | The plan: sync milestones and epics to GitHub, and the per-milestone status and forecast. See [references/roadmap.md](skills/lane-coordinator/references/roadmap.md). |
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
python3 -I examples/demo.py --check    # the user guide matches the real outputs
```

After changing an output, run `python3 -I examples/demo.py` to regenerate `docs/USER_GUIDE.md`. Edit `docs/guide/USER_GUIDE.template.md`, never the generated file.

## Requirements

Claude Code, `python3`, `git`, and `gh` (for closing issues and merging).

## License

MIT
