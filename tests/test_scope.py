"""Tests for repository search-scope enforcement (`craftsman/scope.py` and `pre-tool` hook)."""

from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any
from unittest import mock

from craftsman import hooks
from craftsman.scope import ScopePolicy, evaluate_scope, load_scope_policy


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

        # Assistant session stores
        for rel in (
            ".copilot/session-state",
            ".claude/projects/slug",
            ".gemini/antigravity/brain/conv-1",
            ".gemini/antigravity/conversations",
        ):
            d = self.home / rel
            d.mkdir(parents=True)
            (d / "session.jsonl").write_text("{}\n", encoding="utf-8")

        # Symlink inside repo pointing outside repo
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

    def test_table_of_45_evaluate_scope_cases(self) -> None:
        own_pkt = str(self.repo / ".git" / "craftsman" / "packet-1.md")
        other_pkt = str(self.other_repo / ".git" / "craftsman" / "packet-1.md")
        dep_file = str(self.venv_site / "a.py")
        tmp_file = str(self.scratch_tmp / "x.txt")

        cases: list[tuple[str, str, dict[str, Any], str]] = [
            # 1-6: find, grep, rg
            ("find_tilde", "Bash", {"command": "find ~ -name SKILL.md"}, "deny"),
            ("find_dot", "Bash", {"command": "find . -name x"}, "allow"),
            ("find_sub", "Bash", {"command": "find src -name '*.py'"}, "allow"),
            ("find_root", "Bash", {"command": "find / -name x"}, "deny"),
            ("grep_rec_outside", "Bash", {"command": f"grep -r foo {self.other_repo}"}, "deny"),
            ("grep_nonrec_dep", "Bash", {"command": f"grep foo {dep_file}"}, "allow"),
            ("grep_nonrec_other_repo", "Bash", {"command": "grep foo ../other-repo/secret.py"}, "deny"),
            # 7-12: rg, ag, ack, fd
            ("rg_default_cwd", "Bash", {"command": "rg foo"}, "allow"),
            ("rg_src", "Bash", {"command": "rg foo src"}, "allow"),
            ("rg_parent", "Bash", {"command": "rg foo .."}, "deny"),
            ("rg_sibling_repo", "Bash", {"command": "rg foo ../other-repo"}, "deny"),
            ("rg_files_tilde", "Bash", {"command": "rg --files ~"}, "deny"),
            ("rg_attached_e_tilde", "Bash", {"command": "rg -efoo ~"}, "deny"),
            ("ag_outside", "Bash", {"command": "ag pattern $HOME"}, "deny"),
            ("ag_g_tilde", "Bash", {"command": "ag -g SKILL ~"}, "deny"),
            ("ack_f_tilde", "Bash", {"command": "ack -f ~"}, "deny"),
            ("ack_inside", "Bash", {"command": "ack pattern src"}, "allow"),
            ("fd_outside", "Bash", {"command": "fd SKILL.md ~"}, "deny"),
            ("fd_search_path_out", "Bash", {"command": "fd --search-path .. foo"}, "deny"),
            ("fd_inside", "Bash", {"command": "fd app.py src"}, "allow"),
            # 17-22: ls, tree, du, locate, mdfind
            ("ls_nonrec_copilot", "Bash", {"command": "ls ~/.copilot/skills"}, "allow"),
            ("ls_nonrec_tilde", "Bash", {"command": "ls ~"}, "allow"),
            ("ls_nonrec_other_repo", "Bash", {"command": "ls ../other-repo"}, "deny"),
            ("ls_rec_tilde", "Bash", {"command": "ls -R ~"}, "deny"),
            ("ls_laR_parent", "Bash", {"command": "ls -laR .."}, "deny"),
            ("tree_tilde", "Bash", {"command": "tree ~"}, "deny"),
            ("tree_dot", "Bash", {"command": "tree ."}, "allow"),
            ("du_parent", "Bash", {"command": "du -sh .."}, "deny"),
            ("locate_any", "Bash", {"command": "locate SKILL.md"}, "deny"),
            ("mdfind_global", "Bash", {"command": "mdfind SKILL.md"}, "deny"),
            ("mdfind_onlyin_repo", "Bash", {"command": f"mdfind -onlyin {self.repo} app"}, "allow"),
            ("mdfind_onlyin_home", "Bash", {"command": "mdfind -onlyin ~ app"}, "deny"),
            # 27-32: cat / head / tail / sed / history (D3)
            ("cat_other_packet", "Bash", {"command": "cat ../other-repo/.git/craftsman/packet-1.md"}, "deny"),
            ("cat_abs_other_pkt", "Bash", {"command": f"cat {other_pkt}"}, "deny"),
            ("cat_own_packet", "Bash", {"command": f"cat {own_pkt}"}, "allow"),
            ("cat_tmp_file", "Bash", {"command": f"cat {tmp_file}"}, "allow"),
            ("cat_venv_site_pkg", "Bash", {"command": f"cat {dep_file}"}, "allow"),
            ("head_claude_sess", "Bash", {"command": "head -n 20 ~/.claude/projects/slug/session.jsonl"}, "deny"),
            ("cat_zsh_history", "Bash", {"command": "cat ~/.zsh_history"}, "deny"),
            ("sed_other_repo", "Bash", {"command": "sed -n '1,20p' ../other-repo/secret.py"}, "deny"),
            # 34-39: wrappers, cd tracking, PowerShell, cmd, symlinks, git -C, find -exec
            ("env_bash_c_find", "Bash", {"command": 'env FOO=1 bash -c "find / -name x"'}, "deny"),
            ("env_C_rg", "Bash", {"command": "env -C .. rg foo"}, "deny"),
            ("sudo_D_cat", "Bash", {"command": "sudo -D ../other-repo cat secret.py"}, "deny"),
            ("find_exec_cat_other", "Bash", {"command": "find . -exec cat ../other-repo/secret.py \\;"}, "deny"),
            ("fd_x_cat_other", "Bash", {"command": "fd . -x cat ../other-repo/secret.py"}, "deny"),
            ("git_C_other_grep", "Bash", {"command": "git -C ../other-repo grep foo"}, "deny"),
            ("git_grep_noindex_tilde", "Bash", {"command": "git grep --no-index foo ~"}, "deny"),
            ("git_grep_inside", "Bash", {"command": "git grep foo"}, "allow"),
            ("cd_tilde_grep_r", "Bash", {"command": "cd ~ && grep -r x ."}, "deny"),
            ("cd_src_rg", "Bash", {"command": "cd src && rg x ."}, "allow"),
            ("cd_symlink_cat", "Bash", {"command": "cd escape_link && cat leak.txt"}, "deny"),
            ("cwd_symlink_cat", "Bash", {"command": "cat leak.txt", "cwd": "escape_link"}, "deny"),
            ("ps_set_location_rg", "powershell", {"command": "Set-Location $env:USERPROFILE; rg foo"}, "deny"),
            ("ps_gci_recurse_c", "powershell", {"command": "Get-ChildItem -Recurse C:\\"}, "deny"),
            ("ps_gci_recurse_c_users", "powershell", {"command": r"Get-ChildItem -Path C:\Users -Recurse"}, "deny"),
            ("ps_gci_r_dot", "powershell", {"command": "gci -r ."}, "allow"),
            ("cmd_dir_s_c", "run_in_terminal", {"command": "dir /s C:\\"}, "deny"),
            ("symlink_find", "Bash", {"command": "find escape_link -name '*'"}, "deny"),
            ("symlink_read", "Read", {"file_path": str(self.repo / "escape_link" / "leak.txt")}, "deny"),
            # 42-55: IDE / Claude Read, Grep, Glob, LS, VS Code read_file, list_dir, file_search, grep_search
            ("claude_read_other_repo", "Read", {"file_path": str(self.other_repo / "secret.py")}, "deny"),
            ("claude_read_own_repo", "Read", {"file_path": str(self.repo / "src" / "app.py")}, "allow"),
            ("claude_read_dep", "Read", {"file_path": dep_file}, "allow"),
            ("claude_grep_tilde", "Grep", {"pattern": "foo", "path": "~"}, "deny"),
            ("claude_grep_repo", "Grep", {"pattern": "foo", "path": str(self.repo)}, "allow"),
            ("claude_ls_tilde", "LS", {"path": "~"}, "deny"),
            ("vscode_list_dir_parent", "list_dir", {"DirectoryPath": ".."}, "deny"),
            ("claude_glob_outside", "Glob", {"pattern": f"{self.other_repo}/**/*.py"}, "deny"),
            ("claude_glob_rel_escape", "Glob", {"pattern": "./../../other-repo/**/*.py"}, "deny"),
            ("claude_glob_symlink_escape", "Glob", {"pattern": "escape_link/**/*.py"}, "deny"),
            ("claude_glob_inside", "Glob", {"pattern": "**/*.py"}, "allow"),
            ("claude_glob_src_inside", "Glob", {"pattern": "src/**/*.py"}, "allow"),
            (
                "vscode_read_claude_proj",
                "read_file",
                {"filePath": str(self.home / ".claude" / "projects" / "slug" / "session.jsonl")},
                "deny",
            ),
            (
                "vscode_read_copilot_sess",
                "read_file",
                {"filePath": str(self.home / ".copilot" / "session-state" / "session.jsonl")},
                "deny",
            ),
            (
                "vscode_read_agy_brain",
                "read_file",
                {"filePath": str(self.home / ".gemini" / "antigravity" / "brain" / "conv-1" / "session.jsonl")},
                "deny",
            ),
            ("vscode_grep_search_out", "grep_search", {"query": "foo", "includePattern": "../other-repo/**"}, "deny"),
            ("vscode_file_search_in", "file_search", {"query": "app.py", "folder": str(self.repo / "src")}, "allow"),
        ]
        self.assertGreaterEqual(len(cases), 40)
        for label, tool, tool_input, expected in cases:
            with self.subTest(case=label):
                res = evaluate_scope(tool, tool_input, self.repo, self.repo, self.policy)
                self.assertEqual(
                    res.decision,
                    expected,
                    f"Case {label!r} ({tool} {tool_input}) returned {res}",
                )
                if expected == "deny":
                    self.assertIn("[CRAFTSMAN SCOPE-GUARD]", res.reason)
                    self.assertIn("git config craftsman.scopeAllow", res.reason)

    def test_scope_allow_policy_warn_and_off_and_unparseable_command(self) -> None:
        # 1. scopeAllow unlocks other_repo (including colon/semicolon-separated entries)
        extra_dir = self.tmp / "shared-contracts"
        extra_dir.mkdir()
        subprocess.run(
            [
                "git",
                "config",
                "--add",
                "craftsman.scopeAllow",
                f"{self.other_repo}:{extra_dir}",
            ],
            cwd=self.repo,
            check=True,
        )
        policy = load_scope_policy(self.repo, self.repo)
        self.assertEqual(policy.mode, "deny")
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

        # 2. Unparseable command fails open with diagnostic to stderr
        err = io.StringIO()
        with redirect_stderr(err):
            unparseable = evaluate_scope(
                "Bash",
                {"command": 'find "unterminated quote'},
                self.repo,
                self.repo,
                self.policy,
            )
        self.assertEqual(unparseable.decision, "allow")
        self.assertIn("ignoring unparseable shell command", err.getvalue())

    def test_pre_tool_hook_activation_dialects_warn_and_off_modes(self) -> None:
        # 1. Without active gate session and non-craftsman agent -> allow everything
        copilot_camel = {
            "cwd": str(self.repo),
            "toolName": "run_in_terminal",
            "toolArgs": json.dumps({"command": "find ~ -name SKILL.md"}),
            "agentName": "general-helper",
        }
        out_inactive, _ = call_pre_tool("copilot", copilot_camel)
        self.assertEqual(out_inactive, {})

        # 2. Without active gate session, but agent is `craftsman-reviewer` -> enforced (deny)!
        copilot_reviewer = {
            **copilot_camel,
            "agentType": "custom",
            "agentName": "craftsman-reviewer",
        }
        out_rev, _ = call_pre_tool("copilot", copilot_reviewer)
        self.assertEqual(out_rev["permissionDecision"], "deny")
        self.assertEqual(out_rev["hookSpecificOutput"]["permissionDecision"], "deny")

        # 3. With active gate session -> enforced for both Copilot and Claude
        state_file = self.repo / ".git" / "craftsman" / "craftsman_state.json"
        state_file.write_text(
            json.dumps({"active": True, "tier": "standard", "cwd": str(self.repo)}),
            encoding="utf-8",
        )
        claude_read = {
            "hook_event_name": "PreToolUse",
            "cwd": str(self.repo),
            "tool_name": "Read",
            "tool_input": {"file_path": str(self.other_repo / "secret.py")},
        }
        out_claude_deny, _ = call_pre_tool("claude", claude_read)
        self.assertEqual(out_claude_deny["hookSpecificOutput"]["permissionDecision"], "deny")

        # 4. craftsman.scope = warn -> allow with systemMessage and additionalContext
        subprocess.run(["git", "config", "craftsman.scope", "warn"], cwd=self.repo, check=True)
        out_warn, _ = call_pre_tool("copilot", copilot_camel)
        self.assertNotIn("permissionDecision", out_warn)
        self.assertIn("[CRAFTSMAN SCOPE-GUARD]", out_warn["systemMessage"])
        self.assertIn("[CRAFTSMAN SCOPE-GUARD]", out_warn["additionalContext"])

        # 5. craftsman.scope = off -> allow silently
        subprocess.run(["git", "config", "craftsman.scope", "off"], cwd=self.repo, check=True)
        out_off, _ = call_pre_tool("copilot", copilot_camel)
        self.assertEqual(out_off, {})

        # 6. craftsman.scope = enforce is accepted as an alias for deny
        subprocess.run(["git", "config", "craftsman.scope", "enforce"], cwd=self.repo, check=True)
        self.assertEqual(load_scope_policy(self.repo, self.repo).mode, "deny")

    def test_grep_regex_patterns_and_queries_are_not_treated_as_paths(self) -> None:
        for tool, inp in (
            ("Grep", {"pattern": "/api/v1"}),
            ("Grep", {"pattern": "~/.claude"}),
            ("Grep", {"pattern": "$HOME"}),
            ("grep_search", {"query": "/tmp/"}),
            ("grep_search", {"query": "~/.copilot/session-state"}),
        ):
            with self.subTest(tool=tool, inp=inp):
                dec = evaluate_scope(tool, inp, self.repo, self.repo, self.policy)
                self.assertEqual(dec.decision, "allow", f"{tool} {inp}: {dec.reason}")

    def test_submodules_and_nested_repos_inside_worktree_are_allowed(self) -> None:
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

    def test_subshell_cd_pushd_popd_heredoc_apostrophes_cat_flags_and_nonrec_ls(self) -> None:
        # 1. Subshell cd does not leak into following command; pushd/popd restores cwd
        self.assertEqual(
            evaluate_scope(
                "Bash", {"command": "(cd ~ && pwd); rg foo"}, self.repo, self.repo, self.policy
            ).decision,
            "allow",
        )
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

        # 2. Heredoc with an apostrophe (it's) does not fool _has_unmatched_quotes
        heredoc_cmd = "cat <<'EOF'\nit's done\nEOF\nfind ~ -name SKILL.md"
        self.assertEqual(
            evaluate_scope(
                "Bash", {"command": heredoc_cmd}, self.repo, self.repo, self.policy
            ).decision,
            "deny",
        )

        # 3. `cat -n` and `cat -c` do not treat the file operand as an option value
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
        self.assertEqual(
            evaluate_scope(
                "Bash",
                {"command": f"cat -n {self.other_repo}/secret.py"},
                self.repo,
                self.repo,
                self.policy,
            ).decision,
            "deny",
        )

        # 4. Non-recursive `ls ..` and `ls ~` are allowed, while `ls -R ..`, `ls ../other-repo`, and `ls ~/.claude/projects` are denied
        for allowed_ls in ("ls ..", "ls ~", "ls -la .."):
            with self.subTest(allowed_ls=allowed_ls):
                self.assertEqual(
                    evaluate_scope(
                        "Bash", {"command": allowed_ls}, self.repo, self.repo, self.policy
                    ).decision,
                    "allow",
                )
        for denied_ls in (
            "ls -R ..",
            "ls -R ~",
            f"ls {self.other_repo}",
            "ls ~/.claude/projects",
        ):
            with self.subTest(denied_ls=denied_ls):
                self.assertEqual(
                    evaluate_scope(
                        "Bash", {"command": denied_ls}, self.repo, self.repo, self.policy
                    ).decision,
                    "deny",
                )

    def test_antigravity_run_command_enforces_scope_when_gate_is_owned_by_antigravity(self) -> None:
        from craftsman.guard import evaluate_command

        state_file = self.repo / ".git" / "craftsman" / "craftsman_state.json"
        state_file.write_text(
            json.dumps(
                {
                    "active": True,
                    "tier": "standard",
                    "cwd": str(self.repo),
                    "owner": {"platform": "antigravity", "session_id": "conv-123"},
                }
            ),
            encoding="utf-8",
        )
        dec, reason = evaluate_command(
            {
                "tool": "run_command",
                "conversation_id": "conv-123",
                "toolCall": {
                    "name": "run_command",
                    "args": {"CommandLine": "find ~ -name SKILL.md", "Cwd": str(self.repo)},
                },
            }
        )
        self.assertEqual(dec, "deny")
        self.assertIn("[CRAFTSMAN SCOPE-GUARD]", reason or "")


if __name__ == "__main__":
    unittest.main()
