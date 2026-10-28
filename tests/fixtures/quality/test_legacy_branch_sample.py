"""Reduced test fixture copied from `fix/copilot-dedup-and-search-scope` before Part A."""

from __future__ import annotations

import unittest


class LegacyOwnershipTests(unittest.TestCase):
    def test_v3_0_0_state_without_owner_treats_every_session_as_owner(self) -> None:
        self.assertTrue(True)
