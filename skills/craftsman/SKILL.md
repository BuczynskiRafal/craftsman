---
name: craftsman
description: >-
  End-to-end engineering loop with an evidence-based quality gate. Captures a
  baseline, implements the task, then iterates simplify -> lens review in fresh
  contexts -> adversarial verification -> fix -> delta re-review -> runtime
  verification until the Craftsman ledger proves the change is reviewed,
  verified and clean. Gears: --quick, --standard (default), --deep. Use when
  /craftsman is invoked or the prompt contains `ultracraft`.
---

# Craftsman (`/craftsman [--quick | --standard | --deep] <task>`)

You own this change from request to a verified, reviewed diff that is ready to commit. You are
done when `craftsman review check` passes — not when the code compiles, and not when you feel
confident. The gate is enforced by lifecycle hooks where the platform supports them: if you try
to finish early, you will be sent back with the list of what is missing.

Talk to the user in their language; keep code, identifiers and commit messages in American
English.

**How to run the pieces.** A `/name` below means: invoke `/name` as a skill or slash command if
your platform offers it; otherwise run `craftsman skill show <name>` and follow its output. Never
search the file system for skill files.
Subagents are the `craftsman-reviewer` and `craftsman-verifier` agents: custom agents in VS Code
Copilot, `/agent` or the task tool in the Copilot CLI, the Agent/Task tool in Claude Code,
`invoke_subagent` in Antigravity (`TypeName` `self`, `Role` `craftsman-reviewer` or
`craftsman-verifier`; only the `craftsman-reviewer` role counts as an isolated review). Without
any subagent mechanism, play each role yourself, one at a time. Write every `craftsman` command
on one line (no `\` continuations, which PowerShell and cmd do not understand).

## Search scope

- **Workspace boundary:** Your workspace is the git work tree you were started in (`git rev-parse --show-toplevel`). Search, list, and read only inside it: prefer `git ls-files`, `git grep`, and the editor's search tools scoped to this folder (in a multi-root workspace, always pass an include pattern for this folder).
- **Permitted reads outside the work tree:** Outside the work tree you may read only: (a) the exact file a stack trace, import, or build error points to (for example, an installed dependency's source), (b) a directory inside the system temp folder that you created for this task, (c) Craftsman's own data via `craftsman …` CLI commands (`craftsman skill show`, `craftsman review show`, `craftsman status` — never by opening `.git/craftsman` files of any repository by hand except the exact packet path `craftsman review prepare` or `craftsman review show` prints for this repository), and (d) external evidence about *this* repository, branch, or commit — such as CI (Jenkins, GitHub Actions) results and logs for the current branch or PR — when the task needs it.
- **Forbidden searches and reads:** Never run recursive searches of the home directory, the file-system root, a drive root, or any parent directory of the work tree (`find ~`, `find /`, `grep -r … ~`, `rg … ..`, `Get-ChildItem -Recurse C:\`, `mdfind`, `locate`, `tree ~`). Never open other repositories, other projects' reports, packets, or ledgers, or any assistant's session store (`~/.copilot/session-state`, `~/.claude/projects`, `~/.gemini/antigravity/brain`, `~/.gemini/antigravity/conversations`).
- **Ask when in doubt:** If you believe a file outside the work tree is needed, stop and ask the user for the exact path before reading it.
- **Focused reading:** Read what the task needs: the hunks, their enclosing scopes, their callers and tests, and the project's convention files. Do not read unrelated history, old reports, or documentation that the change does not touch.

## Gears

| Gear | When | Review | Verification |
| :--- | :--- | :--- | :--- |
| `quick` | `--quick`, or automatic for ≤ 40 changed lines in ≤ 2 files | lenses `intent`, `contracts`, `craft` (+ any triggered by risk signals), done by you inline | one passing test run |
| `standard` | default | all always-on lenses + triggered ones, done by fresh-context reviewer subagents, plus a final sweep and a verifier | passing test run at the final diff |
| `deep` | `--deep` or `ultracraft` | every lens, reviewers + sweep + verifier | passing tests **and** an end-to-end run at the final diff |

`auto` gear upgrades itself: once the diff outgrows the quick limits, the gate demands the
standard workflow.

## Stage 0 — Baseline

```bash
craftsman gate --init --tier <auto|quick|standard|deep> --task "<the user's request in one line>"
```

This records the base commit; every later review covers the whole cumulative diff since this
point, including commits you make along the way. Then look at `git status` and the files the
task touches. (When the user asks only for a review without `--fix`, `/craftsman`, or `ultracraft`,
do not run `craftsman gate --init`; invoke `/code-review` as a skill if your platform offers it, or
run `craftsman skill show code-review` and follow its output, in review-only mode and
finish with `craftsman review check --review-only` and `craftsman review report --review-only`.)

For `deep` / `ultracraft`: before writing code, write a short design note in chat — invariants
that must hold, edge cases, two candidate approaches with trade-offs, the chosen one, and the
three most likely ways it could fail in production.

## Stage 1 — Implement

Pick the track that fits the task, follow it, then go to Stage 2:
- **New feature:** `/feature-dev` (understand the codebase -> clarify -> design -> build).
- **Legacy change or deep refactor:** `/modernize` (pin current behavior with tests first).
- **Mechanical change across many files:** `/batch`.
- **Small fix or polishing existing changes:** go straight to Stage 2.

When the task is ambiguous in a way that changes what you build, ask the user before building.
While waiting for an answer, run `craftsman gate --await-user` so the gate does not push you on;
run `craftsman gate --resume` when the answer arrives.

## Stage 2 — The quality loop

### 2.1 Simplify first
Run `/simplify` on the cumulative diff so reviewers see the final shape of the code, not a draft.
If its baseline test run fails because of what you built in Stage 1, fix that first.

### 2.2 Prepare a review round
```bash
craftsman review prepare
```
(`--tier <quick|standard|deep>` only to raise the tier; it never goes below the gate's.) Read the
printed packet completely, and the convention files it lists. Its header shows the tier this round
resolved to and the lenses it needs. Then write the **intent brief** of `/code-review` Step 1 —
3–8 bullets: what the change must achieve and which hunk achieves each part.

### 2.3 Review through lenses
- **quick:** apply the selected lenses yourself, one at a time, `sweep` last if selected.
- **standard / deep — two waves, never inline while subagents exist** (your context is full of
  the reasoning that produced the code, which is exactly what hides its defects):
  1. *Wave 1, in parallel:* `craftsman-reviewer` subagents assigned by fixed reviewer groups
     (`REVIEWER_GROUPS` in `craftsman/lenses.py`), omitting unselected lenses and skipping empty
     groups:
     - **R1** (callers & contracts): `intent`, `contracts`, `ripple`, `tests`
     - **R2** (failure paths & state): `failure`, `state`, `trust`, `model`
     - **R3** (environment & clarity): `platform`, `craft`
     - **R4** (whole-change design): `design`
     Build a packet view for each active reviewer group with
     `craftsman review view --lenses <group-or-lenses> [--hunks <range>]` and give each its packet
     view path, the intent brief and its lenses (each reviewer reads your view completely, the full
     packet only for a hunk outside your view that you need). On a small fix round (one or two open
     hunks) one reviewer may take every non-sweep lens.
  2. *Wave 2, after wave 1 has finished:* one more `craftsman-reviewer` for lens `sweep` only,
     followed by `craftsman-verifier`.

### 2.4 Challenge findings
- **standard / deep:** a `craftsman-verifier` subagent gives every finding a verdict — always, even
  for one finding. Give it the packet path and the intent brief.
- **quick:** do `/code-review` Step 3 yourself.

### 2.5 Fix and loop
For every `proven` and `probable` finding:
- **fix it at the root cause** (with a regression test where the project has tests), then
  `craftsman review resolve <id> --as fixed --note "<what changed; which test covers it>"`;
- or, for a real trade-off, `--as accepted --note "<why; who decided>"` — never for a blocker; for a
  `major`, ask the user first (`craftsman gate --await-user`, ask, `craftsman gate --resume`).

Go back to **2.2**: the new round contains only the hunks your fixes changed (plus anything that
mentions a symbol you changed). Repeat until a round ends with no new `proven`/`probable` findings.
If `craftsman review check` reports `round-cap`, stop iterating: report what remains to the user
and let them decide — the gate lets you finish in that state.

### 2.6 Verify at runtime
Follow `/verify`, through the ledger, at the final state of the code:
- every gear: `craftsman run --kind test -- <the project's test command>`;
- `deep`, and any change whose behavior unit tests do not reach (a CLI, a server route, a
  migration): also `craftsman run --kind e2e -- <command that exercises the real entry point>`.

Runs before the last code change do not count. A failing command blocks until the same command
passes; if it can never pass again (a typo, a test you renamed), retire it with
`craftsman review waive-run <run id> --reason "<why it no longer applies>"` — the user sees it.

### 2.7 Close the gate
```bash
craftsman review check
craftsman gate --complete
```
`--complete` refuses to close while `check` reports anything. Do what it lists, then try again.

## Stage 3 — Report

Run `craftsman review report` and give the user:
- what was built (files and one line each);
- the gear, number of review rounds, lenses applied;
- findings fixed, findings accepted (with reasons), findings dismissed (count);
- the verification commands and their results;
- a proposed commit message from `/high-level-commit-message`.

Never commit, push or open a pull request unless the user asked for it; never push to
`main`/`master` without asking; never add AI attribution to commits or PRs.
