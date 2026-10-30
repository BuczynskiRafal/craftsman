"""End-to-end tests for `craftsman review` against throwaway repositories."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from craftsman.cli import main as cli_main
from craftsman.ledger import open_round
from craftsman.records import FieldError, Ledger
from craftsman.review import parse_view_lenses, view_slug
from craftsman.scope import ScopePolicy, evaluate_scope

from tests.support import (
    AMBIENT_PLATFORM_ENV_VARS,
    REPO_ROOT,
    CliResult,
    EvidenceRepo,
    assert_cli_error,
    coverage_note,
    make_hunk,
    make_snap,
    run_main,
)

BUGGY_TOTAL = '''def total(items):
    """Sum of prices."""
    return sum(item["price"] for item in items) / len(items)
'''
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
STATIC_EVIDENCE = "static: cart.py:3 divides by len(items), which raises ZeroDivisionError on []"
LONG_FUNCTION = "def over_budget(x: int) -> int:\n{body}\n    return step_44\n".format(
    body="\n".join(f"    step_{i} = x + {i}" for i in range(45))
)
ACK_REASON = "over_budget() is a generated dispatch table at long_mod.py:1 with 45 linear steps"


class RepoTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.repo = EvidenceRepo(self.tmp / "repo")

    def assert_error(
        self, proc: subprocess.CompletedProcess[str], exit_code: int, fragment: str
    ) -> None:
        assert_cli_error(self, proc, exit_code, fragment)

    def prepare_bug(self, tier: str = "quick") -> None:
        self.repo.write("cart.py", BUGGY_TOTAL)
        self.repo.cli("review", "prepare", "--tier", tier, "--task", "Add cart totals")

    def review(
        self, *args: str, check: bool = True, stdin: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        return self.repo.cli("review", *args, check=check, stdin=stdin)

    def add(
        self, *extra: str, file: str = "cart.py", line: int = 3, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        return self.review(
            "add", "--file", file, "--line", str(line), *FINDING_FLAGS, *extra, check=check
        )

    def cover(self, lens: str, *hunks: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        note = coverage_note(list(hunks))
        return self.review(
            "cover", "--lens", lens, "--hunks", ",".join(hunks), "--note", note, check=check
        )

    def verdict(
        self, finding_id: str, verdict: str, evidence: str, *extra: str, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        return self.review(
            "verdict", finding_id, "--as", verdict, "--evidence", evidence, *extra, check=check
        )

    def run_tests(
        self, code: str = "print('ok')", *, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        return self.repo.cli("run", "--kind", "test", "--", sys.executable, "-c", code, check=check)

    def json_of(self, *args: str) -> Any:
        return json.loads(self.review(*args, "--json").stdout)

    def packet(self, number: int = 1) -> str:
        return (self.repo.ledger_dir / f"packet-{number}.md").read_text(encoding="utf-8")


class PrepareTests(RepoTestCase):
    def test_prepare_writes_the_packet(self) -> None:
        self.repo.write("cart.py", BUGGY_TOTAL)

        out = self.review("prepare", "--tier", "quick", "--task", "Add cart totals").stdout

        self.assertIn("Round 1 prepared (quick): 1 hunks, 0 carried, 1 to review.", out)
        self.assertIn("Lenses: intent, contracts, failure\n", out)
        self.assertIn("**Task:** Add cart totals", self.packet())
        self.assertIn("Scope: `total` (function, L1–L3)", self.packet())

    def test_preparing_the_same_diff_again_keeps_the_round(self) -> None:
        self.prepare_bug()

        again = self.review("prepare", "--tier", "quick").stdout

        self.assertIn("Round 1 unchanged (same diff and tier)", again)

    def test_prepare_rewrites_a_missing_packet(self) -> None:
        self.prepare_bug()
        (self.repo.ledger_dir / "packet-1.md").unlink()

        self.review("prepare", "--tier", "quick")

        self.assertIn("# Craftsman review packet — round 1", self.packet())

    def test_round_two_carries_unchanged_hunks(self) -> None:
        self.repo.write("cart.py", BUGGY_TOTAL)
        self.repo.write("notes.py", "LABEL = 'cart'\n")
        self.repo.record_full_evidence("quick")
        self.repo.write("cart.py", BUGGY_TOTAL.replace("/ len(items)", ""))

        out = self.review("prepare", "--tier", "quick").stdout

        self.assertIn("Round 2 prepared (quick): 2 hunks, 1 carried, 1 to review.", out)
        self.assertIn(
            "### H2 · `notes.py` L1–L1 · carried (covered in round 1, unchanged)", self.packet(2)
        )

    def test_auto_tier_follows_the_gate(self) -> None:
        self.repo.cli("gate", "--init", "--tier", "auto", "--cwd", ".")
        self.repo.write("cart.py", BUGGY_TOTAL)

        out = self.review("prepare", "--tier", "auto").stdout

        self.assertIn("Round 1 prepared (quick)", out)

    def test_unknown_tier_is_a_usage_error(self) -> None:
        bogus = self.review("prepare", "--tier", "max", check=False)

        self.assert_error(bogus, 2, "invalid choice 'max' (choose from auto, quick, standard, deep)")

    def test_hunks_mentioning_a_changed_symbol_are_marked_reopened(self) -> None:
        self.repo.write("cart.py", "def item_count(items):\n    return len(items)\n")
        self.repo.write(
            "tests/test_cart.py", "from cart import item_count\n\nassert item_count([1]) == 1\n"
        )
        self.repo.record_full_evidence("quick")
        self.repo.write("cart.py", "def item_count(items):\n    return len(list(items))\n")

        out = self.review("prepare", "--tier", "quick").stdout

        self.assertIn("2 hunks, 0 carried, 2 to review.", out)
        self.assertIn("reopened: mentions item_count (changed in H1)\n", self.packet(2))

    def test_prepare_help_mentions_the_ignore_file(self) -> None:
        help_text = self.review("prepare", "--help").stdout

        self.assertIn(".craftsmanignore", " ".join(help_text.split()))

    def test_empty_diff_has_nothing_to_review(self) -> None:
        out = self.review("prepare").stdout

        self.assertIn("Nothing to review", out)
        self.assertFalse((self.repo.ledger_dir / "ledger.json").exists())

    def test_reverting_every_change_leaves_nothing_to_review(self) -> None:
        self.prepare_bug()
        (self.repo.root / "cart.py").unlink()

        checked = self.review("check", check=False)

        self.assertEqual(checked.returncode, 0, checked.stdout)
        self.assertIn("nothing left to review", self.review("show").stdout)

    def test_commands_keep_using_the_base_of_the_round(self) -> None:
        self.repo.write("cart.py", BUGGY_TOTAL)
        self.repo.git("add", "-A")
        self.repo.git("commit", "-q", "--no-verify", "-m", "cart")
        self.repo.cli("gate", "--init", "--tier", "quick", "--cwd", ".")
        self.repo.write("notes.py", "LABEL = 'cart'\n")

        self.review("prepare", "--tier", "quick", "--base", self.repo.base_sha)
        self.repo.cover_all()
        self.run_tests()

        checked = self.review("check", check=False)
        self.assertEqual(checked.returncode, 0, checked.stdout)

    def test_packet_shows_the_task_of_the_active_gate(self) -> None:
        self.repo.cli("gate", "--init", "--tier", "quick", "--task", "Add cart totals")
        self.repo.write("cart.py", BUGGY_TOTAL)

        self.review("prepare")

        self.assertIn("**Task:** Add cart totals", self.packet())


class CoverTests(RepoTestCase):
    def test_cover_reports_what_is_left(self) -> None:
        self.prepare_bug()

        covered = self.cover("intent", "H1")

        self.assertIn(
            "Coverage recorded: `intent` covered H1 in round 1; 2 lens item(s)", covered.stdout
        )

    def test_cover_refuses_a_stale_round(self) -> None:
        self.prepare_bug()
        self.repo.write("cart.py", BUGGY_TOTAL + "# edited\n")

        self.assert_error(self.cover("intent", "H1", check=False), 1, "Code changed since round 1")

    def test_cover_without_a_lens_is_a_usage_error(self) -> None:
        proc = self.review("cover", "--hunks", "H1", "--note", "x", check=False)

        self.assert_error(proc, 2, "the following arguments are required: --lens")

    def test_note_error_names_the_note_flag(self) -> None:
        self.prepare_bug()

        proc = self.review("cover", "--lens", "intent", "--hunks", "H1", "--note", "H1 ok", check=False)

        self.assert_error(proc, 1, "error: --note needs at least 40 characters per hunk or file")

    def test_design_lens_refuses_hunks(self) -> None:
        self.prepare_bug("standard")

        proc = self.cover("design", "H1", check=False)

        self.assert_error(proc, 1, "pass --files instead of --hunks")

    def test_design_lens_covers_files(self) -> None:
        self.prepare_bug("standard")
        note = "cart.py:1 total(items) adds 3 lines -> minimal shape with no extra abstractions"

        covered = self.review("cover", "--lens", "design", "--files", "cart.py", "--note", note)

        self.assertIn("`design` covered cart.py", covered.stdout)

    def test_design_count_error_speaks_of_files(self) -> None:
        self.prepare_bug("standard")

        proc = self.review(
            "cover", "--lens", "design", "--files", "a,b,c,d,e", "--note", "x", check=False
        )

        self.assert_error(proc, 1, "cover 1..4 files per call (got 5)")


class AddTests(RepoTestCase):
    def test_add_echoes_the_cited_line(self) -> None:
        self.prepare_bug()

        added = self.add()

        self.assertEqual(
            added.stdout.splitlines(),
            [
                "F1 recorded (major, contracts, cart.py:3)",
                '  L3: return sum(item["price"] for item in items) / len(items)',
            ],
        )

    def test_add_may_name_the_signal_it_resolves(self) -> None:
        self.repo.write("long_mod.py", LONG_FUNCTION)
        self.review("prepare", "--tier", "quick")

        self.add("--signal", "S1", file="long_mod.py", line=1)

        self.assertEqual(self.repo.show()["findings"][0]["signal_id"], "S1")

    def test_add_resolves_files_against_cwd(self) -> None:
        self.repo.write("pkg/total.py", BUGGY_TOTAL)
        self.review("prepare", "--tier", "quick")

        added = self.add("--cwd", str(self.repo.root / "pkg"), file="total.py")

        self.assertIn("F1 recorded (major, contracts, pkg/total.py:3)", added.stdout)

    def test_add_accepts_a_file_the_diff_deletes(self) -> None:
        self.repo.write("cart.py", BUGGY_TOTAL)
        self.repo.git("add", "-A")
        self.repo.git("commit", "-q", "--no-verify", "-m", "cart")
        self.repo.cli("gate", "--init", "--tier", "quick", "--cwd", ".")
        (self.repo.root / "cart.py").unlink()
        self.review("prepare", "--tier", "quick")

        gone = self.add()

        self.assertIn("deleted in the diff; line 3 refers to the base version", gone.stdout)

    @unittest.skipIf(sys.platform == "win32", "symlinks need extra privileges on Windows")
    def test_add_keeps_the_path_of_a_symlink(self) -> None:
        self.repo.write("cart.py", BUGGY_TOTAL)
        outside = self.tmp / "shared.py"
        outside.write_text(BUGGY_TOTAL, encoding="utf-8")
        (self.repo.root / "linked.py").symlink_to(outside)
        self.review("prepare", "--tier", "quick")

        added = self.add(file="linked.py")

        self.assertIn("F1 recorded (major, contracts, linked.py:3)", added.stdout)

    def test_add_refuses_a_line_past_the_end_of_the_file(self) -> None:
        self.prepare_bug()

        past = self.add(line=40, check=False)

        self.assert_error(past, 1, "error: --line 40 is past the end of cart.py, which has 3 lines")

    def test_line_count_ignores_form_feeds(self) -> None:
        self.repo.write("cart.py", BUGGY_TOTAL)
        (self.repo.root / "feed.py").write_bytes(b"a = 1\x0cb = 2\nc = 3\n")
        self.review("prepare", "--tier", "quick")

        feed = self.add(file="feed.py", check=False)

        self.assert_error(feed, 1, "error: --line 3 is past the end of feed.py, which has 2 lines")

    def test_domain_errors_exit_1_without_traceback(self) -> None:
        self.prepare_bug()
        outside = self.tmp / "outside.py"
        outside.write_text("x = 1\n", encoding="utf-8")
        cases = [
            (
                ("cover", "--lens", "intent", "--hunks", "H7", "--note", coverage_note(["H7"])),
                "unknown hunk ids",
            ),
            (("cover", "--lens", "intent", "--hunks", "H1", "--note", "x" * 40), "missing: H1"),
            (("add", "--file", str(outside), "--line", "1", *FINDING_FLAGS), "outside the repository"),
            (("add", "--file", "nope.py", "--line", "1", *FINDING_FLAGS), "does not exist"),
            (
                ("verdict", "F9", "--as", "dismissed", "--evidence", "guarded at cart.py:2 already"),
                "unknown finding",
            ),
            (
                ("resolve", "F9", "--as", "fixed", "--note", "Dropped the division entirely"),
                "unknown finding",
            ),
        ]
        for args, fragment in cases:
            with self.subTest(args=args[:2]):
                self.assert_error(self.review(*args, check=False), 1, fragment)

    def test_validation_error_names_the_flag(self) -> None:
        self.prepare_bug()

        short_title = self.review(
            "add", "--file", "cart.py", "--line", "3", *FINDING_FLAGS, "--title", "Bad", check=False
        )

        self.assert_error(short_title, 1, "error: --title must have 8..160 characters (got 3)")

    def test_invalid_severity_is_a_usage_error(self) -> None:
        bad = self.review(
            "add", "--file", "cart.py", "--line", "1", *FINDING_FLAGS, "--severity", "huge", check=False
        )

        self.assert_error(bad, 2, "invalid choice 'huge'")

    def test_add_refuses_a_stale_round(self) -> None:
        self.prepare_bug()
        self.repo.write("cart.py", BUGGY_TOTAL + "# edited\n")

        self.assert_error(self.add(check=False), 1, "Code changed since round 1 was prepared")


class MoveTests(RepoTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.repo.write("other.py", "x = 1\n")
        self.prepare_bug()
        self.add(line=1)

    def test_move_corrects_the_line(self) -> None:
        moved = self.review("move", "F1", "--line", "3")

        self.assertIn("F1 now points at cart.py:3", moved.stdout)
        self.assertIn("L3: return sum(", moved.stdout)

    def test_move_corrects_the_file(self) -> None:
        self.review("move", "F1", "--file", "other.py", "--line", "1")

        (finding,) = self.repo.show()["findings"]
        self.assertEqual((finding["path"], finding["line"]), ("other.py", 1))

    def test_move_checks_the_new_line(self) -> None:
        past = self.review("move", "F1", "--line", "9", check=False)
        zero = self.review("move", "F1", "--line", "0", check=False)

        self.assert_error(past, 1, "error: --line 9 is past the end of cart.py, which has 3 lines")
        self.assert_error(zero, 1, "error: --line must be >= 1 (got 0)")

    def test_move_refuses_a_judged_finding(self) -> None:
        self.verdict("F1", "dismissed", "cart.py:3 already guards the empty list")

        judged = self.review("move", "F1", "--line", "2", check=False)

        self.assert_error(judged, 1, "F1 already has a verdict (dismissed); record a new finding")

    def test_move_refuses_changed_code(self) -> None:
        self.repo.write("cart.py", BUGGY_TOTAL + "# edited\n")

        changed = self.review("move", "F1", "--line", "2", check=False)

        self.assert_error(changed, 1, "the code changed since F1 was recorded")


class VerdictTests(RepoTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.prepare_bug()
        self.add()

    def test_proven_verdict_cites_a_repro_run(self) -> None:
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
        judged = self.verdict(
            "F1", "proven", "R1 raised ZeroDivisionError for an empty cart", "--run", "R1"
        )

        self.assertIn("R1 recorded (repro, exit 1", run.stderr)
        self.assertEqual(judged.stdout.strip(), "F1 verdict recorded: proven (run R1)")

    def test_proven_verdict_without_a_run_is_refused(self) -> None:
        no_run = self.verdict("F1", "proven", "it obviously crashes on empty input", check=False)

        self.assert_error(no_run, 1, "required for a 'proven' verdict")

    def test_short_evidence_names_the_evidence_flag(self) -> None:
        short = self.verdict("F1", "dismissed", "fine", check=False)

        self.assert_error(short, 1, "error: --evidence must have at least 20 characters (got 4)")

    def test_fixed_finding_needs_changed_code(self) -> None:
        self.verdict("F1", "probable", STATIC_EVIDENCE)

        unchanged = self.review(
            "resolve", "F1", "--as", "fixed", "--note", "Dropped the division", check=False
        )

        self.assert_error(unchanged, 1, "fix the code first, then resolve")

    def test_resolve_after_the_fix(self) -> None:
        self.verdict("F1", "probable", STATIC_EVIDENCE)
        self.repo.write("cart.py", BUGGY_TOTAL.replace(" / len(items)", ""))

        resolved = self.review(
            "resolve", "F1", "--as", "fixed", "--note", "Dropped the division entirely"
        )

        self.assertEqual(resolved.stdout.strip(), "F1 resolved as fixed")

    def test_verdict_without_evidence_is_a_usage_error(self) -> None:
        proc = self.review("verdict", "F1", "--as", "probable", check=False)

        self.assert_error(proc, 2, "the following arguments are required: --evidence")


class MergeTests(RepoTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.prepare_bug()
        self.add("--severity", "minor")
        self.add()

    def test_merge_folds_duplicates_into_the_target(self) -> None:
        merged = self.review("merge", "F1", "F2")

        self.assertEqual(merged.stdout.strip(), "F2 merged into F1 (major, cart.py:3)")
        report = self.json_of("report")
        self.assertEqual([f["id"] for f in report["findings"]["dismissed"]], ["F2"])

    def test_merge_refuses_judged_findings(self) -> None:
        self.verdict("F1", "probable", STATIC_EVIDENCE)

        refused = self.review("merge", "F1", "F2", check=False)

        self.assert_error(refused, 1, "F1 already has the verdict probable")


class FromJsonTests(RepoTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.prepare_bug()

    def from_json(
        self, command: str, entries: object, *flags: str, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        return self.review(command, "--from-json", "-", *flags, stdin=json.dumps(entries), check=check)

    def finding(self, **overrides: object) -> dict[str, object]:
        entry: dict[str, object] = {
            "lens": "contracts",
            "file": "cart.py",
            "line": 3,
            "severity": "major",
            "title": "Empty list crashes on len(items)",
            "scenario": "total([]) divides by zero at len(items) and raises ZeroDivisionError",
        }
        return {**entry, **overrides}

    def test_cover_records_every_entry(self) -> None:
        entries = [
            {"lens": lens, "hunks": ["H1"], "note": coverage_note(["H1"])}
            for lens in ("intent", "contracts", "failure")
        ]

        covered = self.from_json("cover", entries)

        self.assertEqual(len(covered.stdout.splitlines()), 3)
        self.assertIn("`failure` covered H1 in round 1; 0 lens item(s) left", covered.stdout)

    def test_add_reads_a_file(self) -> None:
        source = self.tmp / "findings.json"
        source.write_text(
            json.dumps([self.finding(), self.finding(line=1, suggestion="Guard it")]), encoding="utf-8"
        )

        added = self.review("add", "--from-json", str(source))

        self.assertIn("F1 recorded (major, contracts, cart.py:3)", added.stdout)
        self.assertIn("L1: def total(items):", added.stdout)

    def test_verdicts_are_recorded_in_order(self) -> None:
        self.add()
        self.add()
        entries = [
            {"id": "F1", "as": "probable", "evidence": STATIC_EVIDENCE},
            {"id": "F2", "as": "dismissed", "evidence": "cart.py:3 is guarded by the caller"},
        ]

        judged = self.from_json("verdict", entries)

        self.assertEqual(
            judged.stdout.splitlines(),
            ["F1 verdict recorded: probable", "F2 verdict recorded: dismissed"],
        )

    def test_a_bad_cover_entry_records_nothing(self) -> None:
        five = ["H1", "H2", "H3", "H4", "H5"]
        entries = [
            {"lens": "intent", "hunks": ["H1"], "note": coverage_note(["H1"])},
            {"lens": "contracts", "hunks": five, "note": coverage_note(five)},
        ]

        failed = self.from_json("cover", entries, check=False)

        self.assert_error(failed, 1, "entry 2 (contracts): hunks: cover 1..4 hunks per call (got 5)")
        self.assertEqual(self.repo.show()["coverage"]["intent"]["covered"], 0)

    def test_a_bad_finding_entry_records_nothing(self) -> None:
        failed = self.from_json("add", [self.finding(), self.finding(severity="critical")], check=False)

        self.assert_error(failed, 1, "entry 2 (F2): severity: must be one of blocker, major, minor, nit")
        self.assertEqual(self.repo.show()["findings"], [])

    def test_a_bad_verdict_entry_records_nothing(self) -> None:
        self.add()
        self.add()
        entries = [
            {"id": "F1", "as": "dismissed", "evidence": "cart.py:3 is guarded by the caller"},
            {"id": "F2", "as": "proven", "evidence": "Reproduced ZeroDivisionError at cart.py:3"},
        ]

        failed = self.from_json("verdict", entries, check=False)

        self.assert_error(failed, 1, "entry 2 (F2): run: is required for a 'proven' verdict")
        self.assertEqual([f["verdict"] for f in self.repo.show()["findings"]], [None, None])

    def test_unknown_entry_key_is_rejected(self) -> None:
        entries = [{"lens": "intent", "hunks": ["H1"], "note": coverage_note(["H1"]), "extra": 1}]

        failed = self.from_json("cover", entries, check=False)

        self.assert_error(failed, 1, "entry 1 (intent): extra: is not a known field")

    def test_empty_list_is_rejected(self) -> None:
        self.assert_error(
            self.from_json("cover", [], check=False), 1, "--from-json list must not be empty"
        )

    def test_design_entry_covers_files(self) -> None:
        self.prepare_bug("standard")
        note = "cart.py:1 total(items) adds 3 lines -> minimal shape with no extra abstractions"

        covered = self.from_json("cover", [{"lens": "design", "files": ["cart.py"], "note": note}])

        self.assertIn("`design` covered cart.py", covered.stdout)

    def test_optional_entry_key_may_not_be_null(self) -> None:
        failed = self.from_json("add", [self.finding(signal=None)], check=False)

        self.assert_error(failed, 1, "entry 1 (F1): signal: must be a string")

    def test_design_entry_takes_files_not_hunks(self) -> None:
        self.prepare_bug("standard")
        entries = [
            {"lens": "design", "hunks": ["H1"], "note": "H1 total(): 2 branches -> L9 returns 0 here"}
        ]

        failed = self.from_json("cover", entries, check=False)

        self.assert_error(failed, 1, "entry 1 (design): files: is missing")

    def test_from_json_cannot_be_combined_with_entry_flags(self) -> None:
        cases = [
            ("cover", ("--lens", "intent"), "--from-json cannot be combined with --lens"),
            ("add", ("--file", "cart.py"), "--from-json cannot be combined with --file"),
            ("verdict", ("F1",), "--from-json cannot be combined with ID"),
        ]
        for command, flags, message in cases:
            with self.subTest(command=command):
                self.assert_error(self.from_json(command, [], *flags, check=False), 1, message)


class InspectionTests(RepoTestCase):
    def test_blocked_check_lists_next_actions(self) -> None:
        self.prepare_bug()

        blocked = self.review("check", check=False)

        self.assertEqual(blocked.returncode, 1)
        self.assertIn("**Craftsman gate: BLOCKED", blocked.stdout)
        self.assertIn("[uncovered] Lenses `intent`, `contracts`, `failure`", blocked.stdout)

    def test_check_passes_with_full_evidence(self) -> None:
        self.repo.write("cart.py", BUGGY_TOTAL)
        self.repo.record_full_evidence("quick")

        self.assertTrue(self.json_of("check")["passed"])

    def test_check_never_evaluates_below_the_task_tier(self) -> None:
        self.repo.cli("gate", "--init", "--tier", "standard", "--cwd", ".")
        self.repo.write("cart.py", BUGGY_TOTAL)
        self.repo.record_full_evidence("quick")

        lowered = self.review("check", "--tier", "quick", check=False)

        self.assertIn("[tier-too-low]", lowered.stdout)

    def test_a_deeper_round_is_held_to_its_own_tier(self) -> None:
        self.repo.write("cart.py", BUGGY_TOTAL)
        self.repo.cli("gate", "--init", "--tier", "standard")
        self.review("prepare", "--tier", "deep")
        self.repo.cover_all()
        self.run_tests()

        completed = self.repo.cli("gate", "--complete", check=False)

        self.assertEqual(self.json_of("report")["tier"], "deep")
        self.assert_error(completed, 1, "[no-e2e-run]")

    def test_show_lists_coverage_per_lens(self) -> None:
        self.prepare_bug()

        shown = self.review("show").stdout

        self.assertIn("Round 1 (quick) prepared", shown)
        self.assertIn("intent     0/1 (missing H1)", shown)
        self.assertIn("Findings: none recorded.", shown)

    def test_show_prints_the_packet_path(self) -> None:
        self.prepare_bug()

        shown = self.review("show", "--packet-path").stdout.strip()

        self.assertEqual(Path(shown), (self.repo.ledger_dir / "packet-1.md").resolve())

    def test_findings_table_escapes_table_syntax(self) -> None:
        self.prepare_bug()
        self.add("--title", "a | b\nbreaks the table")

        self.assertIn("| a \\| b breaks the table |", self.review("show").stdout)

    def test_report_of_a_passing_gate(self) -> None:
        self.repo.write("cart.py", BUGGY_TOTAL)
        self.repo.record_full_evidence("quick")

        report = self.review("report").stdout

        self.assertIn("- Gate: PASSED", report)
        self.assertIn("No findings recorded.", report)
        self.assertIn("(test) → exit 0", report)

    def test_report_lists_only_runs_the_gate_counts(self) -> None:
        self.prepare_bug()
        self.repo.cover_all()
        self.repo.cli("run", "--kind", "other", "--", sys.executable, "-c", "print('ok')")

        report = self.review("report").stdout

        self.assertIn("No passing test or e2e run at the final code.", report)
        self.assertEqual(self.json_of("report")["runs"], [])

    def test_waived_run_appears_in_the_report(self) -> None:
        self.prepare_bug()
        self.repo.cover_all()
        self.repo.cli("run", "--kind", "test", "--", sys.executable, "tests/test_typo.py", check=False)
        self.run_tests()
        self.review(
            "waive-run", "R1", "--reason", "Mistyped path; the suite lives in tests/test_cart.py"
        )

        report = self.review("report").stdout

        self.assertIn("## Waived runs", report)
        self.assertEqual([run["id"] for run in self.json_of("report")["waived_runs"]], ["R1"])

    def test_waive_run_unblocks_the_gate(self) -> None:
        self.prepare_bug()
        self.repo.cover_all()
        self.repo.cli("run", "--kind", "test", "--", sys.executable, "tests/test_typo.py", check=False)
        self.run_tests()

        waived = self.review(
            "waive-run", "R1", "--reason", "Mistyped path; the suite is tests/test_cart.py"
        )

        self.assertEqual(waived.stdout.strip(), "R1 waived; it no longer blocks the gate")
        self.assertEqual(self.review("check", check=False).returncode, 0)

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

    def test_reset_archives_the_review(self) -> None:
        self.prepare_bug()

        reset = self.review("reset").stdout

        self.assertIn("Archived to", reset)
        self.assertFalse((self.repo.ledger_dir / "ledger.json").exists())
        self.assertEqual(len(list(self.repo.ledger_dir.glob("archive-*/packet-1.md"))), 1)
        self.assertIn("Nothing to archive", self.review("reset").stdout)


class ReviewOnlyTests(RepoTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.prepare_bug()

    def judge_open_finding(self) -> None:
        self.repo.cover_all()
        self.add()
        self.verdict("F1", "probable", STATIC_EVIDENCE)

    def test_incomplete_review_names_the_review_only_check(self) -> None:
        report = self.review("report", "--review-only").stdout

        self.assertIn(
            "- Review: INCOMPLETE (1 open items) — uncovered; see `craftsman review check --review-only`",
            report,
        )

    def test_complete_review_passes_with_open_findings(self) -> None:
        self.judge_open_finding()

        checked = self.review("check", "--review-only")

        self.assertIn("**Craftsman review: COMPLETE (review-only)**", checked.stdout)
        self.assertIn("1 finding for the author: F1.", checked.stdout)

    def test_full_gate_still_blocks_on_open_findings(self) -> None:
        self.judge_open_finding()

        self.assertIn("[unresolved]", self.review("check", check=False).stdout)

    def test_review_only_is_refused_during_an_active_gate(self) -> None:
        self.repo.cli("gate", "--init", "--tier", "quick", "--cwd", ".")

        refused = self.review("check", "--review-only", check=False)

        self.assert_error(refused, 1, "--review-only is not allowed during an active /craftsman gate")


class SignalTests(RepoTestCase):
    def prepare_long_function(self) -> None:
        self.repo.write("long_mod.py", LONG_FUNCTION)
        self.review("prepare", "--tier", "quick")
        self.repo.cover_all()
        self.run_tests("import long_mod")

    def prepare_oversized(self) -> None:
        lines = "".join(f"CONST_{i} = {i}\n" for i in range(300))
        for name in ("mod_a.py", "mod_b.py", "mod_c.py"):
            self.repo.write(name, lines)
        self.review("prepare", "--tier", "quick")

    def test_packet_lists_the_quality_signals(self) -> None:
        self.prepare_long_function()

        self.assertIn("- **S1** (`long-function`) `long_mod.py:1`", self.packet())

    def test_check_fails_while_a_quality_signal_is_unresolved(self) -> None:
        self.prepare_long_function()

        blocked = self.review("check", check=False)

        self.assertIn("[unresolved-signal]", blocked.stdout)
        self.assertIn("craftsman review ack S1", blocked.stdout)

    def test_acknowledged_signal_is_reported_once(self) -> None:
        self.prepare_long_function()
        self.review("ack", "S1", "--reason", ACK_REASON)

        report = self.review("report").stdout

        self.assertIn("S1 (`long-function` at `long_mod.py:1`)", report)
        self.assertEqual(report.count(ACK_REASON), 1)

    def test_ack_repeated_in_a_later_round_is_reported_once(self) -> None:
        self.repo.cli("gate", "--init", "--tier", "quick", "--cwd", ".")
        self.prepare_long_function()
        self.review("ack", "S1", "--reason", ACK_REASON)
        self.repo.write("notes.py", "LABEL = 'x'\n")
        self.review("prepare", "--tier", "quick")
        self.review("ack", "S1", "--reason", ACK_REASON + " (unchanged)")

        report = self.review("report").stdout

        self.assertNotIn(ACK_REASON + "\n", report)
        self.assertEqual(report.count(ACK_REASON), 1)

    def test_ack_of_a_signal_that_is_gone_is_not_reported(self) -> None:
        self.prepare_long_function()
        self.review("ack", "S1", "--reason", ACK_REASON)
        self.repo.write("long_mod.py", "def over_budget(x: int) -> int:\n    return x\n")
        self.review("prepare", "--tier", "quick")

        self.assertNotIn("## Acknowledged quality signals", self.review("report").stdout)

    def test_diff_size_signal_cannot_be_acknowledged(self) -> None:
        self.prepare_oversized()

        refused = self.review(
            "ack", "S1", "--reason", "mod_a.py:1 adds 900 constants across 3 modules", check=False
        )

        self.assert_error(refused, 1, "S1 (`diff-size`) cannot be acknowledged")

    def test_oversized_diff_hands_the_review_to_the_user(self) -> None:
        self.repo.cli("gate", "--init", "--tier", "quick", "--cwd", ".")

        self.prepare_oversized()

        state = json.loads((self.repo.ledger_dir / "craftsman_state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["stage"], "needs_human")

    def test_oversized_report_shows_the_budget_with_top_files(self) -> None:
        self.prepare_oversized()

        report = self.review("report").stdout

        self.assertIn("Added production lines: 900 (budget: 800)", report)
        self.assertIn("`mod_a.py` (+300 lines)", report)

    def test_inspection_leaves_the_gate_state_alone(self) -> None:
        self.prepare_oversized()
        self.repo.cli("gate", "--init", "--tier", "quick")
        state = self.repo.ledger_dir / "craftsman_state.json"
        before = state.read_bytes()

        self.review("check", check=False)
        self.review("report")

        self.assertEqual(state.read_bytes(), before)


class FactTests(RepoTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.repo.write("caller.py", "from cart import total\nval = total([])\n")
        self.prepare_bug()

    def add_fact(
        self, source: str, text: str, *, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        return self.review(
            "fact", "add", "--hunks", "H2", "--source", source, "--text", text, check=check
        )

    def view(self, lenses: str = "contracts") -> str:
        return Path(self.review("view", "--lenses", lenses).stdout.strip()).read_text(encoding="utf-8")

    def test_fact_with_a_line_past_the_end_is_rejected(self) -> None:
        proc = self.add_fact(
            "cart.py:99", "total(items) is called with a list of item dicts", check=False
        )

        self.assert_error(proc, 1, "--source 99 is past the end of cart.py")

    def test_fact_with_a_judgement_is_rejected(self) -> None:
        proc = self.add_fact(
            "cart.py:1", "total(items) at cart.py:3 has a bug for empty lists", check=False
        )

        self.assert_error(proc, 1, "without judgement words")

    def test_reviewer_view_shows_the_fact(self) -> None:
        self.add_fact("caller.py:2", "total is invoked from caller.py:2 with an empty list literal")

        self.assertIn("Facts: F-1 total is invoked from caller.py:2", self.view())

    def test_fact_disappears_after_its_source_file_changes(self) -> None:
        self.add_fact("caller.py:2", "total is invoked from caller.py:2 with an empty list literal")
        self.repo.write("caller.py", "from cart import total\nval = total([{'price': 1}])\n")
        self.review("prepare", "--tier", "quick")

        self.assertNotIn("Facts: F-1", self.view())

    def test_findings_never_appear_in_a_reviewer_view(self) -> None:
        self.add("--title", "SECRET_FINDING_TITLE_FOR_VERIFIER_ONLY")

        self.assertNotIn("SECRET_FINDING_TITLE_FOR_VERIFIER_ONLY", self.view())


class ViewTests(RepoTestCase):
    def test_view_is_written_next_to_the_packet(self) -> None:
        self.prepare_bug()

        path = Path(self.review("view", "--lenses", "intent,failure", "--hunks", "H1").stdout.strip())

        self.assertEqual(path.name, "packet-1.intent-failure.H1.md")

    def test_lenses_flag_has_no_abbreviation(self) -> None:
        self.prepare_bug()

        proc = self.review("view", "--lens", "intent", check=False)

        self.assert_error(proc, 2, "the following arguments are required: --lenses")

    def test_view_refuses_a_stale_round(self) -> None:
        self.prepare_bug()
        self.repo.write("cart.py", BUGGY_TOTAL + "# edited\n")

        proc = self.review("view", "--lenses", "intent", check=False)

        self.assert_error(proc, 1, "Code changed since round 1 was prepared")


class ViewArgumentTests(unittest.TestCase):
    def setUp(self) -> None:
        _, self.round = open_round(
            Ledger(),
            make_snap(make_hunk("H1")),
            "standard",
            ("intent", "contracts", "sweep"),
            "p.md",
            {},
            signals=(),
        )

    def test_group_resolves_to_its_lenses_in_the_round(self) -> None:
        self.assertEqual(parse_view_lenses("R1", self.round), ["intent", "contracts"])

    def test_resolved_lenses_are_deduplicated(self) -> None:
        self.assertEqual(parse_view_lenses("intent, R1", self.round), ["intent", "contracts"])

    def test_lens_not_selected_in_the_round_is_rejected(self) -> None:
        with self.assertRaisesRegex(FieldError, "'craft' has no lens selected in round 1"):
            parse_view_lenses("craft", self.round)

    def test_unknown_lens_is_rejected(self) -> None:
        with self.assertRaisesRegex(FieldError, "unknown lens or group 'vibes'"):
            parse_view_lenses("vibes", self.round)

    def test_empty_lens_list_is_rejected(self) -> None:
        with self.assertRaisesRegex(FieldError, "must name at least one lens"):
            parse_view_lenses(" , ", self.round)

    def test_slug_names_lenses_then_hunks(self) -> None:
        self.assertEqual(view_slug(["intent", "craft"], "H1-H3,H7"), "intent-craft.H1-H3-H7")

    def test_slug_without_hunks_names_only_lenses(self) -> None:
        self.assertEqual(view_slug(["intent"], None), "intent")


class PacketPathTests(RepoTestCase):
    def test_packet_path_without_a_round_is_an_error(self) -> None:
        result = run_main(cli_main, "review", "show", "--packet-path", "--cwd", str(self.repo.root))

        self.assertEqual(result.code, 1)
        self.assertIn("no review round yet", result.err)

    def test_packet_path_names_the_current_round_packet(self) -> None:
        self.repo.write_lines("app.py", 5)
        self.assertEqual(run_main(cli_main, "review", "prepare", "--cwd", str(self.repo.root)).code, 0)

        result = run_main(cli_main, "review", "show", "--packet-path", "--cwd", str(self.repo.root))

        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(Path(result.out.strip()).name, "packet-1.md")
        self.assertTrue(Path(result.out.strip()).is_file())


class FastCliTestCase(RepoTestCase):
    def cli_fast(self, *args: str, stdin_bytes: bytes | None = None) -> CliResult:
        if stdin_bytes is None:
            return run_main(cli_main, *args, "--cwd", str(self.repo.root))
        fake_stdin = mock.MagicMock()
        fake_stdin.buffer = io.BytesIO(stdin_bytes)
        with mock.patch("sys.stdin", fake_stdin):
            return run_main(cli_main, *args, "--cwd", str(self.repo.root))

    def prepare_multi_hunk(self, count: int, tier: str = "standard") -> None:
        for index in range(1, count + 1):
            self.repo.write(f"mod_{index}.py", f"def fn_{index}(x):\n    return x + {index}\n")
        res = self.cli_fast("review", "prepare", "--tier", tier, "--task", "Multi-hunk change")
        self.assertEqual(res.code, 0, res.err)


class BatchRejectionTests(FastCliTestCase):
    def test_batch_reports_three_bad_entries_in_one_pass(self) -> None:
        self.prepare_multi_hunk(14, tier="deep")
        lenses = ("intent", "contracts", "failure")
        entries: list[dict[str, object]] = [
            {"lens": lenses[i % 3], "hunks": [f"H{(i % 14) + 1}"], "note": coverage_note([f"H{(i % 14) + 1}"])}
            for i in range(42)
        ]
        entries[39] = {"lens": "trust", "hunks": ["H13", "H14"], "note": coverage_note(["H13"])}
        entries[40] = {"lens": "contracts", "hunks": ["H13", "H14"], "note": coverage_note(["H13"])}
        entries[41] = {"lens": "failure", "hunks": ["H13"], "note": "H13 looks good"}

        res = self.cli_fast("review", "cover", "--from-json", "-", stdin_bytes=json.dumps(entries).encode())

        self.assertEqual(res.code, 1)
        self.assertNotIn("error:", res.err)
        self.assertIn("craftsman review cover: 3 of 42 entries rejected; nothing was recorded.", res.err)
        self.assertIn("entry 40 (trust): note:", res.err)
        self.assertIn("entry 41 (contracts): note:", res.err)
        self.assertIn("entry 42 (failure): note:", res.err)
        self.assertEqual(self.repo.show()["coverage"]["intent"]["covered"], 0)
        for index, lens in ((39, "trust"), (40, "contracts"), (41, "failure")):
            entries[index] = {"lens": lens, "hunks": ["H13", "H14"], "note": coverage_note(["H13", "H14"])}
        fixed = self.cli_fast("review", "cover", "--from-json", "-", stdin_bytes=json.dumps(entries).encode())
        self.assertEqual(fixed.code, 0, fixed.err)

    def test_four_hunk_entry_reports_missing_plus_weak_targets(self) -> None:
        self.prepare_multi_hunk(4, tier="quick")
        note = f"{coverage_note(['H1'])}; H2 looks clean"
        entries = [{"lens": "intent", "hunks": ["H1", "H2", "H3", "H4"], "note": note}]

        res = self.cli_fast("review", "cover", "--from-json", "-", stdin_bytes=json.dumps(entries).encode())

        self.assertEqual(res.code, 1)
        self.assertIn("missing: H3, H4", res.err)
        self.assertIn("too little concrete detail: H2", res.err)

    def test_stale_or_missing_round_reports_one_state_error(self) -> None:
        entries = [
            {"lens": "intent", "hunks": ["H1"], "note": coverage_note(["H1"])},
            {"lens": "contracts", "hunks": ["H1"], "note": coverage_note(["H1"])},
        ]
        payload = json.dumps(entries).encode()
        missing = self.cli_fast("review", "cover", "--from-json", "-", stdin_bytes=payload)
        self.assertEqual(missing.code, 1)
        self.assertIn("craftsman review: error:", missing.err)
        self.assertNotIn("entry 1", missing.err)

        self.prepare_bug()
        self.repo.write("cart.py", BUGGY_TOTAL + "# changed\n")
        stale = self.cli_fast("review", "cover", "--from-json", "-", stdin_bytes=payload)
        self.assertEqual(stale.code, 1)
        self.assertIn("craftsman review: error: Code changed since round 1 was prepared", stale.err)
        self.assertNotIn("entry 1", stale.err)

    def test_rejected_findings_receive_distinct_prospective_ids(self) -> None:
        self.prepare_bug()
        bad_one = {
            "lens": "contracts",
            "file": "cart.py",
            "line": 3,
            "severity": "urgent",
            "title": "Empty list crashes on len(items)",
            "scenario": "total([]) divides by zero at len(items) and raises ZeroDivisionError",
        }
        bad_two = {**bad_one, "line": 0, "severity": "major"}

        res = self.cli_fast(
            "review", "add", "--from-json", "-", stdin_bytes=json.dumps([bad_one, bad_two]).encode()
        )

        self.assertEqual(res.code, 1)
        self.assertIn("entry 1 (F1): severity:", res.err)
        self.assertIn("entry 2 (F2): line:", res.err)

    def test_sweep_annotates_only_lenses_rejected_in_same_batch(self) -> None:
        self.prepare_multi_hunk(1, tier="standard")
        design_note = "mod_1.py:1 fn_1(x) adds 2 lines -> single pure helper with no extra layers"
        self.assertEqual(
            self.cli_fast("review", "cover", "--lens", "design", "--files", "mod_1.py", "--note", design_note).code,
            0,
        )
        entries = [
            {"lens": "intent", "hunks": ["H1"], "note": "H1 looks good"},
            {"lens": "sweep", "hunks": ["H1"], "note": coverage_note(["H1"])},
        ]

        res = self.cli_fast("review", "cover", "--from-json", "-", stdin_bytes=json.dumps(entries).encode())

        self.assertEqual(res.code, 1)
        self.assertIn("intent H1 (depends on entry 1)", res.err)
        self.assertIn("contracts H1;", res.err)
        self.assertNotIn("contracts H1 (depends on", res.err)

    def test_note_format_shown_once_for_notes_omitted_elsewhere(self) -> None:
        self.prepare_bug()
        bad_notes = [
            {"lens": "intent", "hunks": ["H1"], "note": "H1 ok"},
            {"lens": "contracts", "hunks": ["H1"], "note": "short"},
        ]
        note_res = self.cli_fast(
            "review", "cover", "--from-json", "-", stdin_bytes=json.dumps(bad_notes).encode()
        )
        self.assertEqual(note_res.err.count("Note format:"), 1)

        self.add()
        verdict_entries = [{"id": "F1", "as": "proven", "evidence": "Reproduced crash at cart.py:3"}]
        verdict_res = self.cli_fast(
            "review", "verdict", "--from-json", "-", stdin_bytes=json.dumps(verdict_entries).encode()
        )
        self.assertEqual(verdict_res.code, 1)
        self.assertNotIn("Note format:", verdict_res.err)

    def test_batch_rejection_caps_output_at_twenty_items(self) -> None:
        self.prepare_bug()
        entries = [{"lens": "intent", "hunks": ["H1"], "note": "H1 ok"} for _ in range(23)]

        res = self.cli_fast("review", "cover", "--from-json", "-", stdin_bytes=json.dumps(entries).encode())

        self.assertEqual(res.code, 1)
        self.assertIn("23 of 23 entries rejected", res.err)
        self.assertIn("entry 20 (intent):", res.err)
        self.assertNotIn("entry 21 (intent):", res.err)
        self.assertIn("  ... and 3 more", res.err)
        self.assertEqual(res.err.count("Note format:"), 1)


class JsonEncodingTests(FastCliTestCase):
    def _pipe_subproc(self, raw_bytes: bytes) -> subprocess.CompletedProcess[bytes]:
        env = {k: v for k, v in os.environ.items() if k not in AMBIENT_PLATFORM_ENV_VARS}
        env["PYTHONPATH"] = str(REPO_ROOT)
        return subprocess.run(
            [sys.executable, "-m", "craftsman.cli", "review", "cover", "--from-json", "-"],
            cwd=self.repo.root,
            env=env,
            input=raw_bytes,
            capture_output=True,
            timeout=30,
        )

    def _payload(self, lens: str) -> str:
        return json.dumps(
            [{"lens": lens, "hunks": ["H1"], "note": f"{coverage_note(['H1'])} — żółć"}],
            ensure_ascii=False,
        )

    def test_from_json_decodes_bom_inputs_from_file_or_stdin(self) -> None:
        for codec in ("utf-8", "utf-8-sig", "utf-16", "utf-16-le", "utf-16-be"):
            with self.subTest(codec=codec):
                self.prepare_bug()
                file_bytes = self._payload("intent").encode("utf-16" if codec == "utf-16-le" else codec)
                if codec == "utf-16-be":
                    file_bytes = b"\xfe\xff" + self._payload("intent").encode("utf-16-be")
                json_file = self.tmp / f"batch-{codec}.json"
                json_file.write_bytes(file_bytes)
                res = self.cli_fast("review", "cover", "--from-json", str(json_file))
                self.assertEqual(res.code, 0, res.err)

                stdin_bytes = self._payload("contracts").encode("utf-16" if codec == "utf-16-le" else codec)
                if codec == "utf-16-be":
                    stdin_bytes = b"\xfe\xff" + self._payload("contracts").encode("utf-16-be")
                piped = self._pipe_subproc(stdin_bytes)
                self.assertEqual(piped.returncode, 0, piped.stderr.decode("utf-8", errors="replace"))
                self.cli_fast("review", "reset")

    def test_from_json_rejects_cp1250_with_utf8_save_hint(self) -> None:
        self.prepare_bug()
        cp1250_bytes = json.dumps(
            [{"lens": "intent", "hunks": ["H1"], "note": f"{coverage_note(['H1'])} źdźbło"}],
            ensure_ascii=False,
        ).encode("cp1250")
        json_file = self.tmp / "cp1250.json"
        json_file.write_bytes(cp1250_bytes)

        from_file = self.cli_fast("review", "cover", "--from-json", str(json_file))
        from_stdin = self.cli_fast("review", "cover", "--from-json", "-", stdin_bytes=cp1250_bytes)

        for result in (from_file, from_stdin):
            self.assertEqual(result.code, 1)
            self.assertIn("save it as UTF-8 (PowerShell: Set-Content -Encoding utf8)", result.err)


class InboxCliTests(FastCliTestCase):
    def _new_inbox_path(self, kind: str = "cover", cwd: Path | None = None) -> Path:
        target_cwd = str(cwd or self.repo.root)
        res = run_main(cli_main, "review", "inbox", kind, "--cwd", target_cwd)
        self.assertEqual(res.code, 0, res.err)
        return Path(res.out.strip())

    def test_inbox_creates_unique_paths_consumed_on_success(self) -> None:
        self.prepare_bug()
        first = self._new_inbox_path("cover")
        second = self._new_inbox_path("cover")
        self.assertNotEqual(first, second)
        self.assertTrue(first.is_file())
        self.assertTrue(second.is_file())

        first.write_text(json.dumps([{"lens": "intent", "hunks": ["H1"], "note": "H1 weak"}]), encoding="utf-8")
        rejected = self.cli_fast("review", "cover", "--from-json", str(first))
        self.assertEqual(rejected.code, 1)
        self.assertTrue(first.is_file())

        first.write_text(
            json.dumps([{"lens": "intent", "hunks": ["H1"], "note": coverage_note(["H1"])}]),
            encoding="utf-8",
        )
        recorded = self.cli_fast("review", "cover", "--from-json", str(first))
        self.assertEqual(recorded.code, 0, recorded.err)
        self.assertFalse(first.exists())
        self.assertTrue(second.is_file())

    def test_external_file_or_inbox_symlink_is_never_deleted(self) -> None:
        self.prepare_bug()
        outside = self.tmp / "outside-cover.json"
        outside.write_text(
            json.dumps([{"lens": "intent", "hunks": ["H1"], "note": coverage_note(["H1"])}]),
            encoding="utf-8",
        )
        self.assertEqual(self.cli_fast("review", "cover", "--from-json", str(outside)).code, 0)
        self.assertTrue(outside.is_file())

        target = self.tmp / "symlink-target.json"
        target.write_text(
            json.dumps([{"lens": "contracts", "hunks": ["H1"], "note": coverage_note(["H1"])}]),
            encoding="utf-8",
        )
        inbox_dir = self.repo.ledger_dir / "inbox"
        inbox_dir.mkdir(parents=True, exist_ok=True)
        link = inbox_dir / "cover-link.json"
        link.symlink_to(target)
        self.assertEqual(self.cli_fast("review", "cover", "--from-json", str(link)).code, 0)
        self.assertTrue(link.is_symlink())
        self.assertTrue(target.is_file())

    def test_inbox_unlink_oserror_warns_without_failing(self) -> None:
        self.prepare_bug()
        inbox_file = self._new_inbox_path("cover")
        inbox_file.write_text(
            json.dumps([{"lens": "intent", "hunks": ["H1"], "note": coverage_note(["H1"])}]),
            encoding="utf-8",
        )
        real_unlink = Path.unlink

        def fail_inbox_unlink(path_obj: Path, missing_ok: bool = False) -> None:
            if path_obj.resolve() == inbox_file.resolve():
                raise PermissionError("locked by antivirus")
            real_unlink(path_obj, missing_ok=missing_ok)

        with mock.patch.object(Path, "unlink", fail_inbox_unlink):
            res = self.cli_fast("review", "cover", "--from-json", str(inbox_file))

        self.assertEqual(res.code, 0)
        self.assertIn("warning: could not remove inbox file", res.err)
        self.assertEqual(self.repo.show()["coverage"]["intent"]["covered"], 1)

    def test_inbox_pruned_by_new_round_reset_or_review_only(self) -> None:
        self.prepare_bug()
        orphan = self._new_inbox_path("cover")
        same_round = self.cli_fast("review", "prepare", "--tier", "quick")
        self.assertEqual(same_round.code, 0)
        self.assertTrue(orphan.is_file())

        self.repo.write("cart.py", BUGGY_TOTAL + "# next round\n")
        next_round = self.cli_fast("review", "prepare", "--tier", "quick")
        self.assertEqual(next_round.code, 0)
        self.assertFalse(orphan.exists())
        self.assertIn("Pruned 1 inbox file(s):", next_round.err)

        second_orphan = self._new_inbox_path("add")
        report_ro = self.cli_fast("review", "report", "--review-only")
        self.assertEqual(report_ro.code, 0)
        self.assertFalse(second_orphan.exists())
        self.assertIn("Pruned 1 inbox file(s):", report_ro.err)

        third_orphan = self._new_inbox_path("verdict")
        reset = self.cli_fast("review", "reset")
        self.assertEqual(reset.code, 0)
        self.assertFalse(third_orphan.exists())
        self.assertIn("Pruned 1 inbox file(s):", reset.err)

    def test_inbox_pruned_when_gate_complete_succeeds(self) -> None:
        self.repo.write("cart.py", BUGGY_TOTAL)
        self.assertEqual(self.cli_fast("gate", "--init", "--tier", "quick").code, 0)
        self.assertEqual(self.cli_fast("review", "prepare", "--tier", "quick").code, 0)
        orphan = self._new_inbox_path("cover")

        blocked = self.cli_fast("gate", "--complete")
        self.assertEqual(blocked.code, 1)
        self.assertTrue(orphan.is_file())

        self.repo.cover_all()
        self.run_tests()
        completed = self.cli_fast("gate", "--complete")
        self.assertEqual(completed.code, 0, completed.err)
        self.assertFalse(orphan.exists())
        self.assertIn("Pruned 1 inbox file(s):", completed.err)

    def test_linked_worktree_inbox_passes_scope_then_consumes(self) -> None:
        wt = self.tmp / "wt-linked"
        self.repo.git("worktree", "add", "-q", "-b", "wt-branch", str(wt))
        (wt / "cart.py").write_text(BUGGY_TOTAL, encoding="utf-8")
        prep = run_main(cli_main, "review", "prepare", "--tier", "quick", "--cwd", str(wt))
        self.assertEqual(prep.code, 0, prep.err)

        inbox_file = self._new_inbox_path("cover", cwd=wt)
        policy = ScopePolicy(mode="deny")
        allowed = evaluate_scope("Read", {"file_path": str(inbox_file)}, wt, wt, policy)
        denied = evaluate_scope("Read", {"file_path": str(inbox_file)}, self.repo.root, self.repo.root, policy)
        self.assertEqual(allowed.decision, "allow", allowed.reason)
        self.assertEqual(denied.decision, "deny")

        inbox_file.write_text(
            json.dumps([{"lens": "intent", "hunks": ["H1"], "note": coverage_note(["H1"])}]),
            encoding="utf-8",
        )
        res = run_main(cli_main, "review", "cover", "--from-json", str(inbox_file), "--cwd", str(wt))
        self.assertEqual(res.code, 0, res.err)
        self.assertFalse(inbox_file.exists())

    def test_check_flag_validates_without_recording_or_consuming(self) -> None:
        self.prepare_bug()
        inbox_file = self._new_inbox_path("cover")
        inbox_file.write_text(
            json.dumps([{"lens": "intent", "hunks": ["H1"], "note": coverage_note(["H1"])}]),
            encoding="utf-8",
        )
        self.assertEqual(self.cli_fast("review", "cover", "--check", "--from-json", str(inbox_file)).code, 0)
        self.assertEqual(
            self.cli_fast(
                "review", "cover", "--check", "--lens", "intent", "--hunks", "H1", "--note", coverage_note(["H1"])
            ).code,
            0,
        )
        self.assertTrue(inbox_file.is_file())
        self.assertEqual(self.repo.show()["coverage"]["intent"]["covered"], 0)

        add_inbox = self._new_inbox_path("add")
        add_inbox.write_text(
            json.dumps([{
                "lens": "contracts", "file": "cart.py", "line": 3, "severity": "major",
                "title": "Empty cart divides by zero",
                "scenario": "total([]) evaluates sum([]) / len([]) and raises ZeroDivisionError",
            }]),
            encoding="utf-8",
        )
        self.assertEqual(self.cli_fast("review", "add", "--check", "--from-json", str(add_inbox)).code, 0)
        self.assertTrue(add_inbox.is_file())
        self.assertEqual(
            self.cli_fast("review", "add", "--check", "--file", "cart.py", "--line", "0", *FINDING_FLAGS).code,
            1,
        )
        self.assertEqual(
            self.cli_fast("review", "add", "--check", "--file", "cart.py", "--line", "3", *FINDING_FLAGS).code,
            0,
        )
        self.assertEqual(self.repo.show()["findings"], [])

        self.add()
        verdict_inbox = self._new_inbox_path("verdict")
        verdict_inbox.write_text(
            json.dumps([{"id": "F1", "as": "probable", "evidence": STATIC_EVIDENCE}]), encoding="utf-8"
        )
        self.assertEqual(
            self.cli_fast("review", "verdict", "--check", "--from-json", str(verdict_inbox)).code, 0
        )
        self.assertTrue(verdict_inbox.is_file())
        self.assertEqual(
            self.cli_fast(
                "review", "verdict", "--check", "F1", "--as", "probable", "--evidence", STATIC_EVIDENCE
            ).code,
            0,
        )
        self.assertIsNone(self.repo.show()["findings"][0]["verdict"])


if __name__ == "__main__":
    unittest.main()
