"""Acceptance tests for partial review-only reports and `craftsman review show --hunks`."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

from craftsman.diffscan import snapshot
from craftsman.gatecheck import GateIssue, GateReport

from tests.support import EvidenceRepo, assert_cli_error, coverage_note

BUGGY_CART = '''def total(items):
    """Sum of prices."""
    return sum(item["price"] for item in items) / len(items)
'''
STATIC_EVIDENCE = "static: cart.py:3 divides by len(items), which raises ZeroDivisionError on []"
FINDING_FLAGS = (
    "--lens",
    "contracts",
    "--severity",
    "major",
    "--title",
    "Empty cart divides by zero",
    "--scenario",
    "total([]) evaluates sum([]) / len([]) and raises ZeroDivisionError",
)


class GateReportStatusTests(unittest.TestCase):
    def test_status_label_reflects_partial_mode(self) -> None:
        issue_a = GateIssue("uncovered", "Lens `contracts` has 1 uncovered hunk.")
        issue_b = GateIssue("no-verdict", "Finding F1 has no verdict.")
        cases = [
            (GateReport((), (), ""), "PASSED"),
            (GateReport((issue_a,), (), ""), "BLOCKED (1 open items)"),
            (GateReport((), (), "", review_only=True), "COMPLETE (review-only)"),
            (GateReport((issue_a,), (), "", review_only=True), "INCOMPLETE (1 open items)"),
            (GateReport((), (), "", review_only=True, partial=True), "COMPLETE (review-only)"),
            (
                GateReport((issue_a, issue_b), (), "", review_only=True, partial=True),
                "PARTIAL (review-only, 2 open items)",
            ),
        ]
        for report, expected in cases:
            with self.subTest(expected=expected, partial=report.partial):
                self.assertEqual(report.status_label, expected)


class _RepoTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.repo = EvidenceRepo(self.tmp / "repo")

    def review(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return self.repo.cli("review", *args, check=check)

    def json_of(self, *args: str) -> Any:
        return json.loads(self.review(*args, "--json").stdout)


class PartialReportCliTests(_RepoTestCase):

    def test_partial_report_shows_uncovered_lenses_with_unverified_finding(self) -> None:
        self.repo.write("cart.py", BUGGY_CART)
        self.review("prepare", "--tier", "quick")
        self.review(
            "cover", "--lens", "intent", "--hunks", "H1", "--note", coverage_note(["H1"])
        )
        self.review("add", "--file", "cart.py", "--line", "3", *FINDING_FLAGS)

        proc = self.review("report", "--review-only", "--partial")
        report = proc.stdout

        self.assertEqual(proc.returncode, 0)
        self.assertIn("- Review: PARTIAL (review-only,", report)
        self.assertIn("\n## Not covered\n", report)
        self.assertIn("- `contracts`: H1", report)
        self.assertNotIn("- `intent`:", report)
        self.assertIn("\n## Findings\n", report)
        self.assertIn("F1 · major · `cart.py:3`", report)
        self.assertIn("(unverified)", report)

        payload = self.json_of("report", "--review-only", "--partial")
        self.assertTrue(payload["partial"])
        self.assertFalse(payload["passed"])
        self.assertEqual(payload["not_covered"]["contracts"], ["H1"])
        self.assertNotIn("intent", payload["not_covered"])

    def test_partial_report_is_complete_when_no_open_items_remain(self) -> None:
        self.repo.write("cart.py", BUGGY_CART)
        self.review("prepare", "--tier", "quick")
        self.repo.cover_all()
        self.review("add", "--file", "cart.py", "--line", "3", *FINDING_FLAGS)
        self.review("verdict", "F1", "--as", "probable", "--evidence", STATIC_EVIDENCE)

        report = self.review("report", "--review-only", "--partial").stdout
        payload = self.json_of("report", "--review-only", "--partial")

        self.assertIn("- Review: COMPLETE (review-only)", report)
        self.assertNotIn("## Not covered", report)
        self.assertNotIn("(unverified)", report)
        self.assertTrue(payload["partial"])
        self.assertTrue(payload["passed"])
        self.assertEqual(payload["not_covered"], {})

    def test_partial_without_review_only_is_rejected(self) -> None:
        self.repo.write("cart.py", BUGGY_CART)
        self.review("prepare", "--tier", "quick")

        proc = self.review("report", "--partial", check=False)

        assert_cli_error(self, proc, 1, "--partial requires --review-only")

    def test_partial_is_refused_during_an_active_gate(self) -> None:
        self.repo.cli("gate", "--init", "--tier", "quick", "--cwd", ".")
        self.repo.write("cart.py", BUGGY_CART)

        proc = self.review("report", "--review-only", "--partial", check=False)

        assert_cli_error(self, proc, 1, "--review-only is not allowed during an active /craftsman gate")

    def test_review_check_is_unchanged(self) -> None:
        self.repo.write("cart.py", BUGGY_CART)
        self.review("prepare", "--tier", "quick")

        checked = self.review("check", "--review-only", check=False)
        checked_json = json.loads(
            self.review("check", "--review-only", "--json", check=False).stdout
        )
        bad_flag = self.review("check", "--review-only", "--partial", check=False)

        self.assertEqual(checked.returncode, 1)
        self.assertIn("INCOMPLETE", checked.stdout)
        self.assertFalse(checked_json["partial"])
        assert_cli_error(self, bad_flag, 2, "unrecognized arguments: --partial")

    def test_empty_ledger_does_not_crash_report(self) -> None:
        clean_partial = self.review("report", "--review-only", "--partial")
        self.assertEqual(clean_partial.returncode, 0)
        self.assertIn("- Review: COMPLETE (review-only)", clean_partial.stdout)

        self.repo.write("cart.py", BUGGY_CART)
        dirty_report = self.review("report")
        dirty_partial = self.review("report", "--review-only", "--partial")
        dirty_json = self.json_of("report", "--review-only", "--partial")

        self.assertEqual(dirty_report.returncode, 0)
        self.assertEqual(dirty_partial.returncode, 0)
        self.assertIn("- Review: PARTIAL (review-only, 1 open items)", dirty_partial.stdout)
        self.assertIn("## Not covered", dirty_partial.stdout)
        self.assertTrue(dirty_json["partial"])
        self.assertEqual(dirty_json["rounds"], 0)
        self.assertEqual(dirty_json["not_covered"], {})


class ShowHunksTests(_RepoTestCase):

    def test_show_hunks_maps_open_hunks_to_line_ranges(self) -> None:
        header = "".join(f"CONST_{idx} = {idx}\n" for idx in range(1, 9))
        self.repo.write(
            "src/a.py",
            header + "def compute_total(items):\n    return sum(items)\n",
        )
        self.repo.write("old.py", "legacy_a = 1\nlegacy_b = 2\n")
        self.repo.base_sha = self.repo.commit("seed")

        self.repo.write(
            "src/a.py",
            header + "def compute_total(items):\n    return sum(items) if items else 0\n",
        )
        self.repo.write("notes.py", "LABEL = 'cart'\n")
        (self.repo.root / "old.py").unlink()
        self.review("prepare", "--tier", "quick")

        snap = snapshot(self.repo.root, self.repo.base_sha)
        by_path = {hunk.path: hunk for hunk in snap.hunks}
        a_hunk = by_path["src/a.py"]
        a_range = f"src/a.py:{a_hunk.new_start}-{a_hunk.new_start + a_hunk.new_len - 1}"

        proc = self.review("show", "--hunks")
        lines = proc.stdout.strip().splitlines()

        self.assertEqual(proc.returncode, 0)
        self.assertEqual(len(lines), 3)
        self.assertEqual(
            lines,
            [
                f"{by_path['notes.py'].id}  notes.py:1-1  added",
                f"{by_path['old.py'].id}  old.py:1-2  deleted",
                f"{a_hunk.id}  {a_range}  compute_total",
            ],
        )

        payload = json.loads(self.review("show", "--hunks", "--json").stdout)
        self.assertFalse(payload["stale"])
        self.assertEqual([item["path"] for item in payload["hunks"]], ["notes.py", "old.py", "src/a.py"])
        a_json = next(item for item in payload["hunks"] if item["path"] == "src/a.py")
        self.assertEqual(a_json["scope"], "compute_total")
        self.assertEqual(a_json["status"], "modified")
        self.assertEqual(a_json["end_line"], a_hunk.new_start + a_hunk.new_len - 1)

    def test_show_hunks_omits_carried_hunks_until_round_becomes_stale(self) -> None:
        self.repo.write("cart.py", BUGGY_CART)
        self.repo.write("notes.py", "LABEL = 'cart'\n")
        self.repo.record_full_evidence("quick")

        self.repo.write("cart.py", BUGGY_CART.replace(" / len(items)", ""))
        self.review("prepare", "--tier", "quick")

        current_lines = self.review("show", "--hunks").stdout.strip().splitlines()
        self.assertEqual(current_lines, ["H1  cart.py:1-3  total"])

        # Edit cart.py without re-running `prepare`: round 2 becomes stale.
        self.repo.write("cart.py", "def total(items):\n    return 0\n")
        stale_proc = self.review("show", "--hunks")
        stale_lines = stale_proc.stdout.strip().splitlines()

        self.assertEqual(stale_proc.returncode, 0)
        self.assertEqual(
            stale_lines,
            [
                "H1  cart.py:1-3  [open]",
                "H2  notes.py:1-1  [carried]",
                "stale: run `craftsman review prepare`",
            ],
        )

        stale_json = json.loads(self.review("show", "--hunks", "--json").stdout)
        self.assertTrue(stale_json["stale"])
        self.assertEqual(
            stale_json["hunks"],
            [
                {
                    "id": "H1",
                    "path": "cart.py",
                    "new_start": 1,
                    "new_len": 3,
                    "end_line": 3,
                    "carried": False,
                },
                {
                    "id": "H2",
                    "path": "notes.py",
                    "new_start": 1,
                    "new_len": 1,
                    "end_line": 1,
                    "carried": True,
                },
            ],
        )

    def test_show_hunks_handles_missing_round_or_flag_conflict(self) -> None:
        empty = self.review("show", "--hunks")
        empty_json = json.loads(self.review("show", "--hunks", "--json").stdout)
        conflict = self.review("show", "--packet-path", "--hunks", check=False)

        self.assertEqual(empty.returncode, 0)
        self.assertIn("No review round yet; run `craftsman review prepare`.", empty.stdout)
        self.assertEqual(empty_json["hunks"], [])
        self.assertFalse(empty_json["stale"])
        assert_cli_error(self, conflict, 1, "pass either --packet-path or --hunks, not both")


if __name__ == "__main__":
    unittest.main()
