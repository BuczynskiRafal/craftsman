---
name: batch
description: >-
  Plan and execute parallel mass refactorings or migrations across many files or
  modules. Decomposes large migrations into independent Work Units, defines a
  verification recipe, spawns parallel subagents after plan approval, and tracks
  progress in a status table. Use when /batch is invoked.
---

# Batch Parallel Refactor & Migration (`/batch`)

You plan and execute large-scale, mechanical or structural changes across many files in a repository (e.g., API migrations, mass typing upgrades, error-handling standardization, signature updates across dozens of modules) by decomposing them into independent, parallel **Work Units**.

Include clickable `file://` Markdown links for all referenced files.

---

## Phase 1 — Research & Plan (Decomposition)

1. **Survey the Scope:**
   Search the repository (`grep_search` / `find_by_name`, or parallel `research` subagents on large codebases) to locate all files and call sites affected by the user's request.
2. **Decompose into Disjoint Work Units:**
   - Group files into logical, **strictly disjoint** batches (so two subagents never edit the same file concurrently!).
   - For each Work Unit, specify: a short title, the exact list of files, and a one-sentence description of the change.
3. **Define the Verification Recipe:**
   - Specify a fast, concrete command that verifies a single Work Unit (e.g., `pytest tests/unit/test_xyz.py`, `ruff check <files>`, `mypy <files>`).
4. **Present the Plan for Approval:**
   Output a clear plan containing:
   - total affected files and occurrences,
   - numbered table of **Work Units**,
   - verification recipe,
   and ask the user to confirm execution.

---

## Phase 2 — Spawn Workers (Parallel Execution)

Once the user approves the plan:
- Launch parallel subagents (`invoke_subagent`, `TypeName: "self"`, `Model: "inherit"`).
  - Assign disjoint file sets to each subagent in the current workspace (`Workspace: "inherit"`) or use isolated worktrees (`Workspace: "share"`) when separate branches are desired.
- Give each subagent a self-contained prompt containing:
  1. The overarching migration goal,
  2. The exact list of assigned files (**strictly forbid editing files outside its assigned list!**),
  3. Project conventions to follow,
  4. The verification command it must run before finishing.

---

## Phase 3 — Track Progress & Final Verification

1. As subagents complete, present the consolidated status table:

```markdown
| # | Work Unit | Files | Status | Verification |
| :-: | :--- | :--- | :---: | :--- |
| 1 | `<unit 1 title>` | `<files>` | ✅ Done / ❌ Failed | `<test result>` |
| 2 | `<unit 2 title>` | `<files>` | ✅ Done / ❌ Failed | `<test result>` |
```

2. Run a final repository-wide verification (`git status`, `git diff --stat`, full test suite / linter) to confirm cross-unit consistency.
