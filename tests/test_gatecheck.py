"""Tests for the gate's evaluation of the evidence ledger and its report."""

from __future__ import annotations

import tempfile
import unittest
from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from craftsman.diffscan import DiffSnapshot, snapshot
from craftsman.gate import MAX_CONTINUATION_REASON_CHARS, continuation_reason
from craftsman.gatecheck import (
    MAX_HUNT_ISSUE_CHARS,
    MAX_ROUNDS,
    RUN_STALE_AFTER,
    GateIssue,
    GatePolicy,
    GateReport,
    SlotStatus,
    evaluate,
    format_blocker_summary,
    passing_runs,
    render_next_actions,
    render_report,
    slot_statuses,
    verifier_clusters,
)
from craftsman.hooks import REVIEW_GUARD_ACTIONS
from craftsman.huntplan import plan_due_waves, required_slots
from craftsman.ledger import (
    add_agent_event,
    add_coverage,
    resolve,
    set_verdict,
    waive_run,
)
from craftsman.records import (
    AgentEvent,
    Coverage,
    Ledger,
    QualitySignal,
    SignalKind,
    Slot,
    SlotClaim,
    VerdictEvent,
    parse_timestamp,
)

from tests.support import (
    BASE_SHA,
    EvidenceRepo,
    at,
    cover_open_hunks,
    coverage_note,
    fully_reviewed,
    make_hunk,
    make_slot,
    make_snap,
    planned,
    prepared,
    record_finding,
    record_run,
    shifted,
    two_hunks,
)
from tests.support import agent_event as timed_event

EVIDENCE = "Reproduced via tests/test_app.py::test_empty (fails before fix)."
PROBABLE = "no-repro: needs a live database; app.py:3 divides by len(items) == 0"
DISMISSED = "guarded by app.py:2, which returns 0 for an empty list"
POLICY = GatePolicy()
DEEP = GatePolicy(deep=True)
ISOLATED = GatePolicy(isolation_evidence="hooks")
LONG_FUNCTION = QualitySignal("S1", SignalKind.LONG_FUNCTION, "a.py", 1, "`f` spans 50 lines")
DIFF_SIZE = QualitySignal("S2", SignalKind.DIFF_SIZE, "a.py", 1, "+900 production lines added")


def codes(report: GateReport) -> list[str]:
    return [issue.code for issue in report.issues]


def stop_event(agent: str, at: str, event: str = "stop") -> AgentEvent:
    return AgentEvent(platform="copilot", event=event, agent=agent, agent_id="x", at=at)  # type: ignore[arg-type]


class RoundTests(unittest.TestCase):
    def test_fully_reviewed_change_passes(self) -> None:
        report = evaluate(fully_reviewed(two_hunks()), two_hunks(), POLICY)

        self.assertTrue(report.passed, render_report(report))
        self.assertEqual(report.warnings, ())

    def test_empty_snapshot_passes_without_a_round(self) -> None:
        report = evaluate(Ledger(), make_snap(fingerprint="empty"), POLICY)

        self.assertTrue(report.passed)
        self.assertIn("Nothing to review", report.summary)

    def test_empty_snapshot_still_blocks_on_confirmed_findings(self) -> None:
        ledger, _ = prepared(two_hunks())
        ledger = record_run(record_finding(ledger), kind="repro", exit_code=1)
        ledger, _ = set_verdict(ledger, "F1", "proven", EVIDENCE, "R1")

        self.assertEqual(codes(evaluate(ledger, make_snap(fingerprint="empty"), POLICY)), ["unresolved"])

    def test_no_round(self) -> None:
        report = evaluate(Ledger(), two_hunks(), POLICY)

        self.assertEqual(codes(report), ["no-round", "no-passing-run"])
        self.assertIn("craftsman review prepare", report.issues[0].message)

    def test_stale_round_still_reports_the_rest(self) -> None:
        ledger, _ = prepared(two_hunks())

        report = evaluate(ledger, two_hunks("fp2"), POLICY)

        self.assertEqual(codes(report), ["stale-round", "uncovered", "no-passing-run"])
        self.assertIn("since round 1 was prepared", report.issues[0].message)

    def test_changed_base_is_reported_as_stale(self) -> None:
        moved = make_snap(*two_hunks().hunks, base="c" * 40)

        (issue,) = evaluate(fully_reviewed(two_hunks()), moved, POLICY).issues

        self.assertEqual(issue.code, "stale-round")
        self.assertIn("craftsman review reset", issue.message)

    def test_round_cap_stops_iterating(self) -> None:
        ledger = Ledger()
        for number in range(1, MAX_ROUNDS + 2):
            ledger, _ = prepared(two_hunks(f"fp{number}"), ledger=ledger)

        report = evaluate(ledger, two_hunks(f"fp{MAX_ROUNDS + 1}"), POLICY)

        self.assertEqual(codes(report)[-1], "round-cap")
        self.assertIn("stop iterating and report", report.issues[-1].message)

    def test_round_cap_counts_distinct_diffs(self) -> None:
        ledger = Ledger()
        for deep in (False, True):
            for number in range(1, MAX_ROUNDS):
                ledger, _ = prepared(two_hunks(f"fp{number}"), deep=deep, ledger=ledger)

        self.assertNotIn("round-cap", codes(evaluate(ledger, two_hunks("fp1"), POLICY)))


class CoverageTests(unittest.TestCase):
    def test_uncovered_issue_names_the_cover_command(self) -> None:
        ledger, _ = prepared(two_hunks())
        ledger, _ = add_coverage(ledger, ["H1"], coverage_note(["H1"]))

        report = evaluate(record_run(ledger), two_hunks(), POLICY)

        self.assertEqual(codes(report), ["uncovered"])
        self.assertIn("1 hunk(s) uncovered in round 1: H2", report.issues[0].message)
        self.assertIn('craftsman review cover --hunks H2 --note "..."', report.issues[0].message)

    def test_hunks_reopened_by_a_changed_symbol_need_fresh_coverage(self) -> None:
        caller = make_hunk("H1", "a.py", (" total = compute_total(x)", "+y = 1"))
        ledger = fully_reviewed(make_snap(caller))
        changed = make_hunk("H2", "b.py", ("+def compute_total(items, rate):",))
        snap = make_snap(caller, changed, fingerprint="fp2")
        ledger, _ = prepared(snap, ledger=ledger, symbols={"H2": ["compute_total"]})

        report = evaluate(record_run(ledger, fingerprint="fp2"), snap, POLICY)

        self.assertEqual(codes(report), ["uncovered"])
        self.assertIn("2 hunk(s) uncovered in round 2: H1,H2", report.issues[0].message)
        self.assertIn("Coverage: 0/2 hunks", report.summary)

    def test_long_lists_of_hunks_are_cut_short(self) -> None:
        snap = make_snap(*(make_hunk(f"H{index}", f"f{index}.py") for index in range(1, 21)))
        ledger, _ = prepared(snap)

        (issue, *_) = evaluate(record_run(ledger), snap, POLICY).issues

        self.assertIn("20 hunk(s) uncovered", issue.message)
        self.assertIn("(+8 more)", issue.message)


class FindingTests(unittest.TestCase):
    def test_finding_without_a_verdict_blocks(self) -> None:
        report = evaluate(record_finding(fully_reviewed(two_hunks())), two_hunks(), POLICY)

        self.assertEqual(codes(report), ["no-verdict"])
        self.assertIn("craftsman review verdict F1", report.issues[0].message)

    def test_confirmed_finding_blocks_until_resolved(self) -> None:
        ledger = record_finding(fully_reviewed(two_hunks()))
        ledger, _ = set_verdict(ledger, "F1", "probable", PROBABLE, None)

        report = evaluate(ledger, two_hunks(), POLICY)

        self.assertEqual(codes(report), ["unresolved"])
        self.assertIn("craftsman review resolve F1 --as fixed", report.issues[0].message)

    def test_design_question_does_not_block(self) -> None:
        ledger = record_finding(fully_reviewed(two_hunks()))
        evidence = "intent: the task asks for retries; app.py:3 retries forever instead"
        ledger, _ = set_verdict(ledger, "F1", "question", evidence, None)

        report = evaluate(ledger, two_hunks(), POLICY)

        self.assertTrue(report.passed)
        self.assertIn("Design questions for the author: F1", " ".join(report.warnings))

    def test_a_fix_that_is_reverted_counts_as_unresolved_again(self) -> None:
        ledger = record_run(record_finding(fully_reviewed(two_hunks())), kind="repro", exit_code=1)
        ledger, _ = set_verdict(ledger, "F1", "proven", EVIDENCE, "R2")
        ledger, _ = resolve(
            ledger, "F1", "fixed", "Guarded the empty list in total().", fingerprint="fp2"
        )

        (issue,) = evaluate(ledger, two_hunks(), POLICY).issues

        self.assertEqual(issue.code, "unresolved")
        self.assertIn("back to the code it was recorded at", issue.message)


class SignalTests(unittest.TestCase):
    def setUp(self) -> None:
        ledger, _ = prepared(two_hunks(), signals=(LONG_FUNCTION,))
        self.ledger = record_run(cover_open_hunks(ledger))

    def test_quality_signals_never_block(self) -> None:
        self.assertEqual(evaluate(self.ledger, two_hunks(), POLICY).issues, ())

    def test_diff_size_signal_is_a_warning_not_a_hand_off(self) -> None:
        ledger, _ = prepared(two_hunks(), signals=(DIFF_SIZE,))
        ledger = record_run(cover_open_hunks(ledger))

        report = evaluate(ledger, two_hunks(), POLICY)

        self.assertTrue(report.passed)
        self.assertTrue(any("Very large diff" in warning for warning in report.warnings))


class RunTests(unittest.TestCase):
    def setUp(self) -> None:
        ledger, _ = prepared(two_hunks())
        self.covered = cover_open_hunks(ledger)

    def test_only_current_nontrivial_tests_count_as_passing(self) -> None:
        ledger = record_run(self.covered, fingerprint="old")
        ledger = record_run(ledger, ("true",))
        ledger = record_run(ledger, ("ruff", "check"), kind="lint")

        report = evaluate(ledger, two_hunks(), POLICY)

        self.assertEqual(codes(report), ["no-passing-run"])
        self.assertIn("craftsman run --kind test --", report.issues[0].message)
        self.assertEqual(
            report.warnings, ("R2 (`true`) is trivial and does not count as verification.",)
        )

    def test_passing_runs_are_test_or_e2e_runs_at_the_fingerprint(self) -> None:
        ledger = record_run(self.covered, ("python3", "repro.py"), kind="other")
        ledger = record_run(ledger, kind="test")

        self.assertEqual([run.id for run in passing_runs(ledger, "fp1")], ["R2"])

    def test_deep_gate_requires_an_e2e_run(self) -> None:
        ledger = fully_reviewed(two_hunks(), deep=True)
        deep = DEEP

        self.assertEqual(codes(evaluate(ledger, two_hunks(), deep)), ["no-e2e-run"])
        ledger = record_run(ledger, ("python3", "scripts/smoke.py"), kind="e2e")
        self.assertTrue(evaluate(ledger, two_hunks(), deep).passed)

    def test_failing_run_blocks_until_the_same_command_passes(self) -> None:
        ledger = record_run(
            fully_reviewed(two_hunks()), ("ruff", "check"), kind="lint", exit_code=1, fingerprint="old"
        )

        (issue,) = evaluate(ledger, two_hunks(), POLICY).issues
        self.assertIn("`ruff check` (lint, R2) last exited 1", issue.message)

        ledger = record_run(ledger, ("ruff", "check"), kind="lint")
        self.assertTrue(evaluate(ledger, two_hunks(), POLICY).passed)

    def test_failing_runs_are_tracked_per_working_directory(self) -> None:
        ledger = record_run(fully_reviewed(two_hunks()), exit_code=1, cwd="/repo/pkg_a")
        ledger = record_run(ledger, cwd="/repo/pkg_b")

        (issue,) = evaluate(ledger, two_hunks(), POLICY).issues

        self.assertEqual(issue.code, "failing-run")
        self.assertIn("R2", issue.message)

    def test_a_run_that_modified_the_tree_never_counts_as_passing(self) -> None:
        ledger = record_run(self.covered, exit_code=1)
        ledger = record_run(ledger, fingerprint_after="fp2")

        self.assertEqual(codes(evaluate(ledger, two_hunks(), POLICY)), ["no-passing-run", "failing-run"])

    def test_waived_failing_run_no_longer_blocks(self) -> None:
        ledger = record_run(fully_reviewed(two_hunks()), ("pytest", "tests/test_typo.py"), exit_code=4)

        waived, _ = waive_run(ledger, "R2", "Typo in the path; tests/test_cart.py is the real file.")

        report = evaluate(waived, two_hunks(), POLICY)
        self.assertTrue(report.passed, render_report(report))
        self.assertIn("Waived failing runs: R2", " ".join(report.warnings))

    def test_a_later_failure_of_a_waived_command_blocks_again(self) -> None:
        ledger = record_run(fully_reviewed(two_hunks()), ("ruff", "check"), kind="lint", exit_code=1)
        ledger, _ = waive_run(ledger, "R2", "Ran with a stale config before the rename.")
        ledger = record_run(ledger, ("ruff", "check"), kind="lint", exit_code=1)

        self.assertEqual(codes(evaluate(ledger, two_hunks(), POLICY)), ["failing-run"])


T0 = "2026-01-01T00:00:00.000Z"


def retimed(ledger: Ledger, *, rounds: tuple[int, ...], coverage: tuple[int, ...]) -> Ledger:
    """`ledger` with its rounds prepared and its coverage recorded `seconds` after T0."""
    return replace(
        ledger,
        rounds=tuple(
            replace(round_, prepared_at=shifted(T0, seconds))
            for round_, seconds in zip(ledger.rounds, rounds, strict=True)
        ),
        coverage=tuple(
            replace(record, recorded_at=shifted(T0, seconds))
            for record, seconds in zip(ledger.coverage, coverage, strict=True)
        ),
    )


def reviewer_stop(seconds: int) -> AgentEvent:
    return stop_event("craftsman-reviewer", shifted(T0, seconds))


def changed_snap(fingerprint: str) -> DiffSnapshot:
    """`two_hunks` with the second hunk rewritten: the first one is carried over."""
    return make_snap(
        make_hunk("H1", "a.py"), make_hunk("H2", "b.py", ("+y = 2",)), fingerprint=fingerprint
    )


class IsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ledger = fully_reviewed(two_hunks())
        self.prepared_at = self.ledger.rounds[-1].prepared_at

    def test_isolation_requires_a_reviewer_stop(self) -> None:
        report = evaluate(self.ledger, two_hunks(), ISOLATED)

        self.assertEqual(codes(report), ["no-isolation"])
        self.assertIn("spawn the craftsman-reviewer subagent for round 1", report.issues[0].message)

    def test_reviewer_stop_after_the_round_was_prepared_is_isolation(self) -> None:
        stop = stop_event("Craftsman-Reviewer", shifted(self.prepared_at, 60))

        report = evaluate(add_agent_event(self.ledger, stop), two_hunks(), ISOLATED)

        self.assertTrue(report.passed)
        self.assertFalse(report.self_reviewed)

    def test_events_that_do_not_prove_isolation(self) -> None:
        later = shifted(self.prepared_at, 60)
        for event in (
            stop_event("craftsman-reviewer", shifted(self.prepared_at, -60)),
            stop_event("explorer", later),
            stop_event("craftsman-reviewer-helper", later),
            stop_event("craftsman-reviewer", later, event="start"),
        ):
            with self.subTest(event=event):
                report = evaluate(add_agent_event(self.ledger, event), two_hunks(), ISOLATED)
                self.assertEqual(codes(report), ["no-isolation"])

    def test_without_isolation_evidence_the_review_passes_as_self_reviewed(self) -> None:
        report = evaluate(self.ledger, two_hunks(), POLICY)

        self.assertTrue(report.passed)
        self.assertTrue(report.self_reviewed)

    def test_a_round_of_carried_hunks_needs_no_new_reviewer(self) -> None:
        ledger, _ = prepared(two_hunks("fp2"), ledger=self.ledger)
        ledger = retimed(ledger, rounds=(0, 100), coverage=(10,))
        ledger = add_agent_event(ledger, reviewer_stop(60))

        report = evaluate(record_run(ledger, fingerprint="fp2"), two_hunks("fp2"), ISOLATED)

        self.assertTrue(report.passed, render_report(report))

    def test_a_diff_without_hunks_needs_no_reviewer(self) -> None:
        snap = DiffSnapshot(BASE_SHA, "fp1", (), ("logo.png",), 0, 0)
        ledger, _ = prepared(snap)

        report = evaluate(record_run(ledger), snap, ISOLATED)

        self.assertTrue(report.passed, render_report(report))

    def test_review_without_isolation_is_marked_self_reviewed(self) -> None:
        report = evaluate(self.ledger, two_hunks(), POLICY)

        self.assertIn("SELF-REVIEWED", render_report(report))


class RoundIsolationTests(unittest.TestCase):
    """Every round with hunks to review needs a reviewer that finished while it was current."""

    def two_rounds(self, *, round1_covered_at: int = 10, round2_covered_at: int = 110) -> Ledger:
        ledger, _ = prepared(two_hunks())
        ledger = cover_open_hunks(ledger)
        ledger, _ = prepared(changed_snap("fp2"), ledger=ledger)
        ledger = cover_open_hunks(ledger)
        ledger = retimed(
            ledger, rounds=(0, 100), coverage=(round1_covered_at, round2_covered_at)
        )
        return record_run(ledger, fingerprint="fp2")

    def check(self, ledger: Ledger, policy: GatePolicy = ISOLATED) -> GateReport:
        return evaluate(ledger, changed_snap("fp2"), policy)

    def test_a_later_reviewer_does_not_make_up_for_a_round_nobody_reviewed(self) -> None:
        ledger = add_agent_event(self.two_rounds(), reviewer_stop(120))

        report = self.check(ledger)

        self.assertEqual(codes(report), ["no-isolation"])
        self.assertIn("Round 1 has coverage that no craftsman-reviewer run examined", report.issues[0].message)

    def test_the_new_hunks_of_a_later_round_need_their_own_reviewer(self) -> None:
        ledger = add_agent_event(self.two_rounds(), reviewer_stop(20))

        report = self.check(ledger)

        self.assertEqual(codes(report), ["no-isolation"])
        self.assertIn("since round 2 was prepared", report.issues[0].message)

    def test_a_round_replaced_before_any_review_needs_no_reviewer(self) -> None:
        ledger, _ = prepared(two_hunks())
        ledger, _ = prepared(changed_snap("fp2"), ledger=ledger)
        ledger = retimed(cover_open_hunks(ledger), rounds=(0, 100), coverage=(110,))
        ledger = add_agent_event(record_run(ledger, fingerprint="fp2"), reviewer_stop(120))

        report = self.check(ledger)

        self.assertTrue(report.passed, render_report(report))

    def test_a_reviewer_that_outlives_its_round_still_examined_what_it_recorded(self) -> None:
        ledger, _ = prepared(two_hunks())
        ledger = cover_open_hunks(ledger)
        ledger, _ = prepared(changed_snap("fp2"), ledger=ledger)
        ledger = retimed(cover_open_hunks(ledger), rounds=(0, 100), coverage=(10, 110))
        started = AgentEvent("copilot", "start", "craftsman-reviewer", "x", shifted(T0, 5))
        ledger = add_agent_event(add_agent_event(record_run(ledger, fingerprint="fp2"), started), reviewer_stop(120))

        report = self.check(ledger)

        self.assertTrue(report.passed, render_report(report))

    def test_coverage_recorded_outside_any_reviewer_run_still_needs_one(self) -> None:
        ledger, _ = prepared(two_hunks())
        ledger = cover_open_hunks(ledger)
        ledger, _ = prepared(changed_snap("fp2"), ledger=ledger)
        ledger = retimed(cover_open_hunks(ledger), rounds=(0, 100), coverage=(10, 110))
        started = AgentEvent("copilot", "start", "craftsman-reviewer", "x", shifted(T0, 105))
        ledger = add_agent_event(add_agent_event(record_run(ledger, fingerprint="fp2"), started), reviewer_stop(120))

        report = self.check(ledger)

        self.assertEqual(codes(report), ["no-isolation"])
        self.assertIn("Round 1", report.issues[0].message)

    def test_a_reviewer_in_every_round_passes(self) -> None:
        ledger = add_agent_event(add_agent_event(self.two_rounds(), reviewer_stop(20)), reviewer_stop(120))

        report = self.check(ledger)

        self.assertTrue(report.passed, render_report(report))
        self.assertFalse(report.self_reviewed)

    def test_a_round_that_only_carries_hunks_keeps_the_earlier_rounds_checked(self) -> None:
        ledger, _ = prepared(two_hunks())
        ledger = cover_open_hunks(ledger)
        ledger, _ = prepared(two_hunks("fp2"), ledger=ledger)
        ledger = record_run(retimed(ledger, rounds=(0, 100), coverage=(10,)), fingerprint="fp2")

        self.assertEqual(codes(evaluate(ledger, two_hunks("fp2"), ISOLATED)), ["no-isolation"])
        self.assertTrue(evaluate(ledger, two_hunks("fp2"), POLICY).self_reviewed)
        reviewed = add_agent_event(ledger, reviewer_stop(20))
        self.assertTrue(evaluate(reviewed, two_hunks("fp2"), ISOLATED).passed)

    def test_a_reviewer_that_recorded_nothing_is_no_isolation(self) -> None:
        ledger, _ = prepared(two_hunks())
        ledger = retimed(cover_open_hunks(ledger), rounds=(0,), coverage=(50,))
        ledger = record_run(add_agent_event(ledger, reviewer_stop(40)))

        self.assertEqual(codes(evaluate(ledger, two_hunks(), ISOLATED)), ["no-isolation"])
        later = add_agent_event(ledger, reviewer_stop(60))
        self.assertTrue(evaluate(later, two_hunks(), ISOLATED).passed)

    def test_coverage_before_the_reviewer_started_is_not_its_work(self) -> None:
        ledger, _ = prepared(two_hunks())
        ledger = retimed(cover_open_hunks(ledger), rounds=(0,), coverage=(10,))
        started = AgentEvent("copilot", "start", "craftsman-reviewer", "x", shifted(T0, 30))
        ledger = record_run(add_agent_event(add_agent_event(ledger, started), reviewer_stop(40)))

        self.assertEqual(codes(evaluate(ledger, two_hunks(), ISOLATED)), ["no-isolation"])

    def test_self_reviewed_stays_marked_through_a_round_of_carried_hunks(self) -> None:
        ledger, _ = prepared(two_hunks())
        ledger = cover_open_hunks(ledger)
        ledger, _ = prepared(two_hunks("fp2"), ledger=ledger)
        ledger = record_run(ledger, fingerprint="fp2")

        self.assertTrue(evaluate(ledger, two_hunks("fp2"), POLICY).self_reviewed)

    def test_a_deep_review_without_evidence_passes_as_self_reviewed_once_it_has_e2e(self) -> None:
        ledger = fully_reviewed(two_hunks(), deep=True)
        ledger = record_run(ledger, ("python3", "smoke.py"), kind="e2e")

        report = evaluate(ledger, two_hunks(), DEEP)

        self.assertTrue(report.passed, render_report(report))
        self.assertTrue(report.self_reviewed)

    def test_a_deep_gate_needs_a_test_run_as_well_as_an_e2e_run(self) -> None:
        ledger, _ = prepared(two_hunks(), deep=True)
        ledger = record_run(cover_open_hunks(ledger), ("python3", "smoke.py"), kind="e2e")

        self.assertEqual(codes(evaluate(ledger, two_hunks(), DEEP)), ["no-passing-run"])
        self.assertTrue(evaluate(ledger, two_hunks(), POLICY).passed)
        ledger = record_run(ledger, kind="test")
        self.assertTrue(evaluate(ledger, two_hunks(), DEEP).passed)

    def test_the_antigravity_hint_names_when_reviewers_are_recorded(self) -> None:
        ledger = fully_reviewed(two_hunks())

        report = evaluate(ledger, two_hunks(), GatePolicy(isolation_evidence="transcript"))

        self.assertIn("transcript when your turn ends", report.issues[0].message)


def agent_event(kind: str, seconds: int, agent_id: str, records: tuple[str, ...] | None = None) -> AgentEvent:
    return AgentEvent(
        "claude", kind, "craftsman-reviewer", agent_id, shifted(T0, seconds), records=records  # type: ignore[arg-type]
    )


def finding_at(ledger: Ledger, seconds: int) -> Ledger:
    """`ledger` with a dismissed intent finding, recorded `seconds` after T0 by whoever."""
    ledger = record_finding(ledger, lens="intent")
    finding = replace(ledger.findings[-1], recorded_at=shifted(T0, seconds))
    ledger = replace(ledger, findings=(*ledger.findings[:-1], finding))
    return set_verdict(ledger, finding.id, "dismissed", DISMISSED, None)[0]


class CoverageAttributionTests(unittest.TestCase):
    """Coverage must come from a reviewer; findings may come from anyone."""

    def one_round(self, *coverage_at: int) -> Ledger:
        """Round 1 (H1, H2) prepared at T0, covered by one record per time in `coverage_at`."""
        ledger, _ = prepared(two_hunks())
        for _ in coverage_at:
            ledger = cover_open_hunks(ledger)
        return record_run(retimed(ledger, rounds=(0,), coverage=coverage_at))

    def check(self, ledger: Ledger) -> GateReport:
        return evaluate(ledger, two_hunks(), ISOLATED)

    def run_events(self, ledger: Ledger, start: int, stop: int, records: tuple[str, ...] | None = None) -> Ledger:
        ledger = add_agent_event(ledger, agent_event("start", start, "a"))
        return add_agent_event(ledger, agent_event("stop", stop, "a", records))

    def test_coverage_the_orchestrator_recorded_outside_any_run_fails_the_latest_round(self) -> None:
        ledger = finding_at(self.one_round(10), 25)  # the reviewer ran 20-30 and only added a finding
        ledger = self.run_events(ledger, 20, 30)

        report = self.check(ledger)

        self.assertEqual(codes(report), ["no-isolation"])
        message = report.issues[0].message
        self.assertIn("Hunks H1,H2 of round 1 were covered outside any finished craftsman-reviewer run", message)
        self.assertIn("Only a craftsman-reviewer's own coverage counts, once it finishes", message)
        self.assertIn("if the reviewer covering these hunks is still running, wait for it", message)
        self.assertNotIn("A reviewer still running counts", message)
        self.assertIn("craftsman review cover --hunks H1,H2", message)
        self.assertNotIn("reset", message)
        self.assertNotIn("spawn", message)

    def test_a_reviewer_covering_those_hunks_again_clears_it(self) -> None:
        ledger = self.run_events(self.one_round(10, 25), 20, 30)

        self.assertTrue(self.check(ledger).passed, render_report(self.check(ledger)))

    def orchestrator_then_reviewer(self) -> Ledger:
        """C1 covers H1 and H2, C2 covers H1 again; both were recorded inside the run 5-30."""
        ledger, _ = prepared(two_hunks())
        ledger, _ = add_coverage(ledger, ["H1", "H2"], coverage_note(["H1", "H2"]))
        ledger, _ = add_coverage(ledger, ["H1"], coverage_note(["H1"]))
        return record_run(retimed(ledger, rounds=(0,), coverage=(10, 15)))

    def test_with_transcript_evidence_a_cover_inside_the_window_is_not_the_reviewers(self) -> None:
        ledger = self.orchestrator_then_reviewer()

        mine = self.check(self.run_events(ledger, 5, 30, records=("C2",)))
        everything = self.check(self.run_events(ledger, 5, 30, records=("C1",)))

        self.assertEqual(codes(mine), ["no-isolation"])
        self.assertIn("Hunks H2 of round 1 were covered outside", mine.issues[0].message)
        self.assertTrue(everything.passed)
        by_time = self.check(self.run_events(ledger, 5, 30, records=None))
        self.assertTrue(by_time.passed)  # without transcript evidence the window decides

    def test_an_intent_finding_recorded_outside_every_run_needs_no_reviewer(self) -> None:
        ledger = finding_at(self.run_events(self.one_round(25), 20, 30), 5)

        self.assertTrue(self.check(ledger).passed, render_report(self.check(ledger)))

    def test_an_intent_finding_of_an_earlier_round_needs_no_reviewer(self) -> None:
        ledger, _ = prepared(two_hunks())
        ledger = cover_open_hunks(ledger)
        ledger, _ = prepared(changed_snap("fp2"), ledger=ledger)
        ledger = retimed(cover_open_hunks(ledger), rounds=(0, 100), coverage=(10, 110))
        ledger = finding_at(record_run(ledger, fingerprint="fp2"), 50)  # round 1, between the two reviewers
        ledger = add_agent_event(add_agent_event(ledger, reviewer_stop(20)), reviewer_stop(120))

        report = evaluate(ledger, changed_snap("fp2"), ISOLATED)

        self.assertTrue(report.passed, render_report(report))

    def test_parallel_reviewers_are_attributed_separately(self) -> None:
        ledger, _ = prepared(two_hunks())
        ledger, _ = add_coverage(ledger, ["H1"], coverage_note(["H1"]))
        ledger, _ = add_coverage(ledger, ["H2"], coverage_note(["H2"]))
        ledger = record_run(retimed(ledger, rounds=(0,), coverage=(10, 12)))
        both_run = add_agent_event(add_agent_event(ledger, agent_event("start", 5, "a")), agent_event("start", 6, "b"))

        def stopped(a: tuple[str, ...], b: tuple[str, ...]) -> GateReport:
            events = add_agent_event(both_run, agent_event("stop", 30, "a", a))
            return self.check(add_agent_event(events, agent_event("stop", 31, "b", b)))

        self.assertTrue(stopped(("C1",), ("C2",)).passed)
        self.assertTrue(stopped(("C2", "C1"), ()).passed)  # one reviewer did both
        unclaimed = stopped(("C1",), ("F1",))  # both windows hold C2, neither run printed it
        self.assertIn("Hunks H2 of round 1 were covered outside", unclaimed.issues[0].message)

    def test_a_transcript_that_shows_no_records_credits_nothing_by_time(self) -> None:
        ledger = self.one_round(10)

        self.assertTrue(self.check(self.run_events(ledger, 5, 30, records=None)).passed)
        silent = self.check(self.run_events(ledger, 5, 30, records=()))
        self.assertIn("spawn the craftsman-reviewer", silent.issues[0].message)

    def test_stops_without_an_agent_id_pair_with_the_earliest_unpaired_start(self) -> None:
        ledger = self.one_round(6)  # recorded while the first of two parallel runs was going
        for kind, seconds in (("start", 5), ("start", 8), ("stop", 20), ("stop", 25)):
            ledger = add_agent_event(ledger, agent_event(kind, seconds, ""))

        report = self.check(ledger)

        self.assertTrue(report.passed, render_report(report))

    def test_a_token_whose_record_lies_outside_the_run_window_is_not_credited(self) -> None:
        for covered_at in (2, 40):  # before the run started, after it stopped
            with self.subTest(covered_at=covered_at):
                ledger = self.run_events(self.one_round(covered_at), 5, 30, records=("C1",))

                report = self.check(ledger)

                self.assertEqual(codes(report), ["no-isolation"])
                self.assertIn("No independent review finished", report.issues[0].message)

    def test_a_resumed_reviewer_cannot_claim_a_new_record_with_an_old_token(self) -> None:
        ledger = self.run_events(self.one_round(10), 5, 30, records=("C1",))  # the run's own C1
        reset = replace(ledger, coverage=(replace(ledger.coverage[0], recorded_at=shifted(T0, 60)),))  # C1 after a reset

        self.assertTrue(self.check(ledger).passed)
        self.assertEqual(codes(self.check(reset)), ["no-isolation"])

    def test_the_latest_round_ignores_a_run_whose_tokens_all_predate_it(self) -> None:
        ledger = RoundIsolationTests().two_rounds()  # round 1 prepared at 0, round 2 at 100; C1 at 10, C2 at 110
        ledger = add_agent_event(add_agent_event(ledger, agent_event("start", 5, "a")), agent_event("stop", 120, "a", ("C1",)))

        report = evaluate(ledger, changed_snap("fp2"), ISOLATED)

        self.assertEqual(codes(report), ["no-isolation"])
        self.assertIn("No independent review finished since round 2 was prepared", report.issues[0].message)

    def test_the_latest_round_counts_a_run_with_one_of_its_own_current_records(self) -> None:
        ledger = RoundIsolationTests().two_rounds()
        for tokens in (("C1", "C2"), ("C9", "C1", "F9", "C2")):
            with self.subTest(tokens=tokens):
                events = add_agent_event(add_agent_event(ledger, agent_event("start", 5, "a")), agent_event("stop", 120, "a", tokens))

                self.assertTrue(evaluate(events, changed_snap("fp2"), ISOLATED).passed)

    def test_tokens_no_record_carries_are_ignored(self) -> None:
        ledger = self.run_events(self.one_round(10), 5, 30, records=("C9", "F9"))

        self.assertEqual(codes(self.check(ledger)), ["no-isolation"])

    def test_a_stop_with_an_id_no_start_carries_pairs_with_the_unpaired_starts_of_its_name(self) -> None:
        for covered_at, passes in ((6, False), (10, True)):  # before the reviewer started, while it ran
            with self.subTest(covered_at=covered_at):
                ledger = add_agent_event(self.one_round(covered_at), agent_event("start", 8, ""))
                ledger = add_agent_event(ledger, agent_event("stop", 20, "copilot-id"))  # Copilot: start by name only

                self.assertEqual(self.check(ledger).passed, passes)

    def test_a_start_taken_by_an_id_match_is_not_paired_again(self) -> None:
        ledger = self.one_round(6)
        for kind, seconds, agent_id, records in (
            ("start", 5, "a", None),
            ("start", 8, "b", None),
            ("stop", 20, "a", ("F9",)),  # takes the start at 5
            ("stop", 25, "", None),  # pairs with the start at 8, so C1 at 6 is not its
        ):
            ledger = add_agent_event(ledger, agent_event(kind, seconds, agent_id, records))

        self.assertEqual(codes(self.check(ledger)), ["no-isolation"])

    def test_a_stop_with_no_start_at_all_counts_from_the_round_that_was_current(self) -> None:
        ledger = add_agent_event(self.one_round(6), agent_event("stop", 20, "x"))

        self.assertTrue(self.check(ledger).passed)

    def test_a_record_in_an_earlier_round_is_judged_in_its_own_round(self) -> None:
        ledger = self.run_events(RoundIsolationTests().two_rounds(), 100, 120, records=("C2",))

        report = evaluate(ledger, changed_snap("fp2"), ISOLATED)

        self.assertEqual(codes(report), ["no-isolation"])
        self.assertIn("Round 1 has coverage that no craftsman-reviewer run examined (hunks H1,H2)", report.issues[0].message)
        self.assertIn("reset", report.issues[0].message)


class ReviewOnlyTests(unittest.TestCase):
    def test_review_only_passes_without_resolved_findings_or_passing_runs(self) -> None:
        ledger = record_run(fully_reviewed(two_hunks()), exit_code=1)
        ledger = record_run(ledger, kind="repro", exit_code=1)
        ledger = record_finding(record_finding(ledger))
        ledger, _ = set_verdict(ledger, "F1", "proven", EVIDENCE, "R3")
        ledger, _ = set_verdict(ledger, "F2", "probable", PROBABLE, None)

        report = evaluate(ledger, two_hunks(), POLICY, review_only=True)

        self.assertEqual(report.issues, ())
        self.assertIn("2 findings for the author: F1, F2.", report.warnings)
        self.assertIn("Failing run R2 (`pytest -q`, test) exited 1.", report.warnings)

    def test_review_only_still_requires_coverage(self) -> None:
        ledger, _ = prepared(two_hunks())

        self.assertEqual(codes(evaluate(ledger, two_hunks(), POLICY, review_only=True)), ["uncovered"])

    def test_review_only_still_requires_verdicts(self) -> None:
        ledger = record_finding(fully_reviewed(two_hunks()))

        report = evaluate(ledger, two_hunks(), POLICY, review_only=True)

        self.assertEqual(codes(report), ["no-verdict"])


class WarningTests(unittest.TestCase):
    def test_accepted_findings_are_reported_to_the_user(self) -> None:
        ledger = record_finding(fully_reviewed(two_hunks()))
        ledger, _ = set_verdict(ledger, "F1", "probable", PROBABLE, None)
        ledger, _ = resolve(
            ledger, "F1", "accepted", "Callers never pass an empty list.", fingerprint="fp1"
        )

        report = evaluate(ledger, two_hunks(), POLICY)

        self.assertIn("Accepted without a fix: F1; tell the user about them.", report.warnings)
        self.assertIn("1 accepted (F1)", report.summary)

class AnomalyTests(unittest.TestCase):
    def test_all_findings_confirmed_is_an_anomaly(self) -> None:
        ledger = fully_reviewed(two_hunks())
        for number in range(1, 6):
            ledger = record_finding(ledger)
            ledger, _ = set_verdict(ledger, f"F{number}", "probable", PROBABLE, None)
            ledger, _ = resolve(
                ledger, f"F{number}", "fixed", "Guarded the empty list.", fingerprint="fp2"
            )

        warnings = evaluate(ledger, two_hunks(), POLICY).warnings

        self.assertIn(
            "Process anomaly: 5 findings and 0 dismissed (all findings confirmed — typical of self-review).",
            warnings,
        )

    def test_dismissed_findings_are_no_anomaly(self) -> None:
        ledger = fully_reviewed(two_hunks())
        for number in range(1, 6):
            ledger = record_finding(ledger)
            ledger, _ = set_verdict(ledger, f"F{number}", "dismissed", DISMISSED, None)

        self.assertEqual(evaluate(ledger, two_hunks(), POLICY).warnings, ())

    def test_failing_test_rerun_under_another_environment_is_an_anomaly(self) -> None:
        ledger = record_run(fully_reviewed(two_hunks()), exit_code=1)
        ledger = record_run(ledger, ("env", "FOO=1", "pytest", "-q"))

        warnings = evaluate(ledger, two_hunks(), POLICY).warnings

        self.assertIn(
            "Process anomaly: test run R2 failed and was repeated as R3 with a different "
            "environment without a code change.",
            warnings,
        )

    def test_rerunning_the_same_failing_command_is_no_anomaly(self) -> None:
        ledger = record_run(record_run(fully_reviewed(two_hunks()), exit_code=1), exit_code=1)

        self.assertEqual(evaluate(ledger, two_hunks(), POLICY).warnings, ())

    def test_a_failure_retried_repeatedly_is_reported_once(self) -> None:
        ledger = record_run(fully_reviewed(two_hunks()), exit_code=1)
        for _ in range(3):
            ledger = record_run(ledger, ("env", "A=1", "pytest", "-q"))

        warnings = [w for w in evaluate(ledger, two_hunks(), POLICY).warnings if "repeated" in w]

        self.assertEqual(len(warnings), 1)

    def test_every_environment_retry_after_a_failure_is_an_anomaly(self) -> None:
        ledger = record_run(fully_reviewed(two_hunks()), exit_code=1)
        ledger = record_run(ledger, ("env", "A=1", "pytest", "-q"), exit_code=1)
        ledger = record_run(ledger, ("env", "B=1", "pytest", "-q"))

        warnings = " ".join(evaluate(ledger, two_hunks(), POLICY).warnings)

        self.assertIn("R2 failed and was repeated as R3", warnings)
        self.assertIn("R3 failed and was repeated as R4", warnings)

class ReportRenderingTests(unittest.TestCase):
    def test_status_label_names_the_outcome(self) -> None:
        issue = GateIssue("no-round", "No review round yet.")
        cases = [
            (GateReport((), (), ""), "PASSED"),
            (GateReport((issue,), (), ""), "BLOCKED (1 open items)"),
            (GateReport((), (), "", review_only=True), "COMPLETE (review-only)"),
            (GateReport((issue,), (), "", review_only=True), "INCOMPLETE (1 open items)"),
        ]
        for report, label in cases:
            with self.subTest(label=label):
                self.assertEqual(report.status_label, label)

    def test_rendered_report_starts_with_the_status(self) -> None:
        report = evaluate(fully_reviewed(two_hunks()), two_hunks(), POLICY)

        self.assertTrue(render_report(report).startswith("**Craftsman gate: PASSED**"))

    def test_render_report_caps_next_actions(self) -> None:
        ledger, _ = prepared(two_hunks())
        for _ in range(14):
            ledger = record_finding(ledger)

        rendered = render_report(evaluate(ledger, two_hunks(), POLICY))

        self.assertIn("12. [no-verdict]", rendered)
        self.assertNotIn("13. ", rendered)
        self.assertTrue(rendered.endswith("… and 4 more"))


class EndToEndTests(unittest.TestCase):
    def test_editing_a_file_after_review_makes_the_round_stale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = EvidenceRepo(Path(tmp).resolve())
            repo.write("app.py", "def total(items):\n    return sum(items) or 0\n")
            snap = snapshot(repo.root, repo.base_sha)
            ledger = fully_reviewed(snap)
            self.assertTrue(evaluate(ledger, snap, POLICY).passed)

            repo.write("app.py", "def total(items):\n    return sum(items) or None\n")
            edited = snapshot(repo.root, repo.base_sha)

            self.assertEqual(codes(evaluate(ledger, edited, POLICY)), ["stale-round", "no-passing-run"])


class FormatBlockerSummaryTests(unittest.TestCase):
    def test_empty_issues_returns_review_complete(self) -> None:
        report = GateReport((), (), "Summary")
        self.assertEqual(format_blocker_summary(report), "review complete")

    def test_single_round_issue(self) -> None:
        report = GateReport((GateIssue("no-round", "No round prepared"),), (), "Summary")
        self.assertEqual(format_blocker_summary(report), "review incomplete: no review round prepared")

    def test_uncovered_counts_come_from_the_real_coverage_issues(self) -> None:
        ledger, _ = prepared(two_hunks())
        report = evaluate(ledger, two_hunks(), POLICY)
        self.assertEqual(
            format_blocker_summary(report),
            "review incomplete: 2 hunks not covered; no passing test run",
        )

    def test_uncovered_counts_are_summed(self) -> None:
        first = GateIssue("uncovered", "…", missing=1)
        second = GateIssue("uncovered", "…", missing=2)
        report = GateReport((first, second), (), "Summary")
        self.assertEqual(format_blocker_summary(report), "review incomplete: 3 hunks not covered")

    def test_multiple_categories_joined_with_semicolon(self) -> None:
        i1 = GateIssue("uncovered", "…", missing=3)
        i2 = GateIssue("no-verdict", "Finding F1 has no verdict")
        i3 = GateIssue("no-passing-run", "No passing test run")
        report = GateReport((i3, i2, i1), (), "Summary")
        self.assertEqual(
            format_blocker_summary(report),
            "review incomplete: 3 hunks not covered; 1 finding without a verdict; "
            "no passing test run",
        )

    def test_unresolved_findings_and_failing_runs(self) -> None:
        i1 = GateIssue("unresolved", "Finding F1 is proven but unresolved")
        i2 = GateIssue("unresolved", "Finding F2 is proven but unresolved")
        i3 = GateIssue("failing-run", "Run 1 failed")
        i4 = GateIssue("no-isolation", "Review lacks proof of an isolated reviewer")
        report = GateReport((i1, i2, i3, i4), (), "Summary")
        self.assertEqual(
            format_blocker_summary(report),
            "review incomplete: 2 unresolved findings; 1 failing run; no reviewer isolation evidence",
        )


# --- enforced rounds with a hunt plan --------------------------------------------------------

REVIEWER = "craftsman-reviewer"
VERIFIER = "craftsman-verifier"
AGY = "antigravity"
SLOT_LENSES = {"W1-1": ("intent",), "W1-2": ("contracts",), "W1-3": ("failure",), "W1-4": ("ripple",)}
EVIDENCE_OF = {
    "dismissed": DISMISSED,
    "probable": PROBABLE,
    "question": "intent: the task asks for retries; app.py:3 retries forever",
    "proven": EVIDENCE,
}


def moment(seconds: float) -> datetime:
    return parse_timestamp(at(seconds))


def hunt_snap(count: int = 4) -> DiffSnapshot:
    return make_snap(*(make_hunk(f"H{n}", f"f{n}.py") for n in range(1, count + 1)))


def hunt(*slot_ids: str, count: int = 4, enforced: bool = True) -> Ledger:
    """Round 1, prepared at `T0`, with a plan of `slot_ids`: slot n owns hunk Hn (a fresh set owns them all)."""
    ledger, _ = prepared(hunt_snap(count))
    ledger = replace(ledger, rounds=(replace(ledger.rounds[0], prepared_at=T0, enforced=enforced),))
    slots = [
        make_slot(
            slot_id,
            hunks=(f"H{n}",) if slot_id.split("-")[1][0].isdigit() else tuple(f"H{k}" for k in range(1, count + 1)),
            lenses=SLOT_LENSES.get(slot_id, ("intent",)),
        )
        for n, slot_id in enumerate(slot_ids, 1)
    ]
    return planned(ledger, *slots)


def with_agents(ledger: Ledger, *events: AgentEvent) -> Ledger:
    for event in events:
        ledger = add_agent_event(ledger, event)
    return ledger


def run_events(
    agent_id: str,
    start: float,
    stop: float | None,
    *,
    agent: str = REVIEWER,
    platform: str = "claude",
    records: tuple[str, ...] | None = None,
) -> tuple[AgentEvent, ...]:
    """A run's start event, and its stop event unless `stop` is None (the run is still open)."""
    events = [timed_event("start", agent, agent_id, start, platform=platform)]
    if stop is not None:
        events.append(timed_event("stop", agent, agent_id, stop, platform=platform, records=records))
    return tuple(events)


def claim(ledger: Ledger, slot: str, seconds: float, *, by: str | None = None) -> Ledger:
    token = f"B{len(ledger.claims) + 1}"
    return replace(ledger, claims=(*ledger.claims, SlotClaim(1, slot, token, at(seconds), by)))


def cover(
    ledger: Ledger, slot: str, seconds: float, *, hunks: Sequence[str] | None = None, by: str | None = None
) -> Ledger:
    """Coverage tagged with `slot`, of its hunks unless `hunks` says otherwise."""
    held = next(item for item in required_slots(ledger, ledger.rounds[0]) if item.id == slot)
    ids = tuple(hunks) if hunks is not None else held.hunks
    digests = tuple(ref.digest for ref in ledger.rounds[0].hunks if ref.id in ids)
    record = Coverage(1, digests, coverage_note(ids), at(seconds), hunter=slot, by=by)
    return replace(ledger, coverage=(*ledger.coverage, record))


def hunted(ledger: Ledger, slot: str, agent_id: str, start: float, stop: float, **kw: str) -> Ledger:
    """`slot` claimed, covered and finished by a claude reviewer run `agent_id` (the tokens are the next free ones)."""
    claim_token, cover_token = f"B{len(ledger.claims) + 1}", f"C{len(ledger.coverage) + 1}"
    ledger = cover(claim(ledger, slot, start + 1), slot, start + 2)
    return with_agents(ledger, *run_events(agent_id, start, stop, records=(claim_token, cover_token), **kw))


def statuses(ledger: Ledger, seconds: float = 100) -> dict[str, SlotStatus]:
    return {item.slot.id: item for item in slot_statuses(ledger, ledger.rounds[-1], moment(seconds))}


def check(ledger: Ledger, seconds: float = 100, *, review_only: bool = True, snap: DiffSnapshot | None = None) -> GateReport:
    return evaluate(ledger, snap or hunt_snap(), POLICY, review_only=review_only, now=moment(seconds))


def judged(ledger: Ledger, finding_id: str, *events: VerdictEvent, run: str | None = None) -> Ledger:
    """`finding_id` with exactly these verdict events (its current verdict is the last one's)."""
    last = events[-1].verdict
    findings = tuple(
        replace(f, verdict=last, verdict_log=events, verdict_evidence=EVIDENCE_OF[last], verdict_run=run)
        if f.id == finding_id
        else f
        for f in ledger.findings
    )
    return replace(ledger, findings=findings)


def with_findings(ledger: Ledger, *specs: tuple[str, str, str]) -> Ledger:
    """Findings `(severity, path, lens)`, F1 first, recorded against line 1 of their file."""
    for severity, path, lens in specs:
        ledger = record_finding(ledger, severity, path=path, line=1, lens=lens, title=f"Problem number {len(ledger.findings) + 1}")  # type: ignore[arg-type]
    return ledger


def one_slot_done() -> Ledger:
    """A single-slot plan (W1-1, hunk H1) that a claude hunter finished: nothing left in the hunt."""
    return hunted(hunt("W1-1", count=1), "W1-1", "h1", 10, 60)


class SlotStatusTests(unittest.TestCase):
    def test_a_slot_is_done_when_a_vouched_hunter_covered_its_hunks(self) -> None:
        ledger = hunted(hunt("W1-1", "W1-2"), "W1-1", "h1", 10, 60)

        result = statuses(ledger)

        self.assertEqual(result["W1-1"].state, "done")
        self.assertEqual(result["W1-1"].missing, ())
        self.assertEqual((result["W1-2"].state, result["W1-2"].missing), ("unlaunched", ("H2",)))

    def test_credit_from_any_slot_finishes_a_slot_over_the_same_hunk_and_lens(self) -> None:
        both_own_h1 = planned(hunt("W1-1", count=1), make_slot("W1-1"), make_slot("W1-2"))  # lens intent each
        ledger = hunted(both_own_h1, "W1-1", "h1", 10, 60)

        self.assertEqual({item.state for item in statuses(ledger).values()}, {"done"})

    def test_coverage_the_orchestrator_recorded_outside_every_run_counts_for_nothing(self) -> None:
        ledger = cover(hunt("W1-1"), "W1-1", 500)
        ledger = with_agents(ledger, *run_events("h1", 10, 60, records=()))

        self.assertEqual(statuses(ledger)["W1-1"].state, "unlaunched")

    def test_one_run_that_covered_two_slots_credits_the_first(self) -> None:
        ledger = claim(hunt("W1-1", "W1-2"), "W1-1", 11)
        ledger = cover(cover(ledger, "W1-1", 12), "W1-2", 13)
        ledger = with_agents(ledger, *run_events("h1", 10, 60, records=("B1", "C1", "C2")))

        result = statuses(ledger)

        self.assertEqual((result["W1-1"].state, result["W1-2"].state), ("done", "dead"))

    def test_a_claim_backed_by_an_open_run_is_running(self) -> None:
        ledger = with_agents(claim(hunt("W1-1"), "W1-1", 20), *run_events("h1", 10, None))

        result = statuses(ledger)["W1-1"]

        self.assertEqual((result.state, result.since), ("running", moment(10)))

    def test_an_open_run_that_started_before_the_claim_backs_it(self) -> None:
        ledger = with_agents(claim(hunt("W1-1", "W1-2"), "W1-1", 20), *run_events("other", 10, None))

        self.assertEqual(statuses(ledger)["W1-1"].state, "running")
        self.assertEqual(statuses(ledger)["W1-2"].state, "unlaunched")  # no claim at all

    def test_a_bare_claim_with_no_run_is_unlaunched_not_running(self) -> None:
        self.assertEqual(statuses(claim(hunt("W1-1"), "W1-1", 20))["W1-1"].state, "unlaunched")

    def test_an_open_run_that_started_after_the_claim_or_before_the_round_does_not_back_it(self) -> None:
        after = with_agents(claim(hunt("W1-1"), "W1-1", 20), *run_events("late", 30, None))
        before = with_agents(claim(hunt("W1-1"), "W1-1", 20), *run_events("early", -5, None))

        self.assertEqual(statuses(after)["W1-1"].state, "unlaunched")
        self.assertEqual(statuses(before)["W1-1"].state, "unlaunched")

    def test_a_run_that_stopped_without_finishing_the_slot_leaves_it_dead(self) -> None:
        ledger = with_agents(claim(hunt("W1-1"), "W1-1", 20), *run_events("h1", 10, 60, records=("B1",)))

        result = statuses(ledger)["W1-1"]

        self.assertEqual((result.state, result.since), ("dead", moment(60)))

    def test_a_run_that_never_reported_is_dead_once_stale(self) -> None:
        ledger = with_agents(claim(hunt("W1-1"), "W1-1", 20), *run_events("h1", 10, None))
        stale = 10 + RUN_STALE_AFTER.total_seconds()

        self.assertEqual(statuses(ledger, stale - 1)["W1-1"].state, "running")
        result = statuses(ledger, stale + 1)["W1-1"]
        self.assertEqual((result.state, result.since), ("dead", moment(stale)))

    def test_a_relaunch_after_a_dead_hunter_is_running_again(self) -> None:
        ledger = with_agents(claim(hunt("W1-1"), "W1-1", 20), *run_events("first", 10, 60, records=("B1",)))
        ledger = with_agents(claim(ledger, "W1-1", 120), *run_events("second", 100, None))

        self.assertEqual(statuses(ledger, 150)["W1-1"].state, "running")

    def test_an_antigravity_claim_is_backed_by_its_own_conversation_only(self) -> None:
        ledger = claim(hunt("W1-1"), "W1-1", 20, by=f"{AGY}:a1")
        mine = with_agents(ledger, *run_events("a1", 10, None, platform=AGY))
        theirs = with_agents(ledger, *run_events("a2", 10, None, platform=AGY))

        self.assertEqual(statuses(mine)["W1-1"].state, "running")
        self.assertEqual(statuses(theirs)["W1-1"].state, "unlaunched")

    def test_a_sweep_slot_is_not_done_without_sweep_credit_on_every_hunk(self) -> None:
        ledger = hunt("W1-1", "W1-2", count=2)
        ledger = planned(
            ledger,
            make_slot("W1-1", hunks=("H1", "H2"), lenses=("intent",)),
            make_slot("W1-2", hunks=("H1", "H2"), lenses=("sweep",), kind="sweep"),
        )
        ledger = hunted(ledger, "W1-1", "h1", 10, 60)

        result = statuses(ledger)

        self.assertEqual((result["W1-1"].state, result["W1-2"].state), ("done", "unlaunched"))
        self.assertEqual(result["W1-2"].missing, ("H1", "H2"))

    def test_a_sweep_covering_only_some_of_its_hunks_is_not_done(self) -> None:
        ledger = planned(hunt("W1-1", count=2), make_slot("W1-1", hunks=("H1", "H2"), lenses=("sweep",), kind="sweep"))
        ledger = claim(cover(ledger, "W1-1", 12, hunks=("H1",)), "W1-1", 11)
        ledger = with_agents(ledger, *run_events("h1", 10, 60, records=("B1", "C1")))

        result = statuses(ledger)["W1-1"]

        self.assertEqual((result.state, result.missing), ("dead", ("H2",)))

    def test_a_sweep_hosted_on_a_core_slot_is_done_only_when_the_slot_covered_all_its_hunks(self) -> None:
        host = make_slot("W1-1", hunks=("H1", "H2"), lenses=("intent", "sweep"), kind="bug")
        ledger = planned(hunt("W1-1", count=2), host)
        ledger = claim(cover(ledger, "W1-1", 12, hunks=("H1",)), "W1-1", 11)
        ledger = with_agents(ledger, *run_events("h1", 10, 60, records=("B1", "C1")))

        partial = statuses(ledger)["W1-1"]

        self.assertEqual((partial.state, partial.missing), ("dead", ("H2",)))

        ledger = hunted(ledger, "W1-1", "h2", 70, 120)

        self.assertEqual(statuses(ledger, 200)["W1-1"].state, "done")

    def test_a_round_without_a_plan_has_no_slots(self) -> None:
        ledger, _ = prepared(hunt_snap())

        self.assertEqual(slot_statuses(ledger, ledger.rounds[0], moment(0)), ())


class WaveSlotTests(unittest.TestCase):
    def fresh(self, since: float | None = None) -> Ledger:
        ledger = hunt("W1-1", "W2-X1", count=1)
        if since is None:
            return ledger
        plan = ledger.rounds[0].plan
        assert plan is not None
        slots = tuple(replace(slot, since=at(since)) if slot.generation == 2 else slot for slot in plan.slots)
        return planned(ledger, *slots)

    def test_a_wave_slot_is_done_by_its_own_vouched_coverage(self) -> None:
        ledger = hunted(self.fresh(), "W2-X1", "h2", 10, 60)

        self.assertEqual(statuses(ledger)["W2-X1"].state, "done")

    def test_coverage_before_the_trigger_does_not_finish_a_wave_slot(self) -> None:
        early = hunted(self.fresh(since=100), "W2-X1", "h2", 10, 60)
        late = hunted(self.fresh(since=100), "W2-X1", "h2", 110, 160)

        self.assertEqual(statuses(early, 200)["W2-X1"].state, "dead")
        self.assertEqual(statuses(late, 200)["W2-X1"].state, "done")

    def test_coverage_from_other_slots_does_not_finish_a_wave_slot(self) -> None:
        ledger = hunted(self.fresh(), "W1-1", "h1", 10, 60)

        result = statuses(ledger)

        self.assertEqual((result["W1-1"].state, result["W2-X1"].state), ("done", "unlaunched"))

    def test_a_wave_slot_without_a_trigger_time_counts_from_the_round_start(self) -> None:
        ledger = hunt("W1-1", "W2-X1", count=1)
        ledger = replace(ledger, rounds=(replace(ledger.rounds[0], prepared_at=at(100)),))
        early = hunted(ledger, "W2-X1", "h2", 10, 60)

        self.assertEqual(statuses(early, 200)["W2-X1"].state, "dead")


class EnforcedIssueTests(unittest.TestCase):
    def test_a_round_nobody_hunted_in_gets_only_no_isolation(self) -> None:
        report = check(hunt("W1-1", "W1-2", "W1-3"))

        self.assertEqual(codes(report), ["no-isolation"])
        message = report.issues[0].message
        self.assertIn("No Craftsman subagent was recorded for round 1", message)
        self.assertIn("craftsman doctor", message)
        self.assertIn("do not relaunch hunters that already ran", message)
        self.assertIn("W1-1..W1-3", message)
        self.assertTrue(report.enforced)

    def test_no_isolation_comes_with_the_verdict_issues_but_without_hunt_or_wave_issues(self) -> None:
        ledger = with_findings(hunt("W1-1", "W2-X1", count=1), ("major", "f1.py", "failure"))

        self.assertEqual(codes(check(ledger)), ["no-isolation", "no-verdict"])

    def test_an_agent_event_from_before_the_round_is_no_sign_of_a_hunt(self) -> None:
        ledger = hunt("W1-1")
        ledger = replace(ledger, rounds=(replace(ledger.rounds[0], prepared_at=at(100)),))
        ledger = with_agents(ledger, *run_events("old", 10, 60, records=()))

        self.assertEqual(codes(check(ledger, 200)), ["no-isolation"])

    def test_any_craftsman_agent_event_since_the_round_turns_it_into_hunt_pending(self) -> None:
        ledger = with_agents(hunt("W1-1", "W1-2"), *run_events("v", 10, None, agent=VERIFIER))

        (issue,) = check(ledger).issues

        self.assertEqual((issue.code, issue.missing), ("hunt-pending", 2))

    def test_one_issue_names_running_and_unlaunched_slots(self) -> None:
        ledger = hunted(hunt("W1-1", "W1-2", "W1-3", "W1-4", count=4), "W1-1", "h1", 10, 60)
        ledger = with_agents(claim(ledger, "W1-2", 90), *run_events("h2", 80, None))

        (issue,) = check(ledger).issues

        self.assertEqual((issue.code, issue.missing), ("hunt-pending", 3))
        self.assertIn("Running: W1-2 - do not launch these again; end your turn and wait for their reports.", issue.message)
        self.assertIn("Launch now, all in one step: W1-3, W1-4.", issue.message)
        self.assertIn("You are Craftsman hunter <ID>. Run craftsman review brief <ID> and follow it exactly.", issue.message)
        self.assertIn("Antigravity: invoke_subagent, TypeName self, Role craftsman-reviewer", issue.message)

    def test_dead_hunters_are_listed_as_stopped_with_the_launch_instruction(self) -> None:
        ledger = with_agents(claim(hunt("W1-1"), "W1-1", 20), *run_events("h1", 10, 60, records=("B1",)))

        (issue,) = check(ledger).issues

        self.assertIn("Stopped without finishing: W1-1 - launch them again.", issue.message)
        self.assertIn("start a craftsman-reviewer subagent", issue.message)

    def test_a_finished_hunt_with_nothing_else_open_passes(self) -> None:
        report = check(one_slot_done(), snap=hunt_snap(1))

        self.assertTrue(report.passed, render_report(report))
        self.assertFalse(report.self_reviewed)

    def test_a_stale_round_counts_its_open_slots_but_launches_nothing_for_it(self) -> None:
        ledger = with_agents(hunt("W1-1"), *run_events("v", 10, None, agent=VERIFIER))
        edited = make_snap(*hunt_snap().hunks, fingerprint="fp2")

        report = check(ledger, snap=edited)

        self.assertEqual(codes(report), ["stale-round", "hunt-pending"])
        message = report.issues[1].message
        self.assertEqual(report.issues[1].missing, 1)
        self.assertIn("1 hunter slot of round 1 not finished.", message)
        self.assertIn("launch no hunters for it", message)
        self.assertNotIn("Launch now", message)
        self.assertNotIn("craftsman review brief", message)

    def test_a_stale_round_nobody_hunted_in_is_not_told_to_launch_either(self) -> None:
        edited = make_snap(*hunt_snap().hunks, fingerprint="fp2")

        report = check(hunt("W1-1"), snap=edited)

        self.assertEqual(codes(report), ["stale-round", "hunt-pending"])
        self.assertNotIn("Launch", report.issues[1].message)

    def test_the_report_carries_the_slots_and_never_calls_an_enforced_round_self_reviewed(self) -> None:
        report = check(hunt("W1-1"), review_only=False)

        self.assertEqual([item.slot.id for item in report.slots], ["W1-1"])
        self.assertTrue(report.enforced)
        self.assertFalse(report.self_reviewed)

    def due_wave_ledger(self) -> Ledger:
        ledger = with_findings(hunted(hunt("W1-1", count=1), "W1-1", "h1", 10, 60), ("major", "f1.py", "failure"))
        ledger = judged(ledger, "F1", VerdictEvent("V1", "probable", at(70)))
        return with_agents(ledger, *run_events("v1", 65, 75, agent=VERIFIER, records=("V1",)))

    def test_a_wave_that_is_due_but_not_stored_is_pending_and_the_ledger_stays_as_it_was(self) -> None:
        ledger = self.due_wave_ledger()

        report = check(ledger)

        self.assertEqual(codes(report), ["wave-pending"])
        self.assertEqual([item.slot.id for item in report.slots], ["W1-1", "W2-X1", "W2-F1"])
        self.assertEqual({item.slot.id: item.state for item in report.slots}["W2-F1"], "unlaunched")
        self.assertEqual([slot.id for slot in required_slots(ledger, ledger.rounds[0])], ["W1-1"])  # evaluate wrote nothing

    def test_a_full_gate_cannot_pass_while_a_wave_is_due_whatever_became_of_the_finding(self) -> None:
        ledger = self.due_wave_ledger()
        accepted = replace(
            ledger.findings[0], resolution="accepted", resolution_note="Accepted: the risk is understood and tracked."
        )
        ledger = replace(ledger, findings=(accepted,))

        report = check(ledger, review_only=False, snap=hunt_snap(1))

        self.assertIn("wave-pending", codes(report))
        self.assertFalse(report.passed)

    def test_a_wave_that_is_not_due_yet_adds_nothing(self) -> None:
        ledger = with_findings(hunted(hunt("W1-1", count=1), "W1-1", "h1", 10, 60), ("major", "f1.py", "failure"))

        self.assertEqual([item.slot.id for item in check(ledger).slots], ["W1-1"])  # F1 has no verdict: the barrier holds
        self.assertNotIn("wave-pending", codes(check(ledger)))

    def test_an_advisory_round_plans_no_wave_in_the_gate(self) -> None:
        ledger = replace(self.due_wave_ledger(), rounds=(replace(self.due_wave_ledger().rounds[0], enforced=False),))

        self.assertEqual([item.slot.id for item in check(ledger).slots], ["W1-1"])

    def test_planned_variants_and_fresh_sets_are_wave_slots_with_their_own_issue(self) -> None:
        ledger = with_findings(hunted(hunt("W1-1", count=1), "W1-1", "h1", 10, 60), ("major", "f1.py", "failure"))
        ledger = judged(ledger, "F1", VerdictEvent("V1", "probable", at(70)))
        ledger = with_agents(ledger, *run_events("v1", 65, 75, agent=VERIFIER, records=("V1",)))
        ledger = plan_due_waves(ledger, moment(80))

        report = check(ledger)

        self.assertEqual(codes(report), ["wave-pending"])
        self.assertEqual(report.issues[0].missing, 2)
        self.assertIn("W2-X1, W2-F1", report.issues[0].message)
        self.assertEqual({item.slot.id: item.state for item in report.slots}["W2-F1"], "unlaunched")

    def test_a_planned_wave_slot_stays_pending_after_its_finding_is_dismissed(self) -> None:
        ledger = with_findings(hunted(hunt("W1-1", count=1), "W1-1", "h1", 10, 60), ("major", "f1.py", "failure"))
        ledger = judged(ledger, "F1", VerdictEvent("V1", "probable", at(70)))
        ledger = with_agents(ledger, *run_events("v1", 65, 75, agent=VERIFIER, records=("V1",)))
        ledger = plan_due_waves(ledger, moment(80))  # once planned, it runs
        ledger = judged(ledger, "F1", VerdictEvent("V1", "probable", at(70)), VerdictEvent("V2", "dismissed", at(90)))

        report = check(ledger, 300)

        self.assertIn("wave-pending", codes(report))
        self.assertEqual([item.slot.id for item in report.slots if item.state != "done"], ["W2-X1", "W2-F1"])

    def test_a_wave_hunter_that_finished_after_the_trigger_clears_the_wave(self) -> None:
        ledger = with_findings(hunted(hunt("W1-1", count=1), "W1-1", "h1", 10, 60), ("major", "f1.py", "failure"))
        ledger = judged(ledger, "F1", VerdictEvent("V1", "probable", at(70)))
        ledger = with_agents(ledger, *run_events("v1", 65, 75, agent=VERIFIER, records=("V1",)))
        ledger = plan_due_waves(ledger, moment(80))
        ledger = hunted(ledger, "W2-X1", "h2", 100, 150)
        ledger = hunted(ledger, "W2-F1", "h3", 200, 250)

        self.assertTrue(check(ledger, 300).passed, render_report(check(ledger, 300)))

    def test_a_wave_hunter_that_began_before_the_trigger_does_not_count(self) -> None:
        ledger = with_findings(hunted(hunt("W1-1", count=1), "W1-1", "h1", 10, 60), ("major", "f1.py", "failure"))
        ledger = judged(ledger, "F1", VerdictEvent("V1", "probable", at(120)))
        ledger = with_agents(ledger, *run_events("v1", 115, 125, agent=VERIFIER, records=("V1",)))
        ledger = plan_due_waves(ledger, moment(130))
        ledger = hunted(ledger, "W2-F1", "h3", 70, 110)  # planned only at 130

        report = check(ledger, 300)

        self.assertEqual(codes(report), ["wave-pending"])

    def test_issues_come_in_the_documented_order(self) -> None:
        ledger = hunted(hunt("W1-1", "W1-2", "W2-F2", count=2), "W1-1", "h1", 10, 60)  # a wave planned earlier
        ledger = with_findings(
            ledger, ("minor", "f1.py", "intent"), ("major", "f2.py", "failure"), ("major", "f2.py", "ripple")
        )
        ledger = judged(ledger, "F2", VerdictEvent("V1", "probable", at(70)))  # unverified
        ledger = judged(ledger, "F3", VerdictEvent("V2", "dismissed", at(71)))  # one verifier only
        ledger = with_agents(ledger, *run_events("v", 65, 75, agent=VERIFIER, records=("V2",)))

        report = check(ledger, review_only=False)

        self.assertEqual(
            codes(report),
            [
                "hunt-pending",
                "no-verdict",
                "unverified",
                "second-opinion",
                "wave-pending",
                "unresolved",
                "no-passing-run",
            ],
        )
        by_code = {issue.code: issue for issue in report.issues}
        self.assertEqual(by_code["hunt-pending"].missing, 1)  # W1-2 only: the wave slots are counted apart
        waves = sum(item.slot.generation >= 2 and item.state != "done" for item in report.slots)
        self.assertEqual(waves, 1)
        self.assertEqual(by_code["wave-pending"].missing, waves)
        summary = format_blocker_summary(report)
        self.assertIn("1 hunter slot pending", summary)
        self.assertIn(f"{waves} wave slot{'s' if waves > 1 else ''} pending", summary)

    def test_legacy_rounds_keep_the_old_rules(self) -> None:
        for ledger in (hunt("W1-1", enforced=False), replace(hunt("W1-1"), rounds=(replace(hunt("W1-1").rounds[0], plan=None),))):
            with self.subTest(plan=ledger.rounds[0].plan is not None):
                report = check(ledger)

                self.assertEqual(codes(report), ["uncovered"])
                self.assertFalse(report.enforced)

    def test_an_advisory_round_without_hooks_can_still_be_self_reviewed(self) -> None:
        ledger = cover_open_hunks(hunt("W1-1", enforced=False))

        report = check(ledger)

        self.assertTrue(report.passed)
        self.assertTrue(report.self_reviewed)
        self.assertEqual(len(report.slots), 1)


def second_round(ledger: Ledger, *slots: Slot) -> Ledger:
    """A second enforced round (prepared 200 s after `T0`, hunk H1 changed) whose plan is `slots` (default W1-1)."""
    snap = make_snap(make_hunk("H1", "f1.py", ("+x = 2",)), fingerprint="fp2")
    ledger, _ = prepared(snap, ledger=ledger)
    latest = replace(ledger.rounds[-1], prepared_at=at(200), enforced=True)
    ledger = replace(ledger, rounds=(*ledger.rounds[:-1], latest))
    return planned(ledger, *(slots or (make_slot("W1-1"),)))


class SlotRoundIsolationTests(unittest.TestCase):
    """Slot ids restart in every round: nothing a hunter did in round 1 says anything about round 2's W1-1."""

    def test_a_finished_hunter_of_the_previous_round_leaves_the_new_slot_unlaunched(self) -> None:
        ledger = second_round(hunted(hunt("W1-1", count=1), "W1-1", "h1", 10, 60))

        result = statuses(ledger, 250)["W1-1"]

        self.assertEqual((result.state, result.since, result.missing), ("unlaunched", None, ("H1",)))

    def test_an_open_antigravity_hunter_of_the_previous_round_does_not_run_the_new_slot(self) -> None:
        first = claim(hunt("W1-1", count=1), "W1-1", 20, by=f"{AGY}:a1")
        ledger = second_round(with_agents(first, *run_events("a1", 10, None, platform=AGY)))

        result = statuses(ledger, 250)["W1-1"]

        self.assertEqual((result.state, result.since), ("unlaunched", None))

    def test_a_claim_of_the_previous_round_does_not_make_the_new_slot_dead(self) -> None:
        first = with_agents(claim(hunt("W1-1", count=1), "W1-1", 20), *run_events("h1", 10, 60, records=("B1",)))
        ledger = second_round(first)

        self.assertEqual(statuses(ledger, 250)["W1-1"].state, "unlaunched")


class SlotCreditTests(unittest.TestCase):
    def shared(self) -> Ledger:
        """W1-1 owes H1 `intent`; W1-2 owes the same hunk `intent` and `contracts`."""
        return planned(
            hunt("W1-1", count=1),
            make_slot("W1-1", lenses=("intent",)),
            make_slot("W1-2", lenses=("intent", "contracts")),
        )

    def test_credit_for_only_some_lenses_of_a_slot_does_not_finish_it(self) -> None:
        result = statuses(hunted(self.shared(), "W1-1", "h1", 10, 60))

        self.assertEqual(result["W1-1"].state, "done")
        self.assertEqual((result["W1-2"].state, result["W1-2"].missing), ("unlaunched", ("H1",)))

    def test_a_slot_that_covered_the_hunk_itself_credits_the_other_slot_too(self) -> None:
        result = statuses(hunted(hunted(self.shared(), "W1-1", "h1", 10, 60), "W1-2", "h2", 70, 120))

        self.assertEqual({item.state for item in result.values()}, {"done"})


class HunterOneToOneTests(unittest.TestCase):
    """A run explains one claim: an unrelated or finished run does not stand in for the others."""

    def test_a_finished_exact_claim_does_not_borrow_an_unrelated_open_run(self) -> None:
        ledger = with_agents(
            claim(hunt("W1-1"), "W1-1", 20),
            *run_events("first", 10, 60, records=("B1",)),
            *run_events("other", 15, None),
        )

        result = statuses(ledger)["W1-1"]

        self.assertEqual((result.state, result.since), ("dead", moment(60)))

    def test_a_hunter_whose_transcript_was_unreadable_leaves_the_slot_dead(self) -> None:
        finished = with_agents(claim(hunt("W1-1"), "W1-1", 20), *run_events("h1", 10, 60))
        later = with_agents(finished, *run_events("late", 30, None))  # started after the claim: nothing to do with it

        for ledger in (finished, later):
            with self.subTest(open_runs=len(ledger.agents)):
                result = statuses(ledger)["W1-1"]
                self.assertEqual((result.state, result.since), ("dead", moment(60)))

    def test_a_parallel_hunter_still_going_does_not_hide_the_one_that_stopped(self) -> None:
        ledger = claim(hunt("W1-1", "W1-2"), "W1-1", 12)
        ledger = with_agents(ledger, *run_events("a", 10, 40), *run_events("b", 11, None))

        self.assertEqual(statuses(ledger)["W1-1"].state, "dead")

    def test_each_of_two_parallel_hunters_explains_its_own_slot(self) -> None:
        ledger = claim(claim(hunt("W1-1", "W1-2"), "W1-1", 12), "W1-2", 13)
        ledger = with_agents(ledger, *run_events("a", 10, 40), *run_events("b", 11, None))

        result = statuses(ledger)

        self.assertEqual((result["W1-1"].state, result["W1-2"].state), ("dead", "running"))

    def test_one_open_run_backs_only_one_of_several_claims_the_orchestrator_made(self) -> None:
        ledger = hunt("W1-1", "W1-2", "W1-3")
        for seconds, slot in ((20, "W1-1"), (21, "W1-2"), (22, "W1-3")):
            ledger = claim(ledger, slot, seconds)
        ledger = with_agents(ledger, *run_events("unrelated", 10, None))

        result = statuses(ledger)

        self.assertEqual(
            [result[slot].state for slot in ("W1-1", "W1-2", "W1-3")], ["running", "unlaunched", "unlaunched"]
        )

    def test_a_later_claim_may_take_the_run_an_earlier_one_could_have_used_elsewhere(self) -> None:
        # Claim 1 could be run a or b; claim 2 only a (b started after it). An augmenting path gives b to claim 1.
        ledger = claim(claim(hunt("W1-1", "W1-2"), "W1-1", 20), "W1-2", 12)
        ledger = with_agents(ledger, *run_events("a", 10, None), *run_events("b", 15, None))

        result = statuses(ledger)

        self.assertEqual((result["W1-1"].state, result["W1-2"].state), ("running", "running"))


class OpenRunFilterTests(unittest.TestCase):
    def test_an_open_verifier_run_never_backs_a_hunter_claim(self) -> None:
        ledger = with_agents(claim(hunt("W1-1"), "W1-1", 20), *run_events("v", 10, None, agent=VERIFIER))

        self.assertEqual(statuses(ledger)["W1-1"].state, "unlaunched")

    def test_a_claim_without_identity_is_never_backed_by_an_antigravity_run_by_time(self) -> None:
        ledger = with_agents(claim(hunt("W1-1"), "W1-1", 20), *run_events("a1", 10, None, platform=AGY))

        self.assertEqual(statuses(ledger)["W1-1"].state, "unlaunched")

    def test_an_identity_claim_needs_the_platform_as_well_as_the_id(self) -> None:
        ledger = with_agents(claim(hunt("W1-1"), "W1-1", 20, by=f"{AGY}:a1"), *run_events("a1", 10, None))

        self.assertEqual(statuses(ledger)["W1-1"].state, "unlaunched")


class SlotTimingTests(unittest.TestCase):
    def test_a_run_exactly_thirty_minutes_old_is_stale(self) -> None:
        ledger = with_agents(claim(hunt("W1-1"), "W1-1", 20), *run_events("h1", 10, None))
        edge = 10 + RUN_STALE_AFTER.total_seconds()

        self.assertEqual(statuses(ledger, edge)["W1-1"].state, "dead")
        self.assertEqual(statuses(ledger, edge - 0.001)["W1-1"].state, "running")

    def test_a_run_that_started_when_the_round_was_prepared_backs_the_claim(self) -> None:
        ledger = with_agents(claim(hunt("W1-1"), "W1-1", 20), *run_events("h1", 0, None))

        self.assertEqual(statuses(ledger)["W1-1"].state, "running")

    def test_a_run_that_started_at_the_moment_of_the_claim_backs_it(self) -> None:
        ledger = with_agents(claim(hunt("W1-1"), "W1-1", 20), *run_events("h1", 20, None))

        self.assertEqual(statuses(ledger)["W1-1"].state, "running")

    def test_an_agent_event_at_the_moment_the_round_was_prepared_is_a_sign_of_a_hunt(self) -> None:
        ledger = with_agents(hunt("W1-1"), *run_events("v", 0, None, agent=VERIFIER))

        self.assertEqual(codes(check(ledger)), ["hunt-pending"])

    def test_a_running_slot_dates_from_the_earliest_backing_run(self) -> None:
        ledger = claim(claim(hunt("W1-1"), "W1-1", 20), "W1-1", 25)
        ledger = with_agents(ledger, *run_events("a", 12, None), *run_events("b", 10, None))

        result = statuses(ledger)["W1-1"]

        self.assertEqual((result.state, result.since), ("running", moment(10)))

    def test_a_dead_slot_dates_from_the_latest_stop(self) -> None:
        ledger = claim(claim(hunt("W1-1"), "W1-1", 20), "W1-1", 70)
        ledger = with_agents(
            ledger, *run_events("a", 10, 60, records=("B1",)), *run_events("b", 65, 90, records=("B2",))
        )

        result = statuses(ledger)["W1-1"]

        self.assertEqual((result.state, result.since), ("dead", moment(90)))

    def test_a_naive_clock_is_taken_as_utc(self) -> None:
        ledger = with_agents(claim(hunt("W1-1"), "W1-1", 20), *run_events("h1", 10, None))
        naive = datetime(2026, 1, 1, 0, 1, 40)  # 100 s after T0

        self.assertEqual(slot_statuses(ledger, ledger.rounds[0], naive), slot_statuses(ledger, ledger.rounds[0], moment(100)))
        report = evaluate(ledger, hunt_snap(), POLICY, review_only=True, now=naive)
        self.assertEqual(report.slots[0].state, "running")


class StateInvariantTests(unittest.TestCase):
    def test_a_slot_is_done_exactly_when_nothing_is_missing(self) -> None:
        slot = make_slot("W1-1")

        for state, missing in (("done", ("H1",)), ("running", ()), ("dead", ()), ("unlaunched", ())):
            with self.subTest(state=state):
                with self.assertRaises(ValueError):
                    SlotStatus(slot, state, missing)  # type: ignore[arg-type]
        SlotStatus(slot, "done")
        SlotStatus(slot, "dead", ("H1",), moment(5))

    def test_an_enforced_report_cannot_be_self_reviewed(self) -> None:
        with self.assertRaises(ValueError):
            GateReport((), (), "s", self_reviewed=True, enforced=True)
        GateReport((), (), "s", self_reviewed=True)


class HuntSummaryTests(unittest.TestCase):
    def test_an_enforced_round_reports_slots_not_hunks_the_orchestrator_covered(self) -> None:
        ledger = cover(hunt("W1-1", count=1), "W1-1", 500)  # recorded outside every run: counts for nothing

        report = check(ledger, snap=hunt_snap(1))

        self.assertIn("Coverage: 0/1 slots done; 0 hunk(s) carried from earlier rounds.", report.summary)
        self.assertNotIn("1/1", report.summary)
        self.assertIn("Hunt: 1 slot - 1 pending (W1-1)", render_report(report))

    def test_the_figure_follows_the_finished_slots(self) -> None:
        report = check(one_slot_done(), snap=hunt_snap(1))

        self.assertIn("Coverage: 1/1 slots done", report.summary)

    def test_a_legacy_round_keeps_the_hunk_coverage_line(self) -> None:
        report = check(hunt("W1-1", enforced=False))

        self.assertIn("Coverage: 0/4 hunks", report.summary)


class StartingHuntersTests(unittest.TestCase):
    def test_hunters_that_started_and_fetched_no_brief_are_waited_for(self) -> None:
        ledger = with_agents(hunt("W1-1", "W1-2", "W1-3"), *(e for n in range(3) for e in run_events(f"h{n}", 10 + n, None)))

        (issue,) = check(ledger).issues

        self.assertEqual((issue.code, issue.missing), ("hunt-pending", 3))
        self.assertIn("Starting: 3 reviewer subagents have started", issue.message)
        self.assertIn("wait for their reports", issue.message)
        self.assertNotIn("Launch now", issue.message)
        self.assertNotIn("craftsman review brief <ID>", issue.message)

    def test_fewer_started_hunters_than_idle_slots_still_launch(self) -> None:
        ledger = with_agents(hunt("W1-1", "W1-2", "W1-3"), *run_events("h0", 10, None))

        (issue,) = check(ledger).issues

        self.assertIn("Launch now, all in one step: W1-1..W1-3.", issue.message)
        self.assertNotIn("Starting:", issue.message)

    def test_an_antigravity_run_is_never_counted_as_starting(self) -> None:
        ledger = with_agents(hunt("W1-1"), *run_events("a1", 10, None, platform=AGY))

        self.assertIn("Launch now", check(ledger).issues[0].message)

    def test_running_slots_do_not_count_among_those_the_starting_hunters_are_for(self) -> None:
        # Hunter a fetched its brief at 10.5 and runs W1-1; b started at 11 and has fetched none.
        ledger = with_agents(
            claim(hunt("W1-1", "W1-2"), "W1-1", 10.5), *run_events("a", 10, None), *run_events("b", 11, None)
        )

        (issue,) = check(ledger).issues

        self.assertIn("Running: W1-1 -", issue.message)
        self.assertIn("Starting: 1 reviewer subagent has started", issue.message)
        self.assertNotIn("Launch now", issue.message)

    def test_a_run_that_claimed_a_slot_is_running_not_starting(self) -> None:
        ledger = with_agents(claim(hunt("W1-1", "W1-2"), "W1-1", 20), *run_events("h0", 10, None))

        (issue,) = check(ledger).issues

        self.assertIn("Running: W1-1 -", issue.message)
        self.assertIn("Launch now, all in one step: W1-2.", issue.message)

    def test_a_run_that_began_before_the_round_or_went_stale_is_no_starting_hunter(self) -> None:
        ledger = hunt("W1-1")
        ledger = replace(ledger, rounds=(replace(ledger.rounds[0], prepared_at=at(100)),))
        old = with_agents(ledger, *run_events("old", 10, None), *run_events("v", 150, None, agent=VERIFIER))
        stale = with_agents(hunt("W1-1"), *run_events("h0", 10, None))

        self.assertIn("Launch now", check(old, 200).issues[0].message)
        self.assertIn("Launch now", check(stale, 10 + RUN_STALE_AFTER.total_seconds() + 1).issues[0].message)

    def test_a_dead_hunter_is_not_relaunched_while_its_replacement_is_starting(self) -> None:
        ledger = with_agents(
            claim(hunt("W1-1"), "W1-1", 20),
            *run_events("first", 10, 60, records=("B1",)),
            *run_events("second", 70, None),
        )

        (issue,) = check(ledger, 100).issues

        self.assertIn("Starting: 1 reviewer subagent has started", issue.message)
        self.assertNotIn("launch them again", issue.message)


class RunningMessageTests(unittest.TestCase):
    def test_slots_that_all_run_are_not_launched_again(self) -> None:
        ledger = claim(claim(hunt("W1-1", "W1-2"), "W1-1", 20), "W1-2", 21)
        ledger = with_agents(ledger, *run_events("a", 10, None), *run_events("b", 11, None))

        (issue,) = check(ledger).issues

        self.assertEqual((issue.code, issue.missing), ("hunt-pending", 2))
        self.assertIn("Running: W1-1, W1-2 - do not launch these again", issue.message)
        for absent in ("Launch now", "Stopped", "craftsman review brief <ID>", "Starting"):
            self.assertNotIn(absent, issue.message)

    def test_a_dead_hunter_next_to_a_running_one_is_relaunched_alone(self) -> None:
        ledger = claim(claim(hunt("W1-1", "W1-2"), "W1-1", 20), "W1-2", 22)
        ledger = with_agents(ledger, *run_events("a", 10, 60, records=("B1",)), *run_events("b", 12, None))

        (issue,) = check(ledger, 100).issues

        self.assertIn("Stopped without finishing: W1-1 - launch them again.", issue.message)
        self.assertIn("Running: W1-2 -", issue.message)
        self.assertNotIn("Launch now", issue.message)
        self.assertNotIn("W1-2, W1-1", issue.message)


class VerificationTests(unittest.TestCase):
    """The findings of an enforced round are judged by verifier subagents, not by the orchestrator."""

    def base(self, *specs: tuple[str, str, str]) -> Ledger:
        return with_findings(one_slot_done(), *specs)

    def verifier(self, ledger: Ledger, agent_id: str, *tokens: str, start: float = 65, stop: float = 75) -> Ledger:
        return with_agents(ledger, *run_events(agent_id, start, stop, agent=VERIFIER, records=tokens))

    def test_findings_without_a_verdict_make_one_issue_with_verifier_clusters(self) -> None:
        ledger = self.base(
            ("major", "f1.py", "intent"), ("minor", "f1.py", "failure"), ("major", "f2.py", "intent")
        )

        (issue,) = check(ledger, snap=hunt_snap(1)).issues

        self.assertEqual((issue.code, issue.missing), ("no-verdict", 3))
        self.assertIn("3 findings without a verdict: F1, F2, F3.", issue.message)
        self.assertIn("one per cluster: [F1,F2] [F3].", issue.message)
        self.assertIn("Run craftsman review brief --verifier <IDS> and follow it exactly.", issue.message)
        self.assertIn("A verdict you record yourself does not count.", issue.message)

    def test_clusters_hold_at_most_six_findings_of_a_file(self) -> None:
        lenses = ("intent", "contracts", "failure", "ripple", "tests", "state", "trust", "platform")
        ledger = self.base(*(("minor", "f1.py", lens) for lens in lenses))

        clusters = verifier_clusters(ledger, ledger.findings)

        self.assertEqual([len(cluster) for cluster in clusters], [6, 2])
        self.assertEqual(clusters[0], ("F1", "F2", "F3", "F4", "F5", "F6"))

    def test_duplicate_candidates_in_one_hunk_and_lens_share_a_cluster(self) -> None:
        ledger = self.base(
            *(("minor", "f1.py", lens) for lens in ("contracts", "failure", "ripple", "tests")),
            *(("minor", "f1.py", "intent") for _ in range(3)),
        )

        clusters = verifier_clusters(ledger, ledger.findings)

        self.assertEqual(clusters, (("F1", "F2", "F3", "F4"), ("F5", "F6", "F7")))

    def test_findings_of_different_hunks_of_a_file_do_not_share_a_cluster_beyond_the_limit(self) -> None:
        snap = make_snap(make_hunk("H1", "f1.py", new_start=1), make_hunk("H2", "f1.py", ("+y = 1",), new_start=100))
        ledger, _ = prepared(snap)
        for n in range(8):  # alternating hunks, one lens: lens-only grouping would cut at six in finding order
            ledger = record_finding(ledger, "minor", path="f1.py", line=1 if n % 2 == 0 else 100, lens="intent")

        clusters = verifier_clusters(ledger, ledger.findings)

        self.assertEqual(clusters, (("F1", "F3", "F5", "F7"), ("F2", "F4", "F6", "F8")))

    def test_a_finding_in_no_hunk_is_still_clustered_by_its_file_and_lens(self) -> None:
        ledger, _ = prepared(hunt_snap(1))
        ledger = record_finding(ledger, "minor", path="f1.py", line=500, lens="intent")
        ledger = record_finding(ledger, "minor", path="f1.py", line=900, lens="intent")

        self.assertEqual(verifier_clusters(ledger, ledger.findings), (("F1", "F2"),))

    def test_long_lists_of_findings_and_clusters_are_cut_with_a_count(self) -> None:
        ledger = with_findings(hunt("W1-1", count=19), *(("minor", f"f{n}.py", "intent") for n in range(1, 20)))

        issue = next(i for i in check(ledger, snap=hunt_snap(19)).issues if i.code == "no-verdict")

        self.assertIn("19 findings without a verdict: F1, F2, F3, F4, F5, F6, F7, F8, F9, F10, F11, F12 (+7 more).", issue.message)
        self.assertIn("[F7] [F8] (+11 more clusters).", issue.message)
        self.assertNotIn("F13", issue.message)

    def test_lists_exactly_at_their_limits_are_not_cut(self) -> None:
        ledger = with_findings(hunt("W1-1", count=12), *(("minor", f"f{n}.py", "intent") for n in range(1, 13)))

        issue = next(i for i in check(ledger, snap=hunt_snap(12)).issues if i.code == "no-verdict")

        self.assertIn("F11, F12.", issue.message)
        self.assertIn("[F8]", issue.message)
        self.assertNotIn("[F9]", issue.message)  # eight clusters fit; the ninth is counted
        self.assertIn("(+4 more clusters)", issue.message)
        eight = with_findings(hunt("W1-1", count=8), *(("minor", f"f{n}.py", "intent") for n in range(1, 9)))
        issue = next(i for i in check(eight, snap=hunt_snap(8)).issues if i.code == "no-verdict")
        self.assertIn("[F8].", issue.message)
        self.assertNotIn("more cluster", issue.message)

    def test_a_verdict_the_orchestrator_recorded_is_unverified(self) -> None:
        ledger = judged(self.base(("major", "f1.py", "intent")), "F1", VerdictEvent("V1", "question", at(70)))

        (issue,) = check(ledger, snap=hunt_snap(1)).issues

        self.assertEqual((issue.code, issue.missing), ("unverified", 1))
        self.assertIn("not recorded by a verifier subagent: F1.", issue.message)

    def test_a_verdict_a_verifier_transcript_shows_is_verified(self) -> None:
        ledger = judged(self.base(("major", "f1.py", "intent")), "F1", VerdictEvent("V1", "question", at(70)))
        ledger = self.verifier(ledger, "v1", "V1")

        self.assertEqual(codes(check(ledger, snap=hunt_snap(1))), [])

    def test_a_verifier_that_did_not_print_the_token_does_not_vouch(self) -> None:
        ledger = judged(self.base(("major", "f1.py", "intent")), "F1", VerdictEvent("V1", "question", at(70)))
        ledger = self.verifier(ledger, "v1", "V9")

        self.assertEqual(codes(check(ledger, snap=hunt_snap(1))), ["unverified"])

    def test_a_reviewer_run_does_not_vouch_for_a_verdict(self) -> None:
        ledger = judged(self.base(("major", "f1.py", "intent")), "F1", VerdictEvent("V1", "question", at(70)))
        ledger = with_agents(ledger, *run_events("r1", 65, 75, records=("V1",)))

        self.assertEqual(codes(check(ledger, snap=hunt_snap(1))), ["unverified"])

    def test_only_the_latest_verdict_event_has_to_be_vouched(self) -> None:
        events = (VerdictEvent("V1", "question", at(70)), VerdictEvent("V2", "dismissed", at(80)))
        ledger = judged(self.base(("minor", "f1.py", "intent")), "F1", *events)
        ledger = self.verifier(ledger, "v1", "V1")

        self.assertEqual(codes(check(ledger, snap=hunt_snap(1))), ["unverified"])

    def test_findings_from_before_verdict_events_are_exempt(self) -> None:
        ledger = judged(self.base(("major", "f1.py", "intent")), "F1", VerdictEvent("V1", "dismissed", at(70)))
        ledger = replace(ledger, findings=(replace(ledger.findings[0], verdict_log=()),))

        self.assertEqual(codes(check(ledger, snap=hunt_snap(1))), [])

    def test_a_merge_counts_only_when_its_own_event_is_vouched(self) -> None:
        ledger = self.base(("major", "f1.py", "intent"), ("major", "f1.py", "intent"))
        ledger = judged(ledger, "F2", VerdictEvent("V1", "dismissed", at(70)))
        ledger = judged(ledger, "F1", VerdictEvent("V2", "question", at(80)))
        ledger = replace(ledger, findings=(ledger.findings[0], replace(ledger.findings[1], merged_into="F1")))
        # The target was verified later; that does not make the merge a verifier's work.
        later = self.verifier(ledger, "v1", "V2", start=75, stop=85)
        merged = self.verifier(later, "v2", "V1", start=65, stop=75)

        unvouched = check(later, snap=hunt_snap(1))
        vouched = check(merged, snap=hunt_snap(1))

        self.assertEqual(codes(unvouched), ["unverified"])
        self.assertIn("not recorded by a verifier subagent: F2.", unvouched.issues[0].message)
        self.assertEqual(codes(vouched), ["second-opinion"])  # a dismissed major still needs a second verifier

    def test_one_verifier_dismissing_a_major_finding_needs_a_second_opinion(self) -> None:
        ledger = judged(self.base(("major", "f1.py", "intent")), "F1", VerdictEvent("V1", "dismissed", at(70)))
        ledger = self.verifier(ledger, "v1", "V1")

        (issue,) = check(ledger, snap=hunt_snap(1)).issues

        self.assertEqual((issue.code, issue.missing), ("second-opinion", 1))
        self.assertIn("F1", issue.message)

    def test_two_distinct_verifiers_clear_the_second_opinion(self) -> None:
        events = (VerdictEvent("V1", "dismissed", at(70)), VerdictEvent("V2", "dismissed", at(90)))
        ledger = judged(self.base(("blocker", "f1.py", "intent")), "F1", *events)
        ledger = self.verifier(self.verifier(ledger, "v1", "V1"), "v2", "V2", start=85, stop=95)

        self.assertEqual(codes(check(ledger, snap=hunt_snap(1))), [])

    def test_the_same_verifier_twice_is_one_opinion(self) -> None:
        events = (VerdictEvent("V1", "dismissed", at(70)), VerdictEvent("V2", "dismissed", at(72)))
        ledger = judged(self.base(("major", "f1.py", "intent")), "F1", *events)
        ledger = self.verifier(ledger, "v1", "V1", "V2")

        self.assertEqual(codes(check(ledger, snap=hunt_snap(1))), ["second-opinion"])

    def test_a_dismissal_that_cites_a_run_needs_no_second_opinion(self) -> None:
        ledger = record_run(self.base(("major", "f1.py", "intent")), kind="repro", exit_code=0)
        ledger = judged(ledger, "F1", VerdictEvent("V1", "dismissed", at(70)), run="R1")
        ledger = self.verifier(ledger, "v1", "V1")

        self.assertEqual(codes(check(ledger, snap=hunt_snap(1))), [])

    def test_a_dismissed_minor_finding_needs_one_verifier_only(self) -> None:
        ledger = judged(self.base(("minor", "f1.py", "intent")), "F1", VerdictEvent("V1", "dismissed", at(70)))
        ledger = self.verifier(ledger, "v1", "V1")

        self.assertEqual(codes(check(ledger, snap=hunt_snap(1))), [])

    def test_a_dismissed_major_that_no_verifier_vouches_is_unverified_not_second_opinion(self) -> None:
        ledger = judged(self.base(("major", "f1.py", "intent")), "F1", VerdictEvent("V1", "dismissed", at(70)))

        self.assertEqual(codes(check(ledger, snap=hunt_snap(1))), ["unverified"])

    def test_legacy_findings_without_events_are_exempt_from_the_second_opinion(self) -> None:
        ledger = judged(self.base(("major", "f1.py", "intent")), "F1", VerdictEvent("V1", "dismissed", at(70)))
        ledger = replace(ledger, findings=(replace(ledger.findings[0], verdict_log=()),))

        self.assertEqual(codes(check(ledger, snap=hunt_snap(1))), [])

    def test_a_legacy_round_keeps_the_per_finding_verdict_text(self) -> None:
        ledger = with_findings(hunt("W1-1", enforced=False), ("major", "f1.py", "intent"), ("minor", "f1.py", "failure"))

        issues = [issue for issue in check(ledger).issues if issue.code == "no-verdict"]

        self.assertEqual(len(issues), 2)
        self.assertIn("craftsman review verdict F1", issues[0].message)

    def open_verifier(self, ledger: Ledger, start: float = 70) -> Ledger:
        return with_agents(ledger, *run_events("v-open", start, None, agent=VERIFIER))

    def test_verifiers_already_at_work_are_waited_for(self) -> None:
        ledger = self.open_verifier(self.base(("major", "f1.py", "intent")))

        (issue,) = check(ledger, snap=hunt_snap(1)).issues

        self.assertEqual((issue.code, issue.missing), ("no-verdict", 1))
        self.assertIn("Verifiers running: 1 verifier is still at work - do not launch more", issue.message)
        self.assertNotIn("Launch verifiers", issue.message)
        self.assertNotIn("craftsman review brief --verifier", issue.message)

    def test_fewer_verifiers_than_clusters_still_launch(self) -> None:
        ledger = self.open_verifier(self.base(("major", "f1.py", "intent"), ("major", "f2.py", "intent")))

        (issue,) = check(ledger, snap=hunt_snap(1)).issues

        self.assertIn("Launch verifiers, one per cluster: [F1] [F2].", issue.message)
        self.assertNotIn("Verifiers running", issue.message)

    def test_a_verifier_that_never_reported_is_not_waited_for_once_stale(self) -> None:
        ledger = self.open_verifier(self.base(("major", "f1.py", "intent")))
        late = 70 + RUN_STALE_AFTER.total_seconds()

        self.assertIn("Verifiers running", check(ledger, late - 1, snap=hunt_snap(1)).issues[0].message)
        self.assertIn("Launch verifiers", check(ledger, late, snap=hunt_snap(1)).issues[0].message)

    def test_a_verifier_from_before_the_round_is_not_waited_for(self) -> None:
        ledger = self.base(("major", "f1.py", "intent"))
        ledger = replace(ledger, rounds=(replace(ledger.rounds[0], prepared_at=at(5)),))
        ledger = with_agents(ledger, *run_events("old", 1, None, agent=VERIFIER))

        self.assertIn("Launch verifiers", check(ledger, 100, snap=hunt_snap(1)).issues[0].message)

    def test_unverified_and_second_opinion_wait_for_running_verifiers_too(self) -> None:
        ledger = self.base(("major", "f1.py", "intent"), ("major", "f2.py", "intent"))
        ledger = judged(ledger, "F1", VerdictEvent("V1", "question", at(70)))
        ledger = judged(ledger, "F2", VerdictEvent("V2", "dismissed", at(71)))
        ledger = self.verifier(ledger, "v1", "V2")
        ledger = self.open_verifier(ledger, 80)

        report = check(ledger, snap=hunt_snap(1))

        self.assertEqual(codes(report), ["unverified", "second-opinion"])
        for issue in report.issues:
            with self.subTest(code=issue.code):
                self.assertIn("Verifiers running: 1 verifier is still at work", issue.message)


class MessageInvariantTests(unittest.TestCase):
    def scenarios(self) -> list[GateReport]:
        quiet = with_agents(hunt("W1-1", "W1-2", "W1-3"), *run_events("v", 5, None, agent=VERIFIER))
        dead = with_agents(claim(hunt("W1-1", "W1-2"), "W1-1", 20), *run_events("h1", 10, 60, records=("B1",)))
        findings = with_findings(
            hunted(hunt("W1-1", "W1-2", count=2), "W1-1", "h1", 10, 60),
            ("major", "f1.py", "intent"),
            ("major", "f2.py", "failure"),
            ("blocker", "f2.py", "ripple"),
        )
        findings = judged(findings, "F2", VerdictEvent("V1", "dismissed", at(70)))
        findings = judged(findings, "F3", VerdictEvent("V2", "probable", at(71)))
        findings = with_agents(findings, *run_events("v1", 65, 75, agent=VERIFIER, records=("V1",)))
        return [check(ledger, review_only=False) for ledger in (hunt("W1-1", "W1-2"), quiet, dead, findings)]

    def test_no_enforced_message_tells_the_orchestrator_to_record_work_itself(self) -> None:
        issues = [issue for report in self.scenarios() for issue in report.issues if issue.code in HUNT_CODES]

        self.assertGreaterEqual({issue.code for issue in issues}, {"no-isolation", "hunt-pending", "no-verdict", "unverified", "second-opinion"})
        for issue in issues:
            with self.subTest(code=issue.code):
                self.assertNotIn("review verdict", issue.message)
                self.assertNotIn("review cover", issue.message)
                self.assertNotIn("--hunks", issue.message)

    def test_every_launch_instruction_hands_the_hunter_its_brief(self) -> None:
        for report in self.scenarios():
            for issue in report.issues:
                if issue.code in ("hunt-pending", "wave-pending"):
                    self.assertIn("craftsman review brief <ID>", issue.message)


HUNT_CODES = ("no-isolation", "hunt-pending", "wave-pending", "no-verdict", "unverified", "second-opinion")


class BudgetTests(unittest.TestCase):
    """A 19-slot plan with every kind of open item must still render within what the stop hooks show."""

    def nineteen(self) -> tuple[Ledger, DiffSnapshot]:
        snap = hunt_snap(19)
        ledger = hunt(*(f"W1-{n}" for n in range(1, 20)), count=19)
        for n in (2, 3, 4):  # three hunters at work
            ledger = claim(ledger, f"W1-{n}", 60 + n)
            ledger = with_agents(ledger, *run_events(f"run{n}", 50 + n, None))
        ledger = claim(ledger, "W1-7", 30)  # one that died
        ledger = with_agents(ledger, *run_events("gone", 20, 40, records=("B4",)))
        findings = with_findings(ledger, *(("major", f"f{n}.py", "failure") for n in range(1, 20)))
        return findings, snap

    def test_every_hunt_message_fits_its_budget(self) -> None:
        ledger, snap = self.nineteen()

        report = check(ledger, 100, review_only=False, snap=snap)

        hunt_issues = [issue for issue in report.issues if issue.code in HUNT_CODES]
        self.assertEqual({issue.code for issue in hunt_issues}, {"hunt-pending", "no-verdict"})
        for issue in hunt_issues:
            with self.subTest(code=issue.code):
                self.assertLessEqual(len(issue.message), MAX_HUNT_ISSUE_CHARS)

    def test_the_gate_continuation_shows_the_first_actions_in_full(self) -> None:
        ledger, snap = self.nineteen()
        report = check(ledger, 100, review_only=False, snap=snap)

        reason = continuation_reason("[CRAFTSMAN GATE 1/6]", report, None)

        self.assertLessEqual(len(reason), MAX_CONTINUATION_REASON_CHARS)
        first, second = report.issues[0], report.issues[1]
        self.assertIn(first.message, reason)
        self.assertIn(second.message, reason)
        self.assertIn("19 hunter slots pending", reason)

    def test_the_review_guard_block_stays_short(self) -> None:
        ledger, snap = self.nineteen()
        report = check(ledger, 100, snap=snap)

        actions = render_next_actions(report.issues, max_items=REVIEW_GUARD_ACTIONS)
        block = f"{format_blocker_summary(report)}.\n" + "\n".join(actions)

        self.assertLess(len(block), 1500)  # 1.1k today; the two issues of this plan are shown in full
        self.assertIn(report.issues[0].message, block)

    def test_every_state_at_once_with_scattered_slots_and_many_findings_stays_in_budget(self) -> None:
        count = 40
        ledger = hunt(*(f"W1-{n}" for n in range(1, count + 1)), count=count)
        for n in range(1, count + 1):
            if n % 3 == 0:  # running: claim backed by an open run
                ledger = with_agents(claim(ledger, f"W1-{n}", 60 + n), *run_events(f"r{n}", 50 + n, None))
            elif n % 3 == 1:  # dead: its hunter stopped
                token = f"B{len(ledger.claims) + 1}"
                ledger = with_agents(
                    claim(ledger, f"W1-{n}", 30 + n), *run_events(f"d{n}", 20 + n, 100 + n, records=(token,))
                )
        ledger = with_findings(ledger, *(("major", f"f{n % count + 1}.py", "failure") for n in range(120)))

        report = check(ledger, 1000, review_only=False, snap=hunt_snap(count))

        states = {item.state for item in report.slots}
        self.assertEqual(states, {"running", "dead", "unlaunched"})
        hunt_issues = [issue for issue in report.issues if issue.code in HUNT_CODES]
        self.assertEqual([issue.code for issue in hunt_issues], ["hunt-pending", "no-verdict"])
        for issue in hunt_issues:
            with self.subTest(code=issue.code):
                self.assertLessEqual(len(issue.message), MAX_HUNT_ISSUE_CHARS)

    def test_slot_ids_are_listed_as_ranges_and_cut_after_twelve(self) -> None:
        ledger = with_agents(hunt(*(f"W1-{n}" for n in range(1, 20)), count=19), *run_events("v", 5, None, agent=VERIFIER))

        (issue,) = check(ledger, snap=hunt_snap(19)).issues

        self.assertIn("Launch now, all in one step: W1-1..W1-12, +7 more.", issue.message)

    def test_non_consecutive_slots_are_listed_one_by_one(self) -> None:
        ledger = hunted(hunt("W1-1", "W1-2", "W1-3", "W1-4", count=4), "W1-2", "h1", 10, 60)
        ledger = with_agents(ledger, *run_events("v", 5, None, agent=VERIFIER))

        (issue,) = check(ledger).issues

        self.assertIn("Launch now, all in one step: W1-1, W1-3, W1-4.", issue.message)


class HuntReportTests(unittest.TestCase):
    def test_the_report_prints_a_hunt_line_for_planned_rounds(self) -> None:
        ledger = hunted(hunt("W1-1", "W1-2", "W1-3", "W1-4", "W1-5", count=5), "W1-1", "h1", 10, 60)
        ledger = hunted(ledger, "W1-2", "h2", 70, 120)
        ledger = hunted(ledger, "W1-3", "h3", 130, 180)
        ledger = with_agents(claim(ledger, "W1-4", 301), *run_events("h4", 300, None))

        text = render_report(check(ledger, 400, snap=hunt_snap(5)))

        self.assertIn("Hunt: 5 slots - 3 done, 1 running (W1-4), 1 pending (W1-5)", text)

    def test_stopped_hunters_are_named_in_the_hunt_line(self) -> None:
        ledger = with_agents(claim(hunt("W1-1", "W1-2"), "W1-1", 20), *run_events("h1", 10, 60, records=("B1",)))

        self.assertIn("Hunt: 2 slots - 1 stopped (W1-1), 1 pending (W1-2)", render_report(check(ledger)))

    def test_a_round_without_a_plan_prints_no_hunt_line(self) -> None:
        ledger, _ = prepared(hunt_snap())

        self.assertNotIn("Hunt:", render_report(check(ledger)))


class HuntBlockerSummaryTests(unittest.TestCase):
    def test_every_hunt_code_has_a_clause(self) -> None:
        report = GateReport(
            (
                GateIssue("hunt-pending", "m", missing=3),
                GateIssue("no-verdict", "m", missing=2),
                GateIssue("unverified", "m", missing=1),
                GateIssue("second-opinion", "m", missing=2),
                GateIssue("wave-pending", "m", missing=1),
            ),
            (),
            "Summary",
        )

        self.assertEqual(
            format_blocker_summary(report),
            "review incomplete: 3 hunter slots pending; 2 findings without a verdict; "
            "1 verdict not made by a verifier; 2 dismissals needing a second verifier; 1 wave slot pending",
        )

    def test_the_summary_of_a_real_report_counts_the_pending_slots(self) -> None:
        ledger = with_agents(hunt("W1-1", "W1-2"), *run_events("v", 5, None, agent=VERIFIER))

        self.assertEqual(format_blocker_summary(check(ledger)), "review incomplete: 2 hunter slots pending")

    def test_legacy_verdict_issues_still_count_one_each(self) -> None:
        report = GateReport((GateIssue("no-verdict", "a"), GateIssue("no-verdict", "b")), (), "Summary")

        self.assertEqual(format_blocker_summary(report), "review incomplete: 2 findings without a verdict")


if __name__ == "__main__":
    unittest.main()
