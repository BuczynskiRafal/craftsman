#!/usr/bin/env python3
"""Tests for the review lens catalog and the lens suggestion policy."""

from __future__ import annotations

import unittest

from craftsman.diffscan import SIGNALS
from craftsman.lenses import LENS_IDS, LENSES, deep_hint, get_lens, select_lenses

CORE = ["intent", "contracts", "failure", "ripple", "tests"]
SIMPLICITY = ["craft", "design", "sweep"]


def picked_ids(
    signals: dict[str, tuple[str, ...]], *, deep: bool = False, docs_only: bool = False
) -> list[str]:
    return [pick.lens.id for pick in select_lenses(signals, deep=deep, docs_only=docs_only)]


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

    def test_filesystem_and_process_keep_their_wider_triggers(self) -> None:
        self.assertLessEqual({"filesystem"}, get_lens("state").triggers)
        self.assertLessEqual({"filesystem"}, get_lens("trust").triggers)
        self.assertLessEqual({"filesystem", "process"}, get_lens("platform").triggers)


class LensSuggestionTests(unittest.TestCase):
    def test_every_change_gets_the_core_and_simplicity_lenses(self) -> None:
        self.assertEqual(picked_ids({}), [*CORE[:4], "tests", *SIMPLICITY])

    def test_signals_add_triggered_lenses_with_a_reason(self) -> None:
        signals = {"process": ("H2", "H5"), "datamodel": ("H1",)}

        picks = {pick.lens.id: pick.reason for pick in select_lenses(signals, deep=False, docs_only=False)}

        self.assertEqual(
            list(picks),
            ["intent", "contracts", "failure", "ripple", "trust", "tests", "platform", "model", *SIMPLICITY],
        )
        self.assertEqual(picks["trust"], "signals: process (H2, H5)")
        self.assertEqual(picks["platform"], "signals: process (H2, H5)")
        self.assertEqual(picks["model"], "signals: datamodel (H1)")
        self.assertEqual(picks["failure"], "core lens; signals: process (H2, H5)")

    def test_deep_round_suggests_every_lens(self) -> None:
        picks = {pick.lens.id: pick.reason for pick in select_lenses({}, deep=True, docs_only=False)}

        self.assertEqual(list(picks), [lens.id for lens in LENSES])
        self.assertEqual(picks["trust"], "deep round")
        self.assertEqual(picks["intent"], "core lens")

    def test_reason_caps_hunk_ids(self) -> None:
        ids = tuple(f"H{index}" for index in range(1, 9))

        (pick,) = [p for p in select_lenses({"time": ids}, deep=False, docs_only=False) if p.lens.id == "state"]

        self.assertEqual(pick.reason, "signals: time (H1, H2, H3, H4, H5, H6, …)")

    def test_empty_signal_lists_do_not_trigger(self) -> None:
        self.assertNotIn("trust", picked_ids({"sql": ()}))

    def test_docs_only_change_selects_the_prose_lenses(self) -> None:
        self.assertEqual(picked_ids({"auth": ("H1",)}, docs_only=True), ["intent", "craft"])

    def test_a_deep_round_suggests_every_lens_even_for_a_docs_only_change(self) -> None:
        self.assertEqual(
            picked_ids({}, deep=True, docs_only=True), [lens.id for lens in LENSES]
        )

    def test_tests_signal_does_not_repeat_the_core_reason(self) -> None:
        picks = {p.lens.id: p.reason for p in select_lenses({"tests": ("H1",)}, deep=False, docs_only=False)}

        self.assertEqual(picks["tests"], "core lens")


class DeepHintTests(unittest.TestCase):
    def test_security_and_concurrency_signals_suggest_depth(self) -> None:
        hint = deep_hint({"auth": ("H3",), "concurrency": ("H7", "H8"), "errors": ("H1",)})

        self.assertEqual(hint, "Depth: deep suggested (signals: auth H3, concurrency H7,H8)")

    def test_other_signals_do_not(self) -> None:
        self.assertIsNone(deep_hint({"errors": ("H1",), "sql": ()}))


if __name__ == "__main__":
    unittest.main()
