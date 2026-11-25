# Craftsman Review Lenses

A lens is one narrow way of looking at a change: a goal, what to read first, and numbered probes.
Answer every probe for every hunk in your slice. If you cannot answer one from what you have read,
open more files before moving on.

How to use a probe: pick a concrete value or situation and *execute the code in your head*, line
by line, writing the intermediate values down. Most real defects surface at the moment a reviewer
substitutes a specific value instead of reading the code as prose.

---

## `model` — Data model & invariants

**Goal:** data structures reject invalid combinations when they are created, not when they are
used.

**Probes**
1. For each new or changed type, write its rules: which fields must agree, which combinations are
   impossible, which values are out of range. Try to build a violating value through the public
   API (constructor, factory, deserialization, attribute assignment, mutation of an exposed
   collection). Where is the violation first noticed — at creation, at load, at first use, or
   never?
2. Coupled optionals: if field B is only meaningful when field A has a particular value, the
   structure is several states sharing one shape; separate types or a discriminated union remove
   the invalid combinations.
3. Interchangeable scalars: plain strings or numbers that carry a unit, an identifier kind, or an
   "already validated" status can be swapped or mixed by callers; check whether the diff makes such
   a mix-up possible.
4. Mutability: internals exposed for mutation, "immutable" types holding mutable members,
   equality and hashing that disagree.
5. Serialization: every field survives a save/load round trip (enums, dates, optional values,
   nested types) and validation also runs on load.
