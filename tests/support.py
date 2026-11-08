"""Shared fixtures for tests that drive the real `craftsman` CLI against throwaway repositories."""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest import mock

from craftsman.diffscan import ChangeStatus, DiffSnapshot, Hunk
from craftsman.gate import GateOwner, GateState, save_state, state_path_for
from craftsman.install import install_main
from craftsman.ledger import add_coverage, add_finding, add_run, open_round
from craftsman.records import (
    AgentEvent,
    AgentEventKind,
    HuntPlan,
    Ledger,
    QualitySignal,
    Round,
    RunKind,
    Severity,
    Slot,
    SlotKind,
    format_timestamp,
    parse_timestamp,
)
from craftsman.uninstall import uninstall_main

from tests.ambient import AMBIENT_PLATFORM_ENV_VARS

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL_COUNT = len(json.loads((REPO_ROOT / "plugin.json").read_text(encoding="utf-8"))["skills"])
REMOVED_SKILLS = (
    "code-quality",
    "extreme-optimizer",
    "high-level-commit-message",
    "leetcode-style-code-reviewer",
)


def repo_with_removed_skills(tmp: Path) -> Path:
    """The assets of a Craftsman release that still shipped `REMOVED_SKILLS`, for upgrade tests."""
    repo = tmp / "old-release"
    for folder in ("skills", "rules", "scripts", "templates"):
        shutil.copytree(REPO_ROOT / folder, repo / folder, ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy(REPO_ROOT / "hooks.json", repo / "hooks.json")
    (repo / "craftsman").mkdir()
    (repo / "craftsman" / "__init__.py").write_text("", encoding="utf-8")
    for name in REMOVED_SKILLS:
        skill = repo / "skills" / name
        skill.mkdir()
        (skill / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: The removed {name} skill.\n---\n# {name}\n",
            encoding="utf-8",
        )
    return repo


def write_pre_4_0_install(base: Path) -> Path:
    """A manifest of Craftsman 3.0 to 3.2 (`schema_version: 1`) with a global git hooks target."""
    hooks_dir = base / "git-hooks"
    paths = [str(hooks_dir / "commit-msg"), str(hooks_dir / "pre-push")]
    hooks_dir.mkdir(parents=True)
    for path in paths:
        Path(path).write_text("#!/bin/sh\n", encoding="utf-8")
    manifest = {
        "schema_version": 1,
        "version": "3.2.0",
        "mode": "copy",
        "created_paths": paths,
        "created_dirs": [str(hooks_dir)],
        "previous_hooks_path": None,
        "hooks_dir": str(hooks_dir),
        "targets": {
            "git_hooks": {
                "target": "git_hooks",
                "mode": "copy",
                "version": "3.2.0",
                "created_paths": paths,
                "created_dirs": [str(hooks_dir)],
                "modified_files": {},
                "previous_hooks_path": None,
                "hooks_dir": str(hooks_dir),
            }
        },
    }
    manifest_path = base / ".craftsman-install.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest_path


def coverage_note(hunk_ids: Sequence[str]) -> str:
    parts: list[str] = []
    for index, item in enumerate(hunk_ids, 1):
        num = item[1:] if item.startswith("H") and item[1:].isdigit() else str(index)
        parts.append(f"{item} f_{num}.py:{num}: x={num} -> returns {int(num) + 1} on boundary input")
    return "; ".join(parts)


# --- in-memory ledgers ----------------------------------------------------------------------

BASE_SHA = "b" * 40
FINDING: dict[str, Any] = {
    "lens": "intent",
    "path": "app.py",
    "line": 3,
    "title": "Empty list crashes total()",
    "scenario": "Calling total([]) divides by len(items) == 0 and raises.",
    "suggestion": "Return 0 for an empty list.",
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
        added=sum(line.startswith("+") for hunk in hunks for line in hunk.lines),
        deleted=sum(line.startswith("-") for hunk in hunks for line in hunk.lines),
    )


def two_hunks(fingerprint: str = "fp1") -> DiffSnapshot:
    return make_snap(make_hunk("H1", "a.py"), make_hunk("H2", "b.py"), fingerprint=fingerprint)


def prepared(
    snap: DiffSnapshot,
    deep: bool = False,
    ledger: Ledger | None = None,
    symbols: Mapping[str, Sequence[str]] | None = None,
    signals: tuple[QualitySignal, ...] = (),
) -> tuple[Ledger, Round]:
    return open_round(ledger or Ledger(), snap, deep, "packet.md", symbols or {}, signals=signals)


def cover_open_hunks(ledger: Ledger) -> Ledger:
    """Cover every open hunk of the latest round in one record."""
    batch = list(ledger.rounds[-1].open_hunk_ids)
    if not batch:
        return ledger
    ledger, _ = add_coverage(ledger, batch, coverage_note(batch))
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


def fully_reviewed(snap: DiffSnapshot, deep: bool = False) -> Ledger:
    """A round for `snap` with every hunk covered and a passing test run at its fingerprint."""
    ledger, _ = prepared(snap, deep)
    return record_run(cover_open_hunks(ledger), fingerprint=snap.fingerprint)


def record_finding(ledger: Ledger, severity: Severity = "major", **overrides: Any) -> Ledger:
    ledger, _ = add_finding(ledger, **{**FINDING, "severity": severity, **overrides})
    return ledger


def shifted(timestamp: str, seconds: float) -> str:
    """`timestamp` moved by `seconds`, in the ledger's timestamp format."""
    return format_timestamp(parse_timestamp(timestamp) + timedelta(seconds=seconds))


def make_slot(
    slot_id: str = "W1-1",
    *,
    hunks: Sequence[str] = ("H1",),
    lenses: Sequence[str] = ("intent",),
    kind: SlotKind | None = None,
    trigger: str | None = None,
    changed_lines: int = 1,
) -> Slot:
    """A valid slot for `slot_id`; its generation and kind follow the id unless `kind` says otherwise."""
    suffix = slot_id.split("-", 1)[1]
    default_kind: SlotKind = "fresh" if suffix.startswith("X") else "variant" if suffix.startswith("F") else "bug"
    if trigger is None and suffix.startswith("X"):
        trigger = "deep"
    if trigger is None and suffix.startswith("F"):
        trigger = suffix
    return Slot(
        id=slot_id,
        generation=int(slot_id[1]),
        kind=kind or default_kind,
        hunks=tuple(hunks),
        lenses=tuple(lenses),
        changed_lines=changed_lines,
        trigger=trigger,
    )


def planned(ledger: Ledger, *slots: Slot, reasons: Sequence[str] = ()) -> Ledger:
    """`ledger` with the hunt plan `slots` attached to its latest round."""
    plan = HuntPlan(tuple(reasons), tuple(slots))
    latest = replace(ledger.rounds[-1], plan=plan)
    return replace(ledger, rounds=(*ledger.rounds[:-1], latest))


T0 = "2026-01-01T00:00:00.000Z"


def at(seconds: float) -> str:
    """The timestamp `seconds` after `T0`: a fixed clock for ledgers whose record times matter."""
    return shifted(T0, seconds)


def agent_event(
    event: AgentEventKind,
    agent: str,
    agent_id: str,
    seconds: float,
    *,
    platform: str = "claude",
    records: tuple[str, ...] | None = None,
) -> AgentEvent:
    """A subagent start or stop event `seconds` after `T0`."""
    return AgentEvent(platform, event, agent, agent_id, at(seconds), records=records)


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
    deep: bool = False,
    task: str = "",
    owner: GateOwner | None = None,
    **fields: Any,
) -> GateState:
    """Store an active gate for the work tree `root`, as `craftsman gate --init` would."""
    state = GateState.open(owner or GateOwner("cli"), base_sha, deep, task, **fields)
    save_state(state_path_for(root), state)
    return state


# --- Antigravity transcript entries ---------------------------------------------------------


# --- Antigravity transcripts, in the shapes the platform writes (compact `transcript.jsonl`) -----------------------

AGY_EPOCH = parse_timestamp(T0)  # the fixed clock of `at()`: transcripts and in-memory ledgers share one time axis


def agy_time(seconds: float = 0) -> str:
    """A whole-second `created_at` `seconds` after a fixed epoch (Antigravity writes whole seconds)."""
    return (AGY_EPOCH + timedelta(seconds=int(seconds))).strftime("%Y-%m-%dT%H:%M:%SZ")


def user_input(step: int, content: str, at: float | None = None) -> dict[str, Any]:
    entry: dict[str, Any] = {"step_index": step, "source": "USER_EXPLICIT", "type": "USER_INPUT", "content": content}
    if at is not None:
        entry["created_at"] = agy_time(at)
    return entry


def agy_call(name: str, *, encoded: bool = True, **args: Any) -> dict[str, Any]:
    """One tool call of a `PLANNER_RESPONSE`; `transcript.jsonl` JSON-encodes the values of `args`, `transcript_full.jsonl` does not."""
    return {"name": name, "args": {key: json.dumps(value) if encoded else value for key, value in args.items()}}


def invoke_call(role: str = "craftsman-reviewer", *, type_name: str = "self", encoded: bool = True) -> dict[str, Any]:
    return agy_call("invoke_subagent", encoded=encoded, TypeName=type_name, Role=role, Prompt="Review packet-1.md")


def planner(step: int, *calls: dict[str, Any], at: float | None = None, status: str = "DONE") -> dict[str, Any]:
    """A model step: a `PLANNER_RESPONSE`; without calls it ends the turn."""
    entry: dict[str, Any] = {"step_index": step, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": status}
    if calls:
        entry["tool_calls"] = list(calls)
    if at is not None:
        entry["created_at"] = agy_time(at)
    return entry


def generic(step: int, content: str, at: float | None = None) -> dict[str, Any]:
    entry: dict[str, Any] = {"step_index": step, "source": "MODEL", "type": "GENERIC", "status": "DONE", "content": content}
    if at is not None:
        entry["created_at"] = agy_time(at)
    return entry


def subagent_ack(step: int, sub_id: str, at: float | None = 0, *, app_dir: str = "/app") -> dict[str, Any]:
    """The acknowledgement `invoke_subagent` returns: one per call, with the new conversation."""
    info = {
        "conversationId": sub_id,
        "logAbsoluteUri": f"file://{app_dir}/brain/{sub_id}/.system_generated/logs/transcript.jsonl",
        "workspaceUris": ["file:///work/repo"],
    }
    text = (
        "Created At: 2026-11-07T11:43:20+01:00\nCompleted At: 2026-11-07T11:44:08+01:00\n"
        f"Created the following subagent:\n{json.dumps(info, indent=2)}\n"
        "The subagent will send you a message when it has completed its task or requires guidance."
    )
    return generic(step, text, at)


def parent_message(step: int, sender: str, at: float | None = 0) -> dict[str, Any]:
    """The first entry of a subagent's transcript: the message of the conversation that launched it."""
    content = (
        "The following is a <SYSTEM_MESSAGE> not actually sent by the user. It is provided by the system as "
        f"important information to pay attention to.\n\n<SYSTEM_MESSAGE>\n[Message] timestamp=2026-11-07T10:44:08Z "
        f"sender={sender} priority=MESSAGE_PRIORITY_HIGH content=Review the hunks of your slot\n</SYSTEM_MESSAGE>"
    )
    entry: dict[str, Any] = {"step_index": step, "source": "SYSTEM", "type": "SYSTEM_MESSAGE", "status": "DONE", "content": content}
    if at is not None:
        entry["created_at"] = agy_time(at)
    return entry


def subagent_done(step: int, sub_id: str, at: float | None = None) -> dict[str, Any]:
    """The message a subagent sends its parent with `send_message`, as the parent's transcript shows it."""
    entry: dict[str, Any] = {
        "step_index": step,
        "source": "SYSTEM",
        "type": "SYSTEM_MESSAGE",
        "content": f"<SYSTEM_MESSAGE>\n[Message] timestamp=2026-11-07T10:45:12Z sender={sub_id} content=done\n</SYSTEM_MESSAGE>",
    }
    if at is not None:
        entry["created_at"] = agy_time(at)
    return entry


def ask_question(step: int) -> dict[str, Any]:
    return planner(step, agy_call("ask_question", questions=[]))


def launch_reviewers(first_step: int, *sub_ids: str, at: float = 0, role: str = "craftsman-reviewer") -> list[dict[str, Any]]:
    """One `PLANNER_RESPONSE` that launches a subagent per id, then the acknowledgements in call order."""
    calls = [invoke_call(role) for _ in sub_ids]
    return [
        planner(first_step, *calls, at=at),
        *(subagent_ack(first_step + 1 + index, sub_id, at) for index, sub_id in enumerate(sub_ids)),
    ]


class AgyApp:
    """A throwaway Antigravity app data directory (`brain/<conversation id>/.system_generated/...`)."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def conversation_dir(self, conversation_id: str) -> Path:
        return self.root / "brain" / conversation_id

    def transcript_path(self, conversation_id: str, *, full: bool = False) -> Path:
        name = "transcript_full.jsonl" if full else "transcript.jsonl"
        return self.conversation_dir(conversation_id) / ".system_generated" / "logs" / name

    def write_transcript(self, conversation_id: str, *entries: dict[str, Any], full: bool = False) -> Path:
        path = self.transcript_path(conversation_id, full=full)
        write_jsonl(path, *entries)
        return path

    def write_subagent(
        self,
        parent_id: str,
        sub_id: str,
        *entries: dict[str, Any],
        role: str = "craftsman-reviewer",
        state: str = "SUBAGENT_STATE_ALIVE",
        killed_at: float | None = None,
    ) -> None:
        """A subagent of `parent_id`: its transcript (opened by the parent's message) and the parent's registry file."""
        self.write_transcript(sub_id, parent_message(0, parent_id), *entries)
        registry = self.conversation_dir(parent_id) / ".system_generated" / "subagents" / f"{sub_id}.json"
        registry.parent.mkdir(parents=True, exist_ok=True)
        registry.write_text(
            json.dumps(
                {
                    "conversationId": sub_id,
                    "subagentDescriptor": {"typeName": "self", "role": role},
                    "state": state,
                    "spawnStepIndex": 1,
                }
            ),
            encoding="utf-8",
        )
        if killed_at is not None:
            os.utime(registry, (AGY_EPOCH.timestamp() + killed_at, AGY_EPOCH.timestamp() + killed_at))

    def payload(self, conversation_id: str, workspace: Path, **extra: Any) -> dict[str, Any]:
        """The common fields of an Antigravity hook payload."""
        return {
            "conversationId": conversation_id,
            "workspacePaths": [str(workspace)],
            "artifactDirectoryPath": str(self.conversation_dir(conversation_id)),
            "transcriptPath": str(self.transcript_path(conversation_id)),
            **extra,
        }


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
            ["git", *args], cwd=self.root, capture_output=True, encoding="utf-8", check=True, timeout=30
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
            encoding="utf-8",
            check=False,
            timeout=timeout,
        )
        if check and proc.returncode != 0:
            raise AssertionError(
                f"craftsman {' '.join(args)} exited {proc.returncode}:\n{proc.stdout}\n{proc.stderr}"
            )
        return proc

    def state(self) -> dict[str, Any]:
        """The stored gate state."""
        raw = json.loads((self.ledger_dir / "craftsman_state.json").read_text(encoding="utf-8"))
        assert isinstance(raw, dict)
        return raw

    def show(self) -> dict[str, Any]:
        payload = json.loads(self.cli("review", "show", "--json").stdout)
        assert isinstance(payload, dict)
        return payload

    def cover_all(self, *, again: bool = False) -> None:
        """Cover every open hunk of the latest round in one call; `again` also those already covered."""
        shown = self.show()
        missing = (
            [hunk["id"] for hunk in shown["hunks"] if not hunk["carried"]]
            if again
            else list(shown["coverage"]["missing"])
        )
        if missing:
            self.cli(
                "review", "cover", "--hunks", ",".join(missing), "--note", coverage_note(missing)
            )

    def record_full_evidence(self, deep: bool = False) -> None:
        """Everything the gate needs at the current code: a round, coverage and runs."""
        self.cli("review", "prepare", *(["--deep"] if deep else []))
        self.cover_all()
        self.cli("run", "--kind", "test", "--", sys.executable, "-c", "assert True; print('tests ok')")
        if deep:
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
    """A throwaway home folder and global git configuration; the rest of the
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
        # The distributions installed for this interpreter are the machine's too.
        legacy = mock.patch("craftsman.doctor.inspect_legacy_distribution", return_value=[])
        legacy.start()
        self.addCleanup(legacy.stop)

    def install(self, *args: str) -> CliResult:
        return run_main(install_main, "--repo-dir", str(REPO_ROOT), *args)

    def uninstall(self, *args: str) -> CliResult:
        return run_main(uninstall_main, *args)

    def git_global(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "config", "--global", *args],
            env=self.env,
            capture_output=True,
            encoding="utf-8",
            check=False,
            timeout=30,
        )

    def hooks_path(self) -> str | None:
        proc = self.git_global("--get", "core.hooksPath")
        return proc.stdout.strip() if proc.returncode == 0 else None

    def assert_ok(self, result: CliResult) -> None:
        self.assertEqual(result.code, 0, result.err)
