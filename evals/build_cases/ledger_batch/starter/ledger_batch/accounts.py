"""Chart of accounts and balance rules."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

AccountType = Literal["asset", "liability", "equity", "revenue", "expense"]
DEBIT_NORMAL_TYPES: frozenset[str] = frozenset({"asset", "expense"})
CREDIT_NORMAL_TYPES: frozenset[str] = frozenset({"liability", "equity", "revenue"})
VALID_TYPES: frozenset[str] = DEBIT_NORMAL_TYPES | CREDIT_NORMAL_TYPES


class LedgerError(Exception):
    """Base error for accounting domain failures."""


def validate_cents(amount: object, what: str = "amount_cents") -> int:
    if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
        raise LedgerError(f"{what} must be a positive integer")
    return amount


@dataclass
class Account:
    code: str
    name: str
    account_type: AccountType
    currency: str
    allow_negative: bool = False
    active: bool = True
    debits_cents: int = 0
    credits_cents: int = 0

    def __post_init__(self) -> None:
        if not self.code.strip():
            raise LedgerError("account code must be non-empty")
        if self.account_type not in VALID_TYPES:
            raise LedgerError(f"invalid account type: {self.account_type!r}")
        if not self.currency.strip():
            raise LedgerError("currency must be non-empty")

    @property
    def balance_cents(self) -> int:
        if self.account_type in DEBIT_NORMAL_TYPES:
            return self.debits_cents - self.credits_cents
        return self.credits_cents - self.debits_cents

    def apply(self, side: Literal["debit", "credit"], amount_cents: int) -> None:
        validate_cents(amount_cents)
        if side == "debit":
            self.debits_cents += amount_cents
        elif side == "credit":
            self.credits_cents += amount_cents
        else:
            raise LedgerError(f"invalid side: {side!r}")


class ChartOfAccounts:
    def __init__(self) -> None:
        self._accounts: dict[str, Account] = {}

    def register(self, account: Account) -> Account:
        if account.code in self._accounts:
            raise LedgerError(f"duplicate account code: {account.code}")
        self._accounts[account.code] = account
        return account

    def get(self, code: str) -> Account:
        if code not in self._accounts:
            raise LedgerError(f"unknown account: {code}")
        return self._accounts[code]

    def deactivate(self, code: str) -> None:
        self.get(code).active = False

    def all_accounts(self) -> list[Account]:
        return list(self._accounts.values())
