"""Platform lifecycle hooks: `craftsman hook <event> --platform <copilot|claude>`.

Platform `copilot` serves VS Code Copilot and the GitHub Copilot CLI with one hook file: it reads
both input dialects (snake_case and camelCase keys) and answers with both output shapes in one
JSON object. Platform `claude` reads and answers in the Claude Code format.

Events:
- `prompt`: a `/craftsman` or `ultracraft` prompt opens a gate, like `craftsman gate --init`.
- `stop`: keeps the agent working while the evidence ledger does not pass the gate
  (`craftsman.gate.decide_stop`); a new user prompt starts a new streak of continuations.
- `subagent-start` / `subagent-stop`: record subagent runs in the ledger (the evidence of reviewer
  isolation), brief Craftsman reviewers and verifiers, and send them back once when they finish
  without recording anything.
- `pre-tool`: applies the git guard to shell commands and the search scope to search tools.

The first event of the owning session claims an active gate for the hooks: from then on the gate
requires an isolated reviewer on the `standard` and `deep` tiers. Events of other sessions leave
the gate alone.

A hook never crashes the session it serves. When an event cannot be handled, `pre-tool` asks the
user to confirm the tool call (Copilot denies it), and every other event lets the agent go on with
a `systemMessage` that says the gate was not checked.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal, NoReturn, get_args

from craftsman.diffscan import DiffScanError, resolve_base_sha, snapshot, worktree_root
from craftsman.evidence import archive_ledger, packet_path
from craftsman.gate import (
    GateOwner,
    GateState,
    GateStateError,
    GateTier,
    Stage,
    archive_ledger_for_new_base,
    decide_stop,
    is_continuation_prompt,
    load_state,
    raised_tier,
    state_path_for,
    update_state,
)
from craftsman.guard import evaluate_shell_command, find_worktree_mutation
from craftsman.ledger import (
    GROUPED_EVIDENCE_PREFIX,
    add_agent_event,
    ledger_path_for,
    load,
    mutate,
    uncovered_by_lens,
)
from craftsman.packet import CHEAT_SHEET
from craftsman.records import (
    SWEEP_LENS,
    AgentEvent,
    AgentEventKind,
    AgentRole,
    Ledger,
    LedgerError,
    agent_role,
    format_timestamp,
    parse_timestamp,
)
from craftsman.scope import (
    evaluate_scope,
    is_craftsman_agent,
    is_scope_checked_tool,
    load_scope_policy,
)
from craftsman.trigger import detect_trigger, task_from_prompt

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
SHELL_TOOLS = frozenset(
    {"run_in_terminal", "runinterminal", "terminal", "bash", "powershell", "pwsh", "shell"}
)
# Characters that a shell or PowerShell would interpret inside a double-quoted argument.
SHELL_ACTIVE_CHARS_RE = re.compile(r'["`$\\%!\n\r]')
REVIEWER_STOP_REASON = (
    "Before finishing, record coverage (`craftsman review cover ...`) for every lens you "
    "examined, and a finding (`craftsman review add ...`) for every defect you found."
)
COPILOT_PUSH_NOTE = (
    " Copilot hooks cannot see the chat, so a confirmation there does not unlock this command: "
    "ask the user, and if they choose a direct push, they run it themselves."
)


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
    """The work tree a hook runs in."""

    root: Path

    @property
    def state_path(self) -> Path:
        return state_path_for(self.root)

    @property
    def ledger_path(self) -> Path:
        return ledger_path_for(self.root)


def open_session(cwd: str) -> Session | None:
    """The session for the work tree containing `cwd`, or None outside a git work tree."""
    try:
        return Session(worktree_root(cwd))
    except DiffScanError:
        return None


def owns_active_gate(state: GateState | None, caller: GateOwner) -> bool:
    return state is not None and state.active and state.owner.admits(caller)


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


def serve(
    event: Event,
    session: Session,
    stored: GateState | None,
    hook: HookInput,
    reply: Reply,
    caller: GateOwner,
) -> tuple[GateState | None, Output]:
    """Handle one event for the gate `stored`; the gate state afterwards and the hook's answer."""
    state = claim(stored, caller)
    if state is not None and state.active and not owns_active_gate(state, caller):
        return state, on_non_owner_prompt(
            session, state, hook, reply
        ) if event == "prompt" else reply.allow()
    if event == "prompt":
        return on_prompt(session, state, hook, reply, caller)
    if event == "stop":
        return on_stop(session, state, reply)
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


def after_user_prompt(state: GateState, prompt: str, tier: GateTier | None) -> GateState:
    """An active gate after its user wrote `prompt`: a new streak, and the tier can rise."""
    if not is_continuation_prompt(prompt):
        state = replace(state, awaiting_user=False, forced_continuations=0, best_issue_count=None)
    if tier is not None and state.stage is not Stage.NEEDS_HUMAN:
        state = replace(state, tier=raised_tier(state.tier, tier))
    return state


def on_prompt(
    session: Session, state: GateState | None, hook: HookInput, reply: Reply, caller: GateOwner
) -> tuple[GateState | None, Output]:
    tier = detect_trigger(hook.prompt)
    owner = caller
    if state is not None and state.active:
        state = after_user_prompt(state, hook.prompt, tier)
        if tier is None or state.stage is not Stage.NEEDS_HUMAN:
            return state, reply.allow()
        archive_ledger(session.ledger_path)  # the review needed a human; a new request starts over
        owner = state.owner
    elif tier is None:
        return state, reply.allow()
    base_sha = resolve_base_sha(session.root, None)
    started = GateState.open(
        owner, base_sha, tier, task_from_prompt(hook.prompt), isolation_evidence="hooks"
    )
    archive_ledger_for_new_base(session.root, base_sha)
    return started, reply.inject(
        f"Craftsman session started (tier {tier}, base {base_sha[:7]}). Follow the /craftsman "
        f"skill (Stage 0 command: `{init_command(started)}`); the gate will block finishing "
        "until `craftsman review check` passes.\n"
        f"Scope: work only inside {session.root}; see Search scope."
    )


def init_command(state: GateState) -> str:
    """The Stage 0 command for `state`, safe to paste into a POSIX shell or PowerShell."""
    session = f" --session-id {_shell_safe(state.owner.session_id)}" if state.owner.session_id else ""
    return f'craftsman gate --init{session} --tier {state.tier} --task "{_shell_safe(state.task)}"'


def _shell_safe(text: str) -> str:
    return SHELL_ACTIVE_CHARS_RE.sub("", text)


# --- stop -----------------------------------------------------------------------------------


def on_stop(session: Session, state: GateState | None, reply: Reply) -> tuple[GateState | None, Output]:
    if state is None or not state.active:
        return state, reply.allow()
    decision = decide_stop(state, session.root)
    if decision.block_reason is not None:
        return decision.state, reply.block(decision.block_reason)
    if decision.notice is not None:
        return decision.state, notice(reply, decision.notice)
    return decision.state, reply.allow()


def notice(reply: Reply, message: str) -> Output:
    """Let the agent stop with a message for the user (stderr too: not every client shows it)."""
    sys.stderr.write(message + "\n")
    return reply.allow(message)


# --- subagents ------------------------------------------------------------------------------


def record_agent_event(session: Session, hook: HookInput, kind: AgentEventKind, platform: str) -> Ledger:
    """Append the event under the ledger lock and return the ledger as it was before."""
    event = AgentEvent(
        platform=platform,
        event=kind,
        agent=hook.agent or "unknown",
        agent_id=hook.agent_id,
        at=format_timestamp(datetime.now(timezone.utc)),
    )
    return mutate(session.ledger_path, lambda ledger: (add_agent_event(ledger, event), ledger))


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
    ledger = record_agent_event(session, hook, "start", reply.platform)
    if role is None or not ledger.rounds:
        return reply.allow()
    return reply.inject(briefing(role, ledger, session, review_only=not active))


def briefing(role: AgentRole, ledger: Ledger, session: Session, *, review_only: bool) -> str:
    """What a Craftsman reviewer or verifier needs to know about the current round."""
    latest = ledger.rounds[-1]
    lines = [
        f"Craftsman review round {latest.number}. "
        f"Packet: {latest.packet_path or packet_path(session.ledger_path, latest.number)}",
        f"Scope: work only inside {session.root}; see Search scope.",
    ]
    if role == "reviewer":
        missing = {lens: ids for lens, ids in uncovered_by_lens(ledger, latest).items() if ids}
        if missing:
            lines.append("Lenses with uncovered items (take only the lenses you were assigned):")
            lines.extend(
                f"- {lens}: {', '.join(ids)}" + (" (run last)" if lens == SWEEP_LENS else "")
                for lens, ids in missing.items()
            )
        else:
            lines.append("Every lens of this round is covered.")
    else:
        sweep_missing = uncovered_by_lens(ledger, latest).get(SWEEP_LENS, [])
        if review_only and sweep_missing:
            lines.append(
                f"Uncovered `{SWEEP_LENS}` hunks (in review-only mode without --fix, cover `{SWEEP_LENS}` "
                f"in Step 0 before verdicts): {', '.join(sweep_missing)} "
                f"(with --fix, a separate `craftsman-reviewer` covers `{SWEEP_LENS}`)."
            )
        pending = [finding for finding in ledger.findings if finding.verdict is None]
        if pending:
            lines.append("Findings without a verdict:")
            lines.extend(
                f"- {finding.id} ({finding.severity}, {finding.path}:{finding.line}): {finding.title}"
                for finding in pending
            )
        else:
            lines.append("No finding is waiting for a verdict.")
    lines.append("Recording commands:")
    lines.extend(CHEAT_SHEET)
    return "\n".join(lines)


def on_subagent_stop(session: Session, active: bool, hook: HookInput, reply: Reply) -> Output:
    """Record the stop; send a Craftsman agent back once when it finishes without recording."""
    if not active and not session.ledger_path.is_file():
        return reply.allow()
    hook = _with_started_agent_name(session, hook)
    if not active and (agent_role(hook.agent) is None or not _has_current_round(session)):
        return reply.allow()
    before = record_agent_event(session, hook, "stop", reply.platform)
    role = agent_role(hook.agent)
    if role is None or hook.stop_hook_active or not before.rounds:
        return reply.allow()
    since = _run_started_at(before, hook)
    if _stopped_since(before, hook, since):
        return reply.allow()  # already sent back once
    # Records carry no agent id, so "recorded since this run started" is the best evidence that
    # this run did its part; with parallel runs another agent's record can satisfy it.
    if role == "reviewer":
        recorded = any(parse_timestamp(item.recorded_at) >= since for item in before.coverage) or any(
            parse_timestamp(item.recorded_at) >= since for item in before.findings
        )
        return reply.allow() if recorded else reply.block(REVIEWER_STOP_REASON)
    pending = [finding.id for finding in before.findings if finding.verdict is None]
    judged = any(
        finding.verdict is not None
        and not finding.verdict_evidence.startswith(GROUPED_EVIDENCE_PREFIX)
        and parse_timestamp(finding.updated_at) >= since
        for finding in before.findings
    )
    if not pending or judged:
        return reply.allow()
    return reply.block(
        f"Findings still without a verdict: {', '.join(pending)}. Record one for each with "
        '`craftsman review verdict <id> --as proven|probable|question|dismissed --evidence "..."` '
        "before finishing."
    )


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


def _run_started_at(ledger: Ledger, hook: HookInput) -> datetime:
    """When this subagent run started: its start event, else when the latest round was prepared."""
    starts = [event for event in ledger.agents if event.event == "start"]
    by_id = [event for event in starts if hook.agent_id and event.agent_id == hook.agent_id]
    by_name = [event for event in starts if event.agent == hook.agent]
    matches = by_id or by_name
    return parse_timestamp(matches[-1].at if matches else ledger.rounds[-1].prepared_at)


def _stopped_since(ledger: Ledger, hook: HookInput, since: datetime) -> bool:
    return any(
        event.event == "stop"
        and (event.agent_id == hook.agent_id if hook.agent_id else event.agent == hook.agent)
        and parse_timestamp(event.at) >= since
        for event in ledger.agents
    )


# --- pre-tool -------------------------------------------------------------------------------


def _check_shell_git_guard(hook: HookInput, reply: Reply) -> Output | None:
    if hook.tool_name.lower() not in SHELL_TOOLS:
        return None
    command = hook.tool_input.get("command") or hook.tool_input.get("CommandLine")
    if not isinstance(command, str):
        return None
    tool_cwd = hook.tool_input.get("cwd") or hook.tool_input.get("Cwd")
    cwd = str(Path(hook.cwd, tool_cwd)) if isinstance(tool_cwd, str) and tool_cwd else hook.cwd
    transcript = None
    if reply.platform == "claude" and hook.transcript_path:
        candidate = Path(hook.transcript_path).expanduser()
        transcript = candidate if candidate.is_file() else None
    decision, reason = evaluate_shell_command(command, cwd, transcript)
    if decision == "deny":
        return reply.deny(reason or "Blocked by the Craftsman git guard.")
    if decision == "force_ask":
        reason = reason or "The Craftsman git guard needs your confirmation."
        return reply.ask(reason + COPILOT_PUSH_NOTE if reply.platform == "copilot" else reason)
    return None


REVIEW_IN_PROGRESS_MAX_AGE = timedelta(hours=12)


def _review_in_progress(session: Session) -> int | None:
    """The latest round number when an open review-only round ran within 12 hours, else None."""
    try:
        if not session.ledger_path.is_file():
            return None
        state = load_state(session.state_path)
        if state is not None and state.active:
            return None
        ledger = load(session.ledger_path)
    except (DiffScanError, GateStateError, LedgerError) as exc:
        sys.stderr.write(f"craftsman hook error: {exc}\n")
        return None
    if not ledger.rounds:
        return None
    latest = ledger.rounds[-1]
    if datetime.now(timezone.utc) - parse_timestamp(latest.prepared_at) > REVIEW_IN_PROGRESS_MAX_AGE:
        return None
    has_open = any(uncovered_by_lens(ledger, latest).values()) or any(
        finding.verdict is None for finding in ledger.findings
    )
    return latest.number if has_open else None


def _check_worktree_guard(hook: HookInput, reply: Reply, session: Session | None) -> Output | None:
    if hook.tool_name.lower() not in SHELL_TOOLS:
        return None
    command = hook.tool_input.get("command") or hook.tool_input.get("CommandLine")
    if not isinstance(command, str):
        return None
    tool_cwd = hook.tool_input.get("cwd") or hook.tool_input.get("Cwd")
    cwd = str(Path(hook.cwd, tool_cwd)) if isinstance(tool_cwd, str) and tool_cwd else hook.cwd
    if agent_role(hook.agent) is not None:
        mutation = find_worktree_mutation(command, cwd, subagent=True)
        if mutation is None:
            return None
        return reply.deny(
            "Craftsman reviewers and verifiers must not change the work tree or the index "
            f"({mutation}). Read an old version with `git show <base>:<path>`; put reproductions "
            "in a temp directory."
        )
    if session is None:
        return None
    mutation = find_worktree_mutation(command, cwd, subagent=False)
    if mutation is None:
        return None
    round_number = _review_in_progress(session)
    if round_number is None:
        return None
    return reply.ask(
        f"A Craftsman review is in progress (round {round_number}); {mutation} would change the "
        "files under review. Approve only if you mean it."
    )


def on_pre_tool(hook: HookInput, reply: Reply, session: Session | None, caller: GateOwner) -> Output:
    """Guard shell commands against git and work-tree violations, then enforce search scope."""
    guard_reply = _check_shell_git_guard(hook, reply)
    if guard_reply is not None:
        return guard_reply
    worktree_reply = _check_worktree_guard(hook, reply, session)
    if worktree_reply is not None:
        return worktree_reply
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
        _emit(failure(reply, f"unreadable hook input: {exc}"))
        return 0
    if not isinstance(payload, dict):
        _emit(failure(reply, "the hook input is not a JSON object"))
        return 0
    try:
        out = handle(args.event, args.platform, payload)
    except Exception as exc:  # the platform treats a crashed hook as "allow"; `failure` decides
        out = failure(reply, f"{type(exc).__name__}: {exc}")
    _emit(out)
    return 0


def failure(reply: Reply, problem: str) -> Output:
    """The answer when an event cannot be handled: confirm a tool call, else go on with a warning."""
    sys.stderr.write(f"craftsman hook error: {problem}\n")
    if reply.event == "pre-tool":
        return reply.ask(
            f"The Craftsman guard could not check this tool call ({problem}); confirm it only if "
            "it is safe."
        )
    return reply.allow(
        f"Craftsman {reply.event} hook failed ({problem}); the quality gate was not checked. "
        "Run `craftsman review check` yourself."
    )


if __name__ == "__main__":
    raise SystemExit(main())
