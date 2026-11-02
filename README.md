# Craftsman

[![CI](https://github.com/BuczynskiRafal/craftsman/actions/workflows/ci.yml/badge.svg)](https://github.com/BuczynskiRafal/craftsman/actions/workflows/ci.yml)
[![Version](https://img.shields.io/badge/version-4.1.0-emerald.svg)](plugin.json)
[![Python 3.10–3.13](https://img.shields.io/badge/python-3.10--3.13-blue.svg)](pyproject.toml)
[![Dependencies: stdlib only](https://img.shields.io/badge/dependencies-stdlib%20only-brightgreen.svg)](pyproject.toml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Craftsman adds a layer to an AI coding agent that checks the quality and simplicity of the code it
delivers. The agent cannot finish its work until an independent reviewer has examined the change.
Craftsman enforces a review loop in which every reported defect is verified and resolved, and at the
end the tests must pass on the final version of the code.

Craftsman keeps a ledger in the `.git` directory, where it collects evidence from review,
verification, and tests. When the agent tries to finish, a platform hook checks the ledger and, if
anything is missing, sends the agent back with a list of what is missing.

It works with Claude Code, GitHub Copilot, and Google Antigravity. In Codex and Cursor, the same
protocol runs without hooks.

## How It Works

![Craftsman engineering loop](https://raw.githubusercontent.com/BuczynskiRafal/craftsman/main/docs/diagrams/harness-overview.png)

1. `/craftsman <task>` records the base commit that the diff is computed from.
2. The agent implements the change and cleans it up with `/simplify`.
3. Craftsman splits the diff into hunks and attaches to each one the context the diff does not show:
   the rest of the function, its call sites, and risk signals. Defects usually sit where the change
   meets that code.
4. Reviewer subagents that did not write the change examine every hunk through several lenses (for
   example contracts, error handling, tests, security) and record suspected defects.
5. A separate verifier tries to disprove or reproduce each suspected defect. Searching and judging
   are kept apart so that the reviewer does not drop leads too early.
6. The agent fixes confirmed defects. Every piece of evidence in the ledger is tied to a diff
   fingerprint, so a code change invalidates the old review; the next round covers only the changed
   parts.
7. The agent runs the tests through `craftsman run`, which records the exit code.
8. The stop hook lets the agent finish once the ledger is complete for the current version of the
   code. The agent prints a report: what was reviewed, what was found, what was fixed, what was
   dismissed, and what the gate did not assess.

The level of rigor depends on the gear: `quick` for small changes (review in the current context),
`standard` by default (subagents and a verifier), `deep` for risky changes (all lenses, tests, and
e2e).

`/code-review` runs only the review of an existing change, without fixes.

## Platforms

| Platform | What blocks completion |
| :--- | :--- |
| Claude Code | `Stop` hook |
| GitHub Copilot in VS Code | `Stop` hook |
| GitHub Copilot CLI | `agentStop` hook |
| Google Antigravity | `Stop` hook |
| Codex, Cursor, and other tools that read `AGENTS.md` | `craftsman gate --complete` |

Where the platform cannot confirm that a separate subagent performed the review, the report marks it
as `SELF-REVIEWED`.

## Measurements

Pilot on [SWR-Bench](https://doi.org/10.1145/3808144): 5 PRs from `pytest`, `flask`, and `requests`,
9 confirmed defects, 1 repetition per configuration. The sample is too small for statistical
conclusions. Details and Claude Code runs: [docs/benchmark.md](docs/benchmark.md).

| Configuration | Defects: without → with Craftsman | Avg. time per PR |
| :--- | :---: | :---: |
| Antigravity · Gemini 4 Argon `low` | 2/9 → 4/9 | 1.5 → 4.7 min |
| Antigravity · Gemini 4 Argon `medium` | 2/9 → 5/9 | 2.0 → 6.7 min |
| Antigravity · Gemini 4 Argon `high` | 3/9 → 7/9 | 15.6 → 26.3 min |

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

```
/craftsman add pagination to the catalog search
/craftsman --deep migrate the session store to Redis
/code-review
```

`craftsman status` shows the current gear, round, and missing evidence.

## Documentation

- [How it works](docs/how-it-works.md): ledger, lenses, gears, platform enforcement
- [Installation](docs/installation.md): flags, platform setup, uninstall, `craftsman doctor`
- [Usage](docs/usage.md): commands, review-only mode
- [Guards](docs/guards.md): gate state machine, search scope guard
- [Skills](docs/skills.md): catalog of bundled skills
- [Benchmark](docs/benchmark.md): methodology, results, replication
- [Architecture](docs/harness-architecture.md): project internals for contributors

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

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

MIT, see [LICENSE](LICENSE) and attribution in [NOTICE](NOTICE).
