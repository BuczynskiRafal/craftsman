# Usage Guide

You describe the work in plain words. The agent infers the rest.

---

## Ask in Plain Words

In Claude Code, Copilot Chat, or Google Antigravity:

```text
/craftsman add pagination to the catalog search
/craftsman migrate the session store to Redis, this is critical
/code-review review my staged changes
/code-review review my staged changes and fix what you find
/code-review review PR 42
/code-review review the changes in src/api/ against main
/code-review be thorough, this touches authentication
ultracraft rewrite the token refresh logic
```

- `/craftsman <task>` builds the change, reviews the final diff, fixes what the review confirms,
  runs the tests, and closes the gate.
- `/code-review <words>` reviews an existing change. It fixes only when you say so ("and fix what
  you find").
- `ultracraft` in a request starts `/craftsman` at full depth.

To mention a trigger word without starting a session, put it in backticks or quotes.

---

## What the Agent Decides

You never have to choose these. The agent reads the request and the repository.

| Decision | How it decides |
| :--- | :--- |
| **Target** | Nothing named: the working tree against the gate's base commit, else `HEAD`. A branch or commit: that ref. A PR number or URL: it checks the PR out and reviews against its merge base. "Staged changes", "tracked files", or a path freezes that scope for the review. |
| **Fixing** | `/craftsman` always fixes. `/code-review` fixes when the words ask for it; otherwise it only reports. |
| **Depth** | One adaptive depth. The agent goes deep (every lens, at least two waves, an end-to-end run when fixing) for security-relevant code, concurrency or clocks, persistent data or migrations, public APIs or file formats, removed guarantees, or a large or cross-cutting diff. It also goes deep when you ask for care: "critical", "production", "be thorough", `ultracraft`. Depth never goes down within a session. |
| **Hunt plan** | How many hunters, which lenses each gets, how the diff is split between them, and whether another wave is worth running. |
| **When to ask you** | Only for an ambiguity that changes what gets built, to accept a `major` finding instead of fixing it, and when the round cap is reached. |

---

## What the Report Explains

The final report is plain Markdown for a human reader:

- what was built or reviewed, file by file;
- the agent's decisions in plain words: the depth and why, the hunters and their lenses, the waves
  and why another one ran or stopped, how findings were verified (reproduced, or read statically);
- findings fixed, accepted (with reasons), and dismissed (count), by severity;
- the verification commands and their results;
- what the gate did not assess, and a `SELF-REVIEWED` banner when the platform could not confirm that
  separate subagents did the review.

`/code-review` without fixes ends with a review-only report. `partial` marks a review in which some
slices stayed uncovered after retries and lists them under `Not covered`.

---

## Frozen Scope, Archives, and First Commits

- **Review-only scope.** The first round of a standalone `/code-review` freezes the list of changed
  files (including both paths of a rename). Files that change outside that scope while the review
  runs, such as a plan file written by another process, appear under `Ignored (outside the review
  scope)` and do not invalidate the round. When fixing needs a new file, such as a regression test,
  the agent extends the scope with `--add-paths`.
- **Archives.** Packets, views, and ledger snapshots live in `.git/craftsman/`. Superseded packets
  are pruned on every `review prepare`; archives keep the newest `CRAFTSMAN_KEEP_ARCHIVES` entries
  (default `5`). `craftsman review reset` archives the current ledger before an independent review.
- **Initial commit.** In a repository with no commits, the empty tree is the base, so the first
  bootstrap can be reviewed too.

---

## Inspecting a Session

```bash
craftsman status                       # deep flag, round, coverage, findings and verdicts, latest runs
craftsman review show --hunks          # H# -> file:lines map
craftsman review report                # the final report
craftsman skill show code-review --lenses trust,state   # lens playbooks
```

---

## Optional Flags (Reference)

Flags are shortcuts that agents use and that power users may type. None is required; the plain
words above do the same.

| Flag | Where | Effect |
| :--- | :--- | :--- |
| `--deep` | `/craftsman`, `/code-review`, `gate --init`, `review prepare` | Starts or raises the review to deep. Inside a gate session it raises the gate for good; nothing lowers it. |
| `--base <ref>` | `/code-review`, `review prepare` | Review against this ref instead of the default base. |
| `--staged` / `--tracked` / `--paths <csv>` | `/code-review`, `review prepare` | Freeze the review-only scope to the index, to tracked changes, or to these files and directories. |
| `--add-paths <csv>` | `review prepare` | Extend a frozen scope, for example with a new regression test. |
| `--fix` | `/code-review` | Fix confirmed findings, re-check the fix delta, and run the tests. |
| `--comment` | `/code-review` | Post the report on the PR with `gh pr comment`. |
| `--partial` | `review report --review-only` | Report a review that stayed incomplete after retries. |
| `--takeover` | `gate --init` | Take over a gate opened by another session. |
