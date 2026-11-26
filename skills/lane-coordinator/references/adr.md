# Architecture Decision Records (`scripts/adr.py`)

An ADR records one decision: its context, the decision itself, its consequences, and the alternatives rejected. The coordinator owns ADRs. Sessions build on accepted ADRs, and they may draft a new ADR, but they never decide one.

## Files

- **Location:** `<adr.dir>/NNNN-slug.md` (default `docs/adr`), numbered from 0001. `0000-template.md` is the template; `adr.py init` creates it, and you can edit it to match the project.
- **Header:** the first line is `# ADR-NNNN: Title`, followed by a `- **Status:** …` line. Optional lines follow: `Decision issue`, `Decision owner`, `Supersedes`, `Relates to`.
- **Status:** the line starts with one of Proposed, Accepted, Rejected, Deprecated or "Superseded by ADR-MMMM", followed by a date. "Accepted…" is the only status that satisfies a dependency.
- **Accepted ADRs are never edited in substance.** To change a decision, write a new ADR and run `adr.py status OLD superseded --by NEW`.

## Where ADRs enter the loop

| When | Command | What it does |
|---|---|---|
| Setup | `adr.py init` | creates the folder and the template |
| A story needs a decision first | `adr.py new "Title" [--issue N] [--owner WHO]`, then `tracker.py story … --depends ADR-NNNN` | writes a Proposed ADR. The story, and its tasks, stay blocked in the DAG until the ADR is accepted |
| Planning | `adr.py pending` | lists Proposed ADRs ranked by the work they block, directly and downstream. Chase the top ones with the owner |
| The owner decides | `adr.py status NNNN accepted --note "…"` | runs only on the coordinator session (00). The blocked tickets become ready on the next `work_queue.py` run |
| Briefing a session | `adr.py for-ticket N` | the ADRs named by the ticket, its parent story and its epic, with their status. Put them in the brief as required reading. A non-accepted ADR here means the ticket shouldn't have been claimed |
| Reviewing a PR | `adr.py check --pr P` | exits 1 when the PR relies on an ADR that is missing or not accepted, or changes an ADR's Status. For your own decision PRs, use `--coordinator` |
| Changing a decision | `adr.py new … --supersedes OLD`, then `adr.py status OLD superseded --by NEW` | links both ADRs |

## Rules for sessions (put these in briefs)

- **Read first:** read every ADR that `adr.py for-ticket` lists before writing code, and follow their Decision sections.
- **No ADR covers it:** if the work needs an architectural decision that no accepted ADR covers, don't decide it in code. Run `tracker.py status N blocked --note "needs a decision: …"` and tell the coordinator. You may draft the ADR (`adr.py new`) in your PR as **Proposed**.
- **Never change an ADR's Status line.** `adr.py check` fails the review if you do.

## Review checklist (coordinator)

- `adr.py check --pr P` passes.
- The diff follows the Decision section of each ADR it names, and doesn't quietly contradict any other accepted ADR in the area it touches.
- If review shows a decision changed, write the superseding ADR before merging.
