"""Tests for recognizing commands that prove nothing."""

from __future__ import annotations

import unittest

from craftsman.triviality import command_without_env, is_trivial_run

TRIVIAL = (
    ["true"],
    [":"],
    ["exit"],
    ["echo", "tests passed"],
    ["printf", "ok"],
    ["/bin/echo", "x"],
    ["env", "CI=1", "true"],
    ["env", "-i", "PATH=/bin", "echo"],
    ["env", "python3", "-I", "-c", "'ok'"],
    ["cmd", "/c", "exit", "0"],
    ["cmd", "/d", "/c", "exit", "0"],
    ["C:\\Windows\\System32\\cmd.exe", "/C", "exit 0"],
    ["python", "-c", "pass"],
    ["python3", "-B", "-c", "pass"],
    ["python3", "-u", "-c", ""],
    ["python3", "-Bc", "pass"],
    ["python3", "-W", "ignore", "-c", "pass"],
    ["python3", "-c", "0"],
    ["python", "-c", "True"],
    ["python3", "-c", "exit()"],
    ["python3", "-c", "quit(0)"],
    ["python3", "-c", "import sys; sys.exit(0)"],
    ["sh", "-c", "true && echo ok"],
    ["sh", "-e", "-c", "true"],
    ["bash", "-c", "exit 0"],
    ["bash", "-lc", ":"],
    ["bash", "-o", "pipefail", "-c", "exit 0"],
    ["powershell", "-NoProfile", "-Command", "exit 0"],
    ["pwsh", "-c", "exit 0"],
    ["pwsh.exe", "-NonInteractive", "-command", "echo ok"],
)
REAL = (
    ["pytest", "-q"],
    ["env", "CI=1", "npm", "test"],
    ["python3", "-m", "unittest"],
    ["python3", "-c", "print('ok')"],
    ["python3", "-c", "import app"],
    ["python", "-c", "import app; app.main()"],
    ["python3", "-c", "import sys; sys.exit(main())"],
    ["python3", "-c", "exit(run())"],
    ["python3", "-c", "syntax error("],
    ["python3", "-m", "pytest", "-c", "pass"],
    ["python3", "-B", "script.py", "-c", "pass"],
    ["sh", "-c", "echo start && pytest"],
    ["bash", "-c", "make test 'unbalanced"],
    ["bash", "-e", "run_tests.sh"],
    ["bash", "-lc", "pytest -q"],
    ["powershell", "-File", "test.ps1"],
    ["pwsh", "-Command", "Invoke-Pester"],
)


class TrivialRunTests(unittest.TestCase):
    def test_commands_are_classified_by_whether_they_run_code(self) -> None:
        cases = [(argv, True) for argv in TRIVIAL] + [(argv, False) for argv in REAL]
        for argv, trivial in cases:
            with self.subTest(argv=argv):
                self.assertEqual(is_trivial_run(argv), trivial)

    def test_env_option_values_are_not_taken_for_the_command(self) -> None:
        for argv in (
            ["env", "-u", "FOO", "true"],
            ["env", "-uFOO", "true"],
            ["env", "--unset", "FOO", "true"],
            ["env", "--unset=FOO", "true"],
            ["env", "-C", "/tmp", "true"],
            ["env", "--chdir=/tmp", "true"],
        ):
            with self.subTest(argv=argv):
                self.assertTrue(is_trivial_run(argv))

    def test_env_split_string_is_read_as_the_command(self) -> None:
        self.assertEqual(command_without_env(["env", "-S", "A=1 pytest -q"]), ["pytest", "-q"])

    def test_env_without_a_command_is_trivial(self) -> None:
        self.assertTrue(is_trivial_run(["env", "-u", "FOO"]))


if __name__ == "__main__":
    unittest.main()
