"""`bin/launcher.sh`: the first Python 3.10+ runs the CLI in one start, passing everything through."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
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


if __name__ == "__main__":
    unittest.main()
