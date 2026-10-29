#!/usr/bin/env python3
"""Tests for pruning stale review packets and older archives in `.git/craftsman`."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from craftsman import evidence, hooks, review
from craftsman.evidence import (
    KEEP_ARCHIVES_ENV,
    archive_ledger,
    packet_path,
    prune_round_files,
)
from craftsman.ledger import ledger_path_for, save
from craftsman.records import LEDGER_FILENAME, Ledger, LedgerError

from tests.support import EvidenceRepo, make_hunk, make_snap, prepared

STAMP = "20260101T000000.000Z"


def reviewed_ledger(base: str = "b" * 40) -> Ledger:
    ledger, _ = prepared(make_snap(make_hunk("H1"), base=base))
    return ledger


class RoundFilePruningTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.craftsman_dir = Path(tmp.name) / "craftsman"
        self.craftsman_dir.mkdir()

    def touch(self, *names: str) -> None:
        for name in names:
            target = self.craftsman_dir / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(name, encoding="utf-8")

    def run_prune(self, round_number: int) -> tuple[tuple[str, ...], str]:
        err = io.StringIO()
        with redirect_stderr(err):
            removed = prune_round_files(self.craftsman_dir, round_number)
        return tuple(path.name for path in removed), err.getvalue()

    def remaining(self) -> list[str]:
        return sorted(path.name for path in self.craftsman_dir.iterdir())

    def test_round_one_keeps_current_packet_views(self) -> None:
        self.touch("packet-1.md", "packet-1.intent.md", "packet-1.R1.H1-H2.md")

        removed, err = self.run_prune(1)

        self.assertEqual(removed, ())
        self.assertEqual(err, "")
        self.assertEqual(
            self.remaining(),
            ["packet-1.R1.H1-H2.md", "packet-1.intent.md", "packet-1.md"],
        )

    def test_round_two_prunes_prior_views_keeping_prior_packet(self) -> None:
        self.touch(
            "packet-1.md",
            "packet-1.intent.md",
            "packet-1.R1.H1-H4.md",
            "packet-2.md",
            "packet-2.intent.md",
        )

        removed, err = self.run_prune(2)

        self.assertEqual(removed, ("packet-1.R1.H1-H4.md", "packet-1.intent.md"))
        self.assertEqual(
            err.splitlines(),
            ["Pruned: packet-1.R1.H1-H4.md", "Pruned: packet-1.intent.md"],
        )
        self.assertEqual(self.remaining(), ["packet-1.md", "packet-2.intent.md", "packet-2.md"])

    def test_round_three_prunes_packets_older_than_previous_round(self) -> None:
        self.touch(
            "packet-1.md",
            "packet-1.intent.md",
            "packet-2.md",
            "packet-2.craft.md",
            "packet-3.md",
            "packet-3.intent.md",
        )

        removed, err = self.run_prune(3)

        self.assertEqual(removed, ("packet-1.intent.md", "packet-1.md", "packet-2.craft.md"))
        self.assertEqual(
            err.splitlines(),
            [
                "Pruned: packet-1.intent.md",
                "Pruned: packet-1.md",
                "Pruned: packet-2.craft.md",
            ],
        )
        self.assertEqual(self.remaining(), ["packet-2.md", "packet-3.intent.md", "packet-3.md"])

    def test_unrelated_entries_stay_untouched(self) -> None:
        kept_files = (
            "notes.md",
            "craftsman_state.json",
            "ledger.json",
            "ledger.lock",
            " packet-1.md",
            "packet-foo.md",
            ".packet-1.md.tmp",
            "packet-3.md",
        )
        self.touch(*kept_files, "nested/packet-1.md", "packet-1.md")
        folder_like_packet = self.craftsman_dir / "packet-1.folder.md"
        folder_like_packet.mkdir()
        link_target = self.craftsman_dir / "notes.md"
        symlink_packet = self.craftsman_dir / "packet-1.symlink.md"
        symlink_packet.symlink_to(link_target)

        removed, _ = self.run_prune(3)

        self.assertEqual(removed, ("packet-1.md",))
        self.assertTrue((self.craftsman_dir / "nested" / "packet-1.md").is_file())
        self.assertTrue(folder_like_packet.is_dir())
        self.assertTrue(symlink_packet.is_symlink())
        for name in kept_files:
            self.assertTrue((self.craftsman_dir / name).is_file(), name)

    def test_missing_directory_returns_empty_tuple(self) -> None:
        missing = self.craftsman_dir / "absent"
        err = io.StringIO()
        with redirect_stderr(err):
            removed = prune_round_files(missing, 3)
        self.assertEqual(removed, ())
        self.assertEqual(err.getvalue(), "")

    def test_invalid_round_number_is_rejected(self) -> None:
        for invalid in (0, -1, True, False):
            with self.subTest(invalid=invalid), self.assertRaises(LedgerError):
                prune_round_files(self.craftsman_dir, invalid)

    def test_symlinked_craftsman_directory_is_rejected(self) -> None:
        alias = self.craftsman_dir.parent / "craftsman-link"
        alias.symlink_to(self.craftsman_dir, target_is_directory=True)
        with self.assertRaises(LedgerError):
            prune_round_files(alias, 2)


class ArchivePruningTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.path = self.tmp / "craftsman" / LEDGER_FILENAME
        env_without_keep = {k: v for k, v in os.environ.items() if k != KEEP_ARCHIVES_ENV}
        patcher = mock.patch.dict(os.environ, env_without_keep, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def record_review(self, base: str = "b" * 40) -> None:
        save(self.path, reviewed_ledger(base))
        packet_path(self.path, 1).write_text("packet", encoding="utf-8")

    def archive_series(self, stamps: list[str]) -> str:
        err = io.StringIO()
        with (
            mock.patch.object(evidence, "_archive_stamp", side_effect=stamps),
            redirect_stderr(err),
        ):
            for _ in stamps:
                self.record_review()
                archive_ledger(self.path)
        return err.getvalue()

    def entries(self) -> list[str]:
        return sorted(path.name for path in self.path.parent.iterdir())

    def test_default_retention_keeps_five_newest_archives(self) -> None:
        stamps = [f"20260101T00000{index}.000Z" for index in range(1, 8)]

        err = self.archive_series(stamps)

        kept_stamps = stamps[-5:]
        expected = sorted(
            ["ledger.lock"]
            + [f"archive-{stamp}" for stamp in kept_stamps]
            + [f"ledger-{stamp}.json" for stamp in kept_stamps]
        )
        self.assertEqual(self.entries(), expected)
        for stamp in kept_stamps:
            self.assertTrue((self.path.parent / f"archive-{stamp}" / "packet-1.md").is_file())
        self.assertEqual(
            err.splitlines(),
            [
                f"Pruned: archive-{stamps[0]}",
                f"Pruned: ledger-{stamps[0]}.json",
                f"Pruned: archive-{stamps[1]}",
                f"Pruned: ledger-{stamps[1]}.json",
            ],
        )

    def test_same_millisecond_suffixes_prune_in_creation_order(self) -> None:
        os.environ[KEEP_ARCHIVES_ENV] = "3"

        self.archive_series([STAMP] * 11)

        self.assertEqual(
            self.entries(),
            sorted(
                [
                    "ledger.lock",
                    f"archive-{STAMP}-9",
                    f"archive-{STAMP}-10",
                    f"archive-{STAMP}-11",
                    f"ledger-{STAMP}-9.json",
                    f"ledger-{STAMP}-10.json",
                    f"ledger-{STAMP}-11.json",
                ]
            ),
        )

    def test_non_matching_archive_entries_stay_untouched(self) -> None:
        os.environ[KEEP_ARCHIVES_ENV] = "1"
        folder = self.path.parent
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "archive-foo").mkdir()
        (folder / "archive-20260101T000000Z").mkdir()
        (folder / f"archive-{STAMP}").write_text("file not dir", encoding="utf-8")
        (folder / "ledger-backup.json").write_text("{}", encoding="utf-8")
        (folder / f"ledger-{STAMP}.json").mkdir()
        (folder / "notes.md").write_text("notes", encoding="utf-8")
        (folder / "craftsman_state.json").write_text("{}", encoding="utf-8")

        self.archive_series(["20260102T000001.000Z", "20260102T000002.000Z"])

        for untouched in (
            "archive-foo",
            "archive-20260101T000000Z",
            f"archive-{STAMP}",
            "ledger-backup.json",
            f"ledger-{STAMP}.json",
            "notes.md",
            "craftsman_state.json",
            "archive-20260102T000002.000Z",
            "ledger-20260102T000002.000Z.json",
        ):
            self.assertTrue((folder / untouched).exists(), untouched)
        self.assertFalse((folder / "archive-20260102T000001.000Z").exists())
        self.assertFalse((folder / "ledger-20260102T000001.000Z.json").exists())

    def test_keep_archives_env_var_sets_retention_limit(self) -> None:
        os.environ[KEEP_ARCHIVES_ENV] = "2"
        stamps = [f"20260101T00000{index}.000Z" for index in range(1, 5)]

        err = self.archive_series(stamps)

        self.assertEqual(
            self.entries(),
            [
                f"archive-{stamps[2]}",
                f"archive-{stamps[3]}",
                f"ledger-{stamps[2]}.json",
                f"ledger-{stamps[3]}.json",
                "ledger.lock",
            ],
        )
        self.assertIn(f"Pruned: archive-{stamps[0]}", err.splitlines())
        self.assertIn(f"Pruned: ledger-{stamps[1]}.json", err.splitlines())

    def test_invalid_keep_archives_env_var_leaves_active_ledger_intact(self) -> None:
        self.record_review()
        for invalid in ("0", "-1", "abc", "true", "", "   ", "2.5"):
            with self.subTest(invalid=invalid):
                os.environ[KEEP_ARCHIVES_ENV] = invalid
                with self.assertRaisesRegex(LedgerError, KEEP_ARCHIVES_ENV):
                    archive_ledger(self.path)
                self.assertTrue(self.path.is_file())
                self.assertTrue(packet_path(self.path, 1).is_file())

    def test_unexpected_nested_folder_in_old_archive_is_rejected(self) -> None:
        os.environ[KEEP_ARCHIVES_ENV] = "1"
        self.archive_series(["20260101T000001.000Z"])
        nested = self.path.parent / "archive-20260101T000001.000Z" / "unexpected"
        nested.mkdir()
        self.record_review()
        with (
            mock.patch.object(evidence, "_archive_stamp", return_value="20260101T000002.000Z"),
            self.assertRaisesRegex(LedgerError, "unexpected subdirectory"),
        ):
            archive_ledger(self.path)
        self.assertTrue(nested.is_dir())

    def test_hook_archiving_emits_pruned_on_stderr_preserving_json_stdout(self) -> None:
        os.environ[KEEP_ARCHIVES_ENV] = "1"
        repo = EvidenceRepo(self.tmp / "repo")
        repo_ledger = ledger_path_for(repo.root)
        save(repo_ledger, reviewed_ledger("a" * 40))
        with redirect_stderr(io.StringIO()):
            archive_ledger(repo_ledger)
        save(repo_ledger, reviewed_ledger("c" * 40))
        payload = json.dumps(
            {
                "session_id": "s1",
                "transcript_path": str(repo.root / "missing.jsonl"),
                "cwd": str(repo.root),
                "hook_event_name": "UserPromptSubmit",
                "prompt": "/craftsman tighten pruning",
            }
        )
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(payload)), redirect_stdout(out), redirect_stderr(err):
            code = hooks.main(["prompt", "--platform", "claude"])

        self.assertEqual(code, 0, err.getvalue())
        parsed = json.loads(out.getvalue())
        self.assertIsInstance(parsed, dict)
        self.assertIn("Pruned: ledger-", err.getvalue())

    def test_prepare_prunes_stale_packets_only_when_new_round_opens(self) -> None:
        repo = EvidenceRepo(self.tmp / "prepare-prune")
        craftsman_dir = ledger_path_for(repo.root).parent

        repo.write("app.py", "def compute(x: int) -> int:\n    return x + 1\n")
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(
                review.main(["prepare", "--cwd", str(repo.root), "--tier", "standard"]), 0
            )
            self.assertEqual(
                review.main(["view", "--cwd", str(repo.root), "--lenses", "intent"]), 0
            )
        self.assertTrue((craftsman_dir / "packet-1.md").is_file())
        self.assertTrue((craftsman_dir / "packet-1.intent.md").is_file())

        # Re-preparing an unchanged diff does not prune round 1's view.
        err_unchanged = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err_unchanged):
            self.assertEqual(
                review.main(["prepare", "--cwd", str(repo.root), "--tier", "standard"]), 0
            )
        self.assertTrue((craftsman_dir / "packet-1.intent.md").is_file())
        self.assertEqual(err_unchanged.getvalue(), "")

        # Round 2 prunes round 1 views while keeping packet-1.md and packet-2.md.
        repo.write("app.py", "def compute(x: int) -> int:\n    return x + 2\n")
        err_r2 = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err_r2):
            self.assertEqual(
                review.main(["prepare", "--cwd", str(repo.root), "--tier", "standard"]), 0
            )
            self.assertEqual(
                review.main(["view", "--cwd", str(repo.root), "--lenses", "intent"]), 0
            )
        self.assertFalse((craftsman_dir / "packet-1.intent.md").exists())
        self.assertTrue((craftsman_dir / "packet-1.md").is_file())
        self.assertTrue((craftsman_dir / "packet-2.md").is_file())
        self.assertTrue((craftsman_dir / "packet-2.intent.md").is_file())
        self.assertIn("Pruned: packet-1.intent.md", err_r2.getvalue())

        # Round 3 prunes packet-1.md and round 2 views while keeping packet-2.md and packet-3.md.
        repo.write("app.py", "def compute(x: int) -> int:\n    return x + 3\n")
        err_r3 = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err_r3):
            self.assertEqual(
                review.main(["prepare", "--cwd", str(repo.root), "--tier", "standard"]), 0
            )
        self.assertFalse((craftsman_dir / "packet-1.md").exists())
        self.assertFalse((craftsman_dir / "packet-2.intent.md").exists())
        self.assertTrue((craftsman_dir / "packet-2.md").is_file())
        self.assertTrue((craftsman_dir / "packet-3.md").is_file())
        self.assertIn("Pruned: packet-1.md", err_r3.getvalue())
        self.assertIn("Pruned: packet-2.intent.md", err_r3.getvalue())


if __name__ == "__main__":
    unittest.main()
