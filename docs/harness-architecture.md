# Craftsman v3.0 Harness Architecture & Execution Flow

Craftsman is an **adaptive closed-loop engineering harness** for AI coding agents. Rather than relying on prompt-only advice ("remember to review and test your changes"), Craftsman couples **context-enriched review packets**, a **two-phase review protocol** (high-recall candidate collection decoupled from adversarial reproduction), and a **cryptographically fingerprinted evidence ledger** enforced at runtime by IDE/CLI lifecycle hooks.

This reference documents the end-to-end architecture, state machines, data structures, and invariants of the Craftsman v3.0 harness.

---

## Visual Architecture Suite (Excalidraw)

All diagrams in `docs/diagrams/` are maintained as editable Excalidraw scenes (`.excalidraw`) and rendered to high-DPI PNGs via `@excalidraw/cli`:

```bash
npx @excalidraw/cli render docs/diagrams/<name>.excalidraw \
  --out docs/diagrams/<name>.png --scale 2 --max-width 4096 --max-height 4096
```

| Diagram | Shows | Editable Source | Rendered PNG |
| :--- | :--- | :--- | :--- |
| **1. Workflow** | The ten steps from prompt to closed gate, the review loop, and what each tier changes | [`harness-overview.excalidraw`](diagrams/harness-overview.excalidraw) | [`harness-overview.png`](diagrams/harness-overview.png) |
| **2. Evidence ledger** | What `review prepare` builds, what the ledger records, which hunks the next round re-reviews, and when the gate passes | [`evidence-ledger-flow.excalidraw`](diagrams/evidence-ledger-flow.excalidraw) | [`evidence-ledger-flow.png`](diagrams/evidence-ledger-flow.png) |
| **3. Install, hooks and git guard** | Where `craftsman install` puts things, what each session hook does, the stop hook's outcomes, and the two git rules | [`lifecycle-hooks-and-guards.excalidraw`](diagrams/lifecycle-hooks-and-guards.excalidraw) | [`lifecycle-hooks-and-guards.png`](diagrams/lifecycle-hooks-and-guards.png) |
| **4. Review and verdicts** | Lenses per tier, the two review phases, and the evidence each verdict and resolution needs | [`review-protocol-and-verdicts.excalidraw`](diagrams/review-protocol-and-verdicts.excalidraw) | [`review-protocol-and-verdicts.png`](diagrams/review-protocol-and-verdicts.png) |

---

## 1. End-to-End Closed-Loop Execution & 3-Gear Topology

![Craftsman workflow from prompt to closed gate](diagrams/harness-overview.png)

### Lifecycle Stages

Every `/craftsman` (or `ultracraft`) session progresses through four stages tracked in `.git/craftsman/craftsman_state.json`:

1. **Stage 0 — Baseline Capture & Gate Initialization (`craftsman.gate`)**
   - Triggered automatically by the platform's `UserPromptSubmit` / `userPromptSubmitted` hook when the prompt contains `/craftsman` or `ultracraft`, or explicitly via `craftsman gate --init --tier <quick|standard|deep> --task "<description>"`.
   - Captures `base_sha`: `git rev-parse HEAD` if the repository has commits, or the Git empty-tree SHA (`4b825dc642cb6eb9a060e54bf8d69288fbee4904`) in an unborn repository before its first commit.
   - If an existing `.git/craftsman/ledger.json` belongs to a different `base_sha`, `archive_ledger_for_new_base()` / `archive_ledger_if_rebased()` (`craftsman.evidence.archive_ledger()`) archives it to `ledger-<timestamp>.json` and moves old packets into `archive-<timestamp>/` so stale rounds from a previous task never leak into the new session.
   - Supports mid-task human clarification via `craftsman gate --await-user` (which pauses the `Stop` hook so the agent can yield its turn and wait for the user's answer) and `craftsman gate --resume`.

2. **Stage 1 — Implementation Track**
   - The orchestrating agent selects the implementation workflow matching the task shape:
     - `/feature-dev`: codebase exploration -> user clarification -> trade-off design -> incremental implementation.
     - `/modernize`: legacy behavior extraction into numbered rule cards -> characterization tests -> behavior-preserving transformation.
     - `/batch`: decomposition of repository-wide migrations into disjoint parallel work units.
     - **Direct implementation**: targeted bug fixes or small enhancements.

3. **Stage 2 — The Quality Loop (`/simplify` -> `/code-review` -> `/verify`)**
   - **Step 2.1 (`/simplify`)**: Runs a behavior-preserving cleanup pass across the cumulative diff (`base_sha..worktree`) to reuse existing helpers, flatten nested control flow, and strip dead code or dishonest comments before any reviewer sees the diff.
   - **Step 2.2 (`craftsman review prepare`)**: Snapshots the worktree, computes hunk and diff SHA-256 fingerprints, scans 16 risk signals, extracts AST/brace enclosing scopes and cross-repo caller references, and writes `.git/craftsman/packet-N.md`.
   - **Step 2.3 (Lens Passes)**: Fresh-context `craftsman-reviewer` subagents examine `packet-N.md` through the selected lenses (`Wave 1` in parallel for domain lenses, followed by `Wave 2` for the cross-hunk `sweep` lens) and record candidate findings (`review add`) and per-hunk notes (`review cover`).
   - **Step 2.4 (Adversarial Challenge)**: When a round has unjudged findings, a fresh-context `craftsman-verifier` subagent attempts to disprove each candidate against the real source tree and executes minimal reproduction scripts (`craftsman run --kind repro -- ...`) for survivors, recording `proven`, `probable`, or `dismissed` verdicts (`review verdict`).
   - **Step 2.5 (Fix, Resolve & Delta Re-Review)**: The orchestrator fixes every `proven` and `probable` finding at its root cause, writes regression tests, marks findings resolved (`review resolve`), and opens Round `N+1`. Because the code changed, the diff fingerprint rotates and Round `N+1` re-reviews **only** modified hunks and unchanged hunks that reference a modified symbol.
   - **Step 2.6 (`/verify` — Runtime Verification)**: Once all findings are resolved and the current round is 100% covered, the agent runs the project's test suite (`craftsman run --kind test -- ...`) and, on `deep` tier, a real end-to-end execution (`craftsman run --kind e2e -- ...`) at the final diff fingerprint.

4. **Stage 3 — Gate Completion & Reporting**
   - `craftsman review check` (and the platform `Stop` hook) verifies all 12 ledger invariants against the live worktree.
   - `craftsman gate --complete` marks `active = False, stage = "completed"`, and `craftsman review report` emits the final audit table (diff stats, rounds, lenses, fixed/accepted/dismissed counts, and verified commands).

---

### The 3-Gear Adaptive Gearbox (`craftsman.gate.resolve_effective_tier`)

Craftsman scales review ceremony to the blast radius of the change while guaranteeing that **the effective tier never downgrades** once escalated (`TIER_RANK = {"quick": 0, "auto": 1, "standard": 2, "deep": 3}`):

| Gear | Selection Rule | Active Lenses | Subagent Isolation | Runtime Verification |
| :--- | :--- | :--- | :--- | :--- |
| **`quick`** *(Soft-Loop)* | Explicit `--quick`, or auto-selected when `tier == "auto"` and cumulative diff (`git diff --numstat base_sha` + untracked files) touches **<= 40 lines across <= 2 files** | `intent`, `contracts`, `craft` + any signal-triggered lenses (or `intent`, `craft` for docs-only diffs) | Inline in the active agent thread (`0` subagents required) | >= 1 non-trivial passing `test` or `e2e` run at the final diff fingerprint |
| **`standard`** *(Hybrid Hard-Loop)* | Default (`--standard`), or auto-promoted when cumulative diff exceeds 40 lines or 2 files | `intent`, `contracts`, `failure`, `ripple`, `tests`, `craft` + signal-triggered lenses + `sweep` | Required when platform hooks are active (`craftsman-reviewer` + `craftsman-verifier`) | Passing `test` run at the final diff fingerprint + recorded `SubagentStop` event |
| **`deep`** *(Full-Audit Hard-Loop)* | Explicit `--deep` or prompt keyword `ultracraft` (which also mandates an `ultrathink` design analysis before coding) | **All 11 lenses** (`intent`, `contracts`, `failure`, `state`, `trust`, `ripple`, `platform`, `model`, `tests`, `craft`, `sweep`) | Full Wave 1 parallel `craftsman-reviewer` pool + Wave 2 `sweep` reviewer + `craftsman-verifier` | Passing `test` run **AND** passing `e2e` run at the final diff fingerprint |

---

## 2. Evidence Ledger, Diff Fingerprinting & Delta Carry-Over

![Evidence ledger: what is recorded and when the gate passes](diagrams/evidence-ledger-flow.png)

### Worktree Snapshot & Two-Level SHA-256 Fingerprinting (`craftsman.diffscan`)

Every ledger check compares the recorded state against a live `DiffSnapshot` generated by `diffscan.snapshot(cwd, base_sha)`:

1. **Tracked + Untracked Capture**
   - Runs `git diff --no-color --no-ext-diff -U3 --find-renames <base_sha>` to capture all committed, staged, and unstaged modifications since `base_sha`.
   - Runs `git ls-files --others --exclude-standard -z` to discover untracked files not yet staged, synthesizing added hunks for text files up to `1 MiB` (`MAX_UNTRACKED_BYTES`).
   - Synthesizes descriptive contentless-change hunks (`_describe_contentless_change()`: `~ mode ...`, `~ renamed from ...`, or `~ binary file changed`, with `new_start = 0, new_len = 0`) for pure mode changes, pure renames, binary changes, or oversized untracked files so no worktree modification can bypass review coverage.
   - Filters out generated artifacts matching `GENERATED_ARTIFACT_PATTERNS` (`__pycache__/`, `.pytest_cache/`, `.mypy_cache/`, `.ruff_cache/`, `node_modules/`, `.tox/`, `.nox/`, `.eggs/`, `*.egg-info/`, `.coverage`, `htmlcov/`, `*.pyc`, `*.pyo`, `.DS_Store`) plus custom glob patterns in `.craftsmanignore`.

2. **Level 1: Hunk Digest (`Hunk.digest` — 16-hex SHA-256 prefix)**
   - Computed from:
     ```text
     SHA256(file_path + "\0" + fn_header + "\0" + normalized_diff_lines)[:16]
     ```
   - **Line-number invariant**: Hunk start line numbers (`@@ -a,b +c,d @@`) are intentionally excluded from the digest calculation. If Round 2 inserts lines at the top of a file, unchanged hunks further down shift their line numbers but **retain the exact same 16-hex digest**, preserving their coverage from Round 1.
   - **CRLF-invariant**: Trailing `\r` characters are stripped before hashing so staging a file on Windows (`core.autocrlf`) never invalidates a review round.

3. **Level 2: DiffSnapshot Fingerprint (`DiffSnapshot.fingerprint` — 64-hex SHA-256)**
   - Computed over all sorted `ChangedFile` entries:
     ```text
     SHA256(path + "\0" + status_salt + "\0" + (h.digest:h.new_start:h.new_len for h in hunks))
     ```
   - Unchanged untracked files (`A`) and newly staged added files (`A`) share the same empty `status_salt`, so running `git add` on an already-reviewed untracked file keeps the exact same 64-hex `DiffSnapshot.fingerprint`.

---

### Context Indexing (`WorktreeIndex`) & 16 Risk Signals (`craftsman.packet`)

To cure the "reads the diff, not the code" failure mode, `packet.collect_context()` builds a `WorktreeIndex` (`craftsman.diffscan.WorktreeIndex`) that reads and parses each modified source file **at most once** per round:

- **Enclosing Scope Extraction (`enclosing_scope`)**:
  - For Python (`.py`), parses the file with `ast.parse`, walks all `FunctionDef`, `AsyncFunctionDef`, and `ClassDef` nodes (including decorator start lines), and selects the smallest AST node spanning the hunk's modified lines (`<= 160` lines, or falling back to an innermost `def` or `±30`-line window).
  - For braces/indent languages (TypeScript, JavaScript, Go, Rust, Java, C/C++, C#, Ruby), scans upward to the nearest signature starter (`func`, `fn`, `def`, `class`, `struct`, `impl`, etc.) and balances braces `{...}` or indentation to capture the complete enclosing function.
- **Defined Symbol Extraction (`defined_symbols`)**:
  - Collects names of modified functions/classes and enclosing scopes so the harness knows which symbols changed in the current hunk.
- **Cross-Repository Caller Discovery (`find_references`)**:
  - `WorktreeIndex._tracked_files_mentioning()` executes `git grep -l -I -w -F -z -e <symbol> --` across the repository for each modified symbol, and `packet._references()` collects caller locations outside the defining hunk (`REFERENCE_SEARCH_LIMIT = 40`, `REFERENCES_PER_FILE = 5`, `MAX_REFERENCES_PER_SYMBOL = 8`).
  - Lists up to 8 concrete caller call-sites (`path:line: snippet`) per symbol directly under each hunk in `packet-N.md`.
- **16 Automated Risk Signals (`detect_signals`)**:
  - Scans added/removed lines (`+`/`-`) and file paths against 16 regex/path detectors:
    `process`, `filesystem`, `network`, `sql`, `deserialization`, `concurrency`, `time`, `regex`, `auth`, `errors`, `datamodel`, `platform`, `config`, `tests`, `deletion`, and `docs`.
  - Each detected signal is printed per hunk and at the round level in `packet-N.md`, automatically activating specialized review lenses even on `quick` or `standard` tiers.

---

### Evidence Ledger Schema & Atomic Persistence (`craftsman.ledger`)

All verification state resides in `.git/craftsman/ledger.json` (`SCHEMA_VERSION = 1`). Concurrent writes from parallel `craftsman-reviewer` subagents are serialized via `ledger.mutate(path, fn)`:

- **Cross-Platform Exclusive File Lock**: Acquires `.git/craftsman/ledger.lock` using `fcntl.flock(..., LOCK_EX)` on POSIX or `msvcrt.locking(..., LK_NBLCK)` on Windows across a bounded 10-second retry window.
- **Crash-Safe Atomic Write**: Serializes the updated `Ledger` dataclass to a temporary file in `.git/craftsman/` (`tempfile.mkstemp`), flushes to disk, and atomically replaces `ledger.json` via `os.replace()`.
- **Strict Deserialization (`_Fields`)**: Rejects unknown fields, wrong primitive types, booleans masquerading as `int`, and out-of-domain literal values (`VERDICTS`, `RESOLUTIONS`, `SEVERITIES`, `RUN_KINDS`), raising `LedgerError` / `FieldError` with a recovery hint (`craftsman review reset`) if a file is malformed.

The ledger stores five immutable tuple collections:

1. **`rounds: tuple[Round, ...]`** — Each `Round` records `number`, `fingerprint`, `base_sha`, `tier`, `prepared_at`, `lenses`, and `hunks: tuple[HunkRef, ...]`.
2. **`coverage: tuple[Coverage, ...]`** — Written by `craftsman review cover --lens <L> --hunks H1,H2 --note "..."`. Stores hunk **digests** (not transient `H#` labels) and enforces:
   - At most `4` hunks per CLI call (`MAX_HUNKS_PER_COVERAGE = 4`) to prevent rubber-stamping 20 hunks with a single generic note.
   - Minimum note length (`>= 30` chars, `MIN_COVERAGE_NOTE_CHARS = 30`) and explicit mention (`\bH1\b`, `\bH2\b`) of every covered hunk ID inside `--note`.
3. **`findings: tuple[Finding, ...]`** — Written by `review add` (`F1..Fn`) and updated by `review move`, `review verdict`, and `review resolve`. Each finding records the `fingerprint` at which it was filed.
4. **`runs: tuple[Run, ...]`** — Written by `craftsman run --kind <test|e2e|repro|lint|build|other> -- <cmd>`. Captures `fingerprint` before execution and `fingerprint_after` after execution; `counts_as_passing` is `True` only when `exit_code == 0` **and** `fingerprint_after is None` (i.e., the test run did not dirty the working tree) **and** `not is_trivial_run(argv)` (rejecting `true`, `echo`, `python -c "pass"`, etc. via AST and shell segment inspection).
5. **`agents: tuple[AgentEvent, ...]`** — Written exclusively by platform `SubagentStart` / `SubagentStop` hooks (no CLI subcommand is exposed to the agent).

---

### Delta Carry-Over & Transitive Symbol Reopening (`ledger._carry_over`)

When the agent fixes findings after Round `N` and runs `craftsman review prepare` for Round `N+1`, Craftsman avoids re-reviewing untouched code from scratch while catching interaction bugs introduced by the fix:

1. **Changed Hunks**: Any hunk whose 16-hex `digest` was not present in Round `N` has `carried = False, reopened = False` and collects its modified symbols $S_0 = \bigcup \text{defined\_symbols}(h)$.
2. **Transitive Symbol Propagation**: Across all unchanged hunks, if a hunk mentions a symbol $s \in S$ (`\bs\b` on its added or context lines extracted by `_mentioned_words(hunk)`), that hunk is marked **reopened** (`carried = False, reopened = True`) and its own `defined_symbols` are added to $S$ until a fixed point is reached.
3. **Prior Coverage Invalidation**: When a hunk is reopened in Round `N+1`, `evaluate()` restricts valid coverage for that hunk to `c.round >= reopened_since[digest]`, forcing every selected lens to re-examine the caller hunk against the new implementation.
4. **Independent Unchanged Hunks**: Hunks whose digest is unchanged, that do not mention any modified symbol in $S$, and that were covered by all selected lenses in Round `<= N` are marked `carried = True`. `packet-N.md` renders them as a compact one-line header (`### H# · \`path\` Lstart–Lend · carried (covered in round M, unchanged)`).

---

### The 12 Gate Issue Codes (`craftsman.ledger.evaluate`)

`ledger.evaluate(led, snap, policy, *, review_only=False)` returns a `GateReport`. The gate passes (`report.passed == True`) if and only if `issues == ()` across all 12 checks (or in `--review-only` mode for standalone `/code-review` without `--fix`, checks 1–6 and 11–12 while surfacing unresolved `proven`/`probable` findings and failing runs as warnings and rendering `**Craftsman review: COMPLETE (review-only)**` / `Review: complete (review-only)` or `**Craftsman review: INCOMPLETE (N open items)**`; `--review-only` is rejected when an active `/craftsman` gate session exists):

| # | Issue Code | Condition Triggering Failure | Exact CLI Remediation |
| :---: | :--- | :--- | :--- |
| 1 | `no-round` | `ledger.rounds` is empty | `craftsman review prepare` |
| 2 | `stale-round` | Latest round's `fingerprint != snap.fingerprint` or `base_sha != snap.base_sha` | `craftsman review prepare` |
| 3 | `tier-too-low` | Latest round's `tier` rank is lower than `GatePolicy.tier` | `craftsman review prepare` |
| 4 | `uncovered` | Any selected `lens x non-carried hunk` pair lacks a `Coverage` record (since reopening) | `craftsman review cover --lens <L> --hunks H# --note '...'` |
| 5 | `sweep-too-early` | The `sweep` lens recorded coverage at timestamp $t_{\text{sweep}} < \max(t_{\text{other}})$ | Re-run `craftsman review cover --lens sweep ...` after all other lenses finish |
| 6 | `no-verdict` | Any finding `F#` has `verdict is None` | Spawn `craftsman-verifier` -> `craftsman review verdict F# --as ...` |
| 7 | `unresolved` | A `proven`/`probable` finding has `resolution is None`, **or** was marked `fixed` but `finding.fingerprint == snap.fingerprint` (code was not changed or fix was reverted) | Fix code, run `craftsman review resolve F# --as fixed --note '...'`, then `craftsman review prepare` |
| 8 | `no-passing-run` | No non-trivial passing `test` or `e2e` run recorded at `snap.fingerprint` (with clean post-run fingerprint) | `craftsman run --kind test -- <test-command>` |
| 9 | `no-e2e-run` | `GatePolicy.tier == "deep"` and no passing `e2e` run exists at `snap.fingerprint` | `craftsman run --kind e2e -- <real-command>` |
| 10 | `failing-run` | A command's most recent run in the same `cwd` exited non-zero and was not retired | Fix and re-run, or retire via `craftsman review waive-run R# --reason '...'` |
| 11 | `no-isolation` | `GatePolicy.require_isolation` is `True` (`standard`/`deep` with platform hooks) and no `craftsman-reviewer` `stop` event was recorded after `round.prepared_at` | Delegate lens review to the `craftsman-reviewer` subagent |
| 12 | `round-cap` | `> 5` distinct diff fingerprints have been reviewed (`max_rounds = 5`) | Stop automated loop and escalate to the user (`stage = "needs_human"`) |

---

## 3. Multi-Platform Lifecycle Hooks, Stop Gate & Defense-in-Depth Git Guard

![Install, session hooks and git guard](diagrams/lifecycle-hooks-and-guards.png)

### Single-Source Multi-Platform Exporter & Installer (`craftsman.export` & `craftsman.install`)

Craftsman authors all skills (`skills/<name>/SKILL.md`), lens playbooks (`skills/code-review/lenses.md`), subagent prompts (`agents/craftsman-{reviewer,verifier}.md`), and global rules (`rules/*.md`) once, using `{{include path}}` directives (`expand_includes()`) that `craftsman export` and `craftsman install` (or `./install.sh`) resolve when compiling and installing native artifacts for four agent ecosystems (recording owned paths in `<base>/.craftsman-install.json` for clean removal via `craftsman uninstall`):

1. **GitHub Copilot (VS Code & Copilot CLI)**
   - Exports prompt files (`*.prompt.md`), global instructions (`craftsman-rules.instructions.md`), skills (`~/.copilot/skills/<skill>/SKILL.md`), custom agents (`~/.copilot/agents/craftsman*.agent.md` + VS Code profile `prompts/`), and user/workspace hook configurations (`~/.copilot/hooks/craftsman.json` or `.github/hooks/craftsman.json`) with both `bash` and `powershell` commands.
2. **Claude Code (`~/.claude`)**
   - Compiles slash commands (`~/.claude/commands/<skill>.md` with appendices inlined), subagents (`~/.claude/agents/craftsman-{reviewer,verifier}.md`), combined rules (`~/.claude/rules/craftsman-rules.md`), and merges `CLAUDE_HOOK_EVENTS` (`UserPromptSubmit`, `Stop`, `SubagentStart`, `SubagentStop`, and `PreToolUse` for `Bash`) into `~/.claude/settings.json` by default (`--no-claude-hooks` skips it) while preserving a `.bak` copy.
3. **Google Antigravity (`~/.gemini/config`)**
   - Installs skills (`~/.gemini/config/skills/`), rules (`~/.gemini/config/rules/`), and native hook scripts (`scripts/git-guard.py` and `scripts/craftsman-stop-gate.py`) wired into `~/.gemini/config/hooks.json`.
   - On Antigravity, subagent isolation is additionally verified by scanning the conversation's `transcript.jsonl` (`has_quality_gate_subagent_in_entries()`) for `invoke_subagent` tool calls.
4. **OpenAI Codex, Cursor & Team Repositories**
   - Injects a marked `<!-- craftsman:begin --> ... <!-- craftsman:end -->` protocol block into `AGENTS.md` (or `.github/copilot-instructions.md`) and writes `.cursor/rules/craftsman.mdc`.
   - On platforms without runtime `Stop` hooks, `craftsman gate --complete` enforces the identical ledger checks from the CLI.

---

### Lifecycle Hook Engine & Bounded Stop Gate (`craftsman.hooks` & `craftsman.gate`)

`craftsman hook <event>` normalizes the JSON stdin/stdout dialects of Claude Code, GitHub Copilot (VS Code), Copilot CLI, and Google Antigravity through a unified `_Reply` adapter:

- **`prompt` (`UserPromptSubmit` / `userPromptSubmitted`)**:
  - If a session is already `active` and `awaiting_user == True`, receiving the user's reply automatically clears `awaiting_user = False` and resets `forced_continuations = 0` and `best_issue_count = None` so the agent gets a fresh convergence budget.
  - If the user's message invokes `/craftsman` or `ultracraft`, initializes `craftsman_state.json` with `hooks_seen = <platform>` and injects an `additionalContext` banner informing the agent of the active baseline and tier.
- **`subagent-start` (`SubagentStart` / `subagentStart`)**:
  - When `craftsman-reviewer` starts, appends an `AgentEvent(event="start")` to `ledger.json` and injects the exact path to `packet-N.md`, the uncovered `lens: H#` pairs, and the CLI commands (`review add`, `review cover`) into the subagent's context.
  - When `craftsman-verifier` starts, injects the list of unjudged findings (`F# (severity, lens) at path:line — title`) and the CLI commands (`craftsman run --kind repro`, `review verdict`).
- **`subagent-stop` (`SubagentStop` / `subagentStop`)**:
  - Appends an `AgentEvent(event="stop")` to `ledger.json`.
  - Checks whether the reviewer recorded at least one new `Coverage` or `Finding` since its `start` event (or whether the verifier recorded verdicts for pending findings). If the subagent attempted to exit with zero ledger records, the hook blocks its stop **once** with exact CLI remediation instructions.
- **`stop` (`Stop` / `agentStop`) — Monotonic Progress-Tracked Gate**:
  - Evaluates the active session via `gate.evaluate_task(state)`:
    1. If `awaiting_user == True` (or Antigravity's transcript shows the last tool call was `ask_question`), returns `allow` so the user can answer.
    2. If `report.passed == True`, calls `mark_completed()` (`active = False, stage = "completed"`) and returns `allow`.
    3. If `round-cap` is present (`> 5` rounds), sets `stage = "needs_human"` and returns `allow` with a user-visible warning so the agent never loops infinitely.
    4. Otherwise, compares `len(report.issues)` against `state["best_issue_count"]`:
       - **Strict progress** (`len(issues) < best_issue_count`): updates `best_issue_count` and resets `forced_continuations = 0`.
       - **No progress** (`len(issues) >= best_issue_count`): increments `forced_continuations` up to `MAX_FORCED_CONTINUATIONS = 6`. If the counter exceeds `6`, sets `stage = "aborted_max_continuations"` and releases the session with a user warning; otherwise returns `block` with `[CRAFTSMAN GATE k/6]` and numbered CLI remediation steps.
- **Fail-Open Reliability Contract**:
  - Every hook entrypoint wraps `sys.stdin` parsing and handler dispatch in a top-level `try ... except Exception`: invalid JSON, non-Git directories, or unexpected internal errors always exit `0` with `{}` (emitting a `systemMessage` warning on `stop`) so a hook bug can never brick the developer's IDE session.

---

### Defense-in-Depth Git Guard (`craftsman.guard` & `git-hooks/`)

Craftsman enforces two non-negotiable repository hygiene rules across **two independent layers**—an agent-level `PreToolUse` hook (`craftsman hook pre-tool` / `scripts/git-guard.py`) that intercepts shell commands before they execute, and OS-level global Git hooks (`git-hooks/pre-push` and `git-hooks/commit-msg`) that catch `git` invocations from subshells, Makefiles, or IDE buttons:

1. **Shell Command Parser & Wrapper Unwrapper (`guard.split_shell_segments`)**
   - Tokenizes shell command strings across unquoted `;`, `|`, `&&`, `||`, `(...)`, `{...}`, and newlines while preserving multi-line heredoc bodies (`<<EOF ... EOF`) intact so commit messages passed via `cat <<'EOF'` can be inspected.
   - Strips leading `VAR=val` environment assignments and command wrappers (`env`, `sudo`, `nice`, `nohup`, `stdbuf`, `timeout`, `xargs`).
   - Recursively unwraps nested shell invocations (`sh -c "..."`, `bash -lc "..."`, `zsh -c "..."`, `dash`, `ksh`) so wrapped Git commands cannot evade inspection.

2. **Global Rule #1: No Unconfirmed Push to `main` / `master` (`analyze_git_push` & `analyze_gh_pr_merge`)**
   - Detects `git push` targeting `main` or `master` via explicit refspecs (`main`, `HEAD:main`, `+HEAD:refs/heads/master`, `--delete main`), broad flags (`--all`, `--mirror`), bare `git push` when `get_current_branch(cwd)` is `main`/`master`, and `gh pr merge` targeting `main`/`master`.
   - **PreToolUse & OS `pre-push` enforcement**:
     - On Claude Code and Copilot: returns `permissionDecision = "ask"` (or `"deny"` on Copilot where `"ask"` is unsupported), prompting the human user in the IDE UI.
     - On Google Antigravity (`scripts/git-guard.py`) and the stateless OS `pre-push` hook (`run_native_pre_push_hook()`): inspects recent conversation transcripts via `has_recent_antigravity_approval()` -> `was_ask_question_called_since_last_push()` to verify that the agent called `ask_question` / `AskUserQuestion` about pushing/merging to `main`/`master` since the last push (or prompts `/dev/tty` interactively when invoked outside an approved session).

3. **Global Rule #2: Zero AI Attribution (`has_ai_attribution`)**
   - Scans commit messages (`git commit -m`, `-F`, heredocs, `.git/COMMIT_EDITMSG`), annotated tags (`git tag -m`), and pull requests (`gh pr create --title/--body`, `gh pr merge --body`).
   - Respects `git commit -v` diff scissors (`# ------------------------ >8 ------------------------`) and strips `#` comment lines before scanning, so diff lines inside commits that discuss attribution rules never trigger false positives.
   - Blocks `Co-authored-by:`, `Assisted-by:`, and `Generated by/with` trailers or signatures referencing AI tools (`Claude`, `Gemini`, `Antigravity`, `Copilot`, `OpenAI`, `ChatGPT`, `GPT-4/5`, `Cursor`, `Windsurf`, `Codex`, `Aider`, `Devin`, `Codeium`, `Tabnine`, `noreply@anthropic.com`, etc.) while allowing legitimate human `Co-authored-by:` trailers.

4. **Repository Search-Scope Guard (`craftsman.scope` in `pre-tool`) & Session-Bound Gate Ownership**
   - During an active Craftsman session owned by the current agent session (or whenever `hook.agent` is `craftsman`, `craftsman-reviewer`, or `craftsman-verifier`), `on_pre_tool()` runs `scope.evaluate_scope()` after the git guard across shell, file-read, and search/glob tools (`CLAUDE_HOOK_MATCHERS["PreToolUse"] = "Bash|Read|Grep|Glob|LS"`).
   - Blocks recursive search and directory-listing commands (`find`, `rg`, `grep -r`, `fd`, `ls`, `tree`, `du`, `Glob`, `Grep`, `LS`, `find_by_name`, `grep_search`, `list_dir`, and inline `python -c` / `node -e` directory walks) targeting paths outside the active git work tree, blocks reading sibling repositories or prior agent session state (`~/.claude/projects`, `~/.copilot/session-state`, `~/.gemini/antigravity/brain`), and blocks searching the file system for `SKILL.md`, `lenses.md`, `packet-*.md`, or `ledger.json` (which are served deterministically by `craftsman skill show` and `craftsman review show --packet-path`).
   - Allows single-file reads inside installed dependency/stdlib directories (`site-packages`, `node_modules`, `.venv`, `/usr/lib`, `/Library/Frameworks`, etc.), `$TMPDIR`, the bundled Craftsman asset root, and extra roots configured via `git config --get-all craftsman.scopeAllow` (with `git config craftsman.scope enforce|warn|off`).
   - Each active gate records `owner = {"platform": ..., "session_id": ...}` in `.git/craftsman/craftsman_state.json`; hooks from other platforms or sessions in the same work tree fail open without mutating `forced_continuations`, `hooks_seen`, or ledger agent events.

---

## 4. Two-Phase Code Review Protocol, Lens Matrix & Verdict Lifecycle

![Two-phase review and the life of a finding](diagrams/review-protocol-and-verdicts.png)

### Why Two Phases?

A single-pass reviewer tends to filter out uncertain candidates before verifying them, so defects that only show across files, in unchanged callers or in tests are the first to be dropped. Craftsman separates the cognitive modes into two distinct phases executed by separate fresh-context subagents:

1. **Phase 1 — High-Recall Candidate Collection (`craftsman-reviewer`)**
   - **Read Before Judging**: Reads `packet-N.md`, project convention files (`AGENTS.md`, `CLAUDE.md`, etc.), full enclosing scopes, and caller call-sites.
   - **Mental Execution with Concrete Values**: Walks each lens probe in [`skills/code-review/lenses.md`](../skills/code-review/lenses.md) by substituting concrete boundary inputs (`0`, `-1`, `None`, `""`, `[]`, `NaN`, duplicate keys, concurrent calls, subprocess non-zero exits) into the actual code lines.
   - **Generous Collection**: Records every candidate where a concrete input reaches a line and produces a wrong outcome via `craftsman review add --lens <L> --file <path> --line <N> --severity <blocker|major|minor|nit> --title "..." --scenario "<trigger -> line -> wrong outcome>"` (or in bulk via `craftsman review add --from-json - <<'EOF'`).
   - **Line Verification**: `review add` echoes the cited source line (`L<line>: <text[:120]>`) via `review._cited_line()`; if the line number points to a neighbor line, the reviewer corrects it immediately via `craftsman review move F# --line <exact_line>`.
   - **Granular Coverage Notes**: Calls `craftsman review cover --lens <L> --hunks H1,H2 --note "..."` (or in bulk via `craftsman review cover --from-json - <<'EOF'`, `1..4` hunks per entry, `>= 30` chars) naming each `H#` and what was checked.
   - **Wave 2 Cross-Hunk Sweep**: In `--fix` and `/craftsman` mode, after all Wave 1 domain lenses finish, a separate fresh-context `craftsman-reviewer` runs the `sweep` lens to check producer/consumer consistency across all hunks in the round. In standalone review-only mode (without `--fix`), two `craftsman-reviewer` subagents split the domain lenses in parallel (`{intent, contracts, ripple, tests, craft}` and `{failure, state, trust, platform, model}`), and the single `craftsman-verifier` subagent runs `sweep` and root-cause deduplication (`review merge`, only when one fix removes every consequence) in Step 0 before recording verdicts (via `review verdict --from-json - <<'EOF'`), finishing with `craftsman review check --review-only` and `craftsman review report --review-only`.

2. **Phase 2 — Adversarial Challenge & Reproduction (`craftsman-verifier`)**
   - Starts in a fresh context with a skeptic mandate: **try to disprove every candidate finding `F#`**.
   - Runs a strict four-step disproof checklist against the live repository:
     1. **Guard check**: Does an earlier check in the function, caller, type signature, or schema validator prevent the trigger input from reaching `path:line`?
     2. **Reachability check**: Can any real caller or entrypoint construct the state described in `--scenario`?
     3. **Intent check**: Is the behavior explicitly requested by the task, documented in docstrings/comments, or pinned by an existing test?
     4. **Semantics check**: Did the reviewer misread the language construct, operator precedence, or shadowed variable?
   - **Real Execution Reproduction**: For every candidate that survives static disproof, the verifier writes a minimal standalone script in `/tmp` importing the repository's code and runs it through `craftsman run --kind repro -- python3 /tmp/repro_f1.py` so the run (`R#`) is permanently recorded in `ledger.json`. The verifier never runs `git stash`, `git checkout`, or edits repository files.

---

### Verdict & Resolution State Machine

Every finding `F#` transitions through a strictly validated state machine enforced by `review._cmd_verdict()`, `review._cmd_resolve()`, and `ledger.evaluate()`:

| Transition | Command | Evidence Enforced by CLI / Ledger | Gate Outcome |
| :--- | :--- | :--- | :--- |
| **Created** (`verdict = None`) | `craftsman review add ...` (`review merge` groups unjudged duplicates by root cause before verification) | Must reference a valid round lens; echoes cited source line | **BLOCKED** (`[no-verdict]`) |
| **Verdict: `proven`** | `craftsman review verdict F# --as proven --run R# --evidence "..."` | Requires `--run R#` pointing to a real non-trivial `Run` in `ledger.json` and `--evidence` (`>= 20` chars) | **BLOCKED** (`[unresolved]`) until fixed or accepted |
| **Verdict: `probable`** | `craftsman review verdict F# --as probable --evidence "static: <file:line chain>"` or `"no-repro: <why>; <chain>"` | Used for static `file:line` verification without a run (`static:`, must include a `file:line` reference) or when deterministic local verification is impossible (`no-repro: <why>;`); `--evidence` **must** start with `static:` or `no-repro:` (`>= 20` chars after the prefix) | **BLOCKED** (`[unresolved]`) until fixed or accepted |
| **Verdict: `question`** | `craftsman review verdict F# --as question --evidence "intent: <source>; <file:line>"` | **Forbidden** when `severity == "blocker"`. Requires `--evidence` starting with `intent:` (`>= 20` chars after `intent:`) containing both the source of intent (PR task/description, commit, reviewer comment, or docs) and a `file:line` reference in the code | **PASS** (listed under *Question* in final report with an audit warning) |
| **Verdict: `dismissed`** | `craftsman review verdict F# --as dismissed [--run R#] --evidence "..."` | Requires either `--run R#` (deterministically proving the code handles the scenario) **or** a concrete `file:line` reference in `--evidence`; automatically clears any stale `resolution` | **PASS** (terminal; listed briefly under *Dismissed* in final report) |
| **Resolution: `fixed`** | `craftsman review resolve F# --as fixed --note "..."` | Requires `verdict in ("proven", "probable")`, `--note` (`>= 20` chars, `MIN_RESOLUTION_NOTE_CHARS = 20`), **and** a changed worktree (`snap.fingerprint != finding.fingerprint`). If the fix is later reverted, `evaluate()` re-flags `[unresolved]`! | **PASS** (triggers delta Round `N+1` for the modified hunks) |
| **Resolution: `accepted`** | `craftsman review resolve F# --as accepted --note "..."` | **Forbidden** when `severity == "blocker"`. For `major` findings, the agent must pause (`gate --await-user`) and get human approval first. Surfaces a prominent warning in `craftsman review report`. | **PASS** (with audit warning) |

---

## 5. Module Map & Source Cross-Reference

| Module | Responsibility | Key Entrypoints & Types |
| :--- | :--- | :--- |
| [`craftsman/cli.py`](../craftsman/cli.py) | Unified CLI dispatcher (`craftsman <subcommand>`) | `main()`, `build_parser()` |
| [`craftsman/gate.py`](../craftsman/gate.py) | Session state machine (`.git/craftsman/craftsman_state.json`), 3-Gear auto-promotion, and stop-hook evaluation | `resolve_effective_tier()`, `evaluate_task()`, `evaluate_stop()`, `run_cli()` |
| [`craftsman/evidence.py`](../craftsman/evidence.py) | Bridge between session state machine and evidence ledger (archival, packet paths, and evaluation) | `evaluate_evidence()`, `effective_tier()`, `packet_path()`, `archive_ledger()`, `archive_ledger_if_rebased()`, `ledger_overview()` |
| [`craftsman/ledger.py`](../craftsman/ledger.py) | Evidence ledger schema v1 (`.git/craftsman/ledger.json`), OS file locking, atomic writes, delta carry-over, and 12-check gate evaluator | `Ledger`, `Round`, `Coverage`, `Finding`, `Run`, `AgentEvent`, `mutate()`, `_carry_over()`, `evaluate()` |
| [`craftsman/diffscan.py`](../craftsman/diffscan.py) | Git diff + untracked file scanner, `.craftsmanignore` filtering, `WorktreeIndex`, and two-level SHA-256 fingerprinting | `snapshot()`, `DiffSnapshot`, `ChangedFile`, `Hunk`, `WorktreeIndex`, `detect_signals()` |
| [`craftsman/packet.py`](../craftsman/packet.py) | Review packet context builder and Markdown renderer (`packet-N.md`) | `PacketContext`, `collect_context()`, `render_packet()` |
| [`craftsman/lenses.py`](../craftsman/lenses.py) | Catalog of 11 review lenses, tier/signal selection rules, and docs-only fast path | `LENSES`, `Lens`, `select_lenses()` |
| [`craftsman/review.py`](../craftsman/review.py) | CLI handlers for the review lifecycle (`prepare`, `cover`, `add`, `move`, `verdict`, `resolve`, `waive-run`, `show`, `check`, `report`, `reset`) | `run_cli()`, `open_workspace()` |
| [`craftsman/runner.py`](../craftsman/runner.py) | Live-streaming command execution wrapper (`craftsman run`) with before/after diff fingerprinting | `run_cli()`, `execute_and_record()` |
| [`craftsman/hooks.py`](../craftsman/hooks.py) | Multi-platform lifecycle hook adapter (`prompt`, `stop`, `subagent-start`, `subagent-stop`, `pre-tool`) with fail-open wrapper | `run_cli()`, `on_prompt()`, `on_stop()`, `on_subagent_start()`, `on_subagent_stop()`, `on_pre_tool()` |
| [`craftsman/guard.py`](../craftsman/guard.py) | Shell command parser, wrapper unwrapper, protected-branch push detector, and AI attribution blocker | `split_shell_segments()`, `analyze_git_push()`, `analyze_gh_pr_merge()`, `has_ai_attribution()`, `evaluate_command_guard()`, `run_native_pre_push_hook()`, `run_native_commit_msg_hook()` |
| [`craftsman/export.py`](../craftsman/export.py) | Multi-platform skill/agent/hook compiler and installation health checker (`craftsman doctor`) | `export_to_vscode_user()`, `export_to_copilot_skills()`, `export_to_claude_user()`, `export_to_copilot_workspace()`, `export_to_agents_workspace()`, `run_doctor()` |
| [`craftsman/install.py`](../craftsman/install.py) | Cross-platform installer and uninstaller with atomic `.craftsman-install.json` manifest tracking (`craftsman install` / `craftsman uninstall`) | `run_install_cli()`, `run_uninstall_cli()` |
