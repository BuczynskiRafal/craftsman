---
name: audit-errors
description: >-
  Audit error handling: where failures happen, where each error ends up and
  what stays hidden. Use for /audit-errors or /audit errors.
---

# Error-handling audit (`/audit-errors [target]`)

Deepens the `failure` lens (run `craftsman skill show craft-review --lenses failure` to read that
playbook first; never search the file system for `lenses.md`).

## Search scope

Your workspace is the git work tree you were started in (`git rev-parse --show-toplevel`): search,
list, and read only inside it (prefer `git ls-files`, `git grep`, and editor search scoped to this
folder). Never run recursive searches of `~`, `/`, or parent directories, and never open other
repositories, other projects' packets/ledgers/reports, or assistant session stores (run
`craftsman skill show craftsman --file scope.md` for the full contract).

The question for every failure site: **when this goes wrong, who finds out, and can they act on
it?** An error that ends in a log nobody reads, a default value that looks like real data, or a
success exit code is a defect even if nothing crashes.

## 1. Build the inventory

Walk the target and list every **failure site** (a call that can fail: I/O, parsing, network,
subprocess, lookup, conversion, external API, assertion) and every **handling site** (`try`,
`except`/`catch`, `finally`, `defer`/`recover`, `Result`/`Option` handling, error callbacks,
promise `.catch`, return-code checks).

| # | Location | Can fail with / catches | What happens next | Ends at | Assessment |
| - | -------- | ----------------------- | ----------------- | ------- | ---------- |

*Ends at* is where the error becomes visible: raised to the caller, shown to the user, exit code,
HTTP status, log only, or nowhere.

## 2. Assess each row

Run every probe of the `failure` lens against each row, and flag the row when a probe fails. Name
the probe that flagged it (for example "probe 2, disguised: returns `[]` on a parse error"). The
lens defines the defects: a failure site nobody handles (1, 6), swallowed or disguised errors
(2), partial effects (3, 4), unbounded retries (5), unhelpful messages (7) and wrong signals to
the outside, fail-open included (8). The audit adds one question per row, the *Ends at* column: an
error that ends "nowhere" or "log only" while the caller proceeds as if the call succeeded is
flagged whatever else the row does.

Leave a row unflagged when the handling is deliberate, narrow and visible, and say why in one line.

## 3. Report

The inventory table (flagged rows first), then findings in the standard form: `file:line`,
trigger -> outcome, severity, verdict after challenge, and the concrete fix (narrower handler,
propagate with context, atomic write, explicit error result, non-zero exit).
