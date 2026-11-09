"""Tests for the install manifest: its strict format and what it says Craftsman owns."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from craftsman.manifest import (
    BEFORE_4_0_MANIFEST_MESSAGE,
    MANIFEST_FILENAME,
    InstallError,
    InstallManifest,
    ModifiedKind,
    TargetKind,
    TargetManifest,
    load_manifest,
    save_manifest,
)

CLAUDE = TargetManifest(
    created_paths=(
        "/h/.claude/commands/craftsman.md",
        "/h/.claude/scripts/craftsman-stop-gate.py",
    ),
    created_dirs=("/h/.claude/commands",),
    modified_files={"/h/.claude/settings.json": ModifiedKind.CLAUDE_SETTINGS},
)
ANTIGRAVITY = TargetManifest(
    created_paths=("/h/.craftsman/scripts/craftsman-stop-gate.py", "/h/.craftsman/rules/a.md"),
    created_dirs=("/h/.craftsman/rules",),
    modified_files={"/h/.craftsman/hooks.json": ModifiedKind.HOOKS_JSON},
    hooks_json_keys=("craftsman-stop-gate",),
)


class ManifestTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)

    def write(self, data: Any) -> None:
        (self.base / MANIFEST_FILENAME).write_text(json.dumps(data), encoding="utf-8")

    def valid_json(self) -> dict[str, Any]:
        return InstallManifest({TargetKind.CLAUDE: CLAUDE}).to_json()


class RoundTripTests(ManifestTestCase):
    def test_saved_manifest_loads_unchanged(self) -> None:
        manifest = InstallManifest({TargetKind.CLAUDE: CLAUDE, TargetKind.ANTIGRAVITY: ANTIGRAVITY})

        save_manifest(self.base, manifest, dry_run=False)

        self.assertEqual(load_manifest(self.base), manifest)

    def test_saving_no_targets_removes_the_file(self) -> None:
        save_manifest(self.base, InstallManifest({TargetKind.CLAUDE: CLAUDE}), dry_run=False)

        save_manifest(self.base, InstallManifest({}), dry_run=False)

        self.assertFalse((self.base / MANIFEST_FILENAME).exists())

    def test_missing_manifest_loads_as_none(self) -> None:
        self.assertIsNone(load_manifest(self.base))


class AggregateTests(unittest.TestCase):
    def test_created_paths_are_the_union_of_every_target(self) -> None:
        manifest = InstallManifest({TargetKind.CLAUDE: CLAUDE, TargetKind.ANTIGRAVITY: ANTIGRAVITY})

        self.assertEqual(
            sorted(manifest.created_paths),
            sorted({*CLAUDE.created_paths, *ANTIGRAVITY.created_paths}),
        )

    def test_paths_of_others_exclude_the_given_target(self) -> None:
        manifest = InstallManifest({TargetKind.CLAUDE: CLAUDE, TargetKind.ANTIGRAVITY: ANTIGRAVITY})

        self.assertEqual(manifest.paths_of_others(TargetKind.CLAUDE), set(ANTIGRAVITY.created_paths))


class StrictFormatTests(ManifestTestCase):
    def test_corrupt_manifest_is_an_error(self) -> None:
        (self.base / MANIFEST_FILENAME).write_text("{broken", encoding="utf-8")

        with self.assertRaisesRegex(InstallError, "cannot read install manifest"):
            load_manifest(self.base)

    def test_manifest_without_targets_is_an_error(self) -> None:
        self.write({"created_paths": [], "created_dirs": []})

        with self.assertRaisesRegex(InstallError, "targets is missing"):
            load_manifest(self.base)

    def test_empty_targets_are_an_error(self) -> None:
        self.write({"targets": {}})

        with self.assertRaisesRegex(InstallError, "targets is empty"):
            load_manifest(self.base)

    def test_unknown_target_is_an_error(self) -> None:
        self.write({"targets": {"default": self.valid_json()["targets"]["claude"]}})

        with self.assertRaisesRegex(InstallError, "targets.default is not a target"):
            load_manifest(self.base)

    def test_target_with_extra_field_is_an_error(self) -> None:
        data = self.valid_json()
        data["targets"]["claude"]["mode"] = "copy"
        self.write(data)

        with self.assertRaisesRegex(InstallError, "mode is not a known field"):
            load_manifest(self.base)

    def test_unknown_modified_kind_is_an_error(self) -> None:
        data = self.valid_json()
        data["targets"]["claude"]["modified_files"] = {"/h/x.json": "settings"}
        self.write(data)

        with self.assertRaisesRegex(InstallError, "modified_files"):
            load_manifest(self.base)

    def test_hooks_json_keys_outside_antigravity_are_an_error(self) -> None:
        target = TargetManifest(hooks_json_keys=("craftsman-stop-gate",))
        self.write(InstallManifest({TargetKind.CLAUDE: target}).to_json())

        with self.assertRaisesRegex(InstallError, "which only Antigravity has"):
            load_manifest(self.base)

    def test_path_that_is_no_string_is_an_error(self) -> None:
        data = self.valid_json()
        data["targets"]["claude"]["created_paths"] = ["/h/a", 3]
        self.write(data)

        with self.assertRaisesRegex(InstallError, "created_paths"):
            load_manifest(self.base)


class ManifestsOfCraftsman4And5Tests(ManifestTestCase):
    """The format 4.x and 5.0 wrote, which `craftsman install` and `uninstall` still read."""

    def test_the_retired_git_hooks_fields_are_written_as_null_by_the_old_versions(self) -> None:
        self.write(
            {
                "targets": {
                    "claude": {
                        "created_paths": ["/h/.claude/commands/craftsman.md"],
                        "created_dirs": [],
                        "modified_files": {},
                        "hooks_json_keys": [],
                        "previous_hooks_path": None,
                        "hooks_dir": None,
                    }
                }
            }
        )

        manifest = load_manifest(self.base)

        assert manifest is not None
        self.assertEqual(
            manifest.targets[TargetKind.CLAUDE].created_paths, ("/h/.claude/commands/craftsman.md",)
        )

    def test_a_manifest_without_the_retired_fields_loads(self) -> None:
        self.assertIn(TargetKind.CLAUDE, self.loaded(self.valid_json()).targets)

    def test_the_retired_fields_are_no_longer_written(self) -> None:
        written = InstallManifest({TargetKind.CLAUDE: CLAUDE}).to_json()["targets"]["claude"]

        self.assertNotIn("previous_hooks_path", written)
        self.assertNotIn("hooks_dir", written)

    def test_a_retired_field_with_a_value_is_an_error(self) -> None:
        for key in ("previous_hooks_path", "hooks_dir"):
            with self.subTest(key=key):
                data = self.valid_json()
                data["targets"]["claude"][key] = "/h/.craftsman/git-hooks"
                self.write(data)

                with self.assertRaisesRegex(InstallError, f"{key} belongs to the git hooks"):
                    load_manifest(self.base)

    def loaded(self, data: dict[str, Any]) -> InstallManifest:
        self.write(data)
        manifest = load_manifest(self.base)
        assert manifest is not None
        return manifest


class ManifestsBefore4Tests(ManifestTestCase):
    """Craftsman before 4.0 recorded global git hooks; this version no longer migrates its manifests."""

    def assert_asks_for_a_reinstall(self) -> None:
        with self.assertRaises(InstallError) as caught:
            load_manifest(self.base)

        self.assertEqual(
            str(caught.exception),
            BEFORE_4_0_MANIFEST_MESSAGE.format(path=self.base / MANIFEST_FILENAME),
        )
        self.assertIn("run `craftsman install` again", str(caught.exception))

    def test_schema_v1_manifest_asks_for_a_reinstall(self) -> None:
        self.write(
            {
                "schema_version": 1,
                "version": "3.0.0",
                "mode": "copy",
                "created_paths": ["/h/.claude/commands/craftsman.md"],
                "created_dirs": [],
                "previous_hooks_path": None,
                "hooks_dir": None,
                "targets": {
                    "claude": {
                        "target": "claude",
                        "mode": "copy",
                        "version": "3.0.0",
                        "created_paths": ["/h/.claude/commands/craftsman.md"],
                        "created_dirs": [],
                        "modified_files": {"/h/.claude/settings.json": "claude_settings"},
                    }
                },
            }
        )

        self.assert_asks_for_a_reinstall()

    def test_any_schema_version_asks_for_a_reinstall(self) -> None:
        self.write({"schema_version": 99, "targets": {}})

        self.assert_asks_for_a_reinstall()

    def test_manifest_with_a_git_hooks_target_asks_for_a_reinstall(self) -> None:
        data = self.valid_json()
        data["targets"]["git_hooks"] = {
            "created_paths": ["/h/.craftsman/git-hooks/commit-msg"],
            "created_dirs": ["/h/.craftsman/git-hooks"],
            "modified_files": {},
            "hooks_json_keys": [],
            "previous_hooks_path": "~/my-hooks",
            "hooks_dir": "/h/.craftsman/git-hooks",
        }
        self.write(data)

        self.assert_asks_for_a_reinstall()


if __name__ == "__main__":
    unittest.main()
