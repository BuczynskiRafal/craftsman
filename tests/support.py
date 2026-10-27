"""Shared fixtures for tests that drive the real `craftsman` CLI against throwaway repositories."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
STOP_GATE = REPO_ROOT / "scripts" / "craftsman-stop-gate.py"


def coverage_note(hunk_ids: list[str]) -> str:
    return f"{' '.join(hunk_ids)}: traced empty, boundary and typical inputs through every branch"


class EvidenceRepo:
    """A temporary git repository with one committed file, driven through the real CLI."""

    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        for args in (
            ("init", "-q"),
            ("config", "user.email", "dev@example.com"),
            ("config", "user.name", "Dev"),
            ("config", "commit.gpgsign", "false"),
            ("config", "core.autocrlf", "false"),
            ("config", "core.hooksPath", str(root / ".git" / "no-hooks")),
        ):
            self.git(*args)
        (root / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
        (root / "app.py").write_text("x = 1\n", encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-q", "--no-verify", "-m", "init")
        self.base_sha = self.git("rev-parse", "HEAD").strip()

    @property
    def ledger_dir(self) -> Path:
        return self.root / ".git" / "craftsman"

    def git(self, *args: str) -> str:
        proc = subprocess.run(
            ["git", *args], cwd=self.root, capture_output=True, text=True, check=True, timeout=30
        )
        return proc.stdout

    def write(self, name: str, text: str) -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def write_lines(self, name: str, count: int) -> Path:
        return self.write(name, "".join(f"value_{index} = {index}\n" for index in range(count)))

    def cli(
        self, *args: str, check: bool = True, timeout: float = 60, stdin: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
        proc = subprocess.run(
            [sys.executable, "-m", "craftsman.cli", *args],
            cwd=self.root,
            env=env,
            input=stdin,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
        if check and proc.returncode != 0:
            raise AssertionError(
                f"craftsman {' '.join(args)} exited {proc.returncode}:\n{proc.stdout}\n{proc.stderr}"
            )
        return proc

    def gate(self, state_file: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(STOP_GATE), "--state-file", str(state_file), *args],
            cwd=self.root,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )

    def show(self) -> dict[str, Any]:
        payload = json.loads(self.cli("review", "show", "--json").stdout)
        assert isinstance(payload, dict)
        return payload

    def cover_all(self) -> None:
        """Cover every open hunk with every lens of the latest round, sweep last."""
        for lens, entry in self.show()["coverage"].items():
            missing = list(entry["missing"])
            for start in range(0, len(missing), 4):
                batch = missing[start : start + 4]
                self.cli(
                    "review",
                    "cover",
                    "--lens",
                    lens,
                    "--hunks",
                    ",".join(batch),
                    "--note",
                    coverage_note(batch),
                )

    def record_full_evidence(self, tier: str) -> None:
        """Everything the gate needs for `tier` at the current code: a round, coverage and runs."""
        self.cli("review", "prepare", "--tier", tier)
        self.cover_all()
        self.cli("run", "--kind", "test", "--", sys.executable, "-c", "print('tests ok')")
        if tier == "deep":
            self.cli("run", "--kind", "e2e", "--", sys.executable, "-c", "print('e2e ok')")
