"""Review packet: one Markdown file per round that indexes everything a reviewer must read.

`collect_context` performs the git and file I/O; `render_packet` is a pure function of the
collected context so its layout can be tested without a repository.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
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
    git_output,
)
from craftsman.ledger import Ledger, Round, reopened_mentions
from craftsman.lenses import LensPick
from craftsman.quality import QualitySignal, collect_quality_signals

MAX_PACKET_CHARS = 350_000
MAX_SCOPE_LINES = 60
SCOPE_CONTEXT_RADIUS = 15
MAX_COMMITS = 50
MAX_REFERENCES_PER_SYMBOL = 8
REFERENCE_SEARCH_LIMIT = 40
SWEEP_LENS = "sweep"

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
    'H2 g: callers a.py:19 ok"`',
    "- Finding (one per root cause; for >1 entry, prefer `--from-json - <<'EOF'`; group unjudged "
    "duplicates before verification with `craftsman review merge F1 F2` — Merge only when ONE fix "
    "removes every consequence. Different fixes are separate findings, even on the same line, file, "
    "or category, e.g. ordering entries in `AUTHORS` and a missing changelog entry are two separate "
    "findings, not one 'housekeeping' finding):\n"
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
    root: Path
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
    reopened: Mapping[str, tuple[tuple[str, str], ...]] = field(default_factory=dict)
    quality_signals: tuple[QualitySignal, ...] = ()


def collect_context(
    root: Path,
    snap: DiffSnapshot,
    round_: Round,
    picks: Sequence[LensPick],
    task: str,
    *,
    symbols: Mapping[str, tuple[str, ...]],
    ledger: Ledger,
    index: WorktreeIndex | None = None,
    quality_signals: Sequence[QualitySignal] | None = None,
) -> PacketContext:
    """Gather commits, conventions, scopes and references for the hunks under review.

    Pass the `index` that computed `symbols` so work tree files are read only once.
    """
    if index is None:
        index = WorktreeIndex(root)
    if quality_signals is None:
        quality_signals = collect_quality_signals(root, snap)
    carried = {hunk.id for hunk in round_.hunks if hunk.carried}
    open_hunks = [hunk for hunk in snap.hunks if hunk.id not in carried]
    open_symbols = {hunk.id: symbols.get(hunk.id, ()) for hunk in open_hunks}
    return PacketContext(
        root=root,
        snap=snap,
        round=round_,
        picks=tuple(picks),
        task=task.strip(),
        commits=_commits(root, snap.base_sha),
        conventions=_conventions(root, snap.files),
        scopes={hunk.id: index.hunk_scope(hunk) for hunk in open_hunks},
        symbols=open_symbols,
        references=_references(index, open_hunks, open_symbols),
        signals_by_hunk=_signals_by_hunk(snap.hunks),
        carried_from=_carried_from(ledger, round_),
        reopened=reopened_mentions(ledger.coverage, round_, snap.hunks, symbols),
        quality_signals=tuple(quality_signals),
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


def render_packet(ctx: PacketContext) -> str:
    """Render the packet; enclosing scopes are dropped, largest first, if it grows too large."""
    parts = [
        _header(ctx),
        _intent(ctx),
        _conventions_section(ctx),
        _lenses_section(ctx),
        "## Recording cheat sheet\n" + "\n".join(CHEAT_SHEET),
    ]
    if ctx.quality_signals or ctx.round.signals:
        parts.append(_quality_signals_section(ctx))
    preamble = "\n\n".join(parts)
    omitted = _scopes_to_omit(ctx, len(preamble))
    return f"{preamble}\n\n{_hunks_section(ctx, omitted)}\n"


def _quality_signals_section(ctx: PacketContext) -> str:
    signals = ctx.quality_signals or ctx.round.signals
    lines = [
        "## Quality signals",
        "Every signal below must be resolved before `craftsman review check` passes: either "
        "record a finding with `craftsman review add ... --signal <ID>` or acknowledge it with "
        '`craftsman review ack <ID> --reason "<concrete reason, >= 40 chars>"`.',
        "",
    ]
    for sig in signals:
        kind = sig.kind.value if hasattr(sig.kind, "value") else sig.kind
        lines.append(f"- **{sig.id}** (`{kind}`) `{sig.file}:{sig.line}` — {sig.detail}")
    return "\n".join(lines)


def _first_hunk_by_scope(ctx: PacketContext) -> dict[tuple[str, str, int, int], str]:
    first: dict[tuple[str, str, int, int], str] = {}
    for hunk in ctx.snap.hunks:
        if hunk.id in ctx.carried_from:
            continue
        scope = ctx.scopes.get(hunk.id)
        if scope is not None:
            first.setdefault((hunk.path, scope.name, scope.start, scope.end), hunk.id)
    return first


def _scopes_to_omit(ctx: PacketContext, preamble_chars: int) -> frozenset[str]:
    first_by_scope = _first_hunk_by_scope(ctx)
    excess = preamble_chars + len(_hunks_section(ctx, frozenset())) + 3 - MAX_PACKET_CHARS
    savings = sorted(
        (
            (len(_scope_block(hunk, scope)) - len(_omitted_scope_line(scope)), hunk.id)
            for hunk in ctx.snap.hunks
            if (scope := ctx.scopes.get(hunk.id)) is not None
            and first_by_scope.get((hunk.path, scope.name, scope.start, scope.end)) == hunk.id
        ),
        reverse=True,
    )
    omitted: set[str] = set()
    for saved, hunk_id in savings:
        if excess <= 0 or saved <= 0:  # sorted: no later scope saves anything either
            break
        omitted.add(hunk_id)
        excess -= saved
    return frozenset(omitted)


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


def _lenses_section(ctx: PacketContext) -> str:
    rows = ["| Lens | Why selected | Focus |", "| :--- | :--- | :--- |"]
    for pick in ctx.picks:
        reason = pick.reason
        if pick.lens.id == SWEEP_LENS:
            reason += " (run after all other lenses)"
        rows.append(f"| `{pick.lens.id}` — {pick.lens.title} | {reason} | {pick.lens.focus} |")
    return "## Lenses in this round\n" + "\n".join(rows)


def _first_hunk_by_symbol(ctx: PacketContext) -> dict[str, str]:
    first: dict[str, str] = {}
    for hunk in ctx.snap.hunks:
        if hunk.id in ctx.carried_from:
            continue
        for symbol in ctx.symbols.get(hunk.id, ()):
            first.setdefault(symbol, hunk.id)
    return first


def _hunks_section(ctx: PacketContext, omitted: frozenset[str]) -> str:
    first_by_scope = _first_hunk_by_scope(ctx)
    first_by_symbol = _first_hunk_by_symbol(ctx)
    sections = [
        _hunk_section(ctx, hunk, omitted, first_by_scope, first_by_symbol)
        for hunk in ctx.snap.hunks
    ]
    return "## Hunks\n\n" + "\n\n".join(sections)


def _hunk_section(
    ctx: PacketContext,
    hunk: Hunk,
    omitted: frozenset[str],
    first_by_scope: Mapping[tuple[str, str, int, int], str] | None = None,
    first_by_symbol: Mapping[str, str] | None = None,
) -> str:
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
    first_id = (
        first_by_scope.get((hunk.path, scope.name, scope.start, scope.end), hunk.id)
        if scope is not None and first_by_scope is not None
        else hunk.id
    )
    if scope is not None:
        if first_id != hunk.id:
            lines.append(f"Scope: `{scope.name}` — shown at {first_id}")
        else:
            lines.append(f"Scope: `{scope.name}` ({scope.kind}, L{scope.start}–L{scope.end})")
    header = f"@@ -{hunk.old_start},{hunk.old_len} +{hunk.new_start},{hunk.new_len} @@ {hunk.header}"
    lines.append(_fenced("diff", [header.rstrip(), *hunk.lines]))
    if scope is not None and first_id == hunk.id:
        omit = hunk.id in omitted
        lines.append(_omitted_scope_line(scope) if omit else _scope_block(hunk, scope))
    lines.extend(_symbol_lines(ctx, hunk, first_by_symbol))
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
    sig_end = _signature_line_count(raw_lines)
    h_start = max(scope.start, hunk.new_start)
    center = h_start - scope.start
    ctx_lo = max(sig_end, center - SCOPE_CONTEXT_RADIUS)
    ctx_hi = min(len(raw_lines), center + 1 + SCOPE_CONTEXT_RADIUS)
    trimmed = list(raw_lines[:sig_end])
    if ctx_lo > sig_end:
        trimmed.append(f"    # ... L{scope.start + sig_end}–L{scope.start + ctx_lo - 1} omitted ...")
    trimmed.extend(raw_lines[ctx_lo:ctx_hi])
    if ctx_hi < len(raw_lines):
        trimmed.append(f"    # ... L{scope.start + ctx_hi}–L{scope.end} omitted ...")
    fenced = _fenced(_fence_language(hunk.path), trimmed)
    return f"Enclosing scope ({hunk.path}:{scope.start}-{scope.end}):\n{fenced}"


def _omitted_scope_line(scope: Scope) -> str:
    return f"Enclosing scope omitted for size — open {scope.path}:L{scope.start}–L{scope.end}"


def _symbol_lines(
    ctx: PacketContext, hunk: Hunk, first_by_symbol: Mapping[str, str] | None = None
) -> list[str]:
    symbols = ctx.symbols.get(hunk.id, ())
    if not symbols:
        return []
    lines = ["Defines/changes: " + ", ".join(f"`{symbol}`" for symbol in symbols), "References:"]
    for symbol in symbols:
        first_id = first_by_symbol.get(symbol, hunk.id) if first_by_symbol is not None else hunk.id
        if first_id != hunk.id:
            lines.append(f"- `{symbol}`: (shown at {first_id})")
            continue
        refs = ctx.references.get(symbol, ())
        listed = " · ".join(
            f"{_inline_code(f'{ref.path}:{ref.line}')} {_inline_code(ref.text)}" for ref in refs
        )
        lines.append(f"- `{symbol}`: {listed or '(no other references found)'}")
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
