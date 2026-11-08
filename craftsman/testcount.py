"""Count the tests a test run reports, so a run that ran none cannot pass as verification.

`RunnerOutputCounter` reads the output of a run as it streams by, line by line, and recognizes the summary
lines of pytest, unittest, `go test`, `cargo test`, jest, vitest and mocha. Summaries add up when
several appear (a workspace of crates, a Makefile that runs two runners). Output no runner claims
gives None: the run is judged by its exit code alone, as before.

The counts are what the runners print, never a promise: a test that prints another runner's summary
line can inflate them. The exit code and the work tree fingerprint stay the proof; this only catches
the honest mistake of a command that matched no tests. `go test -list` prints test names and an `ok`
line, which reads as one package that ran: use `--min-tests 0` for such a command.
"""

from __future__ import annotations

import codecs
import re

# A line longer than this is not a summary; the rest of it is dropped instead of buffered.
MAX_LINE_CHARS = 2000

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")

# `=== 2 failed, 5 passed, 1 skipped in 0.12s ===`, `5 passed in 0.01s (0:00:00)`, `no tests ran in 0.01s`
_PYTEST_RE = re.compile(
    r"^[= ]*(?P<counts>\d+ [a-z]+(?:, \d+ [a-z]+)*|no tests ran)"
    r" in [\d.]+s(?: \(\d+:\d{2}:\d{2}\))?[= ]*$"
)
_PYTEST_COUNT_RE = re.compile(r"(\d+) (passed|failed|xfailed|xpassed|skipped|deselected|errors?|warnings?|rerun)")
_PYTEST_RAN = frozenset({"passed", "failed", "xfailed", "xpassed"})
_PYTEST_ZERO_RE = re.compile(r"^[= ]*collected 0 items\b")
# `pytest --collect-only` runs nothing: `=== 2 tests collected in 0.01s ===`, `no tests collected (1 deselected) in 0.01s`
_PYTEST_COLLECTED_RE = re.compile(
    r"^[= ]*(?:\d+/)?(?:\d+|no) tests? collected(?: \(\d+ deselected\))? in [\d.]+s[= ]*$"
)
_UNITTEST_RAN_RE = re.compile(r"^Ran (\d+) tests? in [\d.]+s$")
_UNITTEST_RESULT_RE = re.compile(r"^(?:OK|FAILED) \(.*\bskipped=(\d+)")
_GO_OK_RE = re.compile(r"^ok\s+\S+\s+(?:\d+(?:\.\d+)?s|\(cached\))(?P<rest>.*)$")
_GO_NO_TESTS_RE = re.compile(r"^\?\s+\S+\s+\[no test files\]$|\[no tests to run\]")
_GO_RESULT_RE = re.compile(r"^\s*--- (?P<outcome>PASS|FAIL|SKIP):")
_GO_RUN_RE = re.compile(r"^\s*=== (?:RUN|CONT|PAUSE)\b")
_CARGO_RE = re.compile(r"^test result: \w+\. (\d+) passed; (\d+) failed;")
_JEST_RE = re.compile(r"^\s*Tests:\s+(?P<counts>.+)$")
_VITEST_RE = re.compile(r"^\s*Tests\s{2,}(?P<counts>.+)$")
_JS_COUNT_RE = re.compile(r"(\d+) (passed|failed|skipped|todo|total)")
_JS_RAN = frozenset({"passed", "failed"})
_JS_NONE_RE = re.compile(r"^No tests? (?:found|files? found)\b")
_MOCHA_RE = re.compile(r"^\s*(\d+) (passing|failing)\b")


class RunnerOutputCounter:
    """Feeds on output and says how many tests the runners in it reported."""

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._line = ""
        self._skipping = False  # inside a line longer than MAX_LINE_CHARS, up to its newline
        self._total: int | None = None
        self._unittest_last = 0  # tests of the latest `Ran N tests` line that a skip count can still reduce
        self._go_verbose = 0
        self._go_verbose_seen = False  # `go test -v` prints `=== RUN`: the `ok` lines then count no package
        self._go_packages = 0

    def feed(self, chunk: bytes) -> None:
        """Take the next bytes of the output."""
        *lines, self._line = (self._line + self._decoder.decode(chunk)).split("\n")
        for line in lines:
            if self._skipping:
                self._skipping = False
            else:
                self._read_line(line)
        if len(self._line) > MAX_LINE_CHARS:
            self._line, self._skipping = "", True

    def result(self) -> int | None:
        """The tests reported by the whole output; None when no runner summary was recognized.

        Call it once the output has ended: it reads a last line that has no newline.
        """
        if self._line and not self._skipping:
            self._read_line(self._line)
        self._line = ""
        # `go test` prints a count only with -v; without it each package that passed counts as one.
        go = self._go_verbose or (0 if self._go_verbose_seen else self._go_packages)
        seen = self._total is not None or go or self._go_verbose_seen
        return (self._total or 0) + go if seen else None

    def _read_line(self, raw: str) -> None:
        line = _ANSI_RE.sub("", raw[:MAX_LINE_CHARS]).rstrip()
        if not line:
            return
        for read in (
            self._read_pytest,
            self._read_unittest,
            self._read_go,
            self._read_cargo,
            self._read_javascript,
        ):
            if read(line):
                return

    def _add(self, count: int) -> None:
        self._total = (self._total or 0) + count

    def _read_pytest(self, line: str) -> bool:
        if _PYTEST_ZERO_RE.match(line):
            self._add(0)
            return True
        if _PYTEST_COLLECTED_RE.match(line):
            self._add(0)
            return True
        match = _PYTEST_RE.match(line)
        if match is None:
            return False
        counts = match["counts"]
        if counts == "no tests ran":
            self._add(0)
            return True
        found = _PYTEST_COUNT_RE.findall(counts)
        if not found:
            return False
        self._add(sum(int(number) for number, word in found if word in _PYTEST_RAN))
        return True

    def _read_unittest(self, line: str) -> bool:
        ran = _UNITTEST_RAN_RE.match(line)
        if ran is not None:
            self._unittest_last = int(ran[1])
            self._add(self._unittest_last)
            return True
        result = _UNITTEST_RESULT_RE.match(line)
        if result is not None and self._unittest_last:
            skipped = min(int(result[1]), self._unittest_last)
            self._unittest_last = 0
            self._add(-skipped)
            return True
        return False

    def _read_go(self, line: str) -> bool:
        result = _GO_RESULT_RE.match(line)
        if result is not None:
            self._go_verbose_seen = True
            self._go_verbose += result["outcome"] != "SKIP"
            return True
        if _GO_RUN_RE.match(line):
            self._go_verbose_seen = True
            return True
        if _GO_NO_TESTS_RE.search(line):
            self._add(0)
            return True
        if _GO_OK_RE.match(line):
            self._go_packages += 1
            return True
        return False

    def _read_cargo(self, line: str) -> bool:
        match = _CARGO_RE.match(line)
        if match is None:
            return False
        self._add(int(match[1]) + int(match[2]))
        return True

    def _read_javascript(self, line: str) -> bool:
        summary = _JEST_RE.match(line) or _VITEST_RE.match(line)
        if summary is not None:
            # PHPUnit also prints `Tests: 5, Assertions: 5.`: a line with no `N passed|failed|skipped|todo|total` is not ours
            found = _JS_COUNT_RE.findall(summary["counts"])
            if not found:
                return False
            self._add(sum(int(number) for number, word in found if word in _JS_RAN))
            return True
        if _JS_NONE_RE.match(line):
            self._add(0)
            return True
        mocha = _MOCHA_RE.match(line)
        if mocha is not None:
            self._add(int(mocha[1]))
            return True
        return False


def count_tests(output: str) -> int | None:
    """The number of tests the runners in `output` reported; None when none was recognized."""
    counter = RunnerOutputCounter()
    counter.feed(output.encode("utf-8", errors="replace"))
    return counter.result()
