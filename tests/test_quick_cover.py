"""Acceptance tests for WP2: size-based review-only tier, multi-lens cover, and quick file coverage."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from craftsman.diffscan import SIGNALS
from craftsman.gatecheck import GatePolicy, evaluate
from craftsman.ledger import add_coverage, add_multi_coverage, uncovered_by_lens
from craftsman.lenses import QUICK_TRUST_TRIGGERS, Tier, review_only_tier, select_lenses
from craftsman.records import FieldError, Ledger, LedgerError

from tests.support import EvidenceRepo, assert_cli_error, make_hunk, make_snap, prepared, record_run

QUICK = GatePolicy(tier="quick")
STANDARD = GatePolicy(tier="standard")


def _small_functions(count: int, *, lines_per_fn: int = 10, tag: str = "v1") -> str:
    """Generate `count * lines_per_fn` lines of Python without triggering `long-function` (>= 40)."""
    blocks: list[str] = []
    for fn_idx in range(count):
        body = [f"def fn_{fn_idx}(x: int) -> int:"]
        for step in range(lines_per_fn - 2):
            body.append(f"    val_{step} = x + {fn_idx * 100 + step}  # {tag}")
        body.append(f"    return val_{lines_per_fn - 3}")
        blocks.append("\n".join(body))
    return "\n".join(blocks) + "\n"


def _spaced_three_hunk_file(prefix: str, *, values: tuple[int, int, int]) -> str:
    """A module with 3 functions separated by 12 unchanged filler lines so edits form 3 git hunks."""
    sections: list[str] = []
    for idx, val in enumerate(values, 1):
        filler = "\n".join(f"# {prefix} section {idx} filler line {n}" for n in range(12))
        fn = f"def {prefix}_fn_{idx}(x: int) -> int:\n    return x + {val}\n"
        sections.append(f"{filler}\n{fn}")
    return "\n".join(sections)


def _long_note_for(targets: list[str]) -> str:
    """Concrete >= 80-char segment per target, suitable for multi-lens coverage entries."""
    parts = [
        f"{target} line 2: checked x=0 and negative inputs -> returns expected int without side effects"
        for target in targets
    ]
    return "; ".join(parts)


class QuickCoverRepoTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.repo = EvidenceRepo(self.tmp / "repo")

    def review(
        self, *args: str, check: bool = True, stdin: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        return self.repo.cli("review", *args, check=check, stdin=stdin)

    def assert_error(
        self, proc: subprocess.CompletedProcess[str], exit_code: int, fragment: str
    ) -> None:
        assert_cli_error(self, proc, exit_code, fragment)


class ReviewOnlyTierTests(QuickCoverRepoTestCase):
    def test_review_only_tier_follows_size_thresholds(self) -> None:
        self.assertEqual(review_only_tier(120, has_skipped=False, docs_only=False), "quick")
        self.assertEqual(review_only_tier(250, has_skipped=False, docs_only=False), "quick")
        self.assertEqual(review_only_tier(251, has_skipped=False, docs_only=False), "standard")
        self.assertEqual(review_only_tier(300, has_skipped=False, docs_only=False), "standard")
        self.assertEqual(review_only_tier(10, has_skipped=True, docs_only=False), "standard")
        self.assertEqual(review_only_tier(400, has_skipped=True, docs_only=True), "quick")

    def test_prepare_without_tier_selects_quick_for_120_lines(self) -> None:
        self.repo.write("calc.py", _small_functions(12, lines_per_fn=10))

        out = self.review("prepare").stdout

        self.assertIn("tier quick: 120 changed lines <= 250", out.lower())
        shown = self.repo.show()
        self.assertEqual(shown["round"]["tier"], "quick")
        self.assertEqual(shown["round"]["lenses"], ["intent", "contracts", "failure"])

    def test_prepare_without_tier_selects_standard_for_300_lines(self) -> None:
        self.repo.write("calc.py", _small_functions(30, lines_per_fn=10))

        out = self.review("prepare").stdout

        self.assertIn("tier standard: 300 changed lines > 250", out.lower())
        self.assertEqual(self.repo.show()["round"]["tier"], "standard")

    def test_prepare_without_tier_selects_standard_when_binary_file_is_skipped(self) -> None:
        self.repo.write("calc.py", _small_functions(1, lines_per_fn=10))
        (self.repo.root / "asset.bin").write_bytes(b"\x00\x01\x02\xffbinary")

        out = self.review("prepare").stdout

        self.assertIn("tier standard: skipped binary or oversized file", out.lower())
        self.assertEqual(self.repo.show()["round"]["tier"], "standard")

    def test_prepare_with_active_gate_uses_gate_tier(self) -> None:
        self.repo.cli("gate", "--init", "--tier", "standard", "--cwd", ".")
        self.repo.write("calc.py", _small_functions(1, lines_per_fn=10))

        out = self.review("prepare").stdout

        self.assertIn("tier standard: active /craftsman gate", out.lower())
        self.assertEqual(self.repo.show()["round"]["tier"], "standard")

    def test_round_two_escalates_quick_to_standard_when_fix_exceeds_250_lines(self) -> None:
        self.repo.write("calc.py", _small_functions(10, lines_per_fn=10))
        self.review("prepare")
        self.assertEqual(self.repo.show()["round"]["tier"], "quick")

        self.repo.write("calc.py", _small_functions(30, lines_per_fn=10))
        out = self.review("prepare").stdout

        self.assertIn("tier standard: raised from quick (300 changed lines > 250)", out.lower())
        self.assertEqual(self.repo.show()["round"]["number"], 2)
        self.assertEqual(self.repo.show()["round"]["tier"], "standard")

    def test_round_two_keeps_standard_when_fix_shrinks_diff_below_250_lines(self) -> None:
        self.repo.write("calc.py", _small_functions(30, lines_per_fn=10))
        self.review("prepare")
        self.assertEqual(self.repo.show()["round"]["tier"], "standard")

        self.repo.write("calc.py", _small_functions(5, lines_per_fn=10))
        out = self.review("prepare").stdout

        self.assertIn("tier standard: kept from round 1 (50 changed lines <= 250)", out.lower())
        self.assertEqual(self.repo.show()["round"]["number"], 2)
        self.assertEqual(self.repo.show()["round"]["tier"], "standard")


class QuickWarningThresholdTests(QuickCoverRepoTestCase):
    def test_review_only_quick_for_120_lines_emits_no_size_warning(self) -> None:
        self.repo.write("calc.py", _small_functions(12, lines_per_fn=10))
        self.review("prepare")
        self.review(
            "cover",
            "--lens",
            "intent,contracts,failure",
            "--files",
            "calc.py",
            "--note",
            _long_note_for(["calc.py"]),
        )

        checked = self.review("check", "--review-only")

        self.assertEqual(checked.returncode, 0)
        self.assertNotIn("consider the standard tier", checked.stdout)

    def test_full_gate_quick_with_41_lines_still_warns_to_consider_standard(self) -> None:
        lines = tuple(f"+x_{idx} = {idx}" for idx in range(41))
        snap = make_snap(make_hunk("H1", "calc.py", lines))
        ledger, _ = prepared(snap, lenses=("intent", "contracts", "failure"), tier="quick")
        ledger, _ = add_multi_coverage(
            ledger,
            ("intent", "contracts", "failure"),
            ["calc.py"],
            _long_note_for(["calc.py"]),
            target_kind="files",
        )
        ledger = record_run(ledger, fingerprint=snap.fingerprint)

        report = evaluate(ledger, snap, QUICK, review_only=False)

        self.assertTrue(report.passed)
        self.assertTrue(any("consider the standard tier" in w for w in report.warnings))


class LensTriggerTests(unittest.TestCase):
    def _picked(self, tier: Tier, signals: dict[str, tuple[str, ...]]) -> list[str]:
        return [pick.lens.id for pick in select_lenses(tier, signals, docs_only=False)]

    def test_quick_ignores_non_security_signals(self) -> None:
        for signal in sorted(set(SIGNALS) - QUICK_TRUST_TRIGGERS):
            with self.subTest(signal=signal):
                self.assertEqual(
                    self._picked("quick", {signal: ("H1",)}),
                    ["intent", "contracts", "failure"],
                )

    def test_quick_adds_only_trust_for_security_signals(self) -> None:
        for signal in sorted(QUICK_TRUST_TRIGGERS):
            with self.subTest(signal=signal):
                self.assertEqual(
                    self._picked("quick", {signal: ("H1",)}),
                    ["intent", "contracts", "failure", "trust"],
                )

    def test_standard_filesystem_signal_adds_no_extra_lenses(self) -> None:
        picked = self._picked("standard", {"filesystem": ("H1",)})

        self.assertNotIn("state", picked)
        self.assertNotIn("trust", picked)
        self.assertNotIn("platform", picked)
        self.assertEqual(
            picked,
            ["intent", "contracts", "failure", "ripple", "tests", "craft", "design", "sweep"],
        )

    def test_standard_with_process_adds_trust_but_not_platform(self) -> None:
        picked = self._picked("standard", {"process": ("H1",)})

        self.assertIn("trust", picked)
        self.assertNotIn("platform", picked)


class MultiLensAndHintTests(QuickCoverRepoTestCase):
    def test_cover_multi_lens_validates_lens_tokens(self) -> None:
        self.repo.write("a.py", "def f(x: int) -> int:\n    return x + 1\n")
        self.review("prepare")

        unselected = self.review(
            "cover",
            "--lens",
            "intent,craft",
            "--hunks",
            "H1",
            "--note",
            _long_note_for(["H1"]),
            check=False,
        )
        self.assert_error(
            unselected,
            1,
            "--lens 'craft' is not selected in round 1; selected lenses: intent, contracts, failure",
        )

        unknown = self.review(
            "cover",
            "--lens",
            "nope",
            "--hunks",
            "H1",
            "--note",
            _long_note_for(["H1"]),
            check=False,
        )
        self.assert_error(unknown, 1, "--lens names unknown lens 'nope'; valid lenses:")

        snap = make_snap(make_hunk("H1", "a.py"))
        ledger, _ = prepared(snap, lenses=("intent", "contracts", "failure"), tier="quick")
        with self.assertRaisesRegex(FieldError, "unknown lens 'nope'; valid lenses:"):
            add_multi_coverage(ledger, ("nope",), ["H1"], _long_note_for(["H1"]))

    def test_multi_lens_rejects_sweep_or_design_combinations(self) -> None:
        snap = make_snap(make_hunk("H1", "a.py"))
        ledger, _ = prepared(
            snap, lenses=("intent", "contracts", "design", "sweep"), tier="standard"
        )

        with self.assertRaisesRegex(FieldError, "`sweep` runs last and cannot be combined"):
            add_multi_coverage(ledger, ("intent", "sweep"), ["H1"], _long_note_for(["H1"]))

        with self.assertRaisesRegex(FieldError, "cannot combine `design`"):
            add_multi_coverage(ledger, ("intent", "design"), ["a.py"], _long_note_for(["a.py"]))

    def test_multi_lens_note_enforces_constant_80_char_minimum(self) -> None:
        snap = make_snap(make_hunk("H1", "a.py"))
        ledger, _ = prepared(
            snap, lenses=("intent", "contracts", "failure", "craft"), tier="standard"
        )
        note_50 = "H1 a.py:1 f(0) -> returns 1 without raising error!"
        self.assertEqual(len(note_50), 50)

        with self.assertRaisesRegex(FieldError, "needs at least 80 characters"):
            add_multi_coverage(ledger, ("intent", "contracts", "failure"), ["H1"], note_50)

        note_80 = "H1 a.py:1 f(0) -> returns 1; negative x=-1 -> returns 0; callers pass int values"
        self.assertEqual(len(note_80), 80)
        updated, records = add_multi_coverage(
            ledger, ("intent", "contracts", "failure"), ["H1"], note_80
        )
        self.assertEqual([r.lens for r in records], ["intent", "contracts", "failure"])
        self.assertEqual(len(updated.coverage), 3)

        # Even with 4 lenses, the per-segment minimum stays at 80 characters (2 * 40).
        _, four_records = add_multi_coverage(
            ledger, ("intent", "contracts", "failure", "craft"), ["H1"], note_80
        )
        self.assertEqual(len(four_records), 4)

        praise = "H1 ok checked reviewed good clean fine safe ok checked reviewed good clean fine safe!"
        self.assertGreaterEqual(len(praise), 80)
        with self.assertRaisesRegex(FieldError, "too little concrete detail"):
            add_multi_coverage(ledger, ("intent", "contracts"), ["H1"], praise)

        vague_prose = (
            "H1 examined every branch and edge case across the implementation without finding flaws"
        )
        self.assertGreaterEqual(len(vague_prose), 80)
        with self.assertRaisesRegex(FieldError, "must include a concrete detail"):
            add_multi_coverage(ledger, ("intent", "contracts"), ["H1"], vague_prose)

    def test_standard_check_groups_uncovered_ordinary_lenses(self) -> None:
        snap = make_snap(make_hunk("H1", "a.py"), make_hunk("H2", "b.py"))
        ledger, _ = prepared(
            snap, lenses=("intent", "contracts", "design", "sweep"), tier="standard"
        )
        ledger = record_run(ledger, fingerprint=snap.fingerprint)

        report = evaluate(ledger, snap, STANDARD, review_only=True)

        self.assertEqual([issue.code for issue in report.issues], ["uncovered", "uncovered"])
        self.assertIn("--lens intent,contracts --hunks H1,H2", report.issues[0].message)
        self.assertIn("--lens design --files a.py,b.py", report.issues[1].message)

        ledger, _ = add_multi_coverage(
            ledger, ("intent", "contracts"), ["H1", "H2"], _long_note_for(["H1", "H2"])
        )
        ledger, _ = add_coverage(ledger, "design", ["a.py", "b.py"], _long_note_for(["a.py", "b.py"]))

        after = evaluate(ledger, snap, STANDARD, review_only=True)
        self.assertEqual([issue.code for issue in after.issues], ["uncovered"])
        self.assertIn("--lens sweep --hunks H1,H2", after.issues[0].message)


class QuickFileCoverageTests(QuickCoverRepoTestCase):
    def test_six_hunks_in_two_files_complete_review_only_in_one_cover_call(self) -> None:
        self.repo.write("a.py", _spaced_three_hunk_file("a", values=(1, 2, 3)))
        self.repo.write("b.py", _spaced_three_hunk_file("b", values=(10, 20, 30)))
        self.repo.commit("seed two files")
        self.repo.write("a.py", _spaced_three_hunk_file("a", values=(101, 102, 103)))
        self.repo.write("b.py", _spaced_three_hunk_file("b", values=(210, 220, 230)))

        self.review("prepare")
        shown = self.repo.show()
        self.assertEqual(shown["round"]["tier"], "quick")
        self.assertEqual(shown["round"]["hunks"], 6)

        blocked = self.review("check", "--review-only", check=False)
        self.assertIn("--lens intent,contracts,failure --files a.py,b.py", blocked.stdout)

        self.review(
            "add",
            "--lens",
            "contracts",
            "--file",
            "a.py",
            "--line",
            "14",
            "--severity",
            "minor",
            "--title",
            "Unbounded integer offset",
            "--scenario",
            "a_fn_1(x) adds 101 without checking domain bounds when negative values arrive",
        )
        self.review(
            "verdict",
            "F1",
            "--as",
            "dismissed",
            "--evidence",
            "guarded by callers that pass validated non-negative counts from a.py:13",
        )
        covered = self.review(
            "cover",
            "--lens",
            "intent,contracts,failure",
            "--files",
            "a.py,b.py",
            "--note",
            _long_note_for(["a.py", "b.py"]),
        )
        self.assertIn("0 lens item(s) left", covered.stdout)

        checked = self.review("check", "--review-only")
        self.assertEqual(checked.returncode, 0)
        self.assertIn("**Craftsman review: COMPLETE (review-only)**", checked.stdout)

    def test_from_json_allows_files_only_in_quick_rounds(self) -> None:
        self.repo.write("a.py", "def f(x: int) -> int:\n    return x + 1\n")
        self.repo.write("b.py", "def g(x: int) -> int:\n    return x + 2\n")
        self.review("prepare")

        entries = [
            {
                "lenses": ["intent", "contracts", "failure"],
                "files": ["a.py", "b.py"],
                "note": _long_note_for(["a.py", "b.py"]),
            }
        ]
        covered = self.review("cover", "--from-json", "-", stdin=json.dumps(entries))
        self.assertIn("`intent`, `contracts`, `failure` covered a.py, b.py", covered.stdout)
        self.assertEqual(self.review("check", "--review-only").returncode, 0)

        # In standard tier, --files is still rejected for hunk lenses.
        self.review("reset")
        self.review("prepare", "--tier", "standard")
        refused = self.review(
            "cover",
            "--lens",
            "intent",
            "--files",
            "a.py",
            "--note",
            _long_note_for(["a.py"]),
            check=False,
        )
        self.assert_error(refused, 1, "--files is only used with `--lens design`")

    def test_file_coverage_note_disambiguates_shared_basenames(self) -> None:
        snap_single = make_snap(make_hunk("H1", "src/a.py"))
        ledger_single, _ = prepared(
            snap_single, lenses=("intent", "contracts", "failure"), tier="quick"
        )

        # Missing the file name fails.
        with self.assertRaisesRegex(LedgerError, "missing: src/a.py"):
            add_coverage(
                ledger_single,
                "intent",
                ["src/a.py"],
                "checked f(0) -> returns 1 at L2 without raising errors",
                target_kind="files",
            )

        # A different directory prefix with the same basename does not match `src/a.py`.
        with self.assertRaisesRegex(LedgerError, "missing: src/a.py"):
            add_coverage(
                ledger_single,
                "intent",
                ["src/a.py"],
                "other/a.py:2 checked f(0) -> returns 1 without raising errors",
                target_kind="files",
            )

        # Both full path `src/a.py` and unique basename `a.py` cover `src/a.py`.
        add_coverage(
            ledger_single,
            "intent",
            ["src/a.py"],
            "src/a.py:2 checked f(0) -> returns 1 without raising errors",
            target_kind="files",
        )
        add_coverage(
            ledger_single,
            "contracts",
            ["src/a.py"],
            "a.py:2 checked f(0) -> returns 1 without raising errors",
            target_kind="files",
        )

        # Duplicate basenames require full paths.
        snap_dup = make_snap(make_hunk("H1", "pkg1/utils.py"), make_hunk("H2", "pkg2/utils.py"))
        ledger_dup, _ = prepared(snap_dup, lenses=("intent", "contracts", "failure"), tier="quick")
        with self.assertRaisesRegex(LedgerError, "missing: pkg1/utils.py, pkg2/utils.py"):
            add_coverage(
                ledger_dup,
                "intent",
                ["pkg1/utils.py", "pkg2/utils.py"],
                "utils.py:2 checked f(0) -> returns 1 without raising errors",
                target_kind="files",
            )

        updated_dup, _ = add_coverage(
            ledger_dup,
            "intent",
            ["pkg1/utils.py", "pkg2/utils.py"],
            _long_note_for(["pkg1/utils.py", "pkg2/utils.py"]),
            target_kind="files",
        )
        self.assertEqual(uncovered_by_lens(updated_dup, updated_dup.rounds[-1])["intent"], [])

    def test_changing_one_hunk_in_file_requires_re_covering_file_in_next_round(self) -> None:
        h1 = make_hunk("H1", "a.py", ("+x = 1",), new_start=1)
        h2 = make_hunk("H2", "a.py", ("+y = 2",), new_start=20)
        lenses = ("intent", "contracts", "failure")
        ledger, _ = prepared(make_snap(h1, h2, fingerprint="fp1"), lenses=lenses, tier="quick")
        ledger, _ = add_multi_coverage(
            ledger, lenses, ["a.py"], _long_note_for(["a.py"]), target_kind="files"
        )

        h1_edited = make_hunk("H1", "a.py", ("+x = 99",), new_start=1)
        ledger, round2 = prepared(
            make_snap(h1_edited, h2, fingerprint="fp2"), lenses=lenses, tier="quick", ledger=ledger
        )

        self.assertEqual([h.carried for h in round2.hunks], [False, False])
        self.assertEqual(round2.open_files, ("a.py",))
        self.assertEqual(uncovered_by_lens(ledger, round2)["intent"], ["H1", "H2"])

    def test_unchanged_file_coverage_carries_over_until_reopened(self) -> None:
        a1 = make_hunk("H1", "a.py", (" res = helper(1)", "+x = res"), new_start=1)
        a2 = make_hunk("H2", "a.py", ("+y = 2",), new_start=20)
        b1 = make_hunk("H3", "b.py", ("+def helper(v):", "+    return v + 1"), new_start=1)
        lenses = ("intent", "contracts", "failure")
        ledger, _ = prepared(make_snap(a1, a2, b1, fingerprint="fp1"), lenses=lenses, tier="quick")
        ledger, _ = add_multi_coverage(
            ledger, lenses, ["a.py", "b.py"], _long_note_for(["a.py", "b.py"]), target_kind="files"
        )

        # Only b.py changes (no symbol change affecting a.py): a.py hunks carry over.
        b1_changed = make_hunk("H3", "b.py", ("+def other(v):", "+    return v + 2"), new_start=1)
        ledger_carried, round2 = prepared(
            make_snap(a1, a2, b1_changed, fingerprint="fp2"),
            lenses=lenses,
            tier="quick",
            ledger=ledger,
        )
        self.assertEqual([h.carried for h in round2.hunks], [True, True, False])
        self.assertEqual(uncovered_by_lens(ledger_carried, round2)["intent"], ["H3"])

        # When b.py changes `helper` (defined in H3), H1 in a.py is reopened even though covered via --files.
        ledger_reopened, round2_reopened = prepared(
            make_snap(a1, a2, b1_changed, fingerprint="fp2"),
            lenses=lenses,
            tier="quick",
            ledger=ledger,
            symbols={"H3": ["helper"]},
        )
        self.assertTrue(round2_reopened.hunks[0].reopened)
        self.assertFalse(round2_reopened.hunks[0].carried)
        self.assertEqual(uncovered_by_lens(ledger_reopened, round2_reopened)["intent"], ["H1", "H3"])

    def test_ledger_roundtrip_allows_file_digests_only_in_quick(self) -> None:
        snap = make_snap(make_hunk("H1", "a.py"))
        ledger, _ = prepared(snap, lenses=("intent", "contracts", "failure"), tier="quick")
        ledger, _ = add_coverage(
            ledger, "contracts", ["a.py"], _long_note_for(["a.py"]), target_kind="files"
        )

        raw = ledger.to_json()
        reloaded = Ledger.from_json(raw)
        self.assertEqual(reloaded, ledger)

        with self.assertRaisesRegex(FieldError, r"coverage\[0\]\.digests"):
            Ledger(rounds=(replace(ledger.rounds[0], tier="standard"),), coverage=ledger.coverage)

        tampered = json.loads(json.dumps(raw))
        tampered["rounds"][0]["tier"] = "standard"
        with self.assertRaisesRegex(LedgerError, r"coverage\[0\]\.digests"):
            Ledger.from_json(tampered)


if __name__ == "__main__":
    unittest.main()
