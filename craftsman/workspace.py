"""The work tree a `craftsman review` or `craftsman run` command works in."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from craftsman.diffscan import DiffSnapshot, resolve_base_sha, snapshot, worktree_root
from craftsman.files import write_text_atomic
from craftsman.gate import GateOwner, GateState, cli_caller, load_state, state_path_for
from craftsman.ledger import ledger_path_for, load, stable_hunk_ids
from craftsman.records import IsolationEvidence, Ledger, format_timestamp, parse_timestamp

HOOKS_MARKER = "review_hooks.json"
# A marker touched within this long ago was touched by the very command that is running now: the
# PreToolUse hook arms or touches it just before the shell call.
MARKER_FRESH_FOR = timedelta(seconds=120)
# A guarding marker of another session that was touched more recently than this is not taken over.
MARKER_STEAL_AFTER = timedelta(hours=2)
HOOKED_PLATFORMS = ("claude", "copilot", "antigravity")


@dataclass(frozen=True)
class ReviewMarker:
    """What the hooks noted about a session that invoked the review command.

    A review-only run has no gate state to say that hooks serve its session; this file does. While
    `guarding`, the Stop hook keeps the session working until the review-only outcome exists; `forced`
    counts those reminders, `evidence_seen` is the evidence counter at the last one (the progress rule is
    `gate.advance_streak`). Only the hooks write `touched_at`: no `craftsman` command does.
    """

    platform: str
    session_id: str
    at: str
    guarding: bool = True
    forced: int = 0
    touched_at: str | None = None  # when the hooks last saw this session run a review command
    deep: bool = False  # the request that started the review asked for the deep review
    task: str = ""  # what the request said the change is for
    evidence_seen: int | None = None  # the evidence counter when the guard last blocked
    abandoned_at: str | None = None  # when the guard gave up at its continuation cap

    def is_of(self, caller: GateOwner) -> bool:
        return self.platform == caller.platform and self.session_id == caller.session_id

    def last_seen(self) -> datetime:
        """When the hooks last saw this session do anything for the review: the touch, else the start."""
        return parse_timestamp(self.touched_at or self.at)

    def ended(self) -> ReviewMarker:
        """This marker once its review is over (finished, or the user moved on): no longer guarding."""
        return replace(self, guarding=False, forced=0, evidence_seen=None)

    def abandoned(self, now: datetime) -> ReviewMarker:
        """This marker once the guard gave up on an incomplete review at `now`."""
        return replace(self.ended(), abandoned_at=format_timestamp(now))


def read_marker(ledger_path: Path) -> ReviewMarker | None:
    try:
        raw = json.loads((ledger_path.parent / HOOKS_MARKER).read_text(encoding="utf-8"))
        seen = raw.get("evidence_seen")
        task = raw.get("task")
        return ReviewMarker(
            str(raw["platform"]),
            str(raw["session_id"]),
            str(raw["at"]),
            guarding=raw.get("guarding") is True,
            forced=int(raw.get("forced", 0)),
            touched_at=str(raw["touched_at"]) if raw.get("touched_at") else None,
            deep=raw.get("deep") is True,
            task=task if isinstance(task, str) else "",
            evidence_seen=seen if isinstance(seen, int) and not isinstance(seen, bool) else None,
            abandoned_at=str(raw["abandoned_at"]) if raw.get("abandoned_at") else None,
        )
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def write_marker(ledger_path: Path, marker: ReviewMarker) -> None:
    try:
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomic(ledger_path.parent / HOOKS_MARKER, json.dumps(asdict(marker)) + "\n")
    except OSError:
        pass  # without the marker a review-only run is only advisory about isolation


def arm_marker(
    ledger_path: Path,
    caller: GateOwner,
    now: datetime,
    *,
    fresh: bool,
    deep: bool | None = None,
    task: str | None = None,
) -> bool:
    """Note that the hooks saw `caller`'s session ask for or run a review; False when it was left alone.

    A guarding marker of ANOTHER session that was seen less than `MARKER_STEAL_AFTER` ago is kept (the
    single marker file serves one review per checkout; parallel reviews need worktrees).

    `fresh` is a new review request (the `/craft-review` prompt): a NEW marker for `caller` with `at` =
    `now`, no touch (a prompt is not a review command), the counters at zero and `deep`/`task` from the
    request. Without it (a `craftsman review` command seen by PreToolUse) a marker of `caller` that is still
    guarding keeps its `at`, counters, `deep` and `task` unless new ones are given, and is touched `now`.
    A marker that no longer guards (its review passed, the user moved on, the guard gave up) belongs to a
    finished review: a command then begins a new one, so the marker is new, touched `now`, with `deep` and
    `task` only as given (the request of the old review must not leak into it); only `abandoned_at` is
    carried over, since the banner of an abandoned review stays until the next round replaces it.
    """
    if caller.session_id is None:
        return False
    stamp = format_timestamp(now)
    current = read_marker(ledger_path)
    if current is not None and current.guarding and not current.is_of(caller) and now - _seen(current) < MARKER_STEAL_AFTER:
        return False
    if current is not None and current.guarding and current.is_of(caller) and not fresh:
        marker = replace(
            current,
            touched_at=stamp,
            deep=current.deep if deep is None else deep,
            task=current.task if task is None else task,
        )
    else:
        kept = current.abandoned_at if current is not None and current.is_of(caller) and not fresh else None
        marker = ReviewMarker(
            caller.platform,
            caller.session_id,
            stamp,
            touched_at=None if fresh else stamp,
            deep=bool(deep),
            task=task or "",
            abandoned_at=kept,
        )
    write_marker(ledger_path, marker)
    return True


def _seen(marker: ReviewMarker) -> datetime:
    try:
        return marker.last_seen()
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)  # a stamp that cannot be read proves nothing


def touch_marker(ledger_path: Path, caller: GateOwner, now: datetime) -> None:
    """Note that the hooks saw `caller`'s guarding session run a review command (`report` among them)."""
    marker = read_marker(ledger_path)
    if marker is not None and marker.guarding and marker.is_of(caller):
        write_marker(ledger_path, replace(marker, touched_at=format_timestamp(now)))


def end_review_guard(ledger_path: Path, caller: GateOwner | None = None) -> None:
    """Stop holding the session (of `caller`, if given) to a finished review or one the user moved on from."""
    marker = read_marker(ledger_path)
    if marker is not None and marker.guarding and (caller is None or marker.is_of(caller)):
        write_marker(ledger_path, marker.ended())


def abandon_review_guard(ledger_path: Path, caller: GateOwner, now: datetime) -> None:
    """Stop holding `caller`'s session to a review it left incomplete, and say so (`abandoned_at`)."""
    marker = read_marker(ledger_path)
    if marker is not None and marker.guarding and marker.is_of(caller):
        write_marker(ledger_path, marker.abandoned(now))


def live_marker(ledger_path: Path, now: datetime) -> ReviewMarker | None:
    """The marker, when it counts for the command that runs at `now`: guarding, of a hooked platform, and
    touched by the hooks within `MARKER_FRESH_FOR` (the PreToolUse of this very command).

    The one decision of `review_enforcement` and of `prepare` (the marker's `deep` and `task` apply only
    through here, so a stale marker of an earlier review leaks nothing into a new round).
    """
    marker = read_marker(ledger_path)
    if marker is not None and marker.guarding and marker.platform in HOOKED_PLATFORMS and _just_touched(marker, now):
        return marker
    return None


def cli_visible(caller: GateOwner) -> GateOwner:
    """`caller` as a `craftsman` command run by that session sees it.

    Claude Code exports its session id to the commands it runs and an Antigravity conversation exports its
    own id; on other platforms a command cannot tell which session it serves.
    """
    return caller if caller.platform in ("claude", "antigravity") else GateOwner(caller.platform, None)


def caller_identity(env: Mapping[str, str]) -> str | None:
    """Who a command can prove it is: `antigravity:<conversation id>` inside an Antigravity conversation.

    Claude Code subagents share their parent's session id, so nothing in their environment tells a
    hunter from the orchestrator: that identity comes from the subagent's own transcript instead.

    The variable is the caller's own word, so it is only as good as the guard that keeps one agent from
    setting it for another: the Antigravity scope guard and the Claude and Copilot PreToolUse hooks (a later
    phase) must deny any command that assigns `ANTIGRAVITY_CONVERSATION_ID` (an inline `VAR=...` prefix,
    `env`, `export`), found with `shell.iter_simple_commands`. Until then an orchestrator that learned a
    subagent's conversation id could record as that subagent.
    """
    conversation = env.get("ANTIGRAVITY_CONVERSATION_ID", "").strip()
    return f"antigravity:{conversation}" if conversation else None


def brief_platform(env: Mapping[str, str]) -> str:
    """The agent platform a brief is read on: `claude`, `antigravity`, `copilot` or `cli` (unknown).

    Only decides whether a brief spells out the role text: Claude Code and Copilot load the agent
    definition themselves, every other reader needs it inline.
    """
    if env.get("CLAUDECODE") or env.get("CLAUDE_CODE_ENTRYPOINT"):
        return "claude"
    if env.get("ANTIGRAVITY_AGENT") or env.get("AGY_MANAGED_ENV") or caller_identity(env) is not None:
        return "antigravity"
    if env.get("COPILOT_CLI") or env.get("GITHUB_COPILOT_CLI"):
        return "copilot"
    return "cli"


@dataclass(frozen=True)
class Workspace:
    """A work tree, its active gate (None outside a `/craftsman` task) and its evidence ledger."""

    root: Path
    state: GateState | None
    ledger_path: Path

    @property
    def task(self) -> str:
        return self.state.task if self.state is not None else ""

    def isolation_evidence(self, ledger: Ledger, caller: GateOwner | None = None) -> IsolationEvidence:
        """Where proof of an isolated reviewer can come from for this review.

        A gate knows what its platform proved. Without a gate (review-only), hooks serve this review
        when the prompt hook saw `caller` (default: the session this command runs in) invoke the
        review command, or when a subagent event arrived after the current round was prepared. An
        event of an earlier session or platform proves nothing about this one.
        """
        if self.state is not None:
            return self.state.required_isolation
        if self._hooks_saw(caller or cli_caller(None, os.environ)) or self._agent_event_since_round(ledger):
            return "hooks"
        return "none"

    def _hooks_saw(self, caller: GateOwner) -> bool:
        marker = read_marker(self.ledger_path)
        return marker is not None and caller.session_id is not None and marker.is_of(caller)

    @staticmethod
    def _agent_event_since_round(ledger: Ledger) -> bool:
        if not ledger.rounds:
            return False
        prepared = parse_timestamp(ledger.rounds[-1].prepared_at)
        return any(parse_timestamp(event.at) >= prepared for event in ledger.agents)

    def owns_gate(self, env: Mapping[str, str]) -> bool:
        """Whether this command acts for the session that owns the active gate (false without one)."""
        return self.state is not None and self.state.owner.admits(cli_caller(None, env))

    def base_sha(self, ledger: Ledger, explicit: str | None = None) -> str:
        """`explicit`, else the latest round's base, else the active task's base, else HEAD."""
        ledger_base = ledger.rounds[-1].base_sha if ledger.rounds else None
        task_base = self.state.base_sha if self.state is not None else None
        return resolve_base_sha(self.root, explicit or ledger_base or task_base)

    def snapshot(self, ledger: Ledger | None = None) -> DiffSnapshot:
        """The diff since the base the review (or else the active task) is measured against."""
        current = load(self.ledger_path) if ledger is None else ledger
        scope = None if self.state is not None else current.current_scope
        return stable_hunk_ids(current, snapshot(self.root, self.base_sha(current), scope))


def open_workspace(cwd: str | None) -> Workspace:
    """Locate the work tree, its active gate and its ledger for `cwd` (default: the current one).

    Raises `GateStateError` when the gate state file is invalid.
    """
    root = worktree_root(cwd or os.getcwd())
    state = load_state(state_path_for(root))
    active = state if state is not None and state.active else None
    return Workspace(root=root, state=active, ledger_path=ledger_path_for(root))


def review_enforcement(
    ws: Workspace, ledger: Ledger, env: Mapping[str, str], now: datetime
) -> tuple[bool, str]:
    """Whether the hooks hold this review to its hunt plan, and the platform that serves it.

    Enforced when (a) the checkout has an active gate whose platform proved isolation, whoever runs the
    command: one gate governs the review of its checkout (the ledger is shared), and on Antigravity every
    hunter is a conversation of its own and never the gate's owner; the platform is the owner's. Or (b) a
    guarding review marker was touched within `MARKER_FRESH_FOR` of `now` (by the PreToolUse hook of this
    very command), or (c) the latest round was enforced, which is sticky: a review does not become
    advisory because a later command ran without a hook. Otherwise the round is advisory: `(False, "cli")`.
    """
    if ws.state is not None and ws.state.required_isolation != "none":
        return True, ws.state.owner.platform
    marker = live_marker(ws.ledger_path, now)
    if marker is not None:
        return True, marker.platform
    if ledger.rounds and ledger.rounds[-1].enforced:
        return True, ledger.rounds[-1].platform
    return False, "cli"


def _just_touched(marker: ReviewMarker, now: datetime) -> bool:
    """Whether the hooks touched `marker` no more than `MARKER_FRESH_FOR` before `now` (a naive `now` is UTC)."""
    if marker.touched_at is None:
        return False
    try:
        touched = parse_timestamp(marker.touched_at)
    except ValueError:
        return False  # a marker that cannot be read proves nothing
    current = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    return timedelta(0) <= current - touched <= MARKER_FRESH_FOR
