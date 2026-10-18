---
name: simplify
description: >-
  Clean up and simplify recently modified code without changing its behavior
  (Preserve Functionality). Reviews the diff across 4 engineering angles (Reuse,
  Simplification, Efficiency, Altitude) and applies fixes directly to the files.
  Does not hunt for correctness bugs (use /code-review for that). Use when
  /simplify is invoked or when the user asks to clean up recently written code.
---

# Simplify (`/simplify`)

You improve the clarity, consistency, and efficiency of recently modified code **without changing its behavior**. Analyze the diff across **Reuse**, **Simplification**, **Efficiency**, and **Abstraction Altitude**, and **apply the improvements directly to the files**. Do not use this skill to hunt for correctness bugs — that is the job of `/code-review`.

Include clickable Markdown links `[file.ext:L10-L20](file:///absolute/path/to/file.ext#L10-L20)` for all file and symbol references.

---

## Golden Rules (`/simplify`)

1. **Preserve Functionality (100% Behavior Preservation):** Never change *what* the code does — only *how* it expresses it. All observable behaviors, outputs, and API contracts must remain identical.
2. **Clarity over Brevity:** Explicit, readable code is always better than overly dense "clever" one-liners.
   - **IMPORTANT:** Avoid nested ternary operators — prefer clear `if/else` chains, `match/switch` statements, or small helper functions with early returns.
3. **Maintain Balance (Avoid Over-Simplification):**
   - Do not merge unrelated responsibilities into a single function or class.
   - Do not strip helpful abstractions that improve module organization and readability.
   - Do not optimize for "fewer lines" at the expense of debuggability and extensibility.
4. **Apply Project Standards:** Respect all rules defined in `GEMINI.md`, `AGENTS.md`, `.agents/rules/*.md`, and `CLAUDE.md` (naming conventions, typing, import sorting, error handling).

---

## Phase 0 — Acquire the Diff (Scope)

1. Run `git diff @{upstream}...HEAD` (or `git diff main...HEAD` / `git diff HEAD~1` if no upstream exists) to inspect committed changes on the current branch.
2. If uncommitted working tree changes exist or the branch diff is empty, also run `git diff HEAD` and include working tree changes in scope — `/simplify` is most frequently run right before committing.
3. If the user passed a specific file/directory path, branch name, or PR, scope your work strictly to that target.

---

## Phase 1 — 4-Angle Code Cleanup Analysis

### Adaptive Execution Mode
- **Single-Pass Inline (default for small/medium diffs < ~200 lines):** Walk through all **4 angles** below sequentially in the current context — do not skip any angle.
- **4-Agent Parallel Fan-Out (for large diffs ≥ ~200 lines or with `--parallel`):** Launch **4 parallel `research` subagents** via `invoke_subagent` in a single call, assigning one of the four angles below to each subagent.

For each finding, record: `file`, `line`, a one-sentence `summary`, and the **concrete cost** (what is duplicated, wasted, or harder to maintain).

### 1. Reuse (Existing Code Reuse)
Identify newly written code that reimplements functionality already present in the codebase — search (`grep_search`) shared/utility modules (`utils`, `helpers`, `common`, `shared`) and neighboring files, and cite the existing helper or class that should be called instead.

### 2. Simplification (Structural Clarity)
Identify unnecessary complexity introduced by the diff:
- redundant or derivable state,
- copy-paste blocks with minor variation,
- deep nesting (flatten via guard clauses / early returns),
- dead code, unused variables, or unused imports left behind,
- nested ternary expressions,
- redundant comments restating obvious code (while strictly preserving meaningful docstrings and "why" comments per documentation integrity).

### 3. Efficiency (Zero Waste)
Identify wasted work introduced by the diff:
- repeated computation or redundant I/O / queries,
- independent operations executed sequentially,
- blocking operations added to startup or hot paths,
- long-lived objects constructed from closures that retain large surrounding scopes in memory (prefer a class, struct, or `dataclass` copying only the required fields).

### 4. Altitude (Abstraction Depth)
Check whether each change fixes the **root cause** at the appropriate architectural depth rather than patching a symptom with a shallow bandaid. Adding special-case `if/else` branches onto shared infrastructure is a sign the change is too shallow — prefer a simpler, more general fix in the underlying mechanism.

---

## Phase 2 — Apply the Fixes

1. **Deduplicate:** Merge findings from Phase 1 that target the same lines or mechanism.
2. **Direct Code Edits:** Apply each improvement directly to the files using `replace_file_content`.
3. **Safe Skipping:** Skip any finding that would:
   - alter intended runtime behavior,
   - require invasive changes far outside the diff scope,
   - reduce readability.
4. **Final Summary:**
   Conclude with a concise summary:
   - Execution mode (*Single-Pass Inline* or *4-Agent Parallel Fan-Out*),
   - **What Was Simplified:** bulleted list of applied changes with `[file.ext:L10-L20](file:///...)` links, angle (`Reuse` / `Simplification` / `Efficiency` / `Altitude`), and a one-sentence benefit,
   - **Skipped Suggestions:** brief list of skipped items or confirmation that the code was already clean.
