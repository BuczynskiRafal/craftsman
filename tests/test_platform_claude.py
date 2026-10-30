"""Tests for Claude Code export, hook matching, and settings merging (`craftsman.platforms.claude`)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from craftsman.assets import AgentTemplate, Skill, load_agent_templates
from craftsman.files import BACKUP_DIR_NAME
from craftsman.generated import ExportError, is_craftsman_generated_file
from craftsman.platforms.claude import (
    claude_agent_templates,
    export_to_claude_user,
    is_craftsman_hook_command,
    merge_claude_hooks,
    render_claude_agent,
    render_claude_command_file,
    strip_claude_hooks,
)

from tests.support import REPO_ROOT

PREFIX = '"/usr/bin/python3" -m craftsman.cli'


def frontmatter(text: str) -> list[str]:
    assert text.startswith("---\n"), text[:40]
    return text.split("---\n")[1].splitlines()


class ClaudeTestCase(unittest.TestCase):
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


class ClaudeAgentRenderingTests(ClaudeTestCase):
    def templates(self) -> dict[str, AgentTemplate]:
        return {template.name: template for template in load_agent_templates(REPO_ROOT)}

    def test_claude_subagents_list_their_tools(self) -> None:
        templates = self.templates()
        reviewer = render_claude_agent(templates["craftsman-reviewer"])

        self.assertEqual(frontmatter(reviewer)[2], "tools: Read, Grep, Glob, Bash")
        self.assertNotIn("tools:", "".join(frontmatter(render_claude_agent(templates["craftsman"]))))

    def test_claude_gets_no_orchestrator_agent(self) -> None:
        names = sorted(template.name for template in claude_agent_templates(REPO_ROOT))

        self.assertEqual(names, ["craftsman-reviewer", "craftsman-verifier"])


class ClaudeSkillExportTests(ClaudeTestCase):
    def test_claude_command_keeps_backslashes_in_the_description(self) -> None:
        skill = Skill("x", "Match \\bword\\b", "B", "", self.tmp)

        self.assertIn("description: 'Match \\bword\\b'", render_claude_command_file(skill))

    def test_claude_commands_inline_linked_siblings(self) -> None:
        synthetic = self.fake_repo("body\n")
        (synthetic / "rules").mkdir()
        (synthetic / "rules" / "r.md").write_text("Rule.\n", encoding="utf-8")
        demo_dir = synthetic / "skills" / "demo"
        demo_dir.mkdir(parents=True)
        (demo_dir / "SKILL.md").write_text(
            "---\nname: demo\ndescription: Demo skill.\n---\nSee [guide.md](guide.md) and [missing](missing.md).\n",
            encoding="utf-8",
        )
        (demo_dir / "guide.md").write_text(
            "---\nname: guide\n---\n# Guide Heading\n\nGuide body.\n", encoding="utf-8"
        )
        export_to_claude_user(synthetic, self.tmp / "synthetic-claude")
        demo_cmd = (self.tmp / "synthetic-claude" / "commands" / "demo.md").read_text(encoding="utf-8")
        self.assertIn("\n## Appendix: guide.md\n\n# Guide Heading\n\nGuide body.\n", demo_cmd)
        self.assertNotIn("## Appendix: missing.md", demo_cmd)

        export_to_claude_user(REPO_ROOT, self.tmp / "claude")

        review = (self.tmp / "claude" / "commands" / "code-review.md").read_text(encoding="utf-8")
        verify = (self.tmp / "claude" / "commands" / "verify.md").read_text(encoding="utf-8")
        self.assertNotIn("## Appendix: lenses.md", review)
        self.assertNotIn("## Appendix", review)
        self.assertNotIn("## Appendix", verify)
        self.assertIn("craftsman skill show code-review --lenses", review)

    def test_users_agent_file_is_moved_aside(self) -> None:
        mine = self.tmp / "agents" / "craftsman-reviewer.md"
        mine.parent.mkdir()
        mine.write_text("my reviewer\n", encoding="utf-8")

        export_to_claude_user(REPO_ROOT, self.tmp)

        (backup,) = (self.tmp / "agents" / BACKUP_DIR_NAME).iterdir()
        self.assertEqual(backup.read_text(encoding="utf-8"), "my reviewer\n")
        self.assertTrue(is_craftsman_generated_file(mine))


class HookCommandMatchTests(unittest.TestCase):
    def test_generated_commands_match(self) -> None:
        shim = '"/usr/bin/python3" "/src/craftsman/scripts/craftsman-cli.py"'
        for command in (
            f"{PREFIX} hook stop --platform claude",
            f"{shim} hook pre-tool --platform claude",
        ):
            with self.subTest(command=command):
                self.assertTrue(is_craftsman_hook_command(command))

    def test_users_commands_that_mention_craftsman_do_not_match(self) -> None:
        for command in (
            "craftsman hook stop --platform claude",
            f"{PREFIX} hook stop --platform claude && notify-send done",
            f"echo {PREFIX} hook stop --platform claude",
            f"{PREFIX} hook deploy --platform claude",
        ):
            with self.subTest(command=command):
                self.assertFalse(is_craftsman_hook_command(command))


class ClaudeSettingsMergeTests(ClaudeTestCase):
    def settings(self) -> dict[str, Any]:
        loaded = json.loads((self.tmp / "settings.json").read_text(encoding="utf-8"))
        assert isinstance(loaded, dict)
        return loaded

    def write_settings(self, data: Any) -> Path:
        path = self.tmp / "settings.json"
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        return path

    def test_hooks_added_after_ours_keep_their_place(self) -> None:
        path = self.write_settings({})
        merge_claude_hooks(path, PREFIX)
        settings = self.settings()
        late = {"hooks": [{"type": "command", "command": "notify-send late"}]}
        settings["hooks"]["Stop"].append(late)
        path.write_text(json.dumps(settings), encoding="utf-8")

        self.assertFalse(merge_claude_hooks(path, PREFIX).changed)
        self.assertEqual(self.settings()["hooks"]["Stop"][-1], late)

    def test_a_symlinked_settings_file_is_updated_in_place(self) -> None:
        real = self.tmp / "dotfiles" / "settings.json"
        real.parent.mkdir()
        real.write_text("{}", encoding="utf-8")
        link = self.tmp / "settings.json"
        link.symlink_to(real)

        merge_claude_hooks(link, PREFIX)

        self.assertTrue(link.is_symlink())
        self.assertIn("Stop", json.loads(real.read_text(encoding="utf-8"))["hooks"])

    def test_merge_replaces_our_old_command_only(self) -> None:
        foreign = {"type": "command", "command": "notify-send done"}
        old = {
            "type": "command",
            "command": '"/old/python" -m craftsman.cli hook stop --platform claude',
        }
        path = self.write_settings({"model": "opus", "hooks": {"Stop": [{"hooks": [foreign, old]}]}})

        self.assertTrue(merge_claude_hooks(path, PREFIX).changed)

        ours = {"type": "command", "command": f"{PREFIX} hook stop --platform claude", "timeout": 60}
        self.assertEqual(list(self.settings()), ["model", "hooks"])
        self.assertEqual(self.settings()["hooks"]["Stop"], [{"hooks": [ours]}, {"hooks": [foreign]}])

    def test_pre_tool_hook_matches_the_guarded_tools(self) -> None:
        path = self.write_settings({})

        merge_claude_hooks(path, PREFIX)

        self.assertEqual(self.settings()["hooks"]["PreToolUse"][0]["matcher"], "Bash|Read|Grep|Glob|LS")

    def test_first_change_keeps_a_backup_of_the_original(self) -> None:
        path = self.write_settings({"model": "opus"})
        original = path.read_text(encoding="utf-8")
        merge_claude_hooks(path, PREFIX)

        merge_claude_hooks(path, '"/other/python" -m craftsman.cli')

        self.assertEqual((self.tmp / "settings.json.bak").read_text(encoding="utf-8"), original)

    def test_missing_file_is_created_without_backup(self) -> None:
        path = self.tmp / "settings.json"

        self.assertTrue(merge_claude_hooks(path, PREFIX).changed)
        self.assertEqual(len(self.settings()["hooks"]), 5)
        self.assertFalse((self.tmp / "settings.json.bak").exists())

    def test_malformed_settings_are_refused_untouched(self) -> None:
        path = self.tmp / "settings.json"
        for text, fragment in (
            ("{ not json", "cannot read it as JSON"),
            ("[1]", "top level is not a JSON object"),
            ('{"hooks": []}', "`hooks` is not a JSON object"),
            ('{"hooks": {"Stop": {}}}', "`hooks.Stop` is not a list"),
        ):
            with self.subTest(text=text):
                path.write_text(text, encoding="utf-8")
                with self.assertRaisesRegex(ExportError, fragment):
                    merge_claude_hooks(path, PREFIX)
                self.assertEqual(path.read_text(encoding="utf-8"), text)

    def test_stripping_our_hooks_restores_the_users_settings(self) -> None:
        original = {
            "theme": "dark",
            "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo"}]}]},
        }
        path = self.write_settings(original)
        merge_claude_hooks(path, PREFIX)

        self.assertEqual(strip_claude_hooks(self.settings()), original)


if __name__ == "__main__":
    unittest.main()
