# Keyword Triggers: `ultrathink` & `ultracode`

Whenever the user's request (`<USER_REQUEST>`) contains either of the following keywords (case-insensitive), immediately activate the corresponding engineering rigor mode:

---

## 1. Keyword: `ultrathink` (Deep Architectural Reasoning)
When `ultrathink` is present in the prompt, **before writing a single line of code or proposing an edit**, you MUST include an explicit **`### 🧠 Deep Reasoning & Architecture Analysis`** section covering:
1. **Deconstruction & Invariants**: Identify hidden assumptions, edge cases, concurrency/state hazards, and domain invariants that must be preserved.
2. **Complexity & Resource Trade-offs**: Explicitly evaluate time ($O(\dots)$) and memory ($O(\dots)$) complexity, I/O bottlenecks, and failure modes.
3. **Counterfactual Architecture Comparison**: Compare at least **2 distinct approaches** (e.g., pragmatic minimal change vs. clean structural refactor) with concrete pros/cons before selecting the winning design.
4. **Failure-Mode Pre-Mortem**: List the top 3 ways the chosen solution could fail in production and how your implementation prevents them.

---

## 2. Keyword: `ultracode` (Gear 3 Deep Hard-Loop + Independent Quality Gate Subagent)
When `ultracode` is present in the prompt, execute the full **Gear 3 (`deep`) Hard-Loop** pipeline (`Creator != Reviewer`):
1. **Initialize Gear 3 (`deep`) Hard-Loop State**:
   Run:
   ```bash
   python3 ~/.gemini/config/scripts/craftsman-stop-gate.py --state-file <appDataDir>/brain/<conversation-id>/craftsman_state.json --init --mode ultracode --tier deep --cwd "$(pwd)"
   ```
   *(If waiting on `ask_question`, pass `--await-user`, then `--resume` once the user responds).*
2. **Parallel Subagent Exploration**:
   For non-trivial codebase questions, launch 2–3 `research` subagents (`invoke_subagent` with `Model: "inherit"`) in parallel to map all callers, types, and existing patterns before editing.
3. **Production-Grade Implementation**:
   - Strictly enforce type safety (*make illegal states unrepresentable*), explicit error handling (zero silent failures or bare `except:`), and DRY/SOLID principles.
4. **Mandatory Independent Quality Gate Subagent (`Creator != Reviewer`)**:
   - **Never perform the review solely inline in the main thread!** Before finishing your response, spawn an independent subagent via **`invoke_subagent`** (`TypeName: "self"`, `Model: "inherit"`, `Role: "Craftsman Quality Gate Auditor"`), passing the repository path and `base_sha` from `craftsman_state.json`.
   - The subagent must execute in a fresh context across the entire cumulative diff (`git diff <base_sha>`):
     1. **`/simplify`** (Reuse, Simplification, Efficiency, Altitude) and apply simplifications directly,
     2. **`/code-review`** + **`/audit all`** (`/audit-errors`, `/audit-tests`, `/audit-types`, `/audit-comments`, `/audit-security`) and fix all `CONFIRMED` / `PLAUSIBLE` findings,
     3. **`/verify`** (unit tests + real end-to-end runtime verification).
   - Once the subagent reports back, close the Hard-Loop gate:
     ```bash
     python3 ~/.gemini/config/scripts/craftsman-stop-gate.py --state-file <appDataDir>/brain/<conversation-id>/craftsman_state.json --complete
     ```
     *(The script inspects `transcript.jsonl` and rejects `--complete` if `invoke_subagent` was not called).*
