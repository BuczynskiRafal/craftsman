"""Tests for `craftsman install` and the `install.sh` bootstrap."""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from typing import Any
from unittest import mock

from craftsman.doctor import detect_antigravity_drift, scan_copilot_duplicates
from craftsman.export import (
    CRAFTSMAN_BEGIN_MARKER,
    default_vscode_prompts_dir,
    is_craftsman_hook_command,
    iter_skills,
)
from craftsman.files import BACKUP_DIR_NAME
from craftsman.install import parse_install_args
from craftsman.manifest import MANIFEST_FILENAME, TargetKind, load_manifest
from craftsman.staging import InstallMode

from tests.support import REPO_ROOT, IsolatedHomeTestCase, snapshot_tree

SKILL_NAMES = [skill.name for skill in iter_skills(REPO_ROOT)]


def claude_hook_commands(settings: dict[str, Any]) -> list[str]:
    return [
        entry["command"]
        for groups in settings.get("hooks", {}).values()
        for group in groups
        for entry in group["hooks"]
        if is_craftsman_hook_command(entry["command"])
    ]


class InstallTestCase(IsolatedHomeTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.config_dir = self.home / ".gemini" / "config"
        self.claude_dir = self.home / ".claude"


class ArgumentTests(InstallTestCase):
    def test_no_target_auto_detects(self) -> None:
        config = parse_install_args([])

        self.assertEqual((config.auto_detect, config.mode), (True, InstallMode.COPY))

    def test_named_target_installs_only_that_target(self) -> None:
        config = parse_install_args(["--claude", "--symlink"])

        self.assertEqual(
            (config.auto_detect, config.claude, config.antigravity, config.mode),
            (False, True, False, InstallMode.SYMLINK),
        )

    def test_two_modes_are_rejected(self) -> None:
        result = self.install("--copy", "--plugin")

        self.assertEqual(result.code, 2)
        self.assertIn("not allowed with argument", result.err)

    def test_removed_git_hooks_flags_are_rejected(self) -> None:
        for flag in ("--git-hooks", "--no-git-hooks"):
            with self.subTest(flag=flag):
                self.assertEqual(self.install(flag).code, 2)

    def test_no_claude_hooks_without_the_claude_target_is_rejected(self) -> None:
        self.assertEqual(self.install("--copilot", "--no-claude-hooks").code, 2)


class AutoDetectionTests(InstallTestCase):
    def test_detected_platforms_are_installed(self) -> None:
        (self.home / ".gemini" / "antigravity").mkdir(parents=True)
        self.claude_dir.mkdir()

        result = self.install()

        self.assert_ok(result)
        self.assertIn("Detected: Google Antigravity", result.out)
        self.assertIn("Detected: Claude Code", result.out)
        self.assertTrue((self.config_dir / "skills" / "craftsman" / "SKILL.md").is_file())
        self.assertTrue((self.claude_dir / "commands" / "craftsman.md").is_file())
        self.assertIsNone(self.hooks_path())

    def test_nothing_detected_installs_for_copilot(self) -> None:
        with mock.patch("shutil.which", return_value=None):
            result = self.install()

        self.assert_ok(result)
        self.assertIn("installing for GitHub Copilot", result.out)
        self.assertFalse((self.home / ".gemini").exists())
        self.assertTrue((self.home / ".copilot" / "skills" / "craftsman" / "SKILL.md").is_file())


class AntigravityTests(InstallTestCase):
    def test_copy_mode_installs_copies_without_drift(self) -> None:
        self.assert_ok(self.install("--antigravity"))

        self.assertFalse((self.config_dir / "skills" / "craftsman").is_symlink())
        self.assertTrue((self.config_dir / "craftsman" / "__init__.py").is_file())
        self.assertEqual(detect_antigravity_drift(REPO_ROOT, self.config_dir), [])

    def test_symlink_mode_links_to_the_checkout(self) -> None:
        self.assert_ok(self.install("--antigravity", "--symlink"))

        for path in (
            "skills/craftsman",
            "rules/ultra_mode.md",
            "scripts/craftsman-scope-guard.py",
            "craftsman",
        ):
            self.assertTrue((self.config_dir / path).is_symlink(), path)

    def test_plugin_mode_replaces_a_copy_install(self) -> None:
        self.assert_ok(self.install("--antigravity"))

        self.assert_ok(self.install("--antigravity", "--plugin"))

        self.assertEqual((self.config_dir / "plugins" / "craftsman").resolve(), REPO_ROOT.resolve())
        self.assertFalse((self.config_dir / "skills" / "craftsman").exists())
        self.assertFalse((self.config_dir / "hooks.json").exists())

    def test_users_skill_is_moved_where_it_is_not_discovered(self) -> None:
        skill = self.config_dir / "skills" / "craftsman"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("user custom skill\n", encoding="utf-8")

        result = self.install("--antigravity")

        self.assert_ok(result)
        (backup,) = (self.config_dir / "skills" / BACKUP_DIR_NAME).iterdir()
        self.assertEqual((backup / "SKILL.md").read_text(encoding="utf-8"), "user custom skill\n")
        self.assertEqual(
            sorted(p.parent.name for p in (self.config_dir / "skills").glob("*/SKILL.md")),
            sorted(SKILL_NAMES),
        )

    def test_reinstall_replaces_its_own_files_without_backups(self) -> None:
        self.assert_ok(self.install("--antigravity"))

        result = self.install("--antigravity")

        self.assert_ok(result)
        self.assertIn("0 backed up", result.out)
        self.assertFalse((self.config_dir / "skills" / BACKUP_DIR_NAME).exists())

    def test_hooks_json_merge_keeps_what_the_user_set(self) -> None:
        self.config_dir.mkdir(parents=True)
        original = {
            "craftsman-scope-guard": {"enabled": False, "PreToolUse": []},
            "my-hook": {"enabled": True, "Stop": [{"type": "command", "command": "echo hi"}]},
        }
        (self.config_dir / "hooks.json").write_text(json.dumps(original), encoding="utf-8")

        self.assert_ok(self.install("--antigravity"))

        merged = json.loads((self.config_dir / "hooks.json").read_text(encoding="utf-8"))
        self.assertIs(merged["craftsman-scope-guard"]["enabled"], False)
        self.assertTrue(merged["craftsman-scope-guard"]["PreToolUse"])
        self.assertEqual(merged["my-hook"], original["my-hook"])
        self.assertEqual(
            json.loads((self.config_dir / "hooks.json.bak").read_text(encoding="utf-8")), original
        )

    def test_upgrade_from_3x_replaces_git_guard_with_scope_guard_in_hooks_json(self) -> None:
        self.config_dir.mkdir(parents=True)
        hooks_path = self.config_dir / "hooks.json"
        legacy_hooks = {
            "craftsman-git-guard": {
                "enabled": False,
                "PreToolUse": [{"matcher": "run_command", "hooks": []}],
            },
            "craftsman-stop-gate": {"enabled": False, "Stop": []},
            "my-hook": {"enabled": True, "Stop": [{"type": "command", "command": "echo hi"}]},
        }
        hooks_path.write_text(json.dumps(legacy_hooks), encoding="utf-8")
        legacy_manifest = {
            "targets": {
                "antigravity": {
                    "created_paths": [],
                    "created_dirs": [],
                    "modified_files": {str(hooks_path): "hooks_json"},
                    "hooks_json_keys": ["craftsman-git-guard", "craftsman-stop-gate"],
                    "previous_hooks_path": None,
                    "hooks_dir": None,
                }
            }
        }
        (self.config_dir / MANIFEST_FILENAME).write_text(
            json.dumps(legacy_manifest), encoding="utf-8"
        )

        self.assert_ok(self.install("--antigravity"))

        merged = json.loads(hooks_path.read_text(encoding="utf-8"))
        self.assertNotIn("craftsman-git-guard", merged)
        self.assertIn("craftsman-scope-guard", merged)
        self.assertIs(merged["craftsman-scope-guard"]["enabled"], True)
        self.assertIs(merged["craftsman-stop-gate"]["enabled"], False)
        self.assertEqual(merged["my-hook"], legacy_hooks["my-hook"])

    def test_upgrade_from_pre_manifest_3x_removes_git_guard_from_hooks_json(self) -> None:
        self.config_dir.mkdir(parents=True)
        hooks_path = self.config_dir / "hooks.json"
        hooks_path.write_text(
            json.dumps(
                {
                    "craftsman-git-guard": {"enabled": True, "PreToolUse": []},
                    "craftsman-stop-gate": {"enabled": False, "Stop": []},
                    "my-hook": {"enabled": True, "Stop": []},
                }
            ),
            encoding="utf-8",
        )

        self.assert_ok(self.install("--antigravity"))

        merged = json.loads(hooks_path.read_text(encoding="utf-8"))
        self.assertNotIn("craftsman-git-guard", merged)
        self.assertIn("craftsman-scope-guard", merged)
        self.assertIs(merged["craftsman-stop-gate"]["enabled"], False)
        self.assertIn("my-hook", merged)

    def test_corrupt_hooks_json_stops_the_install_before_any_write(self) -> None:
        self.config_dir.mkdir(parents=True)
        (self.config_dir / "hooks.json").write_text("{broken", encoding="utf-8")

        result = self.install("--antigravity")

        self.assertEqual(result.code, 1)
        self.assertIn("cannot read it as JSON", result.err)
        self.assertEqual(sorted(p.name for p in self.config_dir.iterdir()), ["hooks.json"])

    def test_plugin_switch_keeps_a_created_hooks_json_the_user_edited(self) -> None:
        self.assert_ok(self.install("--antigravity"))
        hooks_json = self.config_dir / "hooks.json"
        data = json.loads(hooks_json.read_text(encoding="utf-8"))
        hooks_json.write_text(json.dumps({**data, "my-hook": {"enabled": True}}), encoding="utf-8")

        self.assert_ok(self.install("--antigravity", "--plugin"))

        kept = json.loads(hooks_json.read_text(encoding="utf-8"))
        self.assertEqual(kept, {"my-hook": {"enabled": True}})

    def test_failed_install_prints_no_summary(self) -> None:
        self.config_dir.mkdir(parents=True)
        (self.config_dir / "hooks.json").write_text("{broken", encoding="utf-8")

        result = self.install("--antigravity")

        self.assertNotIn("Summary:", result.out)

    def test_failed_install_saves_no_manifest(self) -> None:
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        self.config_dir.mkdir(parents=True)
        (self.config_dir / "rules").symlink_to(elsewhere, target_is_directory=True)

        result = self.install("--antigravity")

        self.assertEqual(result.code, 1)
        self.assertIn("a folder above it is a symlink", result.err)
        self.assertIsNone(load_manifest(self.config_dir))
        self.assertEqual(list(elsewhere.iterdir()), [])


class ClaudeTests(InstallTestCase):
    def test_claude_install_merges_the_gate_hooks(self) -> None:
        self.assert_ok(self.install("--claude"))

        settings = json.loads((self.claude_dir / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(len(claude_hook_commands(settings)), 5)
        self.assertTrue((self.claude_dir / "agents" / "craftsman-reviewer.md").is_file())

    def test_no_claude_hooks_leaves_settings_alone(self) -> None:
        self.assert_ok(self.install("--claude", "--no-claude-hooks"))

        self.assertFalse((self.claude_dir / "settings.json").exists())
        self.assertTrue((self.claude_dir / "commands" / "craftsman.md").is_file())

    def test_reinstall_without_hooks_takes_the_merged_hooks_out(self) -> None:
        self.claude_dir.mkdir()
        (self.claude_dir / "settings.json").write_text('{"theme": "dark"}', encoding="utf-8")
        self.assert_ok(self.install("--claude"))

        self.assert_ok(self.install("--claude", "--no-claude-hooks"))

        self.assertEqual(
            json.loads((self.claude_dir / "settings.json").read_text(encoding="utf-8")),
            {"theme": "dark"},
        )

    def test_reinstall_without_hooks_keeps_settings_the_user_edited(self) -> None:
        self.assert_ok(self.install("--claude"))
        settings = self.claude_dir / "settings.json"
        data = json.loads(settings.read_text(encoding="utf-8"))
        settings.write_text(json.dumps({**data, "model": "opus"}), encoding="utf-8")

        self.assert_ok(self.install("--claude", "--no-claude-hooks"))

        self.assertEqual(json.loads(settings.read_text(encoding="utf-8")), {"model": "opus"})

    def test_settings_craftsman_cannot_merge_into_stop_the_install(self) -> None:
        self.claude_dir.mkdir()
        (self.claude_dir / "settings.json").write_text('{"hooks": {"Stop": {}}}', encoding="utf-8")

        result = self.install("--claude")

        self.assertEqual(result.code, 1)
        self.assertIn("`hooks.Stop` is not a list", result.err)
        self.assertFalse((self.claude_dir / "commands").exists())

    def test_unreadable_settings_stop_a_reinstall_without_hooks(self) -> None:
        self.assert_ok(self.install("--claude"))
        (self.claude_dir / "settings.json").write_text("{oops", encoding="utf-8")
        command = self.claude_dir / "commands" / "verify.md"
        command.unlink()

        result = self.install("--claude", "--no-claude-hooks")

        self.assertEqual(result.code, 1)
        self.assertFalse(command.exists())

    def test_unreadable_settings_stop_the_install(self) -> None:
        self.claude_dir.mkdir()
        (self.claude_dir / "settings.json").write_text("{oops", encoding="utf-8")

        result = self.install("--claude")

        self.assertEqual(result.code, 1)
        self.assertFalse((self.claude_dir / "commands").exists())


class CopilotTests(InstallTestCase):
    def test_user_install_writes_one_copy_of_each_entity(self) -> None:
        profile = default_vscode_prompts_dir(self.home).parent / "profiles" / "work"
        profile.mkdir(parents=True)

        self.assert_ok(self.install("--copilot"))

        self.assertTrue((profile / "prompts" / "craftsman-rules.instructions.md").is_file())
        self.assertTrue((self.home / ".copilot" / "hooks" / "craftsman.json").is_file())
        self.assertEqual(scan_copilot_duplicates(REPO_ROOT, home=self.home), [])

    def test_workspace_install_writes_one_copy_of_each_entity(self) -> None:
        workspace = self.tmp / "ws"
        workspace.mkdir()

        self.assert_ok(self.install("--copilot-workspace", str(workspace)))

        self.assertTrue((workspace / ".github" / "skills" / "craftsman" / "SKILL.md").is_file())
        self.assertEqual(scan_copilot_duplicates(REPO_ROOT, home=self.home, workspace=workspace), [])

    def test_workspace_install_is_refused_next_to_a_user_install(self) -> None:
        self.assert_ok(self.install("--copilot"))
        workspace = self.tmp / "ws"
        workspace.mkdir()

        result = self.install("--copilot-workspace", str(workspace))

        self.assertEqual(result.code, 1)
        self.assertIn("already installed for your user", result.err)

    def test_interpreter_that_cannot_run_stops_the_install(self) -> None:
        with mock.patch.object(sys, "executable", str(self.tmp / "no-such-python")):
            result = self.install("--copilot")

        self.assertEqual(result.code, 1)
        self.assertFalse((self.home / ".copilot").exists())

    def test_users_hooks_file_is_moved_aside(self) -> None:
        workspace = self.tmp / "ws"
        hooks = workspace / ".github" / "hooks" / "craftsman.json"
        hooks.parent.mkdir(parents=True)
        hooks.write_text('{"version": 1, "hooks": {}}', encoding="utf-8")

        self.assert_ok(self.install("--copilot-workspace", str(workspace)))

        (backup,) = (hooks.parent / BACKUP_DIR_NAME).iterdir()
        self.assertEqual(backup.read_text(encoding="utf-8"), '{"version": 1, "hooks": {}}')

    def test_duplicate_scopes_install_when_allowed(self) -> None:
        self.assert_ok(self.install("--copilot"))
        workspace = self.tmp / "ws"
        workspace.mkdir()

        self.assert_ok(self.install("--copilot-workspace", str(workspace), "--allow-duplicate-scopes"))


class LegacyGitHooksUpgradeTests(InstallTestCase):
    def test_upgrade_retires_craftsman_hooks_path_with_its_files(self) -> None:
        hooks_dir = self.write_legacy_git_hooks(previous_hooks_path=None)

        result = self.install("--claude")

        self.assert_ok(result)
        self.assertIn("removed the global git hooks of Craftsman 3.x", result.out)
        self.assertIsNone(self.hooks_path())
        self.assertFalse(hooks_dir.exists())
        self.assertIsNone(load_manifest(self.home / ".craftsman"))

    def test_upgrade_restores_previous_hooks_path(self) -> None:
        hooks_dir = self.write_legacy_git_hooks(previous_hooks_path="~/my-hooks")

        result = self.install("--copilot")

        self.assert_ok(result)
        self.assertEqual(self.hooks_path(), "~/my-hooks")
        self.assertFalse(hooks_dir.exists())

    def test_upgrade_leaves_user_changed_hooks_path_untouched(self) -> None:
        hooks_dir = self.write_legacy_git_hooks(previous_hooks_path=None)
        self.git_global("core.hooksPath", "/opt/husky")

        result = self.install("--claude")

        self.assert_ok(result)
        self.assertEqual(self.hooks_path(), "/opt/husky")
        self.assertIn("leaving git config --global core.hooksPath (/opt/husky) untouched", result.err)
        self.assertFalse(hooks_dir.exists())

    def test_upgrade_keeps_shared_runtime_needed_by_antigravity(self) -> None:
        self.assert_ok(self.install("--antigravity"))
        hooks_dir = self.write_legacy_git_hooks(self.config_dir)

        result = self.install("--claude")

        self.assert_ok(result)
        self.assertIsNone(self.hooks_path())
        self.assertFalse(hooks_dir.exists())
        self.assertTrue((self.config_dir / "scripts" / "craftsman-stop-gate.py").is_file())
        manifest = load_manifest(self.config_dir)
        assert manifest is not None
        self.assertNotIn(TargetKind.GIT_HOOKS, manifest.targets)
        self.assertIn(TargetKind.ANTIGRAVITY, manifest.targets)

    def test_dry_run_upgrade_leaves_legacy_git_hooks_untouched(self) -> None:
        hooks_dir = self.write_legacy_git_hooks(previous_hooks_path="~/prev")
        before = snapshot_tree(self.home)

        result = self.install("--claude", "--dry-run")

        self.assert_ok(result)
        self.assertEqual(snapshot_tree(self.home), before)
        self.assertEqual(self.hooks_path(), str(hooks_dir))

    def test_corrupt_git_hooks_manifest_does_not_block_other_targets(self) -> None:
        (self.home / ".craftsman").mkdir()
        (self.home / ".craftsman" / MANIFEST_FILENAME).write_text("{", encoding="utf-8")

        self.assert_ok(self.install("--claude"))


class WorkspaceTests(InstallTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.workspace = self.tmp / "project"
        self.workspace.mkdir()

    def test_existing_agents_md_keeps_its_content(self) -> None:
        agents_md = self.workspace / "AGENTS.md"
        agents_md.write_text("# Team Guide\n", encoding="utf-8")

        self.assert_ok(self.install("--agents-workspace", str(self.workspace)))

        text = agents_md.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("# Team Guide\n\n" + CRAFTSMAN_BEGIN_MARKER))
        self.assertEqual(
            (self.workspace / "AGENTS.md.bak").read_text(encoding="utf-8"), "# Team Guide\n"
        )
        self.assertTrue((self.workspace / ".cursor" / "rules" / "craftsman.mdc").is_file())

    def test_agents_md_linking_out_of_the_workspace_is_refused(self) -> None:
        outside = self.tmp / "outside.md"
        outside.write_text("secret\n", encoding="utf-8")
        (self.workspace / "AGENTS.md").symlink_to(outside)

        result = self.install("--agents-workspace", str(self.workspace))

        self.assertEqual(result.code, 1)
        self.assertIn("resolves outside", result.err)
        self.assertEqual(outside.read_text(encoding="utf-8"), "secret\n")

    def test_agents_md_linked_to_claude_md_updates_claude_md(self) -> None:
        (self.workspace / "CLAUDE.md").write_text("# Mine\n", encoding="utf-8")
        (self.workspace / "AGENTS.md").symlink_to(self.workspace / "CLAUDE.md")

        self.assert_ok(self.install("--agents-workspace", str(self.workspace)))

        self.assertTrue((self.workspace / "AGENTS.md").is_symlink())
        claude_md = (self.workspace / "CLAUDE.md").read_text(encoding="utf-8")
        self.assertIn(CRAFTSMAN_BEGIN_MARKER, claude_md)

    def test_workspace_manifest_records_the_merged_file(self) -> None:
        self.assert_ok(self.install("--agents-workspace", str(self.workspace)))

        manifest = load_manifest(self.workspace)

        assert manifest is not None
        target = manifest.targets[TargetKind.AGENTS_WORKSPACE]
        self.assertIn(str(self.workspace / "AGENTS.md"), target.modified_files)


class DryRunTests(InstallTestCase):
    def test_dry_run_install_changes_nothing(self) -> None:
        before = snapshot_tree(self.home)

        result = self.install("--all", "--dry-run")

        self.assert_ok(result)
        self.assertIn("[DRY-RUN] Summary:", result.out)
        self.assertEqual(snapshot_tree(self.home), before)
        self.assertEqual(self.git_config.read_text(encoding="utf-8"), "")


class BootstrapTests(InstallTestCase):
    def test_install_sh_installs_everything_then_uninstall_empties_home(self) -> None:
        modes = {p: p.stat().st_mode for p in (REPO_ROOT / "scripts").iterdir()}

        proc = subprocess.run(
            ["bash", str(REPO_ROOT / "install.sh"), "--all"],
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
            timeout=300,
        )

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual({p: p.stat().st_mode for p in modes}, modes)
        self.assert_ok(self.uninstall())
        self.assertEqual(list(self.home.iterdir()), [])
        self.assertIsNone(self.hooks_path())

    def test_manifest_is_written_in_each_base_folder(self) -> None:
        self.assert_ok(self.install("--all"))

        for base in (self.config_dir, self.claude_dir, self.home / ".copilot"):
            self.assertTrue((base / MANIFEST_FILENAME).is_file(), base)


if __name__ == "__main__":
    unittest.main()
