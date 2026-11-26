# The roadmap (`scripts/roadmap.py`)

The roadmap is the top of the plan. Everything else hangs from it:

```
docs/roadmap.md ──sync──▶ GitHub milestones (due date, goal) ──▶ epic issues (type:epic, epic:<key>, lane)
      │                                                               └─▶ stories ─▶ tasks   (tracker.py)
      │ milestone order                                                     │ Depends on: #N / ADR-NNNN / milestone:
      ▼                                                                     ▼
work_queue.py ranks ready work by roadmap milestone ◀── status labels, claims ── sessions (tracker.py next)
      │                                                     ▲
      ▼                                                     │ ADR-NNNN accepted (adr.py)
roadmap.py status ◀── release:vX.Y.Z labels + tags ◀── release.py (staging rc → production)
```

## The file (`roadmap.path`, default `docs/roadmap.md`)

```markdown
## M1 — Foundation            ← a milestone, in delivery order
Due: 2027-01-31               ← GitHub milestone due date
Goal: Members can sign up.    ← the milestone description
Exit: Sign-up works in staging.

### auth — Accounts and sign-in   ← an epic: key — title
Lane: 01                          ← the lane that owns it
Depends on: ADR-0001              ← the epic's (and so its stories') dependencies
Free text becomes the epic issue's description.
```

- **Order:** milestones appear in delivery order. That order ranks work in the planner, even ahead of due dates.
- **Comments:** text inside `<!-- -->` is ignored.
- **Check:** `roadmap.py check` reports format problems.

## Commands

| Command | What it does |
|---|---|
| `init` | copies the template into the repo |
| `check` | parses the file and lists problems (no GitHub) |
| `sync` | dry run of the GitHub changes needed: create or update milestones (due date, goal and exit as the description), create epic issues, move an epic to its roadmap milestone, set its lane. It also reports drift it won't touch: open milestones and epics that aren't on the roadmap, and epic titles that differ |
| `sync --apply` | makes those changes. It also closes a GitHub milestone once every ticket in it is deployed, done or cancelled. It never deletes |
| `status` | the one-page view described below; `--json` writes it as data |

## `status`: the view that ties it together

A summary table, plus one section per milestone in roadmap order:

- **progress:** done/total, in progress, ready, blocked (from the DAG and the status labels);
- **chain:** the longest chain of not-done work left in the milestone;
- **shipped:** tickets in production, and on staging, with the release versions (from `release:` labels and the production tags);
- **epics:** each one's progress and lane;
- **decisions holding it up:** Proposed ADRs that block tickets in this milestone, directly or downstream;
- **biggest blockers:** the not-done tickets in the milestone that unblock the most;
- **forecast:** from the ledger's throughput (tickets merged per week over the last four weeks). It is the later of two estimates:
  - the remaining tickets in this milestone and every earlier one, divided by throughput;
  - the longest chain multiplied by the time one ticket takes per lane.

  The milestone is 🔴 **at risk** when the forecast is past its due date, 🟢 on track, ✅ complete, or unforecast until the ledger has data;
- **not on the roadmap:** open tickets in no roadmap milestone, and epics with tickets that aren't on the roadmap.

## The coordinator's roadmap rhythm

1. **Planning with the owner:** edit `docs/roadmap.md`, commit it on the integration branch, then run `roadmap.py sync`, check the plan, and run `sync --apply`.
2. **Break each new epic into stories and tasks** with `tracker.py story`/`task`. A story that needs a decision first gets an ADR and `--depends ADR-NNNN`.
3. **Weekly, at sprint end and before each release:** run `roadmap.py status`.
   - **At risk:** re-plan. Cut scope, move an epic to a later milestone in the file and re-sync, open a lane, or chase the decisions holding it up.
   - **Drift:** resolve it by putting the item on the roadmap or closing it.
4. **For the owner:** publish `status` as a page they can read; the `--json` output carries the data.
