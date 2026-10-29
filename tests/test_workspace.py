"""Tests for the work tree a review or run command works in."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from craftsman.records import Ledger
from craftsman.workspace import open_workspace

from tests.support import EvidenceRepo, make_hunk, make_snap, open_gate, prepared


class WorkspaceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = EvidenceRepo(Path(tmp.name) / "repo")
        self.head = self.repo.base_sha
        self.repo.write_lines("app.py", 3)
        self.next = self.repo.commit("next")


class BaseShaTests(WorkspaceTestCase):
    def test_without_round_or_task_the_base_is_head(self) -> None:
        workspace = open_workspace(str(self.repo.root))

        self.assertEqual(workspace.base_sha(Ledger()), self.next)

    def test_active_task_sets_the_base(self) -> None:
        open_gate(self.repo.root, self.head, task="fix")

        self.assertEqual(open_workspace(str(self.repo.root)).base_sha(Ledger()), self.head)

    def test_latest_round_wins_over_the_task(self) -> None:
        open_gate(self.repo.root, self.next, task="fix")
        ledger, _ = prepared(make_snap(make_hunk("H1", "app.py"), base=self.head))

        self.assertEqual(open_workspace(str(self.repo.root)).base_sha(ledger), self.head)

    def test_explicit_base_wins_over_everything(self) -> None:
        open_gate(self.repo.root, self.next, task="fix")

        base = open_workspace(str(self.repo.root)).base_sha(Ledger(), "HEAD~1")

        self.assertEqual(base, self.head)


class StateTests(WorkspaceTestCase):
    def test_closed_gate_is_no_task(self) -> None:
        open_gate(self.repo.root, self.head, task="fix", active=False)

        workspace = open_workspace(str(self.repo.root))

        self.assertEqual((workspace.state, workspace.task), (None, ""))

    def test_snapshot_measures_from_the_task_base(self) -> None:
        open_gate(self.repo.root, self.head, task="fix")

        snap = open_workspace(str(self.repo.root)).snapshot()

        self.assertEqual([hunk.path for hunk in snap.hunks], ["app.py"])


if __name__ == "__main__":
    unittest.main()
