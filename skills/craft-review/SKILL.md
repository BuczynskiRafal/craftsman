---
name: craft-review
description: >-
  Bug-hunting review of a local diff, branch or pull request. Freezes the diff,
  plans the hunt from it, sends fresh-context hunters over every slot, has
  verifiers try to disprove every finding, hunts again while waves prove
  defects, and records the outcome in the Craftsman ledger. Use when
  /craft-review is invoked, when the user asks Craftsman to review a diff,
  branch or pull request, and as the final review of /craftsman.
---

# Craft Review (`/craft-review`)

Your job is to find the bugs that would hurt after merge. The CLI plans the hunt from the diff and checks that it
happened; you launch it and do what `craftsman review check` lists. Hunters (fresh-context subagents) maximize recall;
verifiers maximize precision. In an enforced review only a hunter's coverage and a verifier's verdicts count. **Start
with `craftsman review prepare` (step 1), even for a one-line change:** the context that reads a change cannot see its
own blind spots, and a review not in the ledger did not happen. The user needs no flags: infer everything.

- **Target**: nothing named means the working tree against the base of the active `/craftsman` gate, else `HEAD`. A
  branch or commit: `--base <ref>`. A PR number or URL: `gh pr checkout <n>`, then its merge base as `--base`. "My
  staged changes", "tracked files", a path or directory freeze the review-only scope in round 1 (`--staged`,
  `--tracked`, `--paths <csv>`).
- **Fix**: "and fix what you find" means step 4 (`--fix`), else review only. "Comment on the PR": `gh pr comment`
  (`--comment`).
- **Depth** (defined once; others point here): `prepare` goes deep by itself when a security or concurrency signal fires
  in a production hunk or the change is large (over 250 changed production lines), and says why. Add `--deep` for
  security-relevant code (processes, SQL, deserialization, auth, paths from untrusted input), concurrency or clocks,
  persistent data or migrations, public API or file-format changes, removed guarantees, a cross-cutting diff, or when
  the user asks for care ("critical", "production", "be thorough", `ultracraft`). Deep: every lens on each production
  hunk, a fresh second look, an end-to-end run when fixing; it raises a gate. Depth never goes down.

Search scope: `craftsman skill show craftsman --file scope.md`. Never modify the work tree or index under review (no
`git stash`, `checkout`, `switch`, `reset`, `restore`, `clean`); old code: `git show <base>:<path>`.

## 1. Prepare

```bash
craftsman review prepare [--deep] [--base <ref>] [--task-file - | --task "<what the change is for>"] [--staged | --tracked | --paths <csv>]
```

The task text is the **intent brief**: every brief quotes it as `Task:`, and nothing else tells a hunter or verifier
what the change is for. Write 3-8 claims about what it must do, each with the line that makes it true (from the task,
commits, PR description and `git diff <base>`); a claim without a line is already a finding
(`craftsman review add --hunter lead --lens intent ...`). State intent, never argue that the code is right. Pass it with
`--task-file -` (4000 characters); a later `prepare` with new text replaces it.

`prepare` freezes the diff into round N and prints the packet path, `Depth:` and its reason, quality hints (`S#`),
unscanned files, the frozen scope, warnings and the **hunt plan**, one slot per line:
`W1-2  H2,H4-H7  contracts, failure, sweep  19 lines`. The diff alone decides the plan: a hunk's kind (prose, tests,
configuration, production) and risk signals set the lenses it owes, related lenses share a slot (about 400 changed lines
each), `craft` and `design` get their own, and a `sweep` joins when there are two or more bug slots. A follow-up round
of at most 40 changed lines with no deep signal is one bug slot (plus a simplicity slot). You cannot drop a slot.
`Enforced:` means the platform's hooks see the session and finishing needs every slot; `Advisory:` means no hook saw it,
the plan is a suggestion, the review is complete when every hunk is covered, and the report says `SELF-REVIEWED` (run
`craftsman doctor`).

Risk signals are a floor. If your reading calls for lenses the plan lacks (`trust` for shell, CI, credentials; `state`
for threads, locks, clocks; `platform` for OS, encoding, time zone), run `prepare --deep`: it adds slots, finished ones
stay finished. Review-only with unexpected files: do not widen the scope or `reset`; ask.

## 2. Launch the hunters

Launch **every slot `prepare` lists, together**, in parallel where the platform allows, each as a fresh-context
`craftsman-reviewer` subagent. The whole prompt is the line `prepare` prints, nothing added:

> You are Craftsman hunter W1-2. Run `craftsman review brief W1-2` and follow it exactly.

`brief` records that the slot was taken and prints the rest: task, base ref, slice, the lenses with the command that
loads their playbooks (`craftsman skill show craft-review --lenses <ids>`), and the `add` and `cover` commands with
`--hunter W1-2`. `prepare` prints each platform's launch mechanics under that line. A slot marked
`[Claude Code: model haiku]` gets `model: "haiku"`; other hunters keep the full model. Then **end your turn**: a
finished subagent wakes you. Never poll (`sleep`, timers, repeated `review show`). Only where the platform cannot launch
subagents at all, play the roles in turn (`brief` each slot), record with `--hunter`, and say **SELF-REVIEWED**.

## 3. Check, verify, repeat

When a subagent reports back, run the check `prepare` printed (`craftsman review check [--review-only]`) and do exactly
what it lists:
- **Slots** `not launched` or `stopped without finishing`: launch them as in step 2; a `running` slot is waited for. A
  hunter that dies twice: review-only, report `--partial`; when fixing, ask the user (gate session:
  `craftsman gate --await-user` first); say what is uncovered.
- **Findings** without a verdict: `check` names the clusters. Launch one `craftsman-verifier` per cluster, whole prompt
  `You are a Craftsman verifier. Run craftsman review brief --verifier F1,F2 and follow it exactly.` They merge
  duplicates, disprove, reproduce and record verdicts; in an enforced review one you record yourself does not count. A
  `blocker` or `major` dismissed without a `--run` needs a second, independent verifier. `check` marks a cluster of
  only `minor` or `nit` findings `[Claude Code: model haiku]` (as in step 2, `model: "haiku"`); any other cluster, and a
  verifier resumed in step 4, gets the full model.
- **Waves**: a `proven`, or serious `probable`, bug finding outside documentation makes `check` (also `report`, `brief`,
  `prepare`) store a wave: a **variant** slot per root cause (`W2-F3`, hunts its siblings, at most 3) and a **fresh**
  set (`W2-X1`, an unanchored look). A variant of a variant of a variant spawns nothing; the first deep round plans its
  fresh set up front. Launch them as in step 2. The hunt ends at the first wave that adds nothing.

Where agents run in the background (Claude Code, Antigravity), launch each verifier as soon as its hunters report
(Copilot's parallel subagents are a barrier). When `check` lists only fixes and runs, the hunt is saturated.

## 4. Fix, confirm, re-check, test (when asked to fix, and always in a gate session)

1. Fix each `proven`/`probable` finding at the root cause, with a regression test where the project has tests.
2. **Is it fixed?** `proven`: its regression test fails before the fix and passes after (`craftsman run`). `probable`:
   resume the verifier that judged it where the platform can (Claude Code: agent id; Antigravity: conversation id), send
   only the fix diff, ask "fixed or not, with evidence"; else a fresh verifier gets the finding plus the diff. Then
   `craftsman review resolve F# --as fixed --note "<what changed; the run or confirmation>"`.
   `--as accepted --note "<why; who decided>"` is for real trade-offs: never a `blocker`; a `major` only after the user
   agreed (gate session: `gate --await-user` / `--resume`).
3. **Did the fix break anything?** `craftsman review prepare` again (`--add-paths <csv>` for new files outside a frozen
   scope). The new round owes the changed hunks plus the code that references changed symbols; launch its slots (step 2)
   and verify what they record. Never reuse the finder or its verifier (anchored). Repeat until a round adds nothing
   confirmed; at `round-cap` let the user decide.
4. On the final code: `craftsman run --kind test -- <the project's test command>` (deep, or behavior unit tests do not
   reach: also `--kind e2e -- <real entry point>`). Earlier runs do not count; waive a never-passing command:
   `craftsman review waive-run R# --reason "..."`.

## 5. Report

`craftsman review check`, then `craftsman review report`; review-only uses `check --review-only` and
`report --review-only` (`--partial` when slots stayed uncovered), and plain `check`/`report` show BLOCKED there. Present
findings by severity (dismissed briefly) and your decisions in plain language: depth and why, slots and lenses, why each
wave ran or stopped, how findings were verified. If `craftsman` is not installed, say the review is ungated.
