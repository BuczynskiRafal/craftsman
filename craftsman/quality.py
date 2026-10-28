"""Deterministic AST and diff quality signals computed by the CLI (`craftsman/quality.py`)."""

from __future__ import annotations

import ast
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Any

from craftsman.diffscan import DiffSnapshot, Hunk

_DOC_SUFFIXES = frozenset({".md", ".rst", ".txt", ".adoc"})

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

FORBIDDEN_TEST_NAME_RE = re.compile(r"(_v\d+_\d+|_\d+_of_\d+|table_of_\d+|_[A-Za-z]\d+(?:_|$)|_and_)")
SERIALIZATION_NAMES = frozenset({"to_json", "from_json", "to_dict", "from_dict", "to_state", "from_state"})
SERIALIZATION_MODULES = frozenset({"serde.py", "serialization.py", "json_codec.py"})
DICT_TYPE_RE = re.compile(r"\b(?:dict|typing\.Dict|Dict)(?:\s*\[\s*str\s*,\s*(?:typing\.)?(?:Any|object)\s*\]|(?!\s*\[)\b)")
_STMT_TYPES = (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.Return, ast.If, ast.For, ast.While, ast.Raise, ast.With)


class SignalKind(str, Enum):
    LONG_FUNCTION = "long-function"
    LARGE_MODULE = "large-module"
    TEST_NAME = "test-name"
    DUPLICATE_LOGIC = "duplicate-logic"
    DICT_DOMAIN = "dict-domain"
    DIFF_SIZE = "diff-size"


@dataclass(frozen=True)
class QualitySignal:
    id: str
    kind: SignalKind
    file: str
    line: int
    detail: str

    def to_json(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind.value, "file": self.file, "line": self.line, "detail": self.detail}

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> QualitySignal:
        return cls(str(raw["id"]), SignalKind(str(raw["kind"])), str(raw["file"]), int(raw["line"]), str(raw["detail"]))


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


def is_test_path(rel_path: str) -> bool:
    p = PurePosixPath(rel_path)
    return bool({"tests", "test"} & set(p.parts)) or p.name.startswith("test_") or p.name.endswith("_test.py") or ".test." in p.name


def is_doc_path(rel_path: str) -> bool:
    p = PurePosixPath(rel_path)
    return p.suffix.lower() in _DOC_SUFFIXES or (len(p.parts) > 1 and p.parts[0] == "docs")


def is_production_path(rel_path: str) -> bool:
    return not is_test_path(rel_path) and not is_doc_path(rel_path)


def production_diff_growth(snap: DiffSnapshot) -> tuple[int, tuple[tuple[str, int], ...]]:
    """Return `(total_added_production_lines, top_files_by_added_lines)` for `snap`."""
    added_by_file: dict[str, int] = {}
    for hunk in snap.hunks:
        if is_production_path(hunk.path):
            added = sum(1 for line in hunk.lines if line.startswith("+"))
            added_by_file[hunk.path] = added_by_file.get(hunk.path, 0) + added
    top_files = tuple(sorted(added_by_file.items(), key=lambda item: (-item[1], item[0]))[:5])
    return sum(added_by_file.values()), top_files


def _changed_lines_by_file(hunks: Sequence[Hunk]) -> tuple[dict[str, set[int]], dict[str, str], dict[str, int]]:
    touched: dict[str, set[int]] = {}
    removed: dict[str, list[str]] = {}
    growth: dict[str, int] = {}
    for hunk in hunks:
        lines_set = touched.setdefault(hunk.path, set())
        rem_list = removed.setdefault(hunk.path, [])
        cur = hunk.new_start
        for raw in hunk.lines:
            if raw.startswith("+"):
                lines_set.add(cur)
                cur += 1
                growth[hunk.path] = growth.get(hunk.path, 0) + 1
            elif raw.startswith("-"):
                rem_list.append(raw[1:])
                growth[hunk.path] = growth.get(hunk.path, 0) - 1
            elif raw.startswith(" "):
                cur += 1
    return touched, {k: "\n".join(v) for k, v in removed.items()}, growth


class _AstNormalizer(ast.NodeTransformer):
    def visit_Name(self, node: ast.Name) -> ast.AST:
        return ast.Name(id="_", ctx=node.ctx)

    def visit_arg(self, node: ast.arg) -> ast.AST:
        return ast.arg(arg="_")

    def visit_Return(self, node: ast.Return) -> ast.AST:
        self.generic_visit(node)
        if isinstance(node.value, ast.Tuple) and node.value.elts:
            return ast.Return(value=node.value.elts[0])
        return node


def _normalize_function(node: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[str, frozenset[str]]:
    stmts = [
        s for s in node.body
        if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant) and isinstance(s.value.value, str))
        and not isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    norm = _AstNormalizer()
    body_dump = "|".join(ast.dump(norm.visit(ast.parse(ast.unparse(s)).body[0])) for s in stmts) if len(stmts) >= 2 else ""
    flat: set[str] = set()
    queue: list[ast.AST] = list(stmts)
    while queue:
        child = queue.pop()
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if isinstance(child, _STMT_TYPES):
            copy_node = ast.parse(ast.unparse(child)).body[0]
            if hasattr(copy_node, "body"):
                copy_node.body = [ast.Pass()]
            if hasattr(copy_node, "orelse") and copy_node.orelse:
                copy_node.orelse = [ast.Pass()]
            flat.add(ast.dump(norm.visit(copy_node)))
        queue.extend(ast.iter_child_nodes(child))
    return body_dump, frozenset(flat)


def _check_function_signals(fn: _ParsedFunction, out: list[tuple[SignalKind, str, int, str]]) -> None:
    if not fn.is_touched:
        return
    if fn.name.startswith("test_"):
        match = FORBIDDEN_TEST_NAME_RE.search(fn.name)
        reason = f"contains forbidden token `{match.group(0)}`" if match else (
            f"is {len(fn.name)} characters long (budget: <= {MAX_TEST_NAME_LEN})" if len(fn.name) > MAX_TEST_NAME_LEN else None
        )
        if reason:
            out.append((SignalKind.TEST_NAME, fn.path, fn.lineno, f"`{fn.name}` {reason}"))
        return
    span = fn.end_lineno - fn.lineno
    if span > MAX_FUNCTION_LINES and not is_test_path(fn.path):
        out.append((SignalKind.LONG_FUNCTION, fn.path, fn.lineno, f"`{fn.name}` spans {span} lines (budget: <= {MAX_FUNCTION_LINES})"))
    if fn.is_new and not fn.name.startswith("_") and fn.name not in SERIALIZATION_NAMES and PurePosixPath(fn.path).name not in SERIALIZATION_MODULES and not is_test_path(fn.path):
        annos = [a.annotation for a in (*fn.node.args.posonlyargs, *fn.node.args.args, *fn.node.args.kwonlyargs) if a.annotation]
        if fn.node.returns is not None:
            annos.append(fn.node.returns)
        for ann in annos:
            text = ast.unparse(ann)
            if DICT_TYPE_RE.search(text):
                out.append((SignalKind.DICT_DOMAIN, fn.path, fn.lineno, f"public function `{fn.name}` uses `{text}` instead of a domain type"))
                break


def _check_duplicate_logic(functions: Sequence[_ParsedFunction], out: list[tuple[SignalKind, str, int, str]]) -> None:
    for idx, fn_a in enumerate(functions):
        for fn_b in functions[idx + 1 :]:
            if not (fn_a.is_touched or fn_b.is_touched) or fn_a.name.startswith("test_") or fn_b.name.startswith("test_"):
                continue
            same_body = bool(fn_a.body_dump and fn_a.body_dump == fn_b.body_dump)
            min_len, max_len = min(len(fn_a.statements), len(fn_b.statements)), max(len(fn_a.statements), len(fn_b.statements))
            ratio = (len(fn_a.statements & fn_b.statements) / min_len) if min_len >= MIN_DUPLICATE_STATEMENTS and max_len <= 2 * min_len else 0.0
            if same_body or ratio >= DUPLICATE_STATEMENT_RATIO:
                pct = 100 if same_body else int(round(ratio * 100))
                out.append((SignalKind.DUPLICATE_LOGIC, fn_b.path, fn_b.lineno, f"`{fn_b.name}` duplicates `{fn_a.name}` at `{fn_a.path}:{fn_a.lineno}` ({pct}% shared AST statements)"))


def _parse_file_functions(rel_path: str, tree: ast.AST, touched_lines: set[int], removed_src: str) -> list[_ParsedFunction]:
    candidates: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
    for item in getattr(tree, "body", ()):
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            candidates.append(item)
        elif isinstance(item, ast.ClassDef):
            candidates.extend(sub for sub in item.body if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)))
    result: list[_ParsedFunction] = []
    for node in sorted(candidates, key=lambda n: n.lineno):
        end_lineno = node.end_lineno or node.lineno
        is_touched = any(ln in touched_lines for ln in range(node.lineno, end_lineno + 1))
        is_new = node.lineno in touched_lines and f"def {node.name}(" not in removed_src
        body_dump, stmts = _normalize_function(node)
        result.append(_ParsedFunction(rel_path, node.name, node.lineno, end_lineno, is_new, is_touched, node, body_dump, stmts))
    return result


def collect_quality_signals(root: Path, snap: DiffSnapshot) -> tuple[QualitySignal, ...]:
    """Compute deterministic quality signals for all Python files in `snap` under `root`."""
    touched_by_file, removed_by_file, growth_by_file = _changed_lines_by_file(snap.hunks)
    raw_signals: list[tuple[SignalKind, str, int, str]] = []
    parsed_functions: list[_ParsedFunction] = []

    for rel_path in snap.files:
        p = PurePosixPath(rel_path)
        if p.suffix.lower() != ".py" or ("tests" in p.parts and "fixtures" in p.parts):
            continue
        file_path = root / rel_path
        if not file_path.is_file():
            continue
        try:
            source = file_path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=rel_path)
        except (OSError, UnicodeDecodeError, SyntaxError):
            continue
        total_lines, growth = len(source.splitlines()), growth_by_file.get(rel_path, 0)
        if is_production_path(rel_path) and total_lines > MAX_MODULE_LINES and growth > MAX_MODULE_GROWTH:
            raw_signals.append((SignalKind.LARGE_MODULE, rel_path, 1, f"`{rel_path}` has {total_lines} lines (> {MAX_MODULE_LINES}) and grew by +{growth} lines (> {MAX_MODULE_GROWTH})"))
        for fn in _parse_file_functions(rel_path, tree, touched_by_file.get(rel_path, set()), removed_by_file.get(rel_path, "")):
            parsed_functions.append(fn)
            _check_function_signals(fn, raw_signals)

    _check_duplicate_logic(parsed_functions, raw_signals)
    total_added, top_files = production_diff_growth(snap)
    if total_added > MAX_PRODUCTION_ADDED_LINES and top_files:
        top_desc = ", ".join(f"{path} (+{cnt})" for path, cnt in top_files)
        raw_signals.append((SignalKind.DIFF_SIZE, top_files[0][0], 1, f"+{total_added} production lines added (budget: <= {MAX_PRODUCTION_ADDED_LINES}); top files: {top_desc}"))
    return tuple(QualitySignal(f"S{idx}", kind, path, line, detail) for idx, (kind, path, line, detail) in enumerate(raw_signals, 1))
