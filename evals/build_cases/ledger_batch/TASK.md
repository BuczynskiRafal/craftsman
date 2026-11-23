# Task: Complete Double-Entry Accounting & Idempotent Batch Engine

Complete the implementation of `ledger_batch` so that multi-entry batches, idempotency guarantees, reversing entries, and trial balance reporting satisfy every requirement below, while keeping all existing tests in `tests/test_starter.py` passing.

## Requirements

1. **Integer Cents & Validation (`accounts.py`, `journal.py`)**
   - Monetary amounts (`amount_cents`) must be strictly positive `int` values (reject `bool`, `float`, `str`, `<= 0` with `LedgerError`).
   - Account normal balances:
     - `asset` and `expense` have debit-normal balances (`balance_cents = debits - credits`).
     - `liability`, `equity`, and `revenue` have credit-normal balances (`balance_cents = credits - debits`).
   - Every journal entry must have:
     - non-empty unique `entry_id` not previously posted (and not duplicated within the same batch),
     - at least 2 posting lines,
     - every referenced account exists and has `active == True`,
     - every posting line's `currency` equals its account's `currency`,
     - per-currency sum of debit amounts equals sum of credit amounts across the entry's lines (`debits[ccy] == credits[ccy]` for every currency present).

2. **Atomic Batch Execution (`batch.py`)**
   - `BatchProcessor.post_batch(batch_id: str, entries: Sequence[JournalEntry], idempotency_key: str | None = None) -> BatchResult`
   - When `entries` is empty, raise `LedgerError`.
   - Batch execution must be **all-or-nothing (atomic)**:
     - Entries within the batch are evaluated in sequence, and balances must update across entries within the same batch.
     - No entry in the batch may leave any `allow_negative=False` account with `balance_cents < 0` at the end of that entry (note: an earlier entry in the batch may fund an account that a later entry in the same batch draws down, and vice versa).
     - If ANY entry fails any check (duplicate `entry_id`, unknown/inactive account, currency mismatch, unbalanced debits/credits, invalid amount, or `allow_negative=False` going `< 0`), the entire batch must fail with `LedgerError` (or appropriate subclass), rolling back ALL balance changes and journal appends from earlier entries in that batch.
     - A failed batch must NOT record or consume its `idempotency_key`, so a corrected retry using the same key can succeed.

3. **Idempotency Replay vs. Conflict (`batch.py`)**
   - When `idempotency_key` is provided and already succeeded:
     - Compute a canonical deterministic digest over the batch `entries` payload (entry ids, memos, ordered lines with `account_code`, `side`, `amount_cents`, `currency`). Idempotency compares this canonical entries payload and ignores `batch_id`, so retrying with the same `idempotency_key` and identical entries replays the committed result even if `batch_id` differs.
     - If the canonical payload matches the committed batch for that key, return a `BatchResult` with `replayed=True` and the original `batch_id` / `posted_entry_ids` / `entry_count` WITHOUT re-posting or altering balances.
     - If the canonical payload differs from the committed batch for that key, raise `IdempotencyConflictError` (a subclass of `LedgerError`) WITHOUT mutating state.

4. **Reversing Entries (`journal.py`, `batch.py` / `JournalLedger.reverse_entry`)**
   - `JournalLedger.reverse_entry(original_entry_id: str, reverse_entry_id: str, memo: str = "") -> JournalEntry`
   - Must locate the original entry (raise `LedgerError` if missing).
   - Must reject reversing an entry that has already been reversed (raise `LedgerError`), and reject a `reverse_entry_id` that already exists or is empty.
   - Must reject reversing if any referenced account is now inactive (`active == False`), or if applying the reversal would violate `allow_negative=False` (`balance_cents < 0`).
   - Reverses every line (`"debit"` <-> `"credit"`, keeping `account_code`, `amount_cents`, `currency`), records `reverses=original_entry_id` on the created `JournalEntry`, defaults `memo` to `f"Reversal of {original_entry_id}"` if empty, and updates balances atomically.

5. **Trial Balance Reporting (`reporting.py`)**
   - `trial_balance(ledger: JournalLedger, currency: str) -> TrialBalanceReport`
   - Filter to accounts whose currency equals `currency`.
   - `TrialBalanceReport` contains:
     - `currency`: str
     - `total_debits`: sum of debit postings across all entries in `ledger` for `currency`
     - `total_credits`: sum of credit postings across all entries in `ledger` for `currency`
     - `accounts`: `{account_code: account.balance_cents}` for all accounts in `currency`
     - `net_by_type`: `{account_type: sum of account.balance_cents for accounts of that type in currency}` (all 5 account types present, defaulting to `0`)
   - Note: `total_debits == total_credits` and `accounts_payable_or_revenue` etc. must preserve normal-balance signs cleanly.
