# The work queue (dependency DAG)

`scripts/work_queue.py` answers one question: what should each lane pick up next? It reads stories and their dependencies, works out what is done from GitHub, and ranks what is ready. It never writes to GitHub.

## Where stories come from (`config.queue.source`)

### `"issues"` (default, no backlog file)

Every GitHub issue is a story, except those labelled `epic_issue_label`. If any issue carries `story_label`, only labelled issues count. Each story reads:

| Field | From |
|---|---|
| id | `#<number>` |
| size | a `size:S`, `size:M` or `size:L` label |
| priority | a `priority:p0`…`p3` label |
| epic | an `epic:NN` label (used for lane inheritance and `EPIC NN` dependencies) |
| lane | a label matching `lane_label` (default `lane:NN`) |
| dependencies | lines in the body that start `Depends on:` or `Blocked by:` (bold is fine), such as `Depends on: #12, #15, ADR-0003` |

### `"backlog"` (a markdown planning file)

Set `queue.backlog` to the file's path. Each story is a level-3 heading in this exact form:

```
### E16-S58 ([#598](../../../issues/598)) — The vendor portal · size:L · priority:p1 · depends on: E16-S50, EPIC 26, ADR-0051, #211
```

- **id:** `E<epic>-S<n>`, with a link to its GitHub issue. The issue's title should start with that id.
- **parts:** separated by ` · `. `depends on:` comes last and takes the rest of the line.
- **dependency list:** split on `,` and `;` outside brackets, so `E26 (BudgetBar, Tile)` is one item. A bare number after an item continues it, so `Conflicts 2, 3` is one item.

## Dependency kinds

| Written as | Satisfied when |
|---|---|
| `E09-S50` (or `#N` for an issue that is a story) | that story is done. This is a graph edge. |
| `EPIC 26` / `E26` | every story in the epic is done. These are graph edges. If the backlog has none of its stories, the epic's GitHub stories (`epic:` label plus `story_label`) are checked instead. |
| `ADR-0051` | the ADR's `- **Status:**` line starts with "Accepted". Set `queue.adr_dir`; ADR files are named `NNNN-*.md`. |
| `#211` (not a story) | that issue is done. |
| `milestone:<name>` | every ticket in that milestone is done (milestones on) |
| anything else | it is treated as satisfied and listed under **Unparsed dependencies**, so check those by hand. |

## Done, in progress, ready

- **Done:** the issue is closed, or a merged PR into the integration branch references it (`Refs`, `Closes`, `Fixes` or `Resolves #N`), its title isn't partial, and no open PR references it.
- **Partial titles** (`part A`, `part 1`, `(B)`, `partial`, `slice 2`, `first half`) don't count as done. Title a story's last PR without a part marker.
- **In progress:** an open PR references it.
- **Ready:** not done, not in progress, and every dependency is satisfied.

## Ranking and lanes

Ready items are sorted by:
1. **unblock weight**: not-done stories transitively waiting on this one;
2. **priority** (p0 first);
3. **depth**: the longest not-done chain below it;
4. **size** (S first);
5. **id**.

- **Lane assignment:** the `lane:NN` label, else the most common labelled lane in its epic, else the epic issue's lane.
- **Next item per lane:** each `--lanes` lane takes the top of its own queue. A lane with nothing ready borrows the best item from unassigned or inactive lanes, and no item goes to two lanes.
- **Paused lane owns a blocker:** the report warns when an inactive lane owns a not-done item that blocks others.

## Reading the issue tracker

In issues mode, the planner reads everything `tracker.py` writes, so the plan always matches GitHub:

| Tracker writes | Planner uses it as |
|---|---|
| `type:story` / `type:task` issues; `type:epic` and `type:milestone` are skipped | the work items |
| `Parent: #N` (task → story) | a story with tasks is a **container**: never queued itself, done when its tasks are, in progress when any is; its tasks also wait on its dependencies |
| `Depends on:` / `Blocked by:` lines | graph edges (`#N`, `epic:<key>`, `milestone:<name>`, `ADR-NNNN`) |
| `status:merged`, `staging`, `deployed`, `done`, `cancelled` | done |
| `status:in-progress`, `testing`, `merging`, or a `<project>-NN` claim | in progress (shown with 🔒 and the session) |
| `status:blocked` | not ready (listed under `status:blocked`) |
| `epic:<key>` on stories, and `lane:NN` on the epic issue | lane inheritance, and `epic:<key>` dependencies |
| GitHub milestone (or a `milestone:` label) | ranking, the per-milestone report, `milestone:` dependencies, `--milestone` focus |

## Milestones

These are on by default in issues mode and can be switched with `queue.milestones`.
- **Ranking:** milestones are ordered by due date, then title. Ready work is ranked by milestone first, then by the rules above.
- **Blockers move up:** a ticket inherits the earliest milestone of anything it transitively blocks. An M2 ticket that an M1 story waits on is therefore planned as M1 work.
- **Report:** the report gains a **Milestones** table with done/total, in progress, ready, blocked, and the longest chain left.
- **Focus:** `--milestone M1` (in `work_queue.py` and `tracker.py next`) queues only that milestone's tickets and their transitive blockers. "M1" matches "M1 — Foundation".
- **Dependencies:** `Depends on: milestone:M1` waits for every ticket in that milestone.

## Flags

| Flag | Effect |
|---|---|
| `--lanes 01,04` | active lanes (default: every lane seen) |
| `--milestone M` | focus on one milestone and its blockers |
| `--models` | appends the routed model for each next item (`kind:` and `stack:` labels sharpen it) |
| `--json OUT` | writes the full graph: nodes with `dependsOn`, `blocks`, `unmet`, `weight`, `depth` |
| `--save-data F` / `--data F` | snapshot the GitHub data, or run from a snapshot (offline, tests) |
