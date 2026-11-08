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

    def test_status_counts_uncovered_hunks(self) -> None:
        lines = [f"value_{index} = {index}\n" for index in range(30)]
        self.repo.write("app.py", "".join(lines))
        self.repo.git("commit", "-qam", "values")
        self.repo.write("app.py", "".join(["first = 0\n", *lines[1:-1], "last = 0\n"]))
        self.repo.cli("review", "prepare", "--base", "HEAD")

        ledger = self.status()["ledger"]

        self.assertEqual(ledger["coverage"], {"covered": 0, "total": 2})
        self.assertFalse(ledger["deep"])

    def test_status_without_gate_is_idle(self) -> None:
        payload = self.status()
        self.assertEqual((payload["active"], payload["stage"], payload["owner"]), (False, "idle", None))

    def test_status_reports_the_diff_since_the_base(self) -> None:
        self.repo.write("app.py", "x = 2\ny = 3\n")
        diff = self.status()["diff"]
        self.assertEqual((diff["changed_files"], diff["total_changed_lines"]), (1, 3))

    def test_status_reports_the_active_gate(self) -> None:
        open_gate(self.repo.root, self.repo.base_sha, deep=True, owner=GateOwner("claude", "s1"))
        payload = self.status()
        self.assertEqual(
            (payload["active"], payload["deep"], payload["owner"]),
            (True, True, {"platform": "claude", "session_id": "s1"}),
        )

    def test_status_text_names_the_owner(self) -> None:
        open_gate(self.repo.root, self.repo.base_sha, owner=GateOwner("claude", "s1"))
        self.assertIn("Owner          : claude (session s1)", self.repo.cli("status").stdout)

    def test_status_rejects_an_invalid_gate_state(self) -> None:
        state_file = self.repo.ledger_dir / "craftsman_state.json"
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(json.dumps({"active": True, "mode": "ultracraft"}), encoding="utf-8")
        assert_cli_error(self, self.repo.cli("status", check=False), 1, "not a valid gate state")

    @property
    def ledger_file(self) -> Path:
        return self.repo.ledger_dir / "ledger.json"

    def write_ledger(self, content: str) -> None:
        self.ledger_file.parent.mkdir(parents=True, exist_ok=True)
        self.ledger_file.write_text(content, encoding="utf-8")

    def test_status_of_a_healthy_ledger_reports_no_problem(self) -> None:
        self.repo.write("app.py", "x = 2\n")
        self.repo.cli("review", "prepare")
        payload = self.status()

        self.assertIsNone(payload["ledger_problem"])
        self.assertIsNone(payload["ledger_error"])
        self.assertEqual(payload["ledger_backups"], [])
        self.assertEqual(payload["ledger_path"], str(self.ledger_file))

    def test_status_does_not_show_a_green_exit_code_for_a_run_that_ran_no_tests(self) -> None:
        self.repo.write("app.py", "x = 2\n")
        self.repo.cli("review", "prepare")
        code = "assert True; print('Ran 0 tests in 0.0s')"
        self.repo.cli("run", "--kind", "test", "--", "python3", "-c", code)

        text = self.repo.cli("status")

        self.assertIn("R1 test exit 0, 0 tests - not counted", text.stdout)

    def test_status_names_a_corrupted_ledger_instead_of_showing_an_empty_one(self) -> None:
        self.write_ledger("{not json")

        text = self.repo.cli("status", check=False)
        proc = self.repo.cli("status", "--json", check=False)
        payload = json.loads(proc.stdout)

        self.assertEqual((text.returncode, proc.returncode), (1, 1))
        self.assertIn(f"UNAVAILABLE - {self.ledger_file}", text.stdout)
        self.assertIn("is not valid JSON", text.stdout)
        self.assertNotIn("no review round yet", text.stdout)
        self.assertIn("the review ledger is unusable", text.stderr)
        self.assertEqual(payload["ledger_problem"], "invalid")
        self.assertIsNone(payload["ledger"])
        self.assertEqual(payload["ledger_error"].count(str(self.ledger_file)), 1)
        self.assertEqual(self.ledger_file.read_text(encoding="utf-8"), "{not json")

    def test_status_names_the_unknown_fields_of_a_same_schema_ledger(self) -> None:
        self.repo.write("app.py", "x = 2\n")
        self.repo.cli("review", "prepare")
        raw = json.loads(self.ledger_file.read_text(encoding="utf-8"))
        self.write_ledger(json.dumps({**raw, "from_the_future": 1}))

        proc = self.repo.cli("status", check=False)

        self.assertEqual(proc.returncode, 1)
        self.assertIn("from_the_future is not a known field", proc.stdout)
        self.assertIn(f"{self.ledger_file}: from_the_future", proc.stdout)
        self.assertNotIn("ledger.json: ledger.json", proc.stdout)
        self.assertNotIn(f"{self.ledger_file}: {self.ledger_file.name}:", proc.stdout)
        self.assertIn("a newer Craftsman may have written it", proc.stdout)

    def test_status_reports_a_ledger_of_a_newer_schema_and_leaves_it_alone(self) -> None:
        self.write_ledger(json.dumps({"schema": 5, "rounds": []}))
        before = self.ledger_file.read_text(encoding="utf-8")

        text = self.repo.cli("status", check=False)
        proc = self.repo.cli("status", "--json", check=False)
        payload = json.loads(proc.stdout)

        message = "this ledger was written by a newer Craftsman (schema 5); upgrade craftsman"
        self.assertEqual((text.returncode, proc.returncode), (1, 1))
        self.assertIn(message, text.stdout)
        self.assertEqual(payload["ledger_problem"], "newer-schema")
        self.assertIn(message, payload["ledger_error"])
        self.assertEqual(self.ledger_file.read_text(encoding="utf-8"), before)
        self.assertEqual(list(self.repo.ledger_dir.glob("*.bak*")), [])

    def test_status_reports_an_older_ledger_without_setting_it_aside(self) -> None:
        self.write_ledger(json.dumps({"schema": 2}))

        text = self.repo.cli("status")
        payload = json.loads(self.repo.cli("status", "--json").stdout)

        self.assertIn("written by an older craftsman (schema 2)", text.stdout)
        self.assertEqual(payload["ledger_problem"], "older-schema")
        self.assertTrue(self.ledger_file.is_file())
        self.assertEqual(payload["ledger_backups"], [])

    def test_status_lists_the_ledgers_an_upgrade_set_aside(self) -> None:
        self.write_ledger(json.dumps({"schema": 2}))
        self.repo.cli("review", "show")  # a writing-or-reading review command sets it aside
        (backup,) = self.repo.ledger_dir.glob("ledger.v2.*.bak.json")

        text = self.repo.cli("status")
        payload = json.loads(self.repo.cli("status", "--json").stdout)

        self.assertIn(f"Set-aside ledger: {backup}", text.stdout)
        self.assertEqual(payload["ledger_backups"], [str(backup)])
        self.assertIsNone(payload["ledger_problem"])

    def test_commands_refuse_a_ledger_of_a_newer_schema_and_never_rewrite_it(self) -> None:
        self.write_ledger(json.dumps({"schema": 5, "rounds": []}))
        before = self.ledger_file.read_text(encoding="utf-8")
        message = "this ledger was written by a newer Craftsman (schema 5); upgrade craftsman"
        commands = (
            ("review", "show"),
            ("review", "show", "--json"),
            ("review", "prepare"),
            ("review", "check", "--review-only"),
            ("review", "reset"),
            ("run", "--kind", "test", "--", "pytest"),
        )

        for command in commands:
            with self.subTest(command=command):
                proc = self.repo.cli(*command, check=False)
                assert_cli_error(self, proc, 1, message)
                self.assertEqual(self.ledger_file.read_text(encoding="utf-8"), before)
        self.assertEqual(list(self.repo.ledger_dir.glob("*.bak*")), [])

    def test_status_outside_a_git_work_tree_fails(self) -> None:
        outside = self.tmp / "plain"
        outside.mkdir()
        proc = self.repo.cli("status", "--cwd", str(outside), check=False)
        assert_cli_error(self, proc, 1, "craftsman status: error:")


class SkillCommandTests(unittest.TestCase):
    def test_skill_is_found_by_its_name(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = skill_main(["path", "simplify", "--repo-dir", str(REPO_ROOT)])

        self.assertEqual((code, out.getvalue().strip()), (0, str(REPO_ROOT / "skills" / "simplify")))

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
        result = self.skill("show", "craft-review", "--file", "lenses.md")

        lenses = (REPO_ROOT / "skills" / "craft-review" / "lenses.md").read_text(encoding="utf-8")
        self.assertEqual(result.out, lenses)

    def test_unknown_skill_is_a_usage_error(self) -> None:
        result = self.skill("show", "no-such-skill")

        self.assertEqual(result.code, 2)
        self.assertIn("unknown skill 'no-such-skill'", result.err)

    def test_file_outside_the_skill_is_refused(self) -> None:
        for bad_file in ("../craftsman/SKILL.md", "/etc/passwd", "missing.md", ""):
            with self.subTest(file=bad_file):
                result = self.skill("show", "craft-review", "--file", bad_file)
                self.assertEqual(result.code, 2)
                self.assertIn("craftsman skill: error:", result.err)

    def test_file_symlinked_out_of_the_skill_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp).resolve() / "repo"
            skill_dir = repo / "skills" / "craft-review"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\nname: craft-review\ndescription: x\n---\nbody\n", encoding="utf-8"
            )
            (repo.parent / "secret.md").write_text("top secret\n", encoding="utf-8")
            (skill_dir / "escape.md").symlink_to(repo.parent / "secret.md")

            result = run_main(
                skill_main, "show", "craft-review", "--file", "escape.md", "--repo-dir", str(repo)
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
