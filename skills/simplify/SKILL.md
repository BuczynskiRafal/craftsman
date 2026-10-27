---
name: simplify
description: >-
  Behavior-preserving cleanup of the current diff: removes duplication with
  existing helpers, needless complexity, dead weight, dishonest names or
  comments, and wasted work, then proves behavior is unchanged by re-running
  the same checks. Not a bug hunt (use /code-review). Use when /simplify is
  invoked or as the first step of the /craftsman quality loop.
---

# Simplify (`/simplify [path | ref]`)

Make the changed code as simple as the problem allows **without changing what it does**. You
edit files directly. Scope is the cumulative diff (working tree vs. the `/craftsman` base, or
`HEAD` when no session is active), or the path/ref the user gave.

## Search scope

Your workspace is the git work tree you were started in (`git rev-parse --show-toplevel`): search,
list, and read only inside it (prefer `git ls-files`, `git grep`, and editor search scoped to this
folder). Outside the work tree you may read only an exact file cited by a stack trace/error, a temp
directory you created, or Craftsman's own data via `craftsman …` CLI commands. Never run recursive
searches of `~`, `/`, or parent directories, and never open other repositories, other projects'
packets/ledgers/reports, or assistant session stores (run `craftsman skill show craftsman --file
scope.md` for the full contract).

## What "behavior-preserving" means here

After your edits, for every input: the same return values, the same output (stdout, files,
network), the same errors raised in the same situations, the same side effects in the same
order, and the same public names and signatures. If you cannot convince yourself of all of these
for an edit, do not make it — mention it as a suggestion instead.

## Steps

1. **Pin current behavior.** Find how the project runs its checks (`Makefile`, `package.json`,
   `pyproject.toml`, `AGENTS.md`/`CLAUDE.md`/`GEMINI.md`, CI config) and run the relevant tests
   through the ledger before touching anything:
   ```bash
   craftsman run --kind test -- <test command>
   ```
   Simplifying on top of a red baseline hides regressions. Inside `/craftsman`, a failure caused
   by the change you just built is yours: fix it first, then simplify. A failure that already
   exists on the base commit, or any failure when `/simplify` runs on its own, is reported to the
   user instead (`craftsman gate --await-user` while you wait for their decision).
2. **Read the diff and its surroundings.** For each changed function, read the whole function and
   the module it lives in; skim neighboring modules for helpers.
3. **Look for cost, using the `craft` lens probes** (run `craftsman skill show code-review --file lenses.md` to read the playbook; never search the file system for `lenses.md`):
   - reimplemented helpers, constants or patterns that already exist (search by behavior and by
     likely names, not only the exact name);
   - derivable state, single-use indirection, always-constant flags, deep nesting that guard
     clauses flatten, copy-paste blocks that differ in one value;
   - unused variables, imports, parameters, unreachable branches;
   - names that no longer say what the thing is; comments or docstrings that describe the old
     behavior; comments that only restate the code;
   - repeated I/O or computation in loops, eager work that is rarely needed.
4. **Edit, one concern at a time.** Prefer the smallest edit that removes the cost. Keep the
   surrounding style. Readability beats brevity: do not fold clear branches into dense
   expressions, nested conditionals-in-expressions, or clever one-liners. Do not merge unrelated
   responsibilities, and do not remove an abstraction that the rest of the code relies on for
   organization.
5. **Prove nothing changed.** Re-run exactly the same command through the ledger; it must pass.
   If it fails, undo the offending edit rather than "fixing" the test.
6. **Summarize** in a short list: each edit with `file:line`, the cost it removed, and the
   suggestions you deliberately did not apply (with the reason).

Do not hunt for correctness bugs here; if you notice one, note it for the review step instead
of silently changing behavior.
