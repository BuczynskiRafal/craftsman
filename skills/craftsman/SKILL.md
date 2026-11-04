---
name: craftsman
description: >-
  Build freely, review hard. Captures a baseline, lets you implement the task
  without ceremony, then freezes the final diff and runs an adaptive hunt:
  parallel fresh-context hunters, independent verifiers and repeated waves while
  they keep finding real defects; then fixes, test runs and the Craftsman gate.
  The agent picks the depth itself; the user needs no flags. Use when
  /craftsman is invoked or the prompt contains `ultracraft`.
---

# Craftsman (`/craftsman <task>`)

The user writes the task in plain words and needs no flags: infer everything else from the request
and the repository, and decide depth yourself without asking. You own this change from request to a reviewed, tested diff that is ready to commit. You are done
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
Start deep (`--deep`, a one-line shortcut) when the user's words ask for care ("critical",
"production", "be thorough") or say `ultracraft`; otherwise start normal and escalate later (Stage 2
step 1). It records the base commit: the review covers everything since then, commits included.
A request that only asks for a review (no fixing, no `/craftsman`, no `ultracraft`) starts no
gate: follow `/craft-review` in review-only mode instead.

When deep from the start, write a short design note first: invariants and hazards, two candidate approaches
with trade-offs and your choice, the three likeliest production failures.

## Stage 1 - Build freely

No gate activity here: no ledger entries, no review, no ceremony. Pick the track that fits:
`/feature-dev` (new feature), `/modernize` (legacy or deep refactor), `/batch` (mechanical change
across many files), or just edit. Run tests and tools whenever useful with plain commands. Ask the
user only when an ambiguity changes what you build: `craftsman gate --await-user`, ask, then
`craftsman gate --resume`. End the build with `/simplify`, so the hunters see the final shape.

## Stage 2 - Final review

1. `craftsman review prepare` freezes the diff into round N and prints the packet path and the
   risk map. Decide the depth now, as `/craft-review` describes: when the risk map or your own
   reading shows risk (security-relevant code, concurrency or clocks, persistent data, public API
   or file-format changes, removed guarantees, a large or cross-cutting diff), run
   `craftsman review prepare --deep`. In a gate session that raises the gate to deep for good, and
   deep also requires the e2e run. Depth never goes down.
2. Write a compact summary for the hunt: the task, what you built per file in a line, the
   decisions a reader could mistake for bugs. The summary states intent; it must not argue that
   the code is correct. Your context holds the reasoning that produced the code, which is what
   hides its defects, so the hunters are fresh-context subagents and you only orchestrate.
3. Follow `/craft-review` from its intent brief on, in gate-session mode: hunt plan, wave 1,
   verification, saturation waves, fixes with a regression test where the project has tests,
   `craftsman review resolve`, a fresh round for the fix delta. It is the single source for the
   mechanics; do not improvise a lighter review.
4. Run the tests on the final code through the ledger: `craftsman run --kind test -- <command>`;
   when deep, or when unit tests do not reach the behavior (a CLI, a route, a migration), also
   `craftsman run --kind e2e -- <command that exercises the real entry point>` (see `/verify`).
   Runs before the last code change do not count.
5. `craftsman review check`, do what it lists, then `craftsman gate --complete`. At `round-cap`
   stop iterating, report what remains and let the user decide; the gate lets you finish.

## Stage 3 - Report

Run `craftsman review report` and give the user a short summary: what was built (files, one line
each); your decisions in plain language (the depth and why; the hunters and their lenses; the waves; how
findings were verified); findings fixed, accepted (with reasons) and dismissed
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
