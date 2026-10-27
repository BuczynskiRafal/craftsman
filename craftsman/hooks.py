"""Platform lifecycle hooks: `craftsman hook <event> --platform <copilot|claude>`.

Platform `copilot` serves VS Code Copilot and the GitHub Copilot CLI with one hook file: it reads
both input dialects (snake_case and camelCase keys) and answers with both output shapes in one
JSON object. Platform `claude` reads and answers in the Claude Code format.

Events:
- `prompt`: a `/craftsman` or `ultracraft` prompt starts a gate session, like `craftsman gate --init`.
- `stop`: keeps the agent working while the evidence ledger does not pass the gate, at most
  `max_continuations` times in a row without progress; a new user prompt starts a new streak.
- `subagent-start` / `subagent-stop`: record subagent runs in the ledger (the evidence of reviewer
  isolation), brief Craftsman reviewers and verifiers, and send them back once when they finish
  without recording anything.
- `pre-tool`: applies the git guard to shell commands.

Every event inside an active session records `hooks_seen`, which makes the gate require evidence
that only hooks can provide (an isolated reviewer) on the `standard` and `deep` tiers.

Hooks fail open: a hook must never break the user's session. Input that is not a JSON object, a
working directory outside a git repository, or any unexpected exception results in an allow (`{}`
with exit status 0). An unexpected exception also writes one `craftsman hook error: <type>:
<message>` line to stderr, and a failing stop hook adds a `systemMessage` so the user can see that
the gate was not evaluated.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, NoReturn, get_args

from craftsman.diffscan import DiffScanError, snapshot, worktree_root
from craftsman.evidence import archive_ledger, packet_path
from craftsman.gate import (
    CLAUDE_COMMAND_ARGS_RE,
    TIER_RANK,
    _safe_int,
    archive_ledger_for_new_base,
    continuation_reason,
    default_state,
    evaluate_task,
    get_head_sha,
    is_craftsman_trigger_content,
    load_state,
    mark_completed,
    resolve_cli_state_file,
    save_state_atomic,
)
from craftsman.guard import evaluate_shell_command
from craftsman.ledger import (
    GROUPED_EVIDENCE_PREFIX,
    SWEEP_LENS,
    AgentEvent,
    AgentEventKind,
    Ledger,
    LedgerError,
    add_agent_event,
    format_timestamp,
    ledger_path_for,
    load,
    mutate,
    parse_timestamp,
    uncovered_by_lens,
)
from craftsman.packet import CHEAT_SHEET
from craftsman.scope import evaluate_scope, is_craftsman_agent, load_scope_policy

Platform = Literal["copilot", "claude"]
Event = Literal["prompt", "stop", "subagent-start", "subagent-stop", "pre-tool"]
Role = Literal["reviewer", "verifier"]
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
AGENT_ROLES: dict[str, Role] = {"craftsman-reviewer": "reviewer", "craftsman-verifier": "verifier"}
SHELL_TOOLS = frozenset({"run_in_terminal", "runinterminal", "terminal", "bash", "powershell", "pwsh", "shell"})
DEFAULT_MAX_CONTINUATIONS = 6
GATE_HEADER_PREFIX = "[CRAFTSMAN GATE"
MAX_TASK_CHARS = 300
TRIGGER_TOKEN_RE = re.compile(
    r"(?i)(?<![\w/-])(?:/craftsman(?:\s+--(?:quick|soft|standard|deep))?|ultracraft)(?![\w-])"
)
REVIEWER_STOP_REASON = (
    "Before finishing, record coverage (`craftsman review cover ...`) for every lens you "
    "examined, and a finding (`craftsman review add ...`) for every defect you found."
)
COPILOT_PUSH_NOTE = (
    " Copilot hooks cannot see the chat, so a confirmation there does not unlock this command: "
    "ask the user, and if they choose a direct push, they run it themselves."
)


class UsageError(Exception):
    """The hook command line is invalid; reported without breaking the session."""


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

    @classmethod
    def parse(cls, payload: dict[str, Any]) -> HookInput:
        return cls(
            cwd=_text(payload, "cwd") or os.getcwd(),
            agent=_agent_name(payload),
            agent_id=_text(payload, "agent_id", "agentId"),
            stop_hook_active=any(
                payload.get(key) is True for key in ("stop_hook_active", "stopHookActive")
            ),
            prompt=_text(payload, "prompt"),
            tool_name=_text(payload, "tool_name", "toolName"),
            tool_input=_tool_input(payload),
            transcript_path=_text(payload, "transcript_path", "transcriptPath"),
        )


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
    craftsman = [name for name in names if _role(name) is not None or is_craftsman_agent(name)]
    return (craftsman or names or [""])[0]


def _tool_input(payload: dict[str, Any]) -> dict[str, Any]:
    raw: Any = next(
        (payload[key] for key in ("tool_input", "toolArgs", "toolInput") if payload.get(key)), {}
    )
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return {}
    return raw if isinstance(raw, dict) else {}


def _role(agent: str) -> Role | None:
    lowered = agent.lower()
    return next((role for marker, role in AGENT_ROLES.items() if marker in lowered), None)


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
        if self.platform == "copilot":
            return self._permission("deny", reason + COPILOT_PUSH_NOTE)
        return self._permission("ask", reason)

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


@dataclass
class Session:
    """The work tree a hook runs in and its gate state file."""

    root: Path
    state_file: Path
    state: dict[str, Any] | None

    @property
    def active(self) -> bool:
        return bool(self.state and self.state.get("active"))

    @property
    def ledger_path(self) -> Path:
        return ledger_path_for(self.root)

    def save(self) -> None:
        if self.state is None:
            return
        try:
            save_state_atomic(self.state_file, self.state)
        except OSError as exc:
            sys.stderr.write(f"craftsman hook warning: cannot save {self.state_file}: {exc}\n")


def open_session(cwd: str) -> Session | None:
    """The session for the work tree containing `cwd`, or None outside a git work tree."""
    try:
        root = worktree_root(cwd)
    except DiffScanError:
        return None
    state_file = resolve_cli_state_file(None, str(root))
    return Session(root=root, state_file=state_file, state=load_state(state_file))


def handle(event: Event, platform: Platform, payload: dict[str, Any]) -> Output:
    hook = HookInput.parse(payload)
    reply = Reply(platform, event)
    if event == "pre-tool":
        session = open_session(hook.cwd)
        out = on_pre_tool(hook, reply, session)
        if session is not None:
            mark_hooks_seen(session, platform)
        return out
    session = open_session(hook.cwd)
    if session is None:
        return reply.allow()
    mark_hooks_seen(session, platform)
    if event == "prompt":
        return on_prompt(session, hook, reply)
    if event == "stop":
        return on_stop(session, hook, reply)
    if event == "subagent-start":
        return on_subagent_start(session, hook, reply)
    return on_subagent_stop(session, hook, reply)


def mark_hooks_seen(session: Session, platform: Platform) -> None:
    if session.active and session.state is not None and session.state.get("hooks_seen") != platform:
        session.state["hooks_seen"] = platform
        session.save()


# --- prompt ---------------------------------------------------------------------------------


def on_prompt(session: Session, hook: HookInput, reply: Reply) -> Output:
    triggered, tier = is_craftsman_trigger_content(hook.prompt)
    if session.active and session.state is not None:
        state = session.state
        if not hook.prompt.lstrip().startswith(GATE_HEADER_PREFIX):
            # The user spoke: their answer ends any wait, and the stop hook gets a fresh streak.
            state["awaiting_user"] = False
            state["forced_continuations"] = 0
            state.pop("best_issue_count", None)
        restart = triggered and state.get("stage") == "needs_human"
        if triggered and not restart and TIER_RANK.get(tier, 1) > TIER_RANK.get(str(state.get("tier")), 1):
            state["tier"] = tier
        session.save()
        if not restart:
            return reply.allow()
        archive_ledger(session.ledger_path)  # the capped review needed a human; start over
    elif not triggered:
        return reply.allow()
    root = str(session.root)
    base = get_head_sha(root)
    state = default_state(
        mode="ultracraft" if tier == "deep" else "craftsman", tier=tier, cwd=root, base_sha=base
    )
    state["task"] = task_from_prompt(hook.prompt)
    state["hooks_seen"] = reply.platform
    if base:
        archive_ledger_for_new_base(root, base)
    save_state_atomic(session.state_file, state)
    short_base = base[:7] if base else "none (no commits yet)"
    return reply.inject(
        f"Craftsman session started (tier {state['tier']}, base {short_base}). Follow the /craftsman "
        "skill; the gate will block finishing until `craftsman review check` passes.\n"
        f"Scope: work only inside {session.root.resolve()}; see Search scope."
    )


def task_from_prompt(prompt: str) -> str:
    """The user's request without the trigger, as a one-line task description."""
    command_args = CLAUDE_COMMAND_ARGS_RE.search(prompt)
    text = f"/craftsman {command_args.group(1)}" if command_args else prompt
    return " ".join(TRIGGER_TOKEN_RE.sub(" ", text).split())[:MAX_TASK_CHARS]


# --- stop -----------------------------------------------------------------------------------


def on_stop(session: Session, hook: HookInput, reply: Reply) -> Output:
    state = session.state
    if state is None or not session.active or state.get("awaiting_user"):
        return reply.allow()
    try:
        report, error = evaluate_task(state, str(session.root)).report, None
    except (DiffScanError, LedgerError) as exc:
        report, error = None, str(exc)

    if report is not None and any(issue.code == "round-cap" for issue in report.issues):
        state["stage"] = "needs_human"
        session.save()
        return notice(
            reply,
            "Craftsman gate: the review reached its round cap. The remaining issues need your "
            "decision; `craftsman review report` lists them. Start `/craftsman` again to restart.",
        )
    if report is not None and report.passed:
        mark_completed(state, _safe_int(state.get("started_at_step"), 0))
        session.save()
        return reply.allow()

    # Progress means fewer open issues than ever before in this streak; an issue count that
    # merely oscillates must not keep the agent looping.
    issue_count = len(report.issues) if report is not None else 1
    forced = _safe_int(state.get("forced_continuations"), 0)
    best = state.get("best_issue_count")
    if isinstance(best, int) and issue_count < best:
        forced = 0
    state["best_issue_count"] = min(best, issue_count) if isinstance(best, int) else issue_count
    max_continuations = max(1, _safe_int(state.get("max_continuations"), DEFAULT_MAX_CONTINUATIONS))
    if forced >= max_continuations:
        state["active"] = False
        state["stage"] = "aborted_max_continuations"
        session.save()
        return notice(
            reply,
            f"Craftsman gate not satisfied: the agent stopped after {forced} forced continuations "
            "without passing `craftsman review check`. Run it to see what is still missing.",
        )
    state["forced_continuations"] = forced + 1
    session.save()
    header = f"[CRAFTSMAN GATE {forced + 1}/{max_continuations}]"
    return reply.block(continuation_reason(header, report, error, missing_subagent=False))


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


def _has_current_round(session: Session, ledger: Ledger) -> bool:
    """True when `ledger` has a round whose diff fingerprint matches the current work tree."""
    if not ledger.rounds:
        return False
    latest = ledger.rounds[-1]
    try:
        return snapshot(session.root, latest.base_sha).fingerprint == latest.fingerprint
    except DiffScanError:
        return False


def on_subagent_start(session: Session, hook: HookInput, reply: Reply) -> Output:
    role = _role(hook.agent)
    if not session.active:
        if role is None or not session.ledger_path.is_file():
            return reply.allow()
        if not _has_current_round(session, load(session.ledger_path)):
            return reply.allow()
    ledger = record_agent_event(session, hook, "start", reply.platform)
    if role is None or not ledger.rounds:
        return reply.allow()
    return reply.inject(
        briefing(
            role,
            ledger,
            session.ledger_path,
            review_only=not session.active,
            repo_root=session.root,
        )
    )


def _infer_repo_root_from_ledger_path(ledger_path: Path) -> Path:
    resolved = ledger_path.resolve()
    if resolved.parent.name == "craftsman" and resolved.parent.parent.name in (".git", ".craftsman"):
        return resolved.parent.parent.parent
    return resolved.parent


def briefing(
    role: Role,
    ledger: Ledger,
    ledger_path: Path,
    *,
    review_only: bool = False,
    repo_root: Path | None = None,
) -> str:
    """What a Craftsman reviewer or verifier needs to know about the current round."""
    latest = ledger.rounds[-1]
    effective_root = (repo_root.resolve() if repo_root is not None else _infer_repo_root_from_ledger_path(ledger_path))
    lines = [
        f"Craftsman review round {latest.number}. "
        f"Packet: {latest.packet_path or packet_path(ledger_path, latest.number)}",
        f"Scope: work only inside {effective_root}; see Search scope.",
    ]
    if role == "reviewer":
        missing = {lens: ids for lens, ids in uncovered_by_lens(ledger, latest).items() if ids}
        if missing:
            lines.append("Lenses with uncovered hunks (take only the lenses you were assigned):")
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


def on_subagent_stop(session: Session, hook: HookInput, reply: Reply) -> Output:
    if not session.active:
        if not session.ledger_path.is_file():
            return reply.allow()
        hook = _with_started_agent_name(session, hook)
        if _role(hook.agent) is None or not _has_current_round(session, load(session.ledger_path)):
            return reply.allow()
    else:
        hook = _with_started_agent_name(session, hook)
    before = record_agent_event(session, hook, "stop", reply.platform)
    role = _role(hook.agent)
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


def on_pre_tool(hook: HookInput, reply: Reply, session: Session | None = None) -> Output:
    """Apply the git guard to shell commands, then repository search-scope rules when Craftsman is active."""
    if hook.tool_name.lower() in SHELL_TOOLS:
        command = hook.tool_input.get("command") or hook.tool_input.get("CommandLine")
        if isinstance(command, str):
            tool_cwd = hook.tool_input.get("cwd") or hook.tool_input.get("Cwd")
            cwd = (
                str(Path(hook.cwd, tool_cwd))
                if isinstance(tool_cwd, str) and tool_cwd
                else hook.cwd
            )
            transcript = None
            if reply.platform == "claude" and hook.transcript_path:
                candidate = Path(hook.transcript_path).expanduser()
                transcript = candidate if candidate.is_file() else None
            decision, reason = evaluate_shell_command(command, cwd, transcript)
            if decision == "deny":
                return reply.deny(reason or "Blocked by the Craftsman git guard.")
            if decision == "force_ask":
                return reply.ask(reason or "The Craftsman git guard needs your confirmation.")

    if session is None:
        session = open_session(hook.cwd)
    if session is None or (not session.active and not is_craftsman_agent(hook.agent)):
        return reply.allow()

    policy = load_scope_policy(Path(hook.cwd), session.root)
    if policy.mode == "off":
        return reply.allow()
    scope_decision = evaluate_scope(
        hook.tool_name, hook.tool_input, Path(hook.cwd), session.root, policy
    )
    if scope_decision.decision == "deny":
        if policy.mode == "warn":
            return reply.warn(scope_decision.reason)
        return reply.deny(scope_decision.reason)
    return reply.allow()


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
        sys.stderr.write(f"craftsman hook error: UsageError: {exc}\n")
        _emit({})
        return 0
    event: Event = args.event
    platform: Platform = args.platform
    try:
        raw = _read_stdin()
        payload = json.loads(raw) if raw.strip() else {}
    except (OSError, ValueError) as exc:
        sys.stderr.write(f"craftsman hook: ignoring unreadable hook input: {exc}\n")
        payload = None
    if not isinstance(payload, dict):
        _emit({})
        return 0
    try:
        out = handle(event, platform, payload)
    except Exception as exc:  # fail open by design, see the module docstring
        sys.stderr.write(f"craftsman hook error: {type(exc).__name__}: {exc}\n")
        out = {}
        if event == "stop":
            out = Reply(platform, event).allow(
                f"Craftsman gate hook failed ({type(exc).__name__}: {exc}); the quality gate was "
                "not checked. Run `craftsman review check` yourself."
            )
    _emit(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
