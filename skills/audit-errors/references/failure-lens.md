# Craftsman Review Lenses

A lens is one narrow way of looking at a change: a goal, what to read first, and numbered probes.
Answer every probe for every hunk in your slice. If you cannot answer one from what you have read,
open more files before moving on.

How to use a probe: pick a concrete value or situation and *execute the code in your head*, line
by line, writing the intermediate values down. Most real defects surface at the moment a reviewer
substitutes a specific value instead of reading the code as prose.

---

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
