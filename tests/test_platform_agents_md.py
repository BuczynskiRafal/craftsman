"""Tests for `AGENTS.md` and Cursor rule workspace export (`craftsman.platforms.agents_md`)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from craftsman.assets import iter_skills
from craftsman.platforms.agents_md import (
    export_to_agents_workspace,
    render_agents_md,
)

from tests.support import REPO_ROOT


def frontmatter(text: str) -> list[str]:
    assert text.startswith("---\n"), text[:40]
    return text.split("---\n")[1].splitlines()


class AgentsMdTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()

    def test_every_skill_is_listed(self) -> None:
        text = render_agents_md(REPO_ROOT)

        for skill in iter_skills(REPO_ROOT):
            self.assertIn(f"- **`/{skill.name}`**: ", text)

    def test_cursor_rule_applies_to_every_file(self) -> None:
        workspace = export_to_agents_workspace(REPO_ROOT, self.tmp / "ws")

        rule = workspace.cursor_rule.path.read_text(encoding="utf-8")
        self.assertIn('globs: "*"', frontmatter(rule))


if __name__ == "__main__":
    unittest.main()
