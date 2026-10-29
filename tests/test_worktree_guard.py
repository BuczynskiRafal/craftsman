"""Acceptance tests for the PreToolUse work-tree mutation guard (`guard.py` and `hooks.py`)."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest import mock

from craftsman import hooks
from craftsman.guard import find_worktree_mutation
from craftsman.hooks import COPILOT_PUSH_NOTE, Session, _review_in_progress
from craftsman.records import format_timestamp

from tests.support import EvidenceRepo

CHANGE = "def total(items):\n    return sum(items)\n"

ALWAYS_BLOCKED_COMMANDS: tuple[tuple[str, str], ...] = (
    ("git stash", "`git stash`"),
    ("git stash push", "`git stash push`"),
    ("git stash pop", "`git stash pop`"),
    ("git stash apply", "`git stash apply`"),
    ("git stash drop", "`git stash drop`"),
    ("git stash clear", "`git stash clear`"),
    ("git stash branch tmp", "`git stash branch`"),
    ("git stash save wip", "`git stash save`"),
    ("git stash -u", "`git stash`"),
    ("git stash -p", "`git stash`"),
    ("git stash -m wip", "`git stash`"),
    ('git stash -m "wip"', "`git stash`"),
    ("git stash -m list", "`git stash`"),
    ("git stash -- a.py", "`git stash`"),
    ("git stash -- list", "`git stash`"),
    ("git stash list && git stash drop", "`git stash drop`"),
    ("git -C x stash", "`git stash`"),
    ("git -c alias.s=stash s", "`git stash`"),
    ('git -c alias.sp="stash -u" sp', "`git stash`"),
    ("git reset", "`git reset`"),
    ("git reset --soft HEAD~1", "`git reset`"),
    ("env A=1 git reset --hard", "`git reset`"),
    ("git checkout main", "`git checkout`"),
    ("git checkout -b feat/new", "`git checkout`"),
    ('bash -c "git checkout -- a.py"', "`git checkout`"),
    (r"find . -name '*.py' -exec git checkout -- {} \;", "`git checkout`"),
    ("git switch main", "`git switch`"),
    ("git switch -c feat/new", "`git switch`"),
    ("git restore .", "`git restore`"),
    ("git restore --staged a.py", "`git restore`"),
    ("cd x && git restore .", "`git restore`"),
    ("git clean -fd", "`git clean`"),
    ("git clean -n", "`git clean`"),
)

SUBAGENT_ONLY_COMMANDS: tuple[tuple[str, str], ...] = (
    ('git commit -m "feat: add x"', "`git commit`"),
    ("git merge feature", "`git merge`"),
    ("git rebase main", "`git rebase`"),
    ("git cherry-pick deadbeef", "`git cherry-pick`"),
    ("git revert deadbeef", "`git revert`"),
    ("git am patch.mbox", "`git am`"),
    ("git apply fix.patch", "`git apply`"),
    ("git pull --ff-only", "`git pull`"),
    ("git rm app.py", "`git rm`"),
    ("git mv app.py old.py", "`git mv`"),
)

ALLOWED_READ_COMMANDS: tuple[str, ...] = (
    "",
    "   ",
    "pytest -q",
    "echo git stash",
    "git status",
    "git status --porcelain",
    "git diff",
    "git diff --cached",
    "git show HEAD:app.py",
    "git log -n 5",
    "git grep total",
    "git ls-files",
    "git blame app.py",
    "git rev-parse HEAD",
    "git stash list",
    "git stash list -n 3",
    "git stash show",
    "git stash show -p stash@{0}",
    "git stash create",
    'git -c alias.sl="stash list" sl',
)


def call_hook(event: str, platform: str, payload: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Run `craftsman hook` in-process with `payload` on stdin; return (stdout JSON, stderr)."""
    stdin = io.StringIO(json.dumps(payload))
    out, err = io.StringIO(), io.StringIO()
    with mock.patch("sys.stdin", stdin), redirect_stdout(out), redirect_stderr(err):
        code = hooks.main([event, "--platform", platform])
    assert code == 0, err.getvalue()
    parsed = json.loads(out.getvalue())
    assert isinstance(parsed, dict)
    return parsed, err.getvalue()


def claude_shell(cwd: Path, command: str, **extra: Any) -> dict[str, Any]:
    return {
        "session_id": "claude-session",
        "transcript_path": str(cwd / "no-such-transcript.jsonl"),
        "cwd": str(cwd),
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": command},
        **extra,
    }


def copilot_shell(cwd: Path, command: str, **extra: Any) -> dict[str, Any]:
    return {
        "timestamp": "2026-10-29T06:00:00.000Z",
        "hook_event_name": "PreToolUse",
        "cwd": str(cwd),
        "session_id": "vscode-session",
        "tool_name": "run_in_terminal",
        "tool_input": {"command": command},
        **extra,
    }


class ClassificationTests(unittest.TestCase):
    def test_worktree_mutations_are_blocked_at_both_levels(self) -> None:
        for subagent in (False, True):
            for command, expected in ALWAYS_BLOCKED_COMMANDS:
                with self.subTest(subagent=subagent, command=command):
                    self.assertEqual(
                        find_worktree_mutation(command, None, subagent=subagent), expected
                    )

    def test_reads_are_allowed_at_both_levels(self) -> None:
        for subagent in (False, True):
            for command in ALLOWED_READ_COMMANDS:
                with self.subTest(subagent=subagent, command=command):
                    self.assertIsNone(find_worktree_mutation(command, None, subagent=subagent))

    def test_subagent_extra_commands_depend_on_subagent_flag(self) -> None:
        for command, expected in SUBAGENT_ONLY_COMMANDS:
            with self.subTest(command=command):
                self.assertIsNone(find_worktree_mutation(command, None, subagent=False))
                self.assertEqual(find_worktree_mutation(command, None, subagent=True), expected)

    def test_compound_command_reports_first_blocked_git_call(self) -> None:
        cmd = "git status && git stash list && git reset --hard && git stash"
        self.assertEqual(find_worktree_mutation(cmd, None, subagent=False), "`git reset`")


class WorktreeGuardHookTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.repo = EvidenceRepo(self.tmp / "repo")
        self.root = self.repo.root
        self.repo.write("app.py", CHANGE)

    @property
    def ledger_file(self) -> Path:
        return self.repo.ledger_dir / "ledger.json"

    def prepare_review(self, tier: str = "quick") -> None:
        self.repo.cli("review", "prepare", "--tier", tier)

    def set_prepared_at(self, when: datetime) -> None:
        raw = json.loads(self.ledger_file.read_text(encoding="utf-8"))
        raw["rounds"][-1]["prepared_at"] = format_timestamp(when)
        self.ledger_file.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")

    def add_finding(self) -> None:
        self.repo.cli(
            "review",
            "add",
            "--lens",
            "intent",
            "--file",
            "app.py",
            "--line",
            "2",
            "--severity",
            "major",
            "--title",
            "Strings are summed without validation",
            "--scenario",
            "total(['a']) raises TypeError instead of rejecting non-numeric items",
        )

    def test_reviewer_subagent_with_git_stash_is_denied(self) -> None:
        expected = (
            "Craftsman reviewers and verifiers must not change the work tree or the index "
            "(`git stash`). Read an old version with `git show <base>:<path>`; put reproductions "
            "in a temp directory."
        )
        for platform, payload in (
            ("claude", claude_shell(self.root, "git stash", agent_type="craftsman-reviewer")),
            ("copilot", copilot_shell(self.root, "git stash", agent_type="craftsman-reviewer")),
            (
                "copilot",
                {
                    "sessionId": "cli-s",
                    "cwd": str(self.root),
                    "agentType": "custom",
                    "agentName": "craftsman-reviewer",
                    "toolName": "bash",
                    "toolArgs": json.dumps({"command": "git stash"}),
                },
            ),
        ):
            with self.subTest(platform=platform):
                out, _ = call_hook("pre-tool", platform, payload)
                self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
                self.assertEqual(out["hookSpecificOutput"]["permissionDecisionReason"], expected)

    def test_verifier_subagent_cannot_commit_or_apply_patches(self) -> None:
        self.repo.cli("gate", "--init", "--tier", "standard", "--cwd", ".")
        for command in ('git commit -m "repro"', "git apply repro.patch", "git checkout -- app.py"):
            with self.subTest(command=command):
                payload = claude_shell(self.root, command, agent_type="craftsman-verifier")
                out, _ = call_hook("pre-tool", "claude", payload)
                self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_orchestrator_agent_can_commit_during_review(self) -> None:
        self.prepare_review()
        for platform, builder in (("claude", claude_shell), ("copilot", copilot_shell)):
            with self.subTest(platform=platform):
                payload = builder(self.root, 'git commit -m "feat: add total"', agent_type="craftsman")
                out, _ = call_hook("pre-tool", platform, payload)
                self.assertEqual(out, {})

    def test_main_session_with_open_review_asks_on_git_stash(self) -> None:
        self.prepare_review()
        expected = (
            "A Craftsman review is in progress (round 1); `git stash` would change the files "
            "under review. Approve only if you mean it."
        )
        claude_out, _ = call_hook("pre-tool", "claude", claude_shell(self.root, "git stash"))
        self.assertEqual(claude_out["hookSpecificOutput"]["permissionDecision"], "ask")
        self.assertEqual(claude_out["hookSpecificOutput"]["permissionDecisionReason"], expected)

        copilot_out, _ = call_hook("pre-tool", "copilot", copilot_shell(self.root, "git stash"))
        self.assertEqual(copilot_out["permissionDecision"], "deny")
        self.assertEqual(copilot_out["permissionDecisionReason"], expected)
        self.assertNotIn(COPILOT_PUSH_NOTE, copilot_out["permissionDecisionReason"])

    def test_unverified_finding_keeps_review_in_progress(self) -> None:
        self.prepare_review()
        self.add_finding()
        self.repo.cover_all()

        out, _ = call_hook("pre-tool", "claude", claude_shell(self.root, "git restore ."))
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "ask")
        self.assertIn("`git restore`", out["hookSpecificOutput"]["permissionDecisionReason"])

        self.repo.cli(
            "review",
            "verdict",
            "F1",
            "--as",
            "dismissed",
            "--evidence",
            "guarded by app.py:1 where callers only pass integers",
        )
        closed_out, _ = call_hook("pre-tool", "claude", claude_shell(self.root, "git restore ."))
        self.assertEqual(closed_out, {})

    def test_closed_review_round_allows_worktree_mutation(self) -> None:
        self.prepare_review("standard")
        self.repo.cover_all()

        out, _ = call_hook("pre-tool", "claude", claude_shell(self.root, "git stash"))
        self.assertEqual(out, {})

    def test_review_round_older_than_twelve_hours_allows_mutation(self) -> None:
        self.prepare_review()
        now = datetime.now(timezone.utc)
        self.set_prepared_at(now - timedelta(hours=11, minutes=59))
        recent_out, _ = call_hook("pre-tool", "claude", claude_shell(self.root, "git stash"))
        self.assertEqual(recent_out["hookSpecificOutput"]["permissionDecision"], "ask")

        self.set_prepared_at(now - timedelta(hours=12, minutes=1))
        stale_out, _ = call_hook("pre-tool", "claude", claude_shell(self.root, "git stash"))
        self.assertEqual(stale_out, {})

    def test_active_gate_allows_main_session_worktree_commands(self) -> None:
        self.repo.cli("gate", "--init", "--tier", "standard", "--cwd", ".")
        self.prepare_review("standard")
        for agent_extra in ({}, {"agent_type": "craftsman"}):
            for command in ("git stash", "git checkout -b feat/x", "git reset --soft HEAD~1"):
                with self.subTest(agent=agent_extra, command=command):
                    payload = claude_shell(self.root, command, **agent_extra)
                    out, _ = call_hook("pre-tool", "claude", payload)
                    self.assertEqual(out, {})

    def test_git_status_is_always_allowed(self) -> None:
        self.prepare_review()
        for extra in (
            {},
            {"agent_type": "craftsman"},
            {"agent_type": "craftsman-reviewer"},
            {"agent_type": "craftsman-verifier"},
        ):
            with self.subTest(extra=extra):
                out, _ = call_hook("pre-tool", "claude", claude_shell(self.root, "git status", **extra))
                self.assertEqual(out, {})

    def test_unreadable_ledger_logs_warning_without_blocking(self) -> None:
        self.prepare_review()
        self.ledger_file.write_text("{corrupt json", encoding="utf-8")

        out, err = call_hook("pre-tool", "claude", claude_shell(self.root, "git stash"))
        self.assertEqual(out, {})
        self.assertIn("not valid JSON", err)
        captured = io.StringIO()
        with redirect_stderr(captured):
            self.assertIsNone(_review_in_progress(Session(self.root)))
        self.assertIn("not valid JSON", captured.getvalue())

    def test_worktree_guard_does_not_compute_diff_snapshot(self) -> None:
        self.prepare_review()
        with mock.patch("craftsman.hooks.snapshot", side_effect=AssertionError("snapshot called")):
            out, _ = call_hook("pre-tool", "claude", claude_shell(self.root, "git stash"))
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "ask")

    def test_outside_git_repo_allows_main_session_but_denies_reviewer(self) -> None:
        plain = self.tmp / "plain"
        plain.mkdir()
        self.assertEqual(call_hook("pre-tool", "claude", claude_shell(plain, "git stash"))[0], {})
        reviewer = claude_shell(plain, "git stash", agent_type="craftsman-reviewer")
        out, _ = call_hook("pre-tool", "claude", reviewer)
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_closed_gate_does_not_suppress_review_only_prompt(self) -> None:
        self.repo.cli("gate", "--init", "--tier", "quick", "--cwd", ".")
        self.repo.record_full_evidence("quick")
        self.repo.cli("gate", "--complete", "--cwd", ".")
        self.repo.write("app.py", CHANGE + "x = 2\n")
        self.prepare_review()

        out, _ = call_hook("pre-tool", "claude", claude_shell(self.root, "git clean -fd"))
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "ask")
        self.assertIn("(round 2); `git clean`", out["hookSpecificOutput"]["permissionDecisionReason"])


if __name__ == "__main__":
    unittest.main()
