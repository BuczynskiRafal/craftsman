"""Tests for the Craftsman git guard (`craftsman.guard`), called in-process."""

from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from typing import Any
from unittest import mock

from craftsman.guard import evaluate_command, main, was_ask_question_called_since_last_push

from tests.support import REPO_ROOT, run_main, write_jsonl


def push_question(*questions: dict[str, Any]) -> dict[str, Any]:
    """An Antigravity planner step that asks the user `questions`."""
    return {
        "step_index": 1,
        "type": "PLANNER_RESPONSE",
        "tool_calls": [{"name": "ask_question", "args": {"questions": list(questions)}}],
    }


def answers(content: str) -> dict[str, Any]:
    """The Antigravity step that carries the user's answers to the question step."""
    return {"step_index": 2, "type": "GENERIC", "content": content}


class GuardTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.transcript = Path(tmp.name) / "transcript.jsonl"
        self.write_transcript({"step_index": 1, "type": "USER_INPUT"})

    def write_transcript(self, *entries: dict[str, Any]) -> None:
        write_jsonl(self.transcript, *entries)

    def decision(self, command: str) -> str:
        """The guard's decision on an Antigravity `run_command` of `command`."""
        payload = {
            "transcriptPath": str(self.transcript),
            "toolCall": {"name": "run_command", "args": {"CommandLine": command}},
        }
        return evaluate_command(payload)[0]


class RedirectionTests(GuardTestCase):
    def test_redirection_does_not_hide_a_push_to_main(self) -> None:
        for command in ("git push 2>&1 origin main", "git push &> /dev/null origin main"):
            with self.subTest(command=command):
                self.assertEqual(self.decision(command), "deny")

    def test_redirection_glued_to_a_branch_name_ends_the_branch_name(self) -> None:
        self.assertEqual(self.decision("git push origin main2>/dev/null"), "allow")


class ProtectedTargetTests(GuardTestCase):
    def test_reflog_source_pushed_to_main_is_denied(self) -> None:
        self.assertEqual(self.decision("git push origin HEAD@{0}:main"), "deny")

    def test_force_push_later_in_a_compound_command_needs_confirmation(self) -> None:
        self.write_transcript(
            push_question(
                {
                    "question": "How should we push to main?",
                    "options": ["Push directly to main (no PR)", "Create a feature branch"],
                }
            ),
            answers("Q1: How should we push to main?\nA1: Push directly to main (no PR)"),
        )
        command = "git push origin main && git push --force origin main"
        self.assertEqual(self.decision(command), "force_ask")


class PushApprovalTests(GuardTestCase):
    def test_direct_push_answer_approves_despite_rejected_in_the_question(self) -> None:
        question = "The previous hook rejected the push; how should we push to main?"
        self.write_transcript(
            push_question(
                {
                    "question": question,
                    "options": ["Push directly to main (no PR)", "Create a feature branch"],
                }
            ),
            answers(f"Q1: {question}\nA1: Push directly to main (no PR)"),
        )
        self.assertEqual(self.decision("git push origin main"), "allow")

    def test_yes_to_an_unrelated_question_does_not_approve_the_push(self) -> None:
        self.write_transcript(
            push_question(
                {"question": "Run unit tests first?", "options": ["Yes", "No"]},
                {
                    "question": "Push directly to main or open a PR?",
                    "options": ["Open a PR", "Push directly to main"],
                },
            ),
            answers(
                "Q1: Run unit tests first?\nA1: Yes\n"
                "Q2: Push directly to main or open a PR?\nA2: Open a PR"
            ),
        )
        self.assertEqual(self.decision("git push origin main"), "deny")

    def test_unreadable_transcript_approves_no_push(self) -> None:
        self.transcript.unlink()
        self.transcript.mkdir()
        with redirect_stderr(io.StringIO()) as err:
            self.assertFalse(was_ask_question_called_since_last_push(self.transcript))
        self.assertIn("cannot read the transcript", err.getvalue())


class AttributionTests(GuardTestCase):
    def test_human_co_author_with_an_ai_like_name_is_allowed(self) -> None:
        for author in (
            "Nikolai <nikolai@example.com>",
            "John Talbot <j@talbot.org>",
            "Sarah Raider <s@dubai.ae>",
        ):
            with self.subTest(author=author):
                command = f'git commit -m "feat: x\n\nCo-authored-by: {author}"'
                self.assertEqual(self.decision(command), "allow")

    @unittest.skipUnless(Path("/proc/self/mem").is_file(), "needs a file whose read fails")
    def test_message_file_that_cannot_be_read_asks_the_user(self) -> None:
        command = "git commit -F /proc/self/mem"

        self.assertEqual(self.decision(command), "force_ask")


class HookInputTests(unittest.TestCase):
    def guard(self, stdin: str, *args: str) -> tuple[int, str]:
        with mock.patch("sys.stdin", io.StringIO(stdin)):
            result = run_main(main, *args)
        return result.code, result.out

    def test_input_that_is_no_json_is_denied(self) -> None:
        code, out = self.guard("{not json")

        self.assertEqual((code, json.loads(out)["decision"]), (0, "deny"))

    def test_input_that_is_no_object_is_denied(self) -> None:
        code, out = self.guard("[1]")

        self.assertEqual((code, json.loads(out)["decision"]), (0, "deny"))

    def test_unknown_git_hook_is_a_usage_error(self) -> None:
        self.assertEqual(self.guard("", "--git-hook", "bogus")[0], 2)

    def test_commit_msg_hook_without_a_file_is_a_usage_error(self) -> None:
        self.assertEqual(self.guard("", "--git-hook", "commit-msg")[0], 2)


GIT_GUARD = REPO_ROOT / "scripts" / "git-guard.py"


def run_hook(script: Path, payload: dict[str, Any]) -> dict[str, Any]:
    """The decision `script` prints for a PreToolUse `payload`."""
    proc = subprocess.run(
        [sys.executable, str(script)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    )
    parsed = json.loads(proc.stdout.strip())
    assert isinstance(parsed, dict)
    return parsed


class GuardScriptTests(unittest.TestCase):
    """The guard run as the `scripts/git-guard.py` hook and as the native git hooks."""

    def test_allows_non_git_commands_mentioning_protected_strings(self) -> None:
        for cmd in (
            'grep "Co-authored-by: Claude" README.md',
            'echo "git push origin main"',
            "python3 -m unittest discover -s tests",
        ):
            res = run_hook(
                GIT_GUARD,
                {"toolCall": {"name": "run_command", "args": {"CommandLine": cmd}}},
            )
            self.assertEqual(res["decision"], "allow", f"Failed for command: {cmd}")

    def test_allows_clean_atomic_commit(self) -> None:
        res = run_hook(
            GIT_GUARD,
            {
                "toolCall": {
                    "name": "run_command",
                    "args": {
                        "CommandLine": 'git add . && git commit -m "feat(hooks): add hard-loop enforcement"'
                    },
                }
            },
        )
        self.assertEqual(res["decision"], "allow")

    def test_commit_with_ai_trailers_is_denied(self) -> None:
        bad_commands = (
            'git commit -m "feat: add x\n\nCo-authored-by: Claude <noreply@anthropic.com>"',
            'git commit -m "fix: bug" -m "Generated by Gemini"',
            'gh pr create --title "feat" --body "🤖 Generated with Claude Code"',
            '(git commit -m "feat: subshell\n\nCo-authored-by: Antigravity <ai@google.com>")',
        )
        for cmd in bad_commands:
            res = run_hook(
                GIT_GUARD,
                {"toolCall": {"name": "run_command", "args": {"CommandLine": cmd}}},
            )
            self.assertEqual(res["decision"], "deny", f"Expected deny for: {cmd}")
            self.assertIn("Global Rule #2", res.get("reason", ""))

        with tempfile.TemporaryDirectory() as tmpdir:
            msg_file = Path(tmpdir) / "COMMIT_MSG"
            msg_file.write_text("feat: test\n\nCo-authored-by: Claude <bot@ai>\n", encoding="utf-8")
            res_file = run_hook(
                GIT_GUARD,
                {
                    "toolCall": {
                        "name": "run_command",
                        "args": {"CommandLine": f"git commit -F {msg_file}", "Cwd": tmpdir},
                    }
                },
            )
            self.assertEqual(res_file["decision"], "deny")

    def test_allows_push_to_feature_branch_without_confirmation(self) -> None:
        res = run_hook(
            GIT_GUARD,
            {
                "toolCall": {
                    "name": "run_command",
                    "args": {"CommandLine": "git push -u origin feat/hybrid-hard-loop"},
                }
            },
        )
        self.assertEqual(res["decision"], "allow")

    def test_allows_dry_run_push_to_main(self) -> None:
        res = run_hook(
            GIT_GUARD,
            {
                "toolCall": {
                    "name": "run_command",
                    "args": {"CommandLine": "git push --dry-run origin main"},
                }
            },
        )
        self.assertEqual(res["decision"], "allow")

    def test_push_to_main_needs_an_unconsumed_push_question(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            transcript = Path(tmpdir) / "transcript.jsonl"
            # 1. Transcript with NO ask_question -> deny
            transcript.write_text(
                json.dumps({"step_index": 1, "type": "USER_INPUT", "tool_calls": []}) + "\n",
                encoding="utf-8",
            )
            res = run_hook(
                GIT_GUARD,
                {
                    "transcriptPath": str(transcript),
                    "toolCall": {
                        "name": "run_command",
                        "args": {"CommandLine": "git push -u origin main"},
                    },
                },
            )
            self.assertEqual(res["decision"], "deny")
            self.assertIn("ask_question", res.get("reason", ""))

            # 2. Transcript WITH ask_question -> allow
            with transcript.open("a", encoding="utf-8") as f:
                f.write(
                    json.dumps(
                        {
                            "step_index": 2,
                            "type": "PLANNER_RESPONSE",
                            "tool_calls": [
                                {
                                    "name": "ask_question",
                                    "args": {
                                        "questions": [
                                            {
                                                "question": "Should we push directly to main or open a PR?",
                                                "options": [
                                                    "Push directly to main",
                                                    "Push to a feature branch",
                                                ],
                                            }
                                        ]
                                    },
                                }
                            ],
                        }
                    )
                    + "\n"
                )
            res2 = run_hook(
                GIT_GUARD,
                {
                    "transcriptPath": str(transcript),
                    "toolCall": {
                        "name": "run_command",
                        "args": {"CommandLine": "git push -u origin main"},
                    },
                },
            )
            self.assertEqual(res2["decision"], "allow")

            # 3. After a git push to main is recorded, the previous ask_question is consumed -> deny next push
            with transcript.open("a", encoding="utf-8") as f:
                f.write(
                    json.dumps(
                        {
                            "step_index": 3,
                            "type": "PLANNER_RESPONSE",
                            "tool_calls": [
                                {
                                    "name": "run_command",
                                    "args": {"CommandLine": '"git push -u origin main"'},
                                }
                            ],
                        }
                    )
                    + "\n"
                )
            res3 = run_hook(
                GIT_GUARD,
                {
                    "transcriptPath": str(transcript),
                    "toolCall": {
                        "name": "run_command",
                        "args": {"CommandLine": "git push origin main"},
                    },
                },
            )
            self.assertEqual(res3["decision"], "deny")

    def test_force_push_to_main_always_triggers_force_ask(self) -> None:
        res = run_hook(
            GIT_GUARD,
            {
                "toolCall": {
                    "name": "run_command",
                    "args": {"CommandLine": "git push --force origin main"},
                }
            },
        )
        self.assertEqual(res["decision"], "force_ask")

    def test_claude_code_bash_payload_is_enforced_by_git_guard(self) -> None:
        res = run_hook(
            GIT_GUARD,
            {
                "tool_name": "Bash",
                "tool_input": {
                    "command": 'git commit -m "feat: x\n\nCo-authored-by: Claude <noreply@anthropic.com>"'
                },
            },
        )
        self.assertEqual(res["decision"], "deny")
        self.assertIn("Global Rule #2", res.get("reason", ""))

    def test_commit_msg_hook_blocks_ai_trailers_outside_comments(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            msg_file = Path(tmpdir) / "COMMIT_EDITMSG"

            # Clean message with commented-out trailer -> exit 0
            msg_file.write_text(
                "feat(core): add pipeline\n# Co-authored-by: Claude <noreply@anthropic.com>\n",
                encoding="utf-8",
            )
            ok_proc = subprocess.run(
                [sys.executable, str(GIT_GUARD), "--git-hook", "commit-msg", str(msg_file)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(ok_proc.returncode, 0)

            # Active AI trailer -> exit 1
            msg_file.write_text(
                "feat(core): add pipeline\n\nCo-authored-by: GitHub Copilot <copilot@github.com>\n",
                encoding="utf-8",
            )
            bad_proc = subprocess.run(
                [sys.executable, str(GIT_GUARD), "--git-hook", "commit-msg", str(msg_file)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(bad_proc.returncode, 1)
            self.assertIn("Global Rule #2", bad_proc.stderr)

    def test_native_pre_push_hook_enforces_main_protection(self) -> None:
        import os

        env = dict(os.environ, CRAFTSMAN_NONINTERACTIVE="1", CRAFTSMAN_IGNORE_TRANSCRIPT="1")
        env.pop("CRAFTSMAN_ALLOW_MAIN_PUSH", None)

        # Feature branch push -> exit 0
        feat_proc = subprocess.run(
            [sys.executable, str(GIT_GUARD), "--git-hook", "pre-push", "origin", "https://example.com"],
            input="refs/heads/feat/x 1111 refs/heads/feat/x 0000\n",
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        self.assertEqual(feat_proc.returncode, 0)

        # Unconfirmed push to main -> exit 1
        main_proc = subprocess.run(
            [sys.executable, str(GIT_GUARD), "--git-hook", "pre-push", "origin", "https://example.com"],
            input="refs/heads/main 1111 refs/heads/main 0000\n",
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        self.assertEqual(main_proc.returncode, 1)
        self.assertIn("Global Rule #1", main_proc.stderr)

        # Confirmed push to main via CRAFTSMAN_ALLOW_MAIN_PUSH=1 -> exit 0
        env_allowed = dict(env, CRAFTSMAN_ALLOW_MAIN_PUSH="1")
        allowed_proc = subprocess.run(
            [sys.executable, str(GIT_GUARD), "--git-hook", "pre-push", "origin", "https://example.com"],
            input="refs/heads/main 1111 refs/heads/main 0000\n",
            capture_output=True,
            text=True,
            env=env_allowed,
            check=False,
        )
        self.assertEqual(allowed_proc.returncode, 0)

    def test_failed_push_in_compound_command_does_not_consume_ask_question(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            transcript = Path(tmpdir) / "transcript.jsonl"
            entries = [
                {
                    "step_index": 1,
                    "type": "PLANNER_RESPONSE",
                    "tool_calls": [
                        {
                            "name": "ask_question",
                            "args": {
                                "questions": [
                                    {
                                        "question": "Push directly to main?",
                                        "options": ["Push directly to main", "Feature branch"],
                                    }
                                ]
                            },
                        }
                    ],
                },
                {
                    "step_index": 2,
                    "type": "GENERIC",
                    "status": "DONE",
                    "content": "Q1: Push directly to main?\nA1: Push directly to main",
                },
                {
                    "step_index": 3,
                    "type": "PLANNER_RESPONSE",
                    "status": "DONE",
                    "tool_calls": [
                        {"name": "view_file", "args": {"AbsolutePath": "/tmp/a"}},
                        {
                            "name": "run_command",
                            "args": {"CommandLine": "git status --short && git push origin main"},
                        },
                    ],
                },
                {
                    "step_index": 3,
                    "type": "GENERIC",
                    "status": "DONE",
                    "content": "File contents...",
                },
                {
                    "step_index": 4,
                    "type": "GENERIC",
                    "status": "DONE",
                    "content": "The command exited with code 128.\nOutput:\nfatal: The current branch main has no upstream branch.",
                },
            ]
            transcript.write_text(
                "\n".join(json.dumps(e) for e in entries) + "\n",
                encoding="utf-8",
            )
            res = run_hook(
                GIT_GUARD,
                {
                    "transcriptPath": str(transcript),
                    "toolCall": {
                        "name": "run_command",
                        "args": {"CommandLine": "git push -u origin main"},
                    },
                },
            )
            self.assertEqual(res["decision"], "allow")

    def test_isolated_cd_does_not_bypass_push_to_main_guard(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_dir = Path(tmpdir) / "repo"
            other_dir = Path(tmpdir) / "other"
            repo_dir.mkdir()
            other_dir.mkdir()
            subprocess.run(["git", "init", "-b", "main"], cwd=repo_dir, capture_output=True, check=True)
            subprocess.run(
                ["git", "config", "user.email", "dev@example.com"],
                cwd=repo_dir,
                capture_output=True,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Dev"],
                cwd=repo_dir,
                capture_output=True,
                check=True,
            )
            subprocess.run(
                ["git", "commit", "--allow-empty", "--no-verify", "-m", "init"],
                cwd=repo_dir,
                capture_output=True,
                check=True,
            )
            for cmd in (
                "(cd /tmp && ls) && git push",
                "(cd ~ && ls) && git push origin HEAD",
                f"pushd {other_dir}; popd; git push",
                "echo `cd /tmp`; git push",
                "cd /tmp | true; git push",
                "cd /tmp & git push",
                "f() { cd /tmp; }; git push",
                "(case x in a) cd /tmp;; esac); git push",
            ):
                with self.subTest(cmd=cmd):
                    res = run_hook(
                        GIT_GUARD,
                        {
                            "toolCall": {
                                "name": "run_command",
                                "args": {"CommandLine": cmd, "Cwd": str(repo_dir)},
                            },
                        },
                    )
                    self.assertEqual(res["decision"], "force_ask", cmd)

    def test_wrapped_pushes_to_main_are_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            transcript = Path(tmpdir) / "transcript.jsonl"
            transcript.write_text(
                json.dumps({"step_index": 1, "type": "USER_INPUT", "tool_calls": []}) + "\n",
                encoding="utf-8",
            )
            wrapped_commands = (
                "bash -c 'git push origin main'",
                "sh -lc 'git push origin master'",
                "timeout 30 git push origin main",
                "timeout -k 5 30 git push origin main",
                "xargs git push origin <<< main",
                "eval git push origin main",
                "git -c alias.p=push p origin main",
                "gh pr merge 1 --admin --merge",
            )
            for cmd in wrapped_commands:
                res = run_hook(
                    GIT_GUARD,
                    {
                        "transcriptPath": str(transcript),
                        "toolCall": {
                            "name": "run_command",
                            "args": {"CommandLine": cmd, "Cwd": str(REPO_ROOT)},
                        },
                    },
                )
                self.assertEqual(res["decision"], "deny", f"Expected deny for wrapper: {cmd}")

    def test_only_a_push_question_approves_a_push(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            transcript = Path(tmpdir) / "transcript.jsonl"
            # Unrelated design question must NOT unlock push to main
            transcript.write_text(
                json.dumps(
                    {
                        "step_index": 1,
                        "type": "PLANNER_RESPONSE",
                        "tool_calls": [
                            {
                                "name": "ask_question",
                                "args": {
                                    "questions": [
                                        {
                                            "question": "Which naming convention do you prefer?",
                                            "options": ["snake_case", "camelCase"],
                                        }
                                    ]
                                },
                            }
                        ],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            res_deny = run_hook(
                GIT_GUARD,
                {
                    "transcriptPath": str(transcript),
                    "toolCall": {
                        "name": "run_command",
                        "args": {"CommandLine": "git push origin main"},
                    },
                },
            )
            self.assertEqual(res_deny["decision"], "deny")

            # Push / branch confirmation question unlocks push to main
            with transcript.open("a", encoding="utf-8") as f:
                f.write(
                    json.dumps(
                        {
                            "step_index": 2,
                            "type": "PLANNER_RESPONSE",
                            "tool_calls": [
                                {
                                    "name": "ask_question",
                                    "args": {
                                        "questions": [
                                            {
                                                "question": "How should we push these changes?",
                                                "options": [
                                                    "Push to a feature branch for a PR",
                                                    "Push directly to main",
                                                ],
                                            }
                                        ]
                                    },
                                }
                            ],
                        }
                    )
                    + "\n"
                )
            res_allow = run_hook(
                GIT_GUARD,
                {
                    "transcriptPath": str(transcript),
                    "toolCall": {
                        "name": "run_command",
                        "args": {"CommandLine": "git push origin main"},
                    },
                },
            )
            self.assertEqual(res_allow["decision"], "allow")

    def test_claude_ask_user_question_approves_a_main_push(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            transcript = Path(tmpdir) / "transcript.jsonl"
            transcript.write_text(
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {
                            "content": [
                                {
                                    "type": "tool_use",
                                    "name": "AskUserQuestion",
                                    "input": {
                                        "question": "Should we push directly to main or open a PR?"
                                    },
                                }
                            ]
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            res = run_hook(
                GIT_GUARD,
                {
                    "transcript_path": str(transcript),
                    "tool_name": "Bash",
                    "tool_input": {"command": "git push origin main"},
                },
            )
            self.assertEqual(res["decision"], "allow")

    def test_ai_trailers_in_any_message_form_are_blocked(
        self,
    ) -> None:
        from craftsman.guard import run_native_commit_msg_hook

        blocked_commands = (
            "git commit -m $'feat: x\\n\\nCo-authored-by: Claude <noreply@anthropic.com>'",
            "cat <<'EOF' | git commit -F -\nfeat: x\n\nCo-authored-by: Claude <noreply@anthropic.com>\nEOF",
            'git commit --trailer "Co-authored-by: Claude <noreply@anthropic.com>" -m "feat: x"',
            'gh pr create --title "feat: x" --body "Generated with [Claude Code](https://claude.com/claude-code)"',
            'git commit -m "Generated by Gemini"',
        )
        for cmd in blocked_commands:
            res = run_hook(
                GIT_GUARD,
                {
                    "toolCall": {
                        "name": "run_command",
                        "args": {"CommandLine": cmd},
                    }
                },
            )
            self.assertEqual(res["decision"], "deny", f"Expected deny for: {cmd}")

        # Prose subject discussing "Generated by" must be allowed
        allow_res = run_hook(
            GIT_GUARD,
            {
                "toolCall": {
                    "name": "run_command",
                    "args": {
                        "CommandLine": 'git commit -m "fix(guard): block Generated by AI trailers"'
                    },
                }
            },
        )
        self.assertEqual(allow_res["decision"], "allow")

        # Native commit-msg hook must strip diff below scissors line (# --- >8 ---)
        with tempfile.TemporaryDirectory() as tmpdir:
            msg_file = Path(tmpdir) / "COMMIT_EDITMSG"
            msg_file.write_text(
                "fix(guard): update attribution regex\n\n"
                "# ------------------------ >8 ------------------------\n"
                "# Do not modify or remove the line above.\n"
                "diff --git a/guard.py b/guard.py\n"
                "+Co-authored-by: Claude <noreply@anthropic.com>\n"
                "+Generated with Claude Code\n",
                encoding="utf-8",
            )
            self.assertEqual(run_native_commit_msg_hook(str(msg_file)), 0)

    def test_invalid_or_stale_push_answers_approve_nothing(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            transcript = Path(tmpdir) / "transcript.jsonl"

            # 1. Empty args {} must be rejected
            transcript.write_text(
                json.dumps(
                    {
                        "step_index": 1,
                        "type": "PLANNER_RESPONSE",
                        "tool_calls": [{"name": "ask_question", "args": {}}],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            res_empty = run_hook(
                GIT_GUARD,
                {
                    "transcriptPath": str(transcript),
                    "toolCall": {
                        "name": "run_command",
                        "args": {"CommandLine": "git push origin main"},
                    },
                },
            )
            self.assertEqual(res_empty["decision"], "deny")

            # 2. User choosing "Feature branch + PR" must deny direct push to main
            entries_feature_choice = [
                {
                    "step_index": 1,
                    "type": "PLANNER_RESPONSE",
                    "tool_calls": [
                        {
                            "name": "ask_question",
                            "args": {
                                "questions": [
                                    {
                                        "question": "Should we push directly to main or open a PR?",
                                        "options": [
                                            "Push to a feature branch for a PR",
                                            "Push directly to main",
                                        ],
                                    }
                                ]
                            },
                        }
                    ],
                },
                {
                    "step_index": 2,
                    "type": "GENERIC",
                    "status": "DONE",
                    "content": "Push to a feature branch for a PR",
                },
            ]
            transcript.write_text(
                "\n".join(json.dumps(e) for e in entries_feature_choice) + "\n",
                encoding="utf-8",
            )
            res_declined = run_hook(
                GIT_GUARD,
                {
                    "transcriptPath": str(transcript),
                    "toolCall": {
                        "name": "run_command",
                        "args": {"CommandLine": "git push origin main"},
                    },
                },
            )
            self.assertEqual(res_declined["decision"], "deny")

            # 3. User choosing "Push directly to main" must allow push to main
            entries_feature_choice[1]["content"] = "Push directly to main"
            transcript.write_text(
                "\n".join(json.dumps(e) for e in entries_feature_choice) + "\n",
                encoding="utf-8",
            )
            res_approved = run_hook(
                GIT_GUARD,
                {
                    "transcriptPath": str(transcript),
                    "toolCall": {
                        "name": "run_command",
                        "args": {"CommandLine": "git push origin main"},
                    },
                },
            )
            self.assertEqual(res_approved["decision"], "allow")

    def test_push_to_refs_heads_main_after_options_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            transcript = Path(tmpdir) / "transcript.jsonl"
            transcript.write_text(
                json.dumps({"step_index": 1, "type": "USER_INPUT"}) + "\n",
                encoding="utf-8",
            )
            for cmd in (
                "git push -o -n origin main",
                "git push --push-option -n origin main",
                "git push origin HEAD:heads/main",
                "git push origin @:refs/heads/main",
                "git push origin main>/dev/null",
            ):
                res = run_hook(
                    GIT_GUARD,
                    {
                        "transcriptPath": str(transcript),
                        "toolCall": {
                            "name": "run_command",
                            "args": {"CommandLine": cmd},
                        },
                    },
                )
                self.assertEqual(res["decision"], "deny", f"Expected deny for: {cmd}")

    def test_human_co_authors_pass_where_ai_trailers_fail(
        self,
    ) -> None:
        # Human co-author trailer must be allowed
        human_res = run_hook(
            GIT_GUARD,
            {
                "toolCall": {
                    "name": "run_command",
                    "args": {
                        "CommandLine": 'git commit -m "feat: pair work\n\nCo-authored-by: Jan Kowalski <jan@kowalski.pl>"'
                    },
                }
            },
        )
        self.assertEqual(human_res["decision"], "allow")

        # AI trailers with '=' or Assisted-by or gh pr merge --body must be denied
        for cmd in (
            'git commit -m "feat: x" --trailer "Co-authored-by=Claude"',
            'git commit -m "feat: x\n\nAssisted-by: Claude"',
            'gh pr merge 1 --squash --body "Co-authored-by: Claude <noreply@anthropic.com>"',
            'gh pr comment 1 --body "Generated with [Claude Code](https://claude.com)"',
        ):
            res = run_hook(
                GIT_GUARD,
                {"toolCall": {"name": "run_command", "args": {"CommandLine": cmd}}},
            )
            self.assertEqual(res["decision"], "deny", f"Expected deny for: {cmd}")

    def test_refusals_mentioning_main_approve_no_push(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            transcript = Path(tmpdir) / "transcript.jsonl"
            q_entry = {
                "step_index": 1,
                "type": "PLANNER_RESPONSE",
                "tool_calls": [
                    {
                        "name": "ask_question",
                        "args": {
                            "questions": json.dumps(
                                [
                                    {
                                        "question": "How should I push the changes?",
                                        "options": [
                                            "(Recommended) Feature branch + PR to main",
                                            "Push directly to main",
                                        ],
                                    }
                                ]
                            )
                        },
                    }
                ],
            }
            for refusal_text in (
                "A1: (Recommended) Feature branch + PR to main",
                "A1: No, do not push to main",
                "A1: Nie wypychaj na main, zrób PR",
                "A1: Open PR, then squash merge",
            ):
                transcript.write_text(
                    json.dumps(q_entry)
                    + "\n"
                    + json.dumps({"step_index": 2, "type": "GENERIC", "content": refusal_text})
                    + "\n",
                    encoding="utf-8",
                )
                res = run_hook(
                    GIT_GUARD,
                    {
                        "transcriptPath": str(transcript),
                        "toolCall": {
                            "name": "run_command",
                            "args": {"CommandLine": "git push origin main"},
                        },
                    },
                )
                self.assertEqual(res["decision"], "deny", f"Expected deny for refusal: {refusal_text}")

            # Question asking only about feature branch + PR must not unlock push to main
            pr_only_q = {
                "step_index": 1,
                "type": "PLANNER_RESPONSE",
                "tool_calls": [
                    {
                        "name": "ask_question",
                        "args": {
                            "questions": [
                                {
                                    "question": "Should I push to a feature branch and open a PR?",
                                    "options": ["Yes", "No"],
                                }
                            ]
                        },
                    }
                ],
            }
            transcript.write_text(
                json.dumps(pr_only_q)
                + "\n"
                + json.dumps({"step_index": 2, "type": "GENERIC", "content": "A1: Yes"})
                + "\n",
                encoding="utf-8",
            )
            res_pr_q = run_hook(
                GIT_GUARD,
                {
                    "transcriptPath": str(transcript),
                    "toolCall": {
                        "name": "run_command",
                        "args": {"CommandLine": "git push origin main"},
                    },
                },
            )
            self.assertEqual(res_pr_q["decision"], "deny")


if __name__ == "__main__":
    unittest.main()
