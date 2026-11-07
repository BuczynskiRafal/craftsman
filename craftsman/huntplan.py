"""The hunt plan: what a review round must examine, computed from the change alone.

The amount of review work depends on the diff, never on how hard the model feels like trying. This
module turns a diff into slots (a slot is the hunks one hunter reads and the lenses it applies),
decides which hunks still owe which lenses after earlier rounds, and derives the later waves of slots
that findings call for. It is pure: no I/O, no git, no clock; the CLI and the gate feed it a snapshot
and a ledger and persist what it returns.

Vocabulary:
- required lenses: what a hunk must be examined with, by its kind, its risk signals and the depth;
- credited lenses: what accepted coverage has already examined it with (`attribution`);
- owed lenses: required minus credited. A hunk with nothing owed is carried into the next round;
- generation 1 slots (`W1-<n>`): the plan proper, built from the owed lenses of the open hunks;
- later generations: a fresh set `W<g>-X<j>` (a second, unanchored look) and variant slots
  `W<g>-F<n>` (hunt the root cause of finding F<n> elsewhere), triggered by proven or probable bug
  findings, at most `MAX_GENERATION` generations deep.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Literal

from craftsman.attribution import (
    AgentRun,
    SlotLenses,
    SlotOrder,
    SlotWork,
    agent_runs,
    bound_slots,
    credited_lenses,
    finding_slots,
    slot_work,
)
from craftsman.diffscan import (
    DiffSnapshot,
    Hunk,
    detect_deep_hint_signals,
    detect_signals,
    is_agent_prompt_path,
    is_config_path,
    is_data_path,
    is_docs_hunk,
    is_docs_path,
    is_test_path,
)
from craftsman.ledger import verdict_since
from craftsman.lenses import DEEP_HINT_SIGNALS, LENSES, get_lens
from craftsman.records import (
    LEAD_HUNTER,
    SEVERITIES,
    SLOT_ID_RE,
    Finding,
    HunkRef,
    HuntPlan,
    Ledger,
    Round,
    Slot,
    SlotKind,
    parse_timestamp,
    slot_generation,
)

DEEP_MIN_PRODUCTION_LINES = 251
SLICE_MAX_CHANGED_LINES = 400
DELTA_MAX_CHANGED_LINES = 40
MAX_VARIANTS_PER_WAVE = 3
MAX_GENERATION = 3
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


def _complement(hunk_ids: frozenset[str], generation_one: Sequence[Slot]) -> tuple[str, ...]:
    """The bug lenses that no generation 1 slot over `hunk_ids` used, else the default fresh lenses."""
    used = {lens for slot in generation_one if hunk_ids & set(slot.hunks) for lens in slot.lenses}
    missing = tuple(lens for lens in BUG_LENSES if lens not in used)[:MAX_FRESH_LENSES]
    return missing or FRESH_DEFAULT_LENSES


def _fresh_set(
    units: Sequence[_Unit],
    generation: int,
    trigger: str,
    since: str | None,
    lenses_for: Callable[[int, frozenset[str]], tuple[str, ...]],
) -> list[Slot]:
    """The fresh slots `W<generation>-X<j>`: the same packing as the plan, walked from the other end.

    With a single slice the partition equals the original one; the value is a hunter without the
    first hunters' anchoring. `lenses_for(j, hunk ids)` names the lenses of slot j.
    """
    slots: list[Slot] = []
    for j, part in enumerate(_slices(units, reverse=True), 1):
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
) -> HuntPlan:
    """The hunt plan of a round, or `existing` extended with what is newly owed (append-only).

    `open_hunks` are the hunks the round has to review, `owed` what each still owes. Existing slots stay
    exactly as they are and come first; a (hunk, lens) pair one of them already assigns is not assigned
    again; new generation 1 slots continue the numbering. `pending_wave_slots` are wave slots an earlier
    round left unfinished (see `pending_wave_slots`). `round_hunks` are all hunks of the round, carried
    ones included (default: `open_hunks`), and `carry_sweep` demands a sweep over all of them because an
    earlier round left its sweep unfinished.
    """
    kept = existing.slots if existing is not None else ()
    assigned: dict[str, set[str]] = defaultdict(set)
    for slot in kept:
        for hunk_id in slot.hunks:
            assigned[hunk_id].update(slot.lenses)
    needs = {
        hunk.id: owed.get(hunk.id, frozenset()) - assigned.get(hunk.id, set()) for hunk in open_hunks
    }
    taking = [hunk for hunk in open_hunks if needs[hunk.id]]
    scope = list(open_hunks if round_hunks is None else round_hunks)
    weight = {hunk.id: changed_lines(hunk) for hunk in (*open_hunks, *scope)}
    docs = {hunk.id: is_docs_hunk(hunk) for hunk in taking}

    new, slice_count = _generation_one(taking, needs, docs, weight, kept, round_number)
    new = _with_sweep(new, slice_count, kept, taking, scope, weight, carry_sweep=carry_sweep)

    taken_ids = {slot.id for slot in (*kept, *new)}
    pending = [slot for slot in pending_wave_slots if slot.id not in taken_ids]
    fresh: list[Slot] = []
    holds_fresh = any(slot.id.startswith("W2-X") for slot in (*kept, *pending))
    if depth.deep and first_deep_round and not holds_fresh:
        generation_one = [slot for slot in (*kept, *new) if slot.generation == 1]
        units = [unit for unit in _units(taking) if not unit.docs] or [
            unit for unit in _units(scope) if not unit.docs
        ]
        fresh = _fresh_set(
            units, 2, "deep", None, lambda _, ids: _complement(ids, generation_one)
        )
    reasons = tuple(dict.fromkeys((*(existing.reasons if existing is not None else ()), *depth.reasons)))
    return HuntPlan(reasons=reasons, slots=(*kept, *new, *fresh, *pending))


def _generation_one(
    taking: Sequence[Hunk],
    needs: Mapping[str, frozenset[str]],
    docs: Mapping[str, bool],
    weight: Mapping[str, int],
    kept: Sequence[Slot],
    round_number: int,
) -> tuple[list[Slot], int]:
    """The new generation 1 bug and simplicity slots for the hunks in `taking`, and how many slices they came from."""
    if not taking:
        return [], 0
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
    return new, len(parts)


def _with_sweep(
    new: list[Slot],
    slice_count: int,
    kept: Sequence[Slot],
    taking: Sequence[Hunk],
    scope: Sequence[Hunk],
    weight: Mapping[str, int],
    *,
    carry_sweep: bool,
) -> list[Slot]:
    """`new` with the sweep the plan calls for: whole-diff, once, never on conditional or simplicity slots."""
    everything = (*kept, *new)
    if any(SWEEP in slot.lenses for slot in everything):
        return new
    bug_slots = sum(slot.kind == "bug" for slot in everything)
    if not (new and bug_slots >= 2) and not carry_sweep:
        return new
    if carry_sweep:
        swept = [hunk for hunk in scope if not is_docs_hunk(hunk)] or list(scope)
    else:
        swept = list(taking)
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
    if host is not None and slice_count == 1:
        slot = new[host]
        new[host] = replace(slot, lenses=(*slot.lenses, SWEEP), hunks=ids, changed_lines=lines)
        return new
    counter = max((_slot_number(slot.id) for slot in everything if slot.generation == 1), default=0)
    new.append(
        Slot(id=f"W1-{counter + 1}", generation=1, kind="sweep", hunks=ids, lenses=(SWEEP,), changed_lines=lines)
    )
    return new


# --- later generations ----------------------------------------------------------------------


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


def _stored_order(ledger: Ledger) -> SlotOrder:
    """Slot ids by round from the stored plans alone.

    Binding a run to a slot uses the order only to break ties; the dynamic slots, which depend on
    the binding, sort after the stored ones by id. Using the stored plans keeps the derivation free of
    recursion.
    """

    def order(number: int) -> Sequence[str]:
        plan = ledger.rounds[number - 1].plan if 1 <= number <= len(ledger.rounds) else None
        return () if plan is None else tuple(slot.id for slot in plan.slots)

    return order


def _generation_of(finding: Finding, bound: frozenset[str]) -> int:
    """The generation of a finding: that of the slots its recording run is bound to, else that of its tag.

    The binding is what the CLI attributed, so it outranks the tag, which the recorder wrote itself: a
    hunter must not lift its findings out of a wave by tagging them with a later generation. The tag
    counts only when no exact voucher is bound to a slot (a lead recording for a hunter).
    """
    named: tuple[str, ...] = (
        () if finding.hunter is None or finding.hunter == LEAD_HUNTER else (finding.hunter,)
    )
    generations = [slot_generation(slot) or 1 for slot in (bound or named)]
    return min(max(generations, default=1), MAX_GENERATION)


def finding_generation(
    ledger: Ledger, round_: Round, finding: Finding, runs: Sequence[AgentRun]
) -> int:
    """The generation of `finding`: that of the slot bound to the run that recorded it, else of its tag.

    `lead` and untagged findings with no bound recorder are generation 1.
    """
    if finding.round != round_.number:
        raise ValueError(f"{finding.id} belongs to round {finding.round}, not round {round_.number}")
    bound = finding_slots(ledger, finding, runs, _stored_order(ledger))
    return _generation_of(finding, bound)


def _finding_number(finding: Finding) -> int:
    return int(finding.id[1:])


def _earliest(moments: Sequence[str | None]) -> str | None:
    known = [moment for moment in moments if moment is not None]
    return min(known, key=parse_timestamp) if known else None


def _open_refs(round_: Round) -> list[HunkRef]:
    return [ref for ref in round_.hunks if not ref.carried]


def _own_slots(ledger: Ledger, round_: Round) -> tuple[Slot, ...]:
    """The stored plan of `round_` plus the wave slots the trigger findings of `round_` call for."""
    if round_.plan is None:
        return ()  # a round without a hunt plan keeps the rules of 5.0: no waves
    stored = round_.plan.slots
    findings = [item for item in ledger.findings if item.round == round_.number]
    triggers = [item for item in findings if is_trigger(item, _docs_path(item.path))]
    open_refs = _open_refs(round_)
    if not triggers or not open_refs:
        return stored
    runs = agent_runs(ledger)
    order = _stored_order(ledger)
    table = bound_slots(ledger, runs, order)
    generation = {
        item.id: _generation_of(item, finding_slots(ledger, item, runs, order, bound=table))
        for item in triggers
    }
    slots = list(stored)
    generation_one = [slot for slot in stored if slot.generation == 1]
    for g in range(1, MAX_GENERATION):
        wave = sorted((item for item in triggers if generation[item.id] == g), key=_finding_number)
        if not wave:
            continue
        slots.extend(_fresh_for_wave(wave, g, slots, open_refs, generation_one))
        slots.extend(_variants_for_wave(wave, g, slots, open_refs))
    return tuple(slots)


def required_slots(ledger: Ledger, round_: Round) -> tuple[Slot, ...]:
    """The slots `round_` requires: its stored plan plus the wave slots the hunt calls for.

    A round without a plan requires none. Besides the stored plan there are
    - the wave slots its own trigger findings call for, over its open hunks (changed lines counted from
      the hunks' `new_len`, as the ledger holds no diff lines);
    - for the latest round only, the wave slots that findings of earlier rounds call for and that no
      later plan stores yet: a finding judged after the next round opened still gets its wave;
    - every wave slot a hunter already holds (a claim or tagged coverage in this round), even when
      its finding was re-judged or outranked since: a slot never vanishes under a running hunter.
    Stored slots are never duplicated.
    """
    if round_.plan is None:
        return ()
    slots = list(_own_slots(ledger, round_))
    if round_.number == len(ledger.rounds):
        slots.extend(_inherited_slots(ledger, round_, slots))
    slots.extend(_held_orphans(ledger, round_, slots))
    return tuple(slots)


def _fresh_for_wave(
    wave: Sequence[Finding],
    g: int,
    slots: Sequence[Slot],
    open_refs: Sequence[HunkRef],
    generation_one: Sequence[Slot],
) -> list[Slot]:
    if any(slot.id.startswith(f"W{g + 1}-X") for slot in slots):
        return []
    units = [unit for unit in _unit_refs(open_refs) if not unit.docs]
    return _fresh_set(
        units,
        g + 1,
        wave[0].id,
        _earliest([verdict_since(item) for item in wave]),
        lambda _, ids: _complement(ids, generation_one),
    )


def _variant_slot(
    first: Finding, g: int, since: str | None, units: Sequence[_Unit]
) -> Slot:
    """The variant slot `W<g+1>-F<n>` of finding `first` over the units of its file (else all units)."""
    here = [unit for unit in units if unit.path == first.path] or list(units)
    return Slot(
        id=f"W{g + 1}-F{_finding_number(first)}",
        generation=g + 1,
        kind="variant",
        hunks=tuple(unit.id for unit in here),
        lenses=(first.lens,),
        changed_lines=sum(unit.weight for unit in here),
        trigger=first.id,
        since=since,
    )


def _variants_for_wave(
    wave: Sequence[Finding], g: int, slots: Sequence[Slot], open_refs: Sequence[HunkRef]
) -> list[Slot]:
    clusters: dict[tuple[str, str], list[Finding]] = defaultdict(list)
    for item in wave:
        clusters[(item.path, item.lens)].append(item)
    ranked = sorted(
        clusters.values(),
        key=lambda cluster: (min(SEVERITIES.index(item.severity) for item in cluster), _finding_number(cluster[0])),
    )[:MAX_VARIANTS_PER_WAVE]
    known = {slot.id for slot in slots}
    units = _unit_refs(open_refs)
    variants: list[Slot] = []
    for cluster in ranked:
        first = cluster[0]  # the wave is sorted by finding number
        slot = _variant_slot(first, g, _earliest([verdict_since(item) for item in cluster]), units)
        if slot.id not in known:
            variants.append(slot)
    return variants


def _held_waves(ledger: Ledger, number: int) -> frozenset[str]:
    """The wave slots (generation 2 and 3) a hunter holds in round `number`: claimed, or covered under their id."""
    claimed = (claim.slot for claim in ledger.claims if claim.round == number)
    tagged = (item.hunter for item in ledger.coverage if item.round == number and item.hunter is not None)
    return frozenset(slot for slot in (*claimed, *tagged) if (slot_generation(slot) or 1) >= 2)


def _ever_triggered(findings: Sequence[Finding]) -> tuple[Finding, str] | None:
    """The first finding that was ever judged proven or probable, with the moment it first was."""
    for item in sorted(findings, key=_finding_number):
        moments = [event.at for event in item.verdict_log if event.verdict in ("proven", "probable")]
        if moments:
            return item, min(moments, key=parse_timestamp)
    return None


def _held_orphans(ledger: Ledger, round_: Round, slots: Sequence[Slot]) -> list[Slot]:
    """Held wave slots that the current findings no longer derive, rebuilt so that they stay required.

    A variant is rebuilt from the finding its id names (any bug-lens finding of the ledger), a fresh
    set whole, from the findings of this round; each keeps a `since` of when its finding was first
    judged proven or probable.
    """
    known = {slot.id for slot in slots}
    held = sorted(_held_waves(ledger, round_.number) - known)
    open_refs = _open_refs(round_)
    if not held or not open_refs:
        return []
    units = _unit_refs(open_refs)
    by_id = {item.id: item for item in ledger.findings}
    generation_one = [slot for slot in (round_.plan.slots if round_.plan else ()) if slot.generation == 1]
    orphans: list[Slot] = []
    fresh_generations: set[int] = set()
    for slot_id in held:
        match = SLOT_ID_RE.fullmatch(slot_id)
        if match is None:
            continue
        g = int(match.group(1)) - 1
        if match.group(4) is not None:
            origin = by_id.get(f"F{match.group(4)}")
            if origin is None or origin.lens not in BUG_LENSES:
                continue
            ever = _ever_triggered([origin])
            orphans.append(_variant_slot(origin, g, ever[1] if ever else verdict_since(origin), units))
        elif match.group(3) is not None:
            fresh_generations.add(g + 1)
    for generation in sorted(fresh_generations):
        ever = _ever_triggered([item for item in ledger.findings if item.round == round_.number])
        fresh = _fresh_set(
            [unit for unit in units if not unit.docs],
            generation,
            ever[0].id if ever else "deep",
            ever[1] if ever else None,
            lambda _, ids: _complement(ids, generation_one),
        )
        if any(slot.id in held for slot in fresh):
            orphans.extend(fresh)
    return orphans


def slot_complete(round_: Round, slot: Slot, work: Mapping[tuple[int, str], SlotWork]) -> bool:
    """Whether accepted coverage tagged with `slot` covers every hunk of the slot.

    A slot with a `since` (a wave slot) counts only coverage that began at or after it: work that
    started before the trigger answered something else. This is deliberately strict; a hunter that
    began early is asked to cover again.
    """
    done = work.get((round_.number, slot.id))
    if done is None:
        return False
    digests = {ref.digest for ref in round_.hunks if ref.id in slot.hunks}
    if not digests <= done.digests:
        return False
    return slot.since is None or (
        done.first_at is not None and done.first_at >= parse_timestamp(slot.since)
    )


def _lenses_of(originals: Sequence[Slot]) -> Callable[[int, frozenset[str]], tuple[str, ...]]:
    """The lenses of slot j of a rebuilt fresh set: those of original slot j (the last one if there are fewer)."""

    def lenses_for(j: int, _: frozenset[str]) -> tuple[str, ...]:
        return originals[min(j, len(originals)) - 1].lenses

    return lenses_for


def _wave_order(slot: Slot) -> tuple[int, int, int]:
    return slot.generation, 0 if slot.kind == "fresh" else 1, _slot_number(slot.id)


@dataclass(frozen=True)
class _Unfinished:
    """The wave slots no round finished: variants one by one, fresh sets whole (by generation)."""

    variants: tuple[Slot, ...]
    fresh_sets: Mapping[int, tuple[Slot, ...]]


def _unfinished_waves(
    rounds: Sequence[Round],
    required: Mapping[int, Sequence[Slot]],
    work: Mapping[tuple[int, str], SlotWork],
) -> _Unfinished:
    """The wave slots of `rounds` that are still unfinished where they were last required.

    A variant is judged by the latest round that holds its id. A fresh set is judged as a unit: per
    generation, by the latest round that holds any slot of it, with that round's slots alone (an
    earlier round's partition that a later round replaced no longer counts).
    """
    variants: dict[str, tuple[Round, Slot]] = {}
    fresh: dict[int, tuple[Round, list[Slot]]] = {}
    for round_ in rounds:
        sets: dict[int, list[Slot]] = defaultdict(list)
        for slot in required[round_.number]:
            if slot.kind == "variant":
                variants[slot.id] = (round_, slot)
            elif slot.kind == "fresh":
                sets[slot.generation].append(slot)
        for generation, members in sets.items():
            fresh[generation] = (round_, sorted(members, key=lambda slot: _slot_number(slot.id)))
    return _Unfinished(
        variants=tuple(slot for round_, slot in variants.values() if not slot_complete(round_, slot, work)),
        fresh_sets={
            generation: tuple(members)
            for generation, (round_, members) in fresh.items()
            if not all(slot_complete(round_, slot, work) for slot in members)
        },
    )


def _rebuild_waves(
    ledger: Ledger,
    unfinished: _Unfinished,
    variant_units: Sequence[_Unit],
    fresh_units: Sequence[_Unit],
) -> list[Slot]:
    """The unfinished wave slots for a round: variants over the units of their file, fresh sets repartitioned."""
    rebuilt: list[Slot] = []
    if variant_units:
        for slot in unfinished.variants:
            path = next((item.path for item in ledger.findings if item.id == slot.trigger), None)
            here = [unit for unit in variant_units if unit.path == path] or list(variant_units)
            rebuilt.append(
                replace(slot, hunks=tuple(unit.id for unit in here), changed_lines=sum(unit.weight for unit in here))
            )
    code_units = [unit for unit in fresh_units if not unit.docs]
    for generation, originals in unfinished.fresh_sets.items():
        rebuilt.extend(
            _fresh_set(
                code_units,
                generation,
                originals[0].trigger or "deep",
                originals[0].since,
                _lenses_of(originals),
            )
        )
    return sorted(rebuilt, key=_wave_order)


def _inherited_slots(ledger: Ledger, latest: Round, own: Sequence[Slot]) -> list[Slot]:
    """Wave slots that findings of earlier rounds call for and `latest` does not hold yet.

    A finding judged after the next round was prepared reaches `pending_wave_slots` too late; here it
    is rebuilt for the latest round (variants over its open hunks, fresh sets over all its hunks).
    """
    earlier = ledger.rounds[:-1]
    if latest.number != len(ledger.rounds) or not earlier:
        return []
    owns = {round_.number: _own_slots(ledger, round_) for round_ in earlier}
    work = slot_work(ledger, agent_runs(ledger), lambda number: [slot.id for slot in owns.get(number, ())])
    rebuilt = _rebuild_waves(
        ledger,
        _unfinished_waves(earlier, owns, work),
        _unit_refs(_open_refs(latest)),
        _unit_refs(latest.hunks),
    )
    held_ids = {slot.id for slot in own}
    held_sets = {slot.generation for slot in own if slot.kind == "fresh"}
    return [
        slot
        for slot in rebuilt
        if slot.id not in held_ids and not (slot.kind == "fresh" and slot.generation in held_sets)
    ]


def pending_wave_slots(
    ledger: Ledger, *, open_hunks: Sequence[Hunk], round_hunks: Sequence[Hunk]
) -> tuple[Slot, ...]:
    """The wave slots earlier rounds left unfinished, rebuilt for a round that replaces them.

    A tiny edit and a new `prepare` must not make a pending slot disappear. A variant is judged by the
    latest earlier round that holds its id; when its coverage is incomplete there, it is stored again
    in the new round, keeping its id, lens, trigger and `since`, over the new round's open hunks of its
    finding's file (all open hunks otherwise). A fresh set is judged as a unit by the latest round that
    holds one of its generation; when unfinished it is stored again whole, over all hunks of the new
    round that are not documentation, with the original lenses and repartitioned, so its slots are
    numbered anew. A slot left with no hunks is dropped.
    """
    if not ledger.rounds:
        return ()
    required = {round_.number: required_slots(ledger, round_) for round_ in ledger.rounds}
    work = slot_work(ledger, agent_runs(ledger), lambda number: [slot.id for slot in required.get(number, ())])
    return tuple(
        _rebuild_waves(
            ledger,
            _unfinished_waves(ledger.rounds, required, work),
            _units(open_hunks),
            _units(round_hunks),
        )
    )


# --- adapters for attribution ---------------------------------------------------------------


def slot_lenses(ledger: Ledger) -> SlotLenses:
    """`attribution`'s lookup of a slot's lenses: stored and dynamic slots of every round."""
    cache: dict[int, dict[str, tuple[str, ...]]] = {}

    def lookup(number: int, slot_id: str) -> tuple[str, ...] | None:
        if not 1 <= number <= len(ledger.rounds):
            return None
        if number not in cache:
            cache[number] = {slot.id: slot.lenses for slot in required_slots(ledger, ledger.rounds[number - 1])}
        return cache[number].get(slot_id)

    return lookup


def slot_order(ledger: Ledger) -> SlotOrder:
    """`attribution`'s plan order of the slot ids of a round: the stored plan, then the dynamic slots."""
    cache: dict[int, tuple[str, ...]] = {}

    def order(number: int) -> Sequence[str]:
        if not 1 <= number <= len(ledger.rounds):
            return ()
        if number not in cache:
            cache[number] = tuple(slot.id for slot in required_slots(ledger, ledger.rounds[number - 1]))
        return cache[number]

    return order


def credited(ledger: Ledger) -> dict[str, frozenset[str]]:
    """Per hunk digest, the lenses accepted coverage of all rounds so far has examined it with."""
    return credited_lenses(ledger, len(ledger.rounds), slot_lenses(ledger), slot_order(ledger))


def sweep_pending(ledger: Ledger) -> bool:
    """Whether the latest round that required a sweep left it unfinished.

    The sweep is finished when its lens is credited for the digest of every hunk of the sweep slot, as
    that round numbered them. A later round that required no sweep neither finishes nor forgives it.
    """
    have = credited(ledger)
    for round_ in reversed(ledger.rounds):
        sweep = next((slot for slot in round_.plan.slots if SWEEP in slot.lenses), None) if round_.plan else None
        if sweep is None:
            continue
        digests = {ref.digest for ref in round_.hunks if ref.id in sweep.hunks}
        return not all(SWEEP in have.get(digest, frozenset()) for digest in digests)
    return False
