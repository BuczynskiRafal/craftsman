"""Tests for the global git hooks: core.hooksPath, chaining to earlier hooks, and the guard."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from craftsman.files import BACKUP_DIR_NAME
from craftsman.githooks import (
    PASSTHROUGH_TEMPLATE_NAME,
    PREVIOUS_HOOKS_FILE,
    PYTHON_CMD_FILE,
    STANDARD_PASSTHROUGH_GIT_HOOKS,
    git_config_get,
    git_config_unset,
)
from craftsman.manifest import InstallError

from tests.support import IsolatedHomeTestCase

AI_TRAILER = "feat: bad\n\nCo-authored-by: Claude <noreply@anthropic.com>"


def executable(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/usr/bin/env bash\n{body}\n", encoding="utf-8")
    path.chmod(0o755)
    return path


class GitHooksTestCase(IsolatedHomeTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.hooks_dir = self.home / ".craftsman" / "git-hooks"

    def work_repo(self) -> Path:
        repo = self.tmp / "work"
        repo.mkdir()
        self.git(repo, "init", "-q")
        (repo / "f.txt").write_text("v1\n", encoding="utf-8")
        self.git(repo, "add", "f.txt")
        return repo

    def git(self, repo: Path, *args: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-c", "user.name=Dev", "-c", "user.email=dev@example.com", *args],
            cwd=repo,
            env=self.env,
            input=stdin,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )

    def run_hook(
        self, repo: Path, name: str, *args: str, stdin: str = ""
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(self.hooks_dir / name), *args],
            cwd=repo,
            env=self.env,
            input=stdin,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )


class HooksPathTests(GitHooksTestCase):
    def test_install_points_core_hooks_path_at_craftsman_home(self) -> None:
        self.assert_ok(self.install("--git-hooks"))

        self.assertEqual(self.hooks_path(), str(self.hooks_dir))
        self.assertFalse((self.hooks_dir / PREVIOUS_HOOKS_FILE).exists())

    def test_uninstall_unsets_a_hooks_path_craftsman_set(self) -> None:
        self.assert_ok(self.install("--git-hooks"))

        self.assert_ok(self.uninstall("--git-hooks"))

        self.assertIsNone(self.hooks_path())
        self.assertNotIn("[core]", self.git_config.read_text(encoding="utf-8"))

    def test_uninstall_restores_the_replaced_hooks_path(self) -> None:
        (self.home / "my-hooks").mkdir()
        self.git_global("core.hooksPath", "~/my-hooks")
        self.assert_ok(self.install("--git-hooks"))

        self.assert_ok(self.uninstall("--git-hooks"))

        self.assertEqual(self.hooks_path(), "~/my-hooks")

    def test_reinstall_keeps_the_replaced_hooks_path(self) -> None:
        (self.home / "my-hooks").mkdir()
        self.git_global("core.hooksPath", "~/my-hooks")
        self.assert_ok(self.install("--git-hooks"))

        self.assert_ok(self.install("--git-hooks"))

        recorded = (self.hooks_dir / PREVIOUS_HOOKS_FILE).read_text(encoding="utf-8").strip()
        self.assertEqual(recorded, str(self.home / "my-hooks"))

    def test_hooks_path_changed_since_install_is_left_with_a_warning(self) -> None:
        self.assert_ok(self.install("--git-hooks"))
        self.git_global("core.hooksPath", str(self.tmp / "other-hooks"))

        result = self.uninstall("--git-hooks")

        self.assertEqual(result.code, 0)
        self.assertEqual(self.hooks_path(), str(self.tmp / "other-hooks"))
        self.assertIn("[WARN] leaving git config --global core.hooksPath", result.err)

    def test_second_uninstall_has_nothing_to_do(self) -> None:
        self.assert_ok(self.install("--git-hooks"))
        self.assert_ok(self.uninstall("--git-hooks"))

        result = self.uninstall("--git-hooks")

        self.assertEqual(result.code, 0)
        self.assertIn("Nothing to uninstall", result.out)

    def test_hooks_path_at_other_craftsman_hooks_is_refused(self) -> None:
        self.assert_ok(self.install("--git-hooks"))
        os.environ["CRAFTSMAN_HOME"] = str(self.home / "other-craftsman")

        result = self.install("--git-hooks")

        self.assertEqual(result.code, 1)
        self.assertIn("run `craftsman uninstall --git-hooks` before installing them here", result.err)
        self.assertEqual(self.hooks_path(), str(self.hooks_dir))

    def test_hooks_path_at_hooks_no_manifest_records_is_refused(self) -> None:
        self.assert_ok(self.install("--git-hooks"))
        (self.home / ".craftsman" / ".craftsman-install.json").unlink()

        result = self.install("--git-hooks")

        self.assertEqual(result.code, 1)
        self.assertIn("no install manifest records the value it replaced", result.err)

    def test_relative_hooks_path_is_kept_as_the_previous_one(self) -> None:
        work = self.tmp / "cwd"
        work.mkdir()
        (work / ".craftsman-install.json").write_text("{", encoding="utf-8")
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(work)
        self.git_global("core.hooksPath", ".githooks")

        self.assert_ok(self.install("--git-hooks"))
        self.assert_ok(self.uninstall("--git-hooks"))

        self.assertEqual(self.hooks_path(), ".githooks")

    def test_stale_previous_hooks_file_is_moved_aside(self) -> None:
        stale = self.hooks_dir / PREVIOUS_HOOKS_FILE
        stale.parent.mkdir(parents=True)
        stale.write_text("/stale/hooks\n", encoding="utf-8")

        self.assert_ok(self.install("--git-hooks"))

        self.assertFalse(stale.exists())
        self.assertTrue(any((self.hooks_dir / BACKUP_DIR_NAME).iterdir()))

    def test_antigravity_install_hosts_the_git_hooks(self) -> None:
        self.assert_ok(self.install("--antigravity", "--git-hooks"))

        self.assertEqual(self.hooks_path(), str(self.home / ".gemini" / "config" / "git-hooks"))


class HookScriptTests(GitHooksTestCase):
    def test_every_standard_hook_is_installed_as_the_passthrough(self) -> None:
        self.assert_ok(self.install("--git-hooks"))

        template = (self.hooks_dir / PASSTHROUGH_TEMPLATE_NAME).read_bytes()
        for name in STANDARD_PASSTHROUGH_GIT_HOOKS:
            self.assertEqual((self.hooks_dir / name).read_bytes(), template, name)

    def test_passthrough_forwards_stdin_to_every_chained_hook(self) -> None:
        previous = self.tmp / "previous"
        executable(previous / "post-rewrite", f'cat > "{self.tmp / "previous.txt"}"')
        self.git_global("core.hooksPath", str(previous))
        self.assert_ok(self.install("--git-hooks"))
        repo = self.work_repo()
        executable(repo / ".git" / "hooks" / "post-rewrite", f'cat > "{self.tmp / "local.txt"}"')

        proc = self.run_hook(repo, "post-rewrite", "rebase", stdin="old1 new1\nold2 new2\n")

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            (self.tmp / "previous.txt").read_text(encoding="utf-8"), "old1 new1\nold2 new2\n"
        )
        self.assertEqual((self.tmp / "local.txt").read_text(encoding="utf-8"), "old1 new1\nold2 new2\n")

    def test_failing_local_hook_blocks_the_commit_after_the_previous_one(self) -> None:
        previous = self.tmp / "previous"
        executable(previous / "pre-commit", f'touch "{self.tmp / "previous-ran"}"')
        self.git_global("core.hooksPath", str(previous))
        self.assert_ok(self.install("--git-hooks"))
        repo = self.work_repo()
        executable(repo / ".git" / "hooks" / "pre-commit", "echo 'blocked locally' >&2\nexit 23")

        commit = self.git(repo, "commit", "-m", "feat: test commit")

        self.assertNotEqual(commit.returncode, 0)
        self.assertTrue((self.tmp / "previous-ran").is_file())
        self.assertIn("blocked locally", commit.stderr)

    def test_pre_push_forwards_stdin_to_the_previous_hook(self) -> None:
        previous = self.tmp / "previous"
        executable(previous / "pre-push", f'cat > "{self.tmp / "pushed.txt"}"')
        self.git_global("core.hooksPath", str(previous))
        self.assert_ok(self.install("--git-hooks"))
        push = "refs/heads/feat/x 0000 refs/heads/feat/x 0000\n"

        proc = self.run_hook(
            self.work_repo(), "pre-push", "origin", "https://example.com/r.git", stdin=push
        )

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual((self.tmp / "pushed.txt").read_text(encoding="utf-8"), push)

    def test_commit_with_ai_attribution_is_blocked(self) -> None:
        self.assert_ok(self.install("--git-hooks"))
        repo = self.work_repo()

        commit = self.git(repo, "commit", "-m", AI_TRAILER)

        self.assertNotEqual(commit.returncode, 0)
        self.assertIn("AI attribution", commit.stderr)

    def test_symlinked_hooks_block_ai_attribution(self) -> None:
        self.assert_ok(self.install("--git-hooks", "--symlink"))
        repo = self.work_repo()

        self.assertTrue((self.hooks_dir / "commit-msg").is_symlink())
        self.assertEqual(self.git(repo, "commit", "-m", "feat: fine").returncode, 0)
        (repo / "f.txt").write_text("v2\n", encoding="utf-8")
        self.git(repo, "add", "f.txt")
        self.assertNotEqual(self.git(repo, "commit", "-m", AI_TRAILER).returncode, 0)


class FailClosedTests(GitHooksTestCase):
    def commit_after_removing(self, path: Path) -> subprocess.CompletedProcess[str]:
        self.assert_ok(self.install("--git-hooks"))
        path.unlink()
        return self.git(self.work_repo(), "commit", "-m", "feat: fine")

    def test_missing_guard_script_blocks_the_commit(self) -> None:
        commit = self.commit_after_removing(self.home / ".craftsman" / "scripts" / "git-guard.py")

        self.assertNotEqual(commit.returncode, 0)
        self.assertIn("git-guard.py is missing", commit.stderr)

    def test_missing_python_command_blocks_the_commit(self) -> None:
        commit = self.commit_after_removing(self.hooks_dir / PYTHON_CMD_FILE)

        self.assertNotEqual(commit.returncode, 0)
        self.assertIn("names no runnable Python", commit.stderr)

    def test_missing_passthrough_helpers_block_the_commit(self) -> None:
        commit = self.commit_after_removing(self.hooks_dir / PASSTHROUGH_TEMPLATE_NAME)

        self.assertNotEqual(commit.returncode, 0)
        self.assertIn(PASSTHROUGH_TEMPLATE_NAME, commit.stderr)


class GitConfigTests(GitHooksTestCase):
    def test_unset_key_reads_as_none(self) -> None:
        self.assertIsNone(git_config_get("core.hooksPath", self.home))

    def test_unreadable_global_config_is_an_error(self) -> None:
        self.git_config.write_text("[core\n", encoding="utf-8")

        with self.assertRaisesRegex(InstallError, "git config --global --get core.hooksPath failed"):
            git_config_get("core.hooksPath", self.home)

    def test_config_outside_an_explicit_home_is_not_read(self) -> None:
        self.git_global("core.hooksPath", "/from/the/environment")
        other_home = self.tmp / "other"
        other_home.mkdir()
        (other_home / ".gitconfig").write_text("[core]\n\thooksPath = /from/other\n", encoding="utf-8")

        self.assertEqual(git_config_get("core.hooksPath", other_home), "/from/other")

    def test_unset_keeps_a_section_with_other_keys(self) -> None:
        self.git_global("core.hooksPath", "/x")
        self.git_global("core.editor", "vim")

        git_config_unset("core.hooksPath", self.home)

        self.assertEqual(git_config_get("core.editor", self.home), "vim")
