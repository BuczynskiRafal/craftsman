# Bundled Skills Catalog

Craftsman ships 13 skills following the [Agent Skills](https://agentskills.io) specification. Eleven audit and workflow skills work completely standalone (zero Python or CLI required); the two core loop skills drive the gated build-and-review workflow (with an ungated standalone fallback for `/craft-review`).

Install all 13 skills via [`skills.sh`](https://skills.sh) / `npx skills add`:

```bash
npx skills add BuczynskiRafal/craftsman
```

Or install only the standalone skills you need:

```bash
npx skills add BuczynskiRafal/craftsman --skill audit-security --skill audit-errors
npx skills add BuczynskiRafal/craftsman --skill feature-dev --skill simplify --skill verify
npx skills add BuczynskiRafal/craftsman --skill craft-review
```

See the [Installation Guide](installation.md) for full CLI and platform plugin setup.

---

## Core Gate & Review Loop

| Command / Skill | CLI Requirement | Role & Description |
| :--- | :--- | :--- |
| `/craftsman` | **Requires `craftsman-gate`** | **Orchestrator**: Records the base commit, lets the agent build freely, then runs the planned hunt, verification, fixes, and tests until the Stop gate passes. |
| `/craft-review` | **Enforced with CLI; ungated standalone** | **Review Engine**: With `craftsman-gate`, freezes the diff, plans risk-driven hunt slots, runs fresh-context hunters and verifiers in waves, and records everything in the evidence ledger. Without the CLI, reads its bundled `lenses.md` playbook and marks reports **UNGATED**. |

---

## Standalone Audit & Workflow Skills (zero CLI required)

These 11 skills work standalone via `npx skills add` with no Python installation. When `craftsman` is installed, they use `craftsman run` for ledger-recorded tests and `craftsman skill show craft-review --lenses <id>`; when it is absent, lens-deepening skills read their bundled `references/<lens>-lens.md` file and check commands run directly in the shell.

### Workflow Tracks

| Command / Skill | Role & Description |
| :--- | :--- |
| `/feature-dev` | **Feature Track**: Explores existing code patterns, settles open questions, compares design trade-offs, and implements in small verified steps. |
| `/modernize` | **Legacy Track**: Recovers legacy behavior with numbered rules and characterization tests before refactoring or replacing code. |
| `/batch` | **Mass-Change Track**: Splits mechanical migrations (API updates, type sweeps) into disjoint, verified work units for parallel execution. |
| `/simplify` | **Cleanup Loop**: Applies behavior-preserving cleanups to the diff (duplication, needless complexity, dead weight, misleading names); bundles `references/craft-lens.md`. |
| `/verify` | **Runtime Verification**: Exercises changed behavior against real entry points and records exit codes and outputs. |

### Focused Audits

| Command / Skill | Deepened Lens | Role & Description |
| :--- | :--- | :--- |
| `/audit` | `errors`, `tests`, `types`, `comments`, `security` | **Targeted Multi-Lens Audit**: Coordinates exhaustive single-concern audits across selectable aspects and verifies findings before reporting. |
| `/audit-comments` | `craft` (`references/craft-lens.md`) | **Docstring & Comment Audit**: Extracts factual claims from comments and docstrings and checks them against implementation code. |
| `/audit-errors` | `failure` (`references/failure-lens.md`) | **Error Flow Audit**: Inventories failure and handling sites to catch swallowed exceptions, silent defaults, and misleading exit codes. |
| `/audit-security` | `trust` (`references/trust-lens.md`) | **Security Audit**: Traces untrusted data sources to sinks and inspects path guards, authorization boundaries, secrets, and resource limits. |
| `/audit-tests` | `tests` (`references/tests-lens.md`) | **Test Quality Audit**: Maps observable behaviors to tests and runs mutant thought experiments to find gaps tests would miss. |
| `/audit-types` | `model` (`references/model-lens.md`) | **Type Design Audit**: Checks domain types and schemas for illegal states they still allow and pushes validation to boundaries. |

---

## Inspecting Skills via CLI

When `craftsman-gate` is installed, skills and lens playbooks can be inspected directly from the command line without searching disk paths:

```bash
craftsman skill list                                           # list all 13 bundled skills
craftsman skill show craftsman                                 # display orchestrator skill definition
craftsman skill show craft-review --lenses intent,contracts     # print specific lens playbooks
craftsman skill path simplify                                  # output filesystem path to skill directory
```
