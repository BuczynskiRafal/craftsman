"""The shared bounded JSON Lines reader and the two transcript readers built on it."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from craftsman.jsonl import JsonlLine, JsonlReader, StopReason
from craftsman.transcript import TranscriptError, parent_conversation, read_transcript_entries


class JsonlReaderTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "log.jsonl"

    def read(self, text: str | bytes, **options: object) -> tuple[list[JsonlLine], JsonlReader]:
        self.path.write_bytes(text if isinstance(text, bytes) else text.encode("utf-8"))
        reader = JsonlReader(self.path, **options)  # type: ignore[arg-type]
        return list(reader), reader

    def test_lines_carry_their_number_text_and_parsed_value(self) -> None:
        lines, reader = self.read('{"a": 1}\n\n[1, 2]\n"x"\n')
        self.assertEqual([line.number for line in lines], [1, 2, 3, 4])
        self.assertEqual([line.value for line in lines], [{"a": 1}, None, [1, 2], "x"])
        self.assertEqual([line.blank for line in lines], [False, True, False, False])
        self.assertTrue(all(line.complete for line in lines))
        self.assertIs(reader.stopped, StopReason.END)

    def test_a_line_that_is_not_json_keeps_its_error_and_reading_goes_on(self) -> None:
        lines, _ = self.read('{"a": 1}\nnot json\n{"b": 2}\n')
        self.assertIsNotNone(lines[1].error)
        self.assertIsNone(lines[1].value)
        self.assertEqual(lines[1].text, "not json")
        self.assertEqual(lines[2].value, {"b": 2})

    def test_the_last_line_without_a_newline_is_marked_incomplete(self) -> None:
        lines, _ = self.read('{"a": 1}\n{"b"')
        self.assertEqual([line.complete for line in lines], [True, False])
        self.assertIsNotNone(lines[1].error)
        lines, _ = self.read('{"a": 1}\n{"b": 2}')
        self.assertFalse(lines[1].complete)
        self.assertEqual(lines[1].value, {"b": 2})

    def test_carriage_returns_end_lines_like_the_newline_does(self) -> None:
        lines, _ = self.read(b'{"a": 1}\r\n{"b": 2}\r\n')
        self.assertEqual([line.value for line in lines], [{"a": 1}, {"b": 2}])
        self.assertTrue(all(line.complete for line in lines))

    def test_an_empty_file_has_no_lines_and_ends_normally(self) -> None:
        lines, reader = self.read("")
        self.assertEqual(lines, [])
        self.assertIs(reader.stopped, StopReason.END)

    def test_strict_decoding_raises_and_replace_does_not(self) -> None:
        data = b'{"a": "\xff"}\n'
        with self.assertRaises(UnicodeDecodeError):
            self.read(data)
        lines, _ = self.read(data, errors="replace")
        self.assertEqual(lines[0].value, {"a": "�"})

    def test_a_file_over_the_byte_cap_is_not_read(self) -> None:
        lines, reader = self.read('{"a": 1}\n', max_bytes=3)
        self.assertEqual(lines, [])
        self.assertIs(reader.stopped, StopReason.TOO_LARGE)
        lines, reader = self.read('{"a": 1}\n', max_bytes=9)
        self.assertEqual(len(lines), 1)
        self.assertIs(reader.stopped, StopReason.END)

    def test_stopping_early_reads_no_more_and_leaves_no_reason(self) -> None:
        self.path.write_text('{"a": 1}\n' * 5, encoding="utf-8")
        reader = JsonlReader(self.path)
        for line in reader:
            if line.number == 2:
                break
        self.assertIsNone(reader.stopped)

    def test_a_missing_file_raises_the_filesystem_error(self) -> None:
        with self.assertRaises(FileNotFoundError):
            list(JsonlReader(self.path))


class TranscriptReadersTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "transcript.jsonl"

    def test_a_finished_last_line_without_its_newline_is_an_entry(self) -> None:
        self.path.write_text(
            '{"step_index": 1, "type": "USER_INPUT", "content": "a"}\n'
            '{"step_index": 2, "type": "USER_INPUT", "content": "b"}',
            encoding="utf-8",
        )
        self.assertEqual([entry["content"] for entry in read_transcript_entries(self.path)], ["a", "b"])

    def test_an_unfinished_last_line_is_left_out(self) -> None:
        self.path.write_text('{"step_index": 1, "type": "USER_INPUT", "content": "a"}\n{"step_ind', encoding="utf-8")
        self.assertEqual(len(read_transcript_entries(self.path)), 1)

    def test_a_bad_complete_line_names_its_number(self) -> None:
        self.path.write_text('{"step_index": 1, "type": "USER_INPUT"}\n\nnot json\n', encoding="utf-8")
        with self.assertRaisesRegex(TranscriptError, r"transcript\.jsonl:3 is not JSON"):
            read_transcript_entries(self.path)
        self.path.write_text('{"step_index": 1, "type": "USER_INPUT"}\n[1]\n', encoding="utf-8")
        with self.assertRaisesRegex(TranscriptError, r"transcript\.jsonl:2 is not a JSON object"):
            read_transcript_entries(self.path)

    def test_a_missing_file_has_no_entries_and_an_undecodable_one_is_an_error(self) -> None:
        self.assertEqual(read_transcript_entries(self.path), [])
        self.path.write_bytes(b'{"step_index": 1, "type": "USER_INPUT", "content": "\xff"}\n')
        with self.assertRaisesRegex(TranscriptError, "cannot read the transcript"):
            read_transcript_entries(self.path)

    def test_a_transcript_over_the_size_cap_is_an_error_not_a_partial_read(self) -> None:
        self.path.write_text('{"step_index": 1, "type": "USER_INPUT", "content": "a"}\n', encoding="utf-8")
        with mock.patch("craftsman.transcript.MAX_TRANSCRIPT_BYTES", 10):
            with self.assertRaisesRegex(TranscriptError, "is larger than 0 MB; not read"):
                read_transcript_entries(self.path)

    def test_the_parent_message_counts_only_among_the_first_five_lines(self) -> None:
        filler = '{"step_index": 1, "type": "TOOL"}\n'
        message = '{"step_index": 9, "type": "SYSTEM_MESSAGE", "content": "[Message] sender=parent-1 hi"}\n'
        missing = Path("/nonexistent")
        # Blank lines count toward the position, as they did when the scan counted lines.
        self.path.write_text(filler * 3 + "\n" + message, encoding="utf-8")
        self.assertEqual(parent_conversation(missing, "c", path=self.path), "parent-1")
        self.path.write_text(filler * 4 + message, encoding="utf-8")
        self.assertEqual(parent_conversation(missing, "c", path=self.path), "parent-1")
        self.path.write_text(filler * 5 + message, encoding="utf-8")
        self.assertIsNone(parent_conversation(missing, "c", path=self.path))
        self.path.write_text(filler * 3 + "\n\n" + message, encoding="utf-8")
        self.assertIsNone(parent_conversation(missing, "c", path=self.path))

    def test_the_parent_scan_stops_at_the_first_user_input(self) -> None:
        user = '{"step_index": 1, "type": "USER_INPUT", "content": "hi"}\n'
        message = '{"step_index": 0, "type": "SYSTEM_MESSAGE", "content": "[Message] sender=parent-1 hello"}\n'
        self.path.write_text(user + message + "not json\n", encoding="utf-8")
        self.assertIsNone(parent_conversation(Path("/nonexistent"), "c", path=self.path))
        self.path.write_text("not json\n" + message + user, encoding="utf-8")
        self.assertIsNone(parent_conversation(Path("/nonexistent"), "c", path=self.path))
        self.path.write_text("not json\n" + message, encoding="utf-8")
        self.assertEqual(parent_conversation(Path("/nonexistent"), "c", path=self.path), "parent-1")


if __name__ == "__main__":
    unittest.main()
