"""The review guard: what keeps a session that began a review working until the review is done.

One decision serves every platform. The Claude Code and Copilot hooks call `serve_review_stop` from their
Stop event; the Antigravity Stop hook (a later phase) calls it too. The guard does not import `hooks`.

The guard watches a session through its `ReviewMarker`: the prompt hook writes it for a `/craft-review`
request, the PreToolUse hook arms and touches it whenever the main session runs a `craftsman review`
command (`review_commands` reads the command). Stopping is allowed when the review outcome exists, while
craftsman subagents of the review are still running (their reports wake the session), and, as a last
resort, after `MAX_CONTINUATIONS` reminders that brought no new evidence (the review is then marked
abandoned, never complete). The same progress rule and waiting notice serve the gate (`gate.decide_stop`).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from craftsman import hook_errors
from craftsman.attribution import agent_runs, evidence_counter
from craftsman.command_policy import review_commands as review_commands
from craftsman.diffscan import DiffScanError
from craftsman.evidence import effective_deep
from craftsman.gate import (
    MAX_CONTINUATIONS,
    REVIEW_CONTINUATION_HEADER,
    GateOwner,
    GateStateError,
    advance_streak,
    continuation_reason,
    load_state,
    state_path_for,
)
from craftsman.gatecheck import GateIssue, GatePolicy, GateReport, evaluate, hunters_at_work, is_open_run
from craftsman.huntplan import plan_due_waves
from craftsman.huntview import check_command, waiting_notice
from craftsman.ledger import load, mutate
from craftsman.records import AgentRole, Ledger, LedgerError, Round, parse_timestamp
from craftsman.workspace import (
    ReviewMarker,
    Workspace,
    cli_visible,
    read_marker,
    write_marker,
)

REVIEW_NOT_STARTED = "No review round yet: run `craftsman review prepare` and launch the hunters it lists."
DEEP_REQUESTED = "A deep review was requested: run `craftsman review prepare` (it raises the round to deep)."
HOOKS_ENFORCE = (
    "This session's hooks enforce the review: run `craftsman review prepare` and launch the hunters it lists."
)
ROLE_LABELS: dict[AgentRole, str] = {"reviewer": "craftsman-reviewer", "verifier": "craftsman-verifier"}
ABANDONED_NOTICE = (
    f"Craftsman review guard: still incomplete after {MAX_CONTINUATIONS} reminders without progress; "
    "letting the agent stop. The review is NOT complete: `{check}` lists what is missing."
)


# --- what is running --------------------------------------------------------------------------------


def running_work(ledger: Ledger, round_: Round, now: datetime) -> tuple[str, ...]:
    """What craftsman subagents are working for `round_`: the slots with a running hunter, and the runs not tied to a slot.

    Only OPEN runs count: a start event of the round (after `prepared_at`) without a stop, younger than
    `RUN_STALE_AFTER`. A claim that no open run backs never counts, so an orchestrator that fetched a brief
    itself cannot wave its own stop through. Hunters that were launched and have not fetched their brief yet
    (the gate's `starting`) and verifiers are named by role, with a count. The slot test is the gate's own
    (`gatecheck.hunters_at_work`).
    """
    moment = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    prepared = parse_timestamp(round_.prepared_at)
    slots, starting = hunters_at_work(ledger, round_, moment)
    open_runs = [run for run in agent_runs(ledger) if is_open_run(run, prepared, moment)]
    if round_.plan is None:
        starting = sum(run.role == "reviewer" for run in open_runs)  # no slots: every open hunter is unplaced
    counts: list[tuple[AgentRole, int]] = [
        ("reviewer", starting),
        ("verifier", sum(run.role == "verifier" for run in open_runs)),
    ]
    labels = [ROLE_LABELS[role] + (f" x{count}" if count > 1 else "") for role, count in counts if count]
    return (*slots, *labels)


def store_due_waves(ledger_path: Path, now: datetime) -> None:
    """Store the waves that are due in the latest round's plan, under the ledger lock (and only if any are).

    Both Stop guards call it before they judge the ledger, so a block already lists the next wave's launch lines.
    """
    ledger = load(ledger_path)
    if not ledger.rounds or ledger.rounds[-1].plan is None or plan_due_waves(ledger, now) is ledger:
        return  # the usual case: no lock

    def plan(current: Ledger) -> tuple[Ledger, None]:
        if not current.rounds or current.rounds[-1].plan is None:
            return current, None
        return plan_due_waves(current, now), None

    mutate(ledger_path, plan)


@dataclass(frozen=True)
class StopInputs:
    """What the gate's Stop needs to know about the agents of the review: who is running, how much evidence exists."""

    running: tuple[str, ...] = ()
    evidence: int | None = None  # None: the ledger could not be read


def gate_stop_inputs(ledger_path: Path, now: datetime) -> StopInputs:
    """`StopInputs` of the ledger at `ledger_path`, with the due waves stored first (an unreadable ledger tells nothing)."""
    try:
        store_due_waves(ledger_path, now)
        ledger = load(ledger_path)
    except LedgerError as exc:
        # Not "nothing running": the ledger could not be read (a newer craftsman's, a corrupt file). The hook error
        # log keeps the reason; the caller falls back to the rule that needs no evidence counter.
        hook_errors.report("ledger", f"the stop guard could not read the review ledger: {exc}", ledger_path.parent)
        return StopInputs()
    running = running_work(ledger, ledger.rounds[-1], now) if ledger.rounds else ()
    return StopInputs(running, evidence_counter(ledger, agent_runs(ledger)))


# --- the outcome of a review ------------------------------------------------------------------------


def review_outcome_issues(
    root: Path, ledger_path: Path, marker: ReviewMarker, caller: GateOwner, *, now: datetime | None = None
) -> list[GateIssue]:
    """What `craftsman review check --review-only` still lacks for a review begun at `marker`.

    Judged by the same rules and evidence as that command, as it runs for `caller`'s session. The review
    must have been touched since the marker was written (a hooked `craftsman review` command, a round, a
    coverage record or a finding), else the only issue is that no round exists yet. A re-prepare of an
    unchanged diff keeps its round and its `prepared_at`, so a round is NOT required to be newer than the
    marker. A requested deep review needs a deep round, and in a hooked session the latest round must be
    enforced (a plain `prepare` upgrades both); only then does the ledger decide.
    """
    ledger = load(ledger_path)
    started = parse_timestamp(marker.at)
    latest = ledger.rounds[-1] if ledger.rounds else None
    touched = latest is not None and (
        (marker.touched_at is not None and parse_timestamp(marker.touched_at) >= started)
        or parse_timestamp(latest.prepared_at) >= started
        or any(parse_timestamp(item.recorded_at) >= started for item in ledger.coverage)
        or any(parse_timestamp(item.recorded_at) >= started for item in ledger.findings)
    )
    if latest is None or not touched:
        return [GateIssue("no-round", REVIEW_NOT_STARTED)]
    if marker.deep and not latest.deep:
        return [GateIssue("no-round", DEEP_REQUESTED)]
    if not latest.enforced:
        return [GateIssue("no-round", HOOKS_ENFORCE)]
    workspace = Workspace(root, None, ledger_path)
    policy = GatePolicy(
        deep=effective_deep(ledger, marker.deep),
        isolation_evidence=workspace.isolation_evidence(ledger, cli_visible(caller)),
    )
    return list(evaluate(ledger, workspace.snapshot(ledger), policy, review_only=True, now=now).issues)


# --- the decision -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class GuardDecision:
    """What a Stop hook does for a guarded review: block with `block`, or allow the stop with an optional `notice`.

    `marker` is the marker to store afterwards; None leaves the stored one as it is.
    """

    marker: ReviewMarker | None = None
    block: str | None = None
    notice: str | None = None

    def __post_init__(self) -> None:
        if self.block is not None and self.notice is not None:
            raise ValueError("a guard decision blocks or allows, not both")


def _gate_active(root: Path) -> bool:
    state = load_state(state_path_for(root))
    return state is not None and state.active


def decide_review_stop(
    root: Path, ledger_path: Path, marker: ReviewMarker | None, caller: GateOwner, now: datetime
) -> GuardDecision:
    """Whether `caller` may stop now, for the review `marker` guards (see the module text for the rules)."""
    if marker is None or not marker.guarding or not marker.is_of(caller):
        return GuardDecision()
    try:
        store_due_waves(ledger_path, now)
        issues = review_outcome_issues(root, ledger_path, marker, caller, now=now)
        ledger = load(ledger_path)
        gate_active = _gate_active(root)
    except (DiffScanError, LedgerError, GateStateError) as exc:
        hook_errors.report(hook_errors.kind_of(exc), f"the review guard could not check the review: {exc}", ledger_path.parent)
        return GuardDecision(marker.ended(), notice=f"Craftsman review guard could not check the review ({exc}); not enforced.")
    check = check_command(gate_active=gate_active)
    if not issues:
        return GuardDecision(marker.ended())
    runs = agent_runs(ledger)
    running = running_work(ledger, ledger.rounds[-1], now) if ledger.rounds else ()
    if running:
        # Background subagents report back when they finish and wake the session; a block now would only
        # burn reminders while the agent waits for them.
        return GuardDecision(notice=waiting_notice(running, gate_active=gate_active))
    evidence = evidence_counter(ledger, runs)
    forced = advance_streak(marker.forced, marker.evidence_seen, evidence) + 1
    if forced > MAX_CONTINUATIONS:
        return GuardDecision(marker.abandoned(now), notice=ABANDONED_NOTICE.format(check=check))
    reason = continuation_reason(
        f"{REVIEW_CONTINUATION_HEADER} {forced}/{MAX_CONTINUATIONS}]",
        GateReport(tuple(issues), (), ""),
        None,
        consequence="",
        closing=f"Finish with `{check}` passing.",
        check=check,
    )
    return GuardDecision(replace(marker, forced=forced, evidence_seen=evidence), block=reason)


def serve_review_stop(root: Path, ledger_path: Path, caller: GateOwner, now: datetime) -> GuardDecision:
    """`decide_review_stop` for the stored marker, and the new marker stored (the single-file race is accepted:
    parallel reviews need worktrees)."""
    decision = decide_review_stop(root, ledger_path, read_marker(ledger_path), caller, now)
    if decision.marker is not None:
        write_marker(ledger_path, decision.marker)
    return decision
