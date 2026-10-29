#!/usr/bin/env python3
"""Tests for the review lens catalog and the lens selection policy."""

from __future__ import annotations

import unittest

from craftsman.diffscan import SIGNALS
from craftsman.lenses import (
    LENS_IDS,
    LENSES,
    REVIEWER_GROUPS,
    TIERS,
    Tier,
    get_lens,
    select_lenses,
    strictest_tier,
)


def picked_ids(tier: Tier, signals: dict[str, tuple[str, ...]], *, docs_only: bool = False) -> list[str]:
    return [pick.lens.id for pick in select_lenses(tier, signals, docs_only=docs_only)]


class LensCatalogTests(unittest.TestCase):
    def test_lens_ids_are_unique(self) -> None:
        self.assertEqual(len(LENS_IDS), len(LENSES))

    def test_lenses_trigger_only_on_known_signals(self) -> None:
        for lens in LENSES:
            with self.subTest(lens=lens.id):
                self.assertLessEqual(lens.triggers, set(SIGNALS))

    def test_get_lens_lists_valid_ids_for_unknown_lens(self) -> None:
        self.assertEqual(get_lens("trust").title, "Trust boundaries & security")
        with self.assertRaisesRegex(KeyError, "valid lenses: intent, contracts"):
            get_lens("security")


class LensSelectionTests(unittest.TestCase):
    def test_quick_tier_without_signals_selects_the_core_lenses(self) -> None:
        self.assertEqual(picked_ids("quick", {}), ["intent", "contracts", "failure"])

    def test_standard_tier_adds_its_always_on_lenses(self) -> None:
        self.assertEqual(
            picked_ids("standard", {}),
            ["intent", "contracts", "failure", "ripple", "tests", "craft", "design", "sweep"],
        )

    def test_deep_tier_selects_every_lens(self) -> None:
        self.assertEqual(picked_ids("deep", {}), [lens.id for lens in LENSES])

    def test_signals_add_triggered_lenses_with_a_reason(self) -> None:
        signals = {"process": ("H2", "H5"), "datamodel": ("H1",)}

        picks = {
            pick.lens.id: pick.reason for pick in select_lenses("standard", signals, docs_only=False)
        }

        self.assertEqual(
            list(picks),
            [
                "intent",
                "contracts",
                "failure",
                "ripple",
                "trust",
                "tests",
                "model",
                "craft",
                "design",
                "sweep",
            ],
        )
        self.assertEqual(picks["trust"], "signals: process (H2, H5)")
        self.assertEqual(picks["model"], "signals: datamodel (H1)")
        self.assertEqual(picks["intent"], "always in standard")

    def test_always_selected_lenses_still_name_their_signals(self) -> None:
        picks = {
            pick.lens.id: pick.reason
            for pick in select_lenses("deep", {"sql": ("H1", "H3")}, docs_only=False)
        }

        self.assertEqual(picks["trust"], "always in deep; signals: sql (H1, H3)")
        self.assertEqual(picks["failure"], "always in deep; signals: sql (H1, H3)")
        self.assertEqual(picks["intent"], "always in deep")

    def test_strictest_tier(self) -> None:
        self.assertEqual(strictest_tier("quick"), "quick")
        self.assertEqual(strictest_tier("standard", "deep", "quick"), "deep")

    def test_reason_caps_hunk_ids(self) -> None:
        ids = tuple(f"H{index}" for index in range(1, 9))

        (pick,) = [
            p for p in select_lenses("standard", {"time": ids}, docs_only=False) if p.lens.id == "state"
        ]

        self.assertEqual(pick.reason, "signals: time (H1, H2, H3, H4, H5, H6, …)")

    def test_empty_signal_lists_do_not_trigger(self) -> None:
        self.assertNotIn("trust", picked_ids("quick", {"sql": ()}))

    def test_docs_only_change_selects_the_prose_lenses(self) -> None:
        for tier in TIERS:
            with self.subTest(tier=tier):
                self.assertEqual(
                    picked_ids(tier, {"auth": ("H1",)}, docs_only=True), ["intent", "craft"]
                )

    def test_unknown_tier_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "valid tiers: quick, standard, deep"):
            select_lenses("auto", {}, docs_only=False)  # type: ignore[arg-type]

    def test_every_review_lens_belongs_to_exactly_one_reviewer_group(self) -> None:
        all_grouped = [lens for group in REVIEWER_GROUPS.values() for lens in group]

        self.assertEqual(sorted(all_grouped), sorted(LENS_IDS - {"sweep"}))


if __name__ == "__main__":
    unittest.main()
