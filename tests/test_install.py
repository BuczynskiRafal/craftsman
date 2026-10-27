"""Tests for `craftsman install`, `craftsman uninstall`, `install.sh`, and passthrough git hooks."""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from craftsman.cli import detect_antigravity_drift, inspect_git_hooks, run_doctor
from craftsman.export import default_vscode_prompts_dir, remove_claude_hooks, write_text_atomic
from craftsman.install import (
    MANIFEST_FILENAME,
    PASSTHROUGH_TEMPLATE_NAME,
    STANDARD_PASSTHROUGH_GIT_HOOKS,
    BaseManifest,
    InstallError,
    InstallMode,
    InstallTracker,
    TargetKind,
    TargetManifest,
    _expand_tilde,
    _install_scripts_to,
    _TargetInstallSession,
    install_main,
    load_manifest,
    parse_install_args,
    parse_uninstall_args,
    resolve_git_hooks_base_dir,
    save_manifest,
    uninstall_main,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def _snapshot_tree(root: Path) -> dict[str, tuple[str, bytes | str, int]]:
    """Capture every file, symlink, and directory under `root` for byte-identity assertions."""
    state: dict[str, tuple[str, bytes | str, int]] = {}
    if not root.exists() and not root.is_symlink():
        return state
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dir_p = Path(dirpath)
        rel_dir = str(dir_p.relative_to(root))
        state[rel_dir] = ("dir", "", dir_p.lstat().st_mode & 0o777)
        for name in sorted(dirnames + filenames):
            p = dir_p / name
            rel = str(p.relative_to(root))
            if p.is_symlink():
                state[rel] = ("symlink", os.readlink(p), 0)
            elif p.is_file():
                state[rel] = ("file", p.read_bytes(), p.stat().st_mode & 0o777)
    return state


class IsolatedInstallTestCase(unittest.TestCase):
    """Base test case that isolates HOME, XDG_CONFIG_HOME, APPDATA, CRAFTSMAN_HOME and GIT_CONFIG_GLOBAL."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.home = self.tmp / "home"
        self.home.mkdir(parents=True)
        self.xdg = self.home / ".config"
        self.appdata = self.home / "AppData" / "Roaming"
        self.craftsman_home = self.home / ".craftsman"
        self.git_config_global = self.tmp / "gitconfig"
        self.git_config_global.write_text("", encoding="utf-8")

        self.env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self.home),
            "USERPROFILE": str(self.home),
            "XDG_CONFIG_HOME": str(self.xdg),
            "APPDATA": str(self.appdata),
            "CRAFTSMAN_HOME": str(self.craftsman_home),
            "GIT_CONFIG_GLOBAL": str(self.git_config_global),
            "GIT_CONFIG_NOSYSTEM": "1",
        }
        self._env_patcher = mock.patch.dict(os.environ, self.env, clear=True)
        self._env_patcher.start()
        self.addCleanup(self._env_patcher.stop)

    def run_cli_inproc(self, subcommand: str, *args: str) -> tuple[int, str, str]:
        out = io.StringIO()
        err = io.StringIO()
        fn = install_main if subcommand == "install" else uninstall_main
        argv = ["--repo-dir", str(REPO_ROOT), *args]
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = fn(argv)
        return code, out.getvalue(), err.getvalue()

    def git_global_get(self, key: str) -> str | None:
        proc = subprocess.run(
            ["git", "config", "--global", "--get", key],
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
        )
        return proc.stdout.strip() if proc.returncode == 0 and proc.stdout.strip() else None

    def git_global_set(self, key: str, value: str) -> None:
        subprocess.run(
            ["git", "config", "--global", key, value],
            env=self.env,
            capture_output=True,
            text=True,
            check=True,
        )


class FlagParsingAndConflictTests(IsolatedInstallTestCase):
    def test_mutually_exclusive_modes_rejected_on_install_and_uninstall(self) -> None:
        for flags in (
            ("--copy", "--symlink"),
            ("--symlink", "--plugin"),
            ("--copy", "--plugin"),
            ("--copy", "--symlink", "--plugin"),
        ):
            with self.subTest(cmd="install", flags=flags):
                with self.assertRaisesRegex(InstallError, "mutually exclusive"):
                    parse_install_args(flags)
                code, _, err = self.run_cli_inproc("install", *flags)
                self.assertEqual(code, 1)
                self.assertIn("mutually exclusive", err)
            with self.subTest(cmd="uninstall", flags=flags):
                with self.assertRaisesRegex(InstallError, "mutually exclusive"):
                    parse_uninstall_args(flags)
                code, _, err = self.run_cli_inproc("uninstall", *flags)
                self.assertEqual(code, 1)
                self.assertIn("mutually exclusive", err)

    def test_no_claude_hooks_conflicts_with_claude_hooks(self) -> None:
        with self.assertRaisesRegex(InstallError, "Conflicting flags: --no-claude-hooks"):
            parse_install_args(("--claude-hooks", "--no-claude-hooks"))

    def test_no_git_hooks_conflicts_with_git_hooks_and_all(self) -> None:
        for flags in (
            ("--no-git-hooks", "--git-hooks"),
            ("--git-hooks", "--no-git-hooks"),
            ("--no-git-hooks", "--all"),
            ("--all", "--no-git-hooks"),
        ):
            with self.subTest(cmd="install", flags=flags):
                with self.assertRaisesRegex(InstallError, "Conflicting flags: --no-git-hooks"):
                    parse_install_args(flags)
                code, _, err = self.run_cli_inproc("install", *flags)
                self.assertEqual(code, 1)
                self.assertIn("--no-git-hooks", err)
            with self.subTest(cmd="uninstall", flags=flags):
                with self.assertRaisesRegex(InstallError, "Conflicting flags: --no-git-hooks"):
                    parse_uninstall_args(flags)
                code, _, err = self.run_cli_inproc("uninstall", *flags)
                self.assertEqual(code, 1)
                self.assertIn("--no-git-hooks", err)

    def test_valid_flag_combinations_and_workspace_aliases(self) -> None:
        cfg_default = parse_install_args(())
        self.assertTrue(cfg_default.auto_detect)
        self.assertEqual(cfg_default.mode, InstallMode.COPY)

        cfg_ag_sym = parse_install_args(("--antigravity", "--symlink"))
        self.assertFalse(cfg_ag_sym.auto_detect)
        self.assertTrue(cfg_ag_sym.install_antigravity)
        self.assertEqual(cfg_ag_sym.mode, InstallMode.SYMLINK)

        cfg_claude_hooks = parse_install_args(("--claude-hooks",))
        self.assertTrue(cfg_claude_hooks.install_claude)
        self.assertFalse(cfg_claude_hooks.disable_claude_hooks)

        cfg_claude = parse_install_args(("--claude",))
        self.assertTrue(cfg_claude.install_claude)
        self.assertFalse(cfg_claude.disable_claude_hooks)

        cfg_claude_no_hooks = parse_install_args(("--claude", "--no-claude-hooks"))
        self.assertTrue(cfg_claude_no_hooks.install_claude)
        self.assertTrue(cfg_claude_no_hooks.disable_claude_hooks)

        cfg_auto_sym = parse_install_args(("--auto", "--symlink"))
        self.assertTrue(cfg_auto_sym.auto_detect)
        self.assertEqual(cfg_auto_sym.mode, InstallMode.SYMLINK)

        for alias in ("--agents-workspace", "--codex-workspace", "--cursor-workspace"):
            cfg_ws = parse_install_args((alias, str(self.tmp / "ws")))
            self.assertEqual(cfg_ws.agents_workspace, self.tmp / "ws")
            self.assertFalse(cfg_ws.auto_detect)


class AutoDetectionAndModeTests(IsolatedInstallTestCase):
    def test_auto_detects_antigravity_claude_and_copilot(self) -> None:
        (self.home / ".gemini" / "antigravity").mkdir(parents=True)
        (self.home / ".claude").mkdir(parents=True)
        (self.appdata / "Code" / "User").mkdir(parents=True)

        code, out, err = self.run_cli_inproc("install", "--no-git-hooks")
        self.assertEqual(code, 0, err)
        self.assertIn("Detected: Google Antigravity", out)
        self.assertIn("Detected: Claude Code", out)
        self.assertIn("Detected: GitHub Copilot", out)
        self.assertTrue(
            (self.home / ".gemini" / "config" / "skills" / "craftsman" / "SKILL.md").is_file()
        )
        self.assertTrue((self.home / ".claude" / "commands" / "craftsman.md").is_file())
        self.assertTrue((self.home / ".copilot" / "skills" / "craftsman" / "SKILL.md").is_file())
        self.assertIsNone(self.git_global_get("core.hooksPath"))

    def test_auto_falls_back_to_copilot_when_nothing_detected(self) -> None:
        with mock.patch("shutil.which", return_value=None):
            code, out, err = self.run_cli_inproc("install", "--no-git-hooks")
        self.assertEqual(code, 0, err)
        self.assertIn("defaulting to GitHub Copilot user export", out)
        self.assertFalse((self.home / ".gemini").exists())
        self.assertFalse((self.home / ".claude").exists())
        self.assertTrue((self.home / ".copilot" / "skills" / "craftsman" / "SKILL.md").is_file())

    def test_copy_symlink_and_plugin_modes_and_drift_check(self) -> None:
        cfg_dir = self.home / ".gemini" / "config"

        # 1. Copy mode with git-hooks
        code, _, err = self.run_cli_inproc("install", "--copy", "--git-hooks")
        self.assertEqual(code, 0, err)
        skill_md = cfg_dir / "skills" / "craftsman" / "SKILL.md"
        self.assertTrue(skill_md.is_file())
        self.assertFalse((cfg_dir / "skills" / "craftsman").is_symlink())
        manifest = load_manifest(cfg_dir)
        assert manifest is not None
        self.assertEqual(manifest.mode, "copy")
        self.assertEqual(detect_antigravity_drift(REPO_ROOT, cfg_dir), [])

        # Mutate a passthrough hook to verify detect_antigravity_drift catches git-hooks drift
        (cfg_dir / "git-hooks" / "applypatch-msg").write_text("drifted\n", encoding="utf-8")
        self.assertEqual(detect_antigravity_drift(REPO_ROOT, cfg_dir), ["git-hooks/applypatch-msg"])

        # 2. Symlink mode refreshes both Antigravity and already-installed git-hooks
        code, _, err = self.run_cli_inproc("install", "--symlink")
        self.assertEqual(code, 0, err)
        self.assertTrue((cfg_dir / "skills" / "craftsman").is_symlink())
        self.assertTrue((cfg_dir / "rules" / "git_workflow.md").is_symlink())
        self.assertTrue((cfg_dir / "scripts" / "git-guard.py").is_symlink())
        self.assertTrue((cfg_dir / "craftsman").is_symlink())
        self.assertTrue(os.access(cfg_dir / "git-hooks" / "applypatch-msg", os.X_OK))
        manifest = load_manifest(cfg_dir)
        assert manifest is not None
        self.assertEqual(manifest.mode, "symlink")
        self.assertEqual(detect_antigravity_drift(REPO_ROOT, cfg_dir), [])

        # 3. Plugin mode (cleans up previous skills/rules symlinks and creates plugins/craftsman symlink)
        code, _, err = self.run_cli_inproc("install", "--plugin", "--copy")
        self.assertEqual(code, 1)
        code, _, err = self.run_cli_inproc("install", "--plugin")
        self.assertEqual(code, 0, err)
        plugin_link = cfg_dir / "plugins" / "craftsman"
        self.assertTrue(plugin_link.is_symlink())
        self.assertEqual(plugin_link.resolve(), REPO_ROOT.resolve())
        self.assertFalse((cfg_dir / "skills" / "craftsman").exists())
        manifest = load_manifest(cfg_dir)
        assert manifest is not None
        self.assertEqual(manifest.mode, "plugin")
        self.assertEqual(detect_antigravity_drift(REPO_ROOT, cfg_dir), [])
        (cfg_dir / "git-hooks" / "applypatch-msg").unlink()
        self.assertIn(
            f"git-hooks/applypatch-msg (missing in {cfg_dir})",
            detect_antigravity_drift(REPO_ROOT, cfg_dir),
        )


class BackupAndMergeSafetyTests(IsolatedInstallTestCase):
    def test_foreign_skill_and_rule_are_backed_up_and_idempotent_reinstall_makes_no_second_backup(
        self,
    ) -> None:
        cfg_dir = self.home / ".gemini" / "config"
        foreign_skill_dir = cfg_dir / "skills" / "craftsman"
        foreign_skill_dir.mkdir(parents=True)
        (foreign_skill_dir / "SKILL.md").write_text("user custom skill\n", encoding="utf-8")
        (foreign_skill_dir / "extra_notes.md").write_text("do not lose me\n", encoding="utf-8")

        foreign_rule = cfg_dir / "rules" / "git_workflow.md"
        foreign_rule.parent.mkdir(parents=True)
        foreign_rule.write_text("user custom rule\n", encoding="utf-8")

        code, out, err = self.run_cli_inproc("install", "--antigravity")
        self.assertEqual(code, 0, err)
        self.assertIn("backed up existing", out)

        skill_backups = list((cfg_dir / "skills").glob("craftsman.bak.*"))
        rule_backups = list((cfg_dir / "rules").glob("git_workflow.md.bak.*"))
        self.assertEqual(len(skill_backups), 1)
        self.assertEqual(len(rule_backups), 1)
        self.assertEqual(
            (skill_backups[0] / "SKILL.md").read_text(encoding="utf-8"), "user custom skill\n"
        )
        self.assertEqual(
            (skill_backups[0] / "extra_notes.md").read_text(encoding="utf-8"), "do not lose me\n"
        )
        self.assertEqual(rule_backups[0].read_text(encoding="utf-8"), "user custom rule\n")

        # Second install (idempotent re-install) replaces manifest-owned paths in place without new backups
        code2, _, err2 = self.run_cli_inproc("install", "--antigravity")
        self.assertEqual(code2, 0, err2)
        self.assertEqual(len(list((cfg_dir / "skills").glob("craftsman.bak.*"))), 1)
        self.assertEqual(len(list((cfg_dir / "rules").glob("git_workflow.md.bak.*"))), 1)

        # Uninstall removes manifest-owned paths and leaves foreign backups intact
        code3, _, err3 = self.run_cli_inproc("uninstall", "--antigravity")
        self.assertEqual(code3, 0, err3)
        self.assertFalse((cfg_dir / "skills" / "craftsman").exists())
        self.assertFalse((cfg_dir / "rules" / "git_workflow.md").exists())
        self.assertTrue(skill_backups[0].is_dir())
        self.assertTrue(rule_backups[0].is_file())

    def test_hooks_json_merge_keeps_enabled_flag_backs_up_and_unmerges_on_uninstall(self) -> None:
        cfg_dir = self.home / ".gemini" / "config"
        cfg_dir.mkdir(parents=True)
        hooks_json = cfg_dir / "hooks.json"
        original_hooks: dict[str, Any] = {
            "craftsman-git-guard": {"enabled": False, "PreToolUse": []},
            "my-custom-hook": {"enabled": True, "Stop": [{"type": "command", "command": "echo hi"}]},
        }
        original_raw = json.dumps(original_hooks, indent=2) + "\n"
        hooks_json.write_text(original_raw, encoding="utf-8")

        code, _, err = self.run_cli_inproc("install", "--antigravity")
        self.assertEqual(code, 0, err)

        bak = cfg_dir / "hooks.json.bak"
        self.assertTrue(bak.is_file())
        self.assertEqual(bak.read_text(encoding="utf-8"), original_raw)

        merged = json.loads(hooks_json.read_text(encoding="utf-8"))
        self.assertIs(merged["craftsman-git-guard"]["enabled"], False)
        self.assertTrue(len(merged["craftsman-git-guard"]["PreToolUse"]) > 0)
        self.assertIs(merged["craftsman-stop-gate"]["enabled"], True)
        self.assertEqual(merged["my-custom-hook"], original_hooks["my-custom-hook"])

        # Uninstall strips Craftsman keys and preserves user's my-custom-hook
        code_un, _, err_un = self.run_cli_inproc("uninstall", "--antigravity")
        self.assertEqual(code_un, 0, err_un)
        self.assertTrue(hooks_json.is_file())
        after_un = json.loads(hooks_json.read_text(encoding="utf-8"))
        self.assertEqual(after_un, {"my-custom-hook": original_hooks["my-custom-hook"]})

    def test_corrupt_hooks_json_backs_up_and_fails(self) -> None:
        cfg_dir = self.home / ".gemini" / "config"
        cfg_dir.mkdir(parents=True)
        hooks_json = cfg_dir / "hooks.json"
        hooks_json.write_text("{broken json", encoding="utf-8")

        code, _, err = self.run_cli_inproc("install", "--antigravity")
        self.assertEqual(code, 1)
        self.assertIn("Failed to parse existing", err)
        self.assertEqual((cfg_dir / "hooks.json.bak").read_text(encoding="utf-8"), "{broken json")


class GitHooksChainingAndUninstallTests(IsolatedInstallTestCase):
    def test_stale_previous_hooks_path_removed_when_no_previous_hooks_path(self) -> None:
        hooks_dir = self.craftsman_home / "git-hooks"
        hooks_dir.mkdir(parents=True)
        stale_file = hooks_dir / ".previous_hooks_path"
        stale_file.write_text("/stale/nonexistent/hooks\n", encoding="utf-8")

        code, out, err = self.run_cli_inproc("install", "--git-hooks")
        self.assertEqual(code, 0, err)
        self.assertFalse(stale_file.exists())
        self.assertEqual(self.git_global_get("core.hooksPath"), str(hooks_dir))
        self.assertIn("craftsman uninstall", out)
        self.assertIn("--no-git-hooks", out)

        code_un, _, err_un = self.run_cli_inproc("uninstall", "--git-hooks")
        self.assertEqual(code_un, 0, err_un)
        self.assertIsNone(self.git_global_get("core.hooksPath"))

    def test_previous_hooks_path_round_trip_and_user_modified_hooks_path_preserved(self) -> None:
        prev_dir = self.home / "my-global-hooks"
        prev_dir.mkdir(parents=True)
        self.git_global_set("core.hooksPath", "~/my-global-hooks")

        # 1. Install chains to ~/my-global-hooks
        code, _, err = self.run_cli_inproc("install", "--git-hooks")
        self.assertEqual(code, 0, err)
        hooks_dir = self.craftsman_home / "git-hooks"
        self.assertEqual(self.git_global_get("core.hooksPath"), str(hooks_dir))
        prev_file = hooks_dir / ".previous_hooks_path"
        self.assertTrue(prev_file.is_file())
        self.assertEqual(prev_file.read_text(encoding="utf-8").strip(), str(prev_dir))

        # 2. Idempotent re-install keeps .previous_hooks_path pointing to prev_dir
        code2, _, err2 = self.run_cli_inproc("install", "--git-hooks")
        self.assertEqual(code2, 0, err2)
        self.assertEqual(prev_file.read_text(encoding="utf-8").strip(), str(prev_dir))

        # 3. Uninstall restores ~/my-global-hooks and removes .previous_hooks_path
        code3, _, err3 = self.run_cli_inproc("uninstall", "--git-hooks")
        self.assertEqual(code3, 0, err3)
        self.assertEqual(self.git_global_get("core.hooksPath"), "~/my-global-hooks")
        self.assertFalse(prev_file.exists())

        # 4. Second uninstall is an idempotent no-op with exit code 0
        code4, out4, err4 = self.run_cli_inproc("uninstall", "--git-hooks")
        self.assertEqual(code4, 0, err4)
        self.assertIn("Nothing to uninstall", out4)
        self.assertEqual(self.git_global_get("core.hooksPath"), "~/my-global-hooks")

        # 5. Re-install, then user changes core.hooksPath to something else -> uninstall leaves it and warns
        self.run_cli_inproc("install", "--git-hooks")
        user_new_hooks = str(self.tmp / "other-hooks")
        self.git_global_set("core.hooksPath", user_new_hooks)
        code5, _, err5 = self.run_cli_inproc("uninstall", "--git-hooks")
        self.assertEqual(code5, 0)
        self.assertEqual(self.git_global_get("core.hooksPath"), user_new_hooks)
        self.assertIn("Leaving git config --global core.hooksPath", err5)

    def test_passthrough_hook_forwards_stdin_to_both_hooks_and_propagates_exit_code(self) -> None:
        prev_hooks_dir = self.tmp / "prev_hooks"
        prev_hooks_dir.mkdir(parents=True)
        self.git_global_set("core.hooksPath", str(prev_hooks_dir))

        code, _, err = self.run_cli_inproc("install", "--git-hooks")
        self.assertEqual(code, 0, err)
        installed_hooks_dir = self.craftsman_home / "git-hooks"

        # Template itself must NOT be installed as a hook named _passthrough.sh
        self.assertFalse((installed_hooks_dir / PASSTHROUGH_TEMPLATE_NAME).exists())
        for hook_name in STANDARD_PASSTHROUGH_GIT_HOOKS:
            self.assertTrue((installed_hooks_dir / hook_name).is_file(), hook_name)

        # Create a real git repository with a local hook and a previous-hooksPath hook
        repo = self.tmp / "work_repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, env=self.env, capture_output=True, check=True)
        local_hooks_dir = repo / ".git" / "hooks"
        local_hooks_dir.mkdir(parents=True, exist_ok=True)

        prev_out = self.tmp / "prev_stdin.txt"
        local_out = self.tmp / "local_stdin.txt"

        # Both hooks record stdin and arguments for `post-rewrite` (a standard passthrough hook)
        prev_hook = prev_hooks_dir / "post-rewrite"
        prev_hook.write_text(
            f'#!/usr/bin/env bash\nset -euo pipefail\ncat > "{prev_out}"\n',
            encoding="utf-8",
        )
        prev_hook.chmod(0o755)

        local_hook = local_hooks_dir / "post-rewrite"
        local_hook.write_text(
            f'#!/usr/bin/env bash\nset -euo pipefail\ncat > "{local_out}"\n',
            encoding="utf-8",
        )
        local_hook.chmod(0o755)

        payload = "oldsha1 newsha1\noldsha2 newsha2\n"
        proc = subprocess.run(
            [str(installed_hooks_dir / "post-rewrite"), "rebase"],
            cwd=repo,
            env=self.env,
            input=payload,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(prev_out.read_text(encoding="utf-8"), payload)
        self.assertEqual(local_out.read_text(encoding="utf-8"), payload)

        # Now make local hook fail with exit code 23 and run real `git commit` via `pre-commit`
        prev_pre_commit_ran = self.tmp / "prev_pre_commit_ran"
        (prev_hooks_dir / "pre-commit").write_text(
            f'#!/usr/bin/env bash\ntouch "{prev_pre_commit_ran}"\n',
            encoding="utf-8",
        )
        (prev_hooks_dir / "pre-commit").chmod(0o755)
        (local_hooks_dir / "pre-commit").write_text(
            "#!/usr/bin/env bash\necho 'blocked by local pre-commit' >&2\nexit 23\n",
            encoding="utf-8",
        )
        (local_hooks_dir / "pre-commit").chmod(0o755)

        (repo / "hello.txt").write_text("world\n", encoding="utf-8")
        subprocess.run(
            ["git", "add", "hello.txt"], cwd=repo, env=self.env, capture_output=True, check=True
        )
        commit_proc = subprocess.run(
            [
                "git",
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.com",
                "commit",
                "-m",
                "feat: test commit",
            ],
            cwd=repo,
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(commit_proc.returncode, 0)
        self.assertTrue(prev_pre_commit_ran.is_file())
        self.assertIn("blocked by local pre-commit", commit_proc.stderr)

        # Also exercise installed commit-msg and pre-push hooks end-to-end
        bad_msg_file = self.tmp / "COMMIT_EDITMSG"
        bad_msg_file.write_text(
            "feat: test\n\nCo-authored-by: Claude <noreply@anthropic.com>\n",
            encoding="utf-8",
        )
        cmsg_proc = subprocess.run(
            [str(installed_hooks_dir / "commit-msg"), str(bad_msg_file)],
            cwd=repo,
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(cmsg_proc.returncode, 0)

        prev_pre_push_out = self.tmp / "prev_pre_push.txt"
        (prev_hooks_dir / "pre-push").write_text(
            f'#!/usr/bin/env bash\ncat > "{prev_pre_push_out}"\n',
            encoding="utf-8",
        )
        (prev_hooks_dir / "pre-push").chmod(0o755)
        push_stdin = "refs/heads/feat/x 0000 refs/heads/feat/x 0000\n"
        pp_proc = subprocess.run(
            [str(installed_hooks_dir / "pre-push"), "origin", "https://example.com/repo.git"],
            cwd=repo,
            env=self.env,
            input=push_stdin,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(pp_proc.returncode, 0, pp_proc.stderr)
        self.assertEqual(prev_pre_push_out.read_text(encoding="utf-8"), push_stdin)

    def test_git_hooks_symlink_target_fallback_stale_applypatch_msg_and_copy_mode(self) -> None:
        cfg_dir = self.home / ".gemini" / "config"
        hooks_dir = cfg_dir / "git-hooks"

        # 1. Symlink install with missing _passthrough.sh in hooks_dir (and stale applypatch-msg),
        #    but _passthrough.sh present next to the symlink target in the repo.
        code, _, err = self.run_cli_inproc("install", "--symlink", "--git-hooks")
        self.assertEqual(code, 0, err)
        subprocess.run(
            ["git", "config", "--global", "--unset-all", "core.hooksPath"],
            env=self.env,
            capture_output=True,
            check=False,
        )
        self.assertFalse((hooks_dir / PASSTHROUGH_TEMPLATE_NAME).exists())
        self.assertTrue((hooks_dir / "commit-msg").is_symlink())

        # Replace applypatch-msg with an old-format passthrough script without functions
        (hooks_dir / "applypatch-msg").unlink()
        (hooks_dir / "applypatch-msg").write_text(
            "#!/usr/bin/env bash\nset -euo pipefail\n# legacy passthrough without functions\n",
            encoding="utf-8",
        )
        (hooks_dir / "applypatch-msg").chmod(0o755)

        # Also test a relative symlink target for commit-msg
        (hooks_dir / "commit-msg").unlink()
        rel_target = os.path.relpath(REPO_ROOT / "git-hooks" / "commit-msg", hooks_dir)
        (hooks_dir / "commit-msg").symlink_to(rel_target)

        repo = self.tmp / "commit_hook_repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, env=self.env, capture_output=True, check=True)
        (repo / "f.txt").write_text("v1\n", encoding="utf-8")
        subprocess.run(["git", "add", "f.txt"], cwd=repo, env=self.env, capture_output=True, check=True)

        ok_commit = subprocess.run(
            [
                "git",
                "-c",
                f"core.hooksPath={hooks_dir}",
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.com",
                "commit",
                "-m",
                "feat: valid symlink commit",
            ],
            cwd=repo,
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(ok_commit.returncode, 0, ok_commit.stderr)

        (repo / "f.txt").write_text("v2\n", encoding="utf-8")
        subprocess.run(["git", "add", "f.txt"], cwd=repo, env=self.env, capture_output=True, check=True)
        ai_commit = subprocess.run(
            [
                "git",
                "-c",
                f"core.hooksPath={hooks_dir}",
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.com",
                "commit",
                "-m",
                "feat: bad\n\nCo-authored-by: Claude <noreply@anthropic.com>",
            ],
            cwd=repo,
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(ai_commit.returncode, 0)
        self.assertIn("AI attribution", ai_commit.stderr)

        # 2. Old applypatch-msg without functions and no _passthrough.sh next to symlink target
        isolated_target_dir = self.tmp / "isolated_hook_target"
        isolated_target_dir.mkdir()
        (isolated_target_dir / "commit-msg").write_text(
            (REPO_ROOT / "git-hooks" / "commit-msg").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        (isolated_target_dir / "commit-msg").chmod(0o755)
        (hooks_dir / "commit-msg").unlink()
        (hooks_dir / "commit-msg").symlink_to(isolated_target_dir / "commit-msg")

        stale_commit = subprocess.run(
            [
                "git",
                "-c",
                f"core.hooksPath={hooks_dir}",
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.com",
                "commit",
                "-m",
                "feat: should fail with repair instructions",
            ],
            cwd=repo,
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(stale_commit.returncode, 1)
        self.assertIn(
            "are out of date (missing _passthrough.sh helpers); run ./install.sh --git-hooks from the Craftsman repo, or bypass once with git -c core.hooksPath= commit",
            stale_commit.stderr,
        )
        self.assertNotIn("command not found", stale_commit.stderr)

        # craftsman doctor reports the broken hooks as an error with a repair command
        problems = inspect_git_hooks(str(hooks_dir), REPO_ROOT, self.home)
        self.assertEqual(len(problems), 1)
        self.assertIn("cannot find _passthrough.sh helpers", problems[0])
        self.assertIn("./install.sh --git-hooks", problems[0])
        doc_out = io.StringIO()
        doc_err = io.StringIO()
        with (
            mock.patch(
                "craftsman.cli.git_config_get",
                side_effect=lambda k: str(hooks_dir) if k == "core.hooksPath" else "Test",
            ),
            contextlib.redirect_stdout(doc_out),
            contextlib.redirect_stderr(doc_err),
        ):
            doc_code = run_doctor(REPO_ROOT)
        self.assertEqual(doc_code, 1)
        self.assertIn("cannot find _passthrough.sh helpers", doc_err.getvalue())

        # 3. Copy mode continues to work (helpers loaded from applypatch-msg copy)
        code_copy, _, err_copy = self.run_cli_inproc("install", "--copy", "--git-hooks")
        self.assertEqual(code_copy, 0, err_copy)
        subprocess.run(
            ["git", "config", "--global", "--unset-all", "core.hooksPath"],
            env=self.env,
            capture_output=True,
            check=False,
        )
        self.assertFalse((hooks_dir / "commit-msg").is_symlink())
        self.assertFalse((hooks_dir / PASSTHROUGH_TEMPLATE_NAME).exists())

        copy_ok = subprocess.run(
            [
                "git",
                "-c",
                f"core.hooksPath={hooks_dir}",
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.com",
                "commit",
                "-m",
                "feat: copy mode commit",
            ],
            cwd=repo,
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(copy_ok.returncode, 0, copy_ok.stderr)

        (repo / "f.txt").write_text("v3\n", encoding="utf-8")
        subprocess.run(["git", "add", "f.txt"], cwd=repo, env=self.env, capture_output=True, check=True)
        copy_ai = subprocess.run(
            [
                "git",
                "-c",
                f"core.hooksPath={hooks_dir}",
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.com",
                "commit",
                "-m",
                "feat: bad\n\nCo-authored-by: Claude <noreply@anthropic.com>",
            ],
            cwd=repo,
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(copy_ai.returncode, 0)

    def test_claude_target_installs_gate_hooks_by_default_and_can_skip_them(self) -> None:
        settings_path = self.home / ".claude" / "settings.json"
        self.run_cli_inproc("install", "--claude", "--no-claude-hooks")
        self.assertFalse(settings_path.exists())
        self.assertTrue((self.home / ".claude" / "commands" / "craftsman.md").is_file())

        self.run_cli_inproc("install", "--claude")
        merged = json.loads(settings_path.read_text(encoding="utf-8"))
        self.assertIn("Stop", merged["hooks"])
        self.assertIn("SubagentStart", merged["hooks"])

        self.run_cli_inproc("uninstall", "--claude")
        self.assertFalse(remove_claude_hooks(settings_path))

    def test_claude_settings_unmerge_and_standalone_craftsman_clone_and_security_invariants(
        self,
    ) -> None:
        # 1. Pre-existing ~/.claude/settings.json with user keys and foreign hooks
        claude_dir = self.home / ".claude"
        claude_dir.mkdir(parents=True)
        settings_path = claude_dir / "settings.json"
        initial_settings: dict[str, Any] = {
            "theme": "dark",
            "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo custom-stop"}]}]},
        }
        settings_path.write_text(json.dumps(initial_settings, indent=2) + "\n", encoding="utf-8")

        # Dry-run uninstall when no Craftsman hooks are present reports nothing to uninstall
        _, dry_out, _ = self.run_cli_inproc("uninstall", "--claude", "--dry-run")
        self.assertIn("Nothing to uninstall", dry_out)

        self.run_cli_inproc("install", "--claude-hooks")
        self.run_cli_inproc("install", "--claude")
        self.run_cli_inproc("uninstall", "--claude")
        self.assertTrue(settings_path.is_file())
        restored_settings = json.loads(settings_path.read_text(encoding="utf-8"))
        self.assertEqual(restored_settings, initial_settings)
        self.assertFalse(remove_claude_hooks(settings_path))

        # 2. Standalone clone at ~/.craftsman: commit-msg in repo_dir/git-hooks is never deleted
        fake_repo = self.craftsman_home
        (fake_repo / "skills" / "s1").mkdir(parents=True)
        (fake_repo / "skills" / "s1" / "SKILL.md").write_text(
            "---\nname: s1\ndescription: desc\n---\n" + ("body " * 30) + "\n",
            encoding="utf-8",
        )
        (fake_repo / "rules").mkdir(parents=True)
        (fake_repo / "scripts").mkdir(parents=True)
        (fake_repo / "git-hooks").mkdir(parents=True)
        (fake_repo / "git-hooks" / "commit-msg").write_text(
            (REPO_ROOT / "git-hooks" / "commit-msg").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        (fake_repo / "git-hooks" / "_passthrough.sh").write_text(
            (REPO_ROOT / "git-hooks" / "_passthrough.sh").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        self.assertEqual(
            self.run_cli_inproc("install", "--repo-dir", str(fake_repo), "--git-hooks")[0], 0
        )
        self.assertEqual(
            self.run_cli_inproc("install", "--repo-dir", str(fake_repo), "--git-hooks")[0], 0
        )
        self.assertTrue((fake_repo / "git-hooks" / "commit-msg").is_file())
        self.assertEqual(
            self.run_cli_inproc("uninstall", "--repo-dir", str(fake_repo), "--git-hooks")[0], 0
        )
        self.assertTrue((fake_repo / "git-hooks" / "commit-msg").is_file())

        # 3. Workspace containment against crafted manifest and symlinked AGENTS.md
        outside_victim = self.tmp / "outside_victim.txt"
        outside_victim.write_text("do not delete\n", encoding="utf-8")
        ws_sec = self.tmp / "ws_sec"
        ws_sec.mkdir()
        write_text_atomic(
            ws_sec / MANIFEST_FILENAME,
            json.dumps(
                {
                    "schema_version": 1,
                    "version": "3.0.0",
                    "mode": "copy",
                    "created_paths": [str(outside_victim)],
                    "created_dirs": [],
                    "targets": {
                        "agents_workspace": {
                            "target": "agents_workspace",
                            "mode": "copy",
                            "version": "3.0.0",
                            "created_paths": [str(outside_victim)],
                            "created_dirs": [],
                            "modified_files": {},
                        }
                    },
                }
            ),
        )
        self.run_cli_inproc("uninstall", "--agents-workspace", str(ws_sec))
        self.assertTrue(outside_victim.is_file())

        outside_md = self.tmp / "outside_md.md"
        outside_md.write_text("secret\n", encoding="utf-8")
        (ws_sec / "AGENTS.md").symlink_to(outside_md)
        self.run_cli_inproc("install", "--agents-workspace", str(ws_sec))
        self.assertEqual(outside_md.read_text(encoding="utf-8"), "secret\n")

        # User-added section in created AGENTS.md survives uninstall
        with open(ws_sec / "AGENTS.md", "a", encoding="utf-8") as fh:
            fh.write("\n# User Added Section\nKeep me!\n")
        self.run_cli_inproc("uninstall", "--agents-workspace", str(ws_sec))
        self.assertTrue((ws_sec / "AGENTS.md").is_file())
        self.assertIn("Keep me!", (ws_sec / "AGENTS.md").read_text(encoding="utf-8"))

        # 4. Manifest validation, hashability, tilde expansion, and explicit home isolation
        self.assertEqual(_expand_tilde("~//my-hooks", self.home), str(self.home / "my-hooks"))
        other_home = self.tmp / "other_home"
        other_home.mkdir()
        self.assertIn(other_home, default_vscode_prompts_dir(other_home).parents)
        self.assertIn(other_home, resolve_git_hooks_base_dir(other_home).parents)

        bm = BaseManifest.from_targets(
            {
                "antigravity": TargetManifest(target="antigravity", mode="copy", version="3.0.0"),
                "git_hooks": TargetManifest(target="git_hooks", mode="symlink", version="3.0.0"),
            }
        )
        self.assertEqual(bm.mode, "copy")
        self.assertIsInstance(hash(bm), int)

        bad_dir = self.tmp / "bad_manifest"
        bad_dir.mkdir()
        write_text_atomic(bad_dir / MANIFEST_FILENAME, '{"created_paths": "/tmp/x"}\n')
        with self.assertRaises(InstallError):
            load_manifest(bad_dir)

        # 4. Bundled _assets package resolution in _install_scripts_to
        fake_pkg = self.tmp / "site" / "craftsman"
        fake_assets = fake_pkg / "_assets"
        (fake_pkg).mkdir(parents=True)
        (fake_pkg / "__init__.py").write_text("__version__ = '3.0.0'\n", encoding="utf-8")
        (fake_assets / "scripts").mkdir(parents=True)
        sess = _TargetInstallSession.begin(self.tmp / "out_base", TargetKind.ANTIGRAVITY, self.home)
        _install_scripts_to(
            fake_assets, self.tmp / "out_base", InstallMode.COPY, sess, InstallTracker(dry_run=False)
        )
        self.assertTrue((self.tmp / "out_base" / "craftsman" / "__init__.py").is_file())

        save_manifest(bad_dir, bm, dry_run=False)
        self.assertEqual((bad_dir / MANIFEST_FILENAME).stat().st_mode & 0o777, 0o644)


class DryRunAndBootstrapTests(IsolatedInstallTestCase):
    def test_dry_run_install_and_uninstall_leave_environment_byte_identical(self) -> None:
        before_home = _snapshot_tree(self.home)
        before_gitcfg = self.git_config_global.read_bytes()

        code, out, err = self.run_cli_inproc("install", "--all", "--claude-hooks", "--dry-run")
        self.assertEqual(code, 0, err)
        self.assertIn("[DRY-RUN]", out)
        self.assertEqual(_snapshot_tree(self.home), before_home)
        self.assertEqual(self.git_config_global.read_bytes(), before_gitcfg)

        # Now do a real install, snapshot, and verify uninstall --dry-run leaves everything byte-identical
        code_real, _, err_real = self.run_cli_inproc("install", "--all", "--claude-hooks")
        self.assertEqual(code_real, 0, err_real)
        installed_home = _snapshot_tree(self.home)
        installed_gitcfg = self.git_config_global.read_bytes()

        code_dry_un, out_dry_un, err_dry_un = self.run_cli_inproc("uninstall", "--dry-run")
        self.assertEqual(code_dry_un, 0, err_dry_un)
        self.assertIn("[DRY-RUN]", out_dry_un)
        self.assertEqual(_snapshot_tree(self.home), installed_home)
        self.assertEqual(self.git_config_global.read_bytes(), installed_gitcfg)

    def test_install_sh_bootstrap_end_to_end_and_clean_uninstall_and_source_modes_unchanged(
        self,
    ) -> None:
        source_modes_before = {
            str(p.relative_to(REPO_ROOT)): p.stat().st_mode & 0o777
            for folder in ("scripts", "git-hooks")
            for p in sorted((REPO_ROOT / folder).iterdir())
            if p.is_file()
        }

        proc = subprocess.run(
            ["bash", str(REPO_ROOT / "install.sh"), "--all", "--claude-hooks"],
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)

        source_modes_after = {
            str(p.relative_to(REPO_ROOT)): p.stat().st_mode & 0o777
            for folder in ("scripts", "git-hooks")
            for p in sorted((REPO_ROOT / folder).iterdir())
            if p.is_file()
        }
        self.assertEqual(source_modes_before, source_modes_after)

        # Full uninstall restores temp HOME to empty and unsets core.hooksPath
        un_code, _, un_err = self.run_cli_inproc("uninstall")
        self.assertEqual(un_code, 0, un_err)
        self.assertEqual(list(self.home.iterdir()), [])
        self.assertIsNone(self.git_global_get("core.hooksPath"))

        # Second uninstall is idempotent
        un_code2, _, un_err2 = self.run_cli_inproc("uninstall")
        self.assertEqual(un_code2, 0, un_err2)

    def test_workspace_install_and_uninstall_preserves_existing_markdown(self) -> None:
        ws = self.tmp / "project"
        ws.mkdir()
        agents_md = ws / "AGENTS.md"
        agents_md.write_text("# Team Guide\n\nCustom rules.\n", encoding="utf-8")

        code, _, err = self.run_cli_inproc(
            "install",
            "--copilot-workspace",
            str(ws),
            "--agents-workspace",
            str(ws),
        )
        self.assertEqual(code, 0, err)
        self.assertTrue((ws / "AGENTS.md.bak").is_file())
        self.assertIn("<!-- craftsman:begin -->", agents_md.read_text(encoding="utf-8"))
        self.assertTrue((ws / ".cursor" / "rules" / "craftsman.mdc").is_file())
        self.assertTrue((ws / ".github" / "hooks" / "craftsman.json").is_file())

        un_code, _, un_err = self.run_cli_inproc(
            "uninstall",
            "--copilot-workspace",
            str(ws),
            "--agents-workspace",
            str(ws),
        )
        self.assertEqual(un_code, 0, un_err)
        self.assertNotIn("<!-- craftsman:begin -->", agents_md.read_text(encoding="utf-8"))
        self.assertIn("# Team Guide", agents_md.read_text(encoding="utf-8"))
        self.assertFalse((ws / ".cursor").exists())
        self.assertFalse((ws / ".github").exists())
        self.assertFalse((ws / MANIFEST_FILENAME).exists())


class DriftCheckTests(unittest.TestCase):
    def test_drift_check_reports_a_changed_hooks_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config"
            skill = cfg / "skills" / "craftsman" / "SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_bytes((REPO_ROOT / "skills" / "craftsman" / "SKILL.md").read_bytes())
            hooks = json.loads((REPO_ROOT / "hooks.json").read_text(encoding="utf-8"))
            hooks["craftsman-stop-gate"]["Stop"][0]["timeout"] += 1
            (cfg / "hooks.json").write_text(json.dumps(hooks), encoding="utf-8")
            self.assertIn("hooks.json", detect_antigravity_drift(REPO_ROOT, cfg))


if __name__ == "__main__":
    unittest.main()
