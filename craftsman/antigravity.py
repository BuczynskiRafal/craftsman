"""The Antigravity Stop hook: `scripts/craftsman-stop-gate.py`, wired by `hooks.json`.

Antigravity has no prompt or subagent hooks, so this hook reads the session transcript instead:
- a `/craftsman` or `ultracraft` request opens a gate when the agent did not open one itself;
- `craftsman-reviewer` subagents that finished since the request are recorded in the evidence
  ledger, which is the proof of reviewer isolation for Antigravity sessions;
- an unanswered `ask_question` lets the agent stop to wait for the user.
Then the gate's shared stop decision applies (`craftsman.gate.decide_stop`).

The hook reads a JSON object on stdin with `terminationReason`, `fullyIdle`,
`artifactDirectoryPath` (the session directory, `<appDataDir>/brain/<conversation id>`) and
`workspacePaths`, and answers `{"decision": "continue" | "allow", "reason": ...}`. When the gate
cannot be evaluated, it lets the agent stop and says why on stderr.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from craftsman.diffscan import DiffScanError, resolve_base_sha, snapshot, worktree_root
from craftsman.gate import (
    GateOwner,
    GateState,
    GateStateError,
    GateTier,
    archive_ledger_for_new_base,
    decide_stop,
    state_path_for,
    update_state,
)
from craftsman.ledger import (
    REVIEWER_AGENT,
    AgentEvent,
    Ledger,
    LedgerError,
    add_agent_event,
    format_timestamp,
    ledger_path_for,
    mutate,
)
from craftsman.transcript import (
    Entry,
    TranscriptError,
    finished_reviewers,
    is_waiting_on_question,
    latest_user_input,
    read_transcript_entries,
    step_of,
)
from craftsman.trigger import detect_trigger, task_from_prompt

PLATFORM: Literal["antigravity"] = "antigravity"
TRANSCRIPT = Path(".system_generated") / "logs" / "transcript.jsonl"


class PayloadError(ValueError):
    """The Stop payload lacks what the hook needs to find the session and its work tree."""


@dataclass(frozen=True)
class StopPayload:
    session_dir: Path
    workspace: Path

    @classmethod
    def parse(cls, payload: Mapping[str, Any]) -> StopPayload:
        session_dir = payload.get("artifactDirectoryPath")
        workspaces = payload.get("workspacePaths")
        if not isinstance(session_dir, str) or not session_dir.strip():
            raise PayloadError("the Stop payload has no artifactDirectoryPath")
        if not isinstance(workspaces, list) or not workspaces or not isinstance(workspaces[0], str):
            raise PayloadError("the Stop payload has no workspacePaths")
        return cls(Path(session_dir).expanduser(), Path(workspaces[0]).expanduser())

    @property
    def caller(self) -> GateOwner:
        return GateOwner(PLATFORM, self.session_dir.name)


def session_id(payload: Mapping[str, Any]) -> str | None:
    """The conversation id of an Antigravity hook payload: the name of its session directory."""
    session_dir = payload.get("artifactDirectoryPath")
    if isinstance(session_dir, str) and session_dir.strip():
        return Path(session_dir).expanduser().name
    return None


def is_final_stop(payload: Mapping[str, Any]) -> bool:
    """True when the agent ends its turn, rather than pausing between tool calls."""
    return (
        payload.get("terminationReason", "NO_TOOL_CALL") == "NO_TOOL_CALL"
        and payload.get("fullyIdle") is not False
    )


def on_stop(stop: StopPayload) -> str | None:
    """The reason to keep the agent working, or None to let it stop."""
    root = worktree_root(stop.workspace)
    entries = read_transcript_entries(stop.session_dir / TRANSCRIPT)

    def decide(state: GateState | None) -> tuple[GateState | None, str | None]:
        state = _opened_by_request(state, root, entries, stop.caller)
        if state is None or not state.active or not state.owner.admits(stop.caller):
            return state, None
        state = _claimed(state, entries, stop.caller)
        _record_finished_reviewers(root, stop.session_dir, entries, state.request_step or 0)
        if is_waiting_on_question(entries):
            return state, None
        decision = decide_stop(state, root)
        if decision.notice is not None:
            sys.stderr.write(decision.notice + "\n")
        return decision.state, decision.block_reason

    return update_state(state_path_for(root), decide)


def _opened_by_request(
    state: GateState | None, root: Path, entries: Sequence[Entry], caller: GateOwner
) -> GateState | None:
    """The gate, opened for a `/craftsman` request the agent did not open a gate for."""
    if state is not None and state.active:
        return state
    if state is not None and state.owner.admits(caller):
        if state.request_step is None:
            # Closed with `craftsman gate --complete` before any stop: the request was handled.
            latest = latest_user_input(entries)
            return state if latest is None else replace(state, request_step=latest[0])
        handled_step = state.request_step
    else:
        handled_step = 0
    request = _latest_trigger(entries, handled_step)
    if request is None:
        return state
    step, tier, prompt = request
    base_sha = resolve_base_sha(root, None)
    if snapshot(root, base_sha).is_empty:
        return state  # nothing changed yet, so there is nothing to gate
    archive_ledger_for_new_base(root, base_sha)
    return GateState.open(caller, base_sha, tier, task_from_prompt(prompt), request_step=step)


def _latest_trigger(entries: Sequence[Entry], after_step: int) -> tuple[int, GateTier, str] | None:
    """The step, tier and text of the latest user input after `after_step` that invokes Craftsman."""
    for position in range(len(entries), 0, -1):
        entry = entries[position - 1]
        step = step_of(entry, position)
        if step <= after_step:
            return None
        if entry.get("type") != "USER_INPUT":
            continue
        prompt = str(entry.get("content") or "")
        tier = detect_trigger(prompt)
        if tier is not None:
            return step, tier, prompt
    return None


def _claimed(state: GateState, entries: Sequence[Entry], caller: GateOwner) -> GateState:
    """The gate as this session's stop hook serves it: owned, transcript-isolated, anchored."""
    request_step = state.request_step
    if request_step is None:
        latest = latest_user_input(entries)
        request_step = latest[0] if latest is not None else 0
    return replace(
        state,
        owner=state.owner.claimed_by(caller),
        isolation_evidence="transcript",
        request_step=request_step,
    )


def _record_finished_reviewers(
    root: Path, session_dir: Path, entries: Sequence[Entry], after_step: int
) -> None:
    """Record each finished `craftsman-reviewer` subagent once, at the time its own run ended.

    A subagent's session directory is a sibling of its parent's, named by its conversation id;
    a subagent without one left no trace of having run, so it is not recorded.
    """
    events = [
        event
        for sub_id in finished_reviewers(entries, after_step)
        if (event := _reviewer_stop(session_dir.parent / sub_id, sub_id)) is not None
    ]
    if events:
        mutate(ledger_path_for(root), lambda ledger: (_with_new_events(ledger, events), None))


def _reviewer_stop(subagent_dir: Path, sub_id: str) -> AgentEvent | None:
    try:
        finished_at = (subagent_dir / TRANSCRIPT).stat().st_mtime
    except FileNotFoundError:
        return None
    return AgentEvent(
        platform=PLATFORM,
        event="stop",
        agent=REVIEWER_AGENT,
        agent_id=sub_id,
        at=format_timestamp(datetime.fromtimestamp(finished_at, timezone.utc)),
    )


def _with_new_events(ledger: Ledger, events: Sequence[AgentEvent]) -> Ledger:
    recorded = {(event.platform, event.agent_id) for event in ledger.agents if event.event == "stop"}
    for event in events:
        if (event.platform, event.agent_id) not in recorded:
            ledger = add_agent_event(ledger, event)
    return ledger


def answer(payload: object) -> dict[str, str]:
    """The hook's answer to one Stop payload."""
    if not isinstance(payload, dict):
        return _allow_after_error("the Stop payload is not a JSON object")
    if not is_final_stop(payload):
        return {"decision": "allow"}
    try:
        reason = on_stop(StopPayload.parse(payload))
    except (PayloadError, DiffScanError, LedgerError, GateStateError, TranscriptError, OSError) as exc:
        return _allow_after_error(str(exc))
    return {"decision": "continue", "reason": reason} if reason is not None else {"decision": "allow"}


def _allow_after_error(problem: str) -> dict[str, str]:
    sys.stderr.write(
        f"[craftsman-stop-gate] The quality gate was not checked: {problem}. "
        "Run `craftsman review check` yourself.\n"
    )
    return {"decision": "allow"}


def main() -> int:
    try:
        out = answer(json.loads(sys.stdin.read()))
    except (OSError, ValueError) as exc:
        out = _allow_after_error(f"unreadable hook input ({exc})")
    sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
