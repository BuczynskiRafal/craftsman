"""How long each hook may run, and how much of that its git calls may use.

One table serves the hook configurations the platform modules write (`platforms/claude.py`,
`platforms/copilot.py`) and the hook entry points that wrap their work in `diffscan.git_budget`
(`hooks.main`; the Antigravity entry points use the `antigravity-*` rows). A hook that runs past its platform's
timeout is killed and treated as "allow", so its git calls must give up first, leave a trace in the hook error
log (`hook_errors`) and let the hook answer.
"""

from __future__ import annotations

# Hook event -> the timeout in seconds that is written into the platform's hook configuration.
HOOK_TIMEOUTS_S: dict[str, int] = {
    "prompt": 30,
    "stop": 60,
    "subagent-start": 30,
    "subagent-stop": 30,
    "pre-tool": 30,
    "antigravity-stop": 10,
    "antigravity-pre-tool": 10,
}
MAX_MARGIN_S = 5.0


def git_budget_s(event: str) -> float:
    """The seconds the git calls of one `event` hook share: its timeout less a margin for everything else it does."""
    timeout = HOOK_TIMEOUTS_S[event]
    return timeout - min(MAX_MARGIN_S, timeout / 2)
