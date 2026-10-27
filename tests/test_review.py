#!/usr/bin/env python3
"""End-to-end tests for `craftsman review`, `craftsman run`, the review packet and the gate."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from craftsman import packet, runner
from craftsman.diffscan import DiffSnapshot, Hunk, Reference, Scope
from craftsman.ledger import GENERIC_COVERAGE_WORDS, HunkRef, Round
from craftsman.lenses import select_lenses
from craftsman.packet import PacketContext, render_packet

from tests.support import REPO_ROOT, STOP_GATE, EvidenceRepo, coverage_note

BUGGY_TOTAL = '''def total(items):
    """Sum of prices."""
    return sum(item["price"] for item in items) / len(items)
'''


class RepoTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.repo = EvidenceRepo(self.tmp / "repo")

    def assert_error(
        self, proc: subprocess.CompletedProcess[str], exit_code: int, fragment: str
    ) -> None:
        self.assertEqual(proc.returncode, exit_code, proc.stderr)
        self.assertIn(fragment, proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)

    def prepare_bug(self) -> None:
        self.repo.write("cart.py", BUGGY_TOTAL)
        self.repo.cli("review", "prepare", "--tier", "quick", "--task", "Add cart totals")


class PrepareTests(RepoTestCase):
    def test_prepare_writes_a_packet_and_is_idempotent(self) -> None:
        self.repo.write("cart.py", BUGGY_TOTAL)

        first = self.repo.cli("review", "prepare", "--tier", "quick", "--task", "Add cart totals")
        again = self.repo.cli("review", "prepare", "--tier", "quick")

        self.assertIn("Round 1 prepared (quick): 1 hunks, 0 carried, 1 to review.", first.stdout)
        self.assertIn("Lenses: intent, contracts, craft\n", first.stdout)
        self.assertNotIn("sweep", first.stdout)
        self.assertIn("Round 1 unchanged (same diff and tier)", again.stdout)
        text = (self.repo.ledger_dir / "packet-1.md").read_text(encoding="utf-8")
        self.assertIn("# Craftsman review packet — round 1", text)
        self.assertIn("**Task:** Add cart totals", text)
        self.assertIn("### H1 · `cart.py` L1–L3 · added\n", text)
        self.assertIn("Scope: `total` (function, L1–L3)", text)

    def test_round_two_carries_unchanged_hunks(self) -> None:
        self.repo.write("cart.py", BUGGY_TOTAL)
        self.repo.write("notes.py", "LABEL = 'cart'\n")
        self.repo.record_full_evidence("quick")
        self.repo.write("cart.py", BUGGY_TOTAL.replace("/ len(items)", ""))

        out = self.repo.cli("review", "prepare", "--tier", "quick").stdout

        self.assertIn("Round 2 prepared (quick): 2 hunks, 1 carried, 1 to review.", out)
        text = (self.repo.ledger_dir / "packet-2.md").read_text(encoding="utf-8")
        self.assertIn("### H2 · `notes.py` L1–L1 · carried (covered in round 1, unchanged)", text)

    def test_auto_tier_follows_the_gate(self) -> None:
        self.repo.cli("gate", "--init", "--tier", "auto", "--cwd", ".")
        self.repo.write("cart.py", BUGGY_TOTAL)

        out = self.repo.cli("review", "prepare", "--tier", "auto").stdout
        bogus = self.repo.cli("review", "prepare", "--tier", "max", check=False)

        self.assertIn("Round 1 prepared (quick)", out)
        self.assert_error(bogus, 2, "invalid choice 'max' (choose from auto, quick, standard, deep)")

    def test_hunks_mentioning_a_changed_symbol_are_marked_reopened(self) -> None:
        self.repo.write("cart.py", "def item_count(items):\n    return len(items)\n")
        self.repo.write(
            "tests/test_cart.py", "from cart import item_count\n\nassert item_count([1]) == 1\n"
        )
        self.repo.record_full_evidence("quick")
        self.repo.write("cart.py", "def item_count(items):\n    return len(list(items))\n")

        out = self.repo.cli("review", "prepare", "--tier", "quick").stdout

        self.assertIn("2 hunks, 0 carried, 2 to review.", out)
        text = (self.repo.ledger_dir / "packet-2.md").read_text(encoding="utf-8")
        self.assertIn(
            "### H2 · `tests/test_cart.py` L1–L3 · added · signals: tests · reopened: mentions item_count (changed in H1)\n",
            text,
        )
        self.assertNotIn("reopened", text.split("### H2")[0])

    def test_prepare_help_mentions_the_ignore_file(self) -> None:
        help_text = self.repo.cli("review", "prepare", "--help").stdout
        self.assertIn(".craftsmanignore", " ".join(help_text.split()))

    def test_empty_diff_has_nothing_to_review(self) -> None:
        out = self.repo.cli("review", "prepare").stdout
        self.assertIn("Nothing to review", out)
        self.assertFalse((self.repo.ledger_dir / "ledger.json").exists())


class RecordingTests(RepoTestCase):
    def test_cover_add_verdict_and_resolve_happy_path(self) -> None:
        self.prepare_bug()

        covered = self.repo.cli(
            "review", "cover", "--lens", "intent", "--hunks", "H1", "--note", coverage_note(["H1"])
        )
        added = self.repo.cli(
            "review", "add", "--lens", "contracts", "--file", "cart.py", "--line", "3",
            "--severity", "major", "--title", "Empty cart divides by zero",
            "--scenario", "total([]) evaluates sum([]) / len([]) and raises ZeroDivisionError",
        )  # fmt: skip
        run = self.repo.cli(
            "run",
            "--kind",
            "repro",
            "--",
            sys.executable,
            "-c",
            "from cart import total; total([])",
            check=False,
        )
        verdict = self.repo.cli(
            "review",
            "verdict",
            "F1",
            "--as",
            "proven",
            "--run",
            "R1",
            "--evidence",
            "R1 raised ZeroDivisionError for an empty cart",
        )
        self.repo.write("cart.py", BUGGY_TOTAL.replace(" / len(items)", ""))
        resolved = self.repo.cli(
            "review",
            "resolve",
            "F1",
            "--as",
            "fixed",
            "--note",
            "Dropped the division; empty carts total 0",
        )

        self.assertIn("Coverage recorded: `intent` covered H1 in round 1", covered.stdout)
        self.assertEqual(
            added.stdout.splitlines(),
            [
                "F1 recorded (major, contracts, cart.py:3)",
                '  L3: return sum(item["price"] for item in items) / len(items)',
            ],
        )
        self.assertEqual(run.returncode, 1)
        self.assertIn("ZeroDivisionError", run.stdout)
        self.assertIn("R1 recorded (repro, exit 1", run.stderr)
        self.assertEqual(verdict.stdout.strip(), "F1 verdict recorded: proven (run R1)")
        self.assertEqual(resolved.stdout.strip(), "F1 resolved as fixed")

    def test_domain_errors_exit_1_without_traceback(self) -> None:
        self.prepare_bug()
        outside = self.tmp / "outside.py"
        outside.write_text("x = 1\n", encoding="utf-8")
        finding = (
            "--lens", "contracts", "--line", "3", "--severity", "major", "--title", "Empty cart divides by zero",
            "--scenario", "total([]) evaluates sum([]) / len([]) and raises ZeroDivisionError",
        )  # fmt: skip

        cases = [
            (("review", "cover", "--lens", "intent", "--hunks", "H7", "--note", coverage_note(["H7"])), "unknown hunk ids"),
            (("review", "cover", "--lens", "intent", "--hunks", "H1", "--note", "x" * 40), "missing: H1"),
            (("review", "add", "--file", str(outside), *finding), "outside the repository"),
            (("review", "add", "--file", "nope.py", *finding), "does not exist"),
            (("review", "verdict", "F9", "--as", "dismissed", "--evidence", "guarded at cart.py:2 already"), "unknown finding"),
            (("review", "resolve", "F9", "--as", "fixed", "--note", "Dropped the division entirely"), "unknown finding"),
        ]  # fmt: skip
        for args, fragment in cases:
            with self.subTest(args=args[:2]):
                proc = self.repo.cli(*args, check=False)
                self.assert_error(proc, 1, "craftsman review: error: ")
                self.assertIn(fragment, proc.stderr)

    def test_verdict_and_resolve_rules_are_enforced(self) -> None:
        self.prepare_bug()
        self.repo.cli(
            "review", "add", "--lens", "contracts", "--file", "cart.py", "--line", "3",
            "--severity", "blocker", "--title", "Empty cart divides by zero",
            "--scenario", "total([]) evaluates sum([]) / len([]) and raises ZeroDivisionError",
        )  # fmt: skip

        no_run = self.repo.cli(
            "review",
            "verdict",
            "F1",
            "--as",
            "proven",
            "--evidence",
            "it obviously crashes on empty input",
            check=False,
        )
        self.assert_error(no_run, 1, "required for a 'proven' verdict")
        self.repo.cli(
            "review",
            "verdict",
            "F1",
            "--as",
            "probable",
            "--evidence",
            "no-repro: cart.py:3 divides by len(items) which is 0 for []",
        )
        unchanged = self.repo.cli(
            "review",
            "resolve",
            "F1",
            "--as",
            "fixed",
            "--note",
            "Dropped the division entirely",
            check=False,
        )
        self.assert_error(unchanged, 1, "fix the code first, then resolve")
        accepted = self.repo.cli(
            "review",
            "resolve",
            "F1",
            "--as",
            "accepted",
            "--note",
            "Callers never pass an empty cart",
            check=False,
        )
        self.assert_error(accepted, 1, "not allowed for blocker findings")

    def test_merge_question_and_static_proven_verdict(self) -> None:
        self.prepare_bug()
        self.repo.cli(
            "review", "add", "--lens", "intent", "--file", "cart.py", "--line", "3",
            "--severity", "minor", "--title", "Total returns average instead of sum",
            "--scenario", "total([{'price': 10}, {'price': 20}]) returns 15.0 instead of 30",
        )  # fmt: skip
        self.repo.cli(
            "review", "add", "--lens", "contracts", "--file", "cart.py", "--line", "3",
            "--severity", "major", "--title", "Division changes return type to float",
            "--scenario", "Integer prices produce a float result because of / len(items)",
        )  # fmt: skip
        self.repo.cli(
            "review", "add", "--lens", "intent", "--file", "cart.py", "--line", "3",
            "--severity", "minor", "--title", "Intentional averaging per task",
            "--scenario", "If averaging was requested, the function name total() is misleading",
        )  # fmt: skip

        merged = self.repo.cli("review", "merge", "F1", "F2")
        self.assertEqual(merged.stdout.strip(), "F2 merged into F1 (major, cart.py:3)")

        probable = self.repo.cli(
            "review",
            "verdict",
            "F1",
            "--as",
            "probable",
            "--evidence",
            "static: cart.py:3 divides sum(item['price']) by len(items), returning the mean",
        )
        self.assertEqual(probable.stdout.strip(), "F1 verdict recorded: probable")

        rejected_merge = self.repo.cli("review", "merge", "F1", "F3", check=False)
        self.assert_error(
            rejected_merge,
            1,
            "merge only works on findings without a verdict (before verification); F1 already has a verdict or resolution (probable)",
        )

        question = self.repo.cli(
            "review",
            "verdict",
            "F3",
            "--as",
            "question",
            "--evidence",
            "intent: PR task asks for mean price; cart.py:3 implements it under the name total()",
        )
        self.assertEqual(question.stdout.strip(), "F3 verdict recorded: question")

        report_md = self.repo.cli("review", "report").stdout
        report_json = json.loads(self.repo.cli("review", "report", "--json").stdout)
        self.assertIn("**Question (1)**", report_md)
        self.assertEqual([f["id"] for f in report_json["findings"]["question"]], ["F3"])
        self.assertEqual([f["id"] for f in report_json["findings"]["dismissed"]], ["F2"])

    def test_validation_errors_name_the_flag(self) -> None:
        self.prepare_bug()
        short_title = self.repo.cli(
            "review", "add", "--lens", "contracts", "--file", "cart.py", "--line", "3",
            "--severity", "major", "--title", "Bad", "--scenario", "total([]) raises ZeroDivisionError",
            check=False,
        )  # fmt: skip
        self.assert_error(short_title, 1, "error: --title must have 8..160 characters (got 3)")
        self.repo.cli(
            "review", "add", "--lens", "contracts", "--file", "cart.py", "--line", "3",
            "--severity", "major", "--title", "Empty cart divides by zero",
            "--scenario", "total([]) evaluates sum([]) / len([]) and raises ZeroDivisionError",
        )  # fmt: skip
        short_evidence = self.repo.cli(
            "review", "verdict", "F1", "--as", "dismissed", "--evidence", "fine", check=False
        )
        self.assert_error(
            short_evidence, 1, "error: --evidence must have at least 20 characters (got 4)"
        )
        self.assertNotIn("verdict_evidence", short_evidence.stderr)

    def test_usage_errors_exit_2(self) -> None:
        missing_lens = self.repo.cli("review", "cover", "--hunks", "H1", "--note", "x", check=False)
        bad_severity = self.repo.cli("review", "add", "--lens", "intent", "--file", "a", "--line", "1", "--severity", "huge", "--title", "t", "--scenario", "s", check=False)  # fmt: skip
        self.assert_error(missing_lens, 2, "the following arguments are required: --lens")
        self.assert_error(bad_severity, 2, "invalid choice 'huge'")


class InspectionTests(RepoTestCase):
    def test_show_check_report_and_reset(self) -> None:
        self.prepare_bug()
        blocked = self.repo.cli("review", "check", check=False)
        self.assertEqual(blocked.returncode, 1)
        self.assertIn("**Craftsman gate: BLOCKED", blocked.stdout)
        self.assertIn("[uncovered] Lens `intent`", blocked.stdout)

        shown = self.repo.cli("review", "show").stdout
        self.assertIn("Round 1 (quick) prepared", shown)
        self.assertIn("intent     0/1 (missing H1)", shown)
        self.assertIn("Findings: none recorded.", shown)

        self.repo.cover_all()
        self.repo.cli("run", "--kind", "test", "--", sys.executable, "-c", "print('ok')")
        passed = json.loads(self.repo.cli("review", "check", "--json").stdout)
        self.assertTrue(passed["passed"])
        self.assertEqual(
            self.repo.show()["coverage"]["craft"], {"covered": 1, "total": 1, "missing": []}
        )

        report = self.repo.cli("review", "report").stdout
        self.assertIn("# Craftsman review report", report)
        self.assertIn("- Gate: PASSED", report)
        self.assertIn("No findings recorded.", report)
        self.assertIn("(test) → exit 0", report)

        reset = self.repo.cli("review", "reset").stdout
        self.assertIn("Archived to", reset)
        self.assertFalse((self.repo.ledger_dir / "ledger.json").exists())
        self.assertEqual(len(list(self.repo.ledger_dir.glob("ledger-*.json"))), 1)
        self.assertEqual(len(list(self.repo.ledger_dir.glob("archive-*/packet-1.md"))), 1)
        self.assertIn("Nothing to archive", self.repo.cli("review", "reset").stdout)


FINDING_ARGS = (
    "--lens", "contracts", "--severity", "major", "--title", "Empty cart divides by zero",
    "--scenario", "total([]) evaluates sum([]) / len([]) and raises ZeroDivisionError",
)  # fmt: skip


class HardeningTests(RepoTestCase):
    def test_reverting_every_change_leaves_nothing_to_review(self) -> None:
        self.prepare_bug()
        (self.repo.root / "cart.py").unlink()

        prepared = self.repo.cli("review", "prepare").stdout
        checked = self.repo.cli("review", "check", check=False)
        shown = self.repo.cli("review", "show").stdout

        self.assertIn("Nothing to review", prepared)
        self.assertEqual(checked.returncode, 0, checked.stdout)
        self.assertNotIn("stale", shown)
        self.assertIn("nothing left to review", shown)

    def test_prepare_rewrites_a_missing_packet(self) -> None:
        self.prepare_bug()
        packet_file = self.repo.ledger_dir / "packet-1.md"
        packet_file.unlink()

        out = self.repo.cli("review", "prepare", "--tier", "quick").stdout

        self.assertIn("Round 1 unchanged", out)
        self.assertTrue(packet_file.is_file())

    def test_commands_keep_using_the_base_the_round_was_prepared_against(self) -> None:
        self.repo.write("cart.py", BUGGY_TOTAL)
        self.repo.git("add", "-A")
        self.repo.git("commit", "-q", "--no-verify", "-m", "cart")
        self.repo.cli("gate", "--init", "--tier", "quick", "--cwd", ".")
        self.repo.write("notes.py", "LABEL = 'cart'\n")

        self.repo.cli("review", "prepare", "--tier", "quick", "--base", self.repo.base_sha)
        self.repo.cover_all()
        self.repo.cli("run", "--kind", "test", "--", sys.executable, "-c", "print('ok')")

        self.assertNotIn("stale", self.repo.cli("review", "show").stdout)
        checked = self.repo.cli("review", "check", check=False)
        self.assertEqual(checked.returncode, 0, checked.stdout)

    def test_check_never_evaluates_below_the_task_tier(self) -> None:
        self.repo.cli("gate", "--init", "--tier", "standard", "--cwd", ".")
        self.repo.write("cart.py", BUGGY_TOTAL)
        self.repo.record_full_evidence("quick")

        lowered = self.repo.cli("review", "check", "--tier", "quick", check=False)

        self.assertEqual(lowered.returncode, 1)
        self.assertIn("[tier-too-low]", lowered.stdout)

    def test_a_deeper_round_is_held_to_its_own_tier(self) -> None:
        state_file = self.repo.ledger_dir / "craftsman_state.json"
        self.repo.write("cart.py", BUGGY_TOTAL)
        self.repo.gate(state_file, "--init", "--tier", "standard", "--cwd", str(self.repo.root))
        self.repo.cli("review", "prepare", "--tier", "deep")
        self.repo.cover_all()
        self.repo.cli("run", "--kind", "test", "--", sys.executable, "-c", "print('ok')")

        checked = self.repo.cli("review", "check", check=False)
        report = json.loads(self.repo.cli("review", "report", "--json").stdout)
        completed = self.repo.gate(state_file, "--complete")

        self.assertIn("[no-e2e-run]", checked.stdout)
        self.assertEqual(report["tier"], "deep")
        self.assertEqual(completed.returncode, 1)
        self.assertIn("[no-e2e-run]", completed.stderr)

    def test_a_run_that_modifies_the_tree_does_not_count(self) -> None:
        self.repo.write("cart.py", BUGGY_TOTAL)
        self.repo.cli("review", "prepare", "--tier", "quick")
        self.repo.cover_all()
        make = (sys.executable, "-c", "open('made.txt', 'w').write('x')")

        first = self.repo.cli("run", "--kind", "test", "--", *make)

        self.assertIn("modified the working tree", first.stderr)
        (run,) = self.repo.show()["runs"]
        self.assertIsNotNone(run["fingerprint_after"])
        self.repo.cli("review", "prepare", "--tier", "quick")
        self.repo.cover_all()
        self.assertIn("[no-passing-run]", self.repo.cli("review", "check", check=False).stdout)
        self.repo.cli("run", "--kind", "test", "--", *make)
        self.assertEqual(self.repo.cli("review", "check", check=False).returncode, 0)

    def test_cover_refuses_a_stale_round(self) -> None:
        self.prepare_bug()
        self.repo.write("cart.py", BUGGY_TOTAL + "# edited\n")

        proc = self.repo.cli(
            "review", "cover", "--lens", "intent", "--hunks", "H1", "--note", coverage_note(["H1"]),
            check=False,
        )  # fmt: skip

        self.assert_error(proc, 1, "Code changed since round 1 was prepared")

    def test_add_resolves_files_against_cwd_and_accepts_deleted_files(self) -> None:
        self.repo.write("pkg/cart.py", BUGGY_TOTAL)
        self.repo.git("add", "-A")
        self.repo.git("commit", "-q", "--no-verify", "-m", "cart")
        self.repo.cli("gate", "--init", "--tier", "quick", "--cwd", ".")
        (self.repo.root / "pkg" / "cart.py").unlink()
        self.repo.write("pkg/total.py", BUGGY_TOTAL)
        self.repo.cli("review", "prepare", "--tier", "quick")
        pkg = str(self.repo.root / "pkg")

        live = self.repo.cli("review", "add", "--cwd", pkg, "--file", "total.py", "--line", "3", *FINDING_ARGS)
        gone = self.repo.cli("review", "add", "--cwd", pkg, "--file", "cart.py", "--line", "3", *FINDING_ARGS)

        self.assertIn("F1 recorded (major, contracts, pkg/total.py:3)", live.stdout)
        self.assertIn("F2 recorded (major, contracts, pkg/cart.py:3)", gone.stdout)
        self.assertIn("deleted in the diff; line 3 refers to the base version", gone.stdout)

    @unittest.skipIf(sys.platform == "win32", "symlinks need extra privileges on Windows")
    def test_add_keeps_the_path_of_a_symlink(self) -> None:
        self.repo.write("cart.py", BUGGY_TOTAL)
        outside = self.tmp / "shared.py"
        outside.write_text(BUGGY_TOTAL, encoding="utf-8")
        (self.repo.root / "linked.py").symlink_to(outside)
        self.repo.cli("review", "prepare", "--tier", "quick")

        added = self.repo.cli("review", "add", "--file", "linked.py", "--line", "3", *FINDING_ARGS)

        self.assertIn("F1 recorded (major, contracts, linked.py:3)", added.stdout)

    def test_add_refuses_a_line_past_the_end_of_the_file(self) -> None:
        self.repo.write("cart.py", BUGGY_TOTAL)
        (self.repo.root / "feed.py").write_bytes(b"a = 1\x0cb = 2\nc = 3\n")
        self.repo.cli("review", "prepare", "--tier", "quick")

        past = self.repo.cli("review", "add", "--file", "cart.py", "--line", "40", *FINDING_ARGS, check=False)
        feed = self.repo.cli("review", "add", "--file", "feed.py", "--line", "3", *FINDING_ARGS, check=False)

        self.assert_error(past, 1, "error: --line 40 is past the end of cart.py, which has 3 lines")
        self.assert_error(feed, 1, "error: --line 3 is past the end of feed.py, which has 2 lines")
        self.assertEqual(self.repo.show()["findings"], [])

    def test_move_corrects_the_location_of_an_unjudged_finding(self) -> None:
        self.prepare_bug()
        self.repo.write("other.py", "x = 1\n")
        self.repo.cli("review", "prepare", "--tier", "quick")
        self.repo.cli("review", "add", "--file", "cart.py", "--line", "1", *FINDING_ARGS)

        line = self.repo.cli("review", "move", "F1", "--line", "3")
        file = self.repo.cli("review", "move", "F1", "--file", "other.py", "--line", "1")
        past = self.repo.cli("review", "move", "F1", "--line", "9", check=False)
        zero = self.repo.cli("review", "move", "F1", "--line", "0", check=False)

        self.assertIn("F1 now points at cart.py:3", line.stdout)
        self.assertIn("L3: return sum(", line.stdout)
        self.assertIn("F1 now points at other.py:1", file.stdout)
        self.assert_error(past, 1, "error: --line 9 is past the end of other.py, which has 1 lines")
        self.assert_error(zero, 1, "error: --line must be >= 1 (got 0)")
        (finding,) = self.repo.show()["findings"]
        self.assertEqual((finding["path"], finding["line"]), ("other.py", 1))

    def test_move_refuses_a_judged_finding_or_changed_code(self) -> None:
        self.prepare_bug()
        self.repo.cli("review", "add", "--file", "cart.py", "--line", "3", *FINDING_ARGS)
        self.repo.cli("review", "add", "--file", "cart.py", "--line", "3", *FINDING_ARGS)
        self.repo.cli("review", "verdict", "F1", "--as", "dismissed", "--evidence", "cart.py:3 already guards the empty list")

        judged = self.repo.cli("review", "move", "F1", "--line", "2", check=False)
        self.repo.write("cart.py", BUGGY_TOTAL + "# edited\n")
        changed = self.repo.cli("review", "move", "F2", "--line", "2", check=False)
        unknown = self.repo.cli("review", "move", "F9", "--line", "2", check=False)

        self.assert_error(judged, 1, "F1 already has a verdict (dismissed); record a new finding instead")
        self.assert_error(changed, 1, "the code changed since F2 was recorded")
        self.assert_error(unknown, 1, "unknown finding 'F9'")
        self.assertEqual([f["line"] for f in self.repo.show()["findings"]], [3, 3])

    def test_report_lists_each_command_and_directory_by_its_latest_run(self) -> None:
        self.repo.write("cart.py", BUGGY_TOTAL)
        (self.repo.root / "pkg_a").mkdir()
        (self.repo.root / "pkg_b").mkdir()
        self.repo.cli("review", "prepare", "--tier", "quick")
        self.repo.cover_all()
        code_file = self.tmp / "exit_code"
        flaky = (sys.executable, "-c", f"import sys; sys.exit(int(open({str(code_file)!r}).read()))")
        steady = (sys.executable, "-c", "print('ok')")

        code_file.write_text("0", encoding="utf-8")
        self.repo.cli("run", "--kind", "test", "--", *flaky)
        code_file.write_text("1", encoding="utf-8")
        self.repo.cli("run", "--kind", "test", "--", *flaky, check=False)
        for folder in ("pkg_a", "pkg_b"):
            self.repo.cli("run", "--kind", "test", "--cwd", str(self.repo.root / folder), "--", *steady)

        report = json.loads(self.repo.cli("review", "report", "--json").stdout)

        self.assertEqual([run["id"] for run in report["runs"]], ["R3", "R4"])

    def test_findings_table_escapes_pipes_and_newlines(self) -> None:
        self.prepare_bug()
        self.repo.cli(
            "review", "add", "--file", "cart.py", "--line", "3", "--lens", "contracts",
            "--severity", "major", "--title", "a | b\nbreaks the table",
            "--scenario", "total([]) evaluates sum([]) / len([]) and raises ZeroDivisionError",
        )  # fmt: skip

        shown = self.repo.cli("review", "show").stdout

        self.assertIn("| a \\| b breaks the table |", shown)

    def test_waived_runs_unblock_the_gate_and_appear_in_the_report(self) -> None:
        self.repo.write("cart.py", BUGGY_TOTAL)
        self.repo.cli("review", "prepare", "--tier", "quick")
        self.repo.cover_all()
        self.repo.cli("run", "--kind", "test", "--", sys.executable, "tests/test_typo.py", check=False)
        self.repo.cli("run", "--kind", "test", "--", sys.executable, "-c", "print('ok')")
        blocked = self.repo.cli("review", "check", check=False).stdout
        self.assertIn("craftsman review waive-run R1", blocked)

        short = self.repo.cli("review", "waive-run", "R1", "--reason", "typo", check=False)
        waived = self.repo.cli(
            "review", "waive-run", "R1", "--reason", "Mistyped path; the suite lives in tests/test_cart.py"
        )

        self.assert_error(short, 1, "error: --reason must have at least 20 characters")
        self.assertEqual(waived.stdout.strip(), "R1 waived; it no longer blocks the gate")
        self.assertEqual(self.repo.cli("review", "check", check=False).returncode, 0)
        report = self.repo.cli("review", "report").stdout
        self.assertIn("## Waived runs", report)
        self.assertIn("Mistyped path; the suite lives in tests/test_cart.py", report)
        payload = json.loads(self.repo.cli("review", "report", "--json").stdout)
        self.assertEqual([run["id"] for run in payload["waived_runs"]], ["R1"])

    def test_reports_survive_a_console_that_cannot_encode_them(self) -> None:
        self.prepare_bug()
        env = {**os.environ, "PYTHONPATH": str(REPO_ROOT), "PYTHONIOENCODING": "ascii"}

        proc = subprocess.run(
            [sys.executable, "-m", "craftsman.cli", "review", "report"],
            cwd=self.repo.root,
            env=env,
            capture_output=True,
            check=False,
            timeout=60,
        )

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(b"# Craftsman review report", proc.stdout)

    def test_check_and_report_review_only_mode_vs_full_gate(self) -> None:
        self.prepare_bug()
        incomplete_ro = self.repo.cli("review", "check", "--review-only", check=False)
        self.assertEqual(incomplete_ro.returncode, 1)
        self.assertIn("**Craftsman review: INCOMPLETE (3 open items)**", incomplete_ro.stdout)
        incomplete_ro_report = self.repo.cli("review", "report", "--review-only").stdout
        self.assertIn(
            "- Review: incomplete — 3 open items (uncovered); see `craftsman review check --review-only`",
            incomplete_ro_report,
        )

        static_ev = "static: cart.py:3 divides by len(items), which raises ZeroDivisionError on []"
        for lens in ("intent", "contracts", "craft"):
            self.repo.cli("review", "cover", "--lens", lens, "--hunks", "H1", "--note", coverage_note(["H1"]))
        self.repo.cli("review", "add", "--file", "cart.py", "--line", "3", *FINDING_ARGS)
        self.repo.cli("review", "verdict", "F1", "--as", "probable", "--evidence", static_ev)

        full_check = self.repo.cli("review", "check", check=False)
        self.assertEqual(full_check.returncode, 1)
        self.assertIn("[unresolved]", full_check.stdout)
        self.assertIn("[no-passing-run]", full_check.stdout)

        ro_check = self.repo.cli("review", "check", "--review-only")
        self.assertEqual(ro_check.returncode, 0)
        self.assertIn("**Craftsman review: COMPLETE (review-only)**", ro_check.stdout)
        self.assertNotIn("Craftsman gate: PASSED", ro_check.stdout)
        self.assertIn("1 finding for the author: F1.", ro_check.stdout)

        ro_report = self.repo.cli("review", "report", "--review-only").stdout
        self.assertIn("- Review: complete (review-only)", ro_report)
        self.assertNotIn("- Gate: PASSED", ro_report)
        self.assertNotIn("BLOCKED", ro_report)
        self.assertIn("**Open (1)**", ro_report)
        self.assertIn("1 finding for the author: F1.", ro_report)

        self.repo.cli("gate", "--init", "--tier", "quick", "--cwd", ".")
        active_ro = self.repo.cli("review", "check", "--review-only", check=False)
        self.assert_error(
            active_ro,
            1,
            "--review-only is not allowed during an active /craftsman gate session",
        )
        active_complete = self.repo.cli("gate", "--complete", "--cwd", ".", check=False)
        self.assertEqual(active_complete.returncode, 1)
        self.assertIn("[unresolved]", active_complete.stderr)

    def test_from_json_bulk_cover_add_and_verdict_via_stdin(self) -> None:
        self.prepare_bug()
        static_ev = "static: cart.py:3 divides by len(items), returning average instead of sum"
        norepro_ev = (
            "no-repro: needs concurrent database sessions; cart.py:3 divides by len(items) == 0"
        )
        question_ev = (
            "intent: PR task says 'Add cart totals'; cart.py:1 accepts untyped iterable items"
        )
        cover_payload = json.dumps(
            [
                {"lens": "intent", "hunks": ["H1"], "note": coverage_note(["H1"])},
                {"lens": "contracts", "hunks": ["H1"], "note": coverage_note(["H1"])},
                {"lens": "craft", "hunks": ["H1"], "note": coverage_note(["H1"])},
            ]
        )
        covered = self.repo.cli("review", "cover", "--from-json", "-", stdin=cover_payload)
        self.assertEqual(len(covered.stdout.strip().splitlines()), 3)
        self.assertIn("`craft` covered H1 in round 1; 0 lens-hunk pair(s) left", covered.stdout)

        add_payload = json.dumps(
            [
                {
                    "lens": "intent",
                    "file": "cart.py",
                    "line": 1,
                    "severity": "minor",
                    "title": "Should total() accept any iterable or only sequences?",
                    "scenario": "Passing a generator to total(g) fails at len(items) on cart.py:3",
                },
                {
                    "lens": "contracts",
                    "file": "cart.py",
                    "line": 2,
                    "severity": "major",
                    "title": "Empty list crashes on len(items)",
                    "scenario": "total([]) divides by zero at len(items) and raises ZeroDivisionError",
                },
                {
                    "lens": "craft",
                    "file": "cart.py",
                    "line": 3,
                    "severity": "minor",
                    "title": "Sum divided by length returns average instead of total",
                    "scenario": "total([10, 20]) returns 15.0 instead of 30 because of / len(items)",
                    "suggestion": "Return sum(items) without dividing by len(items).",
                },
            ]
        )
        json_file = self.tmp / "findings.json"
        json_file.write_text(add_payload, encoding="utf-8")
        added = self.repo.cli("review", "add", "--from-json", str(json_file))
        self.assertIn("F1 recorded (minor, intent, cart.py:1)", added.stdout)
        self.assertIn("L1: def total(items):", added.stdout)
        self.assertIn("F2 recorded (major, contracts, cart.py:2)", added.stdout)
        self.assertIn('L2: """Sum of prices."""', added.stdout)
        self.assertIn("F3 recorded (minor, craft, cart.py:3)", added.stdout)
        self.assertIn('L3: return sum(item["price"] for item in items) / len(items)', added.stdout)

        verdict_payload = json.dumps(
            [
                {"id": "F1", "as": "question", "evidence": question_ev},
                {"id": "F2", "as": "probable", "evidence": norepro_ev},
                {"id": "F3", "as": "probable", "evidence": static_ev},
            ]
        )
        judged = self.repo.cli("review", "verdict", "--from-json", "-", stdin=verdict_payload)
        self.assertEqual(
            judged.stdout.strip().splitlines(),
            [
                "F1 verdict recorded: question",
                "F2 verdict recorded: probable",
                "F3 verdict recorded: probable",
            ],
        )
        self.assertEqual(self.repo.cli("review", "check", "--review-only").returncode, 0)

    def test_from_json_atomic_failure_and_flag_validation(self) -> None:
        self.prepare_bug()
        bad_cover = json.dumps(
            [
                {"lens": "intent", "hunks": ["H1"], "note": coverage_note(["H1"])},
                {
                    "lens": "contracts",
                    "hunks": ["H1", "H2", "H3", "H4", "H5"],
                    "note": coverage_note(["H1", "H2", "H3", "H4", "H5"]),
                },
            ]
        )
        failed_cover = self.repo.cli("review", "cover", "--from-json", "-", stdin=bad_cover, check=False)
        self.assert_error(
            failed_cover, 1, "entry 2 (contracts): hunks: cover 1..4 hunks per call (got 5)"
        )
        self.assertEqual(self.repo.show()["coverage"]["intent"]["covered"], 0)

        bad_add = json.dumps(
            [
                {
                    "lens": "contracts",
                    "file": "cart.py",
                    "line": 3,
                    "severity": "major",
                    "title": "Valid first finding",
                    "scenario": "total([10, 20]) returns 15.0 instead of 30 because of / len(items)",
                },
                {
                    "lens": "contracts",
                    "file": "cart.py",
                    "line": 3,
                    "severity": "critical",
                    "title": "Invalid second finding",
                    "scenario": "total([10, 20]) returns 15.0 instead of 30 because of / len(items)",
                },
            ]
        )
        failed = self.repo.cli("review", "add", "--from-json", "-", stdin=bad_add, check=False)
        self.assert_error(failed, 1, "entry 2 (F2): severity: must be one of blocker, major, minor, nit")
        self.assertEqual(self.repo.show()["findings"], [])

        self.repo.cli("review", "add", "--file", "cart.py", "--line", "3", *FINDING_ARGS)
        self.repo.cli("review", "add", "--file", "cart.py", "--line", "3", *FINDING_ARGS)
        bad_verdict = json.dumps(
            [
                {"id": "F1", "as": "dismissed", "evidence": "cart.py:3 is guarded by caller"},
                {
                    "id": "F2",
                    "as": "proven",
                    "evidence": "Reproduced ZeroDivisionError on empty list at cart.py:3",
                },
            ]
        )
        failed_verdict = self.repo.cli(
            "review", "verdict", "--from-json", "-", stdin=bad_verdict, check=False
        )
        self.assert_error(
            failed_verdict, 1, "entry 2 (F2): run: is required for a 'proven' verdict"
        )
        self.assertEqual([f["verdict"] for f in self.repo.show()["findings"]], [None, None])

        unknown_key = json.dumps(
            [{"lens": "intent", "hunks": ["H1"], "note": coverage_note(["H1"]), "extra": 1}]
        )
        self.assert_error(
            self.repo.cli("review", "cover", "--from-json", "-", stdin=unknown_key, check=False),
            1,
            "entry 1 (intent): extra: is not a known field",
        )
        self.assert_error(
            self.repo.cli("review", "cover", "--from-json", "-", stdin="[]", check=False),
            1,
            "--from-json list must not be empty",
        )
        self.assert_error(
            self.repo.cli("review", "cover", "--from-json", "-", "--lens", "intent", stdin="[]", check=False),
            1,
            "--from-json cannot be combined with --lens, --hunks or --note",
        )
        self.assert_error(
            self.repo.cli("review", "add", "--from-json", "-", "--file", "cart.py", stdin="[]", check=False),
            1,
            "--from-json cannot be combined with --lens, --file",
        )
        self.assert_error(
            self.repo.cli("review", "verdict", "F1", "--from-json", "-", stdin="[]", check=False),
            1,
            "--from-json cannot be combined with ID, --as, --evidence or --run",
        )
        self.repo.write("cart.py", BUGGY_TOTAL + "# edited\n")
        stale_add = self.repo.cli(
            "review", "add", "--file", "cart.py", "--line", "3", *FINDING_ARGS, check=False
        )
        self.assert_error(stale_add, 1, "Code changed since round 1 was prepared")


class RunTests(RepoTestCase):
    def runs(self) -> list[dict[str, object]]:
        runs = self.repo.show()["runs"]
        assert isinstance(runs, list)
        return runs

    def test_exit_code_and_output_tail_are_recorded(self) -> None:
        self.repo.write("cart.py", BUGGY_TOTAL)
        script = "import sys; print('x' * 5000); print('END'); sys.exit(3)"

        proc = self.repo.cli("run", "--kind", "test", "--", sys.executable, "-c", script, check=False)

        self.assertEqual(proc.returncode, 3)
        self.assertIn("END", proc.stdout)
        (run,) = self.runs()
        self.assertEqual((run["id"], run["kind"], run["exit_code"]), ("R1", "test", 3))
        tail = run["output_tail"]
        assert isinstance(tail, str)
        self.assertEqual(len(tail), 4000)
        self.assertTrue(tail.rstrip().endswith("END"))

    def test_timeout_is_recorded_as_124(self) -> None:
        proc = self.repo.cli(
            "run",
            "--timeout",
            "0.5",
            "--",
            sys.executable,
            "-c",
            "import time; time.sleep(30)",
            check=False,
        )
        self.assertEqual(proc.returncode, 124)
        self.assertIn("timed out after 0.5s", proc.stderr)
        self.assertEqual(self.runs()[0]["exit_code"], 124)

    def test_modifying_the_work_tree_is_reported(self) -> None:
        proc = self.repo.cli("run", "--", sys.executable, "-c", "open('made.txt', 'w').write('x')")
        self.assertIn("the command modified the working tree (made.txt)", proc.stderr)
        self.assertIn(".craftsmanignore", proc.stderr)
        self.assertEqual(len(self.runs()), 1)

    def test_missing_executable_records_nothing(self) -> None:
        proc = self.repo.cli("run", "--", "definitely-not-a-real-command-xyz", check=False)
        self.assert_error(proc, 127, "cannot start 'definitely-not-a-real-command-xyz'")
        self.assertEqual(self.runs(), [])

    def test_usage_errors(self) -> None:
        self.assert_error(self.repo.cli("run", "pytest", check=False), 2, "put `--` between")
        self.assert_error(self.repo.cli("run", "--", check=False), 2, "missing command after `--`")

    def test_trivial_commands_are_flagged(self) -> None:
        proc = self.repo.cli("run", "--kind", "test", "--", "echo", "ok")
        self.assertIn("is trivial and does not count as verification", proc.stderr)

    def test_help_is_printed_without_a_command(self) -> None:
        for flag in ("-h", "--help"):
            with self.subTest(flag=flag):
                proc = self.repo.cli("run", flag)
                self.assertIn("usage: craftsman run", proc.stdout)

    def test_a_relative_program_path_is_relative_to_cwd(self) -> None:
        cwd = self.tmp / "work"
        relative = os.path.join("bin", "tool")

        self.assertEqual(runner._program(relative, cwd), str(cwd / "bin" / "tool"))
        if os.name != "nt":
            self.assertEqual(runner._program("pytest", cwd), "pytest")

    @unittest.skipIf(os.name == "nt", "process groups are POSIX-only")
    def test_stop_tolerates_a_group_that_exits_before_the_kill(self) -> None:
        process = mock.Mock(pid=4242)
        process.wait.side_effect = [subprocess.TimeoutExpired("cmd", 5.0), 0]

        with mock.patch("craftsman.runner.os.killpg", side_effect=[None, ProcessLookupError()]):
            runner._stop(process, graceful=True)

        self.assertEqual(process.wait.call_count, 2)

    def test_a_closed_output_pipe_does_not_stall_the_command(self) -> None:
        script = "import sys; sys.stdout.write('x' * 1000000); print('done')"
        env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
        proc = subprocess.Popen(
            [sys.executable, "-m", "craftsman.cli", "run", "--timeout", "40", "--", sys.executable, "-c", script],
            cwd=self.repo.root,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert proc.stdout is not None and proc.stderr is not None
        try:
            proc.stdout.read(10)
            proc.stdout.close()
            proc.wait(timeout=30)
        finally:
            proc.kill()
            proc.wait()
            errors = proc.stderr.read().decode("utf-8", errors="replace")
            proc.stderr.close()

        self.assertIn("R1 recorded (other, exit 0", errors)
        self.assertNotIn("Traceback", errors)
        self.assertEqual(self.runs()[0]["exit_code"], 0)

    @unittest.skipIf(os.name == "nt", "uses a POSIX shell")
    def test_a_leftover_process_holding_the_output_does_not_hang_the_run(self) -> None:
        started = time.monotonic()

        proc = self.repo.cli("run", "--", "sh", "-c", "sleep 8 & echo started")

        self.assertLess(time.monotonic() - started, runner.READER_JOIN_TIMEOUT_S + 2.5)
        self.assertEqual(self.runs()[0]["output_tail"], "started\n")
        self.assertIn("R1 recorded", proc.stderr)


class GateIntegrationTests(RepoTestCase):
    def state(self) -> dict[str, object]:
        loaded = json.loads((self.repo.ledger_dir / "craftsman_state.json").read_text(encoding="utf-8"))
        assert isinstance(loaded, dict)
        return loaded

    def test_init_stores_the_task_and_archives_a_ledger_from_another_base(self) -> None:
        state_file = self.repo.ledger_dir / "craftsman_state.json"
        self.repo.write("cart.py", BUGGY_TOTAL)
        self.repo.cli("gate", "--init", "--tier", "quick", "--cwd", ".", "--task", "Add cart totals")
        self.assertEqual(self.state()["task"], "Add cart totals")
        self.assertEqual(self.state()["cwd"], str(self.repo.root))
        self.repo.record_full_evidence("quick")
        packet_text = (self.repo.ledger_dir / "packet-1.md").read_text(encoding="utf-8")
        self.assertIn("**Task:** Add cart totals", packet_text)

        same_base = self.repo.gate(state_file, "--init", "--cwd", str(self.repo.root))
        self.assertNotIn("Archived", same_base.stderr)
        self.assertTrue((self.repo.ledger_dir / "ledger.json").exists())

        self.repo.git("add", "-A")
        self.repo.git("commit", "-q", "--no-verify", "-m", "cart")
        new_base = self.repo.gate(state_file, "--init", "--cwd", str(self.repo.root))
        self.assertIn("Archived previous review evidence", new_base.stderr)
        self.assertFalse((self.repo.ledger_dir / "ledger.json").exists())

    def test_init_with_an_explicit_base_archives_against_that_base(self) -> None:
        state_file = self.repo.ledger_dir / "craftsman_state.json"
        self.repo.write("cart.py", BUGGY_TOTAL)
        self.repo.cli("gate", "--init", "--tier", "quick", "--cwd", ".")
        self.repo.cli("review", "prepare", "--tier", "quick")
        self.repo.git("add", "-A")
        self.repo.git("commit", "-q", "--no-verify", "-m", "cart")
        head = self.repo.git("rev-parse", "HEAD").strip()

        reinit = self.repo.gate(state_file, "--init", "--base-sha", head, "--cwd", str(self.repo.root))

        self.assertIn("Archived previous review evidence", reinit.stderr)
        self.assertEqual(self.state()["base_sha"], head)

    def test_complete_refuses_until_the_ledger_passes(self) -> None:
        state_file = self.repo.ledger_dir / "craftsman_state.json"
        self.repo.write("cart.py", BUGGY_TOTAL)
        self.repo.gate(state_file, "--init", "--tier", "quick", "--cwd", str(self.repo.root))

        refused = self.repo.gate(state_file, "--complete")
        self.assertEqual(refused.returncode, 1)
        self.assertIn("[CRAFTSMAN GATE] Cannot complete:", refused.stderr)
        self.assertIn("[no-round]", refused.stderr)
        self.assertTrue(self.state()["active"])

        self.repo.record_full_evidence("quick")
        completed = self.repo.gate(state_file, "--complete")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(self.state()["stage"], "completed")
        self.assertEqual(
            completed.stdout,
            "Craftsman gate closed: round 1, tier quick, 0 findings fixed, 0 accepted.\n",
        )

    def test_stop_hook_lists_next_actions_and_allows_at_round_cap(self) -> None:
        state_file = self.repo.ledger_dir / "craftsman_state.json"
        self.repo.write("cart.py", BUGGY_TOTAL)
        self.repo.gate(state_file, "--init", "--tier", "quick", "--cwd", str(self.repo.root))
        self.repo.cli("review", "prepare")
        payload = {
            "terminationReason": "NO_TOOL_CALL",
            "fullyIdle": True,
            "artifactDirectoryPath": str(self.repo.ledger_dir),
        }

        blocked = self.stop(payload)
        self.assertEqual(blocked["decision"], "continue")
        reason = blocked["reason"]
        assert isinstance(reason, str)
        self.assertIn("[CRAFTSMAN GATE 1/3]", reason)
        self.assertIn("1. [uncovered] Lens `intent`", reason)
        self.assertLessEqual(len(reason), 2000)

        for index in range(6):
            self.repo.write("cart.py", BUGGY_TOTAL + f"# revision {index}\n")
            self.repo.cli("review", "prepare")
        capped = self.stop(payload)
        self.assertEqual(capped["decision"], "allow")
        self.assertEqual(self.state()["stage"], "needs_human")

    def stop(self, payload: dict[str, object]) -> dict[str, object]:
        proc = subprocess.run(
            [sys.executable, str(STOP_GATE)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )
        decision = json.loads(proc.stdout)
        assert isinstance(decision, dict)
        return decision


class PacketRenderingTests(unittest.TestCase):
    def context(self, *, scope_lines: int = 3) -> PacketContext:
        hunks = (
            Hunk("H1", "pkg/a.py", None, "modified", 1, 1, 1, 2, "def f():", (" x = 1", "+y = ```"), "d1"),
            Hunk("H2", "pkg/b.py", None, "modified", 4, 1, 4, 1, "", ("-a", "+b"), "d2"),
        )  # fmt: skip
        snap = DiffSnapshot("b" * 40, "f" * 64, hunks, ("pkg/a.py", "pkg/b.py"), (), 2, 1)
        round_ = Round(
            number=2,
            fingerprint=snap.fingerprint,
            base_sha=snap.base_sha,
            tier="standard",
            prepared_at="2026-01-01T00:00:00.000Z",
            hunks=(
                HunkRef("H1", "pkg/a.py", "d1", 1, 2, False),
                HunkRef("H2", "pkg/b.py", "d2", 4, 1, True),
            ),
            lenses=("intent", "sweep"),
            packet_path="packet-2.md",
        )
        scope = Scope(
            "pkg/a.py",
            "f",
            "function",
            1,
            scope_lines,
            "\n".join(f"line {i}" for i in range(scope_lines)),
        )
        return PacketContext(
            root=Path("."),
            snap=snap,
            round=round_,
            picks=tuple(
                p
                for p in select_lenses("standard", {}, docs_only=False)
                if p.lens.id in ("intent", "sweep")
            ),
            task="",
            commits=("abc1234 feat: add f",),
            conventions=("AGENTS.md",),
            scopes={"H1": scope},
            symbols={"H1": ("helper_fn",)},
            references={"helper_fn": (Reference("pkg/c.py", 40, "x = helper_fn(y)"),)},
            signals_by_hunk={"H1": ("errors",)},
            carried_from={"H2": 1},
        )

    def test_sections_appear_in_order(self) -> None:
        text = render_packet(self.context())

        headings = [line for line in text.splitlines() if line.startswith("#")]
        self.assertEqual(
            headings[:7],
            [
                "# Craftsman review packet — round 2",
                "## Intent",
                "## Conventions to read first",
                "## Lenses in this round",
                "## Recording cheat sheet",
                "## Hunks",
                "### H1 · `pkg/a.py` L1–L2 · modified · signals: errors",
            ],
        )
        self.assertIn("**Task:** (not recorded — infer from commits and diff)", text)
        self.assertIn("- abc1234 feat: add f", text)
        self.assertIn("always in standard (run after all other lenses)", text)
        self.assertIn("### H2 · `pkg/b.py` L4–L4 · carried (covered in round 1, unchanged)", text)
        self.assertIn("- `helper_fn`: `pkg/c.py:40` `x = helper_fn(y)`", text)
        self.assertIn("````diff\n@@ -1,1 +1,2 @@ def f():\n x = 1\n+y = ```\n````", text)
        self.assertIn("Enclosing scope (L1–L3):\n```python\nline 0", text)

    def test_size_guard_drops_enclosing_scopes(self) -> None:
        ctx = self.context(scope_lines=500)
        with mock.patch.object(packet, "MAX_PACKET_CHARS", 3000):
            text = render_packet(ctx)

        self.assertIn("Enclosing scope omitted for size — open pkg/a.py:L1–L500", text)
        self.assertNotIn("line 499", text)
        self.assertIn("line 499", render_packet(ctx))


class ReviewSignalTests(RepoTestCase):
    def test_check_fails_while_a_quality_signal_is_unresolved(self) -> None:
        body = "\n".join(f"    step_{i} = x + {i}" for i in range(45))
        self.repo.write("long_mod.py", f"def over_budget(x: int) -> int:\n{body}\n    return step_44\n")
        self.repo.cli("review", "prepare", "--tier", "quick")
        packet_text = (self.repo.ledger_dir / "packet-1.md").read_text(encoding="utf-8")
        self.assertIn("## Quality signals", packet_text)
        self.assertIn("S1", packet_text)

        self.repo.cover_all()
        self.repo.cli("run", "--kind", "test", "--", sys.executable, "-c", "import long_mod")
        blocked = self.repo.cli("review", "check", check=False)
        self.assertEqual(blocked.returncode, 1)
        self.assertIn("[unresolved-signal]", blocked.stdout)
        self.assertIn("craftsman review add --signal S1", blocked.stdout)
        self.assertIn("craftsman review ack S1", blocked.stdout)

    def test_ack_reason_appears_in_final_report(self) -> None:
        body = "\n".join(f"    step_{i} = x + {i}" for i in range(45))
        self.repo.write("long_mod.py", f"def over_budget(x: int) -> int:\n{body}\n    return step_44\n")
        self.repo.cli("review", "prepare", "--tier", "quick")
        self.repo.cover_all()
        self.repo.cli(
            "review",
            "ack",
            "S1",
            "--reason",
            "over_budget() is a generated dispatch table at long_mod.py:1 with 45 linear steps",
        )
        self.repo.cli("run", "--kind", "test", "--", sys.executable, "-c", "import long_mod")

        self.repo.cli("review", "check")
        report = self.repo.cli("review", "report").stdout
        self.assertIn("## Acknowledged quality signals", report)
        self.assertIn("S1 (`long-function` at `long_mod.py:1`)", report)
        self.assertIn("generated dispatch table at long_mod.py:1", report)

    def test_diff_size_signal_cannot_be_acknowledged(self) -> None:
        lines = "".join(f"CONST_{i} = {i}\n" for i in range(300))
        for name in ("mod_a.py", "mod_b.py", "mod_c.py"):
            self.repo.write(name, lines)
        self.repo.cli("review", "prepare", "--tier", "quick")

        refused = self.repo.cli(
            "review",
            "ack",
            "S1",
            "--reason",
            "mod_a.py:1 adds 900 constants across 3 modules that cannot be split right now",
            check=False,
        )
        self.assertEqual(refused.returncode, 1)
        self.assertIn("diff-size", refused.stderr)
        self.assertIn("cannot be acknowledged", refused.stderr)

    def test_oversized_diff_sets_needs_human_with_top_files(self) -> None:
        self.repo.cli("gate", "--init", "--tier", "quick", "--cwd", ".")
        lines = "".join(f"CONST_{i} = {i}\n" for i in range(300))
        for name in ("mod_a.py", "mod_b.py", "mod_c.py"):
            self.repo.write(name, lines)
        self.repo.cli("review", "prepare", "--tier", "quick")

        state = json.loads((self.repo.ledger_dir / "craftsman_state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["stage"], "needs_human")
        report = self.repo.cli("review", "report").stdout
        self.assertIn("## Diff size budget exceeded (needs human decision)", report)
        self.assertIn("Added production lines: 900 (budget: 800)", report)
        self.assertIn("`mod_a.py` (+300 lines)", report)
        self.assertIn("Suggested split into smaller changes:", report)


class CoverageNoteValidationTests(RepoTestCase):
    def test_cover_rejects_note_made_of_generic_praise(self) -> None:
        self.prepare_bug()
        for word in GENERIC_COVERAGE_WORDS:
            with self.subTest(word=word):
                repeated = " ".join([word] * max(2, (45 // len(word)) + 1))
                proc = self.repo.cli(
                    "review",
                    "cover",
                    "--lens",
                    "craft",
                    "--hunks",
                    "H1",
                    "--note",
                    f"H1 {repeated}",
                    check=False,
                )
                self.assertEqual(proc.returncode, 1, word)
                self.assertIn("Example:", proc.stderr)

    def test_cover_rejects_note_missing_a_listed_hunk(self) -> None:
        self.repo.write("cart.py", BUGGY_TOTAL)
        self.repo.write("extra.py", "EXTRA = 1\n")
        self.repo.cli("review", "prepare", "--tier", "quick")

        proc = self.repo.cli(
            "review",
            "cover",
            "--lens",
            "intent",
            "--hunks",
            "H1,H2",
            "--note",
            "H1 cart.py:3: items=[] -> raises ZeroDivisionError when cart is empty",
            check=False,
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("missing: H2", proc.stderr)

    def test_cover_rejects_same_note_reused_for_many_hunks(self) -> None:
        for idx in range(1, 5):
            self.repo.write(f"m{idx}.py", f"VAL_{idx} = {idx}\n")
        self.repo.cli("review", "prepare", "--tier", "quick")
        repeated = "; ".join(
            f"H{idx} m1.py:1: x=0 -> returns 0 without mutating caller state"
            for idx in range(1, 5)
        )

        proc = self.repo.cli(
            "review",
            "cover",
            "--lens",
            "intent",
            "--hunks",
            "H1,H2,H3,H4",
            "--note",
            repeated,
            check=False,
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("reuses the same note segment for more than 3 hunks", proc.stderr)

    def test_cover_accepts_note_with_input_and_line_reference(self) -> None:
        self.prepare_bug()
        proc = self.repo.cli(
            "review",
            "cover",
            "--lens",
            "intent",
            "--hunks",
            "H1",
            "--note",
            "H1 cart.py:3: items=[{'price': 10}] -> returns 10.0 as expected",
        )
        self.assertEqual(proc.returncode, 0)
        self.assertIn("Coverage recorded: `intent` covered H1", proc.stdout)

    def test_design_lens_covers_changed_files(self) -> None:
        self.repo.write("cart.py", BUGGY_TOTAL)
        self.repo.cli("review", "prepare", "--tier", "standard")
        wrong_flag = self.repo.cli(
            "review",
            "cover",
            "--lens",
            "design",
            "--hunks",
            "H1",
            "--note",
            "H1 cart.py:3: items=[] -> raises ZeroDivisionError",
            check=False,
        )
        self.assertEqual(wrong_flag.returncode, 1)
        self.assertIn("pass --files instead of --hunks", wrong_flag.stderr)

        covered = self.repo.cli(
            "review",
            "cover",
            "--lens",
            "design",
            "--files",
            "cart.py",
            "--note",
            "cart.py:1 total(items) adds 3 lines -> minimal shape with no extra abstractions",
        )
        self.assertEqual(covered.returncode, 0)
        self.assertIn("`design` covered cart.py", covered.stdout)


if __name__ == "__main__":
    unittest.main()
