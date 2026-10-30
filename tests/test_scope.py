"""Tests for repository search-scope enforcement (`craftsman/scope.py` and `pre-tool` hook)."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any
from unittest import mock

from craftsman import hooks
from craftsman.gate import GateOwner
from craftsman.scope import (
    ScopeConfigError,
    ScopePolicy,
    evaluate_antigravity_scope,
    evaluate_scope,
    is_craftsman_agent,
    load_scope_policy,
)
from craftsman.scope import main as scope_guard_main

from tests.support import REPO_ROOT, open_gate

# The test repositories have no commit; the scope checks never read the gate's base.
UNBORN_BASE = "0" * 40


def call_pre_tool(platform: str, payload: dict[str, Any]) -> tuple[dict[str, Any], str]:
    stdin = io.StringIO(json.dumps(payload))
    out, err = io.StringIO(), io.StringIO()
    with mock.patch("sys.stdin", stdin), redirect_stdout(out), redirect_stderr(err):
        code = hooks.main(["pre-tool", "--platform", platform])
    assert code == 0, err.getvalue()
    return json.loads(out.getvalue()), err.getvalue()


class ScopeEnforcementTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.home = self.tmp / "home"
        self.home.mkdir(parents=True)

        self.repo = self.tmp / "workspace" / "my-repo"
        self.repo.mkdir(parents=True)
        subprocess.run(["git", "init"], cwd=self.repo, capture_output=True, check=True)
        (self.repo / ".git" / "craftsman").mkdir(parents=True, exist_ok=True)
        (self.repo / ".git" / "craftsman" / "packet-1.md").write_text("# Packet 1\n", encoding="utf-8")
        (self.repo / "src").mkdir()
        (self.repo / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")

        self.other_repo = self.tmp / "workspace" / "other-repo"
        self.other_repo.mkdir(parents=True)
        subprocess.run(["git", "init"], cwd=self.other_repo, capture_output=True, check=True)
        (self.other_repo / ".git" / "craftsman").mkdir(parents=True, exist_ok=True)
        (self.other_repo / ".git" / "craftsman" / "packet-1.md").write_text(
            "# Other Packet\n", encoding="utf-8"
        )
        (self.other_repo / "secret.py").write_text("y = 2\n", encoding="utf-8")

        self.venv_site = self.tmp / "venvs" / "env1" / "lib" / "site-packages"
        self.venv_site.mkdir(parents=True)
        (self.venv_site / "a.py").write_text("# dependency\n", encoding="utf-8")

        self.scratch_tmp = self.tmp / "task-scratch"
        self.scratch_tmp.mkdir(parents=True)
        (self.scratch_tmp / "x.txt").write_text("scratch\n", encoding="utf-8")

        for rel in (
            ".copilot/session-state",
            ".claude/projects/slug",
            ".gemini/antigravity/brain/conv-1",
            ".gemini/antigravity/conversations",
        ):
            store_dir = self.home / rel
            store_dir.mkdir(parents=True)
            (store_dir / "session.jsonl").write_text("{}\n", encoding="utf-8")

        self.outside_dir = self.tmp / "outside_target"
        self.outside_dir.mkdir()
        (self.outside_dir / "leak.txt").write_text("leak\n", encoding="utf-8")
        (self.repo / "escape_link").symlink_to(self.outside_dir)

        self.env = {
            **os.environ,
            "HOME": str(self.home),
            "USERPROFILE": str(self.home),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": str(self.home / ".gitconfig"),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
        }
        self._env_patcher = mock.patch.dict(os.environ, self.env, clear=True)
        self._env_patcher.start()
        self.addCleanup(self._env_patcher.stop)

        self.policy = ScopePolicy(mode="deny", allowed_dirs=())

    def test_evaluate_scope_enforces_worktree_boundaries(self) -> None:
        own_pkt = str(self.repo / ".git" / "craftsman" / "packet-1.md")
        other_pkt = str(self.other_repo / ".git" / "craftsman" / "packet-1.md")
        dep_file = str(self.venv_site / "a.py")
        tmp_file = str(self.scratch_tmp / "x.txt")

        cases: list[tuple[str, str, dict[str, Any], str]] = [
            ("recursive find in home is denied", "Bash", {"command": "find ~ -name SKILL.md"}, "deny"),
            (
                "recursive find in current directory is allowed",
                "Bash",
                {"command": "find . -name x"},
                "allow",
            ),
            (
                "recursive find in repo subdirectory is allowed",
                "Bash",
                {"command": "find src -name '*.py'"},
                "allow",
            ),
            (
                "recursive find from filesystem root is denied",
                "Bash",
                {"command": "find / -name x"},
                "deny",
            ),
            (
                "recursive grep in sibling repository is denied",
                "Bash",
                {"command": f"grep -r foo {self.other_repo}"},
                "deny",
            ),
            (
                "non-recursive grep on dependency file is allowed",
                "Bash",
                {"command": f"grep foo {dep_file}"},
                "allow",
            ),
            (
                "non-recursive grep in sibling repository is denied",
                "Bash",
                {"command": "grep foo ../other-repo/secret.py"},
                "deny",
            ),
            ("rg in default working directory is allowed", "Bash", {"command": "rg foo"}, "allow"),
            ("rg in repo subdirectory is allowed", "Bash", {"command": "rg foo src"}, "allow"),
            ("rg in parent directory is denied", "Bash", {"command": "rg foo .."}, "deny"),
            ("rg in sibling repository is denied", "Bash", {"command": "rg foo ../other-repo"}, "deny"),
            ("rg files listing in home is denied", "Bash", {"command": "rg --files ~"}, "deny"),
            (
                "rg with attached pattern flag in home is denied",
                "Bash",
                {"command": "rg -efoo ~"},
                "deny",
            ),
            (
                "ag in home via environment variable is denied",
                "Bash",
                {"command": "ag pattern $HOME"},
                "deny",
            ),
            ("ag filename search in home is denied", "Bash", {"command": "ag -g SKILL ~"}, "deny"),
            ("ack file listing in home is denied", "Bash", {"command": "ack -f ~"}, "deny"),
            ("ack inside repo subdirectory is allowed", "Bash", {"command": "ack pattern src"}, "allow"),
            ("fd in home is denied", "Bash", {"command": "fd SKILL.md ~"}, "deny"),
            (
                "fd with search-path in parent is denied",
                "Bash",
                {"command": "fd --search-path .. foo"},
                "deny",
            ),
            ("fd inside repo subdirectory is allowed", "Bash", {"command": "fd app.py src"}, "allow"),
            (
                "non-recursive ls in copilot skills is allowed",
                "Bash",
                {"command": "ls ~/.copilot/skills"},
                "allow",
            ),
            ("non-recursive ls in home is allowed", "Bash", {"command": "ls ~"}, "allow"),
            (
                "non-recursive ls in sibling repository is denied",
                "Bash",
                {"command": "ls ../other-repo"},
                "deny",
            ),
            ("recursive ls in home is denied", "Bash", {"command": "ls -R ~"}, "deny"),
            (
                "recursive ls with combined flags in parent is denied",
                "Bash",
                {"command": "ls -laR .."},
                "deny",
            ),
            ("tree in home is denied", "Bash", {"command": "tree ~"}, "deny"),
            ("tree in current directory is allowed", "Bash", {"command": "tree ."}, "allow"),
            ("du in parent directory is denied", "Bash", {"command": "du -sh .."}, "deny"),
            ("locate global index query is denied", "Bash", {"command": "locate SKILL.md"}, "deny"),
            ("mdfind without onlyin is denied", "Bash", {"command": "mdfind SKILL.md"}, "deny"),
            (
                "mdfind scoped to repository is allowed",
                "Bash",
                {"command": f"mdfind -onlyin {self.repo} app"},
                "allow",
            ),
            ("mdfind scoped to home is denied", "Bash", {"command": "mdfind -onlyin ~ app"}, "deny"),
            (
                "cat of sibling repository packet is denied",
                "Bash",
                {"command": "cat ../other-repo/.git/craftsman/packet-1.md"},
                "deny",
            ),
            (
                "cat of absolute sibling packet path is denied",
                "Bash",
                {"command": f"cat {other_pkt}"},
                "deny",
            ),
            ("cat of own repository packet is allowed", "Bash", {"command": f"cat {own_pkt}"}, "allow"),
            (
                "cat of temporary scratch file is allowed",
                "Bash",
                {"command": f"cat {tmp_file}"},
                "allow",
            ),
            (
                "cat of virtualenv site-packages file is allowed",
                "Bash",
                {"command": f"cat {dep_file}"},
                "allow",
            ),
            (
                "head of claude session store is denied",
                "Bash",
                {"command": "head -n 20 ~/.claude/projects/slug/session.jsonl"},
                "deny",
            ),
            ("cat of shell history file is denied", "Bash", {"command": "cat ~/.zsh_history"}, "deny"),
            (
                "sed reading sibling repository file is denied",
                "Bash",
                {"command": "sed -n '1,20p' ../other-repo/secret.py"},
                "deny",
            ),
            (
                "env wrapper invoking bash -c find on root is denied",
                "Bash",
                {"command": 'env FOO=1 bash -c "find / -name x"'},
                "deny",
            ),
            (
                "env -C parent directory with rg is denied",
                "Bash",
                {"command": "env -C .. rg foo"},
                "deny",
            ),
            (
                "sudo -D sibling repository with cat is denied",
                "Bash",
                {"command": "sudo -D ../other-repo cat secret.py"},
                "deny",
            ),
            (
                "find -exec cat on sibling repository is denied",
                "Bash",
                {"command": "find . -exec cat ../other-repo/secret.py \\;"},
                "deny",
            ),
            (
                "fd -x cat on sibling repository is denied",
                "Bash",
                {"command": "fd . -x cat ../other-repo/secret.py"},
                "deny",
            ),
            (
                "git -C sibling repository grep is denied",
                "Bash",
                {"command": "git -C ../other-repo grep foo"},
                "deny",
            ),
            (
                "git grep --no-index in home is denied",
                "Bash",
                {"command": "git grep --no-index foo ~"},
                "deny",
            ),
            ("git grep inside repository is allowed", "Bash", {"command": "git grep foo"}, "allow"),
            (
                "cd to home followed by recursive grep is denied",
                "Bash",
                {"command": "cd ~ && grep -r x ."},
                "deny",
            ),
            (
                "cd to repo subdirectory followed by rg is allowed",
                "Bash",
                {"command": "cd src && rg x ."},
                "allow",
            ),
            (
                "cd through escaping symlink followed by cat is denied",
                "Bash",
                {"command": "cd escape_link && cat leak.txt"},
                "deny",
            ),
            (
                "tool cwd on escaping symlink followed by cat is denied",
                "Bash",
                {"command": "cat leak.txt", "cwd": "escape_link"},
                "deny",
            ),
            (
                "powershell Set-Location to home before rg is denied",
                "powershell",
                {"command": "Set-Location $env:USERPROFILE; rg foo"},
                "deny",
            ),
            (
                "powershell recursive Get-ChildItem on drive root is denied",
                "powershell",
                {"command": "Get-ChildItem -Recurse C:\\"},
                "deny",
            ),
            (
                "powershell recursive Get-ChildItem on C:\\Users is denied",
                "powershell",
                {"command": r"Get-ChildItem -Path C:\Users -Recurse"},
                "deny",
            ),
            (
                "powershell recursive gci in current directory is allowed",
                "powershell",
                {"command": "gci -r ."},
                "allow",
            ),
            (
                "cmd dir /s on drive root is denied",
                "run_in_terminal",
                {"command": "dir /s C:\\"},
                "deny",
            ),
            (
                "find on escaping symlink inside repo is denied",
                "Bash",
                {"command": "find escape_link -name '*'"},
                "deny",
            ),
            (
                "Read tool on escaping symlink inside repo is denied",
                "Read",
                {"file_path": str(self.repo / "escape_link" / "leak.txt")},
                "deny",
            ),
            (
                "Read tool on sibling repository file is denied",
                "Read",
                {"file_path": str(self.other_repo / "secret.py")},
                "deny",
            ),
            (
                "Read tool on own repository file is allowed",
                "Read",
                {"file_path": str(self.repo / "src" / "app.py")},
                "allow",
            ),
            ("Read tool on dependency file is allowed", "Read", {"file_path": dep_file}, "allow"),
            ("Grep tool in home directory is denied", "Grep", {"pattern": "foo", "path": "~"}, "deny"),
            (
                "Grep tool in repository is allowed",
                "Grep",
                {"pattern": "foo", "path": str(self.repo)},
                "allow",
            ),
            ("LS tool in home directory is denied", "LS", {"path": "~"}, "deny"),
            ("list_dir tool in parent directory is denied", "list_dir", {"DirectoryPath": ".."}, "deny"),
            (
                "Glob tool in sibling repository is denied",
                "Glob",
                {"pattern": f"{self.other_repo}/**/*.py"},
                "deny",
            ),
            (
                "Glob tool escaping via relative parent is denied",
                "Glob",
                {"pattern": "./../../other-repo/**/*.py"},
                "deny",
            ),
            (
                "Glob tool on escaping symlink is denied",
                "Glob",
                {"pattern": "escape_link/**/*.py"},
                "deny",
            ),
            ("Glob tool inside repository is allowed", "Glob", {"pattern": "**/*.py"}, "allow"),
            (
                "Glob tool inside repo subdirectory is allowed",
                "Glob",
                {"pattern": "src/**/*.py"},
                "allow",
            ),
            (
                "read_file on claude session store is denied",
                "read_file",
                {"filePath": str(self.home / ".claude" / "projects" / "slug" / "session.jsonl")},
                "deny",
            ),
            (
                "read_file on copilot session store is denied",
                "read_file",
                {"filePath": str(self.home / ".copilot" / "session-state" / "session.jsonl")},
                "deny",
            ),
            (
                "read_file on antigravity brain store is denied",
                "read_file",
                {
                    "filePath": str(
                        self.home / ".gemini" / "antigravity" / "brain" / "conv-1" / "session.jsonl"
                    )
                },
                "deny",
            ),
            (
                "grep_search with includePattern outside repo is denied",
                "grep_search",
                {"query": "foo", "includePattern": "../other-repo/**"},
                "deny",
            ),
            (
                "file_search scoped to repo subdirectory is allowed",
                "file_search",
                {"query": "app.py", "folder": str(self.repo / "src")},
                "allow",
            ),
        ]
        for description, tool, tool_input, expected in cases:
            with self.subTest(case=description):
                res = evaluate_scope(tool, tool_input, self.repo, self.repo, self.policy)
                self.assertEqual(
                    res.decision,
                    expected,
                    f"Case {description!r} ({tool} {tool_input}) returned {res}",
                )
                if expected == "deny":
                    self.assertIn("[CRAFTSMAN SCOPE-GUARD]", res.reason)
                    self.assertIn("git config craftsman.scopeAllow", res.reason)

    def test_scope_allow_config_unlocks_external_directories(self) -> None:
        extra_dir = self.tmp / "shared-contracts"
        extra_dir.mkdir()
        subprocess.run(
            ["git", "config", "--add", "craftsman.scopeAllow", f"{self.other_repo}:{extra_dir}"],
            cwd=self.repo,
            check=True,
        )
        policy = load_scope_policy(self.repo, self.repo)
        self.assertIn(self.other_repo.resolve(), policy.allowed_dirs)
        self.assertIn(extra_dir.resolve(), policy.allowed_dirs)
        self.assertEqual(
            evaluate_scope(
                "Read",
                {"file_path": str(self.other_repo / "secret.py")},
                self.repo,
                self.repo,
                policy,
            ).decision,
            "allow",
        )
        self.assertEqual(
            evaluate_scope(
                "Bash",
                {"command": f"rg foo {self.other_repo}"},
                self.repo,
                self.repo,
                policy,
            ).decision,
            "allow",
        )

    def test_unmatched_quotes_in_shell_command_are_denied(self) -> None:
        err = io.StringIO()
        with redirect_stderr(err):
            decision = evaluate_scope(
                "Bash",
                {"command": 'find "unterminated quote'},
                self.repo,
                self.repo,
                self.policy,
            )
        self.assertEqual(decision.decision, "deny")
        self.assertIn("blocking unparseable shell command", err.getvalue())

    def test_pre_tool_allows_outside_commands_when_gate_is_inactive(self) -> None:
        payload = {
            "cwd": str(self.repo),
            "toolName": "run_in_terminal",
            "toolArgs": json.dumps({"command": "find ~ -name SKILL.md"}),
            "agentName": "general-helper",
        }
        out, _ = call_pre_tool("copilot", payload)
        self.assertEqual(out, {})

    def test_pre_tool_enforces_scope_for_craftsman_reviewer_without_gate(self) -> None:
        payload = {
            "cwd": str(self.repo),
            "toolName": "run_in_terminal",
            "toolArgs": json.dumps({"command": "find ~ -name SKILL.md"}),
            "agentType": "custom",
            "agentName": "craftsman-reviewer",
        }
        out, _ = call_pre_tool("copilot", payload)
        self.assertEqual(out["permissionDecision"], "deny")
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_pre_tool_enforces_scope_when_gate_is_active(self) -> None:
        open_gate(self.repo, UNBORN_BASE)
        claude_read = {
            "hook_event_name": "PreToolUse",
            "cwd": str(self.repo),
            "tool_name": "Read",
            "tool_input": {"file_path": str(self.other_repo / "secret.py")},
        }
        out, _ = call_pre_tool("claude", claude_read)
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_pre_tool_emits_warning_when_scope_mode_is_warn(self) -> None:
        open_gate(self.repo, UNBORN_BASE)
        subprocess.run(["git", "config", "craftsman.scope", "warn"], cwd=self.repo, check=True)
        payload = {
            "cwd": str(self.repo),
            "toolName": "run_in_terminal",
            "toolArgs": json.dumps({"command": "find ~ -name SKILL.md"}),
        }
        out, _ = call_pre_tool("copilot", payload)
        self.assertNotIn("permissionDecision", out)
        self.assertIn("[CRAFTSMAN SCOPE-GUARD]", out["systemMessage"])

    def test_pre_tool_allows_silently_when_scope_mode_is_off(self) -> None:
        open_gate(self.repo, UNBORN_BASE)
        subprocess.run(["git", "config", "craftsman.scope", "off"], cwd=self.repo, check=True)
        payload = {
            "cwd": str(self.repo),
            "toolName": "run_in_terminal",
            "toolArgs": json.dumps({"command": "find ~ -name SKILL.md"}),
        }
        out, _ = call_pre_tool("copilot", payload)
        self.assertEqual(out, {})

    def test_only_craftsman_agent_names_are_craftsman_agents(self) -> None:
        self.assertTrue(is_craftsman_agent("Craftsman-Reviewer"))
        self.assertFalse(is_craftsman_agent("my-craftsman-helper"))

    def test_unknown_scope_mode_is_an_error(self) -> None:
        subprocess.run(["git", "config", "craftsman.scope", "enforce"], cwd=self.repo, check=True)

        with self.assertRaisesRegex(ScopeConfigError, "use one of deny, warn, off"):
            load_scope_policy(self.repo, self.repo)

    def test_grep_regex_arguments_are_ignored_by_scope_check(self) -> None:
        for case, tool, inp in (
            ("slash path in Grep pattern", "Grep", {"pattern": "/api/v1"}),
            ("tilde in Grep pattern", "Grep", {"pattern": "~/.claude"}),
            ("env var in Grep pattern", "Grep", {"pattern": "$HOME"}),
            ("tmp path in grep_search query", "grep_search", {"query": "/tmp/"}),
            ("session store in grep_search query", "grep_search", {"query": "~/.copilot/session-state"}),
        ):
            with self.subTest(case=case):
                dec = evaluate_scope(tool, inp, self.repo, self.repo, self.policy)
                self.assertEqual(dec.decision, "allow", f"{tool} {inp}: {dec.reason}")

    def test_nested_git_repository_inside_worktree_is_allowed(self) -> None:
        nested = self.repo / "vendor" / "lib"
        nested.mkdir(parents=True)
        subprocess.run(["git", "init"], cwd=nested, capture_output=True, check=True)
        (nested / "mod.py").write_text("x = 1\n", encoding="utf-8")

        self.assertEqual(
            evaluate_scope(
                "Bash", {"command": "rg foo vendor/lib"}, self.repo, self.repo, self.policy
            ).decision,
            "allow",
        )
        self.assertEqual(
            evaluate_scope(
                "Grep", {"path": "vendor/lib", "pattern": "foo"}, self.repo, self.repo, self.policy
            ).decision,
            "allow",
        )

    def test_subshell_chdir_does_not_leak_to_following_command(self) -> None:
        self.assertEqual(
            evaluate_scope(
                "Bash", {"command": "(cd ~ && pwd); rg foo"}, self.repo, self.repo, self.policy
            ).decision,
            "allow",
        )

    def test_popd_restores_previous_working_directory(self) -> None:
        self.assertEqual(
            evaluate_scope(
                "Bash",
                {"command": f"pushd {self.other_repo}; popd; rg foo"},
                self.repo,
                self.repo,
                self.policy,
            ).decision,
            "allow",
        )

    def test_heredoc_with_apostrophe_does_not_bypass_scope_check(self) -> None:
        heredoc_cmd = "cat <<'EOF'\nit's done\nEOF\nfind ~ -name SKILL.md"
        self.assertEqual(
            evaluate_scope("Bash", {"command": heredoc_cmd}, self.repo, self.repo, self.policy).decision,
            "deny",
        )

    def test_multiline_quoted_argument_does_not_bypass_scope_check(self) -> None:
        for case, cmd, expected in (
            (
                "commit message before rg in home",
                'git commit -m "subject\n\nbody" && rg secret ~',
                "deny",
            ),
            ("python -c before find in home", 'python -c "\nprint(1)\n"; find ~ -name SKILL.md', "deny"),
            (
                "commit message before rg in repo",
                'git commit -m "subject\n\nbody" && rg secret src',
                "allow",
            ),
        ):
            with self.subTest(case=case):
                self.assertEqual(
                    evaluate_scope("Bash", {"command": cmd}, self.repo, self.repo, self.policy).decision,
                    expected,
                )

    def test_cat_line_number_flag_does_not_hide_file_operand(self) -> None:
        self.assertEqual(
            evaluate_scope(
                "Bash",
                {"command": f"cat -n {self.other_repo}/.git/craftsman/packet-1.md"},
                self.repo,
                self.repo,
                self.policy,
            ).decision,
            "deny",
        )

    def test_non_recursive_ls_allows_parent_or_home_directory(self) -> None:
        for case, cmd, expected in (
            ("non-recursive ls on parent", "ls ..", "allow"),
            ("non-recursive ls on home", "ls ~", "allow"),
            ("non-recursive ls -la on parent", "ls -la ..", "allow"),
            ("recursive ls -R on parent", "ls -R ..", "deny"),
            ("recursive ls -R on home", "ls -R ~", "deny"),
            ("non-recursive ls on sibling repo", f"ls {self.other_repo}", "deny"),
            ("non-recursive ls on claude session store", "ls ~/.claude/projects", "deny"),
        ):
            with self.subTest(case=case):
                self.assertEqual(
                    evaluate_scope("Bash", {"command": cmd}, self.repo, self.repo, self.policy).decision,
                    expected,
                )

    def test_antigravity_run_command_enforces_scope_for_owner_session(self) -> None:
        open_gate(self.repo, UNBORN_BASE, owner=GateOwner("antigravity", "conv-123"))
        dec, reason = evaluate_antigravity_scope(
            {"artifactDirectoryPath": str(self.tmp / "brain" / "conv-123")},
            "find ~ -name SKILL.md",
            str(self.repo),
        )
        self.assertEqual(dec, "deny")
        self.assertIn("[CRAFTSMAN SCOPE-GUARD]", reason or "")

        other_dec, _ = evaluate_antigravity_scope(
            {"artifactDirectoryPath": str(self.tmp / "brain" / "conv-other")},
            "find ~ -name SKILL.md",
            str(self.repo),
        )
        self.assertEqual(other_dec, "allow")

    def _ensure_initial_commit(self) -> None:
        head = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", "HEAD"],
            cwd=self.repo,
            capture_output=True,
            check=False,
        )
        if head.returncode == 0:
            return
        subprocess.run(["git", "add", "src/app.py"], cwd=self.repo, capture_output=True, check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.com",
                "-c",
                "commit.gpgsign=false",
                "commit",
                "-m",
                "init",
            ],
            cwd=self.repo,
            capture_output=True,
            check=True,
        )

    def _add_linked_worktree(self, name: str) -> tuple[Path, Path]:
        self._ensure_initial_commit()
        wt = self.tmp / "workspace" / name
        subprocess.run(
            ["git", "worktree", "add", "-b", f"branch-{name}", str(wt)],
            cwd=self.repo,
            capture_output=True,
            check=True,
        )
        proc = subprocess.run(
            ["git", "rev-parse", "--absolute-git-dir"],
            cwd=wt,
            capture_output=True,
            text=True,
            check=True,
        )
        wt_git_dir = Path(proc.stdout.strip()).resolve()
        (wt_git_dir / "craftsman" / "inbox").mkdir(parents=True, exist_ok=True)
        return wt.resolve(), wt_git_dir

    def test_linked_worktree_allows_its_own_craftsman_directory(self) -> None:
        wt1, wt1_git = self._add_linked_worktree("wt-alpha")
        own_packet = wt1_git / "craftsman" / "packet-1.md"
        own_inbox_file = wt1_git / "craftsman" / "inbox" / "cover-1.json"
        own_packet.write_text("# WT1 Packet\n", encoding="utf-8")
        own_inbox_file.write_text("[]\n", encoding="utf-8")

        for case, tool, inp in (
            ("Read own packet", "Read", {"file_path": str(own_packet)}),
            ("Read own inbox file", "Read", {"file_path": str(own_inbox_file)}),
            ("cat own packet", "Bash", {"command": f"cat {own_packet}"}),
            ("cat own inbox file", "Bash", {"command": f"cat {own_inbox_file}"}),
            ("Grep own packet", "Grep", {"pattern": "Packet", "path": str(own_packet)}),
            ("Glob own inbox", "Glob", {"pattern": f"{wt1_git / 'craftsman' / 'inbox'}/*.json"}),
            ("Read own worktree source", "Read", {"file_path": str(wt1 / "src" / "app.py")}),
        ):
            with self.subTest(case=case):
                res = evaluate_scope(tool, inp, wt1, wt1, self.policy)
                self.assertEqual(res.decision, "allow", f"{case}: {res.reason}")

        open_gate(wt1, UNBORN_BASE)
        out, _ = call_pre_tool(
            "claude",
            {
                "hook_event_name": "PreToolUse",
                "cwd": str(wt1),
                "tool_name": "Read",
                "tool_input": {"file_path": str(own_inbox_file)},
            },
        )
        self.assertNotEqual(out.get("hookSpecificOutput", {}).get("permissionDecision"), "deny")

    def test_linked_worktree_denies_other_worktrees_or_main_repo(self) -> None:
        wt1, wt1_git = self._add_linked_worktree("wt-alpha")
        wt2, wt2_git = self._add_linked_worktree("wt-beta")
        wt2_inbox = wt2_git / "craftsman" / "inbox" / "cover-2.json"
        wt2_inbox.write_text("[]\n", encoding="utf-8")
        main_inbox = self.repo / ".git" / "craftsman" / "inbox" / "cover-0.json"
        main_inbox.parent.mkdir(parents=True, exist_ok=True)
        main_inbox.write_text("[]\n", encoding="utf-8")

        for case, tool, inp, expected_reason in (
            (
                "Read sibling worktree inbox",
                "Read",
                {"file_path": str(wt2_inbox)},
                "another git worktree",
            ),
            (
                "cat sibling worktree inbox",
                "Bash",
                {"command": f"cat {wt2_inbox}"},
                "another git worktree",
            ),
            (
                "Read sibling worktree source",
                "Read",
                {"file_path": str(wt2 / "src" / "app.py")},
                "another git repository",
            ),
            ("Read main worktree inbox", "Read", {"file_path": str(main_inbox)}, "another git worktree"),
            (
                "cat main worktree inbox",
                "Bash",
                {"command": f"cat {main_inbox}"},
                "another git worktree",
            ),
            (
                "Read main git config",
                "Read",
                {"file_path": str(self.repo / ".git" / "config")},
                "another git worktree",
            ),
            (
                "Read own gitdir metadata outside craftsman",
                "Read",
                {"file_path": str(wt1_git / "HEAD")},
                "another git worktree",
            ),
            (
                "Glob across all worktree inboxes",
                "Glob",
                {"pattern": f"{self.repo / '.git' / 'worktrees'}/*/craftsman/inbox/*.json"},
                "another git worktree",
            ),
            (
                "Read main worktree source",
                "Read",
                {"file_path": str(self.repo / "src" / "app.py")},
                "another git repository",
            ),
            (
                "cat main worktree source",
                "Bash",
                {"command": f"cat {self.repo / 'src' / 'app.py'}"},
                "another git repository",
            ),
        ):
            with self.subTest(case=case):
                res = evaluate_scope(tool, inp, wt1, wt1, self.policy)
                self.assertEqual(res.decision, "deny", f"{case} unexpectedly allowed")
                self.assertIn(expected_reason, res.reason)

    def test_main_worktree_denies_linked_worktree_craftsman_files(self) -> None:
        _, wt1_git = self._add_linked_worktree("wt-alpha")
        wt1_inbox = wt1_git / "craftsman" / "inbox" / "cover-1.json"
        wt1_packet = wt1_git / "craftsman" / "packet-1.md"
        wt1_inbox.write_text("[]\n", encoding="utf-8")
        wt1_packet.write_text("# WT1\n", encoding="utf-8")
        main_inbox = self.repo / ".git" / "craftsman" / "inbox" / "cover-0.json"
        main_inbox.parent.mkdir(parents=True, exist_ok=True)
        main_inbox.write_text("[]\n", encoding="utf-8")

        self.assertEqual(
            evaluate_scope(
                "Read", {"file_path": str(main_inbox)}, self.repo, self.repo, self.policy
            ).decision,
            "allow",
        )
        for case, tool, inp in (
            ("Read linked worktree inbox", "Read", {"file_path": str(wt1_inbox)}),
            ("cat linked worktree inbox", "Bash", {"command": f"cat {wt1_inbox}"}),
            ("Read linked worktree packet", "Read", {"file_path": str(wt1_packet)}),
            ("Grep linked worktree gitdir", "Grep", {"pattern": "WT1", "path": str(wt1_git)}),
        ):
            with self.subTest(case=case):
                res = evaluate_scope(tool, inp, self.repo, self.repo, self.policy)
                self.assertEqual(res.decision, "deny", f"{case} unexpectedly allowed")
                self.assertIn("another git worktree", res.reason)

    def test_linked_worktree_denies_symlink_escaping_craftsman_inbox(self) -> None:
        wt1, wt1_git = self._add_linked_worktree("wt-alpha")
        escape_file = wt1_git / "craftsman" / "inbox" / "escape.json"
        escape_file.symlink_to(self.outside_dir / "leak.txt")

        for tool, inp in (
            ("Read", {"file_path": str(escape_file)}),
            ("Bash", {"command": f"cat {escape_file}"}),
        ):
            res = evaluate_scope(tool, inp, wt1, wt1, self.policy)
            self.assertEqual(res.decision, "deny")
            self.assertIn("escapes the repository work tree via symlink", res.reason)

        wt2, wt2_git = self._add_linked_worktree("wt-beta")
        os.rmdir(wt2_git / "craftsman" / "inbox")
        os.rmdir(wt2_git / "craftsman")
        (wt2_git / "craftsman").symlink_to(self.outside_dir)
        symlink_dir_res = evaluate_scope(
            "Read",
            {"file_path": str(wt2_git / "craftsman" / "leak.txt")},
            wt2,
            wt2,
            self.policy,
        )
        self.assertEqual(symlink_dir_res.decision, "deny")
        self.assertIn("escapes the repository work tree via symlink", symlink_dir_res.reason)

    def test_relative_gitdir_pointer_resolves_worktree_craftsman_scope(self) -> None:
        rel_wt = self.tmp / "workspace" / "rel-wt"
        rel_wt.mkdir(parents=True)
        rel_git = self.repo / ".git" / "worktrees" / "rel-wt"
        (rel_git / "craftsman" / "inbox").mkdir(parents=True)
        rel_inbox = rel_git / "craftsman" / "inbox" / "cover-1.json"
        rel_inbox.write_text("[]\n", encoding="utf-8")
        (rel_wt / ".git").write_text("gitdir: ../my-repo/.git/worktrees/rel-wt\n", encoding="utf-8")

        self.assertEqual(
            evaluate_scope("Read", {"file_path": str(rel_inbox)}, rel_wt, rel_wt, self.policy).decision,
            "allow",
        )
        self.assertEqual(
            evaluate_scope(
                "Read",
                {"file_path": str(self.repo / ".git" / "craftsman" / "packet-1.md")},
                rel_wt,
                rel_wt,
                self.policy,
            ).decision,
            "deny",
        )

        plain_dir = self.tmp / "plain-non-git"
        plain_dir.mkdir()
        (plain_dir / "note.txt").write_text("hi\n", encoding="utf-8")
        self.assertEqual(
            evaluate_scope(
                "Read", {"file_path": str(plain_dir / "note.txt")}, plain_dir, plain_dir, self.policy
            ).decision,
            "allow",
        )
        self.assertEqual(
            evaluate_scope(
                "Read",
                {"file_path": str(self.other_repo / "secret.py")},
                plain_dir,
                plain_dir,
                self.policy,
            ).decision,
            "deny",
        )

    def test_posix_heredoc_with_semicolons_in_json_note_is_allowed(self) -> None:
        cmd = (
            "craftsman review cover --from-json - <<'EOF'\n"
            '[{"lenses": ["intent", "contracts", "failure"], "hunks": ["H1", "H2"], '
            '"note": "H1 parse_cfg: raw=\'\' -> {}; callers cli.py:19 ok; '
            'H2 save_cfg: rejects ../outside path -> ConfigError at cfg.py:55"}]\n'
            "EOF"
        )
        decision = evaluate_scope("Bash", {"command": cmd}, self.repo, self.repo, self.policy)
        self.assertEqual(decision.decision, "allow", decision.reason)

    def test_powershell_herestring_batch_allowed_while_bad_read_denied(self) -> None:
        inbox = self.repo / ".git" / "craftsman" / "inbox" / "cover-1.json"
        inbox.parent.mkdir(parents=True, exist_ok=True)
        for cmd in (
            "@'\n"
            '[{"lenses": ["intent", "contracts"], "hunks": ["H1"], '
            '"note": "H1 fn: caller\'s raw=\'\' -> {}; rejects /etc/passwd at a.py:9"}]\n'
            "'@ | craftsman review cover --from-json -",
            "@'\r\n"
            '[{"lens": "intent", "hunks": ["H1"], "note": "H1 fn: x=1; y=2 -> caller\'s default at a.py:9"}]\r\n'
            f"'@ | Set-Content -LiteralPath {inbox} -Encoding utf8",
        ):
            with self.subTest(cmd=cmd[:40]):
                dec = evaluate_scope("pwsh", {"command": cmd}, self.repo, self.repo, self.policy)
                self.assertEqual(dec.decision, "allow", dec.reason)

        unclosed = "@'\n[{\"lens\": \"intent\"}]\n"
        self.assertEqual(
            evaluate_scope("pwsh", {"command": unclosed}, self.repo, self.repo, self.policy).decision,
            "deny",
        )
        secret_read = f"cat @'\n{self.other_repo / 'secret.py'}\n'@"
        self.assertEqual(
            evaluate_scope("pwsh", {"command": secret_read}, self.repo, self.repo, self.policy).decision,
            "deny",
        )

    def test_herestring_token_boundaries_distinguish_bash_mid_word_quotes(self) -> None:
        ps_cover = (
            "@'\n"
            '[{"lens": "intent", "hunks": ["H1"], "note": "ok"}]\n'
            "'@ | craftsman review cover --from-json -"
        )
        ps_redirect = "@'\n[]\n'@>out.json\ncraftsman review cover --from-json out.json"
        for tool, cmd, expected in (
            ("Bash", "printf 'ok' foo@'\necho hi'", "allow"),
            ("Bash", 'printf "ok" user@"\nhost"', "allow"),
            ("pwsh", ps_cover, "allow"),
            ("pwsh", ps_redirect, "allow"),
            ("pwsh", "$a = @'\nx\n'@.Trim()", "allow"),
            ("pwsh", "$a = @'\nx\n'@+(cat ~/.zsh_history)+@'\ny's\n'@", "deny"),
            ("pwsh", "$a = @'\nx\n'@,(cat ~/.zsh_history),@'\ny's\n'@", "deny"),
            ("pwsh", "$a = @'\nx\n'@.Trim() + (cat ~/.zsh_history)", "deny"),
        ):
            with self.subTest(cmd=cmd):
                dec = evaluate_scope(tool, {"command": cmd}, self.repo, self.repo, self.policy)
                self.assertEqual(dec.decision, expected, dec.reason)

        chained = "printf 'ok' foo@'\necho hi'\nfind ~ -name SKILL.md"
        chained_dec = evaluate_scope(
            "Bash", {"command": chained}, self.repo, self.repo, self.policy
        )
        self.assertEqual(chained_dec.decision, "deny")
        self.assertNotIn("unmatched quotes", chained_dec.reason)

        unclosed = "@'\n[{\"lens\": \"intent\"}]\n"
        err = io.StringIO()
        with redirect_stderr(err):
            dec = evaluate_scope("pwsh", {"command": unclosed}, self.repo, self.repo, self.policy)
        self.assertEqual(dec.decision, "deny")
        self.assertIn("has unmatched quotes", dec.reason)

    def test_scope_allow_parent_still_denies_other_worktree_git_dirs(self) -> None:
        wt1, _ = self._add_linked_worktree("wt-one")
        wt2, wt2_git = self._add_linked_worktree("wt-two")
        wide_policy = ScopePolicy(mode="deny", allowed_dirs=(self.tmp,))
        other_inbox = wt2_git / "craftsman" / "inbox" / "c.json"
        main_inbox = self.repo / ".git" / "craftsman" / "inbox" / "c.json"
        other_inbox.parent.mkdir(parents=True, exist_ok=True)
        main_inbox.parent.mkdir(parents=True, exist_ok=True)
        other_inbox.write_text("[]", encoding="utf-8")
        main_inbox.write_text("[]", encoding="utf-8")
        (wt2 / "app.py").write_text("x = 2\n", encoding="utf-8")

        self.assertEqual(
            evaluate_scope("Read", {"file_path": str(other_inbox)}, wt1, wt1, wide_policy).decision,
            "deny",
        )
        self.assertEqual(
            evaluate_scope("Read", {"file_path": str(main_inbox)}, wt1, wt1, wide_policy).decision,
            "deny",
        )
        self.assertEqual(
            evaluate_scope("Read", {"file_path": str(wt2 / "app.py")}, wt1, wt1, wide_policy).decision,
            "allow",
        )

    def test_scope_guard_entrypoints_enforce_only_search_scope(self) -> None:
        open_gate(self.repo, UNBORN_BASE, owner=GateOwner("antigravity", "conv-123"))
        session_dir = str(self.tmp / "brain" / "conv-123")

        def run_in_proc(raw_stdin: str, argv: list[str] | None = None) -> tuple[int, str, str]:
            out, err = io.StringIO(), io.StringIO()
            with (
                mock.patch("sys.stdin", io.StringIO(raw_stdin)),
                redirect_stdout(out),
                redirect_stderr(err),
            ):
                code = scope_guard_main(argv or [])
            return code, out.getvalue(), err.getvalue()

        code, out, _ = run_in_proc("", ["--help"])
        self.assertEqual(code, 0)
        self.assertIn("craftsman scope-guard", out)

        code, _, err = run_in_proc("{}", ["--bogus"])
        self.assertEqual(code, 2)
        self.assertIn("unknown arguments --bogus", err)

        code, out, _ = run_in_proc("{not-json")
        self.assertEqual(code, 0)
        bad_json = json.loads(out)
        self.assertEqual(bad_json["decision"], "deny")
        self.assertIn("Unreadable hook input", bad_json["reason"])

        code, out, _ = run_in_proc("[1, 2]")
        self.assertEqual(code, 0)
        non_obj = json.loads(out)
        self.assertEqual(non_obj["decision"], "deny")
        self.assertIn("not a JSON object", non_obj["reason"])

        code, out, _ = run_in_proc(json.dumps({"toolCall": {"name": "view_file", "args": {}}}))
        self.assertEqual((code, json.loads(out)), (0, {"decision": "allow"}))

        # Git policy commands (push to main, AI trailers) are allowed by scope-guard.
        for git_cmd in (
            "git push origin main",
            'git commit -m "feat: x\n\nCo-authored-by: Claude <noreply@anthropic.com>"',
        ):
            payload = {
                "artifactDirectoryPath": session_dir,
                "toolCall": {
                    "name": "run_command",
                    "args": {"CommandLine": git_cmd, "Cwd": str(self.repo)},
                },
            }
            code, out, _ = run_in_proc(json.dumps(payload))
            self.assertEqual((code, json.loads(out)), (0, {"decision": "allow"}), git_cmd)

        # Out-of-scope search via the standalone shim script is denied.
        script = REPO_ROOT / "scripts" / "craftsman-scope-guard.py"
        deny_payload = {
            "artifactDirectoryPath": session_dir,
            "toolCall": {
                "name": "run_command",
                "args": {"CommandLine": "find ~ -name SKILL.md", "Cwd": str(self.repo)},
            },
        }
        proc = subprocess.run(
            [sys.executable, str(script)],
            input=json.dumps(deny_payload),
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
            env={**os.environ, "HOME": str(self.home), "USERPROFILE": str(self.home)},
        )
        script_out = json.loads(proc.stdout)
        self.assertEqual(script_out["decision"], "deny")
        self.assertIn("[CRAFTSMAN SCOPE-GUARD]", script_out["reason"])


if __name__ == "__main__":
    unittest.main()
