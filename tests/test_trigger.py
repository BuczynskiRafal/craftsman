"""Tests for recognizing a prompt that starts a Craftsman session (`craftsman.trigger`)."""

from __future__ import annotations

import unittest

from craftsman.trigger import MAX_TASK_CHARS, detect_trigger, task_from_prompt


class DetectTriggerTests(unittest.TestCase):
    def test_empty_or_whitespace_prompt_does_not_trigger(self) -> None:
        self.assertIsNone(detect_trigger(""))
        self.assertIsNone(detect_trigger("   \n\t  "))

    def test_leading_command_without_flag_is_auto(self) -> None:
        self.assertEqual(detect_trigger("/craftsman fix the bug"), "auto")

    def test_leading_command_after_other_slash_commands_triggers(self) -> None:
        self.assertEqual(detect_trigger("/clear /craftsman --quick fix the bug"), "quick")

    def test_tier_flag_sets_the_tier(self) -> None:
        self.assertEqual(detect_trigger("/craftsman --deep fix the bug"), "deep")
        self.assertEqual(detect_trigger("/craftsman --standard fix the bug"), "standard")
        self.assertEqual(detect_trigger("/craftsman --quick fix the bug"), "quick")

    def test_ultracraft_is_deep(self) -> None:
        self.assertEqual(detect_trigger("ultracraft refactor the cart"), "deep")

    def test_unknown_flag_is_not_a_tier(self) -> None:
        self.assertEqual(detect_trigger("/craftsman --soft fix"), "auto")

    def test_bare_inline_token_triggers_even_in_questions(self) -> None:
        for prompt, expected in (
            ("please run /craftsman on the parser", "auto"),
            ("explain what /craftsman does", "auto"),
            ("what does /craftsman?", "auto"),
            ("what does ultracraft do?", "deep"),
            ("don't skip tests, run /craftsman --deep", "deep"),
            ("Bitte mit /craftsman pruefen?", "auto"),
            ("Was macht ultracraft hier?", "deep"),
            ("Hazlo con /craftsman --quick por favor", "quick"),
        ):
            with self.subTest(prompt=prompt):
                self.assertEqual(detect_trigger(prompt), expected)

    def test_backticked_or_quoted_mentions_do_not_trigger_in_any_language(self) -> None:
        for prompt in (
            "the `/craftsman` command is documented in the README",
            "double backticks ``/craftsman`` and `` `/craftsman` `` are also code spans",
            "fenced example:\n```\n/craftsman --deep\nultracraft\n```\nend",
            "what does `ultracraft` do?",
            'explain "/craftsman" and "ultracraft" modes',
            "What do you think of '/craftsman'? Just an opinion.",
            "don't run `/craftsman` yet",
            "Was meinst du zu `/craftsman`? Nur eine Meinung.",
            'Wie funktioniert "ultracraft"?',
            "Que hace '/craftsman'?",
        ):
            with self.subTest(prompt=prompt):
                self.assertIsNone(detect_trigger(prompt))

    def test_compound_tokens_or_paths_do_not_trigger(self) -> None:
        for prompt in (
            "Check file /Users/dev/craftsman/skills/craftsman/SKILL.md",
            "cd ./craftsman && make test",
            "See https://github.com/BuczynskiRafal/craftsman for details",
            "enable --ultracraft or ultracraft-mode or /craftsman-helper",
        ):
            with self.subTest(prompt=prompt):
                self.assertIsNone(detect_trigger(prompt))

    def test_claude_command_tag_triggers(self) -> None:
        prompt = "<command-name>/craftsman</command-name><command-args>--quick tidy</command-args>"
        self.assertEqual(detect_trigger(prompt), "quick")
        ultra = "<command-name>ultracraft</command-name><command-args>tidy</command-args>"
        self.assertEqual(detect_trigger(ultra), "deep")

    def test_skill_metadata_tag_triggers(self) -> None:
        prompt = (
            "<SKILL>/Users/dev/.gemini/config/skills/craftsman/SKILL.md</SKILL>\n"
            "<USER_REQUEST>Refactor the parser</USER_REQUEST>"
        )
        self.assertEqual(detect_trigger(prompt), "auto")

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
