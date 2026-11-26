#!/usr/bin/env python3
"""Builds the fictional "acme" project on an in-memory GitHub, runs the plugin's real commands through a
coordinator's week, and renders docs/USER_GUIDE.md from docs/guide/USER_GUIDE.template.md with their outputs.

Usage: python3 -I examples/demo.py            regenerate docs/USER_GUIDE.md
       python3 -I examples/demo.py --check    exit 1 if the committed guide is out of date (CI)
"""
import json, os, re, shutil, subprocess, sys, tempfile
from datetime import date, datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "skills", "lane-coordinator", "scripts")
sys.path.insert(0, SCRIPTS)
sys.path.insert(0, os.path.join(ROOT, "tests"))
TEMPLATE = os.path.join(ROOT, "docs", "guide", "USER_GUIDE.template.md")
GUIDE = os.path.join(ROOT, "docs", "USER_GUIDE.md")
TODAY = date(2027, 1, 11)  # a Monday

ROADMAP = """# Roadmap

## M1 — Foundation
Due: 2027-02-15
Goal: People can create an account, sign in and keep a profile.
Exit: Sign-up to profile works end to end in staging.

### auth — Accounts and sign-in
Lane: 01
Depends on: ADR-0001
Email sign-up, sign-in, sessions and password reset.

### profiles — Member profiles
Lane: 02

## M2 — Billing
Due: 2027-03-31
Goal: Members can choose a plan and pay.
Exit: A real card payment succeeds in staging.

### billing — Plans, checkout and receipts
Lane: 03
Depends on: ADR-0002

## M3 — Launch
Due: 2027-05-01
Goal: A public site that explains the product and its prices.

### site — Public site
Lane: 04
"""


def build():
    import adr, release, roadmap, tracker as tr, work_queue as wq
    from common import state_dir
    from fake_gh import FakeGitHub

    out = {}
    tmp = tempfile.mkdtemp(prefix="acme-demo-")
    os.environ["HOME"] = tmp
    os.environ.pop("LANE_SESSION", None)
    repo = os.path.join(tmp, "acme")
    subprocess.run(["git", "init", "-q", repo], check=True)
    subprocess.run(["bash", os.path.join(SCRIPTS, "init.sh"), repo], check=True, stdout=subprocess.DEVNULL)
    sd = state_dir(repo)
    cfg_path = os.path.join(sd, "config.json")
    with open(cfg_path) as fh:
        cfg = json.load(fh)
    cfg["integration_branch"] = "integration"
    cfg["tracker"]["project"] = "acme"
    cfg["tracker"]["repo"] = "acme/acme"
    with open(cfg_path, "w") as fh:
        json.dump(cfg, fh, indent=2)

    # ADRs: one decided earlier, one waiting on the owner, one accepted.
    d = os.path.join(repo, "docs", "adr")
    adr.new(d, "Auth provider: email links and passkeys", today="2026-12-01")
    adr.set_status(d, "1", "accepted", today="2026-12-03")
    adr.new(d, "Billing provider", owner="the product owner", today="2026-12-10")
    adr.new(d, "Money is stored as integer cents", today="2026-12-02")
    adr.set_status(d, "3", "accepted", today="2026-12-04")

    with open(os.path.join(repo, "docs", "roadmap.md"), "w") as fh:
        fh.write(ROADMAP)
    out["roadmap_file"] = ROADMAP.strip()

    gh = FakeGitHub()
    gh.now = "2027-01-11T08:30:00Z"
    t = tr.tracker_cfg(cfg, repo)
    rm, _ = roadmap.parse(ROADMAP)
    plan = roadmap.plan_sync(gh, t, rm)
    out["sync_plan"] = "\n".join(_action(a) for a in plan["actions"]) + \
        ("\n\ndrift:\n" + "\n".join(f"  {x}" for x in plan["drift"]) if plan["drift"] else "")
    roadmap.apply_sync(gh, t, plan)

    def story(title, epic, labels=(), **kw):
        n = tr.create(gh, t, "story", title, epic=epic, **kw)["issue"]
        if labels:
            gh.add_labels(n, list(labels))
        return n

    signup = story("Sign-up with email", "auth", ["kind:feature", "stack:sec"], size="M", priority="p0")
    signin = story("Sign-in and sessions", "auth", ["stack:sec"], size="M", priority="p0", depends=f"#{signup}",
                   body="Follows ADR-0001.")
    reset = story("Password reset", "auth", ["stack:sec"], size="S", priority="p1", depends=f"#{signin}")
    profile = story("Profile page", "profiles", ["stack:ui"], size="M", priority="p1", depends=f"#{signin}")
    avatar = story("Avatar upload", "profiles", size="M", priority="p2")
    bucket = tr.create(gh, t, "task", "Storage bucket and signed URLs", story=avatar, size="S")["issue"]
    upload_ui = tr.create(gh, t, "task", "Upload UI", story=avatar, size="S", depends=f"#{bucket}")["issue"]
    plans = story("Plans table", "billing", ["stack:money"], size="M", priority="p0")
    checkout = story("Checkout", "billing", ["stack:money"], size="L", priority="p0", depends=f"#{plans}")
    receipts = story("Receipts", "billing", ["stack:money"], size="S", priority="p1", depends=f"#{checkout}")
    badge = story("Plan badge on the profile", "profiles", ["stack:ui"], size="S", priority="p2",
                  depends=f"#{profile}, #{plans}")
    landing = story("Landing page", "site", ["stack:ui"], size="M", priority="p1")
    pricing = story("Pricing page", "site", ["stack:ui"], size="S", priority="p1", depends=f"#{plans}")
    out["create_cmds"] = "\n".join([
        'tracker.py story "Sign-up with email" --epic auth --size M --priority p0',
        f'tracker.py story "Sign-in and sessions" --epic auth --size M --priority p0 --depends "#{signup}"',
        'tracker.py story "Avatar upload" --epic profiles --size M --priority p2',
        f'tracker.py task "Storage bucket and signed URLs" --story {avatar} --size S',
        f'tracker.py task "Upload UI" --story {avatar} --size S --depends "#{bucket}"',
        f'tracker.py story "Plan badge on the profile" --epic profiles --size S --depends "#{profile}, #{plans}"'])
    out["create_result"] = json.dumps(tr.create(gh, t, "task", "Avatar crop and resize", story=avatar, size="S"),
                                      indent=2, ensure_ascii=False)

    # Last sprint: sign-up and the landing page went through the whole workflow; v0.1.0 shipped sign-up.
    snap = lambda: wq.data_from_issues(gh.issues(state="all"))
    tr.claim(gh, t, signup, "01")
    for st, who, pr in (("testing", "01", None), ("merging", "01", 101), ("merged", "00", 101)):
        tr.set_status(gh, t, signup, st, who, pr=pr)
    rc = {**release.DEFAULT_RELEASE}
    gh.ahead_by = 7
    out["release_plan"] = release.render_notes(*_plan(release, gh, t, rc))
    pr1 = release.promote(gh, t, rc, "integration")["pr"]
    out["promote_body"] = gh.pr(pr1)["body"].strip()
    gh.merge_pr(pr1, "4f1c2ab")
    out["staging"] = json.dumps(release.staging(gh, t, rc, pr1), indent=2)
    out["prod"] = json.dumps(release.prod(gh, t, rc), indent=2)
    tr.claim(gh, t, landing, "04")
    for st, who, pr in (("testing", "04", None), ("merging", "04", 102), ("merged", "00", 102)):
        tr.set_status(gh, t, landing, st, who, pr=pr)
    pr2 = release.promote(gh, t, rc, "integration")["pr"]
    gh.merge_pr(pr2, "9e07d41")
    release.staging(gh, t, rc, pr2)
    out["release_status"] = json.dumps(release.status(gh, t, rc), indent=2)

    # This morning: sessions claim work from the planner.
    lanes = ["01", "02", "03", "04"]
    out["next_01"] = json.dumps(tr.next_ticket(gh, t, "01", repo, lanes, data=snap()), indent=2, ensure_ascii=False)
    out["next_02"] = json.dumps(tr.next_ticket(gh, t, "02", repo, lanes, data=snap()), indent=2, ensure_ascii=False)
    out["next_03"] = json.dumps(tr.next_ticket(gh, t, "03", repo, lanes, data=snap()), indent=2, ensure_ascii=False)
    tr.set_status(gh, t, signin, "testing", "01")
    out["status_blocked"] = json.dumps(tr.set_status(gh, t, bucket, "blocked", "02",
                                                     note="waits on storage credentials from ops"), indent=2)
    gh._issues[bucket]["updated_at"] = "2027-01-08T16:05:00Z"  # blocked since Friday: shows as a stale claim
    try:
        tr.set_status(gh, t, signin, "merged", "01")
    except tr.Fail as e:
        out["status_refused"] = f"error: {e}"
    out["trail"] = "\n\n".join(c["body"] for c in gh.comments(signup))

    # The coordinator's views.
    r, data = wq.compute(repo, lanes, data=snap())
    from common import load_json
    out["queue"] = _trim_queue(wq.dag.render_report(r) + wq.models_section(r, data, load_json(os.path.join(sd, "routing.json"))))
    adrs = adr.scan(d)
    out["adr_list"] = "\n".join(f"ADR-{x['num']}  {x['status']:<11} {x['title']}" for x in adrs)
    out["adr_pending"] = adr.render_pending(adr.pending(adrs, r)).strip()
    out["adr_for_ticket"] = json.dumps(adr.for_ticket(gh, adrs, checkout), indent=2, ensure_ascii=False)
    gh.prs[120] = {"number": 120, "title": "Sign-in and sessions", "body": f"Refs #{signin}", "state": "open",
                   "merged": False, "base": "integration", "head": "e1-signin",
                   "files": [{"filename": "src/auth/session.ts", "patch": "+export const SESSION_DAYS = 30"}]}
    gh.prs[121] = {"number": 121, "title": "Plans table", "body": f"Refs #{plans}", "state": "open",
                   "merged": False, "base": "integration", "head": "e3-plans",
                   "files": [{"filename": "docs/adr/0002-billing-provider.md",
                              "patch": "-- **Status:** Proposed — 2026-12-10\n+- **Status:** Accepted — 2027-01-11"}]}
    out["adr_check_ok"] = json.dumps(adr.check_pr(gh, adrs, 120, "docs/adr"), indent=2, ensure_ascii=False)
    out["adr_check_bad"] = json.dumps(adr.check_pr(gh, adrs, 121, "docs/adr"), indent=2, ensure_ascii=False)
    out["board"] = tr.render_board(tr.board(gh, t, now=datetime(2027, 1, 11, 9, tzinfo=timezone.utc))).strip()

    # Ledger for the last four weeks: enough history for a forecast and a routing suggestion.
    ledger = os.path.join(sd, "ledger.jsonl")
    rows = []
    for k in range(5):  # L UI tickets tried one tier cheaper (the 10% sample) and all passed first time
        rows.append({"kind": "ticket", "date": f"2026-12-{20 + k}", "ticket": f"#{900 + k}", "type": "L:feature:ui",
                     "model": "claude-haiku-5-5", "explore": True, "review_rounds": 0, "first_pass": True,
                     "escalations": 0, "reverted_7d": False, "est_size": "L", "actual_minutes": 40 + k, "cost_usd": 0.8})
    for k in range(3):
        rows.append({"kind": "ticket", "date": f"2026-12-{27 + k}", "ticket": f"#{910 + k}", "type": "L:feature:ui",
                     "model": "claude-sonnet-5-5", "explore": False, "review_rounds": k % 2, "first_pass": k % 2 == 0,
                     "escalations": 0, "reverted_7d": False, "est_size": "L", "actual_minutes": 45, "cost_usd": 4.1})
    rows.append({"kind": "ticket", "date": "2027-01-04", "ticket": "#920", "type": "M:feature:sec",
                 "model": "claude-sonnet-5-5", "explore": False, "review_rounds": 1, "first_pass": False,
                 "escalations": 0, "reverted_7d": False, "est_size": "M", "actual_minutes": 190, "cost_usd": 3.3})
    with open(ledger, "w") as fh:
        fh.write("".join(json.dumps(x) + "\n" for x in rows))
    env = {**os.environ, "HOME": tmp}
    run = lambda *a: subprocess.run([sys.executable, "-I", *a], env=env, capture_output=True, text=True,
                                    check=True).stdout.strip()
    out["route"] = "\n".join(
        f"$ route.py {ty}" + (f" --issue {n}" if n else "") + "\n" +
        run(os.path.join(SCRIPTS, "route.py"), ty, *(["--issue", str(n)] if n else []), "--repo", repo)
        for ty, n in (("M:feature:money", 131), ("S:bug:ui", None), ("M:feature:api", 140), ("L:feature:ui", None)))
    out["sprint"] = run(os.path.join(SCRIPTS, "sprint.py"), "--repo", repo, "--today", "2027-01-10")

    from common import load_ledger
    s = roadmap.status(rm, r, data, adrs, ["v0.1.0"], load_ledger(sd), today=TODAY, lanes=lanes)
    out["roadmap_status"] = roadmap.render_status(s).strip()

    shutil.rmtree(tmp, ignore_errors=True)
    return {k: _scrub(v, tmp) for k, v in out.items()}


def _plan(release, gh, t, rc):
    p = release.plan(gh, t, rc, "integration")
    return p["version"], p["tickets"], p["compare"]


def _action(a):
    what = {"create_milestone": lambda: f'{a["title"]} (due {a["due"]})',
            "update_milestone": lambda: a["title"],
            "create_epic": lambda: f'{a["key"]} — {a["title"]} → {a["milestone"]}, lane {a["lane"]}',
            "move_epic": lambda: f'#{a["issue"]} {a["key"]}: {a["from"]} → {a["to"]}',
            "set_lane": lambda: f'#{a["issue"]} {a["key"]} → lane {a["lane"]}',
            "close_milestone": lambda: a["title"], "ensure_label": lambda: a["label"]}[a["do"]]()
    return f"{a['do']:<17} {what}"


def _trim_queue(report):
    """Keep the guide readable: the full report, minus the long per-lane queues."""
    out, skip = [], False
    for line in report.split("\n"):
        if line.startswith("## Lane queues"):
            skip = True
            out.append("## Lane queues (top 10)\n\n… one ranked list per lane …\n")
            continue
        if skip and line.startswith("## ") and not line.startswith("## Lane queues"):
            skip = False
        if not skip:
            out.append(line)
    return "\n".join(out)


def _scrub(text, tmp):
    text = text.replace(tmp, "~")
    return re.sub(r"(lane:claim \S+) [0-9a-f]{8}", r"\1 1a2b3c4d", text)


def render(outputs):
    with open(TEMPLATE, encoding="utf-8") as fh:
        tpl = fh.read()
    missing = sorted(set(re.findall(r"\{\{(\w+)\}\}", tpl)) - set(outputs))
    if missing:
        sys.exit(f"template names outputs the demo doesn't produce: {', '.join(missing)}")
    return "<!-- Generated by examples/demo.py from docs/guide/USER_GUIDE.template.md: edit those, not this. -->\n" + \
        re.sub(r"\{\{(\w+)\}\}", lambda m: outputs[m.group(1)], tpl)


def main():
    guide = render(build())
    if "--check" in sys.argv:
        with open(GUIDE, encoding="utf-8") as fh:
            if fh.read() != guide:
                sys.exit("docs/USER_GUIDE.md is out of date: run python3 -I examples/demo.py")
        print("docs/USER_GUIDE.md is up to date")
        return
    os.makedirs(os.path.dirname(GUIDE), exist_ok=True)
    with open(GUIDE, "w", encoding="utf-8") as fh:
        fh.write(guide)
    print(f"wrote {os.path.relpath(GUIDE, ROOT)}")


if __name__ == "__main__":
    main()
