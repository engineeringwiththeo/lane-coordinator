#!/usr/bin/env python3
"""Architecture Decision Records for the coordinator: write them, decide them, and keep work tied to them.

Usage: python3 -I adr.py [--repo PATH] [--session NN] <command> ...

  init                                   create the ADR folder and its 0000 template (idempotent)
  new TITLE [--issue N] [--supersedes NNNN] [--relates "ADR-0003, #12"] [--owner WHO]
                                         write NNNN-slug.md as Proposed (anyone may draft one)
  status NNNN accepted|rejected|deprecated|proposed [--note TEXT]
  status NNNN superseded --by MMMM       change an ADR's status: coordinator only (also marks MMMM "Supersedes")
  list [--status S] [--json]             every ADR with its status and the tickets that name it
  pending [--lanes ..] [--data SNAP]     Proposed ADRs ranked by the work they block (from the DAG): what to get
                                         decided next
  for-ticket N                           the ADRs a ticket, its parent story and its epic name: paste into the brief
  check --pr N [--coordinator]           review gate: ADRs the PR and its tickets rely on must exist and be
                                         Accepted, and only the coordinator changes an ADR's Status; exit 1 on problems

ADR files: <adr.dir>/NNNN-slug.md, first line "# ADR-NNNN: Title", a "- **Status:** …" line. A ticket that needs a
decision says "Depends on: ADR-NNNN" and stays blocked in the DAG until that ADR's status starts with "Accepted".
"""
import argparse, json, os, re, sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dag
import tracker as tr
from common import load_json, repo_root, state_dir

ADR_FILE = re.compile(r"^(\d{4})-(.+)\.md$")
STATUS_LINE = re.compile(r"^-\s*\*\*Status:\*\*.*$", re.M)
REF = re.compile(r"\bADR-(\d{4})\b")
TEMPLATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "templates", "adr-template.md")
STATES = ("accepted", "proposed", "rejected", "deprecated", "superseded")


class Fail(Exception):
    pass


def adr_dir(cfg, root):
    rel = (cfg.get("adr") or {}).get("dir") or (cfg.get("queue") or {}).get("adr_dir") or "docs/adr"
    return os.path.join(root, rel), rel


def classify(raw):
    s = (raw or "").strip().lower()
    return next((k for k in STATES if s.startswith(k)), "unknown")


def scan(d):
    out = []
    if not os.path.isdir(d):
        return out
    for f in sorted(os.listdir(d)):
        m = ADR_FILE.match(f)
        if not m or m.group(1) == "0000":
            continue
        with open(os.path.join(d, f), encoding="utf-8") as fh:
            src = fh.read()
        h = re.match(r"^#\s*ADR-\d{4}:?\s*(.*)$", src.split("\n", 1)[0])
        raw = dag.adr_status_from_file(src)
        out.append({"num": m.group(1), "slug": m.group(2), "file": f, "path": os.path.join(d, f),
                    "title": (h.group(1).strip() if h else m.group(2).replace("-", " ")),
                    "status": classify(raw), "raw": raw})
    return out


def find(adrs, num):
    num = str(num).zfill(4)
    a = next((x for x in adrs if x["num"] == num), None)
    if not a:
        raise Fail(f"no ADR-{num}")
    return a


def slugify(title):
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60] or "decision"


def init(d):
    os.makedirs(d, exist_ok=True)
    tpl = os.path.join(d, "0000-template.md")
    if os.path.exists(tpl):
        return {"dir": d, "template": tpl, "created": False}
    with open(TEMPLATE, encoding="utf-8") as src, open(tpl, "w", encoding="utf-8") as dst:
        dst.write(src.read())
    return {"dir": d, "template": tpl, "created": True}


def new(d, title, issue=None, supersedes=None, relates=None, owner=None, today=None):
    init(d)
    today = today or date.today().isoformat()
    adrs = scan(d)
    num = f"{max([int(a['num']) for a in adrs] + [0]) + 1:04d}"
    with open(os.path.join(d, "0000-template.md"), encoding="utf-8") as fh:
        tpl = fh.read()
    lines = tpl.split("\n")
    lines[0] = f"# ADR-{num}: {title}"
    meta = [f"- **Status:** Proposed — {today}"]
    if issue:
        meta.append(f"- **Decision issue:** #{issue}")
    if owner:
        meta.append(f"- **Decision owner:** {owner}")
    if supersedes:
        meta.append(f"- **Supersedes:** ADR-{str(supersedes).zfill(4)}")
    if relates:
        meta.append(f"- **Relates to:** {relates}")
    body = "\n".join(lines)
    body = STATUS_LINE.sub(lambda _m: "\n".join(meta), body, count=1) if STATUS_LINE.search(body) else \
        body.replace(lines[0], lines[0] + "\n\n" + "\n".join(meta), 1)
    path = os.path.join(d, f"{num}-{slugify(title)}.md")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)
    return {"adr": f"ADR-{num}", "path": path}


def _rewrite_status(path, line):
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    if STATUS_LINE.search(src):
        src = STATUS_LINE.sub(lambda _m: line, src, count=1)
    else:
        first, _, rest = src.partition("\n")
        src = f"{first}\n\n{line}\n{rest}"
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(src)


def set_status(d, num, state, by=None, note=None, today=None):
    if state not in STATES:
        raise Fail(f"status must be one of {', '.join(STATES)}")
    today = today or date.today().isoformat()
    adrs = scan(d)
    a = find(adrs, num)
    if state == "superseded":
        if not by:
            raise Fail("superseded needs --by MMMM (the ADR that replaces it)")
        b = find(adrs, by)
        line = f"- **Status:** Superseded by ADR-{b['num']} — {today}"
        with open(b["path"], encoding="utf-8") as fh:
            src = fh.read()
        if f"Supersedes:** ADR-{a['num']}" not in src:
            src = STATUS_LINE.sub(lambda m: m.group(0) + f"\n- **Supersedes:** ADR-{a['num']}", src, count=1)
            with open(b["path"], "w", encoding="utf-8") as fh:
                fh.write(src)
    else:
        line = f"- **Status:** {state.capitalize()} — {today}" + (f". {note}" if note else "")
    _rewrite_status(a["path"], line)
    return {"adr": f"ADR-{a['num']}", "from": a["status"], "to": state, "line": line}


# ─── links to work ───────────────────────────────────────────────────────────────────────────────

def refs(text):
    return list(dict.fromkeys(REF.findall(text or "")))


def waiting(r):
    """ADR number → the not-done tickets that wait on it directly, and everything downstream of those."""
    out = {}
    for n in r["nodes"].values():
        for u in n["unmet"]:
            m = re.match(r"^ADR-(\d{4})$", u["dep"])
            if m:
                out.setdefault(m.group(1), set()).add(n["id"])
    res = {}
    for num, direct in out.items():
        seen, stack = set(direct), list(direct)
        while stack:
            for b in r["nodes"][stack.pop()]["blocks"]:
                if b not in seen and r["nodes"][b]["status"] != "done":
                    seen.add(b)
                    stack.append(b)
        res[num] = {"direct": sorted(direct), "total": len(seen), "all": sorted(seen)}
    return res


def pending(adrs, r):
    w = waiting(r)
    rows = [{"adr": f"ADR-{a['num']}", "title": a["title"], "raw": a["raw"],
             "blocks_direct": len(w.get(a["num"], {}).get("direct", [])), "blocks_total": w.get(a["num"], {}).get("total", 0),
             "tickets": w.get(a["num"], {}).get("direct", [])}
            for a in adrs if a["status"] == "proposed"]
    return sorted(rows, key=lambda x: (-x["blocks_total"], -x["blocks_direct"], x["adr"]))


def for_ticket(gh, adrs, n):
    i = gh.issue(n)
    texts, chain = [i["body"]], [f"#{n}"]
    pm = re.search(r"^\s*parent:\s*#(\d+)", i["body"], re.I | re.M)
    if pm:
        p = gh.issue(int(pm.group(1)))
        texts.append(p["body"])
        chain.append(f"#{p['number']}")
        pm2 = re.search(r"^\s*parent:\s*#(\d+)", p["body"], re.I | re.M)
        if pm2:
            e = gh.issue(int(pm2.group(1)))
            texts.append(e["body"])
            chain.append(f"#{e['number']}")
    by = {a["num"]: a for a in adrs}
    out = []
    for num in refs("\n".join(texts)):
        a = by.get(num)
        out.append({"adr": f"ADR-{num}", "title": a["title"] if a else None, "status": a["status"] if a else "missing",
                    "file": a["file"] if a else None})
    return {"ticket": n, "read_from": chain, "adrs": out,
            "warnings": [f"{x['adr']} is {x['status']}" for x in out if x["status"] != "accepted"]}


def check_pr(gh, adrs, n, rel_dir, coordinator=False):
    pr = gh.pr(n)
    files = gh.pr_files(n)
    texts = [pr["body"]] + [gh.issue(k)["body"] for k in dag.referenced_issues(pr["body"])]
    by = {a["num"]: a for a in adrs}
    problems, notes = [], []
    prefix = rel_dir.rstrip("/") + "/"
    for f in files:
        if not f["filename"].startswith(prefix):
            continue
        m = ADR_FILE.match(os.path.basename(f["filename"]))
        if not m:
            continue
        changed = [l for l in f["patch"].split("\n") if l[:1] in "+-" and "**Status:**" in l and l[:3] not in ("+++", "---")]
        added = all(l.startswith("+") for l in f["patch"].split("\n") if l[:1] in "+-" and l[:3] not in ("+++", "---"))
        if added and any("Proposed" in l for l in changed):
            notes.append(f"proposes ADR-{m.group(1)}")
        elif changed:
            (notes if coordinator else problems).append(
                f"changes the Status of ADR-{m.group(1)}" + (" (coordinator's decision PR)" if coordinator
                                                             else ": only the coordinator decides ADRs"))
    for num in refs("\n".join(texts)):
        a = by.get(num)
        if not a:
            problems.append(f"relies on ADR-{num}, which doesn't exist")
        elif a["status"] != "accepted":
            problems.append(f"relies on ADR-{num} ({a['title']}), which is {a['status']}, not accepted")
        else:
            notes.append(f"follows ADR-{num} ({a['title']}): check the change against its Decision section")
    return {"pr": n, "ok": not problems, "problems": problems, "notes": notes}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--repo")
    ap.add_argument("--session")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    s = sub.add_parser("new"); s.add_argument("title"); s.add_argument("--issue", type=int)
    s.add_argument("--supersedes"); s.add_argument("--relates"); s.add_argument("--owner")
    s = sub.add_parser("status"); s.add_argument("num"); s.add_argument("state", choices=STATES)
    s.add_argument("--by"); s.add_argument("--note"); s.add_argument("--force", action="store_true")
    s = sub.add_parser("list"); s.add_argument("--status", choices=STATES); s.add_argument("--json", action="store_true")
    s = sub.add_parser("pending"); s.add_argument("--lanes"); s.add_argument("--data")
    s = sub.add_parser("for-ticket"); s.add_argument("issue", type=int)
    s = sub.add_parser("check"); s.add_argument("--pr", type=int, required=True)
    s.add_argument("--coordinator", action="store_true", help="the coordinator's own decision PR: status changes allowed")
    a = ap.parse_args(argv)

    root = repo_root(a.repo)
    cfg = load_json(os.path.join(state_dir(a.repo), "config.json"), {}) or {}
    d, rel = adr_dir(cfg, root)
    out = lambda x: print(json.dumps(x, indent=2, ensure_ascii=False))

    def github():
        from ghapi import GitHub, detect_repo
        t = tr.tracker_cfg(cfg, root)
        return GitHub(t["repo"] or detect_repo(root), cwd=root)

    if a.cmd == "init":
        out(init(d))
    elif a.cmd == "new":
        out(new(d, a.title, a.issue, a.supersedes, a.relates, a.owner))
    elif a.cmd == "status":
        t = tr.tracker_cfg(cfg, root)
        session = (a.session or tr.infer_session(t, a.repo)).zfill(2)
        if session != t["coordinator_session"] and not a.force:
            raise Fail(f"only the coordinator (session {t['coordinator_session']}) decides ADRs; you are {session}")
        out(set_status(d, a.num, a.state, a.by, a.note))
    elif a.cmd == "list":
        rows = [x for x in scan(d) if not a.status or x["status"] == a.status]
        if a.json:
            out(rows)
        else:
            for x in rows:
                print(f"ADR-{x['num']}  {x['status']:<11} {x['title']}")
    elif a.cmd == "pending":
        import work_queue
        lanes = [l.strip() for l in (a.lanes or "").split(",") if l.strip()] or None
        r, _ = work_queue.compute(a.repo, lanes, data_path=a.data)
        rows = pending(scan(d), r)
        print("# Decisions to get made (Proposed ADRs, most blocked work first)\n")
        for x in rows:
            print(f"- **{x['adr']}** {x['title']} · blocks {x['blocks_direct']} directly, {x['blocks_total']} in all"
                  + (f" · {', '.join(x['tickets'][:8])}" if x["tickets"] else "") + f"\n  {x['raw']}")
        if not rows:
            print("_No Proposed ADRs._")
    elif a.cmd == "for-ticket":
        out(for_ticket(github(), scan(d), a.issue))
    elif a.cmd == "check":
        res = check_pr(github(), scan(d), a.pr, rel, a.coordinator)
        out(res)
        if not res["ok"]:
            sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except (Fail, tr.Fail) as e:
        sys.exit(f"error: {e}")
