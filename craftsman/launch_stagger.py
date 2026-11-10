"""Hold back hunters and verifiers that start together, so that all but the first read the prompt cache.

A subagent's request can read a prompt-cache entry only once the response that wrote it has begun, and
hunters launched in one message send their first requests at the same moment, so each of them would
write the whole starting context. The SubagentStart hook (`hooks.on_subagent_start`) therefore holds back
every start of a burst but the first (`hold_back`), and `hooks.handle` waits that time out once it holds
no lock (`release`), just before the hook answers. Nothing the agents read or do changes.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from contextvars import ContextVar
from datetime import timedelta

from craftsman.diffscan import git_time_left
from craftsman.records import AgentEvent, AgentRole, agent_role, parse_timestamp

# Measured on Claude Code 2.1.322 (Sonnet): three craftsman-reviewer subagents launched in one message
# each wrote 6,917 tokens of starting context to the prompt cache and read none, while one started later
# read 6,920 tokens and wrote none. A cache write costs 1.25 times the input price, a read at most 0.1
# times. Five seconds lets the first start's response begin, and its prefix be cached, before the others
# send their first request.
STAGGER_S = 5.0
# Only Claude Code was measured; other platforms start their agents as they always did.
STAGGERED_PLATFORMS = ("claude",)

_HELD_BACK: ContextVar[float] = ContextVar("craftsman_launch_stagger", default=0.0)


def stagger_s(agents: Sequence[AgentEvent], start: AgentEvent) -> float:
    """How long `start` waits, given the agent events recorded before it.

    A start of a Craftsman role on a staggered platform waits until the earliest start of the same role on
    the same platform within `STAGGER_S` seconds of it is `STAGGER_S` old; the first start of a burst does
    not wait. "Within" counts both ways: hooks of one burst take their time before the ledger lock, so a
    start recorded earlier can carry a slightly later time. Roles do not hold each other back: their
    prompts differ, so they share no cache entry. A resumed agent (its id started before) does not wait:
    its own context is cached already.
    """
    role = agent_role(start.agent)
    if role is None or start.platform not in STAGGERED_PLATFORMS:
        return 0.0
    starts = [event for event in agents if _is_start(event, role, start.platform)]
    if start.agent_id and any(event.agent_id == start.agent_id for event in starts):
        return 0.0
    now = parse_timestamp(start.at)
    window = timedelta(seconds=STAGGER_S)
    near = [at for at in (parse_timestamp(event.at) for event in starts) if now - window < at < now + window]
    if not near:
        return 0.0
    return min(STAGGER_S, (min(near) + window - now).total_seconds())


def _is_start(event: AgentEvent, role: AgentRole, platform: str) -> bool:
    return event.event == "start" and event.platform == platform and agent_role(event.agent) == role


def hold_back(agents: Sequence[AgentEvent], start: AgentEvent) -> None:
    """Note the wait (`stagger_s`) of `start` for `release`; any error means no wait."""
    try:
        seconds = stagger_s(agents, start)
    except Exception:  # an optimization: it must never fail the hook
        seconds = 0.0
    _HELD_BACK.set(seconds)


def forget() -> None:
    """Drop a wait `hold_back` noted, without waiting it out."""
    _HELD_BACK.set(0.0)


def release() -> None:
    """Wait out what `hold_back` noted in this hook run, if anything. Call it holding no lock.

    The wait ends within the hook's git budget, which ends a margin before the platform kills the hook
    (`hook_budget`), so the hook still answers in time after slow git calls.
    """
    seconds = _HELD_BACK.get()
    forget()
    try:
        seconds = min(seconds, git_time_left(STAGGER_S))
        if seconds > 0:
            sleep(seconds)
    except Exception:  # an optimization: it must never fail the hook
        pass


def sleep(seconds: float) -> None:
    """The wait itself; tests replace it."""
    time.sleep(seconds)
