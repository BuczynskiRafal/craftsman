"""Tests for the hook error log and the frame every hook run is wrapped in (`craftsman.hook_errors`)."""

from __future__ import annotations

import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from craftsman import hook_errors
from craftsman.diffscan import GitTimeoutError, git_time_left, worktree_root
from craftsman.hook_budget import HOOK_TIMEOUTS_S, git_budget_s
from craftsman.ledger import ledger_path_for, load, mutate
from craftsman.records import Ledger, format_timestamp

from tests.support import EvidenceRepo

NOW = datetime(2026, 11, 8, 12, 0, 0, tzinfo=timezone.utc)


class LogTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name) / "craftsman"

    def test_an_entry_is_one_json_line_with_its_time_source_and_kind(self) -> None:
        self.assertTrue(hook_errors.record(self.dir, "crash", "boom", source="pre-tool", now=NOW))

        (line,) = hook_errors.log_path(self.dir).read_text(encoding="utf-8").splitlines()
        self.assertEqual(
            json.loads(line), {"at": format_timestamp(NOW), "source": "pre-tool", "kind": "crash", "message": "boom"}
        )

    def test_entries_are_read_back_oldest_first_and_filtered_by_time(self) -> None:
        for minutes, message in enumerate(("a", "b", "c")):
            hook_errors.record(self.dir, "ledger", message, source="stop", now=NOW + timedelta(minutes=minutes))

        self.assertEqual([entry.message for entry in hook_errors.read(self.dir)], ["a", "b", "c"])
        self.assertEqual([e.message for e in hook_errors.since(self.dir, NOW + timedelta(minutes=1))], ["b", "c"])

    def test_a_message_is_one_bounded_line(self) -> None:
        hook_errors.record(self.dir, "crash", "first\nsecond\n" + "x" * 5000, now=NOW)

        (entry,) = hook_errors.read(self.dir)
        self.assertNotIn("\n", entry.message)
        self.assertEqual(len(entry.message), hook_errors.MAX_MESSAGE_CHARS)

    def test_the_log_rotates_at_256_kb_and_keeps_the_older_entries_readable(self) -> None:
        log = hook_errors.log_path(self.dir)
        hook_errors.record(self.dir, "crash", "old", now=NOW)
        with log.open("ab") as handle:
            handle.write(b" " * hook_errors.MAX_LOG_BYTES)

        hook_errors.record(self.dir, "crash", "new", now=NOW + timedelta(seconds=1))

        rotated = self.dir / hook_errors.ROTATED_NAME
        self.assertTrue(rotated.is_file())
        self.assertLess(log.stat().st_size, 1000)
        self.assertEqual([entry.message for entry in hook_errors.read(self.dir)], ["old", "new"])

    def test_a_second_rotation_replaces_the_rotated_file(self) -> None:
        for round_ in range(3):
            hook_errors.record(self.dir, "crash", f"m{round_}", now=NOW + timedelta(seconds=round_))
            with hook_errors.log_path(self.dir).open("ab") as handle:
                handle.write(b" " * hook_errors.MAX_LOG_BYTES)

        total = sum(path.stat().st_size for path in self.dir.iterdir())
        self.assertLess(total, 2 * hook_errors.MAX_LOG_BYTES + 4096)  # bounded: current + one rotated file

    def test_lines_that_are_not_entries_are_skipped(self) -> None:
        self.dir.mkdir()
        hook_errors.log_path(self.dir).write_text(
            'not json\n[1]\n{"at": "nope", "source": "", "kind": "crash", "message": "x"}\n'
            '{"at": "2026-11-08T12:00:00.000Z", "source": "", "kind": "mystery", "message": "x"}\n',
            encoding="utf-8",
        )

        self.assertEqual(hook_errors.read(self.dir), ())

    def test_a_log_that_cannot_be_written_is_reported_on_stderr_and_never_raises(self) -> None:
        self.dir.parent.mkdir(exist_ok=True)
        self.dir.write_text("a file where the folder should be", encoding="utf-8")
        err = io.StringIO()

        with redirect_stderr(err):
            written = hook_errors.record(self.dir, "ledger", "the reason", source="stop")

        self.assertFalse(written)
        self.assertIn("the reason", err.getvalue())

    def test_a_symlinked_craftsman_folder_is_not_written_through(self) -> None:
        target = self.dir.parent / "elsewhere"
        target.mkdir()
        self.dir.symlink_to(target)

        with redirect_stderr(io.StringIO()):
            self.assertFalse(hook_errors.record(self.dir, "crash", "x"))

        self.assertEqual(list(target.iterdir()), [])


class SummaryTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = EvidenceRepo(Path(tmp.name).resolve() / "repo")
        self.repo.write("cart.py", "def total(items):\n    return sum(items)\n")
        self.repo.cli("review", "prepare")
        self.ledger_path = self.repo.ledger_dir / "ledger.json"

    def ledger(self) -> Ledger:
        return load(self.ledger_path)

    def test_nothing_to_say_without_entries(self) -> None:
        self.assertIsNone(hook_errors.summary_for(self.ledger_path, self.ledger()))
        self.assertIsNone(hook_errors.summary_json(self.ledger_path, self.ledger()))

    def test_entries_after_the_round_was_prepared_are_counted_and_the_log_is_named(self) -> None:
        hook_errors.record(self.repo.ledger_dir, "crash", "one")
        hook_errors.record(self.repo.ledger_dir, "git-timeout", "two")

        line = hook_errors.summary_for(self.ledger_path, self.ledger())

        self.assertEqual(
            line, f"Hook errors since round 1 was prepared: 2 (see {self.repo.ledger_dir / 'hook-errors.log'})"
        )
        self.assertEqual(
            hook_errors.summary_json(self.ledger_path, self.ledger()),
            {"count": 2, "since_round": 1, "path": str(self.repo.ledger_dir / "hook-errors.log")},
        )

    def test_entries_older_than_the_round_do_not_count(self) -> None:
        hook_errors.record(self.repo.ledger_dir, "crash", "before", now=NOW - timedelta(days=400))

        self.assertIsNone(hook_errors.summary_for(self.ledger_path, self.ledger()))

    def test_without_a_round_there_is_nothing_to_compare_with(self) -> None:
        hook_errors.record(self.repo.ledger_dir, "crash", "x")

        self.assertIsNone(hook_errors.summary_line(self.repo.ledger_dir, Ledger()))

    def test_the_summary_of_a_directory_survives_an_unreadable_ledger(self) -> None:
        self.ledger_path.write_text("{", encoding="utf-8")

        self.assertIsNone(hook_errors.summary_of(self.repo.ledger_dir))

    def test_review_check_and_report_print_the_line_and_json_carries_it(self) -> None:
        hook_errors.record(self.repo.ledger_dir, "crash", "boom")
        expected = "Hook errors since round 1 was prepared: 1 (see "

        for command in (("review", "check"), ("review", "report")):
            with self.subTest(command=command):
                out = self.repo.cli(*command, check=False).stdout
                self.assertIn(expected, out)
                payload = json.loads(self.repo.cli(*command, "--json", check=False).stdout)
                self.assertEqual(payload["hook_errors"]["count"], 1)

    def test_a_clean_review_prints_no_line_and_no_json_key(self) -> None:
        self.assertNotIn("Hook errors", self.repo.cli("review", "check", check=False).stdout)
        self.assertNotIn("hook_errors", json.loads(self.repo.cli("review", "check", "--json", check=False).stdout))

    def test_status_shows_the_count_and_the_gate_complete_failure_names_the_log(self) -> None:
        self.repo.cli("gate", "--init", "--cwd", ".")
        hook_errors.record(self.repo.ledger_dir, "crash", "boom")

        status = self.repo.cli("status", "--json").stdout
        refused = self.repo.cli("gate", "--complete", check=False)

        self.assertEqual(json.loads(status)["hook_errors"]["count"], 1)
        self.assertIn("Hook errors since round 1 was prepared: 1", refused.stderr)


class CraftsmanDirTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.repo = EvidenceRepo(self.tmp / "repo")

    def test_the_directory_is_found_without_running_git(self) -> None:
        sub = self.repo.root / "pkg" / "deep"
        sub.mkdir(parents=True)

        with mock.patch("subprocess.run", side_effect=AssertionError("git was run")):
            found = hook_errors.craftsman_dir_for(sub)

        self.assertEqual(found, self.repo.ledger_dir)
        self.assertEqual(found, ledger_path_for(sub).parent)

    def test_a_linked_work_tree_uses_its_own_git_dir(self) -> None:
        linked = self.tmp / "linked"
        subprocess.run(["git", "worktree", "add", "-q", str(linked), "-b", "side"], cwd=self.repo.root, check=True)

        found = hook_errors.craftsman_dir_for(linked)

        self.assertEqual(found, ledger_path_for(linked).parent)
        self.assertIn("worktrees", found.parts if found else ())

    def test_outside_a_checkout_there_is_none(self) -> None:
        self.assertIsNone(hook_errors.craftsman_dir_for(self.tmp / "nowhere"))


class HookRunTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = EvidenceRepo(Path(tmp.name).resolve() / "repo")

    def test_report_goes_to_the_checkout_of_the_running_hook(self) -> None:
        with hook_errors.hook_run("stop", self.repo.root):
            hook_errors.report("ledger", "cannot read it")

        (entry,) = hook_errors.read(self.repo.ledger_dir)
        self.assertEqual((entry.source, entry.kind, entry.message), ("stop", "ledger", "cannot read it"))

    def test_report_outside_a_hook_run_uses_the_given_directory_or_stderr(self) -> None:
        hook_errors.report("crash", "somewhere", self.repo.ledger_dir)
        err = io.StringIO()
        with redirect_stderr(err):
            hook_errors.report("crash", "nowhere")

        self.assertEqual([entry.message for entry in hook_errors.read(self.repo.ledger_dir)], ["somewhere"])
        self.assertIn("nowhere", err.getvalue())

    def test_a_git_call_inside_the_run_shares_the_events_budget(self) -> None:
        with hook_errors.hook_run("stop", self.repo.root):
            self.assertAlmostEqual(git_time_left(), git_budget_s("stop"), delta=1)
        with hook_errors.hook_run("pre-tool", self.repo.root):
            self.assertAlmostEqual(git_time_left(), git_budget_s("pre-tool"), delta=1)

    def test_the_budget_is_below_every_hook_timeout(self) -> None:
        for event, timeout in HOOK_TIMEOUTS_S.items():
            with self.subTest(event=event):
                self.assertLess(git_budget_s(event), timeout)
                self.assertGreater(git_budget_s(event), 0)

    def test_a_timed_out_git_call_is_logged_once_even_when_its_caller_swallows_the_error(self) -> None:
        expired = subprocess.TimeoutExpired(["git"], 1)

        with hook_errors.hook_run("stop", self.repo.root):
            with mock.patch("craftsman.diffscan.subprocess.run", side_effect=expired):
                for _ in range(3):
                    with self.assertRaises(GitTimeoutError):
                        worktree_root(self.repo.root)

        entries = hook_errors.read(self.repo.ledger_dir)
        self.assertEqual([entry.kind for entry in entries], ["git-timeout"])
        self.assertIn("timed out", entries[0].message)

    def test_a_spent_budget_stops_the_next_git_call_before_it_runs(self) -> None:
        with hook_errors.hook_run("stop", self.repo.root):
            with mock.patch("craftsman.diffscan.time.monotonic", return_value=10**9):
                with mock.patch("craftsman.diffscan.subprocess.run", side_effect=AssertionError("git was run")):
                    with self.assertRaisesRegex(GitTimeoutError, "budget is spent"):
                        worktree_root(self.repo.root)

        self.assertEqual([entry.kind for entry in hook_errors.read(self.repo.ledger_dir)], ["git-timeout"])

    def test_an_older_ledger_set_aside_inside_the_run_is_logged(self) -> None:
        ledger_path = self.repo.ledger_dir / "ledger.json"
        self.repo.ledger_dir.mkdir(exist_ok=True)
        ledger_path.write_text(json.dumps({"schema": 2, "rounds": []}), encoding="utf-8")

        with hook_errors.hook_run("pre-tool", self.repo.root):
            with redirect_stderr(io.StringIO()):
                mutate(ledger_path, lambda ledger: (ledger, None))

        (entry,) = hook_errors.read(self.repo.ledger_dir)
        self.assertEqual(entry.kind, "ledger")
        self.assertIn("set aside", entry.message)
        self.assertIn(".bak", entry.message)

    def test_kinds_follow_the_error_type(self) -> None:
        from craftsman.diffscan import DiffScanError
        from craftsman.gate import GateStateError
        from craftsman.records import LedgerError

        self.assertEqual(hook_errors.kind_of(GitTimeoutError("x")), "git-timeout")
        self.assertEqual(hook_errors.kind_of(DiffScanError("x")), "git")
        self.assertEqual(hook_errors.kind_of(GateStateError("x")), "state")
        self.assertEqual(hook_errors.kind_of(LedgerError("x")), "ledger")
        self.assertEqual(hook_errors.kind_of(RuntimeError("x")), "crash")


if __name__ == "__main__":
    unittest.main()
