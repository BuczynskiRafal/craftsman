---
name: high-level-commit-message
description: Draft a concise, high-level Git commit message in Conventional Commits format and American English from the current repository changes. Use when Codex should inspect `git status`, `git diff`, or `git diff --cached` and summarize what is being introduced to the codebase without listing low-level file-by-file edits, hunks, or implementation details.
---

# High Level Commit Message

## Overview

Draft one short commit message in **Conventional Commits** format that explains the change at repository level, not diff level.

## Workflow

1. Inspect repository state with `git status --short`.
2. Prefer `git diff --cached` when staged changes exist; otherwise inspect `git diff`.
3. Infer the main outcome: feature, fix, refactor, tests, docs, config, data, or mixed change.
4. Select the appropriate Conventional Commits **type** (see Type Inference below).
5. Optionally add a **scope** in parentheses when the change is clearly scoped to one module or area.
6. Compress related edits into one broader theme.
7. Draft the final message in American English.
8. Always generate a matching **branch name** (see Branch Name Format below).

## Conventional Commits Format

Always use the following format:

```
<type>(<optional-scope>): <subject>

<optional body>
```

- **type** — required, lowercase (see Type Inference).
- **scope** — optional, lowercase, in parentheses. Use when the change clearly targets a single module, package, or area.
- **subject** — required, imperative mood, lowercase start, no trailing period.
- **body** — optional, separated by a blank line. Use bullet points (`-`) for multi-theme changes.
- **BREAKING CHANGE** — if the change is breaking, add `!` after the scope/type: `feat(model)!: ...` or note `BREAKING CHANGE:` in the body.

### Type Inference

Choose the type based on the dominant intent of the diff:

| Type         | When to use                                                        |
| ------------ | ------------------------------------------------------------------ |
| `feat`       | New capability, behavior, or user-facing feature                   |
| `fix`        | Bug fix, error correction                                          |
| `refactor`   | Code restructuring without changing behavior                       |
| `perf`       | Performance improvement                                            |
| `test`       | Adding or updating tests only                                      |
| `docs`       | Documentation-only changes                                         |
| `build`      | Build system, dependencies, CI pipeline                            |
| `chore`      | Maintenance tasks, tooling, config changes not fitting other types  |
| `style`      | Formatting, whitespace, linting (no logic change)                  |
| `ci`         | CI configuration and scripts                                       |
| `revert`     | Reverting a previous commit                                        |

When the diff mixes unrelated concerns (e.g., a feature + unrelated refactor), say so and suggest splitting the commit rather than forcing a misleading one-liner.

When the change spans multiple cohesive edits under one theme, use an umbrella phrase and pick the dominant type.

## Rules

- Describe what is being introduced or changed in the repository at a high level.
- Focus on capability, behavior, architecture, workflow, or scope.
- Always write the final message in American English, even when the prompt is in another language, unless the user explicitly asks for a different language.
- Avoid filenames, functions, classes, variables, or command output.
- Avoid paraphrasing hunks, line-by-line edits, or low-level implementation details.
- Avoid dependency minutiae unless they are the primary change.
- Avoid pretending the scope is cleaner than it is; call out mixed changes when needed.
- Return only the commit message when the user asks for just the message.
- Say explicitly when there are no changes to summarize.

## Heuristics

- Prefer short, verb-led phrasing in the subject.
- Use imperative form by default.
- Prefer American spelling and phrasing.
- When the change spans multiple cohesive edits, use an umbrella phrase instead of enumerating subchanges.
- When the diff mixes unrelated concerns, say so and suggest splitting the commit rather than forcing a misleading one-liner.
- Always include a **branch name** alongside the commit message (see Branch Name Format).

## Branch Name Format

Always output a suggested branch name that mirrors the commit type and subject.

**Format**: `<type>/<kebab-case-summary>`

- Use the same `type` as the commit message.
- Derive a short kebab-case slug (2–5 words) from the commit subject.
- If a scope exists, it may appear as the first segment after the type: `<type>/<scope>-<slug>`.
- Keep the total branch name under ~50 characters.

**Examples**:

| Commit message                                          | Branch name                            |
| ------------------------------------------------------- | -------------------------------------- |
| `feat(model): add learnable slow tau`                   | `feat/model-learnable-slow-tau`        |
| `fix(pipeline): resolve missing value imputation`       | `fix/pipeline-missing-imputation`      |
| `chore: tighten LGPL compliance notes`                  | `chore/tighten-lgpl-compliance`        |
| `refactor: unify adapter initialization`                | `refactor/unify-adapter-init`          |
| `feat(config)!: restructure model configuration schema` | `feat/config-restructure-schema`       |

## Output Modes

- Default: Conventional Commits `type(scope): subject` with optional body, **always followed by a branch name**.
- If asked, provide three variants ordered from most neutral to most descriptive (each with its own branch name).
- If asked, provide `subject + body` with bullet-point breakdown.

## Examples

### Single-type change

```
feat(model): add learnable slow tau and per-neuron gating to LNN
```

🌿 Branch: `feat/model-learnable-slow-tau`

### Scoped fix

```
fix(pipeline): resolve missing value imputation for predictor columns
```

🌿 Branch: `fix/pipeline-missing-imputation`

### Multi-theme change (subject + body)

```
feat(model): extend LNN architecture and harden data pipeline

- Add learnable slow tau, per-neuron gating, and pump branch to LNN model
- Add predictor missingness imputation and warmup scheduler support
- Refactor SWMM simulation runner with resume-from-checkpoint capability
```

🌿 Branch: `feat/model-extend-architecture`

### Breaking change

```
feat(config)!: restructure model configuration schema

BREAKING CHANGE: model config keys have been reorganized; existing
config files must be updated to the new schema.
```

🌿 Branch: `feat/config-restructure-schema`

## Example Requests

- `Use $high-level-commit-message for the current staged changes.`
- `Draft a single high-level commit message in American English without listing low-level file edits.`
- `Use $high-level-commit-message and return 3 variants for this branch.`
