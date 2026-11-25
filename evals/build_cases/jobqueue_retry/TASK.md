# Task: Complete Durable Priority Job Queue with Leases, Backoff, Cancellation & Persistence

Complete `jobqueue_retry` so that scheduling, lease expiration, stale-worker protection, exponential backoff, DLQ handling, cancellation, and JSON persistence satisfy every requirement below while keeping `tests/test_starter.py` passing.

## Requirements

1. **Enqueue & Priority Ordering (`models.py`, `scheduler.py`, `worker.py`)**
   - `JobQueue.enqueue(job_id, payload, *, priority=5, available_at=0.0, max_attempts=3, base_backoff_s=1.0, max_backoff_s=60.0, backoff_multiplier=2.0) -> Job`
   - Validate non-empty unique `job_id`, `max_attempts >= 1`, `base_backoff_s >= 0`, `max_backoff_s >= 0`, `backoff_multiplier >= 1.0`. Raise `QueueError` on invalid input or duplicate `job_id`.
   - Assign strictly monotonic integer `seq` starting from `1` (`1, 2, 3, ...`) tracked in the store (`next_seq`).
   - Eligible `pending` jobs (`available_at <= now`) are claimed in ascending order of `(priority, available_at, seq)` (smaller `priority` first; earlier `available_at` breaks ties; lower `seq` breaks ties).

2. **Leases, Expiry Sweep & Stale Worker Protection (`scheduler.py`, `worker.py`)**
   - `sweep_expired_leases(store, now) -> list[str]` scans all `running` jobs where `lease_expires_at is not None and now >= lease_expires_at`:
     - Clear `lease_token`, `worker_id`, `lease_expires_at`.
     - If `job.cancel_requested` is `True`: set `status = "cancelled"`, `cancel_requested = False`.
     - Else if `job.attempts >= job.max_attempts`: set `status = "dead"`, `last_error = "lease expired"`.
     - Else: set `status = "pending"`, `available_at = now + compute_backoff(job)`, `last_error = "lease expired"`.
     - Return the sorted or ordered list of transitioned `job_id`s.
   - `claim(worker_id, now, lease_seconds=30.0) -> Job | None`:
     - First run `sweep_expired_leases(self.store, now)` so expired leases that still have attempts left (and were not cancelled) become reclaimable immediately!
     - On claiming: set `status = "running"`, `worker_id`, generate a unique `lease_token`, set `lease_expires_at = now + lease_seconds`, and increment `attempts += 1`.
   - `stats(now: float | None = None) -> dict[str, int]`: if `now is not None`, run `sweep_expired_leases(self.store, now)` first, then return counts for all 5 statuses (`pending`, `running`, `completed`, `dead`, `cancelled`).
   - `ack`, `nack`, and `heartbeat` MUST validate:
     - job exists (raise `QueueError` if missing),
     - `job.status == "running"`, `job.lease_token == lease_token`, and `job.lease_expires_at is not None and now < job.lease_expires_at`.
     - If status is not `"running"`, `lease_token` mismatches (including after expiry + reclaim by another worker!), or `now >= job.lease_expires_at`, raise `StaleLeaseError` WITHOUT modifying the job!

3. **Exponential Backoff & Dead-Letter Queue (`scheduler.py`, `worker.py`)**
   - `compute_backoff(job)` after attempt #`k` (`job.attempts == k`, 1-based):
     `min(job.max_backoff_s, job.base_backoff_s * (job.backoff_multiplier ** max(0, job.attempts - 1)))`.
   - `nack(job_id, lease_token, now, error: str = "", retry_delay_s: float | None = None) -> Job`:
     - Validate lease (raising `StaleLeaseError` if stale/expired).
     - Clear lease fields (`lease_token`, `worker_id`, `lease_expires_at`), set `last_error = error or "failed"`.
     - If `job.cancel_requested` is `True`: set `status = "cancelled"`, `cancel_requested = False`.
     - Else if `job.attempts >= job.max_attempts`: set `status = "dead"`.
     - Else: `delay = retry_delay_s if retry_delay_s is not None else compute_backoff(job)`, set `status = "pending"`, `available_at = now + max(0.0, delay)`.
   - `requeue_dlq(job_id: str, now: float = 0.0) -> Job`:
     - Raises `QueueError` if job missing or `status != "dead"`.
     - Resets `status = "pending"`, `attempts = 0`, `available_at = now`, `last_error = None`, `cancel_requested = False`, lease fields `None`.

4. **Cancellation (`worker.py`)**
   - `cancel(job_id: str) -> bool`:
     - Raise `QueueError` if job is missing.
     - If `status == "pending"`: set `status = "cancelled"`, return `True`.
     - If `status == "running"`: set `cancel_requested = True`, return `True`.
     - If `status in ("completed", "dead", "cancelled")`: return `False`.
   - `heartbeat(job_id, lease_token, now, extend_seconds=30.0) -> bool`:
     - Validates active lease (raises `StaleLeaseError` if expired or token mismatches).
     - Extends `lease_expires_at = now + extend_seconds`.
     - Returns `not job.cancel_requested` (`False` signals the worker to abort cleanly).
   - `ack(job_id, lease_token, now, result=None) -> Job`:
     - Validates active lease (raises `StaleLeaseError` if expired or token mismatches).
     - Clears lease fields.
     - If `job.cancel_requested` is `True`: set `status = "cancelled"`, `cancel_requested = False`, `result = None`.
     - Else: set `status = "completed"`, `result = result`.

5. **Durable JSON Persistence (`store.py`)**
   - `JobStore.save(path)` and `JobStore.load(path)` must serialize and restore every `Job` field (`seq`, retry parameters, lease fields, `cancel_requested`, `last_error`, `result`) plus `next_seq` and token sequence counter so newly enqueued jobs after reload strictly continue increasing `seq` without collisions and preserve active leases/backoff behavior.
