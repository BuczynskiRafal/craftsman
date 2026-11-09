---
name: audit-tests
description: >-
  Audit whether the tests would actually catch the change being wrong: maps each
  behavior to the test that proves it, runs revert and mutant thought
  experiments, checks boundary and error paths, inspects test quality, and
  writes the most valuable missing tests as code. Use when /audit-tests is
  invoked or via /audit tests.
---

# Test-evidence audit (`/audit-tests [target]`)

Deepens the `tests` lens (run `craftsman skill show craft-review --lenses tests` to read that
playbook first; never search the file system for `lenses.md`).

## Search scope

Your workspace is the git work tree you were started in (`git rev-parse --show-toplevel`): search,
list, and read only inside it (prefer `git ls-files`, `git grep`, and editor search scoped to this
folder). Never run recursive searches of `~`, `/`, or parent directories, and never open other
repositories, other projects' packets/ledgers/reports, or assistant session stores (run
`craftsman skill show craftsman --file scope.md` for the full contract).

Coverage percentages measure which lines ran, not which mistakes would be caught. This audit
measures the second.

## 1. Behavior map

List the behaviors the target provides or the change introduces — one row per observable
behavior, including error behavior — and the test that pins each one.

| Behavior (observable) | Changed? | Pinned by test | Would fail if reverted? | Would fail for a mutant? |
| --------------------- | -------- | -------------- | ----------------------- | ------------------------ |

Fill the last two columns by reasoning concretely: name the mutant (`<` -> `<=`, branch deleted,
early `return`, wrong argument order) and the assertion that would catch it, or write "no".

## 2. Gaps to look for

Apply probes 1 to 6 of the `tests` lens to every row of the map, not only to the changed ones.
A row where a probe finds a gap is a gap row. The lens's *Limits* (one finding for missing tests
per change) do not apply here: this audit lists every gap, then ranks them in step 3.

## 3. Close the most valuable gaps

Pick the 3–5 gaps with the highest risk (likelihood x impact) and write the tests as real code in
the project's framework and style, each with a one-line comment on what mistake it catches. With
`--fix` add them to the suite and run them through `craftsman run --kind test`; a new test that
passes immediately on a known bug is wrong — fix the test.

## 4. Report

The behavior map, the gap findings (`file:line`, risk, verdict), and the tests written.
