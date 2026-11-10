---
name: audit
description: >-
  Focused deep audit of a diff, module or directory, one concern at a time:
  errors, tests, types, comments, security. Use for /audit [aspects] [target].
---

# Audit (`/audit [errors,tests,types,comments,security | all] [target] [--fix]`)

A review looks at a change through many lenses at once; an audit takes **one** concern and goes
through the target exhaustively, building an inventory instead of sampling. Use it for code that
matters more than average, before a release, or when a review hinted at a systemic problem.

## Search scope

Your workspace is the git work tree you were started in (`git rev-parse --show-toplevel`): search,
list, and read only inside it (prefer `git ls-files`, `git grep`, and editor search scoped to this
folder). Outside the work tree you may read only an exact file cited by a stack trace/error, a temp
directory you created, or Craftsman's own data via `craftsman …` CLI commands (invoke `/audit-*` as
a skill or run `craftsman skill show <name>`). Never run recursive searches of `~`, `/`, or parent
directories, and never open other repositories, other projects' packets/ledgers/reports, or
assistant session stores (run `craftsman skill show craftsman --file scope.md` for the full
contract).

| Aspect | Skill | Lens it deepens | Selected by `all` when the target… |
| :--- | :--- | :--- | :--- |
| `errors` | `/audit-errors` | `failure` | handles errors, does I/O, parsing, subprocesses, network |
| `tests` | `/audit-tests` | `tests` | changes behavior (almost always) |
| `types` | `/audit-types` | `model` | adds or changes classes, records, enums, schemas |
| `comments` | `/audit-comments` | `craft` (probe 4) | adds or changes comments or docstrings |
| `security` | `/audit-security` | `trust` | touches paths, processes, SQL, deserialization, auth, network |

## How to run

1. **Target.** Default: the cumulative diff (working tree vs. the `/craftsman` base or `HEAD`),
   *plus* the full files it touches. A path or module audits those files entirely.
2. **Select aspects** as listed, or from the argument.
3. **Execute.** One aspect: do it yourself. Several aspects on a large target: one subagent per
   aspect when the platform offers subagents, each given the skill to follow and the target.
4. **Challenge before reporting.** Every finding goes through the verification discipline of
   `/craft-review` (a verifier tries to disprove it and reproduces it if possible). Keep findings and
   verdicts in your reply and never record them in the ledger: during a `/craftsman` build no review
   round exists, and only the final review writes the ledger.
5. **Report** one merged list ordered by severity (`blocker`, `major`, `minor`), each entry with
   `file:line`, the concrete trigger and outcome, the verdict, and a fix. With `--fix`, apply the
   fixes that do not change intended behavior, then run the project's tests through
   `craftsman run --kind test`.
