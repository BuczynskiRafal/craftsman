---
name: audit-types
description: >-
  Audit data structures and type design: for each new or changed type, lists
  its invariants, the illegal states it still allows (with a constructing
  example), where each invariant is enforced, and the smallest redesign that
  makes the illegal states unrepresentable. Use when /audit-types is invoked
  or via /audit types.
---

# Type & data-model audit (`/audit-types [target]`)

Deepens the `model` lens (run `craftsman skill show code-review --file lenses.md` to read that
playbook first; never search the file system for `lenses.md`).

## Search scope

Your workspace is the git work tree you were started in (`git rev-parse --show-toplevel`): search,
list, and read only inside it (prefer `git ls-files`, `git grep`, and editor search scoped to this
folder). Never run recursive searches of `~`, `/`, or parent directories, and never open other
repositories, other projects' packets/ledgers/reports, or assistant session stores (run
`craftsman skill show craftsman --file scope.md` for the full contract).

The measure of a type here is simple: **how many states can be built that the domain forbids,
and how far does a bad value travel before anything notices?**

## For each new or changed type

(classes, records, dataclasses, structs, enums, interfaces, schemas, typed dicts, protocol
messages, database models, configuration objects)

1. **Invariants.** Write them as checkable statements: ranges, required combinations, mutually
   exclusive fields, ordering, uniqueness, formats, units, lifecycle (a value valid only after
   `open()`).
2. **Illegal states still constructible.** For each invariant, try to construct a violating value
   through the public API — constructor, factory, deserialization, field assignment, mutation of
   an exposed collection. Write the one-line constructing expression. No expression means the
   invariant is enforced.
3. **Enforcement point.** Where would a violation be noticed: at construction, at load, at first
   use deep inside the system, or never?
4. **Redesign.** The smallest change that removes each illegal state: a tagged union or separate
   types instead of flag-plus-optional fields, an enum or `Literal` instead of a free string,
   a validated value type instead of a raw primitive, `frozen`/read-only views instead of exposed
   mutable internals, validation on deserialization. Skip redesigns whose cost exceeds the risk
   and say so.

## Report

Per type:

```markdown
### `TypeName` (file:line)
- Invariants: …
- Illegal but constructible: `TypeName(kind="circle", radius=None)` -> fails at render time (file:line)
- Enforced at: construction | load | first use | never
- Proposal: … (or "keep as is: …")
```

Then the findings in the standard form for anything that can already cause a defect.
