# Changelog

All notable changes to Craftsman are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Removed

- `craftsman gate`: `--pass-*`, `--mode`, `--stage`, `--state-file` (also on `craftsman status`),
  the stdin mode and the `--soft` alias. `craftsman review view`: the `--lens` alias.
- `craftsman install`: `--copilot-prompt-files`, `--claude-hooks`, `--codex-workspace` and
  `--cursor-workspace`. `craftsman export`: `--copilot-prompt-files` and `--claude-hooks`.
- `craftsman doctor --fix-duplicates`; `doctor` still reports duplicates and which copy to keep.
- Every compatibility path for older files: a gate state, ledger or install manifest in an older
  format is an error, and uninstall no longer sweeps Copilot files no manifest records.

### Changed

- **Gate.** The state requires an owner; an invalid state is an error until
  `craftsman gate --init --takeover` replaces it. Every action refuses another session's gate
  (`--session-id` names the caller's). All stop hooks allow at most six continuations without
  fewer open issues; Antigravity keeps the state in the git directory and proves reviewer
  isolation only through finished `invoke_subagent` runs. Re-running `--init` never lowers the
  tier, and an `auto` task with a binary or oversized file gets `standard`.
- **Hooks fail closed.** `pre-tool` asks the user (Copilot denies) when it cannot check a call; an
  invalid hook command line exits 2; the git guard denies input that is not a JSON object and
  asks when a message file cannot be read; `--git-hook` usage errors exit 2. A transcript line
  that is not a JSON object, an unknown `craftsman.scope` value and an Antigravity Stop payload
  without `terminationReason`/`fullyIdle` are errors. Only the exact names `craftsman`,
  `craftsman-reviewer` and `craftsman-verifier` count as Craftsman agents.
- **Ledger and review.** `ledger.json` must contain every section and field, and records must
  name signals and hunks of their own round. `review view` refuses a stale round or an unselected
  lens; the `design` lens covers files only. Merging findings about different signals is refused.
  `review report` lists exactly the runs the gate counts and each acknowledged signal once.
- **Packets and quality signals.** Long scopes are trimmed with `[… Lx–Ly omitted …]` markers; a
  deletion is scoped to its function. A Python file that does not parse is a `parse-error`
  signal, a 41-line function is `long-function`, and `return a, b` no longer equals `return a`.
  `*.spec.ts`, `*_test.go`, `spec/` and `__tests__/` count as tests; only prose under `docs/` is
  docs. The `code_quality` and `extreme_optimizer` skills are `code-quality` and
  `extreme-optimizer`.
- **Doctor.** Reports a skill folder not named after its skill, `plugin.json` mismatches or
  repeats, unreadable settings, unparsable hook commands, an unreadable git config, and git hooks
  whose Python is gone. JSON: Copilot hooks as `state`/`error`, Claude hooks as `hooks`,
  `git_config` as `user_name`/`user_email`/`hooks_path`, one `vscode_instructions` target; no
  `cli_self_check` or `fixed_duplicates`.
- **Install and uninstall.** Each target records exactly what it created and merged into in
  `.craftsman-install.json`; uninstall removes only that, checks every recorded path before
  changing anything, and its `--dry-run` reports what a real run does. A user's file in the way
  moves into a `.craftsman-backup` folder next to it; merged files keep a one-time `.bak`.
  A failed target records nothing; files to merge into are checked first. Uninstall has its own
  flags; `--copy`/`--symlink`/`--plugin` no longer select Antigravity, and `--antigravity` no
  longer reinstalls git hooks.
- **Git hooks.** They source only their installed `_passthrough.sh`, block the commit or push when
  the guard or `.python_cmd` is missing, stay where their manifest records them, and set
  `core.hooksPath` only once the manifest holds the value it replaced.
- **Export.** Generated Markdown carries `<!-- craftsman:generated -->`, and only marked files
  count as Craftsman's; a user's file in the way is moved aside. `--user-dir`,
  `--copilot-skills-dir` and `--copilot-home` each write only their part. Only the exact hook
  commands Craftsman writes count as its Claude Code hooks. A symlinked `AGENTS.md` is updated
  where it points inside the workspace and refused outside it; markers that do not enclose one
  block are an error. A SKILL.md needs frontmatter with `name` and `description`. VS Code folders
  follow the operating system (`APPDATA`, `~/Library/Application Support`, `XDG_CONFIG_HOME`).

### Fixed

- A second `craftsman review fact add` failed; `env -u`/`-C`/`-S` values hid a trivial run.
- The ledger and gate state are flushed to disk before they replace the old file.

## [3.2.0] - 2026-10-27

### Added

- **Mechanical code-quality signals (`craftsman/quality.py`)**: the gate now measures function and
  module size budgets, diff growth (`diff-size`), test-naming hygiene (`test-name`), duplicate logic
  across AST bodies (`duplicate-logic`), and untyped dictionary domain parameters (`dict-domain`).
  Every signal appears in the review packet and must be resolved via `craftsman review add --signal
  S#` or acknowledged with a concrete code reason via `craftsman review ack S# --reason "..."`
  (shown to the user in the final report; `diff-size` cannot be acknowledged by the agent and hands
  the review to the user as `needs_human`).
- **Whole-change `design` lens** (`standard` and `deep`, covered per file with `--files`),
  **per-reviewer packet views** (`craftsman review view --lenses <group-or-lenses> [--hunks ...]`),
  **fixed reviewer lens groups (`REVIEWER_GROUPS`: `R1`–`R4`)**, and **source-cited shared facts**
  (`craftsman review fact add --hunks ... --source path:line --text "..."`, automatically
  invalidated when the source file changes).
- **Packet deduplication, scope trimming, and CLI-computed test/run facts**: shared enclosing scopes
  and symbol references are rendered once per round, scopes longer than 60 lines are trimmed around
  the hunk, and packets list test files referencing changed symbols plus the latest `test`/`e2e`
  runs with staleness.

### Changed

- **Coverage notes must be concrete**: `craftsman review cover` rejects notes made of generic praise
  (`looks good`, `well-factored`, `clean`, `lgtm`, etc.), requires every listed hunk or file to have
  its own segment naming a concrete input, condition, or `file:line`, and rejects notes reused
  verbatim across more than 3 hunks.
- **Reviews without isolation evidence are reported as self-reviewed**: in `deep` mode, missing
  platform hook or transcript evidence sets `needs_human` instead of completing; in `standard` mode,
  the gate and report display a `SELF-REVIEWED` banner.
- **`craftsman review report`** now uses the header `**Process: COMPLETE**`, flags process anomalies
  (zero findings across large rounds, burst coverage timestamps, environment-bypassed test runs),
  and ends with a `## Not assessed by the gate` section.

## [3.1.0] - 2026-10-27

### Added

- **Repository search-scope guard** (`craftsman/scope.py`, `craftsman hook pre-tool`, and
  `skills/craftsman/scope.md`): blocks recursive directory searches (`find`, `rg`, `grep -R`, `fd`,
  `ls -R`, `Glob`, `Grep`, `LS`, etc.) outside the active git work tree and blocks reading other
  git repositories, prior agent session logs (`~/.claude/projects`, `~/.copilot/session-state`,
  `~/.gemini/antigravity/brain`), or shell history during Craftsman sessions while allowing
  single-file reads outside other repositories/session stores, dedicated `$TMPDIR` subdirectories,
  and paths added via `git config craftsman.scopeAllow` (configurable with
  `git config craftsman.scope deny|warn|off`).
- **`craftsman skill {show,list,path}`** and **`craftsman review show --packet-path`**: read bundled
  skills, companion files (such as `lenses.md`), and the active round's review packet path directly
  from the CLI without searching the file system.
- **Copilot duplicate detection and repair** (`craftsman doctor` and `craftsman doctor --fix-duplicates`):
  scans `COPILOT_DISCOVERY` locations across VS Code profiles, `~/.copilot`, `.github`, and `.claude`
  to report duplicated Craftsman agents or slash commands, and removes obsolete Craftsman-generated
  copies while preserving unmarked user files and Claude Code installations.
- **`--copilot-prompt-files`** and **`--allow-duplicate-scopes`** flags on `craftsman install` and
  `craftsman export`, plus **`--takeover`** on `craftsman gate --init`.

### Changed

- **Prompt files (`*.prompt.md`) are no longer generated by default** for GitHub Copilot: slash
  commands are installed once as Agent Skills (`~/.copilot/skills/<name>/SKILL.md` for user scope or
  `.github/skills/<name>/SKILL.md` for workspace scope), and custom agents are installed once in
  `~/.copilot/agents/` or `.github/agents/`. Pass `--copilot-prompt-files` on older VS Code builds
  without Agent Skills support.
- Installing or exporting `--copilot-workspace` when a user-level `--copilot` installation already
  exists (or vice versa) now refuses by default with instructions to uninstall one scope or pass
  `--allow-duplicate-scopes`.
- Claude Code's `PreToolUse` hook matcher now covers `Bash|Read|Grep|Glob|LS` so both the git guard
  and the repository search-scope guard run across shell and file/search tools.

### Fixed

- **Duplicated agents and `/skill` slash commands in VS Code Copilot**: upgrading with
  `craftsman install --copilot` or uninstalling with `craftsman uninstall --copilot` cleans up
  legacy v3.0.0 `.agent.md` and `.prompt.md` files from `Code/User/prompts/` and
  `Code/User/profiles/*/prompts/` without touching user-authored files.
- **Cross-session gate interference in shared work trees**: `.git/craftsman/craftsman_state.json`
  now binds to the session (`owner.platform` + `owner.session_id`) that opened the gate. Parallel
  agent sessions in the same checkout are no longer blocked by `stop`, no longer increment
  `forced_continuations` or abort the owner's gate, and no longer overwrite `hooks_seen` or the
  active gate without `--takeover`.

## [3.0.0] - 2026-10-27

### Added

- **Evidence ledger** (`.git/craftsman/ledger.json`, per worktree): rounds bound to a diff
  fingerprint, lens × hunk coverage, findings with `proven` / `probable` / `question` / `dismissed`
  verdicts, resolutions, recorded runs and reviewer-subagent events. The stop gate passes only when
  the ledger proves the change was reviewed, verified and clean at its final state.
- **Review packet** (`craftsman review prepare`): hunks with enclosing scope, callers, risk signals
  and the lenses selected for the round; delta rounds re-review only what changed.
- **Eleven review lenses** with probe playbooks (`skills/code-review/lenses.md`) and the
  `craftsman-reviewer` / `craftsman-verifier` subagents; gears `quick`, `standard` and `deep`.
- **Review-only mode**: `/code-review` without `--fix` runs two parallel reviewers and one verifier,
  and finishes with `craftsman review check --review-only` and `craftsman review report
  --review-only`, which do not require fixes or test runs. It is refused inside an active `/craftsman`
  gate session.
- `--from-json <file|->` for `review add`, `review cover` and `review verdict`: a whole batch is
  validated and written atomically, and errors name the offending entry.
- `craftsman review merge` groups unjudged duplicates by root cause (one fix must remove every
  consequence); `craftsman run --kind test|e2e|repro` records executions with exit codes.
- Platform hooks for GitHub Copilot (VS Code and CLI), Claude Code and Google Antigravity, plus
  `AGENTS.md` / Cursor rules for tools without stop hooks. Subagent hooks act outside a gate session
  only for Craftsman roles in a current round.
- `craftsman install` / `uninstall` / `export` / `doctor`, including Windows PowerShell and an
  install manifest that restores a previous `core.hooksPath`. `doctor` now reports git hooks that
  cannot find their helper scripts.
- Pilot benchmark on SWR-Bench in the README, with the dataset citation.

### Changed

- Claude Code now installs the same gate hooks as Copilot and Antigravity by default, including the
  `PreToolUse` git guard for `Bash`; `--no-claude-hooks` skips them and `--claude-hooks` stays as an
  alias of `--claude`. `craftsman export --claude-dir` merges the hooks too. Re-run
  `./install.sh --claude` to pick up the guard hook on an existing install.
- The deep-gear prompt keyword is now `ultracraft` instead of `ultracode`, which Claude Code reserves
  for its own multi-agent workflows. `ultracode` no longer starts a Craftsman session; re-run
  `./install.sh` so the installed rules, commands and skills use the new keyword.
- The global `commit-msg` and `pre-push` hooks resolve their helpers through symlinks and fail with
  an actionable message when the helpers are missing, instead of `command not found`.

### Fixed

- Global git hooks left by an older install no longer break commits in unrelated repositories.
- Hook parsers, verdict detection and worktree state handling were hardened (see the v2.1.0
  follow-up fixes).

[3.2.0]: https://github.com/BuczynskiRafal/craftsman/releases/tag/v3.2.0
[3.1.0]: https://github.com/BuczynskiRafal/craftsman/releases/tag/v3.1.0
[3.0.0]: https://github.com/BuczynskiRafal/craftsman/releases/tag/v3.0.0
