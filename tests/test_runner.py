"""Tests for `craftsman run`: running a check command and recording it in the evidence ledger."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from craftsman import runner
from craftsman.gatecheck import passing_runs
from craftsman.ledger import load
from craftsman.records import OUTPUT_TAIL_CHARS

from tests.support import REPO_ROOT, EvidenceRepo, assert_cli_error


class RunTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.repo = EvidenceRepo(self.tmp / "repo")

    def runs(self) -> list[dict[str, object]]:
        runs = self.repo.show()["runs"]
        assert isinstance(runs, list)
        return runs

    def run_check(self, script: str) -> subprocess.CompletedProcess[str]:
        return self.repo.cli("run", "--kind", "test", "--", sys.executable, "-c", script, check=False)

    def test_run_exits_with_the_commands_exit_code(self) -> None:
        self.assertEqual(self.run_check("import sys; sys.exit(3)").returncode, 3)

    def test_run_records_the_commands_exit_code(self) -> None:
        self.run_check("import sys; sys.exit(3)")
        (run,) = self.runs()
        self.assertEqual((run["kind"], run["exit_code"]), ("test", 3))

    def test_run_passes_the_output_through(self) -> None:
        self.assertIn("END", self.run_check("print('END')").stdout)

    def test_output_tail_keeps_the_end_of_long_output(self) -> None:
        self.run_check("print('x' * 5000); print('END')")
        tail = self.runs()[0]["output_tail"]
        assert isinstance(tail, str)
        self.assertEqual(len(tail), OUTPUT_TAIL_CHARS)
        self.assertTrue(tail.rstrip().endswith("END"))

    def test_timeout_is_recorded_with_the_timeout_exit_code(self) -> None:
        proc = self.repo.cli(
            "run", "--timeout", "0.5", "--", sys.executable, "-c", "import time; time.sleep(30)",
            check=False,
        )  # fmt: skip
        self.assertEqual(proc.returncode, runner.EXIT_TIMEOUT)
        self.assertIn("timed out after 0.5s", proc.stderr)
        self.assertEqual(self.runs()[0]["exit_code"], runner.EXIT_TIMEOUT)

    def test_modifying_the_work_tree_is_reported(self) -> None:
        proc = self.repo.cli("run", "--", sys.executable, "-c", "open('made.txt', 'w').write('x')")
        self.assertIn("the command modified the working tree (made.txt)", proc.stderr)
        self.assertIn(".craftsmanignore", proc.stderr)
        self.assertEqual(len(self.runs()), 1)

    def test_run_that_modified_the_tree_records_both_fingerprints(self) -> None:
        self.repo.cli("run", "--", sys.executable, "-c", "open('made.txt', 'w').write('x')")

        (run,) = self.runs()

        self.assertIsNotNone(run["fingerprint_after"])
        self.assertNotEqual(run["fingerprint_after"], run["fingerprint"])

    def test_missing_executable_records_nothing(self) -> None:
        proc = self.repo.cli("run", "--", "definitely-not-a-real-command-xyz", check=False)
        assert_cli_error(
            self, proc, runner.EXIT_NOT_STARTED, "cannot start 'definitely-not-a-real-command-xyz'"
        )
        self.assertEqual(self.runs(), [])

    def test_command_without_separator_is_a_usage_error(self) -> None:
        assert_cli_error(self, self.repo.cli("run", "pytest", check=False), 2, "put `--` between")

    def test_empty_command_is_a_usage_error(self) -> None:
        assert_cli_error(self, self.repo.cli("run", "--", check=False), 2, "missing command after `--`")

    def test_unknown_kind_is_a_usage_error(self) -> None:
        proc = self.repo.cli("run", "--kind", "smoke", "--", "pytest", check=False)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("invalid choice: 'smoke'", proc.stderr)

    def test_trivial_commands_are_flagged(self) -> None:
        proc = self.repo.cli("run", "--kind", "test", "--", "echo", "ok")
        self.assertIn("is trivial and does not count as verification", proc.stderr)

    def run_reporting(self, output: str, *flags: str, kind: str = "test", exit_code: int = 0) -> subprocess.CompletedProcess[str]:
        """Run a command that prints `output` as a test runner would and exits with `exit_code`."""
        script = f"import sys; sys.stdout.write({output!r}); assert True; sys.exit({exit_code})"
        return self.repo.cli("run", "--kind", kind, *flags, "--", sys.executable, "-c", script, check=False)

    def test_a_test_run_records_the_tests_its_runner_reported(self) -> None:
        proc = self.run_reporting("collected 3 items\n\n===== 3 passed in 0.02s =====\n")

        (run,) = self.runs()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual((run["tests_ran"], run["min_tests"]), (3, 1))
        self.assertNotIn("does not count", proc.stderr)
        self.assertTrue(self.passing_ids())

    def test_unrecognized_output_is_recorded_as_unknown_and_still_passes(self) -> None:
        self.run_reporting("all good\n")

        (run,) = self.runs()

        self.assertIsNone(run["tests_ran"])
        self.assertTrue(self.passing_ids())

    def test_a_run_that_ran_no_tests_is_recorded_but_does_not_pass(self) -> None:
        proc = self.run_reporting("collected 0 items\n\n===== no tests ran in 0.01s =====\n")

        (run,) = self.runs()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual((run["exit_code"], run["tests_ran"]), (0, 0))
        self.assertIn("R1 ran 0 tests (minimum 1); it does not count as a passing run", proc.stderr)
        self.assertIn("--min-tests 0", proc.stderr)
        self.assertEqual(self.passing_ids(), [])

    def test_min_tests_raises_or_lowers_the_bar(self) -> None:
        output = "Ran 3 tests in 0.1s\n\nOK\n"
        self.run_reporting(output, "--min-tests", "4")
        self.run_reporting(output, "--min-tests", "3")
        switched_off = self.run_reporting("Ran 0 tests in 0.0s\n", "--min-tests", "0")

        self.assertIn("R3 reported 0 tests and passes only because of --min-tests 0", switched_off.stderr)
        self.assertEqual(
            [(run["tests_ran"], run["min_tests"]) for run in self.runs()], [(3, 4), (3, 3), (0, 0)]
        )
        self.assertEqual(self.passing_ids(), ["R2", "R3"])

    def test_a_negative_minimum_is_a_usage_error(self) -> None:
        proc = self.run_reporting("", "--min-tests", "-1")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("--min-tests must be >= 0", proc.stderr)
        self.assertEqual(self.runs(), [])

    def test_the_whole_output_is_counted_not_only_the_stored_tail(self) -> None:
        self.run_reporting("7 passed in 0.1s\n" + "noise line\n" * 1000)

        (run,) = self.runs()

        self.assertEqual(run["tests_ran"], 7)
        self.assertNotIn("passed", str(run["output_tail"]))

    def test_summaries_of_several_runners_add_up(self) -> None:
        self.run_reporting("test result: ok. 4 passed; 0 failed; 0 ignored\ntest result: ok. 1 passed; 0 failed; 0 ignored\n")
        self.assertEqual(self.runs()[0]["tests_ran"], 5)

    def test_the_commands_exit_code_still_decides_a_failing_run(self) -> None:
        proc = self.run_reporting("1 failed, 2 passed in 0.1s\n", exit_code=1)

        self.assertEqual(proc.returncode, 1)
        self.assertEqual((self.runs()[0]["exit_code"], self.runs()[0]["tests_ran"]), (1, 3))

    def test_only_test_runs_are_counted(self) -> None:
        self.run_reporting("0 passed in 0.1s\n", kind="e2e")

        (run,) = self.runs()

        self.assertIsNone(run["tests_ran"])
        self.assertEqual(self.passing_ids(), ["R1"])

    def passing_ids(self) -> list[str]:
        ledger = load(self.repo.ledger_dir / "ledger.json")
        fingerprint = ledger.runs[0].fingerprint if ledger.runs else ""
        return [run.id for run in passing_runs(ledger, fingerprint)]

    def test_help_is_printed_without_a_command(self) -> None:
        for flag in ("-h", "--help"):
            with self.subTest(flag=flag):
                proc = self.repo.cli("run", flag)
                self.assertIn("usage: craftsman run", proc.stdout)

    def test_invalid_gate_state_records_nothing(self) -> None:
        state_file = self.repo.ledger_dir / "craftsman_state.json"
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text("{}", encoding="utf-8")
        proc = self.repo.cli("run", "--", sys.executable, "-c", "print('ok')", check=False)
        assert_cli_error(self, proc, 1, "not a valid gate state")

    @unittest.skipIf(os.name == "nt", "uses a POSIX shell script")
    def test_a_relative_program_path_is_relative_to_cwd(self) -> None:
        tool = self.tmp / "work" / "bin" / "tool"
        tool.parent.mkdir(parents=True)
        tool.write_text("#!/bin/sh\necho from-tool\n", encoding="utf-8")
        tool.chmod(0o755)

        outcome = runner.execute([os.path.join("bin", "tool")], self.tmp / "work", 30)

        self.assertEqual((outcome.exit_code, outcome.output_tail), (0, "from-tool\n"))

    @unittest.skipIf(os.name == "nt", "process groups are POSIX-only")
    def test_stop_tolerates_a_group_that_exits_before_the_kill(self) -> None:
        # The race is between our kill and the group's own exit; only a mock can order it.
        process = mock.Mock(pid=4242)
        process.wait.side_effect = [subprocess.TimeoutExpired("cmd", 5.0), 0]

        with mock.patch("craftsman.runner.os.killpg", side_effect=[None, ProcessLookupError()]):
            runner._stop(process, graceful=True)

        self.assertEqual(process.wait.call_count, 2)

    def test_a_closed_output_pipe_does_not_stall_the_command(self) -> None:
        script = "import sys; sys.stdout.write('x' * 1000000); print('done')"
        env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "craftsman.cli",
                "run",
                "--timeout",
                "40",
                "--",
                sys.executable,
                "-c",
                script,
            ],
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


if __name__ == "__main__":
    unittest.main()
