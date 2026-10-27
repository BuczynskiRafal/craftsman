---
name: audit
description: >-
  Focused deep audits of a diff, module or directory, one concern at a time:
  errors, tests, types, comments, security, plus optional quality and perf.
  Each audit is an in-depth run of the matching review lens with its own
  inventory table. Use when /audit [aspects] [target] is invoked.
---

# Audit (`/audit [errors,tests,types,comments,security,quality,perf | all] [target] [--fix]`)

A review looks at a change through many lenses at once; an audit takes **one** concern and goes
through the target exhaustively, building an inventory instead of sampling. Use it for code that
matters more than average, before a release, or when a review hinted at a systemic problem.

| Aspect | Skill | Lens it deepens | Selected by `all` when the target… |
| :--- | :--- | :--- | :--- |
| `errors` | `/audit-errors` | `failure` | handles errors, does I/O, parsing, subprocesses, network |
| `tests` | `/audit-tests` | `tests` | changes behavior (almost always) |
| `types` | `/audit-types` | `model` | adds or changes classes, records, enums, schemas |
| `comments` | `/audit-comments` | `craft` (probe 4) | adds or changes comments or docstrings |
| `security` | `/audit-security` | `trust` | touches paths, processes, SQL, deserialization, auth, network |
| `quality` | `/code-quality` | `craft` | on request |
| `perf` | `/extreme-optimizer` | — | on request |

## How to run

1. **Target.** Default: the cumulative diff (working tree vs. the `/craftsman` base or `HEAD`),
   *plus* the full files it touches. A path or module audits those files entirely.
2. **Select aspects** as listed, or from the argument.
3. **Execute.** One aspect: do it yourself. Several aspects on a large target: one subagent per
   aspect when the platform offers subagents, each given the skill to follow and the target.
4. **Challenge before reporting.** Every finding goes through the verification discipline of
   `/code-review` Step 3 (try to disprove it, reproduce it if possible). Inside a `/craftsman`
   session, record findings and verdicts in the ledger with `craftsman review add` /
   `craftsman review verdict`; outside one, keep them in the report.
5. **Report** one merged list ordered by severity (`blocker`, `major`, `minor`), each entry with
   `file:line`, the concrete trigger and outcome, the verdict, and a fix. With `--fix`, apply the
   fixes that do not change intended behavior, then run the project's tests through
   `craftsman run --kind test`.
