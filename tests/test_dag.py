"""Unit tests for the DAG work queue. Run: python3 -I -m unittest discover -s tests (from the repo root)."""
import json, os, subprocess, sys, tempfile, unittest

SCRIPTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "skills", "lane-coordinator", "scripts")
sys.path.insert(0, SCRIPTS)
import dag  # noqa: E402


def heading(id_, issue, deps, extra="size:M · priority:p1"):
    return f"### {id_} ([#{issue}](../../../issues/{issue})) — Title of {id_} · {extra} · depends on: {deps}"


def issue(number, title, labels=(), state="OPEN", body=""):
    return {"number": number, "title": title, "labels": list(labels), "state": state, "assignees": [], "body": body}


def pr(number, title, body, state="MERGED"):
    return {"number": number, "title": title, "body": body, "state": state,
            "mergedAt": "2026-11-20T00:00:00Z" if state == "MERGED" else None}


def fixture(prs=(), closed=(), lanes=None):
    """A ← B ← D ← E, A ← C ← D (B, C depend on A; D on B and C; E on D); F independent, p0."""
    md = "\n".join([
        heading("E01-S01", 1, "none", "size:L · priority:p2"),
        heading("E01-S02", 2, "E01-S01"),
        heading("E01-S03", 3, "E01-S01"),
        heading("E01-S04", 4, "E01-S02, E01-S03"),
        heading("E01-S05", 5, "E01-S04"),
        heading("E02-S01", 6, "none", "size:S · priority:p0"),
    ])
    stories, _ = dag.parse_backlog(md)
    lanes = lanes or {}
    issues = [issue(s["issue"], f"{s['id']}: x", [f"lane:{lanes[s['issue']]}"] if s["issue"] in lanes else [],
                    "CLOSED" if s["issue"] in closed else "OPEN") for s in stories]
    return dict(stories=stories, issues=issues, prs=list(prs), adrs={})


class Parsing(unittest.TestCase):
    def test_heading(self):
        s, problems = dag.parse_heading("### E16-S58 ([#598](../../../issues/598)) — The vendor portal · size:L · "
                                        "priority:p1 · depends on: E16-S50, E16-S52")
        self.assertEqual(problems, [])
        self.assertEqual((s["id"], s["epic"], s["issue"], s["title"], s["size"], s["priority"]),
                         ("E16-S58", 16, 598, "The vendor portal", "L", 1))
        self.assertEqual([d["story"] for d in s["deps"]], ["E16-S50", "E16-S52"])

    def test_brackets_and_semicolons(self):
        self.assertEqual(dag.split_top_level("E26 (BudgetBar, Tile), ADR-0051; EPIC 29 (a, b)"),
                         ["E26 (BudgetBar, Tile)", "ADR-0051", "EPIC 29 (a, b)"])
        s, _ = dag.parse_heading(heading("E09-S50", 1, "E26 (BudgetBar, Tile), ADR-0048 (decided: decision 5, 2026-11-21)"))
        self.assertEqual([d["kind"] for d in s["deps"]], ["epic", "adr"])

    def test_continuations(self):
        s, _ = dag.parse_heading(heading("E03-S51", 1, "E03-S50, ADR-0055, Conflicts 2, 3, 5 and 6"))
        self.assertEqual([d["raw"] for d in s["deps"]], ["E03-S50", "ADR-0055", "Conflicts 2, 3, 5 and 6"])

    def test_problems(self):
        md = "\n".join(["### E01-S01 Title with no link · size:S", "### Not a story",
                        heading("E01-S02", 2, "none"), heading("E01-S02", 3, "none")])
        stories, problems = dag.parse_backlog(md)
        self.assertEqual([s["id"] for s in stories], ["E01-S01", "E01-S02"])
        whats = "\n".join(p["what"] for p in problems)
        for needle in ("issue link", "no priority", "duplicate story id"):
            self.assertIn(needle, whats)


class Deps(unittest.TestCase):
    def test_kinds(self):
        cases = {"E11-S52 (notice periods)": ("story", "story", "E11-S52"), "ADR-0086 §5": ("adr", "adr", "0086"),
                 "EPIC 33 (new plan keys)": ("epic", "epic", 33), "EPIC 2": ("epic", "epic", 2),
                 "E26 (TemplateCard port)": ("epic", "epic", 26), "#211": ("issue", "issue", 211)}
        for raw, (kind, key, val) in cases.items():
            d = dag.parse_dep(raw)
            self.assertEqual((d["kind"], d[key]), (kind, val), raw)
        for raw in ("the motions module", "Conflict 2", "EPIC 16 extension (approvals)", "EPIC 22's AI-provider ADR"):
            self.assertEqual(dag.parse_dep(raw)["kind"], "free", raw)
        self.assertIsNone(dag.parse_dep("none"))

    def test_adr_status(self):
        self.assertTrue(dag.adr_status_from_file("# ADR\n\n- **Status:** Accepted — 2026-11-22\n").startswith("Accepted"))

    def test_satisfaction(self):
        md = "\n".join([heading("E01-S01", 1, "none"), heading("E02-S01", 2, "ADR-0001"),
                        heading("E02-S02", 3, "ADR-0002 §4"), heading("E02-S03", 4, "EPIC 01"),
                        heading("E02-S04", 5, "the motions module")])
        r = dag.build_queue(dag.parse_backlog(md)[0], [issue(n, f"E0x-S0{n}: x") for n in range(1, 6)], [],
                            {"0001": "Accepted — 2026-11-22", "0002": "Proposed — needs decision"})
        n = r["nodes"]
        self.assertTrue(n["E02-S01"]["ready"])
        self.assertEqual(n["E02-S02"]["unmet"], [{"dep": "ADR-0002", "why": "Proposed", "storyIds": []}])
        self.assertFalse(n["E02-S03"]["ready"])
        self.assertEqual((n["E02-S03"]["unmet"][0]["dep"], n["E02-S03"]["unmet"][0]["storyIds"]), ("EPIC 01", ["E01-S01"]))
        self.assertTrue(n["E02-S04"]["ready"])
        self.assertEqual(r["unparsed"], [{"dep": "the motions module", "ids": ["E02-S04"]}])


class Done(unittest.TestCase):
    def test_refs(self):
        self.assertEqual(dag.referenced_issues("Refs #598\n\nresolves #1027. Closes #3, #4"), [598, 1027, 3, 4])
        self.assertEqual(dag.referenced_issues("after #12 lands"), [])

    def test_partial_titles(self):
        for t in ("feat(vendors): portal invites (E16-S58, part A)", "feat: the thing, part 1",
                  "#865 (B): The org envelope", "E33-S16: The founding programme (the decided, pure part)"):
            self.assertTrue(dag.is_partial_title(t), t)
        self.assertFalse(dag.is_partial_title("feat(ledger): a statement of account for a sale (E09-S61)"))

    def test_issue_work(self):
        issues = {i["number"]: i for i in [issue(1, "a", state="CLOSED"), issue(2, "b"), issue(3, "c"),
                                           issue(4, "d"), issue(5, "e")]}
        prs = dag.index_prs([pr(10, "feat: b", "Refs #2"), pr(11, "feat: c (part A)", "Refs #3"),
                             pr(12, "feat: d", "Refs #4"), pr(13, "feat: d, follow-up", "Refs #4", "OPEN"),
                             pr(14, "feat: e", "Refs #5", "CLOSED")])
        w = lambda n: dag.issue_work(n, issues, prs)
        self.assertEqual((w(1)["status"], w(1)["mergedButOpen"]), ("done", False))
        self.assertEqual((w(2)["status"], w(2)["mergedButOpen"], w(2)["mergedPrs"]), ("done", True, [10]))
        self.assertEqual((w(3)["status"], w(3)["partialPrs"]), ("todo", [11]))
        self.assertEqual((w(4)["status"], w(4)["openPrs"]), ("in-progress", [13]))
        self.assertEqual(w(5)["status"], "todo")


class Graph(unittest.TestCase):
    def test_weight_and_depth(self):
        r = dag.build_queue(**fixture())
        w = lambda i: [r["nodes"][i]["weight"], r["nodes"][i]["depth"]]
        self.assertEqual(w("E01-S01"), [4, 4])
        self.assertEqual(w("E01-S02"), [2, 3])
        self.assertEqual(w("E01-S04"), [1, 2])
        self.assertEqual(w("E01-S05"), [0, 1])
        self.assertEqual(w("E02-S01"), [0, 1])
        self.assertEqual(r["topBlockers"][0], "E01-S01")  # unblocks more, so it beats F's p0
        self.assertEqual(sorted(i for i, n in r["nodes"].items() if n["ready"]), ["E01-S01", "E02-S01"])

    def test_unmet_grouped(self):
        r = dag.build_queue(**fixture(closed=[1], prs=[pr(20, "feat: C", "Refs #3", "OPEN")]))
        self.assertTrue(r["nodes"]["E01-S02"]["ready"])
        self.assertEqual(r["nodes"]["E01-S03"]["status"], "in-progress")
        self.assertEqual([u["dep"] for u in r["nodes"]["E01-S04"]["unmet"]], ["E01-S02", "E01-S03"])
        g = next(g for g in r["blockedBy"] if g["dep"] == "E01-S03")
        self.assertEqual((g["ids"], g["why"]), (["E01-S04"], "in progress (#20)"))
        self.assertEqual(r["nodes"]["E01-S01"]["weight"], 0)

    def test_merged_but_open_and_partial(self):
        r = dag.build_queue(**fixture(prs=[pr(30, "feat: A", "Refs #1"), pr(31, "feat: F (part A)", "Refs #6")]))
        self.assertEqual(r["nodes"]["E01-S01"]["status"], "done")
        self.assertTrue(r["nodes"]["E01-S02"]["ready"])
        self.assertTrue(r["nodes"]["E02-S01"]["ready"])
        self.assertEqual(r["mergedButOpen"], [{"issue": 1, "title": "E01-S01: x", "prs": [30], "story": "E01-S01"}])

    def test_dangling(self):
        stories, _ = dag.parse_backlog(heading("E01-S01", 1, "E99-S99"))
        r = dag.build_queue(stories, [issue(1, "E01-S01: x")], [], {})
        self.assertIn("dangling story id E99-S99", [p["what"] for p in r["problems"]])

    def test_cycles(self):
        self.assertEqual(dag.find_cycles({"a": ["b"], "b": ["c"], "c": ["a"], "d": ["d"], "e": ["a"]}),
                         [["a", "b", "c"], ["d"]])
        md = "\n".join([heading("E01-S01", 1, "E01-S02"), heading("E01-S02", 2, "E01-S01")])
        r = dag.build_queue(dag.parse_backlog(md)[0], [issue(1, "E01-S01: x"), issue(2, "E01-S02: y")], [], {})
        self.assertEqual(r["cycles"], [["E01-S01", "E01-S02"]])
        self.assertFalse(any(n["ready"] for n in r["nodes"].values()))
        self.assertIn("E01-S01 ⇄ E01-S02", dag.render_report(r))


class Lanes(unittest.TestCase):
    def test_threading(self):
        ready = [{"id": "X1", "lane": None}, {"id": "P1", "lane": "06"}, {"id": "A1", "lane": "04"},
                 {"id": "A2", "lane": "04"}, {"id": "X2", "lane": None}]
        self.assertEqual(dag.thread_lanes(["04", "05", "08", "09"], ready), [
            {"lane": "04", "id": "A1", "borrowed": False},
            {"lane": "05", "id": "X1", "borrowed": True, "fromLane": None},
            {"lane": "08", "id": "P1", "borrowed": True, "fromLane": "06"},
            {"lane": "09", "id": "X2", "borrowed": True, "fromLane": None},
        ])
        self.assertEqual(dag.thread_lanes(["04", "05"], [{"id": "A1", "lane": "04"}]),
                         [{"lane": "04", "id": "A1", "borrowed": False}, {"lane": "05", "id": None, "borrowed": False}])

    def test_inherit_and_paused(self):
        r = dag.build_queue(**fixture(lanes={2: "06", 3: "06", 6: "04"}), active_lanes=["04", "05"])
        self.assertEqual((r["nodes"]["E01-S01"]["lane"], r["nodes"]["E01-S01"]["laneSource"]), ("06", "epic"))
        self.assertEqual(r["pausedBlockers"][0], {"lane": "06", "id": "E01-S01", "weight": 4, "ready": True})
        self.assertEqual(r["assignments"], [{"lane": "04", "id": "E02-S01", "borrowed": False},
                                            {"lane": "05", "id": "E01-S01", "borrowed": True, "fromLane": "06"}])
        self.assertIn("lane 06 is paused but owns E01-S01, which blocks 4", dag.render_report(r))


class IssuesSource(unittest.TestCase):
    """Backlog-free mode: stories are GitHub issues, dependencies come from `Depends on:` lines."""

    def data(self):
        return [
            issue(1, "Auth schema", ["size:M", "priority:p0", "lane:01"]),
            issue(2, "Login page", ["size:S", "lane:02"], body="Some text.\n\nDepends on: #1"),
            issue(3, "Billing", ["size:L"], body="**Blocked by:** #1, #2, ADR-0007"),
            issue(4, "Epic: auth", ["type:epic"]),
            issue(5, "Done thing", [], state="CLOSED"),
        ]

    def test_stories_and_edges(self):
        issues = self.data()
        stories = dag.stories_from_issues(issues)
        self.assertEqual([s["id"] for s in stories], ["#1", "#2", "#3", "#5"])  # epics excluded
        r = dag.build_queue(stories, issues, [], {"0007": "Accepted"})
        n = r["nodes"]
        self.assertEqual(n["#2"]["dependsOn"], ["#1"])
        self.assertEqual(n["#3"]["dependsOn"], ["#1", "#2"])
        self.assertEqual((n["#1"]["weight"], n["#1"]["depth"]), (2, 3))
        self.assertEqual([i for i, x in n.items() if x["ready"]], ["#1"])
        self.assertEqual(r["assignments"][0], {"lane": "01", "id": "#1", "borrowed": False})
        self.assertEqual(r["problems"], [])
        report = dag.render_report(r)
        self.assertIn("**#1** Auth schema · M · p0", report)

    def test_merged_pr_unblocks(self):
        issues = self.data()
        r = dag.build_queue(dag.stories_from_issues(issues), issues, [pr(9, "feat: auth schema", "Closes #1")], {})
        self.assertEqual(r["nodes"]["#1"]["status"], "done")
        self.assertTrue(r["nodes"]["#2"]["ready"])
        self.assertEqual(r["mergedButOpen"][0]["issue"], 1)
        self.assertIn("- #1: Auth schema — merged in #9", dag.render_report(r))


class Cli(unittest.TestCase):
    def test_issues_mode_end_to_end(self):
        with tempfile.TemporaryDirectory() as repo:
            subprocess.run(["git", "init", "-q", repo], check=True)
            env = {**os.environ, "HOME": repo}  # keep state out of the real ~/.claude
            subprocess.run(["bash", os.path.join(SCRIPTS, "init.sh"), repo], check=True, env=env,
                           stdout=subprocess.DEVNULL)
            snap = os.path.join(repo, "snap.json")
            with open(snap, "w") as fh:
                json.dump({"issues": IssuesSource().data(), "prs": []}, fh)
            out = subprocess.run([sys.executable, "-I", os.path.join(SCRIPTS, "work_queue.py"), "--repo", repo,
                                  "--data", snap, "--models"], check=True, env=env, capture_output=True, text=True)
            self.assertIn("**lane 01** (0 in progress): **#1** Auth schema", out.stdout)
            self.assertIn("lane 01: #1 · M:feature:? → claude-haiku-5-5 (check stack floor)", out.stdout)


if __name__ == "__main__":
    unittest.main()
