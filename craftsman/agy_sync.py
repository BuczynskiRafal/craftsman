"""Bring the Antigravity subagent runs of a conversation into the evidence ledger.

Antigravity has no subagent hooks, so the runs are read back from transcripts: the main conversation's own
transcript names every subagent it launched (`transcript.subagent_launches`), and each subagent's transcript
(and the parent's registry file) tells whether it finished. They are written as `start` and `stop` events of
the platform `antigravity` (agent = the Craftsman role, agent id = the subagent's conversation id), which is all
the attribution and the gate need. The commands that judge a review (`check`, `report`, `brief`, `prepare`) call
`sync_antigravity_runs` first; the Stop hook calls `sync_from_stop`.

Times. Antigravity writes whole seconds, the CLI and the hooks write milliseconds, so a run built from
transcript times is widened to the seconds it touched:
- a run STOPS at the END of the second its last step was written in (raw time + 0.999 s): a CLI record made in
  the same second as the last step falls inside the run;
- a run STARTS at its launch second, except that a launch in the same second as a round's `prepared_at` but
  before it starts at `prepared_at` (it cannot be told from a launch right after `prepare`, and a run that
  started before the round was prepared backs nothing of it).
A subagent that was sent back by its Stop hook (a `nudge` event) is still working: it gets a stop only when it
finished again (its transcript ends with a finished turn at a later step than the nudge recorded: a time cannot
tell, the nudge has milliseconds and the transcript whole seconds) or when it was killed.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final

from craftsman.ledger import ledger_path_for, mutate
from craftsman.records import (
    REVIEWER_AGENT,
    VERIFIER_AGENT,
    AgentEvent,
    AgentRole,
    Ledger,
    format_timestamp,
    parse_timestamp,
)
from craftsman.transcript import (
    SubagentLaunch,
    SubagentState,
    conversation_transcript,
    parent_conversation,
    read_transcript_entries,
    subagent_finish,
    subagent_launches,
)
from craftsman.workspace import Workspace

PLATFORM: Final = "antigravity"
LAST_MILLISECOND_OF_A_SECOND = timedelta(milliseconds=999)
AGENT_OF_ROLE: dict[AgentRole, str] = {"reviewer": REVIEWER_AGENT, "verifier": VERIFIER_AGENT}
MAX_NESTING = 4  # a subagent of a subagent of ...: how far up the main conversation is looked for
CONVERSATION_VARIABLE = "ANTIGRAVITY_CONVERSATION_ID"
APP_DIR_VARIABLE = "ANTIGRAVITY_APP_DATA_DIR"


@dataclass(frozen=True)
class ObservedRun:
    """A subagent the main conversation launched, and where its own transcript says it stands."""

    launch: SubagentLaunch
    state: SubagentState


def caller_parent(env: Mapping[str, str]) -> str | None:
    """The conversation that launched the calling Antigravity conversation, None for a main conversation.

    Nothing is known without both `ANTIGRAVITY_CONVERSATION_ID` and `ANTIGRAVITY_APP_DATA_DIR` in `env`.
    """
    conversation = env.get(CONVERSATION_VARIABLE, "").strip()
    app_dir = env.get(APP_DIR_VARIABLE, "").strip()
    if not conversation or not app_dir:
        return None
    return parent_conversation(Path(app_dir).expanduser(), conversation)


def main_conversation(app_dir: Path, conversation_id: str, transcript: Path | None = None) -> str:
    """The main conversation `conversation_id` belongs to: itself, or the conversation that launched it (and so on up).

    `transcript` is the transcript of `conversation_id` itself when the caller knows it (a hook payload's).
    """
    current, path = conversation_id, transcript
    seen = {current}
    for _ in range(MAX_NESTING):
        parent = parent_conversation(app_dir, current, path=path)
        if parent is None or parent in seen:
            break
        seen.add(parent)
        current, path = parent, None
    return current


def observe_runs(app_dir: Path, main_id: str, transcript: Path | None = None) -> tuple[ObservedRun, ...]:
    """The Craftsman subagents `main_id` launched and where each stands. Reads transcripts only; raises
    `TranscriptError` when the main transcript cannot be read, or when a step that may hold a launch was cut from
    its compact form and `transcript_full.jsonl` cannot give it back (a missing, oversized or unreadable full file):
    a launch that cannot be seen is a problem for the caller to report, never "no launch"."""
    entries = read_transcript_entries(transcript or conversation_transcript(app_dir, main_id), restore=True)
    return tuple(
        ObservedRun(launch, subagent_finish(app_dir, launch.conversation_id, main_id))
        for launch in subagent_launches(entries)
    )


# --- the events of a run ------------------------------------------------------------------------------


def _seconds_since_epoch(moment: datetime) -> int:
    return int(moment.timestamp())


def start_time(ledger: Ledger, launched_at: str) -> datetime:
    """When a run launched at the whole-second `launched_at` started: that moment, or the `prepared_at` of the
    latest round prepared in the same second but after it."""
    launched = parse_timestamp(launched_at)
    started = launched
    for round_ in ledger.rounds:
        prepared = parse_timestamp(round_.prepared_at)
        if prepared > launched and _seconds_since_epoch(prepared) == _seconds_since_epoch(launched):
            started = prepared
    return started


def stop_time(ledger: Ledger, run: ObservedRun) -> datetime | None:
    """When the run stopped, None while it works (or is unreadable): see the module text for the rules."""
    finished = run.state.finished_at
    if finished is None:
        return None
    moment = parse_timestamp(finished)
    if run.state.killed:
        return moment  # the registry file's own time: a real instant, no second to widen
    nudges = [
        event
        for event in ledger.agents
        if event.event == "nudge" and event.platform == PLATFORM and event.agent_id == run.launch.conversation_id
    ]
    if nudges and not _finished_since(max(nudges, key=lambda event: parse_timestamp(event.at)), run.state, moment):
        return None  # it was sent back and has not finished since
    return moment + LAST_MILLISECOND_OF_A_SECOND


def _finished_since(nudge: AgentEvent, state: SubagentState, finished: datetime) -> bool:
    """Whether the turn that ended at `finished` came after the nudge: a later step than the one recorded at the
    nudge, else (a nudge that recorded none, its transcript was unreadable) a strictly later time."""
    if nudge.last_step is not None:
        return state.last_step is not None and state.last_step > nudge.last_step
    return finished > parse_timestamp(nudge.at)


def events_of(ledger: Ledger, run: ObservedRun) -> tuple[AgentEvent, ...]:
    """The `start` (and, when it stopped, `stop`) events of one observed run."""
    launch = run.launch
    agent = AGENT_OF_ROLE[launch.role]
    started = start_time(ledger, launch.launched_at)
    events = [AgentEvent(PLATFORM, "start", agent, launch.conversation_id, format_timestamp(started))]
    stopped = stop_time(ledger, run)
    if stopped is not None:
        events.append(AgentEvent(PLATFORM, "stop", agent, launch.conversation_id, format_timestamp(max(stopped, started))))
    return tuple(events)


def _is_run_event(event: AgentEvent, conversation_id: str) -> bool:
    return event.platform == PLATFORM and event.agent_id == conversation_id and event.event in ("start", "stop")


def apply_runs(ledger: Ledger, runs: tuple[ObservedRun, ...]) -> Ledger:
    """`ledger` with the start and stop events of `runs` as the transcripts show them.

    The earlier start and stop events of a run are replaced (a resumed subagent stops later); a run whose events
    are already as they should be is left where it is. A `nudge` is never touched.
    """
    for run in runs:
        wanted = events_of(ledger, run)
        id_ = run.launch.conversation_id
        recorded = tuple(event for event in ledger.agents if _is_run_event(event, id_))
        if recorded != wanted:
            others = tuple(event for event in ledger.agents if not _is_run_event(event, id_))
            ledger = replace(ledger, agents=(*others, *wanted))
    return ledger


# --- entry points --------------------------------------------------------------------------------------


def sync_from_stop(root: Path, app_dir: Path, conversation_id: str, transcript: Path | None = None) -> None:
    """Record the runs the conversation of `conversation_id` (or the main one it belongs to) launched, in one
    ledger update. Raises on a transcript or a ledger that cannot be read: the Stop hook reports and goes on."""
    main_id = main_conversation(app_dir, conversation_id, transcript)
    runs = observe_runs(app_dir, main_id, transcript if main_id == conversation_id else None)
    if runs:
        mutate(ledger_path_for(root), lambda ledger: (apply_runs(ledger, runs), None))


def sync_antigravity_runs(ws: Workspace, env: Mapping[str, str]) -> None:
    """Record the start and stop of the subagents the calling Antigravity conversation launched.

    Fail-safe by contract: it never raises, so a command that calls it first is never blocked by it; a problem
    is one line on stderr. Without `ANTIGRAVITY_CONVERSATION_ID` and `ANTIGRAVITY_APP_DATA_DIR` in `env` there is
    nothing to read. A subagent that calls it syncs the runs of its main conversation.
    """
    conversation = env.get(CONVERSATION_VARIABLE, "").strip()
    app_dir = env.get(APP_DIR_VARIABLE, "").strip()
    if not conversation or not app_dir:
        return
    try:
        sync_from_stop(ws.root, Path(app_dir).expanduser(), conversation)
    except Exception as exc:  # noqa: BLE001 - the contract above: a command must never fail on the sync
        sys.stderr.write(f"craftsman: the Antigravity subagent runs were not synced ({type(exc).__name__}: {exc})\n")
