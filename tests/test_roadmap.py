"""Roadmap tests: the file, sync to GitHub, and the status view over tickets, the DAG, ADRs and releases."""
import json, os, subprocess, sys, tempfile, unittest
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(HERE, "..", "skills", "lane-coordinator", "scripts")
sys.path.insert(0, SCRIPTS)
sys.path.insert(0, HERE)
import adr  # noqa: E402
import roadmap as rmod  # noqa: E402
import tracker as tr  # noqa: E402
import work_queue as wq  # noqa: E402
from fake_gh import FakeGitHub  # noqa: E402

ROADMAP = """# Roadmap

<!-- comments are ignored
## Not a milestone
-->

## M1 — Foundation
Due: 2027-01-31
Goal: Members can sign up.
Exit: Sign-up works in staging.

### auth — Accounts and sign-in
Lane: 1
Depends on: ADR-0001
Email sign-up and sessions.

## M2 — Billing
Due: 2027-03-15
Goal: Members can pay.

### billing — Payments
Lane: 02
"""


class File(unittest.TestCase):
    def test_parse(self):
        rm, problems = rmod.parse(ROADMAP)
        self.assertEqual(problems, [])
        self.assertEqual([m["title"] for m in rm], ["M1 — Foundation", "M2 — Billing"])
        m1 = rm[0]
        self.assertEqual((m1["due"], m1["goal"], m1["exit"]), ("2027-01-31", "Members can sign up.", "Sign-up works in staging."))
        e = m1["epics"][0]
        self.assertEqual((e["key"], e["title"], e["lane"], e["depends"], e["body"]),
                         ("auth", "Accounts and sign-in", "01", "ADR-0001", "Email sign-up and sessions."))
        self.assertEqual(rmod.epic_body(e), "Email sign-up and sessions.\n\nDepends on: ADR-0001")

    def test_problems(self):
        _, problems = rmod.parse("### early — x\n## M1\nDue: soon\n### Bad heading\n### a — A\n## M2\n### a — again\n## M1\n")
        whats = " | ".join(p["what"] for p in problems)
        for needle in ("before any milestone", "YYYY-MM-DD", "### key — Title", "epic key a", "appears twice"):
            self.assertIn(needle, whats)

    def test_template_parses(self):
        with open(rmod.TEMPLATE, encoding="utf-8") as fh:
            rm, problems = rmod.parse(fh.read())
        self.assertEqual(problems, [])
        self.assertEqual(len(rm), 2)


class Sync(unittest.TestCase):
    T = tr.tracker_cfg({"tracker": {"project": "acme"}}, "/x/acme")

    def test_plan_apply_and_rerun_is_clean(self):
        gh = FakeGitHub()
        gh.create_milestone("M1 — Foundation", "2026-12-01", "old goal")  # exists with a stale date
        gh.create_milestone("Sprint 7")                                   # not on the roadmap
        old = tr.create(gh, self.T, "epic", "Old billing", key="billing", milestone="M1 — Foundation")["issue"]
        tr.create(gh, self.T, "epic", "Legacy", key="legacy")
        rm, _ = rmod.parse(ROADMAP)
        p = rmod.plan_sync(gh, self.T, rm)
        kinds = [a["do"] for a in p["actions"]]
        self.assertEqual(kinds.count("create_milestone"), 1)            # M2
        self.assertIn("update_milestone", kinds)                         # M1's due date and goal
        self.assertIn("create_epic", kinds)                              # auth
        self.assertIn({"do": "move_epic", "issue": old, "key": "billing", "from": "M1 — Foundation",
                       "to": "M2 — Billing"}, p["actions"])
        self.assertTrue(any("Legacy" in d or "legacy" in d for d in p["drift"]))
        self.assertTrue(any("Sprint 7" in d for d in p["drift"]))
        self.assertTrue(any("Old billing" in d for d in p["drift"]))     # title differs: reported, not changed

        rmod.apply_sync(gh, self.T, p)
        ms = {m["title"]: m for m in gh.milestones_full()}
        self.assertEqual(ms["M1 — Foundation"]["due_on"], "2027-01-31")
        self.assertEqual(ms["M1 — Foundation"]["description"], "Members can sign up.\n\nExit criteria: Sign-up works in staging.")
        self.assertEqual(gh.issue(old)["milestone"], "M2 — Billing")
        self.assertIn("lane:02", gh.issue(old)["labels"])
        auth = gh.issues(state="all", labels=["type:epic", "epic:auth"])[0]
        self.assertEqual((auth["milestone"], "lane:01" in auth["labels"]), ("M1 — Foundation", True))
        self.assertIn("Depends on: ADR-0001", auth["body"])
        self.assertEqual(rmod.plan_sync(gh, self.T, rm)["actions"], [])  # idempotent

    def test_closes_shipped_milestone(self):
        gh = FakeGitHub()
        rm, _ = rmod.parse(ROADMAP)
        rmod.apply_sync(gh, self.T, rmod.plan_sync(gh, self.T, rm))
        s = tr.create(gh, self.T, "story", "Sign-up", epic="auth")["issue"]
        self.assertFalse(any(a["do"] == "close_milestone" for a in rmod.plan_sync(gh, self.T, rm)["actions"]))
        gh.add_labels(s, ["status:deployed"]); gh.remove_label(s, "status:todo")
        closing = [a for a in rmod.plan_sync(gh, self.T, rm)["actions"] if a["do"] == "close_milestone"]
        self.assertEqual([a["title"] for a in closing], ["M1 — Foundation"])


class Status(unittest.TestCase):
    """One view over tickets, the DAG, ADRs, releases and the ledger."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_home = os.environ.get("HOME")
        os.environ["HOME"] = self.tmp.name
        self.repo = os.path.join(self.tmp.name, "acme")
        subprocess.run(["git", "init", "-q", self.repo], check=True)
        subprocess.run(["bash", os.path.join(SCRIPTS, "init.sh"), self.repo], check=True, stdout=subprocess.DEVNULL)
        os.makedirs(os.path.join(self.repo, "docs"))
        with open(os.path.join(self.repo, "docs", "roadmap.md"), "w") as fh:
            # The repo's roadmap puts Billing first, although Foundation is due earlier: roadmap order must win.
            m1, m2 = ROADMAP.split("## M2 — Billing")
            fh.write("# Roadmap\n\n## M2 — Billing" + m2 + "\n" + m1.split("# Roadmap", 1)[1])
        self.d = os.path.join(self.repo, "docs", "adr")
        adr.new(self.d, "Auth provider")  # ADR-0001, proposed
        self.t = tr.tracker_cfg({"tracker": {"project": "acme"}}, self.repo)
        gh = self.gh = FakeGitHub()
        rm, _ = rmod.parse(ROADMAP)
        rmod.apply_sync(gh, self.t, rmod.plan_sync(gh, self.t, rm))
        story = lambda title, epic, **kw: tr.create(gh, self.t, "story", title, epic=epic, **kw)["issue"]
        self.a1 = story("Sign-up", "auth", depends="ADR-0001")
        self.a2 = story("Sessions", "auth", depends=f"#{self.a1}")
        self.b1 = story("Plans", "billing")
        self.b2 = story("Checkout", "billing")
        for n, st, rel in ((self.b1, "deployed", "v1.0.0"), (self.b2, "staging", "v1.1.0")):
            gh.remove_label(n, "status:todo"); gh.add_labels(n, [f"status:{st}", f"release:{rel}"])
        self.stray = gh.seed(99, "Unplanned bug", ["type:story", "epic:misc"])["number"]

    def tearDown(self):
        if self.old_home is not None:
            os.environ["HOME"] = self.old_home
        self.tmp.cleanup()

    def run_status(self, ledger_rows=0, today=date(2026, 11, 26)):
        data = wq.data_from_issues(self.gh.issues(state="all"))
        r, data = wq.compute(self.repo, data=data)
        ledger = [{"kind": "ticket", "date": (today.fromordinal(today.toordinal() - 6)).isoformat()}] * ledger_rows
        rm, _ = rmod.parse(ROADMAP)
        return r, rmod.status(rm, r, data, adr.scan(self.d), ["v1.0.0"], ledger, today=today, lanes=["01", "02"])

    def test_roadmap_order_ranks_the_planner(self):
        b3 = tr.create(self.gh, self.t, "story", "Refunds", epic="billing")["issue"]
        r, _ = self.run_status()
        self.assertEqual(r["nodes"][f"#{b3}"]["msRank"], 0)        # M2 is first in the repo's roadmap
        self.assertEqual(r["nodes"][f"#{self.a1}"]["msRank"], 1)   # M1, despite its earlier due date
        self.assertEqual([m["milestone"] for m in r["milestones"]][:2], ["M2 — Billing", "M1 — Foundation"])

    def test_view(self):
        _, s = self.run_status(ledger_rows=8)  # 2 tickets/week
        m1, m2 = s["milestones"]
        self.assertEqual((m1["total"], m1["done"], m1["blocked"]), (2, 0, 2))
        self.assertEqual(m1["decisions"], [{"adr": "ADR-0001", "title": "Auth provider", "blocks": 2}])
        self.assertEqual(m1["epics"][0]["key"], "auth")
        self.assertEqual((m2["done"], m2["in_production"], m2["on_staging"]), (2, 1, 1))
        self.assertEqual(m2["releases"], ["v1.0.0", "v1.1.0"])
        self.assertEqual(m2["state"], "complete")
        self.assertEqual(m1["state"], "on track")  # 2 left at 2/week, chain 2 → ~2 weeks, due 2027-01-31
        self.assertEqual(s["unplanned"], ["#99"])
        self.assertEqual(s["epics_off_roadmap"], ["misc"])
        page = rmod.render_status(s)
        for needle in ("| M1 — Foundation | 2027-01-31 | 0/2 (0%)", "ADR-0001 Auth provider (blocks 2)",
                       "1 prod · 1 staging", "✅ complete", "## Not on any roadmap milestone"):
            self.assertIn(needle, page)

    def test_at_risk_and_no_data(self):
        _, s = self.run_status(ledger_rows=1, today=date(2027, 1, 20))  # 0.25/week: weeks to finish
        self.assertEqual(s["milestones"][0]["state"], "at risk")
        _, s = self.run_status(ledger_rows=0)
        self.assertIn("no forecast", s["milestones"][0]["state"])


if __name__ == "__main__":
    unittest.main()
