"""Domain models and errors for jobqueue_retry."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

JobStatus = Literal["pending", "running", "completed", "dead", "cancelled"]


class QueueError(Exception):
    """Base error raised for queue configuration or state violations."""


class StaleLeaseError(QueueError):
    """Raised when a worker presents an expired or superseded lease token."""


@dataclass
class Job:
    job_id: str
    payload: dict[str, Any]
    priority: int = 5
    available_at: float = 0.0
    seq: int = 0
    status: JobStatus = "pending"
    attempts: int = 0
    max_attempts: int = 3
    base_backoff_s: float = 1.0
    max_backoff_s: float = 60.0
    backoff_multiplier: float = 2.0
    lease_token: str | None = None
    worker_id: str | None = None
    lease_expires_at: float | None = None
    cancel_requested: bool = False
    last_error: str | None = None
    result: Any = None
