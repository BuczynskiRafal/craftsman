"""Starter sanity tests for ledger_batch."""

import unittest

from ledger_batch import (
    Account,
    BatchProcessor,
    ChartOfAccounts,
    JournalEntry,
    JournalLedger,
    LedgerError,
    PostingLine,
)


class StarterLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.chart = ChartOfAccounts()
        self.chart.register(Account("cash", "Cash", "asset", "USD"))
        self.chart.register(Account("rev", "Revenue", "revenue", "USD"))
        self.ledger = JournalLedger(self.chart)
        self.processor = BatchProcessor(self.ledger)

    def test_single_entry_batch_posts_and_balances(self) -> None:
        entry = JournalEntry(
            "E1",
            (
                PostingLine("cash", "debit", 1500, "USD"),
                PostingLine("rev", "credit", 1500, "USD"),
            ),
            memo="Sale",
        )
        res = self.processor.post_batch("B1", [entry])
        self.assertEqual(res.entry_count, 1)
        self.assertFalse(res.replayed)
        self.assertEqual(self.chart.get("cash").balance_cents, 1500)
        self.assertEqual(self.chart.get("rev").balance_cents, 1500)

    def test_unbalanced_entry_raises(self) -> None:
        entry = JournalEntry(
            "E_BAD",
            (
                PostingLine("cash", "debit", 1000, "USD"),
                PostingLine("rev", "credit", 900, "USD"),
            ),
        )
        with self.assertRaises(LedgerError):
            self.processor.post_batch("B_BAD", [entry])


if __name__ == "__main__":
    unittest.main()
