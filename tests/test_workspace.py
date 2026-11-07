"""Tests for the work tree a review or run command works in."""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from craftsman.gate import GateOwner
from craftsman.records import Ledger, format_timestamp
from craftsman.workspace import (
    HOOKS_MARKER,
    ReviewMarker,
    brief_platform,
    caller_identity,
    open_workspace,
    read_marker,
    review_enforcement,
    write_marker,
)

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


class IdentityTests(unittest.TestCase):
    def test_an_antigravity_conversation_proves_who_it_is(self) -> None:
        self.assertEqual(caller_identity({"ANTIGRAVITY_CONVERSATION_ID": " 8bf67bba "}), "antigravity:8bf67bba")

    def test_nothing_else_proves_anything(self) -> None:
        self.assertIsNone(caller_identity({}))
        self.assertIsNone(caller_identity({"ANTIGRAVITY_CONVERSATION_ID": "  "}))
        # Claude Code subagents share their parent's session id, so it is no identity.
        self.assertIsNone(caller_identity({"CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "s1"}))

    def test_the_platform_a_brief_is_read_on(self) -> None:
        cases = [
            ({"CLAUDECODE": "1"}, "claude"),
            ({"CLAUDE_CODE_ENTRYPOINT": "cli"}, "claude"),
            ({"ANTIGRAVITY_AGENT": "1"}, "antigravity"),
            ({"AGY_MANAGED_ENV": "1"}, "antigravity"),
            ({"ANTIGRAVITY_CONVERSATION_ID": "c1"}, "antigravity"),
            ({"COPILOT_CLI": "1"}, "copilot"),
            ({"GITHUB_COPILOT_CLI": "1"}, "copilot"),
            ({"VSCODE_PID": "1", "TERM_PROGRAM": "vscode"}, "cli"),
            ({}, "cli"),
            ({"CLAUDECODE": "1", "ANTIGRAVITY_AGENT": "1"}, "claude"),
        ]
        for env, expected in cases:
            with self.subTest(env=env):
                self.assertEqual(brief_platform(env), expected)


class MarkerFileTests(WorkspaceTestCase):
    @property
    def ledger_path(self) -> Path:
        return self.repo.ledger_dir / "ledger.json"

    def write_raw(self, **fields: object) -> None:
        self.repo.ledger_dir.mkdir(parents=True, exist_ok=True)
        base = {"platform": "claude", "session_id": "s1", "at": "2026-01-01T00:00:00.000Z"}
        (self.repo.ledger_dir / HOOKS_MARKER).write_text(json.dumps({**base, **fields}), encoding="utf-8")

    def test_a_marker_from_before_the_new_fields_still_reads(self) -> None:
        self.write_raw(guarding=True, forced=2, best=3, touched_at="2026-01-01T00:00:05.000Z")

        marker = read_marker(self.ledger_path)

        self.assertEqual(
            marker,
            ReviewMarker("claude", "s1", "2026-01-01T00:00:00.000Z", True, 2, 3, "2026-01-01T00:00:05.000Z"),
        )
        assert marker is not None
        self.assertEqual((marker.deep, marker.task, marker.evidence_seen, marker.abandoned_at), (False, "", None, None))

    def test_the_new_fields_round_trip(self) -> None:
        marker = ReviewMarker(
            "antigravity", "c1", "2026-01-01T00:00:00.000Z", deep=True, task="Add totals", evidence_seen=4,
            abandoned_at="2026-01-01T01:00:00.000Z",
        )  # fmt: skip

        write_marker(self.ledger_path, marker)

        self.assertEqual(read_marker(self.ledger_path), marker)

    def test_fields_of_the_wrong_type_read_as_their_defaults(self) -> None:
        self.write_raw(guarding=True, deep="yes", task=7, evidence_seen="many", abandoned_at="")

        marker = read_marker(self.ledger_path)

        assert marker is not None
        self.assertEqual((marker.deep, marker.task, marker.evidence_seen, marker.abandoned_at), (False, "", None, None))


class EnforcementTests(WorkspaceTestCase):
    NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

    def enforcement(self, ledger: Ledger | None = None, env: dict[str, str] | None = None) -> tuple[bool, str]:
        return review_enforcement(open_workspace(str(self.repo.root)), ledger or Ledger(), env or {}, self.NOW)

    def arm(self, *, seconds_ago: float | None = 5, platform: str = "claude", guarding: bool = True) -> None:
        touched = None if seconds_ago is None else format_timestamp(self.NOW - timedelta(seconds=seconds_ago))
        marker = ReviewMarker(platform, "s1", format_timestamp(self.NOW - timedelta(hours=1)), guarding, touched_at=touched)
        write_marker(self.repo.ledger_dir / "ledger.json", marker)

    def test_nothing_hooked_is_advisory(self) -> None:
        self.assertEqual(self.enforcement(), (False, "cli"))

    def test_a_marker_the_hooks_just_touched_enforces_for_its_platform(self) -> None:
        for platform in ("claude", "copilot", "antigravity"):
            with self.subTest(platform=platform):
                self.arm(platform=platform)
                self.assertEqual(self.enforcement(), (True, platform))

    def test_a_stale_unguarding_or_unknown_marker_does_not(self) -> None:
        cases = {
            "stale": {"seconds_ago": 121},
            "from the future": {"seconds_ago": -30},
            "never touched": {"seconds_ago": None},
            "not guarding": {"guarding": False},
            "unknown platform": {"platform": "cli"},
        }
        for name, options in cases.items():
            with self.subTest(name):
                self.arm(**options)  # type: ignore[arg-type]
                self.assertEqual(self.enforcement(), (False, "cli"))

    def test_the_boundary_of_the_freshness_window_counts(self) -> None:
        self.arm(seconds_ago=120)

        self.assertEqual(self.enforcement(), (True, "claude"))

    def test_an_unreadable_touch_time_proves_nothing(self) -> None:
        self.arm()
        raw = json.loads((self.repo.ledger_dir / HOOKS_MARKER).read_text(encoding="utf-8"))
        raw["touched_at"] = "yesterday"
        (self.repo.ledger_dir / HOOKS_MARKER).write_text(json.dumps(raw), encoding="utf-8")

        self.assertEqual(self.enforcement(), (False, "cli"))

    def test_a_gate_that_proved_isolation_enforces_for_its_owner(self) -> None:
        open_gate(self.repo.root, self.head, owner=GateOwner("claude", "sess-1"), isolation_evidence="hooks")
        env = {"CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "sess-1"}

        self.assertEqual(self.enforcement(env=env), (True, "claude"))

    def test_a_gate_without_isolation_proof_does_not(self) -> None:
        open_gate(self.repo.root, self.head, owner=GateOwner("claude", "sess-1"))
        env = {"CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "sess-1"}

        self.assertEqual(self.enforcement(env=env), (False, "cli"))

    def test_the_gate_of_another_session_does_not_enforce_for_this_one(self) -> None:
        open_gate(self.repo.root, self.head, owner=GateOwner("claude", "sess-1"), isolation_evidence="hooks")
        env = {"CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "sess-2"}

        self.assertEqual(self.enforcement(env=env), (False, "cli"))

    def test_an_antigravity_gate_always_has_transcript_evidence(self) -> None:
        open_gate(self.repo.root, self.head, owner=GateOwner("antigravity", None))

        self.assertEqual(self.enforcement(env={"ANTIGRAVITY_AGENT": "1"}), (True, "antigravity"))

    def test_an_enforced_round_stays_enforced_on_its_platform(self) -> None:
        ledger, round_ = prepared(make_snap(make_hunk("H1", "app.py")))
        ledger = replace(ledger, rounds=(replace(round_, enforced=True, platform="copilot"),))

        self.assertEqual(self.enforcement(ledger), (True, "copilot"))

    def test_an_advisory_round_stays_advisory(self) -> None:
        ledger, _ = prepared(make_snap(make_hunk("H1", "app.py")))

        self.assertEqual(self.enforcement(ledger), (False, "cli"))


if __name__ == "__main__":
    unittest.main()
