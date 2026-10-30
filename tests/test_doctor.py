"""Tests for `craftsman doctor`: repository assets, platform installs and Copilot duplicates."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from craftsman import cli
from craftsman.doctor import (
    LEFTOVER_GIT_HOOKS_PROBLEM,
    build_report,
    command_executables,
    detect_antigravity_drift,
    inspect_legacy_git_hooks,
    loads_jsonc_dict,
    plugin_skill_names,
    scan_copilot_duplicates,
    validate_skill_manifests,
)
from craftsman.export import (
    CRAFTSMAN_GENERATED_MARKER,
    HOOKS_FILENAME,
    ExportError,
    absolute_hook_prefixes,
    default_vscode_prompts_dir,
    default_vscode_user_dir,
    export_copilot_home,
    export_to_claude_user,
    export_to_copilot_skills,
    export_to_workspace,
    write_copilot_hooks,
)
from craftsman.manifest import MANIFEST_FILENAME

from tests.support import REPO_ROOT, CliResult, IsolatedHomeTestCase, run_main

VERIFY_SKILL = "---\nname: verify\n---\nClaude skill\n"


class DoctorTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()

    def run_doctor(self, *extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "craftsman.cli", "doctor", "--repo-dir", str(REPO_ROOT), *extra],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=120,
            env={
                **os.environ,
                "HOME": str(self.tmp),
                "USERPROFILE": str(self.tmp),
                "GIT_CONFIG_GLOBAL": str(self.tmp / ".gitconfig"),
            },
        )

    def doctor(self, *extra: str) -> tuple[int, dict[str, Any]]:
        proc = self.run_doctor("--json", *extra)
        report = json.loads(proc.stdout)
        assert isinstance(report, dict)
        return proc.returncode, report

    def install_user_copilot(self) -> None:
        prefixes = absolute_hook_prefixes()
        export_copilot_home(REPO_ROOT, self.tmp / ".copilot", prefixes)
        export_to_copilot_skills(REPO_ROOT, self.tmp / ".copilot" / "skills")


class PlatformReportTests(DoctorTestCase):
    def test_user_copilot_install_is_healthy(self) -> None:
        self.install_user_copilot()

        code, report = self.doctor()

        self.assertEqual(code, 0, report["problems"])
        self.assertEqual(report["copilot"]["user_hooks"]["state"], "ok")
        self.assertEqual(report["copilot"]["agents"], {"user": True, "workspace": None})

    def test_copilot_installed_in_both_scopes_is_a_duplicate(self) -> None:
        self.install_user_copilot()
        workspace = self.tmp / "ws"
        export_to_workspace(REPO_ROOT, workspace)

        code, report = self.doctor("--workspace", str(workspace))

        self.assertEqual(code, 1)
        self.assertTrue(report["copilot"]["duplicates"])

    def test_missing_lenses_file_is_a_problem(self) -> None:
        self.install_user_copilot()
        (self.tmp / ".copilot" / "skills" / "code-review" / "lenses.md").unlink()

        code, report = self.doctor()

        self.assertEqual(code, 1)
        self.assertIn("has no lenses.md", report["problems"][0])

    def test_hooks_running_a_missing_interpreter_are_broken(self) -> None:
        self.install_user_copilot()
        hooks_dir = self.tmp / ".copilot" / "hooks"
        write_copilot_hooks(hooks_dir, '"/no/such/python3" -m craftsman.cli', "& x")

        _, report = self.doctor()

        hooks = report["copilot"]["user_hooks"]
        self.assertEqual((hooks["state"], hooks["missing"]), ("broken", ["/no/such/python3"]))

    def test_hooks_without_this_platforms_command_are_broken(self) -> None:
        self.install_user_copilot()
        hooks_file = self.tmp / ".copilot" / "hooks" / HOOKS_FILENAME
        key = "powershell" if os.name == "nt" else "bash"
        data = json.loads(hooks_file.read_text(encoding="utf-8"))
        for group in data["hooks"].values():
            for entry in group:
                del entry[key]
        hooks_file.write_text(json.dumps(data), encoding="utf-8")

        _, report = self.doctor()

        hooks = report["copilot"]["user_hooks"]
        self.assertEqual((hooks["state"], hooks["missing"]), ("broken", []))
        self.assertIn(
            "has no `bash` command" if os.name != "nt" else "has no `powershell` command", hooks["error"]
        )

    def test_hook_command_with_unbalanced_quotes_is_broken(self) -> None:
        self.install_user_copilot()
        hooks_dir = self.tmp / ".copilot" / "hooks"
        write_copilot_hooks(hooks_dir, '"/usr/bin/python3 -m craftsman.cli', '& "x')

        _, report = self.doctor()

        hooks = report["copilot"]["user_hooks"]
        self.assertEqual(hooks["state"], "broken")
        self.assertIn("unparsable hook command", hooks["error"])

    def test_unreadable_claude_settings_are_a_problem(self) -> None:
        (self.tmp / ".claude").mkdir()
        (self.tmp / ".claude" / "settings.json").write_text("{", encoding="utf-8")

        code, report = self.doctor()

        self.assertEqual((code, report["claude"]["hooks"]), (1, "unreadable"))

    def test_text_report_lists_the_problems(self) -> None:
        self.install_user_copilot()
        (self.tmp / ".copilot" / "skills" / "code-review" / "lenses.md").unlink()

        proc = self.run_doctor()

        self.assertIn("[ISSUES DETECTED]", proc.stdout)
        self.assertIn("[ERROR]", proc.stderr)
        self.assertIn("has no lenses.md", proc.stderr)

    def test_claude_agents_without_hooks_are_reported(self) -> None:
        export_to_claude_user(REPO_ROOT, self.tmp / ".claude")

        _, report = self.doctor()

        self.assertEqual((report["claude"]["agents"], report["claude"]["hooks"]), (True, "missing"))

    def test_hook_commands_are_parsed_with_the_quoting_of_their_shell(self) -> None:
        windows = r'& "C:\Program Files\Python312\python.exe" "C:\src\craftsman-cli.py" hook stop'
        posix = "'/opt/my python/bin/python3' -m craftsman.cli hook stop --platform copilot"

        self.assertEqual(
            command_executables(windows, powershell=True),
            [r"C:\Program Files\Python312\python.exe", r"C:\src\craftsman-cli.py"],
        )
        self.assertEqual(command_executables(posix, powershell=False), ["/opt/my python/bin/python3"])


class SkillManifestTests(DoctorTestCase):
    def copy_repo(self) -> Path:
        repo = self.tmp / "repo"
        for folder in ("skills", "rules", "scripts", "agents"):
            shutil.copytree(REPO_ROOT / folder, repo / folder)
        shutil.copy(REPO_ROOT / "plugin.json", repo / "plugin.json")
        return repo

    def test_bundled_skills_are_valid(self) -> None:
        count, errors = validate_skill_manifests(REPO_ROOT)

        self.assertEqual(errors, [])
        self.assertEqual(count, len(plugin_skill_names(REPO_ROOT)))

    def test_skill_folder_must_be_named_after_the_skill(self) -> None:
        repo = self.copy_repo()
        (repo / "skills" / "verify").rename(repo / "skills" / "verify_skill")

        _, errors = validate_skill_manifests(repo)

        self.assertEqual(errors, ["verify_skill: the folder must be named after the skill (verify)"])

    def test_plugin_manifest_must_list_the_skill_folders(self) -> None:
        repo = self.copy_repo()
        names = json.loads((repo / "plugin.json").read_text(encoding="utf-8"))
        names["skills"] = ["code_quality" if n == "code-quality" else n for n in names["skills"]]
        (repo / "plugin.json").write_text(json.dumps(names), encoding="utf-8")

        report = build_report(repo, None, self.tmp)

        self.assertIn(
            "plugin.json lists skill 'code_quality' that skills/ does not have", report.skill_errors
        )
        self.assertIn("skills/code-quality is not listed in plugin.json", report.skill_errors)

    def test_unreadable_plugin_manifest_is_an_error(self) -> None:
        repo = self.copy_repo()
        (repo / "plugin.json").write_text("{", encoding="utf-8")

        with self.assertRaisesRegex(ExportError, "cannot read"):
            plugin_skill_names(repo)

    def test_skill_listed_twice_in_plugin_manifest_is_an_error(self) -> None:
        repo = self.copy_repo()
        manifest = json.loads((repo / "plugin.json").read_text(encoding="utf-8"))
        manifest["skills"].append("verify")
        (repo / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")

        with self.assertRaisesRegex(ExportError, "lists verify more than once"):
            plugin_skill_names(repo)


class DriftTests(DoctorTestCase):
    def test_changed_skill_or_module_is_drift(self) -> None:
        config = self.tmp / "config"
        (config / "skills" / "craftsman").mkdir(parents=True)
        (config / "skills" / "craftsman" / "SKILL.md").write_text("drifted\n", encoding="utf-8")
        (config / "craftsman").mkdir(parents=True)
        (config / "craftsman" / "scope.py").write_text("# stale scope\n", encoding="utf-8")

        drifted = detect_antigravity_drift(REPO_ROOT, config)

        self.assertIn("skills/craftsman/SKILL.md", drifted)
        self.assertIn("craftsman/scope.py", drifted)


class JsoncTests(unittest.TestCase):
    def test_jsonc_comments_with_trailing_commas_are_ignored(self) -> None:
        raw = '{\n  // a comment\n  "a": [1, 2,], /* block */\n  "b": {"c": 3,},\n}\n'

        self.assertEqual(loads_jsonc_dict(raw), {"a": [1, 2], "b": {"c": 3}})

    def test_string_ending_in_an_escaped_backslash_closes(self) -> None:
        raw = '{"p": "C:\\\\dir\\\\", // c\n "q": 1}'

        self.assertEqual(loads_jsonc_dict(raw), {"p": "C:\\dir\\", "q": 1})

    def test_comment_markers_inside_strings_are_kept(self) -> None:
        raw = '{"url": "https://x//y", "list": "a,]"}'

        self.assertEqual(loads_jsonc_dict(raw), {"url": "https://x//y", "list": "a,]"})

    def test_text_that_is_no_object_is_none(self) -> None:
        self.assertIsNone(loads_jsonc_dict("[1]"))

    def test_text_that_is_no_jsonc_is_an_error(self) -> None:
        with self.assertRaises(json.JSONDecodeError):
            loads_jsonc_dict('{"a": ')


class CopilotDuplicateTests(DoctorTestCase):
    def claude_skill(self) -> Path:
        path = self.tmp / ".claude" / "skills" / "verify" / "SKILL.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(VERIFY_SKILL, encoding="utf-8")
        return path

    def user_prompt(self) -> Path:
        prompts = default_vscode_prompts_dir(self.tmp)
        prompts.mkdir(parents=True, exist_ok=True)
        path = prompts / "verify.prompt.md"
        path.write_text("---\nname: verify\n---\nPrompt\n", encoding="utf-8")
        return path

    def test_prompt_named_like_a_skill_is_a_duplicate(self) -> None:
        claude_skill, prompt = self.claude_skill(), self.user_prompt()

        (dup,) = scan_copilot_duplicates(REPO_ROOT, home=self.tmp)

        self.assertEqual(set(dup.paths), {str(claude_skill), str(prompt)})

    def test_location_disabled_in_jsonc_settings_is_no_duplicate(self) -> None:
        self.claude_skill()
        settings = default_vscode_prompts_dir(self.tmp).parent / "settings.json"
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text(
            '{\n  // comment\n  "chat.agentSkillsLocations": {\n    "~/.claude/skills": false,\n  },\n}\n',
            encoding="utf-8",
        )
        export_to_copilot_skills(REPO_ROOT, self.tmp / ".copilot" / "skills")

        self.assertEqual(scan_copilot_duplicates(REPO_ROOT, home=self.tmp), [])

    def test_malformed_vscode_settings_are_an_error(self) -> None:
        self.claude_skill()
        settings = default_vscode_prompts_dir(self.tmp).parent / "settings.json"
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text('{"chat.agentSkillsLocations": ', encoding="utf-8")
        export_to_copilot_skills(REPO_ROOT, self.tmp / ".copilot" / "skills")

        with self.assertRaisesRegex(ExportError, "cannot read VS Code settings"):
            scan_copilot_duplicates(REPO_ROOT, home=self.tmp)


class InstalledDuplicateTests(IsolatedHomeTestCase):
    def doctor(self, *args: str) -> CliResult:
        return run_main(cli.main, "doctor", "--repo-dir", str(REPO_ROOT), *args)

    def report(self, *args: str) -> tuple[int, dict[str, Any]]:
        result = self.doctor("--json", *args)
        report = json.loads(result.out)
        assert isinstance(report, dict)
        return result.code, report

    def marked(self, path: Path, frontmatter: str) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"---\n{frontmatter}\n---\n\n{CRAFTSMAN_GENERATED_MARKER}\n", encoding="utf-8")
        return path

    def test_fresh_copilot_install_has_no_duplicates(self) -> None:
        self.assert_ok(self.install("--copilot"))

        code, report = self.report()

        self.assertEqual(code, 0, report["problems"])
        self.assertEqual(report["copilot"]["duplicates"], [])

    def test_marked_agent_in_the_prompts_folder_is_a_duplicate(self) -> None:
        self.assert_ok(self.install("--copilot"))
        self.marked(
            default_vscode_prompts_dir(self.home) / "craftsman.agent.md", "name: craftsman"
        )

        code, report = self.report()

        self.assertEqual(code, 1)
        names = {(d["client"], d["kind"], d["name"]) for d in report["copilot"]["duplicates"]}
        self.assertIn(("vscode", "agent", "craftsman"), names)

    def test_duplicate_names_the_installed_copy_to_keep(self) -> None:
        self.assert_ok(self.install("--copilot"))
        prompt = default_vscode_prompts_dir(self.home) / "simplify.prompt.md"
        prompt.write_text("---\nagent: 'agent'\n---\nMy prompt.\n", encoding="utf-8")

        result = self.doctor()

        self.assertEqual(result.code, 1)
        kept = self.home / ".copilot" / "skills" / "simplify" / "SKILL.md"
        self.assertIn(f"keep {kept}; rename or remove the other copies", result.err)

    def test_claude_agents_next_to_copilot_agents_are_reported(self) -> None:
        self.assert_ok(self.install("--copilot"))
        self.assert_ok(self.install("--claude"))

        code, report = self.report()

        self.assertEqual(code, 1)
        self.assertTrue(any("chat.agentFilesLocations" in problem for problem in report["problems"]))

    def test_claude_agents_disabled_in_vscode_are_no_duplicates(self) -> None:
        self.assert_ok(self.install("--copilot"))
        self.assert_ok(self.install("--claude", "--no-claude-hooks"))
        settings = default_vscode_user_dir(self.home) / "settings.json"
        settings.write_text(
            json.dumps({"chat.agentFilesLocations": {"~/.claude/agents": False}}), encoding="utf-8"
        )

        code, report = self.report()

        self.assertEqual(code, 0, report["problems"])

    def test_both_scopes_installed_point_to_uninstalling_one(self) -> None:
        workspace = self.tmp / "ws"
        workspace.mkdir()
        self.assert_ok(self.install("--copilot"))
        self.assert_ok(self.install("--copilot-workspace", str(workspace), "--allow-duplicate-scopes"))

        result = self.doctor("--workspace", str(workspace))

        self.assertEqual(result.code, 1)
        self.assertIn("both user and workspace scopes are installed", result.err)


class LegacyGitHooksInspectionTests(IsolatedHomeTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.hooks_dir = self.home / ".craftsman" / "git-hooks"

    def test_leftover_git_hooks_manifest_warns(self) -> None:
        self.write_legacy_git_hooks(set_hooks_path=False)

        self.assertEqual(
            inspect_legacy_git_hooks(None, self.home), [LEFTOVER_GIT_HOOKS_PROBLEM]
        )

    def test_craftsman_hooks_path_without_manifest_warns(self) -> None:
        self.assertEqual(
            inspect_legacy_git_hooks(str(self.hooks_dir), self.home), [LEFTOVER_GIT_HOOKS_PROBLEM]
        )

    def test_uninstall_clears_leftover_git_hooks_warning(self) -> None:
        self.write_legacy_git_hooks()

        self.assert_ok(self.uninstall())

        result = run_main(cli.main, "doctor", "--json", "--repo-dir", str(REPO_ROOT))
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(json.loads(result.out)["problems"], [])

    def test_hooks_of_another_tool_are_not_flagged(self) -> None:
        (self.tmp / "husky").mkdir()

        self.assertEqual(inspect_legacy_git_hooks(str(self.tmp / "husky"), self.home), [])

    def test_unreadable_git_config_is_a_problem(self) -> None:
        self.git_config.write_text("[core\n", encoding="utf-8")

        result = run_main(cli.main, "doctor", "--json", "--repo-dir", str(REPO_ROOT))

        self.assertEqual(result.code, 1)
        problems = json.loads(result.out)["problems"]
        self.assertTrue(any("git config --global --get user.name failed" in p for p in problems))

    def test_corrupt_manifest_is_a_problem(self) -> None:
        self.hooks_dir.parent.mkdir(parents=True)
        (self.hooks_dir.parent / MANIFEST_FILENAME).write_text("{", encoding="utf-8")

        (problem,) = inspect_legacy_git_hooks(None, self.home)

        self.assertIn("cannot read install manifest", problem)


if __name__ == "__main__":
    unittest.main()
