# Craftsman

[![CI](https://github.com/BuczynskiRafal/craftsman/actions/workflows/ci.yml/badge.svg)](https://github.com/BuczynskiRafal/craftsman/actions/workflows/ci.yml)
[![Version](https://img.shields.io/badge/version-5.1.0-emerald.svg)](https://github.com/BuczynskiRafal/craftsman/blob/main/plugin.json)
[![Python 3.10–3.13](https://img.shields.io/badge/python-3.10--3.13-blue.svg)](https://github.com/BuczynskiRafal/craftsman/blob/main/pyproject.toml)
[![Dependencies: stdlib only](https://img.shields.io/badge/dependencies-stdlib%20only-brightgreen.svg)](https://github.com/BuczynskiRafal/craftsman/blob/main/pyproject.toml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://github.com/BuczynskiRafal/craftsman/blob/main/LICENSE)

Craftsman is a loop that an AI coding agent is locked in. Normally an agent judges its own work and
declares itself done. Inside Craftsman it cannot finish a task until it proves that its change was
reviewed and tested.

Craftsman does not affect how the agent codes; the principle is **build freely, review hard**. The
review starts only once the agent decides the task is done. Its change (the diff) is frozen, and the
code is checked by agents that did not write it and are seeing it for the first time. Some hunt for
bugs; others try to disprove every report, so that only the real ones remain. The hunt continues while
it keeps turning up real bugs. The agent fixes them and finishes only when the tests pass on the final
code.

The evidence goes into a ledger in the `.git` directory: who checked which part of the change, what
each report was judged to be and why, and how the tests ran. When the agent tries to stop, a platform
hook reads the ledger and sends it back with a list of what is missing. Outcomes count, not the agent's
claims. A failure of the hook itself is logged, never silent.

It works with Claude Code, GitHub Copilot, and Google Antigravity. In Codex and Cursor, the same
protocol runs without hooks.

## How It Works

![Craftsman 5: build freely, review hard](https://raw.githubusercontent.com/BuczynskiRafal/craftsman/main/docs/diagrams/harness-overview.png)

The agent builds its own way; Craftsman reviews nothing in the meantime. Once the agent considers the task
done, the review runs in four steps:

1. **Freeze and plan.** The diff is frozen and the CLI splits it into portions to check (slots). Each
   portion is a part of the change plus the perspectives to examine it from (lenses). The number of
   portions is set by the size of the diff and its risk signals, not by how hard the model feels like
   trying. The agent cannot skip any.
2. **Bug hunt.** Each portion goes to a separate fresh-context agent. It records every suspected bug
   together with the concrete situation that triggers it.
3. **Verification.** Separate agents try to disprove every suspicion and reproduce the ones that
   survive. Hunters must miss nothing; verifiers must filter out false alarms. While a wave of the hunt
   confirms at least one bug, another one runs.
4. **Fixes and tests.** The agent fixes confirmed bugs at the root cause, fresh-context hunters re-check
   the fixes, and the tests run through `craftsman run` on the final code.

`/craft-review` runs the same review on an existing change, and fixes only when you ask.

The full protocol, with diagrams, is in
[docs/how-it-works.md](https://github.com/BuczynskiRafal/craftsman/blob/main/docs/how-it-works.md).

## Where It Fits Best

Craftsman works best for:

- coding hard solutions,
- working with legacy code,
- moving an old codebase to a new solution,
- scientific problems, where certainty about the result matters.

It works well with cheap models, and even more so with self-hosted ones.

It is not needed for prototypes and small fixes.

## Platforms

| Platform | What blocks completion |
| :--- | :--- |
| Claude Code | `Stop` hook |
| GitHub Copilot in VS Code | `Stop` hook |
| GitHub Copilot CLI | `agentStop` hook |
| Google Antigravity | `Stop` hook |
| Codex, Cursor, and other tools that read `AGENTS.md` | `craftsman gate --complete` |

Where the platform cannot confirm that a separate subagent performed the review (Codex, Cursor, or a
session without hooks), the review is advisory: it is complete when every hunk is covered, the gate still
closes, and the report marks it as `SELF-REVIEWED`.

## Measurements

Pilot on [SWR-Bench](https://doi.org/10.1145/3808144): 5 real PRs from `pytest`, `flask`, and `requests`,
each reviewed with and without Craftsman in nine model configurations.

- Human review comments found: 16 without Craftsman, **30 with** (of 81).
- Bugs no human reviewer raised, reproduced with a script: 22 without Craftsman, **30 with** (of 54).

Full tables and limits:
[docs/benchmark.md](https://github.com/BuczynskiRafal/craftsman/blob/main/docs/benchmark.md).

The seeded-defect suite in
[`evals/`](https://github.com/BuczynskiRafal/craftsman/blob/main/evals/README.md) measures recall and
cost on any agent platform.

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
codex plugin marketplace add BuczynskiRafal/craftsman            # Codex
codex plugin add craftsman@craftsman
```

In Codex and Cursor the plugin brings the skills (and, in Cursor, an always-on rule) but no hooks, so
the review stays advisory there. Cursor installs it as a local plugin; see
[docs/installation.md](https://github.com/BuczynskiRafal/craftsman/blob/main/docs/installation.md#cursor-plugin).

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

## Documentation

- [Usage](https://github.com/BuczynskiRafal/craftsman/blob/main/docs/usage.md): example requests, what the agent decides, optional flags
- [How it works](https://github.com/BuczynskiRafal/craftsman/blob/main/docs/how-it-works.md): the protocol in full, the hunt plan, what hunters read, ledger, gate, lenses, platform enforcement
- [Installation](https://github.com/BuczynskiRafal/craftsman/blob/main/docs/installation.md): flags, platform setup, uninstall, `craftsman doctor`
- [Guards](https://github.com/BuczynskiRafal/craftsman/blob/main/docs/guards.md): gate state machine, review guard, command policy, search scope guard, hook error log
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
