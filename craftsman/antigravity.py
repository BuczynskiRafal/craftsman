"""The Antigravity Stop hook: `scripts/craftsman-stop-gate.py`, wired by `hooks.json`.

Antigravity has no prompt or subagent hooks, so this hook reads the transcripts instead. At every final stop:
- a subagent conversation that claimed a slot of the hunt plan and left it unfinished is sent back once (a
  `nudge` event in the ledger); otherwise it is let go, and its run is recorded (`agy_sync`);
- in a main conversation the runs of the subagents it launched are recorded first (`agy_sync.sync_from_stop`), so
  the gate and the review guard see who is working and what they covered (the waves that are due are stored by
  `review_guard.gate_stop_inputs` and `decide_review_stop`, under the ledger lock);
- a `/craftsman` or `ultracraft` request opens a gate when the agent did not open one itself, and the gate's
  shared stop decision applies (`craftsman.gate.decide_stop`) to the conversation that owns it;
- any other main conversation is held to a review it began: a `/craft-review` request in the transcript arms
  the review guard (`sync_marker_from_user_input`) and `review_guard.serve_review_stop` decides;
- an unanswered `ask_question` lets the agent stop to wait for the user.

The hook reads a JSON object on stdin with `terminationReason`, `fullyIdle`, `conversationId`,
`artifactDirectoryPath` (`<app data dir>/brain/<conversation id>`), `transcriptPath` and `workspacePaths`, and
answers `{"decision": "continue" | "allow", "reason": ...}`. When the gate cannot be evaluated, it lets the agent
stop and says why on stderr and in the hook error log. `session_id` and `app_dir` are the one reading of the
conversation and the app data directory of a hook payload; the PreToolUse guard (`craftsman.scope`) uses them too.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from craftsman import hook_errors
from craftsman.agy_sync import (
    AGENT_OF_ROLE,
    PLATFORM,
    apply_runs,
    events_of,
    observe_runs,
    sync_from_stop,
)
from craftsman.diffscan import DiffScanError, GitTimeoutError, resolve_base_sha, snapshot, worktree_root
from craftsman.gate import (
    GateOwner,
    GateState,
    GateStateError,
    archive_ledger_for_new_base,
    decide_stop,
    is_continuation_prompt,
    state_path_for,
    update_state,
)
from craftsman.gatecheck import unfinished_slots
from craftsman.huntview import hunk_ranges
from craftsman.ledger import add_agent_event, ledger_path_for, mutate
from craftsman.paths import user_home
from craftsman.records import AgentEvent, Ledger, LedgerError, format_timestamp, parse_timestamp
from craftsman.review_guard import gate_stop_inputs, serve_review_stop
from craftsman.streams import configure_std_streams
from craftsman.transcript import (
    Entry,
    TranscriptError,
    conversation_transcript,
    is_waiting_on_question,
    latest_user_input,
    latest_user_input_text,
    parent_conversation,
    read_transcript_entries,
    request_body,
    review_request,
    step_of,
    subagent_role,
)
from craftsman.trigger import detect_trigger, task_from_prompt
from craftsman.workspace import arm_marker, end_review_guard, read_marker

__all__ = ["PLATFORM", "app_dir", "session_id", "sync_marker_from_user_input"]

APP_DIR_VARIABLE = "ANTIGRAVITY_APP_DATA_DIR"
DEFAULT_APP_DIR = (".gemini", "antigravity-cli")
BRAIN = "brain"
HOOK_EVENT = "antigravity-stop"


class PayloadError(ValueError):
    """The Stop payload lacks what the hook needs to find the session and its work tree."""


def _text(payload: Mapping[str, Any], key: str) -> str | None:
    value = payload.get(key)
    return value.strip() if isinstance(value, str) and value.strip() else None


def session_id(payload: Mapping[str, Any]) -> str | None:
    """The conversation id of an Antigravity hook payload: its `conversationId`, else the name of its session directory."""
    conversation = _text(payload, "conversationId")
    if conversation is not None:
        return conversation
    session_dir = _text(payload, "artifactDirectoryPath")
    return Path(session_dir).expanduser().name if session_dir is not None else None


def app_dir(payload: Mapping[str, Any], env: Mapping[str, str] | None = None) -> Path:
    """The Antigravity app data directory of a hook payload.

    The grandparent of `artifactDirectoryPath` when that is `<app data dir>/brain/<conversation id>`; else
    `ANTIGRAVITY_APP_DATA_DIR` in `env` (default: the process environment); else `~/.gemini/antigravity-cli`.
    """
    session_dir = _text(payload, "artifactDirectoryPath")
    if session_dir is not None:
        path = Path(session_dir).expanduser()
        if path.parent.name == BRAIN:
            return path.parent.parent
    values = os.environ if env is None else env
    configured = values.get(APP_DIR_VARIABLE, "").strip()
    return Path(configured).expanduser() if configured else user_home().joinpath(*DEFAULT_APP_DIR)


@dataclass(frozen=True)
class StopPayload:
    conversation_id: str
    app_dir: Path
    workspace: Path
    transcript: Path | None = None  # the `transcriptPath` the payload gave

    @classmethod
    def parse(cls, payload: Mapping[str, Any], env: Mapping[str, str] | None = None) -> StopPayload:
        conversation = session_id(payload)
        workspaces = payload.get("workspacePaths")
        if conversation is None:
            raise PayloadError("the Stop payload has no conversationId or artifactDirectoryPath")
        if not isinstance(workspaces, list) or not workspaces or not isinstance(workspaces[0], str):
            raise PayloadError("the Stop payload has no workspacePaths")
        given = _text(payload, "transcriptPath")
        return cls(
            conversation,
            app_dir(payload, env),
            Path(workspaces[0]).expanduser(),
            Path(given).expanduser() if given is not None else None,
        )

    @property
    def caller(self) -> GateOwner:
        return GateOwner(PLATFORM, self.conversation_id)

    def entries(self) -> list[Entry]:
        """The transcript of this conversation (none yet is no entries)."""
        return read_transcript_entries(self.transcript or conversation_transcript(self.app_dir, self.conversation_id))


def is_final_stop(payload: Mapping[str, Any]) -> bool:
    """True when the agent ends its turn, rather than pausing between tool calls."""
    reason, idle = payload.get("terminationReason"), payload.get("fullyIdle")
    if not isinstance(reason, str) or not isinstance(idle, bool):
        raise PayloadError("the Stop payload needs `terminationReason` and `fullyIdle`")
    return reason == "NO_TOOL_CALL" and idle


def _now() -> datetime:
    return datetime.now(timezone.utc)


def on_stop(stop: StopPayload, now: datetime | None = None) -> str | None:
    """The reason to keep the agent working, or None to let it stop."""
    moment = now or _now()
    root = worktree_root(stop.workspace)
    parent = parent_conversation(stop.app_dir, stop.conversation_id, path=stop.transcript)
    if parent is not None:
        return _subagent_stop(stop, root, parent, moment)
    entries = stop.entries()
    sync_from_stop(root, stop.app_dir, stop.conversation_id, stop.transcript)

    def decide(state: GateState | None) -> tuple[GateState | None, str | None]:
        state = _opened_by_request(state, root, entries, stop.caller)
        if state is not None and state.active and state.owner.admits(stop.caller):
            return _serve_gate(_claimed(state, entries, stop.caller), root, entries, stop, moment)
        return state, _serve_review_guard(root, entries, stop, moment)

    return update_state(state_path_for(root), decide)


# --- a main conversation: the gate it owns, or the review it began --------------------------------------


def _serve_gate(
    state: GateState, root: Path, entries: Sequence[Entry], stop: StopPayload, now: datetime
) -> tuple[GateState, str | None]:
    if is_waiting_on_question(entries):
        return state, None
    ledger_path = ledger_path_for(root)
    inputs = None if state.awaiting_user else gate_stop_inputs(ledger_path, now)
    decision = decide_stop(
        state,
        root,
        running=inputs.running if inputs is not None else (),
        evidence=inputs.evidence if inputs is not None else None,
    )
    if decision.notice is not None:
        sys.stderr.write(decision.notice + "\n")
    if decision.block_reason is None and not decision.state.active:
        end_review_guard(ledger_path, stop.caller)  # the gate is over (done or given up): so is a guard it outlived
    return decision.state, decision.block_reason


def _serve_review_guard(root: Path, entries: Sequence[Entry], stop: StopPayload, now: datetime) -> str | None:
    """The review guard for a conversation that holds no gate: a `/craft-review` request arms it, `serve_review_stop` decides."""
    if is_waiting_on_question(entries):
        return None
    sync_marker_from_user_input(root, stop.app_dir, stop.conversation_id, stop.caller, now, entries=entries)
    decision = serve_review_stop(root, ledger_path_for(root), stop.caller, now)
    if decision.notice is not None:
        sys.stderr.write(decision.notice + "\n")
    return decision.block


def sync_marker_from_user_input(
    root: Path,
    app_dir: Path,
    conversation_id: str,
    caller: GateOwner,
    now: datetime,
    *,
    transcript: Path | None = None,
    entries: Sequence[Entry] | None = None,
) -> None:
    """Arm or end the review guard the way the prompt hook does on platforms that have one (Antigravity has none).

    Reads the latest `USER_INPUT` of the conversation (`entries`, else its transcript):
    - a `/craft-review` request, when there is no marker or the request is newer than the marker's `at`: a fresh
      review (`arm_marker(fresh=True)`), with `at` = the time of the request, so the same request never arms twice
      (whole seconds on both sides), and its `deep` flag and task;
    - any other input that is newer than the marker's `at` and its last touch, while the marker guards this
      conversation: the user moved on, so the guard ends (a continuation prompt is no such input);
    - otherwise nothing. A later `craftsman review` command re-arms like on Claude (`review_guard.arm_for_review_command`).
    """
    ledger_path = ledger_path_for(root)
    if entries is None:
        entries = read_transcript_entries(transcript or conversation_transcript(app_dir, conversation_id))
    latest = latest_user_input_text(entries)
    if latest is None:
        return
    _, stamp, text = latest
    if stamp is None:
        return
    asked_at = parse_timestamp(stamp)
    marker = read_marker(ledger_path)
    request = review_request(text)
    if request is not None:
        if marker is None or asked_at > parse_timestamp(marker.at):
            deep, task = request
            arm_marker(ledger_path, caller, asked_at, fresh=True, deep=deep, task=task)
        return
    if marker is None or not marker.guarding or not marker.is_of(caller) or is_continuation_prompt(request_body(text)):
        return
    if asked_at > marker.last_seen():  # newer than the start of the review and than the hooks' last touch
        end_review_guard(ledger_path, caller)


def _opened_by_request(
    state: GateState | None, root: Path, entries: Sequence[Entry], caller: GateOwner
) -> GateState | None:
    """The gate, opened for a `/craftsman` request the agent did not open a gate for."""
    if state is not None and state.active:
        return _raised_by_request(state, entries)
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
    step, deep, prompt = request
    base_sha = resolve_base_sha(root, None)
    if snapshot(root, base_sha).is_empty:
        return state  # nothing changed yet, so there is nothing to gate
    archive_ledger_for_new_base(root, base_sha)
    return GateState.open(caller, base_sha, deep, task_from_prompt(prompt), request_step=step)


def _raised_by_request(state: GateState, entries: Sequence[Entry]) -> GateState:
    """An active gate, made deep when a later user input asked for `/craftsman --deep` or `ultracraft`."""
    if state.deep or state.request_step is None:
        return state
    for position, entry in enumerate(entries, 1):
        if step_of(entry, position) <= state.request_step or entry.get("type") != "USER_INPUT":
            continue
        if detect_trigger(str(entry.get("content") or "")) is True:
            return replace(state, deep=True)
    return state


def _latest_trigger(entries: Sequence[Entry], after_step: int) -> tuple[int, bool, str] | None:
    """The step, deep flag and text of the latest user input after `after_step` that invokes Craftsman."""
    for position in range(len(entries), 0, -1):
        entry = entries[position - 1]
        step = step_of(entry, position)
        if step <= after_step:
            return None
        if entry.get("type") != "USER_INPUT":
            continue
        prompt = str(entry.get("content") or "")
        deep = detect_trigger(prompt)
        if deep is not None:
            return step, deep, prompt
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


# --- a subagent conversation ----------------------------------------------------------------------------


def _subagent_stop(stop: StopPayload, root: Path, parent: str, now: datetime) -> str | None:
    """The stop of a subagent conversation: sent back once when it leaves a slot it claimed unfinished, else recorded.

    The decision is made on the ledger as if the subagent had stopped (its run synced from its transcript);
    that ledger is stored only when the subagent may stop. The nudge-once memory is the `nudge` event.
    """
    runs = observe_runs(stop.app_dir, parent)
    mine = next((run for run in runs if run.launch.conversation_id == stop.conversation_id), None)
    role = mine.launch.role if mine is not None else subagent_role(stop.app_dir, stop.conversation_id, parent)

    def judge(ledger: Ledger) -> tuple[Ledger, str | None]:
        candidate = apply_runs(ledger, runs)
        problem = None
        if mine is not None and role == "reviewer" and not _was_sent_back(ledger, stop.conversation_id):
            problem = _unfinished_slot(candidate, events_of(ledger, mine), now)
        if problem is None or mine is None:
            return candidate, None
        nudge = AgentEvent(
            PLATFORM,
            "nudge",
            AGENT_OF_ROLE[mine.launch.role],
            stop.conversation_id,
            format_timestamp(now),
            last_step=mine.state.last_step,
        )
        return add_agent_event(ledger, nudge), problem

    return mutate(ledger_path_for(root), judge)


def _was_sent_back(ledger: Ledger, conversation_id: str) -> bool:
    return any(
        event.event == "nudge" and event.platform == PLATFORM and event.agent_id == conversation_id
        for event in ledger.agents
    )


def _unfinished_slot(candidate: Ledger, run_events: Sequence[AgentEvent], now: datetime) -> str | None:
    """What the subagent still owes on a slot it claimed in the latest round, None when it owes nothing.

    `run_events` are the events its transcript gives (`agy_sync.events_of`). This is its final Stop, so when the
    transcript does not show the end of the turn yet (the last entry is not written when the hook runs) it is
    judged as stopping `now`; that stop is used for the judgement only, the transcript decides what is stored.
    """
    start = run_events[0]
    stop = next((event for event in run_events if event.event == "stop"), None)
    if stop is None:
        stop = replace(start, event="stop", at=format_timestamp(max(now, parse_timestamp(start.at))))
        candidate = add_agent_event(candidate, stop)
    if not candidate.rounds:
        return None
    latest = candidate.rounds[-1]
    mine = {claim.slot for claim in candidate.claims if claim.round == latest.number and claim.by == f"{PLATFORM}:{start.agent_id}"}
    for slot_id, missing in unfinished_slots(candidate, latest, parse_timestamp(stop.at), now):
        if slot_id in mine:
            hunks = hunk_ranges(missing)
            return (
                f"Your slot {slot_id} is not finished: cover hunks {hunks} with "
                f'`craftsman review cover --hunter {slot_id} --hunks {hunks} --note "..."`, '
                "then send your 2-3 line summary to your parent with send_message."
            )
    return None


# --- the answer ------------------------------------------------------------------------------------------


def answer(payload: object, env: Mapping[str, str] | None = None, now: datetime | None = None) -> dict[str, str]:
    """The hook's answer to one Stop payload (`now`: the clock, for tests)."""
    if not isinstance(payload, dict):
        return _allow_after_error("the Stop payload is not a JSON object")
    try:
        if not is_final_stop(payload):
            return {"decision": "allow"}
        stop = StopPayload.parse(payload, env)
    except PayloadError as exc:
        return _allow_after_error(str(exc))
    with hook_errors.hook_run(HOOK_EVENT, stop.workspace) as log:
        try:
            reason = on_stop(stop, now)
        except (DiffScanError, LedgerError, GateStateError, TranscriptError, OSError) as exc:
            _log(log, exc)
            return _allow_after_error(str(exc))
    return {"decision": "continue", "reason": reason} if reason is not None else {"decision": "allow"}


def _log(log: hook_errors.HookLog, exc: Exception) -> None:
    if isinstance(exc, GitTimeoutError):
        log.git_timeout(str(exc))
    elif isinstance(exc, TranscriptError):
        log.record("transcript", str(exc))
    else:
        log.record(hook_errors.kind_of(exc), str(exc))


def _allow_after_error(problem: str) -> dict[str, str]:
    sys.stderr.write(
        f"[craftsman-stop-gate] The quality gate was not checked: {problem}. "
        "Run `craftsman review check` yourself.\n"
    )
    return {"decision": "allow"}


def main() -> int:
    configure_std_streams()
    try:
        out = answer(json.loads(sys.stdin.read()))
    except (OSError, ValueError) as exc:
        out = _allow_after_error(f"unreadable hook input ({exc})")
    sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
