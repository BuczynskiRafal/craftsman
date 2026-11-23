"""Journal entries and ledger storage."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Literal

from .accounts import Account, ChartOfAccounts, LedgerError, validate_cents


@dataclass(frozen=True)
class PostingLine:
    account_code: str
    side: Literal["debit", "credit"]
    amount_cents: int
    currency: str


@dataclass(frozen=True)
class JournalEntry:
    entry_id: str
    lines: tuple[PostingLine, ...]
    memo: str = ""
    reverses: str | None = None


@dataclass
class JournalLedger:
    chart: ChartOfAccounts
    entries: list[JournalEntry] = field(default_factory=list)
    _by_id: dict[str, JournalEntry] = field(default_factory=dict, repr=False)

    def post_entry(self, entry: JournalEntry) -> JournalEntry:
        if not entry.entry_id.strip():
            raise LedgerError("entry_id must be non-empty")
        if entry.entry_id in self._by_id:
            raise LedgerError(f"duplicate entry_id: {entry.entry_id}")
        if len(entry.lines) < 2:
            raise LedgerError("journal entry requires at least two posting lines")

        debits: dict[str, int] = defaultdict(int)
        credits: dict[str, int] = defaultdict(int)
        touched: list[Account] = []
        for line in entry.lines:
            validate_cents(line.amount_cents)
            acct = self.chart.get(line.account_code)
            if not acct.active:
                raise LedgerError(f"inactive account: {acct.code}")
            if line.currency != acct.currency:
                raise LedgerError(f"currency mismatch for {acct.code}: {line.currency} != {acct.currency}")
            if line.side == "debit":
                debits[line.currency] += line.amount_cents
            elif line.side == "credit":
                credits[line.currency] += line.amount_cents
            else:
                raise LedgerError(f"invalid side: {line.side!r}")
            touched.append(acct)

        for ccy in set(debits) | set(credits):
            if debits[ccy] != credits[ccy]:
                raise LedgerError(f"unbalanced entry {entry.entry_id} for {ccy}")

        # NOTE: applies directly without checking post-entry allow_negative across batch or rollback.
        for line in entry.lines:
            self.chart.get(line.account_code).apply(line.side, line.amount_cents)
        for acct in touched:
            if not acct.allow_negative and acct.balance_cents < 0:
                raise LedgerError(f"negative balance not allowed for {acct.code}")

        self.entries.append(entry)
        self._by_id[entry.entry_id] = entry
        return entry

    def reverse_entry(self, original_entry_id: str, reverse_entry_id: str, memo: str = "") -> JournalEntry:
        raise NotImplementedError("reverse_entry is not implemented yet")
