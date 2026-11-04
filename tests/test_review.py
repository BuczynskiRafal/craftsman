"""End-to-end tests for `craftsman review` against throwaway repositories."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest import mock

from craftsman.cli import main as cli_main
from craftsman.gate import GateOwner
from craftsman.review import normalize_round_files, view_slug
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
    open_gate,
    prepared,
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
SHELL_OUT = "import subprocess\n\n\ndef go(cmd):\n    subprocess.run(cmd, shell=True)\n"
LONG_FUNCTION = "def over_budget(x: int) -> int:\n{body}\n    return step_44\n".format(
    body="\n".join(f"    step_{i} = x + {i}" for i in range(45))
)


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

    def prepare_bug(self, deep: bool = False) -> None:
        self.repo.write("cart.py", BUGGY_TOTAL)
        self.repo.cli("review", "prepare", *(["--deep"] if deep else []), "--task", "Add cart totals")

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

    def cover(self, *hunks: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        note = coverage_note(list(hunks))
        return self.review("cover", "--hunks", ",".join(hunks), "--note", note, check=check)

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

        out = self.review("prepare", "--task", "Add cart totals").stdout

        self.assertIn(
            "Round 1 prepared: 1 files, 1 hunks, 3 changed lines; 0 hunks carried, 1 to review.", out
        )
        self.assertIn("**Task:** Add cart totals", self.packet())
        self.assertIn("Scope: `total` (function, L1–L3)", self.packet())

    def test_prepare_prints_the_risk_map(self) -> None:
        self.repo.write("cart.py", BUGGY_TOTAL)

        out = self.review("prepare").stdout

        self.assertIn("Risk map (suggested lenses):\n  intent — core lens\n", out)
        self.assertIn("  craft — simplicity lens\n", out)
        self.assertNotIn("  trust", out)
        self.assertIn(f"Packet: {self.repo.ledger_dir / 'packet-1.md'}", out)

    def test_risk_map_names_signals_and_hunks_and_hints_at_depth(self) -> None:
        self.repo.write("run.py", SHELL_OUT)

        out = self.review("prepare").stdout

        self.assertRegex(out, r"  trust — signals: process \(H1\)")
        self.assertIn("Depth: deep suggested (signals: process H1)", out)
        self.assertIn("signals: process (→ failure, trust, platform)", self.packet())

    def test_a_deep_round_suggests_every_lens_and_needs_no_depth_hint(self) -> None:
        self.repo.write("run.py", SHELL_OUT)

        out = self.review("prepare", "--deep").stdout

        self.assertIn("Round 1 prepared (deep)", out)
        self.assertIn("  model — deep round\n", out)
        self.assertNotIn("Depth:", out)

    def test_the_depth_hint_is_not_repeated_under_a_deep_gate(self) -> None:
        self.repo.cli("gate", "--init", "--deep")
        self.repo.write("run.py", SHELL_OUT)

        out = self.review("prepare").stdout

        self.assertIn("Round 1 prepared (deep)", out)
        self.assertNotIn("Depth:", out)

    def test_the_depth_hint_needs_real_usage_in_production_code(self) -> None:
        self.repo.write("login.py", "def check(token, session_id):\n    return token == session_id\n")
        self.repo.write("tests/test_run.py", SHELL_OUT)
        self.repo.write("deploy.yaml", "token: abc\nrun: os.system(rm)\n")

        out = self.review("prepare").stdout

        self.assertNotIn("Depth:", out)

    def test_a_deep_round_over_docs_still_suggests_every_lens(self) -> None:
        self.repo.write("README.md", "# Cart\n\nTotals.\n")

        out = self.review("prepare", "--deep").stdout

        self.assertIn("  trust — deep round\n", out)

    def test_an_agent_prompt_file_is_not_a_docs_change(self) -> None:
        self.repo.write("skills/x/SKILL.md", "# Skill\n\nRun `rm -rf build` first.\n")
        self.repo.write("README.md", "# Cart\n")

        out = self.review("prepare").stdout

        self.assertNotIn("docs-only change", out)
        self.assertIn("  contracts — core lens\n", out)

    def test_prose_changes_get_the_docs_lenses(self) -> None:
        self.repo.write("README.md", "# Cart\n")

        out = self.review("prepare").stdout

        self.assertIn("  intent — docs-only change\n", out)
        self.assertNotIn("  contracts", out)

    def test_later_rounds_read_signals_from_the_open_hunks_only(self) -> None:
        self.repo.write("run.py", SHELL_OUT)
        self.repo.write("big.py", LONG_FUNCTION)
        self.repo.write("cart.py", BUGGY_TOTAL)
        first = self.review("prepare").stdout
        self.assertIn("Depth: deep suggested", first)
        self.assertIn("Quality hints:", first)
        self.repo.record_full_evidence()
        self.repo.write("cart.py", BUGGY_TOTAL.replace("/ len(items)", ""))

        out = self.review("prepare").stdout

        self.assertIn("2 hunks carried, 1 to review.", out)
        self.assertNotIn("Depth:", out)
        self.assertNotIn("Quality hints:", out)
        self.assertNotIn("trust", out)
        rounds = json.loads((self.repo.ledger_dir / "ledger.json").read_text(encoding="utf-8"))["rounds"]
        self.assertTrue(rounds[0]["signals"])
        self.assertEqual(rounds[1]["signals"], [])

    def test_preparing_the_same_diff_again_keeps_the_round(self) -> None:
        self.prepare_bug()

        again = self.review("prepare").stdout

        self.assertIn("Round 1 unchanged (same diff)", again)
        self.assertEqual(self.repo.show()["round"]["number"], 1)

    def test_preparing_an_unchanged_diff_deep_raises_the_round_in_place(self) -> None:
        self.prepare_bug()
        self.repo.cover_all()

        out = self.review("prepare", "--deep").stdout

        shown = self.repo.show()
        self.assertIn("Round 1 raised to deep (same diff)", out)
        self.assertEqual((shown["round"]["number"], shown["round"]["deep"]), (1, True))
        self.assertEqual(shown["coverage"]["missing"], [])
        self.assertIn("deep round", self.packet())

    def test_a_review_stays_deep_once_it_was_deep(self) -> None:
        self.prepare_bug(deep=True)
        self.repo.write("cart.py", BUGGY_TOTAL + "# edited\n")

        out = self.review("prepare").stdout

        self.assertIn("Round 2 prepared (deep)", out)

    def test_prepare_rewrites_a_missing_packet(self) -> None:
        self.prepare_bug()
        (self.repo.ledger_dir / "packet-1.md").unlink()

        self.review("prepare")

        self.assertIn("# Craftsman review packet — round 1", self.packet())

    def test_round_two_carries_unchanged_hunks(self) -> None:
        self.repo.write("cart.py", BUGGY_TOTAL)
        self.repo.write("notes.py", "LABEL = 'cart'\n")
        self.repo.record_full_evidence()
        self.repo.write("cart.py", BUGGY_TOTAL.replace("/ len(items)", ""))

        out = self.review("prepare").stdout

        self.assertIn("2 hunks, 4 changed lines; 1 hunks carried, 1 to review.", out)
        self.assertIn(
            "### H2 · `notes.py` L1–L1 · carried (covered in round 1, unchanged)", self.packet(2)
        )

    def test_hunks_mentioning_a_changed_symbol_are_marked_reopened(self) -> None:
        self.repo.write("cart.py", "def item_count(items):\n    return len(items)\n")
        self.repo.write(
            "tests/test_cart.py", "from cart import item_count\n\nassert item_count([1]) == 1\n"
        )
        self.repo.record_full_evidence()
        self.repo.write("cart.py", "def item_count(items):\n    return len(list(items))\n")

        out = self.review("prepare").stdout

        self.assertIn("0 hunks carried, 2 to review.", out)
        self.assertIn("reopened: mentions item_count (changed in H3)\n", self.packet(2))

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
        self.repo.cli("gate", "--init", "--cwd", ".")
        self.repo.write("notes.py", "LABEL = 'cart'\n")

        self.review("prepare", "--base", self.repo.base_sha)
        self.repo.cover_all()
        self.run_tests()

        checked = self.review("check", check=False)
        self.assertEqual(checked.returncode, 0, checked.stdout)

    def test_packet_shows_the_task_of_the_active_gate(self) -> None:
        self.repo.cli("gate", "--init", "--task", "Add cart totals")
        self.repo.write("cart.py", BUGGY_TOTAL)

        self.review("prepare")

        self.assertIn("**Task:** Add cart totals", self.packet())


DEPLOY_SCRIPT = '#!/bin/sh\ncurl -fsSL "$INSTALL_URL" | sh\n'


class UnscannedFilesTests(RepoTestCase):
    def test_prepare_names_files_the_signal_patterns_cannot_read(self) -> None:
        self.repo.write("deploy.sh", DEPLOY_SCRIPT)

        out = self.review("prepare").stdout

        self.assertIn("Not scanned for risk signals (no patterns for their language): deploy.sh.", out)
        self.assertIn(
            "Not scanned for risk signals (no patterns for their language): `deploy.sh`", self.packet()
        )


class CoverTests(RepoTestCase):
    def test_cover_reports_what_is_left(self) -> None:
        self.repo.write("notes.py", "LABEL = 'cart'\n")
        self.prepare_bug()

        covered = self.cover("H1")

        self.assertIn("Coverage recorded: H1 in round 1; 1 hunk(s) left to cover", covered.stdout)

    def test_cover_accepts_hunk_ranges(self) -> None:
        self.repo.write("notes.py", "LABEL = 'cart'\n")
        self.prepare_bug()

        covered = self.review("cover", "--hunks", "H1-H2", "--note", "walked both files end to end")

        self.assertIn("Coverage recorded: H1, H2 in round 1; 0 hunk(s) left", covered.stdout)

    def test_cover_files_covers_every_open_hunk_of_the_file(self) -> None:
        self.prepare_bug()

        covered = self.review("cover", "--files", "cart.py", "--note", "read cart.py and its callers")

        self.assertIn("Coverage recorded: cart.py in round 1; 0 hunk(s) left", covered.stdout)

    def test_cover_refuses_a_stale_round(self) -> None:
        self.prepare_bug()
        self.repo.write("cart.py", BUGGY_TOTAL + "# edited\n")

        self.assert_error(self.cover("H1", check=False), 1, "Code changed since round 1")

    def test_cover_needs_exactly_one_target_kind(self) -> None:
        self.prepare_bug()

        neither = self.review("cover", "--note", "x" * 25, check=False)
        both = self.review("cover", "--hunks", "H1", "--files", "cart.py", "--note", "x" * 25, check=False)

        self.assert_error(neither, 1, "pass exactly one of --hunks or --files")
        self.assert_error(both, 1, "pass exactly one of --hunks or --files")

    def test_cover_without_a_note_is_a_usage_error(self) -> None:
        proc = self.review("cover", "--hunks", "H1", check=False)

        self.assert_error(proc, 2, "the following arguments are required: --note")

    def test_the_lens_option_is_gone(self) -> None:
        proc = self.review("cover", "--lens", "intent", "--hunks", "H1", "--note", "x", check=False)

        self.assert_error(proc, 2, "unrecognized arguments: --lens intent")

    def test_note_error_names_the_note_flag(self) -> None:
        self.prepare_bug()

        proc = self.review("cover", "--hunks", "H1", "--note", "H1 ok", check=False)

        self.assert_error(proc, 1, "error: --note must have at least 20 characters")

    def test_unknown_hunk_or_file_is_refused(self) -> None:
        self.prepare_bug()

        self.assert_error(self.cover("H7", check=False), 1, "unknown hunk 'H7'")
        unknown = self.review("cover", "--files", "nope.py", "--note", "x" * 25, check=False)
        self.assert_error(unknown, 1, "unknown files in round 1: nope.py")


class SecondRoundTests(RepoTestCase):
    """Round 2 of a review: `notes.py` keeps its covered hunk, `cart.py` is a new one."""

    def setUp(self) -> None:
        super().setUp()
        self.repo.write("cart.py", BUGGY_TOTAL)
        self.repo.write("notes.py", "LABEL = 'cart'\n")
        self.repo.record_full_evidence()
        self.repo.write("cart.py", BUGGY_TOTAL.replace("/ len(items)", ""))
        self.review("prepare")

    def missing(self) -> list[str]:
        missing = self.repo.show()["coverage"]["missing"]
        assert isinstance(missing, list)
        return missing

    def test_hunk_ids_are_never_reused_for_another_hunk(self) -> None:
        self.assertEqual(self.missing(), ["H3"])
        stale_id = self.cover("H1", check=False)  # H1 was the cart.py hunk of round 1

        self.assert_error(stale_id, 1, "unknown hunk 'H1'")
        self.assertEqual(self.missing(), ["H3"])

    def test_an_unchanged_hunk_keeps_its_id(self) -> None:
        shown = self.review("show", "--hunks").stdout

        self.assertEqual(self.missing(), ["H3"])
        self.assertNotIn("notes.py", shown)  # carried: H2 is not listed as open
        self.assertIn("carried (covered in round 1, unchanged)", self.packet(2).split("### H2")[1])

    def test_a_range_over_carried_hunks_covers_the_open_ones(self) -> None:
        covered = self.review("cover", "--hunks", "H1-H3", "--note", "walked the whole range once")

        self.assertIn("Coverage recorded: H3 in round 2 (1 carried hunk(s) skipped)", covered.stdout)
        self.assertEqual(self.missing(), [])

    def test_a_file_of_carried_hunks_is_a_no_op(self) -> None:
        covered = self.review("cover", "--files", "notes.py", "--note", "nothing new in this file")

        self.assertIn("Nothing to record: H2 already covered in an earlier round", covered.stdout)
        self.assertEqual(self.missing(), ["H3"])

    def test_a_batch_may_name_carried_hunks_and_files(self) -> None:
        batch = json.dumps(
            [
                {"files": ["notes.py"], "note": "nothing new in the carried file"},
                {"hunks": ["H2", "H3"], "note": "read the open hunk and its neighbour"},
            ]
        )

        self.review("cover", "--from-json", "-", stdin=batch)

        self.assertEqual(self.missing(), [])

    def test_files_are_found_by_any_spelling(self) -> None:
        for spelling in ("./cart.py", str(self.repo.root / "cart.py"), "cart.py"):
            with self.subTest(spelling=spelling):
                path = Path(self.review("view", "--files", spelling).stdout.strip())
                self.assertEqual(path.name, f"packet-2.{view_slug(spelling, None)}.md")
        covered = self.review("cover", "--files", "./cart.py", "--note", "read the open hunk twice")
        self.assertIn("Coverage recorded: cart.py in round 2", covered.stdout)
        self.assertEqual(self.missing(), [])


class NormalizeFilesTests(unittest.TestCase):
    def test_backslashes_and_the_old_path_of_a_rename_name_the_new_file(self) -> None:
        old = make_hunk("H1", "pkg/new.py")
        renamed = replace(old, old_path="pkg/old.py", status="renamed")
        snap = make_snap(renamed, make_hunk("H2", "pkg/other.py"))
        _, round_ = prepared(snap)
        root = Path(tempfile.gettempdir()).resolve()

        names = normalize_round_files(
            root, root, "pkg\\other.py,pkg/old.py, ./pkg/new.py ,missing.py", round_, snap
        )

        self.assertEqual(names, ["pkg/other.py", "pkg/new.py", "pkg/new.py", "missing.py"])


class GateDepthTests(RepoTestCase):
    def test_a_foreign_session_cannot_raise_the_gate_with_prepare(self) -> None:
        self.repo.write("cart.py", BUGGY_TOTAL)
        open_gate(
            self.repo.root, self.repo.base_sha, owner=GateOwner("claude", "owner-session")
        )
        other = {"CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "someone-else"}

        out = self.repo.cli("review", "prepare", "--deep", env_extra=other).stdout

        self.assertIn("--deep ignored", out)
        self.assertFalse(self.repo.state()["deep"])
        self.assertFalse(self.repo.show()["round"]["deep"])

    def test_the_owner_raises_the_gate_with_prepare(self) -> None:
        self.repo.write("cart.py", BUGGY_TOTAL)
        open_gate(self.repo.root, self.repo.base_sha, owner=GateOwner("claude", "owner-session"))
        owner = {"CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "owner-session"}

        out = self.repo.cli("review", "prepare", "--deep", env_extra=owner).stdout

        self.assertIn("Gate raised to deep", out)
        self.assertTrue(self.repo.state()["deep"])


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

    def test_add_resolves_files_against_cwd(self) -> None:
        self.repo.write("pkg/total.py", BUGGY_TOTAL)
        self.review("prepare")

        added = self.add("--cwd", str(self.repo.root / "pkg"), file="total.py")

        self.assertIn("F1 recorded (major, contracts, pkg/total.py:3)", added.stdout)

    def test_add_accepts_a_file_the_diff_deletes(self) -> None:
        self.repo.write("cart.py", BUGGY_TOTAL)
        self.repo.git("add", "-A")
        self.repo.git("commit", "-q", "--no-verify", "-m", "cart")
        self.repo.cli("gate", "--init", "--cwd", ".")
        (self.repo.root / "cart.py").unlink()
        self.review("prepare")

        gone = self.add()

        self.assertIn("deleted in the diff; line 3 refers to the base version", gone.stdout)

    @unittest.skipIf(sys.platform == "win32", "symlinks need extra privileges on Windows")
    def test_add_keeps_the_path_of_a_symlink(self) -> None:
        self.repo.write("cart.py", BUGGY_TOTAL)
        outside = self.tmp / "shared.py"
        outside.write_text(BUGGY_TOTAL, encoding="utf-8")
        (self.repo.root / "linked.py").symlink_to(outside)
        self.review("prepare")

        added = self.add(file="linked.py")

        self.assertIn("F1 recorded (major, contracts, linked.py:3)", added.stdout)

    def test_add_refuses_a_line_past_the_end_of_the_file(self) -> None:
        self.prepare_bug()

        past = self.add(line=40, check=False)

        self.assert_error(past, 1, "error: --line 40 is past the end of cart.py, which has 3 lines")

    def test_line_count_ignores_form_feeds(self) -> None:
        self.repo.write("cart.py", BUGGY_TOTAL)
        (self.repo.root / "feed.py").write_bytes(b"a = 1\x0cb = 2\nc = 3\n")
        self.review("prepare")

        feed = self.add(file="feed.py", check=False)

        self.assert_error(feed, 1, "error: --line 3 is past the end of feed.py, which has 2 lines")

    def test_domain_errors_exit_1_without_traceback(self) -> None:
        self.prepare_bug()
        outside = self.tmp / "outside.py"
        outside.write_text("x = 1\n", encoding="utf-8")
        cases = [
            (("cover", "--hunks", "H7", "--note", coverage_note(["H7"])), "unknown hunk"),
            (("cover", "--hunks", "H1", "--note", "x" * 10), "--note must have at least 20"),
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

    def test_a_severity_marker_may_precede_the_evidence_prefix(self) -> None:
        probable = self.verdict("F1", "probable", f"[severity: minor] {STATIC_EVIDENCE}")
        self.assertEqual(probable.stdout.strip(), "F1 verdict recorded: probable")

        self.add()
        question = self.verdict(
            "F2", "question", "[severity: nit] intent: task asks for totals, cart.py:3 averages"
        )
        self.assertEqual(question.stdout.strip(), "F2 verdict recorded: question")

    def test_the_marker_does_not_replace_the_required_prefix(self) -> None:
        proc = self.verdict("F1", "probable", "[severity: minor] it is probably wrong at cart.py:3", check=False)

        self.assert_error(proc, 1, "must start with")

    def test_a_verdict_batch_takes_the_run_key_for_proven(self) -> None:
        self.repo.cli(
            "run", "--kind", "repro", "--", sys.executable, "-c", "from cart import total; total([])",
            check=False,
        )
        entry = {"id": "F1", "as": "proven", "evidence": "R1 raised ZeroDivisionError", "run": "R1"}

        out = self.review("verdict", "--from-json", "-", stdin=json.dumps([entry])).stdout

        self.assertEqual(out.strip(), "F1 verdict recorded: proven (run R1)")

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
            {"hunks": ["H1"], "note": coverage_note(["H1"])},
            {"files": ["cart.py"], "note": "read cart.py and the code that calls it"},
        ]

        covered = self.from_json("cover", entries)

        self.assertEqual(len(covered.stdout.splitlines()), 2)
        self.assertIn("0 hunk(s) left to cover", covered.stdout)

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
        entries = [
            {"hunks": ["H1"], "note": coverage_note(["H1"])},
            {"hunks": ["H1"], "note": "too short"},
        ]

        failed = self.from_json("cover", entries, check=False)

        self.assert_error(failed, 1, "entry 2 (H1): note: must have at least 20 characters")
        self.assertEqual(self.repo.show()["coverage"]["covered"], 0)

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

    def test_every_problem_of_an_entry_is_reported_at_once(self) -> None:
        entry = self.finding(severity="huge", title="x", scenario="short", extra=1)

        failed = self.from_json("add", [entry], check=False)

        self.assert_error(failed, 1, "1 of 1 entry rejected")
        for fragment in ("severity: must be one of", "title: must have", "scenario: must have", "extra: is not"):
            self.assertIn(fragment, failed.stderr)

    def test_a_bad_hunk_does_not_hide_a_short_note(self) -> None:
        failed = self.from_json("cover", [{"hunks": ["H9"], "note": "short"}], check=False)

        self.assert_error(failed, 1, "1 of 1 entry rejected")
        self.assertIn("H9", failed.stderr)
        self.assertIn("note: must have at least 20 characters", failed.stderr)

    def test_a_cover_entry_without_targets_names_both_keys(self) -> None:
        failed = self.from_json("cover", [{"note": coverage_note(["H1"])}], check=False)

        self.assert_error(failed, 1, "pass 'hunks' or 'files'")

    def test_each_entry_reports_its_own_problems(self) -> None:
        entries = [self.finding(title="x"), self.finding(severity="huge", scenario="s")]

        failed = self.from_json("add", entries, check=False)

        self.assert_error(failed, 1, "2 of 2 entries rejected")
        self.assertIn("entry 1 (F1): title:", failed.stderr)
        self.assertIn("entry 2 (F2): severity:", failed.stderr)
        self.assertIn("entry 2 (F2): scenario:", failed.stderr)

    def test_unknown_entry_key_is_rejected(self) -> None:
        entries = [{"hunks": ["H1"], "note": coverage_note(["H1"]), "extra": 1}]

        failed = self.from_json("cover", entries, check=False)

        self.assert_error(failed, 1, "entry 1 (H1): extra: is not a known field")

    def test_the_lens_key_is_gone(self) -> None:
        entries = [{"lens": "intent", "hunks": ["H1"], "note": coverage_note(["H1"])}]

        failed = self.from_json("cover", entries, check=False)

        self.assert_error(failed, 1, "entry 1 (H1): lens: is not a known field")

    def test_the_signal_key_is_gone(self) -> None:
        for key in ("signal", "signal_id"):
            with self.subTest(key):
                failed = self.from_json("add", [self.finding(**{key: "S1"})], check=False)

                self.assert_error(failed, 1, f"{key}: is not a known field")

    def test_empty_list_is_rejected(self) -> None:
        self.assert_error(
            self.from_json("cover", [], check=False), 1, "--from-json list must not be empty"
        )

    def test_optional_entry_key_may_not_be_null(self) -> None:
        failed = self.from_json("add", [self.finding(suggestion=None)], check=False)

        self.assert_error(failed, 1, "entry 1 (F1): suggestion: must be a string")

    def test_entry_takes_hunks_or_files_not_both(self) -> None:
        entries = [{"hunks": ["H1"], "files": ["cart.py"], "note": coverage_note(["H1"])}]

        failed = self.from_json("cover", entries, check=False)

        self.assert_error(failed, 1, "pass either 'hunks' or 'files', not both")

    def test_from_json_cannot_be_combined_with_entry_flags(self) -> None:
        cases = [
            ("cover", ("--hunks", "H1"), "--from-json cannot be combined with --hunks"),
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
        self.assertIn("[uncovered] 1 hunk(s) uncovered in round 1: H1", blocked.stdout)

    def test_check_passes_with_full_evidence(self) -> None:
        self.repo.write("cart.py", BUGGY_TOTAL)
        self.repo.record_full_evidence()

        self.assertTrue(self.json_of("check")["passed"])

    def test_a_deeper_round_raises_the_gate_and_is_held_to_it(self) -> None:
        self.repo.write("cart.py", BUGGY_TOTAL)
        self.repo.cli("gate", "--init")
        out = self.review("prepare", "--deep").stdout
        self.repo.cover_all()
        self.run_tests()

        completed = self.repo.cli("gate", "--complete", check=False)

        self.assertIn("Gate raised to deep", out)
        self.assertTrue(self.repo.state()["deep"])
        self.assertTrue(self.json_of("report")["deep"])
        self.assert_error(completed, 1, "[no-e2e-run]")

    def test_a_round_prepared_deep_makes_check_deep_without_a_gate(self) -> None:
        self.repo.write("cart.py", BUGGY_TOTAL)
        self.review("prepare", "--deep")
        self.repo.cover_all()
        self.run_tests()

        blocked = self.review("check", check=False)

        self.assertIn("[no-e2e-run]", blocked.stdout)

    def test_show_lists_coverage(self) -> None:
        self.prepare_bug()

        shown = self.review("show").stdout

        self.assertIn("Round 1 prepared", shown)
        self.assertIn("0/1 hunks (missing H1)", shown)
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
        self.repo.record_full_evidence()

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
        self.repo.cli("gate", "--init", "--cwd", ".")

        refused = self.review("check", "--review-only", check=False)

        self.assert_error(refused, 1, "--review-only is not allowed during an active /craftsman gate")


class QualityHintTests(RepoTestCase):
    def prepare_long_function(self) -> None:
        self.repo.write("long_mod.py", LONG_FUNCTION)
        self.review("prepare")
        self.repo.cover_all()
        self.run_tests("import long_mod")

    def prepare_oversized(self) -> None:
        lines = "".join(f"CONST_{i} = {i}\n" for i in range(300))
        for name in ("mod_a.py", "mod_b.py", "mod_c.py"):
            self.repo.write(name, lines)
        self.review("prepare")

    def test_packet_lists_the_quality_hints(self) -> None:
        self.prepare_long_function()

        self.assertIn("- **S1** (`long-function`) `long_mod.py:1`", self.packet())

    def test_near_identical_functions_cannot_flood_prepare_the_packet_or_a_view(self) -> None:
        body = "".join(f"    step_{n} = value + {n}\n" for n in range(8))
        handlers = "".join(f"def handler_{index}(value):\n{body}    return step_7\n\n" for index in range(40))
        self.repo.write("handlers.py", handlers)
        self.repo.write("small.py", "x = 1\ny = 2\n")

        out = self.review("prepare").stdout
        view = Path(self.review("view", "--files", "small.py").stdout.strip()).read_text(encoding="utf-8")

        self.assertLess(len(out), 6_000)
        self.assertRegex(out, r"Quality hints: \d+ hints \(.*duplicate-logic \d+.*\); \+\d+ more not listed")
        self.assertLess(len(self.packet()), 60_000)
        self.assertLess(len(view), 12_000)

    def test_quality_hints_do_not_block_the_gate(self) -> None:
        self.prepare_long_function()

        self.assertEqual(self.review("check", check=False).returncode, 0)

    def test_oversized_diff_is_a_warning_and_leaves_the_gate_running(self) -> None:
        self.repo.cli("gate", "--init", "--cwd", ".")

        self.prepare_oversized()

        state = json.loads((self.repo.ledger_dir / "craftsman_state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["stage"], "implement")
        self.assertIn("Very large diff", self.review("report").stdout)

    def test_removed_flags_are_rejected(self) -> None:
        self.prepare_bug()
        finding = ("--file", "cart.py", "--line", "3", *FINDING_FLAGS)
        for label, args in (
            ("gate --init --tier", ("gate", "--init", "--tier", "deep")),
            ("gate --tier", ("gate", "--tier", "deep")),
            ("review prepare --tier", ("review", "prepare", "--tier", "deep")),
            ("review prepare --add-lens", ("review", "prepare", "--add-lens", "trust")),
            ("review add --signal", ("review", "add", *finding, "--signal", "S1")),
        ):
            with self.subTest(label):
                self.assert_error(self.repo.cli(*args, check=False), 2, "unrecognized arguments")

    def test_ack_and_fact_commands_are_gone(self) -> None:
        self.assert_error(self.review("ack", "S1", check=False), 2, "invalid choice")
        self.assert_error(self.review("fact", check=False), 2, "invalid choice")


class ViewIsolationTests(RepoTestCase):
    def test_findings_never_appear_in_a_reviewer_view(self) -> None:
        self.prepare_bug()
        self.add("--title", "SECRET_FINDING_TITLE_FOR_VERIFIER_ONLY")

        view = Path(self.review("view", "--hunks", "H1").stdout.strip()).read_text(encoding="utf-8")

        self.assertNotIn("SECRET_FINDING_TITLE_FOR_VERIFIER_ONLY", view)


class ViewTests(RepoTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.repo.write("other.py", "def other():\n    return 1\n")
        self.prepare_bug()

    def test_hunk_view_is_written_next_to_the_packet(self) -> None:
        path = Path(self.review("view", "--hunks", "H1").stdout.strip())

        self.assertEqual(path.name, "packet-1.H1.md")
        self.assertTrue(path.is_file())

    def test_file_view_holds_the_hunks_of_those_files(self) -> None:
        path = Path(self.review("view", "--files", "other.py").stdout.strip())
        text = path.read_text(encoding="utf-8")

        self.assertRegex(path.name, r"\Apacket-1\.other-py-[0-9a-f]{8}\.md\Z")
        self.assertIn("`other.py`", text.split("## Hunks")[1].split("### Other hunks")[0])

    def test_view_needs_exactly_one_selector(self) -> None:
        self.assert_error(self.review("view", check=False), 1, "pass exactly one of --files or --hunks")
        both = self.review("view", "--files", "cart.py", "--hunks", "H1", check=False)
        self.assert_error(both, 1, "pass exactly one of --files or --hunks")

    def test_view_rejects_unknown_files_and_hunks(self) -> None:
        self.assert_error(self.review("view", "--files", "nope.py", check=False), 1, "not in round 1")
        self.assert_error(self.review("view", "--hunks", "H99", check=False), 1, "unknown hunk")

    def test_lens_and_shard_options_are_gone(self) -> None:
        self.assert_error(self.review("view", "--lenses", "intent", check=False), 2, "unrecognized arguments")
        self.assert_error(self.review("view", "--shard", "1/2", check=False), 2, "unrecognized arguments")

    def test_view_refuses_a_stale_round(self) -> None:
        self.repo.write("cart.py", BUGGY_TOTAL + "# edited\n")

        proc = self.review("view", "--hunks", "H1", check=False)

        self.assert_error(proc, 1, "Code changed since round 1 was prepared")


class ViewSlugTests(unittest.TestCase):
    def test_slug_names_the_hunk_selection_in_runs(self) -> None:
        self.assertEqual(view_slug(hunks=("H1", "H2", "H3", "H7")), "H1-H3_H7")

    def test_slug_ignores_the_order_and_repeats_of_a_selection(self) -> None:
        self.assertEqual(view_slug(hunks=("H7", "H1", "H7", "H2")), view_slug(hunks=("H1", "H2", "H7")))

    def test_different_hunk_sets_get_different_slugs(self) -> None:
        self.assertNotEqual(view_slug(hunks=("H1", "H3")), view_slug(hunks=("H1", "H2", "H3")))

    def test_paths_that_flatten_alike_get_different_slugs(self) -> None:
        for first, second in (
            ("pkg/a-b.py", "pkg/a/b.py"),
            ("other-file.py", "other_file.py"),
            ("a.py", "a-py"),
        ):
            with self.subTest(first=first, second=second):
                self.assertNotEqual(view_slug(files=(first,)), view_slug(files=(second,)))

    def test_slug_names_the_files_with_a_hash(self) -> None:
        self.assertRegex(view_slug(files=("pkg/b.py", "pkg/a.py")), r"\Apkg-a-py-pkg-b-py-[0-9a-f]{8}\Z")

    def test_long_selections_are_cut_to_the_limit_and_stay_distinct(self) -> None:
        shared = tuple(f"pkg/module_{index}/handler.py" for index in range(10))
        first, second = view_slug(files=(*shared, "a.py")), view_slug(files=(*shared, "b.py"))

        self.assertLessEqual(len(first), 60)
        self.assertNotEqual(first, second)
        every_other = tuple(f"H{number}" for number in range(1, 200, 2))
        self.assertLessEqual(len(view_slug(hunks=every_other)), 60)
        self.assertNotEqual(view_slug(hunks=every_other), view_slug(hunks=(*every_other, "H300")))


class ViewFileCollisionTests(RepoTestCase):
    NAMES = ("a/b.py", "a_b.py", "a-b.py")

    def test_files_whose_names_flatten_alike_get_their_own_view_each(self) -> None:
        for name in self.NAMES:
            self.repo.write(name, f"def only_in_{name.replace('/', '_').replace('-', '_').split('.')[0]}():\n    return 1\n")
        self.review("prepare")

        views = {name: Path(self.review("view", "--files", name).stdout.strip()) for name in self.NAMES}

        self.assertEqual(len(set(views.values())), 3)
        for name, path in views.items():
            hunks = path.read_text(encoding="utf-8").split("## Hunks")[1].split("### Other hunks")[0]
            self.assertIn(f"`{name}`", hunks)
            self.assertEqual(hunks.count("`.py` L"), 0)
            for other in set(self.NAMES) - {name}:
                self.assertNotIn(f"### H1 · `{other}`", hunks)
                self.assertNotIn(f"· `{other}` L", hunks)


class ViewCollisionTests(RepoTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.repo.write("mod.py", "".join(f"value_{index} = {index}\n" for index in range(40)))
        self.repo.commit("add mod")
        text = (self.repo.root / "mod.py").read_text(encoding="utf-8")
        for index in (2, 18, 34):
            text = text.replace(f"value_{index} = {index}", f"value_{index} = -{index}")
        self.repo.write("mod.py", text)
        self.review("prepare")

    def view(self, spec: str) -> tuple[Path, str]:
        path = Path(self.review("view", "--hunks", spec).stdout.strip())
        return path, path.read_text(encoding="utf-8")

    def test_two_spellings_of_different_hunk_sets_write_two_views(self) -> None:
        listed, listed_text = self.view("H1,H3")
        ranged, ranged_text = self.view("H1-H3")

        self.assertNotEqual(listed, ranged)
        self.assertEqual(listed_text, listed.read_text(encoding="utf-8"), "the second view left it alone")
        self.assertNotIn("### H2 ·", listed_text)
        self.assertIn("### H2 ·", ranged_text)

    def test_the_same_set_spelled_two_ways_shares_one_view(self) -> None:
        self.assertEqual(self.view("H3,H1")[0], self.view("H1,H3")[0])

    def test_a_view_is_written_through_an_atomic_replace(self) -> None:
        with mock.patch("craftsman.review.write_text_atomic") as write:
            result = run_main(cli_main, "review", "view", "--hunks", "H1", "--cwd", str(self.repo.root))

        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(write.call_count, 1)
        self.assertEqual(write.call_args.args[0].name, "packet-1.H1.md")


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

    def prepare_multi_hunk(self, count: int) -> None:
        for index in range(1, count + 1):
            self.repo.write(f"mod_{index}.py", f"def fn_{index}(x):\n    return x + {index}\n")
        res = self.cli_fast("review", "prepare", "--task", "Multi-hunk change")
        self.assertEqual(res.code, 0, res.err)


class BatchRejectionTests(FastCliTestCase):
    def test_batch_reports_three_bad_entries_in_one_pass(self) -> None:
        self.prepare_multi_hunk(14)
        entries: list[dict[str, object]] = [
            {"hunks": [f"H{index}"], "note": coverage_note([f"H{index}"])} for index in range(1, 15)
        ]
        entries[3] = {"hunks": ["H4"], "note": "H4 ok"}
        entries[7] = {"hunks": ["H99"], "note": coverage_note(["H8"])}
        entries[11] = {"files": ["nope.py"], "note": coverage_note(["H12"])}

        res = self.cli_fast(
            "review", "cover", "--from-json", "-", stdin_bytes=json.dumps(entries).encode()
        )

        self.assertEqual(res.code, 1)
        self.assertNotIn("error:", res.err)
        self.assertIn(
            "craftsman review cover: 3 of 14 entries rejected; nothing was recorded.", res.err
        )
        self.assertIn("entry 4 (H4): note: must have at least 20 characters", res.err)
        self.assertIn("entry 8 (H99): hunks: unknown hunk 'H99'", res.err)
        self.assertIn("entry 12 (nope.py): files: unknown files in round 1: nope.py", res.err)
        self.assertEqual(self.repo.show()["coverage"]["covered"], 0)
        entries[3] = {"hunks": ["H4"], "note": coverage_note(["H4"])}
        entries[7] = {"hunks": ["H8"], "note": coverage_note(["H8"])}
        entries[11] = {"hunks": ["H12"], "note": coverage_note(["H12"])}
        fixed = self.cli_fast(
            "review", "cover", "--from-json", "-", stdin_bytes=json.dumps(entries).encode()
        )
        self.assertEqual(fixed.code, 0, fixed.err)

    def test_stale_or_missing_round_reports_one_state_error(self) -> None:
        entries = [
            {"hunks": ["H1"], "note": coverage_note(["H1"])},
            {"files": ["cart.py"], "note": coverage_note(["H1"])},
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

    def test_batch_rejection_caps_output_at_twenty_items(self) -> None:
        self.prepare_bug()
        entries = [{"hunks": ["H1"], "note": "H1 ok"} for _ in range(23)]

        res = self.cli_fast(
            "review", "cover", "--from-json", "-", stdin_bytes=json.dumps(entries).encode()
        )

        self.assertEqual(res.code, 1)
        self.assertIn("23 of 23 entries rejected", res.err)
        self.assertIn("entry 20 (H1):", res.err)
        self.assertNotIn("entry 21 (H1):", res.err)
        self.assertIn("  ... and 3 more", res.err)


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

    def _payload(self) -> str:
        return json.dumps(
            [{"hunks": ["H1"], "note": f"{coverage_note(['H1'])} — Größe & naïve"}],
            ensure_ascii=False,
        )

    def test_from_json_decodes_bom_inputs_from_file_or_stdin(self) -> None:
        for codec in ("utf-8", "utf-8-sig", "utf-16", "utf-16-le", "utf-16-be"):
            with self.subTest(codec=codec):
                self.prepare_bug()
                target_codec = "utf-16" if codec == "utf-16-le" else codec
                file_bytes = self._payload().encode(target_codec)
                if codec == "utf-16-be":
                    file_bytes = b"\xfe\xff" + self._payload().encode("utf-16-be")
                json_file = self.tmp / f"batch-{codec}.json"
                json_file.write_bytes(file_bytes)
                res = self.cli_fast("review", "cover", "--from-json", str(json_file))
                self.assertEqual(res.code, 0, res.err)

                stdin_bytes = self._payload().encode(target_codec)
                if codec == "utf-16-be":
                    stdin_bytes = b"\xfe\xff" + self._payload().encode("utf-16-be")
                piped = self._pipe_subproc(stdin_bytes)
                piped_err = piped.stderr.decode("utf-8", errors="replace")
                self.assertEqual(piped.returncode, 0, piped_err)
                self.cli_fast("review", "reset")

    def test_from_json_rejects_cp1250_with_utf8_save_hint(self) -> None:
        self.prepare_bug()
        cp1250_bytes = json.dumps(
            [{"hunks": ["H1"], "note": f"{coverage_note(['H1'])} Größe"}],
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

        first.write_text(
            json.dumps([{"hunks": ["H1"], "note": "H1 weak"}]),
            encoding="utf-8",
        )
        rejected = self.cli_fast("review", "cover", "--from-json", str(first))
        self.assertEqual(rejected.code, 1)
        self.assertTrue(first.is_file())

        first.write_text(
            json.dumps([{"hunks": ["H1"], "note": coverage_note(["H1"])}]),
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
            json.dumps([{"hunks": ["H1"], "note": coverage_note(["H1"])}]),
            encoding="utf-8",
        )
        self.assertEqual(self.cli_fast("review", "cover", "--from-json", str(outside)).code, 0)
        self.assertTrue(outside.is_file())

        target = self.tmp / "symlink-target.json"
        target.write_text(
            json.dumps([{"hunks": ["H1"], "note": coverage_note(["H1"])}]),
            encoding="utf-8",
        )
        inbox_dir = self.repo.ledger_dir / "inbox"
        inbox_dir.mkdir(parents=True, exist_ok=True)
        link = inbox_dir / "cover-link.json"
        link.symlink_to(target)
        self.assertEqual(self.cli_fast("review", "cover", "--from-json", str(link)).code, 0)
        self.assertTrue(link.is_symlink())
        self.assertTrue(target.is_file())

    @staticmethod
    def _locked_unlink(locked_inbox: Path) -> Callable[..., None]:
        real_unlink = Path.unlink

        def fail_unlink(path_obj: Path, missing_ok: bool = False) -> None:
            if path_obj.resolve() == locked_inbox.resolve():
                raise PermissionError("locked by antivirus")
            real_unlink(path_obj, missing_ok=missing_ok)

        return fail_unlink

    def test_inbox_unlink_oserror_warns_without_failing(self) -> None:
        self.prepare_bug()
        inbox_file = self._new_inbox_path("cover")
        inbox_file.write_text(
            json.dumps([{"hunks": ["H1"], "note": coverage_note(["H1"])}]),
            encoding="utf-8",
        )

        with mock.patch.object(Path, "unlink", self._locked_unlink(inbox_file)):
            res = self.cli_fast("review", "cover", "--from-json", str(inbox_file))

        self.assertEqual(res.code, 0)
        self.assertIn("warning: could not remove inbox file", res.err)
        self.assertEqual(self.repo.show()["coverage"]["covered"], 1)

    def test_locked_inbox_file_warns_without_failing_lifecycle_commands(self) -> None:
        self.repo.write("cart.py", BUGGY_TOTAL)
        self.assertEqual(self.cli_fast("review", "prepare").code, 0)
        locked_inbox = self._new_inbox_path("cover")
        self.repo.write("cart.py", BUGGY_TOTAL + "# round 2\n")

        with mock.patch.object(Path, "unlink", self._locked_unlink(locked_inbox)):
            prep = self.cli_fast("review", "prepare")
            report = self.cli_fast("review", "report", "--review-only")
            reset = self.cli_fast("review", "reset")
            self.assertEqual(self.cli_fast("gate", "--init").code, 0)
            self.assertEqual(self.cli_fast("review", "prepare").code, 0)
            note = coverage_note(["H1"])
            cover = self.cli_fast("review", "cover", "--hunks", "H1", "--note", note)
            self.assertEqual(cover.code, 0, cover.err)
            self.run_tests()
            complete = self.cli_fast("gate", "--complete")

        checks = (("prepare", prep), ("report", report), ("reset", reset), ("complete", complete))
        for label, res in checks:
            with self.subTest(command=label):
                self.assertEqual(res.code, 0, res.err)
                self.assertIn("warning: could not remove inbox file", res.err)
        self.assertTrue(locked_inbox.is_file())

    def test_inbox_pruned_by_new_round_reset_or_review_only(self) -> None:
        self.prepare_bug()
        orphan = self._new_inbox_path("cover")
        same_round = self.cli_fast("review", "prepare")
        self.assertEqual(same_round.code, 0)
        self.assertTrue(orphan.is_file())

        self.repo.write("cart.py", BUGGY_TOTAL + "# next round\n")
        next_round = self.cli_fast("review", "prepare")
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
        self.assertEqual(self.cli_fast("gate", "--init").code, 0)
        self.assertEqual(self.cli_fast("review", "prepare").code, 0)
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
        prep = run_main(cli_main, "review", "prepare", "--cwd", str(wt))
        self.assertEqual(prep.code, 0, prep.err)

        inbox_file = self._new_inbox_path("cover", cwd=wt)
        policy = ScopePolicy(mode="deny")
        read_args = {"file_path": str(inbox_file)}
        allowed = evaluate_scope("Read", read_args, wt, wt, policy)
        denied = evaluate_scope("Read", read_args, self.repo.root, self.repo.root, policy)
        self.assertEqual(allowed.decision, "allow", allowed.reason)
        self.assertEqual(denied.decision, "deny")

        inbox_file.write_text(
            json.dumps([{"hunks": ["H1"], "note": coverage_note(["H1"])}]),
            encoding="utf-8",
        )
        res = run_main(
            cli_main, "review", "cover", "--from-json", str(inbox_file), "--cwd", str(wt)
        )
        self.assertEqual(res.code, 0, res.err)
        self.assertFalse(inbox_file.exists())

    def test_check_flag_validates_without_recording_or_consuming(self) -> None:
        self.prepare_bug()
        inbox_file = self._new_inbox_path("cover")
        note = coverage_note(["H1"])
        inbox_file.write_text(
            json.dumps([{"hunks": ["H1"], "note": note}]),
            encoding="utf-8",
        )
        cover_json = self.cli_fast("review", "cover", "--check", "--from-json", str(inbox_file))
        self.assertEqual(cover_json.code, 0)
        cover_cli = self.cli_fast(
            "review", "cover", "--check", "--hunks", "H1", "--note", note
        )
        self.assertEqual(cover_cli.code, 0)
        self.assertTrue(inbox_file.is_file())
        self.assertEqual(self.repo.show()["coverage"]["covered"], 0)

        add_inbox = self._new_inbox_path("add")
        add_inbox.write_text(
            json.dumps([{
                "lens": "contracts", "file": "cart.py", "line": 3, "severity": "major",
                "title": "Empty cart divides by zero",
                "scenario": "total([]) evaluates sum([]) / len([]) and raises ZeroDivisionError",
            }]),
            encoding="utf-8",
        )
        add_json = self.cli_fast("review", "add", "--check", "--from-json", str(add_inbox))
        self.assertEqual(add_json.code, 0)
        self.assertTrue(add_inbox.is_file())
        bad_add = self.cli_fast(
            "review", "add", "--check", "--file", "cart.py", "--line", "0", *FINDING_FLAGS
        )
        self.assertEqual(bad_add.code, 1)
        ok_add = self.cli_fast(
            "review", "add", "--check", "--file", "cart.py", "--line", "3", *FINDING_FLAGS
        )
        self.assertEqual(ok_add.code, 0)
        self.assertEqual(self.repo.show()["findings"], [])

        self.add()
        verdict_inbox = self._new_inbox_path("verdict")
        verdict_inbox.write_text(
            json.dumps([{"id": "F1", "as": "probable", "evidence": STATIC_EVIDENCE}]),
            encoding="utf-8",
        )
        verdict_json = self.cli_fast(
            "review", "verdict", "--check", "--from-json", str(verdict_inbox)
        )
        self.assertEqual(verdict_json.code, 0)
        self.assertTrue(verdict_inbox.is_file())
        verdict_cli = self.cli_fast(
            "review", "verdict", "--check", "F1", "--as", "probable", "--evidence", STATIC_EVIDENCE
        )
        self.assertEqual(verdict_cli.code, 0)
        self.assertIsNone(self.repo.show()["findings"][0]["verdict"])


if __name__ == "__main__":
    unittest.main()
