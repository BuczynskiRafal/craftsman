"""Gate evaluation: decide whether the ledger's evidence covers the current diff, and report why.

`evaluate` is a pure function of a ledger, a diff snapshot and a policy; `render_report` turns its
`GateReport` into the Markdown block shown to agents and users.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from craftsman.attribution import (
    IDENTITY_PLATFORMS,
    RUN_STALE_AFTER,
    AgentRun,
    agent_runs,
    distinct_verdict_runs,
    identity,
    round_vouchers,
    run_key,
    verdict_vouched,
)
from craftsman.diffscan import DiffSnapshot
from craftsman.huntplan import plan_due_waves, required_slots, slots_missing, window_slots
from craftsman.ledger import uncovered_hunks
from craftsman.records import (
    REVIEWER_AGENT,
    Finding,
    IsolationEvidence,
    Ledger,
    LedgerError,
    Round,
    Run,
    RunKind,
    SignalKind,
    Slot,
    SlotClaim,
    agent_role,
    parse_timestamp,
)
from craftsman.text import defang_tokens, inline_md
from craftsman.triviality import command_without_env, is_trivial_run

IssueCode = Literal[
    "no-round",
    "stale-round",
    "uncovered",
    "hunt-pending",
    "no-verdict",
    "unverified",
    "second-opinion",
    "wave-pending",
    "unresolved",
    "no-passing-run",
    "no-e2e-run",
    "failing-run",
    "no-isolation",
    "round-cap",
]

SELF_REVIEWED_BANNER = (
    "SELF-REVIEWED: no evidence that anyone other than the author reviewed this change"
)
MAX_NEXT_ACTIONS = 12
MAX_LISTED_HUNKS = 12
SUMMARY_RECENT_RUNS = 3
# Review rounds of different diffs before the agent must stop iterating and ask the user.
MAX_ROUNDS = 5
# Process anomalies: patterns typical of a review that was recorded rather than performed.
ANOMALY_MIN_FINDINGS = 5
PASSING_KINDS: tuple[RunKind, ...] = ("test", "e2e")
_VERIFYING_KINDS: tuple[RunKind, ...] = ("test", "e2e", "lint", "build")

MAX_LISTED_SLOTS = 12
MAX_LISTED_FINDINGS = 12
MAX_LISTED_CLUSTERS = 8
MAX_CLUSTER_FINDINGS = 6
# What one hunt issue message may take, so that the stop hooks can show several of them in full.
MAX_HUNT_ISSUE_CHARS = 900


# --- gate evaluation ------------------------------------------------------------------------


@dataclass(frozen=True)
class GateIssue:
    code: IssueCode
    message: str
    # How many things the issue stands for, where one issue stands for many: hunks still uncovered
    # (`uncovered`), hunter slots not finished (`hunt-pending`, `wave-pending`), findings still to be
    # judged (`no-verdict`, `unverified`, `second-opinion`).
    missing: int = 0


SlotState = Literal["done", "running", "dead", "unlaunched"]


@dataclass(frozen=True)
class SlotStatus:
    """Where one slot of the hunt plan stands.

    `missing` lists the hunks of the slot that still owe one of its lenses (empty when done). `since`
    is when the state began, where that is known: the start of the run that backs a running slot, the
    moment the hunter of a dead slot stopped (or its claim went stale).
    """

    slot: Slot
    state: SlotState
    missing: tuple[str, ...] = ()
    since: datetime | None = None

    def __post_init__(self) -> None:
        if (self.state == "done") != (not self.missing):
            raise ValueError(f"slot {self.slot.id} is {self.state} but has missing hunks {self.missing!r}")


@dataclass(frozen=True)
class GateReport:
    issues: tuple[GateIssue, ...]
    warnings: tuple[str, ...]
    summary: str
    review_only: bool = False
    self_reviewed: bool = False
    partial: bool = False
    # The slots of the latest round's hunt plan and where each stands; empty for a round without a plan.
    slots: tuple[SlotStatus, ...] = ()
    # Whether the hunt rules judged the round (it is enforced and has a plan) instead of the 5.0 rules.
    enforced: bool = False

    def __post_init__(self) -> None:
        if self.enforced and self.self_reviewed:
            raise ValueError("an enforced round is judged by vouched subagents, so it is never self-reviewed")

    @property
    def passed(self) -> bool:
        return not self.issues

    @property
    def subject(self) -> str:
        """What was judged: the review alone, or the whole gate."""
        return "review" if self.review_only else "gate"

    @property
    def status_label(self) -> str:
        """PASSED / BLOCKED for the gate; COMPLETE / INCOMPLETE / PARTIAL for a review-only check."""
        if self.partial:
            if self.issues:
                return f"PARTIAL (review-only, {len(self.issues)} open items)"
            return "COMPLETE (review-only)"
        if self.passed:
            return "COMPLETE (review-only)" if self.review_only else "PASSED"
        blocked = "INCOMPLETE" if self.review_only else "BLOCKED"
        return f"{blocked} ({len(self.issues)} open items)"

    @property
    def issue_codes(self) -> str:
        """The distinct issue codes, in order of first appearance."""
        return ", ".join(dict.fromkeys(issue.code for issue in self.issues))


@dataclass(frozen=True)
class GatePolicy:
    """How strictly the gate judges a ledger.

    `isolation_evidence` names where proof of an isolated reviewer can come from; with `none`, the
    review passes as self-reviewed. `deep` additionally requires a passing end-to-end run.
    """

    deep: bool = False
    isolation_evidence: IsolationEvidence = "none"

    @property
    def require_isolation(self) -> bool:
        return self.isolation_evidence != "none"


def evaluate(
    ledger: Ledger,
    snap: DiffSnapshot,
    policy: GatePolicy,
    *,
    review_only: bool = False,
    now: datetime | None = None,
) -> GateReport:
    """Decide whether the evidence in `ledger` covers the current diff `snap`.

    An enforced round with a hunt plan is judged by the hunt rules: its slots must be finished by
    vouched subagents and every finding judged by a verifier. Any other round keeps the 5.0 rules.
    `now` (default: the current time) decides whether a hunter is still running and when a due wave is
    planned.

    `evaluate` never writes, yet a wave that is due but not stored yet (`plan_due_waves` runs under the
    lock at `review check`, `report`, `brief` and `prepare`) must not let a caller finish: for an
    enforced round the slots of such a wave are judged as if they were stored, so they are pending and
    unlaunched until the next `review brief` stores them.
    """
    if snap.is_empty:
        summary = f"Nothing to review: the work tree matches base {snap.base_sha[:12]}."
        issues = () if review_only else tuple(_unresolved_issues(ledger, snap))
        return GateReport(issues, (), summary, review_only=review_only)
    fix_and_test = (
        ()
        if review_only
        else (
            *_unresolved_issues(ledger, snap),
            *_passing_run_issues(ledger, snap, policy),
            *_failing_run_issues(ledger),
        )
    )
    moment = _utc(now)
    latest, runs, state = _judged_hunt(ledger, moment)
    slots = state.statuses if state is not None else ()
    if latest is not None and state is not None:
        round_issues = _round_issues(ledger, snap)
        hunt, waves = _hunt_issues(ledger, latest, state, stale=bool(round_issues))
        issues = (
            *round_issues,
            *hunt,
            *_verification_issues(ledger, latest, runs, state.now),
            *waves,
            *fix_and_test,
            *_round_cap_issues(ledger),
        )
        return GateReport(
            issues,
            _warnings(ledger, review_only=review_only),
            _summary(ledger, snap, slots=slots),
            review_only=review_only,
            slots=slots,
            enforced=True,
        )
    issues = (
        *_round_issues(ledger, snap),
        *(_coverage_issues(ledger, latest) if latest else ()),
        *_verdict_issues(ledger),
        *fix_and_test,
        *_isolation_issues(ledger, policy),
        *_round_cap_issues(ledger),
    )
    self_reviewed = bool(not policy.require_isolation and _review_gaps(ledger))
    return GateReport(
        issues,
        _warnings(ledger, review_only=review_only),
        _summary(ledger, snap),
        review_only=review_only,
        self_reviewed=self_reviewed,
        slots=slots,
    )


def _judged_hunt(
    ledger: Ledger, moment: datetime
) -> tuple[Round | None, Sequence[AgentRun], _HuntState | None]:
    """The latest round as `evaluate` judges it (with its due wave planned in memory), the agent runs and the hunt state.

    The one place that decides which slots a round has: for an enforced round with a plan the waves that are
    due are planned in memory (`evaluate` never writes), so the slots of a wave that is not stored yet count.
    The state is None for a round that is not judged by slots: one without a plan, and an advisory one, whose
    completion is its hunk coverage (its plan is a suggestion, so no display may list slot states for it).
    """
    latest = ledger.rounds[-1] if ledger.rounds else None
    if latest is None or latest.plan is None or not latest.enforced:
        return latest, (), None
    planned = plan_due_waves(ledger, moment)
    latest = planned.rounds[-1]
    runs = agent_runs(ledger)
    return latest, runs, _hunt_state(planned, latest, moment, runs)


def hunt_slots(ledger: Ledger, now: datetime | None = None) -> tuple[SlotStatus, ...] | None:
    """The slots the latest round is judged by, as `evaluate` sees them, or None when it is not judged by slots.

    A round is judged by slots when it is enforced and has a plan; every other round (advisory, or without a
    plan) is judged by its hunk coverage. Slots a due wave would add are included although the stored plan does
    not hold them yet, exactly as in `evaluate`. Every display that reports the progress of the latest round
    asks this, so that none of them claims a coverage the gate does not accept.
    """
    latest = ledger.rounds[-1] if ledger.rounds else None
    if latest is None or latest.plan is None or not latest.enforced:
        return None
    state = _judged_hunt(ledger, _utc(now))[2]
    assert state is not None  # the round is enforced and has a plan
    return state.statuses


def require_current_round(ledger: Ledger, snap: DiffSnapshot) -> Round:
    """The latest round, provided it was prepared for exactly the diff `snap`."""
    issues = _round_issues(ledger, snap)
    if issues:
        raise LedgerError(issues[0].message)
    return ledger.rounds[-1]


def _round_issues(ledger: Ledger, snap: DiffSnapshot) -> list[GateIssue]:
    if not ledger.rounds:
        return [
            GateIssue(
                "no-round",
                "No review round yet; run `craftsman review prepare` to snapshot the diff "
                "and see the suggested lenses.",
            )
        ]
    base = ledger.rounds[0].base_sha
    latest = ledger.rounds[-1]
    if snap.base_sha != base:
        message = (
            f"The base commit changed from {base[:12]} to {snap.base_sha[:12]} since round 1; "
            "run `craftsman review reset` and start a new review."
        )
        return [GateIssue("stale-round", message)]
    if latest.fingerprint != snap.fingerprint:
        message = (
            f"Code changed since round {latest.number} was prepared; "
            "run `craftsman review prepare` to review the delta."
        )
        return [GateIssue("stale-round", message)]
    return []


def _coverage_issues(ledger: Ledger, latest: Round) -> list[GateIssue]:
    missing = uncovered_hunks(ledger, latest)
    if not missing:
        return []
    batch = ",".join(missing[:MAX_LISTED_HUNKS])
    extra = len(missing) - MAX_LISTED_HUNKS
    more = f" (+{extra} more)" if extra > 0 else ""
    return [
        GateIssue(
            "uncovered",
            f"{len(missing)} hunk(s) uncovered in round {latest.number}: {batch}{more}; have them "
            f'reviewed and run `craftsman review cover --hunks {batch} --note "..."`.',
            missing=len(missing),
        )
    ]


def _verdict_issues(ledger: Ledger) -> list[GateIssue]:
    return [
        GateIssue(
            "no-verdict",
            f"Finding {finding.id} ({finding.severity}: {defang_tokens(inline_md(finding.title))}) has no verdict; verify it "
            f"independently and run `craftsman review verdict {finding.id} "
            '--as proven|probable|question|dismissed --evidence "..."`.',
        )
        for finding in ledger.findings
        if finding.verdict is None
    ]


# --- the hunt: slots of an enforced round --------------------------------------------------------


def _utc(moment: datetime | None) -> datetime:
    """`moment` as an aware time: now when absent, UTC when naive (callers may pass either)."""
    if moment is None:
        return datetime.now(timezone.utc)
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class _HuntState:
    """What the ledger shows about a round's hunt at `now`.

    `starting` counts the reviewer runs that began since the round was prepared, are still going and
    fetched no brief (no claim accounts for them): hunters that were just launched.
    """

    now: datetime
    statuses: tuple[SlotStatus, ...]
    starting: int = 0


def slot_statuses(ledger: Ledger, round_: Round, now: datetime) -> tuple[SlotStatus, ...]:
    """Where every slot `round_` requires stands at `now` (see `SlotStatus`); empty for a round without a plan.

    A naive `now` is taken as UTC.
    """
    return _hunt_state(ledger, round_, _utc(now), agent_runs(ledger)).statuses


def _hunt_state(ledger: Ledger, round_: Round, now: datetime, runs: Sequence[AgentRun]) -> _HuntState:
    """The statuses of `round_`'s stored slots, and how many hunters are starting.

    A slot is done when `huntplan.slots_missing` finds nothing missing: that module owns the one definition
    of "done" (credit for a generation 1 slot, the sweep judged by the slot's own coverage, own vouched
    coverage at or after the trigger for a wave slot), which the planner's barriers use as well.
    """
    slots = required_slots(ledger, round_)
    if not slots:
        return _HuntState(now, ())
    missing = slots_missing(ledger, round_, runs=runs)
    signs = _hunter_signs(ledger, round_, runs, now, done={slot_id for slot_id, lacking in missing.items() if not lacking})
    statuses: list[SlotStatus] = []
    for slot in slots:
        lacking = missing[slot.id]
        if not lacking:
            statuses.append(SlotStatus(slot, "done"))
        elif signs.running.get(slot.id):
            statuses.append(SlotStatus(slot, "running", lacking, min(signs.running[slot.id])))
        elif signs.ended.get(slot.id):
            statuses.append(SlotStatus(slot, "dead", lacking, max(signs.ended[slot.id])))
        else:
            statuses.append(SlotStatus(slot, "unlaunched", lacking))
    return _HuntState(now, tuple(statuses), signs.starting)


@dataclass(frozen=True)
class _HunterSigns:
    """When hunters were seen working on, and giving up, each slot of a round (see `_hunter_signs`)."""

    running: dict[str, list[datetime]]  # slot id -> start of each open run backing a claim of it
    ended: dict[str, list[datetime]]  # slot id -> when each hunter that worked on it stopped
    starting: int


def _hunter_signs(
    ledger: Ledger, round_: Round, runs: Sequence[AgentRun], now: datetime, *, done: AbstractSet[str] = frozenset()
) -> _HunterSigns:
    """What the hooks' runs show about the hunters of `round_`.

    Only a run the hooks recorded can show that someone is working: a claim alone is not enough, since
    the orchestrator may have fetched the brief itself. A run that started before the round was prepared
    belongs to an earlier round and backs no claim of this one (`round_vouchers`). A claim that a finished
    run vouches for by its transcript (or by identity) belongs to that run, which ended. Any other claim is
    explained by a run of its own: a finished run that vouches for it by time (a hunter whose transcript
    could not be read), or, per slot, ONE open reviewer run that may be its hunter (`_open_backers`).
    Matching is by slot, not by claim, and evidence comes first:
    - a finished window run belongs to the slot whose coverage or findings were recorded in its window
      (`_window_evidence`) before any other slot's claim may take it, and a slot that is done explains its
      claims like any other: a finished run is not left over for a claim of another slot;
    - one finished run explains every claim of ONE slot made inside its window (a hunter that fetched its
      brief twice is still one hunter); the slot needs as many runs as it has claims no single run covers;
    - the remaining claims are matched in claim order, a run explaining at most one slot.
    Finished runs are preferred to open ones, which at worst has the slot launched again. Among open runs the
    ones that are still going are matched to the waiting slots first, and only the slots left over may take a
    run that is not (a slot relaunched after a hung run keeps its live run). Claims of slots in `done` take no
    run from the slots still waiting: they are no longer waiting for a hunter. Only after those are matched
    does an open run that may be the hunter of a done slot nobody finished (one slot, one run) stop counting
    as starting. Coverage tagged with a slot that a finished run vouches for shows that hunter ended too. An
    open run counts as going on for `RUN_STALE_AFTER`; after that it is taken for dead, and its slot ended
    when that time ran out.
    """
    number = round_.number
    prepared = parse_timestamp(round_.prepared_at)
    running: dict[str, list[datetime]] = defaultdict(list)
    ended: dict[str, list[datetime]] = defaultdict(list)
    window_claims: dict[str, list[tuple[SlotClaim, list[int]]]] = defaultdict(list)  # slot -> its claims no exact voucher settles
    finished_done: set[str] = set()  # slots in `done` whose hunter a finished run accounts for
    for claim in ledger.claims:
        if claim.round != number:
            continue
        finished = round_vouchers(
            ledger, runs, number, role="reviewer", token=claim.token, recorded_at=parse_timestamp(claim.at), by=claim.by
        )
        if any(voucher.exact for voucher in finished):
            if claim.slot in done:
                finished_done.add(claim.slot)
            else:
                ended[claim.slot].extend(_stopped(voucher.run) for voucher in finished)
        else:
            window_claims[claim.slot].append((claim, [voucher.index for voucher in finished]))
    explained = _explain_by_finished_runs(window_claims, _window_evidence(ledger, round_, runs))
    accounted: set[int] = set()
    waiting: dict[str, list[SlotClaim]] = defaultdict(list)  # slot -> its claims no finished run explains
    done_claims: dict[str, list[SlotClaim]] = defaultdict(list)  # slot in `done` -> its claims no finished run explains
    for slot_id, claims in window_claims.items():
        taken = explained[slot_id]
        for claim, candidates in claims:
            if taken.intersection(candidates):
                if slot_id in done:
                    finished_done.add(slot_id)
            elif slot_id in done:
                done_claims[slot_id].append(claim)
            else:
                waiting[slot_id].append(claim)
        if slot_id not in done:
            ended[slot_id].extend(_stopped(runs[index]) for index in taken)
        accounted.update(taken)
    slot_ids = list(waiting)
    options = {slot_id: _open_backers(runs, waiting[slot_id], prepared, now) for slot_id in slot_ids}
    live = _one_each([[index for index in options[slot_id] if _is_going(runs[index], now)] for slot_id in slot_ids])
    left = [slot_id for slot_id, index in zip(slot_ids, live, strict=True) if index is None]
    for slot_id, index in zip(slot_ids, live, strict=True):
        if index is not None:
            accounted.add(index)
            running[slot_id].append(runs[index].started)
    stale = _one_each([[index for index in options[slot_id] if not _is_going(runs[index], now)] for slot_id in left])
    for slot_id, index in zip(left, stale, strict=True):
        if index is not None:
            accounted.add(index)
            ended[slot_id].append(runs[index].started + RUN_STALE_AFTER)
    for position, record in enumerate(ledger.coverage, 1):
        if record.round == number and record.hunter is not None:
            recorded = round_vouchers(
                ledger,
                runs,
                number,
                role="reviewer",
                token=f"C{position}",
                recorded_at=parse_timestamp(record.recorded_at),
                by=record.by,
            )
            ended[record.hunter].extend(_stopped(voucher.run) for voucher in recorded)
    # A slot can be done (credited from another slot's coverage) while its own hunter is still going on: that
    # open run is the hunter of the slot, not one that is still starting.
    done_slots = [slot_id for slot_id in done_claims if slot_id not in finished_done]
    free = [
        [index for index in _open_backers(runs, done_claims[slot_id], prepared, now) if index not in accounted]
        for slot_id in done_slots
    ]
    done_backers = {index for index in _one_each(free) if index is not None}
    starting = sum(
        1
        for index, run in enumerate(runs)
        if index not in accounted
        and index not in done_backers
        and run.role == "reviewer"
        and run.platform not in IDENTITY_PLATFORMS
        and run.started >= prepared
        and _is_going(run, now)
    )
    return _HunterSigns(running, ended, starting)


def _window_evidence(ledger: Ledger, round_: Round, runs: Sequence[AgentRun]) -> dict[int, str]:
    """The slot each finished window run (one whose records could not be read) certainly worked for, by index into `runs`.

    Parallel Copilot hunters overlap, so a time window vouches for every one of them and a record shows
    only that one of the overlapping runs did the work. A run is therefore reserved for a slot only on the
    strongest of that evidence, and only one run per piece of work, so the others stay free for the claims
    of other slots (a hunter that crashed beside it):
    - the run the matching gave the slot that holds accepted coverage in its window
      (`huntplan.window_slots` with `matched_only`, the very assignment the credit uses), else
    - for the first slot, in plan order, that has a finding recorded in the window of exactly one finished
      run (a finding is tagged with its slot): that run.
    A run that left no such trace is absent: only its claims can place it.
    """
    matched = window_slots(ledger, runs, matched_only=True)
    evidence: dict[int, str] = {}
    for index, run in enumerate(runs):
        slot_id = matched.get((round_.number, run_key(run, index)))
        if slot_id is not None:
            evidence[index] = slot_id
    order = {slot.id: place for place, slot in enumerate(round_.plan.slots)} if round_.plan is not None else {}
    tagged = sorted(
        (finding for finding in ledger.findings if finding.round == round_.number and finding.hunter in order),
        key=lambda finding: order[finding.hunter or ""],
    )
    for finding in tagged:
        vouchers = round_vouchers(
            ledger,
            runs,
            round_.number,
            role="reviewer",
            token=finding.id,
            recorded_at=parse_timestamp(finding.recorded_at),
            by=finding.by,
        )
        if len(vouchers) == 1 and not vouchers[0].exact and finding.hunter is not None:
            evidence.setdefault(vouchers[0].index, finding.hunter)
    return evidence


def _explain_by_finished_runs(
    window_claims: Mapping[str, Sequence[tuple[SlotClaim, list[int]]]], evidence: Mapping[int, str]
) -> dict[str, set[int]]:
    """The finished window runs that account for the claims of each slot, one run for a slot's every claim it covers.

    `window_claims` maps a slot to its claims, each with the finished runs that vouch for it by time. A run in
    `evidence` is reserved for its slot. A slot needs one run per group of its claims that no single run
    covers, and those are the runs of the greedy cover (`_covering_runs`), which it takes before any other
    run of its options; slots are matched in the order of their first claim, a run to one slot, and a
    slot that gets fewer runs than it needs leaves its other claims unexplained.
    """
    slots = list(window_claims)
    options: list[list[int]] = []
    wanted: list[str] = []  # the slot each item of `options` stands for
    for slot_id in slots:
        groups = [candidates for _, candidates in window_claims[slot_id]]
        usable = {index for group in groups for index in group if evidence.get(index, slot_id) == slot_id}
        reserved = {index for index in usable if evidence.get(index) == slot_id}
        cover = _covering_runs([[index for index in group if index in usable] for group in groups], reserved)
        rest = sorted(usable.difference(cover), key=lambda index: (index not in reserved, index))
        for _ in cover:
            wanted.append(slot_id)
            options.append([*cover, *rest])
    matched = _one_each(options)
    result: dict[str, set[int]] = {slot_id: set() for slot_id in slots}
    for slot_id, index in zip(wanted, matched, strict=True):
        if index is not None:
            result[slot_id].add(index)
    return result


def _covering_runs(candidates: Sequence[Sequence[int]], preferred: AbstractSet[int]) -> list[int]:
    """The runs that explain every claim that has a candidate, the most useful first: the claims are `candidates`, run indexes each.

    Greedy: the run that explains the most of the claims left comes first, a `preferred` run before another
    that explains as many. Claims with no candidate need none.
    """
    left = [set(group) for group in candidates if group]
    cover: list[int] = []
    while left:
        best = max(
            sorted({index for group in left for index in group}),
            key=lambda index: (sum(index in group for group in left), index in preferred),
        )
        left = [group for group in left if best not in group]
        cover.append(best)
    return cover


def _is_going(run: AgentRun, now: datetime) -> bool:
    """Whether `run` has not stopped and began less than `RUN_STALE_AFTER` ago."""
    return run.stopped is None and now - run.started < RUN_STALE_AFTER


def _open_backers(
    runs: Sequence[AgentRun], claims: Sequence[SlotClaim], prepared: datetime, now: datetime
) -> list[int]:
    """The indexes of the open reviewer runs that may be the hunter of one of `claims` (all of one slot), best first.

    Only a run that started since the round was prepared can be: an earlier one belongs to an earlier round.
    By identity when a claim names its caller (Antigravity: the run of that conversation). Without one,
    any open run that started before the claim was made (the hunter calls `brief` after it starts) and was
    not stale by then: a run that had gone `RUN_STALE_AFTER` without a brief was taken for dead already,
    so it cannot be the hunter that a later relaunch stands for. A platform whose honest claims name
    the caller never matches by time. Runs that are still going come before the ones that are not, the
    older start first.
    """
    backers: set[int] = set()
    for claim in claims:
        caller = identity(claim.by)
        claimed_at = parse_timestamp(claim.at)
        for index, run in enumerate(runs):
            if run.role != "reviewer" or run.stopped is not None or run.started < prepared:
                continue
            if caller is not None:
                matches = (run.platform, run.agent_id) == caller
            else:
                matches = (
                    run.platform not in IDENTITY_PLATFORMS
                    and run.started <= claimed_at
                    and claimed_at - run.started < RUN_STALE_AFTER
                )
            if matches:
                backers.add(index)
    return sorted(backers, key=lambda index: (not _is_going(runs[index], now), runs[index].started, index))


def _one_each(options: Sequence[Sequence[int]]) -> list[int | None]:
    """A maximum matching of items to the runs they may take, one run each: each item's run, None if it got none.

    Items are taken in order. An item takes its first free option; only when all its options are taken
    does it move an earlier item to another option (an augmenting path). So an earlier item keeps its
    run unless that is the only way to give a later item one.
    """
    owner: dict[int, int] = {}  # run index -> the item holding it

    def take(item: int, tried: set[int]) -> bool:
        free = next((run for run in options[item] if run not in owner), None)
        if free is not None:
            owner[free] = item
            return True
        for run in options[item]:
            if run in tried:
                continue
            tried.add(run)
            if take(owner[run], tried):
                owner[run] = item
                return True
        return False

    for item in range(len(options)):
        take(item, set())
    result: list[int | None] = [None] * len(options)
    for run, item in owner.items():
        result[item] = run
    return result


def _stopped(run: AgentRun) -> datetime:
    assert run.stopped is not None  # vouching runs are finished
    return run.stopped


# --- hunt messages ---------------------------------------------------------------------------

_SLOT_ID = re.compile(r"(W\d-[A-Z]?)(\d+)")
_LAUNCH_HOW = (
    "For each, start a craftsman-reviewer subagent whose whole prompt is: "
    "`You are Craftsman hunter <ID>. Run craftsman review brief <ID> and follow it exactly.` "
    "(Claude Code: Agent tool, subagent_type craftsman-reviewer; Copilot: custom agent "
    "craftsman-reviewer; Antigravity: invoke_subagent, TypeName self, Role craftsman-reviewer.)"
)
_VERIFIER_HOW = (
    "For each cluster, start a craftsman-verifier subagent whose whole prompt is: "
    "`You are a Craftsman verifier. Run craftsman review brief --verifier <IDS> and follow it exactly.` "
    "A verdict you record yourself does not count."
)


def _compress(ids: Sequence[str], limit: int) -> str:
    """`ids` with runs of three or more consecutive slot numbers as `W1-1..W1-3`; at most `limit` ids, then `+N more`."""
    groups: list[list[str]] = []
    for item in ids[:limit]:
        match = _SLOT_ID.fullmatch(item)
        last = _SLOT_ID.fullmatch(groups[-1][-1]) if groups else None
        if match and last and match.group(1) == last.group(1) and int(match.group(2)) == int(last.group(2)) + 1:
            groups[-1].append(item)
        else:
            groups.append([item])
    parts = [f"{group[0]}..{group[-1]}" if len(group) >= 3 else ", ".join(group) for group in groups]
    if len(ids) > limit:
        parts.append(f"+{len(ids) - limit} more")
    return ", ".join(parts)


def _slot_ids(statuses: Sequence[SlotStatus]) -> str:
    return _compress([status.slot.id for status in statuses], MAX_LISTED_SLOTS)


def _launch_text(unlaunched: Sequence[SlotStatus], dead: Sequence[SlotStatus]) -> list[str]:
    """What to tell the caller to launch: the slots nobody has started, and the ones whose hunter died."""
    parts: list[str] = []
    if dead:
        parts.append(f"Stopped without finishing: {_slot_ids(dead)} - launch them again.")
    if unlaunched:
        parts.append(f"Launch now, all in one step: {_slot_ids(unlaunched)}. {_LAUNCH_HOW}")
    elif dead:
        parts.append(_LAUNCH_HOW)
    return parts


def _starting_text(count: int) -> str:
    subject = "reviewer subagent has" if count == 1 else "reviewer subagents have"
    return (
        f"Starting: {count} {subject} started since the round was prepared and not fetched a brief yet - if you "
        "launched them just now, do not launch more; end your turn and wait for their reports."
    )


def _pending_message(
    label: str, round_number: int, pending: Sequence[SlotStatus], *, starting: int = 0, stale: bool = False
) -> str:
    """The text of a `hunt-pending` or `wave-pending` issue.

    A non-zero `starting` is the number of hunters that were just launched and have not fetched a brief
    yet: for slots nobody works on the caller is told to wait for them instead of launching more. `stale` is a round whose code
    changed: nothing is launched for it.
    """
    running = [status for status in pending if status.state == "running"]
    dead = [status for status in pending if status.state == "dead"]
    unlaunched = [status for status in pending if status.state == "unlaunched"]
    parts = [f"{_counted(len(pending), (f'{label} slot', f'{label} slots'))} of round {round_number} not finished."]
    if stale:
        parts.append("The round is out of date, so launch no hunters for it; `craftsman review prepare` opens the next one.")
        return " ".join(parts)
    if running:
        parts.append(
            f"Running: {_slot_ids(running)} - do not launch these again; end your turn and wait for their reports."
        )
    if unlaunched or dead:
        # Hunters just launched may be the ones these slots wait for; say so only where there is something to launch.
        if starting:
            parts.append(_starting_text(starting))
        else:
            parts.extend(_launch_text(unlaunched, dead))
    return " ".join(parts)


def _agent_seen(ledger: Ledger, round_: Round) -> bool:
    """Whether the hooks recorded a craftsman reviewer run that began since the round was prepared.

    Judged on runs (`agent_runs`), not on raw events: the stop of a hunter of an earlier round that lands
    after this round was prepared is no hunter of this one, and a verifier is not a hunter.
    """
    prepared = parse_timestamp(round_.prepared_at)
    return any(run.role == "reviewer" and run.started >= prepared for run in agent_runs(ledger))


def _hunt_issues(
    ledger: Ledger, round_: Round, state: _HuntState, *, stale: bool
) -> tuple[list[GateIssue], list[GateIssue]]:
    """The issues of the slots: `(hunt-pending or no-isolation, wave-pending)`, which sit at different places in the order.

    A round whose slots are open while no subagent was recorded at all gets `no-isolation` instead of
    both: the hooks may be missing, and telling the caller to launch hunters again would repeat work.
    A round whose code changed (`stale`) only counts its open slots: its hunters would be wasted.
    """
    pending = [status for status in state.statuses if status.state != "done"]
    if not pending:
        return [], []
    if not stale and not _agent_seen(ledger, round_):
        message = (
            f"No Craftsman subagent was recorded for round {round_.number}. If you launched hunters, the "
            "Craftsman hooks did not see them: run `craftsman doctor` and fix the install; do not "
            "relaunch hunters that already ran. If you did not launch any, launch them now, all in one "
            f"step: {_slot_ids(pending)}. {_LAUNCH_HOW}"
        )
        return [GateIssue("no-isolation", message)], []
    first = [status for status in pending if status.slot.generation == 1]
    later = [status for status in pending if status.slot.generation >= 2]
    # Hunters that were just launched are waited for where they are enough for every slot nobody works on. Each
    # one is counted once: the generation 1 slots take their share first, the later generations the rest.
    idle_first = sum(status.state in ("unlaunched", "dead") for status in first)
    idle_later = sum(status.state in ("unlaunched", "dead") for status in later)
    share_first = min(state.starting, idle_first)
    share_later = min(state.starting - share_first, idle_later)
    starting_first = share_first if idle_first and share_first >= idle_first else 0
    starting_later = share_later if idle_later and share_later >= idle_later else 0
    return (
        [
            GateIssue(
                "hunt-pending",
                _pending_message("hunter", round_.number, first, starting=starting_first, stale=stale),
                missing=len(first),
            )
        ]
        if first
        else [],
        [
            GateIssue(
                "wave-pending",
                _pending_message("wave", round_.number, later, starting=starting_later, stale=stale),
                missing=len(later),
            )
        ]
        if later
        else [],
    )


# --- verdicts of an enforced round ---------------------------------------------------------------


def _hunk_of(ledger: Ledger, finding: Finding) -> str | None:
    """The hunk of its round the finding points into, if any."""
    if not 1 <= finding.round <= len(ledger.rounds):
        return None
    for ref in ledger.rounds[finding.round - 1].hunks:
        if ref.path == finding.path and ref.new_start <= finding.line < ref.new_start + max(ref.new_len, 1):
            return ref.id
    return None


def _finding_number(finding_id: str) -> int:
    return int(finding_id[1:])


def verifier_clusters(ledger: Ledger, findings: Sequence[Finding]) -> tuple[tuple[str, ...], ...]:
    """Group `findings` for verifiers: one cluster per file, at most `MAX_CLUSTER_FINDINGS` findings each.

    Findings keep their order. Findings in the same hunk and lens (duplicate candidates) stay in one
    cluster, unless there are more of them than a cluster holds.
    """
    by_path: dict[str, dict[tuple[str, str | None], list[Finding]]] = defaultdict(lambda: defaultdict(list))
    for finding in findings:
        by_path[finding.path][(finding.lens, _hunk_of(ledger, finding))].append(finding)
    clusters: list[tuple[str, ...]] = []
    for groups in by_path.values():
        current: list[Finding] = []
        for group in groups.values():
            for start in range(0, len(group), MAX_CLUSTER_FINDINGS):
                chunk = group[start : start + MAX_CLUSTER_FINDINGS]
                if len(current) + len(chunk) > MAX_CLUSTER_FINDINGS:
                    clusters.append(_cluster_ids(current))
                    current = []
                current.extend(chunk)
        if current:
            clusters.append(_cluster_ids(current))
    return tuple(clusters)


def _cluster_ids(findings: Sequence[Finding]) -> tuple[str, ...]:
    return tuple(sorted((finding.id for finding in findings), key=_finding_number))


def _verifier_text(ledger: Ledger, findings: Sequence[Finding], running: int) -> str:
    """Who to launch for `findings`; with `running` verifiers already going for every cluster, to wait for them."""
    clusters = verifier_clusters(ledger, findings)
    if running >= len(clusters):
        subject = "verifier is" if running == 1 else "verifiers are"
        return (
            f"Verifiers running: {running} {subject} still at work - do not launch more; "
            "end your turn and wait for their reports."
        )
    shown = " ".join(f"[{','.join(cluster)}]" for cluster in clusters[:MAX_LISTED_CLUSTERS])
    more = f" (+{len(clusters) - MAX_LISTED_CLUSTERS} more clusters)" if len(clusters) > MAX_LISTED_CLUSTERS else ""
    return f"Launch verifiers, one per cluster: {shown}{more}. {_VERIFIER_HOW}"


def _finding_ids(findings: Sequence[Finding]) -> str:
    shown = ", ".join(finding.id for finding in findings[:MAX_LISTED_FINDINGS])
    extra = len(findings) - MAX_LISTED_FINDINGS
    return f"{shown} (+{extra} more)" if extra > 0 else shown


def _verification_issues(ledger: Ledger, round_: Round, runs: Sequence[AgentRun], now: datetime) -> list[GateIssue]:
    """The findings of an enforced round must be judged by verifier subagents, two of them for a dismissed serious one.

    Findings from before verdict events existed (an empty log) are exempt from the vouching rules.
    Verifiers that started since the round was prepared and are still going are waited for, not launched again.
    """
    prepared = parse_timestamp(round_.prepared_at)
    running = sum(
        run.role == "verifier" and run.started >= prepared and _is_going(run, now) for run in runs
    )
    no_verdict: list[Finding] = []
    unverified: list[Finding] = []
    second: list[Finding] = []
    for finding in ledger.findings:
        if finding.verdict is None:
            no_verdict.append(finding)
        elif not finding.verdict_log:
            continue
        elif not verdict_vouched(ledger, finding, finding.verdict_log[-1], runs):
            unverified.append(finding)
        elif (
            finding.severity in ("blocker", "major")
            and finding.verdict == "dismissed"
            and finding.verdict_run is None
            and distinct_verdict_runs(ledger, finding, runs) < 2
        ):
            second.append(finding)
    issues: list[GateIssue] = []
    if no_verdict:
        issues.append(
            GateIssue(
                "no-verdict",
                f"{_counted(len(no_verdict), ('finding', 'findings'))} without a verdict: "
                f"{_finding_ids(no_verdict)}. {_verifier_text(ledger, no_verdict, running)}",
                missing=len(no_verdict),
            )
        )
    if unverified:
        issues.append(
            GateIssue(
                "unverified",
                f"{_counted(len(unverified), ('verdict', 'verdicts'))} not recorded by a verifier subagent: "
                f"{_finding_ids(unverified)}. {_verifier_text(ledger, unverified, running)}",
                missing=len(unverified),
            )
        )
    if second:
        issues.append(
            GateIssue(
                "second-opinion",
                f"{_counted(len(second), ('dismissed blocker or major finding', 'dismissed blocker or major findings'))} "
                f"still need a second, independent verifier: {_finding_ids(second)}. {_verifier_text(ledger, second, running)}",
                missing=len(second),
            )
        )
    return issues


def _unresolved_issues(ledger: Ledger, snap: DiffSnapshot) -> list[GateIssue]:
    issues: list[GateIssue] = []
    for finding in ledger.findings:
        label = f"Finding {finding.id} ({finding.severity}: {defang_tokens(inline_md(finding.title))})"
        if finding.verdict in ("proven", "probable") and finding.resolution is None:
            alternative = "" if finding.severity == "blocker" else ' (or `--as accepted --note "why"`)'
            issues.append(
                GateIssue(
                    "unresolved",
                    f"{label} is {finding.verdict} but unresolved; fix the code, then run "
                    f'`craftsman review resolve {finding.id} --as fixed --note "..."`{alternative}.',
                )
            )
        elif finding.resolution == "fixed" and finding.fingerprint == snap.fingerprint:
            issues.append(
                GateIssue(
                    "unresolved",
                    f"{label} was resolved as fixed, but the code is back to the code it was "
                    "recorded at; the fix was reverted, apply it again.",
                )
            )
    return issues


def _counting_runs(ledger: Ledger, kinds: Sequence[RunKind]) -> list[Run]:
    return [run for run in ledger.runs if run.kind in kinds and not is_trivial_run(run.argv)]


def passing_runs(ledger: Ledger, fingerprint: str) -> list[Run]:
    """The non-trivial test and e2e runs that passed at `fingerprint`: what the gate counts."""
    return [
        run
        for run in _counting_runs(ledger, PASSING_KINDS)
        if run.counts_as_passing and run.fingerprint == fingerprint
    ]


def _passing_run_issues(ledger: Ledger, snap: DiffSnapshot, policy: GatePolicy) -> list[GateIssue]:
    passing = passing_runs(ledger, snap.fingerprint)
    issues: list[GateIssue] = []
    if not passing:
        issues.append(
            GateIssue(
                "no-passing-run",
                "No passing test or e2e run at the current code; "
                "run `craftsman run --kind test -- <your test command>`.",
            )
        )
    elif policy.deep and not any(run.kind == "test" for run in passing):
        issues.append(
            GateIssue(
                "no-passing-run",
                "A deep gate needs a passing test run as well as the e2e run at the current "
                "code; run `craftsman run --kind test -- <your test command>`.",
            )
        )
    if policy.deep and not any(run.kind == "e2e" for run in passing):
        issues.append(
            GateIssue(
                "no-e2e-run",
                "A deep gate needs a passing end-to-end run at the current code; run "
                "`craftsman run --kind e2e -- <command that exercises the real flow>`.",
            )
        )
    return issues


def _unwaived_failing_runs(ledger: Ledger) -> list[Run]:
    """Latest non-trivial verifying runs per (cwd, command) that failed without a waiver."""
    latest = {
        (run.cwd, run.argv): run
        for run in _counting_runs(ledger, _VERIFYING_KINDS)
        if run.exit_code != 0 or run.counts_as_passing
    }
    return [run for run in latest.values() if run.exit_code != 0 and run.waiver is None]


def _failing_run_issues(ledger: Ledger) -> list[GateIssue]:
    """The latest run of each command (per working directory) must not fail unless it was waived.

    A passing run that changed the work tree does not clear an earlier failure.
    """
    return [
        GateIssue(
            "failing-run",
            f"`{run.command}` ({run.kind}, {run.id}) last exited {run.exit_code}; fix the failure "
            f"and re-run `craftsman run --kind {run.kind} -- {run.command}`. If the command itself "
            f"is obsolete (a typo, a renamed test), retire it with "
            f'`craftsman review waive-run {run.id} --reason "..."`.',
        )
        for run in _unwaived_failing_runs(ledger)
    ]


@dataclass(frozen=True)
class _ReviewerRun:
    """A finished `craftsman-reviewer` run: when it ran, and the records its transcript shows."""

    started: datetime
    stopped: datetime
    records: tuple[str, ...] | None  # None: no transcript evidence, so records go by time

    def vouches_for(self, token: str, recorded_at: datetime) -> bool:
        """Whether this run wrote the record `token` names, made at `recorded_at`.

        By time always: nobody writes a record before it started or after it stopped, so a reviewer
        resumed across `review reset` (which restarts the numbering) cannot claim a new record with
        an old token. By identity as well where the transcript was read.
        """
        if not self.started <= recorded_at <= self.stopped:
            return False
        return self.records is None or token in self.records


def _reviewer_runs(ledger: Ledger) -> list[_ReviewerRun]:
    """Every finished `craftsman-reviewer` run.

    A stop with an agent id starts at the latest start of that id, which then leaves the queue of
    unpaired starts. Any other stop (no id, or an id no start carries, as Copilot starts by name
    only) is paired first-in, first-out with the unpaired starts of its name, so parallel runs of
    one name keep their union window. A stop without a start counts from the round that was current.
    """
    runs: list[_ReviewerRun] = []
    starts: list[tuple[str, str, datetime]] = []  # (agent name, agent id, when)
    taken: set[int] = set()  # indexes into `starts` that a stop has claimed
    for event in ledger.agents:
        if event.event == "start":
            starts.append((event.agent, event.agent_id, parse_timestamp(event.at)))
        elif agent_role(event.agent) == "reviewer":
            stopped = parse_timestamp(event.at)
            by_id = [i for i, (_, agent_id, _) in enumerate(starts) if event.agent_id and agent_id == event.agent_id]
            by_name = [i for i, (name, _, _) in enumerate(starts) if name == event.agent and i not in taken]
            chosen = by_id[-1] if by_id else by_name[0] if by_name else None
            if chosen is not None:
                taken.add(chosen)
                started = starts[chosen][2]
            else:
                current = [r for r in ledger.rounds if parse_timestamp(r.prepared_at) <= stopped]
                started = parse_timestamp(current[-1].prepared_at) if current else stopped
            runs.append(_ReviewerRun(started, stopped, event.records))
    return runs


def _record_times(ledger: Ledger) -> dict[str, datetime]:
    """When each record was made, by token: `C<n>` is `ledger.coverage[n - 1]`, `F<n>` the finding."""
    times = {f"C{index}": parse_timestamp(item.recorded_at) for index, item in enumerate(ledger.coverage, 1)}
    times.update({item.id: parse_timestamp(item.recorded_at) for item in ledger.findings})
    return times


def _latest_has_reviewer(ledger: Ledger, runs: Sequence[_ReviewerRun]) -> bool:
    """Whether a reviewer finished, having recorded something, while the latest round was current.

    Where a run's transcript was read, one of its own records must date from this round and from
    the run's window: a run whose tokens all belong to an earlier round does not count. Tokens no
    record carries are ignored.
    """
    start = parse_timestamp(ledger.rounds[-1].prepared_at)
    times = _record_times(ledger)
    for run in runs:
        if run.stopped < start:
            continue
        mine = times.values() if run.records is None else [times[t] for t in run.records if t in times]
        if any(max(start, run.started) <= at <= run.stopped for at in mine):
            return True
    return False


def _unexamined_hunks(ledger: Ledger, runs: Sequence[_ReviewerRun]) -> dict[int, list[str]]:
    """The hunks covered only outside every reviewer run, by round number.

    Coverage counts when a finished reviewer vouches for its record (`C<n>`, its position in the
    append-only coverage list): by identity where the run's transcript was read, else by time.
    Findings need no reviewer: verifiers judge every one. A round with no coverage holds nothing
    to examine, so one replaced before anyone reviewed it passes.
    """
    examined: dict[tuple[int, str], bool] = {}
    for index, record in enumerate(ledger.coverage, 1):
        recorded = parse_timestamp(record.recorded_at)
        vouched = any(run.vouches_for(f"C{index}", recorded) for run in runs)
        for digest in record.digests:
            key = (record.round, digest)
            examined[key] = examined.get(key, False) or vouched
    return {
        round_.number: ids
        for round_ in ledger.rounds
        if (ids := [h.id for h in round_.hunks if not h.carried and examined.get((round_.number, h.digest)) is False])
    }


@dataclass(frozen=True)
class _ReviewGaps:
    unexamined: dict[int, list[str]]  # round number -> hunks covered outside any reviewer run
    latest_lacks_reviewer: bool

    def __bool__(self) -> bool:
        return self.latest_lacks_reviewer or bool(self.unexamined)


def _review_gaps(ledger: Ledger) -> _ReviewGaps:
    """Where the isolated review is missing: the latest round needs a reviewer that recorded
    something; every round needs a reviewer behind each hunk it covered. A round whose hunks are
    all carried over needs no new reviewer."""
    if not ledger.rounds:
        return _ReviewGaps({}, False)
    runs = _reviewer_runs(ledger)
    return _ReviewGaps(
        _unexamined_hunks(ledger, runs),
        not all(hunk.carried for hunk in ledger.rounds[-1].hunks) and not _latest_has_reviewer(ledger, runs),
    )


def _isolation_issues(ledger: Ledger, policy: GatePolicy) -> list[GateIssue]:
    if not policy.require_isolation:
        return []
    latest_number = len(ledger.rounds)
    gaps = _review_gaps(ledger)
    latest: list[str] = []
    outside = gaps.unexamined.get(latest_number, [])
    if gaps.latest_lacks_reviewer:
        hint = (
            " Antigravity records finished reviewers from the transcript when your turn ends."
            if policy.isolation_evidence == "transcript"
            else ""
        )
        latest.append(
            f"No independent review finished since round {latest_number} was prepared; spawn "
            f"the {REVIEWER_AGENT} subagent for round {latest_number}.{hint}"
        )
    if outside:
        listed = _listed(outside)
        latest.append(
            f"Hunks {listed} of round {latest_number} were covered outside any finished "
            f"{REVIEWER_AGENT} run. Only a {REVIEWER_AGENT}'s own coverage counts, once it finishes: "
            f"if the reviewer covering these hunks is still running, wait for it; otherwise have a "
            f"{REVIEWER_AGENT} examine them and record its own coverage "
            f'(`craftsman review cover --hunks {",".join(outside[:MAX_LISTED_HUNKS])} --note "..."`).'
        )
    messages = [" ".join(latest)] if latest else []
    for number, ids in gaps.unexamined.items():
        if number != latest_number:
            messages.append(
                f"Round {number} has coverage that no {REVIEWER_AGENT} run examined (hunks "
                f"{_listed(ids)}), and a later round cannot make up for it. Run `craftsman "
                f"review reset`, prepare again and have the {REVIEWER_AGENT} examine the whole diff."
            )
    return [GateIssue("no-isolation", message) for message in messages]


def _listed(ids: Sequence[str]) -> str:
    more = f" (+{len(ids) - MAX_LISTED_HUNKS} more)" if len(ids) > MAX_LISTED_HUNKS else ""
    return ",".join(ids[:MAX_LISTED_HUNKS]) + more


def _round_cap_issues(ledger: Ledger) -> list[GateIssue]:
    diffs = len({round_.fingerprint for round_ in ledger.rounds})
    if diffs <= MAX_ROUNDS:
        return []
    return [
        GateIssue(
            "round-cap",
            f"{diffs} review rounds of different diffs exceed the cap of {MAX_ROUNDS}; stop "
            "iterating and report the remaining issues to the user.",
        )
    ]


def _process_anomaly_warnings(ledger: Ledger) -> list[str]:
    return [
        *_all_confirmed_warnings(ledger),
        *_retried_with_other_env_warnings(ledger),
    ]


def _all_confirmed_warnings(ledger: Ledger) -> list[str]:
    findings = ledger.findings
    if len(findings) < ANOMALY_MIN_FINDINGS:
        return []
    if any(finding.verdict is None or finding.verdict == "dismissed" for finding in findings):
        return []
    return [
        f"Process anomaly: {len(findings)} findings and 0 dismissed "
        "(all findings confirmed — typical of self-review)."
    ]


def _retried_with_other_env_warnings(ledger: Ledger) -> list[str]:
    """A failing test rerun with the same code under another environment, e.g. `env CI= pytest`."""
    unretried: dict[tuple[str, tuple[str, ...]], list[Run]] = {}
    warnings: list[str] = []
    for run in ledger.runs:
        if run.kind not in PASSING_KINDS:
            continue
        failures = unretried.setdefault((run.fingerprint, tuple(command_without_env(run.argv))), [])
        retried = [failed for failed in failures if failed.argv != run.argv]
        warnings.extend(
            f"Process anomaly: {failed.kind} run {failed.id} failed and was repeated as "
            f"{run.id} with a different environment without a code change."
            for failed in retried
        )
        failures[:] = [failed for failed in failures if failed not in retried]
        if run.exit_code != 0:
            failures.append(run)
    return warnings


def _review_only_warnings(ledger: Ledger) -> list[str]:
    warnings: list[str] = []
    for_author = [
        finding.id
        for finding in ledger.findings
        if finding.verdict in ("proven", "probable") and finding.resolution is None
    ]
    if for_author:
        noun = "finding" if len(for_author) == 1 else "findings"
        warnings.append(f"{len(for_author)} {noun} for the author: {', '.join(for_author)}.")
    for run in _unwaived_failing_runs(ledger):
        warnings.append(
            f"Failing run {run.id} (`{run.command}`, {run.kind}) exited {run.exit_code}."
        )
    return warnings


def _warnings(ledger: Ledger, *, review_only: bool = False) -> tuple[str, ...]:
    warnings = _review_only_warnings(ledger) if review_only else []
    accepted = [finding.id for finding in ledger.findings if finding.resolution == "accepted"]
    if accepted:
        warnings.append(f"Accepted without a fix: {', '.join(accepted)}; tell the user about them.")
    questions = [finding.id for finding in ledger.findings if finding.verdict == "question"]
    if questions:
        warnings.append(
            f"Design questions for the author: {', '.join(questions)}; highlight them in the report."
        )
    waived = [run.id for run in ledger.runs if run.waiver is not None]
    if waived:
        warnings.append(f"Waived failing runs: {', '.join(waived)}; tell the user about them.")
    warnings.extend(_large_diff_warnings(ledger))
    warnings.extend(
        f"{run.id} (`{run.command}`) is trivial and does not count as verification."
        for run in ledger.runs
        if is_trivial_run(run.argv)
    )
    warnings.extend(_process_anomaly_warnings(ledger))
    return tuple(warnings)


def large_diff_warning(round_: Round) -> str | None:
    """A warning when the round's quality signals flag a diff too big for a thorough review."""
    for sig in round_.signals:
        if sig.kind is SignalKind.DIFF_SIZE:
            return f"Very large diff: {sig.detail}. Consider splitting the change; a review of this size misses things."
    return None


def _large_diff_warnings(ledger: Ledger) -> list[str]:
    warning = large_diff_warning(ledger.rounds[-1]) if ledger.rounds else None
    return [warning] if warning else []


def _summary(ledger: Ledger, snap: DiffSnapshot, *, slots: Sequence[SlotStatus] | None = None) -> str:
    """The summary lines; `slots` (an enforced round's) replace the hunk coverage, which that round does not judge by."""
    if not ledger.rounds:
        round_line = "Round: none prepared yet."
        coverage_line = "Coverage: n/a."
    else:
        latest = ledger.rounds[-1]
        current = latest.fingerprint == snap.fingerprint
        kind = f"{'deep, ' if latest.deep else ''}{'current' if current else 'stale'}"
        if current:
            round_line = (
                f"Round {latest.number} ({kind}): "
                f"{len(latest.hunks)} hunks in {len(snap.files)} files, +{snap.added}/-{snap.deleted} lines."
            )
        else:  # the hunks are the round's own, so the files must be too; the diff now is another snapshot
            round_files = len({ref.path for ref in latest.hunks})
            round_line = (
                f"Round {latest.number} ({kind}): {len(latest.hunks)} hunks in {round_files} files as prepared; "
                f"the diff now has {len(snap.files)} files, +{snap.added}/-{snap.deleted} lines."
            )
        coverage_line = _coverage_summary(ledger, latest) if slots is None else _slot_summary(latest, slots)
    return "\n".join((round_line, coverage_line, _findings_summary(ledger), _runs_summary(ledger, snap)))


def _coverage_summary(ledger: Ledger, latest: Round) -> str:
    total = len(latest.open_hunk_ids)
    done = total - len(uncovered_hunks(ledger, latest))
    carried = len(latest.hunks) - total
    return f"Coverage: {done}/{total} hunks; {carried} hunk(s) carried from earlier rounds."


def _slot_summary(latest: Round, slots: Sequence[SlotStatus]) -> str:
    """Progress of an enforced round: the finished slots; coverage the orchestrator recorded itself shows nowhere."""
    done = sum(status.state == "done" for status in slots)
    carried = len(latest.hunks) - len(latest.open_hunk_ids)
    return f"Coverage: {done}/{len(slots)} slots done; {carried} hunk(s) carried from earlier rounds."


def _findings_summary(ledger: Ledger) -> str:
    if not ledger.findings:
        return "Findings: none."
    findings = ledger.findings
    awaiting = sum(finding.verdict is None for finding in findings)
    dismissed = sum(finding.verdict == "dismissed" for finding in findings)
    questions = [finding.id for finding in findings if finding.verdict == "question"]
    unresolved = sum(
        finding.verdict in ("proven", "probable") and finding.resolution is None for finding in findings
    )
    fixed = sum(finding.resolution == "fixed" for finding in findings)
    accepted = [finding.id for finding in findings if finding.resolution == "accepted"]
    accepted_detail = f" ({', '.join(accepted)})" if accepted else ""
    question_part = f", {len(questions)} question(s) ({', '.join(questions)})" if questions else ""
    return (
        f"Findings: {len(findings)} total; {awaiting} awaiting verdict, {unresolved} unresolved, "
        f"{fixed} fixed, {len(accepted)} accepted{accepted_detail}{question_part}, {dismissed} dismissed."
    )


def _runs_summary(ledger: Ledger, snap: DiffSnapshot) -> str:
    if not ledger.runs:
        return "Runs: none recorded."
    recent = ", ".join(
        f"{run.id} {run.kind} `{run.short_command}` exit {run.exit_code} "
        f"({'current' if run.fingerprint == snap.fingerprint else 'stale'})"
        for run in ledger.runs[-SUMMARY_RECENT_RUNS:]
    )
    return f"Runs: {len(ledger.runs)} recorded; latest: {recent}."


def render_next_actions(issues: Sequence[GateIssue], *, max_items: int = MAX_NEXT_ACTIONS) -> list[str]:
    """Numbered next-action lines, capped at `max_items` with a trailing count of the rest."""
    lines = [
        f"{index}. [{issue.code}] {issue.message}" for index, issue in enumerate(issues[:max_items], 1)
    ]
    if len(issues) > max_items:
        lines.append(f"… and {len(issues) - max_items} more")
    return lines


def hunt_line(slots: Sequence[SlotStatus]) -> str:
    """`Hunt: 5 slots - 3 done, 1 running (W1-4), 1 pending (W2-X1)`; stopped hunters are listed too."""
    if not slots:
        return "Hunt: no slots"
    parts: list[str] = []
    done = sum(status.state == "done" for status in slots)
    if done:
        parts.append(f"{done} done")
    for state, label in (("running", "running"), ("dead", "stopped"), ("unlaunched", "pending")):
        members = [status for status in slots if status.state == state]
        if members:
            parts.append(f"{len(members)} {label} ({_slot_ids(members)})")
    return f"Hunt: {_counted(len(slots), ('slot', 'slots'))} - {', '.join(parts)}"


def render_report(report: GateReport) -> str:
    """Render a gate report as a compact Markdown block with numbered next actions."""
    lines = [f"**Craftsman {report.subject}: {report.status_label}**"]
    if report.self_reviewed:
        lines.append(f"**{SELF_REVIEWED_BANNER}**")
    lines += ["", report.summary]
    if report.slots:
        lines.append(hunt_line(report.slots))
    if report.issues:
        lines += ["", "**Next actions**", *render_next_actions(report.issues)]
    if report.warnings:
        lines += ["", "**Warnings**", *(f"- {warning}" for warning in report.warnings)]
    return "\n".join(lines)


# One summary clause per issue code, in the order the agent should address them.
_ROUND_CLAUSES: dict[IssueCode, str] = {
    "no-round": "no review round prepared",
    "stale-round": "code changed since the review round",
    "round-cap": "maximum review rounds reached",
}
_COUNTED_CLAUSES: dict[IssueCode, tuple[str, str]] = {
    "hunt-pending": ("hunter slot pending", "hunter slots pending"),
    "no-verdict": ("finding without a verdict", "findings without a verdict"),
    "unverified": ("verdict not made by a verifier", "verdicts not made by a verifier"),
    "second-opinion": ("dismissal needing a second verifier", "dismissals needing a second verifier"),
    "wave-pending": ("wave slot pending", "wave slots pending"),
    "unresolved": ("unresolved finding", "unresolved findings"),
}
_RUN_CLAUSES: dict[IssueCode, str] = {
    "no-passing-run": "no passing test run",
    "no-e2e-run": "no passing e2e run",
}


def _counted(count: int, forms: tuple[str, str]) -> str:
    return f"{count} {forms[0] if count == 1 else forms[1]}"


def _coverage_clauses(issues: Sequence[GateIssue]) -> list[str]:
    total = sum(issue.missing for issue in issues if issue.code == "uncovered")
    return [f"{_counted(total, ('hunk', 'hunks'))} not covered"] if total else []


def format_blocker_summary(report: GateReport) -> str:
    """One line naming what still blocks the gate, e.g. `review incomplete: 3 hunks not covered;
    1 finding without a verdict`."""
    if not report.issues:
        return "review complete"
    codes = [issue.code for issue in report.issues]
    clauses = [clause for code, clause in _ROUND_CLAUSES.items() if code in codes]
    clauses += _coverage_clauses(report.issues)
    clauses += [
        _counted(sum(issue.missing or 1 for issue in report.issues if issue.code == code), forms)
        for code, forms in _COUNTED_CLAUSES.items()
        if code in codes
    ]
    clauses += [clause for code, clause in _RUN_CLAUSES.items() if code in codes]
    if "failing-run" in codes:
        clauses.append(_counted(codes.count("failing-run"), ("failing run", "failing runs")))
    if "no-isolation" in codes:
        clauses.append("no reviewer isolation evidence")
    return f"review incomplete: {'; '.join(clauses) or _counted(len(codes), ('open item', 'open items'))}"
