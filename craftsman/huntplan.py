"""The hunt plan: what a review round must examine, computed from the change alone.

The amount of review work depends on the diff, never on how hard the model feels like trying. This
module turns a diff into slots (a slot is the hunks one hunter reads and the lenses it applies),
decides which hunks still owe which lenses after earlier rounds, and plans the later waves of slots
that findings call for. It is pure: no I/O, no git, no clock; the CLI and the gate feed it a snapshot,
a ledger and the time, and persist what it returns.

Vocabulary:
- required lenses: what a hunk must be examined with, by its kind, its risk signals and the depth;
- credited lenses: what accepted coverage has already examined it with (`attribution`);
- owed lenses: required minus credited. A hunk with nothing owed is carried into the next round;
- generation 1 slots (`W1-<n>`): the plan proper, built from the owed lenses of the open hunks;
- later generations: a fresh set `W<g>-X<j>` (a second, unanchored look) and variant slots
  `W<g>-F<n>` (hunt the root cause of finding F<n> elsewhere), triggered by proven or probable bug
  findings, at most `MAX_GENERATION` generations deep.

Waves are planned, never derived: `plan_due_waves` stores them in the plan of the latest round at
discrete points (check, report, brief, prepare, the Stop guards), so what a round requires is always
exactly its stored plan (`required_slots`), and no read ever computes a slot.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Container, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Literal

from craftsman.attribution import (
    AgentRun,
    SlotHunks,
    SlotLenses,
    SlotOrder,
    SlotSince,
    SlotWork,
    agent_runs,
    bound_slots,
    credited_lenses,
    finding_slots,
    slot_work,
    window_run_slots,
)
from craftsman.diffscan import (
    DiffSnapshot,
    Hunk,
    detect_deep_hint_signals,
    detect_signals,
    is_docs_hunk,
)
from craftsman.diffscan_paths import (
    is_agent_prompt_path,
    is_config_path,
    is_data_path,
    is_docs_path,
    is_test_path,
)
from craftsman.lenses import DEEP_HINT_SIGNALS, LENSES, get_lens
from craftsman.records import (
    MAX_GENERATION,
    SEVERITIES,
    Finding,
    HunkRef,
    HuntPlan,
    Ledger,
    Round,
    Slot,
    SlotKind,
    Wave,
    format_timestamp,
    parse_timestamp,
    slot_generation,
)

DEEP_MIN_PRODUCTION_LINES = 251
SLICE_MAX_CHANGED_LINES = 400
DELTA_MAX_CHANGED_LINES = 40
MAX_VARIANTS_PER_WAVE = 3
MAX_FRESH_LENSES = 3
MAX_REASON_HUNK_IDS = 6

CORE_LENSES = ("intent", "contracts", "failure", "ripple", "tests")
CONDITIONAL_LENSES = ("state", "trust", "platform", "model")
# Catalog order (`lenses.LENSES`), which is the order in which owed lenses are listed in a slot.
BUG_LENSES = tuple(lens.id for lens in LENSES if lens.id in (*CORE_LENSES, *CONDITIONAL_LENSES))
SIMPLICITY_LENSES = ("craft", "design")
SWEEP = "sweep"
DOCS_LENSES = ("intent", "craft")
AFFINITY_GROUPS = (
    ("intent", "ripple", "tests"),
    ("contracts", "failure"),
    ("state", "platform"),
    ("trust", "model"),
)
FRESH_DEFAULT_LENSES = ("intent", "contracts", "failure")
_SWEEP_HOST_MAX_LENSES = 3  # a core slot with fewer lenses than this takes the sweep along

HunkKind = Literal["docs", "test", "config", "production"]
HunkLike = HunkRef | Hunk


# --- classifying hunks ----------------------------------------------------------------------


def changed_lines(hunk: Hunk) -> int:
    """Added plus deleted lines of the hunk."""
    return sum(line.startswith(("+", "-")) for line in hunk.lines)


def hunk_kind(hunk: Hunk) -> HunkKind:
    """Prose, tests, configuration (data, lockfiles and generated files included) or production code.

    An agent prompt (`SKILL.md`, `AGENTS.md`, ...) is production code: its prose is behavior.
    """
    if is_docs_hunk(hunk):
        return "docs"
    if is_test_path(hunk.path):
        return "test"
    if is_config_path(hunk.path) or is_data_path(hunk.path):
        return "config"
    return "production"


def is_production_hunk(hunk: Hunk) -> bool:
    return hunk_kind(hunk) == "production"


def _docs_path(path: str) -> bool:
    """Whether `path` is prose that is not an agent prompt: how a finding's file is told apart from code."""
    return is_docs_path(path) and not is_agent_prompt_path(path)


# --- depth ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Depth:
    deep: bool
    reasons: tuple[str, ...]


def _format_hunk_ids(ids: Sequence[str]) -> str:
    shown = ", ".join(ids[:MAX_REASON_HUNK_IDS])
    return f"{shown}, ..." if len(ids) > MAX_REASON_HUNK_IDS else shown


def _deep_signals(hunks: Sequence[Hunk]) -> dict[str, tuple[str, ...]]:
    """Deep-hint signals that fired in production hunks (data, lockfiles and generated files never count)."""
    return detect_deep_hint_signals([hunk for hunk in hunks if is_production_hunk(hunk)])


def decide_depth(snap: DiffSnapshot, *, floor: bool, floor_reason: str | None = None) -> Depth:
    """How deep the round must be, and why.

    Deep when the caller floors it (`--deep`, a deep gate, an earlier deep round, a deep request), when
    a security or concurrency signal fires in a production hunk, or when the change as a whole is large:
    more than 250 changed production lines. Depth is a property of the whole snapshot, not of the hunks
    that happen to be open.
    """
    reasons: list[str] = []
    if floor:
        reasons.append(floor_reason or "requested")
    fired = _deep_signals(snap.hunks)
    for signal in DEEP_HINT_SIGNALS:
        if signal in fired:
            reasons.append(f"{signal} signal in {_format_hunk_ids(fired[signal])}")
    lines = sum(changed_lines(hunk) for hunk in snap.hunks if is_production_hunk(hunk))
    if lines >= DEEP_MIN_PRODUCTION_LINES:
        reasons.append(f"{lines} changed production lines > {DEEP_MIN_PRODUCTION_LINES - 1}")
    return Depth(deep=floor or bool(reasons), reasons=tuple(reasons))


# --- required and owed lenses ---------------------------------------------------------------


def signals_by_hunk(hunks: Sequence[Hunk]) -> dict[str, frozenset[str]]:
    """The risk signals that fired in each hunk (an empty set when none did)."""
    fired: dict[str, set[str]] = {hunk.id: set() for hunk in hunks}
    for signal, ids in detect_signals(hunks).items():
        for hunk_id in ids:
            fired[hunk_id].add(signal)
    return {hunk_id: frozenset(signals) for hunk_id, signals in fired.items()}


def required_lenses(hunk: Hunk, *, deep: bool, signals: frozenset[str]) -> frozenset[str]:
    """The lenses `hunk` must be examined with, given its risk `signals`.

    Production code gets the core and simplicity lenses plus every conditional lens whose trigger fired
    in this very hunk (all of them when deep). Tests and configuration get the core and simplicity
    lenses only: a filesystem call in a test is not a filesystem risk. Prose gets `intent` and `craft`.
    """
    kind = hunk_kind(hunk)
    if kind == "docs":
        return frozenset(DOCS_LENSES)
    base = frozenset((*CORE_LENSES, *SIMPLICITY_LENSES))
    if kind != "production":
        return base
    if deep:
        return base | frozenset(CONDITIONAL_LENSES)
    return base | frozenset(
        lens for lens in CONDITIONAL_LENSES if get_lens(lens).triggers & signals
    )


def required_by_hunk(hunks: Sequence[Hunk], *, deep: bool) -> dict[str, frozenset[str]]:
    """`required_lenses` for every hunk, by hunk id."""
    signals = signals_by_hunk(hunks)
    return {
        hunk.id: required_lenses(hunk, deep=deep, signals=signals[hunk.id]) for hunk in hunks
    }


def owed_lenses(
    required: Mapping[str, frozenset[str]],
    hunks: Sequence[HunkLike],
    credited: Mapping[str, frozenset[str]],
) -> dict[str, frozenset[str]]:
    """Per hunk id, the lenses still owed: `required` minus what `credited` holds for the hunk's digest.

    Only hunks that owe something appear. A `HunkRef` of a prepared round is judged by its state: a
    carried hunk owes nothing, and a reopened one gets no credit, because coverage from before the
    reopening does not count.
    """
    owed: dict[str, frozenset[str]] = {}
    for hunk in hunks:
        if isinstance(hunk, HunkRef):
            if hunk.carried:
                continue
            have = frozenset() if hunk.reopened else credited.get(hunk.digest, frozenset())
        else:
            have = credited.get(hunk.digest, frozenset())
        missing = required.get(hunk.id, frozenset()) - have
        if missing:
            owed[hunk.id] = missing
    return owed


# --- slicing --------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Unit:
    """A hunk as the slicer sees it."""

    index: int  # position in the snapshot, which orders the hunks of a slot
    id: str
    path: str
    weight: int  # changed lines
    docs: bool


def _units(hunks: Sequence[Hunk]) -> list[_Unit]:
    return [
        _Unit(index, hunk.id, hunk.path, changed_lines(hunk), is_docs_hunk(hunk))
        for index, hunk in enumerate(hunks)
    ]


def _unit_refs(refs: Sequence[HunkRef]) -> list[_Unit]:
    """Units for hunks known only by their references: `new_len` stands in for the changed lines."""
    return [
        _Unit(index, ref.id, ref.path, ref.new_len, _docs_path(ref.path)) for index, ref in enumerate(refs)
    ]


def _slices(units: Sequence[_Unit], *, reverse: bool = False) -> list[list[_Unit]]:
    """Pack hunks into slices of at most `SLICE_MAX_CHANGED_LINES` changed lines, whole files first.

    A file that fits stays in one slice; a file over the budget is split between its hunks; one hunk
    over the budget is a slice of its own; a hunk with no changed lines joins whichever slice it falls
    in. `reverse` packs from the last hunk to the first. Each slice lists its hunks in snapshot order.
    """
    ordered = list(reversed(units)) if reverse else list(units)
    files: dict[str, list[_Unit]] = {}
    for unit in ordered:
        files.setdefault(unit.path, []).append(unit)
    slices: list[list[_Unit]] = []
    current: list[_Unit] = []
    used = 0

    def close() -> None:
        nonlocal current, used
        if current:
            slices.append(current)
        current, used = [], 0

    for members in files.values():
        total = sum(unit.weight for unit in members)
        if total <= SLICE_MAX_CHANGED_LINES:
            if used + total > SLICE_MAX_CHANGED_LINES:
                close()
            current.extend(members)
            used += total
            continue
        for unit in members:
            if unit.weight > SLICE_MAX_CHANGED_LINES:
                close()
                slices.append([unit])
                continue
            if used + unit.weight > SLICE_MAX_CHANGED_LINES:
                close()
            current.append(unit)
            used += unit.weight
    close()
    return [sorted(part, key=lambda unit: unit.index) for part in slices]


def slice_hunks(hunks: Sequence[Hunk], *, reverse: bool = False) -> tuple[tuple[str, ...], ...]:
    """The hunk ids of each slice of `hunks` (see `_slices`)."""
    return tuple(tuple(unit.id for unit in part) for part in _slices(_units(hunks), reverse=reverse))


# --- building the plan ----------------------------------------------------------------------


def _complement(
    hunk_ids: frozenset[str], generation_one: Sequence[Slot], examined: Mapping[str, frozenset[str]]
) -> tuple[str, ...]:
    """The bug lenses nothing has used on `hunk_ids` yet, else the default fresh lenses.

    Used, for every hunk of the set: the lenses of the generation 1 slots of the round that list it, and
    the lenses accepted coverage has credited to it (`examined`, hunk id -> lenses; earlier rounds
    included, so a carried hunk is not looked at with the lenses it already got). The sweep is not a
    bug lens, so it is never in the way.
    """
    used = {lens for slot in generation_one if hunk_ids & set(slot.hunks) for lens in slot.lenses}
    used.update(lens for hunk_id in hunk_ids for lens in examined.get(hunk_id, ()))
    missing = tuple(lens for lens in BUG_LENSES if lens not in used)[:MAX_FRESH_LENSES]
    return missing or FRESH_DEFAULT_LENSES


def _fresh_set(
    units: Sequence[_Unit],
    generation: int,
    trigger: str,
    since: str | None,
    lenses_for: Callable[[int, frozenset[str]], tuple[str, ...]],
    *,
    first: int = 1,
) -> list[Slot]:
    """The fresh slots `W<generation>-X<j>`: the same packing as the plan, walked from the other end.

    With a single slice the partition equals the original one; the value is a hunter without the
    first hunters' anchoring. `lenses_for(j, hunk ids)` names the lenses of slot j; numbering starts at
    `first` (a top-up continues after the slots a set already has).
    """
    slots: list[Slot] = []
    for j, part in enumerate(_slices(units, reverse=True), first):
        ids = tuple(unit.id for unit in part)
        slots.append(
            Slot(
                id=f"W{generation}-X{j}",
                generation=generation,
                kind="fresh",
                hunks=ids,
                lenses=lenses_for(j, frozenset(ids)),
                changed_lines=sum(unit.weight for unit in part),
                trigger=trigger,
                since=since,
            )
        )
    return slots


def _slot_number(slot_id: str) -> int:
    """The trailing number of a slot id (`W1-3` -> 3, `W2-F12` -> 12)."""
    digits = "".join(ch for ch in slot_id.rsplit("-", 1)[1] if ch.isdigit())
    return int(digits)


def build_plan(
    open_hunks: Sequence[Hunk],
    owed: Mapping[str, frozenset[str]],
    *,
    depth: Depth,
    round_number: int,
    first_deep_round: bool,
    existing: HuntPlan | None,
    pending_wave_slots: Sequence[Slot] = (),
    round_hunks: Sequence[Hunk] | None = None,
    carry_sweep: bool = False,
    credited: Mapping[str, frozenset[str]] | None = None,
) -> HuntPlan:
    """The hunt plan of a round, or `existing` extended with what is newly owed (append-only).

    `open_hunks` are the hunks the round has to review, `owed` what each still owes. Existing slots stay
    exactly as they are and come first; a (hunk, lens) pair one of them already assigns is not assigned
    again; new generation 1 slots continue the numbering. Only the generation 1 slots of `existing` count as
    lenses a hunk is assigned: a wave slot has examined nothing until its coverage is credited, which
    `owed` already accounts for. `pending_wave_slots` are wave slots an earlier round left unfinished (see
    `pending_wave_slots`). `round_hunks` are all hunks of the round, carried ones included (default:
    `open_hunks`), and `carry_sweep` demands a sweep over all of them because an earlier round left its
    sweep unfinished. `credited` (hunk digest -> lenses, as `credited(ledger)` sees the round being planned)
    lets the fresh slots, the static set of the first deep round and the carried sets alike, take the lenses
    that neither this round's generation 1 nor the credit has used on their hunks. The static set is topped
    up over the hunks of the scope that no stored or carried generation 2 fresh slot lists.

    Everything `existing` holds is kept, its `Wave` records included: a plan never forgets a wave
    decision, so re-preparing a round never re-plans a finding a wave considered.
    """
    kept = existing.slots if existing is not None else ()
    assigned: dict[str, set[str]] = defaultdict(set)
    for slot in kept:
        if slot.generation == 1:  # a wave slot has not examined anything until its coverage is credited
            for hunk_id in slot.hunks:
                assigned[hunk_id].update(slot.lenses)
    needs = {
        hunk.id: owed.get(hunk.id, frozenset()) - assigned.get(hunk.id, set()) for hunk in open_hunks
    }
    taking = [hunk for hunk in open_hunks if needs[hunk.id]]
    scope = list(open_hunks if round_hunks is None else round_hunks)
    weight = {hunk.id: changed_lines(hunk) for hunk in (*open_hunks, *scope)}
    docs = {hunk.id: is_docs_hunk(hunk) for hunk in taking}

    new, slice_count, delta = _generation_one(taking, needs, docs, weight, kept, round_number)
    new = _with_sweep(new, slice_count, kept, taking, scope, weight, carry_sweep=carry_sweep, delta=delta)

    taken_ids = {slot.id for slot in (*kept, *new)}
    pending = [slot for slot in pending_wave_slots if slot.id not in taken_ids]
    generation_one = [slot for slot in (*kept, *new) if slot.generation == 1]
    examined = {hunk.id: (credited or {}).get(hunk.digest, frozenset()) for hunk in scope}

    def complement(ids: frozenset[str]) -> tuple[str, ...]:
        return _complement(ids, generation_one, examined)

    # A carried fresh set was repartitioned for this round: it takes the lenses this round's generation 1
    # and the credit have not used on its new hunks, never those of the old partition.
    pending = [
        replace(slot, lenses=complement(frozenset(slot.hunks))) if slot.kind == "fresh" else slot
        for slot in pending
    ]
    fresh: list[Slot] = []
    if depth.deep and first_deep_round:
        held = [slot for slot in (*kept, *pending) if slot.kind == "fresh" and slot.generation == 2]
        listed = {hunk_id for slot in held for hunk_id in slot.hunks}
        uncovered = [unit for unit in _units(scope) if not unit.docs and unit.id not in listed]
        if uncovered:  # the static set spans the whole scope, whatever a wave stored before it
            first = max((_slot_number(slot.id) for slot in held), default=0) + 1
            fresh = _fresh_set(uncovered, 2, "deep", None, lambda _, ids: complement(ids), first=first)
    reasons = tuple(dict.fromkeys((*(existing.reasons if existing is not None else ()), *depth.reasons)))
    waves = existing.waves if existing is not None else ()
    return HuntPlan(reasons=reasons, slots=(*kept, *new, *fresh, *pending), waves=waves)


def _generation_one(
    taking: Sequence[Hunk],
    needs: Mapping[str, frozenset[str]],
    docs: Mapping[str, bool],
    weight: Mapping[str, int],
    kept: Sequence[Slot],
    round_number: int,
) -> tuple[list[Slot], int, bool]:
    """The new generation 1 bug and simplicity slots for the hunks in `taking`.

    Also how many slices they came from and whether the round is a delta round (a later round whose
    owed hunks are few and quiet, planned as one bug slot).
    """
    if not taking:
        return [], 0, False
    counter = max((_slot_number(slot.id) for slot in kept if slot.generation == 1), default=0)
    new: list[Slot] = []

    def add(kind: SlotKind, members: Sequence[Hunk], lenses: Sequence[str]) -> None:
        nonlocal counter
        counter += 1
        new.append(
            Slot(
                id=f"W1-{counter}",
                generation=1,
                kind=kind,
                hunks=tuple(hunk.id for hunk in members),
                lenses=tuple(lenses),
                changed_lines=sum(weight[hunk.id] for hunk in members),
            )
        )

    def owing(members: Sequence[Hunk], lenses: Sequence[str]) -> list[Hunk]:
        return [hunk for hunk in members if needs[hunk.id] & set(lenses)]

    def listed(members: Sequence[Hunk], candidates: Sequence[str]) -> list[str]:
        return [lens for lens in candidates if any(lens in needs[hunk.id] for hunk in members)]

    total = sum(weight[hunk.id] for hunk in taking)
    delta = (
        round_number >= 2
        and total <= DELTA_MAX_CHANGED_LINES
        and not _deep_signals(taking)
    )
    if delta:
        parts = [list(taking)]
    else:
        by_id = {hunk.id: hunk for hunk in taking}
        parts = [[by_id[unit.id] for unit in part] for part in _slices(_units(taking))]
    for part in parts:
        if all(docs[hunk.id] for hunk in part):
            lenses = listed(part, DOCS_LENSES)
            add("bug" if "intent" in lenses else "simplicity", part, lenses)
            continue
        if delta:
            groups: Sequence[Sequence[str]] = (BUG_LENSES,)
        else:
            groups = AFFINITY_GROUPS
        for group in groups:
            lenses = listed(part, group)
            if lenses:
                add("bug", owing(part, lenses), lenses)
        lenses = listed(part, SIMPLICITY_LENSES)
        if lenses:
            add("simplicity", owing(part, lenses), lenses)
    return new, len(parts), delta


def _with_sweep(
    new: list[Slot],
    slice_count: int,
    kept: Sequence[Slot],
    taking: Sequence[Hunk],
    scope: Sequence[Hunk],
    weight: Mapping[str, int],
    *,
    carry_sweep: bool,
    delta: bool,
) -> list[Slot]:
    """`new` with the sweep the plan calls for: whole-diff, once, never on conditional or simplicity slots.

    An ordinary sweep (the plan has two bug slots) covers the hunks being taken, may ride on a core slot
    when there is a single slice, and is left out of a delta round, also when a delta round is raised to
    deep in place. A sweep slot of its own is never planned over fewer than two hunks. A carried
    sweep (`carry_sweep`: an earlier round left its sweep unfinished) covers all hunks of the round,
    code before documentation, and is always a slot of its own: a core slot never grows beyond its
    slice to carry it. It is required even in a delta round, so that a tiny edit does not erase it.
    """
    everything = (*kept, *new)
    if any(SWEEP in slot.lenses for slot in everything):
        return new
    bug_slots = sum(slot.kind == "bug" for slot in everything)
    if not carry_sweep and not (new and bug_slots >= 2 and not delta):
        return new
    if carry_sweep:
        swept = [hunk for hunk in scope if not is_docs_hunk(hunk)] or list(scope)
    else:
        swept = [hunk for hunk in taking if not is_docs_hunk(hunk)] or list(taking)
    ids = tuple(hunk.id for hunk in swept)
    if not ids:
        return new
    lines = sum(weight[hunk_id] for hunk_id in ids)
    host = next(
        (
            index
            for index, slot in enumerate(new)
            if slot.kind == "bug"
            and len(slot.lenses) < _SWEEP_HOST_MAX_LENSES
            and set(slot.lenses) <= set(CORE_LENSES)
        ),
        None,
    )
    lone = carry_sweep and len(ids) == 1 and host is not None and ids[0] in new[host].hunks
    if host is not None and slice_count == 1 and (not carry_sweep or lone):
        slot = new[host]
        # The host grows over the code being swept, never onto documentation it did not already list.
        members = tuple(hunk.id for hunk in taking if hunk.id in set(slot.hunks) | set(ids))
        new[host] = replace(
            slot,
            lenses=(*slot.lenses, SWEEP),
            hunks=members,
            changed_lines=sum(weight[hunk_id] for hunk_id in members),
        )
        return new
    if len(ids) < 2:
        return new  # a sweep slot of its own looks across hunks: one hunk has nothing to look across
    counter = max((_slot_number(slot.id) for slot in everything if slot.generation == 1), default=0)
    new.append(
        Slot(id=f"W1-{counter + 1}", generation=1, kind="sweep", hunks=ids, lenses=(SWEEP,), changed_lines=lines)
    )
    return new


# --- later generations ----------------------------------------------------------------------
#
# Waves are planned, not derived. `plan_due_waves` runs at discrete points (check, report, brief,
# prepare, the Stop guards), under the ledger lock, and appends the slots a wave calls for to the plan
# of the LATEST round together with a `Wave` record naming the findings it answered. Everything a round
# requires is therefore stored, and `required_slots` is the stored plan and nothing else. The cycle of
# the earlier read-time derivation (a finding's generation comes from run binding, binding depends on
# slot order, slot order depends on which slots exist, which depends on finding generations) cannot
# arise: a record can only name a stored slot, so binding never sees a slot that does not exist yet.


def is_trigger(finding: Finding, hunk_is_docs: bool) -> bool:
    """Whether `finding` calls for a further wave of hunters.

    A proven or probable finding of a bug lens, outside documentation, that is either serious (blocker,
    major) or proven. Craft and design findings, documentation findings and merged duplicates never do.
    """
    return (
        finding.verdict in ("proven", "probable")
        and finding.lens in BUG_LENSES
        and not hunk_is_docs
        and finding.merged_into is None
        and (finding.severity in ("blocker", "major") or finding.verdict == "proven")
    )


class _Stored:
    """The stored plan of every round, looked up by round number and slot id: the one source of slots."""

    def __init__(self, ledger: Ledger) -> None:
        self._slots: dict[int, dict[str, Slot]] = {
            round_.number: {slot.id: slot for slot in round_.plan.slots} if round_.plan is not None else {}
            for round_ in ledger.rounds
        }

    def slot(self, number: int, slot_id: str) -> Slot | None:
        return self._slots.get(number, {}).get(slot_id)

    def order(self, number: int) -> Sequence[str]:
        return tuple(self._slots.get(number, {}))

    def hunks(self, number: int, slot_id: str) -> frozenset[str] | None:
        slot = self.slot(number, slot_id)
        return None if slot is None else frozenset(slot.hunks)

    def lenses(self, number: int, slot_id: str) -> tuple[str, ...] | None:
        slot = self.slot(number, slot_id)
        return None if slot is None else slot.lenses

    def since(self, number: int, slot_id: str) -> datetime | None:
        slot = self.slot(number, slot_id)
        return None if slot is None or slot.since is None else parse_timestamp(slot.since)


def window_slots(
    ledger: Ledger, runs: Sequence[AgentRun] | None = None, *, matched_only: bool = False
) -> dict[tuple[int, str], str]:
    """The slot each window run serves, by (round number, `attribution.run_key`): the assignment that `slots_missing` credits by.

    `matched_only` lists just the run each slot was matched to (see `attribution.window_run_slots`).
    """
    stored = _Stored(ledger)
    return window_run_slots(
        ledger,
        agent_runs(ledger) if runs is None else runs,
        stored.order,
        slot_hunks=stored.hunks,
        slot_since=stored.since,
        matched_only=matched_only,
    )


def required_slots(ledger: Ledger, round_: Round) -> tuple[Slot, ...]:
    """The slots `round_` requires: its stored plan, always, and none for a round without a plan.

    Nothing is computed at read time. `ledger` is accepted so that callers keep one signature for
    every question about a round.
    """
    del ledger
    return round_.plan.slots if round_.plan is not None else ()


def _generation_of(finding: Finding, bound: frozenset[str], stored: Container[str]) -> int:
    """The generation of a finding: that of the stored slot its recording run is bound to, else of its tag.

    The binding is what the CLI attributed, so it outranks the tag, which the recorder wrote itself: a
    hunter must not lift its findings out of a wave by tagging them with a later generation. The tag
    counts only when no exact voucher is bound to a stored slot (a lead recording for a hunter), and only
    when it names a slot the finding's round stores. Otherwise the finding is of generation 1.
    """
    chosen = [slot for slot in bound if slot in stored]
    if not chosen and finding.hunter is not None and finding.hunter in stored:
        chosen = [finding.hunter]
    generations = [slot_generation(slot) or 1 for slot in chosen]
    return min(max(generations, default=1), MAX_GENERATION)


def finding_generation(
    ledger: Ledger, round_: Round, finding: Finding, runs: Sequence[AgentRun]
) -> int:
    """The generation of `finding`: that of the stored slot bound to the run that recorded it, else of its tag.

    The binding uses the stored plan order of the finding's round. `lead` and untagged findings with no
    bound recorder are generation 1.
    """
    if finding.round != round_.number:
        raise ValueError(f"{finding.id} belongs to round {finding.round}, not round {round_.number}")
    order = _Stored(ledger).order
    return _generation_of(finding, finding_slots(ledger, finding, runs, order), frozenset(order(round_.number)))


def _finding_number(finding: Finding) -> int:
    return int(finding.id[1:])


def _open_refs(round_: Round) -> list[HunkRef]:
    return [ref for ref in round_.hunks if not ref.carried]


# --- whether a slot is done -----------------------------------------------------------------


def slot_complete(round_: Round, slot: Slot, work: Mapping[tuple[int, str], SlotWork]) -> bool:
    """Whether accepted coverage tagged with `slot` covers every hunk of the slot.

    A slot with a `since` (a wave slot) counts, per hunk, only coverage recorded at or after it: work
    that was done before the trigger answered something else. Earlier coverage never blocks: covering
    the hunks again after the trigger completes the slot.
    """
    done = work.get((round_.number, slot.id))
    if done is None:
        return False
    digests = {ref.digest for ref in round_.hunks if ref.id in slot.hunks}
    if not digests <= done.digests:
        return False
    if slot.since is None:
        return True
    threshold = parse_timestamp(slot.since)
    return all(done.last_at[digest] >= threshold for digest in digests)


def slots_missing(
    ledger: Ledger, round_: Round, *, runs: Sequence[AgentRun] | None = None
) -> dict[str, tuple[str, ...]]:
    """For every slot in the stored plan of `round_`, the hunks it still lacks (empty: the slot is done).

    The one definition of "done", used by the planner's barriers and by the gate. A generation 1 slot is
    done when every hunk it lists has all its lenses credited (from any slot, earlier coverage of the same
    code included: the plan lists a hunk only because the hunk owes one of the lenses). The `sweep` is the
    exception, since it looks across hunks and no hunk requires it: a slot that carries it needs its OWN
    coverage to reach every hunk it lists, and the hunks it has not reached are named. A later generation
    slot is done when its own vouched coverage, recorded at or after its trigger (the moment the round was
    prepared for a slot that names none), covers all its hunks.
    """
    if round_.plan is None:
        return {}
    seen = agent_runs(ledger) if runs is None else runs
    stored = _Stored(ledger)
    work = slot_work(ledger, seen, stored.order, slot_hunks=stored.hunks, slot_since=stored.since)
    credit = credited_upto(ledger, round_.number, runs=seen, enforced=round_.enforced)
    digest_of = {ref.id: ref.digest for ref in round_.hunks}
    return {slot.id: _missing_hunks(round_, slot, credit, work, digest_of) for slot in round_.plan.slots}


def _missing_hunks(
    round_: Round,
    slot: Slot,
    credit: Mapping[str, frozenset[str]],
    work: Mapping[tuple[int, str], SlotWork],
    digest_of: Mapping[str, str],
) -> tuple[str, ...]:
    done = work.get((round_.number, slot.id))
    covered = done.digests if done is not None else frozenset()
    if slot.generation == 1:
        # A slot's hunks are checked with ALL of its lenses, not only those each hunk requires: the stored slot
        # does not record per hunk which lens put the hunk in it. That can only demand redundant work (a mixed
        # slot whose hunk never required one of its lenses), never pass a slot wrongly.
        wanted = frozenset(slot.lenses) - {SWEEP}
        lacking = {hunk_id for hunk_id in slot.hunks if not wanted <= credit.get(digest_of[hunk_id], frozenset())}
        if SWEEP in slot.lenses:
            lacking.update(hunk_id for hunk_id in slot.hunks if digest_of[hunk_id] not in covered)
        return tuple(hunk_id for hunk_id in slot.hunks if hunk_id in lacking)
    since = slot.since or round_.prepared_at
    if slot_complete(round_, replace(slot, since=since), work):
        return ()
    threshold = parse_timestamp(since)
    fresh = {digest for digest, moment in (done.last_at if done is not None else {}).items() if moment >= threshold}
    return tuple(hunk_id for hunk_id in slot.hunks if digest_of[hunk_id] not in fresh) or slot.hunks


# --- planning the waves ---------------------------------------------------------------------


def plan_due_waves(ledger: Ledger, now: datetime) -> Ledger:
    """`ledger` (`now`: a naive time is taken as UTC) with every wave that is due stored in the plan of its latest round (idempotent, append-only).

    Trigger findings of any round (`is_trigger`) that no wave has answered yet call for generation g+1,
    g being the finding's generation (`finding_generation`). The wave of generation g+1 is due when the
    barrier for g passes in the latest round: every stored slot of generation g or lower that holds a bug
    lens is done, and every finding of the latest round of generation g or lower has a verdict. Findings
    of earlier rounds that are judged late pass the barrier of their generation as soon as the latest
    round's own barrier does. A due wave appends one `Wave` record naming ALL the trigger findings of that
    generation it answers (those a cap leaves without a slot included, so none leaks into a later wave)
    and stores its slots, timestamped `now`:
    - variant slots `W<g+1>-F<n>`, one per (file, lens) cluster of those findings that the round does not
      hold a variant for already, best severity first, at most `MAX_VARIANTS_PER_WAVE`, over the open
      non-documentation hunks of the file in the latest round (else all non-documentation hunks of the round);
    - fresh slots `W<g+1>-X<j>` so that the round's fresh set of that generation lists every hunk of the
      wave's scope: the open non-documentation hunks when every finding comes from the latest round and
      some hunk is open, else all non-documentation hunks of the round. The hunks the stored set (if any)
      does not list get new slots that continue its numbering.
    Findings of the last generation spawn nothing and are only marked as decided. A planned slot stays
    required even if its trigger is dismissed later: once planned, it runs. Repeats until nothing is due.
    """
    planned_at = format_timestamp(now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc))
    while True:
        step = _plan_next_wave(ledger, planned_at)
        if step is ledger:
            return ledger
        ledger = step


class _WaveState:
    """What the planner needs to decide whether a wave is due, computed once for one ledger."""

    def __init__(self, ledger: Ledger) -> None:
        assert ledger.rounds and ledger.rounds[-1].plan is not None
        self.ledger = ledger
        self.latest = ledger.rounds[-1]
        self.runs = agent_runs(ledger)
        self._stored = _Stored(ledger)
        self._table = bound_slots(ledger, self.runs, self._stored.order)
        self._ids = {round_.number: frozenset(self._stored.order(round_.number)) for round_ in ledger.rounds}
        answered = {
            finding_id
            for round_ in ledger.rounds
            if round_.plan is not None
            for wave in round_.plan.waves
            for finding_id in wave.considered
        }
        self.unanswered = [
            item for item in ledger.findings if item.id not in answered and is_trigger(item, _docs_path(item.path))
        ]
        self._missing: dict[str, tuple[str, ...]] | None = None
        self._examined: dict[str, frozenset[str]] | None = None

    def examined(self) -> dict[str, frozenset[str]]:
        """Per hunk id of the latest round, the lenses accepted coverage (all rounds so far) has examined it with."""
        if self._examined is None:
            credit = credited_upto(self.ledger, self.latest.number, runs=self.runs, enforced=self.latest.enforced)
            self._examined = {ref.id: credit.get(ref.digest, frozenset()) for ref in self.latest.hunks}
        return self._examined

    def generation(self, item: Finding) -> int:
        bound = finding_slots(self.ledger, item, self.runs, self._stored.order, bound=self._table)
        return _generation_of(item, bound, self._ids[item.round])

    def barrier_passed(self, g: int) -> bool:
        """Whether the latest round finished everything of generation `g` or lower that bears on a wave."""
        if self._missing is None:
            self._missing = slots_missing(self.ledger, self.latest, runs=self.runs)
        assert self.latest.plan is not None
        for slot in self.latest.plan.slots:
            if slot.generation <= g and not set(BUG_LENSES).isdisjoint(slot.lenses) and self._missing[slot.id]:
                return False
        return all(
            item.verdict is not None
            for item in self.ledger.findings
            if item.round == self.latest.number and self.generation(item) <= g
        )


def _wave_state(ledger: Ledger) -> _WaveState | None:
    return _WaveState(ledger) if ledger.rounds and ledger.rounds[-1].plan is not None else None


def unanswered_triggers(ledger: Ledger) -> tuple[Finding, ...]:
    """The trigger findings (any round) that no stored wave has answered yet, in finding order."""
    state = _wave_state(ledger)
    return () if state is None else tuple(state.unanswered)


def barrier_passed(ledger: Ledger, generation: int) -> bool:
    """Whether the barrier for `generation` passes in the latest round (see `plan_due_waves`); false without a plan."""
    state = _wave_state(ledger)
    return state is not None and state.barrier_passed(generation)


def _plan_next_wave(ledger: Ledger, planned_at: str) -> Ledger:
    """`ledger` with the lowest due wave stored, or `ledger` itself when none is due."""
    state = _wave_state(ledger)
    if state is None or not state.unanswered:
        return ledger
    by_generation: dict[int, list[Finding]] = defaultdict(list)
    for item in state.unanswered:
        by_generation[state.generation(item)].append(item)
    for g in sorted(by_generation):
        if state.barrier_passed(g):
            return _store_wave(ledger, state, g, by_generation[g], planned_at)
    return ledger


def _store_wave(ledger: Ledger, state: _WaveState, g: int, triggers: Sequence[Finding], planned_at: str) -> Ledger:
    """`ledger` with the wave that answers `triggers` (findings of generation `g`) stored in the latest round."""
    latest = state.latest
    assert latest.plan is not None
    plan = latest.plan
    considered = tuple(item.id for item in sorted(triggers, key=_finding_number))
    if g >= MAX_GENERATION:
        return _with_wave(ledger, latest, Wave(MAX_GENERATION, planned_at, considered), ())
    target = g + 1
    units = [unit for unit in _unit_refs(latest.hunks) if not unit.docs]
    open_ids = {ref.id for ref in _open_refs(latest)}
    open_units = [unit for unit in units if unit.id in open_ids]
    adding = _fresh_slots(state, plan, target, considered[0], triggers, units, open_units, planned_at)
    adding.extend(_variant_slots(ledger, plan, target, triggers, units, open_units, planned_at, {slot.id for slot in adding}))
    return _with_wave(ledger, latest, Wave(target, planned_at, considered, tuple(slot.id for slot in adding)), adding)


def _fresh_slots(
    state: _WaveState,
    plan: HuntPlan,
    target: int,
    trigger: str,
    triggers: Sequence[Finding],
    units: Sequence[_Unit],
    open_units: Sequence[_Unit],
    planned_at: str,
) -> list[Slot]:
    """The fresh slots `W<target>-X<j>` a wave adds, so that the round's set of this generation spans its scope.

    The scope: the open hunks when every trigger comes from the latest round and some hunk is open, else all
    hunks of the round (documentation never counts). The hunks no stored fresh slot of this generation lists
    yet get slots that continue the numbering: the whole set when the round stores none, otherwise a top-up
    (the static deep set was narrower, an earlier trigger planned the set before these hunks were open, or a
    trigger of an earlier round needs fresh eyes over the whole round). A wave decision stays a decision at
    its moment: a set is not extended later by an in-place deep raise, only by another wave.
    """
    stored = [slot for slot in plan.slots if slot.kind == "fresh" and slot.generation == target]
    from_earlier_round = any(item.round < state.latest.number for item in triggers)
    wanted = open_units if open_units and not from_earlier_round else units
    listed = {hunk_id for slot in stored for hunk_id in slot.hunks}
    scope = [unit for unit in wanted if unit.id not in listed]
    if not scope:
        return []
    generation_one = [slot for slot in plan.slots if slot.generation == 1]
    examined = state.examined()
    return _fresh_set(
        scope,
        target,
        trigger,
        planned_at,
        lambda _, ids: _complement(ids, generation_one, examined),
        first=max((_slot_number(slot.id) for slot in stored), default=0) + 1,
    )


def _variant_slots(
    ledger: Ledger,
    plan: HuntPlan,
    target: int,
    triggers: Sequence[Finding],
    units: Sequence[_Unit],
    open_units: Sequence[_Unit],
    planned_at: str,
    taken: set[str],
) -> list[Slot]:
    """The variant slots `W<target>-F<n>` for `triggers`: one per (file, lens) cluster, best first.

    `MAX_VARIANTS_PER_WAVE` is the budget of a whole generation of a round: the variants of this
    generation the plan stores already (carried ones included) are subtracted. A cluster that cannot get
    a slot (its id is taken, no hunk to read) does not use the budget; the clusters left over are
    considered all the same, since the wave names every trigger.
    """
    by_id = {item.id: item for item in ledger.findings}
    stored = [slot for slot in plan.slots if slot.kind == "variant" and slot.generation == target]
    held = {(by_id[slot.trigger].path, by_id[slot.trigger].lens) for slot in stored if slot.trigger in by_id}
    budget = MAX_VARIANTS_PER_WAVE - len(stored)
    clusters: dict[tuple[str, str], list[Finding]] = defaultdict(list)
    for item in sorted(triggers, key=_finding_number):
        clusters[(item.path, item.lens)].append(item)
    ranked = sorted(
        (cluster for key, cluster in clusters.items() if key not in held),
        key=lambda cluster: (min(SEVERITIES.index(item.severity) for item in cluster), _finding_number(cluster[0])),
    )
    taken = taken | {slot.id for slot in plan.slots}
    variants: list[Slot] = []
    for cluster in ranked:
        if len(variants) >= budget:
            break
        first = cluster[0]
        here = [unit for unit in open_units if unit.path == first.path] or list(units)
        slot_id = f"W{target}-F{_finding_number(first)}"
        if not here or slot_id in taken:
            continue
        variants.append(
            Slot(
                id=slot_id,
                generation=target,
                kind="variant",
                hunks=tuple(unit.id for unit in here),
                lenses=(first.lens,),
                changed_lines=sum(unit.weight for unit in here),
                trigger=first.id,
                since=planned_at,
            )
        )
    return variants


def _with_wave(ledger: Ledger, round_: Round, wave: Wave, adding: Sequence[Slot]) -> Ledger:
    """`ledger` with `wave` recorded in the plan of `round_` and `adding` appended to its slots."""
    assert round_.plan is not None
    plan = HuntPlan(round_.plan.reasons, (*round_.plan.slots, *adding), (*round_.plan.waves, wave))
    rounds = tuple(replace(item, plan=plan) if item.number == round_.number else item for item in ledger.rounds)
    return replace(ledger, rounds=rounds)


# --- waves an earlier round left unfinished -------------------------------------------------


def _latest_since(slots: Sequence[Slot]) -> str | None:
    """The latest `since` among `slots` (a static slot has none); None when none has one."""
    return max((slot.since for slot in slots if slot.since is not None), key=parse_timestamp, default=None)


def _wave_order(slot: Slot) -> tuple[int, int, int]:
    return slot.generation, 0 if slot.kind == "fresh" else 1, _slot_number(slot.id)


@dataclass(frozen=True)
class _Unfinished:
    """The wave slots a round left unfinished: variants one by one, fresh sets whole (by generation)."""

    variants: tuple[Slot, ...]
    fresh_sets: Mapping[int, tuple[Slot, ...]]


def _unfinished_waves(round_: Round, missing: Mapping[str, tuple[str, ...]]) -> _Unfinished:
    """The wave slots of the stored plan of `round_` that are not done.

    A fresh set is judged as a unit: when any slot of a generation's set is unfinished, the whole set is.
    """
    assert round_.plan is not None
    slots = round_.plan.slots
    sets: dict[int, list[Slot]] = defaultdict(list)
    for slot in slots:
        if slot.kind == "fresh":
            sets[slot.generation].append(slot)
    return _Unfinished(
        variants=tuple(slot for slot in slots if slot.kind == "variant" and missing[slot.id]),
        fresh_sets={
            generation: tuple(sorted(members, key=lambda slot: _slot_number(slot.id)))
            for generation, members in sets.items()
            if any(missing[slot.id] for slot in members)
        },
    )


def _rebuild_waves(
    ledger: Ledger,
    unfinished: _Unfinished,
    open_units: Sequence[_Unit],
    round_units: Sequence[_Unit],
) -> list[Slot]:
    """The unfinished wave slots for a round, over non-documentation hunks only.

    A variant is rebuilt over the open hunks of its file, else over all hunks of the round (the rule
    `_store_wave` plans a variant by); it is dropped only when the round has no hunk that is not
    documentation. A fresh set is repartitioned over the hunks of the round; all its slots carry the
    trigger of the first original slot (`deep` for a static one) and the latest `since` among the
    originals, and placeholder lenses that `build_plan` replaces.
    """
    code_round = [unit for unit in round_units if not unit.docs]
    code_open = [unit for unit in open_units if not unit.docs]
    rebuilt: list[Slot] = []
    for slot in unfinished.variants:
        path = next((item.path for item in ledger.findings if item.id == slot.trigger), None)
        here = [unit for unit in code_open if unit.path == path] or code_round
        if here:
            rebuilt.append(
                replace(slot, hunks=tuple(unit.id for unit in here), changed_lines=sum(unit.weight for unit in here))
            )
    for generation, originals in unfinished.fresh_sets.items():
        rebuilt.extend(
            _fresh_set(
                code_round,
                generation,
                originals[0].trigger or "deep",
                _latest_since(originals),
                lambda _, __: FRESH_DEFAULT_LENSES,  # a placeholder: `build_plan` complements the new round's lenses
            )
        )
    return sorted(rebuilt, key=_wave_order)


def pending_wave_slots(
    ledger: Ledger, *, open_hunks: Sequence[Hunk], round_hunks: Sequence[Hunk]
) -> tuple[Slot, ...]:
    """The wave slots the latest round left unfinished, rebuilt for the round that replaces it.

    Only the stored plan of the latest round is judged (waves it carried from earlier rounds are in it).
    A variant that is not done is stored again in the new round, keeping its id, lens, trigger and
    `since`, over the new round's open hunks of its finding's file that are not documentation (all
    non-documentation hunks of the round otherwise); it is dropped only when the round has nothing but
    documentation. A fresh set is judged as a unit: when any of its slots is not done it is
    stored again whole, over all hunks of the new round that are not documentation, repartitioned, so
    its slots are numbered anew. The lenses of such a slot are `FRESH_DEFAULT_LENSES` here: the lenses
    that fit the new round are known only once its generation 1 exists, and `build_plan` chooses them.
    """
    if not ledger.rounds or ledger.rounds[-1].plan is None:
        return ()
    previous = ledger.rounds[-1]
    return tuple(
        _rebuild_waves(
            ledger,
            _unfinished_waves(previous, slots_missing(ledger, previous)),
            _units(open_hunks),
            _units(round_hunks),
        )
    )


# --- adapters for attribution ---------------------------------------------------------------


def slot_lenses(ledger: Ledger) -> SlotLenses:
    """`attribution`'s lookup of a slot's lenses: the stored slots of every round.

    The lenses are the ones the slot lists; the sweep among them is never credited (`credited_upto`).
    """
    return _Stored(ledger).lenses


def slot_order(ledger: Ledger) -> SlotOrder:
    """`attribution`'s plan order of the slot ids of a round: the stored plan."""
    return _Stored(ledger).order


def slot_hunks(ledger: Ledger) -> SlotHunks:
    """`attribution`'s lookup of the hunk ids of a slot: the stored slots of every round."""
    return _Stored(ledger).hunks


def slot_since(ledger: Ledger) -> SlotSince:
    """`attribution`'s lookup of the `since` of a slot: the stored slots of every round."""
    return _Stored(ledger).since


def credited_upto(
    ledger: Ledger, upto: int, *, runs: Sequence[AgentRun] | None = None, enforced: bool = False
) -> dict[str, frozenset[str]]:
    """Per hunk digest, the lenses accepted coverage of rounds up to `upto` has examined it with.

    A slot's coverage credits its lenses except `sweep`, which is never credited: the sweep looks across
    the hunks of its slot and has no carry-over role, so `slots_missing` and `sweep_pending` judge it by the
    slot's own coverage. `enforced`: the credit is wanted for an enforced round, so unvouched coverage of
    any planned round counts for nothing (see `credited_lenses`).
    """
    stored = _Stored(ledger)

    def lenses(number: int, slot_id: str) -> tuple[str, ...] | None:
        slot = stored.slot(number, slot_id)
        return None if slot is None else tuple(lens for lens in slot.lenses if lens != SWEEP)

    return credited_lenses(
        ledger,
        upto,
        lenses,
        stored.order,
        runs=agent_runs(ledger) if runs is None else runs,
        slot_hunks=stored.hunks,
        slot_since=stored.since,
        enforced=enforced,
    )


def credited(ledger: Ledger, *, enforced: bool = False) -> dict[str, frozenset[str]]:
    """Per hunk digest, the lenses accepted coverage of all rounds so far has examined it with."""
    return credited_upto(ledger, len(ledger.rounds), enforced=enforced)


def sweep_pending(ledger: Ledger) -> bool:
    """Whether the latest round that required a sweep left it unfinished.

    The sweep is finished when that round's own sweep-bearing slot is complete (`slot_complete`): coverage
    tagged with it spans every hunk it lists. A later round that required no sweep neither finishes nor
    forgives it, and partial sweeps of several rounds do not add up.
    """
    stored = _Stored(ledger)
    work = slot_work(ledger, agent_runs(ledger), stored.order, slot_hunks=stored.hunks, slot_since=stored.since)
    for round_ in reversed(ledger.rounds):
        sweep = next((slot for slot in round_.plan.slots if SWEEP in slot.lenses), None) if round_.plan else None
        if sweep is not None:
            return not slot_complete(round_, sweep, work)
    return False
