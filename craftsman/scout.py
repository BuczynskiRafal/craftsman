"""`craftsman review scout`: grounded, bounded context for a hunk, a location or a symbol.

The scout answers with code, never opinions, so a hunter need not read whole files: the base
version of the code a hunk changes, who calls the symbols it defines, and the tests that use
them. It is deterministic (git and the scope and reference finders of `diffscan`) and its output
is capped at `MAX_SCOUT_CHARS`; what does not fit is counted in a closing line.
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from craftsman.diffscan import (
    DiffScanError,
    Hunk,
    Reference,
    Scope,
    WorktreeIndex,
    defines_symbol,
    file_at_revision,
    reference_kind,
    text_scope_at,
    text_scope_named,
)
from craftsman.packet import fence_language, fenced, inside_hunk, omission_marker
from craftsman.records import FieldError

MAX_SCOUT_CHARS = 12_000
MAX_CALLERS_PER_SYMBOL = 3
CALLER_RADIUS = 5
MAX_TESTS = 3
TEST_BODY_LINES = 40
DEFINITION_LINES = 60
BASE_VERSION_LINES = 60
MAX_SYMBOL_REFERENCES = 6
SYMBOL_RADIUS = 3
MAX_SYMBOLS_PER_HUNK = 6

_HUNK_TARGET_RE = re.compile(r"H\d+(?:-H\d+)?(?:\s*,\s*H\d+(?:-H\d+)?)*")
_LOCATION_RE = re.compile(r"(?P<path>.+):(?P<line>\d+)")
_TRAILER = "(trimmed: {count} more; narrow the target)"
_KIND_ORDER = {"call": 0, "import": 1, "docs": 2, "test": 3}

TargetKind = Literal["hunks", "location", "symbol"]


@dataclass(frozen=True)
class Target:
    kind: TargetKind
    raw: str  # hunk spec, symbol name or the path of a location
    line: int = 0  # location only


def parse_target(raw: str) -> Target:
    """`H3`, `H3-H5`, `H3,H7` are hunks; `path:line` a location; anything else a symbol."""
    text = raw.strip()
    if not text:
        raise FieldError("target", "must name a hunk (H3), a location (path:line) or a symbol")
    if _HUNK_TARGET_RE.fullmatch(text):
        return Target("hunks", text)
    location = _LOCATION_RE.fullmatch(text)
    if location is not None:
        return Target("location", location.group("path"), int(location.group("line")))
    return Target("symbol", text)


@dataclass(frozen=True)
class _Item:
    """One piece of the answer; `heads` are the headings it sits under."""

    heads: tuple[str, ...]
    text: str


class Scout:
    """Answers scout questions about one work tree against one base revision."""

    def __init__(self, index: WorktreeIndex, base: str) -> None:
        self.index = index
        self.base = base
        self._seen: set[tuple[str, int]] = set()  # caller and test snippets already shown

    # --- entry points -----------------------------------------------------------------------

    def hunks(self, hunks: Sequence[Hunk]) -> str:
        items: list[_Item] = []
        tests_for: set[tuple[str, int]] = set()
        for hunk in hunks:
            items.extend(self._hunk_items(hunk, tests_for))
        title = f"# Scout {', '.join(hunk.id for hunk in hunks)} · base {self.base[:7]}"
        return _compose(title, items)

    def location(self, path: str, line: int) -> str:
        scope = self.index.scope(path, line)
        if scope is None:
            raise DiffScanError(f"cannot read {path}:{line} (missing, binary or past the end of the file)")
        title = f"# Scout {path}:{line} · base {self.base[:7]}"
        symbols = () if scope.kind == "block" else (_short(scope.name),)
        heads = (f"## {path}:{line} · {scope.name} ({scope.kind}, L{scope.start}–L{scope.end})",)
        items = self._base_items(heads, scope, path, line, line)
        items += self._related_items(heads, symbols, scope, None, set())
        return _compose(title, items)

    def symbol(self, name: str) -> str:
        short = _short(name)
        title = f"# Scout `{name}` · base {self.base[:7]}"
        mentions = self.index.mentions(short)
        items = self._definition_items(name, short, mentions)
        references = sorted(
            (ref for ref in mentions if reference_kind(ref) != "test" and not defines_symbol(ref.path, ref.text, short)),
            key=lambda ref: _KIND_ORDER[reference_kind(ref)],
        )
        head = (f"## References to `{name}`",)
        if not references:
            items.append(_Item(head, "(no references found outside tests)"))
        for ref in references[:MAX_SYMBOL_REFERENCES]:
            items.append(self._snippet(head, ref, SYMBOL_RADIUS))
        if len(references) > MAX_SYMBOL_REFERENCES:
            items.append(_Item(head, f"(+{len(references) - MAX_SYMBOL_REFERENCES} more: grep -rnw {short})"))
        items += self._test_items((f"## Tests referencing `{name}`",), (short,), set())
        if not mentions:
            items.insert(0, _Item((), f"No definition or reference of `{name}` found in the work tree."))
        return _compose(title, items)

    # --- a hunk -----------------------------------------------------------------------------

    def _hunk_items(self, hunk: Hunk, tests_for: set[tuple[str, int]]) -> list[_Item]:
        scope = self.index.hunk_scope(hunk)
        label = f"{scope.name} ({scope.kind})" if scope is not None else f"no enclosing scope, {hunk.status}"
        heads = (f"## {hunk.id} · {hunk.path} · {label}",)
        if scope is None:
            return [_Item(heads, f"(nothing to compare: {hunk.status} file without code at HEAD)")]
        anchor = _old_anchor(hunk)
        items = self._base_items(heads, scope, hunk.old_path or hunk.path, anchor, anchor, hunk)
        symbols = self.index.symbols(hunk)[:MAX_SYMBOLS_PER_HUNK]
        return items + self._related_items(heads, symbols, scope, hunk, tests_for)

    def _related_items(
        self,
        heads: tuple[str, ...],
        symbols: Sequence[str],
        scope: Scope,
        hunk: Hunk | None,
        tests_for: set[tuple[str, int]],
    ) -> list[_Item]:
        items: list[_Item] = []
        for symbol in symbols:
            head = (*heads, f"### Callers of `{symbol}`")
            callers = self._callers(symbol, scope, hunk)
            if not callers:
                items.append(_Item(head, "(no callers found outside tests)"))
            fresh = [ref for ref in callers if (ref.path, ref.line) not in self._seen]
            for ref in fresh[:MAX_CALLERS_PER_SYMBOL]:
                self._seen.add((ref.path, ref.line))
                items.append(self._snippet(head, ref, CALLER_RADIUS))
            if len(fresh) > MAX_CALLERS_PER_SYMBOL:
                items.append(_Item(head, f"(+{len(fresh) - MAX_CALLERS_PER_SYMBOL} more callers)"))
        return items + self._test_items((*heads, "### Tests"), symbols, tests_for)

    def _callers(self, symbol: str, scope: Scope, hunk: Hunk | None) -> list[Reference]:
        """Call sites, the changed lines and the symbol's own body excluded; same file first."""
        own = _short(scope.name) == symbol
        found = [
            ref
            for ref in self.index.mentions(symbol)
            if reference_kind(ref) == "call"
            and not defines_symbol(ref.path, ref.text, symbol)
            and not (hunk is not None and inside_hunk(ref, hunk))
            and not (own and ref.path == scope.path and scope.start <= ref.line <= scope.end)
        ]
        home = scope.path
        folder = os.path.dirname(home)
        return sorted(found, key=lambda ref: (0 if ref.path == home else 1 if os.path.dirname(ref.path) == folder else 2))

    # --- base version -----------------------------------------------------------------------

    def _base_items(
        self, heads: tuple[str, ...], scope: Scope, base_path: str, anchor: int, focus: int, hunk: Hunk | None = None
    ) -> list[_Item]:
        head = (*heads, f"### Base version at {self.base[:7]}")
        text = file_at_revision(self.index.root, self.base, base_path)
        if text is None:
            return [_Item(head, "new in this change (the file does not exist in the base)")]
        if scope.kind == "block":
            found = text_scope_at(base_path, text, max(1, anchor))
        else:
            found = text_scope_named(base_path, text, scope.name, near=scope.start)
        if found is None:
            return [_Item(head, f"new in this change (no `{scope.name}` in the base)")]
        lines = found.text.split("\n")
        shown = _numbered(lines, found.start, max(found.start, focus), BASE_VERSION_LINES)
        caption = f"{base_path}:L{found.start}–L{found.end}"
        return [_Item(head, f"`{caption}`\n{fenced(fence_language(base_path), shown)}")]

    # --- tests and definitions --------------------------------------------------------------

    def _test_items(
        self, head: tuple[str, ...], symbols: Sequence[str], tests_for: set[tuple[str, int]]
    ) -> list[_Item]:
        candidates: list[tuple[Scope, int]] = []
        for symbol in symbols:
            for ref in self.index.mentions(symbol):
                if reference_kind(ref) != "test":
                    continue
                found = self.index.scope(ref.path, ref.line)
                if found is None or found.kind == "block" or (found.path, found.start) in tests_for:
                    continue
                tests_for.add((found.path, found.start))
                candidates.append((found, ref.line))
        candidates.sort(key=lambda item: "test" not in item[0].name.lower())
        if not candidates:
            return [_Item(head, "(no test function references them)")]
        items = [
            _Item(head, self._test_text(scope, line)) for scope, line in candidates[:MAX_TESTS]
        ]
        if len(candidates) > MAX_TESTS:
            items.append(_Item(head, f"(+{len(candidates) - MAX_TESTS} more tests)"))
        return items

    def _test_text(self, scope: Scope, line: int) -> str:
        shown = _numbered(scope.text.split("\n"), scope.start, line, TEST_BODY_LINES)
        return f"`{scope.path}:L{scope.start}–L{scope.end}` {scope.name}\n{fenced(fence_language(scope.path), shown)}"

    def _definition_items(self, name: str, short: str, mentions: Sequence[Reference]) -> list[_Item]:
        head = (f"## Definition of `{name}`",)
        items: list[_Item] = []
        seen: set[tuple[str, int]] = set()
        for ref in mentions:
            if not defines_symbol(ref.path, ref.text, short):
                continue
            found = self.index.scope(ref.path, ref.line)
            if found is None or found.kind == "block" or (found.path, found.start) in seen:
                continue
            seen.add((found.path, found.start))
            shown = _numbered(found.text.split("\n"), found.start, found.start, DEFINITION_LINES)
            caption = f"`{found.path}:L{found.start}–L{found.end}` {found.name}"
            items.append(_Item(head, f"{caption}\n{fenced(fence_language(found.path), shown)}"))
        if len(items) > MAX_CALLERS_PER_SYMBOL:
            extra = len(items) - MAX_CALLERS_PER_SYMBOL
            items = [*items[:MAX_CALLERS_PER_SYMBOL], _Item(head, f"(+{extra} more definitions)")]
        return items or [_Item(head, "(no definition found)")]

    def _snippet(self, head: tuple[str, ...], ref: Reference, radius: int) -> _Item:
        lines = self.index.lines(ref.path) or ()
        first, last = max(1, ref.line - radius), min(len(lines), ref.line + radius)
        shown = _numbered(list(lines[first - 1 : last]), first, ref.line, last - first + 1, mark=ref.line)
        return _Item(head, f"`{ref.path}:{ref.line}`\n{fenced(fence_language(ref.path), shown)}")


# --- helpers --------------------------------------------------------------------------------


def _short(name: str) -> str:
    return name.rsplit(".", 1)[-1]


def _old_anchor(hunk: Hunk) -> int:
    """A line of the hunk's old side: its first removed line, else its first context line."""
    removed = hunk.removed_lines
    if removed:
        return removed[0][0]
    return max(1, hunk.old_start)


def _numbered(lines: Sequence[str], first: int, focus: int, limit: int, *, mark: int = 0) -> list[str]:
    """`lines` (starting at line `first`) numbered; a long body keeps its signature and a window
    around line `focus`, the gaps named by an omission marker. The line `mark` gets a `>`."""
    width = len(str(first + len(lines) - 1))

    def row(offset: int) -> str:
        number = first + offset
        return f"{'>' if number == mark else ' '}{number:>{width}} {lines[offset]}"

    if len(lines) <= limit:
        return [row(offset) for offset in range(len(lines))]
    room = max(1, limit - 3)  # the signature and up to two markers
    start = min(max(1, focus - first - room // 2), len(lines) - room)
    shown = [row(0)]
    if start > 1:
        shown.append(omission_marker(first + 1, first + start - 1))
    shown.extend(row(offset) for offset in range(start, start + room))
    if start + room < len(lines):
        shown.append(omission_marker(first + start + room, first + len(lines) - 1))
    return shown


def _compose(title: str, items: Sequence[_Item]) -> str:
    """The title and as many items as fit in `MAX_SCOUT_CHARS`; the rest are counted."""
    out = [title]
    size = len(title) + 1
    reserve = len(_TRAILER.format(count=len(items))) + 2
    shown: tuple[str, ...] = ()
    for position, item in enumerate(items):
        chunk = _chunk(item, shown)
        if size + len(chunk) + reserve > MAX_SCOUT_CHARS:
            if position == 0:
                chunk = chunk[: MAX_SCOUT_CHARS - size - reserve].rsplit("\n", 1)[0]
                out.append(chunk)
                position += 1
            out.append(_TRAILER.format(count=len(items) - position))
            return "\n".join(out)
        out.append(chunk)
        size += len(chunk) + 1
        shown = item.heads
    return "\n".join(out)


def _chunk(item: _Item, shown: tuple[str, ...]) -> str:
    """The item's text under the headings that differ from the previous item's."""
    common = 0
    while common < len(shown) and common < len(item.heads) and shown[common] == item.heads[common]:
        common += 1
    return "\n".join([*item.heads[common:], item.text])

