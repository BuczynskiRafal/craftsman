"""Tests for the seeded-defect benchmark: matching, scoring, ledger reading and the case suite."""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from evals import run

REPO_ROOT = Path(__file__).resolve().parent.parent
README = REPO_ROOT / "evals" / "README.md"


def _loc(file: str, start: int, end: int | None = None) -> run.Location:
    return run.Location(file, start, start if end is None else end)


def _case(*defects: run.Defect, decoys: tuple[run.Decoy, ...] = ()) -> run.Case:
    return run.Case("synthetic", Path("."), tuple(defects), decoys)


def _defect(defect_id: str, *locations: run.Location) -> run.Defect:
    return run.Defect(defect_id, "swallowed-error", "summary", locations)


def _finding(file: str, line: int, title: str = "t") -> run.Finding:
    return run.Finding(file, line, title)


class DistanceTests(unittest.TestCase):
    def test_window_is_three_lines_around_the_location(self) -> None:
        location = _loc("a.py", 10, 12)
        self.assertEqual(run.distance(_finding("a.py", 11), location), 0)
        self.assertEqual(run.distance(_finding("a.py", 7), location), 3)
        self.assertEqual(run.distance(_finding("a.py", 15), location), 3)
        self.assertIsNone(run.distance(_finding("a.py", 6), location))
        self.assertIsNone(run.distance(_finding("a.py", 16), location))

    def test_other_file_never_matches(self) -> None:
        self.assertIsNone(run.distance(_finding("b.py", 10), _loc("a.py", 10)))

    def test_paths_are_normalized(self) -> None:
        self.assertEqual(run.normalize_path("./pkg/a.py"), "pkg/a.py")
        self.assertEqual(run.normalize_path("pkg\\sub\\a.py"), "pkg/sub/a.py")
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp).resolve()
            self.assertEqual(run.normalize_path(f"{work}/pkg/a.py", work), "pkg/a.py")
            self.assertEqual(run.normalize_path("/elsewhere/a.py", work), "/elsewhere/a.py")


class MatchingTests(unittest.TestCase):
    def test_finding_inside_the_window_finds_the_defect(self) -> None:
        case = _case(_defect("d1", _loc("a.py", 10, 12)))
        matching = run.match_findings(case, [_finding("a.py", 15)])
        self.assertEqual(matching.found, {"d1": 0})
        self.assertEqual(matching.unmatched, ())

    def test_each_defect_counts_once_and_each_finding_matches_one_defect(self) -> None:
        case = _case(_defect("d1", _loc("a.py", 10)), _defect("d2", _loc("a.py", 30)))
        # Two findings on d1's line: d1 is found once, the second finding is a duplicate and
        # d2 stays missed.
        matching = run.match_findings(case, [_finding("a.py", 10), _finding("a.py", 10)])
        self.assertEqual(len(matching.found), 1)
        self.assertEqual(len(matching.duplicates), 1)
        result = run.score_findings(case, [_finding("a.py", 10), _finding("a.py", 10)])
        self.assertEqual(result["found"], 1)
        self.assertEqual(result["recall"], 0.5)

    def test_matching_is_maximal_when_a_finding_is_near_two_defects(self) -> None:
        case = _case(_defect("d1", _loc("a.py", 10)), _defect("d2", _loc("a.py", 16)))
        # Line 13 is within the window of both; line 8 only of d1. A greedy pass that gives 13 to
        # d1 would lose d2... 8 -> d1 and 13 -> d2 finds both.
        matching = run.match_findings(case, [_finding("a.py", 13), _finding("a.py", 8)])
        self.assertEqual(set(matching.found), {"d1", "d2"})
        self.assertEqual(matching.found["d1"], 1)
        self.assertEqual(matching.found["d2"], 0)

    def test_a_defect_may_have_several_locations(self) -> None:
        case = _case(_defect("d1", _loc("a.py", 10), _loc("b.py", 50)))
        self.assertEqual(set(run.match_findings(case, [_finding("b.py", 52)]).found), {"d1"})
        self.assertEqual(set(run.match_findings(case, [_finding("c.py", 52)]).found), set())

    def test_decoy_hits_and_unmatched_findings_are_separated(self) -> None:
        decoy = run.Decoy("x1", "fine", (_loc("a.py", 40),))
        case = _case(_defect("d1", _loc("a.py", 10)), decoys=(decoy,))
        findings = [_finding("a.py", 41), _finding("a.py", 90), _finding("a.py", 10)]
        matching = run.match_findings(case, findings)
        self.assertEqual(matching.found, {"d1": 2})
        self.assertEqual(matching.decoy_hits, ((0, "x1"),))
        self.assertEqual(matching.unmatched, (1,))

    def test_exact_hit_wins_over_a_nearby_finding(self) -> None:
        case = _case(_defect("d1", _loc("a.py", 10)))
        matching = run.match_findings(case, [_finding("a.py", 12), _finding("a.py", 10)])
        self.assertEqual(matching.found, {"d1": 1})
        self.assertEqual(matching.duplicates, (0,))

    def test_no_findings_scores_zero(self) -> None:
        result = run.score_findings(_case(_defect("d1", _loc("a.py", 1))), [])
        self.assertEqual((result["found"], result["total"], result["recall"]), (0, 1, 0.0))
        self.assertIn("MISSED", run.render_score(result))


class ReadingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def _write(self, name: str, payload: Any) -> Path:
        path = self.tmp / name
        path.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")
        return path

    def test_ledger_ignores_dismissed_and_reads_file_or_path(self) -> None:
        ledger = {
            "schema_version": 3,
            "findings": [
                {"id": "F1", "file": "a.py", "line": 3, "title": "one", "verdict": "proven"},
                {"id": "F2", "path": "./b.py", "line": 4, "title": "two", "verdict": None},
                {"id": "F3", "file": "c.py", "line": 5, "title": "three", "verdict": "dismissed"},
                {"id": "F4", "file": "d.py", "title": "no line"},
                "garbage",
            ],
            "rounds": [{"unrelated": True}],
        }
        findings, dismissed, unreadable = run.read_ledger(self._write("ledger.json", ledger))
        self.assertEqual([(f.file, f.line) for f in findings], [("a.py", 3), ("b.py", 4)])
        self.assertEqual((dismissed, unreadable), (1, 2))

    def test_ledger_without_findings_is_an_error(self) -> None:
        with self.assertRaises(run.EvalError):
            run.read_ledger(self._write("ledger.json", {"rounds": []}))
        with self.assertRaises(run.EvalError):
            run.read_ledger(self._write("ledger.json", "not json"))
        with self.assertRaises(run.EvalError):
            run.read_ledger(self.tmp / "missing.json")

    def test_baseline_list_and_wrapped_list(self) -> None:
        entries = [{"file": "a.py", "line": 3, "title": "x"}, {"file": "b.py", "line": "7"}, {"line": 1}]
        for payload in (entries, {"findings": entries}):
            findings, dismissed, unreadable = run.read_findings_file(self._write("f.json", payload))
            self.assertEqual([(f.file, f.line) for f in findings], [("a.py", 3), ("b.py", 7)])
            self.assertEqual((dismissed, unreadable), (0, 1))
        with self.assertRaises(run.EvalError):
            run.read_findings_file(self._write("f.json", {"a": 1}))

    def test_booleans_are_not_line_numbers(self) -> None:
        findings, _, unreadable = run.read_findings_file(
            self._write("f.json", [{"file": "a.py", "line": True}])
        )
        self.assertEqual((findings, unreadable), ([], 1))


class SuiteTests(unittest.TestCase):
    """The shipped cases are well formed and their shipped tests are green."""

    cases: list[run.Case]

    @classmethod
    def setUpClass(cls) -> None:
        cls.cases = [run.load_case(case_id) for case_id in run.case_ids()]

    def test_suite_size_and_coverage(self) -> None:
        self.assertGreaterEqual(len(self.cases), 6)
        total = sum(len(c.defects) for c in self.cases)
        self.assertTrue(12 <= total <= 16, str(total))
        covered = {d.kind for c in self.cases for d in c.defects}
        self.assertEqual(covered, set(run.KINDS))
        for case in self.cases:
            self.assertIn(len(case.decoys), (1, 2), case.id)

    def test_case_layout(self) -> None:
        for case in self.cases:
            with self.subTest(case=case.id):
                self.assertTrue((case.path / "base").is_dir())
                self.assertTrue((case.path / "head").is_dir())
                self.assertTrue((case.path / "task.md").read_text(encoding="utf-8").strip())
                for side in ("base", "head"):
                    self.assertFalse(list((case.path / side).rglob("truth.json")))
                    self.assertFalse(list((case.path / side).rglob("task.md")))

    def test_case_sizes(self) -> None:
        for case in self.cases:
            with self.subTest(case=case.id):
                base = _text_files(case.path / "base")
                head = _text_files(case.path / "head")
                self.assertLessEqual(sum(len(t) for t in base.values()), 200)
                changed = 0
                for name in set(base) | set(head):
                    old, new = base.get(name, []), head.get(name, [])
                    changed += sum(1 for line in old if line not in new)
                    changed += sum(1 for line in new if line not in old)
                self.assertTrue(0 < changed <= 150, str(changed))

    def test_truth_locations_point_at_existing_lines(self) -> None:
        for case in self.cases:
            items: list[run.Defect | run.Decoy] = [*case.defects, *case.decoys]
            for item in items:
                for loc in item.locations:
                    with self.subTest(case=case.id, item=item.id):
                        path = case.path / "head" / loc.file
                        self.assertTrue(path.is_file(), f"{loc.file} is not in head/")
                        lines = path.read_text(encoding="utf-8").splitlines()
                        self.assertLessEqual(loc.end, len(lines))
                        self.assertTrue(lines[loc.start - 1].strip(), f"{loc.file}:{loc.start} is blank")

    def test_base_and_head_tests_pass(self) -> None:
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        env.pop("PYTHONPATH", None)
        for case in self.cases:
            for side in ("base", "head"):
                with self.subTest(case=case.id, side=side):
                    done = subprocess.run(
                        [sys.executable, "-B", "-m", "unittest"],
                        cwd=case.path / side,
                        env=env,
                        capture_output=True,
                        text=True,
                        timeout=120,
                    )
                    self.assertEqual(done.returncode, 0, done.stderr[-2000:])
                    self.assertNotIn("NO TESTS RAN", done.stderr)


def _text_files(root: Path) -> dict[str, list[str]]:
    return {
        p.relative_to(root).as_posix(): p.read_text(encoding="utf-8").splitlines()
        for p in root.rglob("*")
        if p.is_file() and not {"__pycache__", ".git"} & set(p.relative_to(root).parts)
    }


@unittest.skipIf(shutil.which("git") is None, "git is required")
class PrepareAndScoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def _run(self, *argv: str) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stderr(io.StringIO()):
            code = run.main(list(argv), out)
        return code, out.getvalue()

    def _git(self, work: Path, *args: str) -> str:
        done = subprocess.run(["git", *args], cwd=work, capture_output=True, text=True, check=True)
        return done.stdout

    def test_list_shows_every_case(self) -> None:
        code, text = self._run("list")
        self.assertEqual(code, 0)
        for case_id in run.case_ids():
            self.assertIn(case_id, text)
        code, text = self._run("list", "--json")
        self.assertEqual(sorted(row["case"] for row in json.loads(text)), run.case_ids())

    def test_prepare_commits_base_and_leaves_the_change_uncommitted(self) -> None:
        for case_id in run.case_ids():
            with self.subTest(case=case_id):
                case = run.load_case(case_id)
                work = self.tmp / case_id
                code, text = self._run("prepare", case_id, str(work))
                self.assertEqual(code, 0)
                self.assertIn("/code-review", text)
                self.assertIn("Do not modify any file", text)
                self.assertEqual(self._git(work, "rev-list", "--count", "HEAD").strip(), "1")
                self.assertTrue(self._git(work, "status", "--porcelain").strip())
                committed = set(self._git(work, "ls-tree", "-r", "--name-only", "HEAD").split())
                self.assertEqual(committed, set(_text_files(case.path / "base")))
                self.assertEqual(_text_files(work), _text_files(case.path / "head"))
                self.assertEqual(
                    (work / ".git" / "evals" / "task.md").read_text(encoding="utf-8"),
                    (case.path / "task.md").read_text(encoding="utf-8"),
                )
                self.assertFalse(list(work.rglob("truth.json")))

    def test_prepare_refuses_a_non_empty_destination(self) -> None:
        work = self.tmp / "busy"
        work.mkdir()
        (work / "keep.txt").write_text("x", encoding="utf-8")
        self.assertEqual(self._run("prepare", "kvstore", str(work))[0], run.EXIT_USAGE)
        self.assertEqual(self._run("prepare", "no-such-case", str(self.tmp / "new"))[0], run.EXIT_USAGE)

    def _perfect_findings(self, case: run.Case) -> list[dict[str, Any]]:
        return [
            {"file": d.locations[0].file, "line": d.locations[0].start, "title": d.id}
            for d in case.defects
        ]

    def test_score_from_baseline_file_ledger_and_default_findings_file(self) -> None:
        case = run.load_case("quotes")
        work = self.tmp / "quotes"
        self._run("prepare", "quotes", str(work))
        findings = self._perfect_findings(case)
        noise = {"file": "quotes/pricing.py", "line": 1, "title": "unrelated"}

        explicit = self.tmp / "findings.json"
        explicit.write_text(json.dumps([*findings[:2], noise]), encoding="utf-8")
        code, text = self._run("score", "quotes", str(work), "--findings", str(explicit), "--json")
        result = json.loads(text)
        self.assertEqual((code, result["found"], result["total"]), (0, 2, 3))
        self.assertEqual(len(result["unmatched"]), 1)

        code, text = self._run("score", "quotes", str(work))
        self.assertEqual(code, run.EXIT_USAGE)  # no ledger and no findings file yet

        default = work / ".git" / "evals" / "findings.json"
        default.write_text(json.dumps(findings), encoding="utf-8")
        result = json.loads(self._run("score", "quotes", str(work), "--json")[1])
        self.assertEqual(result["recall"], 1.0)

        ledger = work / ".git" / "craftsman" / "ledger.json"
        ledger.parent.mkdir(parents=True)
        entries = [{**f, "verdict": "probable"} for f in findings[:1]]
        entries.append({**findings[1], "verdict": "dismissed"})
        entries.append({**findings[2], "file": str(work / findings[2]["file"])})
        ledger.write_text(json.dumps({"schema_version": 3, "findings": entries}), encoding="utf-8")
        code, text = self._run("score", "quotes", str(work))
        self.assertEqual(code, 0)
        self.assertIn("recall 2/3", text)
        self.assertIn("1 dismissed", text)
        self.assertIn("MISSED", text)

    def test_perfect_findings_score_full_recall_on_every_case(self) -> None:
        for case_id in run.case_ids():
            with self.subTest(case=case_id):
                case = run.load_case(case_id)
                work = self.tmp / case_id
                path = self.tmp / f"{case_id}.json"
                path.write_text(json.dumps(self._perfect_findings(case)), encoding="utf-8")
                work.mkdir()
                result = json.loads(
                    self._run("score", case_id, str(work), "--findings", str(path), "--json")[1]
                )
                self.assertEqual(result["found"], len(case.defects))
                self.assertEqual(result["unmatched"], [])

    def test_decoy_locations_do_not_find_defects(self) -> None:
        for case_id in run.case_ids():
            with self.subTest(case=case_id):
                case = run.load_case(case_id)
                decoy_findings = [
                    run.Finding(loc.file, loc.start, d.id) for d in case.decoys for loc in d.locations
                ]
                matching = run.match_findings(case, decoy_findings)
                hits = {index for index, _ in matching.decoy_hits}
                # A decoy that sits next to a defect may be read as a duplicate or a defect hit,
                # so only decoys clear of every defect window must be reported as decoy hits.
                for index, finding in enumerate(decoy_findings):
                    near_defect = any(
                        run.distance(finding, loc) is not None
                        for df in case.defects
                        for loc in df.locations
                    )
                    if not near_defect:
                        self.assertIn(index, hits, finding)


class DocsTests(unittest.TestCase):
    def test_readme_carries_the_exact_baseline_prompt(self) -> None:
        self.assertIn(run.BASELINE_PROMPT, README.read_text(encoding="utf-8"))

    def test_readme_names_every_case_and_kind(self) -> None:
        text = README.read_text(encoding="utf-8")
        for case_id in run.case_ids():
            self.assertIn(f"`{case_id}`", text)
        for kind in run.KINDS:
            self.assertIn(f"`{kind}`", text)


if __name__ == "__main__":
    unittest.main()
