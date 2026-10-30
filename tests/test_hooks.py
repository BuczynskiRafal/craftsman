"""Tests for `craftsman hook`: the VS Code Copilot, Copilot CLI and Claude Code lifecycle hooks."""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any
from unittest import mock

from craftsman import antigravity, hooks
from craftsman.gate import MAX_CONTINUATIONS
from craftsman.gatecheck import MAX_ROUNDS
from craftsman.hooks import REVIEWER_STOP_REASON, HookInput, HookInputError

from tests.support import EvidenceRepo, coverage_note

CHANGE = "def total(items):\n    return sum(items)\n"
STARTED = (
    "Craftsman session started (tier {tier}, base {base}). Follow the /craftsman skill "
    '(Stage 0 command: `craftsman gate --init --session-id {sid} --tier {tier} --task "{task}"`); '
    "the gate will block finishing until `craftsman review check` passes.\n"
    "Scope: work only inside {root}; see Search scope."
)

REVIEW_ONLY_VERIFIER = {"agent_id": "v-ro", "agent_type": "craftsman-verifier"}


def header(continuation: int) -> str:
    return f"[CRAFTSMAN GATE {continuation}/{MAX_CONTINUATIONS}] "


def call_hook(event: str, platform: str, payload: dict[str, Any] | str) -> tuple[dict[str, Any], str]:
    """Run `craftsman hook` in-process with `payload` on stdin; return (stdout JSON, stderr)."""
    stdin = io.StringIO(payload if isinstance(payload, str) else json.dumps(payload))
    out, err = io.StringIO(), io.StringIO()
    with mock.patch("sys.stdin", stdin), redirect_stdout(out), redirect_stderr(err):
        code = hooks.main([event, "--platform", platform])
    assert code == 0, err.getvalue()
    parsed = json.loads(out.getvalue())
    assert isinstance(parsed, dict)
    return parsed, err.getvalue()


def vscode(event_name: str, cwd: Path, **extra: Any) -> dict[str, Any]:
    return {
        "timestamp": "2026-10-21T10:00:00.000Z",
        "hook_event_name": event_name,
        "cwd": str(cwd),
        "session_id": "vscode-session",
        "transcript_path": str(cwd / "no-such-transcript.json"),
        **extra,
    }


def copilot_cli(cwd: Path, **extra: Any) -> dict[str, Any]:
    return {"sessionId": "cli-session", "timestamp": 1792576800000, "cwd": str(cwd), **extra}


def claude(event_name: str, cwd: Path, **extra: Any) -> dict[str, Any]:
    return {
        "session_id": "claude-session",
        "transcript_path": str(cwd / "no-such-transcript.jsonl"),
        "cwd": str(cwd),
        "hook_event_name": event_name,
        **extra,
    }


class HookTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.repo = EvidenceRepo(self.tmp / "repo")
        self.root = self.repo.root

    @property
    def state_file(self) -> Path:
        return self.repo.ledger_dir / "craftsman_state.json"

    def state(self) -> dict[str, Any]:
        loaded = json.loads(self.state_file.read_text(encoding="utf-8"))
        assert isinstance(loaded, dict)
        return loaded

    def update_state(self, **changes: Any) -> None:
        self.state_file.write_text(json.dumps({**self.state(), **changes}), encoding="utf-8")

    def init(self, tier: str) -> None:
        self.repo.cli("gate", "--init", "--tier", tier, "--cwd", ".")

    def ledger(self) -> dict[str, Any]:
        loaded = json.loads((self.repo.ledger_dir / "ledger.json").read_text(encoding="utf-8"))
        assert isinstance(loaded, dict)
        return loaded

    def add_finding(self) -> None:
        self.repo.cli(
            "review", "add", "--lens", "contracts", "--file", "cart.py", "--line", "2",
            "--severity", "major", "--title", "Strings are summed without a check",
            "--scenario", "total(['a']) raises TypeError instead of a clear error",
        )  # fmt: skip


class InactiveTests(HookTestCase):
    def test_every_event_allows_without_an_active_state(self) -> None:
        for event, platform, payload in (
            ("stop", "copilot", vscode("Stop", self.root, stop_hook_active=False)),
            ("stop", "copilot", copilot_cli(self.root, stopReason="end_turn")),
            ("stop", "claude", claude("Stop", self.root, stop_hook_active=False)),
            ("subagent-start", "copilot", vscode("SubagentStart", self.root, agent_type="craftsman-reviewer")),
            ("subagent-stop", "claude", claude("SubagentStop", self.root, agent_type="craftsman-reviewer")),
            ("prompt", "copilot", vscode("UserPromptSubmit", self.root, prompt="what does `/craftsman` do?")),
        ):  # fmt: skip
            with self.subTest(event=event, platform=platform):
                self.assertEqual(call_hook(event, platform, payload)[0], {})
        self.assertFalse(self.state_file.exists())
        self.assertFalse((self.repo.ledger_dir / "ledger.json").exists())

    def test_outside_a_git_repository_allows(self) -> None:
        outside = self.tmp / "plain"
        outside.mkdir()
        payload = vscode("UserPromptSubmit", outside, prompt="/craftsman --deep add totals")
        self.assertEqual(call_hook("prompt", "copilot", payload)[0], {})
        self.assertEqual(call_hook("stop", "claude", claude("Stop", outside))[0], {})

    def test_unreadable_input_lets_the_agent_go_on_with_a_warning(self) -> None:
        for event in ("prompt", "stop", "subagent-start", "subagent-stop"):
            with self.subTest(event=event):
                out, err = call_hook(event, "copilot", "{not json")
                self.assertIn("the quality gate was not checked", out["systemMessage"])
                self.assertIn("unreadable hook input", err)

    def test_input_that_is_not_an_object_is_reported(self) -> None:
        out, _ = call_hook("stop", "claude", "[1, 2]")
        self.assertIn("not a JSON object", out["systemMessage"])

    def test_pre_tool_input_without_cwd_asks_the_user(self) -> None:
        payload = {"tool_name": "Bash", "tool_input": {"command": "ls"}, "session_id": "s1"}

        out, _ = call_hook("pre-tool", "claude", payload)

        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "ask")
        self.assertIn("has no `cwd`", out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_unreadable_pre_tool_input_asks_the_user(self) -> None:
        out, _ = call_hook("pre-tool", "claude", "{not json")
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "ask")

    def test_undecodable_shell_tool_args_ask_the_user(self) -> None:
        payload = claude("PreToolUse", self.root, tool_name="Bash", tool_input='{"command": "git push"')
        out, _ = call_hook("pre-tool", "claude", payload)
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "ask")

    def test_undecodable_shell_tool_args_are_denied_on_copilot(self) -> None:
        payload = copilot_cli(
            self.root, toolName="bash", toolArgs='{"command": "git push origin HEAD:main"'
        )
        out, _ = call_hook("pre-tool", "copilot", payload)
        self.assertEqual(out["permissionDecision"], "deny")

    def test_unreadable_pre_tool_input_is_denied_on_copilot(self) -> None:
        out, _ = call_hook("pre-tool", "copilot", "{not json")
        self.assertEqual(out["permissionDecision"], "deny")

    def test_invalid_arguments_block_the_event(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdin", io.StringIO("{}")), redirect_stdout(out), redirect_stderr(err):
            code = hooks.main(["finish", "--platform", "copilot"])
        self.assertEqual((code, out.getvalue()), (2, ""))
        self.assertIn("craftsman hook error: argument event: invalid choice", err.getvalue())


class PromptTests(HookTestCase):
    def deep_trigger(self) -> dict[str, Any]:
        payload = vscode("UserPromptSubmit", self.root, prompt="/craftsman --deep add cart totals")
        return call_hook("prompt", "copilot", payload)[0]

    def test_trigger_briefs_the_agent_with_the_init_command(self) -> None:
        context = STARTED.format(
            tier="deep",
            base=self.repo.base_sha[:7],
            sid="vscode-session",
            task="add cart totals",
            root=self.root.resolve(),
        )
        self.assertEqual(
            self.deep_trigger(),
            {
                "additionalContext": context,
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": context,
                },
            },
        )

    def test_trigger_opens_a_gate_owned_by_the_prompting_session(self) -> None:
        self.deep_trigger()
        state = self.state()
        self.assertEqual(
            {
                key: state[key]
                for key in ("active", "tier", "base_sha", "task", "owner", "isolation_evidence")
            },
            {
                "active": True,
                "tier": "deep",
                "base_sha": self.repo.base_sha,
                "task": "add cart totals",
                "owner": {"platform": "copilot", "session_id": "vscode-session"},
                "isolation_evidence": "hooks",
            },
        )

    def test_copilot_cli_trigger_opens_a_gate(self) -> None:
        call_hook("prompt", "copilot", copilot_cli(self.root, prompt="ultracraft refactor the cart"))
        self.assertEqual((self.state()["tier"], self.state()["task"]), ("deep", "refactor the cart"))

    def test_second_trigger_keeps_the_active_task(self) -> None:
        call_hook("prompt", "copilot", copilot_cli(self.root, prompt="ultracraft refactor the cart"))
        call_hook("prompt", "copilot", copilot_cli(self.root, prompt="/craftsman --quick other"))
        self.assertEqual(self.state()["task"], "refactor the cart")

    def test_trigger_after_a_closed_gate_opens_a_new_one(self) -> None:
        call_hook("prompt", "copilot", copilot_cli(self.root, prompt="ultracraft refactor the cart"))
        self.update_state(active=False)
        call_hook("prompt", "claude", claude("UserPromptSubmit", self.root, prompt="/craftsman fix it"))
        self.assertEqual((self.state()["active"], self.state()["task"]), (True, "fix it"))

    def test_claude_trigger_answers_in_the_claude_dialect(self) -> None:
        out, _ = call_hook(
            "prompt", "claude", claude("UserPromptSubmit", self.root, prompt="/craftsman fix it")
        )
        self.assertEqual(set(out), {"hookSpecificOutput"})
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")

    def test_suggested_init_command_drops_shell_active_characters(self) -> None:
        prompt = '/craftsman fix "$(touch pwned)" and `id` \\'
        out, _ = call_hook("prompt", "claude", claude("UserPromptSubmit", self.root, prompt=prompt))
        command = (
            out["hookSpecificOutput"]["additionalContext"].split("Stage 0 command: `")[1].split("`)")[0]
        )
        self.assertEqual(
            command,
            'craftsman gate --init --session-id claude-session --tier auto --task "fix (touch pwned) and id "',
        )


class StopTests(HookTestCase):
    def open_unsatisfied_gate(self, tier: str = "quick") -> None:
        self.init(tier)
        self.repo.write("cart.py", CHANGE)

    def test_unsatisfied_gate_blocks_in_the_copilot_dialect(self) -> None:
        self.open_unsatisfied_gate()
        out, _ = call_hook("stop", "copilot", vscode("Stop", self.root, stop_hook_active=False))
        reason = out["reason"]
        self.assertEqual(
            out,
            {
                "decision": "block",
                "reason": reason,
                "hookSpecificOutput": {"hookEventName": "Stop", "decision": "block", "reason": reason},
            },
        )

    def test_unsatisfied_gate_blocks_in_the_claude_dialect(self) -> None:
        self.open_unsatisfied_gate()
        out, _ = call_hook("stop", "claude", claude("Stop", self.root))
        self.assertEqual((set(out), out["decision"]), ({"decision", "reason"}, "block"))

    def test_block_reason_counts_the_continuation(self) -> None:
        self.open_unsatisfied_gate()
        out, _ = call_hook("stop", "copilot", vscode("Stop", self.root, stop_hook_active=False))
        self.assertTrue(out["reason"].startswith(header(1)))

    def test_stop_hook_active_does_not_end_the_block(self) -> None:
        self.open_unsatisfied_gate()
        out, _ = call_hook("stop", "copilot", vscode("Stop", self.root, stop_hook_active=True))
        self.assertEqual(out["decision"], "block")

    def test_copilot_cli_stop_without_stop_hook_active_is_blocked(self) -> None:
        self.open_unsatisfied_gate()
        out, _ = call_hook("stop", "copilot", copilot_cli(self.root, stopReason="end_turn"))
        self.assertEqual(out["decision"], "block")

    def test_claude_retry_after_a_block_is_blocked_again(self) -> None:
        self.open_unsatisfied_gate()
        call_hook("stop", "claude", claude("Stop", self.root, stop_hook_active=False))
        out, _ = call_hook("stop", "claude", claude("Stop", self.root, stop_hook_active=True))
        self.assertTrue(out["reason"].startswith(header(2)))

    def test_passing_ledger_closes_the_gate_at_stop(self) -> None:
        self.open_unsatisfied_gate()
        self.repo.record_full_evidence("quick")
        done, _ = call_hook("stop", "copilot", copilot_cli(self.root, stopReason="end_turn"))
        self.assertEqual(done, {})
        self.assertEqual((self.state()["active"], self.state()["stage"]), (False, "completed"))

    def test_cap_answers_with_a_system_message(self) -> None:
        self.open_unsatisfied_gate()
        self.update_state(forced_continuations=MAX_CONTINUATIONS)
        capped, _ = call_hook("stop", "copilot", copilot_cli(self.root))
        self.assertEqual(set(capped), {"systemMessage"})
        self.assertIn("Craftsman gate not satisfied", capped["systemMessage"])

    def prepare_unisolated_review(self) -> None:
        """A standard change with a covered round and a test run, but no reviewer agent."""
        self.open_unsatisfied_gate("standard")
        self.repo.cli("review", "prepare", "--tier", "standard")
        self.repo.cover_all()
        self.repo.cli("run", "--kind", "test", "--", sys.executable, "-c", "print('ok')")

    def test_hooked_gate_blocks_a_review_without_a_reviewer(self) -> None:
        self.prepare_unisolated_review()
        blocked, _ = call_hook("stop", "claude", claude("Stop", self.root))
        self.assertIn("[no-isolation]", blocked["reason"])

    def test_reviewer_hook_events_prove_the_isolated_review(self) -> None:
        self.prepare_unisolated_review()
        reviewer = {"agent_id": "a1", "agent_type": "craftsman-reviewer"}
        call_hook("subagent-start", "claude", claude("SubagentStart", self.root, **reviewer))
        call_hook(
            "subagent-stop",
            "claude",
            claude("SubagentStop", self.root, stop_hook_active=True, **reviewer),
        )
        self.assertEqual(call_hook("stop", "claude", claude("Stop", self.root))[0], {})
        self.assertEqual(self.state()["stage"], "completed")

    def test_continuation_prompt_keeps_the_streak(self) -> None:
        self.open_unsatisfied_gate()
        self.update_state(forced_continuations=4, best_issue_count=2)
        prompt = header(4) + "1. ..."
        call_hook("prompt", "claude", claude("UserPromptSubmit", self.root, prompt=prompt))
        self.assertEqual(self.state()["forced_continuations"], 4)

    def test_user_prompt_starts_a_new_streak(self) -> None:
        self.open_unsatisfied_gate()
        self.update_state(forced_continuations=4, best_issue_count=2)
        call_hook("prompt", "claude", claude("UserPromptSubmit", self.root, prompt="Use option B"))
        state = self.state()
        self.assertEqual((state["forced_continuations"], state["best_issue_count"]), (0, None))

    def test_user_prompt_ends_the_wait_for_the_user(self) -> None:
        self.open_unsatisfied_gate()
        self.repo.cli("gate", "--await-user", "--cwd", ".")
        call_hook("prompt", "claude", claude("UserPromptSubmit", self.root, prompt="Use option B"))
        self.assertFalse(self.state()["awaiting_user"])

    def test_trigger_during_a_session_raises_the_tier(self) -> None:
        self.open_unsatisfied_gate()
        prompt = "/craftsman --deep go"
        call_hook("prompt", "claude", claude("UserPromptSubmit", self.root, prompt=prompt))
        self.assertEqual(self.state()["tier"], "deep")

    def test_trigger_during_a_session_never_lowers_the_tier(self) -> None:
        self.open_unsatisfied_gate("deep")
        prompt = "/craftsman --quick"
        call_hook("prompt", "claude", claude("UserPromptSubmit", self.root, prompt=prompt))
        self.assertEqual(self.state()["tier"], "deep")

    def retry_after_the_round_cap(self) -> dict[str, Any]:
        self.open_unsatisfied_gate()
        self.repo.cli("review", "prepare", "--tier", "quick")
        self.update_state(stage="needs_human")
        prompt = "/craftsman retry"
        out, _ = call_hook("prompt", "claude", claude("UserPromptSubmit", self.root, prompt=prompt))
        return out

    def test_trigger_after_the_round_cap_restarts_the_gate(self) -> None:
        self.retry_after_the_round_cap()
        self.assertEqual((self.state()["stage"], self.state()["task"]), ("implement", "retry"))

    def test_restarted_gate_archives_the_previous_review(self) -> None:
        self.retry_after_the_round_cap()
        self.assertFalse((self.repo.ledger_dir / "ledger.json").exists())
        self.assertEqual(len(list(self.repo.ledger_dir.glob("ledger-*.json"))), 1)

    def test_awaiting_user_allows(self) -> None:
        self.open_unsatisfied_gate()
        self.repo.cli("gate", "--await-user", "--cwd", ".")
        self.assertEqual(call_hook("stop", "claude", claude("Stop", self.root))[0], {})

    def exceed_the_round_cap(self) -> None:
        self.init("quick")
        for revision in range(MAX_ROUNDS + 1):
            self.repo.write("cart.py", CHANGE + f"# revision {revision}\n")
            self.repo.cli("review", "prepare", "--tier", "quick")

    def test_round_cap_answers_with_a_system_message(self) -> None:
        self.exceed_the_round_cap()
        out, _ = call_hook("stop", "copilot", vscode("Stop", self.root, stop_hook_active=False))
        self.assertEqual(set(out), {"systemMessage"})
        self.assertIn("round cap", out["systemMessage"])

    def test_round_cap_hands_the_gate_to_the_user(self) -> None:
        self.exceed_the_round_cap()
        call_hook("stop", "copilot", vscode("Stop", self.root, stop_hook_active=False))
        self.assertEqual(self.state()["stage"], "needs_human")

    def test_stop_failure_lets_the_agent_stop_with_a_warning(self) -> None:
        self.open_unsatisfied_gate()
        with mock.patch("craftsman.gate.evaluate_task", side_effect=RuntimeError("boom")):
            out, err = call_hook("stop", "copilot", vscode("Stop", self.root))
        self.assertEqual(set(out), {"systemMessage"})
        self.assertIn("quality gate was not checked", out["systemMessage"])
        self.assertIn("RuntimeError: boom", err)

    def test_invalid_gate_state_is_reported_at_stop(self) -> None:
        self.init("quick")
        self.update_state(owner=None)
        out, _ = call_hook("stop", "claude", claude("Stop", self.root))
        self.assertIn("not a valid gate state", out["systemMessage"])


class SubagentTests(HookTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.init("standard")
        self.repo.write("cart.py", CHANGE)

    def start_reviewer(self) -> dict[str, Any]:
        self.repo.cli("review", "prepare", "--tier", "standard")
        payload = vscode("SubagentStart", self.root, agent_id="r-1", agent_type="craftsman-reviewer")
        return call_hook("subagent-start", "copilot", payload)[0]

    def test_reviewer_start_is_briefed_with_its_packet(self) -> None:
        out = self.start_reviewer()
        context = out["additionalContext"]
        self.assertEqual(
            out["hookSpecificOutput"], {"hookEventName": "SubagentStart", "additionalContext": context}
        )
        packet = self.repo.ledger_dir / "packet-1.md"
        self.assertTrue(context.startswith(f"Craftsman review round 1. Packet: {packet}\n"))

    def test_reviewer_brief_lists_the_hunks_of_each_lens(self) -> None:
        context = self.start_reviewer()["additionalContext"]
        self.assertIn("- intent: H1\n", context)
        self.assertIn("- sweep: H1 (run last)\n", context)

    def test_reviewer_start_is_recorded(self) -> None:
        self.start_reviewer()
        events = self.ledger()["agents"]
        self.assertEqual(
            [(e["platform"], e["event"], e["agent"], e["agent_id"]) for e in events],
            [("copilot", "start", "craftsman-reviewer", "r-1")],
        )

    def test_verifier_brief_lists_findings_without_a_verdict(self) -> None:
        self.repo.cli("review", "prepare", "--tier", "standard")
        self.add_finding()
        out, _ = call_hook(
            "subagent-start",
            "copilot",
            copilot_cli(
                self.root, agentName="craftsman-verifier", agentDisplayName="Craftsman Verifier"
            ),
        )
        self.assertIn(
            "Findings without a verdict:\n- F1 (major, cart.py:2): Strings are summed",
            out["additionalContext"],
        )

    def test_other_agents_are_recorded_without_a_briefing(self) -> None:
        self.repo.cli("review", "prepare", "--tier", "standard")
        explore = {"agent_id": "x", "agent_type": "Explore"}
        started, _ = call_hook("subagent-start", "claude", claude("SubagentStart", self.root, **explore))
        stopped, _ = call_hook("subagent-stop", "claude", claude("SubagentStop", self.root, **explore))
        self.assertEqual((started, stopped), ({}, {}))
        self.assertEqual(
            [(e["event"], e["agent"]) for e in self.ledger()["agents"]],
            [("start", "Explore"), ("stop", "Explore")],
        )

    def test_no_round_means_no_briefing(self) -> None:
        out, _ = call_hook(
            "subagent-start",
            "claude",
            claude("SubagentStart", self.root, agent_type="craftsman-reviewer"),
        )
        self.assertEqual(out, {})
        self.assertEqual(len(self.ledger()["agents"]), 1)

    def test_reviewer_stop_without_records_is_sent_back_once(self) -> None:
        self.repo.cli("review", "prepare", "--tier", "standard")
        call_hook("subagent-start", "copilot", copilot_cli(self.root, agentName="craftsman-reviewer"))
        stop = copilot_cli(self.root, agentId="r-9", agentType="custom", agentName="craftsman-reviewer")

        first, _ = call_hook("subagent-stop", "copilot", stop)
        second, _ = call_hook("subagent-stop", "copilot", stop)

        self.assertEqual(first, {"decision": "block", "reason": REVIEWER_STOP_REASON})
        self.assertEqual(second, {})
        self.assertEqual([e["event"] for e in self.ledger()["agents"]], ["start", "stop", "stop"])

    def test_reviewer_that_recorded_coverage_may_stop(self) -> None:
        self.repo.cli("review", "prepare", "--tier", "standard")
        reviewer = {"agent_id": "r-2", "agent_type": "craftsman-reviewer"}
        call_hook("subagent-start", "copilot", vscode("SubagentStart", self.root, **reviewer))
        self.repo.cli(
            "review", "cover", "--lens", "intent", "--hunks", "H1", "--note", coverage_note(["H1"])
        )

        out, _ = call_hook(
            "subagent-stop",
            "copilot",
            vscode("SubagentStop", self.root, stop_hook_active=False, **reviewer),
        )

        self.assertEqual(out, {})

    def stop_verifier_with_a_pending_verdict(self, *, stop_hook_active: bool) -> dict[str, Any]:
        self.repo.cli("review", "prepare", "--tier", "standard")
        self.add_finding()
        verifier = {"agent_id": "v-1", "agent_type": "craftsman-verifier"}
        call_hook("subagent-start", "claude", claude("SubagentStart", self.root, **verifier))
        payload = claude("SubagentStop", self.root, stop_hook_active=stop_hook_active, **verifier)
        return call_hook("subagent-stop", "claude", payload)[0]

    def test_verifier_stop_with_pending_verdicts_blocks(self) -> None:
        out = self.stop_verifier_with_a_pending_verdict(stop_hook_active=False)
        self.assertEqual(out["decision"], "block")
        self.assertTrue(out["reason"].startswith("Findings still without a verdict: F1. "))

    def test_verifier_sent_back_once_may_stop(self) -> None:
        out = self.stop_verifier_with_a_pending_verdict(stop_hook_active=True)
        self.assertEqual(out, {})

    def test_verifier_that_judged_its_findings_may_stop_while_others_wait(self) -> None:
        self.repo.cli("review", "prepare", "--tier", "standard")
        self.add_finding()
        self.add_finding()
        verifier = {"agent_id": "v-2", "agent_type": "craftsman-verifier"}
        call_hook("subagent-start", "claude", claude("SubagentStart", self.root, **verifier))
        self.repo.cli(
            "review", "verdict", "F1", "--as", "dismissed",
            "--evidence", "cart.py:2 only receives numbers from the typed API layer",
        )  # fmt: skip

        out, _ = call_hook("subagent-stop", "claude", claude("SubagentStop", self.root, **verifier))

        self.assertEqual(out, {})

    def start_review_only_verifier(self) -> dict[str, Any]:
        """Two findings in review-only mode (no active gate), then the verifier starts."""
        self.update_state(active=False)
        self.repo.cli("review", "prepare", "--tier", "standard")
        self.add_finding()
        self.add_finding()
        payload = claude("SubagentStart", self.root, **REVIEW_ONLY_VERIFIER)
        return call_hook("subagent-start", "claude", payload)[0]

    def test_review_only_verifier_brief_names_uncovered_sweep_hunks(self) -> None:
        context = self.start_review_only_verifier()["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Uncovered `sweep` hunks", context)
        self.assertIn(": H1", context.split("Uncovered `sweep` hunks")[1].splitlines()[0])

    def test_merge_does_not_count_as_a_verdict(self) -> None:
        self.start_review_only_verifier()
        self.repo.cli("review", "merge", "F1", "F2")
        payload = claude("SubagentStop", self.root, stop_hook_active=False, **REVIEW_ONLY_VERIFIER)
        blocked, _ = call_hook("subagent-stop", "claude", payload)
        self.assertIn("Findings still without a verdict: F1.", blocked["reason"])

    def test_other_agents_outside_a_session_are_not_recorded(self) -> None:
        self.update_state(active=False)
        self.repo.cli("review", "prepare", "--tier", "standard")
        agents_before = list(self.ledger()["agents"])
        for event, extra in (
            ("subagent-start", {"agent_id": "e-1", "agent_type": "Explore"}),
            ("subagent-stop", {"agent_id": "e-1", "agent_type": "Explore"}),
            ("subagent-stop", {"agent_id": "e-1"}),
            ("subagent-start", {"agent_id": "g-1", "agent_type": "general-purpose"}),
            ("subagent-stop", {"agent_id": "g-1", "agent_type": "general-purpose"}),
        ):
            with self.subTest(event=event, extra=extra):
                out, _ = call_hook(event, "claude", claude("Subagent", self.root, **extra))
                self.assertEqual(out, {})
                self.assertEqual(self.ledger()["agents"], agents_before)

    def test_craftsman_agents_of_a_stale_round_are_ignored(self) -> None:
        self.update_state(active=False)
        self.repo.cli("review", "prepare", "--tier", "standard")
        agents_before = list(self.ledger()["agents"])
        self.repo.write("cart.py", CHANGE + "# stale round\n")
        for role_name in ("craftsman-reviewer", "craftsman-verifier"):
            with self.subTest(agent=role_name):
                agent = {"agent_id": "stale", "agent_type": role_name}
                start_out, _ = call_hook(
                    "subagent-start", "claude", claude("SubagentStart", self.root, **agent)
                )
                stop_out, _ = call_hook(
                    "subagent-stop", "claude", claude("SubagentStop", self.root, **agent)
                )
                self.assertEqual((start_out, stop_out), ({}, {}))
                self.assertEqual(self.ledger()["agents"], agents_before)

    def test_a_stop_with_only_an_agent_id_is_matched_to_its_start(self) -> None:
        self.repo.cli("review", "prepare", "--tier", "standard")
        start = vscode("SubagentStart", self.root, agent_id="r-7", agent_type="craftsman-reviewer")
        call_hook("subagent-start", "copilot", start)

        out, _ = call_hook("subagent-stop", "copilot", vscode("SubagentStop", self.root, agent_id="r-7"))

        self.assertEqual(out, {"decision": "block", "reason": REVIEWER_STOP_REASON})
        self.assertEqual(self.ledger()["agents"][-1]["agent"], "craftsman-reviewer")


class PreToolTests(HookTestCase):
    def shell_payloads(self, command: str) -> list[tuple[str, dict[str, Any]]]:
        """`command` as each platform's shell tool sends it."""
        transcript = self.tmp / "claude.jsonl"
        transcript.write_text("", encoding="utf-8")
        return [
            ("copilot", vscode("PreToolUse", self.root, tool_name="run_in_terminal", tool_use_id="t1", tool_input={"command": command, "explanation": "run"})),
            ("copilot", copilot_cli(self.root, toolName="bash", toolArgs=json.dumps({"command": command}))),
            ("copilot", copilot_cli(self.root, toolName="powershell", toolArgs={"command": command})),
            ("claude", claude("PreToolUse", self.root, tool_name="Bash", tool_input={"command": command}, transcript_path=str(transcript))),
        ]  # fmt: skip

    def test_harmless_command_is_allowed_for_every_payload_flavor(self) -> None:
        for platform, payload in self.shell_payloads("git status"):
            with self.subTest(platform=platform, payload=payload):
                self.assertEqual(call_hook("pre-tool", platform, payload)[0], {})

    def test_git_commit_or_push_is_allowed_for_every_payload_flavor(self) -> None:
        commit = 'git commit -m "feat: x" -m "Co-authored-by: Claude <noreply@anthropic.com>"'
        for command in ("git push origin main", commit):
            for platform, payload in self.shell_payloads(command):
                with self.subTest(command=command, platform=platform):
                    self.assertEqual(call_hook("pre-tool", platform, payload)[0], {})

    def test_non_shell_tool_is_not_guarded(self) -> None:
        edit = vscode(
            "PreToolUse",
            self.root,
            tool_name="editFiles",
            tool_input={"command": "git push origin main"},
        )
        self.assertEqual(call_hook("pre-tool", "copilot", edit)[0], {})

    def test_shell_commands_mark_the_session_as_hooked(self) -> None:
        self.repo.cli("gate", "--init", "--tier", "standard", "--cwd", ".")
        self.assertEqual(self.state()["isolation_evidence"], "none")
        payload = vscode(
            "PreToolUse", self.root, tool_name="run_in_terminal",
            tool_input={"command": "craftsman gate --complete"},
        )  # fmt: skip
        self.assertEqual(call_hook("pre-tool", "copilot", payload)[0], {})
        self.assertEqual(self.state()["isolation_evidence"], "hooks")

    def test_guard_failure_asks_the_user(self) -> None:
        payload = claude("PreToolUse", self.root, tool_name="Bash", tool_input={"command": "git status"})
        with mock.patch.object(hooks, "find_worktree_mutation", side_effect=RuntimeError("boom")):
            out, _ = call_hook("pre-tool", "claude", payload)
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "ask")

    def test_guard_failure_is_denied_on_copilot(self) -> None:
        payload = vscode(
            "PreToolUse", self.root, tool_name="run_in_terminal", tool_input={"command": "ls"}
        )
        with mock.patch.object(hooks, "find_worktree_mutation", side_effect=RuntimeError("boom")):
            out, _ = call_hook("pre-tool", "copilot", payload)
        self.assertEqual(out["permissionDecision"], "deny")

    def test_invalid_gate_state_asks_before_a_search(self) -> None:
        self.init("standard")
        self.update_state(owner=None)
        payload = claude("PreToolUse", self.root, tool_name="Grep", tool_input={"pattern": "x"})
        out, _ = call_hook("pre-tool", "claude", payload)
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "ask")

    def test_non_scope_checked_tools_skip_load_scope_policy_subprocesses(self) -> None:
        self.repo.cli("gate", "--init", "--tier", "standard", "--cwd", ".")
        with mock.patch("craftsman.hooks.load_scope_policy") as mock_policy:
            for tool in ("Edit", "Write", "editFiles", "replace_string_in_file"):
                out, _ = call_hook(
                    "pre-tool",
                    "copilot",
                    vscode("PreToolUse", self.root, tool_name=tool, tool_input={"file_path": "a.py"}),
                )
                self.assertEqual(out, {})
            mock_policy.assert_not_called()


class HookInputTests(unittest.TestCase):
    def test_snake_case_dialect_is_read(self) -> None:
        hook = HookInput.parse(
            {"cwd": "/w", "agent_type": "craftsman-reviewer", "agent_id": "a", "stop_hook_active": True,
             "tool_name": "run_in_terminal", "tool_input": {"command": "ls"}}
        )  # fmt: skip
        self.assertEqual(
            (hook.agent, hook.agent_id, hook.stop_hook_active, hook.tool_input),
            ("craftsman-reviewer", "a", True, {"command": "ls"}),
        )

    def test_camel_case_dialect_is_read(self) -> None:
        hook = HookInput.parse(
            {"cwd": "/w", "agentType": "custom", "agentName": "craftsman-reviewer", "agentId": "a",
             "toolName": "bash", "toolArgs": '{"command": "ls"}'}
        )  # fmt: skip
        self.assertEqual(
            (hook.agent, hook.agent_id, hook.stop_hook_active, hook.tool_input),
            ("craftsman-reviewer", "a", False, {"command": "ls"}),
        )

    def test_tool_args_that_are_not_json_are_an_error(self) -> None:
        with self.assertRaises(HookInputError):
            HookInput.parse({"toolArgs": '{"command": "ls"'})

    def test_tool_input_that_is_not_an_object_is_an_error(self) -> None:
        with self.assertRaises(HookInputError):
            HookInput.parse({"tool_input": ["git", "push"]})


class SessionOwnershipTests(HookTestCase):
    def test_non_owner_stops_leave_owner_continuation_budget_intact(self) -> None:
        call_hook(
            "prompt",
            "copilot",
            vscode("UserPromptSubmit", self.root, prompt="/craftsman --deep build cart"),
        )
        self.repo.write("cart.py", CHANGE)
        raw_before = self.state_file.read_bytes()
        state_before = self.state()
        self.assertEqual(state_before["owner"], {"platform": "copilot", "session_id": "vscode-session"})

        for _ in range(10):
            out, _ = call_hook("stop", "claude", claude("Stop", self.root))
            self.assertEqual(out, {})
            self.assertEqual(self.state_file.read_bytes(), raw_before)

        antigravity_stop = {
            "terminationReason": "NO_TOOL_CALL",
            "artifactDirectoryPath": str(self.tmp / "brain" / "conv-x"),
            "workspacePaths": [str(self.root)],
        }
        self.assertEqual(antigravity.answer(antigravity_stop), {"decision": "allow"})
        self.assertEqual(self.state_file.read_bytes(), raw_before)

        owner_out, _ = call_hook("stop", "copilot", vscode("Stop", self.root))
        self.assertEqual(owner_out["decision"], "block")
        self.assertTrue(owner_out["reason"].startswith(header(1)))
        self.assertEqual(self.state()["forced_continuations"], 1)

    def test_same_platform_different_session_id_is_non_owner(self) -> None:
        call_hook(
            "prompt",
            "claude",
            claude("UserPromptSubmit", self.root, session_id="claude-A", prompt="/craftsman fix"),
        )
        self.repo.write("cart.py", CHANGE)
        self.repo.cli("gate", "--await-user", "--cwd", ".", "--session-id", "claude-A")
        self.update_state(forced_continuations=3)
        raw_before = self.state_file.read_bytes()

        out_p, _ = call_hook(
            "prompt",
            "claude",
            claude("UserPromptSubmit", self.root, session_id="claude-B", prompt="hello"),
        )
        self.assertEqual(out_p, {})
        self.assertEqual(self.state_file.read_bytes(), raw_before)

        self.update_state(awaiting_user=False)
        raw_unpaused = self.state_file.read_bytes()
        out_s, _ = call_hook(
            "stop",
            "claude",
            claude("Stop", self.root, session_id="claude-B"),
        )
        self.assertEqual(out_s, {})
        self.assertEqual(self.state_file.read_bytes(), raw_unpaused)

        owner_s, _ = call_hook(
            "stop",
            "claude",
            claude("Stop", self.root, session_id="claude-A"),
        )
        self.assertEqual(owner_s["decision"], "block")
        self.assertTrue(owner_s["reason"].startswith(header(4)))

    def open_claude_gate_from_the_terminal(self) -> None:
        self.repo.cli("gate", "--init", "--tier", "quick", "--cwd", ".", env_extra={"CLAUDECODE": "1"})
        self.repo.write("cart.py", CHANGE)

    def test_another_platforms_stop_leaves_the_gate_unclaimed(self) -> None:
        self.open_claude_gate_from_the_terminal()
        out, _ = call_hook("stop", "copilot", vscode("Stop", self.root))
        self.assertEqual(out, {})
        self.assertEqual(self.state()["owner"], {"platform": "claude", "session_id": None})

    def test_first_stop_of_the_owning_platform_claims_the_gate(self) -> None:
        self.open_claude_gate_from_the_terminal()
        call_hook("stop", "claude", claude("Stop", self.root, session_id="claude-1"))
        state = self.state()
        self.assertEqual(
            (state["owner"], state["isolation_evidence"]),
            ({"platform": "claude", "session_id": "claude-1"}, "hooks"),
        )

    def open_antigravity_gate_from_the_terminal(self) -> None:
        self.repo.cli(
            "gate", "--init", "--tier", "standard", "--cwd", ".",
            env_extra={"ANTIGRAVITY_AGENT": "1"},
        )  # fmt: skip
        self.repo.write("cart.py", CHANGE)
        self.repo.cli("review", "prepare", "--tier", "standard")

    def test_tool_call_of_another_session_does_not_claim_the_gate(self) -> None:
        self.open_antigravity_gate_from_the_terminal()
        payload = claude("PreToolUse", self.root, tool_name="Bash", tool_input={"command": "git status"})
        self.assertEqual(call_hook("pre-tool", "claude", payload)[0], {})
        self.assertEqual(self.state()["owner"], {"platform": "antigravity", "session_id": None})

    def test_worktree_guard_blocks_reviewer_in_a_session_that_does_not_own_the_gate(self) -> None:
        self.open_antigravity_gate_from_the_terminal()
        payload = vscode(
            "PreToolUse", self.root, agent_type="craftsman-reviewer", tool_name="run_in_terminal",
            tool_input={"command": "git checkout -- ."},
        )  # fmt: skip
        out, _ = call_hook("pre-tool", "copilot", payload)
        self.assertEqual(out["permissionDecision"], "deny")

    def test_subagent_hooks_ignore_non_owner_sessions(self) -> None:
        self.open_antigravity_gate_from_the_terminal()
        sub_out, _ = call_hook(
            "subagent-start",
            "claude",
            claude("SubagentStart", self.root, agent_id="x", agent_type="Explore"),
        )
        self.assertEqual(sub_out, {})
        self.assertEqual(self.ledger()["agents"], [])
        self.assertEqual(self.state()["isolation_evidence"], "none")

    def test_craftsman_trigger_in_other_session_preserves_owner_gate(self) -> None:
        call_hook(
            "prompt",
            "copilot",
            vscode("UserPromptSubmit", self.root, prompt="/craftsman --deep original task"),
        )
        self.update_state(forced_continuations=2)
        raw_before = self.state_file.read_bytes()

        out, _ = call_hook(
            "prompt",
            "claude",
            claude("UserPromptSubmit", self.root, prompt="/craftsman --quick hijack task"),
        )
        context = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("already active and owned by another session", context)
        self.assertIn("copilot (session vscode-session)", context)
        self.assertIn("git worktree add", context)
        self.assertIn("--takeover", context)
        self.assertEqual(self.state_file.read_bytes(), raw_before)

    def test_reviewer_hook_events_let_a_deep_gate_close_as_isolated(self) -> None:
        self.init("deep")
        self.repo.write("cart.py", CHANGE)
        self.repo.record_full_evidence("deep")
        reviewer = {"agent_id": "a1", "agent_type": "craftsman-reviewer"}
        call_hook("subagent-start", "claude", claude("SubagentStart", self.root, **reviewer))
        call_hook(
            "subagent-stop",
            "claude",
            claude("SubagentStop", self.root, stop_hook_active=True, **reviewer),
        )

        completed = self.repo.cli("gate", "--complete", "--cwd", ".", "--session-id", "claude-session")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual((self.state()["active"], self.state()["stage"]), (False, "completed"))
        report = self.repo.cli("review", "report").stdout
        self.assertNotIn("SELF-REVIEWED", report)


if __name__ == "__main__":
    unittest.main()
