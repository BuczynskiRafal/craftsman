"""Tests for the hook security and visibility items of Phase FS, through the real hook entry point.

Every case runs with Claude Code and Copilot payloads where the platform matters: the command policy
(`craftsman.command_policy`) behind PreToolUse, the provenance of a subagent's transcript, the hook error log,
the git budget of a hook, and `gate --await-user`.
"""

from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from craftsman import diffscan, hook_errors, hooks
from craftsman.command_policy import (
    HOOKS_ARE_THE_PLATFORMS,
    IDENTITY_IS_THE_PLATFORMS,
    LEAD_ONLY,
    LEDGER_IS_WRITTEN_BY_THE_CLI,
    OWNER_CANNOT_TAKE_OVER,
    TRANSCRIPTS_ARE_THE_PLATFORMS,
)
from craftsman.gate import TAKEOVER_NOTE, GateOwner, GateState
from craftsman.platforms.claude import CLAUDE_HOOK_MATCHERS

from tests import test_hooks as th
from tests.support import REPO_ROOT

REVIEWER = {"agent_id": "h1", "agent_type": "craftsman-reviewer"}


def reason(out: dict[str, Any]) -> str:
    """The reason of a PreToolUse answer that denies or asks (either dialect)."""
    nested = out.get("hookSpecificOutput", {})
    return str(nested.get("permissionDecisionReason") or out.get("permissionDecisionReason") or "")


def decision(out: dict[str, Any]) -> str:
    nested = out.get("hookSpecificOutput", {})
    return str(nested.get("permissionDecision") or out.get("permissionDecision") or "allow")


class PreToolCase(th.HookTestCase):
    def shell(self, command: str, **extra: Any) -> list[tuple[str, dict[str, Any]]]:
        """`command` as each platform's shell tool sends it (the agent fields of `extra` go to every payload)."""
        return [
            ("copilot", th.vscode("PreToolUse", self.root, tool_name="run_in_terminal", tool_input={"command": command}, **extra)),
            ("copilot", th.copilot_cli(self.root, toolName="bash", toolArgs=json.dumps({"command": command}), **extra)),
            ("claude", th.claude("PreToolUse", self.root, tool_name="Bash", tool_input={"command": command}, **extra)),
        ]  # fmt: skip

    def writing(self, path: str | Path) -> list[tuple[str, dict[str, Any]]]:
        """A file-writing tool call into `path` in each platform's dialect."""
        target = str(path)
        return [
            ("claude", th.claude("PreToolUse", self.root, tool_name="Write", tool_input={"file_path": target, "content": "x"})),
            ("claude", th.claude("PreToolUse", self.root, tool_name="Edit", tool_input={"file_path": target, "old_string": "a", "new_string": "b"})),
            ("claude", th.claude("PreToolUse", self.root, tool_name="MultiEdit", tool_input={"file_path": target, "edits": []})),
            ("claude", th.claude("PreToolUse", self.root, tool_name="NotebookEdit", tool_input={"notebook_path": target, "new_source": "x"})),
            ("copilot", th.vscode("PreToolUse", self.root, tool_name="replace_string_in_file", tool_input={"filePath": target, "oldString": "a", "newString": "b"})),
            ("copilot", th.vscode("PreToolUse", self.root, tool_name="editFiles", tool_input={"files": [target]})),
            ("copilot", th.copilot_cli(self.root, toolName="edit", toolArgs=json.dumps({"path": target, "old_str": "a", "new_str": "b"}))),
            ("copilot", th.copilot_cli(self.root, toolName="create", toolArgs={"path": target, "file_text": "x"})),
        ]  # fmt: skip

    def assertDeniedEverywhere(self, payloads: list[tuple[str, dict[str, Any]]], message: str) -> None:
        for platform, payload in payloads:
            with self.subTest(platform=platform, tool=payload.get("tool_name") or payload.get("toolName")):
                out, _ = th.call_hook("pre-tool", platform, payload)
                self.assertEqual((decision(out), reason(out)), ("deny", message))

    def assertAllowedEverywhere(self, payloads: list[tuple[str, dict[str, Any]]]) -> None:
        for platform, payload in payloads:
            with self.subTest(platform=platform, tool=payload.get("tool_name") or payload.get("toolName")):
                self.assertEqual(th.call_hook("pre-tool", platform, payload)[0], {})


class HookForgeryTests(PreToolCase):
    def test_hook_commands_are_denied_from_every_platform(self) -> None:
        for command in (
            "craftsman hook stop --platform claude",
            "python -m craftsman.cli hook pre-tool --platform copilot",
            "env X=1 craftsman.exe hook prompt --platform claude",
            "python3 -m craftsman.hooks stop --platform claude",
            "python -c 'import craftsman.hooks'",
        ):
            with self.subTest(command=command):
                self.assertDeniedEverywhere(self.shell(command), HOOKS_ARE_THE_PLATFORMS)

    def test_a_subagent_cannot_forge_a_hook_event_either(self) -> None:
        command = "craftsman hook subagent-stop --platform claude"

        self.assertDeniedEverywhere(self.shell(command, **REVIEWER), HOOKS_ARE_THE_PLATFORMS)
        self.assertDeniedEverywhere(
            self.shell(command, agent_id="v1", agent_type="craftsman-verifier"), HOOKS_ARE_THE_PLATFORMS
        )

    def test_quoted_and_echoed_text_is_not_a_hook_command(self) -> None:
        for command in ("echo craftsman hook stop", 'grep -n "craftsman hook" README.md', "git status"):
            with self.subTest(command=command):
                self.assertAllowedEverywhere(self.shell(command))

    def test_the_denial_leaves_no_trace_in_the_ledger_and_the_state(self) -> None:
        self.init()
        before = self.state_file.read_bytes()

        self.assertDeniedEverywhere(self.shell("craftsman hook stop --platform claude"), HOOKS_ARE_THE_PLATFORMS)

        self.assertEqual(self.state_file.read_bytes(), before)


class LedgerProtectionTests(PreToolCase):
    def test_the_claude_matcher_names_the_file_writing_tools(self) -> None:
        matcher = CLAUDE_HOOK_MATCHERS["PreToolUse"]

        for tool in ("Bash", "Read", "Grep", "Glob", "LS", "Write", "Edit", "MultiEdit", "NotebookEdit"):
            self.assertIn(tool, matcher.split("|"))
        plugin = json.loads((REPO_ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
        self.assertEqual(plugin["hooks"]["PreToolUse"][0]["matcher"], matcher)

    def test_file_writing_tools_cannot_edit_the_ledger_in_a_review_only_session(self) -> None:
        self.repo.cli("review", "prepare")  # a ledger, and no gate at all
        self.assertFalse(self.state_file.exists())

        self.assertDeniedEverywhere(self.writing(self.repo.ledger_dir / "ledger.json"), LEDGER_IS_WRITTEN_BY_THE_CLI)

    def test_the_same_holds_inside_a_gate_for_every_file_of_the_directory(self) -> None:
        self.init()

        for name in ("ledger.json", "craftsman_state.json", "review_hooks.json", "hook-errors.log"):
            with self.subTest(name=name):
                self.assertDeniedEverywhere(self.writing(self.repo.ledger_dir / name), LEDGER_IS_WRITTEN_BY_THE_CLI)

    def test_dot_dot_and_a_symlink_do_not_get_around_it(self) -> None:
        link = self.repo.root / "notes"
        link.symlink_to(self.repo.ledger_dir)
        for path in (self.root / "src" / ".." / ".git" / "craftsman" / "ledger.json", link / "ledger.json"):
            with self.subTest(path=path):
                self.assertDeniedEverywhere(self.writing(path), LEDGER_IS_WRITTEN_BY_THE_CLI)

    def test_a_linked_work_trees_ledger_is_protected_in_the_same_way(self) -> None:
        linked = self.tmp / "linked"
        subprocess.run(["git", "worktree", "add", "-q", str(linked), "-b", "side"], cwd=self.root, check=True)
        git_dir = Path(subprocess.run(["git", "rev-parse", "--absolute-git-dir"], cwd=linked, capture_output=True, text=True, check=True).stdout.strip())
        payload = th.claude("PreToolUse", linked, tool_name="Write", tool_input={"file_path": str(git_dir / "craftsman" / "ledger.json")})

        out, _ = th.call_hook("pre-tool", "claude", payload)

        self.assertEqual((decision(out), reason(out)), ("deny", LEDGER_IS_WRITTEN_BY_THE_CLI))

    def test_the_transcripts_of_the_platforms_cannot_be_edited_either(self) -> None:
        self.assertDeniedEverywhere(
            self.writing(self.transcripts / "agent-h1.jsonl"), TRANSCRIPTS_ARE_THE_PLATFORMS
        )

    def test_shell_commands_that_write_the_ledger_are_denied_and_reads_are_not(self) -> None:
        ledger = self.repo.ledger_dir / "ledger.json"
        self.repo.cli("review", "prepare")
        for command in (
            f"echo '{{}}' > {ledger}",
            f"sed -i 's/1/2/' {ledger}",
            f"cp /tmp/forged.json {ledger}",
            f"tee {ledger} < /tmp/forged.json",
            "cd .git/craftsman && rm ledger.json",
        ):
            with self.subTest(command=command):
                self.assertDeniedEverywhere(self.shell(command), LEDGER_IS_WRITTEN_BY_THE_CLI)
        for command in (f"cat {ledger}", f"cp {ledger} /tmp/ledger-copy.json", f"python3 -c \"import json; print(len(json.load(open('{ledger}'))['rounds']))\""):
            with self.subTest(command=command):
                self.assertAllowedEverywhere(self.shell(command))

    def test_a_redirection_after_a_heredoc_operator_is_a_ledger_write_on_every_platform(self) -> None:
        ledger = self.repo.ledger_dir / "ledger.json"
        self.repo.cli("review", "prepare")
        for command in (f"cat <<EOF > {ledger}\n{{}}\nEOF", f"cat <<'EOF' >>{ledger}\n{{}}\nEOF", f"cat <<-EOF >{ledger}\n{{}}\nEOF"):
            with self.subTest(command=command):
                self.assertDeniedEverywhere(self.shell(command), LEDGER_IS_WRITTEN_BY_THE_CLI)
        self.assertDeniedEverywhere(self.shell(f"echo '{{}}' >| {ledger}"), LEDGER_IS_WRITTEN_BY_THE_CLI)

    def test_the_agents_files_below_a_transcript_root_stay_writable(self) -> None:
        # Claude Code's auto-memory lives at <root>/<project>/memory/MEMORY.md; Copilot keeps plan.md next to its log.
        memory = self.transcripts / "-Users-x-Git-y" / "memory" / "MEMORY.md"
        plan = self.transcripts / "session-1" / "plan.md"

        for path in (memory, plan):
            self.assertAllowedEverywhere(self.writing(path))
            self.assertAllowedEverywhere(self.shell(f"echo note >> {path}"))
        self.assertDeniedEverywhere(
            self.writing(self.transcripts / "session-1" / "subagents" / "agent-x.jsonl"), TRANSCRIPTS_ARE_THE_PLATFORMS
        )

    def test_python_options_do_not_hide_a_hook_run(self) -> None:
        for command in ("python -W ignore -m craftsman.hooks pre-tool", "python3 -X utf8 -m craftsman hook stop", "python -um craftsman.cli hook stop"):
            with self.subTest(command=command):
                self.assertDeniedEverywhere(self.shell(command), HOOKS_ARE_THE_PLATFORMS)

    def test_a_batch_file_in_the_inbox_may_be_written(self) -> None:
        inbox = self.repo.cli("review", "inbox", "cover").stdout.strip()

        self.assertAllowedEverywhere(self.writing(inbox))
        self.assertAllowedEverywhere(self.shell(f"echo '[]' > {inbox}"))

    def test_other_writes_never_ask_git_for_the_ledger_directory(self) -> None:
        with mock.patch.object(hooks, "ledger_path_for", side_effect=AssertionError("git was asked for the ledger")):
            self.assertAllowedEverywhere(self.writing(self.root / "src" / "a.py"))
            self.assertAllowedEverywhere(self.writing("b.py"))

    def test_the_protection_does_not_wait_for_the_scope_filter(self) -> None:
        self.init()
        with mock.patch("craftsman.hooks.load_scope_policy") as policy:
            self.assertDeniedEverywhere(self.writing(self.repo.ledger_dir / "ledger.json"), LEDGER_IS_WRITTEN_BY_THE_CLI)
        policy.assert_not_called()


class IdentityTests(PreToolCase):
    def test_identity_variables_cannot_be_set_for_a_craftsman_command(self) -> None:
        for command in (
            "CLAUDE_CODE_SESSION_ID=other craftsman review check",
            "env -u CLAUDE_CODE_SESSION_ID craftsman review check",
            "export ANTIGRAVITY_CONVERSATION_ID=x; craftsman review check",
            "unset CLAUDE_CODE_SESSION_ID && craftsman gate --complete",
            "CRAFTSMAN_SESSION_ID=x craftsman status",
        ):
            with self.subTest(command=command):
                self.assertDeniedEverywhere(self.shell(command), IDENTITY_IS_THE_PLATFORMS)

    def test_ordinary_variables_pass(self) -> None:
        self.assertAllowedEverywhere(self.shell("FOO=1 craftsman status"))


class LeadOnlyTests(PreToolCase):
    def test_the_main_session_records_only_as_lead(self) -> None:
        for command in (
            "craftsman review cover --hunter W1-1 --hunks H1 --note x",
            "craftsman review add --hunter=W3-2 --lens intent --file a.py --line 1",
        ):
            with self.subTest(command=command):
                self.assertDeniedEverywhere(self.shell(command), LEAD_ONLY)
        self.assertAllowedEverywhere(self.shell("craftsman review cover --hunter lead --hunks H1 --note x"))
        self.assertAllowedEverywhere(self.shell("craftsman review add --lens intent --file a.py --line 1"))

    def test_a_batch_with_a_slot_entry_is_denied(self) -> None:
        batch = self.tmp / "cover.json"
        batch.write_text(json.dumps([{"hunks": "H1", "note": "x", "hunter": "W1-1"}]), encoding="utf-8")

        self.assertDeniedEverywhere(self.shell(f"craftsman review cover --from-json {batch}"), LEAD_ONLY)

    def test_a_hunter_may_tag_its_own_records(self) -> None:
        self.assertAllowedEverywhere(self.shell("craftsman review cover --hunter W1-1 --hunks H1 --note x", **REVIEWER))
        self.assertAllowedEverywhere(self.shell("craftsman review add --hunter W1-1 --lens intent", **REVIEWER))

    def test_a_session_started_as_the_orchestrator_is_still_the_main_session(self) -> None:
        self.assertDeniedEverywhere(self.shell("craftsman review cover --hunter W1-1 --hunks H1", agent_type="craftsman"), LEAD_ONLY)

    def test_the_denied_command_does_not_arm_the_review_guard(self) -> None:
        self.assertDeniedEverywhere(self.shell("craftsman review cover --hunter W1-1 --hunks H1"), LEAD_ONLY)

        self.assertFalse((self.repo.ledger_dir / "review_hooks.json").exists())


class TakeoverTests(PreToolCase):
    def claude_owner(self) -> None:
        """A gate owned by the Claude session `claude-session` (the one `th.claude` payloads come from)."""
        self.repo.cli("gate", "--init", "--cwd", ".", "--session-id", "claude-session")

    def test_the_owner_cannot_take_over_its_own_gate(self) -> None:
        self.claude_owner()

        out, _ = th.call_hook("pre-tool", "claude", th.claude("PreToolUse", self.root, tool_name="Bash", tool_input={"command": "craftsman gate --init --takeover --session-id fake"}))
        self.assertEqual(decision(out), "deny")
        out, _ = th.call_hook("pre-tool", "claude", th.claude("PreToolUse", self.root, tool_name="Bash", tool_input={"command": "craftsman gate --init --takeover"}))

        self.assertEqual((decision(out), reason(out)), ("deny", OWNER_CANNOT_TAKE_OVER))

    def test_the_owner_cannot_take_over_under_a_made_up_session_id(self) -> None:
        self.claude_owner()

        self.assertDeniedEverywhere(
            [("claude", th.claude("PreToolUse", self.root, tool_name="Bash", tool_input={"command": "craftsman gate --init --takeover --session-id made-up"}))],
            OWNER_CANNOT_TAKE_OVER,
        )

    def test_the_stage_zero_command_with_the_sessions_own_id_passes(self) -> None:
        state = GateState.open(GateOwner("claude", "claude-session"), "a" * 40, True, "build the cart")
        command = hooks.init_command(state)
        self.assertIn("--session-id claude-session", command)

        self.assertAllowedEverywhere([("claude", th.claude("PreToolUse", self.root, tool_name="Bash", tool_input={"command": command}))])

    def test_the_prompt_hooks_stage_zero_command_passes_end_to_end(self) -> None:
        started, _ = th.call_hook("prompt", "claude", th.claude("UserPromptSubmit", self.root, prompt="/craftsman build the cart"))
        command = started["hookSpecificOutput"]["additionalContext"].split("`")[1]
        self.assertTrue(command.startswith("craftsman gate --init --session-id claude-session"))

        self.assertAllowedEverywhere([("claude", th.claude("PreToolUse", self.root, tool_name="Bash", tool_input={"command": command}))])

    def test_a_foreign_session_id_in_a_gate_command_is_denied_on_every_platform(self) -> None:
        for platform, payload in self.shell("craftsman gate --init --session-id someone-else"):
            with self.subTest(platform=platform):
                out, _ = th.call_hook("pre-tool", platform, payload)
                self.assertEqual((decision(out), reason(out)), ("deny", IDENTITY_IS_THE_PLATFORMS))

    def test_a_new_session_can_take_over_a_gate_whose_owner_is_gone_and_the_gate_stays_hooked(self) -> None:
        self.claude_owner()
        self.repo.write("cart.py", th.CHANGE)  # a clean tree would let the Stop close the gate
        th.call_hook("stop", "claude", th.claude("Stop", self.root))  # the owner's hooks prove the session
        self.assertEqual((self.state()["active"], self.state()["isolation_evidence"]), (True, "hooks"))
        payload = th.claude("PreToolUse", self.root, session_id="claude-new", tool_name="Bash", tool_input={"command": "craftsman gate --init --takeover --session-id claude-new"})

        out, _ = th.call_hook("pre-tool", "claude", payload)
        self.repo.cli("gate", "--init", "--takeover", "--session-id", "claude-new")

        self.assertEqual(decision(out), "allow")
        state = self.state()
        self.assertEqual((state["active"], state["owner"]["session_id"]), (True, "claude-new"))
        self.assertEqual(state["isolation_evidence"], "hooks")  # taking a gate over never weakens it

    def test_agys_reproduction_the_takeover_keeps_the_round_enforced(self) -> None:
        self.claude_owner()
        self.repo.write("cart.py", th.CHANGE)  # a clean tree would let the Stop close the gate
        th.call_hook("stop", "claude", th.claude("Stop", self.root))  # proves the hooks
        self.repo.cli("review", "prepare")
        self.assertTrue(self.ledger()["rounds"][-1]["enforced"])

        self.repo.cli("gate", "--init", "--takeover", "--session-id", "fake-owner")
        self.repo.cli("review", "prepare")

        self.assertEqual(self.state()["isolation_evidence"], "hooks")
        self.assertTrue(self.ledger()["rounds"][-1]["enforced"])


class ProvenanceTests(th.HookTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.init()
        self.repo.write("cart.py", th.CHANGE)

    def start_reviewer(self) -> dict[str, str]:
        self.repo.cli("review", "prepare")
        call_hook = th.call_hook
        call_hook("subagent-start", "claude", th.claude("SubagentStart", self.root, **REVIEWER))
        return dict(REVIEWER)

    def stop_event(self) -> dict[str, Any]:
        event: dict[str, Any] = self.ledger()["agents"][-1]
        return event

    def covering_transcript(self, directory: Path) -> Path:
        _, covered = self.cover_h1()
        path = directory / "agent-h1.jsonl"
        path.write_text("\n".join(covered) + "\n", encoding="utf-8")
        return path

    def test_a_transcript_under_the_transcript_root_is_read(self) -> None:
        self.start_reviewer()
        path = self.covering_transcript(self.transcripts)

        th.call_hook("subagent-stop", "claude", th.claude("SubagentStop", self.root, agent_transcript_path=str(path), **REVIEWER))

        self.assertEqual(self.stop_event()["records"], ["C1"])
        self.assertEqual(hook_errors.read(self.repo.ledger_dir), ())

    def test_a_transcript_beside_the_session_transcript_is_read(self) -> None:
        self.start_reviewer()
        path = self.covering_transcript(self.root)  # `th.claude` puts the session transcript in the work tree

        th.call_hook("subagent-stop", "claude", th.claude("SubagentStop", self.root, agent_transcript_path=str(path), **REVIEWER))

        self.assertEqual(self.stop_event()["records"], ["C1"])

    def test_a_forged_transcript_elsewhere_is_not_read_and_the_stop_says_why(self) -> None:
        self.start_reviewer()
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        path = self.covering_transcript(elsewhere)

        th.call_hook("subagent-stop", "claude", th.claude("SubagentStop", self.root, agent_transcript_path=str(path), **REVIEWER))

        event = self.stop_event()
        self.assertEqual(event["event"], "stop")  # recorded, judged by the time window
        self.assertNotIn("records", event)
        self.assertNotIn("usage", event)
        (entry,) = hook_errors.read(self.repo.ledger_dir)
        self.assertEqual((entry.kind, entry.source), ("provenance", "subagent-stop"))
        self.assertIn(str(path), entry.message)

    def test_a_symlink_out_of_the_root_is_not_followed(self) -> None:
        self.start_reviewer()
        real = self.covering_transcript(self.tmp)
        link = self.transcripts / "agent-h1.jsonl"
        link.symlink_to(real)

        th.call_hook("subagent-stop", "claude", th.claude("SubagentStop", self.root, agent_transcript_path=str(link), **REVIEWER))

        self.assertNotIn("records", self.stop_event())
        self.assertEqual([entry.kind for entry in hook_errors.read(self.repo.ledger_dir)], ["provenance"])

    def test_a_dot_dot_path_into_the_root_from_outside_is_judged_by_where_it_ends(self) -> None:
        self.start_reviewer()
        path = self.covering_transcript(self.tmp)
        sneaky = self.transcripts / ".." / ".." / ".." / path.name

        th.call_hook("subagent-stop", "claude", th.claude("SubagentStop", self.root, agent_transcript_path=str(sneaky), **REVIEWER))

        self.assertNotIn("records", self.stop_event())

    def test_the_copilot_root_is_its_session_state_folder(self) -> None:
        home = self.tmp / "home"
        (home / ".copilot" / "session-state" / "s1").mkdir(parents=True)
        inside = home / ".copilot" / "session-state" / "s1" / "agent.jsonl"
        inside.write_text("{}\n", encoding="utf-8")
        outside = self.tmp / "agent.jsonl"
        outside.write_text("{}\n", encoding="utf-8")
        hook = hooks.HookInput.parse({"cwd": str(self.root), "transcript_path": str(self.tmp / "t" / "s.jsonl")})

        with mock.patch.dict("os.environ", {"HOME": str(home)}):
            roots = hooks.transcript_roots("copilot", hook)

        self.assertTrue(hooks._has_provenance(inside, roots))
        self.assertFalse(hooks._has_provenance(outside, roots))

    def test_a_transcript_that_cannot_be_read_is_logged(self) -> None:
        self.start_reviewer()
        directory = self.transcripts / "agent-h1.jsonl"
        directory.mkdir()

        th.call_hook("subagent-stop", "claude", th.claude("SubagentStop", self.root, agent_transcript_path=str(directory), stop_hook_active=True, **REVIEWER))

        self.assertEqual(self.stop_event()["event"], "stop")
        (entry,) = hook_errors.read(self.repo.ledger_dir)
        self.assertEqual(entry.kind, "transcript")

    def test_a_crash_while_reading_is_logged_and_never_fails_the_stop(self) -> None:
        self.start_reviewer()
        path = self.covering_transcript(self.transcripts)

        with mock.patch("craftsman.hooks.read_transcript", side_effect=RuntimeError("boom")):
            out, _ = th.call_hook("subagent-stop", "claude", th.claude("SubagentStop", self.root, agent_transcript_path=str(path), **REVIEWER))

        self.assertEqual(out, {})
        self.assertEqual(self.stop_event()["event"], "stop")
        self.assertEqual([entry.kind for entry in hook_errors.read(self.repo.ledger_dir)], ["transcript"])

    def test_a_transcript_with_a_line_that_is_not_json_is_logged(self) -> None:
        self.start_reviewer()
        path = self.transcripts / "agent-h1.jsonl"
        path.write_text("not json\n", encoding="utf-8")

        th.call_hook("subagent-stop", "claude", th.claude("SubagentStop", self.root, agent_transcript_path=str(path), stop_hook_active=True, **REVIEWER))

        self.assertEqual([entry.kind for entry in hook_errors.read(self.repo.ledger_dir)], ["transcript"])


class HookErrorLogTests(th.HookTestCase):
    def test_a_crashing_hook_allows_and_leaves_one_crash_line(self) -> None:
        with mock.patch.object(hooks, "handle", side_effect=RuntimeError("boom")):
            out, _ = th.call_hook("stop", "claude", th.claude("Stop", self.root))

        self.assertIn("the quality gate was not checked", out["systemMessage"])
        (entry,) = hook_errors.read(self.repo.ledger_dir)
        self.assertEqual((entry.kind, entry.source, entry.message), ("crash", "stop", "RuntimeError: boom"))

    def test_a_pre_tool_crash_asks_and_is_logged(self) -> None:
        payload = th.claude("PreToolUse", self.root, tool_name="Bash", tool_input={"command": "ls"})
        with mock.patch.object(hooks, "handle", side_effect=RuntimeError("boom")):
            out, _ = th.call_hook("pre-tool", "claude", payload)

        self.assertEqual(decision(out), "ask")
        self.assertEqual([entry.source for entry in hook_errors.read(self.repo.ledger_dir)], ["pre-tool"])

    def test_unreadable_input_is_logged_when_the_directory_is_known(self) -> None:
        with mock.patch("os.getcwd", return_value=str(self.root)):
            out, _ = th.call_hook("stop", "claude", "{not json")

        self.assertIn("unreadable hook input", out["systemMessage"])
        self.assertEqual([entry.kind for entry in hook_errors.read(self.repo.ledger_dir)], ["crash"])

    def test_a_healthy_hook_leaves_no_log(self) -> None:
        th.call_hook("stop", "claude", th.claude("Stop", self.root))
        th.call_hook("pre-tool", "claude", th.claude("PreToolUse", self.root, tool_name="Bash", tool_input={"command": "ls"}))

        self.assertFalse((self.repo.ledger_dir / "hook-errors.log").exists())

    def test_review_check_prints_the_hook_errors_of_the_round(self) -> None:
        self.repo.write("cart.py", th.CHANGE)
        self.repo.cli("review", "prepare")
        with mock.patch.object(hooks, "handle", side_effect=RuntimeError("boom")):
            th.call_hook("stop", "claude", th.claude("Stop", self.root))

        out = self.repo.cli("review", "check", check=False).stdout

        self.assertIn(f"Hook errors since round 1 was prepared: 1 (see {self.repo.ledger_dir / 'hook-errors.log'})", out)


class GitBudgetTests(th.HookTestCase):
    def open_gate_with_a_change(self) -> None:
        self.init()
        self.repo.write("cart.py", th.CHANGE)
        self.repo.cli("review", "prepare")

    def test_a_git_timeout_in_the_stop_hook_is_survived_and_logged_once(self) -> None:
        self.open_gate_with_a_change()
        expired = subprocess.TimeoutExpired(["git"], 1)
        real_run = subprocess.run

        def slow_git(argv: Any, *args: Any, **kwargs: Any) -> Any:
            if isinstance(argv, list) and argv[:1] == ["git"] and "diff" in argv:
                raise expired
            return real_run(argv, *args, **kwargs)

        with mock.patch("craftsman.diffscan.subprocess.run", side_effect=slow_git):
            out, _ = th.call_hook("stop", "claude", th.claude("Stop", self.root))

        self.assertEqual(out.get("decision"), "block")  # the gate cannot be evaluated: the agent keeps working
        self.assertIn("timed out", out["reason"])
        entries = hook_errors.read(self.repo.ledger_dir)
        self.assertEqual([entry.kind for entry in entries], ["git-timeout"])
        self.assertEqual(entries[0].source, "stop")

    def test_a_git_timeout_that_reaches_the_hook_entry_point_is_logged_once(self) -> None:
        self.open_gate_with_a_change()

        def run_out_of_time(event: str, platform: str, payload: dict[str, Any]) -> Any:
            raise diffscan.git_timed_out("`git diff` timed out after 25s")  # logged by the budget, then raised

        with mock.patch.object(hooks, "handle", run_out_of_time):
            out, err = th.call_hook("stop", "claude", th.claude("Stop", self.root))

        entries = hook_errors.read(self.repo.ledger_dir)
        self.assertEqual([entry.kind for entry in entries], ["git-timeout"])
        self.assertIn("timed out", err)
        self.assertIn("not checked", json.dumps(out))

    def test_review_check_surfaces_the_timeout(self) -> None:
        self.open_gate_with_a_change()
        hook_errors.record(self.repo.ledger_dir, "git-timeout", "`git diff` timed out after 25s", source="stop")

        out = self.repo.cli("review", "check", check=False).stdout

        self.assertIn("Hook errors since round 1 was prepared: 1", out)

    def test_the_hook_runs_with_the_budget_of_its_event(self) -> None:
        self.open_gate_with_a_change()
        real = diffscan.git_time_left

        def longest_wait(event: str, payload: dict[str, Any]) -> float:
            seen: list[float] = []

            def spy(limit: float | None = None) -> float:
                seen.append(real(limit))
                return seen[-1]

            with mock.patch.object(diffscan, "git_time_left", spy):
                th.call_hook(event, "claude", payload)
            return max(seen, default=0.0)

        pre_tool = longest_wait("pre-tool", th.claude("PreToolUse", self.root, tool_name="Bash", tool_input={"command": "git status"}))
        stop = longest_wait("stop", th.claude("Stop", self.root))

        self.assertGreater(pre_tool, 0.0)
        self.assertLessEqual(pre_tool, 25.0)  # the pre-tool hook has 30 s of which 5 s stay for the answer
        self.assertGreater(stop, 25.0)  # the stop hook has 60 s
        self.assertLessEqual(stop, 55.0)  # not the 60 s a git call has when no budget is installed


class SchemaFiveTests(th.HookTestCase):
    def test_a_ledger_of_a_newer_craftsman_is_fail_open_with_a_notice_and_one_log_line(self) -> None:
        th.call_hook("prompt", "claude", th.claude("UserPromptSubmit", self.root, prompt="/craftsman:craft-review the cart"))
        self.repo.write("cart.py", th.CHANGE)
        self.repo.ledger_dir.mkdir(exist_ok=True)
        ledger = self.repo.ledger_dir / "ledger.json"
        ledger.write_text(json.dumps({"schema": 5, "rounds": [], "future": True}), encoding="utf-8")
        before = (ledger.read_bytes(), ledger.stat().st_mtime_ns)

        out, _ = th.call_hook("stop", "claude", th.claude("Stop", self.root))

        self.assertNotIn("decision", out)
        self.assertIn("could not check the review", out["systemMessage"])
        self.assertIn("newer Craftsman", out["systemMessage"])
        self.assertEqual((ledger.read_bytes(), ledger.stat().st_mtime_ns), before)  # not set aside, not rewritten
        self.assertEqual(sorted(path.name for path in self.repo.ledger_dir.glob("ledger*")), ["ledger.json"])
        (entry,) = hook_errors.read(self.repo.ledger_dir)
        self.assertEqual(entry.kind, "ledger")
        self.assertIn("schema 5", entry.message)

    def test_the_work_tree_guard_logs_a_ledger_it_cannot_read(self) -> None:
        self.repo.ledger_dir.mkdir(exist_ok=True)
        (self.repo.ledger_dir / "ledger.json").write_text(json.dumps({"schema": 5}), encoding="utf-8")
        payload = th.claude("PreToolUse", self.root, tool_name="Bash", tool_input={"command": "git checkout -- ."})

        out, _ = th.call_hook("pre-tool", "claude", payload)

        self.assertEqual(out, {})  # fail open: no review is known to be in progress
        self.assertEqual([entry.kind for entry in hook_errors.read(self.repo.ledger_dir)], ["ledger"])

    def test_a_newer_ledger_in_the_subagent_hook_is_a_logged_ledger_problem_not_a_crash(self) -> None:
        self.repo.ledger_dir.mkdir(exist_ok=True)
        (self.repo.ledger_dir / "ledger.json").write_text(json.dumps({"schema": 5}), encoding="utf-8")

        out, _ = th.call_hook("subagent-start", "claude", th.claude("SubagentStart", self.root, **REVIEWER))

        self.assertIn("systemMessage", out)
        self.assertEqual([entry.kind for entry in hook_errors.read(self.repo.ledger_dir)], ["ledger"])

    def test_an_older_ledger_a_hook_moves_aside_is_logged(self) -> None:
        self.repo.ledger_dir.mkdir(exist_ok=True)
        (self.repo.ledger_dir / "ledger.json").write_text(json.dumps({"schema": 2, "rounds": []}), encoding="utf-8")

        th.call_hook("subagent-start", "claude", th.claude("SubagentStart", self.root, **REVIEWER))

        entries = hook_errors.read(self.repo.ledger_dir)
        self.assertEqual([entry.kind for entry in entries], ["ledger"])
        self.assertIn("older craftsman was set aside", entries[0].message)


class AwaitUserTests(th.HookTestCase):
    def park(self, *extra: str) -> None:
        self.init()
        self.repo.write("cart.py", th.CHANGE)
        self.repo.cli("gate", "--await-user", "--cwd", ".", *extra)

    def session_transcript(self, text: str) -> Path:
        path = self.transcripts / "session.jsonl"
        message = {"role": "assistant", "content": [{"type": "text", "text": text}]}
        path.write_text(json.dumps({"type": "user", "message": {"role": "user", "content": "go"}}) + "\n" + json.dumps({"type": "assistant", "message": message}) + "\n", encoding="utf-8")
        return path

    def stop(self, **extra: Any) -> dict[str, Any]:
        return th.call_hook("stop", "claude", th.claude("Stop", self.root, **extra))[0]

    def entries(self) -> list[str]:
        return [entry.kind for entry in hook_errors.read(self.repo.ledger_dir)]

    def test_the_state_keeps_the_time_and_the_question_and_resume_clears_them(self) -> None:
        self.park("--question", "Which database should the cart use?")

        state = self.state()
        self.assertTrue(state["awaiting_user"])
        self.assertEqual(state["question"], "Which database should the cart use?")
        self.assertRegex(state["awaiting_since"], r"^\d{4}-\d\d-\d\dT")
        status = json.loads(self.repo.cli("status", "--json").stdout)
        self.assertEqual((status["awaiting_user"], status["question"]), (True, "Which database should the cart use?"))
        self.assertIn("Question       : Which database", self.repo.cli("status").stdout)

        self.repo.cli("gate", "--resume", "--cwd", ".")
        state = self.state()
        self.assertEqual((state["awaiting_user"], state["awaiting_since"], state["question"]), (False, None, None))

    def test_a_question_needs_await_user(self) -> None:
        self.init()

        refused = self.repo.cli("gate", "--resume", "--question", "x", "--cwd", ".", check=False)

        self.assertEqual(refused.returncode, 2)
        self.assertIn("--question: only valid with --await-user", refused.stderr)

    def test_a_stop_that_asks_a_question_is_fine(self) -> None:
        self.park()

        out = self.stop(last_assistant_message="I need a decision.\n\nShould the cart round prices up?")

        self.assertEqual(out, {})
        self.assertEqual(self.entries(), [])

    def test_a_stop_without_a_question_is_allowed_and_logged(self) -> None:
        self.park()

        out = self.stop(last_assistant_message="All done, waiting.")

        self.assertEqual(out, {})  # the stop is visible to the user: nothing is blocked
        (entry,) = hook_errors.read(self.repo.ledger_dir)
        self.assertEqual((entry.kind, entry.source), ("await-user", "stop"))
        self.assertIn("await-user without a question", entry.message)

    def test_the_last_message_is_read_from_the_session_transcript(self) -> None:
        self.park()

        self.stop(transcript_path=str(self.session_transcript("Which one do you prefer?")))
        self.assertEqual(self.entries(), [])
        self.stop(transcript_path=str(self.session_transcript("Waiting for you.")))
        self.assertEqual(self.entries(), ["await-user"])

    def test_a_stored_question_text_in_the_message_counts_even_without_a_question_mark(self) -> None:
        self.park("--question", "Use Postgres or SQLite")

        self.stop(last_assistant_message="Decision needed. Use Postgres or SQLite -- tell me.")

        self.assertEqual(self.entries(), [])

    def test_a_question_mark_in_an_earlier_paragraph_is_not_enough(self) -> None:
        self.park()

        self.stop(last_assistant_message="Why did it fail?\n\nI will wait now.")

        self.assertEqual(self.entries(), ["await-user"])

    def test_a_message_that_cannot_be_found_is_not_judged(self) -> None:
        self.park()

        self.stop()  # no last message and a transcript path that does not exist

        self.assertEqual(self.entries(), ["transcript"])  # the unreadable transcript is logged, the stop is not judged

    def test_a_gate_that_is_not_parked_logs_nothing(self) -> None:
        self.init()
        self.repo.write("cart.py", th.CHANGE)

        out = self.stop(last_assistant_message="Done.")

        self.assertEqual(out["decision"], "block")
        self.assertEqual(self.entries(), [])


class FeedbackTests(th.HookTestCase):
    def test_the_foreign_gate_note_ends_with_the_takeover_hint(self) -> None:
        self.repo.cli("gate", "--init", "--cwd", ".", "--session-id", "owner-a")
        self.repo.write("cart.py", th.CHANGE)

        out = self.repo.cli("review", "prepare", env_extra={"CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "someone-else"}).stdout

        self.assertIn("This checkout has an active /craftsman gate", out)
        self.assertIn(TAKEOVER_NOTE, out)
        self.assertEqual(TAKEOVER_NOTE, "If that session is gone, take its gate over with `craftsman gate --init --takeover`.")


if __name__ == "__main__":
    unittest.main()
