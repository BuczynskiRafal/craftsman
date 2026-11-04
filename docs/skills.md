# Bundled Skills Catalog

Craftsman includes 17 specialized skills and agent roles designed to assist throughout the entire software development lifecycle:

| Command / Skill | Role & Description |
| :--- | :--- |
| `/craftsman` | **Orchestrator**: Records the base commit, lets the agent build freely, then runs the final review hunt, fixes, tests, and closes the gate. |
| `/feature-dev` | **Feature Track**: Explores existing code patterns, clarifies open questions, evaluates design trade-offs, and implements in small verified steps. |
| `/modernize` | **Legacy Track**: Pins legacy behavior using characterization tests and rule cards before safe transformation. |
| `/batch` | **Mass-Change Track**: Splits extensive mechanical migrations (API updates, type sweeps) into disjoint work units for parallel execution. |
| `/simplify` | **Cleanup Loop**: Applies behavior-preserving cleanups: reduces complexity, eliminates duplication, and prunes unused work. |
| `/code-review` | **Review Engine**: Freezes the diff, runs fresh-context hunters and verifiers in adaptive waves, and records the outcome in the evidence ledger. |
| `/verify` | **Runtime Verification**: Exercises changed behavior against live environments, recording exit codes and outputs. |
| `/audit` | **Targeted Multi-Lens Audit**: In-depth audit across selectable lenses (errors, tests, types, security, quality). |
| `/audit-comments` | **Docstring & Comment Audit**: Verifies factual accuracy of docstrings and comments against actual implementation code. |
| `/audit-errors` | **Error Flow Audit**: Traces exceptions and error propagation paths to prevent swallowed failures and broad catch-alls. |
| `/audit-security` | **Security Audit**: Traces untrusted data sources to sinks, inspecting validation guards and privilege boundaries. |
| `/audit-tests` | **Test Quality Audit**: Evaluates test effectiveness through mutant survival thought experiments and boundary checks. |
| `/audit-types` | **Type Design Audit**: Evaluates domain types to ensure illegal states are unrepresentable in the type system. |
| `/code-quality` | **Clean Code Analysis**: Audits SOLID principles, DRY violations, cohesion, and naming hygiene. |
| `/extreme-optimizer` | **Performance Optimization**: Profile-driven performance loop enforcing benchmark proof and strict output isomorphism. |
| `/leetcode-style-code-reviewer` | **Algorithmic Review**: Competitive-programming evaluation of algorithmic efficiency and complexity. |
| `/high-level-commit-message` | **Commit Helper**: Generates concise, atomic Conventional Commits messages summarizing high-level intent. |

---

## Inspecting Skills via CLI

Skills can be inspected and listed directly from the command line without searching disk paths:

```bash
craftsman skill list                                           # list all 17 bundled skills
craftsman skill show craftsman                                 # display orchestrator skill definition
craftsman skill show code-review --lenses intent,contracts     # print specific lens playbooks
craftsman skill path simplify                                  # output filesystem path to skill directory
```
