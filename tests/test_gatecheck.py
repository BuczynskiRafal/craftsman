"""Tests for the gate's evaluation of the evidence ledger and its report."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from craftsman.diffscan import DiffSnapshot, snapshot
from craftsman.gatecheck import (
    MAX_ROUNDS,
    GateIssue,
    GatePolicy,
    GateReport,
    evaluate,
    format_blocker_summary,
    passing_runs,
    render_report,
)
from craftsman.ledger import (
    add_agent_event,
    add_coverage,
    resolve,
    set_verdict,
    waive_run,
)
from craftsman.records import AgentEvent, Ledger, QualitySignal, SignalKind

from tests.support import (
    BASE_SHA,
    EvidenceRepo,
    cover_open_hunks,
    coverage_note,
    fully_reviewed,
    make_hunk,
    make_snap,
    prepared,
    record_finding,
    record_run,
    shifted,
    two_hunks,
)

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

    def test_a_round_of_carried_hunks_needs_no_reviewer(self) -> None:
        ledger, _ = prepared(two_hunks("fp2"), ledger=self.ledger)

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


if __name__ == "__main__":
    unittest.main()
