"""Prose normalization and Markdown inline rendering helpers."""

from __future__ import annotations

import re
import sys

__all__ = ["ONE_LINE_WARNING", "inline_md", "one_line"]

ONE_LINE_WARNING = "wrote one line: literal \\n replaced; put code in backticks"

_TOKEN_RE = re.compile(
    r"(?P<backtick>`+.*?`+)"
    r"|(?P<quoted_dq>\"(?:\\[rnt]|\\r\\n|`n)\")"
    r"|(?P<quoted_sq>\'(?:\\[rnt]|\\r\\n|`n)\')"
    r"|(?P<literal_escape>\\r\\n|\\[nrt]|`n)"
)


def one_line(text: str, *, warn: bool = True) -> str:
    """Normalize free-text prose string to a single line.

    Replaces unquoted literal escape sequences (`\\n`, `\\r\\n`, `\\r`, `\\t`, `` `n ``) and
    runs of whitespace/control characters with a single space, while preserving backtick code spans
    and quote-adjacent escapes. If any literal escapes were replaced and `warn` is True, a warning
    is emitted to stderr.
    """
    tokens: list[str] = []
    had_literal = False

    def repl(m: re.Match[str]) -> str:
        nonlocal had_literal
        if m.group("backtick") or m.group("quoted_dq") or m.group("quoted_sq"):
            tokens.append(m.group(0))
            return f"\x00{len(tokens) - 1}\x00"
        had_literal = True
        return " "

    intermediate = _TOKEN_RE.sub(repl, text)
    if had_literal and warn:
        sys.stderr.write(f"craftsman: warning: {ONE_LINE_WARNING}\n")

    collapsed = re.sub(r"\s+", " ", intermediate).strip()

    def restore(m: re.Match[str]) -> str:
        idx = int(m.group(1))
        return tokens[idx]

    return re.sub(r"\x00(\d+)\x00", restore, collapsed)


def inline_md(text: str, *, table: bool = False) -> str:
    """Normalize prose string for inline Markdown rendering without stderr warnings.

    If `table` is True, unescaped table pipes `|` are escaped to `\\|`.
    """
    cleaned = one_line(text, warn=False)
    if not table:
        return cleaned
    return re.sub(r"(?<!\\)\|", r"\|", cleaned)
