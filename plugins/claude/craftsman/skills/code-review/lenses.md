# Craftsman Review Lenses

A lens is one narrow way of looking at a change. Each lens below is a playbook: a goal, the
material to read first, and numbered probes — concrete questions you answer for **every hunk**
assigned to you. If you cannot answer a probe from what you have read, you have not read enough
yet: open more files before moving on.

How to use a probe: pick a concrete value or situation and *execute the code in your head*, line
by line, writing the intermediate values down. Most real defects surface at the moment a
reviewer substitutes a specific value instead of reading the code as prose.

Record what you find with `craftsman review add` (one finding per root cause, listing all
consequences in `--scenario`; fold unjudged duplicates from other lenses before verification with
`craftsman review merge`) and
what you examined with `craftsman review cover`. Coverage notes name each hunk id and what you
tried on it, e.g.
`H3 parse_cfg: x=None -> L42 TypeError, empty string -> empty dict; callers cli.py:19, api.py:88`.

---

## `intent` — Intent & completeness

**Goal:** the change does everything it claims, and nothing it does not claim.

**Read first:** the task statement and commit messages in the packet, then the whole diff once,
top to bottom, before any other lens.

**Probes**
1. State the purpose of the change in one sentence. List each observable behavior that must be
   true after the change (3–8 claims). For every claim, name the line that makes it true. A claim
   with no line is an *incompleteness* finding. Qualifiers in the task count as claims of their
   own: "cache *with TTL*", "*atomic* write", "*idempotent* retry", "*bulk* import" each need a
   line that implements the qualifier, not only the noun.
2. Partial propagation: when the change alters how one place does something (a renamed key, a new
   parameter, a new enum value, a new platform, a new file location), search the repository for
   every *other* place that does the same thing — handlers, call sites, serializers, docs, CLI
   help, config schemas, install/export scripts, manifests, tests, fixtures — and check each one.
3. Unclaimed behavior changes: anything the diff changes that the task did not ask for (changed
   defaults, reordered operations, widened permissions, removed output). Each is either a finding
   or must be justified by the task.
4. Leftovers: debug output, commented-out code, TODOs the change was meant to resolve, temporary
   flags left on, placeholder values.
5. Root cause: follow the bad input or state that the change handles back to where it is created.
   List the other consumers of that origin. If any of them still receives the same unhandled value,
   the fix sits too far downstream — report it.

## `contracts` — Inputs, boundaries & contracts

**Goal:** every changed function behaves correctly for the whole domain of inputs it can
actually receive.

**Read first:** the full enclosing scope of each hunk (the packet shows it) and the call sites
listed under "References".

**Probes**
1. Write the contract of each changed function in one line: what it requires, what it returns or
   guarantees, what it raises. Check every branch against it — including untouched branches that
   now receive different data.
2. For each parameter, substitute the values that match its type and note the outcome:
   - **numbers:** `0`, `-1`, `limit - 1`, `limit`, `limit + 1`, very large, and for floats `NaN`
     and `inf` — every ordered comparison with `NaN` is false, so `if x < 0 or x > MAX: raise`
     lets `NaN` through;
   - **strings and paths:** `""`, whitespace only, non-ASCII, embedded newline or `\0`, different
     case, leading `/` or `..`, trailing separator;
   - **collections:** empty, one element, duplicates, unsorted, very large;
   - **optional values:** `None`/`null`/`undefined`, a missing key or attribute;
   - **lifecycle:** target does not exist, already exists, the same call made twice in a row.
3. Boundary arithmetic: `<` vs `<=`, inclusive vs exclusive ranges, slice ends, `len(x) - 1`, the
   last partial page or batch, integer division and its rounding direction, loops starting at 1.
4. Truthiness and equality: a legitimate `0`, `0.0`, `""`, `False` or empty collection treated as
   "missing"; equality vs identity; float equality; comparing different types (`"1"` vs `1`,
   `str` vs `bytes`, naive vs aware datetimes); unnormalized paths, URLs or Unicode compared as
   strings.
5. Wrong variable: in every assignment, comparison or argument list that resembles a neighbor
   (copy-pasted lines, symmetric pairs like width/height, src/dst, old/new), check each side
   refers to the intended variable — `a.x == b.x and a.y == a.y` compares a value with itself.
   Check that inner bindings (loop variables, comprehension variables, `except ... as e`, `:=`,
   Go `:=` in an inner block) do not shadow an outer name that is read later.
6. Every path returns the same kind of value: look for branches that fall off the end (implicit
   `None`), return a half-built object, or return a different type.
7. Defaults: mutable default arguments; defaults whose meaning changed; parameters inserted before
   existing ones, breaking positional callers. On partial-update paths (upserts, imports, patches,
   config merges — not creates or full replacements), a field the input omits or leaves blank must
   keep the stored value: coercing it to a type default (`0`, `""`, `False`) before the merge
   silently overwrites data the caller never meant to change.
8. Parsing and patterns: regular expressions that must match the whole input use full-match
   semantics (`fullmatch`, `^...$` with the right multiline flags, `\A...\Z`); metacharacters
   from data are escaped; what is written can be read back (round trip).

## `failure` — Failure paths & error handling

**Goal:** when something goes wrong, the program fails loudly, precisely, and without leaving a
mess.

**Read first:** every call in the hunk that can fail: I/O, parsing, network, subprocess,
database, lookups, type conversions, external APIs.

**Probes**
1. For each call that can fail, write the trace `call -> handler -> outcome`. Is the error handled
   at a level that can do something useful? Is the user or caller told?
2. Swallowed errors: broad handlers (`except Exception`, `catch (e) {}`, `.catch(() => {})`) that
   also catch programming errors; handlers that log and continue as if the operation succeeded;
   fallbacks that return `None`, `0`, `[]`, `{}` or `True` so a failure looks like a valid result.
3. Partial effects outside the process: what has already been written, sent, charged or committed
   when the failure happens? Is it rolled back, made atomic (temp file + rename, transaction), or
   reported? Is cleanup in `finally`/`defer`/a context manager so it runs on every path?
4. Partial effects inside the process: an object whose fields were updated before the failing
   step (`self.connected = True` then the handshake raises) is left in a state its methods do not
   expect, and the caller may keep using it.
5. Retries and repetition: is the retried operation idempotent? A retried POST, payment, email or
   queue publish can happen twice. Is there a bound, a backoff, and a distinction between
   retryable errors (timeouts, 503) and permanent ones (400, 401, 404, validation)?
6. Subprocesses and external tools: exit code checked, stderr captured, timeout set, output
   decoded with an explicit encoding and error policy, missing executable handled. Where a
   function has its own error type (documented, or the one its callers catch), every operational
   failure of the calls it wraps — timeout, missing executable, decode error, not only a non-zero
   exit — is translated into it; the fix is to name those exceptions, not to add `except
   Exception`.
7. Error messages contain what a person needs to act (which file, value, limit, id) and no
   secrets.
8. The failure is visible to the outside: a CLI that prints an error and exits 0, an API that
   returns 200 with an error body, or a guard that allows the action because its own check crashed
   (fail-open) is a finding.

## `ripple` — Ripple effects on callers & removed behavior

**Goal:** nothing outside the diff silently breaks.

**Read first:** the "References" section of the packet for every changed symbol, and every
removed (`-`) line in your hunks.

**Probes**
1. For each changed signature, return shape, raised exception, default, side effect, file format,
   CLI flag, environment variable or public name: open every reference and confirm the caller
   still works. The usual victims call positionally, unpack tuples, catch a specific exception
   type, compare against old values, or parse the output.
2. Deletion audit: for every removed line, write down the guarantee it provided (a check, a
   default, an error path, an ordering, a lock, a log, an `fsync`). Find where that guarantee
   lives now. Nowhere, with nothing making it unnecessary, is a finding — and so is a docstring
   that still promises it.
3. Inheritance and interfaces: when a changed method overrides or implements one from a base
   class, interface or protocol, check it still honors the base contract (arguments, return type,
   exceptions, side effects) and that subclasses overriding it are still consistent. Mutable
   values defined at class level (`items = []`) are shared by every instance.
4. Persisted and exchanged data: state files, caches, config, database rows or messages written
   by the previous version — can the new version still read them? Can an older peer read what the
   new version writes?
5. Ordering and registration: import-time side effects, initialization order, plugin or hook
   registration, middleware order — did the change move something across one of these lines?
6. Moved or extracted code: compare it with the original line by line. Lost guards, lost `else`
   branches, a different variable captured, a narrower lock or `try` scope.

## `state` — State, data flow, resources & concurrency

**Goal:** values flow where they should, shared things stay consistent, resources are released.

**Probes**
1. Mutation: list every in-place mutation in the hunk (`.append`, `.pop`, `.update`, `.sort`,
   `del`, `x[k] = v`, `x.attr = v`). Trace each mutated object to its origin. If it came from a
   parameter, a cache, a class or module attribute, a default value, or a *shallow* copy of a
   nested structure (`dict(d)`, `d.copy()`, `[:]`, `{**d}`), check what callers and later calls
   now observe.
2. Caches and memoization: the key includes every input that affects the result (new parameters
   added by the change are the classic miss); entries are invalidated when the underlying data
   changes; unbounded caches have a limit.
3. Resource lifetime: files, sockets, processes, locks, temporary directories, database
   connections — released on every path, including exceptions and early returns.
4. Check-then-act: `exists()` then `open()`, read-modify-write of shared data, "if not in dict then
   insert". If the read and the write are not under the same lock or transaction, two concurrent
   callers both pass the check. Splitting one critical section into two is a common refactoring
   bug.
5. Holding while waiting: a lock, transaction or connection held across network, subprocess or
   user I/O blocks everyone else and can deadlock; a transaction that commits after an external
   side effect (charge, email) can fail after the side effect already happened.
6. Async and threads: missing `await`; async callbacks passed to APIs that do not await them
   (`forEach`, `map` without `Promise.all`, event emitters); blocking calls inside async code;
   unbounded fan-out (`gather`/`Promise.all` over user-sized lists); cancellation or timeouts that
   are caught and ignored; goroutines or tasks that can block forever on a channel nobody reads.
7. Iteration: mutating a collection while iterating it; iterating a generator or stream twice;
   closures in a loop capturing the loop variable (late binding).
8. Time: naive and aware datetimes mixed; wall-clock time (`time.time()`, `Date.now()`) used for
   durations or rate windows instead of a monotonic clock; timeout units (ms vs s); DST and
   end-of-month arithmetic.
9. Generated names and ids that must be unique (output files, keys, ids — not display
   timestamps): if they come from a timestamp, counter or hash, can two runs produce the same one
   (two jobs within the clock's resolution, the same input twice, concurrent processes)? Without
   a disambiguator or exclusive creation (`O_EXCL`, mode `"x"`), the second silently overwrites the
   first.

## `trust` — Trust boundaries & security

**Goal:** data from outside the trust boundary cannot make the program do something its owner
did not intend.

**Probes**
1. List the sources of untrusted data reaching the changed code: request parameters, CLI
   arguments, environment, files a user or a cloned repository controls, network responses, hook
   or webhook payloads, model output, database content written by users, archive entries.
2. Follow each source to the sinks it reaches: shell commands, SQL and other query languages,
   filesystem paths, deserializers, templates/HTML, regular expressions, outgoing URLs,
   redirects, logs. A source reaching a sink without validation or a safe API is a finding; name
   the CWE when you are sure.
3. Paths specifically: `os.path.join`/`Path /` with an absolute second argument discards the base;
   `..` checks that split on `/` miss `\` on Windows; symlinks escape a directory; the only safe
   pattern is resolve-then-verify-containment against the intended root.
4. Authorization: for every new or changed entry point, cite (a) the line that authenticates the
   caller and (b) the line that checks the caller may act on *this specific* resource (owner,
   tenant, role). A missing (b) is the most common access-control bug. Request fields bound
   directly to model or config attributes need an allow-list.
5. Secrets: not logged, not written to files with default permissions, not passed on command lines
   visible to other users, not committed, not echoed in error messages.
6. Insecure defaults: permissive file modes, disabled TLS or signature verification, predictable
   temporary file names, `shell=True` with interpolated strings, `yaml.load`/`pickle` on untrusted
   input.
7. Resource exhaustion from outside: unbounded reads, uploads, decompression, regex backtracking,
   loops or allocations sized by a request value; logging or metric labels built from
   attacker-controlled values (unbounded cardinality).

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
   asserting on the mock that replaced the code under test.
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

## `platform` — Environment & portability

**Goal:** the change works on every platform and environment the project claims to support.

**Probes**
1. Paths: separators, drive letters, case-insensitive filesystems, spaces and non-ASCII in paths,
   `~` expansion, long paths, home and config directory resolution on each OS.
2. Text: default encodings (Windows is often not UTF-8), line endings, BOMs, locale-dependent
   number and date formatting.
3. Processes: shell differences (PowerShell, cmd, bash, zsh), quoting, executable lookup (`.exe`,
   `.cmd` shims, shebangs, PATH), signals, exit codes.
4. Versions: syntax or library features newer than the minimum version the project declares
   (check `pyproject.toml`, `package.json` engines, `go.mod`, CI matrix).
5. Environment assumptions: variables that may be unset, tools that may be missing, no network,
   read-only filesystem, running in CI or a container, running as a different user.

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

## `craft` — Clarity, reuse & simplicity

**Goal:** the change is as simple as the problem allows and consistent with the codebase. Only
record items with a concrete cost (a likely future bug, duplicated maintenance, measurable waste).

**Probes**
1. Reuse: search for an existing helper, constant or pattern that does what the new code
   reimplements (search by behavior and likely names, not just the exact name). Name it.
2. Needless complexity: derivable state, single-use indirection, flags that are always the same
   value, nesting that guard clauses would flatten, copy-pasted blocks that differ in one value.
3. Dead weight: unused variables, imports, parameters, unreachable branches, code kept "just in
   case".
4. Honest names and comments: each name says what the thing is; every comment and docstring in
   the touched area still describes the code *after* the change. Test names state the behavior
   under test (`test_<subject>_<when>_<then>`) without version numbers, plan IDs, or case counters.
5. One question, one function: two functions must not answer the same domain question with
   slightly different callers; domain concepts with a fixed shape use explicit types (dataclasses,
   NamedTuples, TypedDicts) rather than plain `dict`s.
6. Wasted work: repeated I/O or computation in a loop, eager work that is rarely needed,
   independent sequential calls, quadratic behavior on inputs that can be large, logging inside hot
   loops.
7. Consistency: error handling, logging, naming and layout match the surrounding code and the
   project's convention files.

## `design` — Design & size: the change as a whole

Run by whichever reviewer is assigned `design` (a shard reviewer, the single `standard`
reviewer, or the `R4` group on `deep`), before `sweep`. Coverage is recorded per changed file (`craftsman review cover --lens design --files path/a.py,path/b.py --note "..."`),
not per hunk.

**Probes**
1. Size vs. task: how many production lines were added, and how many does the task actually
   require? List three things that could be deleted without losing required behavior.
2. Duplicate questions: do two functions in the change answer the same question?
3. Domain types vs. dicts: are fixed-shape domain values represented as explicit types
   (dataclasses, NamedTuples, TypedDicts) rather than plain `dict`s?
4. Honest names: do symbol and test names state what the thing is or what behavior the test
   verifies, without references to plan IDs, version numbers, or counters?
5. Soundness of the model: is the underlying model sound, or are findings patching symptoms of
   the wrong model (for example, per-tool option tables instead of path candidates vs. forbidden
   zones)?
6. Quality signals: for every quality signal reported for this file, verify that there is either
   a finding (`--signal <ID>`) or a concrete `ack` justification.

## `sweep` — Final sweep: interactions between hunks

Run after all other lenses, by a reviewer who did none of them (or in review-only mode without
`--fix`, by `craftsman-verifier` in Step 0 before issuing verdicts). Read the whole diff top to
bottom once more and look only for what single-lens passes miss:
- two hunks that are each correct but disagree (one writes a format the other does not read; one
  adds a step the other skips; one renames, the other still uses the old name);
- the same decision implemented differently in two places;
- a fix for one finding that re-opened another;
- configuration, documentation and code that now contradict each other;
- **root-cause deduplication:** check `craftsman review show` for unjudged findings recorded by
  earlier lenses that stem from the same root cause (the same changed line or decision producing
  multiple symptoms), and fold them into one finding before verification with `craftsman review
  merge <target_id> <dup_id> ...` (`merge` only works on findings without a verdict) so all
  consequences are listed on `<target_id>`:
  > Merge only when ONE fix removes every consequence. Different fixes are separate findings, even on the same line, file, or category.

  For example, a CLI command missing a documented `--format` option and a stale README example are two
  separate findings, not one "housekeeping" finding.
