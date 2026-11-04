# AGENTS.md — Craftsman Engineering Standard

This repository uses **Craftsman**: every change is reviewed and verified against an evidence ledger before it counts as done.

## The Craftsman loop
- **Build freely, review hard:** no gate activity while building. The gate judges the final diff: fresh-context hunters covered every hunk, every finding has a verdict with evidence, the tests pass on the final code. Users write plain words; infer target, fixing and depth yourself.
- **Depth:** one adaptive depth. Go deep (`craftsman review prepare --deep`, or start with `/craftsman --deep` / `ultracraft`) for security-relevant code, concurrency or clocks, persistent data or migrations, public API or file formats, removed guarantees, a large or cross-cutting diff, or when the user asks for care. Deep suggests every lens and requires an end-to-end run; depth never goes down.
- **Start (`/craftsman` only):** `craftsman gate --init [--deep] --task "<request>"` records the base commit; the review always covers the whole diff since then (standalone `/code-review` without fixing does not start a gate session).
- **Review:** `craftsman review prepare` freezes the diff and prints a risk map. Write an intent brief, then launch fresh-context `craftsman-reviewer` hunters in parallel (`craftsman review view --files <csv>` gives each a slice). They record every candidate with `craftsman review add` and what they examined with `craftsman review cover --hunks <ids> --note "..."` (or `--from-json - <<'EOF'`).
- **Verify and saturate:** `craftsman-verifier` subagents merge duplicates (`craftsman review merge <target> <dup>`, only when one fix removes every consequence), try to disprove each finding, and record `craftsman review verdict`. While a wave yields confirmed defects, run another (variant hunters plus a fresh hunter, at most 3 waves per round).
- **Fix:** fix confirmed defects at the root cause with a regression test, `craftsman review resolve <id> --as fixed --note "..."`, then `craftsman review prepare` for the changed hunks.
- **Run checks through the ledger:** `craftsman run --kind test -- <command>` (and `--kind e2e` when deep); only runs on the final code count.
- **Done means** `craftsman review check` passes; then `craftsman gate --complete` and report with `craftsman review report` (in standalone review-only `/code-review`, finish with `craftsman review check --review-only` and `craftsman review report --review-only`).
- **Skills & lenses:** invoke `/name` as a skill if your platform offers it; otherwise run `craftsman skill show <name>` (and `craftsman skill show code-review --lenses <lenses>` for the playbooks of the lenses you apply). Never search the file system for skill or lens files.

## Search scope
- Work only inside this git work tree (`git rev-parse --show-toplevel`): prefer `git ls-files`, `git grep`, and editor search scoped to this folder.
- Outside the work tree you may read only an exact file cited by a stack trace/error, a temp directory you created, CI logs for this branch/PR, or Craftsman's own data via `craftsman …` CLI commands (never open `.git/craftsman` files by hand except the exact packet path or an inbox file path that `craftsman review prepare`, `craftsman review show` or `craftsman review inbox` prints for this repository).
- Batch JSON files go only to `craftsman review inbox` paths; never to `%TEMP%` or into the repository; a file you had to create elsewhere must be deleted before you report.
- Never run recursive searches of `~`, `/`, or parent directories, and never open other repositories, other projects' packets/ledgers/reports, or assistant session stores.

## Available Skills
