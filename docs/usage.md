# Usage Guide

Craftsman provides both conversational chat commands for AI coding assistants and CLI commands for direct inspection and orchestration.

---

## Chat Commands

In Copilot Chat, Claude Code, or Google Antigravity:

```bash
/craftsman add pagination to the catalog search          # full engineering loop (implement, review, test)
/craftsman --deep migrate session store to Redis         # deep gear (all 12 lenses, tests + e2e)
/code-review                                             # review-only mode (quick tier up to 250 changed lines)
/code-review --staged                                    # review only currently staged files
/code-review --paths src/api/                            # review only changed files under src/api/
/code-review --fix                                       # review, fix confirmed bugs, re-review
```

---

## CLI Inspection

Inspect the active session and review ledger directly:

```bash
craftsman status                                         # gear, round, missing evidence, active gate
craftsman review show --hunks                            # compact H# -> file:lines map for verifiers
craftsman review view --lenses intent,contracts          # bounded reviewer packet view
craftsman review view --shard 1/2                        # file-disjoint shard view for parallel reviewers
craftsman review report                                  # final markdown report
craftsman skill show code-review --lenses trust,state    # view specific lens playbooks
```

---

## Review-Only Mode & Frozen Scope

In standalone review-only mode (`/code-review` without an active `/craftsman` gate session):
1. **Scope freezing**: The first round (`review prepare`, optionally with `--staged`, `--tracked`, or `--paths <csv>`) freezes the list of changed files in `Round.scope` (including both old and new paths of renamed files).
2. **Ignoring out-of-scope files**: Files created or modified outside the frozen scope (e.g. by concurrent processes or temporary scripts) are reported as `Ignored (outside the review scope): ...` and do not invalidate the active round.
3. **Scope expansion**: In `--fix` mode, if addressing a bug requires modifying or adding files (such as a new regression test), `craftsman review prepare --add-paths <csv>` extends the frozen scope monotonically.
4. **Adding lenses**: Reviewers can dynamically escalate review rigor by adding custom lenses:
   ```bash
   craftsman review prepare --add-lens "trust=deploy.sh:14 pipes installer into sh"
   ```
   An added lens remains required across all subsequent rounds and cannot be removed.
5. **Partial reports**: If a reviewer subagent encounters timeouts or rate limits after a sequential retry, the review can complete partially with:
   ```bash
   craftsman review report --review-only --partial
   ```
   This generates a `PARTIAL (review-only, N open items)` report with unreviewed areas clearly itemized under `## Not covered`.

---

## Archive Management

Review packets, views, and ledger snapshots are maintained in `.git/craftsman/`:
- **Pruning rule**: Archives are pruned when a new one is created. Superseded packet views and older packets are cleaned up during `craftsman review prepare`.
- **Retention limit**: Archival snapshots (`archive-*` directories and `ledger-*.json`) retain the newest entries defined by `CRAFTSMAN_KEEP_ARCHIVES` (default `5`).
- **Review reset**: Run `craftsman review reset` to cleanly archive the current ledger and packets before starting an independent review session.

---

## Initial Commit Review

Craftsman supports reviewing repositories before the first Git commit. When no prior commits exist, the empty tree hash is automatically used as the base commit, enabling full review and verification of initial project bootstraps.
