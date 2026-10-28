#!/usr/bin/env python3
"""Custom build_py step to bundle skills, agents, rules, scripts, git-hooks, templates, and plugin.json into wheels."""

from __future__ import annotations

import shutil
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py as _build_py


class build_py(_build_py):  # type: ignore[misc]
    def run(self) -> None:
        super().run()
        repo_root = Path(__file__).resolve().parent
        assets_dir = Path(self.build_lib) / "craftsman" / "_assets"
        if assets_dir.exists():
            shutil.rmtree(assets_dir)
        assets_dir.mkdir(parents=True, exist_ok=True)

        for dirname in ("skills", "agents", "rules", "scripts", "git-hooks", "templates"):
            src_dir = repo_root / dirname
            if src_dir.is_dir():
                shutil.copytree(
                    src_dir,
                    assets_dir / dirname,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"),
                )
        for filename in ("plugin.json", "hooks.json"):
            src_file = repo_root / filename
            if src_file.is_file():
                shutil.copy2(src_file, assets_dir / filename)


setup(cmdclass={"build_py": build_py})
