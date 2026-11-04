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

Each configuration evaluated the pull requests under identical conditions: once in an unassisted baseline run and once
using Craftsman's `/code-review` protocol.

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
[`evals/`](../evals/README.md): 6 small stdlib-only Python projects, each with a pull request that implements a
written task and hides 2 or 3 defects that look like honest mistakes (15 in total). The shipped tests of every
pull request pass, and each case also carries 1 or 2 decoys, correct code that only looks wrong.

The defects cover twelve kinds: boundary and off-by-one errors, a legitimate empty value treated as missing, swallowed
errors, non-atomic writes, incomplete propagation, tests that cannot fail or were weakened, aliasing and mutable
defaults, cache keys missing a new parameter, platform dependence, path traversal, help text that contradicts the
code, and a qualifier of the task that is not implemented.

```bash
python3 evals/run.py list
python3 evals/run.py prepare quotes ~/evals/quotes-1   # git repo: base committed, change uncommitted
# review it with /code-review, or with the baseline prompt from evals/README.md
python3 evals/run.py score quotes ~/evals/quotes-1     # recall, unmatched findings, decoy hits
```

`score` reads the findings from the Craftsman ledger or, for a baseline run, from a JSON list of
`{"file", "line", "title"}`. A finding matches a defect when it is in the same file and within 3 lines of the seeded
location; findings the review dismissed are ignored. Compare the recall of `/code-review` and of a plain review on
the same model, with at least three repetitions per arm: the suite is small, so single runs are anecdotes. How to run
each arm, the exact baseline prompt, the scoring rules and how to add a case are in the
[evals README](../evals/README.md).

---

## Replication Guide

Researchers and developers are encouraged to replicate these findings or contribute evaluations from other agent architectures:

1. Obtain instance definitions from [SWR-Bench](https://github.com/ZZR0/SWRench) (`data/swr_datasets_d5c5.jsonl`).
2. Run baseline reviews without Craftsman hooks or skills.
3. Run reviews under `/code-review` with the evidence ledger enabled.
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
