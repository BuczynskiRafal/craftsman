"""Tests for `craftsman status`."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from craftsman import __version__
from craftsman.cli import main, skill_main
from craftsman.gate import GateOwner

from tests.support import (
    REPO_ROOT,
    SKILL_COUNT,
    CliResult,
    EvidenceRepo,
    assert_cli_error,
    open_gate,
    run_main,
)


class StatusTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.repo = EvidenceRepo(self.tmp / "repo")

    def status(self) -> dict[str, Any]:
        payload = json.loads(self.repo.cli("status", "--json").stdout)
        assert isinstance(payload, dict)
        return payload

    def test_status_counts_design_coverage_per_file(self) -> None:
        lines = [f"value_{index} = {index}\n" for index in range(30)]
        self.repo.write("app.py", "".join(lines))
        self.repo.git("commit", "-qam", "values")
        self.repo.write("app.py", "".join(["first = 0\n", *lines[1:-1], "last = 0\n"]))
        self.repo.cli("review", "prepare", "--tier", "standard", "--base", "HEAD")

        ledger = self.status()["ledger"]

        lenses = len(self.repo.show()["round"]["lenses"])
        self.assertEqual(ledger["coverage"], {"covered": 0, "total": 2 * (lenses - 1) + 1})

    def test_status_without_gate_is_idle(self) -> None:
        payload = self.status()
        self.assertEqual((payload["active"], payload["stage"], payload["owner"]), (False, "idle", None))

    def test_status_reports_the_diff_since_the_base(self) -> None:
        self.repo.write("app.py", "x = 2\ny = 3\n")
        diff = self.status()["diff"]
        self.assertEqual((diff["changed_files"], diff["total_changed_lines"]), (1, 3))

    def test_small_change_gets_the_quick_gear(self) -> None:
        self.repo.write("app.py", "x = 2\n")
        self.assertEqual(self.status()["effective_gear"], "quick")

    def test_change_beyond_the_quick_file_limit_gets_the_standard_gear(self) -> None:
        for name in ("app.py", "u1.py", "u2.py"):
            self.repo.write(name, "a = 9\n")
        self.assertEqual(self.status()["effective_gear"], "standard")

    def test_status_reports_the_active_gate(self) -> None:
        open_gate(self.repo.root, self.repo.base_sha, tier="deep", owner=GateOwner("claude", "s1"))
        payload = self.status()
        self.assertEqual(
            (payload["active"], payload["effective_gear"], payload["owner"]),
            (True, "deep", {"platform": "claude", "session_id": "s1"}),
        )

    def test_status_text_names_the_owner(self) -> None:
        open_gate(self.repo.root, self.repo.base_sha, owner=GateOwner("claude", "s1"))
        self.assertIn("Owner          : claude (session s1)", self.repo.cli("status").stdout)

    def test_status_rejects_an_invalid_gate_state(self) -> None:
        state_file = self.repo.ledger_dir / "craftsman_state.json"
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(json.dumps({"active": True, "mode": "ultracraft"}), encoding="utf-8")
        assert_cli_error(self, self.repo.cli("status", check=False), 1, "not a valid gate state")

    def test_status_outside_a_git_work_tree_fails(self) -> None:
        outside = self.tmp / "plain"
        outside.mkdir()
        proc = self.repo.cli("status", "--cwd", str(outside), check=False)
        assert_cli_error(self, proc, 1, "craftsman status: error:")


class SkillCommandTests(unittest.TestCase):
    def test_skill_is_found_by_its_name(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = skill_main(["path", "code-quality", "--repo-dir", str(REPO_ROOT)])

        self.assertEqual((code, out.getvalue().strip()), (0, str(REPO_ROOT / "skills" / "code-quality")))

    def skill(self, *args: str) -> CliResult:
        return run_main(skill_main, *args, "--repo-dir", str(REPO_ROOT))

    def test_list_names_every_skill(self) -> None:
        result = self.skill("list", "--json")

        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(len(json.loads(result.out)), SKILL_COUNT)

    def test_show_prints_the_skill(self) -> None:
        result = self.skill("show", "craftsman")

        self.assertEqual(
            result.out, (REPO_ROOT / "skills" / "craftsman" / "SKILL.md").read_text(encoding="utf-8")
        )

    def test_show_prints_a_companion_file(self) -> None:
        result = self.skill("show", "code-review", "--file", "lenses.md")

        lenses = (REPO_ROOT / "skills" / "code-review" / "lenses.md").read_text(encoding="utf-8")
        self.assertEqual(result.out, lenses)

    def test_unknown_skill_is_a_usage_error(self) -> None:
        result = self.skill("show", "no-such-skill")

        self.assertEqual(result.code, 2)
        self.assertIn("unknown skill 'no-such-skill'", result.err)

    def test_file_outside_the_skill_is_refused(self) -> None:
        for bad_file in ("../craftsman/SKILL.md", "/etc/passwd", "missing.md", ""):
            with self.subTest(file=bad_file):
                result = self.skill("show", "code-review", "--file", bad_file)
                self.assertEqual(result.code, 2)
                self.assertIn("craftsman skill: error:", result.err)

    def test_file_symlinked_out_of_the_skill_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp).resolve() / "repo"
            skill_dir = repo / "skills" / "code-review"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\nname: code-review\ndescription: x\n---\nbody\n", encoding="utf-8"
            )
            (repo.parent / "secret.md").write_text("top secret\n", encoding="utf-8")
            (skill_dir / "escape.md").symlink_to(repo.parent / "secret.md")

            result = run_main(
                skill_main, "show", "code-review", "--file", "escape.md", "--repo-dir", str(repo)
            )

        self.assertEqual(result.code, 2)
        self.assertIn("escapes skill directory", result.err)


class DispatchTests(unittest.TestCase):
    def test_version_names_this_package(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()) as out, self.assertRaises(SystemExit):
            main(["--version"])

        self.assertEqual(out.getvalue().strip(), f"craftsman {__version__}")

    def test_unknown_command_is_a_usage_error(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            main(["frobnicate"])

        self.assertEqual(caught.exception.code, 2)

    def test_without_a_command_the_help_is_printed(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = main([])

        self.assertEqual(code, 0)
        self.assertIn("doctor", out.getvalue())


if __name__ == "__main__":
    unittest.main()
