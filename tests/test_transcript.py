"""Tests for reading agent transcripts (`craftsman.transcript`)."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from craftsman.records import format_timestamp
from craftsman.transcript import (
    SubagentLaunch,
    TranscriptError,
    cut_tool_calls_step,
    decode_arg,
    has_cut_tool_calls,
    has_cut_user_input,
    is_waiting_on_question,
    latest_user_input,
    latest_user_input_text,
    launch_role,
    parent_conversation,
    read_conversation,
    read_transcript_entries,
    request_body,
    review_request,
    subagent_finish,
    subagent_launches,
    subagent_role,
)

from tests.support import (
    AGY_EPOCH,
    AgyApp,
    agy_call,
    agy_time,
    ask_question,
    cut_user_input,
    generic,
    invoke_call,
    launch_reviewers,
    long_user_input,
    parent_message,
    planner,
    subagent_ack,
    user_input,
)

MAIN = "main-1"


class DecodeArgTests(unittest.TestCase):
    def test_both_encodings_read_the_same(self) -> None:
        self.assertEqual(decode_arg('"self"'), "self")  # transcript.jsonl: the value is JSON text
        self.assertEqual(decode_arg("self"), "self")  # transcript_full.jsonl: the native string

    def test_a_value_that_only_looks_quoted_is_kept(self) -> None:
        self.assertEqual(decode_arg('"unterminated'), '"unterminated')
        self.assertEqual(decode_arg('"not "json" at all"'), '"not "json" at all"')

    def test_values_that_are_no_text(self) -> None:
        self.assertEqual((decode_arg(None), decode_arg(3), decode_arg("")), ("", "3", ""))

    def test_an_encoded_non_string_stays_as_it_is(self) -> None:
        self.assertEqual(decode_arg("[1]"), "[1]")


class LaunchRoleTests(unittest.TestCase):
    def test_roles_are_read_whatever_their_spelling(self) -> None:
        for role, expected in (
            ("craftsman-reviewer", "reviewer"),
            ("Craftsman-Verifier", "verifier"),
            ("  craftsman:craftsman-reviewer ", "reviewer"),
            ("craftsman-reviewer W1-1", "reviewer"),
            ("craftsman-verifier: findings F1-F3", "verifier"),
        ):
            with self.subTest(role=role):
                self.assertEqual(launch_role(role, "self"), expected)

    def test_other_roles_are_no_craftsman_role(self) -> None:
        for role in ("reviewer", "Craftsman Reviewer", "craftsman-reviewers", "craftsman-reviewerX", "Code Reviewer", ""):
            with self.subTest(role=role):
                self.assertIsNone(launch_role(role, "self"))

    def test_the_type_name_may_name_the_role(self) -> None:
        self.assertEqual(launch_role("", "craftsman-verifier"), "verifier")
        self.assertEqual(launch_role("helper", "craftsman-reviewer"), "reviewer")

    def test_a_read_only_type_is_no_craftsman_subagent(self) -> None:
        self.assertIsNone(launch_role("craftsman-reviewer", "research"))

    def test_encoded_values_are_read(self) -> None:
        self.assertEqual(launch_role('"craftsman-reviewer"', '"self"'), "reviewer")


class SubagentLaunchTests(unittest.TestCase):
    def test_the_acknowledgement_names_the_conversation_and_its_time(self) -> None:
        entries = [user_input(0, "go", 0), *launch_reviewers(1, "sub-a", at=5)]
        self.assertEqual(
            subagent_launches(entries),
            (SubagentLaunch("sub-a", "reviewer", agy_time(5), Path("/app/brain/sub-a/.system_generated/logs/transcript.jsonl")),),
        )

    def test_both_transcript_encodings_give_the_same_launches(self) -> None:
        for encoded in (True, False):
            with self.subTest(encoded=encoded):
                entries = [planner(1, invoke_call(encoded=encoded), at=3), subagent_ack(2, "sub-a", 3)]
                self.assertEqual([item.conversation_id for item in subagent_launches(entries)], ["sub-a"])

    def test_one_response_with_other_tools_pairs_each_call_with_its_own_result(self) -> None:
        entries = [
            planner(
                3,
                agy_call("read_file", AbsolutePath="/x"),
                invoke_call("craftsman-reviewer"),
                invoke_call("craftsman-verifier"),
                at=10,
            ),
            generic(4, "file contents", 10),
            subagent_ack(5, "sub-a", 10),
            subagent_ack(6, "sub-b", 11),
        ]
        self.assertEqual(
            [(item.conversation_id, item.role, item.launched_at) for item in subagent_launches(entries)],
            [("sub-a", "reviewer", agy_time(10)), ("sub-b", "verifier", agy_time(11))],
        )

    def test_a_result_between_the_calls_keeps_the_pairing_by_position(self) -> None:
        entries = [
            planner(1, invoke_call(), agy_call("run_command", CommandLine="ls"), invoke_call("craftsman-verifier"), at=1),
            subagent_ack(2, "sub-a", 1),
            generic(3, "Command exited with code 0", 1),
            subagent_ack(4, "sub-b", 2),
        ]
        self.assertEqual([item.conversation_id for item in subagent_launches(entries)], ["sub-a", "sub-b"])

    def test_the_ack_is_found_with_trailing_text_after_the_json(self) -> None:
        entries = launch_reviewers(1, "sub-a")
        self.assertTrue(entries[1]["content"].endswith("requires guidance."))
        self.assertEqual(len(subagent_launches(entries)), 1)

    def test_a_non_craftsman_subagent_is_skipped_but_keeps_the_pairing(self) -> None:
        entries = [
            planner(1, invoke_call("explorer"), invoke_call("craftsman-reviewer"), at=1),
            subagent_ack(2, "sub-a", 1),
            subagent_ack(3, "sub-b", 1),
        ]
        self.assertEqual([item.conversation_id for item in subagent_launches(entries)], ["sub-b"])

    def test_a_launch_without_an_acknowledgement_is_not_listed(self) -> None:
        self.assertEqual(subagent_launches([planner(1, invoke_call(), at=1)]), ())

    def test_a_call_never_takes_the_acknowledgement_of_a_later_response(self) -> None:
        entries = [planner(1, invoke_call(), at=1), *launch_reviewers(2, "sub-x", at=5)]

        self.assertEqual([item.conversation_id for item in subagent_launches(entries)], ["sub-x"])

    def test_a_call_whose_own_result_is_missing_does_not_borrow_a_later_one(self) -> None:
        entries = [
            planner(1, agy_call("read_file", AbsolutePath="/x"), invoke_call(), at=1),
            generic(2, "file contents", 1),
            *launch_reviewers(3, "sub-x", at=5),
        ]

        self.assertEqual([item.conversation_id for item in subagent_launches(entries)], ["sub-x"])

    def test_an_acknowledgement_without_a_time_is_not_listed(self) -> None:
        entries = launch_reviewers(1, "sub-a")
        for broken in (None, "", "yesterday"):
            with self.subTest(created_at=broken):
                changed = [dict(entry) for entry in entries]
                changed[1]["created_at"] = broken
                self.assertEqual(subagent_launches(changed), ())
        del changed[1]["created_at"]
        self.assertEqual(subagent_launches(changed), ())

    def test_a_result_that_is_no_acknowledgement_is_not_listed(self) -> None:
        entries = [planner(1, invoke_call(), at=1), generic(2, "Error: no such role", 1)]
        self.assertEqual(subagent_launches(entries), ())

    def test_a_failed_invocation_is_not_listed(self) -> None:
        entries = launch_reviewers(1, "sub-a")
        entries[0] = {**entries[0], "status": "ERROR"}
        self.assertEqual(subagent_launches(entries), ())

    def test_launches_come_in_call_order_across_responses(self) -> None:
        entries = [*launch_reviewers(1, "sub-a", at=1), *launch_reviewers(5, "sub-b", at=9, role="craftsman-verifier")]
        self.assertEqual(
            [(item.conversation_id, item.role) for item in subagent_launches(entries)],
            [("sub-a", "reviewer"), ("sub-b", "verifier")],
        )


class SubagentFinishTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.app = AgyApp(Path(tmp.name).resolve())

    def finish(self, sub_id: str = "sub-1") -> object:
        return subagent_finish(self.app.root, sub_id, MAIN)

    def test_a_subagent_is_finished_when_its_last_entry_ends_its_turn(self) -> None:
        self.app.write_subagent(MAIN, "sub-1", planner(1, agy_call("run_command", CommandLine="ls"), at=3), generic(2, "ok", 4), planner(3, at=7))
        state = subagent_finish(self.app.root, "sub-1", MAIN)
        self.assertEqual((state.finished_at, state.killed, state.readable), (agy_time(7), False, True))

    def test_the_last_step_is_the_step_of_the_last_entry(self) -> None:
        self.app.write_subagent(MAIN, "sub-1", planner(1, agy_call("run_command", CommandLine="ls"), at=3), generic(2, "ok", 4))
        self.assertEqual(subagent_finish(self.app.root, "sub-1", MAIN).last_step, 2)
        self.app.write_subagent(MAIN, "sub-1", planner(1, at=3), generic(9, "ok", 4), planner(12, at=7))
        self.assertEqual(subagent_finish(self.app.root, "sub-1", MAIN).last_step, 12)
        self.assertIsNone(subagent_finish(self.app.root, "nobody", MAIN).last_step)

    def test_a_time_that_is_not_a_timestamp_is_no_time(self) -> None:
        for broken in ("yesterday", "2026-11-07T10:00:00", "  "):  # the last one has no zone
            with self.subTest(created_at=broken):
                self.app.write_subagent(MAIN, "sub-1", {**planner(1), "created_at": broken})
                self.assertEqual(subagent_finish(self.app.root, "sub-1", MAIN).finished_at, None)
                self.assertEqual(latest_user_input_text([{**user_input(1, "go"), "created_at": broken}]), (1, None, "go"))

    def test_the_registry_saying_alive_changes_nothing(self) -> None:
        self.app.write_subagent(MAIN, "sub-1", planner(1, at=3), state="SUBAGENT_STATE_ALIVE")
        self.assertEqual(subagent_finish(self.app.root, "sub-1", MAIN).finished_at, agy_time(3))

    def test_a_subagent_in_the_middle_of_its_work_is_running(self) -> None:
        for last in (
            planner(2, agy_call("run_command", CommandLine="ls"), at=4),  # waits for a tool
            generic(2, "output", 4),
            planner(2, at=4, status="RUNNING"),
        ):
            with self.subTest(last=last["type"], status=last.get("status")):
                self.app.write_subagent(MAIN, "sub-1", planner(1, at=1, status="RUNNING"), last)
                state = subagent_finish(self.app.root, "sub-1", MAIN)
                self.assertEqual((state.finished_at, state.killed, state.readable), (None, False, True))

    def test_a_resumed_subagent_finishes_later(self) -> None:
        self.app.write_subagent(MAIN, "sub-1", planner(1, at=3))
        first = subagent_finish(self.app.root, "sub-1", MAIN).finished_at
        self.app.write_subagent(MAIN, "sub-1", planner(1, at=3), parent_message(2, MAIN, 20), planner(3, at=25))
        self.assertEqual((first, subagent_finish(self.app.root, "sub-1", MAIN).finished_at), (agy_time(3), agy_time(25)))

    def test_a_killed_subagent_is_final_at_the_time_of_its_registry_file(self) -> None:
        self.app.write_subagent(MAIN, "sub-1", planner(1, agy_call("run_command", CommandLine="ls"), at=3), state="SUBAGENT_STATE_KILLED", killed_at=40)
        state = subagent_finish(self.app.root, "sub-1", MAIN)
        self.assertEqual((state.killed, state.readable), (True, True))
        self.assertEqual(state.finished_at, format_timestamp(AGY_EPOCH + timedelta(seconds=40)))

    def test_a_subagent_killed_before_its_first_entry_has_no_transcript(self) -> None:
        self.app.write_subagent(MAIN, "sub-1", state="SUBAGENT_STATE_KILLED", killed_at=9)
        self.app.transcript_path("sub-1").unlink()
        state = subagent_finish(self.app.root, "sub-1", MAIN)
        self.assertEqual((state.killed, state.readable, state.finished_at is not None), (True, False, True))

    def test_no_transcript_is_unreadable_not_finished(self) -> None:
        state = subagent_finish(self.app.root, "nobody", MAIN)
        self.assertEqual((state.finished_at, state.killed, state.readable), (None, False, False))

    def test_a_corrupt_transcript_is_unreadable_not_finished(self) -> None:
        path = self.app.transcript_path("sub-1")
        path.parent.mkdir(parents=True)
        path.write_text("not json\n" + json.dumps(planner(2, at=3)) + "\n", encoding="utf-8")
        state = subagent_finish(self.app.root, "sub-1", MAIN)
        self.assertEqual((state.finished_at, state.readable), (None, False))

    def test_the_full_transcript_is_the_fallback(self) -> None:
        self.app.write_transcript("sub-1", parent_message(0, MAIN), planner(1, at=6), full=True)
        state = subagent_finish(self.app.root, "sub-1", MAIN)
        self.assertEqual((state.finished_at, state.readable), (agy_time(6), True))

    def test_the_compact_transcript_wins_over_the_full_one(self) -> None:
        self.app.write_transcript("sub-1", parent_message(0, MAIN), planner(1, at=6))
        self.app.write_transcript("sub-1", parent_message(0, MAIN), planner(1, at=9), full=True)
        self.assertEqual(subagent_finish(self.app.root, "sub-1", MAIN).finished_at, agy_time(6))


class CutFieldsTests(unittest.TestCase):
    """Antigravity's compact transcript cuts long fields and lists them in `truncated_fields`."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.app = AgyApp(Path(tmp.name).resolve())

    @staticmethod
    def cut(entry: dict[str, object], *fields: str) -> dict[str, object]:
        return {**{key: value for key, value in entry.items() if key not in fields}, "truncated_fields": list(fields)}

    def test_without_restore_the_compact_entries_are_returned_as_written(self) -> None:
        full = planner(1, agy_call("run_command", encoded=False, CommandLine="ls"), at=3)
        self.app.write_transcript("c", user_input(0, "go", 0), self.cut(full, "tool_calls"))
        self.app.write_transcript("c", user_input(0, "go", 0), full, full=True)

        entries = read_transcript_entries(self.app.transcript_path("c"))

        self.assertEqual(entries[1]["truncated_fields"], ["tool_calls"])
        self.assertNotIn("tool_calls", entries[1])

    def test_restore_takes_the_cut_entry_from_the_same_line_of_the_full_file(self) -> None:
        full = planner(2, agy_call("run_command", encoded=False, CommandLine="ls"), at=3)
        path = self.app.write_transcript("c", user_input(0, "go", 0), self.cut(full, "tool_calls"))
        self.app.write_transcript("c", user_input(0, "go", 0), full, full=True)

        entries = read_transcript_entries(path, restore=True)

        self.assertEqual(entries[1], full)
        self.assertEqual(entries[0], user_input(0, "go", 0))

    def test_a_cut_response_is_a_question_only_while_nothing_restored_it(self) -> None:
        question = ask_question(2)
        cut = self.cut(question, "tool_calls")
        command = planner(2, agy_call("run_command", encoded=False, CommandLine="ls"), at=3)

        self.assertTrue(has_cut_tool_calls([user_input(0, "go", 0), cut]))
        self.assertTrue(is_waiting_on_question([user_input(0, "go", 0), cut]))  # unrestored: never trap the user
        self.assertFalse(has_cut_tool_calls([user_input(0, "go", 0), question]))  # restored
        self.assertTrue(is_waiting_on_question([user_input(0, "go", 0), question]))
        self.assertFalse(is_waiting_on_question([user_input(0, "go", 0), command]))
        self.assertFalse(is_waiting_on_question([cut, user_input(1, "answer", 4)]))  # the user spoke last
        self.assertFalse(has_cut_tool_calls([self.cut(command, "content"), user_input(1, "answer", 4)]))
        self.assertFalse(is_waiting_on_question([user_input(0, "go", 0), self.cut(command, "content")]))  # another field was cut

    def test_the_step_of_a_cut_response_is_named(self) -> None:
        cut = self.cut(ask_question(7), "tool_calls")

        self.assertEqual(cut_tool_calls_step([user_input(0, "go", 0), cut]), 7)
        self.assertIsNone(cut_tool_calls_step([user_input(0, "go", 0), ask_question(7)]))
        self.assertIsNone(cut_tool_calls_step([]))

    def test_only_the_latest_user_input_decides_whether_a_user_input_is_cut(self) -> None:
        long = cut_user_input(long_user_input(1, "/craft-review " + "x " * 3000, 1))
        short = user_input(4, "thanks", 9)

        self.assertTrue(has_cut_user_input([long]))
        self.assertTrue(has_cut_user_input([short, ask_question(5), long]))
        self.assertFalse(has_cut_user_input([long, ask_question(2), short]))  # an older cut input does not matter
        self.assertFalse(has_cut_user_input([self.cut(user_input(1, "x"), "other")]))
        self.assertFalse(has_cut_user_input([ask_question(1)]))

    def test_read_conversation_restores_a_cut_user_input_and_a_cut_response(self) -> None:
        full_input = long_user_input(1, "/craft-review " + "x " * 3000, 1)
        question = ask_question(2)
        path = self.app.write_transcript("c", cut_user_input(full_input), self.cut(question, "tool_calls"))
        self.app.write_transcript("c", full_input, question, full=True)

        self.assertEqual(read_conversation(path), [full_input, question])

        self.app.write_transcript("c", cut_user_input(full_input), question)
        self.assertEqual(read_conversation(path), [full_input, question])  # only the input was cut

    def test_read_conversation_without_a_full_file_keeps_what_was_cut(self) -> None:
        full_input = long_user_input(1, "/craft-review " + "x " * 3000, 1)
        path = self.app.write_transcript("c", cut_user_input(full_input))

        entries = read_conversation(path)

        self.assertTrue(has_cut_user_input(entries))
        self.assertEqual(read_conversation(None), [])

    def test_read_conversation_leaves_an_uncut_transcript_alone(self) -> None:
        path = self.app.write_transcript("c", user_input(0, "go", 0), ask_question(1))
        with mock.patch("craftsman.transcript._restored") as restored:
            self.assertEqual(read_conversation(path), [user_input(0, "go", 0), ask_question(1)])
        restored.assert_not_called()

    def test_restore_follows_line_numbers_across_blank_lines(self) -> None:
        full = planner(2, agy_call("run_command", encoded=False, CommandLine="ls"), at=3)
        path = self.app.transcript_path("c")
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(user_input(0, "go", 0)) + "\n\n" + json.dumps(self.cut(full, "tool_calls")) + "\n", encoding="utf-8")
        self.app.transcript_path("c", full=True).write_text(
            json.dumps(user_input(0, "go", 0)) + "\n\n" + json.dumps(full) + "\n", encoding="utf-8"
        )

        self.assertEqual(read_transcript_entries(path, restore=True)[1], full)

    def test_a_line_of_the_full_file_that_is_another_entry_is_not_taken(self) -> None:
        cut = self.cut(planner(2, agy_call("run_command", encoded=False, CommandLine="ls"), at=3), "tool_calls")
        path = self.app.write_transcript("c", user_input(0, "go", 0), cut)
        self.app.write_transcript("c", user_input(0, "go", 0), planner(7, at=3), full=True)  # another step

        self.assertEqual(read_transcript_entries(path, restore=True)[1], cut)

    def test_restore_reads_nothing_when_no_entry_was_cut(self) -> None:
        path = self.app.write_transcript("c", user_input(0, "go", 0), planner(1, at=3))
        self.app.transcript_path("c", full=True).write_text("not json at all\n", encoding="utf-8")  # never opened

        self.assertEqual(len(read_transcript_entries(path, restore=True)), 2)

    def test_a_transcript_that_is_not_the_compact_file_has_no_full_sibling(self) -> None:
        full = planner(1, agy_call("run_command", encoded=False, CommandLine="ls"), at=3)
        other = self.app.transcript_path("c").with_name("elsewhere.jsonl")
        other.parent.mkdir(parents=True)
        other.write_text(json.dumps(self.cut(full, "tool_calls")) + "\n", encoding="utf-8")
        self.app.write_transcript("c", full, full=True)

        self.assertEqual(read_transcript_entries(other, restore=True)[0]["truncated_fields"], ["tool_calls"])

    def test_a_full_file_that_cannot_be_read_is_an_error_only_when_it_is_needed(self) -> None:
        cut = self.cut(planner(1, agy_call("run_command", encoded=False, CommandLine="ls"), at=3), "tool_calls")
        path = self.app.write_transcript("c", cut)
        self.app.transcript_path("c", full=True).write_text("not json\n", encoding="utf-8")  # a line that is not JSON: skipped

        self.assertEqual(read_transcript_entries(path, restore=True), [cut])  # nothing restores, nothing raises here

        self.app.transcript_path("c", full=True).unlink()
        self.app.transcript_path("c", full=True).mkdir()  # unreadable as a file

        with self.assertRaisesRegex(TranscriptError, "cannot read the transcript .*transcript_full.jsonl"):
            read_transcript_entries(path, restore=True)

    def test_a_launch_in_a_cut_response_is_never_read_as_no_launch(self) -> None:
        response = planner(1, agy_call("invoke_subagent", encoded=False, TypeName="self", Role="craftsman-reviewer", Prompt="p"), at=5)
        ack = subagent_ack(2, "sub-1", 5)

        with self.assertRaisesRegex(TranscriptError, "the tool_calls of step 1 was cut"):
            subagent_launches([self.cut(response, "tool_calls"), ack])
        with self.assertRaisesRegex(TranscriptError, "the content of step 2 was cut"):
            subagent_launches([response, {**ack, "content": "Created the foll", "truncated_fields": ["content"]}])
        self.assertEqual([item.conversation_id for item in subagent_launches([response, ack])], ["sub-1"])

    def test_a_subagent_whose_last_response_lost_its_tool_calls_has_not_finished(self) -> None:
        # The compact file shows a response without tool calls, which would end a turn; it may be a call that was cut.
        running = planner(2, agy_call("run_command", encoded=False, CommandLine="ls"), at=9)
        self.app.write_subagent(MAIN, "sub-1", planner(1, at=3), self.cut(running, "tool_calls"))

        state = subagent_finish(self.app.root, "sub-1", MAIN)

        self.assertEqual((state.finished_at, state.readable, state.last_step), (None, True, 2))

    def test_a_cut_last_response_is_restored_from_the_full_file_alone(self) -> None:
        running = planner(2, at=9)  # the full file says it really ended the turn
        self.app.write_subagent(MAIN, "sub-1", planner(1, at=3), self.cut(running, "tool_calls"))
        self.app.write_transcript("sub-1", parent_message(0, MAIN), planner(1, at=3), running, full=True)

        self.assertEqual(subagent_finish(self.app.root, "sub-1", MAIN).finished_at, agy_time(9))

    def test_the_role_of_a_subagent_is_read_from_the_full_file_when_the_registry_has_none(self) -> None:
        response = planner(1, agy_call("invoke_subagent", encoded=False, TypeName="self", Role="craftsman-verifier", Prompt="p"), at=5)
        ack = subagent_ack(2, "sub-1", 5)
        self.app.write_transcript(MAIN, user_input(0, "go", 0), self.cut(response, "tool_calls"), ack)
        self.app.write_transcript(MAIN, user_input(0, "go", 0), response, ack, full=True)

        self.assertEqual(subagent_role(self.app.root, "sub-1", MAIN), "verifier")


class ParentConversationTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.app = AgyApp(Path(tmp.name).resolve())

    def test_a_subagent_names_the_conversation_that_launched_it(self) -> None:
        self.app.write_subagent(MAIN, "sub-1", planner(1, at=3))
        self.assertEqual(parent_conversation(self.app.root, "sub-1"), MAIN)

    def test_a_main_conversation_has_no_parent(self) -> None:
        self.app.write_transcript(MAIN, user_input(0, "review this", 0), planner(1, at=3))
        self.assertIsNone(parent_conversation(self.app.root, MAIN))

    def test_a_user_input_makes_it_a_main_conversation_whatever_came_first(self) -> None:
        self.app.write_transcript("x", parent_message(0, MAIN), user_input(1, "hello", 2))
        self.assertIsNone(parent_conversation(self.app.root, "x"))

    def test_the_message_must_be_among_the_first_entries(self) -> None:
        late = [planner(index, at=index) for index in range(1, 7)]
        self.app.write_transcript("x", *late, parent_message(7, MAIN))
        self.assertIsNone(parent_conversation(self.app.root, "x"))

    def test_a_message_without_a_sender_names_nobody(self) -> None:
        entry = parent_message(0, MAIN)
        entry["content"] = "<SYSTEM_MESSAGE>\n[Message] priority=HIGH content=hello\n</SYSTEM_MESSAGE>"
        self.app.write_transcript("x", entry, planner(1, at=1))
        self.assertIsNone(parent_conversation(self.app.root, "x"))

    def test_the_transcript_may_be_given(self) -> None:
        path = self.app.write_transcript("elsewhere", parent_message(0, MAIN))
        self.assertEqual(parent_conversation(self.app.root, "unknown", path=path), MAIN)

    def test_a_missing_or_unreadable_transcript_has_no_parent(self) -> None:
        self.assertIsNone(parent_conversation(self.app.root, "nobody"))
        path = self.app.transcript_path("x")
        path.parent.mkdir(parents=True)
        path.mkdir()
        self.assertIsNone(parent_conversation(self.app.root, "x", path=path))

    def test_a_corrupt_line_does_not_hide_the_parent(self) -> None:
        path = self.app.transcript_path("x")
        path.parent.mkdir(parents=True)
        path.write_text("{broken\n" + json.dumps(parent_message(1, MAIN)) + "\n", encoding="utf-8")
        self.assertEqual(parent_conversation(self.app.root, "x"), MAIN)


class SubagentRoleTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.app = AgyApp(Path(tmp.name).resolve())

    def test_the_registry_file_names_the_role(self) -> None:
        self.app.write_subagent(MAIN, "sub-1", planner(1, at=3), role="craftsman-verifier")
        self.assertEqual(subagent_role(self.app.root, "sub-1", MAIN), "verifier")

    def test_the_parents_transcript_names_it_without_a_registry_file(self) -> None:
        self.app.write_transcript(MAIN, user_input(0, "go", 0), *launch_reviewers(1, "sub-1"))
        self.assertEqual(subagent_role(self.app.root, "sub-1", MAIN), "reviewer")

    def test_another_subagent_has_no_craftsman_role(self) -> None:
        self.app.write_subagent(MAIN, "sub-1", planner(1, at=3), role="explorer")
        self.assertIsNone(subagent_role(self.app.root, "sub-1", MAIN))


class RequestBodyTests(unittest.TestCase):
    def test_the_last_wrapped_request_is_what_the_user_typed(self) -> None:
        self.assertEqual(request_body("<USER_REQUEST>\nfix it\n</USER_REQUEST>").strip(), "fix it")
        self.assertEqual(request_body("<USER_REQUEST>a</USER_REQUEST> x <USER_REQUEST>b</USER_REQUEST>"), "b")

    def test_an_unwrapped_text_is_itself(self) -> None:
        self.assertEqual(request_body("fix it"), "fix it")


class ConversationStateTests(unittest.TestCase):
    def test_unanswered_question_is_waiting(self) -> None:
        self.assertTrue(is_waiting_on_question([user_input(1, "go"), ask_question(2)]))

    def test_answered_question_is_not_waiting(self) -> None:
        entries = [ask_question(1), user_input(2, "option B")]
        self.assertFalse(is_waiting_on_question(entries))

    def test_latest_user_input_has_its_step(self) -> None:
        entries = [user_input(1, "first"), ask_question(2), user_input(7, "second")]
        self.assertEqual(latest_user_input(entries), (7, "second"))

    def test_latest_user_input_text_has_its_time(self) -> None:
        entries = [user_input(1, "first", 1), ask_question(2), user_input(7, "second", 30)]
        self.assertEqual(latest_user_input_text(entries), (7, agy_time(30), "second"))
        self.assertEqual(latest_user_input_text([user_input(1, "no time")]), (1, None, "no time"))
        self.assertIsNone(latest_user_input_text([ask_question(1)]))

    def test_review_request_reads_the_prompt_hooks_way(self) -> None:
        self.assertEqual(review_request("/craft-review --deep the totals change"), (True, "the totals change"))
        self.assertEqual(review_request("/craft-review the totals change"), (False, "the totals change"))
        self.assertIsNone(review_request("please fix the typo"))
        self.assertIsNone(review_request("what does /craft-review do?"))


class ReadTranscriptTests(unittest.TestCase):
    def test_claude_code_prompt_reads_as_user_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.jsonl"
            line = {"type": "user", "message": {"content": [{"type": "text", "text": "hello"}]}}
            path.write_text("\n" + json.dumps(line) + "\n", encoding="utf-8")
            self.assertEqual(
                read_transcript_entries(path),
                [{"step_index": 2, "type": "USER_INPUT", "content": "hello"}],
            )

    def test_corrupt_line_is_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.jsonl"
            path.write_text('not json\n{"type": "USER_INPUT"}\n', encoding="utf-8")

            with self.assertRaisesRegex(TranscriptError, "t.jsonl:1 is not JSON"):
                read_transcript_entries(path)

    def test_last_line_being_written_is_left_out(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.jsonl"
            path.write_text('{"step_index": 1, "type": "USER_INPUT"}\n{"step_ind', encoding="utf-8")

            self.assertEqual(read_transcript_entries(path), [{"step_index": 1, "type": "USER_INPUT"}])

    def test_missing_transcript_has_no_entries(self) -> None:
        self.assertEqual(read_transcript_entries(Path("/nonexistent/transcript.jsonl")), [])

    def test_unreadable_transcript_is_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(TranscriptError):
            read_transcript_entries(Path(tmp))


if __name__ == "__main__":
    unittest.main()
