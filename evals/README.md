# Seeded-defect review benchmark

A cheap, repeatable way to measure how many real defects a review protocol finds on any agent
platform: Craftsman `/craft-review` against a plain "review this change" prompt, on Claude Code,
Google Antigravity, Copilot, Codex or anything else that can read a git repository.

Each **case** is a small Python library or CLI (stdlib only, with its own `unittest` tests) plus a
pull request that implements a written task. The pull request contains a few **seeded defects**
that look like honest mistakes, and the shipped tests are green, as in a real PR with passing CI.
A review is scored by how many seeded defects its findings point at.

The suite has 8 cases and 27 defects: 6 small cases (2 or 3 defects each) and 2 large ones that show how
cost scales with the size of the change. Run a case several times (3 or more) before drawing
conclusions: model output varies from run to run. To measure recall, cost and wall time of a real
Claude Code review automatically, see [Measure a review end to end](#measure-a-review-end-to-end-agent-compare).

| Case | What the PR does | Defect kinds |
| :--- | :--- | :--- |
| `invtool` | `search` command for a CSV inventory CLI | `qualifier-not-implemented`, `help-contradicts-code` |
| `kvstore` | atomic saves, snapshots and namespaces for a JSON key-value store | `non-atomic-write`, `aliasing` |
| `quotes` | quotes in other currencies, with a cache | `cache-key-missing-param`, `swallowed-error`, `aliasing` |
| `attach` | folders in ticket attachments, zip export | `path-traversal`, `platform-dependence` |
| `reminders` | daily digest and local times for reminders | `platform-dependence`, `off-by-one-boundary`, `weak-test` |
| `taskboard` | task priorities in model, JSON, CLI and CSV | `incomplete-propagation`, `empty-as-missing`, `weak-test` |
| `orderdesk` (large) | shipping for an order desk: zones, carrier rate tables, free-shipping threshold, tracking numbers and carrier events (~670 changed lines, 14 files) | `cache-key-missing-param`, `off-by-one-boundary`, `empty-as-missing`, `incomplete-propagation` (across files), `swallowed-error`, `weak-test` |
| `feedbox` (large) | feed tags, export to Markdown/HTML files with an index, and a digest for an RSS reader (~450 changed lines, 15 files) | `path-traversal`, `platform-dependence`, `aliasing`, `help-contradicts-code`, `qualifier-not-implemented`, `incomplete-propagation` (across files) |

Every defect kind appears at least once: `off-by-one-boundary`, `empty-as-missing`,
`swallowed-error`, `non-atomic-write`, `incomplete-propagation`, `weak-test` (a test that cannot
fail or a weakened assertion), `aliasing` (shallow copy or mutable default),
`cache-key-missing-param`, `platform-dependence` (path separator, naive vs aware datetime),
`path-traversal`, `help-contradicts-code` and `qualifier-not-implemented`. Each small case also has 1 or
2 **decoys** (the large ones 3): code that looks suspicious but is correct. A finding on a decoy is
reported as a decoy hit. There is no cap on the number of defects in the suite.

## Measure a review end to end (`agent`, `compare`)

`python3 -m evals.run agent` runs a real, unattended Claude Code session (`claude -p`) per case,
scores its findings with the scorer below and appends one JSON line per run to a results file. It
measures what a protocol change has to be judged on: **recall, cost and wall time**.

The benchmark runs on Linux and macOS only: the harness launches the session through a POSIX shell
wrapper, and several cases seed defects that only show on POSIX paths.

```bash
# one run of every case with the checkout you are in (about 8 runs, see "What it costs")
python3 -m evals.run agent --cases all --arm current --out bench/evals/current.jsonl

python3 -m evals.run agent --help
python3 -m evals.run compare bench/evals/current.jsonl
python3 -m evals.run breakdown --out bench/evals/current.jsonl   # where the subagents and tokens went
```

| Flag | Meaning |
| :--- | :--- |
| `--cases <ids\|all>` | comma separated case ids, default `all` |
| `--craftsman-src <dir>` | the Craftsman checkout to test, default this repository |
| `--arm <label>` | name of this configuration in the results (`main`, `compact-prompt`, `baseline`) |
| `--model <name>` | default `sonnet`; keep it equal across arms |
| `--repeat N` | runs per case, default 1; a later invocation numbers its runs after the earlier ones |
| `--jobs N` | parallel runs, default 2 |
| `--budget-usd N` | spending cap of one run (`--max-budget-usd`), default 5 |
| `--timeout-min N` | wall clock cap of one run, default 30 |
| `--out <file>` | results file, appended to; default `bench/evals/<arm>.jsonl`. Logs go to `<file stem>.logs/` |
| `--prompt <template or file>` | the prompt; `{skill}` `{base}` `{task_file}` `{case}` are replaced |
| `--baseline` | no Craftsman at all: no plugin, no `craftsman` command, the baseline prompt below |
| `--keep-workdirs`, `--dry-run` | keep the temp repositories; print the first command and stop |
| `--claude`, `--permission-mode`, `--allow-tool` | the executable, the permission mode, one more allowed tool rule |

**What one run does.** It prepares the case in a fresh temp directory (`repo/`, the base committed and
the change uncommitted, exactly as `prepare`), puts a `craftsman` wrapper script in front of `PATH`
(`exec python3 <src>/bin/craftsman-cli.py "$@"`, so the session runs the CLI of the checkout under test,
not an installed one and not a `craftsman/` directory of the repository it reviews), starts `claude` in `repo/`, scores the ledger in
`repo/.git/craftsman/ledger.json` and appends the record. The checkout under test is copied once per
invocation, without `.git`, `evals/`, `docs/` and `tests/`: the arm cannot change while a long run is in
progress, and the session cannot find `truth.json` next to the plugin. The record holds the source commit
(`+dirty` when tracked plugin files had uncommitted changes).

**How Craftsman is loaded.** With `--plugin-dir <copy of src>`. The plugin manifest
(`.claude-plugin/plugin.json`) brings the skills (as `craftsman:<skill>`), the `craftsman-reviewer`,
`craftsman-verifier` and `craftsman` agents (as `craftsman:<agent>`) and the lifecycle hooks, which are the
ones that record the isolation evidence in the ledger. The harness reads the `system`/`init` event of the
stream and **stops the run at once** (status `load-failed`, almost no cost) when the review skill or one of
the two agents is not in it, and, for `--baseline`, when any `craftsman:` skill or agent is. The record keeps
how often each hook ran and how often it failed. The review skill is `skills/craft-review` if the source has
it, else (checkouts older than Craftsman 5) `skills/code-review`; the default prompt is

```text
/craftsman:craft-review review the changes against HEAD (review only, do not fix). The change description is in .git/evals/task.md.
```

**Permissions.** Unattended, with an explicit allow list and no `bypassPermissions`:
`--permission-mode dontAsk` (anything not allowed is refused instead of asked, so nothing can hang on a
prompt) plus `--allowedTools`:

- `Skill`, `Task`/`Agent` (subagents), `TodoWrite`;
- `Bash(craftsman:*)`, the read-only git commands (`status`, `diff`, `log`, `show`, `grep`, `ls-files`,
  `ls-tree`, `cat-file`, `rev-parse`, `rev-list`, `merge-base`, `blame`, `branch`), `python3`/`python` (to run
  the tests and to reproduce a finding), `cd`, `pwd`, `ls`, `wc`;
- `Read`, `Grep` and `Glob` need no rule: Claude Code allows them inside the working directory, the temp
  case, and `dontAsk` refuses them elsewhere (checked: `Read /etc/hostname` is denied);
- `--disallowedTools Edit,Write,NotebookEdit,WebFetch,WebSearch`: a review does not edit, and cannot look
  anything up (in the baseline arm `Write` is allowed for `.git/evals/findings.json` only).

`git stash`, `checkout`, `reset`, `rm` and the like are not on the list, which is also what the review
protocol demands. Why not `bypassPermissions`: it also lets a misbehaving run edit or delete anything the
user can, and a measurement should fail loudly (a refused tool shows up as `permission_denials` in the
record) rather than succeed by doing something the protocol forbids. What this does not do: the Bash rules
are prefix rules, not a sandbox; `python3` can read any file and the session has the user's network and
credentials. The cwd of every session is its own temp directory and only that directory is ever written
by the harness, but run it in a container or a throw-away VM if the machine matters. The session also gets
only the project and local settings (`--setting-sources project,local`) and no MCP servers
(`--strict-mcp-config`), so user plugins, skills, memory and connectors cannot differ between machines
or arms.

**Result record** (one JSON object per line): `arm`, `case`, `run`, `status`
(`ok`, `budget`, `timeout`, `max-turns`, `error`, `load-failed`, `no-result`, `harness-error`),
`recall` (found, total, ids found and missed, decoy hits, unmatched findings, duplicates, ignored
dismissed findings), `cost_usd`, `duration_ms` (Claude's own clock), `wall_s` (the harness's clock),
`turns`, `subagents` (spawned, completed, failed), `model_usage` (tokens and cost per model, cumulative for the whole session, subagents included) and
`usage` (tokens of the last turn of the main loop only),
`tool_calls`, `hooks`, `permission_denials`, the `init` check, the prompt, the source commit and `logs`:
the stream (`stream.jsonl`, without the token-level `stream_event` lines), `stderr.txt` and the ledger
of the run. `status` other than `ok` makes the command exit 1. Runs with `load-failed`, `no-result`,
`error` or `harness-error` are left out of every mean in `compare` and counted in its `invalid` note; a
`budget` or `timeout` run is kept with the findings it recorded before it stopped, because a protocol that
runs out of money is a result.

### Compare two protocols (A/B)

```bash
git worktree add ../craftsman-a main            # the protocol you have
git worktree add ../craftsman-b my-experiment   # the protocol you changed

# interleave the arms so that API speed drift hits both the same way
for i in 1 2 3 4 5; do
  python3 -m evals.run agent --craftsman-src ../craftsman-a --arm a --out bench/evals/ab.jsonl --jobs 2
  python3 -m evals.run agent --craftsman-src ../craftsman-b --arm b --out bench/evals/ab.jsonl --jobs 2
done

python3 -m evals.run compare bench/evals/ab.jsonl --defects
```

Keep everything but the checkout equal: model, `--jobs` (parallel runs share the API and the machine, so
wall times are only comparable at the same `--jobs`; use `--jobs 1` when time is the question), prompt,
budget. Both arms may share one results file (the `arm` label tells them apart) and `agent` can be run
again later to add repeats: the run numbers of an arm and case continue after the ones already in the file
(or in its logs folder), so earlier runs, logs and kept ledgers are never replaced. A baseline arm for the same comparison: `--baseline --arm baseline`.

Scoring changes (a new matching rule, a corrected truth location) do not need new runs: `agent` keeps each
run's ledger, and `python3 -m evals.run rescore bench/evals/ab.jsonl` scores those ledgers again with the
current truth and rewrites the `recall` of every record in place. The original file is kept as `ab.jsonl.bak`
(`.bak.1`, ... when that exists). Records without a kept ledger (baseline runs, runs without findings) stay
as they are, with a warning.

`compare` prints, per arm and case: runs, recall (mean defects found / seeded), the fewest and most found
in one run, decoys flagged, unmatched findings (possible false positives), mean cost in USD with the
spread over the repeats, minutes, turns, subagents spawned, **tokens** (the whole session, subagents
included: uncached input, output, cache read and cache write) and **$/defect**, the cost per found defect
(mean cost over mean seeded defects found; `-` when none was found); then a TOTAL row (one pass over all
cases; its range is the suite recall of each repeat, its $/defect the cost of a pass over the defects it
finds). With several arms it adds, for every later arm, the ratios of cost, minutes, turns, tokens and cost
per found defect per case and in a TOTAL row, and the **difference of the suite recall with a 90%
interval** (Welch's t on the per-repeat suite recalls). `--tokens` adds the token split (input, output,
cache read, cache write). `--defects` shows how often each seeded defect was found, which is the number to
look at when the totals look equal: a defect that dropped from 5/5 to 1/5 is a regression the mean can
hide. `--json` prints the same tables as JSON, all columns included, plus the token means and the cost per
found defect as numbers and, for every later arm, its comparison with the first (`versus`).

The tokens of a session are read from the record's `model_usage`, summed over the models: the cost is
computed from it, so it counts every agent of the session. The record's `usage` is not used: it is only
the last turn of the main loop (0.3-7.6% of the session in runs with subagents), so it would understate
the session. A record without token counts in `model_usage` is counted in the `note` column (`1 without tokens`) and left out of the token means.

### Where the tokens go (`breakdown`)

```bash
python3 -m evals.run breakdown --out bench/evals/ab.jsonl              # per arm, all cases together
python3 -m evals.run breakdown --out bench/evals/ab.jsonl --cases      # and a row per case
python3 -m evals.run breakdown --out bench/evals/ab.jsonl --arm b --json
```

Reviews score well but cost a lot, and `compare` only shows the sum. `breakdown` combines each record with
the ledger the run kept (`logs.ledger`) and prints, per arm (and per case with `--cases`), one table each:

| Table | What it holds |
| :--- | :--- |
| Runs | valid runs, how many kept a ledger and full token counts, mean cost, mean minutes, cost per found defect |
| Subagents | per run with a ledger: hunters and verifiers (a resumed agent counts once); slots launched more than once (a hunter died or was relaunched); verifier runs that only judged findings another verifier run had judged before them (second opinions) |
| Hunters | per run: by the kind of the slot they hunted (`bug`, `simplicity`, `sweep`, `variant`, `fresh`, or `none` when no slot accounts for them) and by its generation (`gen 1`; the waves are `gen 2` and `gen 3`) |
| Tokens | per run with full token counts: input, output, cache read, cache write, total and agent minutes of the hunters and of the verifiers (from the stop events of the ledger, as `craftsman review report` counts them), of the **main session** (the whole session minus its subagents) and of the whole session, each with its share |
| Cache write per subagent | mean and minimum, for the hunters and for the verifiers |
| Findings | per run: recorded, merged into another, and by current verdict (`proven`, `probable`, `question`, `dismissed`, `open`) |
| Seeded defects found | sums: for each seeded defect found, the slot that recorded it first, by slot kind (`lead` is the orchestrator's own finding) and by generation |
| Findings on no seeded defect | sums: the findings the scorer matches to no seeded defect or to a decoy (dismissed ones included, merged duplicates not), by slot kind: the noise the verifiers had to judge |

It ends with one line per arm, `cost per found defect`, and names every run a figure leaves out, with the
reason. Nothing is estimated: a run without a kept ledger (a baseline run, a session that never prepared a
review) is left out of every ledger figure, and a run in which a subagent has no usage in the ledger (its
transcript could not be read), whose record has no token counts, or whose subagents report more tokens of
a kind than its record (the two sources disagree, so the main session's share would be negative) is left
out of the token figures only.
Invalid runs are left out of everything, as in `compare`.

Everything is tied together by the rules the product and the scorer use, not by new ones: a hunter belongs
to the slot `craftsman.attribution` binds it to (its claim, shown by its transcript), else to the slot a run
without a readable transcript serves; a verdict to the verifier runs that vouch for it; a finding to the
stored slot bound to the run that recorded it, else to the slot it names; a finding to a seeded defect by
the matching rule of `score`, and "first" is the finding recorded earliest. `breakdown` only reads: it
starts no agent and changes neither the results file nor a ledger.

**How to read it.**

- *Are the waves worth their cost?* Set the `gen 2` and `gen 3` columns of *Hunters* (what the waves launch)
  against the same columns of *Seeded defects found* (the defects only a wave found first) and the
  `variant` and `fresh` columns of *Findings on no seeded defect* (the noise the waves add for the
  verifiers). Waves that launch many hunters, find few defects first and add noise are the first thing to
  cut; confirm with `compare` that the recall does not drop.
- *How much is fixed starting context?* The minimum cache write per subagent is about what every subagent
  pays before it reads anything (system prompt, agent definition, tools, brief); times the subagents per
  run, it is the floor of a run's cache writes. A mean far above the minimum says the work dominates, not
  the start; a mean close to it says shorter starting context or fewer subagents is where tokens are saved.
- *How much is the main session?* The share of the *main session* row. The product's own telemetry
  (`review report`) counts only the subagents, so this is the one place the orchestrator's tokens show. A
  large share points at the protocol's own steps (reading briefs, checks, reports) rather than the hunt.

### What it costs, and how many repeats a claim needs

*Cost.* Runs = arms x cases x repeats; the spending of a run is capped by `--budget-usd`, so the ceiling is
runs x cap (8 cases x 5 repeats x 2 arms = 80 runs, 400 USD at the default cap of 5). A session that only
loads the plugin and runs one command costs about 0.06-0.15 USD (measured on `sonnet`); a real review is
far more, and the large cases cost more than the small ones, which is what they are for. Measure before you
plan: run one repeat of `kvstore` and `orderdesk`, read the cost column, and set `--budget-usd` to about twice
the dearest run. `compare` prints the cost of a full pass over all cases.

*Repeats.* A single run is an anecdote: the same prompt on the same case finds a different number of
defects from run to run. The unit to compare is the **suite recall of one repeat** (all cases run once). If
that varies by a standard deviation of sigma between repeats, the 90% interval of the difference of two arms
with n repeats each is about +-t x sigma x sqrt(2/n) (t is 2.1 at n=3, 1.9 at n=5, 1.7 at n=10), so:

| Repeats per arm | Smallest recall drop the interval can rule out (sigma = 0.10) |
| :---: | :---: |
| 3 | about 17 points |
| 5 | about 12 points |
| 10 | about 8 points |
| 20 | about 5 points |

Equivalently, to detect a real difference d with 80% power at 5% (one sided) you need about
`12.4 x sigma^2 / d^2` repeats per arm: with sigma = 0.10, d = 0.20 needs 4, d = 0.15 needs 6, d = 0.10 needs
13. The sigma of 0.10 is an assumption: run 5 repeats of the current protocol first, read the spread of the
suite recall in the `compare` TOTAL row (and the per-case `±`), and replace it. The practical rule: 3
repeats find only large changes (20 points); to claim "recall did not drop" for a token or time
optimization use at least 5 repeats per arm and state the margin the interval actually excludes; 10 if the
margin has to be under 10 points. Add large cases rather than repeats when the interval is wide because
of a few hard defects: `--defects` shows which.

## Run a case by hand

```bash
python3 -m evals.run list                               # case, size, defects, decoys, kinds
python3 -m evals.run prepare quotes ~/evals/quotes-1     # a fresh, empty directory
```

This is for other agents and platforms, or to look at a case. `agent` below does the same, unattended,
for Claude Code.

`prepare` creates a git repository in the destination, commits the case's `base/` tree, then
copies `head/` over it **without committing**, so the pull request is the uncommitted diff
(`git diff HEAD`, plus any new files). The change description (`task.md`) is copied to
`.git/evals/task.md`, outside the work tree. `truth.json` is never copied. Use a new destination
for every run; the reviewer must not edit the files (line numbers in `truth.json` refer to
`head/`).

### With Craftsman

Install Craftsman for the platform once (`craftsman install`, see
[installation](../docs/installation.md)), start the agent inside the prepared directory and run:

```text
/craft-review review the changes against HEAD (review only, do not fix)
```

Tell it that the change description is the file `.git/evals/task.md` (it can be passed as the
task of the review, or read by the agent). The findings end up
in `.git/craftsman/ledger.json`, which is where `score` reads them. Findings the review dismissed
are ignored.

### Without Craftsman (baseline)

Start the agent in the prepared directory with no Craftsman skills, rules or hooks (a different
user profile or a platform without it installed) and paste this prompt:

```text
You are reviewing an uncommitted change in this git repository. The change description is in
`.git/evals/task.md`. See the change with `git status` and `git diff HEAD`; new files are
untracked, so read them too.

Find the real defects the change introduces or leaves behind. Read the code around the change,
not only the diff, and run the tests if that helps. Review only: do not modify any file in the
repository.

When you are done, write your findings to `.git/evals/findings.json` as a JSON list, one object
per defect, with exactly these keys:
  {"file": "<path relative to the repository root>", "line": <line number in the changed file>,
   "title": "<one sentence: what is wrong and when it shows>"}
Report each defect once, at the line where it is clearest. Also print the same list in your answer.
```

If the agent cannot write the file, save the JSON list it prints to any file and pass it with
`--findings`.

### Claude Code, Antigravity, other agents

- **Claude Code**: `cd` into the prepared directory, run `claude`. For the Craftsman arm use
  `/craft-review`; for the baseline arm start from a profile without the Craftsman plugin (for
  example a fresh `CLAUDE_CONFIG_DIR`) and paste the baseline prompt.
- **Google Antigravity**: open the prepared directory as the workspace. The Craftsman arm needs
  the plugin or `craftsman install --antigravity`; the baseline arm needs a profile without it.
  Compare the same model on both arms.
- **Any other agent**: the baseline prompt only needs a shell and file access. If the agent has
  no Craftsman, use the baseline arm; save its JSON list and score it with `--findings`.

Keep model, effort and tools equal between the two arms, otherwise the comparison measures the
setup and not the protocol.

## Score a run

```bash
python3 -m evals.run score quotes ~/evals/quotes-1                    # ledger, or .git/evals/findings.json
python3 -m evals.run score quotes ~/evals/quotes-1 --findings f.json  # baseline list from anywhere
python3 -m evals.run score quotes ~/evals/quotes-1 --json             # machine-readable
```

The output has the recall for the case (defects found / defects seeded), every defect with the
finding that found it or `MISSED`, the **unmatched** findings (possible false positives: read
them, a real problem outside the seeded list lands there too), the **decoy hits**, and duplicates
(extra findings on a defect that was already found).

Matching rules:

- A finding is assigned to the nearest defect or decoy of its file. The distance is 0 inside
  `[start, end]` of a location and otherwise the number of lines to the nearer end; an item with
  several locations uses the nearest one. The finding counts for that item only when the distance is
  at most 5 lines, so a finding a few lines off still counts, and a finding with no item within 5
  lines is unmatched. A tie goes to the defect; a finding inside two overlapping ranges goes to the
  narrower one, so a decoy nested in a defect's span is still hit by a finding on its own lines.
  Paths are compared relative to the repository root (`./`, backslashes, absolute paths inside the
  work directory and Windows drive letters are normalized).
- A finding on a decoy's own lines is a decoy hit, even when a defect is next to it.
- Each finding is assigned to at most one defect or decoy and each defect counts once: when
  several findings are assigned to a defect, the closest one finds it and the rest are duplicates.
- **Precision** is the share of the scored findings that point at a seeded defect (found or
  duplicate). It is a floor: an unmatched finding may be a real problem outside the seeded list.
- Findings with the verdict `dismissed` are ignored. Only `findings[].file` (or `.path`), `.line`,
  `.title` and `.verdict` of the ledger are read, so ledger schema changes do not matter.
- Findings that have no usable file or line are counted as unreadable and ignored.

To compare arms by hand, score each run, then average the recalls over the repetitions of each arm and
list the unmatched findings of each arm side by side. For Claude Code, `compare` does this (a run without
a ledger or findings file counts as zero defects found, and is marked in the record).

## Add a case

1. Create `evals/cases/<id>/` with
   - `base/`: a small stdlib-only Python library or CLI, at most about 200 lines including its
     tests, with `unittest` tests in `tests/` (`tests/__init__.py` too). The tests run with
     `python3 -m unittest` from `base/`. A **large** case has a base that looks like a real project:
     6 to 9 modules, a test file for most of them, shared helpers.
   - `head/`: the same tree after a change of at most about 150 changed lines that implements
     `task.md`. Its tests pass. Defects must look like honest mistakes: no comments, names or
     TODOs that give them away. A large case changes 400 to 700 lines in 10 to 15 files (new
     modules, edits to existing ones, new and edited tests) and seeds 4 to 6 defects, at least one
     of them an `incomplete-propagation` whose locations are in two different files, plus 2 to 4
     decoys. A decoy must be in code the change touches and be correct for a reason the task or the
     code states.
   - `task.md`: the pull request description a reviewer receives. State the requirements the
     defects violate (a defect is only fair if the task or the code around it says what is right).
   - `truth.json`:

     ```json
     {
       "size": "large",
       "defects": [
         {"id": "x-1", "kind": "swallowed-error", "summary": "one line",
          "locations": [{"file": "pkg/mod.py", "start": 40, "end": 44, "anchor": "except OSError:"}]}
       ],
       "decoys": [
         {"id": "x-d1", "summary": "why this is correct",
          "locations": [{"file": "pkg/mod.py", "start": 10, "end": 12, "anchor": "if not items:"}]}
       ]
     }
     ```

     `"size"` is optional (`"small"` is the default; a `"large"` case is checked against the large limits).
     `file` is relative to `head/`, lines are 1-based and inclusive. `anchor` is text that occurs on
     one of the lines start..end of the head file; the tests fail when an edit of the file moves the
     code away from the stated lines. Keep decoys out of the lines of defects and mind the gap to
     neighbouring defects (see the matching rules). A propagation defect can be
     reported at the caller or the callee: list both as locations. `kind` is one of the kinds above (`KINDS` in `run.py`).
2. Run `python3 -m unittest tests.test_evals`. It checks that the base and head tests pass, that
   every location points at an existing, non-blank line, that the sizes are within the limits for its
   size, that `evals/README.md` still carries the baseline prompt of `run.py`, and that the suite has
   at least 12 defects covering every kind. The tests of `agent` and `compare` use a fake `claude`
   and never call the real one.
3. Add the case to the table above. The suite has no cap on defects; keep the large cases few, they
   are what makes a run expensive.

Lint: `python3 -m ruff check evals tests/test_evals.py tests/test_evals_usage.py` and
`python3 -m mypy --strict --exclude 'evals/cases/' evals tests/test_evals.py tests/test_evals_usage.py` are
clean (the case trees are fixtures with seeded defects and are not linted).
