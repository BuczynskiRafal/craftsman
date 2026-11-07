"""The environment variables that tell a program which agent platform runs it.

Kept free of imports so that `tests/__init__.py` can scrub them before any test module loads, and so that
every helper that builds a child environment uses the one list.
"""

from __future__ import annotations

AMBIENT_PLATFORM_ENV_VARS = frozenset(
    {
        "CLAUDECODE",
        "CLAUDE_CODE_ENTRYPOINT",
        "CLAUDE_SESSION_ID",
        "CLAUDE_CODE_SESSION_ID",
        "ANTIGRAVITY_AGENT",
        "AGY_MANAGED_ENV",
        "ANTIGRAVITY_CONVERSATION_ID",
        "ANTIGRAVITY_SESSION_ID",
        "COPILOT_CLI",
        "GITHUB_COPILOT_CLI",
        "COPILOT_SESSION_ID",
        "VSCODE_PID",
        "VSCODE_IPC_HOOK",
        "TERM_PROGRAM",
        "CRAFTSMAN_SESSION_ID",
    }
)
