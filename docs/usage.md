# Usage Guide

You describe the work in plain words. The agent infers the rest.

---

## Ask in Plain Words

In Claude Code, Copilot Chat, or Google Antigravity:

```text
/craftsman add pagination to the catalog search
/craftsman migrate the session store to Redis, this is critical
/craft-review review my staged changes
/craft-review review my staged changes and fix what you find
/craft-review review PR 42
/craft-review review the changes in src/api/ against main
/craft-review be thorough, this touches authentication
ultracraft rewrite the token refresh logic
```

- `/craftsman <task>` builds the change, reviews the final diff, fixes what the review confirms,
  runs the tests, and closes the gate.
- `/craft-review <words>` reviews an existing change. It fixes only when you say so ("and fix what
  you find").
- `ultracraft` in a request starts `/craftsman` at full depth. Inside `/craft-review` it means a deep
  review; it never opens a gate.

To mention a trigger word without starting a session, put it in backticks or quotes.

---

## What the Agent Decides

You never have to choose these. The agent reads the request and the repository.

| Decision | How it decides |
| :--- | :--- |
| **Target** | Nothing named: the working tree against the gate's base commit, else `HEAD`. A branch or commit: that ref. A PR number or URL: it checks the PR out and reviews against its merge base. "Staged changes", "tracked files", or a path freezes that scope for the review. |
| **Fixing** | `/craftsman` always fixes. `/craft-review` fixes when the words ask for it; otherwise it only reports. |
| **Depth** | One adaptive depth, `deep`: every lens on every production hunk, a fresh second look, an end-to-end run in addition to the tests. `review prepare` goes deep by itself when a security or concurrency signal fires in a production hunk or the change is large (over 250 changed production lines); the agent adds its own judgment, and goes deep when you ask for care ("critical", "be thorough", `ultracraft`). The list is under **Depth** in the [`craft-review` skill](../skills/craft-review/SKILL.md). Depth never goes down within a session. |
| **Hunt plan** | Not a decision of the agent: `review prepare` computes it from the diff. How many hunters, which lenses each gets and how the diff is split between them follow from the kinds of hunks and their risk signals; findings that are proven call for further waves, up to three generations. The agent launches every slot and cannot skip one. |
| **When to ask you** | Only for an ambiguity that changes what gets built, to accept a `major` finding instead of fixing it, and when the round cap is reached. |

---

## What the Report Explains

The final report is plain Markdown for a human reader:

- what was built or reviewed, file by file;
- the agent's decisions in plain words: the depth and why, the hunt plan's slots and their lenses, the waves
  and why another one ran or stopped, how findings were verified (reproduced, or read statically);
- findings fixed, accepted (with reasons), and dismissed (count), by severity;
- the verification commands and their results;
- the agent usage per role (hunters, verifiers), where the platform provides it;
- what the gate did not assess, a `SELF-REVIEWED` banner when the platform could not confirm that
  separate subagents did the review (an advisory round), and a count of hook errors if any hook failed
  while the review ran.

`/craft-review` without fixes ends with a review-only report. `partial` marks a review in which some
slots stayed uncovered after retries and lists them under `Not covered`; `REVIEW ABANDONED - NOT COMPLETE` marks one
that the review guard let the agent leave after six reminders without progress.

---

## Frozen Scope, Archives, and First Commits

- **Review-only scope.** The first round of a standalone `/craft-review` freezes the list of changed
  files (including both paths of a rename). Files that change outside that scope while the review
  runs, such as a plan file written by another process, appear under `Ignored (outside the review
  scope)` and do not invalidate the round. When fixing needs a new file, such as a regression test,
  the agent extends the scope with `--add-paths`.
- **Archives.** Packets, views, and ledger snapshots live in `.git/craftsman/`. Superseded packets
  are pruned on every `review prepare`; archives keep the newest `CRAFTSMAN_KEEP_ARCHIVES` entries
  (default `5`). `craftsman review reset` archives the current ledger before an independent review. A
  pre-5.0 gate state or schema 2 ledger is archived automatically, so there is nothing to reset when
  upgrading.
- **Initial commit.** In a repository with no commits, the empty tree is the base, so the first
  bootstrap can be reviewed too.

---

## Inspecting a Session

```bash
craftsman status                       # deep flag, round, coverage, findings and verdicts, latest runs, agent usage
craftsman review check                 # what is missing: slots to launch, verifiers, runs (--review-only outside a gate)
craftsman review show --hunks          # the hunks still to cover (H# -> file:lines)
craftsman review scout H3              # base version, callers and tests of a hunk (or path:line, or a symbol)
craftsman review report                # the final report, with agent usage per role
craftsman skill show craft-review --lenses trust,state   # lens playbooks
```

`craftsman run` prints a command's output as it arrives, unchanged, up to 6,000 characters. When the
output passes that limit, printing stops and a line names the log that holds the full output:
`[craftsman run: output continues; its end prints when the command exits; full output, written as it arrives: PATH]`.
`PATH` is `run-output.log` in a directory of the `craftsman review scratch` area, which is pruned when
the review ends; it holds everything from the first byte, grows while the command runs (so
`tail PATH` shows where a slow or hung command is) and keeps its name. When the command exits, the
rest prints whole if it is at most 6,000 characters; otherwise a line
`[craftsman run: N characters omitted; full output: PATH]` and the last 6,000 characters print. Cuts
move to a line boundary where that costs at most 200 characters. If the log cannot be written, the
line says `full output not saved: <reason>` and all of the output prints as it arrives. `--full`
prints everything as it arrives, with no log. The ledger record, test count and exit code are the
same either way. On POSIX, SIGTERM or SIGHUP to `craftsman run` stops the command as Ctrl-C does:
its process group is stopped, the run is recorded as interrupted, the end of the output prints, and
`craftsman run` exits with 128 plus the signal number (143 or 129).

`craftsman review check --json` (and `report --json`) print the verdict of the gate for scripts: `passed`,
`partial`, `self_reviewed`, `enforced`, `platform`, `abandoned`, `slots` (each with `id`, `state` of `done`,
`running`, `dead` or `unlaunched`, `lenses`, `hunks` and `missing`), `issues` (`code`, `message`), `warnings`,
`summary`, and `hook_errors` (`count`, `since_round`, `path`) when a hook failed since the round was prepared.
`check` exits 1 while `passed` is false.

---

## Optional Flags (Reference)

Flags are shortcuts that agents use and that power users may type. None is required; the plain
words above do the same.

| Flag | Where | Effect |
| :--- | :--- | :--- |
| `--deep` | `/craftsman`, `/craft-review`, `gate --init`, `review prepare` | Starts or raises the review to deep. Inside a gate session it raises the gate for good; nothing lowers it. |
| `--base <ref>` | `/craft-review`, `review prepare` | Review against this ref instead of the default base. |
| `--task "<text>"` / `--task-file <path or ->` | `review prepare` | The intent brief that every hunter and verifier brief quotes as `Task:` (at most 4000 characters, for both `--task` and `--task-file`). |
| `--staged` / `--tracked` / `--paths <csv>` | `/craft-review`, `review prepare` | Freeze the review-only scope to the index, to tracked changes, or to these files and directories. |
| `--add-paths <csv>` | `review prepare` | Extend a frozen scope, for example with a new regression test. |
| `--fix` | `/craft-review` | Fix confirmed findings, re-check the fix delta, and run the tests. |
| `--comment` | `/craft-review` | Post the report on the PR with `gh pr comment`. |
| `--partial` | `review report --review-only` | Report a review that stayed incomplete after retries. Refused when the round is enforced and no hunter was launched in it; it never ends the review guard (only a report of a review that passed does). |
| `--hunter <slot or lead>` | `review add`, `review cover` | The hunt-plan slot that did the work, as the brief prints it (required in an enforced round). `lead` marks the orchestrator's own findings and never credits coverage. |
| `brief <slot>` | `review brief` | Records that a hunter took the slot, then prints its task, slice, lenses and the commands that record its work. A hunter's launch prompt is one line naming the slot. |
| `brief --verifier F1,F2` | `review brief` | Prints a verifier's instruction for these findings and records nothing. |
| `--takeover` | `gate --init` | Take over a gate opened by another session. |
