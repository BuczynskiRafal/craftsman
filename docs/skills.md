# Bundled Skills Catalog

Craftsman ships 13 skills following the [Agent Skills](https://agentskills.io) specification (`npx skills add BuczynskiRafal/craftsman`). Eleven workflow and audit skills work standalone without Python; `/craft-review` works standalone in `UNGATED` mode; `/craftsman` requires the `craftsman-gate` CLI.

```bash
npx skills add BuczynskiRafal/craftsman --skill audit-security --skill audit-errors
npx skills add BuczynskiRafal/craftsman --skill feature-dev --skill simplify --skill verify
npx skills add BuczynskiRafal/craftsman --skill craft-review
```

See the [Installation Guide](installation.md) for CLI and platform plugin setup.

---

## Core Gate & Review Loop

| Command / Skill | CLI Requirement | Role & Description |
| :--- | :--- | :--- |
| `/craftsman` | **Requires `craftsman-gate`** | **Orchestrator**: Records the base commit, lets the agent build freely, then runs the planned hunt, verification, fixes, and tests until the Stop gate passes. |
| `/craft-review` | **Enforced with CLI; ungated standalone** | **Review Engine**: With `craftsman-gate`, freezes the diff, plans risk-driven hunt slots, runs fresh-context hunters and verifiers in waves, and records everything in the evidence ledger. Without the CLI, reads its bundled `lenses.md` playbook and marks reports **UNGATED**. |

---

## Standalone Audit & Workflow Skills (zero CLI required)

When `craftsman` is absent, these 11 skills run check commands directly in the shell and read their bundled `references/<lens>-lens.md` files (with the CLI installed, checks run via `craftsman run` and playbooks via `craftsman skill show craft-review --lenses <id>`).

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
