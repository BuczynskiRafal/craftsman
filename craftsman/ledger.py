"""Evidence ledger: review rounds, lens coverage, findings, verification runs and agent events.

The ledger is one JSON file per worktree (`<git-dir>/craftsman/ledger.json`). Every record is
bound to the fingerprint of the diff it refers to, so evidence goes stale when the code changes.
All operations are pure functions that return a new `Ledger`; `mutate` applies one of them under
an exclusive file lock. `add_agent_event` is reserved for platform hooks and must never be exposed
through an agent-facing CLI command: subagent isolation evidence has to come from the platform.
"""

from __future__ import annotations

import ast
import errno
import hashlib
import json
import os
import re
import shlex
import sys
import tempfile
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, fields, is_dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, TypeVar, get_args

from craftsman.diffscan import DiffScanError, DiffSnapshot, Hunk, git_dir
from craftsman.lenses import LENS_IDS, TIERS, Tier

Severity = Literal["blocker", "major", "minor", "nit"]
Verdict = Literal["proven", "probable", "question", "dismissed"]
Resolution = Literal["fixed", "accepted"]
RunKind = Literal["test", "e2e", "repro", "lint", "build", "other"]
AgentEventKind = Literal["start", "stop"]
IsolationEvidence = Literal["hooks", "transcript", "none"]
IssueCode = Literal[
    "no-round",
    "stale-round",
    "tier-too-low",
    "uncovered",
    "sweep-too-early",
    "unresolved-signal",
    "no-verdict",
    "unresolved",
    "no-passing-run",
    "no-e2e-run",
    "failing-run",
    "no-isolation",
    "round-cap",
]

SEVERITIES: tuple[Severity, ...] = get_args(Severity)
VERDICTS: tuple[Verdict, ...] = get_args(Verdict)
RESOLUTIONS: tuple[Resolution, ...] = get_args(Resolution)
RUN_KINDS: tuple[RunKind, ...] = get_args(RunKind)
AGENT_EVENT_KINDS: tuple[AgentEventKind, ...] = get_args(AgentEventKind)
ISOLATION_EVIDENCES: tuple[IsolationEvidence, ...] = get_args(IsolationEvidence)
SELF_REVIEWED_BANNER = (
    "SELF-REVIEWED: no evidence that anyone other than the author reviewed this change"
)

SCHEMA_VERSION = 1
LEDGER_FILENAME = "ledger.json"
OUTPUT_TAIL_CHARS = 4000
MAX_HUNKS_PER_COVERAGE = 4
MIN_COVERAGE_NOTE_CHARS = 30
MIN_TITLE_CHARS = 8
MAX_TITLE_CHARS = 160
MIN_SCENARIO_CHARS = 20
MIN_EVIDENCE_CHARS = 20
SHORT_COMMAND_CHARS = 60
MIN_RESOLUTION_NOTE_CHARS = 20
MIN_WAIVER_REASON_CHARS = 20
MIN_SIGNAL_ACK_REASON_CHARS = 40
MIN_FACT_TEXT_CHARS = 30
MIN_HUNK_NOTE_SEGMENT_CHARS = 40
MIN_SUBSTANTIVE_NOTE_CHARS = 20
MAX_REUSED_NOTE_SEGMENTS = 3
GENERIC_COVERAGE_WORDS: tuple[str, ...] = (
    "looks good",
    "well-factored",
    "well factored",
    "clear structure",
    "no issues",
    "nothing to report",
    "clean",
    "ok",
    "okay",
    "fine",
    "good",
    "lgtm",
    "readable",
    "clear",
    "dry",
    "consistent",
    "concise",
    "accurate",
    "checked",
    "reviewed",
)
FACT_JUDGEMENT_WORDS: tuple[str, ...] = (
    *GENERIC_COVERAGE_WORDS,
    "bug",
    "wrong",
    "issue",
    "problem",
    "should",
)
QUICK_TIER_MAX_CHANGED_LINES = 40
MAX_NEXT_ACTIONS = 12
SUMMARY_RECENT_RUNS = 3
LOCK_POLL_S = 0.05
REPLACE_RETRIES = 5

_PASSING_KINDS: tuple[RunKind, ...] = ("test", "e2e")
_VERIFYING_KINDS: tuple[RunKind, ...] = ("test", "e2e", "lint", "build")
_TRIVIAL_PROGRAMS = frozenset({"true", ":", "exit", "echo", "printf"})
_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh"})
_POWERSHELLS = frozenset({"powershell", "pwsh"})
_SHELL_SEPARATORS = frozenset({"&&", "||", ";", "|", "&", "(", ")"})
_PYTHON_RE = re.compile(r"python(?:\d+(?:\.\d+)*)?|py")
_PYTHON_EXITS = frozenset({"exit", "quit"})
_WORD_RE = re.compile(r"\w+")
_EXTENSIONLESS_FILES = (
    "Makefile|Dockerfile|Jenkinsfile|Gemfile|Rakefile|Procfile|BUILD|WORKSPACE|Vagrantfile|"
    "Containerfile|Justfile|AUTHORS|CONTRIBUTORS|CHANGELOG|CHANGES|NEWS|HISTORY|README|"
    "CONTRIBUTING|LICENSE|COPYING|NOTICE|PATENTS|CODEOWNERS|MAINTAINERS|SECURITY|TODO"
)
_FILE_REFERENCE_RE = re.compile(
    r"(?:[\w./\\-]+\.\w+"  # with an extension: app.py:12
    r"|[\w./\\-]*[/\\][\w.-]+"  # with a directory: bin/tool:12
    rf"|(?<![\w.-])(?i:{_EXTENSIONLESS_FILES})"  # a well-known extensionless name: Makefile:12
    r"):\d+"
)
_NO_REPRO_PREFIX = "no-repro:"
_STATIC_PREFIX = "static:"
_INTENT_PREFIX = "intent:"
GROUPED_EVIDENCE_PREFIX = "Grouped into "
DESIGN_LENS = "design"
SWEEP_LENS = "sweep"
COVERAGE_NOTE_EXAMPLE = "H3 parse_cfg: x=None -> L42 TypeError; callers cli.py:19 ok"
PROBABLE_EVIDENCE_EXAMPLE = (
    "no-repro: needs a live Postgres; api.py:40 passes None -> db.py:88 calls None.execute()"
)
STATIC_EVIDENCE_EXAMPLE = (
    "static: cart.py:3 divides sum(prices) by len(items) -> total([10, 20]) returns 15.0, "
    "callers checkout.py:40 expect the sum"
)
QUESTION_EVIDENCE_EXAMPLE = (
    "intent: PR task says '-c sets rootdir to the config file's directory'; "
    "findpaths.py:182 does exactly that, which changes testpaths for configs outside the project root"
)
DISMISSED_EVIDENCE_EXAMPLE = "guarded by cli.py:19, which rejects an empty list before total() runs"
_ID_PATTERNS = {prefix: re.compile(rf"{prefix}[1-9]\d*") for prefix in ("H", "F", "R", "S", "F-")}
_CONCRETE_TOKEN_RE = re.compile(r"(?:->|`[^`]+`|\b\d+\b|[A-Za-z_]\w*\s*\(|[A-Za-z_]\w*\.[A-Za-z_]\w*)")
_GENERIC_WORDS_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(word) for word in GENERIC_COVERAGE_WORDS) + r")\b",
    re.IGNORECASE,
)
_FACT_JUDGEMENT_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(word) for word in FACT_JUDGEMENT_WORDS) + r")\b",
    re.IGNORECASE,
)
_CONCRETE_COVERAGE_RE = re.compile(
    r"(?:->|\bL?\d+\b|['\"`]|\b(?:None|True|False)\b|\[\]|\{\}|[A-Za-z_]\w*\s*\(|[A-Za-z_]\w*_[A-Za-z0-9_]*)"
)

T = TypeVar("T")
S = TypeVar("S", bound=str)


class LedgerError(RuntimeError):
    """Raised for invalid ledger data, rejected operations and ledger I/O failures."""

    def __init__(self, message: str, *, field: str = "") -> None:
        super().__init__(message)
        self.field = field


class FieldError(LedgerError):
    """A validation error that knows which (possibly nested) field it concerns."""

    def __init__(self, field: str, problem: str) -> None:
        super().__init__(" ".join(part for part in (field, problem) if part), field=field)
        self.problem = problem

    def within(self, parent: str) -> FieldError:
        return FieldError(f"{parent}.{self.field}" if self.field else parent, self.problem)


def format_timestamp(moment: datetime) -> str:
    """UTC ISO-8601 with millisecond precision and a trailing `Z`, as stored in the ledger."""
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _now() -> str:
    return format_timestamp(datetime.now(timezone.utc))


def parse_timestamp(value: str) -> datetime:
    """Parse an ISO-8601 timestamp that carries a UTC offset or a trailing `Z`."""
    parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp {value!r} has no time zone")
    return parsed


# --- field validation -----------------------------------------------------------------------


def _require(condition: bool, field: str, problem: str) -> None:
    if not condition:
        raise FieldError(field, problem)


def _require_text(value: str, field: str, *, min_chars: int = 1, max_chars: int | None = None) -> None:
    length = len(value.strip())
    if max_chars is not None:
        problem = f"must have {min_chars}..{max_chars} characters (got {length})"
        _require(min_chars <= length <= max_chars, field, problem)
    elif min_chars == 1:
        _require(length >= 1, field, "must not be blank")
    else:
        _require(length >= min_chars, field, f"must have at least {min_chars} characters (got {length})")


def _require_choice(value: str, choices: Sequence[str], field: str) -> None:
    _require(value in choices, field, f"must be one of {', '.join(choices)} (got {value!r})")


def _require_id(value: str, prefix: str, field: str) -> None:
    _require(
        _ID_PATTERNS[prefix].fullmatch(value) is not None,
        field,
        f"must look like {prefix}1, {prefix}2, ... (got {value!r})",
    )


def _require_timestamp(value: str, field: str) -> None:
    try:
        parse_timestamp(value)
    except ValueError:
        raise FieldError(
            field, f"must be an ISO-8601 timestamp with a time zone (got {value!r})"
        ) from None


class _Fields:
    """Typed accessors over one JSON object; errors name the offending field."""

    def __init__(self, raw: Any) -> None:
        if not isinstance(raw, dict):
            raise FieldError("", "must be a JSON object")
        self._raw: dict[str, Any] = raw
        self._seen: set[str] = set()

    def _value(self, key: str, expected: type | tuple[type, ...], label: str) -> Any:
        self._seen.add(key)
        if key not in self._raw:
            raise FieldError(key, "is missing")
        value = self._raw[key]
        # bool is a subclass of int, so `true` must not pass for a number
        if not isinstance(value, expected) or (isinstance(value, bool) and expected is not bool):
            raise FieldError(key, f"must be {label}")
        return value

    def text(self, key: str) -> str:
        value: str = self._value(key, str, "a string")
        return value

    def optional_text(self, key: str) -> str | None:
        value: str | None = self._value(key, (str, type(None)), "a string or null")
        return value

    def get_text(self, key: str, default: str = "") -> str:
        if key not in self._raw or self._raw[key] is None:
            self._seen.add(key)
            return default
        return self.text(key)

    def get_optional_text(self, key: str) -> str | None:
        if key not in self._raw:
            self._seen.add(key)
            return None
        return self.optional_text(key)

    def integer(self, key: str) -> int:
        value: int = self._value(key, int, "an integer")
        return value

    def number(self, key: str) -> float:
        return float(self._value(key, (int, float), "a number"))

    def flag(self, key: str) -> bool:
        value: bool = self._value(key, bool, "true or false")
        return value

    def choice(self, key: str, choices: tuple[S, ...]) -> S:
        value = self.text(key)
        for choice in choices:
            if value == choice:
                return choice
        raise FieldError(key, f"must be one of {', '.join(choices)} (got {value!r})")

    def optional_choice(self, key: str, choices: tuple[S, ...]) -> S | None:
        if key in self._raw and self._raw[key] is None:
            self._seen.add(key)
            return None
        return self.choice(key, choices)

    def texts(self, key: str) -> tuple[str, ...]:
        values: list[Any] = self._value(key, list, "a list of strings")
        for index, value in enumerate(values):
            _require(isinstance(value, str), f"{key}[{index}]", "must be a string")
        return tuple(values)

    def records(self, key: str, parse: Callable[[Any], T]) -> tuple[T, ...]:
        values: list[Any] = self._value(key, list, "a list")
        parsed: list[T] = []
        for index, value in enumerate(values):
            try:
                parsed.append(parse(value))
            except FieldError as exc:
                raise exc.within(f"{key}[{index}]") from None
        return tuple(parsed)

    def get_records(self, key: str, parse: Callable[[Any], T]) -> tuple[T, ...]:
        if key not in self._raw or self._raw[key] is None:
            self._seen.add(key)
            return ()
        return self.records(key, parse)

    def optional_record(self, key: str, parse: Callable[[Any], T]) -> T | None:
        value: dict[str, Any] | None = self._value(key, (dict, type(None)), "an object or null")
        if value is None:
            return None
        try:
            return parse(value)
        except FieldError as exc:
            raise exc.within(key) from None

    def done(self, record: T) -> T:
        """Return `record` once every field of the JSON object has been consumed."""
        unknown = sorted(set(self._raw) - self._seen)
        if unknown:
            raise FieldError(unknown[0], "is not a known field")
        return record


def _record_json(record: Any) -> dict[str, Any]:
    return {field.name: _json_value(getattr(record, field.name)) for field in fields(record)}


def _json_value(value: Any) -> Any:
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    if is_dataclass(value) and not isinstance(value, type):
        return _record_json(value)
    return value


# --- records --------------------------------------------------------------------------------


@dataclass(frozen=True)
class HunkRef:
    id: str
    path: str
    digest: str
    new_start: int
    new_len: int
    carried: bool
    reopened: bool = False  # open again because it mentions a symbol changed since its coverage

    def __post_init__(self) -> None:
        _require_id(self.id, "H", "id")
        _require_text(self.path, "path")
        _require_text(self.digest, "digest")
        _require(self.new_start >= 0, "new_start", "must be >= 0")
        _require(self.new_len >= 0, "new_len", "must be >= 0")
        _require(not (self.carried and self.reopened), "reopened", "must be false for a carried hunk")

    def to_json(self) -> dict[str, Any]:
        return _record_json(self)

    @classmethod
    def from_json(cls, raw: Any) -> HunkRef:
        f = _Fields(raw)
        return f.done(
            cls(
                id=f.text("id"),
                path=f.text("path"),
                digest=f.text("digest"),
                new_start=f.integer("new_start"),
                new_len=f.integer("new_len"),
                carried=f.flag("carried"),
                reopened=f.flag("reopened"),
            )
        )


@dataclass(frozen=True)
class SignalRef:
    id: str
    kind: str
    file: str
    line: int
    detail: str

    def __post_init__(self) -> None:
        _require_id(self.id, "S", "id")
        _require_text(self.kind, "kind")
        _require_text(self.file, "file")
        _require(self.line >= 1, "line", "must be >= 1")
        _require_text(self.detail, "detail")

    def to_json(self) -> dict[str, Any]:
        return _record_json(self)

    @classmethod
    def from_json(cls, raw: Any) -> SignalRef:
        f = _Fields(raw)
        return f.done(
            cls(
                id=f.text("id"),
                kind=f.text("kind"),
                file=f.text("file"),
                line=f.integer("line"),
                detail=f.text("detail"),
            )
        )


@dataclass(frozen=True)
class SignalAck:
    signal_id: str
    round: int
    fingerprint: str
    reason: str
    recorded_at: str

    def __post_init__(self) -> None:
        _require_id(self.signal_id, "S", "signal_id")
        _require(self.round >= 1, "round", "must be >= 1")
        _require_text(self.fingerprint, "fingerprint")
        _require_text(self.reason, "reason", min_chars=MIN_SIGNAL_ACK_REASON_CHARS)
        _require_timestamp(self.recorded_at, "recorded_at")

    def to_json(self) -> dict[str, Any]:
        return _record_json(self)

    @classmethod
    def from_json(cls, raw: Any) -> SignalAck:
        f = _Fields(raw)
        return f.done(
            cls(
                signal_id=f.text("signal_id"),
                round=f.integer("round"),
                fingerprint=f.text("fingerprint"),
                reason=f.text("reason"),
                recorded_at=f.text("recorded_at"),
            )
        )


@dataclass(frozen=True)
class Round:
    number: int
    fingerprint: str
    base_sha: str
    tier: Tier
    prepared_at: str
    hunks: tuple[HunkRef, ...]
    lenses: tuple[str, ...]
    packet_path: str
    signals: tuple[SignalRef, ...] = ()

    def __post_init__(self) -> None:
        _require(self.number >= 1, "number", "must be >= 1")
        _require_text(self.fingerprint, "fingerprint")
        _require_text(self.base_sha, "base_sha")
        _require_choice(self.tier, TIERS, "tier")
        _require_timestamp(self.prepared_at, "prepared_at")
        _require(bool(self.lenses), "lenses", "must name at least one lens")
        for lens in self.lenses:
            _require_choice(lens, sorted(LENS_IDS), "lenses")
        _require(len(set(self.lenses)) == len(self.lenses), "lenses", "must not repeat a lens")
        hunk_ids = [hunk.id for hunk in self.hunks]
        _require(len(set(hunk_ids)) == len(hunk_ids), "hunks", "must not repeat a hunk id")
        sig_ids = [sig.id for sig in self.signals]
        _require(len(set(sig_ids)) == len(sig_ids), "signals", "must not repeat a signal id")

    def to_json(self) -> dict[str, Any]:
        return _record_json(self)

    @classmethod
    def from_json(cls, raw: Any) -> Round:
        f = _Fields(raw)
        return f.done(
            cls(
                number=f.integer("number"),
                fingerprint=f.text("fingerprint"),
                base_sha=f.text("base_sha"),
                tier=f.choice("tier", TIERS),
                prepared_at=f.text("prepared_at"),
                hunks=f.records("hunks", HunkRef.from_json),
                lenses=f.texts("lenses"),
                packet_path=f.text("packet_path"),
                signals=f.get_records("signals", SignalRef.from_json),
            )
        )


@dataclass(frozen=True)
class Coverage:
    round: int
    lens: str
    digests: tuple[str, ...]
    note: str
    recorded_at: str

    def __post_init__(self) -> None:
        _require(self.round >= 1, "round", "must be >= 1")
        _require_choice(self.lens, sorted(LENS_IDS), "lens")
        _require(
            1 <= len(self.digests) <= MAX_HUNKS_PER_COVERAGE,
            "digests",
            f"must list 1..{MAX_HUNKS_PER_COVERAGE} hunk digests (got {len(self.digests)})",
        )
        _require_text(self.note, "note", min_chars=MIN_COVERAGE_NOTE_CHARS)
        _require_timestamp(self.recorded_at, "recorded_at")

    def to_json(self) -> dict[str, Any]:
        return _record_json(self)

    @classmethod
    def from_json(cls, raw: Any) -> Coverage:
        f = _Fields(raw)
        return f.done(
            cls(
                round=f.integer("round"),
                lens=f.text("lens"),
                digests=f.texts("digests"),
                note=f.text("note"),
                recorded_at=f.text("recorded_at"),
            )
        )


def _check_verdict_evidence(verdict: Verdict, evidence: str, run_id: str | None) -> None:
    """Each verdict needs evidence a reader can check; see the module constants for examples."""
    if verdict == "proven":
        _require(
            run_id is not None,
            "verdict_run",
            "is required for a 'proven' verdict: pass --run <id> of the run that demonstrates the "
            "defect (record it with `craftsman run --kind repro -- ...`)",
        )
    elif verdict == "probable":
        stripped = evidence.strip()
        lower = stripped.lower()
        if lower.startswith(_NO_REPRO_PREFIX):
            reason = stripped[len(_NO_REPRO_PREFIX) :].strip()
            valid = len(reason) >= MIN_EVIDENCE_CHARS
        elif lower.startswith(_STATIC_PREFIX):
            reason = stripped[len(_STATIC_PREFIX) :].strip()
            valid = len(reason) >= MIN_EVIDENCE_CHARS and _FILE_REFERENCE_RE.search(reason) is not None
        else:
            valid = False
        _require(
            valid,
            "verdict_evidence",
            f"for 'probable' must start with '{_NO_REPRO_PREFIX}' or '{_STATIC_PREFIX}' followed "
            f"by at least {MIN_EVIDENCE_CHARS} characters (and cite file:line for "
            f"'{_STATIC_PREFIX}'), e.g. \"{PROBABLE_EVIDENCE_EXAMPLE}\" or "
            f'"{STATIC_EVIDENCE_EXAMPLE}"',
        )
    elif verdict == "question":
        stripped = evidence.strip()
        reason = stripped[len(_INTENT_PREFIX) :].strip()
        _require(
            stripped.lower().startswith(_INTENT_PREFIX)
            and len(reason) >= MIN_EVIDENCE_CHARS
            and _FILE_REFERENCE_RE.search(reason) is not None,
            "verdict_evidence",
            f"for 'question' must start with '{_INTENT_PREFIX}' followed by at least "
            f"{MIN_EVIDENCE_CHARS} characters naming the intent source and citing file:line in "
            f'the code, e.g. "{QUESTION_EVIDENCE_EXAMPLE}"',
        )
    else:
        _require(
            run_id is not None or _FILE_REFERENCE_RE.search(evidence) is not None,
            "verdict_evidence",
            "for 'dismissed' must cite the guard as file:line, or pass --run <id> of a run that "
            f'shows correct behavior, e.g. "{DISMISSED_EVIDENCE_EXAMPLE}"',
        )


@dataclass(frozen=True)
class Finding:
    id: str
    round: int
    lens: str
    path: str
    line: int
    severity: Severity
    title: str
    scenario: str
    suggestion: str
    fingerprint: str
    verdict: Verdict | None
    verdict_evidence: str
    verdict_run: str | None
    resolution: Resolution | None
    resolution_note: str
    recorded_at: str
    updated_at: str
    signal_id: str | None = None

    def __post_init__(self) -> None:
        _require_id(self.id, "F", "id")
        if self.signal_id is not None:
            _require_id(self.signal_id, "S", "signal_id")
        _require(self.round >= 1, "round", "must be >= 1")
        _require_choice(self.lens, sorted(LENS_IDS), "lens")
        _require_text(self.path, "path")
        _require(self.line >= 1, "line", f"must be >= 1 (got {self.line})")
        _require_choice(self.severity, SEVERITIES, "severity")
        _require_text(self.title, "title", min_chars=MIN_TITLE_CHARS, max_chars=MAX_TITLE_CHARS)
        _require_text(self.scenario, "scenario", min_chars=MIN_SCENARIO_CHARS)
        _require_text(self.fingerprint, "fingerprint")
        _require_timestamp(self.recorded_at, "recorded_at")
        _require_timestamp(self.updated_at, "updated_at")
        if self.verdict is None:
            _require(self.verdict_run is None, "verdict_run", "must be null without a verdict")
        else:
            _require_choice(self.verdict, VERDICTS, "verdict")
            _require_text(self.verdict_evidence, "verdict_evidence", min_chars=MIN_EVIDENCE_CHARS)
            _check_verdict_evidence(self.verdict, self.verdict_evidence, self.verdict_run)
            _require(
                not (self.severity == "blocker" and self.verdict == "question"),
                "verdict",
                "a blocker is a defect, not a design question; prove, dismiss or fix it",
            )
        if self.resolution is not None:
            _require_choice(self.resolution, RESOLUTIONS, "resolution")
            _require(
                self.verdict in ("proven", "probable"),
                "resolution",
                "is only allowed for proven or probable findings",
            )
            _require_text(self.resolution_note, "resolution_note", min_chars=MIN_RESOLUTION_NOTE_CHARS)
            _require(
                not (self.severity == "blocker" and self.resolution == "accepted"),
                "resolution",
                "'accepted' is not allowed for blocker findings; fix the code",
            )

    def to_json(self) -> dict[str, Any]:
        return _record_json(self)

    @classmethod
    def from_json(cls, raw: Any) -> Finding:
        f = _Fields(raw)
        return f.done(
            cls(
                id=f.text("id"),
                round=f.integer("round"),
                lens=f.text("lens"),
                path=f.text("path"),
                line=f.integer("line"),
                severity=f.choice("severity", SEVERITIES),
                title=f.text("title"),
                scenario=f.text("scenario"),
                suggestion=f.text("suggestion"),
                fingerprint=f.text("fingerprint"),
                verdict=f.optional_choice("verdict", VERDICTS),
                verdict_evidence=f.text("verdict_evidence"),
                verdict_run=f.optional_text("verdict_run"),
                resolution=f.optional_choice("resolution", RESOLUTIONS),
                resolution_note=f.text("resolution_note"),
                recorded_at=f.text("recorded_at"),
                updated_at=f.text("updated_at"),
                signal_id=f.get_optional_text("signal_id"),
            )
        )


@dataclass(frozen=True)
class RunWaiver:
    """Why a failing run no longer blocks the gate, e.g. a mistyped or since-renamed command."""

    reason: str
    waived_at: str

    def __post_init__(self) -> None:
        _require_text(self.reason, "reason", min_chars=MIN_WAIVER_REASON_CHARS)
        _require_timestamp(self.waived_at, "waived_at")

    @classmethod
    def from_json(cls, raw: Any) -> RunWaiver:
        f = _Fields(raw)
        return f.done(cls(reason=f.text("reason"), waived_at=f.text("waived_at")))


@dataclass(frozen=True)
class Run:
    id: str  # "" until add_run assigns the next free id
    kind: RunKind
    argv: tuple[str, ...]
    cwd: str
    exit_code: int
    duration_s: float
    fingerprint: str  # the work tree before the command ran
    started_at: str
    output_tail: str
    fingerprint_after: str | None = None  # set when the command changed the work tree
    waiver: RunWaiver | None = None

    def __post_init__(self) -> None:
        if self.id:
            _require_id(self.id, "R", "id")
        _require_choice(self.kind, RUN_KINDS, "kind")
        _require(bool(self.argv) and bool(self.argv[0].strip()), "argv", "must name a command")
        _require(self.duration_s >= 0, "duration_s", "must be >= 0")
        _require_text(self.fingerprint, "fingerprint")
        _require_timestamp(self.started_at, "started_at")
        _require(
            len(self.output_tail) <= OUTPUT_TAIL_CHARS,
            "output_tail",
            f"must keep only the last {OUTPUT_TAIL_CHARS} characters",
        )
        if self.fingerprint_after is not None:
            _require_text(self.fingerprint_after, "fingerprint_after")
            _require(
                self.fingerprint_after != self.fingerprint,
                "fingerprint_after",
                "must differ from fingerprint (leave it null when the work tree did not change)",
            )
        _require(
            self.waiver is None or self.exit_code != 0, "waiver", "is only allowed for failing runs"
        )

    @property
    def counts_as_passing(self) -> bool:
        """Exited 0 without changing the work tree, so it verified the code it ran against."""
        return self.exit_code == 0 and self.fingerprint_after is None

    @property
    def command(self) -> str:
        return shlex.join(self.argv)

    @property
    def short_command(self) -> str:
        """The command shortened for one-line summaries."""
        command = self.command
        if len(command) <= SHORT_COMMAND_CHARS:
            return command
        return command[: SHORT_COMMAND_CHARS - 1] + "…"

    def to_json(self) -> dict[str, Any]:
        return _record_json(self)

    @classmethod
    def from_json(cls, raw: Any) -> Run:
        f = _Fields(raw)
        return f.done(
            cls(
                id=f.text("id"),
                kind=f.choice("kind", RUN_KINDS),
                argv=f.texts("argv"),
                cwd=f.text("cwd"),
                exit_code=f.integer("exit_code"),
                duration_s=f.number("duration_s"),
                fingerprint=f.text("fingerprint"),
                started_at=f.text("started_at"),
                output_tail=f.text("output_tail"),
                fingerprint_after=f.optional_text("fingerprint_after"),
                waiver=f.optional_record("waiver", RunWaiver.from_json),
            )
        )


@dataclass(frozen=True)
class AgentEvent:
    platform: str
    event: AgentEventKind
    agent: str
    agent_id: str
    at: str

    def __post_init__(self) -> None:
        _require_text(self.platform, "platform")
        _require_choice(self.event, AGENT_EVENT_KINDS, "event")
        _require_timestamp(self.at, "at")

    def to_json(self) -> dict[str, Any]:
        return _record_json(self)

    @classmethod
    def from_json(cls, raw: Any) -> AgentEvent:
        f = _Fields(raw)
        return f.done(
            cls(
                platform=f.text("platform"),
                event=f.choice("event", AGENT_EVENT_KINDS),
                agent=f.text("agent"),
                agent_id=f.text("agent_id"),
                at=f.text("at"),
            )
        )


@dataclass(frozen=True)
class SourceRef:
    path: str
    line: int

    def __post_init__(self) -> None:
        _require_text(self.path, "path")
        _require(self.line >= 1, "line", "must be >= 1")

    def to_json(self) -> dict[str, Any]:
        return _record_json(self)

    @classmethod
    def from_json(cls, raw: Any) -> SourceRef:
        f = _Fields(raw)
        return f.done(cls(path=f.text("path"), line=f.integer("line")))


@dataclass(frozen=True)
class Fact:
    id: str
    hunks: tuple[str, ...]
    source: SourceRef
    text: str
    file_digest: str
    author_lens: str

    def __post_init__(self) -> None:
        _require_id(self.id, "F-", "id")
        _require(bool(self.hunks), "hunks", "must list at least one hunk id")
        for hunk_id in self.hunks:
            _require_id(hunk_id, "H", "hunks")
        _require_text(self.text, "text", min_chars=MIN_FACT_TEXT_CHARS)
        match = _FACT_JUDGEMENT_RE.search(self.text)
        if match is not None:
            raise FieldError(
                "text",
                f"must state a verifiable source fact without judgement words ({match.group(0)!r} found; "
                "record defects with `craftsman review add`)",
            )
        _require_text(self.file_digest, "file_digest")
        if self.author_lens:
            _require_choice(self.author_lens, sorted(LENS_IDS), "author_lens")

    def to_json(self) -> dict[str, Any]:
        return _record_json(self)

    @classmethod
    def from_json(cls, raw: Any) -> Fact:
        f = _Fields(raw)
        return f.done(
            cls(
                id=f.text("id"),
                hunks=f.texts("hunks"),
                source=SourceRef.from_json(f._value("source", dict, "an object")),
                text=f.text("text"),
                file_digest=f.text("file_digest"),
                author_lens=f.text("author_lens"),
            )
        )


@dataclass(frozen=True)
class Ledger:
    rounds: tuple[Round, ...] = ()
    coverage: tuple[Coverage, ...] = ()
    findings: tuple[Finding, ...] = ()
    runs: tuple[Run, ...] = ()
    agents: tuple[AgentEvent, ...] = ()
    signal_acks: tuple[SignalAck, ...] = ()
    facts: tuple[Fact, ...] = ()

    def __post_init__(self) -> None:
        for index, round_ in enumerate(self.rounds):
            _require(round_.number == index + 1, f"rounds[{index}].number", f"must be {index + 1}")
            _require(
                round_.base_sha == self.rounds[0].base_sha,
                f"rounds[{index}].base_sha",
                "must match the base of round 1",
            )
        for index, coverage in enumerate(self.coverage):
            where = f"coverage[{index}]"
            _require(coverage.round <= len(self.rounds), f"{where}.round", "references an unknown round")
            _require(
                coverage.lens in self.rounds[coverage.round - 1].lenses,
                f"{where}.lens",
                f"was not selected in round {coverage.round}",
            )
        run_ids = [run.id for run in self.runs]
        _require(all(run_ids), "runs", "must all have ids")
        _require(len(set(run_ids)) == len(run_ids), "runs", "must not repeat an id")
        finding_ids = [finding.id for finding in self.findings]
        _require(len(set(finding_ids)) == len(finding_ids), "findings", "must not repeat an id")
        fact_ids = [fact.id for fact in self.facts]
        _require(len(set(fact_ids)) == len(fact_ids), "facts", "must not repeat an id")
        for index, finding in enumerate(self.findings):
            where = f"findings[{index}]"
            _require(finding.round <= len(self.rounds), f"{where}.round", "references an unknown round")
            _require(
                finding.verdict_run is None or finding.verdict_run in run_ids,
                f"{where}.verdict_run",
                f"references an unknown run {finding.verdict_run!r}",
            )

    def to_json(self) -> dict[str, Any]:
        return {"schema": SCHEMA_VERSION, **_record_json(self)}

    @classmethod
    def from_json(cls, raw: Any) -> Ledger:
        try:
            f = _Fields(raw)
            schema = f.integer("schema")
            _require(
                schema == SCHEMA_VERSION,
                "schema",
                f"{schema} is not supported (expected {SCHEMA_VERSION}); upgrade craftsman",
            )
            return f.done(
                cls(
                    rounds=f.records("rounds", Round.from_json),
                    coverage=f.records("coverage", Coverage.from_json),
                    findings=f.records("findings", Finding.from_json),
                    runs=f.records("runs", Run.from_json),
                    agents=f.records("agents", AgentEvent.from_json),
                    signal_acks=f.get_records("signal_acks", SignalAck.from_json),
                    facts=f.get_records("facts", Fact.from_json),
                )
            )
        except FieldError as exc:
            raise LedgerError(f"{LEDGER_FILENAME}: {exc}") from None


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
    """Write the ledger atomically: a temporary file in the same directory replaces `path`."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, staged_name = tempfile.mkstemp(prefix=".ledger_", suffix=".tmp", dir=path.parent)
    except OSError as exc:
        raise LedgerError(f"cannot write {path}: {exc}") from exc
    staged = Path(staged_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(ledger.to_json(), handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        replace_with_retry(staged, path)
    except BaseException as exc:
        staged.unlink(missing_ok=True)
        if isinstance(exc, OSError):
            raise LedgerError(f"cannot write {path}: {exc}") from exc
        raise


def replace_with_retry(source: Path, target: Path) -> None:
    """`os.replace`, retried briefly: Windows refuses while another process has the file open."""
    for attempt in range(REPLACE_RETRIES + 1):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == REPLACE_RETRIES:
                raise
            time.sleep(LOCK_POLL_S)


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


def _next_id(prefix: str, existing: Iterable[str]) -> str:
    return f"{prefix}{max((int(item[1:]) for item in existing), default=0) + 1}"


def _covered_pairs(coverage: Iterable[Coverage]) -> set[tuple[str, str]]:
    return {(record.lens, digest) for record in coverage for digest in record.digests}


def _find(ledger: Ledger, finding_id: str) -> Finding:
    for finding in ledger.findings:
        if finding.id == finding_id:
            return finding
    known = ", ".join(finding.id for finding in ledger.findings) or "none recorded"
    raise LedgerError(f"unknown finding {finding_id!r}; known findings: {known}", field="id")


def _with_finding(ledger: Ledger, updated: Finding) -> tuple[Ledger, Finding]:
    findings = tuple(updated if item.id == updated.id else item for item in ledger.findings)
    return replace(ledger, findings=findings), updated


def _to_signal_ref(sig: Any) -> SignalRef:
    if isinstance(sig, SignalRef):
        return sig
    kind = sig.kind.value if hasattr(sig.kind, "value") else str(sig.kind)
    return SignalRef(id=sig.id, kind=kind, file=sig.file, line=sig.line, detail=sig.detail)


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


def open_round(
    ledger: Ledger,
    snap: DiffSnapshot,
    tier: Tier,
    lenses: Sequence[str],
    packet_path: str,
    symbols_by_hunk: Mapping[str, Sequence[str]],
    *,
    signals: Sequence[Any] = (),
) -> tuple[Ledger, Round]:
    """Start the next review round for `snap`; re-preparing an unchanged diff is a no-op."""
    if ledger.rounds:
        if snap.base_sha != ledger.rounds[0].base_sha:
            raise LedgerError(
                f"the base commit changed from {ledger.rounds[0].base_sha[:12]} to "
                f"{snap.base_sha[:12]}; run `craftsman review reset` to archive this ledger first"
            )
        latest = ledger.rounds[-1]
        if latest.fingerprint == snap.fingerprint and latest.tier == tier:
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
        signals=tuple(_to_signal_ref(sig) for sig in signals),
    )
    return replace(ledger, rounds=(*ledger.rounds, round_)), round_


def _carry_over(
    ledger: Ledger,
    hunks: Sequence[Hunk],
    lenses: Sequence[str],
    symbols_by_hunk: Mapping[str, Sequence[str]],
) -> tuple[frozenset[str], frozenset[str]]:
    """Ids of the hunks of the next round that are carried over, and of those that are reopened.

    A hunk is reopened when it is unchanged since the previous round and was reviewed, but
    mentions a symbol defined by a hunk that changed since then, directly or through another
    such hunk; only coverage recorded from then on counts for it. A hunk every lens already
    covered is carried over unless it is reopened.
    """
    if not ledger.rounds:
        return frozenset(), frozenset()
    covered = _carryable_coverage(ledger)
    reviewed = {digest for _, digest in covered}
    previous = {hunk.digest for hunk in ledger.rounds[-1].hunks}
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
    reopened = {hunk.id for hunk in hunks if hunk.id in affected and hunk.digest in reviewed}
    carried = {
        hunk.id
        for hunk in hunks
        if hunk.id not in reopened
        and all(
            (lens, _file_digest(hunks, hunk.path) if lens == DESIGN_LENS else hunk.digest) in covered
            for lens in lenses
        )
    }
    return frozenset(carried), frozenset(reopened)


def _file_digest(hunks: Sequence[Hunk | HunkRef], path: str) -> str:
    return "file:" + path + ":" + ",".join(h.digest for h in hunks if h.path == path)


def _round_files(round_: Round) -> list[tuple[str, str, bool]]:
    paths = list(dict.fromkeys(h.path for h in round_.hunks))
    return [
        (
            path,
            _file_digest(round_.hunks, path),
            all(h.carried for h in round_.hunks if h.path == path),
        )
        for path in paths
    ]


def _mentioned_words(hunk: Hunk) -> frozenset[str]:
    """Identifiers on the hunk's added and context lines.

    Deleted lines, `\\ No newline` markers and `~ mode` lines are not mentions.
    """
    return frozenset(
        word for line in hunk.lines if line[:1] in ("+", " ") for word in _WORD_RE.findall(line, 1)
    )


def reopened_mentions(
    coverage: Iterable[Coverage],
    round_: Round,
    hunks: Sequence[Hunk],
    symbols_by_hunk: Mapping[str, Sequence[str]],
) -> dict[str, tuple[tuple[str, str], ...]]:
    """Why the reopened hunks of `round_` are open again.

    Maps each reopened hunk id to the `(symbol, defining hunk id)` pairs it mentions, where the
    defining hunk is open in `round_`. `coverage` is not needed any more and is ignored.
    """
    open_ids = {hunk.id for hunk in round_.hunks if not hunk.carried}
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


def _validate_design_targets(round_: Round, targets: Sequence[str]) -> tuple[str, ...]:
    files_by_path = {path: (digest, carried) for path, digest, carried in _round_files(round_)}
    by_hunk = {hunk.id: hunk.path for hunk in round_.hunks}
    resolved = [by_hunk.get(item, item) for item in targets]
    unknown = [item for item, path in zip(targets, resolved, strict=True) if path not in files_by_path]
    if unknown:
        open_files = [path for path, _, carried in _round_files(round_) if not carried]
        raise LedgerError(
            f"unknown files in round {round_.number}: {', '.join(unknown)}; "
            f"files to cover: {', '.join(open_files) or 'none'}",
            field="digests",
        )
    carried = [path for path in resolved if files_by_path[path][1]]
    if carried:
        raise LedgerError(
            f"{', '.join(carried)} already covered in an earlier round (carried over); "
            "cover only the remaining files",
            field="digests",
        )
    return tuple(files_by_path[path][0] for path in resolved)


def _validate_coverage_target(round_: Round, lens: str, hunk_ids: Sequence[str]) -> tuple[str, ...]:
    if lens not in round_.lenses:
        if lens not in LENS_IDS:
            raise LedgerError(
                f"unknown lens {lens!r}; valid lenses: {', '.join(sorted(LENS_IDS))}",
                field="lens",
            )
        raise LedgerError(
            f"lens {lens!r} is not selected in round {round_.number}; "
            f"selected lenses: {', '.join(round_.lenses)}",
            field="lens",
        )
    if not 1 <= len(hunk_ids) <= MAX_HUNKS_PER_COVERAGE:
        raise LedgerError(
            f"cover 1..{MAX_HUNKS_PER_COVERAGE} hunks per call (got {len(hunk_ids)}); "
            "split the hunks into smaller batches with a note for each",
            field="digests",
        )
    if len(set(hunk_ids)) != len(hunk_ids):
        raise LedgerError(f"hunk ids must not repeat: {', '.join(hunk_ids)}", field="digests")
    if lens == DESIGN_LENS:
        return _validate_design_targets(round_, hunk_ids)
    by_id = {hunk.id: hunk for hunk in round_.hunks}
    unknown = [hunk_id for hunk_id in hunk_ids if hunk_id not in by_id]
    if unknown:
        open_ids = [hunk.id for hunk in round_.hunks if not hunk.carried]
        raise LedgerError(
            f"unknown hunk ids in round {round_.number}: {', '.join(unknown)}; "
            f"hunks to cover: {', '.join(open_ids) or 'none'}",
            field="digests",
        )
    carried = [hunk_id for hunk_id in hunk_ids if by_id[hunk_id].carried]
    if carried:
        raise LedgerError(
            f"{', '.join(carried)} already covered in an earlier round (carried over); "
            "cover only the remaining hunks",
            field="digests",
        )
    return tuple(by_id[hunk_id].digest for hunk_id in hunk_ids)


def _target_pattern(target: str) -> str:
    return rf"(?<!\w){re.escape(target)}(?!\w)"


def _extract_hunk_segments(hunk_ids: Sequence[str], note: str) -> dict[str, str]:
    spans: list[tuple[int, str]] = []
    for hid in hunk_ids:
        match = re.search(_target_pattern(hid), note)
        if match is not None:
            spans.append((match.start(), hid))
    spans.sort()
    segments: dict[str, str] = {}
    for index, (start, hid) in enumerate(spans):
        end = spans[index + 1][0] if index + 1 < len(spans) else len(note)
        segments[hid] = note[start:end].strip()
    return segments


def _normalize_note_segment(segment: str, target: str = "") -> str:
    cleaned = re.sub(_target_pattern(target), "", segment) if target else segment
    without_ids = re.sub(r"\bH[1-9]\d*\b", "", cleaned)
    return re.sub(r"\s+", " ", without_ids.strip().strip(";:,-.")).strip().lower()


def _validate_hunk_segment(hid: str, segment: str) -> str:
    without_id = re.sub(_target_pattern(hid), " ", segment)
    stripped_generic = _GENERIC_WORDS_RE.sub(" ", without_id)
    substantive = re.sub(r"[\W_]+", "", stripped_generic)
    if len(segment) < MIN_HUNK_NOTE_SEGMENT_CHARS or len(substantive) < MIN_SUBSTANTIVE_NOTE_CHARS:
        raise FieldError(
            "note",
            f"must have at least {MIN_COVERAGE_NOTE_CHARS} characters (>= {MIN_HUNK_NOTE_SEGMENT_CHARS} "
            f"chars and >= {MIN_SUBSTANTIVE_NOTE_CHARS} non-generic chars per hunk; {hid} has too little "
            f'concrete detail). Example: "{COVERAGE_NOTE_EXAMPLE}"',
        )
    if (
        _FILE_REFERENCE_RE.search(without_id) is None
        and _CONCRETE_COVERAGE_RE.search(without_id) is None
    ):
        raise FieldError(
            "note",
            f"for {hid} must include a concrete detail (file:line, number/literal, ->, or symbol name). "
            f'Example: "{COVERAGE_NOTE_EXAMPLE}"',
        )
    return _normalize_note_segment(segment, hid)


def _validate_coverage_note(
    ledger: Ledger, round_: Round, lens: str, hunk_ids: Sequence[str], note: str
) -> None:
    unnamed = [hunk_id for hunk_id in hunk_ids if not re.search(_target_pattern(hunk_id), note)]
    if unnamed:
        raise LedgerError(
            f"the coverage note must name every covered hunk and what was tried on it; missing: "
            f'{", ".join(unnamed)}. Example: "{COVERAGE_NOTE_EXAMPLE}"',
            field="note",
        )
    segments = _extract_hunk_segments(hunk_ids, note)
    norm_by_hunk: dict[str, str] = {}
    all_files = [path for path, _, _ in _round_files(round_)]
    for record in ledger.coverage:
        if record.round == round_.number and record.lens == lens:
            rec_ids = (
                [p for p in all_files if re.search(_target_pattern(p), record.note)]
                if lens == DESIGN_LENS
                else re.findall(r"\bH[1-9]\d*\b", record.note)
            )
            for hid, seg in _extract_hunk_segments(rec_ids, record.note).items():
                norm_by_hunk[hid] = _normalize_note_segment(seg, hid)
    for hid in hunk_ids:
        norm_by_hunk[hid] = _validate_hunk_segment(hid, segments[hid])
    counts: dict[str, int] = {}
    for body in norm_by_hunk.values():
        counts[body] = counts.get(body, 0) + 1
        if counts[body] > MAX_REUSED_NOTE_SEGMENTS:
            raise FieldError(
                "note",
                f"reuses the same note segment for more than {MAX_REUSED_NOTE_SEGMENTS} hunks in lens "
                f'`{lens}`; write a specific note per hunk. Example: "{COVERAGE_NOTE_EXAMPLE}"',
            )


def add_coverage(
    ledger: Ledger, lens: str, hunk_ids: Sequence[str], note: str
) -> tuple[Ledger, Coverage]:
    """Record that `lens` reviewed the given hunks (or files for `design`) of the latest round."""
    round_ = _latest_round(ledger)
    digests = _validate_coverage_target(round_, lens, hunk_ids)
    _validate_coverage_note(ledger, round_, lens, hunk_ids, note)
    if lens == SWEEP_LENS:
        _require_lenses_before_sweep(ledger, round_)
    record = Coverage(
        round=round_.number,
        lens=lens,
        digests=digests,
        note=note.strip(),
        recorded_at=_now(),
    )
    return replace(ledger, coverage=(*ledger.coverage, record)), record


def _require_lenses_before_sweep(ledger: Ledger, round_: Round) -> None:
    pending = [
        f"{lens} {','.join(ids)}"
        for lens, ids in uncovered_by_lens(ledger, round_).items()
        if lens != SWEEP_LENS and ids
    ]
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
    signal_id: str | None = None,
) -> tuple[Ledger, Finding]:
    """Record a finding against the latest round and the code it reviewed."""
    round_ = _latest_round(ledger)
    if signal_id is not None and all(sig.id != signal_id for sig in round_.signals):
        known = ", ".join(sig.id for sig in round_.signals) or "none in this round"
        raise LedgerError(
            f"unknown quality signal {signal_id!r} in round {round_.number}; known: {known}",
            field="signal_id",
        )
    now = _now()
    finding = Finding(
        id=_next_id("F", (item.id for item in ledger.findings)),
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
    sig = next((item for item in round_.signals if item.id == signal_id), None)
    if sig is None:
        known = ", ".join(item.id for item in round_.signals) or "none in this round"
        raise LedgerError(
            f"unknown quality signal {signal_id!r} in round {round_.number}; known: {known}",
            field="signal_id",
        )
    if sig.kind == "diff-size":
        raise LedgerError(
            f"{signal_id} (`diff-size`) cannot be acknowledged by the agent; shrink the change "
            "or hand the review to the user",
            field="signal_id",
        )
    stripped = reason.strip()
    _require_text(stripped, "reason", min_chars=MIN_SIGNAL_ACK_REASON_CHARS)
    if _CONCRETE_TOKEN_RE.search(stripped) is None and _FILE_REFERENCE_RE.search(stripped) is None:
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


def file_sha256(root: Path, rel_path: str) -> str | None:
    target = root / rel_path
    if not target.is_file():
        return None
    try:
        return hashlib.sha256(target.read_bytes()).hexdigest()
    except OSError:
        return None


def add_fact(
    ledger: Ledger,
    *,
    hunks: Sequence[str],
    source: SourceRef,
    text: str,
    file_digest: str,
    author_lens: str = "contracts",
) -> tuple[Ledger, Fact]:
    round_ = _latest_round(ledger)
    known_hunks = {h.id for h in round_.hunks}
    clean_hunks = tuple(dict.fromkeys(h.strip() for h in hunks if h.strip()))
    _require(bool(clean_hunks), "hunks", "must name at least one hunk id")
    for hid in clean_hunks:
        _require(hid in known_hunks, "hunks", f"{hid} is not in round {round_.number}")
    fact_id = _next_id("F-", (item.id for item in ledger.facts))
    fact = Fact(
        id=fact_id,
        hunks=clean_hunks,
        source=source,
        text=text.strip(),
        file_digest=file_digest,
        author_lens=author_lens,
    )
    return replace(ledger, facts=(*ledger.facts, fact)), fact


def valid_facts_for_hunk(ledger: Ledger, root: Path) -> dict[str, tuple[Fact, ...]]:
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
    target = _find(ledger, target_id)
    _require(
        bool(duplicate_ids),
        "duplicate_ids",
        "must name at least one duplicate finding to merge",
    )
    if target_id in duplicate_ids:
        raise LedgerError(f"cannot merge {target_id} into itself")
    if len(set(duplicate_ids)) != len(duplicate_ids):
        raise LedgerError(f"duplicate finding ids must not repeat: {', '.join(duplicate_ids)}")
    duplicates = [_find(ledger, dup_id) for dup_id in duplicate_ids]
    for item in (target, *duplicates):
        if item.verdict is not None or item.resolution is not None:
            state = item.resolution or item.verdict
            raise LedgerError(
                f"merge only works on findings without a verdict (before verification); "
                f"{item.id} already has a verdict or resolution ({state})"
            )
    now = _now()
    best_severity = min((item.severity for item in (target, *duplicates)), key=SEVERITIES.index)
    if scenario is not None:
        merged_scenario = scenario.strip()
    else:
        seen_raw = {
            re.sub(r"^\[F\d+/[^\]]+\]\s*", "", part.strip())
            for part in target.scenario.split("; ")
            if part.strip()
        }
        parts = [target.scenario]
        for dup in duplicates:
            if dup.scenario not in seen_raw:
                seen_raw.add(dup.scenario)
                parts.append(f"[{dup.id}/{dup.lens}] {dup.scenario}")
        merged_scenario = "; ".join(parts)
    if suggestion is not None:
        merged_suggestion = suggestion.strip()
    else:
        merged_suggestion = target.suggestion or next(
            (dup.suggestion for dup in duplicates if dup.suggestion), ""
        )
    updated_target = replace(
        target,
        severity=best_severity,
        title=target.title if title is None else title.strip(),
        scenario=merged_scenario,
        suggestion=merged_suggestion,
        updated_at=now,
    )
    evidence = f"{GROUPED_EVIDENCE_PREFIX}{target.id} ({target.path}:{target.line}): same root cause."
    updated_duplicates = {
        dup.id: replace(
            dup,
            verdict="dismissed",
            verdict_evidence=evidence,
            verdict_run=None,
            resolution=None,
            resolution_note="",
            updated_at=now,
        )
        for dup in duplicates
    }
    by_id = {updated_target.id: updated_target, **updated_duplicates}
    findings = tuple(by_id.get(item.id, item) for item in ledger.findings)
    return (
        replace(ledger, findings=findings),
        updated_target,
        tuple(updated_duplicates[dup.id] for dup in duplicates),
    )


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


def add_run(ledger: Ledger, run: Run) -> Ledger:
    """Append a verification run, assigning the next free id when `run.id` is empty."""
    if not run.id:
        run = replace(run, id=_next_id("R", (item.id for item in ledger.runs)))
    return replace(ledger, runs=(*ledger.runs, run))


def add_agent_event(ledger: Ledger, ev: AgentEvent) -> Ledger:
    """Append a subagent lifecycle event. Hook-only: never expose this to agents."""
    return replace(ledger, agents=(*ledger.agents, ev))


# --- trivial run detection ------------------------------------------------------------------


def is_trivial_run(argv: Sequence[str]) -> bool:
    """True for commands that prove nothing, such as `true`, `echo ok` or `sh -c 'exit 0'`."""
    tokens = _without_env_prefix(argv)
    if not tokens:
        return True
    program = _program_name(tokens[0])
    if program in _TRIVIAL_PROGRAMS:
        return True
    if _PYTHON_RE.fullmatch(program):
        code = _python_inline_code(tokens[1:])
        return code is not None and _is_trivial_python(code)
    script = _shell_script(program, tokens[1:])
    return script is not None and _is_trivial_script(script)


def _program_name(token: str) -> str:
    name = re.split(r"[\\/]", token)[-1].lower()
    return name.removesuffix(".exe")


def _without_env_prefix(argv: Sequence[str]) -> list[str]:
    tokens = list(argv)
    if tokens and _program_name(tokens[0]) == "env":
        tokens.pop(0)
        while tokens and ("=" in tokens[0] or tokens[0].startswith("-")):
            tokens.pop(0)
    return tokens


def _python_inline_code(args: Sequence[str]) -> str | None:
    """The program of `python [options] -c <code>`, or None when Python runs a file or module."""
    remaining = list(args)
    while remaining and remaining[0].startswith("-") and not remaining[0].startswith("--"):
        flags = remaining.pop(0)[1:]
        for index, flag in enumerate(flags):
            rest = flags[index + 1 :]
            if flag == "c":
                return rest or (remaining[0] if remaining else "")
            if flag == "m":
                return None
            if flag in "WX":  # these take a value: -Wignore or -W ignore
                if not rest and remaining:
                    remaining.pop(0)
                break
    return None


def _is_trivial_python(code: str) -> bool:
    """Code made only of `pass`, constants, `import sys` and exit calls with constant arguments."""
    try:
        statements = ast.parse(code).body
    except (SyntaxError, ValueError, RecursionError):
        return False  # Python would reject it, so it is not a trivial pass
    return all(_is_trivial_statement(statement) for statement in statements)


def _is_trivial_statement(statement: ast.stmt) -> bool:
    if isinstance(statement, ast.Pass):
        return True
    if isinstance(statement, ast.Import):
        return all(alias.name == "sys" and alias.asname is None for alias in statement.names)
    if not isinstance(statement, ast.Expr):
        return False
    value = statement.value
    if isinstance(value, ast.Constant):
        return True
    if not isinstance(value, ast.Call) or value.keywords:
        return False
    func = value.func
    exits = (isinstance(func, ast.Name) and func.id in _PYTHON_EXITS) or (
        isinstance(func, ast.Attribute)
        and func.attr == "exit"
        and isinstance(func.value, ast.Name)
        and func.value.id == "sys"
    )
    return exits and all(isinstance(arg, ast.Constant) for arg in value.args)


def _shell_script(program: str, args: Sequence[str]) -> str | None:
    """The inline script of a shell invocation such as `bash -lc '...'` or `cmd /d /c ...`."""
    if program in _SHELLS:
        return _posix_shell_script(args)
    if program == "cmd":
        for index, arg in enumerate(args):
            if arg.lower() in ("/c", "/k"):
                return " ".join(args[index + 1 :])
            if not arg.startswith("/"):
                return None
        return None
    if program in _POWERSHELLS:
        for index, arg in enumerate(args):
            name = arg[1:].lower() if arg[:1] in ("-", "/") else ""
            if name and "command".startswith(name):  # -Command, -c
                return " ".join(args[index + 1 :])
            if name and ("file".startswith(name) or "encodedcommand".startswith(name) or name == "ec"):
                return None
        return None
    return None


def _posix_shell_script(args: Sequence[str]) -> str | None:
    remaining = list(args)
    inline = False
    while remaining and remaining[0][:1] in ("-", "+") and remaining[0] not in ("-", "--"):
        flags = remaining.pop(0)
        if flags.startswith("--"):
            continue  # long options such as --norc or --login take no value we need to skip
        inline = inline or "c" in flags
        if ("o" in flags or "O" in flags) and remaining:
            remaining.pop(0)  # -o pipefail, -O extglob
    if remaining and remaining[0] == "--":
        remaining.pop(0)
    return remaining[0] if inline and remaining else None


def _is_trivial_script(script: str) -> bool:
    lexer = shlex.shlex(script, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return False  # unbalanced quotes: the shell would reject it, so it is not a trivial pass
    segments: list[list[str]] = [[]]
    for token in tokens:
        if token in _SHELL_SEPARATORS:
            segments.append([])
        else:
            segments[-1].append(token)
    return all(is_trivial_run(segment) for segment in segments if segment)


# --- gate evaluation ------------------------------------------------------------------------


@dataclass(frozen=True)
class GateIssue:
    code: IssueCode
    message: str


@dataclass(frozen=True)
class GateReport:
    issues: tuple[GateIssue, ...]
    warnings: tuple[str, ...]
    summary: str
    review_only: bool = False
    self_reviewed: bool = False

    @property
    def passed(self) -> bool:
        return not self.issues


@dataclass(frozen=True)
class GatePolicy:
    tier: Tier
    require_isolation: bool = False
    reviewer_agent_markers: tuple[str, ...] = ("craftsman-reviewer",)
    max_rounds: int = 5
    isolation_evidence: IsolationEvidence = "none"

    def __post_init__(self) -> None:
        _require_choice(self.tier, TIERS, "tier")
        _require_choice(self.isolation_evidence, ISOLATION_EVIDENCES, "isolation_evidence")
        _require(self.max_rounds >= 1, "max_rounds", "must be >= 1")
        _require(
            bool(self.reviewer_agent_markers) and all(m.strip() for m in self.reviewer_agent_markers),
            "reviewer_agent_markers",
            "must contain at least one non-blank marker",
        )
        if self.require_isolation and self.isolation_evidence == "none":
            object.__setattr__(self, "isolation_evidence", "hooks")
        elif self.isolation_evidence != "none" and self.tier != "quick":
            object.__setattr__(self, "require_isolation", True)


def evaluate(
    ledger: Ledger, snap: DiffSnapshot, policy: GatePolicy, *, review_only: bool = False
) -> GateReport:
    """Decide whether the evidence in `ledger` covers the current diff `snap`."""
    if snap.is_empty:
        summary = f"Nothing to review: the work tree matches base {snap.base_sha[:12]}."
        return GateReport(
            () if review_only else tuple(_unresolved_issues(ledger, snap)),
            (),
            summary,
            review_only=review_only,
        )
    latest = ledger.rounds[-1] if ledger.rounds else None
    fix_and_test = (
        ()
        if review_only
        else (
            *_unresolved_issues(ledger, snap),
            *_passing_run_issues(ledger, snap, policy),
            *_failing_run_issues(ledger),
        )
    )
    issues = [
        *_round_issues(ledger, snap),
        *(_tier_issues(latest, policy) if latest else ()),
        *(_coverage_issues(ledger, latest) if latest else ()),
        *(_signal_issues(ledger, latest) if latest else ()),
        *_verdict_issues(ledger),
        *fix_and_test,
        *(_isolation_issues(ledger, latest, policy) if latest else ()),
        *_round_cap_issues(ledger, policy),
    ]
    self_reviewed = bool(
        latest is not None
        and policy.tier != "quick"
        and not policy.require_isolation
        and not _has_isolated_reviewer_stop(ledger, latest, policy)
    )
    return GateReport(
        tuple(issues),
        _warnings(ledger, snap, policy, review_only=review_only),
        _summary(ledger, snap),
        review_only=review_only,
        self_reviewed=self_reviewed,
    )


def _acknowledged_signal_keys(ledger: Ledger) -> set[tuple[str, str, str]]:
    by_round_id = {
        (round_.number, sig.id): (sig.kind, sig.file, sig.detail)
        for round_ in ledger.rounds
        for sig in round_.signals
    }
    return {
        key
        for ack in ledger.signal_acks
        if (key := by_round_id.get((ack.round, ack.signal_id))) is not None
    }


def _is_signal_resolved(
    ledger: Ledger, latest: Round, sig: SignalRef, ack_keys: set[tuple[str, str, str]]
) -> bool:
    if sig.kind == "diff-size":
        return False
    if (sig.kind, sig.file, sig.detail) in ack_keys:
        return True
    return any(
        f.round == latest.number and f.signal_id == sig.id and f.verdict != "dismissed"
        for f in ledger.findings
    )


def has_unresolved_diff_size(ledger: Ledger) -> bool:
    """Return True when the latest round recorded an unresolved `diff-size` quality signal."""
    return bool(ledger.rounds) and any(
        sig.kind == "diff-size" for sig in ledger.rounds[-1].signals
    )


def _signal_issues(ledger: Ledger, latest: Round) -> list[GateIssue]:
    ack_keys = _acknowledged_signal_keys(ledger)
    issues: list[GateIssue] = []
    for sig in latest.signals:
        if _is_signal_resolved(ledger, latest, sig, ack_keys):
            continue
        action = (
            "shrink the change below the budget or hand the review to the user"
            if sig.kind == "diff-size"
            else f"record a finding with `craftsman review add --signal {sig.id} ...` or acknowledge it "
            f'with `craftsman review ack {sig.id} --reason "<concrete reason, >= 40 chars>"`'
        )
        issues.append(
            GateIssue(
                "unresolved-signal",
                f"Quality signal {sig.id} ({sig.kind} at {sig.file}:{sig.line} — {sig.detail}) "
                f"is unresolved; {action}.",
            )
        )
    return issues


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
                "and select lenses.",
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


def _tier_issues(latest: Round, policy: GatePolicy) -> list[GateIssue]:
    if TIERS.index(latest.tier) >= TIERS.index(policy.tier):
        return []
    return [
        GateIssue(
            "tier-too-low",
            f"Round {latest.number} was prepared for the {latest.tier} tier but this task needs "
            f"{policy.tier}; run `craftsman review prepare --tier {policy.tier}`.",
        )
    ]


def _reopened_in(ledger: Ledger, upto: int) -> dict[str, int]:
    """Per hunk (and file) digest, the latest round up to `upto` that reopened it."""
    latest: dict[str, int] = {}
    for round_ in ledger.rounds[:upto]:
        for hunk in round_.hunks:
            if hunk.reopened:
                latest[hunk.digest] = round_.number
                latest[_file_digest(round_.hunks, hunk.path)] = round_.number
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
    done = datetime.min.replace(tzinfo=timezone.utc)
    for lens in round_.lenses:
        if lens == SWEEP_LENS:
            continue
        units = (
            [(digest, carried) for _, digest, carried in _round_files(round_)]
            if lens == DESIGN_LENS
            else [(hunk.digest, hunk.carried) for hunk in round_.hunks]
        )
        for digest, carried in units:
            if carried:
                continue
            at = first.get((lens, digest))
            if at is None:
                return None
            done = max(done, at)
    return done


def _counting_sweeps(ledger: Ledger) -> list[Coverage]:
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
        for record in _counting_sweeps(ledger)
        for digest in record.digests
        if record.round >= reopened.get(digest, 1)
    }
    return set(_lens_coverage(ledger, len(ledger.rounds))) | swept


def uncovered_by_lens(ledger: Ledger, round_: Round) -> dict[str, list[str]]:
    """Hunk ids (or file paths for `design`) of `round_` that each of its lenses still has to cover."""
    lenses = _lens_coverage(ledger, round_.number)
    swept = {
        digest
        for record in _counting_sweeps(ledger)
        if record.round == round_.number
        for digest in record.digests
    }
    missing: dict[str, list[str]] = {lens: [] for lens in round_.lenses}
    for lens in round_.lenses:
        if lens == DESIGN_LENS:
            for path, digest, carried in _round_files(round_):
                if not carried and (DESIGN_LENS, digest) not in lenses:
                    missing[lens].append(path)
            continue
        for hunk in round_.hunks:
            covered = hunk.digest in swept if lens == SWEEP_LENS else (lens, hunk.digest) in lenses
            if not hunk.carried and not covered:
                missing[lens].append(hunk.id)
    return missing


def _coverage_issues(ledger: Ledger, latest: Round) -> list[GateIssue]:
    missing = uncovered_by_lens(ledger, latest)
    sweep_missing = missing.pop(SWEEP_LENS, [])
    issues = [_uncovered_issue(lens, ids, latest) for lens, ids in missing.items() if ids]
    premature = _premature_sweep_ids(ledger, latest, sweep_missing)
    if premature:
        batch = ",".join(premature[:MAX_HUNKS_PER_COVERAGE])
        issues.append(
            GateIssue(
                "sweep-too-early",
                f"The final sweep of {', '.join(premature)} was recorded before the other lenses "
                f"of round {latest.number} were complete, so it does not count; once every other "
                f"lens is covered, repeat it: "
                f'`craftsman review cover --lens {SWEEP_LENS} --hunks {batch} --note "..."`.',
            )
        )
    remaining = [hunk_id for hunk_id in sweep_missing if hunk_id not in premature]
    if remaining and not any(missing.values()):
        issues.append(_uncovered_issue(SWEEP_LENS, remaining, latest))
    return issues


def _uncovered_issue(lens: str, missing: Sequence[str], round_: Round) -> GateIssue:
    batch = ",".join(missing[:MAX_HUNKS_PER_COVERAGE])
    extra = len(missing) - MAX_HUNKS_PER_COVERAGE
    more = f" (+{extra} more)" if extra > 0 else ""
    unit = "file(s)" if lens == DESIGN_LENS else "hunk(s)"
    flag = "--files" if lens == DESIGN_LENS else "--hunks"
    return GateIssue(
        "uncovered",
        f"Lens `{lens}` has {len(missing)} uncovered {unit} in round {round_.number}: "
        f"{batch}{more}; review them and run "
        f'`craftsman review cover --lens {lens} {flag} {batch} --note "..."`.',
    )


def _premature_sweep_ids(ledger: Ledger, round_: Round, sweep_missing: Sequence[str]) -> list[str]:
    """Still-uncovered hunk ids whose only sweep coverage in `round_` came too early."""
    counting = _counting_sweeps(ledger)
    digests = {
        digest
        for record in ledger.coverage
        if record.round == round_.number and record.lens == SWEEP_LENS and record not in counting
        for digest in record.digests
    }
    return [hunk.id for hunk in round_.hunks if hunk.id in sweep_missing and hunk.digest in digests]


def _verdict_issues(ledger: Ledger) -> list[GateIssue]:
    return [
        GateIssue(
            "no-verdict",
            f"Finding {finding.id} ({finding.severity}: {finding.title}) has no verdict; verify it "
            f"independently and run `craftsman review verdict {finding.id} "
            '--as proven|probable|question|dismissed --evidence "..."`.',
        )
        for finding in ledger.findings
        if finding.verdict is None
    ]


def _unresolved_issues(ledger: Ledger, snap: DiffSnapshot) -> list[GateIssue]:
    issues: list[GateIssue] = []
    for finding in ledger.findings:
        label = f"Finding {finding.id} ({finding.severity}: {finding.title})"
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


def _passing_run_issues(ledger: Ledger, snap: DiffSnapshot, policy: GatePolicy) -> list[GateIssue]:
    passing = [
        run
        for run in _counting_runs(ledger, _PASSING_KINDS)
        if run.counts_as_passing and run.fingerprint == snap.fingerprint
    ]
    issues: list[GateIssue] = []
    if not passing:
        issues.append(
            GateIssue(
                "no-passing-run",
                "No passing test or e2e run at the current code; "
                "run `craftsman run --kind test -- <your test command>`.",
            )
        )
    if policy.tier == "deep" and not any(run.kind == "e2e" for run in passing):
        issues.append(
            GateIssue(
                "no-e2e-run",
                "The deep tier needs a passing end-to-end run at the current code; run "
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


def _has_isolated_reviewer_stop(ledger: Ledger, latest: Round, policy: GatePolicy) -> bool:
    if all(hunk.carried for hunk in latest.hunks):
        return True
    prepared_at = parse_timestamp(latest.prepared_at)
    markers = [marker.lower() for marker in policy.reviewer_agent_markers]
    return any(
        event.event == "stop"
        and any(marker in event.agent.lower() for marker in markers)
        and parse_timestamp(event.at) >= prepared_at
        for event in ledger.agents
    )


def _isolation_issues(ledger: Ledger, latest: Round, policy: GatePolicy) -> list[GateIssue]:
    if not policy.require_isolation or policy.tier == "quick":
        return []
    if _has_isolated_reviewer_stop(ledger, latest, policy):
        return []
    return [
        GateIssue(
            "no-isolation",
            f"No independent review finished since round {latest.number} was prepared; spawn the "
            f"{policy.reviewer_agent_markers[0]} subagent for round {latest.number}.",
        )
    ]


def _round_cap_issues(ledger: Ledger, policy: GatePolicy) -> list[GateIssue]:
    # Re-preparing the same diff for a stricter tier is not another iteration.
    diffs = len({round_.fingerprint for round_ in ledger.rounds})
    if diffs <= policy.max_rounds:
        return []
    return [
        GateIssue(
            "round-cap",
            f"{diffs} review rounds of different diffs exceed the cap of {policy.max_rounds}; stop "
            "iterating and report the remaining issues to the user.",
        )
    ]


def _coverage_anomaly_warnings(ledger: Ledger) -> list[str]:
    warnings: list[str] = []
    if not ledger.rounds:
        return warnings
    latest = ledger.rounds[-1]
    hunk_lenses = [lens for lens in latest.lenses if lens != DESIGN_LENS]
    if len(latest.hunks) > 30 and len(hunk_lenses) >= 2:
        counts = {
            lens: sum(1 for r in ledger.coverage if r.round == latest.number and r.lens == lens)
            for lens in hunk_lenses
        }
        if all(c > 0 for c in counts.values()) and len(set(counts.values())) == 1:
            count_val = next(iter(counts.values()))
            warnings.append(
                f"Process anomaly: all lenses covered with an identical entry count ({count_val}) "
                f"across {len(latest.hunks)} hunks (> 30)."
            )
    for lens in hunk_lenses:
        records = [r for r in ledger.coverage if r.round == latest.number and r.lens == lens]
        digests = sum(len(r.digests) for r in records)
        if digests > 20 and records:
            times = [parse_timestamp(r.recorded_at) for r in records]
            if (max(times) - min(times)).total_seconds() < 120:
                warnings.append(
                    f"Process anomaly: {digests} hunks covered in lens `{lens}` in under 2 minutes."
                )
    return warnings


def _process_anomaly_warnings(ledger: Ledger) -> list[str]:
    warnings: list[str] = []
    if len(ledger.findings) >= 5 and all(f.verdict is not None for f in ledger.findings):
        if sum(1 for f in ledger.findings if f.verdict == "dismissed") == 0:
            warnings.append(
                f"Process anomaly: {len(ledger.findings)} findings and 0 dismissed "
                "(all findings confirmed — typical of self-review)."
            )
    warnings.extend(_coverage_anomaly_warnings(ledger))
    test_runs = [r for r in ledger.runs if r.kind in ("test", "e2e")]
    for idx, failed in enumerate(test_runs):
        if failed.exit_code == 0:
            continue
        base_cmd = tuple(_without_env_prefix(failed.argv))
        if not base_cmd:
            continue
        for later in test_runs[idx + 1 :]:
            if (
                later.fingerprint == failed.fingerprint
                and tuple(_without_env_prefix(later.argv)) == base_cmd
                and tuple(later.argv) != tuple(failed.argv)
            ):
                warnings.append(
                    f"Process anomaly: {failed.kind} run {failed.id} failed and was repeated as "
                    f"{later.id} with a different environment without a code change."
                )
                break
    return warnings


def _warnings(
    ledger: Ledger, snap: DiffSnapshot, policy: GatePolicy, *, review_only: bool = False
) -> tuple[str, ...]:
    warnings: list[str] = []
    if review_only:
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
    changed_lines = snap.added + snap.deleted
    if policy.tier == "quick" and changed_lines > QUICK_TIER_MAX_CHANGED_LINES:
        warnings.append(
            f"The quick tier reviews {changed_lines} changed lines "
            f"(> {QUICK_TIER_MAX_CHANGED_LINES}); consider the standard tier."
        )
    warnings.extend(
        f"{run.id} (`{run.command}`) is trivial and does not count as verification."
        for run in ledger.runs
        if is_trivial_run(run.argv)
    )
    warnings.extend(_process_anomaly_warnings(ledger))
    return tuple(warnings)


def _summary(ledger: Ledger, snap: DiffSnapshot) -> str:
    if not ledger.rounds:
        round_line = "Round: none prepared yet."
        coverage_line = "Coverage: n/a."
    else:
        latest = ledger.rounds[-1]
        freshness = "current" if latest.fingerprint == snap.fingerprint else "stale"
        round_line = (
            f"Round {latest.number} ({latest.tier}, {len(latest.lenses)} lenses, {freshness}): "
            f"{len(latest.hunks)} hunks in {len(snap.files)} files, +{snap.added}/-{snap.deleted} lines."
        )
        coverage_line = _coverage_summary(ledger, latest)
    return "\n".join((round_line, coverage_line, _findings_summary(ledger), _runs_summary(ledger, snap)))


def _coverage_summary(ledger: Ledger, latest: Round) -> str:
    missing = uncovered_by_lens(ledger, latest)
    open_hunks = sum(not hunk.carried for hunk in latest.hunks)
    open_files = sum(not carried for _, _, carried in _round_files(latest))
    total = sum(open_files if lens == DESIGN_LENS else open_hunks for lens in latest.lenses)
    done = total - sum(len(ids) for ids in missing.values())
    carried = len(latest.hunks) - open_hunks
    line = f"Coverage: {done}/{total} lens-hunk pairs; {carried} hunk(s) carried from earlier rounds."
    lenses_pending = any(ids for lens, ids in missing.items() if lens != SWEEP_LENS)
    if SWEEP_LENS in latest.lenses and lenses_pending:
        line += " Sweep pending (runs after every other lens)."
    return line


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


def render_report(report: GateReport) -> str:
    """Render a gate report as a compact Markdown block with numbered next actions."""
    if report.review_only:
        status = (
            "COMPLETE (review-only)"
            if report.passed
            else f"INCOMPLETE ({len(report.issues)} open items)"
        )
        header = f"**Craftsman review: {status}**"
    else:
        status = "PASSED" if report.passed else f"BLOCKED ({len(report.issues)} open items)"
        header = f"**Craftsman gate: {status}**"
    lines = [header]
    if report.self_reviewed:
        lines.append(f"**{SELF_REVIEWED_BANNER}**")
    lines += ["", report.summary]
    if report.issues:
        lines += ["", "**Next actions**", *render_next_actions(report.issues)]
    if report.warnings:
        lines += ["", "**Warnings**", *(f"- {warning}" for warning in report.warnings)]
    return "\n".join(lines)
