"""Tests for recognizing a prompt that starts a Craftsman session (`craftsman.trigger`)."""

from __future__ import annotations

import unittest

from craftsman.trigger import MAX_TASK_CHARS, detect_trigger, task_from_prompt


class DetectTriggerTests(unittest.TestCase):
    def test_leading_command_without_flag_is_auto(self) -> None:
        self.assertEqual(detect_trigger("/craftsman fix the bug"), "auto")

    def test_tier_flag_sets_the_tier(self) -> None:
        self.assertEqual(detect_trigger("/craftsman --deep fix the bug"), "deep")

    def test_ultracraft_is_deep(self) -> None:
        self.assertEqual(detect_trigger("ultracraft refactor the cart"), "deep")

    def test_unknown_flag_is_not_a_tier(self) -> None:
        self.assertEqual(detect_trigger("/craftsman --soft fix"), "auto")

    def test_imperative_inline_mention_triggers(self) -> None:
        self.assertEqual(detect_trigger("zrób to przez /craftsman"), "auto")

    def test_question_does_not_trigger(self) -> None:
        self.assertIsNone(detect_trigger("Co sądzisz o /craftsman? Tylko opinia."))

    def test_explanation_request_does_not_trigger(self) -> None:
        self.assertIsNone(detect_trigger("explain what /craftsman does"))

    def test_quoted_mention_does_not_trigger(self) -> None:
        self.assertIsNone(detect_trigger("the `/craftsman` command is documented in the README"))

    def test_path_mention_does_not_trigger(self) -> None:
        self.assertIsNone(detect_trigger("Sprawdź plik /Users/dev/craftsman/skills/craftsman/SKILL.md"))

    def test_claude_command_tag_triggers(self) -> None:
        prompt = "<command-name>/craftsman</command-name><command-args>--quick tidy</command-args>"
        self.assertEqual(detect_trigger(prompt), "quick")

    def test_rule_text_outside_the_user_request_does_not_trigger(self) -> None:
        prompt = (
            "<RULE>When `<USER_REQUEST>` contains `ultracraft`, activate Gear 3.</RULE>\n"
            "<USER_REQUEST>Fix typo in README</USER_REQUEST>"
        )
        self.assertIsNone(detect_trigger(prompt))


class TaskFromPromptTests(unittest.TestCase):
    def test_task_drops_the_trigger(self) -> None:
        self.assertEqual(task_from_prompt("/craftsman --standard  fix the bug"), "fix the bug")

    def test_task_is_one_line(self) -> None:
        self.assertEqual(task_from_prompt("ultracraft fix\nthe  bug"), "fix the bug")

    def test_task_of_a_claude_command_is_its_arguments(self) -> None:
        prompt = "<command-name>/craftsman</command-name><command-args>--deep tidy</command-args>"
        self.assertEqual(task_from_prompt(prompt), "tidy")

    def test_task_is_capped(self) -> None:
        self.assertEqual(len(task_from_prompt("ultracraft " + "x" * 500)), MAX_TASK_CHARS)


if __name__ == "__main__":
    unittest.main()
