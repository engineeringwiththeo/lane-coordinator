"""ADR workflow tests. Run: python3 -I -m unittest discover -s tests"""
import os, subprocess, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(HERE, "..", "skills", "lane-coordinator", "scripts")
sys.path.insert(0, SCRIPTS)
sys.path.insert(0, HERE)
import adr  # noqa: E402
import dag  # noqa: E402
import tracker as tr  # noqa: E402
import work_queue as wq  # noqa: E402
from fake_gh import FakeGitHub  # noqa: E402


class Files(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = os.path.join(self.tmp.name, "docs", "adr")

    def tearDown(self):
        self.tmp.cleanup()

    def read(self, path):
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    def test_init_new_and_numbering(self):
        self.assertTrue(adr.init(self.d)["created"])
        self.assertFalse(adr.init(self.d)["created"])
        a = adr.new(self.d, "Use Postgres row-level security", issue=12, owner="the owner", today="2026-11-26")
        b = adr.new(self.d, "Money is integer cents", relates="ADR-0001", today="2026-11-26")
        self.assertEqual((a["adr"], b["adr"]), ("ADR-0001", "ADR-0002"))
        self.assertTrue(a["path"].endswith("0001-use-postgres-row-level-security.md"))
        src = self.read(a["path"])
        self.assertTrue(src.startswith("# ADR-0001: Use Postgres row-level security\n"))
        self.assertIn("- **Status:** Proposed — 2026-11-26\n- **Decision issue:** #12\n- **Decision owner:** the owner", src)
        self.assertIn("## Decision", src)
        rows = adr.scan(self.d)
        self.assertEqual([(r["num"], r["status"]) for r in rows], [("0001", "proposed"), ("0002", "proposed")])
        self.assertEqual(dag.adr_status_from_file(src), "Proposed — 2026-11-26")

    def test_accept_and_supersede(self):
        adr.new(self.d, "Queue on Redis", today="2026-11-01")
        adr.new(self.d, "Queue on Postgres", today="2026-11-20")
        r = adr.set_status(self.d, "1", "accepted", note="Owner sign-off", today="2026-11-02")
        self.assertEqual((r["from"], r["to"]), ("proposed", "accepted"))
        self.assertTrue(dag.is_accepted(dag.adr_status_from_file(self.read(adr.find(adr.scan(self.d), 1)["path"]))))
        adr.set_status(self.d, "1", "superseded", by="2", today="2026-11-26")
        rows = {r["num"]: r for r in adr.scan(self.d)}
        self.assertEqual(rows["0001"]["status"], "superseded")
        self.assertIn("Superseded by ADR-0002", rows["0001"]["raw"])
        self.assertIn("- **Supersedes:** ADR-0001", self.read(rows["0002"]["path"]))
        with self.assertRaisesRegex(adr.Fail, "--by"):
            adr.set_status(self.d, "2", "superseded")

    def test_classify_existing_styles(self):  # status lines as written in real projects
        for raw, want in (("Accepted — 2026-11-22, by the owner", "accepted"), ("Accepted in direction", "accepted"),
                          ("Proposed — needs decision. Decision issue: [#697]", "proposed"),
                          ("Superseded by [ADR-0075](0075-x.md)", "superseded"), ("Deprecated", "deprecated")):
            self.assertEqual(adr.classify(raw), want, raw)


class Coordinator(unittest.TestCase):
    """ADRs gate work in the DAG, rank the decisions to chase, feed briefs and gate PR review."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_home = os.environ.get("HOME")
        os.environ["HOME"] = self.tmp.name
        self.repo = os.path.join(self.tmp.name, "acme")
        subprocess.run(["git", "init", "-q", self.repo], check=True)
        subprocess.run(["bash", os.path.join(SCRIPTS, "init.sh"), self.repo], check=True, stdout=subprocess.DEVNULL)
        self.d = os.path.join(self.repo, "docs", "adr")
        adr.new(self.d, "Billing provider", today="2026-11-01")       # 0001 proposed
        adr.new(self.d, "Tenancy model", today="2026-11-01")          # 0002 accepted
        adr.set_status(self.d, "2", "accepted", today="2026-11-02")
        self.t = tr.tracker_cfg({"tracker": {"project": "acme"}}, self.repo)
        gh = self.gh = FakeGitHub()
        new = lambda kind, title, **kw: tr.create(gh, self.t, kind, title, **kw)["issue"]
        self.epic = new("epic", "Billing", key="billing", body="Built on ADR-0002.")
        self.S1 = new("story", "Charge cards", epic="billing", depends="ADR-0001")
        self.S2 = new("story", "Receipts", epic="billing", depends=f"#{self.S1}")
        self.S3 = new("story", "Org settings", epic="billing", depends="ADR-0002")
        self.T1 = new("task", "Card form", story=self.S1)

    def tearDown(self):
        if self.old_home is not None:
            os.environ["HOME"] = self.old_home
        self.tmp.cleanup()

    def plan(self):
        return wq.compute(self.repo, data=wq.data_from_issues(self.gh.issues(state="all")))[0]

    def test_proposed_adr_blocks_and_ranks(self):
        r = self.plan()
        n = r["nodes"]
        self.assertFalse(n[f"#{self.T1}"]["ready"])  # the task inherits its story's ADR dependency
        self.assertEqual(n[f"#{self.T1}"]["unmet"][0], {"dep": "ADR-0001", "why": "Proposed", "storyIds": []})
        self.assertTrue(n[f"#{self.S3}"]["ready"])   # ADR-0002 is accepted
        p = adr.pending(adr.scan(self.d), r)
        self.assertEqual(p[0]["adr"], "ADR-0001")
        self.assertEqual(p[0]["blocks_direct"], 2)   # the story and its task
        self.assertEqual(p[0]["blocks_total"], 3)    # … and Receipts downstream
        adr.set_status(self.d, "1", "accepted")
        self.assertTrue(self.plan()["nodes"][f"#{self.T1}"]["ready"])
        self.assertEqual(adr.pending(adr.scan(self.d), self.plan()), [])

    def test_brief_lists_adrs_from_ticket_story_and_epic(self):
        b = adr.for_ticket(self.gh, adr.scan(self.d), self.T1)
        self.assertEqual(b["read_from"], [f"#{self.T1}", f"#{self.S1}", f"#{self.epic}"])
        self.assertEqual([(x["adr"], x["status"]) for x in b["adrs"]], [("ADR-0001", "proposed"), ("ADR-0002", "accepted")])
        self.assertEqual(b["warnings"], ["ADR-0001 is proposed"])

    def test_review_gate(self):
        self.gh.prs[50] = {"number": 50, "title": "Org settings", "body": f"Refs #{self.S3}", "state": "open",
                           "merged": False, "base": "integration", "head": "s3", "files": [
                               {"filename": "src/settings.ts", "patch": "+x"}]}
        ok = adr.check_pr(self.gh, adr.scan(self.d), 50, "docs/adr")
        self.assertTrue(ok["ok"])
        self.assertIn("follows ADR-0002", ok["notes"][0])

        self.gh.prs[51] = {"number": 51, "title": "Cards", "body": f"Refs #{self.S1}", "state": "open", "merged": False,
                           "base": "integration", "head": "s1", "files": [
                               {"filename": "docs/adr/0001-billing-provider.md",
                                "patch": "-- **Status:** Proposed — 2026-11-01\n+- **Status:** Accepted — 2026-11-26"},
                               {"filename": "docs/adr/0003-retry-policy.md",
                                "patch": "+# ADR-0003: Retry policy\n+- **Status:** Proposed — 2026-11-26"}]}
        bad = adr.check_pr(self.gh, adr.scan(self.d), 51, "docs/adr")
        self.assertFalse(bad["ok"])
        self.assertTrue(any("changes the Status of ADR-0001" in p for p in bad["problems"]))
        self.assertTrue(any("ADR-0001" in p and "proposed" in p for p in bad["problems"]))
        self.assertIn("proposes ADR-0003", bad["notes"])
        own = adr.check_pr(self.gh, adr.scan(self.d), 51, "docs/adr", coordinator=True)
        self.assertFalse(any("changes the Status" in p for p in own["problems"]))

    def test_only_coordinator_decides(self):
        with self.assertRaises(SystemExit) as cm:
            env = os.environ.get("LANE_SESSION")
            os.environ["LANE_SESSION"] = "03"
            try:
                adr.main(["--repo", self.repo, "status", "0001", "accepted"])
            except adr.Fail as e:
                raise SystemExit(str(e))
            finally:
                if env is None:
                    os.environ.pop("LANE_SESSION", None)
                else:
                    os.environ["LANE_SESSION"] = env
        self.assertIn("only the coordinator", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
