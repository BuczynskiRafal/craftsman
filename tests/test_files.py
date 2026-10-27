"""Tests for the shared file operations: atomic writes, backups and guarded removal."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from craftsman.files import (
    BACKUP_DIR_NAME,
    UnsafePathError,
    copy_file,
    copy_once_backup,
    is_within,
    move_aside,
    remove_path,
    write_bytes_atomic,
)


class FilesTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()


class AtomicWriteTests(FilesTestCase):
    @unittest.skipIf(os.name == "nt", "POSIX permissions")
    def test_rewrite_keeps_the_file_permissions(self) -> None:
        path = self.tmp / "run.sh"
        path.write_text("old\n", encoding="utf-8")
        path.chmod(0o750)

        write_bytes_atomic(path, b"new\n")

        self.assertEqual((path.read_bytes(), path.stat().st_mode & 0o777), (b"new\n", 0o750))

    def test_symlinked_file_is_written_where_it_lives(self) -> None:
        real = self.tmp / "dotfiles" / "settings.json"
        real.parent.mkdir()
        real.write_text("{}", encoding="utf-8")
        link = self.tmp / "settings.json"
        link.symlink_to(real)

        write_bytes_atomic(link, b'{"a": 1}')

        self.assertTrue(link.is_symlink())
        self.assertEqual(real.read_bytes(), b'{"a": 1}')

    def test_copy_replaces_a_symlink_instead_of_writing_through(self) -> None:
        victim = self.tmp / "victim.md"
        victim.write_text("keep\n", encoding="utf-8")
        source = self.tmp / "source.md"
        source.write_text("copied\n", encoding="utf-8")
        target = self.tmp / "target.md"
        target.symlink_to(victim)

        copy_file(source, target)

        self.assertEqual(victim.read_text(encoding="utf-8"), "keep\n")
        self.assertEqual(target.read_text(encoding="utf-8"), "copied\n")


class BackupTests(FilesTestCase):
    def test_moved_aside_folder_lands_in_the_backup_folder(self) -> None:
        skill = self.tmp / "skills" / "craftsman"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("mine\n", encoding="utf-8")

        backup = move_aside(skill, dry_run=False)

        self.assertEqual(backup.parent, self.tmp / "skills" / BACKUP_DIR_NAME)
        self.assertEqual((backup / "SKILL.md").read_text(encoding="utf-8"), "mine\n")
        self.assertFalse(skill.exists())

    def test_dry_run_move_aside_leaves_the_path(self) -> None:
        path = self.tmp / "rule.md"
        path.write_text("mine\n", encoding="utf-8")

        move_aside(path, dry_run=True)

        self.assertTrue(path.is_file())
        self.assertFalse((self.tmp / BACKUP_DIR_NAME).exists())

    def test_copy_once_backup_keeps_the_first_original(self) -> None:
        path = self.tmp / "AGENTS.md"
        path.write_text("first\n", encoding="utf-8")
        copy_once_backup(path, dry_run=False)
        path.write_text("second\n", encoding="utf-8")

        self.assertIsNone(copy_once_backup(path, dry_run=False))
        self.assertEqual((self.tmp / "AGENTS.md.bak").read_text(encoding="utf-8"), "first\n")


class RemovalTests(FilesTestCase):
    def test_removal_below_a_symlinked_folder_is_refused(self) -> None:
        outside = self.tmp / "outside"
        outside.mkdir()
        (outside / "file.md").write_text("keep\n", encoding="utf-8")
        base = self.tmp / "base"
        base.mkdir()
        (base / "link").symlink_to(outside, target_is_directory=True)

        with self.assertRaises(UnsafePathError):
            remove_path(base / "link" / "file.md", stop_roots=[base], dry_run=False)
        self.assertTrue((outside / "file.md").is_file())

    def test_removing_a_symlinked_folder_keeps_its_target(self) -> None:
        outside = self.tmp / "outside"
        outside.mkdir()
        link = self.tmp / "link"
        link.symlink_to(outside, target_is_directory=True)

        self.assertTrue(remove_path(link, stop_roots=[self.tmp], dry_run=False))
        self.assertTrue(outside.is_dir())

    def test_path_through_a_symlink_out_of_the_root_is_not_within(self) -> None:
        root = self.tmp / "ws"
        root.mkdir()
        (root / "escape").symlink_to(self.tmp, target_is_directory=True)

        self.assertTrue(is_within(root / "AGENTS.md", root))
        self.assertFalse(is_within(root / "escape" / "victim.txt", root))


if __name__ == "__main__":
    unittest.main()
