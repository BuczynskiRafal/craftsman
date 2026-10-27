---
name: code-review
description: >-
  Evidence-driven code review of a local diff, branch, or pull request. Builds a
  review packet (hunks, enclosing scopes, callers, risk signals), examines every
  hunk through focused lenses, challenges each finding until it is proven,
  probable, or dismissed, and records everything in the Craftsman evidence
  ledger so the quality gate can check it. Use when /code-review or /review is
  invoked, or as the review step of /craftsman.
---

# Code Review (`/code-review`)

You review a change the way a careful senior engineer does when they are accountable for what
ships: you read the code around the change, you try concrete inputs, you check who else depends
on what changed, and you describe every problem as *"with this input, in this state, this line
does X instead of Y"*.

Everything you examine and conclude is written to the **evidence ledger** through the
`craftsman` CLI. The quality gate reads the ledger, not your prose. A review that is not in the
ledger did not happen.

## Search scope

Your workspace is the git work tree you were started in (`git rev-parse --show-toplevel`): search,
list, and read only inside it (prefer `git ls-files`, `git grep`, and editor search scoped to this
folder). Outside the work tree you may read only an exact file cited by a stack trace/error, a temp
directory you created, CI logs for this branch/PR, or Craftsman's own data via `craftsman …` CLI
commands. Never run recursive searches of `~`, `/`, or parent directories, and never open other
repositories, other projects' packets/ledgers/reports, or assistant session stores (for the full
contract run `craftsman skill show craftsman --file scope.md`).

## Arguments

`/code-review [target] [--tier quick|standard|deep] [--fix] [--comment]`

- **target** — nothing (working tree vs. the base recorded by `/craftsman`, or `HEAD` when no
  session is active), a branch or commit (`--base <ref>`), or a PR number/URL — check it out
  first (`gh pr checkout <n>`) and pass its merge base as `--base`; the packet and every finding
  are read from the files on disk.
- **tier** — how many lenses run (default: the active `/craftsman` tier, otherwise `standard`).
  `quick` for tiny, low-risk edits; `deep` for security-sensitive, concurrent, or core code.
- **--fix** — after findings are challenged, fix every `proven`/`probable` one and re-review the
  delta (Step 5).
- **--comment** — publish the final report on the PR with `gh pr comment`. Never add AI
  attribution of any kind.

## The five rules

1. **Read before you judge.** Never conclude anything about a hunk without having read its full
   enclosing function and the call sites the packet lists. The diff alone hides most bugs.
2. **Name the trigger.** Every finding names a local trigger (input, state, timing, platform)
   and the wrong outcome it causes. "Could be cleaner" or "might fail" is not a finding.
3. **Collect first, judge later.** In the lens passes (Step 2) record *every* candidate for which
   you can name a trigger — even if you have not checked whether some caller prevents it. Deciding
   reachability is the job of the challenge step (Step 3), never of Step 2. Recall comes from
   collecting, precision from challenging; mixing the two loses real defects.
4. **Evidence beats opinion.** When a finding can be demonstrated by running something (a
   one-off script that imports the real code, a failing test), run it through `craftsman run`
   and cite the run.
5. **Fixes get reviewed too.** Code written to fix a finding is new code; it goes through the
   lenses again (the ledger enforces this through diff fingerprints).

## Step 0 — Prepare the packet

```bash
craftsman review prepare [--tier <quick|standard|deep>] [--base <ref>] [--task "<what the change is for, one line>"]
```

This prints the absolute path of the current review packet (`packet-N.md`), and
`craftsman review view --lenses <lenses> [--hunks <range>]` writes and prints a per-reviewer view
(`packet-N.<slug>.md`). Read your view completely, the full packet only for a hunk outside your view
that you need (if you do not have the path, run `craftsman review show` or
`craftsman review show --packet-path` in the repository root; never search for packet or ledger
files, as packets, ledgers, and reports of other repositories are never relevant). It contains:
- the round number, diff fingerprint, and statistics;
- the task statement and commit messages (the *intent*);
- the project convention files to respect (`AGENTS.md`, `CLAUDE.md`, `GEMINI.md`,
  `.github/copilot-instructions.md`, …) — read them;
- the selected lenses and why each was selected;
- every hunk with its id (`H1`, `H2`, …), the enclosing scope, symbols it defines or changes,
  references to those symbols elsewhere in the repository, and its risk signals;
- hunks marked *carried* were already reviewed unchanged in an earlier round; skip them.

The packet is an index into the code, not a replacement for it: open the real files whenever you need
more context.

## Step 1 — Intent brief

Before hunting for defects, write down (in your working notes, 3–8 bullets) what the change must
achieve and which line achieves each part. This is the `intent` lens; it also tells every other
lens what "correct" and "intended" mean so reviewers do not flag requested behavior as a defect.

When `--fix` is set (or an active `/craftsman` gate session is open) and the project has a test
suite, run it once now through the ledger (`craftsman run --kind test -- <the project's test
command>`). In review-only mode (without `--fix`), skip running the full suite upfront so the
review stays fast and focused on the diff.

## Step 2 — Lens passes

The lens playbooks are in [`lenses.md`](lenses.md) (inlined at the end of this prompt on
single-file platforms, or readable via `craftsman skill show code-review --file lenses.md`; never
search the file system for `lenses.md`). Read the playbook of each lens before applying it.

For each selected lens, for each non-carried hunk:
1. apply every probe of the playbook, substituting concrete values;
2. record each candidate defect immediately — **one finding per root cause, listing all
   consequences in `--scenario`**. When recording more than one finding, **prefer the bulk JSON
   heredoc** so all findings are validated and written in one atomic call:
   ```bash
   craftsman review add --from-json - <<'EOF'
   [
     {
       "lens": "contracts",
       "file": "src/a.py",
       "line": 42,
       "severity": "major",
       "title": "<what is wrong, one line>",
       "scenario": "<root cause -> all wrong outcomes, concrete>",
       "suggestion": "<the fix you would make>"
     }
   ]
   EOF
   ```
   Or for a single finding:
   ```bash
   craftsman review add --lens <lens> --file <path> --line <n> --severity <blocker|major|minor|nit> --title "<what is wrong, one line>" --scenario "<root cause -> all wrong outcomes, concrete>" --suggestion "<the fix you would make>"
   ```
   Both forms echo the cited source line for every finding. If it is not the line you meant,
   correct it with `craftsman review move <id> --line <n>` (add `--file <path>` for another file) —
   never record the finding a second time. When one line or decision causes several downstream
   problems (for example breaking two callers, a config path and a test), anchor a single finding
   at the root cause and list every consequence in `--scenario` instead of opening separate
   findings for each symptom. If an earlier lens already recorded the same root cause, fold the new
   consequence into it before verification with
   `craftsman review merge <target_id> <dup_id> [--scenario "..."]` (`merge` only works on findings
   without a verdict):
   > Merge only when ONE fix removes every consequence. Different fixes are separate findings, even on the same line, file, or category.

   For example, ordering entries in `AUTHORS` and a missing changelog entry are two separate
   findings, not one "housekeeping" finding;
3. write the coverage note while you probe, then record it for groups of **1–4 hunks per entry**.
   For every hunk id give the inputs you tried and what the code does with them, naming the guard
   or line that makes it safe — "ok" or "checked" is not a note. When recording more than one
   coverage entry, **prefer the bulk JSON heredoc**:
   ```bash
   craftsman review cover --from-json - <<'EOF'
   [
     {
       "lens": "contracts",
       "hunks": ["H1", "H3"],
       "note": "H1 paginate: page=0 -> start=-20 (F2); size=None -> default 20 at catalog.py:14. H3 search_page: q='' rejected by validate_query at api.py:40 before the call"
     }
   ]
   EOF
   ```
   Or for a single entry:
   ```bash
   craftsman review cover --lens contracts --hunks H1,H3 --note "H1 paginate: page=0 -> start=-20 (F2); size=None -> default 20 at catalog.py:14. H3 search_page: q='' rejected by validate_query at api.py:40 before the call"
   ```

`cover` and `add` refuse to record against a round whose code has changed since it was prepared:
run `craftsman review prepare` again first (unchanged hunks carry their coverage over).

Record candidates generously in this step — a candidate that turns out wrong is dismissed in
Step 3 at low cost; a real defect you filtered out early is lost for good.

**Severity**
- `blocker` — wrong results, data loss, security hole, or crash on a path real users take.
- `major` — wrong behavior on a plausible path, a broken contract for some caller, or an error
  that is hidden instead of reported.
- `minor` — a real but low-impact edge case, a maintainability cost with a concrete future bug, or
  a name or structure that misleads the reader, with the misleading reading stated. Record naming
  and structure problems when you can state the wrong reading: what a reader would believe from the
  name, and what the code actually does. 'I don't like it' is not a finding.
- `nit` — only when a project convention file explicitly requires it.

**Use isolated reviewers when the platform offers them.** On tiers `standard` and `deep`:
- **Without `--fix` (review-only):** spawn **two `craftsman-reviewer` subagents in parallel**, each
  assigned half of the round's domain lenses:
  - reviewer 1: `{intent, contracts, ripple, tests, craft}`
  - reviewer 2: `{failure, state, trust, platform, model}`
  (omitting lenses not selected in the round), followed by **one `craftsman-verifier` subagent**
  that performs the `sweep` pass and duplicate grouping at the start of its run (Step 0) before
  recording verdicts.
- **With `--fix` or inside `/craftsman`:** hand the lens passes to fresh-context reviewer
  subagents (`craftsman-reviewer`), 1–3 lenses each in parallel, then run one more reviewer for
  the **final sweep** described at the end of `lenses.md`, followed by a separate
  `craftsman-verifier` subagent.
- **Keep subagent briefs lean:** generate a view per reviewer with
  `craftsman review view --lenses <lenses> [--hunks <range>]` and pass each reviewer subagent its
  packet view path (`packet-N.<slug>.md`), its assigned lenses, and the hunk list — never paste file
  contents into the prompt. Each reviewer reads your view completely, the full packet only for a
  hunk outside your view that you need. The verifier should read `craftsman review show` and the
  full packet first, then open source files only around the cited `file:line` ranges and callers
  rather than reading full files top to bottom.
- When no subagent mechanism exists, do the passes yourself one lens at a time, re-reading the
  packet at the start of each lens.

## Step 3 — Challenge every finding

For each finding without a verdict (`craftsman review show` lists them), switch roles: your goal
is now to prove the finding **wrong**. On tiers `standard` and `deep`, this step **always**
belongs to a separate `craftsman-verifier` subagent (even without `--fix`); do it inline yourself
only on `quick` or when the platform has no subagents.

0. **Final `sweep` pass first (without `--fix` only).** In review-only mode (without `--fix`),
   the verifier already reads the whole diff and all findings, so it runs the `sweep` lens at the
   start of its work before issuing any verdict: check for cross-hunk interactions (`sweep`
   playbook in `lenses.md`), record any `sweep` findings (`craftsman review add --lens sweep ...`),
   and record `sweep` coverage (`craftsman review cover --lens sweep ...`).
1. **Group duplicates by root cause first (before any verdict).** `craftsman review merge` only
   works on findings without a verdict, before verification. If two or more unjudged findings stem
   from the same root cause (the same changed line or decision producing multiple symptoms), fold
   the duplicates into the primary finding so one finding carries all consequences and the
   duplicates are dismissed:
   ```bash
   craftsman review merge F1 F4 F7 --scenario "<root cause -> (1) effect A; (2) effect B; (3) effect C>"
   ```
   > Merge only when ONE fix removes every consequence. Different fixes are separate findings, even on the same line, file, or category.

   For example, ordering entries in `AUTHORS` and a missing changelog entry are two separate
   findings, not one "housekeeping" finding. A finding that duplicates an already-judged one (for
   example in a later round, where `merge` no longer applies) is `dismissed` with a reference to
   the other finding's `file:line`.
2. **Look for a guard or unreachability.** Check callers, types, validation on load, or tests that
   make the trigger impossible, and check whether any real caller can produce the triggering input
   or state.
3. **Check PR intent (`--as question`, not a defect).** If a non-blocker finding complains about
   behavior that the PR task, description, commit message, reviewer comment, or documentation
   explicitly asked for (or questions an intentional design trade-off by the maintainer), mark it
   `--as question`, never `proven` or `probable` — even if a script can reproduce the behavior.
   `question` is forbidden for `blocker` findings (a blocker is a defect, not a design question);
   its `--evidence` must start with `intent:` (at least 20 characters after `intent:`) and contain
   both the source of intent and a `file:line` reference in the code.
4. **Never dismiss flakiness or platform dependence on a single green run.** A claim of
   nondeterminism (wall-clock boundary, race condition, unseeded randomness, test order) or
   platform/environment dependence (Windows paths, NTFS timestamp resolution, case-insensitive
   filesystem, another OS, locale, Python version) is **not** disproved by one passing run on the
   host machine. Dismiss it only if you can cite a deterministic guard in the code (`file:line`
   with the guard quoted) or a run that explicitly forces the boundary/platform condition and
   passes; otherwise record at most `--as probable --evidence "no-repro: <why>; <chain>"`.
5. **Reproduce or verify statically according to the mode:**
   - **Without `--fix` (review-only):** verify statically by default (`--as probable --evidence
     "static: <file:line chain>"`). Run `craftsman run --kind repro` only when the outcome does not
     follow unambiguously from the code alone (depends on data, call order, platform, clock, or
     library version) — **at most 3 `repro` runs across the entire review**, starting with
     `blocker` and `major` findings. Once the 3-run cap is reached, record the remaining findings
     with `static:`.
   - **With `--fix` or inside `/craftsman`:** no cap on `repro` runs. For importable code you must
     attempt a reproduction before choosing a verdict:
     ```bash
     craftsman run --kind repro -- python -c "from pkg.mod import f; print(f(0))"
     ```
     Use `static:` or `no-repro:` only when the code cannot be executed in this environment.
6. **Record the verdict** (when recording more than one verdict, **prefer the bulk JSON heredoc**):
   ```bash
   craftsman review verdict --from-json - <<'EOF'
   [
     {"id": "F1", "as": "probable", "evidence": "static: <file:line chain showing trigger -> wrong outcome>"},
     {"id": "F2", "as": "dismissed", "evidence": "<where exactly the chain breaks, file:line>"}
   ]
   EOF
   ```
   Or for a single finding:
   ```bash
   craftsman review verdict F3 --as proven    --run R4 --evidence "<what the run did and why it is wrong>"
   craftsman review verdict F3 --as probable  --evidence "static: <file:line chain showing trigger -> wrong outcome>"
   craftsman review verdict F3 --as probable  --evidence "no-repro: <why>; <the chain, step by step with file:line>"
   craftsman review verdict F3 --as question  --evidence "intent: <PR task/commit/comment/docs>; <file:line trade-off>"
   craftsman review verdict F3 --as dismissed --evidence "<where exactly the chain breaks, file:line>" [--run R4]
   ```

The ledger enforces the evidence: `proven` requires `--run` pointing to the reproduction run (`R#`);
`probable` evidence must start with `static:` (including a `file:line` reference) or `no-repro:`
(saying why it could not be verified deterministically, followed by the chain); `question` is not
allowed for `blocker` findings and its evidence must start with `intent:` naming the intent source
and citing `file:line`; `dismissed` needs either `--run` or a `file:line` reference to the guard
(quote the guard expression).

**Leave the change under review untouched.** Reproductions live in a fresh temporary directory
outside the repository. Never `git stash`, `checkout`, `reset` or edit files to compare with the
old behavior: read the old version with `git show <base>:<path>` (or copy it into the temporary
directory) instead. Delete caches your runs created inside the repository (`__pycache__`, …).

## Step 4 — Report (review-only mode)

Without `--fix`, finish with:

```bash
craftsman review check --review-only
craftsman review report --review-only
```

and present the report output. `--review-only` checks that the round is current, every selected
lens (including `sweep`) covered all non-carried hunks, and every finding has a verdict, without
requiring `proven`/`probable` findings to be resolved or a passing test run (reporting
`**Craftsman review: COMPLETE (review-only)**` / `Review: complete (review-only)`, or
`**Craftsman review: INCOMPLETE (N open items)**` while review steps remain open). Running
`craftsman review check` or `craftsman review report` *without* `--review-only` in review-only mode
is expected to show `BLOCKED` (because findings are left open for the author and the full test
suite was not run) — always pass `--review-only` here and do not guess other flags. Findings are
ordered by severity; dismissed ones are listed briefly so the reader sees what was considered.

## Step 5 — Fix and re-review (`--fix`, and always inside `/craftsman`)

1. Fix every `proven` and `probable` finding. Prefer the root cause over the symptom; add a
   regression test that fails without the fix whenever the project has tests.
2. Mark each one:
   ```bash
   craftsman review resolve F3 --as fixed --note "<what changed and which test covers it>"
   craftsman review resolve F5 --as accepted --note "<why it is acceptable; who decided>"
   ```
   `accepted` is for real trade-offs only and is shown to the user in the final report;
   blockers cannot be accepted, and a `major` finding is accepted only after the user agreed.
3. Run `craftsman review prepare` again. The ledger detects which hunks changed since the last
   round; only those need the lenses again. Repeat Steps 2–5 for the delta until a round produces
   no new `proven`/`probable` findings.
4. Run the project's checks through the ledger (see `/verify`), then:
   ```bash
   craftsman review check
   ```
   It exits non-zero and prints the exact next actions while anything is missing.

## Platform notes

- **Subagents.** GitHub Copilot (VS Code): use the `craftsman-reviewer` / `craftsman-verifier`
  custom agents as subagents. Claude Code: the `Task`/`Agent` tool with those agent names.
  Antigravity: `invoke_subagent` with `TypeName` `self` and `Role` `craftsman-reviewer` or
  `craftsman-verifier`. Copilot CLI: `/agent` or the task tool. Codex and others:
  run the lenses yourself, one at a time.
- **Shell.** All `craftsman` commands are identical in bash, zsh, PowerShell and cmd. Keep each
  on one line (no `\` continuations). When a note, scenario or evidence quotes code containing `$`
  or backticks, use single quotes so the shell does not expand them. Use the project's Python
  (`python`, or the virtualenv's); `python3` does not exist on many Windows machines.
- **No CLI available?** If `craftsman` is not installed, say so explicitly in the report — the
  review then has no gate and must not be presented as gated.
