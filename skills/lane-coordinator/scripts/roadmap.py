#!/usr/bin/env python3
"""The roadmap: the plan that milestones, epics, tickets, ADRs, the DAG and releases all hang from.

Usage: python3 -I roadmap.py [--repo PATH] <command> ...

  init                         copy the roadmap template to roadmap.path (default docs/roadmap.md) if missing
  check                        parse the roadmap and report format problems (no GitHub)
  sync [--apply]               make GitHub match the roadmap: milestones (due date, goal), epic issues (milestone,
                               lane, description). Dry run by default. Never deletes; reports GitHub milestones and
                               epics the roadmap doesn't mention, and closes milestones whose tickets have all shipped
  status [--json OUT] [--data SNAP] [--lanes ..]
                               the one-page view: per milestone, progress, epics, the decisions (ADRs) blocking
                               it, what is in staging and production, the critical chain, and a forecast from the
                               ledger's throughput, flagged when it lands after the due date

Roadmap file: "## <milestone>" headings in delivery order with Due:/Goal:/Exit: lines; under each, "### <key> — <Title>"
epics with optional Lane:/Depends on: lines and a description. The order of milestones ranks work in the planner.
"""
import argparse, json, os, re, statistics, sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dag
import tracker as tr
from common import load_json, load_ledger, repo_root, state_dir

TEMPLATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "templates", "roadmap.md")
SHIPPED = {"deployed", "done"}


class Fail(Exception):
    pass


# ─── the file ────────────────────────────────────────────────────────────────────────────────────

def parse(md):
    """→ (milestones, problems). Each milestone: title, due, goal, exit, epics[{key, title, lane, depends, body}]."""
    md = re.sub(r"<!--.*?-->", lambda m: "\n" * m.group(0).count("\n"), md, flags=re.S)
    milestones, problems, cur, epic, keys = [], [], None, None, {}
    for i, line in enumerate(md.split("\n"), 1):
        if line.startswith("## "):
            cur = {"title": line[3:].strip(), "due": None, "goal": "", "exit": "", "epics": [], "line": i}
            milestones.append(cur)
            epic = None
            continue
        if line.startswith("### "):
            m = re.match(r"^###\s+([A-Za-z0-9][\w.-]*)\s+[—–-]\s+(.+)$", line)
            if not cur:
                problems.append({"line": i, "what": "epic before any milestone"})
            elif not m:
                problems.append({"line": i, "what": f'epic heading must be "### key — Title": {line[:60]}'})
            else:
                epic = {"key": m.group(1), "title": m.group(2).strip(), "lane": None, "depends": None, "body": [],
                        "line": i}
                if epic["key"] in keys:
                    problems.append({"line": i, "what": f"epic key {epic['key']} also at line {keys[epic['key']]}"})
                keys[epic["key"]] = i
                cur["epics"].append(epic)
            continue
        kv = re.match(r"^(Due|Goal|Exit|Lane|Depends on):\s*(.*)$", line, re.I)
        if kv and (cur or epic):
            k, v = kv.group(1).lower(), kv.group(2).strip()
            if epic is not None and k in ("lane", "depends on"):
                epic["lane" if k == "lane" else "depends"] = v.zfill(2) if k == "lane" else v
            elif epic is None and k in ("due", "goal", "exit"):
                if k == "due" and v and not re.match(r"^\d{4}-\d{2}-\d{2}$", v):
                    problems.append({"line": i, "what": f"Due must be YYYY-MM-DD, got {v}"})
                cur[k] = v or None if k == "due" else v
            else:
                (epic["body"] if epic is not None else []).append(line)
            continue
        if epic is not None:
            epic["body"].append(line)
    titles = [m["title"] for m in milestones]
    for t in {t for t in titles if titles.count(t) > 1}:
        problems.append({"line": None, "what": f'milestone "{t}" appears twice'})
    for m in milestones:
        for e in m["epics"]:
            e["body"] = "\n".join(e["body"]).strip()
    return milestones, problems


def ms_description(m):
    return "\n\n".join(x for x in [m["goal"], f"Exit criteria: {m['exit']}" if m["exit"] else ""] if x)


def epic_body(e):
    return "\n\n".join(x for x in [e["body"], f"Depends on: {e['depends']}" if e["depends"] else ""] if x)


# ─── sync ────────────────────────────────────────────────────────────────────────────────────────

def _milestone_title(t, issue):
    return dag.milestone_of(issue)[0]


def plan_sync(gh, t, rm):
    """The changes that would make GitHub match the roadmap, plus drift it won't touch."""
    actions, drift = [], []
    label_mode = t["milestone_mode"] == "label"
    gh_ms = {m["title"]: m for m in ([] if label_mode else gh.milestones_full())}
    issues = gh.issues(state="all")
    for m in rm:
        if label_mode:
            actions.append({"do": "ensure_label", "label": f"milestone:{m['title']}"})
        elif m["title"] not in gh_ms:
            actions.append({"do": "create_milestone", "title": m["title"], "due": m["due"], "description": ms_description(m)})
        else:
            g, fields = gh_ms[m["title"]], {}
            if (m["due"] or None) != g["due_on"]:
                fields["due_on"] = m["due"]
            if ms_description(m) != (g["description"] or ""):
                fields["description"] = ms_description(m)
            if fields:
                actions.append({"do": "update_milestone", "title": m["title"], "number": g["number"], **fields})

    epics = {}
    for i in issues:
        if "type:epic" in i["labels"]:
            for l in i["labels"]:
                if l.startswith("epic:"):
                    epics.setdefault(l[5:], i)
    planned = set()
    for m in rm:
        for e in m["epics"]:
            planned.add(e["key"])
            i = epics.get(e["key"])
            if not i:
                actions.append({"do": "create_epic", "key": e["key"], "title": e["title"], "milestone": m["title"],
                                "lane": e["lane"], "body": epic_body(e)})
                continue
            if _milestone_title(t, i) != m["title"]:
                actions.append({"do": "move_epic", "issue": i["number"], "key": e["key"], "from": _milestone_title(t, i),
                                "to": m["title"]})
            lanes = [l for l in i["labels"] if l.startswith("lane:")]
            if e["lane"] and lanes != [f"lane:{e['lane']}"]:
                actions.append({"do": "set_lane", "issue": i["number"], "key": e["key"], "lane": e["lane"],
                                "remove": lanes})
            if i["title"] != e["title"]:
                drift.append(f"epic {e['key']} (#{i['number']}) is titled \"{i['title']}\" on GitHub, "
                             f"\"{e['title']}\" in the roadmap")
    for k, i in sorted(epics.items()):
        if k not in planned and i["state"] == "open":
            drift.append(f"epic {k} (#{i['number']}) is open on GitHub but not on the roadmap")
    titles = {m["title"] for m in rm}
    for title, g in sorted(gh_ms.items()):
        if title not in titles and g["state"] == "open":
            drift.append(f'milestone "{title}" is open on GitHub but not on the roadmap')

    # Completed milestones: every ticket shipped (or closed); close them on GitHub.
    for m in rm:
        tickets = [i for i in issues if "type:epic" not in i["labels"] and _milestone_title(t, i) == m["title"]]
        if tickets and all(i["state"] == "closed" or tr.status_of(t, i) in SHIPPED | {"cancelled"} for i in tickets) \
                and not label_mode and m["title"] in gh_ms and gh_ms[m["title"]]["state"] == "open":
            actions.append({"do": "close_milestone", "title": m["title"], "number": gh_ms[m["title"]]["number"],
                            "tickets": len(tickets)})
    return {"actions": actions, "drift": drift}


def apply_sync(gh, t, plan):
    done = []
    for a in plan["actions"]:  # milestones first: epics are created into them
        if a["do"] == "create_milestone":
            gh.create_milestone(a["title"], a["due"], a["description"])
        elif a["do"] == "update_milestone":
            gh.update_milestone(a["number"], **{k: a[k] for k in ("due_on", "description") if k in a})
        elif a["do"] == "ensure_label":
            tr.ensure_labels(gh, [a["label"]])
        else:
            continue
        done.append(a)
    for a in plan["actions"]:
        if a["do"] == "create_epic":
            r = tr.create(gh, t, "epic", a["title"], key=a["key"], milestone=a["milestone"], body=a["body"])
            if a["lane"]:
                tr.ensure_labels(gh, [f"lane:{a['lane']}"])
                gh.add_labels(r["issue"], [f"lane:{a['lane']}"])
            a = {**a, "issue": r["issue"]}
        elif a["do"] == "move_epic":
            if t["milestone_mode"] == "label":
                for l in gh.issue(a["issue"])["labels"]:
                    if l.startswith("milestone:"):
                        gh.remove_label(a["issue"], l)
                tr.ensure_labels(gh, [f"milestone:{a['to']}"])
                gh.add_labels(a["issue"], [f"milestone:{a['to']}"])
            else:
                gh.set_issue_milestone(a["issue"], gh.milestones()[a["to"]])
        elif a["do"] == "set_lane":
            for l in a["remove"]:
                gh.remove_label(a["issue"], l)
            tr.ensure_labels(gh, [f"lane:{a['lane']}"])
            gh.add_labels(a["issue"], [f"lane:{a['lane']}"])
        elif a["do"] == "close_milestone":
            gh.update_milestone(a["number"], state="closed")
        else:
            continue
        done.append(a)
    return done


# ─── status ──────────────────────────────────────────────────────────────────────────────────────

def throughput(ledger, today, days=28):
    """Tickets merged per week over the window, from the ledger's ticket rows."""
    since = (today - timedelta(days=days)).isoformat()
    n = sum(1 for r in ledger if r.get("kind") == "ticket" and r.get("date", "") >= since)
    return n / (days / 7)


def status(rm, r, data, adrs, prod_versions, ledger, today=None, lanes=None):
    """Everything the coordinator and owner need, per milestone, in roadmap order."""
    import adr as adr_mod
    today = today or date.today()
    issues = {i["number"]: i for i in data["issues"]}
    nodes = [n for n in r["nodes"].values() if not n.get("container")]
    waiting = adr_mod.waiting(r)
    proposed = {a["num"]: a for a in adrs if a["status"] == "proposed"}
    per_week = throughput(ledger, today)
    n_lanes = max(1, len(lanes or r["activeLanes"] or [1]))
    days_per_ticket_per_lane = (7 * n_lanes / per_week) if per_week else None

    def release_of(n):
        i = issues.get(n["issue"]) or {}
        rel = next((l[len("release:"):] for l in i.get("labels", []) if l.startswith("release:")), None)
        return rel

    out, cumulative = [], 0
    for m in rm:
        mine = [n for n in nodes if dag.milestone_matches(n.get("milestone"), m["title"])]
        ids = {n["id"] for n in mine}
        nd = [n for n in mine if n["status"] != "done"]
        cumulative += len(nd)
        prod_v = {v for v in prod_versions}
        in_prod = [n for n in mine if release_of(n) and release_of(n) in prod_v]
        on_staging = [n for n in mine if release_of(n) and release_of(n) not in prod_v and n["status"] == "done"]
        epics = []
        for e in m["epics"]:
            en = [n for n in mine if n.get("epic") == e["key"] or str(n.get("epic")) == e["key"]]
            epics.append({"key": e["key"], "title": e["title"], "lane": e["lane"], "total": len(en),
                          "done": sum(n["status"] == "done" for n in en),
                          "in_progress": sum(n["status"] == "in-progress" for n in en),
                          "ready": sum(n["ready"] for n in en),
                          "blocked": sum(n["status"] == "todo" and not n["ready"] for n in en)})
        decisions = []
        for num, w in waiting.items():
            hit = ids & set(w["all"])
            if hit and num in proposed:
                decisions.append({"adr": f"ADR-{num}", "title": proposed[num]["title"], "blocks": len(hit)})
        decisions.sort(key=lambda d: -d["blocks"])
        chain = max((n["depth"] for n in nd), default=0)
        forecast = None
        if not nd:
            state = "complete" if mine else "empty"
        elif days_per_ticket_per_lane:
            days = max(cumulative / per_week * 7, chain * days_per_ticket_per_lane)
            forecast = (today + timedelta(days=round(days))).isoformat()
            state = "at risk" if m["due"] and forecast > m["due"] else "on track"
        else:
            state = "no forecast (no ledger throughput yet)"
        top = [n["id"] for n in sorted((n for n in nd if n["weight"] > 0), key=dag.score_key)[:3]]
        out.append({"milestone": m["title"], "due": m["due"], "goal": m["goal"], "exit": m["exit"],
                    "total": len(mine), "done": len(mine) - len(nd),
                    "in_progress": sum(n["status"] == "in-progress" for n in mine),
                    "ready": sum(n["ready"] for n in mine),
                    "blocked": sum(n["status"] == "todo" and not n["ready"] for n in mine),
                    "chain": chain, "in_production": len(in_prod), "on_staging": len(on_staging),
                    "releases": sorted({release_of(n) for n in mine if release_of(n)}),
                    "epics": epics, "decisions": decisions, "top_blockers": top,
                    "forecast": forecast, "state": state})
    titles = [m["title"] for m in rm]
    planned_epics = {e["key"] for m in rm for e in m["epics"]}
    unplanned = sorted(n["id"] for n in nodes if n["status"] != "done"
                       and not any(dag.milestone_matches(n.get("milestone"), t) for t in titles))
    off_roadmap = sorted({str(n["epic"]) for n in nodes if n.get("epic") is not None and str(n["epic"]) not in planned_epics})
    return {"today": today.isoformat(), "throughput_per_week": round(per_week, 1), "lanes": n_lanes,
            "milestones": out, "unplanned": unplanned, "epics_off_roadmap": off_roadmap}


def render_status(s):
    icon = {"complete": "✅", "on track": "🟢", "at risk": "🔴", "empty": "⚪"}
    out = ["# Roadmap status", "",
           f"_{s['today']} · throughput {s['throughput_per_week']} tickets/week over the last 4 weeks · "
           f"{s['lanes']} lanes_", "",
           "| Milestone | Due | Done | In progress | Ready | Blocked | Chain | Shipped | Forecast | |",
           "|---|---|---:|---:|---:|---:|---:|---|---|---|"]
    for m in s["milestones"]:
        pct = f"{m['done']}/{m['total']}" + (f" ({round(100 * m['done'] / m['total'])}%)" if m["total"] else "")
        shipped = " · ".join(x for x in [f"{m['in_production']} prod" if m["in_production"] else "",
                                          f"{m['on_staging']} staging" if m["on_staging"] else ""] if x) or "—"
        out.append(f"| {m['milestone']} | {m['due'] or '—'} | {pct} | {m['in_progress']} | {m['ready']} | "
                   f"{m['blocked']} | {m['chain']} | {shipped} | {m['forecast'] or '—'} | "
                   f"{icon.get(m['state'], '❔')} {m['state']} |")
    for m in s["milestones"]:
        out += ["", f"## {m['milestone']}" + (f" · due {m['due']}" if m["due"] else ""), ""]
        if m["goal"]:
            out.append(f"**Goal:** {m['goal']}")
        if m["exit"]:
            out.append(f"**Exit:** {m['exit']}")
        if m["releases"]:
            out.append(f"**Releases:** {', '.join(m['releases'])}")
        out += ["", "| Epic | Lane | Done | In progress | Ready | Blocked |", "|---|---|---:|---:|---:|---:|"]
        for e in m["epics"]:
            out.append(f"| {e['key']} — {e['title']} | {e['lane'] or '—'} | {e['done']}/{e['total']} | "
                       f"{e['in_progress']} | {e['ready']} | {e['blocked']} |")
        if m["decisions"]:
            out += ["", "**Decisions holding it up:** " + "; ".join(
                f"{d['adr']} {d['title']} (blocks {d['blocks']})" for d in m["decisions"])]
        if m["top_blockers"]:
            out.append(f"**Biggest blockers:** {', '.join(m['top_blockers'])}")
    if s["unplanned"]:
        out += ["", f"## Not on any roadmap milestone ({len(s['unplanned'])} open)", "", ", ".join(s["unplanned"][:40])]
    if s["epics_off_roadmap"]:
        out += ["", "## Epics with tickets but not on the roadmap", "", ", ".join(s["epics_off_roadmap"])]
    return "\n".join(out) + "\n"


# ─── CLI ─────────────────────────────────────────────────────────────────────────────────────────

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--repo")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    sub.add_parser("check")
    s = sub.add_parser("sync"); s.add_argument("--apply", action="store_true")
    s = sub.add_parser("status"); s.add_argument("--json"); s.add_argument("--data"); s.add_argument("--lanes")
    a = ap.parse_args(argv)

    root = repo_root(a.repo)
    sd = state_dir(a.repo)
    cfg = load_json(os.path.join(sd, "config.json"), {}) or {}
    path = os.path.join(root, (cfg.get("roadmap") or {}).get("path") or "docs/roadmap.md")
    if a.cmd == "init":
        if os.path.exists(path):
            print(json.dumps({"roadmap": path, "created": False}))
            return
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(TEMPLATE, encoding="utf-8") as src, open(path, "w", encoding="utf-8") as dst:
            dst.write(src.read())
        print(json.dumps({"roadmap": path, "created": True}))
        return
    if not os.path.exists(path):
        raise Fail(f"no roadmap at {path}; run: roadmap.py init")
    with open(path, encoding="utf-8") as fh:
        rm, problems = parse(fh.read())
    if a.cmd == "check":
        print(json.dumps({"milestones": [{"title": m["title"], "due": m["due"], "epics": [e["key"] for e in m["epics"]]}
                                         for m in rm], "problems": problems}, indent=2, ensure_ascii=False))
        if problems:
            sys.exit(1)
        return
    if problems:
        raise Fail("fix the roadmap first: " + "; ".join(f"line {p['line']}: {p['what']}" for p in problems))
    t = tr.tracker_cfg(cfg, root)
    from ghapi import GitHub, detect_repo
    gh = GitHub(t["repo"] or detect_repo(root), cwd=root)
    if a.cmd == "sync":
        p = plan_sync(gh, t, rm)
        if a.apply:
            p["applied"] = apply_sync(gh, t, p)
        print(json.dumps(p, indent=2, ensure_ascii=False))
    elif a.cmd == "status":
        import adr as adr_mod, release as rel, work_queue
        lanes = [l.strip() for l in (a.lanes or "").split(",") if l.strip()] or None
        r, data = work_queue.compute(a.repo, lanes, data_path=a.data)
        adrs = adr_mod.scan(adr_mod.adr_dir(cfg, root)[0])
        rc = {**rel.DEFAULT_RELEASE, **(cfg.get("release") or {})}
        prod_v = [rel.vstr(v, rc["tag_prefix"]) for v in rel.parse_tags(gh.tags(), rc["tag_prefix"])[0]]
        s = status(rm, r, data, adrs, prod_v, load_ledger(sd), lanes=lanes)
        sys.stdout.write(render_status(s))
        if a.json:
            with open(a.json, "w") as fh:
                json.dump(s, fh, indent=2, ensure_ascii=False)


if __name__ == "__main__":
    try:
        main()
    except (Fail, tr.Fail) as e:
        sys.exit(f"error: {e}")
