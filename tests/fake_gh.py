"""An in-memory stand-in for ghapi.GitHub, enough for tracker tests."""
import itertools

_clock = itertools.count(1)


def _ts():
    return f"2026-11-26T10:{next(_clock):05d}Z"  # sortable, strictly increasing


class FakeGitHub:
    def __init__(self):
        self._issues, self._comments, self._labels, self._milestones, self.subs = {}, {}, set(), {}, []
        self.prs = {}
        self._ids = itertools.count(1000)
        self.on_comment = None  # hook(n, comment) to simulate another session acting concurrently

    def seed(self, number, title, labels=(), state="open", body="", milestone=None, updated_at=None):
        self._issues[number] = {"number": number, "id": next(self._ids), "title": title, "body": body,
                                "state": state, "labels": list(labels), "milestone": milestone,
                                "updated_at": updated_at or "2026-11-26T09:00:00Z"}
        self._labels.update(labels)
        return self._issues[number]

    def issue(self, n):
        i = self._issues[n]
        return {**i, "labels": list(i["labels"])}

    def issues(self, state="open", labels=()):
        return [self.issue(n) for n, i in sorted(self._issues.items())
                if (state == "all" or i["state"] == state) and all(l in i["labels"] for l in labels)]

    def create_issue(self, title, body, labels, milestone=None):
        n = max(self._issues, default=0) + 1
        ms = next((t for t, num in self._milestones.items() if num == milestone), None)
        return self.issue(self.seed(n, title, labels, body=body, milestone=ms)["number"])

    def set_state(self, n, state, reason=None):
        self._issues[n]["state"] = state
        self._issues[n]["state_reason"] = reason
        return self.issue(n)

    def add_labels(self, n, labels):
        for l in labels:
            if l not in self._issues[n]["labels"]:
                self._issues[n]["labels"].append(l)
            self._labels.add(l)

    def remove_label(self, n, label):
        if label in self._issues[n]["labels"]:
            self._issues[n]["labels"].remove(label)

    def comments(self, n):
        return list(self._comments.get(n, []))

    def comment(self, n, body):
        c = {"id": next(self._ids), "created_at": _ts(), "body": body}
        self._comments.setdefault(n, []).append(c)
        if self.on_comment:
            hook, self.on_comment = self.on_comment, None
            hook(n, c)
        return c

    def add_sub_issue(self, parent, child_id):
        self.subs.append((parent, child_id))

    def pr_body(self, n):
        return self.prs[n]

    def labels(self):
        return set(self._labels)

    def create_label(self, name, color, description=""):
        self._labels.add(name)

    def milestones(self):
        return dict(self._milestones)

    def create_milestone(self, title, due_on=None, description=""):
        self._milestones[title] = len(self._milestones) + 1
        return self._milestones[title]
