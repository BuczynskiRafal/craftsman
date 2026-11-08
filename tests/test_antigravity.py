"""Tests for the Antigravity Stop hook (`craftsman.antigravity`, `scripts/craftsman-stop-gate.py`)."""

from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest import mock

from craftsman import antigravity, hook_errors
from craftsman.attribution import agent_runs
from craftsman.gate import MAX_CONTINUATIONS, GateOwner, GateState, Stage, load_state, state_path_for
from craftsman.ledger import ledger_path_for, load
from craftsman.records import Ledger, format_timestamp, parse_timestamp
from craftsman.review_guard import REVIEW_NOT_STARTED
from craftsman.scope import evaluate_antigravity_payload
from craftsman.workspace import ReviewMarker, arm_marker, read_marker

from tests import support
from tests.support import (
    REPO_ROOT,
    AgyApp,
    EvidenceRepo,
    agy_call,
    ask_question,
    coverage_note,
    launch_reviewers,
    open_gate,
    planner,
    user_input,
)

MAIN = "main-1"
SUB = "sub-1"
OTHER = "main-2"


def clock(seconds: float) -> datetime:
    return support.AGY_EPOCH + timedelta(seconds=seconds)


class AntigravityTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.repo = EvidenceRepo(self.tmp / "repo")
        self.app = AgyApp(self.tmp / "app")
        self.caller = GateOwner("antigravity", MAIN)

    def write_transcript(self, *entries: dict[str, Any], conversation: str = MAIN) -> None:
        self.app.write_transcript(conversation, *entries)

    def stop(self, conversation: str = MAIN, *, now: datetime | None = None, **extra: Any) -> tuple[dict[str, str], str]:
        payload = self.app.payload(conversation, self.repo.root, **{"terminationReason": "NO_TOOL_CALL", "fullyIdle": True, **extra})
        err = io.StringIO()
        with redirect_stderr(err):
            answer = antigravity.answer(payload, {}, now)
        return answer, err.getvalue()

    def stored(self) -> GateState | None:
        return load_state(state_path_for(self.repo.root))

    def ledger(self) -> Ledger:
        return load(ledger_path_for(self.repo.root))


class PayloadTests(unittest.TestCase):
    def test_the_conversation_is_the_conversation_id_else_the_session_directory(self) -> None:
        self.assertEqual(antigravity.session_id({"conversationId": "c-1", "artifactDirectoryPath": "/x/brain/c-2"}), "c-1")
        self.assertEqual(antigravity.session_id({"artifactDirectoryPath": "/x/brain/c-2"}), "c-2")
        self.assertEqual(antigravity.session_id({"conversationId": " ", "artifactDirectoryPath": "/x/brain/c-2"}), "c-2")
        self.assertIsNone(antigravity.session_id({}))
        self.assertIsNone(antigravity.session_id({"conversationId": 3}))

    def test_the_app_dir_is_the_grandparent_of_the_brain_directory(self) -> None:
        payload = {"artifactDirectoryPath": "/data/antigravity-ide/brain/c-1"}
        self.assertEqual(antigravity.app_dir(payload, {"ANTIGRAVITY_APP_DATA_DIR": "/elsewhere"}), Path("/data/antigravity-ide"))

    def test_the_app_dir_falls_back_to_the_environment_then_to_the_default(self) -> None:
        elsewhere = {"artifactDirectoryPath": "/work/repo/.gemini/artifacts"}
        self.assertEqual(antigravity.app_dir(elsewhere, {"ANTIGRAVITY_APP_DATA_DIR": "/env/app"}), Path("/env/app"))
        with mock.patch("craftsman.antigravity.user_home", return_value=Path("/home/u")):
            self.assertEqual(antigravity.app_dir({}, {}), Path("/home/u/.gemini/antigravity-cli"))
            self.assertEqual(antigravity.app_dir(elsewhere, {"ANTIGRAVITY_APP_DATA_DIR": "  "}), Path("/home/u/.gemini/antigravity-cli"))

    def test_the_stop_payload_keeps_the_transcript_path(self) -> None:
        payload = {
            "conversationId": "c-1",
            "workspacePaths": ["/w"],
            "artifactDirectoryPath": "/data/brain/c-1",
            "transcriptPath": "/data/brain/c-1/t.jsonl",
        }
        stop = antigravity.StopPayload.parse(payload, {})
        self.assertEqual((stop.conversation_id, stop.app_dir, stop.workspace, stop.transcript), ("c-1", Path("/data"), Path("/w"), Path("/data/brain/c-1/t.jsonl")))

    def test_a_payload_that_names_no_conversation_or_workspace_is_an_error(self) -> None:
        with self.assertRaises(antigravity.PayloadError):
            antigravity.StopPayload.parse({"workspacePaths": ["/w"]}, {})
        with self.assertRaises(antigravity.PayloadError):
            antigravity.StopPayload.parse({"conversationId": "c-1", "workspacePaths": []}, {})


class NoGateTests(AntigravityTestCase):
    def test_stop_without_gate_or_request_allows(self) -> None:
        self.write_transcript(user_input(1, "fix the typo", 0))
        self.repo.write_lines("app.py", 5)
        self.assertEqual(self.stop()[0], {"decision": "allow"})
        self.assertIsNone(self.stored())

    def test_pause_between_tool_calls_allows(self) -> None:
        open_gate(self.repo.root, self.repo.base_sha)
        self.repo.write_lines("app.py", 5)
        self.assertEqual(self.stop(terminationReason="TOOL_CALL")[0], {"decision": "allow"})

    def test_payload_without_termination_reason_is_reported(self) -> None:
        payload = {"fullyIdle": True, "conversationId": MAIN}
        err = io.StringIO()
        with redirect_stderr(err):
            answer = antigravity.answer(payload)

        self.assertEqual(answer, {"decision": "allow"})
        self.assertIn("needs `terminationReason` and `fullyIdle`", err.getvalue())

    def test_payload_without_workspace_allows_with_an_error(self) -> None:
        answer, err = self.stop(workspacePaths=[])
        self.assertEqual(answer, {"decision": "allow"})
        self.assertIn("The quality gate was not checked", err)

    def test_unreadable_transcript_allows_with_an_error(self) -> None:
        transcript = self.app.transcript_path(MAIN)
        transcript.mkdir(parents=True)
        answer, err = self.stop()
        self.assertEqual(answer, {"decision": "allow"})
        self.assertIn("The quality gate was not checked: cannot read the transcript", err)
        entries = hook_errors.read(self.repo.ledger_dir)
        self.assertEqual([(entry.kind, entry.source) for entry in entries], [("transcript", "antigravity-stop")])

    def test_a_transcript_over_the_size_cap_allows_with_an_error(self) -> None:
        transcript = self.app.transcript_path(MAIN)
        transcript.parent.mkdir(parents=True, exist_ok=True)
        transcript.write_text('{"step_index": 1, "type": "USER_INPUT", "content": "hi"}\n', encoding="utf-8")
        with mock.patch("craftsman.transcript.MAX_TRANSCRIPT_BYTES", 10):
            answer, err = self.stop()
        self.assertEqual(answer, {"decision": "allow"})
        self.assertIn("The quality gate was not checked", err)
        self.assertIn("is larger than", err)
        entries = hook_errors.read(self.repo.ledger_dir)
        self.assertEqual([(entry.kind, entry.source) for entry in entries], [("transcript", "antigravity-stop")])

    def test_invalid_gate_state_allows_with_an_error(self) -> None:
        path = state_path_for(self.repo.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"active": True}), encoding="utf-8")
        answer, err = self.stop()
        self.assertEqual(answer, {"decision": "allow"})
        self.assertIn("not a valid gate state", err)

    def test_the_stop_runs_inside_the_antigravity_stop_budget(self) -> None:
        seen: list[str] = []
        real = hook_errors.hook_run

        def spy(event: str, cwd: object) -> object:
            seen.append(event)
            return real(event, cwd)  # type: ignore[arg-type]

        self.write_transcript(user_input(1, "hello", 0))
        with mock.patch.object(hook_errors, "hook_run", spy):
            self.stop()
        self.assertEqual(seen, ["antigravity-stop"])

    def test_a_git_timeout_is_logged_and_the_agent_may_stop(self) -> None:
        from craftsman.diffscan import GitTimeoutError

        self.write_transcript(user_input(1, "hello", 0))
        with mock.patch("craftsman.antigravity.worktree_root", side_effect=GitTimeoutError("git timed out")):
            answer, _ = self.stop()
        self.assertEqual(answer, {"decision": "allow"})
        self.assertEqual([entry.kind for entry in hook_errors.read(self.repo.ledger_dir)], ["git-timeout"])


class RequestTests(AntigravityTestCase):
    def test_request_opens_a_gate_the_agent_did_not_open(self) -> None:
        self.write_transcript(user_input(1, "/craftsman --deep add totals", 0))
        self.repo.write_lines("app.py", 5)
        answer, _ = self.stop()
        self.assertEqual(answer["decision"], "continue")
        self.assertIn(f"1/{MAX_CONTINUATIONS}]", answer["reason"])
        state = self.stored()
        assert state is not None
        self.assertEqual((state.owner, state.deep, state.task), (self.caller, True, "add totals"))

    def test_later_deep_request_raises_an_active_gate(self) -> None:
        open_gate(self.repo.root, self.repo.base_sha, owner=self.caller, request_step=1)
        self.write_transcript(user_input(1, "/craftsman fix a", 0), user_input(4, "now /craftsman --deep", 5))
        self.repo.write_lines("app.py", 5)

        self.stop()

        state = self.stored()
        assert state is not None
        self.assertTrue(state.deep)

    def test_later_plain_request_leaves_an_active_gate_alone(self) -> None:
        open_gate(self.repo.root, self.repo.base_sha, owner=self.caller, request_step=1)
        self.write_transcript(user_input(1, "/craftsman fix a", 0), user_input(4, "/craftsman fix b", 5))
        self.repo.write_lines("app.py", 5)

        self.stop()

        state = self.stored()
        assert state is not None
        self.assertFalse(state.deep)

    def test_request_without_changes_opens_no_gate(self) -> None:
        self.write_transcript(user_input(1, "/craftsman add totals", 0))
        self.assertEqual(self.stop()[0], {"decision": "allow"})
        self.assertIsNone(self.stored())

    def test_question_about_craftsman_opens_no_gate(self) -> None:
        self.write_transcript(user_input(1, "What do you think of `/craftsman`? Just an opinion.", 0))
        self.repo.write_lines("app.py", 5)
        self.assertEqual(self.stop()[0], {"decision": "allow"})
        self.assertIsNone(self.stored())

    def test_request_handled_by_a_closed_gate_is_not_reopened(self) -> None:
        self.write_transcript(user_input(1, "/craftsman add totals", 0))
        open_gate(self.repo.root, self.repo.base_sha, owner=self.caller, active=False, stage=Stage.COMPLETED)
        self.repo.write_lines("app.py", 5)
        self.assertEqual(self.stop()[0], {"decision": "allow"})
        state = self.stored()
        assert state is not None
        self.assertFalse(state.active)

    def test_new_request_after_a_closed_gate_opens_a_new_one(self) -> None:
        open_gate(self.repo.root, self.repo.base_sha, owner=self.caller, active=False, request_step=1)
        self.write_transcript(user_input(1, "/craftsman first", 0), user_input(5, "/craftsman second", 9))
        self.repo.write_lines("app.py", 5)
        self.assertEqual(self.stop()[0]["decision"], "continue")
        state = self.stored()
        assert state is not None
        self.assertEqual((state.active, state.task, state.request_step), (True, "second", 5))


class OwnershipTests(AntigravityTestCase):
    def test_another_sessions_gate_is_left_alone(self) -> None:
        open_gate(self.repo.root, self.repo.base_sha, owner=GateOwner("claude", "s1"))
        self.repo.write_lines("app.py", 5)
        before = state_path_for(self.repo.root).read_bytes()
        self.assertEqual(self.stop()[0], {"decision": "allow"})
        self.assertEqual(state_path_for(self.repo.root).read_bytes(), before)

    def test_gate_opened_from_the_terminal_is_claimed(self) -> None:
        open_gate(self.repo.root, self.repo.base_sha)
        self.write_transcript(user_input(3, "go", 0))
        self.repo.write_lines("app.py", 5)
        self.assertEqual(self.stop()[0]["decision"], "continue")
        state = self.stored()
        assert state is not None
        self.assertEqual((state.owner, state.isolation_evidence, state.request_step), (self.caller, "transcript", 3))

    def test_a_gate_that_is_given_up_ends_the_review_guard_its_owner_outlived(self) -> None:
        open_gate(
            self.repo.root, self.repo.base_sha, owner=self.caller, request_step=1,
            forced_continuations=MAX_CONTINUATIONS, evidence_seen=10**6,
        )  # fmt: skip
        self.write_transcript(user_input(1, "go", 0))
        self.repo.write_lines("app.py", 5)
        arm_marker(ledger_path_for(self.repo.root), self.caller, clock(1), fresh=False)
        marker = read_marker(ledger_path_for(self.repo.root))
        assert marker is not None and marker.guarding

        answer, err = self.stop()

        self.assertEqual(answer, {"decision": "allow"})
        self.assertIn("Craftsman gate not satisfied", err)
        stored = self.stored()
        assert stored is not None
        self.assertFalse(stored.active)
        ended = read_marker(ledger_path_for(self.repo.root))
        assert ended is not None
        self.assertFalse(ended.guarding)

    def test_pending_question_lets_the_agent_stop(self) -> None:
        open_gate(self.repo.root, self.repo.base_sha)
        self.write_transcript(user_input(1, "go", 0), ask_question(2))
        self.repo.write_lines("app.py", 5)
        self.assertEqual(self.stop()[0], {"decision": "allow"})

    def test_a_subagent_is_not_served_by_the_gate_of_its_parent(self) -> None:
        open_gate(self.repo.root, self.repo.base_sha, owner=self.caller, request_step=1)
        self.repo.write_lines("app.py", 5)
        self.write_transcript(user_input(1, "go", 0))
        self.app.write_subagent(MAIN, SUB, planner(1, at=3))
        before = state_path_for(self.repo.root).read_bytes()

        self.assertEqual(self.stop(SUB)[0], {"decision": "allow"})
        self.assertEqual(state_path_for(self.repo.root).read_bytes(), before)


class HuntTestCase(AntigravityTestCase):
    """A review driven through the real CLI as an Antigravity conversation, with transcripts on the same clock.

    The transcripts' clock starts at the second the round was prepared (`support.AGY_EPOCH` is patched), so a
    hunter launched at second 0 is launched in the second of the `prepare`.
    """

    def setUp(self) -> None:
        super().setUp()
        self.repo.write_lines("app.py", 5)
        self.write_transcript(user_input(0, "/craft-review check the totals", -5))
        self.pre_tool(MAIN, "craftsman review prepare")
        self.cli(MAIN, "review", "prepare")
        prepared = parse_timestamp(self.ledger().rounds[-1].prepared_at)
        patcher = mock.patch.object(support, "AGY_EPOCH", datetime.fromtimestamp(int(prepared.timestamp()), timezone.utc))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.launched: list[str] = []
        self.write_transcript(user_input(0, "/craft-review check the totals", -5))

    def cli(self, conversation: str, *args: str) -> subprocess.CompletedProcess[str]:
        env = {"ANTIGRAVITY_CONVERSATION_ID": conversation, "ANTIGRAVITY_APP_DATA_DIR": str(self.app.root), "ANTIGRAVITY_AGENT": "1"}
        return self.repo.cli(*args, env_extra=env)

    def pre_tool(self, conversation: str, command: str, *, now: datetime | None = None) -> tuple[str, str | None]:
        payload = self.app.payload(
            conversation, self.repo.root, toolCall={"name": "run_command", "args": {"CommandLine": command, "Cwd": str(self.repo.root)}}
        )
        return evaluate_antigravity_payload(payload, {}, now)

    def slots(self) -> list[dict[str, Any]]:
        return [slot for slot in json.loads(self.cli(MAIN, "review", "show", "--json").stdout)["slots"]]

    def launch(self, *sub_ids: str, at: float = 0) -> None:
        """The main conversation launches hunters (acknowledged at second `at` of the review's clock)."""
        self.launched.extend(sub_ids)
        self.write_transcript(
            user_input(0, "/craft-review check the totals", -5), *launch_reviewers(1, *self.launched, at=at)
        )

    def working(self, sub_id: str, *, at: float = 5) -> None:
        self.app.write_subagent(MAIN, sub_id, planner(1, agy_call("run_command", CommandLine="ls"), at=at))

    def done(self, sub_id: str, *, at: float = 60, resumed_at: float | None = None) -> None:
        """The subagent finished its turn at second `at` (a resumed one: it was resumed at `resumed_at` first)."""
        entries = [planner(1, agy_call("run_command", CommandLine="ls"), at=5)]
        if resumed_at is not None:
            entries += [support.parent_message(2, MAIN, resumed_at)]
        self.app.write_subagent(MAIN, sub_id, *entries, planner(len(entries) + 1, at=at))

    def brief(self, sub_id: str, slot: str) -> None:
        self.cli(sub_id, "review", "brief", slot)

    def cover(self, sub_id: str, slot: dict[str, Any]) -> None:
        missing = slot["missing"] or slot["hunks"]
        self.cli(sub_id, "review", "cover", "--hunter", slot["id"], "--hunks", ",".join(missing), "--note", coverage_note(missing))

    def events(self, event: str | None = None) -> list[tuple[str, str, str]]:
        return [(e.event, e.agent_id, e.at) for e in self.ledger().agents if event in (None, e.event)]


class MarkerSyncTests(AntigravityTestCase):
    """`sync_marker_from_user_input`: Antigravity has no prompt hook, so the transcript's user input is its prompt."""

    def setUp(self) -> None:
        super().setUp()
        self.ledger_path = ledger_path_for(self.repo.root)

    def sync(self, *entries: dict[str, Any], caller: GateOwner | None = None, now: float = 100) -> None:
        self.write_transcript(*entries)
        antigravity.sync_marker_from_user_input(
            self.repo.root, self.app.root, MAIN, caller or self.caller, clock(now)
        )

    def marker(self) -> ReviewMarker | None:
        return read_marker(self.ledger_path)

    def test_a_review_request_arms_a_fresh_review_at_the_time_of_the_request(self) -> None:
        self.sync(user_input(1, "/craft-review --deep check the totals", 7))

        marker = self.marker()
        assert marker is not None
        self.assertEqual(
            (marker.platform, marker.session_id, marker.at, marker.touched_at, marker.deep, marker.task, marker.forced),
            ("antigravity", MAIN, "2026-01-01T00:00:07.000Z", None, True, "check the totals", 0),
        )

    def test_the_same_request_never_arms_twice(self) -> None:
        self.sync(user_input(1, "/craft-review check", 7))
        first = (self.repo.ledger_dir / "review_hooks.json").read_bytes()

        self.sync(user_input(1, "/craft-review check", 7), now=500)

        self.assertEqual((self.repo.ledger_dir / "review_hooks.json").read_bytes(), first)

    def test_a_request_older_than_the_marker_does_not_arm_over_it(self) -> None:
        arm_marker(self.ledger_path, self.caller, clock(50), fresh=False)

        self.sync(user_input(1, "/craft-review --deep check", 7))

        marker = self.marker()
        assert marker is not None
        self.assertEqual((marker.at, marker.deep), ("2026-01-01T00:00:50.000Z", False))

    def test_a_newer_request_replaces_a_finished_review(self) -> None:
        self.sync(user_input(1, "/craft-review check", 7))
        self.sync(user_input(1, "/craft-review check", 7), user_input(4, "/craft-review again", 90))

        marker = self.marker()
        assert marker is not None
        self.assertEqual((marker.at, marker.task), ("2026-01-01T00:01:30.000Z", "again"))

    def test_a_later_message_that_is_no_request_ends_the_guard(self) -> None:
        self.sync(user_input(1, "/craft-review check", 7))

        self.sync(user_input(1, "/craft-review check", 7), user_input(4, "thanks, now explain the diff", 90))

        marker = self.marker()
        assert marker is not None
        self.assertFalse(marker.guarding)

    def test_a_message_older_than_the_last_touch_leaves_the_guard(self) -> None:
        arm_marker(self.ledger_path, self.caller, clock(10), fresh=False)
        arm_marker(self.ledger_path, self.caller, clock(100), fresh=False)  # touched at 100

        self.sync(user_input(1, "an earlier remark", 60))

        marker = self.marker()
        assert marker is not None
        self.assertTrue(marker.guarding)

    def test_a_continuation_prompt_is_no_user_moving_on(self) -> None:
        self.sync(user_input(1, "/craft-review check", 7))

        self.sync(user_input(1, "/craft-review check", 7), user_input(4, "[CRAFTSMAN REVIEW 2/6] review incomplete", 90))

        marker = self.marker()
        assert marker is not None
        self.assertTrue(marker.guarding)

    def test_a_wrapped_continuation_prompt_is_no_user_moving_on(self) -> None:
        self.sync(user_input(1, "/craft-review check", 7))
        wrapped = "<USER_REQUEST>\n[CRAFTSMAN REVIEW 2/6] review incomplete\n</USER_REQUEST>"

        self.sync(user_input(1, "/craft-review check", 7), user_input(4, wrapped, 90))

        marker = self.marker()
        assert marker is not None
        self.assertTrue(marker.guarding)

    def test_the_guard_of_another_session_is_not_ended(self) -> None:
        arm_marker(self.ledger_path, GateOwner("antigravity", OTHER), clock(5), fresh=False)

        self.sync(user_input(1, "hello there", 90))

        marker = self.marker()
        assert marker is not None
        self.assertEqual((marker.session_id, marker.guarding), (OTHER, True))

    def test_nothing_happens_without_a_time_or_a_user_input(self) -> None:
        self.sync(user_input(1, "/craft-review check"))
        self.assertIsNone(self.marker())
        self.sync(ask_question(1))
        self.assertIsNone(self.marker())

    def test_a_review_request_in_a_subagent_message_is_not_a_request(self) -> None:
        self.sync(support.parent_message(0, OTHER, 7))
        self.assertIsNone(self.marker())


class ReviewGuardTests(HuntTestCase):
    def test_a_review_that_began_with_the_request_is_held_until_it_has_hunters(self) -> None:
        answer, _ = self.stop(now=clock(10))
        self.assertEqual(answer["decision"], "continue")
        self.assertIn("launch them now, all in one step: W1-1", answer["reason"])
        self.assertIn("[CRAFTSMAN REVIEW 1/", answer["reason"])

    def test_a_replay_with_prepare_and_no_invoke_subagent_is_blocked_with_the_launch_text(self) -> None:
        answer, _ = self.stop(now=clock(10))
        self.assertIn("invoke_subagent, TypeName self, Role craftsman-reviewer", answer["reason"])
        answer, _ = self.stop(now=clock(11))
        self.assertIn("[CRAFTSMAN REVIEW 2/", answer["reason"])

    def test_the_guard_waits_while_the_hunters_it_launched_work(self) -> None:
        self.launch(SUB)
        self.working(SUB)

        answer, err = self.stop(now=clock(10))

        self.assertEqual(answer, {"decision": "allow"})
        self.assertIn("craftsman-reviewer", err)  # the notice names what is running
        self.assertEqual([event for event, _, _ in self.events()], ["start"])

    def test_the_stop_records_a_finished_hunter_and_blocks_for_what_it_left(self) -> None:
        self.launch(SUB)
        self.brief(SUB, self.slots()[0]["id"])
        self.done(SUB, at=60)

        answer, _ = self.stop(now=clock(70))

        self.assertEqual(answer["decision"], "continue")
        self.assertEqual([event for event, _, _ in self.events()], ["start", "stop"])
        self.assertEqual(self.events("stop")[0][2], (support.AGY_EPOCH + timedelta(seconds=60)).strftime("%Y-%m-%dT%H:%M:%S.999Z"))

    def test_a_question_to_the_user_lets_the_agent_stop(self) -> None:
        self.write_transcript(user_input(0, "/craft-review check the totals", -5), ask_question(2))
        self.assertEqual(self.stop(now=clock(10))[0], {"decision": "allow"})

    def test_a_newer_message_that_is_no_request_ends_the_guard(self) -> None:
        self.write_transcript(
            user_input(0, "/craft-review check the totals", -5), user_input(5, "never mind, explain the diff", 30)
        )
        self.assertEqual(self.stop(now=clock(40))[0], {"decision": "allow"})
        marker = read_marker(ledger_path_for(self.repo.root))
        assert marker is not None
        self.assertFalse(marker.guarding)

    def test_a_new_request_in_a_long_session_arms_a_fresh_review(self) -> None:
        self.write_transcript(
            user_input(0, "/craft-review check the totals", -5),
            user_input(5, "/craft-review --deep check the totals again", 100),
        )
        answer, _ = self.stop(now=clock(110))
        marker = read_marker(ledger_path_for(self.repo.root))
        assert marker is not None
        self.assertEqual((answer["decision"], marker.deep, marker.task, marker.forced), ("continue", True, "check the totals again", 1))
        self.assertIn(REVIEW_NOT_STARTED, answer["reason"])  # the new request began a new review: nothing was prepared for it

    def test_a_request_with_no_prepare_is_blocked_with_the_prepare_text(self) -> None:
        other = EvidenceRepo(self.tmp / "other")
        other.write_lines("app.py", 5)
        self.app.write_transcript(OTHER, user_input(0, "/craft-review check", 0))
        payload = self.app.payload(OTHER, other.root, terminationReason="NO_TOOL_CALL", fullyIdle=True)
        with redirect_stderr(io.StringIO()):
            answer = antigravity.answer(payload, {}, clock(5))
        self.assertEqual(answer["decision"], "continue")
        self.assertIn(REVIEW_NOT_STARTED, answer["reason"])


class DueWaveTests(HuntTestCase):
    """E8: the Stop stores the waves that are due before it judges, so its block lists the next wave's slots."""

    def hunt_everything(self) -> None:
        for number in range(1, 10):
            pending = [slot for slot in self.slots() if slot["state"] != "done"]
            if not pending:
                return
            sub_id = f"hunter-{number}"
            self.launch(sub_id)
            self.working(sub_id)
            self.brief(sub_id, pending[0]["id"])
            self.cover(sub_id, pending[0])
            self.done(sub_id, at=20 + number)

    def stored_slots(self) -> list[str]:
        plan = json.loads((self.repo.ledger_dir / "ledger.json").read_text(encoding="utf-8"))["rounds"][-1]["plan"]
        return [slot["id"] for slot in plan["slots"]]

    def make_a_wave_due_but_not_stored(self) -> None:
        self.hunt_everything()
        self.cli(
            MAIN, "review", "add", "--lens", "contracts", "--file", "app.py", "--line", "2", "--severity", "major",
            "--title", "Values are used without a check", "--scenario", "value_1 is read before it is set", "--hunter", "lead",
        )  # fmt: skip
        self.cli(MAIN, "review", "verdict", "F1", "--as", "probable", "--evidence", "static: app.py:2 value_1 is read before it is set -> raises")
        ledger = json.loads((self.repo.ledger_dir / "ledger.json").read_text(encoding="utf-8"))
        plan = ledger["rounds"][-1]["plan"]
        plan["slots"] = [slot for slot in plan["slots"] if not slot["id"].startswith("W2-")]
        plan["waves"] = []
        (self.repo.ledger_dir / "ledger.json").write_text(json.dumps(ledger), encoding="utf-8")

    def test_the_review_guard_stores_the_due_wave_before_it_blocks(self) -> None:
        self.make_a_wave_due_but_not_stored()
        self.assertFalse([slot for slot in self.stored_slots() if slot.startswith("W2-")])

        answer, _ = self.stop(now=clock(200))

        self.assertEqual(answer["decision"], "continue")
        self.assertIn("W2-", answer["reason"])
        self.assertTrue([slot for slot in self.stored_slots() if slot.startswith("W2-")])

    def test_the_gates_stop_stores_the_due_wave_before_it_blocks(self) -> None:
        self.make_a_wave_due_but_not_stored()
        open_gate(self.repo.root, self.repo.base_sha, owner=self.caller, request_step=0)

        answer, _ = self.stop(now=clock(200))

        self.assertEqual(answer["decision"], "continue")
        self.assertIn("W2-", answer["reason"])
        self.assertTrue([slot for slot in self.stored_slots() if slot.startswith("W2-")])


class BriefParentTests(HuntTestCase):
    """E7: an Antigravity brief ends with the exact call that reports to the conversation that launched the hunter."""

    def test_a_hunter_brief_names_its_parent(self) -> None:
        self.launch(SUB)
        self.working(SUB)

        brief = self.cli(SUB, "review", "brief", self.slots()[0]["id"]).stdout

        self.assertIn(f"call send_message with Recipient `{MAIN}` and a 2-3 line Message (slot, hunks covered, findings)", brief)

    def test_a_brief_read_where_the_parent_is_unknown_keeps_the_generic_sentence(self) -> None:
        env = {"ANTIGRAVITY_CONVERSATION_ID": SUB, "ANTIGRAVITY_AGENT": "1"}  # no app data dir: nothing to resolve it by

        brief = self.repo.cli("review", "brief", self.slots()[0]["id"], env_extra=env).stdout

        self.assertIn("send that summary to your parent with send_message (Recipient: the conversation that launched you)", brief)
        self.assertNotIn("Recipient `", brief)

    def test_a_verifier_brief_names_its_parent(self) -> None:
        self.cli(
            MAIN, "review", "add", "--lens", "contracts", "--file", "app.py", "--line", "2", "--severity", "major",
            "--title", "Values are used without a check", "--scenario", "value_1 is read before it is set", "--hunter", "lead",
        )  # fmt: skip
        self.app.write_subagent(MAIN, "ver-1", planner(1, agy_call("run_command", CommandLine="ls"), at=5), role="craftsman-verifier")

        brief = self.cli("ver-1", "review", "brief", "--verifier", "F1").stdout

        self.assertIn(f"call send_message with Recipient `{MAIN}` and a 2-3 line Message (the findings judged", brief)

    def test_the_main_conversation_gets_no_parent(self) -> None:
        # The policy denies the main conversation a brief; the command itself still tells it apart from a subagent.
        brief = self.cli(MAIN, "review", "brief", self.slots()[0]["id"]).stdout

        self.assertIn("send that summary to your parent with send_message (Recipient: the conversation that launched you)", brief)


class SubagentStopTests(HuntTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.slot = self.slots()[0]
        self.launch(SUB)
        self.working(SUB)
        self.brief(SUB, self.slot["id"])

    def test_a_hunter_that_left_its_slot_unfinished_is_sent_back_once(self) -> None:
        self.done(SUB, at=60)

        first, _ = self.stop(SUB, now=clock(61))

        self.assertEqual(first["decision"], "continue")
        self.assertIn(f"Your slot {self.slot['id']} is not finished: cover hunks", first["reason"])
        self.assertIn(f"craftsman review cover --hunter {self.slot['id']}", first["reason"])
        self.assertIn("send your 2-3 line summary to your parent with send_message", first["reason"])
        self.assertEqual([event for event, _, _ in self.events()], ["start", "nudge"])  # not stopped: its stop is not stored

        self.done(SUB, at=90, resumed_at=70)
        second, _ = self.stop(SUB, now=clock(91))

        self.assertEqual(second, {"decision": "allow"})
        self.assertEqual([event for event, _, _ in self.events()], ["nudge", "start", "stop"])  # a run's events are written together

    def test_the_nudge_names_the_hunks_still_missing(self) -> None:
        self.done(SUB, at=60)

        first, _ = self.stop(SUB, now=clock(61))

        missing = self.slot["missing"] or self.slot["hunks"]
        self.assertIn(f"--hunks {missing[0]}", first["reason"])

    def test_a_hunter_that_finished_its_slot_is_let_go_and_recorded(self) -> None:
        self.cover(SUB, self.slot)
        self.done(SUB, at=60)

        answer, _ = self.stop(SUB, now=clock(61))

        self.assertEqual(answer, {"decision": "allow"})
        self.assertEqual([event for event, _, _ in self.events()], ["start", "stop"])

    def test_a_hunter_that_claimed_no_slot_is_let_go(self) -> None:
        self.launch("sub-2")
        self.working("sub-2")
        self.done("sub-2", at=60)

        self.assertEqual(self.stop("sub-2", now=clock(61))[0], {"decision": "allow"})

    def test_a_verifier_is_let_go(self) -> None:
        self.app.write_subagent(MAIN, "ver-1", planner(1, at=60), role="craftsman-verifier")
        self.write_transcript(
            user_input(0, "/craft-review check the totals", -5),
            *launch_reviewers(1, SUB, at=0),
            *launch_reviewers(4, "ver-1", at=1, role="craftsman-verifier"),
        )

        self.assertEqual(self.stop("ver-1", now=clock(61))[0], {"decision": "allow"})

    def test_a_subagent_the_parent_does_not_know_is_let_go(self) -> None:
        self.app.write_subagent(MAIN, "stray", planner(1, at=60))

        self.assertEqual(self.stop("stray", now=clock(61))[0], {"decision": "allow"})

    def test_a_subagent_whose_launch_is_not_acknowledged_yet_is_let_go(self) -> None:
        self.write_transcript(user_input(0, "/craft-review check the totals", -5))
        self.done(SUB, at=60)

        self.assertEqual(self.stop(SUB, now=clock(61))[0], {"decision": "allow"})

    def test_a_subagent_whose_last_entry_is_not_written_yet_is_judged_as_stopping_now(self) -> None:
        # Its Stop hook fires at the end of its turn: a transcript that still ends in a tool call is a flush that
        # has not happened, and must not be a way to skip the slot check.
        answer, _ = self.stop(SUB, now=clock(10))

        self.assertEqual(answer["decision"], "continue")
        self.assertIn(f"Your slot {self.slot['id']} is not finished", answer["reason"])
        self.assertEqual([event for event, _, _ in self.events()], ["start", "nudge"])  # the stop made up for the judgement is not stored

    def test_a_subagent_whose_last_entry_is_not_written_yet_and_who_owes_nothing_is_let_go_without_a_stop(self) -> None:
        self.cover(SUB, self.slot)

        self.assertEqual(self.stop(SUB, now=clock(10))[0], {"decision": "allow"})
        self.assertEqual([event for event, _, _ in self.events()], ["start"])  # the transcript decides when it stopped

    def test_a_nudge_remembers_the_last_step_of_the_transcript(self) -> None:
        self.done(SUB, at=60)

        self.stop(SUB, now=clock(61))

        (nudge,) = [event for event in self.ledger().agents if event.event == "nudge"]
        self.assertEqual(nudge.last_step, 2)

    def test_a_hunter_that_ignores_the_nudge_and_ends_its_turn_in_the_same_second_is_stopped(self) -> None:
        self.done(SUB, at=60)
        first, _ = self.stop(SUB, now=clock(60.494))
        self.assertEqual(first["decision"], "continue")

        # no new slot work: the response after the nudge is stamped with the second it started in
        self.app.write_subagent(
            MAIN, SUB, planner(1, agy_call("run_command", CommandLine="ls"), at=5), planner(2, at=60), planner(3, at=60)
        )
        second, _ = self.stop(SUB, now=clock(60.9))

        self.assertEqual(second, {"decision": "allow"})
        self.assertEqual(
            [(event, at) for event, _, at in self.events() if event == "stop"],
            [("stop", format_timestamp(clock(60.999)))],
        )
        (run,) = [run for run in agent_runs(self.ledger()) if run.agent_id == SUB]
        self.assertIsNotNone(run.stopped)

    def test_the_stop_of_a_subagent_does_not_arm_or_end_the_guard_of_its_parent(self) -> None:
        before = read_marker(ledger_path_for(self.repo.root))
        self.done(SUB, at=60)
        self.stop(SUB, now=clock(61))
        self.assertEqual(read_marker(ledger_path_for(self.repo.root)), before)


class RunRecordTests(AntigravityTestCase):
    def setUp(self) -> None:
        super().setUp()
        open_gate(self.repo.root, self.repo.base_sha, owner=self.caller, request_step=1)
        self.repo.write_lines("app.py", 5)

    def reviewer_stops(self) -> list[tuple[str, str, str]]:
        return [(e.platform, e.agent_id, e.at) for e in self.ledger().agents if e.event == "stop"]

    def launch_and_finish(self, finished: float = 60) -> None:
        self.write_transcript(user_input(1, "go", 0), *launch_reviewers(2, SUB, at=5))
        self.app.write_subagent(MAIN, SUB, planner(1, at=finished))

    def test_finished_reviewer_is_recorded_when_its_run_ended(self) -> None:
        self.launch_and_finish(60)
        self.stop(now=clock(100))
        self.assertEqual(self.reviewer_stops(), [("antigravity", SUB, "2026-01-01T00:01:00.999Z")])

    def test_finished_reviewer_is_recorded_once(self) -> None:
        self.launch_and_finish()
        self.stop(now=clock(100))
        stored = (self.repo.ledger_dir / "ledger.json").read_bytes()
        self.stop(now=clock(101))
        self.assertEqual((self.repo.ledger_dir / "ledger.json").read_bytes(), stored)

    def test_a_killed_reviewer_is_recorded_as_stopped(self) -> None:
        self.write_transcript(user_input(1, "go", 0), *launch_reviewers(2, SUB, at=5))
        self.app.write_subagent(MAIN, SUB, planner(1, at=7), state="SUBAGENT_STATE_KILLED", killed_at=30)
        self.stop(now=clock(100))
        self.assertEqual(len(self.reviewer_stops()), 1)

    def test_a_reviewer_without_a_launch_acknowledgement_is_not_recorded(self) -> None:
        self.write_transcript(user_input(1, "go", 0))
        self.app.write_subagent(MAIN, SUB, planner(1, at=60))
        self.stop(now=clock(100))
        self.assertEqual(self.reviewer_stops(), [])

    def test_the_stop_reads_the_transcript_the_payload_names(self) -> None:
        elsewhere = self.tmp / "elsewhere.jsonl"
        support.write_jsonl(elsewhere, user_input(1, "/craftsman --deep add totals", 0))
        self.repo.write_lines("app.py", 5)
        (self.repo.ledger_dir / "craftsman_state.json").unlink()

        answer, _ = self.stop(transcriptPath=str(elsewhere))

        self.assertEqual(answer["decision"], "continue")
        state = self.stored()
        assert state is not None
        self.assertTrue(state.deep)


class EntryPointTests(unittest.TestCase):
    def test_unreadable_input_allows_with_an_error(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch("sys.stdin", io.StringIO("{not json")),
            mock.patch("sys.stdout", out),
            redirect_stderr(err),
        ):
            self.assertEqual(antigravity.main(), 0)
        self.assertEqual(json.loads(out.getvalue()), {"decision": "allow"})
        self.assertIn("unreadable hook input", err.getvalue())

    def test_script_answers_a_stop_payload(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "craftsman-stop-gate.py")],
            input=json.dumps({"terminationReason": "TOOL_CALL"}),
            capture_output=True,
            encoding="utf-8",
            check=True,
            timeout=30,
        )
        self.assertEqual(json.loads(proc.stdout), {"decision": "allow"})


if __name__ == "__main__":
    unittest.main()
