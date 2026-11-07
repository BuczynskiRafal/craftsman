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

## Seeded Suite Results (November 2026)

End-to-end runs of `python3 -m evals.run agent` on the 8 cases of the seeded suite: 27 defects and 18 decoys. Each run
is a headless Claude Code session with Sonnet 5.5, `--permission-mode auto` and a $5 cap. Findings are scored with
nearest-item matching.

| Version | Passes | Recall | Precision | Protocol skipped | Cost per pass | Time per pass |
| :-- | --: | --: | --: | --: | --: | --: |
| 4.1.0 (inline quick review) | 2 | 89% | 86% | n/a | $1.72 | 9.9 min |
| 5.0, before the cost work | 2 | 89% | 71% | 2 of 16 runs | $3.11 | 19.6 min |
| 5.0, after the cost work | 3 | 96% | 73% | 0 of 24 runs | $2.58 | 17.3 min |
| 5.0, proven-only waves, lean packet, cheaper verifiers | 3 | 89% | 74% | 0 of 24 runs (2 left findings unjudged) | $2.58 | 18.8 min |
| 5.0, final (review guard kept on agent reports) | 1 | 96% | 75% | 0 of 8 runs | $3.03 | 27.0 min |

Cost and time for "after the cost work" come from its last pass. The two earlier passes cost $2.99 and took 29.7
minutes, because the review stop guard prompted again while background agents were still running; that is fixed.
In the final pass one session stayed open for 8 minutes after its review was done, waiting for timers its
orchestrator had set to poll for a hunter; without that idle tail the pass took about 18 minutes. The skill now tells
the orchestrator to end its turn instead of polling.

- Recall rose because every review now runs the protocol. Before, the orchestrator sometimes reviewed a small diff
  itself and recorded nothing. The review stop guard and the "prepare first" step brought that to 0 of 24 runs.
- On the two large cases, the cost work (scout, slimmer views, pipelining, a cheaper simplicity hunter) cut cost by
  about a third: feedbox fell from $0.95 to $0.60 and orderdesk from $0.84 to $0.58. Time fell by 20-30%. On small
  cases cost stayed flat, because every review now runs at least one hunter.
- Running another wave only after a `proven` finding, the lean packet and cheaper verifiers for minor-only clusters
  did not move the cost of this suite: a second wave rarely ran here, a small diff's packet is a small share of
  the context every subagent starts with, and clusters of only minor findings are rare. Subagents account for
  about four fifths of the cost, mostly their fixed starting context.
- A background agent's report used to switch the review stop guard off, so 2 of 24 runs ended with findings
  unjudged. With the guard kept on, every review finishes its verification, which costs more on small cases.
- Precision is lower than in 4.1.0 because hunters report more findings outside the seeded set. Some of these are real
  issues that the cases did not seed; the scorer counts them as unmatched.
- These numbers rest on 1-3 passes per version, small synthetic cases, one model and one platform. At this sample size
  a recall difference below about 13 points cannot be told apart from noise.

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
