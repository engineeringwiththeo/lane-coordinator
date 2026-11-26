"""The dependency-DAG work queue: pure logic, no network or disk (see work_queue.py for the CLI).

Answers the coordinator's question, *what should each lane pick up next?*, from three inputs: stories (from a
backlog file or from GitHub issues), the issues and PRs on GitHub, and ADR statuses. An item is **ready** when it is
not done, nobody has an open PR on it, and everything it depends on is satisfied. Ready items are ranked by how much
not-done work they unblock (transitively), then priority, then the length of the chain hanging below them, then
smaller first. Each active lane takes the top of its own queue; a lane with nothing ready borrows the best
unassigned or paused-lane item.

Nothing here closes or edits an issue: "merged but still open" is reported for the coordinator to act on.
Ported from myhoa2's scripts/lib/work-queue.ts; field names stay camelCase so --json output matches it.
"""
import re
import sys
from collections import deque

sys.setrecursionlimit(max(10000, sys.getrecursionlimit()))

SIZE_ORDER = {"S": 0, "M": 1, "L": 2}

# Project-specific names; work_queue.py overrides these from config.json.
DEFAULTS = {
    "lane_label": r"^lane:(\d+)$",
    "epic_issue_label": "type:epic",
    "story_label": "type:story",
    "epic_label_prefix": "epic:",
    "priority_label": r"^priority:p(\d)$",
    "task_label": "type:task",
    "status_prefix": "status:",
    "session_label_re": None,  # e.g. ^myhoa2-(\d+)$ : set by work_queue.py from the tracker config
}

DONE_STATUSES = {"merged", "staging", "deployed", "done", "cancelled"}
ACTIVE_STATUSES = {"in-progress", "testing", "merging"}


def _natural(s):
    """Approximates JS localeCompare(..., {numeric: true}): case-insensitive, digit runs compared as numbers."""
    return [(0, int(t), "") if t.isdigit() else (1, 0, t.lower()) for t in re.findall(r"\d+|\D+", s or "")]


def _locale(s):
    return ((s or "").lower(), s or "")


# ─── Parsing the backlog ─────────────────────────────────────────────────────────────────────────

def split_top_level(s, seps=",;"):
    """Splits on any of `seps` outside brackets, so `E26 (BudgetBar, Tile)` stays one item."""
    out, depth, cur = [], 0, ""
    for ch in s:
        if ch in "([":
            depth += 1
        elif ch in ")]" and depth > 0:
            depth -= 1
        if depth == 0 and ch in seps:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    out.append(cur)
    return [x.strip() for x in out if x.strip()]


_CONT = re.compile(r"^(?:§\s*)?\d+(?:\s*[–-]\s*\d+)?(?:\s+and\s+(?:§\s*)?\d+)?$")


def join_continuations(items):
    """`Conflicts 1, 3–5` and `ADR-0085 §1, §4`: bare numbers belong to the item before them."""
    out = []
    for item in items:
        if out and _CONT.match(item):
            out[-1] += f", {item}"
        else:
            out.append(item)
    return out


def parse_dep(text):
    """One `depends on:` item, classified. None for "none"."""
    raw = re.sub(r"\.$", "", text.strip()).strip()
    if not raw or re.match(r"^(none|—|-)$", raw, re.I):
        return None
    m = re.match(r"^(E\d+-S\d+)(?![\w-])", raw)
    if m:
        return {"raw": raw, "kind": "story", "story": m.group(1)}
    m = re.match(r"^ADR-(\d{4})(?!\d)", raw)
    if m:
        return {"raw": raw, "kind": "adr", "adr": m.group(1)}
    m = re.match(r"^(?:EPIC\s+(\d+)|E(\d+))(?![\w-])(.*)$", raw, re.I | re.S)
    # "EPIC 16 extension" and "EPIC 22's AI-provider ADR" name work that is not that epic's stories.
    if m and not re.match(r"^\s*(extension\b|['’]s\b)", m.group(3), re.I):
        return {"raw": raw, "kind": "epic", "epic": int(m.group(1) or m.group(2))}
    m = re.match(r"^#(\d+)$", raw)
    if m:
        return {"raw": raw, "kind": "issue", "issue": int(m.group(1))}
    m = re.match(r"^epic:([\w.-]+)$", raw)
    if m:
        return {"raw": raw, "kind": "epic", "epic": _epic_key(m.group(1))}
    return {"raw": raw, "kind": "free"}


def _epic_key(v):
    return int(v) if v.isdigit() else v


def parse_deps(text):
    return [d for d in (parse_dep(x) for x in join_continuations(split_top_level(text))) if d]


HEADING = re.compile(r"^###\s+(E(\d+)-S\d+)\b(.*)$")


def _problem(line, what, id=None):
    p = {"line": line}
    if id is not None:
        p["id"] = id
    p["what"] = what
    return p


def parse_heading(text, line=0):
    """One `### E16-S58 ([#598](…)) — Title · size:L · priority:p1 · depends on: …` heading."""
    problems = []
    h = HEADING.match(text)
    if not h:
        return None, problems
    id_, rest = h.group(1), h.group(3).strip()
    issue = None
    im = re.match(r"^\(\[#(\d+)\]\([^)]*\)\)\s*", rest)
    if im:
        issue = int(im.group(1))
        rest = rest[im.end():]
    else:
        problems.append(_problem(line, "no `([#N](…))` issue link", id_))
    if re.match(r"^[—-]\s*", rest):
        rest = re.sub(r"^[—-]\s*", "", rest, count=1)
    else:
        problems.append(_problem(line, "no ` — ` before the title", id_))

    parts = re.split(r"\s+·\s+", rest)
    title_parts, size, priority, dep_text = [], None, None, None
    for i, raw in enumerate(parts):
        p = raw.strip()
        m = re.match(r"^size:\s*([SML])\b", p, re.I)
        if m:
            size = m.group(1).upper()
            continue
        m = re.match(r"^priority:\s*p([0-3])\b", p, re.I)
        if m:
            priority = int(m.group(1))
            continue
        m = re.match(r"^depends on:\s*(.*)$", p, re.I | re.S)
        if m:
            dep_text = " · ".join([m.group(1)] + parts[i + 1:])
            break
        if size is None and priority is None:
            title_parts.append(p)
        else:
            problems.append(_problem(line, f'unrecognised heading part "{p}"', id_))
    if size is None:
        problems.append(_problem(line, "no size:S|M|L", id_))
    if priority is None:
        problems.append(_problem(line, "no priority:pN", id_))
    if dep_text is None:
        problems.append(_problem(line, "no `depends on:`", id_))
    story = {"id": id_, "epic": int(h.group(2)), "issue": issue, "title": " · ".join(title_parts),
             "size": size, "priority": priority, "deps": parse_deps(dep_text) if dep_text is not None else [],
             "line": line}
    return story, problems


def parse_backlog(md):
    stories, problems, seen = [], [], {}
    for i, text in enumerate(md.split("\n")):
        line = i + 1
        if not text.startswith("### "):
            continue
        if not HEADING.match(text):
            if re.match(r"^###\s+E\d+", text, re.I):
                problems.append(_problem(line, f"malformed story heading: {text[:80]}"))
            continue
        story, probs = parse_heading(text, line)
        problems.extend(probs)
        if not story:
            continue
        if story["id"] in seen:
            problems.append(_problem(line, f"duplicate story id (first at line {seen[story['id']]})", story["id"]))
            continue
        seen[story["id"]] = line
        stories.append(story)
    return stories, problems


_PARENT_LINE = re.compile(r"^\s*(?:\*\*)?parent(?:\*\*)?\s*:?(?:\*\*)?\s*#(\d+)\s*$", re.I | re.M)
_DEP_LINE = re.compile(r"^\s*(?:\*\*)?(?:depends on|blocked by)(?:\*\*)?\s*:?(?:\*\*)?\s*(.+)$", re.I | re.M)


def stories_from_issues(issues, cfg=None):
    """Backlog-free mode: every open or closed issue is a story (only those labelled `story_label` or
    `task_label`, if any are).

    Size and priority come from `size:S|M|L` and `priority:pN` labels, the epic from an `epic:<key>` label,
    dependencies from `Depends on:` / `Blocked by:` lines in the body (`#12, #15`, `ADR-0003`, free text), and the
    parent story of a task from a `Parent: #N` line (written by tracker.py).
    """
    cfg = {**DEFAULTS, **(cfg or {})}
    kinds = {cfg["story_label"], cfg["task_label"]}
    pool = [i for i in issues if cfg["epic_issue_label"] not in i["labels"] and "type:milestone" not in i["labels"]]
    if any(kinds & set(i["labels"]) for i in pool):
        pool = [i for i in pool if kinds & set(i["labels"])]
    stories = []
    for i in sorted(pool, key=lambda x: x["number"]):
        labels = i["labels"]
        size = next((m.group(1).upper() for l in labels if (m := re.match(r"^size:([SML])$", l, re.I))), None)
        prio = next((int(m.group(1)) for l in labels if (m := re.match(r"^priority:p(\d)$", l))), None)
        epic = next((_epic_key(m.group(1)) for l in labels
                     if (m := re.match(rf"^{re.escape(cfg['epic_label_prefix'])}(.+)$", l))), None)
        pm = _PARENT_LINE.search(i.get("body") or "")
        deps = []
        for m in _DEP_LINE.finditer(i.get("body") or ""):
            deps.extend(d for d in parse_deps(m.group(1)) if d["raw"] != f"#{i['number']}")
        story = {"id": f"#{i['number']}", "epic": epic, "issue": i["number"], "title": i["title"],
                 "size": size, "priority": prio, "deps": deps, "line": 0}
        if pm:
            story["parent"] = int(pm.group(1))
        stories.append(story)
    # A task waits on whatever its parent story waits on.
    by_num = {s["issue"]: s for s in stories}
    for s in stories:
        p = by_num.get(s.get("parent"))
        if p:
            raws = {d["raw"] for d in s["deps"]}
            s["deps"] += [d for d in p["deps"] if d["raw"] not in raws]
    return stories


def adr_status_from_file(src):
    """An ADR's `- **Status:**` line."""
    m = re.search(r"^-\s*\*\*Status:\*\*\s*(.+)$", src, re.M)
    return m.group(1).strip() if m else None


def is_accepted(status):
    return bool(status) and bool(re.match(r"^accepted\b", status, re.I))


# ─── Done detection ──────────────────────────────────────────────────────────────────────────────

_REF = re.compile(r"\b(?:refs?|resolve[sd]?|close[sd]?|fix(?:e[sd])?)\b:?\s+(#\d+(?:\s*(?:,|and|&)\s*#\d+)*)", re.I)


def referenced_issues(body):
    """Issue numbers a PR body references with Refs / Resolves / Closes / Fixes (lists like `Refs #1, #2` too)."""
    out = {}
    for m in _REF.finditer(body or ""):
        for n in re.findall(r"#(\d+)", m.group(1)):
            out[int(n)] = None
    return list(out)


def is_partial_title(title):
    """"part A", "part 1", "(B)", "pure part)", "partial", "slice 2" mark a PR as not the whole story."""
    return bool(
        re.search(r"\bpart\s+(?:[A-Z]|\d+|one|two|three|i{1,3}|iv)\b", title, re.I)
        or re.search(r"\bpart\)", title, re.I)
        or re.search(r"\(\s*[A-Z]\s*\)", title)
        or re.search(r"\bpartial(?:ly)?\b", title, re.I)
        or re.search(r"\bslice\s+\d+\b", title, re.I)
        or re.search(r"\bfirst half\b|\bsecond half\b", title, re.I)
    )


def index_prs(prs):
    idx = {}
    for pr in prs:
        for n in referenced_issues(pr.get("body")):
            e = idx.setdefault(n, {"open": [], "merged": [], "partial": []})
            if pr["state"] == "OPEN":
                e["open"].append(pr)
            elif pr["state"] == "MERGED":
                (e["partial"] if is_partial_title(pr["title"]) else e["merged"]).append(pr)
    return idx


def issue_work(number, issues, prs):
    """Done: closed, or a merged whole-story PR references it and no open PR does. In progress: an open PR."""
    e = prs.get(number) if number is not None else None
    open_prs = [p["number"] for p in (e["open"] if e else [])]
    merged = [p["number"] for p in (e["merged"] if e else [])]
    partial = [p["number"] for p in (e["partial"] if e else [])]
    issue = issues.get(number) if number is not None else None
    base = {"openPrs": open_prs, "mergedPrs": merged, "partialPrs": partial}
    if issue and issue["state"] == "CLOSED":
        return {"status": "done", "mergedButOpen": False, **base}
    if open_prs:
        return {"status": "in-progress", "mergedButOpen": False, **base}
    if merged:
        return {"status": "done", "mergedButOpen": bool(issue), **base}
    return {"status": "todo", "mergedButOpen": False, **base}


# ─── The graph ───────────────────────────────────────────────────────────────────────────────────

def score_key(n):
    """Sort key: earlier = work it first."""
    return (-n["weight"], n["priority"] if n["priority"] is not None else 9, -n["depth"],
            SIZE_ORDER[n["size"]] if n["size"] else 9, _natural(n["id"]))


def find_cycles(edges):
    """Strongly connected components (Tarjan) of size > 1, plus self-loops."""
    counter = [0]
    idx, low, stack, on, out = {}, {}, [], set(), []

    def visit(v):
        idx[v] = low[v] = counter[0]
        counter[0] += 1
        stack.append(v)
        on.add(v)
        for w in edges.get(v, []):
            if w not in idx:
                visit(w)
                low[v] = min(low[v], low[w])
            elif w in on:
                low[v] = min(low[v], idx[w])
        if low[v] == idx[v]:
            scc = []
            while True:
                w = stack.pop()
                on.discard(w)
                scc.append(w)
                if w == v:
                    break
            if len(scc) > 1 or v in edges.get(v, []):
                out.append(sorted(scc))

    for v in sorted(edges):
        if v not in idx:
            visit(v)
    return out


def thread_lanes(active_lanes, ready):
    """Each active lane gets the top of its own ready queue; an empty lane borrows from unassigned or inactive
    lanes; no item goes to two lanes. `ready` must already be in score order."""
    active, taken, out = set(active_lanes), set(), []
    for lane in active_lanes:
        own = next((r for r in ready if r["lane"] == lane and r["id"] not in taken), None)
        if own:
            taken.add(own["id"])
        out.append({"lane": lane, "id": own["id"] if own else None, "borrowed": False})
    pool = [r for r in ready if r["lane"] is None or r["lane"] not in active]
    for a in out:
        if a["id"]:
            continue
        b = next((r for r in pool if r["id"] not in taken), None)
        if not b:
            continue
        taken.add(b["id"])
        a.update({"id": b["id"], "borrowed": True, "fromLane": b["lane"]})
    return out


def _most_common(values):
    counts = {}
    for v in values:
        counts[v] = counts.get(v, 0) + 1
    best = None
    for v, n in sorted(counts.items(), key=lambda kv: _locale(kv[0])):
        if best is None or n > counts[best]:
            best = v
    return best


def build_queue(stories, issues, prs, adrs, active_lanes=None, cfg=None):
    cfg = {**DEFAULTS, **(cfg or {})}
    lane_re = re.compile(cfg["lane_label"])
    prio_re = re.compile(cfg["priority_label"])
    sess_re = re.compile(cfg["session_label_re"]) if cfg.get("session_label_re") else None
    sp = cfg["status_prefix"]

    def tracked_status(labels):
        return next((l[len(sp):] for l in labels if l.startswith(sp)), None)

    def lane_of(labels):
        return next((m.group(1) for l in labels if (m := lane_re.match(l))), None)

    problems = []
    issue_map = {i["number"]: i for i in issues}
    pr_idx = index_prs(prs)
    story_ids = {s["id"] for s in stories}
    by_issue = {s["issue"]: s["id"] for s in stories if s["issue"] is not None}
    by_title_id = {}
    for i in issues:
        m = re.match(r"^(E\d+-S\d+)\b", i["title"])
        if m and m.group(1) not in by_title_id:
            by_title_id[m.group(1)] = i
    epic_issues = {}
    for i in issues:
        m = re.match(r"^EPIC\s+(\d+)\b", i["title"])
        if m and cfg["epic_issue_label"] in i["labels"]:
            epic_issues[int(m.group(1))] = i

    nodes = {}
    for s in stories:
        issue = issue_map.get(s["issue"]) if s["issue"] is not None else None
        if s["issue"] is not None and not issue:
            problems.append(_problem(s["line"], f"issue #{s['issue']} not on GitHub", s["id"]))
        if issue and not s["id"].startswith("#") and not issue["title"].startswith(s["id"]):
            problems.append(_problem(s["line"], f'issue #{s["issue"]} is titled "{issue["title"][:60]}"', s["id"]))
        work = issue_work(s["issue"], issue_map, pr_idx)
        labels = issue["labels"] if issue else []
        tracked = tracked_status(labels)
        claimed = [l for l in labels if sess_re and sess_re.match(l)]
        if tracked in DONE_STATUSES:
            work["status"] = "done"
        elif work["status"] != "done" and (tracked in ACTIVE_STATUSES or claimed):
            work["status"] = "in-progress"
        lane = lane_of(issue["labels"]) if issue else None
        plabel = next((m.group(1) for l in (issue["labels"] if issue else []) if (m := prio_re.match(l))), None)
        nodes[s["id"]] = {**s,
                          "priority": s["priority"] if s["priority"] is not None else (int(plabel) if plabel else None),
                          "lane": lane, "laneSource": "label" if lane else None, "status": work["status"],
                          "work": work, "assignees": issue.get("assignees", []) if issue else [],
                          "dependsOn": [], "blocks": [], "unmet": [], "ready": False, "weight": 0, "depth": 0}
        if claimed or tracked:
            nodes[s["id"]].update(claimedBy=claimed, tracked=tracked)
        if tracked == "blocked" and work["status"] == "todo":
            nodes[s["id"]]["unmet"].append({"dep": f"{sp}blocked", "why": "labelled blocked", "storyIds": []})

    # Containers: a story with tasks is never queued itself; it waits on its tasks and is done when they are.
    children = {}
    for n in nodes.values():
        if n.get("parent") is not None and n["parent"] in by_issue and by_issue[n["parent"]] in nodes:
            children.setdefault(by_issue[n["parent"]], []).append(n["id"])
    for pid, kids in children.items():
        p = nodes[pid]
        p["container"] = True
        if p["status"] != "done":
            states = [nodes[k]["status"] for k in kids]
            if all(x == "done" for x in states):
                p["status"] = "done"
            elif any(x != "todo" for x in states):
                p["status"] = "in-progress"

    # Lane inheritance: the most common lane of the epic's labelled stories, then the epic issue's own lane.
    epic_members = {}
    for n in nodes.values():
        if n["epic"] is not None:
            epic_members.setdefault(n["epic"], []).append(n)
    for n in nodes.values():
        if n["lane"] or n["epic"] is None:
            continue
        inherited = _most_common([m["lane"] for m in epic_members.get(n["epic"], []) if m["laneSource"] == "label"])
        if inherited:
            n.update(lane=inherited, laneSource="epic")
        else:
            el = lane_of(epic_issues[n["epic"]]["labels"]) if n["epic"] in epic_issues else None
            if el:
                n.update(lane=el, laneSource="epic-issue")

    # Dependencies.
    unparsed = {}

    def note_unparsed(dep, id_):
        unparsed.setdefault(dep, set()).add(id_)

    def status_word(id_):
        d = nodes[id_]
        if d["status"] == "in-progress":
            return f"in progress (#{', #'.join(str(x) for x in d['work']['openPrs'])})"
        return "ready" if d["ready"] else "not done"

    def external_done(issue):
        return issue_work(issue["number"], issue_map, pr_idx)["status"] == "done"

    pending = []
    for n in nodes.values():
        for dep in n["deps"]:
            if dep["kind"] == "story" or (dep["kind"] == "issue" and dep["issue"] in by_issue):
                target = dep["story"] if dep["kind"] == "story" else by_issue[dep["issue"]]
                if target == n["id"]:
                    problems.append(_problem(n["line"], "depends on itself", n["id"]))
                    continue
                if target in story_ids:
                    pending.append((n, dep, [target]))
                else:
                    ext = by_title_id.get(target)
                    if not ext:
                        problems.append(_problem(n["line"], f"dangling story id {target}", n["id"]))
                        note_unparsed(dep["raw"], n["id"])
                    elif not external_done(ext):
                        n["unmet"].append({"dep": dep["raw"], "why": f"#{ext['number']} (outside the backlog) is open",
                                           "storyIds": []})
            elif dep["kind"] == "epic":
                members = [m["id"] for m in epic_members.get(dep["epic"], []) if m["id"] != n["id"]]
                if members:
                    pending.append((n, dep, members))
                else:
                    ep = epic_issues.get(dep["epic"])
                    label = next((l for l in ep["labels"] if l.startswith(cfg["epic_label_prefix"])), None) if ep else None
                    gh_stories = [i for i in issues if label in i["labels"] and cfg["story_label"] in i["labels"]] \
                        if label else []
                    if not gh_stories:
                        note_unparsed(dep["raw"], n["id"])
                    else:
                        open_ = [i for i in gh_stories if not external_done(i)]
                        if open_:
                            n["unmet"].append({"dep": dep["raw"],
                                               "why": f"{len(open_)} of its {len(gh_stories)} stories open",
                                               "storyIds": []})
            elif dep["kind"] == "adr":
                st = adrs.get(dep["adr"], ...)
                if not is_accepted(None if st is ... else st):
                    label = f"ADR-{dep['adr']}"
                    why = "no such ADR" if st is ... else re.split(r"\s+—\s+|\s+\(", st or "no Status line")[0]
                    if not any(u["dep"] == label for u in n["unmet"]):
                        n["unmet"].append({"dep": label, "why": why, "storyIds": []})
            elif dep["kind"] == "issue":
                i = issue_map.get(dep["issue"])
                if not i:
                    note_unparsed(dep["raw"], n["id"])
                elif not external_done(i):
                    n["unmet"].append({"dep": dep["raw"], "why": f"#{i['number']} is open", "storyIds": []})
            else:
                note_unparsed(dep["raw"], n["id"])
    for pid, kids in children.items():
        pending.append((nodes[pid], {"raw": "tasks", "kind": "tasks"}, kids))
    for node, _dep, members in pending:
        for m in members:
            if m not in node["dependsOn"]:
                node["dependsOn"].append(m)
            if node["id"] not in nodes[m]["blocks"]:
                nodes[m]["blocks"].append(node["id"])

    # Readiness: first the external conditions, then the story edges.
    for n in nodes.values():
        n["ready"] = (n["status"] == "todo" and not n["unmet"] and not n.get("container")
                      and all(nodes[d]["status"] == "done" for d in n["dependsOn"]))
    for node, dep, members in pending:
        if node["status"] != "todo" or dep["kind"] == "tasks":
            continue
        open_ = [m for m in members if nodes[m]["status"] != "done"]
        if not open_:
            continue
        if dep["kind"] == "epic":
            why = f"{len(open_)} of its {len(members)} stories not done"
            label = f"EPIC {str(dep['epic']).zfill(2)}"
        else:
            why, label = status_word(open_[0]), open_[0]
        if not any(u["dep"] == label for u in node["unmet"]):
            node["unmet"].append({"dep": label, "why": why, "storyIds": open_})

    # Unblock weight: not-done stories reachable through not-done dependents.
    def not_done(id_):
        return nodes[id_]["status"] != "done"

    for n in nodes.values():
        if not not_done(n["id"]):
            continue
        seen, q = {n["id"]}, deque([n["id"]])
        while q:
            for b in nodes[q.popleft()]["blocks"]:
                if b not in seen and not_done(b):
                    seen.add(b)
                    q.append(b)
        n["weight"] = len(seen) - 1

    # Critical path: the longest not-done chain below; a back edge (cycle) contributes nothing.
    memo, on_path = {}, set()

    def depth_of(id_):
        if id_ in memo:
            return memo[id_]
        if id_ in on_path:
            return 0
        on_path.add(id_)
        best = 0
        for b in nodes[id_]["blocks"]:
            if not_done(b):
                best = max(best, depth_of(b))
        on_path.discard(id_)
        memo[id_] = best + 1
        return best + 1

    for n in nodes.values():
        n["depth"] = depth_of(n["id"]) if not_done(n["id"]) else 0

    cycles = find_cycles({n["id"]: n["dependsOn"] for n in nodes.values()})

    # Lanes and queues.
    lanes = sorted({n["lane"] for n in nodes.values() if n["lane"]})
    active = [l.zfill(2) for l in (active_lanes or lanes)]
    ready = sorted((n for n in nodes.values() if n["ready"]), key=score_key)
    queues = {l: [n["id"] for n in ready if n["lane"] == l] for l in active}
    assignments = thread_lanes(active, [{"id": n["id"], "lane": n["lane"]} for n in ready])
    active_set = set(active)
    paused = []
    for l in lanes:
        if l in active_set:
            continue
        cands = sorted((n for n in nodes.values() if n["lane"] == l and n["status"] != "done" and n["weight"] > 0),
                       key=score_key)
        if cands:
            n = cands[0]
            paused.append({"lane": l, "id": n["id"], "weight": n["weight"], "ready": n["ready"]})

    top_blockers = [n["id"] for n in sorted((n for n in nodes.values() if n["status"] != "done" and n["weight"] > 0),
                                            key=score_key)[:15]]

    groups = {}
    for n in sorted((x for x in nodes.values() if x["status"] == "todo" and not x["ready"] and not x.get("container")),
                    key=score_key):
        for u in n["unmet"]:
            g = groups.setdefault(f"{u['dep']} — {u['why']}", {"dep": u["dep"], "why": u["why"], "ids": []})
            g["ids"].append(n["id"])
    blocked_by = sorted(groups.values(), key=lambda g: (-len(g["ids"]), _locale(g["dep"])))

    merged_but_open = []
    for num, e in sorted(pr_idx.items()):
        i = issue_map.get(num)
        if i and i["state"] == "OPEN" and e["merged"] and not e["open"] and cfg["epic_issue_label"] not in i["labels"] \
                and tracked_status(i["labels"]) not in DONE_STATUSES:
            merged_but_open.append({"issue": num, "title": i["title"], "prs": [p["number"] for p in e["merged"]],
                                    "story": by_issue.get(num)})

    return {
        "nodes": nodes, "lanes": lanes, "activeLanes": active, "queues": queues, "assignments": assignments,
        "pausedBlockers": paused, "topBlockers": top_blockers, "blockedBy": blocked_by,
        "mergedButOpen": merged_but_open,
        "unparsed": sorted(({"dep": d, "ids": sorted(ids)} for d, ids in unparsed.items()),
                           key=lambda u: _locale(u["dep"])),
        "cycles": cycles, "problems": problems,
    }


# ─── Rendering ───────────────────────────────────────────────────────────────────────────────────

def _item_line(n, model=None):
    extra = [x for x in [
        f"partly merged #{', #'.join(map(str, n['work']['partialPrs']))}" if n["work"]["partialPrs"] else "",
        f"@ {', '.join(n['assignees'])}" if n["assignees"] else "",
        f"🔒 {', '.join(n['claimedBy'])}" if n.get("claimedBy") else "",
        f"{n['tracked']}" if n.get("tracked") and n["tracked"] not in ("todo",) and not n.get("claimedBy") else "",
    ] if x]
    issue = f" #{n['issue']}" if n["issue"] and not n["id"].startswith("#") else ""
    p = "p?" if n["priority"] is None else f"p{n['priority']}"
    tail = f" · {' · '.join(extra)}" if extra else ""
    return (f"**{n['id']}**{issue} {n['title']} · {n['size'] or '?'} · {p} · "
            f"unblocks {n['weight']}, chain {n['depth']}{tail}")


def render_report(r, queue_length=10, blocker_count=15):
    all_ = list(r["nodes"].values())

    def count(f):
        return sum(1 for n in all_ if f(n))

    out = ["# Work queue", ""]
    out.append(f"- Stories: {len(all_)} · done {count(lambda n: n['status'] == 'done')} · in progress "
               f"{count(lambda n: n['status'] == 'in-progress')} · ready {count(lambda n: n['ready'])} · blocked "
               f"{count(lambda n: n['status'] == 'todo' and not n['ready'])}")
    out.append(f"- Lanes: {', '.join(r['lanes']) or 'none'} · active {', '.join(r['activeLanes']) or 'none'} · "
               f"unassigned stories {count(lambda n: not n['lane'] and n['status'] != 'done')}")
    out.append(f"- Merged but open: {len(r['mergedButOpen'])} · unparsed dependencies: {len(r['unparsed'])} · "
               f"cycles: {len(r['cycles'])} · parse problems: {len(r['problems'])}")

    out += ["", "## Next per lane", ""]
    for a in r["assignments"]:
        n = r["nodes"][a["id"]] if a["id"] else None
        frm = ""
        if a["borrowed"]:
            frm = f" _(borrowed from {'lane ' + a['fromLane'] if a.get('fromLane') else 'unassigned'})_"
        busy = count(lambda x: x["lane"] == a["lane"] and x["status"] == "in-progress")
        out.append(f"- **lane {a['lane']}** ({busy} in progress): {_item_line(n) + frm if n else '_nothing ready_'}")
    for b in r["pausedBlockers"]:
        out.append(f"- ⚠ lane {b['lane']} is paused but owns {b['id']}, which blocks {b['weight']}"
                   f"{' (ready)' if b['ready'] else ''}")

    out += ["", f"## Lane queues (top {queue_length})"]
    for lane, ids in r["queues"].items():
        out += ["", f"### Lane {lane} — {len(ids)} ready", ""]
        if not ids:
            out.append("_nothing ready_")
        for i, id_ in enumerate(ids[:queue_length]):
            out.append(f"{i + 1}. {_item_line(r['nodes'][id_])}")

    out += ["", f"## Top {blocker_count} blockers", ""]
    for i, id_ in enumerate(r["topBlockers"][:blocker_count]):
        n = r["nodes"][id_]
        state = ("ready" if n["ready"] else "in progress" if n["status"] == "in-progress"
                 else f"blocked on {', '.join(u['dep'] for u in n['unmet'])}")
        out.append(f"{i + 1}. {_item_line(n)} · lane {n['lane'] or '—'} · {state}")

    out += ["", "## Blocked, by what blocks them", ""]
    if not r["blockedBy"]:
        out.append("_none_")
    for g in r["blockedBy"]:
        out.append(f"- **{g['dep']}** ({g['why']}) → {', '.join(g['ids'])}")

    out += ["", "## Merged but issue still open", ""]
    if not r["mergedButOpen"]:
        out.append("_none_")
    for m in r["mergedButOpen"]:
        story = f" {m['story']}" if m["story"] and m["story"] != f"#{m['issue']}" else ""
        out.append(f"- #{m['issue']}{story}: {m['title']} — merged in #{', #'.join(map(str, m['prs']))}")

    out += ["", "## Unparsed dependencies (treated as satisfied — check by hand)", ""]
    if not r["unparsed"]:
        out.append("_none_")
    for u in r["unparsed"]:
        out.append(f'- "{u["dep"]}" ← {", ".join(u["ids"])}')

    out += ["", "## Cycles", ""]
    if not r["cycles"]:
        out.append("_none_")
    for c in r["cycles"]:
        out.append(f"- {' ⇄ '.join(c)}")

    if r["problems"]:
        out += ["", "## Backlog parse problems", ""]
        for p in r["problems"]:
            out.append(f"- {'line ' + str(p['line']) if p.get('line') else ''}"
                       f"{' ' + p['id'] if p.get('id') else ''}: {p['what']}")
    return "\n".join(out) + "\n"


def to_json(r):
    return {k: (list(v.values()) if k == "nodes" else v) for k, v in r.items()}
