"""Tests for the cleanup of the `code-review` skill that Craftsman < 5 installed (now `craft-review`)."""

from __future__ import annotations

import json
from pathlib import Path

from craftsman.doctor import build_report
from craftsman.generated import CRAFTSMAN_GENERATED_MARKER
from craftsman.manifest import MANIFEST_FILENAME

from tests.support import REPO_ROOT, IsolatedHomeTestCase

MINE = "---\nname: code-review\ndescription: my own review\n---\nmine\n"
GENERATED = f"---\nname: code-review\n---\n{CRAFTSMAN_GENERATED_MARKER}\nold\n"


class LegacySkillTestCase(IsolatedHomeTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.config_dir = self.home / ".gemini" / "config"
        self.claude_dir = self.home / ".claude"
        self.copilot_dir = self.home / ".copilot"
        self.workspace = self.tmp / "project"
        self.workspace.mkdir()

    def legacy_dir(self, skills_dir: Path, text: str = MINE) -> Path:
        directory = skills_dir / "code-review"
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text(text, encoding="utf-8")
        return directory

    def record(self, base_dir: Path, kind: str, path: Path) -> None:
        """Add `path` to the paths the manifest of `base_dir` says Craftsman created for `kind`."""
        manifest_path = base_dir / MANIFEST_FILENAME
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["targets"][kind]["created_paths"].append(str(path))
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


class InstallTests(LegacySkillTestCase):
    def test_a_recorded_legacy_directory_is_removed(self) -> None:
        self.assert_ok(self.install("--antigravity"))
        legacy = self.legacy_dir(self.config_dir / "skills")
        self.record(self.config_dir, "antigravity", legacy)

        result = self.install("--antigravity")

        self.assert_ok(result)
        self.assertFalse(legacy.exists())
        self.assertIn(str(legacy), result.out)
        self.assertTrue((self.config_dir / "skills" / "craft-review" / "SKILL.md").is_file())

    def test_a_legacy_directory_craftsman_generated_is_removed_without_a_record(self) -> None:
        legacy = self.legacy_dir(self.copilot_dir / "skills", GENERATED)

        self.assert_ok(self.install("--copilot"))

        self.assertFalse(legacy.exists())
        self.assertTrue((self.copilot_dir / "skills" / "craft-review" / "SKILL.md").is_file())

    def test_a_generated_legacy_claude_command_is_removed(self) -> None:
        command = self.claude_dir / "commands" / "code-review.md"
        command.parent.mkdir(parents=True)
        command.write_text(f"{CRAFTSMAN_GENERATED_MARKER}\nold\n", encoding="utf-8")

        self.assert_ok(self.install("--claude"))

        self.assertFalse(command.exists())
        self.assertTrue((self.claude_dir / "commands" / "craft-review.md").is_file())

    def test_a_workspace_legacy_directory_is_removed(self) -> None:
        legacy = self.legacy_dir(self.workspace / ".github" / "skills", GENERATED)

        self.assert_ok(self.install("--copilot-workspace", str(self.workspace)))

        self.assertFalse(legacy.exists())

    def test_a_users_own_code_review_skill_is_kept(self) -> None:
        mine = {
            "antigravity": self.legacy_dir(self.config_dir / "skills"),
            "copilot": self.legacy_dir(self.copilot_dir / "skills"),
            "copilot_workspace": self.legacy_dir(self.workspace / ".github" / "skills"),
        }
        command = self.claude_dir / "commands" / "code-review.md"
        command.parent.mkdir(parents=True)
        command.write_text("my own command\n", encoding="utf-8")
        for _ in range(2):  # a first install and a reinstall over a manifest
            result = self.install(
                "--antigravity",
                "--copilot",
                "--claude",
                "--copilot-workspace",
                str(self.workspace),
                "--allow-duplicate-scopes",
            )
            self.assert_ok(result)

        for name, directory in mine.items():
            with self.subTest(target=name):
                self.assertEqual((directory / "SKILL.md").read_text(encoding="utf-8"), MINE)
        self.assertEqual(command.read_text(encoding="utf-8"), "my own command\n")

    def test_dry_run_leaves_the_legacy_directory(self) -> None:
        legacy = self.legacy_dir(self.copilot_dir / "skills", GENERATED)

        result = self.install("--copilot", "--dry-run")

        self.assert_ok(result)
        self.assertIn(str(legacy), result.out)
        self.assertTrue(legacy.is_dir())


class UninstallTests(LegacySkillTestCase):
    def test_uninstall_removes_the_new_and_the_recorded_legacy_directory(self) -> None:
        self.assert_ok(self.install("--antigravity"))
        legacy = self.legacy_dir(self.config_dir / "skills")
        self.record(self.config_dir, "antigravity", legacy)
        current = self.config_dir / "skills" / "craft-review"
        self.assertTrue(current.is_dir())

        self.assert_ok(self.uninstall("--antigravity"))

        self.assertFalse(current.exists())
        self.assertFalse(legacy.exists())
        self.assertFalse((self.config_dir / "skills").exists())

    def test_uninstall_removes_a_generated_legacy_directory_the_manifest_lacks(self) -> None:
        self.assert_ok(self.install("--copilot"))
        legacy = self.legacy_dir(self.copilot_dir / "skills", GENERATED)
        current = self.copilot_dir / "skills" / "craft-review"

        self.assert_ok(self.uninstall("--copilot"))

        self.assertFalse(current.exists())
        self.assertFalse(legacy.exists())

    def test_uninstall_keeps_a_users_own_code_review_skill(self) -> None:
        self.assert_ok(self.install("--copilot"))
        mine = self.legacy_dir(self.copilot_dir / "skills")

        self.assert_ok(self.uninstall("--copilot"))

        self.assertFalse((self.copilot_dir / "skills" / "craft-review").exists())
        self.assertEqual((mine / "SKILL.md").read_text(encoding="utf-8"), MINE)


class DoctorTests(LegacySkillTestCase):
    def problems(self) -> list[str]:
        return list(build_report(REPO_ROOT, self.workspace, self.home).problems)

    def test_a_leftover_owned_legacy_skill_is_reported(self) -> None:
        legacy = self.legacy_dir(self.copilot_dir / "skills", GENERATED)

        (problem,) = [p for p in self.problems() if "code-review" in p]

        self.assertIn(str(legacy), problem)
        self.assertIn("craft-review", problem)

    def test_a_recorded_legacy_skill_is_reported(self) -> None:
        self.assert_ok(self.install("--antigravity"))
        legacy = self.legacy_dir(self.config_dir / "skills")
        self.record(self.config_dir, "antigravity", legacy)

        self.assertTrue(any(str(legacy) in problem for problem in self.problems()))

    def test_a_users_own_code_review_skill_is_not_reported(self) -> None:
        self.legacy_dir(self.copilot_dir / "skills")

        self.assertEqual([p for p in self.problems() if "code-review" in p], [])
