"""Tests for frozen review scope (WP1): scoped snapshots, ledger invariants and CLI flags."""

from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest import mock

from craftsman import hooks
from craftsman.diffscan import (
    DiffScanError,
    resolve_base_sha,
    snapshot,
    staged_paths,
    untracked_paths,
)
from craftsman.ledger import FIXED_SCOPE_ERROR, load, open_round
from craftsman.records import SCHEMA_VERSION, FieldError, Ledger, LedgerError, Round

from tests.support import (
    BASE_SHA,
    EvidenceRepo,
    GitRepo,
    assert_cli_error,
    coverage_note,
    make_hunk,
    make_snap,
)


def _call_hook(event: str, platform: str, payload: dict[str, Any]) -> dict[str, Any]:
    stdin = io.StringIO(json.dumps(payload))
    out, err = io.StringIO(), io.StringIO()
    with mock.patch("sys.stdin", stdin), redirect_stdout(out), redirect_stderr(err):
        code = hooks.main([event, "--platform", platform])
    assert code == 0, err.getvalue()
    parsed = json.loads(out.getvalue())
    assert isinstance(parsed, dict)
    return parsed


class DiffscanScopedSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        (self.tmp / "repo").mkdir(parents=True, exist_ok=True)
        self.repo = GitRepo(self.tmp / "repo")
        self.repo.write("a.py", "a = 1\n")
        self.repo.write("b.py", "b = 1\n")
        self.repo.write("pkg/x.py", "x = 1\n")
        self.repo.write("file[1].py", "v = 1\n")
        self.base_sha = self.repo.commit("init")

    def test_snapshot_none_vs_empty_vs_scoped_paths(self) -> None:
        self.repo.write("a.py", "a = 2\n")
        self.repo.write("b.py", "b = 2\n")
        self.repo.write("pkg/x.py", "x = 2\n")
        self.repo.write("pkg/new.py", "n = 1\n")
        self.repo.write("pkg_extra/other.py", "o = 1\n")
        self.repo.write("file[1].py", "v = 2\n")
        self.repo.write("PLAN.md", "# plan\n")

        full = snapshot(self.repo.root, self.base_sha, None)
        empty = snapshot(self.repo.root, self.base_sha, ())
        just_a = snapshot(self.repo.root, self.base_sha, ("a.py",))
        just_pkg = snapshot(self.repo.root, self.base_sha, ("pkg/",))
        literal_glob = snapshot(self.repo.root, self.base_sha, ("file[1].py",))

        self.assertEqual(
            full.files,
            ("PLAN.md", "a.py", "b.py", "file[1].py", "pkg/new.py", "pkg/x.py", "pkg_extra/other.py"),
        )
        self.assertTrue(empty.is_empty)
        self.assertEqual(empty.hunks, ())
        self.assertEqual(just_a.files, ("a.py",))
        self.assertEqual(just_pkg.files, ("pkg/new.py", "pkg/x.py"))
        self.assertEqual(literal_glob.files, ("file[1].py",))
        self.assertEqual(
            set(untracked_paths(self.repo.root)),
            {"PLAN.md", "pkg/new.py", "pkg_extra/other.py"},
        )

    def test_snapshot_detects_deleted_tracked_file_inside_scope(self) -> None:
        (self.repo.root / "a.py").unlink()
        self.repo.write("PLAN.md", "# plan\n")

        scoped = snapshot(self.repo.root, self.base_sha, ("a.py",))

        self.assertEqual(scoped.files, ("a.py",))
        self.assertEqual(scoped.hunks[0].status, "deleted")

    def test_snapshot_rejects_invalid_filter_paths(self) -> None:
        for invalid in ("", "/a.py", "../a.py", "a/../b.py", "./a.py", "a\\b.py", "C:a.py"):
            with self.subTest(path=invalid):
                with self.assertRaises(DiffScanError):
                    snapshot(self.repo.root, self.base_sha, (invalid,))

    def test_staged_paths_splits_renames_even_with_unborn_head(self) -> None:
        self.repo.write("a.py", "a = 2\n")
        self.repo.git("add", "a.py")
        self.repo.git("mv", "b.py", "b_renamed.py")
        self.repo.write("pkg/x.py", "x = 2\n")
        self.repo.write("PLAN.md", "# plan\n")

        self.assertEqual(staged_paths(self.repo.root, self.base_sha), ("a.py", "b.py", "b_renamed.py"))

        unborn_root = self.tmp / "unborn"
        unborn_root.mkdir()
        unborn = GitRepo(unborn_root)
        unborn.write("first.py", "x = 1\n")
        unborn.git("add", "first.py")
        unborn.write("untracked.py", "y = 2\n")
        empty_base = resolve_base_sha(unborn.root, None)
        self.assertEqual(staged_paths(unborn.root, empty_base), ("first.py",))


class RoundScopeValidationTests(unittest.TestCase):
    def _sample_round(
        self, number: int = 1, scope: tuple[str, ...] | None = ("a.py",)
    ) -> Round:
        _, round_ = open_round(
            Ledger(),
            make_snap(make_hunk("H1", "a.py"), fingerprint=f"fp{number}", base=BASE_SHA),
            "quick",
            ("intent", "craft"),
            f"packet-{number}.md",
            {},
            signals=(),
            scope=scope,
        )
        return replace(round_, number=number)

    def test_schema_errors_distinguish_older_from_newer_ledgers(self) -> None:
        self.assertEqual(SCHEMA_VERSION, 2)
        base_payload = Ledger().to_json()

        with self.assertRaises(LedgerError) as old_ctx:
            Ledger.from_json({**base_payload, "schema": 1})
        self.assertIn(
            "schema 1 is from an older craftsman; run `craftsman review reset` to archive it",
            str(old_ctx.exception),
        )

        with self.assertRaises(LedgerError) as future_ctx:
            Ledger.from_json({**base_payload, "schema": 3})
        self.assertIn(
            "schema 3 is not supported (expected 2); upgrade craftsman",
            str(future_ctx.exception),
        )

    def test_round_scope_roundtrips_json_with_path_validation(self) -> None:
        scoped = self._sample_round(scope=("a.py", "pkg/b.py"))
        unscoped = self._sample_round(scope=None)

        self.assertEqual(Round.from_json(scoped.to_json()).scope, ("a.py", "pkg/b.py"))
        self.assertIsNone(Round.from_json(unscoped.to_json()).scope)

        bad_scopes: tuple[tuple[str, ...], ...] = (
            (),
            ("",),
            ("/abs.py",),
            ("pkg/",),
            ("./a.py",),
            ("../a.py",),
            ("pkg/../a.py",),
            ("pkg//a.py",),
            ("pkg\\a.py",),
            ("C:a.py",),
            ("a.py", "a.py"),
            ("b.py", "a.py"),
        )
        for bad in bad_scopes:
            with self.subTest(scope=bad):
                with self.assertRaises(FieldError):
                    replace(scoped, scope=bad)

    def test_ledger_enforces_consistent_scope_across_rounds(self) -> None:
        r1 = self._sample_round(1, ("a.py",))
        r2_same = self._sample_round(2, ("a.py",))
        r2_expanded = self._sample_round(2, ("a.py", "tests/test_a.py"))
        r2_gate = self._sample_round(2, None)
        r3_narrowed_after_gate = self._sample_round(3, ("a.py",))
        r2_diff = self._sample_round(2, ("b.py",))

        self.assertEqual(Ledger(rounds=(r1, r2_same)).rounds[1].scope, ("a.py",))
        self.assertEqual(
            Ledger(rounds=(r1, r2_expanded)).rounds[1].scope, ("a.py", "tests/test_a.py")
        )
        self.assertIsNone(Ledger(rounds=(r1, r2_gate)).rounds[1].scope)
        with self.assertRaisesRegex(
            FieldError, r"rounds\[2\]\.scope cannot narrow a full-worktree review scope"
        ):
            Ledger(rounds=(r1, r2_gate, r3_narrowed_after_gate))
        with self.assertRaisesRegex(
            FieldError,
            r"rounds\[1\]\.scope must include every path from the previous round's scope",
        ):
            Ledger(rounds=(r1, r2_diff))

        ledger1 = Ledger(rounds=(r1,))
        for bad_scope in (("b.py",), ("a.py", "b.py")):
            with self.subTest(bad_scope=bad_scope):
                with self.assertRaisesRegex(LedgerError, "the review scope is fixed"):
                    open_round(
                        ledger1,
                        make_snap(make_hunk("H1", "b.py"), fingerprint="fp2", base=BASE_SHA),
                        "quick",
                        ("intent", "craft"),
                        "packet-2.md",
                        {},
                        signals=(),
                        scope=bad_scope,
                    )
        widened, round2 = open_round(
            ledger1,
            make_snap(
                make_hunk("H1", "a.py"),
                make_hunk("H2", "b.py"),
                fingerprint="fp2",
                base=BASE_SHA,
            ),
            "quick",
            ("intent", "craft"),
            "packet-2.md",
            {},
            signals=(),
            scope=("a.py", "b.py"),
            allow_scope_expansion=True,
        )
        self.assertEqual(round2.scope, ("a.py", "b.py"))
        self.assertEqual(widened.current_scope, ("a.py", "b.py"))


class FrozenReviewScopeCliTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.repo = EvidenceRepo(self.tmp / "repo")

    def review(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return self.repo.cli("review", *args, check=check)

    def cover(self, lens: str, *hunks: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return self.review(
            "cover",
            "--lens",
            lens,
            "--hunks",
            ",".join(hunks),
            "--note",
            coverage_note(list(hunks)),
            check=check,
        )

    def ledger(self) -> Ledger:
        return load(self.repo.ledger_dir / "ledger.json")

    def packet(self, number: int = 1) -> str:
        return (self.repo.ledger_dir / f"packet-{number}.md").read_text(encoding="utf-8")

    def assert_error(
        self, proc: subprocess.CompletedProcess[str], exit_code: int, fragment: str
    ) -> None:
        assert_cli_error(self, proc, exit_code, fragment)

    def test_review_only_freezes_initial_scope_against_new_files(self) -> None:
        self.repo.write("app.py", "x = 2\n")

        prepared = self.review("prepare", "--tier", "quick").stdout

        self.assertEqual(self.ledger().rounds[0].scope, ("app.py",))
        self.assertIn("Scope: 1 file(s), frozen for this review.", prepared)
        self.assertNotIn("Ignored (outside the review scope):", prepared)

        # Create an untracked file after Round 1 is frozen.
        self.repo.write("PLAN.md", "# Scratch plan\n")

        # Round 1 remains current for cover, show, status and subagent hooks.
        self.cover("intent", "H1")
        show_text = self.review("show").stdout
        show_json = self.repo.show()
        self.assertNotIn("PLAN.md", show_text)
        self.assertNotIn("stale", show_text.lower())
        self.assertTrue(show_json["round"]["current"])

        status_json = json.loads(self.repo.cli("status", "--json").stdout)
        self.assertEqual(status_json["diff"]["changed_files"], 1)

        hook_reply = _call_hook(
            "subagent-start",
            "claude",
            {
                "session_id": "s1",
                "transcript_path": str(self.repo.root / "missing.jsonl"),
                "cwd": str(self.repo.root),
                "hook_event_name": "SubagentStart",
                "agent_type": "craftsman-reviewer",
                "agent_id": "rev-1",
            },
        )
        self.assertIn("Craftsman review round 1", json.dumps(hook_reply))
        self.assertEqual(len(self.ledger().agents), 1)

        again = self.review("prepare", "--tier", "quick").stdout
        self.assertIn("Round 1 unchanged", again)
        self.assertIn("Scope: 1 file(s), frozen for this review.", again)
        self.assertIn("Ignored (outside the review scope): PLAN.md", again)

        # More than 5 ignored files truncate cleanly with `(+K more)`.
        for index in range(6):
            self.repo.write(f"extra_{index}.md", f"note {index}\n")
        many = self.review("prepare", "--tier", "quick").stdout
        self.assertIn("(+2 more)", many)

    def test_editing_in_scope_file_opens_next_round_keeping_scope(self) -> None:
        self.repo.write("app.py", "x = 2\n")
        self.review("prepare", "--tier", "quick")
        self.repo.write("PLAN.md", "# Scratch plan\n")
        self.repo.write("app.py", "x = 3\n")

        stale = self.cover("intent", "H1", check=False)
        self.assert_error(stale, 1, "Code changed since round 1")

        round2_out = self.review("prepare", "--tier", "quick").stdout
        self.assertIn("Round 2 prepared (quick)", round2_out)
        self.assertIn("Ignored (outside the review scope): PLAN.md", round2_out)
        round2 = self.ledger().rounds[1]
        self.assertEqual(round2.scope, ("app.py",))
        self.assertEqual([hunk.path for hunk in round2.hunks], ["app.py"])
        self.assertNotIn("PLAN.md", self.packet(2))

    def test_active_gate_keeps_unbounded_worktree_scope(self) -> None:
        self.repo.cli("gate", "--init", "--tier", "quick", "--cwd", ".")
        self.repo.write("app.py", "x = 2\n")

        round1_out = self.review("prepare", "--tier", "quick").stdout
        self.assertIsNone(self.ledger().rounds[0].scope)
        self.assertNotIn("frozen for this review", round1_out)

        self.repo.write("PLAN.md", "# New file during gate task\n")
        stale = self.cover("intent", "H1", check=False)
        self.assert_error(stale, 1, "Code changed since round 1")

        round2_out = self.review("prepare", "--tier", "quick").stdout
        self.assertIn("Round 2 prepared (quick): 2 hunks", round2_out)
        round2 = self.ledger().rounds[1]
        self.assertIsNone(round2.scope)
        self.assertEqual([hunk.path for hunk in round2.hunks], ["PLAN.md", "app.py"])

    def test_staged_flag_freezes_staged_paths_using_worktree_lines(self) -> None:
        self.repo.write("a.py", "a = 1\n")
        self.repo.write("b.py", "b = 1\n")
        self.repo.commit("add a and b")

        nothing_staged = self.review("prepare", "--tier", "quick", "--staged", check=False)
        self.assert_error(nothing_staged, 1, "nothing staged against base")

        self.repo.write("a.py", "a = 2\n")
        self.repo.git("add", "a.py")
        # Further unstaged edit to the staged file is read from disk so line citations match.
        self.repo.write("a.py", "a = 2\na_worktree = 3\n")
        self.repo.write("b.py", "b = 2\n")
        self.repo.write("c.py", "c = 1\n")

        out = self.review("prepare", "--tier", "quick", "--staged").stdout

        self.assertEqual(self.ledger().rounds[0].scope, ("a.py",))
        self.assertIn("Scope: 1 file(s), frozen for this review.", out)
        self.assertIn("Ignored (outside the review scope): b.py, c.py", out)
        self.assertIn("+a_worktree = 3", self.packet(1))
        self.assertNotIn("b.py", self.packet(1))
        self.assertNotIn("c.py", self.packet(1))

    def test_tracked_flag_omits_existing_untracked_files(self) -> None:
        self.repo.write("app.py", "x = 2\n")
        self.repo.write("PLAN.md", "# Pre-existing scratch plan\n")

        out = self.review("prepare", "--tier", "quick", "--tracked").stdout

        self.assertEqual(self.ledger().rounds[0].scope, ("app.py",))
        self.assertIn("Scope: 1 file(s), frozen for this review.", out)
        self.assertIn("Ignored (outside the review scope): PLAN.md", out)
        self.assertNotIn("PLAN.md", self.packet(1))

    def test_later_round_rejects_changed_scope_selector(self) -> None:
        self.repo.write("a.py", "a = 1\n")
        self.repo.write("b.py", "b = 1\n")
        self.repo.commit("add a and b")

        self.repo.write("a.py", "a = 2\n")

        # Mutually exclusive flags and unmatched paths in Round 1.
        both = self.review("prepare", "--paths", "a.py", "--staged", check=False)
        self.assert_error(both, 1, "--paths, --staged are mutually exclusive")
        unmatched = self.review("prepare", "--paths", "b.py", check=False)
        self.assert_error(unmatched, 1, "--paths 'b.py' matched no changed files")

        self.review("prepare", "--tier", "quick", "--paths", "a.py")
        self.assertEqual(self.ledger().rounds[0].scope, ("a.py",))

        # Requesting a different `--paths` in Round 2 fails whether `b.py` is unchanged or changed.
        diff_unchanged = self.review("prepare", "--tier", "quick", "--paths", "b.py", check=False)
        self.assert_error(diff_unchanged, 1, FIXED_SCOPE_ERROR)

        self.repo.write("b.py", "b = 2\n")
        diff_changed = self.review("prepare", "--tier", "quick", "--paths", "b.py", check=False)
        self.assert_error(diff_changed, 1, FIXED_SCOPE_ERROR)

        # Passing the same `--paths a.py` succeeds and opens Round 2 when `a.py` changes.
        self.repo.write("a.py", "a = 3\n")
        same_scope = self.review("prepare", "--tier", "quick", "--paths", "a.py").stdout
        self.assertIn("Round 2 prepared (quick)", same_scope)
        self.assertEqual(self.ledger().rounds[1].scope, ("a.py",))

    def test_cli_rejects_older_schema_ledger_with_reset_hint(self) -> None:
        self.repo.ledger_dir.mkdir(parents=True, exist_ok=True)
        (self.repo.ledger_dir / "ledger.json").write_text(
            json.dumps({**Ledger().to_json(), "schema": 1}),
            encoding="utf-8",
        )

        proc = self.review("show", check=False)

        self.assert_error(
            proc,
            1,
            "schema 1 is from an older craftsman; run `craftsman review reset` to archive it",
        )

    def test_scope_flags_are_rejected_during_active_gate(self) -> None:
        self.repo.cli("gate", "--init", "--tier", "quick", "--cwd", ".")
        self.repo.write("app.py", "x = 2\n")

        for flags in (
            ("--paths", "app.py"),
            ("--add-paths", "app.py"),
            ("--staged",),
            ("--tracked",),
        ):
            with self.subTest(flags=flags):
                proc = self.review("prepare", "--tier", "quick", *flags, check=False)
                self.assert_error(
                    proc,
                    1,
                    f"{flags[0]} cannot be used during an active /craftsman gate session",
                )

    def test_renamed_file_freezes_both_paths_keeping_round_current(self) -> None:
        self.repo.write(
            "old.py",
            "line_1 = 1\nline_2 = 2\nline_3 = 3\nline_4 = 4\nline_5 = 5\n",
        )
        self.repo.commit("add old.py")

        self.repo.git("mv", "old.py", "new.py")
        self.repo.write(
            "new.py",
            "line_1 = 1\nline_2 = 2\nline_3 = 3\nline_4 = 4\nline_5 = 5\nline_6 = 6\n",
        )

        out = self.review("prepare", "--tier", "quick").stdout

        round0 = self.ledger().rounds[0]
        self.assertEqual(round0.scope, ("new.py", "old.py"))
        self.assertIn("Scope: 1 file(s), frozen for this review.", out)
        scoped_snap = snapshot(self.repo.root, round0.base_sha, round0.scope)
        self.assertEqual(len(scoped_snap.hunks), 1)
        self.assertEqual(scoped_snap.hunks[0].status, "renamed")
        self.assertEqual(scoped_snap.hunks[0].old_path, "old.py")
        self.assertEqual(scoped_snap.hunks[0].path, "new.py")

        # Subsequent commands compute the identical rename snapshot and do not see a stale round.
        self.cover("intent", "H1")

    def test_directory_paths_freeze_matched_files_only(self) -> None:
        self.repo.write("src/a/one.py", "one = 1\n")
        self.repo.write("src/b/x.py", "x = 1\n")
        self.repo.commit("add src tree")

        self.repo.write("src/a/one.py", "one = 2\n")
        self.repo.write("src/b/x.py", "x = 2\n")

        out = self.review("prepare", "--tier", "quick", "--paths", "src/b/").stdout

        self.assertEqual(self.ledger().rounds[0].scope, ("src/b/x.py",))
        self.assertIn("Scope: 1 file(s), frozen for this review.", out)
        self.assertIn("Ignored (outside the review scope): src/a/one.py", out)

        # A new untracked file inside `src/b/` is NOT included because concrete paths were frozen.
        self.repo.write("src/b/PLAN.md", "# Local dir notes\n")
        self.cover("intent", "H1")

        again = self.review("prepare", "--tier", "quick").stdout
        self.assertIn("Round 1 unchanged", again)
        self.assertIn("Ignored (outside the review scope): src/a/one.py, src/b/PLAN.md", again)

    def test_recorded_run_stays_current_across_out_of_scope_writes(self) -> None:
        self.repo.write("app.py", "x = 2\n")
        self.review("prepare", "--tier", "quick")
        self.repo.write("PLAN.md", "# Out-of-scope plan\n")

        run_proc = self.repo.cli(
            "run",
            "--kind",
            "test",
            "--",
            sys.executable,
            "-c",
            "from pathlib import Path; Path('NOTES.md').write_text('scratch\\n', encoding='utf-8')",
        )
        self.assertEqual(run_proc.returncode, 0, run_proc.stderr)

        ledger = self.ledger()
        round0 = ledger.rounds[0]
        (recorded_run,) = ledger.runs
        self.assertEqual(recorded_run.fingerprint, round0.fingerprint)
        self.assertIsNone(recorded_run.fingerprint_after)

        show_text = self.review("show").stdout
        self.assertIn("- R1 test exit 0", show_text)
        self.assertIn("· current ·", show_text)
        show_json = self.repo.show()
        self.assertEqual(len(show_json["runs"]), 1)
        self.assertEqual(show_json["runs"][0]["fingerprint"], round0.fingerprint)
        self.assertIsNone(show_json["runs"][0]["fingerprint_after"])

        self.repo.cover_all()
        self.assertEqual(self.review("check", "--review-only").returncode, 0)
        self.assertEqual(self.review("check").returncode, 0)

    def test_add_paths_extends_scope_for_fix_files_and_persists_across_later_rounds(self) -> None:
        self.repo.write("calc.py", "def add(a, b):\n    return a - b\n")

        first_round_err = self.review("prepare", "--add-paths", "calc.py", check=False)
        self.assert_error(first_round_err, 1, "--add-paths extends an existing review scope")

        self.review("prepare", "--tier", "quick")
        self.assertEqual(self.ledger().current_scope, ("calc.py",))
        self.repo.cover_all()

        # Unrelated file from another session plus a new regression test file created during --fix.
        self.repo.write("PLAN.md", "# Unrelated session plan\n")
        self.repo.write("calc.py", "def add(a, b):\n    return a + b\n")
        self.repo.write("tests/test_calc.py", "from calc import add\nassert add(2, 3) == 5\n")

        unmatched = self.review("prepare", "--add-paths", "missing.py", check=False)
        self.assert_error(unmatched, 1, "--add-paths 'missing.py' matched no changed files")

        r2_out = self.review("prepare", "--tier", "quick", "--add-paths", "tests/").stdout
        self.assertIn("Round 2 prepared (quick): 2 hunks", r2_out)
        self.assertIn("Scope: 2 file(s), frozen for this review.", r2_out)
        self.assertIn("Ignored (outside the review scope): PLAN.md", r2_out)
        self.assertEqual(self.ledger().current_scope, ("calc.py", "tests/test_calc.py"))

        # Check must block until the newly added test file is also covered (Rule 5).
        self.assertEqual(self.review("check", "--review-only", check=False).returncode, 1)
        self.repo.cover_all()
        self.assertEqual(self.review("check", "--review-only").returncode, 0)

        # Round 3 without flags inherits the expanded `current_scope`, not Round 1's narrow scope.
        self.repo.write(
            "tests/test_calc.py",
            "from calc import add\nassert add(2, 3) == 5\nassert add(-1, 1) == 0\n",
        )
        r3_out = self.review("prepare", "--tier", "quick").stdout
        self.assertIn("Round 3 prepared (quick): 2 hunks, 1 carried, 1 to review.", r3_out)
        self.assertEqual(self.ledger().rounds[2].scope, ("calc.py", "tests/test_calc.py"))

    def test_completed_review_with_out_of_scope_edits_hints_reset(self) -> None:
        self.repo.write("app.py", "x = 2\n")
        self.review("prepare", "--tier", "quick")
        self.repo.write("next_day.py", "y = 1\n")

        # While Round 1 is still incomplete, `prepare` must NOT suggest `review reset`.
        in_progress = self.review("prepare", "--tier", "quick").stdout
        self.assertIn("Ignored (outside the review scope): next_day.py", in_progress)
        self.assertNotIn("already complete", in_progress)

        # Once Round 1 is complete, `prepare` explicitly hints to run `craftsman review reset`.
        self.repo.cover_all()
        completed_out = self.review("prepare", "--tier", "quick").stdout
        self.assertIn("Ignored (outside the review scope): next_day.py", completed_out)
        self.assertIn(
            "Hint: round 1 is already complete; run `craftsman review reset`", completed_out
        )

        # If the previously reviewed file is reverted to HEAD while new files are modified,
        # `prepare` explains that the frozen scope is empty and points to `review reset`.
        self.repo.git("checkout", "--", "app.py")
        empty_scoped = self.review("prepare", "--tier", "quick").stdout
        self.assertIn("Nothing to review within the frozen scope", empty_scoped)
        self.assertIn("changed files outside scope: next_day.py", empty_scoped)
        self.assertIn("craftsman review reset", empty_scoped)


if __name__ == "__main__":
    unittest.main()
