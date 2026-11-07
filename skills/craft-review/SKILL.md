---
name: craft-review
description: >-
  Bug-hunting review of a local diff, branch or pull request. Freezes the diff,
  writes an intent brief, sends fresh-context hunters over it in parallel waves,
  has independent verifiers try to disprove every finding, keeps hunting while
  waves still find real defects, and records the outcome in the Craftsman
  ledger. Use when /craft-review is invoked, when the user asks Craftsman to
  review a diff, branch or pull request, and as the final review of /craftsman.
---

# Craft Review (`/craft-review`)

Your job is to find the bugs that would hurt after merge. Hunters (fresh-context subagents)
maximize recall: they record every candidate with a nameable trigger. Verifiers maximize precision:
they try to disprove each candidate and reproduce the survivors. You orchestrate.

**Start every review with `craftsman review prepare` (step 1), then launch hunters (step 3), even
for a one-line change.** The context that reads a change cannot see its own blind spots, and a
review that is not in the ledger did not happen.

The user needs no flags: infer everything from the request and the repository.

- **Target**: nothing named means the working tree against the base of the active `/craftsman`
  gate, else `HEAD`. A branch or commit: `--base <ref>`. A PR number or URL: `gh pr checkout <n>`
  and pass its merge base as `--base`. "My staged changes", "tracked files", a path or directory
  freeze the review-only scope in round 1 (`--staged`, `--tracked`, `--paths <csv>`).
- **Fix**: "and fix what you find" means step 6 (`--fix`); without it, review only.
  "Comment on the PR" means post the report with `gh pr comment` (`--comment`).
- **Depth** (defined once; others point here): decide it yourself from the risk map and your
  reading. Go deep (every lens, at least two waves, an end-to-end run when fixing)
  for security-relevant code (processes, SQL, deserialization, auth, paths from untrusted input),
  concurrency or clocks, persistent data or migrations, public API or file-format changes, removed
  guarantees, a large or cross-cutting diff, or when the user asks for care ("critical",
  "production", "be thorough", `ultracraft`). Escalate with `craftsman review prepare --deep`;
  `prepare` may print a deep hint. In a gate session this raises the gate to deep, which also
  requires the e2e run. Depth never goes down.

Search scope: `craftsman skill show craftsman --file scope.md`. Never modify the work tree or
index under review: no `git stash`, `checkout`, `switch`, `reset`, `restore`, `clean`; read old code
with `git show <base>:<path>`.

## 1. Prepare

```bash
craftsman review prepare [--deep] [--base <ref>] [--task "<what the change is for>"] [--staged | --tracked | --paths <csv>]
```

It freezes the diff into round N and prints the packet path, the size, the **risk map** (each suggested lens with reasons and hunk ids), quality
hints (`S#`), files not scanned for risk signals, the frozen scope, and warnings (a very large
diff: say so in the report).

Risk signals are a floor, not a verdict. Read the diff yourself and add the lenses it calls for (`trust` for shell, CI, credentials, parsers; `state` for threads, locks, clocks;
`platform` for OS, encoding, time zone, file systems).

If `cover`, `add` or `view` reports a stale round, run `craftsman review prepare` once (unchanged
hunks stay covered). In review-only mode, if the new round holds unexpected files or `prepare`
prints `Ignored (outside the review scope)`, do not widen the scope or run `craftsman review
reset`: ask the user.

## 2. Intent brief

Write 3-8 claims about what the change must do, each with the line that makes it true. A claim
without a line is already a finding (`craftsman review add --lens intent ...`). Qualifiers
("atomic", "with TTL") are claims too. Take the intent from the task, commits, PR description and docs, not
from assumptions about the code. Work from `git diff <base>` and the risk
map, not the full packet (hunters read their slices).

## 3. Hunt plan and wave 1

Decide the hunters, then launch **all hunters of the wave together** (in parallel where the
platform allows) as fresh-context `craftsman-reviewer` subagents.

Every hunter and verifier prompt has the same layout, so the prompt cache serves the shared part:
1. the **shared block**, word for word identical in all of them: intent brief, base ref, rules;
2. its slice: a packet path or `craftsman review view (--files <csv> | --hunks <ranges>)` (a
   bounded view; never paste file contents);
3. its lenses, last (it runs `craftsman skill show craft-review --lenses <csv>` itself).

Guidance (deviate when the change calls for it):
- Every code change gets at least `intent`, `contracts`, `failure`, `ripple`, `tests`, plus every
  lens the risk map suggests or your reading calls for. Docs-only: `intent`, `craft`. Deep:
  every lens.
- `craft` and `design` go to a separate **simplicity hunter**, so they never distract bug
  hunting. On Claude Code launch it with the Agent tool's `model: "haiku"`. Bug hunters and you
  always keep the full model; verifiers too, except as step 4 says.
- 1-3 lenses per hunter. Split large changes by files so each slice stays readable (<= ~400
  changed lines per hunter).
- With two or more hunters, one also runs `sweep` over the whole diff. A tiny change gets one
  hunter with the core lenses.

Platform mechanics: Claude Code, the Agent tool (`craftsman:craftsman-reviewer` /
`craftsman:craftsman-verifier` as a plugin); Copilot, the custom agents of the same names;
Antigravity, `invoke_subagent` with `TypeName` `self` and `Role` `craftsman-reviewer` or
`craftsman-verifier`. Only where the platform cannot launch subagents at all, play the roles one after another
(hunt first, judge afterwards), still record every finding (`review add`) and every hunk
(`cover`), and say **SELF-REVIEWED** in the report.

A subagent that fails (rate limit, timeout) is retried once with a smaller slice. If that fails
too: review-only: `craftsman review report --review-only --partial`; when fixing: ask the user
(in a gate session, `craftsman gate --await-user` first); either way say what is uncovered.

## 4. Verify

When findings exist (`craftsman review show`), launch `craftsman-verifier` subagents in parallel,
one per cluster of related findings or files (no shared root cause across clusters). Brief each
as in step 3, with its finding ids. They merge duplicates, try to disprove, reproduce, and record
the verdicts. No findings, no verification.

On Claude Code, launch a verifier whose cluster holds only `minor` or `nit` findings with the
Agent tool's `model: "haiku"`. A cluster with any `blocker` or `major` finding gets the full model.
Use the clusters you formed; do not split them by severity. A resumed verifier (step 6) keeps the
model it had.

Where agents run in the background (Claude Code, Antigravity async subagents), launch a hunter's
verifier as soon as it finishes. Where parallel subagents are a barrier (Copilot), keep the waves.
A background agent wakes you when it finishes: end your turn instead of polling for it (no `sleep`,
timers or repeated `review show`); a pending timer also keeps a headless session open after the review.

## 5. Saturate

If wave k produced at least one finding judged `proven` (reproduced), run wave k+1 (at most 3
waves per round, counted as generations: a variant of a variant of a variant spawns nothing; at
least 2 when deep). Wave k+1 launches together:
- one **variant hunter** per `proven` root cause, with a mini-slice instead of the whole slice:
  the root cause, its location, `craftsman review view --hunks <affected ids>`, and the task to
  grep for the same pattern across the diff and the code it touches or is touched by;
- one **fresh hunter** with a different partition of the files, or with lenses nobody applied yet.
  Do not tell it earlier findings (it must not be anchored).

When pipelining, start a variant hunter the moment a finding is judged `proven`, and the fresh
hunter at the first `proven` verdict instead of with wave 1 (when deep, with wave 1: it needs a
second wave anyway).

Verify the new findings (step 4). Stop at the first wave that adds nothing proven. Every hunk
needs a `cover`: `craftsman review show --hunks` lists what is left.

## 6. Fix, confirm, re-check, test (when asked to fix, and always in a gate session)

1. Fix every `proven`/`probable` finding at the root cause, with a regression test where the
   project has tests.
2. **Is it fixed?** `proven`: its regression test fails before the fix and passes after
   (`craftsman run`). `probable` (no repro): resume the verifier that judged it where the
   platform can (Claude Code: agent id; Antigravity: conversation id), send only the fix
   diff and ask "fixed or not, with evidence"; else a fresh verifier gets the finding plus the
   diff. Then `craftsman review resolve F# --as fixed --note "<what changed; the run or
   confirmation that shows it>"`. `--as accepted --note "<why; who decided>"` is for
   real trade-offs only: never for a `blocker`; for a `major` only after the user agreed
   (ask; in a gate session wrap the question in `craftsman gate --await-user` / `--resume`).
3. **Did the fix break anything?** `craftsman review prepare` again (add `--add-paths <csv>` for
   new files outside a frozen scope). The new round holds the changed hunks plus the code that
   references changed symbols. One fresh, unanchored hunter checks it (lenses `ripple`,
   `contracts`, `tests`, plus any the risk map suggests), plus a verifier if it records findings.
   Never send the finder or its verifier: they are anchored. Repeat until a round adds nothing
   confirmed. At `round-cap` stop and let the user decide.
4. On the final code: `craftsman run --kind test -- <the project's test command>` (deep, or
   behavior unit tests do not reach: also `--kind e2e -- <real entry point>`); see `/verify`. Runs
   before the last code change do not count; retire a command that can never pass again with
   `craftsman review waive-run R# --reason "..."`.
5. `craftsman review check` prints what is still missing. Fix mode without a gate ends with
   `check`, then `craftsman review report` (no `--review-only`).

## 7. Report

Review-only: `craftsman review check --review-only`, then `craftsman review report --review-only`
(`--partial` when slices stayed uncovered after retries); plain `check`/`report` show BLOCKED
there. Present findings by severity, dismissed ones briefly, and your decisions in plain
language: the depth and why, the hunters and their lenses, why each wave ran or stopped, how
findings were verified (reproduced, or static reading). If `craftsman` is not installed, say the review is ungated. Write rendered Markdown, never `\n`
escapes.
