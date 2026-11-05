"""Token and time telemetry per subagent, and the records a subagent's transcript shows, best effort.

`read_transcript` makes one pass over a subagent's JSONL transcript: it sums the token usage
(Claude Code writes one assistant entry per content block, all repeating the usage of their message,
so each message counts once) and lists the record tokens (`C3`, `F2`) that the agent's successful
`craftsman review cover` and `review add` calls printed, which is how the gate knows what a
reviewer recorded. `read_usage` is its usage half. `role_totals` adds up what the ledger recorded
per role for `review report` and `craftsman status`. None of them ever raises: telemetry must not
fail a hook or a report.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from craftsman.records import AgentRole, AgentUsage, Ledger, agent_role, parse_timestamp

MAX_TRANSCRIPT_BYTES = 256 * 1024 * 1024
# A longer pause between two transcript entries is the agent being idle (a resumed agent waits for
# its parent between runs), not working.
IDLE_GAP_S = 300.0
ROLE_LABELS: Mapping[AgentRole, str] = {"reviewer": "hunters", "verifier": "verifiers"}
# What a successful recording call prints; see `review._record_coverage` and `review._add_one`.
_COVERAGE_TOKEN_RE = re.compile(r"Coverage recorded \((C\d+)\)")
_FINDING_TOKEN_RE = re.compile(r"\b(F\d+) recorded \(")
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

    `records` is None when the transcript cannot vouch for them: it was unreadable, oversized, had
    a line that is not JSON, or a recording call whose outcome is unclear (no result, or output
    that shows neither a token nor that nothing was recorded, e.g. because it was piped away).
    """

    usage: AgentUsage | None = None
    records: tuple[str, ...] | None = None


def read_usage(path: Path) -> AgentUsage | None:
    """The tokens and the active time in the transcript at `path`; None when it has no usage."""
    return read_transcript(path).usage


def read_transcript(path: Path) -> TranscriptFacts:
    """Usage and records of the transcript at `path`; never raises.

    Active time adds up the gaps between entries shorter than `IDLE_GAP_S`.
    """
    try:
        return _read_transcript(path)
    except Exception:  # noqa: BLE001 - best effort: any unreadable transcript just has no telemetry
        return TranscriptFacts()


def _read_transcript(path: Path) -> TranscriptFacts:
    if path.stat().st_size > MAX_TRANSCRIPT_BYTES:
        return TranscriptFacts()
    by_message: dict[str, dict[str, int]] = {}
    anonymous: list[dict[str, int]] = []
    previous: datetime | None = None
    active_s = 0.0
    calls: set[str] = set()  # recording calls still waiting for their result
    tokens: list[str] = []
    vouched = True
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            entry = _entry(line)
            if entry is None:
                vouched = vouched and not line.strip()
                continue
            stamp = _moment(entry.get("timestamp"))
            if stamp is not None:
                if previous is not None:
                    gap = (stamp - previous).total_seconds()
                    active_s += gap if 0 < gap <= IDLE_GAP_S else 0.0
                previous = stamp if previous is None or stamp > previous else previous
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
    records = tuple(dict.fromkeys(tokens)) if vouched and not calls else None
    messages = [*by_message.values(), *anonymous]
    if not messages:
        return TranscriptFacts(None, records)
    totals = {name: sum(message[name] for message in messages) for _, name in _TOKEN_KEYS}
    usage_total = AgentUsage(
        input_tokens=totals["input_tokens"],
        output_tokens=totals["output_tokens"],
        cache_read_tokens=totals["cache_read_tokens"],
        cache_write_tokens=totals["cache_write_tokens"],
        duration_s=active_s,
    )
    return TranscriptFacts(usage_total, records)


def _follow_recording_calls(content: Any, calls: set[str], tokens: list[str]) -> bool:
    """Track the `review cover` / `review add` calls in a message's content blocks.

    A call joins `calls` until its result arrives; the tokens of a successful one join `tokens`.
    False when a call cannot be paired or its output cannot be read.
    """
    if not isinstance(content, list):
        return True
    clear = True
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_use" and _is_recording_call(block.get("input")):
            call_id = block.get("id")
            if isinstance(call_id, str) and call_id:
                calls.add(call_id)
            else:
                clear = False
        elif block.get("type") == "tool_result" and block.get("tool_use_id") in calls:
            calls.discard(block["tool_use_id"])
            output = _result_text(block.get("content"))
            # A token is printed only after the record is written, so it counts even when a later
            # part of the command failed (`craftsman review cover ... && false`).
            found = _COVERAGE_TOKEN_RE.findall(output) + _FINDING_TOKEN_RE.findall(output)
            tokens.extend(found)
            if block.get("is_error") is not True:
                clear = clear and (bool(found) or any(mark in output for mark in _NOTHING_RECORDED))
    return clear


def _is_recording_call(tool_input: Any) -> bool:
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    return isinstance(command, str) and ("review cover" in command or "review add" in command)


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
