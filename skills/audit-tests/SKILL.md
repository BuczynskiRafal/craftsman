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

Deepens the `tests` lens of `skills/code-review/lenses.md` — read that playbook first.

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

- behaviors with no test, especially error paths, empty inputs, limits and the boundary the change
  moved;
- a bug fix with no test that fails before the fix;
- tests that execute the code but assert nothing meaningful (only "no exception", only types,
  only the mock was called);
- mocks that replace the logic under test, or fixtures so large that the test passes because of
  the setup;
- dependence on wall-clock time, randomness without a seed, test order, network, environment
  variables or the developer's machine;
- shared mutable state between tests, missing cleanup of files or processes.

## 3. Close the most valuable gaps

Pick the 3–5 gaps with the highest risk (likelihood x impact) and write the tests as real code in
the project's framework and style, each with a one-line comment on what mistake it catches. With
`--fix` add them to the suite and run them through `craftsman run --kind test`; a new test that
passes immediately on a known bug is wrong — fix the test.

## 4. Report

The behavior map, the gap findings (`file:line`, risk, verdict), and the tests written.
