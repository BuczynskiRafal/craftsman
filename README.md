<div align="center">

![Craftsman](https://raw.githubusercontent.com/BuczynskiRafal/craftsman/main/assets/logo.svg)

# Craftsman

[![CI](https://github.com/BuczynskiRafal/craftsman/actions/workflows/ci.yml/badge.svg)](https://github.com/BuczynskiRafal/craftsman/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/craftsman-gate)](https://pypi.org/project/craftsman-gate/)
[![Version](https://img.shields.io/badge/version-5.2.0-emerald.svg)](https://github.com/BuczynskiRafal/craftsman/blob/main/plugin.json)
[![Agent Skills](https://img.shields.io/badge/Agent_Skills-skills.sh-0F766E.svg)](https://github.com/BuczynskiRafal/craftsman/blob/main/docs/skills.md)
[![Python 3.10–3.13](https://img.shields.io/badge/python-3.10--3.13-blue.svg)](https://github.com/BuczynskiRafal/craftsman/blob/main/pyproject.toml)
[![Dependencies: stdlib only](https://img.shields.io/badge/dependencies-stdlib%20only-brightgreen.svg)](https://github.com/BuczynskiRafal/craftsman/blob/main/pyproject.toml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://github.com/BuczynskiRafal/craftsman/blob/main/LICENSE)

</div>

Craftsman locks an AI coding agent in a quality loop: **build freely, review hard**. The agent codes normally, and only when it thinks it is done does Craftsman freeze the diff, send fresh-context bug hunters and disproof-oriented verifiers over the change, record verdicts and real test runs under `.git/`, and block completion in platform hooks until confirmed bugs are fixed and tests pass.

It works with Claude Code, GitHub Copilot, and Google Antigravity via Stop hooks, and runs without hooks in Codex and Cursor.

## How It Works

![Craftsman 5: build freely, review hard](https://raw.githubusercontent.com/BuczynskiRafal/craftsman/main/docs/diagrams/harness-overview.png)

1. **Freeze and plan.** The diff is frozen and split by size and risk signals into mandatory review slices and lenses.
2. **Bug hunt.** Each slice goes to a separate fresh-context agent that records suspected bugs with concrete triggers.
3. **Verification.** Separate verifiers try to disprove every suspicion and reproduce survivors; confirmed bugs trigger follow-up waves.
4. **Fixes and tests.** Root-cause fixes are re-checked by fresh hunters, and tests pass on the final code through `craftsman run`.

`/craft-review` runs the same review on an existing change and fixes only when asked. Full protocol: [docs/how-it-works.md](https://github.com/BuczynskiRafal/craftsman/blob/main/docs/how-it-works.md) · Interactive walkthrough: [catalog pagination demo](https://github.com/BuczynskiRafal/craftsman/blob/main/examples/demo/README.md).

## Where It Fits Best

Craftsman works best for:

- non-trivial changes and tricky bug fixes,
- legacy refactors and migrations,
- changes touching security, concurrency, or public contracts,
- code where failing silently is expensive.

It is overkill for prototypes and small greenfield tasks — for lightweight daily checks, you can install individual skills standalone via `skills.sh` below.

## Platforms

| Platform | What blocks completion |
| :--- | :--- |
| Claude Code | `Stop` hook |
| GitHub Copilot in VS Code | `Stop` hook |
| GitHub Copilot CLI | `agentStop` hook |
| Google Antigravity | `Stop` hook |
| Codex, Cursor, and `AGENTS.md` tools | `craftsman gate --complete` |

Where a host does not expose subagent hooks (Codex, Cursor, or unhooked sessions), the review stays advisory once every hunk is covered and marks the report `SELF-REVIEWED`.

## Measurements

Tested on `gemini-3.8-flash-low` via Antigravity CLI (2 full passes each):

| What we tested | Without Craftsman | With Craftsman 5.2 |
| :--- | :--- | :--- |
| **Finding bugs in existing PRs** ([`evals/`](https://github.com/BuczynskiRafal/craftsman/blob/main/evals/README.md))<br>8 repos, 27 planted bugs, green unit tests | **24 / 27** bugs caught (`89%`)<br>`12 min` · `3.3M` tokens | **26.5 / 27** bugs caught (**`98%`**)<br>`143 min` · `86.6M` tokens |
| **Building features from scratch** ([`evals/build_cases/`](https://github.com/BuczynskiRafal/craftsman/blob/main/evals/README.md#build-and-verify-benchmark))<br>6 multi-file specs graded by hidden tests | **8 / 12** runs passed 100% (`67%`)<br>`3 min` · `404k` tokens | **9 / 12** runs passed 100% (**`75%`**)<br>`34 min` · `21.7M` tokens\* |

*\*In the build-from-scratch test, `7 / 12` Craftsman runs hit the 40-minute timeout while re-checking fixes (`5` after already passing `100%` of hidden tests). Full breakdown and limits: [docs/benchmark.md](https://github.com/BuczynskiRafal/craftsman/blob/main/docs/benchmark.md).*

## Installation

Requires Python 3.10+ with zero external dependencies (`stdlib` only):

```bash
pipx install craftsman-gate        # or: uv tool install craftsman-gate
craftsman install                  # detects Claude Code, Copilot, and Antigravity
```

Or install as a platform plugin:

```bash
/plugin marketplace add BuczynskiRafal/craftsman              # Claude Code
/plugin install craftsman@craftsman
agy plugin install https://github.com/BuczynskiRafal/craftsman   # Antigravity
codex plugin marketplace add BuczynskiRafal/craftsman            # Codex
codex plugin add craftsman@craftsman
```

Cursor installs from a local checkout — see [Cursor plugin setup](https://github.com/BuczynskiRafal/craftsman/blob/main/docs/installation.md#cursor-plugin).

Or install individual Agent Skills standalone without Python (`11` run standalone; `/craft-review` runs ungated):

```bash
npx skills add BuczynskiRafal/craftsman
```

## Usage

Describe the task in plain words — no flags to remember:

```text
/craftsman add pagination to the catalog search
/craftsman migrate the session store to Redis, this is critical
/craft-review review my staged changes and fix what you find
/craft-review review PR 42
/craft-review be thorough, this touches authentication
```

The agent infers scope, whether to fix findings, and review depth (scaling up on risky signals or words like `critical` and `ultracraft`; see [docs/usage.md](https://github.com/BuczynskiRafal/craftsman/blob/main/docs/usage.md) and **Depth** in [`craft-review`](https://github.com/BuczynskiRafal/craftsman/blob/main/skills/craft-review/SKILL.md)). Run `craftsman status` at any time to check coverage and missing evidence.

## Documentation

- [Usage](https://github.com/BuczynskiRafal/craftsman/blob/main/docs/usage.md) · [How it works](https://github.com/BuczynskiRafal/craftsman/blob/main/docs/how-it-works.md) · [Installation](https://github.com/BuczynskiRafal/craftsman/blob/main/docs/installation.md) · [Guards](https://github.com/BuczynskiRafal/craftsman/blob/main/docs/guards.md) · [Skills](https://github.com/BuczynskiRafal/craftsman/blob/main/docs/skills.md) · [Benchmark](https://github.com/BuczynskiRafal/craftsman/blob/main/docs/benchmark.md) · [Demo](https://github.com/BuczynskiRafal/craftsman/blob/main/examples/demo/README.md) · [Architecture](https://github.com/BuczynskiRafal/craftsman/blob/main/docs/harness-architecture.md)

## Contributing & License

Contributions: [CONTRIBUTING.md](https://github.com/BuczynskiRafal/craftsman/blob/main/CONTRIBUTING.md) · Citation: [CITATION.cff](https://github.com/BuczynskiRafal/craftsman/blob/main/CITATION.cff) · License: MIT ([LICENSE](https://github.com/BuczynskiRafal/craftsman/blob/main/LICENSE), [NOTICE](https://github.com/BuczynskiRafal/craftsman/blob/main/NOTICE)).
