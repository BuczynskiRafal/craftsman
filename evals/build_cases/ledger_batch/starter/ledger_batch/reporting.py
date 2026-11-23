"""Financial reporting helpers."""

from __future__ import annotations

from dataclasses import dataclass

from .accounts import DEBIT_NORMAL_TYPES
from .journal import JournalLedger


@dataclass(frozen=True)
class TrialBalanceReport:
    currency: str
    total_debits: int
    total_credits: int
    accounts: dict[str, int]
    net_by_type: dict[str, int]


def trial_balance(ledger: JournalLedger, currency: str) -> TrialBalanceReport:
    # Starter bug: ignores currency filtering and credit-normal balance rules.
    accounts: dict[str, int] = {}
    total_debits = 0
    total_credits = 0
    for acct in ledger.chart.all_accounts():
        accounts[acct.code] = acct.debits_cents - acct.credits_cents
        total_debits += acct.debits_cents
        total_credits += acct.credits_cents
    net_by_type = {t: 0 for t in (*DEBIT_NORMAL_TYPES, "liability", "equity", "revenue")}
    return TrialBalanceReport(
        currency=currency,
        total_debits=total_debits,
        total_credits=total_credits,
        accounts=accounts,
        net_by_type=net_by_type,
    )
