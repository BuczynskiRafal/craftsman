#!/usr/bin/env python3
"""Tests for the evidence ledger: records, operations, persistence, locking and gate evaluation."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest import mock

from craftsman import ledger as ledger_module
from craftsman.diffscan import DiffSnapshot, Hunk, snapshot
from craftsman.ledger import (
    LEDGER_FILENAME,
    AgentEvent,
    Coverage,
    FieldError,
    GatePolicy,
    GateReport,
    Ledger,
    LedgerError,
    Round,
    Run,
    RunKind,
    RunWaiver,
    Severity,
    add_agent_event,
    add_coverage,
    add_finding,
    add_run,
    evaluate,
    is_trivial_run,
    ledger_path_for,
    load,
    locked,
    merge_findings,
    mutate,
    open_round,
    render_report,
    reopened_mentions,
    resolve,
    save,
    set_verdict,
    uncovered_by_lens,
    waive_run,
)
from craftsman.lenses import Tier

BASE = "b" * 40
T0 = "2026-01-01T00:00:00.000Z"
T1 = "2026-01-01T00:05:00.000Z"
EVIDENCE = "Reproduced via tests/test_app.py::test_empty (fails before fix)."
PROBABLE = "no-repro: needs a live database; app.py:3 divides by len(items) == 0"
DISMISSED = "guarded by app.py:2, which returns 0 for an empty list"


def note_for(hunk_ids: Sequence[str]) -> str:
    return "; ".join(
        f"{hid} f_{hid[1:]}.py:{hid[1:]}: x={hid[1:]} -> returns {int(hid[1:]) + 1} on boundary input"
        for hid in hunk_ids
    )


def init_repo(root: Path) -> None:
    for args in (
        ("init", "-q"),
        ("config", "user.email", "dev@example.com"),
        ("config", "user.name", "Dev"),
        ("config", "commit.gpgsign", "false"),
        ("config", "core.hooksPath", str(root / ".git" / "no-hooks")),
    ):
        git(root, *args)


def git(root: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=True, timeout=30
    )
    return proc.stdout


def make_hunk(hunk_id: str, path: str = "app.py", lines: Sequence[str] = ("+x = 1",)) -> Hunk:
    return Hunk(
        id=hunk_id,
        path=path,
        old_path=None,
        status="modified",
        old_start=1,
        old_len=0,
        new_start=1,
        new_len=len(lines),
        header="",
        lines=tuple(lines),
        digest=f"d-{path}-{'|'.join(lines)}",
    )


def make_snap(*hunks: Hunk, fingerprint: str = "fp1", base: str = BASE) -> DiffSnapshot:
    return DiffSnapshot(
        base_sha=base,
        fingerprint=fingerprint,
        hunks=hunks,
        files=tuple(sorted({hunk.path for hunk in hunks})),
        skipped=(),
        added=sum(line.startswith("+") for hunk in hunks for line in hunk.lines),
        deleted=sum(line.startswith("-") for hunk in hunks for line in hunk.lines),
    )


def make_run(
    argv: Sequence[str] = ("pytest", "-q"),
    *,
    kind: RunKind = "test",
    exit_code: int = 0,
    fingerprint: str = "fp1",
) -> Run:
    return Run(
        id="",
        kind=kind,
        argv=tuple(argv),
        cwd=".",
        exit_code=exit_code,
        duration_s=1.5,
        fingerprint=fingerprint,
        started_at=T0,
        output_tail="ok",
    )


def prepared(
    snap: DiffSnapshot,
    lenses: Sequence[str] = ("intent", "craft"),
    tier: Tier = "quick",
    ledger: Ledger | None = None,
    symbols: Mapping[str, Sequence[str]] | None = None,
) -> tuple[Ledger, Round]:
    return open_round(ledger or Ledger(), snap, tier, lenses, "packet.md", symbols or {})


def fully_reviewed(
    snap: DiffSnapshot, lenses: Sequence[str] = ("intent", "craft"), tier: Tier = "quick"
) -> Ledger:
    ledger, round_ = prepared(snap, lenses, tier)
    for lens in lenses:
        ids = [hunk.id for hunk in round_.hunks if not hunk.carried]
        for start in range(0, len(ids), 4):
            batch = ids[start : start + 4]
            ledger, _ = add_coverage(ledger, lens, batch, note_for(batch))
    return add_run(ledger, make_run(fingerprint=snap.fingerprint))


def with_finding(ledger: Ledger, severity: Severity = "major") -> Ledger:
    ledger, _ = add_finding(
        ledger,
        lens="intent",
        path="app.py",
        line=3,
        severity=severity,
        title="Empty list crashes total()",
        scenario="Calling total([]) divides by len(items) == 0 and raises.",
        suggestion="Return 0 for an empty list.",
    )
    return ledger


def codes(report: GateReport) -> list[str]:
    return [issue.code for issue in report.issues]


QUICK = GatePolicy(tier="quick", require_isolation=False)


class OpenRoundTests(unittest.TestCase):
    def test_rounds_are_numbered_and_preparing_twice_is_idempotent(self) -> None:
        snap = make_snap(make_hunk("H1"))
        ledger, first = prepared(snap)

        again, same = prepared(snap, ledger=ledger)
        deeper, second = prepared(snap, tier="deep", ledger=ledger)

        self.assertEqual(first.number, 1)
        self.assertIs(again, ledger)
        self.assertEqual(same, first)
        self.assertEqual(second.number, 2)
        self.assertEqual(len(deeper.rounds), 2)

    def test_a_changed_base_requires_a_reset(self) -> None:
        ledger, _ = prepared(make_snap(make_hunk("H1")))
        with self.assertRaisesRegex(LedgerError, "craftsman review reset"):
            prepared(make_snap(make_hunk("H1"), base="c" * 40, fingerprint="fp2"), ledger=ledger)

    def test_hunks_fully_covered_earlier_are_carried(self) -> None:
        h1, h2 = make_hunk("H1", "a.py"), make_hunk("H2", "b.py")
        ledger, _ = prepared(make_snap(h1, h2))
        ledger, _ = add_coverage(ledger, "intent", ["H1", "H2"], note_for(["H1", "H2"]))
        ledger, _ = add_coverage(ledger, "craft", ["H1"], note_for(["H1"]))

        h3 = make_hunk("H3", "c.py")
        _, round_ = prepared(make_snap(h1, h2, h3, fingerprint="fp2"), ledger=ledger)

        self.assertEqual([hunk.carried for hunk in round_.hunks], [True, False, False])

    def test_carried_hunk_is_reopened_when_it_mentions_a_changed_symbol(self) -> None:
        caller = make_hunk("H1", "a.py", (" total = compute_total(x)", "+y = 1"))
        unrelated = make_hunk("H2", "b.py", ("+z = 2",))
        ledger, _ = prepared(make_snap(caller, unrelated))
        for lens in ("intent", "craft"):
            ledger, _ = add_coverage(ledger, lens, ["H1", "H2"], note_for(["H1", "H2"]))

        changed = make_hunk(
            "H3", "c.py", ("-def compute_total(items):", "+def compute_total(items, rate):")
        )
        snap = make_snap(caller, unrelated, changed, fingerprint="fp2")
        _, round_ = prepared(snap, ledger=ledger, symbols={"H3": ["compute_total"]})

        self.assertEqual([hunk.carried for hunk in round_.hunks], [False, True, False])

    def test_reopening_cascades_through_symbols_of_reopened_hunks(self) -> None:
        first = make_hunk("H1", "a.py", ("+def helper_one():",))
        second = make_hunk("H2", "b.py", ("+value = helper_one()",))
        ledger, _ = prepared(make_snap(first, second))
        for lens in ("intent", "craft"):
            ledger, _ = add_coverage(ledger, lens, ["H1", "H2"], note_for(["H1", "H2"]))

        trigger = make_hunk("H3", "c.py", ("+def helper_zero():",))
        reworked = make_hunk("H1", "a.py", ("+def helper_one():", " helper_zero()"))
        snap = make_snap(reworked, second, trigger, fingerprint="fp2")
        symbols = {"H1": ["helper_one"], "H3": ["helper_zero"]}
        _, round_ = prepared(snap, ledger=ledger, symbols=symbols)

        self.assertEqual([hunk.carried for hunk in round_.hunks], [False, False, False])

    def test_symbols_for_unknown_hunks_are_rejected(self) -> None:
        with self.assertRaisesRegex(LedgerError, "H9"):
            prepared(make_snap(make_hunk("H1")), symbols={"H9": ["x"]})

    def test_unknown_lens_is_rejected(self) -> None:
        with self.assertRaisesRegex(LedgerError, "lenses must be one of"):
            prepared(make_snap(make_hunk("H1")), lenses=("intent", "security"))


class CoverageTests(unittest.TestCase):
    def setUp(self) -> None:
        hunks = [make_hunk(f"H{index}", f"f{index}.py") for index in range(1, 11)]
        self.ledger, self.round = prepared(make_snap(*hunks), lenses=("intent", "craft"))

    def test_records_digests_against_the_latest_round(self) -> None:
        note = note_for(["H2", "H1"])
        ledger, record = add_coverage(self.ledger, "intent", ["H2", "H1"], f"  {note}  ")

        self.assertEqual(record.round, 1)
        self.assertEqual(record.digests, (self.round.hunks[1].digest, self.round.hunks[0].digest))
        self.assertEqual(record.note, note)
        self.assertEqual(ledger.coverage, (record,))

    def test_rejections_explain_what_is_valid(self) -> None:
        cases = [
            ("security", ["H1"], note_for(["H1"]), "unknown lens 'security'; valid lenses:"),
            (
                "trust",
                ["H1"],
                note_for(["H1"]),
                "not selected in round 1; selected lenses: intent, craft",
            ),
            ("intent", [], note_for([]), "cover 1..4 hunks per call"),
            (
                "intent",
                ["H1", "H2", "H3", "H4", "H5"],
                note_for(["H1", "H2", "H3", "H4", "H5"]),
                "got 5",
            ),
            ("intent", ["H1", "H1"], note_for(["H1"]), "must not repeat"),
            (
                "intent",
                ["H42"],
                note_for(["H42"]),
                "unknown hunk ids in round 1: H42; hunks to cover: H1, H2",
            ),
            ("intent", ["H1", "H2"], note_for(["H1", "H12"]), 'missing: H2. Example: "H3 parse_cfg'),
            ("intent", ["H1"], "H1 looked at it", "note must have at least 30 characters"),
        ]
        for lens, ids, note, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(LedgerError, message):
                add_coverage(self.ledger, lens, ids, note)

    def test_requires_a_round(self) -> None:
        with self.assertRaisesRegex(LedgerError, "craftsman review prepare"):
            add_coverage(Ledger(), "intent", ["H1"], note_for(["H1"]))

    def test_carried_hunks_cannot_be_covered_again(self) -> None:
        ledger = self.ledger
        for lens in ("intent", "craft"):
            ledger, _ = add_coverage(ledger, lens, ["H1"], note_for(["H1"]))
        snap = make_snap(
            *(make_hunk(f"H{index}", f"f{index}.py") for index in range(1, 11)), fingerprint="fp2"
        )
        ledger, _ = prepared(snap, ledger=ledger)

        with self.assertRaisesRegex(LedgerError, "H1 already covered in an earlier round"):
            add_coverage(ledger, "intent", ["H1"], note_for(["H1"]))


class FindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ledger, _ = prepared(make_snap(make_hunk("H1")))

    def test_findings_get_sequential_ids_and_trimmed_text(self) -> None:
        ledger = with_finding(with_finding(self.ledger))

        self.assertEqual([finding.id for finding in ledger.findings], ["F1", "F2"])
        self.assertEqual(ledger.findings[0].round, 1)
        self.assertIsNone(ledger.findings[0].verdict)

    def test_invalid_findings_are_rejected(self) -> None:
        valid: dict[str, Any] = {
            "lens": "intent",
            "path": "app.py",
            "line": 3,
            "severity": "major",
            "title": "Empty list crashes total()",
            "scenario": "Calling total([]) divides by zero and raises.",
            "suggestion": "",
        }
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
                add_finding(self.ledger, **{**valid, **change})

    def test_requires_a_round(self) -> None:
        with self.assertRaisesRegex(LedgerError, "craftsman review prepare"):
            with_finding(Ledger())


class VerdictAndResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        ledger, _ = prepared(make_snap(make_hunk("H1")))
        self.ledger = add_run(
            with_finding(ledger), make_run(("pytest", "-k", "empty"), kind="repro", exit_code=1)
        )

    def test_proven_verdict_requires_an_existing_run(self) -> None:
        with self.assertRaisesRegex(LedgerError, "required for a 'proven' verdict"):
            set_verdict(self.ledger, "F1", "proven", EVIDENCE, None)
        with self.assertRaisesRegex(FieldError, "required for a 'proven' verdict") as caught:
            set_verdict(
                self.ledger, "F1", "proven", "app.py:3 divides by len(items) which is 0 for []", None
            )
        self.assertEqual(caught.exception.field, "verdict_run")
        with self.assertRaisesRegex(LedgerError, "unknown run 'R7'; recorded runs: R1"):
            set_verdict(self.ledger, "F1", "proven", EVIDENCE, "R7")

        _, finding = set_verdict(self.ledger, "F1", "proven", EVIDENCE, "R1")

        self.assertEqual((finding.verdict, finding.verdict_run), ("proven", "R1"))

    def test_probable_verdict_accepts_static_and_no_repro_prefixes(self) -> None:
        _, static = set_verdict(
            self.ledger,
            "F1",
            "probable",
            "static: app.py:3 divides by len(items) which is 0 for []",
            None,
        )
        self.assertEqual(static.verdict, "probable")

        with self.assertRaisesRegex(LedgerError, "cite file:line for 'static:'"):
            set_verdict(
                self.ledger,
                "F1",
                "probable",
                "static: divides by len(items) which is 0 for empty list",
                None,
            )

        _, no_repro = set_verdict(
            self.ledger,
            "F1",
            "probable",
            "no-repro: needs Windows NTFS; path check fails on case collision",
            None,
        )
        self.assertEqual(no_repro.verdict, "probable")

        with self.assertRaises(LedgerError) as missing_prefix:
            set_verdict(
                self.ledger,
                "F1",
                "probable",
                "app.py:3 divides by zero when items is empty",
                None,
            )
        self.assertIn("no-repro:", str(missing_prefix.exception))
        self.assertIn("static:", str(missing_prefix.exception))

    def test_question_verdict_validation_and_gate_behavior(self) -> None:
        snap = make_snap(make_hunk("H1"), fingerprint="fp1")
        blocker_ledger, _ = prepared(snap)
        blocker_ledger = with_finding(blocker_ledger, "blocker")
        with self.assertRaisesRegex(
            LedgerError, "a blocker is a defect, not a design question; prove, dismiss or fix it"
        ):
            set_verdict(
                blocker_ledger,
                "F1",
                "question",
                "intent: PR task asks for this; app.py:3 implements it",
                None,
            )

        with self.assertRaisesRegex(LedgerError, "for 'question' must start with 'intent:'"):
            set_verdict(
                self.ledger,
                "F1",
                "question",
                "app.py:3 behavior was explicitly requested by the PR task description",
                None,
            )
        with self.assertRaisesRegex(LedgerError, "citing file:line in the code"):
            set_verdict(
                self.ledger,
                "F1",
                "question",
                "intent: PR task says rootdir is config dir and changes behavior",
                None,
            )

        ledger, question = set_verdict(
            self.ledger,
            "F1",
            "question",
            "intent: PR task requests setting rootdir to inipath.parent at app.py:3",
            None,
        )
        self.assertEqual(question.verdict, "question")
        with self.assertRaisesRegex(LedgerError, "is a design question"):
            resolve(ledger, "F1", "accepted", "Requested by maintainer.", fingerprint="fp2")

        report = evaluate(ledger, snap, GatePolicy(tier="quick", require_isolation=False))
        self.assertNotIn("unresolved", [issue.code for issue in report.issues])

    def test_merge_findings_rejects_judged_or_resolved_findings_and_deduplicates_scenarios(
        self,
    ) -> None:
        snap1 = make_snap(make_hunk("H1", lines=("+x = 1",)), fingerprint="fp1")
        snap2 = make_snap(make_hunk("H1", lines=("+x = 2",)), fingerprint="fp2")
        ledger, _ = prepared(snap1)
        ledger = add_run(ledger, make_run(("pytest", "-k", "empty"), kind="repro", exit_code=1))
        ledger, f1 = add_finding(
            ledger,
            lens="intent",
            path="app.py",
            line=3,
            severity="minor",
            title="Minor issue on line 3",
            scenario="With -c sub/pytest.ini, rootdir becomes sub/ and breaks testpaths.",
            suggestion="",
        )
        ledger, f2 = add_finding(
            ledger,
            lens="contracts",
            path="app.py",
            line=3,
            severity="blocker",
            title="Blocker issue on line 3",
            scenario="With -c sub/pytest.ini, parent conftest.py is no longer discovered.",
            suggestion="",
        )
        ledger, _ = set_verdict(ledger, f1.id, "proven", EVIDENCE, "R1")
        ledger, _ = resolve(
            ledger, f1.id, "fixed", "Fixed minor issue at app.py:3.", fingerprint="fp2"
        )
        ledger, _ = set_verdict(ledger, f2.id, "proven", EVIDENCE, "R1")

        # Bypass #1 regression: merging an open blocker into a fixed minor target must fail,
        # and evaluate must still block on F2.
        with self.assertRaisesRegex(LedgerError, "only works on findings without a verdict.*F1"):
            merge_findings(ledger, f1.id, [f2.id])
        report = evaluate(ledger, snap2, GatePolicy(tier="quick", require_isolation=False))
        unresolved = [issue for issue in report.issues if issue.code == "unresolved"]
        self.assertEqual(len(unresolved), 1)
        self.assertIn("F2", unresolved[0].message)

        # Bypass #2 regression: accepted target or duplicate with a verdict must fail.
        acc_ledger, _ = prepared(snap1)
        acc_ledger, a1 = add_finding(
            acc_ledger,
            lens="intent",
            path="app.py",
            line=3,
            severity="minor",
            title="Accepted target finding",
            scenario="With -c sub/pytest.ini, rootdir becomes sub/ and breaks testpaths.",
            suggestion="",
        )
        acc_ledger, a2 = add_finding(
            acc_ledger,
            lens="ripple",
            path="app.py",
            line=4,
            severity="major",
            title="Unjudged duplicate finding",
            scenario="With -c sub/pytest.ini, parent conftest.py is no longer discovered.",
            suggestion="",
        )
        acc_ledger, _ = set_verdict(acc_ledger, a1.id, "probable", PROBABLE, None)
        acc_ledger, _ = resolve(
            acc_ledger, a1.id, "accepted", "Intentional trade-off accepted.", fingerprint="fp1"
        )
        with self.assertRaisesRegex(LedgerError, "only works on findings without a verdict.*F1"):
            merge_findings(acc_ledger, a1.id, [a2.id])

        dup_judged_ledger, _ = prepared(snap1)
        dup_judged_ledger, d1 = add_finding(
            dup_judged_ledger,
            lens="intent",
            path="app.py",
            line=3,
            severity="minor",
            title="Unjudged target finding",
            scenario="With -c sub/pytest.ini, rootdir becomes sub/ and breaks testpaths.",
            suggestion="",
        )
        dup_judged_ledger, d2 = add_finding(
            dup_judged_ledger,
            lens="ripple",
            path="app.py",
            line=4,
            severity="major",
            title="Probable duplicate finding",
            scenario="With -c sub/pytest.ini, parent conftest.py is no longer discovered.",
            suggestion="",
        )
        dup_judged_ledger, _ = set_verdict(dup_judged_ledger, d2.id, "probable", PROBABLE, None)
        with self.assertRaisesRegex(LedgerError, "only works on findings without a verdict.*F2"):
            merge_findings(dup_judged_ledger, d1.id, [d2.id])

        # Valid merge on unjudged findings + deduplication of identical scenario text.
        base_ledger, _ = prepared(snap1)
        base_ledger, u1 = add_finding(
            base_ledger,
            lens="intent",
            path="app.py",
            line=3,
            severity="minor",
            title="Rootdir set to inipath.parent",
            scenario="With -c sub/pytest.ini, rootdir becomes sub/ and breaks testpaths.",
            suggestion="Use ancestor directory.",
        )
        base_ledger, u2 = add_finding(
            base_ledger,
            lens="ripple",
            path="app.py",
            line=4,
            severity="major",
            title="Conftest above inipath is ignored",
            scenario="With -c sub/pytest.ini, parent conftest.py is no longer discovered.",
            suggestion="",
        )
        base_ledger, u3 = add_finding(
            base_ledger,
            lens="tests",
            path="app.py",
            line=5,
            severity="minor",
            title="Same consequence reported by tests lens",
            scenario="With -c sub/pytest.ini, parent conftest.py is no longer discovered.",
            suggestion="",
        )
        merged_ledger, target, (dup,) = merge_findings(base_ledger, u1.id, [u2.id])
        self.assertEqual(target.severity, "major")
        self.assertIn("[F2/ripple] With -c sub/pytest.ini, parent conftest.py", target.scenario)
        self.assertEqual(dup.verdict, "dismissed")
        self.assertEqual(dup.verdict_evidence, "Grouped into F1 (app.py:3): same root cause.")

        merged_ledger2, target2, _ = merge_findings(merged_ledger, u1.id, [u3.id])
        self.assertEqual(
            target2.scenario.count("With -c sub/pytest.ini, parent conftest.py is no longer discovered."),
            1,
        )
        with self.assertRaisesRegex(LedgerError, "cannot merge F1 into itself"):
            merge_findings(merged_ledger2, "F1", ["F1"])
        with self.assertRaisesRegex(LedgerError, "only works on findings without a verdict.*F2"):
            merge_findings(merged_ledger2, "F2", ["F1"])
        with self.assertRaisesRegex(LedgerError, "unknown finding 'F99'"):
            merge_findings(merged_ledger2, "F1", ["F99"])

    def test_verdict_validation_and_unknown_findings(self) -> None:
        with self.assertRaisesRegex(LedgerError, "verdict_evidence must have at least 20 characters"):
            set_verdict(self.ledger, "F1", "probable", "looks right", None)
        cases = [
            ("probable", "The chain from api.py:40 to db.py:88 crashes.", "must start with 'no-repro:'"),
            ("probable", "no-repro: needs a database", "followed by at least 20 characters"),
            ("dismissed", "I do not think this can happen at all.", "must cite the guard as file:line"),
        ]
        for verdict, evidence, message in cases:
            with self.subTest(verdict=verdict, evidence=evidence):
                with self.assertRaisesRegex(LedgerError, message) as caught:
                    set_verdict(self.ledger, "F1", verdict, evidence, None)  # type: ignore[arg-type]
                self.assertIn("e.g.", str(caught.exception))
        _, probable = set_verdict(self.ledger, "F1", "probable", f"  NO-REPRO: {PROBABLE[10:]}", None)
        _, by_run = set_verdict(
            self.ledger, "F1", "dismissed", "The repro run prints the right total.", "R1"
        )
        _, by_file = set_verdict(
            self.ledger, "F1", "dismissed", "see src\\app.py:12 for the guard", None
        )
        self.assertEqual(
            (probable.verdict, by_run.verdict, by_file.verdict), ("probable",) + ("dismissed",) * 2
        )
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

    def test_resolution_rules(self) -> None:
        with self.assertRaisesRegex(LedgerError, "has no verdict yet"):
            resolve(self.ledger, "F1", "fixed", "Guarded the empty list in total().", fingerprint="fp2")
        dismissed, _ = set_verdict(self.ledger, "F1", "dismissed", DISMISSED, None)
        with self.assertRaisesRegex(LedgerError, "was dismissed"):
            resolve(dismissed, "F1", "accepted", "Out of scope for this change.", fingerprint="fp2")

        probable, _ = set_verdict(self.ledger, "F1", "probable", PROBABLE, None)
        with self.assertRaisesRegex(LedgerError, "fix the code first, then resolve"):
            resolve(probable, "F1", "fixed", "Guarded the empty list in total().", fingerprint="fp1")
        with self.assertRaisesRegex(LedgerError, "resolution_note must have at least 20 characters"):
            resolve(probable, "F1", "accepted", "meh", fingerprint="fp1")

        _, fixed = resolve(
            probable, "F1", "fixed", "Guarded the empty list in total().", fingerprint="fp2"
        )
        _, accepted = resolve(
            probable, "F1", "accepted", "Callers never pass an empty list.", fingerprint="fp1"
        )
        self.assertEqual((fixed.resolution, accepted.resolution), ("fixed", "accepted"))

    def test_blockers_cannot_be_accepted(self) -> None:
        ledger, _ = prepared(make_snap(make_hunk("H1")))
        ledger, _ = set_verdict(with_finding(ledger, "blocker"), "F1", "probable", PROBABLE, None)

        with self.assertRaisesRegex(LedgerError, "'accepted' is not allowed for blocker findings"):
            resolve(ledger, "F1", "accepted", "We will live with this for now.", fingerprint="fp2")


class RunAndAgentTests(unittest.TestCase):
    def test_runs_get_sequential_ids_and_explicit_duplicates_fail(self) -> None:
        ledger = add_run(add_run(Ledger(), make_run()), make_run())

        self.assertEqual([run.id for run in ledger.runs], ["R1", "R2"])
        with self.assertRaisesRegex(LedgerError, "must not repeat an id"):
            add_run(ledger, replace(make_run(), id="R2"))

    def test_run_validation(self) -> None:
        with self.assertRaisesRegex(LedgerError, "argv must name a command"):
            replace(make_run(), argv=())
        with self.assertRaisesRegex(LedgerError, "output_tail must keep only the last 4000"):
            replace(make_run(), output_tail="x" * 4001)

    def test_agent_events_are_appended(self) -> None:
        event = AgentEvent(
            platform="claude", event="stop", agent="craftsman-reviewer", agent_id="a1", at=T1
        )
        self.assertEqual(add_agent_event(Ledger(), event).agents, (event,))

    def test_trivial_commands_are_recognized(self) -> None:
        trivial = [
            ["true"],
            [":"],
            ["exit"],
            ["echo", "tests passed"],
            ["printf", "ok"],
            ["/bin/echo", "x"],
            ["env", "CI=1", "true"],
            ["env", "-i", "PATH=/bin", "echo"],
            ["cmd", "/c", "exit", "0"],
            ["python", "-c", "pass"],
            ["python3", "-c", "pass"],
            ["sh", "-c", "true && echo ok"],
            ["bash", "-c", "exit 0"],
            ["C:\\Windows\\System32\\cmd.exe", "/C", "exit 0"],
        ]
        real = [
            ["pytest", "-q"],
            ["python3", "-m", "unittest"],
            ["sh", "-c", "echo start && pytest"],
            ["env", "CI=1", "npm", "test"],
            ["python", "-c", "import app; app.main()"],
            ["bash", "-c", "make test 'unbalanced"],
        ]
        for argv in trivial:
            with self.subTest(argv=argv):
                self.assertTrue(is_trivial_run(argv))
        for argv in real:
            with self.subTest(argv=argv):
                self.assertFalse(is_trivial_run(argv))

    def test_short_command_truncates_long_commands_only(self) -> None:
        long_run = make_run(("python3", "-c", "x" * 100))

        self.assertEqual(make_run().short_command, "pytest -q")
        self.assertEqual(len(long_run.short_command), 60)
        self.assertTrue(long_run.short_command.startswith("python3 -c xxx"))
        self.assertTrue(long_run.short_command.endswith("…"))


class GateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.snap = make_snap(make_hunk("H1", "a.py"), make_hunk("H2", "b.py"))

    def test_empty_snapshot_passes_unless_confirmed_findings_are_open(self) -> None:
        empty = make_snap(fingerprint="empty")
        self.assertTrue(evaluate(Ledger(), empty, QUICK).passed)
        self.assertIn("Nothing to review", evaluate(Ledger(), empty, QUICK).summary)

        ledger, _ = prepared(self.snap)
        ledger = add_run(with_finding(ledger), make_run(kind="repro", exit_code=1))
        ledger, _ = set_verdict(ledger, "F1", "proven", EVIDENCE, "R1")

        self.assertEqual(codes(evaluate(ledger, empty, QUICK)), ["unresolved"])

    def test_fully_reviewed_change_passes(self) -> None:
        report = evaluate(fully_reviewed(self.snap), self.snap, QUICK)

        self.assertTrue(report.passed, render_report(report))
        self.assertEqual(report.warnings, ())
        self.assertIn("Coverage: 4/4 lens-hunk pairs", report.summary)
        self.assertTrue(render_report(report).startswith("**Craftsman gate: PASSED**"))

    def test_no_round(self) -> None:
        report = evaluate(Ledger(), self.snap, QUICK)
        self.assertEqual(codes(report), ["no-round", "no-passing-run"])
        self.assertIn("craftsman review prepare", report.issues[0].message)

    def test_stale_round_still_reports_the_rest(self) -> None:
        ledger, _ = prepared(self.snap)
        changed = make_snap(*self.snap.hunks, fingerprint="fp2")

        report = evaluate(ledger, changed, QUICK)

        self.assertEqual(codes(report), ["stale-round", "uncovered", "uncovered", "no-passing-run"])
        self.assertIn("since round 1 was prepared", report.issues[0].message)

    def test_changed_base_is_reported_as_stale(self) -> None:
        ledger = fully_reviewed(self.snap)
        moved = make_snap(*self.snap.hunks, base="c" * 40)

        (issue,) = evaluate(ledger, moved, QUICK).issues

        self.assertEqual(issue.code, "stale-round")
        self.assertIn("craftsman review reset", issue.message)

    def test_uncovered_lists_hunks_and_the_command(self) -> None:
        ledger, _ = prepared(self.snap)
        ledger, _ = add_coverage(ledger, "intent", ["H1"], note_for(["H1"]))
        ledger = add_run(ledger, make_run())

        report = evaluate(ledger, self.snap, QUICK)

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
        ledger, _ = prepared(snap, ledger=ledger, symbols={"H2": ["compute_total"]})
        ledger = add_run(ledger, make_run(fingerprint="fp2"))

        report = evaluate(ledger, snap, QUICK)

        self.assertEqual(codes(report), ["uncovered", "uncovered"])
        self.assertIn("2 uncovered hunk(s) in round 2: H1,H2", report.issues[0].message)
        self.assertIn("Coverage: 0/4 lens-hunk pairs", report.summary)

    def test_sweep_is_reported_only_after_every_other_lens_and_must_come_last(self) -> None:
        with mock.patch("craftsman.ledger._now", side_effect=clock_from()):
            ledger, _ = prepared(self.snap, lenses=("intent", "sweep"), tier="standard")
            ledger = add_run(ledger, make_run())

            pending = evaluate(ledger, self.snap, QUICK)
            self.assertEqual(codes(pending), ["uncovered"])
            self.assertIn("Lens `intent`", pending.issues[0].message)
            self.assertIn("Sweep pending", pending.summary)

            with self.assertRaisesRegex(LedgerError, "still uncovered in round 1: intent H1,H2"):
                add_coverage(ledger, "sweep", ["H1", "H2"], note_for(["H1", "H2"]))

            ledger, _ = add_coverage(ledger, "intent", ["H1", "H2"], note_for(["H1", "H2"]))
            ledger, _ = add_coverage(ledger, "sweep", ["H1"], note_for(["H1"]))
            partial = evaluate(ledger, self.snap, QUICK)
            self.assertEqual(codes(partial), ["uncovered"])
            self.assertIn("Lens `sweep` has 1 uncovered hunk(s) in round 1: H2", partial.issues[0].message)

            ledger, _ = add_coverage(ledger, "sweep", ["H2"], note_for(["H2"]))
            self.assertTrue(evaluate(ledger, self.snap, QUICK).passed)

    def test_uncovered_sweep_is_listed_once_the_lenses_are_done(self) -> None:
        ledger, _ = prepared(self.snap, lenses=("intent", "sweep"), tier="standard")
        ledger, _ = add_coverage(ledger, "intent", ["H1", "H2"], note_for(["H1", "H2"]))
        ledger = add_run(ledger, make_run())

        report = evaluate(ledger, self.snap, QUICK)

        self.assertEqual(codes(report), ["uncovered"])
        self.assertIn("Lens `sweep` has 2 uncovered hunk(s)", report.issues[0].message)
        self.assertNotIn("Sweep pending", report.summary)

    def test_round_prepared_below_the_required_tier_is_rejected(self) -> None:
        ledger = fully_reviewed(self.snap)
        standard = GatePolicy(tier="standard", require_isolation=False)

        report = evaluate(ledger, self.snap, standard)

        self.assertEqual(codes(report), ["tier-too-low"])
        self.assertIn("craftsman review prepare --tier standard", report.issues[0].message)

    def test_no_verdict(self) -> None:
        report = evaluate(with_finding(fully_reviewed(self.snap)), self.snap, QUICK)
        self.assertEqual(codes(report), ["no-verdict"])
        self.assertIn("craftsman review verdict F1", report.issues[0].message)

    def test_unresolved(self) -> None:
        ledger, _ = set_verdict(
            with_finding(fully_reviewed(self.snap)), "F1", "probable", PROBABLE, None
        )
        report = evaluate(ledger, self.snap, QUICK)
        self.assertEqual(codes(report), ["unresolved"])
        self.assertIn("craftsman review resolve F1 --as fixed", report.issues[0].message)

    def test_no_passing_run_ignores_stale_failing_and_trivial_runs(self) -> None:
        ledger, _ = prepared(self.snap)
        for lens in ("intent", "craft"):
            ledger, _ = add_coverage(ledger, lens, ["H1", "H2"], note_for(["H1", "H2"]))
        ledger = add_run(ledger, make_run(fingerprint="old"))
        ledger = add_run(ledger, make_run(("true",)))
        ledger = add_run(ledger, make_run(("pytest", "-x"), kind="lint"))

        report = evaluate(ledger, self.snap, QUICK)

        self.assertEqual(codes(report), ["no-passing-run"])
        self.assertIn("craftsman run --kind test --", report.issues[0].message)
        self.assertEqual(
            report.warnings, ("R2 (`true`) is trivial and does not count as verification.",)
        )

    def test_deep_tier_requires_an_e2e_run(self) -> None:
        ledger = fully_reviewed(self.snap, tier="deep")
        deep = GatePolicy(tier="deep", require_isolation=False)

        self.assertEqual(codes(evaluate(ledger, self.snap, deep)), ["no-e2e-run"])
        ledger = add_run(ledger, make_run(("python3", "scripts/smoke.py"), kind="e2e"))
        self.assertTrue(evaluate(ledger, self.snap, deep).passed)

    def test_failing_run_persists_across_fingerprints_until_the_same_command_passes(self) -> None:
        ledger = fully_reviewed(self.snap)
        ledger = add_run(
            ledger, make_run(("ruff", "check"), kind="lint", exit_code=1, fingerprint="old")
        )

        report = evaluate(ledger, self.snap, QUICK)
        self.assertEqual(codes(report), ["failing-run"])
        self.assertIn("`ruff check` (lint, R2) last exited 1", report.issues[0].message)

        ledger = add_run(ledger, make_run(("ruff", "check"), kind="lint"))
        self.assertTrue(evaluate(ledger, self.snap, QUICK).passed)

    def test_isolation_requires_a_reviewer_stop_after_the_round_was_prepared(self) -> None:
        standard = GatePolicy(tier="standard", require_isolation=True)
        with mock.patch("craftsman.ledger._now", return_value=T1):
            ledger = fully_reviewed(self.snap, tier="standard")

        def stop(agent: str, at: str) -> AgentEvent:
            return AgentEvent(platform="copilot", event="stop", agent=agent, agent_id="x", at=at)

        report = evaluate(ledger, self.snap, standard)
        self.assertEqual(codes(report), ["no-isolation"])
        self.assertIn("spawn the craftsman-reviewer subagent for round 1", report.issues[0].message)

        too_early = add_agent_event(ledger, stop("craftsman-reviewer", T0))
        wrong_agent = add_agent_event(ledger, stop("explorer", "2026-01-01T01:00:00+00:00"))
        started_only = add_agent_event(ledger, replace(stop("craftsman-reviewer", T1), event="start"))
        for candidate in (too_early, wrong_agent, started_only):
            self.assertEqual(codes(evaluate(candidate, self.snap, standard)), ["no-isolation"])

        reviewed = add_agent_event(ledger, stop("Craftsman-Reviewer#2", T1))
        self.assertTrue(evaluate(reviewed, self.snap, standard).passed)
        self.assertFalse(evaluate(reviewed, self.snap, standard).self_reviewed)
        self.assertTrue(evaluate(ledger, self.snap, replace(standard, tier="quick")).passed)

    def test_standard_review_without_isolation_is_marked_self_reviewed(self) -> None:
        unisolated = GatePolicy(tier="standard", require_isolation=False)
        ledger = fully_reviewed(self.snap, tier="standard")

        report = evaluate(ledger, self.snap, unisolated)

        self.assertTrue(report.passed)
        self.assertTrue(report.self_reviewed)
        self.assertIn(
            "SELF-REVIEWED: no evidence that anyone other than the author reviewed this change",
            render_report(report),
        )

    def test_round_cap(self) -> None:
        ledger = Ledger()
        for number in range(1, 4):
            ledger, _ = prepared(make_snap(*self.snap.hunks, fingerprint=f"fp{number}"), ledger=ledger)
        policy = GatePolicy(tier="quick", require_isolation=False, max_rounds=2)

        report = evaluate(ledger, make_snap(*self.snap.hunks, fingerprint="fp3"), policy)

        self.assertEqual(codes(report)[-1], "round-cap")
        self.assertIn("stop iterating and report", report.issues[-1].message)

    def test_warnings_for_accepted_findings_and_oversized_quick_reviews(self) -> None:
        big = make_snap(
            *(
                make_hunk(f"H{index}", f"f{index}.py", ("+a", "+b", "+c", "+d", "+e"))
                for index in range(1, 10)
            )
        )
        ledger = with_finding(with_finding(fully_reviewed(big)))
        ledger, _ = set_verdict(ledger, "F1", "probable", PROBABLE, None)
        ledger, _ = resolve(
            ledger, "F1", "accepted", "Callers never pass an empty list.", fingerprint="fp1"
        )
        ledger, _ = set_verdict(ledger, "F2", "dismissed", DISMISSED, None)

        report = evaluate(ledger, big, QUICK)

        self.assertTrue(report.passed)
        self.assertEqual(
            report.warnings,
            (
                "Accepted without a fix: F1; tell the user about them.",
                "The quick tier reviews 45 changed lines (> 40); consider the standard tier.",
            ),
        )
        self.assertIn("1 accepted (F1)", report.summary)

    def test_process_anomaly_warnings_are_reported_without_blocking(self) -> None:
        ledger = fully_reviewed(self.snap)
        for index in range(1, 6):
            ledger = with_finding(ledger)
            ledger, _ = set_verdict(ledger, f"F{index}", "probable", PROBABLE, None)
            ledger, _ = resolve(
                ledger, f"F{index}", "accepted", f"Accepted reason {index} at a.py:1", fingerprint="fp1"
            )
        ledger = add_run(ledger, make_run(("pytest", "-q"), kind="test", exit_code=1))
        ledger = add_run(ledger, make_run(("env", "FOO=1", "pytest", "-q"), kind="test", exit_code=0))

        report = evaluate(ledger, self.snap, QUICK)

        self.assertIn(
            "Process anomaly: 5 findings and 0 dismissed (all findings confirmed — typical of self-review).",
            report.warnings,
        )
        self.assertTrue(
            any("repeated as R3 with a different environment" in w for w in report.warnings)
        )

    def test_render_report_caps_next_actions(self) -> None:
        ledger, _ = prepared(self.snap)
        for _ in range(14):
            ledger = with_finding(ledger)

        rendered = render_report(evaluate(ledger, self.snap, QUICK))

        self.assertIn("**Craftsman gate: BLOCKED (17 open items)**", rendered)
        self.assertIn("12. [no-verdict]", rendered)
        self.assertNotIn("13. ", rendered)
        self.assertTrue(rendered.endswith("… and 5 more"))

    def test_review_only_passes_without_resolved_findings_or_passing_runs(self) -> None:
        with mock.patch("craftsman.ledger._now", side_effect=clock_from()):
            ledger, _ = prepared(self.snap, lenses=("intent", "sweep"), tier="standard")
            ledger, _ = add_coverage(ledger, "intent", ["H1", "H2"], note_for(["H1", "H2"]))
            ledger, _ = add_coverage(ledger, "sweep", ["H1", "H2"], note_for(["H1", "H2"]))
            ledger = add_run(ledger, make_run(kind="repro", exit_code=1))
            ledger = add_run(ledger, make_run(("pytest", "-q"), kind="test", exit_code=1))
            ledger = with_finding(with_finding(ledger))
            ledger, _ = set_verdict(ledger, "F1", "proven", EVIDENCE, "R1")
            ledger, _ = set_verdict(ledger, "F2", "probable", PROBABLE, None)

        standard = GatePolicy(tier="standard", require_isolation=False)
        full_report = evaluate(ledger, self.snap, standard)
        self.assertFalse(full_report.passed)
        self.assertEqual(
            codes(full_report), ["unresolved", "unresolved", "no-passing-run", "failing-run"]
        )

        review_report = evaluate(ledger, self.snap, standard, review_only=True)
        self.assertTrue(review_report.passed, render_report(review_report))
        self.assertEqual(review_report.issues, ())
        self.assertIn("2 findings for the author: F1, F2.", review_report.warnings)
        self.assertIn("Failing run R2 (`pytest -q`, test) exited 1.", review_report.warnings)
        self.assertTrue(
            render_report(review_report).startswith("**Craftsman review: COMPLETE (review-only)**")
        )
        self.assertFalse(hasattr(standard, "review_only"))

    def test_review_only_still_requires_round_coverage_sweep_and_verdicts(self) -> None:
        standard = GatePolicy(tier="standard", require_isolation=False)
        self.assertEqual(
            codes(evaluate(Ledger(), self.snap, standard, review_only=True)), ["no-round"]
        )

        with mock.patch("craftsman.ledger._now", side_effect=clock_from()):
            ledger, _ = prepared(self.snap, lenses=("intent", "sweep"), tier="standard")
            self.assertEqual(
                codes(evaluate(ledger, self.snap, standard, review_only=True)), ["uncovered"]
            )
            ledger, _ = add_coverage(ledger, "intent", ["H1", "H2"], note_for(["H1", "H2"]))
            self.assertEqual(
                codes(evaluate(ledger, self.snap, standard, review_only=True)), ["uncovered"]
            )
            ledger, _ = add_coverage(ledger, "sweep", ["H1", "H2"], note_for(["H1", "H2"]))
            with_unjudged = with_finding(ledger)
            unjudged_report = evaluate(with_unjudged, self.snap, standard, review_only=True)
            self.assertEqual(codes(unjudged_report), ["no-verdict"])
            self.assertTrue(
                render_report(unjudged_report).startswith(
                    "**Craftsman review: INCOMPLETE (1 open items)**"
                )
            )
            changed = make_snap(*self.snap.hunks, fingerprint="fp2")
            self.assertIn(
                "stale-round", codes(evaluate(ledger, changed, standard, review_only=True))
            )


def clock_from(start: int = 0) -> Iterator[str]:
    return (f"2026-01-01T00:{second // 60:02d}:{second % 60:02d}.000Z" for second in range(start, 3600))


def coverage_record(round_number: int, lens: str, digests: Sequence[str], at: str) -> Coverage:
    return Coverage(round_number, lens, tuple(digests), note_for(["H1"]), at)


class HardeningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.snap = make_snap(make_hunk("H1", "a.py"), make_hunk("H2", "b.py"))

    def test_a_fix_that_is_reverted_counts_as_unresolved_again(self) -> None:
        ledger = add_run(with_finding(fully_reviewed(self.snap)), make_run(kind="repro", exit_code=1))
        ledger, _ = set_verdict(ledger, "F1", "proven", EVIDENCE, "R2")
        ledger, _ = resolve(ledger, "F1", "fixed", "Guarded the empty list in total().", fingerprint="fp2")

        (issue,) = evaluate(ledger, self.snap, QUICK).issues

        self.assertEqual(issue.code, "unresolved")
        self.assertIn("back to the code it was recorded at", issue.message)
        fixed = make_snap(*self.snap.hunks, fingerprint="fp2")
        self.assertNotIn("unresolved", codes(evaluate(ledger, fixed, QUICK)))

    def test_trivial_commands_hidden_behind_flags_are_recognized(self) -> None:
        trivial = [
            ["python3", "-B", "-c", "pass"],
            ["python3", "-u", "-c", ""],
            ["python3", "-Bc", "pass"],
            ["python3", "-W", "ignore", "-c", "pass"],
            ["python3", "-c", "0"],
            ["python", "-c", "True"],
            ["python3", "-c", "exit()"],
            ["python3", "-c", "quit(0)"],
            ["python3", "-c", "import sys; sys.exit(0)"],
            ["env", "python3", "-I", "-c", "'ok'"],
            ["sh", "-e", "-c", "true"],
            ["bash", "-lc", ":"],
            ["bash", "-o", "pipefail", "-c", "exit 0"],
            ["cmd", "/d", "/c", "exit", "0"],
            ["powershell", "-NoProfile", "-Command", "exit 0"],
            ["pwsh", "-c", "exit 0"],
            ["pwsh.exe", "-NonInteractive", "-command", "echo ok"],
        ]
        real = [
            ["python3", "-c", "print('ok')"],
            ["python3", "-c", "import app"],
            ["python3", "-c", "import sys; sys.exit(main())"],
            ["python3", "-c", "exit(run())"],
            ["python3", "-c", "syntax error("],
            ["python3", "-m", "pytest", "-c", "pass"],
            ["python3", "-B", "script.py", "-c", "pass"],
            ["bash", "-e", "run_tests.sh"],
            ["bash", "-lc", "pytest -q"],
            ["powershell", "-File", "test.ps1"],
            ["pwsh", "-Command", "Invoke-Pester"],
        ]
        for argv in trivial:
            with self.subTest(argv=argv):
                self.assertTrue(is_trivial_run(argv))
        for argv in real:
            with self.subTest(argv=argv):
                self.assertFalse(is_trivial_run(argv))

    def test_obsolete_failing_runs_can_be_waived(self) -> None:
        ledger = add_run(
            fully_reviewed(self.snap),
            make_run(("pytest", "tests/test_typo.py"), exit_code=4, fingerprint="old"),
        )
        (issue,) = evaluate(ledger, self.snap, QUICK).issues
        self.assertEqual(issue.code, "failing-run")
        self.assertIn("craftsman review waive-run R2", issue.message)

        cases = [
            ("R1", "The command was replaced by another one.", "R1 passed"),
            ("R9", "The command was replaced by another one.", "unknown run 'R9'; recorded runs: R1, R2"),
            ("R2", "typo", "reason must have at least 20 characters"),
        ]
        for run_id, reason, message in cases:
            with self.subTest(run_id=run_id), self.assertRaisesRegex(LedgerError, message):
                waive_run(ledger, run_id, reason)

        with mock.patch("craftsman.ledger._now", return_value=T1):
            waived, run = waive_run(ledger, "R2", "  Typo in the path; tests/test_cart.py is the real file.  ")

        self.assertEqual(
            run.waiver, RunWaiver("Typo in the path; tests/test_cart.py is the real file.", T1)
        )
        report = evaluate(waived, self.snap, QUICK)
        self.assertTrue(report.passed, render_report(report))
        self.assertIn("Waived failing runs: R2", " ".join(report.warnings))
        with self.assertRaisesRegex(LedgerError, "R2 is already waived"):
            waive_run(waived, "R2", "Typo in the path; tests/test_cart.py is the real file.")
        self.assertEqual(Ledger.from_json(json.loads(json.dumps(waived.to_json()))), waived)

    def test_a_later_failure_of_the_same_command_is_not_waived(self) -> None:
        ledger = add_run(fully_reviewed(self.snap), make_run(("ruff", "check"), kind="lint", exit_code=1))
        ledger, _ = waive_run(ledger, "R2", "Ran with a stale config before the rename.")
        ledger = add_run(ledger, make_run(("ruff", "check"), kind="lint", exit_code=1))

        self.assertEqual(codes(evaluate(ledger, self.snap, QUICK)), ["failing-run"])

    def test_failing_runs_are_tracked_per_working_directory(self) -> None:
        ledger = fully_reviewed(self.snap)
        ledger = add_run(ledger, replace(make_run(exit_code=1), cwd="/repo/pkg_a"))
        ledger = add_run(ledger, replace(make_run(), cwd="/repo/pkg_b"))

        (issue,) = evaluate(ledger, self.snap, QUICK).issues

        self.assertEqual(issue.code, "failing-run")
        self.assertIn("R2", issue.message)

    def test_a_run_that_modified_the_tree_never_counts_as_passing(self) -> None:
        ledger, _ = prepared(self.snap)
        for lens in ("intent", "craft"):
            ledger, _ = add_coverage(ledger, lens, ["H1", "H2"], note_for(["H1", "H2"]))
        ledger = add_run(ledger, make_run(exit_code=1))
        ledger = add_run(ledger, replace(make_run(), fingerprint_after="fp2"))

        self.assertEqual(codes(evaluate(ledger, self.snap, QUICK)), ["no-passing-run", "failing-run"])
        with self.assertRaisesRegex(LedgerError, "fingerprint_after must differ"):
            replace(make_run(), fingerprint_after="fp1")

    def test_partially_covered_hunks_reopened_by_a_changed_symbol_need_every_lens_again(self) -> None:
        caller = make_hunk("H1", "a.py", (" total = compute_total(x)", "+y = 1"))
        definer = make_hunk("H2", "b.py", ("+def compute_total(items):",))
        ledger, _ = prepared(make_snap(caller, definer))
        ledger, _ = add_coverage(ledger, "intent", ["H1", "H2"], note_for(["H1", "H2"]))
        ledger, _ = add_coverage(ledger, "craft", ["H2"], note_for(["H2"]))

        changed = make_hunk("H2", "b.py", ("+def compute_total(items, rate):",))
        snap = make_snap(caller, changed, fingerprint="fp2")
        ledger, round_ = prepared(snap, ledger=ledger, symbols={"H2": ["compute_total"]})

        self.assertEqual([(h.carried, h.reopened) for h in round_.hunks], [(False, True), (False, False)])
        self.assertEqual(uncovered_by_lens(ledger, round_), {"intent": ["H1", "H2"], "craft": ["H1", "H2"]})

    def test_a_hunk_reopened_in_an_abandoned_round_keeps_needing_fresh_coverage(self) -> None:
        caller = make_hunk("H1", "a.py", (" total = compute_total(x)", "+y = 1"))
        ledger = fully_reviewed(make_snap(caller, make_hunk("H2", "b.py", ("+def compute_total(a):",))))
        changed = make_hunk("H2", "b.py", ("+def compute_total(a, b):",))
        ledger, _ = prepared(
            make_snap(caller, changed, fingerprint="fp2"), ledger=ledger, symbols={"H2": ["compute_total"]}
        )
        ledger, _ = add_coverage(ledger, "intent", ["H1"], note_for(["H1"]))

        unrelated = make_hunk("H3", "c.py")
        snap = make_snap(caller, changed, unrelated, fingerprint="fp3")
        ledger, round_ = prepared(snap, ledger=ledger, symbols={"H2": ["compute_total"]})

        self.assertFalse(round_.hunks[0].reopened)
        self.assertEqual(uncovered_by_lens(ledger, round_)["craft"], ["H1", "H2", "H3"])
        self.assertEqual(uncovered_by_lens(ledger, round_)["intent"], ["H2", "H3"])

    def test_a_tier_upgrade_keeps_lens_coverage_but_needs_a_fresh_sweep(self) -> None:
        with mock.patch("craftsman.ledger._now", side_effect=clock_from()):
            ledger, _ = prepared(self.snap, lenses=("intent", "sweep"), tier="standard")
            ledger, _ = add_coverage(ledger, "intent", ["H1", "H2"], note_for(["H1", "H2"]))
            ledger, _ = add_coverage(ledger, "sweep", ["H1", "H2"], note_for(["H1", "H2"]))
            ledger = add_run(ledger, make_run())
            ledger, round_ = prepared(self.snap, ("intent", "craft", "sweep"), "deep", ledger)

            self.assertEqual(
                uncovered_by_lens(ledger, round_), {"intent": [], "craft": ["H1", "H2"], "sweep": ["H1", "H2"]}
            )
            ledger, _ = add_coverage(ledger, "craft", ["H1", "H2"], note_for(["H1", "H2"]))
            self.assertEqual(codes(evaluate(ledger, self.snap, QUICK)), ["uncovered"])
            ledger, _ = add_coverage(ledger, "sweep", ["H1", "H2"], note_for(["H1", "H2"]))
            self.assertTrue(evaluate(ledger, self.snap, QUICK).passed)

    def test_a_premature_sweep_is_never_carried_into_a_later_round(self) -> None:
        ledger, _ = prepared(make_snap(self.snap.hunks[0]), lenses=("intent", "sweep"), tier="standard")
        digest = self.snap.hunks[0].digest
        ledger = replace(
            ledger,
            coverage=(
                coverage_record(1, "sweep", [digest], T0),
                coverage_record(1, "intent", [digest], T1),
            ),
        )

        snap = make_snap(*self.snap.hunks, fingerprint="fp2")
        _, round_ = prepared(snap, ("intent", "sweep"), "standard", ledger)

        self.assertEqual([hunk.carried for hunk in round_.hunks], [False, False])

    def test_a_later_duplicate_lens_note_does_not_invalidate_the_sweep(self) -> None:
        with mock.patch("craftsman.ledger._now", side_effect=clock_from()):
            ledger, _ = prepared(self.snap, lenses=("intent", "sweep"), tier="standard")
            ledger = add_run(ledger, make_run())
            ledger, _ = add_coverage(ledger, "intent", ["H1", "H2"], note_for(["H1", "H2"]))
            ledger, _ = add_coverage(ledger, "sweep", ["H1", "H2"], note_for(["H1", "H2"]))
            ledger, _ = add_coverage(ledger, "intent", ["H1"], note_for(["H1"]))

        self.assertTrue(evaluate(ledger, self.snap, QUICK).passed)

    def test_a_sweep_recorded_before_the_lenses_completed_does_not_count(self) -> None:
        ledger, _ = prepared(self.snap, lenses=("intent", "sweep"), tier="standard")
        h1, h2 = (hunk.digest for hunk in self.snap.hunks)
        ledger = add_run(
            replace(
                ledger,
                coverage=(
                    coverage_record(1, "intent", [h1], T0),
                    coverage_record(1, "sweep", [h1, h2], "2026-01-01T00:01:00.000Z"),
                    coverage_record(1, "intent", [h2], T1),
                ),
            ),
            make_run(),
        )

        (issue,) = evaluate(ledger, self.snap, QUICK).issues

        self.assertEqual(issue.code, "sweep-too-early")
        self.assertIn("The final sweep of H1, H2 was recorded before", issue.message)

    def test_a_trivial_run_is_not_verdict_evidence(self) -> None:
        ledger = add_run(with_finding(fully_reviewed(self.snap)), make_run(("true",), kind="repro"))

        with self.assertRaises(FieldError) as caught:
            set_verdict(ledger, "F1", "proven", EVIDENCE, "R2")

        self.assertEqual(caught.exception.field, "verdict_run")
        self.assertIn("R2 (`true`) is trivial", str(caught.exception))

    def test_dismissals_may_cite_extensionless_files(self) -> None:
        ledger = with_finding(fully_reviewed(self.snap))
        for evidence in (
            "guarded by the check in Makefile:12 before deploy",
            "the Dockerfile:3 base image pins the version",
            "bin/tool:12 rejects an empty list first",
            "build\\run:7 rejects an empty list first",
            "the justfile:4 recipe sets the flag already",
        ):
            with self.subTest(evidence=evidence):
                _, finding = set_verdict(ledger, "F1", "dismissed", evidence, None)
                self.assertEqual(finding.verdict, "dismissed")
        with self.assertRaisesRegex(LedgerError, "must cite the guard as file:line"):
            set_verdict(ledger, "F1", "dismissed", "the ratio 3:4 is always respected here", None)

    def test_findings_are_bound_to_the_reviewed_code(self) -> None:
        ledger, _ = prepared(self.snap)
        ledger, finding = add_finding(
            ledger,
            lens="intent",
            path="a.py",
            line=1,
            severity="major",
            title="Empty list crashes total()",
            scenario="Calling total([]) divides by len(items) == 0 and raises.",
            suggestion="",
        )
        self.assertEqual(finding.fingerprint, "fp1")

    def test_a_diff_without_hunks_needs_no_reviewer(self) -> None:
        snap = DiffSnapshot(BASE, "fp1", (), ("logo.png",), (), 0, 0)
        ledger, _ = prepared(snap, tier="standard")
        ledger = add_run(ledger, make_run())
        isolated = GatePolicy(tier="standard", require_isolation=True)

        report = evaluate(ledger, snap, isolated)

        self.assertTrue(report.passed, render_report(report))

    def test_deleted_lines_and_markers_do_not_reopen_a_hunk(self) -> None:
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

    def test_reopening_scales_linearly_with_the_diff(self) -> None:
        count = 1500
        hunks = [
            make_hunk(f"H{i}", f"f{i}.py", (f"+def symbol_{i}():", f" symbol_{i + 1}()"))
            for i in range(1, count + 1)
        ]
        symbols = {f"H{i}": [f"symbol_{i}"] for i in range(1, count + 1)}
        ledger = fully_reviewed(make_snap(*hunks))
        changed = make_hunk(f"H{count}", f"f{count}.py", (f"+def symbol_{count}(x):",))
        snap = make_snap(*hunks[:-1], changed, fingerprint="fp2")

        started = time.monotonic()
        ledger, round_ = prepared(snap, ledger=ledger, symbols=symbols)
        mentions = reopened_mentions(ledger.coverage, round_, snap.hunks, symbols)

        self.assertLess(time.monotonic() - started, 5.0)
        self.assertFalse(any(hunk.carried for hunk in round_.hunks))
        self.assertEqual(mentions["H1"], (("symbol_2", "H2"),))

    def test_round_cap_counts_distinct_diffs(self) -> None:
        ledger, _ = prepared(self.snap)
        ledger, _ = prepared(self.snap, tier="standard", ledger=ledger)
        ledger, _ = prepared(self.snap, tier="deep", ledger=ledger)
        policy = GatePolicy(tier="quick", require_isolation=False, max_rounds=1)

        self.assertNotIn("round-cap", codes(evaluate(ledger, self.snap, policy)))


class SerializationTests(unittest.TestCase):
    def full_ledger(self) -> Ledger:
        snap = make_snap(make_hunk("H1", "a.py"), make_hunk("H2", "b.py"))
        ledger = fully_reviewed(snap)
        ledger = add_run(with_finding(ledger), make_run(kind="repro", exit_code=1))
        ledger, _ = set_verdict(ledger, "F1", "proven", EVIDENCE, "R2")
        ledger, _ = resolve(
            ledger, "F1", "fixed", "Guarded the empty list in total().", fingerprint="fp2"
        )
        event = AgentEvent(
            platform="claude", event="stop", agent="craftsman-reviewer", agent_id="a", at=T1
        )
        return add_agent_event(ledger, event)

    def test_round_trip_preserves_every_record(self) -> None:
        ledger = self.full_ledger()
        encoded = json.loads(json.dumps(ledger.to_json()))

        self.assertEqual(encoded["schema"], 1)
        self.assertEqual(Ledger.from_json(encoded), ledger)

    def test_invalid_data_names_the_offending_field(self) -> None:
        def broken(change: dict[str, object], section: str = "findings") -> dict[str, object]:
            raw = self.full_ledger().to_json()
            raw[section][0].update(change)
            return raw

        cases = [
            (
                broken({"severity": "critical"}),
                "ledger.json: findings[0].severity must be one of blocker, major",
            ),
            (broken({"line": True}), "ledger.json: findings[0].line must be an integer"),
            (broken({"surprise": 1}), "ledger.json: findings[0].surprise is not a known field"),
            (
                broken({"verdict_run": "R9"}),
                r"ledger.json: findings\[0\].verdict_run references an unknown run",
            ),
            (broken({"digest": 5}, "rounds"), "ledger.json: rounds[0].digest is not a known field"),
            ({**self.full_ledger().to_json(), "schema": 2}, "schema 2 is not supported"),
            ([], "ledger.json: must be a JSON object"),
        ]
        for raw, message in cases:
            with self.subTest(message=message), self.assertRaises(LedgerError) as caught:
                Ledger.from_json(raw)
            self.assertIn(message.replace("\\", ""), str(caught.exception))

    def test_nested_record_errors_include_their_path(self) -> None:
        raw = self.full_ledger().to_json()
        raw["rounds"][0]["hunks"][1]["new_len"] = "two"

        with self.assertRaisesRegex(
            LedgerError, r"ledger.json: rounds\[0\].hunks\[1\].new_len must be an integer"
        ):
            Ledger.from_json(raw)


class PersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "craftsman" / LEDGER_FILENAME

    def test_missing_file_loads_as_empty_and_save_round_trips(self) -> None:
        self.assertEqual(load(self.path), Ledger())
        ledger = fully_reviewed(make_snap(make_hunk("H1")))

        save(self.path, ledger)

        self.assertEqual(load(self.path), ledger)
        self.assertEqual([p.name for p in self.path.parent.iterdir()], [LEDGER_FILENAME])

    def test_corrupt_json_raises(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{not json", encoding="utf-8")
        with self.assertRaisesRegex(LedgerError, "is not valid JSON"):
            load(self.path)

    def test_mutate_persists_changes_and_returns_the_result(self) -> None:
        number = mutate(self.path, lambda ledger: (add_run(ledger, make_run()), len(ledger.runs) + 1))
        unchanged = mutate(self.path, lambda ledger: (ledger, len(ledger.runs)))

        self.assertEqual((number, unchanged), (1, 1))
        self.assertEqual([run.id for run in load(self.path).runs], ["R1"])

    def test_second_lock_times_out_quickly(self) -> None:
        with locked(self.path):
            started = time.monotonic()
            with self.assertRaisesRegex(LedgerError, "ledger.lock"), locked(self.path, timeout_s=0.2):
                pass
            self.assertLess(time.monotonic() - started, 2.0)
        with locked(self.path, timeout_s=0.2):
            pass
        self.assertTrue(self.path.with_suffix(".lock").is_file())

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
            root = Path(tmp).resolve()
            init_repo(root)
            (root / "sub").mkdir()
            expected = root / ".git" / "craftsman" / LEDGER_FILENAME
            self.assertEqual(ledger_path_for(root / "sub"), expected)
        with (
            tempfile.TemporaryDirectory() as outside,
            self.assertRaisesRegex(LedgerError, "cannot locate"),
        ):
            ledger_path_for(outside)


class EndToEndTests(unittest.TestCase):
    def test_editing_a_file_after_review_makes_the_round_stale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            init_repo(root)
            source = root / "app.py"
            source.write_text("def total(items):\n    return sum(items)\n", encoding="utf-8")
            git(root, "add", "-A")
            git(root, "commit", "-q", "--no-verify", "-m", "base")
            base = git(root, "rev-parse", "HEAD").strip()
            source.write_text("def total(items):\n    return sum(items) or 0\n", encoding="utf-8")
            snap = snapshot(root, base)
            ledger = fully_reviewed(snap)
            self.assertTrue(evaluate(ledger, snap, QUICK).passed)

            source.write_text("def total(items):\n    return sum(items) or None\n", encoding="utf-8")
            edited = snapshot(root, base)

            self.assertEqual(codes(evaluate(ledger, edited, QUICK)), ["stale-round", "no-passing-run"])
            ledger, round_ = prepared(edited, ledger=ledger)
            self.assertEqual((round_.number, [hunk.carried for hunk in round_.hunks]), (2, [False]))


if __name__ == "__main__":
    unittest.main()
