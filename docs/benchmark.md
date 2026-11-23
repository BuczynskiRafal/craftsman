# Benchmark

Craftsman `5.1.0` is evaluated on the repository's 8-case seeded-defect suite ([`evals/`](../evals/README.md): 27 defects, 18 decoys), comparing `/craft-review` against an unassisted `baseline` on the same model.

---

## Gemini 3.8 Flash (`low`) — Craftsman 5.1.0 vs. Baseline

End-to-end A/B evaluation using the Antigravity CLI (`agy -p --output-format json --dangerously-skip-permissions`) with `gemini-3.8-flash-low`, comparing `evals.run.BASELINE_PROMPT` (no plugin or `craftsman` on `PATH`) against Craftsman `5.1.0` loaded via `.agents/plugins/craftsman` and `bin/craftsman.cmd`. Each arm ran `2` complete passes (`32` runs total, concurrency `3`, `30 min` timeout).

Run conditions on Windows:
- `agy` runs hooks via `cmd.exe /c`, where `./scripts/craftsman-*.py` fails; the temporary benchmark plugin copy rewrote hook commands to `python scripts\craftsman-*.py` without changing the repository.
- `~/.gemini/antigravity-cli/settings.json` denies `write_file(.git/)`, so baseline runs wrote `.git/evals/findings.json` via shell commands (two PowerShell UTF-8 BOMs in pass 1 were stripped before scoring, with stdout JSON extraction as fallback).
- Antigravity runs are subscription-billed, so cost is reported in tokens summed across the orchestrator and all subagent transcripts (`0` cache-write tokens recorded). Two pass-1 large Craftsman runs (`feedbox#1` at `5/6`, `orderdesk#1` at `6/6`) hit the 30-minute timeout during late verification and are included with their saved ledgers (`14/16` `ok`, `2/16` `timeout`).

### Summary (Mean per Pass, `n=2`)

| Arm | Passes (Runs) | Recall (Range) | Precision | Decoys / Pass | Unmatched / Pass | Protocol / Hook Errors | Time / Pass | Main Steps / Pass | Subagents / Pass | Tokens / Pass (`in` · `out` · `cache read`) |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| `baseline` | 2 (16/16 `ok`) | **89%** `24.00/27` (`89%–89%`) | **100%** | 0.00 | 0.00 | n/a | **11.9 min** | 138 | 0.0 | **3.3M** (`417k` · `14k` · `2.9M`) |
| `craftsman` (`5.1.0`) | 2 (14 `ok`, 2 `timeout`) | **98%** `26.50/27` (`96%–100%`) | **83%** | 1.50 | 29.00 | 16/16 enforced · 0 errors | **144.1 min** (`×12.14`) | 426 (`×3.08`) | 208.0 | **111.3M** (`×33.54`; `13.6M` · `460k` · `97.3M`) |

Suite recall difference (`craftsman` minus `baseline`): **+9.3 pts** (Welch 90% interval `-2.4 to +21.0 pts` at `n=2` repeats).

### Per-Case Breakdown (`baseline` → `craftsman`)

| Case (Size) | Defects | Recall (`base → craft`, `Δ`) | Precision (`base → craft`) | Craft Decoys | Craft Unmatched | Time / Run (`base → craft`) | Main Steps (`base → craft`) | Craft Subagents | Tokens / Run (`base → craft`) |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| `attach` (small) | 2 | `2.00` → `2.00` (100% → 100%, `+0 pts`) | `100%` → `90%` | 0.50 | 1.00 | `1.3` → `19.7 min` (`×14.76`) | `15` → `40` | 18.5 | `214k` → `8.0M` (`×37.17`) |
| `invtool` (small) | 2 | `1.00` → `2.00` (50% → 100%, `+50 pts`) | `100%` → `79%` | 0.00 | 2.00 | `0.7` → `12.4 min` (`×18.92`) | `8` → `38` | 18.0 | `108k` → `5.5M` (`×50.71`) |
| `kvstore` (small) | 2 | `2.00` → `2.00` (100% → 100%, `+0 pts`) | `100%` → `86%` | 0.00 | 2.00 | `0.9` → `19.5 min` (`×21.71`) | `11` → `47` | 21.0 | `144k` → `8.2M` (`×57.01`) |
| `quotes` (small) | 3 | `3.00` → `3.00` (100% → 100%, `+0 pts`) | `100%` → `88%` | 0.00 | 1.50 | `0.6` → `13.4 min` (`×20.65`) | `8` → `40` | 18.0 | `109k` → `5.5M` (`×50.73`) |
| `reminders` (small) | 3 | `3.00` → `3.00` (100% → 100%, `+0 pts`) | `100%` → `100%` | 0.00 | 0.00 | `1.3` → `12.1 min` (`×9.66`) | `14` → `42` | 21.0 | `209k` → `6.0M` (`×28.88`) |
| `taskboard` (small) | 3 | `2.00` → `3.00` (67% → 100%, `+33 pts`) | `100%` → `86%` | 0.00 | 1.50 | `0.9` → `11.6 min` (`×12.68`) | `10` → `38` | 19.0 | `164k` → `5.9M` (`×36.10`) |
| `feedbox` (large) | 6 | `6.00` → `5.50` (100% → 92%, `-8 pts`) | `100%` → `70%` | 1.00 | 8.50 | `4.5` → `28.6 min` (`×6.33`) | `54` → `90` | 39.5 | `1.9M` → `30.6M` (`×16.15`) |
| `orderdesk` (large) | 6 | `5.00` → `6.00` (83% → 100%, `+17 pts`) | `100%` → `66%` | 0.00 | 12.50 | `1.7` → `26.9 min` (`×16.27`) | `18` → `93` | 53.0 | `476k` → `41.6M` (`×87.32`) |
| **TOTAL (1 pass)** | **27** | **`24.00` → `26.50` (`89% → 98%`, `+9 pts`)** | **`100%` → `83%`** | **1.50** | **29.00** | **`11.9` → `144.1 min` (`×12.14`)** | **`138` → `426`** | **208.0** | **`3.3M` → `111.3M` (`×33.54`)** |

### Key Takeaways

1. **Missed defects caught (`2/2` vs. `0/2`):** Baseline missed `inv-2` (`help-contradicts-code`), `od-6` (`weak-test`), and `tb-3` (`weak-test`) in both passes. Craftsman caught all three in both passes, reaching `27/27` (`100%`) in pass 2 (its lone miss, `fb-6`, happened when `feedbox#1` timed out at 30 min in pass 1).
2. **Cost concentrated in follow-up waves:** Per `evals.run breakdown`, `49/53` (`92%`) seeded-defect first detections happened in generation 1 (`42` `bug`, `3` `simplicity`, `4` `sweep`), `4/53` in generation-2 `fresh` slots on large cases, and **0** in `variant` (`4.9` hunters/run) or generation 3 (`3.1` hunters/run). Runs averaged `12.9` hunters and `13.1` verifiers (`26.0` subagents/run), with subagents accounting for `84.8%` of tokens (`94.4M` of `111.3M` per pass).
3. **Precision (`83%` vs. `100%`):** Verifiers merged `5.4` duplicates and dismissed `1.6` candidates per run, leaving `1.50` decoy hits (`att-d1`, `fb-d3`) and `29.00` unmatched findings per pass (`21.00` on `orderdesk` + `feedbox`).

---

## Limits

- **Sample size (`n=2` passes):** The Welch 90% recall interval (`-2.4 to +21.0 pts`) crosses zero; 3–5+ repeats are needed to measure smaller deltas reliably.
- **Synthetic Python suite:** All 8 cases are self-contained Python CLI/library PRs rather than multi-language production repositories.
- **Single cheap subscription model:** Runs used `gemini-3.8-flash-low` on Antigravity; cost is measured in tokens rather than USD and will differ on frontier or API-billed models.
- **Cost multiplier:** On Flash, spawning follow-up waves multiplied wall time (`×12.14`, including `2` 30-minute timeouts) and tokens (`×33.54`).

---

## Replicate

Run automated Claude Code A/B benchmarks or prepare individual cases for any agent platform:

```bash
python3 -m evals.run agent --cases all --arm current --out bench/evals/current.jsonl
python3 -m evals.run compare bench/evals/current.jsonl --defects --tokens
python3 -m evals.run breakdown --out bench/evals/current.jsonl

# Or prepare/score any case manually on any agent platform:
python3 -m evals.run list
python3 -m evals.run prepare quotes ~/evals/quotes-1
python3 -m evals.run score quotes ~/evals/quotes-1
```

See [`evals/README.md`](../evals/README.md) for permissions, matching rules, statistical repeat sizing, and how to add cases. Share results in [GitHub Discussions (Benchmark replications)](https://github.com/BuczynskiRafal/craftsman/discussions?category=benchmark-replications).
