# Guardrails & Safety Architecture

Craftsman protects repository integrity and enforces review honesty through four runtime guards: the
**Stop Gate** (with the review guard that serves `/craft-review`), the **Command Policy** (which also holds the
**Work-tree Mutation Guard**), and the **Repository Search Scope Guard**. Whatever a hook survives goes to the
**hook error log**, described at the end.

---

## Stop Gate

![Stop gate: the states of a /craftsman session](diagrams/stop-gate.png)

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
- **Fail-safe posture**: Malformed or unreadable gate states cause immediate warnings and an entry in the hook error
  log. A `pre-tool` check that cannot decide asks the user to confirm the tool call (Copilot denies it), and the search
  scope guard blocks a shell command it cannot parse. Every other hook lets the agent go on with a message that the
  gate was not checked.

### Review stop guard

`/craft-review` has no gate, so the prompt hook leaves a marker (`review_hooks.json`, next to the ledger) for the
session that invoked it, and the Stop hook holds that session to the review-only outcome. Only Craftsman's own
`/craft-review` arms it; the host agent's built-in `/code-review` neither arms it nor opens a gate.

- The guard judges the review with the same rules and the same isolation evidence as
  `craftsman review check --review-only`: in an enforced round every slot of the hunt plan must be done, every
  finding judged by a verifier subagent. It stores the waves that are due before it judges (`store_due_waves`), so a
  block already lists the next launch. Hooks count as evidence when the marker names the session the command runs in
  (Claude Code only; other platforms export no session id to commands) or when a reviewer or verifier event arrived
  after the current round was prepared. Events of an earlier session prove nothing about this one.
- The guard ends when the user moves on to another request, not when the platform speaks for the session: a stop hook's
  own block reason and a background subagent's report (`<task-notification>`, `Another Claude session sent a message:`)
  leave it armed.
- Running `craftsman review prepare` counts as starting the review even when the diff is unchanged or the depth was
  only raised.
- While a Craftsman reviewer or verifier that started in this review has not stopped (a background subagent), the guard
  lets the agent wait for it: its completion wakes the session, and no reminder is spent.
- Otherwise a review with slots not done, hunks uncovered, findings without a verdict, no reviewer run or hunks covered
  outside any reviewer run is sent back, up to six times (`MAX_CONTINUATIONS`) without progress. After that the agent
  may stop, and the review is marked `REVIEW ABANDONED - NOT COMPLETE`: it is never reported as complete.
- Coverage must come from a `craftsman-reviewer`, and a verdict from a `craftsman-verifier`. A run vouches for a record
  by identity (the record names its caller: `antigravity:<conversation id>`), by transcript (the run's own transcript
  printed the record's token, Claude Code) or, weakest and bounded, by the time window of the run. Findings may come
  from anyone.

---

## Command Policy

`craftsman/command_policy.py` is the one entry point that decides every PreToolUse call, whichever platform made it:
the Claude Code and Copilot `craftsman hook pre-tool` hook and the Antigravity scope guard are adapters over it. It
denies, in this order, for a shell command (a file-writing tool is checked against the same protected places):

- **Hook entry points.** `craftsman hook ...` in every spelling, `python -m craftsman.hooks`, the guard scripts, and
  inline Python programs that import `craftsman` are run by the platform, never by an agent. Every script named
  `craftsman-cli.py` is the CLI, so the commands the hooks themselves run are denied as well: the plugin's
  `bin/craftsman-cli.py`, the checkout's `scripts/craftsman-cli.py` and the `craftsman/craftsman-cli.py` that user-level
  hooks run. These bootstraps, and the Antigravity scripts, put Craftsman's own directory first on `sys.path`, so a
  project's own `craftsman/` directory never stands in for the guard; no hook starts Craftsman with `python -m`, which
  would put the project there first. What a `python` command
  line runs is decided by `shell.python_program`; the words of an `env -S "<command line>"` string are read as the command
  they start (`shell.read_env_option`: every spelling of the options that take a value, `-C`'s directory included), so no wrapper hides a command from any rule below.
- **The evidence.** Writes into `<git-dir>/craftsman/` (the ledger, the gate state, the hook error log; `inbox/` and
  `scratch/` stay open) and into the transcript folders of the platforms, by any write verb, redirection, inline
  program or file-writing tool (the Claude Code matcher covers `Write|Edit|MultiEdit|NotebookEdit`). Reading stays
  allowed.
- **Identity.** `CLAUDE_CODE_SESSION_ID`, `CRAFTSMAN_*` and the other identity variables cannot be set or unset for a
  craftsman command; a made-up `craftsman gate --session-id` is denied; the gate's owner cannot `--takeover` its own
  gate. The main session records only as `lead`.
- **Roles.** Hunters and verifiers do not re-plan (`review prepare`, `reset`), and the main session does not fetch a
  hunter's brief or record a slot-tagged finding or coverage.
- **The work tree** (below).

**Read-only fast path.** A call that only reads never waits for this module to load: `craftsman hook pre-tool` first
runs `craftsman/hook_fast.py`, which imports nothing of Craftsman but the tool names and answers `{}` (allow) when the
tool is in the policy's read-only set (`READ_ONLY_TOOL_NAMES`, which `decide` allows whatever the call names), no agent
name contains `craftsman` (the search scope binds Craftsman's agents even without a gate), and every repository git
could find from the call's `cwd` has no gate or a valid inactive one, read from
`<git-dir>/craftsman/craftsman_state.json` without running git. For such a call the full handler allows too and changes
nothing: neither the scope guard nor the claim of a gate runs. Whatever it cannot be sure of (another tool, input the
handler would reject, an active gate of any session, a state file it cannot read or validate, a git environment
variable, a `core.worktree` or a `.git` pointer git may read otherwise that could move the repository, a `cwd` inside a
git directory) goes to the full handler with the payload already read. The fast path never denies, asks or writes, and
`tests/test_hook_fast.py` holds its answers to the full handler's.

These guards stop low-effort shortcuts; they are not a sandbox. A script file written first and run later, a program
name or path built by command substitution, and an interpreter that assembles the path at run time pass a shell parser
unseen.

---

## Work-tree Mutation Guard

Implemented in `craftsman/worktree_guard.py`, called by the command policy:


- **Reviewer protection**: During an active review round, `craftsman-reviewer` and `craftsman-verifier` subagents
  are denied access to commands that modify the work tree or index:
  - `git stash` (except `list`, `show`, `create`)
  - `git reset`, `git checkout`, `git switch`, `git restore`, `git clean`
  - Subagent git mutations: `commit`, `merge`, `rebase`, `cherry-pick`, `revert`, `am`, `apply`, `pull`, `rm`, `mv`
- **User confirmation in main sessions**: If the main agent session attempts a worktree-mutating git command while
  a review-only round is open, the hook prompts the user for confirmation (`ask`).
- **Platform enforcement**: Intercepted in real time on Claude Code, GitHub Copilot in VS Code, GitHub Copilot CLI, and Antigravity (through the scope guard script).

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

---

## Hook Error Log

A hook never crashes the session it serves, so every failure it survives would otherwise vanish: a crash, an
unreadable transcript, a git call that ran out of the hook's time budget, a ledger it could not load or had to set
aside, a transcript without provenance. Each one is appended as a JSON line to `<git-dir>/craftsman/hook-errors.log`
(`craftsman/hook_errors.py`; the last 512 KB are kept). `craftsman review check`, `review report`, `gate --complete` and
`status` print `Hook errors since round N was prepared: K (see <path>)` when the log holds entries newer than the latest
round. The git calls of one hook share the budget of its event (its timeout less a margin, `craftsman/hook_budget.py`),
so a slow repository ends in a logged timeout and an answer instead of a killed hook.
