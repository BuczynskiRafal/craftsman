"""`bin/launcher.sh`: the first Python 3.10+ runs the CLI in one start, passing everything through."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from craftsman import __version__

from tests.support import REPO_ROOT

LAUNCHER = REPO_ROOT / "bin" / "launcher.sh"
BOOTSTRAP = REPO_ROOT / "bin" / "craftsman-cli.py"
VERSION_LINE = f"craftsman {__version__}\n"
SHELLS = tuple(shell for name in ("sh", "dash", "bash") if (shell := shutil.which(name)) is not None)
# Runs the bootstrap as an interpreter older than 3.10 would: the version it sees is 3.9.
AS_PYTHON_3_9 = (
    "import runpy, sys; sys.version_info = (3, 9, 0); sys.argv = sys.argv[1:]; "
    "runpy.run_path(sys.argv[0], run_name='__main__')"
)


@unittest.skipIf(os.name == "nt", "a POSIX shell script")
class LauncherTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.log = self.tmp / "calls.log"

    def fake(self, name: str, body: str) -> None:
        """An interpreter `name` on the PATH that logs its arguments, then runs `body`."""
        path = self.bin / name
        path.write_text(f'#!/bin/sh\necho "{name} $*" >> "{self.log}"\n{body}\n', encoding="utf-8")
        path.chmod(0o755)

    def working(self, name: str) -> None:
        self.fake(name, f'exec "{sys.executable}" -B "$@"')

    def too_old(self, name: str) -> None:
        """Python 3.9: it fails the version check and runs the bootstrap as 3.9 would."""
        self.fake(
            name,
            f'[ "$1" = "-c" ] && exec "{sys.executable}" -c "import sys; sys.exit(1)"\n'
            f'exec "{sys.executable}" -B -c "{AS_PYTHON_3_9}" "$@"',
        )

    def launch(self, shell: str, *args: str, stdin: str = "") -> subprocess.CompletedProcess[str]:
        env = {"PATH": str(self.bin), "HOME": str(self.tmp)}
        return subprocess.run(
            [shell, str(LAUNCHER), *args], input=stdin, capture_output=True, encoding="utf-8", env=env,
            cwd=self.tmp, timeout=60, check=False,
        )  # fmt: skip

    def calls(self) -> list[str]:
        return self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []

    def test_the_first_working_interpreter_runs_the_cli_once(self) -> None:
        self.working("python3")
        self.working("python")
        for shell in SHELLS:
            with self.subTest(shell=shell):
                self.log.unlink(missing_ok=True)
                result = self.launch(shell, "--version")
                self.assertEqual((result.returncode, result.stdout), (0, VERSION_LINE), result.stderr)
                self.assertEqual(self.calls(), [f"python3 {BOOTSTRAP} --version"])

    def test_an_interpreter_older_than_3_10_is_skipped_with_stdin_left_unread(self) -> None:
        self.too_old("python3")
        self.working("python")
        payload = json.dumps({"cwd": str(self.tmp), "tool_name": "Read", "tool_input": {}})
        for shell in SHELLS:
            with self.subTest(shell=shell):
                self.log.unlink(missing_ok=True)
                result = self.launch(shell, "hook", "pre-tool", "--platform", "claude", stdin=payload)
                # `{}` needs the payload: without it the hook would ask (the input has no `cwd`).
                self.assertEqual((result.returncode, result.stdout), (0, "{}\n"), result.stderr)
                self.assertEqual(
                    [call.split()[0] for call in self.calls()],
                    ["python3", "python3", "python"],
                    self.calls(),
                )

    def test_no_python_3_10_leaves_the_gate_inactive(self) -> None:
        self.too_old("python3")
        self.fake("python", "exit 127")  # not a working interpreter at all
        for shell in SHELLS:
            with self.subTest(shell=shell):
                result = self.launch(shell, "hook", "stop", "--platform", "claude", stdin="{}")
                self.assertEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr, "Craftsman gate inactive: Python 3.10+ not found\n")

    def test_the_exit_code_and_arguments_pass_through(self) -> None:
        self.working("python3")
        self.working("python")
        for shell in SHELLS:
            with self.subTest(shell=shell):
                self.log.unlink(missing_ok=True)
                result = self.launch(shell, "hook", "pre-tool", "--platform", "no such platform")
                self.assertEqual(result.returncode, 2)
                self.assertIn("craftsman hook error", result.stderr)
                # A version check tells a failing command from a failing interpreter: one run.
                self.assertEqual([call.split()[0] for call in self.calls()], ["python3", "python3"])
                self.assertIn("no such platform", self.calls()[0])

    def test_the_reserved_code_from_a_working_interpreter_is_the_commands(self) -> None:
        # `craftsman run` passes on its command's exit code, 97 included: the command must not run twice.
        self.fake(
            "python3",
            f'[ "$1" = "-c" ] && exec "{sys.executable}" "$@"\n'
            f'exec "{sys.executable}" -c "import sys; sys.stdin.read(); sys.exit(97)"',
        )
        self.working("python")
        for shell in SHELLS:
            with self.subTest(shell=shell):
                self.log.unlink(missing_ok=True)
                result = self.launch(shell, "run", "--", "false", stdin="x")
                self.assertEqual(result.returncode, 97)
                self.assertEqual([call.split()[0] for call in self.calls()], ["python3", "python3"])

    @unittest.skipUnless(os.path.isdir("/proc"), "reads the state of a process in /proc")
    def test_a_stopped_launcher_stops_the_interpreter(self) -> None:
        # A platform stops a hook that ran too long by signalling the launcher: the interpreter must not live on.
        pid_file = self.tmp / "child.pid"
        self.fake("python3", f'echo $$ > "{pid_file}"\nexec sleep 60')
        for shell in SHELLS:
            for sent, code in ((signal.SIGTERM, 143), (signal.SIGINT, 130), (signal.SIGHUP, 129)):
                with self.subTest(shell=shell, signal=sent.name):
                    pid_file.unlink(missing_ok=True)
                    launcher = subprocess.Popen(
                        [shell, str(LAUNCHER), "hook", "stop"], stdin=subprocess.PIPE, cwd=self.tmp,
                        env={"PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}", "HOME": str(self.tmp)},
                    )  # fmt: skip
                    assert launcher.stdin is not None
                    self.addCleanup(launcher.stdin.close)
                    self.addCleanup(launcher.kill)
                    deadline = time.monotonic() + 30
                    while not pid_file.is_file() or not pid_file.read_text(encoding="utf-8").strip():
                        self.assertLess(time.monotonic(), deadline, "the interpreter did not start")
                        time.sleep(0.02)
                    child = int(pid_file.read_text(encoding="utf-8"))

                    launcher.send_signal(sent)

                    self.assertEqual(launcher.wait(timeout=30), code)
                    while running(child):
                        self.assertLess(time.monotonic(), deadline, "the interpreter outlived the launcher")
                        time.sleep(0.02)


def running(pid: int) -> bool:
    """Whether process `pid` exists and has not finished (a zombie waits only to be reaped)."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return False
    return stat.rpartition(")")[2].split()[0] != "Z"


class LauncherPathTests(unittest.TestCase):
    """How the launcher finds its own folder, the first lines of the script, run with a given `$0`."""

    def bin_dir(self, shell: str, zero: str) -> str:
        lines = LAUNCHER.read_text(encoding="utf-8").splitlines()
        start = lines.index("case $0 in")
        end = next(index for index, line in enumerate(lines) if line.startswith("BIN_DIR="))
        program = "\n".join([*lines[start : end + 1], 'printf %s "$BIN_DIR"'])
        result = subprocess.run(
            [shell, "-c", program, zero], cwd=REPO_ROOT, capture_output=True, encoding="utf-8", timeout=30,
            check=True,
        )  # fmt: skip
        return result.stdout

    @unittest.skipIf(os.name == "nt", "runs a POSIX shell")
    def test_windows_and_posix_paths_resolve_to_the_bin_folder(self) -> None:
        for zero, expected in (
            # Git Bash on Windows: "${CLAUDE_PLUGIN_ROOT}/bin/launcher.sh" with a drive letter, either slash.
            ("C:/Users/Ana Lee/.claude/plugins/craftsman/bin/launcher.sh", "C:/Users/Ana Lee/.claude/plugins/craftsman/bin"),
            ("C:\\Users\\x\\craftsman\\bin\\launcher.sh", "C:\\Users\\x\\craftsman\\bin"),
            ("d:\\craftsman/bin\\launcher.sh", "d:\\craftsman/bin"),
            ("\\\\server\\share\\craftsman\\bin\\launcher.sh", "\\\\server\\share\\craftsman\\bin"),
            ("/opt/craftsman/bin/launcher.sh", "/opt/craftsman/bin"),
            ("bin/launcher.sh", f"{REPO_ROOT}/bin"),
            ("bin\\launcher.sh", f"{REPO_ROOT}/bin"),
        ):
            for shell in SHELLS:
                with self.subTest(zero=zero, shell=shell):
                    self.assertEqual(self.bin_dir(shell, zero), expected)


class BootstrapTests(unittest.TestCase):
    def run_bootstrap(self, *prefix: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-B", *prefix, str(BOOTSTRAP), "--version"],
            input="", capture_output=True, encoding="utf-8", timeout=60, check=False,
        )  # fmt: skip

    def test_it_runs_the_cli_of_its_own_checkout(self) -> None:
        result = self.run_bootstrap()
        self.assertEqual((result.returncode, result.stdout), (0, VERSION_LINE), result.stderr)

    def test_an_older_interpreter_exits_with_the_reserved_code_before_running_anything(self) -> None:
        result = self.run_bootstrap("-c", AS_PYTHON_3_9)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (97, "", ""))

    def test_it_is_a_launcher_of_the_plugin(self) -> None:
        from craftsman.platforms.claude import PLUGIN_LAUNCHERS

        self.assertIn("bin/craftsman-cli.py", PLUGIN_LAUNCHERS)
        self.assertNotIn("dirname", LAUNCHER.read_text(encoding="utf-8"))


    def test_the_windows_launcher_runs_the_bootstrap(self) -> None:
        # Not run here (no Windows): its text is pinned instead. `-m craftsman.cli` would import a
        # `craftsman/` directory of the hook's working directory, the guarded project, in place of the plugin.
        text = (REPO_ROOT / "bin" / "launcher.cmd").read_text(encoding="utf-8")
        self.assertNotIn("-m craftsman", text)
        self.assertIn('set "SCRIPT_DIR=%~dp0"', text)  # the folder of the script, with its trailing backslash
        runs = [line for line in text.splitlines() if line.startswith("%PYTHON_CMD% ")]
        self.assertEqual(runs, ['%PYTHON_CMD% "%SCRIPT_DIR%craftsman-cli.py" %*'])
        self.assertEqual(text.splitlines()[-1], runs[0])


if __name__ == "__main__":
    unittest.main()
