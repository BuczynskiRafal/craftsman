"""Catalog of review lenses and the policy that selects them for a change.

A lens is one focused review perspective. This module is the single source of truth for lens
ids and selection; the long-form review protocol for each lens lives in the skills.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, get_args

Tier = Literal["quick", "standard", "deep"]

TIERS: tuple[Tier, ...] = get_args(Tier)
MAX_REASON_HUNK_IDS = 6

_ALL_TIERS: frozenset[Tier] = frozenset(TIERS)
_STANDARD_AND_DEEP: frozenset[Tier] = frozenset({"standard", "deep"})
_DEEP_ONLY: frozenset[Tier] = frozenset({"deep"})
_DOCS_ONLY_LENSES = ("intent", "craft")


@dataclass(frozen=True)
class Lens:
    id: str
    title: str
    focus: str
    always_in: frozenset[Tier]
    triggers: frozenset[str]


@dataclass(frozen=True)
class LensPick:
    lens: Lens
    reason: str


LENSES: tuple[Lens, ...] = (
    Lens(
        id="intent",
        title="Intent & completeness",
        focus="Does the change do all it claims, everywhere it should, and nothing else?",
        always_in=_ALL_TIERS,
        triggers=frozenset(),
    ),
    Lens(
        id="contracts",
        title="Inputs, boundaries & contracts",
        focus="Does each changed function handle the full range of inputs it can receive?",
        always_in=_ALL_TIERS,
        triggers=frozenset(),
    ),
    Lens(
        id="failure",
        title="Failure paths & error handling",
        focus="When an operation fails, does the code fail loudly, precisely and cleanly?",
        always_in=_STANDARD_AND_DEEP,
        triggers=frozenset({"errors", "process", "filesystem", "network", "sql"}),
    ),
    Lens(
        id="ripple",
        title="Ripple effects on callers & removed behavior",
        focus="Do the callers, configs and users of what changed or disappeared still work?",
        always_in=_STANDARD_AND_DEEP,
        triggers=frozenset({"deletion", "config"}),
    ),
    Lens(
        id="state",
        title="State, data flow, resources & concurrency",
        focus="Is shared state consistent, are resources released, are repeated runs safe?",
        always_in=_DEEP_ONLY,
        triggers=frozenset({"concurrency", "filesystem", "time"}),
    ),
    Lens(
        id="trust",
        title="Trust boundaries & security",
        focus="Can untrusted input reach a dangerous sink, or a permission check be bypassed?",
        always_in=_DEEP_ONLY,
        triggers=frozenset(
            {"process", "sql", "deserialization", "auth", "network", "regex", "filesystem"}
        ),
    ),
    Lens(
        id="tests",
        title="Test evidence",
        focus="Would the tests fail if the changed behavior broke, and do they hit risky paths?",
        always_in=_STANDARD_AND_DEEP,
        triggers=frozenset({"tests"}),
    ),
    Lens(
        id="platform",
        title="Environment & portability",
        focus="Does the change hold on every supported OS, shell, encoding, locale and time zone?",
        always_in=_DEEP_ONLY,
        triggers=frozenset({"platform", "time", "process", "filesystem"}),
    ),
    Lens(
        id="model",
        title="Data model & invariants",
        focus="Do the types make illegal states unrepresentable and keep invariants on every path?",
        always_in=_DEEP_ONLY,
        triggers=frozenset({"datamodel"}),
    ),
    Lens(
        id="craft",
        title="Clarity, reuse & simplicity",
        focus="Is this the simplest clear code, reusing what exists, at the right abstraction level?",
        always_in=_ALL_TIERS,
        triggers=frozenset(),
    ),
    Lens(
        id="design",
        title="Design & size — the change as a whole",
        focus="Is this the smallest clear design for the task? What could be deleted?",
        always_in=_STANDARD_AND_DEEP,
        triggers=frozenset(),
    ),
    Lens(
        id="sweep",
        title="Final sweep — interactions between hunks",
        focus="Do hunks that are each correct still agree with each other, the docs and the config?",
        always_in=_STANDARD_AND_DEEP,
        triggers=frozenset(),
    ),
)
LENS_IDS: frozenset[str] = frozenset(lens.id for lens in LENSES)

_LENSES_BY_ID = {lens.id: lens for lens in LENSES}


def strictest_tier(tier: Tier, *others: Tier) -> Tier:
    """The most demanding of the given tiers."""
    return max((tier, *others), key=TIERS.index)


def get_lens(lens_id: str) -> Lens:
    try:
        return _LENSES_BY_ID[lens_id]
    except KeyError:
        valid = ", ".join(lens.id for lens in LENSES)
        raise KeyError(f"unknown lens {lens_id!r}; valid lenses: {valid}") from None


def select_lenses(
    tier: Tier, signals: Mapping[str, Sequence[str]], *, docs_only: bool
) -> tuple[LensPick, ...]:
    """Pick the lenses for a change, in catalog order, each with the reason it was selected."""
    if tier not in TIERS:
        raise ValueError(f"unknown tier {tier!r}; valid tiers: {', '.join(TIERS)}")
    if docs_only:
        return tuple(LensPick(get_lens(lens_id), "docs-only change") for lens_id in _DOCS_ONLY_LENSES)
    picks: list[LensPick] = []
    for lens in LENSES:
        fired = [signal for signal in sorted(lens.triggers) if signals.get(signal)]
        detail = ", ".join(f"{signal} ({_format_hunk_ids(signals[signal])})" for signal in fired)
        reasons = [f"always in {tier}"] if tier in lens.always_in else []
        if fired:
            reasons.append(f"signals: {detail}")
        if reasons:
            picks.append(LensPick(lens, "; ".join(reasons)))
    return tuple(picks)


def _format_hunk_ids(hunk_ids: Sequence[str]) -> str:
    shown = ", ".join(hunk_ids[:MAX_REASON_HUNK_IDS])
    return f"{shown}, …" if len(hunk_ids) > MAX_REASON_HUNK_IDS else shown
