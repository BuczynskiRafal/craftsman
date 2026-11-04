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
they try to disprove each candidate and reproduce the survivors. You orchestrate: where to look,
how many hunters, when the hunt is dry. The gate checks outcomes (the frozen diff reviewed by
fresh hunters, every finding judged with evidence), not effort.

`/craft-review` plus plain words; the user needs no flags. Infer the rest from
the request and the repository; never ask about depth.

- **Target**: nothing named means the working tree against the base of the active `/craftsman`
  gate, else `HEAD`. A branch or commit: `--base <ref>`. A PR number or URL: `gh pr checkout <n>`
  and pass its merge base as `--base`. "My staged changes" / "tracked files" / a path or directory
  freezes the review-only scope in round 1 (`--staged`, `--tracked`, `--paths <csv>`; not in a gate session).
- **Fix**: "and fix what you find", "fix it" or similar means step 6 (shortcut: `--fix`). Without
  it, review only. "Comment on the PR" means post the report with `gh pr comment` (`--comment`).
- **Depth**: decide it yourself, from the risk map and your own reading. Go deep (every lens, at
  least two waves, an end-to-end run when fixing) for security-relevant code (processes, SQL,
  deserialization, auth, paths from untrusted input), concurrency or clocks, persistent data or
  migrations, public API or file-format changes, removed guarantees, a large or cross-cutting
  diff, or when the user asks for care ("critical", "production", "be thorough", `ultracraft`).
  Escalate with `craftsman review prepare --deep`; `prepare` may print a `Depth: deep suggested
  (signals: ...)` hint. In a gate session this raises the gate to deep for good, which also
  requires the e2e run. Depth never goes down.

Search scope: `craftsman skill show craftsman --file scope.md`. Never modify the work tree or
index under review: no `git stash`, `checkout`, `switch`, `reset`, `restore`, `clean`; read old code
with `git show <base>:<path>`.

## 1. Prepare

```bash
craftsman review prepare [--deep] [--base <ref>] [--task "<what the change is for>"] [--staged | --tracked | --paths <csv>]
```

It freezes the diff into round N and prints the packet path (an index into the code: hunks with
enclosing scopes, callers, risk signals), the size, the **risk map** (each suggested lens with
reasons and hunk ids), quality hints (`S#`), files not scanned for risk signals, the frozen scope,
and warnings (a very large diff: say so in the report).

Risk signals are pattern matches for a few languages: a floor, not a verdict. Read the diff
yourself and add the lenses it calls for (`trust` for shell, CI, credentials, parsers;
`state` for threads, locks, clocks; `platform` for OS, encoding, time zone, file systems).

If `cover`, `add` or `view` reports a stale round, run `craftsman review prepare` once (unchanged
hunks stay covered). In review-only mode, if the new round holds files you did not expect or
`prepare` prints `Ignored (outside the review scope)`, do not widen the scope and do not run
`craftsman review reset`: ask the user.

## 2. Intent brief

Write 3-8 claims about what the change must do, each with the line that makes it true. A claim
without a line is already a finding (`craftsman review add --lens intent ...`). Qualifiers are
claims of their own ("atomic", "with TTL", "idempotent"). Take the intent from the task, commits, PR
description and docs, not from your belief about the code. The brief goes to every
hunter and verifier; so does the base ref.

## 3. Hunt plan and wave 1

Decide the hunters, then launch **all hunters of the wave together** (in parallel where the
platform allows), each as a fresh-context `craftsman-reviewer` subagent. Brief each with: its slice
(a packet path or `craftsman review view (--files <csv> | --hunks <ranges>)`, which writes and
prints a bounded view), the intent brief, the base ref, and its lenses (it loads their playbooks itself with `craftsman skill show craft-review --lenses <csv>`). Never paste file contents.
Never review inline while subagents exist: the context that wrote or read the change hides its
defects. Only `craftsman-reviewer` subagents count as isolated review.

Guidance (you may deviate when the change calls for it):
- Every code change gets at least `intent`, `contracts`, `failure`, `ripple`, `tests`, plus every
  lens the risk map suggests or your reading calls for. Docs-only: `intent`, `craft`. Deep:
  every lens.
- `craft` and `design` go to a separate **simplicity hunter**, so they never take attention from
  bug hunting.
- 1-3 lenses per hunter. Split large changes by files so each slice stays readable (roughly <= 400
  changed lines per hunter).
- With two or more hunters, one also runs `sweep` over the whole diff.
- A tiny change (a few lines, one file) may get one hunter with all core lenses.

Platform mechanics: Claude Code, the Agent tool (`craftsman:craftsman-reviewer` /
`craftsman:craftsman-verifier` when installed as a plugin); Copilot, the custom agents of the same
names; Antigravity, `invoke_subagent` with `TypeName` `self` and `Role` `craftsman-reviewer` or
`craftsman-verifier`. With no subagent mechanism, play the roles one after another (hunt first,
judge afterwards) and say **SELF-REVIEWED** in the report.

A subagent that fails (rate limit, timeout) is retried once with a smaller slice. If they keep
failing: review-only: `craftsman review report --review-only --partial` and tell the user what is
uncovered; when fixing or in a gate session: `craftsman gate --await-user` and say the same.

## 4. Verify

When findings exist (`craftsman review show`), launch `craftsman-verifier` subagents in parallel,
one per cluster of related findings or files (clusters should not share a root cause). Brief each
with its finding ids, the intent brief and the base ref. They merge duplicates, try to disprove,
reproduce, and record the verdicts. With no findings, skip verification.

## 5. Saturate

If wave k produced at least one finding judged `proven` or `probable`, run wave k+1 (at most 3
waves per round; at least 2 when deep). Wave k+1 launches together:
- one **variant hunter** per confirmed root cause: give it the root cause and its location and ask
  for the same mistake elsewhere in the diff and in the code it touches or is touched by;
- one **fresh hunter** with a different partition of the files, or with lenses nobody applied yet.
  Do not tell it earlier findings (it must not be anchored).

Verify the new findings (step 4). Stop at the first wave that adds nothing confirmed. Every open
hunk needs a `cover`: `craftsman review show --hunks` lists what is left.

## 6. Fix, re-check, test (when asked to fix, and always in a gate session)

1. Fix every `proven`/`probable` finding at the root cause, with a regression test where the
   project has tests. Then `craftsman review resolve F# --as fixed --note "<what changed; which
   test covers it>"`. `--as accepted --note "<why; who decided>"` is for real trade-offs only: never
   for a `blocker`; for a `major` only after the user agreed (`craftsman gate --await-user`, ask,
   `craftsman gate --resume`).
2. `craftsman review prepare` again (add `--add-paths <csv>` for new files outside a frozen
   scope). The new round holds only the changed hunks plus hunks that reference a changed symbol.
   One fresh hunter re-checks them, plus a verifier if it records findings. Repeat until a round
   adds nothing confirmed. At `round-cap` stop and let the user decide.
3. On the final code: `craftsman run --kind test -- <the project's test command>` (deep, or
   behavior unit tests do not reach: also `--kind e2e -- <real entry point>`); see `/verify`. Runs
   before the last code change do not count; retire a command that can never pass again with
   `craftsman review waive-run R# --reason "..."`.
4. `craftsman review check`: it prints what is still missing.

## 7. Report

Review-only: `craftsman review check --review-only`, then `craftsman review report --review-only`
(`--partial` when slices stayed uncovered after retries). Present the report: findings by
severity, dismissed ones briefly, plus your own decisions in plain language: the depth and why,
the hunters and their lenses, the waves and why another wave ran or stopped, and how findings were
verified (reproduced, or static reading); SELF-REVIEWED when no subagents ran. Plain `check`/`report` show BLOCKED in review-only mode, so always pass `--review-only`. If
`craftsman` is not installed, say the review is ungated. Write the reply as
rendered Markdown with real line breaks, never `\n` escapes.

## Recording mechanics (for hunters and verifiers)

- One command per line, no `\` continuations. bash/zsh/PowerShell: single quotes (PowerShell
  escapes `'` as `''`); cmd.exe: double quotes, inner `\"`. Use the project's Python (`python3`
  does not exist on many Windows machines).
- Batches: `craftsman review add|cover|verdict --from-json - <<'EOF' [ ... ] EOF` with entries
  `{"lens","file","line","severity","title","scenario","suggestion"}`, `{"hunks":[...] or
  "files":[...],"note"}`, `{"id","as","evidence"}`. A rejected batch records nothing and lists
  every entry to fix: fix them all, resend once.
- PowerShell: `@'` newline JSON newline `'@ | craftsman review add --from-json -`. Windows
  PowerShell 5.1 pipes ASCII only; for other text run `craftsman review inbox <add|cover|verdict>`,
  write UTF-8 to the printed path (`Set-Content -LiteralPath <path> -Encoding utf8`) and pass
  `--from-json <path>`.
- Throwaway scripts: `craftsman review scratch` prints a managed directory.
