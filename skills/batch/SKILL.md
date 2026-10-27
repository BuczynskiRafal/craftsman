---
name: batch
description: >-
  Carry out one mechanical or structural change across many files (API
  migrations, signature changes, typing or error-handling sweeps) by splitting
  it into disjoint work units executed in parallel, each with its own check,
  followed by a whole-repository verification. Use when /batch is invoked or as
  the mass-change track of /craftsman.
---

# Batch change (`/batch <the change to apply everywhere>`)

## 1. Measure the change
Find every occurrence the change applies to (search by name, by pattern, and by behavior). Count
files and occurrences, and note the odd cases that do not fit the common pattern — they are where
batch changes go wrong.

## 2. Define the transformation precisely
Write the rule as "before -> after" with one worked example per variant you found, including the
odd cases. Decide what a worker must do when it meets a case the rule does not cover: stop and
report it, never improvise.

## 3. Split into work units
Group files into units that share no file (two workers must never edit the same file), small
enough to review (roughly 3–15 files). Give each unit a title, its file list, and a check command
that runs quickly for just those files (tests, type checker, linter).

## 4. Get approval
Show the user the count, the rule with examples, the unit table and the check commands. Wait for
a go-ahead before changing anything.

## 5. Execute
Start one worker per unit (subagents in parallel where the platform allows; otherwise one unit at
a time yourself). Each worker receives: the rule and examples, its file list with the explicit
instruction not to edit anything else, the project conventions, and its check command, which it
must run and report.

## 6. Converge
Track units in a table (`unit | files | status | check result | cases reported`). Resolve
reported odd cases yourself. Then verify the whole repository: full test suite, type checker,
linter, and a search showing no old-pattern occurrences remain. Inside `/craftsman`, continue
with its quality loop.
