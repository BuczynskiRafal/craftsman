"""Tests for `craftsman uninstall`: only what the manifests record goes, and nothing else."""

from __future__ import annotations

import json
import os
import shutil
import unittest

from craftsman.files import BACKUP_DIR_NAME
from craftsman.legacy_githooks import git_config_get, git_config_set, git_config_unset
from craftsman.manifest import MANIFEST_FILENAME, TargetKind, load_manifest
from craftsman.paths import default_vscode_user_dir
from craftsman.staging import Tracker
from craftsman.uninstall import apply_plan, parse_uninstall_args, plan_base

from tests.support import IsolatedHomeTestCase, snapshot_tree


class UninstallTestCase(IsolatedHomeTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.config_dir = self.home / ".gemini" / "config"
        self.claude_dir = self.home / ".claude"
        self.workspace = self.tmp / "project"
        self.workspace.mkdir()


class ArgumentTests(UninstallTestCase):
    def test_no_target_uninstalls_every_user_target(self) -> None:
        config = parse_uninstall_args([])

        self.assertEqual(
            (config.antigravity, config.claude, config.copilot),
            (True, True, True),
        )

    def test_unsupported_uninstall_flags_are_rejected(self) -> None:
        for flag in ("--symlink", "--repo-dir", "--claude-hooks", "--git-hooks", "--no-git-hooks"):
            with self.subTest(flag=flag):
                self.assertEqual(self.uninstall(flag).code, 2)


class RemovalTests(UninstallTestCase):
    def test_uninstall_keeps_the_users_backups(self) -> None:
        skill = self.config_dir / "skills" / "craftsman"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("mine\n", encoding="utf-8")
        self.assert_ok(self.install("--antigravity"))

        self.assert_ok(self.uninstall("--antigravity"))

        self.assertFalse(skill.exists())
        (backup,) = (self.config_dir / "skills" / BACKUP_DIR_NAME).iterdir()
        self.assertEqual((backup / "SKILL.md").read_text(encoding="utf-8"), "mine\n")
        self.assertFalse((self.config_dir / MANIFEST_FILENAME).exists())

    def test_files_without_a_manifest_are_left_alone(self) -> None:
        agent = self.home / ".copilot" / "agents" / "craftsman.agent.md"
        agent.parent.mkdir(parents=True)
        agent.write_text("<!-- craftsman:generated -->\n", encoding="utf-8")

        result = self.uninstall("--copilot")

        self.assertEqual(result.code, 0)
        self.assertIn("Nothing to uninstall", result.out)
        self.assertTrue(agent.is_file())

    def test_manifest_path_outside_the_workspace_stops_the_uninstall(self) -> None:
        victim = self.tmp / "victim.txt"
        victim.write_text("keep\n", encoding="utf-8")
        self.assert_ok(self.install("--agents-workspace", str(self.workspace)))
        manifest_path = self.workspace / MANIFEST_FILENAME
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["targets"]["agents_workspace"]["created_paths"].append(str(victim))
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        result = self.uninstall("--agents-workspace", str(self.workspace))

        self.assertEqual(result.code, 1)
        self.assertIn("refusing to touch", result.err)
        self.assertTrue(victim.is_file())

    def test_tampered_git_hooks_manifest_changes_no_git_config(self) -> None:
        hooks_dir = self.write_legacy_git_hooks()
        manifest_path = self.home / ".craftsman" / MANIFEST_FILENAME
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["targets"]["git_hooks"]["created_paths"].append(str(self.tmp / "victim.txt"))
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        result = self.uninstall()

        self.assertEqual(result.code, 1)
        self.assertEqual(self.hooks_path(), str(hooks_dir))

    def test_configured_folders_outside_home_are_uninstalled(self) -> None:
        outside = self.tmp / "outside"
        os.environ["CRAFTSMAN_HOME"] = str(outside / "craftsman")
        os.environ["XDG_CONFIG_HOME"] = str(outside / "xdg")
        self.assert_ok(self.install("--copilot"))
        self.write_legacy_git_hooks(outside / "craftsman")

        self.assert_ok(self.uninstall("--all"))

        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual(list(self.home.iterdir()), [])

    def test_agents_md_relinked_out_of_the_workspace_is_refused(self) -> None:
        (self.workspace / "CLAUDE.md").write_text("# Mine\n", encoding="utf-8")
        (self.workspace / "AGENTS.md").symlink_to(self.workspace / "CLAUDE.md")
        self.assert_ok(self.install("--agents-workspace", str(self.workspace)))
        other = self.tmp / "other.md"
        other.write_text((self.workspace / "CLAUDE.md").read_text(encoding="utf-8"), encoding="utf-8")
        (self.workspace / "AGENTS.md").unlink()
        (self.workspace / "AGENTS.md").symlink_to(other)
        before = other.read_text(encoding="utf-8")

        result = self.uninstall("--agents-workspace", str(self.workspace))

        self.assertEqual(result.code, 1)
        self.assertEqual(other.read_text(encoding="utf-8"), before)

    def test_refusal_in_one_folder_changes_no_other_folder(self) -> None:
        self.assert_ok(self.install("--antigravity", "--agents-workspace", str(self.workspace)))
        manifest_path = self.workspace / MANIFEST_FILENAME
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["targets"]["agents_workspace"]["created_paths"].append(str(self.tmp / "victim.txt"))
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        result = self.uninstall("--antigravity", "--agents-workspace", str(self.workspace))

        self.assertEqual(result.code, 1)
        self.assertTrue((self.config_dir / "skills" / "craftsman" / "SKILL.md").is_file())

    def test_copilot_uninstall_survives_a_deleted_vscode_profile(self) -> None:
        profile = default_vscode_user_dir(self.home) / "profiles" / "work"
        profile.mkdir(parents=True)
        self.assert_ok(self.install("--copilot"))
        shutil.rmtree(profile)

        self.assert_ok(self.uninstall("--copilot"))

        self.assertFalse((self.home / ".copilot").exists())

    def test_shared_scripts_stay_while_another_target_needs_them(self) -> None:
        self.assert_ok(self.install("--antigravity"))
        hooks_dir = self.write_legacy_git_hooks(self.config_dir)

        plan = plan_base(self.config_dir, [TargetKind.GIT_HOOKS], self.home, roots=(self.config_dir,))
        assert plan is not None
        apply_plan(plan, self.home, Tracker(dry_run=False))

        self.assertTrue((self.config_dir / "scripts" / "craftsman-stop-gate.py").is_file())
        self.assertFalse(hooks_dir.exists())

    def test_uninstall_restores_previous_hooks_path_or_unsets_it(self) -> None:
        for previous in (None, "~/my-hooks", ".githooks"):
            with self.subTest(previous=previous):
                hooks_dir = self.write_legacy_git_hooks(previous_hooks_path=previous)
                self.assert_ok(self.uninstall("--claude"))
                self.assertEqual(self.hooks_path(), previous)
                self.assertFalse(hooks_dir.exists())
                self.assertIsNone(load_manifest(self.home / ".craftsman"))

    def test_uninstall_from_v1_manifest_restores_previous_hooks_path_or_unsets_it(self) -> None:
        for previous in (None, "~/my-hooks"):
            with self.subTest(previous=previous):
                hooks_dir = self.write_legacy_v1_git_hooks(previous_hooks_path=previous)
                self.assert_ok(self.uninstall("--claude"))
                self.assertEqual(self.hooks_path(), previous)
                self.assertFalse(hooks_dir.exists())
                self.assertFalse((self.home / ".craftsman").exists())

    def test_uninstall_leaves_user_changed_hooks_path_untouched(self) -> None:
        hooks_dir = self.write_legacy_git_hooks(previous_hooks_path=None)
        self.git_global("core.hooksPath", "/opt/husky")

        result = self.uninstall()

        self.assert_ok(result)
        self.assertEqual(self.hooks_path(), "/opt/husky")
        self.assertIn("leaving git config --global core.hooksPath (/opt/husky) untouched", result.err)
        self.assertFalse(hooks_dir.exists())

    def test_git_config_unset_drops_only_an_empty_section(self) -> None:
        git_config_set("core.hooksPath", "/tmp/hooks", self.home)
        git_config_unset("core.hooksPath", self.home)
        self.assertIsNone(git_config_get("core.hooksPath", self.home))
        self.assertNotIn("[core]", self.git_config.read_text(encoding="utf-8"))

        git_config_set("core.hooksPath", "/tmp/hooks", self.home)
        git_config_set("core.autocrlf", "false", self.home)
        git_config_unset("core.hooksPath", self.home)
        self.assertEqual(git_config_get("core.autocrlf", self.home), "false")


class MergedFileTests(UninstallTestCase):
    def test_claude_settings_return_to_the_users_original(self) -> None:
        self.claude_dir.mkdir()
        original = {
            "theme": "dark",
            "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo"}]}]},
        }
        (self.claude_dir / "settings.json").write_text(json.dumps(original), encoding="utf-8")
        self.assert_ok(self.install("--claude"))

        self.assert_ok(self.uninstall("--claude"))

        self.assertEqual(
            json.loads((self.claude_dir / "settings.json").read_text(encoding="utf-8")), original
        )

    def test_settings_craftsman_created_are_removed(self) -> None:
        self.assert_ok(self.install("--claude"))

        self.assert_ok(self.uninstall("--claude"))

        self.assertFalse(self.claude_dir.exists())

    def test_hooks_json_keeps_the_users_entries(self) -> None:
        self.config_dir.mkdir(parents=True)
        mine = {"my-hook": {"enabled": True, "Stop": []}}
        (self.config_dir / "hooks.json").write_text(json.dumps(mine), encoding="utf-8")
        self.assert_ok(self.install("--antigravity"))

        self.assert_ok(self.uninstall("--antigravity"))

        self.assertEqual(json.loads((self.config_dir / "hooks.json").read_text(encoding="utf-8")), mine)

    def test_workspace_markdown_keeps_the_users_text(self) -> None:
        (self.workspace / "AGENTS.md").write_text("# Team Guide\n", encoding="utf-8")
        self.install(
            "--agents-workspace", str(self.workspace), "--copilot-workspace", str(self.workspace)
        )

        self.assert_ok(
            self.uninstall(
                "--agents-workspace", str(self.workspace), "--copilot-workspace", str(self.workspace)
            )
        )

        self.assertEqual((self.workspace / "AGENTS.md").read_text(encoding="utf-8"), "# Team Guide\n")
        self.assertFalse((self.workspace / ".github").exists())
        self.assertFalse((self.workspace / ".cursor").exists())

    def test_text_added_to_a_created_agents_md_survives(self) -> None:
        self.assert_ok(self.install("--agents-workspace", str(self.workspace)))
        with (self.workspace / "AGENTS.md").open("a", encoding="utf-8") as handle:
            handle.write("\n# Added later\n")

        self.assert_ok(self.uninstall("--agents-workspace", str(self.workspace)))

        self.assertEqual((self.workspace / "AGENTS.md").read_text(encoding="utf-8"), "# Added later\n")


class DryRunTests(UninstallTestCase):
    def test_dry_run_changes_nothing(self) -> None:
        self.assert_ok(self.install("--all"))
        before, config = snapshot_tree(self.home), self.git_config.read_bytes()

        self.assert_ok(self.uninstall("--dry-run"))

        self.assertEqual(snapshot_tree(self.home), before)
        self.assertEqual(self.git_config.read_bytes(), config)

    def test_dry_run_reports_what_the_real_run_does(self) -> None:
        self.claude_dir.mkdir()
        (self.claude_dir / "settings.json").write_text('{"theme": "dark"}', encoding="utf-8")
        self.assert_ok(self.install("--all"))

        planned = self.uninstall("--dry-run").out.splitlines()
        done = self.uninstall().out.splitlines()

        self.assertEqual(planned[:-1], done[:-1])
        self.assertEqual(planned[-1], f"[DRY-RUN] {done[-1]}")


if __name__ == "__main__":
    unittest.main()
