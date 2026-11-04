# Seeded-defect review benchmark

A cheap, repeatable way to measure how many real defects a review protocol finds on any agent
platform: Craftsman `/code-review` against a plain "review this change" prompt, on Claude Code,
Google Antigravity, Copilot, Codex or anything else that can read a git repository.

Each **case** is a small Python library or CLI (stdlib only, with its own `unittest` tests) plus a
pull request that implements a written task. The pull request contains a few **seeded defects**
that look like honest mistakes, and the shipped tests are green, as in a real PR with passing CI.
A review is scored by how many seeded defects its findings point at.

The suite has 6 cases and 15 defects. Run a case several times (3 or more) before drawing
conclusions: model output varies from run to run.

| Case | What the PR does | Defect kinds |
| :--- | :--- | :--- |
| `invtool` | `search` command for a CSV inventory CLI | `qualifier-not-implemented`, `help-contradicts-code` |
| `kvstore` | atomic saves, snapshots and namespaces for a JSON key-value store | `non-atomic-write`, `aliasing` |
| `quotes` | quotes in other currencies, with a cache | `cache-key-missing-param`, `swallowed-error`, `aliasing` |
| `attach` | folders in ticket attachments, zip export | `path-traversal`, `platform-dependence` |
| `reminders` | daily digest and local times for reminders | `platform-dependence`, `off-by-one-boundary`, `weak-test` |
| `taskboard` | task priorities in model, JSON, CLI and CSV | `incomplete-propagation`, `empty-as-missing`, `weak-test` |

Every defect kind appears at least once: `off-by-one-boundary`, `empty-as-missing`,
`swallowed-error`, `non-atomic-write`, `incomplete-propagation`, `weak-test` (a test that cannot
fail or a weakened assertion), `aliasing` (shallow copy or mutable default),
`cache-key-missing-param`, `platform-dependence` (path separator, naive vs aware datetime),
`path-traversal`, `help-contradicts-code` and `qualifier-not-implemented`. Each case also has 1 or
2 **decoys**: code that looks suspicious but is correct. A finding on a decoy is reported as a
decoy hit.

## Run a case

```bash
python3 evals/run.py list
python3 evals/run.py prepare quotes ~/evals/quotes-1     # a fresh, empty directory
```

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
/code-review
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

Find the real defects the change introduces or leaves behind: wrong behavior, boundary and edge
cases, errors that are lost, partial writes, security problems, places that should have been
updated with the change but were not, tests that cannot fail or that check less than they claim,
and help text or docs that contradict the code. Read the code around the change, not only the
diff, and run the tests if that helps. Do not modify any file in the repository.

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
  `/code-review`; for the baseline arm start from a profile without the Craftsman plugin (for
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
python3 evals/run.py score quotes ~/evals/quotes-1                    # ledger, or .git/evals/findings.json
python3 evals/run.py score quotes ~/evals/quotes-1 --findings f.json  # baseline list from anywhere
python3 evals/run.py score quotes ~/evals/quotes-1 --json             # machine-readable
```

The output has the recall for the case (defects found / defects seeded), every defect with the
finding that found it or `MISSED`, the **unmatched** findings (possible false positives: read
them, a real problem outside the seeded list lands there too), the **decoy hits**, and duplicates
(extra findings on a defect that was already found).

Matching rules:

- A finding matches a defect when its file is the same as one of the defect's locations and its
  line lies within `[start - 3, end + 3]` of that location. Paths are compared relative to the
  repository root (`./`, backslashes and absolute paths inside the work directory are
  normalized).
- Each finding matches at most one defect and each defect counts once. The assignment gives the
  most defects found; ties go to the nearest line.
- Findings with the verdict `dismissed` are ignored. Only `findings[].file` (or `.path`), `.line`,
  `.title` and `.verdict` of the ledger are read, so ledger schema changes do not matter.
- Findings that have no usable file or line are counted as unreadable and ignored.

To compare arms, score each run, then average the recalls over the repetitions of each arm and
list the unmatched findings of each arm side by side.

## Add a case

1. Create `evals/cases/<id>/` with
   - `base/`: a small stdlib-only Python library or CLI, at most about 200 lines including its
     tests, with `unittest` tests in `tests/` (`tests/__init__.py` too). The tests run with
     `python3 -m unittest` from `base/`.
   - `head/`: the same tree after a change of at most about 150 changed lines that implements
     `task.md`. Its tests pass. Defects must look like honest mistakes: no comments, names or
     TODOs that give them away.
   - `task.md`: the pull request description a reviewer receives. State the requirements the
     defects violate (a defect is only fair if the task or the code around it says what is right).
   - `truth.json`:

     ```json
     {
       "defects": [
         {"id": "x-1", "kind": "swallowed-error", "summary": "one line",
          "locations": [{"file": "pkg/mod.py", "start": 40, "end": 44}]}
       ],
       "decoys": [
         {"id": "x-d1", "summary": "why this is correct",
          "locations": [{"file": "pkg/mod.py", "start": 10, "end": 12}]}
       ]
     }
     ```

     `file` is relative to `head/`, lines are 1-based and inclusive. A propagation defect can be
     reported at the caller or the callee: list both as locations. `kind` is one of the kinds above (`KINDS` in `run.py`).
2. Run `python3 -m unittest tests.test_evals`. It checks that the base and head tests pass, that
   every location points at an existing, non-blank line, that the sizes are within the limits,
   that `evals/README.md` still carries the baseline prompt of `run.py`, and that the suite has
   12 to 16 defects covering every kind.
3. Keep the total at 16 defects or fewer and add the case to the table above.
