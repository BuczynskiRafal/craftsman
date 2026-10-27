---
trigger: always_on
---

# Keyword Triggers: `ultrathink` & `ultracraft`

When the user's request contains one of these keywords (case-insensitive), switch to the matching
rigor mode.

## `ultrathink` — think before building

Before writing or editing any code, include a section **`### Design analysis`** with:
1. **Invariants and hazards:** hidden assumptions, edge cases, state and concurrency hazards, and
   the domain rules the change must preserve.
2. **Costs:** time and memory complexity of the chosen approach, I/O and network bottlenecks,
   failure modes of external dependencies.
3. **Alternatives:** at least two genuinely different approaches (for example the smallest change
   that fits the current structure vs. a cleaner restructuring) with concrete pros and cons, and
   the reason for your choice.
4. **Pre-mortem:** the three most likely ways the chosen solution fails in production and what in
   your implementation prevents each.

## `ultracraft` — the deep quality loop

Treat the request as `/craftsman --deep <request>` and follow the `/craftsman` skill end to end:
1. `craftsman gate --init --tier deep --task "<request in one line>"` before anything else
   (`--await-user` / `--resume` around questions to the user).
2. Include the `ultrathink` design analysis before implementing.
3. Implement with explicit types and explicit error handling: no silent failures, no broad
   catch-alls, invalid states made impossible where the type system allows it.
4. Run the quality loop on the whole cumulative diff: `/simplify`, then review rounds with every
   lens in fresh-context `craftsman-reviewer` subagents plus a final sweep, a
   `craftsman-verifier` for every finding, fixes with delta re-review, and runtime verification
   through `craftsman run` (tests **and** an end-to-end run).
5. Finish only when `craftsman review check` passes, then `craftsman gate --complete` and report
   with `craftsman review report`.
