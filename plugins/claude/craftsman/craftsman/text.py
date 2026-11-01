"""Prose normalization and Markdown inline rendering helpers.

Ledger prose fields are single-line Markdown. Agents often pass multi-line text through CLI flags
as literal escape sequences (`\\n` from bash or JSON habits, `` `n `` from PowerShell), which
would otherwise reach the report verbatim and break its Markdown. `one_line` flattens real line
breaks and those literal sequences to spaces, and keeps what only looks like an escape: code
spans, Windows paths, escaped backslashes and quoted escapes such as `newline="\\n"`.

Every step is linear in the input length; this runs on each render, including inside hooks.
"""

from __future__ import annotations

import re
import sys

__all__ = ["ONE_LINE_WARNING", "inline_md", "one_line"]

ONE_LINE_WARNING = "wrote one line: literal \\n replaced; put code in backticks"

_BACKTICK_RUN_RE = re.compile(r"`+")
_LINE_BREAK_RE = re.compile(r"[\r\n]+")
_WHITESPACE_RE = re.compile(r"\s+")
_TOKEN_RE = re.compile(r"\S+")
# `\\` is kept as a pair so `\\n` stays; a sequence wrapped in matching quotes is a quoted literal.
_ESCAPE_RE = re.compile(r"""\\\\|(["'])(?:\\r\\n|\\[nrt]|`n)\1|\\r\\n|\\[nrt]|`n""")
_OPENING_PUNCTUATION = "([<\"'"
_PATH_PREFIX_RE = re.compile(r"[A-Za-z]:[\\/]|\.{1,2}[\\/]")
_EXTENSION_END_RE = re.compile(r"\.[A-Za-z0-9_]+(?::\d+)*[)\]>\"',.;:]*\Z")
_TABLE_PIPE_RE = re.compile(r"(\\*)\|")


def _code_spans(text: str) -> list[tuple[int, int]]:
    """(start, end) of each Markdown code span: a backtick run closed by the next run of equal length."""
    runs = [(m.start(), m.end()) for m in _BACKTICK_RUN_RE.finditer(text)]
    by_length: dict[int, list[int]] = {}
    for index, (start, end) in enumerate(runs):
        by_length.setdefault(end - start, []).append(index)
    cursor = dict.fromkeys(by_length, 0)
    spans: list[tuple[int, int]] = []
    index = 0
    while index < len(runs):
        start, end = runs[index]
        length = end - start
        same = by_length[length]
        position = cursor[length]
        while position < len(same) and same[position] <= index:
            position += 1
        cursor[length] = position
        if position == len(same):
            index += 1
            continue
        closing = same[position]
        spans.append((start, runs[closing][1]))
        index = closing + 1
    return spans


def _is_path(token: str) -> bool:
    bare = token.lstrip(_OPENING_PUNCTUATION)
    if _PATH_PREFIX_RE.match(bare):
        return True
    return "\\" in bare and _EXTENSION_END_RE.search(bare) is not None


def one_line(text: str, *, warn: bool = True) -> str:
    """`text` as one line: line breaks, whitespace runs and literal escapes become one space.

    Literal `\\n`, `\\r\\n`, `\\r`, `\\t` and PowerShell `` `n `` are replaced outside code spans,
    path tokens (`C:\\x`, `.\\x`, `src\\new.py:12`), escaped backslashes and matching quotes
    (`"\\n"`, `'\\t'`). When any literal was replaced and `warn` is true, a warning goes to stderr.
    """
    replaced = False

    def flatten_escape(match: re.Match[str]) -> str:
        nonlocal replaced
        if match.group(0) == "\\\\" or match.group(1) is not None:
            return match.group(0)
        replaced = True
        return " "

    def flatten_token(match: re.Match[str]) -> str:
        token = match.group(0)
        return token if _is_path(token) else _ESCAPE_RE.sub(flatten_escape, token)

    def flatten_prose(segment: str) -> str:
        return _WHITESPACE_RE.sub(" ", _TOKEN_RE.sub(flatten_token, segment))

    parts: list[str] = []
    position = 0
    for start, end in _code_spans(text):
        parts.append(flatten_prose(text[position:start]))
        parts.append(_LINE_BREAK_RE.sub(" ", text[start:end]))
        position = end
    parts.append(flatten_prose(text[position:]))
    if replaced and warn:
        sys.stderr.write(f"craftsman: warning: {ONE_LINE_WARNING}\n")
    return "".join(parts).strip()


def _escape_table_pipe(match: re.Match[str]) -> str:
    backslashes = match.group(1)
    return match.group(0) if len(backslashes) % 2 else f"{backslashes}\\|"


def inline_md(text: str, *, table: bool = False) -> str:
    """`one_line(text)` without the warning, for rendering stored prose inline in Markdown.

    With `table`, every pipe not already escaped becomes `\\|` so it cannot split a table cell.
    """
    cleaned = one_line(text, warn=False)
    return _TABLE_PIPE_RE.sub(_escape_table_pipe, cleaned) if table else cleaned
