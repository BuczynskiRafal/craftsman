#!/usr/bin/env python3
"""End-to-end and integration tests for working tree and metadata hygiene (Milestone 1 / Faza 0.5)."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from craftsman.evidence import (
    create_inbox_file,
    create_scratch_dir,
    prune_all_temp_dirs,
    prune_scratch_dirs,
    scratch_dir_for,
)
from craftsman.hooks import Reply, failure

from tests.support import EvidenceRepo


class ScratchDirectoryTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.craftsman_dir = Path(tmp.name) / "craftsman"
        self.craftsman_dir.mkdir(parents=True, exist_ok=True)

    def test_scratch_dir_creation_and_pruning(self) -> None:
        s1 = create_scratch_dir(self.craftsman_dir)
        s2 = create_scratch_dir(self.craftsman_dir)
        self.assertTrue(s1.is_dir())
        self.assertTrue(s2.is_dir())
        self.assertNotEqual(s1, s2)
        self.assertEqual(s1.parent, scratch_dir_for(self.craftsman_dir))

        # Write scratch files inside
        (s1 / "repro.py").write_text("print('test')", encoding="utf-8")
        (s2 / "sub" / "deep.txt").parent.mkdir()
        (s2 / "sub" / "deep.txt").write_text("deep", encoding="utf-8")

        # Prune scratch dirs
        pruned = prune_scratch_dirs(self.craftsman_dir)
        self.assertEqual(len(pruned), 2)
        self.assertFalse(s1.exists())
        self.assertFalse(s2.exists())

        # Second prune is a no-op
        self.assertEqual(prune_scratch_dirs(self.craftsman_dir), ())

    def test_prune_all_temp_dirs_cleans_inbox_and_scratch(self) -> None:
        inbox_file = create_inbox_file(self.craftsman_dir, "cover")
        scratch = create_scratch_dir(self.craftsman_dir)
        (scratch / "script.sh").write_text("echo hi", encoding="utf-8")

        self.assertTrue(inbox_file.is_file())
        self.assertTrue(scratch.is_dir())

        prune_all_temp_dirs(self.craftsman_dir)

        self.assertFalse(inbox_file.exists())
        self.assertFalse(scratch.exists())

    def test_prune_scratch_dirs_handles_readonly_trees_and_symlinks(self) -> None:
        # 1. Read-only subdirectory
        s1 = create_scratch_dir(self.craftsman_dir)
        sub = s1 / "ro_sub"
        sub.mkdir()
        (sub / "inner.txt").write_text("inner", encoding="utf-8")
        sub.chmod(0o500)

        # 2. Read-only file
        s2 = create_scratch_dir(self.craftsman_dir)
        ro_file = s2 / "ro_file.txt"
        ro_file.write_text("read-only", encoding="utf-8")
        ro_file.chmod(0o400)

        # 3. Read-only root directory
        s3 = create_scratch_dir(self.craftsman_dir)
        (s3 / "data.txt").write_text("data", encoding="utf-8")
        s3.chmod(0o500)

        # 4. Symlink inside scratch dir
        scratch_parent = scratch_dir_for(self.craftsman_dir)
        link = scratch_parent / "external_symlink"
        link.symlink_to(Path(tempfile.gettempdir()))

        # Prune all scratch dirs
        pruned = prune_scratch_dirs(self.craftsman_dir)
        self.assertIn(s1, pruned)
        self.assertIn(s2, pruned)
        self.assertIn(s3, pruned)
        self.assertIn(link, pruned)

        self.assertFalse(s1.exists())
        self.assertFalse(s2.exists())
        self.assertFalse(s3.exists())
        self.assertFalse(link.is_symlink())
        self.assertFalse(scratch_parent.exists())


class WorkingTreeHygieneE2ETests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = EvidenceRepo(Path(tmp.name) / "repo")

    def test_cli_review_scratch_creates_valid_directory(self) -> None:
        proc = self.repo.cli("review", "scratch")
        created_path = Path(proc.stdout.strip()).resolve()
        expected_parent = (self.repo.ledger_dir / "scratch").resolve()
        self.assertTrue(created_path.is_dir())
        self.assertTrue(str(created_path).startswith(str(expected_parent)))

    def test_full_gate_session_leaves_working_tree_clean(self) -> None:
        baseline_status = self.repo.git("status", "--porcelain", "--ignored").strip()
        self.assertEqual(baseline_status, "")

        # Intended change
        self.repo.write("app.py", "x = 42\n")

        # Start session
        self.repo.cli("gate", "--init", "--tier", "quick", "--task", "hygiene test")

        # Create scratch dir and temporary files
        scratch_proc = self.repo.cli("review", "scratch")
        scratch_path = Path(scratch_proc.stdout.strip())
        (scratch_path / "temp_script.py").write_text("print('hello')", encoding="utf-8")

        # Prepare review
        self.repo.cli("review", "prepare")
        self.repo.cover_all()

        # Run verification
        self.repo.cli("run", "--kind", "test", "--", sys.executable, "-c", "print('tests pass')")

        # Check passes
        self.repo.cli("review", "check")

        # Complete gate
        self.repo.cli("gate", "--complete")

        # Verify working tree status: only the intended edit to app.py exists
        current_status = self.repo.git("status", "--porcelain", "--ignored").strip()
        self.assertEqual(current_status, "M app.py")

        # Verify .git/craftsman temp dirs are pruned
        scratch_dir = self.repo.ledger_dir / "scratch"
        inbox_dir = self.repo.ledger_dir / "inbox"
        if scratch_dir.exists():
            self.assertEqual(list(scratch_dir.iterdir()), [])
        if inbox_dir.exists():
            self.assertEqual(list(inbox_dir.iterdir()), [])

    def test_review_only_session_leaves_working_tree_clean(self) -> None:
        # Intended change
        self.repo.write("app.py", "x = 99\n")

        # Create scratch directory
        scratch_proc = self.repo.cli("review", "scratch")
        scratch_path = Path(scratch_proc.stdout.strip())
        (scratch_path / "repro.py").write_text("assert False", encoding="utf-8")

        # Prepare review-only
        self.repo.cli("review", "prepare", "--tier", "quick")
        self.repo.cover_all()

        # Review check and report
        self.repo.cli("review", "check", "--review-only")
        self.repo.cli("review", "report", "--review-only")

        # After report --review-only, scratch directory is pruned
        self.assertFalse(scratch_path.exists())

        # Working tree status has only M app.py
        current_status = self.repo.git("status", "--porcelain", "--ignored").strip()
        self.assertEqual(current_status, "M app.py")

        # Review reset also archives and prunes temp dirs
        self.repo.cli("review", "reset")
        self.assertFalse((self.repo.ledger_dir / "scratch").exists() and list((self.repo.ledger_dir / "scratch").iterdir()))

    def test_out_of_scope_dirty_tracking_warns_on_new_changes(self) -> None:
        # Commit a second tracked file
        self.repo.write("other.py", "y = 1\n")
        self.repo.commit("add other.py")

        # Modify app.py (to be in scope) and other.py (already dirty out of scope)
        self.repo.write("app.py", "x = 10\n")
        self.repo.write("other.py", "y = 20\n")

        # Prepare with scope restricted to app.py
        self.repo.cli("review", "prepare", "--paths", "app.py", "--tier", "quick")
        self.repo.cover_all()

        # Report does NOT warn about other.py because it was already dirty at prepare
        report_initial = self.repo.cli("review", "report", "--review-only")
        self.assertNotIn("Files created or changed during this review outside its scope", report_initial.stdout)

        # Now agent modifies a third file outside scope
        self.repo.write("leak.py", "z = 30\n")

        # Report warns about leak.py
        report_warn = self.repo.cli("review", "report", "--review-only")
        self.assertIn("Warning: Files created or changed during this review outside its scope: leak.py", report_warn.stdout)
        self.assertIn("delete or revert them if this session made them", report_warn.stdout)

        # Review check --review-only also warns
        check_warn = self.repo.cli("review", "check", "--review-only")
        self.assertIn("Warning: Files created or changed during this review outside its scope: leak.py", check_warn.stdout)

        # Removing the leak resolves the warning
        (self.repo.root / "leak.py").unlink()
        report_clean = self.repo.cli("review", "report", "--review-only")
        self.assertNotIn("Files created or changed during this review outside its scope", report_clean.stdout)


class HookHygieneTests(unittest.TestCase):
    def test_hook_failure_visible_reporting_on_prompt(self) -> None:
        reply_copilot = Reply("copilot", "prompt")
        out_copilot = failure(reply_copilot, "resolve_base_sha error: empty repo")
        expected_msg = "Craftsman gate was NOT opened: resolve_base_sha error: empty repo"
        self.assertIn(expected_msg, out_copilot.get("systemMessage", ""))
        self.assertIn(expected_msg, out_copilot.get("additionalContext", ""))
        self.assertIn(expected_msg, out_copilot.get("hookSpecificOutput", {}).get("additionalContext", ""))

        reply_claude = Reply("claude", "prompt")
        out_claude = failure(reply_claude, "storage error")
        self.assertIn("Craftsman gate was NOT opened: storage error", out_claude.get("systemMessage", ""))
        self.assertIn("Craftsman gate was NOT opened: storage error", out_claude.get("hookSpecificOutput", {}).get("additionalContext", ""))


if __name__ == "__main__":
    unittest.main()
