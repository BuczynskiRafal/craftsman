"""Token and time telemetry per subagent, best effort.

`read_usage` sums the token usage in a subagent's JSONL transcript (Claude Code writes one
assistant entry per content block, all repeating the usage of their message, so each message
counts once). `role_totals` adds up what the ledger recorded per role for `review report` and
`craftsman status`. Neither ever raises: telemetry must not fail a hook or a report.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from craftsman.records import AgentRole, AgentUsage, Ledger, agent_role, parse_timestamp

MAX_TRANSCRIPT_BYTES = 256 * 1024 * 1024
ROLE_LABELS: Mapping[AgentRole, str] = {"reviewer": "hunters", "verifier": "verifiers"}
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


def read_usage(path: Path) -> AgentUsage | None:
    """The tokens and the time span in the transcript at `path`; None when it has no usage."""
    try:
        return _read_usage(path)
    except Exception:  # noqa: BLE001 - best effort: any unreadable transcript just has no telemetry
        return None


def _read_usage(path: Path) -> AgentUsage | None:
    if path.stat().st_size > MAX_TRANSCRIPT_BYTES:
        return None
    by_message: dict[str, dict[str, int]] = {}
    anonymous: list[dict[str, int]] = []
    first: datetime | None = None
    last: datetime | None = None
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            entry = _entry(line)
            if entry is None:
                continue
            stamp = _moment(entry.get("timestamp"))
            if stamp is not None:
                first = stamp if first is None or stamp < first else first
                last = stamp if last is None or stamp > last else last
            message = entry.get("message")
            if entry.get("type") != "assistant" or not isinstance(message, dict):
                continue
            usage = message.get("usage")
            if not isinstance(usage, dict):
                continue
            counts = {name: _count(usage.get(key)) for key, name in _TOKEN_KEYS}
            message_id = message.get("id")
            if isinstance(message_id, str) and message_id:
                seen = by_message.setdefault(message_id, dict.fromkeys(counts, 0))
                for name, value in counts.items():
                    seen[name] = max(seen[name], value)
            else:
                anonymous.append(counts)
    messages = [*by_message.values(), *anonymous]
    if not messages:
        return None
    totals = {name: sum(message[name] for message in messages) for _, name in _TOKEN_KEYS}
    span = 0.0 if first is None or last is None else (last - first).total_seconds()
    return AgentUsage(
        input_tokens=totals["input_tokens"],
        output_tokens=totals["output_tokens"],
        cache_read_tokens=totals["cache_read_tokens"],
        cache_write_tokens=totals["cache_write_tokens"],
        duration_s=max(0.0, span),
    )


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
