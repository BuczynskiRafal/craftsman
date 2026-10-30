"""Shared fixtures for tests that drive the real `craftsman` CLI against throwaway repositories."""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest import mock

from craftsman.diffscan import ChangeStatus, DiffSnapshot, Hunk
from craftsman.gate import GateOwner, GateState, GateTier, save_state, state_path_for
from craftsman.install import install_main
from craftsman.ledger import add_coverage, add_finding, add_run, open_round
from craftsman.lenses import Tier
from craftsman.manifest import (
    InstallManifest,
    TargetKind,
    TargetManifest,
    load_manifest,
    save_manifest,
)
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
from craftsman.uninstall import uninstall_main

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
    hunk_id: str,
    path: str = "app.py",
    lines: Sequence[str] = ("+x = 1",),
    *,
    new_start: int = 1,
    status: ChangeStatus = "modified",
) -> Hunk:
    return Hunk(
        id=hunk_id,
        path=path,
        old_path=None,
        status=status,
        old_start=new_start,
        old_len=sum(not line.startswith("+") for line in lines),
        new_start=new_start,
        new_len=sum(not line.startswith("-") for line in lines),
        header="",
        lines=tuple(lines),
        digest=f"d-{path}-{'|'.join(lines)}",
    )


def change_hunk(
    path: str,
    added: Sequence[str] = (),
    removed: Sequence[str] = (),
    *,
    hunk_id: str = "H1",
    status: ChangeStatus = "modified",
) -> Hunk:
    """A hunk that removes then adds the given lines, as git lists a modification."""
    lines = (*(f"-{text}" for text in removed), *(f"+{text}" for text in added))
    return make_hunk(hunk_id, path, lines, status=status)


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


class GitRepo:
    """A throwaway git repository with deterministic configuration and no hooks."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.git("init", "-q")
        for key, value in (
            ("user.email", "dev@example.com"),
            ("user.name", "Dev"),
            ("commit.gpgsign", "false"),
            ("core.autocrlf", "false"),
            ("core.hooksPath", str(root / ".git" / "no-hooks")),
        ):
            self.git("config", key, value)

    def git(self, *args: str) -> str:
        proc = subprocess.run(
            ["git", *args], cwd=self.root, capture_output=True, text=True, check=True, timeout=30
        )
        return proc.stdout

    def write(self, path: str, content: str | bytes) -> Path:
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        data = content.encode("utf-8") if isinstance(content, str) else content
        target.write_bytes(data)
        return target

    def commit(self, message: str = "commit") -> str:
        self.git("add", "-A")
        self.git("commit", "-q", "--no-verify", "-m", message)
        return self.git("rev-parse", "HEAD").strip()

    def commit_empty(self) -> str:
        self.git("commit", "-q", "--allow-empty", "--no-verify", "-m", "root")
        return self.git("rev-parse", "HEAD").strip()


class EvidenceRepo(GitRepo):
    """A temporary git repository with one committed file, driven through the real CLI."""

    def __init__(self, root: Path) -> None:
        root.mkdir(parents=True, exist_ok=True)
        super().__init__(root)
        self.write(".gitignore", "__pycache__/\n")
        self.write("app.py", "x = 1\n")
        self.base_sha = self.commit("init")

    @property
    def ledger_dir(self) -> Path:
        return self.root / ".git" / "craftsman"

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


def write_jsonl(path: Path, *entries: dict[str, Any]) -> None:
    """A transcript of `entries`, one JSON object per line."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(entry) + "\n" for entry in entries), encoding="utf-8")


@dataclass(frozen=True)
class CliResult:
    code: int
    out: str
    err: str


def run_main(main: Callable[[list[str]], int], *args: str) -> CliResult:
    """Run a CLI entry point in this process, capturing its output; argparse exits become codes."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = main(list(args))
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 2
    return CliResult(code, out.getvalue(), err.getvalue())


def snapshot_tree(root: Path) -> dict[str, tuple[str, bytes | str, int]]:
    """Every file, symlink and folder under `root`, with contents and permissions."""
    state: dict[str, tuple[str, bytes | str, int]] = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        folder = Path(dirpath)
        state[str(folder.relative_to(root))] = ("dir", "", folder.lstat().st_mode & 0o777)
        for name in dirnames + filenames:
            path = folder / name
            rel = str(path.relative_to(root))
            if path.is_symlink():
                state[rel] = ("symlink", os.readlink(path), 0)
            elif path.is_file():
                state[rel] = ("file", path.read_bytes(), path.stat().st_mode & 0o777)
    return state


class IsolatedHomeTestCase(unittest.TestCase):
    """A throwaway home folder, CRAFTSMAN_HOME and global git configuration; the rest of the
    environment is cleared, so no detected platform or setting of the real user leaks in."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.git_config = self.tmp / "gitconfig"
        self.git_config.write_text("", encoding="utf-8")
        self.env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self.home),
            "USERPROFILE": str(self.home),
            "GIT_CONFIG_GLOBAL": str(self.git_config),
            "GIT_CONFIG_NOSYSTEM": "1",
        }
        patcher = mock.patch.dict(os.environ, self.env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def install(self, *args: str) -> CliResult:
        return run_main(install_main, "--repo-dir", str(REPO_ROOT), *args)

    def uninstall(self, *args: str) -> CliResult:
        return run_main(uninstall_main, *args)

    def git_global(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "config", "--global", *args],
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )

    def hooks_path(self) -> str | None:
        proc = self.git_global("--get", "core.hooksPath")
        return proc.stdout.strip() if proc.returncode == 0 else None

    def assert_ok(self, result: CliResult) -> None:
        self.assertEqual(result.code, 0, result.err)

    def write_legacy_git_hooks(
        self,
        base_dir: Path | None = None,
        *,
        previous_hooks_path: str | None = None,
        set_hooks_path: bool = True,
    ) -> Path:
        """Create a Craftsman 3.x `TargetKind.GIT_HOOKS` install under `base_dir` (`~/.craftsman`)."""
        base = base_dir if base_dir is not None else self.home / ".craftsman"
        hooks_dir = base / "git-hooks"
        scripts_dir = base / "scripts"
        existing = load_manifest(base)
        created_dirs: list[str] = []
        for directory in (base, hooks_dir, scripts_dir):
            if not directory.exists():
                created_dirs.append(str(directory))
                directory.mkdir(parents=True, exist_ok=True)
        hook_files = [hooks_dir / "commit-msg", hooks_dir / "pre-push"]
        shared_script = scripts_dir / "craftsman-stop-gate.py"
        for hook_file in hook_files:
            hook_file.write_text("#!/bin/sh\n", encoding="utf-8")
        if not shared_script.exists():
            shared_script.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
        target = TargetManifest(
            created_paths=tuple(str(p) for p in (*hook_files, shared_script)),
            created_dirs=tuple(created_dirs),
            previous_hooks_path=previous_hooks_path,
            hooks_dir=str(hooks_dir),
        )
        manifest = (existing or InstallManifest({})).with_target(TargetKind.GIT_HOOKS, target)
        save_manifest(base, manifest, dry_run=False)
        if set_hooks_path:
            self.git_global("core.hooksPath", str(hooks_dir))
        return hooks_dir

    def write_legacy_v1_git_hooks(
        self,
        base_dir: Path | None = None,
        *,
        previous_hooks_path: str | None = None,
        created_dirs: Sequence[str] | None = None,
        extra_targets: Mapping[str, Any] | None = None,
        set_hooks_path: bool = True,
    ) -> Path:
        """Create a Craftsman 3.0-3.2 (`schema_version: 1`) `git_hooks` install under `base_dir`."""
        base = base_dir if base_dir is not None else self.home / ".craftsman"
        hooks_dir = base / "git-hooks"
        scripts_dir = base / "scripts"
        recorded_dirs: list[str] = []
        for directory in (base, hooks_dir, scripts_dir):
            if not directory.exists():
                recorded_dirs.append(str(directory))
                directory.mkdir(parents=True, exist_ok=True)
        hook_files = [hooks_dir / "commit-msg", hooks_dir / "pre-push", hooks_dir / ".python_cmd"]
        for hook_file in hook_files:
            hook_file.write_text("#!/bin/sh\n", encoding="utf-8")
        shared_script = scripts_dir / "craftsman-stop-gate.py"
        if not shared_script.exists():
            shared_script.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
        git_hooks_paths = [str(p) for p in (*hook_files, shared_script)]
        git_hooks_dirs = list(recorded_dirs if created_dirs is None else created_dirs)
        targets: dict[str, Any] = {
            **(dict(extra_targets) if extra_targets else {}),
            "git_hooks": {
                "target": "git_hooks",
                "mode": "copy",
                "version": "3.2.0",
                "created_paths": git_hooks_paths,
                "created_dirs": git_hooks_dirs,
                "modified_files": {},
                "previous_hooks_path": previous_hooks_path,
                "hooks_dir": str(hooks_dir),
            },
        }
        all_paths = list(
            dict.fromkeys(p for entry in targets.values() for p in entry.get("created_paths", ()))
        )
        all_dirs = list(
            dict.fromkeys(d for entry in targets.values() for d in entry.get("created_dirs", ()))
        )
        raw_manifest = {
            "schema_version": 1,
            "version": "3.2.0",
            "mode": "copy",
            "created_paths": all_paths,
            "created_dirs": all_dirs,
            "previous_hooks_path": previous_hooks_path,
            "hooks_dir": str(hooks_dir),
            "targets": targets,
        }
        (base / ".craftsman-install.json").write_text(
            json.dumps(raw_manifest, indent=2) + "\n", encoding="utf-8"
        )
        if set_hooks_path:
            self.git_global("core.hooksPath", str(hooks_dir))
        return hooks_dir
