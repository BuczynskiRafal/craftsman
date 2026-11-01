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

Per Decision D3, preliminary pilot runs for Claude Code were conducted on the same 5 PR instances (1 repetition per
configuration, 2–4 runs per instance). While directional improvements mirrored the Antigravity results (increased
recall on contract and edge-case defects), Claude Code numbers are reserved for the expanded benchmark suite currently
under evaluation to ensure statistical confidence intervals and multi-run stability before publication on the main
project page.

---

## Limitations

We present these preliminary results transparently with their methodological limitations:
- **Sample Size**: Small initial dataset ($n=5$ pull requests, 9 verified defects).
- **Single Repetition**: 1 run per configuration in the initial pilot.
- **Language Focus**: Python projects exclusively.
- **Model Stochasticity**: LLM nondeterminism requires multi-seed evaluations for formal variance reporting.

An expanded, multi-language benchmark suite addressing these limitations is actively in progress.

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
