---
name: audit-errors
description: >-
  Exhaustive error-handling audit: inventories every place where failure can
  happen or is handled, traces where each error ends up, and flags hidden
  failures, over-broad handlers, misleading fallbacks, partial side effects and
  unhelpful messages. Use when /audit-errors is invoked or via /audit errors.
---

# Error-handling audit (`/audit-errors [target]`)

Deepens the `failure` lens (run `craftsman skill show code-review --lenses failure` to read that
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

Flag a row when any of these holds:
- **Hidden:** the error ends "nowhere" or "log only" while the caller proceeds as if it succeeded.
- **Disguised:** the handler returns a value indistinguishable from a legitimate result
  (`None`, `0`, `""`, `[]`, `{}`, `False`, `True`, a cached or stale value).
- **Over-broad:** the handler catches far more than the failure it was written for (all
  exceptions around a large block, catching programming errors such as type or key errors).
- **Lossy:** re-raised or wrapped without the original cause, or the message lacks the file,
  value, limit or identifier needed to act.
- **Half-done:** state already mutated, files partially written, or messages already sent when the
  error occurs, with no rollback, atomic write, or report.
- **Unbounded:** retries without limit, backoff or idempotency.
- **Wrong signal:** CLI exits 0 after an error, API returns 2xx with an error body, a hook
  "allows" an action because its own check crashed (fail-open).
- **Missing:** a failure site with no handling where the caller cannot reasonably handle it
  either (for example a subprocess without a timeout or exit-code check).

Leave a row unflagged when the handling is deliberate, narrow and visible — say why in one line.

## 3. Report

The inventory table (flagged rows first), then findings in the standard form: `file:line`,
trigger -> outcome, severity, verdict after challenge, and the concrete fix (narrower handler,
propagate with context, atomic write, explicit error result, non-zero exit).
