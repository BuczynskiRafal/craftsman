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

`/code-review [target] [--tier quick|standard|deep] [--staged | --tracked | --paths <csv>] [--parallel <N>] [--fix] [--comment]`

- **target** — nothing (working tree vs. the base recorded by `/craftsman`, or `HEAD` when no
  session is active), a branch or commit (`--base <ref>`), or a PR number/URL — check it out
  first (`gh pr checkout <n>`) and pass its merge base as `--base`; the packet and every finding
  are read from the files on disk.
- **tier** — how many lenses and subagents run. With an active `/craftsman` gate session, defaults
  to the gate's tier. In standalone review-only mode (no active gate), defaults to **`quick` up to
  250 changed lines** (and for docs-only changes; binary or oversized skipped files force
  `standard`) and **`standard` above 250 changed lines**; **`deep` runs only when explicitly
  requested** (`--tier deep`). If a later `--fix` round grows past 250 changed lines, `prepare`
  promotes the tier to `standard` and never downgrades it.
- **--staged / --tracked / --paths `<csv>`** — select the initial review-only file scope on round 1
  (mutually exclusive, passed straight through to `craftsman review prepare`; forbidden when an
  active `/craftsman` gate is open, because the gate reviews the whole work tree):
  - `--staged` — freeze the scope to files currently staged against `--base` (file content is still
    read from the work tree; `--staged` selects which files enter the review);
  - `--tracked` — freeze the scope to tracked files modified against `--base` (ignoring untracked
    files);
  - `--paths a.py,src/b/` — freeze the scope to changed files matching the given file paths or
    directory prefixes (every entry must match at least one changed file; `prepare` pins the
    concrete matched file paths, never directory wildcards).
  Without these flags, round 1 freezes the scope to the files changed at that moment (tracked and
  untracked).
- **--parallel `<N>`** — maximum number of subagents running simultaneously on `standard` and
  `deep` (default `2`; queue any remaining shards or reviewer groups).
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
craftsman review prepare [--tier <quick|standard|deep>] [--base <ref>] [--staged | --tracked | --paths <csv> | --add-paths <csv>] [--task "<what the change is for, one line>"]
```

This prints:
- the absolute path of the current review packet (`packet-N.md` — capped at `40k` chars without
  inlined scope bodies on `quick`, and `120k` chars on `standard`/`deep`; if a large diff exceeds
  budget, `## Omitted hunk bodies` lists hunks whose diff bodies were replaced with explicit
  `open <path>` or `git diff <base7> -- <path>` markers while preserving every hunk header);
- the resolved tier with its reason (e.g. `Tier quick: 120 changed lines <= 250`);
- in review-only mode, `Scope: N file(s), frozen for this review.` (and `Ignored (outside the
  review scope): ...` when unrelated files exist outside the pinned scope);
- on `standard` and `deep`, `Suggested reviewer shards: N`.

On `standard` and `deep`, `craftsman review view --lenses <lenses> [--shard K/N] [--hunks <range>]`
writes and prints a bounded per-reviewer view (`packet-N.<lenses>.shard-K-of-N.md` or
`packet-N.<slug>.md`, capped at `60k` chars; shard headers include `Files in this shard: ...`).
Never search the file system for packet or ledger files (`craftsman review show` and
`craftsman review show --packet-path` print the active paths for this repository).

The packet lists:
- the round number, diff fingerprint, and statistics;
- the task statement and commit messages (the *intent*);
- the project convention files to respect (`AGENTS.md`, `CLAUDE.md`, `GEMINI.md`,
  `.github/copilot-instructions.md`, …) — read them;
- the selected lenses and why each was selected, plus any mechanical `Quality signals` (`S1..Sn`);
- every hunk with its id (`H1`, `H2`, …), enclosing scope (line-range pointer on `quick`, trimmed
  body on `standard`/`deep`), defined symbols, caller references, and risk signals;
- hunks marked *carried* were already reviewed unchanged in an earlier round; skip them.

The packet is an index into the code, not a replacement for it: open the real files whenever you
need more context.

**Frozen scope and stale rounds.** In review-only mode, round 1 pins the exact list of changed
files (including both paths of any rename) for the ledger; later rounds can only keep that scope or
extend it explicitly with `--add-paths <csv>` when `--fix` touches additional files (Step 5).
Unrelated files created later outside that list (for example by another agent session in the same
checkout) are ignored and do not invalidate an active round. If `cover`, `add`, or `view` reports
that the round is stale (because a file *inside* the scope changed), run `craftsman review prepare`
**once** (unchanged hunks carry their coverage over). If the new round contains files you did not
expect, or if `prepare` prints `Ignored (outside the review scope): ...` (including `Hint: round N
is already complete; run craftsman review reset ...` from an earlier finished review), do **not**
widen the scope during a review-only run and do **not** run `craftsman review reset` on your own —
stop and ask the user how to proceed.

**Never mutate the work tree or index under review.** Never run `git stash`, `checkout`, `switch`,
`reset`, `restore`, or `clean` on the repository (read old versions with `git show <base>:<path>`
and keep reproductions in a temp directory outside the repository). On platforms with `PreToolUse`
hooks (Claude Code, VS Code Copilot, Copilot CLI), Craftsman's pre-tool hook enforces this guard —
denying worktree/index mutations for `craftsman-reviewer` and `craftsman-verifier`, and asking the
user for confirmation in the main session while an open review-only round is in progress. On
platforms without `PreToolUse` hooks (such as Codex or `agy -p`), this rule in the skill is the
only guard.

## Step 1 — Intent brief

Before hunting for defects, write down (in your working notes, 3–8 bullets) what the change must
achieve and which line achieves each part. This is the `intent` lens; it also tells every other
lens what "correct" and "intended" mean so reviewers do not flag requested behavior as a defect.

When `--fix` is set (or an active `/craftsman` gate session is open) and the project has a test
suite, run it once now through the ledger (`craftsman run --kind test -- <the project's test
command>`). In review-only mode (without `--fix`), skip running the full suite upfront so the
review stays fast and focused on the diff.

## Step 2 — Lens passes

Load the playbooks **only for the lenses you are applying** via:

```bash
craftsman skill show code-review --lenses <comma-separated-lenses-or-groups>
```

(for example `craftsman skill show code-review --lenses intent,contracts,failure` or `--lenses R1`;
never search the file system for `lenses.md`). Read each selected lens's playbook before applying
its probes.

**Recording findings (all tiers).** Record each candidate defect as soon as you find it — **one
finding per root cause, listing all consequences in `--scenario`**. When recording more than one
finding, prefer a JSON heredoc batch:

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

Both forms echo the cited source line. If it is not the line you meant, fix it with
`craftsman review move <id> --line <n>` (add `--file <path>` for another file) — never record the
finding twice. When one changed line or decision causes multiple downstream problems, anchor one
finding at the root cause and list every consequence in `--scenario`. If an earlier lens already
recorded the same root cause, fold the new consequence into it before verification with
`craftsman review merge <target_id> <dup_id> [--scenario "..."]` (`merge` works only on findings
without a verdict):
> Merge only when ONE fix removes every consequence. Different fixes are separate findings, even on the same line, file, or category.

For example, a CLI command missing a documented `--format` option and a stale README example are
two separate findings, not one "housekeeping" finding.

**Mechanical quality signals (`S1..Sn`, all tiers).** If the packet contains a `Quality signals`
section, every signal `S#` must be closed before `craftsman review check` / `check --review-only`
can pass: either link a finding to it (`craftsman review add --signal S# ...`, and give that
finding a verdict) or acknowledge it with a concrete code reason (`craftsman review ack S# --reason
"<>= 40 chars concrete code reason>"`; `diff-size` cannot be acknowledged by the agent).

**Severity**
- `blocker` — wrong results, data loss, security hole, or crash on a path real users take.
- `major` — wrong behavior on a plausible path, a broken contract for some caller, or an error
  that is hidden instead of reported.
- `minor` — a real but low-impact edge case, a maintainability cost with a concrete future bug, or
  a name or structure that misleads the reader, with the misleading reading stated. Record naming
  and structure problems when you can state the wrong reading: what a reader would believe from the
  name, and what the code actually does. 'I don't like it' is not a finding.
- `nit` — only when a project convention file explicitly requires it.

### `quick` tier (default "review my changes" path up to 250 changed lines)

- **Single agent, inline, zero subagents.** Do the entire review yourself in the main context.
- **Lenses:** `intent`, `contracts`, `failure` (plus `trust` when security risk signals `sql`,
  `deserialization`, `auth`, or `process` fire; `intent` and `craft` for docs-only diffs). Load
  only the round's selected playbooks:
  `craftsman skill show code-review --lenses <round-lenses>`.
- **Read enclosing scopes in the real files:** `quick` packets omit scope bodies (`Scope: <name>
  (<kind>, L<start>–L<end>)`); open those line ranges and the listed callers directly.
- **Single multi-lens `--files` coverage entry per batch of 1–4 files:** in `quick`, every lens
  accepts `--files`, so record all selected lenses for up to 4 changed files in one call:
  ```bash
  craftsman review cover --lens intent,contracts,failure --files src/a.py,src/b.py --note "src/a.py: paginate(page=0) -> ValueError at L18, size=None -> DEFAULT_SIZE at L22, callers api.py:44 ok. src/b.py: load_items maps OSError -> ConfigError at L31 without partial state, callers cli.py:19 ok"
  ```
  (or `cover --from-json` with `"lenses": ["intent", "contracts", "failure"]` and `"files": [...]`).
  Name each file (full path, or basename when unique in the round) with a concrete code-detail
  segment of **>= 80 characters per file** (20 concrete) whenever 2 or more lenses are combined
  (`>= 40` chars for a single lens).
- **No `sweep` lens in `quick`:** `sweep` is not selected on `quick` — never call
  `craftsman review cover --lens sweep`.
- **Close any `Quality signals` (`S1..Sn`)** via `review ack S# --reason "..."` or
  `review add --signal S# ...` + verdict.
- **Verification on `quick`:** if you recorded **0 findings**, skip Step 3 completely and finish at
  Step 4 (or Step 5 / `/verify` inside `/craftsman`). If you recorded findings (typically 1–3),
  verify them inline in Step 3 (static verification by default in review-only mode, at most 3
  `craftsman run --kind repro` runs across the review).

### `standard` and `deep` tiers (fresh-context subagents and shards)

When the platform offers subagents, delegate the lens passes to fresh-context `craftsman-reviewer`
subagents (when no subagent mechanism exists, run the lenses yourself, loading playbooks via
`craftsman skill show code-review --lenses <lenses>`):

- **Shards vs. reviewer groups (check `Suggested reviewer shards: N` from `prepare`):**
  - **When `N > 1` (`standard` or `deep`):** assign `N` `craftsman-reviewer` subagents to **shards**,
    **not** to `R1`–`R4` groups. Build each shard view with:
    ```bash
    craftsman review view --shard K/N --lenses <all-selected-lenses-except-sweep>
    ```
    for `K = 1..N`. Files are never split across shards; each shard header lists
    `Files in this shard: a.py, b.py` so the reviewer covers both the shard's hunks (for hunk
    lenses) and those files (for `design`).
  - **When `N = 1` on `standard`:** use **1** `craftsman-reviewer` for all selected non-`sweep`
    lenses (`craftsman review view --shard 1/1 --lenses <non-sweep-lenses>`). If a single large
    file pushes the packet past `60k` chars (`MAX_VIEW_CHARS`) while `prepare` still suggests `1`
    shard, split the selected non-`sweep` lenses across **2** `craftsman-reviewer` subagents by
    lens groups (e.g. `R1,R3` and `R2,R4`).
  - **When `N = 1` on `deep`:** assign `craftsman-reviewer` subagents by the fixed reviewer groups
    (`REVIEWER_GROUPS` in `craftsman/lenses.py`), omitting unselected lenses and skipping empty
    groups (`craftsman review view --lenses R1`, etc.):
    - **R1** (callers & contracts): `intent`, `contracts`, `ripple`, `tests`
    - **R2** (failure paths & state): `failure`, `state`, `trust`, `model`
    - **R3** (environment & clarity): `platform`, `craft`
    - **R4** (whole-change design): `design`
- **Keep subagent briefs lean:** pass each reviewer only its view/shard path, the intent brief, and
  its assigned lenses — never paste file contents into the prompt. Each reviewer loads only its own
  playbooks (`craftsman skill show code-review --lenses <assigned-lenses>`) and reads **only** its
  view/shard (never the full packet).
- **Multi-lens, incremental coverage:** on `standard` and `deep`, hunk lenses cover `--hunks` while
  `design` covers `--files` (hunk targets and file targets cannot be mixed in one entry, and `sweep`
  cannot be combined with other lenses). Combine all assigned hunk lenses onto the same **1–4
  hunks** in one `"lenses": [...]` entry (never emit separate single-lens entries for the same
  hunks; `"lens"` is only for single-lens passes like `design` or `sweep`). Combining `>= 2` lenses
  requires ~2x longer concrete detail per hunk (**`>= 80` characters per hunk, 20 concrete**;
  `>= 40` chars for a single lens), and `design` is recorded separately on `--files`:
  ```bash
  craftsman review cover --from-json - <<'EOF'
  [
    {
      "lenses": ["intent", "contracts", "failure"],
      "hunks": ["H1", "H3"],
      "note": "H1 paginate: page=0 -> start=-20 (F2), size=None -> default 20 at catalog.py:14, OSError propagated at L29. H3 search_page: q='' rejected by validate_query at api.py:40 before query_db, callers routes.py:18 ok"
    }
  ]
  EOF
  ```
  **Flush incrementally (1–4 hunks per entry) and fix rejected batches at once:** call
  `cover --from-json` and `add --from-json` right after every **1–4 hunks** (or every **2–3
  lenses**). Never hoard dozens of entries into one giant end-of-turn batch, where an output-token
  limit or a late typo rejects the whole batch. When a `--from-json` batch is rejected, nothing in
  it was recorded:
  - A rejection lists every entry that needs fixing. Fix all of them in one edit, resend once. Never
    rerun unchanged input.
  - If the same entry is rejected twice, rewrite its note from scratch: every hunk id of the entry,
    each with a concrete detail (`file:line`, value, `->`, symbol).
- **Concurrency cap (`--parallel`, default `2`):** run at most `--parallel` subagents at the same
  time (default `2`), queuing any remaining shards or groups until a running subagent finishes.
- **Subagent failure (rate limit, quota, timeout):** **never enter respawn loops**. Retry a failed
  subagent **at most once, sequentially**. If that retry also fails:
  - in review-only mode (without `--fix` and with no active gate), stop spawning subagents, run
    `craftsman review report --review-only --partial`, and tell the user clearly which lenses/hunks
    were not covered and which findings remain unverified;
  - with `--fix` or inside an active `/craftsman` gate, run `craftsman gate --await-user`, report
    what failed to the user, and wait for their instruction.

## Step 3 — Challenge findings and run final `sweep`

Spawn `craftsman-verifier` **only when there is at least 1 finding without a verdict**
(`craftsman review show` lists unjudged findings):

- **`quick`:** no `sweep` lens. With **0 findings**, skip Step 3 completely. With **>= 1 finding**,
  verify inline yourself using rules 1–6 below.
- **Review-only `standard` / `deep` (without `--fix`):**
  - **>= 1 unjudged finding:** spawn `craftsman-verifier` (or run inline when the platform has no
    subagents). At Step 0, the verifier performs the `sweep` pass and root-cause deduplication
    before recording verdicts — **without reading `packet-N.md`**, using `craftsman review show`,
    `craftsman review show --hunks`, `git diff <base> --stat`, targeted `git diff <base> -- <file>`,
    and targeted source reads around cited `file:line` locations and callers.
  - **0 findings:** do **not** spawn `craftsman-verifier`. Perform the `sweep` lens **inline
    yourself** after all other lenses finish: load the playbook with
    `craftsman skill show code-review --lenses sweep`, get the `H# -> file:lines` map from
    `craftsman review show --hunks`, inspect `git diff <base> --stat` and targeted
    `git diff <base> -- <file>` (plus enclosing code where hunks interact), and record
    `craftsman review cover --lens sweep` in batches of 1–4 hunks with a concrete **>= 40-char**
    code-detail segment for every `H#` (`--stat` counts or hunk headers alone will fail coverage
    validation). If `sweep` uncovers a defect, record it with `review add --lens sweep ...` and
    verify it.
- **With `--fix` or inside `/craftsman` (`standard` / `deep`):** after Wave 1 finishes, run a
  dedicated `craftsman-reviewer` for `sweep`; then spawn `craftsman-verifier` **only if >= 1
  finding awaits a verdict** (when 0 findings exist after `sweep`, skip `craftsman-verifier`).

When verifying findings, switch roles — your goal is to prove each finding **wrong**:

1. **Group duplicates by root cause first (before any verdict).** `craftsman review merge` only
   works on findings without a verdict, before verification. If two or more unjudged findings stem
   from the same root cause (the same changed line or decision producing multiple symptoms), fold
   the duplicates into the primary finding so one finding carries all consequences and the
   duplicates are dismissed:
   ```bash
   craftsman review merge F1 F4 F7 --scenario "<root cause -> (1) effect A; (2) effect B; (3) effect C>"
   ```
   > Merge only when ONE fix removes every consequence. Different fixes are separate findings, even on the same line, file, or category.

   For example, a CLI command missing a documented `--format` option and a stale README example are two
   separate findings, not one "housekeeping" finding. A finding that duplicates an already-judged one (for
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
(quote the guard expression). A batch rejection records nothing and lists every verdict entry that
needs fixing — fix all of them in one edit and resend once (never rerun unchanged input).

**Leave the change under review untouched.** Reproductions live in a fresh temporary directory
outside the repository. Never `git stash`, `checkout`, `switch`, `reset`, `restore`, `clean`, or
edit files to compare with the old behavior: read the old version with `git show <base>:<path>` (or
copy it into the temporary directory) instead. Delete caches your runs created inside the repository
(`__pycache__`, …).

## Step 4 — Report (review-only mode)

Without `--fix`, finish with:

```bash
craftsman review check --review-only
craftsman review report --review-only
```

and present the report output. `--review-only` checks that the round matches the frozen scope, every
selected lens (`sweep` included on `standard`/`deep`, omitted on `quick`) covered all non-carried
targets, every mechanical quality signal (`S#`) is acknowledged or linked to a finding, and every
finding has a verdict — without requiring `proven`/`probable` findings to be resolved or a passing
test run (reporting `COMPLETE (review-only)` or `INCOMPLETE (N open items)`). If a subagent failed
on rate limits, quota, or timeout after one sequential retry, finish with
`craftsman review report --review-only --partial` (`PARTIAL (review-only, N open items)`, listing
`## Not covered` and `(unverified)` findings) and summarize what could not be checked. Running
`craftsman review check` or `craftsman review report` *without* `--review-only` in review-only mode
is expected to show `BLOCKED` (because findings are left open for the author and the full test
suite was not run) — always pass `--review-only` here. Findings are ordered by severity; dismissed
ones are listed briefly so the reader sees what was considered.

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
3. Run `craftsman review prepare` again — if your fixes created a new file (such as a regression
   test) or edited an existing file outside a frozen review scope, pass
   `craftsman review prepare --add-paths <comma-separated-paths>` so every file touched by the fix
   is included in the next round. The ledger detects which hunks changed since the last round; only
   those need the lenses again. Repeat Steps 2–5 for the delta until a round produces no new
   `proven`/`probable` findings.
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
- **Shell.** Single-line flag commands have no shell expansion when single-quoted and work
  identically in `bash`, `zsh`, `PowerShell`, and `cmd`: keep each on one line (no `\`
  continuations), and use single quotes whenever a note, scenario, or evidence quotes `$` or
  backticks. Multi-line `--from-json - <<'EOF'` heredocs are POSIX-shell (`bash`/`zsh`) syntax; in
  `PowerShell`, pipe a single-quoted here-string (`@'` and `'@` on their own lines, which does not
  expand `$` or backticks, matching `<<'EOF'`):
  ```powershell
  @'
  [ ...json... ]
  '@ | craftsman review cover --from-json -
  ```
  In Windows PowerShell 5.1, piping to a native executable encodes via `$OutputEncoding` (ASCII by
  default, turning non-ASCII characters into `?`), so either keep notes in ASCII or write UTF-8 to a
  managed inbox file: run `craftsman review inbox cover` (or `add` / `verdict`) to print a unique
  inbox path, write UTF-8 to it (`Set-Content -LiteralPath <path> -Encoding utf8` for read-only
  subagents without `Edit`), and pass `--from-json <path>` (consumed automatically on success; kept
  in place on rejection so you can fix every listed entry in one edit and resend once; never write
  batch JSON files to `%TEMP%` or into the repository). Use the project's Python (`python`, or the
  virtualenv's); `python3` does not exist on many Windows machines.
- **No CLI available?** If `craftsman` is not installed, say so explicitly in the report — the
  review then has no gate and must not be presented as gated.
