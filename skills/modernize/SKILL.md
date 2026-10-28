---
name: modernize
description: >-
  Change legacy or poorly understood code safely: write down the rules the code
  actually enforces, pin them with characterization tests against the current
  implementation, transform in small steps, and prove equivalence (or document
  every intended difference). Use when /modernize is invoked or as the legacy
  track of /craftsman.
---

# Modernize (`/modernize <module or area> [goal]`)

Old code is a specification nobody wrote down. Rewrite it from memory and you lose the rules it
was quietly enforcing. This workflow recovers the rules first and keeps them under test while
the code changes.

## Search scope

Your workspace is the git work tree you were started in (`git rev-parse --show-toplevel`): search,
list, and read only inside it (prefer `git ls-files`, `git grep`, and editor search scoped to this
folder). Outside the work tree you may read only an exact file cited by a stack trace/error, a temp
directory you created, or Craftsman's own data via `craftsman …` CLI commands. Never run recursive
searches of `~`, `/`, or parent directories, and never open other repositories, other projects'
packets/ledgers/reports, or assistant session stores (run
`craftsman skill show craftsman --file scope.md` for the full contract).

## 1. Survey and agree on the goal
Map the target: entry points, inputs and outputs, dependencies in both directions, existing tests,
known problems. Agree with the user on the kind of change — **upgrade** (newer language, library or
API version, same structure), **restructure** (same behavior, better boundaries), **replace** (new
design meeting the same rules), or **document only** — and on whether behavior must stay identical,
quirks included, or which known bugs may be fixed.

## 2. Recover the rules
Read the code for the behavior it enforces, not the way it is written: calculations and formulas
(with units and rounding), validations and limits, state transitions and required ordering,
defaults, special cases, hard-coded constants (with locations). Write each as a numbered rule:

> **R7** — Given a discount code that expired yesterday, when an order is priced, then the
> code is ignored and the order is priced at list price. *(source: pricing.py:120–134,
> confidence: high)*

Ask the user about every rule marked low confidence. Never copy secrets into rules; reference
their location only.

## 3. Pin the behavior
Turn the rules into characterization tests and run them against the **current** code — they must
pass before you change anything. Add golden-output tests for complex outputs (reports, files,
numeric results) using representative inputs.

## 4. Transform in small steps
Each step keeps the characterization tests green (`craftsman run --kind test`). Give every
recovered rule an explicit home in the new code — a named constant, a function, a validated
type — instead of leaving it implicit.

## 5. Prove equivalence
Run the characterization and golden tests on the result, compare outputs, error behavior and
performance on the same inputs, and list every intended difference with the rule it changes.

## 6. Hand over
Inside `/craftsman`, continue with its quality loop. Finish with a table of rules (preserved /
intentionally changed), structural improvements, and verification results.
