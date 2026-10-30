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

from craftsman import evidence, gate, hooks, review
from craftsman.evidence import (
    INBOX_DIRNAME,
    KEEP_ARCHIVES_ENV,
    archive_ledger,
    archive_ledger_if_rebased,
    consume_inbox_file,
    create_inbox_file,
    inbox_dir_for,
    packet_path,
    prune_inbox_files,
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


class InboxLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.craftsman_dir = self.tmp / "craftsman"

    def test_create_inbox_file_returns_unique_paths(self) -> None:
        paths = [
            create_inbox_file(self.craftsman_dir, "cover"),
            create_inbox_file(self.craftsman_dir, "cover"),
            create_inbox_file(self.craftsman_dir, "add"),
            create_inbox_file(self.craftsman_dir, "verdict"),
        ]

        self.assertEqual(len(set(paths)), 4)
        for path in paths:
            self.assertEqual(path.parent, inbox_dir_for(self.craftsman_dir))
            self.assertTrue(path.is_file())
            self.assertEqual(path.read_text(encoding="utf-8"), "")
            self.assertEqual(path.suffix, ".json")

    def test_create_inbox_file_rejects_invalid_kind_or_symlink_dir(self) -> None:
        with self.assertRaisesRegex(LedgerError, "unknown inbox kind"):
            create_inbox_file(self.craftsman_dir, "bogus")  # type: ignore[arg-type]

        real_outside = self.tmp / "outside"
        real_outside.mkdir()
        self.craftsman_dir.mkdir()
        (self.craftsman_dir / INBOX_DIRNAME).symlink_to(real_outside, target_is_directory=True)
        with self.assertRaises(LedgerError):
            create_inbox_file(self.craftsman_dir, "cover")

        alias_craftsman = self.tmp / "craftsman-link"
        alias_craftsman.symlink_to(real_outside, target_is_directory=True)
        with self.assertRaises(LedgerError):
            create_inbox_file(alias_craftsman, "cover")

    def test_consume_inbox_file_removes_file_cleaning_empty_inbox(self) -> None:
        first = create_inbox_file(self.craftsman_dir, "cover")
        second = create_inbox_file(self.craftsman_dir, "add")
        inbox_dir = inbox_dir_for(self.craftsman_dir)

        self.assertTrue(consume_inbox_file(self.craftsman_dir, str(first)))
        self.assertFalse(first.exists())
        self.assertTrue(second.is_file())
        self.assertTrue(inbox_dir.is_dir())

        self.assertTrue(consume_inbox_file(self.craftsman_dir, str(second.resolve())))
        self.assertFalse(second.exists())
        self.assertFalse(inbox_dir.exists())

    def test_consume_inbox_file_ignores_stdin_or_outside_paths(self) -> None:
        outside = self.tmp / "cover.json"
        outside.write_text("[]", encoding="utf-8")
        create_inbox_file(self.craftsman_dir, "cover")

        self.assertFalse(consume_inbox_file(self.craftsman_dir, "-"))
        self.assertFalse(consume_inbox_file(self.craftsman_dir, str(outside)))
        self.assertTrue(outside.is_file())

    def test_consume_inbox_file_refuses_symlink_inside_inbox(self) -> None:
        outside = self.tmp / "secret.json"
        outside.write_text("[]", encoding="utf-8")
        inbox_dir = inbox_dir_for(self.craftsman_dir)
        inbox_dir.mkdir(parents=True)
        link = inbox_dir / "cover-link.json"
        link.symlink_to(outside)

        self.assertFalse(consume_inbox_file(self.craftsman_dir, str(link)))
        self.assertTrue(link.is_symlink())
        self.assertTrue(outside.is_file())

    def test_consume_inbox_file_refuses_symlinked_parent_aliases(self) -> None:
        real_file = create_inbox_file(self.craftsman_dir, "cover")
        inbox_dir = inbox_dir_for(self.craftsman_dir)
        fake_inbox = self.tmp / "fake-inbox"
        fake_inbox.symlink_to(inbox_dir, target_is_directory=True)
        fake_craftsman = self.tmp / "fake-craftsman"
        fake_craftsman.symlink_to(self.craftsman_dir, target_is_directory=True)

        self.assertFalse(consume_inbox_file(self.craftsman_dir, str(fake_inbox / real_file.name)))
        self.assertFalse(
            consume_inbox_file(
                self.craftsman_dir, str(fake_craftsman / INBOX_DIRNAME / real_file.name)
            )
        )
        self.assertFalse(consume_inbox_file(fake_craftsman, str(real_file)))
        self.assertTrue(real_file.is_file())

    def test_consume_inbox_file_warns_on_unlink_oserror_without_raising(self) -> None:
        real_file = create_inbox_file(self.craftsman_dir, "cover")
        err = io.StringIO()
        with (
            mock.patch.object(Path, "unlink", side_effect=PermissionError("file in use")),
            redirect_stderr(err),
        ):
            consumed = consume_inbox_file(self.craftsman_dir, str(real_file))

        self.assertFalse(consumed)
        self.assertIn("craftsman review: warning: could not remove inbox file", err.getvalue())
        self.assertIn("file in use", err.getvalue())

        race_err = io.StringIO()
        with (
            mock.patch.object(Path, "unlink", side_effect=FileNotFoundError("gone")),
            redirect_stderr(race_err),
        ):
            self.assertFalse(consume_inbox_file(self.craftsman_dir, str(real_file)))
        self.assertEqual(race_err.getvalue(), "")

    def test_prune_inbox_files_reports_removed_count_on_stderr(self) -> None:
        inbox_dir = inbox_dir_for(self.craftsman_dir)
        inbox_dir.mkdir(parents=True)
        second = inbox_dir / "verdict-2.json"
        first = inbox_dir / "cover-1.json"
        second.write_text("[]", encoding="utf-8")
        first.write_text("[]", encoding="utf-8")

        err = io.StringIO()
        with redirect_stderr(err):
            pruned = prune_inbox_files(self.craftsman_dir)

        self.assertEqual([item.name for item in pruned], ["cover-1.json", "verdict-2.json"])
        self.assertEqual(
            err.getvalue(), "Pruned 2 inbox file(s): cover-1.json, verdict-2.json\n"
        )
        self.assertFalse(inbox_dir.exists())

    def test_prune_inbox_files_is_silent_when_empty(self) -> None:
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(prune_inbox_files(self.craftsman_dir), ())
        self.assertEqual(err.getvalue(), "")

        inbox_dir = inbox_dir_for(self.craftsman_dir)
        inbox_dir.mkdir(parents=True)
        with redirect_stderr(err):
            self.assertEqual(prune_inbox_files(self.craftsman_dir), ())
        self.assertEqual(err.getvalue(), "")
        self.assertFalse(inbox_dir.exists())

    def test_prune_inbox_files_skips_symlinks_or_subdirectories(self) -> None:
        outside = self.tmp / "outside.json"
        outside.write_text("[]", encoding="utf-8")
        inbox_dir = inbox_dir_for(self.craftsman_dir)
        inbox_dir.mkdir(parents=True)
        real_file = inbox_dir / "add-1.json"
        real_file.write_text("[]", encoding="utf-8")
        symlink = inbox_dir / "link.json"
        symlink.symlink_to(outside)
        subdir = inbox_dir / "nested"
        subdir.mkdir()

        err = io.StringIO()
        with redirect_stderr(err):
            pruned = prune_inbox_files(self.craftsman_dir)

        self.assertEqual(pruned, (real_file,))
        self.assertEqual(err.getvalue(), "Pruned 1 inbox file(s): add-1.json\n")
        self.assertTrue(symlink.is_symlink())
        self.assertTrue(outside.is_file())
        self.assertTrue(subdir.is_dir())

    def test_archive_ledger_prunes_inbox_files(self) -> None:
        ledger_path = self.craftsman_dir / LEDGER_FILENAME
        save(ledger_path, reviewed_ledger())
        stale = create_inbox_file(self.craftsman_dir, "cover")

        err = io.StringIO()
        with redirect_stderr(err):
            archive_ledger(ledger_path)

        self.assertFalse(stale.exists())
        self.assertFalse(inbox_dir_for(self.craftsman_dir).exists())
        self.assertIn(f"Pruned 1 inbox file(s): {stale.name}", err.getvalue())

    def test_archive_if_rebased_prunes_inbox_only_when_base_changed(self) -> None:
        repo = EvidenceRepo(self.tmp.resolve() / "rebase-repo")
        ledger_path = ledger_path_for(repo.root)
        save(ledger_path, reviewed_ledger("a" * 40))
        stale = create_inbox_file(ledger_path.parent, "add")

        same_err = io.StringIO()
        with redirect_stderr(same_err):
            self.assertEqual(archive_ledger_if_rebased(repo.root, "a" * 40), ())
        self.assertTrue(stale.is_file())
        self.assertEqual(same_err.getvalue(), "")

        rebased_err = io.StringIO()
        with redirect_stderr(rebased_err):
            archived = archive_ledger_if_rebased(repo.root, "b" * 40)
        self.assertEqual(len(archived), 1)
        self.assertFalse(stale.exists())
        self.assertIn(f"Pruned 1 inbox file(s): {stale.name}", rebased_err.getvalue())

    def test_gate_complete_prunes_inbox_only_on_success(self) -> None:
        repo = EvidenceRepo(self.tmp.resolve() / "gate-repo")
        repo.write_lines("app.py", 5)
        self.assertEqual(
            gate.run_cli(["--init", "--tier", "quick", "--cwd", str(repo.root)], {}), 0
        )
        stale = create_inbox_file(repo.ledger_dir, "cover")

        refused_out, refused_err = io.StringIO(), io.StringIO()
        with redirect_stdout(refused_out), redirect_stderr(refused_err):
            refused_code = gate.run_cli(["--complete", "--cwd", str(repo.root)], {})
        self.assertEqual(refused_code, 1)
        self.assertTrue(stale.is_file())

        repo.record_full_evidence("quick")
        stale_at_close = create_inbox_file(repo.ledger_dir, "cover")
        closed_out, closed_err = io.StringIO(), io.StringIO()
        with redirect_stdout(closed_out), redirect_stderr(closed_err):
            closed_code = gate.run_cli(["--complete", "--cwd", str(repo.root)], {})
        self.assertEqual(closed_code, 0, closed_err.getvalue())
        self.assertFalse(stale_at_close.exists())
        self.assertFalse(inbox_dir_for(repo.ledger_dir).exists())
        self.assertIn(f"Pruned 1 inbox file(s): {stale_at_close.name}", closed_err.getvalue())

    def test_consume_inbox_rejects_zero_inode_external_or_reparse_point(self) -> None:
        inbox_file = create_inbox_file(self.craftsman_dir, "cover")
        outside = self.tmp / "external.json"
        outside.write_text("[]", encoding="utf-8")
        real_lstat = os.lstat

        def zero_inode_lstat(target: str | os.PathLike[str]) -> os.stat_result:
            st = real_lstat(target)
            vals = list(st)
            vals[1] = 0  # st_ino
            vals[2] = 0  # st_dev
            return os.stat_result(vals)

        with mock.patch("craftsman.evidence.os.lstat", side_effect=zero_inode_lstat):
            self.assertFalse(consume_inbox_file(self.craftsman_dir, str(outside)))
            self.assertTrue(outside.is_file())
            self.assertTrue(consume_inbox_file(self.craftsman_dir, str(inbox_file)))
            self.assertFalse(inbox_file.exists())

        junction_item = create_inbox_file(self.craftsman_dir, "add")
        with mock.patch("craftsman.evidence._is_symlink_or_junction", return_value=True):
            self.assertFalse(consume_inbox_file(self.craftsman_dir, str(junction_item)))
            with self.assertRaisesRegex(LedgerError, "refusing to use symlinked directory"):
                create_inbox_file(self.craftsman_dir, "add")
        self.assertTrue(junction_item.is_file())


if __name__ == "__main__":
    unittest.main()
