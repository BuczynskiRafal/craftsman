#!/usr/bin/env python3
"""Antigravity PreToolUse scope-guard hook entry point; see `craftsman.scope`."""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from craftsman.scope import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
