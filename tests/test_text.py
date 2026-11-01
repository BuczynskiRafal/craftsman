"""Unit tests for string normalization helpers and record field classification."""

from __future__ import annotations

import io
import sys
import unittest
from dataclasses import fields

from craftsman.records import (
    PROSE_RECORD_FIELDS,
    RAW_RECORD_FIELDS,
    RECORD_CLASSES,
)
from craftsman.text import ONE_LINE_WARNING, inline_md, one_line


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


class TestRecordFieldsClassification(unittest.TestCase):
    def test_all_dataclass_fields_classified(self) -> None:
        """Every field of every record dataclass must be in PROSE or RAW, never unclassified."""
        self.assertTrue(bool(RECORD_CLASSES), "RECORD_CLASSES must not be empty")
        unclassified: list[str] = []

        for cls in RECORD_CLASSES:
            for field in fields(cls):
                key = f"{cls.__name__}.{field.name}"
                in_prose = key in PROSE_RECORD_FIELDS
                in_raw = key in RAW_RECORD_FIELDS
                if not (in_prose or in_raw):
                    unclassified.append(key)

        self.assertEqual(
            unclassified,
            [],
            f"New record fields must be classified in PROSE_RECORD_FIELDS or RAW_RECORD_FIELDS: {unclassified}",
        )

    def test_prose_and_raw_are_disjoint(self) -> None:
        intersection = PROSE_RECORD_FIELDS & RAW_RECORD_FIELDS
        self.assertEqual(
            intersection,
            set(),
            f"Fields cannot be both prose and raw: {sorted(intersection)}",
        )

    def test_expected_prose_fields_present(self) -> None:
        expected = {
            "Finding.title",
            "Finding.scenario",
            "Finding.suggestion",
            "Finding.verdict_evidence",
            "Finding.resolution_note",
            "Coverage.note",
            "SignalAck.reason",
            "RunWaiver.reason",
            "LensAddition.reason",
        }
        self.assertEqual(PROSE_RECORD_FIELDS, expected)

    def test_expected_raw_fields_present(self) -> None:
        raw_critical = {
            "Run.argv",
            "Run.cwd",
            "Run.output_tail",
            "HunkRef.path",
            "HunkRef.digest",
            "Round.base_sha",
            "Round.out_of_scope",
        }
        for field in raw_critical:
            self.assertIn(field, RAW_RECORD_FIELDS)


if __name__ == "__main__":
    unittest.main()
