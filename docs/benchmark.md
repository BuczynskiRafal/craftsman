# Craftsman Benchmark & Evaluation Methodology

This document outlines the benchmark evaluation methodology, empirical pilot findings on real-world pull requests,
known limitations, and reproduction guidelines for Craftsman.

---

## Overview

The pilot benchmark was conducted in October 2026 using the [SWR-Bench](https://doi.org/10.1145/3808144) dataset.
Five pull requests from popular Python open-source projects were evaluated:
- `pytest-5680` (clean control PR)
- `pytest-5784` (contains reviewer-confirmed logic defects)
- `pytest-5801` (contains 32-bit `st_mtime_ns` timestamp truncation defect)
- `flask-4992` (contains documentation and contract divergence)
- `requests-764` (clean PR with stylistic comments)

Three of the pull requests contain a total of 9 ground-truth defects verified by human maintainers (spanning logic,
contracts, edge conditions, documentation divergence, and test coverage regressions). Two pull requests are clean
controls.

---

## Antigravity Pilot Results

These numbers were measured on the earlier, 4.x `/code-review` protocol (the skill is `/craft-review` since
Craftsman 5), not on the current one. Each configuration evaluated the pull requests under identical conditions: once in
an unassisted baseline run and once using that protocol. The seeded suite below is what to use for the current protocol.

| Agent & Configuration | Without Craftsman | With Craftsman | Defects Found | Avg Time / PR | Findings |
| :--- | :---: | :---: | :---: | :---: | :---: |
| Antigravity · Gemini 4 Argon (`low`) | baseline | /code-review | 22% → **44%** (4/9) | 1.5 → 4.7 min | 4 → 11 |
| Antigravity · Gemini 4 Argon (`medium`) | baseline | /code-review | 22% → **56%** (5/9) | 2.0 → 6.7 min | 8 → 10 |
| Antigravity · Gemini 4 Argon (`high`) | baseline | /code-review | 33% → **78%** (7/9) | 15.6 → 26.3 min | 12 → 17 |

### Key Observations
1. **Faster High-Recall Detection**: With Craftsman, `low` (4/9 in 4.7 min) and `medium` (5/9 in 6.7 min) discovered
   more ground-truth defects than unassisted `high` thinking (3/9 in 15.6 min) in less than half the execution time.
2. **Defect Types Caught**: Craftsman enabled detection of subtle contract violations, docstring mismatches, weakened
   assertions, and system-level edge cases (such as the 32-bit `st_mtime_ns` truncation).
3. **Adversarial Verification**: The two-phase protocol dismissed 8 spurious candidate findings that failed verification
   (e.g., inputs outside defined contracts or style nitpicks conforming to existing codebase patterns).

---

## Claude Code Pilot Runs

Pilot runs with Claude Code were made on the same 5 PRs and are not reported on this page yet.
<!-- TODO(author, plan 4.5): state why the Claude Code pilot numbers are left out. -->
Claude Code is part of the expanded benchmark, with one repetition per configuration.

---

## Limitations

We present these preliminary results transparently with their methodological limitations:
- **Sample Size**: Small initial dataset ($n=5$ pull requests, 9 verified defects).
- **Single Repetition**: 1 run per configuration in the initial pilot.
- **Language Focus**: Python projects exclusively.
- **Model Stochasticity**: LLM nondeterminism requires multi-seed evaluations for formal variance reporting.

An expanded, multi-language benchmark suite addressing these limitations is actively in progress.

---

## Seeded Defect Suite

The pilot depends on a third-party dataset and needs real pull requests. For a cheap, repeatable check that works on
any agent platform (Claude Code, Antigravity, Copilot, Codex), the repository ships a seeded suite in
[`evals/`](../evals/README.md): 8 stdlib-only Python projects, each with a pull request that implements a
written task and hides defects that look like honest mistakes (27 in total). Six cases are small (2 or 3 defects,
up to 150 changed lines); two are large (6 defects, 450 to 670 changed lines in 14 or 15 files, one of the defects
an incomplete propagation across files) so that the way cost and time grow with the size of a change shows. The
shipped tests of every pull request pass, and each case also carries decoys (1 or 2 in a small case, 3 in a large
one), correct code that only looks wrong. The suite has no cap on the number of defects.

The defects cover twelve kinds: boundary and off-by-one errors, a legitimate empty value treated as missing, swallowed
errors, non-atomic writes, incomplete propagation, tests that cannot fail or were weakened, aliasing and mutable
defaults, cache keys missing a new parameter, platform dependence, path traversal, help text that contradicts the
code, and a qualifier of the task that is not implemented.

```bash
python3 -m evals.run list
python3 -m evals.run prepare quotes ~/evals/quotes-1   # git repo: base committed, change uncommitted
# review it with /craft-review, or with the baseline prompt from evals/README.md
python3 -m evals.run score quotes ~/evals/quotes-1     # recall, unmatched findings, decoy hits
```

`score` reads the findings from the Craftsman ledger or, for a baseline run, from a JSON list of
`{"file", "line", "title"}`. Each finding is assigned to the nearest seeded item (defect or decoy) of the same file, measured as 0 inside its
lines and otherwise the distance to the nearer end; it counts only if that distance is at most 5 lines, and a tie goes
to the defect. A defect is found by at least one finding assigned to it, so one finding never credits two items, and
a finding on a decoy's own lines is a decoy hit. Findings the review dismissed are ignored. After the truth or this
rule changes, `python3 -m evals.run rescore results.jsonl` scores the kept ledgers of saved runs again (the original
is kept as `results.jsonl.bak`). Compare the recall of `/craft-review` and of a plain review on
the same model, with at least three repetitions per arm: the suite is small, so single runs are anecdotes. How to run
each arm, the exact baseline prompt, the scoring rules and how to add a case are in the
[evals README](../evals/README.md).

### Measuring a review end to end

`python3 -m evals.run agent` runs the reviews unattended in headless Claude Code (`claude -p` with the
checkout loaded as a plugin, an explicit tool allow list and a per-run budget cap) and records, per run, which
seeded defects were found, the decoys flagged, the unmatched findings, the cost in USD, the wall time, the turns,
the subagents spawned and the tokens per model. `python3 -m evals.run compare` turns the result files into a table
per arm and case with the spread over the repeats.

To judge a change of the review protocol (fewer tokens, less wall time) check out the old and the new protocol in
two directories and run both on the same cases with the same model and `--jobs`:

```bash
for i in 1 2 3 4 5; do
  python3 -m evals.run agent --craftsman-src ../craftsman-a --arm a --out bench/evals/ab.jsonl
  python3 -m evals.run agent --craftsman-src ../craftsman-b --arm b --out bench/evals/ab.jsonl
done
python3 -m evals.run compare bench/evals/ab.jsonl --defects
```

What it costs: runs x the per-run cap (`--budget-usd`, default 5) is the ceiling; a session that only loads the
plugin costs 0.06 to 0.15 USD, and the real cost of a review per case has to be measured with a first repeat
before planning more (the large cases are the dear ones). How many repeats a claim needs: the unit is the suite
recall of one repeat, and with a repeat-to-repeat standard deviation of 0.10 the 90% interval of the difference
between two arms excludes a drop of about 17 points with 3 repeats per arm, 12 with 5, 8 with 10 and 5 with 20.
Three repeats can only show large differences; to claim that an optimization did not lower recall use at least
5 per arm and report the margin the interval excludes. The 0.10 is an assumption to be replaced by the spread
`compare` prints. The details, the permission model of the unattended runs and the record format are in the
[evals README](../evals/README.md#measure-a-review-end-to-end-agent-compare).

---

## Replication Guide

Researchers and developers are encouraged to replicate these findings or contribute evaluations from other agent architectures:

1. Obtain instance definitions from [SWR-Bench](https://github.com/ZZR0/SWRench) (`data/swr_datasets_d5c5.jsonl`).
2. Run baseline reviews without Craftsman hooks or skills.
3. Run reviews under `/craft-review` with the evidence ledger enabled.
4. Compare reported findings and verifier logs against the ground-truth annotations.
5. Share replication results in GitHub Discussions under the **Benchmark replications** category.

---

## Citation

```bibtex
@article{zeng2026swrbench,
  author    = {Zeng, Zhengran and Shi, Ruikai and Han, Keke and Li, Yixin and Sun, Kaicheng and Wang, Yidong and Yu, Zhuohao and Xie, Rui and Ye, Wei and Zhang, Shikun},
  title     = {{SWR-Bench}: Assessing {LLM} Performance in Real-World Code Review Comment Generation},
  journal   = {Proceedings of the ACM on Software Engineering},
  volume    = {3},
  number    = {FSE},
  articleno = {FSE137},
  numpages  = {23},
  year      = {2026},
  publisher = {Association for Computing Machinery},
  issn      = {2994-970X},
  doi       = {10.1145/3808144},
  url       = {https://doi.org/10.1145/3808144},
  eprint    = {2509.01494},
  archivePrefix = {arXiv},
  primaryClass  = {cs.SE}
}
```
