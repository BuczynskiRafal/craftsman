"""The Codex and Cursor plugin manifests of the repository.

The repository root is the plugin for both, as it is for Claude Code (`craftsman.platforms.claude`):
each marketplace points at `./` and each manifest lists the shared `skills/`.

Neither manifest declares hooks. Codex and Cursor read their own hook formats and Craftsman has no
adapter for them, so in both the review stays advisory, as with `--agents-workspace`. Codex
falls back to `.claude-plugin/plugin.json` when `.codex-plugin/plugin.json` is missing, and it
sets `CLAUDE_PLUGIN_ROOT`, so the Codex manifest also keeps the Claude Code hooks out of Codex.
Neither tool finds a default `hooks/hooks.json` here: the Antigravity `hooks.json` sits at the
root, not in `hooks/`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from craftsman.generated import ExportError
from craftsman.platforms.agents_md import CURSOR_RULE_HEADER, render_agents_md
from craftsman.platforms.claude import (
    PLUGIN_AUTHOR,
    PLUGIN_DESCRIPTION,
    PLUGIN_NAME,
    PLUGIN_REPOSITORY,
)

CODEX_MARKETPLACE = ".agents/plugins/marketplace.json"
CODEX_MANIFEST = ".codex-plugin/plugin.json"
CURSOR_DIR = ".cursor-plugin"
CURSOR_MARKETPLACE = f"{CURSOR_DIR}/marketplace.json"
CURSOR_MANIFEST = f"{CURSOR_DIR}/plugin.json"
CURSOR_RULES_DIR = f"{CURSOR_DIR}/rules"
CURSOR_RULE = f"{CURSOR_RULES_DIR}/craftsman.mdc"

DISPLAY_NAME = "Craftsman"
CATEGORY = "Developer Tools"
KEYWORDS = ["ai-agents", "code-review", "craft-review", "quality-gate", "testing"]
SKILLS_PATH = "./skills/"


def _dump(value: dict[str, Any]) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False) + "\n"


def _common_manifest_fields() -> dict[str, Any]:
    from craftsman import __version__

    return {
        "name": PLUGIN_NAME,
        "version": __version__,
        "description": PLUGIN_DESCRIPTION,
        "author": PLUGIN_AUTHOR,
        "homepage": PLUGIN_REPOSITORY,
        "repository": PLUGIN_REPOSITORY,
        "license": "MIT",
    }


def _render_codex_marketplace() -> str:
    return _dump(
        {
            "name": PLUGIN_NAME,
            "interface": {"displayName": DISPLAY_NAME},
            "plugins": [
                {
                    "name": PLUGIN_NAME,
                    "source": {"source": "local", "path": "./"},
                    "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
                    "category": CATEGORY,
                }
            ],
        }
    )


def _render_codex_manifest() -> str:
    return _dump(
        {
            **_common_manifest_fields(),
            "keywords": ["codex", *KEYWORDS],
            "skills": SKILLS_PATH,
            "interface": {
                "displayName": DISPLAY_NAME,
                "shortDescription": "Prove the change was reviewed and tested",
                "longDescription": (
                    "Build freely, review hard. Craftsman freezes the final diff, plans a bug hunt from it, "
                    "sends fresh-context hunters over every slot, has verifiers try to disprove every "
                    "finding, and records the outcome and the test runs in a ledger in the .git directory. "
                    "Codex runs the protocol without hooks, so the review is advisory and the report marks "
                    "it SELF-REVIEWED. The skills call the craftsman CLI: install it with "
                    "`pipx install craftsman-gate`."
                ),
                "developerName": PLUGIN_AUTHOR["name"],
                "category": CATEGORY,
                "capabilities": ["Interactive", "Read", "Write"],
                "websiteURL": PLUGIN_REPOSITORY,
                "defaultPrompt": [
                    "Use $craftsman:craftsman to add pagination to the catalog search",
                    "Use $craftsman:craft-review to review my staged changes and fix what you find",
                    "Use $craftsman:craft-review to review this branch, be thorough",
                ],
            },
        }
    )


def _render_cursor_marketplace() -> str:
    return _dump(
        {
            "name": PLUGIN_NAME,
            "owner": PLUGIN_AUTHOR,
            "metadata": {"description": PLUGIN_DESCRIPTION},
            "plugins": [{"name": PLUGIN_NAME, "source": "./", "description": PLUGIN_DESCRIPTION}],
        }
    )


def _render_cursor_manifest() -> str:
    return _dump(
        {
            "name": PLUGIN_NAME,
            "displayName": DISPLAY_NAME,
            **{key: value for key, value in _common_manifest_fields().items() if key != "name"},
            "keywords": ["cursor", *KEYWORDS],
            "category": "developer-tools",
            "skills": SKILLS_PATH,
            # Only the rendered rule: rules/ holds Antigravity rules, which this one already includes.
            "rules": f"./{CURSOR_RULE}",
        }
    )


def build_agent_plugin_files(repo_dir: Path) -> dict[str, str]:
    """The generated Codex and Cursor plugin files, by path relative to `repo_dir`."""
    return {
        CODEX_MARKETPLACE: _render_codex_marketplace(),
        CODEX_MANIFEST: _render_codex_manifest(),
        CURSOR_MARKETPLACE: _render_cursor_marketplace(),
        CURSOR_MANIFEST: _render_cursor_manifest(),
        CURSOR_RULE: CURSOR_RULE_HEADER + render_agents_md(repo_dir),
    }


def _drift(repo_dir: Path, files: dict[str, str]) -> list[str]:
    diffs: list[str] = []
    for rel_path, expected in files.items():
        dest = repo_dir / rel_path
        if not dest.is_file():
            diffs.append(f"missing: {rel_path}")
        elif dest.read_text(encoding="utf-8").replace("\r\n", "\n") != expected:
            diffs.append(f"differs: {rel_path}")
    rules_dir = repo_dir / CURSOR_RULES_DIR
    if rules_dir.is_dir():
        for path in sorted(rules_dir.iterdir()):
            rel = path.relative_to(repo_dir).as_posix()
            if rel not in files:
                diffs.append(f"extra: {rel}")
    return diffs


def export_agent_plugins(repo_dir: Path, *, check: bool = False) -> list[str]:
    """Write, or with `check` verify, the Codex and Cursor plugin files of the checkout at `repo_dir`."""
    if not (repo_dir / "craftsman" / "__init__.py").is_file():
        raise ExportError(
            f"--plugins builds from a Craftsman source checkout and {repo_dir} is not one; "
            "pass --repo-dir <checkout>"
        )
    files = build_agent_plugin_files(repo_dir)
    if check:
        diffs = _drift(repo_dir, files)
        if diffs:
            raise ExportError(
                "Codex/Cursor plugin drift detected (run 'craftsman export --plugins'):\n  "
                + "\n  ".join(diffs)
            )
        return sorted(files)
    rules_dir = repo_dir / CURSOR_RULES_DIR
    if rules_dir.is_dir():
        for stale in rules_dir.iterdir():
            if stale.is_file() and stale.relative_to(repo_dir).as_posix() not in files:
                stale.unlink()
    for rel_path, content in files.items():
        dest = repo_dir / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8", newline="\n")
    return sorted(files)
