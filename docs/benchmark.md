# Benchmark

Tested on `gemini-3.8-flash-low` via Antigravity CLI across two separate benchmarks (2 full passes each).

| Test | Without Craftsman | With Craftsman 5.2 |
| :--- | :--- | :--- |
| **1. Finding bugs in existing PRs** (`/craft-review`)<br>8 repos with 27 planted bugs & passing unit tests | Caught **24 / 27** bugs (`89%`)<br>`12 min` · `3.3M` tokens | Caught **26.5 / 27** bugs (**`98%`**)<br>`16` extra notes across 8 repos · `143 min` · `86.6M` tokens |
| **2. Building features from scratch** (`/craftsman`)<br>6 multi-file specs graded by hidden tests | **8 / 12** runs passed 100% (`67%`)<br>`106 / 110` hidden tests · `3 min` · `404k` tokens | **9 / 12** runs passed 100% (**`75%`**)<br>`107 / 110` hidden tests · `34 min` · `21.7M` tokens\* |

*\*In Test 2, `7 / 12` Craftsman runs hit the 40-minute timeout while running second-round re-reviews (`5` of those `7` already passed `100%` of the hidden tests before timing out; runs that finished within the limit averaged `12.8M` tokens and `25 min`, with `4 / 5` passing `100%`).*

### Bottom Line

- **Where Craftsman pays off:** Reviewing finished diffs and tricky changes. Fresh-context reviewers caught all 3 subtle bugs the plain prompt missed every single time (CLI help contradicting code and weak tests that passed anyway).
- **Where it's overkill:** Small greenfield features. Craftsman did beat the baseline (`75%` vs `67%` fully passing tasks), but at roughly `54×` the tokens and `11×` the wait time.
- **Practical recommendation:** Use the standalone `npx skills add` skills for day-to-day coding; turn on the full Stop-hook gate for risky PRs, legacy refactors, and high-stakes code.

---

## 1. Test 1: Finding Bugs in Existing PRs ([`evals/`](../evals/README.md))

Eight small-to-medium Python repos where a PR implements a written spec, all shipped unit tests pass, and 27 realistic bugs (plus 18 correct-looking decoys) are planted in the diff. Each repo was reviewed twice with a plain prompt and twice with `/craft-review` (`30 min` cap per repo).

| Project | Planted bugs | Bugs caught (`Without → With`) | Time / run (`Without → With`) | Tokens / run (`Without → With`) |
| :--- | :---: | :---: | :---: | :---: |
| `attach` (small) | 2 | `2.0 → 2.0` (`100% → 100%`) | `1.3 → 20.1 min` | `214k → 5.9M` |
| `invtool` (small) | 2 | `1.0 → 2.0` (`50% → 100%`) | `0.7 → 16.6 min` | `108k → 4.2M` |
| `kvstore` (small) | 2 | `2.0 → 2.0` (`100% → 100%`) | `0.9 → 16.6 min` | `144k → 6.5M` |
| `quotes` (small) | 3 | `3.0 → 3.0` (`100% → 100%`) | `0.6 → 12.5 min` | `109k → 3.3M` |
| `reminders` (small) | 3 | `3.0 → 3.0` (`100% → 100%`) | `1.3 → 9.2 min` | `209k → 2.8M` |
| `taskboard` (small) | 3 | `2.0 → 3.0` (`67% → 100%`) | `0.9 → 9.3 min` | `164k → 3.2M` |
| `feedbox` (large) | 6 | `6.0 → 5.5` (`100% → 92%`) | `4.5 → 28.4 min` | `1.9M → 31.6M` |
| `orderdesk` (large) | 6 | `5.0 → 6.0` (`83% → 100%`) | `1.7 → 30.0 min` | `476k → 29.0M` |
| **Total (1 full pass)** | **27** | **`24.0 → 26.5` (`89% → 98%`)** | **`11.9 → 142.7 min`** | **`3.3M → 86.6M`** |

Note: `3` of the `16` Craftsman runs (`orderdesk` twice, `feedbox` once) hit the `30 min` limit and were scored from the findings already logged. Across a full 8-repo pass, Craftsman 5.2 launched `111` helper subagents, flagged `1` decoy, and left `16` extra out-of-scope notes (`84%` accuracy) — cutting subagents by `47%`, extra noise by `45%`, and tokens by `22%` compared to `5.1` while keeping the same `98%` catch rate.

---

## 2. Test 2: Building Features from Scratch ([`evals/build_cases/`](../evals/README.md#build-and-verify-benchmark))

Six multi-module Python tasks where the agent receives a written spec (`TASK.md`) and green starter tests, then writes the implementation and its own tests. After the agent stops, we grade its repo against hidden tests it never saw (`40 min` cap per run, 2 runs per task).

| Task | Hidden tests | Without Craftsman (`run 1, run 2`) | With Craftsman 5.2 (`run 1, run 2`) | Mean time & tokens (`Without → With`) |
| :--- | :---: | :---: | :---: | :---: |
| `config_overlay` | 9 | `8/9, 9/9` (`1/2` full) | `9/9, 9/9 (timeout)` (`2/2` full) | `1.9 min · 177k → 31.8 min · 25.1M` |
| `docindex_search` | 9 | `8/9, 8/9` (`0/2` full) | `8/9 (timeout), 8/9 (timeout)` (`0/2` full) | `4.6 min · 582k → 40.0 min · 26.0M` |
| `jobqueue_retry` | 10 | `10/10, 10/10` (`2/2` full) | `10/10, 10/10` (`2/2` full) | `2.6 min · 328k → 16.3 min · 6.9M` |
| `ledger_batch` | 10 | `9/10, 10/10` (`1/2` full) | `9/10, 10/10` (`1/2` full) | `2.8 min · 418k → 34.7 min · 16.5M` |
| `pkgcache_sync` | 9 | `9/9, 9/9` (`2/2` full) | `9/9 (timeout), 9/9 (timeout)` (`2/2` full) | `2.7 min · 447k → 40.0 min · 25.6M` |
| `schedule_rrule` | 8 | `8/8, 8/8` (`2/2` full) | `8/8 (timeout), 8/8 (timeout)` (`2/2` full) | `3.0 min · 471k → 40.0 min · 30.1M` |
| **Total (12 runs)** | **110** | **`8 / 12` full (`67%`) · `106 / 110` (`96%`)** | **`9 / 12` full (`75%`) · `107 / 110` (`97%`)** | **`2.9 min · 404k → 33.8 min · 21.7M`** |

Across these `12` runs, Craftsman fixed `49` confirmed issues and ran natively on Windows via [`hooks.json`](../hooks.json) with `0` hook errors across all `28` Craftsman runs in both tests (earlier `5.1` runs used a temporary Windows wrapper inside the test harness). Antigravity runs are subscription-billed, so cost is reported as total tokens summed across the orchestrator and all subagent transcripts (`0` cache-write tokens recorded).

---

## Limits

- **Small sample size (`2` passes per task):** Enough to see clear patterns, too small to pin down narrow percentage margins.
- **Self-contained Python stdlib tasks:** Neither test covers large polyglot monorepos or live external services.
- **One evaluated model (`gemini-3.8-flash-low`):** Token ratios and timeout latency reflect Flash `low`; an attempted full run on `claude-sonnet-4-6` hit Antigravity's third-party quota cap after ~12 minutes (after a clean `2/2` smoke run). Stronger or API-billed models will differ in both speed and cost-benefit.

---

## Run It Yourself

```bash
# Test 1: finding planted bugs in existing PRs
python3 -m evals.run list
python3 -m evals.run agent --cases all --arm current --out bench/evals/current.jsonl
python3 -m evals.run compare bench/evals/current.jsonl --defects --tokens
python3 -m evals.run breakdown --out bench/evals/current.jsonl

# Test 2: building features from scratch against hidden tests
python3 -m evals.build_bench list
python3 -m evals.build_bench prepare ledger_batch ~/evals/ledger-1
python3 -m evals.build_bench grade ledger_batch ~/evals/ledger-1
python3 -m evals.build_bench summary bench/build_eval.jsonl
```

See [`evals/README.md`](../evals/README.md) for permissions, grading rules, and replication templates in [GitHub Discussions (Benchmark replications)](https://github.com/BuczynskiRafal/craftsman/discussions?category=benchmark-replications).
