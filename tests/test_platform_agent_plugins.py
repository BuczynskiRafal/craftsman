"""Tests for the Codex and Cursor plugin manifests (`craftsman.platforms.agent_plugins`)."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any

from craftsman import __version__
from craftsman.generated import ExportError
from craftsman.platforms.agent_plugins import (
    CODEX_MANIFEST,
    CODEX_MARKETPLACE,
    CURSOR_MANIFEST,
    CURSOR_MARKETPLACE,
    CURSOR_RULE,
    export_agent_plugins,
)

from tests.support import REPO_ROOT


def load(rel_path: str) -> Any:
    return json.loads((REPO_ROOT / rel_path).read_text(encoding="utf-8"))


class CommittedAgentPluginTests(unittest.TestCase):
    def test_committed_plugins_match_the_source(self) -> None:
        files = export_agent_plugins(REPO_ROOT, check=True)
        self.assertEqual(
            files, sorted([CODEX_MARKETPLACE, CODEX_MANIFEST, CURSOR_MARKETPLACE, CURSOR_MANIFEST, CURSOR_RULE])
        )

    def test_both_marketplaces_point_at_the_repository_root(self) -> None:
        codex = load(CODEX_MARKETPLACE)
        self.assertEqual(
            [entry["source"] for entry in codex["plugins"]], [{"source": "local", "path": "./"}]
        )
        self.assertEqual(codex["plugins"][0]["policy"]["installation"], "AVAILABLE")
        cursor = load(CURSOR_MARKETPLACE)
        self.assertIn("name", cursor["owner"])
        self.assertEqual([entry["source"] for entry in cursor["plugins"]], ["./"])

    def test_manifests_list_the_shared_skills_and_no_hooks(self) -> None:
        for rel_path in (CODEX_MANIFEST, CURSOR_MANIFEST):
            with self.subTest(manifest=rel_path):
                manifest = load(rel_path)
                self.assertEqual(manifest["name"], "craftsman")
                self.assertEqual(manifest["version"], __version__)
                # Paths must start with ./ and stay inside the plugin root.
                self.assertEqual(manifest["skills"], "./skills/")
                self.assertTrue((REPO_ROOT / "skills" / "craftsman" / "SKILL.md").is_file())
                # Craftsman has no adapter for Codex or Cursor hook events.
                self.assertNotIn("hooks", manifest)
        # Neither tool may fall back to a default hooks file.
        self.assertFalse((REPO_ROOT / "hooks" / "hooks.json").exists())

    def test_codex_interface_stays_within_its_limits(self) -> None:
        interface = load(CODEX_MANIFEST)["interface"]
        self.assertLessEqual(len(interface["defaultPrompt"]), 3)
        self.assertTrue(all(len(prompt) <= 512 for prompt in interface["defaultPrompt"]))

    def test_cursor_manifest_uses_only_schema_fields(self) -> None:
        allowed = {
            "name", "displayName", "description", "version", "minClientVersions", "author", "publisher",
            "homepage", "repository", "license", "logo", "keywords", "category", "tags", "commands",
            "agents", "skills", "rules", "hooks", "variables", "mcpServers",
        }  # fmt: skip
        manifest = load(CURSOR_MANIFEST)
        self.assertLessEqual(set(manifest), allowed)
        self.assertEqual(set(manifest["author"]), {"name", "email"})
        self.assertTrue((REPO_ROOT / manifest["rules"]).is_file())

    def test_cursor_rule_is_always_applied(self) -> None:
        rule = (REPO_ROOT / CURSOR_RULE).read_text(encoding="utf-8")
        self.assertTrue(rule.startswith("---\n"))
        self.assertIn("alwaysApply: true", rule)
        self.assertIn("craftsman review prepare", rule)


class AgentPluginExportTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name) / "checkout"
        (self.repo / "craftsman").mkdir(parents=True)
        (self.repo / "craftsman" / "__init__.py").write_text("", encoding="utf-8")
        shutil.copytree(REPO_ROOT / "templates", self.repo / "templates")
        shutil.copytree(REPO_ROOT / "rules", self.repo / "rules")
        shutil.copytree(REPO_ROOT / "skills", self.repo / "skills")

    def test_export_writes_what_check_accepts(self) -> None:
        written = export_agent_plugins(self.repo)
        self.assertEqual(export_agent_plugins(self.repo, check=True), written)

    def test_check_names_every_drifted_file(self) -> None:
        export_agent_plugins(self.repo)
        (self.repo / CODEX_MANIFEST).write_text("{}\n", encoding="utf-8")
        (self.repo / CURSOR_MARKETPLACE).unlink()
        (self.repo / ".cursor-plugin" / "rules" / "old.mdc").write_text("", encoding="utf-8")
        with self.assertRaises(ExportError) as caught:
            export_agent_plugins(self.repo, check=True)
        for line in (
            f"differs: {CODEX_MANIFEST}",
            f"missing: {CURSOR_MARKETPLACE}",
            "extra: .cursor-plugin/rules/old.mdc",
        ):
            self.assertIn(line, str(caught.exception))

    def test_export_drops_stale_rules(self) -> None:
        stale = self.repo / ".cursor-plugin" / "rules" / "old.mdc"
        stale.parent.mkdir(parents=True)
        stale.write_text("", encoding="utf-8")
        export_agent_plugins(self.repo)
        self.assertFalse(stale.exists())

    def test_assets_without_the_package_source_are_refused(self) -> None:
        (self.repo / "craftsman" / "__init__.py").unlink()
        for check in (False, True):
            with self.subTest(check=check), self.assertRaisesRegex(ExportError, "source checkout"):
                export_agent_plugins(self.repo, check=check)
        self.assertFalse((self.repo / ".cursor-plugin").exists())


if __name__ == "__main__":
    unittest.main()
