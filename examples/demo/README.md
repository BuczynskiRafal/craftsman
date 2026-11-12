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
   its own tests. Craftsman reviews nothing in the meantime.
2. **Freeze and plan.** Once the agent considers the feature done, the diff is frozen and split into
   portions to check (slots), each with a few perspectives to examine it from (lenses such as
   `contracts`, `intent`, `failure`, `tests`).
3. **Bug hunt.** Each portion goes to a separate fresh-context agent that did not write the code. It
   records every suspected bug with the concrete situation that triggers it, such as `page=0` or 25
   items at page size 20.
4. **Verification.** Separate agents try to disprove each suspicion and reproduce the survivors with
   `craftsman run --kind repro`, proving the boundary bugs against the real code. Because this wave
   confirmed bugs, another one runs: it looks for the same mistakes elsewhere and takes one fresh look
   with no knowledge of the earlier findings.
5. **Fixes and tests.** The agent fixes the boundary bugs at the root cause and adds regression tests
   that fail before the fix and pass after. A fresh-context hunter re-checks the fixes, the agent runs
   `craftsman run --kind test`, and the gate passes. The report says what was found, fixed, and
   dismissed, and why the agent chose its depth.

If the agent tries to finish before all of this is done, the stop hook sends it back with the list of
what is missing:

```
[CRAFTSMAN GATE 1/6] review incomplete: 2 hunks not covered; 1 finding without a verdict. Task completion is blocked until the evidence ledger proves the change is reviewed and verified.
```

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
