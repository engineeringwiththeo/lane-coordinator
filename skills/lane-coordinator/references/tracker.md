# Ticket tracking (`scripts/tracker.py`)

Tickets are GitHub issues, and the state of every ticket lives in its labels, so `gh issue list --label …` and the issue sidebar always show the truth. Every change also leaves a comment on the issue, which serves as the audit trail and the claim lock.

## Config (`config.json` → `tracker`)

| Key | Default | Meaning |
|---|---|---|
| `project` | repo folder name | prefix of the session tags |
| `repo` | `gh repo view` | `owner/name` |
| `session_label` | `{project}-{session}` | session tag, e.g. `myhoa2-03` |
| `coordinator_session` | `00` | the only session that may set merged / deployed / done / cancelled |
| `milestone_mode` | `github` | `github` = native milestones; `label` = `milestone:<name>` labels |
| `close_on` | `done` | first state that closes the issue: `merged`, `deployed` or `done` |
| `stale_hours` | `24` | a claim with no issue update for this long shows as stale on the board |
| `sessions` | `12` | how many `<project>-NN` labels `setup` creates (00 to N) |
| `status_prefix` | `status:` | prefix of the status labels |

## Labels

| Label | Meaning |
|---|---|
| `type:epic`, `type:story`, `type:task`, `type:milestone` | hierarchy level |
| `epic:<key>` | the epic the ticket belongs to (keys are slugs or numbers) |
| `feature:<slug>` | optional feature tag |
| `milestone:<name>` | the milestone, in label mode only |
| `lane:NN` | the planned queue (ownership) |
| `<project>-NN` | the claiming session: present only while the ticket is in-progress, testing, blocked or merging |
| `status:<state>` | exactly one per issue |
| `size:S\|M\|L`, `priority:pN` | scheduling inputs |

The body of each story or task carries `Parent: #N` and `Depends on: …` lines. The DAG reads them, and they also link the issue as a GitHub sub-issue.

## States

| State | Who sets it | Session tag | Issue |
|---|---|---|---|
| todo | creation, release | removed | open (reopened if it was closed) |
| in-progress | claim / session | kept | open |
| testing | session | kept | open |
| blocked | session (`--note` required) | kept | open |
| merging | session (`--pr`) | kept | open |
| merged | coordinator | released | closed if `close_on` = merged |
| deployed | coordinator (`deployed --from-pr`) | released | closed if `close_on` ≤ deployed |
| done | coordinator, after review | released | closed |
| cancelled | coordinator | released | closed as not planned |

Allowed moves are defined in `TRANSITIONS` in `tracker.py`. A move outside them is refused unless you pass `--force`. An issue with no status label counts as todo while it is open, and done once it is closed.

How the DAG reads these states:
- merged, deployed, done and cancelled count as **done**;
- in-progress, testing and merging, or any claim, count as **in progress**;
- blocked means **not ready**.

## Claims are race-safe

1. `claim` refuses if another session's tag is on the issue, if the status isn't todo, or if this session already holds an active ticket.
2. Otherwise it posts `<!-- lane:claim <tag> <nonce> -->`, adds its tag and `status:in-progress`, and re-reads the comments.
3. Since the last release or reset, the earliest claim comment wins. A session that loses removes its tag and posts a release comment.
4. Two sessions racing for one ticket therefore always agree on a single owner.

## Worktree cleanup

`cleanup [--worktree PATH] [--remove] [--force]`:
1. Refuses if the worktree has uncommitted changes, or if its branch has no merged PR (`gh pr list --head <branch> --state merged`). `--force` skips the PR check.
2. Fetches.
3. Then either detaches the worktree at `origin/<integration_branch>` and deletes the local branch, which suits long-lived lane worktrees, or with `--remove` deletes the worktree entirely.
4. It never removes the main checkout.
