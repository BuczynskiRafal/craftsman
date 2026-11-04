---
name: craftsman
description: >-
  Build freely, review hard. Captures a baseline, lets you implement the task
  without ceremony, then freezes the final diff and runs an adaptive hunt:
  parallel fresh-context hunters, independent verifiers and repeated waves while
  they keep finding real defects; then fixes, test runs and the Craftsman gate.
  Flag: --deep (every lens, end-to-end run). Use when /craftsman is invoked or
  the prompt contains `ultracraft`.
---

# Craftsman (`/craftsman [--deep] <task>`)

You own this change from request to a reviewed, tested diff that is ready to commit. You are done
when `craftsman review check` passes, not when the code compiles and not when you feel confident.
Lifecycle hooks (where the platform has them) send you back with the list of what is missing if you
try to finish early. The gate judges outcomes on the final diff, so build however works best and
spend your care on the final review.

Talk to the user in their language; keep code, identifiers and commit messages in American English.

A `/name` below means: invoke `/name` as a skill if your platform offers it, otherwise run
`craftsman skill show <name>` and follow its output. Never search the file system for skill files.
Write every `craftsman` command on one line.

## Search scope

- **Workspace:** the git work tree you were started in (`git rev-parse --show-toplevel`). Search, list and read only inside it: prefer `git ls-files`, `git grep` and editor search scoped to this folder (in a multi-root workspace always pass an include pattern for it).
- **Allowed outside the work tree:** (a) the exact file a stack trace, import or build error points to; (b) a temp directory you created for this task; (c) Craftsman's own data through `craftsman …` commands (`skill show`, `review show`, `status`, `review inbox`) — never open `.git/craftsman` files by hand except the packet path or inbox path that `craftsman review prepare`, `show` or `inbox` printed for this repository; (d) CI results and logs of this repository's current branch or PR.
- **Batch files:** JSON batches go only to `craftsman review inbox` paths, never to `%TEMP%` or the repository. Delete any file you had to create elsewhere before you report.
- **Forbidden:** recursive searches of the home directory, the file-system root, a drive root or any parent of the work tree (`find ~`, `find /`, `grep -r … ~`, `rg … ..`, `Get-ChildItem -Recurse C:\`, `mdfind`, `locate`, `tree ~`); opening other repositories, other projects' packets, ledgers or reports, or any assistant's session store (`~/.copilot/session-state`, `~/.claude/projects`, `~/.gemini/antigravity/brain`, `~/.gemini/antigravity/conversations`).
- **In doubt:** if a file outside the work tree seems necessary, ask the user for its exact path first.
- **Focus:** read the hunks, their enclosing scopes, callers, tests and the project's convention files — not unrelated history, old reports or documentation the change does not touch.

## Stage 0 - Gate

```bash
craftsman gate --init [--deep] --task "<the user's request in one line>"
```

The prompt hook runs this on platforms with hooks; otherwise run it yourself before touching code.
Use `--deep` for `/craftsman --deep` and `ultracraft`; `--quick` and `--standard` are accepted and
ignored. It records the base commit: the review covers everything since then, commits included.
A request that only asks for a review (no `--fix`, `/craftsman` or `ultracraft`) starts no gate:
follow `/code-review` in review-only mode instead.

For `--deep`, write a short design note first: invariants and hazards, two candidate approaches
with trade-offs and your choice, the three likeliest production failures.

## Stage 1 - Build freely

No gate activity here: no ledger entries, no review, no ceremony. Pick the track that fits:
`/feature-dev` (new feature), `/modernize` (legacy or deep refactor), `/batch` (mechanical change
across many files), or just edit. Run tests and tools whenever useful with plain commands. Ask the
user only when an ambiguity changes what you build: `craftsman gate --await-user`, ask, then
`craftsman gate --resume`. End the build with `/simplify`, so the hunters see the final shape.

## Stage 2 - Final review

1. `craftsman review prepare` freezes the diff into round N and prints the packet path and the
   risk map.
2. Write a compact summary for the hunt: the task, what you built per file in a line, the
   decisions a reader could mistake for bugs. The summary states intent; it must not argue that
   the code is correct. Your context holds the reasoning that produced the code, which is what
   hides its defects, so the hunters are fresh-context subagents and you only orchestrate.
3. Follow `/code-review` from its intent brief on, in gate-session mode: hunt plan, wave 1,
   verification, saturation waves, fixes with a regression test where the project has tests,
   `craftsman review resolve`, a fresh round for the fix delta. It is the single source for the
   mechanics; do not improvise a lighter review.
4. Run the tests on the final code through the ledger: `craftsman run --kind test -- <command>`;
   with `--deep`, or when unit tests do not reach the behavior (a CLI, a route, a migration), also
   `craftsman run --kind e2e -- <command that exercises the real entry point>` (see `/verify`).
   Runs before the last code change do not count.
5. `craftsman review check`, do what it lists, then `craftsman gate --complete`. At `round-cap`
   stop iterating, report what remains and let the user decide; the gate lets you finish.

## Stage 3 - Report

Run `craftsman review report` and give the user a short summary: what was built (files, one line
each); the hunt (hunters, lenses, waves); findings fixed, accepted (with reasons) and dismissed
(count); the verification commands and results; a proposed commit message from
`/high-level-commit-message`. Write it as rendered Markdown with real line breaks, never `\n`
escapes, and do not wrap it in quotes, JSON or a code block. Never commit, push or open a pull
request unless the user asked.

## Autonomy

- Never stop to ask what you can decide yourself. Involve the user only for a real ambiguity in
  what to build, for accepting a `major` finding, and at the round cap.
- Never lower the rigor to save effort: no inline review where subagents exist, no skipped waves.
- A subagent that fails (rate limit, timeout) is retried once with a smaller slice. If subagents
  keep failing, run `craftsman gate --await-user` and tell the user what is not covered.
