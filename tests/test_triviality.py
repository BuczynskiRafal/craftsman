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


# Inline programs that exercise code without checking anything: trivial as a `test` run only.
UNCHECKED_PROGRAMS = (
    "import os",
    "import app",
    "import app; app.main()",
    "from app import f; f(1)",
    "print('ok')",
    "x = 1 + 1",
    "import sys; sys.exit(0)",
    "import sys; sys.exit(1)",
    "exit()",
)
# Inline programs that can fail: an assertion, a raise, a test framework or a computed exit status.
CHECKING_PROGRAMS = (
    "from app import f; assert f(1) == 2",
    "import app\nif app.f() != 2:\n    raise SystemExit('bad')",
    "raise RuntimeError('bad')",
    "import unittest; unittest.main(module='tests.test_app')",
    "import pytest; pytest.main(['-q'])",
    "from unittest import main; main()",
    "import doctest, app; doctest.testmod(app)",
    "import os.path, pytest.config",
    "__import__('pytest').main([])",
    "import importlib; importlib.import_module('unittest').main()",
    "import sys, app; sys.exit(app.main())",
    "import sys, app; sys.exit(0 if app.f() == 2 else 1)",
    "exit(run())",
    "import subprocess; subprocess.check_call(['make', 'test'])",
    "import subprocess; subprocess.check_output(['make', 'test'])",
    "import subprocess; subprocess.run(['make', 'test'], check=True)",
    "import runpy; runpy.run_path('selftest.py')",
    "import runpy; runpy.run_module('app.selftest', run_name='__main__')",
)
HEREDOC_UNCHECKED = "python3 - <<'EOF'\nimport app\napp.main()\nEOF"
HEREDOC_CHECKED = "python3 - <<'EOF'\nimport app\nassert app.f() == 2\nEOF"


class TestRunsMustCheckSomethingTests(unittest.TestCase):
    def test_an_inline_program_that_checks_nothing_is_trivial_as_a_test_run(self) -> None:
        for code in UNCHECKED_PROGRAMS:
            for python in ("python", "python3", "/usr/bin/python3.12"):
                with self.subTest(code=code, python=python):
                    self.assertTrue(is_trivial_run([python, "-c", code], "test"))

    def test_the_same_program_keeps_todays_rule_for_every_other_kind(self) -> None:
        for kind in (None, "e2e", "repro", "lint", "build", "other"):
            with self.subTest(kind=kind):
                self.assertFalse(is_trivial_run(["python3", "-c", "import app; app.main()"], kind))
                self.assertFalse(is_trivial_run(["python3", "-c", "print('ok')"], kind))
                self.assertTrue(is_trivial_run(["python3", "-c", "pass"], kind))

    def test_an_inline_program_that_can_fail_is_a_real_test_run(self) -> None:
        for code in CHECKING_PROGRAMS:
            with self.subTest(code=code):
                self.assertFalse(is_trivial_run(["python3", "-c", code], "test"))

    def test_python_that_does_not_parse_is_not_trivial(self) -> None:
        self.assertFalse(is_trivial_run(["python3", "-c", "import app; app.main("], "test"))

    def test_env_flags_and_shell_wrappers_do_not_hide_a_checkless_program(self) -> None:
        for argv in (
            ["env", "CI=1", "python3", "-c", "import app"],
            ["python3", "-B", "-c", "import app"],
            ["sh", "-c", "python3 -c 'import app'"],
            ["bash", "-lc", "python3 -c 'import app' && python3 -c 'import os'"],
        ):
            with self.subTest(argv=argv):
                self.assertTrue(is_trivial_run(argv, "test"))
        self.assertFalse(is_trivial_run(["sh", "-c", "python3 -c 'import app' && pytest -q"], "test"))
        self.assertFalse(is_trivial_run(["sh", "-c", "python3 -c 'assert 1 == 1'"], "test"))

    def test_a_test_command_that_is_not_an_inline_program_is_untouched(self) -> None:
        for argv in (
            ["pytest", "-q"],
            ["python3", "-m", "unittest"],
            ["python3", "-m", "pytest", "-c", "pass"],
            ["python3", "run_tests.py"],
            ["npm", "test"],
            ["sh", "-c", "make test"],
        ):
            with self.subTest(argv=argv):
                self.assertFalse(is_trivial_run(argv, "test"))

    def test_a_subprocess_call_that_does_not_raise_on_failure_checks_nothing(self) -> None:
        for code in (
            "import subprocess; subprocess.run(['make', 'test'])",
            "import subprocess; subprocess.run(['make', 'test'], check=False)",
            "import subprocess; subprocess.call(['make', 'test'])",
            "import os; os.run_path('x')",
        ):
            with self.subTest(code=code):
                self.assertTrue(is_trivial_run(["python3", "-c", code], "test"))

    def test_a_constant_assert_and_a_bare_framework_import_are_accepted_checks(self) -> None:
        # Decision (spec H1): the rule catches the honest mistake of a program that checks nothing, not
        # a program written to look like it checks. `assert True` and `import unittest` count.
        for code in ("assert True", "import unittest", "import pytest"):
            with self.subTest(code=code):
                self.assertFalse(is_trivial_run(["python3", "-c", code], "test"))

    def test_a_newline_separates_commands_of_an_inline_script(self) -> None:
        for kind in ("test", "e2e", None):
            with self.subTest(kind=kind):
                self.assertFalse(is_trivial_run(["bash", "-c", "python3 -c 'import os'\npytest"], kind))
                self.assertFalse(is_trivial_run(["bash", "-c", "true\nnpm test"], kind))
        self.assertTrue(is_trivial_run(["bash", "-c", "true\n\necho ok\n"], "test"))
        self.assertTrue(is_trivial_run(["bash", "-c", "python3 -c 'import os'\npython3 -c 'import sys'"], "test"))

    def test_a_shift_in_shell_arithmetic_is_not_a_heredoc(self) -> None:
        self.assertFalse(is_trivial_run(["bash", "-c", "echo $((1 << n))\nnpm test"], "test"))
        # Read as a heredoc, the shift would swallow `npm test` and leave a lone `echo "..."`.
        self.assertFalse(is_trivial_run(["bash", "-c", 'echo "$((1 << n))"\nnpm test'], "test"))
        self.assertFalse(is_trivial_run(["bash", "-c", "(( x = 1 << n ))\nnpm test"], "test"))

    def test_a_python_heredoc_without_a_check_is_trivial_as_a_test_run(self) -> None:
        self.assertTrue(is_trivial_run(["bash", "-c", HEREDOC_UNCHECKED], "test"))
        self.assertTrue(is_trivial_run(["sh", "-c", "python <<EOF\nimport os\nEOF"], "test"))
        self.assertTrue(is_trivial_run(["bash", "-c", "python3 - <<-'PY'\n\timport os\n\tPY\n"], "test"))
        self.assertFalse(is_trivial_run(["bash", "-c", HEREDOC_UNCHECKED], "e2e"))
        self.assertFalse(is_trivial_run(["bash", "-c", HEREDOC_UNCHECKED]))

    def test_a_python_heredoc_with_a_check_is_a_real_test_run(self) -> None:
        self.assertFalse(is_trivial_run(["bash", "-c", HEREDOC_CHECKED], "test"))
        self.assertFalse(
            is_trivial_run(["bash", "-c", "python3 - <<'EOF'\nimport unittest\nunittest.main()\nEOF"], "test")
        )

    def test_a_heredoc_beside_a_real_test_command_does_not_hide_it(self) -> None:
        self.assertFalse(is_trivial_run(["bash", "-c", "pytest -q && python3 - <<EOF\nimport os\nEOF"], "test"))
        self.assertFalse(is_trivial_run(["bash", "-c", "python3 - <<EOF\nimport os\nEOF\npytest -q"], "test"))
        self.assertTrue(
            is_trivial_run(["bash", "-c", "python3 - <<A\nimport os\nA\npython3 - <<B\nimport sys\nB"], "test")
        )
        self.assertFalse(
            is_trivial_run(["bash", "-c", "python3 - <<A\nimport os\nA\npython3 - <<B\nassert 1\nB"], "test")
        )

    def test_a_heredoc_fed_to_another_program_is_not_python_that_checks_nothing(self) -> None:
        self.assertFalse(is_trivial_run(["bash", "-c", "cat <<EOF\nhello\nEOF"], "test"))
        self.assertFalse(is_trivial_run(["bash", "-c", "pytest <<EOF\nimport os\nEOF"], "test"))

    def test_a_here_string_is_not_a_heredoc(self) -> None:
        self.assertFalse(is_trivial_run(["bash", "-c", "cat <<< hello"], "test"))


if __name__ == "__main__":
    unittest.main()
