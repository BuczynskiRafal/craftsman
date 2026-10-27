#!/usr/bin/env python3
"""Tests for the review lens catalog and the lens selection policy."""

from __future__ import annotations

import unittest

from craftsman.diffscan import SIGNALS
from craftsman.lenses import (
    LENS_IDS,
    LENSES,
    TIERS,
    Tier,
    get_lens,
    select_lenses,
    strictest_tier,
)


def picked_ids(tier: Tier, signals: dict[str, tuple[str, ...]], *, docs_only: bool = False) -> list[str]:
    return [pick.lens.id for pick in select_lenses(tier, signals, docs_only=docs_only)]


class LensCatalogTests(unittest.TestCase):
    def test_catalog_ids_are_unique_and_triggers_are_known_signals(self) -> None:
        self.assertEqual(len(LENS_IDS), len(LENSES))
        self.assertEqual(
            [lens.id for lens in LENSES],
            [
                "intent",
                "contracts",
                "failure",
                "ripple",
                "state",
                "trust",
                "tests",
                "platform",
                "model",
                "craft",
                "sweep",
            ],
        )
        for lens in LENSES:
            with self.subTest(lens=lens.id):
                self.assertTrue(lens.triggers <= set(SIGNALS))
                self.assertIn("deep", lens.always_in)
                self.assertTrue(lens.focus.endswith("?"))

    def test_get_lens_lists_valid_ids_for_unknown_lens(self) -> None:
        self.assertEqual(get_lens("trust").title, "Trust boundaries & security")
        with self.assertRaisesRegex(KeyError, "valid lenses: intent, contracts"):
            get_lens("security")


class LensSelectionTests(unittest.TestCase):
    def test_quick_tier_without_signals_selects_the_core_lenses(self) -> None:
        self.assertEqual(picked_ids("quick", {}), ["intent", "contracts", "craft"])

    def test_standard_tier_adds_its_always_on_lenses(self) -> None:
        self.assertEqual(
            picked_ids("standard", {}),
            ["intent", "contracts", "failure", "ripple", "tests", "craft", "sweep"],
        )

    def test_deep_tier_selects_every_lens(self) -> None:
        self.assertEqual(picked_ids("deep", {}), [lens.id for lens in LENSES])

    def test_signals_add_triggered_lenses_with_a_reason(self) -> None:
        signals = {"process": ("H2", "H5"), "datamodel": ("H1",)}

        picks = {pick.lens.id: pick.reason for pick in select_lenses("quick", signals, docs_only=False)}

        self.assertEqual(
            list(picks), ["intent", "contracts", "failure", "trust", "platform", "model", "craft"]
        )
        self.assertEqual(picks["trust"], "signals: process (H2, H5)")
        self.assertEqual(picks["model"], "signals: datamodel (H1)")
        self.assertEqual(picks["intent"], "always in quick")

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

    def test_docs_only_change_selects_intent_and_craft_in_every_tier(self) -> None:
        for tier in TIERS:
            with self.subTest(tier=tier):
                self.assertEqual(
                    picked_ids(tier, {"auth": ("H1",)}, docs_only=True), ["intent", "craft"]
                )

    def test_unknown_tier_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "valid tiers: quick, standard, deep"):
            select_lenses("auto", {}, docs_only=False)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
