"""Platform lifecycle hooks: `craftsman hook <event> --platform <copilot|claude>`.

Platform `copilot` serves VS Code Copilot and the GitHub Copilot CLI with one hook file: it reads
both input dialects (snake_case and camelCase keys) and answers with both output shapes in one
JSON object. Platform `claude` reads and answers in the Claude Code format.

Events:
- `prompt`: a `/craftsman` or `ultracraft` prompt opens a gate, like `craftsman gate --init`.
- `stop`: keeps the agent working while the evidence ledger does not pass the gate
  (`craftsman.gate.decide_stop`) or, for a session that began a `/craft-review`, the review
  (`craftsman.review_guard`); a new user prompt starts a new streak of continuations.
- `subagent-start` / `subagent-stop`: record subagent runs in the ledger (the evidence of reviewer
  isolation) of ANY session of the checkout, brief Craftsman hunters and verifiers, and send them back
  once when they finish without doing their part.
- `pre-tool`: applies the command policy (`craftsman.command_policy`: no forged hooks, no direct ledger
  or transcript writes, no forged identity, lead-only records for the main session, no re-planning by hunters
  and verifiers, the work-tree guard), enforces search scope, and arms the review guard when the main session
  runs a `craftsman review` command.

The first event of the owning session claims an active gate for the hooks: from then on the gate
requires an isolated reviewer for every round with hunks to review. Another session's prompt and Stop
leave the gate alone (its Stop runs its own review guard); its subagents are recorded all the same, since
one ledger serves the checkout.

A hook never crashes the session it serves. When an event cannot be handled, `pre-tool` asks the
user to confirm the tool call (Copilot denies it), and every other event lets the agent go on with
a `systemMessage` that says the gate was not checked. Nothing a hook survives is silent: a crash, a transcript
it could not read, a git call that ran out of the hook's budget, a ledger it could not load or had to set
aside, all go to the hook error log (`craftsman.hook_errors`), which `review check` shows.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal, NoReturn, get_args

from craftsman import hook_errors
from craftsman.attribution import agent_runs, pair_unnamed_stop
from craftsman.command_policy import (
    PolicyContext,
    arming_action,
    decide,
    is_main_session,
    tool_call,
)
from craftsman.diffscan import (
    DiffScanError,
    resolve_base_sha,
    snapshot,
    worktree_paths,
)
from craftsman.evidence import archive_ledger
from craftsman.gate import (
    GateOwner,
    GateState,
    Stage,
    archive_ledger_for_new_base,
    decide_stop,
    is_continuation_prompt,
    load_state,
    owns_active_gate,
    state_path_for,
    update_state,
)
from craftsman.gatecheck import claim_holders, unfinished_slots
from craftsman.huntview import hunk_ranges
from craftsman.ledger import (
    add_agent_event,
    ledger_path_for,
    load,
    mutate,
)
from craftsman.paths import user_home
from craftsman.records import (
    AgentEvent,
    AgentEventKind,
    AgentRole,
    Ledger,
    LedgerError,
    Round,
    agent_role,
    format_timestamp,
    parse_timestamp,
)
from craftsman.review_guard import (
    arm_for_review_command,
    gate_stop_inputs,
    review_in_progress,
    serve_review_stop,
)
from craftsman.scope import (
    evaluate_scope,
    is_craftsman_agent,
    is_scope_checked_tool,
    load_scope_policy,
)
from craftsman.subagent_stop import REVIEWER_STOP_REASON, verifier_debt
from craftsman.trigger import detect_trigger, is_review_prompt, review_request, task_from_prompt
from craftsman.usage import TranscriptFacts, read_transcript
from craftsman.workspace import arm_marker, end_review_guard

Platform = Literal["copilot", "claude"]
Event = Literal["prompt", "stop", "subagent-start", "subagent-stop", "pre-tool"]
Output = dict[str, Any]

PLATFORMS: tuple[Platform, ...] = get_args(Platform)
EVENTS: tuple[Event, ...] = get_args(Event)
HOOK_EVENT_NAMES: dict[Event, str] = {
    "prompt": "UserPromptSubmit",
    "stop": "Stop",
    "subagent-start": "SubagentStart",
    "subagent-stop": "SubagentStop",
    "pre-tool": "PreToolUse",
}
# Characters that a shell or PowerShell would interpret inside a double-quoted argument.
SHELL_ACTIVE_CHARS_RE = re.compile(r'["`$\\%!\n\r]')


class UsageError(Exception):
    """The hook command line is invalid; the event is blocked, since nothing was checked."""


class HookInputError(ValueError):
    """The hook payload carries a field Craftsman cannot read; the event is answered by `failure`."""


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise UsageError(message)


@dataclass(frozen=True)
class HookInput:
    """The fields Craftsman reads from a hook payload, whichever dialect it came in."""

    cwd: str
    agent: str
    agent_id: str
    stop_hook_active: bool
    prompt: str
    tool_name: str
    tool_input: dict[str, Any]
    transcript_path: str
    session_id: str | None = None
    agent_transcript_path: str = ""  # a subagent's own transcript (Claude Code SubagentStop)
    last_message: str = ""  # the agent's last message (Claude Code Stop), when the platform sends it

    @classmethod
    def parse(cls, payload: dict[str, Any]) -> HookInput:
        return cls(
            cwd=_required_text(payload, "cwd"),
            agent=_agent_name(payload),
            agent_id=_text(payload, "agent_id", "agentId"),
            stop_hook_active=any(
                payload.get(key) is True for key in ("stop_hook_active", "stopHookActive")
            ),
            prompt=_text(payload, "prompt"),
            tool_name=_text(payload, "tool_name", "toolName"),
            tool_input=_tool_input(payload),
            transcript_path=_text(payload, "transcript_path", "transcriptPath"),
            session_id=_text(payload, "session_id", "sessionId").strip() or None,
            agent_transcript_path=_text(payload, "agent_transcript_path", "agentTranscriptPath"),
            last_message=_text(payload, "last_assistant_message", "lastAssistantMessage"),
        )


def _required_text(payload: dict[str, Any], key: str) -> str:
    value = _text(payload, key)
    if not value:
        raise HookInputError(f"the hook input has no `{key}`")
    return value


def _text(payload: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _agent_name(payload: dict[str, Any]) -> str:
    """The agent's name; a Craftsman agent name wins when the platform sends several."""
    names = [
        value
        for key in ("agent_type", "agentType", "agentName", "agentDisplayName")
        if isinstance(value := payload.get(key), str) and value.strip()
    ]
    craftsman = [name for name in names if is_craftsman_agent(name)]
    return (craftsman or names or [""])[0]


def _tool_input(payload: dict[str, Any]) -> dict[str, Any]:
    """The tool call's arguments; an absent input is empty, an undecodable one an error."""
    raw: Any = next(
        (payload[key] for key in ("tool_input", "toolArgs", "toolInput") if payload.get(key)), {}
    )
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise HookInputError(f"the tool input is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise HookInputError(f"the tool input is a {type(raw).__name__}, not a JSON object")
    return raw


@dataclass(frozen=True)
class Reply:
    """Builds hook outputs in the shapes `platform` understands."""

    platform: Platform
    event: Event

    def allow(self, system_message: str | None = None) -> Output:
        return {"systemMessage": system_message} if system_message else {}

    def block(self, reason: str) -> Output:
        out: Output = {"decision": "block", "reason": reason}
        if self.platform == "copilot" and self.event == "stop":
            out["hookSpecificOutput"] = {"hookEventName": "Stop", "decision": "block", "reason": reason}
        return out

    def inject(self, context: str) -> Output:
        out: Output = {
            "hookSpecificOutput": {
                "hookEventName": HOOK_EVENT_NAMES[self.event],
                "additionalContext": context,
            }
        }
        if self.platform == "copilot":
            out = {"additionalContext": context, **out}
        return out

    def warn(self, message: str) -> Output:
        out: Output = {
            "systemMessage": message,
            "hookSpecificOutput": {
                "hookEventName": HOOK_EVENT_NAMES[self.event],
                "additionalContext": message,
            },
        }
        if self.platform == "copilot":
            out = {"additionalContext": message, **out}
        return out

    def deny(self, reason: str) -> Output:
        return self._permission("deny", reason)

    def ask(self, reason: str) -> Output:
        """Ask the user to confirm; Copilot cannot ask reliably, so it denies instead."""
        return self._permission("deny" if self.platform == "copilot" else "ask", reason)

    def _permission(self, decision: str, reason: str) -> Output:
        out: Output = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": decision,
                "permissionDecisionReason": reason,
            }
        }
        if self.platform == "copilot":
            out = {"permissionDecision": decision, "permissionDecisionReason": reason, **out}
        return out


@dataclass(frozen=True)
class Session:
    """The work tree a hook runs in, and its git directory when known (None: git is asked for it)."""

    root: Path
    git_dir: Path | None = None

    @property
    def state_path(self) -> Path:
        return state_path_for(self.root, git_directory=self.git_dir)

    @property
    def ledger_path(self) -> Path:
        return ledger_path_for(self.root, git_directory=self.git_dir)

    @property
    def craftsman_dir(self) -> Path:
        """`<git-dir>/craftsman`, found without running git when it can be (the hook error log must work after a git timeout)."""
        return hook_errors.craftsman_dir_for(self.root) or self.ledger_path.parent


def open_session(cwd: str) -> Session | None:
    """The session for the work tree containing `cwd`, or None outside a git work tree."""
    try:
        root, git_dir = worktree_paths(cwd)
    except DiffScanError:
        return None
    return Session(root, git_dir)


def claim(state: GateState | None, caller: GateOwner) -> GateState | None:
    """`state` as served by the hooks of `caller`'s session, when that session owns the gate."""
    if state is None or not owns_active_gate(state, caller):
        return state
    return replace(state, owner=state.owner.claimed_by(caller), isolation_evidence="hooks")


def handle(event: Event, platform: Platform, payload: dict[str, Any]) -> Output:
    hook = HookInput.parse(payload)
    reply = Reply(platform, event)
    session = open_session(hook.cwd)
    caller = GateOwner(platform, hook.session_id)
    if event == "prompt" and session is not None:
        if is_review_prompt(hook.prompt):
            deep, task = review_request(hook.prompt)
            arm_marker(session.ledger_path, caller, _now(), fresh=True, deep=deep, task=task)
        elif not is_continuation_prompt(hook.prompt):
            end_review_guard(session.ledger_path, caller)  # the user moved on to something else
    if event == "pre-tool":
        return on_pre_tool(hook, reply, session, caller)
    if session is None:
        return reply.allow()
    if not session.state_path.exists() and not (
        event == "prompt" and detect_trigger(hook.prompt) is not None
    ):
        # No gate and none to open: nothing to lock.
        return serve(event, session, None, hook, reply, caller)[1]
    return update_state(
        session.state_path, lambda state: serve(event, session, state, hook, reply, caller)
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def serve(
    event: Event,
    session: Session,
    stored: GateState | None,
    hook: HookInput,
    reply: Reply,
    caller: GateOwner,
) -> tuple[GateState | None, Output]:
    """Handle one event for the gate `stored`; the gate state afterwards and the hook's answer.

    Only the gate's own session is served by the gate (its prompts and Stops). Another session's prompt gets
    the foreign-gate note and its Stop its own review guard; the subagent events of ANY session of the
    checkout are recorded, since one ledger holds the evidence of the checkout.
    """
    state = claim(stored, caller)
    foreign = state is not None and state.active and not owns_active_gate(state, caller)
    if event == "prompt":
        if foreign:
            assert state is not None
            return state, on_non_owner_prompt(session, state, hook, reply)
        return on_prompt(session, state, hook, reply, caller)
    if event == "stop":
        if foreign:
            return state, review_stop_guard(session, reply, caller)
        return on_stop(session, state, reply, caller, hook)
    active = state is not None and state.active
    if event == "subagent-start":
        return state, on_subagent_start(session, active, hook, reply)
    return state, on_subagent_stop(session, active, hook, reply)


# --- prompt ---------------------------------------------------------------------------------


def on_non_owner_prompt(session: Session, state: GateState, hook: HookInput, reply: Reply) -> Output:
    if detect_trigger(hook.prompt) is None:
        return reply.allow()
    return reply.inject(
        f"Craftsman gate in {session.root} is already active and owned by another session "
        f"({state.owner.describe()}, since {state.started_at}). Your `/craftsman` prompt did not "
        "overwrite that gate. To run an independent Craftsman session in parallel, create a "
        "separate worktree (`git worktree add ../<branch-dir> -b <branch>`), or run "
        "`craftsman gate --init --takeover` to take over the gate in this work tree."
    )


def after_user_prompt(state: GateState, prompt: str, deep: bool | None) -> GateState:
    """An active gate after its user wrote `prompt`: a new streak, and the gate can become deep."""
    if not is_continuation_prompt(prompt):
        state = replace(
            state, awaiting_user=False, awaiting_since=None, question=None, forced_continuations=0, best_issue_count=None
        )
    if deep and state.stage is not Stage.NEEDS_HUMAN:
        state = replace(state, deep=True)
    return state


def on_prompt(
    session: Session, state: GateState | None, hook: HookInput, reply: Reply, caller: GateOwner
) -> tuple[GateState | None, Output]:
    deep = detect_trigger(hook.prompt)
    owner = caller
    restarted_deep = False  # a restart after a needs-human stop starts as deep as that gate was
    if state is not None and state.active:
        state = after_user_prompt(state, hook.prompt, deep)
        if deep is None or state.stage is not Stage.NEEDS_HUMAN:
            return state, reply.allow()
        archive_ledger(session.ledger_path)  # the review needed a human; a new request starts over
        owner, restarted_deep = state.owner, state.deep
    elif deep is None:
        return state, reply.allow()
    base_sha = resolve_base_sha(session.root, None)
    started = GateState.open(
        owner,
        base_sha,
        bool(deep) or restarted_deep,
        task_from_prompt(hook.prompt),
        isolation_evidence="hooks",
    )
    archive_ledger_for_new_base(session.root, base_sha)
    return started, reply.inject(
        f"Craftsman session started ({'deep, ' if started.deep else ''}base {base_sha[:7]}). Follow the /craftsman "
        f"skill (Stage 0 command: `{init_command(started)}`); the gate will block finishing "
        "until `craftsman review check` passes.\n"
        f"Scope: work only inside {session.root}; see Search scope."
    )


def init_command(state: GateState) -> str:
    """The Stage 0 command for `state`, safe to paste into a POSIX shell or PowerShell."""
    session = f" --session-id {_shell_safe(state.owner.session_id)}" if state.owner.session_id else ""
    deep = " --deep" if state.deep else ""
    return f'craftsman gate --init{session}{deep} --task "{_shell_safe(state.task)}"'


def _shell_safe(text: str) -> str:
    return SHELL_ACTIVE_CHARS_RE.sub("", text)


# --- stop -----------------------------------------------------------------------------------


def on_stop(
    session: Session,
    state: GateState | None,
    reply: Reply,
    caller: GateOwner | None = None,
    hook: HookInput | None = None,
) -> tuple[GateState | None, Output]:
    if state is None or not state.active:
        guard = review_stop_guard(session, reply, caller) if caller is not None else reply.allow()
        return state, guard
    if state.awaiting_user and hook is not None:
        _check_await_user(session, state, hook)
    inputs = gate_stop_inputs(session.ledger_path, _now()) if not state.awaiting_user else None
    decision = decide_stop(
        state,
        session.root,
        running=inputs.running if inputs is not None else (),
        evidence=inputs.evidence if inputs is not None else None,
    )
    if decision.block_reason is not None:
        return decision.state, reply.block(decision.block_reason)
    if caller is not None and not decision.state.active:
        end_review_guard(session.ledger_path, caller)  # the gate is over (done or given up): so is a guard it outlived
    if decision.notice is not None:
        return decision.state, notice(reply, decision.notice)
    return decision.state, reply.allow()


MAX_TRANSCRIPT_TAIL_BYTES = 1024 * 1024


def _check_await_user(session: Session, state: GateState, hook: HookInput) -> None:
    """The Stop hook lets an agent stop because it awaits the user: log it when its last message asks nothing.

    The stop itself is never blocked (the user sees the message), but an agent that parks the gate with
    `--await-user` and stops without a question is leaving the user to guess; `review check` shows the log entry.
    A message that cannot be found is not judged.
    """
    message = hook.last_message or _last_assistant_message(session, hook)
    if message is not None and not asks_a_question(message, state.question):
        report_error("await-user", "await-user without a question: the last message does not end with a question", session)


def asks_a_question(message: str, question: str | None) -> bool:
    """Whether `message` asks the user something: a `?` in its last paragraph, or the stored question text in it."""
    paragraphs = [part for part in re.split(r"\n\s*\n", message.strip()) if part.strip()]
    if paragraphs and "?" in paragraphs[-1]:
        return True
    wanted = " ".join((question or "").split()).casefold()
    return bool(wanted) and wanted in " ".join(message.split()).casefold()


def _last_assistant_message(session: Session, hook: HookInput) -> str | None:
    """The text of the last assistant message of the session transcript, None when it holds none or is not readable."""
    if not hook.transcript_path:
        return None
    path = Path(hook.transcript_path)
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - MAX_TRANSCRIPT_TAIL_BYTES))
            tail = handle.read().decode("utf-8", errors="replace")
    except OSError as exc:
        report_error("transcript", f"cannot read the session transcript {path} to check the await-user question: {exc}", session)
        return None
    for line in reversed(tail.splitlines()):
        text = _assistant_text(line)
        if text is not None:
            return text
    return None


def _assistant_text(line: str) -> str | None:
    """The text of one transcript line when it is an assistant message (Claude Code and Copilot CLI shapes), else None."""
    try:
        entry = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(entry, dict):
        return None
    message = entry.get("message") if isinstance(entry.get("message"), dict) else entry.get("data")
    holder: dict[str, Any] = message if isinstance(message, dict) else entry
    role = holder.get("role") or entry.get("role")
    if entry.get("type") not in ("assistant", "assistant.message") and role != "assistant":
        return None
    content = holder.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = [
            block["text"] for block in content if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str)
        ]
        return "\n".join(texts) if texts else None
    return None


def review_stop_guard(session: Session, reply: Reply, caller: GateOwner) -> Output:
    """Keep a session that invoked the review skill working until its review-only outcome exists.

    The decision is `review_guard.decide_review_stop`, the same one for every platform; this only words it.
    """
    decision = serve_review_stop(session.root, session.ledger_path, caller, _now())
    if decision.block is not None:
        return reply.block(decision.block)
    if decision.notice is not None:
        return notice(reply, decision.notice)
    return reply.allow()


def notice(reply: Reply, message: str) -> Output:
    """Let the agent stop with a message for the user (stderr too: not every client shows it)."""
    sys.stderr.write(message + "\n")
    return reply.allow(message)


# --- subagents ------------------------------------------------------------------------------


def record_agent_event(
    session: Session, hook: HookInput, kind: AgentEventKind, platform: str
) -> tuple[Ledger, AgentEvent]:
    """Append the event under the ledger lock; return the ledger as it was before, and the event."""
    now = _now()
    recorded: list[AgentEvent] = []

    def append(ledger: Ledger) -> tuple[Ledger, Ledger]:
        event = AgentEvent(
            platform=platform,
            event=kind,
            agent=hook.agent or "unknown",
            agent_id=hook.agent_id,
            at=format_timestamp(_distinct_start(ledger, now) if kind == "start" and not hook.agent_id else now),
        )
        recorded.append(event)
        return add_agent_event(ledger, event), ledger

    before = mutate(session.ledger_path, append)
    return before, recorded[-1]


def _distinct_start(ledger: Ledger, moment: datetime) -> datetime:
    """`moment`, moved on by whole milliseconds until no start without an agent id has it.

    A nudge names the start it answered by its time (`AgentEvent.answered`), so two such starts in the same
    millisecond (parallel hunters of a platform that names no agents) would be one start to every later reader.
    """
    taken = {
        format_timestamp(parse_timestamp(event.at))
        for event in ledger.agents
        if event.event == "start" and not event.agent_id
    }
    while format_timestamp(moment) in taken:
        moment += timedelta(milliseconds=1)
    return moment


def _has_current_round(session: Session) -> bool:
    """True when the ledger's latest round reviews the diff the work tree has now."""
    if not session.ledger_path.is_file():
        return False
    ledger = load(session.ledger_path)
    if not ledger.rounds:
        return False
    latest = ledger.rounds[-1]
    try:
        return (
            snapshot(session.root, latest.base_sha, ledger.current_scope).fingerprint
            == latest.fingerprint
        )
    except DiffScanError:
        return False


def on_subagent_start(session: Session, active: bool, hook: HookInput, reply: Reply) -> Output:
    """Record the start; brief a Craftsman reviewer or verifier about the current round.

    Outside an active gate, only Craftsman agents reviewing the current round are tracked.
    """
    role = agent_role(hook.agent)
    if not active and (role is None or not _has_current_round(session)):
        return reply.allow()
    ledger, _ = record_agent_event(session, hook, "start", reply.platform)
    if role is None or not ledger.rounds:
        return reply.allow()
    return reply.inject(briefing(role, ledger))


def briefing(role: AgentRole, ledger: Ledger) -> str:
    """The round and where the agent's work comes from; the brief command (and the templates) hold the rest.

    Slot-agnostic on purpose: the slot is in the agent's launch prompt, and the hook lists neither
    uncovered hunks nor other agents' slots. No packet path: it holds every hunter's hunks.
    """
    number = ledger.rounds[-1].number
    if role == "reviewer":
        return (
            f"Craftsman review round {number}. Your slot id is in your prompt: run "
            "`craftsman review brief <ID>` and follow it; cover only your slot (other slots belong to "
            "other hunters)."
        )
    return (
        f"Craftsman review round {number}. Run the `craftsman review brief --verifier ...` command "
        "from your prompt."
    )


def on_subagent_stop(session: Session, active: bool, hook: HookInput, reply: Reply) -> Output:
    """Decide whether a stopping subagent may finish, as the ledger would look had it stopped.

    A Craftsman agent is judged on the candidate ledger with its stop event in it; it is sent back
    (one `nudge` event, no stop) at most once per run, and only an allowed stop is recorded, with the facts
    of its whole transcript. A stop of an agent the ledger never saw start (a hunter of a round that was
    reset since) is dropped.
    """
    if not active and not session.ledger_path.is_file():
        return reply.allow()
    hook = _with_started_agent_name(session, hook)
    role = agent_role(hook.agent)
    if not active and (role is None or not _has_current_round(session)):
        return reply.allow()
    if role is not None and not _saw_start(load(session.ledger_path), hook, role, _now()):
        _drop_unseen_stop(hook)
        return reply.allow()
    facts = _agent_transcript(session, hook, reply.platform)  # slow: read outside the ledger lock

    def judge(ledger: Ledger) -> tuple[Ledger, Output]:
        now = _now()
        if role is not None and not _saw_start(ledger, hook, role, now):
            _drop_unseen_stop(hook)
            return ledger, reply.allow()
        stop = AgentEvent(
            platform=reply.platform,
            event="stop",
            agent=hook.agent or "unknown",
            agent_id=hook.agent_id,
            at=format_timestamp(now),
            usage=facts.usage,
            records=facts.records,
        )
        candidate = add_agent_event(ledger, stop)
        problem = (
            _unfinished_work(ledger, candidate, hook, role, stop, now)
            if role is not None and not hook.stop_hook_active and not _was_sent_back(ledger, hook, role, now)
            else None
        )
        if problem is None:
            return candidate, reply.allow()
        started = _run_start(ledger, hook, role, now) if role is not None else None
        answered = None if started is None else format_timestamp(started)
        nudge = replace(stop, event="nudge", usage=None, records=None, answered=answered)
        return add_agent_event(ledger, nudge), reply.block(problem)

    return mutate(session.ledger_path, judge)


def _drop_unseen_stop(hook: HookInput) -> None:
    who = hook.agent_id or hook.agent or "unknown"
    sys.stderr.write(f"craftsman hook: stop of an agent this ledger never saw start ({who}); not recorded\n")


def _nudged_starts(ledger: Ledger) -> set[datetime]:
    """The starts of the runs without an agent id that were sent back already (the `answered` of their nudges)."""
    return {
        parse_timestamp(event.answered)
        for event in ledger.agents
        if event.event == "nudge" and event.answered is not None and not event.agent_id
    }


def _pairing_start(ledger: Ledger, at: datetime, starts: Sequence[datetime]) -> datetime | None:
    """The one of `starts` (times of unpaired starts of one role) that a stop without an agent id at `at` ends."""
    rounds = [item for item in ledger.rounds if parse_timestamp(item.prepared_at) <= at]
    prepared = parse_timestamp(rounds[-1].prepared_at) if rounds else None
    pick = pair_unnamed_stop(starts, at, prepared, _nudged_starts(ledger))
    return None if pick is None else starts[pick]


def _unpaired_start(ledger: Ledger, role: AgentRole, stop_at: datetime, *, without_id: bool) -> datetime | None:
    """The start of the run a stop without an agent id ends (`attribution.pair_unnamed_stop`), None when it ends none.

    A stop that carries an id no start carries (a platform that names its agents at the stop only) may end only
    a start that has no id (`without_id`), as `attribution.agent_runs` pairs it.
    """
    starts = [
        run.started
        for run in agent_runs(ledger)
        if run.role == role and run.stopped is None and not (without_id and run.agent_id)
    ]
    return _pairing_start(ledger, stop_at, starts)


def _run_start(ledger: Ledger, hook: HookInput, role: AgentRole, stop_at: datetime) -> datetime | None:
    """When the run this stop ends started, None when the ledger never saw it start.

    With an agent id: its start, paired or not (a resumed subagent stops again with its id). Without one
    (Copilot), or with an id no start carries: an unpaired start of that agent name that the stop can end.
    """
    if hook.agent_id:
        started = [
            parse_timestamp(event.at)
            for event in ledger.agents
            if event.event == "start" and event.agent_id == hook.agent_id
        ]
        if started:
            return max(started)
    return _unpaired_start(ledger, role, stop_at, without_id=bool(hook.agent_id))


def _saw_start(ledger: Ledger, hook: HookInput, role: AgentRole, stop_at: datetime) -> bool:
    return _run_start(ledger, hook, role, stop_at) is not None


def _was_sent_back(ledger: Ledger, hook: HookInput, role: AgentRole, stop_at: datetime) -> bool:
    """Whether the run this stop ends was sent back already: a nudge of its agent id; for Copilot (no ids) a nudge
    that answered the start this stop pairs with (`attribution.pair_unnamed_stop` prefers a start that was not
    sent back, so a sibling's nudge never excuses this run)."""
    if hook.agent_id:
        return any(
            event.event == "nudge" and event.agent_id == hook.agent_id and agent_role(event.agent) == role
            for event in ledger.agents
        )
    started = _run_start(ledger, hook, role, stop_at)
    return started is not None and started in _nudged_starts(ledger)


def _unfinished_work(
    before: Ledger,
    after: Ledger,
    hook: HookInput,
    role: AgentRole,
    stop: AgentEvent,
    now: datetime,
) -> str | None:
    """What the stopping agent still owes, or None when it may finish.

    `after` is the ledger as if it had stopped (its stop event, with the records its transcript shows,
    included), `before` the ledger without it. The decision is made on `after`: judged on `before` no run
    would vouch for what the agent recorded, and every honest hunter would be sent back.
    """
    if not after.rounds:
        return None
    stop_at = parse_timestamp(stop.at)
    since = _run_start(before, hook, role, stop_at)
    assert since is not None  # `on_subagent_stop` dropped the stop of a run the ledger never saw start
    if role == "verifier":
        return verifier_debt(after, stop.records, since)
    latest = after.rounds[-1]
    unfinished = unfinished_slots(after, latest, stop_at, now)
    if unfinished:
        slot_id, missing = unfinished[0]
        hunks = hunk_ranges(missing)
        return (
            f"Your slot {slot_id} is not finished: cover hunks {hunks} with "
            f'`craftsman review cover --hunter {slot_id} --hunks {hunks} --note "..."`, then reply.'
        )
    if _holds_claim(after, latest, stop, now):
        return None
    if stop.records is not None:
        return None if stop.records else REVIEWER_STOP_REASON
    # In a round held to its hunters, records that name a slot belong to the hunter that claimed it: a hunter that
    # holds no claim is excused by records of nobody's slot only.
    held = latest.plan is not None and latest.enforced
    recorded = any(
        parse_timestamp(item.recorded_at) >= since and not (held and item.hunter is not None) for item in after.coverage
    ) or any(
        parse_timestamp(item.recorded_at) >= since and not (held and item.hunter is not None) for item in after.findings
    )
    return None if recorded else REVIEWER_STOP_REASON


def _holds_claim(after: Ledger, latest: Round, stop: AgentEvent, now: datetime) -> bool:
    """Whether the stopping reviewer fetched a brief for the latest round.

    With records, its `B` token is among them. Without (no transcript was readable) the claim is the one the
    gate's one-to-one matching gives this run (`gatecheck.claim_holders`): a claim made while several hunters
    ran belongs to one of them, not to all.
    """
    if stop.records is not None:
        return any(claim.token in stop.records for claim in after.claims if claim.round == latest.number)
    stop_at = parse_timestamp(stop.at)
    holders = claim_holders(after, latest, now)
    return any(
        index in holders
        for index, run in enumerate(agent_runs(after))
        if run.role == "reviewer" and run.stopped == stop_at
    )


def transcript_roots(platform: Platform, hook: HookInput, env: Mapping[str, str] | None = None) -> tuple[Path, ...]:
    """Where the transcript of a subagent of this session may be: next to the session's own transcript, or under
    the platform's transcript root (Claude Code: `$CLAUDE_CONFIG_DIR/projects` and `~/.claude/projects`; Copilot:
    `~/.copilot/session-state`)."""
    values = os.environ if env is None else env
    home = user_home()
    roots: list[Path] = []
    if hook.transcript_path:
        roots.append(Path(hook.transcript_path).parent)
    if platform == "claude":
        if values.get("CLAUDE_CONFIG_DIR", "").strip():
            roots.append(Path(values["CLAUDE_CONFIG_DIR"]).expanduser() / "projects")
        roots.append(home / ".claude" / "projects")
    else:
        roots.append(home / ".copilot" / "session-state")
    return tuple(roots)


def _has_provenance(path: Path, roots: Sequence[Path]) -> bool:
    resolved = Path(os.path.realpath(path))
    return any(resolved == base or base in resolved.parents for base in (Path(os.path.realpath(root)) for root in roots))


def _agent_transcript(session: Session, hook: HookInput, platform: Platform) -> TranscriptFacts:
    """The agent's usage and records from its own transcript, if the payload names one; never raises.

    The payload's plain `transcript_path` is the parent session's, so it is not read here. A transcript that is
    not under the platform's transcript root for this session (`transcript_roots`) is not read: the stop is
    recorded with `records=None` (the hunter is judged by the time window) and the hook error log says so. A
    transcript that cannot be read is logged too.
    """
    if not hook.agent_transcript_path:
        return TranscriptFacts()
    path = Path(hook.agent_transcript_path)
    if not _has_provenance(path, transcript_roots(platform, hook)):
        report_error(
            "provenance",
            f"the agent transcript {path} is not under the {platform} transcript root for this session; not read",
            session,
        )
        return TranscriptFacts()
    try:
        facts = read_transcript(path)
    except Exception as exc:  # noqa: BLE001 - telemetry must never fail the hook
        report_error("transcript", f"cannot read the agent transcript {path}: {type(exc).__name__}: {exc}", session)
        return TranscriptFacts()
    if facts.problem is not None:
        report_error("transcript", facts.problem, session)
    return facts


def _with_started_agent_name(session: Session, hook: HookInput) -> HookInput:
    """A stop payload that carries only the agent id gets the name its start event recorded."""
    if hook.agent or not hook.agent_id:
        return hook
    starts = [
        event.agent
        for event in load(session.ledger_path).agents
        if event.event == "start" and event.agent_id == hook.agent_id
    ]
    return replace(hook, agent=starts[-1]) if starts else hook


# --- pre-tool -------------------------------------------------------------------------------


def _review_in_progress(session: Session) -> int | None:
    """`review_guard.review_in_progress` for the work tree of `session`."""
    return review_in_progress(session.ledger_path, session.state_path)


def _policy_context(hook: HookInput, session: Session | None, caller: GateOwner) -> PolicyContext:
    """What the command policy may ask of this work tree, each asked only if a rule needs it."""

    def ledger_dir() -> Path | None:
        try:
            return session.ledger_path.parent if session is not None else ledger_path_for(hook.cwd).parent
        except LedgerError:
            return None

    def owns_gate() -> bool:
        return session is not None and owns_active_gate(load_state(session.state_path), caller)

    def review_in_progress() -> int | None:
        return _review_in_progress(session) if session is not None else None

    return PolicyContext(ledger_dir, owns_gate, review_in_progress)


def _arm_for_review_command(hook: HookInput, review: tuple[str, ...], session: Session, caller: GateOwner) -> None:
    """The main session runs a `craftsman review` command: arm the review guard for it (a `report` only touches)."""
    if arming_action(review) is not None and is_main_session(hook.agent, hook.agent_id):  # no git for other calls
        arm_for_review_command(session.ledger_path, session.state_path, caller, review, _now())


def _is_refusal(out: Output) -> bool:
    """Whether a PreToolUse answer denies the tool call or asks the user about it."""
    decisions = {out.get("permissionDecision"), out.get("hookSpecificOutput", {}).get("permissionDecision")}
    return bool(decisions & {"deny", "ask"})


def on_pre_tool(hook: HookInput, reply: Reply, session: Session | None, caller: GateOwner) -> Output:
    """Apply the command policy to the tool call, enforce search scope, and arm the review guard for a
    `craftsman review` command that was allowed to run.

    The policy (`command_policy.decide`) answers first for every tool: the write protection of the ledger and the
    transcripts must reach tools outside `SCOPE_CHECKED_TOOLS` (Write, Edit, Copilot's edit tools) too.
    """
    call = tool_call(
        hook.tool_name,
        hook.tool_input,
        hook.cwd,
        role=agent_role(hook.agent),
        main_session=is_main_session(hook.agent, hook.agent_id),
        session_id=hook.session_id,
    )
    decision = decide(call, _policy_context(hook, session, caller))
    if decision.action == "deny":
        return reply.deny(decision.reason)
    if decision.action == "ask":
        return reply.ask(decision.reason)
    out = _enforce_scope(hook, reply, session, caller)
    if session is not None and not _is_refusal(out):
        _arm_for_review_command(hook, decision.review, session, caller)
    return out


def _enforce_scope(hook: HookInput, reply: Reply, session: Session | None, caller: GateOwner) -> Output:
    if session is None or not is_scope_checked_tool(hook.tool_name):
        return reply.allow()
    if not _serves_owned_gate(session, caller) and not is_craftsman_agent(hook.agent):
        return reply.allow()
    policy = load_scope_policy(Path(hook.cwd), session.root)
    if policy.mode == "off":
        return reply.allow()
    scope_decision = evaluate_scope(
        hook.tool_name, hook.tool_input, Path(hook.cwd), session.root, policy
    )
    if scope_decision.decision == "deny":
        return (
            reply.warn(scope_decision.reason)
            if policy.mode == "warn"
            else reply.deny(scope_decision.reason)
        )
    return reply.allow()


def _serves_owned_gate(session: Session, caller: GateOwner) -> bool:
    """Whether `caller`'s session owns the active gate; claims it for the hooks on first contact."""
    state = load_state(session.state_path)
    if not owns_active_gate(state, caller):
        return False
    if claim(state, caller) != state:
        update_state(session.state_path, lambda current: (claim(current, caller), None))
    return True


# --- the hook error log -----------------------------------------------------------------------


def report_error(kind: hook_errors.ErrorKind, message: str, session: Session | None = None) -> None:
    """Log a problem the hook survives (`hook_errors.report`), in `session`'s checkout when no hook run is active."""
    directory: Path | None = None
    if session is not None:
        try:
            directory = session.craftsman_dir
        except (DiffScanError, LedgerError):
            directory = None
    hook_errors.report(kind, message, directory)


# --- entry point ----------------------------------------------------------------------------


def _parser() -> _Parser:
    parser = _Parser(
        prog="craftsman hook",
        description=(
            "Lifecycle hook for VS Code Copilot, the Copilot CLI and Claude Code. Reads the "
            "platform's JSON payload on stdin and prints the hook's JSON answer."
        ),
    )
    parser.add_argument("event", choices=EVENTS)
    parser.add_argument("--platform", required=True, choices=PLATFORMS)
    return parser


def _read_stdin() -> str:
    buffer = getattr(sys.stdin, "buffer", None)
    if buffer is not None:
        data: bytes = buffer.read()
        return data.decode("utf-8", errors="replace")
    return sys.stdin.read()


def _emit(out: Output) -> None:
    sys.stdout.write(json.dumps(out) + "\n")
    sys.stdout.flush()


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(sys.argv[1:] if argv is None else argv)
    except UsageError as exc:
        # Exit code 2 makes the platform block the event: an unchecked tool call must not pass.
        sys.stderr.write(f"craftsman hook error: {exc}\n")
        return 2
    reply = Reply(args.platform, args.event)
    try:
        payload = json.loads(_read_stdin().strip() or "{}")
    except (OSError, ValueError) as exc:
        with hook_errors.hook_run(args.event, None) as log:
            _emit(failure(reply, f"unreadable hook input: {exc}", log))
        return 0
    if not isinstance(payload, dict):
        with hook_errors.hook_run(args.event, None) as log:
            _emit(failure(reply, "the hook input is not a JSON object", log))
        return 0
    cwd = payload.get("cwd")
    with hook_errors.hook_run(args.event, cwd if isinstance(cwd, str) and cwd else None) as log:
        try:
            out = handle(args.event, args.platform, payload)
        except Exception as exc:  # the platform treats a crashed hook as "allow"; `failure` decides
            out = failure(reply, f"{type(exc).__name__}: {exc}", log, hook_errors.kind_of(exc))
    _emit(out)
    return 0


def failure(
    reply: Reply, problem: str, log: hook_errors.HookLog | None = None, kind: hook_errors.ErrorKind = "crash"
) -> Output:
    """The answer when an event cannot be handled: confirm a tool call, else go on with a warning.

    The problem goes to the hook error log (`log`), so `review check` can say a hook failed.
    """
    sys.stderr.write(f"craftsman hook error: {problem}\n")
    if log is not None:
        if kind == "git-timeout":
            log.git_timeout(problem)  # the budget may have logged this run's timeout already
        else:
            log.record(kind, problem)
    if reply.event == "pre-tool":
        return reply.ask(
            f"The Craftsman guard could not check this tool call ({problem}); confirm it only if "
            "it is safe."
        )
    if reply.event == "prompt":
        # Shown to the user and the agent: a prompt meant to open the gate must not look accepted.
        return reply.warn(
            f"Craftsman prompt hook failed ({problem}). If this prompt asked for /craftsman, the "
            "gate was NOT opened; fix the problem and send the prompt again."
        )
    return reply.allow(
        f"Craftsman {reply.event} hook failed ({problem}); the quality gate was not checked. "
        "Run `craftsman review check` yourself."
    )


if __name__ == "__main__":
    raise SystemExit(main())
