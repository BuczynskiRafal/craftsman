"""Tests for `craftsman run`: running a check command and recording it in the evidence ledger."""

from __future__ import annotations

import io
import os
import random
import re
import select
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import suppress
from pathlib import Path
from unittest import mock

from craftsman import runner
from craftsman.gatecheck import passing_runs
from craftsman.ledger import load
from craftsman.records import OUTPUT_TAIL_CHARS, LedgerError

from tests.support import REPO_ROOT, EvidenceRepo, assert_cli_error

if sys.platform == "win32":  # mypy checks this module for Windows too; the tests that send it are skipped there
    SIGHUP = signal.SIGTERM
else:
    SIGHUP = signal.SIGHUP


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


LIVE = runner.LIVE_LIMIT_CHARS
TAIL = runner.TAIL_CHARS
SLACK = runner._LINE_SLACK_CHARS
LIVE_MARKER = re.compile(
    rb"\[craftsman run: output continues; its end prints when the command exits; "
    rb"full output, written as it arrives: (?P<path>[^\]\n]+)\]\n"
)
END_MARKER = re.compile(
    rb"\[craftsman run: (?P<omitted>\d+) characters omitted; full output: (?P<path>[^\]\n]+)\]\n"
)
NOT_SAVED = re.compile(
    rb"\[craftsman run: (?:(?P<omitted>\d+) characters omitted; )?full output not saved: (?P<why>[^\n]+); "
    rb"the output continues below as it arrives\]\n"
)
ANY_MARKER = re.compile(rb"\[craftsman run: [^\n]*\]\n")
LAST_LINE = b"LAST LINE BEFORE THE HANG\n"


def numbered_lines(count: int) -> str:
    return "".join(f"line {index:05d} of the test log\n" for index in range(count))


def blocks(size: int) -> str:
    """`size` characters in lines of 500: a line ends exactly at the live limit, so no cut moves."""
    return (("x" * 499 + "\n") * (size // 500 + 1))[:size]


def reassembles(printed: bytes, data: bytes) -> bool:
    """Whether `printed`, without its marker lines (and a line break put in front of one), is `data`."""
    pieces = ANY_MARKER.split(printed)
    options = [b""]
    for index, piece in enumerate(pieces):
        variants = [piece]
        if index < len(pieces) - 1 and piece.endswith(b"\n"):
            variants.append(piece[:-1])
        options = [done + variant for done in options for variant in variants]
    return data in options


class _Stdout:
    """A stdout with a byte buffer, like the real one."""

    def __init__(self) -> None:
        self.buffer = io.BytesIO()

    def flush(self) -> None:
        pass


def echo_through(craftsman_dir: Path | None, chunks: list[bytes]) -> bytes:
    """What `OutputEcho` prints for `chunks` that arrive one after the other."""
    stdout = _Stdout()
    echo = runner.OutputEcho(craftsman_dir)
    with mock.patch.object(sys, "stdout", stdout):
        for chunk in chunks:
            echo.feed(chunk)
        echo.finish()
    return stdout.buffer.getvalue()


def without_paths(printed: bytes) -> bytes:
    """`printed` with the log path in its marker lines replaced: the scratch directory name is random."""
    return re.sub(rb"(full output(?:, written as it arrives)?): [^\]\n]+\]\n", rb"\1: PATH]\n", printed)


def found(pattern: re.Pattern[bytes], printed: bytes) -> re.Match[bytes]:
    match = pattern.search(printed)
    assert match is not None, printed[:3000]
    return match


def path_in(match: re.Match[bytes]) -> Path:
    return Path(match.group("path").decode("utf-8"))


def is_running(pid: int) -> bool:
    """Whether `pid` is a live process; a zombie nobody has reaped yet counts as gone."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return True
    return stat.rsplit(")", 1)[1].split()[0] != "Z"


class LongOutputTests(unittest.TestCase):
    """Output prints live up to a limit; past it a log takes all of it, and its end prints when it ends."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.repo = EvidenceRepo(self.tmp / "repo")

    def run_bytes(self, data: bytes, *flags: str, exit_code: int = 0) -> tuple[bytes, str, int]:
        """Run a test command that writes `data` and exits with `exit_code`: its stdout, stderr and exit code."""
        source = self.tmp / "output.bin"  # outside the work tree; too long for an argument
        source.write_bytes(data)
        script = f"import sys; sys.stdout.buffer.write(open({str(source)!r}, 'rb').read()); sys.exit({exit_code})"
        env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
        proc = subprocess.run(
            [sys.executable, "-B", "-m", "craftsman.cli", "run", "--kind", "test", *flags, "--",
             sys.executable, "-c", script],
            cwd=self.repo.root, env=env, capture_output=True, check=False, timeout=60,
        )  # fmt: skip
        return proc.stdout, proc.stderr.decode("utf-8", errors="replace"), proc.returncode

    def run_text(self, text: str, *flags: str, exit_code: int = 0) -> tuple[bytes, str, int]:
        return self.run_bytes(text.encode("utf-8"), *flags, exit_code=exit_code)

    def runs(self) -> list[dict[str, object]]:
        runs = self.repo.show()["runs"]
        assert isinstance(runs, list)
        return runs

    def log_of(self, printed: bytes) -> bytes:
        """The content of the log file the first marker line of `printed` names."""
        return path_in(found(LIVE_MARKER, printed)).read_bytes()

    def scratch_dir(self) -> Path:
        return self.repo.ledger_dir / "scratch"

    def test_output_up_to_the_live_limit_prints_unchanged_and_writes_no_log(self) -> None:
        for text in (blocks(LIVE), numbered_lines(1000)[:LIVE], "é" * LIVE):
            with self.subTest(text=text[:10]):
                printed, _, code = self.run_text(text)

                self.assertEqual((printed, code), (text.encode("utf-8"), 0))
                self.assertFalse(self.scratch_dir().exists())

    def test_one_character_past_the_limit_names_the_log_and_prints_that_character_at_the_end(self) -> None:
        text = numbered_lines(1000)[: LIVE + 1]  # the limit falls inside a line whose end never comes

        printed, _, _ = self.run_text(text)

        marker = found(LIVE_MARKER, printed)
        self.assertEqual(printed[: marker.start()], text[:LIVE].encode("utf-8") + b"\n")
        self.assertEqual(printed[marker.end() :], text[LIVE:].encode("utf-8"))
        self.assertIsNone(END_MARKER.search(printed))
        self.assertEqual(self.log_of(printed), text.encode("utf-8"))

    def test_up_to_twice_the_limit_arrives_complete(self) -> None:
        text = blocks(LIVE + TAIL)

        printed, _, _ = self.run_text(text)

        marker = found(LIVE_MARKER, printed)
        self.assertEqual(printed[: marker.start()] + printed[marker.end() :], text.encode("utf-8"))
        self.assertIsNone(END_MARKER.search(printed))
        self.assertEqual(self.log_of(printed), text.encode("utf-8"))

    def test_one_character_more_omits_one_character(self) -> None:
        text = blocks(LIVE + TAIL + 1)

        printed, _, _ = self.run_text(text)

        live, end = found(LIVE_MARKER, printed), found(END_MARKER, printed)
        self.assertEqual(live.end(), end.start())
        self.assertEqual(printed[: live.start()], text[:LIVE].encode("utf-8"))
        self.assertEqual(int(end.group("omitted")), 1)
        self.assertEqual(printed[end.end() :], text[-TAIL:].encode("utf-8"))
        self.assertEqual(self.log_of(printed), text.encode("utf-8"))

    def test_long_output_is_the_live_part_two_markers_and_the_tail_cut_at_lines(self) -> None:
        text = numbered_lines(2000)  # 62,000 characters in lines of 31

        printed, stderr, code = self.run_text(text)

        live, end = found(LIVE_MARKER, printed), found(END_MARKER, printed)
        # The live part ends at the limit, or with its line when the end of that line came with it.
        at_limit, with_line = text[:LIVE] + "\n", text[: text.index("\n", LIVE) + 1]
        head = printed[: live.start()].decode("utf-8")
        self.assertIn(head, (at_limit, with_line))
        shown = LIVE if head == at_limit else len(with_line)
        tail = text[text.index("\n", len(text) - TAIL) + 1 :]
        self.assertEqual(printed[end.end() :], tail.encode("utf-8"))
        self.assertEqual(int(end.group("omitted")), len(text) - shown - len(tail))
        self.assertEqual(self.log_of(printed), text.encode("utf-8"))
        self.assertEqual(code, 0)
        self.assertIn("R1 recorded (test, exit 0", stderr)

    def test_the_log_keeps_the_path_it_was_named_with(self) -> None:
        printed, _, _ = self.run_text(numbered_lines(2000))
        self.run_text(numbered_lines(2000))

        log = path_in(found(LIVE_MARKER, printed))
        self.assertEqual(path_in(found(END_MARKER, printed)), log)
        self.assertTrue(log.is_file())
        self.assertEqual((log.name, log.parent.parent), ("run-output.log", self.scratch_dir()))
        self.assertEqual(len(list(self.scratch_dir().glob("*/run-output.log"))), 2)

    def test_markers_never_look_like_the_recorded_line(self) -> None:
        printed, _, _ = self.run_text(numbered_lines(2000))

        self.assertEqual(len(ANY_MARKER.findall(printed)), 2)
        self.assertNotIn(b"recorded (", printed)

    def test_output_without_any_line_break_is_cut_by_characters(self) -> None:
        printed, _, _ = self.run_text("x" * 50_000)

        live, end = found(LIVE_MARKER, printed), found(END_MARKER, printed)
        self.assertEqual(printed[: live.start()], b"x" * LIVE + b"\n")
        self.assertEqual(int(end.group("omitted")), 50_000 - LIVE - TAIL)
        self.assertEqual(printed[end.end() :], b"x" * TAIL)
        self.assertEqual(self.log_of(printed), b"x" * 50_000)

    def test_a_multibyte_character_is_never_split_at_either_cut(self) -> None:
        text = "€" * 20_000  # three bytes each, no line breaks

        printed, _, _ = self.run_text(text)

        live, end = found(LIVE_MARKER, printed), found(END_MARKER, printed)
        self.assertEqual(printed[: live.start()], ("€" * LIVE + "\n").encode("utf-8"))
        self.assertEqual(printed[end.end() :], ("€" * TAIL).encode("utf-8"))
        self.assertEqual(int(end.group("omitted")), 20_000 - LIVE - TAIL)
        self.assertEqual(self.log_of(printed), text.encode("utf-8"))

    def test_bytes_that_are_not_utf8_reach_stdout_and_the_log_unchanged(self) -> None:
        data = b"\xff\xfe head\n" + b"ok \xc3\xa9\n" * 4000 + b"bad \x80\xc3 tail\n"

        printed, _, _ = self.run_bytes(data)

        self.assertEqual(self.log_of(printed), data)
        self.assertTrue(data.startswith(printed[: found(LIVE_MARKER, printed).start()]))
        self.assertTrue(data.endswith(printed[found(END_MARKER, printed).end() :]))
        self.assertTrue(printed.endswith(b"bad \x80\xc3 tail\n"))

    def test_crlf_output_keeps_its_line_endings(self) -> None:
        text = numbered_lines(2000).replace("\n", "\r\n")

        printed, _, _ = self.run_text(text)

        end = found(END_MARKER, printed)
        self.assertTrue(printed[: found(LIVE_MARKER, printed).start()].endswith(b"\r\n"))
        self.assertTrue(printed[end.end() :].startswith(b"line "))
        self.assertTrue(printed.endswith(b"of the test log\r\n"))
        self.assertEqual(self.log_of(printed), text.encode("utf-8"))

    def test_a_command_that_prints_nothing_prints_nothing(self) -> None:
        printed, stderr, code = self.run_bytes(b"")

        self.assertEqual((printed, code), (b"", 0))
        self.assertIn("R1 recorded", stderr)
        self.assertFalse(self.scratch_dir().exists())

    def test_a_failing_command_shows_its_failure_summary_in_the_tail(self) -> None:
        text = numbered_lines(2000) + "FAILED (failures=2)\n"

        printed, _, code = self.run_text(text, exit_code=1)

        self.assertEqual(code, 1)
        self.assertTrue(printed.endswith(b"FAILED (failures=2)\n"))
        self.assertEqual(self.runs()[0]["exit_code"], 1)

    def test_the_whole_output_is_counted_when_the_summary_is_not_printed(self) -> None:
        text = "." * 10_000 + "\nRan 5 tests in 0.1s\n\nOK\n" + "." * 10_000 + "\n"

        printed, _, _ = self.run_text(text)

        self.assertNotIn(b"Ran 5 tests", printed)
        self.assertEqual(self.runs()[0]["tests_ran"], 5)

    def test_the_ledger_record_is_the_same_with_and_without_the_log(self) -> None:
        text = numbered_lines(2000) + "Ran 4 tests in 0.1s\n\nOK\n"

        self.run_text(text)
        self.run_text(text, "--full")

        held, full = self.runs()
        for key in ("kind", "exit_code", "output_tail", "tests_ran", "min_tests", "fingerprint", "argv"):
            self.assertEqual(held[key], full[key], key)
        self.assertEqual(held["tests_ran"], 4)

    def test_full_prints_everything_and_writes_no_log(self) -> None:
        text = numbered_lines(2000)

        printed, _, _ = self.run_text(text, "--full")

        self.assertEqual(printed, text.encode("utf-8"))
        self.assertFalse(self.scratch_dir().exists())

    def test_an_unwritable_scratch_directory_hides_nothing(self) -> None:
        self.scratch_dir().parent.mkdir(parents=True, exist_ok=True)
        self.scratch_dir().write_text("in the way", encoding="utf-8")
        text = blocks(30_000)

        printed, stderr, code = self.run_text(text)

        marker = found(NOT_SAVED, printed)
        self.assertIsNone(marker.group("omitted"))
        self.assertEqual(printed[: marker.start()] + printed[marker.end() :], text.encode("utf-8"))
        self.assertEqual(code, 0)
        self.assertIn("R1 recorded (test, exit 0", stderr)
        self.assertEqual(self.runs()[0]["exit_code"], 0)

    def test_help_describes_the_live_limit_the_log_and_full(self) -> None:
        text = " ".join(self.repo.cli("run", "--help").stdout.split())

        self.assertIn("--full", text)
        self.assertIn("6,000", text)
        self.assertIn("scratch", text)
        self.assertIn("grows while the command runs", text)

    def test_the_end_still_prints_when_the_run_cannot_be_recorded(self) -> None:
        stdout = _Stdout()
        script = "print('x' * 20_000); print('FAILED (failures=1)')"

        with mock.patch.object(sys, "stdout", stdout), mock.patch.object(
            runner, "_persist_run", side_effect=LedgerError("ledger is locked")
        ), self.assertRaises(LedgerError):
            runner._run_recorded([sys.executable, "-c", script], self.repo.root, "test", 60)

        printed = stdout.buffer.getvalue()
        self.assertEqual(path_in(found(END_MARKER, printed)).name, "run-output.log")
        self.assertTrue(printed.endswith(b"FAILED (failures=1)\n"))


@unittest.skipIf(os.name == "nt", "process groups, SIGHUP and select on pipes are POSIX-only")
class WhileTheCommandRunsTests(unittest.TestCase):
    """What a reader sees while the command still runs, and what SIGTERM and SIGHUP to `craftsman run` do."""

    # Writes its pid, prints 300 lines (21,000 characters) and a last line, then hangs.
    SCRIPT = (
        "import os, sys, time\n"
        "open(sys.argv[1], 'w').write(str(os.getpid()))\n"
        "for i in range(1, 301): print(f'progress line {i:03d} ' + '.' * 50, flush=True)\n"
        "print('LAST LINE BEFORE THE HANG', flush=True)\n"
        "time.sleep(120)\n"
    )

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.repo = EvidenceRepo(self.tmp / "repo")
        self.pid_file = self.tmp / "child.pid"

    def start(self) -> subprocess.Popen[bytes]:
        self.pid_file.unlink(missing_ok=True)
        proc = subprocess.Popen(
            [sys.executable, "-B", "-m", "craftsman.cli", "run", "--kind", "test", "--",
             sys.executable, "-c", self.SCRIPT, str(self.pid_file)],
            cwd=self.repo.root, env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )  # fmt: skip
        self.addCleanup(self.clean_up, proc, self.pid_file)
        return proc

    def clean_up(self, proc: subprocess.Popen[bytes], pid_file: Path) -> None:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        if sys.platform != "win32":  # the class is skipped on Windows; this keeps mypy's Windows check
            with suppress(OSError, ValueError):
                os.killpg(int(pid_file.read_text(encoding="utf-8")), signal.SIGKILL)
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                stream.close()

    def read_until_marker(self, proc: subprocess.Popen[bytes]) -> tuple[bytes, Path]:
        """Read stdout until the live marker has come; the bytes so far and the log the marker names."""
        assert proc.stdout is not None
        printed, deadline = b"", time.monotonic() + 30
        while (match := LIVE_MARKER.search(printed)) is None:
            self.assertLess(time.monotonic(), deadline, printed[-500:])
            ready, _, _ = select.select([proc.stdout], [], [], 1)
            if ready:
                chunk = os.read(proc.stdout.fileno(), 65536)
                self.assertTrue(chunk, "stdout ended before the marker")
                printed += chunk
        return printed, path_in(match)

    def wait_for_log(self, log: Path, needle: bytes) -> bytes:
        deadline = time.monotonic() + 30
        while needle not in (content := log.read_bytes()):
            self.assertLess(time.monotonic(), deadline, content[-500:])
            time.sleep(0.05)
        return content

    def start_and_wait_for_the_hang(self) -> tuple[subprocess.Popen[bytes], bytes, Path]:
        proc = self.start()
        printed, log = self.read_until_marker(proc)
        self.wait_for_log(log, LAST_LINE)
        return proc, printed, log

    def test_the_live_part_and_a_readable_log_arrive_while_the_command_runs(self) -> None:
        proc = self.start()

        printed, log = self.read_until_marker(proc)
        content = self.wait_for_log(log, LAST_LINE)

        self.assertIsNone(proc.poll())  # the command still hangs
        self.assertTrue(log.is_absolute())
        self.assertTrue(content.startswith(b"progress line 001 "))
        self.assertTrue(content.endswith(LAST_LINE))
        head = printed[: found(LIVE_MARKER, printed).start()]  # with a line break put in front of the marker
        self.assertGreaterEqual(len(head), LIVE)
        self.assertTrue(content.startswith(head) or content.startswith(head[:-1]))
        self.assertTrue(printed.endswith(b"]\n"))  # nothing past the marker yet

    def terminate(self, signum: signal.Signals) -> None:
        proc, printed, log = self.start_and_wait_for_the_hang()
        child = int(self.pid_file.read_text(encoding="utf-8"))

        proc.send_signal(signum)
        rest, errors = proc.communicate(timeout=30)
        printed += rest

        self.assertEqual(proc.returncode, 128 + signum)
        self.assertFalse(is_running(child))
        end = found(END_MARKER, printed)
        self.assertEqual(path_in(end), log)
        self.assertTrue(printed.endswith(LAST_LINE))
        self.assertTrue(log.read_bytes().endswith(printed[end.end() :]))
        stderr = errors.decode("utf-8", errors="replace")
        self.assertIn(f"{signum.name} received; the command was stopped", stderr)
        self.assertIn(f"R1 recorded (test, exit {runner.EXIT_INTERRUPTED}", stderr)
        self.assertNotIn("Traceback", stderr)
        (run,) = self.repo.show()["runs"]
        self.assertEqual(run["exit_code"], runner.EXIT_INTERRUPTED)  # what Ctrl-C records
        self.assertTrue(str(run["output_tail"]).endswith(LAST_LINE.decode()))

    def test_sigterm_stops_the_command_records_it_and_prints_the_end(self) -> None:
        self.terminate(signal.SIGTERM)

    def test_sighup_stops_the_command_records_it_and_prints_the_end(self) -> None:
        self.terminate(SIGHUP)

    def test_sigterm_records_what_an_interrupt_records(self) -> None:
        codes = []
        for signum in (signal.SIGINT, signal.SIGTERM):
            proc, _, _ = self.start_and_wait_for_the_hang()
            proc.send_signal(signum)
            proc.communicate(timeout=30)
            codes.append(proc.returncode)

        first, second = self.repo.show()["runs"]
        self.assertEqual(codes, [runner.EXIT_INTERRUPTED, 128 + signal.SIGTERM])
        for key in ("kind", "exit_code", "output_tail", "tests_ran", "argv"):
            self.assertEqual(first[key], second[key], key)

    def test_the_previous_handlers_come_back(self) -> None:
        before = signal.getsignal(signal.SIGTERM), signal.getsignal(SIGHUP)

        with runner._TerminationSignals():
            self.assertNotEqual(signal.getsignal(signal.SIGTERM), before[0])
            self.assertNotEqual(signal.getsignal(SIGHUP), before[1])

        self.assertEqual((signal.getsignal(signal.SIGTERM), signal.getsignal(SIGHUP)), before)

    def test_a_signal_while_the_command_runs_stops_it_like_ctrl_c(self) -> None:
        with runner._TerminationSignals() as signals, self.assertRaises(KeyboardInterrupt):
            os.kill(os.getpid(), SIGHUP)
            time.sleep(5)

        self.assertEqual(signals.exit_code(0), 128 + SIGHUP)

    def test_a_signal_after_the_command_ended_waits_for_the_record(self) -> None:
        with runner._TerminationSignals() as signals:
            signals.defer()
            os.kill(os.getpid(), signal.SIGTERM)
            time.sleep(0.1)  # the handler runs here and must not raise
            os.kill(os.getpid(), SIGHUP)  # a second signal changes nothing
            time.sleep(0.1)

        self.assertEqual(signals.exit_code(0), 128 + signal.SIGTERM)


class OutputEchoTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name).resolve() / "craftsman"

    def test_output_prints_as_it_arrives_and_the_log_is_current(self) -> None:
        stdout = _Stdout()
        echo = runner.OutputEcho(self.dir)

        with mock.patch.object(sys, "stdout", stdout):
            echo.feed(b"a" * 5000)
            self.assertEqual(stdout.buffer.getvalue(), b"a" * 5000)
            self.assertIsNone(echo.log_path)
            echo.feed(b"b" * 3000)
            at_the_limit = stdout.buffer.getvalue()
            log = echo.log_path
            assert log is not None
            self.assertEqual(log.read_bytes(), b"a" * 5000 + b"b" * 3000)
            echo.feed(b"c" * 10)
            self.assertEqual(log.read_bytes(), b"a" * 5000 + b"b" * 3000 + b"c" * 10)
            self.assertEqual(stdout.buffer.getvalue(), at_the_limit)
            echo.finish()

        marker = found(LIVE_MARKER, at_the_limit)
        self.assertEqual(at_the_limit[: marker.start()], b"a" * 5000 + b"b" * 1000 + b"\n")
        self.assertEqual(marker.end(), len(at_the_limit))
        self.assertEqual(path_in(marker), log)
        self.assertEqual(stdout.buffer.getvalue()[marker.end() :], b"b" * 2000 + b"c" * 10)
        self.assertEqual(echo.log_path, log)

    def test_the_live_part_ends_with_its_line_when_that_line_has_arrived(self) -> None:
        text = "x" * (LIVE - 10) + "y" * 50 + "\n" + "z" * 100

        printed = echo_through(self.dir, [text.encode("utf-8")])

        marker = found(LIVE_MARKER, printed)
        self.assertEqual(printed[: marker.start()], ("x" * (LIVE - 10) + "y" * 50 + "\n").encode("utf-8"))
        self.assertEqual(printed[marker.end() :], b"z" * 100)

    def test_a_line_end_too_far_past_the_limit_does_not_move_the_cut(self) -> None:
        printed = echo_through(self.dir, [b"x" * (LIVE + SLACK) + b"\n"])

        marker = found(LIVE_MARKER, printed)
        self.assertEqual(printed[: marker.start()], b"x" * LIVE + b"\n")
        self.assertEqual(printed[marker.end() :], b"x" * SLACK + b"\n")

    def test_chunking_does_not_change_what_prints(self) -> None:
        data = ("é" * 7 + "\n").encode("utf-8") * 3000 + b"\xe2\x82"  # ends inside a character
        whole = without_paths(echo_through(self.dir, [data]))

        for size in (1, 5, 4096):
            with self.subTest(size=size):
                pieces = [data[i : i + size] for i in range(0, len(data), size)]
                self.assertEqual(without_paths(echo_through(self.dir, pieces)), whole)
        self.assertTrue(whole.endswith(b"\xe2\x82"))

    def test_memory_stays_bounded_for_huge_output(self) -> None:
        stdout = _Stdout()
        echo = runner.OutputEcho(self.dir)
        line = b"0123456789abcdef\n"

        with mock.patch.object(sys, "stdout", stdout):
            for _ in range(200):
                echo.feed(line * 4000)
            held = len(echo._live) + len(echo._rest)
            echo.finish()

        self.assertLessEqual(held, LIVE + SLACK + TAIL)
        self.assertLess(len(stdout.buffer.getvalue()), LIVE + SLACK + TAIL + 1000)
        (log,) = self.dir.glob("scratch/*/run-output.log")
        self.assertEqual(log.stat().st_size, 200 * 4000 * len(line))

    def test_a_log_that_cannot_be_started_hides_nothing(self) -> None:
        data = b"x\n" * 20_000

        with mock.patch.object(runner, "create_scratch_dir", side_effect=LedgerError("cannot create here")):
            printed = echo_through(self.dir, [data[:7000], data[7000:]])

        marker = found(NOT_SAVED, printed)
        self.assertEqual(marker.group("why"), b"cannot create here")
        self.assertEqual(printed[: marker.start()] + printed[marker.end() :], data)

    def test_a_log_that_fails_midway_is_removed_and_the_rest_prints(self) -> None:
        stdout = _Stdout()
        echo = runner.OutputEcho(self.dir)
        data = b"x\n" * 3500 + b"y\n" * 100 + b"z\n" * 100

        with mock.patch.object(sys, "stdout", stdout):
            echo.feed(data[:7000])
            assert echo._log is not None
            echo._log.write = mock.Mock(side_effect=OSError("disk full"))  # type: ignore[method-assign]
            echo.feed(data[7000:7200])
            echo.feed(data[7200:])
            echo.finish()

        printed = stdout.buffer.getvalue()
        marker = found(NOT_SAVED, printed)
        self.assertEqual((marker.group("why"), marker.group("omitted")), (b"disk full", None))
        self.assertTrue(reassembles(printed, data))
        self.assertEqual(list(self.dir.glob("scratch/*/*")), [])
        self.assertIsNone(echo.log_path)

    def test_a_log_that_fails_after_much_output_says_what_was_omitted(self) -> None:
        stdout = _Stdout()
        echo = runner.OutputEcho(self.dir)

        with mock.patch.object(sys, "stdout", stdout):
            echo.feed(b"x" * 20_000)
            assert echo._log is not None
            echo._log.write = mock.Mock(side_effect=OSError("disk full"))  # type: ignore[method-assign]
            echo.feed(b"y" * 10)
            echo.feed(b"z" * 10)
            echo.finish()

        printed = stdout.buffer.getvalue()
        marker = found(NOT_SAVED, printed)
        self.assertEqual(int(marker.group("omitted")), 20_010 - LIVE - TAIL)
        self.assertEqual(printed[marker.end() :], b"x" * (TAIL - 10) + b"y" * 10 + b"z" * 10)

    def test_a_tail_that_starts_at_a_line_keeps_that_line(self) -> None:
        text = ("x" * 39 + "\n") * 600  # the last TAIL characters are 150 whole lines

        printed = echo_through(self.dir, [text.encode("utf-8")])

        self.assertTrue(printed.endswith(b"]\n" + text[-TAIL:].encode("utf-8")))

    def test_finishing_twice_prints_once(self) -> None:
        stdout = _Stdout()
        echo = runner.OutputEcho(self.dir)
        with mock.patch.object(sys, "stdout", stdout):
            echo.feed(b"x\n" * 20_000)
            echo.finish()
            once = stdout.buffer.getvalue()
            echo.finish()
            echo.feed(b"late\n")

        self.assertEqual(stdout.buffer.getvalue(), once)

    def test_a_reader_that_went_away_does_not_stop_the_log(self) -> None:
        stdout = _Stdout()
        stdout.buffer = mock.Mock(write=mock.Mock(side_effect=BrokenPipeError()))
        echo = runner.OutputEcho(self.dir)

        with mock.patch.object(sys, "stdout", stdout):
            echo.feed(b"x\n" * 20_000)
            echo.finish()

        (log,) = self.dir.glob("scratch/*/run-output.log")
        self.assertEqual(log.read_bytes(), b"x\n" * 20_000)

    def test_random_output_keeps_its_head_its_tail_and_its_log(self) -> None:
        rng = random.Random(20261110)
        alphabet = [b"a", b"b", b"\n", b"\r\n", b"\xc3\xa9", b"\xe2\x82\xac", b"\xf0\x9f\x98\x80", b"\xff", b"\x80", b"\xc3"]
        for case in range(150):
            size = rng.choice([rng.randrange(0, 200), rng.randrange(4_000, 14_000), rng.randrange(14_000, 40_000)])
            data = b"".join(rng.choice(alphabet) for _ in range(size))
            chunks = []
            at = 0
            while at < len(data):
                step = rng.choice([1, 3, 64, 4096, 65536])
                chunks.append(data[at : at + step])
                at += step
            with self.subTest(case=case, size=len(data)):
                printed = echo_through(self.dir / str(case), chunks)
                live, end = LIVE_MARKER.search(printed), END_MARKER.search(printed)
                if live is None:
                    self.assertEqual(printed, data)
                    continue
                self.assertEqual(path_in(live).read_bytes(), data)
                head = printed[: live.start()]
                self.assertTrue(data.startswith(head) or data.startswith(head[:-1]))
                if end is None:
                    self.assertTrue(reassembles(printed, data))
                else:
                    self.assertEqual(live.end(), end.start())
                    self.assertTrue(data.endswith(printed[end.end() :]))
                shown = printed.decode("utf-8", errors="surrogateescape")
                self.assertLess(len(shown), LIVE + SLACK + TAIL + 1000)


if __name__ == "__main__":
    unittest.main()
