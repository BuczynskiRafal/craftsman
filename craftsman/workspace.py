"""The work tree a `craftsman review` or `craftsman run` command works in."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from craftsman.diffscan import resolve_base_sha, worktree_root
from craftsman.gate import GateState, load_state, state_path_for
from craftsman.ledger import IsolationEvidence, Ledger, ledger_path_for


@dataclass(frozen=True)
class Workspace:
    """A work tree, its active gate (None outside a `/craftsman` task) and its evidence ledger."""

    root: Path
    state: GateState | None
    ledger_path: Path

    @property
    def task(self) -> str:
        return self.state.task if self.state is not None else ""

    @property
    def isolation_evidence(self) -> IsolationEvidence:
        return self.state.isolation_evidence if self.state is not None else "none"


def open_workspace(cwd: str | None) -> Workspace:
    """Locate the work tree, its active gate and its ledger for `cwd` (default: the current one).

    Raises `GateStateError` when the gate state file is invalid.
    """
    root = worktree_root(cwd or os.getcwd())
    state = load_state(state_path_for(root))
    active = state if state is not None and state.active else None
    return Workspace(root=root, state=active, ledger_path=ledger_path_for(root))


def resolve_base(ws: Workspace, ledger: Ledger, explicit: str | None) -> str:
    """`--base`, else the latest round's base, else the active task's base, else HEAD."""
    ledger_base = ledger.rounds[-1].base_sha if ledger.rounds else None
    task_base = ws.state.base_sha if ws.state is not None else None
    return resolve_base_sha(ws.root, explicit or ledger_base or task_base)
