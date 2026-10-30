"""Tests for Craftsman generated-file markers and managed Markdown block upserts."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from craftsman.generated import (
    CRAFTSMAN_BEGIN_MARKER,
    ExportError,
    is_craftsman_generated_file,
    strip_managed_block,
    upsert_managed_markdown,
)


class GeneratedFileTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()


class ManagedMarkdownTests(GeneratedFileTestCase):
    def test_file_without_markers_gets_the_block_appended(self) -> None:
        path = self.tmp / "AGENTS.md"
        heading = "# AGENTS.md — Craftsman Adaptive Closed-Loop Engineering Standard\n"
        path.write_text(heading, encoding="utf-8")

        upsert_managed_markdown(path, "rules", within=self.tmp)

        self.assertEqual(
            path.read_text(encoding="utf-8"),
            f"{heading}\n{CRAFTSMAN_BEGIN_MARKER}\nrules\n<!-- craftsman:end -->\n",
        )

    def test_second_upsert_replaces_the_block_in_place(self) -> None:
        path = self.tmp / "AGENTS.md"
        path.write_text("# Mine\n", encoding="utf-8")
        upsert_managed_markdown(path, "old", within=self.tmp)
        path.write_text(path.read_text(encoding="utf-8") + "\n# After\n", encoding="utf-8")

        upsert_managed_markdown(path, "new", within=self.tmp)

        text = path.read_text(encoding="utf-8")
        self.assertEqual(text.count(CRAFTSMAN_BEGIN_MARKER), 1)
        self.assertEqual(strip_managed_block(text, Path("AGENTS.md")), "# Mine\n\n# After\n")

    def test_dry_run_upsert_writes_nothing(self) -> None:
        path = self.tmp / "AGENTS.md"
        path.write_text("# Mine\n", encoding="utf-8")

        change = upsert_managed_markdown(path, "rules", within=self.tmp, dry_run=True)

        self.assertTrue(change.changed)
        self.assertEqual(path.read_text(encoding="utf-8"), "# Mine\n")
        self.assertFalse((self.tmp / "AGENTS.md.bak").exists())

    def test_block_without_its_end_marker_is_refused(self) -> None:
        path = self.tmp / "AGENTS.md"
        text = f"intro\n{CRAFTSMAN_BEGIN_MARKER}\nold\n## My team rules\nkeep me\n"
        path.write_text(text, encoding="utf-8")

        with self.assertRaisesRegex(ExportError, "do not enclose one block"):
            upsert_managed_markdown(path, "rules", within=self.tmp)
        self.assertEqual(path.read_text(encoding="utf-8"), text)

    def test_symlinked_file_inside_the_root_is_updated_where_it_lives(self) -> None:
        (self.tmp / "CLAUDE.md").write_text("# Mine\n", encoding="utf-8")
        (self.tmp / "AGENTS.md").symlink_to(self.tmp / "CLAUDE.md")

        upsert_managed_markdown(self.tmp / "AGENTS.md", "rules", within=self.tmp)

        self.assertTrue((self.tmp / "AGENTS.md").is_symlink())
        self.assertIn(CRAFTSMAN_BEGIN_MARKER, (self.tmp / "CLAUDE.md").read_text(encoding="utf-8"))

    def test_symlink_out_of_the_root_is_refused(self) -> None:
        outside = self.tmp / "outside.md"
        outside.write_text("secret\n", encoding="utf-8")
        workspace = self.tmp / "ws"
        workspace.mkdir()
        (workspace / "AGENTS.md").symlink_to(outside)

        with self.assertRaisesRegex(ExportError, "resolves outside"):
            upsert_managed_markdown(workspace / "AGENTS.md", "rules", within=workspace)
        self.assertEqual(outside.read_text(encoding="utf-8"), "secret\n")

    def test_unreadable_file_is_refused(self) -> None:
        path = self.tmp / "AGENTS.md"
        path.write_bytes(b"\xff\xfe")

        with self.assertRaisesRegex(ExportError, "cannot read it as UTF-8"):
            upsert_managed_markdown(path, "rules", within=self.tmp)

    def test_file_without_the_marker_is_not_generated(self) -> None:
        path = self.tmp / "verify.prompt.md"
        path.write_text("---\nagent: 'agent'\n---\n\n> **GitHub Copilot:** note\n", encoding="utf-8")

        self.assertFalse(is_craftsman_generated_file(path))


if __name__ == "__main__":
    unittest.main()
