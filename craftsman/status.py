"""`craftsman status`: the work tree's gate, its cumulative diff and a summary of the evidence ledger."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from craftsman import __version__, hook_errors
from craftsman.diffscan import DiffScanError, DiffSnapshot, resolve_base_sha, snapshot, worktree_root
from craftsman.gate import GateState, GateStateError, load_state, state_path_for
from craftsman.gatecheck import hunt_slots
from craftsman.huntview import STALE_NOTE
from craftsman.ledger import backups_of, ledger_path_for, load, uncovered_hunks
from craftsman.records import VERDICTS, Ledger, LedgerError, NewerSchemaError, OlderSchemaError
from craftsman.usage import usage_json, usage_summary

LATEST_RUNS_SHOWN = 3


@dataclass(frozen=True)
class RunSummary:
    id: str
    kind: str
    command: str
    exit_code: int
    exit_text: str  # "exit 0", or "exit 0, 0 tests - not counted" for a run that ran too few tests


@dataclass(frozen=True)
class Coverage:
    covered: int
    total: int


@dataclass(frozen=True)
class Hunt:
    """Progress of a round judged by its hunt slots (an enforced round with a plan)."""

    done: int
    total: int


@dataclass(frozen=True)
class LedgerOverview:
    """A compact summary of the evidence ledger for `craftsman status`."""

    rounds: int
    deep: bool
    coverage: Coverage | None  # the latest round's hunk coverage; None for no round or a round judged by slots
    hunt: Hunt | None  # the latest round's slots done; None unless it is judged by slots
    findings: int
    verdicts: dict[str, int]
    resolved: int
    latest_runs: tuple[RunSummary, ...]
    agent_usage: dict[str, dict[str, Any]] = field(default_factory=dict)  # per role, when recorded
    stale: bool = False  # the latest round no longer matches the diff: its progress describes code that changed

    @classmethod
    def of(cls, ledger: Ledger, snap: DiffSnapshot | None = None) -> LedgerOverview:
        """The overview of `ledger`; with the current diff `snap` it also says whether the latest round is stale."""
        latest = ledger.rounds[-1] if ledger.rounds else None
        coverage, hunt = None, None
        slots = hunt_slots(ledger)
        if slots is not None:
            hunt = Hunt(sum(status.state == "done" for status in slots), len(slots))
        elif latest is not None:
            total = len(latest.open_hunk_ids)
            coverage = Coverage(total - len(uncovered_hunks(ledger, latest)), total)
        verdicts = dict.fromkeys((*VERDICTS, "none"), 0)
        for finding in ledger.findings:
            verdicts[finding.verdict or "none"] += 1
        return cls(
            rounds=len(ledger.rounds),
            deep=latest is not None and latest.deep,
            coverage=coverage,
            hunt=hunt,
            findings=len(ledger.findings),
            verdicts=verdicts,
            resolved=sum(finding.resolution is not None for finding in ledger.findings),
            latest_runs=tuple(
                RunSummary(run.id, run.kind, run.command, run.exit_code, run.exit_text)
                for run in ledger.runs[-LATEST_RUNS_SHOWN:]
            ),
            agent_usage=usage_json(ledger),
            stale=latest is not None and snap is not None and not snap.is_empty and latest.fingerprint != snap.fingerprint,
        )

    def __post_init__(self) -> None:
        progress_given = (self.coverage is not None) + (self.hunt is not None)
        if progress_given != (1 if self.rounds else 0):
            raise ValueError("a ledger with rounds has exactly one of coverage and hunt; one without has neither")

    def _progress(self) -> str:
        if self.hunt is not None:
            return f"hunt {self.hunt.done}/{self.hunt.total} slots done"
        assert self.coverage is not None  # guaranteed by __post_init__
        return f"coverage {self.coverage.covered}/{self.coverage.total} hunks"

    def _freshness(self) -> str:
        return f" ({STALE_NOTE})" if self.stale else ""

    def describe(self) -> str:
        if self.rounds == 0:
            return "no review round yet (`craftsman review prepare`)"
        verdicts = ", ".join(
            f"{'no verdict' if name == 'none' else name} {count}"
            for name, count in self.verdicts.items()
        )
        runs = ", ".join(f"{run.id} {run.kind} {run.exit_text}" for run in self.latest_runs)
        return (
            f"{self.rounds} round(s){', deep' if self.deep else ''}, {self._progress()}{self._freshness()}; findings {self.findings} ({verdicts}, "
            f"resolved {self.resolved}); latest runs: {runs or 'none'}{usage_summary(self.agent_usage)}"
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
    deep: bool
    stage: str
    isolation_evidence: str
    base_sha: str
    cwd: str
    diff: DiffSummary
    ledger: LedgerOverview | None
    ledger_error: str | None
    ledger_path: str
    # Why the evidence cannot be shown: "newer-schema", "older-schema" or "invalid"; None when it can.
    ledger_problem: str | None
    ledger_backups: tuple[str, ...]  # ledgers set aside by an upgrade, next to the ledger
    awaiting_user: bool = False  # `gate --await-user` parked the gate: the Stop hook lets the agent stop
    awaiting_since: str | None = None
    question: str | None = None
    hook_errors: dict[str, object] | None = None  # entries of the hook error log since the latest round, if any

    @property
    def ledger_broken(self) -> bool:
        """The ledger exists but cannot be used by this craftsman (an older one is merely set aside later)."""
        return self.ledger_problem in ("newer-schema", "invalid")

    @classmethod
    def of(
        cls, state_path: Path, state: GateState | None, root: Path, snap: DiffSnapshot
    ) -> StatusReport:
        ledger_path = ledger_path_for(root)
        overview: LedgerOverview | None = None
        ledger: Ledger | None = None
        ledger_error, problem = None, None
        try:  # status only looks: it never sets an older ledger aside
            ledger = load(ledger_path, set_aside=False)
            overview = LedgerOverview.of(ledger, snap)
        except NewerSchemaError as exc:
            ledger_error, problem = str(exc), "newer-schema"
        except OlderSchemaError as exc:
            ledger_error = (
                f"{ledger_path} was written by an older craftsman (schema {exc.schema}); "
                "the next review command that writes sets it aside and starts a new review"
            )
            problem = "older-schema"
        except LedgerError as exc:
            ledger_error, problem = _named_by_path(ledger_path, str(exc)), "invalid"
        deep = bool(state is not None and state.deep) or bool(overview is not None and overview.deep)
        return cls(
            state_file=str(state_path),
            active=state is not None and state.active,
            owner=state.owner.to_json() if state is not None else None,
            takeover_from=state.takeover_from.to_json() if state and state.takeover_from else None,
            started_at=state.started_at if state is not None else None,
            deep=deep,
            stage=state.stage.value if state is not None else "idle",
            isolation_evidence=state.isolation_evidence if state is not None else "none",
            base_sha=snap.base_sha,
            cwd=str(root),
            diff=DiffSummary(len(snap.files), snap.added, snap.deleted, snap.added + snap.deleted),
            ledger=overview,
            ledger_error=ledger_error,
            ledger_path=str(ledger_path),
            ledger_problem=problem,
            ledger_backups=tuple(str(backup) for backup in backups_of(ledger_path)),
            awaiting_user=state is not None and state.awaiting_user,
            awaiting_since=state.awaiting_since if state is not None else None,
            question=state.question if state is not None else None,
            hook_errors=hook_errors.summary_json(ledger_path, ledger) if ledger is not None else None,
        )


def _named_by_path(path: Path, message: str) -> str:
    """`message` led by `path` once: loader errors already carry the path or only the file name."""
    if str(path) in message:
        return message
    return f"{path}: {message.removeprefix(f'{path.name}: ')}"


def _status_base_and_scope(
    root: Path, state: GateState | None
) -> tuple[str | None, tuple[str, ...] | None]:
    if state is not None and state.active:
        return state.base_sha, None
    try:
        ledger = load(ledger_path_for(root), set_aside=False)
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
        return 1 if report.ledger_broken else 0
    if report.ledger is not None:
        evidence = report.ledger.describe()
    else:
        evidence = f"UNAVAILABLE - {report.ledger_error}"
    print(f"Craftsman Status (v{__version__})")
    print(f"  Active         : {report.active}")
    print(f"  Owner          : {state.owner.describe() if state is not None else 'none'}")
    print(f"  Deep           : {report.deep}")
    print(f"  Stage          : {report.stage}")
    print(f"  Base SHA       : {report.base_sha}")
    diff = report.diff
    print(
        f"  Cumulative Diff: {diff.changed_files} files, +{diff.added_lines}/-{diff.deleted_lines} lines"
    )
    print(f"  Evidence       : {evidence}")
    for backup in report.ledger_backups:
        print(f"  Set-aside ledger: {backup}")
    if report.awaiting_user:
        print(f"  Awaiting user  : since {report.awaiting_since or 'unknown'}")
        if report.question:
            print(f"  Question       : {report.question}")
    if report.hook_errors is not None:
        info = report.hook_errors
        print(f"  Hook errors    : {info['count']} since round {info['since_round']} was prepared (see {info['path']})")
    if report.ledger_broken:
        sys.stderr.write(f"craftsman status: error: the review ledger is unusable: {report.ledger_error}\n")
    return 1 if report.ledger_broken else 0
