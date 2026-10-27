"""Shared fixtures for tests that drive the real `craftsman` CLI against throwaway repositories."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from collections.abc import Mapping, Sequence
from datetime import timedelta
from pathlib import Path
from typing import Any

from craftsman.diffscan import DiffSnapshot, Hunk
from craftsman.gate import GateOwner, GateState, GateTier, save_state, state_path_for
from craftsman.ledger import add_coverage, add_finding, add_run, open_round
from craftsman.lenses import Tier
from craftsman.records import (
    Coverage,
    Ledger,
    QualitySignal,
    Round,
    RunKind,
    Severity,
    format_timestamp,
    parse_timestamp,
)

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


def coverage_note(hunk_ids: Sequence[str]) -> str:
    parts: list[str] = []
    for index, item in enumerate(hunk_ids, 1):
        num = item[1:] if item.startswith("H") and item[1:].isdigit() else str(index)
        parts.append(f"{item} f_{num}.py:{num}: x={num} -> returns {int(num) + 1} on boundary input")
    return "; ".join(parts)


# --- in-memory ledgers ----------------------------------------------------------------------

BASE_SHA = "b" * 40
FINDING = {
    "lens": "intent",
    "path": "app.py",
    "line": 3,
    "title": "Empty list crashes total()",
    "scenario": "Calling total([]) divides by len(items) == 0 and raises.",
    "suggestion": "Return 0 for an empty list.",
    "signal_id": None,
}


def make_hunk(
    hunk_id: str, path: str = "app.py", lines: Sequence[str] = ("+x = 1",), *, new_start: int = 1
) -> Hunk:
    return Hunk(
        id=hunk_id,
        path=path,
        old_path=None,
        status="modified",
        old_start=new_start,
        old_len=sum(not line.startswith("+") for line in lines),
        new_start=new_start,
        new_len=sum(not line.startswith("-") for line in lines),
        header="",
        lines=tuple(lines),
        digest=f"d-{path}-{'|'.join(lines)}",
    )


def make_snap(*hunks: Hunk, fingerprint: str = "fp1", base: str = BASE_SHA) -> DiffSnapshot:
    return DiffSnapshot(
        base_sha=base,
        fingerprint=fingerprint,
        hunks=hunks,
        files=tuple(dict.fromkeys(hunk.path for hunk in hunks)),
        skipped=(),
        added=sum(line.startswith("+") for hunk in hunks for line in hunk.lines),
        deleted=sum(line.startswith("-") for hunk in hunks for line in hunk.lines),
    )


def two_hunks(fingerprint: str = "fp1") -> DiffSnapshot:
    return make_snap(make_hunk("H1", "a.py"), make_hunk("H2", "b.py"), fingerprint=fingerprint)


def prepared(
    snap: DiffSnapshot,
    lenses: Sequence[str] = ("intent", "craft"),
    tier: Tier = "quick",
    ledger: Ledger | None = None,
    symbols: Mapping[str, Sequence[str]] | None = None,
    signals: tuple[QualitySignal, ...] = (),
) -> tuple[Ledger, Round]:
    return open_round(
        ledger or Ledger(), snap, tier, lenses, "packet.md", symbols or {}, signals=signals
    )


def cover_open_hunks(ledger: Ledger, lens: str) -> Ledger:
    """Cover every open hunk (or file, for `design`) of the latest round with `lens`."""
    round_ = ledger.rounds[-1]
    targets = round_.open_files if lens == "design" else round_.open_hunk_ids
    for start in range(0, len(targets), 4):
        batch = list(targets[start : start + 4])
        ledger, _ = add_coverage(ledger, lens, batch, coverage_note(batch))
    return ledger


def record_run(
    ledger: Ledger,
    argv: Sequence[str] = ("pytest", "-q"),
    *,
    kind: RunKind = "test",
    exit_code: int = 0,
    fingerprint: str = "fp1",
    cwd: str = ".",
    fingerprint_after: str | None = None,
) -> Ledger:
    ledger, _ = add_run(
        ledger,
        kind=kind,
        argv=argv,
        cwd=cwd,
        exit_code=exit_code,
        duration_s=1.5,
        fingerprint=fingerprint,
        started_at="2026-01-01T00:00:00.000Z",
        output_tail="ok",
        fingerprint_after=fingerprint_after,
    )
    return ledger


def fully_reviewed(
    snap: DiffSnapshot, lenses: Sequence[str] = ("intent", "craft"), tier: Tier = "quick"
) -> Ledger:
    """A round for `snap` covered by every lens, with a passing test run at its fingerprint."""
    ledger, _ = prepared(snap, lenses, tier)
    for lens in lenses:
        ledger = cover_open_hunks(ledger, lens)
    return record_run(ledger, fingerprint=snap.fingerprint)


def record_finding(ledger: Ledger, severity: Severity = "major", **overrides: Any) -> Ledger:
    ledger, _ = add_finding(ledger, **{**FINDING, "severity": severity, **overrides})
    return ledger


def coverage_record(round_number: int, lens: str, digests: list[str], at: str) -> Coverage:
    return Coverage(round_number, lens, tuple(digests), coverage_note(["H1"]), at)


def swept(snap: DiffSnapshot) -> Ledger:
    """A standard round with `intent` and `sweep`, both covered in order, and a passing run."""
    ledger, _ = prepared(snap, lenses=("intent", "sweep"), tier="standard")
    ledger = cover_open_hunks(ledger, "intent")
    return record_run(cover_open_hunks(ledger, "sweep"), fingerprint=snap.fingerprint)


def shifted(timestamp: str, seconds: float) -> str:
    """`timestamp` moved by `seconds`, in the ledger's timestamp format."""
    return format_timestamp(parse_timestamp(timestamp) + timedelta(seconds=seconds))


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
