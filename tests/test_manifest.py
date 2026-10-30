"""Tests for the install manifest: its strict format and what it says Craftsman owns."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from craftsman.manifest import (
    LEGACY_V1_HOOKS_JSON_KEYS,
    MANIFEST_FILENAME,
    InstallError,
    InstallManifest,
    ModifiedKind,
    TargetKind,
    TargetManifest,
    load_manifest,
    save_manifest,
)

HOOKS = TargetManifest(
    created_paths=(
        "/h/.craftsman/git-hooks/commit-msg",
        "/h/.craftsman/scripts/craftsman-stop-gate.py",
    ),
    created_dirs=("/h/.craftsman/git-hooks",),
    previous_hooks_path="~/hooks",
    hooks_dir="/h/.craftsman/git-hooks",
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
        return InstallManifest({TargetKind.GIT_HOOKS: HOOKS}).to_json()


class RoundTripTests(ManifestTestCase):
    def test_saved_manifest_loads_unchanged(self) -> None:
        manifest = InstallManifest({TargetKind.GIT_HOOKS: HOOKS, TargetKind.ANTIGRAVITY: ANTIGRAVITY})

        save_manifest(self.base, manifest, dry_run=False)

        self.assertEqual(load_manifest(self.base), manifest)

    def test_saving_no_targets_removes_the_file(self) -> None:
        save_manifest(self.base, InstallManifest({TargetKind.GIT_HOOKS: HOOKS}), dry_run=False)

        save_manifest(self.base, InstallManifest({}), dry_run=False)

        self.assertFalse((self.base / MANIFEST_FILENAME).exists())

    def test_missing_manifest_loads_as_none(self) -> None:
        self.assertIsNone(load_manifest(self.base))


class AggregateTests(unittest.TestCase):
    def test_created_paths_are_the_union_of_every_target(self) -> None:
        manifest = InstallManifest({TargetKind.GIT_HOOKS: HOOKS, TargetKind.ANTIGRAVITY: ANTIGRAVITY})

        self.assertEqual(
            sorted(manifest.created_paths),
            sorted({*HOOKS.created_paths, *ANTIGRAVITY.created_paths}),
        )

    def test_paths_of_others_exclude_the_given_target(self) -> None:
        manifest = InstallManifest({TargetKind.GIT_HOOKS: HOOKS, TargetKind.ANTIGRAVITY: ANTIGRAVITY})

        self.assertEqual(manifest.paths_of_others(TargetKind.GIT_HOOKS), set(ANTIGRAVITY.created_paths))


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
        self.write({"targets": {"default": self.valid_json()["targets"]["git_hooks"]}})

        with self.assertRaisesRegex(InstallError, "targets.default is not a target"):
            load_manifest(self.base)

    def test_target_with_extra_field_is_an_error(self) -> None:
        data = self.valid_json()
        data["targets"]["git_hooks"]["mode"] = "copy"
        self.write(data)

        with self.assertRaisesRegex(InstallError, "mode is not a known field"):
            load_manifest(self.base)

    def test_unknown_modified_kind_is_an_error(self) -> None:
        data = self.valid_json()
        data["targets"]["git_hooks"]["modified_files"] = {"/h/x.json": "settings"}
        self.write(data)

        with self.assertRaisesRegex(InstallError, "modified_files"):
            load_manifest(self.base)

    def test_git_hooks_target_without_hooks_dir_is_an_error(self) -> None:
        data = self.valid_json()
        data["targets"]["git_hooks"]["hooks_dir"] = None
        self.write(data)

        with self.assertRaisesRegex(InstallError, "needs `hooks_dir`"):
            load_manifest(self.base)

    def test_hooks_path_on_another_target_is_an_error(self) -> None:
        data = self.valid_json()
        data["targets"]["claude"] = data["targets"].pop("git_hooks")
        self.write(data)

        with self.assertRaisesRegex(InstallError, "which only git hooks have"):
            load_manifest(self.base)

    def test_hooks_json_keys_outside_antigravity_are_an_error(self) -> None:
        target = TargetManifest(hooks_json_keys=("craftsman-stop-gate",))
        self.write(InstallManifest({TargetKind.CLAUDE: target}).to_json())

        with self.assertRaisesRegex(InstallError, "which only Antigravity has"):
            load_manifest(self.base)

    def test_path_that_is_no_string_is_an_error(self) -> None:
        data = self.valid_json()
        data["targets"]["git_hooks"]["created_paths"] = ["/h/a", 3]
        self.write(data)

        with self.assertRaisesRegex(InstallError, "created_paths"):
            load_manifest(self.base)


class LegacyV1ManifestTests(ManifestTestCase):
    def test_schema_v1_antigravity_manifest_loads_with_legacy_hook_keys(self) -> None:
        self.write(
            {
                "schema_version": 1,
                "version": "3.0.0",
                "mode": "symlink",
                "created_paths": ["/h/.gemini/config/scripts/git-guard.py"],
                "created_dirs": [],
                "previous_hooks_path": None,
                "hooks_dir": "/h/.gemini/config/git-hooks",
                "targets": {
                    "antigravity": {
                        "target": "antigravity",
                        "mode": "symlink",
                        "version": "3.0.0",
                        "created_paths": ["/h/.gemini/config/scripts/git-guard.py"],
                        "created_dirs": [],
                        "modified_files": {"/h/.gemini/config/hooks.json": "hooks_json"},
                    },
                    "git_hooks": {
                        "target": "git_hooks",
                        "mode": "symlink",
                        "version": "3.0.0",
                        "created_paths": ["/h/.gemini/config/git-hooks/commit-msg"],
                        "created_dirs": [],
                        "modified_files": {},
                        "previous_hooks_path": None,
                        "hooks_dir": "/h/.gemini/config/git-hooks",
                    },
                },
            }
        )

        manifest = load_manifest(self.base)

        assert manifest is not None
        self.assertEqual(
            manifest.targets[TargetKind.ANTIGRAVITY].hooks_json_keys,
            LEGACY_V1_HOOKS_JSON_KEYS,
        )
        self.assertEqual(
            manifest.targets[TargetKind.GIT_HOOKS].created_dirs,
            ("/h/.gemini/config/git-hooks",),
        )

    def test_schema_v1_claude_manifest_loads(self) -> None:
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

        manifest = load_manifest(self.base)

        assert manifest is not None
        self.assertEqual(
            manifest.targets[TargetKind.CLAUDE].created_paths,
            ("/h/.claude/commands/craftsman.md",),
        )

    def test_schema_v1_standalone_git_hooks_manifest_preserves_previous_hooks_path(self) -> None:
        self.write(
            {
                "schema_version": 1,
                "version": "3.2.0",
                "mode": "copy",
                "created_paths": ["/h/.craftsman/git-hooks/commit-msg"],
                "created_dirs": [],
                "previous_hooks_path": "~/my-hooks",
                "hooks_dir": "/h/.craftsman/git-hooks",
                "targets": {
                    "git_hooks": {
                        "target": "git_hooks",
                        "mode": "copy",
                        "version": "3.2.0",
                        "created_paths": ["/h/.craftsman/git-hooks/commit-msg"],
                        "created_dirs": [],
                        "modified_files": {},
                        "previous_hooks_path": "~/my-hooks",
                        "hooks_dir": "/h/.craftsman/git-hooks",
                    }
                },
            }
        )

        manifest = load_manifest(self.base)

        assert manifest is not None
        git_hooks = manifest.targets[TargetKind.GIT_HOOKS]
        self.assertEqual(git_hooks.previous_hooks_path, "~/my-hooks")
        self.assertEqual(git_hooks.hooks_dir, "/h/.craftsman/git-hooks")
        self.assertEqual(git_hooks.created_dirs, ("/h/.craftsman/git-hooks",))

    def test_unsupported_schema_version_is_an_error(self) -> None:
        self.write({"schema_version": 99, "targets": {}})

        with self.assertRaisesRegex(InstallError, "schema_version must be 1"):
            load_manifest(self.base)


if __name__ == "__main__":
    unittest.main()
