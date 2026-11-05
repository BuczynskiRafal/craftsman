"""Tests for the Craftsman quality gate: its state, the stop decision and `craftsman gate`."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from collections.abc import Iterator
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest import mock

from craftsman import gate
from craftsman.gate import (
    MAX_CONTINUATIONS,
    GateOwner,
    GateState,
    GateStateError,
    Stage,
    cli_caller,
    decide_stop,
    is_continuation_prompt,
    load_state,
    save_state,
    state_path_for,
    update_state,
)

from tests.support import EvidenceRepo, open_gate

CLAUDE_ENV = {"CLAUDECODE": "1"}
VSCODE_ENV = {"VSCODE_PID": "42"}


class RepoTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = EvidenceRepo(Path(tmp.name) / "repo")
        self.state_path = state_path_for(self.repo.root)

    def stored(self) -> GateState:
        state = load_state(self.state_path)
        assert state is not None
        return state

    def run_gate(self, *args: str, env: dict[str, str] | None = None) -> tuple[int, str, str]:
        """Run `craftsman gate` in-process for the test repository; return (exit code, stdout, stderr)."""
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = gate.run_cli([*args, "--cwd", str(self.repo.root)], env or {})
        return code, out.getvalue(), err.getvalue()

    def ledger_is_archived(self) -> bool:
        return any(self.repo.ledger_dir.glob("ledger-*.json"))


class GateOwnerTests(unittest.TestCase):
    def test_cli_owner_admits_any_platform(self) -> None:
        self.assertTrue(GateOwner("cli").admits(GateOwner("copilot", "s1")))

    def test_unknown_session_admits_any_session_of_its_platform(self) -> None:
        self.assertTrue(GateOwner("claude").admits(GateOwner("claude", "s1")))

    def test_caller_without_session_is_admitted_by_its_platform(self) -> None:
        self.assertTrue(GateOwner("claude", "s1").admits(GateOwner("claude")))

    def test_another_session_is_not_admitted(self) -> None:
        self.assertFalse(GateOwner("claude", "s1").admits(GateOwner("claude", "s2")))

    def test_another_platform_is_not_admitted(self) -> None:
        self.assertFalse(GateOwner("claude").admits(GateOwner("copilot")))

    def test_unidentified_caller_without_session_is_not_admitted(self) -> None:
        self.assertFalse(GateOwner("claude", "s1").admits(GateOwner("cli")))

    def test_unidentified_caller_naming_the_session_is_admitted(self) -> None:
        self.assertTrue(GateOwner("claude", "s1").admits(GateOwner("cli", "s1")))

    def test_unidentified_caller_cannot_name_an_unknown_session(self) -> None:
        self.assertFalse(GateOwner("claude").admits(GateOwner("cli", "s1")))

    def test_caller_without_session_needs_the_owners_exact_platform(self) -> None:
        self.assertFalse(GateOwner("cli", "s1").admits(GateOwner("copilot")))

    def test_claim_fills_in_what_the_owner_did_not_know(self) -> None:
        claimed = GateOwner("cli").claimed_by(GateOwner("copilot", "s1"))
        self.assertEqual(claimed, GateOwner("copilot", "s1"))

    def test_claim_keeps_the_identified_platform(self) -> None:
        claimed = GateOwner("claude", "s1").claimed_by(GateOwner("cli", "s1"))
        self.assertEqual(claimed, GateOwner("claude", "s1"))

    def test_unknown_platform_is_rejected(self) -> None:
        with self.assertRaises(GateStateError):
            GateOwner("bogus")  # type: ignore[arg-type]

    def test_blank_session_id_is_rejected(self) -> None:
        with self.assertRaises(GateStateError):
            GateOwner("claude", " ")


class GateStateJsonTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = GateState.open(
            GateOwner("claude", "s1"),
            "a" * 40,
            True,
            "fix it",
            takeover_from=GateOwner("copilot"),
            best_issue_count=3,
        )

    def test_state_round_trips_through_json(self) -> None:
        raw = json.loads(json.dumps(self.state.to_json()))
        self.assertEqual(GateState.from_json(raw), self.state)

    def test_missing_field_is_an_error(self) -> None:
        raw = self.state.to_json()
        del raw["owner"]
        with self.assertRaisesRegex(GateStateError, "owner is missing"):
            GateState.from_json(raw)

    def test_unknown_field_is_an_error(self) -> None:
        raw = {**self.state.to_json(), "mode": "ultracraft"}
        with self.assertRaisesRegex(GateStateError, "mode is not a known field"):
            GateState.from_json(raw)

    def test_corrupt_owner_is_an_error(self) -> None:
        raw = {**self.state.to_json(), "owner": {"platform": "bogus", "session_id": None}}
        with self.assertRaises(GateStateError):
            GateState.from_json(raw)

    def test_boolean_counter_is_an_error(self) -> None:
        raw = {**self.state.to_json(), "forced_continuations": True}
        with self.assertRaises(GateStateError):
            GateState.from_json(raw)

    def test_unknown_stage_is_an_error(self) -> None:
        raw = {**self.state.to_json(), "stage": "done"}
        with self.assertRaises(GateStateError):
            GateState.from_json(raw)


class StateFileTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "craftsman_state.json"

    def test_missing_state_file_means_no_gate(self) -> None:
        self.assertIsNone(load_state(self.path))

    def test_invalid_state_file_names_the_takeover_remedy(self) -> None:
        self.path.write_text(json.dumps({"active": True}), encoding="utf-8")
        with self.assertRaisesRegex(GateStateError, "--init --takeover"):
            load_state(self.path)

    def test_state_of_craftsman_4_is_archived_and_read_as_absent(self) -> None:
        self.path.write_text(json.dumps(OLD_STATE), encoding="utf-8")
        err = io.StringIO()

        with redirect_stderr(err):
            loaded = load_state(self.path)

        self.assertIsNone(loaded)
        self.assertFalse(self.path.exists())
        self.assertEqual(
            json.loads(self.path.with_name("craftsman_state.json.v4.bak").read_text("utf-8")), OLD_STATE
        )
        self.assertEqual(err.getvalue().count("\n"), 1)
        self.assertIn("Archived the gate state of an older craftsman", err.getvalue())

    def test_a_gate_written_while_the_old_state_was_set_aside_is_left_alone(self) -> None:
        self.path.write_text(json.dumps(OLD_STATE), encoding="utf-8")
        newer = GateState.open(GateOwner("claude", "s2"), "abc", True, "new task")

        @contextmanager
        def replaced_first(path: Path) -> Iterator[None]:
            save_state(path, newer)  # a concurrent 5.x gate was opened before this one got the lock
            yield

        with mock.patch.object(gate, "locked_unless_held", replaced_first):
            loaded = load_state(self.path)

        self.assertEqual(loaded, newer)
        self.assertEqual(load_state(self.path), newer)
        self.assertFalse(self.path.with_name("craftsman_state.json.v4.bak").exists())

    def test_an_old_state_met_inside_update_state_does_not_deadlock(self) -> None:
        self.path.write_text(json.dumps(OLD_STATE), encoding="utf-8")

        with redirect_stderr(io.StringIO()):
            seen = update_state(self.path, lambda state: (None, state))

        self.assertIsNone(seen)
        self.assertTrue(self.path.with_name("craftsman_state.json.v4.bak").is_file())

    def test_state_file_that_is_not_json_is_invalid(self) -> None:
        self.path.write_text("{", encoding="utf-8")
        with self.assertRaises(GateStateError):
            load_state(self.path)


class ContinuationPromptTests(unittest.TestCase):
    def test_prompts_the_platform_sends_on_the_sessions_behalf_are_continuations(self) -> None:
        for prompt in (
            'Another Claude session sent a message:\n<agent-message from="a1">done</agent-message>',
            '  \n<agent-message from="a1">done</agent-message>',
            "<task-notification>\n<status>completed</status>\n</task-notification>",
            "\t <task-notification>x</task-notification>",
        ):
            with self.subTest(prompt=prompt[:40]):
                self.assertTrue(is_continuation_prompt(prompt))

    def test_the_users_own_words_are_not(self) -> None:
        for prompt in (
            "thanks, now rename the variable",
            "please explain what an <agent-message> block is",
            "Another session? No: sent a message is not it.",
            "/craft-review the cart",
            "",
        ):
            with self.subTest(prompt=prompt[:40]):
                self.assertFalse(is_continuation_prompt(prompt))


class DecideStopTests(RepoTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.repo.write_lines("app.py", 5)
        self.state = GateState.open(GateOwner("cli"), self.repo.base_sha, False, "")

    def test_unsatisfied_gate_blocks_with_next_actions(self) -> None:
        decision = decide_stop(self.state, self.repo.root)
        assert decision.block_reason is not None
        self.assertTrue(is_continuation_prompt(decision.block_reason))
        self.assertIn("review incomplete: no review round prepared", decision.block_reason)
        self.assertIn("[no-round]", decision.block_reason)

    def test_every_blocked_stop_counts_as_a_continuation(self) -> None:
        state = self.state
        for expected in range(1, MAX_CONTINUATIONS + 1):
            decision = decide_stop(state, self.repo.root)
            self.assertIsNotNone(decision.block_reason)
            state = decision.state
            self.assertEqual(state.forced_continuations, expected)

    def capped(self) -> GateState:
        return replace(self.state, forced_continuations=MAX_CONTINUATIONS, best_issue_count=1)

    def test_cap_lets_the_agent_stop_with_a_notice(self) -> None:
        decision = decide_stop(self.capped(), self.repo.root)
        self.assertEqual((decision.block_reason is None, decision.notice is None), (True, False))

    def test_cap_aborts_the_gate(self) -> None:
        state = decide_stop(self.capped(), self.repo.root).state
        self.assertEqual((state.active, state.stage), (False, Stage.ABORTED))

    def test_fewer_issues_than_ever_start_a_new_streak(self) -> None:
        improving = replace(self.state, forced_continuations=MAX_CONTINUATIONS, best_issue_count=99)
        decision = decide_stop(improving, self.repo.root)
        self.assertIsNotNone(decision.block_reason)
        self.assertEqual(decision.state.forced_continuations, 1)

    def test_awaiting_user_lets_the_agent_stop(self) -> None:
        waiting = replace(self.state, awaiting_user=True)
        self.assertEqual(decide_stop(waiting, self.repo.root).block_reason, None)

    def test_unreadable_ledger_blocks_with_the_error(self) -> None:
        self.repo.ledger_dir.mkdir(parents=True, exist_ok=True)
        (self.repo.ledger_dir / "ledger.json").write_text("{", encoding="utf-8")
        decision = decide_stop(self.state, self.repo.root)
        assert decision.block_reason is not None
        self.assertIn("Cannot evaluate the evidence ledger", decision.block_reason)

    def test_passing_ledger_closes_the_gate(self) -> None:
        self.repo.record_full_evidence()
        decision = decide_stop(self.state, self.repo.root)
        self.assertIsNone(decision.block_reason)
        self.assertEqual(
            (decision.state.active, decision.state.stage), (False, Stage.COMPLETE_UNISOLATED)
        )


OLD_STATE = {
    "owner": {"platform": "claude", "session_id": "s1"},
    "base_sha": "abc",
    "tier": "standard",
    "task": "t",
    "started_at": "2026-01-01T00:00:00.000Z",
    "active": True,
    "stage": "implement",
    "awaiting_user": False,
    "isolation_evidence": "hooks",
    "forced_continuations": 0,
    "best_issue_count": None,
    "takeover_from": None,
    "request_step": None,
}


class ClosedStateTests(RepoTestCase):
    def complete_without_isolation(self, *flags: str) -> GateState:
        self.repo.write_lines("app.py", 5)
        self.assertEqual(self.run_gate("--init", *flags)[0], 0)
        self.repo.record_full_evidence(deep="--deep" in flags)
        code, _, err = self.run_gate("--complete")
        self.assertEqual(code, 0, err)
        return self.stored()

    def test_gate_without_isolation_closes_as_unisolated(self) -> None:
        state = self.complete_without_isolation()
        self.assertEqual((state.active, state.stage), (False, Stage.COMPLETE_UNISOLATED))

    def test_self_reviewed_deep_gate_closes_with_the_banner(self) -> None:
        self.repo.write_lines("app.py", 5)
        self.assertEqual(self.run_gate("--init", "--deep")[0], 0)
        self.repo.record_full_evidence(deep=True)

        code, out, err = self.run_gate("--complete")

        self.assertEqual(code, 0, err)
        self.assertIn("SELF-REVIEWED", out)
        state = self.stored()
        self.assertEqual((state.active, state.stage), (False, Stage.COMPLETE_UNISOLATED))

    def test_self_reviewed_deep_gate_still_needs_its_e2e_run(self) -> None:
        self.repo.write_lines("app.py", 5)
        self.assertEqual(self.run_gate("--init", "--deep")[0], 0)
        self.repo.record_full_evidence()

        code, _, err = self.run_gate("--complete")

        self.assertEqual(code, 1)
        self.assertIn("no-e2e-run", err)
        self.assertTrue(self.stored().active)

    def test_an_antigravity_gate_needs_the_transcript_evidence_to_close(self) -> None:
        self.repo.write_lines("app.py", 5)
        open_gate(self.repo.root, self.repo.base_sha, owner=GateOwner("antigravity", "conv-1"))
        self.repo.record_full_evidence()

        code, _, err = self.run_gate("--complete", "--session-id", "conv-1")

        self.assertEqual(code, 1)
        self.assertIn("[no-isolation]", err)
        self.assertIn("transcript when your turn ends", err)
        self.assertTrue(self.stored().active)


class InitTests(RepoTestCase):
    def test_init_opens_an_active_gate_at_head(self) -> None:
        code, _, err = self.run_gate("--init", "--deep", "--task", "add totals")
        self.assertEqual(code, 0, err)
        state = self.stored()
        self.assertEqual(
            (state.active, state.base_sha, state.deep, state.task),
            (True, self.repo.base_sha, True, "add totals"),
        )

    def test_init_archives_a_ledger_from_another_base(self) -> None:
        self.repo.write_lines("app.py", 5)
        self.repo.cli("review", "prepare")
        self.repo.git("commit", "-qam", "next")
        self.run_gate("--init")
        self.assertTrue(self.ledger_is_archived())

    def test_init_keeps_a_ledger_from_the_same_base(self) -> None:
        self.repo.write_lines("app.py", 5)
        self.repo.cli("review", "prepare")
        self.run_gate("--init")
        self.assertFalse(self.ledger_is_archived())
        self.assertTrue((self.repo.ledger_dir / "ledger.json").is_file())

    def test_init_compares_a_base_ref_by_its_commit(self) -> None:
        self.repo.write_lines("app.py", 5)
        self.repo.cli("review", "prepare")
        self.run_gate("--init", "--base-sha", "HEAD")
        self.assertFalse(self.ledger_is_archived())
        self.assertEqual(self.stored().base_sha, self.repo.base_sha)

    def test_init_rejects_an_unknown_base_ref(self) -> None:
        code, _, err = self.run_gate("--init", "--base-sha", "no-such-ref")
        self.assertEqual(code, 1)
        self.assertIn("craftsman gate: error:", err)
        self.assertFalse(self.state_path.exists())

    def test_init_refuses_another_sessions_gate(self) -> None:
        open_gate(self.repo.root, self.repo.base_sha, owner=GateOwner("claude", "s1"), task="theirs")
        code, _, err = self.run_gate("--init", "--task", "mine", env=VSCODE_ENV)
        self.assertEqual(code, 1)
        self.assertIn("already owned by claude (session s1)", err)
        self.assertEqual(self.stored().task, "theirs")

    def test_init_from_an_unidentified_platform_is_refused(self) -> None:
        open_gate(self.repo.root, self.repo.base_sha, owner=GateOwner("claude", "s1"), task="theirs")
        self.assertEqual(self.run_gate("--init", "--task", "mine")[0], 1)
        self.assertEqual(self.stored().task, "theirs")

    def test_init_from_an_unidentified_platform_naming_the_session_reopens(self) -> None:
        open_gate(self.repo.root, self.repo.base_sha, owner=GateOwner("claude", "s1"))
        code, _, err = self.run_gate("--init", "--session-id", "s1", "--task", "mine")
        self.assertEqual(code, 0, err)
        self.assertEqual((self.stored().owner, self.stored().task), (GateOwner("claude", "s1"), "mine"))

    def test_failed_init_leaves_no_gate(self) -> None:
        self.repo.ledger_dir.mkdir(parents=True, exist_ok=True)
        (self.repo.ledger_dir / "ledger.json").write_text("{", encoding="utf-8")
        code, _, err = self.run_gate("--init")
        self.assertEqual(code, 1)
        self.assertIn("craftsman gate: error:", err)
        self.assertFalse(self.state_path.exists())

    def test_takeover_records_the_previous_owner(self) -> None:
        open_gate(self.repo.root, self.repo.base_sha, owner=GateOwner("claude", "s1"))
        code, _, err = self.run_gate("--init", "--takeover", env=VSCODE_ENV)
        self.assertEqual(code, 0, err)
        state = self.stored()
        self.assertEqual(
            (state.owner, state.takeover_from), (GateOwner("copilot"), GateOwner("claude", "s1"))
        )

    def test_init_without_session_id_reopens_its_platforms_gate(self) -> None:
        open_gate(self.repo.root, self.repo.base_sha, owner=GateOwner("claude", "s1"))
        code, _, err = self.run_gate("--init", "--task", "refined", env=CLAUDE_ENV)
        self.assertEqual(code, 0, err)
        self.assertEqual(
            (self.stored().owner, self.stored().task), (GateOwner("claude", "s1"), "refined")
        )

    def test_init_refuses_the_gate_of_another_exported_session(self) -> None:
        open_gate(self.repo.root, self.repo.base_sha, owner=GateOwner("claude", "s1"))
        env = {**CLAUDE_ENV, "CLAUDE_CODE_SESSION_ID": "s2"}
        self.assertEqual(self.run_gate("--init", env=env)[0], 1)

    def test_reopening_keeps_the_isolation_evidence(self) -> None:
        open_gate(
            self.repo.root,
            self.repo.base_sha,
            owner=GateOwner("claude", "s1"),
            isolation_evidence="hooks",
        )
        self.run_gate("--init", "--session-id", "s1", env=CLAUDE_ENV)
        self.assertEqual(self.stored().isolation_evidence, "hooks")

    def test_reopening_without_deep_keeps_a_deep_gate_deep(self) -> None:
        open_gate(self.repo.root, self.repo.base_sha, owner=GateOwner("claude", "s1"), deep=True)
        code, _, err = self.run_gate("--init", "--session-id", "s1", env=CLAUDE_ENV)
        self.assertEqual(code, 0, err)
        self.assertTrue(self.stored().deep)

    def test_takeover_of_a_deep_gate_stays_deep(self) -> None:
        open_gate(
            self.repo.root, self.repo.base_sha, deep=True, owner=GateOwner("claude", "s1")
        )

        code, _, err = self.run_gate(
            "--init", "--takeover", "--task", "x", env={**CLAUDE_ENV, "CLAUDE_CODE_SESSION_ID": "s2"}
        )

        self.assertEqual(code, 0, err)
        state = self.stored()
        self.assertEqual((state.owner.session_id, state.deep), ("s2", True))

    def test_init_over_the_state_and_ledger_of_craftsman_4_needs_no_takeover(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(OLD_STATE), encoding="utf-8")
        (self.repo.ledger_dir / "ledger.json").write_text(
            json.dumps({"schema": 2, "rounds": []}), encoding="utf-8"
        )

        code, _, err = self.run_gate("--init", "--task", "after the upgrade")

        self.assertEqual(code, 0, err)
        self.assertTrue(self.stored().active)
        self.assertTrue((self.repo.ledger_dir / "craftsman_state.json.v4.bak").is_file())

    def test_reopening_with_deep_raises_the_gate(self) -> None:
        open_gate(self.repo.root, self.repo.base_sha, owner=GateOwner("claude", "s1"))
        self.run_gate("--init", "--session-id", "s1", "--deep", env=CLAUDE_ENV)
        self.assertTrue(self.stored().deep)


class CallerTests(unittest.TestCase):
    def test_claude_session_comes_from_the_environment(self) -> None:
        env = {**CLAUDE_ENV, "CLAUDE_CODE_SESSION_ID": "s1"}
        self.assertEqual(cli_caller(None, env), GateOwner("claude", "s1"))

    def test_session_flag_wins_over_the_environment(self) -> None:
        env = {**CLAUDE_ENV, "CLAUDE_CODE_SESSION_ID": "s1"}
        self.assertEqual(cli_caller("s9", env), GateOwner("claude", "s9"))

    def test_plain_terminal_is_the_cli_platform(self) -> None:
        self.assertEqual(cli_caller(None, {}), GateOwner("cli"))


class UpdateTests(RepoTestCase):
    def setUp(self) -> None:
        super().setUp()
        open_gate(self.repo.root, self.repo.base_sha, task="first")

    def test_deep_update_raises_the_gate(self) -> None:
        self.run_gate("--deep")
        self.assertTrue(self.stored().deep)

    def test_a_plain_update_never_lowers_a_deep_gate(self) -> None:
        self.run_gate("--deep")
        self.run_gate("--task", "second")
        self.assertTrue(self.stored().deep)

    def test_task_update_replaces_the_task(self) -> None:
        self.run_gate("--task", "second")
        self.assertEqual(self.stored().task, "second")

    def test_await_user_pauses_the_gate(self) -> None:
        self.run_gate("--await-user")
        self.assertTrue(self.stored().awaiting_user)

    def test_resume_enforces_the_gate_again(self) -> None:
        self.run_gate("--await-user")
        self.run_gate("--resume")
        self.assertFalse(self.stored().awaiting_user)

    def test_update_of_another_sessions_gate_is_refused(self) -> None:
        open_gate(self.repo.root, self.repo.base_sha, owner=GateOwner("claude", "s1"))
        env = {**CLAUDE_ENV, "CLAUDE_CODE_SESSION_ID": "s2"}
        code, _, err = self.run_gate("--await-user", env=env)
        self.assertEqual(code, 1)
        self.assertIn("already owned by claude (session s1)", err)
        self.assertFalse(self.stored().awaiting_user)

    def test_update_naming_the_owning_session_applies(self) -> None:
        open_gate(self.repo.root, self.repo.base_sha, owner=GateOwner("claude", "s1"))
        self.run_gate("--await-user", "--session-id", "s1")
        self.assertTrue(self.stored().awaiting_user)

    def test_update_without_an_active_gate_fails(self) -> None:
        self.state_path.unlink()
        code, _, err = self.run_gate("--await-user")
        self.assertEqual(code, 1)
        self.assertIn("no active gate", err)


class CompleteTests(RepoTestCase):
    def test_complete_refuses_until_the_ledger_passes(self) -> None:
        self.repo.write_lines("app.py", 5)
        self.run_gate("--init")
        code, _, err = self.run_gate("--complete")
        self.assertEqual(code, 1)
        self.assertIn("[no-round]", err)
        self.assertTrue(self.stored().active)

    def test_complete_closes_a_passing_gate(self) -> None:
        self.repo.write_lines("app.py", 5)
        self.run_gate("--init")
        self.repo.record_full_evidence()
        code, out, err = self.run_gate("--complete")
        self.assertEqual(code, 0, err)
        self.assertIn("Craftsman gate closed", out)
        self.assertEqual(self.stored().stage, Stage.COMPLETE_UNISOLATED)

    def test_complete_without_changes_closes_the_gate(self) -> None:
        self.run_gate("--init")
        self.assertEqual(self.run_gate("--complete")[0], 0)

    def test_complete_of_another_sessions_gate_is_refused(self) -> None:
        open_gate(self.repo.root, self.repo.base_sha, owner=GateOwner("claude", "s1"))
        env = {**CLAUDE_ENV, "CLAUDE_CODE_SESSION_ID": "s2"}
        code, _, err = self.run_gate("--complete", env=env)
        self.assertEqual(code, 1)
        self.assertIn("already owned by claude (session s1)", err)
        self.assertTrue(self.stored().active)

    def test_complete_of_an_invalid_state_fails(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text("[]", encoding="utf-8")
        code, _, err = self.run_gate("--complete")
        self.assertEqual(code, 1)
        self.assertIn("not a valid gate state", err)


class ArgumentTests(RepoTestCase):
    def assert_usage_error(self, *args: str) -> None:
        with self.assertRaises(SystemExit) as raised:
            self.run_gate(*args)
        self.assertEqual(raised.exception.code, 2)

    def test_an_action_is_required(self) -> None:
        self.assert_usage_error()

    def test_init_only_flags_require_init(self) -> None:
        for flag in (("--takeover",), ("--base-sha", "HEAD")):
            with self.subTest(flag=flag[0]):
                self.assert_usage_error("--complete", *flag)

    def test_deep_cannot_change_while_completing(self) -> None:
        self.assert_usage_error("--complete", "--deep")

    def test_actions_are_mutually_exclusive(self) -> None:
        self.assert_usage_error("--await-user", "--resume")


if __name__ == "__main__":
    unittest.main()
