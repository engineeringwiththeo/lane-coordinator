"""Tracker → DAG → claim, end to end on an in-memory GitHub: the planner reads exactly what the tracker writes."""
import os, subprocess, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(HERE, "..", "skills", "lane-coordinator", "scripts")
sys.path.insert(0, SCRIPTS)
sys.path.insert(0, HERE)
import dag  # noqa: E402
import tracker as tr  # noqa: E402
import work_queue as wq  # noqa: E402
from fake_gh import FakeGitHub  # noqa: E402


class TrackerDrivesThePlanner(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home, self.old_home = self.tmp.name, os.environ.get("HOME")
        os.environ["HOME"] = self.home  # state dir under a throwaway ~/.claude
        self.repo = os.path.join(self.tmp.name, "acme")
        subprocess.run(["git", "init", "-q", self.repo], check=True)
        subprocess.run(["bash", os.path.join(SCRIPTS, "init.sh"), self.repo], check=True, stdout=subprocess.DEVNULL)
        self.t = tr.tracker_cfg({"tracker": {"project": "acme"}}, self.repo)

        gh = self.gh = FakeGitHub()
        gh.create_milestone("M1 — Foundation")
        gh.create_milestone("M2 — Billing")
        new = lambda kind, title, **kw: tr.create(gh, self.t, kind, title, **kw)["issue"]
        self.auth = new("epic", "Accounts and sign-in", key="auth", milestone="M1 — Foundation")
        self.billing = new("epic", "Billing", key="billing", milestone="M2 — Billing")
        gh.add_labels(self.billing, ["lane:02"])  # the epic issue owns lane 02; its stories inherit it
        self.A = new("story", "Sign-up", epic="auth", lane="01", size="M", priority="p1")
        self.B = new("story", "Plans table", epic="billing", size="S", priority="p2")
        self.C = new("story", "Invoices", epic="billing", size="L", priority="p1")
        self.C1 = new("task", "Invoice model", story=self.C, size="S")
        self.C2 = new("task", "Invoice PDF", story=self.C, size="M", depends=f"#{self.C1}")
        # An M1 story that waits on the M2 ticket B: B must be planned as M1 work.
        self.D = new("story", "Plan picker at sign-up", epic="auth", lane="01", depends=f"#{self.B}")
        self.E = new("story", "Launch checklist", epic="billing", depends="milestone:M1")

    def tearDown(self):
        if self.old_home is not None:
            os.environ["HOME"] = self.old_home
        self.tmp.cleanup()

    def plan(self, **kw):
        return wq.compute(self.repo, data=wq.data_from_issues(self.gh.issues(state="all")), **kw)[0]

    def test_hierarchy_milestones_and_lanes(self):
        r = self.plan()
        n = lambda i: r["nodes"][f"#{i}"]
        self.assertNotIn(f"#{self.auth}", r["nodes"])  # epics are not work items
        self.assertTrue(n(self.C)["container"])
        self.assertTrue(n(self.C1)["ready"])
        self.assertFalse(n(self.C2)["ready"])
        self.assertEqual((n(self.B)["lane"], n(self.B)["laneSource"]), ("02", "epic-issue"))
        self.assertEqual(n(self.C1)["lane"], "02")  # task copied nothing, but inherits through its epic
        self.assertEqual(n(self.B)["milestone"], "M2 — Billing")
        self.assertEqual(n(self.B)["msRank"], 0)  # promoted: it blocks D in M1
        self.assertEqual(n(self.C1)["msRank"], 1)
        self.assertFalse(n(self.E)["ready"])
        self.assertEqual(n(self.E)["unmet"][0]["dep"], "milestone:M1")
        self.assertEqual(sorted(n(self.E)["dependsOn"]), sorted([f"#{self.A}", f"#{self.D}"]))
        ready = [x["id"] for x in sorted((x for x in r["nodes"].values() if x["ready"]), key=dag.score_key)]
        self.assertEqual(ready[:2], [f"#{self.B}", f"#{self.A}"])  # M1 work (incl. B) before M2 work
        report = dag.render_report(r)
        self.assertIn("## Milestones", report)
        self.assertIn("| M1 — Foundation |", report)

    def test_claims_and_statuses_flow_back_into_the_plan(self):
        data = lambda: wq.data_from_issues(self.gh.issues(state="all"))
        got = tr.next_ticket(self.gh, self.t, "02", self.repo, data=data())
        self.assertEqual((got["claimed"], got["issue"]), (True, self.B))
        r = self.plan()
        self.assertEqual((r["nodes"][f"#{self.B}"]["status"], r["nodes"][f"#{self.B}"]["claimedBy"]),
                         ("in-progress", ["acme-02"]))
        # One ticket per session: 02 can't take another until B moves on.
        self.assertFalse(tr.next_ticket(self.gh, self.t, "02", self.repo, data=data())["claimed"])
        # Session 03 (no lane of its own) may borrow only from paused lanes; with lane 02 paused and the focus on
        # M2, it gets the invoice model task. While lane 02 is active, its work stays with it.
        self.assertFalse(tr.next_ticket(self.gh, self.t, "03", self.repo, data=data(), milestone="M2")["claimed"])
        got = tr.next_ticket(self.gh, self.t, "03", self.repo, lanes=["01", "03"], data=data(), milestone="M2")
        self.assertEqual(got["issue"], self.C1)
        # B goes through review and is merged by the coordinator → D becomes ready.
        for st, who in (("testing", "02"), ("merging", "02"), ("merged", "00")):
            tr.set_status(self.gh, self.t, self.B, st, who)
        r = self.plan()
        self.assertEqual(r["nodes"][f"#{self.B}"]["status"], "done")
        self.assertTrue(r["nodes"][f"#{self.D}"]["ready"])
        # A blocked label keeps a ticket out of the queue.
        tr.set_status(self.gh, self.t, self.D, "blocked", "00", note="waits on legal copy")
        self.assertFalse(self.plan()["nodes"][f"#{self.D}"]["ready"])

    def test_focus_limits_assignments(self):
        r = self.plan(focus="M1")
        self.assertEqual(set(r["focus"]) >= {f"#{self.A}", f"#{self.B}", f"#{self.D}"}, True)
        self.assertNotIn(f"#{self.C1}", r["focus"])
        assigned = {a["id"] for a in r["assignments"] if a["id"]}
        self.assertNotIn(f"#{self.C1}", assigned)


if __name__ == "__main__":
    unittest.main()
