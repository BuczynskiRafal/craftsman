# Craftsman Demo: Catalog Pagination

A minimal demo project showcasing how Craftsman stops AI coding agents from declaring "done" on incomplete or buggy implementations.

## The Challenge

Task given to the AI coding agent:
> *"Add 1-based pagination to catalog search results (page number and optional page size)."*

### Why LLM Agents Typically Fail This Task
When implementing pagination, AI agents frequently introduce subtle boundary defects:
1. **Partial Page Truncation**: Using floor division `total_pages = len(products) // size`, dropping the remainder (e.g., 25 items at page size 20 yields 1 page instead of 2; 5 items yields 0 pages).
2. **Missing Page Validation**: Computing `start = (page - 1) * size` without validating `page >= 1`. In Python, `page = 0` produces negative slice indices (e.g. `products[-20:0]`), returning items from the end of the list instead of an error or empty result.

## How Craftsman Protects the Codebase

1. **Gate Engagement**: When the agent is prompted with `/craftsman ...`, Craftsman locks the baseline commit and monitors the evidence ledger.
2. **Review & Lens Contracts**: Review subagents examine changed hunks through specialized lenses (`contracts`, `intent`, `failure`, `tests`).
3. **STOP-Hook Enforcement**: When the agent attempts to conclude prematurely, the STOP-hook blocks completion with an immediate summary:
   ```
   [CRAFTSMAN GATE 1/5] review incomplete: 2 hunks not covered by lens contracts; 1 finding without a verdict. Task completion is blocked until the evidence ledger proves the change is reviewed and verified.
   ```
4. **Adversarial Verification**: A verifier agent executes reproduction scripts (`craftsman run --kind repro`) against the real code, proving the boundary defects before allowing a fix.
5. **Verified Resolution**: The agent fixes the boundary bugs, adds regression tests, runs verification (`craftsman run --kind test`), and the gate passes cleanly.

## Quickstart: Running the Demo

Set up an isolated demo repository:

```bash
./setup.sh /tmp/craftsman-demo
cd /tmp/craftsman-demo
```

Then in your AI coding assistant (Claude Code, GitHub Copilot, or Antigravity):

```bash
/craftsman Add 1-based pagination to catalog search results (page number and optional page size)
```
