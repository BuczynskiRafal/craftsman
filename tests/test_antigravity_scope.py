"""Tests for the Antigravity PreToolUse hook (`craftsman.scope`, `scripts/craftsman-scope-guard.py`)."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from unittest import mock

from craftsman import hook_errors
from craftsman.command_policy import (
    BRIEFS_ARE_FOR_SUBAGENTS,
    HOOKS_ARE_THE_PLATFORMS,
    IDENTITY_IS_THE_PLATFORMS,
    LEDGER_IS_WRITTEN_BY_THE_CLI,
    NO_RESTART,
    TRANSCRIPTS_ARE_THE_PLATFORMS,
    tool_kind,
)
from craftsman.gate import GateOwner
from craftsman.hook_budget import HOOK_TIMEOUTS_S, git_budget_s
from craftsman.ledger import ledger_path_for
from craftsman.scope import evaluate_antigravity_payload
from craftsman.scope import main as scope_guard_main
from craftsman.workspace import ReviewMarker, live_marker, read_marker

from tests.support import (
    AGY_EPOCH,
    REPO_ROOT,
    AgyApp,
    EvidenceRepo,
    cut_user_input,
    long_user_input,
    open_gate,
    planner,
    user_input,
)

MAIN = "main-1"
SUB = "sub-1"
OTHER = "main-2"


def clock(seconds: float) -> datetime:
    return AGY_EPOCH + timedelta(seconds=seconds)


class AntigravityPreToolTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.repo = EvidenceRepo(self.tmp / "repo")
        self.app = AgyApp(self.tmp / "app")
        self.ledger_path = ledger_path_for(self.repo.root)
        self.app.write_transcript(MAIN, user_input(0, "fix the totals", 0))

    def subagent(self, role: str = "craftsman-reviewer", conversation: str = SUB, parent: str = MAIN) -> None:
        self.app.write_subagent(parent, conversation, planner(1, at=3), role=role)

    def call(
        self,
        conversation: str,
        name: str,
        args: dict[str, Any] | None = None,
        *,
        now: datetime | None = None,
        **extra: Any,
    ) -> tuple[str, str | None]:
        payload = self.app.payload(conversation, self.repo.root, toolCall={"name": name, "args": args or {}}, **extra)
        return evaluate_antigravity_payload(payload, {}, now or clock(10))

    def shell(self, conversation: str, command: str, **kwargs: Any) -> tuple[str, str | None]:
        return self.call(conversation, "run_command", {"CommandLine": command, "Cwd": str(self.repo.root)}, **kwargs)

    def marker(self) -> ReviewMarker | None:
        return read_marker(self.ledger_path)


class PolicyTests(AntigravityPreToolTestCase):
    """The shared command policy reaches Antigravity's tools (E10.1, E9)."""

    def test_a_hook_command_is_denied(self) -> None:
        for command in ("craftsman hook stop --platform claude", "python3 scripts/craftsman-stop-gate.py < payload.json"):
            with self.subTest(command=command):
                self.assertEqual(self.shell(MAIN, command), ("deny", HOOKS_ARE_THE_PLATFORMS))

    def test_a_write_to_the_ledger_is_denied(self) -> None:
        ledger = self.repo.ledger_dir / "ledger.json"
        self.assertEqual(self.shell(MAIN, f"echo '{{}}' > {ledger}"), ("deny", LEDGER_IS_WRITTEN_BY_THE_CLI))

    def test_arguments_spelled_as_json_text_are_judged_like_the_plain_spelling(self) -> None:
        ledger = self.repo.ledger_dir / "ledger.json"
        command = f"echo '{{}}' > {ledger}"

        for label, args in (
            ("plain", {"CommandLine": command, "Cwd": str(self.repo.root)}),
            ("json text", {"CommandLine": json.dumps(command), "Cwd": json.dumps(str(self.repo.root))}),
        ):
            with self.subTest(label):
                self.assertEqual(self.call(MAIN, "run_command", args), ("deny", LEDGER_IS_WRITTEN_BY_THE_CLI))
        self.assertEqual(self.call(MAIN, "run_command", {"CommandLine": json.dumps("ls"), "Cwd": json.dumps(str(self.repo.root))}), ("allow", None))
        transcript = self.app.transcript_path(MAIN)
        self.assertEqual(
            self.call(MAIN, "write_to_file", {"TargetFile": json.dumps(str(transcript))}), ("deny", TRANSCRIPTS_ARE_THE_PLATFORMS)
        )

    def test_a_transcript_write_is_denied_for_a_shell_command_and_a_file_tool(self) -> None:
        transcript = self.app.transcript_path(MAIN)
        self.assertEqual(self.shell(MAIN, f"echo x >> {transcript}"), ("deny", TRANSCRIPTS_ARE_THE_PLATFORMS))
        for tool, key in (("write_to_file", "TargetFile"), ("replace_file_content", "TargetFile"), ("edit_file", "path")):
            with self.subTest(tool=tool):
                self.assertEqual(self.call(MAIN, tool, {key: str(transcript)}), ("deny", TRANSCRIPTS_ARE_THE_PLATFORMS))

    def test_a_file_tool_may_write_inside_the_work_tree(self) -> None:
        self.assertEqual(self.call(MAIN, "write_to_file", {"TargetFile": str(self.repo.root / "app.py")}), ("allow", None))

    def test_a_file_tool_may_not_write_the_ledger(self) -> None:
        decision, reason = self.call(MAIN, "write_to_file", {"TargetFile": str(self.repo.ledger_dir / "ledger.json")})
        self.assertEqual((decision, reason), ("deny", LEDGER_IS_WRITTEN_BY_THE_CLI))

    def test_an_identity_variable_is_denied(self) -> None:
        for command in (
            f"ANTIGRAVITY_CONVERSATION_ID={SUB} craftsman review show",
            f"export ANTIGRAVITY_CONVERSATION_ID={SUB}; craftsman review show",
            f"env ANTIGRAVITY_CONVERSATION_ID={SUB} craftsman review cover --hunks H1 --note x",
        ):
            with self.subTest(command=command):
                self.assertEqual(self.shell(MAIN, command), ("deny", IDENTITY_IS_THE_PLATFORMS))

    def test_the_policy_applies_outside_a_git_work_tree_too(self) -> None:
        plain = self.tmp / "plain"
        plain.mkdir()
        payload = self.app.payload(
            MAIN, plain, toolCall={"name": "run_command", "args": {"CommandLine": f"echo x > {self.app.transcript_path(MAIN)}"}}
        )
        self.assertEqual(evaluate_antigravity_payload(payload, {}), ("deny", TRANSCRIPTS_ARE_THE_PLATFORMS))
        payload["toolCall"]["args"]["CommandLine"] = "ls"
        self.assertEqual(evaluate_antigravity_payload(payload, {}), ("allow", None))

    def test_a_subagent_may_not_prepare_or_reset_the_review(self) -> None:
        self.subagent()
        for command in ("craftsman review prepare", "craftsman review reset", "craftsman review prepare --deep && ls"):
            with self.subTest(command=command):
                self.assertEqual(self.shell(SUB, command), ("deny", NO_RESTART))

    def test_a_subagent_may_run_the_commands_of_its_work(self) -> None:
        self.subagent()
        self.assertEqual(self.shell(SUB, "craftsman review brief W1-1"), ("allow", None))
        self.assertEqual(self.shell(SUB, "craftsman review cover --hunter W1-1 --hunks H1 --note x"), ("allow", None))

    def test_the_main_conversation_may_not_fetch_a_brief(self) -> None:
        self.assertEqual(self.shell(MAIN, "craftsman review brief W1-1"), ("deny", BRIEFS_ARE_FOR_SUBAGENTS))

    def test_the_main_conversation_may_prepare(self) -> None:
        self.assertEqual(self.shell(MAIN, "craftsman review prepare"), ("allow", None))

    def test_a_subagent_may_not_change_the_work_tree(self) -> None:
        self.subagent()
        decision, reason = self.shell(SUB, "git checkout -- app.py")
        self.assertEqual(decision, "deny")
        self.assertIn("must not change the work tree", reason or "")

    def test_the_main_conversation_is_asked_while_a_review_is_open(self) -> None:
        self.repo.write_lines("app.py", 5)
        self.repo.cli("review", "prepare")
        decision, reason = self.shell(MAIN, "git stash")
        self.assertEqual(decision, "force_ask")
        self.assertIn("A Craftsman review is in progress", reason or "")


class ScopeBindingTests(AntigravityPreToolTestCase):
    SEARCH = "find ~ -name SKILL.md"

    def own_gate(self, conversation: str = MAIN) -> None:
        open_gate(self.repo.root, self.repo.base_sha, owner=GateOwner("antigravity", conversation))

    def test_the_owner_is_scope_checked(self) -> None:
        self.own_gate()
        decision, reason = self.shell(MAIN, self.SEARCH)
        self.assertEqual(decision, "deny")
        self.assertIn("[CRAFTSMAN SCOPE-GUARD]", reason or "")

    def test_a_subagent_of_the_owner_is_scope_checked_as_its_parent(self) -> None:
        self.own_gate()
        self.subagent(role="explorer")  # not a Craftsman role: bound only because its parent owns the gate
        self.assertEqual(self.shell(SUB, self.SEARCH)[0], "deny")

    def test_a_conversation_that_owns_nothing_is_not_scope_checked(self) -> None:
        self.own_gate(OTHER)
        self.app.write_transcript(OTHER, user_input(0, "other", 0))
        self.assertEqual(self.shell(MAIN, self.SEARCH), ("allow", None))
        self.subagent(role="explorer")
        self.assertEqual(self.shell(SUB, self.SEARCH), ("allow", None))

    def test_a_craftsman_subagent_is_scope_checked_without_any_gate(self) -> None:
        self.subagent()
        self.assertEqual(self.shell(SUB, self.SEARCH)[0], "deny")

    def test_a_subagent_of_the_conversation_whose_review_the_marker_guards_is_scope_checked(self) -> None:
        self.subagent(role="explorer")
        self.assertEqual(self.shell(SUB, self.SEARCH), ("allow", None))
        self.shell(MAIN, "craftsman review show")  # arms the guard for MAIN
        self.assertEqual(self.shell(SUB, self.SEARCH)[0], "deny")
        self.assertEqual(self.shell(MAIN, self.SEARCH), ("allow", None))  # the main conversation of a review is free

    def test_file_tools_are_scope_checked_with_the_workspace_as_their_directory(self) -> None:
        self.own_gate()
        other = EvidenceRepo(self.tmp / "other-repo")
        for tool, args in (
            ("view_file", {"AbsolutePath": str(other.root / "app.py")}),
            ("grep_search", {"SearchPath": str(self.tmp), "Query": "x"}),
            ("find_by_name", {"SearchDirectory": str(self.tmp), "Pattern": "*.txt"}),
            ("list_dir", {"DirectoryPath": str(self.tmp)}),
        ):
            with self.subTest(tool=tool):
                self.assertEqual(self.call(MAIN, tool, args)[0], "deny")
        self.assertEqual(self.call(MAIN, "view_file", {"AbsolutePath": str(self.repo.root / "app.py")}), ("allow", None))

    def test_a_relative_path_is_read_from_the_workspace_not_the_process(self) -> None:
        self.own_gate()
        self.assertEqual(self.call(MAIN, "view_file", {"AbsolutePath": "app.py"}), ("allow", None))

    def test_an_invalid_gate_state_asks_the_user(self) -> None:
        state = self.repo.ledger_dir / "craftsman_state.json"
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(json.dumps({"active": True}), encoding="utf-8")
        decision, reason = self.shell(MAIN, self.SEARCH)
        self.assertEqual(decision, "force_ask")
        self.assertIn("gate state", reason or "")


class ReadFileTests(AntigravityPreToolTestCase):
    """`read_file(Path=...)` is the CLI variant's file reader: it is judged exactly like `view_file` (REVIEW-FSE 2)."""

    def own_gate(self) -> None:
        open_gate(self.repo.root, self.repo.base_sha, owner=GateOwner("antigravity", MAIN))

    def test_a_read_of_another_repository_is_denied_like_view_file(self) -> None:
        self.own_gate()
        other = EvidenceRepo(self.tmp / "other-repo")
        target = str(other.root / "app.py")

        by_view = self.call(MAIN, "view_file", {"AbsolutePath": target})
        by_read = self.call(MAIN, "read_file", {"Path": target})

        self.assertEqual(by_view[0], "deny")
        self.assertEqual(by_read[0], "deny")
        self.assertIn("belongs to another git repository", by_read[1] or "")
        self.assertEqual(self.call(MAIN, "read_file", {"Path": str(self.repo.root / "app.py")}), ("allow", None))

    def test_a_read_of_a_protected_transcript_is_denied_like_view_file(self) -> None:
        self.subagent()
        transcript = str(self.app.transcript_path(MAIN))

        by_view = self.call(SUB, "view_file", {"AbsolutePath": transcript})
        by_read = self.call(SUB, "read_file", {"Path": transcript})

        self.assertEqual(by_view[0], "deny")
        self.assertEqual(by_read[0], "deny")
        self.assertIn("protected session store", by_read[1] or "")

    def test_the_app_data_directory_of_the_call_is_protected_for_reads(self) -> None:
        # The conversation store of an app data directory that is not under the home directory.
        stored = AgyApp(self.tmp / "custom-app")
        stored.write_transcript(MAIN, user_input(0, "x", 0))
        stored.write_subagent(MAIN, SUB, planner(1, at=3), role="craftsman-reviewer")
        payload = stored.payload(SUB, self.repo.root, toolCall={"name": "read_file", "args": {"Path": str(stored.transcript_path(MAIN))}})

        decision, reason = evaluate_antigravity_payload(payload, {}, clock(10))

        self.assertEqual(decision, "deny")
        self.assertIn("protected session store", reason or "")

    def test_a_configured_app_data_directory_or_claude_directory_is_protected_for_reads(self) -> None:
        self.subagent()
        app_dir = self.tmp / "configured-app"
        claude_dir = self.tmp / "configured-claude"
        stores = {
            "antigravity transcript": app_dir / "brain" / "c1" / ".system_generated" / "logs" / "transcript.jsonl",
            "claude transcript": claude_dir / "projects" / "slug" / "session.jsonl",
        }
        env = {"ANTIGRAVITY_APP_DATA_DIR": str(app_dir), "CLAUDE_CONFIG_DIR": str(claude_dir)}
        for label, path in stores.items():
            path.parent.mkdir(parents=True)
            path.write_text("{}\n", encoding="utf-8")
            for tool, key in (("read_file", "Path"), ("view_file", "AbsolutePath")):
                with self.subTest(store=label, tool=tool):
                    payload = self.app.payload(SUB, self.repo.root, toolCall={"name": tool, "args": {key: str(path)}})

                    self.assertEqual(evaluate_antigravity_payload(payload, env, clock(10))[0], "deny")
                    self.assertEqual(evaluate_antigravity_payload(payload, {}, clock(10))[0], "allow")  # nobody configured it


class ArmingTests(AntigravityPreToolTestCase):
    def test_a_review_command_of_the_main_conversation_arms_the_guard(self) -> None:
        self.assertEqual(self.shell(MAIN, "craftsman review prepare", now=clock(10)), ("allow", None))
        marker = self.marker()
        assert marker is not None
        self.assertEqual((marker.platform, marker.session_id, marker.guarding), ("antigravity", MAIN, True))
        self.assertEqual(marker.touched_at, "2026-01-01T00:00:10.000Z")
        self.assertIsNotNone(live_marker(self.ledger_path, clock(11)))

    def test_a_report_only_touches(self) -> None:
        self.shell(MAIN, "craftsman review prepare", now=clock(10))
        self.shell(MAIN, "craftsman review report", now=clock(50))
        marker = self.marker()
        assert marker is not None
        self.assertEqual((marker.at, marker.touched_at), ("2026-01-01T00:00:10.000Z", "2026-01-01T00:00:50.000Z"))

    def test_a_command_that_is_no_review_command_arms_nothing(self) -> None:
        self.shell(MAIN, "ls -la")
        self.shell(MAIN, "craftsman run --kind test -- pytest")
        self.assertIsNone(self.marker())

    def test_a_subagent_never_arms_the_guard(self) -> None:
        self.subagent()
        self.shell(SUB, "craftsman review brief W1-1")
        self.assertIsNone(self.marker())

    def test_a_denied_command_arms_nothing(self) -> None:
        self.shell(MAIN, "craftsman review brief W1-1")
        self.assertIsNone(self.marker())

    def test_the_owner_of_a_gate_is_held_by_the_gate_not_the_guard(self) -> None:
        open_gate(self.repo.root, self.repo.base_sha, owner=GateOwner("antigravity", MAIN))
        self.shell(MAIN, "craftsman review prepare")
        self.assertIsNone(self.marker())

    def test_a_craft_review_request_gives_prepare_its_depth_and_task(self) -> None:
        self.app.write_transcript(MAIN, user_input(0, "/craft-review --deep check the totals change", 2))
        self.shell(MAIN, "craftsman review prepare", now=clock(10))
        marker = self.marker()
        assert marker is not None
        self.assertEqual((marker.deep, marker.task, marker.at), (True, "check the totals change", "2026-01-01T00:00:02.000Z"))
        live = live_marker(self.ledger_path, clock(11))
        assert live is not None
        self.assertEqual((live.deep, live.task), (True, "check the totals change"))

    LONG_TASK = "check the totals " + "and the rest of the cart " * 200  # over 4 KB: the compact transcript cuts it

    def test_a_long_craft_review_request_cut_in_the_compact_transcript_gives_prepare_its_depth_and_task(self) -> None:
        full = long_user_input(0, f"/craft-review --deep {self.LONG_TASK}", 2)
        self.app.write_transcript(MAIN, cut_user_input(full))
        self.app.write_transcript(MAIN, full, full=True)

        err = io.StringIO()
        with redirect_stderr(err):
            self.shell(MAIN, "craftsman review prepare", now=clock(10))

        marker = self.marker()
        assert marker is not None
        self.assertEqual((marker.deep, marker.task, marker.at), (True, " ".join(self.LONG_TASK.split())[:300], "2026-01-01T00:00:02.000Z"))
        self.assertEqual(err.getvalue(), "")

    def test_a_cut_request_nobody_can_restore_is_not_read_and_is_reported(self) -> None:
        full = long_user_input(0, f"/craft-review --deep {self.LONG_TASK}", 2)
        self.app.write_transcript(MAIN, cut_user_input(full))  # no transcript_full.jsonl

        err = io.StringIO()
        with redirect_stderr(err):
            result = self.shell(MAIN, "craftsman review prepare", now=clock(10))

        self.assertEqual(result, ("allow", None))
        self.assertIn("was cut from the compact transcript", err.getvalue())
        marker = self.marker()
        assert marker is not None  # the command still arms the guard, as it does without any request
        self.assertEqual((marker.deep, marker.task, marker.at), (False, "", "2026-01-01T00:00:10.000Z"))

    def test_the_same_request_never_arms_twice(self) -> None:
        self.app.write_transcript(MAIN, user_input(0, "/craft-review check", 2))
        self.shell(MAIN, "craftsman review prepare", now=clock(10))
        self.shell(MAIN, "craftsman review show", now=clock(20))
        marker = self.marker()
        assert marker is not None
        self.assertEqual((marker.at, marker.touched_at), ("2026-01-01T00:00:02.000Z", "2026-01-01T00:00:20.000Z"))

    def test_a_newer_request_starts_a_fresh_review(self) -> None:
        self.app.write_transcript(MAIN, user_input(0, "/craft-review check", 2))
        self.shell(MAIN, "craftsman review prepare", now=clock(10))
        self.app.write_transcript(MAIN, user_input(0, "/craft-review check", 2), user_input(5, "/craft-review --deep again", 60))
        self.shell(MAIN, "craftsman review show", now=clock(70))
        marker = self.marker()
        assert marker is not None
        self.assertEqual((marker.at, marker.deep, marker.task, marker.forced), ("2026-01-01T00:01:00.000Z", True, "again", 0))

    def test_a_later_user_message_that_is_no_request_disarms_before_the_command_re_arms(self) -> None:
        self.app.write_transcript(MAIN, user_input(0, "/craft-review --deep check", 2))
        self.shell(MAIN, "craftsman review prepare", now=clock(10))
        self.app.write_transcript(
            MAIN, user_input(0, "/craft-review --deep check", 2), user_input(5, "thanks, now explain the diff", 60)
        )

        self.shell(MAIN, "craftsman review show", now=clock(70))

        marker = self.marker()
        assert marker is not None
        # The guard ended when the user moved on; the command began a new review, without the old request's depth.
        self.assertEqual((marker.guarding, marker.deep, marker.task, marker.at), (True, False, "", "2026-01-01T00:01:10.000Z"))

    def test_a_non_review_message_before_the_marker_changes_nothing(self) -> None:
        self.shell(MAIN, "craftsman review prepare", now=clock(10))
        self.app.write_transcript(MAIN, user_input(0, "fix the totals", 0))
        self.shell(MAIN, "craftsman review show", now=clock(20))
        marker = self.marker()
        assert marker is not None
        self.assertEqual(marker.at, "2026-01-01T00:00:10.000Z")

    def test_a_request_without_a_time_is_not_read(self) -> None:
        self.app.write_transcript(MAIN, user_input(0, "/craft-review --deep check"))
        self.shell(MAIN, "craftsman review prepare", now=clock(10))
        marker = self.marker()
        assert marker is not None
        self.assertFalse(marker.deep)

    def test_an_unreadable_transcript_does_not_block_the_command(self) -> None:
        path = self.app.transcript_path(MAIN)
        path.write_text("not json\n" + path.read_text(encoding="utf-8"), encoding="utf-8")
        err = io.StringIO()
        with redirect_stderr(err):
            result = self.shell(MAIN, "craftsman review prepare", now=clock(10))
        self.assertEqual(result, ("allow", None))
        self.assertIn("could not read the review request", err.getvalue())
        self.assertIsNotNone(self.marker())

    def test_another_sessions_fresh_guard_is_kept(self) -> None:
        self.app.write_transcript(OTHER, user_input(0, "/craft-review", 0))
        self.shell(OTHER, "craftsman review prepare", now=clock(10))
        self.shell(MAIN, "craftsman review prepare", now=clock(20))
        marker = self.marker()
        assert marker is not None
        self.assertEqual(marker.session_id, OTHER)


class WorkspaceFallbackTests(AntigravityPreToolTestCase):
    def test_a_file_tool_carries_no_cwd_so_the_workspace_decides_the_checkout(self) -> None:
        self.repo.write_lines("app.py", 3)
        self.shell(MAIN, "craftsman review prepare", now=clock(10))
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        payload = self.app.payload(
            MAIN, self.repo.root, toolCall={"name": "write_to_file", "args": {"TargetFile": str(self.repo.ledger_dir / "ledger.json")}}
        )
        self.assertEqual(evaluate_antigravity_payload(payload, {})[0], "deny")

    def test_without_a_workspace_the_process_directory_is_used(self) -> None:
        payload: dict[str, Any] = {"conversationId": MAIN, "toolCall": {"name": "run_command", "args": {"CommandLine": "ls"}}}
        with mock.patch("craftsman.scope.Path.cwd", return_value=self.repo.root):
            self.assertEqual(evaluate_antigravity_payload(payload, {}), ("allow", None))

    def test_a_payload_without_a_tool_call_or_name_is_allowed(self) -> None:
        payloads: list[dict[str, Any]] = [{}, {"toolCall": "run_command"}, {"toolCall": {"args": {}}}, {"toolCall": {"name": " "}}]
        for payload in payloads:
            with self.subTest(payload=payload):
                self.assertEqual(evaluate_antigravity_payload(payload, {}), ("allow", None))


class EntryPointTests(AntigravityPreToolTestCase):
    def run_main(self, payload: dict[str, Any]) -> dict[str, Any]:
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(scope_guard_main([]), 0)
        return json.loads(out.getvalue())  # type: ignore[no-any-return]

    def test_the_hook_runs_inside_the_antigravity_pre_tool_budget(self) -> None:
        seen: list[tuple[str, object]] = []
        real = hook_errors.hook_run

        def spy(event: str, cwd: object) -> object:
            seen.append((event, cwd))
            return real(event, cwd)  # type: ignore[arg-type]

        payload = self.app.payload(MAIN, self.repo.root, toolCall={"name": "run_command", "args": {"CommandLine": "ls"}})
        with mock.patch.object(hook_errors, "hook_run", spy):
            self.assertEqual(self.run_main(payload), {"decision": "allow"})
        self.assertEqual(seen, [("antigravity-pre-tool", self.repo.root)])

    def test_a_crash_asks_the_user_and_is_logged(self) -> None:
        payload = self.app.payload(MAIN, self.repo.root, toolCall={"name": "run_command", "args": {"CommandLine": "ls"}})
        with mock.patch("craftsman.scope.decide", side_effect=RuntimeError("boom")):
            answer = self.run_main(payload)
        self.assertEqual(answer["decision"], "force_ask")
        self.assertIn("RuntimeError: boom", answer["reason"])
        entries = hook_errors.read(self.repo.ledger_dir)
        self.assertEqual([(entry.kind, entry.source) for entry in entries], [("crash", "antigravity-pre-tool")])

    def test_a_denial_is_the_answer(self) -> None:
        payload = self.app.payload(MAIN, self.repo.root, toolCall={"name": "run_command", "args": {"CommandLine": "craftsman hook stop --platform claude"}})
        self.assertEqual(self.run_main(payload), {"decision": "deny", "reason": HOOKS_ARE_THE_PLATFORMS})


class HooksJsonTests(unittest.TestCase):
    """`hooks.json` wires the guard to every tool that carries a path or a command, with the timeout the budget assumes."""

    def setUp(self) -> None:
        self.config = json.loads((REPO_ROOT / "hooks.json").read_text(encoding="utf-8"))

    def test_the_guard_is_wired_to_every_tool_that_runs_commands_reads_or_writes_files(self) -> None:
        groups = self.config["craftsman-scope-guard"]["PreToolUse"]
        kinds = {group["matcher"]: tool_kind(group["matcher"]) for group in groups}

        self.assertEqual(
            {name for name, kind in kinds.items() if kind == "shell"}, {"run_command"}
        )
        self.assertEqual(
            {name for name, kind in kinds.items() if kind == "read"},
            {"view_file", "read_file", "grep_search", "find_by_name", "list_dir"},
        )
        self.assertGreaterEqual(
            {name for name, kind in kinds.items() if kind == "write"},
            {"write_to_file", "replace_file_content", "multi_replace_file_content", "write_file", "edit_file"},
        )

    def test_every_tool_of_the_cli_variant_is_wired_and_none_of_the_ide_names_is_dropped(self) -> None:
        # `agy` listed its own tools: run_command(CommandLine), read_file(Path), write_file(Path), edit_file(Path).
        # `view_file`, `write_to_file`, `replace_file_content` and `multi_replace_file_content` are the IDE variant's.
        wired = {group["matcher"] for group in self.config["craftsman-scope-guard"]["PreToolUse"]}

        self.assertGreaterEqual(
            wired,
            {"run_command", "read_file", "write_file", "edit_file", "view_file", "write_to_file", "replace_file_content", "multi_replace_file_content"},
        )

    def test_the_read_file_group_is_the_view_file_group(self) -> None:
        groups = {group["matcher"]: group["hooks"] for group in self.config["craftsman-scope-guard"]["PreToolUse"]}

        self.assertEqual(groups["read_file"], groups["view_file"])

    def test_each_matcher_is_one_plain_tool_name(self) -> None:
        # Right whether the platform compares the matcher as a name or searches it as a regular expression. (Under
        # the second reading `multi_replace_file_content` also matches `replace_file_content`: the guard runs twice
        # on that call, and it is idempotent.)
        names = [group["matcher"] for group in self.config["craftsman-scope-guard"]["PreToolUse"]]

        self.assertEqual(len(names), len(set(names)))
        for name in names:
            with self.subTest(name=name):
                self.assertRegex(name, r"^[a-z_]+$")

    def test_the_timeouts_are_the_ones_the_hook_budget_is_derived_from(self) -> None:
        stop = self.config["craftsman-stop-gate"]["Stop"][0]["timeout"]
        pre_tool = {hook["timeout"] for group in self.config["craftsman-scope-guard"]["PreToolUse"] for hook in group["hooks"]}

        self.assertEqual(stop, HOOK_TIMEOUTS_S["antigravity-stop"])
        self.assertEqual(pre_tool, {HOOK_TIMEOUTS_S["antigravity-pre-tool"]})
        self.assertEqual(stop, 30)
        self.assertEqual(git_budget_s("antigravity-stop"), 25)
        self.assertEqual(git_budget_s("antigravity-pre-tool"), 25)


if __name__ == "__main__":
    unittest.main()
