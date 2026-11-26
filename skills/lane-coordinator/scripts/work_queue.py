#!/usr/bin/env python3
"""What each lane should pick up next: the dependency-DAG work queue.

Usage: python3 -I work_queue.py [--repo PATH] [--lanes 01,04] [--json OUT] [--models]
                                [--data SNAPSHOT.json] [--save-data SNAPSHOT.json]

Reads stories (config `queue.source`: "backlog" = story headings in a markdown file, "issues" = GitHub issues with
`Depends on:` lines), ADR statuses (optional) and every issue and every PR into the integration branch (two `gh`
calls), and prints a markdown report: next item per active lane, ranked ready queues, top blockers, what blocks the
rest, merged-but-open issues, unparsed dependencies and cycles. Read-only on GitHub.

--models     adds the routed model for each lane's next item (size from the story; kind/stack from `kind:` and
             `stack:` issue labels if present, otherwise the size default plus a reminder to check the stack floor).
--data       uses a saved {"issues": [...], "prs": [...]} snapshot instead of calling gh (tests, offline).
--save-data  writes the fetched snapshot for later --data runs.
"""
import json, os, re, subprocess, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dag
from common import load_json, repo_root, resolve, state_dir


def arg(name):
    a = sys.argv
    return a[a.index(name) + 1] if name in a else None


DEFAULT_QUEUE = {
    "source": "issues",
    "backlog": None,
    "adr_dir": None,
    "issue_limit": 2000,
    "pr_limit": 1000,
}


def gh(args, cwd):
    out = subprocess.run(["gh", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def fetch(root, base, qc):
    raw_issues = gh(["issue", "list", "--state", "all", "--limit", str(qc["issue_limit"]), "--json",
                     "number,state,labels,title,assignees" + (",body" if qc["source"] == "issues" else "")], root)
    issues = [{"number": i["number"], "state": "CLOSED" if i["state"] == "CLOSED" else "OPEN", "title": i["title"],
               "labels": [l["name"] for l in i["labels"]], "assignees": [a["login"] for a in i["assignees"]],
               **({"body": i.get("body") or ""} if qc["source"] == "issues" else {})} for i in raw_issues]
    prs = gh(["pr", "list", "--base", base, "--state", "all", "--limit", str(qc["pr_limit"]), "--json",
              "number,title,body,state,mergedAt"], root)
    if len(issues) >= qc["issue_limit"]:
        print(f"⚠ gh returned {len(issues)} issues, the limit; raise queue.issue_limit", file=sys.stderr)
    if len(prs) >= qc["pr_limit"]:
        print(f"⚠ gh returned {len(prs)} PRs, the limit; raise queue.pr_limit", file=sys.stderr)
    return {"issues": issues, "prs": prs}


def read_adrs(root, adr_dir):
    adrs = {}
    if not adr_dir:
        return adrs
    d = os.path.join(root, adr_dir)
    for f in sorted(os.listdir(d)):
        m = re.match(r"^(\d{4})-.*\.md$", f)
        if m:
            with open(os.path.join(d, f), encoding="utf-8") as fh:
                adrs[m.group(1)] = dag.adr_status_from_file(fh.read())
    return adrs


def model_hint(node, issue, routing):
    labels = issue["labels"] if issue else []
    kind = next((l.split(":", 1)[1] for l in labels if l.startswith("kind:")), "feature")
    stack = next((l.split(":", 1)[1] for l in labels if l.startswith("stack:")), None)
    if not node["size"]:
        return "model ? (no size)"
    t = f"{node['size']}:{kind}:{stack or '?'}"
    model = resolve(routing, t)
    return f"{t} → {model}" + ("" if stack else " (check stack floor)")


def queue_cfg(cfg, root):
    """dag config: label names from config.queue, plus the status prefix and session-label pattern from
    config.tracker so claimed and status-labelled tickets count as in progress / done / blocked."""
    labels_cfg = {k: v for k, v in (cfg.get("queue") or {}).items() if k in dag.DEFAULTS}
    try:
        import tracker
        t = tracker.tracker_cfg(cfg, root)
        labels_cfg.setdefault("status_prefix", t["status_prefix"])
        labels_cfg.setdefault("session_label_re", tracker.session_re(t).pattern)
    except Exception:  # tracker config invalid: plan without it
        pass
    return labels_cfg


def compute(repo=None, lanes=None, data_path=None, save_path=None):
    root = repo_root(repo)
    sd = state_dir(repo)
    cfg = load_json(os.path.join(sd, "config.json"), {}) or {}
    qc = {**DEFAULT_QUEUE, **(cfg.get("queue") or {})}
    labels_cfg = queue_cfg(cfg, root)
    base = cfg.get("integration_branch", "main")

    data = load_json(data_path) if data_path else fetch(root, base, qc)
    if save_path:
        with open(save_path, "w") as fh:
            json.dump(data, fh)

    if qc["source"] == "backlog":
        if not qc["backlog"]:
            sys.exit("queue.source is 'backlog' but queue.backlog (the markdown path) is not set in config.json")
        with open(os.path.join(root, qc["backlog"]), encoding="utf-8") as fh:
            stories, problems = dag.parse_backlog(fh.read())
    else:
        stories, problems = dag.stories_from_issues(data["issues"], labels_cfg), []

    r = dag.build_queue(stories, data["issues"], data["prs"], read_adrs(root, qc["adr_dir"]), lanes, labels_cfg)
    r["problems"][:0] = problems
    return r, data


def main():
    repo = arg("--repo")
    sd = state_dir(repo)
    lanes = [l.strip() for l in (arg("--lanes") or "").split(",") if l.strip()] or None
    r, data = compute(repo, lanes, arg("--data"), arg("--save-data"))

    report = dag.render_report(r)
    if "--models" in sys.argv:
        routing = load_json(os.path.join(sd, "routing.json"))
        if routing:
            issues = {i["number"]: i for i in data["issues"]}
            hints = ["", "## Routed model per next item", ""]
            for a in r["assignments"]:
                if a["id"]:
                    n = r["nodes"][a["id"]]
                    hints.append(f"- lane {a['lane']}: {a['id']} · {model_hint(n, issues.get(n['issue']), routing)}")
            report += "\n".join(hints) + "\n"
    sys.stdout.write(report)

    if arg("--json"):
        with open(arg("--json"), "w") as fh:
            json.dump(dag.to_json(r), fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        print(f"wrote {arg('--json')}", file=sys.stderr)


if __name__ == "__main__":
    main()
