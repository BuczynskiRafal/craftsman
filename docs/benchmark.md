# Craftsman Benchmark & Evaluation Methodology

This document outlines the benchmark evaluation methodology, empirical pilot findings on real-world pull requests,
known limitations, and reproduction guidelines for Craftsman.

---

## Overview

Pilot run in November 2026 on five pull requests from the [SWR-Bench](https://doi.org/10.1145/3808144) dataset:

| Instance | Title | Ground truth |
| :--- | :--- | :--- |
| `pallets__flask-2748` | Fix blueprint route for "" | `D1` |
| `pytest-dev__pytest-8537` | fix #8464 wrong root dir when -c is passed | `D2–D6` |
| `pytest-dev__pytest-13293` | Fix incorrect pyc cache loaded when test file is updated in short time | `D7–D9` |
| `psf__requests-764` | test and fix regressions from #760 | none (control) |
| `pytest-dev__pytest-5680` | Fix some typos | none (control) |

The nine ground-truth items `D1–D9` are the review comments SWR-Bench records for the three PRs, in dataset order.
SWR-Bench types three of them as functional (`D1`, `D3`, `D9`) and six as evolvability (`D2`, `D4–D8`: documentation,
formatting, test structure, changelog); the list is in the per-item matrix below. SWR-Bench labels a PR a control
when it was merged without review comments.

Exploratory Claude Code pilot runs (one repetition per configuration) were also made on the same five PRs under
Craftsman 5.0 and are omitted from the pilot tables; automated Claude Code and Antigravity runs on the seeded defect
suite are reported below.

---

## Antigravity Pilot Results (Craftsman 5.0)

Each configuration reviewed the five PRs twice offline, one repetition per arm: an unassisted `baseline` run, then
`/craft-review` under Craftsman 5.0 with fresh-context `craftsman-reviewer` and `craftsman-verifier` subagents.

The runs are not uniform:

- The Gemini runs (9 November) wrote schema-4 ledgers and ran up to three waves. The Claude Sonnet 5.5 runs
  (7–8 November) wrote schema-3 ledgers, so they ran on a build from before the schema-4 ledger; all of them ran
  one wave, with 1–4 hunters per PR. Their time and tool-call counts are not comparable with the Gemini rows.
- The session-start hook that states the offline and protocol rules was reworded between runs (seven variants
  over both arms). One run (Argon `high`, `pytest-8537`) also had a rule limiting each subagent to 8 tool calls.

Scoring is manual. An item counts as found when a reported finding names the problem the reviewer raised. A
different defect on the same lines does not count. The second column applies the same rule to the six defects
`B1–B6` that no reviewer raised; they are listed, with their limits as a measure, in the last section of this
chapter.

| Agent & Configuration | Ground-Truth Items Found, of 9 (`baseline` → `craftsman`) | Defects Beyond Ground Truth, of 6 (`baseline` → `craftsman`) | Avg Time / PR | Findings (5 PRs) | Subagents (`hunter` / `verifier` per PR) |
| :--- | :---: | :---: | :---: | :---: | :---: |
| Antigravity · Gemini 3.8 Flash (`low`) | 33% (3/9) → 33% (3/9) | 1 → 2 | 0.7 → 5.1 min | 5 → 13 | `3–8` / `0–7` |
| Antigravity · Gemini 3.8 Flash (`medium`) | 22% (2/9) → 33% (3/9) | 4 → 4 | 3.1 → 18.4 min | 7 → 22 | `3–10` / `0–14` |
| Antigravity · Gemini 3.8 Flash (`high`) | 22% (2/9) → 44% (4/9) | 4 → 4 | 5.5 → 24.7 min | 11 → 18 | `3–9` / `0–12` |
| Antigravity · Gemini 4 Argon (`low`) | 11% (1/9) → 33% (3/9) | 2 → 4 | 2.4 → 12.6 min | 5 → 17 | `3–8` / `0–7` |
| Antigravity · Gemini 4 Argon (`medium`) | 22% (2/9) → 44% (4/9) | 5 → 5 | 3.5 → 25.4 min | 8 → 19 | `3–10` / `0–10` |
| Antigravity · Gemini 4 Argon (`high`) | 11% (1/9) → 22% (2/9) | 3 → 6 | 5.2 → 27.8 min | 6 → 17 | `3–10` / `0–11` |
| Antigravity · Claude Sonnet 5.5 (`low`) | 22% (2/9) → 33% (3/9) | 0 → 2 | 0.2 → 1.3 min | 4 → 7 | `1–2` / `0–1` |
| Antigravity · Claude Sonnet 5.5 (`medium`) | 22% (2/9) → 44% (4/9) | 1 → 1 | 0.2 → 1.2 min | 4 → 8 | `1–2` / `0–2` |
| Antigravity · Claude Sonnet 5.5 (`high`) | 11% (1/9) → 44% (4/9) | 2 → 2 | 0.3 → 2.9 min | 3 → 14 | `1–4` / `0–3` |

### Token Usage & Tool Calls

Runs go through the subscription CLI (`agy -p --output-format json`). Conversation and tool-call counts come from
`meta.json` and cover the orchestrator and every subagent. Token counts are the per-step counts summed over all
transcripts of a run. The `usage` block of `response.json`, which `meta.json` copies, covers only the orchestrator
conversation and is not used here. Subagent transcripts carry no thinking-token count, so thinking tokens are not
reported. The table shows `baseline` → `craftsman`.

| Agent & Configuration | Conversations (5 PRs) | Avg Tool Calls / PR | Avg Input + Output Tokens / PR | Avg Cache Read Tokens / PR | 5-PR Total Tokens (`in+out` · `cache`) |
| :--- | :---: | :---: | :---: | :---: | :---: |
| Gemini 3.8 Flash (`low`) | 5 → 51 | 17 → 224 | 104k → 1.33M | 0.26M → 4.63M | 519k → 6.65M · 1.28M → 23.16M |
| Gemini 3.8 Flash (`medium`) | 5 → 87 | 37 → 663 | 246k → 3.82M | 1.04M → 25.13M | 1.23M → 19.12M · 5.19M → 125.64M |
| Gemini 3.8 Flash (`high`) | 5 → 76 | 55 → 707 | 346k → 4.10M | 2.06M → 32.44M | 1.73M → 20.48M · 10.30M → 162.19M |
| Gemini 4 Argon (`low`) | 5 → 51 | 16 → 184 | 60k → 585k | 0.52M → 3.87M | 298k → 2.93M · 2.62M → 19.37M |
| Gemini 4 Argon (`medium`) | 5 → 96 | 15 → 525 | 72k → 2.60M | 0.40M → 20.26M | 359k → 12.98M · 2.00M → 101.29M |
| Gemini 4 Argon (`high`) | 5 → 52 | 20 → 170 | 89k → 689k | 0.58M → 3.91M | 446k → 3.44M · 2.88M → 19.53M |
| Claude Sonnet 5.5 (`low`) | 5 → 15 | 1 → 24 | 18k → 100k | 0.03M → 0.69M | 88k → 499k · 0.14M → 3.45M |
| Claude Sonnet 5.5 (`medium`) | 5 → 15 | 1 → 25 | 18k → 101k | 0.03M → 0.67M | 91k → 505k · 0.16M → 3.34M |
| Claude Sonnet 5.5 (`high`) | 5 → 23 | 2 → 50 | 23k → 205k | 0.05M → 1.45M | 116k → 1.03M · 0.27M → 7.23M |

### Evidence Ledger Verdicts (5 PRs)

| Configuration (`craftsman` 5.0) | Max Wave (`W1..W3`) per PR | Total Hunters / Verifiers (5 PRs) | Ledger Candidates | `proven` (script-reproduced) | `probable` | `question` | `dismissed` (rejected by verifier) | Final Reported Findings |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| Gemini 3.8 Flash (`low`) | `[2, 3, 3, 2, 1]` | 29 / 17 | 22 | 3 (14%) | 15 | 0 | 4 (18%) | 13 |
| Gemini 3.8 Flash (`medium`) | `[2, 3, 2, 3, 1]` | 32 / 40 | 58 | 17 (29%) | 14 | 2 | 25 (43%) | 22 |
| Gemini 3.8 Flash (`high`) | `[2, 3, 2, 3, 1]` | 31 / 40 | 64 | 12 (19%) | 17 | 5 | 30 (47%) | 18 |
| Gemini 4 Argon (`low`) | `[1, 2, 2, 3, 1]` | 25 / 21 | 38 | 6 (16%) | 12 | 3 | 17 (45%) | 17 |
| Gemini 4 Argon (`medium`) | `[3, 3, 3, 2, 1]` | 35 / 30 | 48 | 15 (31%) | 12 | 3 | 18 (38%) | 19 |
| Gemini 4 Argon (`high`) | `[2, 2, 3, 2, 1]` | 29 / 25 | 44 | 15 (34%) | 7 | 0 | 22 (50%) | 17 |
| Claude Sonnet 5.5 (`low`) | `[1, 1, 1, 1, 1]` | 6 / 4 | 7 | 1 (14%) | 6 | 0 | 0 (0%) | 7 |
| Claude Sonnet 5.5 (`medium`) | `[1, 1, 1, 1, 1]` | 6 / 4 | 12 | 0 (0%) | 9 | 1 | 2 (17%) | 8 |
| Claude Sonnet 5.5 (`high`) | `[1, 1, 1, 1, 1]` | 10 / 8 | 29 | 5 (17%) | 12 | 0 | 12 (41%) | 14 |

### Per-Item Ground-Truth Matrix (`D1–D9`)

Items and types as in `data/swr_datasets_d5c5.jsonl`:

- **D1** (`flask-2748`, `F.2 Logic`): `flask/blueprints.py:72` — the new join of prefix and rule drops the trailing slash: `@bp.route('/')` under prefix `/test` gives `/test`, and prefix `/foo/` with rule `''` gives `/foo`
- **D2** (`pytest-8537`, `E.1.1 Textual Changes`): `doc/en/reference/customize.rst:167` — move the `-c` rule to the top of the `rootdir` algorithm and reword it
- **D3** (`pytest-8537`, `F.2 Logic`): `src/_pytest/config/findpaths.py:179` — with `-c`, `rootdir` should be `inipath.parent` unconditionally, without the `locate_config([ancestor])` lookup
- **D4** (`pytest-8537`, `E.2 Visual Representation`): `testing/test_config.py:1409` — no spaces after the commas in the `parametrize` argument string
- **D5** (`pytest-8537`, `E.3.2 Solution Approach`): `testing/test_config.py:1437` — replace the parametrized test with one short test of the `-c` case
- **D6** (`pytest-8537`, `E.1.1 Textual Changes`): `changelog/8464.bugfix.rst:1` — the entry should say what changed
- **D7** (`pytest-13293`, `E.1.1 Textual Changes`): `testing/test_assertrewrite.py:2380` — the new test needs a docstring that says what it checks
- **D8** (`pytest-13293`, `E.1.1 Textual Changes`): `changelog/13292.bugfix.rst` is missing
- **D9** (`pytest-13293`, `F.6 Larger Defects`): `src/_pytest/assertion/rewrite.py:320` — `st_mtime_ns & 0xFFFFFFFF` keeps the low 32 bits, which wrap every ~4.29 s

| Model (`craftsman` 5.0) | D1 | D2 | D3 | D4 | D5 | D6 | D7 | D8 | D9 | Functional (`D1,D3,D9`) | Evolvability (`D2,D4–D8`) | Total `craftsman` | Total `baseline` |
| :--- | :-: | :-: | :-: | :-: | :-: | :-: | :-: | :-: | :-: | :---: | :---: | :---: | :---: |
| Gemini 3.8 Flash (`low`) | — | ✅ | ✅ | — | — | — | — | — | ✅ | 2/3 | 1/6 | 3/9 | 3/9 |
| Gemini 3.8 Flash (`medium`) | — | ✅ | ✅ | — | — | — | — | — | ✅ | 2/3 | 1/6 | 3/9 | 2/9 |
| Gemini 3.8 Flash (`high`) | — | ✅ | — | — | — | ✅ | — | ✅ | ✅ | 1/3 | 3/6 | 4/9 | 2/9 |
| Gemini 4 Argon (`low`) | — | ✅ | ✅ | — | — | — | — | — | ✅ | 2/3 | 1/6 | 3/9 | 1/9 |
| Gemini 4 Argon (`medium`) | ✅ | ✅ | ✅ | — | — | — | — | — | ✅ | 3/3 | 1/6 | 4/9 | 2/9 |
| Gemini 4 Argon (`high`) | — | — | ✅ | — | — | — | — | — | ✅ | 2/3 | 0/6 | 2/9 | 1/9 |
| Claude Sonnet 5.5 (`low`) | — | ✅ | ✅ | — | — | — | — | — | ✅ | 2/3 | 1/6 | 3/9 | 2/9 |
| Claude Sonnet 5.5 (`medium`) | — | ✅ | ✅ | — | — | ✅ | — | — | ✅ | 2/3 | 2/6 | 4/9 | 2/9 |
| Claude Sonnet 5.5 (`high`) | — | ✅ | ✅ | — | — | ✅ | — | ✅ | — | 1/3 | 3/6 | 4/9 | 1/9 |

The baseline found `D3` in all nine configurations, `D9` in five (Flash at every level, Sonnet `low` and `medium`),
`D2` in one (Flash `low`), and `D1` in one (Argon `medium`).

### Key Observations

1. **Most of the gain over the baseline is on documentation and changelog items.** Summed over the nine
   configurations, the baseline found 16 items and `/craft-review` 30. `D2` went from 1 configuration to 8, `D6` from
   0 to 3, `D8` from 0 to 2, `D9` from 5 to 8. `D3` was found by every baseline run and by 8 `/craft-review` runs;
   Flash `high` reported it in the baseline and not under `/craft-review`. `D1` was found only by Argon `medium`, in
   both arms; most other runs reported a different defect on the same line (first row of the table below). No run
   reported `D4`, `D5`, or `D7`. Counting the six defects `B1–B6` as well, the baseline reported 38 of 135
   (15 issues × 9 configurations) and `/craft-review` 60.
2. **`/craft-review` uses about 6× to 36× the uncached tokens of the baseline.** The orchestrator conversation
   accounts for 5% to 43% of them; the rest is subagents. Flash `high` used 5.9× the uncached and 8.3× the cache-read
   tokens of Argon `high` (707 vs 170 tool calls per PR) and found 4/9 items against 2/9. Every tool call replays
   the conversation prefix from the cache, so cache reads grow with the number of tool calls.
3. **The two controls behave differently.** On `pytest-5680` (typo fixes) every `craftsman` run covered all hunks
   and reported 0 findings. On `psf__requests-764` hunters reproduced two bugs the PR introduced (below). SWR-Bench
   calls a PR clean when it was merged without review comments; that label measures reviewer attention, not the
   correctness of the change.

### Verified Defects Found Beyond SWR-Bench Ground Truth

SWR-Bench takes its ground truth from reviewer comments on GitHub, so a bug nobody commented on is absent from
`D1–D9`. Gemini 4 Argon and Gemini 3.8 Flash hunters found, and verifiers reproduced with executable scripts
(`proven`), the following defects introduced by the PRs or by their effect on existing callers and tests:

| ID | PR | Location | Kind | Defect (`proven` via `craftsman run`) |
| :-: | :--- | :--- | :--- | :--- |
| **B1** | `pallets__flask-2748` | `flask/blueprints.py:72` | PR regression (`contracts` / `failure`) | `url_prefix='/'` with `@bp.route('')` raises `ValueError`. `BlueprintSetupState.__init__` strips the trailing slash, so `url_prefix` is `''`; the new `else: rule = self.url_prefix` branch hands `''` to Werkzeug's `Rule`, which rejects a URL without a leading slash. Before the PR, `'/'.join(('', ''))` gave `'/'`. |
| **B2** | `pytest-dev__pytest-13293` | `src/_pytest/assertion/rewrite.py:377` & `testing/test_assertrewrite.py:1340` | Ripple on existing test (`ripple` / `tests`) | `test_read_pyc` no longer exercises corrupt-marshal handling. It still compiles with `py_compile.compile()`, which writes seconds into the `.pyc` header, then corrupts the payload. `_read_pyc()` now compares the header with `st_mtime_ns`, so it returns `None` as "out of date" before reaching `marshal.loads()`. Both paths return `None`, so the test stays green. |
| **B3** | `pytest-dev__pytest-13293` | `testing/test_assertrewrite.py:1836` | Ripple on existing test (`ripple` / `tests`) | `test_source_mtime_long_long` no longer hits the 32-bit boundary. It sets `timestamp = 2**32 + offset` seconds; after the switch to `st_mtime_ns & 0xFFFFFFFF`, `(timestamp * 10**9) & 0xFFFFFFFF` is `0xc4653600`, nowhere near the rollover. |
| **B4** | `pytest-dev__pytest-13293` | `testing/test_assertrewrite.py:1386` | PR defect (`contracts` / platform) | Timestamp mismatch on filesystems with 100 ns resolution. `os.utime(source, ns=(1605385304, 1605385304))` is stored as `1605385300` on NTFS, while the `.pyc` header holds the raw `1605385304`. |
| **B5** | `psf__requests-764` *(control in SWR-Bench)* | `requests/models.py:295, 633` | PR bug (`state` / `contracts`) | `Request.send(prefetch=False)` is ignored across redirects. The argument is resolved at line 633, after `_build_response(r)` at line 623 has already followed redirects with `self.prefetch`. With `req.prefetch` true, the redirected body is consumed eagerly and `iter_lines()` raises `RuntimeError: The content for this response was already consumed`. |
| **B6** | `psf__requests-764` *(control in SWR-Bench)* | `tests/test_requests.py:989, 997` | PR test bug (`tests` / Python 3) | The two new `iter_lines()` tests fail on Python 3 with `TypeError`. They call `.startswith('{')` with a `str` on the `bytes` that `iter_lines()` yields. |

Two `proven` findings on `pytest-8537` are left out of this table. The crash of `locate_config([ancestor])` on a
parent directory's legacy `setup.cfg` comes from the lookup that `D3` asks to remove. A `rootdir` of `inipath.parent`
outside the test tree is the behaviour the maintainer asked for in `D3`.

Which runs reported each of them, by the rule used for `D1–D9`:

| Model | B1 | B2 | B3 | B4 | B5 | B6 | Total `baseline` | Total `craftsman` |
| :--- | :-: | :-: | :-: | :-: | :-: | :-: | :---: | :---: |
| Gemini 3.8 Flash (`low`) | both | — | — | — | craftsman | — | 1/6 | 2/6 |
| Gemini 3.8 Flash (`medium`) | both | — | — | both | both | both | 4/6 | 4/6 |
| Gemini 3.8 Flash (`high`) | both | — | — | both | both | both | 4/6 | 4/6 |
| Gemini 4 Argon (`low`) | baseline | — | craftsman | craftsman | both | craftsman | 2/6 | 4/6 |
| Gemini 4 Argon (`medium`) | both | both | — | both | both | both | 5/6 | 5/6 |
| Gemini 4 Argon (`high`) | both | craftsman | craftsman | both | both | craftsman | 3/6 | 6/6 |
| Claude Sonnet 5.5 (`low`) | craftsman | — | — | — | craftsman | — | 0/6 | 2/6 |
| Claude Sonnet 5.5 (`medium`) | — | — | — | — | both | — | 1/6 | 1/6 |
| Claude Sonnet 5.5 (`high`) | both | — | — | — | both | — | 2/6 | 2/6 |

Summed over the nine configurations, the baseline reported 22 of these and `/craft-review` 30. The list holds only
defects that a `/craft-review` run reproduced, so a defect that only a baseline run reported is not in it; the count
favours the `craftsman` arm for that reason and is not a recall figure.

Eight of the nine `/craft-review` runs also reported that the new `AUTHORS` entry in `pytest-13293` is appended
instead of inserted alphabetically (`probable` in most ledgers). No reviewer commented on it.

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

## Seeded Suite A/B Results: Gemini 3.8 Flash (Craftsman 5.1.0)

End-to-end A/B evaluation on the 8-case seeded defect suite (`27` defects, `18` decoys) using the Antigravity CLI
(`agy -p --output-format json --dangerously-skip-permissions`) with `gemini-3.8-flash-low`, comparing an unassisted
`baseline` (`evals.run.BASELINE_PROMPT`, no plugin or `craftsman` on `PATH`) against Craftsman `5.1.0` (`/craft-review`
loaded via `.agents/plugins/craftsman` and `bin/craftsman.cmd`). Each arm ran `2` complete passes across all 8 cases
(`32` runs total, interleaved at concurrency `3` with a `30 min` per-run timeout; a third pass was omitted after
passes 1 and 2 took ~59 min and ~53 min wall time respectively, exceeding the 45-minute budget cutoff).

Methodology and Windows notes:
- **Windows hook workaround**: `agy` runs plugin hooks via `cmd.exe /c` on Windows, where `./scripts/craftsman-*.py`
  fails (`'.' is not recognized as an internal or external command`). The temporary plugin copy rewrote the hook
  commands in `.agents/plugins/craftsman/hooks.json` to `python scripts\craftsman-*.py` without altering the repository
  files.
- **Baseline output handling**: `~/.gemini/antigravity-cli/settings.json` includes `"deny": ["write_file(.git/)"]`.
  Baseline agents still produced `.git/evals/findings.json` via shell commands (two PowerShell UTF-8 BOMs in pass 1
  were stripped before `evals.run.read_findings_file`, with stdout JSON extraction as fallback).
- **Subscription billing & timeouts**: `agy` runs are subscription-billed (`$0.00` per-run USD), so cost is measured
  in tokens (`input`, `output`, `cache read`; Antigravity records `0` cache-write tokens) summed across the orchestrator
  and all subagent transcripts. Two pass-1 large Craftsman runs (`feedbox#1` at `5/6` and `orderdesk#1` at `6/6`) hit
  the 30-minute timeout during late verification and are included (`14/16` finished `ok`, `2/16` ended early) with
  their kept ledgers and transcript-recovered telemetry.

### Summary Comparison (Mean per Pass, `n=2` Repeats)

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

### Observations

1. **Craftsman caught every defect the Flash baseline missed (`inv-2`, `od-6`, `tb-3`), lifting pass-2 recall to `27/27` (`100%`).**
   Baseline missed the exact same three defects in both passes (`0/2` each): `inv-2` (`help-contradicts-code`: `--min-qty` help
   wording vs. `>= N` logic), `od-6` (`weak-test`: `assertLessEqual` cannot fail at the threshold boundary), and `tb-3`
   (`weak-test`: substring-only CSV test assertion). Craftsman detected all three `2/2` times. Its sole miss (`fb-6`,
   `incomplete-propagation`, `1/2`) occurred in pass 1 when `feedbox#1` timed out at 30 min while waiting on a subagent;
   pass 2 finished in `27.2 min` at `27/27` (`100%`).
2. **Wave proliferation on Flash drives a `12.1×` time and `33.5×` token trade-off.**
   Per `evals.run breakdown`, `49` of `53` seeded-defect first detections (`92%`) occurred in generation 1 (`42` `bug`,
   `3` `simplicity`, `4` `sweep`), `4` (`8%`) came from generation-2 `fresh` hunters on the two large cases, and **0**
   came from `variant` slots (`4.9` hunters/run) or generation 3 (`3.1` hunters/run). Because `proven` findings trigger
   follow-up waves, each run averaged `12.9` hunters and `13.1` verifiers (`26.0` subagents/run), with subagents
   consuming `84.8%` of Craftsman's tokens (`94.4M` of `111.3M` per pass) vs. `15.2%` (`16.9M`) in the orchestrator.
3. **Precision (`83%` vs. `100%`) reflects non-seeded findings and three decoy hits on larger/edge cases.**
   Verifiers filtered out `5.4` merged duplicates and `1.6` dismissed findings per run (`24.5` recorded → `14.8` `proven`,
   `2.6` `probable`, `0.2` `question`). Of the `29.00` unmatched findings per pass, `21.00` came from `orderdesk` (`12.50`)
   and `feedbox` (`8.50`), and `1.50` decoy hits per pass occurred on `att-d1` (`attach#1`, 1 hit) and `fb-d3`
   (`feedbox#1`/`#2`, 1 hit each). On `gemini-3.8-flash-low`, the full multi-wave protocol trades substantial wall time
   and token volume for near-complete recall on subtle test-weakness and doc-vs-code defects.

---

## Limitations

We present these preliminary results transparently with their methodological limitations:
- **Sample Size**: Small initial dataset ($n=5$ pull requests, 9 ground-truth review comments).
- **Single Repetition**: 1 run per configuration in the initial pilot.
- **Manual Scoring**: Findings were matched to the ground-truth items by hand, by one scorer.
- **Protocol Drift**: The Claude Sonnet 5.5 runs used an earlier build than the Gemini runs, and the wording of
  the session-start hook changed between runs.
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
