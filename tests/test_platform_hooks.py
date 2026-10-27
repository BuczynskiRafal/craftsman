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

from craftsman import hooks
from craftsman.hooks import REVIEWER_STOP_REASON, HookInput, task_from_prompt

from tests.support import EvidenceRepo, coverage_note

CHANGE = "def total(items):\n    return sum(items)\n"
STARTED = (
    "Craftsman session started (tier {tier}, base {base}). Follow the /craftsman skill "
    "(Stage 0 command: `craftsman gate --init --session-id {sid} --tier {tier} --task \"{task}\"`); "
    "the gate will block finishing until `craftsman review check` passes.\n"
    "Scope: work only inside {root}; see Search scope."
)


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
            ("prompt", "copilot", vscode("UserPromptSubmit", self.root, prompt="what does /craftsman do?")),
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

    def test_malformed_stdin_allows(self) -> None:
        for event in hooks.EVENTS:
            with self.subTest(event=event):
                out, err = call_hook(event, "copilot", "{not json")
                self.assertEqual(out, {})
                self.assertIn("ignoring unreadable hook input", err)
        self.assertEqual(call_hook("stop", "claude", "[1, 2]")[0], {})

    def test_invalid_arguments_allow_with_an_error_line(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdin", io.StringIO("{}")), redirect_stdout(out), redirect_stderr(err):
            code = hooks.main(["finish", "--platform", "copilot"])
        self.assertEqual((code, out.getvalue()), (0, "{}\n"))
        self.assertIn("craftsman hook error: UsageError: argument event: invalid choice", err.getvalue())


class PromptTests(HookTestCase):
    def test_trigger_initializes_state_like_gate_init(self) -> None:
        payload = vscode("UserPromptSubmit", self.root, prompt="/craftsman --deep add cart totals")
        out, _ = call_hook("prompt", "copilot", payload)

        context = STARTED.format(
            tier="deep",
            base=self.repo.base_sha[:7],
            sid="vscode-session",
            task="add cart totals",
            root=self.root.resolve(),
        )
        self.assertEqual(
            out,
            {
                "additionalContext": context,
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": context,
                },
            },
        )
        state = self.state()
        self.assertEqual(
            {key: state[key] for key in ("active", "tier", "base_sha", "task", "hooks_seen", "cwd")},
            {
                "active": True,
                "tier": "deep",
                "base_sha": self.repo.base_sha,
                "task": "add cart totals",
                "hooks_seen": "copilot",
                "cwd": str(self.root),
            },
        )

    def test_copilot_cli_and_claude_prompts(self) -> None:
        out, _ = call_hook(
            "prompt", "copilot", copilot_cli(self.root, prompt="ultracraft refactor the cart")
        )
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")
        self.assertEqual((self.state()["tier"], self.state()["task"]), ("deep", "refactor the cart"))

        again, _ = call_hook(
            "prompt", "copilot", copilot_cli(self.root, prompt="/craftsman --quick other")
        )
        self.assertEqual(again, {})
        self.assertEqual(self.state()["task"], "refactor the cart")

        self.update_state(active=False)
        out, _ = call_hook(
            "prompt", "claude", claude("UserPromptSubmit", self.root, prompt="/craftsman fix it")
        )
        context = STARTED.format(
            tier="auto",
            base=self.repo.base_sha[:7],
            sid="claude-session",
            task="fix it",
            root=self.root.resolve(),
        )
        self.assertEqual(
            out,
            {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": context}},
        )
        self.assertEqual((self.state()["tier"], self.state()["hooks_seen"]), ("auto", "claude"))

    def test_task_strips_the_trigger_and_is_capped(self) -> None:
        self.assertEqual(task_from_prompt("/craftsman --standard  fix\nthe bug"), "fix the bug")
        self.assertEqual(
            task_from_prompt(
                "<command-name>/craftsman</command-name><command-args>--deep tidy</command-args>"
            ),
            "tidy",
        )
        self.assertEqual(len(task_from_prompt("ultracraft " + "x" * 500)), 300)


class StopTests(HookTestCase):
    def test_blocks_with_next_actions_then_allows_once_evidence_is_complete(self) -> None:
        self.init("quick")
        self.repo.write("cart.py", CHANGE)

        out, _ = call_hook("stop", "copilot", vscode("Stop", self.root, stop_hook_active=False))
        self.assertEqual(set(out), {"decision", "reason", "hookSpecificOutput"})
        reason = out["reason"]
        self.assertEqual(out["decision"], "block")
        self.assertEqual(
            out["hookSpecificOutput"], {"hookEventName": "Stop", "decision": "block", "reason": reason}
        )
        self.assertTrue(reason.startswith("[CRAFTSMAN GATE 1/6] "))
        self.assertIn("1. [no-round]", reason)
        self.assertLessEqual(len(reason), 1800)
        self.assertEqual(self.state()["hooks_seen"], "copilot")

        claude_out, _ = call_hook(
            "stop",
            "claude",
            claude("Stop", self.root, stop_hook_active=True),
        )
        # Non-owner platform is allowed without incrementing forced_continuations
        self.assertEqual(claude_out, {})
        self.assertEqual(self.state()["forced_continuations"], 1)

        # Legacy/unowned state (or after takeover) blocks in Claude dialect too
        self.update_state(owner={"platform": "claude", "session_id": "claude-session"})
        claude_block, _ = call_hook(
            "stop", "claude", claude("Stop", self.root, stop_hook_active=True)
        )
        self.assertEqual(set(claude_block), {"decision", "reason"})
        self.assertTrue(claude_block["reason"].startswith("[CRAFTSMAN GATE 2/6] "))

        self.update_state(owner={"platform": "copilot", "session_id": "cli-session"})
        self.repo.record_full_evidence("quick")
        done, _ = call_hook("stop", "copilot", copilot_cli(self.root, stopReason="end_turn"))
        self.assertEqual(done, {})
        self.assertEqual((self.state()["active"], self.state()["stage"]), (False, "completed"))

    def test_isolation_is_required_once_hooks_are_seen(self) -> None:
        self.init("standard")
        self.repo.write("cart.py", CHANGE)
        self.repo.cli("review", "prepare", "--tier", "standard")
        self.repo.cover_all()
        self.repo.cli("run", "--kind", "test", "--", sys.executable, "-c", "print('ok')")

        blocked, _ = call_hook("stop", "claude", claude("Stop", self.root))
        self.assertIn("[no-isolation]", blocked["reason"])
        self.assertEqual(self.repo.cli("review", "check", check=False).returncode, 1)

        reviewer = {"agent_id": "a1", "agent_type": "craftsman-reviewer"}
        call_hook("subagent-start", "claude", claude("SubagentStart", self.root, **reviewer))
        stopped, _ = call_hook(
            "subagent-stop",
            "claude",
            claude("SubagentStop", self.root, stop_hook_active=True, **reviewer),
        )
        self.assertEqual(stopped, {})

        self.assertEqual(call_hook("stop", "claude", claude("Stop", self.root))[0], {})
        self.assertEqual(self.state()["stage"], "completed")

    def test_cap_allows_and_marks_aborted_whatever_the_platform_sends(self) -> None:
        self.init("quick")
        self.repo.write("cart.py", CHANGE)
        self.update_state(forced_continuations=2)

        out, _ = call_hook("stop", "copilot", vscode("Stop", self.root, stop_hook_active=True))
        self.assertTrue(out["reason"].startswith("[CRAFTSMAN GATE 3/6] "))
        self.update_state(owner={"platform": "copilot", "session_id": "cli-session"})
        cli_stop, _ = call_hook("stop", "copilot", copilot_cli(self.root))  # no stop_hook_active
        self.assertTrue(cli_stop["reason"].startswith("[CRAFTSMAN GATE 4/6] "))

        self.update_state(forced_continuations=6)
        capped, err = call_hook("stop", "copilot", copilot_cli(self.root))
        self.assertEqual(set(capped), {"systemMessage"})
        self.assertIn("Craftsman gate not satisfied", capped["systemMessage"])
        self.assertIn("Craftsman gate not satisfied", err)
        self.assertEqual(
            (self.state()["active"], self.state()["stage"]), (False, "aborted_max_continuations")
        )

    def test_only_a_new_best_issue_count_resets_the_streak(self) -> None:
        self.init("quick")
        self.repo.write("cart.py", CHANGE)
        self.update_state(forced_continuations=6, best_issue_count=5)

        out, _ = call_hook("stop", "claude", claude("Stop", self.root, stop_hook_active=True))
        self.assertTrue(out["reason"].startswith("[CRAFTSMAN GATE 1/6] "))
        self.assertEqual(self.state()["best_issue_count"], 2)

        self.update_state(forced_continuations=6, best_issue_count=1)  # worse than the best: no reset
        capped, _ = call_hook("stop", "claude", claude("Stop", self.root, stop_hook_active=True))
        self.assertEqual(set(capped), {"systemMessage"})

    def test_a_user_prompt_starts_a_new_streak_but_a_gate_continuation_does_not(self) -> None:
        self.init("quick")
        self.repo.write("cart.py", CHANGE)
        self.repo.cli("gate", "--await-user", "--cwd", ".")
        self.update_state(forced_continuations=4, best_issue_count=2)

        call_hook(
            "prompt",
            "claude",
            claude("UserPromptSubmit", self.root, prompt="[CRAFTSMAN GATE 4/6] 1. ..."),
        )
        self.assertEqual(self.state()["forced_continuations"], 4)

        call_hook("prompt", "claude", claude("UserPromptSubmit", self.root, prompt="Use option B"))
        state = self.state()
        self.assertEqual((state["forced_continuations"], state["awaiting_user"]), (0, False))
        self.assertNotIn("best_issue_count", state)
        blocked, _ = call_hook("stop", "claude", claude("Stop", self.root))
        self.assertEqual(blocked["decision"], "block")

    def test_trigger_during_a_session_upgrades_the_tier_and_restarts_after_round_cap(self) -> None:
        self.init("quick")
        self.repo.write("cart.py", CHANGE)
        call_hook("prompt", "claude", claude("UserPromptSubmit", self.root, prompt="/craftsman --deep go"))
        self.assertEqual(self.state()["tier"], "deep")
        call_hook("prompt", "claude", claude("UserPromptSubmit", self.root, prompt="/craftsman --quick"))
        self.assertEqual(self.state()["tier"], "deep")

        self.repo.cli("review", "prepare", "--tier", "quick")
        self.update_state(stage="needs_human")
        out, _ = call_hook("prompt", "claude", claude("UserPromptSubmit", self.root, prompt="/craftsman retry"))
        self.assertIn("Craftsman session started (tier", out["hookSpecificOutput"]["additionalContext"])
        self.assertEqual((self.state()["stage"], self.state()["task"]), ("implement", "retry"))
        self.assertFalse((self.repo.ledger_dir / "ledger.json").exists())

    def test_awaiting_user_allows(self) -> None:
        self.init("quick")
        self.repo.write("cart.py", CHANGE)
        self.repo.cli("gate", "--await-user", "--cwd", ".")
        self.assertEqual(call_hook("stop", "claude", claude("Stop", self.root))[0], {})

    def test_round_cap_allows_with_a_system_message(self) -> None:
        self.init("quick")
        for revision in range(6):
            self.repo.write("cart.py", CHANGE + f"# revision {revision}\n")
            self.repo.cli("review", "prepare", "--tier", "quick")

        out, _ = call_hook("stop", "copilot", vscode("Stop", self.root, stop_hook_active=False))

        self.assertEqual(set(out), {"systemMessage"})
        self.assertIn("round cap", out["systemMessage"])
        self.assertEqual(self.state()["stage"], "needs_human")

    def test_internal_errors_allow_and_are_reported(self) -> None:
        self.init("quick")
        self.repo.write("cart.py", CHANGE)
        with mock.patch.object(hooks, "evaluate_task", side_effect=RuntimeError("boom")):
            out, err = call_hook("stop", "copilot", vscode("Stop", self.root))
        self.assertEqual(set(out), {"systemMessage"})
        self.assertIn("quality gate was not checked", out["systemMessage"])
        self.assertEqual(err, "craftsman hook error: RuntimeError: boom\n")

        with mock.patch.object(hooks, "open_session", side_effect=OSError("disk gone")):
            out, err = call_hook("subagent-start", "claude", claude("SubagentStart", self.root))
        self.assertEqual(out, {})
        self.assertEqual(err, "craftsman hook error: OSError: disk gone\n")


class SubagentTests(HookTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.init("standard")
        self.repo.write("cart.py", CHANGE)

    def test_reviewer_start_is_recorded_and_briefed(self) -> None:
        self.repo.cli("review", "prepare", "--tier", "standard")
        payload = vscode("SubagentStart", self.root, agent_id="r-1", agent_type="craftsman-reviewer")

        out, _ = call_hook("subagent-start", "copilot", payload)

        context = out["additionalContext"]
        self.assertEqual(
            out["hookSpecificOutput"], {"hookEventName": "SubagentStart", "additionalContext": context}
        )
        packet = self.repo.ledger_dir / "packet-1.md"
        self.assertTrue(context.startswith(f"Craftsman review round 1. Packet: {packet}\n"))
        self.assertIn("- intent: H1\n", context)
        self.assertIn("- sweep: H1 (run last)\n", context)
        self.assertIn("Recording commands:\n- Cover (1–4 hunks per call", context)
        cheat_sheet = context.split("Recording commands:\n")[1].splitlines()
        self.assertEqual(sum(line.startswith("- ") for line in cheat_sheet), 5)
        events = self.ledger()["agents"]
        self.assertEqual(
            [(e["platform"], e["event"], e["agent"], e["agent_id"]) for e in events],
            [("copilot", "start", "craftsman-reviewer", "r-1")],
        )

    def test_verifier_brief_lists_findings_and_other_agents_are_only_recorded(self) -> None:
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
        self.assertEqual(set(out), {"additionalContext", "hookSpecificOutput"})

        self.update_state(owner={"platform": "claude", "session_id": "claude-session"})
        other, _ = call_hook(
            "subagent-start",
            "claude",
            claude("SubagentStart", self.root, agent_id="x", agent_type="Explore"),
        )
        other_stop, _ = call_hook(
            "subagent-stop",
            "claude",
            claude("SubagentStop", self.root, agent_id="x", agent_type="Explore"),
        )
        self.assertEqual((other, other_stop), ({}, {}))
        self.assertEqual(
            [(e["event"], e["agent"]) for e in self.ledger()["agents"]],
            [("start", "craftsman-verifier"), ("start", "Explore"), ("stop", "Explore")],
        )
        self.assertEqual(self.state()["hooks_seen"], "claude")  # fmt: skip

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

        self.assertEqual(out, {})  # fmt: skip

    def test_verifier_stop_with_pending_verdicts_blocks(self) -> None:
        self.repo.cli("review", "prepare", "--tier", "standard")
        self.add_finding()
        verifier = {"agent_id": "v-1", "agent_type": "craftsman-verifier"}
        call_hook("subagent-start", "claude", claude("SubagentStart", self.root, **verifier))

        out, _ = call_hook(
            "subagent-stop",
            "claude",
            claude("SubagentStop", self.root, stop_hook_active=False, **verifier),
        )
        self.assertEqual(out["decision"], "block")
        self.assertTrue(out["reason"].startswith("Findings still without a verdict: F1. "))

        active, _ = call_hook(
            "subagent-stop",
            "claude",
            claude("SubagentStop", self.root, stop_hook_active=True, **verifier),
        )
        self.assertEqual(active, {})  # fmt: skip


    def test_a_verifier_that_judged_its_findings_may_stop_while_others_are_pending(self) -> None:
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

    def test_verifier_merge_does_not_count_as_judging_and_review_only_briefs_sweep(self) -> None:
        self.update_state(active=False)
        self.repo.cli("review", "prepare", "--tier", "standard")
        self.add_finding()
        self.add_finding()
        verifier = {"agent_id": "v-ro", "agent_type": "craftsman-verifier"}

        started, _ = call_hook("subagent-start", "claude", claude("SubagentStart", self.root, **verifier))
        context = started["hookSpecificOutput"]["additionalContext"]
        self.assertIn(
            "Uncovered `sweep` hunks (in review-only mode without --fix, cover `sweep` in Step 0 before verdicts): H1",
            context,
        )
        self.assertIn("(with --fix, a separate `craftsman-reviewer` covers `sweep`)", context)

        self.repo.cli("review", "merge", "F1", "F2")
        blocked, _ = call_hook(
            "subagent-stop", "claude", claude("SubagentStop", self.root, stop_hook_active=False, **verifier)
        )
        self.assertEqual(blocked["decision"], "block")
        self.assertIn("Findings still without a verdict: F1.", blocked["reason"])

    def test_outside_session_non_craftsman_agents_and_stale_rounds_are_ignored(self) -> None:
        self.update_state(active=False)
        self.repo.cli("review", "prepare", "--tier", "standard")
        self.add_finding()
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

        self.repo.write("cart.py", CHANGE + "# stale round\n")
        for role_name in ("craftsman-reviewer", "craftsman-verifier"):
            start_out, _ = call_hook(
                "subagent-start",
                "claude",
                claude("SubagentStart", self.root, agent_id="stale", agent_type=role_name),
            )
            stop_out, _ = call_hook(
                "subagent-stop",
                "claude",
                claude("SubagentStop", self.root, agent_id="stale", agent_type=role_name),
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
    def deny_reason(self, out: dict[str, Any]) -> str:
        reason = out["hookSpecificOutput"]["permissionDecisionReason"]
        assert isinstance(reason, str)
        return reason

    def test_unconfirmed_push_to_main_is_denied_for_every_payload_flavor(self) -> None:
        transcript = self.tmp / "claude.jsonl"
        transcript.write_text("", encoding="utf-8")
        push = "git push origin main"
        for platform, payload in (
            ("copilot", vscode("PreToolUse", self.root, tool_name="run_in_terminal", tool_use_id="t1", tool_input={"command": push, "explanation": "push"})),
            ("copilot", copilot_cli(self.root, toolName="bash", toolArgs=json.dumps({"command": push}))),
            ("copilot", copilot_cli(self.root, toolName="powershell", toolArgs={"command": push})),
            ("claude", claude("PreToolUse", self.root, tool_name="Bash", tool_input={"command": push}, transcript_path=str(transcript))),
        ):  # fmt: skip
            with self.subTest(
                platform=platform, tool=payload.get("tool_name") or payload.get("toolName")
            ):
                out, _ = call_hook("pre-tool", platform, payload)
                reason = self.deny_reason(out)
                self.assertIn("[CRAFTSMAN GIT-GUARD]", reason)
                self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
                self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "PreToolUse")
                if platform == "copilot":
                    self.assertEqual(out["permissionDecision"], "deny")
                    self.assertEqual(out["permissionDecisionReason"], reason)
                else:
                    self.assertEqual(set(out), {"hookSpecificOutput"})

                status_payload = json.loads(json.dumps(payload).replace(push, "git status"))
                self.assertEqual(call_hook("pre-tool", platform, status_payload)[0], {})

    def test_claude_without_a_transcript_asks_the_user(self) -> None:
        payload = claude(
            "PreToolUse", self.root, tool_name="Bash", tool_input={"command": "git push origin main"}
        )
        out, _ = call_hook("pre-tool", "claude", payload)
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "ask")

    def test_attribution_and_non_shell_tools(self) -> None:
        commit = 'git commit -m "feat: x" -m "Co-authored-by: Claude <noreply@anthropic.com>"'
        out, _ = call_hook(
            "pre-tool",
            "copilot",
            vscode("PreToolUse", self.root, tool_name="run_in_terminal", tool_input={"command": commit}),
        )
        self.assertIn("Zero AI Attribution", self.deny_reason(out))
        edit = vscode(
            "PreToolUse",
            self.root,
            tool_name="editFiles",
            tool_input={"command": "git push origin main"},
        )
        self.assertEqual(call_hook("pre-tool", "copilot", edit)[0], {})  # fmt: skip

    def test_powershell_tools_and_relative_working_directories_are_guarded(self) -> None:
        self.repo.git("checkout", "-q", "-b", "main")
        (self.root / "sub").mkdir()
        for tool in ("PowerShell", "pwsh"):
            with self.subTest(tool=tool):
                payload = claude(
                    "PreToolUse", self.root, tool_name=tool,
                    tool_input={"command": "git push", "cwd": "sub"},
                )  # fmt: skip
                out, _ = call_hook("pre-tool", "copilot", payload)
                self.assertEqual(out["permissionDecision"], "deny")

    def test_shell_commands_mark_the_session_as_hooked(self) -> None:
        self.repo.cli("gate", "--init", "--tier", "standard", "--cwd", ".")
        self.assertNotIn("hooks_seen", self.state())
        payload = vscode(
            "PreToolUse", self.root, tool_name="run_in_terminal",
            tool_input={"command": "craftsman gate --complete"},
        )  # fmt: skip
        self.assertEqual(call_hook("pre-tool", "copilot", payload)[0], {})
        self.assertEqual(self.state()["hooks_seen"], "copilot")

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


class TranscriptReviewerTests(unittest.TestCase):
    def test_antigravity_recognizes_the_craftsman_reviewer_role(self) -> None:
        from craftsman.gate import has_quality_gate_subagent_in_entries

        for role in ("Craftsman Reviewer", "craftsman-reviewer", "Craftsman Quality Gate Auditor"):
            with self.subTest(role=role):
                entries = [
                    {
                        "step_index": 3,
                        "type": "PLANNER_RESPONSE",
                        "tool_calls": [
                            {
                                "name": "invoke_subagent",
                                "args": {
                                    "Subagents": json.dumps(
                                        [{"TypeName": "self", "Role": role, "Prompt": "Review lenses contracts, failure for packet-1.md"}]
                                    )
                                },
                            }
                        ],
                    }
                ]
                self.assertTrue(has_quality_gate_subagent_in_entries(entries, 0))


class HookInputTests(unittest.TestCase):
    def test_both_dialects_are_read(self) -> None:
        snake = HookInput.parse(
            {"cwd": "/w", "agent_type": "craftsman-reviewer", "agent_id": "a", "stop_hook_active": True,
             "tool_name": "run_in_terminal", "tool_input": {"command": "ls"}}
        )  # fmt: skip
        camel = HookInput.parse(
            {"cwd": "/w", "agentType": "custom", "agentName": "craftsman-reviewer", "agentId": "a",
             "toolName": "bash", "toolArgs": '{"command": "ls"}'}
        )  # fmt: skip
        self.assertEqual(
            (snake.agent, snake.agent_id, snake.stop_hook_active), ("craftsman-reviewer", "a", True)
        )
        self.assertEqual(
            (camel.agent, camel.agent_id, camel.stop_hook_active), ("craftsman-reviewer", "a", False)
        )
        self.assertEqual((snake.tool_input, camel.tool_input), ({"command": "ls"}, {"command": "ls"}))
        self.assertEqual(HookInput.parse({"toolArgs": "not json"}).tool_input, {})


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
        self.assertEqual(
            state_before["owner"], {"platform": "copilot", "session_id": "vscode-session"}
        )

        for _ in range(10):
            out, _ = call_hook("stop", "claude", claude("Stop", self.root))
            self.assertEqual(out, {})
            self.assertEqual(self.state_file.read_bytes(), raw_before)

        ag_proc = self.repo.cli(
            "gate",
            stdin=json.dumps(
                {
                    "terminationReason": "NO_TOOL_CALL",
                    "workspacePaths": [str(self.root)],
                    "transcript_path": str(self.root / "no-such.jsonl"),
                }
            ),
        )
        self.assertEqual(json.loads(ag_proc.stdout), {"decision": "allow"})
        self.assertEqual(self.state_file.read_bytes(), raw_before)

        owner_out, _ = call_hook("stop", "copilot", vscode("Stop", self.root))
        self.assertEqual(owner_out["decision"], "block")
        self.assertTrue(owner_out["reason"].startswith("[CRAFTSMAN GATE 1/6] "))
        self.assertEqual(self.state()["forced_continuations"], 1)
        self.assertEqual(self.state()["hooks_seen"], "copilot")

    def test_same_platform_different_session_id_is_non_owner(self) -> None:
        call_hook(
            "prompt",
            "claude",
            claude("UserPromptSubmit", self.root, session_id="claude-A", prompt="/craftsman fix"),
        )
        self.repo.write("cart.py", CHANGE)
        self.repo.cli("gate", "--await-user", "--cwd", ".")
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
        self.assertTrue(owner_s["reason"].startswith("[CRAFTSMAN GATE 4/6] "))

    def test_claim_on_cli_init_accepts_only_matching_platform(self) -> None:
        self.repo.cli(
            "gate", "--init", "--tier", "quick", "--cwd", ".", env_extra={"CLAUDECODE": "1"}
        )
        self.repo.write("cart.py", CHANGE)
        self.assertEqual(self.state()["owner"], {"platform": "claude", "session_id": ""})

        cop_out, _ = call_hook("stop", "copilot", vscode("Stop", self.root))
        self.assertEqual(cop_out, {})
        self.assertEqual(self.state()["owner"], {"platform": "claude", "session_id": ""})
        self.assertNotIn("hooks_seen", self.state())

        cl_out, _ = call_hook(
            "stop", "claude", claude("Stop", self.root, session_id="claude-1")
        )
        self.assertEqual(cl_out["decision"], "block")
        self.assertEqual(self.state()["owner"], {"platform": "claude", "session_id": "claude-1"})
        self.assertEqual(self.state()["hooks_seen"], "claude")

        cl2_out, _ = call_hook(
            "stop", "claude", claude("Stop", self.root, session_id="claude-2")
        )
        self.assertEqual(cl2_out, {})
        self.assertEqual(self.state()["forced_continuations"], 1)

    def test_pre_tool_guard_protects_non_owner_without_setting_hooks_seen(self) -> None:
        self.repo.cli(
            "gate",
            "--init",
            "--tier",
            "standard",
            "--cwd",
            ".",
            env_extra={"ANTIGRAVITY_AGENT": "1"},
        )
        self.repo.write("cart.py", CHANGE)
        self.repo.cli("review", "prepare", "--tier", "standard")
        self.assertEqual(self.state()["owner"], {"platform": "antigravity", "session_id": ""})

        pre_out, _ = call_hook(
            "pre-tool",
            "claude",
            claude("PreToolUse", self.root, tool_name="Bash", tool_input={"command": "git status"}),
        )
        self.assertEqual(pre_out, {})
        self.assertNotIn("hooks_seen", self.state())

        push_out, _ = call_hook(
            "pre-tool",
            "copilot",
            vscode(
                "PreToolUse",
                self.root,
                tool_name="run_in_terminal",
                tool_input={"command": "git push origin main"},
            ),
        )
        self.assertEqual(push_out["permissionDecision"], "deny")
        self.assertNotIn("hooks_seen", self.state())

    def test_subagent_hooks_ignore_non_owner_sessions(self) -> None:
        self.repo.cli(
            "gate",
            "--init",
            "--tier",
            "standard",
            "--cwd",
            ".",
            env_extra={"ANTIGRAVITY_AGENT": "1"},
        )
        self.repo.write("cart.py", CHANGE)
        self.repo.cli("review", "prepare", "--tier", "standard")

        sub_out, _ = call_hook(
            "subagent-start",
            "claude",
            claude("SubagentStart", self.root, agent_id="x", agent_type="Explore"),
        )
        self.assertEqual(sub_out, {})
        self.assertEqual(self.ledger()["agents"], [])
        self.assertNotIn("hooks_seen", self.state())

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

    def test_status_reports_gate_owner(self) -> None:
        call_hook(
            "prompt",
            "claude",
            claude("UserPromptSubmit", self.root, prompt="/craftsman --deep owner task"),
        )
        status_json = json.loads(self.repo.cli("status", "--json").stdout)
        self.assertEqual(
            status_json["owner"], {"platform": "claude", "session_id": "claude-session"}
        )
        status_text = self.repo.cli("status").stdout
        self.assertIn("Owner          : claude (session claude-session)", status_text)

    def test_gate_init_refuses_foreign_active_gate_without_takeover(self) -> None:
        call_hook(
            "prompt",
            "claude",
            claude("UserPromptSubmit", self.root, prompt="/craftsman --deep owner task"),
        )
        refused = self.repo.cli(
            "gate",
            "--init",
            "--tier",
            "quick",
            "--cwd",
            ".",
            check=False,
            env_extra={"VSCODE_PID": "999"},
        )
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("already owned by claude (session claude-session)", refused.stderr)
        self.assertIn("git worktree add", refused.stderr)
        self.assertIn("--takeover", refused.stderr)
        self.assertEqual(self.state()["tier"], "deep")

    def test_gate_init_requires_matching_session_id_on_same_platform(self) -> None:
        call_hook(
            "prompt",
            "claude",
            claude("UserPromptSubmit", self.root, prompt="/craftsman --deep owner task"),
        )
        no_sid_refused = self.repo.cli(
            "gate",
            "--init",
            "--tier",
            "quick",
            "--task",
            "hijack task",
            "--cwd",
            ".",
            check=False,
            env_extra={"CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": ""},
        )
        self.assertNotEqual(no_sid_refused.returncode, 0)
        self.assertIn("already owned by claude (session claude-session)", no_sid_refused.stderr)
        self.assertIn("--session-id", no_sid_refused.stderr)
        self.assertEqual(self.state()["tier"], "deep")
        self.assertEqual(self.state()["task"], "owner task")

        by_flag = self.repo.cli(
            "gate",
            "--init",
            "--session-id",
            "claude-session",
            "--tier",
            "deep",
            "--task",
            "refined owner task",
            "--cwd",
            ".",
            env_extra={"CLAUDECODE": "", "CLAUDE_CODE_SESSION_ID": ""},
        )
        self.assertEqual(by_flag.returncode, 0, by_flag.stderr)
        self.assertEqual(
            self.state()["owner"], {"platform": "claude", "session_id": "claude-session"}
        )
        self.assertEqual(self.state()["task"], "refined owner task")

    def test_gate_init_takeover_records_previous_owner(self) -> None:
        call_hook(
            "prompt",
            "claude",
            claude("UserPromptSubmit", self.root, prompt="/craftsman --deep owner task"),
        )
        taken = self.repo.cli(
            "gate",
            "--init",
            "--takeover",
            "--tier",
            "standard",
            "--task",
            "taken over",
            "--cwd",
            ".",
            env_extra={"VSCODE_PID": "999"},
        )
        self.assertEqual(taken.returncode, 0, taken.stderr)
        state = self.state()
        self.assertEqual(state["owner"], {"platform": "copilot", "session_id": ""})
        self.assertEqual(
            state["takeover_from"], {"platform": "claude", "session_id": "claude-session"}
        )
        self.assertIn("takeover_at", state)

    def test_legacy_state_without_owner_treats_every_session_as_owner(self) -> None:
        self.init("quick")
        self.repo.write("cart.py", CHANGE)
        state = self.state()
        state.pop("owner", None)
        self.state_file.write_text(json.dumps(state), encoding="utf-8")

        out1, _ = call_hook("stop", "copilot", vscode("Stop", self.root))
        self.assertTrue(out1["reason"].startswith("[CRAFTSMAN GATE 1/6] "))
        out2, _ = call_hook("stop", "claude", claude("Stop", self.root))
        self.assertTrue(out2["reason"].startswith("[CRAFTSMAN GATE 2/6] "))
        self.assertNotIn("owner", self.state())


if __name__ == "__main__":
    unittest.main()
