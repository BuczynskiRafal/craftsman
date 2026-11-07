"""Evidence ledger: persistence and the operations that record review evidence.

The ledger is one JSON file per worktree (`<git-dir>/craftsman/ledger.json`). Every record is
bound to the fingerprint of the diff it refers to, so evidence goes stale when the code changes.
All operations are pure functions that return a new `Ledger`; `mutate` applies one of them under
an exclusive file lock. `add_agent_event` is reserved for platform hooks and must never be exposed
through an agent-facing CLI command: subagent isolation evidence has to come from the platform.
"""

from __future__ import annotations

import errno
import json
import os
import re
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, TypeVar, cast

from craftsman.diffscan import DiffScanError, DiffSnapshot, Hunk, git_dir
from craftsman.files import write_text_atomic
from craftsman.records import (
    GROUPED_EVIDENCE_PREFIX,
    LEDGER_FILENAME,
    MIN_READABLE_SCHEMA,
    SEVERITIES,
    AgentEvent,
    Coverage,
    FieldError,
    Finding,
    HunkRef,
    Ledger,
    LedgerError,
    QualitySignal,
    Resolution,
    Round,
    Run,
    RunKind,
    RunWaiver,
    Severity,
    SlotClaim,
    Verdict,
    VerdictEvent,
    format_timestamp,
    next_id,
    scope_contains,
)
from craftsman.text import one_line
from craftsman.triviality import is_trivial_run

LOCK_POLL_S = 0.05
_WORD_RE = re.compile(r"\w+")

T = TypeVar("T")
TargetKind = Literal["hunks", "files"]


def _now() -> str:
    return format_timestamp(datetime.now(timezone.utc))


# --- persistence ----------------------------------------------------------------------------


def ledger_path_for(cwd: str | Path) -> Path:
    """Path of the per-worktree ledger, next to the craftsman state file."""
    try:
        return git_dir(cwd) / "craftsman" / LEDGER_FILENAME
    except DiffScanError as exc:
        raise LedgerError(f"cannot locate the evidence ledger: {exc}") from exc


_HELD = threading.local()


def _held_locks() -> list[Path]:
    if not hasattr(_HELD, "paths"):
        _HELD.paths = []
    return cast("list[Path]", _HELD.paths)


def holds_lock(path: Path) -> bool:
    """Whether this thread holds the lock of `path` (taken by `locked`)."""
    return path.with_suffix(".lock") in _held_locks()


@contextmanager
def locked_unless_held(path: Path) -> Iterator[None]:
    """Hold the lock of `path`; a caller that holds it already (it cannot be taken twice) keeps it."""
    if holds_lock(path):
        yield
    else:
        with locked(path):
            yield


@contextmanager
def locked(path: Path, *, timeout_s: float = 10.0) -> Iterator[None]:
    """Hold an exclusive OS lock on a lock file next to `path` for the duration of the block.

    The operating system releases the lock when its holder exits, however that happens, so a
    crashed command never leaves a stale lock behind. The lock file itself stays in place.
    """
    lock = path.with_suffix(".lock")
    try:
        lock.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o644)
    except OSError as exc:
        raise LedgerError(f"cannot open {lock}: {exc}") from exc
    try:
        deadline = time.monotonic() + timeout_s
        while not _try_lock(fd):
            if time.monotonic() >= deadline:
                raise LedgerError(
                    f"timed out after {timeout_s:g}s waiting for {lock}; another craftsman command "
                    "holds it."
                )
            time.sleep(LOCK_POLL_S)
        _held_locks().append(lock)
        try:
            yield
        finally:
            _held_locks().remove(lock)
    finally:
        # Also runs when an interrupt arrives mid-acquisition; closing the file releases any lock.
        _unlock(fd)
        os.close(fd)


if sys.platform == "win32":
    import msvcrt

    def _try_lock(fd: int) -> bool:
        os.lseek(fd, 0, os.SEEK_SET)  # msvcrt locks bytes from the current position
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EDEADLK):
                return False  # another process holds the byte
            raise LedgerError(f"cannot lock the ledger: {exc}") from exc
        return True

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass  # not held (interrupted before it was taken); closing the file releases it anyway

else:
    import fcntl

    def _try_lock(fd: int) -> bool:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        except OSError as exc:
            raise LedgerError(f"cannot lock the ledger: {exc}") from exc
        return True

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


def load(path: Path) -> Ledger:
    """Read the ledger at `path`; a missing file is an empty ledger."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return Ledger()
    except (OSError, UnicodeDecodeError) as exc:
        raise LedgerError(f"cannot read {path}: {exc}") from exc
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LedgerError(
            f"{path} is not valid JSON ({exc}); restore it or archive it with `craftsman review reset`"
        ) from exc
    if _unreadable_schema(raw) is not None:
        _set_aside_older_ledger(path)
        return load(path)  # the old ledger is gone: empty, or a new one written meanwhile
    return Ledger.from_json(raw)


def _unreadable_schema(raw: object) -> int | None:
    """The schema of a ledger this craftsman cannot read because it is too old; None otherwise."""
    schema = raw.get("schema") if isinstance(raw, dict) else None
    if isinstance(schema, int) and not isinstance(schema, bool) and schema < MIN_READABLE_SCHEMA:
        return schema
    return None


def _set_aside_older_ledger(path: Path) -> None:
    """Rename a ledger of an older craftsman, which this one cannot read, and go on without it.

    Under the lock and after reading the file again: it may have been replaced by a ledger of this
    craftsman since it was read, and that one must stay where it is.
    """
    with locked_unless_held(path):
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return  # gone, or unreadable: not ours to set aside
        schema = _unreadable_schema(current)
        if schema is None:
            return
        backup = path.with_name(f"{path.stem}.v{schema}.bak{path.suffix}")
        try:
            os.replace(path, backup)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise LedgerError(f"cannot archive the ledger of an older craftsman {path}: {exc}") from exc
    sys.stderr.write(
        f"[craftsman] Archived the review ledger of an older craftsman to {backup.name}; "
        "starting a new review.\n"
    )


def save(path: Path, ledger: Ledger) -> None:
    try:
        write_text_atomic(path, json.dumps(ledger.to_json(), indent=2, ensure_ascii=False) + "\n")
    except OSError as exc:
        raise LedgerError(f"cannot write {path}: {exc}") from exc


def mutate(path: Path, fn: Callable[[Ledger], tuple[Ledger, T]]) -> T:
    """Apply `fn` to the ledger under the lock and persist the result when it changed."""
    with locked(path):
        before = load(path)
        after, result = fn(before)
        if after != before:
            save(path, after)
        return result


# --- operations -----------------------------------------------------------------------------


def _latest_round(ledger: Ledger) -> Round:
    if not ledger.rounds:
        raise LedgerError("no review round yet; run `craftsman review prepare` first")
    return ledger.rounds[-1]


def next_finding_id(ledger: Ledger) -> str:
    return next_id("F", (item.id for item in ledger.findings))


def _find(ledger: Ledger, finding_id: str) -> Finding:
    for finding in ledger.findings:
        if finding.id == finding_id:
            return finding
    known = ", ".join(finding.id for finding in ledger.findings) or "none recorded"
    raise LedgerError(f"unknown finding {finding_id!r}; known findings: {known}", field="id")


def _with_finding(ledger: Ledger, updated: Finding) -> tuple[Ledger, Finding]:
    findings = tuple(updated if item.id == updated.id else item for item in ledger.findings)
    return replace(ledger, findings=findings), updated


def stable_hunk_ids(ledger: Ledger, snap: DiffSnapshot) -> DiffSnapshot:
    """`snap` with hunk ids that keep their meaning from round to round.

    A hunk whose digest an earlier round already numbered keeps that id; a new digest gets the
    next number after every id the ledger ever used. An id named from an old packet therefore
    either still means the same hunk or is unknown, never another hunk.
    """
    known: dict[str, str] = {}
    top = 0
    for round_ in ledger.rounds:
        for ref in round_.hunks:
            known[ref.digest] = ref.id
            top = max(top, int(ref.id[1:]))
    used: set[str] = set()
    hunks: list[Hunk] = []
    for hunk in snap.hunks:
        hunk_id = known.get(hunk.digest)
        if hunk_id is None or hunk_id in used:
            top += 1
            hunk_id = f"H{top}"
        used.add(hunk_id)
        hunks.append(replace(hunk, id=hunk_id))
    return replace(snap, hunks=tuple(hunks))


def _build_hunk_refs(
    hunks: Sequence[Hunk], carried: frozenset[str], reopened: frozenset[str]
) -> tuple[HunkRef, ...]:
    return tuple(
        HunkRef(
            hunk.id,
            hunk.path,
            hunk.digest,
            hunk.new_start,
            hunk.new_len,
            carried=hunk.id in carried,
            reopened=hunk.id in reopened,
        )
        for hunk in hunks
    )


FIXED_SCOPE_ERROR = (
    "the review scope is fixed for this ledger; pass --add-paths <paths> to extend it for files "
    "touched by fixes, or run `craftsman review reset` to start a new review"
)


def _validate_round_continuation(
    ledger: Ledger,
    snap: DiffSnapshot,
    normalized_scope: tuple[str, ...] | None,
    *,
    allow_scope_expansion: bool,
) -> None:
    if snap.base_sha != ledger.rounds[0].base_sha:
        raise LedgerError(
            f"the base commit changed from {ledger.rounds[0].base_sha[:12]} to "
            f"{snap.base_sha[:12]}; run `craftsman review reset` to archive this ledger first"
        )
    prev_scope = ledger.current_scope
    if not scope_contains(normalized_scope, prev_scope):
        raise LedgerError(FIXED_SCOPE_ERROR)
    if normalized_scope != prev_scope and not allow_scope_expansion:
        raise LedgerError(FIXED_SCOPE_ERROR)


def round_unchanged(ledger: Ledger, snap: DiffSnapshot, scope: Sequence[str] | None) -> bool:
    """Whether `open_round` would hand back the latest round as it is (or raise it in place) for `snap`.

    The same test `open_round` applies: the snapshot has the latest round's fingerprint and the scope
    is the one that round was prepared with. A pure predicate: it validates nothing, so a base or
    scope change that `open_round` would refuse reads as "changed" here.
    """
    normalized_scope = None if scope is None else tuple(sorted(scope))
    return bool(ledger.rounds) and (
        ledger.rounds[-1].fingerprint == snap.fingerprint and ledger.rounds[-1].scope == normalized_scope
    )


def _still_carried(
    hunks: Sequence[HunkRef],
    required: Mapping[str, frozenset[str]] | None,
    credited: Mapping[str, frozenset[str]] | None,
) -> tuple[HunkRef, ...]:
    """`hunks` with every carried hunk that lacks a lens of `required` made open again.

    This is the in-place raise of a round to deep: its carried hunks were carried against the lenses
    required at normal depth. A hunk is never carried again by this, only released. Without `required`
    and `credited` nothing changes.
    """
    if required is None or credited is None:
        return tuple(hunks)
    return tuple(
        replace(ref, carried=False)
        if ref.carried and not (ref.id in required and required[ref.id] <= credited.get(ref.digest, frozenset()))
        else ref
        for ref in hunks
    )


def open_round(
    ledger: Ledger,
    snap: DiffSnapshot,
    deep: bool,
    packet_path: str,
    symbols_by_hunk: Mapping[str, Sequence[str]],
    *,
    signals: tuple[QualitySignal, ...] = (),
    scope: Sequence[str] | None = None,
    allow_scope_expansion: bool = False,
    out_of_scope: Sequence[str] = (),
    required: Mapping[str, frozenset[str]] | None = None,
    credited: Mapping[str, frozenset[str]] | None = None,
) -> tuple[Ledger, Round]:
    """Start the next review round for `snap`; re-preparing an unchanged diff is a no-op.

    Asking for `deep` on an unchanged diff raises the latest round in place: its coverage stays.
    Given `required` and `credited` (the deep ones), a carried hunk that lacks a deep lens becomes
    open again, so the raised round owes it; nothing else changes.

    Given both `required` (hunk id -> the lenses the hunk must be examined with) and `credited` (hunk
    digest -> the lenses accepted coverage has examined it with), a hunk is carried over only when
    it is not reopened and everything it requires is credited. Without them a hunk is carried when
    any coverage reviewed its digest, as before. Giving only one of the two is an error.
    """
    if (required is None) != (credited is None):
        raise LedgerError("lens-aware carry-over needs both `required` and `credited`, or neither")
    normalized_scope = None if scope is None else tuple(sorted(scope))
    if ledger.rounds:
        _validate_round_continuation(
            ledger, snap, normalized_scope, allow_scope_expansion=allow_scope_expansion
        )
        latest = ledger.rounds[-1]
        if round_unchanged(ledger, snap, scope):
            if latest.deep or not deep:
                return ledger, latest
            raised = replace(latest, deep=True, hunks=_still_carried(latest.hunks, required, credited))
            return replace(ledger, rounds=(*ledger.rounds[:-1], raised)), raised
    unknown = sorted(set(symbols_by_hunk) - {hunk.id for hunk in snap.hunks})
    if unknown:
        raise LedgerError(f"symbols given for hunks not in the snapshot: {', '.join(unknown)}")
    carried, reopened = _carry_over(ledger, snap.hunks, symbols_by_hunk, required, credited)
    round_ = Round(
        number=len(ledger.rounds) + 1,
        fingerprint=snap.fingerprint,
        base_sha=snap.base_sha,
        deep=deep,
        prepared_at=_now(),
        hunks=_build_hunk_refs(snap.hunks, carried, reopened),
        packet_path=packet_path,
        signals=signals,
        scope=normalized_scope,
        out_of_scope=tuple(sorted(out_of_scope)),
    )
    return replace(ledger, rounds=(*ledger.rounds, round_)), round_


def _reopened_hunk_ids(
    hunks: Sequence[Hunk],
    previous: set[str],
    reviewed: set[str],
    symbols_by_hunk: Mapping[str, Sequence[str]],
) -> set[str]:
    mentioned_by: dict[str, list[Hunk]] = {}
    for hunk in hunks:
        if hunk.digest in previous:
            for word in _mentioned_words(hunk):
                mentioned_by.setdefault(word, []).append(hunk)
    pending = [
        symbol
        for hunk in hunks
        if hunk.digest not in previous
        for symbol in symbols_by_hunk.get(hunk.id, ())
    ]
    seen: set[str] = set()
    affected: set[str] = set()
    while pending:
        symbol = pending.pop()
        if symbol in seen:
            continue
        seen.add(symbol)
        for hunk in mentioned_by.get(symbol, ()):
            if hunk.id not in affected:
                affected.add(hunk.id)
                pending.extend(symbols_by_hunk.get(hunk.id, ()))
    return {hunk.id for hunk in hunks if hunk.id in affected and hunk.digest in reviewed}


def _carry_over(
    ledger: Ledger,
    hunks: Sequence[Hunk],
    symbols_by_hunk: Mapping[str, Sequence[str]],
    required: Mapping[str, frozenset[str]] | None = None,
    credited: Mapping[str, frozenset[str]] | None = None,
) -> tuple[frozenset[str], frozenset[str]]:
    """Ids of the hunks of the next round that are carried over, and of those that are reopened.

    With `required` and `credited` (the planned path) a hunk is carried only when all it requires is
    credited; a hunk without an entry in `required` is never carried.
    """
    if not ledger.rounds:
        return frozenset(), frozenset()
    reviewed = covered_digests(ledger, len(ledger.rounds))
    previous = {hunk.digest for hunk in ledger.rounds[-1].hunks}
    reopened = _reopened_hunk_ids(hunks, previous, reviewed, symbols_by_hunk)
    if required is not None and credited is not None:
        carried = {
            hunk.id
            for hunk in hunks
            if hunk.id not in reopened
            and hunk.id in required
            and required[hunk.id] <= credited.get(hunk.digest, frozenset())
        }
    else:
        carried = {hunk.id for hunk in hunks if hunk.id not in reopened and hunk.digest in reviewed}
    return frozenset(carried), frozenset(reopened)


def _mentioned_words(hunk: Hunk) -> frozenset[str]:
    """Identifiers on the hunk's added and context lines.

    Deleted lines, `\\ No newline` markers and `~ mode` lines are not mentions.
    """
    return frozenset(
        word for line in hunk.lines if line[:1] in ("+", " ") for word in _WORD_RE.findall(line, 1)
    )


def reopened_mentions(
    round_: Round, hunks: Sequence[Hunk], symbols_by_hunk: Mapping[str, Sequence[str]]
) -> dict[str, tuple[tuple[str, str], ...]]:
    """Why the reopened hunks of `round_` are open again.

    Maps each reopened hunk id to the `(symbol, defining hunk id)` pairs it mentions, where the
    defining hunk is open in `round_`.
    """
    open_ids = set(round_.open_hunk_ids)
    reopened = {hunk.id for hunk in round_.hunks if hunk.reopened}
    defined = [
        (symbol, hunk.id)
        for hunk in hunks
        if hunk.id in open_ids
        for symbol in symbols_by_hunk.get(hunk.id, ())
    ]
    owners: dict[str, list[tuple[int, str]]] = {}
    for order, (symbol, owner) in enumerate(defined):
        owners.setdefault(symbol, []).append((order, owner))
    mentions: dict[str, tuple[tuple[str, str], ...]] = {}
    for hunk in hunks:
        if hunk.id not in reopened:
            continue
        found = sorted(
            (order, symbol, owner)
            for symbol in _mentioned_words(hunk) & owners.keys()
            for order, owner in owners[symbol]
            if owner != hunk.id
        )
        if found:
            mentions[hunk.id] = tuple((symbol, owner) for _, symbol, owner in found)
    return mentions


def plan_coverage(
    round_: Round, targets: Sequence[str], *, target_kind: TargetKind = "hunks"
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The open hunk ids to cover for `targets` (hunk ids, or files), and the carried ones skipped.

    Hunks carried over from an earlier round are already covered, so naming them, alone or inside
    a range or a file, is not an error; an unknown hunk id or file is.
    """
    if not targets:
        raise LedgerError(f"name at least one of the {target_kind} to cover", field=target_kind)
    if target_kind == "files":
        known = {hunk.path for hunk in round_.hunks}
        unknown = [path for path in targets if path not in known]
        if unknown:
            raise LedgerError(
                f"unknown files in round {round_.number}: {', '.join(unknown)}; "
                f"changed files: {', '.join(sorted(known))}",
                field="files",
            )
        selected = [hunk for hunk in round_.hunks if hunk.path in set(targets)]
    else:
        by_id = {hunk.id: hunk for hunk in round_.hunks}
        unknown = [hunk_id for hunk_id in targets if hunk_id not in by_id]
        if unknown:
            raise LedgerError(
                f"unknown hunk ids in round {round_.number}: {', '.join(unknown)}; "
                f"hunks to cover: {', '.join(round_.open_hunk_ids) or 'none'}",
                field="hunks",
            )
        selected = [by_id[hunk_id] for hunk_id in dict.fromkeys(targets)]
    return (
        tuple(hunk.id for hunk in selected if not hunk.carried),
        tuple(hunk.id for hunk in selected if hunk.carried),
    )


def add_coverage(
    ledger: Ledger,
    hunk_ids: Sequence[str],
    note: str,
    *,
    hunter: str | None = None,
    by: str | None = None,
) -> tuple[Ledger, Coverage]:
    """Record that the open hunks `hunk_ids` of the latest round were examined, with a free-text
    note of what was tried. `plan_coverage` turns what a user named into those ids.

    `hunter` names the plan slot the coverage answers and `by` the caller's identity, when known;
    whether the slot exists and owns these hunks is for the caller to check against the plan. Coverage
    tagged with a slot of the round's stored plan may also name the carried hunks that slot lists (a
    sweep or a fresh set looks at the whole round); untagged coverage never does.
    """
    round_ = _latest_round(ledger)
    if not hunk_ids:
        raise LedgerError("name at least one of the hunks to cover", field="hunks")
    slot = round_.plan.slot(hunter) if hunter is not None and round_.plan is not None else None
    by_id = {
        hunk.id: hunk
        for hunk in round_.hunks
        if not hunk.carried or (slot is not None and hunk.id in slot.hunks)
    }
    closed = [hunk_id for hunk_id in dict.fromkeys(hunk_ids) if hunk_id not in by_id]
    if closed:
        raise LedgerError(
            f"not an open hunk of round {round_.number}: {', '.join(closed)}; "
            f"hunks to cover: {', '.join(round_.open_hunk_ids) or 'none'}",
            field="hunks",
        )
    record = Coverage(
        round=round_.number,
        digests=tuple(dict.fromkeys(by_id[hunk_id].digest for hunk_id in hunk_ids)),
        note=one_line(note),
        recorded_at=_now(),
        hunter=hunter,
        by=by,
    )
    return replace(ledger, coverage=(*ledger.coverage, record)), record


def add_finding(
    ledger: Ledger,
    *,
    lens: str,
    path: str,
    line: int,
    severity: Severity,
    title: str,
    scenario: str,
    suggestion: str,
    hunter: str | None = None,
    by: str | None = None,
) -> tuple[Ledger, Finding]:
    """Record a finding against the latest round and the code it reviewed.

    `hunter` is the slot that found it, or `LEAD_HUNTER` for the orchestrator's own; `by` the caller's identity.
    """
    round_ = _latest_round(ledger)
    now = _now()
    finding = Finding(
        id=next_finding_id(ledger),
        round=round_.number,
        lens=lens,
        path=path.strip(),
        line=line,
        severity=severity,
        title=one_line(title),
        scenario=one_line(scenario),
        suggestion=one_line(suggestion),
        fingerprint=round_.fingerprint,
        verdict=None,
        verdict_evidence="",
        verdict_run=None,
        resolution=None,
        resolution_note="",
        recorded_at=now,
        updated_at=now,
        hunter=hunter,
        by=by,
    )
    return replace(ledger, findings=(*ledger.findings, finding)), finding


def relocate_finding(
    ledger: Ledger, finding_id: str, *, path: str | None, line: int, fingerprint: str
) -> tuple[Ledger, Finding]:
    """Correct a finding's line (`path=None` keeps its file) before it has a verdict, on unchanged code."""
    finding = _find(ledger, finding_id)
    if finding.verdict is not None:
        raise LedgerError(
            f"{finding_id} already has a verdict ({finding.verdict}); record a new finding instead"
        )
    if finding.fingerprint != fingerprint:
        raise LedgerError(
            f"the code changed since {finding_id} was recorded, so its location cannot be corrected; "
            "record a new finding against the current code"
        )
    new_path = finding.path if path is None else path.strip()
    updated = replace(finding, path=new_path, line=line, updated_at=_now())
    return _with_finding(ledger, updated)


def merge_findings(
    ledger: Ledger,
    target_id: str,
    duplicate_ids: Sequence[str],
    *,
    title: str | None = None,
    scenario: str | None = None,
    suggestion: str | None = None,
    by: str | None = None,
) -> tuple[Ledger, Finding, tuple[Finding, ...]]:
    """Fold duplicate findings that share a root cause into `target_id` before verification.

    Each duplicate is dismissed with a verdict event of its own, so the merge has a token (`V<k>`)
    that a verifier run can be shown to have printed. Merges do not chain, so a duplicate that already
    has duplicates folded into it takes them along: they are re-pointed at `target_id`, each with a new
    event of its own, and returned after the named duplicates.
    """
    target, duplicates = _mergeable(ledger, target_id, duplicate_ids)
    now = _now()
    updated_target = replace(
        target,
        severity=min((item.severity for item in (target, *duplicates)), key=SEVERITIES.index),
        title=target.title if title is None else one_line(title),
        scenario=_merged_scenario(target, duplicates) if scenario is None else one_line(scenario),
        suggestion=_merged_suggestion(target, duplicates) if suggestion is None else one_line(suggestion),
        updated_at=now,
    )
    evidence = f"{GROUPED_EVIDENCE_PREFIX}{target.id} ({target.path}:{target.line}): same root cause."
    first_token = int(next_verdict_token(ledger)[1:])
    # Duplicates already folded into a duplicate follow it to the new target: merges do not chain.
    moved = tuple(
        item
        for item in ledger.findings
        if item.merged_into in duplicate_ids and item.id not in duplicate_ids
    )
    dismissed = tuple(
        replace(
            dup,
            verdict="dismissed",
            verdict_evidence=evidence,
            updated_at=now,
            verdict_log=(*dup.verdict_log, VerdictEvent(f"V{first_token + offset}", "dismissed", now, by)),
            merged_into=target.id,
        )
        for offset, dup in enumerate(duplicates)
    )
    repointed = tuple(
        replace(
            item,
            verdict_evidence=(
                evidence if item.verdict_evidence.startswith(GROUPED_EVIDENCE_PREFIX) else item.verdict_evidence
            ),
            updated_at=now,
            verdict_log=(
                *item.verdict_log,
                VerdictEvent(f"V{first_token + len(dismissed) + offset}", "dismissed", now, by),
            ),
            merged_into=target.id,
        )
        for offset, item in enumerate(moved)
    )
    by_id = {item.id: item for item in (updated_target, *dismissed, *repointed)}
    findings = tuple(by_id.get(item.id, item) for item in ledger.findings)
    return replace(ledger, findings=findings), updated_target, (*dismissed, *repointed)


def _mergeable(
    ledger: Ledger, target_id: str, duplicate_ids: Sequence[str]
) -> tuple[Finding, tuple[Finding, ...]]:
    if not duplicate_ids:
        raise LedgerError("name at least one duplicate finding to merge")
    if target_id in duplicate_ids:
        raise LedgerError(f"cannot merge {target_id} into itself")
    if len(set(duplicate_ids)) != len(duplicate_ids):
        raise LedgerError(f"duplicate finding ids must not repeat: {', '.join(duplicate_ids)}")
    target = _find(ledger, target_id)
    duplicates = tuple(_find(ledger, dup_id) for dup_id in duplicate_ids)
    if target.merged_into is not None:
        raise LedgerError(
            f"{target_id} was merged into {target.merged_into}; merge into {target.merged_into} instead "
            "(merges do not chain)"
        )
    if target.verdict is not None:
        raise LedgerError(_not_mergeable(target))
    for dup in duplicates:
        # Recording a merge again is allowed so that a verifier can vouch for a merge someone else made.
        if dup.verdict is not None and dup.merged_into != target_id:
            raise LedgerError(_not_mergeable(dup))
    return target, duplicates


def _not_mergeable(item: Finding) -> str:
    return (
        "merge only works on findings without a verdict (before verification); "
        f"{item.id} already has the verdict {item.verdict}"
    )


def _merged_scenario(target: Finding, duplicates: Sequence[Finding]) -> str:
    """The target's scenario plus every duplicate consequence it does not already describe."""
    merged = target.scenario
    for dup in duplicates:
        if dup.scenario not in merged:
            merged += f"; [{dup.id}/{dup.lens}] {dup.scenario}"
    return merged


def _merged_suggestion(target: Finding, duplicates: Sequence[Finding]) -> str:
    return target.suggestion or next((dup.suggestion for dup in duplicates if dup.suggestion), "")


def next_verdict_token(ledger: Ledger) -> str:
    """The token (`V<k>`) of the next verdict event: one past the highest in the whole ledger."""
    return next_id("V", (event.token for finding in ledger.findings for event in finding.verdict_log))


def next_claim_token(ledger: Ledger) -> str:
    """The token (`B<n>`) of the next slot claim."""
    return next_id("B", (claim.token for claim in ledger.claims))


def add_claim(ledger: Ledger, slot_id: str, *, by: str | None = None) -> tuple[Ledger, SlotClaim]:
    """Record that a hunter took `slot_id` of the latest round.

    Claiming a slot again is allowed (a relaunched hunter); whether the slot exists in the round's plan
    is for the caller to check.
    """
    round_ = _latest_round(ledger)
    claim = SlotClaim(
        round=round_.number, slot=slot_id, token=next_claim_token(ledger), at=_now(), by=by
    )
    return replace(ledger, claims=(*ledger.claims, claim)), claim


def set_verdict(
    ledger: Ledger,
    finding_id: str,
    verdict: Verdict,
    evidence: str,
    run_id: str | None,
    *,
    by: str | None = None,
) -> tuple[Ledger, Finding]:
    """Record an independent verdict; the latest verdict wins and a non-defect verdict drops any resolution.

    Every call appends a `VerdictEvent`; its token is `finding.verdict_log[-1].token`. A merged
    duplicate stays merged when it is dismissed; any other verdict un-merges it, because whoever
    judged it found it is not a duplicate.
    """
    flat = one_line(evidence)  # what is stored, so the reservation holds for any spelling of the space
    if flat.startswith(GROUPED_EVIDENCE_PREFIX):
        raise FieldError(
            "verdict_evidence",
            f"must not start with '{GROUPED_EVIDENCE_PREFIX}': that wording is reserved for "
            "`craftsman review merge`",
        )
    finding = _find(ledger, finding_id)
    if run_id is not None:
        run = _find_run(ledger, run_id)
        if is_trivial_run(run.argv):
            raise FieldError(
                "verdict_run",
                f"{run_id} (`{run.command}`) is trivial and proves nothing; record the command that "
                "exercises the code with `craftsman run --kind repro -- ...` and cite that run",
            )
    keeps_resolution = verdict in ("proven", "probable")
    now = _now()
    event = VerdictEvent(next_verdict_token(ledger), verdict, now, by)
    updated = replace(
        finding,
        verdict=verdict,
        verdict_log=(*finding.verdict_log, event),
        verdict_evidence=flat,
        verdict_run=run_id,
        resolution=finding.resolution if keeps_resolution else None,
        resolution_note=finding.resolution_note if keeps_resolution else "",
        updated_at=now,
        # A merged duplicate stays merged while it is dismissed; any other verdict says it is not a duplicate.
        merged_into=finding.merged_into if verdict == "dismissed" else None,
    )
    return _with_finding(ledger, updated)


def verdict_streak(finding: Finding) -> tuple[VerdictEvent, ...]:
    """The trailing events of `finding`'s verdict log that record its current verdict, oldest first.

    Empty without a verdict or without events (a verdict from before events existed).
    """
    streak: list[VerdictEvent] = []
    for event in reversed(finding.verdict_log):
        if event.verdict != finding.verdict:
            break
        streak.append(event)
    return tuple(reversed(streak))


def verdict_since(finding: Finding) -> str | None:
    """When the finding's current verdict was first recorded, or None without a verdict.

    Recording the same verdict again does not move it; a different verdict in between does. A
    finding from before verdict events existed (empty log) falls back to its last update.
    """
    if finding.verdict is None:
        return None
    streak = verdict_streak(finding)
    return streak[0].at if streak else finding.updated_at


def resolve(
    ledger: Ledger, finding_id: str, resolution: Resolution, note: str, *, fingerprint: str
) -> tuple[Ledger, Finding]:
    """Mark a confirmed finding as fixed (the code must have changed since) or accepted."""
    finding = _find(ledger, finding_id)
    if finding.verdict is None:
        raise LedgerError(
            f"{finding_id} has no verdict yet; run `craftsman review verdict {finding_id} ...` first"
        )
    if finding.verdict == "dismissed":
        raise LedgerError(f"{finding_id} was dismissed; only proven or probable findings are resolved")
    if finding.verdict == "question":
        raise LedgerError(
            f"{finding_id} is a design question (not a defect); only proven or probable findings are resolved"
        )
    if resolution == "fixed" and fingerprint == finding.fingerprint:
        raise LedgerError(
            f"the code has not changed since {finding_id} was recorded; fix the code first, then resolve"
        )
    updated = replace(finding, resolution=resolution, resolution_note=one_line(note), updated_at=_now())
    return _with_finding(ledger, updated)


def _find_run(ledger: Ledger, run_id: str) -> Run:
    for run in ledger.runs:
        if run.id == run_id:
            return run
    known = ", ".join(run.id for run in ledger.runs) or "none recorded"
    raise LedgerError(f"unknown run {run_id!r}; recorded runs: {known}", field="verdict_run")


def waive_run(ledger: Ledger, run_id: str, reason: str) -> tuple[Ledger, Run]:
    """Retire an obsolete failing run (a typo, a renamed test) so it no longer blocks the gate."""
    run = _find_run(ledger, run_id)
    if run.exit_code == 0:
        raise LedgerError(f"{run_id} passed (exit 0); only failing runs can be waived")
    if run.waiver is not None:
        raise LedgerError(f"{run_id} is already waived: {run.waiver.reason}")
    updated = replace(run, waiver=RunWaiver(reason=one_line(reason), waived_at=_now()))
    runs = tuple(updated if item.id == run_id else item for item in ledger.runs)
    return replace(ledger, runs=runs), updated


def add_run(
    ledger: Ledger,
    *,
    kind: RunKind,
    argv: Sequence[str],
    cwd: str,
    exit_code: int,
    duration_s: float,
    fingerprint: str,
    started_at: str,
    output_tail: str,
    fingerprint_after: str | None,
) -> tuple[Ledger, Run]:
    """Append a verification run under the next free run id."""
    run = Run(
        id=next_id("R", (item.id for item in ledger.runs)),
        kind=kind,
        argv=tuple(argv),
        cwd=cwd,
        exit_code=exit_code,
        duration_s=duration_s,
        fingerprint=fingerprint,
        started_at=started_at,
        output_tail=output_tail,
        fingerprint_after=fingerprint_after,
    )
    return replace(ledger, runs=(*ledger.runs, run)), run


def add_agent_event(ledger: Ledger, ev: AgentEvent) -> Ledger:
    """Append a subagent lifecycle event. Hook-only: never expose this to agents."""
    return replace(ledger, agents=(*ledger.agents, ev))


# --- coverage accounting -------------------------------------------------------------------


def reopened_in(ledger: Ledger, upto: int) -> dict[str, int]:
    """Per hunk digest, the latest round up to `upto` that reopened it."""
    latest: dict[str, int] = {}
    for round_ in ledger.rounds[:upto]:
        for hunk in round_.hunks:
            if hunk.reopened:
                latest[hunk.digest] = round_.number
    return latest


def covered_digests(ledger: Ledger, upto: int) -> set[str]:
    """The hunk digests covered as of round `upto`, by that round or an earlier one.

    Coverage recorded before the latest round that reopened a digest does not count for it.
    """
    reopened = reopened_in(ledger, upto)
    return {
        digest
        for record in ledger.coverage
        if record.round <= upto
        for digest in record.digests
        if record.round >= reopened.get(digest, 1)
    }


def uncovered_hunks(ledger: Ledger, round_: Round) -> list[str]:
    """Ids of the open hunks of `round_` that no coverage record accounts for yet."""
    covered = covered_digests(ledger, round_.number)
    return [hunk.id for hunk in round_.hunks if not hunk.carried and hunk.digest not in covered]
