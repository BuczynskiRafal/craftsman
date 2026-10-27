"""Tests for Copilot discovery table, deduplication invariant, v3.0.0 upgrade migration, and scope guards."""

from __future__ import annotations

import contextlib
import io
import json
import os
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
        for d in resolve_discovery_dirs(loc, home=home, workspace=workspace, profile=profile):
            for name, path in iter_discovered_files(d, loc.pattern):
                if name in agents_found and (
                    not only_craftsman_generated or is_craftsman_generated_file(path, valid_all)
                ):
                    agents_found[name].append(path)

    for kind in ("skill", "prompt"):
        for loc in COPILOT_DISCOVERY[("vscode", kind)]:
            for d in resolve_discovery_dirs(loc, home=home, workspace=workspace, profile=profile):
                for name, path in iter_discovered_files(d, loc.pattern):
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
        for d in resolve_discovery_dirs(loc, home=home, workspace=workspace):
            for name, path in iter_discovered_files(d, loc.pattern):
                if name in agents_found:
                    agents_found[name].append(path)

    for loc in COPILOT_DISCOVERY[("copilot-cli", "skill")]:
        for d in resolve_discovery_dirs(loc, home=home, workspace=workspace):
            for name, path in iter_discovered_files(d, loc.pattern):
                if name in skills_found:
                    skills_found[name].append(path)

    return {"agent": agents_found, "skill": skills_found}


class CopilotDiscoveryAndDedupTests(unittest.TestCase):
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
        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = install_main(["--repo-dir", str(REPO_ROOT), *args])
        return code, out.getvalue(), err.getvalue()

    def run_uninstall(self, *args: str) -> tuple[int, str, str]:
        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = uninstall_main(["--repo-dir", str(REPO_ROOT), *args])
        return code, out.getvalue(), err.getvalue()

    def run_export(self, *args: str) -> tuple[int, str, str]:
        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = export.main(["--repo-dir", str(REPO_ROOT), *args])
            except SystemExit as exc:
                code = int(exc.code) if isinstance(exc.code, int) else 2
        return code, out.getvalue(), err.getvalue()

    def run_doctor(self, *args: str) -> tuple[int, str, str]:
        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["doctor", "--repo-dir", str(REPO_ROOT), *args])
        return code, out.getvalue(), err.getvalue()

    def test_one_name_one_location_invariant_across_install_configurations(self) -> None:
        scenarios = (
            "user",
            "user_with_two_profiles",
            "workspace",
            "legacy_prompt_files",
        )
        for scenario in scenarios:
            with self.subTest(scenario=scenario):
                # Reset home for each scenario
                for child in list(self.home.iterdir()):
                    if child.is_dir():
                        import shutil

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
                    # Every profile gets instructions, never agents or prompt files by default
                    for p_dir in vscode_prompt_dirs(default_vscode_prompts_dir(self.home)):
                        self.assertTrue((p_dir / "craftsman-rules.instructions.md").is_file())
                        self.assertEqual(list(p_dir.glob("*.agent.md")), [])
                        self.assertEqual(list(p_dir.glob("*.prompt.md")), [])
                elif scenario == "workspace":
                    code, _, err = self.run_install("--copilot-workspace", str(ws))
                    self.assertEqual(code, 0, err)
                    active_ws = ws
                elif scenario == "legacy_prompt_files":
                    vsc_user = default_vscode_prompts_dir(self.home).parent
                    p1 = vsc_user / "profiles" / "profile_one"
                    p1.mkdir(parents=True)
                    profiles.append(p1)
                    code, _, err = self.run_install("--copilot", "--copilot-prompt-files")
                    self.assertEqual(code, 0, err)
                    active_ws = None

                # Verify VS Code invariant in every profile: each agent == 1, each command == 1
                for prof in profiles:
                    counts = _discover_counts_for_profile(self.home, active_ws, prof)
                    for agent_name, paths in counts["agent"].items():
                        self.assertEqual(
                            len(paths),
                            1,
                            f"{scenario} profile={prof}: agent {agent_name} found at {paths}",
                        )
                        self.assertIn(
                            CRAFTSMAN_GENERATED_MARKER,
                            paths[0].read_text(encoding="utf-8"),
                        )
                    for cmd_name, paths in counts["command"].items():
                        self.assertEqual(
                            len(paths),
                            1,
                            f"{scenario} profile={prof}: command {cmd_name} found at {paths}",
                        )
                        self.assertIn(
                            CRAFTSMAN_GENERATED_MARKER,
                            paths[0].read_text(encoding="utf-8"),
                        )

                # Verify Copilot CLI invariant
                cli_counts = _discover_counts_for_copilot_cli(self.home, active_ws)
                for agent_name, paths in cli_counts["agent"].items():
                    self.assertEqual(len(paths), 1, f"{scenario} cli agent {agent_name}: {paths}")
                if scenario != "legacy_prompt_files":
                    for skill_name, paths in cli_counts["skill"].items():
                        self.assertEqual(
                            len(paths), 1, f"{scenario} cli skill {skill_name}: {paths}"
                        )
                else:
                    for _skill_name, paths in cli_counts["skill"].items():
                        self.assertEqual(len(paths), 0)

    def test_upgrade_from_v3_0_0_removes_duplicates_and_preserves_user_files(self) -> None:
        copilot_home = default_copilot_home(self.home)
        vscode_prompts = default_vscode_prompts_dir(self.home)
        profile_prompts = vscode_prompts.parent / "profiles" / "work" / "prompts"
        profile_prompts.mkdir(parents=True)

        templates = load_agent_templates(REPO_ROOT)
        skill_names = craftsman_skill_names(REPO_ROOT)

        # Simulate v3.0.0 files WITHOUT `<!-- craftsman:generated -->` (only legacy `> **GitHub Copilot:**`)
        legacy_created: list[str] = []
        for p_dir in (vscode_prompts, profile_prompts):
            p_dir.mkdir(parents=True, exist_ok=True)
            for s_name in skill_names:
                pf = p_dir / f"{s_name}.prompt.md"
                write_text_atomic(
                    pf,
                    "---\nagent: 'agent'\ndescription: 'legacy'\n---\n\n> **GitHub Copilot:** legacy note\n\nBody\n",
                )
                legacy_created.append(str(pf))
            for tmpl in templates:
                af = p_dir / f"{tmpl.name}.agent.md"
                write_text_atomic(
                    af,
                    f"---\nname: {tmpl.name}\ndescription: 'legacy'\n---\n\n> **GitHub Copilot:** legacy note\n\nBody\n",
                )
                legacy_created.append(str(af))
            instr = p_dir / "craftsman-rules.instructions.md"
            write_text_atomic(
                instr,
                "---\napplyTo: '**'\ndescription: 'Craftsman Global Engineering & Git Rules'\n---\n# Rules\n",
            )
            legacy_created.append(str(instr))

        # Place two user files in Code/User/prompts:
        # 1) Same name as a Craftsman skill (`simplify.prompt.md`) but user-written (no marker, no Copilot note)
        user_same_name = vscode_prompts / "simplify.prompt.md"
        user_same_name_content = "---\nagent: 'agent'\ndescription: 'My personal simplify prompt'\n---\nDo my custom cleanup.\n"
        write_text_atomic(user_same_name, user_same_name_content)

        # 2) Unrelated user agent (`my-helper.agent.md`)
        user_custom_agent = vscode_prompts / "my-helper.agent.md"
        user_custom_agent_content = "---\nname: my-helper\ndescription: 'Custom'\n---\nCustom agent.\n"
        write_text_atomic(user_custom_agent, user_custom_agent_content)

        # Write v3.0.0 manifest
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

        # Run upgrade (`craftsman install --copilot`)
        code, out, err = self.run_install("--copilot")
        self.assertEqual(code, 0, err)
        self.assertIn("removed legacy duplicate:", out)

        # Verify user files are completely untouched
        self.assertTrue(user_same_name.is_file())
        self.assertEqual(user_same_name.read_text(encoding="utf-8"), user_same_name_content)
        self.assertTrue(user_custom_agent.is_file())
        self.assertEqual(user_custom_agent.read_text(encoding="utf-8"), user_custom_agent_content)

        # Verify all Craftsman legacy .agent.md and .prompt.md in prompts and profile prompts are gone
        self.assertEqual(list(vscode_prompts.glob("*.agent.md")), [user_custom_agent])
        self.assertEqual(list(vscode_prompts.glob("*.prompt.md")), [user_same_name])
        self.assertEqual(list(profile_prompts.glob("*.agent.md")), [])
        self.assertEqual(list(profile_prompts.glob("*.prompt.md")), [])

        # Verify canonical locations have exactly 1 copy of each Craftsman entity
        for prof in (None, vscode_prompts.parent / "profiles" / "work"):
            counts = _discover_counts_for_profile(
                self.home, None, prof, only_craftsman_generated=True
            )
            for agent_name, paths in counts["agent"].items():
                self.assertEqual(len(paths), 1, f"{agent_name}: {paths}")
            for cmd_name, paths in counts["command"].items():
                self.assertEqual(len(paths), 1, f"{cmd_name}: {paths}")

        # Verify manifest is consistent and only lists existing canonical paths
        manifest = load_manifest(copilot_home)
        assert manifest is not None
        for p_str in manifest.targets["copilot"].created_paths:
            self.assertTrue(Path(p_str).exists(), f"Missing manifest path: {p_str}")
            self.assertFalse(p_str.endswith(".prompt.md"))

        # Second install is idempotent (no legacy removals, no backups)
        code2, out2, err2 = self.run_install("--copilot")
        self.assertEqual(code2, 0, err2)
        self.assertNotIn("removed legacy duplicate:", out2)
        self.assertNotIn("backed up existing", out2)
        self.assertIn("0 backed up", out2)
        self.assertEqual(user_same_name.read_text(encoding="utf-8"), user_same_name_content)

    def test_workspace_and_user_duplicate_scope_refusal_and_override(self) -> None:
        ws = self.tmp / "project_ws"
        ws.mkdir()

        # 1. Install user scope first
        code, _, err = self.run_install("--copilot")
        self.assertEqual(code, 0, err)

        # 2. Installing workspace scope without flag is refused
        code_ws, _, err_ws = self.run_install("--copilot-workspace", str(ws))
        self.assertNotEqual(code_ws, 0)
        self.assertIn("Craftsman is already installed for your user in ~/.copilot", err_ws)
        self.assertIn("--allow-duplicate-scopes", err_ws)
        self.assertFalse((ws / ".github" / "agents").exists())

        # 3. Exporting workspace scope without flag is also refused
        code_exp, _, err_exp = self.run_export("--workspace", str(ws))
        self.assertNotEqual(code_exp, 0)
        self.assertIn("Craftsman is already installed for your user in ~/.copilot", err_exp)

        # 4. Passing --allow-duplicate-scopes succeeds
        code_ok, _, err_ok = self.run_install(
            "--copilot-workspace", str(ws), "--allow-duplicate-scopes"
        )
        self.assertEqual(code_ok, 0, err_ok)
        self.assertTrue((ws / ".github" / "agents" / "craftsman.agent.md").is_file())

        # 5. Uninstall user scope, then test reverse refusal (installing user scope from inside workspace)
        self.run_uninstall("--copilot")
        prev_cwd = Path.cwd()
        os.chdir(ws)
        try:
            code_rev, _, err_rev = self.run_install("--copilot")
            self.assertNotEqual(code_rev, 0)
            self.assertIn("--allow-duplicate-scopes", err_rev)

            code_rev_ok, _, err_rev_ok = self.run_install(
                "--copilot", "--allow-duplicate-scopes"
            )
            self.assertEqual(code_rev_ok, 0, err_rev_ok)
        finally:
            os.chdir(prev_cwd)

    def test_copilot_prompt_files_and_skills_dir_are_mutually_exclusive(self) -> None:
        code, _, err = self.run_export(
            "--copilot-prompt-files", "--copilot-skills-dir", str(self.tmp / "skills")
        )
        self.assertNotEqual(code, 0)
        self.assertIn("mutually exclusive", err)

    def test_uninstall_cleans_untracked_legacy_copilot_files_and_keeps_user_files(self) -> None:
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
        user_file = vscode_prompts / "verify.prompt.md"
        write_text_atomic(user_file, "---\nagent: 'agent'\n---\nUser custom verify prompt.\n")

        code, out, err = self.run_uninstall("--copilot")
        self.assertEqual(code, 0, err)
        self.assertFalse(legacy_prompt.exists())
        self.assertFalse(legacy_agent.exists())
        self.assertTrue(user_file.is_file())
        self.assertIn("removed:", out)

    def test_doctor_detects_duplicates_and_fix_duplicates_preserves_user_and_claude_files(
        self,
    ) -> None:
        # 1. Clean user install -> doctor is healthy and copilot.duplicates is empty
        code_inst, _, err_inst = self.run_install("--copilot")
        self.assertEqual(code_inst, 0, err_inst)

        code_doc, out_doc, _ = self.run_doctor("--json")
        report = json.loads(out_doc)
        self.assertEqual(code_doc, 0, report["problems"])
        self.assertTrue(report["healthy"])
        self.assertEqual(report["copilot"]["duplicates"], [])
        self.assertTrue(report["installed_targets"]["github_copilot"])

        # 2. Add legacy Craftsman duplicates + an unmarked user file with a Craftsman name
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
        user_prompt = vscode_prompts / "simplify.prompt.md"
        user_prompt_text = "---\nagent: 'agent'\ndescription: 'User simplify'\n---\nMy personal prompt.\n"
        write_text_atomic(user_prompt, user_prompt_text)

        # Doctor detects all 3 duplicates (`craftsman` agent, `code-review` skill, `simplify` skill)
        code_dup, out_dup, _ = self.run_doctor("--json")
        dup_report = json.loads(out_dup)
        self.assertEqual(code_dup, 1)
        self.assertFalse(dup_report["healthy"])
        dup_names = {(d["client"], d["kind"], d["name"]) for d in dup_report["copilot"]["duplicates"]}
        self.assertIn(("vscode", "agent", "craftsman"), dup_names)
        self.assertIn(("vscode", "skill", "code-review"), dup_names)
        self.assertIn(("vscode", "skill", "simplify"), dup_names)

        # 3. `craftsman doctor --fix-duplicates` removes Craftsman files, preserves user file
        code_fix, out_fix, err_fix = self.run_doctor("--fix-duplicates")
        self.assertFalse(legacy_agent.exists())
        self.assertFalse(legacy_prompt.exists())
        self.assertTrue(user_prompt.is_file())
        self.assertEqual(user_prompt.read_text(encoding="utf-8"), user_prompt_text)
        self.assertIn(str(legacy_agent), out_fix)
        self.assertIn(str(legacy_prompt), out_fix)
        # Still unhealthy because unmarked user_prompt collides with ~/.copilot/skills/simplify/SKILL.md
        self.assertEqual(code_fix, 1)
        self.assertIn("unmarked user file preserved", err_fix)

        # Remove user collision -> now doctor is completely healthy
        user_prompt.unlink()
        code_clean, out_clean, _ = self.run_doctor("--json")
        clean_report = json.loads(out_clean)
        self.assertEqual(code_clean, 0, clean_report["problems"])
        self.assertTrue(clean_report["healthy"])
        self.assertEqual(clean_report["copilot"]["duplicates"], [])

    def test_doctor_reports_claude_collision_and_respects_vscode_settings_disable(self) -> None:
        self.run_install("--copilot")
        self.run_install("--claude")

        claude_reviewer = self.home / ".claude" / "agents" / "craftsman-reviewer.md"
        self.assertTrue(claude_reviewer.is_file())

        # Doctor detects that VS Code discovers both ~/.copilot/agents and ~/.claude/agents
        code, out, _ = self.run_doctor("--json")
        report = json.loads(out)
        self.assertEqual(code, 1)
        self.assertFalse(report["healthy"])
        self.assertTrue(
            any("chat.agentFilesLocations" in p for p in report["problems"]),
            report["problems"],
        )

        # `--fix-duplicates` NEVER deletes Claude Code files
        self.run_doctor("--fix-duplicates")
        self.assertTrue(claude_reviewer.is_file())

        # Disabling ~/.claude/agents in VS Code settings.json resolves the duplicate in VS Code
        vscode_settings = default_vscode_prompts_dir(self.home).parent / "settings.json"
        write_text_atomic(
            vscode_settings,
            json.dumps({"chat.agentFilesLocations": {"~/.claude/agents": False}}, indent=2) + "\n",
        )
        code_ok, out_ok, _ = self.run_doctor("--json")
        ok_report = json.loads(out_ok)
        self.assertEqual(code_ok, 0, ok_report["problems"])
        self.assertTrue(ok_report["healthy"])
        self.assertEqual(ok_report["copilot"]["duplicates"], [])

    def test_doctor_fix_duplicates_preserves_workspace_canonical_install_when_both_scopes_exist(
        self,
    ) -> None:
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
        self.assertTrue(ws_agent.is_file())
        self.assertTrue(ws_skill.is_file())

        # Running `doctor --workspace ws --fix-duplicates` must NOT delete any canonical workspace or user files!
        code_fix, _, err_fix = self.run_doctor("--workspace", str(ws), "--fix-duplicates")
        self.assertEqual(code_fix, 1)
        self.assertTrue(ws_agent.is_file())
        self.assertTrue(ws_skill.is_file())
        self.assertTrue(user_agent.is_file())
        self.assertTrue(user_skill.is_file())
        self.assertIn("both user and workspace scopes are installed", err_fix)
        self.assertIn("craftsman uninstall --copilot-workspace", err_fix)

        # Workspace manifest paths all still exist on disk
        ws_manifest = load_manifest(ws)
        assert ws_manifest is not None
        for p_str in ws_manifest.targets["copilot_workspace"].created_paths:
            self.assertTrue(Path(p_str).exists(), f"Deleted workspace file: {p_str}")


if __name__ == "__main__":
    unittest.main()
