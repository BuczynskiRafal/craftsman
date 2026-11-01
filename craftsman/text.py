"""Prose normalization and Markdown inline rendering helpers."""

from __future__ import annotations

import re
import sys

__all__ = ["ONE_LINE_WARNING", "inline_md", "one_line"]

ONE_LINE_WARNING = "wrote one line: literal \\n replaced; put code in backticks"

_TOKEN_RE = re.compile(
    r"(?P<backtick>(?P<bt_delim>`+)(?P<bt_content>.*?)(?P=bt_delim))"
    r"|(?P<quoted_dq>\"(?:\\[rnt]|\\r\\n|`n)\")"
    r"|(?P<quoted_sq>\'(?:\\[rnt]|\\r\\n|`n)\')"
    r"|(?P<escaped_bs>\\\\)"
    r"|(?P<path>"
        r"[A-Za-z]:[\\/][^\s\"'`]*"
        r"|[^\s\"'`]*\/[^\s\"'`]*"
        r"|[^\s\"'`]*\\[^\s\"'`]*\.[a-zA-Z0-9_]+(?::\d+)*"
        r"|\.\.?[\\/][^\s\"'`]*"
    r")"
    r"|(?P<literal_escape>\\r\\n|\\[nrt]|`n)"
    r"|(?P<whitespace>\s+)",
    re.DOTALL,
)


def one_line(text: str, *, warn: bool = True) -> str:
    """Normalize free-text prose string to a single line.

    Replaces unquoted literal escape sequences (`\\n`, `\\r\\n`, `\\r`, `\\t`, `` `n ``) and
    runs of whitespace/control characters with a single space, while preserving backtick code spans,
    Windows path tokens, escaped backslashes, and quote-adjacent escapes. If any literal escapes
    were replaced and `warn` is True, a warning is emitted to stderr.
    """
    pos = 0
    chunks: list[str] = []
    pending_space = False
    had_literal = False

    for m in _TOKEN_RE.finditer(text):
        start, end = m.span()
        if start > pos:
            unmatched = text[pos:start]
            if pending_space and chunks:
                chunks.append(" ")
                pending_space = False
            chunks.append(unmatched)

        pos = end

        if m.group("backtick"):
            if pending_space and chunks:
                chunks.append(" ")
                pending_space = False
            delim = m.group("bt_delim")
            content = m.group("bt_content")
            normalized_content = re.sub(r"[\r\n]+", " ", content)
            chunks.append(f"{delim}{normalized_content}{delim}")
        elif m.group("quoted_dq") or m.group("quoted_sq") or m.group("escaped_bs") or m.group("path"):
            if pending_space and chunks:
                chunks.append(" ")
                pending_space = False
            chunks.append(m.group(0))
        elif m.group("literal_escape"):
            had_literal = True
            pending_space = True
        elif m.group("whitespace"):
            pending_space = True

    if pos < len(text):
        unmatched = text[pos:]
        if pending_space and chunks:
            chunks.append(" ")
            pending_space = False
        chunks.append(unmatched)

    if had_literal and warn:
        sys.stderr.write(f"craftsman: warning: {ONE_LINE_WARNING}\n")

    return "".join(chunks).strip()


def _escape_table_pipes(s: str) -> str:
    def repl(m: re.Match[str]) -> str:
        bs = m.group(1)
        if len(bs) % 2 == 0:
            return bs + r"\|"
        return m.group(0)

    return re.sub(r"(\\*)(\|)", repl, s)


def inline_md(text: str, *, table: bool = False) -> str:
    """Normalize prose string for inline Markdown rendering without stderr warnings.

    If `table` is True, unescaped table pipes `|` are escaped to `\\|`, even when preceded
    by escaped backslashes (`\\\\`).
    """
    cleaned = one_line(text, warn=False)
    if not table:
        return cleaned
    return _escape_table_pipes(cleaned)
