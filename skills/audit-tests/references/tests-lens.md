# Craftsman Review Lenses

A lens is one narrow way of looking at a change: a goal, what to read first, and numbered probes.
Answer every probe for every hunk in your slice. If you cannot answer one from what you have read,
open more files before moving on.

How to use a probe: pick a concrete value or situation and *execute the code in your head*, line
by line, writing the intermediate values down. Most real defects surface at the moment a reviewer
substitutes a specific value instead of reading the code as prose.

---

## `tests` — Test evidence

**Goal:** the tests would catch this change being wrong.

**Probes**
1. For each claim from the `intent` lens, find the test that proves it. A claim without a test is
   a finding (severity by risk).
2. Delete-the-change check: imagine the production hunk removed. Which test fails? If none, the
   tests do not protect the change.
3. Flip-one-operator check: imagine one comparison flipped, one branch deleted, or an early return
   added in the changed code. Name the assertion that would notice — or record that none would.
4. Assertions that cannot fail: `assertGreaterEqual(x, 0)` on a value that is never negative,
   `assertTrue(result)` on a non-empty object, asserting only that no exception was raised, or
   asserting on the mock that replaced the code under test. Also look for assertions the diff
   *weakened* to make a test pass: `assertEqual` turned into `assertIn`/`assertTrue`, an exact value
   turned into a range or a wider tolerance, a removed `assertRaises`, a deleted, skipped or
   `xfail`ed test. Ask what the old assertion protected and whether the production change now
   violates it.
5. Negative and boundary paths: error paths, empty inputs and limits are exercised, not only the
   happy path. A bug fix needs a test that fails before the fix.
6. Test hygiene: dependence on wall-clock time, randomness without a seed, test order, network,
   environment variables or a particular machine; state leaking between tests; files or processes
   left behind.

**Limits:** missing tests are one finding per change, not one per hunk, and it names the riskiest
claim that no test would catch. Its severity never exceeds that of the defect the missing test
would have caught. If the repository has no automated tests at all, record at most one `minor`
finding, and none if the project evidently does not use them. A test that exists but checks the
wrong thing (probe 4) is a separate finding, anchored at that test.
