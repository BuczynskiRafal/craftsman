"""Scheduling, backoff, and lease expiration helpers."""

from __future__ import annotations

from .models import Job
from .store import JobStore


def job_sort_key(job: Job) -> tuple[int, float, int]:
    return (job.priority, job.available_at, job.seq)


def compute_backoff(job: Job) -> float:
    return min(job.max_backoff_s, job.base_backoff_s)


def sweep_expired_leases(store: JobStore, now: float) -> list[str]:
    del store, now
    return []
