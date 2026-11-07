"""Bring the Antigravity subagent runs of the calling conversation into the ledger.

Antigravity subagents leave no hook events of their own, so the runs are read back from the parent's
transcript and written as start and stop events (plan section 4.4). The commands that judge a review
(`check`, `report`, `brief`, `prepare`) call `sync_antigravity_runs` first.

Phase C only fixes the hook point: nothing is synced yet.
"""

from __future__ import annotations

from collections.abc import Mapping

from craftsman.workspace import Workspace


def sync_antigravity_runs(ws: Workspace, env: Mapping[str, str]) -> None:
    """Record the start and stop of the subagents the calling Antigravity conversation launched.

    Fail-safe by contract: it never raises, so a command that calls it first is never blocked by it.
    Without `ANTIGRAVITY_CONVERSATION_ID` in `env` there is nothing to read.
    """
    del ws, env  # Phase E reads the transcript here
