# Benchmark

Craftsman is evaluated on two benchmarks using `gemini-3.8-flash-low` via Antigravity CLI (`agy -p`):

1. **Seeded-defect review ([`evals/`](../evals/README.md)):** 8 cases, 27 defects, 18 decoys — measures defect recall, precision, subagents, tokens, and time on `/craft-review` across `5.1.0` pre-opt, wave optimization (`27556a4`), and `5.2.0` (`ab08b11`).
2. **Build-and-verify ([`evals/build_cases/`](../evals/README.md#build-and-verify-benchmark)):** 6 multi-module Python tasks (`110` hidden tests across 2 passes) — measures end-to-end `/craftsman` implementation + gate review (`00a8fab` and `5.2.0`) against an unassisted `baseline`.

Windows harness conditions: `5.2.0` (`3eb5e65` / `ab08b11`) executes [`hooks.json`](../hooks.json) natively on Windows (`cmd /c`) and POSIX (`sh -c`) with zero temporary hook patching (`hooks_patched_win=False`, `0` hook errors, `28/28` enforced ledgers across both `5.2.0` suites). Earlier pre-`5.2.0` Flash runs (`5.1.0`, `27556a4`, `00a8fab`) temporarily rewrote `./scripts/craftsman-*.py` to `python scripts\craftsman-*.py` inside benchmark temp copies before `3eb5e65` landed. Antigravity runs are subscription-billed, so cost is reported in tokens summed across the orchestrator and all subagent transcripts (`0` cache-write tokens recorded).

---

## 1. Seeded-Defect Review — Protocol Evolution (`evals/`)

Each arm ran `2` passes across all 8 cases (`30 min` timeout, concurrency `3`).

- `baseline`: `evals.run.BASELINE_PROMPT`, no plugin or `craftsman` on `PATH` (`16/16 ok`).
- `craftsman` (`5.1.0` pre-opt): normal+deep waves up to generation 3 with `3` variants/wave and per-hunter verifiers (`14 ok`, `2` timeouts at `30 min` on pass-1 `feedbox#1` and `orderdesk#1`, both scored with kept ledgers).
- `craftsman-opt` (`27556a4`): tightened wave budgets (normal stops at gen 2 with `0` variants; deep caps at `1` variant/wave and enters gen 3 only on gen-2 `blocker`s; `minor`/`nit` findings skip waves; batched verifiers `<= 6`/verifier). Two 0-turn CLI startup flakes (`attach#1`, `feedbox#2`: `"empty conversation id"` in `3s`) were excluded by `evals.run compare` as invalid and replaced once (`15 ok`, `1` valid timeout on `feedbox#3` catching `5/6`).
- `craftsman-5.2.0` (`ab08b11`): adaptive follow-up rounds, scoped hunk reopening on fixes, complete lens sets per wave slot (`d393fba`, `f039f3b`), and native cross-platform `hooks.json` (`16/16` valid runs: `13 ok`, `3` timeouts on `orderdesk#1`, `feedbox#2`, `orderdesk#2`; `0` hook errors; `16/16` enforced).

### Summary (Mean per Pass, `n=2`)

| Arm | Valid Runs | Recall (Range) | Precision | Decoys | Unmatched | Time / Pass | Main Steps | Subagents | Tokens / Pass (`in` · `out` · `cache read`) |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| `baseline` | 16/16 `ok` | **89%** `24.00/27` (`89%–89%`) | **100%** | 0.00 | 0.00 | **11.9 min** | 138 | 0.0 | **3.3M** (`417k` · `14k` · `2.9M`) |
| `craftsman` (`5.1.0`) | 16 (`14 ok`, `2 t/o`) | **98%** `26.50/27` (`96%–100%`) | **83%** | 1.50 | 29.00 | **144.1 min** (`×12.14`) | 426 (`×3.08`) | 208.0 | **111.3M** (`×33.54`; `13.6M` · `460k` · `97.3M`) |
| `craftsman-opt` (`27556a4`) | 16 (`15 ok`, `1 t/o`) | **94%** `25.50/27` (`+6 pts`) | **81%** | 1.00 | 21.00 | **135.4 min** (`×11.40`) | 282 (`×2.04`) | **116.5** (`-44%`) | **71.2M** (`×21.46`, **`-36%`**; `8.5M` · `320k` · `62.4M`) |
| **`craftsman-5.2.0`** (`ab08b11`) | 16 (`13 ok`, `3 t/o`) | **98%** `26.50/27` (`96%–100%`) | **84%** | 1.00 | **16.00** | **142.7 min** (`×12.02`) | 300 (`×2.17`) | **111.0** (`-47%`) | **86.6M** (`×26.08`, `-22%`; `9.1M` · `304k` · `77.2M`) |

Welch 90% recall interval (`craftsman-5.2.0` minus `baseline`): `+9.3 pts` (`-2.4 to +21.0 pts`). `craftsman-5.2.0` caught `2/2` (`100%`) on all three defects `baseline` missed twice (`inv-2`, `od-6`, `tb-3`), restored `od-4` (`2/2` vs `1/2` in `27556a4`) and `fb-6` (`1/2` vs `0/2` in `27556a4`), achieved the highest Craftsman precision (**`84%`**), and cut unmatched findings by **`-45%`** (`29.00 → 16.00`) vs `5.1.0`.

### Per-Case Breakdown (`baseline` / `5.1.0` / `27556a4` / `5.2.0`)

| Case | Defects | Recall (`base / 5.1.0 / opt / 5.2.0`) | Subagents (`5.1.0 / opt / 5.2.0`) | Tokens (`base / 5.1.0 / opt / 5.2.0`) | Time / Run (`base / 5.1.0 / opt / 5.2.0`) |
| :--- | :---: | :---: | :---: | :---: | :---: |
| `attach` (small) | 2 | `100% / 100% / 100% / 100%` | `18.5 / 12.0 / 10.5` | `214k / 8.0M / 5.4M / 5.9M` | `1.3 / 19.7 / 18.1 / 20.1 min` |
| `invtool` (small) | 2 | `50% / 100% / 100% / 100%` | `18.0 / 7.5 / 10.0` | `108k / 5.5M / 4.4M / 4.2M` | `0.7 / 12.4 / 13.8 / 16.6 min` |
| `kvstore` (small) | 2 | `100% / 100% / 100% / 100%` | `21.0 / 12.0 / 11.5` | `144k / 8.2M / 5.9M / 6.5M` | `0.9 / 19.5 / 15.4 / 16.6 min` |
| `quotes` (small) | 3 | `100% / 100% / 100% / 100%` | `18.0 / 10.0 / 9.0` | `109k / 5.5M / 4.0M / 3.3M` | `0.6 / 13.4 / 12.9 / 12.5 min` |
| `reminders` (small) | 3 | `100% / 100% / 100% / 100%` | `21.0 / 9.5 / 9.0` | `209k / 6.0M / 3.4M / 2.8M` | `1.3 / 12.1 / 11.1 / 9.2 min` |
| `taskboard` (small) | 3 | `67% / 100% / 100% / 100%` | `19.0 / 7.0 / 8.0` | `164k / 5.9M / 3.0M / 3.2M` | `0.9 / 11.6 / 9.4 / 9.3 min` |
| `feedbox` (large) | 6 | `100% / 92% / 83% / 92%` | `39.5 / 31.5 / 26.0` | `1.9M / 30.6M / 21.5M / 31.6M` | `4.5 / 28.6 / 29.3 / 28.4 min` |
| `orderdesk` (large) | 6 | `83% / 100% / 92% / 100%` | `53.0 / 27.0 / 27.0` | `476k / 41.6M / 23.6M / 29.0M` | `1.7 / 26.9 / 25.4 / 30.0 min` |
| **TOTAL (1 pass)** | **27** | **`89% / 98% / 94% / 98%`** | **`208.0 / 116.5 / 111.0`** (`-47%`) | **`3.3M / 111.3M / 71.2M / 86.6M`** (`-22%`) | **`11.9 / 144.1 / 135.4 / 142.7 min`** |

Per `evals.run breakdown`, `craftsman-5.2.0` averaged `7.2` hunters (`0.3` `variant`, `1.4` `fresh`, `0.1` gen 3) and `6.6` verifiers per run, finding `50/53` seeded defect hits in gen 1 and `3/53` in gen 2 while cutting unmatched noise from `29.00 → 16.00`.

---

## 2. Build-and-Verify A/B Suite (`evals/build_cases/`, `00a8fab` → `5.2.0`)

To test full coding workflows rather than pure review, 6 self-contained multi-module Python tasks (`ledger_batch`, `jobqueue_retry`, `config_overlay`, `docindex_search`, `pkgcache_sync`, `schedule_rrule`) were built with starter tests passing out of the box and external `hidden_tests.py` withheld from agent workspaces.

Each arm ran `2` passes (`12` runs/arm, concurrency `3`, `40 min` timeout). Prompts required pure-stdlib implementation and unit tests matching `TASK.md`. Note: before the first benchmark, a smoke run caught a real mid-round edit race fixed in `00a8fab` (editing files while `W2-X2` was still running caused a 106-step loop).

### Summary (`n=12` Runs per Arm)

| Arm | Completed (`ok`) | Enforced Ledgers | Full Case Pass (All / `ok` Runs) | Hidden Tests (All / `ok` Runs) | Starter Tests | Review Activity | Subagents / Run | Mean Tokens / Run | Mean Time / Run |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| `baseline` | 12/12 | 0/12 | **66.7%** (`8/12`) | **96.4%** (`106/110`) | 100% | — | 0.0 | **404k** (`4.8M` total) | **2.9 min** (`176s`) |
| `craftsman` (`00a8fab`) | 9/12 (`3` t/o) | 11/12 | **58.3%** (`7/12`) / **55.6%** (`5/9`) | **95.5%** (`105/110`) / **95.3%** | 100% | `181` findings · `30` proven · `82` fixed (`2.00` rnd/run) | 31.2 (`26.4` `ok`) | **28.2M** (`338.4M`, `×69.9`) / `21.0M` `ok` (`×52.1`) | **29.1 min** (`×9.9`) / `25.5 min` `ok` |
| **`craftsman-5.2.0`** (`ab08b11`) | 5/12 (`7` t/o) | **12/12** | **75.0%** (`9/12`) / **80.0%** (`4/5`) | **97.3%** (`107/110`) / **98.0%** (`48/49`) | 100% | `217` findings · `32` proven · `49` fixed (`1.67` rnd/run) | **24.2** (`16.6` `ok`) | **21.7M** (`260.5M`, `×53.8`, `-23%`) / **`12.8M`** `ok` (`×31.6`, `-39%`) | **33.8 min** (`×11.5`) / `25.1 min` `ok` |

Protocol notes:
- `00a8fab`: `schedule_rrule#1` skipped `/craftsman` (`enforced=False`, `0` subagents, `235s`, `8/8` hidden); `config_overlay#2` logged `3` `git config` 5s timeout hook warnings; `config_overlay#1`, `config_overlay#2`, and `docindex_search#2` hit the `40 min` timeout while re-reviewing fixes.
- `5.2.0` (`ab08b11`): all `12/12` runs were enforced with `0` hook errors. Seven runs hit the `40 min` wall-clock cap (`config_overlay#2`, `docindex_search#1/#2`, `pkgcache_sync#1/#2`, `schedule_rrule#1/#2`). In `5` of those `7` timeouts (`config_overlay#2`, both `pkgcache_sync` runs, both `schedule_rrule` runs), the implementation already passed `100%` of starter and hidden tests while running round-2 adaptive re-reviews (`docindex_search#1` timed out while computing verifier clusters after `63` round-1 findings).

### Per-Case Results (All 36 Runs, `run 1, run 2`)

| Case (Hidden Tests) | `baseline` Hidden (Full) | `00a8fab` Hidden (Full) | `5.2.0` Hidden (Full) | Mean Tokens / Time (`base` → `5.2.0`) |
| :--- | :---: | :---: | :---: | :---: |
| `config_overlay` (9) | `8/9, 9/9` (`1/2`) | `9/9 (t/o), 9/9 (t/o)` (`2/2`) | `9/9, 9/9 (t/o)` (**`2/2`**) | `177k · 113s` → `25.1M · 1905s` |
| `docindex_search` (9) | `8/9, 8/9` (`0/2`) | `8/9, 8/9 (t/o)` (`0/2`) | `8/9 (t/o), 8/9 (t/o)` (`0/2`) | `582k · 273s` → `26.0M · 2401s` |
| `jobqueue_retry` (10) | `10/10, 10/10` (`2/2`) | `10/10, 10/10` (`2/2`) | `10/10, 10/10` (**`2/2`**) | `328k · 157s` → `6.9M · 978s` |
| `ledger_batch` (10) | `9/10, 10/10` (`1/2`) | `9/10, 9/10` (`0/2`) | `9/10, 10/10` (`1/2`) | `418k · 169s` → `16.5M · 2084s` |
| `pkgcache_sync` (9) | `9/9, 9/9` (`2/2`) | `8/9, 9/9` (`1/2`) | `9/9 (t/o), 9/9 (t/o)` (**`2/2`**) | `447k · 164s` → `25.6M · 2401s` |
| `schedule_rrule` (8) | `8/8, 8/8` (`2/2`) | `8/8 (no ledger), 8/8` (`2/2`) | `8/8 (t/o), 8/8 (t/o)` (**`2/2`**) | `471k · 178s` → `30.1M · 2400s` |

### What Both Benchmarks Show

1. **Where Craftsman wins:** In dedicated diff review (`evals/`), `5.2.0` reaches **`98%`** recall (`26.50/27`, catching `2/2` on `inv-2`, `od-6`, `tb-3` that `baseline` missed twice) at the highest precision (**`84%`**), with **`45%`** less unmatched noise (`29.00 → 16.00`), **`47%`** fewer subagents (`208.0 → 111.0`), and **`22%`** fewer tokens (`111.3M → 86.6M`) than `5.1.0`.
2. **Quality vs. latency trade-off on greenfield builds (`evals/build_cases/`):** Adaptive follow-up rounds in `5.2.0` lifted full-case passes to **`75.0%`** (`9/12`, `97.3%` hidden, `12/12` enforced) — beating both `00a8fab` (`58.3%`) and `baseline` (`66.7%`) while cutting tokens **`23%`** across all runs (`21.7M`, `-39%` on `ok` runs to `12.8M`) vs `00a8fab`. Even so, `5.2.0` still uses `32×–54×` baseline tokens (`404k`) and `7/12` runs hit the `40 min` cap during round-2 re-reviews (5 after already passing `100%` of hidden tests).
3. **Practical guidance:** Use standalone `npx skills add` skills for small greenfield features and day-to-day work; reserve full `craftsman-gate` for existing risky diffs, legacy refactors, security/concurrency boundaries, and high-stakes PRs.

---

## Limits

- **Sample size (`n=2` passes):** Too small to separate narrow recall differences tightly; 3–5+ repeats give narrower confidence intervals.
- **Self-contained Python stdlib tasks:** Neither suite tests large polyglot monorepos with external services.
- **Single evaluated model (`gemini-3.8-flash-low`):** Token ratios and timeout latency reflect Flash `low`; an attempted full A/B on `claude-sonnet-4-6` hit Antigravity's third-party quota cap after ~12 minutes (after a clean `2/2` smoke run on `invtool` with native Windows `hooks.json`). API-billed frontier models will differ in both speed and cost-benefit.

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
