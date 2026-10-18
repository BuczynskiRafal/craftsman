---
name: audit
description: >-
  Comprehensive engineering audit orchestrator. Runs selected or all specialized
  audits: errors (/audit-errors), tests (/audit-tests), types (/audit-types),
  comments (/audit-comments), security (/audit-security), quality (code-quality),
  and perf (extreme-optimizer / leetcode-style). Use when /audit [aspects]
  [target] is invoked.
---

# Comprehensive Engineering Audit (`/audit`)

You are the orchestrator for specialized engineering audits. A single `/audit` invocation runs targeted deep audits across the current diff, a Pull Request, or a specified module.

Include clickable `[file.ext:L10-L20](file:///absolute/path/to/file.ext#L10-L20)` links for all findings.

---

## 1. Available Audit Aspects (`/audit [aspects...] [<target>]`)

Parse the arguments passed to `/audit`:

| Aspect (argument) | Corresponding Skill | When Automatically Selected (no argument or `all`) |
| :--- | :--- | :--- |
| **`errors`** | [`audit-errors`] | When the diff/module contains exception handling (`try/except`), I/O, parsing, or fallbacks |
| **`tests`** | [`audit-tests`] | When business logic or test files (`tests/`, `test_*.py`) are modified |
| **`types`** | [`audit-types`] | When classes, `@dataclass`, Pydantic models, `Enum`, or API contracts are added/modified |
| **`comments`** | [`audit-comments`] | When docstrings, comments, or signatures of documented functions are added/modified |
| **`security`** | [`audit-security`] | When code touches file paths, subprocesses, SQL/expressions, network, deserialization, or auth |
| **`quality`** | [`code-quality`] | On demand (`/audit quality`) or on large classes/modules (DRY, SOLID, orthogonality) |
| **`perf`** | [`leetcode-style-code-reviewer`] | On demand (`/audit perf`) or on numerical loops / NumPy / Pandas / hot-path code |
| **`all`** *(default)* | All matching aspects above | Automatically selects aspects based on diff contents (`git diff --name-only` + hunk contents) |

---

## 2. Adaptive Execution Process

1. **Determine Scope & Matching Aspects:**
   - Inspect modified files (`git diff @{upstream}...HEAD` + `git diff HEAD`, or the specified file/directory/PR).
   - If the user specified concrete aspects (e.g., `/audit errors types` or `/audit security`), read the corresponding `SKILL.md` files from the table above and execute those exact audits.
   - If the user invoked `/audit` or `/audit all`, inspect the diff contents and run all applicable audit aspects.
2. **Execution Mode (Inline vs. Subagents):**
   - **Small/Medium Scope (1–2 aspects or diff < ~200 lines):** Read the selected skill instructions and execute the audits sequentially in the current context.
   - **Large Scope (3+ aspects on a large diff/module or with `--parallel`):** Launch parallel `research` subagents via `invoke_subagent` (one per selected audit aspect), passing each subagent the full guidelines of its assigned skill.
3. **Consolidated Final Report:**
   Combine findings from all executed aspects into a single prioritized report (`CRITICAL` → `HIGH` → `MEDIUM`), with clickable `[file.ext:L10-L20](file:///...)` links and concrete code fixes. If `--fix` was passed, apply unambiguous fixes directly.
