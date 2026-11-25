"""Hidden acceptance tests for ledger_batch."""

import unittest

from ledger_batch import (
    Account,
    BatchProcessor,
    ChartOfAccounts,
    IdempotencyConflictError,
    JournalEntry,
    JournalLedger,
    LedgerError,
    PostingLine,
    trial_balance,
)


def make_system():
    chart = ChartOfAccounts()
    chart.register(Account("cash", "Cash", "asset", "USD"))
    chart.register(Account("bank", "Bank", "asset", "USD"))
    chart.register(Account("ap", "Accounts Payable", "liability", "USD"))
    chart.register(Account("rev", "Revenue", "revenue", "USD"))
    chart.register(Account("exp", "Expense", "expense", "USD"))
    chart.register(Account("eq", "Equity", "equity", "USD"))
    chart.register(Account("eur_cash", "EUR Cash", "asset", "EUR"))
    chart.register(Account("eur_rev", "EUR Rev", "revenue", "EUR"))
    ledger = JournalLedger(chart)
    proc = BatchProcessor(ledger)
    return chart, ledger, proc


class HiddenLedgerBatchTests(unittest.TestCase):
    def test_atomic_rollback_on_later_entry_failure_preserves_balances_and_entries(self) -> None:
        chart, ledger, proc = make_system()
        seed = JournalEntry(
            "E0",
            (PostingLine("cash", "debit", 1000, "USD"), PostingLine("eq", "credit", 1000, "USD")),
        )
        proc.post_batch("B0", [seed])

        e1 = JournalEntry(
            "E1",
            (PostingLine("bank", "debit", 400, "USD"), PostingLine("cash", "credit", 400, "USD")),
        )
        e2 = JournalEntry(
            "E2",
            (PostingLine("exp", "debit", 200, "USD"), PostingLine("cash", "credit", 100, "USD")),
        )
        with self.assertRaises(LedgerError):
            proc.post_batch("B1", [e1, e2])

        self.assertEqual(chart.get("cash").balance_cents, 1000)
        self.assertEqual(chart.get("bank").balance_cents, 0)
        self.assertEqual([e.entry_id for e in ledger.entries], ["E0"])

        # E1 must still be postable afterwards because it rolled back cleanly.
        proc.post_batch("B1_RETRY", [e1])
        self.assertEqual(chart.get("cash").balance_cents, 600)
        self.assertEqual(chart.get("bank").balance_cents, 400)

    def test_atomic_rollback_when_single_entry_violates_allow_negative(self) -> None:
        chart, ledger, proc = make_system()
        overdraw = JournalEntry(
            "E_NEG",
            (PostingLine("exp", "debit", 500, "USD"), PostingLine("cash", "credit", 500, "USD")),
        )
        with self.assertRaises(LedgerError):
            proc.post_batch("B_NEG", [overdraw])

        self.assertEqual(chart.get("exp").balance_cents, 0)
        self.assertEqual(chart.get("cash").balance_cents, 0)
        self.assertEqual(ledger.entries, [])

    def test_allow_negative_within_same_batch_respects_order(self) -> None:
        chart, _, proc = make_system()
        fund = JournalEntry(
            "E1",
            (PostingLine("cash", "debit", 500, "USD"), PostingLine("rev", "credit", 500, "USD")),
        )
        spend = JournalEntry(
            "E2",
            (PostingLine("exp", "debit", 300, "USD"), PostingLine("cash", "credit", 300, "USD")),
        )
        res = proc.post_batch("B1", [fund, spend])
        self.assertEqual(res.entry_count, 2)
        self.assertEqual(chart.get("cash").balance_cents, 200)

        # Spending before funding must fail and leave balances at post-B1 values.
        bad_spend = JournalEntry(
            "E3",
            (PostingLine("exp", "debit", 300, "USD"), PostingLine("cash", "credit", 300, "USD")),
        )
        later_fund = JournalEntry(
            "E4",
            (PostingLine("cash", "debit", 500, "USD"), PostingLine("rev", "credit", 500, "USD")),
        )
        with self.assertRaises(LedgerError):
            proc.post_batch("B2", [bad_spend, later_fund])
        self.assertEqual(chart.get("cash").balance_cents, 200)

    def test_duplicate_entry_id_within_batch_rolls_back(self) -> None:
        chart, ledger, proc = make_system()
        e1 = JournalEntry("DUP", (PostingLine("cash", "debit", 100, "USD"), PostingLine("rev", "credit", 100, "USD")))
        e2 = JournalEntry("DUP", (PostingLine("bank", "debit", 100, "USD"), PostingLine("rev", "credit", 100, "USD")))
        with self.assertRaises(LedgerError):
            proc.post_batch("B_DUP", [e1, e2])
        self.assertEqual(chart.get("cash").balance_cents, 0)
        self.assertEqual(ledger.entries, [])

    def test_idempotency_replay_and_conflict_and_retry_after_failure(self) -> None:
        chart, ledger, proc = make_system()
        bad = [JournalEntry("E1", (PostingLine("cash", "debit", 100, "USD"), PostingLine("rev", "credit", 50, "USD")))]
        with self.assertRaises(LedgerError):
            proc.post_batch("B1", bad, idempotency_key="key-1")

        good = [JournalEntry("E1", (PostingLine("cash", "debit", 100, "USD"), PostingLine("rev", "credit", 100, "USD")))]
        first = proc.post_batch("B1", good, idempotency_key="key-1")
        self.assertFalse(first.replayed)
        self.assertEqual(chart.get("cash").balance_cents, 100)

        replay = proc.post_batch("B1_DUP", good, idempotency_key="key-1")
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.batch_id, "B1")
        self.assertEqual(chart.get("cash").balance_cents, 100)
        self.assertEqual(len(ledger.entries), 1)

        different = [JournalEntry("E1", (PostingLine("cash", "debit", 200, "USD"), PostingLine("rev", "credit", 200, "USD")))]
        with self.assertRaises(IdempotencyConflictError):
            proc.post_batch("B2", different, idempotency_key="key-1")
        self.assertEqual(chart.get("cash").balance_cents, 100)
        self.assertEqual(len(ledger.entries), 1)

    def test_float_bool_and_non_positive_amounts_rejected(self) -> None:
        _, _, proc = make_system()
        for bad_amt in (0, -100, 12.5, True, "100"):
            entry = JournalEntry(
                "E_AMT",
                (PostingLine("cash", "debit", bad_amt, "USD"), PostingLine("rev", "credit", bad_amt, "USD")),  # type: ignore[arg-type]
            )
            with self.assertRaises(LedgerError):
                proc.post_batch("B_AMT", [entry])

    def test_reverse_entry_reverses_balances_and_prevents_double_reversal(self) -> None:
        chart, ledger, proc = make_system()
        proc.post_batch(
            "B1",
            [
                JournalEntry("E1", (PostingLine("cash", "debit", 500, "USD"), PostingLine("rev", "credit", 500, "USD"))),
            ],
        )
        rev_entry = ledger.reverse_entry("E1", "E1_REV")
        self.assertEqual(rev_entry.reverses, "E1")
        self.assertEqual(rev_entry.memo, "Reversal of E1")
        self.assertEqual(chart.get("cash").balance_cents, 0)
        self.assertEqual(chart.get("rev").balance_cents, 0)

        with self.assertRaises(LedgerError):
            ledger.reverse_entry("E1", "E1_REV2")

        with self.assertRaises(LedgerError):
            ledger.reverse_entry("MISSING", "E_M_REV")

    def test_reverse_entry_enforces_negative_balance_and_inactive_accounts(self) -> None:
        chart, ledger, proc = make_system()
        proc.post_batch(
            "B1",
            [
                JournalEntry("E1", (PostingLine("cash", "debit", 500, "USD"), PostingLine("rev", "credit", 500, "USD"))),
                JournalEntry("E2", (PostingLine("exp", "debit", 400, "USD"), PostingLine("cash", "credit", 400, "USD"))),
            ],
        )
        # Cash is now 100; reversing E1 tries to credit cash 500 -> -400 -> rejected cleanly.
        with self.assertRaises(LedgerError):
            ledger.reverse_entry("E1", "E1_REV")
        self.assertEqual(chart.get("cash").balance_cents, 100)
        self.assertEqual(chart.get("rev").balance_cents, 500)

        # Deactivating exp blocks reversing E2.
        chart.deactivate("exp")
        with self.assertRaises(LedgerError):
            ledger.reverse_entry("E2", "E2_REV")
        self.assertEqual(chart.get("cash").balance_cents, 100)

    def test_mixed_currency_entry_must_balance_per_currency(self) -> None:
        chart, _, proc = make_system()
        valid = JournalEntry(
            "E_FX",
            (
                PostingLine("cash", "debit", 1000, "USD"),
                PostingLine("rev", "credit", 1000, "USD"),
                PostingLine("eur_cash", "debit", 900, "EUR"),
                PostingLine("eur_rev", "credit", 900, "EUR"),
            ),
        )
        proc.post_batch("B_FX", [valid])
        self.assertEqual(chart.get("cash").balance_cents, 1000)
        self.assertEqual(chart.get("eur_cash").balance_cents, 900)

        # Unbalanced per currency (even though total sum across lines happens to match debits==credits).
        bad = JournalEntry(
            "E_FX_BAD",
            (
                PostingLine("cash", "debit", 1000, "USD"),
                PostingLine("eur_rev", "credit", 1000, "EUR"),
            ),
        )
        with self.assertRaises(LedgerError):
            proc.post_batch("B_FX_BAD", [bad])

    def test_trial_balance_filters_currency_and_computes_net_by_type(self) -> None:
        _, ledger, proc = make_system()
        proc.post_batch(
            "B1",
            [
                JournalEntry("E1", (PostingLine("cash", "debit", 2500, "USD"), PostingLine("rev", "credit", 2500, "USD"))),
                JournalEntry("E2", (PostingLine("exp", "debit", 700, "USD"), PostingLine("ap", "credit", 700, "USD"))),
                JournalEntry("E3", (PostingLine("eur_cash", "debit", 900, "EUR"), PostingLine("eur_rev", "credit", 900, "EUR"))),
            ],
        )
        rep = trial_balance(ledger, "USD")
        self.assertEqual(rep.currency, "USD")
        self.assertEqual(rep.total_debits, 3200)
        self.assertEqual(rep.total_credits, 3200)
        self.assertNotIn("eur_cash", rep.accounts)
        self.assertEqual(rep.accounts["cash"], 2500)
        self.assertEqual(rep.accounts["rev"], 2500)
        self.assertEqual(rep.accounts["ap"], 700)
        self.assertEqual(rep.net_by_type["asset"], 2500)
        self.assertEqual(rep.net_by_type["expense"], 700)
        self.assertEqual(rep.net_by_type["revenue"], 2500)
        self.assertEqual(rep.net_by_type["liability"], 700)
        self.assertEqual(rep.net_by_type["equity"], 0)


if __name__ == "__main__":
    unittest.main()
