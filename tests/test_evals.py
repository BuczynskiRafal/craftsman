"""Tests for the seeded-defect benchmark: matching, scoring, ledger reading and the case suite."""

from __future__ import annotations

import contextlib
import io
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from evals import agent as run_agent
from evals import compare as run_compare
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
        self.assertGreaterEqual(sum(len(c.defects) for c in self.cases), 12)  # no upper limit
        covered = {d.kind for c in self.cases for d in c.defects}
        self.assertEqual(covered, set(run.KINDS))
        for case in self.cases:
            self.assertIn(case.size, run.SIZES)
            allowed = (1, 2) if case.size == "small" else (2, 3, 4)
            self.assertIn(len(case.decoys), allowed, case.id)

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
                changed = 0
                files = 0
                for name in set(base) | set(head):
                    old, new = base.get(name, []), head.get(name, [])
                    delta = sum(1 for line in old if line not in new) + sum(1 for line in new if line not in old)
                    changed += delta
                    files += 1 if old != new else 0
                if case.size == "small":
                    self.assertLessEqual(sum(len(t) for t in base.values()), 200)
                    self.assertTrue(0 < changed <= 150, str(changed))
                else:
                    self.assertTrue(400 <= changed <= 700, f"{changed} changed lines")
                    self.assertTrue(10 <= files <= 15, f"{files} changed files")
                    self.assertTrue(4 <= len(case.defects) <= 6, "large cases seed 4 to 6 defects")
                    cross_file = [
                        d
                        for d in case.defects
                        if d.kind == "incomplete-propagation" and len({loc.file for loc in d.locations}) > 1
                    ]
                    self.assertTrue(
                        cross_file,
                        "needs a cross-file propagation defect",
                    )

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


FAKE_CLAUDE = r"""#!{python}
import json, os, subprocess, sys, time
from pathlib import Path

argv = sys.argv[1:]
mode = os.environ.get("FAKE_CLAUDE_MODE", "ok")
plugin = argv[argv.index("--plugin-dir") + 1] if "--plugin-dir" in argv else None
cwd = Path.cwd()
try:
    wrapper_out = subprocess.run(["craftsman", "--help"], capture_output=True, text=True).stdout.strip()
except FileNotFoundError:
    wrapper_out = ""
with open(os.environ["FAKE_CLAUDE_LOG"], "a") as fh:
    fh.write(json.dumps({{
        "argv": argv,
        "cwd": str(cwd),
        "task_file": (cwd / ".git" / "evals" / "task.md").is_file(),
        "path_head": os.environ["PATH"].split(os.pathsep)[0],
        "plugin_listing": sorted(os.listdir(plugin)) if plugin else None,
        "wrapper": wrapper_out,
        "inherited": sorted(k for k in os.environ if k.startswith("GIT_")),
    }}) + "\n")

def emit(event):
    print(json.dumps(event), flush=True)

skills = ["deep-research"]
agents = ["Explore"]
if plugin and mode != "noplugin":
    skills.append("craftsman:code-review")
    agents += ["craftsman:craftsman-reviewer", "craftsman:craftsman-verifier"]
if mode == "leaky":
    skills.append("craftsman:code-review")
emit({{"type": "system", "subtype": "init", "skills": skills, "agents": agents,
      "plugins": [{{"name": "craftsman"}}] if plugin else [], "permissionMode": "dontAsk"}})
emit({{"type": "stream_event", "event": {{"type": "message_start"}}}})
if mode == "noplugin":
    time.sleep(60)
emit({{"type": "system", "subtype": "hook_response", "hook_event": "UserPromptSubmit", "outcome": "success"}})
emit({{"type": "system", "subtype": "hook_response", "hook_event": "Stop", "outcome": "error"}})
emit({{"type": "assistant", "message": {{"content": [{{"type": "tool_use", "name": "Bash", "input": {{}}}},
                                                     {{"type": "tool_use", "name": "Task", "input": {{}}}}]}}}})

ledger = os.environ.get("FAKE_LEDGER")
if ledger:
    target = cwd / ".git" / "craftsman" / "ledger.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(Path(ledger).read_text())
findings = os.environ.get("FAKE_FINDINGS")
if findings:
    (cwd / ".git" / "evals" / "findings.json").write_text(Path(findings).read_text())
if mode == "hang":
    time.sleep(60)

subtype = "error_max_budget_usd" if mode == "budget" else "success"
emit({{"type": "result", "subtype": subtype, "is_error": mode == "budget", "stop_reason": "end_turn",
      "terminal_reason": "completed", "total_cost_usd": 0.25, "duration_ms": 90000, "duration_api_ms": 80000,
      "num_turns": 7, "result": "done: " + wrapper_out,
      "usage": {{"input_tokens": 10, "output_tokens": 20, "cache_read_input_tokens": 300,
                "cache_creation_input_tokens": 40, "iterations": [{{"x": 1}}]}},
      "modelUsage": {{"claude-sonnet-5-5": {{"inputTokens": 10, "outputTokens": 20, "costUSD": 0.25}}}},
      "subagent_stats": {{"spawned": 3, "completed": 3, "failed": 0}}, "permission_denials": []}})
sys.exit(1 if mode == "budget" else 0)
"""

FAKE_CLI = """import sys
print("fake craftsman cli " + " ".join(sys.argv[1:]))
"""


class AgentTestCase(unittest.TestCase):
    """Runs ``evals.run agent`` against a fake ``claude`` that never touches the network."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.src = self._make_src(self.tmp / "src")
        self.fake = self.tmp / "fake-claude"
        self.fake.write_text(FAKE_CLAUDE.format(python=sys.executable), encoding="utf-8")
        self.fake.chmod(0o755)
        self.log = self.tmp / "fake.log"
        self.out = self.tmp / "results" / "r.jsonl"

    def _make_src(self, root: Path, skill: str = "code-review", name: str = "craftsman") -> Path:
        (root / ".claude-plugin").mkdir(parents=True)
        (root / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": name}), encoding="utf-8")
        (root / "skills" / skill).mkdir(parents=True)
        (root / "skills" / skill / "SKILL.md").write_text("---\nname: x\n---\n", encoding="utf-8")
        (root / "craftsman").mkdir()
        (root / "craftsman" / "__init__.py").write_text("", encoding="utf-8")
        (root / "craftsman" / "cli.py").write_text(FAKE_CLI, encoding="utf-8")
        (root / "evals" / "cases" / "x").mkdir(parents=True)
        (root / "evals" / "cases" / "x" / "truth.json").write_text("{}", encoding="utf-8")
        return root

    def _ledger(self, case_id: str = "quotes", found: int = 2) -> Path:
        """A ledger that finds `found` defects, hits a decoy, has one stray and one dismissed finding."""
        case = run.load_case(case_id)
        findings = [
            {"file": d.locations[0].file, "line": d.locations[0].start, "title": d.id, "verdict": "proven"}
            for d in case.defects[:found]
        ]
        decoy = next(
            loc
            for d in case.decoys
            for loc in d.locations
            if not any(run.distance(run.Finding(loc.file, loc.start, ""), df) is not None for x in case.defects for df in x.locations)
        )
        findings.append({"file": decoy.file, "line": decoy.start, "title": "decoy", "verdict": "probable"})
        findings.append({"file": "nowhere.py", "line": 1, "title": "noise", "verdict": "probable"})
        findings.append({"file": decoy.file, "line": decoy.start, "title": "gone", "verdict": "dismissed"})
        path = self.tmp / f"ledger-{case_id}.json"
        path.write_text(json.dumps({"schema_version": 3, "findings": findings}), encoding="utf-8")
        return path

    def base_argv(self, *extra: str) -> list[str]:
        return [
            "agent",
            "--cases",
            "quotes",
            "--arm",
            "t",
            "--craftsman-src",
            str(self.src),
            "--claude",
            str(self.fake),
            "--out",
            str(self.out),
            "--jobs",
            "1",
            "--budget-usd",
            "0.5",
            *extra,
        ]

    def agent(self, *extra: str, env: dict[str, str] | None = None) -> tuple[int, list[dict[str, Any]]]:
        full_env = {"FAKE_CLAUDE_LOG": str(self.log), "FAKE_LEDGER": str(self._ledger()), **(env or {})}
        with mock.patch.dict(os.environ, full_env), contextlib.redirect_stderr(io.StringIO()):
            code = run.main(self.base_argv(*extra), io.StringIO())
        return code, self.records()

    def records(self) -> list[dict[str, Any]]:
        if not self.out.is_file():
            return []
        return [json.loads(line) for line in self.out.read_text(encoding="utf-8").splitlines()]

    def calls(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]


class AgentRunTests(AgentTestCase):
    def test_a_run_is_recorded_with_recall_cost_time_and_logs(self) -> None:
        code, records = self.agent("--repeat", "2")
        self.assertEqual(code, 0)
        self.assertEqual([(r["arm"], r["case"], r["run"], r["status"]) for r in records][:1], [("t", "quotes", 1, "ok")])
        self.assertEqual(sorted(r["run"] for r in records), [1, 2])
        record = records[0]
        recall = record["recall"]
        self.assertEqual((recall["found"], recall["total"]), (2, 3))
        self.assertEqual(len(recall["found_ids"]), 2)
        self.assertEqual(len(recall["missed_ids"]), 1)
        self.assertEqual(len(recall["decoy_hits"]), 1)
        self.assertEqual([u["title"] for u in recall["unmatched"]], ["noise"])
        self.assertEqual(recall["dismissed_ignored"], 1)
        self.assertEqual((record["cost_usd"], record["duration_ms"], record["turns"]), (0.25, 90000, 7))
        self.assertEqual(record["subagents"]["spawned"], 3)
        self.assertIn("claude-sonnet-5-5", record["model_usage"])
        self.assertEqual(
            record["usage"],
            {"input_tokens": 10, "output_tokens": 20, "cache_read_input_tokens": 300, "cache_creation_input_tokens": 40},
        )
        self.assertEqual(record["tool_calls"], {"Bash": 1, "Task": 1})
        self.assertEqual(
            record["hooks"], {"UserPromptSubmit": {"runs": 1, "failed": 0}, "Stop": {"runs": 1, "failed": 1}}
        )
        self.assertTrue(record["init"]["skill_loaded"])
        self.assertEqual(record["exit_code"], 0)
        self.assertEqual(record["skill"], "craftsman:code-review")
        for key in ("stream", "stderr", "ledger"):
            self.assertTrue(Path(record["logs"][key]).is_file(), key)
        self.assertNotIn("workdir", record["logs"])
        stream = Path(record["logs"]["stream"]).read_text(encoding="utf-8")
        self.assertNotIn("stream_event", stream)  # token-level noise is not kept
        self.assertIn('"type": "result"', stream)

    def test_the_session_gets_the_plugin_the_wrapper_and_a_clean_case_dir(self) -> None:
        with mock.patch.dict(os.environ, {"GIT_DIR": "/nonexistent"}):
            _, records = self.agent()
        call = self.calls()[0]
        argv = call["argv"]
        self.assertEqual(argv[argv.index("-p") + 1], records[0]["prompt"])
        self.assertEqual(
            records[0]["prompt"],
            "/craftsman:code-review review the changes against HEAD (review only, do not fix). "
            "The change description is in .git/evals/task.md.",
        )
        self.assertEqual(argv[argv.index("--model") + 1], "sonnet")
        self.assertEqual(argv[argv.index("--max-budget-usd") + 1], "0.5")
        self.assertEqual(argv[argv.index("--output-format") + 1], "stream-json")
        self.assertIn("--verbose", argv)
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "dontAsk")
        self.assertEqual(argv[argv.index("--setting-sources") + 1], "project,local")
        allowed = argv[argv.index("--allowedTools") + 1].split(",")
        self.assertIn("Bash(craftsman:*)", allowed)
        self.assertNotIn("Bash(git stash:*)", allowed)
        self.assertFalse([t for t in allowed if t.startswith(("Write", "Edit"))])
        self.assertIn("Write", argv[argv.index("--disallowedTools") + 1].split(","))
        plugin = argv[argv.index("--plugin-dir") + 1]
        self.assertNotEqual(plugin, str(self.src))  # a snapshot, not the live checkout
        self.assertIn("skills", call["plugin_listing"])
        self.assertNotIn("evals", call["plugin_listing"])  # no truth.json next to the plugin
        self.assertTrue(call["task_file"])
        self.assertEqual(Path(call["cwd"]).name, "repo")
        self.assertFalse(Path(call["cwd"]).exists())  # the temp case dir is removed after the run
        self.assertEqual(Path(call["path_head"]).name, "bin")  # the wrapper directory comes first on PATH
        self.assertEqual(call["wrapper"], "fake craftsman cli --help")
        self.assertEqual(records[0]["result_text"], "done: fake craftsman cli --help")
        self.assertEqual(call["inherited"], [])

    def test_wrapper_script_runs_the_cli_of_the_given_source(self) -> None:
        wrapper = run_agent.write_wrapper(self.tmp / "bin", self.src)
        self.assertEqual(
            wrapper.read_text(encoding="utf-8"),
            f'#!/bin/sh\nPYTHONPATH={self.src} exec python3 -m craftsman.cli "$@"\n',
        )
        done = subprocess.run([str(wrapper), "status"], capture_output=True, text=True, check=True, cwd=self.tmp)
        self.assertEqual(done.stdout.strip(), "fake craftsman cli status")

    def test_keep_workdirs_and_custom_prompt(self) -> None:
        _, records = self.agent("--keep-workdirs", "--prompt", "/{skill} look at {case} versus {base}", "--model", "opus")
        self.assertEqual(records[0]["prompt"], "/craftsman:code-review look at quotes versus HEAD")
        self.assertEqual(records[0]["model"], "opus")
        workdir = Path(records[0]["logs"]["workdir"])
        self.assertTrue((workdir / ".git" / "craftsman" / "ledger.json").is_file())
        shutil.rmtree(workdir.parent)

    def test_prompt_template_can_be_a_file(self) -> None:
        template = self.tmp / "prompt.txt"
        template.write_text("Review {case} please\n", encoding="utf-8")
        _, records = self.agent("--prompt", str(template))
        self.assertEqual(records[0]["prompt"], "Review quotes please")

    def test_results_are_appended(self) -> None:
        self.agent()
        self.agent()
        self.assertEqual(len(self.records()), 2)

    def test_runs_without_a_ledger_are_recorded_with_a_warning(self) -> None:
        with mock.patch.dict(os.environ, {"FAKE_CLAUDE_LOG": str(self.log)}):
            run.main(self.base_argv(), io.StringIO())
        record = self.records()[0]
        self.assertIsNone(record["recall"])
        self.assertEqual(record["defects_total"], 3)
        self.assertTrue(any("no findings" in w for w in record["warnings"]))

    def test_run_fails_when_the_plugin_does_not_load_and_stops_the_session(self) -> None:
        started = time.monotonic()
        code, records = self.agent(env={"FAKE_CLAUDE_MODE": "noplugin"})
        self.assertLess(time.monotonic() - started, 30)  # the fake would sleep for 60 s
        self.assertEqual(code, 1)
        self.assertEqual(records[0]["status"], "load-failed")
        self.assertIn("skill craftsman:code-review", records[0]["init"]["problems"])
        self.assertIn("agent craftsman:craftsman-reviewer", records[0]["init"]["problems"])

    def test_timeout_keeps_the_findings_written_so_far(self) -> None:
        code, records = self.agent("--timeout-min", "0.05", env={"FAKE_CLAUDE_MODE": "hang"})
        self.assertEqual((code, records[0]["status"]), (1, "timeout"))
        self.assertEqual(records[0]["recall"]["found"], 2)
        self.assertEqual(records[0]["cost_usd"], 0.0)  # no result event, no cost

    def test_budget_overrun_is_a_status_not_an_invalid_run(self) -> None:
        code, records = self.agent(env={"FAKE_CLAUDE_MODE": "budget"})
        self.assertEqual((code, records[0]["status"], records[0]["is_error"]), (1, "budget", True))
        self.assertNotIn(records[0]["status"], run_agent.INVALID_STATUSES)

    def test_missing_claude_and_bad_options_are_usage_errors(self) -> None:
        for extra in (["--claude", str(self.tmp / "nope")], ["--repeat", "0"], ["--cases", "nope"], ["--budget-usd", "0"]):
            with self.subTest(extra=extra), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(run.main(self.base_argv(*extra), io.StringIO()), run.EXIT_USAGE)

    def test_dry_run_prints_the_command_and_calls_nothing(self) -> None:
        out = io.StringIO()
        self.assertEqual(run.main(self.base_argv("--dry-run"), out), 0)
        self.assertIn("--permission-mode dontAsk", out.getvalue())
        self.assertIn("--plugin-dir", out.getvalue())
        self.assertFalse(self.log.exists())

    def test_baseline_arm_has_no_plugin_and_reads_the_findings_file(self) -> None:
        case = run.load_case("quotes")
        findings = [{"file": d.locations[0].file, "line": d.locations[0].start, "title": d.id} for d in case.defects]
        path = self.tmp / "baseline-findings.json"
        path.write_text(json.dumps(findings), encoding="utf-8")
        code, records = self.agent("--baseline", env={"FAKE_FINDINGS": str(path), "FAKE_LEDGER": ""})
        self.assertEqual(code, 0)
        argv = self.calls()[0]["argv"]
        self.assertNotIn("--plugin-dir", argv)
        self.assertEqual(argv[argv.index("-p") + 1], run.BASELINE_PROMPT)
        allowed = argv[argv.index("--allowedTools") + 1].split(",")
        self.assertTrue(any(t.startswith("Write(//") and t.endswith("/.git/evals/findings.json)") for t in allowed))
        self.assertNotIn("Write", argv[argv.index("--disallowedTools") + 1].split(","))
        self.assertEqual(records[0]["recall"]["found"], 3)
        self.assertIsNone(records[0]["craftsman_src"])

    def test_baseline_arm_fails_when_craftsman_is_loaded_anyway(self) -> None:
        _, records = self.agent("--baseline", env={"FAKE_CLAUDE_MODE": "leaky"})
        self.assertEqual(records[0]["status"], "load-failed")
        self.assertIn("unexpected craftsman:code-review", records[0]["init"]["problems"])

    def test_cases_and_repeats_run_in_parallel_and_all_land_in_the_file(self) -> None:
        argv = self.base_argv("--repeat", "2", "--jobs", "3", "--cases", "quotes,kvstore")
        argv = argv[: argv.index("--cases")] + argv[argv.index("--cases") + 2 :]  # drop the first --cases
        with mock.patch.dict(os.environ, {"FAKE_CLAUDE_LOG": str(self.log)}):
            self.assertEqual(run.main(argv, io.StringIO()), 0)
        runs = sorted((r["case"], r["run"]) for r in self.records())
        self.assertEqual(runs, [("kvstore", 1), ("kvstore", 2), ("quotes", 1), ("quotes", 2)])


class AgentHelperTests(AgentTestCase):
    def test_detect_plugin_prefers_craft_review_and_uses_the_manifest_name(self) -> None:
        both = self._make_src(self.tmp / "both", "craft-review", "forge")
        (both / "skills" / "code-review").mkdir()
        (both / "skills" / "code-review" / "SKILL.md").write_text("x", encoding="utf-8")
        info = run_agent.detect_plugin(both)
        self.assertEqual((info.name, info.skill, info.qualified_skill), ("forge", "craft-review", "forge:craft-review"))
        self.assertEqual(info.agents, ("forge:craftsman-reviewer", "forge:craftsman-verifier"))
        self.assertEqual(run_agent.detect_plugin(self.src).qualified_skill, "craftsman:code-review")

    def test_detect_plugin_errors(self) -> None:
        empty = self.tmp / "empty"
        empty.mkdir()
        with self.assertRaises(run.EvalError):
            run_agent.detect_plugin(empty)
        (empty / "plugin.json").write_text(json.dumps({"name": "craftsman"}), encoding="utf-8")
        with self.assertRaises(run.EvalError):  # a manifest but no review skill
            run_agent.detect_plugin(empty)
        with self.assertRaises(run.EvalError):
            run_agent.detect_plugin(self.tmp / "missing")

    def test_this_repository_is_a_valid_source(self) -> None:
        info = run_agent.detect_plugin(REPO_ROOT)
        self.assertEqual(info.name, "craftsman")
        self.assertIn(info.skill, ("craft-review", "code-review"))

    def test_snapshot_leaves_out_the_evals_and_vcs_files(self) -> None:
        (self.src / ".git").mkdir()
        (self.src / "docs").mkdir()
        snapshot = run_agent.snapshot_source(self.src, self.tmp / "snap")
        self.assertEqual(sorted(p.name for p in snapshot.iterdir()), [".claude-plugin", "craftsman", "skills"])

    @unittest.skipIf(shutil.which("git") is None, "git is required")
    def test_source_revision_marks_edits_of_plugin_files_only(self) -> None:
        repo = self.tmp / "checkout"
        repo.mkdir()
        (repo / "plugin.json").write_text("{}", encoding="utf-8")
        (repo / "evals").mkdir()
        (repo / "evals" / "notes.txt").write_text("a", encoding="utf-8")
        for args in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "x"]):
            subprocess.run(
                ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
                cwd=repo,
                check=True,
                capture_output=True,
            )
        clean = run_agent.source_revision(repo)
        self.assertRegex(clean, r"^[0-9a-f]{7,}$")
        (repo / "evals" / "notes.txt").write_text("b", encoding="utf-8")
        self.assertEqual(run_agent.source_revision(repo), clean)
        (repo / "plugin.json").write_text("{ }", encoding="utf-8")
        self.assertEqual(run_agent.source_revision(repo), clean + "+dirty")
        self.assertEqual(run_agent.source_revision(self.tmp / "nowhere"), "")

    def test_parse_output_reads_lines_a_list_or_one_object(self) -> None:
        init = {"type": "system", "subtype": "init", "skills": ["a"]}
        result = {"type": "result", "subtype": "success", "num_turns": 2}
        as_lines = "\n".join(json.dumps(e) for e in (init, result)) + "\nnot json\n"
        for text in (as_lines, json.dumps([init, result])):
            stream = run_agent.parse_output(text)
            self.assertEqual((stream.init, stream.result), (init, result))
        self.assertEqual(run_agent.parse_output(json.dumps(result)).result, result)
        self.assertIsNone(run_agent.parse_output("").result)

    def test_check_init(self) -> None:
        info = run_agent.PluginInfo("craftsman", "code-review")
        cfg = run_agent.AgentConfig(
            (), self.src, info, "a", "sonnet", 1, 1, 1.0, 1.0, self.out, self.out, None, "claude", "dontAsk", (), False
        )
        agents = ["craftsman:craftsman-reviewer", "craftsman:craftsman-verifier"]
        self.assertEqual(run_agent.check_init({"skills": ["craftsman:code-review"], "agents": agents}, cfg), [])
        self.assertEqual(run_agent.check_init({"slash_commands": ["craftsman:code-review"], "agents": agents}, cfg), [])
        self.assertEqual(
            run_agent.check_init({"skills": [], "agents": agents[:1]}, cfg),
            ["skill craftsman:code-review", "agent craftsman:craftsman-verifier"],
        )

    def test_render_prompt_keeps_unknown_braces(self) -> None:
        self.assertEqual(run_agent.render_prompt('{skill} {"a": 1} {x}', {"skill": "s"}), 's {"a": 1} {x}')


def _record(arm: str, case: str, index: int, found: int, **extra: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "arm": arm,
        "case": case,
        "run": index,
        "defects_total": 4,
        "status": "ok",
        "recall": {
            "found": found,
            "total": 4,
            "found_ids": [f"d{i}" for i in range(found)],
            "decoy_hits": [],
            "unmatched": [],
        },
        "cost_usd": 1.0,
        "wall_s": 120.0,
        "turns": 10,
        "subagents": {"spawned": 4},
    }
    record.update(extra)
    return record


class CompareTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def _write(self, name: str, records: list[dict[str, Any]]) -> Path:
        path = self.tmp / name
        path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
        return path

    def _compare(self, *paths: Path, extra: tuple[str, ...] = ()) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stderr(io.StringIO()):
            code = run.main(["compare", *map(str, paths), *extra], out)
        return code, out.getvalue()

    def test_statistics(self) -> None:
        self.assertEqual(run_compare.mean([1, 2, 3]), 2)
        self.assertAlmostEqual(run_compare.spread([1, 2, 3]) or 0, 1.0)
        self.assertIsNone(run_compare.spread([5]))
        diff = run_compare.difference([0.5, 0.6, 0.7], [0.7, 0.8, 0.9])
        assert diff is not None
        self.assertAlmostEqual(diff[0], 0.2)
        # equal spreads of 0.1 and three values each: df 4, t 2.132, standard error 0.1 * sqrt(2 / 3)
        self.assertAlmostEqual(diff[1], 0.2 - 2.132 * 0.1 * math.sqrt(2 / 3), places=3)
        self.assertAlmostEqual(diff[2], 0.2 + 2.132 * 0.1 * math.sqrt(2 / 3), places=3)
        self.assertIsNone(run_compare.difference([0.5], [0.7, 0.8]))
        self.assertEqual(run_compare.difference([1, 1], [2, 2]), (1.0, 1.0, 1.0))

    def test_table_per_arm_and_case_with_totals_and_spread(self) -> None:
        records = [
            _record("base", "alpha", 1, 2, cost_usd=1.0),
            _record("base", "alpha", 2, 4, cost_usd=3.0),
            _record("base", "beta", 1, 1),
            _record("base", "beta", 2, 3),
        ]
        code, text = self._compare(self._write("a.jsonl", records))
        self.assertEqual(code, 0)
        self.assertIn("arm base: 4 valid runs, $6.00 spent", text)
        alpha = next(line for line in text.splitlines() if line.startswith("alpha"))
        self.assertIn("3.00/4 (75%)", alpha)
        self.assertIn("2-4", alpha)
        self.assertIn("2.00 ±1.41", alpha)  # mean cost and spread over the two runs
        total = next(line for line in text.splitlines() if line.startswith("TOTAL"))
        self.assertIn("5.00/8 (62%)", total)  # sum of the case means over the seeded defects
        self.assertIn("38%-88%", total)  # suite recall per repeat: (2+1)/8 and (4+3)/8
        self.assertIn("3.00", total)  # a full pass costs the sum of the case means: 2.00 + 1.00

    def test_invalid_runs_are_left_out_and_counted(self) -> None:
        records = [
            _record("a", "alpha", 1, 4),
            _record("a", "alpha", 2, 0, status="load-failed", recall=None),
            _record("a", "alpha", 3, 2, status="timeout"),
        ]
        _, text = self._compare(self._write("a.jsonl", records))
        alpha = next(line for line in text.splitlines() if line.startswith("alpha"))
        self.assertIn("1 invalid", alpha)
        self.assertIn("1 ended early", alpha)
        self.assertIn("3.00/4", alpha)  # the timeout run counts with what it found, the load failure does not
        self.assertIn("2 valid runs", text)

    def test_two_arms_are_compared_with_an_interval(self) -> None:
        old = [_record("old", c, i, f) for c in ("alpha", "beta") for i, f in enumerate((3, 3, 4), 1)]
        new = [_record("new", c, i, f, cost_usd=0.5) for c in ("alpha", "beta") for i, f in enumerate((3, 4, 4), 1)]
        code, text = self._compare(self._write("old.jsonl", old), self._write("new.jsonl", new), extra=("--defects",))
        self.assertEqual(code, 0)
        self.assertIn("new versus old", text)
        self.assertIn("x0.50", text)  # cost ratio
        self.assertRegex(
            text,
            r"Suite recall, new minus old: \+\d+\.\d pts, 90% interval -?\d+\.\d to \+\d+\.\d pts \(3 vs 3 repeats\)",
        )
        self.assertIn("Defects found / valid runs", text)

    def test_single_repeat_gives_no_interval(self) -> None:
        records = [_record("a", "alpha", 1, 2), _record("b", "alpha", 1, 3)]
        _, text = self._compare(self._write("r.jsonl", records))
        self.assertIn("run at least 2 per arm for an interval", text)

    def test_json_output_and_errors(self) -> None:
        path = self._write("r.jsonl", [_record("a", "alpha", 1, 2)])
        code, text = self._compare(path, extra=("--json",))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(text)["a"]["rows"][-1]["case"], "TOTAL")
        bad = self.tmp / "bad.jsonl"
        bad.write_text("{not json}\n", encoding="utf-8")
        other = self.tmp / "other.jsonl"
        other.write_text('{"x": 1}\n', encoding="utf-8")
        for target in (bad, other, self.tmp / "missing.jsonl", self._write("empty.jsonl", [])):
            with self.subTest(target=target.name):
                self.assertEqual(self._compare(target)[0], run.EXIT_USAGE)

    def test_several_files_are_merged(self) -> None:
        one = self._write("1.jsonl", [_record("a", "alpha", 1, 2)])
        two = self._write("2.jsonl", [_record("a", "alpha", 2, 4)])
        _, text = self._compare(one, two)
        self.assertIn("arm a: 2 valid runs", text)


class AgentToCompareTests(AgentTestCase):
    def test_what_agent_writes_compare_reads(self) -> None:
        self.agent("--repeat", "2")
        out = io.StringIO()
        self.assertEqual(run.main(["compare", str(self.out)], out), 0)
        text = out.getvalue()
        self.assertIn("arm t: 2 valid runs, $0.50 spent", text)
        self.assertIn("2.00/3 (67%)", text)
        self.assertIn("0.25 ±0.00", text)
        self.assertIn("3.0", next(line for line in text.splitlines() if line.startswith("quotes")))  # subagents


if __name__ == "__main__":
    unittest.main()
