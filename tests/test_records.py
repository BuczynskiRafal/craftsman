"""Tests for the ledger records: validation and JSON round-trips."""

from __future__ import annotations

import json
import re
import unittest
from dataclasses import replace
from typing import Any

from craftsman.ledger import add_agent_event, resolve, set_verdict
from craftsman.records import (
    LEAD_HUNTER,
    MIN_READABLE_SCHEMA,
    SCHEMA_VERSION,
    AgentEvent,
    Coverage,
    Finding,
    HuntPlan,
    Ledger,
    LedgerError,
    QualitySignal,
    Round,
    Run,
    RunKind,
    SignalKind,
    Slot,
    SlotClaim,
    VerdictEvent,
    Wave,
    slot_generation,
)

from tests.support import (
    at,
    cover_open_hunks,
    make_hunk,
    make_slot,
    make_snap,
    planned,
    prepared,
    record_finding,
    record_run,
)

EVIDENCE = "Reproduced via tests/test_app.py::test_empty (fails before fix)."
LONG_FUNCTION = QualitySignal("S1", SignalKind.LONG_FUNCTION, "a.py", 1, "`f` spans 50 lines")


def full_ledger() -> Ledger:
    snap = make_snap(make_hunk("H1", "a.py"), make_hunk("H2", "b.py"))
    ledger, _ = prepared(snap, signals=(LONG_FUNCTION,))
    ledger = cover_open_hunks(ledger)
    ledger = record_run(record_finding(ledger), kind="repro", exit_code=1)
    ledger, _ = set_verdict(ledger, "F1", "proven", EVIDENCE, "R1")
    ledger, _ = resolve(ledger, "F1", "fixed", "Guarded the empty list in total().", fingerprint="fp2")
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

    def test_a_stop_event_round_trips_with_and_without_records(self) -> None:
        stop = AgentEvent("claude", "stop", "craftsman-reviewer", "a", "2026-01-01T00:05:00.000Z")
        for records in (None, (), ("C1", "F2")):
            with self.subTest(records=records):
                event = replace(stop, records=records)
                raw = json.loads(json.dumps(event.to_json()))

                self.assertEqual(AgentEvent.from_json(raw), event)
                self.assertEqual("records" in raw, records is not None)

    def test_records_are_machine_tokens_of_a_stop_event(self) -> None:
        stop = AgentEvent("claude", "stop", "craftsman-reviewer", "a", "2026-01-01T00:05:00.000Z")
        for bad in (("H1",), ("C",), ("c1",), ("C1 ",)):
            with self.subTest(records=bad), self.assertRaises(LedgerError):
                replace(stop, records=bad)
        with self.assertRaises(LedgerError):
            replace(stop, event="start", records=("C1",))

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
            (
                {**ledger_json(), "schema": 2},
                "schema 2 is from an older craftsman; run `craftsman review reset` to archive it",
            ),
            ({**ledger_json(), "schema": 5}, "this ledger was written by a newer Craftsman (schema 5); upgrade craftsman"),
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
            ("coverage", None, "coverage is missing"),
            ("rounds", "signals", "rounds[0].signals is missing"),
            ("findings", "title", "findings[0].title is missing"),
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
    def test_coverage_must_cover_hunks_of_its_round(self) -> None:
        raw = ledger_json()
        raw["coverage"] = [
            {
                "round": 1,
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

    def test_a_test_run_must_report_at_least_its_minimum_of_tests(self) -> None:
        cases: list[tuple[RunKind, int | None, int, bool, str]] = [
            # kind, tests_ran, min_tests, counts, shortfall
            ("test", None, 1, True, ""),
            ("test", 1, 1, True, ""),
            ("test", 40, 5, True, ""),
            ("test", 0, 1, False, "R1 ran 0 tests (minimum 1)"),
            ("test", 1, 2, False, "R1 ran 1 test (minimum 2)"),
            ("test", 0, 0, True, ""),
            ("e2e", 0, 1, True, ""),
            ("other", 0, 1, True, ""),
        ]
        for kind, ran, minimum, counts, shortfall in cases:
            with self.subTest(kind=kind, ran=ran, minimum=minimum):
                run = replace(self.recorded, kind=kind, tests_ran=ran, min_tests=minimum)
                self.assertEqual(run.counts_as_passing, counts)
                self.assertEqual(run.too_few_tests, not counts)
                self.assertEqual(run.tests_shortfall, shortfall)

    def test_a_run_that_ran_too_few_tests_says_so_next_to_its_exit_code(self) -> None:
        self.assertEqual(self.recorded.exit_text, "exit 0")
        self.assertEqual(replace(self.recorded, tests_ran=0).exit_text, "exit 0, 0 tests - not counted")
        self.assertEqual(
            replace(self.recorded, tests_ran=1, min_tests=2).exit_text, "exit 0, 1 test - not counted"
        )
        self.assertEqual(replace(self.recorded, tests_ran=0, min_tests=0).exit_text, "exit 0")
        self.assertEqual(replace(self.recorded, exit_code=1, tests_ran=0).exit_text, "exit 1")
        self.assertTrue(replace(self.recorded, tests_ran=0, min_tests=0).switched_off_test_minimum)
        self.assertFalse(replace(self.recorded, tests_ran=3, min_tests=0).switched_off_test_minimum)

    def test_a_failing_or_tree_changing_run_is_not_too_few_tests(self) -> None:
        failing = replace(self.recorded, exit_code=1, tests_ran=0)
        changed = replace(self.recorded, tests_ran=0, fingerprint_after="fp2")

        self.assertFalse(failing.counts_as_passing)
        self.assertFalse(failing.too_few_tests)
        self.assertFalse(changed.counts_as_passing)
        self.assertFalse(changed.too_few_tests)

    def test_test_counts_must_not_be_negative(self) -> None:
        with self.assertRaisesRegex(LedgerError, "tests_ran must be >= 0 or null"):
            replace(self.recorded, tests_ran=-1)
        with self.assertRaisesRegex(LedgerError, "min_tests must be >= 0"):
            replace(self.recorded, min_tests=-1)

    def test_test_counts_round_trip_and_are_optional_on_read(self) -> None:
        counted = replace(self.recorded, tests_ran=0, min_tests=3)
        raw = counted.to_json()
        older = {key: value for key, value in self.recorded.to_json().items() if key not in ("tests_ran", "min_tests")}

        self.assertEqual((raw["tests_ran"], raw["min_tests"]), (0, 3))
        self.assertEqual(Run.from_json(json.loads(json.dumps(raw))), counted)
        loaded = Run.from_json(older)
        self.assertEqual((loaded.tests_ran, loaded.min_tests), (None, 1))
        self.assertEqual(loaded, self.recorded)
        with self.assertRaisesRegex(LedgerError, "tests_ran must be an integer or null"):
            Run.from_json({**raw, "tests_ran": "many"})
        with self.assertRaisesRegex(LedgerError, "min_tests must be an integer"):
            Run.from_json({**raw, "min_tests": None})

    def test_a_ledger_without_the_new_run_fields_loads_and_stays_schema_4(self) -> None:
        raw = ledger_json()
        for run in raw["runs"]:
            run.pop("tests_ran")
            run.pop("min_tests")

        loaded = Ledger.from_json(raw)

        self.assertEqual({run.tests_ran for run in loaded.runs}, {None})
        self.assertEqual(loaded.to_json()["schema"], 4)


def schema_3_json() -> dict[str, Any]:
    """The ledger of full_ledger() as Craftsman 5.0 wrote it: none of the keys schema 4 added."""
    raw = ledger_json()
    raw["schema"] = 3
    del raw["claims"]
    for round_ in raw["rounds"]:
        for key in ("plan", "platform", "enforced", "task"):
            del round_[key]
    for record in raw["coverage"]:
        del record["hunter"], record["by"]
    for finding in raw["findings"]:
        for key in ("hunter", "by", "verdict_log", "merged_into"):
            del finding[key]
    return raw


def legacy_duplicate(finding_id: str, evidence: str) -> dict[str, Any]:
    """A finding as Craftsman 5.0 stored a merged duplicate: dismissed, its target only in the wording of the evidence."""
    raw = schema_3_json()["findings"][0]
    return {
        **raw,
        "id": finding_id,
        "verdict": "dismissed",
        "verdict_evidence": evidence,
        "verdict_run": None,
        "resolution": None,
        "resolution_note": "",
    }


def grouped(target: str) -> str:
    return f"Grouped into {target} (requests/models.py:633): same root cause."


def schema_3_with_merges() -> dict[str, Any]:
    """Shaped like the real schema-3 ledgers: judged findings plus duplicates that name a target in their evidence.

    F1 proven; F2 and F3 grouped into F1; F4 dismissed on its own evidence; F5 grouped into F9, which does
    not exist; F6 grouped into F2, which is itself a duplicate; F7 "grouped" into itself.
    """
    raw = schema_3_json()
    raw["findings"] = [
        raw["findings"][0],
        legacy_duplicate("F2", grouped("F1")),
        legacy_duplicate("F3", grouped("F1")),
        legacy_duplicate("F4", "guarded by requests/models.py:633, which returns early on an empty body"),
        legacy_duplicate("F5", grouped("F9")),
        legacy_duplicate("F6", grouped("F2")),
        legacy_duplicate("F7", grouped("F7")),
    ]
    return raw


class LegacyMergeTests(unittest.TestCase):
    def test_a_schema_3_duplicate_keeps_its_target(self) -> None:
        loaded = Ledger.from_json(schema_3_with_merges())

        self.assertEqual(
            {finding.id: finding.merged_into for finding in loaded.findings},
            {"F1": None, "F2": "F1", "F3": "F1", "F4": None, "F5": None, "F6": None, "F7": None},
        )
        self.assertTrue(all(finding.verdict_log == () for finding in loaded.findings))

    def test_a_duplicate_of_a_missing_or_merged_finding_loads_without_a_link(self) -> None:
        loaded = Ledger.from_json(schema_3_with_merges())
        by_id = {finding.id: finding for finding in loaded.findings}

        self.assertEqual(by_id["F5"].verdict, "dismissed")  # still dismissed, only the link is dropped
        self.assertEqual(by_id["F6"].verdict, "dismissed")

    def test_the_derived_links_are_written_and_read_back(self) -> None:
        loaded = Ledger.from_json(schema_3_with_merges())

        again = Ledger.from_json(json.loads(json.dumps(loaded.to_json())))

        self.assertEqual(again, loaded)
        self.assertEqual(again.findings[1].merged_into, "F1")

    def test_only_a_dismissed_finding_with_the_wording_at_its_start_is_a_duplicate(self) -> None:
        raw = schema_3_with_merges()
        raw["findings"][3]["verdict_evidence"] = "see the note: Grouped into F1 (a.py:1): same root cause."
        raw["findings"][2]["verdict"] = "probable"
        raw["findings"][2]["verdict_evidence"] = "no-repro: " + grouped("F1")

        loaded = Ledger.from_json(raw)

        self.assertEqual((loaded.findings[2].merged_into, loaded.findings[3].merged_into), (None, None))

    def test_a_stored_link_is_never_derived_over_or_forgiven(self) -> None:
        raw = schema_3_with_merges()
        raw["findings"][1]["merged_into"] = None  # schema 4 stored "not merged"
        self.assertIsNone(Ledger.from_json(raw).findings[1].merged_into)
        raw["findings"][2]["merged_into"] = "F9"
        assert_rejected(self, raw, "findings[2].merged_into references an unknown finding 'F9'")


class MergeChainTests(unittest.TestCase):
    def merged(self, finding_id: str, target: str) -> dict[str, Any]:
        return {**legacy_duplicate(finding_id, grouped(target)), "merged_into": target}

    def test_a_ledger_rejects_a_merge_into_a_merged_finding(self) -> None:
        raw = schema_3_json()
        raw["schema"] = 4
        raw["findings"] = [raw["findings"][0], self.merged("F2", "F1"), self.merged("F3", "F2")]

        assert_rejected(self, raw, "findings[2].merged_into F2 is itself merged into F1")

    def test_a_ledger_rejects_a_merge_cycle(self) -> None:
        raw = schema_3_json()
        raw["schema"] = 4
        raw["findings"] = [self.merged("F1", "F2"), self.merged("F2", "F1")]

        assert_rejected(self, raw, "merges do not chain")


class SchemaTests(unittest.TestCase):
    def test_the_schema_is_four_and_schema_three_is_still_readable(self) -> None:
        self.assertEqual((SCHEMA_VERSION, MIN_READABLE_SCHEMA), (4, 3))
        self.assertEqual(ledger_json()["schema"], 4)

    def test_a_schema_3_ledger_loads_with_defaults_and_is_written_as_schema_4(self) -> None:
        loaded = Ledger.from_json(schema_3_json())

        self.assertEqual(loaded.claims, ())
        round_ = loaded.rounds[0]
        self.assertEqual((round_.plan, round_.platform, round_.enforced, round_.task), (None, "cli", False, ""))
        self.assertEqual([(c.hunter, c.by) for c in loaded.coverage], [(None, None)])
        finding = loaded.findings[0]
        self.assertEqual((finding.hunter, finding.by, finding.verdict_log, finding.merged_into), (None, None, (), None))
        self.assertEqual(finding.verdict, "proven")
        written = loaded.to_json()
        self.assertEqual(written["schema"], 4)
        self.assertEqual(Ledger.from_json(json.loads(json.dumps(written))), loaded)

    def test_schemas_below_3_and_above_4_are_rejected_with_their_own_message(self) -> None:
        for schema, message in (
            (2, "schema 2 is from an older craftsman; run `craftsman review reset` to archive it"),
            (1, "schema 1 is from an older craftsman"),
            (5, "this ledger was written by a newer Craftsman (schema 5); upgrade craftsman"),
        ):
            with self.subTest(schema=schema):
                assert_rejected(self, {**ledger_json(), "schema": schema}, message)

    def test_new_keys_stay_strict_inside_a_schema_4_ledger(self) -> None:
        raw = ledger_json()
        raw["findings"][0]["surprise"] = 1

        assert_rejected(self, raw, "findings[0].surprise is not a known field")

    def test_unknown_fields_are_all_named_and_blamed_on_a_newer_craftsman(self) -> None:
        raw = ledger_json()
        raw["findings"][0]["surprise"] = 1
        assert_rejected(self, raw, "a newer Craftsman may have written it")

        top = {**ledger_json(), "zeta": 1, "alpha": 2}
        assert_rejected(self, top, "alpha is not a known field (also unknown: zeta)")
        assert_rejected(self, top, "a newer Craftsman may have written them")

        nested = ledger_json()
        nested["runs"][0].update({"b_field": 1, "a_field": 2})
        assert_rejected(self, nested, "runs[0].a_field is not a known field (also unknown: b_field)")

    def test_stop_event_records_may_be_claims_coverage_findings_or_verdicts(self) -> None:
        stop = AgentEvent("claude", "stop", "craftsman-reviewer", "a", at(1), records=("B1", "C3", "F2", "V4"))

        self.assertEqual(AgentEvent.from_json(json.loads(json.dumps(stop.to_json()))), stop)
        with self.assertRaisesRegex(LedgerError, "must look like B1, C3, F2 or V4"):
            replace(stop, records=("X1",))


class SlotTests(unittest.TestCase):
    def test_every_kind_of_slot_id_is_accepted(self) -> None:
        slots = [
            make_slot("W1-1"),
            make_slot("W1-2", kind="simplicity", lenses=("craft", "design")),
            make_slot("W1-3", kind="sweep", lenses=("sweep",)),
            make_slot("W2-X1"),
            make_slot("W3-X2", trigger="F4"),
            make_slot("W2-F3"),
        ]
        self.assertEqual([slot_generation(slot.id) for slot in slots], [1, 1, 1, 2, 3, 2])
        for slot in slots:
            with self.subTest(slot=slot.id):
                self.assertEqual(Slot.from_json(json.loads(json.dumps(slot.to_json()))), slot)

    def test_slot_generation_is_none_for_anything_that_is_not_a_slot_id(self) -> None:
        for value in ("lead", "W4-1", "W1-0", "W1-X0", "W2-F", "w1-1", "W1-1 ", "H1", ""):
            with self.subTest(value=value):
                self.assertIsNone(slot_generation(value))

    def test_invalid_slots_are_rejected_with_the_field(self) -> None:
        good = make_slot("W1-1")
        cases: list[tuple[Slot | dict[str, Any], str]] = [
            ({"id": "W9-1"}, "id must look like W1-1, W2-X1 or W2-F3"),
            ({"generation": 2}, "generation must be 1 to match the id W1-1"),
            ({"kind": "fresh"}, "kind of W1-1 must be bug, simplicity or sweep"),
            ({"kind": "mystery"}, "kind must be one of bug"),
            ({"trigger": "deep"}, "trigger must be null for a generation 1 slot"),
            ({"id": "W2-3", "generation": 2}, "a numbered slot belongs to generation 1"),
            ({"id": "W1-X1", "kind": "fresh", "trigger": "deep"}, "a fresh or variant slot belongs to generation 2 or 3"),
            ({"id": "W2-X1", "generation": 2, "trigger": "deep"}, "kind of W2-X1 must be fresh"),
            ({"id": "W2-X1", "generation": 2, "kind": "fresh", "trigger": None}, 'trigger of a fresh slot must be "deep"'),
            ({"id": "W2-X1", "generation": 2, "kind": "fresh", "trigger": "later"}, 'trigger of a fresh slot must be "deep"'),
            ({"id": "W2-F3", "generation": 2, "kind": "fresh", "trigger": "F3"}, "kind of W2-F3 must be variant"),
            ({"id": "W2-F3", "generation": 2, "kind": "variant", "trigger": "F4"}, "trigger of a variant slot must be the finding F3"),
            ({"id": "W2-F3", "generation": 2, "kind": "variant", "trigger": None}, "trigger of a variant slot must be the finding F3"),
            ({"hunks": ()}, "hunks must list at least one hunk"),
            ({"hunks": ("H1", "H1")}, "hunks must not repeat an id"),
            ({"hunks": ("1",)}, "hunks[0] must look like H1"),
            ({"lenses": ()}, "lenses must list at least one lens"),
            ({"lenses": ("intent", "intent")}, "lenses must not repeat an id"),
            ({"lenses": ("vibes",)}, "lenses[0] must be one of"),
            ({"changed_lines": -1}, "changed_lines must be >= 0"),
            ({"since": "yesterday"}, "since must be an ISO-8601 timestamp"),
        ]
        for change, message in cases:
            assert isinstance(change, dict)
            with self.subTest(message=message), self.assertRaisesRegex(LedgerError, re.escape(message)):
                replace(good, **change)

    def test_slot_json_is_strict(self) -> None:
        raw = make_slot("W1-1").to_json()
        for broken, message in (
            ({**raw, "surprise": 1}, "surprise is not a known field"),
            ({k: v for k, v in raw.items() if k != "trigger"}, "trigger is missing"),
            ({**raw, "hunks": "H1"}, "hunks must be a list of strings"),
        ):
            with self.subTest(message=message), self.assertRaisesRegex(LedgerError, message):
                Slot.from_json(broken)


class HuntPlanTests(unittest.TestCase):
    def test_a_plan_looks_up_its_slots_and_round_trips(self) -> None:
        plan = HuntPlan(("deep: auth signal",), (make_slot("W1-1"), make_slot("W1-2", hunks=("H2",))))

        self.assertEqual(plan.slot("W1-2"), plan.slots[1])
        self.assertIsNone(plan.slot("W1-3"))
        self.assertEqual(HuntPlan.from_json(json.loads(json.dumps(plan.to_json()))), plan)

    def test_slot_ids_are_unique_within_a_plan(self) -> None:
        with self.assertRaisesRegex(LedgerError, "slots must not repeat an id"):
            HuntPlan((), (make_slot("W1-1"), make_slot("W1-1")))

    def test_a_plan_keeps_its_waves_through_json_and_reads_one_without(self) -> None:
        slots = (make_slot("W1-1"), make_slot("W2-X1", trigger="F1"), make_slot("W2-F1"))
        plan = HuntPlan(("normal",), slots, (Wave(2, at(5), ("F1", "F2"), ("W2-X1", "W2-F1")),))
        raw = json.loads(json.dumps(plan.to_json()))

        self.assertEqual(HuntPlan.from_json(raw), plan)
        del raw["waves"]  # a plan written before waves were recorded
        self.assertEqual(HuntPlan.from_json(raw), replace(plan, waves=()))
        self.assertEqual(HuntPlan((), slots).waves, ())

    def test_a_wave_may_only_name_slots_of_its_plan(self) -> None:
        with self.assertRaisesRegex(LedgerError, r"waves\[0\]\.slots are not slots of this plan: \['W2-X1'\]"):
            HuntPlan((), (make_slot("W1-1"),), (Wave(2, at(1), ("F1",), ("W2-X1",)),))

    def test_a_wave_is_a_decision_about_findings_of_a_later_generation(self) -> None:
        for kwargs, message in (
            ({"generation": 1}, "generation must be 2..3"),
            ({"generation": 4}, "generation must be 2..3"),
            ({"planned_at": "yesterday"}, "planned_at must be an ISO-8601 timestamp"),
            ({"considered": ()}, "considered must name at least one finding"),
            ({"considered": ("F1", "F1")}, "considered must not repeat an id"),
            ({"considered": ("X1",)}, r"considered\[0\] must look like F1"),
            ({"slots": ("W2-X1", "W2-X1")}, "slots must not repeat an id"),
            ({"slots": ("nope",)}, r"slots\[0\] must look like W1-1"),
            ({"slots": ("W3-X1",)}, r"slots\[0\] W3-X1 is not a slot of generation 2"),
            ({"slots": ("W2-X1", "W1-1")}, r"slots\[1\] W1-1 is not a slot of generation 2"),
        ):
            with self.subTest(message=message):
                base: dict[str, Any] = {"generation": 2, "planned_at": at(1), "considered": ("F1",), "slots": ()}
                with self.assertRaisesRegex(LedgerError, message):
                    Wave(**{**base, **kwargs})

    def test_a_finding_is_answered_by_one_wave_of_a_plan_only(self) -> None:
        slots = (make_slot("W1-1"), make_slot("W2-X1"), make_slot("W3-X1"))
        waves = (Wave(2, at(1), ("F1", "F2"), ("W2-X1",)), Wave(3, at(2), ("F2",), ("W3-X1",)))

        with self.assertRaisesRegex(LedgerError, "waves must not repeat an id"):
            HuntPlan((), slots, waves)

    def test_the_last_generation_decides_findings_without_planning_slots(self) -> None:
        wave = Wave(3, at(2), ("F4",))

        self.assertEqual(wave.slots, ())
        self.assertEqual(Wave.from_json(json.loads(json.dumps(wave.to_json()))), wave)

    def test_a_round_carries_its_plan_through_json(self) -> None:
        ledger, _ = prepared(make_snap(make_hunk("H1", "a.py"), make_hunk("H2", "b.py")))
        ledger = planned(ledger, make_slot("W1-1", hunks=("H1", "H2")), reasons=("normal",))
        round_ = replace(ledger.rounds[0], platform="antigravity", enforced=True, task="fix totals")

        self.assertEqual(Round.from_json(json.loads(json.dumps(round_.to_json()))), round_)

    def test_a_plan_may_only_name_hunks_of_its_round(self) -> None:
        ledger, round_ = prepared(make_snap(make_hunk("H1")))

        with self.assertRaisesRegex(LedgerError, r"plan\.slots\.W1-1\.hunks are not hunks of this round: \['H7'\]"):
            replace(round_, plan=HuntPlan((), (make_slot("W1-1", hunks=("H1", "H7")),)))

    def test_a_round_needs_a_platform(self) -> None:
        _, round_ = prepared(make_snap(make_hunk("H1")))

        with self.assertRaisesRegex(LedgerError, "platform must not be blank"):
            replace(round_, platform=" ")


class AttributionFieldTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ledger, _ = prepared(make_snap(make_hunk("H1", "a.py")))
        self.digest = self.ledger.rounds[0].hunks[0].digest

    def coverage(self, **overrides: Any) -> Coverage:
        fields: dict[str, Any] = {
            "round": 1,
            "digests": (self.digest,),
            "note": "H1 a.py:1: x=1 -> returns 2 on boundary input",
            "recorded_at": at(1),
        }
        return Coverage(**{**fields, **overrides})

    def test_coverage_names_a_slot_or_none_but_never_the_lead(self) -> None:
        for hunter in (None, "W1-2", "W2-X1", "W2-F3"):
            with self.subTest(hunter=hunter):
                record = self.coverage(hunter=hunter, by="antigravity:conv-1")
                self.assertEqual(Coverage.from_json(json.loads(json.dumps(record.to_json()))), record)
        for hunter, message in ((LEAD_HUNTER, "never credits coverage"), ("slot-one", "hunter must look like W1-1")):
            with self.subTest(hunter=hunter), self.assertRaisesRegex(LedgerError, message):
                self.coverage(hunter=hunter)
        with self.assertRaisesRegex(LedgerError, "by must not be blank"):
            self.coverage(by=" ")

    def test_a_coverage_record_without_the_new_keys_still_parses(self) -> None:
        raw = self.coverage().to_json()
        del raw["hunter"], raw["by"]

        self.assertEqual(Coverage.from_json(raw), self.coverage())

    def finding(self, **overrides: Any) -> Finding:
        return replace(record_finding(self.ledger).findings[0], **overrides)

    def test_a_finding_names_its_slot_or_the_lead(self) -> None:
        for hunter in (None, LEAD_HUNTER, "W1-3", "W2-F1"):
            with self.subTest(hunter=hunter):
                found = self.finding(hunter=hunter, by="antigravity:conv-1")
                self.assertEqual(Finding.from_json(json.loads(json.dumps(found.to_json()))), found)
        with self.assertRaisesRegex(LedgerError, "hunter must look like W1-1"):
            self.finding(hunter="orchestrator")

    def judged(self) -> Finding:
        events = (VerdictEvent("V1", "probable", at(1)), VerdictEvent("V2", "dismissed", at(2), "antigravity:c"))
        return self.finding(
            verdict="dismissed",
            verdict_evidence="guarded by app.py:2, which returns 0 for an empty list",
            verdict_log=events,
        )

    def test_the_verdict_log_ends_with_the_current_verdict_and_has_unique_tokens(self) -> None:
        finding = self.judged()

        self.assertEqual(Finding.from_json(json.loads(json.dumps(finding.to_json()))), finding)
        with self.assertRaisesRegex(LedgerError, "verdict_log must end with the current verdict"):
            replace(finding, verdict="probable", verdict_evidence="no-repro: needs a live database; app.py:3")
        with self.assertRaisesRegex(LedgerError, "verdict_log must end with the current verdict"):
            self.finding(verdict_log=finding.verdict_log)  # events without a verdict
        with self.assertRaisesRegex(LedgerError, "verdict_log must not repeat an id"):
            replace(finding, verdict_log=(VerdictEvent("V1", "probable", at(1)), VerdictEvent("V1", "dismissed", at(2))))

    def test_a_merged_duplicate_is_dismissed_and_points_at_another_finding(self) -> None:
        dismissed = self.judged()

        self.assertEqual(replace(dismissed, merged_into="F2").merged_into, "F2")
        with self.assertRaisesRegex(LedgerError, "merged_into must name another finding"):
            replace(dismissed, merged_into=dismissed.id)
        with self.assertRaisesRegex(LedgerError, "merged_into is only allowed for a dismissed finding"):
            self.finding(merged_into="F2")
        with self.assertRaisesRegex(LedgerError, "merged_into must look like F1"):
            replace(dismissed, merged_into="target")

    def test_a_ledger_rejects_a_merge_into_an_unknown_finding_and_repeated_tokens(self) -> None:
        ledger = record_finding(record_finding(self.ledger))
        first, second = ledger.findings
        merged = replace(second, merged_into="F9", verdict="dismissed", verdict_evidence="guarded by app.py:2 already")

        with self.assertRaisesRegex(LedgerError, r"findings\[1\]\.merged_into references an unknown finding 'F9'"):
            replace(ledger, findings=(first, merged))
        event = VerdictEvent("V1", "dismissed", at(1))
        twice = (
            replace(first, verdict="dismissed", verdict_evidence="guarded by app.py:2 already", verdict_log=(event,)),
            replace(second, verdict="dismissed", verdict_evidence="guarded by app.py:2 already", verdict_log=(event,)),
        )
        with self.assertRaisesRegex(LedgerError, "verdict_log must not repeat an id"):
            replace(ledger, findings=twice)


class VerdictEventAndClaimTests(unittest.TestCase):
    def test_verdict_events_round_trip_and_validate(self) -> None:
        event = VerdictEvent("V3", "question", at(2), "antigravity:conv-1")

        self.assertEqual(VerdictEvent.from_json(json.loads(json.dumps(event.to_json()))), event)
        cases: list[tuple[dict[str, Any], str]] = [
            ({"token": "F3"}, "token must look like V1"),
            ({"token": "V0"}, "token must look like V1"),
            ({"verdict": "maybe"}, "verdict must be one of proven"),
            ({"at": "noon"}, "at must be an ISO-8601 timestamp"),
            ({"by": ""}, "by must not be blank"),
        ]
        for change, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(LedgerError, message):
                replace(event, **change)

    def test_slot_claims_round_trip_and_validate(self) -> None:
        claim = SlotClaim(1, "W2-X1", "B4", at(3), "antigravity:conv-1")

        self.assertEqual(SlotClaim.from_json(json.loads(json.dumps(claim.to_json()))), claim)
        for change, message in (
            ({"round": 0}, "round must be >= 1"),
            ({"slot": "lead"}, "slot must look like W1-1"),
            ({"token": "C4"}, "token must look like B1"),
            ({"at": "noon"}, "at must be an ISO-8601 timestamp"),
        ):
            with self.subTest(message=message), self.assertRaisesRegex(LedgerError, message):
                replace(claim, **change)

    def test_a_ledger_checks_its_claims(self) -> None:
        ledger, _ = prepared(make_snap(make_hunk("H1")))
        claim = SlotClaim(1, "W1-1", "B1", at(1))

        self.assertEqual(Ledger.from_json(replace(ledger, claims=(claim,)).to_json()).claims, (claim,))
        with self.assertRaisesRegex(LedgerError, r"claims\[0\]\.round references an unknown round"):
            replace(ledger, claims=(replace(claim, round=2),))
        with self.assertRaisesRegex(LedgerError, "claims must not repeat an id"):
            replace(ledger, claims=(claim, replace(claim, slot="W1-2")))


if __name__ == "__main__":
    unittest.main()
