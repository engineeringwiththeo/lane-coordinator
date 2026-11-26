"""Release flow tests against an in-memory GitHub. Run: python3 -I -m unittest discover -s tests"""
import os, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "skills", "lane-coordinator", "scripts"))
sys.path.insert(0, HERE)
import release as rel  # noqa: E402
import tracker as tr  # noqa: E402
from fake_gh import FakeGitHub  # noqa: E402

T = tr.tracker_cfg({"tracker": {"project": "acme"}}, "/x/acme")
R = dict(rel.DEFAULT_RELEASE)


class Versions(unittest.TestCase):
    def test_parse_and_pending(self):
        names = ["v1.2.0", "v1.3.0-rc.1", "v1.3.0-rc.2", "v1.10.0", "x1.0.0", "v2.0.0-beta"]
        prod, rcs = rel.parse_tags(names)
        self.assertEqual(prod, [(1, 2, 0), (1, 10, 0)])  # numeric, not string, order
        self.assertEqual(rcs, {(1, 3, 0): [1, 2]})
        self.assertEqual(rel.pending_version(names, "minor"), (1, 11, 0))  # 1.3 rcs are older than 1.10

    def test_first_release_and_bumps(self):
        self.assertEqual(rel.pending_version([], "minor"), (0, 1, 0))
        self.assertEqual(rel.pending_version(["v1.4.2"], "patch"), (1, 4, 3))
        self.assertEqual(rel.pending_version(["v1.4.2"], "major"), (2, 0, 0))

    def test_rejected_candidate_reuses_version(self):
        names = ["v1.4.0", "v1.5.0-rc.1"]
        self.assertEqual(rel.pending_version(names, "minor"), (1, 5, 0))
        self.assertEqual(rel.next_rc(names, (1, 5, 0)), 2)
        self.assertEqual(rel.next_rc(names, (1, 6, 0)), 1)


class Flow(unittest.TestCase):
    def setUp(self):
        self.gh = gh = FakeGitHub()
        gh._tags = {"v1.4.0": "sha-old"}
        gh.ahead_by = 3
        gh.seed(10, "Invoices", ["type:story", "epic:billing", "feature:invoicing", "status:merged"], milestone="M2")
        gh.seed(11, "Login", ["type:story", "epic:auth", "status:merged"], milestone="M1")
        gh.seed(12, "Still in review", ["type:story", "epic:auth", "status:merging", "acme-03"])

    def test_plan_and_promote(self):
        p = rel.plan(self.gh, T, R, "integration")
        self.assertEqual(p["version"], "v1.5.0")
        self.assertEqual([r["issue"] for r in p["tickets"]], [11, 10])  # grouped by milestone, M1 first
        r = rel.promote(self.gh, T, R, "integration")
        pr = self.gh.pr(r["pr"])
        self.assertEqual((pr["title"], pr["base"], pr["head"]), ("Release v1.5.0", "main", "integration"))
        self.assertIn("### M2", pr["body"])
        self.assertIn("- #10 Invoices (billing · invoicing)", pr["body"])
        self.assertIn("Refs #10", pr["body"])
        self.assertNotIn("Closes", pr["body"])
        self.assertNotIn("#12", pr["body"])
        again = rel.promote(self.gh, T, R, "integration")  # refreshes the open PR instead of opening a second
        self.assertEqual((again["pr"], again.get("updated")), (r["pr"], True))

    def test_nothing_to_promote(self):
        self.gh.ahead_by = 0
        with self.assertRaisesRegex(rel.Fail, "nothing"):
            rel.promote(self.gh, T, R, "integration")

    def test_staging_then_prod(self):
        n = rel.promote(self.gh, T, R, "integration")["pr"]
        with self.assertRaisesRegex(rel.Fail, "isn't merged"):
            rel.staging(self.gh, T, R, n)
        self.gh.merge_pr(n, "sha-rc1")
        s = rel.staging(self.gh, T, R, n)
        self.assertEqual((s["tag"], s["sha"], sorted(s["staged"]), s["warnings"]), ("v1.5.0-rc.1", "sha-rc1", [10, 11], []))
        self.assertTrue(self.gh.releases[-1]["prerelease"])
        for i in (10, 11):
            labels = self.gh.issue(i)["labels"]
            self.assertIn("status:staging", labels)
            self.assertIn("release:v1.5.0", labels)
        self.assertEqual(rel.status(self.gh, T, R)["staging"], "v1.5.0-rc.1")

        # Staging found a bug: fixed forward on integration and promoted again → rc.2 of the same version.
        self.gh.seed(13, "Fix invoice rounding", ["type:task", "epic:billing", "status:merged"], milestone="M2")
        n2 = rel.promote(self.gh, T, R, "integration")["pr"]
        self.assertEqual(self.gh.pr(n2)["title"], "Release v1.5.0")
        self.gh.merge_pr(n2, "sha-rc2")
        self.assertEqual(rel.staging(self.gh, T, R, n2)["tag"], "v1.5.0-rc.2")

        p = rel.prod(self.gh, T, R)
        self.assertEqual((p["tag"], p["from"], p["sha"]), ("v1.5.0", "v1.5.0-rc.2", "sha-rc2"))
        self.assertEqual(sorted(p["deployed"]), [10, 11, 13])
        self.assertEqual(p["done"], [])
        self.assertTrue(self.gh.releases[-1]["latest"])
        self.assertFalse(self.gh.releases[-1]["prerelease"])
        self.assertEqual(self.gh.issue(10)["state"], "open")  # closing waits for the coordinator's review
        self.assertEqual(rel.status(self.gh, T, R)["production"], "v1.5.0")
        with self.assertRaisesRegex(rel.Fail, "no staged release candidate"):
            rel.prod(self.gh, T, R)

    def test_prod_done_closes(self):
        n = rel.promote(self.gh, T, R, "integration")["pr"]
        self.gh.merge_pr(n, "sha1")
        rel.staging(self.gh, T, R, n)
        p = rel.prod(self.gh, T, R, done=True)
        self.assertEqual(sorted(p["done"]), [10, 11])
        self.assertEqual(self.gh.issue(10)["state"], "closed")

    def test_squash_merge_warns(self):
        n = rel.promote(self.gh, T, R, "integration")["pr"]
        self.gh.merge_pr(n, "sha-squash", parents=1)
        self.assertIn("merge commit", rel.staging(self.gh, T, R, n)["warnings"][0])

    def test_hooks_run_with_tag(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "deployed.txt")
            cfg = {**R, "staging_cmd": f"echo {{tag}} {{sha}} > {out}"}
            n = rel.promote(self.gh, T, cfg, "integration")["pr"]
            self.gh.merge_pr(n, "sha9")
            rel.staging(self.gh, T, cfg, n, root=d)
            with open(out) as fh:
                self.assertEqual(fh.read().strip(), "v1.5.0-rc.1 sha9")
            bad = {**R, "prod_cmd": "exit 3"}
            with self.assertRaisesRegex(rel.Fail, "hook failed"):
                rel.prod(self.gh, T, bad, root=d)


class Workflow(unittest.TestCase):
    def test_staging_status_rules(self):
        gh = FakeGitHub()
        gh.seed(1, "x", ["status:merged"])
        with self.assertRaisesRegex(tr.Fail, "only the coordinator"):
            tr.set_status(gh, T, 1, "staging", "03")
        tr.set_status(gh, T, 1, "staging", "00")
        tr.set_status(gh, T, 1, "deployed", "00")
        self.assertEqual(gh.issue(1)["state"], "open")


if __name__ == "__main__":
    unittest.main()
