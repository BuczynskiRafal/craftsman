# Craftsman Review Lenses

A lens is one narrow way of looking at a change: a goal, what to read first, and numbered probes.
Answer every probe for every hunk in your slice. If you cannot answer one from what you have read,
open more files before moving on.

How to use a probe: pick a concrete value or situation and *execute the code in your head*, line
by line, writing the intermediate values down. Most real defects surface at the moment a reviewer
substitutes a specific value instead of reading the code as prose.

---

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
