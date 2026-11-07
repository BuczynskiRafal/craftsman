"""Tests for `craftsman review scout`: base version, callers and tests for a hunk, place or symbol."""

from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from craftsman import scout as scout_module
from craftsman.diffscan import WorktreeIndex, resolve_base_sha, snapshot
from craftsman.records import FieldError
from craftsman.scout import Scout, parse_target

from tests.support import EvidenceRepo, GitRepo

PRICING = '''"""Pricing."""


def unit_price(sku):
    return PRICES[sku]


def compute_total(items):
    total = 0
    for item in items:
        total += unit_price(item)
    return total


def unrelated():
    a = 1
    b = 2
    c = 3
    d = 4
    e = 5
    f = 6
    return a + b + c + d + e + f
'''
PRICING_NEW = PRICING.replace("    return total\n", "    return round(total, 2)\n") + "\n\ndef brand_new(x):\n    return x\n"
PRICING_ADD = PRICING.replace("def unrelated", "def brand_new(x):\n    return x\n\n\ndef unrelated")
CALL_LINE = len(PRICING.splitlines()) + 2


class ParseTargetTests(unittest.TestCase):
    def test_forms_are_told_apart_by_pattern(self) -> None:
        for raw, kind in (
            ("H3", "hunks"),
            ("H3-H5", "hunks"),
            ("H3,H7", "hunks"),
            ("H3, H5-H8", "hunks"),
            ("pkg/a.py:42", "location"),
            ("compute_total", "symbol"),
            ("Quoter.quote", "symbol"),
            ("Hunk", "symbol"),
            ("H3x", "symbol"),
            ("ns::Type", "symbol"),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(parse_target(raw).kind, kind)

    def test_a_location_carries_its_path_and_line(self) -> None:
        target = parse_target("src/x:y/a.py:42")

        self.assertEqual((target.raw, target.line), ("src/x:y/a.py", 42))

    def test_an_empty_target_is_rejected(self) -> None:
        with self.assertRaisesRegex(FieldError, "must name a hunk"):
            parse_target("  ")


class ScoutTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = GitRepo(Path(tmp.name).resolve())

    def commit_then_edit(self, before: dict[str, str], after: dict[str, str]) -> str:
        for path, text in before.items():
            self.repo.write(path, text)
        base = self.repo.commit("base")
        for path, text in after.items():
            self.repo.write(path, text)
        return base

    def scout(self, base: str) -> Scout:
        return Scout(WorktreeIndex(self.repo.root), resolve_base_sha(self.repo.root, base))

    def hunk_text(self, base: str, *ids: str) -> str:
        snap = snapshot(self.repo.root, base)
        wanted = set(ids)
        return self.scout(base).hunks([hunk for hunk in snap.hunks if hunk.id in wanted])


class HunkScoutTests(ScoutTestCase):
    def test_the_base_version_is_the_scope_before_the_change_with_its_old_line_numbers(self) -> None:
        base = self.commit_then_edit({"pricing.py": PRICING}, {"pricing.py": PRICING_NEW})

        text = self.hunk_text(base, "H1")

        self.assertIn("## H1 · pricing.py · compute_total (function)", text)
        self.assertIn("### Base version at ", text)
        self.assertIn("`pricing.py:L8–L12`", text)
        self.assertIn("  8 def compute_total(items):", text)
        self.assertIn(" 12     return total\n", text)
        self.assertNotIn("round(total, 2)", text.split("### Callers")[0])

    def test_a_scope_the_change_adds_is_new_in_this_change(self) -> None:
        base = self.commit_then_edit({"pricing.py": PRICING}, {"pricing.py": PRICING_ADD})

        text = self.hunk_text(base, "H1")

        self.assertIn("brand_new", text)
        self.assertIn("new in this change (no `brand_new` in the base)", text)

    def test_a_new_file_is_new_in_this_change(self) -> None:
        base = self.commit_then_edit({"keep.py": "x = 1\n"}, {"fresh.py": "def fresh_fn():\n    return 1\n"})

        text = self.hunk_text(base, "H1")

        self.assertIn("new in this change (the file does not exist in the base)", text)

    def test_callers_come_with_five_lines_around_each_and_the_call_marked(self) -> None:
        user = "\n".join(f"line_{n} = {n}" for n in range(1, 21)) + "\nresult = compute_total([1, 2])\n" + "tail = 1\n"
        base = self.commit_then_edit({"pricing.py": PRICING, "app.py": user}, {"pricing.py": PRICING_NEW})

        text = self.hunk_text(base, "H1")

        callers = text.split("### Callers of `compute_total`")[1].split("###")[0]
        self.assertIn("`app.py:21`", callers)
        self.assertIn(">21 result = compute_total([1, 2])", callers)
        self.assertIn(" 16 line_16 = 16", callers)
        self.assertIn(" 22 tail = 1", callers)
        self.assertNotIn("line_15 = 15", callers)

    def test_callers_rank_the_same_file_then_the_same_folder_then_the_rest_and_stop_at_three(self) -> None:
        files = {
            "pkg/core.py": PRICING + "\nresult = compute_total([0])\n",
            "pkg/sibling.py": "compute_total([1])\n",
            "pkg/core2.py": "compute_total([3])\n",
            "far/away.py": "compute_total([2])\n",
            "a_first.py": "compute_total([4])\n",
        }
        edited = files["pkg/core.py"].replace("    return total\n", "    return round(total, 2)\n")
        base = self.commit_then_edit(files, {"pkg/core.py": edited})

        text = self.hunk_text(base, "H1")

        callers = text.split("### Callers of `compute_total`")[1].split("###")[0]
        found = re.findall(r"^`([^`]+:\d+)`$", callers, re.MULTILINE)
        self.assertEqual(found, [f"pkg/core.py:{CALL_LINE}", "pkg/core2.py:1", "pkg/sibling.py:1"])
        self.assertIn("(+2 more callers)", callers)

    def test_imports_prose_and_the_changed_lines_are_not_callers(self) -> None:
        files = {
            "pricing.py": PRICING,
            "app.py": "from pricing import compute_total\n",
            "README.md": "compute_total adds prices.\n",
        }
        base = self.commit_then_edit(files, {"pricing.py": PRICING_NEW})

        text = self.hunk_text(base, "H1")

        self.assertIn("(no callers found outside tests)", text)
        self.assertNotIn("app.py", text)
        self.assertNotIn("README.md", text)

    def test_tests_are_listed_up_to_three_with_bodies_trimmed_to_forty_lines(self) -> None:
        body = "\n".join(f"        step_{n} = {n}" for n in range(60))
        tests = "".join(
            f"def test_case_{index}():\n    compute_total([{index}])\n{body}\n\n\n" for index in range(5)
        )
        base = self.commit_then_edit(
            {"pricing.py": PRICING, "tests/test_pricing.py": tests}, {"pricing.py": PRICING_NEW}
        )

        text = self.hunk_text(base, "H1")

        tests_part = text.split("### Tests")[1]
        self.assertEqual(tests_part.count("`tests/test_pricing.py:L"), 3)
        self.assertIn("(+2 more tests)", tests_part)
        first = tests_part.split("```python")[1].split("```")[0]
        self.assertLessEqual(len(first.splitlines()), 41)
        self.assertIn("test_case_0()", first)
        self.assertIn("compute_total([0])", first)
        self.assertIn("omitted", first)

    def test_a_hunk_without_tests_says_so(self) -> None:
        base = self.commit_then_edit({"pricing.py": PRICING}, {"pricing.py": PRICING_NEW})

        self.assertIn("(no test function references them)", self.hunk_text(base, "H1"))

    def test_several_hunks_are_scouted_together_without_repeating_a_caller(self) -> None:
        user = "result = compute_total([1])\nother = brand_new(2)\n"
        base = self.commit_then_edit({"pricing.py": PRICING, "app.py": user}, {"pricing.py": PRICING_NEW})

        text = self.hunk_text(base, "H1", "H2")

        self.assertTrue(text.startswith("# Scout H1, H2 · base "))
        self.assertEqual(text.count("`app.py:1`"), 1)
        self.assertEqual(text.count("`app.py:2`"), 1)

    def test_the_output_is_the_same_every_time(self) -> None:
        base = self.commit_then_edit({"pricing.py": PRICING, "app.py": "compute_total([1])\n"}, {"pricing.py": PRICING_NEW})

        self.assertEqual(self.hunk_text(base, "H1"), self.hunk_text(base, "H1"))

    def test_tool_caches_and_generated_directories_are_not_searched(self) -> None:
        base = self.commit_then_edit(
            {"pricing.py": PRICING},
            {"pricing.py": PRICING_NEW, ".mypy_cache/x.py": "compute_total([9])\n", "__pycache__/y.py": "compute_total([8])\n"},
        )

        text = self.hunk_text(base, "H1")

        self.assertNotIn(".mypy_cache", text)
        self.assertNotIn("__pycache__", text)

    def test_output_over_the_cap_is_cut_and_counts_what_is_left(self) -> None:
        users = {f"u{index:02}.py": "".join(f"value_{n} = compute_total([{n}])\n" for n in range(4)) for index in range(12)}
        base = self.commit_then_edit({"pricing.py": PRICING, **users}, {"pricing.py": PRICING_NEW})

        with mock.patch.object(scout_module, "MAX_SCOUT_CHARS", 1500), mock.patch.object(
            scout_module, "MAX_CALLERS_PER_SYMBOL", 30
        ):
            text = self.hunk_text(base, "H1")

        self.assertLessEqual(len(text), 1500)
        self.assertRegex(text.splitlines()[-1], r"\A\(trimmed: \d+ more; narrow the target\)\Z")

    def test_the_real_cap_is_twelve_thousand_characters(self) -> None:
        self.assertEqual(scout_module.MAX_SCOUT_CHARS, 12_000)
        self.assertEqual(scout_module.MAX_CALLERS_PER_SYMBOL, 3)
        self.assertEqual(scout_module.MAX_TESTS, 3)
        self.assertEqual(scout_module.TEST_BODY_LINES, 40)
        self.assertEqual(scout_module.DEFINITION_LINES, 60)
        self.assertEqual(scout_module.MAX_SYMBOL_REFERENCES, 6)


class LocationAndSymbolScoutTests(ScoutTestCase):
    def test_a_location_scouts_the_scope_around_the_line(self) -> None:
        base = self.commit_then_edit({"pricing.py": PRICING, "app.py": "compute_total([1])\n"}, {"pricing.py": PRICING_NEW})

        text = self.scout(base).location("pricing.py", 10)

        self.assertIn("# Scout pricing.py:10 · base ", text)
        self.assertIn("## pricing.py:10 · compute_total (function, L8–L12)", text)
        self.assertIn("  8 def compute_total(items):", text)
        self.assertIn("`app.py:1`", text)

    def test_a_location_outside_any_file_is_an_error(self) -> None:
        base = self.commit_then_edit({"pricing.py": PRICING}, {})

        with self.assertRaisesRegex(Exception, "cannot read nope.py:3"):
            self.scout(base).location("nope.py", 3)

    def test_a_symbol_gets_its_definition_references_and_tests(self) -> None:
        users = {f"u{index}.py": f"result_{index} = unit_price('a')\n" for index in range(8)}
        tests = "def test_price():\n    assert unit_price('a') == 1\n"
        base = self.commit_then_edit({"pricing.py": PRICING, "tests/test_p.py": tests, **users}, {})

        text = self.scout(base).symbol("unit_price")

        self.assertTrue(text.startswith("# Scout `unit_price` · base "))
        definition = text.split("## References")[0]
        self.assertIn("`pricing.py:L4–L5` unit_price", definition)
        self.assertIn(" 4 def unit_price(sku):", definition)
        references = text.split("## References to `unit_price`")[1].split("## Tests")[0]
        self.assertEqual(references.count("```python"), 6)
        self.assertIn("(+3 more: grep -rnw unit_price)", references)
        self.assertIn("`pricing.py:11`", references)
        self.assertIn("`tests/test_p.py:L1–L2` test_price", text.split("## Tests referencing")[1])

    def test_a_symbol_that_exists_nowhere_is_reported(self) -> None:
        base = self.commit_then_edit({"pricing.py": PRICING}, {})

        text = self.scout(base).symbol("no_such_symbol")

        self.assertIn("No definition or reference of `no_such_symbol` found in the work tree.", text)

    def test_a_long_definition_is_trimmed_to_sixty_lines(self) -> None:
        body = "".join(f"    step_{n} = {n}\n" for n in range(150))
        base = self.commit_then_edit({"big.py": f"def huge_fn():\n{body}    return 0\n"}, {})

        definition = self.scout(base).symbol("huge_fn").split("## References")[0]

        shown = definition.split("```python")[1].split("```")[0].strip("\n").splitlines()
        self.assertLessEqual(len(shown), 60)
        self.assertEqual(shown[0], "   1 def huge_fn():")


class ScoutCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = EvidenceRepo(Path(tmp.name).resolve() / "repo")
        self.repo.write("pricing.py", PRICING)
        self.repo.write("app.py", "result = compute_total([1])\n")
        self.repo.commit("add pricing")
        self.repo.write("pricing.py", PRICING_NEW)

    def test_hunks_need_a_round(self) -> None:
        proc = self.repo.cli("review", "scout", "H1", check=False)

        self.assertEqual(proc.returncode, 1)
        self.assertIn("No review round yet", proc.stderr)

    def test_a_hunk_is_scouted_against_the_rounds_base(self) -> None:
        self.repo.cli("review", "prepare")

        out = self.repo.cli("review", "scout", "H1").stdout

        self.assertIn("## H1 · pricing.py · compute_total (function)", out)
        self.assertIn("  8 def compute_total(items):", out)
        self.assertIn("`app.py:1`", out)
        self.assertLessEqual(len(out), 12_000)

    def test_an_unknown_hunk_is_rejected(self) -> None:
        self.repo.cli("review", "prepare")

        proc = self.repo.cli("review", "scout", "H9", check=False)

        self.assertEqual(proc.returncode, 1)
        self.assertIn("unknown hunk 'H9'", proc.stderr)

    def test_a_stale_round_is_rejected(self) -> None:
        self.repo.cli("review", "prepare")
        self.repo.write("pricing.py", PRICING_NEW + "# edited\n")

        proc = self.repo.cli("review", "scout", "H1", check=False)

        self.assertEqual(proc.returncode, 1)

    def test_locations_and_symbols_work_without_a_round_and_take_a_base(self) -> None:
        location = self.repo.cli("review", "scout", "pricing.py:10").stdout
        symbol = self.repo.cli("review", "scout", "compute_total", "--base", self.repo.base_sha).stdout

        self.assertIn("## pricing.py:10 · compute_total", location)
        self.assertIn(f"base {self.repo.base_sha[:7]}", symbol)
        self.assertIn("## Definition of `compute_total`", symbol)


if __name__ == "__main__":
    unittest.main()
