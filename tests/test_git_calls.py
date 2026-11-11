"""How many git processes commands and hooks start: a few milliseconds each on Linux, tens on Windows.

The counts are exact on purpose. A change that adds a git call to a hot path has to change a number here,
so the cost is a decision and not an accident.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any
from unittest import mock

from craftsman import cli, hooks
from craftsman.gate import state_path_for
from craftsman.hooks import open_session
from craftsman.ledger import ledger_path_for
from craftsman.workspace import open_workspace

from tests.support import EvidenceRepo, coverage_note, run_main

LOCATE = ("rev-parse", "--show-toplevel", "--absolute-git-dir")
SNAPSHOT = ("rev-parse", "diff", "ls-files")  # the base commit, the diff, the untracked files


@contextmanager
def git_calls() -> Iterator[list[tuple[str, ...]]]:
    """Each git process this process starts inside the block, as its arguments after the `-c` options."""
    calls: list[tuple[str, ...]] = []
    run = subprocess.run

    def counting(argv: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(argv, list) and argv[:1] == ["git"]:
            calls.append(_without_config_options(argv[1:]))
        return run(argv, *args, **kwargs)

    with mock.patch("subprocess.run", counting):
        yield calls


def _without_config_options(args: Sequence[str]) -> tuple[str, ...]:
    rest = list(args)
    while rest[:1] == ["-c"]:
        del rest[:2]
    return tuple(rest)


def commands(calls: Sequence[tuple[str, ...]]) -> list[str]:
    """The git subcommand of each call, in order."""
    return [call[0] for call in calls]


class GitCallsTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.repo = EvidenceRepo(self.tmp / "repo")
        self.root = self.repo.root
        env = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.tmp / "claude-config")})
        env.start()
        self.addCleanup(env.stop)
        self.repo.write("cart.py", "def total(items):\n    return sum(items)\n")
        self.repo.write("price.py", "def scale(value):\n    return value\n")
        self.repo.write("use.py", "from cart import total\nfrom price import scale\nscale(total([1]))\n")
        self.repo.commit("cart")
        self.repo.write("cart.py", "def total(items):\n    return sum(items) + 0\n")
        self.repo.write("price.py", "def scale(value):\n    return value * 2\n")

    def craftsman(self, *args: str) -> list[tuple[str, ...]]:
        """The git calls of `craftsman <args>` run in this process; the command must succeed."""
        with git_calls() as calls:
            result = run_main(cli.main, *args)
        self.assertEqual(result.code, 0, result.err)
        return calls

    def hook(self, event: str, hook_event_name: str, **extra: Any) -> list[tuple[str, ...]]:
        """The git calls of a Claude Code hook of session S1, run in this process."""
        payload = {
            "session_id": "S1",
            "transcript_path": str(self.tmp / "no-such-transcript.jsonl"),
            "cwd": str(self.root),
            "hook_event_name": hook_event_name,
            **extra,
        }
        out, err = io.StringIO(), io.StringIO()
        with (
            git_calls() as calls,
            mock.patch("sys.stdin", io.StringIO(json.dumps(payload))),
            redirect_stdout(out),
            redirect_stderr(err),
        ):
            code = hooks.main([event, "--platform", "claude"])
        self.assertEqual(code, 0, err.getvalue())
        self.assertNotIn("not checked", out.getvalue())
        return calls

    def open_gate_of_s1(self) -> None:
        self.repo.cli("gate", "--init", "--session-id", "S1", env_extra={"CLAUDECODE": "1"})


class WorkspaceTests(GitCallsTestCase):
    def test_opening_a_workspace_asks_git_once(self) -> None:
        (self.root / "pkg").mkdir()

        with git_calls() as calls:
            workspace = open_workspace(str(self.root / "pkg"))

        self.assertEqual(calls, [LOCATE])
        self.assertEqual(workspace.root, self.root)
        self.assertEqual(workspace.ledger_path, self.root / ".git" / "craftsman" / "ledger.json")
        self.assertEqual(workspace.state_path, self.root / ".git" / "craftsman" / "craftsman_state.json")

    def test_a_snapshot_looks_up_the_base_and_reads_the_diff_and_the_untracked_files(self) -> None:
        workspace = open_workspace(str(self.root))

        with git_calls() as calls:
            snap = workspace.snapshot()

        self.assertEqual(commands(calls), list(SNAPSHOT))
        self.assertEqual(snap.files, ("cart.py", "price.py"))

    def test_a_session_asks_git_once_for_its_paths(self) -> None:
        with git_calls() as calls:
            session = open_session(str(self.root))
            assert session is not None
            paths = (session.state_path, session.ledger_path, session.craftsman_dir)

        self.assertEqual(calls, [LOCATE])
        craftsman_dir = self.root / ".git" / "craftsman"
        expected = (craftsman_dir / "craftsman_state.json", craftsman_dir / "ledger.json", craftsman_dir)
        self.assertEqual(paths, expected)

    def test_the_carried_paths_are_those_git_gives_for_the_root(self) -> None:
        linked = self.tmp / "linked"
        self.repo.git("worktree", "add", "-q", str(linked), "-b", "side")
        (linked / "pkg").mkdir()
        git_variables = {"GIT_DIR": str(self.root / ".git"), "GIT_WORK_TREE": str(self.root)}
        places: tuple[tuple[str, Path, dict[str, str], Path], ...] = (
            ("linked worktree", linked / "pkg", {}, self.root / ".git" / "worktrees" / "linked"),
            ("GIT_DIR and GIT_WORK_TREE", self.tmp, git_variables, self.root / ".git"),
        )
        for name, cwd, variables, git_directory in places:
            with self.subTest(name), mock.patch.dict(os.environ, variables):
                workspace, session = open_workspace(str(cwd)), open_session(str(cwd))
                assert session is not None
                # What git answers when asked from the root, as before the paths were carried.
                expected = (state_path_for(workspace.root), ledger_path_for(workspace.root))

                self.assertEqual(expected[1].parent, git_directory / "craftsman")
                self.assertEqual(session.root, workspace.root)
                self.assertEqual((workspace.state_path, workspace.ledger_path), expected)
                self.assertEqual((session.state_path, session.ledger_path), expected)

    def test_a_session_without_its_git_dir_asks_git_for_it(self) -> None:
        session = hooks.Session(self.root)

        with git_calls() as calls:
            path = session.ledger_path

        self.assertEqual(calls, [("rev-parse", "--absolute-git-dir")])
        self.assertEqual(path, self.root / ".git" / "craftsman" / "ledger.json")


class CommandTests(GitCallsTestCase):
    def test_review_add_and_cover_snapshot_once(self) -> None:
        self.repo.cli("review", "prepare")
        cwd = ("--cwd", str(self.root))
        add = self.craftsman(
            "review", "add", *cwd, "--lens", "contracts", "--file", "cart.py", "--line", "2",
            "--severity", "major", "--title", "Strings are summed",
            "--scenario", "total(['a']) raises TypeError",
        )  # fmt: skip
        cover = self.craftsman("review", "cover", *cwd, "--hunks", "H1", "--note", coverage_note(["H1"]))

        for calls in (add, cover):
            self.assertEqual(calls[0], LOCATE)
            self.assertEqual(commands(calls), ["rev-parse", *SNAPSHOT])

    def test_a_run_snapshots_before_and_after_the_command(self) -> None:
        command = ("--", sys.executable, "-c", "")
        calls = self.craftsman("run", "--kind", "lint", "--cwd", str(self.root), *command)

        self.assertEqual(calls[0], LOCATE)
        self.assertEqual(commands(calls), ["rev-parse", *SNAPSHOT, *SNAPSHOT])

    def test_a_view_searches_only_the_symbols_of_its_hunks(self) -> None:
        self.repo.cli("review", "prepare")
        packet = self.repo.ledger_dir / "packet-1.md"
        self.assertIn("`scale`", packet.read_text(encoding="utf-8"))

        calls = self.craftsman("review", "view", "--cwd", str(self.root), "--hunks", "H1")

        searched = [call[-2] for call in calls if call[0] == "grep"]  # `git grep ... -e <symbol> --`
        self.assertEqual(searched, ["total"])


class HookTests(GitCallsTestCase):
    def test_an_idle_pre_tool_hook_asks_git_once(self) -> None:
        path = str(self.root / "cart.py")
        read = self.hook("pre-tool", "PreToolUse", tool_name="Read", tool_input={"file_path": path})
        shell = self.hook("pre-tool", "PreToolUse", tool_name="Bash", tool_input={"command": "ls"})

        self.assertEqual(read, [LOCATE])
        self.assertEqual(shell, [LOCATE])

    def test_a_stop_hook_with_an_active_gate_snapshots_once(self) -> None:
        self.open_gate_of_s1()

        calls = self.hook("stop", "Stop")

        self.assertEqual(calls[0], LOCATE)
        # The last call is the ledger lookup of `evidence.evaluate_evidence`, which has no path to reuse.
        self.assertEqual(commands(calls), ["rev-parse", *SNAPSHOT, "rev-parse"])
        self.assertEqual(calls[-1], ("rev-parse", "--absolute-git-dir"))


if __name__ == "__main__":
    unittest.main()
