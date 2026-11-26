#!/usr/bin/env python3
"""Ticket tracking on GitHub issues: milestone → epic → story → task, session claims and a status workflow.

Usage: python3 -I tracker.py [--repo PATH] [--session NN] <command> ...

  setup [--sessions N] [--dry-run]                  create the status/type/session labels (idempotent)
  milestone NAME [--due YYYY-MM-DD]                 create a milestone (GitHub milestone, or a label in label mode)
  epic TITLE --key SLUG [--milestone M] [--feature F] [--body TEXT]
  story TITLE --epic SLUG [--feature F] [--milestone M] [--size S|M|L] [--priority pN] [--depends "#12, ADR-0003"]
              [--lane NN] [--body TEXT]             a sub-issue of the epic; inherits its milestone
  task TITLE --story N [--size] [--depends] [--body] a sub-issue of the story; inherits epic/feature/milestone/lane
  claim N [--force]                                 claim a ticket for this session (race-safe)
  next [--lanes 01,02] [--dry-run]                  claim the best ready ticket from the DAG work queue
  status N STATE [--note TEXT] [--pr N] [--force]   move a ticket through the workflow
  release N [--note TEXT]                           give a claim back (status → todo)
  mine                                              this session's claimed tickets
  board [--milestone M] [--epic SLUG] [--json OUT]  status counts per milestone and epic, claims, stale and odd tickets
  deployed (--from-pr N | --issues 1,2)             move merged tickets to deployed (e.g. after a promotion PR)

  cleanup [--worktree PATH] [--remove] [--force]   after your ticket merged: delete its branch, reset the worktree
                                                    to origin/<integration> (or remove it with --remove)

Workflow: todo → in-progress → testing → merging → merged → deployed → done, plus blocked and cancelled.
Sessions move their own tickets up to merging (ready for review). Only the coordinator session
(tracker.coordinator_session, default 00) sets merged, deployed, done and cancelled; done closes the issue.
A session holds one active ticket at a time.
The session is --session, else $LANE_SESSION, else the worktree folder's "-NN" suffix (myhoa2-03 → 03), else 00.
"""
import argparse, json, os, re, subprocess, sys, uuid
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dag
from common import load_json, repo_root, state_dir
from ghapi import GitHub, GhError, detect_repo

STATUSES = ["todo", "in-progress", "testing", "blocked", "merging", "merged", "deployed", "done", "cancelled"]
TRANSITIONS = {
    "todo": {"in-progress", "blocked", "cancelled"},
    "in-progress": {"todo", "testing", "blocked", "merging", "cancelled"},
    "testing": {"in-progress", "blocked", "merging", "todo", "cancelled"},
    "blocked": {"todo", "in-progress", "testing", "cancelled"},
    "merging": {"in-progress", "testing", "blocked", "merged", "cancelled"},
    "merged": {"deployed", "done", "in-progress"},   # back to in-progress after a revert
    "deployed": {"done", "in-progress"},
    "done": {"in-progress"},                          # reopened
    "cancelled": {"todo"},
}
HELD = {"in-progress", "testing", "blocked", "merging"}  # states where the claiming session keeps its label
COORDINATOR_ONLY = {"merged", "deployed", "done", "cancelled"}  # the coordinator merges, reviews and closes
DONE_LIKE = {"merged", "deployed", "done", "cancelled"}
RANK = {"merged": 1, "deployed": 2, "done": 3}
COLORS = {"todo": "d4d4d8", "in-progress": "3b82f6", "testing": "f59e0b", "blocked": "dc2626", "merging": "a855f7", "merged": "16a34a",
          "deployed": "0d9488", "done": "15803d", "cancelled": "71717a"}
TYPES = {"type:milestone": "fbbf24", "type:epic": "7c3aed", "type:story": "2563eb", "type:task": "64748b"}

DEFAULT_TRACKER = {"project": None, "repo": None, "session_label": "{project}-{session}", "milestone_mode": "github",
                   "close_on": "done", "stale_hours": 24, "sessions": 12, "status_prefix": "status:",
                   "coordinator_session": "00"}


class Fail(Exception):
    pass


# ─── config and session ──────────────────────────────────────────────────────────────────────────

def tracker_cfg(cfg, root):
    t = {**DEFAULT_TRACKER, **(cfg.get("tracker") or {})}
    t["project"] = t["project"] or os.path.basename(root)
    if t["close_on"] not in RANK:
        raise Fail(f"tracker.close_on must be one of {', '.join(RANK)}")
    return t


def session_label(t, session):
    return t["session_label"].format(project=t["project"], session=str(session).zfill(2))


def session_re(t):
    pat = re.escape(t["session_label"]).replace(re.escape("{project}"), re.escape(t["project"]))
    return re.compile("^" + pat.replace(re.escape("{session}"), r"(\d+)") + "$")


def infer_session(t, cwd=None):
    if os.environ.get("LANE_SESSION"):
        return os.environ["LANE_SESSION"].zfill(2)
    try:
        top = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=cwd, capture_output=True, text=True,
                             check=True).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "00"
    m = re.match(rf"^{re.escape(t['project'])}-(\d+)$", os.path.basename(top))
    return m.group(1).zfill(2) if m else "00"


def status_of(t, issue):
    p = t["status_prefix"]
    found = [l[len(p):] for l in issue["labels"] if l.startswith(p) and l[len(p):] in STATUSES]
    if found:
        return found[0]
    return "todo" if issue["state"] == "open" else "done"


def holders(t, issue):
    rx = session_re(t)
    return [l for l in issue["labels"] if rx.match(l)]


# ─── claims ──────────────────────────────────────────────────────────────────────────────────────

MARK = re.compile(r"<!-- lane:(claim|release|reset) ?([^ >]*)? ?[^>]*-->")


def claim_winner(comments):
    """The session holding the claim, from the marker comments in order: the earliest claim since the last
    release/reset wins, so two sessions racing on one ticket agree on the same answer."""
    holder = None
    for c in sorted(comments, key=lambda c: (c["created_at"], c["id"])):
        for kind, who in MARK.findall(c["body"]):
            if kind == "claim" and holder is None:
                holder = who
            elif kind == "release" and who == holder:
                holder = None
            elif kind == "reset":
                holder = None
    return holder


def claim(gh, t, n, session, force=False):
    me = session_label(t, session)
    i = gh.issue(n)
    if i["state"] != "open" and not force:
        raise Fail(f"#{n} is closed")
    st, others = status_of(t, i), [h for h in holders(t, i) if h != me]
    if me in i["labels"] and st in HELD:
        return {"claimed": True, "issue": n, "session": me, "note": "already yours"}
    if others and not force:
        raise Fail(f"#{n} is claimed by {', '.join(others)}")
    if st != "todo" and not force:
        raise Fail(f"#{n} is {st}; only todo tickets can be claimed (--force to override)")
    if not force and str(session).zfill(2) != t["coordinator_session"]:
        active = [i2["number"] for i2 in gh.issues(state="open", labels=[me])
                  if i2["number"] != n and status_of(t, i2) in HELD]
        if active:
            raise Fail(f"{me} already holds #{', #'.join(map(str, active))}; finish, block or release it first")
    gh.comment(n, f"<!-- lane:claim {me} {uuid.uuid4().hex[:8]} -->\n🔒 Claimed by **{me}**.")
    gh.add_labels(n, [me, t["status_prefix"] + "in-progress"])
    for s in STATUSES:
        if s != "in-progress" and t["status_prefix"] + s in i["labels"]:
            gh.remove_label(n, t["status_prefix"] + s)
    winner = claim_winner(gh.comments(n))
    if winner != me:
        gh.remove_label(n, me)
        gh.comment(n, f"<!-- lane:release {me} -->\n↩︎ {me} lost the claim race to **{winner}**.")
        return {"claimed": False, "issue": n, "session": me, "winner": winner}
    return {"claimed": True, "issue": n, "session": me}


def set_status(gh, t, n, new, session=None, note=None, pr=None, force=False):
    if new not in STATUSES:
        raise Fail(f"unknown status {new}; use one of {', '.join(STATUSES)}")
    i = gh.issue(n)
    cur = status_of(t, i)
    if new == cur:
        return {"issue": n, "status": new, "changed": False}
    if new not in TRANSITIONS[cur] and not force:
        raise Fail(f"#{n}: {cur} → {new} isn't allowed (from {cur}: {', '.join(sorted(TRANSITIONS[cur]))}; "
                   "--force to override)")
    if new == "blocked" and not note:
        raise Fail("blocked needs --note saying what it waits on")
    if new in COORDINATOR_ONLY and session is not None and str(session).zfill(2) != t["coordinator_session"] \
            and not force:
        raise Fail(f"only the coordinator (session {t['coordinator_session']}) sets {new}; "
                   "report the PR as ready and let it review")
    me = session_label(t, session) if session is not None else None
    held = holders(t, i)
    markers, lines = [], [f"**{cur} → {new}**" + (f" by {me}" if me else "") + (f" · PR #{pr}" if pr else "")]
    if note:
        lines.append(note)

    gh.add_labels(n, [t["status_prefix"] + new])
    for s in STATUSES:
        if s != new and t["status_prefix"] + s in i["labels"]:
            gh.remove_label(n, t["status_prefix"] + s)
    if new in HELD:
        if not held and me:
            gh.add_labels(n, [me])
            markers.append(f"<!-- lane:claim {me} {uuid.uuid4().hex[:8]} -->")
    else:
        for h in held:
            gh.remove_label(n, h)
            markers.append(f"<!-- lane:release {h} -->")
        if new == "todo":
            markers.append("<!-- lane:reset -->")
    gh.comment(n, "\n".join(markers + lines))

    closed = i["state"] != "open"
    if new == "cancelled" and not closed:
        gh.set_state(n, "closed", "not_planned")
    elif new in RANK and RANK[new] >= RANK[t["close_on"]] and not closed:
        gh.set_state(n, "closed", "completed")
    elif (new in HELD or new == "todo") and closed:
        gh.set_state(n, "open")
    return {"issue": n, "status": new, "from": cur, "changed": True}


# ─── creating tickets ────────────────────────────────────────────────────────────────────────────

def milestone_ref(gh, t, name):
    """GitHub mode → (milestone number, []); label mode → (None, ["milestone:<name>"])."""
    if not name:
        return None, []
    if t["milestone_mode"] == "label":
        return None, [f"milestone:{name}"]
    ms = gh.milestones()
    if name not in ms:
        raise Fail(f'no milestone "{name}"; create it with: tracker.py milestone "{name}"')
    return ms[name], []


def ensure_labels(gh, names, have=None):
    have = gh.labels() if have is None else have
    for name in names:
        if name in have:
            continue
        color = COLORS.get(name.split(":", 1)[-1], TYPES.get(name, "c7d2fe" if name.startswith("epic:") else "e5e7eb"))
        try:
            gh.create_label(name, color)
        except GhError as e:
            if "already_exists" not in str(e):
                raise
        have.add(name)


def find_epic(gh, key):
    found = [i for i in gh.issues(state="all", labels=["type:epic", f"epic:{key}"])]
    if not found:
        raise Fail(f"no epic issue labelled type:epic + epic:{key}; create it with: tracker.py epic TITLE --key {key}")
    return sorted(found, key=lambda i: i["number"])[0]


def _body(text, parent=None, depends=None):
    parts = [text.strip()] if text else []
    meta = ([f"Parent: #{parent}"] if parent else []) + ([f"Depends on: {depends}"] if depends else [])
    return "\n\n".join(parts + (["\n".join(meta)] if meta else []))


def create(gh, t, kind, title, *, key=None, epic=None, story=None, milestone=None, feature=None, size=None,
           priority=None, depends=None, lane=None, body=""):
    labels, ms_num, parent = [f"type:{kind}"], None, None
    if kind == "epic":
        labels.append(f"epic:{key}")
        ms_num, extra = milestone_ref(gh, t, milestone)
        labels += extra
    elif kind == "story":
        e = find_epic(gh, epic)
        parent = e
        labels.append(f"epic:{epic}")
        if milestone:
            ms_num, extra = milestone_ref(gh, t, milestone)
        elif t["milestone_mode"] == "label":
            extra = [l for l in e["labels"] if l.startswith("milestone:")]
        else:
            ms_num, extra = milestone_ref(gh, t, e["milestone"])
        labels += extra
    elif kind == "task":
        s = gh.issue(story)
        parent = s
        labels += [l for l in s["labels"] if re.match(r"^(epic|feature|milestone|lane|priority):", l)]
        if t["milestone_mode"] != "label" and s["milestone"]:
            ms_num, _ = milestone_ref(gh, t, s["milestone"])
    if feature:
        labels.append(f"feature:{feature}")
    if size:
        labels.append(f"size:{size.upper()}")
    if priority:
        labels.append(f"priority:{priority if priority.startswith('p') else 'p' + priority}")
    if lane:
        labels.append(f"lane:{str(lane).zfill(2)}")
    labels.append(t["status_prefix"] + "todo")
    labels = list(dict.fromkeys(labels))
    ensure_labels(gh, labels)
    issue = gh.create_issue(title, _body(body, parent["number"] if parent else None, depends), labels, ms_num)
    if parent:
        gh.add_sub_issue(parent["number"], issue["id"])
    return {"issue": issue["number"], "labels": labels, "parent": parent["number"] if parent else None}


# ─── reports ─────────────────────────────────────────────────────────────────────────────────────

def board(gh, t, milestone=None, epic=None, now=None):
    now = now or datetime.now(timezone.utc)
    issues = [i for i in gh.issues(state="all") if "type:epic" not in i["labels"] and "type:milestone" not in i["labels"]]
    if milestone:
        issues = [i for i in issues if i["milestone"] == milestone or f"milestone:{milestone}" in i["labels"]]
    if epic:
        issues = [i for i in issues if f"epic:{epic}" in i["labels"]]

    def group_of(i, prefix):
        return next((l[len(prefix):] for l in i["labels"] if l.startswith(prefix)), "—")

    def ms_of(i):
        return i["milestone"] or group_of(i, "milestone:")

    rows = {}
    for i in issues:
        key = (ms_of(i), group_of(i, "epic:"))
        rows.setdefault(key, {s: 0 for s in STATUSES})[status_of(t, i)] += 1
    claims, stale, odd = [], [], []
    stale_before = now - timedelta(hours=t["stale_hours"])
    for i in issues:
        st, hs = status_of(t, i), holders(t, i)
        statuses = [l for l in i["labels"] if l.startswith(t["status_prefix"])]
        if hs and i["state"] == "open":
            claims.append({"issue": i["number"], "title": i["title"], "session": ", ".join(hs), "status": st})
            if i["updated_at"] and datetime.fromisoformat(i["updated_at"].replace("Z", "+00:00")) < stale_before:
                stale.append({"issue": i["number"], "session": ", ".join(hs), "status": st, "updated": i["updated_at"]})
        why = []
        if len(statuses) > 1:
            why.append(f"{len(statuses)} status labels")
        if len(hs) > 1:
            why.append(f"claimed by {len(hs)} sessions")
        if st in HELD and st != "blocked" and not hs:
            why.append(f"{st} with no session")
        if st in HELD and i["state"] != "open":
            why.append(f"closed but {st}")
        if st in ("todo", "merged", "deployed", "done", "cancelled") and hs:
            why.append(f"{st} but still claimed")
        if why:
            odd.append({"issue": i["number"], "title": i["title"], "why": "; ".join(why)})
    return {"rows": [{"milestone": k[0], "epic": k[1], **v} for k, v in sorted(rows.items())],
            "claims": sorted(claims, key=lambda c: (c["session"], c["issue"])), "stale": stale, "odd": odd}


def render_board(b):
    cols = STATUSES
    out = ["# Board", "", "| Milestone | Epic | " + " | ".join(cols) + " |", "|---|---|" + "---:|" * len(cols)]
    totals = {s: 0 for s in cols}
    for r in b["rows"]:
        out.append(f"| {r['milestone']} | {r['epic']} | " + " | ".join(str(r[s] or "") for s in cols) + " |")
        for s in cols:
            totals[s] += r[s]
    out.append("| **all** | | " + " | ".join(f"**{totals[s]}**" for s in cols) + " |")
    out += ["", "## Claims", ""] + ([f"- **{c['session']}** #{c['issue']} {c['title']} · {c['status']}"
                                       for c in b["claims"]] or ["_none_"])
    out += ["", "## Stale claims (no update within tracker.stale_hours)", ""] + (
        [f"- #{s['issue']} · {s['session']} · {s['status']} · last update {s['updated']}" for s in b["stale"]]
        or ["_none_"])
    out += ["", "## Needs fixing", ""] + ([f"- #{o['issue']} {o['title']}: {o['why']}" for o in b["odd"]]
                                          or ["_none_"])
    return "\n".join(out) + "\n"


# ─── worktree cleanup ────────────────────────────────────────────────────────────────────────────

def _git(args, cwd, check=True):
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and p.returncode != 0:
        raise Fail(f"git {' '.join(args)}: {p.stderr.strip()}")
    return p.stdout.strip()


def pr_merged(branch, cwd):
    """True when a PR from this branch has merged (squash merges hide it from git ancestry)."""
    p = subprocess.run(["gh", "pr", "list", "--head", branch, "--state", "merged", "--json", "number"], cwd=cwd,
                       capture_output=True, text=True)
    return p.returncode == 0 and bool(json.loads(p.stdout or "[]"))


def cleanup(worktree, base, remove=False, force=False, merged_check=pr_merged, remote="origin"):
    """Reset a lane worktree after its ticket merged: refuse on uncommitted work or an unmerged branch, then
    switch to a detached origin/<base> and delete the local branch (or remove the whole worktree)."""
    top = _git(["rev-parse", "--show-toplevel"], worktree)
    main = os.path.dirname(_git(["rev-parse", "--path-format=absolute", "--git-common-dir"], top))
    if _git(["status", "--porcelain"], top):
        raise Fail(f"{top} has uncommitted changes; commit, stash or discard them first")
    branch = _git(["branch", "--show-current"], top)
    if remove and os.path.realpath(top) == os.path.realpath(main):
        raise Fail("refusing to remove the main checkout")
    if branch and branch != base and not force and not merged_check(branch, top):
        raise Fail(f"branch {branch} has no merged PR; use --force if it really is done")
    has_remote = bool(_git(["remote"], top, check=False))
    if has_remote:
        _git(["fetch", "--prune", remote], top)
    target = f"{remote}/{base}" if has_remote else base
    done = []
    if remove:
        _git(["worktree", "remove", top], main)
        done.append(f"removed worktree {top}")
    else:
        _git(["switch", "--detach", target], top)
        done.append(f"worktree now at {target}")
    if branch and branch != base:
        _git(["branch", "-D", branch], main)
        done.append(f"deleted branch {branch}")
    _git(["worktree", "prune"], main)
    return {"worktree": top, "branch": branch, "done": done}


# ─── CLI ─────────────────────────────────────────────────────────────────────────────────────────

def next_ticket(gh, t, session, repo, lanes=None, dry_run=False, tries=5):
    import work_queue
    me = session_label(t, session)
    held = [i["number"] for i in gh.issues(state="open", labels=[me]) if status_of(t, i) in HELD]
    if held and str(session).zfill(2) != t["coordinator_session"] and not dry_run:
        return {"claimed": False, "note": f"{me} already holds #{', #'.join(map(str, held))}; finish it first"}
    r, _data = work_queue.compute(repo, lanes)
    lane = str(session).zfill(2)
    ready = sorted((n for n in r["nodes"].values() if n["ready"] and n["issue"]), key=dag.score_key)
    active = set(r["activeLanes"])
    cands = [n for n in ready if n["lane"] == lane] + [n for n in ready if n["lane"] is None or n["lane"] not in active]
    if dry_run:
        return {"candidates": [{"id": n["id"], "issue": n["issue"], "lane": n["lane"]} for n in cands[:tries]]}
    for n in cands[:tries]:
        try:
            res = claim(gh, t, n["issue"], session)
        except Fail:
            continue
        if res["claimed"]:
            return {**res, "id": n["id"], "title": n["title"], "size": n["size"], "lane": n["lane"],
                    "borrowed": n["lane"] != lane}
    return {"claimed": False, "note": "nothing ready to claim for this lane"}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--repo")
    ap.add_argument("--session")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("setup"); s.add_argument("--sessions", type=int); s.add_argument("--dry-run", action="store_true")
    s = sub.add_parser("milestone"); s.add_argument("name"); s.add_argument("--due")
    for kind in ("epic", "story", "task"):
        s = sub.add_parser(kind)
        s.add_argument("title")
        s.add_argument("--body", default="")
        s.add_argument("--feature")
        s.add_argument("--size")
        s.add_argument("--priority")
        s.add_argument("--depends")
        if kind == "epic":
            s.add_argument("--key", required=True); s.add_argument("--milestone")
        if kind == "story":
            s.add_argument("--epic", required=True); s.add_argument("--milestone"); s.add_argument("--lane")
        if kind == "task":
            s.add_argument("--story", type=int, required=True)
    s = sub.add_parser("claim"); s.add_argument("issue", type=int); s.add_argument("--force", action="store_true")
    s = sub.add_parser("next"); s.add_argument("--lanes"); s.add_argument("--dry-run", action="store_true")
    s = sub.add_parser("status"); s.add_argument("issue", type=int); s.add_argument("state")
    s.add_argument("--note"); s.add_argument("--pr", type=int); s.add_argument("--force", action="store_true")
    s = sub.add_parser("release"); s.add_argument("issue", type=int); s.add_argument("--note")
    sub.add_parser("mine")
    s = sub.add_parser("board"); s.add_argument("--milestone"); s.add_argument("--epic"); s.add_argument("--json")
    s = sub.add_parser("deployed"); s.add_argument("--from-pr", type=int); s.add_argument("--issues")
    s = sub.add_parser("cleanup"); s.add_argument("--worktree"); s.add_argument("--remove", action="store_true")
    s.add_argument("--force", action="store_true")
    a = ap.parse_args(argv)

    root = repo_root(a.repo)
    cfg = load_json(os.path.join(state_dir(a.repo), "config.json"), {}) or {}
    t = tracker_cfg(cfg, root)
    if a.cmd == "cleanup":
        out = cleanup(a.worktree or a.repo or os.getcwd(), cfg.get("integration_branch", "main"), a.remove, a.force)
        print(json.dumps(out, indent=2))
        return
    session = (a.session or infer_session(t, a.repo)).zfill(2)
    gh = GitHub(t["repo"] or detect_repo(a.repo or root), cwd=a.repo or root)
    out = lambda x: print(json.dumps(x, indent=2, ensure_ascii=False))

    if a.cmd == "setup":
        want = [t["status_prefix"] + s for s in STATUSES] + list(TYPES) + \
               [session_label(t, n) for n in range(0, (a.sessions or t["sessions"]) + 1)]
        have = gh.labels()
        missing = [w for w in want if w not in have]
        if not a.dry_run:
            ensure_labels(gh, missing, have)
        out({"created" if not a.dry_run else "would_create": missing, "existing": len(want) - len(missing)})
    elif a.cmd == "milestone":
        if t["milestone_mode"] == "label":
            ensure_labels(gh, [f"milestone:{a.name}"])
            out({"label": f"milestone:{a.name}"})
        else:
            ms = gh.milestones()
            out({"milestone": ms[a.name] if a.name in ms else gh.create_milestone(a.name, a.due), "title": a.name})
    elif a.cmd in ("epic", "story", "task"):
        out(create(gh, t, a.cmd, a.title, key=getattr(a, "key", None), epic=getattr(a, "epic", None),
                   story=getattr(a, "story", None), milestone=getattr(a, "milestone", None), feature=a.feature,
                   size=a.size, priority=a.priority, depends=a.depends, lane=getattr(a, "lane", None), body=a.body))
    elif a.cmd == "claim":
        out(claim(gh, t, a.issue, session, a.force))
    elif a.cmd == "next":
        lanes = [l.strip() for l in (a.lanes or "").split(",") if l.strip()] or None
        out(next_ticket(gh, t, session, a.repo, lanes, a.dry_run))
    elif a.cmd == "status":
        out(set_status(gh, t, a.issue, a.state, session, a.note, a.pr, a.force))
    elif a.cmd == "release":
        out(set_status(gh, t, a.issue, "todo", session, a.note or "released", force=True))
    elif a.cmd == "mine":
        me = session_label(t, session)
        out([{"issue": i["number"], "title": i["title"], "status": status_of(t, i)}
             for i in gh.issues(state="open", labels=[me])])
    elif a.cmd == "board":
        b = board(gh, t, a.milestone, a.epic)
        sys.stdout.write(render_board(b))
        if a.json:
            with open(a.json, "w") as fh:
                json.dump(b, fh, indent=2)
    elif a.cmd == "deployed":
        nums = dag.referenced_issues(gh.pr_body(a.from_pr)) if a.from_pr else \
            [int(x.strip().lstrip("#")) for x in (a.issues or "").split(",") if x.strip()]
        res = []
        for n in nums:
            try:
                i = gh.issue(n)
                if status_of(t, i) == "merged":
                    res.append(set_status(gh, t, n, "deployed", session, pr=a.from_pr))
                else:
                    res.append({"issue": n, "skipped": status_of(t, i)})
            except (Fail, GhError) as e:
                res.append({"issue": n, "error": str(e)})
        out(res)


if __name__ == "__main__":
    try:
        main()
    except (Fail, GhError) as e:
        sys.exit(f"error: {e}")
