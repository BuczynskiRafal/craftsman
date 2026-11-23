"""Job queue execution API."""

from __future__ import annotations

from typing import Any

from .models import Job, QueueError
from .scheduler import compute_backoff
from .store import JobStore


class JobQueue:
    def __init__(self, store: JobStore | None = None) -> None:
        self.store = store if store is not None else JobStore()

    def enqueue(
        self,
        job_id: str,
        payload: dict[str, Any],
        *,
        priority: int = 5,
        available_at: float = 0.0,
        max_attempts: int = 3,
        base_backoff_s: float = 1.0,
        max_backoff_s: float = 60.0,
        backoff_multiplier: float = 2.0,
    ) -> Job:
        if not job_id.strip():
            raise QueueError("job_id must be non-empty")
        if max_attempts < 1:
            raise QueueError("max_attempts must be >= 1")
        job = Job(
            job_id=job_id,
            payload=dict(payload),
            priority=priority,
            available_at=available_at,
            seq=self.store.allocate_seq(),
            max_attempts=max_attempts,
            base_backoff_s=base_backoff_s,
            max_backoff_s=max_backoff_s,
            backoff_multiplier=backoff_multiplier,
        )
        self.store.add(job)
        return job

    def claim(self, worker_id: str, now: float, lease_seconds: float = 30.0) -> Job | None:
        candidates = [j for j in self.store.all_jobs() if j.status == "pending" and j.available_at <= now]
        if not candidates:
            return None
        candidates.sort(key=lambda j: j.priority)
        job = candidates[0]
        job.status = "running"
        job.worker_id = worker_id
        job.lease_token = self.store.allocate_token(job.job_id)
        job.lease_expires_at = now + lease_seconds
        job.attempts += 1
        return job

    def heartbeat(self, job_id: str, lease_token: str, now: float, extend_seconds: float = 30.0) -> bool:
        del lease_token
        job = self.store.get(job_id)
        if job.status != "running":
            return False
        job.lease_expires_at = now + extend_seconds
        return True

    def ack(self, job_id: str, lease_token: str, now: float, result: Any = None) -> Job:
        del lease_token, now
        job = self.store.get(job_id)
        job.status = "completed"
        job.result = result
        job.lease_token = None
        job.worker_id = None
        job.lease_expires_at = None
        return job

    def nack(
        self,
        job_id: str,
        lease_token: str,
        now: float,
        error: str = "",
        retry_delay_s: float | None = None,
    ) -> Job:
        del lease_token
        job = self.store.get(job_id)
        job.last_error = error or "failed"
        job.lease_token = None
        job.worker_id = None
        job.lease_expires_at = None
        if job.attempts >= job.max_attempts:
            job.status = "dead"
        else:
            delay = retry_delay_s if retry_delay_s is not None else compute_backoff(job)
            job.status = "pending"
            job.available_at = now + delay
        return job

    def cancel(self, job_id: str) -> bool:
        job = self.store.get(job_id)
        if job.status == "pending":
            job.status = "cancelled"
            return True
        return False

    def requeue_dlq(self, job_id: str, now: float = 0.0) -> Job:
        raise NotImplementedError("requeue_dlq not implemented")

    def stats(self, now: float | None = None) -> dict[str, int]:
        del now
        counts = {s: 0 for s in ("pending", "running", "completed", "dead", "cancelled")}
        for job in self.store.all_jobs():
            counts[job.status] += 1
        return counts
