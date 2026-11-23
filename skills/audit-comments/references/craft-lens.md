# Craftsman Review Lenses

A lens is one narrow way of looking at a change: a goal, what to read first, and numbered probes.
Answer every probe for every hunk in your slice. If you cannot answer one from what you have read,
open more files before moving on.

How to use a probe: pick a concrete value or situation and *execute the code in your head*, line
by line, writing the intermediate values down. Most real defects surface at the moment a reviewer
substitutes a specific value instead of reading the code as prose.

---

## `craft` — Clarity, reuse & simplicity

**Goal:** the change is as simple as the problem allows and consistent with the codebase. Only
record items with a concrete cost (a likely future bug, duplicated maintenance, measurable waste).

**Probes**
1. Reuse: search for an existing helper, constant or pattern that does what the new code
   reimplements (search by behavior and likely names, not just the exact name). Name it.
2. Needless complexity: derivable state, single-use indirection, flags that are always the same
   value, nesting that guard clauses would flatten, copy-pasted blocks that differ in one value.
3. Dead weight: unused variables, imports, parameters, unreachable branches, code kept "just in
   case".
4. Honest names and comments: each name says what the thing is; every comment and docstring in
   the touched area still describes the code *after* the change. Test names state the behavior
   under test (`test_<subject>_<when>_<then>`) without version numbers, plan IDs, or case counters.
5. One question, one function: two functions must not answer the same domain question with
   slightly different callers; domain concepts with a fixed shape use explicit types (dataclasses,
   NamedTuples, TypedDicts) rather than plain `dict`s.
6. Wasted work: repeated I/O or computation in a loop, eager work that is rarely needed,
   independent sequential calls, quadratic behavior on inputs that can be large, logging inside hot
   loops.
7. Consistency: error handling, logging, naming and layout match the surrounding code and the
   project's convention files.
