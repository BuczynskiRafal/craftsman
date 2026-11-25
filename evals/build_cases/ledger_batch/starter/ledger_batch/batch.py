"""Batch processing and idempotency support."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .accounts import LedgerError
from .journal import JournalEntry, JournalLedger


class IdempotencyConflictError(LedgerError):
    """Raised when an idempotency key is reused with a different batch payload."""


@dataclass(frozen=True)
class BatchResult:
    batch_id: str
    entry_count: int
    posted_entry_ids: tuple[str, ...]
    replayed: bool = False


class BatchProcessor:
    def __init__(self, ledger: JournalLedger) -> None:
        self.ledger = ledger
        self._idempotent_results: dict[str, BatchResult] = {}

    def post_batch(
        self,
        batch_id: str,
        entries: Sequence[JournalEntry],
        idempotency_key: str | None = None,
    ) -> BatchResult:
        if not batch_id.strip():
            raise LedgerError("batch_id must be non-empty")
        if not entries:
            raise LedgerError("batch must contain at least one entry")
        if idempotency_key is not None and idempotency_key in self._idempotent_results:
            prev = self._idempotent_results[idempotency_key]
            return BatchResult(prev.batch_id, prev.entry_count, prev.posted_entry_ids, replayed=True)

        posted: list[str] = []
        for entry in entries:
            self.ledger.post_entry(entry)
            posted.append(entry.entry_id)

        result = BatchResult(batch_id=batch_id, entry_count=len(posted), posted_entry_ids=tuple(posted), replayed=False)
        if idempotency_key is not None:
            self._idempotent_results[idempotency_key] = result
        return result
