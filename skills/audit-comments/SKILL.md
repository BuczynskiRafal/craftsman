---
name: audit-comments
description: >-
  Audit comments and docstrings against the code they describe: extracts each
  factual claim, checks it against the implementation, and classifies it as
  accurate, stale, misleading, redundant, or missing an important why. Use when
  /audit-comments is invoked or via /audit comments.
---

# Comment & docstring audit (`/audit-comments [target]`)

Deepens probe 4 of the `craft` lens in `skills/code-review/lenses.md`.

A comment is a claim about code that no compiler checks. When the code changes and the comment
does not, the comment starts lying — and readers trust comments more than code.

## 1. Extract claims

For every comment, docstring, README snippet and help text in the target (and every one that
describes code the diff changed, wherever it lives), list the factual claims it makes:
parameters and their meaning, return values, raised errors, side effects, units, ranges,
complexity, thread-safety, ordering, defaults, "this is only called from X", "never None".

## 2. Check each claim against the code

Open the implementation and its callers. Classify:
- **accurate** — the code does what the claim says;
- **stale** — it was true before a change and is not now (check `git log -L` or the diff);
- **misleading** — technically true but invites a wrong conclusion (omits an important case);
- **redundant** — restates the next line of code and adds nothing;
- **missing why** — the code makes a non-obvious choice (a workaround, an ordering requirement,
  a magic number) with no explanation anywhere.

## 3. Report

| Location | Claim | Classification | Evidence (file:line in code) | Fix |
| -------- | ----- | -------------- | ---------------------------- | --- |

Stale and misleading claims are findings (`minor`, or `major` when a caller would misuse the API
because of them). Propose exact replacement text. Prefer deleting redundant comments over
rewriting them. With `--fix`, apply the text changes.
