"""Tests for the ledger records: validation and JSON round-trips."""

from __future__ import annotations

import json
import unittest
from dataclasses import replace
from typing import Any

from craftsman.ledger import ack_signal, add_agent_event, resolve, set_verdict
from craftsman.records import (
    AgentEvent,
    Ledger,
    LedgerError,
    QualitySignal,
    SignalKind,
)

from tests.support import (
    cover_open_hunks,
    make_hunk,
    make_snap,
    prepared,
    record_finding,
    record_run,
)

EVIDENCE = "Reproduced via tests/test_app.py::test_empty (fails before fix)."
LONG_FUNCTION = QualitySignal("S1", SignalKind.LONG_FUNCTION, "a.py", 1, "`f` spans 50 lines")


def full_ledger() -> Ledger:
    snap = make_snap(make_hunk("H1", "a.py"), make_hunk("H2", "b.py"))
    ledger, _ = prepared(snap, signals=(LONG_FUNCTION,))
    ledger = cover_open_hunks(ledger, "intent")
    ledger = record_run(record_finding(ledger, signal_id="S1"), kind="repro", exit_code=1)
    ledger, _ = set_verdict(ledger, "F1", "proven", EVIDENCE, "R1")
    ledger, _ = resolve(ledger, "F1", "fixed", "Guarded the empty list in total().", fingerprint="fp2")
    ledger, _ = ack_signal(ledger, "S1", "`f` in a.py:1 is a flat table of 50 literal cases")
    event = AgentEvent(
        platform="claude",
        event="stop",
        agent="craftsman-reviewer",
        agent_id="a",
        at="2026-01-01T00:05:00.000Z",
    )
    return add_agent_event(ledger, event)


def ledger_json() -> dict[str, Any]:
    raw: dict[str, Any] = json.loads(json.dumps(full_ledger().to_json()))
    return raw


def assert_rejected(case: unittest.TestCase, raw: Any, message: str) -> None:
    with case.assertRaises(LedgerError) as caught:
        Ledger.from_json(raw)
    case.assertIn(message, str(caught.exception))


class SerializationTests(unittest.TestCase):
    def test_round_trip_preserves_every_record(self) -> None:
        ledger = full_ledger()

        self.assertEqual(Ledger.from_json(json.loads(json.dumps(ledger.to_json()))), ledger)

    def test_signal_kind_is_stored_as_its_name(self) -> None:
        self.assertEqual(ledger_json()["rounds"][0]["signals"][0]["kind"], "long-function")

    def test_invalid_data_names_the_offending_field(self) -> None:
        def broken(change: dict[str, object], section: str = "findings") -> dict[str, Any]:
            raw = ledger_json()
            raw[section][0].update(change)
            return raw

        cases = [
            (broken({"severity": "critical"}), "findings[0].severity must be one of blocker, major"),
            (broken({"line": True}), "findings[0].line must be an integer"),
            (broken({"surprise": 1}), "findings[0].surprise is not a known field"),
            (broken({"verdict_run": "R9"}), "findings[0].verdict_run references an unknown run"),
            (broken({"digest": 5}, "rounds"), "rounds[0].digest is not a known field"),
            ({**ledger_json(), "schema": 2}, "schema 2 is not supported"),
            ([], "ledger.json: must be a JSON object"),
        ]
        for raw, message in cases:
            with self.subTest(message=message):
                assert_rejected(self, raw, message)

    def test_nested_record_errors_include_their_path(self) -> None:
        raw = ledger_json()
        raw["rounds"][0]["hunks"][1]["new_len"] = "two"

        assert_rejected(self, raw, "rounds[0].hunks[1].new_len must be an integer")

    def test_every_record_field_is_required(self) -> None:
        cases = [
            ("signal_acks", None, "signal_acks is missing"),
            ("facts", None, "facts is missing"),
            ("rounds", "signals", "rounds[0].signals is missing"),
            ("findings", "signal_id", "findings[0].signal_id is missing"),
        ]
        for section, field, message in cases:
            raw = ledger_json()
            if field is None:
                del raw[section]
            else:
                del raw[section][0][field]
            with self.subTest(message=message):
                assert_rejected(self, raw, message)

    def test_unknown_signal_kind_is_rejected(self) -> None:
        raw = ledger_json()
        raw["rounds"][0]["signals"][0]["kind"] = "vibes"

        assert_rejected(self, raw, "rounds[0].signals[0].kind must be one of long-function")


class LedgerConsistencyTests(unittest.TestCase):
    def test_finding_must_name_a_signal_of_its_round(self) -> None:
        raw = ledger_json()
        raw["findings"][0]["signal_id"] = "S7"

        assert_rejected(self, raw, "findings[0].signal_id names an unknown quality signal 'S7'")

    def test_signal_ack_must_name_a_signal_of_its_round(self) -> None:
        raw = ledger_json()
        raw["signal_acks"][0]["signal_id"] = "S7"

        assert_rejected(self, raw, "signal_acks[0].signal_id names an unknown quality signal 'S7'")

    def test_signal_ack_must_match_the_fingerprint_of_its_round(self) -> None:
        raw = ledger_json()
        raw["signal_acks"][0]["fingerprint"] = "other"

        assert_rejected(self, raw, "signal_acks[0].fingerprint must match round 1")

    def test_coverage_must_cover_hunks_of_its_round(self) -> None:
        raw = ledger_json()
        raw["coverage"] = [
            {
                "round": 1,
                "lens": "intent",
                "digests": ["not-a-digest"],
                "note": "H1 f_1.py:1: x=1 -> returns 2 on boundary input",
                "recorded_at": "2026-01-01T00:00:00.000Z",
            }
        ]

        assert_rejected(self, raw, "coverage[0].digests are not in round 1")


class RunRecordTests(unittest.TestCase):
    def setUp(self) -> None:
        self.recorded = record_run(Ledger()).runs[0]

    def test_run_must_name_a_command(self) -> None:
        with self.assertRaisesRegex(LedgerError, "argv must name a command"):
            replace(self.recorded, argv=())

    def test_run_keeps_only_the_tail_of_its_output(self) -> None:
        with self.assertRaisesRegex(LedgerError, "output_tail must keep only the last 4000"):
            replace(self.recorded, output_tail="x" * 4001)

    def test_run_that_left_the_tree_unchanged_has_no_after_fingerprint(self) -> None:
        with self.assertRaisesRegex(LedgerError, "fingerprint_after must differ"):
            replace(self.recorded, fingerprint_after=self.recorded.fingerprint)

    def test_short_command_truncates_long_commands_only(self) -> None:
        long_run = replace(self.recorded, argv=("python3", "-c", "x" * 100))

        self.assertEqual(self.recorded.short_command, "pytest -q")
        self.assertEqual(long_run.short_command, "python3 -c " + "x" * 48 + "…")


if __name__ == "__main__":
    unittest.main()
