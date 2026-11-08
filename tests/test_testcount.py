"""Tests for counting the tests a runner's output reports."""

from __future__ import annotations

import unittest

from craftsman.testcount import MAX_LINE_CHARS, RunnerOutputCounter, count_tests

PYTEST_OK = """\
============================= test session starts ==============================
platform linux -- Python 3.12.1, pytest-8.1.1, pluggy-1.4.0
rootdir: /work/app
collected 7 items

tests/test_app.py .......                                                [100%]

============================== 7 passed in 0.12s ===============================
"""
PYTEST_MIXED = """\
collected 12 items

tests/test_a.py ....F.s..xX.

=========================== short test summary info ============================
FAILED tests/test_a.py::test_x - assert 1 == 2
==== 1 failed, 8 passed, 1 skipped, 1 xfailed, 1 xpassed, 2 warnings in 3.45s ====
"""
PYTEST_QUIET = "..........\n10 passed in 0.31s\n"
PYTEST_TIMED = "== 4 passed in 61.02s (0:01:01) ==\n"
PYTEST_NONE = """\
============================= test session starts ==============================
collected 0 items

============================ no tests ran in 0.01s =============================
"""
PYTEST_ALL_SKIPPED = "collected 3 items\n\ns.s\n\n======================== 3 skipped in 0.02s ========================\n"
UNITTEST_OK = """\
..........
----------------------------------------------------------------------
Ran 10 tests in 0.004s

OK
"""
UNITTEST_SKIPPED = """\
...ss
----------------------------------------------------------------------
Ran 5 tests in 0.004s

OK (skipped=2)
"""
UNITTEST_FAILED = """\
F.
======================================================================
FAIL: test_x (tests.test_a.A.test_x)
----------------------------------------------------------------------
Ran 2 tests in 0.002s

FAILED (failures=1, skipped=1)
"""
UNITTEST_NONE = """\
----------------------------------------------------------------------
Ran 0 tests in 0.000s

NO TESTS RAN
"""
GO_OK = """\
ok  \tgithub.com/acme/app/cart\t0.004s
ok  \tgithub.com/acme/app/db\t(cached)
?   \tgithub.com/acme/app/cmd\t[no test files]
"""
GO_NO_FILES = "?   \tgithub.com/acme/app\t[no test files]\n"
GO_NO_TESTS_TO_RUN = "ok  \tgithub.com/acme/app\t0.002s [no tests to run]\n"
GO_VERBOSE = """\
=== RUN   TestAdd
--- PASS: TestAdd (0.00s)
=== RUN   TestSub
=== RUN   TestSub/negative
    --- PASS: TestSub/negative (0.00s)
--- PASS: TestSub (0.00s)
PASS
ok  \tgithub.com/acme/app\t0.003s
"""
GO_VERBOSE_ALL_SKIPPED = "=== RUN   TestA\n--- SKIP: TestA (0.00s)\nPASS\nok  \tpkg\t0.002s\n"
GO_VERBOSE_SOME_SKIPPED = (
    "=== RUN   TestA\n--- SKIP: TestA (0.00s)\n=== RUN   TestB\n--- PASS: TestB (0.00s)\nPASS\nok  \tpkg\t0.002s\n"
)
PYTEST_COLLECT_ONLY = "tests/a.py::test_a\ntests/a.py::test_b\n\n2 tests collected in 0.01s\n"
PYTEST_COLLECT_ONLY_VERBOSE = "<Function test_a>\n\n=========================== 2 tests collected in 0.01s ===========================\n"
PYTEST_COLLECT_ONLY_DESELECTED = "1/3 tests collected (2 deselected) in 0.01s\n"
PYTEST_COLLECT_ONLY_NONE = "no tests collected (3 deselected) in 0.01s\n"
PHPUNIT_WARNINGS = "OK, but there were issues!\nTests: 5, Assertions: 5, Warnings: 1.\n"
PHPUNIT_SKIPPED = "OK, but some tests were skipped!\nTests: 5, Assertions: 4, Skipped: 1.\n"
JEST_ALL_SKIPPED = "Tests:       2 skipped, 2 total\n"
VITEST_ALL_SKIPPED = " Test Files  1 skipped (1)\n      Tests  2 skipped (2)\n"
GO_FAIL = "--- FAIL: TestAdd (0.00s)\nFAIL\nFAIL\tgithub.com/acme/app\t0.003s\n"
CARGO_OK = """\
running 5 tests
test a ... ok
test result: ok. 5 passed; 0 failed; 1 ignored; 0 measured; 0 filtered out; finished in 0.00s

running 2 tests
test result: ok. 2 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.00s

   Doc-tests app
running 0 tests
test result: ok. 0 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.00s
"""
CARGO_NONE = "running 0 tests\n\ntest result: ok. 0 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out\n"
JEST_OK = """\
Test Suites: 2 passed, 2 total
Tests:       1 skipped, 7 passed, 8 total
Snapshots:   0 total
Time:        1.2 s
"""
JEST_FAILED = "Tests:       2 failed, 5 passed, 7 total\n"
JEST_NONE = "No tests found, exiting with code 0\n"
VITEST_OK = """\
 Test Files  2 passed (2)
      Tests  9 passed | 1 skipped (10)
   Duration  1.02s
"""
VITEST_FAILED = " Test Files  1 failed (1)\n      Tests  1 failed | 3 passed (4)\n"
VITEST_NONE = "No test files found, exiting with code 0\n"
MOCHA_OK = """\
  Calculator
    ✔ adds

  12 passing (30ms)
  1 pending
"""
MOCHA_FAILED = "  3 passing (12ms)\n  2 failing\n"


class CountTestsTests(unittest.TestCase):
    def test_each_runner_is_read_from_its_own_output(self) -> None:
        cases = {
            "pytest": (PYTEST_OK, 7),
            "pytest with every outcome": (PYTEST_MIXED, 11),
            "pytest -q": (PYTEST_QUIET, 10),
            "pytest with a timer": (PYTEST_TIMED, 4),
            "pytest collected nothing": (PYTEST_NONE, 0),
            "pytest skipped everything": (PYTEST_ALL_SKIPPED, 0),
            "unittest": (UNITTEST_OK, 10),
            "unittest with skips": (UNITTEST_SKIPPED, 3),
            "unittest failing with skips": (UNITTEST_FAILED, 1),
            "unittest ran nothing": (UNITTEST_NONE, 0),
            "go test packages": (GO_OK, 2),
            "go test without test files": (GO_NO_FILES, 0),
            "go test no tests to run": (GO_NO_TESTS_TO_RUN, 0),
            "go test -v": (GO_VERBOSE, 3),
            "go test failing": (GO_FAIL, 1),
            "go test -v all skipped": (GO_VERBOSE_ALL_SKIPPED, 0),
            "go test -v some skipped": (GO_VERBOSE_SOME_SKIPPED, 1),
            "pytest --collect-only -q": (PYTEST_COLLECT_ONLY, 0),
            "pytest --collect-only": (PYTEST_COLLECT_ONLY_VERBOSE, 0),
            "pytest --collect-only with deselected": (PYTEST_COLLECT_ONLY_DESELECTED, 0),
            "pytest --collect-only none": (PYTEST_COLLECT_ONLY_NONE, 0),
            "jest all skipped": (JEST_ALL_SKIPPED, 0),
            "vitest all skipped": (VITEST_ALL_SKIPPED, 0),
            "cargo test (three binaries)": (CARGO_OK, 7),
            "cargo test none": (CARGO_NONE, 0),
            "jest": (JEST_OK, 7),
            "jest failing": (JEST_FAILED, 7),
            "jest none": (JEST_NONE, 0),
            "vitest": (VITEST_OK, 9),
            "vitest failing": (VITEST_FAILED, 4),
            "vitest none": (VITEST_NONE, 0),
            "mocha": (MOCHA_OK, 12),
            "mocha failing": (MOCHA_FAILED, 5),
        }
        for name, (output, expected) in cases.items():
            with self.subTest(name):
                self.assertEqual(count_tests(output), expected)

    def test_output_no_runner_claims_is_unknown_not_zero(self) -> None:
        for output in (PHPUNIT_WARNINGS, PHPUNIT_SKIPPED, "", "hello\n", "all good\nbuild ok\n", "ok\n", "Ran quickly\n", "5 apples in 3s\n"):
            with self.subTest(output=output):
                self.assertIsNone(count_tests(output))

    def test_several_summaries_add_up(self) -> None:
        self.assertEqual(count_tests(PYTEST_OK + UNITTEST_OK + JEST_OK + MOCHA_OK), 7 + 10 + 7 + 12)

    def test_color_codes_around_a_summary_are_ignored(self) -> None:
        self.assertEqual(count_tests("\x1b[1m\x1b[32m============ 3 passed\x1b[0m\x1b[32m in 0.01s ============\x1b[0m\n"), 3)

    def test_windows_line_endings_and_a_last_line_without_newline(self) -> None:
        self.assertEqual(count_tests("Ran 4 tests in 0.1s\r\n\r\nOK\r\n"), 4)
        self.assertEqual(count_tests("9 passed in 0.01s"), 9)

    def test_a_summary_split_over_chunks_is_still_read(self) -> None:
        counter = RunnerOutputCounter()
        text = ("noise\n" * 3 + PYTEST_OK).encode()
        for index in range(0, len(text), 7):
            counter.feed(text[index : index + 7])
        self.assertEqual(counter.result(), 7)

    def test_a_multibyte_character_split_over_chunks_does_not_break_the_next_line(self) -> None:
        counter = RunnerOutputCounter()
        data = "✔ ok é\n12 passing (3ms)\n".encode()
        for byte in data:
            counter.feed(bytes([byte]))
        self.assertEqual(counter.result(), 12)

    def test_a_summary_that_scrolled_out_of_the_tail_is_still_counted(self) -> None:
        counter = RunnerOutputCounter()
        counter.feed(b"======== 6 passed in 1.00s ========\n")
        for _ in range(50):
            counter.feed(b"x" * 4096 + b"\n")
        self.assertEqual(counter.result(), 6)

    def test_an_overlong_line_is_skipped_up_to_its_newline(self) -> None:
        counter = RunnerOutputCounter()
        counter.feed(b"1 passed in 0.1s" + b"x" * (MAX_LINE_CHARS + 10))
        counter.feed(b"x" * 100 + b" 5 passed in 0.1s\n")
        counter.feed(b"2 passed in 0.1s\n")
        self.assertEqual(counter.result(), 2)

    def test_result_can_be_read_twice(self) -> None:
        counter = RunnerOutputCounter()
        counter.feed(b"Ran 3 tests in 0.1s\n")
        self.assertEqual((counter.result(), counter.result()), (3, 3))


if __name__ == "__main__":
    unittest.main()
