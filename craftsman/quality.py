"""Deterministic quality signals the CLI computes from the diff and the Python syntax trees."""

from __future__ import annotations

import ast
import math
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from craftsman.diffscan import DiffScanError, DiffSnapshot, Hunk, is_docs_path, is_test_path
from craftsman.records import QualitySignal, SignalKind

# Functions longer than 40 lines mix multiple responsibilities or levels of abstraction.
MAX_FUNCTION_LINES = 40
# Modules over 400 lines become hard to navigate as a single cohesive unit.
MAX_MODULE_LINES = 400
# Growing an existing large module by more than 150 lines signals a missing domain boundary.
MAX_MODULE_GROWTH = 150
# Test names over 70 characters usually pack multiple behaviors or implementation details into one test.
MAX_TEST_NAME_LEN = 70
# Sharing at least 80% of normalized statements indicates one decision implemented in two places.
DUPLICATE_STATEMENT_RATIO = 0.80
# Short helpers under 5 statements often share boilerplate without duplicating domain logic.
MIN_DUPLICATE_STATEMENTS = 5
# Changes adding more than 800 production lines exceed the reviewable cognitive budget for a single PR.
MAX_PRODUCTION_ADDED_LINES = 800
# The diff-size signal names this many of the fastest-growing production files.
TOP_GROWTH_FILES = 5

FORBIDDEN_TEST_NAME_RE = re.compile(r"(_v\d+_\d+|_\d+_of_\d+|table_of_\d+|_[A-Za-z]\d+(?:_|$)|_and_)")
# Converters legitimately take and return plain dictionaries.
SERIALIZATION_NAMES = frozenset(
    {"to_json", "from_json", "to_dict", "from_dict", "to_state", "from_state"}
)
DICT_TYPE_RE = re.compile(r"\b(?:dict|typing\.Dict|Dict)\s*(?:\[|$)")
_STMT_TYPES = (
    ast.Assign,
    ast.AnnAssign,
    ast.AugAssign,
    ast.Return,
    ast.If,
    ast.For,
    ast.While,
    ast.Raise,
    ast.With,
)
_BLOCK_FIELDS = ("body", "orelse")

RawSignal = tuple[SignalKind, str, int, str]


@dataclass(frozen=True)
class _ParsedFunction:
    path: str
    name: str
    lineno: int
    end_lineno: int
    is_new: bool
    is_touched: bool
    node: ast.FunctionDef | ast.AsyncFunctionDef
    body_dump: str
    statements: frozenset[str]

    @property
    def is_test(self) -> bool:
        return self.name.startswith("test_")


def is_production_path(rel_path: str) -> bool:
    return not is_test_path(rel_path) and not is_docs_path(rel_path)


def production_diff_growth(snap: DiffSnapshot) -> tuple[int, tuple[tuple[str, int], ...]]:
    """Return `(total_added_production_lines, top_files_by_added_lines)` for `snap`."""
    added_by_file: dict[str, int] = {}
    for hunk in snap.hunks:
        if is_production_path(hunk.path):
            added = sum(1 for line in hunk.lines if line.startswith("+"))
            added_by_file[hunk.path] = added_by_file.get(hunk.path, 0) + added
    ranked = sorted(added_by_file.items(), key=lambda item: (-item[1], item[0]))
    return sum(added_by_file.values()), tuple(ranked[:TOP_GROWTH_FILES])


@dataclass(frozen=True)
class _FileChanges:
    touched: frozenset[int]  # new-file line numbers of added lines
    removed: str  # the removed lines, to tell a new definition from a changed one
    growth: int  # added minus removed lines


def _changes_by_file(hunks: Sequence[Hunk]) -> dict[str, _FileChanges]:
    touched: dict[str, set[int]] = {}
    removed: dict[str, list[str]] = {}
    growth: dict[str, int] = {}
    for hunk in hunks:
        added, removed_lines = hunk.added_lines, hunk.removed_lines
        touched.setdefault(hunk.path, set()).update(number for number, _ in added)
        removed.setdefault(hunk.path, []).extend(text for _, text in removed_lines)
        growth[hunk.path] = growth.get(hunk.path, 0) + len(added) - len(removed_lines)
    return {
        path: _FileChanges(frozenset(touched[path]), "\n".join(removed[path]), growth[path])
        for path in touched
    }


def _shape(node: ast.AST, *, header_only: bool = False) -> str:
    """The structure of `node` with every name erased, so code differing only in names compares equal.

    With `header_only`, nested blocks are left out: the statement's own decision is compared, not
    its body. Nothing is copied, which keeps this linear in the size of the tree.
    """
    if isinstance(node, ast.Name):
        return "Name"
    parts = [type(node).__name__]
    for name, value in ast.iter_fields(node):
        if name in ("ctx", "type_comment") or (name == "arg" and isinstance(node, ast.arg)):
            continue
        if header_only and name in _BLOCK_FIELDS:
            parts.append(f"{name}={'[...]' if value else '[]'}")  # whether there is an else counts
            continue
        parts.append(f"{name}={_shape_value(value)}")
    return "(" + " ".join(parts) + ")"


def _shape_value(value: object) -> str:
    if isinstance(value, ast.AST):
        return _shape(value)
    if isinstance(value, list):
        return "[" + ",".join(_shape_value(item) for item in value) + "]"
    return repr(value)


def _normalize_function(node: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[str, frozenset[str]]:
    body = [
        statement
        for statement in node.body
        if not (isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant))
    ]
    body_dump = "|".join(_shape(statement) for statement in body) if len(body) >= 2 else ""
    statements = frozenset(
        _shape(child, header_only=True) for child in ast.walk(node) if isinstance(child, _STMT_TYPES)
    )
    return body_dump, statements


def _test_name_signal(fn: _ParsedFunction) -> RawSignal | None:
    match = FORBIDDEN_TEST_NAME_RE.search(fn.name)
    if match:
        reason = f"contains forbidden token `{match.group(0)}`"
    elif len(fn.name) > MAX_TEST_NAME_LEN:
        reason = f"is {len(fn.name)} characters long (budget: <= {MAX_TEST_NAME_LEN})"
    else:
        return None
    return (SignalKind.TEST_NAME, fn.path, fn.lineno, f"`{fn.name}` {reason}")


def _long_function_signal(fn: _ParsedFunction) -> RawSignal | None:
    span = fn.end_lineno - fn.lineno + 1
    if span <= MAX_FUNCTION_LINES or is_test_path(fn.path):
        return None
    detail = f"`{fn.name}` spans {span} lines (budget: <= {MAX_FUNCTION_LINES})"
    return (SignalKind.LONG_FUNCTION, fn.path, fn.lineno, detail)


def _dict_domain_signal(fn: _ParsedFunction) -> RawSignal | None:
    if (
        not fn.is_new
        or fn.name.startswith("_")
        or fn.name in SERIALIZATION_NAMES
        or is_test_path(fn.path)
    ):
        return None
    args = fn.node.args
    annotations = [
        a.annotation for a in (*args.posonlyargs, *args.args, *args.kwonlyargs) if a.annotation
    ]
    if fn.node.returns is not None:
        annotations.append(fn.node.returns)
    for annotation in annotations:
        text = ast.unparse(annotation)
        if DICT_TYPE_RE.search(text):
            detail = f"public function `{fn.name}` uses `{text}` instead of a domain type"
            return (SignalKind.DICT_DOMAIN, fn.path, fn.lineno, detail)
    return None


def _function_signals(fn: _ParsedFunction) -> Iterator[RawSignal]:
    if not fn.is_touched:
        return
    checks = (_test_name_signal,) if fn.is_test else (_long_function_signal, _dict_domain_signal)
    for check in checks:
        found = check(fn)
        if found is not None:
            yield found


def _duplicate_signals(functions: Sequence[_ParsedFunction]) -> Iterator[RawSignal]:
    """Pairs of functions with the same body or most statements in common, one of them changed.

    Earlier functions are indexed by body and by statement. A pair can only qualify when it shares
    one of the rarest statements of the later function (see `_rare_statements`), so statements
    that nearly every function has, such as `return _`, never make every pair a candidate.
    """
    by_body: dict[str, list[int]] = {}
    by_statement: dict[str, list[int]] = {}
    for index, fn_b in enumerate(functions):
        if fn_b.is_test:
            continue
        candidates = set(by_body.get(fn_b.body_dump, ())) if fn_b.body_dump else set()
        if len(fn_b.statements) >= MIN_DUPLICATE_STATEMENTS:
            for statement in _rare_statements(fn_b, by_statement):
                candidates.update(by_statement.get(statement, ()))
        for earlier in sorted(candidates):
            fn_a = functions[earlier]
            if not (fn_a.is_touched or fn_b.is_touched):
                continue
            share = _shared_share(fn_a, fn_b)
            if share is not None:
                yield (
                    SignalKind.DUPLICATE_LOGIC,
                    fn_b.path,
                    fn_b.lineno,
                    f"`{fn_b.name}` duplicates `{fn_a.name}` at `{fn_a.path}:{fn_a.lineno}` "
                    f"({share}% shared AST statements)",
                )
        if fn_b.body_dump:
            by_body.setdefault(fn_b.body_dump, []).append(index)
        for statement in fn_b.statements:
            by_statement.setdefault(statement, []).append(index)


def _rare_statements(fn: _ParsedFunction, by_statement: dict[str, list[int]]) -> list[str]:
    """The statements of `fn` that any statement-based duplicate must share at least one of.

    A duplicate shares at least 80% of the smaller function, which is at least half the larger,
    so at least `need` = 40% of `fn`'s statements; of any `len - need + 1` of them one is shared.
    """
    need = math.ceil(DUPLICATE_STATEMENT_RATIO / 2 * len(fn.statements))
    ranked = sorted(fn.statements, key=lambda statement: len(by_statement.get(statement, ())))
    return ranked[: len(ranked) - need + 1]


def _shared_share(fn_a: _ParsedFunction, fn_b: _ParsedFunction) -> int | None:
    """The percentage of statements the two share when it marks a duplicate, else None."""
    if fn_a.body_dump and fn_a.body_dump == fn_b.body_dump:
        return 100
    smaller = min(len(fn_a.statements), len(fn_b.statements))
    larger = max(len(fn_a.statements), len(fn_b.statements))
    if smaller < MIN_DUPLICATE_STATEMENTS or larger > 2 * smaller:
        return None
    ratio = len(fn_a.statements & fn_b.statements) / smaller
    return round(ratio * 100) if ratio >= DUPLICATE_STATEMENT_RATIO else None


def _parse_file_functions(rel_path: str, tree: ast.AST, changes: _FileChanges) -> list[_ParsedFunction]:
    nodes = sorted(
        (n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))),
        key=lambda n: n.lineno,
    )
    result: list[_ParsedFunction] = []
    for node in nodes:
        end_lineno = node.end_lineno or node.lineno
        is_touched = any(line in changes.touched for line in range(node.lineno, end_lineno + 1))
        is_new = node.lineno in changes.touched and f"def {node.name}(" not in changes.removed
        body_dump, statements = _normalize_function(node)
        result.append(
            _ParsedFunction(
                rel_path,
                node.name,
                node.lineno,
                end_lineno,
                is_new,
                is_touched,
                node,
                body_dump,
                statements,
            )
        )
    return result


@dataclass(frozen=True)
class _PythonFile:
    source: str
    tree: ast.Module


def _parse_python(root: Path, rel_path: str) -> _PythonFile | RawSignal:
    """The source and syntax tree of a changed Python file, or a signal that it does not parse."""
    try:
        source = (root / rel_path).read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        return (SignalKind.PARSE_ERROR, rel_path, 1, f"`{rel_path}` is not UTF-8 ({exc.reason})")
    except OSError as exc:
        raise DiffScanError(f"cannot read {rel_path}: {exc}") from exc
    try:
        return _PythonFile(source, ast.parse(source, filename=rel_path))
    except SyntaxError as exc:
        line = exc.lineno or 1
        return (SignalKind.PARSE_ERROR, rel_path, line, f"`{rel_path}` does not parse: {exc.msg}")
    except (ValueError, RecursionError) as exc:  # NUL bytes; nesting too deep for the parser
        return (SignalKind.PARSE_ERROR, rel_path, 1, f"`{rel_path}` does not parse: {exc}")


def _large_module_signal(rel_path: str, source: str, growth: int) -> RawSignal | None:
    total = len(source.splitlines())
    if not is_production_path(rel_path) or total <= MAX_MODULE_LINES or growth <= MAX_MODULE_GROWTH:
        return None
    detail = (
        f"`{rel_path}` has {total} lines (> {MAX_MODULE_LINES}) and grew by +{growth} lines "
        f"(> {MAX_MODULE_GROWTH})"
    )
    return (SignalKind.LARGE_MODULE, rel_path, 1, detail)


def _diff_size_signal(snap: DiffSnapshot) -> RawSignal | None:
    total, top = production_diff_growth(snap)
    if total <= MAX_PRODUCTION_ADDED_LINES:
        return None
    files = ", ".join(f"{path} (+{count})" for path, count in top)
    detail = (
        f"+{total} production lines added (budget: <= {MAX_PRODUCTION_ADDED_LINES}); top files: {files}"
    )
    return (SignalKind.DIFF_SIZE, top[0][0], 1, detail)


def _is_work_tree_file(root: Path, rel_path: str) -> bool:
    """A regular file inside the work tree; a symlink to code elsewhere is not this change's code."""
    path = root / rel_path
    return path.is_file() and path.resolve().is_relative_to(root.resolve())


def collect_quality_signals(root: Path, snap: DiffSnapshot) -> tuple[QualitySignal, ...]:
    """The quality signals of the Python files changed in `snap`, as found in the work tree `root`."""
    changes = _changes_by_file(snap.hunks)
    raw: list[RawSignal] = []
    functions: list[_ParsedFunction] = []
    for rel_path in snap.files:
        if PurePosixPath(rel_path).suffix.lower() != ".py" or not _is_work_tree_file(root, rel_path):
            continue
        parsed = _parse_python(root, rel_path)
        if not isinstance(parsed, _PythonFile):
            raw.append(parsed)
            continue
        file_changes = changes[rel_path]
        module_signal = _large_module_signal(rel_path, parsed.source, file_changes.growth)
        if module_signal is not None:
            raw.append(module_signal)
        try:
            parsed_functions = _parse_file_functions(rel_path, parsed.tree, file_changes)
        except RecursionError:  # an expression nested too deeply to compare
            raw.append(
                (SignalKind.PARSE_ERROR, rel_path, 1, f"`{rel_path}` is nested too deeply to check")
            )
            continue
        for fn in parsed_functions:
            functions.append(fn)
            raw.extend(_function_signals(fn))
    raw.extend(_duplicate_signals(functions))
    size = _diff_size_signal(snap)
    if size is not None:
        raw.append(size)
    return tuple(
        QualitySignal(f"S{index}", kind, path, line, detail)
        for index, (kind, path, line, detail) in enumerate(raw, 1)
    )
