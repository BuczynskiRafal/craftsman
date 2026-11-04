# Guardrails & Safety Architecture

Craftsman protects repository integrity and enforces review honesty through three distinct runtime guards:
the **Stop Gate**, the **Work-tree Mutation Guard**, and the **Repository Search Scope Guard**.

---

## Stop Gate

```mermaid
stateDiagram-v2
    [*] --> Idle
    Idle --> Active: craftsman or ultracraft in the prompt, gate init
    Active --> Waiting: gate await-user
    Waiting --> Active: gate resume, or the user replies
    Active --> Active: stop hook finds issues, sends the agent back (up to 6 times)
    Active --> Completed: review check passes, gate complete
    Active --> Released: 6 forced continuations without fewer issues, warns the user
    Completed --> [*]
    Released --> [*]
```

The Stop Gate prevents AI coding assistants from prematurely concluding tasks when code changes remain unreviewed,
unverified, or failing tests:

- **State storage**: Gate state is persisted in `<git-dir>/craftsman/craftsman_state.json`, alongside the evidence ledger.
- **Session ownership**: Each gate is strictly bound to the specific agent session (`owner.platform` + `owner.session_id`)
  that opened it. Parallel agent sessions in the same work tree cannot corrupt, advance, or be halted by another session's gate.
- **Explicit takeover**: If a session terminates unexpectedly or a user desires to assume control of an orphaned gate,
  `craftsman gate --init --takeover` transfers session ownership safely.
- **Forced continuation cap**: To prevent infinite loops caused by pathological agent behavior, the stop gate permits
  completion after six consecutive forced continuations if no decrease in open defects is detected, alerting the user
  that the gate was not satisfied.
- **Fail-closed posture**: Malformed or unreadable gate states cause immediate warnings, while tool permission checks
  fail closed to protect the repository.

---

## Work-tree Mutation Guard

Implemented in `craftsman/worktree_guard.py` and invoked via `craftsman hook pre-tool`:

- **Reviewer protection**: During an active review round, `craftsman-reviewer` and `craftsman-verifier` subagents
  are denied access to commands that modify the work tree or index:
  - `git stash` (except `list`, `show`, `create`)
  - `git reset`, `git checkout`, `git switch`, `git restore`, `git clean`
  - Subagent git mutations: `commit`, `merge`, `rebase`, `cherry-pick`, `revert`, `am`, `apply`, `pull`, `rm`, `mv`
- **User confirmation in main sessions**: If the main agent session attempts a worktree-mutating git command while
  a review-only round is open, the hook prompts the user for confirmation (`ask`).
- **Platform enforcement**: Intercepted in real time on Claude Code, GitHub Copilot in VS Code, and GitHub Copilot CLI.

---

## Repository Search Scope Guard

Implemented in `craftsman/scope.py` and invoked via `craftsman hook pre-tool` (or `craftsman scope-guard` on Antigravity):

Confines automated tools and subagents to the current repository, preventing data leakage across private projects
and preventing agents from wandering into external directories:

### Allowed Operations
- Reading, listing, and searching within the active git work tree (`git rev-parse --show-toplevel`, including submodules).
- File operations within dedicated temporary directories in the system `$TMPDIR` (`/tmp`).
- Direct single-file reads or non-recursive directory listings outside the work tree (such as reading standard library files
  or dependencies cited in tracebacks), provided the target is not inside another git repository or sensitive directory.

### Blocked Operations
- Recursive searches (`find`, `rg`, `grep -R`, `fd`, `ls -R`, `tree`, `Glob`, `Grep`, `LS`) rooted outside the work tree
  (e.g., `~`, `..`, `/home`, `/Users`, or sibling directories).
- Accessing other git repositories or agent session transcripts:
  - `~/.claude/projects/`
  - `~/.copilot/session-state/`
  - `~/.gemini/antigravity/brain/` and `~/.gemini/antigravity/conversations/`
  - External `.git/craftsman/` directories
  - User shell history files (`.bash_history`, `.zsh_history`)

### Configuration (per repository via Git config)

```bash
# Allow specific external directories (colon-separated on POSIX, semicolon on Windows):
git config --add craftsman.scopeAllow ../shared-schemas

# Configure enforcement mode: deny (default, blocks), warn (logs warning), or off (disabled):
git config craftsman.scope warn
git config craftsman.scope off
```
