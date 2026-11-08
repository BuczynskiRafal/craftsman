"""Token and time telemetry per subagent, and the records a subagent's transcript shows, best effort.

`read_transcript` makes one pass over a subagent's JSONL transcript: it sums the token usage
(Claude Code writes one assistant entry per content block, all repeating the usage of their message,
so each message counts once) and lists the record tokens (`C3`, `F2`) that the agent's successful
`craftsman review cover` and `review add` calls printed, plus the `B` token of a `review brief` and the `V`
token of a `review verdict` or `merge`, which is how the gate knows what a hunter or a verifier recorded. `read_usage` is its usage half. `role_totals` adds up what the ledger recorded
per role for `review report` and `craftsman status`. None of them ever raises: telemetry must not
fail a hook or a report.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from craftsman.command_policy import BRIEF_OPTIONS, has_option, invocations
from craftsman.records import AgentRole, AgentUsage, Ledger, agent_role, parse_timestamp
from craftsman.shell import SimpleCommand, iter_simple_commands

MAX_TRANSCRIPT_BYTES = 256 * 1024 * 1024
# A longer pause between two transcript entries is the agent being idle (a resumed agent waits for
# its parent between runs), not working.
IDLE_GAP_S = 300.0
ROLE_LABELS: Mapping[AgentRole, str] = {"reviewer": "hunters", "verifier": "verifiers"}
# What a successful recording call prints; see `review._record_coverage`, `review._add_one`, `review._brief`
# (`Brief issued (B1)`) and `review._print_verdict` (`F3 verdict recorded (V7)`, also printed by `merge`).
_COVERAGE_TOKEN_RE = re.compile(r"Coverage recorded \((C\d+)\)")
_FINDING_TOKEN_RE = re.compile(r"\b(F\d+) recorded \(")
_CLAIM_TOKEN_RE = re.compile(r"Brief issued \((B\d+)\)")
_VERDICT_TOKEN_RE = re.compile(r"verdict recorded \((V\d+)\)")
_TOKEN_RES = (_COVERAGE_TOKEN_RE, _FINDING_TOKEN_RE, _CLAIM_TOKEN_RE, _VERDICT_TOKEN_RE)
# The `craftsman review` subcommands whose output is the proof of a record.
_RECORDING_SUBCOMMANDS = frozenset({"cover", "add", "brief", "verdict", "merge"})
# Commands that print nothing a transcript reader could take for a record: they may share a command line with a
# recording call (`cd repo && craftsman review cover ...`), and so may `tail` / `head` / `cat` filtering its output.
_SILENT_COMMANDS = frozenset({"cd", "pushd", "popd", "export", "set", "unset", "true", "false", ":", "test", "[", "exit"})
_ECHOES = frozenset({"echo", "printf"})
_STDIN_FILTERS = frozenset({"tail", "head", "cat"})
# Shells that only run the commands inside them, which are read on their own.
_SHELL_RUNNERS = frozenset({"bash", "sh", "zsh", "dash", "ksh", "eval"})
# Output of a recording call that recorded nothing, which is not a failure to read it.
_NOTHING_RECORDED = ("Nothing to record", "Check passed")
_TOKEN_KEYS = (
    ("input_tokens", "input_tokens"),
    ("output_tokens", "output_tokens"),
    ("cache_read_input_tokens", "cache_read_tokens"),
    ("cache_creation_input_tokens", "cache_write_tokens"),
)


@dataclass(frozen=True)
class RoleUsage:
    """The usage of every agent of one role."""

    agents: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    duration_s: float

    def to_json(self) -> dict[str, Any]:
        return {
            "agents": self.agents,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "duration_s": round(self.duration_s, 1),
        }

    def describe(self) -> str:
        return (
            f"{self.agents} agent(s) · in {_compact(self.input_tokens)} · out {_compact(self.output_tokens)}"
            f" · cache read {_compact(self.cache_read_tokens)} · cache write {_compact(self.cache_write_tokens)}"
            f" · {self.duration_s / 60:.1f} min agent time"
        )


@dataclass(frozen=True)
class TranscriptFacts:
    """What one transcript tells: the agent's usage and the records it made.

    `records` is None when the transcript cannot vouch for them: it was unreadable, oversized, had a line that
    is not JSON, or a recording call whose outcome is unclear (no result, or output that shows neither a token
    nor that nothing was recorded, e.g. because it was piped away), so it fails safe to the time window instead
    of proving the agent recorded nothing. Only the tokens that a recognized recording call printed count: text
    the agent merely read (a document that quotes `Coverage recorded (C1)`) is not its record, and a recording
    made through a wrapper this reader does not know is not vouched.

    `problem` says why a transcript could not be read (it was unreadable, oversized or had a line that is not
    JSON); the hooks log it. A fail-safe `records` of None for an unclear call is not a problem.
    """

    usage: AgentUsage | None = None
    records: tuple[str, ...] | None = None
    problem: str | None = None


def read_usage(path: Path) -> AgentUsage | None:
    """The tokens and the active time in the transcript at `path`; None when it has no usage."""
    return read_transcript(path).usage


def read_transcript(path: Path) -> TranscriptFacts:
    """Usage and records of the transcript at `path`; never raises.

    Active time adds up the gaps between entries shorter than `IDLE_GAP_S`.
    """
    try:
        return _read_transcript(path)
    except Exception as exc:  # noqa: BLE001 - best effort: any unreadable transcript just has no telemetry
        return TranscriptFacts(problem=f"cannot read the transcript {path}: {type(exc).__name__}: {exc}")


def _read_transcript(path: Path) -> TranscriptFacts:
    if path.stat().st_size > MAX_TRANSCRIPT_BYTES:
        return TranscriptFacts(problem=f"the transcript {path} is larger than {MAX_TRANSCRIPT_BYTES // (1024 * 1024)} MB; not read")
    by_message: dict[str, dict[str, int]] = {}
    anonymous: list[dict[str, int]] = []
    previous: datetime | None = None
    active_s = 0.0
    calls: dict[str, bool] = {}  # recording calls still waiting for their result: may it print no token?
    tokens: list[str] = []
    vouched = True
    bad_line = False
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            entry = _entry(line)
            if entry is None:
                if line.strip():
                    vouched = False
                    bad_line = True
                continue
            stamp = _moment(entry.get("timestamp"))
            if stamp is not None:
                if previous is not None:
                    gap = (stamp - previous).total_seconds()
                    active_s += gap if 0 < gap <= IDLE_GAP_S else 0.0
                previous = stamp if previous is None or stamp > previous else previous
            vouched = _follow_copilot_event(entry, calls, tokens) and vouched
            message = entry.get("message")
            if not isinstance(message, dict):
                continue
            vouched = _follow_recording_calls(message.get("content"), calls, tokens) and vouched
            usage = message.get("usage")
            if entry.get("type") != "assistant" or not isinstance(usage, dict):
                continue
            counts = {name: _count(usage.get(key)) for key, name in _TOKEN_KEYS}
            message_id = message.get("id")
            if isinstance(message_id, str) and message_id:
                seen = by_message.setdefault(message_id, dict.fromkeys(counts, 0))
                for name, value in counts.items():
                    seen[name] = max(seen[name], value)
            else:
                anonymous.append(counts)
    vouched = vouched and not calls
    records = tuple(dict.fromkeys(tokens)) if vouched else None
    problem = f"the transcript {path} has a line that is not JSON" if bad_line else None
    messages = [*by_message.values(), *anonymous]
    if not messages:
        return TranscriptFacts(None, records, problem)
    totals = {name: sum(message[name] for message in messages) for _, name in _TOKEN_KEYS}
    usage_total = AgentUsage(
        input_tokens=totals["input_tokens"],
        output_tokens=totals["output_tokens"],
        cache_read_tokens=totals["cache_read_tokens"],
        cache_write_tokens=totals["cache_write_tokens"],
        duration_s=active_s,
    )
    return TranscriptFacts(usage_total, records, problem)


def _follow_recording_calls(content: Any, calls: dict[str, bool], tokens: list[str]) -> bool:
    """Track the recording calls (`review cover|add|brief|verdict|merge`) in a Claude message's content blocks.

    A call joins `calls` until its result arrives; the tokens of a successful one join `tokens`.
    False when a call cannot be paired or its output cannot be read.
    """
    if not isinstance(content, list):
        return True
    clear = True
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_use":
            clear = _start_call(block.get("id"), block.get("input"), calls) and clear
        elif block.get("type") == "tool_result" and block.get("tool_use_id") in calls:
            clear = _finish_call(
                block["tool_use_id"], _result_text(block.get("content")), block.get("is_error") is True, calls, tokens
            ) and clear
    return clear


def _follow_copilot_event(entry: dict[str, Any], calls: dict[str, bool], tokens: list[str]) -> bool:
    """The same tracking for the events of a Copilot CLI session log: `tool.execution_start` (the call's
    `arguments`) and `tool.execution_complete` (its `result`), paired by `toolCallId`."""
    data = entry.get("data")
    if not isinstance(data, dict):
        return True
    call_id = data.get("toolCallId")
    if entry.get("type") == "tool.execution_start":
        return _start_call(call_id, data.get("arguments"), calls)
    if entry.get("type") == "tool.execution_complete" and call_id in calls:
        result = data.get("result")
        text = result if isinstance(result, str) else _result_text(result.get("content")) if isinstance(result, dict) else ""
        return _finish_call(str(call_id), text, data.get("success") is False, calls, tokens)
    return True


def _start_call(call_id: Any, tool_input: Any, calls: dict[str, bool]) -> bool:
    """Register a tool call that is a recording call; False when it has no id to pair its result with, or when its
    command line holds a command whose output could pass for a record."""
    recording = _recording_call(tool_input)
    if recording is None:
        return True
    if recording.foreign_output:
        return False
    if isinstance(call_id, str) and call_id:
        calls[call_id] = recording.prints_no_token
        return True
    return False


def _finish_call(call_id: str, output: str, is_error: bool, calls: dict[str, bool], tokens: list[str]) -> bool:
    """Settle a recording call with its result; False when a successful call's output shows neither a token nor
    that nothing was recorded."""
    tokenless = calls.pop(call_id)
    # A token is printed only after the record is written, so it counts even when a later
    # part of the command failed (`craftsman review cover ... && false`).
    found = [token for pattern in _TOKEN_RES for token in pattern.findall(output)]
    tokens.extend(found)
    return is_error or bool(found) or tokenless or any(mark in output for mark in _NOTHING_RECORDED)


@dataclass(frozen=True)
class _RecordingCall:
    """A shell command line that runs `craftsman review cover|add|brief|verdict|merge`.

    `prints_no_token` is true when every such command is a `brief --verifier` (it records nothing); `foreign_output`
    when the line holds another command whose output could carry a token the recording call did not print.
    """

    prints_no_token: bool
    foreign_output: bool


def _recording_call(tool_input: Any) -> _RecordingCall | None:
    """Read a tool call's command line with the parser of the PreToolUse rules: None unless it really runs a
    recording command, so text that merely names one (`grep -rn "craftsman review cover" docs/`) is no call."""
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str):
        return None
    commands = iter_simple_commands(command)
    recording = [
        call
        for call in invocations(commands)
        if call.kind == "cli" and len(call.args) >= 2 and call.args[0] == "review" and call.args[1] in _RECORDING_SUBCOMMANDS
    ]
    if not recording:
        return None
    mine = {id(call.command) for call in recording}
    verifier_briefs = [
        call for call in recording if call.args[1] == "brief" and has_option(call.args[2:], "--verifier", BRIEF_OPTIONS)
    ]
    return _RecordingCall(
        prints_no_token=len(verifier_briefs) == len(recording),
        foreign_output=any(id(item) not in mine and _prints_text(item) for item in commands),
    )


def _prints_text(item: SimpleCommand) -> bool:
    """Whether a simple command that is no recording call may print text of its own."""
    if not item.tokens:
        return False  # only variable assignments
    name = os.path.basename(item.tokens[0]).lower()
    if name in _SILENT_COMMANDS or name in _SHELL_RUNNERS:
        return False  # a shell runner's inner commands are listed (and judged) on their own
    if name in _ECHOES:  # a literal can say "ok", but not forge the phrase a recording call prints
        return any(pattern.search(" ".join(item.tokens[1:])) for pattern in _TOKEN_RES)
    if name in _STDIN_FILTERS:
        return any(not word.startswith("-") and not word.isdigit() for word in item.tokens[1:])  # a file to read
    return True


def _result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(item.get("text") or "") for item in content if isinstance(item, dict) and item.get("type") == "text"
        )
    return ""


def _entry(line: str) -> dict[str, Any] | None:
    if not line.strip():
        return None
    try:
        entry = json.loads(line)
    except json.JSONDecodeError:
        return None
    return entry if isinstance(entry, dict) else None


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _moment(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return parse_timestamp(value)
    except ValueError:
        return None


def role_totals(ledger: Ledger) -> dict[AgentRole, RoleUsage]:
    """Usage per role: the latest stop event with usage of each agent id, summed.

    An agent sent back once stops twice and its transcript then holds both runs, so only its
    latest stop counts. A stop without an agent id cannot be matched and counts as its own agent.
    """
    latest: dict[tuple[AgentRole, str], AgentUsage] = {}
    for position, event in enumerate(ledger.agents):
        role = agent_role(event.agent)
        if event.event != "stop" or event.usage is None or role is None:
            continue
        latest[(role, event.agent_id or f"#{position}")] = event.usage
    totals: dict[AgentRole, RoleUsage] = {}
    roles: tuple[AgentRole, ...] = ("reviewer", "verifier")
    for wanted in roles:
        used = [usage for (kind, _), usage in latest.items() if kind == wanted]
        if used:
            totals[wanted] = RoleUsage(
                agents=len(used),
                input_tokens=sum(usage.input_tokens for usage in used),
                output_tokens=sum(usage.output_tokens for usage in used),
                cache_read_tokens=sum(usage.cache_read_tokens for usage in used),
                cache_write_tokens=sum(usage.cache_write_tokens for usage in used),
                duration_s=sum(usage.duration_s for usage in used),
            )
    return totals


def usage_json(ledger: Ledger) -> dict[str, dict[str, Any]]:
    """`role_totals` for JSON output, keyed by the role's label (`hunters`, `verifiers`)."""
    return {ROLE_LABELS[role]: total.to_json() for role, total in role_totals(ledger).items()}


def usage_lines(ledger: Ledger) -> list[str]:
    """One line per role that has telemetry; none when no agent recorded any."""
    return [f"- {ROLE_LABELS[role]}: {total.describe()}" for role, total in role_totals(ledger).items()]


def _compact(count: int) -> str:
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 1_000:
        return f"{count / 1_000:.1f}k"
    return str(count)


def usage_summary(usage: Mapping[str, Mapping[str, Any]]) -> str:
    """The `usage_json` totals as a short tail for the status line, e.g. `; hunters 5 (45.6k out, 14.2 min)`."""
    return "".join(
        f"; {label} {item['agents']} ({_compact(item['output_tokens'])} out, {item['duration_s'] / 60:.1f} min)"
        for label, item in usage.items()
    )
