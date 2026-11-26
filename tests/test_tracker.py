"""Tracker tests against an in-memory GitHub. Run: python3 -I -m unittest discover -s tests"""
import os, subprocess, sys, tempfile, unittest
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "skills", "lane-coordinator", "scripts"))
sys.path.insert(0, HERE)
import dag  # noqa: E402
import tracker as tr  # noqa: E402
from fake_gh import FakeGitHub  # noqa: E402

T = tr.tracker_cfg({"tracker": {"project": "acme"}}, "/x/acme")


def statuses(gh, n):
    return sorted(l for l in gh.issue(n)["labels"] if l.startswith("status:"))


class Config(unittest.TestCase):
    def test_session_label_and_pattern(self):
        self.assertEqual(tr.session_label(T, 3), "acme-03")
        self.assertTrue(tr.session_re(T).match("acme-12"))
        self.assertFalse(tr.session_re(T).match("lane:03"))

    def test_infer_from_worktree_name(self):
        with tempfile.TemporaryDirectory() as d:
            wt = os.path.join(d, "acme-07")
            subprocess.run(["git", "init", "-q", wt], check=True)
            env = os.environ.pop("LANE_SESSION", None)
            try:
                self.assertEqual(tr.infer_session(T, wt), "07")
            finally:
                if env:
                    os.environ["LANE_SESSION"] = env


class Claims(unittest.TestCase):
    def setUp(self):
        self.gh = FakeGitHub()
        self.gh.seed(1, "Story one", ["type:story", "status:todo"])

    def test_claim_sets_label_and_status(self):
        r = tr.claim(self.gh, T, 1, "03")
        self.assertTrue(r["claimed"])
        self.assertIn("acme-03", self.gh.issue(1)["labels"])
        self.assertEqual(statuses(self.gh, 1), ["status:in-progress"])

    def test_cannot_claim_someone_elses(self):
        tr.claim(self.gh, T, 1, "03")
        with self.assertRaisesRegex(tr.Fail, "claimed by acme-03"):
            tr.claim(self.gh, T, 1, "04")

    def test_race_earliest_claim_comment_wins(self):
        # Session 04 has posted its claim comment but not yet added its label when 03 claims.
        self.gh.comment(1, "<!-- lane:claim acme-04 aaaa -->")
        r = tr.claim(self.gh, T, 1, "03")
        self.assertEqual((r["claimed"], r["winner"]), (False, "acme-04"))
        self.assertNotIn("acme-03", self.gh.issue(1)["labels"])
        # 04 finishes its claim and still wins.
        self.gh.add_labels(1, ["acme-04"])
        self.assertEqual(tr.claim_winner(self.gh.comments(1)), "acme-04")

    def test_race_later_claimer_loses(self):
        def other_session(n, _c):  # 04 comments right after 03, before 03 re-reads
            self.gh.comment(n, "<!-- lane:claim acme-04 bbbb -->")
        self.gh.on_comment = other_session
        self.assertTrue(tr.claim(self.gh, T, 1, "03")["claimed"])
        self.assertEqual(tr.claim_winner(self.gh.comments(1)), "acme-03")

    def test_release_then_reclaim(self):
        tr.claim(self.gh, T, 1, "03")
        tr.set_status(self.gh, T, 1, "todo", "03", "handing back", force=True)
        self.assertEqual(tr.holders(T, self.gh.issue(1)), [])
        self.assertTrue(tr.claim(self.gh, T, 1, "05")["claimed"])
        self.assertEqual(tr.claim_winner(self.gh.comments(1)), "acme-05")

    def test_one_active_ticket_per_session(self):
        self.gh.seed(2, "Story two", ["type:story", "status:todo"])
        tr.claim(self.gh, T, 1, "03")
        with self.assertRaisesRegex(tr.Fail, "already holds #1"):
            tr.claim(self.gh, T, 2, "03")

    def test_only_todo_is_claimable(self):
        self.gh.seed(3, "Blocked", ["status:blocked"])
        with self.assertRaisesRegex(tr.Fail, "is blocked"):
            tr.claim(self.gh, T, 3, "03")


class Workflow(unittest.TestCase):
    def setUp(self):
        self.gh = FakeGitHub()
        self.gh.seed(1, "Story", ["type:story", "status:todo"])
        tr.claim(self.gh, T, 1, "03")

    def test_happy_path(self):
        for st in ("testing", "merging"):
            tr.set_status(self.gh, T, 1, st, "03", pr=42)
            self.assertIn("acme-03", self.gh.issue(1)["labels"])  # the session keeps its claim
        tr.set_status(self.gh, T, 1, "merged", "00", pr=42)
        self.assertEqual(tr.holders(T, self.gh.issue(1)), [])  # claim released on merge
        self.assertEqual(self.gh.issue(1)["state"], "open")      # not closed until the coordinator says done
        tr.set_status(self.gh, T, 1, "deployed", "00")
        tr.set_status(self.gh, T, 1, "done", "00", note="reviewed")
        self.assertEqual((self.gh.issue(1)["state"], statuses(self.gh, 1)), ("closed", ["status:done"]))

    def test_testing_can_send_back(self):
        tr.set_status(self.gh, T, 1, "testing", "03")
        tr.set_status(self.gh, T, 1, "in-progress", "03", note="e2e failed")
        self.assertEqual(statuses(self.gh, 1), ["status:in-progress"])

    def test_illegal_transition(self):
        with self.assertRaisesRegex(tr.Fail, "isn't allowed"):
            tr.set_status(self.gh, T, 1, "deployed", "00")

    def test_sessions_cannot_merge_or_close(self):
        tr.set_status(self.gh, T, 1, "merging", "03")
        for st in ("merged", "cancelled"):
            with self.assertRaisesRegex(tr.Fail, "only the coordinator"):
                tr.set_status(self.gh, T, 1, st, "03")

    def test_blocked_needs_note(self):
        with self.assertRaisesRegex(tr.Fail, "--note"):
            tr.set_status(self.gh, T, 1, "blocked", "03")
        tr.set_status(self.gh, T, 1, "blocked", "03", note="waits on #9")
        self.assertIn("acme-03", self.gh.issue(1)["labels"])

    def test_cancel_closes_not_planned(self):
        tr.set_status(self.gh, T, 1, "cancelled", "00")
        i = self.gh._issues[1]
        self.assertEqual((i["state"], i["state_reason"]), ("closed", "not_planned"))

    def test_close_on_merged(self):
        t = {**T, "close_on": "merged"}
        tr.set_status(self.gh, t, 1, "merging", "03")
        tr.set_status(self.gh, t, 1, "merged", "00")
        self.assertEqual(self.gh.issue(1)["state"], "closed")


class Create(unittest.TestCase):
    def test_hierarchy_and_inheritance(self):
        gh = FakeGitHub()
        gh.create_milestone("M1")
        e = tr.create(gh, T, "epic", "Billing", key="billing", milestone="M1")
        s = tr.create(gh, T, "story", "Invoices", epic="billing", feature="invoicing", size="M", priority="p1",
                      depends="#99", lane="4")
        k = tr.create(gh, T, "task", "PDF export", story=s["issue"])
        si, ki = gh.issue(s["issue"]), gh.issue(k["issue"])
        self.assertEqual(si["milestone"], "M1")  # inherited from the epic
        for l in ("type:story", "epic:billing", "feature:invoicing", "size:M", "priority:p1", "lane:04", "status:todo"):
            self.assertIn(l, si["labels"])
        for l in ("type:task", "epic:billing", "feature:invoicing", "lane:04", "priority:p1", "status:todo"):
            self.assertIn(l, ki["labels"])
        self.assertEqual(ki["milestone"], "M1")
        self.assertIn(f"Parent: #{e['issue']}", si["body"])
        self.assertIn("Depends on: #99", si["body"])
        self.assertIn(f"Parent: #{s['issue']}", ki["body"])
        self.assertEqual(gh.subs, [(e["issue"], si["id"]), (s["issue"], ki["id"])])

    def test_label_milestones(self):
        gh = FakeGitHub()
        t = {**T, "milestone_mode": "label"}
        tr.create(gh, t, "epic", "Billing", key="billing", milestone="M6")
        s = tr.create(gh, t, "story", "Invoices", epic="billing")
        self.assertIn("milestone:M6", gh.issue(s["issue"])["labels"])
        self.assertIsNone(gh.issue(s["issue"])["milestone"])

    def test_missing_epic(self):
        with self.assertRaisesRegex(tr.Fail, "no epic issue"):
            tr.create(FakeGitHub(), T, "story", "x", epic="nope")


class Board(unittest.TestCase):
    def test_counts_claims_stale_and_odd(self):
        gh = FakeGitHub()
        gh.seed(1, "a", ["epic:billing", "status:in-progress", "acme-03"], updated_at="2026-11-20T00:00:00Z")
        gh.seed(2, "b", ["epic:billing", "status:todo"])
        gh.seed(3, "c", ["epic:auth", "status:merged", "acme-04"])
        gh.seed(4, "d", ["epic:auth", "status:todo", "status:testing"])
        gh.seed(5, "e", ["epic:auth"], state="closed")
        b = tr.board(gh, T, now=datetime(2026, 11, 26, tzinfo=timezone.utc))
        rows = {r["epic"]: r for r in b["rows"]}
        self.assertEqual((rows["billing"]["in-progress"], rows["billing"]["todo"]), (1, 1))
        self.assertEqual(rows["auth"]["done"], 1)
        self.assertEqual([s["issue"] for s in b["stale"]], [1])
        odd = {o["issue"]: o["why"] for o in b["odd"]}
        self.assertIn("still claimed", odd[3])
        self.assertIn("2 status labels", odd[4])
        self.assertIn("| **all** |", tr.render_board(b))


class DagIntegration(unittest.TestCase):
    CFG = {"session_label_re": r"^acme-(\d+)$"}

    def issue(self, n, labels, body="", state="OPEN"):
        return {"number": n, "title": f"T{n}", "labels": labels, "state": state, "assignees": [], "body": body}

    def test_status_labels_drive_the_queue(self):
        issues = [self.issue(1, ["type:story", "status:merged"]),
                  self.issue(2, ["type:story", "status:todo"], "Depends on: #1"),
                  self.issue(3, ["type:story", "status:testing", "acme-03"]),
                  self.issue(4, ["type:story", "status:blocked"]),
                  self.issue(5, ["type:story", "status:todo"], "Depends on: #3")]
        r = dag.build_queue(dag.stories_from_issues(issues, self.CFG), issues, [], {}, cfg=self.CFG)
        n = r["nodes"]
        self.assertEqual(n["#1"]["status"], "done")
        self.assertTrue(n["#2"]["ready"])
        self.assertEqual((n["#3"]["status"], n["#3"]["claimedBy"]), ("in-progress", ["acme-03"]))
        self.assertFalse(n["#4"]["ready"])
        self.assertEqual(n["#4"]["unmet"][0]["dep"], "status:blocked")
        self.assertFalse(n["#5"]["ready"])
        self.assertIn("🔒 acme-03", dag.render_report(r))

    def test_story_with_tasks_is_a_container(self):
        issues = [self.issue(1, ["type:story"], "Depends on: #9"),
                  self.issue(2, ["type:task"], "Parent: #1"),
                  self.issue(3, ["type:task"], "Parent: #1\nDepends on: #2"),
                  self.issue(9, ["type:story"]),
                  self.issue(4, ["type:story"], "Depends on: #1")]
        r = dag.build_queue(dag.stories_from_issues(issues), issues, [], {})
        n = r["nodes"]
        self.assertTrue(n["#1"]["container"])
        self.assertFalse(n["#1"]["ready"])
        self.assertFalse(n["#2"]["ready"])  # inherits the story's dependency on #9
        self.assertEqual(sorted(n["#2"]["dependsOn"]), ["#9"])
        self.assertEqual(n["#9"]["weight"], 4)  # #2, #3, the story #1 and #4 all wait on #9
        self.assertEqual([i for i, x in n.items() if x["ready"]], ["#9"])

    def test_container_done_when_tasks_done(self):
        issues = [self.issue(1, ["type:story"]),
                  self.issue(2, ["type:task", "status:done"], "Parent: #1", state="CLOSED"),
                  self.issue(3, ["type:story"], "Depends on: #1")]
        r = dag.build_queue(dag.stories_from_issues(issues), issues, [], {})
        self.assertEqual(r["nodes"]["#1"]["status"], "done")
        self.assertTrue(r["nodes"]["#3"]["ready"])

    def test_epic_slug_dependency(self):
        issues = [self.issue(1, ["type:story", "epic:billing"]),
                  self.issue(2, ["type:story"], "Depends on: epic:billing")]
        r = dag.build_queue(dag.stories_from_issues(issues), issues, [], {})
        self.assertEqual(r["nodes"]["#2"]["dependsOn"], ["#1"])


class Cleanup(unittest.TestCase):
    def git(self, *a, cwd):
        return subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        self.origin, self.main = os.path.join(d, "origin.git"), os.path.join(d, "acme")
        self.git("init", "-q", "--bare", "-b", "integration", self.origin, cwd=d)
        self.git("clone", "-q", self.origin, self.main, cwd=d)
        for k, v in (("user.email", "t@example.com"), ("user.name", "t")):
            self.git("config", k, v, cwd=self.main)
        self.git("switch", "-q", "-c", "integration", cwd=self.main)
        self.git("commit", "-q", "--allow-empty", "-m", "init", cwd=self.main)
        self.git("push", "-q", "origin", "integration", cwd=self.main)
        self.wt = os.path.join(d, "acme-03")
        self.git("worktree", "add", "-q", "-b", "e1-s1", self.wt, "origin/integration", cwd=self.main)
        self.git("commit", "-q", "--allow-empty", "-m", "work", cwd=self.wt)

    def tearDown(self):
        self.tmp.cleanup()

    def test_refuses_dirty_or_unmerged(self):
        with open(os.path.join(self.wt, "x.txt"), "w") as fh:
            fh.write("wip")
        with self.assertRaisesRegex(tr.Fail, "uncommitted"):
            tr.cleanup(self.wt, "integration", merged_check=lambda b, c: True)
        os.remove(os.path.join(self.wt, "x.txt"))
        with self.assertRaisesRegex(tr.Fail, "no merged PR"):
            tr.cleanup(self.wt, "integration", merged_check=lambda b, c: False)

    def test_reset_keeps_worktree(self):
        r = tr.cleanup(self.wt, "integration", merged_check=lambda b, c: True)
        self.assertEqual(r["branch"], "e1-s1")
        self.assertEqual(self.git("branch", "--show-current", cwd=self.wt), "")  # detached at origin/integration
        self.assertNotIn("e1-s1", self.git("branch", "--list", "e1-s1", cwd=self.main))

    def test_remove_worktree(self):
        tr.cleanup(self.wt, "integration", remove=True, merged_check=lambda b, c: True)
        self.assertFalse(os.path.exists(self.wt))
        with self.assertRaisesRegex(tr.Fail, "main checkout"):
            tr.cleanup(self.main, "integration", remove=True, merged_check=lambda b, c: True)


if __name__ == "__main__":
    unittest.main()
