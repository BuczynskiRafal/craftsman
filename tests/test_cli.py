"""Tests for `craftsman status`."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from craftsman.gate import GateOwner

from tests.support import EvidenceRepo, assert_cli_error, open_gate


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


if __name__ == "__main__":
    unittest.main()
