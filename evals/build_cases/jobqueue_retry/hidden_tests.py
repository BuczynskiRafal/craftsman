"""Hidden acceptance tests for jobqueue_retry."""

import tempfile
import unittest
from pathlib import Path

from jobqueue_retry import (
    JobQueue,
    JobStore,
    QueueError,
    StaleLeaseError,
    sweep_expired_leases,
)


class HiddenJobQueueTests(unittest.TestCase):
    def test_priority_available_at_and_seq_tie_breaking(self) -> None:
        q = JobQueue()
        q.enqueue("p2_later", {}, priority=2, available_at=5.0)
        q.enqueue("p5_early", {}, priority=5, available_at=0.0)
        q.enqueue("p2_early_b", {}, priority=2, available_at=2.0)
        q.enqueue("p2_early_a", {}, priority=2, available_at=1.0)
        q.enqueue("p2_same_t_first", {}, priority=2, available_at=1.0)

        claimed = []
        while (j := q.claim("w1", now=10.0)) is not None:
            claimed.append(j.job_id)
            q.ack(j.job_id, j.lease_token or "", now=10.5)

        self.assertEqual(
            claimed,
            ["p2_early_a", "p2_same_t_first", "p2_early_b", "p2_later", "p5_early"],
        )

    def test_exponential_backoff_and_dlq_boundary(self) -> None:
        q = JobQueue()
        q.enqueue("j1", {"v": 1}, max_attempts=3, base_backoff_s=2.0, max_backoff_s=10.0, backoff_multiplier=3.0)

        j1 = q.claim("w1", now=0.0, lease_seconds=5.0)
        assert j1 is not None and j1.lease_token is not None
        res1 = q.nack("j1", j1.lease_token, now=1.0, error="boom1")
        # Attempt 1 backoff: min(10, 2 * 3^0) = 2.0 -> available_at = 3.0
        self.assertEqual((res1.status, res1.available_at), ("pending", 3.0))
        self.assertIsNone(q.claim("w1", now=2.9))

        j2 = q.claim("w1", now=3.0, lease_seconds=5.0)
        assert j2 is not None and j2.lease_token is not None
        res2 = q.nack("j1", j2.lease_token, now=4.0, error="boom2")
        # Attempt 2 backoff: min(10, 2 * 3^1) = 6.0 -> available_at = 10.0
        self.assertEqual((res2.status, res2.available_at), ("pending", 10.0))

        j3 = q.claim("w1", now=10.0, lease_seconds=5.0)
        assert j3 is not None and j3.lease_token is not None
        res3 = q.nack("j1", j3.lease_token, now=11.0, error="boom3")
        self.assertEqual(res3.status, "dead")
        self.assertEqual(res3.last_error, "boom3")

    def test_stale_lease_after_expiry_and_reclaim_does_not_clobber_new_worker(self) -> None:
        q = JobQueue()
        q.enqueue("j1", {}, max_attempts=3, base_backoff_s=1.0)

        first = q.claim("w1", now=0.0, lease_seconds=5.0)
        assert first is not None and first.lease_token is not None
        old_token = first.lease_token

        # Lease expired at 5.0; claiming at 5.0 must sweep and leave j1 in backoff until 5.0 + 1.0 = 6.0.
        self.assertIsNone(q.claim("w2", now=5.0))
        self.assertEqual(q.store.get("j1").status, "pending")

        second = q.claim("w2", now=6.0, lease_seconds=10.0)
        assert second is not None and second.lease_token is not None
        self.assertNotEqual(second.lease_token, old_token)

        # Old worker w1 wakes up late and tries ack, nack, heartbeat with old_token.
        with self.assertRaises(StaleLeaseError):
            q.ack("j1", old_token, now=7.0, result="stale")
        with self.assertRaises(StaleLeaseError):
            q.nack("j1", old_token, now=7.0, error="stale")
        with self.assertRaises(StaleLeaseError):
            q.heartbeat("j1", old_token, now=7.0, extend_seconds=10.0)

        # New worker w2's running job must be completely untouched.
        current = q.store.get("j1")
        self.assertEqual((current.status, current.worker_id, current.lease_token), ("running", "w2", second.lease_token))

        q.ack("j1", second.lease_token, now=8.0, result="fresh")
        self.assertEqual((q.store.get("j1").status, q.store.get("j1").result), ("completed", "fresh"))

    def test_expired_lease_direct_ack_before_sweep_raises_stale_and_preserves_state(self) -> None:
        q = JobQueue()
        q.enqueue("j1", {})
        claimed = q.claim("w1", now=0.0, lease_seconds=5.0)
        assert claimed is not None and claimed.lease_token is not None
        with self.assertRaises(StaleLeaseError):
            q.ack("j1", claimed.lease_token, now=5.0)
        with self.assertRaises(StaleLeaseError):
            q.heartbeat("j1", claimed.lease_token, now=5.0)

    def test_sweep_moves_exhausted_lease_to_dlq(self) -> None:
        q = JobQueue()
        q.enqueue("j1", {}, max_attempts=1)
        claimed = q.claim("w1", now=0.0, lease_seconds=4.0)
        assert claimed is not None
        swept = sweep_expired_leases(q.store, now=4.0)
        self.assertEqual(swept, ["j1"])
        job = q.store.get("j1")
        self.assertEqual((job.status, job.lease_token, job.last_error), ("dead", None, "lease expired"))

    def test_heartbeat_extends_lease_past_initial_expiry(self) -> None:
        q = JobQueue()
        q.enqueue("j1", {})
        claimed = q.claim("w1", now=0.0, lease_seconds=5.0)
        assert claimed is not None and claimed.lease_token is not None
        ok = q.heartbeat("j1", claimed.lease_token, now=4.0, extend_seconds=6.0)
        self.assertTrue(ok)
        self.assertEqual(sweep_expired_leases(q.store, now=9.0), [])
        done = q.ack("j1", claimed.lease_token, now=9.5, result="ok")
        self.assertEqual(done.status, "completed")

    def test_cancel_pending_running_and_terminal_states(self) -> None:
        q = JobQueue()
        q.enqueue("j_pend", {})
        q.enqueue("j_run_ack", {})
        q.enqueue("j_run_nack", {}, max_attempts=3)
        q.enqueue("j_run_expire", {}, max_attempts=3)

        self.assertTrue(q.cancel("j_pend"))
        self.assertEqual(q.store.get("j_pend").status, "cancelled")
        self.assertFalse(q.cancel("j_pend"))

        c1 = q.claim("w1", now=0.0, lease_seconds=10.0)
        c2 = q.claim("w2", now=0.0, lease_seconds=10.0)
        c3 = q.claim("w3", now=0.0, lease_seconds=10.0)
        assert c1 and c2 and c3 and c1.lease_token and c2.lease_token and c3.lease_token

        self.assertTrue(q.cancel(c1.job_id))
        self.assertTrue(q.cancel(c2.job_id))
        self.assertTrue(q.cancel(c3.job_id))

        self.assertFalse(q.heartbeat(c1.job_id, c1.lease_token, now=2.0))
        res1 = q.ack(c1.job_id, c1.lease_token, now=3.0, result={"should": "drop"})
        self.assertEqual((res1.status, res1.result), ("cancelled", None))

        res2 = q.nack(c2.job_id, c2.lease_token, now=3.0, error="aborted")
        self.assertEqual(res2.status, "cancelled")

        swept = sweep_expired_leases(q.store, now=12.0)
        self.assertEqual(swept, [c3.job_id])
        self.assertEqual(q.store.get(c3.job_id).status, "cancelled")

    def test_requeue_dlq_resets_attempts_and_validates_state(self) -> None:
        q = JobQueue()
        q.enqueue("j1", {}, max_attempts=1)
        c = q.claim("w1", now=0.0)
        assert c is not None and c.lease_token is not None
        q.nack("j1", c.lease_token, now=1.0, error="fatal")
        self.assertEqual(q.store.get("j1").status, "dead")

        requeued = q.requeue_dlq("j1", now=20.0)
        self.assertEqual((requeued.status, requeued.attempts, requeued.available_at, requeued.last_error), ("pending", 0, 20.0, None))
        with self.assertRaises(QueueError):
            q.requeue_dlq("j1", now=21.0)

    def test_json_persistence_preserves_seq_backoff_leases_and_cancel(self) -> None:
        q = JobQueue()
        q.enqueue("a", {"k": 1}, priority=1, base_backoff_s=4.0, max_backoff_s=20.0, backoff_multiplier=2.5)
        q.enqueue("b", {"k": 2}, priority=1)
        q.enqueue("c", {"k": 3}, priority=1)
        claimed = q.claim("w1", now=0.0, lease_seconds=50.0)
        assert claimed is not None and claimed.lease_token is not None
        q.cancel("a")
        old_token = claimed.lease_token

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "queue.json"
            q.store.save(path)
            loaded = JobQueue(JobStore.load(path))

            # New job must get seq > max previous seq (3), not collide on 3 if store size changed or reloaded.
            d = loaded.enqueue("d", {"k": 4}, priority=1)
            self.assertGreater(d.seq, q.store.get("c").seq)

            # Active lease and cancel_requested must survive reload.
            reloaded_a = loaded.store.get("a")
            self.assertTrue(reloaded_a.cancel_requested)
            self.assertEqual(reloaded_a.lease_token, old_token)
            self.assertEqual(loaded.heartbeat("a", old_token, now=10.0), False)
            loaded.nack("a", old_token, now=11.0, error="e")
            self.assertEqual(loaded.store.get("a").status, "cancelled")

            # Backoff parameters must survive reload too.
            b = loaded.claim("w2", now=12.0)
            c = loaded.claim("w2", now=12.0)
            d_claim = loaded.claim("w2", now=12.0)
            self.assertEqual([x.job_id for x in (b, c, d_claim) if x], ["b", "c", "d"])

    def test_stats_sweeps_when_now_provided_and_rejects_bad_enqueue_args(self) -> None:
        q = JobQueue()
        with self.assertRaises(QueueError):
            q.enqueue("", {})
        with self.assertRaises(QueueError):
            q.enqueue("ok", {}, max_attempts=0)
        with self.assertRaises(QueueError):
            q.enqueue("ok", {}, backoff_multiplier=0.5)
        q.enqueue("j1", {}, max_attempts=2, base_backoff_s=5.0)
        q.enqueue("j2", {}, max_attempts=1)
        q.claim("w1", now=0.0, lease_seconds=5.0)
        q.claim("w1", now=0.0, lease_seconds=5.0)
        self.assertEqual(q.stats(now=5.0), {"pending": 1, "running": 0, "completed": 0, "dead": 1, "cancelled": 0})


if __name__ == "__main__":
    unittest.main()
