#!/usr/bin/env python3
"""Antigravity PreToolUse scope-guard hook entry point; see `craftsman.scope`."""

from __future__ import annotations

import sys
from pathlib import Path

# First on the path even when it is there already: an empty `PYTHONPATH` entry puts the working directory,
# a project that may hold a `craftsman/` directory of its own, ahead of it.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from craftsman.scope import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
