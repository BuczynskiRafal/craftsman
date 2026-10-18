---
name: feature-dev
description: >-
  Structured 7-phase process for designing and implementing new features with
  deep architectural understanding. Use when /feature-dev is invoked or when
  building non-trivial new functionality.
---

# Guided Feature Development (`/feature-dev`)

You guide the user through a disciplined, 7-phase feature development workflow. Instead of guessing requirements and jumping straight into code, you first explore existing patterns in the repository, resolve ambiguities early, present architectural trade-offs, and implement the chosen design with full verification.

Include clickable `file://` Markdown links for all referenced files and symbols.

---

## Core Principles

- **Understand before acting:** Always inspect existing project conventions and abstractions first.
- **Ask clarifying questions early:** Surface all underspecified requirements and edge cases **after** exploring the codebase (Phase 2), but **before** designing the architecture (Phase 4). Use `ask_question` for architectural and design choices.
- **Adaptive exploration:** For focused features, explore directly; for multi-module features, launch parallel `research` subagents (`invoke_subagent`).

---

## The 7 Phases of `/feature-dev`

### Phase 1: Discovery
1. If the user's goal is initially ambiguous, clarify what problem is being solved, the target behavior, and any constraints.
2. Summarize the objective in 2–3 sentences and proceed immediately to codebase exploration.

### Phase 2: Codebase Exploration (`code-explorer`)
**Goal:** Understand existing code, conventions, and extension points at both high and low levels.
1. Search the repository (directly or via 2–3 parallel `research` subagents using `invoke_subagent`) for:
   - analogous features already implemented in the project,
   - architectural layers, control flow, and domain models,
   - error-handling conventions, UI/CLI patterns, and testing utilities.
2. Read the key identified files (`view_file`).
3. Present a concise summary of discovered patterns and the primary files (`[file.py](file:///...)`) where the new feature will integrate.

### Phase 3: Clarifying Questions
**CRITICAL PHASE — DO NOT SKIP WHEN AMBIGUITIES EXIST.**
1. Based on Phase 2 findings, identify any underspecified aspects:
   - edge cases and boundary conditions,
   - error handling and invalid input behavior,
   - integration points and backward compatibility,
   - scope boundaries (*what is in scope now vs. out of scope*).
2. Ask the user via `ask_question` (putting your `(Recommended)` option first) and **wait for their response before moving to Phase 4**.

### Phase 4: Architecture Design (`code-architect`)
**Goal:** Design and compare implementation approaches.
1. Formulate **2–3 architectural options**:
   - **Option A (Minimal changes):** smallest footprint, maximum reuse of existing code,
   - **Option B (Clean architecture):** cleanest separation of concerns, extensibility, and testability,
   - **Option C (Pragmatic balance):** sweet spot between simplicity and structural cleanliness.
2. Present trade-offs, exact files to create/modify, and **your explicit recommendation with rationale**.
3. Confirm the chosen architecture with the user.

### Phase 5: Implementation
**DO NOT START WITHOUT ARCHITECTURAL APPROVAL IN PHASE 4.**
1. Implement the feature strictly following the chosen design and project conventions (`GEMINI.md` / `AGENTS.md` / `CLAUDE.md`).
2. Add tests covering both the happy path and the edge cases established in Phase 3.

### Phase 6: Quality Review (`/simplify` + `/code-review` + `/verify`)
1. Review the newly written code for:
   - simplicity, DRY, and proper abstraction altitude,
   - functional correctness and zero silent failures,
   - automated test execution and end-to-end runtime verification (`/verify`).
2. Fix any defects immediately.

### Phase 7: Summary
Conclude with a concise report:
- what was built,
- key architectural decisions,
- modified and newly created files with clickable `file://` links,
- test and runtime verification results.
