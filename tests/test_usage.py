"""Tests for the best-effort token and time telemetry per subagent role."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from craftsman.records import AgentEvent, AgentUsage, FieldError, Ledger
from craftsman.usage import read_usage, role_totals, usage_json, usage_lines, usage_summary

AT = "2026-10-21T10:00:00.000Z"


def assistant(message_id: str | None, at: str, **usage: int) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "usage": usage}
    if message_id is not None:
        message["id"] = message_id
    return {"type": "assistant", "timestamp": at, "message": message}


def stop(agent: str, agent_id: str, usage: AgentUsage | None, at: str = AT) -> AgentEvent:
    return AgentEvent("claude", "stop", agent, agent_id, at, usage)


def used(out: int, seconds: float = 60.0) -> AgentUsage:
    return AgentUsage(input_tokens=10, output_tokens=out, cache_read_tokens=1000, cache_write_tokens=200, duration_s=seconds)


class ReadUsageTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def transcript(self, *entries: dict[str, Any] | str) -> Path:
        path = self.dir / "agent.jsonl"
        path.write_text(
            "\n".join(entry if isinstance(entry, str) else json.dumps(entry) for entry in entries) + "\n",
            encoding="utf-8",
        )
        return path

    def test_tokens_are_summed_over_messages_and_time_spans_the_entries(self) -> None:
        path = self.transcript(
            {"type": "user", "timestamp": "2026-10-21T10:00:00.000Z", "message": {"content": "go"}},
            assistant("m1", "2026-10-21T10:00:05.000Z", input_tokens=10, output_tokens=5,
                      cache_read_input_tokens=100, cache_creation_input_tokens=20),
            assistant("m2", "2026-10-21T10:01:30.000Z", input_tokens=1, output_tokens=7,
                      cache_read_input_tokens=130, cache_creation_input_tokens=0),
        )  # fmt: skip

        self.assertEqual(
            read_usage(path),
            AgentUsage(
                input_tokens=11, output_tokens=12, cache_read_tokens=230, cache_write_tokens=20, duration_s=90.0
            ),
        )

    def test_a_message_written_as_several_entries_counts_once(self) -> None:
        block = {"input_tokens": 4, "output_tokens": 9, "cache_read_input_tokens": 50}
        path = self.transcript(
            assistant("m1", "2026-10-21T10:00:01.000Z", **{**block, "output_tokens": 3}),
            assistant("m1", "2026-10-21T10:00:02.000Z", **block),
            assistant("m1", "2026-10-21T10:00:03.000Z", **block),
        )

        usage = read_usage(path)

        assert usage is not None
        self.assertEqual((usage.input_tokens, usage.output_tokens, usage.cache_read_tokens), (4, 9, 50))

    def test_entries_without_a_message_id_each_count(self) -> None:
        path = self.transcript(
            assistant(None, AT, output_tokens=2), assistant(None, AT, output_tokens=3)
        )

        usage = read_usage(path)

        assert usage is not None
        self.assertEqual(usage.output_tokens, 5)

    def test_junk_lines_and_odd_values_are_skipped(self) -> None:
        path = self.transcript(
            "not json",
            "[1, 2]",
            {"type": "assistant", "message": {"id": "m0", "usage": "lots"}},
            assistant("m1", "no timestamp", input_tokens=True, output_tokens=-4, cache_read_input_tokens=8),
        )

        usage = read_usage(path)

        assert usage is not None
        self.assertEqual((usage.input_tokens, usage.output_tokens, usage.cache_read_tokens), (0, 0, 8))
        self.assertEqual(usage.duration_s, 0.0)

    def test_a_missing_empty_or_usage_free_transcript_has_no_telemetry(self) -> None:
        self.assertIsNone(read_usage(self.dir / "absent.jsonl"))
        self.assertIsNone(read_usage(self.transcript()))
        self.assertIsNone(read_usage(self.transcript({"type": "user", "message": {"content": "hi"}})))

    def test_a_directory_or_binary_file_has_no_telemetry_and_raises_nothing(self) -> None:
        binary = self.dir / "binary.jsonl"
        binary.write_bytes(b"\xff\xfe\x00\x01" * 100)

        self.assertIsNone(read_usage(self.dir))
        self.assertIsNone(read_usage(binary))


class RoleTotalsTests(unittest.TestCase):
    def ledger(self, *events: AgentEvent) -> Ledger:
        return Ledger(agents=events)

    def test_no_telemetry_gives_no_totals_and_no_lines(self) -> None:
        ledger = self.ledger(AgentEvent("claude", "start", "craftsman-reviewer", "r1", AT), stop("craftsman-reviewer", "r1", None))

        self.assertEqual(role_totals(ledger), {})
        self.assertEqual(usage_lines(ledger), [])
        self.assertEqual(usage_json(ledger), {})
        self.assertEqual(usage_summary({}), "")

    def test_totals_are_per_role(self) -> None:
        ledger = self.ledger(
            stop("craftsman-reviewer", "r1", used(100, 60)),
            stop("craftsman:craftsman-reviewer", "r2", used(50, 30)),
            stop("craftsman-verifier", "v1", used(7, 12)),
            stop("Explore", "x1", used(9999)),
        )

        totals = role_totals(ledger)

        self.assertEqual(set(totals), {"reviewer", "verifier"})
        self.assertEqual(
            (totals["reviewer"].agents, totals["reviewer"].output_tokens, totals["reviewer"].duration_s),
            (2, 150, 90.0),
        )
        self.assertEqual((totals["verifier"].agents, totals["verifier"].output_tokens), (1, 7))

    def test_an_agent_stopped_twice_counts_once_by_its_latest_stop(self) -> None:
        ledger = self.ledger(stop("craftsman-reviewer", "r1", used(10)), stop("craftsman-reviewer", "r1", used(40)))

        self.assertEqual(role_totals(ledger)["reviewer"].output_tokens, 40)
        self.assertEqual(role_totals(ledger)["reviewer"].agents, 1)

    def test_lines_and_json_name_hunters_and_verifiers(self) -> None:
        ledger = self.ledger(stop("craftsman-reviewer", "r1", used(45_600, 852)), stop("craftsman-verifier", "v1", used(900, 60)))

        self.assertEqual(
            usage_lines(ledger),
            [
                "- hunters: 1 agent(s) · in 10 · out 45.6k · cache read 1.0k · cache write 200 · 14.2 min agent time",
                "- verifiers: 1 agent(s) · in 10 · out 900 · cache read 1.0k · cache write 200 · 1.0 min agent time",
            ],
        )
        self.assertEqual(usage_json(ledger)["hunters"]["output_tokens"], 45_600)
        self.assertEqual(usage_summary(usage_json(ledger)), "; hunters 1 (45.6k out, 14.2 min); verifiers 1 (900 out, 1.0 min)")


class AgentEventRecordTests(unittest.TestCase):
    BASE = {"platform": "claude", "event": "stop", "agent": "craftsman-reviewer", "agent_id": "r1", "at": AT}

    def test_usage_round_trips_through_json(self) -> None:
        event = stop("craftsman-reviewer", "r1", used(5))

        self.assertEqual(AgentEvent.from_json(event.to_json()), event)

    def test_an_event_without_usage_serializes_as_before_and_old_ledgers_still_load(self) -> None:
        event = AgentEvent.from_json(self.BASE)

        self.assertIsNone(event.usage)
        self.assertEqual(event.to_json(), self.BASE)
        self.assertEqual(AgentEvent.from_json({**self.BASE, "usage": None}), event)

    def test_usage_is_only_allowed_on_a_stop_event(self) -> None:
        with self.assertRaisesRegex(FieldError, "usage is only allowed on a stop event"):
            AgentEvent("claude", "start", "craftsman-reviewer", "r1", AT, used(5))

    def test_usage_rejects_negative_numbers_and_unknown_fields(self) -> None:
        good = used(5).to_json()
        with self.assertRaises(FieldError):
            AgentEvent.from_json({**self.BASE, "usage": {**good, "output_tokens": -1}})
        with self.assertRaisesRegex(FieldError, "not a known field"):
            AgentEvent.from_json({**self.BASE, "usage": {**good, "cost": 1}})


if __name__ == "__main__":
    unittest.main()
