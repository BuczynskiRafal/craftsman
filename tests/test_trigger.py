"""Tests for recognizing a prompt that starts a Craftsman session (`craftsman.trigger`)."""

from __future__ import annotations

import unittest

from craftsman.trigger import (
    MAX_TASK_CHARS,
    detect_trigger,
    is_review_prompt,
    review_request,
    task_from_prompt,
)


class DetectTriggerTests(unittest.TestCase):
    def test_empty_or_whitespace_prompt_does_not_trigger(self) -> None:
        self.assertIsNone(detect_trigger(""))
        self.assertIsNone(detect_trigger("   \n\t  "))

    def test_leading_command_without_flag_is_not_deep(self) -> None:
        self.assertIs(detect_trigger("/craftsman fix the bug"), False)

    def test_leading_command_after_other_slash_commands_triggers(self) -> None:
        self.assertIs(detect_trigger("/clear /craftsman --quick fix the bug"), False)

    def test_only_the_deep_flag_makes_the_gate_deep(self) -> None:
        self.assertIs(detect_trigger("/craftsman --deep fix the bug"), True)
        self.assertIs(detect_trigger("/craftsman --standard fix the bug"), False)
        self.assertIs(detect_trigger("/craftsman --quick fix the bug"), False)

    def test_ultracraft_is_deep(self) -> None:
        self.assertIs(detect_trigger("ultracraft refactor the cart"), True)

    def test_ultracraft_anywhere_after_the_command_is_deep(self) -> None:
        for prompt in (
            "/craftsman fix the bug, ultracraft",
            "fix the bug and /craftsman, then ultracraft it",
        ):
            with self.subTest(prompt=prompt):
                self.assertIs(detect_trigger(prompt), True)

    def test_deep_dive_is_not_the_deep_flag(self) -> None:
        self.assertIs(detect_trigger("/craftsman --deep-dive into the cache"), False)
        self.assertEqual(task_from_prompt("/craftsman --deep-dive into the cache"), "--deep-dive into the cache")

    def test_the_review_command_never_starts_a_session(self) -> None:
        for prompt in (
            "/craft-review ultracraft review PR 42",
            "/code-review ultracraft",
            "/craftsman:craft-review ultracraft",
            "/clear /craft-review ultracraft",
            "<command-name>/craft-review</command-name><command-args>ultracraft PR 42</command-args>",
        ):
            with self.subTest(prompt=prompt):
                self.assertIsNone(detect_trigger(prompt))

    def test_only_craft_review_is_the_guarded_review_command(self) -> None:
        for prompt in ("/craft-review the cart", "/craftsman:craft-review", "<command-name>/craft-review</command-name>"):
            with self.subTest(prompt=prompt):
                self.assertTrue(is_review_prompt(prompt))
        for prompt in ("/code-review max", "/clear /code-review", "<command-name>/code-review</command-name>"):
            with self.subTest(prompt=prompt):
                self.assertFalse(is_review_prompt(prompt))
                self.assertIsNone(detect_trigger(prompt))  # the host's command is not ours either

    def test_unknown_flag_is_not_deep(self) -> None:
        self.assertIs(detect_trigger("/craftsman --soft fix"), False)

    def test_bare_inline_token_triggers_even_in_questions(self) -> None:
        for prompt, expected in (
            ("please run /craftsman on the parser", False),
            ("explain what /craftsman does", False),
            ("what does /craftsman?", False),
            ("what does ultracraft do?", True),
            ("don't skip tests, run /craftsman --deep", True),
            ("Bitte mit /craftsman pruefen?", False),
            ("Was macht ultracraft hier?", True),
            ("Hazlo con /craftsman --quick por favor", False),
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
        self.assertIs(detect_trigger(prompt), False)
        ultra = "<command-name>ultracraft</command-name><command-args>tidy</command-args>"
        self.assertIs(detect_trigger(ultra), True)

    def test_skill_metadata_tag_triggers(self) -> None:
        prompt = (
            "<SKILL>/Users/dev/.gemini/config/skills/craftsman/SKILL.md</SKILL>\n"
            "<USER_REQUEST>Refactor the parser</USER_REQUEST>"
        )
        self.assertIs(detect_trigger(prompt), False)

    def test_namespaced_triggers(self) -> None:
        # Claude Code command tags
        self.assertEqual(
            detect_trigger("<command-name>craftsman:craftsman</command-name><command-args>fix</command-args>"),
            False,
        )
        self.assertEqual(
            detect_trigger(
                "<command-name>/craftsman:craftsman</command-name><command-args>--deep fix</command-args>"
            ),
            True,
        )
        self.assertEqual(
            detect_trigger(
                "<command-name>craftsman:ultracraft</command-name><command-args>fix</command-args>"
            ),
            True,
        )
        self.assertEqual(
            detect_trigger(
                "<command-name>/craftsman:ultracraft</command-name><command-args>fix</command-args>"
            ),
            True,
        )
        self.assertEqual(
            detect_trigger(
                "<command-name>/Craftsman:Craftsman</command-name><command-args>fix</command-args>"
            ),
            False,
        )

        # Prompt text triggers
        self.assertIs(detect_trigger("/craftsman:craftsman --quick test"), False)
        self.assertIs(detect_trigger("craftsman:craftsman --standard test"), False)
        self.assertIs(detect_trigger("/craftsman:craftsman test"), False)
        self.assertIs(detect_trigger("craftsman:craftsman test"), False)
        self.assertIs(detect_trigger("/craftsman:ultracraft test"), True)
        self.assertIs(detect_trigger("craftsman:ultracraft test"), True)

        # Quoted spans do not trigger
        self.assertIsNone(detect_trigger('what does "/craftsman:craftsman" do?'))
        self.assertIsNone(detect_trigger('what does `craftsman:ultracraft` do?'))
        self.assertIsNone(detect_trigger("do not run '/craftsman:craftsman'"))

    def test_colon_after_the_command_ends_it_unless_another_command_follows(self) -> None:
        self.assertIs(detect_trigger("/craftsman: add pagination"), False)
        self.assertIs(detect_trigger("please run /craftsman: now"), False)
        self.assertIs(detect_trigger("ultracraft: harden the parser"), True)
        self.assertIsNone(detect_trigger("/craftsman:craft-review fix the parser"))
        self.assertIsNone(detect_trigger("run /craftsman:simplify on it"))


class TaskFromPromptTests(unittest.TestCase):
    def test_task_drops_the_trigger(self) -> None:
        self.assertEqual(task_from_prompt("/craftsman --standard  fix the bug"), "fix the bug")
        self.assertEqual(
            task_from_prompt("/craftsman:craftsman --standard fix the auth bug"),
            "fix the auth bug",
        )
        self.assertEqual(
            task_from_prompt("craftsman:ultracraft fix the auth bug"),
            "fix the auth bug",
        )

    def test_task_keeps_no_colon_of_the_trigger(self) -> None:
        for prompt in (
            "/craftsman:ultracraft fix the bug",
            "/craftsman: fix the bug",
            "ultracraft: fix the bug",
            "/craftsman --quick: fix the bug",
        ):
            with self.subTest(prompt=prompt):
                self.assertEqual(task_from_prompt(prompt), "fix the bug")

    def test_old_flags_are_ignored_and_stripped_from_the_task(self) -> None:
        self.assertIs(detect_trigger("/craftsman --quick --deep fix it"), True)
        self.assertEqual(task_from_prompt("/craftsman --quick --deep fix it"), "fix it")
        self.assertEqual(task_from_prompt("/craftsman --standard fix it"), "fix it")

    def test_task_keeps_other_craftsman_commands(self) -> None:
        self.assertEqual(
            task_from_prompt("/craftsman fix it, then /craftsman:craft-review it"),
            "fix it, then /craftsman:craft-review it",
        )

    def test_task_is_one_line(self) -> None:
        self.assertEqual(task_from_prompt("ultracraft fix\nthe  bug"), "fix the bug")

    def test_task_of_a_claude_command_is_its_arguments(self) -> None:
        prompt = "<command-name>/craftsman</command-name><command-args>--deep tidy</command-args>"
        self.assertEqual(task_from_prompt(prompt), "tidy")
        namespaced = (
            "<command-name>craftsman:craftsman</command-name><command-args>--quick tidy</command-args>"
        )
        self.assertEqual(task_from_prompt(namespaced), "tidy")

    def test_task_is_capped(self) -> None:
        self.assertEqual(len(task_from_prompt("ultracraft " + "x" * 500)), MAX_TASK_CHARS)


class ReviewRequestTests(unittest.TestCase):
    def test_a_plain_request_is_normal_depth_and_its_text_is_the_task(self) -> None:
        for prompt in ("/craft-review the cart", "/craftsman:craft-review the cart", "/clear /craft-review the cart"):
            with self.subTest(prompt=prompt):
                self.assertEqual(review_request(prompt), (False, "the cart"))

    def test_ultracraft_or_the_deep_flag_asks_for_a_deep_review(self) -> None:
        cases = {
            "/craft-review ultracraft the cart": "the cart",
            "/craft-review --deep the cart": "the cart",
            "/craftsman:craft-review the cart --deep": "the cart",
            "/craft-review review the cart with ultracraft": "review the cart with",
        }
        for prompt, task in cases.items():
            with self.subTest(prompt=prompt):
                self.assertEqual(review_request(prompt), (True, task))

    def test_quoted_mentions_do_not_ask_for_depth(self) -> None:
        for prompt in ("/craft-review explain `--deep` in the docs", '/craft-review the "ultracraft" flag'):
            with self.subTest(prompt=prompt):
                self.assertFalse(review_request(prompt)[0])

    def test_a_claude_command_tag_carries_its_arguments(self) -> None:
        tagged = "<command-name>/craftsman:craft-review</command-name><command-args>--deep tidy up</command-args>"
        self.assertEqual(review_request(tagged), (True, "tidy up"))
        bare = "<command-name>/craftsman:craft-review</command-name>"
        self.assertEqual(review_request(bare), (False, ""))

    def test_the_task_is_one_capped_line(self) -> None:
        self.assertEqual(review_request("/craft-review fix\n the  bug"), (False, "fix the bug"))
        self.assertEqual(len(review_request("/craft-review " + "x" * 500)[1]), MAX_TASK_CHARS)

    def test_a_flag_that_only_looks_like_the_deep_flag_is_no_flag(self) -> None:
        self.assertFalse(review_request("/craft-review --deeper --deep-dive the cart")[0])


if __name__ == "__main__":
    unittest.main()

