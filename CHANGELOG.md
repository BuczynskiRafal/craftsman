# Changelog

All notable changes to Craftsman are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

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

[3.0.0]: https://github.com/BuczynskiRafal/craftsman/releases/tag/v3.0.0
