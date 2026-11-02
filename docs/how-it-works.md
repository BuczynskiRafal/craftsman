# How Craftsman Works

Craftsman is an evidence-gated engineering loop for AI coding agents. After implementing a change,
the agent simplifies it, hands it to reviewers with a fresh context, challenges and disproves findings,
fixes confirmed defects, reviews the fixes, and tests the code. The gate blocks completion until the
evidence ledger proves every required step passed on the final code.

```
/craftsman <task>          prompt hook (or `craftsman gate --init`) records the base commit
        │
        ▼
   implement               /feature-dev, /modernize, /batch, or a direct fix
        │
        ▼
   /simplify
        │
        ▼
craftsman review prepare   packet-N.md: hunks, enclosing scopes, call sites,
        │                  risk signals, lenses selected for the round
        ▼
   lens passes             craftsman-reviewer subagents with a fresh context
        │                  record coverage and findings
        ▼
   challenge               craftsman-verifier tries to disprove each finding and
        │                  reproduces the ones that hold up: proven / probable / dismissed
        ▼
   fix + resolve  ──►  code changed?  ──►  next round reviews only the changes
        │
        ▼
craftsman run --kind test|e2e -- …   runs recorded with exit codes
        │
        ▼
craftsman review check  →  stop hook lets the agent finish  →  craftsman review report
```

---

## Evidence Ledger

![Ledger: diff fingerprint, hunk context, and delta carry-over](diagrams/evidence-ledger-flow.png)

Everything goes into `.git/craftsman/ledger.json`, stored separately for each worktree and outside commits.

| Record | Written by | Gate requirement |
| :--- | :--- | :--- |
| **Round**: diff fingerprint, hunks, selected lenses | `review prepare` | The latest round matches the current code |
| **Coverage**: lens × hunk, with a note on what was checked in each hunk | `review cover` (or `--from-json`) | Every selected lens covers every hunk of the round |
| **Finding**: location, severity, root cause → outcomes | `review add` (or `--from-json`). `review move` corrects line numbers. `review merge` merges unjudged duplicates with a shared root cause | Every finding has a verdict |
| **Verdict**: `proven` (reproduction `--run R#`), `probable` (`static: <file:line chain>` or `no-repro: <why>; <chain>`), `question` (`intent: <source>; <file:line>`, non-blocking findings only), `dismissed` (run or `file:line`) | `review verdict` (or `--from-json`) | `proven` and `probable` findings are resolved. With `--review-only`, they go into the report as warnings |
| **Resolution**: `fixed` (only after a code change) or `accepted` (never for blocking bugs) | `review resolve` | All blocking findings are resolved |
| **Run**: argv, working directory, exit code, output tail, diff fingerprint | `craftsman run` | A passing test run (and e2e in `deep`) on the final code that did not modify files. The last run of every command succeeds unless retired with `review waive-run` and a user-visible reason |
| **Agent event**: reviewer subagent start and stop | platform hooks | In `standard` and `deep`: an independent reviewer finished after the round was prepared |

Every record is tied to a diff fingerprint, so any code change invalidates the evidence. The next round reviews
the changed hunks and any hunks that reference a changed symbol. Tests must be run again. Generated files
(`__pycache__`, build caches, `node_modules`, …) are skipped automatically, and custom patterns go in `.craftsmanignore`.

---

## What the Gate Checks and What It Does Not

The gate enforces **review and verification evidence** plus **mechanical code-quality signals**:
every hunk and changed file is covered with a concrete note (naming inputs, callers, or `file:line` guards
rather than generic praise), every mechanical signal (`long-function`, `large-module`, `diff-size`, `test-name`,
`duplicate-logic`, `dict-domain`, `parse-error`) is either resolved or explicitly acknowledged with a code
reason shown in the report, oversized changes (`> 800` added production lines) are handed to the user as
`needs_human`, and reviews without platform or transcript isolation evidence are flagged as `SELF-REVIEWED`
(or `needs_human` in `deep` mode).

The gate **does not** assess business-requirement fit, naming quality beyond the mechanical test-name rules,
or architecture outside the files touched by the diff — those remain the responsibility of the human reviewer
and are listed explicitly under `Not assessed by the gate` in every report.

---

## Review Lenses

![Two-phase review: lens matrix, verification, and verdict lifecycle](diagrams/review-protocol-and-verdicts.png)

A lens is a focused review angle with a list of specific checks. The review packet selects them by gear and
by the risk signals detected in the diff.

| Lens | What it looks for | When |
| :--- | :--- | :--- |
| `intent` | Missing pieces, incomplete change propagation, unrequested changes, fixes in the wrong layer | Always |
| `contracts` | Input domains, boundary conditions, truthiness, wrong variable, inconsistent return values | Always |
| `failure` | Swallowed errors, partial side effects, retries, fail-open guards | Always (except `docs-only` changes) |
| `craft` | Code reuse, needless complexity, misleading names and comments, wasted work | `standard`+ and `docs-only` changes |
| `design` | Overall design and size of the change, what can be simplified or removed (file-level `--files` coverage) | `standard`+ |
| `ripple` | Broken callers, removed guarantees, overridden methods, persisted data compatibility | `standard`+ or deletions and config |
| `tests` | Tests that would pass even if the change were wrong | `standard`+ or test files |
| `state` | Aliasing, caches, check-then-act races, `async` bugs, clocks | `deep` or concurrency and time signals |
| `trust` | Untrusted data in shells, SQL, paths, deserializers; authorization | `deep` or process, SQL, deserialization, auth signals (in `standard` also network and regex) |
| `platform` | Paths, encodings, shells, versions across operating systems | `deep` or platform and time signals |
| `model` | Invalid states allowed by the data model | `deep` or new types |
| `sweep` | Interactions between hunks, at the end | `standard`+ |

---

## Gears

| Gear | Trigger | Review | Verification |
| :--- | :--- | :--- | :--- |
| `quick` | `--quick`, automatically in `/craftsman` for ≤ 40 changed lines in ≤ 2 files, and **by default in standalone `/code-review` (review-only) up to 250 changed lines** (plus `docs-only` changes; skipped binary/oversized files escalate to `standard`) | `intent`, `contracts`, `failure` (+ `trust` on security signals; `intent` and `craft` for `docs-only`), single inline agent (0 subagents), multi-lens `--files` coverage, no `sweep` | In `/craftsman`: passing tests; in review-only: inline static verification only when findings exist (max 3 reproductions) |
| `standard` | Default in `/craftsman`, and above 250 changed lines in review-only | Always-on + narrower signal-triggered lenses in 1–2 `craftsman-reviewer` subagents or shards (`review view --shard K/N`, `--parallel` limit 2 by default), `sweep` (inline when 0 findings in review-only), `craftsman-verifier` only when findings exist | In `/craftsman`: passing tests on the final code |
| `deep` | `--deep` / `--tier deep` or the `ultracraft` keyword | All 12 lenses (shards or `R1`–`R4` groups), `sweep`, `craftsman-verifier` when findings exist | Passing tests **and** e2e |

To mention `/craftsman` or `ultracraft` in a prompt without triggering the gate, put the keyword in backticks or quotes.

---

## Platform Enforcement

![Lifecycle hooks, gate state machine, and runtime guards](diagrams/lifecycle-hooks-and-guards.png)

The protocol, lenses, subagents, and ledger are shared across all platforms. What differs is the mechanism
that blocks completion.

| Platform | Stop gate | Reviewer isolation | Subagents |
| :--- | :--- | :--- | :--- |
| GitHub Copilot in VS Code | `Stop` hook blocks until `review check` passes | `SubagentStart`/`SubagentStop` hooks | `craftsman-reviewer`, `craftsman-verifier` agents |
| GitHub Copilot CLI | `agentStop` hook | `subagentStart`/`subagentStop` hooks | The same agents |
| Claude Code | `Stop` hook | `SubagentStart`/`SubagentStop` hooks | The same agents in `~/.claude/agents` |
| Google Antigravity | `Stop` hook | Finished `craftsman-reviewer` subagents in the transcript | `invoke_subagent` (`TypeName: self`, `Role: craftsman-reviewer`) |
| Codex, Cursor, and others | `craftsman gate --complete` refuses completion until the ledger passes validation | None | None |

The gate runs only in a `/craftsman` session; outside it, the hooks do nothing. After six forced continuations
in a row without fewer open issues, the stop hook lets the agent through and informs the user that the gate
was not satisfied. When a hook fails, the agent continues with a warning that the gate was not checked;
a failing `pre-tool` check asks the user to confirm the tool call instead (Copilot denies it).
