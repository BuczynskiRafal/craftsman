"""Evidence ledger records: the typed, validated data the review ledger is made of.

Every record validates itself on construction and round-trips through JSON with `to_json` and
`from_json`; parse errors name the offending (possibly nested) field.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, TypeVar, get_args

from craftsman.diffscan import is_relative_posix_path
from craftsman.lenses import LENS_IDS

Severity = Literal["blocker", "major", "minor", "nit"]
Verdict = Literal["proven", "probable", "question", "dismissed"]
Resolution = Literal["fixed", "accepted"]
RunKind = Literal["test", "e2e", "repro", "lint", "build", "other"]
AgentEventKind = Literal["start", "stop"]
AgentRole = Literal["reviewer", "verifier"]
IsolationEvidence = Literal["hooks", "transcript", "none"]

SEVERITIES: tuple[Severity, ...] = get_args(Severity)
VERDICTS: tuple[Verdict, ...] = get_args(Verdict)
RESOLUTIONS: tuple[Resolution, ...] = get_args(Resolution)
RUN_KINDS: tuple[RunKind, ...] = get_args(RunKind)
AGENT_EVENT_KINDS: tuple[AgentEventKind, ...] = get_args(AgentEventKind)
ISOLATION_EVIDENCES: tuple[IsolationEvidence, ...] = get_args(IsolationEvidence)
REVIEWER_AGENT = "craftsman-reviewer"
VERIFIER_AGENT = "craftsman-verifier"
ORCHESTRATOR_AGENT = "craftsman"
AGENT_ROLES: dict[str, AgentRole] = {REVIEWER_AGENT: "reviewer", VERIFIER_AGENT: "verifier"}
CRAFTSMAN_AGENTS = frozenset({ORCHESTRATOR_AGENT, *AGENT_ROLES})
# Claude Code names a plugin's agents `<plugin>:<agent>`, so the plugin's reviewer reports itself
# as `craftsman:craftsman-reviewer`.
PLUGIN_AGENT_PREFIX = "craftsman:"

SCHEMA_VERSION = 3
LEDGER_FILENAME = "ledger.json"
OUTPUT_TAIL_CHARS = 4000
MIN_TITLE_CHARS = 8
MAX_TITLE_CHARS = 160
MIN_SCENARIO_CHARS = 20
MIN_EVIDENCE_CHARS = 20
SHORT_COMMAND_CHARS = 60
MIN_RESOLUTION_NOTE_CHARS = 20
MIN_WAIVER_REASON_CHARS = 20
MIN_COVERAGE_NOTE_CHARS = 20
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
PROBABLE_EVIDENCE_EXAMPLE = (
    "no-repro: needs a live Postgres; api.py:40 passes None -> db.py:88 calls None.execute()"
)
STATIC_EVIDENCE_EXAMPLE = (
    "static: cart.py:3 divides sum(prices) by len(items) -> total([10, 20]) returns 15.0, "
    "callers checkout.py:40 expect the sum"
)
QUESTION_EVIDENCE_EXAMPLE = (
    "intent: the task says 'retry failed uploads three times'; uploader.py:58 retries forever, "
    "which keeps the worker busy when the server is down for good"
)
DISMISSED_EVIDENCE_EXAMPLE = "guarded by cli.py:19, which rejects an empty list before total() runs"
_ID_PATTERNS = {prefix: re.compile(rf"{prefix}[1-9]\d*") for prefix in ("H", "F", "R", "S")}

T = TypeVar("T")
S = TypeVar("S", bound=str)
E = TypeVar("E", bound=Enum)


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


@dataclass(frozen=True)
class EntryProblem:
    """One rejected item inside a `--from-json` batch."""

    index: int
    label: str | None
    field: str
    problem: str


class BatchRejected(LedgerError):
    """Raised when one or more `--from-json` entries fail validation."""

    def __init__(
        self,
        command: str,
        problems: tuple[EntryProblem, ...],
        total: int,
    ) -> None:
        if not problems:
            raise ValueError("BatchRejected requires at least one EntryProblem")
        noun = "entry" if total == 1 else "entries"
        super().__init__(
            f"{command}: {len({item.index for item in problems})} of {total} {noun} rejected; "
            "nothing was recorded."
        )
        self.command = command
        self.problems = problems
        self.total = total


def format_timestamp(moment: datetime) -> str:
    """UTC ISO-8601 with millisecond precision and a trailing `Z`, as stored in the ledger."""
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_timestamp(value: str) -> datetime:
    """Parse an ISO-8601 timestamp that carries a UTC offset or a trailing `Z`."""
    parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp {value!r} has no time zone")
    return parsed


# --- field validation -----------------------------------------------------------------------


def file_reference_in(text: str) -> bool:
    """True when `text` cites a location as file:line, e.g. `app.py:12` or `Makefile:3`."""
    return _FILE_REFERENCE_RE.search(text) is not None


def next_id(prefix: str, existing: Iterable[str]) -> str:
    """The id after the highest of `existing`, e.g. F3 after F1 and F2."""
    return f"{prefix}{max((int(item[len(prefix) :]) for item in existing), default=0) + 1}"


class MultiFieldError(LedgerError):
    """Every problem found in one record, reported together instead of one at a time."""

    def __init__(self, errors: Sequence[LedgerError]) -> None:
        seen: set[str] = set()
        kept: list[LedgerError] = []
        for error in errors:  # one problem per field: later ones are usually echoes of the first
            if error.field and error.field in seen:
                continue
            seen.add(error.field)
            kept.append(error)
        super().__init__("; ".join(str(error) for error in kept))
        self.errors = tuple(kept)


_problem_sink: ContextVar[list[LedgerError] | None] = ContextVar("problem_sink", default=None)


@contextmanager
def collecting_problems() -> Iterator[list[LedgerError]]:
    """Inside the block a failed record check is collected, not raised, so a record reports all its problems.

    The records built inside may be invalid; the caller discards them when the list is not empty.
    """
    sink: list[LedgerError] = []
    token = _problem_sink.set(sink)
    try:
        yield sink
    finally:
        _problem_sink.reset(token)


def _require(condition: bool, field: str, problem: str) -> None:
    if condition:
        return
    error = FieldError(field, problem)
    sink = _problem_sink.get()
    if sink is None:
        raise error
    sink.append(error)


def check_coverage_note(note: str) -> None:
    """Fail (or collect) when `note` is too short to say what was examined."""
    _require_text(note, "note", min_chars=MIN_COVERAGE_NOTE_CHARS)


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


def _require_unique(ids: Sequence[str], field: str) -> None:
    _require(len(set(ids)) == len(ids), field, "must not repeat an id")


def _require_timestamp(value: str, field: str) -> None:
    try:
        parse_timestamp(value)
    except ValueError:
        raise FieldError(
            field, f"must be an ISO-8601 timestamp with a time zone (got {value!r})"
        ) from None


def as_choice(value: str, choices: Iterable[S]) -> S | None:
    """`value` narrowed to the matching `Literal` choice, or None when it is not one of them."""
    return next((choice for choice in choices if value == choice), None)


class Fields:
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

    def _absent(self, key: str) -> bool:
        """True, and the key counts as read, when the optional `key` is left out."""
        if key in self._raw:
            return False
        self._seen.add(key)
        return True

    def text(self, key: str) -> str:
        value: str = self._value(key, str, "a string")
        return value

    def optional_text(self, key: str) -> str | None:
        value: str | None = self._value(key, (str, type(None)), "a string or null")
        return value

    def text_or(self, key: str, default: str) -> str:
        """A string input field; `default` when it is left out."""
        return default if self._absent(key) else self.text(key)

    def text_if_present(self, key: str) -> str | None:
        """A string input field; None when it is left out."""
        return None if self._absent(key) else self.text(key)

    def integer(self, key: str) -> int:
        value: int = self._value(key, int, "an integer")
        return value

    def optional_integer(self, key: str) -> int | None:
        value: int | None = self._value(key, (int, type(None)), "an integer or null")
        return value

    def number(self, key: str) -> float:
        return float(self._value(key, (int, float), "a number"))

    def flag(self, key: str) -> bool:
        value: bool = self._value(key, bool, "true or false")
        return value

    def choice(self, key: str, choices: tuple[S, ...]) -> S:
        value = self.text(key)
        chosen = as_choice(value, choices)
        if chosen is None:
            raise FieldError(key, f"must be one of {', '.join(choices)} (got {value!r})")
        return chosen

    def optional_choice(self, key: str, choices: tuple[S, ...]) -> S | None:
        if key in self._raw and self._raw[key] is None:
            self._seen.add(key)
            return None
        return self.choice(key, choices)

    def member(self, key: str, enum: type[E]) -> E:
        value = self.text(key)
        try:
            return enum(value)
        except ValueError:
            names = ", ".join(str(item.value) for item in enum)
            raise FieldError(key, f"must be one of {names} (got {value!r})") from None

    def texts(self, key: str) -> tuple[str, ...]:
        values: list[Any] = self._value(key, list, "a list of strings")
        for index, value in enumerate(values):
            _require(isinstance(value, str), f"{key}[{index}]", "must be a string")
        return tuple(values)

    def optional_texts(self, key: str) -> tuple[str, ...] | None:
        if key in self._raw and self._raw[key] is None:
            self._seen.add(key)
            return None
        values: list[Any] = self._value(key, (list, type(None)), "a list of strings or null")
        assert isinstance(values, list)
        for index, value in enumerate(values):
            _require(isinstance(value, str), f"{key}[{index}]", "must be a string")
        return tuple(values)

    def optional_texts_if_present(self, key: str) -> tuple[str, ...] | None:
        """A list of strings; None when it is left out or null."""
        return None if self._absent(key) else self.optional_texts(key)

    def texts_or_empty(self, key: str) -> tuple[str, ...]:
        """A list of strings; empty when it is left out."""
        return () if self._absent(key) else self.texts(key)

    def records(self, key: str, parse: Callable[[Any], T]) -> tuple[T, ...]:
        values: list[Any] = self._value(key, list, "a list")
        parsed: list[T] = []
        for index, value in enumerate(values):
            try:
                parsed.append(parse(value))
            except FieldError as exc:
                raise exc.within(f"{key}[{index}]") from None
        return tuple(parsed)

    def record(self, key: str, parse: Callable[[Any], T]) -> T:
        value: dict[str, Any] = self._value(key, dict, "an object")
        try:
            return parse(value)
        except FieldError as exc:
            raise exc.within(key) from None

    def optional_record(self, key: str, parse: Callable[[Any], T]) -> T | None:
        if key in self._raw and self._raw[key] is None:
            self._seen.add(key)
            return None
        return self.record(key, parse)

    def unknown_fields(self) -> list[str]:
        """The fields of the JSON object that were never read."""
        return sorted(set(self._raw) - self._seen)

    def optional_record_if_present(self, key: str, parse: Callable[[Any], T]) -> T | None:
        """The record at `key`; None when the key is left out or null."""
        return None if self._absent(key) else self.optional_record(key, parse)

    def reject_unknown(self) -> None:
        """Fail on any field of the JSON object that was never read."""
        unknown = self.unknown_fields()
        if unknown:
            raise FieldError(unknown[0], "is not a known field")

    def done(self, record: T) -> T:
        """Return `record` once every field of the JSON object has been consumed."""
        self.reject_unknown()
        return record


def _record_json(record: Any) -> dict[str, Any]:
    return {field.name: _json_value(getattr(record, field.name)) for field in fields(record)}


def _json_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    if is_dataclass(value) and not isinstance(value, type):
        to_json = getattr(value, "to_json", None)  # a record may shape its own JSON
        return to_json() if callable(to_json) else _record_json(value)
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
        f = Fields(raw)
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


class SignalKind(str, Enum):
    """What a deterministic quality signal flags in the changed code."""

    LONG_FUNCTION = "long-function"
    LARGE_MODULE = "large-module"
    TEST_NAME = "test-name"
    DUPLICATE_LOGIC = "duplicate-logic"
    DICT_DOMAIN = "dict-domain"
    DIFF_SIZE = "diff-size"
    PARSE_ERROR = "parse-error"


@dataclass(frozen=True)
class QualitySignal:
    """A quality hint computed for a round and shown to the reviewer; it never blocks the gate."""

    id: str
    kind: SignalKind
    file: str
    line: int
    detail: str

    def __post_init__(self) -> None:
        _require_id(self.id, "S", "id")
        _require_text(self.file, "file")
        _require(self.line >= 1, "line", "must be >= 1")
        _require_text(self.detail, "detail")

    def to_json(self) -> dict[str, Any]:
        return _record_json(self)

    @classmethod
    def from_json(cls, raw: Any) -> QualitySignal:
        f = Fields(raw)
        return f.done(
            cls(
                id=f.text("id"),
                kind=f.member("kind", SignalKind),
                file=f.text("file"),
                line=f.integer("line"),
                detail=f.text("detail"),
            )
        )


def _require_scope(scope: tuple[str, ...] | None) -> None:
    if scope is None:
        return
    _require(bool(scope), "scope", "must list at least one file path (or be null)")
    for index, path in enumerate(scope):
        _require(
            is_relative_posix_path(path),
            f"scope[{index}]",
            f"must be a normalized relative POSIX file path (got {path!r})",
        )
    _require(len(set(scope)) == len(scope), "scope", "must not repeat a path")
    _require(tuple(sorted(scope)) == scope, "scope", "must be sorted")


@dataclass(frozen=True)
class Round:
    number: int
    fingerprint: str
    base_sha: str
    deep: bool
    prepared_at: str
    hunks: tuple[HunkRef, ...]
    packet_path: str
    signals: tuple[QualitySignal, ...]
    scope: tuple[str, ...] | None = None
    out_of_scope: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require(self.number >= 1, "number", "must be >= 1")
        _require_text(self.fingerprint, "fingerprint")
        _require_text(self.base_sha, "base_sha")
        _require_timestamp(self.prepared_at, "prepared_at")
        hunk_ids = [hunk.id for hunk in self.hunks]
        _require(len(set(hunk_ids)) == len(hunk_ids), "hunks", "must not repeat a hunk id")
        sig_ids = [sig.id for sig in self.signals]
        _require(len(set(sig_ids)) == len(sig_ids), "signals", "must not repeat a signal id")
        _require_scope(self.scope)
        for index, path in enumerate(self.out_of_scope):
            _require_text(path, f"out_of_scope[{index}]")

    @property
    def open_hunk_ids(self) -> tuple[str, ...]:
        return tuple(hunk.id for hunk in self.hunks if not hunk.carried)

    def to_json(self) -> dict[str, Any]:
        return _record_json(self)

    @classmethod
    def from_json(cls, raw: Any) -> Round:
        f = Fields(raw)
        return f.done(
            cls(
                number=f.integer("number"),
                fingerprint=f.text("fingerprint"),
                base_sha=f.text("base_sha"),
                deep=f.flag("deep"),
                prepared_at=f.text("prepared_at"),
                hunks=f.records("hunks", HunkRef.from_json),
                packet_path=f.text("packet_path"),
                signals=f.records("signals", QualitySignal.from_json),
                scope=f.optional_texts("scope"),
                out_of_scope=f.texts_or_empty("out_of_scope"),
            )
        )


@dataclass(frozen=True)
class Coverage:
    round: int
    digests: tuple[str, ...]
    note: str
    recorded_at: str

    def __post_init__(self) -> None:
        _require(self.round >= 1, "round", "must be >= 1")
        _require(bool(self.digests), "digests", "must list at least one hunk digest")
        check_coverage_note(self.note)
        _require_timestamp(self.recorded_at, "recorded_at")

    def to_json(self) -> dict[str, Any]:
        return _record_json(self)

    @classmethod
    def from_json(cls, raw: Any) -> Coverage:
        f = Fields(raw)
        return f.done(
            cls(
                round=f.integer("round"),
                digests=f.texts("digests"),
                note=f.text("note"),
                recorded_at=f.text("recorded_at"),
            )
        )


_SEVERITY_MARKER_RE = re.compile(r"\s*\[severity:\s*[a-z]+\]\s*", re.IGNORECASE)


def _check_verdict_evidence(verdict: Verdict, evidence: str, run_id: str | None) -> None:
    """Each verdict needs evidence a reader can check; see the module constants for examples.

    A leading `[severity: <new>]` marker, which a verifier uses to correct the severity, comes
    before the required prefix and is not part of the check.
    """
    marker = _SEVERITY_MARKER_RE.match(evidence)
    if marker is not None:
        evidence = evidence[marker.end() :]
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
            valid = len(reason) >= MIN_EVIDENCE_CHARS and file_reference_in(reason)
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
            and file_reference_in(reason),
            "verdict_evidence",
            f"for 'question' must start with '{_INTENT_PREFIX}' followed by at least "
            f"{MIN_EVIDENCE_CHARS} characters naming the intent source and citing file:line in "
            f'the code, e.g. "{QUESTION_EVIDENCE_EXAMPLE}"',
        )
    else:
        _require(
            run_id is not None or file_reference_in(evidence),
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

    def __post_init__(self) -> None:
        _require_id(self.id, "F", "id")
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
        f = Fields(raw)
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
        f = Fields(raw)
        return f.done(cls(reason=f.text("reason"), waived_at=f.text("waived_at")))


@dataclass(frozen=True)
class Run:
    id: str
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
        f = Fields(raw)
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


def craftsman_agent_name(agent: str) -> str:
    """The agent name compared against Craftsman's own: lower-cased, without the plugin namespace.

    Only the Craftsman plugin's own namespace is dropped; another plugin's
    `other:craftsman-reviewer` keeps its prefix and so never matches.
    """
    return agent.strip().lower().removeprefix(PLUGIN_AGENT_PREFIX)


def agent_role(agent: str) -> AgentRole | None:
    """The Craftsman role an agent name plays (see `craftsman_agent_name`); other names play none."""
    return AGENT_ROLES.get(craftsman_agent_name(agent))


@dataclass(frozen=True)
class AgentUsage:
    """What one subagent run used, read from its transcript when its stop hook ran."""

    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    duration_s: float

    def __post_init__(self) -> None:
        for name in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"):
            _require(getattr(self, name) >= 0, name, "must be >= 0")
        _require(self.duration_s >= 0, "duration_s", "must be >= 0")

    def to_json(self) -> dict[str, Any]:
        return _record_json(self)

    @classmethod
    def from_json(cls, raw: Any) -> AgentUsage:
        f = Fields(raw)
        return f.done(
            cls(
                input_tokens=f.integer("input_tokens"),
                output_tokens=f.integer("output_tokens"),
                cache_read_tokens=f.integer("cache_read_tokens"),
                cache_write_tokens=f.integer("cache_write_tokens"),
                duration_s=f.number("duration_s"),
            )
        )


RECORD_TOKEN_RE = re.compile(r"[CF]\d+")


@dataclass(frozen=True)
class AgentEvent:
    platform: str
    event: AgentEventKind
    agent: str
    agent_id: str
    at: str
    usage: AgentUsage | None = None  # a stop event only, and only when the transcript was readable
    # A stop event only: the record tokens (`C3`, `F2`) the run's own transcript shows it printed;
    # None when no transcript was readable, so the gate attributes records by time instead.
    records: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        _require_text(self.platform, "platform")
        _require_choice(self.event, AGENT_EVENT_KINDS, "event")
        _require_timestamp(self.at, "at")
        _require(self.usage is None or self.event == "stop", "usage", "is only allowed on a stop event")
        _require(self.records is None or self.event == "stop", "records", "is only allowed on a stop event")
        for index, token in enumerate(self.records or ()):
            _require(RECORD_TOKEN_RE.fullmatch(token) is not None, f"records[{index}]", "must look like C3 or F2")

    def to_json(self) -> dict[str, Any]:
        data = _record_json(self)
        for optional in ("usage", "records"):
            if getattr(self, optional) is None:
                del data[optional]  # ledgers without telemetry or transcripts stay byte-identical
        return data

    @classmethod
    def from_json(cls, raw: Any) -> AgentEvent:
        f = Fields(raw)
        return f.done(
            cls(
                platform=f.text("platform"),
                event=f.choice("event", AGENT_EVENT_KINDS),
                agent=f.text("agent"),
                agent_id=f.text("agent_id"),
                at=f.text("at"),
                usage=f.optional_record_if_present("usage", AgentUsage.from_json),
                records=f.optional_texts_if_present("records"),
            )
        )


def scope_contains(superset: tuple[str, ...] | None, subset: tuple[str, ...] | None) -> bool:
    """True when `superset` covers every path in `subset` (`None` means the full work tree)."""
    if superset is None:
        return True
    if subset is None:
        return False
    return set(subset).issubset(superset)


@dataclass(frozen=True)
class Ledger:
    rounds: tuple[Round, ...] = ()
    coverage: tuple[Coverage, ...] = ()
    findings: tuple[Finding, ...] = ()
    runs: tuple[Run, ...] = ()
    agents: tuple[AgentEvent, ...] = ()

    @property
    def current_scope(self) -> tuple[str, ...] | None:
        """The path scope in force for this ledger (`None` means the full work tree)."""
        return self.rounds[-1].scope if self.rounds else None

    def __post_init__(self) -> None:
        for index, round_ in enumerate(self.rounds):
            _require(round_.number == index + 1, f"rounds[{index}].number", f"must be {index + 1}")
            _require(
                round_.base_sha == self.rounds[0].base_sha,
                f"rounds[{index}].base_sha",
                "must match the base of round 1",
            )
            if index > 0:
                prev_scope = self.rounds[index - 1].scope
                if prev_scope is None:
                    _require(
                        round_.scope is None,
                        f"rounds[{index}].scope",
                        "cannot narrow a full-worktree review scope",
                    )
                else:
                    _require(
                        scope_contains(round_.scope, prev_scope),
                        f"rounds[{index}].scope",
                        "must include every path from the previous round's scope",
                    )
        _require_unique([run.id for run in self.runs], "runs")
        _require_unique([finding.id for finding in self.findings], "findings")
        for index, coverage in enumerate(self.coverage):
            self._check_coverage(coverage, f"coverage[{index}]")
        for index, finding in enumerate(self.findings):
            self._check_finding(finding, f"findings[{index}]")

    def _round(self, number: int, where: str) -> Round:
        _require(number <= len(self.rounds), f"{where}.round", "references an unknown round")
        return self.rounds[number - 1]

    def _check_coverage(self, coverage: Coverage, where: str) -> None:
        round_ = self._round(coverage.round, where)
        known = {hunk.digest for hunk in round_.hunks}
        unknown = [digest for digest in coverage.digests if digest not in known]
        _require(not unknown, f"{where}.digests", f"are not in round {coverage.round}: {unknown}")

    def _check_finding(self, finding: Finding, where: str) -> None:
        self._round(finding.round, where)
        _require(
            finding.verdict_run is None or any(run.id == finding.verdict_run for run in self.runs),
            f"{where}.verdict_run",
            f"references an unknown run {finding.verdict_run!r}",
        )

    def to_json(self) -> dict[str, Any]:
        return {"schema": SCHEMA_VERSION, **_record_json(self)}

    @classmethod
    def from_json(cls, raw: Any) -> Ledger:
        try:
            f = Fields(raw)
            schema = f.integer("schema")
            if schema < SCHEMA_VERSION:
                raise FieldError(
                    "schema",
                    f"{schema} is from an older craftsman; run `craftsman review reset` to archive it",
                )
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
                )
            )
        except FieldError as exc:
            raise LedgerError(f"{LEDGER_FILENAME}: {exc}") from None
