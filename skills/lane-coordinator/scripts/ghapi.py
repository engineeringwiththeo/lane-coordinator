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

    # ─── labels and milestones ───
    def labels(self):
        return {l["name"] for l in self.api("GET", self._r("/labels?per_page=100"), paginate=True)}

    def create_label(self, name, color, description=""):
        self.api("POST", self._r("/labels"), {"name": name, "color": color, "description": description})

    def milestones(self):
        return {m["title"]: m["number"] for m in self.api("GET", self._r("/milestones?state=all&per_page=100"),
                                                          paginate=True)}

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
