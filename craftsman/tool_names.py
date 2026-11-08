"""The names of the tools that run commands, read files and search, as the platforms spell them (lower case).

A leaf module: `scope` (the search-scope guard) and `command_policy` (the PreToolUse policy) both read these
names, and `scope` imports `antigravity`, which the policy's callers import in turn.
"""

from __future__ import annotations

SHELL_TOOL_NAMES = frozenset(
    {
        "bash",
        "run_in_terminal",
        "runinterminal",
        "terminal",
        "powershell",
        "pwsh",
        "shell",
        "run_command",
    }
)
READ_TOOL_NAMES = frozenset({"read", "read_file", "view_file", "notebookread", "view"})
SEARCH_TOOL_NAMES = frozenset(
    {
        "grep",
        "grep_search",
        "ripgrep",
        "glob",
        "file_search",
        "find_by_name",
        "ls",
        "list_dir",
        "list_directory",
    }
)
