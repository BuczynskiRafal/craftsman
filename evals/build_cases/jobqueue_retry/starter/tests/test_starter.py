"""Starter sanity tests for jobqueue_retry."""

import unittest

from jobqueue_retry import JobQueue


class StarterJobQueueTests(unittest.TestCase):
    def test_enqueue_claim_ack_happy_path(self) -> None:
        q = JobQueue()
        q.enqueue("j-low", {"x": 1}, priority=10)
        q.enqueue("j-high", {"x": 2}, priority=1)

        job = q.claim("w1", now=0.0, lease_seconds=10.0)
        assert job is not None
        self.assertEqual(job.job_id, "j-high")
        self.assertEqual(job.status, "running")
        self.assertEqual(job.attempts, 1)

        done = q.ack("j-high", job.lease_token or "", now=5.0, result={"ok": True})
        self.assertEqual(done.status, "completed")
        self.assertEqual(done.result, {"ok": True})


if __name__ == "__main__":
    unittest.main()
