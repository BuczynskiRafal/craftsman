"""Tests for the ledger operations, persistence and locking."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest import mock

from craftsman import ledger as ledger_module
from craftsman.ledger import (
    COVERAGE_NOTE_EXAMPLE,
    ack_signal,
    add_coverage,
    add_fact,
    add_multi_coverage,
    coverage_units_for_targets,
    file_sha256,
    ledger_path_for,
    load,
    locked,
    merge_findings,
    mutate,
    reopened_mentions,
    resolve,
    save,
    set_verdict,
    uncovered_by_lens,
    valid_facts_for_hunk,
    waive_run,
)
from craftsman.records import (
    LEDGER_FILENAME,
    BatchRejected,
    EntryProblem,
    FieldError,
    Ledger,
    LedgerError,
    QualitySignal,
    SignalKind,
    SourceRef,
)

from tests.support import (
    EvidenceRepo,
    cover_open_hunks,
    coverage_note,
    coverage_record,
    fully_reviewed,
    make_hunk,
    make_snap,
    prepared,
    record_finding,
    record_run,
    shifted,
    swept,
    two_hunks,
)

EVIDENCE = "Reproduced via tests/test_app.py::test_empty (fails before fix)."
PROBABLE = "no-repro: needs a live database; app.py:3 divides by len(items) == 0"
DISMISSED = "guarded by app.py:2, which returns 0 for an empty list"
LONG_FUNCTION = QualitySignal("S1", SignalKind.LONG_FUNCTION, "a.py", 1, "`f` spans 50 lines")
TEST_NAME = QualitySignal("S2", SignalKind.TEST_NAME, "t.py", 3, "`test_x_and_y` contains `_and_`")
DIFF_SIZE = QualitySignal("S3", SignalKind.DIFF_SIZE, "a.py", 1, "+900 production lines added")
CONCRETE_REASON = "`f` in a.py:1 is a flat table of 50 literal cases"


class OpenRoundTests(unittest.TestCase):
    def test_rounds_are_numbered_from_one(self) -> None:
        _, first = prepared(make_snap(make_hunk("H1")))

        self.assertEqual(first.number, 1)

    def test_preparing_an_unchanged_round_again_changes_nothing(self) -> None:
        snap = make_snap(make_hunk("H1"))
        ledger, first = prepared(snap)

        again, same = prepared(snap, ledger=ledger)

        self.assertIs(again, ledger)
        self.assertEqual(same, first)

    def test_a_stricter_tier_opens_a_new_round_for_the_same_diff(self) -> None:
        snap = make_snap(make_hunk("H1"))
        ledger, _ = prepared(snap)

        _, second = prepared(snap, tier="deep", ledger=ledger)

        self.assertEqual(second.number, 2)

    def test_a_changed_base_requires_a_reset(self) -> None:
        ledger, _ = prepared(make_snap(make_hunk("H1")))
        with self.assertRaisesRegex(LedgerError, "craftsman review reset"):
            prepared(make_snap(make_hunk("H1"), base="c" * 40, fingerprint="fp2"), ledger=ledger)

    def test_hunks_fully_covered_earlier_are_carried(self) -> None:
        h1, h2 = make_hunk("H1", "a.py"), make_hunk("H2", "b.py")
        ledger, _ = prepared(make_snap(h1, h2))
        ledger, _ = add_coverage(ledger, "intent", ["H1", "H2"], coverage_note(["H1", "H2"]))
        ledger, _ = add_coverage(ledger, "craft", ["H1"], coverage_note(["H1"]))

        h3 = make_hunk("H3", "c.py")
        _, round_ = prepared(make_snap(h1, h2, h3, fingerprint="fp2"), ledger=ledger)

        self.assertEqual([hunk.carried for hunk in round_.hunks], [True, False, False])

    def test_carried_hunk_is_reopened_when_it_mentions_a_changed_symbol(self) -> None:
        caller = make_hunk("H1", "a.py", (" total = compute_total(x)", "+y = 1"))
        unrelated = make_hunk("H2", "b.py", ("+z = 2",))
        ledger = fully_reviewed(make_snap(caller, unrelated))

        changed = make_hunk(
            "H3", "c.py", ("-def compute_total(items):", "+def compute_total(items, rate):")
        )
        snap = make_snap(caller, unrelated, changed, fingerprint="fp2")
        _, round_ = prepared(snap, ledger=ledger, symbols={"H3": ["compute_total"]})

        self.assertEqual([hunk.carried for hunk in round_.hunks], [False, True, False])

    def test_reopening_cascades_through_symbols_of_reopened_hunks(self) -> None:
        first = make_hunk("H1", "a.py", ("+def helper_one():",))
        second = make_hunk("H2", "b.py", ("+value = helper_one()",))
        ledger = fully_reviewed(make_snap(first, second))

        trigger = make_hunk("H3", "c.py", ("+def helper_zero():",))
        reworked = make_hunk("H1", "a.py", ("+def helper_one():", " helper_zero()"))
        snap = make_snap(reworked, second, trigger, fingerprint="fp2")
        symbols = {"H1": ["helper_one"], "H3": ["helper_zero"]}
        _, round_ = prepared(snap, ledger=ledger, symbols=symbols)

        self.assertEqual([hunk.carried for hunk in round_.hunks], [False, False, False])

    def test_only_added_or_context_lines_reopen_a_hunk(self) -> None:
        caller = make_hunk(
            "H1",
            "a.py",
            (
                "~ mode compute_total",
                "-total = compute_total(x)",
                "+total = 0",
                "\\ No newline at end of file",
            ),
        )
        ledger = fully_reviewed(make_snap(caller))
        changed = make_hunk("H2", "b.py", ("+def compute_total(items, rate):", "+    file = end"))
        snap = make_snap(caller, changed, fingerprint="fp2")

        _, round_ = prepared(snap, ledger=ledger, symbols={"H2": ["compute_total", "newline", "file"]})

        self.assertEqual([hunk.carried for hunk in round_.hunks], [True, False])

    def test_symbols_for_unknown_hunks_are_rejected(self) -> None:
        with self.assertRaisesRegex(LedgerError, "H9"):
            prepared(make_snap(make_hunk("H1")), symbols={"H9": ["x"]})

    def test_unknown_lens_is_rejected(self) -> None:
        with self.assertRaisesRegex(LedgerError, "lenses must be one of"):
            prepared(make_snap(make_hunk("H1")), lenses=("intent", "security"))


class ReopenedMentionsTests(unittest.TestCase):
    def test_mentions_name_the_symbol_with_its_defining_hunk(self) -> None:
        caller = make_hunk("H1", "a.py", (" total = compute_total(x)", "+y = 1"))
        ledger = fully_reviewed(make_snap(caller))
        changed = make_hunk("H2", "b.py", ("+def compute_total(items, rate):",))
        snap = make_snap(caller, changed, fingerprint="fp2")
        symbols = {"H2": ["compute_total"]}
        _, round_ = prepared(snap, ledger=ledger, symbols=symbols)

        self.assertEqual(
            reopened_mentions(round_, snap.hunks, symbols), {"H1": (("compute_total", "H2"),)}
        )


class CoverageTests(unittest.TestCase):
    def setUp(self) -> None:
        hunks = [make_hunk(f"H{index}", f"f{index}.py") for index in range(1, 11)]
        self.ledger, self.round = prepared(make_snap(*hunks), lenses=("intent", "craft"))

    def test_records_digests_against_the_latest_round(self) -> None:
        note = coverage_note(["H2", "H1"])
        ledger, record = add_coverage(self.ledger, "intent", ["H2", "H1"], f"  {note}  ")

        self.assertEqual(record.round, 1)
        self.assertEqual(record.digests, (self.round.hunks[1].digest, self.round.hunks[0].digest))
        self.assertEqual(record.note, note)
        self.assertEqual(ledger.coverage, (record,))

    def test_rejections_explain_what_is_valid(self) -> None:
        five = ["H1", "H2", "H3", "H4", "H5"]
        cases = [
            ("security", ["H1"], coverage_note(["H1"]), "unknown lens 'security'; valid lenses:"),
            ("trust", ["H1"], coverage_note(["H1"]), "not selected in round 1; selected lenses:"),
            ("intent", [], "", "cover 1..4 hunks per call"),
            ("intent", five, coverage_note(five), "got 5"),
            ("intent", ["H1", "H1"], coverage_note(["H1"]), "hunks must not repeat"),
            ("intent", ["H42"], coverage_note(["H42"]), "unknown hunk ids in round 1: H42"),
            ("intent", ["H1", "H2"], coverage_note(["H1", "H12"]), "missing: H2"),
        ]
        for lens, ids, note, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(LedgerError, message):
                add_coverage(self.ledger, lens, ids, note)

    def test_note_rejection_stores_example_separately(self) -> None:
        with self.assertRaises(FieldError) as caught:
            add_coverage(self.ledger, "intent", ["H1", "H2"], coverage_note(["H1", "H12"]))

        self.assertEqual(caught.exception.field, "note")
        self.assertEqual(caught.exception.example, COVERAGE_NOTE_EXAMPLE)
        self.assertNotIn("Example:", str(caught.exception))

    def test_note_reports_every_bad_target_in_one_pass(self) -> None:
        vague = "H3 examined every branch across the whole implementation without finding flaws"
        note = f"H1 short; {vague}"

        with self.assertRaises(FieldError) as caught:
            add_coverage(self.ledger, "intent", ["H1", "H2", "H3", "H4"], note)

        exc = caught.exception
        self.assertEqual(exc.field, "note")
        self.assertEqual(exc.example, COVERAGE_NOTE_EXAMPLE)
        self.assertNotIn("Example:", str(exc))
        self.assertEqual(
            exc.problem,
            "must name every covered hunk and what was tried on it; missing: H2, H4; "
            "needs at least 40 characters per hunk or file, 20 of them beyond generic praise; "
            "too little concrete detail: H1; "
            "must include a concrete detail (file:line, number/literal, ->, or symbol name): H3",
        )

    def test_field_error_within_preserves_example(self) -> None:
        nested = FieldError("note", "too short", example=COVERAGE_NOTE_EXAMPLE).within("entries[0]")

        self.assertEqual(nested.field, "entries[0].note")
        self.assertEqual(nested.example, COVERAGE_NOTE_EXAMPLE)

    def test_batch_rejected_tracks_entry_problems(self) -> None:
        with self.assertRaises(ValueError):
            BatchRejected("cover", (), 1)

        problem = EntryProblem(2, "intent", "note", "missing: H2", example=COVERAGE_NOTE_EXAMPLE)
        single = BatchRejected("cover", (problem,), 1)
        self.assertEqual(str(single), "cover: 1 of 1 entry rejected; nothing was recorded.")
        plural = BatchRejected("cover", (problem,), 3)
        self.assertEqual(str(plural), "cover: 1 of 3 entries rejected; nothing was recorded.")
        self.assertEqual((plural.command, plural.problems, plural.total), ("cover", (problem,), 3))

    def test_note_may_follow_a_hunk_id_with_an_arrow(self) -> None:
        note = "H1->L42 total([]) raises ZeroDivisionError at cart.py:9 for an empty cart"

        _, record = add_coverage(self.ledger, "intent", ["H1"], note)

        self.assertEqual(record.note, note)

    def test_short_note_names_the_per_hunk_minimum(self) -> None:
        with self.assertRaises(FieldError) as caught:
            add_coverage(self.ledger, "intent", ["H1"], "H1 looked at it")

        self.assertIn("needs at least 40 characters per hunk or file, 20 of them", str(caught.exception))

    def test_note_of_praise_needs_a_concrete_detail(self) -> None:
        note = "H1 looks good, clean, readable and consistent; reviewed and checked"

        with self.assertRaisesRegex(FieldError, "too little concrete detail"):
            add_coverage(self.ledger, "intent", ["H1"], note)

    def test_one_note_segment_may_not_describe_more_than_three_hunks(self) -> None:
        ledger = self.ledger
        segment = "parse_cfg: x=None -> L42 TypeError; callers cli.py:19 ok"
        ledger, _ = add_coverage(ledger, "intent", ["H1", "H2"], f"H1 {segment} H2 {segment}")

        with self.assertRaisesRegex(FieldError, "reuses the same note segment"):
            add_coverage(ledger, "intent", ["H3", "H4"], f"H3 {segment} H4 {segment}")

    def test_requires_a_round(self) -> None:
        with self.assertRaisesRegex(LedgerError, "craftsman review prepare"):
            add_coverage(Ledger(), "intent", ["H1"], coverage_note(["H1"]))

    def test_carried_hunks_cannot_be_covered_again(self) -> None:
        ledger = self.ledger
        for lens in ("intent", "craft"):
            ledger, _ = add_coverage(ledger, lens, ["H1"], coverage_note(["H1"]))
        snap = make_snap(
            *(make_hunk(f"H{index}", f"f{index}.py") for index in range(1, 11)), fingerprint="fp2"
        )
        ledger, _ = prepared(snap, ledger=ledger)

        with self.assertRaisesRegex(LedgerError, "H1 already covered in an earlier round"):
            add_coverage(ledger, "intent", ["H1"], coverage_note(["H1"]))


class DesignCoverageTests(unittest.TestCase):
    def setUp(self) -> None:
        snap = make_snap(make_hunk("H1", "a.py"), make_hunk("H2", "a.py", ("+y = 2",), new_start=9))
        self.ledger, _ = prepared(snap, lenses=("intent", "design"), tier="standard")

    def test_design_lens_covers_a_changed_file(self) -> None:
        ledger, record = add_coverage(
            self.ledger, "design", ["a.py"], "a.py total(): 2 branches -> L9 returns 0 for an empty cart"
        )

        self.assertEqual(uncovered_by_lens(ledger, ledger.rounds[-1])["design"], [])
        self.assertEqual(len(record.digests), 1)

    def test_design_lens_rejects_hunk_ids(self) -> None:
        with self.assertRaisesRegex(LedgerError, "unknown files in round 1: H1; files to cover: a.py"):
            add_coverage(self.ledger, "design", ["H1"], "H1 total(): 2 branches -> L9 returns 0")

    def test_design_lens_rejects_a_repeated_file(self) -> None:
        with self.assertRaisesRegex(LedgerError, "files must not repeat: a.py, a.py"):
            add_coverage(self.ledger, "design", ["a.py", "a.py"], "x")

    def test_note_naming_a_longer_path_does_not_cover_the_file(self) -> None:
        note = "src/a.py total(): 2 branches -> L9 returns 0 for an empty cart"

        with self.assertRaisesRegex(LedgerError, "missing: a.py"):
            add_coverage(self.ledger, "design", ["a.py"], note)

    def test_design_count_error_names_files(self) -> None:
        targets = ["a.py", "b.py", "c.py", "d.py", "e.py"]

        with self.assertRaises(LedgerError) as caught:
            add_coverage(self.ledger, "design", targets, "x")

        self.assertEqual(caught.exception.field, "files")
        self.assertIn("cover 1..4 files per call", str(caught.exception))


class CoverageAccountingTests(unittest.TestCase):
    def test_partially_covered_reopened_hunks_need_every_lens_again(self) -> None:
        caller = make_hunk("H1", "a.py", (" total = compute_total(x)", "+y = 1"))
        definer = make_hunk("H2", "b.py", ("+def compute_total(items):",))
        ledger, _ = prepared(make_snap(caller, definer))
        ledger, _ = add_coverage(ledger, "intent", ["H1", "H2"], coverage_note(["H1", "H2"]))
        ledger, _ = add_coverage(ledger, "craft", ["H2"], coverage_note(["H2"]))

        changed = make_hunk("H2", "b.py", ("+def compute_total(items, rate):",))
        snap = make_snap(caller, changed, fingerprint="fp2")
        ledger, round_ = prepared(snap, ledger=ledger, symbols={"H2": ["compute_total"]})

        self.assertEqual(
            uncovered_by_lens(ledger, round_), {"intent": ["H1", "H2"], "craft": ["H1", "H2"]}
        )

    def test_a_hunk_reopened_in_an_abandoned_round_keeps_needing_coverage(self) -> None:
        caller = make_hunk("H1", "a.py", (" total = compute_total(x)", "+y = 1"))
        ledger = fully_reviewed(make_snap(caller, make_hunk("H2", "b.py", ("+def compute_total(a):",))))
        changed = make_hunk("H2", "b.py", ("+def compute_total(a, b):",))
        symbols = {"H2": ["compute_total"]}
        ledger, _ = prepared(
            make_snap(caller, changed, fingerprint="fp2"), ledger=ledger, symbols=symbols
        )
        ledger, _ = add_coverage(ledger, "intent", ["H1"], coverage_note(["H1"]))

        snap = make_snap(caller, changed, make_hunk("H3", "c.py"), fingerprint="fp3")
        ledger, round_ = prepared(snap, ledger=ledger, symbols=symbols)

        self.assertFalse(round_.hunks[0].reopened)
        self.assertEqual(uncovered_by_lens(ledger, round_)["craft"], ["H1", "H2", "H3"])
        self.assertEqual(uncovered_by_lens(ledger, round_)["intent"], ["H2", "H3"])

    def test_sweep_cannot_be_recorded_before_the_other_lenses(self) -> None:
        ledger, _ = prepared(two_hunks(), lenses=("intent", "sweep"), tier="standard")

        with self.assertRaisesRegex(LedgerError, "still uncovered in round 1: intent H1,H2"):
            add_coverage(ledger, "sweep", ["H1", "H2"], coverage_note(["H1", "H2"]))

    def test_sweep_marks_uncovered_targets_from_rejected_entries(self) -> None:
        snap = make_snap(
            make_hunk("H1", "a.py"), make_hunk("H2", "b.py"), make_hunk("H3", "c.py")
        )
        ledger, _ = prepared(snap, lenses=("intent", "contracts", "craft", "sweep"), tier="standard")
        ledger, _ = add_coverage(ledger, "intent", ["H1"], coverage_note(["H1"]))
        rejected_by = {
            ("intent", "H2"): 2,
            ("intent", "H3"): 2,
            ("contracts", "H1"): 4,
            ("contracts", "H3"): 4,
        }

        with self.assertRaises(LedgerError) as caught:
            add_multi_coverage(
                ledger,
                ("sweep",),
                ["H1", "H2", "H3"],
                coverage_note(["H1", "H2", "H3"]),
                rejected_by=rejected_by,
            )

        self.assertIn(
            "still uncovered in round 1: "
            "intent H2,H3 (depends on entry 2); "
            "contracts H1,H3 (depends on entry 4); "
            "contracts H2; "
            "craft H1,H2,H3. Cover those first",
            str(caught.exception),
        )

    def test_coverage_units_map_open_hunks_or_files_best_effort(self) -> None:
        snap = make_snap(
            make_hunk("H1", "src/a.py"),
            make_hunk("H2", "src/a.py", ("+y = 2",), new_start=10),
            make_hunk("H3", "pkg1/u.py"),
            make_hunk("H4", "pkg2/u.py"),
        )
        _, std_round = prepared(
            snap, lenses=("intent", "design", "sweep"), tier="standard"
        )
        self.assertEqual(
            coverage_units_for_targets(std_round, ["intent", "sweep", "nope"], ["H1", "H99", "a.py"]),
            (("intent", "H1"),),
        )
        self.assertEqual(
            coverage_units_for_targets(std_round, ["design"], ["a.py", "u.py", "pkg1/u.py"]),
            (("design", "src/a.py"), ("design", "pkg1/u.py")),
        )

        _, quick_round = prepared(
            snap, lenses=("intent", "contracts", "failure"), tier="quick"
        )
        self.assertEqual(
            coverage_units_for_targets(quick_round, ["intent", "contracts"], ["a.py", "u.py"]),
            (
                ("intent", "H1"),
                ("intent", "H2"),
                ("contracts", "H1"),
                ("contracts", "H2"),
            ),
        )

    def test_a_tier_upgrade_keeps_lens_coverage_but_needs_a_fresh_sweep(self) -> None:
        ledger, round_ = prepared(two_hunks(), ("intent", "craft", "sweep"), "deep", swept(two_hunks()))

        self.assertEqual(
            uncovered_by_lens(ledger, round_),
            {"intent": [], "craft": ["H1", "H2"], "sweep": ["H1", "H2"]},
        )

    def test_a_premature_sweep_is_never_carried_into_a_later_round(self) -> None:
        h1 = two_hunks().hunks[0]
        ledger, round_ = prepared(make_snap(h1), lenses=("intent", "sweep"), tier="standard")
        start = round_.prepared_at
        coverage = (
            coverage_record(1, "sweep", [h1.digest], shifted(start, 1)),
            coverage_record(1, "intent", [h1.digest], shifted(start, 2)),
        )

        _, later = prepared(
            two_hunks("fp2"), ("intent", "sweep"), "standard", replace(ledger, coverage=coverage)
        )

        self.assertEqual([hunk.carried for hunk in later.hunks], [False, False])


class FindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ledger, _ = prepared(make_snap(make_hunk("H1")), signals=(LONG_FUNCTION,))

    def test_findings_get_sequential_ids(self) -> None:
        ledger = record_finding(record_finding(self.ledger))

        self.assertEqual([finding.id for finding in ledger.findings], ["F1", "F2"])

    def test_finding_text_is_trimmed(self) -> None:
        ledger = record_finding(self.ledger, title="  Empty list crashes total()  ")

        self.assertEqual(ledger.findings[0].title, "Empty list crashes total()")

    def test_invalid_findings_are_rejected(self) -> None:
        cases: list[tuple[dict[str, Any], str]] = [
            ({"title": "Too sho"}, "title must have 8..160 characters"),
            ({"title": "x" * 161}, "title must have 8..160 characters"),
            ({"scenario": "It breaks."}, "scenario must have at least 20 characters"),
            ({"line": 0}, "line must be >= 1"),
            ({"severity": "critical"}, "severity must be one of blocker, major, minor, nit"),
            ({"lens": "vibes"}, "lens must be one of"),
        ]
        for change, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(LedgerError, message):
                record_finding(self.ledger, **change)

    def test_finding_may_resolve_a_signal_of_the_round(self) -> None:
        ledger = record_finding(self.ledger, signal_id="S1")

        self.assertEqual(ledger.findings[0].signal_id, "S1")

    def test_finding_for_an_unknown_signal_is_rejected(self) -> None:
        with self.assertRaisesRegex(LedgerError, "unknown quality signal 'S9' in round 1; known: S1"):
            record_finding(self.ledger, signal_id="S9")

    def test_requires_a_round(self) -> None:
        with self.assertRaisesRegex(LedgerError, "craftsman review prepare"):
            record_finding(Ledger())


class VerdictTests(unittest.TestCase):
    def setUp(self) -> None:
        ledger, _ = prepared(make_snap(make_hunk("H1")))
        self.ledger = record_run(
            record_finding(ledger), ("pytest", "-k", "empty"), kind="repro", exit_code=1
        )

    def test_proven_verdict_cites_a_run(self) -> None:
        _, finding = set_verdict(self.ledger, "F1", "proven", EVIDENCE, "R1")

        self.assertEqual((finding.verdict, finding.verdict_run), ("proven", "R1"))

    def test_proven_verdict_without_a_run_names_the_run_flag(self) -> None:
        with self.assertRaises(FieldError) as caught:
            set_verdict(self.ledger, "F1", "proven", EVIDENCE, None)

        self.assertEqual(caught.exception.field, "verdict_run")

    def test_verdict_citing_an_unknown_run_is_rejected(self) -> None:
        with self.assertRaisesRegex(LedgerError, "unknown run 'R7'; recorded runs: R1"):
            set_verdict(self.ledger, "F1", "proven", EVIDENCE, "R7")

    def test_a_trivial_run_is_not_verdict_evidence(self) -> None:
        ledger = record_run(self.ledger, ("true",), kind="repro")

        with self.assertRaises(FieldError) as caught:
            set_verdict(ledger, "F1", "proven", EVIDENCE, "R2")

        self.assertIn("R2 (`true`) is trivial", str(caught.exception))

    def test_probable_verdict_accepts_both_evidence_prefixes(self) -> None:
        for evidence in (
            "static: app.py:3 divides by len(items) which is 0 for []",
            "no-repro: needs Windows NTFS; path check fails on case collision",
            f"  NO-REPRO: {PROBABLE[10:]}",
        ):
            with self.subTest(evidence=evidence):
                _, finding = set_verdict(self.ledger, "F1", "probable", evidence, None)
                self.assertEqual(finding.verdict, "probable")

    def test_insufficient_evidence_is_rejected_with_an_example(self) -> None:
        cases = [
            ("probable", "The chain from api.py:40 to db.py:88 crashes.", "must start with 'no-repro:'"),
            ("probable", "no-repro: needs a database", "followed by at least 20 characters"),
            ("probable", "static: divides by len(items) which is 0 always", "cite file:line"),
            (
                "question",
                "app.py:3 behavior was requested by the task text",
                "must start with 'intent:'",
            ),
            ("question", "intent: the task asks for retries and this differs", "citing file:line"),
            ("dismissed", "I do not think this can happen at all.", "must cite the guard as file:line"),
            ("dismissed", "the ratio 3:4 is always respected here", "must cite the guard as file:line"),
        ]
        for verdict, evidence, message in cases:
            with self.subTest(verdict=verdict, evidence=evidence):
                with self.assertRaisesRegex(LedgerError, message) as caught:
                    set_verdict(self.ledger, "F1", verdict, evidence, None)  # type: ignore[arg-type]
                self.assertIn("e.g.", str(caught.exception))

    def test_dismissal_may_cite_the_guard_or_a_run(self) -> None:
        for evidence, run in (
            ("The repro run prints the right total.", "R1"),
            ("see src\\app.py:12 for the guard", None),
            ("guarded by the check in Makefile:12 before deploy", None),
            ("the Dockerfile:3 base image pins the version", None),
            ("bin/tool:12 rejects an empty list first", None),
            ("the justfile:4 recipe sets the flag already", None),
        ):
            with self.subTest(evidence=evidence):
                _, finding = set_verdict(self.ledger, "F1", "dismissed", evidence, run)
                self.assertEqual(finding.verdict, "dismissed")

    def test_a_blocker_cannot_be_a_design_question(self) -> None:
        ledger, _ = prepared(make_snap(make_hunk("H1")))
        ledger = record_finding(ledger, "blocker")
        evidence = "intent: the task asks for this; app.py:3 implements it"

        with self.assertRaisesRegex(LedgerError, "a blocker is a defect, not a design question"):
            set_verdict(ledger, "F1", "question", evidence, None)

    def test_unknown_finding_lists_the_known_ones(self) -> None:
        with self.assertRaisesRegex(LedgerError, "unknown finding 'F9'; known findings: F1"):
            set_verdict(self.ledger, "F9", "probable", PROBABLE, None)

    def test_changing_the_verdict_to_dismissed_drops_the_resolution(self) -> None:
        ledger, _ = set_verdict(self.ledger, "F1", "probable", PROBABLE, None)
        ledger, _ = resolve(
            ledger, "F1", "fixed", "Guarded the empty list in total().", fingerprint="fp2"
        )

        _, finding = set_verdict(ledger, "F1", "dismissed", "False alarm: see app.py:3 guard.", None)

        self.assertEqual(
            (finding.verdict, finding.resolution, finding.resolution_note), ("dismissed", None, "")
        )


class ResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        ledger, _ = prepared(make_snap(make_hunk("H1")))
        self.ledger = record_finding(ledger)
        self.probable, _ = set_verdict(self.ledger, "F1", "probable", PROBABLE, None)

    def test_finding_without_a_verdict_cannot_be_resolved(self) -> None:
        with self.assertRaisesRegex(LedgerError, "has no verdict yet"):
            resolve(self.ledger, "F1", "fixed", "Guarded the empty list in total().", fingerprint="fp2")

    def test_dismissed_finding_cannot_be_resolved(self) -> None:
        dismissed, _ = set_verdict(self.ledger, "F1", "dismissed", DISMISSED, None)

        with self.assertRaisesRegex(LedgerError, "was dismissed"):
            resolve(dismissed, "F1", "accepted", "Out of scope for this change.", fingerprint="fp2")

    def test_design_question_cannot_be_resolved(self) -> None:
        evidence = "intent: the task asks for retries; app.py:3 retries forever instead"
        question, _ = set_verdict(self.ledger, "F1", "question", evidence, None)

        with self.assertRaisesRegex(LedgerError, "is a design question"):
            resolve(question, "F1", "accepted", "Requested by the maintainer.", fingerprint="fp2")

    def test_fixed_requires_changed_code(self) -> None:
        with self.assertRaisesRegex(LedgerError, "fix the code first, then resolve"):
            resolve(
                self.probable, "F1", "fixed", "Guarded the empty list in total().", fingerprint="fp1"
            )

    def test_resolution_note_needs_substance(self) -> None:
        with self.assertRaisesRegex(LedgerError, "resolution_note must have at least 20 characters"):
            resolve(self.probable, "F1", "accepted", "meh", fingerprint="fp1")

    def test_confirmed_finding_is_fixed_after_the_code_changed(self) -> None:
        _, fixed = resolve(
            self.probable, "F1", "fixed", "Guarded the empty list in total().", fingerprint="fp2"
        )

        self.assertEqual(fixed.resolution, "fixed")

    def test_blockers_cannot_be_accepted(self) -> None:
        ledger, _ = prepared(make_snap(make_hunk("H1")))
        ledger, _ = set_verdict(record_finding(ledger, "blocker"), "F1", "probable", PROBABLE, None)

        with self.assertRaisesRegex(LedgerError, "'accepted' is not allowed for blocker findings"):
            resolve(ledger, "F1", "accepted", "We will live with this for now.", fingerprint="fp2")


class MergeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ledger, _ = prepared(make_snap(make_hunk("H1")), signals=(LONG_FUNCTION, TEST_NAME))

    def finding(self, ledger: Ledger, scenario: str, **overrides: Any) -> Ledger:
        return record_finding(ledger, scenario=scenario, **overrides)

    def test_duplicates_are_dismissed_into_the_target(self) -> None:
        ledger = self.finding(self.ledger, "Calling total([]) raises ZeroDivisionError.")
        ledger = self.finding(ledger, "An empty cart shows an error page.", lens="contracts")

        _, target, (dup,) = merge_findings(ledger, "F1", ["F2"])

        self.assertEqual(dup.verdict, "dismissed")
        self.assertEqual(dup.verdict_evidence, "Grouped into F1 (app.py:3): same root cause.")
        self.assertIn("[F2/contracts] An empty cart shows an error page.", target.scenario)

    def test_target_takes_the_most_severe_severity(self) -> None:
        ledger = self.finding(
            self.ledger, "Calling total([]) raises ZeroDivisionError.", severity="minor"
        )
        ledger = self.finding(ledger, "An empty cart shows an error page.", severity="blocker")

        _, target, _ = merge_findings(ledger, "F1", ["F2"])

        self.assertEqual(target.severity, "blocker")

    def test_a_consequence_already_described_is_not_repeated(self) -> None:
        shared = "Totals are wrong; the receipt shows NaN for an empty cart."
        ledger = self.finding(self.ledger, "Calling total([]) raises ZeroDivisionError.")
        ledger = self.finding(ledger, shared)
        ledger = self.finding(ledger, shared)
        ledger, _, _ = merge_findings(ledger, "F1", ["F2"])

        _, target, _ = merge_findings(ledger, "F1", ["F3"])

        self.assertEqual(target.scenario.count(shared), 1)

    def test_target_resolves_the_signal_of_a_duplicate(self) -> None:
        ledger = self.finding(self.ledger, "Calling total([]) raises ZeroDivisionError.")
        ledger = self.finding(ledger, "total() mixes parsing and summing.", signal_id="S1")

        _, target, _ = merge_findings(ledger, "F1", ["F2"])

        self.assertEqual(target.signal_id, "S1")

    def test_signal_of_another_round_is_not_merged(self) -> None:
        ledger = self.finding(self.ledger, "Calling total([]) raises ZeroDivisionError.")
        ledger, _ = prepared(
            make_snap(make_hunk("H1"), fingerprint="fp2"), ledger=ledger, signals=(TEST_NAME,)
        )
        ledger = self.finding(ledger, "The test name packs two behaviors.", signal_id="S2")

        with self.assertRaisesRegex(LedgerError, "F2 resolve a quality signal of another round"):
            merge_findings(ledger, "F1", ["F2"])

    def test_findings_about_different_signals_are_not_merged(self) -> None:
        ledger = self.finding(self.ledger, "total() mixes parsing and summing.", signal_id="S1")
        ledger = self.finding(ledger, "The test name packs two behaviors.", signal_id="S2")

        with self.assertRaisesRegex(LedgerError, "different quality signals \\(S1, S2\\)"):
            merge_findings(ledger, "F1", ["F2"])

    def test_judged_findings_are_not_merged(self) -> None:
        ledger = self.finding(self.ledger, "Calling total([]) raises ZeroDivisionError.")
        ledger = self.finding(ledger, "An empty cart shows an error page.")
        for judged in ("F1", "F2"):
            with self.subTest(judged=judged):
                verdict, _ = set_verdict(ledger, judged, "probable", PROBABLE, None)
                with self.assertRaisesRegex(LedgerError, f"{judged} already has the verdict probable"):
                    merge_findings(verdict, "F1", ["F2"])

    def test_invalid_merge_requests_are_rejected(self) -> None:
        ledger = self.finding(self.ledger, "Calling total([]) raises ZeroDivisionError.")
        ledger = self.finding(ledger, "An empty cart shows an error page.")
        cases = [
            ("F1", ["F1"], "cannot merge F1 into itself"),
            ("F1", ["F2", "F2"], "must not repeat"),
            ("F1", ["F99"], "unknown finding 'F99'"),
            ("F1", [], "name at least one duplicate"),
        ]
        for target, duplicates, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(LedgerError, message):
                merge_findings(ledger, target, duplicates)


class SignalAckTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ledger, _ = prepared(make_snap(make_hunk("H1")), signals=(LONG_FUNCTION, DIFF_SIZE))

    def test_ack_records_the_reason_for_the_round(self) -> None:
        _, ack = ack_signal(self.ledger, "S1", f"  {CONCRETE_REASON}  ")

        self.assertEqual((ack.signal_id, ack.round, ack.reason), ("S1", 1, CONCRETE_REASON))

    def test_acking_again_replaces_the_earlier_ack(self) -> None:
        ledger, _ = ack_signal(self.ledger, "S1", CONCRETE_REASON)
        ledger, _ = ack_signal(ledger, "S1", CONCRETE_REASON + " (see a.py:9)")

        self.assertEqual(len(ledger.signal_acks), 1)

    def test_unknown_signal_is_rejected(self) -> None:
        with self.assertRaisesRegex(LedgerError, "unknown quality signal 'S9'"):
            ack_signal(self.ledger, "S9", CONCRETE_REASON)

    def test_diff_size_cannot_be_acknowledged(self) -> None:
        with self.assertRaisesRegex(LedgerError, "S3 \\(`diff-size`\\) cannot be acknowledged"):
            ack_signal(self.ledger, "S3", CONCRETE_REASON)

    def test_short_reason_is_rejected(self) -> None:
        with self.assertRaisesRegex(FieldError, "reason must have at least 40 characters"):
            ack_signal(self.ledger, "S1", "a.py:1 is fine")

    def test_reason_without_a_code_detail_is_rejected(self) -> None:
        with self.assertRaisesRegex(FieldError, "must include a concrete code detail"):
            ack_signal(self.ledger, "S1", "this function is long but it is easy to read as it is")


class FactTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ledger, _ = prepared(make_snap(make_hunk("H1"), make_hunk("H2", "b.py")))
        self.source = SourceRef("app.py", 1)
        self.text = "total() returns 0 for an empty list at app.py:1"

    def record(self, ledger: Ledger, hunks: list[str], digest: str = "d1") -> Ledger:
        ledger, _ = add_fact(
            ledger,
            hunks=hunks,
            source=self.source,
            text=self.text,
            source_digest=digest,
            author_lens="contracts",
        )
        return ledger

    def test_facts_get_sequential_ids(self) -> None:
        ledger = self.record(self.record(self.ledger, ["H1"]), ["H2"])

        self.assertEqual([fact.id for fact in ledger.facts], ["F-1", "F-2"])

    def test_fact_about_an_unknown_hunk_is_rejected(self) -> None:
        with self.assertRaisesRegex(FieldError, "hunks are not in round 1: H9"):
            self.record(self.ledger, ["H9"])

    def test_fact_with_a_judgement_is_rejected(self) -> None:
        with self.assertRaisesRegex(FieldError, "without judgement words \\('bug' found"):
            add_fact(
                self.ledger,
                hunks=["H1"],
                source=self.source,
                text="total() has a bug when the list at app.py:1 is empty",
                source_digest="d1",
                author_lens="contracts",
            )

    def test_fact_is_valid_while_its_source_file_is_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "app.py").write_text("x = 1\n", encoding="utf-8")
            digest = file_sha256(root, "app.py")
            assert digest is not None
            ledger = self.record(self.ledger, ["H1"], digest)
            self.assertEqual(list(valid_facts_for_hunk(ledger, root)), ["H1"])

            (root / "app.py").write_text("x = 2\n", encoding="utf-8")

            self.assertEqual(valid_facts_for_hunk(ledger, root), {})


class RunTests(unittest.TestCase):
    def test_runs_get_sequential_ids(self) -> None:
        ledger = record_run(record_run(Ledger()))

        self.assertEqual([run.id for run in ledger.runs], ["R1", "R2"])

    def test_passing_run_cannot_be_waived(self) -> None:
        with self.assertRaisesRegex(LedgerError, "R1 passed"):
            waive_run(record_run(Ledger()), "R1", "The command was replaced by another one.")

    def test_waiver_needs_a_reason(self) -> None:
        ledger = record_run(Ledger(), exit_code=1)

        with self.assertRaisesRegex(LedgerError, "reason must have at least 20 characters"):
            waive_run(ledger, "R1", "typo")

    def test_run_is_waived_only_once(self) -> None:
        ledger, _ = waive_run(record_run(Ledger(), exit_code=1), "R1", "Typo in the test path.")

        with self.assertRaisesRegex(LedgerError, "R1 is already waived"):
            waive_run(ledger, "R1", "Typo in the test path.")


class PersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "craftsman" / LEDGER_FILENAME

    def test_missing_file_loads_as_empty(self) -> None:
        self.assertEqual(load(self.path), Ledger())

    def test_save_round_trips_without_leaving_temporary_files(self) -> None:
        ledger = cover_open_hunks(fully_reviewed(make_snap(make_hunk("H1"))), "intent")

        save(self.path, ledger)

        self.assertEqual(load(self.path), ledger)
        self.assertEqual([p.name for p in self.path.parent.iterdir()], [LEDGER_FILENAME])

    def test_corrupt_json_raises(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{not json", encoding="utf-8")
        with self.assertRaisesRegex(LedgerError, "is not valid JSON"):
            load(self.path)

    def test_mutate_persists_the_change_it_returns(self) -> None:
        number = mutate(self.path, lambda ledger: (record_run(ledger), len(ledger.runs) + 1))
        unchanged = mutate(self.path, lambda ledger: (ledger, len(ledger.runs)))

        self.assertEqual((number, unchanged), (1, 1))
        self.assertEqual([run.id for run in load(self.path).runs], ["R1"])

    def test_second_lock_times_out(self) -> None:
        with locked(self.path):
            with self.assertRaisesRegex(LedgerError, "ledger.lock"), locked(self.path, timeout_s=0.2):
                pass
        with locked(self.path, timeout_s=0.2):
            pass

    def test_the_lock_of_a_process_that_died_is_released(self) -> None:
        code = (
            "import sys, time; from pathlib import Path; from craftsman.ledger import locked\n"
            "with locked(Path(sys.argv[1])):\n"
            "    print('locked', flush=True)\n"
            "    time.sleep(60)\n"
        )
        env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parent.parent)}
        holder = subprocess.Popen(
            [sys.executable, "-c", code, str(self.path)], stdout=subprocess.PIPE, text=True, env=env
        )
        try:
            assert holder.stdout is not None
            self.assertEqual(holder.stdout.readline().strip(), "locked")
            with self.assertRaisesRegex(LedgerError, "timed out"), locked(self.path, timeout_s=0.2):
                pass
        finally:
            holder.kill()
            holder.wait(timeout=30)
            if holder.stdout is not None:
                holder.stdout.close()
        with locked(self.path, timeout_s=5.0):
            pass

    def test_an_interrupt_while_acquiring_does_not_leak_the_lock(self) -> None:
        # No public path delivers an interrupt between taking the OS lock and entering the block.
        acquire = ledger_module._try_lock

        def interrupted(fd: int) -> bool:
            acquire(fd)
            raise KeyboardInterrupt

        with (
            mock.patch.object(ledger_module, "_try_lock", interrupted),
            self.assertRaises(KeyboardInterrupt),
            locked(self.path),
        ):
            pass
        with locked(self.path, timeout_s=0.2):
            pass

    def test_ledger_path_follows_the_git_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = EvidenceRepo(Path(tmp).resolve())
            (repo.root / "sub").mkdir()
            expected = repo.root / ".git" / "craftsman" / LEDGER_FILENAME
            self.assertEqual(ledger_path_for(repo.root / "sub"), expected)

    def test_ledger_path_outside_a_repository_is_an_error(self) -> None:
        with (
            tempfile.TemporaryDirectory() as outside,
            self.assertRaisesRegex(LedgerError, "cannot locate"),
        ):
            ledger_path_for(outside)


if __name__ == "__main__":
    unittest.main()
