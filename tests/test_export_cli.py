"""Tests for the `craftsman export` CLI and cross-target rendered Markdown checks."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from craftsman import export
from craftsman.generated import CRAFTSMAN_GENERATED_MARKER
from craftsman.platforms.agents_md import export_to_agents_workspace
from craftsman.platforms.claude import export_to_claude_user
from craftsman.platforms.copilot import (
    export_copilot_home,
    export_to_copilot_skills,
    export_vscode_instructions,
)

from tests.support import REPO_ROOT, SKILL_COUNT, CliResult, run_main


class CommandLineTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()

    def export(self, *args: str) -> CliResult:
        return run_main(export.main, "--repo-dir", str(REPO_ROOT), *args)

    def test_no_target_is_an_error(self) -> None:
        result = self.export()

        self.assertEqual(result.code, 2)
        self.assertIn("Specify at least one target", result.err)

    def test_no_claude_hooks_requires_claude_dir(self) -> None:
        result = self.export("--no-claude-hooks")

        self.assertEqual(result.code, 2)
        self.assertIn("--no-claude-hooks requires --claude-dir", result.err)

    def test_refused_settings_are_reported(self) -> None:
        (self.tmp / "settings.json").write_text("{oops", encoding="utf-8")

        result = self.export("--claude-dir", str(self.tmp))

        self.assertEqual(result.code, 1)
        self.assertIn("[ERROR] refusing to modify", result.err)
        self.assertEqual((self.tmp / "settings.json").read_text(encoding="utf-8"), "{oops")

    def test_explicit_folders_leave_the_copilot_home_alone(self) -> None:
        home = self.tmp / "home"
        with mock.patch.dict(os.environ, {"HOME": str(home)}):
            result = self.export(
                "--user-dir", str(self.tmp / "prompts"), "--copilot-skills-dir", str(self.tmp / "skills")
            )

        self.assertEqual(result.code, 0, result.err)
        self.assertFalse(home.exists())
        self.assertTrue((self.tmp / "skills" / "code-review" / "lenses.md").is_file())

    def test_each_target_is_reported_once(self) -> None:
        skills = self.tmp / "skills"

        result = self.export(
            "--copilot-skills-dir",
            str(skills),
            "--claude-dir",
            str(self.tmp / "claude"),
            "--no-claude-hooks",
        )

        self.assertEqual(result.out.count("[OK] Exported"), 2)
        self.assertIn(f"[OK] Exported {SKILL_COUNT} Copilot Agent Skills -> {skills}", result.out)

    def test_export_script_writes_every_target(self) -> None:
        exporter = REPO_ROOT / "scripts" / "copilot-export.py"
        with tempfile.TemporaryDirectory() as tmpdir:
            user_prompts = Path(tmpdir) / "user_prompts"
            workspace = Path(tmpdir) / "ws"
            claude_dir = Path(tmpdir) / "claude"
            subprocess.run(
                [
                    sys.executable,
                    str(exporter),
                    "--repo-dir",
                    str(REPO_ROOT),
                    "--user-dir",
                    str(user_prompts),
                    "--workspace",
                    str(workspace),
                    "--allow-duplicate-scopes",
                    "--claude-dir",
                    str(claude_dir),
                    "--agents-workspace",
                    str(workspace),
                ],
                check=True,
                env={**os.environ, "HOME": tmpdir, "USERPROFILE": tmpdir},
            )
            self.assertFalse((user_prompts / "craftsman.prompt.md").exists())
            self.assertTrue((user_prompts / "craftsman-rules.instructions.md").is_file())
            self.assertEqual(len(list(user_prompts.glob("*.prompt.md"))), 0)
            self.assertTrue((workspace / ".github" / "copilot-instructions.md").is_file())
            self.assertEqual(
                len(list((workspace / ".github" / "skills").glob("*/SKILL.md"))), SKILL_COUNT
            )
            self.assertFalse((workspace / ".github" / "prompts").exists())
            self.assertEqual(len(list((claude_dir / "commands").glob("*.md"))), SKILL_COUNT)
            self.assertTrue((claude_dir / "rules" / "craftsman-rules.md").is_file())
            self.assertTrue((workspace / "AGENTS.md").is_file())
            self.assertTrue((workspace / ".cursor" / "rules" / "craftsman.mdc").is_file())


class RenderedTextTests(unittest.TestCase):
    """Checks over every Markdown file one export to every target writes."""

    root: Path
    files: dict[Path, str]

    @classmethod
    def setUpClass(cls) -> None:
        tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(tmp.cleanup)
        cls.root = Path(tmp.name)
        export_vscode_instructions(REPO_ROOT, cls.root / "prompts")
        export_to_copilot_skills(REPO_ROOT, cls.root / "skills")
        export_copilot_home(REPO_ROOT, cls.root / "copilot", ("craftsman", "craftsman"))
        export_to_claude_user(REPO_ROOT, cls.root / "claude")
        export_to_agents_workspace(REPO_ROOT, cls.root / "ws")
        cls.files = {path: path.read_text(encoding="utf-8") for path in cls.root.rglob("*.md")}

    def test_no_file_looks_skills_up_on_disk(self) -> None:
        lookup = re.compile(r"skills/[a-z0-9_-]+/SKILL\.md")
        for path, text in self.files.items():
            with self.subTest(path=path.name):
                self.assertIsNone(lookup.search(text))
                self.assertNotIn("installed next to this agent", text)

    def test_agents_include_the_search_scope(self) -> None:
        agents = [path for path in self.files if path.parent.name == "agents"]

        self.assertEqual(len(agents), 5)
        for path in agents:
            with self.subTest(path=path.name):
                self.assertIn("Search scope", self.files[path])

    def test_no_file_names_the_removed_gate_invocation(self) -> None:
        for path, text in self.files.items():
            with self.subTest(path=path.name):
                self.assertNotIn("--state-file", text)
                self.assertNotIn("craftsman-stop-gate.py", text)

    def test_every_generated_file_carries_the_marker(self) -> None:
        generated = [
            path
            for path in self.files
            if path.name == "SKILL.md" or path.parent.name in ("agents", "commands", "rules", "prompts")
        ]

        self.assertGreater(len(generated), 2 * SKILL_COUNT)
        for path in generated:
            with self.subTest(path=path.relative_to(self.root)):
                self.assertIn(CRAFTSMAN_GENERATED_MARKER, self.files[path])


if __name__ == "__main__":
    unittest.main()
