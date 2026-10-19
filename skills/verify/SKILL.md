---
name: verify
description: >-
  End-to-end runtime verification of code changes (/verify). Goes beyond running
  unit tests or linters — actually executes the modified flow (CLI, script, HTTP
  server, data pipeline, headless GUI) and observes real runtime behavior and
  outputs. Use when /verify is invoked or before committing non-trivial
  production changes.
---

# Verify (`/verify` — End-to-End Runtime Verification)

You verify that a code change **actually accomplishes what it was built to do** by exercising it **end-to-end at runtime** and observing real behavior — not merely by running static linters or unit tests in isolation.

> **When NOT to use:** If the diff touches only documentation (`*.md`), comments, or test files with zero production runtime surface, state that briefly and finish. Every production code change has a runtime surface that must be exercised.

Include clickable `file://` Markdown links in your verification report.

---

## Phase 1 — Identify the Runtime Surface

1. Inspect the current diff (`git diff @{upstream}...HEAD` and `git diff HEAD`, or the target passed to `/verify <target>`).
2. Check whether the repository defines verification commands or scripts in `GEMINI.md`, `AGENTS.md`, `CLAUDE.md`, `Makefile`, `pyproject.toml`, or `package.json`.
3. Determine the runtime surface touched by the change:
   - **CLI Tool / Script / Data Pipeline:** which command, flags, and input data exercise the exact modified code paths?
   - **Library / Computational Module:** what minimal smoke-test command (`python3 -c "..."`) invokes the modified API on realistic inputs (including boundary conditions from the diff) and asserts output invariants?
   - **Server / API / Web:** start the server and send a real request (`curl` / test client) hitting the modified endpoint, verifying HTTP status, payload, and logs.
   - **Desktop / CAD / GIS Plugin:** verify clean module import without errors, invoke controller/session logic in headless mode, or run the package verification script.

---

## Phase 2 — Multi-Layer Verification Execution

Execute verification from the fastest static check up to full end-to-end runtime execution:

1. **Layer 1: Static & Import Health**
   - Verify modified modules compile and import cleanly in the project environment (zero `SyntaxError`, `ImportError`, circular imports, or type errors).
2. **Layer 2: Automated Test Suite**
   - Run automated tests covering the modified module (e.g., `pytest <test_path>` or `python3 -m unittest`).
3. **Layer 3: Live End-to-End Runtime Execution (Core of `/verify`!)**
   - **Exercise the touched flow live** via `run_command`:
     - Pass realistic inputs that force execution through **the exact branches (`if/else`, new methods, refactored functions)** modified in the diff.
     - Test at least **1 primary happy path** and **1 boundary/error path** introduced or modified by the change.
     - Inspect actual `stdout`, `stderr`, exit code, and generated artifacts to confirm zero hidden warnings, exceptions, or corrupted outputs.
   - Clean up any temporary files generated during verification.

---

## Phase 3 — Verdict & Report

Conclude with a concise verification report:

```markdown
### Runtime Verification (`/verify`)
- **Target / Scope:** `<description of verified changes>`
- **Status:** **PASS (VERIFIED)** / **FAIL** / **PARTIAL (WARNING)**

#### Verification Runs:
1. **Import & Static Check:** `<command>` → `<result>`
2. **Unit / Integration Tests:** `<command>` → `<result>`
3. **End-to-End Runtime Execution:**
   - **Invocation:** `<exact command / scenario>`
   - **Observed Behavior:** `<concrete output, exit code, and verified invariants>`
```

If `/verify` uncovers a runtime defect:
- Cite the exact cause and line (`[file.ext:L10-L20](file:///...)`),
- If the fix is clear and within scope, apply it and re-run verification until `PASS (VERIFIED)` is achieved.
