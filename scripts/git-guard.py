#!/usr/bin/env python3
"""PreToolUse and native Git hook entrypoint shim delegating to `craftsman.guard`."""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from craftsman.guard import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
