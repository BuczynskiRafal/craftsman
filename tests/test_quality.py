"""Tests for the quality signals computed from the diff and the Python syntax trees."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from craftsman.diffscan import snapshot
from craftsman.quality import (
    MAX_FUNCTION_LINES,
    MAX_MODULE_GROWTH,
    MAX_MODULE_LINES,
    MAX_PRODUCTION_ADDED_LINES,
    collect_quality_signals,
)
from craftsman.records import QualitySignal, SignalKind

from tests.support import EvidenceRepo

ROLE_CHECKS = (
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
    "    return current == target_role\n"
)


SHARED_STEPS = (
    "def first(a: int) -> int:\n    b = a + 1\n    c = b * 2\n    d = c - 3\n"
    "    e = d // 4\n    f = e % 5\n    return {last}\n"
)


def function_of(lines: int, name: str = "over_budget") -> str:
    """A function spanning exactly `lines` lines, its `def` line included."""
    body = "".join(f"    x_{i} = {i}\n" for i in range(lines - 2))
    return f"def {name}(a: int) -> int:\n{body}    return a\n"


def constants(count: int, prefix: str = "VALUE") -> str:
    return "".join(f"{prefix}_{index} = {index}\n" for index in range(count))


class QualitySignalTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = EvidenceRepo(Path(tmp.name))

    def signals(self, kind: SignalKind) -> list[QualitySignal]:
        snap = snapshot(self.repo.root, self.repo.git("rev-parse", "HEAD").strip())
        return [s for s in collect_quality_signals(self.repo.root, snap) if s.kind is kind]

    def test_function_one_line_over_budget_is_reported(self) -> None:
        self.repo.write("service.py", function_of(MAX_FUNCTION_LINES + 1))

        (signal,) = self.signals(SignalKind.LONG_FUNCTION)

        self.assertIn(f"`over_budget` spans {MAX_FUNCTION_LINES + 1} lines", signal.detail)

    def test_function_at_the_budget_is_not_reported(self) -> None:
        self.repo.write("service.py", function_of(MAX_FUNCTION_LINES))

        self.assertEqual(self.signals(SignalKind.LONG_FUNCTION), [])

    def test_unchanged_long_function_is_not_reported(self) -> None:
        self.repo.write("service.py", function_of(45) + "\n\ndef short() -> int:\n    return 1\n")
        self.repo.commit()
        self.repo.write("service.py", function_of(45) + "\n\ndef short() -> int:\n    return 2\n")

        self.assertEqual(self.signals(SignalKind.LONG_FUNCTION), [])

    def test_forbidden_test_names_are_reported(self) -> None:
        for name in (
            "test_v3_0_0_state_without_owner",
            "test_owner_continues_at_1_of_6",
            "test_evaluate_scope_table_of_45_cases",
            "test_f7_scope_override_works",
            "test_doctor_detects_duplicates_and_fixes_them",
            "test_gate_init_refuses_foreign_active_gate_until_takeover_is_passed_explicitly",
        ):
            with self.subTest(name=name):
                self.repo.write("tests/test_sample.py", f"def {name}() -> None:\n    assert True\n")
                (signal,) = self.signals(SignalKind.TEST_NAME)
                self.assertIn(name, signal.detail)

    def test_behavior_named_test_is_not_reported(self) -> None:
        name = "test_gate_without_owner_accepts_stop_from_any_session"
        self.repo.write("tests/test_sample.py", f"def {name}() -> None:\n    assert True\n")

        self.assertEqual(self.signals(SignalKind.TEST_NAME), [])

    def test_function_differing_only_in_names_is_duplicate_logic(self) -> None:
        self.repo.write("owners.py", ROLE_CHECKS)

        (signal,) = self.signals(SignalKind.DUPLICATE_LOGIC)

        self.assertIn("`can_session_proceed` duplicates `is_user_allowed`", signal.detail)

    def test_functions_returning_different_values_are_not_duplicates(self) -> None:
        self.repo.write(
            "pairs.py",
            "def both(a: int) -> tuple[int, int]:\n    b = a + 1\n    return b, a\n\n\n"
            "def first(a: int) -> int:\n    b = a + 1\n    return b\n",
        )

        self.assertEqual(self.signals(SignalKind.DUPLICATE_LOGIC), [])

    def test_functions_sharing_most_statements_are_duplicate_logic(self) -> None:
        self.repo.write(
            "steps.py",
            SHARED_STEPS.format(last="f")
            + "\n\n"
            + SHARED_STEPS.format(last="f + 1").replace("first", "second"),
        )

        (signal,) = self.signals(SignalKind.DUPLICATE_LOGIC)

        self.assertIn("`second` duplicates `first`", signal.detail)

    def test_functions_sharing_few_statements_are_not_duplicates(self) -> None:
        other = (
            "def second(a: int) -> int:\n    b = a + 1\n    c = b * 2\n    d = c + 9\n"
            "    e = d * 8\n    f = e - 7\n    return f\n"
        )
        self.repo.write("steps.py", SHARED_STEPS.format(last="f") + "\n\n" + other)

        self.assertEqual(self.signals(SignalKind.DUPLICATE_LOGIC), [])

    def test_symlink_to_code_outside_the_work_tree_is_not_checked(self) -> None:
        elsewhere = tempfile.TemporaryDirectory()
        self.addCleanup(elsewhere.cleanup)
        outside = Path(elsewhere.name) / "long.py"
        outside.write_text(function_of(MAX_FUNCTION_LINES + 1), encoding="utf-8")
        (self.repo.root / "long.py").symlink_to(outside)

        self.assertEqual(self.signals(SignalKind.LONG_FUNCTION), [])

    def test_public_function_taking_plain_dict_is_reported(self) -> None:
        self.repo.write(
            "domain.py", "def check_owner(state: dict[str, int]) -> bool:\n    return bool(state)\n"
        )

        (signal,) = self.signals(SignalKind.DICT_DOMAIN)

        self.assertIn("check_owner", signal.detail)

    def test_file_that_does_not_parse_is_reported(self) -> None:
        self.repo.write("broken.py", "def broken(:\n    pass\n")

        (signal,) = self.signals(SignalKind.PARSE_ERROR)

        self.assertEqual((signal.file, signal.line), ("broken.py", 1))

    def test_large_module_growing_past_the_budget_is_reported(self) -> None:
        self.repo.write("big.py", constants(MAX_MODULE_LINES - MAX_MODULE_GROWTH))
        self.repo.commit()
        self.repo.write("big.py", constants(MAX_MODULE_LINES + 1))

        (signal,) = self.signals(SignalKind.LARGE_MODULE)

        self.assertIn(f"has {MAX_MODULE_LINES + 1} lines", signal.detail)

    def test_large_module_growing_by_the_budget_is_not_reported(self) -> None:
        self.repo.write("big.py", constants(MAX_MODULE_LINES + 1 - MAX_MODULE_GROWTH))
        self.repo.commit()
        self.repo.write("big.py", constants(MAX_MODULE_LINES + 1))

        self.assertEqual(self.signals(SignalKind.LARGE_MODULE), [])

    def test_file_that_is_not_utf8_is_reported(self) -> None:
        self.repo.write("latin.py", "")
        (self.repo.root / "latin.py").write_bytes(b"name = '\xe9t\xe9'\n")

        (signal,) = self.signals(SignalKind.PARSE_ERROR)

        self.assertIn("is not UTF-8", signal.detail)

    def test_module_at_the_line_budget_is_not_reported(self) -> None:
        self.repo.write("big.py", constants(1))
        self.repo.commit()
        self.repo.write("big.py", constants(MAX_MODULE_LINES))

        self.assertEqual(self.signals(SignalKind.LARGE_MODULE), [])

    def test_production_lines_over_the_budget_are_a_diff_size_signal(self) -> None:
        self.repo.write("mod_a.py", constants(MAX_PRODUCTION_ADDED_LINES // 2, "A"))
        self.repo.write("mod_b.py", constants(MAX_PRODUCTION_ADDED_LINES // 2 + 1, "B"))

        (signal,) = self.signals(SignalKind.DIFF_SIZE)

        self.assertIn(f"+{MAX_PRODUCTION_ADDED_LINES + 1} production lines", signal.detail)

    def test_production_lines_at_the_budget_are_no_signal(self) -> None:
        self.repo.write("mod_a.py", constants(MAX_PRODUCTION_ADDED_LINES))

        self.assertEqual(self.signals(SignalKind.DIFF_SIZE), [])

    def test_non_production_lines_do_not_count_toward_the_diff_size(self) -> None:
        for path in ("web/cart.spec.ts", "pkg/cart_test.go", "spec/cart.rb", "docs/guide.md"):
            self.repo.write(path, constants(MAX_PRODUCTION_ADDED_LINES + 1))

        self.assertEqual(self.signals(SignalKind.DIFF_SIZE), [])

    def test_non_python_files_get_no_code_signals(self) -> None:
        self.repo.write("config.json", "{\n" + '  "k": "v",\n' * 500 + '  "z": 1\n}\n')

        snap = snapshot(self.repo.root, self.repo.base_sha)
        self.assertEqual(collect_quality_signals(self.repo.root, snap), ())


if __name__ == "__main__":
    unittest.main()
