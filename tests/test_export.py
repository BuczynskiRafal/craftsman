"""Tests for the export of agents, skills, rules and hooks to Copilot, Claude Code and workspaces."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from craftsman import __version__, export
from craftsman.export import (
    CRAFTSMAN_BEGIN_MARKER,
    CRAFTSMAN_GENERATED_MARKER,
    REVIEWER_GROUPS_PLACEHOLDER,
    DiscoveryRoot,
    EntityPattern,
    ExportError,
    Skill,
    claude_agent_templates,
    default_vscode_user_dir,
    is_craftsman_generated_file,
    is_craftsman_hook_command,
    iter_discovered_files,
    load_agent_templates,
    merge_claude_hooks,
    parse_frontmatter,
    read_skill,
    render_claude_agent,
    render_claude_command_file,
    render_combined_rules,
    render_copilot_agent,
    render_copilot_hooks,
    strip_claude_hooks,
    strip_managed_block,
    upsert_managed_markdown,
)
from craftsman.files import BACKUP_DIR_NAME

from tests.support import REPO_ROOT, SKILL_COUNT, CliResult, run_main

PREFIX = '"/usr/bin/python3" -m craftsman.cli'


def frontmatter(text: str) -> list[str]:
    assert text.startswith("---\n"), text[:40]
    return text.split("---\n")[1].splitlines()


class ExportTestCase(unittest.TestCase):
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


class AgentRenderingTests(ExportTestCase):
    def templates(self) -> dict[str, export.AgentTemplate]:
        return {template.name: template for template in load_agent_templates(REPO_ROOT)}

    def test_copilot_orchestrator_may_delegate_to_subagents(self) -> None:
        orchestrator = render_copilot_agent(self.templates()["craftsman"])

        self.assertEqual(
            frontmatter(orchestrator)[2:],
            [
                "tools: ['read', 'edit', 'search', 'execute', 'agent', 'todo', 'web']",
                "agents: ['craftsman-reviewer', 'craftsman-verifier']",
            ],
        )
        self.assertIn("`craftsman-reviewer` and `craftsman-verifier` custom agents", orchestrator)

    def test_copilot_subagents_are_read_only(self) -> None:
        templates = self.templates()
        for name in ("craftsman-reviewer", "craftsman-verifier"):
            with self.subTest(name=name):
                rendered = render_copilot_agent(templates[name])
                self.assertEqual(
                    frontmatter(rendered)[2:],
                    ["tools: ['read', 'search', 'execute']", "user-invocable: false"],
                )
                self.assertNotIn("runSubagent", rendered)

    def test_reviewer_includes_the_lens_playbook(self) -> None:
        reviewer = render_copilot_agent(self.templates()["craftsman-reviewer"])
        lenses = (REPO_ROOT / "skills" / "code-review" / "lenses.md").read_text(encoding="utf-8")

        self.assertIn(lenses.strip().splitlines()[-1], reviewer)
        self.assertNotIn("{{include", reviewer)

    def test_claude_subagents_list_their_tools(self) -> None:
        templates = self.templates()
        reviewer = render_claude_agent(templates["craftsman-reviewer"])

        self.assertEqual(frontmatter(reviewer)[2], "tools: Read, Grep, Glob, Bash")
        self.assertNotIn("tools:", "".join(frontmatter(render_claude_agent(templates["craftsman"]))))

    def test_claude_gets_no_orchestrator_agent(self) -> None:
        names = sorted(template.name for template in claude_agent_templates(REPO_ROOT))

        self.assertEqual(names, ["craftsman-reviewer", "craftsman-verifier"])

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

    def test_include_paths_must_stay_in_the_repository_and_be_well_formed(self) -> None:
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


class FrontmatterTests(ExportTestCase):
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

    def test_claude_command_keeps_backslashes_in_the_description(self) -> None:
        skill = Skill("x", "Match \\bword\\b", "B", "", self.tmp)

        self.assertIn("description: 'Match \\bword\\b'", render_claude_command_file(skill))


class SkillExportTests(ExportTestCase):
    def test_skill_folders_get_every_file_of_the_skill(self) -> None:
        out = self.tmp / "skills"
        (out / "code-review").mkdir(parents=True)
        (out / "code-review" / "SKILL.md").write_text(CRAFTSMAN_GENERATED_MARKER, encoding="utf-8")
        (out / "code-review" / "mine.md").write_text("user file\n", encoding="utf-8")

        export.export_to_copilot_skills(REPO_ROOT, out)

        self.assertEqual(
            (out / "code-review" / "lenses.md").read_bytes(),
            (REPO_ROOT / "skills" / "code-review" / "lenses.md").read_bytes(),
        )
        skill_md = (out / "code-review" / "SKILL.md").read_text(encoding="utf-8")
        self.assertTrue(skill_md.startswith("---\nname: code-review\n"))
        self.assertEqual((out / "code-review" / "mine.md").read_text(encoding="utf-8"), "user file\n")

    def test_users_skill_folder_is_moved_aside(self) -> None:
        out = self.tmp / "skills"
        (out / "verify").mkdir(parents=True)
        (out / "verify" / "SKILL.md").write_text("my verify\n", encoding="utf-8")

        changes = export.export_to_copilot_skills(REPO_ROOT, out)

        (backup,) = [change.backup for change in changes if change.backup is not None]
        self.assertEqual(backup.parent, out / BACKUP_DIR_NAME)
        self.assertEqual((backup / "SKILL.md").read_text(encoding="utf-8"), "my verify\n")
        self.assertEqual(len(changes), SKILL_COUNT)

    def test_symlinked_destination_files_are_replaced_not_written_through(self) -> None:
        source_copy = self.tmp / "source-lenses.md"
        source_copy.write_text("original\n", encoding="utf-8")
        out = self.tmp / "skills"
        (out / "code-review").mkdir(parents=True)
        (out / "code-review" / "SKILL.md").write_text(CRAFTSMAN_GENERATED_MARKER, encoding="utf-8")
        (out / "code-review" / "lenses.md").symlink_to(source_copy)

        export.export_to_copilot_skills(REPO_ROOT, out)

        self.assertEqual(source_copy.read_text(encoding="utf-8"), "original\n")
        self.assertFalse((out / "code-review" / "lenses.md").is_symlink())

    def test_claude_commands_inline_linked_siblings(self) -> None:
        export.export_to_claude_user(REPO_ROOT, self.tmp)

        review = (self.tmp / "commands" / "code-review.md").read_text(encoding="utf-8")
        verify = (self.tmp / "commands" / "verify.md").read_text(encoding="utf-8")
        self.assertIn("\n## Appendix: lenses.md\n\n# Craftsman Review Lenses\n", review)
        self.assertNotIn("## Appendix", verify)

    def test_users_agent_file_is_moved_aside(self) -> None:
        mine = self.tmp / "agents" / "craftsman-reviewer.md"
        mine.parent.mkdir()
        mine.write_text("my reviewer\n", encoding="utf-8")

        export.export_to_claude_user(REPO_ROOT, self.tmp)

        (backup,) = (self.tmp / "agents" / BACKUP_DIR_NAME).iterdir()
        self.assertEqual(backup.read_text(encoding="utf-8"), "my reviewer\n")
        self.assertTrue(is_craftsman_generated_file(mine))

    def test_instructions_reach_every_vscode_profile(self) -> None:
        prompts = self.tmp / "Code" / "User" / "prompts"
        profile = self.tmp / "Code" / "User" / "profiles" / "work"
        profile.mkdir(parents=True)

        export.export_vscode_instructions(REPO_ROOT, prompts)

        self.assertTrue((prompts / "craftsman-rules.instructions.md").is_file())
        self.assertTrue((profile / "prompts" / "craftsman-rules.instructions.md").is_file())

    def test_folder_not_named_prompts_ignores_sibling_profiles(self) -> None:
        unrelated = self.tmp / "profiles" / "unrelated"
        unrelated.mkdir(parents=True)

        export.export_vscode_instructions(REPO_ROOT, self.tmp / "out")

        self.assertFalse((unrelated / "prompts").exists())

    def test_rules_are_separated_by_horizontal_rules(self) -> None:
        rules = self.tmp / "rules"
        rules.mkdir()
        (rules / "a.md").write_text("---\nname: a\n---\nFirst.\n", encoding="utf-8")
        (rules / "b.md").write_text("Second.\n", encoding="utf-8")

        combined = render_combined_rules(rules, with_frontmatter=False)

        self.assertEqual(combined, "First.\n\n---\n\nSecond.\n")


class AgentsMdTests(ExportTestCase):
    def test_reviewer_groups_replace_the_placeholder(self) -> None:
        text = export.render_agents_md(REPO_ROOT)

        self.assertNotIn(REVIEWER_GROUPS_PLACEHOLDER, text)
        self.assertIn("  Reviewer groups:\n  ", text)

    def test_every_skill_is_listed(self) -> None:
        text = export.render_agents_md(REPO_ROOT)

        for skill in export.iter_skills(REPO_ROOT):
            self.assertIn(f"- **`/{skill.name}`**: ", text)

    def test_template_without_placeholder_is_an_error(self) -> None:
        repo = self.tmp / "repo"
        (repo / "templates").mkdir(parents=True)
        (repo / "templates" / "AGENTS.md").write_text("# AGENTS.md\n", encoding="utf-8")

        with self.assertRaisesRegex(ExportError, "has no \\{\\{reviewer_groups\\}\\} placeholder"):
            export.render_agents_md(repo)

    def test_cursor_rule_applies_to_every_file(self) -> None:
        workspace = export.export_to_agents_workspace(REPO_ROOT, self.tmp / "ws")

        rule = workspace.cursor_rule.path.read_text(encoding="utf-8")
        self.assertIn('globs: "*"', frontmatter(rule))


class ManagedMarkdownTests(ExportTestCase):
    def test_file_without_markers_gets_the_block_appended(self) -> None:
        path = self.tmp / "AGENTS.md"
        heading = "# AGENTS.md — Craftsman Adaptive Closed-Loop Engineering Standard\n"
        path.write_text(heading, encoding="utf-8")

        upsert_managed_markdown(path, "rules", within=self.tmp)

        self.assertEqual(
            path.read_text(encoding="utf-8"),
            f"{heading}\n{CRAFTSMAN_BEGIN_MARKER}\nrules\n<!-- craftsman:end -->\n",
        )

    def test_second_upsert_replaces_the_block_in_place(self) -> None:
        path = self.tmp / "AGENTS.md"
        path.write_text("# Mine\n", encoding="utf-8")
        upsert_managed_markdown(path, "old", within=self.tmp)
        path.write_text(path.read_text(encoding="utf-8") + "\n# After\n", encoding="utf-8")

        upsert_managed_markdown(path, "new", within=self.tmp)

        text = path.read_text(encoding="utf-8")
        self.assertEqual(text.count(CRAFTSMAN_BEGIN_MARKER), 1)
        self.assertEqual(strip_managed_block(text, Path("AGENTS.md")), "# Mine\n\n# After\n")

    def test_dry_run_upsert_writes_nothing(self) -> None:
        path = self.tmp / "AGENTS.md"
        path.write_text("# Mine\n", encoding="utf-8")

        change = upsert_managed_markdown(path, "rules", within=self.tmp, dry_run=True)

        self.assertTrue(change.changed)
        self.assertEqual(path.read_text(encoding="utf-8"), "# Mine\n")
        self.assertFalse((self.tmp / "AGENTS.md.bak").exists())

    def test_block_without_its_end_marker_is_refused(self) -> None:
        path = self.tmp / "AGENTS.md"
        text = f"intro\n{CRAFTSMAN_BEGIN_MARKER}\nold\n## My team rules\nkeep me\n"
        path.write_text(text, encoding="utf-8")

        with self.assertRaisesRegex(ExportError, "do not enclose one block"):
            upsert_managed_markdown(path, "rules", within=self.tmp)
        self.assertEqual(path.read_text(encoding="utf-8"), text)

    def test_symlinked_file_inside_the_root_is_updated_where_it_lives(self) -> None:
        (self.tmp / "CLAUDE.md").write_text("# Mine\n", encoding="utf-8")
        (self.tmp / "AGENTS.md").symlink_to(self.tmp / "CLAUDE.md")

        upsert_managed_markdown(self.tmp / "AGENTS.md", "rules", within=self.tmp)

        self.assertTrue((self.tmp / "AGENTS.md").is_symlink())
        self.assertIn(CRAFTSMAN_BEGIN_MARKER, (self.tmp / "CLAUDE.md").read_text(encoding="utf-8"))

    def test_symlink_out_of_the_root_is_refused(self) -> None:
        outside = self.tmp / "outside.md"
        outside.write_text("secret\n", encoding="utf-8")
        workspace = self.tmp / "ws"
        workspace.mkdir()
        (workspace / "AGENTS.md").symlink_to(outside)

        with self.assertRaisesRegex(ExportError, "resolves outside"):
            upsert_managed_markdown(workspace / "AGENTS.md", "rules", within=workspace)
        self.assertEqual(outside.read_text(encoding="utf-8"), "secret\n")

    def test_unreadable_file_is_refused(self) -> None:
        path = self.tmp / "AGENTS.md"
        path.write_bytes(b"\xff\xfe")

        with self.assertRaisesRegex(ExportError, "cannot read it as UTF-8"):
            upsert_managed_markdown(path, "rules", within=self.tmp)


class HookExportTests(ExportTestCase):
    def test_copilot_hooks_call_every_event(self) -> None:
        hooks = render_copilot_hooks("craftsman", "craftsman")

        self.assertEqual(
            list(hooks["hooks"]),
            ["userPromptSubmitted", "agentStop", "subagentStart", "subagentStop", "preToolUse"],
        )
        self.assertEqual(
            hooks["hooks"]["agentStop"],
            [
                {
                    "type": "command",
                    "bash": "craftsman hook stop --platform copilot",
                    "powershell": "craftsman hook stop --platform copilot",
                    "timeoutSec": 60,
                }
            ],
        )

    def test_workspace_hooks_run_craftsman_from_path(self) -> None:
        export.export_to_workspace(REPO_ROOT, self.tmp)

        hooks = json.loads(
            (self.tmp / ".github" / "hooks" / "craftsman.json").read_text(encoding="utf-8")
        )
        command = hooks["hooks"]["subagentStart"][0]["bash"]
        self.assertEqual(command, "craftsman hook subagent-start --platform copilot")

    def test_user_hooks_run_the_interpreter_by_absolute_path(self) -> None:
        copilot_home = self.tmp / "copilot"

        result = run_main(export.main, "--copilot-home", str(copilot_home))

        self.assertEqual(result.code, 0, result.err)
        hooks = json.loads((copilot_home / "hooks" / "craftsman.json").read_text(encoding="utf-8"))
        entry = hooks["hooks"]["agentStop"][0]
        self.assertEqual(shlex.split(entry["bash"])[0], sys.executable)
        self.assertEqual(shlex.split(entry["bash"])[-4:], ["hook", "stop", "--platform", "copilot"])
        self.assertTrue(entry["powershell"].startswith(f'& "{sys.executable}" '))

    def fake_python(self, folder: Path, imports: Path) -> str:
        """An interpreter whose `import craftsman` finds the package at `imports`."""
        folder.mkdir(parents=True)
        python = folder / "python3"
        python.write_text(f"#!/bin/sh\nprintf '%s' '{imports}'\n", encoding="utf-8")
        python.chmod(0o755)
        return str(python)

    def test_prefix_quotes_the_interpreter_for_bash(self) -> None:
        folder = self.tmp / "py $HOME" / '"x"'
        python = self.fake_python(folder, Path(export.__file__))

        bash, powershell = export.absolute_hook_prefixes(python)

        quoted = str(folder).replace("$", "\\$").replace('"', '\\"')
        self.assertEqual(bash, f'"{quoted}/python3" -m craftsman.cli')
        ps_quoted = str(folder).replace("$", "`$").replace('"', '`"')
        self.assertEqual(powershell, f'& "{ps_quoted}/python3" -m craftsman.cli')
        echoed = subprocess.run(
            ["bash", "-c", f"printf '%s\\n' {bash}"],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        self.assertEqual(echoed.stdout.splitlines(), [python, "-m", "craftsman.cli"])

    def test_interpreter_without_this_package_runs_the_shim(self) -> None:
        python = self.fake_python(self.tmp / "py", self.tmp / "elsewhere" / "craftsman" / "__init__.py")

        bash, _ = export.absolute_hook_prefixes(python)

        shim = str(REPO_ROOT / "scripts" / "craftsman-cli.py")
        self.assertEqual(shlex.split(bash), [python, shim])

    def test_unreachable_package_without_a_shim_is_an_error(self) -> None:
        python = self.fake_python(self.tmp / "py", self.tmp / "elsewhere" / "craftsman" / "__init__.py")
        with (
            mock.patch.object(export, "CLI_SHIM", Path("scripts") / "no-such-shim.py"),
            self.assertRaisesRegex(ExportError, "cannot import craftsman"),
        ):
            export.absolute_hook_prefixes(python)

    def test_interpreter_that_cannot_run_is_an_error(self) -> None:
        with self.assertRaisesRegex(ExportError, "cannot run"):
            export.absolute_hook_prefixes(str(self.tmp / "no-such-python"))

    def test_shim_runs_this_checkout(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "craftsman-cli.py"), "--version"],
            cwd=self.tmp,
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )

        self.assertEqual(proc.stdout.strip(), f"craftsman {__version__}")


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


class ClaudeSettingsMergeTests(ExportTestCase):
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


class CommandLineTests(ExportTestCase):
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


class LocationTests(ExportTestCase):
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
            self.assertEqual(export.env_var_for_home("XDG_CONFIG_HOME", None), "/opt/cfg")
            self.assertEqual(export.env_var_for_home("XDG_CONFIG_HOME", self.tmp / "isolated"), "")

    def test_assets_without_a_package_are_an_error(self) -> None:
        with self.assertRaisesRegex(ExportError, "no craftsman package next to the assets"):
            export.resolve_craftsman_pkg_dir(self.tmp)


class DiscoveryTests(ExportTestCase):
    def test_skill_pattern_names_the_folder(self) -> None:
        (self.tmp / "verify").mkdir()
        (self.tmp / "verify" / "SKILL.md").write_text("x", encoding="utf-8")

        found = list(iter_discovered_files(self.tmp, EntityPattern.SKILL_DIR))

        self.assertEqual(found, [("verify", self.tmp / "verify" / "SKILL.md")])

    def test_agent_pattern_strips_the_suffix(self) -> None:
        (self.tmp / "craftsman.agent.md").write_text("x", encoding="utf-8")
        (self.tmp / "notes.md").write_text("x", encoding="utf-8")

        found = list(iter_discovered_files(self.tmp, EntityPattern.AGENT))

        self.assertEqual(found, [("craftsman", self.tmp / "craftsman.agent.md")])

    def test_profile_prompts_are_found_for_every_profile(self) -> None:
        location = export.CopilotDiscoveryLocation(
            DiscoveryRoot.VSCODE_PROFILE_PROMPTS, EntityPattern.PROMPT, canonical=False
        )
        profiles = export.default_vscode_prompts_dir(self.tmp).parent / "profiles"
        (profiles / "a").mkdir(parents=True)
        (profiles / "b").mkdir()

        found = export.resolve_discovery_dirs(location, home=self.tmp, include_all_profiles=True)

        self.assertEqual(found, [profiles / "a" / "prompts", profiles / "b" / "prompts"])

    def test_file_without_the_marker_is_not_generated(self) -> None:
        path = self.tmp / "verify.prompt.md"
        path.write_text("---\nagent: 'agent'\n---\n\n> **GitHub Copilot:** note\n", encoding="utf-8")

        self.assertFalse(is_craftsman_generated_file(path))


class RenderedTextTests(unittest.TestCase):
    """Checks over every Markdown file one export to every target writes."""

    root: Path
    files: dict[Path, str]

    @classmethod
    def setUpClass(cls) -> None:
        tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(tmp.cleanup)
        cls.root = Path(tmp.name)
        export.export_vscode_instructions(REPO_ROOT, cls.root / "prompts")
        export.export_to_copilot_skills(REPO_ROOT, cls.root / "skills")
        export.export_copilot_home(REPO_ROOT, cls.root / "copilot", ("craftsman", "craftsman"))
        export.export_to_claude_user(REPO_ROOT, cls.root / "claude")
        export.export_to_agents_workspace(REPO_ROOT, cls.root / "ws")
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
