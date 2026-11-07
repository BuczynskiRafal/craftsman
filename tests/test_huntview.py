"""Tests for the text the review commands print about the hunt plan."""

from __future__ import annotations

import re
import unittest
from dataclasses import replace

from craftsman import huntview
from craftsman.gatecheck import SlotStatus
from craftsman.records import Finding, Round
from craftsman.usage import _COVERAGE_TOKEN_RE, _FINDING_TOKEN_RE

from tests.support import make_hunk, make_slot, make_snap, prepared, record_finding


def round_of(*hunk_ids: str, task: str = "") -> Round:
    ledger, round_ = prepared(make_snap(*(make_hunk(hunk_id, f"{hunk_id.lower()}.py") for hunk_id in hunk_ids)))
    return replace(round_, task=task)


def finding(**overrides: object) -> Finding:
    ledger = record_finding(prepared(make_snap(make_hunk("H1", "app.py")))[0], **overrides)  # type: ignore[arg-type]
    return ledger.findings[0]


class HunkRangeTests(unittest.TestCase):
    def test_consecutive_numbers_become_ranges(self) -> None:
        self.assertEqual(huntview.hunk_ranges(["H1", "H2", "H3", "H5"]), "H1-H3,H5")

    def test_the_order_and_repeats_do_not_matter(self) -> None:
        self.assertEqual(huntview.hunk_ranges(["H10", "H2", "H3", "H2", "H9"]), "H2-H3,H9-H10")

    def test_a_single_hunk_is_not_a_range(self) -> None:
        self.assertEqual(huntview.hunk_ranges(["H7"]), "H7")

    def test_a_gap_is_never_bridged(self) -> None:
        self.assertEqual(huntview.hunk_ranges(["H1", "H3"]), "H1,H3")


class PlanLinesTests(unittest.TestCase):
    def test_one_aligned_line_per_slot_with_the_state_when_asked(self) -> None:
        statuses = [
            SlotStatus(make_slot("W1-1", hunks=("H1", "H2"), lenses=("intent", "ripple"), changed_lines=62), "done"),
            SlotStatus(
                make_slot("W1-2", hunks=("H1",), lenses=("craft", "design"), kind="simplicity", changed_lines=1),
                "unlaunched",
                ("H1",),
            ),
        ]

        plain = huntview.plan_lines(statuses, with_state=False)
        stated = huntview.plan_lines(statuses, with_state=True)

        self.assertRegex(plain[0], r"^  W1-1  H1-H2  intent, ripple +62 lines$")
        self.assertRegex(plain[1], r"^  W1-2  H1 +craft, design \(simplicity\)  1 line +  \[Claude Code: model haiku\]$")
        self.assertEqual(len(plain[0].split("62")[0]), len(plain[1].split("1 line")[0]))
        self.assertTrue(stated[0].endswith("[done]"))
        self.assertTrue(stated[1].endswith("[not launched] [Claude Code: model haiku]"))

    def test_wave_slots_say_what_they_are(self) -> None:
        fresh = make_slot("W2-X1", lenses=("intent",))
        variant = make_slot("W2-F3", lenses=("contracts",))

        self.assertEqual(huntview.lens_text(fresh), "intent (fresh look)")
        self.assertEqual(huntview.lens_text(variant), "contracts (variant of F3)")


class LaunchLinesTests(unittest.TestCase):
    def test_several_hunters_are_launched_all_in_one_step(self) -> None:
        text = "\n".join(huntview.launch_lines(["W1-1", "W1-2"]))

        self.assertIn("Launch all 2 hunters now, in one step.", text)
        self.assertIn("Run `craftsman review brief <ID>` and follow it exactly.", text)
        self.assertIn("end your turn while they run", text)

    def test_one_hunter_is_not_all(self) -> None:
        self.assertTrue(huntview.launch_lines(["W1-3"])[0].startswith("Launch 1 hunter now"))


class BriefTests(unittest.TestCase):
    def brief(self, slot_id: str = "W1-1", **overrides: object) -> str:
        slot = make_slot(slot_id, hunks=("H1", "H2", "H4"), lenses=("intent", "ripple"), changed_lines=30)
        options: dict[str, object] = {
            "slot": slot,
            "round_": round_of("H1", "H2", "H3", "H4", task="Add cart totals"),
            "hunters": 3,
            "trigger": None,
            "role_text": None,
            "antigravity": False,
        }
        options.update(overrides)
        return huntview.hunter_brief(**options)  # type: ignore[arg-type]

    def test_the_brief_names_the_slice_the_lenses_and_the_recording_commands(self) -> None:
        text = self.brief()

        self.assertIn("You are Craftsman hunter W1-1 (bug slot, generation 1): one of 3 hunters", text)
        self.assertIn("Task: Add cart totals", text)
        self.assertIn("craftsman review view --hunks H1-H2,H4", text)
        self.assertIn("craftsman skill show craft-review --lenses intent,ripple", text)
        self.assertIn("craftsman review add --hunter W1-1 --lens <one of your lenses>", text)
        self.assertIn('craftsman review cover --hunter W1-1 --hunks <ids you examined> --note "', text)
        self.assertIn('"hunter": "W1-1"', text)
        self.assertIn("Every hunk of your slot (H1-H2,H4) must be covered", text)
        self.assertIn("Never run `craftsman review verdict`, `merge`, `prepare` or `reset`", text)
        self.assertNotIn("send_message", text)
        self.assertNotIn("--- Role text ---", text)

    def test_a_missing_task_is_said_so(self) -> None:
        self.assertIn("Task: (no task text recorded)", self.brief(round_=round_of("H1")))

    def test_antigravity_hunters_report_to_their_parent_and_get_the_role_text(self) -> None:
        text = self.brief(antigravity=True, role_text="# Craftsman Hunter\nYou assume it has bugs.")

        self.assertIn("send that summary to your parent with send_message", text)
        self.assertTrue(text.rstrip().endswith("You assume it has bugs."))
        self.assertIn("--- Role text ---", text)

    def test_a_variant_brief_describes_the_defect_to_hunt(self) -> None:
        trigger = finding(severity="major", title="Empty list crashes total()", path="app.py", line=3)
        text = self.brief("W2-F1", slot=make_slot("W2-F1", hunks=("H1",), lenses=("contracts",)), trigger=trigger)

        self.assertIn("The defect: F1 (major, lens intent) at app.py:3 - Empty list crashes total()", text)
        self.assertIn("Hunt the same root cause across the diff and the code it touches", text)

    def test_a_fresh_brief_forbids_reading_other_findings(self) -> None:
        text = self.brief("W2-X1", slot=make_slot("W2-X1", hunks=("H1",), lenses=("intent",)))

        self.assertIn("You are a fresh, independent hunter. Do not read other findings", text)

    def test_the_brief_never_looks_like_a_recording(self) -> None:
        for text in (self.brief(), self.brief("W2-X1", slot=make_slot("W2-X1")), self.brief(antigravity=True)):
            self.assertIsNone(_COVERAGE_TOKEN_RE.search(text))
            self.assertIsNone(_FINDING_TOKEN_RE.search(text))
            self.assertIsNone(re.search(r"\b[BCFV]\d+\b", text.replace("F1", "")), text)


class VerifierBriefTests(unittest.TestCase):
    def test_the_verifier_gets_the_findings_text_and_the_verdict_commands(self) -> None:
        item = finding(title="Empty list crashes total()", scenario="total([]) divides by zero on an empty list")
        text = huntview.verifier_brief(
            findings=[item], round_=round_of("H1"), base_sha="abc123", task="Add cart totals", role_text=None
        )

        self.assertIn("independent Craftsman verifier for F1", text)
        self.assertIn("- F1 (major, lens intent) app.py:3 - Empty list crashes total()", text)
        self.assertIn("Scenario: total([]) divides by zero on an empty list", text)
        self.assertIn("craftsman review merge <TARGET> <DUPLICATES...>", text)
        self.assertIn("craftsman review verdict <ID> --as proven --run <R#>", text)
        self.assertIn("Base ref: abc123 (round 1)", text)
        self.assertIsNone(_FINDING_TOKEN_RE.search(text))

    def test_the_role_text_is_appended_when_given(self) -> None:
        text = huntview.verifier_brief(
            findings=[finding()], round_=None, base_sha="abc", task="", role_text="# Craftsman Verifier"
        )

        self.assertIn("Task: (no task text recorded)", text)
        self.assertTrue(text.rstrip().endswith("# Craftsman Verifier"))


if __name__ == "__main__":
    unittest.main()
