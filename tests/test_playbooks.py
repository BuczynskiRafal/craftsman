"""Tests for `craftsman.playbooks` and `craftsman skill show code-review --lenses`."""

from __future__ import annotations

import re
import unittest
from typing import ClassVar

from craftsman.cli import skill_main
from craftsman.lenses import LENS_IDS, LENSES, REVIEWER_GROUPS, get_lens
from craftsman.playbooks import (
    expand_playbook_lenses,
    lens_playbooks,
    parse_playbook_sections,
)

from tests.support import REPO_ROOT, CliResult, run_main

LENSES_PATH = REPO_ROOT / "skills" / "code-review" / "lenses.md"


class PlaybookTests(unittest.TestCase):
    full_md: ClassVar[str]

    @classmethod
    def setUpClass(cls) -> None:
        cls.full_md = LENSES_PATH.read_text(encoding="utf-8")

    def skill_show(self, *args: str) -> CliResult:
        return run_main(skill_main, "show", *args, "--repo-dir", str(REPO_ROOT))

    def test_bundled_lenses_markdown_matches_lens_catalog(self) -> None:
        raw_level2 = [
            line for line in self.full_md.splitlines() if re.match(r"^##(?!#)", line)
        ]
        matched_ids = [
            match.group(1)
            for line in raw_level2
            if (match := re.match(r"^## `([a-z][a-z0-9_-]*)` — \S.*$", line)) is not None
        ]
        catalog_order = [lens.id for lens in LENSES]

        self.assertEqual(len(matched_ids), len(raw_level2))
        self.assertEqual(len(matched_ids), len(set(matched_ids)))
        self.assertEqual(set(matched_ids), LENS_IDS)
        self.assertEqual(matched_ids, catalog_order)

        preamble, sections = parse_playbook_sections(self.full_md)
        self.assertTrue(preamble.startswith("# Craftsman Review Lenses\n"))
        self.assertEqual(list(sections), catalog_order)
        self.assertEqual(lens_playbooks(self.full_md, catalog_order), self.full_md)

    def test_show_reviewer_group_emits_only_group_playbooks(self) -> None:
        preamble, sections = parse_playbook_sections(self.full_md)

        result = self.skill_show("code-review", "--lenses", "R3")

        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(result.err, "")
        self.assertEqual(
            result.out,
            f"{preamble}\n\n{sections['platform']}\n\n{sections['craft']}\n",
        )
        for other_id in LENS_IDS - set(REVIEWER_GROUPS["R3"]):
            self.assertNotIn(f"## `{other_id}` —", result.out)

    def test_show_orders_by_catalog_with_duplicates_removed(self) -> None:
        preamble, sections = parse_playbook_sections(self.full_md)

        reversed_pair = self.skill_show("code-review", "--lenses", "contracts,intent")
        self.assertEqual(reversed_pair.code, 0, reversed_pair.err)
        self.assertEqual(
            reversed_pair.out,
            f"{preamble}\n\n{sections['intent']}\n\n{sections['contracts']}\n",
        )

        with_dups = self.skill_show("code-review", "--lenses", "R1,intent")
        self.assertEqual(with_dups.code, 0, with_dups.err)
        expected_r1 = "\n\n".join(
            [preamble, *(sections[lens_id] for lens_id in ("intent", "contracts", "ripple", "tests"))]
        )
        self.assertEqual(with_dups.out, expected_r1 + "\n")
        self.assertEqual(
            expand_playbook_lenses(" R4 , R3 , craft "),
            ("platform", "craft", "design"),
        )

    def test_single_lens_output_is_small_fraction_of_full_file(self) -> None:
        self.assertGreater(len(self.full_md), 20_000)
        for lens in LENSES:
            with self.subTest(lens=lens.id):
                result = self.skill_show("code-review", "--lenses", lens.id)
                self.assertEqual(result.code, 0, result.err)
                self.assertLess(len(result.out), len(self.full_md) // 4)

    def test_subheadings_stay_inside_enclosing_lens_section(self) -> None:
        synthetic = (
            "# Header\n\nShared rules.\n\n"
            "## `intent` — Intent & completeness\n\n"
            "### Subheading inside intent\n"
            "#### Deeper subheading\n"
            "```python\n"
            "## comment inside code fence\n"
            "```\n\n"
            "## `craft` — Clarity, reuse & simplicity\n\n"
            "Craft body.\n"
        )

        rendered = lens_playbooks(synthetic, ["intent"])

        self.assertIn("### Subheading inside intent", rendered)
        self.assertIn("#### Deeper subheading", rendered)
        self.assertIn("## comment inside code fence", rendered)
        self.assertNotIn("## `craft`", rendered)
        self.assertTrue(rendered.endswith("\n"))

    def test_lens_playbooks_rejects_unknown_or_empty_lens_ids(self) -> None:
        with self.assertRaises(KeyError) as expected_err:
            get_lens("bogus")
        with self.assertRaises(KeyError) as actual_err:
            lens_playbooks(self.full_md, ["bogus"])
        self.assertEqual(actual_err.exception.args, expected_err.exception.args)

        with self.assertRaisesRegex(ValueError, "at least one lens"):
            lens_playbooks(self.full_md, [])

        with self.assertRaisesRegex(TypeError, "not str"):
            lens_playbooks(self.full_md, "intent")

        partial_md = "# Intro\n\n## `intent` — Intent & completeness\n\nOnly intent.\n"
        with self.assertRaisesRegex(ValueError, "missing playbook section.*contracts"):
            lens_playbooks(partial_md, ["contracts"])

    def test_parser_rejects_malformed_or_duplicate_headings(self) -> None:
        bad_inputs = (
            ("malformed heading", "# Intro\n\n## Notes\n\nText.\n"),
            ("missing backticks", "# Intro\n\n## intent — Intent\n\nText.\n"),
            ("ascii hyphen", "# Intro\n\n## `intent` - Intent\n\nText.\n"),
            (
                "duplicate lens",
                "## `intent` — First\n\nA.\n\n## `intent` — Second\n\nB.\n",
            ),
            ("no sections", "# Only preamble\n\nNo level-2 headings.\n"),
            ("unclosed fence", "## `intent` — First\n\n```python\nx = 1\n"),
        )
        for label, markdown in bad_inputs:
            with self.subTest(case=label), self.assertRaises(ValueError):
                parse_playbook_sections(markdown)

    def test_lenses_flag_on_other_skill_exits_with_usage_error(self) -> None:
        result = self.skill_show("simplify", "--lenses", "intent")

        self.assertEqual(result.code, 2)
        self.assertEqual(result.out, "")
        self.assertIn("craftsman skill: error:", result.err)
        self.assertIn("code-review", result.err)

    def test_lenses_flag_combined_with_file_flag_is_refused(self) -> None:
        result = self.skill_show("code-review", "--lenses", "intent", "--file", "lenses.md")

        self.assertEqual(result.code, 2)
        self.assertEqual(result.out, "")
        self.assertIn("craftsman skill: error:", result.err)
        self.assertIn("--file", result.err)

    def test_unknown_or_empty_lenses_flag_exits_with_usage_error(self) -> None:
        for bad_raw in ("bogus", "R9", "", ",", " , , "):
            with self.subTest(lenses=bad_raw):
                result = self.skill_show("code-review", "--lenses", bad_raw)
                self.assertEqual(result.code, 2)
                self.assertEqual(result.out, "")
                self.assertIn("craftsman skill: error:", result.err)
                self.assertIn("valid lenses:", result.err)
                self.assertIn("groups:", result.err)


if __name__ == "__main__":
    unittest.main()
