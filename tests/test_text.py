"""Unit tests for string normalization helpers and record field classification."""

from __future__ import annotations

import inspect
import io
import json
import sys
import tempfile
import time
import unittest
from dataclasses import fields, is_dataclass
from pathlib import Path

from craftsman import records
from craftsman.text import ONE_LINE_WARNING, defang_tokens, inline_md, one_line

from tests.support import EvidenceRepo


class TestOneLine(unittest.TestCase):
    def _capture_one_line(self, text: str, *, warn: bool = True) -> tuple[str, str]:
        stderr = io.StringIO()
        old_stderr = sys.stderr
        try:
            sys.stderr = stderr
            result = one_line(text, warn=warn)
        finally:
            sys.stderr = old_stderr
        return result, stderr.getvalue()

    def test_literal_newline_replaced_with_space_and_warns(self) -> None:
        result, err = self._capture_one_line(r"Off by one\nwhen x is 0")
        self.assertEqual(result, "Off by one when x is 0")
        self.assertIn(ONE_LINE_WARNING, err)

    def test_real_newline_replaced_with_space_no_warn(self) -> None:
        result, err = self._capture_one_line("First line\nSecond line")
        self.assertEqual(result, "First line Second line")
        self.assertEqual(err, "")

    def test_real_crlf_and_tabs_replaced_no_warn(self) -> None:
        result, err = self._capture_one_line("Col1\tCol2\r\nCol3")
        self.assertEqual(result, "Col1 Col2 Col3")
        self.assertEqual(err, "")

    def test_backticks_preserve_literal_and_real_escapes(self) -> None:
        result, err = self._capture_one_line(r"`\n`")
        self.assertEqual(result, r"`\n`")
        self.assertEqual(err, "")

        result2, err2 = self._capture_one_line(r"`a\nb`")
        self.assertEqual(result2, r"`a\nb`")
        self.assertEqual(err2, "")

        result3, err3 = self._capture_one_line("`a\nb`")
        self.assertEqual(result3, "`a b`")
        self.assertEqual(err3, "")

    def test_adjacent_double_quotes_preserve_literal_escapes(self) -> None:
        result, err = self._capture_one_line(r'format="\n"')
        self.assertEqual(result, r'format="\n"')
        self.assertEqual(err, "")

        result2, err2 = self._capture_one_line(r'format="\r\n"')
        self.assertEqual(result2, r'format="\r\n"')
        self.assertEqual(err2, "")

        result3, err3 = self._capture_one_line(r'format="\t"')
        self.assertEqual(result3, r'format="\t"')
        self.assertEqual(err3, "")

    def test_adjacent_single_quotes_preserve_literal_escapes(self) -> None:
        result, err = self._capture_one_line(r"delim='\t'")
        self.assertEqual(result, r"delim='\t'")
        self.assertEqual(err, "")

        result2, err2 = self._capture_one_line(r"delim='\n'")
        self.assertEqual(result2, r"delim='\n'")
        self.assertEqual(err2, "")

    def test_apostrophe_contractions_do_not_prevent_replacement_and_warn(self) -> None:
        result, err = self._capture_one_line(r"doesn't handle 0\nreturns 1")
        self.assertEqual(result, "doesn't handle 0 returns 1")
        self.assertIn(ONE_LINE_WARNING, err)

        result2, err2 = self._capture_one_line(r"it's an issue\twith parsing")
        self.assertEqual(result2, "it's an issue with parsing")
        self.assertIn(ONE_LINE_WARNING, err2)

    def test_powershell_backtick_n_replaced_with_space_and_warns(self) -> None:
        result, err = self._capture_one_line("Heading`nDetails")
        self.assertEqual(result, "Heading Details")
        self.assertIn(ONE_LINE_WARNING, err)

    def test_powershell_backtick_n_in_quotes_preserved(self) -> None:
        result, err = self._capture_one_line('newline="`n"')
        self.assertEqual(result, 'newline="`n"')
        self.assertEqual(err, "")

    def test_string_empty_or_whitespace_after_normalization(self) -> None:
        result, err = self._capture_one_line(r"   \n\t  ")
        self.assertEqual(result, "")
        self.assertIn(ONE_LINE_WARNING, err)

        result2, err2 = self._capture_one_line("    ")
        self.assertEqual(result2, "")
        self.assertEqual(err2, "")

        result3, err3 = self._capture_one_line("")
        self.assertEqual(result3, "")
        self.assertEqual(err3, "")

    def test_warn_false_suppresses_stderr(self) -> None:
        result, err = self._capture_one_line(r"Off by one\nwhen x is 0", warn=False)
        self.assertEqual(result, "Off by one when x is 0")
        self.assertEqual(err, "")

    def test_whitespace_collapsing_and_stripping(self) -> None:
        result, _ = self._capture_one_line("  multiple   spaces   and\nlines  ", warn=False)
        self.assertEqual(result, "multiple spaces and lines")

    def test_windows_paths_preserved_without_warning(self) -> None:
        result, err = self._capture_one_line(r"src\new_parser.py:12")
        self.assertEqual(result, r"src\new_parser.py:12")
        self.assertEqual(err, "")

        result2, err2 = self._capture_one_line(r"tests\run.py")
        self.assertEqual(result2, r"tests\run.py")
        self.assertEqual(err2, "")

        result3, err3 = self._capture_one_line(r"C:\tools\review.py")
        self.assertEqual(result3, r"C:\tools\review.py")
        self.assertEqual(err3, "")

        result4, err4 = self._capture_one_line(r"C:\test\new.py")
        self.assertEqual(result4, r"C:\test\new.py")
        self.assertEqual(err4, "")

        result5, err5 = self._capture_one_line(r".\new_test.py")
        self.assertEqual(result5, r".\new_test.py")
        self.assertEqual(err5, "")

    def test_escaped_backslashes_preserved(self) -> None:
        result, err = self._capture_one_line(r"foo\\nbar")
        self.assertEqual(result, r"foo\\nbar")
        self.assertEqual(err, "")

        result2, err2 = self._capture_one_line(r"src\\new_parser.py:12")
        self.assertEqual(result2, r"src\\new_parser.py:12")
        self.assertEqual(err2, "")

    def test_nul_placeholder_safety(self) -> None:
        result, err = self._capture_one_line("hello \x000\x00 world", warn=False)
        self.assertEqual(result, "hello \x000\x00 world")
        self.assertEqual(err, "")

        result2, err2 = self._capture_one_line("test \x00999\x00 buffer `code`", warn=False)
        self.assertEqual(result2, "test \x00999\x00 buffer `code`")
        self.assertEqual(err2, "")

    def test_symmetric_backtick_spans(self) -> None:
        result, err = self._capture_one_line(r"`` `foo\nbar` ``")
        self.assertEqual(result, r"`` `foo\nbar` ``")
        self.assertEqual(err, "")

        result2, err2 = self._capture_one_line(r"``` `a\nb` and ``c\nd`` ```")
        self.assertEqual(result2, r"``` `a\nb` and ``c\nd`` ```")
        self.assertEqual(err2, "")

    def test_label_colon_before_literal_newline_is_not_a_drive_letter(self) -> None:
        for text, expected in (
            (r"Problem:\nthe parser fails", "Problem: the parser fails"),
            (r"Steps:\n1. run it", "Steps: 1. run it"),
            (r"see src/a.py:3\nthen rerun", "see src/a.py:3 then rerun"),
        ):
            with self.subTest(text=text):
                result, err = self._capture_one_line(text)
                self.assertEqual(result, expected)
                self.assertIn(ONE_LINE_WARNING, err)

    def test_long_tokens_are_normalized_in_linear_time(self) -> None:
        # A regex with nested quantifiers took minutes here; each input is ~60k characters.
        for text in ("a\\b" * 20_000, "`" * 30_000 + "x", "C:" + "\\x" * 30_000, ".a" * 30_000):
            with self.subTest(prefix=text[:8]):
                started = time.perf_counter()
                one_line(text, warn=False)
                self.assertLess(time.perf_counter() - started, 1.0)

    def test_unclosed_backtick_does_not_swallow_the_rest(self) -> None:
        result, err = self._capture_one_line(r"Off by one`nwhen x\nis 0 and `` stays")
        self.assertEqual(result, "Off by one when x is 0 and `` stays")
        self.assertIn(ONE_LINE_WARNING, err)

    def test_carriage_return_inside_backticks_normalized(self) -> None:
        result, err = self._capture_one_line("`a\rb`", warn=False)
        self.assertEqual(result, "`a b`")
        self.assertNotIn("\r", result)
        self.assertEqual(err, "")


class TestInlineMd(unittest.TestCase):
    def test_inline_md_basic_no_stderr_warning(self) -> None:
        stderr = io.StringIO()
        old_stderr = sys.stderr
        try:
            sys.stderr = stderr
            result = inline_md(r"Off by one\nwhen x is 0")
        finally:
            sys.stderr = old_stderr
        self.assertEqual(result, "Off by one when x is 0")
        self.assertEqual(stderr.getvalue(), "")

    def test_inline_md_table_escapes_pipe(self) -> None:
        self.assertEqual(inline_md("a | b\nc", table=True), r"a \| b c")

    def test_inline_md_table_does_not_double_escape_existing_pipe(self) -> None:
        self.assertEqual(inline_md(r"a \| b\nc", table=True), r"a \| b c")

    def test_inline_md_table_escapes_pipe_after_escaped_backslashes(self) -> None:
        self.assertEqual(inline_md(r"C:\\|D:\\", table=True), r"C:\\\|D:\\")
        self.assertEqual(inline_md(r"foo\\\|bar", table=True), r"foo\\\|bar")

    def test_inline_md_table_multiple_pipes(self) -> None:
        self.assertEqual(inline_md("a | b | c", table=True), r"a \| b \| c")

    def test_inline_md_table_false_preserves_pipe(self) -> None:
        self.assertEqual(inline_md("a | b\nc", table=False), "a | b c")

    def test_inline_md_empty(self) -> None:
        self.assertEqual(inline_md("", table=True), "")
        self.assertEqual(inline_md("", table=False), "")


# Single-line prose an agent writes; the ledger flattens each one with `one_line` on the way in.
PROSE_FIELDS: frozenset[str] = frozenset(
    {
        "Coverage.note",
        "Finding.title",
        "Finding.scenario",
        "Finding.suggestion",
        "Finding.verdict_evidence",
        "Finding.resolution_note",
        "RunWaiver.reason",
    }
)

# Everything else is stored byte for byte: `Run.argv` holds legitimate multi-line `python -c`
# scripts, and paths, ids and digests must round-trip unchanged.
RAW_FIELDS: frozenset[str] = frozenset(
    {
        # EntryProblem
        "EntryProblem.index",
        "EntryProblem.label",
        "EntryProblem.field",
        "EntryProblem.problem",
        # HunkRef
        "HunkRef.id",
        "HunkRef.path",
        "HunkRef.digest",
        "HunkRef.new_start",
        "HunkRef.new_len",
        "HunkRef.carried",
        "HunkRef.reopened",
        # Round
        "Round.deep",
        # QualitySignal
        "QualitySignal.id",
        "QualitySignal.kind",
        "QualitySignal.file",
        "QualitySignal.line",
        "QualitySignal.detail",
        # Round
        "Round.number",
        "Round.fingerprint",
        "Round.base_sha",
        "Round.prepared_at",
        "Round.hunks",
        "Round.packet_path",
        "Round.signals",
        "Round.scope",
        "Round.out_of_scope",
        # Coverage
        "Coverage.round",
        "Coverage.digests",
        "Coverage.recorded_at",
        # Finding
        "Finding.id",
        "Finding.round",
        "Finding.lens",
        "Finding.path",
        "Finding.line",
        "Finding.severity",
        "Finding.fingerprint",
        "Finding.verdict",
        "Finding.verdict_run",
        "Finding.resolution",
        "Finding.recorded_at",
        "Finding.updated_at",
        # RunWaiver
        "RunWaiver.waived_at",
        # Run
        "Run.id",
        "Run.kind",
        "Run.argv",
        "Run.cwd",
        "Run.exit_code",
        "Run.duration_s",
        "Run.fingerprint",
        "Run.started_at",
        "Run.output_tail",
        "Run.fingerprint_after",
        "Run.waiver",
        "Run.tests_ran",
        "Run.min_tests",
        # AgentEvent
        "AgentEvent.platform",
        "AgentEvent.event",
        "AgentEvent.agent",
        "AgentEvent.agent_id",
        "AgentEvent.at",
        "AgentEvent.usage",
        "AgentEvent.records",
        "AgentEvent.answered",
        "AgentEvent.last_step",
        # AgentUsage
        "AgentUsage.input_tokens",
        "AgentUsage.output_tokens",
        "AgentUsage.cache_read_tokens",
        "AgentUsage.cache_write_tokens",
        "AgentUsage.duration_s",
        # Ledger
        "Ledger.rounds",
        "Ledger.coverage",
        "Ledger.findings",
        "Ledger.runs",
        "Ledger.agents",
        # Schema 4: the hunt plan, slot claims and verdict events (ids, enums, timestamps and caller
        # identities; `Round.task` is the request text as given, not agent prose).
        "Coverage.hunter",
        "Coverage.by",
        "Finding.hunter",
        "Finding.by",
        "Finding.verdict_log",
        "Finding.merged_into",
        "HuntPlan.reasons",
        "HuntPlan.slots",
        "HuntPlan.waves",
        "Wave.generation",
        "Wave.planned_at",
        "Wave.considered",
        "Wave.slots",
        "Slot.id",
        "Slot.generation",
        "Slot.kind",
        "Slot.hunks",
        "Slot.lenses",
        "Slot.changed_lines",
        "Slot.trigger",
        "Slot.since",
        "Round.plan",
        "Round.platform",
        "Round.enforced",
        "Round.task",
        "Round.last_prepared_at",
        "VerdictEvent.token",
        "VerdictEvent.verdict",
        "VerdictEvent.at",
        "VerdictEvent.by",
        "VerdictEvent.merge",
        "SlotClaim.round",
        "SlotClaim.slot",
        "SlotClaim.token",
        "SlotClaim.at",
        "SlotClaim.by",
        "Ledger.claims",
    }
)


def _record_classes() -> list[type]:
    return [
        cls
        for _, cls in inspect.getmembers(records, inspect.isclass)
        if is_dataclass(cls) and cls.__module__ == records.__name__
    ]


class TestDefangTokens(unittest.TestCase):
    def test_every_shape_a_recording_command_prints_is_rewritten(self) -> None:
        for text, expected in (
            ("F9 recorded (major)", "F9 recorded [major]"),
            ("Coverage recorded (C9) for W1-1", "Coverage recorded [C9] for W1-1"),
            ("Brief issued (B2) for W1-1", "Brief issued [B2] for W1-1"),
            ("F3 verdict recorded (V7): proven", "F3 verdict recorded [V7]: proven"),
            ("recorded   (C1)", "recorded   [C1]"),
        ):
            with self.subTest(text=text):
                self.assertEqual(defang_tokens(text), expected)

    def test_an_unclosed_or_nested_shape_loses_its_opening_parenthesis_too(self) -> None:
        for text in ("see Coverage recorded (C1", "F1 recorded (a (b) c)", "recorded ((x)"):
            with self.subTest(text=text):
                self.assertNotRegex(defang_tokens(text), r"(recorded|issued)\s*\(")

    def test_line_breaks_stay_and_a_break_inside_a_shape_cannot_rejoin_it(self) -> None:
        self.assertEqual(defang_tokens("one\nF9 recorded (major)\ntwo"), "one\nF9 recorded [major]\ntwo")
        flattened = one_line(defang_tokens("F9 recorded\n(major)"), warn=False)
        self.assertNotRegex(flattened, r"recorded\s*\(")

    def test_text_without_a_shape_is_returned_as_it_is(self) -> None:
        for text in ("", "unrecorded (x)", "recorded", "call issue(1) and record(2)", "a (recorded) b"):
            with self.subTest(text=text):
                self.assertEqual(defang_tokens(text), text)


class TestRecordFieldsClassification(unittest.TestCase):
    def test_every_record_field_is_classified_exactly_once(self) -> None:
        classes = _record_classes()
        self.assertIn(records.Finding, classes)
        keys = {f"{cls.__name__}.{field.name}" for cls in classes for field in fields(cls)}
        self.assertEqual(sorted(keys - PROSE_FIELDS - RAW_FIELDS), [], "classify new record fields here")
        self.assertEqual(sorted(PROSE_FIELDS & RAW_FIELDS), [])
        self.assertEqual(sorted((PROSE_FIELDS | RAW_FIELDS) - keys), [], "drop fields that no longer exist")


class TestProseFieldsReachTheReportAsOneLine(unittest.TestCase):
    """Literal and real line breaks in every prose flag end up as one Markdown line (plan A4)."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = EvidenceRepo(Path(tmp.name) / "repo")
        self.repo.write("app.py", "def f(x):\n    return x + 1\n")

    def test_cli_and_json_input_are_stored_and_reported_on_one_line(self) -> None:
        repo = self.repo
        repo.cli("review", "prepare")
        added = repo.cli(
            "review", "add", "--lens", "intent", "--file", "app.py", "--line", "2",
            "--severity", "major", "--title", r"Off by one\nwhen x is 0",
            "--scenario", r"f(0) returns 1\nexpected 0 for the empty page",
            "--suggestion", "Return x\nunchanged",
        )
        self.assertIn(ONE_LINE_WARNING, added.stderr)
        batch = [{
            "lens": "intent", "file": "app.py", "line": 1, "severity": "minor",
            "title": "Real newline\nin a JSON title",
            "scenario": "f(None) raises TypeError\ninstead of returning None",
        }]
        repo.cli("review", "add", "--from-json", "-", stdin=json.dumps(batch))
        repo.cover_all()
        repo.cli(
            "review", "verdict", "F1", "--as", "dismissed",
            "--evidence", r"guard at app.py:1\nrejects x=0 before the call",
        )
        repo.cli(
            "review", "verdict", "F2", "--as", "dismissed",
            "--evidence", "guard at app.py:1\nrejects None before the call",
        )

        findings = {item["id"]: item for item in repo.show()["findings"]}
        self.assertEqual(findings["F1"]["title"], "Off by one when x is 0")
        self.assertEqual(findings["F1"]["suggestion"], "Return x unchanged")
        self.assertEqual(findings["F2"]["title"], "Real newline in a JSON title")
        self.assertEqual(findings["F2"]["verdict_evidence"], "guard at app.py:1 rejects None before the call")

        report = repo.cli("review", "report", "--review-only").stdout
        finding_lines = [line for line in report.splitlines() if line.startswith(("- F1 ", "- F2 "))]
        self.assertEqual(len(finding_lines), 2, report)
        for line in finding_lines:
            self.assertNotIn("\\n", line)
        self.assertIn("Off by one when x is 0", finding_lines[0] + finding_lines[1])


if __name__ == "__main__":
    unittest.main()
