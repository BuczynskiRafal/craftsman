"""Test enforcing version synchronization across all repository manifests."""

import json
import re
import unittest
from pathlib import Path

SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")
PYPROJECT_VERSION_RE = re.compile(r'^\s*version\s*=\s*"([^"]+)"', re.MULTILINE)
INIT_VERSION_RE = re.compile(r'^\s*__version__\s*=\s*"([^"]+)"', re.MULTILINE)


class TestVersionConsistency(unittest.TestCase):
    """Enforce that package and manifest versions never diverge."""

    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parent.parent

    def test_version_consistency(self) -> None:
        versions: dict[str, str] = {}

        # 1. pyproject.toml
        pyproject_path = self.repo_root / "pyproject.toml"
        self.assertTrue(pyproject_path.exists(), "pyproject.toml missing")
        pyproject_content = pyproject_path.read_text(encoding="utf-8")
        match = PYPROJECT_VERSION_RE.search(pyproject_content)
        self.assertIsNotNone(match, "Could not find version in pyproject.toml")
        assert match is not None
        versions["pyproject.toml"] = match.group(1)

        # 2. craftsman/__init__.py
        init_path = self.repo_root / "craftsman" / "__init__.py"
        self.assertTrue(init_path.exists(), "craftsman/__init__.py missing")
        init_content = init_path.read_text(encoding="utf-8")
        match = INIT_VERSION_RE.search(init_content)
        self.assertIsNotNone(match, "Could not find __version__ in craftsman/__init__.py")
        assert match is not None
        versions["craftsman/__init__.py"] = match.group(1)

        # 3. plugin.json (Antigravity plugin manifest)
        plugin_path = self.repo_root / "plugin.json"
        self.assertTrue(plugin_path.exists(), "plugin.json missing")
        with plugin_path.open(encoding="utf-8") as f:
            plugin_data = json.load(f)
        self.assertIn("version", plugin_data, "plugin.json missing 'version' key")
        versions["plugin.json"] = str(plugin_data["version"])

        # 4. plugins/claude/craftsman/.claude-plugin/plugin.json (if present)
        claude_plugin_path = (
            self.repo_root
            / "plugins"
            / "claude"
            / "craftsman"
            / ".claude-plugin"
            / "plugin.json"
        )
        if claude_plugin_path.exists():
            with claude_plugin_path.open(encoding="utf-8") as f:
                claude_plugin_data = json.load(f)
            self.assertIn(
                "version",
                claude_plugin_data,
                "Claude plugin.json missing 'version' key",
            )
            versions["plugins/claude/craftsman/.claude-plugin/plugin.json"] = str(
                claude_plugin_data["version"]
            )

        # 5. .claude-plugin/marketplace.json (if present)
        marketplace_path = self.repo_root / ".claude-plugin" / "marketplace.json"
        if marketplace_path.exists():
            with marketplace_path.open(encoding="utf-8") as f:
                marketplace_data = json.load(f)
            plugins = marketplace_data.get("plugins", [])
            for p in plugins:
                if p.get("name") == "craftsman" and "version" in p:
                    versions[".claude-plugin/marketplace.json"] = str(p["version"])
                    break

        # Validate format
        for file_name, version in versions.items():
            self.assertRegex(
                version,
                SEMVER_RE,
                f"Version '{version}' in {file_name} does not match SemVer (X.Y.Z)",
            )

        # Check all match
        unique_versions = set(versions.values())
        self.assertEqual(
            len(unique_versions),
            1,
            f"Version mismatch detected across manifests: {versions}",
        )


if __name__ == "__main__":
    unittest.main()
