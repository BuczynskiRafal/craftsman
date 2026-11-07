"""Who did what: attributing ledger records to the subagent runs that made them.

Pure functions of a `Ledger`; nothing here reads the file system or the clock. A record counts as
a hunter's or a verifier's work only when a finished run of the right role vouches for it, so the
orchestrator cannot cover hunks or record verdicts in place of its subagents.

A run vouches for a record in one of three ways, from the strongest:
- identity: the record names its caller (`by`, `antigravity:<conversation id>`), and that is the run;
- transcript: the run's own transcript shows it printed the record's token (`C3`, `V2`);
- time: the record was made inside the run's window (Copilot, or a transcript that could not be read).

Time is the weakest, so it is bounded: see `slot_work` for the cap on slots it can credit.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from craftsman.ledger import reopened_in
from craftsman.lenses import LENS_IDS
from craftsman.records import (
    AgentEvent,
    AgentRole,
    Coverage,
    Finding,
    Ledger,
    VerdictEvent,
    agent_role,
    craftsman_agent_name,
    parse_timestamp,
)
from craftsman.triviality import is_trivial_run

# Test and end-to-end runs are what counts as verification (the same kinds as `gatecheck.PASSING_KINDS`,
# which cannot be imported here: the gate imports this module).
_PASSING_KINDS = ("test", "e2e")


@dataclass(frozen=True)
class AgentRun:
    """One craftsman subagent run, paired from its start and stop events."""

    role: AgentRole
    platform: str
    agent_id: str
    started: datetime
    stopped: datetime | None  # None: no stop event (still running, or died)
    records: tuple[str, ...] | None  # tokens from the run's transcript; None = unknown

    def contains(self, moment: datetime) -> bool:
        """Whether `moment` falls inside the finished run's window."""
        return self.stopped is not None and self.started <= moment <= self.stopped


@dataclass(frozen=True)
class Voucher:
    """A run that vouches for a record, and how it knows.

    `exact` is true for identity and transcript attribution, false for a time window. `index` is the
    position of the run in the sequence that was searched, which tells two equal runs apart.
    """

    run: AgentRun
    exact: bool
    index: int


@dataclass(frozen=True)
class _Start:
    event: AgentEvent
    role: AgentRole
    at: datetime


def agent_runs(ledger: Ledger) -> tuple[AgentRun, ...]:
    """Every craftsman reviewer and verifier run in `ledger.agents`, oldest start first.

    A stop with an agent id pairs with the latest start of that id. Any other stop (no id, or an id
    no start carries, as Copilot starts by name only) pairs first-in, first-out with the unpaired
    starts of its name, so parallel runs of one name keep their own windows. A stop without a start
    begins at the `prepared_at` of the round that was current. Starts no stop claims become runs
    that have not stopped. Agents that are not a craftsman role are ignored.
    """
    starts: list[_Start] = []
    taken: set[int] = set()  # indexes into `starts` that a stop has claimed
    runs: list[AgentRun] = []
    for event in ledger.agents:
        role = agent_role(event.agent)
        if role is None:
            continue
        at = parse_timestamp(event.at)
        if event.event == "start":
            starts.append(_Start(event, role, at))
            continue
        by_id = [i for i, item in enumerate(starts) if event.agent_id and item.event.agent_id == event.agent_id]
        name = craftsman_agent_name(event.agent)
        by_name = [
            i
            for i, item in enumerate(starts)
            if i not in taken and craftsman_agent_name(item.event.agent) == name
        ]
        chosen = by_id[-1] if by_id else by_name[0] if by_name else None
        if chosen is not None:
            taken.add(chosen)
            started = min(starts[chosen].at, at)
            agent_id = event.agent_id or starts[chosen].event.agent_id
            platform = starts[chosen].event.platform
        else:
            current = [r for r in ledger.rounds if parse_timestamp(r.prepared_at) <= at]
            started = parse_timestamp(current[-1].prepared_at) if current else at
            agent_id = event.agent_id
            platform = event.platform
        runs.append(AgentRun(role, platform, agent_id, started, at, event.records))
    runs.extend(
        AgentRun(item.role, item.event.platform, item.event.agent_id, item.at, None, None)
        for index, item in enumerate(starts)
        if index not in taken
    )
    return tuple(sorted(runs, key=lambda run: run.started))


def identity(by: str | None) -> tuple[str, str] | None:
    """`(platform, id)` from a caller identity such as `antigravity:<conversation id>`; None when `by` names none."""
    if by is None:
        return None
    platform, separator, agent_id = by.partition(":")
    if not separator or not platform or not agent_id:
        return None
    return platform, agent_id


def vouching_runs(
    runs: Sequence[AgentRun], *, role: AgentRole, token: str, recorded_at: datetime, by: str | None
) -> tuple[Voucher, ...]:
    """The finished runs of `role` that vouch for the record `token`, made at `recorded_at` by `by`.

    A record that names its caller is vouched by that caller's run alone: identity is decisive, so
    it never falls back to whichever run happened to be going on. Without one, a run whose transcript
    was read vouches when it shows the token, and a run whose transcript was not read when the
    record falls in its window. A transcript that shows the token settles who made the record, so a
    window then vouches only when no transcript does: an idle unread run overlapping an exactly
    attributed record neither gets credit for it nor lets it count for a second agent.
    """
    caller = identity(by)
    exact: list[Voucher] = []
    window: list[Voucher] = []
    for index, run in enumerate(runs):
        if run.role != role or not run.contains(recorded_at):
            continue
        if caller is not None:
            if (run.platform, run.agent_id) == caller:
                exact.append(Voucher(run, True, index))
        elif run.records is None:
            window.append(Voucher(run, False, index))
        elif token in run.records:
            exact.append(Voucher(run, True, index))
    return tuple(exact or window)


# --- slot work ------------------------------------------------------------------------------

SlotLenses = Callable[[int, str], tuple[str, ...] | None]  # (round number, slot id) -> lenses, None if unknown
SlotOrder = Callable[[int], Sequence[str]]  # round number -> the slot ids of its plan, in plan order


@dataclass(frozen=True)
class SlotWork:
    """What was examined under one slot of one round, counting only acceptable records."""

    round: int
    slot: str
    digests: frozenset[str]  # digests covered under this slot by acceptable records
    first_at: datetime | None  # earliest acceptable record time
    runs: tuple[str, ...]  # agent ids of the runs whose records were accepted


@dataclass(frozen=True)
class _Tagged:
    token: str
    record: Coverage
    at: datetime
    vouchers: tuple[Voucher, ...]


def _tagged_records(ledger: Ledger, runs: Sequence[AgentRun]) -> dict[int, list[_Tagged]]:
    """The coverage records that name a slot and that a reviewer run vouches for, by round."""
    by_round: dict[int, list[_Tagged]] = defaultdict(list)
    for index, record in enumerate(ledger.coverage, 1):
        if record.hunter is None:
            continue
        token = f"C{index}"
        at = parse_timestamp(record.recorded_at)
        vouchers = vouching_runs(runs, role="reviewer", token=token, recorded_at=at, by=record.by)
        if vouchers:
            by_round[record.round].append(_Tagged(token, record, at, vouchers))
    return by_round


def _run_key(run: AgentRun, index: int) -> str:
    """What makes two runs one: the agent id (a resumed agent keeps it), or the position when a platform reports none."""
    return f"id:{run.agent_id}" if run.agent_id else f"run:{index}"


def _bound_slots(
    ledger: Ledger,
    runs: Sequence[AgentRun],
    tagged: Mapping[int, Sequence[_Tagged]],
    slot_order: SlotOrder,
) -> dict[tuple[int, str], str]:
    """The one slot each exactly attributed agent answers for in each round, by (round, run key).

    That is the slot of the agent's earliest claim of the round that it vouches for exactly; when it
    vouches for no claim, the first slot, in plan order, among the coverage records it vouches for
    exactly. Runs of one agent id (a resumed agent) are one agent, so resuming never unlocks a slot.
    """
    claimed: dict[tuple[int, str], tuple[datetime, str]] = {}
    for claim in ledger.claims:
        at = parse_timestamp(claim.at)
        for voucher in vouching_runs(runs, role="reviewer", token=claim.token, recorded_at=at, by=claim.by):
            slot_key = (claim.round, _run_key(voucher.run, voucher.index))
            if voucher.exact and (slot_key not in claimed or at < claimed[slot_key][0]):
                claimed[slot_key] = (at, claim.slot)
    bound = {slot_key: slot for slot_key, (_, slot) in claimed.items()}
    for number, items in tagged.items():
        order = {slot: place for place, slot in enumerate(slot_order(number))}
        first: dict[str, str] = {}
        for item in items:
            assert item.record.hunter is not None
            for voucher in item.vouchers:
                run_key = _run_key(voucher.run, voucher.index)
                best = first.get(run_key)
                if voucher.exact and (
                    best is None
                    or (order.get(item.record.hunter, len(order)), item.record.hunter)
                    < (order.get(best, len(order)), best)
                ):
                    first[run_key] = item.record.hunter
        for run_key, slot in first.items():
            bound.setdefault((number, run_key), slot)
    return bound


def slot_work(
    ledger: Ledger, runs: Sequence[AgentRun], slot_order: SlotOrder
) -> dict[tuple[int, str], SlotWork]:
    """The acceptable work per (round, slot).

    A coverage record tagged with slot S of round r is acceptable when a finished reviewer run
    vouches for it and
    - exactly (identity or transcript), and the agent is bound to S: each agent answers for ONE slot
      per round (see `_bound_slots`), so one subagent cannot do several slots, whether by fetching
      several briefs or by being resumed, and the orchestrator, outside every run, answers for none; or
    - by time window only. Parallel runs overlap, so a window cannot tell their claims apart and skips
      binding; instead the slots that rest on windows alone ("window slots") are capped at the number
      of distinct runs vouching for them, keeping the first in plan order. Slots with an exact
      voucher are never capped, and they are credited with their exact records only: a window record
      tagged with such a slot comes from an agent nobody can name, not from the slot's bound hunter.
    """
    tagged = _tagged_records(ledger, runs)
    bound = _bound_slots(ledger, runs, tagged, slot_order)
    work: dict[tuple[int, str], SlotWork] = {}
    for number, items in tagged.items():
        order = {slot: place for place, slot in enumerate(slot_order(number))}

        def rank(slot: str, order: Mapping[str, int] = order) -> tuple[int, str]:
            return order.get(slot, len(order)), slot

        accepted: list[tuple[_Tagged, tuple[Voucher, ...]]] = []
        windowed: list[tuple[_Tagged, tuple[Voucher, ...]]] = []
        for item in items:
            exact = tuple(
                v
                for v in item.vouchers
                if v.exact and bound.get((number, _run_key(v.run, v.index))) == item.record.hunter
            )
            if exact:
                accepted.append((item, exact))
            elif window := tuple(v for v in item.vouchers if not v.exact):
                windowed.append((item, window))
        exact_slots = {item.record.hunter for item, _ in accepted}
        window_candidates = [pair for pair in windowed if pair[0].record.hunter not in exact_slots]
        window_slots = sorted({item.record.hunter for item, _ in window_candidates if item.record.hunter}, key=rank)
        window_runs = {_run_key(v.run, v.index) for _, vouchers in window_candidates for v in vouchers}
        kept = set(window_slots[: len(window_runs)])
        accepted.extend(pair for pair in windowed if pair[0].record.hunter in kept)
        for slot in dict.fromkeys(item.record.hunter for item, _ in accepted):
            assert slot is not None
            mine = [(item, vouchers) for item, vouchers in accepted if item.record.hunter == slot]
            work[(number, slot)] = SlotWork(
                round=number,
                slot=slot,
                digests=frozenset(digest for item, _ in mine for digest in item.record.digests),
                first_at=min(item.at for item, _ in mine),
                runs=tuple(
                    sorted(
                        {voucher.run.agent_id for _, vouchers in mine for voucher in vouchers if voucher.run.agent_id}
                    )
                ),
            )
    return work


def bound_slots(
    ledger: Ledger, runs: Sequence[AgentRun], slot_order: SlotOrder
) -> dict[tuple[int, str], str]:
    """The one slot each exactly attributed agent answers for in each round, keyed by (round, agent).

    The agent key is opaque: look a run up with `finding_slots`, not by building the key. See
    `slot_work` for what binding means.
    """
    return _bound_slots(ledger, runs, _tagged_records(ledger, runs), slot_order)


def finding_slots(
    ledger: Ledger,
    finding: Finding,
    runs: Sequence[AgentRun],
    slot_order: SlotOrder,
    *,
    bound: Mapping[tuple[int, str], str] | None = None,
) -> frozenset[str]:
    """The slots bound to the reviewer runs that vouch for the record of `finding` (its `F<n>` token).

    Only an exact voucher (identity or transcript) has a bound slot; a finding that only a time
    window vouches for belongs to no slot. `bound` is the result of `bound_slots`, when the caller
    asks about many findings and has it already.
    """
    table = bound_slots(ledger, runs, slot_order) if bound is None else bound
    vouchers = vouching_runs(
        runs,
        role="reviewer",
        token=finding.id,
        recorded_at=parse_timestamp(finding.recorded_at),
        by=finding.by,
    )
    slots = (table.get((finding.round, _run_key(v.run, v.index))) for v in vouchers if v.exact)
    return frozenset(slot for slot in slots if slot is not None)


def credited_lenses(
    ledger: Ledger,
    upto: int,
    slot_lenses: SlotLenses,
    slot_order: SlotOrder,
    *,
    runs: Sequence[AgentRun] | None = None,
) -> dict[str, frozenset[str]]:
    """Per hunk digest, the lenses that acceptable coverage of rounds up to `upto` has examined it with.

    A slot's coverage credits that slot's lenses (`slot_lenses` returns None for a slot it does not
    know, which credits nothing). Coverage from before the latest round that reopened a digest does
    not count for it, as in `ledger.covered_digests`. Untagged coverage, which is all 5.0 knew,
    credits every lens when its round has no hunt plan and nothing when it has one.
    """
    reopened = reopened_in(ledger, upto)
    credit: dict[str, set[str]] = defaultdict(set)
    seen_runs = agent_runs(ledger) if runs is None else runs
    for (number, slot), work in slot_work(ledger, seen_runs, slot_order).items():
        lenses = slot_lenses(number, slot)
        if number > upto or lenses is None:
            continue
        for digest in work.digests:
            if number >= reopened.get(digest, 1):
                credit[digest].update(lenses)
    for record in ledger.coverage:
        if record.hunter is not None or record.round > upto or ledger.rounds[record.round - 1].plan is not None:
            continue
        for digest in record.digests:
            if record.round >= reopened.get(digest, 1):
                credit[digest].update(LENS_IDS)
    return {digest: frozenset(lenses) for digest, lenses in credit.items()}


# --- verdicts -------------------------------------------------------------------------------


def verdict_vouched(ledger: Ledger, finding: Finding, event: VerdictEvent, runs: Sequence[AgentRun]) -> bool:
    """Whether a finished verifier run vouches for `event`, a verdict (or merge) event of `finding`.

    The finding is looked up in `ledger`, so an event that the ledger does not hold for it is never
    vouched. A merge writes ordinary verdict events, so merges follow the same rule.
    """
    current = next((item for item in ledger.findings if item.id == finding.id), None)
    if current is None or event not in current.verdict_log:
        return False
    return bool(
        vouching_runs(runs, role="verifier", token=event.token, recorded_at=parse_timestamp(event.at), by=event.by)
    )


def distinct_verdict_runs(ledger: Ledger, finding: Finding, runs: Sequence[AgentRun]) -> int:
    """How many distinct verifier runs vouch for verdict events of `finding` (the second-opinion rule).

    One event proves at most one run and one run proves at most one event, so this is the size of a
    maximum matching between the events and the runs that vouch for them. Two overlapping runs that
    both "vouch" for the same single event count once, and an early run is never spent on an event
    that a later one could have taken from another run. A run is its agent id (a resumed verifier is
    one run).
    """
    current = next((item for item in ledger.findings if item.id == finding.id), None)
    if current is None:
        return 0
    candidates = [
        {
            _run_key(v.run, v.index)
            for v in vouching_runs(
                runs, role="verifier", token=event.token, recorded_at=parse_timestamp(event.at), by=event.by
            )
        }
        for event in current.verdict_log
    ]
    owner: dict[str, int] = {}  # run key -> index of the event it is matched to

    def assign(event: int, tried: set[str]) -> bool:
        for key in sorted(candidates[event]):
            if key in tried:
                continue
            tried.add(key)
            if key not in owner or assign(owner[key], tried):
                owner[key] = event
                return True
        return False

    return sum(assign(event, set()) for event in range(len(candidates)))


# --- progress -------------------------------------------------------------------------------


def _verdicts_recorded(finding: Finding) -> set[str]:
    """The distinct verdicts ever recorded for `finding` (the current one alone for a verdict from before events)."""
    if finding.verdict_log:
        return {event.verdict for event in finding.verdict_log}
    return set() if finding.verdict is None else {finding.verdict}


def evidence_counter(ledger: Ledger, runs: Sequence[AgentRun]) -> int:
    """How much vouched work the ledger holds; it only grows as review work is recorded.

    Counts coverage records a reviewer run vouches for, verdict events a verifier run vouches for,
    the distinct verdicts recorded per finding (a verdict from before events existed counts once),
    resolutions, passing non-trivial test and e2e runs and rounds. Agent stop events and slot claims
    are deliberately not counted: a hunter that dies recording nothing is not progress, and counting
    it would let a loop of dying hunters reset the stop guard forever. Neither does repeating a
    verdict: it adds an event but no new (finding, verdict) pair, so a caller stuck re-recording it
    cannot reset the guard. The stop hooks compare the counter before and after a continuation to see
    whether the caller made progress.
    """
    vouched = sum(
        bool(
            vouching_runs(
                runs, role="reviewer", token=f"C{index}", recorded_at=parse_timestamp(record.recorded_at), by=record.by
            )
        )
        for index, record in enumerate(ledger.coverage, 1)
    )
    verdicts = sum(
        bool(
            vouching_runs(
                runs, role="verifier", token=event.token, recorded_at=parse_timestamp(event.at), by=event.by
            )
        )
        for finding in ledger.findings
        for event in finding.verdict_log
    ) + sum(len(_verdicts_recorded(finding)) for finding in ledger.findings)
    resolutions = sum(f.resolution is not None for f in ledger.findings)
    passing = sum(
        run.kind in _PASSING_KINDS and run.counts_as_passing and not is_trivial_run(run.argv)
        for run in ledger.runs
    )
    return vouched + verdicts + resolutions + passing + len(ledger.rounds)
