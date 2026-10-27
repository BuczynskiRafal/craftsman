"""Review packet: one Markdown file per round that indexes everything a reviewer must read.

`collect_context` performs the git and file I/O; `render_packet` is a pure function of the
collected context so its layout can be tested without a repository.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from craftsman.diffscan import (
    DiffSnapshot,
    Hunk,
    Reference,
    Scope,
    WorktreeIndex,
    commit_sha,
    detect_signals,
    git_output,
    is_test_path,
)
from craftsman.ledger import reopened_mentions, valid_facts_for_hunk
from craftsman.lenses import LensPick
from craftsman.records import SWEEP_LENS, Fact, FieldError, Ledger, Round

MAX_PACKET_CHARS = 350_000
MAX_SCOPE_LINES = 60
SCOPE_CONTEXT_RADIUS = 15
MAX_COMMITS = 50
MAX_REFERENCES_PER_SYMBOL = 8
MAX_TEST_FILES_PER_SYMBOL = 5
REFERENCE_SEARCH_LIMIT = 40

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
CHEAT_SHEET = (
    "- Cover (1–4 hunks per call; the note names every hunk id and what you tried on it; "
    "for >1 entry, prefer `--from-json - <<'EOF'`):\n"
    '  `craftsman review cover --from-json - <<\'EOF\'` with `[{"lens": "<lens>", "hunks": ["H1", "H2"], '
    '"note": "H1 f: x=None -> L42 raises; H2 g: callers a.py:19 ok"}]`, or single:\n'
    '  `craftsman review cover --lens <lens> --hunks H1,H2 --note "H1 f: x=None -> L42 raises; '
    'H2 g: callers a.py:19 ok"`; the `design` lens takes `--files a.py` (`"files"` in JSON) '
    "instead of hunk ids",
    "- Finding (one per root cause; for >1 entry, prefer `--from-json - <<'EOF'`; group unjudged "
    "duplicates before verification with `craftsman review merge F1 F2` — Merge only when ONE fix "
    "removes every consequence. Different fixes are separate findings, even on the same line, file, "
    "or category, e.g. a missing `--json` flag in `craftsman status` and a stale README example are "
    "two separate findings, not one 'housekeeping' finding):\n"
    '  `craftsman review add --from-json - <<\'EOF\'` with `[{"lens": "<lens>", "file": "<path>", '
    '"line": 42, "severity": "major", "title": "...", "scenario": "<root cause -> wrong outcomes>", '
    '"suggestion": "..."}]`, or single:\n'
    "  `craftsman review add --lens <lens> --file <path> --line <n> --severity "
    'blocker|major|minor|nit --title "..." --scenario "<root cause -> wrong outcomes>" '
    '--suggestion "..."`',
    "- Verdict (for >1 entry, prefer `craftsman review verdict --from-json - <<'EOF'` with "
    '`[{"id": "F1", "as": "probable", "evidence": "static: <file.py:42 chain>"}]`): '
    '`craftsman review verdict F1 --as proven --run R1 --evidence "..."` · '
    '`--as probable --evidence "static: <file.py:42 chain>"` (or `"no-repro: <why>; <chain>"`) · '
    '`--as question --evidence "intent: <source>; <file.py:42 trade-off>"` · '
    '`--as dismissed --evidence "<guard at file.py:42>"`',
    "- Run (tests, repro, e2e): `craftsman run --kind test|e2e|repro|lint|build -- <command>`",
    "- Progress: `craftsman review show` · gate status: `craftsman review check` "
    "(review-only mode without `--fix`: `craftsman review check --review-only` then "
    "`craftsman review report --review-only`)",
)


@dataclass(frozen=True)
class PacketContext:
    snap: DiffSnapshot
    round: Round
    picks: tuple[LensPick, ...]
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
    facts_by_hunk: Mapping[str, tuple[Fact, ...]]


def collect_context(
    root: Path,
    snap: DiffSnapshot,
    round_: Round,
    picks: Sequence[LensPick],
    task: str,
    *,
    symbols: Mapping[str, tuple[str, ...]],
    ledger: Ledger,
    index: WorktreeIndex,
) -> PacketContext:
    """Gather commits, conventions, scopes and references for the hunks under review."""
    carried = {hunk.id for hunk in round_.hunks if hunk.carried}
    open_hunks = [hunk for hunk in snap.hunks if hunk.id not in carried]
    open_symbols = {hunk.id: symbols.get(hunk.id, ()) for hunk in open_hunks}
    return PacketContext(
        snap=snap,
        round=round_,
        picks=tuple(picks),
        task=task.strip(),
        commits=_commits(root, snap.base_sha),
        conventions=_conventions(root, snap.files),
        scopes={hunk.id: index.hunk_scope(hunk) for hunk in snap.hunks},
        symbols=open_symbols,
        references=_references(index, open_hunks, open_symbols),
        signals_by_hunk=_signals_by_hunk(snap.hunks),
        carried_from=_carried_from(ledger, round_),
        reopened=reopened_mentions(round_, snap.hunks, symbols),
        test_files_by_symbol=_test_files_by_symbol(index, open_symbols),
        last_runs=_last_runs(ledger, snap),
        facts_by_hunk=valid_facts_for_hunk(ledger, root),
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
    """References to each symbol, excluding the changed lines of the hunks that define it."""
    owners: dict[str, list[Hunk]] = {}
    for hunk in hunks:
        for symbol in symbols.get(hunk.id, ()):
            owners.setdefault(symbol, []).append(hunk)
    references: dict[str, tuple[Reference, ...]] = {}
    for symbol, defining in owners.items():
        found = index.references(
            symbol, limit=REFERENCE_SEARCH_LIMIT, defined_in={hunk.path for hunk in defining}
        )
        outside = [ref for ref in found if not any(_inside(ref, hunk) for hunk in defining)]
        references[symbol] = tuple(outside[:MAX_REFERENCES_PER_SYMBOL])
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


def _inside(ref: Reference, hunk: Hunk) -> bool:
    return ref.path == hunk.path and hunk.new_start <= ref.line < hunk.new_start + hunk.new_len


def _signals_by_hunk(hunks: Sequence[Hunk]) -> dict[str, tuple[str, ...]]:
    by_hunk: dict[str, list[str]] = {}
    for signal, hunk_ids in detect_signals(hunks).items():
        for hunk_id in hunk_ids:
            by_hunk.setdefault(hunk_id, []).append(signal)
    return {hunk_id: tuple(signals) for hunk_id, signals in by_hunk.items()}


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
    """Render the packet: every lens and every hunk of the round in full."""
    return _render(ctx, lenses=None, assigned=None)


def render_packet_view(
    ctx: PacketContext, *, lenses: Collection[str], assigned_hunks: Collection[str]
) -> str:
    """Render a per-reviewer view: its lenses, its hunks in full and the other hunks as an index."""
    return _render(ctx, lenses=frozenset(lenses), assigned=frozenset(assigned_hunks))


def _render(
    ctx: PacketContext, *, lenses: frozenset[str] | None, assigned: frozenset[str] | None
) -> str:
    """Enclosing scopes are dropped, largest first, while the text exceeds its size budget."""
    preamble = _preamble(ctx, lenses)
    shown = [hunk for hunk in ctx.snap.hunks if assigned is None or hunk.id in assigned]
    owners = _Owners.of(ctx, shown)
    sections = {hunk.id: _hunk_section(ctx, hunk, owners, omit_scope=False) for hunk in shown}
    index = [_hunk_summary_line(ctx, hunk) for hunk in ctx.snap.hunks if hunk.id not in sections]
    excess = len(_assemble(preamble, sections, index)) - _char_budget(ctx, len(shown), assigned)
    for saved, hunk in _scope_savings(ctx, shown, owners):
        if excess <= 0:
            break
        sections[hunk.id] = _hunk_section(ctx, hunk, owners, omit_scope=True)
        excess -= saved
    return _assemble(preamble, sections, index)


def _char_budget(ctx: PacketContext, shown: int, assigned: frozenset[str] | None) -> int:
    """The full packet gets the whole budget; a view the share its hunks have of the round.

    The views of a round together then stay within what one full packet may hold.
    """
    if assigned is None:
        return MAX_PACKET_CHARS
    return MAX_PACKET_CHARS * max(1, shown) // max(1, len(ctx.snap.hunks))


def _preamble(ctx: PacketContext, lenses: frozenset[str] | None) -> str:
    parts = [
        _header(ctx),
        _intent(ctx),
        _conventions_section(ctx),
        _lenses_section(ctx, lenses),
        "## Recording cheat sheet\n" + "\n".join(CHEAT_SHEET),
    ]
    if ctx.round.signals:
        parts.append(_quality_signals_section(ctx))
    return "\n\n".join(parts)


def _assemble(preamble: str, sections: Mapping[str, str], index: Sequence[str]) -> str:
    hunks = "\n\n".join(sections.values()) if sections else "(no hunks assigned)"
    parts = [preamble, f"## Hunks\n\n{hunks}"]
    if index:
        parts.append("### Other hunks (index)\n" + "\n".join(index))
    return "\n\n".join(parts) + "\n"


def _quality_signals_section(ctx: PacketContext) -> str:
    lines = [
        "## Quality signals",
        "Every signal below must be resolved before `craftsman review check` passes: either "
        "record a finding with `craftsman review add ... --signal <ID>` or acknowledge it with "
        '`craftsman review ack <ID> --reason "<concrete reason, >= 40 chars>"`.',
        "",
    ]
    for sig in ctx.round.signals:
        lines.append(f"- **{sig.id}** (`{sig.kind.value}`) `{sig.file}:{sig.line}` — {sig.detail}")
    return "\n".join(lines)


@dataclass(frozen=True)
class _Owners:
    """The shown hunk that renders each shared scope and each symbol's references; others link to it."""

    scope: Mapping[tuple[str, str, int, int], str]
    symbol: Mapping[str, str]

    @classmethod
    def of(cls, ctx: PacketContext, shown: Sequence[Hunk]) -> _Owners:
        scope: dict[tuple[str, str, int, int], str] = {}
        symbol: dict[str, str] = {}
        for hunk in shown:
            if hunk.id in ctx.carried_from:
                continue
            found = ctx.scopes.get(hunk.id)
            if found is not None:
                scope.setdefault(_scope_key(hunk, found), hunk.id)
            for name in ctx.symbols.get(hunk.id, ()):
                symbol.setdefault(name, hunk.id)
        return cls(scope, symbol)


def _scope_key(hunk: Hunk, scope: Scope) -> tuple[str, str, int, int]:
    return (hunk.path, scope.name, scope.start, scope.end)


def _scope_savings(ctx: PacketContext, shown: Sequence[Hunk], owners: _Owners) -> list[tuple[int, Hunk]]:
    """Characters saved by omitting each rendered scope, largest first; savings of 0 are left out."""
    savings = []
    for hunk in shown:
        scope = ctx.scopes.get(hunk.id)
        if scope is None or owners.scope.get(_scope_key(hunk, scope)) != hunk.id:
            continue
        saved = len(_scope_block(hunk, scope)) - len(_omitted_scope_line(scope))
        if saved > 0:
            savings.append((saved, hunk))
    return sorted(savings, key=lambda item: item[0], reverse=True)


def _header(ctx: PacketContext) -> str:
    snap = ctx.snap
    carried = sum(hunk.carried for hunk in ctx.round.hunks)
    return (
        f"# Craftsman review packet — round {ctx.round.number}\n"
        f"Base `{snap.base_sha[:7]}` · fingerprint `{snap.fingerprint[:12]}` · tier `{ctx.round.tier}` · "
        f"{len(snap.files)} files, +{snap.added}/−{snap.deleted} lines, "
        f"{len(snap.hunks)} hunks ({carried} carried)"
    )


def _intent(ctx: PacketContext) -> str:
    task = ctx.task or "(not recorded — infer from commits and diff)"
    commits = (
        "\n".join(f"- {commit}" for commit in ctx.commits) or "- (none — the change is uncommitted)"
    )
    return f"## Intent\n**Task:** {task}\n\n**Commits since base:**\n{commits}"


def _conventions_section(ctx: PacketContext) -> str:
    listed = "\n".join(f"- {path}" for path in ctx.conventions) or "- (none found)"
    return f"## Conventions to read first\n{listed}"


def _lenses_section(ctx: PacketContext, only: frozenset[str] | None) -> str:
    rows = ["| Lens | Why selected | Focus |", "| :--- | :--- | :--- |"]
    for pick in ctx.picks:
        if only is not None and pick.lens.id not in only:
            continue
        reason = pick.reason
        if pick.lens.id == SWEEP_LENS:
            reason += " (run after all other lenses)"
        rows.append(f"| `{pick.lens.id}` — {pick.lens.title} | {reason} | {pick.lens.focus} |")
    return "## Lenses in this round\n" + "\n".join(rows)


def _hunk_summary_line(ctx: PacketContext, hunk: Hunk) -> str:
    scope = ctx.scopes.get(hunk.id)
    if scope is not None:
        label = scope.name
    elif hunk.id in ctx.carried_from:
        label = f"carried (round {ctx.carried_from[hunk.id]})"
    else:
        label = hunk.status
    return f"- {hunk.id} · {hunk.path} · {label}"


def _hunk_section(ctx: PacketContext, hunk: Hunk, owners: _Owners, *, omit_scope: bool) -> str:
    title = f"### {hunk.id} · `{hunk.path}`{_line_span(hunk)}"
    if hunk.id in ctx.carried_from:
        return f"{title} · carried (covered in round {ctx.carried_from[hunk.id]}, unchanged)"
    status = f"renamed from `{hunk.old_path}`" if hunk.old_path else hunk.status
    signals = ctx.signals_by_hunk.get(hunk.id, ())
    title += f" · {status}" + (f" · signals: {', '.join(signals)}" if signals else "")
    mentions = ctx.reopened.get(hunk.id, ())
    if mentions:
        title += " · reopened: mentions " + ", ".join(
            f"{symbol} (changed in {owner})" for symbol, owner in mentions
        )
    lines = [title]
    scope = ctx.scopes.get(hunk.id)
    owner = owners.scope.get(_scope_key(hunk, scope), hunk.id) if scope is not None else hunk.id
    if scope is not None:
        if owner != hunk.id:
            lines.append(f"Scope: `{scope.name}` — shown at {owner}")
        else:
            lines.append(f"Scope: `{scope.name}` ({scope.kind}, L{scope.start}–L{scope.end})")
    header = f"@@ -{hunk.old_start},{hunk.old_len} +{hunk.new_start},{hunk.new_len} @@ {hunk.header}"
    lines.append(_fenced("diff", [header.rstrip(), *hunk.lines]))
    if scope is not None and owner == hunk.id:
        lines.append(_omitted_scope_line(scope) if omit_scope else _scope_block(hunk, scope))
    lines.extend(_symbol_lines(ctx, hunk, owners))
    return "\n".join(lines)


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


def _scope_block(hunk: Hunk, scope: Scope) -> str:
    raw_lines = scope.text.split("\n")
    hunk_end = hunk.new_start + max(1, hunk.new_len) - 1
    if hunk.new_len > 10 and scope.start >= hunk.new_start and scope.end <= hunk_end:
        return f"Enclosing scope ({hunk.path}:{scope.start}-{scope.end}) — shown in diff above"
    if len(raw_lines) <= MAX_SCOPE_LINES:
        fenced = _fenced(_fence_language(hunk.path), raw_lines)
        return f"Enclosing scope (L{scope.start}–L{scope.end}):\n{fenced}"
    fenced = _fenced(_fence_language(hunk.path), _trimmed_scope(hunk, scope, raw_lines))
    return f"Enclosing scope ({hunk.path}:{scope.start}-{scope.end}):\n{fenced}"


def _trimmed_scope(hunk: Hunk, scope: Scope, raw_lines: Sequence[str]) -> list[str]:
    """The scope's signature and the lines around the hunk; the gaps name the lines left out."""
    sig_end = _signature_line_count(raw_lines)
    center = max(scope.start, hunk.new_start) - scope.start
    ctx_lo = max(sig_end, center - SCOPE_CONTEXT_RADIUS)
    ctx_hi = min(len(raw_lines), center + 1 + SCOPE_CONTEXT_RADIUS)
    trimmed = list(raw_lines[:sig_end])
    if ctx_lo > sig_end:
        trimmed.append(_omission_marker(scope.start + sig_end, scope.start + ctx_lo - 1))
    trimmed.extend(raw_lines[ctx_lo:ctx_hi])
    if ctx_hi < len(raw_lines):
        trimmed.append(_omission_marker(scope.start + ctx_hi, scope.end))
    return trimmed


def _omission_marker(first: int, last: int) -> str:
    # Not a comment of any one language: the block may hold Python, Go, SQL or YAML.
    return f"[… L{first}–L{last} omitted …]"


def _omitted_scope_line(scope: Scope) -> str:
    return f"Enclosing scope omitted for size — open {scope.path}:L{scope.start}–L{scope.end}"


def _symbol_lines(ctx: PacketContext, hunk: Hunk, owners: _Owners) -> list[str]:
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
            listed = " · ".join(
                f"{_inline_code(f'{ref.path}:{ref.line}')} {_inline_code(ref.text)}" for ref in refs
            )
            lines.append(f"- `{symbol}`: {listed or '(no other references found)'}")
            test_files = ctx.test_files_by_symbol.get(symbol, ())
            if test_files:
                lines.append(f"Tests referencing `{symbol}`: " + ", ".join(f"`{p}`" for p in test_files))
    open_ids = [h.id for h in ctx.snap.hunks if h.id not in ctx.carried_from]
    if ctx.last_runs and (len(open_ids) <= 5 or (open_ids and hunk.id == open_ids[0])):
        lines.append("Last runs: " + " · ".join(ctx.last_runs))
    for fact in ctx.facts_by_hunk.get(hunk.id, ()):
        lines.append(f"Facts: {fact.id} {fact.text} (source {fact.source.path}:{fact.source.line})")
    return lines


def _fence_language(path: str) -> str:
    return _FENCE_LANGUAGES.get(PurePosixPath(path).suffix.lower(), "")


def _inline_code(text: str) -> str:
    """Wrap `text` in a code span that backticks inside it cannot close early (CommonMark)."""
    longest = max((len(run) for run in _BACKTICK_RUN_RE.findall(text)), default=0)
    fence = "`" * (longest + 1)
    pad = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{fence}{pad}{text}{pad}{fence}"


def _fenced(language: str, lines: Sequence[str]) -> str:
    """Fence `lines` with more backticks than any run inside them so the block cannot break."""
    longest = max((len(run) for line in lines for run in _BACKTICK_RUN_RE.findall(line)), default=0)
    fence = "`" * max(3, longest + 1)
    return "\n".join([f"{fence}{language}", *lines, fence])
