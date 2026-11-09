"""Tests for bringing Antigravity subagent runs into the ledger (`craftsman.agy_sync`)."""

from __future__ import annotations

import io
import tempfile
import unittest
import unittest.mock
from contextlib import redirect_stderr
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

from craftsman import agy_sync
from craftsman.agy_sync import (
    apply_runs,
    caller_parent,
    main_conversation,
    observe_runs,
    start_time,
    sync_antigravity_runs,
    sync_from_stop,
)
from craftsman.attribution import agent_runs
from craftsman.ledger import ledger_path_for, load, save
from craftsman.records import AgentEvent, Ledger, parse_timestamp
from craftsman.transcript import TranscriptError
from craftsman.workspace import Workspace

from tests.support import (
    AGY_EPOCH,
    AgyApp,
    EvidenceRepo,
    agent_event,
    agy_call,
    at,
    generic,
    launch_reviewers,
    parent_message,
    planner,
    subagent_ack,
    user_input,
)
from tests.test_attribution import hunt_ledger, with_agents

MAIN = "main-1"
SUB = "sub-1"
AGY = "antigravity"


def run_events(ledger: Ledger, agent_id: str = SUB) -> list[tuple[str, str, str]]:
    """`(event, agent, at)` of the antigravity events of one conversation, in ledger order."""
    return [(e.event, e.agent, e.at) for e in ledger.agents if e.platform == AGY and e.agent_id == agent_id and e.event != "nudge"]


class SyncTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.app = AgyApp(self.tmp / "app")

    def launch(self, *sub_ids: str, at_second: float = 5, role: str = "craftsman-reviewer") -> None:
        """The main conversation launched `sub_ids` at `at_second`."""
        self.app.write_transcript(MAIN, user_input(0, "/craft-review", 0), *launch_reviewers(1, *sub_ids, at=at_second, role=role))

    def finish(self, sub_id: str = SUB, at_second: float = 20, *, role: str = "craftsman-reviewer") -> None:
        self.app.write_subagent(MAIN, sub_id, planner(1, at=at_second), role=role)

    def run_(self, sub_id: str = SUB, at_second: float = 20, *, role: str = "craftsman-reviewer") -> None:
        """A subagent in the middle of its work."""
        self.app.write_subagent(MAIN, sub_id, planner(1, agy_call("run_command", CommandLine="ls"), at=at_second), role=role)

    def sync(self, ledger: Ledger) -> Ledger:
        return apply_runs(ledger, observe_runs(self.app.root, MAIN))


class EventTests(SyncTestCase):
    def test_a_finished_run_is_a_start_at_its_launch_and_a_stop_at_the_end_of_its_last_second(self) -> None:
        self.launch(SUB, at_second=5)
        self.finish(at_second=20)

        events = run_events(self.sync(hunt_ledger()))

        self.assertEqual(events, [("start", "craftsman-reviewer", at(5)), ("stop", "craftsman-reviewer", at(20.999))])

    def test_a_verifier_is_recorded_as_a_verifier(self) -> None:
        self.launch(SUB, role="craftsman-verifier")
        self.finish(role="craftsman-verifier")

        self.assertEqual({agent for _, agent, _ in run_events(self.sync(hunt_ledger()))}, {"craftsman-verifier"})

    def test_a_run_that_is_still_working_has_a_start_only(self) -> None:
        self.launch(SUB)
        self.run_()

        self.assertEqual(run_events(self.sync(hunt_ledger())), [("start", "craftsman-reviewer", at(5))])

    def test_a_run_without_a_readable_transcript_has_a_start_only(self) -> None:
        self.launch(SUB)

        self.assertEqual([event for event, _, _ in run_events(self.sync(hunt_ledger()))], ["start"])

    def test_a_killed_run_stops_when_its_registry_file_was_written_and_counts_as_dead_not_running(self) -> None:
        self.launch(SUB)
        self.app.write_subagent(
            MAIN, SUB, planner(1, agy_call("run_command", CommandLine="ls"), at=7), state="SUBAGENT_STATE_KILLED", killed_at=40.25
        )

        ledger = self.sync(hunt_ledger())

        self.assertEqual(run_events(ledger), [("start", "craftsman-reviewer", at(5)), ("stop", "craftsman-reviewer", at(40.25))])
        self.assertIsNotNone(agent_runs(ledger)[0].stopped)

    def test_a_resumed_subagent_stops_later(self) -> None:
        self.launch(SUB)
        self.finish(at_second=10)
        first = self.sync(hunt_ledger())
        self.app.write_subagent(MAIN, SUB, planner(1, at=10), parent_message(2, MAIN, 30), planner(3, at=45))

        second = self.sync(first)

        self.assertEqual(run_events(first)[1][2], at(10.999))
        self.assertEqual(run_events(second), [("start", "craftsman-reviewer", at(5)), ("stop", "craftsman-reviewer", at(45.999))])

    def test_a_resumed_subagent_that_works_again_has_no_stop_until_it_finishes(self) -> None:
        self.launch(SUB)
        self.finish(at_second=10)
        first = self.sync(hunt_ledger())
        self.app.write_subagent(MAIN, SUB, planner(1, at=10), parent_message(2, MAIN, 30), planner(3, agy_call("run_command", CommandLine="ls"), at=31))

        self.assertEqual([event for event, _, _ in run_events(self.sync(first))], ["start"])

    def test_several_launches_are_each_recorded(self) -> None:
        self.launch("sub-a", "sub-b")
        self.finish("sub-a", 20)
        self.finish("sub-b", 30)

        ledger = self.sync(hunt_ledger())

        self.assertEqual([run.agent_id for run in agent_runs(ledger)], ["sub-a", "sub-b"])
        self.assertEqual([run.stopped for run in agent_runs(ledger)], [parse_timestamp(at(20.999)), parse_timestamp(at(30.999))])


class ReplacementTests(SyncTestCase):
    def test_syncing_twice_changes_nothing(self) -> None:
        self.launch(SUB)
        self.finish()
        once = self.sync(hunt_ledger())

        self.assertEqual(self.sync(once), once)

    def test_an_unchanged_run_keeps_its_place_among_later_events(self) -> None:
        self.launch(SUB)
        self.finish()
        ledger = replace(self.sync(hunt_ledger()), agents=())
        ledger = self.sync(ledger)
        later = agent_event("start", "craftsman-reviewer", "other", 3, platform="claude")
        ledger = with_agents(ledger, later)

        self.assertEqual(self.sync(ledger), ledger)

    def test_a_nudge_is_never_dropped(self) -> None:
        self.launch(SUB)
        self.finish(at_second=20)
        nudge = AgentEvent(AGY, "nudge", "craftsman-reviewer", SUB, at(12))
        ledger = with_agents(hunt_ledger(), nudge)

        self.assertIn(nudge, self.sync(ledger).agents)
        self.assertIn(nudge, self.sync(self.sync(ledger)).agents)

    def test_events_of_other_platforms_and_other_conversations_are_left_alone(self) -> None:
        self.launch(SUB)
        self.finish()
        others = (
            agent_event("start", "craftsman-reviewer", SUB, 2, platform="claude"),
            agent_event("stop", "craftsman-reviewer", SUB, 3, platform="claude"),
            agent_event("start", "craftsman-reviewer", "elsewhere", 4, platform=AGY),
        )
        ledger = with_agents(hunt_ledger(), *others)

        synced = self.sync(ledger)

        self.assertEqual(tuple(e for e in synced.agents if e in others), others)
        self.assertEqual(len(synced.agents), len(others) + 2)

    def test_a_stale_start_is_replaced_by_the_one_the_transcript_gives(self) -> None:
        self.launch(SUB, at_second=5)
        self.finish()
        wrong = AgentEvent(AGY, "start", "craftsman-reviewer", SUB, at(1))

        synced = self.sync(with_agents(hunt_ledger(), wrong))

        self.assertNotIn(wrong, synced.agents)
        self.assertEqual(run_events(synced)[0], ("start", "craftsman-reviewer", at(5)))


class NudgedRunTests(SyncTestCase):
    """A run that was sent back is still working: it stops when it finishes AFTER the nudge (or is killed)."""

    def nudged(self, nudge_second: float) -> Ledger:
        return with_agents(hunt_ledger(), AgentEvent(AGY, "nudge", "craftsman-reviewer", SUB, at(nudge_second)))

    def test_a_transcript_that_ended_before_the_nudge_gives_no_stop(self) -> None:
        self.launch(SUB)
        self.finish(at_second=10)

        self.assertEqual([event for event, _, _ in run_events(self.sync(self.nudged(12)))], ["start"])

    def test_a_transcript_that_ended_in_the_second_of_the_nudge_gives_no_stop(self) -> None:
        self.launch(SUB)
        self.finish(at_second=12)

        self.assertEqual([event for event, _, _ in run_events(self.sync(self.nudged(12)))], ["start"])
        self.assertEqual([event for event, _, _ in run_events(self.sync(self.nudged(12.7)))], ["start"])

    def test_a_transcript_that_ended_after_the_nudge_gives_a_stop(self) -> None:
        self.launch(SUB)
        self.finish(at_second=13)

        self.assertEqual(run_events(self.sync(self.nudged(12.7)))[1], ("stop", "craftsman-reviewer", at(13.999)))

    def stepped(self, nudge_second: float, last_step: int) -> Ledger:
        nudge = AgentEvent(AGY, "nudge", "craftsman-reviewer", SUB, at(nudge_second), last_step=last_step)
        return with_agents(hunt_ledger(), nudge)

    def test_a_turn_finished_in_the_second_of_the_nudge_but_at_a_later_step_gives_a_stop(self) -> None:
        # A response is stamped with the start of its generation: the hunter that ignores the nudge ends its
        # turn in the same whole second, which a time comparison cannot tell from the turn it was sent back at.
        self.launch(SUB)
        self.app.write_subagent(MAIN, SUB, planner(1, at=12))
        sent_back = self.stepped(12.494, 1)
        self.assertEqual([event for event, _, _ in run_events(self.sync(sent_back))], ["start"])

        self.app.write_subagent(MAIN, SUB, planner(1, at=12), planner(2, at=12))

        self.assertEqual(run_events(self.sync(sent_back))[1], ("stop", "craftsman-reviewer", at(12.999)))

    def test_a_transcript_that_has_not_moved_past_the_step_of_the_nudge_gives_no_stop_whatever_its_time(self) -> None:
        self.launch(SUB)
        self.finish(at_second=30)

        self.assertEqual([event for event, _, _ in run_events(self.sync(self.stepped(12, 1)))], ["start"])
        self.assertEqual([event for event, _, _ in run_events(self.sync(self.stepped(12, 2)))], ["start"])
        self.assertEqual([event for event, _, _ in run_events(self.sync(self.stepped(12, 0)))], ["start", "stop"])

    def test_a_resumed_subagent_that_works_again_after_a_nudge_has_no_stop(self) -> None:
        self.launch(SUB)
        self.app.write_subagent(MAIN, SUB, planner(1, at=12), planner(2, agy_call("run_command", CommandLine="ls"), at=12))

        self.assertEqual([event for event, _, _ in run_events(self.sync(self.stepped(12.4, 1)))], ["start"])

    def test_a_killed_run_stops_whatever_the_nudge(self) -> None:
        self.launch(SUB)
        self.app.write_subagent(MAIN, SUB, planner(1, at=10), state="SUBAGENT_STATE_KILLED", killed_at=11)

        self.assertEqual([event for event, _, _ in run_events(self.sync(self.nudged(12)))], ["start", "stop"])

    def test_the_nudge_of_another_conversation_changes_nothing(self) -> None:
        self.launch(SUB)
        self.finish(at_second=10)
        ledger = with_agents(hunt_ledger(), AgentEvent(AGY, "nudge", "craftsman-reviewer", "elsewhere", at(12)))

        self.assertEqual([event for event, _, _ in run_events(self.sync(ledger))], ["start", "stop"])


class WholeSecondTests(SyncTestCase):
    """E10.2: transcript times are whole seconds, the CLI's are milliseconds."""

    def ledger_prepared(self, *seconds: float) -> Ledger:
        ledger = hunt_ledger()
        first = ledger.rounds[0]
        rounds = tuple(replace(first, number=index, prepared_at=at(second)) for index, second in enumerate(seconds, 1))
        return replace(ledger, rounds=rounds)

    def test_a_launch_in_the_second_of_a_later_prepare_starts_at_the_prepare(self) -> None:
        self.assertEqual(start_time(self.ledger_prepared(10.4), at(10)), parse_timestamp(at(10.4)))

    def test_a_launch_in_an_earlier_second_keeps_its_time(self) -> None:
        self.assertEqual(start_time(self.ledger_prepared(10.4), at(9)), parse_timestamp(at(9)))

    def test_a_launch_after_the_prepare_keeps_its_time(self) -> None:
        for prepared in (10.4, 10.0):
            with self.subTest(prepared=prepared):
                self.assertEqual(start_time(self.ledger_prepared(prepared), at(11)), parse_timestamp(at(11)))
        self.assertEqual(start_time(self.ledger_prepared(10.0), at(10)), parse_timestamp(at(10)))

    def test_the_round_whose_second_it_is_decides_among_several(self) -> None:
        ledger = self.ledger_prepared(3.2, 10.4)

        self.assertEqual(start_time(ledger, at(10)), parse_timestamp(at(10.4)))
        self.assertEqual(start_time(ledger, at(3)), parse_timestamp(at(3.2)))

    def test_a_hunter_launched_in_the_second_of_the_prepare_backs_the_round(self) -> None:
        ledger = replace(hunt_ledger(), rounds=(replace(hunt_ledger().rounds[0], prepared_at=at(10.4)),))
        self.launch(SUB, at_second=10)
        self.finish(at_second=20)

        (run,) = agent_runs(self.sync(ledger))

        self.assertGreaterEqual(run.started, parse_timestamp(ledger.rounds[0].prepared_at))

    def test_a_cli_record_in_the_second_of_the_last_step_falls_inside_the_run(self) -> None:
        self.launch(SUB, at_second=5)
        self.finish(at_second=20)

        (run,) = agent_runs(self.sync(hunt_ledger()))

        self.assertTrue(run.contains(parse_timestamp(at(20.5))))
        self.assertTrue(run.contains(parse_timestamp(at(20.998))))
        self.assertFalse(run.contains(parse_timestamp(at(21.0))))

    def test_a_stop_is_never_before_its_start(self) -> None:
        ledger = replace(hunt_ledger(), rounds=(replace(hunt_ledger().rounds[0], prepared_at=at(20.9)),))
        self.launch(SUB, at_second=20)
        self.finish(at_second=20)

        events = run_events(self.sync(ledger))

        self.assertLessEqual(parse_timestamp(events[0][2]), parse_timestamp(events[1][2]))

    def test_a_killed_run_that_was_killed_before_its_clamped_start_stops_at_its_start(self) -> None:
        # The registry file's own time is a real instant: it can be earlier than a start that was clamped up to the
        # `prepared_at` of the second it was launched in.
        ledger = replace(hunt_ledger(), rounds=(replace(hunt_ledger().rounds[0], prepared_at=at(20.9)),))
        self.launch(SUB, at_second=20)
        self.app.write_subagent(
            MAIN, SUB, planner(1, agy_call("run_command", CommandLine="ls"), at=7), state="SUBAGENT_STATE_KILLED", killed_at=20.3
        )

        events = run_events(self.sync(ledger))

        self.assertEqual(events, [("start", "craftsman-reviewer", at(20.9)), ("stop", "craftsman-reviewer", at(20.9))])


def compact(entry: dict[str, Any], **cut: Any) -> dict[str, Any]:
    """The entry as Antigravity's compact `transcript.jsonl` writes it when it cuts fields: each field of `cut` is
    dropped (value None) or replaced, and listed in `truncated_fields`."""
    kept = {key: value for key, value in entry.items() if key not in cut}
    kept.update({key: value for key, value in cut.items() if value is not None})
    return {**kept, "truncated_fields": sorted(cut)}


class CompactTranscriptTests(SyncTestCase):
    """The compact transcript cuts long fields; launches and acknowledgements are read from the full one (REVIEW-FSE 8)."""

    def write(self, compact_entries: list[dict[str, Any]], full_entries: list[dict[str, Any]] | None) -> None:
        self.app.write_transcript(MAIN, *compact_entries)
        if full_entries is not None:
            self.app.write_transcript(MAIN, *full_entries, full=True)

    def launch_steps(self) -> tuple[dict[str, Any], dict[str, Any]]:
        """The launch response as the full transcript has it (native argument values) and its acknowledgement."""
        response = planner(1, agy_call("invoke_subagent", encoded=False, TypeName="self", Role="craftsman-reviewer", Prompt="p"), at=5)
        return response, subagent_ack(2, SUB, 5)

    def launched(self) -> list[str]:
        return [run.launch.conversation_id for run in observe_runs(self.app.root, MAIN)]

    def test_a_launch_only_the_full_file_has_is_found(self) -> None:
        response, ack = self.launch_steps()
        first = user_input(0, "/craft-review", 0)
        self.write([first, compact(response, tool_calls=None), ack], [first, response, ack])

        self.assertEqual(self.launched(), [SUB])

    def test_an_acknowledgement_cut_in_the_compact_file_is_read_from_the_full_one(self) -> None:
        response, ack = self.launch_steps()
        first = user_input(0, "/craft-review", 0)
        self.write([first, response, compact(ack, content="Created the follow...")], [first, response, ack])

        self.assertEqual(self.launched(), [SUB])

    def test_a_cut_response_without_a_launch_is_no_launch_and_no_problem(self) -> None:
        first = user_input(0, "/craft-review", 0)
        ran = planner(1, agy_call("run_command", encoded=False, CommandLine="ls"), at=5)
        self.write([first, compact(ran, tool_calls=None)], [first, ran])

        self.assertEqual(self.launched(), [])

    def test_the_compact_file_stays_the_authority_on_the_lines_that_exist(self) -> None:
        response, ack = self.launch_steps()
        first = user_input(0, "/craft-review", 0)
        self.write([first, compact(response, tool_calls=None), ack], [first])  # the full file is behind

        with self.assertRaisesRegex(TranscriptError, "cut from the compact transcript"):
            observe_runs(self.app.root, MAIN)

    def test_a_cut_step_without_a_full_file_is_a_problem_never_no_launch(self) -> None:
        response, ack = self.launch_steps()
        first = user_input(0, "/craft-review", 0)
        for label, entries in (
            ("tool_calls", [first, compact(response, tool_calls=None), ack]),
            ("content", [first, response, compact(ack, content="Created the follow...")]),
        ):
            with self.subTest(cut=label):
                self.write(entries, None)

                with self.assertRaisesRegex(TranscriptError, f"the {label} of step"):
                    observe_runs(self.app.root, MAIN)

    def test_a_cut_result_that_is_no_acknowledgement_does_not_matter(self) -> None:
        response, ack = self.launch_steps()
        first = user_input(0, "/craft-review", 0)
        read = planner(3, agy_call("view_file", encoded=False, AbsolutePath="/x"), at=6)
        result = generic(4, "a long file", 6)
        self.write([first, response, ack, read, compact(result, content="a lo...")], None)

        self.assertEqual(self.launched(), [SUB])

    def test_a_cut_acknowledgement_of_another_call_than_a_launch_is_not_read(self) -> None:
        # Two calls in one response: the k-th result belongs to the k-th call; only the launch's result is looked at.
        first = user_input(0, "/craft-review", 0)
        response = planner(1, agy_call("view_file", encoded=False, AbsolutePath="/x"), at=5)
        self.write([first, response, compact(generic(2, "long", 5), content="lo...")], None)

        self.assertEqual(self.launched(), [])

    def test_the_full_file_past_the_byte_cap_is_a_problem(self) -> None:
        response, ack = self.launch_steps()
        first = user_input(0, "/craft-review", 0)
        self.write([first, compact(response, tool_calls=None), ack], [first, response, ack])
        small = self.app.transcript_path(MAIN).stat().st_size
        large = self.app.transcript_path(MAIN, full=True).stat().st_size
        self.assertLess(small, large)

        with unittest.mock.patch("craftsman.transcript.MAX_TRANSCRIPT_BYTES", small + 1):
            with self.assertRaisesRegex(TranscriptError, "transcript_full.jsonl is larger than"):
                observe_runs(self.app.root, MAIN)

    def test_a_cap_hit_costs_a_problem_line_and_credits_nothing(self) -> None:
        response, ack = self.launch_steps()
        first = user_input(0, "/craft-review", 0)
        self.write([first, compact(response, tool_calls=None), ack], [first, response, ack])
        repo = EvidenceRepo(self.tmp / "repo")
        ledger_path = ledger_path_for(repo.root)
        env = {"ANTIGRAVITY_CONVERSATION_ID": MAIN, "ANTIGRAVITY_APP_DATA_DIR": str(self.app.root)}
        err = io.StringIO()

        with redirect_stderr(err), unittest.mock.patch(
            "craftsman.transcript.MAX_TRANSCRIPT_BYTES", self.app.transcript_path(MAIN).stat().st_size + 1
        ):
            sync_antigravity_runs(Workspace(repo.root, None, ledger_path), env)

        self.assertEqual(len(err.getvalue().strip().splitlines()), 1)
        self.assertIn("not synced", err.getvalue())
        self.assertIn("transcript_full.jsonl is larger than", err.getvalue())
        self.assertFalse(ledger_path.exists())


class ConversationTests(SyncTestCase):
    def test_a_main_conversation_is_its_own_main(self) -> None:
        self.app.write_transcript(MAIN, user_input(0, "go", 0))
        self.assertEqual(main_conversation(self.app.root, MAIN), MAIN)

    def test_a_subagent_resolves_to_its_parent_and_a_nested_one_to_the_main(self) -> None:
        self.app.write_transcript(MAIN, user_input(0, "go", 0))
        self.app.write_subagent(MAIN, SUB, planner(1, at=3))
        self.app.write_subagent(SUB, "sub-2", planner(1, at=4))

        self.assertEqual(main_conversation(self.app.root, SUB), MAIN)
        self.assertEqual(main_conversation(self.app.root, "sub-2"), MAIN)

    def test_a_loop_of_parents_ends(self) -> None:
        self.app.write_subagent("a", "b", planner(1, at=1))
        self.app.write_subagent("b", "a", planner(1, at=1))

        self.assertIn(main_conversation(self.app.root, "a"), {"a", "b"})

    def test_the_caller_parent_comes_from_the_environment(self) -> None:
        self.app.write_subagent(MAIN, SUB, planner(1, at=3))
        env = {"ANTIGRAVITY_CONVERSATION_ID": SUB, "ANTIGRAVITY_APP_DATA_DIR": str(self.app.root)}

        self.assertEqual(caller_parent(env), MAIN)
        self.assertIsNone(caller_parent({**env, "ANTIGRAVITY_CONVERSATION_ID": MAIN}))
        self.assertIsNone(caller_parent({"ANTIGRAVITY_CONVERSATION_ID": SUB}))
        self.assertIsNone(caller_parent({}))


class CommandSyncTests(SyncTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.repo = EvidenceRepo(self.tmp / "repo")
        self.ledger_path = ledger_path_for(self.repo.root)
        self.ws = Workspace(self.repo.root, None, self.ledger_path)
        self.env = {"ANTIGRAVITY_CONVERSATION_ID": MAIN, "ANTIGRAVITY_APP_DATA_DIR": str(self.app.root)}
        self.launch(SUB)
        self.finish()

    def events(self) -> list[tuple[str, str, str]]:
        return run_events(load(self.ledger_path))

    def test_the_main_conversation_records_the_runs_it_launched(self) -> None:
        sync_antigravity_runs(self.ws, self.env)

        self.assertEqual([event for event, _, _ in self.events()], ["start", "stop"])

    def test_a_subagent_records_the_runs_of_its_main_conversation(self) -> None:
        sync_antigravity_runs(self.ws, {**self.env, "ANTIGRAVITY_CONVERSATION_ID": SUB})

        self.assertEqual([event for event, _, _ in self.events()], ["start", "stop"])

    def test_without_the_conversation_or_the_app_dir_nothing_is_read(self) -> None:
        sync_antigravity_runs(self.ws, {})
        sync_antigravity_runs(self.ws, {"ANTIGRAVITY_CONVERSATION_ID": MAIN})
        sync_antigravity_runs(self.ws, {"ANTIGRAVITY_APP_DATA_DIR": str(self.app.root)})

        self.assertFalse(self.ledger_path.exists())

    def test_a_conversation_that_launched_nothing_leaves_no_ledger(self) -> None:
        self.app.write_transcript(MAIN, user_input(0, "go", 0))

        sync_antigravity_runs(self.ws, self.env)

        self.assertFalse(self.ledger_path.exists())

    def test_a_transcript_that_cannot_be_read_costs_one_line_and_no_exception(self) -> None:
        path = self.app.transcript_path(MAIN)
        path.write_text("not json\n", encoding="utf-8")
        err = io.StringIO()

        with redirect_stderr(err):
            sync_antigravity_runs(self.ws, self.env)

        self.assertEqual(len(err.getvalue().strip().splitlines()), 1)
        self.assertIn("not synced", err.getvalue())
        self.assertFalse(self.ledger_path.exists())

    def test_a_transcript_over_the_size_cap_costs_one_line_and_credits_nothing(self) -> None:
        path = self.app.transcript_path(MAIN)
        path.write_text('{"step_index": 1, "type": "USER_INPUT", "content": "hi"}\n', encoding="utf-8")
        err = io.StringIO()

        with redirect_stderr(err), unittest.mock.patch("craftsman.transcript.MAX_TRANSCRIPT_BYTES", 10):
            sync_antigravity_runs(self.ws, self.env)

        self.assertEqual(len(err.getvalue().strip().splitlines()), 1)
        self.assertIn("is larger than", err.getvalue())
        self.assertFalse(self.ledger_path.exists())

    def test_a_ledger_that_cannot_be_written_costs_one_line_and_no_exception(self) -> None:
        err = io.StringIO()
        with redirect_stderr(err), unittest.mock.patch.object(agy_sync, "mutate", side_effect=OSError("disk full")):
            sync_antigravity_runs(self.ws, self.env)

        self.assertIn("disk full", err.getvalue())

    def test_sync_from_stop_records_for_explicit_inputs_and_is_stable(self) -> None:
        sync_from_stop(self.repo.root, self.app.root, SUB)
        stored = self.ledger_path.read_bytes()

        sync_from_stop(self.repo.root, self.app.root, MAIN)

        self.assertEqual(self.ledger_path.read_bytes(), stored)

    def test_the_ledger_it_leaves_is_a_valid_ledger(self) -> None:
        sync_antigravity_runs(self.ws, self.env)
        save(self.ledger_path, load(self.ledger_path))

        self.assertEqual(len(agent_runs(load(self.ledger_path))), 1)


class TimeAxisTests(unittest.TestCase):
    def test_the_whole_second_is_a_millisecond_short_of_the_next(self) -> None:
        self.assertEqual(agy_sync.LAST_MILLISECOND_OF_A_SECOND, timedelta(milliseconds=999))
        self.assertEqual(AGY_EPOCH, parse_timestamp(at(0)))
        self.assertEqual(generic(1, "x", 3)["created_at"], "2026-01-01T00:00:03Z")


if __name__ == "__main__":
    unittest.main()
