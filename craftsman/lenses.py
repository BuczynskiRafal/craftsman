"""Catalog of review lenses: their ids, focus and the risk signals that trigger them.

A lens is one focused review perspective. This module is the single source of truth for lens ids
and for which risk signals call for which lens (`Lens.triggers`); the long-form playbook for each
lens lives in `skills/craft-review/lenses.md`. Which lenses a hunk of a round actually owes, and
how they are grouped into hunter slots, is decided by `craftsman.huntplan` from this catalog, and
that plan is what the gate holds a review to. `select_lenses` and `deep_hint` are the earlier
suggestion API: nothing in the review commands calls them any more (the tests still do), so they
shape no hunt and no gate decision.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

MAX_REASON_HUNK_IDS = 6
_DOCS_ONLY_LENSES = ("intent", "craft")
DEEP_REASON = "deep round"
MAX_HINT_HUNK_IDS = 3
# Signals that suggest a deep round: the security signals and concurrency.
DEEP_HINT_SIGNALS = ("process", "sql", "deserialization", "auth", "concurrency")


@dataclass(frozen=True)
class Lens:
    id: str
    title: str
    focus: str
    always: str | None  # why the lens is suggested for every non-docs change, if it is
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
        always="core lens",
        triggers=frozenset(),
    ),
    Lens(
        id="contracts",
        title="Inputs, boundaries & contracts",
        focus="Does each changed function handle the full range of inputs it can receive?",
        always="core lens",
        triggers=frozenset(),
    ),
    Lens(
        id="failure",
        title="Failure paths & error handling",
        focus="When an operation fails, does the code fail loudly, precisely and cleanly?",
        always="core lens",
        triggers=frozenset({"errors", "process", "filesystem", "network", "sql"}),
    ),
    Lens(
        id="ripple",
        title="Ripple effects on callers & removed behavior",
        focus="Do the callers, configs and users of what changed or disappeared still work?",
        always="core lens",
        triggers=frozenset({"deletion", "config"}),
    ),
    Lens(
        id="state",
        title="State, data flow, resources & concurrency",
        focus="Is shared state consistent, are resources released, are repeated runs safe?",
        always=None,
        triggers=frozenset({"concurrency", "filesystem", "time"}),
    ),
    Lens(
        id="trust",
        title="Trust boundaries & security",
        focus="Can untrusted input reach a dangerous sink, or a permission check be bypassed?",
        always=None,
        triggers=frozenset(
            {"process", "sql", "deserialization", "auth", "network", "regex", "filesystem"}
        ),
    ),
    Lens(
        id="tests",
        title="Test evidence",
        focus="Would the tests fail if the changed behavior broke, and do they hit risky paths?",
        always="core lens",
        triggers=frozenset(),
    ),
    Lens(
        id="platform",
        title="Environment & portability",
        focus="Does the change hold on every supported OS, shell, encoding, locale and time zone?",
        always=None,
        triggers=frozenset({"platform", "time", "process", "filesystem"}),
    ),
    Lens(
        id="model",
        title="Data model & invariants",
        focus="Do the types make illegal states unrepresentable and keep invariants on every path?",
        always=None,
        triggers=frozenset({"datamodel"}),
    ),
    Lens(
        id="craft",
        title="Clarity, reuse & simplicity",
        focus="Is this the simplest clear code, reusing what exists, at the right abstraction level?",
        always="simplicity lens",
        triggers=frozenset(),
    ),
    Lens(
        id="design",
        title="Design & size — the change as a whole",
        focus="Is this the smallest clear design for the task? What could be deleted?",
        always="simplicity lens",
        triggers=frozenset(),
    ),
    Lens(
        id="sweep",
        title="Final sweep — interactions between hunks",
        focus="Do hunks that are each correct still agree with each other, the docs and the config?",
        always="whole-diff lens",
        triggers=frozenset(),
    ),
)
LENS_IDS: frozenset[str] = frozenset(lens.id for lens in LENSES)

_LENSES_BY_ID = {lens.id: lens for lens in LENSES}


def get_lens(lens_id: str) -> Lens:
    try:
        return _LENSES_BY_ID[lens_id]
    except KeyError:
        valid = ", ".join(lens.id for lens in LENSES)
        raise KeyError(f"unknown lens {lens_id!r}; valid lenses: {valid}") from None


def select_lenses(
    signals: Mapping[str, Sequence[str]], *, deep: bool, docs_only: bool
) -> tuple[LensPick, ...]:
    """Suggest the lenses for a change, in catalog order, each with the reasons behind it.

    Every non-docs change gets the core and simplicity lenses; a lens whose risk signals fired
    is added with the hunks that fired it; a deep round suggests every lens, a docs-only change
    included.
    """
    picks: list[LensPick] = []
    for lens in LENSES:
        if docs_only and not deep:
            reasons = ["docs-only change"] if lens.id in _DOCS_ONLY_LENSES else []
        else:
            reasons = _reasons(lens, signals, deep=deep)
        if reasons:
            picks.append(LensPick(lens, "; ".join(reasons)))
    return tuple(picks)


def _reasons(lens: Lens, signals: Mapping[str, Sequence[str]], *, deep: bool) -> list[str]:
    reasons = [lens.always] if lens.always else []
    fired = [signal for signal in sorted(lens.triggers) if signals.get(signal)]
    if fired:
        detail = ", ".join(f"{signal} ({_format_hunk_ids(signals[signal])})" for signal in fired)
        reasons.append(f"signals: {detail}")
    if deep and not reasons:
        reasons.append(DEEP_REASON)
    return reasons


def _format_hunk_ids(hunk_ids: Sequence[str]) -> str:
    shown = ", ".join(hunk_ids[:MAX_REASON_HUNK_IDS])
    return f"{shown}, …" if len(hunk_ids) > MAX_REASON_HUNK_IDS else shown


def deep_hint(signals: Mapping[str, Sequence[str]]) -> str | None:
    """A hint that the change deserves a deep round, from its security and concurrency signals."""
    fired = [
        f"{signal} {','.join(signals[signal][:MAX_HINT_HUNK_IDS])}"
        for signal in DEEP_HINT_SIGNALS
        if signals.get(signal)
    ]
    return f"Depth: deep suggested (signals: {', '.join(fired)})" if fired else None
