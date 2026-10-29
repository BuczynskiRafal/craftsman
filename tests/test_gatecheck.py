"""Tests for the gate's evaluation of the evidence ledger and its report."""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from craftsman.diffscan import DiffSnapshot, snapshot
from craftsman.gatecheck import (
    MAX_ROUNDS,
    GateIssue,
    GatePolicy,
    GateReport,
    evaluate,
    passing_runs,
    render_report,
)
from craftsman.ledger import (
    ack_signal,
    add_agent_event,
    add_coverage,
    resolve,
    set_verdict,
    waive_run,
)
from craftsman.records import AgentEvent, Coverage, Ledger, QualitySignal, SignalKind

from tests.support import (
    BASE_SHA,
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
QUICK = GatePolicy(tier="quick")
STANDARD = GatePolicy(tier="standard")
ISOLATED = GatePolicy(tier="standard", isolation_evidence="hooks")
LONG_FUNCTION = QualitySignal("S1", SignalKind.LONG_FUNCTION, "a.py", 1, "`f` spans 50 lines")
DIFF_SIZE = QualitySignal("S2", SignalKind.DIFF_SIZE, "a.py", 1, "+900 production lines added")


def codes(report: GateReport) -> list[str]:
    return [issue.code for issue in report.issues]


def stop_event(agent: str, at: str, event: str = "stop") -> AgentEvent:
    return AgentEvent(platform="copilot", event=event, agent=agent, agent_id="x", at=at)  # type: ignore[arg-type]


class RoundTests(unittest.TestCase):
    def test_fully_reviewed_change_passes(self) -> None:
        report = evaluate(fully_reviewed(two_hunks()), two_hunks(), QUICK)

        self.assertTrue(report.passed, render_report(report))
        self.assertEqual(report.warnings, ())

    def test_empty_snapshot_passes_without_a_round(self) -> None:
        report = evaluate(Ledger(), make_snap(fingerprint="empty"), QUICK)

        self.assertTrue(report.passed)
        self.assertIn("Nothing to review", report.summary)

    def test_empty_snapshot_still_blocks_on_confirmed_findings(self) -> None:
        ledger, _ = prepared(two_hunks())
        ledger = record_run(record_finding(ledger), kind="repro", exit_code=1)
        ledger, _ = set_verdict(ledger, "F1", "proven", EVIDENCE, "R1")

        self.assertEqual(codes(evaluate(ledger, make_snap(fingerprint="empty"), QUICK)), ["unresolved"])

    def test_no_round(self) -> None:
        report = evaluate(Ledger(), two_hunks(), QUICK)

        self.assertEqual(codes(report), ["no-round", "no-passing-run"])
        self.assertIn("craftsman review prepare", report.issues[0].message)

    def test_stale_round_still_reports_the_rest(self) -> None:
        ledger, _ = prepared(two_hunks())

        report = evaluate(ledger, two_hunks("fp2"), QUICK)

        self.assertEqual(codes(report), ["stale-round", "uncovered", "no-passing-run"])
        self.assertIn("since round 1 was prepared", report.issues[0].message)

    def test_changed_base_is_reported_as_stale(self) -> None:
        moved = make_snap(*two_hunks().hunks, base="c" * 40)

        (issue,) = evaluate(fully_reviewed(two_hunks()), moved, QUICK).issues

        self.assertEqual(issue.code, "stale-round")
        self.assertIn("craftsman review reset", issue.message)

    def test_round_prepared_below_the_required_tier_is_rejected(self) -> None:
        report = evaluate(fully_reviewed(two_hunks()), two_hunks(), STANDARD)

        self.assertEqual(codes(report), ["tier-too-low"])
        self.assertIn("craftsman review prepare --tier standard", report.issues[0].message)

    def test_round_cap_stops_iterating(self) -> None:
        ledger = Ledger()
        for number in range(1, MAX_ROUNDS + 2):
            ledger, _ = prepared(two_hunks(f"fp{number}"), ledger=ledger)

        report = evaluate(ledger, two_hunks(f"fp{MAX_ROUNDS + 1}"), QUICK)

        self.assertEqual(codes(report)[-1], "round-cap")
        self.assertIn("stop iterating and report", report.issues[-1].message)

    def test_round_cap_counts_distinct_diffs(self) -> None:
        ledger = Ledger()
        for tier in ("quick", "standard", "deep"):
            for number in range(1, MAX_ROUNDS):
                ledger, _ = prepared(two_hunks(f"fp{number}"), tier=tier, ledger=ledger)

        self.assertNotIn("round-cap", codes(evaluate(ledger, two_hunks("fp1"), QUICK)))


class CoverageTests(unittest.TestCase):
    def test_uncovered_issue_names_the_cover_command(self) -> None:
        ledger, _ = prepared(two_hunks(), tier="standard")
        ledger, _ = add_coverage(ledger, "intent", ["H1"], coverage_note(["H1"]))

        report = evaluate(record_run(ledger), two_hunks(), QUICK)

        self.assertEqual(codes(report), ["uncovered", "uncovered"])
        self.assertIn("Lens `intent` has 1 uncovered hunk(s) in round 1: H2", report.issues[0].message)
        self.assertIn(
            'craftsman review cover --lens craft --hunks H1,H2 --note "..."', report.issues[1].message
        )

    def test_hunks_reopened_by_a_changed_symbol_need_fresh_coverage(self) -> None:
        caller = make_hunk("H1", "a.py", (" total = compute_total(x)", "+y = 1"))
        ledger = fully_reviewed(make_snap(caller))
        changed = make_hunk("H2", "b.py", ("+def compute_total(items, rate):",))
        snap = make_snap(caller, changed, fingerprint="fp2")
        ledger, _ = prepared(snap, tier="standard", ledger=ledger, symbols={"H2": ["compute_total"]})

        report = evaluate(record_run(ledger, fingerprint="fp2"), snap, QUICK)

        self.assertEqual(codes(report), ["uncovered"])
        self.assertIn("2 uncovered hunk(s) in round 2: H1,H2", report.issues[0].message)
        self.assertIn("Coverage: 0/4 lens items", report.summary)

    def test_design_lens_lists_uncovered_files(self) -> None:
        ledger, _ = prepared(two_hunks(), lenses=("intent", "design"), tier="standard")
        ledger = record_run(cover_open_hunks(ledger, "intent"))

        (issue,) = evaluate(ledger, two_hunks(), QUICK).issues

        self.assertIn("Lens `design` has 2 uncovered file(s) in round 1: a.py,b.py", issue.message)
        self.assertIn("--files a.py,b.py", issue.message)


class SweepTests(unittest.TestCase):
    def test_sweep_is_reported_only_after_every_other_lens(self) -> None:
        ledger, _ = prepared(two_hunks(), lenses=("intent", "sweep"), tier="standard")

        report = evaluate(record_run(ledger), two_hunks(), QUICK)

        self.assertEqual(codes(report), ["uncovered"])
        self.assertIn("Lens `intent`", report.issues[0].message)
        self.assertIn("Sweep pending", report.summary)

    def test_uncovered_sweep_is_listed_once_the_lenses_are_done(self) -> None:
        ledger, _ = prepared(two_hunks(), lenses=("intent", "sweep"), tier="standard")
        ledger = record_run(cover_open_hunks(ledger, "intent"))

        report = evaluate(ledger, two_hunks(), QUICK)

        self.assertEqual(codes(report), ["uncovered"])
        self.assertIn("Lens `sweep` has 2 uncovered hunk(s)", report.issues[0].message)
        self.assertNotIn("Sweep pending", report.summary)

    def test_sweep_after_the_lenses_passes(self) -> None:
        self.assertTrue(evaluate(swept(two_hunks()), two_hunks(), QUICK).passed)

    def test_a_later_duplicate_lens_note_does_not_invalidate_the_sweep(self) -> None:
        ledger, _ = add_coverage(swept(two_hunks()), "intent", ["H1"], coverage_note(["H1"]))

        self.assertTrue(evaluate(ledger, two_hunks(), QUICK).passed)

    def test_a_sweep_recorded_before_the_lenses_completed_does_not_count(self) -> None:
        ledger, round_ = prepared(two_hunks(), lenses=("intent", "sweep"), tier="standard")
        h1, h2 = (hunk.digest for hunk in two_hunks().hunks)
        start = round_.prepared_at
        coverage = (
            coverage_record(1, "intent", [h1], shifted(start, 1)),
            coverage_record(1, "sweep", [h1, h2], shifted(start, 2)),
            coverage_record(1, "intent", [h2], shifted(start, 3)),
        )

        (issue,) = evaluate(record_run(replace(ledger, coverage=coverage)), two_hunks(), QUICK).issues

        self.assertEqual(issue.code, "sweep-too-early")
        self.assertIn("The final sweep of H1, H2 was recorded before", issue.message)


class FindingTests(unittest.TestCase):
    def test_finding_without_a_verdict_blocks(self) -> None:
        report = evaluate(record_finding(fully_reviewed(two_hunks())), two_hunks(), QUICK)

        self.assertEqual(codes(report), ["no-verdict"])
        self.assertIn("craftsman review verdict F1", report.issues[0].message)

    def test_confirmed_finding_blocks_until_resolved(self) -> None:
        ledger = record_finding(fully_reviewed(two_hunks()))
        ledger, _ = set_verdict(ledger, "F1", "probable", PROBABLE, None)

        report = evaluate(ledger, two_hunks(), QUICK)

        self.assertEqual(codes(report), ["unresolved"])
        self.assertIn("craftsman review resolve F1 --as fixed", report.issues[0].message)

    def test_design_question_does_not_block(self) -> None:
        ledger = record_finding(fully_reviewed(two_hunks()))
        evidence = "intent: the task asks for retries; app.py:3 retries forever instead"
        ledger, _ = set_verdict(ledger, "F1", "question", evidence, None)

        report = evaluate(ledger, two_hunks(), QUICK)

        self.assertTrue(report.passed)
        self.assertIn("Design questions for the author: F1", " ".join(report.warnings))

    def test_a_fix_that_is_reverted_counts_as_unresolved_again(self) -> None:
        ledger = record_run(record_finding(fully_reviewed(two_hunks())), kind="repro", exit_code=1)
        ledger, _ = set_verdict(ledger, "F1", "proven", EVIDENCE, "R2")
        ledger, _ = resolve(
            ledger, "F1", "fixed", "Guarded the empty list in total().", fingerprint="fp2"
        )

        (issue,) = evaluate(ledger, two_hunks(), QUICK).issues

        self.assertEqual(issue.code, "unresolved")
        self.assertIn("back to the code it was recorded at", issue.message)


class SignalTests(unittest.TestCase):
    def setUp(self) -> None:
        ledger, _ = prepared(two_hunks(), signals=(LONG_FUNCTION,))
        self.ledger = record_run(cover_open_hunks(cover_open_hunks(ledger, "intent"), "craft"))

    def test_unresolved_signal_blocks(self) -> None:
        (issue,) = evaluate(self.ledger, two_hunks(), QUICK).issues

        self.assertEqual(issue.code, "unresolved-signal")
        self.assertIn("craftsman review ack S1", issue.message)

    def test_finding_about_the_signal_resolves_it(self) -> None:
        ledger = record_finding(self.ledger, signal_id="S1")
        ledger, _ = set_verdict(ledger, "F1", "probable", PROBABLE, None)

        self.assertNotIn("unresolved-signal", codes(evaluate(ledger, two_hunks(), QUICK)))

    def test_dismissed_finding_does_not_resolve_the_signal(self) -> None:
        ledger = record_finding(self.ledger, signal_id="S1")
        ledger, _ = set_verdict(ledger, "F1", "dismissed", DISMISSED, None)

        self.assertEqual(codes(evaluate(ledger, two_hunks(), QUICK)), ["unresolved-signal"])

    def test_ack_carries_over_to_the_same_signal_in_a_later_round(self) -> None:
        ledger, _ = ack_signal(self.ledger, "S1", "`f` in a.py:1 is a flat table of 50 literal cases")
        ledger, _ = prepared(two_hunks("fp2"), ledger=ledger, signals=(LONG_FUNCTION,))

        self.assertNotIn("unresolved-signal", codes(evaluate(ledger, two_hunks("fp2"), QUICK)))

    def test_equal_looking_signals_need_one_ack_each(self) -> None:
        twin = QualitySignal("S2", SignalKind.LONG_FUNCTION, "a.py", 90, "`render` spans 61 lines")
        first = replace(twin, id="S1", line=10)
        ledger, _ = prepared(two_hunks(), signals=(first, twin))
        ledger = record_run(cover_open_hunks(cover_open_hunks(ledger, "intent"), "craft"))
        ledger, _ = ack_signal(ledger, "S1", "`render` in a.py:10 is a flat table of 61 literal rows")

        (issue,) = evaluate(ledger, two_hunks(), QUICK).issues

        self.assertIn("Quality signal S2", issue.message)

    def test_diff_size_signal_always_blocks(self) -> None:
        ledger, _ = prepared(two_hunks(), signals=(DIFF_SIZE,))
        ledger = record_run(cover_open_hunks(cover_open_hunks(ledger, "intent"), "craft"))

        (issue,) = evaluate(ledger, two_hunks(), QUICK).issues

        self.assertIn("hand the review to the user", issue.message)


class RunTests(unittest.TestCase):
    def setUp(self) -> None:
        ledger, _ = prepared(two_hunks())
        self.covered = cover_open_hunks(cover_open_hunks(ledger, "intent"), "craft")

    def test_only_current_nontrivial_tests_count_as_passing(self) -> None:
        ledger = record_run(self.covered, fingerprint="old")
        ledger = record_run(ledger, ("true",))
        ledger = record_run(ledger, ("ruff", "check"), kind="lint")

        report = evaluate(ledger, two_hunks(), QUICK)

        self.assertEqual(codes(report), ["no-passing-run"])
        self.assertIn("craftsman run --kind test --", report.issues[0].message)
        self.assertEqual(
            report.warnings, ("R2 (`true`) is trivial and does not count as verification.",)
        )

    def test_passing_runs_are_test_or_e2e_runs_at_the_fingerprint(self) -> None:
        ledger = record_run(self.covered, ("python3", "repro.py"), kind="other")
        ledger = record_run(ledger, kind="test")

        self.assertEqual([run.id for run in passing_runs(ledger, "fp1")], ["R2"])

    def test_deep_tier_requires_an_e2e_run(self) -> None:
        ledger = fully_reviewed(two_hunks(), tier="deep")
        deep = GatePolicy(tier="deep")

        self.assertEqual(codes(evaluate(ledger, two_hunks(), deep)), ["no-e2e-run"])
        ledger = record_run(ledger, ("python3", "scripts/smoke.py"), kind="e2e")
        self.assertTrue(evaluate(ledger, two_hunks(), deep).passed)

    def test_failing_run_blocks_until_the_same_command_passes(self) -> None:
        ledger = record_run(
            fully_reviewed(two_hunks()), ("ruff", "check"), kind="lint", exit_code=1, fingerprint="old"
        )

        (issue,) = evaluate(ledger, two_hunks(), QUICK).issues
        self.assertIn("`ruff check` (lint, R2) last exited 1", issue.message)

        ledger = record_run(ledger, ("ruff", "check"), kind="lint")
        self.assertTrue(evaluate(ledger, two_hunks(), QUICK).passed)

    def test_failing_runs_are_tracked_per_working_directory(self) -> None:
        ledger = record_run(fully_reviewed(two_hunks()), exit_code=1, cwd="/repo/pkg_a")
        ledger = record_run(ledger, cwd="/repo/pkg_b")

        (issue,) = evaluate(ledger, two_hunks(), QUICK).issues

        self.assertEqual(issue.code, "failing-run")
        self.assertIn("R2", issue.message)

    def test_a_run_that_modified_the_tree_never_counts_as_passing(self) -> None:
        ledger = record_run(self.covered, exit_code=1)
        ledger = record_run(ledger, fingerprint_after="fp2")

        self.assertEqual(codes(evaluate(ledger, two_hunks(), QUICK)), ["no-passing-run", "failing-run"])

    def test_waived_failing_run_no_longer_blocks(self) -> None:
        ledger = record_run(fully_reviewed(two_hunks()), ("pytest", "tests/test_typo.py"), exit_code=4)

        waived, _ = waive_run(ledger, "R2", "Typo in the path; tests/test_cart.py is the real file.")

        report = evaluate(waived, two_hunks(), QUICK)
        self.assertTrue(report.passed, render_report(report))
        self.assertIn("Waived failing runs: R2", " ".join(report.warnings))

    def test_a_later_failure_of_a_waived_command_blocks_again(self) -> None:
        ledger = record_run(fully_reviewed(two_hunks()), ("ruff", "check"), kind="lint", exit_code=1)
        ledger, _ = waive_run(ledger, "R2", "Ran with a stale config before the rename.")
        ledger = record_run(ledger, ("ruff", "check"), kind="lint", exit_code=1)

        self.assertEqual(codes(evaluate(ledger, two_hunks(), QUICK)), ["failing-run"])


class IsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ledger = fully_reviewed(two_hunks(), tier="standard")
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

    def test_quick_tier_needs_no_isolation(self) -> None:
        self.assertTrue(evaluate(self.ledger, two_hunks(), replace(ISOLATED, tier="quick")).passed)

    def test_a_diff_without_hunks_needs_no_reviewer(self) -> None:
        snap = DiffSnapshot(BASE_SHA, "fp1", (), ("logo.png",), (), 0, 0)
        ledger, _ = prepared(snap, tier="standard")

        report = evaluate(record_run(ledger), snap, ISOLATED)

        self.assertTrue(report.passed, render_report(report))

    def test_standard_review_without_isolation_is_marked_self_reviewed(self) -> None:
        report = evaluate(self.ledger, two_hunks(), STANDARD)

        self.assertTrue(report.passed)
        self.assertTrue(report.self_reviewed)
        self.assertIn("SELF-REVIEWED", render_report(report))


class ReviewOnlyTests(unittest.TestCase):
    def test_review_only_passes_without_resolved_findings_or_passing_runs(self) -> None:
        ledger = record_run(swept(two_hunks()), exit_code=1)
        ledger = record_run(ledger, kind="repro", exit_code=1)
        ledger = record_finding(record_finding(ledger))
        ledger, _ = set_verdict(ledger, "F1", "proven", EVIDENCE, "R3")
        ledger, _ = set_verdict(ledger, "F2", "probable", PROBABLE, None)

        report = evaluate(ledger, two_hunks(), STANDARD, review_only=True)

        self.assertEqual(report.issues, ())
        self.assertIn("2 findings for the author: F1, F2.", report.warnings)
        self.assertIn("Failing run R2 (`pytest -q`, test) exited 1.", report.warnings)

    def test_review_only_still_requires_coverage(self) -> None:
        ledger, _ = prepared(two_hunks(), lenses=("intent", "sweep"), tier="standard")

        self.assertEqual(codes(evaluate(ledger, two_hunks(), STANDARD, review_only=True)), ["uncovered"])

    def test_review_only_still_requires_verdicts(self) -> None:
        ledger = record_finding(swept(two_hunks()))

        report = evaluate(ledger, two_hunks(), STANDARD, review_only=True)

        self.assertEqual(codes(report), ["no-verdict"])


class WarningTests(unittest.TestCase):
    def test_accepted_findings_are_reported_to_the_user(self) -> None:
        ledger = record_finding(fully_reviewed(two_hunks()))
        ledger, _ = set_verdict(ledger, "F1", "probable", PROBABLE, None)
        ledger, _ = resolve(
            ledger, "F1", "accepted", "Callers never pass an empty list.", fingerprint="fp1"
        )

        report = evaluate(ledger, two_hunks(), QUICK)

        self.assertIn("Accepted without a fix: F1; tell the user about them.", report.warnings)
        self.assertIn("1 accepted (F1)", report.summary)

    def test_oversized_quick_review_suggests_the_standard_tier(self) -> None:
        lines = ("+a", "+b", "+c", "+d", "+e")
        big = make_snap(*(make_hunk(f"H{index}", f"f{index}.py", lines) for index in range(1, 10)))

        report = evaluate(fully_reviewed(big), big, QUICK)

        self.assertEqual(
            report.warnings,
            ("The quick tier reviews 45 changed lines (> 40); consider the standard tier.",),
        )


class AnomalyTests(unittest.TestCase):
    def test_all_findings_confirmed_is_an_anomaly(self) -> None:
        ledger = fully_reviewed(two_hunks())
        for number in range(1, 6):
            ledger = record_finding(ledger)
            ledger, _ = set_verdict(ledger, f"F{number}", "probable", PROBABLE, None)
            ledger, _ = resolve(
                ledger, f"F{number}", "fixed", "Guarded the empty list.", fingerprint="fp2"
            )

        warnings = evaluate(ledger, two_hunks(), QUICK).warnings

        self.assertIn(
            "Process anomaly: 5 findings and 0 dismissed (all findings confirmed — typical of self-review).",
            warnings,
        )

    def test_dismissed_findings_are_no_anomaly(self) -> None:
        ledger = fully_reviewed(two_hunks())
        for number in range(1, 6):
            ledger = record_finding(ledger)
            ledger, _ = set_verdict(ledger, f"F{number}", "dismissed", DISMISSED, None)

        self.assertEqual(evaluate(ledger, two_hunks(), QUICK).warnings, ())

    def test_failing_test_rerun_under_another_environment_is_an_anomaly(self) -> None:
        ledger = record_run(fully_reviewed(two_hunks()), exit_code=1)
        ledger = record_run(ledger, ("env", "FOO=1", "pytest", "-q"))

        warnings = evaluate(ledger, two_hunks(), QUICK).warnings

        self.assertIn(
            "Process anomaly: test run R2 failed and was repeated as R3 with a different "
            "environment without a code change.",
            warnings,
        )

    def burst_covered(self, lens: str, count: int) -> list[str]:
        """Warnings for a round whose `lens` covered `count` targets within ten seconds."""
        snap = make_snap(*(make_hunk(f"H{index}", f"f{index}.py") for index in range(1, count + 1)))
        ledger, round_ = prepared(snap, lenses=(lens,), tier="standard")
        units = (
            [file.digest for file in round_.files]
            if lens == "design"
            else [h.digest for h in round_.hunks]
        )
        coverage = tuple(
            coverage_record(1, lens, units[start : start + 4], shifted(round_.prepared_at, start))
            for start in range(0, len(units), 4)
        )
        return list(evaluate(replace(ledger, coverage=coverage), snap, QUICK).warnings)

    def test_many_hunks_covered_within_seconds_is_an_anomaly(self) -> None:
        self.assertIn(
            "Process anomaly: 21 hunks covered in lens `intent` in under 2 minutes.",
            self.burst_covered("intent", 21),
        )

    def test_fast_design_coverage_is_no_anomaly(self) -> None:
        self.assertEqual(self.burst_covered("design", 21), [])

    def test_rerunning_the_same_failing_command_is_no_anomaly(self) -> None:
        ledger = record_run(record_run(fully_reviewed(two_hunks()), exit_code=1), exit_code=1)

        self.assertEqual(evaluate(ledger, two_hunks(), QUICK).warnings, ())

    def test_a_failure_retried_repeatedly_is_reported_once(self) -> None:
        ledger = record_run(fully_reviewed(two_hunks()), exit_code=1)
        for _ in range(3):
            ledger = record_run(ledger, ("env", "A=1", "pytest", "-q"))

        warnings = [w for w in evaluate(ledger, two_hunks(), QUICK).warnings if "repeated" in w]

        self.assertEqual(len(warnings), 1)

    def test_every_environment_retry_after_a_failure_is_an_anomaly(self) -> None:
        ledger = record_run(fully_reviewed(two_hunks()), exit_code=1)
        ledger = record_run(ledger, ("env", "A=1", "pytest", "-q"), exit_code=1)
        ledger = record_run(ledger, ("env", "B=1", "pytest", "-q"))

        warnings = " ".join(evaluate(ledger, two_hunks(), QUICK).warnings)

        self.assertIn("R2 failed and was repeated as R3", warnings)
        self.assertIn("R3 failed and was repeated as R4", warnings)

    def test_evenly_batched_coverage_of_a_large_diff_is_no_anomaly(self) -> None:
        snap = make_snap(*(make_hunk(f"H{index}", f"f{index}.py") for index in range(1, 33)))
        ledger, round_ = prepared(snap)
        coverage: list[Coverage] = []
        for lens in ("intent", "craft"):
            for batch in range(8):
                digests = [hunk.digest for hunk in round_.hunks[batch * 4 : batch * 4 + 4]]
                coverage.append(
                    coverage_record(1, lens, digests, shifted(round_.prepared_at, batch * 60))
                )
        ledger = record_run(replace(ledger, coverage=tuple(coverage)))

        report = evaluate(ledger, snap, QUICK)

        self.assertTrue(report.passed, render_report(report))
        self.assertEqual(report.warnings, ())


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
        report = evaluate(fully_reviewed(two_hunks()), two_hunks(), QUICK)

        self.assertTrue(render_report(report).startswith("**Craftsman gate: PASSED**"))

    def test_render_report_caps_next_actions(self) -> None:
        ledger, _ = prepared(two_hunks())
        for _ in range(14):
            ledger = record_finding(ledger)

        rendered = render_report(evaluate(ledger, two_hunks(), QUICK))

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
            self.assertTrue(evaluate(ledger, snap, QUICK).passed)

            repo.write("app.py", "def total(items):\n    return sum(items) or None\n")
            edited = snapshot(repo.root, repo.base_sha)

            self.assertEqual(codes(evaluate(ledger, edited, QUICK)), ["stale-round", "no-passing-run"])


if __name__ == "__main__":
    unittest.main()
