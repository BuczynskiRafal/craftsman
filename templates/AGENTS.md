# AGENTS.md — Craftsman Adaptive Closed-Loop Engineering Standard

This repository uses **Craftsman**: every change is reviewed and verified against an evidence ledger before it counts as done.

## The Craftsman loop
- **Gears:** `quick` (≤ 40 changed lines in ≤ 2 files; lenses applied inline), `standard` (default; every always-on lens in fresh-context reviewers, a final sweep and a verifier), `deep` (`/craftsman --deep` or `ultracraft`; every lens plus an end-to-end run).
- **Start (`/craftsman` only):** `craftsman gate --init --tier <auto|quick|standard|deep> --task "<request>"` records the base commit; the review always covers the whole diff since then (standalone `/code-review` without `--fix` does not start a gate session).
- **Review:** `craftsman review prepare` writes a packet of hunks and lenses; record what each lens examined with `craftsman review cover` (or `--from-json - <<'EOF'`) and every defect with `craftsman review add` (or `--from-json - <<'EOF'`).
  Reviewer groups:
{{reviewer_groups}}
- **Verify findings:** group unjudged same-root-cause duplicates first with `craftsman review merge <target> <dup>` (only when one fix removes every consequence), then record verdicts with `craftsman review verdict` (or `--from-json - <<'EOF'`), fix and `craftsman review resolve <id> --as fixed --note "..."`; prepare a new round for the changed hunks.
- **Run checks through the ledger:** `craftsman run --kind test -- <command>` (and `--kind e2e` on deep); only runs at the final code count.
- **Done means** `craftsman review check` passes; then `craftsman gate --complete` and report with `craftsman review report` (in standalone review-only `/code-review` without `--fix`, finish with `craftsman review check --review-only` and `craftsman review report --review-only`).
- **Skills & lenses:** invoke `/name` as a skill if your platform offers it; otherwise run `craftsman skill show <name>` (and `craftsman skill show code-review --file lenses.md` for the review lens playbooks). Never search the file system for skill or lens files.

## Search scope
- Work only inside this git work tree (`git rev-parse --show-toplevel`): prefer `git ls-files`, `git grep`, and editor search scoped to this folder.
- Outside the work tree you may read only an exact file cited by a stack trace/error, a temp directory you created, CI logs for this branch/PR, or Craftsman's own data via `craftsman …` CLI commands.
- Never run recursive searches of `~`, `/`, or parent directories, and never open other repositories, other projects' packets/ledgers/reports, or assistant session stores.

## Available Skills
