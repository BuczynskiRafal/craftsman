# 🛠️ Craftsman — Universal Agentic Engineering Framework

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Version](https://img.shields.io/badge/version-2.0.0-emerald.svg)](plugin.json)
[![Platforms](https://img.shields.io/badge/platforms-Claude_Code_%7C_Codex_%7C_Copilot_%7C_Cursor_%7C_Antigravity-purple.svg)](#multi-platform-installation)

**Craftsman** is a universal, zero-trust **Agentic Software Engineering & Quality Gate Framework** (`Twórca ≠ Recenzent` / *Creator ≠ Reviewer*). It equips AI coding agents (**Claude Code**, **OpenAI Codex CLI**, **GitHub Copilot**, **Cursor**, and **Google Antigravity**) with 17 structured engineering workflows, a **3-Gear Adaptive Quality Loop** (Soft-Loop vs. Hybrid Hard-Loop), and deterministic OS-level Git/Lifecycle guardrails.

---

## ✨ Why Craftsman?

1. **Creator ≠ Reviewer (`Twórca ≠ Recenzent`) Verified at Runtime:**
   AI agents suffer from confirmation bias when reviewing code they just wrote in the same context window. For non-trivial changes, Craftsman's `Stop` hook scans `transcript.jsonl` and physically blocks task completion until the primary agent spawns a fresh, independent **`Craftsman Quality Gate Auditor` subagent** to execute `/simplify` -> `/code-review` -> `/verify` on the full cumulative diff (`base_sha..HEAD`).
2. **3-Gear Automatic Gearbox (`Quick` vs `Standard` vs `Deep`):**
   No brute-force overhead on 5-line fixes. Craftsman automatically measures the cumulative git diff (`git diff --numstat <base_sha>`) and selects the optimal verification depth:
   - **Gear 1 — `Quick` (Soft-Loop):** `<= 40` changed lines across `<= 2` files (or `/craftsman --quick`). Runs fast inline `/simplify` + `/verify` with **zero subagent overhead**.
   - **Gear 2 — `Standard` (Hybrid Hard-Loop):** `> 40` changed lines or `> 2` files. Requires an independent `Craftsman Quality Gate Auditor` subagent verified in `transcript.jsonl`.
   - **Gear 3 — `Deep` (Full-Audit Hard-Loop):** Triggered via `/craftsman --deep` or `ultracode`. Activates pre-implementation `ultrathink` architecture analysis + independent subagent running all 5 specialized `/audit` suites.
3. **Write Once, Run Across Every AI Agent:**
   One repository exports native slash commands, prompts, instructions, and rules to **Claude Code** (`~/.claude/`), **GitHub Copilot** (`Code/User/prompts` & `.github/prompts/`), **OpenAI Codex / Windsurf** (`AGENTS.md`), **Cursor** (`.cursor/rules/craftsman.mdc`), **Google Antigravity** (`~/.gemini/config/`), and **native OS Git hooks** (`commit-msg` & `pre-push`).

---

## 🚀 Multi-Platform Installation

```bash
git clone https://github.com/BuczynskiRafal/craftsman.git ~/.craftsman
cd ~/.craftsman

# Install everywhere at once (Antigravity + Claude Code + GitHub Copilot + Global Git Hooks):
./install.sh --all
```

### Target-Specific Flags (`./install.sh`)
| Flag | Target Platform / Effect |
| :--- | :--- |
| `./install.sh --all` | Installs across **Google Antigravity**, **Claude Code**, **GitHub Copilot**, and **Global Git Hooks** in one shot. |
| `./install.sh --claude` | Exports all 17 slash commands (`~/.claude/commands/*.md`) and global rules (`~/.claude/rules/craftsman-rules.md`) for **Claude Code**. |
| `./install.sh --copilot` | Exports all 17 prompt files (`*.prompt.md`) and global instructions (`craftsman-rules.instructions.md`) to VS Code User profile for **GitHub Copilot Chat**. |
| `./install.sh --copilot-workspace <dir>` | Exports `.github/prompts/*.prompt.md` and `.github/copilot-instructions.md` into a project repository. |
| `./install.sh --agents-workspace <dir>` | Generates universal `AGENTS.md` (**OpenAI Codex CLI**, **Windsurf**, **Zed**) and `.cursor/rules/craftsman.mdc` (**Cursor**) in a project repository. |
| `./install.sh --git-hooks` | Installs OS-level `commit-msg` and `pre-push` hooks (`git config --global core.hooksPath`) enforcing Zero AI Attribution and `main`/`master` push confirmation across all IDEs and terminals. |
| `./install.sh --symlink` / `--copy` | Installs skills, rules, `hooks.json`, and scripts into `~/.gemini/config/` for **Google Antigravity**. |

---

## ⚙️ 3-Gear Adaptive Architecture (`/craftsman`)

```mermaid
flowchart TD
    User(["User: /craftsman [task]"]) --> Triage["Stage 0: Dynamic Router + Capture base_sha (--init)"]

    Triage -->|"Path A: Feature"| Arch["/feature-dev (Explore -> Clarify -> Architect -> Implement)"]
    Triage -->|"Path B: Legacy / Refactor"| Mod["/modernize (Given/When/Then Spec -> Characterization Tests -> Uplift)"]
    Triage -->|"Path C: Mass Migration"| Batch["/batch (Decompose into Work Units -> Parallel Subagents)"]
    Triage -->|"Path D: Polish / Fix"| Gearbox{"Stage 1: 3-Gear Gearbox\n(git diff --numstat base_sha)"}

    Arch --> Gearbox
    Mod --> Gearbox
    Batch --> Gearbox

    Gearbox -->|"Gear 1: Quick (<=40 lines & <=2 files\nor --quick)"| SoftLoop["Inline Soft-Loop:\n/simplify + /verify (0 subagents)"]
    Gearbox -->|"Gear 2: Standard (>40 lines or >2 files)\nGear 3: Deep (--deep / ultracode)"| Subagent["Independent Subagent (Twórca != Recenzent):\ninvoke_subagent (Craftsman Quality Gate Auditor)"]

    Subagent --> Simplify["Step 1: /simplify on cumulative diff (base_sha..HEAD)"]
    Simplify --> Review["Step 2: /code-review + /audit-* + Auto-Fix (Angles A-E)"]
    Review --> Verify["Step 3: /verify (Unit tests + real End-to-End runtime check)"]

    SoftLoop -->|"PASS + --complete"| Commit["Stage 2: Sign-Off Card + /high-level-commit-message"]
    Verify -->|"PASS + --complete"| Commit
```

### Gearbox Comparison Table

| Gear | Trigger | Subagent Required? | Verification Scope | Best Used For |
| :--- | :--- | :---: | :--- | :--- |
| **Gear 1: `Quick` (Soft-Loop)** | Automatic when cumulative diff is **$\le 40$ lines across $\le 2$ files**, or manual `/craftsman --quick` | ❌ No (Fast Inline) | `/simplify` + `/verify` in main thread | Config tweaks, small bugfixes, 1–2 file adjustments |
| **Gear 2: `Standard` (Hybrid Hard-Loop)** | Automatic when cumulative diff is **$> 40$ lines or $> 2$ files**, or manual `/craftsman --standard` | ✅ Yes (`Craftsman Quality Gate Auditor`) | Independent subagent runs `/simplify` -> `/code-review` -> `/verify` on `base_sha..HEAD` | Everyday features, multi-file refactors, bugfix PRs |
| **Gear 3: `Deep` (Full-Audit Hard-Loop)** | Manual `/craftsman --deep` or keyword `ultracode` | ✅ Yes (`Craftsman Quality Gate Auditor`) | `ultrathink` pre-analysis + independent subagent runs `/simplify` -> `/code-review` -> `/audit all` -> `/verify` | Critical core logic, security-sensitive modules, complex migrations |

---

## 📦 Included Skills (17 Core Engineering Workflows)

### 1. Master Orchestrator
| Command | Description |
| :--- | :--- |
| **`/craftsman`** | End-to-end engineering orchestrator. Tracks cumulative diff (`base_sha..HEAD`), selects architectural workflow (A–D), and enforces the 3-Gear Quality Gate. |

### 2. Architectural & Transformation Workflows
| Command | Description |
| :--- | :--- |
| **`/feature-dev`** | 7-phase feature development: codebase exploration -> clarifying questions -> 2–3 architectural trade-off options -> implementation -> review. |
| **`/modernize`** | Safe legacy modernization: extracts hidden business rules into `Given / When / Then` specs, builds characterization tests, refactors, and proves equivalence. |
| **`/batch`** | Parallel mass refactoring & migrations across 5–30 independent work units executed by background subagents. |

### 3. Daily Quality Gate
| Command | Description |
| :--- | :--- |
| **`/simplify`** | Behavior-preserving code cleanup across 4 angles: Reuse, Simplification, Efficiency, and Abstraction Altitude. |
| **`/code-review`** | 10-angle code review (local diff, branch, or GitHub/GitLab PR) with finding verification (`CONFIRMED` / `PLAUSIBLE` / `REFUTED`). |
| **`/verify`** | End-to-end runtime verification (CLI, HTTP server, data pipeline, headless GUI) beyond static unit tests. |
| **`/high-level-commit-message`** | Concise, high-level Conventional Commits message generator in American English. |

### 4. Specialized Deep Audits (`/audit`)
| Command | Description |
| :--- | :--- |
| **`/audit`** | Multi-aspect audit orchestrator (`/audit all`, `/audit errors,security`). |
| **`/audit-errors`** | *Silent Failure Hunter* — detects swallowed exceptions, overly broad `try/except`, and silent `None`/`0`/`[]` fallbacks. |
| **`/audit-tests`** | *Behavioral Test Coverage* — evaluates behavioral coverage, edge cases, and refactor resilience on a 1–10 scale. |
| **`/audit-types`** | *Type & Invariant Analyzer* — audits domain models, dataclasses, and Pydantic schemas across 4 axes (*make illegal states unrepresentable*). |
| **`/audit-comments`** | *Comment Rot Analyzer* — cross-checks docstrings and comments against actual implementation behavior. |
| **`/audit-security`** | *Source-to-Sink Taint Auditor* — traces untrusted input to sensitive sinks with CWE classification and minimal patches. |

### 5. Performance & Code Hygiene
| Command | Description |
| :--- | :--- |
| **`/code-quality`** | Clean architecture audit focusing on DRY, SOLID, orthogonality, and naming consistency. |
| **`/extreme-optimizer`** | Iterative profile-driven performance optimization loop requiring benchmarks and output isomorphism proof. |
| **`/leetcode-style-code-reviewer`** | Competitive-programming-grade Python/NumPy/PyTorch hot-path performance review. |

---

## 🛡️ Deterministic System Guards (`scripts/` & `git-hooks/`)

1. **`scripts/git-guard.py` (`PreToolUse` hook + native `commit-msg` & `pre-push` Git hooks):**
   - **Rule #1 (Protected Branch Guard):** Blocks unconfirmed `git push` to `main`/`master` unless the user explicitly confirmed via `ask_question` / `AskUserQuestion` or interactive terminal prompt.
   - **Rule #2 (Zero AI Attribution):** Rejects any commit, tag, or PR containing `Co-authored-by:` or `Generated by/with ...` AI trailers.
2. **`scripts/craftsman-stop-gate.py` (`Stop` hook + state CLI):**
   - Enforces the 3-Gear Quality Loop and Zero-Trust `transcript.jsonl` verification for Gear 2 (`standard`) and Gear 3 (`deep`).

---

## 🙏 Acknowledgments & License

- **License:** Released under the [MIT License](LICENSE) (`Copyright (c) 2026 Rafał Buczyński`).
- **Architectural Inspiration:** Informed by multi-agent separation-of-concerns patterns (*Creator ≠ Reviewer*), lifecycle hook state machines, and structured specification-driven modernization practices from the AI software engineering ecosystem.
