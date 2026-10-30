"""Tests for GitHub Copilot / VS Code export, hooks, and discovery (`craftsman.platforms.copilot`)."""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from craftsman import __version__, export, hook_command
from craftsman.assets import AgentTemplate, load_agent_templates
from craftsman.files import BACKUP_DIR_NAME
from craftsman.generated import CRAFTSMAN_GENERATED_MARKER, ExportError
from craftsman.hook_command import absolute_hook_prefixes
from craftsman.paths import default_vscode_prompts_dir
from craftsman.platforms.copilot import (
    CopilotDiscoveryLocation,
    DiscoveryRoot,
    EntityPattern,
    export_to_copilot_skills,
    export_to_workspace,
    export_vscode_instructions,
    iter_discovered_files,
    render_copilot_agent,
    render_copilot_hooks,
    resolve_discovery_dirs,
)

from tests.support import REPO_ROOT, SKILL_COUNT, run_main


def frontmatter(text: str) -> list[str]:
    assert text.startswith("---\n"), text[:40]
    return text.split("---\n")[1].splitlines()


class CopilotTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()


class CopilotAgentRenderingTests(CopilotTestCase):
    def templates(self) -> dict[str, AgentTemplate]:
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

    def test_reviewer_loads_assigned_lenses_without_inlining_playbook(self) -> None:
        reviewer = render_copilot_agent(self.templates()["craftsman-reviewer"])
        lenses = (REPO_ROOT / "skills" / "code-review" / "lenses.md").read_text(encoding="utf-8")

        self.assertIn("craftsman skill show code-review --lenses", reviewer)
        self.assertIn("## Search scope", reviewer)
        self.assertNotIn(lenses.strip().splitlines()[-1], reviewer)
        self.assertNotIn("{{include", reviewer)


class CopilotSkillExportTests(CopilotTestCase):
    def test_skill_folders_get_every_file_of_the_skill(self) -> None:
        out = self.tmp / "skills"
        (out / "code-review").mkdir(parents=True)
        (out / "code-review" / "SKILL.md").write_text(CRAFTSMAN_GENERATED_MARKER, encoding="utf-8")
        (out / "code-review" / "mine.md").write_text("user file\n", encoding="utf-8")

        export_to_copilot_skills(REPO_ROOT, out)

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

        changes = export_to_copilot_skills(REPO_ROOT, out)

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

        export_to_copilot_skills(REPO_ROOT, out)

        self.assertEqual(source_copy.read_text(encoding="utf-8"), "original\n")
        self.assertFalse((out / "code-review" / "lenses.md").is_symlink())

    def test_instructions_reach_every_vscode_profile(self) -> None:
        prompts = self.tmp / "Code" / "User" / "prompts"
        profile = self.tmp / "Code" / "User" / "profiles" / "work"
        profile.mkdir(parents=True)

        export_vscode_instructions(REPO_ROOT, prompts)

        self.assertTrue((prompts / "craftsman-rules.instructions.md").is_file())
        self.assertTrue((profile / "prompts" / "craftsman-rules.instructions.md").is_file())

    def test_folder_not_named_prompts_ignores_sibling_profiles(self) -> None:
        unrelated = self.tmp / "profiles" / "unrelated"
        unrelated.mkdir(parents=True)

        export_vscode_instructions(REPO_ROOT, self.tmp / "out")

        self.assertFalse((unrelated / "prompts").exists())


class HookExportTests(CopilotTestCase):
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
        export_to_workspace(REPO_ROOT, self.tmp)

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
        python = self.fake_python(folder, Path(hook_command.__file__))

        bash, powershell = absolute_hook_prefixes(python)

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

        bash, _ = absolute_hook_prefixes(python)

        shim = str(REPO_ROOT / "scripts" / "craftsman-cli.py")
        self.assertEqual(shlex.split(bash), [python, shim])

    def test_unreachable_package_without_a_shim_is_an_error(self) -> None:
        python = self.fake_python(self.tmp / "py", self.tmp / "elsewhere" / "craftsman" / "__init__.py")
        with (
            mock.patch.object(hook_command, "CLI_SHIM", Path("scripts") / "no-such-shim.py"),
            self.assertRaisesRegex(ExportError, "cannot import craftsman"),
        ):
            absolute_hook_prefixes(python)

    def test_interpreter_that_cannot_run_is_an_error(self) -> None:
        with self.assertRaisesRegex(ExportError, "cannot run"):
            absolute_hook_prefixes(str(self.tmp / "no-such-python"))

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


class DiscoveryTests(CopilotTestCase):
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
        location = CopilotDiscoveryLocation(
            DiscoveryRoot.VSCODE_PROFILE_PROMPTS, EntityPattern.PROMPT, canonical=False
        )
        profiles = default_vscode_prompts_dir(self.tmp).parent / "profiles"
        (profiles / "a").mkdir(parents=True)
        (profiles / "b").mkdir()

        found = resolve_discovery_dirs(location, home=self.tmp, include_all_profiles=True)

        self.assertEqual(found, [profiles / "a" / "prompts", profiles / "b" / "prompts"])


if __name__ == "__main__":
    unittest.main()
