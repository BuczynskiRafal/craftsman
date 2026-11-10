# Changelog

All notable changes to Craftsman are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Security

- **Every hook runs Craftsman's own code.** A hook runs with the guarded project as its working directory, and
  `python -m craftsman.cli` put that directory first on `sys.path`, so a project's own `craftsman/` directory or
  `json.py`, which the agent can write like any other file, could stand in for the gate. No entry point starts
  Craftsman with `-m` any more; each runs a bootstrap script that puts Craftsman's own directory first:
  - the Claude Code plugin: `bin/launcher.sh` and `bin/launcher.cmd` run the new `bin/craftsman-cli.py`.
    `launcher.sh` takes a path with a drive letter or a UNC path (Git Bash on Windows) as absolute, and still starts
    one interpreter, as a child that it stops when the platform stops a hook that ran too long (SIGTERM, SIGINT or
    SIGHUP), so no hook outlives its time;
  - the user-level hooks of `craftsman install --claude` and `--copilot`: `"<python>" "<package>/craftsman-cli.py"`, a
    bootstrap inside the installed package, for a virtual environment, `pip install --user`, an editable install and a
    source checkout alike (they wrote `"<python>" -m craftsman.cli` before);
  - the checkout's `scripts/craftsman-cli.py` and the Antigravity hook scripts put their root first even when it is
    on `PYTHONPATH` already (an empty `PYTHONPATH` entry put the working directory ahead of it);
  - `install.sh` and the evals' `craftsman` wrapper run a bootstrap too.

  The bootstraps are named `craftsman-cli.py`, so the command policy denies running them as a forged hook like any
  other spelling of `craftsman hook`. **If you installed user-level hooks with `craftsman install --claude` or
  `--copilot`, run `craftsman install` again**: it replaces the old hook commands, and `craftsman doctor` reports
  hooks still in the old form as outdated. The Claude Code plugin needs no action.
- **An agent can no longer run a hook through the plugin's launchers.** The command policy denied `craftsman hook ...`
  but did not read `bin/launcher.sh`, `bin/launcher.cmd` or `bin/craftsman.cmd` as the Craftsman CLI, so an agent
  could forge a hook call with `"<plugin>/bin/launcher.sh" hook stop ...`. These launchers and `bin/craftsman` are now
  the CLI, run directly, by a Windows path, or through `sh`, `bash`, `dash` or `cmd /c`: `hook` is denied, and any
  other subcommand is read as the command it is.

### Added

- **`craftsman run` bounds long output and names a live log of all of it.** Output prints as it arrives, unchanged,
  up to 6,000 characters, so shorter output behaves exactly as before at every moment. Past that, printing stops and
  a line names `<git-dir>/craftsman/scratch/<id>/run-output.log`, which holds the full output, grows while the
  command runs and keeps its name until the review ends, so an agent whose shell call returned before the command
  finished can follow it there. When the command exits, the rest prints whole if it is at most 6,000 characters,
  else as a line `[craftsman run: N characters omitted; full output: PATH]` and the last 6,000 characters (cuts move
  to a line where one is near). A long test log no longer fills the agent's context, and memory stays bounded
  however much the command prints. If the log cannot be written, the line says why and everything prints. The
  ledger record, test count, fingerprints and exit code are unchanged, and `--full` prints everything as it arrives.
- **The benchmark reports tokens and where a review spends them.** `python3 -m evals.run compare` gains a `tokens`
  and a `$/defect` column (cost per seeded defect found), their ratios in the versus table, `--tokens` for the
  input / output / cache split, and the new values in `--json` (every existing key is kept). The new
  `python3 -m evals.run breakdown` reads each run's ledger and shows subagents per run, hunters by slot kind and
  wave, tokens and agent time per role with the main session's share, the findings funnel, which slot first found
  each seeded defect, and findings on no defect. Every run it leaves out is named with the reason. A run's tokens
  come from its per-model usage, which covers the whole session; a run without it counts as having no tokens, never
  as the last turn's usage.

### Changed

- **Hooks and commands start much faster.** A command imports only the modules it needs, and a read-only tool call
  (`Read`, `Grep`, `Glob`, ...) outside an active gate is answered on Claude Code and Copilot before the gate code
  loads. Through `bin/launcher.sh`, median of 15 runs, before and after: a read-only `pre-tool` hook 234 -> 31 ms,
  a `Bash` `pre-tool` hook 227 -> 131 ms, a prompt hook 226 -> 134 ms, `--version` 213 -> 31 ms. Every case where
  the gate, the scope guard or git could decide otherwise (an active gate, a Craftsman agent, a git directory or work
  tree moved by configuration or the environment, a gate state the gate would not read as inactive) still goes
  through the full hook, and tests check that every call answered early gets the full hook's answer.
- **Fewer git processes per command and hook.** The work tree and git directory are found with one `git rev-parse`
  and carried along instead of being looked up again for every snapshot and path. Git processes per operation,
  before and after: `review prepare` 17 -> 12, `review add`, `cover`, `brief` and `check` 9 -> 4, `craftsman run`
  15 -> 7, the Stop hook with an active gate 9 -> 5, an idle `pre-tool` hook 2 -> 1. Each is a few milliseconds on
  Linux and tens on Windows. `review view` searches references only for the hunks it shows; its output is unchanged.
- **Hunters that start together no longer each write their starting context to the prompt cache.** On Claude Code a
  reviewer or verifier that starts within 5 seconds of an earlier one of the same role waits in its `SubagentStart`
  hook until 5 seconds after the first, so it reads the cache entry the first one wrote; a resumed agent never
  waits. Measured on one cold-cache run of three reviewers (Claude Code 2.1.322, Sonnet): the subagents of the burst
  cost about $0.041 instead of $0.081, and the wave starts about 5 seconds later. Other platforms are unchanged.
- **The review packet no longer lists Craftsman's own files as conventions.** A file Craftsman generated, or one that
  holds only Craftsman's managed block, is left out of "Conventions to read first"; a file with the block and text of
  your own stays listed with a note that the block is not a project convention. Hunters no longer spend their
  reading on Craftsman's instructions.
- **`review check` marks the launches that run on the cheaper model.** Simplicity slots and verifier clusters of only
  `minor` or `nit` findings carry `[Claude Code: model haiku]` where `check` says what to launch, so the orchestrator
  no longer has to work this out from the prompts. Messages stay within the stop hook's 900-character budget.
- **Shorter skill and agent descriptions.** These are listed in every session whether a skill is used or not: the
  skill descriptions went from 4,293 to 1,993 characters and the agent descriptions from 817 to 450, and the
  workspace `AGENTS.md` from 11,263 to 8,963. The commands and triggers they name are unchanged, and `craft-review`
  says it is Craftsman's review: for when you ask Craftsman to review, and the final review of `/craftsman`. A test
  holds the limits.
- **`craftsman.cli` exports less.** The status report and the skill reader moved to `craftsman/status.py` and
  `craftsman/skill_reader.py`, so a script that imported `run_status` or `discover_skill_dirs` from `craftsman.cli`
  must import them from there. The commands and their output are unchanged.

### Fixed

- **A work tree whose directory name starts or ends with a blank.** Its path was trimmed, so commands and hooks
  looked for a directory that does not exist; only the line ending git adds is removed now.
- **A relative `GIT_DIR` with `GIT_WORK_TREE` from a subdirectory.** The git directory is now resolved from the
  directory the command runs in, together with the work tree, so the hooks find the gate there instead of failing.
- **SIGTERM and SIGHUP stop `craftsman run` the way Ctrl-C does** (POSIX): the command's process group is stopped,
  the run is recorded as interrupted, the end of its output prints, and `craftsman run` exits with 143 or 129.
  Before, such a signal left the command running and recorded nothing.

## [5.0.0] - 2026-11-09

### Security

- **Hooks cannot be forged and the evidence cannot be edited from the session.** One entry point,
  `craftsman/command_policy.py`, decides every PreToolUse call for every platform: `craftsman hook ...` (and
  `import craftsman` in an inline program) is denied, as is any write into `<git-dir>/craftsman/` (ledger, state,
  hook log; `inbox/` and `scratch/` stay open) or into the transcript folders of the platforms, by a shell command or
  by a file-writing tool. The Claude Code matcher now covers `Write|Edit|MultiEdit|NotebookEdit`, and the write check
  runs before the scope filter. Identity variables (`CLAUDE_CODE_SESSION_ID`, `CRAFTSMAN_*`, ...) cannot be set for a
  craftsman command, the main session records only as `lead`, the gate owner cannot `--takeover` its own gate, and a
  made-up `--session-id` is denied. A subagent's transcript is read only from under the platform's transcript folders.
- **Taking a gate over keeps what the hooks proved.** `gate --init --takeover` keeps `isolation_evidence`, so the next
  round is still enforced.

### Added

- **An enforced hunt plan.** `craftsman review prepare` now computes the hunt from the diff (`craftsman/huntplan.py`)
  and stores it in the round as slots: `W1-<n>`, each with its hunks and lenses. What a hunk owes follows from its kind
  (prose, tests, configuration, production) and the risk signals that fired in it, minus the lenses accepted coverage
  already applied to it; related lenses share a slot (about 400 changed lines each), `craft` and `design` get a slot of
  their own, a `sweep` joins when there are two or more bug slots, and a small follow-up round (40 changed lines at
  most, no deep signal) is one bug slot plus a simplicity slot. The round is **enforced** when the platform's hooks see the session (the gate's
  Stop hook and the review guard then require every slot done) and **advisory** otherwise, as before
  (`SELF-REVIEWED`). Depth is decided by the CLI too: deep on a security or concurrency signal in a production hunk or
  at least 251 changed production lines (`Depth: deep (<reasons>)`); `--deep` still raises it.
- **`craftsman review brief <slot>` and `brief --verifier F1,F2`.** A hunter's whole launch prompt is one line naming its
  slot; the brief records the claim and prints the task, the slice, the lenses and the recording commands. `review add`
  and `review cover` take `--hunter <slot>` (required in an enforced round; `lead` marks the orchestrator's own
  findings and never credits coverage); `review prepare` takes `--task-file` for a longer intent brief.
- **Waves are stored, not derived.** A proven (or serious probable) bug finding makes `check`, `report`, `brief` and
  `prepare` store a variant slot per root cause (`W2-F3`) and a fresh set (`W2-X1`), up to three generations. `check`
  groups the findings without a verdict into clusters for verifiers (one per file, six findings at most), and a
  `blocker` or `major` finding dismissed without a run needs a second, independent verifier (`hunt-pending`,
  `wave-pending`, `unverified` and `second-opinion` join the gate issue codes).
- **Ledger schema 4** (schema 3 is still read): slot plan and waves in the round, slot-tagged coverage and findings, slot
  claims (`B1`) and a verdict log (`V1`). A run vouches for a record by identity (`by`), by its transcript or, weakest
  and bounded, by time.
- **Antigravity reviews are enforced.** The Stop hook and the review commands read the subagent transcripts back into
  the ledger (`craftsman/agy_sync.py`), and a hunter that claimed a slot and left it unfinished is sent back once.
- **A hook error log.** Whatever a hook survives (a crash, an unreadable transcript, a git call out of budget, a ledger
  it cannot load or had to set aside, a transcript without provenance) is appended to
  `<git-dir>/craftsman/hook-errors.log`; `review check`, `review report`, `gate --complete` and `status` print
  `Hook errors since round N was prepared: K (see <path>)`. Every git call of a hook shares the budget of its event.
- **`gate --await-user --question "..."`** stores the time and the question; the Stop hook logs an `await-user` entry
  when the last message of an agent that parked the gate asks nothing.

- **Scout:** `craftsman review scout <H#|path:line|symbol>` prints the base version of the enclosing
  scope, callers, and tests, deterministically and capped at 12,000 characters, so hunters need not open
  whole files.
- **`craftsman review show --hunks`** lists the hunks still to cover, and `review show` prints each
  finding's scenario and verdict evidence.
- **Agent usage.** Where the platform hands over a subagent transcript, tokens and agent time are
  recorded per subagent, and `craftsman review report` and `craftsman status` total them per role
  (hunters, verifiers).
- **Inbox fallback.** When a heredoc needs approval, subagents write their JSON to the path that
  `craftsman review inbox` prints and pass `--from-json <path>`.
- **Public seeded-defect suite** in [`evals/`](evals/README.md): six small Python projects with 15 seeded
  defects and decoys, run with `python3 -m evals.run` and described in
  [`docs/benchmark.md`](docs/benchmark.md).

### Changed

- **`review prepare` prints the plan.** After the summary it prints the depth with its reasons (`Depth: deep
  (<reasons>)`), the slot table (slot, hunks, lenses, state) and the one-line launch prompt for each pending slot.
- **`--hunter` is required in an enforced round.** `review add` and `review cover` refuse a record without
  `--hunter <slot>` (or `lead`) there; the main session can record only as `lead`, which never credits coverage.
- **`review report --partial` is refused when no hunter was launched** in an enforced round, and it never ends the
  review guard: only a report of a review that passed does. It still needs `--review-only` and a round.
- **The hook timeouts of Antigravity are 30 s** (`hooks.json`, they were 10 s), and the git calls of a hook share the
  budget of its event.
- **The skills, agent templates, `AGENTS.md` block, rules and docs describe the enforced hunt.** `/craft-review` launches the slots `prepare` lists with the one-line prompt and follows `review check`; the hunter and verifier
  templates start from `review brief` and record with `--hunter`; how-it-works, guards, usage and the architecture
  reference describe the plan, enforcement, attribution, the command policy and the hook error log.
- **Internal restructuring, no behavior change.** `craftsman/review.py` is split by responsibility
  (`review_parser`, `review_common`, `review_scope`, `review_ingest`, `review_records`, `review_inspect`,
  `review_present`) and stays the entry module; `WorktreeIndex` moved from `diffscan.py` to `worktree_index.py` and the
  path classifiers to `diffscan_paths.py`; `usage.py` and `transcript.py` share one lazy JSON Lines reader
  (`jsonl.py`); the one visible difference is that an Antigravity transcript larger than 256 MB is now refused as
  unreadable (the hooks fail safe and log it) instead of being read in full, as the Claude usage reader already did. The output of every `craftsman review` command is pinned by golden files (`tests/golden/review`)
  recorded from the code before the split. `lenses.select_lenses` and `deep_hint`, the earlier suggestion API, are no
  longer called by any command.
- **The `audit-*` skills load their review lens instead of repeating its probes** (`craftsman skill show craft-review
  --lenses <id>`) and keep only their own inventory table and method.

- **Diagrams are Excalidraw scenes again.** The five Mermaid diagrams (workflow, evidence ledger, finding
  lifecycle, hooks and guards, stop gate) are redrawn for the 5.0 protocol as editable
  `docs/diagrams/*.excalidraw` scenes with rendered PNGs, and the README shows the workflow diagram again.

- **BREAKING: the review skill is now `/craft-review`** (was `/code-review`), because Claude Code ships a
  built-in `/code-review` and the two names collided. The skill lives in `skills/craft-review/`, and the
  playbooks are read with `craftsman skill show craft-review --lenses <ids>`. There is no alias:
  `craftsman skill show code-review` fails with the unknown-skill error that lists the valid names.
- **Upgrade cleanup of the old `code-review` skill**: `craftsman install` and `craftsman uninstall` remove
  the `code-review` skill an earlier version put in the Antigravity, Copilot and Claude Code folders, but
  only when Craftsman owns it (an install manifest records it, or its files carry the generated marker);
  a `code-review` skill of your own is kept. `craftsman doctor` reports a leftover one.
- **BREAKING: "Build freely, review hard" redesign.** Recall dropped after 3.3.0 because the review
  was optimized for cost and for satisfying the ledger. The agent now builds without review loops or
  ledger bookkeeping; the gate judges outcomes on the final diff only: the diff was frozen, fresh-context
  hunters covered every hunk, every finding has a verdict with checkable evidence, and the tests ran on
  the final code.
- **New review protocol** (`/craft-review` is the single source; `/craftsman` follows it):
  `review prepare` freezes the diff and prints a risk map, the orchestrator writes an intent brief and
  an adaptive hunt plan, and `craftsman-reviewer` hunters (recall) run in parallel, followed by
  `craftsman-verifier` subagents (precision). While a wave produces a `proven` (reproduced) finding,
  another wave runs (a variant hunter per proven root cause plus a fresh unanchored hunter), at most
  3 waves per round; a `probable` finding alone does not start one. There is no inline-only review: every review uses a fresh-context hunter where the
  platform has subagents. The prompts are rewritten to teach how to hunt and how to judge, and are much
  shorter.
- **No flags for users.** `/craftsman <task>` and `/craft-review <plain words>` ("review PR 42 and fix
  what you find", "review my staged changes", "be thorough") are enough: the agent infers target, scope,
  fixing, and depth. Flags remain optional shortcuts for agents and power users.
- **One adaptive depth.** The agent goes deep on its own for security-relevant code, concurrency or
  clocks, persistent data or migrations, public API or file formats, removed guarantees, and large or
  cross-cutting diffs, or when the user asks for care. `review prepare` prints a
  `Depth: deep suggested (signals: ...)` hint, and `review prepare --deep` inside a gate session raises
  the gate to deep for good. Depth never goes down. `ultracraft` is `/craftsman --deep`.
- **Cheaper bookkeeping.** `review cover` takes `--hunks` or `--files` (every open hunk of those files)
  and one free-text note of at least 20 characters. There are no per-hunk note segments, no 80-character
  rule, and no batch-size limit.
- **Ledger schema 3** and a simpler gate. Coverage records hold hunk digests and a note, with no lens;
  a hunk is covered when this round or an earlier one holds its digest. Gate state and rounds carry
  `deep: bool` instead of a tier. The gate issues are `no-round`, `stale-round`, `uncovered`,
  `no-verdict`, `unresolved`, `no-passing-run`, `no-e2e-run` (deep only), `failing-run`,
  `no-isolation`, and `round-cap`. A very large diff is now a warning in `prepare` and the report, not
  a `needs_human` hand-off. Whenever the platform provides isolation evidence, the coverage of every
  round must come from a finished `craftsman-reviewer`, in gate sessions and in review-only mode.
  Owner binding, `MAX_CONTINUATIONS`, the round cap, run rules, evidence rules, and the frozen
  review-only scope are unchanged.
- **Restored risk triggers.** `filesystem` signals suggest `state`, `trust`, and `platform` again, and
  `process` suggests `platform`. Both were narrowed in 3.3.0.
- **Larger review packet.** One 120k budget and one format for every round. Enclosing scope bodies are
  always included (subject to the trimming ladder), callers are capped at 6 per symbol instead of 3,
  and risk signals show per hunk with the lenses they suggest.
- **Cheaper reviews.** The packet shows each hunk once, inside its enclosing scope at HEAD, with `+` and
  `-` marks and `[H3]` markers. The packet and the hunter views share one layout with no lens table, no
  recording cheat sheet and no empty sections, which makes a small diff's packet about 30% smaller; test
  files a listed caller already names are not repeated.
  Hunters and verifiers get briefs that start with an identical shared block (served from the prompt
  cache), then the slice, then the lenses. Where agents run in the background (Claude Code, Antigravity
  async subagents) a verifier starts when its hunter finishes, and a variant hunter and the fresh hunter
  when a finding is judged `proven` (the fresh hunter starts with wave 1 when deep); variant hunters get
  a mini-slice. On Claude Code the simplicity hunter (`craft`, `design`) and every verifier whose
  cluster holds only `minor` or `nit` findings run on a cheaper model. The orchestrator ends its turn while
  background agents run instead of polling for them (a leftover timer kept one benchmark session open for
  8 minutes after its review was done). The orchestrator writes
  the intent brief from `git diff <base>` and the risk map, not from the full packet. The SubagentStart
  briefing carries the round, the packet path and the open work, without the packet.
- **Fix confirmation is separate from the regression check.** A `proven` finding is confirmed by a
  regression test that fails before the fix and passes after; a `probable` one by the resumed verifier
  that judged it (or a fresh verifier given the fix diff). The regression check is a fresh, unanchored
  hunter on the fix-delta round plus the test run.
- **Gate behavior.** Hunk ids are stable and never reused, so a round's ids can be sparse. `review cover`
  skips carried hunks, and `--files` accepts `./x`, backslashes, absolute paths, and the old path of a
  renamed file. A reviewer must record coverage or a finding before it stops. Deep needs both a test run
  and an e2e run. With no isolation evidence the gate closes with the `SELF-REVIEWED` banner, deep or
  not. Isolation is checked per round, and on Antigravity reviewers are recorded when the turn ends.
  `ultracraft` inside `/craft-review` means a deep review and never opens a gate. Every review
  launches at least one hunter wherever the platform has subagents.
- **Docs** rewritten around plain-language usage; `docs/harness-architecture.md` roughly halved. The
  Excalidraw diagrams (which still showed tiers and per-lens coverage) are replaced by Mermaid diagrams
  in `docs/how-it-works.md`, `docs/guards.md`, and `docs/harness-architecture.md`, and the README
  shows a short text flow. `docs/how-it-works.md` is the one full explanation of the protocol, its
  lens table follows `craftsman/lenses.py`, and the depth triggers live only in the `craft-review`
  skill.

### Removed

- **The `mentioned` rule of the usage reader:** a command counts as run only when a recognized recording call printed it.
- **Four skills unrelated to the review loop:** `leetcode-style-code-reviewer`, `code-quality`,
  `high-level-commit-message` and `extreme-optimizer`. `craftsman install` removes the copies, links and
  commands that an earlier install recorded in its manifest (and nothing it did not create); `/audit` no longer
  lists the `quality` and `perf` aspects, and `/craftsman` proposes the commit message itself.
- **The migration of Craftsman < 4.0 installs** (`legacy_githooks.py`, `TargetKind.GIT_HOOKS`, the `schema_version: 1`
  manifest reader and the restore of `core.hooksPath`). A manifest of that age now stops `install`, `uninstall` and
  `doctor` with a message to delete it and run `craftsman install` again.
  `craftsman install` reads the manifest of every selected target before it writes anything. `craftsman doctor`
  still reports a global `core.hooksPath` that points to the git hooks folder of those releases. The manifests this
  version writes no longer carry the retired `previous_hooks_path` and `hooks_dir` keys, so Craftsman 5.0 cannot read
  them back: reinstall with 5.0 after a downgrade.

- **Tiers.** The `quick` / `standard` / `deep` tiers, `--tier`, and automatic tier selection (including
  the review-only `quick` default up to 250 changed lines). `--quick` and `--standard` right after
  `/craftsman` are still accepted and ignored (stripped from the task text).
- **Commands and flags.** `review ack`, `review fact`, `review prepare --add-lens`,
  `review add --signal`, `review cover --lens` (and the `"lens"` / `"lenses"` JSON keys),
  `review view --lenses` and `--shard`, and the `R1`-`R4` reviewer groups in
  `craftsman skill show craft-review --lenses`.
- **Concepts.** Signal acknowledgements, shared facts, lens additions, per-lens coverage, per-lens views,
  reviewer shards and groups, and the gate issues `tier-too-low`, `sweep-too-early`, the quality-signal
  issues, and design-file coverage. Quality signals (`S#`) stay as display hints.
- The `{{reviewer_groups}}` placeholder in `templates/AGENTS.md`.

### Fixed

- **Review of the hook guards (Antigravity and the shell parser).** `env -S "<command line>"` (and `--split-string`, in
  every attached form and as the shortest unique prefix `--s`, with `\_` as a separator; `env -a`/`--argv0` values and an
  `env -C` inside the string are read too) no longer hides its command from the command policy, the work-tree guard, the scope guard and
  the triviality check; `python craftsman/cli.py ...` is read as the CLI, and the `__main__` blocks of `gate.py` and
  `export.py`, which ran around the policy, now only say that the module is no program (exit 2). Antigravity's `read_file(Path=...)` is wired to the scope guard
  and judged like `view_file`, and a configured `ANTIGRAVITY_APP_DATA_DIR` or `CLAUDE_CONFIG_DIR` is protected for
  reads as it is for writes. An Antigravity verifier that stops while the gate still waits for verdicts, and a hunter
  that claimed no slot and recorded nothing, are sent back once, as on Claude Code. Launches that Antigravity cut from
  its compact transcript are read from `transcript_full.jsonl`; a cut that cannot be restored is reported, never read
  as "no launch". The cited source line of `review add` and `review move` is printed defanged, and an `echo` or `printf`
  that could build the phrase of a record next to a recording call makes its output foreign. A hunter that covered
  parts of several slots without a brief is dead for one slot only. `FOO=1 true` is as trivial as `true`.
- **Long Antigravity requests and stdin filters.** A `/craft-review` request or continuation prompt that Antigravity
  cut in `transcript.jsonl` is read whole from `transcript_full.jsonl` (one reader, `transcript.read_conversation`); one
  that cannot be restored changes nothing and is reported, and so is a stop allowed only because a cut response could not
  be read. `cat`, `head` and `tail` next to a recording call are foreign output unless they filter a pipe and have no
  input of their own, so they can no longer forge a record token.
- **Antigravity reviews are read back correctly.** Parallel `invoke_subagent` calls, whose acknowledgements carry no
  id, are paired with their calls in call order and attributed to the right subagent; verifier runs are recorded like
  hunter runs; `gate --init` records the Antigravity conversation as the gate owner.
- **The depth floor of a review comes from the diff again.** `review prepare` makes a round deep on a security or
  concurrency signal in a production hunk, or at least 251 changed production lines, on every platform and whatever
  effort the agent runs at (5.0 lost this floor); `--deep` still raises it.
- **A test run must have run tests.** `craftsman run --kind test` reads the whole output of the command for the
  summaries of pytest, unittest, `go test`, `cargo test`, jest, vitest and mocha, adds them up and stores the count
  (`tests_ran`, with `min_tests`). A run that exited 0 but reported fewer tests than `--min-tests` (default 1;
  `0` turns the check off) is still recorded, but is no passing evidence anywhere: the gate says "R3 ran 0 tests
  (minimum 1)" and the progress counter ignores it. Output no runner claims keeps the exit code as the only
  judge. A run refused for too few tests says how to proceed (`--min-tests 0`), and a run that passes only
  because of `--min-tests 0` is a gate warning. The run summaries show "exit 0, 0 tests - not counted". `pytest
  --collect-only` and an all-skipped `go test -v` count as 0 tests. An inline Python program (`python -c`, or a
  heredoc fed to `python -`) that checks nothing (no `assert`, `raise`, test framework, `subprocess.check_call`,
  `subprocess.run(check=True)`, `runpy` call or computed exit status) is now trivial as a `test` run; `e2e` runs
  keep the old rule. A newline in an inline shell script now separates commands, as `;` does.
- **`craftsman status` never hides a broken ledger.** A corrupted ledger, a ledger of a newer schema and the
  ledgers an upgrade set aside are reported with their path (text and `--json`: `ledger_path`, `ledger_problem`,
  `ledger_backups`); a corrupted or newer ledger makes `status` exit 1. `status` only looks: it no longer sets an
  older ledger aside.
- **A ledger written by a newer Craftsman is never moved or rewritten.** Every command, `review reset` included,
  stops with "this ledger was written by a newer Craftsman (schema N); upgrade craftsman". Unknown fields in a
  same-schema ledger are named in the error, which says a newer Craftsman may have written them.
- **Set-aside ledgers are never overwritten.** An older ledger now moves to
  `ledger.v<schema>.<UTC yyyymmddThhmmssZ>.bak.json` (with a counter before `.bak` if that name exists), so two
  upgrades keep both backups.

- **The host's `/code-review` no longer arms the review stop guard** (nor opens a gate). Only Craftsman's
  `/craft-review` and `/craftsman:craft-review` do; there is no alias for the old name.
- **The review stop guard agrees with `craftsman review check --review-only`.** It uses the same isolation
  evidence (on Copilot, where no session id reaches the CLI, the two used to disagree), and re-running
  `review prepare` on an unchanged diff or raising its depth counts as starting the review instead of repeating
  "no review round yet". A session that waits for a background reviewer or verifier is no longer sent back and no
  longer spends reminders; benchmark reviews used to be stopped 2 to 6 times for that.
- **Review-only isolation evidence is per review, not per ledger.** A subagent event from an earlier session or
  platform no longer makes a terminal or hookless review need a reviewer run it can never record; hooks count only
  for the session that invoked `/craft-review`, or once a subagent event follows the current round.
- **Coverage must come from a `craftsman-reviewer`, in every round.** The latest round used to pass once any
  reviewer had recorded anything, so the orchestrator could cover the remaining hunks itself; now every covered hunk
  needs a reviewer behind its coverage, and the gate names the hunks that lack one (a reviewer re-covers them, no
  reset needed). Findings need no reviewer, so an intent finding the orchestrator records no longer fails an earlier
  round. A round replaced before any review holds no coverage and needs nothing; a reviewer that finished after the
  next round was prepared still counts for what it recorded.
- **Claude Code reviewers are identified by their own transcript, not by time.** `review cover` prints a token
  (`Coverage recorded (C12): ...`, as findings print `F3 recorded`), and the stop hook stores the tokens the
  reviewer's own transcript shows on its stop event, so the orchestrator's records written while a background
  reviewer runs no longer count as the reviewer's. A record counts only inside its reviewer's start-to-stop window.
  The reader fails safe: without a readable transcript (Copilot, Antigravity) or with a token that no recognized
  `review cover|add` call printed, the gate goes by the window alone instead of concluding the reviewer recorded
  nothing. A stop whose start cannot be matched by agent id (Copilot starts by name only) pairs first in, first out
  with the unpaired starts of its name.
- **A background agent's report no longer ends the review guard.** Claude Code delivers a background subagent's
  report as a prompt, which the prompt hook took for the user moving on: it disarmed the `/craft-review` stop guard
  (a session could then stop with findings unjudged) and reset an active gate's continuation count. Such prompts
  now count as the platform's, like a stop hook's continuation.
- **Agent time counts active time.** A subagent's `duration_s` adds up the gaps between transcript entries of
  at most 5 minutes instead of spanning first to last entry, so a resumed agent no longer counts the time it
  sat idle between runs.
- **The "session started" text after a needs-human restart** says `deep` when the restarted gate is deep.
- **`evals` `agent` continues run numbering** per arm and case from the results file and the logs folder. Repeats
  added later no longer restart at 1 and replace earlier runs, logs and kept ledgers (which skewed `rescore` and
  the ranges of `compare`).
- **Safer upgrades from Craftsman 4.x.** The old gate state and ledger are set aside only under their lock, after
  reading the file again, so a 5.x gate or ledger written in between is never renamed to the backup.
- **`review prepare` no longer holds the ledger lock while it parses the changed Python files** for quality hints,
  so a `SubagentStop` event that proves isolation cannot time out behind it.
- **A reviewer's stop event is written before its transcript is read** for token usage; a hook killed while reading
  a huge transcript no longer loses the isolation evidence.

### Migration

- **Old ledgers and gate state migrate on their own.** The first command that reads a pre-5.0 gate state
  or a schema 2 ledger archives it (`craftsman_state.json.v4.bak`, `ledger.v2.bak.json`) and starts
  fresh, so neither `gate --init --takeover` nor `review reset` is needed. Nothing is converted. Re-run
  `craftsman install` to refresh the skills, agents, and `AGENTS.md` block.
- `gate --init --tier deep` and `review prepare --tier deep` become `--deep`. `--tier quick` and
  `--tier standard` become nothing: omit the flag and the agent chooses.
- `review cover --lens a,b --hunks H1,H2 --note "..."` becomes
  `review cover --hunks H1,H2 --note "..."`; drop `"lens"` / `"lenses"` from `--from-json` entries.
- `review add --signal S#` and `review ack S#`: drop them. Mention the point in a finding if it is a
  real defect.
- `review prepare --add-lens`: assign the lens to a hunter in the hunt plan instead.
- `review view --shard K/N` and `--lenses`: use `--files <csv>` or `--hunks <ids>` to give each hunter
  a slice; the lenses go in the hunter's brief.
- Scripts that read `tier` from `craftsman status` or the state file: read the deep flag.

## [4.1.0] - 2026-11-02

### Added

- **Modular documentation (`docs/`)**: Reference material moved from `README.md` into modular guides:
  [`docs/how-it-works.md`](docs/how-it-works.md), [`docs/installation.md`](docs/installation.md),
  [`docs/usage.md`](docs/usage.md), [`docs/guards.md`](docs/guards.md), [`docs/skills.md`](docs/skills.md),
  and [`docs/benchmark.md`](docs/benchmark.md). `README.md` is rewritten as a short overview: what the gate
  enforces, the review loop step by step, supported platforms, and the pilot measurements.
- **Claude Code plugin**: The repository root is a Claude Code plugin and its own marketplace
  (`.claude-plugin/marketplace.json` with `"source": "./"`), installed with
  `/plugin marketplace add BuczynskiRafal/craftsman` and `/plugin install craftsman@craftsman`. Skills and
  the stdlib-only `craftsman/` package are used in place; `bin/` holds the POSIX and Windows launchers,
  which look for Python 3.10+ and put `craftsman` on the agent's `PATH`.
  `craftsman export --claude-plugin [--check]` regenerates (or verifies) the manifest with its hooks, the
  catalog and the rendered agents in `.claude-plugin/`; CI fails when they are out of date.
- **Namespaced plugin triggers**: Recognizes `craftsman:craftsman`, `craftsman:ultracraft`, `/craftsman:craftsman`,
  and `/craftsman:ultracraft` in prompt text and Claude Code `<command-name>` hook payloads.
- **Doctor duplicate Claude hook check**: `craftsman doctor` inspects both `~/.claude/settings.json` and
  plugin state (`installed_plugins.json` / `enabledPlugins`) to flag duplicate active hooks that would fire twice.
- **Install guard against duplicate Claude hooks**: `craftsman install --claude` (and `export --claude-dir`)
  refuses to install user-level hooks when the Claude Code plugin is active unless `--allow-duplicate-scopes`
  is supplied; `--no-claude-hooks` installs the rest beside the plugin. An auto-detected Claude Code is skipped
  with a note instead, so a plain `craftsman install` still sets up the other platforms.
- **Antigravity plugin duplicate guard**: `agy plugin install https://github.com/BuczynskiRafal/craftsman` is
  documented. While that plugin is enabled, `craftsman install` skips an auto-detected Antigravity and refuses
  `--antigravity` (copy or symlink) unless `--allow-duplicate-scopes` is supplied, and `craftsman doctor` reports
  the plugin beside a copy install. Craftsman's own `--plugin` install is not mistaken for the agy plugin.
- **Ephemeral `uvx` interpreter refusal**: `craftsman install` detects if running under an ephemeral uv cache
  interpreter (`.cache/uv/archive-...`) and refuses installation with instructions to use `uv tool install craftsman-gate`
  or `pipx install craftsman-gate`, preventing broken hook paths when uv cleans its cache. The interpreter path
  is checked as the hooks embed it (uvx's `bin/python` is a symlink out of the cache), and only for targets
  whose hooks embed it.
- **Automated release workflow & CI wheel test**: Added `.github/workflows/release.yml` with tag validation,
  isolated virtualenv smoke tests, and PyPI Trusted Publishing (OIDC). Wheel smoke test is mirrored in CI.
- **Automated documentation link checker (`tests/test_docs_links.py`)**: Tests all relative file links,
  section anchors, and raw GitHub diagram URLs across `README.md` and `docs/*.md`.
- **Version consistency check (`tests/test_version.py`)**: Enforces exact version synchronization across
  all repo manifests (`pyproject.toml`, `plugin.json`, `craftsman/__init__.py`, and plugin manifests).
- **One-line prose in the ledger and reports**: Finding titles, scenarios, suggestions, coverage notes,
  verdict evidence, resolution notes, facts and reasons are flattened to one line on the way in, including
  literal `\n` / `\t` / PowerShell `` `n `` sequences (with a warning). Code spans, Windows paths and quoted
  escapes such as `newline="\n"` are kept. Every Markdown renderer applies the same rules, so ledgers
  written by older versions render correctly too.
- **Managed scratch directory (`craftsman review scratch`)**: Prints a fresh `<git-dir>/craftsman/scratch/<id>/`
  for repro and verification scripts; removed by `gate --complete`, `review reset` and the final
  review-only report.
- **Out-of-scope change warning**: `review report` and `review check --review-only` list files created or
  changed outside a frozen review scope since the review started.
- **Stop-hook summary line**: The continuation reason now starts with one line naming what blocks the gate,
  e.g. `review incomplete: 3 hunks not covered by lens contracts; 1 finding without a verdict`.

### Changed

- **PyPI distribution package rename**: Distributed as `craftsman-gate` on PyPI (`pipx install craftsman-gate`,
  `uv tool install craftsman-gate`). Runtime import `import craftsman` and CLI command `craftsman` remain unchanged
  with zero third-party dependencies (`dependencies = []`). Both distributions install the same files, so
  remove a `pip install .` of an older checkout (`pip uninstall craftsman`) before installing `craftsman-gate`;
  `craftsman doctor` reports the old distribution when both are installed.
- **Tagline and messaging cleanup**: Unified project tagline to "Make AI coding agents prove they reviewed and tested their change"
  across all files, eliminating ungrounded marketing buzzwords.
- **Canonical MIT license and NOTICE**: Canonical MIT text preserved in `LICENSE`; detailed attribution and
  project notices separated into `NOTICE`.
- **Prompt hook failures**: A failing prompt hook tells the user and the agent that a `/craftsman` prompt did
  not open the gate.
- **Windows CI**: A `windows-latest` job runs lint, type checks, tests, `doctor` and an installer dry run.

### Fixed

- **Antigravity plugin manifest**: `plugin.json` lists its skills as `./skills/<name>` paths and no longer
  carries the `agents` and `rules` fields, as the current agy loader reads it (`agy plugin validate`
  reported every entry as missing and loaded the plugin only from its default folders). `craftsman doctor`
  checks the paths against `skills/`.
- **Unrendered agents in the agy plugin**: The agent templates moved from `agents/` to `templates/agents/`.
  Plugin loaders read a repository's `agents/` folder as finished agents, so `agy plugin install` gave
  Antigravity three agents whose bodies were bare `{{include ...}}` lines, the orchestrator without any of
  its instructions. The rendered Claude Code, Copilot and plugin agents are unchanged.
- **Antigravity `--plugin` install was never loaded**: `craftsman install --antigravity --plugin` linked
  `~/.gemini/config/plugins/craftsman` to the checkout, and agy skips a plugin folder that is a symlink, so
  Antigravity had neither the skills nor the gate hooks it had just taken out of `hooks.json`. The plugin is
  now a copy of what agy loads (`plugin.json`, `hooks.json`, `skills/`, `rules/`, `scripts/` and the
  package), without the unrendered agent templates, and `craftsman doctor` reports it when it falls behind
  the checkout.
- **UTF-8 through pipes on Windows**: The CLI and the Antigravity hook scripts read and write UTF-8 when
  stdin/stdout are pipes, as the agents do. Before, Python used the ANSI code page there: a `ł` in a
  prompt arrived garbled, every `→` of a report left as `?`, and the Antigravity hooks could crash on a
  path they could not encode. `git config` output and the interpreter check of `install` are decoded as
  UTF-8 and raw path bytes, so a home such as `C:\Users\Rafał` works.
- **VS Code folder without `APPDATA`**: On Windows `install`, `uninstall` and `doctor` fall back to
  `<home>\AppData\Roaming` when `APPDATA` is unset or points outside the given home, instead of failing.

## [4.0.0] - 2026-10-30

### Added

- **Cleanup of legacy 3.x global Git hooks**: `craftsman install` and `craftsman uninstall` automatically
  remove global Git hooks installed by Craftsman 3.x and restore `core.hooksPath`.

### Changed

- **Google Antigravity hooks**: Scope guard (`craftsman-scope-guard`) replaces the legacy git guard in `hooks.json`.
- **Syntactic trigger detection**: Trigger detection for `/craftsman` and `ultracraft` outside quotes and backticks
  is purely syntactic.

### Removed

- **Git policy enforcement**: Removed Git commit, push, and AI attribution policies (`craftsman guard`,
  global `commit-msg` / `pre-push` hooks, `--git-hooks` / `--no-git-hooks`, and `rules/git_workflow.md`).
  Attribution and branch protection should be configured in user/agent instructions or remote repository settings.

## [3.3.0] - 2026-10-29

### Added

- **Frozen review-only scope (`--staged`, `--tracked`, `--paths <csv>`, `--add-paths <csv>`)**:
  standalone `craftsman review prepare` pins the matched file paths (including both paths of
  renames) in `Round.scope`, filters tracked diffs with literal pathspecs and untracked scans across
  `review`, `hooks`, `runner`, and `status`, reports out-of-scope edits as `Ignored (outside the
  review scope): …` without marking an active round stale, allows monotonic scope expansion via
  `prepare --add-paths <csv>` for files touched during `--fix`, and hints to run
  `craftsman review reset` when an earlier review is already complete.
- **Deterministic reviewer shards (`craftsman review view --shard K/N`)**: `review prepare` reports
  `Suggested reviewer shards: N`, `review view --shard K/N` partitions open hunks into file-disjoint
  shard views capped at `60,000` characters (`MAX_VIEW_CHARS`) with a `Files in this shard:` header,
  and `/code-review` caps concurrent subagents with `--parallel <N>` (default `2`).
- **Multi-lens and `quick` file-level coverage**: `craftsman review cover --lens a,b,c` (and
  `"lenses": [...]` in `--from-json`) records atomic coverage across multiple same-target lenses
  (`>= 80`-character concrete segments per target when combining two or more lenses), and `quick`
  rounds accept `--files` coverage for every selected lens.
- **On-demand lens playbooks (`craftsman skill show code-review --lenses <csv>`)**: prints the
  shared playbook preamble plus only the requested lenses or `R1`–`R4` reviewer groups.
- **Partial review-only reports and compact hunk maps**: `craftsman review report --review-only
  --partial` exits `0` with `PARTIAL (review-only, N open items)`, a `## Not covered` section, and
  `(unverified)` finding markers when a subagent fails after one retry; `craftsman review show
  --hunks` prints a one-line-per-hunk `H# -> path:lines` map for verifiers and inline `sweep`.
- **Pre-tool worktree mutation guard**: `craftsman hook pre-tool` (`guard.find_worktree_mutation`)
  blocks `git stash` (except `list`, `show`, and `create`), `reset`, `checkout`, `switch`,
  `restore`, and `clean` (plus `commit`, `merge`, `rebase`, `cherry-pick`, `revert`, `am`, `apply`,
  `pull`, `rm`, and `mv` for subagents) — denying calls from `craftsman-reviewer` and
  `craftsman-verifier` and asking for confirmation in the main session during an open review-only
  round on platforms with `PreToolUse` hooks.
- **`.git/craftsman` packet and archive pruning**: preparing a new round deletes superseded
  `packet-<k>.*.md` views and older `packet-<k>.md` packets (`prune_round_files`), and archival
  retains the newest `CRAFTSMAN_KEEP_ARCHIVES` (default `5`) `archive-*` directories and
  `ledger-*.json` snapshots.

### Removed

- `craftsman gate`: `--pass-*`, `--mode`, `--stage`, `--state-file` (also on `craftsman status`),
  the stdin mode and the `--soft` alias. `craftsman review view`: the `--lens` alias.
- `craftsman install`: `--copilot-prompt-files`, `--claude-hooks`, `--codex-workspace` and
  `--cursor-workspace`. `craftsman export`: `--copilot-prompt-files` and `--claude-hooks`.
- `craftsman doctor --fix-duplicates`; `doctor` still reports duplicates and which copy to keep.
- Every compatibility path for older files: a gate state, ledger or install manifest in an older
  format is an error, and uninstall no longer sweeps Copilot files no manifest records.

### Changed

- **Review-only tier, `quick` lenses, and narrower `standard` triggers.** Without an active gate,
  `craftsman review prepare` defaults to `quick` up to `250` changed lines (`REVIEW_ONLY_QUICK_MAX_LINES`,
  and for docs-only diffs; skipped binary or oversized files force `standard`) and `standard` above
  `250` lines, suppressing the `quick` diff-size warning up to `250` lines in `--review-only`.
  `quick` runs `intent`, `contracts`, and `failure` inline with zero subagents (plus `trust` on
  `sql`, `deserialization`, `auth`, or `process` signals; `craft` moves to `standard`/`deep` and
  docs-only, and `sweep` does not run on `quick`). On `standard`, `filesystem` no longer triggers
  `state`, `trust`, or `platform`, and `process` no longer triggers `platform`.
- **Packet budgets, lean subagents, and conditional verifier.** `MAX_PACKET_CHARS` is now `120,000`
  (`40,000` on `quick`, where packets omit enclosing scope bodies in favor of line-range pointers),
  with progressive trimming across scopes, caller references (capped to `3` per symbol), and omitted
  hunk diff bodies (`## Omitted hunk bodies`). `craftsman-reviewer` and single-file `/code-review`
  exports no longer inline `lenses.md`; `craftsman-verifier` runs only when unjudged findings exist
  and reads `review show --hunks` instead of `packet-N.md` (with `sweep` performed inline when
  review-only `standard` records zero findings).
- **Ledger schema v2.** `SCHEMA_VERSION` is now `2` (`Round.scope`); older schema `1` ledgers fail
  with instructions to run `craftsman review reset`.
- **Gate.** The state requires an owner; an invalid state is an error until
  `craftsman gate --init --takeover` replaces it. Every action refuses another session's gate
  (`--session-id` names the caller's). All stop hooks allow at most six continuations without
  fewer open issues; Antigravity keeps the state in the git directory and proves reviewer
  isolation only through finished `invoke_subagent` runs. Re-running `--init` never lowers the
  tier, and an `auto` task with a binary or oversized file gets `standard`.
- **Hooks fail closed.** `pre-tool` asks the user (Copilot denies) when it cannot check a call; an
  invalid hook command line exits 2; the git guard denies input that is not a JSON object and
  asks when a message file cannot be read; `--git-hook` usage errors exit 2. A transcript line
  that is not a JSON object, an unknown `craftsman.scope` value and an Antigravity Stop payload
  without `terminationReason`/`fullyIdle` are errors. Only the exact names `craftsman`,
  `craftsman-reviewer` and `craftsman-verifier` count as Craftsman agents.
- **Ledger and review.** `ledger.json` must contain every section and field, and records must
  name signals and hunks of their own round. `review view` refuses a stale round or an unselected
  lens; the `design` lens covers files only. Merging findings about different signals is refused.
  `review report` lists exactly the runs the gate counts and each acknowledged signal once.
- **Packets and quality signals.** Long scopes are trimmed with `[… Lx–Ly omitted …]` markers; a
  deletion is scoped to its function. A Python file that does not parse is a `parse-error`
  signal, a 41-line function is `long-function`, and `return a, b` no longer equals `return a`.
  `*.spec.ts`, `*_test.go`, `spec/` and `__tests__/` count as tests; only prose under `docs/` is
  docs. The `code_quality` and `extreme_optimizer` skills are `code-quality` and
  `extreme-optimizer`.
- **Doctor.** Reports a skill folder not named after its skill, `plugin.json` mismatches or
  repeats, unreadable settings, unparsable hook commands, an unreadable git config, and git hooks
  whose Python is gone. JSON: Copilot hooks as `state`/`error`, Claude hooks as `hooks`,
  `git_config` as `user_name`/`user_email`/`hooks_path`, one `vscode_instructions` target; no
  `cli_self_check` or `fixed_duplicates`.
- **Install and uninstall.** Each target records exactly what it created and merged into in
  `.craftsman-install.json`; uninstall removes only that, checks every recorded path before
  changing anything, and its `--dry-run` reports what a real run does. A user's file in the way
  moves into a `.craftsman-backup` folder next to it; merged files keep a one-time `.bak`.
  A failed target records nothing; files to merge into are checked first. Uninstall has its own
  flags; `--copy`/`--symlink`/`--plugin` no longer select Antigravity, and `--antigravity` no
  longer reinstalls git hooks.
- **Git hooks.** They source only their installed `_passthrough.sh`, block the commit or push when
  the guard or `.python_cmd` is missing, stay where their manifest records them, and set
  `core.hooksPath` only once the manifest holds the value it replaced.
- **Export.** Generated Markdown carries `<!-- craftsman:generated -->`, and only marked files
  count as Craftsman's; a user's file in the way is moved aside. `--user-dir`,
  `--copilot-skills-dir` and `--copilot-home` each write only their part. Only the exact hook
  commands Craftsman writes count as its Claude Code hooks. A symlinked `AGENTS.md` is updated
  where it points inside the workspace and refused outside it; markers that do not enclose one
  block are an error. A SKILL.md needs frontmatter with `name` and `description`. VS Code folders
  follow the operating system (`APPDATA`, `~/Library/Application Support`, `XDG_CONFIG_HOME`).

### Fixed

- A second `craftsman review fact add` failed; `env -u`/`-C`/`-S` values hid a trivial run.
- The ledger and gate state are flushed to disk before they replace the old file.

## [3.2.0] - 2026-10-27

### Added

- **Mechanical code-quality signals (`craftsman/quality.py`)**: the gate now measures function and
  module size budgets, diff growth (`diff-size`), test-naming hygiene (`test-name`), duplicate logic
  across AST bodies (`duplicate-logic`), and untyped dictionary domain parameters (`dict-domain`).
  Every signal appears in the review packet and must be resolved via `craftsman review add --signal
  S#` or acknowledged with a concrete code reason via `craftsman review ack S# --reason "..."`
  (shown to the user in the final report; `diff-size` cannot be acknowledged by the agent and hands
  the review to the user as `needs_human`).
- **Whole-change `design` lens** (`standard` and `deep`, covered per file with `--files`),
  **per-reviewer packet views** (`craftsman review view --lenses <group-or-lenses> [--hunks ...]`),
  **fixed reviewer lens groups (`REVIEWER_GROUPS`: `R1`–`R4`)**, and **source-cited shared facts**
  (`craftsman review fact add --hunks ... --source path:line --text "..."`, automatically
  invalidated when the source file changes).
- **Packet deduplication, scope trimming, and CLI-computed test/run facts**: shared enclosing scopes
  and symbol references are rendered once per round, scopes longer than 60 lines are trimmed around
  the hunk, and packets list test files referencing changed symbols plus the latest `test`/`e2e`
  runs with staleness.

### Changed

- **Coverage notes must be concrete**: `craftsman review cover` rejects notes made of generic praise
  (`looks good`, `well-factored`, `clean`, `lgtm`, etc.), requires every listed hunk or file to have
  its own segment naming a concrete input, condition, or `file:line`, and rejects notes reused
  verbatim across more than 3 hunks.
- **Reviews without isolation evidence are reported as self-reviewed**: in `deep` mode, missing
  platform hook or transcript evidence sets `needs_human` instead of completing; in `standard` mode,
  the gate and report display a `SELF-REVIEWED` banner.
- **`craftsman review report`** now uses the header `**Process: COMPLETE**`, flags process anomalies
  (zero findings across large rounds, burst coverage timestamps, environment-bypassed test runs),
  and ends with a `## Not assessed by the gate` section.

## [3.1.0] - 2026-10-27

### Added

- **Repository search-scope guard** (`craftsman/scope.py`, `craftsman hook pre-tool`, and
  `skills/craftsman/scope.md`): blocks recursive directory searches (`find`, `rg`, `grep -R`, `fd`,
  `ls -R`, `Glob`, `Grep`, `LS`, etc.) outside the active git work tree and blocks reading other
  git repositories, prior agent session logs (`~/.claude/projects`, `~/.copilot/session-state`,
  `~/.gemini/antigravity/brain`), or shell history during Craftsman sessions while allowing
  single-file reads outside other repositories/session stores, dedicated `$TMPDIR` subdirectories,
  and paths added via `git config craftsman.scopeAllow` (configurable with
  `git config craftsman.scope deny|warn|off`).
- **`craftsman skill {show,list,path}`** and **`craftsman review show --packet-path`**: read bundled
  skills, companion files (such as `lenses.md`), and the active round's review packet path directly
  from the CLI without searching the file system.
- **Copilot duplicate detection and repair** (`craftsman doctor` and `craftsman doctor --fix-duplicates`):
  scans `COPILOT_DISCOVERY` locations across VS Code profiles, `~/.copilot`, `.github`, and `.claude`
  to report duplicated Craftsman agents or slash commands, and removes obsolete Craftsman-generated
  copies while preserving unmarked user files and Claude Code installations.
- **`--copilot-prompt-files`** and **`--allow-duplicate-scopes`** flags on `craftsman install` and
  `craftsman export`, plus **`--takeover`** on `craftsman gate --init`.

### Changed

- **Prompt files (`*.prompt.md`) are no longer generated by default** for GitHub Copilot: slash
  commands are installed once as Agent Skills (`~/.copilot/skills/<name>/SKILL.md` for user scope or
  `.github/skills/<name>/SKILL.md` for workspace scope), and custom agents are installed once in
  `~/.copilot/agents/` or `.github/agents/`. Pass `--copilot-prompt-files` on older VS Code builds
  without Agent Skills support.
- Installing or exporting `--copilot-workspace` when a user-level `--copilot` installation already
  exists (or vice versa) now refuses by default with instructions to uninstall one scope or pass
  `--allow-duplicate-scopes`.
- Claude Code's `PreToolUse` hook matcher now covers `Bash|Read|Grep|Glob|LS` so both the git guard
  and the repository search-scope guard run across shell and file/search tools.

### Fixed

- **Duplicated agents and `/skill` slash commands in VS Code Copilot**: upgrading with
  `craftsman install --copilot` or uninstalling with `craftsman uninstall --copilot` cleans up
  legacy v3.0.0 `.agent.md` and `.prompt.md` files from `Code/User/prompts/` and
  `Code/User/profiles/*/prompts/` without touching user-authored files.
- **Cross-session gate interference in shared work trees**: `.git/craftsman/craftsman_state.json`
  now binds to the session (`owner.platform` + `owner.session_id`) that opened the gate. Parallel
  agent sessions in the same checkout are no longer blocked by `stop`, no longer increment
  `forced_continuations` or abort the owner's gate, and no longer overwrite `hooks_seen` or the
  active gate without `--takeover`.

## [3.0.0] - 2026-10-27

### Added

- **Evidence ledger** (`.git/craftsman/ledger.json`, per worktree): rounds bound to a diff
  fingerprint, lens × hunk coverage, findings with `proven` / `probable` / `question` / `dismissed`
  verdicts, resolutions, recorded runs and reviewer-subagent events. The stop gate passes only when
  the ledger proves the change was reviewed, verified and clean at its final state.
- **Review packet** (`craftsman review prepare`): hunks with enclosing scope, callers, risk signals
  and the lenses selected for the round; delta rounds re-review only what changed.
- **Eleven review lenses** with probe playbooks (`skills/code-review/lenses.md`) and the
  `craftsman-reviewer` / `craftsman-verifier` subagents; gears `quick`, `standard` and `deep`.
- **Review-only mode**: `/code-review` without `--fix` runs two parallel reviewers and one verifier,
  and finishes with `craftsman review check --review-only` and `craftsman review report
  --review-only`, which do not require fixes or test runs. It is refused inside an active `/craftsman`
  gate session.
- `--from-json <file|->` for `review add`, `review cover` and `review verdict`: a whole batch is
  validated and written atomically, and errors name the offending entry.
- `craftsman review merge` groups unjudged duplicates by root cause (one fix must remove every
  consequence); `craftsman run --kind test|e2e|repro` records executions with exit codes.
- Platform hooks for GitHub Copilot (VS Code and CLI), Claude Code and Google Antigravity, plus
  `AGENTS.md` / Cursor rules for tools without stop hooks. Subagent hooks act outside a gate session
  only for Craftsman roles in a current round.
- `craftsman install` / `uninstall` / `export` / `doctor`, including Windows PowerShell and an
  install manifest that restores a previous `core.hooksPath`. `doctor` now reports git hooks that
  cannot find their helper scripts.
- Pilot benchmark on SWR-Bench in the README, with the dataset citation.

### Changed

- Claude Code now installs the same gate hooks as Copilot and Antigravity by default, including the
  `PreToolUse` git guard for `Bash`; `--no-claude-hooks` skips them and `--claude-hooks` stays as an
  alias of `--claude`. `craftsman export --claude-dir` merges the hooks too. Re-run
  `./install.sh --claude` to pick up the guard hook on an existing install.
- The deep-gear prompt keyword is now `ultracraft` instead of `ultracode`, which Claude Code reserves
  for its own multi-agent workflows. `ultracode` no longer starts a Craftsman session; re-run
  `./install.sh` so the installed rules, commands and skills use the new keyword.
- The global `commit-msg` and `pre-push` hooks resolve their helpers through symlinks and fail with
  an actionable message when the helpers are missing, instead of `command not found`.

### Fixed

- Global git hooks left by an older install no longer break commits in unrelated repositories.
- Hook parsers, verdict detection and worktree state handling were hardened (see the v2.1.0
  follow-up fixes).

[Unreleased]: https://github.com/BuczynskiRafal/craftsman/compare/v5.0.0...HEAD
[5.0.0]: https://github.com/BuczynskiRafal/craftsman/compare/v4.1.0...v5.0.0
[4.1.0]: https://github.com/BuczynskiRafal/craftsman/compare/a4458c4...v4.1.0
[4.0.0]: https://github.com/BuczynskiRafal/craftsman/commit/a4458c4
[3.3.0]: https://github.com/BuczynskiRafal/craftsman/compare/v3.2.0...v3.3.0
[3.2.0]: https://github.com/BuczynskiRafal/craftsman/compare/v3.1.0...v3.2.0
[3.1.0]: https://github.com/BuczynskiRafal/craftsman/compare/v3.0.0...v3.1.0
[3.0.0]: https://github.com/BuczynskiRafal/craftsman/releases/tag/v3.0.0
