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
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from craftsman.ledger import reopened_in, verdict_streak
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
from craftsman.triviality import counts_as_verification

# Platforms where every honest record names its caller (`by`): a record without `by` is vouched by no run of
# these, not even by the time window, so withholding the identity cannot borrow a run.
IDENTITY_PLATFORMS = ("antigravity",)

# A hunter or verifier that started longer ago than this and never reported is taken for dead.
RUN_STALE_AFTER = timedelta(minutes=30)



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


def pair_unnamed_stop(
    starts: Sequence[datetime],
    stop_at: datetime,
    prepared: datetime | None,
    nudged: AbstractSet[datetime] = frozenset(),
) -> int | None:
    """Which of `starts` a stop without an agent id ends: an index into `starts`, None when it ends none.

    `starts` are the times of the unpaired starts of the stop's role (and name), `prepared` the `prepared_at`
    of the latest round that began no later than the stop (None before any round), `nudged` the starts whose
    run was sent back already (the `answered` of the nudge events). The stop ends the oldest start that began at
    or after `prepared` and is not after the stop: the hunter of the CURRENT round, so a hunter of an earlier
    round that hung without a stop never takes the place of one that began in this round. Among those, a start
    that was not sent back yet comes first: a nudge does not close its run, and parallel hunters of one name
    that stop one after the other must each be sent back once, not the first one twice. Only when there is no
    start of this round, the oldest start that began less than `RUN_STALE_AFTER` before the stop (a run kept
    across the preparation of a round, or a ledger whose rounds say nothing), by the same preference.
    Otherwise the stop is an orphan (a stop of a run the ledger never saw start, e.g. one from before a reset)
    and ends nothing.

    The one place that pairs such a stop: reading the ledger (`agent_runs`) and recording a stop (the
    SubagentStop hook) must agree on it.
    """
    eligible = [i for i, began in enumerate(starts) if began <= stop_at]
    tiers = [[i for i in eligible if starts[i] >= prepared]] if prepared is not None else []
    tiers.append([i for i in eligible if stop_at - starts[i] < RUN_STALE_AFTER])
    for tier in tiers:
        if tier:
            unsent = [i for i in tier if starts[i] not in nudged]
            return min(unsent or tier, key=lambda i: (starts[i], i))
    return None


def agent_runs(ledger: Ledger) -> tuple[AgentRun, ...]:
    """Every craftsman reviewer and verifier run in `ledger.agents`, oldest start first.

    Pairing, per stop:
    - a stop with an agent id pairs with the earliest unpaired start of the same id and the same role,
      never with a start of another id. When every such start is paired already (a subagent that was
      sent back and stopped again), it extends the run of the latest of them instead of making a
      second run: later stop time, records of both (unknown if either is), so one start stays one run;
    - a stop whose id no start carries pairs first-in, first-out with the unpaired starts of its name that
      have no id (Copilot starts by name only), so parallel runs of one name keep their own windows;
    - a stop without an id pairs by `pair_unnamed_stop`: with the oldest unpaired start of its name that
      began since the latest round was prepared, else with one that began less than `RUN_STALE_AFTER` before
      it; a stop that finds none is an orphan and ignored;
    - any other stop that finds no start begins at the `prepared_at` of the round that was current.
    Starts no stop claims become runs that have not stopped. A `nudge` event (a subagent sent back to finish
    its slot) is neither a start nor a stop: it opens and closes nothing, and its `answered` start is
    excluded from the first choice of an unnamed stop. Agents that are not a craftsman role are ignored.
    """
    starts: list[_Start] = []
    run_of_start: dict[int, int] = {}  # start index -> index into `runs`, for the starts a stop has claimed
    runs: list[AgentRun] = []
    nudged: set[datetime] = set()  # starts of runs without an id that were sent back, as far as the events read go
    for event in ledger.agents:
        role = agent_role(event.agent)
        if role is None:
            continue
        at = parse_timestamp(event.at)
        if event.event == "start":
            starts.append(_Start(event, role, at))
            continue
        if event.event == "nudge":  # opens and closes nothing
            if event.answered is not None and not event.agent_id:
                nudged.add(parse_timestamp(event.answered))
            continue
        same_id = [
            i
            for i, item in enumerate(starts)
            if event.agent_id and item.role == role and item.event.agent_id == event.agent_id
        ]
        untaken_of_id = [i for i in same_id if i not in run_of_start]
        name = craftsman_agent_name(event.agent)
        by_name = [
            i
            for i, item in enumerate(starts)
            if i not in run_of_start
            and craftsman_agent_name(item.event.agent) == name
            and (not event.agent_id or not item.event.agent_id)
        ]
        if not event.agent_id:
            before = [r for r in ledger.rounds if parse_timestamp(r.prepared_at) <= at]
            prepared = parse_timestamp(before[-1].prepared_at) if before else None
            pick = pair_unnamed_stop([starts[i].at for i in by_name], at, prepared, nudged)
            if pick is None:
                continue  # an orphan: no start of this round (or just before it) is left for it
            by_name = [by_name[pick]]
        if untaken_of_id or not same_id and by_name:
            chosen = untaken_of_id[0] if untaken_of_id else by_name[0]
            started = min(starts[chosen].at, at)
            agent_id = event.agent_id or starts[chosen].event.agent_id
            run_of_start[chosen] = len(runs)
            runs.append(AgentRun(role, starts[chosen].event.platform, agent_id, started, at, event.records))
        elif same_id:
            index = run_of_start[same_id[-1]]
            earlier = runs[index]
            records = (
                None if earlier.records is None or event.records is None else (*earlier.records, *event.records)
            )
            runs[index] = replace(
                earlier, stopped=at if earlier.stopped is None else max(at, earlier.stopped), records=records
            )
        else:
            current = [r for r in ledger.rounds if parse_timestamp(r.prepared_at) <= at]
            started = parse_timestamp(current[-1].prepared_at) if current else at
            runs.append(AgentRun(role, event.platform, event.agent_id, started, at, event.records))
    runs.extend(
        AgentRun(item.role, item.event.platform, item.event.agent_id, item.at, None, None)
        for index, item in enumerate(starts)
        if index not in run_of_start
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
    attributed record neither gets credit for it nor lets it count for a second agent. A run of an
    identity platform (`IDENTITY_PLATFORMS`) never vouches by window: its honest records name it.
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
            if run.platform not in IDENTITY_PLATFORMS:
                window.append(Voucher(run, False, index))
        elif token in run.records:
            exact.append(Voucher(run, True, index))
    return tuple(exact or window)


def round_vouchers(
    ledger: Ledger,
    runs: Sequence[AgentRun],
    round_number: int,
    *,
    role: AgentRole,
    token: str,
    recorded_at: datetime,
    by: str | None,
) -> tuple[Voucher, ...]:
    """`vouching_runs` for a record of round `round_number`: a run that started before the round was prepared vouches for nothing.

    Such a run belongs to an earlier round (a hunter or verifier still going, or hung, when this one was
    prepared). The cut is applied after the vouchers are chosen, so an exact voucher from before the round
    is not replaced by a window run that happened to be going on. Every path that asks who vouches for a
    record of a round goes through here.
    """
    prepared = parse_timestamp(ledger.rounds[round_number - 1].prepared_at)
    return tuple(
        voucher
        for voucher in vouching_runs(runs, role=role, token=token, recorded_at=recorded_at, by=by)
        if voucher.run.started >= prepared
    )


# --- slot work ------------------------------------------------------------------------------

SlotLenses = Callable[[int, str], tuple[str, ...] | None]  # (round number, slot id) -> lenses, None if unknown
SlotOrder = Callable[[int], Sequence[str]]  # round number -> the slot ids of its plan, in plan order
SlotHunks = Callable[[int, str], frozenset[str] | None]  # (round number, slot id) -> hunk ids, None if unknown
SlotSince = Callable[[int, str], datetime | None]  # (round number, slot id) -> the slot's `since`, None if it has none


@dataclass(frozen=True)
class SlotWork:
    """What was examined under one slot of one round, counting only acceptable records."""

    round: int
    slot: str
    digests: frozenset[str]  # digests covered under this slot by acceptable records
    first_at: datetime | None  # earliest acceptable record time
    last_at: Mapping[str, datetime]  # per covered digest, the latest acceptable record time that covers it
    runs: tuple[str, ...]  # agent ids of the runs whose records were accepted (the run key for a platform without ids)


@dataclass(frozen=True)
class _Tagged:
    token: str
    record: Coverage
    at: datetime
    vouchers: tuple[Voucher, ...]


def _tagged_records(ledger: Ledger, runs: Sequence[AgentRun]) -> dict[int, list[_Tagged]]:
    """The coverage records that name a slot and that a reviewer run vouches for, by round.

    Only a run that started since the round was prepared can vouch: an earlier one is a hunter of an earlier
    round, still going when this one was prepared, and not the hunter of any slot of it.
    """
    by_round: dict[int, list[_Tagged]] = defaultdict(list)
    for index, record in enumerate(ledger.coverage, 1):
        if record.hunter is None:
            continue
        token = f"C{index}"
        at = parse_timestamp(record.recorded_at)
        vouchers = round_vouchers(
            ledger, runs, record.round, role="reviewer", token=token, recorded_at=at, by=record.by
        )
        if vouchers:
            by_round[record.round].append(_Tagged(token, record, at, vouchers))
    return by_round


def run_key(run: AgentRun, index: int) -> str:
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
        for voucher in round_vouchers(
            ledger, runs, claim.round, role="reviewer", token=claim.token, recorded_at=at, by=claim.by
        ):
            slot_key = (claim.round, run_key(voucher.run, voucher.index))
            if voucher.exact and (slot_key not in claimed or at < claimed[slot_key][0]):
                claimed[slot_key] = (at, claim.slot)
    bound = {slot_key: slot for slot_key, (_, slot) in claimed.items()}
    for number, items in tagged.items():
        order = {slot: place for place, slot in enumerate(slot_order(number))}
        first: dict[str, str] = {}
        for item in items:
            assert item.record.hunter is not None
            for voucher in item.vouchers:
                key = run_key(voucher.run, voucher.index)
                best = first.get(key)
                if voucher.exact and (
                    best is None
                    or (order.get(item.record.hunter, len(order)), item.record.hunter)
                    < (order.get(best, len(order)), best)
                ):
                    first[key] = item.record.hunter
        for key, slot in first.items():
            bound.setdefault((number, key), slot)
    return bound


def _round_digests(ledger: Ledger, number: int, hunk_ids: frozenset[str]) -> frozenset[str]:
    return frozenset(ref.digest for ref in ledger.rounds[number - 1].hunks if ref.id in hunk_ids)


def _window_matching(rank_order: Sequence[str], vouched_by: Mapping[str, Mapping[str, AbstractSet[str]]]) -> dict[str, str]:
    """The slots, taken in plan order, that can each be given a run of their own, and that run: a maximum matching.

    `vouched_by` maps a slot to the window runs that vouch for its records (run key -> the digests the run
    vouches for). A slot is kept when it can be matched to a run no other kept slot needs, moving an
    earlier match to another of its runs if that frees one (an augmenting path). Earlier slots win, so a
    run that covers several slots credits the first and leaves the rest to other runs. A slot tries the
    run that vouches for the most of its digests first: honest hunters that overlap in time are all
    vouched for by every window, and the run that holds the most of a slot's work is the likeliest to be
    its hunter, so the others are left for the other slots. The result maps each kept slot to its run key.
    """
    owner: dict[str, str] = {}  # run key -> the slot it is matched to

    def assign(slot: str, tried: set[str]) -> bool:
        runs = vouched_by[slot]
        for key in sorted(runs, key=lambda key: (-len(runs[key]), key)):
            if key in tried:
                continue
            tried.add(key)
            if key not in owner or assign(owner[key], tried):
                owner[key] = slot
                return True
        return False

    for slot in rank_order:
        assign(slot, set())
    return {slot: key for key, slot in owner.items()}


def _window_run_slots(
    matched: Mapping[str, str], rank_order: Sequence[str], vouched_by: Mapping[str, Mapping[str, AbstractSet[str]]]
) -> dict[str, str]:
    """The one slot each window run serves: a run never completes two slots.

    A run matched to a slot (`_window_matching`) serves that slot. Any other run that vouches for records of
    kept slots serves the first of them in plan order, so a slot a dead hunter left half done is still
    finished by the replacement runs that vouch for it, but no run serves two slots.
    """
    served = {key: slot for slot, key in matched.items()}
    for slot in rank_order:
        if slot in matched:
            for key in sorted(vouched_by[slot]):
                served.setdefault(key, slot)
    return served


def _window_candidates(
    windowed: Sequence[tuple[_Tagged, tuple[Voucher, ...]]],
    exact: Sequence[tuple[_Tagged, tuple[Voucher, ...]]],
    exact_covered: Mapping[str, frozenset[str]],
    digests_of: Mapping[str, frozenset[str]],
) -> list[tuple[_Tagged, tuple[Voucher, ...]]]:
    """The window-vouched records that may add to their slot: all for a slot no exact record holds, and for
    a slot an exact hunter holds only those from a run that is disjoint in time from every exact holder
    (it ran before them or after them, never alongside) and that cover a digest `exact_covered` does not
    (the relaunch rule of `slot_work`)."""
    holders: dict[str, list[AgentRun]] = defaultdict(list)
    for item, vouchers in exact:
        assert item.record.hunter is not None
        holders[item.record.hunter].extend(voucher.run for voucher in vouchers)
    candidates: list[tuple[_Tagged, tuple[Voucher, ...]]] = []
    for item, vouchers in windowed:
        slot = item.record.hunter
        assert slot is not None
        if slot not in holders:
            candidates.append((item, vouchers))
            continue
        apart = tuple(v for v in vouchers if all(_apart(v.run, holder) for holder in holders[slot]))
        if apart and digests_of[item.token] - exact_covered.get(slot, frozenset()):
            candidates.append((item, apart))
    return candidates


def _apart(run: AgentRun, other: AgentRun) -> bool:
    """Whether two finished runs did not overlap: one stopped strictly before the other started."""
    assert run.stopped is not None and other.stopped is not None  # vouchers are finished runs
    return other.stopped < run.started or run.stopped < other.started


def slot_work(
    ledger: Ledger,
    runs: Sequence[AgentRun],
    slot_order: SlotOrder,
    *,
    slot_hunks: SlotHunks | None = None,
    slot_since: SlotSince | None = None,
) -> dict[tuple[int, str], SlotWork]:
    """The acceptable work per (round, slot).

    A coverage record tagged with slot S of round r is acceptable when a finished reviewer run
    vouches for it and
    - exactly (identity or transcript), and the agent is bound to S: each agent answers for ONE slot
      per round (see `_bound_slots`), so one subagent cannot do several slots, whether by fetching
      several briefs or by being resumed, and the orchestrator, outside every run, answers for none; or
    - by time window only, from a run that is not bound to a slot in that round. Parallel runs
      overlap, so a window cannot tell their claims apart and skips binding; instead the slots that
      rest on windows alone ("window slots") are matched one-to-one to the window runs that vouch
      for their records, earlier slots in plan order first (see `_window_matching`): a run serves
      ONE slot (`_window_run_slots`), whatever it recorded, and only the records made under that run
      count. Slots with an exact voucher are never capped, and they are credited with their exact records, plus one exception for a relaunch: when the exact hunter
      died with the slot half done, a replacement whose transcript cannot be read must still be able
      to finish it. A window record tagged with such a slot therefore adds the digests no exact record
      of the slot covers, but only when the window run is apart in time from every exact holder of the
      slot (it ran before or after them, in whatever order the relaunches came; a run that overlaps one
      comes from an agent nobody can name and adds nothing). For a slot with a `since` (`slot_since`),
      a record of the slot made before it is dropped up front (it counts for nothing, here and in
      `credited_lenses`), so a relaunch that covers the digest again after the trigger completes the slot.
      These top-ups take part in the one-to-one matching to window runs like any other window slot.

    With `slot_hunks`, a record counts only for the hunks that belong to its slot: its digests are
    cut to those of the slot's hunks, and a record left with none adds nothing. A slot `slot_hunks`
    does not know (None) is not cut.
    """
    return _slot_work(ledger, runs, slot_order, slot_hunks, slot_since)[0]


def window_run_slots(
    ledger: Ledger,
    runs: Sequence[AgentRun],
    slot_order: SlotOrder,
    *,
    slot_hunks: SlotHunks | None = None,
    slot_since: SlotSince | None = None,
    matched_only: bool = False,
) -> dict[tuple[int, str], str]:
    """The slot each window run (a run whose records cannot be read) serves, by (round, `run_key`).

    The same assignment `slot_work` credits by: a run serves one slot, the slot whose accepted window
    records it holds. A run that served no slot is absent. With `matched_only`, only the runs the
    matching gave a slot of their own (`_window_matching`: one run per slot) are listed, not the further
    runs that finish a slot a dead hunter left half done: those are evidence of the slot's work, but not
    the one run that is certainly its hunter's.
    """
    _, serves, matched = _slot_work(ledger, runs, slot_order, slot_hunks, slot_since)
    return matched if matched_only else serves


def _slot_work(
    ledger: Ledger,
    runs: Sequence[AgentRun],
    slot_order: SlotOrder,
    slot_hunks: SlotHunks | None,
    slot_since: SlotSince | None,
) -> tuple[dict[tuple[int, str], SlotWork], dict[tuple[int, str], str], dict[tuple[int, str], str]]:
    tagged = _tagged_records(ledger, runs)
    bound = _bound_slots(ledger, runs, tagged, slot_order)
    work: dict[tuple[int, str], SlotWork] = {}
    window_runs: dict[tuple[int, str], str] = {}  # every window run -> the slot it serves
    matched_runs: dict[tuple[int, str], str] = {}  # the runs the matching gave a slot of their own -> that slot
    for number, all_items in tagged.items():
        order = {slot: place for place, slot in enumerate(slot_order(number))}
        digests_of: dict[str, frozenset[str]] = {}
        items: list[_Tagged] = []
        for item in all_items:
            assert item.record.hunter is not None
            since = None if slot_since is None else slot_since(number, item.record.hunter)
            if since is not None and item.at < since:
                continue  # made before the slot's trigger: it counts for nothing, anywhere
            digests = frozenset(item.record.digests)
            allowed = None if slot_hunks is None else slot_hunks(number, item.record.hunter)
            if allowed is not None:
                digests &= _round_digests(ledger, number, allowed)
            if digests:
                digests_of[item.token] = digests
                items.append(item)
        accepted: list[tuple[_Tagged, tuple[Voucher, ...]]] = []
        windowed: list[tuple[_Tagged, tuple[Voucher, ...]]] = []
        for item in items:
            exact = tuple(
                v
                for v in item.vouchers
                if v.exact and bound.get((number, run_key(v.run, v.index))) == item.record.hunter
            )
            if exact:
                accepted.append((item, exact))
            elif window := tuple(
                v for v in item.vouchers if not v.exact and (number, run_key(v.run, v.index)) not in bound
            ):
                windowed.append((item, window))
        exact_tokens = {item.token for item, _ in accepted}
        exact_covered: dict[str, frozenset[str]] = {}
        for item, _ in accepted:
            assert item.record.hunter is not None
            exact_covered[item.record.hunter] = exact_covered.get(item.record.hunter, frozenset()) | digests_of[item.token]
        window_candidates = _window_candidates(windowed, accepted, exact_covered, digests_of)
        vouched_by: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
        for item, vouchers in window_candidates:
            assert item.record.hunter is not None
            for v in vouchers:
                vouched_by[item.record.hunter][run_key(v.run, v.index)] |= digests_of[item.token]
        rank = sorted(vouched_by, key=lambda slot: (order.get(slot, len(order)), slot))
        matched = _window_matching(rank, vouched_by)
        serves = _window_run_slots(matched, rank, vouched_by)
        for key, serving in serves.items():
            window_runs[(number, key)] = serving
        for matched_slot, key in matched.items():
            matched_runs[(number, key)] = matched_slot
        for item, vouchers in window_candidates:
            own = tuple(v for v in vouchers if serves.get(run_key(v.run, v.index)) == item.record.hunter)
            if own:
                accepted.append((item, own))
        for slot in dict.fromkeys(item.record.hunter for item, _ in accepted):
            assert slot is not None
            mine = [(item, vouchers) for item, vouchers in accepted if item.record.hunter == slot]
            # What a record adds to the slot: all its digests, except that a window top-up for a slot an
            # exact hunter holds adds only digests no exact record covers (it must not refresh the time of
            # a digest the exact hunter covered).
            held = exact_covered.get(slot, frozenset())
            added = {
                item.token: digests_of[item.token] - (held if item.token not in exact_tokens else frozenset())
                for item, _ in mine
            }
            covered = frozenset(digest for digests in added.values() for digest in digests)
            work[(number, slot)] = SlotWork(
                round=number,
                slot=slot,
                digests=covered,
                first_at=min(item.at for item, _ in mine),
                last_at={
                    digest: max(item.at for item, _ in mine if digest in added[item.token])
                    for digest in covered
                },
                runs=tuple(
                    sorted(
                        {
                            voucher.run.agent_id or run_key(voucher.run, voucher.index)
                            for _, vouchers in mine
                            for voucher in vouchers
                        }
                    )
                ),
            )
    return work, window_runs, matched_runs


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
    vouchers = round_vouchers(
        ledger,
        runs,
        finding.round,
        role="reviewer",
        token=finding.id,
        recorded_at=parse_timestamp(finding.recorded_at),
        by=finding.by,
    )
    slots = (table.get((finding.round, run_key(v.run, v.index))) for v in vouchers if v.exact)
    return frozenset(slot for slot in slots if slot is not None)


# What untagged coverage of a round without a hunt plan (or of an advisory one) can credit an enforced round
# with when a reviewer run vouches for it: the normal-depth lenses (`huntplan.CORE_LENSES` and
# `SIMPLICITY_LENSES`, which `tests/test_attribution.py` keeps equal), never a conditional lens or the sweep.
LEGACY_CREDIT_LENSES = frozenset(("intent", "contracts", "failure", "ripple", "tests", "craft", "design"))


def credited_lenses(
    ledger: Ledger,
    upto: int,
    slot_lenses: SlotLenses,
    slot_order: SlotOrder,
    *,
    runs: Sequence[AgentRun] | None = None,
    slot_hunks: SlotHunks | None = None,
    slot_since: SlotSince | None = None,
    enforced: bool = False,
) -> dict[str, frozenset[str]]:
    """Per hunk digest, the lenses that acceptable coverage of rounds up to `upto` has examined it with.

    A slot's coverage credits that slot's lenses (`slot_lenses` returns None for a slot it does not
    know, which credits nothing). Coverage from before the latest round that reopened a digest does
    not count for it, as in `ledger.covered_digests`. Coverage of a round that is not held to a hunt (no plan,
    or advisory), tagged or not, credits by the 5.0 rules: every lens, vouched or not, as nothing there
    asks for a hunter. Judged for an `enforced` round that is too generous: a self-review of an earlier
    advisory or legacy round must not pay for a hunt the enforced round owes. So then
    - untagged coverage credits only when a finished reviewer run vouches for it (by any means), and then
      only `LEGACY_CREDIT_LENSES`;
    - tagged coverage credits through its slot only (`slot_work`).
    Coverage of a round that is itself enforced and planned credits through its slots only, always.
    With `slot_hunks`, a slot credits only the hunks it owns; with `slot_since`, see `slot_work`.
    """
    reopened = reopened_in(ledger, upto)
    credit: dict[str, set[str]] = defaultdict(set)
    seen_runs = agent_runs(ledger) if runs is None else runs
    work = slot_work(ledger, seen_runs, slot_order, slot_hunks=slot_hunks, slot_since=slot_since)
    for (number, slot), done in work.items():
        lenses = slot_lenses(number, slot)
        if number > upto or lenses is None:
            continue
        for digest in done.digests:
            if number >= reopened.get(digest, 1):
                credit[digest].update(lenses)
    for index, record in enumerate(ledger.coverage, 1):
        if record.round > upto:
            continue
        covered = ledger.rounds[record.round - 1]
        if covered.plan is not None and covered.enforced:
            continue  # the hooks hold this round to its hunters: only `slot_work` credits there
        credits: frozenset[str] = LENS_IDS
        if enforced:
            if record.hunter is not None or not round_vouchers(
                ledger,
                seen_runs,
                record.round,
                role="reviewer",
                token=f"C{index}",
                recorded_at=parse_timestamp(record.recorded_at),
                by=record.by,
            ):
                continue
            credits = LEGACY_CREDIT_LENSES
        for digest in record.digests:
            if record.round >= reopened.get(digest, 1):
                credit[digest].update(credits)
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
    return bool(_verifier_keys(ledger, current, event, runs))


def distinct_verdict_runs(ledger: Ledger, finding: Finding, runs: Sequence[AgentRun]) -> int:
    """How many distinct verifier runs vouch for events that record the current verdict of `finding`.

    Only the trailing streak of events with the current verdict counts (`verdict_streak`, the same
    streak `verdict_since` starts from): a verifier who judged otherwise, or an event the current
    verdict has overruled, is not agreement. That is the second-opinion rule.

    One event proves at most one run and one run proves at most one event, so this is the size of a
    maximum matching between the events and the runs that vouch for them. Two overlapping runs that
    both "vouch" for the same single event count once, and an early run is never spent on an event
    that a later one could have taken from another run. A run is its agent id (a resumed verifier is
    one run).
    """
    current = next((item for item in ledger.findings if item.id == finding.id), None)
    if current is None:
        return 0
    return _matching_size([_verifier_keys(ledger, current, event, runs) for event in verdict_streak(current)])


def _matching_size(candidates: Sequence[set[str]]) -> int:
    """Size of a maximum matching between items and the run keys each of them may take (augmenting paths)."""
    owner: dict[str, int] = {}  # run key -> index of the item it is matched to

    def assign(item: int, tried: set[str]) -> bool:
        for key in sorted(candidates[item]):
            if key in tried:
                continue
            tried.add(key)
            if key not in owner or assign(owner[key], tried):
                owner[key] = item
                return True
        return False

    return sum(assign(item, set()) for item in range(len(candidates)))


# --- progress -------------------------------------------------------------------------------


def _verifier_keys(ledger: Ledger, finding: Finding, event: VerdictEvent, runs: Sequence[AgentRun]) -> set[str]:
    """The keys of the verifier runs that vouch for `event`, a verdict event of `finding` (see `round_vouchers`)."""
    return {
        run_key(v.run, v.index)
        for v in round_vouchers(
            ledger,
            runs,
            finding.round,
            role="verifier",
            token=event.token,
            recorded_at=parse_timestamp(event.at),
            by=event.by,
        )
    }


def evidence_counter(ledger: Ledger, runs: Sequence[AgentRun]) -> int:
    """How much vouched work the ledger holds: what the stop hooks compare before and after a continuation.

    Counts
    - coverage records a reviewer run vouches for;
    - distinct (finding, verdict, verifier run) triples over the verdict events a verifier run vouches
      for, so a verifier repeating itself counts once and another verifier agreeing counts too. Per
      (finding, verdict) that is the size of a maximum matching between the events and the runs that
      vouch for them: two overlapping window runs that each judge the same way count twice, one run
      that vouches for two events counts once;
    - a verdict from before events existed, once;
    - resolutions of findings whose latest verdict event is vouched, or that predate events;
    - passing non-trivial test and e2e runs, and rounds.

    Anything the orchestrator records by itself (unvouched verdicts, resolutions on them) counts
    nothing, as do agent stops and slot claims: a hunter that dies recording nothing, or a caller
    cycling verdicts, must not be able to reset the stop guard forever. The value is not monotone: a
    re-verification that overturns a resolved finding drops its resolution and may lower it, which
    is harmless because the hooks only ask whether it grew.
    """
    vouched = sum(
        bool(
            round_vouchers(
                ledger,
                runs,
                record.round,
                role="reviewer",
                token=f"C{index}",
                recorded_at=parse_timestamp(record.recorded_at),
                by=record.by,
            )
        )
        for index, record in enumerate(ledger.coverage, 1)
    )
    judged = 0
    resolutions = 0
    for finding in ledger.findings:
        events = [(event.verdict, _verifier_keys(ledger, finding, event, runs)) for event in finding.verdict_log]
        for verdict in dict.fromkeys(verdict for verdict, _ in events):
            judged += _matching_size([keys for judged_as, keys in events if judged_as == verdict])
        if finding.resolution is None:
            continue
        resolutions += not events or bool(events[-1][1])
    legacy = sum(finding.verdict is not None and not finding.verdict_log for finding in ledger.findings)
    passing = sum(counts_as_verification(run) for run in ledger.runs)
    return vouched + judged + legacy + resolutions + passing + len(ledger.rounds)
