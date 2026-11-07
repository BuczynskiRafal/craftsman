"""Review packet: one Markdown file per round that indexes everything a reviewer must read.

`collect_context` performs the git and file I/O; `render_packet` is a pure function of the
collected context so its layout can be tested without a repository.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from craftsman.diffscan import (
    DiffSnapshot,
    Hunk,
    Reference,
    Scope,
    WorktreeIndex,
    commit_sha,
    detect_signals,
    files_without_signal_patterns,
    git_output,
    is_test_path,
    reference_kind,
)
from craftsman.ledger import reopened_mentions
from craftsman.lenses import LENSES
from craftsman.records import FieldError, Ledger, QualitySignal, Round, SignalKind
from craftsman.text import defang_tokens

MAX_PACKET_CHARS = 120_000
MAX_VIEW_CHARS = 60_000
MAX_SCOPE_LINES = 60
SCOPE_CONTEXT_RADIUS = 8
MAX_COMMITS = 50
MAX_HINTS_LISTED = 12
MAX_HINTS_PER_KIND = 4
MAX_REFERENCES_PER_SYMBOL = 6
TRIMMED_REFERENCES_PER_SYMBOL = 3
MAX_TEST_FILES_PER_SYMBOL = 5
REFERENCE_SEARCH_LIMIT = 40  # ranked references kept per symbol; the render shows fewer
_KIND_RANK = {"call": 0, "test": 1, "import": 2, "docs": 3}

_CONVENTION_FILES = (
    "AGENTS.md",
    "CLAUDE.md",
    "CLAUDE.local.md",
    "GEMINI.md",
    ".github/copilot-instructions.md",
    "CONTRIBUTING.md",
    "CONTRIBUTING.rst",
    "CONTRIBUTING.txt",
    ".github/CONTRIBUTING.md",
    ".github/CONTRIBUTING.rst",
    "docs/CONTRIBUTING.md",
    "docs/CONTRIBUTING.rst",
)
_CONVENTION_GLOBS = (
    "changelog/README.*",
    ".github/instructions/*.instructions.md",
    ".cursor/rules/*.mdc",
)
_NESTED_CONVENTION_FILES = ("AGENTS.md", "CLAUDE.md")
_FENCE_LANGUAGES = {
    ".py": "python",
    ".pyi": "python",
    ".ts": "ts",
    ".tsx": "ts",
    ".js": "js",
    ".jsx": "js",
    ".mjs": "js",
    ".cjs": "js",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".kt": "kotlin",
    ".groovy": "groovy",
    ".gradle": "groovy",
    ".c": "c",
    ".h": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".hh": "cpp",
    ".hpp": "cpp",
    ".hxx": "cpp",
    ".jenkinsfile": "groovy",
    ".cs": "csharp",
    ".rb": "ruby",
    ".sh": "sh",
    ".ps1": "powershell",
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".md": "markdown",
}
_BACKTICK_RUN_RE = re.compile(r"`+")
HUNKS_LEGEND = (
    "Each hunk is shown inside its enclosing scope at HEAD: `+` added, `-` removed, unmarked "
    "unchanged; numbers are HEAD lines; `[H3]` … `[/H3]` bracket hunk H3. More context: "
    "`craftsman review scout H3` (base version, callers, tests) or `craftsman review scout <symbol>`."
)

@dataclass(frozen=True)
class PacketContext:
    snap: DiffSnapshot
    round: Round
    task: str
    commits: tuple[str, ...]
    conventions: tuple[str, ...]
    scopes: Mapping[str, Scope | None]
    symbols: Mapping[str, tuple[str, ...]]
    references: Mapping[str, tuple[Reference, ...]]
    signals_by_hunk: Mapping[str, tuple[str, ...]]
    carried_from: Mapping[str, int]
    reopened: Mapping[str, tuple[tuple[str, str], ...]]
    test_files_by_symbol: Mapping[str, tuple[str, ...]]
    last_runs: tuple[str, ...]
    unscanned_files: tuple[str, ...] = ()  # changed code the risk-signal patterns cannot read


def collect_context(
    root: Path,
    snap: DiffSnapshot,
    round_: Round,
    task: str,
    *,
    symbols: Mapping[str, tuple[str, ...]],
    ledger: Ledger,
    index: WorktreeIndex,
    full_hunks: Collection[str] = (),
) -> PacketContext:
    """Gather commits, conventions, scopes and references for the hunks under review.

    A carried hunk is a one-line stub, except those named in `full_hunks`: a slot can own carried hunks
    (a sweep spans the whole round), and the view of such a slot must show their code.
    """
    shown_in_full = frozenset(full_hunks)
    carried = {hunk.id for hunk in round_.hunks if hunk.carried and hunk.id not in shown_in_full}
    open_hunks = [hunk for hunk in snap.hunks if hunk.id not in carried]
    open_symbols = {hunk.id: symbols.get(hunk.id, ()) for hunk in open_hunks}
    return PacketContext(
        snap=snap,
        round=round_,
        task=task.strip(),
        commits=_commits(root, snap.base_sha),
        conventions=_conventions(root, snap.files),
        scopes={hunk.id: index.hunk_scope(hunk) for hunk in snap.hunks},
        symbols=open_symbols,
        references=_references(index, open_hunks, open_symbols),
        signals_by_hunk=_signals_by_hunk(snap.hunks),
        carried_from={
            hunk_id: found
            for hunk_id, found in _carried_from(ledger, round_).items()
            if hunk_id not in shown_in_full
        },
        reopened=reopened_mentions(round_, snap.hunks, symbols),
        test_files_by_symbol=_test_files_by_symbol(index, open_symbols),
        last_runs=_last_runs(ledger, snap),
        unscanned_files=files_without_signal_patterns(snap),
    )


def _commits(root: Path, base_sha: str) -> tuple[str, ...]:
    if commit_sha(root, "HEAD") is None:
        return ()  # no commits yet on this branch
    # Before the first commit the base is the empty tree, so every commit is new.
    revisions = f"{base_sha}..HEAD" if commit_sha(root, base_sha) is not None else "HEAD"
    out = git_output(
        root, "log", "--reverse", f"--max-count={MAX_COMMITS}", "--format=%h %s", revisions, "--"
    )
    return tuple(line for line in out.splitlines() if line.strip())


def _conventions(root: Path, changed_files: Sequence[str]) -> tuple[str, ...]:
    found = [name for name in _CONVENTION_FILES if (root / name).is_file()]
    for pattern in _CONVENTION_GLOBS:
        found.extend(
            sorted(path.relative_to(root).as_posix() for path in root.glob(pattern) if path.is_file())
        )
    for changed in changed_files:
        for directory in PurePosixPath(changed).parents:
            if directory == PurePosixPath("."):
                continue
            for name in _NESTED_CONVENTION_FILES:
                candidate = (directory / name).as_posix()
                if (root / candidate).is_file():
                    found.append(candidate)
    return tuple(dict.fromkeys(found))


def _references(
    index: WorktreeIndex, hunks: Sequence[Hunk], symbols: Mapping[str, tuple[str, ...]]
) -> dict[str, tuple[Reference, ...]]:
    """Ranked references to each symbol, excluding the changed lines of the hunks that define it.

    Real call sites come first, then test uses, imports and prose. The list is cut only after
    ranking, and the render shows fewer than are kept so it can say how many more there are.
    """
    owners: dict[str, list[Hunk]] = {}
    for hunk in hunks:
        for symbol in symbols.get(hunk.id, ()):
            owners.setdefault(symbol, []).append(hunk)
    references: dict[str, tuple[Reference, ...]] = {}
    for symbol, defining in owners.items():
        defined_in = {hunk.path for hunk in defining}
        outside = [
            ref for ref in index.mentions(symbol) if not any(inside_hunk(ref, hunk) for hunk in defining)
        ]
        # Stable: `mentions` lists each file's lines together, in path order.
        outside.sort(key=lambda ref: (_KIND_RANK[reference_kind(ref)], ref.path in defined_in))
        references[symbol] = tuple(outside[:REFERENCE_SEARCH_LIMIT])
    return references


def _test_files_by_symbol(
    index: WorktreeIndex, symbols: Mapping[str, tuple[str, ...]]
) -> dict[str, tuple[str, ...]]:
    """Test files that mention each symbol, from the same search as its references."""
    result: dict[str, tuple[str, ...]] = {}
    for symbol in sorted({symbol for items in symbols.values() for symbol in items}):
        paths = dict.fromkeys(ref.path for ref in index.mentions(symbol) if is_test_path(ref.path))
        if paths:
            result[symbol] = tuple(paths)[:MAX_TEST_FILES_PER_SYMBOL]
    return result


def _last_runs(ledger: Ledger, snap: DiffSnapshot) -> tuple[str, ...]:
    latest_by_kind = {run.kind: run for run in ledger.runs if run.kind in ("test", "e2e")}
    lines: list[str] = []
    for kind in ("test", "e2e"):
        run = latest_by_kind.get(kind)
        if run is not None:
            freshness = "current" if run.fingerprint == snap.fingerprint else "stale"
            lines.append(
                f"{run.id} `{run.short_command}` ({run.kind}) exit {run.exit_code} ({freshness})"
            )
    return tuple(lines)


def inside_hunk(ref: Reference, hunk: Hunk) -> bool:
    """Whether `ref` points at a line the hunk adds or keeps."""
    return ref.path == hunk.path and hunk.new_start <= ref.line < hunk.new_start + hunk.new_len


def _signals_by_hunk(hunks: Sequence[Hunk]) -> dict[str, tuple[str, ...]]:
    by_hunk: dict[str, list[str]] = {}
    for signal, hunk_ids in detect_signals(hunks).items():
        for hunk_id in hunk_ids:
            by_hunk.setdefault(hunk_id, []).append(signal)
    return {hunk_id: tuple(signals) for hunk_id, signals in by_hunk.items()}


def _signal_label(signal: str) -> str:
    """A risk signal with the lenses it suggests, e.g. `process (→ failure, trust, platform)`."""
    lenses = [lens.id for lens in LENSES if signal in lens.triggers]
    return f"{signal} (→ {', '.join(lenses)})" if lenses else signal


def _carried_from(ledger: Ledger, round_: Round) -> dict[str, int]:
    """For each carried hunk, the latest earlier round whose coverage included its digest."""
    latest_round: dict[str, int] = {}
    for record in ledger.coverage:
        if record.round < round_.number:
            for digest in record.digests:
                latest_round[digest] = max(record.round, latest_round.get(digest, 0))
    return {hunk.id: latest_round.get(hunk.digest, 0) for hunk in round_.hunks if hunk.carried}


# --- rendering ------------------------------------------------------------------------------


_HUNK_RANGE_RE = re.compile(r"^H(\d+)-H(\d+)$")


def expand_hunk_spec(raw: str, all_hunk_ids: Sequence[str]) -> tuple[str, ...]:
    """Expand a comma-separated list of hunk IDs and ranges (`H3-H17`) against `all_hunk_ids`."""
    tokens = [part.strip() for part in raw.split(",") if part.strip()]
    if not tokens:
        raise FieldError("hunks", "must name at least one hunk id or range")
    known = set(all_hunk_ids)
    ordered: list[str] = []
    for token in tokens:
        match = _HUNK_RANGE_RE.match(token)
        if match:
            lo, hi = int(match.group(1)), int(match.group(2))
            if lo > hi:
                raise FieldError("hunks", f"invalid hunk range {token!r}")
            expanded = [f"H{n}" for n in range(lo, hi + 1) if f"H{n}" in known]
            if not expanded:
                raise FieldError("hunks", f"range {token!r} matches no hunks in the round")
            ordered.extend(expanded)
        elif token in known:
            ordered.append(token)
        else:
            raise FieldError("hunks", f"unknown hunk {token!r}")
    return tuple(dict.fromkeys(ordered))


def render_packet(ctx: PacketContext) -> str:
    """Render the packet: every hunk of the round in full, in the same layout as a view."""
    return _render(ctx, assigned=None)


def render_packet_view(ctx: PacketContext, *, assigned_hunks: Collection[str]) -> str:
    """Render a per-reviewer view: its hunks in full and the other hunks as an index."""
    return _render(ctx, assigned=frozenset(assigned_hunks))


def _render(ctx: PacketContext, *, assigned: frozenset[str] | None) -> str:
    """Trim scopes, then symbol references, then largest hunk diffs to stay within budget."""
    preamble = _preamble(ctx, assigned)
    shown = [hunk for hunk in ctx.snap.hunks if assigned is None or hunk.id in assigned]
    owners = _Owners.of(ctx, shown)
    detail = _Detail()
    sections = {hunk.id: _hunk_section(ctx, hunk, owners, detail) for hunk in shown}
    index = [_hunk_summary_line(ctx, hunk) for hunk in ctx.snap.hunks if hunk.id not in sections]
    budget = MAX_PACKET_CHARS if assigned is None else MAX_VIEW_CHARS
    if len(_assemble(preamble, sections, index)) > budget:
        # The preamble counts against the budget: cut the listed hints to their counts first.
        preamble = _preamble(ctx, assigned, list_hints=False)
    for _, hunk in _scope_savings(ctx, shown, owners):
        if len(_assemble(preamble, sections, index)) <= budget:
            break
        detail.no_scope.add(hunk.id)
        sections[hunk.id] = _hunk_section(ctx, hunk, owners, detail)
    for _, symbol, hunk in _reference_savings(ctx, shown, owners):
        if len(_assemble(preamble, sections, index)) <= budget:
            break
        detail.trimmed.add(symbol)
        sections[hunk.id] = _hunk_section(ctx, hunk, owners, detail)
    omitted = _omit_diffs(ctx, shown, owners, preamble, sections, index, budget, detail)
    return _assemble(preamble, sections, index, omitted)


def _omit_diffs(
    ctx: PacketContext, shown: Sequence[Hunk], owners: _Owners, preamble: str,
    sections: dict[str, str], index: Sequence[str], budget: int, detail: _Detail,
) -> tuple[str, ...]:
    baseline = len(_assemble(preamble, sections, index))
    if baseline <= budget:
        return ()
    candidates = sorted(
        ((s, h) for h in shown if h.id not in ctx.carried_from and (s := len(_diff_block(h)) - len(_omitted_diff_line(ctx, h))) > 0),
        key=lambda item: item[0],
        reverse=True,
    )
    by_id = {hunk.id: hunk for hunk in shown}
    before, chosen = dict(sections), set[str]()
    ordered: tuple[str, ...] = ()
    for _, hunk in candidates:
        chosen.add(hunk.id)
        detail.omitted.add(hunk.id)
        for hunk_id in dict.fromkeys((hunk.id, owners.owner_of(ctx, hunk))):
            sections[hunk_id] = _hunk_section(ctx, by_id[hunk_id], owners, detail)
        ordered = tuple(h.id for h in shown if h.id in chosen)
        if len(_assemble(preamble, sections, index, ordered)) <= budget:
            break
    if len(_assemble(preamble, sections, index, ordered)) >= baseline:
        sections.update(before)
        detail.omitted.clear()
        return ()
    return ordered


def _preamble(ctx: PacketContext, assigned: frozenset[str] | None, *, list_hints: bool = True) -> str:
    """The opening sections: only what a hunter lacks elsewhere; a section with nothing to say is left out.

    Lens suggestions are in the risk map `prepare` prints, and the recording commands are in the
    hunter's agent definition and playbooks.
    """
    parts = [_header(ctx), _intent(ctx), _conventions_section(ctx)]
    if ctx.unscanned_files:
        parts.append(_unscanned_note(ctx))
    parts.append(_quality_signals_section(ctx, assigned, list_hints=list_hints))
    return "\n\n".join(part for part in parts if part)


def _assemble(
    preamble: str, sections: Mapping[str, str], index: Sequence[str], omitted_diffs: Sequence[str] = ()
) -> str:
    hunks = "\n\n".join(sections.values()) if sections else "(no hunks assigned)"
    parts = [preamble]
    if omitted_diffs:
        parts.append("## Omitted hunk bodies\n" + ", ".join(omitted_diffs))
    parts.append(f"## Hunks\n{HUNKS_LEGEND}\n\n{hunks}")
    if index:
        parts.append("### Other hunks (index)\n" + "\n".join(index))
    return "\n\n".join(parts) + "\n"


def bound_hints(signals: Sequence[QualitySignal]) -> tuple[list[QualitySignal], str]:
    """The hints worth listing and a one-line count of them all.

    A diff can raise one hint per pair of similar functions, so at most `MAX_HINTS_PER_KIND` of
    each kind and `MAX_HINTS_LISTED` in all are listed; the line says how many more there are.
    """
    per_kind: dict[SignalKind, int] = {}
    listed: list[QualitySignal] = []
    for sig in signals:
        per_kind[sig.kind] = per_kind.get(sig.kind, 0) + 1
        if per_kind[sig.kind] <= MAX_HINTS_PER_KIND and len(listed) < MAX_HINTS_LISTED:
            listed.append(sig)
    counts = ", ".join(f"{kind.value} {count}" for kind, count in per_kind.items())
    more = len(signals) - len(listed)
    summary = f"{len(signals)} hints ({counts})" + (f"; +{more} more not listed" if more else "")
    return listed, summary


def _quality_signals_section(
    ctx: PacketContext, assigned: frozenset[str] | None, *, list_hints: bool = True
) -> str:
    """The round's quality hints, bounded; a view keeps those on the files of its hunks."""
    paths = None if assigned is None else {hunk.path for hunk in ctx.snap.hunks if hunk.id in assigned}
    signals = [sig for sig in ctx.round.signals if paths is None or sig.file in paths]
    if not signals:
        return ""
    listed, summary = bound_hints(signals)
    lines = [
        "## Quality hints",
        "Deterministic hints for the simplicity review; they are not obligations.",
        summary,
    ]
    if list_hints:
        lines.append("")
        for sig in listed:
            lines.append(f"- **{sig.id}** (`{sig.kind.value}`) `{sig.file}:{sig.line}` — {sig.detail}")
    return "\n".join(lines)


def _unscanned_note(ctx: PacketContext) -> str:
    files = ", ".join(f"`{path}`" for path in ctx.unscanned_files)
    return (
        f"Not scanned for risk signals (no patterns for their language): {files}. "
        "No signal there does not mean no risk."
    )


@dataclass
class _Detail:
    """What the size guard has cut so far: scope bodies (by owner hunk), references, hunk bodies."""

    no_scope: set[str] = field(default_factory=set)
    trimmed: set[str] = field(default_factory=set)
    omitted: set[str] = field(default_factory=set)


_ScopeKey = tuple[str, str, int, int]


@dataclass(frozen=True)
class _Owners:
    """The shown hunk that renders each shared scope and each symbol's references; others link to it."""

    scope: Mapping[_ScopeKey, str]
    symbol: Mapping[str, str]
    unit: Mapping[_ScopeKey, tuple[Hunk, ...]]  # the hunks that share each scope's code block

    @classmethod
    def of(cls, ctx: PacketContext, shown: Sequence[Hunk]) -> _Owners:
        scope: dict[_ScopeKey, str] = {}
        symbol: dict[str, str] = {}
        unit: dict[_ScopeKey, list[Hunk]] = {}
        for hunk in shown:
            if hunk.id in ctx.carried_from:
                continue
            found = ctx.scopes.get(hunk.id)
            if found is not None and not _has_notes(hunk):
                key = _scope_key(hunk, found)
                scope.setdefault(key, hunk.id)
                unit.setdefault(key, []).append(hunk)
            for name in ctx.symbols.get(hunk.id, ()):
                symbol.setdefault(name, hunk.id)
        return cls(scope, symbol, {key: tuple(items) for key, items in unit.items()})

    def owner_of(self, ctx: PacketContext, hunk: Hunk) -> str:
        """The hunk whose section holds the code of `hunk`: itself unless it shares a scope."""
        found = ctx.scopes.get(hunk.id)
        if found is None or _has_notes(hunk):
            return hunk.id
        return self.scope.get(_scope_key(hunk, found), hunk.id)


def _has_notes(hunk: Hunk) -> bool:
    """Whether the hunk carries `~` notes (a mode change or rename); those stay a plain diff."""
    return any(line.startswith("~") for line in hunk.lines)


def _scope_key(hunk: Hunk, scope: Scope) -> _ScopeKey:
    return (hunk.path, scope.name, scope.start, scope.end)


def _scope_savings(ctx: PacketContext, shown: Sequence[Hunk], owners: _Owners) -> list[tuple[int, Hunk]]:
    """Characters saved by cutting each scope body, largest first; savings of 0 are left out."""
    savings = []
    for hunk in shown:
        scope = ctx.scopes.get(hunk.id)
        if scope is None or hunk.id in ctx.carried_from or owners.scope.get(_scope_key(hunk, scope)) != hunk.id:
            continue
        group = owners.unit[_scope_key(hunk, scope)]
        full = len(_unit_block(group, scope, bare=False))
        bare = len(_unit_block(group, scope, bare=True)) + len(_omitted_scope_line(scope))
        if full > bare:
            savings.append((full - bare, hunk))
    return sorted(savings, key=lambda item: item[0], reverse=True)


def _header(ctx: PacketContext) -> str:
    snap = ctx.snap
    carried = sum(hunk.carried for hunk in ctx.round.hunks)
    return (
        f"# Craftsman review packet — round {ctx.round.number}\n"
        f"Base `{snap.base_sha[:7]}` · fingerprint `{snap.fingerprint[:12]}` · "
        f"{'deep round · ' if ctx.round.deep else ''}"
        f"{len(snap.files)} files, +{snap.added}/−{snap.deleted} lines, "
        f"{len(snap.hunks)} hunks ({carried} carried)"
    )


def _intent(ctx: PacketContext) -> str:
    """The task and the commits since base, whichever exist."""
    parts = []
    if ctx.task:
        parts.append(f"**Task:** {defang_tokens(ctx.task)}")
    if ctx.commits:
        parts.append("**Commits since base:**\n" + "\n".join(f"- {defang_tokens(commit)}" for commit in ctx.commits))
    return "## Intent\n" + "\n\n".join(parts) if parts else ""


def _conventions_section(ctx: PacketContext) -> str:
    if not ctx.conventions:
        return ""
    return "## Conventions to read first\n" + "\n".join(f"- {path}" for path in ctx.conventions)


def _hunk_summary_line(ctx: PacketContext, hunk: Hunk) -> str:
    scope = ctx.scopes.get(hunk.id)
    if scope is not None:
        label = scope.name
    elif hunk.id in ctx.carried_from:
        label = f"carried (round {ctx.carried_from[hunk.id]})"
    else:
        label = hunk.status
    return f"- {hunk.id} · {hunk.path} · {label}"


def _hunk_section(ctx: PacketContext, hunk: Hunk, owners: _Owners, detail: _Detail) -> str:
    title = f"### {hunk.id} · `{hunk.path}`{_line_span(hunk)}"
    if hunk.id in ctx.carried_from:
        return f"{title} · carried (covered in round {ctx.carried_from[hunk.id]}, unchanged)"
    status = f"renamed from `{hunk.old_path}`" if hunk.old_path else hunk.status
    signals = ctx.signals_by_hunk.get(hunk.id, ())
    title += f" · {status}" + (f" · signals: {', '.join(_signal_label(s) for s in signals)}" if signals else "")
    mentions = ctx.reopened.get(hunk.id, ())
    if mentions:
        title += " · reopened: mentions " + ", ".join(
            f"{symbol} (changed in {owner})" for symbol, owner in mentions
        )
    lines = [title, *_code_lines(ctx, hunk, owners, detail)]
    lines.extend(_symbol_lines(ctx, hunk, owners, detail.trimmed))
    return "\n".join(lines)


def _code_lines(ctx: PacketContext, hunk: Hunk, owners: _Owners, detail: _Detail) -> list[str]:
    """The scope line and the code of a hunk: its scope's block if it owns it, else a pointer to it."""
    scope = ctx.scopes.get(hunk.id)
    omitted_line = [_omitted_diff_line(ctx, hunk)] if hunk.id in detail.omitted else []
    if scope is None:
        return omitted_line or [_diff_block(hunk)]
    if _has_notes(hunk):
        return [_scope_line(scope, ()), *(omitted_line or [_diff_block(hunk)])]
    key = _scope_key(hunk, scope)
    owner = owners.scope[key]
    if owner != hunk.id:
        return [f"Scope: `{scope.name}` — shown at {owner}", *omitted_line]
    group = owners.unit[key]
    cut = detail.omitted & {member.id for member in group}
    bare = hunk.id in detail.no_scope or bool(cut)
    lines = [_scope_line(scope, [member.id for member in group if member.id != hunk.id]), *omitted_line]
    block = _unit_block(group, scope, bare=bare, omitted=cut)
    if block:
        lines.append(block)
    if bare:
        lines.append(_omitted_scope_line(scope))
    return lines


def _scope_line(scope: Scope, also: Sequence[str]) -> str:
    shared = f" — also covers {', '.join(also)}" if also else ""
    return f"Scope: `{scope.name}` ({scope.kind}, L{scope.start}–L{scope.end}){shared}"


def _diff_block(hunk: Hunk) -> str:
    header = f"@@ -{hunk.old_start},{hunk.old_len} +{hunk.new_start},{hunk.new_len} @@ {hunk.header}"
    return fenced("diff", [header.rstrip(), *hunk.lines])


def _omitted_diff_line(ctx: PacketContext, hunk: Hunk) -> str:
    if hunk.status == "added":
        return f"diff omitted ({len(hunk.lines)} lines): open {hunk.path}"
    paths = f"{hunk.old_path} {hunk.path}" if hunk.old_path else hunk.path
    return f"diff omitted ({len(hunk.lines)} lines): git diff {ctx.snap.base_sha[:7]} -- {paths}"


def _line_span(hunk: Hunk) -> str:
    if hunk.new_len > 0:
        return f" L{hunk.new_start}–L{hunk.new_start + hunk.new_len - 1}"
    if hunk.old_len > 0:
        return f" (removed L{hunk.old_start}–L{hunk.old_start + hunk.old_len - 1})"
    return ""


def _signature_line_count(raw_lines: Sequence[str]) -> int:
    for idx, line in enumerate(raw_lines[:5], 1):
        stripped = line.rstrip()
        if stripped.endswith((":", "{")):
            return idx
    return 1


@dataclass(frozen=True)
class _Row:
    """One line of a scope's code block: HEAD text, or a removed line (no number)."""

    tag: str  # " " unchanged, "+" added, "-" removed, "\\" the no-newline note
    number: int | None  # the HEAD line number; None for removed lines and notes
    text: str
    hunk: str | None  # the hunk the line belongs to; None for scope lines no hunk touches


def _unit_block(
    group: Sequence[Hunk], scope: Scope, *, bare: bool, omitted: Collection[str] = ()
) -> str:
    """The fenced code of the scope with the changes of every hunk of `group` marked in place."""
    hunks = [hunk for hunk in group if hunk.id not in omitted]
    if not hunks:
        return ""
    rows = _unit_rows(hunks, scope)
    return fenced(fence_language(group[0].path), _display_lines(rows, scope, bare=bare))


def _unit_rows(hunks: Sequence[Hunk], scope: Scope) -> list[_Row]:
    """The scope's lines at HEAD with each hunk's added, removed and context lines overlaid.

    A removed line sits just before the HEAD line that follows it; the keys order the rows
    `(HEAD line, removed lines < the line < notes, order of appearance)`.
    """
    keyed: dict[tuple[int, int, int], _Row] = {}
    for offset, text in enumerate(scope.text.split("\n")):
        keyed[(scope.start + offset, 1, 0)] = _Row(" ", scope.start + offset, text, None)
    order = 0
    for hunk in hunks:
        number = hunk.new_start
        last: tuple[int, int, int] | None = None
        for line in hunk.lines:
            tag, text = line[:1], line[1:]
            order += 1
            if tag == "-":
                last = (number, 0, order)
                keyed[last] = _Row("-", None, text, hunk.id)
            elif tag in (" ", "+"):
                last = (number, 1, 0)
                keyed[last] = _Row(tag, number, text, hunk.id)
                number += 1
            elif tag == "\\" and last is not None:
                keyed[(last[0], last[1] * 2, order)] = _Row("\\", None, text, hunk.id)
    return [keyed[key] for key in sorted(keyed)]


def _display_lines(rows: Sequence[_Row], scope: Scope, *, bare: bool) -> list[str]:
    """Number and mark the rows to show; `[H3]`..`[/H3]` bracket a hunk, gaps name the lines left out."""
    keep = _kept_rows(rows, scope, bare=bare)
    top = max((row.number for row in rows if row.number is not None), default=0)
    width = max(2, len(str(top)))
    lines: list[str] = []
    open_hunk: str | None = None
    gap: list[int] = []
    for row, kept in zip(rows, keep, strict=True):
        if not kept:
            if row.number is not None:
                gap.append(row.number)
            continue
        if gap or row.hunk != open_hunk:
            if open_hunk is not None:
                lines.append(f"[/{open_hunk}]")
            open_hunk = None
        if gap:
            if not bare:
                lines.append(omission_marker(gap[0], gap[-1]))
            gap = []
        if row.hunk != open_hunk:
            open_hunk = row.hunk
            if open_hunk is not None:
                lines.append(f"[{open_hunk}]")
        lines.append(_row_text(row, width))
    if open_hunk is not None:
        lines.append(f"[/{open_hunk}]")
    if gap and not bare:
        lines.append(omission_marker(gap[0], gap[-1]))
    return lines


def _kept_rows(rows: Sequence[_Row], scope: Scope, *, bare: bool) -> list[bool]:
    """Which rows to show: the hunks only when bare; a long scope as its signature and hunk windows."""
    if bare:
        return [row.hunk is not None for row in rows]
    if scope.end - scope.start + 1 <= MAX_SCOPE_LINES:
        return [True] * len(rows)
    keep = [False] * len(rows)
    for position, row in enumerate(rows):
        if row.hunk is not None:
            for near in range(max(0, position - SCOPE_CONTEXT_RADIUS), position + SCOPE_CONTEXT_RADIUS + 1):
                if near < len(rows):
                    keep[near] = True
    signature = _signature_line_count(scope.text.split("\n"))
    for position, row in enumerate(rows):
        if row.number is not None and scope.start <= row.number < scope.start + signature:
            keep[position] = True
    return keep


def _row_text(row: _Row, width: int) -> str:
    if row.tag == "\\":
        return f"\\{row.text}"
    if row.number is None:
        return f"{row.tag}{' ' * width} {row.text}"
    return f"{row.tag}{row.number:>{width}} {row.text}"


def omission_marker(first: int, last: int) -> str:
    # Not a comment of any one language: the block may hold Python, Go, SQL or YAML.
    return f"[… L{first}–L{last} omitted …]"


def _omitted_scope_line(scope: Scope) -> str:
    return f"Enclosing scope omitted for size — open {scope.path}:L{scope.start}–L{scope.end}"


def _symbol_lines(
    ctx: PacketContext, hunk: Hunk, owners: _Owners, trimmed_symbols: Collection[str] = ()
) -> list[str]:
    symbols = ctx.symbols.get(hunk.id, ())
    lines: list[str] = []
    if symbols:
        lines += ["Defines/changes: " + ", ".join(f"`{symbol}`" for symbol in symbols), "References:"]
        for symbol in symbols:
            owner = owners.symbol.get(symbol, hunk.id)
            if owner != hunk.id:
                lines.append(f"- `{symbol}`: (shown at {owner})")
                continue
            refs = ctx.references.get(symbol, ())
            trim = symbol in trimmed_symbols
            lines.append(_reference_line(symbol, refs, trim=trim))
            # A test file already named by a listed reference, or the file of the hunk, adds nothing.
            known = {ref.path for ref in _listed_references(refs, trim=trim)} | {hunk.path}
            test_files = [path for path in ctx.test_files_by_symbol.get(symbol, ()) if path not in known]
            if test_files:
                lines.append(f"Tests referencing `{symbol}`: " + ", ".join(f"`{p}`" for p in test_files))
    open_ids = [h.id for h in ctx.snap.hunks if h.id not in ctx.carried_from]
    if ctx.last_runs and (len(open_ids) <= 5 or (open_ids and hunk.id == open_ids[0])):
        lines.append("Last runs: " + " · ".join(ctx.last_runs))
    return lines


def _listed_references(refs: Sequence[Reference], *, trim: bool) -> Sequence[Reference]:
    return refs[: TRIMMED_REFERENCES_PER_SYMBOL if trim else MAX_REFERENCES_PER_SYMBOL]


def _reference_line(symbol: str, refs: Sequence[Reference], *, trim: bool) -> str:
    shown = _listed_references(refs, trim=trim)
    listed = " · ".join(
        f"{inline_code(f'{ref.path}:{ref.line}')} {inline_code(ref.text)}" for ref in shown
    )
    more = len(refs) - len(shown)
    suffix = f" (+{more} more: git grep -n {symbol})" if more > 0 else ""
    return f"- `{symbol}`: {listed or '(no other references found)'}{suffix}"


def _reference_savings(
    ctx: PacketContext, shown: Sequence[Hunk], owners: _Owners
) -> list[tuple[int, str, Hunk]]:
    savings: list[tuple[int, str, Hunk]] = []
    for hunk in shown:
        if hunk.id in ctx.carried_from:
            continue
        for symbol in ctx.symbols.get(hunk.id, ()):
            refs = ctx.references.get(symbol, ())
            if owners.symbol.get(symbol) != hunk.id or len(refs) <= TRIMMED_REFERENCES_PER_SYMBOL:
                continue
            saved = len(_reference_line(symbol, refs, trim=False)) - len(_reference_line(symbol, refs, trim=True))
            if saved > 0:
                savings.append((saved, symbol, hunk))
    return sorted(savings, key=lambda item: (-item[0], item[1]))


def fence_language(path: str) -> str:
    posix = PurePosixPath(path)
    if posix.name == "Jenkinsfile" or posix.name.startswith("Jenkinsfile."):
        return "groovy"
    return _FENCE_LANGUAGES.get(posix.suffix.lower(), "")


def inline_code(text: str) -> str:
    """Wrap `text` in a code span that backticks inside it cannot close early (CommonMark)."""
    longest = max((len(run) for run in _BACKTICK_RUN_RE.findall(text)), default=0)
    fence = "`" * (longest + 1)
    pad = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{fence}{pad}{text}{pad}{fence}"


def fenced(language: str, lines: Sequence[str]) -> str:
    """Fence `lines` with more backticks than any run inside them so the block cannot break."""
    longest = max((len(run) for line in lines for run in _BACKTICK_RUN_RE.findall(line)), default=0)
    fence = "`" * max(3, longest + 1)
    return "\n".join([f"{fence}{language}", *lines, fence])
