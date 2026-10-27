"""Tests for the Craftsman quality gate: its state, the stop decision and `craftsman gate`."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path

from craftsman import gate
from craftsman.diffscan import snapshot
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
    state_path_for,
    task_tier,
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
            "deep",
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
        self.path.write_text(json.dumps({"active": True, "tier": "auto"}), encoding="utf-8")
        with self.assertRaisesRegex(GateStateError, "--init --takeover"):
            load_state(self.path)

    def test_state_file_that_is_not_json_is_invalid(self) -> None:
        self.path.write_text("{", encoding="utf-8")
        with self.assertRaises(GateStateError):
            load_state(self.path)


class TaskTierTests(RepoTestCase):
    def tier_of_current_diff(self) -> str:
        return task_tier("auto", snapshot(self.repo.root, self.repo.base_sha))

    def test_auto_tier_is_quick_for_a_small_change(self) -> None:
        self.repo.write_lines("app.py", 5)
        self.assertEqual(self.tier_of_current_diff(), "quick")

    def test_auto_tier_is_standard_beyond_the_quick_file_limit(self) -> None:
        for name in ("a.py", "b.py", "c.py"):
            self.repo.write(name, "x = 1\n")
        self.assertEqual(self.tier_of_current_diff(), "standard")

    def test_auto_tier_is_standard_beyond_the_quick_line_limit(self) -> None:
        self.repo.write_lines("app.py", 60)
        self.assertEqual(self.tier_of_current_diff(), "standard")

    def test_auto_tier_is_standard_for_a_binary_file(self) -> None:
        (self.repo.root / "blob.bin").write_bytes(b"\0" * 64)
        self.assertEqual(self.tier_of_current_diff(), "standard")

    def test_auto_tier_is_standard_for_an_oversized_file(self) -> None:
        self.repo.write("huge.txt", "x\n" * 600_000)
        self.assertEqual(self.tier_of_current_diff(), "standard")

    def test_explicit_tier_ignores_the_diff_size(self) -> None:
        self.repo.write_lines("app.py", 60)
        snap = snapshot(self.repo.root, self.repo.base_sha)
        self.assertEqual(task_tier("quick", snap), "quick")


class DecideStopTests(RepoTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.repo.write_lines("app.py", 5)
        self.state = GateState.open(GateOwner("cli"), self.repo.base_sha, "quick", "")

    def test_unsatisfied_gate_blocks_with_next_actions(self) -> None:
        decision = decide_stop(self.state, self.repo.root)
        assert decision.block_reason is not None
        self.assertTrue(is_continuation_prompt(decision.block_reason))
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
        self.repo.record_full_evidence("quick")
        decision = decide_stop(self.state, self.repo.root)
        self.assertIsNone(decision.block_reason)
        self.assertEqual((decision.state.active, decision.state.stage), (False, Stage.COMPLETED))


class ClosedStateTests(RepoTestCase):
    def complete_without_isolation(self, tier: str) -> GateState:
        self.repo.write_lines("app.py", 5)
        self.assertEqual(self.run_gate("--init", "--tier", tier)[0], 0)
        self.repo.record_full_evidence(tier)
        code, _, err = self.run_gate("--complete")
        self.assertEqual(code, 0, err)
        return self.stored()

    def test_standard_gate_without_isolation_closes_as_unisolated(self) -> None:
        state = self.complete_without_isolation("standard")
        self.assertEqual((state.active, state.stage), (False, Stage.COMPLETE_UNISOLATED))

    def test_self_reviewed_deep_gate_stays_open_for_the_user(self) -> None:
        state = self.complete_without_isolation("deep")
        self.assertEqual((state.active, state.stage), (True, Stage.NEEDS_HUMAN))


class InitTests(RepoTestCase):
    def test_init_opens_an_active_gate_at_head(self) -> None:
        code, _, err = self.run_gate("--init", "--tier", "deep", "--task", "add totals")
        self.assertEqual(code, 0, err)
        state = self.stored()
        self.assertEqual(
            (state.active, state.base_sha, state.tier, state.task),
            (True, self.repo.base_sha, "deep", "add totals"),
        )

    def test_init_archives_a_ledger_from_another_base(self) -> None:
        self.repo.write_lines("app.py", 5)
        self.repo.cli("review", "prepare", "--tier", "quick")
        self.repo.git("commit", "-qam", "next")
        self.run_gate("--init")
        self.assertTrue(self.ledger_is_archived())

    def test_init_keeps_a_ledger_from_the_same_base(self) -> None:
        self.repo.write_lines("app.py", 5)
        self.repo.cli("review", "prepare", "--tier", "quick")
        self.run_gate("--init")
        self.assertFalse(self.ledger_is_archived())
        self.assertTrue((self.repo.ledger_dir / "ledger.json").is_file())

    def test_init_compares_a_base_ref_by_its_commit(self) -> None:
        self.repo.write_lines("app.py", 5)
        self.repo.cli("review", "prepare", "--tier", "quick")
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

    def test_reopening_never_lowers_the_tier(self) -> None:
        open_gate(self.repo.root, self.repo.base_sha, owner=GateOwner("claude", "s1"), tier="deep")
        code, _, err = self.run_gate("--init", "--session-id", "s1", "--tier", "quick", env=CLAUDE_ENV)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.stored().tier, "deep")

    def test_reopening_without_a_tier_keeps_the_tier(self) -> None:
        open_gate(self.repo.root, self.repo.base_sha, owner=GateOwner("claude", "s1"), tier="deep")
        self.run_gate("--init", "--session-id", "s1", env=CLAUDE_ENV)
        self.assertEqual(self.stored().tier, "deep")

    def test_reopening_raises_the_tier(self) -> None:
        open_gate(self.repo.root, self.repo.base_sha, owner=GateOwner("claude", "s1"), tier="quick")
        self.run_gate("--init", "--session-id", "s1", "--tier", "standard", env=CLAUDE_ENV)
        self.assertEqual(self.stored().tier, "standard")

    def test_init_refuses_an_invalid_state_file(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps({"active": True}), encoding="utf-8")
        code, _, err = self.run_gate("--init")
        self.assertEqual(code, 1)
        self.assertIn("--takeover", err)

    def test_takeover_replaces_an_invalid_state_file(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps({"active": True}), encoding="utf-8")
        code, _, err = self.run_gate("--init", "--takeover")
        self.assertEqual(code, 0, err)
        self.assertTrue(self.stored().active)


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
        open_gate(self.repo.root, self.repo.base_sha, tier="standard", task="first")

    def test_tier_update_raises_the_tier(self) -> None:
        self.run_gate("--tier", "deep")
        self.assertEqual(self.stored().tier, "deep")

    def test_tier_update_never_lowers_the_tier(self) -> None:
        self.run_gate("--tier", "quick")
        self.assertEqual(self.stored().tier, "standard")

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
        self.run_gate("--init", "--tier", "quick")
        code, _, err = self.run_gate("--complete")
        self.assertEqual(code, 1)
        self.assertIn("[no-round]", err)
        self.assertTrue(self.stored().active)

    def test_complete_closes_a_passing_gate(self) -> None:
        self.repo.write_lines("app.py", 5)
        self.run_gate("--init", "--tier", "quick")
        self.repo.record_full_evidence("quick")
        code, out, err = self.run_gate("--complete")
        self.assertEqual(code, 0, err)
        self.assertIn("Craftsman gate closed", out)
        self.assertEqual(self.stored().stage, Stage.COMPLETED)

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

    def test_tier_cannot_change_while_completing(self) -> None:
        self.assert_usage_error("--complete", "--tier", "quick")

    def test_actions_are_mutually_exclusive(self) -> None:
        self.assert_usage_error("--await-user", "--resume")


if __name__ == "__main__":
    unittest.main()
