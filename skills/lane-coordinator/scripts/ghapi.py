"""A thin GitHub REST layer over `gh api`, so tracker logic can be tested against an in-memory fake (tests/fake_gh.py)."""
import json, subprocess, urllib.parse


class GhError(RuntimeError):
    pass


class GitHub:
    def __init__(self, repo, cwd=None):
        self.repo = repo  # "owner/name"
        self.cwd = cwd

    # ─── transport ───
    def api(self, method, path, body=None, paginate=False):
        args = ["gh", "api", "-X", method, path, "-H", "Accept: application/vnd.github+json"]
        if paginate:
            args += ["--paginate", "--slurp"]
        if body is not None:
            args += ["--input", "-"]
        p = subprocess.run(args, cwd=self.cwd, input=json.dumps(body) if body is not None else None,
                           capture_output=True, text=True)
        if p.returncode != 0:
            raise GhError(f"gh api {method} {path}: {p.stderr.strip() or p.stdout.strip()}")
        if not p.stdout.strip():
            return None
        out = json.loads(p.stdout)
        if paginate:  # --slurp gives a list of pages
            return [x for page in out for x in (page if isinstance(page, list) else [page])]
        return out

    def _r(self, suffix=""):
        return f"repos/{self.repo}{suffix}"

    # ─── issues ───
    def issue(self, n):
        return _issue(self.api("GET", self._r(f"/issues/{n}")))

    def issues(self, state="open", labels=()):
        q = urllib.parse.urlencode({"state": state, "per_page": 100, **({"labels": ",".join(labels)} if labels else {})})
        return [_issue(i) for i in self.api("GET", self._r(f"/issues?{q}"), paginate=True) if "pull_request" not in i]

    def create_issue(self, title, body, labels, milestone=None):
        payload = {"title": title, "body": body, "labels": list(labels)}
        if milestone is not None:
            payload["milestone"] = milestone
        return _issue(self.api("POST", self._r("/issues"), payload))

    def set_state(self, n, state, reason=None):
        payload = {"state": state}
        if reason:
            payload["state_reason"] = reason
        return _issue(self.api("PATCH", self._r(f"/issues/{n}"), payload))

    def add_labels(self, n, labels):
        if labels:
            self.api("POST", self._r(f"/issues/{n}/labels"), {"labels": list(labels)})

    def remove_label(self, n, label):
        try:
            self.api("DELETE", self._r(f"/issues/{n}/labels/{urllib.parse.quote(label, safe='')}"))
        except GhError as e:
            if "Label does not exist" not in str(e) and "404" not in str(e):
                raise

    def comments(self, n):
        return [{"id": c["id"], "created_at": c["created_at"], "body": c.get("body") or ""}
                for c in self.api("GET", self._r(f"/issues/{n}/comments?per_page=100"), paginate=True)]

    def comment(self, n, body):
        c = self.api("POST", self._r(f"/issues/{n}/comments"), {"body": body})
        return {"id": c["id"], "created_at": c["created_at"], "body": c.get("body") or ""}

    def add_sub_issue(self, parent, child_id):
        self.api("POST", self._r(f"/issues/{parent}/sub_issues"), {"sub_issue_id": child_id})

    def pr_body(self, n):
        return self.api("GET", self._r(f"/pulls/{n}")).get("body") or ""

    # ─── pull requests, commits, tags and releases ───
    def pr(self, n):
        p = self.api("GET", self._r(f"/pulls/{n}"))
        return {"number": p["number"], "title": p["title"], "body": p.get("body") or "", "state": p["state"],
                "merged": bool(p.get("merged_at")), "merge_commit_sha": p.get("merge_commit_sha"),
                "base": p["base"]["ref"], "head": p["head"]["ref"]}

    def pr_files(self, n):
        return [{"filename": f["filename"], "patch": f.get("patch") or ""}
                for f in self.api("GET", self._r(f"/pulls/{n}/files?per_page=100"), paginate=True)]

    def open_pr(self, head, base):
        owner = self.repo.split("/")[0]
        found = self.api("GET", self._r(f"/pulls?state=open&head={owner}:{head}&base={base}"))
        return found[0]["number"] if found else None

    def create_pr(self, title, body, head, base):
        return self.api("POST", self._r("/pulls"), {"title": title, "body": body, "head": head, "base": base})["number"]

    def update_pr(self, n, title, body):
        self.api("PATCH", self._r(f"/pulls/{n}"), {"title": title, "body": body})

    def compare(self, base, head):
        c = self.api("GET", self._r(f"/compare/{base}...{head}"))
        return {"ahead_by": c["ahead_by"], "commits": [x["commit"]["message"].split("\n")[0] for x in c["commits"]],
                "url": c.get("html_url")}

    def commit_parents(self, sha):
        return len(self.api("GET", self._r(f"/commits/{sha}"))["parents"])

    def tags(self):
        return [t["name"] for t in self.api("GET", self._r("/tags?per_page=100"), paginate=True)]

    def tag_sha(self, tag):
        obj = self.api("GET", self._r(f"/git/ref/tags/{urllib.parse.quote(tag)}"))["object"]
        if obj["type"] == "tag":  # annotated tag → its commit
            obj = self.api("GET", self._r(f"/git/tags/{obj['sha']}"))["object"]
        return obj["sha"]

    def create_release(self, tag, sha, title, notes, prerelease, latest):
        r = self.api("POST", self._r("/releases"), {"tag_name": tag, "target_commitish": sha, "name": title,
                                                     "body": notes, "prerelease": prerelease,
                                                     "make_latest": "true" if latest else "false"})
        return r.get("html_url")

    # ─── labels and milestones ───
    def labels(self):
        return {l["name"] for l in self.api("GET", self._r("/labels?per_page=100"), paginate=True)}

    def create_label(self, name, color, description=""):
        self.api("POST", self._r("/labels"), {"name": name, "color": color, "description": description})

    def milestones(self):
        return {m["title"]: m["number"] for m in self.api("GET", self._r("/milestones?state=all&per_page=100"),
                                                          paginate=True)}

    def milestones_full(self):
        return [{"number": m["number"], "title": m["title"], "state": m["state"], "description": m.get("description") or "",
                 "due_on": (m.get("due_on") or "")[:10] or None}
                for m in self.api("GET", self._r("/milestones?state=all&per_page=100"), paginate=True)]

    def update_milestone(self, number, **fields):
        if "due_on" in fields and fields["due_on"] and len(fields["due_on"]) == 10:
            fields["due_on"] = f"{fields['due_on']}T23:59:59Z"
        self.api("PATCH", self._r(f"/milestones/{number}"), fields)

    def set_issue_milestone(self, n, number):
        self.api("PATCH", self._r(f"/issues/{n}"), {"milestone": number})

    def create_milestone(self, title, due_on=None, description=""):
        payload = {"title": title, "description": description}
        if due_on:
            payload["due_on"] = f"{due_on}T23:59:59Z" if len(due_on) == 10 else due_on
        return self.api("POST", self._r("/milestones"), payload)["number"]


def _issue(i):
    return {"number": i["number"], "id": i["id"], "title": i["title"], "body": i.get("body") or "",
            "state": i["state"], "labels": [l["name"] if isinstance(l, dict) else l for l in i.get("labels", [])],
            "milestone": (i.get("milestone") or {}).get("title"), "updated_at": i.get("updated_at")}


def detect_repo(cwd):
    p = subprocess.run(["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"], cwd=cwd,
                       capture_output=True, text=True)
    if p.returncode != 0:
        raise GhError("could not detect the GitHub repo; set tracker.repo in config.json")
    return p.stdout.strip()
