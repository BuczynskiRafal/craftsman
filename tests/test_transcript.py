"""Tests for reading agent transcripts (`craftsman.transcript`)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from craftsman.transcript import (
    TranscriptError,
    finished_reviewers,
    is_waiting_on_question,
    latest_user_input,
    read_transcript_entries,
)

from tests.support import (
    REVIEWER,
    ask_question,
    invoke_subagent,
    subagent_ack,
    subagent_done,
    user_input,
)


def reviewer_run(
    spec: dict[str, Any], sub_id: str = "sub-1", *, first_step: int = 2
) -> list[dict[str, Any]]:
    return [
        invoke_subagent(first_step, spec),
        subagent_ack(first_step + 1, sub_id),
        subagent_done(first_step + 2, sub_id),
    ]


class FinishedReviewerTests(unittest.TestCase):
    def test_finished_reviewer_is_listed(self) -> None:
        self.assertEqual(finished_reviewers(reviewer_run(REVIEWER), 1), ("sub-1",))

    def test_other_roles_are_not_reviewers(self) -> None:
        roles = ("reviewer", "Craftsman Reviewer", "Craftsman Quality Gate Auditor", "Code Reviewer")
        for role in roles:
            with self.subTest(role=role):
                self.assertEqual(finished_reviewers(reviewer_run({**REVIEWER, "Role": role}), 1), ())

    def test_reviewer_role_matches_regardless_of_case(self) -> None:
        spec = {**REVIEWER, "Role": "Craftsman-Reviewer"}
        self.assertEqual(finished_reviewers(reviewer_run(spec), 1), ("sub-1",))

    def test_read_only_subagent_is_not_a_reviewer(self) -> None:
        spec = {**REVIEWER, "TypeName": "research"}
        self.assertEqual(finished_reviewers(reviewer_run(spec), 1), ())

    def test_reviewer_still_running_is_not_listed(self) -> None:
        entries = reviewer_run(REVIEWER)[:2]
        self.assertEqual(finished_reviewers(entries, 1), ())

    def test_reviewer_invoked_before_the_request_is_not_listed(self) -> None:
        self.assertEqual(finished_reviewers(reviewer_run(REVIEWER, first_step=2), 2), ())

    def test_failed_invocation_is_not_listed(self) -> None:
        entries = reviewer_run(REVIEWER)
        entries[0] = {**entries[0], "status": "ERROR"}
        self.assertEqual(finished_reviewers(entries, 1), ())

    def test_acknowledged_ids_match_the_requested_order(self) -> None:
        other = {**REVIEWER, "Role": "Explorer"}
        entries = [
            invoke_subagent(2, other, REVIEWER),
            subagent_ack(3, "sub-a", "sub-b"),
            subagent_done(4, "sub-a"),
            subagent_done(5, "sub-b"),
        ]
        self.assertEqual(finished_reviewers(entries, 1), ("sub-b",))

    def test_top_level_spec_with_json_quoted_fields_is_read(self) -> None:
        entries = reviewer_run(REVIEWER)
        quoted = {key: json.dumps(value) for key, value in REVIEWER.items()}
        entries[0] = {**entries[0], "tool_calls": [{"name": "invoke_subagent", "args": quoted}]}
        self.assertEqual(finished_reviewers(entries, 1), ("sub-1",))


class ConversationStateTests(unittest.TestCase):
    def test_unanswered_question_is_waiting(self) -> None:
        self.assertTrue(is_waiting_on_question([user_input(1, "go"), ask_question(2)]))

    def test_answered_question_is_not_waiting(self) -> None:
        entries = [ask_question(1), user_input(2, "option B")]
        self.assertFalse(is_waiting_on_question(entries))

    def test_latest_user_input_has_its_step(self) -> None:
        entries = [user_input(1, "first"), ask_question(2), user_input(7, "second")]
        self.assertEqual(latest_user_input(entries), (7, "second"))


class ReadTranscriptTests(unittest.TestCase):
    def test_claude_code_prompt_reads_as_user_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.jsonl"
            line = {"type": "user", "message": {"content": [{"type": "text", "text": "hello"}]}}
            path.write_text("not json\n" + json.dumps(line) + "\n", encoding="utf-8")
            self.assertEqual(
                read_transcript_entries(path),
                [{"step_index": 2, "type": "USER_INPUT", "content": "hello"}],
            )

    def test_missing_transcript_has_no_entries(self) -> None:
        self.assertEqual(read_transcript_entries(Path("/nonexistent/transcript.jsonl")), [])

    def test_unreadable_transcript_is_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(TranscriptError):
            read_transcript_entries(Path(tmp))


if __name__ == "__main__":
    unittest.main()
