"""Standard stream encodings for every process entry point (CLI and platform hook scripts)."""

from __future__ import annotations

import io
import sys

__all__ = ["configure_std_streams"]


def configure_std_streams() -> None:
    """Exchange UTF-8 through pipes on Windows, and never crash on a character a console lacks.

    Agents pass hook input and read our output through pipes as UTF-8, while Python on Windows
    uses the ANSI code page there: a `ł` in a prompt or path arrives garbled, and every `→` of a
    report leaves as `?` or, in a stream with strict errors, raises UnicodeEncodeError.
    """
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if not isinstance(stream, io.TextIOWrapper) or stream.closed:
            continue
        if sys.platform == "win32" and not stream.isatty():
            stream.reconfigure(encoding="utf-8", errors="replace")
        elif stream is not sys.stdin:
            stream.reconfigure(errors="replace")
