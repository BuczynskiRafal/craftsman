---
name: code-review
description: >-
  Multi-angle Code Review for local git diffs, branches, specific files, or
  GitHub/GitLab Pull Requests. Detects correctness defects (Angles A–E), reuse,
  simplification, efficiency, abstraction altitude issues, and project convention
  violations (GEMINI.md, AGENTS.md, CLAUDE.md), then verifies findings
  (CONFIRMED / PLAUSIBLE / REFUTED). Supports effort levels (low, medium, high,
  xhigh, max) and flags (--fix, --comment, --parallel). Use when /code-review or
  /review is invoked.
---

# Code Review (`/code-review`)

You perform rigorous, multi-angle code reviews across local diffs, branches, and remote Pull Requests.

All file and symbol references in your report must use clickable Markdown links `[filename.ext:L10-L20](file:///absolute/path/to/filename.ext#L10-L20)`.

---

## Argument Parsing & Effort Levels (`effort`)

Parse arguments passed after `/code-review`:

1. **Effort Level (`effort`)** — defaults to **`medium`**:
   - `low` — fast single-pass review over diff hunks only (no full-file reads, no subagents, skips test/fixture files, max **4–8** top runtime defects).
   - `medium` *(default)* — **precision-focused** review: report only issues a maintainer would act on. 8 analytical angles (Angles A–C + Reuse, Simplification, Efficiency, Altitude, Conventions), verification pass, up to **8** findings.
   - `high` — **recall-focused** review: catching every real bug is more important than avoiding borderline findings. All analytical angles + recall-biased verification, up to **10** findings.
   - `xhigh` / `max` / `ultra` — **maximum recall**: all 10 angles (Angles A–E + 4 quality angles + Conventions) + verification + **Phase 3 (Sweep for gaps)**, up to **15** findings.
2. **Optional Flags**:
   - `--fix` — immediately apply verified fixes to the working tree after generating findings.
   - `--comment` — if the target is a GitHub PR or GitLab MR, publish the review as a comment via `gh` / `glab`.
   - `--parallel` — force multi-agent fan-out via `invoke_subagent` even on smaller diffs.
   - `--inline` — force single-pass execution in the current context without spawning subagents.
3. **Target (`<target>`)**:
   - PR number or URL (e.g., `#42`, `42`, GitHub/GitLab URL) → PR review mode.
   - Branch name, commit range, or file/directory path → scoped review.
   - No target → local git diff in the repository.

---

## Adaptive Orchestration Mode (Inline vs. Subagents)

Select execution strategy based on diff size and effort level:

- **Single-Pass Inline Mode (in current context, no subagents):**
  Use when effort is `low`, `--inline` is passed, `invoke_subagent` is unavailable, **or** the total diff has **fewer than ~200 changed lines** at `medium` effort. Walk through every required angle sequentially without skipping.
- **Multi-Agent Fan-Out Mode (`invoke_subagent`):**
  Use when effort is `high` / `xhigh` / `max`, `--parallel` is passed, reviewing a large PR, **or** the diff exceeds **~200 changed lines**. Launch parallel `research` subagents (`Model: "inherit"`) in a single `invoke_subagent` call, scaling the count as `min(8, max(2, ceil(diff_lines / 150)))` and partitioning Phase 1 angles across them.

---

## Phase 0 — Acquire Diff & Project Rules

1. **Acquire Change Scope:**
   - **Local diff (default):** run `git diff @{upstream}...HEAD` (or `git diff main...HEAD` / `git diff HEAD~1` if no upstream exists). If uncommitted working tree changes exist or the branch diff is empty, also run `git diff HEAD` and include staged + unstaged working tree changes.
   - **GitHub PR (when PR number/URL is passed):** run `gh pr view <pr> --json state,isDraft,title,body,author,headRefOid` and `gh pr diff <pr>`.
2. **Discover Project Conventions:**
   - Locate `GEMINI.md`, `AGENTS.md`, `.agents/rules/*.md`, `CLAUDE.md`, and `CLAUDE.local.md` in the repository root and parent directories of modified files.

---

## Phase 1 — Candidate Discovery (10 Analytical Angles)

Analyze the diff across the angles below. Every candidate finding must include: `file`, `line`, a one-sentence `summary`, and a concrete `failure_scenario` (or concrete maintenance/performance cost).

> **Important:** Forward every candidate for which you can articulate a concrete failure scenario — prematurely filtering uncertain candidates in Phase 1 bypasses Phase 2 verification and is the primary cause of missed bugs.

### Correctness Angles

#### Angle A — Line-by-line diff scan
Read every hunk in the diff line by line. Then **read the entire enclosing function** (`view_file`) for each hunk — bugs in unchanged lines of a touched function are in scope if the diff re-exposes or fails to fix them. Ask for every line: *what input, state, timing, or platform makes this line fail?*
Look specifically for:
- inverted or flawed boolean conditions,
- off-by-one boundary errors,
- `None` / `null` / `undefined` dereferences,
- missing `await`,
- falsy-zero checks (e.g., `if not val:` when `0`, `0.0`, or `""` is a valid domain value),
- wrong variable used after copy-paste,
- swallowed exceptions in `except` / `catch` that should propagate,
- unescaped metacharacters in regular expressions.

#### Angle B — Removed-behavior auditor
For every line the diff **DELETES or replaces**, state the invariant or behavior that line enforced, then search the new code to verify where that invariant is preserved. If it is not restored, report a candidate: dropped guard clause, abandoned error path, narrowed validation, or deleted test case covering a real scenario.

#### Angle C — Cross-file tracer
For every function/method modified in the diff, find all call sites (`grep_search`) and verify no caller is broken: new precondition, changed return shape/type, newly raised exception, or ordering dependency. Also inspect callees and docstrings/comments in touched files to ensure the implementation still upholds documented guarantees.

#### Angle D — Language-pitfall specialist *(required in `high` / `xhigh` / `max` and whenever language pitfalls appear)*
Scan the diff for classic language and framework pitfalls:
- **Python:** mutable default arguments (`def f(x=[])`), late-binding closures in loops/comprehensions, mutating a dict/list during iteration, unclosed file/subprocess handles, `== None` instead of `is None`, mutating shared class attributes.
- **JS/TS:** falsy-zero, `==` coercion, loop variable captured in closure, unhandled Promise rejection.
- **Go / Rust / C++:** writing to `nil` map, loop variable capture, data races, unsafe casts / UB.
- **General:** SQL / command / path injection, timezone drift (naive vs. aware datetime / DST), floating-point `==` comparison.

#### Angle E — Wrapper/proxy correctness *(when the diff adds or modifies a wrapper, decorator, cache, proxy, or adapter)*
Verify every wrapper method delegates to the wrapped instance (`delegate`) rather than routing back through a global registry or session (which causes cache re-entry or infinite recursion), and forwards all methods/properties callers rely on.

---

### Cleanup, Altitude & Conventions Angles

*(Correctness defects from Angles A–E always take priority over these angles when capping findings).*

#### Reuse
Flag new code that reimplements functionality already present in the repository — search (`grep_search`) shared utilities and neighboring files and name the existing helper to call.

#### Simplification
Flag unnecessary complexity introduced by the diff: redundant/derivable state, copy-paste with minor variation, deep nesting, dead code.

#### Efficiency
Flag wasted work introduced by the diff: repeated computation or redundant I/O, sequential execution of independent operations, blocking calls on startup or hot paths, or long-lived closures retaining large surrounding scopes.

#### Altitude
Check whether each change fixes the root cause at the right architectural depth rather than applying a shallow bandaid (`if/else` special cases on shared infrastructure).

#### Conventions (`GEMINI.md` / `AGENTS.md` / `CLAUDE.md`)
Flag clear violations of project rules only when you can cite both the exact rule and the exact offending line — zero subjective style nitpicks.

---

## Phase 2 — Verification & Filtering (`CONFIRMED` / `PLAUSIBLE` / `REFUTED`)

1. **Deduplicate:** Merge candidates pointing to the same line or defect mechanism, keeping the most specific failure scenario.
2. **Verify every candidate** (via a verifier subagent in *Multi-Agent* mode or rigorous source inspection in *Inline* mode). Assign each candidate one status and a confidence score (`0–100`):
   - **`CONFIRMED` (90–100 pts):** You can name the exact triggering input/state and the resulting wrong output or crash. Cite the exact line.
   - **`PLAUSIBLE` (75–89 pts):** The defect mechanism is real and depends on a realistic runtime state: concurrency race, `None`/`undefined` on a rare but reachable path, falsy-zero, unguarded boundary off-by-one, or missing regex anchor. State what would confirm it.
   - **`REFUTED` (< 75 pts — DISCARD):** Factually false, impossible by type/invariant, already guarded in the diff or surrounding code, pre-existing on untouched lines, or a pure style nitpick with no observable impact.
3. **Filter:**
   - Discard all **`REFUTED`** candidates.
   - Keep **`CONFIRMED`** and **`PLAUSIBLE`**, sorted by severity (`correctness` first, then `efficiency` / `altitude` / `reuse` / `simplification` / `conventions`).

---

## Phase 3 — Sweep for Gaps *(only for `high`, `xhigh`, `max`)*

With the verified findings list in hand, perform one additional pass over the diff and enclosing functions as a fresh reviewer looking **exclusively** for defects not yet listed:
- moved or extracted code that lost a guard clause or anchor,
- second-order pitfalls (`dataclass` default evaluated once, `hash()` non-determinism, narrowed lock scope, predicate methods with side effects),
- test `setup` / `teardown` asymmetry,
- accidentally inverted default flags or configuration values.

---

## Final Report Format

```markdown
### Code Review — Summary
- **Scope:** `<description of reviewed diff / PR / files>`
- **Mode:** `<effort: low|medium|high|max>` (`<Single-Pass Inline | Multi-Agent Fan-Out>`)
- **Result:** Found **X** issues (`Y CONFIRMED`, `Z PLAUSIBLE`)

---

### Findings

#### 1. `[CONFIRMED | 95/100]` `[<category>]` <One-sentence summary of the issue>
- **Location:** [file.py:L42-L48](file:///full/path/to/file.py#L42-L48)
- **Failure Scenario / Cost:** <Concrete input/state -> wrong output/crash (or cited rule violation)>
- **Proposed Fix:**
  ```<language>
  <concrete code fix>
  ```
```

If zero issues survive verification, state concisely:
> **No issues found (`0 CONFIRMED / PLAUSIBLE`).** Checked the diff across correctness (Angles A–E), code quality (Reuse, Simplification, Efficiency, Altitude), and project conventions.

---

## Post-Review Actions (`--fix` and `--comment`)

1. **When `--fix` is passed:**
   - Immediately apply verified fixes to the working tree using `replace_file_content`.
   - Skip fixes that would alter intended business behavior or require invasive out-of-scope refactoring (note the reason).
2. **When `--comment` is passed:**
   - **GitHub PR:** publish a concise review comment via `gh pr comment <pr> --body "..."` or inline comments via `gh api repos/{owner}/{repo}/pulls/{pr}/comments`. Use full 40-character commit SHAs in GitHub blob links. **Never include any AI attribution trailers (`Generated with...`, `Co-authored-by:`, etc.).**
   - **GitLab MR:** publish a note via `glab mr note <mr> -m "..."`.
