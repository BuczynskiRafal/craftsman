"""Evidence ledger: persistence and the operations that record review evidence.

The ledger is one JSON file per worktree (`<git-dir>/craftsman/ledger.json`). Every record is
bound to the fingerprint of the diff it refers to, so evidence goes stale when the code changes.
All operations are pure functions that return a new `Ledger`; `mutate` applies one of them under
an exclusive file lock. `add_agent_event` is reserved for platform hooks and must never be exposed
through an agent-facing CLI command: subagent isolation evidence has to come from the platform.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import sys
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, TypeVar

from craftsman.diffscan import DiffScanError, DiffSnapshot, Hunk, git_dir
from craftsman.files import write_text_atomic
from craftsman.lenses import LENS_IDS, Tier
from craftsman.records import (
    DESIGN_LENS,
    GENERIC_COVERAGE_WORDS,
    LEDGER_FILENAME,
    MAX_HUNKS_PER_COVERAGE,
    MIN_HUNK_NOTE_SEGMENT_CHARS,
    MIN_SIGNAL_ACK_REASON_CHARS,
    SEVERITIES,
    SWEEP_LENS,
    AgentEvent,
    Coverage,
    Fact,
    FieldError,
    Finding,
    HunkRef,
    Ledger,
    LedgerError,
    LensAddition,
    QualitySignal,
    Resolution,
    Round,
    Run,
    RunKind,
    RunWaiver,
    Severity,
    SignalAck,
    SourceRef,
    Verdict,
    file_digest,
    file_reference_in,
    format_timestamp,
    next_id,
    parse_timestamp,
    scope_contains,
)
from craftsman.triviality import is_trivial_run

LOCK_POLL_S = 0.05
# Of a hunk's coverage-note segment, at least this many characters must be more than praise.
MIN_SUBSTANTIVE_NOTE_CHARS = 20
# One note segment may describe at most this many hunks of a lens before it reads as boilerplate.
MAX_REUSED_NOTE_SEGMENTS = 3
GROUPED_EVIDENCE_PREFIX = "Grouped into "
COVERAGE_NOTE_EXAMPLE = "H3 parse_cfg: x=None -> L42 TypeError; callers cli.py:19 ok"
_WORD_RE = re.compile(r"\w+")
_HUNK_ID_RE = re.compile(r"\bH[1-9]\d*\b")
_CONCRETE_TOKEN_RE = re.compile(r"(?:->|`[^`]+`|\b\d+\b|[A-Za-z_]\w*\s*\(|[A-Za-z_]\w*\.[A-Za-z_]\w*)")
_GENERIC_WORDS_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(word) for word in GENERIC_COVERAGE_WORDS) + r")\b",
    re.IGNORECASE,
)
_CONCRETE_COVERAGE_RE = re.compile(
    r"(?:->|\bL?\d+\b|['\"`]|\b(?:None|True|False)\b|\[\]|\{\}|[A-Za-z_]\w*\s*\(|[A-Za-z_]\w*_[A-Za-z0-9_]*)"
)

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
        yield
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
    return Ledger.from_json(raw)


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


def open_round(
    ledger: Ledger,
    snap: DiffSnapshot,
    tier: Tier,
    lenses: Sequence[str],
    packet_path: str,
    symbols_by_hunk: Mapping[str, Sequence[str]],
    *,
    signals: tuple[QualitySignal, ...],
    scope: Sequence[str] | None = None,
    allow_scope_expansion: bool = False,
) -> tuple[Ledger, Round]:
    """Start the next review round for `snap`; re-preparing an unchanged diff is a no-op.

    A new lens on an unchanged diff opens a new round, as a deeper tier does.
    """
    normalized_scope = None if scope is None else tuple(sorted(scope))
    if ledger.rounds:
        _validate_round_continuation(
            ledger, snap, normalized_scope, allow_scope_expansion=allow_scope_expansion
        )
        latest = ledger.rounds[-1]
        if (
            latest.fingerprint == snap.fingerprint
            and latest.tier == tier
            and latest.scope == normalized_scope
            and latest.lenses == tuple(lenses)
        ):
            return ledger, latest
    unknown = sorted(set(symbols_by_hunk) - {hunk.id for hunk in snap.hunks})
    if unknown:
        raise LedgerError(f"symbols given for hunks not in the snapshot: {', '.join(unknown)}")
    carried, reopened = _carry_over(ledger, snap.hunks, lenses, symbols_by_hunk)
    round_ = Round(
        number=len(ledger.rounds) + 1,
        fingerprint=snap.fingerprint,
        base_sha=snap.base_sha,
        tier=tier,
        prepared_at=_now(),
        hunks=_build_hunk_refs(snap.hunks, carried, reopened),
        lenses=tuple(lenses),
        packet_path=packet_path,
        signals=signals,
        scope=normalized_scope,
    )
    return replace(ledger, rounds=(*ledger.rounds, round_)), round_


def _reopened_hunk_ids(
    hunks: Sequence[Hunk],
    previous: set[str],
    reviewed: set[str],
    symbols_by_hunk: Mapping[str, Sequence[str]],
    file_digests: Mapping[str, str],
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
    return {
        hunk.id
        for hunk in hunks
        if hunk.id in affected and (hunk.digest in reviewed or file_digests[hunk.path] in reviewed)
    }


def _carry_over(
    ledger: Ledger,
    hunks: Sequence[Hunk],
    lenses: Sequence[str],
    symbols_by_hunk: Mapping[str, Sequence[str]],
) -> tuple[frozenset[str], frozenset[str]]:
    """Ids of the hunks of the next round that are carried over, and of those that are reopened."""
    if not ledger.rounds:
        return frozenset(), frozenset()
    covered = _carryable_coverage(ledger)
    reviewed = {digest for _, digest in covered}
    previous = {hunk.digest for hunk in ledger.rounds[-1].hunks}
    file_digests = {path: file_digest(hunks, path) for path in {hunk.path for hunk in hunks}}
    reopened = _reopened_hunk_ids(hunks, previous, reviewed, symbols_by_hunk, file_digests)
    carried = {
        hunk.id
        for hunk in hunks
        if hunk.id not in reopened
        and all(
            (lens, file_digests[hunk.path]) in covered
            or (lens != DESIGN_LENS and (lens, hunk.digest) in covered)
            for lens in lenses
        )
    }
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


def _validate_design_targets(round_: Round, paths: Sequence[str]) -> tuple[str, ...]:
    files = {file.path: file for file in round_.files}
    unknown = [path for path in paths if path not in files]
    if unknown:
        raise LedgerError(
            f"unknown files in round {round_.number}: {', '.join(unknown)}; "
            f"files to cover: {', '.join(round_.open_files) or 'none'}",
            field="files",
        )
    carried = [path for path in paths if files[path].carried]
    if carried:
        raise LedgerError(
            f"{', '.join(carried)} already covered in an earlier round (carried over); "
            "cover only the remaining files",
            field="files",
        )
    return tuple(files[path].digest for path in paths)


def _validate_coverage_lenses(round_: Round, lenses: Sequence[str]) -> tuple[str, ...]:
    cleaned = tuple(lens.strip() for lens in lenses if lens.strip())
    if not cleaned:
        raise FieldError("lens", "must name at least one lens")
    if len(set(cleaned)) != len(cleaned):
        raise FieldError("lens", f"must not repeat a lens: {', '.join(cleaned)}")
    for lens in cleaned:
        if lens not in LENS_IDS:
            raise FieldError(
                "lens", f"names unknown lens {lens!r}; valid lenses: {', '.join(sorted(LENS_IDS))}"
            )
    if len(cleaned) > 1 and SWEEP_LENS in cleaned:
        raise FieldError(
            "lens", f"`{SWEEP_LENS}` runs last and cannot be combined with other lenses"
        )
    if len(cleaned) > 1 and DESIGN_LENS in cleaned:
        raise FieldError(
            "lens", f"cannot combine `{DESIGN_LENS}` (covers files) with hunk lenses"
        )
    for lens in cleaned:
        if lens not in round_.lenses:
            raise FieldError(
                "lens",
                f"{lens!r} is not selected in round {round_.number}; "
                f"selected lenses: {', '.join(round_.lenses)}",
            )
    return cleaned


def _resolve_coverage_unit(
    round_: Round,
    lenses: tuple[str, ...],
    targets: Sequence[str],
    target_kind: TargetKind | None,
) -> TargetKind:
    if lenses == (DESIGN_LENS,):
        if target_kind == "hunks":
            raise LedgerError(
                "the `design` lens covers changed files, not hunks; pass --files instead of --hunks",
                field="hunks",
            )
        return "files"
    if target_kind == "files":
        if round_.tier != "quick":
            raise LedgerError(
                "--files is only used with `--lens design`; pass --hunks for hunk lenses",
                field="files",
            )
        return "files"
    if target_kind == "hunks":
        return "hunks"
    if round_.tier == "quick" and targets and not any(_HUNK_ID_RE.fullmatch(t) for t in targets):
        return "files"
    return "hunks"


def _validate_coverage_targets(
    round_: Round,
    lenses: tuple[str, ...],
    targets: Sequence[str],
    target_kind: TargetKind | None = None,
) -> tuple[TargetKind, tuple[str, ...]]:
    unit = _resolve_coverage_unit(round_, lenses, targets, target_kind)
    if not 1 <= len(targets) <= MAX_HUNKS_PER_COVERAGE:
        raise LedgerError(
            f"cover 1..{MAX_HUNKS_PER_COVERAGE} {unit} per call (got {len(targets)}); "
            f"split the {unit} into smaller batches with a note for each",
            field=unit,
        )
    if len(set(targets)) != len(targets):
        raise LedgerError(f"{unit} must not repeat: {', '.join(targets)}", field=unit)
    if unit == "files":
        return unit, _validate_design_targets(round_, targets)
    return unit, _validate_hunk_targets(round_, targets)


def _validate_hunk_targets(round_: Round, hunk_ids: Sequence[str]) -> tuple[str, ...]:
    by_id = {hunk.id: hunk for hunk in round_.hunks}
    unknown = [hunk_id for hunk_id in hunk_ids if hunk_id not in by_id]
    if unknown:
        raise LedgerError(
            f"unknown hunk ids in round {round_.number}: {', '.join(unknown)}; "
            f"hunks to cover: {', '.join(round_.open_hunk_ids) or 'none'}",
            field="hunks",
        )
    carried = [hunk_id for hunk_id in hunk_ids if by_id[hunk_id].carried]
    if carried:
        raise LedgerError(
            f"{', '.join(carried)} already covered in an earlier round (carried over); "
            "cover only the remaining hunks",
            field="hunks",
        )
    return tuple(by_id[hunk_id].digest for hunk_id in hunk_ids)


def _target_pattern(target: str) -> str:
    """`target` as a whole word; a path must also not match inside `src/a.py` or `a.pyc`."""
    if _HUNK_ID_RE.fullmatch(target):
        return rf"(?<!\w){re.escape(target)}(?!\w)"
    return rf"(?<![\w./\\-]){re.escape(target)}(?![\w/\\-]|\.\w)"


def _unique_basenames(round_: Round) -> frozenset[str]:
    counts: dict[str, int] = {}
    for file in round_.files:
        name = Path(file.path).name
        counts[name] = counts.get(name, 0) + 1
    return frozenset(name for name, count in counts.items() if count == 1)


def _resolve_file_pattern(path: str, unique_basenames: frozenset[str], note: str) -> str:
    full = _target_pattern(path)
    if re.search(full, note) is not None:
        return full
    name = Path(path).name
    if name != path and name in unique_basenames:
        return _target_pattern(name)
    return full


def _target_patterns(
    round_: Round, targets: Sequence[str], note: str, *, unit: TargetKind
) -> dict[str, str]:
    if unit == "files":
        unique = _unique_basenames(round_)
        return {path: _resolve_file_pattern(path, unique, note) for path in targets}
    return {hid: _target_pattern(hid) for hid in targets}


def _extract_target_segments(patterns: Mapping[str, str], note: str) -> dict[str, str]:
    spans: list[tuple[int, str]] = []
    for target, pattern in patterns.items():
        match = re.search(pattern, note)
        if match is not None:
            spans.append((match.start(), target))
    spans.sort()
    segments: dict[str, str] = {}
    for index, (start, target) in enumerate(spans):
        end = spans[index + 1][0] if index + 1 < len(spans) else len(note)
        segments[target] = note[start:end].strip()
    return segments


def _normalize_note_segment(segment: str, pattern: str = "") -> str:
    cleaned = re.sub(pattern, "", segment) if pattern else segment
    without_ids = _HUNK_ID_RE.sub("", cleaned)
    return re.sub(r"\s+", " ", without_ids.strip().strip(";:,-.")).strip().lower()


def _classify_target_segment(
    segment: str, pattern: str, *, min_segment_chars: int
) -> tuple[Literal["too_short", "no_concrete"] | None, str]:
    without_id = re.sub(pattern, " ", segment)
    stripped_generic = _GENERIC_WORDS_RE.sub(" ", without_id)
    substantive = re.sub(r"[\W_]+", "", stripped_generic)
    if len(segment) < min_segment_chars or len(substantive) < MIN_SUBSTANTIVE_NOTE_CHARS:
        return "too_short", ""
    if not file_reference_in(without_id) and _CONCRETE_COVERAGE_RE.search(without_id) is None:
        return "no_concrete", ""
    return None, _normalize_note_segment(segment, pattern)


def _raise_note_target_errors(
    *,
    unnamed: Sequence[str],
    too_short: Sequence[str],
    no_concrete: Sequence[str],
    noun: str,
    min_chars: int,
) -> None:
    parts: list[str] = []
    if unnamed:
        parts.append(
            f"must name every covered {noun} and what was tried on it; missing: {', '.join(unnamed)}"
        )
    if too_short:
        parts.append(
            f"needs at least {min_chars} characters per hunk or file, "
            f"{MIN_SUBSTANTIVE_NOTE_CHARS} of them beyond generic praise; "
            f"too little concrete detail: {', '.join(too_short)}"
        )
    if no_concrete:
        parts.append(
            "must include a concrete detail (file:line, number/literal, ->, or symbol name): "
            f"{', '.join(no_concrete)}"
        )
    raise FieldError("note", "; ".join(parts), example=COVERAGE_NOTE_EXAMPLE)


def _check_reused_note_segments(
    ledger: Ledger,
    round_: Round,
    lenses: Sequence[str],
    validated: Mapping[str, str],
    *,
    unit: TargetKind,
    noun: str,
) -> None:
    for lens in lenses:
        norm_by_target = _recorded_note_segments(ledger, round_, lens)
        norm_by_target.update(validated)
        counts: dict[str, int] = {}
        for body in norm_by_target.values():
            counts[body] = counts.get(body, 0) + 1
            if counts[body] > MAX_REUSED_NOTE_SEGMENTS:
                raise FieldError(
                    "note",
                    f"reuses the same note segment for more than {MAX_REUSED_NOTE_SEGMENTS} {unit} "
                    f"in lens `{lens}`; write a specific note per {noun}",
                    example=COVERAGE_NOTE_EXAMPLE,
                )


def _validate_coverage_note(
    ledger: Ledger,
    round_: Round,
    lenses: Sequence[str],
    targets: Sequence[str],
    note: str,
    *,
    unit: TargetKind = "hunks",
) -> None:
    patterns = _target_patterns(round_, targets, note, unit=unit)
    unnamed = [target for target, pattern in patterns.items() if not re.search(pattern, note)]
    unnamed_set = set(unnamed)
    named = [target for target in targets if target not in unnamed_set]
    noun = "file" if unit == "files" else "hunk"
    min_chars = 2 * MIN_HUNK_NOTE_SEGMENT_CHARS if len(lenses) >= 2 else MIN_HUNK_NOTE_SEGMENT_CHARS
    named_patterns = {t: patterns[t] for t in named}
    segments = _extract_target_segments(named_patterns, note)
    too_short: list[str] = []
    no_concrete: list[str] = []
    validated: dict[str, str] = {}
    for target in named:
        issue, normalized = _classify_target_segment(
            segments[target], patterns[target], min_segment_chars=min_chars
        )
        if issue == "too_short":
            too_short.append(target)
        elif issue == "no_concrete":
            no_concrete.append(target)
        else:
            validated[target] = normalized
    if unnamed or too_short or no_concrete:
        _raise_note_target_errors(
            unnamed=unnamed,
            too_short=too_short,
            no_concrete=no_concrete,
            noun=noun,
            min_chars=min_chars,
        )
    _check_reused_note_segments(ledger, round_, lenses, validated, unit=unit, noun=noun)


def _recorded_note_segments(ledger: Ledger, round_: Round, lens: str) -> dict[str, str]:
    """The normalized note segment `lens` already recorded for each hunk (or file) of `round_`."""
    unique = _unique_basenames(round_)
    path_by_digest = {file.digest: file.path for file in round_.files}
    segments: dict[str, str] = {}
    for record in ledger.coverage:
        if record.round != round_.number or record.lens != lens:
            continue
        file_paths = [path_by_digest[d] for d in record.digests if d in path_by_digest]
        if file_paths:
            patterns = {path: _resolve_file_pattern(path, unique, record.note) for path in file_paths}
        else:
            patterns = {hid: _target_pattern(hid) for hid in _HUNK_ID_RE.findall(record.note)}
        for target, segment in _extract_target_segments(patterns, record.note).items():
            segments[target] = _normalize_note_segment(segment, patterns[target])
    return segments


def _match_open_file(round_: Round, unique_basenames: frozenset[str], target: str) -> str | None:
    open_paths = [file.path for file in round_.files if not file.carried]
    if target in open_paths:
        return target
    for path in open_paths:
        name = Path(path).name
        if name == target and name in unique_basenames:
            return path
    return None


def coverage_units_for_targets(
    round_: Round, lenses: Sequence[str], targets: Sequence[str]
) -> tuple[tuple[str, str], ...]:
    """Best-effort `(lens, unit_id)` pairs an unrecorded coverage entry tried to cover."""
    valid_lenses = [
        cleaned
        for lens in lenses
        if (cleaned := lens.strip()) in round_.lenses and cleaned != SWEEP_LENS
    ]
    if not valid_lenses:
        return ()
    unique = _unique_basenames(round_)
    open_hunks = {hunk.id for hunk in round_.hunks if not hunk.carried}
    pairs: list[tuple[str, str]] = []
    for lens in valid_lenses:
        for raw in targets:
            target = raw.strip()
            if not target:
                continue
            if lens == DESIGN_LENS:
                matched = _match_open_file(round_, unique, target)
                if matched is not None:
                    pairs.append((DESIGN_LENS, matched))
            elif target in open_hunks:
                pairs.append((lens, target))
            elif round_.tier == "quick":
                matched = _match_open_file(round_, unique, target)
                if matched is not None:
                    pairs.extend(
                        (lens, hunk.id)
                        for hunk in round_.hunks
                        if hunk.path == matched and not hunk.carried
                    )
    return tuple(dict.fromkeys(pairs))


def add_multi_coverage(
    ledger: Ledger,
    lenses: Sequence[str],
    targets: Sequence[str],
    note: str,
    *,
    target_kind: TargetKind | None = None,
    rejected_by: Mapping[tuple[str, str], int] | None = None,
) -> tuple[Ledger, tuple[Coverage, ...]]:
    """Record that every lens in `lenses` reviewed `targets` of the latest round."""
    round_ = _latest_round(ledger)
    cleaned_lenses = _validate_coverage_lenses(round_, lenses)
    unit, digests = _validate_coverage_targets(round_, cleaned_lenses, targets, target_kind)
    _validate_coverage_note(ledger, round_, cleaned_lenses, targets, note, unit=unit)
    if SWEEP_LENS in cleaned_lenses:
        _require_lenses_before_sweep(ledger, round_, rejected_by=rejected_by)
    now = _now()
    stripped_note = note.strip()
    records = tuple(
        Coverage(
            round=round_.number,
            lens=lens,
            digests=digests,
            note=stripped_note,
            recorded_at=now,
        )
        for lens in cleaned_lenses
    )
    return replace(ledger, coverage=(*ledger.coverage, *records)), records


def add_coverage(
    ledger: Ledger,
    lens: str,
    hunk_ids: Sequence[str],
    note: str,
    *,
    target_kind: TargetKind | None = None,
) -> tuple[Ledger, Coverage]:
    """Record that `lens` reviewed the given hunks (or files) of the latest round."""
    updated, (record,) = add_multi_coverage(
        ledger, (lens,), hunk_ids, note, target_kind=target_kind
    )
    return updated, record


def _require_lenses_before_sweep(
    ledger: Ledger,
    round_: Round,
    *,
    rejected_by: Mapping[tuple[str, str], int] | None = None,
) -> None:
    pending: list[str] = []
    for lens, ids in uncovered_by_lens(ledger, round_).items():
        if lens == SWEEP_LENS or not ids:
            continue
        groups: dict[int | None, list[str]] = {}
        for uid in ids:
            entry_idx = rejected_by.get((lens, uid)) if rejected_by else None
            groups.setdefault(entry_idx, []).append(uid)
        for entry_idx, group_ids in groups.items():
            joined = ",".join(group_ids)
            if entry_idx is None:
                pending.append(f"{lens} {joined}")
            else:
                pending.append(f"{lens} {joined} (depends on entry {entry_idx})")
    if pending:
        raise LedgerError(
            f"the `{SWEEP_LENS}` lens runs after every other lens; still uncovered in round "
            f"{round_.number}: {'; '.join(pending)}. Cover those first, then record the sweep",
            field="lens",
        )


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
    signal_id: str | None,
) -> tuple[Ledger, Finding]:
    """Record a finding against the latest round and the code it reviewed."""
    round_ = _latest_round(ledger)
    if signal_id is not None:
        round_.signal(signal_id)
    now = _now()
    finding = Finding(
        id=next_finding_id(ledger),
        round=round_.number,
        lens=lens,
        path=path.strip(),
        line=line,
        severity=severity,
        title=title.strip(),
        scenario=scenario.strip(),
        suggestion=suggestion.strip(),
        fingerprint=round_.fingerprint,
        verdict=None,
        verdict_evidence="",
        verdict_run=None,
        resolution=None,
        resolution_note="",
        recorded_at=now,
        updated_at=now,
        signal_id=signal_id,
    )
    return replace(ledger, findings=(*ledger.findings, finding)), finding


def ack_signal(ledger: Ledger, signal_id: str, reason: str) -> tuple[Ledger, SignalAck]:
    """Acknowledge a quality signal in the latest round with a concrete reason (>= 40 chars)."""
    round_ = _latest_round(ledger)
    sig = round_.signal(signal_id)
    if sig.needs_user:
        raise LedgerError(
            f"{signal_id} (`{sig.kind.value}`) cannot be acknowledged by the agent; shrink the "
            "change or hand the review to the user",
            field="signal_id",
        )
    stripped = reason.strip()
    if len(stripped) < MIN_SIGNAL_ACK_REASON_CHARS:
        raise FieldError(
            "reason",
            f"must have at least {MIN_SIGNAL_ACK_REASON_CHARS} characters (got {len(stripped)})",
        )
    if _CONCRETE_TOKEN_RE.search(stripped) is None and not file_reference_in(stripped):
        raise FieldError(
            "reason",
            "must include a concrete code detail (a file:line, symbol(), `identifier`, number, or ->)",
        )
    ack = SignalAck(signal_id, round_.number, round_.fingerprint, stripped, _now())
    kept = tuple(
        item
        for item in ledger.signal_acks
        if not (item.round == round_.number and item.signal_id == signal_id)
    )
    return replace(ledger, signal_acks=(*kept, ack)), ack


def add_lenses(ledger: Ledger, round_number: int, reasons: Mapping[str, str]) -> Ledger:
    """Record the lenses the agent added to round `round_number`, each with its reason."""
    recorded_at = _now()
    additions = tuple(
        LensAddition(lens, reason.strip(), round_number, recorded_at) for lens, reason in reasons.items()
    )
    return replace(ledger, lens_additions=(*ledger.lens_additions, *additions))


def file_sha256(root: Path, rel_path: str) -> str | None:
    """The SHA-256 of a work-tree file, or None when the file does not exist."""
    target = root / rel_path
    if not target.is_file():
        return None
    try:
        return hashlib.sha256(target.read_bytes()).hexdigest()
    except OSError as exc:
        raise LedgerError(f"cannot read {target}: {exc}") from exc


def add_fact(
    ledger: Ledger,
    *,
    hunks: Sequence[str],
    source: SourceRef,
    text: str,
    source_digest: str,
    author_lens: str,
) -> tuple[Ledger, Fact]:
    """Record a fact about the source that other reviewers of the named hunks can rely on."""
    round_ = _latest_round(ledger)
    known = {hunk.id for hunk in round_.hunks}
    unknown = [hunk_id for hunk_id in hunks if hunk_id not in known]
    if unknown:
        raise FieldError("hunks", f"are not in round {round_.number}: {', '.join(unknown)}")
    fact = Fact(
        id=next_id("F-", (item.id for item in ledger.facts)),
        hunks=tuple(dict.fromkeys(hunks)),
        source=source,
        text=text.strip(),
        file_digest=source_digest,
        author_lens=author_lens,
    )
    return replace(ledger, facts=(*ledger.facts, fact)), fact


def valid_facts_for_hunk(ledger: Ledger, root: Path) -> dict[str, tuple[Fact, ...]]:
    """Facts per hunk id whose source file is unchanged since the fact was recorded."""
    by_hunk: dict[str, list[Fact]] = {}
    digest_cache: dict[str, str | None] = {}
    for fact in ledger.facts:
        path = fact.source.path
        if path not in digest_cache:
            digest_cache[path] = file_sha256(root, path)
        if digest_cache[path] is not None and digest_cache[path] == fact.file_digest:
            for hid in fact.hunks:
                by_hunk.setdefault(hid, []).append(fact)
    return {hid: tuple(items) for hid, items in by_hunk.items()}


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
) -> tuple[Ledger, Finding, tuple[Finding, ...]]:
    """Fold duplicate findings that share a root cause into `target_id` before verification."""
    target, duplicates = _mergeable(ledger, target_id, duplicate_ids)
    now = _now()
    updated_target = replace(
        target,
        severity=min((item.severity for item in (target, *duplicates)), key=SEVERITIES.index),
        title=target.title if title is None else title.strip(),
        scenario=_merged_scenario(target, duplicates) if scenario is None else scenario.strip(),
        suggestion=_merged_suggestion(target, duplicates) if suggestion is None else suggestion.strip(),
        signal_id=_merged_signal(target, duplicates),
        updated_at=now,
    )
    evidence = f"{GROUPED_EVIDENCE_PREFIX}{target.id} ({target.path}:{target.line}): same root cause."
    dismissed = tuple(
        replace(dup, verdict="dismissed", verdict_evidence=evidence, updated_at=now)
        for dup in duplicates
    )
    by_id = {item.id: item for item in (updated_target, *dismissed)}
    findings = tuple(by_id.get(item.id, item) for item in ledger.findings)
    return replace(ledger, findings=findings), updated_target, dismissed


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
    for item in (target, *duplicates):
        if item.verdict is not None:
            raise LedgerError(
                "merge only works on findings without a verdict (before verification); "
                f"{item.id} already has the verdict {item.verdict}"
            )
    return target, duplicates


def _merged_scenario(target: Finding, duplicates: Sequence[Finding]) -> str:
    """The target's scenario plus every duplicate consequence it does not already describe."""
    merged = target.scenario
    for dup in duplicates:
        if dup.scenario not in merged:
            merged += f"; [{dup.id}/{dup.lens}] {dup.scenario}"
    return merged


def _merged_suggestion(target: Finding, duplicates: Sequence[Finding]) -> str:
    return target.suggestion or next((dup.suggestion for dup in duplicates if dup.suggestion), "")


def _merged_signal(target: Finding, duplicates: Sequence[Finding]) -> str | None:
    """The quality signal the merged finding resolves; it must not resolve two different ones.

    Signal ids are per round, so a signal never moves to a finding of another round.
    """
    moved = [dup.id for dup in duplicates if dup.signal_id is not None and dup.round != target.round]
    if moved:
        raise LedgerError(
            f"{', '.join(moved)} resolve a quality signal of another round than {target.id}; "
            "merge only findings of the same round"
        )
    signals = {item.signal_id for item in (target, *duplicates) if item.signal_id is not None}
    if len(signals) > 1:
        raise LedgerError(
            f"the findings resolve different quality signals ({', '.join(sorted(signals))}); "
            "merge only findings about the same signal"
        )
    return next(iter(signals), None)


def set_verdict(
    ledger: Ledger, finding_id: str, verdict: Verdict, evidence: str, run_id: str | None
) -> tuple[Ledger, Finding]:
    """Record an independent verdict; the latest verdict wins and a non-defect verdict drops any resolution."""
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
    updated = replace(
        finding,
        verdict=verdict,
        verdict_evidence=evidence.strip(),
        verdict_run=run_id,
        resolution=finding.resolution if keeps_resolution else None,
        resolution_note=finding.resolution_note if keeps_resolution else "",
        updated_at=_now(),
    )
    return _with_finding(ledger, updated)


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
    updated = replace(finding, resolution=resolution, resolution_note=note.strip(), updated_at=_now())
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
    updated = replace(run, waiver=RunWaiver(reason=reason.strip(), waived_at=_now()))
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


def _reopened_in(ledger: Ledger, upto: int) -> dict[str, int]:
    """Per hunk (and file) digest, the latest round up to `upto` that reopened it."""
    latest: dict[str, int] = {}
    for round_ in ledger.rounds[:upto]:
        for hunk in round_.hunks:
            if hunk.reopened:
                latest[hunk.digest] = round_.number
                latest[file_digest(round_.hunks, hunk.path)] = round_.number
    return latest


def _lens_coverage(ledger: Ledger, upto: int) -> dict[tuple[str, str], datetime]:
    """When each (lens, digest) pair other than the sweep was first covered, as of round `upto`.

    Coverage recorded before the latest round that reopened a digest does not count for it.
    """
    reopened = _reopened_in(ledger, upto)
    first: dict[tuple[str, str], datetime] = {}
    for record in ledger.coverage:
        if record.lens == SWEEP_LENS or record.round > upto:
            continue
        at = parse_timestamp(record.recorded_at)
        for digest in record.digests:
            key = (record.lens, digest)
            if record.round >= reopened.get(digest, 1) and (key not in first or at < first[key]):
                first[key] = at
    return first


def _lenses_done_at(ledger: Ledger, round_: Round) -> datetime | None:
    """When every lens but the sweep had covered every open hunk/file of `round_`; None if not yet."""
    first = _lens_coverage(ledger, round_.number)
    file_digests = {file.path: file.digest for file in round_.files}
    done = datetime.min.replace(tzinfo=timezone.utc)
    for lens in round_.lenses:
        if lens == SWEEP_LENS:
            continue
        if lens == DESIGN_LENS:
            for file in round_.files:
                if file.carried:
                    continue
                at = first.get((DESIGN_LENS, file.digest))
                if at is None:
                    return None
                done = max(done, at)
            continue
        for hunk in round_.hunks:
            if hunk.carried:
                continue
            times = [
                at
                for key in ((lens, hunk.digest), (lens, file_digests[hunk.path]))
                if (at := first.get(key)) is not None
            ]
            if not times:
                return None
            done = max(done, min(times))
    return done


def counting_sweeps(ledger: Ledger) -> list[Coverage]:
    """Sweep records made once their round's other lenses were complete; earlier ones do not count."""
    swept_rounds = {record.round for record in ledger.coverage if record.lens == SWEEP_LENS}
    done_at = {number: _lenses_done_at(ledger, ledger.rounds[number - 1]) for number in swept_rounds}
    return [
        record
        for record in ledger.coverage
        if record.lens == SWEEP_LENS
        and (done := done_at[record.round]) is not None
        and parse_timestamp(record.recorded_at) >= done
    ]


def _carryable_coverage(ledger: Ledger) -> set[tuple[str, str]]:
    """(lens, digest) pairs that still count after the latest round, for carrying hunks over."""
    reopened = _reopened_in(ledger, len(ledger.rounds))
    swept = {
        (SWEEP_LENS, digest)
        for record in counting_sweeps(ledger)
        for digest in record.digests
        if record.round >= reopened.get(digest, 1)
    }
    return set(_lens_coverage(ledger, len(ledger.rounds))) | swept


def open_units(round_: Round, lens: str) -> tuple[str, ...]:
    """What `lens` has to cover in `round_`: open file paths for `design`, else open hunk ids."""
    return round_.open_files if lens == DESIGN_LENS else round_.open_hunk_ids


def uncovered_by_lens(ledger: Ledger, round_: Round) -> dict[str, list[str]]:
    """Hunk ids (or file paths for `design`) of `round_` that each of its lenses still has to cover."""
    lenses = _lens_coverage(ledger, round_.number)
    swept = {
        digest
        for record in counting_sweeps(ledger)
        if record.round == round_.number
        for digest in record.digests
    }
    file_digests = {file.path: file.digest for file in round_.files}
    missing: dict[str, list[str]] = {lens: [] for lens in round_.lenses}
    for lens in round_.lenses:
        if lens == DESIGN_LENS:
            for file in round_.files:
                if not file.carried and (DESIGN_LENS, file.digest) not in lenses:
                    missing[lens].append(file.path)
            continue
        for hunk in round_.hunks:
            fdigest = file_digests[hunk.path]
            covered = (
                (hunk.digest in swept or fdigest in swept)
                if lens == SWEEP_LENS
                else ((lens, hunk.digest) in lenses or (lens, fdigest) in lenses)
            )
            if not hunk.carried and not covered:
                missing[lens].append(hunk.id)
    return missing
