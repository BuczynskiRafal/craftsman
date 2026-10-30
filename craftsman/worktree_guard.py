"""Work-tree and index mutation detector for PreToolUse review guards (`craftsman/worktree_guard.py`).

Inspects shell command lines for `git` invocations that alter the working tree or index so review
subagents cannot mutate files under review and main sessions are prompted while a review round is
in progress.
"""

from __future__ import annotations

from craftsman.shell import iter_simple_commands, parse_git_invocation

ALLOWED_STASH_SUBCOMMANDS = frozenset({"list", "show", "create"})
WORKTREE_MUTATION_SUBCOMMANDS = frozenset({"reset", "checkout", "switch", "restore", "clean"})
SUBAGENT_MUTATION_SUBCOMMANDS = WORKTREE_MUTATION_SUBCOMMANDS | frozenset(
    {"commit", "merge", "rebase", "cherry-pick", "revert", "am", "apply", "pull", "rm", "mv"}
)


def find_worktree_mutation(command: str, cwd: str | None, *, subagent: bool) -> str | None:
    """Describe the first work-tree-mutating `git` invocation in `command`, or return None."""
    if not command.strip():
        return None
    blocked = SUBAGENT_MUTATION_SUBCOMMANDS if subagent else WORKTREE_MUTATION_SUBCOMMANDS
    for item in iter_simple_commands(command, cwd):
        sub, sub_args, _ = parse_git_invocation(item.tokens, item.cwd)
        if sub is None:
            continue
        if sub == "stash":
            if sub_args and sub_args[0] in ALLOWED_STASH_SUBCOMMANDS:
                continue
            action = f" {sub_args[0]}" if sub_args and not sub_args[0].startswith("-") else ""
            return f"`git stash{action}`"
        if sub in blocked:
            return f"`git {sub}`"
    return None
