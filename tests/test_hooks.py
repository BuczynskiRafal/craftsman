"""Tests for `craftsman hook`: the VS Code Copilot, Copilot CLI and Claude Code lifecycle hooks."""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest import mock

from craftsman import antigravity, command_policy, hook_errors, hooks, review_guard
from craftsman.attribution import agent_runs
from craftsman.gate import MAX_CONTINUATION_REASON_CHARS, MAX_CONTINUATIONS
from craftsman.gatecheck import MAX_ROUNDS
from craftsman.hooks import HookInput, HookInputError
from craftsman.huntview import hunk_ranges
from craftsman.ledger import holds_lock, load
from craftsman.subagent_stop import REVIEWER_STOP_REASON

from tests.support import EvidenceRepo, coverage_note

CHANGE = "def total(items):\n    return sum(items)\n"
STARTED = (
    "Craftsman session started (deep, base {base}). Follow the /craftsman skill "
    '(Stage 0 command: `craftsman gate --init --session-id {sid} --deep --task "{task}"`); '
    "the gate will block finishing until `craftsman review check` passes.\n"
    "Scope: work only inside {root}; see Search scope."
)

REVIEW_ONLY_VERIFIER = {"agent_id": "v-ro", "agent_type": "craftsman-verifier"}


def shell_run(call_id: str, command: str, output: str) -> list[str]:
    """Transcript lines of an agent running `command` and seeing `output`."""
    call = {"type": "tool_use", "id": call_id, "name": "Bash", "input": {"command": command}}
    result = {"type": "tool_result", "tool_use_id": call_id, "content": output}
    return [
        json.dumps({"type": "assistant", "message": {"content": [call]}}),
        json.dumps({"type": "user", "message": {"content": [result]}}),
    ]


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
        # Where Claude Code keeps transcripts; a subagent's transcript is read only from under it (F7.3).
        self.config_dir = self.tmp / "claude-config"
        self.transcripts = self.config_dir / "projects" / "repo"
        self.transcripts.mkdir(parents=True)
        env = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.config_dir)})
        env.start()
        self.addCleanup(env.stop)
        # Hunters started together here would be held back for seconds (`launch_stagger`); no test waits.
        stagger = mock.patch("craftsman.launch_stagger.sleep")
        self.stagger_sleep = stagger.start()
        self.addCleanup(stagger.stop)

    @property
    def state_file(self) -> Path:
        return self.repo.ledger_dir / "craftsman_state.json"

    def state(self) -> dict[str, Any]:
        loaded = json.loads(self.state_file.read_text(encoding="utf-8"))
        assert isinstance(loaded, dict)
        return loaded

    def update_state(self, **changes: Any) -> None:
        self.state_file.write_text(json.dumps({**self.state(), **changes}), encoding="utf-8")

    def init(self, deep: bool = False) -> None:
        self.repo.cli("gate", "--init", *(["--deep"] if deep else []), "--cwd", ".")

    def run_reviewer(self, agent_type: str = "craftsman-reviewer", agent_id: str = "a1") -> None:
        """A reviewer that starts, covers every open hunk (again, if the main session did) and stops."""
        reviewer = {"agent_id": agent_id, "agent_type": agent_type}
        call_hook("subagent-start", "claude", claude("SubagentStart", self.root, **reviewer))
        self.repo.cover_all(again=True)
        call_hook(
            "subagent-stop",
            "claude",
            claude("SubagentStop", self.root, stop_hook_active=True, **reviewer),
        )

    def pre_tool(self, command: str, platform: str = "claude", **extra: Any) -> dict[str, Any]:
        """The PreToolUse hook of the main session's shell call `command`."""
        if platform == "copilot":
            payload = vscode(
                "PreToolUse", self.root, tool_name="run_in_terminal", tool_input={"command": command}, **extra
            )
        else:
            payload = claude("PreToolUse", self.root, tool_name="Bash", tool_input={"command": command}, **extra)
        return call_hook("pre-tool", platform, payload)[0]

    def prepare(self, *args: str, platform: str = "claude") -> None:
        """`craftsman review prepare` as the hooked main session runs it: PreToolUse first, then the command."""
        self.pre_tool(" ".join(["craftsman", "review", "prepare", *args]), platform)
        self.repo.cli("review", "prepare", *args)

    def marker(self) -> dict[str, Any]:
        loaded = json.loads((self.repo.ledger_dir / "review_hooks.json").read_text(encoding="utf-8"))
        assert isinstance(loaded, dict)
        return loaded

    def run_hunter(
        self,
        slot: dict[str, Any],
        *,
        platform: str = "claude",
        agent_id: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """The hunter of `slot` (a slot of `show --json`) as its platform's hooks see it: start, brief, cover, stop.

        Claude Code hunters stop with a transcript that shows the commands they ran; Copilot hunters have no ids
        and no transcript. Returns the SubagentStop answer.
        """
        slot_id, missing = slot["id"], slot["missing"] or slot["hunks"]
        ids = ",".join(missing)
        if platform == "copilot":
            call_hook("subagent-start", "copilot", copilot_cli(self.root, agentName="craftsman-reviewer"))
            self.repo.cli("review", "brief", slot_id)
            self.repo.cli("review", "cover", "--hunter", slot_id, "--hunks", ids, "--note", coverage_note(missing))
            return call_hook("subagent-stop", "copilot", copilot_cli(self.root, agentName="craftsman-reviewer"))[0]
        reviewer = {"agent_id": agent_id or f"h-{slot_id}", "agent_type": "craftsman-reviewer"}
        if session_id is not None:
            reviewer["session_id"] = session_id
        call_hook("subagent-start", "claude", claude("SubagentStart", self.root, **reviewer))
        brief = self.repo.cli("review", "brief", slot_id)
        cover = self.repo.cli("review", "cover", "--hunter", slot_id, "--hunks", ids, "--note", coverage_note(missing))
        lines = [
            *shell_run(f"b-{slot_id}", f"craftsman review brief {slot_id}", brief.stdout),
            *shell_run(f"c-{slot_id}", f'craftsman review cover --hunter {slot_id} --hunks {ids} --note "x"', cover.stdout),
        ]
        return self.stop_with_transcript(reviewer, *lines)

    def unfinished_slots(self) -> list[dict[str, Any]]:
        return [slot for slot in self.repo.show()["slots"] if slot["state"] != "done"]

    def hunt(self, *, platform: str = "claude") -> None:
        """Every unfinished slot of the latest (enforced) round hunted to the end, one hunter after the other."""
        for _ in range(20):  # a hunter may finish other slots by credit; ask again after each
            slots = self.unfinished_slots()
            if not slots:
                return
            self.assertEqual(self.run_hunter(slots[0], platform=platform), {})
        self.fail("the hunt did not finish")

    def stop_with_transcript(self, reviewer: dict[str, str], *lines: str, **extra: Any) -> dict[str, Any]:
        """A subagent stops; its own transcript consists of `lines`."""
        path = self.transcripts / f"agent-{reviewer['agent_id']}.jsonl"  # outside the work tree: it is not part of the diff
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        payload = claude("SubagentStop", self.root, agent_transcript_path=str(path), **reviewer, **extra)
        return call_hook("subagent-stop", "claude", payload)[0]

    def cover_h1(self) -> tuple[str, list[str]]:
        """Cover H1 from the command line: the command, and the transcript lines of an agent that ran it."""
        command = 'craftsman review cover --hunks H1 --note "x"'
        proc = self.repo.cli("review", "cover", "--hunks", "H1", "--note", coverage_note(["H1"]))
        return command, shell_run("cover-1", command, proc.stdout)

    def ledger(self) -> dict[str, Any]:
        loaded = json.loads((self.repo.ledger_dir / "ledger.json").read_text(encoding="utf-8"))
        assert isinstance(loaded, dict)
        return loaded

    def add_finding(self, hunter: str | None = None) -> None:
        """A finding of the orchestrator; in an enforced round it has to name a slot (`lead` is its own)."""
        self.repo.cli(
            "review", "add", "--lens", "contracts", "--file", "cart.py", "--line", "2",
            "--severity", "major", "--title", "Strings are summed without a check",
            "--scenario", "total(['a']) raises TypeError instead of a clear error",
            *(["--hunter", hunter] if hunter else []),
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
                expected = "the gate was NOT opened" if event == "prompt" else "the quality gate was not checked"
                self.assertIn(expected, out["systemMessage"])
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
                for key in ("active", "deep", "base_sha", "task", "owner", "isolation_evidence")
            },
            {
                "active": True,
                "deep": True,
                "base_sha": self.repo.base_sha,
                "task": "add cart totals",
                "owner": {"platform": "copilot", "session_id": "vscode-session"},
                "isolation_evidence": "hooks",
            },
        )

    def test_copilot_cli_trigger_opens_a_gate(self) -> None:
        call_hook("prompt", "copilot", copilot_cli(self.root, prompt="ultracraft refactor the cart"))
        self.assertEqual((self.state()["deep"], self.state()["task"]), (True, "refactor the cart"))

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
            'craftsman gate --init --session-id claude-session --task "fix (touch pwned) and id "',
        )


class ReviewStopGuardTests(HookTestCase):
    """A session that invoked the review skill cannot end its turn without a review outcome."""

    def prompt(self, text: str, **extra: Any) -> dict[str, Any]:
        return call_hook(
            "prompt", "claude", claude("UserPromptSubmit", self.root, prompt=text, **extra)
        )[0]

    def stop(self, **extra: Any) -> dict[str, Any]:
        return call_hook("stop", "claude", claude("Stop", self.root, **extra))[0]

    def guarding(self) -> bool:
        return bool(self.marker()["guarding"])

    def begin_review(self, prompt: str = "/craftsman:craft-review the cart") -> None:
        self.prompt(prompt)
        self.repo.write("cart.py", CHANGE)

    def test_a_review_that_skipped_the_protocol_cannot_stop(self) -> None:
        self.begin_review()

        out = self.stop()

        self.assertEqual(out["decision"], "block")
        self.assertIn(f"[CRAFTSMAN REVIEW 1/{MAX_CONTINUATIONS}]", out["reason"])
        self.assertIn("craftsman review prepare", out["reason"])
        self.assertIn("launch the hunters it lists", out["reason"])
        self.assertFalse((self.repo.ledger_dir / "craftsman_state.json").exists())

    def test_the_hosts_own_review_command_is_never_guarded(self) -> None:
        for prompt in ("/code-review max", "/code-review ultracraft", "/clear /code-review"):
            with self.subTest(prompt=prompt):
                self.begin_review(prompt)

                self.assertEqual(self.stop(), {})
                self.assertFalse((self.repo.ledger_dir / "review_hooks.json").exists())
                self.assertFalse(self.state_file.exists())

    def test_a_reviewer_still_running_lets_the_agent_wait_for_it_without_spending_a_reminder(self) -> None:
        self.begin_review()
        self.prepare()
        reviewer = {"agent_id": "bg-1", "agent_type": "craftsman-reviewer"}
        call_hook("subagent-start", "claude", claude("SubagentStart", self.root, **reviewer))

        waiting = self.stop()  # its completion wakes the session

        self.assertEqual(set(waiting), {"systemMessage"})
        self.assertIn("waiting for craftsman-reviewer", waiting["systemMessage"])
        self.assertIn("`craftsman review check --review-only` will say to relaunch them", waiting["systemMessage"])
        self.assertEqual(self.marker()["forced"], 0)
        self.assertTrue(self.guarding())

        call_hook(
            "subagent-stop",
            "claude",
            claude("SubagentStop", self.root, stop_hook_active=True, **reviewer),
        )
        self.assertIn("[CRAFTSMAN REVIEW 1/", self.stop()["reason"])

    def test_a_claim_without_an_open_run_does_not_wave_the_stop_through(self) -> None:
        self.begin_review()
        self.prepare()
        self.repo.cli("review", "brief", "W1-1")  # the orchestrator fetched a brief itself: no run behind it

        blocked = self.stop()

        self.assertIn("[CRAFTSMAN REVIEW 1/", blocked["reason"])

    def test_the_orchestrator_cannot_fetch_a_brief_of_its_own(self) -> None:
        self.begin_review()
        self.prepare()

        out = self.pre_tool("craftsman review brief W1-1")

        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("Briefs are for the subagents you launch", out["hookSpecificOutput"]["permissionDecisionReason"])
        self.assertEqual(self.pre_tool("craftsman review brief --verifier F1")["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_preparing_an_unchanged_diff_again_counts_as_starting_the_review(self) -> None:
        self.begin_review()
        self.prepare()
        self.hunt()
        self.assertEqual(self.stop(), {})
        self.prompt("/craft-review again")
        self.assertIn("No review round yet", self.stop()["reason"])

        self.prepare()  # an unchanged diff keeps its round and its `prepared_at`

        self.assertEqual(self.stop(), {})

    def test_the_guard_judges_a_copilot_review_like_the_cli_check_does(self) -> None:
        call_hook("prompt", "copilot", vscode("UserPromptSubmit", self.root, prompt="/craft-review"))
        self.repo.write("cart.py", CHANGE)
        self.prepare(platform="copilot")
        self.assertEqual(call_hook("stop", "copilot", vscode("Stop", self.root, stop_hook_active=False))[0]["decision"], "block")
        self.assertEqual(self.repo.cli("review", "check", "--review-only", check=False).returncode, 1)

        self.hunt(platform="copilot")

        checked = self.repo.cli("review", "check", "--review-only", check=False)
        out, _ = call_hook("stop", "copilot", vscode("Stop", self.root, stop_hook_active=False))
        self.assertEqual(checked.returncode, 0, checked.stdout)  # no session id reaches the CLI
        self.assertEqual(out, {})

    def test_a_round_without_a_finished_hunt_names_the_slots(self) -> None:
        self.begin_review()
        self.prepare()

        reason = self.stop()["reason"]

        self.assertIn("[no-isolation]", reason)  # no craftsman subagent was recorded at all
        self.assertIn("W1-1..W1-3", reason)
        self.assertNotIn("review cover --hunks", reason)

    def test_a_finished_review_lets_the_agent_stop_once(self) -> None:
        self.begin_review()
        self.prepare()
        self.hunt()

        self.assertEqual(self.stop(), {})
        self.assertFalse(self.guarding())
        self.repo.write("cart.py", CHANGE + "# later edit\n")
        self.assertEqual(self.stop(), {})

    def test_an_old_round_does_not_satisfy_a_new_review(self) -> None:
        self.begin_review()
        self.prepare()
        self.hunt()
        self.assertEqual(self.stop(), {})

        self.prompt("/craft-review again")

        self.assertEqual(self.stop()["decision"], "block")

    def test_a_deep_request_is_not_met_by_an_earlier_normal_round(self) -> None:
        self.begin_review()
        self.prepare()
        self.hunt()
        self.assertEqual(self.stop(), {})

        self.prompt("/craft-review --deep")
        self.repo.cli("review", "prepare")  # no hook saw it: the round stays normal and advisory

        blocked = self.stop()["reason"]

        self.assertIn("A deep review was requested: run `craftsman review prepare`", blocked)
        self.assertTrue(self.marker()["deep"])

    def test_a_read_only_review_command_does_not_begin_a_new_review(self) -> None:
        self.begin_review()
        self.prepare()
        self.hunt()
        self.assertEqual(self.stop(), {})
        self.prompt("/craft-review again")

        for command in ("craftsman review show", "craftsman review check --review-only"):
            with self.subTest(command=command):
                self.pre_tool(command)  # arms and touches the marker, but nothing was prepared
                self.assertIn("No review round yet", self.stop()["reason"])

    def test_a_prepare_that_failed_does_not_begin_a_new_review(self) -> None:
        self.begin_review()
        self.prepare()
        self.hunt()
        self.assertEqual(self.stop(), {})
        self.prompt("/craft-review again")

        self.pre_tool("craftsman review prepare --paths nothing.py")  # PreToolUse fires before the command runs
        failed = self.repo.cli("review", "prepare", "--paths", "nothing.py", check=False)

        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("No review round yet", self.stop()["reason"])

    def test_an_advisory_round_does_not_satisfy_a_hooked_session(self) -> None:
        self.begin_review()
        self.repo.cli("review", "prepare")  # a command the hooks never saw: an advisory round
        self.pre_tool("craftsman review show")

        blocked = self.stop()["reason"]

        self.assertIn("This session's hooks enforce the review: run `craftsman review prepare`", blocked)

    def test_the_guard_lets_go_after_the_continuation_cap_and_says_the_review_is_not_complete(self) -> None:
        self.begin_review()
        for count in range(1, MAX_CONTINUATIONS + 1):
            self.assertIn(f"[CRAFTSMAN REVIEW {count}/", self.stop()["reason"])

        out = self.stop()

        self.assertEqual(set(out), {"systemMessage"})
        self.assertIn("still incomplete after 6 reminders without progress", out["systemMessage"])
        self.assertIn("The review is NOT complete", out["systemMessage"])
        self.assertIsNotNone(self.marker()["abandoned_at"])
        self.assertFalse(self.guarding())
        self.assertEqual(self.stop(), {})

    def test_progress_resets_the_reminders(self) -> None:
        self.begin_review()
        self.prepare()
        for count in range(1, 4):
            self.assertIn(f"[CRAFTSMAN REVIEW {count}/", self.stop()["reason"])
        slots = self.unfinished_slots()
        self.run_hunter(slots[0])  # vouched coverage: the evidence counter grows

        self.assertIn("[CRAFTSMAN REVIEW 1/", self.stop()["reason"])

    def test_activity_without_evidence_does_not_reset_the_reminders(self) -> None:
        self.begin_review()
        self.prepare()
        for count in range(1, 4):
            self.assertIn(f"[CRAFTSMAN REVIEW {count}/", self.stop()["reason"])
        reviewer = {"agent_id": "idle", "agent_type": "craftsman-reviewer"}
        call_hook("subagent-start", "claude", claude("SubagentStart", self.root, **reviewer))
        call_hook("subagent-stop", "claude", claude("SubagentStop", self.root, stop_hook_active=True, **reviewer))

        self.assertIn("[CRAFTSMAN REVIEW 4/", self.stop()["reason"])

    def test_a_continuation_prompt_keeps_the_guard(self) -> None:
        self.begin_review()
        reason = self.stop()["reason"]

        self.prompt(reason)

        self.assertTrue(self.guarding())

    def test_a_background_agents_report_keeps_the_guard_and_blocks_the_next_stop(self) -> None:
        self.begin_review()
        self.prepare()
        self.hunt()
        self.add_finding("lead")  # no verdict yet
        reports = (
            'Another Claude session sent a message:\n<agent-message from="a1">Hunt done: F1</agent-message>',
            "<task-notification>\n<status>completed</status>\n</task-notification>",
        )
        for report in reports:
            with self.subTest(report=report[:30]):
                self.prompt(report)

                self.assertTrue(self.guarding())
                self.assertIn("[no-verdict]", self.stop()["reason"])

    def test_an_ordinary_prompt_that_mentions_agent_message_ends_the_guard(self) -> None:
        self.begin_review()

        self.prompt("thanks, now explain the <agent-message> format")

        self.assertEqual(self.stop(), {})

    def test_nothing_to_review_is_an_outcome(self) -> None:
        self.prompt("/craft-review")

        self.prepare()

        self.assertEqual(self.stop(), {})

    def test_a_prompt_without_the_review_skill_is_never_guarded(self) -> None:
        self.repo.write("cart.py", CHANGE)
        self.prompt("explain the cart module")
        self.assertEqual(self.stop(), {})

    def test_moving_on_to_another_request_ends_the_guard(self) -> None:
        self.begin_review()

        self.prompt("thanks, now rename the variable")

        self.assertEqual(self.stop(), {})

    def test_another_session_is_not_guarded(self) -> None:
        self.begin_review()

        self.assertEqual(self.stop(session_id="someone-else"), {})

    def test_copilot_sessions_are_guarded_in_the_copilot_dialect(self) -> None:
        call_hook("prompt", "copilot", vscode("UserPromptSubmit", self.root, prompt="/craft-review"))
        self.repo.write("cart.py", CHANGE)

        out, _ = call_hook("stop", "copilot", vscode("Stop", self.root, stop_hook_active=False))

        self.assertEqual(out["decision"], "block")
        self.assertEqual(out["hookSpecificOutput"]["decision"], "block")

    def test_a_craftsman_gate_keeps_its_own_behavior(self) -> None:
        self.init()
        self.repo.write("cart.py", CHANGE)

        reason = self.stop()["reason"]

        self.assertIn("[CRAFTSMAN GATE 1/", reason)

    def test_a_session_under_a_foreign_gate_is_still_guarded_and_told_the_gate_check(self) -> None:
        self.repo.cli("gate", "--init", "--cwd", ".", "--session-id", "gate-owner", env_extra={"CLAUDECODE": "1"})
        self.begin_review()

        reason = self.stop()["reason"]  # session claude-session is not the gate's owner

        self.assertIn("[CRAFTSMAN REVIEW 1/", reason)
        self.assertIn("Finish with `craftsman review check` passing.", reason)
        self.assertNotIn("--review-only", reason)

    def test_the_owner_of_a_gate_is_held_by_the_gate_not_the_review_guard(self) -> None:
        self.repo.cli("gate", "--init", "--cwd", ".", "--session-id", "claude-session", env_extra={"CLAUDECODE": "1"})
        self.begin_review()

        reason = self.stop()["reason"]

        self.assertIn("[CRAFTSMAN GATE 1/", reason)

    def test_the_block_fits_the_budget_and_never_cuts_a_sentence(self) -> None:
        self.begin_review()
        self.prepare()

        reason = self.stop()["reason"]

        self.assertLessEqual(len(reason), MAX_CONTINUATION_REASON_CHARS)
        self.assertTrue(reason.rstrip().endswith("passing."))


class StopTests(HookTestCase):
    def open_unsatisfied_gate(self) -> None:
        self.init()
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
        self.assertIn("review incomplete:", out["reason"])

    def test_block_reason_counts_the_continuation(self) -> None:
        self.open_unsatisfied_gate()
        out, _ = call_hook("stop", "copilot", vscode("Stop", self.root, stop_hook_active=False))
        self.assertTrue(out["reason"].startswith(header(1)))
        self.assertIn("review incomplete:", out["reason"])

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

    def test_cap_answers_with_a_system_message(self) -> None:
        self.open_unsatisfied_gate()
        self.update_state(forced_continuations=MAX_CONTINUATIONS, evidence_seen=0)
        capped, _ = call_hook("stop", "copilot", copilot_cli(self.root))
        self.assertEqual(set(capped), {"systemMessage"})
        self.assertIn("Craftsman gate not satisfied", capped["systemMessage"])

    def prepare_unisolated_review(self) -> None:
        """A standard change with a covered round and a test run, but no reviewer agent."""
        self.open_unsatisfied_gate()
        self.repo.cli("review", "prepare")
        self.repo.cover_all()
        self.repo.cli("run", "--kind", "test", "--", sys.executable, "-c", "assert True; print('ok')")

    def test_hooked_gate_blocks_a_review_without_a_reviewer(self) -> None:
        self.prepare_unisolated_review()
        blocked, _ = call_hook("stop", "claude", claude("Stop", self.root))
        self.assertIn("[no-isolation]", blocked["reason"])

    def test_reviewer_hook_events_prove_the_isolated_review(self) -> None:
        self.assert_reviewer_proves_isolation("craftsman-reviewer")

    def test_plugin_reviewer_hook_events_prove_the_isolated_review(self) -> None:
        # Claude Code namespaces a plugin's agents `<plugin>:<agent>`.
        self.assert_reviewer_proves_isolation("craftsman:craftsman-reviewer")

    def test_other_plugins_reviewer_does_not_prove_the_isolated_review(self) -> None:
        self.prepare_unisolated_review()
        reviewer = {"agent_id": "a1", "agent_type": "other:craftsman-reviewer"}
        call_hook("subagent-start", "claude", claude("SubagentStart", self.root, **reviewer))
        call_hook("subagent-stop", "claude", claude("SubagentStop", self.root, **reviewer))
        blocked, _ = call_hook("stop", "claude", claude("Stop", self.root))
        self.assertIn("[no-isolation]", blocked["reason"])

    def assert_reviewer_proves_isolation(self, agent_type: str) -> None:
        self.prepare_unisolated_review()
        self.run_reviewer(agent_type)
        self.assertEqual(call_hook("stop", "claude", claude("Stop", self.root))[0], {})
        self.assertEqual(self.state()["stage"], "completed")

    def start_hooked_review(self) -> dict[str, str]:
        """A hooked gate with a test run and a reviewer that started, before anything is covered."""
        self.open_unsatisfied_gate()
        self.repo.cli("review", "prepare")
        self.repo.cli("run", "--kind", "test", "--", sys.executable, "-c", "assert True; print('ok')")
        reviewer = {"agent_id": "a1", "agent_type": "craftsman-reviewer"}
        call_hook("subagent-start", "claude", claude("SubagentStart", self.root, **reviewer))
        return reviewer

    def test_the_orchestrators_cover_inside_a_reviewers_run_is_not_the_reviewers(self) -> None:
        reviewer = self.start_hooked_review()
        self.repo.cover_all()  # the orchestrator covers while the reviewer runs: C1
        self.assertEqual(self.stop_with_transcript(reviewer, *shell_run("t1", "ls", "cart.py")).get("decision"), "block")
        self.stop_with_transcript(reviewer, *shell_run("t1", "ls", "cart.py"), stop_hook_active=True)
        self.assertEqual(self.ledger()["agents"][-1]["records"], [])

        blocked, _ = call_hook("stop", "claude", claude("Stop", self.root))

        self.assertIn("[no-isolation]", blocked["reason"])

    def test_the_reviewers_own_cover_is_credited_by_its_transcript(self) -> None:
        reviewer = self.start_hooked_review()
        self.repo.cover_all()  # C1, the orchestrator's
        _, covered = self.cover_h1()  # C2, the reviewer's own
        self.stop_with_transcript(reviewer, *covered, stop_hook_active=True)
        self.assertEqual(self.ledger()["agents"][-1]["records"], ["C2"])

        self.assertEqual(call_hook("stop", "claude", claude("Stop", self.root))[0], {})

    def test_a_cover_only_the_orchestrator_made_fails_the_latest_round_naming_the_hunks(self) -> None:
        reviewer = self.start_hooked_review()
        self.repo.cover_all()  # C1, the orchestrator's
        _, finding = self.add_finding_lines()
        self.stop_with_transcript(reviewer, *finding, stop_hook_active=True)  # the reviewer only added a finding

        blocked, _ = call_hook("stop", "claude", claude("Stop", self.root))

        self.assertIn("Hunks H1 of round 1 were covered outside any finished craftsman-reviewer run", blocked["reason"])

    def add_finding_lines(self) -> tuple[str, list[str]]:
        command = "craftsman review add --lens contracts --file cart.py --line 2 --severity major"
        proc = self.repo.cli(
            "review", "add", "--lens", "contracts", "--file", "cart.py", "--line", "2",
            "--severity", "major", "--title", "Strings are summed without a check",
            "--scenario", "total(['a']) raises TypeError instead of a clear error",
        )  # fmt: skip
        return command, shell_run("add-1", command, proc.stdout)

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

    def test_trigger_during_a_session_raises_the_gate_to_deep(self) -> None:
        self.open_unsatisfied_gate()
        prompt = "/craftsman --deep go"
        call_hook("prompt", "claude", claude("UserPromptSubmit", self.root, prompt=prompt))
        self.assertTrue(self.state()["deep"])

    def test_trigger_during_a_session_never_lowers_a_deep_gate(self) -> None:
        self.init(deep=True)
        self.repo.write("cart.py", CHANGE)
        prompt = "/craftsman --quick"
        call_hook("prompt", "claude", claude("UserPromptSubmit", self.root, prompt=prompt))
        self.assertTrue(self.state()["deep"])

    def retry_after_the_round_cap(self) -> dict[str, Any]:
        self.open_unsatisfied_gate()
        self.repo.cli("review", "prepare")
        self.update_state(stage="needs_human")
        prompt = "/craftsman retry"
        out, _ = call_hook("prompt", "claude", claude("UserPromptSubmit", self.root, prompt=prompt))
        return out

    def test_trigger_after_the_round_cap_restarts_the_gate(self) -> None:
        self.retry_after_the_round_cap()
        self.assertEqual((self.state()["stage"], self.state()["task"]), ("implement", "retry"))

    def test_restart_after_a_needs_human_stop_keeps_a_deep_gate_deep(self) -> None:
        self.init(deep=True)
        self.repo.write("cart.py", CHANGE)
        self.repo.cli("review", "prepare")
        self.update_state(stage="needs_human")

        out, _ = call_hook(
            "prompt", "claude", claude("UserPromptSubmit", self.root, prompt="/craftsman retry")
        )

        self.assertEqual((self.state()["stage"], self.state()["deep"]), ("implement", True))
        self.assertIn("Craftsman session started (deep, base", out["hookSpecificOutput"]["additionalContext"])

    def test_review_prompt_never_opens_a_gate_even_with_ultracraft(self) -> None:
        self.repo.write("cart.py", CHANGE)
        for prompt in ("/craft-review ultracraft", "/code-review ultracraft the cart"):
            with self.subTest(prompt=prompt):
                out, _ = call_hook(
                    "prompt", "claude", claude("UserPromptSubmit", self.root, prompt=prompt)
                )
                self.assertEqual(out, {})
                self.assertFalse(self.state_file.exists())

    def test_review_only_needs_a_reviewer_once_the_prompt_hook_saw_the_session(self) -> None:
        call_hook(
            "prompt", "claude", claude("UserPromptSubmit", self.root, prompt="/craft-review the cart")
        )
        self.repo.write("cart.py", CHANGE)
        self.repo.cli("review", "prepare")
        self.repo.cover_all()
        session = {"CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "claude-session"}

        inline = self.repo.cli("review", "check", "--review-only", check=False, env_extra=session)
        elsewhere = self.repo.cli("review", "check", "--review-only", check=False)

        self.assertEqual(inline.returncode, 1, inline.stdout)
        self.assertIn("[no-isolation]", inline.stdout)
        self.assertEqual(elsewhere.returncode, 0, elsewhere.stdout)  # another session: advisory only

    def test_agent_events_of_an_earlier_round_do_not_make_a_terminal_check_need_a_reviewer(self) -> None:
        self.repo.write("cart.py", CHANGE)
        self.repo.cli("review", "prepare")
        self.repo.cover_all()
        self.run_reviewer()  # a hooked session reviewed this round
        self.repo.write("cart.py", CHANGE + "# edited from a terminal\n")
        self.repo.cli("review", "prepare")
        self.repo.cover_all()

        elsewhere = self.repo.cli("review", "check", "--review-only", check=False)

        self.assertEqual(elsewhere.returncode, 0, elsewhere.stdout)

    def test_restarted_gate_archives_the_previous_review(self) -> None:
        self.retry_after_the_round_cap()
        self.assertFalse((self.repo.ledger_dir / "ledger.json").exists())
        self.assertEqual(len(list(self.repo.ledger_dir.glob("ledger-*.json"))), 1)

    def test_awaiting_user_allows(self) -> None:
        self.open_unsatisfied_gate()
        self.repo.cli("gate", "--await-user", "--cwd", ".")
        self.assertEqual(call_hook("stop", "claude", claude("Stop", self.root))[0], {})

    def exceed_the_round_cap(self) -> None:
        self.init()
        for revision in range(MAX_ROUNDS + 1):
            self.repo.write("cart.py", CHANGE + f"# revision {revision}\n")
            self.repo.cli("review", "prepare")

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
        self.init()
        self.update_state(owner=None)
        out, _ = call_hook("stop", "claude", claude("Stop", self.root))
        self.assertIn("not a valid gate state", out["systemMessage"])


class SubagentTests(HookTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.init()
        self.repo.write("cart.py", CHANGE)

    def start_reviewer(self) -> dict[str, Any]:
        self.repo.cli("review", "prepare")
        payload = vscode("SubagentStart", self.root, agent_id="r-1", agent_type="craftsman-reviewer")
        return call_hook("subagent-start", "copilot", payload)[0]

    def test_reviewer_start_is_briefed_with_the_round_and_where_its_slot_comes_from(self) -> None:
        out = self.start_reviewer()
        context = out["additionalContext"]
        self.assertEqual(
            out["hookSpecificOutput"], {"hookEventName": "SubagentStart", "additionalContext": context}
        )
        self.assertEqual(
            context,
            "Craftsman review round 1. Your slot id is in your prompt: run `craftsman review brief <ID>` and "
            "follow it; cover only your slot (other slots belong to other hunters).",
        )

    def test_briefs_list_neither_uncovered_hunks_nor_slots_and_never_name_the_packet(self) -> None:
        reviewer = self.start_reviewer()["additionalContext"]

        self.assertLess(len(reviewer), 300)
        for text in ("packet", "H1", "W1-", "Hunks not covered", "craftsman review cover", "Scope:"):
            self.assertNotIn(text, reviewer)

    def test_reviewer_start_is_recorded(self) -> None:
        self.start_reviewer()
        events = self.ledger()["agents"]
        self.assertEqual(
            [(e["platform"], e["event"], e["agent"], e["agent_id"]) for e in events],
            [("copilot", "start", "craftsman-reviewer", "r-1")],
        )

    def test_verifier_is_pointed_at_its_brief_command_and_told_no_findings(self) -> None:
        self.repo.cli("review", "prepare")
        self.add_finding()
        out, _ = call_hook(
            "subagent-start",
            "copilot",
            copilot_cli(
                self.root, agentName="craftsman-verifier", agentDisplayName="Craftsman Verifier"
            ),
        )
        context = out["additionalContext"]
        self.assertEqual(
            context,
            "Craftsman review round 1. Run the `craftsman review brief --verifier ...` command from your prompt.",
        )
        for text in ("F1", "Strings are summed", "craftsman review verdict", "packet"):
            self.assertNotIn(text, context)

    def stop_recording_coverage(self, transcript: tuple[str, ...] | None = None, **extra: Any) -> dict[str, Any]:
        """A reviewer that covered its hunk stops; return the stop event the ledger got.

        With `transcript` lines, the agent's own transcript is those plus its cover call.
        """
        self.repo.cli("review", "prepare")
        reviewer = {"agent_id": "r-t", "agent_type": "craftsman-reviewer"}
        call_hook("subagent-start", "claude", claude("SubagentStart", self.root, **reviewer))
        _, covered = self.cover_h1()
        if transcript is not None:
            extra["agent_transcript_path"] = str(self.agent_transcript(*transcript, *covered))
        out, _ = call_hook(
            "subagent-stop", "claude", claude("SubagentStop", self.root, **reviewer, **extra)
        )
        self.assertEqual(out, {})
        event: dict[str, Any] = self.ledger()["agents"][-1]
        self.assertEqual(event["event"], "stop")
        return event

    def agent_transcript(self, *lines: str) -> Path:
        path = self.root / "agent-r-t.jsonl"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def test_stop_records_the_agents_token_usage_and_time_from_its_own_transcript(self) -> None:
        lines = (
            json.dumps({"type": "assistant", "timestamp": "2026-10-21T10:00:00.000Z", "message": {
                "id": "m1", "usage": {"input_tokens": 3, "output_tokens": 40,
                                      "cache_read_input_tokens": 500, "cache_creation_input_tokens": 60}}}),
            json.dumps({"type": "assistant", "timestamp": "2026-10-21T10:02:00.000Z", "message": {
                "id": "m2", "usage": {"input_tokens": 1, "output_tokens": 2}}}),
        )  # fmt: skip

        event = self.stop_recording_coverage(lines)

        self.assertEqual(
            event["usage"],
            {"input_tokens": 4, "output_tokens": 42, "cache_read_tokens": 500, "cache_write_tokens": 60, "duration_s": 120.0},
        )
        self.assertEqual(event["records"], ["C1"])

    def test_a_stop_is_recorded_only_with_the_facts_of_the_transcript_it_was_judged_on(self) -> None:
        self.repo.cli("review", "prepare")
        reviewer = {"agent_id": "r-t", "agent_type": "craftsman-reviewer"}
        call_hook("subagent-start", "claude", claude("SubagentStart", self.root, **reviewer))
        path = self.agent_transcript("{}")

        with mock.patch("craftsman.usage.read_transcript", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):  # the hook is killed while it reads
                call_hook(
                    "subagent-stop",
                    "claude",
                    claude("SubagentStop", self.root, agent_transcript_path=str(path), **reviewer),
                )

        events = self.ledger()["agents"]
        self.assertEqual([(e["event"], e["agent_id"]) for e in events], [("start", "r-t")])  # nothing half-recorded

    def test_the_sessions_own_transcript_path_is_not_read_as_the_agents(self) -> None:
        path = self.agent_transcript(
            json.dumps({"type": "assistant", "message": {"id": "m1", "usage": {"output_tokens": 9}}})
        )

        event = self.stop_recording_coverage(transcript_path=str(path))

        self.assertNotIn("usage", event)

    # One test per path: once a hook proved isolation, a later `prepare` of the same checkout is enforced
    # and would no longer take the untagged coverage `stop_recording_coverage` records.
    def test_a_missing_agent_transcript_never_fails_the_stop(self) -> None:
        event = self.stop_recording_coverage(agent_transcript_path=str(self.root / "missing.jsonl"))

        self.assertNotIn("usage", event)

    def test_an_agent_transcript_that_is_a_directory_never_fails_the_stop(self) -> None:
        event = self.stop_recording_coverage(agent_transcript_path=str(self.root))

        self.assertNotIn("usage", event)

    def test_a_telemetry_crash_never_fails_the_stop(self) -> None:
        with mock.patch("craftsman.usage.read_transcript", side_effect=RuntimeError("boom")):
            event = self.stop_recording_coverage(agent_transcript_path=str(self.root / "x.jsonl"))

        self.assertNotIn("usage", event)

    def test_other_agents_are_recorded_without_a_briefing(self) -> None:
        self.repo.cli("review", "prepare")
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
        self.repo.cli("review", "prepare")
        call_hook("subagent-start", "copilot", copilot_cli(self.root, agentName="craftsman-reviewer"))
        stop = copilot_cli(self.root, agentId="r-9", agentType="custom", agentName="craftsman-reviewer")

        first, _ = call_hook("subagent-stop", "copilot", stop)
        second, _ = call_hook("subagent-stop", "copilot", stop)

        self.assertEqual(first, {"decision": "block", "reason": REVIEWER_STOP_REASON})
        self.assertEqual(second, {})
        self.assertEqual([e["event"] for e in self.ledger()["agents"]], ["start", "nudge", "stop"])

    def test_reviewer_that_recorded_coverage_may_stop(self) -> None:
        self.repo.cli("review", "prepare")
        reviewer = {"agent_id": "r-2", "agent_type": "craftsman-reviewer"}
        call_hook("subagent-start", "copilot", vscode("SubagentStart", self.root, **reviewer))
        self.repo.cli(
            "review", "cover", "--hunks", "H1", "--note", coverage_note(["H1"])
        )

        out, _ = call_hook(
            "subagent-stop",
            "copilot",
            vscode("SubagentStop", self.root, stop_hook_active=False, **reviewer),
        )

        self.assertEqual(out, {})

    def stop_verifier_with_a_pending_verdict(self, *, stop_hook_active: bool) -> dict[str, Any]:
        self.repo.cli("review", "prepare")
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
        self.repo.cli("review", "prepare")
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
        self.repo.cli("review", "prepare")
        self.add_finding()
        self.add_finding()
        payload = claude("SubagentStart", self.root, **REVIEW_ONLY_VERIFIER)
        return call_hook("subagent-start", "claude", payload)[0]

    def test_review_only_requires_an_isolated_reviewer_once_hooks_recorded_agents(self) -> None:
        self.start_review_only_verifier()
        self.repo.cover_all()
        for finding in ("F1", "F2"):
            self.repo.cli(
                "review", "verdict", finding, "--as", "dismissed",
                "--evidence", "guarded by cart.py:2, which rejects the input first",
            )  # fmt: skip

        blocked = self.repo.cli("review", "check", "--review-only", check=False)
        self.assertEqual(blocked.returncode, 1, blocked.stdout)
        self.assertIn("[no-isolation]", blocked.stdout)

        self.run_reviewer(agent_id="r1")
        passed = self.repo.cli("review", "check", "--review-only", check=False)
        self.assertEqual(passed.returncode, 0, passed.stdout)

    def test_merge_does_not_count_as_a_verdict(self) -> None:
        self.start_review_only_verifier()
        self.repo.cli("review", "merge", "F1", "F2")
        payload = claude("SubagentStop", self.root, stop_hook_active=False, **REVIEW_ONLY_VERIFIER)
        blocked, _ = call_hook("subagent-stop", "claude", payload)
        self.assertIn("Findings still without a verdict: F1.", blocked["reason"])

    def test_other_agents_outside_a_session_are_not_recorded(self) -> None:
        self.update_state(active=False)
        self.repo.cli("review", "prepare")
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
        self.repo.cli("review", "prepare")
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
        self.repo.cli("review", "prepare")
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
        self.repo.cli("gate", "--init", "--cwd", ".")
        self.assertEqual(self.state()["isolation_evidence"], "none")
        payload = vscode(
            "PreToolUse", self.root, tool_name="run_in_terminal",
            tool_input={"command": "craftsman gate --complete"},
        )  # fmt: skip
        self.assertEqual(call_hook("pre-tool", "copilot", payload)[0], {})
        self.assertEqual(self.state()["isolation_evidence"], "hooks")

    def test_guard_failure_asks_the_user(self) -> None:
        payload = claude("PreToolUse", self.root, tool_name="Bash", tool_input={"command": "git status"})
        with mock.patch.object(command_policy, "find_worktree_mutation", side_effect=RuntimeError("boom")):
            out, _ = call_hook("pre-tool", "claude", payload)
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "ask")

    def test_guard_failure_is_denied_on_copilot(self) -> None:
        payload = vscode(
            "PreToolUse", self.root, tool_name="run_in_terminal", tool_input={"command": "ls"}
        )
        with mock.patch.object(command_policy, "find_worktree_mutation", side_effect=RuntimeError("boom")):
            out, _ = call_hook("pre-tool", "copilot", payload)
        self.assertEqual(out["permissionDecision"], "deny")

    def test_invalid_gate_state_asks_before_a_search(self) -> None:
        self.init()
        self.update_state(owner=None)
        payload = claude("PreToolUse", self.root, tool_name="Grep", tool_input={"pattern": "x"})
        out, _ = call_hook("pre-tool", "claude", payload)
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "ask")

    def test_non_scope_checked_tools_skip_load_scope_policy_subprocesses(self) -> None:
        self.repo.cli("gate", "--init", "--cwd", ".")
        with mock.patch("craftsman.scope.load_scope_policy") as mock_policy:
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
        self.update_state(forced_continuations=3, evidence_seen=0)
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
        self.repo.cli("gate", "--init", "--cwd", ".", env_extra={"CLAUDECODE": "1"})
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
            "gate", "--init", "--cwd", ".",
            env_extra={"ANTIGRAVITY_AGENT": "1"},
        )  # fmt: skip
        self.repo.write("cart.py", CHANGE)
        self.repo.cli("review", "prepare")

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

    def test_the_subagents_of_a_non_owner_session_are_recorded_all_the_same(self) -> None:
        self.open_antigravity_gate_from_the_terminal()
        sub_out, _ = call_hook(
            "subagent-start",
            "claude",
            claude("SubagentStart", self.root, agent_id="x", agent_type="Explore"),
        )
        self.assertEqual(sub_out, {})
        self.assertEqual([(e["event"], e["agent"]) for e in self.ledger()["agents"]], [("start", "Explore")])
        self.assertEqual(self.state()["isolation_evidence"], "none")  # recording is not claiming the gate

    def test_a_second_sessions_review_under_a_foreign_gate_is_recorded_and_can_finish(self) -> None:
        self.repo.cli("gate", "--init", "--cwd", ".", "--session-id", "sess-A", env_extra={"CLAUDECODE": "1"})
        self.repo.write("cart.py", CHANGE)
        call_hook(
            "prompt", "claude", claude("UserPromptSubmit", self.root, session_id="sess-B", prompt="/craft-review the cart")
        )
        self.pre_tool("craftsman review prepare", session_id="sess-B")
        self.repo.cli("review", "prepare")
        self.assertTrue(self.repo.show()["slots"])  # an enforced round: the foreign gate and the marker both enforce it

        for _ in range(10):
            slots = self.unfinished_slots()
            if not slots:
                break
            self.assertEqual(self.run_hunter(slots[0], session_id="sess-B"), {})

        slots = self.repo.show()["slots"]
        self.assertEqual({slot["state"] for slot in slots}, {"done"})
        self.assertEqual(self.state()["owner"]["session_id"], "sess-A")

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
        self.init(deep=True)
        self.repo.write("cart.py", CHANGE)
        self.repo.record_full_evidence(deep=True)
        self.run_reviewer()

        completed = self.repo.cli("gate", "--complete", "--cwd", ".", "--session-id", "claude-session")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual((self.state()["active"], self.state()["stage"]), (False, "completed"))
        report = self.repo.cli("review", "report").stdout
        self.assertNotIn("SELF-REVIEWED", report)


class ArmingTests(HookTestCase):
    """PreToolUse arms the review guard when the main session runs a `craftsman review` command."""

    def marker_file(self) -> Path:
        return self.repo.ledger_dir / "review_hooks.json"

    def role_pre_tool(self, command: str, agent_type: str = "craftsman-reviewer") -> dict[str, Any]:
        return self.pre_tool(command, agent_id="sub-1", agent_type=agent_type)

    def decision(self, out: dict[str, Any]) -> str | None:
        found = out.get("hookSpecificOutput", {}).get("permissionDecision", out.get("permissionDecision"))
        return str(found) if found is not None else None

    def test_the_main_sessions_prepare_arms_the_guard_on_claude_and_copilot(self) -> None:
        for platform, session in (("claude", "claude-session"), ("copilot", "vscode-session")):
            with self.subTest(platform=platform):
                self.marker_file().unlink(missing_ok=True)

                self.assertEqual(self.pre_tool("craftsman review prepare", platform), {})

                marker = self.marker()
                self.assertEqual((marker["platform"], marker["session_id"]), (platform, session))
                self.assertTrue(marker["guarding"])
                self.assertIsNotNone(marker["touched_at"])
                self.assertEqual(marker["forced"], 0)

    def test_every_spelling_of_the_command_arms(self) -> None:
        for command in (
            "python -m craftsman.cli review prepare",
            "cd . && craftsman review show",
            "python scripts/craftsman-cli.py review check --review-only",
            "uv run craftsman review prepare",
            "uvx craftsman review prepare",
        ):
            with self.subTest(command=command):
                self.marker_file().unlink(missing_ok=True)

                self.pre_tool(command)

                self.assertTrue(self.marker_file().is_file())

    def test_text_that_only_mentions_the_command_does_not_arm(self) -> None:
        for command in ("echo craftsman review prepare", 'git commit -m "craftsman review prepare"', "git status"):
            with self.subTest(command=command):
                self.pre_tool(command)

                self.assertFalse(self.marker_file().exists())

    def test_a_non_review_craftsman_command_does_not_arm(self) -> None:
        self.pre_tool("craftsman run --kind test -- pytest")

        self.assertFalse(self.marker_file().exists())

    def test_a_report_only_touches_a_guard_that_exists(self) -> None:
        self.pre_tool("craftsman review report")
        self.assertFalse(self.marker_file().exists())
        self.pre_tool("craftsman review prepare")
        old = {**self.marker(), "touched_at": "2020-01-01T00:00:00.000Z", "forced": 3}
        self.marker_file().write_text(json.dumps(old), encoding="utf-8")

        self.pre_tool("craftsman review report")

        marker = self.marker()
        self.assertGreater(marker["touched_at"], old["touched_at"])
        self.assertEqual((marker["forced"], marker["at"]), (3, old["at"]))

    def test_a_command_of_a_guarding_session_keeps_its_counters(self) -> None:
        self.pre_tool("craftsman review prepare")
        guarded = {**self.marker(), "forced": 2}
        self.marker_file().write_text(json.dumps(guarded), encoding="utf-8")

        self.pre_tool("craftsman review show")

        self.assertEqual((self.marker()["guarding"], self.marker()["forced"], self.marker()["at"]), (True, 2, guarded["at"]))

    def test_a_command_after_the_guard_ended_starts_a_new_review_without_the_old_request(self) -> None:
        call_hook("prompt", "claude", claude("UserPromptSubmit", self.root, prompt="/craft-review --deep the parser"))
        self.assertEqual((self.marker()["deep"], self.marker()["task"]), (True, "the parser"))
        call_hook("prompt", "claude", claude("UserPromptSubmit", self.root, prompt="thanks, now something else"))
        self.assertFalse(self.marker()["guarding"])
        ended = self.marker()
        self.repo.write("cart.py", CHANGE)

        self.prepare()

        marker = self.marker()
        self.assertTrue(marker["guarding"])
        self.assertEqual((marker["deep"], marker["task"]), (False, ""))
        self.assertGreater(marker["at"], ended["at"])
        latest = self.ledger()["rounds"][-1]
        self.assertEqual((latest["enforced"], latest["deep"], latest["task"]), (True, False, ""))

    def test_a_marker_the_guard_gave_up_on_keeps_its_abandoned_stamp_when_the_session_runs_a_command_again(self) -> None:
        self.pre_tool("craftsman review prepare")
        abandoned = {**self.marker(), "guarding": False, "abandoned_at": "2026-01-01T00:00:00.000Z"}
        self.marker_file().write_text(json.dumps(abandoned), encoding="utf-8")

        self.pre_tool("craftsman review check --review-only")

        self.assertEqual(self.marker()["abandoned_at"], "2026-01-01T00:00:00.000Z")

    def test_the_gate_owners_review_commands_do_not_arm_a_second_guard_after_the_gate_gives_up(self) -> None:
        call_hook("prompt", "claude", claude("UserPromptSubmit", self.root, prompt="ultracraft add the totals"))
        self.repo.write("cart.py", CHANGE)

        self.pre_tool("craftsman review prepare")
        self.assertFalse(self.marker_file().exists())
        stops = [call_hook("stop", "claude", claude("Stop", self.root))[0] for _ in range(MAX_CONTINUATIONS + 4)]

        self.assertEqual([("decision" in out) for out in stops], [True] * MAX_CONTINUATIONS + [False] * 4)
        self.assertFalse(self.state()["active"])
        self.assertEqual(stops[-1], {})

    def test_a_guard_the_gate_owner_armed_before_the_gate_ended_with_the_gate(self) -> None:
        call_hook("prompt", "claude", claude("UserPromptSubmit", self.root, prompt="/craft-review the cart"))
        self.repo.write("cart.py", CHANGE)
        self.init()
        self.update_state(owner={"platform": "claude", "session_id": "claude-session"})
        self.assertTrue(self.marker()["guarding"])

        stops = [call_hook("stop", "claude", claude("Stop", self.root))[0] for _ in range(MAX_CONTINUATIONS + 2)]

        self.assertEqual([("decision" in out) for out in stops], [True] * MAX_CONTINUATIONS + [False] * 2)
        self.assertEqual(set(stops[MAX_CONTINUATIONS]), {"systemMessage"})
        self.assertFalse(self.marker()["guarding"])

    def test_a_subagent_does_not_arm_and_may_not_restart_the_review(self) -> None:
        for command in ("craftsman review prepare", "craftsman review prepare --deep", "craftsman review reset"):
            with self.subTest(command=command):
                out = self.role_pre_tool(command)

                self.assertEqual(self.decision(out), "deny")
                self.assertFalse(self.marker_file().exists())

    def test_a_denied_restart_names_the_reason_and_hands_the_work_back(self) -> None:
        out = self.role_pre_tool("craftsman review reset", "craftsman-verifier")

        reason = out["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("must not re-plan or reset the review", reason)

    def test_a_subagent_may_run_the_other_review_commands_without_arming(self) -> None:
        for command in (
            "craftsman review brief W1-1",
            "craftsman review brief --verifier F1",
            'craftsman review cover --hunks H1 --note "x"',
            "craftsman review verdict F1 --as dismissed",
            "craftsman review add --lens contracts",
            "craftsman review show --json",
        ):
            with self.subTest(command=command):
                self.assertEqual(self.role_pre_tool(command), {})
                self.assertFalse(self.marker_file().exists())

    def test_a_subagent_of_the_copilot_cli_is_recognised_by_its_name(self) -> None:
        payload = copilot_cli(
            self.root, toolName="bash", toolArgs=json.dumps({"command": "craftsman review prepare"}),
            agentName="craftsman-reviewer",
        )  # fmt: skip

        out, _ = call_hook("pre-tool", "copilot", payload)

        self.assertEqual(self.decision(out), "deny")
        self.assertFalse(self.marker_file().exists())

    def test_an_agent_that_is_not_craftsmans_is_not_the_main_session_and_does_not_arm(self) -> None:
        self.assertEqual(self.pre_tool("craftsman review prepare", agent_id="x-1", agent_type="Explore"), {})
        self.assertFalse(self.marker_file().exists())

    def test_the_main_session_may_not_fetch_a_brief_and_the_denial_does_not_arm(self) -> None:
        for command in (
            "craftsman review brief W1-1",
            "craftsman review brief --verifier F1",
            "uv run craftsman review brief W1-1",
            "pipx run craftsman review brief W1-1",
        ):
            with self.subTest(command=command):
                self.assertEqual(self.decision(self.pre_tool(command)), "deny")
                self.assertFalse(self.marker_file().exists())

    def test_a_chain_with_a_brief_is_denied_as_a_whole(self) -> None:
        out = self.pre_tool("craftsman review prepare && craftsman review brief W1-1")

        self.assertEqual(self.decision(out), "deny")
        self.assertFalse(self.marker_file().exists())

    def test_a_worktree_violation_is_denied_and_does_not_arm(self) -> None:
        self.init()
        self.update_state(owner={"platform": "claude", "session_id": "claude-session"})

        out = self.pre_tool("craftsman review prepare && git -C /elsewhere reset --hard")

        if self.decision(out) == "deny":
            self.assertFalse(self.marker_file().exists())

    def test_a_failure_to_arm_never_blocks_the_command(self) -> None:
        payload = claude("PreToolUse", self.root, tool_name="Bash", tool_input={"command": "craftsman review prepare"})
        with mock.patch.object(review_guard, "arm_marker", side_effect=OSError("disk full")):
            out, err = call_hook("pre-tool", "claude", payload)

        self.assertEqual(out, {})
        self.assertIn("could not arm the review guard: disk full", err)

    def test_a_prompt_without_a_session_id_arms_nothing(self) -> None:
        payload = copilot_cli(self.root, toolName="bash", toolArgs=json.dumps({"command": "craftsman review prepare"}))
        payload.pop("sessionId")

        call_hook("pre-tool", "copilot", payload)

        self.assertFalse(self.marker_file().exists())

    def test_a_marker_of_another_session_is_kept_for_two_hours(self) -> None:
        self.pre_tool("craftsman review prepare")
        mine = self.marker()

        out = self.pre_tool("craftsman review prepare", "copilot")  # vscode-session
        self.assertEqual(out, {})
        self.assertEqual(self.marker(), mine)

        stale = {**mine, "at": "2020-01-01T00:00:00.000Z", "touched_at": "2020-01-01T00:00:00.000Z"}
        self.marker_file().write_text(json.dumps(stale), encoding="utf-8")
        self.pre_tool("craftsman review prepare", "copilot")

        self.assertEqual((self.marker()["platform"], self.marker()["session_id"]), ("copilot", "vscode-session"))

    def test_the_prompt_arms_a_fresh_guard_with_the_request_and_a_new_prompt_disarms_it(self) -> None:
        prompt = "/craftsman:craft-review --deep the cart totals"
        call_hook("prompt", "claude", claude("UserPromptSubmit", self.root, prompt=prompt))

        marker = self.marker()
        self.assertEqual((marker["deep"], marker["task"], marker["guarding"]), (True, "the cart totals", True))
        self.assertIsNone(marker["touched_at"])

        call_hook("prompt", "claude", claude("UserPromptSubmit", self.root, prompt="thanks, now something else"))
        self.assertFalse(self.marker()["guarding"])

    def test_a_prompt_does_not_take_over_a_live_review_of_another_session(self) -> None:
        self.pre_tool("craftsman review prepare")
        mine = self.marker()

        call_hook("prompt", "copilot", vscode("UserPromptSubmit", self.root, prompt="/craft-review"))

        self.assertEqual(self.marker(), mine)

    def test_the_marker_makes_the_next_prepare_an_enforced_round(self) -> None:
        self.repo.write("cart.py", CHANGE)
        self.prepare()

        self.assertTrue(self.ledger()["rounds"][-1]["enforced"])

    def test_a_prepare_without_the_hook_is_an_advisory_round(self) -> None:
        self.repo.write("cart.py", CHANGE)
        self.repo.cli("review", "prepare")

        self.assertFalse(self.ledger()["rounds"][-1]["enforced"])

    def test_an_arm_that_went_stale_makes_an_advisory_round(self) -> None:
        self.repo.write("cart.py", CHANGE)
        self.pre_tool("craftsman review show")
        old = {**self.marker(), "touched_at": "2020-01-01T00:00:00.000Z"}
        self.marker_file().write_text(json.dumps(old), encoding="utf-8")

        self.repo.cli("review", "prepare")

        self.assertFalse(self.ledger()["rounds"][-1]["enforced"])


class GateWaitingTests(HookTestCase):
    """A gate's Stop does not spend reminders while craftsman subagents of its review are at work."""

    def setUp(self) -> None:
        super().setUp()
        self.init()
        self.repo.write("cart.py", CHANGE)
        self.prepare()

    def stop(self) -> dict[str, Any]:
        return call_hook("stop", "claude", claude("Stop", self.root))[0]

    def start(self, agent_id: str, agent_type: str = "craftsman-reviewer") -> dict[str, str]:
        agent = {"agent_id": agent_id, "agent_type": agent_type}
        call_hook("subagent-start", "claude", claude("SubagentStart", self.root, **agent))
        return agent

    def age_starts(self, minutes: int) -> None:
        """Make every start event `minutes` old; the round is made older still, so the starts stay in it."""
        ledger = self.ledger()

        def earlier(stamp: str, by: int) -> str:
            moved = datetime.fromisoformat(stamp.replace("Z", "+00:00")) - timedelta(minutes=by)
            return moved.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

        for event in ledger["agents"]:
            event["at"] = earlier(event["at"], minutes)
        for round_ in ledger["rounds"]:
            round_["prepared_at"] = earlier(round_["prepared_at"], minutes + 60)
        (self.repo.ledger_dir / "ledger.json").write_text(json.dumps(ledger), encoding="utf-8")

    def test_seven_stops_while_a_hunter_runs_spend_no_reminder(self) -> None:
        self.start("bg-1")

        for _ in range(7):
            out = self.stop()
            self.assertEqual(set(out), {"systemMessage"})
            self.assertIn("waiting for craftsman-reviewer", out["systemMessage"])

        self.assertEqual(self.state()["forced_continuations"], 0)
        self.assertTrue(self.state()["active"])

    def test_the_waiting_notice_names_the_gate_check(self) -> None:
        self.start("bg-1")

        message = self.stop()["systemMessage"]

        self.assertIn("`craftsman review check` will say to relaunch them", message)
        self.assertNotIn("--review-only", message)

    def test_when_the_hunter_stops_the_next_stop_is_blocked_and_counted(self) -> None:
        agent = self.start("bg-1")
        self.stop()
        call_hook("subagent-stop", "claude", claude("SubagentStop", self.root, stop_hook_active=True, **agent))

        out = self.stop()

        self.assertEqual(out["decision"], "block")
        self.assertTrue(out["reason"].startswith(header(1)))

    def test_a_start_older_than_the_stale_limit_no_longer_exempts_the_stop(self) -> None:
        self.start("bg-1")
        self.age_starts(31)

        out = self.stop()

        self.assertEqual(out["decision"], "block")
        self.assertEqual(self.state()["forced_continuations"], 1)

    def test_a_start_just_inside_the_limit_still_exempts_the_stop(self) -> None:
        self.start("bg-1")
        self.age_starts(25)

        self.assertEqual(set(self.stop()), {"systemMessage"})

    def test_a_claim_without_an_open_run_does_not_exempt_the_stop(self) -> None:
        self.repo.cli("review", "brief", "W1-1")

        self.assertEqual(self.stop()["decision"], "block")

    def test_a_satisfied_gate_stops_even_with_a_hunter_running(self) -> None:
        self.hunt()
        self.repo.cli("run", "--kind", "test", "--", sys.executable, "-c", "assert True; print('ok')")
        self.start("late-1")

        self.assertEqual(self.stop(), {})

    def test_a_hunter_of_an_earlier_round_does_not_exempt_a_stop(self) -> None:
        self.start("old-1")
        self.repo.write("cart.py", CHANGE + "# changed\n")
        self.prepare()

        self.assertEqual(self.stop()["decision"], "block")


class DueWaveTests(HookTestCase):
    """Both Stop paths store the waves that are due before they judge, so the block lists the next wave's slots."""

    def setUp(self) -> None:
        super().setUp()
        self.repo.write("cart.py", CHANGE)

    def judged_trigger(self) -> None:
        """Every slot of generation 1 hunted, and one probable major finding: a second wave is due, not yet stored.

        The commands store the wave themselves; it is taken out of the plan again, as if no command had come by.
        """
        self.hunt()
        self.add_finding(hunter="lead")
        self.repo.cli("review", "verdict", "F1", "--as", "probable", "--evidence", "static: cart.py:2 sums strings -> total(['a']) raises TypeError")
        ledger = self.ledger()
        plan = ledger["rounds"][-1]["plan"]
        plan["slots"] = [slot for slot in plan["slots"] if not slot["id"].startswith("W2-")]
        plan["waves"] = []
        (self.repo.ledger_dir / "ledger.json").write_text(json.dumps(ledger), encoding="utf-8")

    def stored_slots(self) -> list[str]:
        return [slot["id"] for slot in self.ledger()["rounds"][-1]["plan"]["slots"]]

    def stop(self) -> dict[str, Any]:
        return call_hook("stop", "claude", claude("Stop", self.root))[0]

    def check_stop(self) -> None:
        self.judged_trigger()
        self.assertFalse([slot for slot in self.stored_slots() if slot.startswith("W2-")])
        ledger_file = self.repo.ledger_dir / "ledger.json"

        first = self.stop()

        self.assertEqual(first["decision"], "block")
        self.assertIn("W2-", first["reason"])
        self.assertTrue([slot for slot in self.stored_slots() if slot.startswith("W2-")])
        stored = ledger_file.read_bytes()
        self.stop()
        self.assertEqual(ledger_file.read_bytes(), stored)  # planned once: the second Stop does not rewrite the ledger

    def test_the_review_guard_stores_the_due_wave_before_it_blocks(self) -> None:
        self.prepare()

        self.check_stop()

    def test_the_gates_stop_stores_the_due_wave_before_it_blocks(self) -> None:
        self.init()
        self.prepare()

        self.check_stop()


class GateProgressTests(HookTestCase):
    """The gate's reminders start over when the review gained evidence, and only then."""

    def setUp(self) -> None:
        super().setUp()
        self.init()
        self.repo.write("cart.py", CHANGE)
        self.prepare()

    def stop(self) -> dict[str, Any]:
        return call_hook("stop", "claude", claude("Stop", self.root))[0]

    def test_new_evidence_between_stops_resets_the_count(self) -> None:
        for expected in (1, 2, 3):
            self.assertTrue(self.stop()["reason"].startswith(header(expected)))
        self.assertEqual(self.state()["forced_continuations"], 3)

        self.hunt_one()

        self.assertTrue(self.stop()["reason"].startswith(header(1)))

    def hunt_one(self) -> None:
        slots = self.unfinished_slots()
        self.assertEqual(self.run_hunter(slots[0]), {})

    def test_a_stop_without_new_evidence_keeps_counting(self) -> None:
        for expected in (1, 2, 3, 4):
            self.assertTrue(self.stop()["reason"].startswith(header(expected)))
        self.repo.cli("review", "show")  # looking is not evidence

        self.assertTrue(self.stop()["reason"].startswith(header(5)))

    def test_the_count_is_stored_with_the_evidence_it_was_judged_on(self) -> None:
        self.stop()

        self.assertIsNotNone(self.state()["evidence_seen"])


class SubagentGuardTests(HookTestCase):
    """SubagentStop in an enforced round: a hunter that left its slot unfinished is sent back once."""

    def setUp(self) -> None:
        super().setUp()
        self.repo.write("cart.py", CHANGE)
        self.prepare()

    def start_hunter(self, agent_id: str = "h-1") -> dict[str, str]:
        agent = {"agent_id": agent_id, "agent_type": "craftsman-reviewer"}
        call_hook("subagent-start", "claude", claude("SubagentStart", self.root, **agent))
        return agent

    def events(self) -> list[str]:
        return [str(e["event"]) for e in self.ledger()["agents"]]

    def test_a_hunter_that_only_fetched_its_brief_is_sent_back_once_with_the_command_to_run(self) -> None:
        hunter = self.start_hunter()
        slot = self.unfinished_slots()[0]
        brief = self.repo.cli("review", "brief", slot["id"])
        lines = shell_run("b1", f"craftsman review brief {slot['id']}", brief.stdout)

        first = self.stop_with_transcript(hunter, *lines)
        second = self.stop_with_transcript(hunter, *lines)

        self.assertEqual(first["decision"], "block")
        self.assertEqual(
            first["reason"],
            f"Your slot {slot['id']} is not finished: cover hunks {hunk_ranges(slot['hunks'])} with "
            f'`craftsman review cover --hunter {slot["id"]} --hunks {hunk_ranges(slot["hunks"])} --note "..."`, then reply.',
        )
        self.assertEqual(second, {})
        self.assertEqual(self.events(), ["start", "nudge", "stop"])

    def test_the_stop_of_a_nudged_hunter_records_the_transcript_facts_once(self) -> None:
        hunter = self.start_hunter()
        slot = self.unfinished_slots()[0]
        brief = self.repo.cli("review", "brief", slot["id"])
        lines = shell_run("b1", f"craftsman review brief {slot['id']}", brief.stdout)
        self.stop_with_transcript(hunter, *lines)
        self.stop_with_transcript(hunter, *lines)

        stops = [e for e in self.ledger()["agents"] if e["event"] == "stop"]
        nudges = [e for e in self.ledger()["agents"] if e["event"] == "nudge"]

        self.assertEqual([e["records"] for e in stops], [["B1"]])
        self.assertIsNone(nudges[0].get("records"))
        self.assertIsNone(nudges[0].get("usage"))

    def test_a_hunter_that_stopped_after_a_nudge_leaves_its_slot_dead_not_unlaunched(self) -> None:
        hunter = self.start_hunter()
        slot = self.unfinished_slots()[0]
        brief = self.repo.cli("review", "brief", slot["id"])
        lines = shell_run("b1", f"craftsman review brief {slot['id']}", brief.stdout)
        self.stop_with_transcript(hunter, *lines)
        self.stop_with_transcript(hunter, *lines)

        shown = {item["id"]: item["state"] for item in self.repo.show()["slots"]}

        self.assertEqual(shown[slot["id"]], "dead")

    def test_a_hunter_is_held_to_its_own_slot_not_to_a_slot_another_hunter_left_dead(self) -> None:
        dead = self.start_hunter("h-dead")
        first, second = self.unfinished_slots()[:2]
        brief = self.repo.cli("review", "brief", first["id"])
        lines = shell_run("b1", f"craftsman review brief {first['id']}", brief.stdout)
        self.assertEqual(self.stop_with_transcript(dead, *lines)["decision"], "block")
        self.assertEqual(self.stop_with_transcript(dead, *lines), {})  # sent back once, then let go: its slot is dead
        shown = {item["id"]: item["state"] for item in self.repo.show()["slots"]}
        self.assertEqual(shown[first["id"]], "dead")

        out = self.run_hunter(second)

        self.assertEqual(out, {})
        self.assertEqual(self.events(), ["start", "nudge", "stop", "start", "stop"])

    def test_an_honest_hunter_stops_on_its_first_subagent_stop(self) -> None:
        slot = self.unfinished_slots()[0]

        self.assertEqual(self.run_hunter(slot), {})
        self.assertEqual(self.events(), ["start", "stop"])

    def test_a_copilot_hunter_that_did_nothing_is_sent_back_once_and_makes_one_run(self) -> None:
        call_hook("subagent-start", "copilot", copilot_cli(self.root, agentName="craftsman-reviewer"))
        stop = copilot_cli(self.root, agentName="craftsman-reviewer")

        first, _ = call_hook("subagent-stop", "copilot", stop)
        second, _ = call_hook("subagent-stop", "copilot", stop)

        self.assertEqual(first["decision"], "block")
        self.assertEqual(second, {})
        self.assertEqual(self.events(), ["start", "nudge", "stop"])
        self.assertEqual(len(agent_runs(load(self.repo.ledger_dir / "ledger.json"))), 1)

    def test_two_parallel_copilot_hunters_are_each_sent_back_once(self) -> None:
        copilot = copilot_cli(self.root, agentName="craftsman-reviewer")
        for _ in range(2):
            call_hook("subagent-start", "copilot", copilot)

        decisions = [call_hook("subagent-stop", "copilot", copilot)[0].get("decision") for _ in range(4)]

        self.assertEqual(decisions, ["block", "block", None, None])
        self.assertEqual(self.events(), ["start", "start", "nudge", "nudge", "stop", "stop"])

    def test_a_copilot_hunter_that_stopped_honestly_does_not_use_up_the_nudge_of_the_next_one(self) -> None:
        slot = self.unfinished_slots()[0]
        self.assertEqual(self.run_hunter(slot, platform="copilot"), {})
        copilot = copilot_cli(self.root, agentName="craftsman-reviewer")
        call_hook("subagent-start", "copilot", copilot)

        decisions = [call_hook("subagent-stop", "copilot", copilot)[0].get("decision") for _ in range(2)]

        self.assertEqual(decisions, ["block", None])
        self.assertEqual(self.events(), ["start", "stop", "start", "nudge", "stop"])

    def stop_without_transcript(self, agent: dict[str, str]) -> dict[str, Any]:
        return call_hook("subagent-stop", "claude", claude("SubagentStop", self.root, **agent))[0]

    def test_a_hunter_without_a_readable_transcript_is_not_given_the_claim_of_another_hunter(self) -> None:
        first, second = self.start_hunter("h-1"), self.start_hunter("h-2")
        slot = self.unfinished_slots()[0]
        self.repo.cli("review", "brief", slot["id"])  # the claim of ONE hunter, made while both run
        self.assertEqual(self.stop_without_transcript(first)["decision"], "block")  # its slot is unfinished
        self.assertEqual(self.stop_without_transcript(first), {})

        out = self.stop_without_transcript(second)

        self.assertEqual(out, {"decision": "block", "reason": REVIEWER_STOP_REASON})

    def test_a_claim_made_at_the_very_millisecond_a_hunter_started_still_belongs_to_it(self) -> None:
        hunter = self.start_hunter()
        slot = self.unfinished_slots()[0]
        self.repo.cli("review", "brief", slot["id"])
        ledger = self.ledger()
        start = next(e for e in ledger["agents"] if e["event"] == "start")
        ledger["claims"][0]["at"] = start["at"]
        (self.repo.ledger_dir / "ledger.json").write_text(json.dumps(ledger), encoding="utf-8")

        out = self.stop_without_transcript(hunter)

        self.assertIn(f"Your slot {slot['id']} is not finished", out["reason"])

    def test_a_copilot_hunter_that_finished_its_slot_stops_at_once(self) -> None:
        slot = self.unfinished_slots()[0]

        self.assertEqual(self.run_hunter(slot, platform="copilot"), {})
        self.assertEqual(self.events(), ["start", "stop"])

    def test_the_stop_of_a_hunter_the_ledger_never_saw_start_is_dropped_with_a_note(self) -> None:
        ghost = {"agent_id": "ghost", "agent_type": "craftsman-reviewer"}
        path = self.tmp / "ghost.jsonl"
        path.write_text("\n".join(shell_run("c1", "craftsman review cover --hunks H1", "Brief issued (B1)")) + "\n")

        out, err = call_hook(
            "subagent-stop", "claude", claude("SubagentStop", self.root, agent_transcript_path=str(path), **ghost)
        )

        self.assertEqual(out, {})
        self.assertEqual(self.events(), [])
        self.assertIn("never saw start (ghost)", err)

    def test_a_hunter_of_a_reset_review_vouches_for_nothing(self) -> None:
        hunter = self.start_hunter("pre-reset")
        slot = self.unfinished_slots()[0]
        brief = self.repo.cli("review", "brief", slot["id"])
        self.repo.cli("review", "reset")
        self.repo.cli("review", "prepare")
        lines = shell_run("b1", f"craftsman review brief {slot['id']}", brief.stdout)

        out = self.stop_with_transcript(hunter, *lines, "{}")

        self.assertEqual(out, {})
        self.assertNotIn("stop", self.events())
        shown = {item["id"]: item["state"] for item in self.repo.show()["slots"]}
        self.assertNotIn("done", shown.values())

    def test_two_parallel_verifiers_the_one_that_judged_nothing_is_sent_back_once(self) -> None:
        self.hunt()
        self.add_finding(hunter="lead")
        self.add_finding(hunter="lead")
        judged = {"agent_id": "v-1", "agent_type": "craftsman-verifier"}
        idle = {"agent_id": "v-2", "agent_type": "craftsman-verifier"}
        for verifier in (judged, idle):
            call_hook("subagent-start", "claude", claude("SubagentStart", self.root, **verifier))
        brief = self.repo.cli("review", "brief", "--verifier", "F1")
        verdict = self.repo.cli(
            "review", "verdict", "F1", "--as", "dismissed",
            "--evidence", "cart.py:2 only receives numbers from the typed API layer",
        )  # fmt: skip
        lines = [
            *shell_run("vb", "craftsman review brief --verifier F1", brief.stdout),
            *shell_run("vv", "craftsman review verdict F1 --as dismissed --evidence x", verdict.stdout),
        ]

        self.assertEqual(self.stop_with_transcript(judged, *lines), {})
        sent_back = self.stop_with_transcript(idle, *shell_run("ib", "craftsman review brief --verifier F2", ""))
        again = self.stop_with_transcript(idle, *shell_run("ib", "craftsman review brief --verifier F2", ""))

        self.assertEqual(sent_back["decision"], "block")
        self.assertIn("Findings that still need your independent verdict: F1, F2", sent_back["reason"])
        self.assertEqual(again, {})

    def verifier_with_pending_finding(self) -> dict[str, str]:
        self.hunt()
        self.add_finding(hunter="lead")
        verifier = {"agent_id": "v-1", "agent_type": "craftsman-verifier"}
        call_hook("subagent-start", "claude", claude("SubagentStart", self.root, **verifier))
        return verifier

    def test_a_verifier_that_recorded_a_finding_instead_of_a_verdict_is_sent_back(self) -> None:
        verifier = self.verifier_with_pending_finding()
        brief = self.repo.cli("review", "brief", "--verifier", "F1")
        added = self.repo.cli(
            "review", "add", "--lens", "failure", "--file", "cart.py", "--line", "1", "--severity", "minor",
            "--title", "Empty cart is not handled", "--scenario", "total([]) returns 0 without a note", "--hunter", "lead",
        )  # fmt: skip
        lines = [
            *shell_run("vb", "craftsman review brief --verifier F1", brief.stdout),
            *shell_run("va", "craftsman review add --lens failure", added.stdout),
        ]

        first = self.stop_with_transcript(verifier, *lines)

        self.assertIn("F2 recorded", added.stdout)
        self.assertEqual(first["decision"], "block")
        self.assertIn("Findings still without a verdict: F1, F2", first["reason"])
        self.assertEqual(self.stop_with_transcript(verifier, *lines), {})

    def test_a_verifier_that_only_recorded_coverage_is_sent_back(self) -> None:
        verifier = self.verifier_with_pending_finding()
        brief = self.repo.cli("review", "brief", "--verifier", "F1")
        lines = [
            *shell_run("vb", "craftsman review brief --verifier F1", brief.stdout),
            *shell_run("vc", "craftsman review cover --hunter W1-1 --hunks H1", "Coverage recorded (C9) for W1-1: H1"),
        ]

        first = self.stop_with_transcript(verifier, *lines)

        self.assertEqual(first["decision"], "block")
        self.assertIn("Findings still without a verdict: F1", first["reason"])

    def test_a_verifier_stopping_when_nothing_waits_for_a_verdict_any_more_is_let_go(self) -> None:
        verifier = self.verifier_with_pending_finding()
        other = {"agent_id": "v-2", "agent_type": "craftsman-verifier"}
        call_hook("subagent-start", "claude", claude("SubagentStart", self.root, **other))
        judged = self.repo.cli(
            "review", "verdict", "F1", "--as", "probable",
            "--evidence", "static: cart.py:2 sums the items without a check -> total(['a']) raises TypeError",
        )  # fmt: skip
        self.assertEqual(
            self.stop_with_transcript(other, *shell_run("ov", "craftsman review verdict F1 --as probable", judged.stdout)), {}
        )
        brief = self.repo.cli("review", "brief", "--verifier", "F1")

        out = self.stop_with_transcript(verifier, *shell_run("vb", "craftsman review brief --verifier F1", brief.stdout))

        self.assertEqual(out, {})
        self.assertEqual(self.events()[-1], "stop")

    def test_a_verifier_with_a_brief_and_a_verdict_stops_and_the_gate_sees_no_unverified_finding(self) -> None:
        self.hunt()
        self.add_finding(hunter="lead")
        verifier = {"agent_id": "v-1", "agent_type": "craftsman-verifier"}
        call_hook("subagent-start", "claude", claude("SubagentStart", self.root, **verifier))
        brief = self.repo.cli("review", "brief", "--verifier", "F1")
        verdict = self.repo.cli(
            "review", "verdict", "F1", "--as", "dismissed",
            "--evidence", "cart.py:2 only receives numbers from the typed API layer",
        )  # fmt: skip
        lines = [
            *shell_run("vb", "craftsman review brief --verifier F1", brief.stdout),
            *shell_run("vv", "craftsman review verdict F1 --as dismissed --evidence x", verdict.stdout),
        ]

        out = self.stop_with_transcript(verifier, *lines)

        stop = [e for e in self.ledger()["agents"] if e["event"] == "stop"][-1]
        checked = self.repo.cli("review", "check", "--review-only", check=False)
        self.assertEqual(out, {})
        self.assertEqual(stop["records"], ["V1"])
        self.assertNotIn("unverified", checked.stdout)


class VerifierDebtTests(HookTestCase):
    """REVIEW-FH 1: a verifier is let go only after a real verdict on what the gate still waits for."""

    DISMISSAL = "cart.py:2 only receives numbers from the typed API layer"

    def setUp(self) -> None:
        super().setUp()
        self.repo.write("cart.py", CHANGE)
        self.prepare()

    def hunted(self, findings: int) -> None:
        self.hunt()
        for _ in range(findings):
            self.add_finding(hunter="lead")

    def start_verifier(self, agent_id: str) -> dict[str, str]:
        verifier = {"agent_id": agent_id, "agent_type": "craftsman-verifier"}
        call_hook("subagent-start", "claude", claude("SubagentStart", self.root, **verifier))
        return verifier

    def dismiss_as_verifier(self, agent_id: str, finding: str) -> dict[str, Any]:
        verifier = self.start_verifier(agent_id)
        brief = self.repo.cli("review", "brief", "--verifier", finding)
        verdict = self.repo.cli("review", "verdict", finding, "--as", "dismissed", "--evidence", self.DISMISSAL)
        return self.stop_with_transcript(
            verifier,
            *shell_run(f"{agent_id}-b", f"craftsman review brief --verifier {finding}", brief.stdout),
            *shell_run(f"{agent_id}-v", f"craftsman review verdict {finding} --as dismissed --evidence x", verdict.stdout),
        )

    def test_a_verifier_launched_for_a_second_opinion_that_judges_nothing_is_sent_back_once(self) -> None:
        self.hunted(1)
        self.assertEqual(self.dismiss_as_verifier("v-1", "F1"), {})
        self.assertIn("[second-opinion]", self.repo.cli("review", "check", "--review-only", check=False).stdout)
        second = self.start_verifier("v-2")
        brief = self.repo.cli("review", "brief", "--verifier", "F1")
        lines = shell_run("v2-b", "craftsman review brief --verifier F1", brief.stdout)

        first = self.stop_with_transcript(second, *lines)
        again = self.stop_with_transcript(second, *lines)

        self.assertEqual(first["decision"], "block")
        self.assertIn("F1", first["reason"])
        self.assertEqual(again, {})

    def test_a_second_opinion_verifier_that_dismisses_is_let_go_at_once(self) -> None:
        self.hunted(1)
        self.assertEqual(self.dismiss_as_verifier("v-1", "F1"), {})

        self.assertEqual(self.dismiss_as_verifier("v-2", "F1"), {})

        self.assertNotIn("[second-opinion]", self.repo.cli("review", "check", "--review-only", check=False).stdout)

    def test_a_verifier_launched_for_a_verdict_nobody_vouches_for_is_sent_back(self) -> None:
        self.hunted(1)
        self.repo.cli("review", "verdict", "F1", "--as", "dismissed", "--evidence", self.DISMISSAL)  # the orchestrator's own
        self.assertIn("[unverified]", self.repo.cli("review", "check", "--review-only", check=False).stdout)
        verifier = self.start_verifier("v-1")
        brief = self.repo.cli("review", "brief", "--verifier", "F1")

        out = self.stop_with_transcript(verifier, *shell_run("vb", "craftsman review brief --verifier F1", brief.stdout))

        self.assertEqual(out["decision"], "block")
        self.assertIn("Findings that still need your independent verdict: F1", out["reason"])

    def test_a_merge_is_no_verdict_for_a_verifier_with_a_transcript(self) -> None:
        self.hunted(2)
        verifier = self.start_verifier("v-1")
        merged = self.repo.cli("review", "merge", "F1", "F2")
        self.assertIn("V1", merged.stdout)  # the merge prints a verdict token, which the transcript shows
        lines = shell_run("vm", "craftsman review merge F1 F2", merged.stdout)

        first = self.stop_with_transcript(verifier, *lines)
        again = self.stop_with_transcript(verifier, *lines)

        self.assertEqual(first["decision"], "block")
        self.assertIn("F1", first["reason"])
        self.assertEqual(again, {})

    def test_chained_merges_are_no_verdict_for_a_verifier_without_a_transcript(self) -> None:
        self.hunted(3)
        copilot = copilot_cli(self.root, agentName="craftsman-verifier")
        call_hook("subagent-start", "copilot", copilot)
        self.repo.cli("review", "merge", "F2", "F3")
        self.repo.cli("review", "merge", "F1", "F2")  # re-points F3 at F1: F3 now holds two merge events
        self.assertEqual(len([e for e in self.ledger()["findings"][2]["verdict_log"] if e.get("merge")]), 2)

        first, _ = call_hook("subagent-stop", "copilot", copilot)
        again, _ = call_hook("subagent-stop", "copilot", copilot)

        self.assertEqual(first["decision"], "block")
        self.assertIn("F1", first["reason"])
        self.assertEqual(again, {})

    def test_a_verdict_beside_a_merge_lets_the_verifier_go(self) -> None:
        self.hunted(2)
        verifier = self.start_verifier("v-1")
        merged = self.repo.cli("review", "merge", "F1", "F2")
        verdict = self.repo.cli("review", "verdict", "F1", "--as", "dismissed", "--evidence", self.DISMISSAL)

        out = self.stop_with_transcript(
            verifier,
            *shell_run("vm", "craftsman review merge F1 F2", merged.stdout),
            *shell_run("vv", "craftsman review verdict F1 --as dismissed --evidence x", verdict.stdout),
        )

        self.assertEqual(out, {})


class IdleHunterTests(HookTestCase):
    """REVIEW-FH 3 and 4: Copilot hunters without an agent id, and a hunter that fetched two briefs."""

    def setUp(self) -> None:
        super().setUp()
        self.repo.write("cart.py", CHANGE)
        self.prepare(platform="copilot")
        self.copilot = copilot_cli(self.root, agentName="craftsman-reviewer")

    def decisions(self, count: int) -> list[Any]:
        return [call_hook("subagent-stop", "copilot", self.copilot)[0].get("decision") for _ in range(count)]

    def events(self) -> list[dict[str, Any]]:
        return list(self.ledger()["agents"])

    def test_two_idle_hunters_that_stop_before_either_retries_are_each_sent_back_once(self) -> None:
        for _ in range(2):
            call_hook("subagent-start", "copilot", self.copilot)

        self.assertEqual(self.decisions(4), ["block", "block", None, None])

        events = self.events()
        starts = [e["at"] for e in events if e["event"] == "start"]
        self.assertEqual([e["event"] for e in events], ["start", "start", "nudge", "nudge", "stop", "stop"])
        self.assertEqual([e["answered"] for e in events if e["event"] == "nudge"], starts)

    def test_hunters_that_start_in_the_same_millisecond_are_still_two_starts_and_each_sent_back_once(self) -> None:
        with mock.patch("craftsman.hooks._now", return_value=datetime.now(timezone.utc)):
            for _ in range(2):
                call_hook("subagent-start", "copilot", self.copilot)

        starts = [e["at"] for e in self.events() if e["event"] == "start"]
        self.assertEqual(len(set(starts)), 2)  # a nudge names its start by the time: the times must tell them apart
        self.assertEqual(self.decisions(4), ["block", "block", None, None])
        self.assertEqual(sorted(e["answered"] for e in self.events() if e["event"] == "nudge"), sorted(starts))

    def test_the_runs_a_stop_closes_are_the_runs_that_were_judged(self) -> None:
        for _ in range(2):
            call_hook("subagent-start", "copilot", self.copilot)
        self.decisions(4)

        runs = agent_runs(load(self.repo.ledger_dir / "ledger.json"))

        self.assertEqual(len(runs), 2)
        self.assertTrue(all(run.stopped is not None for run in runs))

    def test_an_idle_hunter_is_not_excused_by_the_slot_coverage_of_a_parallel_hunter(self) -> None:
        for _ in range(2):
            call_hook("subagent-start", "copilot", self.copilot)
        slot = self.unfinished_slots()[0]
        missing = slot["missing"] or slot["hunks"]
        self.repo.cli("review", "brief", slot["id"])
        self.repo.cli("review", "cover", "--hunter", slot["id"], "--hunks", ",".join(missing), "--note", coverage_note(missing))

        honest, idle = self.decisions(2)

        self.assertIsNone(honest)  # the hunter that holds the claim and covered its slot
        self.assertEqual(idle, "block")  # the other one recorded nothing: the slot coverage is not its own

    def test_an_advisory_round_keeps_excusing_a_hunter_by_any_record_since_its_start(self) -> None:
        self.repo.cli("review", "reset")
        (self.repo.ledger_dir / "review_hooks.json").unlink()  # no hook armed this session
        self.repo.cli("review", "prepare")  # no hook saw it: an advisory round
        self.assertFalse(self.ledger()["rounds"][-1]["enforced"])
        call_hook("subagent-start", "copilot", self.copilot)
        self.repo.cli("review", "cover", "--hunks", "H1", "--note", coverage_note(["H1"]))

        self.assertEqual(self.decisions(1), [None])

    def unfinished_slots(self) -> list[dict[str, Any]]:
        return [slot for slot in self.repo.show()["slots"] if slot["state"] != "done"]


class BoundSlotTests(HookTestCase):
    """REVIEW-FH 4: an exactly attributed hunter answers for one slot only."""

    def setUp(self) -> None:
        super().setUp()
        self.repo.write("cart.py", CHANGE)
        self.prepare()

    def test_a_hunter_that_fetched_two_briefs_and_finished_its_first_slot_may_stop(self) -> None:
        hunter = {"agent_id": "h-1", "agent_type": "craftsman-reviewer"}
        call_hook("subagent-start", "claude", claude("SubagentStart", self.root, **hunter))
        first, second = [slot["id"] for slot in self.repo.show()["slots"][:2]]
        brief_first = self.repo.cli("review", "brief", first)
        brief_second = self.repo.cli("review", "brief", second)
        hunks = next(slot for slot in self.repo.show()["slots"] if slot["id"] == first)["hunks"]
        cover = self.repo.cli("review", "cover", "--hunter", first, "--hunks", ",".join(hunks), "--note", coverage_note(hunks))

        out = self.stop_with_transcript(
            hunter,
            *shell_run("b1", f"craftsman review brief {first}", brief_first.stdout),
            *shell_run("b2", f"craftsman review brief {second}", brief_second.stdout),
            *shell_run("c1", f"craftsman review cover --hunter {first} --hunks x --note x", cover.stdout),
        )

        shown = {slot["id"]: slot["state"] for slot in self.repo.show()["slots"]}
        self.assertEqual(out, {})
        self.assertEqual(shown[second], "unlaunched")  # nobody works on it: launch it, it is not a dead slot
        checked = self.repo.cli("review", "check", "--review-only", check=False).stdout
        self.assertNotIn(f"{second} (dead", checked)


class LaunchStaggerTests(HookTestCase):
    """Hunters and verifiers started together on Claude Code: every start but the first is held back."""

    def setUp(self) -> None:
        super().setUp()
        self.init()
        self.repo.write("cart.py", CHANGE)
        self.repo.cli("review", "prepare")
        self.moment = datetime(2026, 11, 10, 9, 0, tzinfo=timezone.utc)

    def start(self, agent_id: str, after_s: float, agent: str = "craftsman-reviewer") -> dict[str, Any]:
        """A Claude Code subagent starts `after_s` seconds after `self.moment`; the hook's answer."""
        payload = claude("SubagentStart", self.root, agent_id=agent_id, agent_type=agent)
        with mock.patch("craftsman.hooks._now", return_value=self.moment + timedelta(seconds=after_s)):
            return call_hook("subagent-start", "claude", payload)[0]

    def test_the_first_start_of_a_burst_does_not_wait(self) -> None:
        self.start("r-1", 0)

        self.stagger_sleep.assert_not_called()

    def test_a_follower_waits_until_the_first_start_plus_the_stagger(self) -> None:
        self.start("r-1", 0)
        self.start("r-2", 1.5)
        self.start("r-3", 2)

        self.assertEqual(self.stagger_sleep.call_args_list, [mock.call(3.5), mock.call(3.0)])

    def test_a_follower_waits_after_its_start_is_recorded_and_every_lock_is_released(self) -> None:
        seen: list[tuple[int, bool, bool]] = []

        def wait(seconds: float) -> None:
            ledger_path = self.repo.ledger_dir / "ledger.json"
            starts = [event for event in load(ledger_path).agents if event.event == "start"]
            seen.append((len(starts), holds_lock(ledger_path), holds_lock(self.state_file)))

        self.stagger_sleep.side_effect = wait
        self.start("r-1", 0)
        self.start("r-2", 1)

        self.assertEqual(seen, [(2, False, False)])

    def test_the_held_back_answer_is_the_briefing_the_first_start_gets(self) -> None:
        first = self.start("r-1", 0)
        second = self.start("r-2", 1)

        self.stagger_sleep.assert_called_once()
        self.assertEqual(second, first)
        self.assertIn("Craftsman review round 1.", first["hookSpecificOutput"]["additionalContext"])

    def test_a_start_outside_the_window_and_one_of_another_role_or_agent_do_not_wait(self) -> None:
        self.start("r-1", 0)
        self.start("v-1", 1, "craftsman-verifier")
        self.start("e-1", 1, "Explore")
        self.start("r-2", 5)

        self.stagger_sleep.assert_not_called()

    def test_a_failure_in_the_stagger_neither_waits_nor_changes_the_answer(self) -> None:
        first = self.start("r-1", 0)
        with mock.patch("craftsman.launch_stagger.stagger_s", side_effect=RuntimeError("boom")):
            second = self.start("r-2", 1)
        self.stagger_sleep.side_effect = OSError("interrupted")
        third = self.start("r-3", 2)

        self.assertEqual(second, first)
        self.assertEqual(third, first)
        self.assertEqual(self.stagger_sleep.call_count, 1)  # only the third start's wait, which failed
        self.assertFalse(hook_errors.log_path(self.repo.ledger_dir).exists())
        self.assertEqual([e["agent_id"] for e in self.ledger()["agents"]], ["r-1", "r-2", "r-3"])

    def test_copilot_starts_are_not_held_back(self) -> None:
        cli = copilot_cli(self.root, agentName="craftsman-reviewer")
        vs_code = vscode("SubagentStart", self.root, agent_type="craftsman-reviewer")
        for _ in range(3):
            call_hook("subagent-start", "copilot", cli)
            call_hook("subagent-start", "copilot", vs_code)

        self.assertEqual(len(self.ledger()["agents"]), 6)
        self.stagger_sleep.assert_not_called()

    def test_a_start_recorded_without_a_gate_for_the_current_round_is_held_back_outside_the_ledger_lock(
        self,
    ) -> None:
        self.state_file.unlink()  # no gate state to lock: the start is recorded for the current round
        locks: list[bool] = []
        self.stagger_sleep.side_effect = lambda seconds: locks.append(
            holds_lock(self.repo.ledger_dir / "ledger.json")
        )
        self.start("r-1", 0)
        self.start("r-2", 1)

        self.stagger_sleep.assert_called_once_with(4.0)
        self.assertEqual(locks, [False])

    def test_a_start_that_took_the_lock_second_but_read_the_clock_first_is_held_back(self) -> None:
        self.start("r-1", 1)
        self.start("r-2", 0.999)  # its hook read the clock first and then queued for the ledger lock

        self.stagger_sleep.assert_called_once_with(5.0)

    def test_a_resumed_agent_is_not_held_back(self) -> None:
        self.start("r-1", 0)
        self.start("r-1", 1)

        self.stagger_sleep.assert_not_called()

    def test_a_hook_run_that_fails_after_holding_back_neither_waits_nor_makes_a_later_run_wait(self) -> None:
        self.start("r-1", 0)
        with mock.patch("craftsman.hooks.briefing", side_effect=RuntimeError("boom")):
            failed = self.start("r-2", 1)
        self.start("e-1", 1.5, "Explore")
        call_hook("stop", "claude", claude("Stop", self.root))

        self.assertIn("boom", json.dumps(failed))
        self.stagger_sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
