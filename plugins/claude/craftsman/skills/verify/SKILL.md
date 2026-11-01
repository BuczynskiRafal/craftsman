---
name: verify
description: >-
  Runtime verification of a change: runs the project's checks and then exercises
  the changed behavior for real (CLI, script, server request, pipeline run,
  headless UI), recording every execution in the Craftsman ledger so the gate
  can see exit codes at the final state of the code. Use when /verify is
  invoked or as the verification step of /craftsman.
---

# Verify (`/verify [target]`)

Tests tell you the code agrees with the tests. Verification tells you the change does what it was
built to do when it actually runs. You need both, and both are recorded: every command goes
through `craftsman run`, which stores the exit code, duration and output tail against the exact
diff it ran on.

```bash
craftsman run --kind <test|e2e|lint|build|repro|other> -- <command and arguments>
```

A run only counts for the gate when it happened **after the last code change**, and only if the
command itself left the files unchanged (a formatter or code generator run does not verify
anything — run the checks again after it). If you edit code afterwards, run again. A failing
command that can no longer pass (a typo, a renamed test) is retired with
`craftsman review waive-run <run id> --reason "<why>"`; the user sees every waiver in the report.

## Search scope

Your workspace is the git work tree you were started in (`git rev-parse --show-toplevel`): search,
list, and read only inside it (prefer `git ls-files`, `git grep`, and editor search scoped to this
folder). Outside the work tree you may read only an exact file cited by a stack trace/error, a temp
directory you created, CI logs for this branch/PR, or Craftsman's own data via `craftsman …` CLI
commands. Never run recursive searches of `~`, `/`, or parent directories, and never open other
repositories, other projects' packets/ledgers/reports, or assistant session stores (run
`craftsman skill show craftsman --file scope.md` for the full contract).

## 1. Find the runtime surface

From the diff (and the `/craftsman` task statement), answer:
- What can a user or a caller now do differently? Which entry point reaches the changed code —
  a CLI command, a script, an HTTP route, a job, a library function, a UI action?
- Which inputs drive execution through the **changed branches** specifically?
- How does the project run its checks? Look in `Makefile`, `package.json` scripts,
  `pyproject.toml`/`tox.ini`/`noxfile.py`, CI workflows, and convention files
  (`AGENTS.md`, `CLAUDE.md`, `GEMINI.md`, `.github/copilot-instructions.md`).

Documentation-only changes have no runtime surface: say so and run only the project's doc or
link checks if it has any.

## 2. Run the checks, fastest first

1. **Static:** the project's linters and type checkers (`--kind lint`).
2. **Build / import:** the package builds and the touched modules import (`--kind build`).
3. **Tests:** the tests for the touched modules first, then the full suite when it is affordable
   (`--kind test`). A failing test is information: read it before touching code.

## 3. Exercise the change for real (`--kind e2e`)

Run the actual entry point with realistic input:
- at least one input that takes the main changed path, and
- at least one input that takes a changed boundary or error path.

Check what really happened — output, exit code, files written, HTTP status and body, log lines —
against what the change is supposed to do. Prefer the project's real interfaces over calling
internals; for libraries, a short script that imports the public API is the entry point.

Examples:
```bash
craftsman run --kind e2e -- python -m mytool convert tests/data/sample.csv --out <temp-dir>/out.json
craftsman run --kind e2e -- python scripts/smoke_api.py
craftsman run --kind e2e -- node scripts/smoke.mjs
```
For a server, prefer one self-contained command that starts it, sends the requests, checks the
answers and stops it (a short script, or the framework's test client) — a server left running
in the background between tool calls is often killed or hangs the terminal tool. Put temporary
files in a temp directory and remove them when done. Write each command on one line, and use
`python` or the project's virtualenv interpreter (`python3` is missing on many Windows machines).

## 4. When something fails

Find the cause in the code (with `file:line`), fix it if it is within the scope of the change,
and run the same commands again. Never weaken a test or an assertion to make a run pass.

## 5. Report

```markdown
### Verification
- Status: PASS | FAIL | PARTIAL (what could not be exercised and why)
- Checks: `<command>` -> <result> (run id)
- End-to-end: `<command>` -> <what was observed and why it is correct> (run id)
```
