"""Tests for the Craftsman git guard (`craftsman.guard`), called in-process."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from typing import Any
from unittest import mock

from craftsman.guard import evaluate_command, main, was_ask_question_called_since_last_push

from tests.support import run_main


def push_question(*questions: dict[str, Any]) -> dict[str, Any]:
    """An Antigravity planner step that asks the user `questions`."""
    return {
        "step_index": 1,
        "type": "PLANNER_RESPONSE",
        "tool_calls": [{"name": "ask_question", "args": {"questions": list(questions)}}],
    }


def answers(content: str) -> dict[str, Any]:
    """The Antigravity step that carries the user's answers to the question step."""
    return {"step_index": 2, "type": "GENERIC", "content": content}


class GuardTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.transcript = Path(tmp.name) / "transcript.jsonl"
        self.write_transcript({"step_index": 1, "type": "USER_INPUT"})

    def write_transcript(self, *entries: dict[str, Any]) -> None:
        self.transcript.write_text(
            "".join(json.dumps(entry) + "\n" for entry in entries), encoding="utf-8"
        )

    def decision(self, command: str) -> str:
        """The guard's decision on an Antigravity `run_command` of `command`."""
        payload = {
            "transcriptPath": str(self.transcript),
            "toolCall": {"name": "run_command", "args": {"CommandLine": command}},
        }
        return evaluate_command(payload)[0]


class RedirectionTests(GuardTestCase):
    def test_redirection_does_not_hide_a_push_to_main(self) -> None:
        for command in ("git push 2>&1 origin main", "git push &> /dev/null origin main"):
            with self.subTest(command=command):
                self.assertEqual(self.decision(command), "deny")

    def test_redirection_glued_to_a_branch_name_ends_the_branch_name(self) -> None:
        self.assertEqual(self.decision("git push origin main2>/dev/null"), "allow")


class ProtectedTargetTests(GuardTestCase):
    def test_reflog_source_pushed_to_main_is_denied(self) -> None:
        self.assertEqual(self.decision("git push origin HEAD@{0}:main"), "deny")

    def test_force_push_later_in_a_compound_command_needs_confirmation(self) -> None:
        self.write_transcript(
            push_question(
                {
                    "question": "How should we push to main?",
                    "options": ["Push directly to main (no PR)", "Create a feature branch"],
                }
            ),
            answers("Q1: How should we push to main?\nA1: Push directly to main (no PR)"),
        )
        command = "git push origin main && git push --force origin main"
        self.assertEqual(self.decision(command), "force_ask")


class PushApprovalTests(GuardTestCase):
    def test_direct_push_answer_approves_despite_rejected_in_the_question(self) -> None:
        question = "The previous hook rejected the push; how should we push to main?"
        self.write_transcript(
            push_question(
                {
                    "question": question,
                    "options": ["Push directly to main (no PR)", "Create a feature branch"],
                }
            ),
            answers(f"Q1: {question}\nA1: Push directly to main (no PR)"),
        )
        self.assertEqual(self.decision("git push origin main"), "allow")

    def test_yes_to_an_unrelated_question_does_not_approve_the_push(self) -> None:
        self.write_transcript(
            push_question(
                {"question": "Run unit tests first?", "options": ["Yes", "No"]},
                {
                    "question": "Push directly to main or open a PR?",
                    "options": ["Open a PR", "Push directly to main"],
                },
            ),
            answers(
                "Q1: Run unit tests first?\nA1: Yes\n"
                "Q2: Push directly to main or open a PR?\nA2: Open a PR"
            ),
        )
        self.assertEqual(self.decision("git push origin main"), "deny")

    def test_unreadable_transcript_approves_no_push(self) -> None:
        self.transcript.unlink()
        self.transcript.mkdir()
        with redirect_stderr(io.StringIO()) as err:
            self.assertFalse(was_ask_question_called_since_last_push(self.transcript))
        self.assertIn("cannot read the transcript", err.getvalue())


class AttributionTests(GuardTestCase):
    def test_human_co_author_with_an_ai_like_name_is_allowed(self) -> None:
        for author in (
            "Nikolai <nikolai@example.com>",
            "John Talbot <j@talbot.org>",
            "Sarah Raider <s@dubai.ae>",
        ):
            with self.subTest(author=author):
                command = f'git commit -m "feat: x\n\nCo-authored-by: {author}"'
                self.assertEqual(self.decision(command), "allow")

    @unittest.skipUnless(Path("/proc/self/mem").is_file(), "needs a file whose read fails")
    def test_message_file_that_cannot_be_read_asks_the_user(self) -> None:
        command = "git commit -F /proc/self/mem"

        self.assertEqual(self.decision(command), "force_ask")


class HookInputTests(unittest.TestCase):
    def guard(self, stdin: str, *args: str) -> tuple[int, str]:
        with mock.patch("sys.stdin", io.StringIO(stdin)):
            result = run_main(main, *args)
        return result.code, result.out

    def test_input_that_is_no_json_is_denied(self) -> None:
        code, out = self.guard("{not json")

        self.assertEqual((code, json.loads(out)["decision"]), (0, "deny"))

    def test_input_that_is_no_object_is_denied(self) -> None:
        code, out = self.guard("[1]")

        self.assertEqual((code, json.loads(out)["decision"]), (0, "deny"))

    def test_unknown_git_hook_is_a_usage_error(self) -> None:
        self.assertEqual(self.guard("", "--git-hook", "bogus")[0], 2)

    def test_commit_msg_hook_without_a_file_is_a_usage_error(self) -> None:
        self.assertEqual(self.guard("", "--git-hook", "commit-msg")[0], 2)


if __name__ == "__main__":
    unittest.main()
