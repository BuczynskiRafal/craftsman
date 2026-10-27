"""Shared fixtures for tests that drive the real `craftsman` CLI against throwaway repositories."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from typing import Any

from craftsman.gate import GateOwner, GateState, GateTier, save_state, state_path_for

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL_COUNT = len(json.loads((REPO_ROOT / "plugin.json").read_text(encoding="utf-8"))["skills"])
AMBIENT_PLATFORM_ENV_VARS = frozenset(
    {
        "CLAUDECODE",
        "CLAUDE_CODE_ENTRYPOINT",
        "CLAUDE_SESSION_ID",
        "CLAUDE_CODE_SESSION_ID",
        "ANTIGRAVITY_AGENT",
        "AGY_MANAGED_ENV",
        "ANTIGRAVITY_CONVERSATION_ID",
        "ANTIGRAVITY_SESSION_ID",
        "COPILOT_CLI",
        "GITHUB_COPILOT_CLI",
        "COPILOT_SESSION_ID",
        "VSCODE_PID",
        "VSCODE_IPC_HOOK",
        "TERM_PROGRAM",
        "CRAFTSMAN_SESSION_ID",
    }
)


def coverage_note(hunk_ids: list[str]) -> str:
    parts: list[str] = []
    for index, item in enumerate(hunk_ids, 1):
        num = item[1:] if item.startswith("H") and item[1:].isdigit() else str(index)
        parts.append(f"{item} f_{num}.py:{num}: x={num} -> returns {int(num) + 1} on boundary input")
    return "; ".join(parts)


def assert_cli_error(
    case: unittest.TestCase, proc: subprocess.CompletedProcess[str], exit_code: int, fragment: str
) -> None:
    """The command failed with `exit_code` and a message containing `fragment`, not a traceback."""
    case.assertEqual(proc.returncode, exit_code, proc.stderr)
    case.assertIn(fragment, proc.stderr)
    case.assertNotIn("Traceback", proc.stderr)


def open_gate(
    root: Path,
    base_sha: str,
    *,
    tier: GateTier = "standard",
    task: str = "",
    owner: GateOwner | None = None,
    **fields: Any,
) -> GateState:
    """Store an active gate for the work tree `root`, as `craftsman gate --init` would."""
    state = GateState.open(owner or GateOwner("cli"), base_sha, tier, task, **fields)
    save_state(state_path_for(root), state)
    return state


# --- Antigravity transcript entries ---------------------------------------------------------


def user_input(step: int, content: str) -> dict[str, Any]:
    return {"step_index": step, "type": "USER_INPUT", "content": content}


def invoke_subagent(step: int, *specs: dict[str, str]) -> dict[str, Any]:
    return {
        "step_index": step,
        "type": "PLANNER_RESPONSE",
        "tool_calls": [{"name": "invoke_subagent", "args": {"Subagents": json.dumps(list(specs))}}],
    }


def subagent_ack(step: int, *sub_ids: str) -> dict[str, Any]:
    ids = ",\n".join(f'  "conversationId": "{sub_id}"' for sub_id in sub_ids)
    return {
        "step_index": step,
        "type": "GENERIC",
        "content": f"Created the following subagent:\n{{\n{ids}\n}}",
    }


def subagent_done(step: int, sub_id: str) -> dict[str, Any]:
    return {
        "step_index": step,
        "type": "SYSTEM_MESSAGE",
        "content": f"<SYSTEM_MESSAGE>[Message] sender={sub_id} content=done</SYSTEM_MESSAGE>",
    }


def ask_question(step: int) -> dict[str, Any]:
    return {
        "step_index": step,
        "type": "PLANNER_RESPONSE",
        "tool_calls": [{"name": "ask_question", "args": {"questions": []}}],
    }


# An Antigravity `invoke_subagent` spec of a Craftsman reviewer.
REVIEWER = {"TypeName": "self", "Role": "craftsman-reviewer", "Prompt": "Review packet-1.md"}


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
        self,
        *args: str,
        check: bool = True,
        timeout: float = 60,
        stdin: str | None = None,
        env_extra: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = {k: v for k, v in os.environ.items() if k not in AMBIENT_PLATFORM_ENV_VARS}
        env["PYTHONPATH"] = str(REPO_ROOT)
        if env_extra:
            env.update(env_extra)
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

    def show(self) -> dict[str, Any]:
        payload = json.loads(self.cli("review", "show", "--json").stdout)
        assert isinstance(payload, dict)
        return payload

    def cover_all(self) -> None:
        """Cover every open hunk/file with every lens of the latest round, sweep last."""
        for lens, entry in self.show()["coverage"].items():
            missing = list(entry["missing"])
            flag = "--files" if lens == "design" else "--hunks"
            for start in range(0, len(missing), 4):
                batch = missing[start : start + 4]
                self.cli(
                    "review",
                    "cover",
                    "--lens",
                    lens,
                    flag,
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
