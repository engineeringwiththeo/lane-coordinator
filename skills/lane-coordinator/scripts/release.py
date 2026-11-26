#!/usr/bin/env python3
"""Feature releases: integration → main, a release-candidate tag for staging, the same commit tagged for production.

Usage: python3 -I release.py [--repo PATH] <command> ...

  plan [--bump minor|patch|major]       read-only: what would ship (merged tickets, commits) and the next version
  promote [--bump ...] [--draft-only]   open (or refresh) the "Release vX.Y.Z" PR from integration into main,
                                        with release notes grouped by milestone and epic
  staging --pr N                        after the coordinator merges the promotion PR (merge commit, never squash):
                                        tag its merge commit vX.Y.Z-rc.K, publish a GitHub prerelease, move the
                                        tickets merged → staging, label them release:vX.Y.Z, run release.staging_cmd
  prod [--version vX.Y.Z] [--done]      tag the newest staged candidate's commit vX.Y.Z, publish it as the latest
                                        release, move its tickets staging → deployed, run release.prod_cmd;
                                        --done also marks them done (closes the issues) after the owner's review
  status                                production and staging versions, and what is waiting at each stage

Versions are semver. A feature release bumps minor by default (config release.bump). A rejected candidate is
fixed forward on integration and promoted again: the pending version is reused with the next rc number.
Only the coordinator runs this; ticket moves are made as tracker.coordinator_session.
"""
import argparse, json, os, re, shlex, subprocess, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dag
import tracker as tr
from common import load_json, repo_root, state_dir
from ghapi import GitHub, GhError, detect_repo

DEFAULT_RELEASE = {"main_branch": "main", "bump": "minor", "tag_prefix": "v", "staging_cmd": None, "prod_cmd": None}
SEMVER = re.compile(r"^(?P<p>.*?)(\d+)\.(\d+)\.(\d+)(?:-rc\.(\d+))?$")


class Fail(Exception):
    pass


# ─── versions ────────────────────────────────────────────────────────────────────────────────────

def parse_tags(names, prefix="v"):
    """→ (sorted production versions, {version: [rc numbers]}) for tags like v1.4.0 and v1.4.0-rc.2."""
    prod, rcs = set(), {}
    for n in names:
        if not n.startswith(prefix):
            continue
        m = SEMVER.match(n[len(prefix):])
        if not m or m.group("p"):
            continue
        v = (int(m.group(2)), int(m.group(3)), int(m.group(4)))
        if m.group(5) is None:
            prod.add(v)
        else:
            rcs.setdefault(v, []).append(int(m.group(5)))
    return sorted(prod), {v: sorted(r) for v, r in rcs.items()}


def bump(v, kind):
    major, minor, patch = v
    return {"major": (major + 1, 0, 0), "minor": (major, minor + 1, 0), "patch": (major, minor, patch + 1)}[kind]


def vstr(v, prefix="v", rc=None):
    return f"{prefix}{v[0]}.{v[1]}.{v[2]}" + (f"-rc.{rc}" if rc else "")


def parse_version(s, prefix="v"):
    m = SEMVER.match(s[len(prefix):] if s.startswith(prefix) else s)
    if not m:
        raise Fail(f"not a version: {s}")
    return int(m.group(2)), int(m.group(3)), int(m.group(4))


def pending_version(names, kind, prefix="v"):
    """The version the next candidate belongs to: a staged-but-unreleased version if there is one, else a bump
    of the latest production version (v0.0.0 when there is none)."""
    prod, rcs = parse_tags(names, prefix)
    latest = prod[-1] if prod else (0, 0, 0)
    pending = [v for v in rcs if v > latest and v not in prod]
    return max(pending) if pending else bump(latest, kind)


def next_rc(names, v, prefix="v"):
    _prod, rcs = parse_tags(names, prefix)
    return (rcs.get(v) or [0])[-1] + 1


# ─── notes ───────────────────────────────────────────────────────────────────────────────────────

def ticket_rows(t, issues):
    rows = []
    for i in issues:
        g = lambda p: next((l[len(p):] for l in i["labels"] if l.startswith(p)), None)
        rows.append({"issue": i["number"], "title": i["title"], "milestone": i["milestone"] or g("milestone:") or "—",
                     "epic": g("epic:") or "—", "feature": g("feature:"), "type": g("type:") or "issue"})
    return sorted(rows, key=lambda r: (r["milestone"], r["epic"], r["issue"]))


def render_notes(version, rows, compare=None):
    out = [f"## {version}"]
    if not rows:
        out += ["", "_No tracked tickets._"]
    ms = None
    for r in rows:
        if r["milestone"] != ms:
            ms = r["milestone"]
            out += ["", f"### {ms}", ""]
        feat = f" · {r['feature']}" if r["feature"] else ""
        out.append(f"- #{r['issue']} {r['title']} ({r['epic']}{feat})")
    if compare:
        out += ["", f"{compare['ahead_by']} commits" + (f" · [diff]({compare['url']})" if compare.get("url") else "")]
    return "\n".join(out).strip() + "\n"


def refs_block(rows):
    return "\n".join(f"Refs #{r['issue']}" for r in rows)  # Refs, not Closes: the coordinator closes after review


# ─── commands ────────────────────────────────────────────────────────────────────────────────────

def plan(gh, t, rc_cfg, integration, kind=None):
    names = gh.tags()
    v = pending_version(names, kind or rc_cfg["bump"], rc_cfg["tag_prefix"])
    cmp_ = gh.compare(rc_cfg["main_branch"], integration)
    merged = gh.issues(state="all", labels=[t["status_prefix"] + "merged"])
    rows = ticket_rows(t, merged)
    return {"version": vstr(v, rc_cfg["tag_prefix"]), "ahead_by": cmp_["ahead_by"], "tickets": rows,
            "commits": cmp_["commits"], "compare": cmp_}


def promote(gh, t, rc_cfg, integration, kind=None, draft_only=False):
    p = plan(gh, t, rc_cfg, integration, kind)
    if p["ahead_by"] == 0:
        raise Fail(f"{integration} has nothing that {rc_cfg['main_branch']} lacks")
    title = f"Release {p['version']}"
    body = (f"Promotes `{integration}` into `{rc_cfg['main_branch']}`. Merge with a **merge commit** (never squash), "
            f"then run `release.py staging --pr <this PR>`.\n\n" + render_notes(p["version"], p["tickets"], p["compare"])
            + "\n" + refs_block(p["tickets"]) + "\n")
    if draft_only:
        return {"title": title, "body": body}
    n = gh.open_pr(integration, rc_cfg["main_branch"])
    if n:
        gh.update_pr(n, title, body)
        return {"pr": n, "title": title, "updated": True, "tickets": len(p["tickets"])}
    return {"pr": gh.create_pr(title, body, integration, rc_cfg["main_branch"]), "title": title, "created": True,
            "tickets": len(p["tickets"])}


def run_hook(cmd, root, **kw):
    if not cmd:
        return None
    line = cmd.format(**{k: shlex.quote(str(v)) for k, v in kw.items()})
    p = subprocess.run(line, shell=True, cwd=root, capture_output=True, text=True)
    if p.returncode != 0:
        raise Fail(f"hook failed ({p.returncode}): {line}\n{p.stderr.strip()[-2000:]}")
    return {"cmd": line, "ok": True}


def staging(gh, t, rc_cfg, pr_number, root=None):
    pr = gh.pr(pr_number)
    if not pr["merged"]:
        raise Fail(f"PR #{pr_number} isn't merged yet; review and merge it (merge commit) first")
    if pr["base"] != rc_cfg["main_branch"]:
        raise Fail(f"PR #{pr_number} targets {pr['base']}, not {rc_cfg['main_branch']}")
    m = re.search(r"\b(" + re.escape(rc_cfg["tag_prefix"]) + r"\d+\.\d+\.\d+)\b", pr["title"])
    if not m:
        raise Fail(f'PR #{pr_number} title "{pr["title"]}" names no version; promotion PRs are "Release vX.Y.Z"')
    v = parse_version(m.group(1), rc_cfg["tag_prefix"])
    names = gh.tags()
    if vstr(v, rc_cfg["tag_prefix"]) in names:
        raise Fail(f"{vstr(v, rc_cfg['tag_prefix'])} is already released; promote again for a new version")
    sha = pr["merge_commit_sha"]
    warnings = []
    if gh.commit_parents(sha) < 2:
        warnings.append("the promotion PR was not merged with a merge commit: integration and main now differ in "
                        "history, so the next promotion will show these commits again")
    rc = next_rc(names, v, rc_cfg["tag_prefix"])
    tag = vstr(v, rc_cfg["tag_prefix"], rc)
    issues = [gh.issue(n) for n in dag.referenced_issues(pr["body"])]
    rows = ticket_rows(t, issues)
    url = gh.create_release(tag, sha, f"{tag} (staging)", render_notes(tag, rows), prerelease=True, latest=False)
    label = f"release:{vstr(v, rc_cfg['tag_prefix'])}"
    tr.ensure_labels(gh, [label])
    moved = []
    for i in issues:
        st = tr.status_of(t, i)
        gh.add_labels(i["number"], [label])
        if st == "merged":
            tr.set_status(gh, t, i["number"], "staging", t["coordinator_session"], note=f"On staging in {tag}.",
                          pr=pr_number)
            moved.append(i["number"])
    hook = run_hook(rc_cfg["staging_cmd"], root, tag=tag, sha=sha, version=vstr(v, rc_cfg["tag_prefix"]))
    return {"tag": tag, "sha": sha, "release": url, "staged": moved, "label": label, "hook": hook,
            "warnings": warnings}


def prod(gh, t, rc_cfg, version=None, done=False, root=None):
    names = gh.tags()
    prod_v, rcs = parse_tags(names, rc_cfg["tag_prefix"])
    v = parse_version(version, rc_cfg["tag_prefix"]) if version else \
        max([x for x in rcs if not prod_v or x > prod_v[-1]], default=None)
    if v is None or v not in rcs:
        raise Fail("no staged release candidate to ship; run staging first")
    final = vstr(v, rc_cfg["tag_prefix"])
    if final in names:
        raise Fail(f"{final} is already released")
    rc_tag = vstr(v, rc_cfg["tag_prefix"], rcs[v][-1])
    sha = gh.tag_sha(rc_tag)
    label = f"release:{final}"
    issues = gh.issues(state="all", labels=[label])
    rows = ticket_rows(t, issues)
    url = gh.create_release(final, sha, final, render_notes(final, rows) + f"\nShipped from {rc_tag}.\n",
                            prerelease=False, latest=True)
    moved, closed = [], []
    for i in issues:
        if tr.status_of(t, i) == "staging":
            tr.set_status(gh, t, i["number"], "deployed", t["coordinator_session"], note=f"In production in {final}.")
            moved.append(i["number"])
        if done and tr.status_of(t, gh.issue(i["number"])) == "deployed":
            tr.set_status(gh, t, i["number"], "done", t["coordinator_session"], note=f"Released in {final}.")
            closed.append(i["number"])
    hook = run_hook(rc_cfg["prod_cmd"], root, tag=final, sha=sha, version=final)
    return {"tag": final, "from": rc_tag, "sha": sha, "release": url, "deployed": moved, "done": closed, "hook": hook}


def status(gh, t, rc_cfg):
    names = gh.tags()
    prod_v, rcs = parse_tags(names, rc_cfg["tag_prefix"])
    latest = prod_v[-1] if prod_v else None
    pending = sorted(v for v in rcs if latest is None or v > latest)
    count = lambda s: len(gh.issues(state="all", labels=[t["status_prefix"] + s]))
    return {"production": vstr(latest, rc_cfg["tag_prefix"]) if latest else None,
            "staging": vstr(pending[-1], rc_cfg["tag_prefix"], rcs[pending[-1]][-1]) if pending else None,
            "tickets": {s: count(s) for s in ("merged", "staging", "deployed")}}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--repo")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("plan"); s.add_argument("--bump", choices=["major", "minor", "patch"])
    s = sub.add_parser("promote"); s.add_argument("--bump", choices=["major", "minor", "patch"])
    s.add_argument("--draft-only", action="store_true")
    s = sub.add_parser("staging"); s.add_argument("--pr", type=int, required=True)
    s = sub.add_parser("prod"); s.add_argument("--version"); s.add_argument("--done", action="store_true")
    sub.add_parser("status")
    a = ap.parse_args(argv)

    root = repo_root(a.repo)
    cfg = load_json(os.path.join(state_dir(a.repo), "config.json"), {}) or {}
    t = tr.tracker_cfg(cfg, root)
    rc_cfg = {**DEFAULT_RELEASE, **(cfg.get("release") or {})}
    integration = cfg.get("integration_branch", "integration")
    if integration == rc_cfg["main_branch"]:
        raise Fail("integration_branch and release.main_branch are the same; releases need a separate integration branch")
    gh = GitHub(t["repo"] or detect_repo(root), cwd=root)

    if a.cmd == "plan":
        p = plan(gh, t, rc_cfg, integration, a.bump)
        sys.stdout.write(render_notes(p["version"], p["tickets"], p["compare"]))
        if p["ahead_by"] == 0:
            print("\nNothing to release.")
    elif a.cmd == "promote":
        r = promote(gh, t, rc_cfg, integration, a.bump, a.draft_only)
        print(r["body"] if a.draft_only else json.dumps(r, indent=2))
    elif a.cmd == "staging":
        print(json.dumps(staging(gh, t, rc_cfg, a.pr, root), indent=2))
    elif a.cmd == "prod":
        print(json.dumps(prod(gh, t, rc_cfg, a.version, a.done, root), indent=2))
    elif a.cmd == "status":
        print(json.dumps(status(gh, t, rc_cfg), indent=2))


if __name__ == "__main__":
    try:
        main()
    except (Fail, tr.Fail, GhError) as e:
        sys.exit(f"error: {e}")
