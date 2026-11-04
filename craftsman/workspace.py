"""The work tree a `craftsman review` or `craftsman run` command works in."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from craftsman.diffscan import DiffSnapshot, resolve_base_sha, snapshot, worktree_root
from craftsman.files import write_text_atomic
from craftsman.gate import GateOwner, GateState, cli_caller, load_state, state_path_for
from craftsman.ledger import ledger_path_for, load, stable_hunk_ids
from craftsman.records import IsolationEvidence, Ledger, format_timestamp

HOOKS_MARKER = "review_hooks.json"


@dataclass(frozen=True)
class ReviewMarker:
    """What the prompt hook noted about a session that invoked the review command.

    A review-only run has no gate state to say that hooks serve its session; this file does. While
    `guarding`, the Stop hook keeps the session working until the review-only outcome exists;
    `forced` and `best` count those reminders like the gate's continuations.
    """

    platform: str
    session_id: str
    at: str
    guarding: bool = True
    forced: int = 0
    best: int | None = None

    def is_of(self, caller: GateOwner) -> bool:
        return self.platform == caller.platform and self.session_id == caller.session_id


def read_marker(ledger_path: Path) -> ReviewMarker | None:
    try:
        raw = json.loads((ledger_path.parent / HOOKS_MARKER).read_text(encoding="utf-8"))
        best = raw.get("best")
        return ReviewMarker(
            str(raw["platform"]),
            str(raw["session_id"]),
            str(raw["at"]),
            guarding=raw.get("guarding") is True,
            forced=int(raw.get("forced", 0)),
            best=best if isinstance(best, int) else None,
        )
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def write_marker(ledger_path: Path, marker: ReviewMarker) -> None:
    try:
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomic(ledger_path.parent / HOOKS_MARKER, json.dumps(asdict(marker)) + "\n")
    except OSError:
        pass  # without the marker a review-only run is only advisory about isolation


def mark_hooks_active(ledger_path: Path, caller: GateOwner) -> None:
    """Note that the prompt hook saw `caller`'s session invoke the review command."""
    if caller.session_id is None:
        return
    now = format_timestamp(datetime.now(timezone.utc))
    write_marker(ledger_path, ReviewMarker(caller.platform, caller.session_id, now))


def end_review_guard(ledger_path: Path, caller: GateOwner | None = None) -> None:
    """Stop holding the session (of `caller`, if given) to a finished or abandoned review."""
    marker = read_marker(ledger_path)
    if marker is not None and marker.guarding and (caller is None or marker.is_of(caller)):
        write_marker(ledger_path, replace(marker, guarding=False, forced=0, best=None))


@dataclass(frozen=True)
class Workspace:
    """A work tree, its active gate (None outside a `/craftsman` task) and its evidence ledger."""

    root: Path
    state: GateState | None
    ledger_path: Path

    @property
    def task(self) -> str:
        return self.state.task if self.state is not None else ""

    def isolation_evidence(self, ledger: Ledger) -> IsolationEvidence:
        """Where proof of an isolated reviewer can come from for this review.

        A gate knows what its platform proved. Without a gate (review-only), hooks serve the
        session when a subagent event is in the ledger or when the prompt hook saw this session
        invoke the review command.
        """
        if self.state is not None:
            return self.state.required_isolation
        return "hooks" if ledger.agents or self._hooks_saw_this_session() else "none"

    def _hooks_saw_this_session(self) -> bool:
        caller = cli_caller(None, os.environ)
        marker = read_marker(self.ledger_path)
        return marker is not None and caller.session_id is not None and marker.is_of(caller)

    @property
    def hooks_marker(self) -> Path:
        return self.ledger_path.parent / HOOKS_MARKER

    def base_sha(self, ledger: Ledger, explicit: str | None = None) -> str:
        """`explicit`, else the latest round's base, else the active task's base, else HEAD."""
        ledger_base = ledger.rounds[-1].base_sha if ledger.rounds else None
        task_base = self.state.base_sha if self.state is not None else None
        return resolve_base_sha(self.root, explicit or ledger_base or task_base)

    def snapshot(self, ledger: Ledger | None = None) -> DiffSnapshot:
        """The diff since the base the review (or else the active task) is measured against."""
        current = load(self.ledger_path) if ledger is None else ledger
        scope = None if self.state is not None else current.current_scope
        return stable_hunk_ids(current, snapshot(self.root, self.base_sha(current), scope))


def open_workspace(cwd: str | None) -> Workspace:
    """Locate the work tree, its active gate and its ledger for `cwd` (default: the current one).

    Raises `GateStateError` when the gate state file is invalid.
    """
    root = worktree_root(cwd or os.getcwd())
    state = load_state(state_path_for(root))
    active = state if state is not None and state.active else None
    return Workspace(root=root, state=active, ledger_path=ledger_path_for(root))
