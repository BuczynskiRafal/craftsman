"""Unit and acceptance tests for `craftsman.quality` (`tests/test_quality.py`)."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from craftsman.diffscan import resolve_base_sha, snapshot
from craftsman.quality import QualitySignal, SignalKind, collect_quality_signals

from tests.support import EvidenceRepo

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures" / "quality"


class QualitySignalTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.repo = EvidenceRepo(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _signals_for_current_diff(self) -> tuple[QualitySignal, ...]:
        base_sha = resolve_base_sha(self.root, "HEAD")
        snap = snapshot(self.root, base_sha)
        return collect_quality_signals(self.root, snap)

    def test_function_longer_than_budget_is_reported(self) -> None:
        body = "\n".join(f"    x_{i} = {i}" for i in range(45))
        self.repo.write("service.py", f"def over_budget(a: int) -> int:\n{body}\n    return x_0\n")

        signals = self._signals_for_current_diff()
        long_fn = [s for s in signals if s.kind == SignalKind.LONG_FUNCTION]
        self.assertEqual(len(long_fn), 1)
        self.assertEqual(long_fn[0].file, "service.py")
        self.assertIn("over_budget", long_fn[0].detail)

    def test_unchanged_long_function_is_not_reported(self) -> None:
        body = "\n".join(f"    x_{i} = {i}" for i in range(45))
        initial = (
            f"def legacy_long_fn(a: int) -> int:\n{body}\n    return x_0\n\n\n"
            "def short_helper() -> int:\n    return 1\n"
        )
        self.repo.write("service.py", initial)
        self.repo.git("add", "-A")
        self.repo.git("commit", "-q", "--no-verify", "-m", "add legacy long function")

        updated = initial.replace("    return 1\n", "    return 2\n")
        self.repo.write("service.py", updated)

        signals = self._signals_for_current_diff()
        self.assertEqual([s for s in signals if s.kind == SignalKind.LONG_FUNCTION], [])

    def test_test_name_with_version_number_is_reported(self) -> None:
        cases = (
            ("version_number", "test_v3_0_0_state_without_owner"),
            ("step_counter", "test_owner_continues_at_1_of_6"),
            ("table_count", "test_evaluate_scope_table_of_45_cases"),
            ("plan_identifier", "test_f7_scope_override_works"),
            ("conjunction_and", "test_doctor_detects_duplicates_and_fixes_them"),
            (
                "over_seventy_chars",
                "test_gate_init_refuses_foreign_active_gate_until_takeover_is_passed_explicitly",
            ),
        )
        for label, bad_name in cases:
            with self.subTest(case=label):
                self.repo.write(
                    "tests/test_sample.py",
                    f"def {bad_name}() -> None:\n    assert True\n",
                )
                signals = self._signals_for_current_diff()
                names = [s for s in signals if s.kind == SignalKind.TEST_NAME]
                self.assertEqual(len(names), 1, bad_name)
                self.assertIn(bad_name, names[0].detail)

    def test_behavior_named_test_is_not_reported(self) -> None:
        self.repo.write(
            "tests/test_sample.py",
            "def test_gate_without_owner_accepts_stop_from_any_session() -> None:\n    assert True\n",
        )
        signals = self._signals_for_current_diff()
        self.assertEqual([s for s in signals if s.kind == SignalKind.TEST_NAME], [])

    def test_identical_function_bodies_are_reported_as_duplicate_logic(self) -> None:
        self.repo.write(
            "owners.py",
            "def is_user_allowed(user_map: object, role: str) -> bool:\n"
            "    if not isinstance(user_map, dict):\n"
            "        return False\n"
            "    found = str(user_map.get('role') or '').strip()\n"
            "    if not found:\n"
            "        return False\n"
            "    return found == role\n\n\n"
            "def can_session_proceed(state_obj: object, target_role: str) -> bool:\n"
            "    if not isinstance(state_obj, dict):\n"
            "        return False\n"
            "    current = str(state_obj.get('role') or '').strip()\n"
            "    if not current:\n"
            "        return False\n"
            "    return current == target_role\n",
        )
        signals = self._signals_for_current_diff()
        dups = [s for s in signals if s.kind == SignalKind.DUPLICATE_LOGIC]
        self.assertEqual(len(dups), 1)
        self.assertIn("can_session_proceed", dups[0].detail)
        self.assertIn("is_user_allowed", dups[0].detail)

    def test_public_function_taking_plain_dict_is_reported(self) -> None:
        self.repo.write(
            "domain.py",
            "from typing import Any\n\n"
            "def check_owner(state: dict[str, Any]) -> bool:\n"
            "    return bool(state)\n",
        )
        signals = self._signals_for_current_diff()
        dict_sigs = [s for s in signals if s.kind == SignalKind.DICT_DOMAIN]
        self.assertEqual(len(dict_sigs), 1)
        self.assertIn("check_owner", dict_sigs[0].detail)

    def test_non_python_files_produce_no_signals(self) -> None:
        self.repo.write("README.md", "# Docs\n" + "line\n" * 900)
        self.repo.write("config.json", "{\n" + '  "k": "v",\n' * 500 + '  "z": 1\n}\n')
        signals = self._signals_for_current_diff()
        self.assertEqual(signals, ())

    def test_pre_refactor_branch_fixture_reports_expected_signals(
        self,
    ) -> None:
        shutil.copyfile(
            FIXTURES_DIR / "legacy_branch_sample.py",
            self.root / "legacy_branch_sample.py",
        )
        (self.root / "tests").mkdir(exist_ok=True)
        shutil.copyfile(
            FIXTURES_DIR / "test_legacy_branch_sample.py",
            self.root / "tests" / "test_legacy_branch_sample.py",
        )
        signals = self._signals_for_current_diff()
        kinds = {s.kind for s in signals}
        self.assertIn(SignalKind.DUPLICATE_LOGIC, kinds)
        self.assertIn(SignalKind.TEST_NAME, kinds)
        self.assertIn(SignalKind.LONG_FUNCTION, kinds)
        self.assertIn(SignalKind.DICT_DOMAIN, kinds)

        self.repo.cli("review", "prepare", "--tier", "quick")
        rejected = self.repo.cli(
            "review",
            "cover",
            "--lens",
            "craft",
            "--hunks",
            "H1",
            "--note",
            "H1 looks good, well-factored and readable with clear structure",
            check=False,
        )
        self.assertEqual(rejected.returncode, 1)
        self.assertIn("Example:", rejected.stderr)


if __name__ == "__main__":
    unittest.main()
