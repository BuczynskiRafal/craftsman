"""Integrity tests for the build-and-verify benchmark suite."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from evals import build_bench

EXPECTED_CASES = (
    "config_overlay",
    "docindex_search",
    "jobqueue_retry",
    "ledger_batch",
    "pkgcache_sync",
    "schedule_rrule",
)


class BuildCasesIntegrityTests(unittest.TestCase):
    def test_expected_cases_exist(self) -> None:
        self.assertEqual(build_bench.case_ids(), sorted(EXPECTED_CASES))

    def test_all_build_bench_files_use_lf_and_no_bom(self) -> None:
        files = [
            Path(build_bench.__file__),
            Path(__file__),
            *(p for p in build_bench.BUILD_CASES_DIR.rglob("*") if p.is_file() and "__pycache__" not in p.parts),
        ]
        bad: list[str] = []
        for path in files:
            raw = path.read_bytes()
            if raw.startswith(b"\xef\xbb\xbf") or b"\r\n" in raw:
                bad.append(str(path))
        self.assertEqual(bad, [])

    def test_starters_pass_starter_tests_fail_hidden_tests_and_prepare_cleanly(self) -> None:
        for cid in EXPECTED_CASES:
            with self.subTest(case=cid):
                case = build_bench.load_case(cid)
                with tempfile.TemporaryDirectory() as tmp:
                    workdir = build_bench.prepare_case(case, Path(tmp) / cid)
                    self.assertTrue((workdir / "TASK.md").is_file())
                    self.assertTrue((workdir / "tests" / "test_starter.py").is_file())
                    self.assertFalse((workdir / "hidden_tests.py").exists())
                    self.assertEqual(list(workdir.rglob("*hidden_tests*")), [])

                    status = subprocess.run(
                        ["git", "status", "--short"],
                        cwd=workdir,
                        capture_output=True,
                        text=True,
                        check=True,
                    )
                    self.assertEqual(status.stdout.strip(), "")

                    grade = build_bench.grade_workdir(case, workdir)
                    self.assertTrue(grade["starter"]["ok"], grade["starter"])
                    self.assertFalse(grade["starter"]["timed_out"])
                    self.assertFalse(grade["hidden"]["ok"])
                    self.assertFalse(grade["hidden"]["timed_out"])
                    self.assertGreaterEqual(grade["hidden"]["ran"], 8)
                    for item in grade["hidden"]["failed_tests"]:
                        self.assertNotIn("unittest.loader", item)
                        self.assertNotIn("<runner-error", item)


if __name__ == "__main__":
    unittest.main()
