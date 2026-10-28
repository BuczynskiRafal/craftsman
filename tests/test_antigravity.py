"""Tests for the Antigravity Stop hook (`craftsman.antigravity`, `scripts/craftsman-stop-gate.py`)."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest import mock

from craftsman import antigravity
from craftsman.gate import MAX_CONTINUATIONS, GateOwner, GateState, Stage, load_state, state_path_for
from craftsman.ledger import ledger_path_for, load
from craftsman.records import format_timestamp

from tests.support import (
    REPO_ROOT,
    REVIEWER,
    EvidenceRepo,
    ask_question,
    invoke_subagent,
    open_gate,
    subagent_ack,
    subagent_done,
    user_input,
    write_jsonl,
)

SESSION = "conv-1"
REVIEWER_ID = "sub-1"


class AntigravityTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.repo = EvidenceRepo(self.tmp / "repo")
        self.brain = self.tmp / "brain"
        self.session_dir = self.brain / SESSION
        self.caller = GateOwner("antigravity", SESSION)

    def write_transcript(self, *entries: dict[str, Any], session_dir: Path | None = None) -> None:
        write_jsonl((session_dir or self.session_dir) / antigravity.TRANSCRIPT, *entries)

    def stop(self, **extra: Any) -> tuple[dict[str, str], str]:
        payload = {
            "terminationReason": "NO_TOOL_CALL",
            "fullyIdle": True,
            "artifactDirectoryPath": str(self.session_dir),
            "workspacePaths": [str(self.repo.root)],
            **extra,
        }
        err = io.StringIO()
        with redirect_stderr(err):
            answer = antigravity.answer(payload)
        return answer, err.getvalue()

    def stored(self) -> GateState | None:
        return load_state(state_path_for(self.repo.root))


class NoGateTests(AntigravityTestCase):
    def test_stop_without_gate_or_request_allows(self) -> None:
        self.write_transcript(user_input(1, "fix the typo"))
        self.repo.write_lines("app.py", 5)
        self.assertEqual(self.stop()[0], {"decision": "allow"})
        self.assertIsNone(self.stored())

    def test_pause_between_tool_calls_allows(self) -> None:
        open_gate(self.repo.root, self.repo.base_sha, tier="quick")
        self.repo.write_lines("app.py", 5)
        self.assertEqual(self.stop(terminationReason="TOOL_CALL")[0], {"decision": "allow"})

    def test_payload_without_termination_reason_is_reported(self) -> None:
        payload = {"fullyIdle": True, "artifactDirectoryPath": str(self.session_dir)}
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
        (self.session_dir / antigravity.TRANSCRIPT).mkdir(parents=True)
        answer, err = self.stop()
        self.assertEqual(answer, {"decision": "allow"})
        self.assertIn("The quality gate was not checked: cannot read the transcript", err)

    def test_invalid_gate_state_allows_with_an_error(self) -> None:
        path = state_path_for(self.repo.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"active": True}), encoding="utf-8")
        answer, err = self.stop()
        self.assertEqual(answer, {"decision": "allow"})
        self.assertIn("not a valid gate state", err)


class RequestTests(AntigravityTestCase):
    def test_request_opens_a_gate_the_agent_did_not_open(self) -> None:
        self.write_transcript(user_input(1, "/craftsman --deep add totals"))
        self.repo.write_lines("app.py", 5)
        answer, _ = self.stop()
        self.assertEqual(answer["decision"], "continue")
        self.assertIn(f"1/{MAX_CONTINUATIONS}]", answer["reason"])
        state = self.stored()
        assert state is not None
        self.assertEqual((state.owner, state.tier, state.task), (self.caller, "deep", "add totals"))

    def test_request_without_changes_opens_no_gate(self) -> None:
        self.write_transcript(user_input(1, "/craftsman add totals"))
        self.assertEqual(self.stop()[0], {"decision": "allow"})
        self.assertIsNone(self.stored())

    def test_question_about_craftsman_opens_no_gate(self) -> None:
        self.write_transcript(user_input(1, "Co sądzisz o /craftsman? Tylko opinia."))
        self.repo.write_lines("app.py", 5)
        self.assertEqual(self.stop()[0], {"decision": "allow"})
        self.assertIsNone(self.stored())

    def test_request_handled_by_a_closed_gate_is_not_reopened(self) -> None:
        self.write_transcript(user_input(1, "/craftsman add totals"))
        open_gate(
            self.repo.root, self.repo.base_sha, owner=self.caller, active=False, stage=Stage.COMPLETED
        )
        self.repo.write_lines("app.py", 5)
        self.assertEqual(self.stop()[0], {"decision": "allow"})
        state = self.stored()
        assert state is not None
        self.assertFalse(state.active)

    def test_new_request_after_a_closed_gate_opens_a_new_one(self) -> None:
        open_gate(self.repo.root, self.repo.base_sha, owner=self.caller, active=False, request_step=1)
        self.write_transcript(user_input(1, "/craftsman first"), user_input(5, "/craftsman second"))
        self.repo.write_lines("app.py", 5)
        self.assertEqual(self.stop()[0]["decision"], "continue")
        state = self.stored()
        assert state is not None
        self.assertEqual((state.active, state.task, state.request_step), (True, "second", 5))


class OwnershipTests(AntigravityTestCase):
    def test_another_sessions_gate_is_left_alone(self) -> None:
        open_gate(self.repo.root, self.repo.base_sha, tier="quick", owner=GateOwner("claude", "s1"))
        self.repo.write_lines("app.py", 5)
        before = state_path_for(self.repo.root).read_bytes()
        self.assertEqual(self.stop()[0], {"decision": "allow"})
        self.assertEqual(state_path_for(self.repo.root).read_bytes(), before)

    def test_gate_opened_from_the_terminal_is_claimed(self) -> None:
        open_gate(self.repo.root, self.repo.base_sha, tier="quick")
        self.write_transcript(user_input(3, "go"))
        self.repo.write_lines("app.py", 5)
        self.assertEqual(self.stop()[0]["decision"], "continue")
        state = self.stored()
        assert state is not None
        self.assertEqual(
            (state.owner, state.isolation_evidence, state.request_step), (self.caller, "transcript", 3)
        )

    def test_pending_question_lets_the_agent_stop(self) -> None:
        open_gate(self.repo.root, self.repo.base_sha, tier="quick")
        self.write_transcript(user_input(1, "go"), ask_question(2))
        self.repo.write_lines("app.py", 5)
        self.assertEqual(self.stop()[0], {"decision": "allow"})

    def test_passing_ledger_closes_the_gate(self) -> None:
        open_gate(self.repo.root, self.repo.base_sha, tier="quick")
        self.repo.write_lines("app.py", 5)
        self.repo.record_full_evidence("quick")
        self.assertEqual(self.stop()[0], {"decision": "allow"})
        state = self.stored()
        assert state is not None
        self.assertEqual(state.stage, Stage.COMPLETED)


class ReviewerRecordTests(AntigravityTestCase):
    def setUp(self) -> None:
        super().setUp()
        open_gate(self.repo.root, self.repo.base_sha, tier="standard", owner=self.caller, request_step=1)
        self.repo.write_lines("app.py", 5)

    def finish_reviewer(self, sub_id: str = REVIEWER_ID) -> int:
        """Write the subagent's transcript, last modified after the round; return that time."""
        subagent_transcript = self.brain / sub_id / antigravity.TRANSCRIPT
        subagent_transcript.parent.mkdir(parents=True, exist_ok=True)
        subagent_transcript.write_text("", encoding="utf-8")
        finished = int(time.time()) + 60
        os.utime(subagent_transcript, (finished, finished))
        return finished

    def reviewer_stops(self) -> list[tuple[str, str, str]]:
        ledger = load(ledger_path_for(self.repo.root))
        return [
            (event.platform, event.agent_id, event.at)
            for event in ledger.agents
            if event.event == "stop"
        ]

    def test_finished_reviewer_is_recorded_when_its_run_ended(self) -> None:
        self.write_transcript(
            user_input(1, "go"),
            invoke_subagent(2, REVIEWER),
            subagent_ack(3, REVIEWER_ID),
            subagent_done(4, REVIEWER_ID),
        )
        finished = self.finish_reviewer()
        self.stop()
        finished_at = format_timestamp(datetime.fromtimestamp(finished, timezone.utc))
        self.assertEqual(self.reviewer_stops(), [("antigravity", REVIEWER_ID, finished_at)])

    def test_finished_reviewer_is_recorded_once(self) -> None:
        self.write_transcript(
            user_input(1, "go"),
            invoke_subagent(2, REVIEWER),
            subagent_ack(3, REVIEWER_ID),
            subagent_done(4, REVIEWER_ID),
        )
        self.finish_reviewer()
        self.stop()
        self.stop()
        self.assertEqual(len(self.reviewer_stops()), 1)

    def test_recorded_reviewer_proves_an_isolated_review(self) -> None:
        self.repo.record_full_evidence("standard")
        self.write_transcript(
            user_input(1, "go"),
            invoke_subagent(2, REVIEWER),
            subagent_ack(3, REVIEWER_ID),
            subagent_done(4, REVIEWER_ID),
        )
        self.finish_reviewer()
        self.assertEqual(self.stop()[0], {"decision": "allow"})
        state = self.stored()
        assert state is not None
        self.assertEqual(state.stage, Stage.COMPLETED)

    def test_review_without_a_recorded_reviewer_is_not_isolated(self) -> None:
        self.repo.record_full_evidence("standard")
        self.write_transcript(user_input(1, "go"))
        answer, _ = self.stop()
        self.assertEqual(answer["decision"], "continue")
        self.assertIn("[no-isolation]", answer["reason"])

    def test_reviewer_without_its_own_transcript_is_not_recorded(self) -> None:
        self.write_transcript(
            user_input(1, "go"),
            invoke_subagent(2, REVIEWER),
            subagent_ack(3, REVIEWER_ID),
            subagent_done(4, REVIEWER_ID),
        )
        self.stop()
        self.assertEqual(self.reviewer_stops(), [])


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
            text=True,
            check=True,
            timeout=30,
        )
        self.assertEqual(json.loads(proc.stdout), {"decision": "allow"})


if __name__ == "__main__":
    unittest.main()
