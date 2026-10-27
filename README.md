# Craftsman: evidence-gated engineering loop for AI coding agents

**English** · [Polski](README.pl.md)

[![CI](https://github.com/BuczynskiRafal/craftsman/actions/workflows/ci.yml/badge.svg)](https://github.com/BuczynskiRafal/craftsman/actions/workflows/ci.yml)
[![Python 3.10–3.13](https://img.shields.io/badge/python-3.10--3.13-blue.svg)](pyproject.toml)
[![mypy --strict](https://img.shields.io/badge/mypy-strict-blue.svg)](pyproject.toml)
[![Dependencies: stdlib only](https://img.shields.io/badge/dependencies-stdlib%20only-brightgreen.svg)](pyproject.toml)
[![Version](https://img.shields.io/badge/version-3.1.0-emerald.svg)](plugin.json)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Craftsman is a quality loop for AI coding agents. After implementing a change, the agent simplifies
it, hands it to reviewers with a fresh context, tries to disprove every finding, fixes the confirmed
bugs, reviews the fixes, and runs the code. The gate blocks completion until the evidence ledger
shows that every step ran on the final version of the code.

- **GitHub Copilot** (VS Code agent, Copilot CLI), **Claude Code**, **Google Antigravity**: hooks
  enforce the gate.
- **Codex, Cursor**, and other tools that read `AGENTS.md`: the same protocol, checked on completion
  by `craftsman gate --complete`.

## Why

An agent's review of its own change usually fails for three reasons:

1. **The agent reads the diff, not the code.** Defects usually happen where the changed lines meet
   code the diff does not show: the rest of the function, its callers, shared state, tests. The
   **review packet** attaches the enclosing scope, call sites, and risk signals to each hunk.
2. **The agent judges while it searches.** It drops leads right away, stops at the first plausible
   explanation, and skips uncertain cases. The **two-phase protocol** splits these steps: lenses
   only collect findings, and a separate verifier tries to disprove and reproduce each one.
3. **Nothing checks that the review happened.** "Run /code-review before finishing" is only a hint.
   An agent in a hurry skips it or reviews 3 of 12 hunks. The **ledger** records coverage, verdicts,
   and runs, and the gate checks it before letting the agent finish.

## Benchmark

Pilot from 26 October 2026 on [SWR-Bench](https://doi.org/10.1145/3808144) data (see
[Citation](#citation)). Five pull requests from real projects: three from `pytest`, one each from
`flask` and `requests`. Each agent reviewed them twice: alone and with the `/code-review` protocol.
Both runs had the same model, PR description, and output format. The agents did not see the ground
truth. Three PRs contain 9 reviewer-confirmed defects in total (logic, contracts, tests, docs,
changelog). Two are labeled clean in the dataset.

| Agent | Model and thinking level | Variant | Defects found | Avg time per PR | Findings reported |
| :--- | :--- | :--- | :---: | :---: | :---: |
| Google Antigravity | Gemini 4 Argon, high (`gemini-4-argon-high`) | without Craftsman, 2 repetitions | **22%** (2/9, 2/9) | 4.8 min | 15 |
| Google Antigravity | Gemini 4 Argon, high (`gemini-4-argon-high`) | with Craftsman, 2 repetitions | **72%** (6/9, 7/9) | 10.8 min | 34 |
| Claude Code | Claude Sonnet 5.5, high (`claude-sonnet-5-5`) | without Craftsman, 1 repetition | **56%** (5/9) | 0.4 min | 9 |
| Claude Code | Claude Sonnet 5.5, high (`claude-sonnet-5-5`) | with Craftsman, 1 repetition | **78%** (7/9) | 9.0 min | 19 |

On Antigravity, the agent without Craftsman found the same 2 defects in both repetitions. With
Craftsman, it found 6 and 7, including both earlier ones. The new catches were mostly mismatches
between code and docs or changelog, weakened tests, and a timestamp handling bug in `pytest`.

On Claude Code, the variant without Craftsman is a single reply after a few dozen seconds. With
Craftsman, the agent goes through the lenses and verification.

The verifier dismissed 32 findings: 31 duplicates merged by root cause and one false alarm disproved
by a reproduction script. No real defect was dismissed. On the clean `pytest-5680`, neither variant
reported anything. On `requests-764`, also labeled clean, both variants reported comments. The
dataset does not grade them, so they are not counted in the score.

## How it works

![Craftsman v3.0: engineering loop and three gears](docs/diagrams/harness-overview.png)

Architecture, state machines, and editable diagram sources:
[`docs/harness-architecture.md`](docs/harness-architecture.md), [`docs/diagrams/`](docs/diagrams/).

```
/craftsman <task>          prompt hook (or `craftsman gate --init`) records the base commit
        │
        ▼
   implement               /feature-dev, /modernize, /batch, or a direct fix
        │
        ▼
   /simplify
        │
        ▼
craftsman review prepare   packet-N.md: hunks, enclosing scopes, call sites,
        │                  risk signals, lenses selected for the round
        ▼
   lens passes             craftsman-reviewer subagents with a fresh context
        │                  record coverage and findings
        ▼
   challenge               craftsman-verifier tries to disprove each finding and
        │                  reproduces the ones that hold up: proven / probable / dismissed
        ▼
   fix + resolve  ──►  code changed?  ──►  next round reviews only the changes
        │
        ▼
craftsman run --kind test|e2e -- …   runs recorded with exit codes
        │
        ▼
craftsman review check  →  stop hook lets the agent finish  →  craftsman review report
```

### Ledger

![Ledger: diff fingerprint, hunk context, and delta carry-over](docs/diagrams/evidence-ledger-flow.png)

Everything goes into `.git/craftsman/ledger.json`, separately for each worktree and outside
commits.

| Record | Written by | Gate requirement |
| :--- | :--- | :--- |
| **Round**: diff fingerprint, hunks, selected lenses | `review prepare` | the latest round matches the current code |
| **Coverage**: lens × hunk, with a note on what was checked in each hunk | `review cover` (or `--from-json`) | every selected lens covers every hunk of the round |
| **Finding**: location, severity, root cause → outcomes | `review add` (or `--from-json`). `review move` corrects the line number. `review merge` merges unjudged duplicates with a shared root cause when one fix removes all outcomes | every finding has a verdict |
| **Verdict**: `proven` (reproduction `--run R#`), `probable` (`static: <file:line chain>` or `no-repro: <why>; <chain>`), `question` (`intent: <source>; <file:line>`, non-blocking findings only), `dismissed` (run or `file:line`) | `review verdict` (or `--from-json`) | `proven` and `probable` findings are resolved. With `--review-only`, they go into the report as warnings |
| **Resolution**: `fixed` (only after a code change) or `accepted` (never for blocking bugs) | `review resolve` | none |
| **Run**: argv, working directory, exit code, output tail, diff fingerprint | `craftsman run` | a passing test run (and e2e in `deep`) on the final code that did not modify files. The last run of every command succeeds unless retired with `review waive-run` and a user-visible reason |
| **Agent event**: reviewer subagent start and stop | platform hooks | in `standard` and `deep`: an independent reviewer finished after the round was prepared |

Every record is tied to a diff fingerprint, so a code change invalidates the evidence. The next
round reviews the changed hunks and any hunks that reference a changed symbol. Tests must be run
again. Generated files (`__pycache__`, build caches, `node_modules`, …) are skipped, and custom
patterns go in `.craftsmanignore`.

### Lenses

![Two-phase review: lens matrix, verification, and verdict lifecycle](docs/diagrams/review-protocol-and-verdicts.png)

A lens is a narrow review angle with a list of specific checks
([`skills/code-review/lenses.md`](skills/code-review/lenses.md)). The review packet selects them by
gear and by the risk signals in the diff.

| Lens | What it looks for | When |
| :--- | :--- | :--- |
| `intent` | missing pieces, incomplete change propagation, unrequested changes, fixes in the wrong layer | always |
| `contracts` | input domains, boundary conditions, truthiness, wrong variable, inconsistent return values | always |
| `craft` | code reuse, needless complexity, misleading names and comments, wasted work | always |
| `failure` | swallowed errors, partial side effects, retries, fail-open guards | `standard`+ or error and I/O signals |
| `ripple` | broken callers, removed guarantees, overridden methods, persisted data compatibility | `standard`+ or deletions and config |
| `tests` | tests that would pass even if the change were wrong | `standard`+ or test files |
| `state` | aliasing, caches, check-then-act races, `async` bugs, clocks | `deep` or concurrency, filesystem, time signals |
| `trust` | untrusted data in shells, SQL, paths, deserializers; authorization | `deep` or process, SQL, auth, network signals |
| `platform` | paths, encodings, shells, versions across operating systems | `deep` or platform signals |
| `model` | invalid states allowed by the data model | `deep` or new types |
| `sweep` | interactions between hunks, at the end, fresh reviewer | `standard`+ |

### Gears

| Gear | Trigger | Review | Verification |
| :--- | :--- | :--- | :--- |
| `quick` | `--quick` or automatically for ≤ 40 changed lines in ≤ 2 files | `intent`, `contracts`, `craft` + signal-triggered lenses, in the current context | passing tests |
| `standard` | default | always-on + signal-triggered lenses in subagents, `sweep`, verifier | passing tests on the final code |
| `deep` | `--deep` or the `ultracraft` keyword | all lenses, reviewers, `sweep`, verifier | passing tests **and** e2e |

### Platform enforcement

![Lifecycle hooks, gate state machine, and Git protection](docs/diagrams/lifecycle-hooks-and-guards.png)

The protocol, lenses, subagents, and ledger are shared across all platforms. What differs is the
mechanism that blocks completion.

| Platform | Stop gate | Reviewer isolation | Subagents |
| :--- | :--- | :--- | :--- |
| GitHub Copilot in VS Code | `Stop` hook blocks until `review check` passes | `SubagentStart`/`SubagentStop` hooks | `craftsman-reviewer`, `craftsman-verifier` agents |
| GitHub Copilot CLI | `agentStop` hook | `subagentStart`/`subagentStop` hooks | the same agents |
| Claude Code | `Stop` hook | `SubagentStart`/`SubagentStop` hooks | the same agents in `~/.claude/agents` |
| Google Antigravity | `Stop` hook | finished `craftsman-reviewer` subagents in the transcript | `invoke_subagent` (`TypeName: self`, `Role: craftsman-reviewer`) |
| Codex, Cursor, and others | `craftsman gate --complete` refuses completion until the ledger passes validation | none | none |

The gate runs only in a `/craftsman` session; outside it, the hooks do nothing. After six forced
continuations in a row without fewer open issues, the stop hook lets the agent through and tells
the user that the gate was not satisfied. When a hook fails, the agent goes on with a warning that
the gate was not checked; a failing `pre-tool` check asks the user to confirm the tool call
instead (Copilot denies it).

## Installation

Python 3.10 or newer, standard library only.

```bash
git clone https://github.com/BuczynskiRafal/craftsman.git ~/.craftsman
cd ~/.craftsman
./install.sh                 # auto-detect Copilot / Claude Code / Antigravity + global Git hooks
```

For a chosen platform:

```bash
./install.sh --copilot --git-hooks        # VS Code + Copilot CLI: instructions, skills, agents, user hooks
./install.sh --claude --git-hooks         # Claude Code: commands, agents, and the gate hooks
./install.sh --antigravity --git-hooks    # Google Antigravity
./install.sh --copilot-workspace ~/src/app   # commit .github/{skills,agents,hooks} to a repo
./install.sh --agents-workspace ~/src/app    # AGENTS.md + .cursor/rules for Codex and Cursor
```

The same through the CLI, on any OS, including Windows PowerShell:

```bash
pip install .
craftsman install                                  # same flags as ./install.sh (--auto, --all, --dry-run, …)
craftsman uninstall                                # remove manifest-owned files and restore core.hooksPath
craftsman export --user-dir --copilot-skills-dir   # VS Code profile instructions + ~/.copilot/{skills,agents,hooks}
craftsman export --workspace path/to/repo          # .github/ for a team repository
craftsman export --claude-dir                      # ~/.claude (settings.json is merged, a .bak is kept)
craftsman doctor                                   # installation health, hooks, agents, duplicates, drift
craftsman doctor --fix-duplicates                  # remove obsolete Craftsman duplicate files, keeping canonical copies
craftsman skill list                               # list bundled skills without searching the disk
craftsman skill show code-review --file lenses.md  # print a bundled skill or companion file
```

| `craftsman install` / `install.sh` flag | Effect |
| :--- | :--- |
| *(none)* / `--auto` | Detects installed platforms, installs for each, and adds global Git hooks. |
| `--copilot` | Global instructions (`craftsman-rules.instructions.md` per VS Code profile), Agent Skills (`~/.copilot/skills/<skill>/SKILL.md`), the three custom agents (`~/.copilot/agents/<name>.agent.md`), and gate hooks (`~/.copilot/hooks/craftsman.json`). |
| `--copilot-prompt-files` | Legacy fallback for older VS Code versions without Agent Skills: writes `<skill>.prompt.md` into VS Code user/profile `prompts/` instead of `~/.copilot/skills/` (mutually exclusive with `--copilot-skills-dir`). |
| `--claude` | Commands, rules, reviewer and verifier agents in `~/.claude`, gate hooks in `settings.json`. `--no-claude-hooks` skips the hooks, `--claude-hooks` is an alias for `--claude`. |
| `--antigravity` (`--copy`, `--symlink`, `--plugin`) | Skills, rules, scripts, and `hooks.json` in `~/.gemini/config`. `--copy`, `--symlink`, and `--plugin` are mutually exclusive. |
| `--copilot-workspace <dir>` | `.github/skills`, `.github/agents`, `.github/hooks/craftsman.json`, and a marked block in `.github/copilot-instructions.md` (plus `.github/prompts` when `--copilot-prompt-files` is passed). Workspace hooks call `craftsman` from `PATH`. |
| `--allow-duplicate-scopes` | Overrides the default refusal when installing `--copilot-workspace` on a machine that already has a user-level `--copilot` install (or vice versa). |
| `--agents-workspace <dir>` | Marked block in `AGENTS.md` and `.cursor/rules/craftsman.mdc` (aliases: `--codex-workspace`, `--cursor-workspace`). |
| `--git-hooks` / `--no-git-hooks` | Installs or skips global `commit-msg`, `pre-push`, and passthrough hooks (`core.hooksPath`). |
| `--all` | All platforms, without detection. `--no-claude-hooks` skips the Claude Code hooks. |
| `--dry-run` | Prints the install or uninstall plan without changing files or Git config. |

#### GitHub Copilot discovery locations ("one name = one location")

VS Code Copilot and the GitHub Copilot CLI scan multiple directories at once and merge discovered items without deduplication. To ensure every Craftsman agent and `/skill` slash command appears **exactly once**, Craftsman installs into a single canonical location per scope (`COPILOT_DISCOVERY` in [`craftsman/export.py`](craftsman/export.py)):

| Entity | User scope (`--copilot`, canonical) | Workspace scope (`--copilot-workspace`, canonical) | Legacy / non-canonical locations scanned by VS Code (`doctor` checks for duplicates) |
| :--- | :--- | :--- | :--- |
| **Custom agents** (`craftsman`, `craftsman-reviewer`, `craftsman-verifier`) | `~/.copilot/agents/*.agent.md` | `<workspace>/.github/agents/*.agent.md` | `Code/User/prompts/*.agent.md`, `Code/User/profiles/*/prompts/*.agent.md`, `~/.claude/agents/*.md`, `.claude/agents/*.md` |
| **Slash commands / Agent Skills** (`/craftsman`, `/code-review`, …) | `~/.copilot/skills/<name>/SKILL.md` | `<workspace>/.github/skills/<name>/SKILL.md` | `Code/User/prompts/*.prompt.md`, `Code/User/profiles/*/prompts/*.prompt.md`, `.github/prompts/*.prompt.md` (`--copilot-prompt-files`), `~/.claude/skills/*/SKILL.md` |
| **Global instructions** | `Code/User/prompts/craftsman-rules.instructions.md` (+ each profile) | `<workspace>/.github/copilot-instructions.md` | — |

- Installing both `--copilot` (user) and `--copilot-workspace` (workspace) in the same project would show every agent and command twice in VS Code, so `install` and `export` refuse by default unless `--allow-duplicate-scopes` is passed.
- Upgrading from v3.0.0 with `craftsman install --copilot` (or running `craftsman doctor --fix-duplicates`) automatically removes obsolete Craftsman-generated `.agent.md` and `.prompt.md` files from `Code/User/prompts/` and `profiles/*/prompts/` while leaving user-authored files untouched.
- If both GitHub Copilot and Claude Code are installed on the same machine, VS Code also scans `~/.claude/agents` by default; `craftsman doctor` detects the collision and shows the `"chat.agentFilesLocations": {"~/.claude/agents": false}` setting to add to VS Code `settings.json`.

### Uninstall

Each install target writes a `<base>/.craftsman-install.json` manifest with the created paths and
the previous `core.hooksPath` value. Files that existed before are backed up instead of being
overwritten.

`craftsman uninstall` (or `make uninstall`) accepts the same target flags as installation (`--all`,
`--antigravity`, `--claude`, `--copilot`, `--git-hooks`, `--copilot-workspace <dir>`,
`--agents-workspace <dir>`, `--dry-run`). It removes only the paths from the manifest and Craftsman
entries from `~/.claude/settings.json` and `~/.gemini/config/hooks.json`. It restores the previous
`core.hooksPath`. If that value was changed after installation, it leaves the current value and
prints a warning.

## Usage

In Copilot Chat or Claude Code:

```
/craftsman add pagination to the catalog search
/craftsman --deep migrate the session store to Redis
/code-review                      # review only, findings + verdicts, no fixes
/code-review --fix                # review, fix, re-review
```

- `craftsman status`: gear, round, missing evidence.
- `craftsman review show`: coverage, findings, runs.
- `craftsman review report`: final report for the user.

A review without `--fix` finishes with `craftsman review check --review-only` and
`craftsman review report --review-only`. They check the round, lens and `sweep` coverage, and
verdicts, without requiring fixes or tests. The result is
`**Craftsman review: COMPLETE (review-only)**` / `Review: complete (review-only)` or
`**Craftsman review: INCOMPLETE (N open items)**`.

Review also works before the first commit, using the empty tree as the base. `craftsman review reset`
archives a ledger from an older development version that cannot be read.

## Skills

| Command | What it does |
| :--- | :--- |
| `/craftsman` | Orchestrator: base commit, track selection, quality loop, gate. |
| `/feature-dev` | Code exploration, resolving open questions, comparing designs, implementation. |
| `/modernize` | Capturing current behavior in rule cards and characterization tests, then transformation. |
| `/batch` | Splitting a large migration into independent units for parallel agents. |
| `/simplify` | Cleaning up the change without changing behavior: code reuse, simpler control flow, less wasted work. |
| `/code-review` | Review with the ledger described above. |
| `/verify` | Running the changed behavior (CLI, server, pipeline) with recorded runs. |
| `/audit`, `/audit-errors`, `/audit-tests`, `/audit-types`, `/audit-comments`, `/audit-security` | Single-angle audit of a change or module. |
| `/code-quality`, `/extreme-optimizer`, `/leetcode-style-code-reviewer` | Review of code structure and performance. |
| `/high-level-commit-message` | Commit message in Conventional Commits format for the current change. |

## Guards

- **Gate** (`craftsman gate`, `craftsman hook stop`): the state machine and stop hook described
  above. The state lives in `<git-dir>/craftsman/craftsman_state.json`, next to the ledger. Each
  active gate is bound to the agent session (`owner.platform` + `owner.session_id`) that opened
  it, so parallel agent sessions in the same checkout are never blocked by or allowed to corrupt
  another session's gate (use `git worktree add` for parallel work, or `craftsman gate --init --takeover`
  to explicitly take over an active gate). Every `craftsman gate` action refuses another session's
  gate; a command that cannot be traced to its session names it with `--session-id`. A state file that is not a valid gate state is an error
  for every command and hook until `craftsman gate --init --takeover` replaces it. On platforms
  without hooks, `craftsman gate --complete` checks the same thing.
- **Git guard** (`craftsman guard`, `craftsman hook pre-tool`, global `pre-push` and `commit-msg`
  hooks): blocks unconfirmed pushes to `main`/`master`, including through `bash -c`, aliases, and
  `gh pr merge`. Rejects commits, tags, and PRs with AI attribution trailers.
- **Search scope guard** (`craftsman hook pre-tool`, [`craftsman/scope.py`](craftsman/scope.py)):
  confines Craftsman sessions and `craftsman-*` subagents to the current git work tree.

### Search scope

During an active `/craftsman` session (or inside a `craftsman`, `craftsman-reviewer`, or
`craftsman-verifier` agent), the `pre-tool` hook enforces a repository search-scope policy:

- **Allowed**:
  - Reading, listing, and searching inside the active git work tree (`git rev-parse --show-toplevel`,
    including submodules and nested repositories inside it) and inside dedicated subdirectories in the
    system temp directory (`$TMPDIR` / `/tmp`).
  - Reading a specific file or non-recursively listing a directory outside the work tree (such as
    installed dependency/stdlib paths, parent directories like `ls ..`, or files cited by stack traces),
    as long as the target is not inside another git repository, an assistant session store, or a shell
    history file.
- **Blocked**:
  - Recursive search or directory listing (`find`, `rg`, `grep -R`, `fd`, `ls -R`, `tree`, `du`, `Glob`,
    `Grep`, `LS`, etc.) rooted outside the work tree (such as `~`, `..`, `/Users`, `/home`, or a sibling repo).
  - Reading or listing files in other git repositories, `~/.claude/projects`, `~/.copilot/session-state`,
    `~/.gemini/antigravity/brain`, `~/.gemini/antigravity/conversations`, other repositories'
    `.git/craftsman` data, or shell history files.
- **Configuration (per repository via `git config`)**:
  ```bash
  # Allow additional directories outside the work tree (colon-separated on POSIX, semicolon on Windows, or multi-valued):
  git config --add craftsman.scopeAllow ../shared-schemas
  # Switch enforcement mode: deny (or enforce, default, blocks), warn (allows with warning), or off (disabled):
  git config craftsman.scope warn
  git config craftsman.scope off
  ```

Guards protect against an agent that rushes or forgets a step. They are not a sandbox: an agent
that deliberately edits Craftsman's state files will bypass the gate.

## Development

```bash
make check            # ruff, mypy --strict, the unit tests, doctor
```

## Citation

The pilot uses the **SWR-Bench** dataset (in the repository: SWRBench):

```bibtex
@article{zeng2026swrbench,
  author    = {Zeng, Zhengran and Shi, Ruikai and Han, Keke and Li, Yixin and Sun, Kaicheng and Wang, Yidong and Yu, Zhuohao and Xie, Rui and Ye, Wei and Zhang, Shikun},
  title     = {{SWR-Bench}: Assessing {LLM} Performance in Real-World Code Review Comment Generation},
  journal   = {Proceedings of the ACM on Software Engineering},
  volume    = {3},
  number    = {FSE},
  articleno = {FSE137},
  numpages  = {23},
  year      = {2026},
  publisher = {Association for Computing Machinery},
  issn      = {2994-970X},
  doi       = {10.1145/3808144},
  url       = {https://doi.org/10.1145/3808144},
  eprint    = {2509.01494},
  archivePrefix = {arXiv},
  primaryClass  = {cs.SE}
}
```

The data comes from `data/swr_datasets_d5c5.jsonl` in the authors' repository
[ZZR0/SWRench](https://github.com/ZZR0/SWRench) (MIT license, the repository README asks to cite
the paper above). The instances contain code, comments, and descriptions from twelve Python
projects, covered by their own licenses. Craftsman does not redistribute the dataset.

## License

MIT, see [LICENSE](LICENSE). The review protocol, lens playbooks, agents, gate, and ledger were
created for Craftsman.
