# Craftsman Demo: Catalog Pagination

A minimal demo project showing how Craftsman stops an AI coding agent from declaring "done" on an
incomplete or buggy implementation.

## The Request

Say it in plain words:
> *"Add 1-based pagination to catalog search results (page number and optional page size)."*

### Why Agents Typically Get This Wrong
Pagination invites subtle boundary defects:
1. **Partial page truncation**: `total_pages = len(products) // size` drops the remainder (25 items at
   page size 20 yields 1 page instead of 2; 5 items yields 0 pages).
2. **Missing page validation**: `start = (page - 1) * size` without checking `page >= 1`. In Python,
   `page = 0` produces negative slice indices (`products[-20:0]`), returning the wrong items instead
   of an error or an empty result.

## What Craftsman Does

1. **Build freely.** `/craftsman` records the base commit; the agent implements the feature and runs
   its own tests without review loops.
2. **Hunt.** The agent freezes the final diff and sends fresh-context hunters over it, each with a
   few lenses (`contracts`, `intent`, `failure`, `tests`, ...). They did not write the code and
   record every candidate defect with a concrete trigger, such as `page=0` or 25 items at page size
   20.
3. **Verify.** Separate verifier subagents try to disprove each candidate and reproduce the survivors
   with `craftsman run --kind repro`, proving the boundary defects against the real code.
4. **Saturate.** Because the hunt proved defects, the agent runs another wave: variant
   hunters look for the same mistake elsewhere, plus one fresh hunter with no knowledge of the
   earlier findings.
5. **Stop hook.** If the agent tries to finish early, the hook sends it back with the list of what is
   missing:
   ```
   [CRAFTSMAN GATE 1/6] review incomplete: 2 hunks not covered; 1 finding without a verdict. Task completion is blocked until the evidence ledger proves the change is reviewed and verified.
   ```
6. **Fix and test.** The agent fixes the boundary bugs at the root cause and adds regression tests
   that fail before the fix and pass after. A fresh hunter checks the fix delta for regressions, the
   agent runs `craftsman run --kind test`, and the gate passes. The report says what was found, fixed, and
   dismissed, and why the agent chose its depth.

## Quickstart

Set up an isolated demo repository:

```bash
./setup.sh /tmp/craftsman-demo
cd /tmp/craftsman-demo
```

Then, in Claude Code, GitHub Copilot, or Antigravity, ask for the feature in your own words:

```text
/craftsman Add 1-based pagination to catalog search results (page number and optional page size)
```

To see only the review, implement the feature yourself and ask:

```text
/craft-review review my changes and fix what you find
```
