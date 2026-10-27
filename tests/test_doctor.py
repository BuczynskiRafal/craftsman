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

from craftsman import export
from craftsman.doctor import (
    build_report,
    command_executables,
    detect_antigravity_drift,
    fix_copilot_duplicates,
    loads_jsonc_dict,
    plugin_skill_names,
    scan_copilot_duplicates,
    validate_skill_manifests,
)
from craftsman.export import ExportError

from tests.support import REPO_ROOT

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
        export.export_copilot_home(REPO_ROOT, self.tmp / ".copilot")
        export.export_to_copilot_skills(REPO_ROOT, self.tmp / ".copilot" / "skills")


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
        export.export_to_workspace(REPO_ROOT, workspace)

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
        export.write_copilot_hooks(hooks_dir, '"/no/such/python3" -m craftsman.cli', "& x")

        _, report = self.doctor()

        hooks = report["copilot"]["user_hooks"]
        self.assertEqual((hooks["state"], hooks["missing"]), ("broken", ["/no/such/python3"]))

    def test_hooks_without_this_platforms_command_are_broken(self) -> None:
        self.install_user_copilot()
        hooks_file = self.tmp / ".copilot" / "hooks" / export.HOOKS_FILENAME
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
        export.write_copilot_hooks(hooks_dir, '"/usr/bin/python3 -m craftsman.cli', '& "x')

        _, report = self.doctor()

        hooks = report["copilot"]["user_hooks"]
        self.assertEqual(hooks["state"], "broken")
        self.assertIn("unparsable hook command", hooks["error"])

    def test_unreadable_claude_settings_are_a_problem(self) -> None:
        (self.tmp / ".claude").mkdir()
        (self.tmp / ".claude" / "settings.json").write_text("{", encoding="utf-8")

        code, report = self.doctor()

        self.assertEqual((code, report["claude"]["hooks"]), (1, "unreadable"))

    def test_text_report_names_the_health_and_the_problems(self) -> None:
        self.install_user_copilot()
        (self.tmp / ".copilot" / "skills" / "code-review" / "lenses.md").unlink()

        proc = self.run_doctor()

        self.assertIn("[ISSUES DETECTED]", proc.stdout)
        self.assertIn("[ERROR]", proc.stderr)
        self.assertIn("has no lenses.md", proc.stderr)

    def test_claude_agents_without_hooks_are_reported(self) -> None:
        export.export_to_claude_user(REPO_ROOT, self.tmp / ".claude")

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
        (config / "craftsman").mkdir()
        (config / "craftsman" / "guard.py").write_text("# stale guard\n", encoding="utf-8")

        drifted = detect_antigravity_drift(REPO_ROOT, config)

        self.assertIn("skills/craftsman/SKILL.md", drifted)
        self.assertIn("craftsman/guard.py", drifted)


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

    def generated_prompt(self) -> Path:
        prompts = export.default_vscode_prompts_dir(self.tmp)
        prompts.mkdir(parents=True, exist_ok=True)
        path = prompts / "verify.prompt.md"
        path.write_text(
            "---\nname: verify\n---\n<!-- craftsman:generated -->\nPrompt\n", encoding="utf-8"
        )
        return path

    def test_claude_skill_is_canonical_over_a_generated_prompt(self) -> None:
        claude_skill, prompt = self.claude_skill(), self.generated_prompt()

        (dup,) = scan_copilot_duplicates(REPO_ROOT, home=self.tmp)

        self.assertEqual((dup.canonical_path, dup.removable_paths), (str(claude_skill), (str(prompt),)))

    def test_fix_removes_the_generated_copy_only(self) -> None:
        claude_skill, prompt = self.claude_skill(), self.generated_prompt()

        removed = fix_copilot_duplicates(REPO_ROOT, home=self.tmp)

        self.assertEqual(removed, [prompt])
        self.assertTrue(claude_skill.is_file())

    def test_location_disabled_in_jsonc_settings_is_no_duplicate(self) -> None:
        self.claude_skill()
        settings = export.default_vscode_prompts_dir(self.tmp).parent / "settings.json"
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text(
            '{\n  // comment\n  "chat.agentSkillsLocations": {\n    "~/.claude/skills": false,\n  },\n}\n',
            encoding="utf-8",
        )
        export.export_to_copilot_skills(REPO_ROOT, self.tmp / ".copilot" / "skills")

        self.assertEqual(scan_copilot_duplicates(REPO_ROOT, home=self.tmp), [])

    def test_malformed_vscode_settings_are_an_error(self) -> None:
        self.claude_skill()
        settings = export.default_vscode_prompts_dir(self.tmp).parent / "settings.json"
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text('{"chat.agentSkillsLocations": ', encoding="utf-8")
        export.export_to_copilot_skills(REPO_ROOT, self.tmp / ".copilot" / "skills")

        with self.assertRaisesRegex(ExportError, "cannot read VS Code settings"):
            scan_copilot_duplicates(REPO_ROOT, home=self.tmp)


if __name__ == "__main__":
    unittest.main()
