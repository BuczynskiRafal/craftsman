"""Tests for repository asset loading, frontmatter parsing, rules, and platform paths."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from craftsman.assets import load_agent_templates, parse_frontmatter, read_skill, render_combined_rules
from craftsman.generated import ExportError
from craftsman.paths import default_vscode_user_dir, env_var_for_home, resolve_craftsman_pkg_dir
from craftsman.platforms.copilot import render_copilot_agent


class AssetsTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()

    def fake_repo(self, agent_body: str) -> Path:
        repo = self.tmp / "fake-repo"
        (repo / "agents").mkdir(parents=True)
        (repo / "docs").mkdir()
        (repo / "docs" / "part.md").write_text(
            "---\nname: part\n---\nIncluded text.\n", encoding="utf-8"
        )
        (repo / "agents" / "helper.md").write_text(
            "---\nname: helper\ndescription: >-\n  Helps with\n  things that aren't hard.\nrole: reviewer\n---\n"
            + agent_body,
            encoding="utf-8",
        )
        return repo

    def skill_file(self, text: str) -> Path:
        path = self.tmp / "skills" / "x" / "SKILL.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path


class AgentTemplateTests(AssetsTestCase):
    def test_folded_description_is_joined(self) -> None:
        (template,) = load_agent_templates(self.fake_repo("Intro\n{{include docs/part.md}}\nOutro\n"))

        self.assertEqual(template.description, "Helps with things that aren't hard.")
        self.assertEqual(template.body, "Intro\nIncluded text.\nOutro")
        self.assertIn(
            "description: 'Helps with things that aren''t hard.'", render_copilot_agent(template)
        )

    def test_missing_include_is_an_error(self) -> None:
        repo = self.fake_repo("{{include docs/missing.md}}\n")

        with self.assertRaisesRegex(ExportError, r"included file not found: .*docs[/\\]missing\.md"):
            load_agent_templates(repo)

    def test_unknown_role_is_an_error(self) -> None:
        repo = self.fake_repo("body\n")
        (repo / "agents" / "helper.md").write_text(
            "---\nname: helper\ndescription: x\nrole: boss\n---\nbody\n", encoding="utf-8"
        )

        with self.assertRaisesRegex(ExportError, "role must be one of orchestrator, reviewer, verifier"):
            load_agent_templates(repo)

    def test_include_must_be_a_well_formed_repository_path(self) -> None:
        for directive, message in (
            ("{{include ../outside.md}}", "must be a path inside"),
            ("{{include /etc/passwd}}", "must be a path inside"),
            ("{{include}}", "malformed include directive"),
            ("{{ include docs/part.md }}", "malformed include directive"),
        ):
            with self.subTest(directive=directive):
                shutil.rmtree(self.tmp / "fake-repo", ignore_errors=True)
                with self.assertRaisesRegex(ExportError, message):
                    load_agent_templates(self.fake_repo(directive + "\n"))

    def test_includes_follow_symlinked_skill_folders(self) -> None:
        repo = self.fake_repo("{{include skills/code-review/lenses.md}}\n")
        elsewhere = self.tmp / "checkout" / "code-review"
        elsewhere.mkdir(parents=True)
        (elsewhere / "lenses.md").write_text("Lens text.\n", encoding="utf-8")
        (repo / "skills").mkdir()
        (repo / "skills" / "code-review").symlink_to(elsewhere, target_is_directory=True)

        (template,) = load_agent_templates(repo)

        self.assertEqual(template.body, "Lens text.")


class FrontmatterTests(AssetsTestCase):
    def test_quoted_scalars_keep_inner_quotes(self) -> None:
        text = '---\na: \'it\'\'s\'\nb: "say \\"hi\\""\nc: Run "craftsman review"\n---\nBody\n'

        fields, _ = parse_frontmatter(text, Path("x"))

        self.assertEqual(fields, {"a": "it's", "b": 'say "hi"', "c": 'Run "craftsman review"'})

    def test_block_scalar_is_joined(self) -> None:
        skill = read_skill(self.skill_file("---\nname: x\ndescription: |-\n  Block text.\n---\nBody\n"))

        self.assertEqual(skill.description, "Block text.")

    def test_skill_without_description_is_an_error(self) -> None:
        with self.assertRaisesRegex(ExportError, "frontmatter needs `description`"):
            read_skill(self.skill_file("---\nname: x\n---\nBody\n"))

    def test_skill_without_frontmatter_is_an_error(self) -> None:
        with self.assertRaisesRegex(ExportError, "missing frontmatter block"):
            read_skill(self.skill_file("Body only\n"))


class CombinedRulesTests(AssetsTestCase):
    def test_rules_are_separated_by_horizontal_rules(self) -> None:
        rules = self.tmp / "rules"
        rules.mkdir()
        (rules / "a.md").write_text("---\nname: a\n---\nFirst.\n", encoding="utf-8")
        (rules / "b.md").write_text("Second.\n", encoding="utf-8")

        combined = render_combined_rules(rules, with_frontmatter=False)

        self.assertEqual(combined, "First.\n\n---\n\nSecond.\n")


class LocationTests(AssetsTestCase):
    def test_linux_vscode_folder_follows_xdg(self) -> None:
        with (
            mock.patch.object(sys, "platform", "linux"),
            mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.tmp / "cfg")}),
        ):
            self.assertEqual(default_vscode_user_dir(self.tmp), self.tmp / "cfg" / "Code" / "User")

    def test_linux_vscode_folder_defaults_to_dot_config(self) -> None:
        with mock.patch.object(sys, "platform", "linux"), mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(default_vscode_user_dir(self.tmp), self.tmp / ".config" / "Code" / "User")

    def test_macos_vscode_folder_is_in_application_support(self) -> None:
        expected = self.tmp / "Library" / "Application Support" / "Code" / "User"
        with mock.patch.object(sys, "platform", "darwin"):
            self.assertEqual(default_vscode_user_dir(self.tmp), expected)

    def test_windows_vscode_folder_is_in_appdata(self) -> None:
        appdata = self.tmp / "AppData" / "Roaming"
        with (
            mock.patch.object(sys, "platform", "win32"),
            mock.patch.dict(os.environ, {"APPDATA": str(appdata)}),
        ):
            self.assertEqual(default_vscode_user_dir(self.tmp), appdata / "Code" / "User")

    def test_windows_without_appdata_is_an_error(self) -> None:
        with (
            mock.patch.object(sys, "platform", "win32"),
            mock.patch.dict(os.environ, {}, clear=True),
            self.assertRaisesRegex(ExportError, "APPDATA is not set"),
        ):
            default_vscode_user_dir(self.tmp)

    def test_setting_outside_an_explicit_home_is_ignored(self) -> None:
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": "/opt/cfg"}):
            self.assertEqual(env_var_for_home("XDG_CONFIG_HOME", None), "/opt/cfg")
            self.assertEqual(env_var_for_home("XDG_CONFIG_HOME", self.tmp / "isolated"), "")

    def test_assets_without_a_package_are_an_error(self) -> None:
        with self.assertRaisesRegex(ExportError, "no craftsman package next to the assets"):
            resolve_craftsman_pkg_dir(self.tmp)


if __name__ == "__main__":
    unittest.main()
