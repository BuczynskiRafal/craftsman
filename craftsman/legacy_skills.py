"""Removal of the review skill that Craftsman < 5 installed as `code-review`; delete in 6.0.

Claude Code ships a built-in `/code-review`, so the skill is now `craft-review`. An earlier install
left `code-review` where the new one would sit beside it. Craftsman removes only what it owns: a
path an install manifest of that base folder records, or a file it generated (it carries the
generated marker). A `code-review` skill of the user's own is never touched.
"""

from __future__ import annotations

from pathlib import Path

from craftsman.files import exists
from craftsman.generated import is_craftsman_generated_file
from craftsman.manifest import InstallError, InstallManifest, TargetKind, load_manifest

LEGACY_SKILL_NAME = "code-review"
LEFTOVER_SKILL_PROBLEM = (
    "leftover {path} from Craftsman before 5.0, now `craft-review`; run `craftsman install` "
    "or `craftsman uninstall`"
)


def legacy_skill_paths(kind: TargetKind, base_dir: Path) -> tuple[Path, ...]:
    """Where an install of `kind` in `base_dir` put the `code-review` skill before 5.0."""
    if kind is TargetKind.CLAUDE:
        return (base_dir / "commands" / f"{LEGACY_SKILL_NAME}.md",)
    if kind in (TargetKind.ANTIGRAVITY, TargetKind.COPILOT):
        return (base_dir / "skills" / LEGACY_SKILL_NAME,)
    if kind is TargetKind.COPILOT_WORKSPACE:
        return (base_dir / ".github" / "skills" / LEGACY_SKILL_NAME,)
    return ()


def _generated(path: Path) -> bool:
    """Whether Craftsman generated `path`: a file, or a skill folder whose SKILL.md it generated."""
    if path.is_symlink():
        return False
    return is_craftsman_generated_file(path / "SKILL.md" if path.is_dir() else path)


def is_owned_legacy_skill(path: Path, manifest: InstallManifest | None) -> bool:
    """Whether `path` exists and Craftsman put it there."""
    if not exists(path):
        return False
    return (manifest is not None and manifest.records(path)) or _generated(path)


def owned_legacy_skills(
    kinds: tuple[TargetKind, ...], base_dir: Path, manifest: InstallManifest | None
) -> list[Path]:
    """The legacy `code-review` paths Craftsman owns for the targets `kinds` of `base_dir`."""
    return [
        path
        for kind in kinds
        for path in legacy_skill_paths(kind, base_dir)
        if is_owned_legacy_skill(path, manifest)
    ]


def inspect_legacy_skills(kinds: tuple[TargetKind, ...], base_dir: Path) -> list[str]:
    """Problems for the legacy `code-review` skills Craftsman owns in `base_dir`, for the doctor."""
    try:
        manifest = load_manifest(base_dir)
    except InstallError as exc:
        return [str(exc)]
    return [
        LEFTOVER_SKILL_PROBLEM.format(path=path)
        for path in owned_legacy_skills(kinds, base_dir, manifest)
    ]
