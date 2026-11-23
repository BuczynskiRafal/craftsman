# Benchmark

Craftsman is evaluated on two benchmarks using `gemini-3.8-flash-low` via Antigravity CLI (`agy -p`):

1. **Seeded-defect review ([`evals/`](../evals/README.md)):** 8 cases, 27 defects, 18 decoys — measures defect recall, precision, subagents, tokens, and time on `/craft-review` before and after wave optimization (`27556a4`).
2. **Build-and-verify ([`evals/build_cases/`](../evals/README.md#build-and-verify-benchmark)):** 6 multi-module Python tasks (`110` hidden tests across 2 passes) — measures end-to-end `/craftsman` implementation + gate review (`00a8fab`) against an unassisted `baseline`.

Windows harness conditions: `agy` executes hooks via `cmd.exe /c`, where `./scripts/craftsman-*.py` fails; temporary benchmark plugin copies rewrote hook commands to `python scripts\craftsman-*.py` without changing repository files. Antigravity runs are subscription-billed, so cost is reported in tokens summed across the orchestrator and all subagent transcripts (`0` cache-write tokens recorded).

---

## 1. Seeded-Defect Review — Wave Optimization A/B (`evals/`)

Each arm ran `2` passes across all 8 cases (`30 min` timeout, concurrency `3`).

- `baseline`: `evals.run.BASELINE_PROMPT`, no plugin or `craftsman` on `PATH` (`16/16 ok`).
- `craftsman` (`5.1.0` pre-opt): normal+deep waves up to generation 3 with `3` variants/wave and per-hunter verifiers (`14 ok`, `2` timeouts at `30 min` on pass-1 `feedbox#1` and `orderdesk#1`, both scored with kept ledgers).
- `craftsman-opt` (`27556a4`): normal depth stops at gen 2 (`0` variants); deep depth caps at `1` variant/wave and enters gen 3 only on gen-2 `blocker`s; `minor`/`nit` findings never trigger waves; verifiers batch across files (`<= 6`/verifier). Two 0-turn CLI startup flakes (`attach#1`, `feedbox#2`: `"empty conversation id"` in `3s`) were excluded by `evals.run compare` as invalid and replaced once (`15 ok`, `1` valid timeout on `feedbox#3` catching `5/6`).

### Summary (Mean per Pass, `n=2`)

| Arm | Valid Runs | Recall (Range) | Precision | Decoys | Unmatched | Time / Pass | Main Steps | Subagents | Tokens / Pass (`in` · `out` · `cache read`) |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| `baseline` | 16/16 `ok` | **89%** `24.00/27` (`89%–89%`) | **100%** | 0.00 | 0.00 | **11.9 min** | 138 | 0.0 | **3.3M** (`417k` · `14k` · `2.9M`) |
| `craftsman` (`5.1.0`) | 16 (`14 ok`, `2 t/o`) | **98%** `26.50/27` (`96%–100%`) | **83%** | 1.50 | 29.00 | **144.1 min** (`×12.14`) | 426 (`×3.08`) | 208.0 | **111.3M** (`×33.54`; `13.6M` · `460k` · `97.3M`) |
| `craftsman-opt` (`27556a4`) | 16 (`15 ok`, `1 t/o`) | **94%** `25.50/27` (`+6 pts`) | **81%** | 1.00 | 21.00 | **135.4 min** (`×11.40`) | 282 (`×2.04`) | **116.5** (`-44%`) | **71.2M** (`×21.46`, **`-36%`**; `8.5M` · `320k` · `62.4M`) |

Welch 90% recall interval (`craftsman` minus `baseline`): `+9.3 pts` (`-2.4 to +21.0 pts`). Both `craftsman` and `craftsman-opt` caught `2/2` (`100%`) on all three defects `baseline` missed twice (`inv-2`, `od-6`, `tb-3`); `craftsman-opt` missed `fb-6` (`0/2`) and `od-4` (`1/2`).

### Per-Case Breakdown (`baseline` / `craftsman` / `craftsman-opt`)

| Case | Defects | Recall (`base / pre / opt`) | Subagents (`pre → opt`) | Tokens (`base / pre / opt`) | Time / Run (`base / pre / opt`) |
| :--- | :---: | :---: | :---: | :---: | :---: |
| `attach` (small) | 2 | `100% / 100% / 100%` | `18.5 → 12.0` | `214k / 8.0M / 5.4M` | `1.3 / 19.7 / 18.1 min` |
| `invtool` (small) | 2 | `50% / 100% / 100%` | `18.0 → 7.5` | `108k / 5.5M / 4.4M` | `0.7 / 12.4 / 13.8 min` |
| `kvstore` (small) | 2 | `100% / 100% / 100%` | `21.0 → 12.0` | `144k / 8.2M / 5.9M` | `0.9 / 19.5 / 15.4 min` |
| `quotes` (small) | 3 | `100% / 100% / 100%` | `18.0 → 10.0` | `109k / 5.5M / 4.0M` | `0.6 / 13.4 / 12.9 min` |
| `reminders` (small) | 3 | `100% / 100% / 100%` | `21.0 → 9.5` | `209k / 6.0M / 3.4M` | `1.3 / 12.1 / 11.1 min` |
| `taskboard` (small) | 3 | `67% / 100% / 100%` | `19.0 → 7.0` | `164k / 5.9M / 3.0M` | `0.9 / 11.6 / 9.4 min` |
| `feedbox` (large) | 6 | `100% / 92% / 83%` | `39.5 → 31.5` | `1.9M / 30.6M / 21.5M` | `4.5 / 28.6 / 29.3 min` |
| `orderdesk` (large) | 6 | `83% / 100% / 92%` | `53.0 → 27.0` | `476k / 41.6M / 23.6M` | `1.7 / 26.9 / 25.4 min` |
| **TOTAL (1 pass)** | **27** | **`89% / 98% / 94%`** | **`208.0 → 116.5`** (`-44%`) | **`3.3M / 111.3M / 71.2M`** (`-36%`) | **`11.9 / 144.1 / 135.4 min`** |

Per `evals.run breakdown`, optimization cut `variant` hunters from `4.9` to `0.4`/run, generation-3 hunters from `3.1` to `0.4`/run, verifiers from `13.1` to `7.1`/run, decoys (`1.50 → 1.00`), and unmatched noise (`29.00 → 21.00`).

---

## 2. Build-and-Verify A/B Suite (`evals/build_cases/`, `00a8fab`)

To test full coding workflows rather than pure review, 6 self-contained multi-module Python tasks (`ledger_batch`, `jobqueue_retry`, `config_overlay`, `docindex_search`, `pkgcache_sync`, `schedule_rrule`) were built with starter tests passing out of the box and external `hidden_tests.py` withheld from agent workspaces.

Both arms ran `2` passes (`24` runs total, concurrency `3`, `40 min` timeout). Prompts required pure-stdlib implementation and unit tests matching `TASK.md`. Note: a pre-benchmark smoke run caught a real mid-round edit race fixed in `00a8fab` (editing files while `W2-X2` was still running caused a 106-step loop).

### Summary (`n=12` Runs per Arm)

| Arm | Completed (`ok`) | Enforced Ledgers | Full Case Pass (All / `ok` Runs) | Hidden Tests (All / `ok` Runs) | Starter Tests | Review Activity | Subagents / Run | Mean Tokens / Run | Mean Time / Run |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| `baseline` | 12/12 | 0/12 | **66.7%** (`8/12`) | **96.4%** (`106/110`) | 100% | — | 0.0 | **404k** (`4.8M` total) | **2.9 min** (`175.7s`) |
| `craftsman` | 9/12 (`3` t/o) | 11/12 | **58.3%** (`7/12`) / **55.6%** (`5/9`) | **95.5%** (`105/110`) / **95.2%** (`79/83`) | 100% | `181` findings · `30` proven · `82` fixed (`2.00` rnd/run) | 31.2 (`26.4` `ok`) | **28.2M** (`338.4M`, `×69.9`) / `21.0M` `ok` (`×52.1`) | **29.1 min** (`×9.9`) / `25.5 min` `ok` |

Protocol notes: `schedule_rrule#1` skipped `/craftsman` (`enforced=False`, `0` subagents, `235s`, `8/8` hidden); `config_overlay#2` logged `3` `git config` 5s timeout hook warnings; `config_overlay#1`, `config_overlay#2`, and `docindex_search#2` hit the `40 min` timeout while re-reviewing fixes (`config_overlay` already passed `9/9` hidden tests in both timeouts).

### Per-Case Results (All 24 Runs, `run 1` / `run 2`)

| Case (Hidden Tests) | `baseline` Hidden Passed | `craftsman` Hidden Passed | `baseline` Mean Tokens / Time | `craftsman` Mean Tokens / Time |
| :--- | :---: | :---: | :---: | :---: |
| `config_overlay` (9) | `8/9`, `9/9` (`1/2` full) | `9/9` (t/o), `9/9` (t/o) (**`2/2`** full) | `177k` · `113s` | `56.9M` · `2402s` (t/o) |
| `docindex_search` (9) | `8/9`, `8/9` (`0/2` full) | `8/9`, `8/9` (t/o) (`0/2` full) | `582k` · `273s` | `38.1M` · `2244s` |
| `jobqueue_retry` (10) | `10/10`, `10/10` (`2/2` full) | `10/10`, `10/10` (`2/2` full) | `328k` · `157s` | `9.0M` · `1162s` |
| `ledger_batch` (10) | `9/10`, `10/10` (`1/2` full) | `9/10`, `9/10` (`0/2` full) | `418k` · `169s` | `19.6M` · `1554s` |
| `pkgcache_sync` (9) | `9/9`, `9/9` (`2/2` full) | `8/9`, `9/9` (`1/2` full) | `447k` · `164s` | `27.2M` · `1986s` |
| `schedule_rrule` (8) | `8/8`, `8/8` (`2/2` full) | `8/8` (no ledger), `8/8` (`2/2` full) | `471k` · `178s` | `18.5M` · `1137s` |

### What Both Benchmarks Show

1. **Where Craftsman wins:** In dedicated code review of finished diffs (`evals/`), fresh-context hunters reliably catch cross-file propagation and doc-vs-code or weak-test bugs that single-pass Flash `low` misses (`inv-2`, `od-6`, `tb-3`: `2/2` vs `0/2`). Optimizing waves (`27556a4`) cut subagents by `44%` and tokens by `36%`.
2. **Where full gating hurts on small greenfield tasks:** When Flash `low` builds small stdlib modules cleanly (`96.4%` baseline hidden pass rate at `404k` tokens), running full `/craftsman` review loops burns `52×–70×` tokens (`21M–28M`) and `10×` wall time without improving spec edge-case pass rates (`58.3%` all / `55.6%` `ok` vs `66.7%` baseline). Hunters spent waves on internal style and test naming while shared spec misses (`AND`/`OR`/`NOT` precedence in `docindex_search`, `bool`-as-`int` validation in `ledger_batch`) slipped through because neither arm saw `hidden_tests.py`.
3. **Practical guidance:** Use standalone `npx skills add` skills for small greenfield features and day-to-day work; reserve full `craftsman-gate` for existing risky diffs, legacy refactors, security/concurrency boundaries, and high-stakes PRs.

---

## Limits

- **Sample size (`n=2` passes):** Too small to distinguish small recall deltas cleanly; 3–5+ repeats are needed for tight confidence intervals.
- **Self-contained Python stdlib tasks:** Neither suite tests large polyglot monorepos with external services.
- **Single cheap model (`gemini-3.8-flash-low`):** Token ratios and spec-adherence misses reflect Flash `low`; API-billed frontier models will differ in both latency and cost-benefit.

---

## Replicate

```bash
# Seeded-defect review suite
python3 -m evals.run list
python3 -m evals.run agent --cases all --arm current --out bench/evals/current.jsonl
python3 -m evals.run compare bench/evals/current.jsonl --defects --tokens
python3 -m evals.run breakdown --out bench/evals/current.jsonl

# Build-and-verify suite
python3 -m evals.build_bench list
python3 -m evals.build_bench prepare ledger_batch ~/evals/ledger-1
python3 -m evals.build_bench grade ledger_batch ~/evals/ledger-1
python3 -m evals.build_bench summary bench/build_eval.jsonl
```

See [`evals/README.md`](../evals/README.md) for permissions, grading rules, and replication templates in [GitHub Discussions (Benchmark replications)](https://github.com/BuczynskiRafal/craftsman/discussions?category=benchmark-replications).
