"""Job persistence store."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import Job, QueueError


class JobStore:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self.next_seq: int = 1
        self._token_seq: int = 0

    def add(self, job: Job) -> None:
        if job.job_id in self._jobs:
            raise QueueError(f"duplicate job_id: {job.job_id}")
        self._jobs[job.job_id] = job

    def get(self, job_id: str) -> Job:
        if job_id not in self._jobs:
            raise QueueError(f"unknown job_id: {job_id}")
        return self._jobs[job_id]

    def all_jobs(self) -> list[Job]:
        return list(self._jobs.values())

    def allocate_seq(self) -> int:
        seq = self.next_seq
        self.next_seq += 1
        return seq

    def allocate_token(self, job_id: str) -> str:
        self._token_seq += 1
        return f"lease-{self._token_seq}-{job_id}"

    def save(self, path: Path) -> None:
        data: dict[str, Any] = {
            "jobs": [
                {
                    "job_id": j.job_id,
                    "payload": j.payload,
                    "priority": j.priority,
                    "available_at": j.available_at,
                    "seq": j.seq,
                    "status": j.status,
                    "attempts": j.attempts,
                    "max_attempts": j.max_attempts,
                    "last_error": j.last_error,
                    "result": j.result,
                }
                for j in self._jobs.values()
            ]
        }
        path.write_text(json.dumps(data, indent=2), encoding="utf-8", newline="\n")

    @classmethod
    def load(cls, path: Path) -> JobStore:
        raw = json.loads(path.read_text(encoding="utf-8"))
        store = cls()
        for entry in raw.get("jobs", []):
            job = Job(
                job_id=entry["job_id"],
                payload=entry["payload"],
                priority=entry.get("priority", 5),
                available_at=entry.get("available_at", 0.0),
                seq=entry.get("seq", 0),
                status=entry.get("status", "pending"),
                attempts=entry.get("attempts", 0),
                max_attempts=entry.get("max_attempts", 3),
                last_error=entry.get("last_error"),
                result=entry.get("result"),
            )
            store._jobs[job.job_id] = job
        store.next_seq = len(store._jobs) + 1
        return store
