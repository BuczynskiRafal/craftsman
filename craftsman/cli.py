#!/usr/bin/env python3
"""Unified CLI entrypoint for the Craftsman Agentic Engineering Framework."""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

from craftsman import __version__
from craftsman.diffscan import DiffScanError, DiffSnapshot, resolve_base_sha, snapshot, worktree_root
from craftsman.doctor import run_doctor
from craftsman.export import ExportError, Skill, default_repo_dir, iter_skills
from craftsman.export import main as export_main
from craftsman.gate import GateState, GateStateError, load_state, state_path_for, task_tier
from craftsman.gate import main as gate_main
from craftsman.guard import main as guard_main
from craftsman.hooks import main as hook_main
from craftsman.install import install_main
from craftsman.ledger import ledger_path_for, load, open_units, uncovered_by_lens
from craftsman.manifest import InstallError
from craftsman.playbooks import (
    CODE_REVIEW_SKILL,
    LENSES_FILENAME,
    expand_playbook_lenses,
    lens_playbooks,
)
from craftsman.records import VERDICTS, Ledger, LedgerError
from craftsman.review import main as review_main
from craftsman.runner import main as run_main
from craftsman.uninstall import uninstall_main

LATEST_RUNS_SHOWN = 3


# --- status ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class RunSummary:
    id: str
    kind: str
    command: str
    exit_code: int


@dataclass(frozen=True)
class Coverage:
    covered: int
    total: int


@dataclass(frozen=True)
class LedgerOverview:
    """A compact summary of the evidence ledger for `craftsman status`."""

    rounds: int
    tier: str | None
    coverage: Coverage | None
    findings: int
    verdicts: dict[str, int]
    resolved: int
    latest_runs: tuple[RunSummary, ...]

    @classmethod
    def of(cls, ledger: Ledger) -> LedgerOverview:
        latest = ledger.rounds[-1] if ledger.rounds else None
        coverage = None
        if latest is not None:
            total = sum(len(open_units(latest, lens)) for lens in latest.lenses)
            missing = sum(len(ids) for ids in uncovered_by_lens(ledger, latest).values())
            coverage = Coverage(total - missing, total)
        verdicts = {name: 0 for name in (*VERDICTS, "none")}
        for finding in ledger.findings:
            verdicts[finding.verdict or "none"] += 1
        return cls(
            rounds=len(ledger.rounds),
            tier=latest.tier if latest is not None else None,
            coverage=coverage,
            findings=len(ledger.findings),
            verdicts=verdicts,
            resolved=sum(finding.resolution is not None for finding in ledger.findings),
            latest_runs=tuple(
                RunSummary(run.id, run.kind, run.command, run.exit_code)
                for run in ledger.runs[-LATEST_RUNS_SHOWN:]
            ),
        )

    def describe(self) -> str:
        if self.coverage is None:
            return "no review round yet (`craftsman review prepare`)"
        verdicts = ", ".join(
            f"{'no verdict' if name == 'none' else name} {count}"
            for name, count in self.verdicts.items()
        )
        runs = ", ".join(f"{run.id} {run.kind} exit {run.exit_code}" for run in self.latest_runs)
        return (
            f"{self.rounds} round(s), tier {self.tier}, coverage "
            f"{self.coverage.covered}/{self.coverage.total}; findings {self.findings} ({verdicts}, "
            f"resolved {self.resolved}); latest runs: {runs or 'none'}"
        )


@dataclass(frozen=True)
class DiffSummary:
    changed_files: int
    added_lines: int
    deleted_lines: int
    total_changed_lines: int


@dataclass(frozen=True)
class StatusReport:
    state_file: str
    active: bool
    owner: dict[str, str | None] | None
    takeover_from: dict[str, str | None] | None
    started_at: str | None
    configured_tier: str
    effective_gear: str
    stage: str
    isolation_evidence: str
    base_sha: str
    cwd: str
    diff: DiffSummary
    ledger: LedgerOverview | None
    ledger_error: str | None

    @classmethod
    def of(
        cls, state_path: Path, state: GateState | None, root: Path, snap: DiffSnapshot
    ) -> StatusReport:
        try:
            overview: LedgerOverview | None = LedgerOverview.of(load(ledger_path_for(root)))
            ledger_error = None
        except LedgerError as exc:
            overview, ledger_error = None, str(exc)
        configured = state.tier if state is not None else "auto"
        return cls(
            state_file=str(state_path),
            active=state is not None and state.active,
            owner=state.owner.to_json() if state is not None else None,
            takeover_from=state.takeover_from.to_json() if state and state.takeover_from else None,
            started_at=state.started_at if state is not None else None,
            configured_tier=configured,
            effective_gear=task_tier(configured, snap),
            stage=state.stage.value if state is not None else "idle",
            isolation_evidence=state.isolation_evidence if state is not None else "none",
            base_sha=snap.base_sha,
            cwd=str(root),
            diff=DiffSummary(len(snap.files), snap.added, snap.deleted, snap.added + snap.deleted),
            ledger=overview,
            ledger_error=ledger_error,
        )


def _status_base_and_scope(
    root: Path, state: GateState | None
) -> tuple[str | None, tuple[str, ...] | None]:
    if state is not None and state.active:
        return state.base_sha, None
    try:
        ledger = load(ledger_path_for(root))
    except LedgerError:
        ledger = Ledger()
    if ledger.rounds:
        return ledger.rounds[-1].base_sha, ledger.current_scope
    return (state.base_sha if state is not None else None), None


def run_status(cwd: str | None, *, as_json: bool) -> int:
    """Show the work tree's gate, its cumulative diff and the evidence ledger."""
    try:
        root = worktree_root(cwd or os.getcwd())
        state_path = state_path_for(root)
        state = load_state(state_path)
        base_ref, scope = _status_base_and_scope(root, state)
        snap = snapshot(root, resolve_base_sha(root, base_ref), scope)
    except (DiffScanError, GateStateError) as exc:
        sys.stderr.write(f"craftsman status: error: {exc}\n")
        return 1
    report = StatusReport.of(state_path, state, root, snap)
    if as_json:
        sys.stdout.write(json.dumps(asdict(report), indent=2, ensure_ascii=False) + "\n")
        return 0
    if report.ledger is not None:
        evidence = report.ledger.describe()
    else:
        evidence = f"unavailable ({report.ledger_error})"
    print(f"Craftsman Status (v{__version__})")
    print(f"  Active         : {report.active}")
    print(f"  Owner          : {state.owner.describe() if state is not None else 'none'}")
    print(f"  Effective Gear : {report.effective_gear}")
    print(f"  Stage          : {report.stage}")
    print(f"  Base SHA       : {report.base_sha}")
    diff = report.diff
    print(
        f"  Cumulative Diff: {diff.changed_files} files, +{diff.added_lines}/-{diff.deleted_lines} lines"
    )
    print(f"  Evidence       : {evidence}")
    return 0


# --- skills ---------------------------------------------------------------------------------


def discover_skill_dirs(repo_dir: Path) -> dict[str, Skill]:
    """Bundled skills by name, each inside the skills folder (a symlink out of it is skipped)."""
    skills_root = (repo_dir / "skills").resolve()
    return {
        skill.name: skill
        for skill in iter_skills(repo_dir)
        if skill.directory.resolve().is_relative_to(skills_root)
    }


def _validate_skill_relative_file(skill_dir: Path, rel_file: str, skill_name: str) -> Path:
    if not rel_file or not rel_file.strip():
        raise ValueError(f"--file for skill {skill_name!r} must not be empty")
    rel_path = Path(rel_file)
    if (
        rel_path.is_absolute()
        or rel_file.startswith(("/", "\\"))
        or ".." in rel_path.parts
        or (len(rel_path.parts) > 0 and ":" in rel_path.parts[0])
    ):
        raise ValueError(
            f"--file {rel_file!r} must be a relative path inside skill {skill_name!r} (no '..' or absolute paths)"
        )
    cur = skill_dir
    for part in rel_path.parts:
        cur = cur / part
        if cur.is_symlink():
            resolved_link = cur.resolve()
            if not resolved_link.is_relative_to(skill_dir):
                raise ValueError(
                    f"--file {rel_file!r} escapes skill directory {skill_name!r} via symlink"
                )
    resolved = (skill_dir / rel_path).resolve()
    if resolved == skill_dir or not resolved.is_relative_to(skill_dir):
        raise ValueError(f"--file {rel_file!r} must resolve inside skill directory {skill_name!r}")
    if not resolved.is_file():
        raise FileNotFoundError(f"file {rel_file!r} not found in skill {skill_name!r}")
    return resolved


def _build_skill_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--repo-dir", type=Path, help="Path to the Craftsman repository or bundled assets root."
    )
    parser = argparse.ArgumentParser(
        prog="craftsman skill",
        description="Read bundled Craftsman skills and companion files without searching the disk.",
    )
    sub = parser.add_subparsers(dest="subcommand", required=True)
    show_p = sub.add_parser(
        "show",
        parents=[common],
        help="Print a bundled SKILL.md or companion file (such as lenses.md) to stdout.",
    )
    show_p.add_argument("name", help="Skill name (e.g. craftsman, code-review, simplify)")
    show_p.add_argument(
        "--file", dest="rel_file", help="Relative file inside the skill directory (e.g. lenses.md)"
    )
    show_p.add_argument(
        "--lenses",
        help=(
            "Comma-separated lens ids or reviewer groups (code-review only), "
            "e.g. intent,contracts or R3"
        ),
    )
    list_p = sub.add_parser(
        "list",
        parents=[common],
        help="List bundled skill names and their one-sentence descriptions.",
    )
    list_p.add_argument("--json", action="store_true", help="Output skill list as JSON.")
    path_p = sub.add_parser(
        "path", parents=[common], help="Print the resolved directory path of a bundled skill."
    )
    path_p.add_argument("name", help="Skill name (e.g. craftsman, code-review)")
    return parser


@dataclass(frozen=True)
class SkillEntry:
    name: str
    description: str
    path: str


def _first_sentence(text: str) -> str:
    return text.split(". ", 1)[0].rstrip(".") + "."


def _list_skills(known: dict[str, Skill], *, as_json: bool) -> int:
    entries = [
        SkillEntry(name, _first_sentence(skill.description), str(skill.directory.resolve()))
        for name, skill in known.items()
    ]
    if as_json:
        sys.stdout.write(
            json.dumps([asdict(entry) for entry in entries], indent=2, ensure_ascii=False) + "\n"
        )
        return 0
    width = max((len(entry.name) for entry in entries), default=12)
    for entry in entries:
        print(f"{entry.name:<{width}}  {entry.description}")
    return 0


def _read_skill_show_content(
    skill_dir: Path, skill_name: str, rel_file: str | None, raw_lenses: str | None
) -> str:
    if raw_lenses is None:
        target = (
            _validate_skill_relative_file(skill_dir, rel_file, skill_name)
            if rel_file is not None
            else (skill_dir / "SKILL.md").resolve()
        )
        return target.read_text(encoding="utf-8")
    if skill_name != CODE_REVIEW_SKILL:
        raise ValueError(
            f"--lenses is only supported for skill {CODE_REVIEW_SKILL!r} (got {skill_name!r})"
        )
    if rel_file is not None:
        raise ValueError("--lenses cannot be combined with --file")
    expanded = expand_playbook_lenses(raw_lenses)
    target = _validate_skill_relative_file(skill_dir, LENSES_FILENAME, skill_name)
    return lens_playbooks(target.read_text(encoding="utf-8"), expanded)


def skill_main(argv: list[str]) -> int:
    parser = _build_skill_parser()
    args = parser.parse_args(argv)
    try:
        known = discover_skill_dirs((args.repo_dir or default_repo_dir()).resolve())
    except ExportError as exc:
        sys.stderr.write(f"craftsman skill: error: {exc}\n")
        return 1

    if args.subcommand == "list":
        return _list_skills(known, as_json=bool(args.json))

    skill_name: str = args.name
    if skill_name not in known:
        available = ", ".join(sorted(known)) or "none"
        sys.stderr.write(
            f"craftsman skill: error: unknown skill {skill_name!r} (available: {available})\n"
        )
        return 2

    skill_dir = known[skill_name].directory.resolve()
    if args.subcommand == "path":
        print(str(skill_dir))
        return 0

    try:
        content = _read_skill_show_content(skill_dir, skill_name, args.rel_file, args.lenses)
    except (ValueError, OSError) as exc:
        sys.stderr.write(f"craftsman skill: error: {exc}\n")
        return 2

    sys.stdout.write(content if content.endswith("\n") else content + "\n")
    return 0


# --- entry point ----------------------------------------------------------------------------


def _tolerate_unencodable_output() -> None:
    """Print `?` for characters the console cannot encode (Windows code pages) instead of crashing."""
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(errors="replace")


DOCTOR_HELP = "Audit skill manifests, core scripts, Git config, drift, and installed agent targets."
STATUS_HELP = "Show the work tree's quality gate, cumulative diff and review evidence."


def doctor_main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="craftsman doctor", description=DOCTOR_HELP)
    parser.add_argument("--repo-dir", type=Path, help="Path to the Craftsman repository root.")
    parser.add_argument("--json", action="store_true", help="Output diagnostic report as JSON.")
    parser.add_argument(
        "--workspace", type=Path, help="Also check the Copilot agents and skills of this project."
    )
    args = parser.parse_args(list(argv))
    try:
        return run_doctor(
            (args.repo_dir or default_repo_dir()).resolve(),
            as_json=args.json,
            workspace=args.workspace.resolve() if args.workspace else None,
        )
    except (ExportError, InstallError) as exc:
        sys.stderr.write(f"craftsman doctor: error: {exc}\n")
        return 1


def status_main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="craftsman status", description=STATUS_HELP)
    parser.add_argument("--cwd", help="A directory inside the work tree (default: the current one).")
    parser.add_argument("--json", action="store_true", help="Output status report as JSON.")
    args = parser.parse_args(list(argv))
    return run_status(args.cwd, as_json=args.json)


COMMANDS: dict[str, tuple[Callable[[list[str]], int], str]] = {
    "doctor": (doctor_main, DOCTOR_HELP),
    "status": (status_main, STATUS_HELP),
    "install": (
        install_main,
        "Install Craftsman skills, rules, agents, hooks, and git guards across platforms.",
    ),
    "uninstall": (uninstall_main, "Uninstall the Craftsman files recorded in install manifests."),
    "export": (
        export_main,
        "Export skills and rules to Claude Code, GitHub Copilot, Codex, and Cursor.",
    ),
    "gate": (gate_main, "Open, update and close the quality gate of the current work tree."),
    "guard": (guard_main, "Invoke the Git Guard PreToolUse or native Git hook engine."),
    "hook": (hook_main, "Lifecycle hook for VS Code Copilot, the Copilot CLI and Claude Code."),
    "review": (review_main, "Prepare review rounds and record findings, coverage and verdicts."),
    "run": (run_main, "Run a command and record its exit code in the evidence ledger."),
    "skill": (
        skill_main,
        "Read bundled Craftsman skills and companion files without searching the disk.",
    ),
}


def main(argv: list[str] | None = None) -> int:
    _tolerate_unencodable_output()
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in COMMANDS:
        handler, _ = COMMANDS[args[0]]
        return handler(args[1:])
    parser = argparse.ArgumentParser(
        prog="craftsman", description="Craftsman: Universal Agentic Engineering & Quality Gate CLI."
    )
    parser.add_argument("--version", action="version", version=f"craftsman {__version__}")
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")
    for name, (_, help_text) in COMMANDS.items():
        commands.add_parser(name, help=help_text)
    parser.parse_args(args)  # prints --version, help or the error for an unknown command
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
