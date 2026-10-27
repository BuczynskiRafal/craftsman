#!/usr/bin/env python3
"""Tests for archiving the evidence ledger and its review packets."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from craftsman import evidence
from craftsman.evidence import archive_ledger, archive_ledger_if_rebased, packet_path
from craftsman.ledger import ledger_path_for, save
from craftsman.records import LEDGER_FILENAME, Ledger

from tests.support import EvidenceRepo, make_hunk, make_snap, prepared

STAMP = "20260101T000000.000Z"


def reviewed_ledger(base: str) -> Ledger:
    ledger, _ = prepared(make_snap(make_hunk("H1"), base=base))
    return ledger


class ArchiveTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "craftsman" / LEDGER_FILENAME

    def record_review(self) -> None:
        save(self.path, reviewed_ledger("b" * 40))
        packet_path(self.path, 1).write_text("packet", encoding="utf-8")

    def test_archives_made_in_the_same_millisecond_are_all_kept(self) -> None:
        with mock.patch.object(evidence, "_archive_stamp", return_value=STAMP):
            for _ in range(2):
                self.record_review()
                archive_ledger(self.path)

        self.assertEqual(
            sorted(path.name for path in self.path.parent.iterdir()),
            [
                f"archive-{STAMP}",
                f"archive-{STAMP}-2",
                f"ledger-{STAMP}-2.json",
                f"ledger-{STAMP}.json",
                "ledger.lock",
            ],
        )

    def test_archiving_retries_a_file_another_process_has_open(self) -> None:
        self.record_review()
        real_replace = os.replace
        calls: list[Any] = []

        def busy_once(source: Any, target: Any) -> None:
            calls.append(source)
            if len(calls) == 1:
                raise PermissionError("in use")
            real_replace(source, target)

        with mock.patch("craftsman.ledger.os.replace", side_effect=busy_once):
            archived = archive_ledger(self.path)

        self.assertEqual(len(archived), 2)
        self.assertFalse(self.path.exists())


class RebaseArchiveTests(unittest.TestCase):
    def test_only_a_ledger_from_another_base_is_archived(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = EvidenceRepo(Path(tmp).resolve() / "repo")
            path = ledger_path_for(repo.root)
            save(path, reviewed_ledger(repo.base_sha))

            self.assertEqual(archive_ledger_if_rebased(repo.root, repo.base_sha), ())
            (archived,) = archive_ledger_if_rebased(repo.root, "c" * 40)

            self.assertTrue(archived.name.startswith("ledger-"))
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
