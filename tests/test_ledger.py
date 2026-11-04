"""Tests for the ledger operations, persistence and locking."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Iterator
from contextlib import contextmanager, redirect_stderr
from pathlib import Path
from typing import Any
from unittest import mock

from craftsman import ledger as ledger_module
from craftsman.ledger import (
    add_coverage,
    covered_digests,
    ledger_path_for,
    load,
    locked,
    merge_findings,
    mutate,
    plan_coverage,
    reopened_mentions,
    resolve,
    save,
    set_verdict,
    stable_hunk_ids,
    uncovered_hunks,
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
)

from tests.support import (
    EvidenceRepo,
    cover_open_hunks,
    coverage_note,
    fully_reviewed,
    make_hunk,
    make_snap,
    prepared,
    record_finding,
    record_run,
    two_hunks,
)

EVIDENCE = "Reproduced via tests/test_app.py::test_empty (fails before fix)."
PROBABLE = "no-repro: needs a live database; app.py:3 divides by len(items) == 0"
DISMISSED = "guarded by app.py:2, which returns 0 for an empty list"
LONG_FUNCTION = QualitySignal("S1", SignalKind.LONG_FUNCTION, "a.py", 1, "`f` spans 50 lines")


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

    def test_asking_for_deep_raises_the_round_of_an_unchanged_diff_in_place(self) -> None:
        snap = make_snap(make_hunk("H1"))
        ledger, _ = prepared(snap)
        ledger = cover_open_hunks(ledger)

        raised, round_ = prepared(snap, deep=True, ledger=ledger)

        self.assertEqual((round_.number, round_.deep, len(raised.rounds)), (1, True, 1))
        self.assertEqual(raised.coverage, ledger.coverage)
        self.assertEqual(prepared(snap, deep=False, ledger=raised)[1], round_)

    def test_a_changed_base_requires_a_reset(self) -> None:
        ledger, _ = prepared(make_snap(make_hunk("H1")))
        with self.assertRaisesRegex(LedgerError, "craftsman review reset"):
            prepared(make_snap(make_hunk("H1"), base="c" * 40, fingerprint="fp2"), ledger=ledger)

    def test_hunks_covered_earlier_are_carried(self) -> None:
        h1, h2 = make_hunk("H1", "a.py"), make_hunk("H2", "b.py")
        ledger, _ = prepared(make_snap(h1, h2))
        ledger, _ = add_coverage(ledger, ["H1", "H2"], coverage_note(["H1", "H2"]))

        h3 = make_hunk("H3", "c.py")
        _, round_ = prepared(make_snap(h1, h2, h3, fingerprint="fp2"), ledger=ledger)

        self.assertEqual([hunk.carried for hunk in round_.hunks], [True, True, False])

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
        self.ledger, self.round = prepared(make_snap(*hunks))

    def test_records_digests_against_the_latest_round(self) -> None:
        note = coverage_note(["H2", "H1"])
        ledger, record = add_coverage(self.ledger, ["H2", "H1"], f"  {note}  ")

        self.assertEqual(record.round, 1)
        self.assertEqual(record.digests, (self.round.hunks[1].digest, self.round.hunks[0].digest))
        self.assertEqual(record.note, note)
        self.assertEqual(ledger.coverage, (record,))

    def test_there_is_no_batch_size_limit(self) -> None:
        ids = [f"H{index}" for index in range(1, 11)]

        ledger, record = add_coverage(self.ledger, ids, "walked every hunk of the slice end to end")

        self.assertEqual(len(record.digests), 10)
        self.assertEqual(uncovered_hunks(ledger, ledger.rounds[-1]), [])

    def test_files_cover_every_open_hunk_of_those_files(self) -> None:
        snap = make_snap(
            make_hunk("H1", "a.py"), make_hunk("H2", "a.py", ("+y = 2",), new_start=9), make_hunk("H3", "b.py")
        )
        ledger, _ = prepared(snap)

        ledger, record = add_coverage(ledger, ["a.py"], "read a.py and its callers", target_kind="files")

        self.assertEqual(len(record.digests), 2)
        self.assertEqual(uncovered_hunks(ledger, ledger.rounds[-1]), ["H3"])

    def test_rejections_explain_what_is_valid(self) -> None:
        note = "examined the slice and tried None"
        cases = [
            ([], note, "hunks", "name at least one of the hunks to cover"),
            (["H42"], note, "hunks", "unknown hunk ids in round 1: H42"),
            (["nope.py"], note, "files", "unknown files in round 1: nope.py; changed files: f1.py"),
            (["H1"], "too short", "hunks", "note must have at least 20 characters"),
        ]
        for ids, text, kind, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(LedgerError, message):
                add_coverage(self.ledger, ids, text, target_kind=kind)  # type: ignore[arg-type]

    def test_repeated_targets_are_recorded_once(self) -> None:
        _, record = add_coverage(self.ledger, ["H1", "H1"], coverage_note(["H1"]))

        self.assertEqual(len(record.digests), 1)

    def test_batch_rejected_tracks_entry_problems(self) -> None:
        with self.assertRaises(ValueError):
            BatchRejected("cover", (), 1)

        problem = EntryProblem(2, "H2", "note", "must have at least 20 characters")
        single = BatchRejected("cover", (problem,), 1)
        self.assertEqual(str(single), "cover: 1 of 1 entry rejected; nothing was recorded.")
        plural = BatchRejected("cover", (problem,), 3)
        self.assertEqual(str(plural), "cover: 1 of 3 entries rejected; nothing was recorded.")
        self.assertEqual((plural.command, plural.problems, plural.total), ("cover", (problem,), 3))

    def test_requires_a_round(self) -> None:
        with self.assertRaisesRegex(LedgerError, "craftsman review prepare"):
            add_coverage(Ledger(), ["H1"], coverage_note(["H1"]))

    def test_carried_hunks_cannot_be_covered_again(self) -> None:
        ledger, _ = add_coverage(self.ledger, ["H1"], coverage_note(["H1"]))
        snap = make_snap(
            *(make_hunk(f"H{index}", f"f{index}.py") for index in range(1, 11)), fingerprint="fp2"
        )
        ledger, _ = prepared(snap, ledger=ledger)

        with self.assertRaisesRegex(LedgerError, "H1 already covered in an earlier round"):
            add_coverage(ledger, ["H1"], coverage_note(["H1"]))
        with self.assertRaisesRegex(LedgerError, "f1.py already covered in an earlier round"):
            add_coverage(ledger, ["f1.py"], coverage_note(["H1"]), target_kind="files")


class StableHunkIdTests(unittest.TestCase):
    def test_an_unchanged_hunk_keeps_its_id_and_a_new_one_never_reuses_an_old_id(self) -> None:
        ledger, _ = prepared(make_snap(make_hunk("H1", "a.py"), make_hunk("H2", "b.py")))
        rewritten = make_snap(
            make_hunk("H1", "b.py"), make_hunk("H2", "c.py", ("+z = 3",)), fingerprint="fp2"
        )

        stable = stable_hunk_ids(ledger, rewritten)

        self.assertEqual([(h.path, h.id) for h in stable.hunks], [("b.py", "H2"), ("c.py", "H3")])

    def test_a_first_round_is_numbered_in_order(self) -> None:
        snap = make_snap(make_hunk("H7", "a.py"), make_hunk("H8", "b.py"))

        self.assertEqual([h.id for h in stable_hunk_ids(Ledger(), snap).hunks], ["H1", "H2"])

    def test_plan_coverage_splits_open_and_carried_hunks(self) -> None:
        ledger, _ = prepared(make_snap(make_hunk("H1", "a.py"), make_hunk("H2", "b.py")))
        ledger = cover_open_hunks(ledger)
        ledger, round_ = prepared(
            make_snap(make_hunk("H1", "a.py"), make_hunk("H2", "b.py", ("+y",)), fingerprint="fp2"),
            ledger=ledger,
        )

        self.assertEqual(plan_coverage(round_, ["H1", "H2"]), (("H2",), ("H1",)))
        self.assertEqual(plan_coverage(round_, ["a.py"], target_kind="files"), ((), ("H1",)))
        with self.assertRaisesRegex(LedgerError, "unknown hunk ids"):
            plan_coverage(round_, ["H9"])


class CoverageAccountingTests(unittest.TestCase):
    def test_reopened_hunks_need_coverage_again(self) -> None:
        caller = make_hunk("H1", "a.py", (" total = compute_total(x)", "+y = 1"))
        definer = make_hunk("H2", "b.py", ("+def compute_total(items):",))
        ledger, _ = prepared(make_snap(caller, definer))
        ledger, _ = add_coverage(ledger, ["H1", "H2"], coverage_note(["H1", "H2"]))

        changed = make_hunk("H2", "b.py", ("+def compute_total(items, rate):",))
        snap = make_snap(caller, changed, fingerprint="fp2")
        ledger, round_ = prepared(snap, ledger=ledger, symbols={"H2": ["compute_total"]})

        self.assertEqual(uncovered_hunks(ledger, round_), ["H1", "H2"])

    def test_a_hunk_reopened_in_an_abandoned_round_keeps_needing_coverage(self) -> None:
        caller = make_hunk("H1", "a.py", (" total = compute_total(x)", "+y = 1"))
        ledger = fully_reviewed(make_snap(caller, make_hunk("H2", "b.py", ("+def compute_total(a):",))))
        changed = make_hunk("H2", "b.py", ("+def compute_total(a, b):",))
        symbols = {"H2": ["compute_total"]}
        ledger, _ = prepared(
            make_snap(caller, changed, fingerprint="fp2"), ledger=ledger, symbols=symbols
        )

        snap = make_snap(caller, changed, make_hunk("H3", "c.py"), fingerprint="fp3")
        ledger, round_ = prepared(snap, ledger=ledger, symbols=symbols)

        self.assertFalse(round_.hunks[0].reopened)
        self.assertEqual(uncovered_hunks(ledger, round_), ["H1", "H2", "H3"])

    def test_coverage_of_an_earlier_round_counts_for_the_same_digest(self) -> None:
        ledger, round_ = prepared(two_hunks())
        ledger = cover_open_hunks(ledger)

        self.assertEqual(uncovered_hunks(ledger, round_), [])
        self.assertEqual(covered_digests(ledger, 1), {hunk.digest for hunk in round_.hunks})


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
        self.ledger, _ = prepared(make_snap(make_hunk("H1")), signals=(LONG_FUNCTION,))

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
        ledger = cover_open_hunks(fully_reviewed(make_snap(make_hunk("H1"))))

        save(self.path, ledger)

        self.assertEqual(load(self.path), ledger)
        self.assertEqual([p.name for p in self.path.parent.iterdir()], [LEDGER_FILENAME])

    def test_an_older_ledger_is_set_aside_under_the_lock(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({**Ledger().to_json(), "schema": 2}), encoding="utf-8")
        taken: list[bool] = []
        real = ledger_module.locked

        @contextmanager
        def watching(path: Path) -> Iterator[None]:
            with real(path):
                taken.append(True)
                yield

        with mock.patch.object(ledger_module, "locked", watching), redirect_stderr(io.StringIO()):
            self.assertEqual(load(self.path), Ledger())

        self.assertEqual(taken, [True])
        self.assertTrue(self.path.with_name("ledger.v2.bak.json").is_file())

    def test_a_ledger_written_while_the_old_one_was_set_aside_is_left_alone(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({**Ledger().to_json(), "schema": 2}), encoding="utf-8")
        newer = cover_open_hunks(fully_reviewed(make_snap(make_hunk("H1"))))

        @contextmanager
        def replaced_first(path: Path) -> Iterator[None]:
            save(path, newer)  # a concurrent craftsman wrote its ledger before this one got the lock
            yield

        with mock.patch.object(ledger_module, "locked_unless_held", replaced_first):
            loaded = load(self.path)

        self.assertEqual(loaded, newer)
        self.assertEqual(load(self.path), newer)
        self.assertFalse(self.path.with_name("ledger.v2.bak.json").exists())

    def test_setting_an_older_ledger_aside_inside_mutate_does_not_deadlock(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({**Ledger().to_json(), "schema": 2}), encoding="utf-8")

        with redirect_stderr(io.StringIO()):
            number = mutate(self.path, lambda ledger: (record_run(ledger), len(ledger.runs) + 1))

        self.assertEqual(number, 1)

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
