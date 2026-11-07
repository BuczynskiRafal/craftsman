# Craftsman

[![CI](https://github.com/BuczynskiRafal/craftsman/actions/workflows/ci.yml/badge.svg)](https://github.com/BuczynskiRafal/craftsman/actions/workflows/ci.yml)
[![Version](https://img.shields.io/badge/version-5.0.0-emerald.svg)](https://github.com/BuczynskiRafal/craftsman/blob/main/plugin.json)
[![Python 3.10–3.13](https://img.shields.io/badge/python-3.10--3.13-blue.svg)](https://github.com/BuczynskiRafal/craftsman/blob/main/pyproject.toml)
[![Dependencies: stdlib only](https://img.shields.io/badge/dependencies-stdlib%20only-brightgreen.svg)](https://github.com/BuczynskiRafal/craftsman/blob/main/pyproject.toml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://github.com/BuczynskiRafal/craftsman/blob/main/LICENSE)

Craftsman makes an AI coding agent prove its change was reviewed and tested. The idea is **build
freely, review hard**: while building, the agent works without review loops or ceremony. When it
finishes, the final diff is frozen and attacked by fresh-context hunter subagents that did not write
the code, independent verifiers try to disprove every finding, and the hunt continues while it keeps
turning up real defects. The agent cannot finish until the tests pass on the final code.

Craftsman keeps a ledger in the `.git` directory with the evidence: which hunks the hunters covered,
what each finding was judged to be and why, and the test runs. A platform hook checks the ledger when
the agent tries to stop and sends it back with a list of what is missing. The gate checks outcomes,
not effort.

It works with Claude Code, GitHub Copilot, and Google Antigravity. In Codex and Cursor, the same
protocol runs without hooks.

## Installation

Requires Python 3.10+, with no external dependencies.

```bash
pipx install craftsman-gate        # or: uv tool install craftsman-gate
craftsman install                  # detects Claude Code, Copilot, and Antigravity
```

Or as a plugin:

```bash
/plugin marketplace add BuczynskiRafal/craftsman              # Claude Code
/plugin install craftsman@craftsman
agy plugin install https://github.com/BuczynskiRafal/craftsman   # Antigravity
```

## Usage

Type a request in plain words. There are no flags to remember.

```
/craftsman add pagination to the catalog search
/craftsman migrate the session store to Redis, this is critical
/craft-review review my staged changes and fix what you find
/craft-review review PR 42
/craft-review be thorough, this touches authentication
```

The agent works out the target (working tree, staged changes, a branch, a PR), whether to fix what it
finds, and how deep to go. It goes deep on its own for risky changes or when you ask for care
("critical", "be thorough", `ultracraft`); the list of triggers is under **Depth** in the
[`craft-review` skill](https://github.com/BuczynskiRafal/craftsman/blob/main/skills/craft-review/SKILL.md).
Depth never goes down during a session. The final report explains what the agent decided and why. See
[docs/usage.md](https://github.com/BuczynskiRafal/craftsman/blob/main/docs/usage.md).

`craftsman status` shows the current round, coverage, and missing evidence.

## How It Works

![Craftsman 5: build freely, review hard](https://raw.githubusercontent.com/BuczynskiRafal/craftsman/main/docs/diagrams/harness-overview.png)

```text
/craftsman <task>  ->  build freely  ->  review prepare (freeze the diff, risk map)
  ->  hunters (fresh context, parallel)  ->  verifiers try to disprove every finding
  ->  another wave while it finds real defects  ->  fix, confirm, re-check the fix delta
  ->  craftsman run (tests on the final code)  ->  stop hook lets the agent finish  ->  report
```

The agent builds with no gate activity. Then the diff is frozen into a round, fresh-context hunters
record every candidate defect with a concrete trigger, and separate verifiers try to disprove each one
and reproduce the survivors. Hunters maximize recall; verifiers maximize precision. While a wave
proves a defect, another one runs. Confirmed defects are fixed at the root cause, the fix delta is
re-checked by a fresh hunter, and the tests run through `craftsman run` on the final code.
`/craft-review` runs the same hunt on an existing change, and fixes only when you ask.

The full protocol, with diagrams, is in
[docs/how-it-works.md](https://github.com/BuczynskiRafal/craftsman/blob/main/docs/how-it-works.md).

## Platforms

| Platform | What blocks completion |
| :--- | :--- |
| Claude Code | `Stop` hook |
| GitHub Copilot in VS Code | `Stop` hook |
| GitHub Copilot CLI | `agentStop` hook |
| Google Antigravity | `Stop` hook |
| Codex, Cursor, and other tools that read `AGENTS.md` | `craftsman gate --complete` |

Where the platform cannot confirm that a separate subagent performed the review, the gate still
closes and the report marks it as `SELF-REVIEWED`.

## Measurements

Pilot on [SWR-Bench](https://doi.org/10.1145/3808144): 5 PRs from `pytest`, `flask`, and `requests`,
9 confirmed defects, 1 repetition per configuration. The sample is too small for statistical
conclusions. Details and Claude Code runs: [docs/benchmark.md](https://github.com/BuczynskiRafal/craftsman/blob/main/docs/benchmark.md).
These numbers were measured on an earlier protocol. The repository also ships a public seeded-defect
suite, [`evals/`](https://github.com/BuczynskiRafal/craftsman/blob/main/evals/README.md), to measure
recall on any agent platform.

| Configuration | Defects: without → with Craftsman | Avg. time per PR |
| :--- | :---: | :---: |
| Antigravity · Gemini 4 Argon `low` | 2/9 → 4/9 | 1.5 → 4.7 min |
| Antigravity · Gemini 4 Argon `medium` | 2/9 → 5/9 | 2.0 → 6.7 min |
| Antigravity · Gemini 4 Argon `high` | 3/9 → 7/9 | 15.6 → 26.3 min |

## Documentation

- [Usage](https://github.com/BuczynskiRafal/craftsman/blob/main/docs/usage.md): example requests, what the agent decides, optional flags
- [How it works](https://github.com/BuczynskiRafal/craftsman/blob/main/docs/how-it-works.md): the protocol in full, what hunters read, ledger, gate, lenses, platform enforcement
- [Installation](https://github.com/BuczynskiRafal/craftsman/blob/main/docs/installation.md): flags, platform setup, uninstall, `craftsman doctor`
- [Guards](https://github.com/BuczynskiRafal/craftsman/blob/main/docs/guards.md): gate state machine, search scope guard
- [Skills](https://github.com/BuczynskiRafal/craftsman/blob/main/docs/skills.md): catalog of bundled skills
- [Benchmark](https://github.com/BuczynskiRafal/craftsman/blob/main/docs/benchmark.md): methodology, results, replication
- [Architecture](https://github.com/BuczynskiRafal/craftsman/blob/main/docs/harness-architecture.md): project internals for contributors

## Contributing

See [CONTRIBUTING.md](https://github.com/BuczynskiRafal/craftsman/blob/main/CONTRIBUTING.md).

## Citation

```bibtex
@article{zeng2026swrbench,
  author    = {Zeng, Zhengran and Shi, Ruikai and Han, Keke and Li, Yixin and Sun, Kaicheng and Wang, Yidong and Yu, Zhuohao and Xie, Rui and Ye, Wei and Zhang, Shikun},
  title     = {{SWR-Bench}: Assessing {LLM} Performance in Real-World Code Review Comment Generation},
  journal   = {Proceedings of the ACM on Software Engineering},
  volume    = {3},
  number    = {FSE},
  articleno = {FSE137},
  year      = {2026},
  doi       = {10.1145/3808144}
}
```

## License

MIT, see [LICENSE](https://github.com/BuczynskiRafal/craftsman/blob/main/LICENSE) and attribution in [NOTICE](https://github.com/BuczynskiRafal/craftsman/blob/main/NOTICE).
