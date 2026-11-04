# Changelog

All notable changes to Craftsman are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed

- **BREAKING: the review skill is now `/craft-review`** (was `/code-review`), because Claude Code ships a
  built-in `/code-review` and the two names collided. The skill lives in `skills/craft-review/`, and the
  playbooks are read with `craftsman skill show craft-review --lenses <ids>`. There is no alias:
  `craftsman skill show code-review` fails with the unknown-skill error that lists the valid names.
- **Upgrade cleanup of the old `code-review` skill**: `craftsman install` and `craftsman uninstall` remove
  the `code-review` skill an earlier version put in the Antigravity, Copilot and Claude Code folders, but
  only when Craftsman owns it (an install manifest records it, or its files carry the generated marker);
  a `code-review` skill of your own is kept. `craftsman doctor` reports a leftover one.
- **BREAKING: "Build freely, review hard" redesign.** Recall dropped after 3.3.0 because the review
  was optimized for cost and for satisfying the ledger. The agent now builds without review loops or
  ledger bookkeeping; the gate judges outcomes on the final diff only: the diff was frozen, fresh-context
  hunters covered every hunk, every finding has a verdict with checkable evidence, and the tests ran on
  the final code.
- **New review protocol** (`/craft-review` is the single source; `/craftsman` follows it):
  `review prepare` freezes the diff and prints a risk map, the orchestrator writes an intent brief and
  an adaptive hunt plan, and `craftsman-reviewer` hunters (recall) run in parallel, followed by
  `craftsman-verifier` subagents (precision). While a wave produces `proven` or `probable` findings,
  another wave runs (a variant hunter per confirmed root cause plus a fresh unanchored hunter), at most
  3 waves per round. There is no inline-only review: every review uses a fresh-context hunter where the
  platform has subagents. The prompts are rewritten to teach how to hunt and how to judge, and are much
  shorter.
- **No flags for users.** `/craftsman <task>` and `/craft-review <plain words>` ("review PR 42 and fix
  what you find", "review my staged changes", "be thorough") are enough: the agent infers target, scope,
  fixing, and depth. Flags remain optional shortcuts for agents and power users.
- **One adaptive depth.** The agent goes deep on its own for security-relevant code, concurrency or
  clocks, persistent data or migrations, public API or file formats, removed guarantees, and large or
  cross-cutting diffs, or when the user asks for care. `review prepare` prints a
  `Depth: deep suggested (signals: ...)` hint, and `review prepare --deep` inside a gate session raises
  the gate to deep for good. Depth never goes down. `ultracraft` is `/craftsman --deep`.
- **Cheaper bookkeeping.** `review cover` takes `--hunks` or `--files` (every open hunk of those files)
  and one free-text note of at least 20 characters. There are no per-hunk note segments, no 80-character
  rule, and no batch-size limit.
- **Ledger schema 3** and a simpler gate. Coverage records hold hunk digests and a note, with no lens;
  a hunk is covered when this round or an earlier one holds its digest. Gate state and rounds carry
  `deep: bool` instead of a tier. The gate issues are `no-round`, `stale-round`, `uncovered`,
  `no-verdict`, `unresolved`, `no-passing-run`, `no-e2e-run` (deep only), `failing-run`,
  `no-isolation`, and `round-cap`. A very large diff is now a warning in `prepare` and the report, not
  a `needs_human` hand-off. Whenever the platform provides isolation evidence, every round with
  non-carried hunks needs a finished `craftsman-reviewer`, in gate sessions and in review-only mode.
  Owner binding, `MAX_CONTINUATIONS`, the round cap, run rules, evidence rules, and the frozen
  review-only scope are unchanged.
- **Restored risk triggers.** `filesystem` signals suggest `state`, `trust`, and `platform` again, and
  `process` suggests `platform`. Both were narrowed in 3.3.0.
- **Larger review packet.** One 120k budget and one format for every round. Enclosing scope bodies are
  always included (subject to the trimming ladder), callers are capped at 6 per symbol instead of 3,
  and risk signals show per hunk with the lenses they suggest.
- **Docs** rewritten around plain-language usage; `docs/harness-architecture.md` roughly halved.

### Added

- **Public seeded-defect suite** in [`evals/`](evals/README.md): six small Python projects with 15 seeded
  defects and decoys, run with `python3 -m evals.run` and described in
  [`docs/benchmark.md`](docs/benchmark.md).

### Removed

- **Tiers.** The `quick` / `standard` / `deep` tiers, `--tier`, and automatic tier selection (including
  the review-only `quick` default up to 250 changed lines). `--quick` and `--standard` right after
  `/craftsman` are still accepted and ignored (stripped from the task text).
- **Commands and flags.** `review ack`, `review fact`, `review prepare --add-lens`,
  `review add --signal`, `review cover --lens` (and the `"lens"` / `"lenses"` JSON keys),
  `review view --lenses` and `--shard`, and the `R1`-`R4` reviewer groups in
  `craftsman skill show craft-review --lenses`.
- **Concepts.** Signal acknowledgements, shared facts, lens additions, per-lens coverage, per-lens views,
  reviewer shards and groups, and the gate issues `tier-too-low`, `sweep-too-early`, the quality-signal
  issues, and design-file coverage. Quality signals (`S#`) stay as display hints.
- The `{{reviewer_groups}}` placeholder in `templates/AGENTS.md`.

### Migration

- **Old ledgers and gate state fail.** A schema 2 ledger raises the existing error: run
  `craftsman review reset` to archive it. A gate state file from before this release is invalid: run
  `craftsman gate --init --takeover`. Nothing is converted. Re-run `craftsman install` to refresh the
  skills, agents, and `AGENTS.md` block.
- `gate --init --tier deep` and `review prepare --tier deep` become `--deep`. `--tier quick` and
  `--tier standard` become nothing: omit the flag and the agent chooses.
- `review cover --lens a,b --hunks H1,H2 --note "..."` becomes
  `review cover --hunks H1,H2 --note "..."`; drop `"lens"` / `"lenses"` from `--from-json` entries.
- `review add --signal S#` and `review ack S#`: drop them. Mention the point in a finding if it is a
  real defect.
- `review prepare --add-lens`: assign the lens to a hunter in the hunt plan instead.
- `review view --shard K/N` and `--lenses`: use `--files <csv>` or `--hunks <ids>` to give each hunter
  a slice; the lenses go in the hunter's brief.
- Scripts that read `tier` from `craftsman status` or the state file: read the deep flag.

## [4.1.0] - 2026-11-02

### Added

- **Modular documentation (`docs/`)**: Reference material moved from `README.md` into modular guides:
  [`docs/how-it-works.md`](docs/how-it-works.md), [`docs/installation.md`](docs/installation.md),
  [`docs/usage.md`](docs/usage.md), [`docs/guards.md`](docs/guards.md), [`docs/skills.md`](docs/skills.md),
  and [`docs/benchmark.md`](docs/benchmark.md). `README.md` is rewritten as a short overview: what the gate
  enforces, the review loop step by step, supported platforms, and the pilot measurements.
- **Claude Code plugin**: The repository root is a Claude Code plugin and its own marketplace
  (`.claude-plugin/marketplace.json` with `"source": "./"`), installed with
  `/plugin marketplace add BuczynskiRafal/craftsman` and `/plugin install craftsman@craftsman`. Skills and
  the stdlib-only `craftsman/` package are used in place; `bin/` holds the POSIX and Windows launchers,
  which look for Python 3.10+ and put `craftsman` on the agent's `PATH`.
  `craftsman export --claude-plugin [--check]` regenerates (or verifies) the manifest with its hooks, the
  catalog and the rendered agents in `.claude-plugin/`; CI fails when they are out of date.
- **Namespaced plugin triggers**: Recognizes `craftsman:craftsman`, `craftsman:ultracraft`, `/craftsman:craftsman`,
  and `/craftsman:ultracraft` in prompt text and Claude Code `<command-name>` hook payloads.
- **Doctor duplicate Claude hook check**: `craftsman doctor` inspects both `~/.claude/settings.json` and
  plugin state (`installed_plugins.json` / `enabledPlugins`) to flag duplicate active hooks that would fire twice.
- **Install guard against duplicate Claude hooks**: `craftsman install --claude` (and `export --claude-dir`)
  refuses to install user-level hooks when the Claude Code plugin is active unless `--allow-duplicate-scopes`
  is supplied; `--no-claude-hooks` installs the rest beside the plugin. An auto-detected Claude Code is skipped
  with a note instead, so a plain `craftsman install` still sets up the other platforms.
- **Antigravity plugin duplicate guard**: `agy plugin install https://github.com/BuczynskiRafal/craftsman` is
  documented. While that plugin is enabled, `craftsman install` skips an auto-detected Antigravity and refuses
  `--antigravity` (copy or symlink) unless `--allow-duplicate-scopes` is supplied, and `craftsman doctor` reports
  the plugin beside a copy install. Craftsman's own `--plugin` install is not mistaken for the agy plugin.
- **Ephemeral `uvx` interpreter refusal**: `craftsman install` detects if running under an ephemeral uv cache
  interpreter (`.cache/uv/archive-...`) and refuses installation with instructions to use `uv tool install craftsman-gate`
  or `pipx install craftsman-gate`, preventing broken hook paths when uv cleans its cache. The interpreter path
  is checked as the hooks embed it (uvx's `bin/python` is a symlink out of the cache), and only for targets
  whose hooks embed it.
- **Automated release workflow & CI wheel test**: Added `.github/workflows/release.yml` with tag validation,
  isolated virtualenv smoke tests, and PyPI Trusted Publishing (OIDC). Wheel smoke test is mirrored in CI.
- **Automated documentation link checker (`tests/test_docs_links.py`)**: Tests all relative file links,
  section anchors, and raw GitHub diagram URLs across `README.md` and `docs/*.md`.
- **Version consistency check (`tests/test_version.py`)**: Enforces exact version synchronization across
  all repo manifests (`pyproject.toml`, `plugin.json`, `craftsman/__init__.py`, and plugin manifests).
- **One-line prose in the ledger and reports**: Finding titles, scenarios, suggestions, coverage notes,
  verdict evidence, resolution notes, facts and reasons are flattened to one line on the way in, including
  literal `\n` / `\t` / PowerShell `` `n `` sequences (with a warning). Code spans, Windows paths and quoted
  escapes such as `newline="\n"` are kept. Every Markdown renderer applies the same rules, so ledgers
  written by older versions render correctly too.
- **Managed scratch directory (`craftsman review scratch`)**: Prints a fresh `<git-dir>/craftsman/scratch/<id>/`
  for repro and verification scripts; removed by `gate --complete`, `review reset` and the final
  review-only report.
- **Out-of-scope change warning**: `review report` and `review check --review-only` list files created or
  changed outside a frozen review scope since the review started.
- **Stop-hook summary line**: The continuation reason now starts with one line naming what blocks the gate,
  e.g. `review incomplete: 3 hunks not covered by lens contracts; 1 finding without a verdict`.

### Changed

- **PyPI distribution package rename**: Distributed as `craftsman-gate` on PyPI (`pipx install craftsman-gate`,
  `uv tool install craftsman-gate`). Runtime import `import craftsman` and CLI command `craftsman` remain unchanged
  with zero third-party dependencies (`dependencies = []`). Both distributions install the same files, so
  remove a `pip install .` of an older checkout (`pip uninstall craftsman`) before installing `craftsman-gate`;
  `craftsman doctor` reports the old distribution when both are installed.
- **Tagline and messaging cleanup**: Unified project tagline to "Make AI coding agents prove they reviewed and tested their change"
  across all files, eliminating ungrounded marketing buzzwords.
- **Canonical MIT license and NOTICE**: Canonical MIT text preserved in `LICENSE`; detailed attribution and
  project notices separated into `NOTICE`.
- **Prompt hook failures**: A failing prompt hook tells the user and the agent that a `/craftsman` prompt did
  not open the gate.
- **Windows CI**: A `windows-latest` job runs lint, type checks, tests, `doctor` and an installer dry run.

### Fixed

- **Antigravity plugin manifest**: `plugin.json` lists its skills as `./skills/<name>` paths and no longer
  carries the `agents` and `rules` fields, as the current agy loader reads it (`agy plugin validate`
  reported every entry as missing and loaded the plugin only from its default folders). `craftsman doctor`
  checks the paths against `skills/`.
- **Unrendered agents in the agy plugin**: The agent templates moved from `agents/` to `templates/agents/`.
  Plugin loaders read a repository's `agents/` folder as finished agents, so `agy plugin install` gave
  Antigravity three agents whose bodies were bare `{{include ...}}` lines, the orchestrator without any of
  its instructions. The rendered Claude Code, Copilot and plugin agents are unchanged.
- **Antigravity `--plugin` install was never loaded**: `craftsman install --antigravity --plugin` linked
  `~/.gemini/config/plugins/craftsman` to the checkout, and agy skips a plugin folder that is a symlink, so
  Antigravity had neither the skills nor the gate hooks it had just taken out of `hooks.json`. The plugin is
  now a copy of what agy loads (`plugin.json`, `hooks.json`, `skills/`, `rules/`, `scripts/` and the
  package), without the unrendered agent templates, and `craftsman doctor` reports it when it falls behind
  the checkout.
- **UTF-8 through pipes on Windows**: The CLI and the Antigravity hook scripts read and write UTF-8 when
  stdin/stdout are pipes, as the agents do. Before, Python used the ANSI code page there: a `ł` in a
  prompt arrived garbled, every `→` of a report left as `?`, and the Antigravity hooks could crash on a
  path they could not encode. `git config` output and the interpreter check of `install` are decoded as
  UTF-8 and raw path bytes, so a home such as `C:\Users\Rafał` works.
- **VS Code folder without `APPDATA`**: On Windows `install`, `uninstall` and `doctor` fall back to
  `<home>\AppData\Roaming` when `APPDATA` is unset or points outside the given home, instead of failing.

## [4.0.0] - 2026-10-30

### Added

- **Cleanup of legacy 3.x global Git hooks**: `craftsman install` and `craftsman uninstall` automatically
  remove global Git hooks installed by Craftsman 3.x and restore `core.hooksPath`.

### Changed

- **Google Antigravity hooks**: Scope guard (`craftsman-scope-guard`) replaces the legacy git guard in `hooks.json`.
- **Syntactic trigger detection**: Trigger detection for `/craftsman` and `ultracraft` outside quotes and backticks
  is purely syntactic.

### Removed

- **Git policy enforcement**: Removed Git commit, push, and AI attribution policies (`craftsman guard`,
  global `commit-msg` / `pre-push` hooks, `--git-hooks` / `--no-git-hooks`, and `rules/git_workflow.md`).
  Attribution and branch protection should be configured in user/agent instructions or remote repository settings.

## [3.3.0] - 2026-10-29

### Added

- **Frozen review-only scope (`--staged`, `--tracked`, `--paths <csv>`, `--add-paths <csv>`)**:
  standalone `craftsman review prepare` pins the matched file paths (including both paths of
  renames) in `Round.scope`, filters tracked diffs with literal pathspecs and untracked scans across
  `review`, `hooks`, `runner`, and `status`, reports out-of-scope edits as `Ignored (outside the
  review scope): …` without marking an active round stale, allows monotonic scope expansion via
  `prepare --add-paths <csv>` for files touched during `--fix`, and hints to run
  `craftsman review reset` when an earlier review is already complete.
- **Deterministic reviewer shards (`craftsman review view --shard K/N`)**: `review prepare` reports
  `Suggested reviewer shards: N`, `review view --shard K/N` partitions open hunks into file-disjoint
  shard views capped at `60,000` characters (`MAX_VIEW_CHARS`) with a `Files in this shard:` header,
  and `/code-review` caps concurrent subagents with `--parallel <N>` (default `2`).
- **Multi-lens and `quick` file-level coverage**: `craftsman review cover --lens a,b,c` (and
  `"lenses": [...]` in `--from-json`) records atomic coverage across multiple same-target lenses
  (`>= 80`-character concrete segments per target when combining two or more lenses), and `quick`
  rounds accept `--files` coverage for every selected lens.
- **On-demand lens playbooks (`craftsman skill show code-review --lenses <csv>`)**: prints the
  shared playbook preamble plus only the requested lenses or `R1`–`R4` reviewer groups.
- **Partial review-only reports and compact hunk maps**: `craftsman review report --review-only
  --partial` exits `0` with `PARTIAL (review-only, N open items)`, a `## Not covered` section, and
  `(unverified)` finding markers when a subagent fails after one retry; `craftsman review show
  --hunks` prints a one-line-per-hunk `H# -> path:lines` map for verifiers and inline `sweep`.
- **Pre-tool worktree mutation guard**: `craftsman hook pre-tool` (`guard.find_worktree_mutation`)
  blocks `git stash` (except `list`, `show`, and `create`), `reset`, `checkout`, `switch`,
  `restore`, and `clean` (plus `commit`, `merge`, `rebase`, `cherry-pick`, `revert`, `am`, `apply`,
  `pull`, `rm`, and `mv` for subagents) — denying calls from `craftsman-reviewer` and
  `craftsman-verifier` and asking for confirmation in the main session during an open review-only
  round on platforms with `PreToolUse` hooks.
- **`.git/craftsman` packet and archive pruning**: preparing a new round deletes superseded
  `packet-<k>.*.md` views and older `packet-<k>.md` packets (`prune_round_files`), and archival
  retains the newest `CRAFTSMAN_KEEP_ARCHIVES` (default `5`) `archive-*` directories and
  `ledger-*.json` snapshots.

### Removed

- `craftsman gate`: `--pass-*`, `--mode`, `--stage`, `--state-file` (also on `craftsman status`),
  the stdin mode and the `--soft` alias. `craftsman review view`: the `--lens` alias.
- `craftsman install`: `--copilot-prompt-files`, `--claude-hooks`, `--codex-workspace` and
  `--cursor-workspace`. `craftsman export`: `--copilot-prompt-files` and `--claude-hooks`.
- `craftsman doctor --fix-duplicates`; `doctor` still reports duplicates and which copy to keep.
- Every compatibility path for older files: a gate state, ledger or install manifest in an older
  format is an error, and uninstall no longer sweeps Copilot files no manifest records.

### Changed

- **Review-only tier, `quick` lenses, and narrower `standard` triggers.** Without an active gate,
  `craftsman review prepare` defaults to `quick` up to `250` changed lines (`REVIEW_ONLY_QUICK_MAX_LINES`,
  and for docs-only diffs; skipped binary or oversized files force `standard`) and `standard` above
  `250` lines, suppressing the `quick` diff-size warning up to `250` lines in `--review-only`.
  `quick` runs `intent`, `contracts`, and `failure` inline with zero subagents (plus `trust` on
  `sql`, `deserialization`, `auth`, or `process` signals; `craft` moves to `standard`/`deep` and
  docs-only, and `sweep` does not run on `quick`). On `standard`, `filesystem` no longer triggers
  `state`, `trust`, or `platform`, and `process` no longer triggers `platform`.
- **Packet budgets, lean subagents, and conditional verifier.** `MAX_PACKET_CHARS` is now `120,000`
  (`40,000` on `quick`, where packets omit enclosing scope bodies in favor of line-range pointers),
  with progressive trimming across scopes, caller references (capped to `3` per symbol), and omitted
  hunk diff bodies (`## Omitted hunk bodies`). `craftsman-reviewer` and single-file `/code-review`
  exports no longer inline `lenses.md`; `craftsman-verifier` runs only when unjudged findings exist
  and reads `review show --hunks` instead of `packet-N.md` (with `sweep` performed inline when
  review-only `standard` records zero findings).
- **Ledger schema v2.** `SCHEMA_VERSION` is now `2` (`Round.scope`); older schema `1` ledgers fail
  with instructions to run `craftsman review reset`.
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

[Unreleased]: https://github.com/BuczynskiRafal/craftsman/compare/v4.1.0...HEAD
[4.1.0]: https://github.com/BuczynskiRafal/craftsman/compare/a4458c4...v4.1.0
[4.0.0]: https://github.com/BuczynskiRafal/craftsman/commit/a4458c4
[3.3.0]: https://github.com/BuczynskiRafal/craftsman/compare/v3.2.0...v3.3.0
[3.2.0]: https://github.com/BuczynskiRafal/craftsman/compare/v3.1.0...v3.2.0
[3.1.0]: https://github.com/BuczynskiRafal/craftsman/compare/v3.0.0...v3.1.0
[3.0.0]: https://github.com/BuczynskiRafal/craftsman/releases/tag/v3.0.0
