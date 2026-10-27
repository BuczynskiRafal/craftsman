"""Tests for the Copilot and Claude Code export of agents, skills, prompt files and hooks."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from craftsman import export
from craftsman.export import (
    ExportError,
    load_agent_templates,
    merge_claude_hooks,
    render_claude_agent,
    render_copilot_agent,
    render_copilot_hooks,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


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


class AgentRenderingTests(ExportTestCase):
    def templates(self) -> dict[str, export.AgentTemplate]:
        return {template.name: template for template in load_agent_templates(REPO_ROOT)}

    def test_copilot_agents_have_role_specific_frontmatter(self) -> None:
        templates = self.templates()
        self.assertEqual(set(templates), {"craftsman", "craftsman-reviewer", "craftsman-verifier"})

        orchestrator = render_copilot_agent(templates["craftsman"])
        reviewer = render_copilot_agent(templates["craftsman-reviewer"])
        verifier = render_copilot_agent(templates["craftsman-verifier"])

        self.assertEqual(
            frontmatter(orchestrator)[2:],
            [
                "tools: ['read', 'edit', 'search', 'execute', 'agent', 'todo', 'web']",
                "agents: ['craftsman-reviewer', 'craftsman-verifier']",
            ],
        )
        self.assertEqual(frontmatter(orchestrator)[0], "name: craftsman")
        self.assertTrue(frontmatter(orchestrator)[1].startswith("description: 'Engineering agent that"))
        for rendered in (reviewer, verifier):
            self.assertEqual(
                frontmatter(rendered)[2:],
                ["tools: ['read', 'search', 'execute']", "user-invocable: false"],
            )
            self.assertIn("every `craftsman` command works in bash, zsh and PowerShell", rendered)
            self.assertNotIn("runSubagent", rendered)
        self.assertIn("`craftsman-reviewer` and `craftsman-verifier` custom agents", orchestrator)
        self.assertNotIn("{{include", reviewer + orchestrator)
        lenses = (REPO_ROOT / "skills" / "code-review" / "lenses.md").read_text(encoding="utf-8")
        self.assertIn(lenses.strip().splitlines()[-1], reviewer)
        self.assertIn("## Stage 0", orchestrator)
        self.assertIn("craftsman review check --review-only", orchestrator)
        self.assertIn("craftsman review report --review-only", orchestrator)

        out_agents = self.tmp / "exported_agents"
        export.export_copilot_agents(REPO_ROOT, out_agents)
        agent_file_text = (out_agents / "craftsman.agent.md").read_text(encoding="utf-8")
        self.assertIn("without `--fix`", agent_file_text)
        self.assertIn("craftsman review check --review-only", agent_file_text)
        self.assertIn("craftsman review report --review-only", agent_file_text)

    def test_claude_agents(self) -> None:
        templates = self.templates()
        reviewer = render_claude_agent(templates["craftsman-reviewer"])
        self.assertEqual(frontmatter(reviewer)[0], "name: craftsman-reviewer")
        self.assertEqual(frontmatter(reviewer)[2], "tools: Read, Grep, Glob, Bash")
        self.assertNotIn("tools:", "".join(frontmatter(render_claude_agent(templates["craftsman"]))))

        written = export.export_claude_agents(REPO_ROOT, self.tmp / "agents")
        self.assertEqual(
            sorted(path.name for path in written), ["craftsman-reviewer.md", "craftsman-verifier.md"]
        )

    def test_description_quotes_and_includes(self) -> None:
        repo = self.fake_repo("Intro\n{{include docs/part.md}}\nOutro\n")
        (template,) = load_agent_templates(repo)
        self.assertEqual(template.description, "Helps with things that aren't hard.")
        self.assertEqual(template.body, "Intro\nIncluded text.\nOutro")
        self.assertIn(
            "description: 'Helps with things that aren''t hard.'", render_copilot_agent(template)
        )

    def test_missing_include_and_bad_role_are_errors(self) -> None:
        repo = self.fake_repo("{{include docs/missing.md}}\n")
        with self.assertRaisesRegex(ExportError, r"included file not found: .*docs[/\\]missing\.md"):
            load_agent_templates(repo)
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
                repo = self.fake_repo(directive + "\n")
                with self.assertRaisesRegex(ExportError, message):
                    load_agent_templates(repo)

    def test_includes_follow_symlinked_skill_folders(self) -> None:
        repo = self.fake_repo("{{include skills/code-review/lenses.md}}\n")
        elsewhere = self.tmp / "checkout" / "code-review"
        elsewhere.mkdir(parents=True)
        (elsewhere / "lenses.md").write_text("Lens text.\n", encoding="utf-8")
        (repo / "skills").mkdir()
        (repo / "skills" / "code-review").symlink_to(elsewhere, target_is_directory=True)

        (template,) = load_agent_templates(repo)

        self.assertEqual(template.body, "Lens text.")

    def test_frontmatter_scalars_keep_inner_quotes(self) -> None:
        self.assertEqual(export._unquote('Run "craftsman review"'), 'Run "craftsman review"')
        self.assertEqual(export._unquote("'it''s'"), "it's")
        self.assertEqual(export._unquote('"say \\"hi\\""'), 'say "hi"')
        skill = "---\nname: x\ndescription: |-\n  Block text.\n---\nBody\n"
        self.assertEqual(export.extract_description_and_body(skill, "x")[0], "Block text.")
        claude = export.render_claude_command_file("x", "---\nname: x\ndescription: Match \\bword\\b\n---\nB\n")
        self.assertIn("description: 'Match \\bword\\b'", claude)


class SkillExportTests(ExportTestCase):
    def test_skill_dirs_copy_every_file(self) -> None:
        out = self.tmp / "skills"
        (out / "code-review").mkdir(parents=True)
        (out / "code-review" / "mine.md").write_text("user file\n", encoding="utf-8")

        export.export_to_copilot_skills(REPO_ROOT, out)

        lenses = (out / "code-review" / "lenses.md").read_text(encoding="utf-8")
        self.assertEqual(
            lenses, (REPO_ROOT / "skills" / "code-review" / "lenses.md").read_text(encoding="utf-8")
        )
        self.assertTrue(
            (out / "code-review" / "SKILL.md")
            .read_text(encoding="utf-8")
            .startswith("---\nname: code-review\n")
        )
        self.assertEqual((out / "code-review" / "mine.md").read_text(encoding="utf-8"), "user file\n")

    def test_symlinked_destination_files_are_replaced_not_written_through(self) -> None:
        source_copy = self.tmp / "source-lenses.md"
        source_copy.write_text("original\n", encoding="utf-8")
        out = self.tmp / "skills"
        (out / "code-review").mkdir(parents=True)
        (out / "code-review" / "lenses.md").symlink_to(source_copy)

        export.export_to_copilot_skills(REPO_ROOT, out)

        self.assertEqual(source_copy.read_text(encoding="utf-8"), "original\n")
        self.assertFalse((out / "code-review" / "lenses.md").is_symlink())

    def test_single_file_formats_inline_linked_siblings(self) -> None:
        prompts = self.tmp / "prompts"
        export.export_to_vscode_user(REPO_ROOT, prompts, prompt_files=True)
        claude = self.tmp / "claude"
        export.export_to_claude_user(REPO_ROOT, claude)

        for rendered in (
            (prompts / "code-review.prompt.md").read_text(encoding="utf-8"),
            (claude / "commands" / "code-review.md").read_text(encoding="utf-8"),
        ):
            self.assertIn("\n## Appendix: lenses.md\n\n# Craftsman Review Lenses\n", rendered)
        self.assertNotIn("## Appendix", (prompts / "verify.prompt.md").read_text(encoding="utf-8"))

    def test_prompt_files_route_craftsman_to_its_agent(self) -> None:
        default_prompts = self.tmp / "default_prompts"
        count, instr = export.export_to_vscode_user(REPO_ROOT, default_prompts)
        self.assertEqual(count, 0)
        self.assertTrue(instr.is_file())
        self.assertEqual(list(default_prompts.glob("*.prompt.md")), [])
        self.assertEqual(list(default_prompts.glob("*.agent.md")), [])

        prompts = self.tmp / "prompts"
        export.export_to_vscode_user(REPO_ROOT, prompts, prompt_files=True)
        craftsman = (prompts / "craftsman.prompt.md").read_text(encoding="utf-8")
        verify = (prompts / "verify.prompt.md").read_text(encoding="utf-8")
        self.assertEqual(frontmatter(craftsman)[0], "agent: 'craftsman'")
        self.assertEqual(frontmatter(verify)[0], "agent: 'agent'")
        self.assertNotIn("mode:", "".join(frontmatter(craftsman) + frontmatter(verify)))
        self.assertEqual(list(prompts.glob("*.agent.md")), [])

    def test_agents_md_describes_the_v3_loop(self) -> None:
        text = export.render_agents_md(REPO_ROOT)
        self.assertIn("**Done means** `craftsman review check` passes", text)
        self.assertNotIn("invoke_subagent", text.split("## Available Skills")[0])


class HookExportTests(ExportTestCase):
    def test_copilot_hooks_format(self) -> None:
        hooks = render_copilot_hooks("craftsman", "craftsman")
        self.assertEqual(hooks["version"], 1)
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
        self.assertEqual(hooks["hooks"]["preToolUse"][0]["timeoutSec"], 30)

    def test_workspace_hooks_use_path_and_never_delete_user_files(self) -> None:
        ws = self.tmp / "ws"
        mine = ws / ".github" / "agents" / "mine.agent.md"
        mine.parent.mkdir(parents=True)
        mine.write_text("keep\n", encoding="utf-8")

        export.export_to_workspace(REPO_ROOT, ws)

        hooks = json.loads((ws / ".github" / "hooks" / "craftsman.json").read_text(encoding="utf-8"))
        self.assertEqual(
            hooks["hooks"]["subagentStart"][0]["bash"],
            "craftsman hook subagent-start --platform copilot",
        )
        self.assertEqual(mine.read_text(encoding="utf-8"), "keep\n")
        self.assertEqual(
            sorted(path.name for path in mine.parent.iterdir()),
            [
                "craftsman-reviewer.agent.md",
                "craftsman-verifier.agent.md",
                "craftsman.agent.md",
                "mine.agent.md",
            ],
        )

    def test_user_hooks_run_the_interpreter_by_absolute_path(self) -> None:
        home = self.tmp / "copilot"
        proc = subprocess.run(
            [sys.executable, "-m", "craftsman.cli", "export", "--copilot-home", str(home)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
            env={**os.environ, "HOME": str(self.tmp), "USERPROFILE": str(self.tmp)},
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        entry = json.loads((home / "hooks" / "craftsman.json").read_text(encoding="utf-8"))["hooks"][
            "agentStop"
        ][0]
        words = shlex.split(entry["bash"])
        self.assertEqual(words[0], sys.executable)
        self.assertEqual(words[-4:], ["hook", "stop", "--platform", "copilot"])
        self.assertTrue(entry["powershell"].startswith(f'& "{sys.executable}" '))
        self.assertEqual(len(list((home / "agents").glob("*.agent.md"))), 3)

    def test_prefixes_quote_and_choose_module_or_shim(self) -> None:
        python = '/opt/py $HOME/"x"/python3'
        with mock.patch.object(export, "_imports_package", return_value=True):
            bash, powershell = export.absolute_hook_prefixes(python)
        self.assertEqual(bash, '"/opt/py \\$HOME/\\"x\\"/python3" -m craftsman.cli')
        if shutil.which("bash"):
            echoed = subprocess.run(
                ["bash", "-c", f"printf '%s\\n' {bash}"],
                capture_output=True,
                text=True,
                timeout=30,
                check=True,
            )
            self.assertEqual(echoed.stdout.splitlines(), [python, "-m", "craftsman.cli"])
        self.assertEqual(powershell, '& "/opt/py `$HOME/`"x`"/python3" -m craftsman.cli')
        with mock.patch.object(export, "_imports_package", return_value=False):
            bash, _ = export.absolute_hook_prefixes("/usr/bin/python3")
        self.assertEqual(
            shlex.split(bash), ["/usr/bin/python3", str(REPO_ROOT / "scripts" / "craftsman-cli.py")]
        )

    def test_an_unreachable_package_without_a_shim_is_an_error(self) -> None:
        with (
            mock.patch.object(export, "_imports_package", return_value=False),
            mock.patch.object(export, "CLI_SHIM", "scripts/no-such-shim.py"),
            self.assertRaisesRegex(ExportError, "cannot import craftsman"),
        ):
            export.absolute_hook_prefixes("/usr/bin/python3")

    def test_explicit_user_dirs_leave_the_real_copilot_home_alone(self) -> None:
        home = self.tmp / "home"
        with mock.patch.object(export, "default_copilot_home", return_value=home / ".copilot"):
            code = export.main(
                ["--repo-dir", str(REPO_ROOT), "--user-dir", str(self.tmp / "prompts"),
                 "--copilot-skills-dir", str(self.tmp / "skills")]
            )  # fmt: skip
        self.assertEqual(code, 0)
        self.assertFalse((home / ".copilot").exists())
        self.assertTrue((self.tmp / "skills" / "code-review" / "lenses.md").is_file())

    def test_shim_runs_this_checkout(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "craftsman-cli.py"), "--version"],
            cwd=self.tmp,
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
        self.assertEqual(proc.stdout.strip(), f"craftsman {export_version()}")


def export_version() -> str:
    from craftsman import __version__

    return __version__


class ClaudeSettingsMergeTests(ExportTestCase):
    PREFIX = '"/usr/bin/python3" -m craftsman.cli'

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
        merge_claude_hooks(path, self.PREFIX)
        settings = self.settings()
        late = {"hooks": [{"type": "command", "command": "notify-send late"}]}
        settings["hooks"]["Stop"].append(late)
        path.write_text(json.dumps(settings), encoding="utf-8")

        self.assertFalse(merge_claude_hooks(path, self.PREFIX))
        self.assertEqual(self.settings()["hooks"]["Stop"][-1], late)

    def test_a_symlinked_settings_file_is_updated_in_place(self) -> None:
        real = self.tmp / "dotfiles" / "settings.json"
        real.parent.mkdir()
        real.write_text("{}", encoding="utf-8")
        real.chmod(0o644)
        link = self.tmp / "settings.json"
        link.symlink_to(real)

        merge_claude_hooks(link, self.PREFIX)

        self.assertTrue(link.is_symlink())
        self.assertIn("Stop", json.loads(real.read_text(encoding="utf-8"))["hooks"])
        if os.name != "nt":
            self.assertEqual(real.stat().st_mode & 0o777, 0o644)

    def test_merge_is_idempotent_and_keeps_foreign_hooks(self) -> None:
        foreign = {"type": "command", "command": "notify-send done"}
        original: dict[str, Any] = {
            "model": "opus",
            "hooks": {
                "Stop": [
                    {
                        "hooks": [
                            foreign,
                            {"type": "command", "command": "craftsman hook stop --platform claude"},
                        ]
                    }
                ],
                "PreToolUse": [{"matcher": "Bash", "hooks": [foreign]}],
            },
            "permissions": {"allow": []},
        }
        path = self.write_settings(original)
        original_text = path.read_text(encoding="utf-8")

        self.assertTrue(merge_claude_hooks(path, self.PREFIX))
        merged = self.settings()
        self.assertEqual(list(merged), ["model", "hooks", "permissions"])
        self.assertEqual(
            list(merged["hooks"]),
            ["Stop", "PreToolUse", "UserPromptSubmit", "SubagentStart", "SubagentStop"],
        )
        self.assertEqual(
            merged["hooks"]["Stop"],
            [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": f"{self.PREFIX} hook stop --platform claude",
                            "timeout": 60,
                        }
                    ]
                },
                {"hooks": [foreign]},
            ],
        )
        self.assertEqual(
            merged["hooks"]["PreToolUse"],
            [
                *original["hooks"]["PreToolUse"],
                {
                    "matcher": "Bash|Read|Grep|Glob|LS",
                    "hooks": [
                        {
                            "type": "command",
                            "command": f"{self.PREFIX} hook pre-tool --platform claude",
                            "timeout": 30,
                        }
                    ],
                },
            ],
        )
        backup = self.tmp / "settings.json.bak"
        self.assertEqual(backup.read_text(encoding="utf-8"), original_text)

        self.assertFalse(merge_claude_hooks(path, self.PREFIX))
        self.assertEqual(self.settings(), merged)
        self.assertTrue(merge_claude_hooks(path, '"/other/python" -m craftsman.cli'))
        self.assertEqual(backup.read_text(encoding="utf-8"), original_text)
        self.assertEqual(len(self.settings()["hooks"]["Stop"]), 2)

    def test_missing_file_is_created_without_backup(self) -> None:
        path = self.tmp / "settings.json"
        self.assertTrue(merge_claude_hooks(path, self.PREFIX))
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
                    merge_claude_hooks(path, self.PREFIX)
                self.assertEqual(path.read_text(encoding="utf-8"), text)
                self.assertFalse((self.tmp / "settings.json.bak").exists())

    def test_cli_requires_claude_dir_for_hooks_and_reports_refusals(self) -> None:
        env = {**os.environ, "HOME": str(self.tmp), "USERPROFILE": str(self.tmp)}
        base = [sys.executable, "-m", "craftsman.cli", "export"]
        alone = subprocess.run(
            [*base, "--claude-hooks"], cwd=REPO_ROOT, capture_output=True, text=True, timeout=60, env=env
        )
        self.assertEqual(alone.returncode, 2)
        self.assertIn("--claude-hooks and --no-claude-hooks require --claude-dir", alone.stderr)

        claude = self.tmp / "claude"
        claude.mkdir()
        (claude / "settings.json").write_text("{oops", encoding="utf-8")
        refused = subprocess.run(
            [*base, "--claude-dir", str(claude), "--claude-hooks"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        )
        self.assertEqual(refused.returncode, 1)
        self.assertIn("[ERROR] refusing to modify", refused.stderr)
        self.assertEqual((claude / "settings.json").read_text(encoding="utf-8"), "{oops")


class DoctorTests(ExportTestCase):
    def doctor(self, *extra: str) -> tuple[int, dict[str, Any]]:
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "craftsman.cli",
                "doctor",
                "--json",
                "--repo-dir",
                str(REPO_ROOT),
                *extra,
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=120,
            env={
                **os.environ,
                "HOME": str(self.tmp),
                "USERPROFILE": str(self.tmp),
                "APPDATA": "",
                "XDG_CONFIG_HOME": "",
            },
        )
        report = json.loads(proc.stdout)
        assert isinstance(report, dict)
        return proc.returncode, report

    def test_reports_platform_integration(self) -> None:
        ws = self.tmp / "ws"
        export.export_copilot_home(REPO_ROOT, self.tmp / ".copilot")
        export.export_to_copilot_skills(REPO_ROOT, self.tmp / ".copilot" / "skills")

        code, report = self.doctor()

        self.assertEqual(code, 0, report["problems"])
        self.assertTrue(report["healthy"])
        self.assertTrue(report["installed_targets"]["github_copilot"])
        self.assertEqual(report["cli_self_check"], {"ledger": True, "diffscan": True})
        copilot = report["copilot"]
        self.assertTrue(copilot["user_hooks"]["exists"])
        self.assertTrue(copilot["user_hooks"]["runnable"])
        self.assertEqual(copilot["agents"], {"user": True, "workspace": None})
        self.assertEqual([entry["code_review_lenses"] for entry in copilot["skills"]], [True])
        self.assertEqual(copilot["duplicates"], [])
        self.assertEqual(report["claude"], {"agents": False, "hooks_merged": False})

        # Exporting to workspace alongside user install creates duplicate scopes that doctor flags
        export.export_to_workspace(REPO_ROOT, ws)
        dup_code, dup_report = self.doctor("--workspace", str(ws))
        self.assertEqual(dup_code, 1)
        self.assertFalse(dup_report["healthy"])
        self.assertEqual(dup_report["copilot"]["agents"], {"user": True, "workspace": True})
        self.assertEqual(
            [entry["code_review_lenses"] for entry in dup_report["copilot"]["skills"]],
            [True, True],
        )
        self.assertTrue(dup_report["copilot"]["duplicates"])

        export.export_to_claude_user(REPO_ROOT, self.tmp / ".claude")
        text_proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "craftsman.cli",
                "doctor",
                "--repo-dir",
                str(REPO_ROOT),
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=120,
            env={**os.environ, "HOME": str(self.tmp), "USERPROFILE": str(self.tmp)},
        )
        self.assertIn(
            "Claude Code    : agents installed, hooks not installed (run ./install.sh --claude)",
            text_proc.stdout,
        )

    def test_missing_lenses_and_interpreter_are_unhealthy(self) -> None:
        home = self.tmp / ".copilot"
        export.export_to_copilot_skills(REPO_ROOT, home / "skills")
        (home / "skills" / "code-review" / "lenses.md").unlink()
        export.write_copilot_hooks(home / "hooks", '"/no/such/python3" -m craftsman.cli', "& x")

        code, report = self.doctor()

        self.assertEqual(code, 1)
        self.assertFalse(report["healthy"])
        self.assertEqual(report["copilot"]["user_hooks"]["missing"], ["/no/such/python3"])
        self.assertEqual(len(report["problems"]), 2)
        self.assertIn("has no lenses.md", report["problems"][0])

    def test_hook_commands_are_parsed_with_the_quoting_of_their_shell(self) -> None:
        from craftsman.cli import _command_executables

        windows = r'& "C:\Program Files\Python312\python.exe" "C:\src\craftsman-cli.py" hook stop'
        self.assertEqual(
            _command_executables(windows, powershell=True),
            [r"C:\Program Files\Python312\python.exe", r"C:\src\craftsman-cli.py"],
        )
        posix = "'/opt/my python/bin/python3' -m craftsman.cli hook stop --platform copilot"
        self.assertEqual(_command_executables(posix), ["/opt/my python/bin/python3"])

    def test_rendered_prompts_omit_filesystem_skill_lookups(self) -> None:
        import re

        user_prompts = self.tmp / "prompts"
        copilot_skills = self.tmp / "copilot_skills"
        copilot_home = self.tmp / "copilot_home"
        claude_dir = self.tmp / "claude"
        ws = self.tmp / "ws"
        export.export_to_vscode_user(REPO_ROOT, user_prompts)
        export.export_to_copilot_skills(REPO_ROOT, copilot_skills)
        export.export_copilot_home(REPO_ROOT, copilot_home)
        export.export_to_claude_user(REPO_ROOT, claude_dir)
        export.export_to_agents_workspace(REPO_ROOT, ws)

        rendered_files = [
            *user_prompts.rglob("*.md"),
            *copilot_skills.rglob("*.md"),
            *copilot_home.rglob("*.md"),
            *claude_dir.rglob("*.md"),
            ws / "AGENTS.md",
        ]
        self.assertGreater(len(rendered_files), 30)

        forbidden_phrases = (
            "installed next to this agent",
            "open the file and follow it",
            "in the `code-review` skill folder (`lenses.md`)",
        )
        bare_skill_lookup_re = re.compile(r"skills/[a-z0-9_-]+/SKILL\.md")

        for path in rendered_files:
            text = path.read_text(encoding="utf-8")
            for phrase in forbidden_phrases:
                self.assertNotIn(phrase, text, f"Forbidden phrase {phrase!r} in {path}")
            self.assertIsNone(
                bare_skill_lookup_re.search(text),
                f"Bare skills/<name>/SKILL.md lookup in {path}",
            )

    def test_rendered_prompts_include_search_scope_rules(self) -> None:
        copilot_skills = self.tmp / "copilot_skills"
        copilot_home = self.tmp / "copilot_home"
        claude_dir = self.tmp / "claude"
        ws = self.tmp / "ws"
        export.export_to_copilot_skills(REPO_ROOT, copilot_skills)
        export.export_copilot_home(REPO_ROOT, copilot_home)
        export.export_to_claude_user(REPO_ROOT, claude_dir)
        export.export_to_agents_workspace(REPO_ROOT, ws)

        for agent_file in [*copilot_home.glob("agents/*.agent.md"), *claude_dir.glob("agents/*.md")]:
            self.assertIn("Search scope", agent_file.read_text(encoding="utf-8"), str(agent_file))

        for skill_name in ("craftsman", "feature-dev", "verify", "code-review", "simplify", "modernize", "batch"):
            skill_md = copilot_skills / skill_name / "SKILL.md"
            claude_cmd = claude_dir / "commands" / f"{skill_name}.md"
            self.assertIn("## Search scope", skill_md.read_text(encoding="utf-8"), str(skill_md))
            self.assertIn("## Search scope", claude_cmd.read_text(encoding="utf-8"), str(claude_cmd))
        self.assertIn("## Search scope", (ws / "AGENTS.md").read_text(encoding="utf-8"))

    def test_skill_cli_resolves_hyphenated_frontmatter_names(self) -> None:
        from craftsman.cli import skill_main

        for name in ("code-quality", "code_quality", "extreme-optimizer", "extreme_optimizer"):
            self.assertEqual(skill_main(["show", name, "--repo-dir", str(REPO_ROOT)]), 0)
            self.assertEqual(skill_main(["path", name, "--repo-dir", str(REPO_ROOT)]), 0)

    def test_fix_duplicates_removes_legacy_prompt_keeping_claude_skill(self) -> None:
        from craftsman.cli import fix_copilot_duplicates, scan_copilot_duplicates

        vscode_prompts = export.default_vscode_prompts_dir(self.tmp)
        vscode_prompts.mkdir(parents=True, exist_ok=True)
        legacy_prompt = vscode_prompts / "verify.prompt.md"
        legacy_prompt.write_text(
            "---\nname: verify\n---\n<!-- craftsman:generated -->\nLegacy prompt\n",
            encoding="utf-8",
        )
        claude_skill = self.tmp / ".claude" / "skills" / "verify" / "SKILL.md"
        claude_skill.parent.mkdir(parents=True, exist_ok=True)
        claude_skill.write_text("---\nname: verify\n---\nClaude skill\n", encoding="utf-8")

        dups = scan_copilot_duplicates(REPO_ROOT, home=self.tmp)
        self.assertEqual(len(dups), 1)
        dup0 = dups[0] if isinstance(dups[0], dict) else dups[0].to_json()
        self.assertEqual(dup0["canonical_path"], str(claude_skill))
        self.assertEqual(dup0["removable_paths"], [str(legacy_prompt)])
        removed, _ = fix_copilot_duplicates(REPO_ROOT, home=self.tmp)
        self.assertEqual(removed, [legacy_prompt])
        self.assertFalse(legacy_prompt.exists())
        self.assertTrue(claude_skill.is_file())

    def test_jsonc_settings_disable_claude_skill_location(self) -> None:
        from craftsman.cli import scan_copilot_duplicates

        vscode_prompts = export.default_vscode_prompts_dir(self.tmp)
        vscode_prompts.mkdir(parents=True, exist_ok=True)
        claude_skill = self.tmp / ".claude" / "skills" / "verify" / "SKILL.md"
        claude_skill.parent.mkdir(parents=True, exist_ok=True)
        claude_skill.write_text("---\nname: verify\n---\nClaude skill\n", encoding="utf-8")

        settings_json = vscode_prompts.parent / "settings.json"
        settings_json.write_text(
            '{\n  // comment in JSONC\n  "chat.agentSkillsLocations": {\n    "~/.claude/skills": false,\n  },\n}\n',
            encoding="utf-8",
        )
        export.export_to_copilot_skills(REPO_ROOT, self.tmp / ".copilot" / "skills")
        self.assertEqual(scan_copilot_duplicates(REPO_ROOT, home=self.tmp), [])

    def test_env_var_for_home_honors_xdg_outside_real_home(self) -> None:
        real_home = export.user_home(None)
        with unittest.mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": "/opt/cfg"}, clear=False):
            self.assertEqual(export.env_var_for_home("XDG_CONFIG_HOME", real_home), "/opt/cfg")
            self.assertEqual(export.env_var_for_home("XDG_CONFIG_HOME", None), "/opt/cfg")
            self.assertEqual(
                export.env_var_for_home("XDG_CONFIG_HOME", self.tmp / "isolated_home"), ""
            )

    def test_craftsman_scope_fragment_matches_skill_document(self) -> None:
        from craftsman.cli import validate_skill_manifests

        scope_md = (REPO_ROOT / "skills" / "craftsman" / "scope.md").read_text(encoding="utf-8").strip()
        skill_md = (REPO_ROOT / "skills" / "craftsman" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn(scope_md, skill_md)
        _, errors = validate_skill_manifests(REPO_ROOT)
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
