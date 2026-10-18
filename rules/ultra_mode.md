# Deep Engineering Protocols (`ultrathink` & `ultracode`)

Whenever the user includes the keyword **`ultrathink`** or **`ultracode`** anywhere in their message, immediately activate the corresponding engineering protocol for the current task:

## 1. Protocol `ultrathink` (Deep Architecture & Algorithmic Reasoning)
The keyword `ultrathink` demands maximum analytical rigor before making a design decision or writing code:
1. **No Shortcuts:** Do not jump straight into generating surface-level code.
2. **Explicit Problem Decomposition:** Before writing implementation code, concisely lay out:
   - **Assumptions & Invariants:** what must hold true before and after the operation,
   - **Edge Cases & Failure Modes:** empty inputs, boundary values (`0`, `None`, `NaN`), concurrency, I/O failures, invalid states,
   - **Time & Space Complexity:** computational and memory cost on critical hot paths,
   - **Architectural Trade-offs:** compare at least 2 viable approaches and justify the chosen abstraction level (*Altitude*).

## 2. Protocol `ultracode` (Deep Hard-Loop Multi-Agent Execution — Gear 3)
The keyword `ultracode` activates **Gear 3 (`Deep` Hard-Loop)** — optimizing for exhaustive correctness, security, and runtime verification:
0. **Phase 0 — Arm Hard-Loop (`craftsman_state.json`):** Initialize the session gate in `deep` mode:
   `~/.gemini/config/scripts/craftsman-stop-gate.py --state-file "<artifactDirectoryPath>/craftsman_state.json" --init --mode ultracode --tier deep --cwd "<repo_dir>"`
1. **Phase 1 — Discovery (`research` subagents):** For non-trivial tasks, launch parallel `research` subagents (`invoke_subagent`) to map existing codebase patterns, call sites, and edge cases.
2. **Phase 2 — High-Precision Implementation:** Implement the solution following `ultrathink` invariants and clean architecture principles.
3. **Phase 3 — Mandatory Independent Quality Gate Subagent (`Creator != Reviewer`):**
   Do not self-certify your own code inline. Spawn an independent subagent via `invoke_subagent` (`TypeName: "self"`, `Role: "Craftsman Quality Gate Auditor"`) on the full cumulative diff (`git diff <base_sha>`) to read and execute:
   - **`/simplify`** — clean up code across *Reuse*, *Simplification*, *Efficiency*, and *Altitude*,
   - **`/code-review`** + **`/audit all`** — run full correctness and specialist audits (*errors*, *types*, *tests*, *security*, *perf*) and fix all `CONFIRMED` / `PLAUSIBLE` findings directly,
   - **`/verify`** — run unit tests and end-to-end runtime verification.
4. **Phase 4 — Close Hard-Loop Gate:** Only after receiving the report from `Craftsman Quality Gate Auditor`, run:
   `~/.gemini/config/scripts/craftsman-stop-gate.py --state-file "<artifactDirectoryPath>/craftsman_state.json" --complete`
