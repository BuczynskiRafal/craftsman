"""Tests for what one install target stages and records in its manifest."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from craftsman.files import BACKUP_DIR_NAME
from craftsman.manifest import ModifiedKind, TargetKind, TargetManifest, load_manifest
from craftsman.staging import EventKind, TargetSession, Tracker, path_key, revert_modified_file


class StagingTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name).resolve()
        self.base = self.home / ".craftsman"

    def session(self, *, dry_run: bool = False) -> TargetSession:
        return TargetSession.begin(self.base, TargetKind.ANTIGRAVITY, self.home, Tracker(dry_run))


class SessionTests(StagingTestCase):
    def test_created_folders_are_recorded_up_to_the_first_existing_one(self) -> None:
        session = self.session()

        session.make_dirs(self.base / "skills")

        self.assertEqual(session.created_dirs, [str(self.base), str(self.base / "skills")])

    def test_users_file_in_the_way_is_moved_aside(self) -> None:
        target = self.base / "skills" / "SKILL.md"
        target.parent.mkdir(parents=True)
        target.write_text("mine\n", encoding="utf-8")
        session = self.session()

        session.stage(target)

        (backup,) = (target.parent / BACKUP_DIR_NAME).iterdir()
        self.assertEqual(backup.read_text(encoding="utf-8"), "mine\n")
        self.assertEqual(session.tracker.count(EventKind.BACKED_UP), 1)

    def test_finish_records_the_target(self) -> None:
        session = self.session()
        session.make_dirs(self.base)
        session.created(self.base / "a")

        session.finish()

        manifest = load_manifest(self.base)
        assert manifest is not None
        self.assertEqual(
            manifest.targets[TargetKind.ANTIGRAVITY].created_paths, (str(self.base / "a"),)
        )

    def test_reinstall_removes_what_it_no_longer_creates(self) -> None:
        first = self.session()
        first.make_dirs(self.base)
        (self.base / "old").write_text("x", encoding="utf-8")
        first.created(self.base / "old")
        first.finish()

        self.session().finish()

        self.assertFalse((self.base / "old").exists())

    def test_dry_run_finish_writes_no_manifest(self) -> None:
        self.session(dry_run=True).finish()

        self.assertIsNone(load_manifest(self.base))


class RevertTests(StagingTestCase):
    def settings(self, data: dict[str, object]) -> Path:
        path = self.home / "settings.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_created_file_left_empty_is_removed(self) -> None:
        path = self.settings({"craftsman-stop-gate": {}})
        target = TargetManifest(
            created_paths=(path_key(path),), hooks_json_keys=("craftsman-stop-gate",)
        )

        revert_modified_file(path, ModifiedKind.HOOKS_JSON, target, Tracker(dry_run=False))

        self.assertFalse(path.exists())

    def test_users_file_keeps_its_own_entries(self) -> None:
        path = self.settings({"craftsman-stop-gate": {}, "mine": {}})
        target = TargetManifest(hooks_json_keys=("craftsman-stop-gate",))

        revert_modified_file(path, ModifiedKind.HOOKS_JSON, target, Tracker(dry_run=False))

        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"mine": {}})

    def test_dry_run_reports_without_writing(self) -> None:
        path = self.settings({"craftsman-stop-gate": {}, "mine": {}})
        tracker = Tracker(dry_run=True)

        revert_modified_file(
            path,
            ModifiedKind.HOOKS_JSON,
            TargetManifest(hooks_json_keys=("craftsman-stop-gate",)),
            tracker,
        )

        self.assertEqual(tracker.count(EventKind.RESTORED), 1)
        self.assertIn("craftsman-stop-gate", path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
