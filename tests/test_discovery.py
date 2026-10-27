"""Tests for Copilot discovery locations, deduplication, and upgrade cleanup."""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from craftsman import cli, export
from craftsman.export import (
    COPILOT_DISCOVERY,
    CRAFTSMAN_GENERATED_MARKER,
    craftsman_agent_names,
    craftsman_entity_names,
    craftsman_skill_names,
    default_copilot_home,
    default_vscode_prompts_dir,
    is_craftsman_generated_file,
    iter_discovered_files,
    load_agent_templates,
    resolve_discovery_dirs,
    vscode_prompt_dirs,
    write_text_atomic,
)
from craftsman.install import (
    MANIFEST_FILENAME,
    install_main,
    load_manifest,
    uninstall_main,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def _discover_counts_for_profile(
    home: Path,
    workspace: Path | None,
    profile: Path | None,
    *,
    only_craftsman_generated: bool = False,
) -> dict[str, dict[str, list[Path]]]:
    """Count discovered paths per Craftsman agent and slash command for a VS Code profile."""
    valid_agents = set(craftsman_agent_names(REPO_ROOT))
    valid_skills = set(craftsman_skill_names(REPO_ROOT))
    valid_all = craftsman_entity_names(REPO_ROOT)

    agents_found: dict[str, list[Path]] = {name: [] for name in sorted(valid_agents)}
    commands_found: dict[str, list[Path]] = {name: [] for name in sorted(valid_skills)}

    for loc in COPILOT_DISCOVERY[("vscode", "agent")]:
        for directory in resolve_discovery_dirs(loc, home=home, workspace=workspace, profile=profile):
            for name, path in iter_discovered_files(directory, loc.pattern):
                if name in agents_found and (
                    not only_craftsman_generated or is_craftsman_generated_file(path, valid_all)
                ):
                    agents_found[name].append(path)

    for kind in ("skill", "prompt"):
        for loc in COPILOT_DISCOVERY[("vscode", kind)]:
            for directory in resolve_discovery_dirs(
                loc, home=home, workspace=workspace, profile=profile
            ):
                for name, path in iter_discovered_files(directory, loc.pattern):
                    if name in commands_found and (
                        not only_craftsman_generated or is_craftsman_generated_file(path, valid_all)
                    ):
                        commands_found[name].append(path)

    return {"agent": agents_found, "command": commands_found}


def _discover_counts_for_copilot_cli(
    home: Path,
    workspace: Path | None,
) -> dict[str, dict[str, list[Path]]]:
    """Count discovered paths per Craftsman agent and skill for Copilot CLI."""
    valid_agents = set(craftsman_agent_names(REPO_ROOT))
    valid_skills = set(craftsman_skill_names(REPO_ROOT))

    agents_found: dict[str, list[Path]] = {name: [] for name in sorted(valid_agents)}
    skills_found: dict[str, list[Path]] = {name: [] for name in sorted(valid_skills)}

    for loc in COPILOT_DISCOVERY[("copilot-cli", "agent")]:
        for directory in resolve_discovery_dirs(loc, home=home, workspace=workspace):
            for name, path in iter_discovered_files(directory, loc.pattern):
                if name in agents_found:
                    agents_found[name].append(path)

    for loc in COPILOT_DISCOVERY[("copilot-cli", "skill")]:
        for directory in resolve_discovery_dirs(loc, home=home, workspace=workspace):
            for name, path in iter_discovered_files(directory, loc.pattern):
                if name in skills_found:
                    skills_found[name].append(path)

    return {"agent": agents_found, "skill": skills_found}


class CopilotDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.home = self.tmp / "home"
        self.home.mkdir(parents=True)
        self.xdg = self.home / ".config"
        self.appdata = self.home / "AppData" / "Roaming"
        self.craftsman_home = self.home / ".craftsman"
        self.git_config_global = self.tmp / "gitconfig"
        self.git_config_global.write_text("", encoding="utf-8")

        self.env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self.home),
            "USERPROFILE": str(self.home),
            "XDG_CONFIG_HOME": str(self.xdg),
            "APPDATA": str(self.appdata),
            "CRAFTSMAN_HOME": str(self.craftsman_home),
            "GIT_CONFIG_GLOBAL": str(self.git_config_global),
            "GIT_CONFIG_NOSYSTEM": "1",
        }
        self._env_patcher = mock.patch.dict(os.environ, self.env, clear=True)
        self._env_patcher.start()
        self.addCleanup(self._env_patcher.stop)

    def run_install(self, *args: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = install_main(["--repo-dir", str(REPO_ROOT), *args])
        return code, out.getvalue(), err.getvalue()

    def run_uninstall(self, *args: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = uninstall_main(["--repo-dir", str(REPO_ROOT), *args])
        return code, out.getvalue(), err.getvalue()

    def run_export(self, *args: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = export.main(["--repo-dir", str(REPO_ROOT), *args])
            except SystemExit as exc:
                code = int(exc.code) if isinstance(exc.code, int) else 2
        return code, out.getvalue(), err.getvalue()

    def run_doctor(self, *args: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["doctor", "--repo-dir", str(REPO_ROOT), *args])
        return code, out.getvalue(), err.getvalue()

    def _seed_legacy_copilot_files(self) -> tuple[Path, Path, Path, Path]:
        copilot_home = default_copilot_home(self.home)
        vscode_prompts = default_vscode_prompts_dir(self.home)
        profile_prompts = vscode_prompts.parent / "profiles" / "work" / "prompts"
        profile_prompts.mkdir(parents=True)

        templates = load_agent_templates(REPO_ROOT)
        skill_names = craftsman_skill_names(REPO_ROOT)
        legacy_created: list[str] = []
        for prompts_dir in (vscode_prompts, profile_prompts):
            prompts_dir.mkdir(parents=True, exist_ok=True)
            for skill_name in skill_names:
                prompt_file = prompts_dir / f"{skill_name}.prompt.md"
                write_text_atomic(
                    prompt_file,
                    "---\nagent: 'agent'\ndescription: 'legacy'\n---\n\n> **GitHub Copilot:** legacy note\n\nBody\n",
                )
                legacy_created.append(str(prompt_file))
            for tmpl in templates:
                agent_file = prompts_dir / f"{tmpl.name}.agent.md"
                write_text_atomic(
                    agent_file,
                    f"---\nname: {tmpl.name}\ndescription: 'legacy'\n---\n\n> **GitHub Copilot:** legacy note\n\nBody\n",
                )
                legacy_created.append(str(agent_file))
            instr = prompts_dir / "craftsman-rules.instructions.md"
            write_text_atomic(
                instr,
                "---\napplyTo: '**'\ndescription: 'Craftsman Global Engineering & Git Rules'\n---\n# Rules\n",
            )
            legacy_created.append(str(instr))

        user_same_name = vscode_prompts / "simplify.prompt.md"
        write_text_atomic(
            user_same_name,
            "---\nagent: 'agent'\ndescription: 'My personal simplify prompt'\n---\nDo my custom cleanup.\n",
        )
        user_custom_agent = vscode_prompts / "my-helper.agent.md"
        write_text_atomic(
            user_custom_agent,
            "---\nname: my-helper\ndescription: 'Custom'\n---\nCustom agent.\n",
        )
        copilot_home.mkdir(parents=True, exist_ok=True)
        write_text_atomic(
            copilot_home / MANIFEST_FILENAME,
            json.dumps(
                {
                    "schema_version": 1,
                    "version": "3.0.0",
                    "mode": "copy",
                    "created_paths": legacy_created,
                    "created_dirs": [],
                    "targets": {
                        "copilot": {
                            "target": "copilot",
                            "mode": "copy",
                            "version": "3.0.0",
                            "created_paths": legacy_created,
                            "created_dirs": [],
                            "modified_files": {},
                        }
                    },
                },
                indent=2,
            )
            + "\n",
        )
        return vscode_prompts, profile_prompts, user_same_name, user_custom_agent

    def test_each_copilot_entity_is_installed_in_single_location(self) -> None:
        scenarios = (
            ("user scope", "user"),
            ("user scope with two profiles", "user_with_two_profiles"),
            ("workspace scope", "workspace"),
            ("legacy prompt files flag", "legacy_prompt_files"),
        )
        for label, scenario in scenarios:
            with self.subTest(case=label):
                for child in list(self.home.iterdir()):
                    if child.is_dir():
                        shutil.rmtree(child)
                    else:
                        child.unlink()
                ws = self.tmp / f"ws_{scenario}"
                ws.mkdir(parents=True, exist_ok=True)

                profiles: list[Path | None] = [None]
                if scenario == "user":
                    code, _, err = self.run_install("--copilot")
                    self.assertEqual(code, 0, err)
                    active_ws = None
                elif scenario == "user_with_two_profiles":
                    vsc_user = default_vscode_prompts_dir(self.home).parent
                    p1 = vsc_user / "profiles" / "profile_one"
                    p2 = vsc_user / "profiles" / "profile_two"
                    p1.mkdir(parents=True)
                    p2.mkdir(parents=True)
                    profiles.extend([p1, p2])
                    code, _, err = self.run_install("--copilot")
                    self.assertEqual(code, 0, err)
                    active_ws = None
                    for p_dir in vscode_prompt_dirs(default_vscode_prompts_dir(self.home)):
                        self.assertTrue((p_dir / "craftsman-rules.instructions.md").is_file())
                        self.assertEqual(list(p_dir.glob("*.agent.md")), [])
                        self.assertEqual(list(p_dir.glob("*.prompt.md")), [])
                elif scenario == "workspace":
                    code, _, err = self.run_install("--copilot-workspace", str(ws))
                    self.assertEqual(code, 0, err)
                    active_ws = ws
                else:
                    vsc_user = default_vscode_prompts_dir(self.home).parent
                    p1 = vsc_user / "profiles" / "profile_one"
                    p1.mkdir(parents=True)
                    profiles.append(p1)
                    code, _, err = self.run_install("--copilot", "--copilot-prompt-files")
                    self.assertEqual(code, 0, err)
                    active_ws = None

                for prof in profiles:
                    counts = _discover_counts_for_profile(self.home, active_ws, prof)
                    for agent_name, paths in counts["agent"].items():
                        self.assertEqual(len(paths), 1, f"{scenario} {agent_name}: {paths}")
                        self.assertIn(CRAFTSMAN_GENERATED_MARKER, paths[0].read_text(encoding="utf-8"))
                    for cmd_name, paths in counts["command"].items():
                        self.assertEqual(len(paths), 1, f"{scenario} {cmd_name}: {paths}")
                        self.assertIn(CRAFTSMAN_GENERATED_MARKER, paths[0].read_text(encoding="utf-8"))

                cli_counts = _discover_counts_for_copilot_cli(self.home, active_ws)
                for agent_name, paths in cli_counts["agent"].items():
                    self.assertEqual(len(paths), 1, f"{scenario} cli agent {agent_name}: {paths}")
                expected_cli_skills = 0 if scenario == "legacy_prompt_files" else 1
                for skill_name, paths in cli_counts["skill"].items():
                    self.assertEqual(
                        len(paths), expected_cli_skills, f"{scenario} {skill_name}: {paths}"
                    )

    def test_reinstall_removes_legacy_copilot_files_from_prompts_dirs(self) -> None:
        vscode_prompts, profile_prompts, user_same_name, user_custom_agent = (
            self._seed_legacy_copilot_files()
        )
        code, out, err = self.run_install("--copilot")
        self.assertEqual(code, 0, err)
        self.assertIn("removed legacy duplicate:", out)
        self.assertEqual(list(vscode_prompts.glob("*.agent.md")), [user_custom_agent])
        self.assertEqual(list(vscode_prompts.glob("*.prompt.md")), [user_same_name])
        self.assertEqual(list(profile_prompts.glob("*.agent.md")), [])
        self.assertEqual(list(profile_prompts.glob("*.prompt.md")), [])

        manifest = load_manifest(default_copilot_home(self.home))
        assert manifest is not None
        for path_str in manifest.targets["copilot"].created_paths:
            self.assertTrue(Path(path_str).exists(), f"Missing manifest path: {path_str}")
            self.assertFalse(path_str.endswith(".prompt.md"))

    def test_reinstall_preserves_user_files_without_generated_marker(self) -> None:
        _, _, user_same_name, user_custom_agent = self._seed_legacy_copilot_files()
        expected_prompt = user_same_name.read_text(encoding="utf-8")
        expected_agent = user_custom_agent.read_text(encoding="utf-8")

        self.assertEqual(self.run_install("--copilot")[0], 0)
        self.assertEqual(user_same_name.read_text(encoding="utf-8"), expected_prompt)
        self.assertEqual(user_custom_agent.read_text(encoding="utf-8"), expected_agent)

        code2, out2, err2 = self.run_install("--copilot")
        self.assertEqual(code2, 0, err2)
        self.assertNotIn("removed legacy duplicate:", out2)
        self.assertIn("0 backed up", out2)
        self.assertEqual(user_same_name.read_text(encoding="utf-8"), expected_prompt)

    def test_workspace_install_refuses_when_user_scope_is_installed(self) -> None:
        ws = self.tmp / "project_ws"
        ws.mkdir()
        self.assertEqual(self.run_install("--copilot")[0], 0)

        code_ws, _, err_ws = self.run_install("--copilot-workspace", str(ws))
        self.assertNotEqual(code_ws, 0)
        self.assertIn("Craftsman is already installed for your user in ~/.copilot", err_ws)
        self.assertIn("--allow-duplicate-scopes", err_ws)
        self.assertFalse((ws / ".github" / "agents").exists())

        code_exp, _, err_exp = self.run_export("--workspace", str(ws))
        self.assertNotEqual(code_exp, 0)
        self.assertIn("Craftsman is already installed for your user in ~/.copilot", err_exp)

    def test_allow_duplicate_scopes_flag_permits_both_copilot_scopes(self) -> None:
        ws = self.tmp / "project_ws"
        ws.mkdir()
        self.assertEqual(self.run_install("--copilot")[0], 0)

        code_ok, _, err_ok = self.run_install("--copilot-workspace", str(ws), "--allow-duplicate-scopes")
        self.assertEqual(code_ok, 0, err_ok)
        self.assertTrue((ws / ".github" / "agents" / "craftsman.agent.md").is_file())

    def test_user_install_refuses_when_workspace_scope_is_installed(self) -> None:
        ws = self.tmp / "project_ws"
        ws.mkdir()
        self.assertEqual(self.run_install("--copilot-workspace", str(ws))[0], 0)

        prev_cwd = Path.cwd()
        os.chdir(ws)
        try:
            code_rev, _, err_rev = self.run_install("--copilot")
            self.assertNotEqual(code_rev, 0)
            self.assertIn("--allow-duplicate-scopes", err_rev)

            code_ok, _, err_ok = self.run_install("--copilot", "--allow-duplicate-scopes")
            self.assertEqual(code_ok, 0, err_ok)
        finally:
            os.chdir(prev_cwd)

    def test_export_rejects_prompt_files_combined_with_skills_dir(self) -> None:
        code, _, err = self.run_export(
            "--copilot-prompt-files", "--copilot-skills-dir", str(self.tmp / "skills")
        )
        self.assertNotEqual(code, 0)
        self.assertIn("mutually exclusive", err)

    def test_uninstall_removes_untracked_legacy_copilot_files(self) -> None:
        vscode_prompts = default_vscode_prompts_dir(self.home)
        vscode_prompts.mkdir(parents=True, exist_ok=True)
        legacy_prompt = vscode_prompts / "craftsman.prompt.md"
        write_text_atomic(
            legacy_prompt,
            "---\nagent: 'craftsman'\ndescription: 'x'\n---\n\n> **GitHub Copilot:** legacy\n",
        )
        legacy_agent = vscode_prompts / "craftsman-reviewer.agent.md"
        write_text_atomic(
            legacy_agent,
            "---\nname: craftsman-reviewer\ndescription: 'x'\n---\n\n> **GitHub Copilot:** legacy\n",
        )
        code, out, err = self.run_uninstall("--copilot")
        self.assertEqual(code, 0, err)
        self.assertFalse(legacy_prompt.exists())
        self.assertFalse(legacy_agent.exists())
        self.assertIn("removed:", out)

    def test_uninstall_keeps_user_files_without_generated_marker(self) -> None:
        vscode_prompts = default_vscode_prompts_dir(self.home)
        vscode_prompts.mkdir(parents=True, exist_ok=True)
        user_file = vscode_prompts / "verify.prompt.md"
        write_text_atomic(user_file, "---\nagent: 'agent'\n---\nUser custom verify prompt.\n")

        code, _, err = self.run_uninstall("--copilot")
        self.assertEqual(code, 0, err)
        self.assertTrue(user_file.is_file())

    def test_doctor_reports_agent_installed_in_two_scopes(self) -> None:
        self.assertEqual(self.run_install("--copilot")[0], 0)
        code_doc, out_doc, _ = self.run_doctor("--json")
        report = json.loads(out_doc)
        self.assertEqual(code_doc, 0, report["problems"])
        self.assertEqual(report["copilot"]["duplicates"], [])

        vscode_prompts = default_vscode_prompts_dir(self.home)
        write_text_atomic(
            vscode_prompts / "craftsman.agent.md",
            "---\nname: craftsman\ndescription: 'x'\n---\n\n> **GitHub Copilot:** legacy\n",
        )
        code_dup, out_dup, _ = self.run_doctor("--json")
        dup_report = json.loads(out_dup)
        self.assertEqual(code_dup, 1)
        dup_names = {(d["client"], d["kind"], d["name"]) for d in dup_report["copilot"]["duplicates"]}
        self.assertIn(("vscode", "agent", "craftsman"), dup_names)

    def test_fix_duplicates_removes_obsolete_craftsman_copies(self) -> None:
        self.assertEqual(self.run_install("--copilot")[0], 0)
        vscode_prompts = default_vscode_prompts_dir(self.home)
        legacy_agent = vscode_prompts / "craftsman.agent.md"
        write_text_atomic(
            legacy_agent,
            "---\nname: craftsman\ndescription: 'x'\n---\n\n> **GitHub Copilot:** legacy\n",
        )
        legacy_prompt = vscode_prompts / "code-review.prompt.md"
        write_text_atomic(
            legacy_prompt,
            "---\nagent: 'agent'\ndescription: 'x'\n---\n\n> **GitHub Copilot:** legacy\n",
        )
        code_fix, out_fix, _ = self.run_doctor("--fix-duplicates")
        self.assertEqual(code_fix, 0)
        self.assertFalse(legacy_agent.exists())
        self.assertFalse(legacy_prompt.exists())
        self.assertIn(str(legacy_agent), out_fix)

    def test_fix_duplicates_keeps_files_without_generated_marker(self) -> None:
        self.assertEqual(self.run_install("--copilot")[0], 0)
        vscode_prompts = default_vscode_prompts_dir(self.home)
        user_prompt = vscode_prompts / "simplify.prompt.md"
        user_prompt_text = (
            "---\nagent: 'agent'\ndescription: 'User simplify'\n---\nMy personal prompt.\n"
        )
        write_text_atomic(user_prompt, user_prompt_text)

        code_fix, _, err_fix = self.run_doctor("--fix-duplicates")
        self.assertEqual(code_fix, 1)
        self.assertTrue(user_prompt.is_file())
        self.assertEqual(user_prompt.read_text(encoding="utf-8"), user_prompt_text)
        self.assertIn("unmarked user file preserved", err_fix)

    def test_doctor_reports_duplicate_when_claude_agents_are_present(self) -> None:
        self.run_install("--copilot")
        self.run_install("--claude")

        code, out, _ = self.run_doctor("--json")
        report = json.loads(out)
        self.assertEqual(code, 1)
        self.assertTrue(any("chat.agentFilesLocations" in p for p in report["problems"]))

    def test_fix_duplicates_never_deletes_claude_code_files(self) -> None:
        self.run_install("--copilot")
        self.run_install("--claude")
        claude_reviewer = self.home / ".claude" / "agents" / "craftsman-reviewer.md"
        self.run_doctor("--fix-duplicates")
        self.assertTrue(claude_reviewer.is_file())

    def test_doctor_ignores_claude_agents_disabled_in_vscode_settings(self) -> None:
        self.run_install("--copilot")
        self.run_install("--claude")
        vscode_settings = default_vscode_prompts_dir(self.home).parent / "settings.json"
        write_text_atomic(
            vscode_settings,
            json.dumps({"chat.agentFilesLocations": {"~/.claude/agents": False}}, indent=2) + "\n",
        )
        code_ok, out_ok, _ = self.run_doctor("--json")
        ok_report = json.loads(out_ok)
        self.assertEqual(code_ok, 0, ok_report["problems"])
        self.assertEqual(ok_report["copilot"]["duplicates"], [])

    def test_fix_duplicates_preserves_canonical_workspace_install(self) -> None:
        ws = self.tmp / "both_scopes_ws"
        ws.mkdir()
        self.assertEqual(self.run_install("--copilot")[0], 0)
        self.assertEqual(
            self.run_install("--copilot-workspace", str(ws), "--allow-duplicate-scopes")[0], 0
        )

        ws_agent = ws / ".github" / "agents" / "craftsman.agent.md"
        ws_skill = ws / ".github" / "skills" / "craftsman" / "SKILL.md"
        user_agent = self.home / ".copilot" / "agents" / "craftsman.agent.md"
        user_skill = self.home / ".copilot" / "skills" / "craftsman" / "SKILL.md"

        code_fix, _, err_fix = self.run_doctor("--workspace", str(ws), "--fix-duplicates")
        self.assertEqual(code_fix, 1)
        self.assertTrue(ws_agent.is_file())
        self.assertTrue(ws_skill.is_file())
        self.assertTrue(user_agent.is_file())
        self.assertTrue(user_skill.is_file())
        self.assertIn("both user and workspace scopes are installed", err_fix)
        self.assertIn("craftsman uninstall --copilot-workspace", err_fix)

        ws_manifest = load_manifest(ws)
        assert ws_manifest is not None
        for path_str in ws_manifest.targets["copilot_workspace"].created_paths:
            self.assertTrue(Path(path_str).exists(), f"Deleted workspace file: {path_str}")


if __name__ == "__main__":
    unittest.main()
