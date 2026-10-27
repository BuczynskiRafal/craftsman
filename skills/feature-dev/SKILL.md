---
name: feature-dev
description: >-
  Build a non-trivial feature by first learning how the codebase already solves
  similar problems, settling open questions with the user, choosing a design on
  explicit trade-offs, and then implementing it in small verified steps. Use
  when /feature-dev is invoked or as the feature track of /craftsman.
---

# Feature development (`/feature-dev <what to build>`)

Most feature bugs are decided before the first line is written: a missed existing helper, an
unasked question about an edge case, a design that fights the codebase. This workflow front-loads
those decisions and keeps them visible.

## Search scope

Your workspace is the git work tree you were started in (`git rev-parse --show-toplevel`): search,
list, and read only inside it (prefer `git ls-files`, `git grep`, and editor search scoped to this
folder). Outside the work tree you may read only an exact file cited by a stack trace/error, a temp
directory you created, CI logs for this branch/PR, or Craftsman's own data via `craftsman …` CLI
commands. Never run recursive searches of `~`, `/`, or parent directories, and never open other
repositories, other projects' packets/ledgers/reports, or assistant session stores (run
`craftsman skill show craftsman --file scope.md` for the full contract).

## 1. Frame the feature
Write two or three sentences: who needs what, and how they will notice it works. Add explicit
**non-goals**. If you cannot write this without guessing, ask now.

## 2. Learn the terrain
Find out how the codebase already does similar things before designing anything:
- the closest existing feature — read it end to end, from entry point to storage/output;
- the layers it crosses, the extension points it uses, the conventions for errors, logging,
  configuration, naming and tests;
- helpers you can reuse, and the test utilities and fixtures the project relies on.

For a feature that spans several areas, split this exploration between parallel subagents (one
per area) and merge their summaries. Finish with a short map: files the feature will touch or
add, and the patterns it must follow.

## 3. Close the open questions
List what the request leaves undecided that changes the implementation: edge cases, invalid
input, error behavior, limits, compatibility with existing data or APIs, permissions, what is out
of scope. Ask the user the ones that matter, each with your recommended answer first, and wait.
Do not ask what the codebase already answers.

## 4. Choose a design
Sketch two or three genuinely different approaches (for example: smallest change that reuses
existing structures vs. a new module with a clean boundary). For each: files touched, how it fits
the existing patterns, what becomes easy or hard later, risks. Recommend one with a reason and
get agreement when the choice is not obvious or is expensive to reverse.

## 5. Build in verified steps
Implement in small steps that each leave the project working. Write or extend tests alongside
the code — at least the main path, the edge cases settled in step 3, and one error path. Run them
through `craftsman run --kind test` as you go.

## 6. Hand over to the quality loop
Inside `/craftsman`, continue with its Stage 2. Standalone, run `/simplify`, `/code-review`, and
`/verify` on the result.

## 7. Summarize
What was built, the design chosen and why, files added or changed, and how it was verified.
