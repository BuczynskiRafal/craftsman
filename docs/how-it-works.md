# How Craftsman Works

Craftsman is built on one idea: **build freely, review hard**. The agent implements without review
loops or ledger bookkeeping. When it finishes, the final diff is frozen and attacked by fresh-context
hunters; every finding is judged with evidence; the tests run on the final code. The gate checks
outcomes on that final diff, not the effort that led there.

This page is the one place that explains the protocol in full. The prompts are the source of truth for
what the orchestrator does:
[`skills/craft-review/SKILL.md`](../skills/craft-review/SKILL.md) (the review) and
[`skills/craftsman/SKILL.md`](../skills/craftsman/SKILL.md) (the build around it). The hunt plan and the
gate are code ([`huntplan.py`](../craftsman/huntplan.py), [`gatecheck.py`](../craftsman/gatecheck.py)).
The README and the [usage guide](usage.md) only summarize it.

![Craftsman 5: build freely, review hard](diagrams/harness-overview.png)

---

## The Protocol

1. **Gate.** `craftsman gate --init [--deep] --task "<one line>"` records the base commit. The prompt
   hook runs it on platforms that have hooks.
2. **Build.** No ledger entries, no review. The agent asks you only when an ambiguity changes what
   gets built (`gate --await-user`, then `gate --resume`).
3. **Freeze and plan.** The orchestrator writes the **intent brief** (3-8 claims about what the change
   must do, each tied to a line; a claim without a line is already a finding) and runs
   `review prepare --task-file -` with it. `prepare` computes the diff since the base into round N and
   prints the packet path, the depth and why, whether the round is enforced or advisory, quality
   hints, warnings, and the **hunt plan**: one slot per hunter, each with its hunks and lenses. The
   plan comes from the diff alone (see [The hunt plan](#the-hunt-plan)); the orchestrator cannot drop
   a slot.
4. **Hunt.** The orchestrator launches every slot together, each as a fresh-context
   `craftsman-reviewer` subagent whose whole prompt is one line:
   `You are Craftsman hunter W1-2. Run craftsman review brief W1-2 and follow it exactly.` `review brief`
   records that the slot was taken and prints the task, the base ref, the slice, the lenses and the
   recording commands, which carry `--hunter W1-2`. Every review launches at least one hunter, even for
   a one-line change, wherever the platform has subagents.
5. **Check and verify.** When the hunters report, `review check` says what is missing, in order:
   slots to launch or relaunch, then findings without a verdict, grouped into clusters (one per file,
   at most 6 findings each). One `craftsman-verifier` per cluster, with the prompt `You are a Craftsman
   verifier. Run craftsman review brief --verifier F1,F2 and follow it exactly.`, merges duplicates,
   tries to disprove each finding, reproduces it where the code is runnable, and records a verdict. A
   `blocker` or `major` finding dismissed without a run needs a second, independent verifier. On Claude
   Code, the simplicity slots and the verifiers of clusters that hold only `minor` or `nit` findings run
   on the cheaper model (`haiku`); everything else, and a verifier resumed to confirm a fix, does not.
6. **Saturate.** Waves are planned, not derived: `check`, `report`, `brief` and `prepare` store a wave
   in the plan when a `proven`, or serious `probable`, bug finding outside documentation calls for it.
   A wave is one **variant** slot per root cause (`W2-F3`, which hunts the siblings of finding F3, at
   most 3 per wave) plus one **fresh** set (`W2-X1`, an unanchored look at the hunks with the lenses
   nobody applied there). Waves count as generations, at most 3: a variant of a variant of a variant
   spawns nothing. The hunt stops at the first wave that adds nothing. A deep round plans its fresh
   set up front.
7. **Fix, confirm, re-check.** Three separate jobs.
   - *Fix.* Every `proven` or `probable` finding is fixed at the root cause, with a regression test
     where the project has tests.
   - *Is it fixed?* A `proven` finding is confirmed by its regression test: it fails before the fix
     and passes after, through `craftsman run`. A `probable` one (no repro) goes back to the verifier
     that judged it, resumed with only the fix diff, where the platform can resume an agent;
     otherwise a fresh verifier gets the finding and the diff. Then `review resolve --as fixed`, with
     a note that cites the run or the confirmation.
   - *Did the fix break anything?* `review prepare` opens the next round: the changed hunks plus the
     hunks that reference a changed symbol. If they are few and quiet (at most 40 changed lines, no
     deep signal), the plan is one bug slot with every owed bug lens, plus a simplicity slot. Fresh hunters take its slots (never the finder or its
     verifier, who are anchored), and the round cap bounds the loop.
8. **Test.** `craftsman run --kind test -- <cmd>` on the final code; deep also `--kind e2e`. A test run
   counts only when its runner reported at least `--min-tests` tests (default 1); a command that matched
   no tests is recorded but is no evidence. Output prints as it arrives up to 6,000 characters; past
   that, a line names a log in the scratch area that holds the full output and grows while the
   command runs, and the last 6,000 characters print when the command exits (`--full` prints it all).
9. **Close.** `craftsman review check`, then `craftsman gate --complete`, then `craftsman review
   report`.

A subagent that fails (rate limit, timeout) shows in `review check` as a slot that stopped without
finishing, and the orchestrator launches it once more. If subagents keep failing, the agent runs
`gate --await-user` and tells you what is not covered; in review-only mode the report is `--partial`.

Accepting a finding instead of fixing it is a protocol rule, not something the CLI checks beyond
refusing blockers: a `major` is accepted only after the user agreed.

### The hunt plan

The amount of review work depends on the diff, never on how hard the model feels like trying.
[`craftsman/huntplan.py`](../craftsman/huntplan.py) is pure: it turns a snapshot and the ledger into
slots, and the CLI stores them in the round.

| Rule | What it does |
| :--- | :--- |
| **Required lenses** | By hunk kind. Prose owes `intent` and `craft`. Tests and configuration owe the core lenses (`intent`, `contracts`, `failure`, `ripple`, `tests`) and the simplicity lenses (`craft`, `design`). Production code owes those plus every conditional lens (`state`, `trust`, `platform`, `model`) whose risk signal fired in that very hunk, or all of them when the round is deep. |
| **Owed lenses** | Required minus *credited*: the lenses that accepted coverage of the same hunk digest already applied, from this round or an earlier one. A hunk with nothing owed is carried into the next round. |
| **Generation 1 slots** (`W1-<n>`) | Owed hunks are cut into slices of about 400 changed lines, whole files first. Each slice becomes up to four bug slots, one per group of related lenses (`intent`/`ripple`/`tests`, `contracts`/`failure`, `state`/`platform`, `trust`/`model`), plus a simplicity slot for `craft` and `design`. Prose-only slices get one slot. |
| **Sweep** | When the plan has two or more bug slots, one whole-diff slot (or a core slot with room) also applies `sweep`: hunks that are each correct but disagree. |
| **Delta rounds** | A later round with at most 40 owed changed lines and no deep signal is one bug slot with every owed bug lens, a simplicity slot when `craft` or `design` is owed, and no new sweep (a sweep an earlier round left unfinished is still carried). |
| **Later generations** | `W<g>-F<n>` variant slots and `W<g>-X<j>` fresh slots, from proven or probable bug findings, up to generation 3. |

Plans are append-only: re-running `prepare` on the same diff, or raising the round to deep, adds the
slots newly owed and keeps every slot and wave already stored, so the same finding never triggers the
same wave twice.

### Enforced and advisory reviews

`prepare` prints one of two lines. **Enforced** means the platform's hooks see the session: finishing
(the Stop hook of a gate, the review guard of a `/craft-review`) requires every slot of the plan done.
**Advisory** means no hook saw it (Codex, Cursor, or a session whose hooks are not installed; run
`craftsman doctor`): the plan is a suggestion, the review is complete when every hunk is covered, and
the report carries the `SELF-REVIEWED` banner. A round stays enforced once it was: a later command
without a hook does not downgrade it. In an enforced round, `review add` and `review cover` must name
their slot with `--hunter` (`lead` marks the orchestrator's own findings and never credits coverage).

### `/craft-review`

The same hunt (steps 3-6) against a base, with a frozen scope, and no fixes unless asked. It ends
with `review check --review-only` and `review report --review-only`. Fixing adds steps 7-8.
`ultracraft` inside `/craft-review` makes it a deep review. It never opens a gate: only `/craftsman`
does.

### Adaptive depth

There is one depth, `deep`. `prepare` decides it from the diff: deep when a security or concurrency
signal (`process`, `sql`, `deserialization`, `auth`, `concurrency`) fires in a production hunk, or when
the change holds at least 251 changed production lines; it prints `Depth: deep (<reasons>)`, or
`Depth: normal (no deep signal; N changed production lines <= 250)`. The orchestrator can raise it with
`review prepare --deep` for the judgment calls listed under **Depth** in the
[`craft-review` skill](../skills/craft-review/SKILL.md) (persistent data, public APIs, removed
guarantees, a user who asks for care). Deep owes every lens on every production hunk, plans a fresh
second look up front, and makes the gate require both a test run and an e2e run. Inside a gate session,
`--deep` raises the gate for good. Depth never goes down.

---

## What Hunters Read

A review costs agents times turns times context, so the inputs are kept small.

- **Packet.** Each hunk appears once, inside its enclosing scope at HEAD, with `+` and `-` marks on
  the changed lines and `[H3]` markers around each hunk, plus callers, tests and the hunk's risk
  signals. It has no lens table or recording cheat sheet (the hunt plan and the hunter's brief already carry them), so a small diff costs a hunter little more than its code context.
  A `review view` is the same layout for some of the hunks.
- **Scout.** `craftsman review scout <H#|path:line|symbol>` answers with code, deterministically and
  without a model: the base version of the enclosing scope, callers, and tests, capped at 12,000
  characters. Hunters ask the scout before opening whole files.
- **Briefs.** A hunter's launch prompt is one line naming its slot; `craftsman review brief <slot>`
  prints the rest: the task (the intent brief), the base ref, the slice as a `review view --hunks`
  command, the lenses with the command that loads their playbooks, and the recording commands with
  `--hunter <slot>`. A variant slot's slice is small: the root cause, its location and the affected
  hunks. A verifier's brief (`review brief --verifier F1,F2`) lists its findings with their scenarios
  and the verdict commands, and records nothing. On Antigravity and on platforms without agent definitions (Codex, Cursor) the brief ends with the
  role text of the agent template, because the reader did not load it itself.
- **Pipelining.** Where agents run in the background (Claude Code, Antigravity async subagents), the
  orchestrator runs `review check` after every report and launches what is ready: the verifiers of a
  finished hunter's findings while other hunters still run, then the slots of a wave as soon as a
  finding is judged. Slots that are running are waited for, never launched twice. Where parallel
  subagents are a barrier (Copilot), the waves stay as they are.
- **Launch stagger.** On Claude Code a subagent's request reads the prompt cache only once the
  response that wrote it has begun, so hunters launched together would each write their whole
  starting context (6,917 tokens per reviewer, measured on Claude Code 2.1.322 with Sonnet, where one
  started later read 6,920 and wrote none). The `SubagentStart` hook therefore holds every hunter or
  verifier that starts within 5 seconds of an earlier one of the same role until that earlier start is
  5 seconds old: one cache write and reads for the rest, instead of a write each. Nothing the agents
  read or do changes.
- **Remaining work.** `craftsman review show --hunks` lists the hunks still to cover.
  `review show` also prints each finding's scenario and verdict evidence, so a verifier needs no
  `--json` call.
- **Inbox fallback.** If a heredoc needs approval, the agent writes the JSON to the path that
  `craftsman review inbox <add|cover>` prints and passes `--from-json <path>`.
- **Usage.** Where the platform hands over the subagent transcript, `review report` and
  `craftsman status` total tokens and agent time per role (hunters, verifiers).

---

## Evidence Ledger

Everything goes into `.git/craftsman/ledger.json`, stored separately for each worktree and outside
commits (schema 4; schema 3 ledgers are still read).

![Evidence ledger: what is recorded and when the gate passes](diagrams/evidence-ledger-flow.png)

| Record | Written by | Gate requirement |
| :--- | :--- | :--- |
| **Round**: diff fingerprint, hunks with digests, scope, `deep` flag, task text, whether it is enforced, and the stored hunt plan (slots and waves) | `review prepare` | The latest round matches the current code |
| **Claim**: a hunter taking its slot (`B1`) | `review brief <slot>` | A slot with a claim and no stop is `running`; a claim that no open run backs counts for nothing |
| **Coverage**: hunk digests, the slot (`--hunter`) and a one-line note (at least 20 characters) on what a hunter examined and tried | `review cover` (or `--from-json`) | Every hunk of the round is covered, in this round or an earlier one, and in an enforced round every slot is done |
| **Finding**: location, lens, severity, scenario, the slot that found it (or `lead`) | `review add` (or `--from-json`); `review move` fixes a line; `review merge` merges duplicates with a shared root cause | Every finding has a verdict |
| **Verdict**: `proven` (reproduction `--run R#`), `probable` (`static: <file:line chain>` or `no-repro: <why>; <chain>`), `question` (`intent: <source>; <file:line>`, non-blocking findings only), `dismissed` (run or `file:line`). Every verdict is kept in a log (`V1`...) | `review verdict` (or `--from-json`) | `proven` and `probable` findings are resolved. In review-only mode they go into the report as warnings. In an enforced round a verifier subagent must have recorded the verdict |
| **Resolution**: `fixed` (only after a code change) or `accepted` (never for blockers) | `review resolve` | All blocking findings are resolved |
| **Run**: argv, working directory, exit code, output tail, diff fingerprint | `craftsman run` | A passing test run (and an e2e run when deep) on the final code that did not modify files. The last run of every command succeeds unless retired with `review waive-run` and a visible reason |
| **Agent event**: reviewer and verifier subagent start, stop and nudge, with token usage when known | platform hooks, or `agy_sync` from the transcripts on Antigravity | A reviewer subagent run (not the orchestrator) finished after the round was prepared |

Every record is tied to a diff fingerprint, so a code change invalidates old evidence. Details that
matter when reading the ledger:

- **Hunk ids are stable and never reused.** A carried hunk keeps its id, so ids can be sparse (`H1`,
  `H4`, `H9`). Do not assume `H1..Hn`; `review show --hunks` lists what is left.
- **A hunk stays covered across rounds by its digest**, which ignores line numbers and CRLF, so the
  next round re-reviews only changed hunks and hunks that reference a changed symbol. `cover` skips
  carried hunks.
- **`cover --files` is forgiving.** It accepts `./x`, backslashes, absolute paths inside the
  repository, and the old path of a renamed file.
- Generated files (`__pycache__`, build caches, `node_modules`, ...) are skipped; custom patterns go
  in `.craftsmanignore`.

---

## What the Gate Checks and What It Does Not

The gate reports an issue list. It passes when the list is empty:

| Issue | Meaning |
| :--- | :--- |
| `no-round`, `stale-round` | No review round, or the code changed since the last one |
| `uncovered` | Open hunks no hunter has covered |
| `hunt-pending` | Enforced round: slots of the first generation are not done (not launched, running, or stopped without finishing) |
| `wave-pending` | Enforced round: slots of a later wave (variant or fresh) are not done |
| `no-verdict` | A finding has no verdict |
| `unverified` | Enforced round: a verdict no verifier subagent recorded |
| `second-opinion` | Enforced round: a `blocker` or `major` finding dismissed without a run has only one verifier behind it |
| `unresolved` | A `proven` or `probable` finding is neither fixed nor accepted |
| `no-passing-run` | No passing test run on the final code |
| `no-e2e-run` | Deep only: no passing e2e run. A deep gate needs both a test run and an e2e run |
| `failing-run` | The latest run of a command failed and was not waived |
| `no-isolation` | The platform gives isolation evidence, but no `craftsman-reviewer` finished after this round was prepared, or a hunk was covered outside any reviewer run |
| `round-cap` | More than 5 distinct diffs were reviewed: the agent stops and the user decides |

- **The plan is stored, never recomputed.** What an enforced round requires is exactly its stored plan.
  A slot is done when its hunks owe none of its lenses any more; it is `running` while its hunter's run is
  open, and `stopped without finishing` when the hunter stopped or its claim went stale (30 minutes).
  `review check` shows each slot's state, and the Stop hooks list the next launch in their message.
- **Who did the work.** Coverage and verdicts count only when a finished subagent of the right role
  vouches for the record, so the orchestrator cannot cover hunks or judge findings in place of its
  subagents. A run vouches in one of three ways, from the strongest: its identity (the record names its
  caller, `antigravity:<conversation id>`), its transcript (the run printed the record's token, such as
  `Coverage recorded (C3)`, which the SubagentStop hook reads on Claude Code), or the time window of
  the run (Copilot, or a transcript that could not be read), which is bounded in how many slots it can
  credit. On Antigravity every hunter and verifier is a conversation of its own; `agy_sync` reads their
  transcripts back into the ledger when `check`, `report`, `brief` or `prepare` runs and at the Stop hook.
- **Isolation is checked per round.** The latest round with non-carried hunks needs a reviewer that
  finished and recorded something while it was current; a round replaced before anything was covered in
  it needs none. Findings may come from anyone (the orchestrator records intent findings itself as
  `lead`); verifiers judge every one. A reviewer must record coverage or a finding before it stops; the
  hook sends one that did not back to work, once. A stop whose agent id matches no start is paired
  first-in, first-out with the unpaired starts of its agent name.
- **No isolation evidence, no claim.** When the platform gives none, the round is advisory: the gate
  still closes, and the report carries the `SELF-REVIEWED` banner, whether the review was deep or not.
- **Commands cannot forge the evidence.** One policy (`craftsman/command_policy.py`) decides every
  pre-tool call on every platform: `craftsman hook ...` and inline programs that import craftsman are
  denied, so are writes into `<git-dir>/craftsman/` (the ledger, the gate state, the hook log; `inbox/`
  and `scratch/` stay open) and into the platforms' transcript folders, by a shell command or a
  file-writing tool. The identity variables cannot be set for a craftsman command, and the main session
  records only as `lead`. Details: [guards.md](guards.md). These checks stop low-effort shortcuts; they
  are not a sandbox.
- **Hook errors are never silent.** Whatever a hook survives (a crash, an unreadable transcript, a git
  call out of budget, a ledger set aside) is appended to `<git-dir>/craftsman/hook-errors.log`, and
  `review check`, `review report`, `gate --complete` and `status` print `Hook errors since round N was
  prepared: K (see <path>)`.
- **What a passing gate proves, and what it does not.** It proves that coverage and verdicts were recorded
  by separate subagent runs (attributed by identity on Antigravity, by transcript token on Claude Code, by
  time window on Copilot), that hook commands cannot be run from the agent's shell, that the ledger cannot
  be edited by the agent's tools, and that the tests passed on the final code. It does not prove that the
  review was good: a passing gate is no evidence that the hunters looked hard or found everything.
- **What it does not assess.** Business-requirement fit, naming quality, and architecture outside the
  files touched by the diff. Every report lists these under `Not assessed by the gate`. The gate also
  does not judge how hard the hunters looked beyond the plan: it checks that every slot of the plan is
  done, that fresh hunters covered every hunk of the frozen diff and that every finding has checkable
  evidence. A very large diff produces a warning in `prepare` and in the report; it does not hand off
  to the user.

### Upgrading from Craftsman 4.x

Nothing to run. The first command that reads a pre-5.0 gate state or a schema 2 ledger archives it
(`craftsman_state.json.v4.bak`, `ledger.v2.<UTC stamp>.bak.json`) and starts fresh, so neither `gate --init
--takeover` nor `review reset` is needed. Re-run `craftsman install` to refresh the skills and agents.

---

## Review Lenses

A lens is a focused review angle with specific probes. `review prepare` decides which lenses each hunk
owes, from its kind and the risk signals that fired in it, and groups them into slots (see [The hunt
plan](#the-hunt-plan)). The catalog, with the signals that trigger each lens, is
[`craftsman/lenses.py`](../craftsman/lenses.py), the probes are in
[`skills/craft-review/lenses.md`](../skills/craft-review/lenses.md), and a hunter prints only its own
with `craftsman skill show craft-review --lenses <ids>`.

| Lens | What it looks for | Owed by |
| :--- | :--- | :--- |
| `intent` | Whether the change does all it claims, everywhere it should, and nothing else | Every code change (core) |
| `contracts` | Whether each changed function handles the full range of inputs it can receive | Every code change (core) |
| `failure` | Whether failures are loud, precise, and clean | Every code change (core); also on `errors`, `process`, `filesystem`, `network`, `sql` signals |
| `ripple` | Callers, configs, and users of what changed or disappeared | Every code change (core); also on `deletion`, `config` signals |
| `tests` | Whether the tests would fail if the changed behavior broke | Every code change (core) |
| `state` | Shared state, resource release, repeated runs, concurrency | `concurrency`, `filesystem`, `time` signals |
| `trust` | Untrusted input reaching a dangerous sink, bypassed permission checks | `process`, `sql`, `deserialization`, `auth`, `network`, `regex`, `filesystem` signals |
| `platform` | Operating systems, shells, encodings, locales, time zones | `platform`, `time`, `process`, `filesystem` signals |
| `model` | Illegal states the data model allows, invariants | `datamodel` signal |
| `craft` | Reuse, needless complexity, misleading names and comments | Every code change (simplicity slot) |
| `design` | Overall design and size of the change, what can be deleted | Every code change (simplicity slot) |
| `sweep` | Hunks that are each correct but disagree with each other, the docs, or the config | Every code change (whole-diff lens; one slot runs it when the plan has two or more bug slots) |

Docs-only changes owe `intent` and `craft`. A deep round owes every lens on every production hunk. Tests and configuration owe the core and simplicity lenses only: a filesystem call in a test is not a filesystem risk.

---

## Platform Enforcement

The protocol, lenses, subagents, and ledger are shared across platforms. What differs is the
mechanism that blocks completion.

| Platform | Stop gate | Reviewer isolation | Subagents |
| :--- | :--- | :--- | :--- |
| GitHub Copilot in VS Code | `Stop` hook blocks until `review check` passes | `SubagentStart`/`SubagentStop` hooks | `craftsman-reviewer`, `craftsman-verifier` agents |
| GitHub Copilot CLI | `agentStop` hook | `subagentStart`/`subagentStop` hooks | The same agents |
| Claude Code | `Stop` hook | `SubagentStart`/`SubagentStop` hooks | The same agents in `~/.claude/agents` |
| Google Antigravity | `Stop` hook | Subagent runs are read back from the transcripts (`agy_sync`) at `check`, `report`, `brief`, `prepare` and the Stop hook; records name their caller | `invoke_subagent` (`TypeName: self`, `Role: craftsman-reviewer` or `craftsman-verifier`) |
| Codex, Cursor, and others | `craftsman gate --complete` refuses completion until the ledger passes validation | None (the review is advisory: complete when every hunk is covered, `SELF-REVIEWED`) | None |

The Stop gate runs only in a `/craftsman` session. A `/craft-review` session is held to the review by the
review guard instead, and the command policy and the search scope guard run on every tool call. Outside a
Craftsman session the hooks allow everything. After six forced
continuations in a row without fewer open issues (`MAX_CONTINUATIONS`), the stop hook lets the agent
through and tells the user the gate was not satisfied; the review guard of a `/craft-review` does the same and marks the review `REVIEW ABANDONED - NOT COMPLETE`.
When a hook fails, the agent continues with a warning that the gate was not checked, and the failure goes to
the hook error log; a failing `pre-tool` check asks the user to confirm the tool call instead (Copilot denies it). Install, hook, and guard internals:
[harness-architecture.md](harness-architecture.md) and [guards.md](guards.md).
