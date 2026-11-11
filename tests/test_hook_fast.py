"""The PreToolUse fast path (`craftsman.hook_fast`) answers only as the full handler would."""

from __future__ import annotations

import contextlib
import io
import itertools
import json
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import fields, replace
from pathlib import Path
from typing import Any
from unittest import mock

from craftsman import command_policy, gate, hook_fast, hooks, records, scope
from craftsman.cli import main as cli_main
from craftsman.gate import GateOwner, GateState, GateStateError, Stage, load_state, save_state

from tests.ambient import AMBIENT_PLATFORM_ENV_VARS
from tests.support import REPO_ROOT, EvidenceRepo, GitRepo

SESSION = "claude-session"
STATE = ("craftsman", "craftsman_state.json")
ARGV = {"claude": ["pre-tool", "--platform", "claude"], "copilot": ["pre-tool", "--platform", "copilot"]}


def claude_call(
    cwd: str | None, tool: str = "Read", tool_input: object = None, **extra: Any
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "session_id": SESSION,
        "hook_event_name": "PreToolUse",
        "tool_name": tool,
        **extra,
    }
    if cwd is not None:
        payload["cwd"] = cwd
    if tool_input is not None:
        payload["tool_input"] = tool_input
    return payload


def copilot_call(
    cwd: str, tool: str = "read_file", tool_args: object = None, **extra: Any
) -> dict[str, Any]:
    payload: dict[str, Any] = {"sessionId": "cli-session", "cwd": cwd, "toolName": tool, **extra}
    if tool_args is not None:
        payload["toolArgs"] = tool_args
    return payload


def inactive_state(**changes: Any) -> dict[str, Any]:
    """A closed gate, as `craftsman gate --complete` leaves it."""
    state = GateState.open(
        GateOwner("claude", SESSION), "a" * 40, False, "task", isolation_evidence="hooks"
    )
    return {**replace(state, active=False, stage=Stage.COMPLETED).to_json(), **changes}


def active_state(owner: GateOwner) -> dict[str, Any]:
    return GateState.open(owner, "a" * 40, False, "task").to_json()


def run_full(platform: str, text: str, environ: dict[str, str]) -> tuple[int, str]:
    """`hooks.main` on the payload `text` with exactly `environ`: (exit code, stdout)."""
    out = io.StringIO()
    with mock.patch.dict(os.environ, environ, clear=True), contextlib.redirect_stdout(out):
        with contextlib.redirect_stderr(io.StringIO()):
            code = hooks.main(ARGV[platform], stdin=text)
    return code, out.getvalue()


def hook_environ(home: Path) -> dict[str, str]:
    """This environment without what makes the fast path defer (platform identity, git discovery), plus
    configuration in the environment that cannot move a work tree, which is no reason to defer."""
    environ = {
        name: value
        for name, value in os.environ.items()
        if name not in AMBIENT_PLATFORM_ENV_VARS
        and name not in hook_fast.GIT_DISCOVERY_VARIABLES
        and not name.startswith("GIT_CONFIG")
        and name not in ("HOME", "USERPROFILE", "CLAUDE_CONFIG_DIR")
    }
    environ.update(
        HOME=str(home),
        GIT_CONFIG_COUNT="1",
        GIT_CONFIG_KEY_0="credential.interactive",
        GIT_CONFIG_VALUE_0="false",
        GIT_CONFIG_PARAMETERS="'color.ui'='never'",
    )
    return environ


def craftsman_files(root: Path) -> dict[str, bytes]:
    """Every file in a `craftsman` directory under `root`: gate states, markers, error logs."""
    found: dict[str, bytes] = {}
    for directory, _, names in os.walk(root):
        if "craftsman" in Path(directory).parts:
            for name in names:
                path = os.path.join(directory, name)
                with contextlib.suppress(OSError):
                    found[path] = Path(path).read_bytes()
    return found


class Layouts:
    """Work trees in every state the fast path must tell apart, built once for the module."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.home = tmp / "home"
        self.home.mkdir()
        self.plain = tmp / "plain"
        (self.plain / "sub").mkdir(parents=True)
        self.idle = self.repo("idle")
        (self.idle / "pkg" / "deep").mkdir(parents=True)
        self.inactive = self.repo("inactive", inactive_state())
        self.write(self.inactive / ".git" / "craftsman" / "review_hooks.json", b'{"marker": true}\n')
        self.write(self.inactive / ".git" / "craftsman" / "hook-errors.log", b"")
        self.inactive_minimal = self.repo(
            "inactive-minimal",
            {key: value for key, value in inactive_state().items() if key != "evidence_seen"},
        )
        self.mine = self.repo("active-mine", active_state(GateOwner("claude", SESSION)))
        self.other = self.repo("active-other", active_state(GateOwner("claude", "other-session")))
        self.unnamed = self.repo("active-unnamed", active_state(GateOwner("claude")))
        self.cli_owned = self.repo("active-cli", active_state(GateOwner("cli")))
        self.broken: dict[str, Path] = {
            "not json": self.repo("broken-text", b"{not json"),
            "an array": self.repo("broken-array", b"[]"),
            "a byte-order mark": self.repo(
                "broken-bom", b"\xef\xbb\xbf" + json.dumps(inactive_state()).encode()
            ),
            "not UTF-8": self.repo("broken-bytes", b'{"active": false, "task": "\xff"}'),
            "no owner": self.repo("broken-owner", inactive_state(owner=None)),
            "a blank session id": self.repo(
                "broken-session", inactive_state(owner={"platform": "claude", "session_id": " "})
            ),
            "an unknown key": self.repo("broken-extra", inactive_state(extra=1)),
            "a missing key": self.repo(
                "broken-missing", {k: v for k, v in inactive_state().items() if k != "task"}
            ),
            "active as text": self.repo("broken-active", inactive_state(active="false")),
            "a negative count": self.repo("broken-count", inactive_state(forced_continuations=-1)),
            "an empty base": self.repo("broken-base", inactive_state(base_sha="")),
            "a 4.x state": self.repo("older", {"tier": "standard", "active": False}),
        }
        self.state_dir = self.repo("state-dir")
        (self.state_dir / ".git" / "craftsman" / "craftsman_state.json").mkdir(parents=True)
        self.main = EvidenceRepo(tmp / "main").root
        self.linked_active = self.worktree("linked-active", active_state(GateOwner("claude", SESSION)))
        self.linked_inactive = self.worktree("linked-inactive", inactive_state())
        self.linked_idle = self.worktree("linked-idle")
        self.bad_dot_git = tmp / "bad-dot-git"
        self.write(self.bad_dot_git / ".git", b"not a pointer\n")
        self.link_idle = self.symlink(tmp / "link-idle", self.idle)
        self.link_mine = self.symlink(tmp / "link-mine", self.mine)
        self.work_tree_config = self.repo("work-tree-config")
        GitRepo(self.work_tree_config).git("config", "core.worktree", str(self.plain))
        self.outer = self.repo("outer", active_state(GateOwner("claude", SESSION)))
        self.inner = self.outer / "vendor" / "inner"
        self.inner.mkdir(parents=True)
        GitRepo(self.inner)
        # Git keeps the blank after `gitdir: `: this work tree's gate is in ` x.git`, not `x.git`.
        self.blank_pointer = tmp / "blank-pointer"
        self.blank_pointer.mkdir()
        separate = self.blank_pointer / " x.git"
        subprocess.run(
            ["git", "init", "-q", "--separate-git-dir", str(separate), str(self.blank_pointer)],
            check=True,
        )
        self.write(
            separate.joinpath(*STATE), json.dumps(active_state(GateOwner("claude", SESSION))).encode()
        )
        self.write(self.blank_pointer / ".git", b"gitdir:  x.git\n")
        # A key after a section header on the same line moves the work tree into the active gate.
        self.header_key = self.repo("header-key")
        self.append(self.header_key / ".git" / "config", f"[core][core] worktree = {self.mine}\n")
        # Claude Code names its work-tree branches `worktree-<name>`: no reason to defer.
        self.worktree_branch = self.repo("worktree-branch")
        self.append(
            self.worktree_branch / ".git" / "config",
            '[branch "worktree-agent-1"]\n\tremote = origin\n\tmerge = refs/heads/worktree-agent-1\n',
        )

    def repo(self, name: str, state: dict[str, Any] | bytes | None = None) -> Path:
        root = self.tmp / name
        root.mkdir()
        GitRepo(root)
        if state is not None:
            data = state if isinstance(state, bytes) else json.dumps(state).encode()
            self.write(root.joinpath(".git", *STATE), data)
        return root

    def worktree(self, name: str, state: dict[str, Any] | None = None) -> Path:
        root = self.tmp / name
        subprocess.run(
            ["git", "worktree", "add", "-q", "--detach", str(root)], cwd=self.main, check=True
        )
        if state is not None:
            self.write(self.main.joinpath(".git", "worktrees", name, *STATE), json.dumps(state).encode())
        return root

    @staticmethod
    def symlink(link: Path, target: Path) -> Path | None:
        """`link` to the directory `target`; None where symlinks need privileges this process lacks."""
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError:
            return None
        return link

    @staticmethod
    def write(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Git for Windows marks a `.git` file hidden, and Windows refuses to open a hidden file for overwriting.
        path.unlink(missing_ok=True)
        path.write_bytes(data)

    @staticmethod
    def append(path: Path, text: str) -> None:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(text)


def _relative(name: str, path: Path) -> dict[str, str]:
    """`{name: path relative to the working directory}`, or nothing where none exists (another drive on Windows)."""
    try:
        return {name: os.path.relpath(path)}
    except ValueError:
        return {}


class FastPathCase(unittest.TestCase):
    layouts: Layouts
    environ: dict[str, str]

    @classmethod
    def setUpClass(cls) -> None:
        tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(tmp.cleanup)
        cls.layouts = Layouts(Path(tmp.name).resolve())
        cls.environ = hook_environ(cls.layouts.home)

    def allows(self, payload: dict[str, Any] | str, environ: dict[str, str] | None = None) -> bool:
        text = payload if isinstance(payload, str) else json.dumps(payload)
        return hook_fast.allows(text, self.environ if environ is None else environ)


class EquivalenceTests(FastPathCase):
    def test_whenever_the_fast_path_answers_the_full_handler_allows_and_changes_nothing(self) -> None:
        places = self.places()
        payloads = self.payloads()
        answered: set[str] = set()
        for (where, cwd), (label, platform, build) in itertools.product(places.items(), payloads):
            payload = build(cwd)
            text = json.dumps(payload)
            if not self.allows(text):
                continue
            answered.add(where)
            with self.subTest(where=where, payload=label):
                before = craftsman_files(self.layouts.tmp)
                code, out = run_full(platform, text, self.environ)
                self.assertEqual((code, out), (0, hook_fast.ALLOW))
                self.assertEqual(craftsman_files(self.layouts.tmp), before)
        # The fast path is no fast path unless it answers in the ordinary places.
        self.assertGreaterEqual(
            answered,
            {
                "plain",
                "idle root",
                "idle subdirectory",
                "inactive gate",
                "inactive gate of an older state shape",
                "linked work tree, inactive",
                "linked work tree, idle",
                "symlink to an idle repository",
                "missing directory in an idle repository",
                "a branch named worktree-...",
            },
        )

    def places(self) -> dict[str, str]:
        layouts = self.layouts
        return {
            "plain": str(layouts.plain / "sub"),
            "idle root": str(layouts.idle),
            "idle subdirectory": str(layouts.idle / "pkg" / "deep"),
            "inactive gate": str(layouts.inactive),
            "inactive gate of an older state shape": str(layouts.inactive_minimal),
            "linked work tree, inactive": str(layouts.linked_inactive),
            "linked work tree, idle": str(layouts.linked_idle),
            **({"symlink to an idle repository": str(layouts.link_idle)} if layouts.link_idle else {}),
            "missing directory in an idle repository": str(layouts.idle / "no" / "such"),
            **_relative("relative path", layouts.idle),
            "active gate": str(layouts.mine),
            "linked work tree, active": str(layouts.linked_active),
            **({"symlink to an active gate": str(layouts.link_mine)} if layouts.link_mine else {}),
            "inside .git": str(layouts.idle / ".git"),
            "nested in an active gate": str(layouts.inner),
            "gitdir pointer with a blank git keeps": str(layouts.blank_pointer),
            "work tree set after a section header": str(layouts.header_key),
            "a branch named worktree-...": str(layouts.worktree_branch),
            **{f"state: {problem}": str(root) for problem, root in layouts.broken.items()},
        }

    @staticmethod
    def payloads() -> list[tuple[str, str, Any]]:
        def claude(tool: str, tool_input: object = None, **extra: Any) -> Any:
            return lambda cwd: claude_call(cwd, tool, tool_input, **extra)

        def copilot(tool: str, tool_args: object = None, **extra: Any) -> Any:
            return lambda cwd: copilot_call(cwd, tool, tool_args, **extra)

        return [
            ("Read", "claude", claude("Read", {"file_path": "/etc/passwd"})),
            ("Grep", "claude", claude("Grep", {"pattern": "x", "path": "/"})),
            ("Glob without input", "claude", claude("Glob")),
            ("LS of the ledger", "claude", claude("LS", {"path": ".git/craftsman"})),
            (
                "Read by a subagent",
                "claude",
                claude("Read", {}, agent_type="general-purpose", agent_id="a1"),
            ),
            ("WebFetch", "claude", claude("WebFetch", {"url": "https://example.com"})),
            ("TodoWrite", "claude", claude("TodoWrite", {"todos": []})),
            ("tool name in other case", "claude", claude(" read ", {"file_path": "x"})),
            ("input as a JSON string", "claude", claude("Read", '{"file_path": "x"}')),
            ("empty input", "claude", claude("Read", "")),
            ("copilot read_file", "copilot", copilot("read_file", '{"filePath": "/etc/passwd"}')),
            ("copilot grep_search", "copilot", copilot("grep_search", {"query": "x"})),
            ("copilot agent", "copilot", copilot("semantic_search", None, agentName="Plan")),
        ]


class DeferTests(FastPathCase):
    """The cases the full handler has to see: the fast path stays out of them."""

    def assertDefers(self, payload: dict[str, Any] | str, environ: dict[str, str] | None = None) -> None:
        self.assertFalse(self.allows(payload, environ), payload)

    def test_an_active_gate_whoever_owns_it(self) -> None:
        layouts = self.layouts
        for root in (
            layouts.mine,
            layouts.other,
            layouts.unnamed,
            layouts.cli_owned,
            layouts.linked_active,
        ):
            with self.subTest(root=root.name):
                self.assertDefers(claude_call(str(root)))
                self.assertDefers(claude_call(str(root / "sub" / "dir")))

    def test_a_craftsman_agent_in_any_name_field(self) -> None:
        names = (
            "craftsman",
            "craftsman-reviewer",
            "craftsman:craftsman-reviewer",
            "CRAFTSMAN-VERIFIER",
            " Craftsman-Reviewer ",
            "other:craftsman-reviewer",
        )
        for key, name in itertools.product(hook_fast.AGENT_KEYS, names):
            with self.subTest(key=key, name=name):
                self.assertDefers(claude_call(str(self.layouts.idle), **{key: name}))
                self.assertDefers(copilot_call(str(self.layouts.idle), **{key: name}))

    def test_a_tool_that_is_not_read_only(self) -> None:
        for tool in (
            "Bash",
            "Write",
            "Edit",
            "MultiEdit",
            "NotebookEdit",
            "run_in_terminal",
            "Frobnicate",
            "",
            "mcp__fs__read_file",
            "readers",
        ):
            with self.subTest(tool=tool):
                self.assertDefers(claude_call(str(self.layouts.idle), tool, {"file_path": "x"}))
        self.assertDefers({"cwd": str(self.layouts.idle), "tool_name": 5, "tool_input": {}})

    def test_a_tool_input_the_full_handler_rejects(self) -> None:
        for tool_input in ("not json", "[1]", '"text"', [1], 5, True, "{"):
            with self.subTest(tool_input=tool_input):
                self.assertDefers(claude_call(str(self.layouts.idle), "Read", tool_input))
        self.assertDefers({**claude_call(str(self.layouts.idle)), "toolArgs": "[]"})

    def test_a_missing_or_blank_cwd(self) -> None:
        for cwd in (None, "", "   ", 5, ["x"]):
            with self.subTest(cwd=cwd):
                self.assertDefers({**claude_call(None), **({} if cwd is None else {"cwd": cwd})})

    def test_input_that_is_not_a_json_object(self) -> None:
        for text in ("", "   ", "[]", "null", "{not json", '"text"', "[" * 100000):
            with self.subTest(text=text[:10]):
                self.assertDefers(text)

    def test_an_environment_that_moves_the_repository(self) -> None:
        for name in sorted(hook_fast.GIT_DISCOVERY_VARIABLES):
            with self.subTest(name=name):
                self.assertDefers(claude_call(str(self.layouts.idle)), {**self.environ, name: ""})
        for config in (
            {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "core.worktree",
                "GIT_CONFIG_VALUE_0": "/elsewhere",
            },
            {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "includeIf.gitdir:/x/.path",
                "GIT_CONFIG_VALUE_0": "y",
            },
            {"GIT_CONFIG_PARAMETERS": "'core.WorkTree'='/elsewhere'"},
            {"GIT_CONFIG_PARAMETERS": "'include.path'='/x'"},
        ):
            with self.subTest(config=config):
                self.assertDefers(claude_call(str(self.layouts.idle)), {**self.environ, **config})

    def test_a_work_tree_in_a_repository_config(self) -> None:
        for config in (
            b"[core]\n\tworktree = /x\n",
            b"[core] WorkTree=/x\n",
            b"\xef\xbb\xbf[core] worktree = /x\n",  # git skips a byte-order mark
            b"[core][core] worktree = /x\n",
            b'[core "x"]\t[core] worktree\n',
            b"[core]\r\n\tworktree\r\n",
            b"[extensions]\n\tworktreeConfig = true\n",
            b'[includeIf "gitdir:/x/"]\n\tpath = y\n',
            b"[include]\npath = y\n",
        ):
            with self.subTest(config=config):
                self.assertTrue(hook_fast._may_move_work_tree(config))
        for harmless in (
            b'[core]\n\thooksPath = /tmp/worktrees/include\n[remote "worktree"]\n\turl = x/worktree\n',
            b'[branch "worktree-agent-1"]\n\tmerge = refs/heads/worktree-agent-1\n',
            b"[core]\n\tworktreeish = 1\n",
        ):
            with self.subTest(harmless=harmless):
                self.assertFalse(hook_fast._may_move_work_tree(harmless))

    def test_git_takes_a_work_tree_only_from_the_repositorys_own_configuration(self) -> None:
        # Why the user's and the system's configuration need not be read.
        layouts = self.layouts
        global_config = layouts.tmp / "global-config"
        # Forward slashes: git reads a backslash in a config value as an escape.
        global_config.write_text(f"[core]\n\tworktree = {layouts.plain.as_posix()}\n", encoding="utf-8")
        environ = {**self.environ, "GIT_CONFIG_GLOBAL": str(global_config)}
        top = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], cwd=layouts.idle, env=environ,
            capture_output=True, encoding="utf-8", check=True, timeout=30,
        ).stdout.strip()  # fmt: skip
        self.assertEqual(Path(top).resolve(), layouts.idle)

    def test_an_unknown_home_directory(self) -> None:
        environ = {
            name: value for name, value in self.environ.items() if name not in ("HOME", "USERPROFILE")
        }
        self.assertDefers(claude_call(str(self.layouts.plain)), environ)
        self.assertDefers(
            claude_call(str(self.layouts.plain)), {**environ, "HOME": " ", "USERPROFILE": ""}
        )

    def test_a_transcript_root_under_a_home_it_cannot_find(self) -> None:
        payload = json.dumps(claude_call(str(self.layouts.idle)))
        for name in hook_fast.TRANSCRIPT_ROOT_VARIABLES:
            environ = {**self.environ, name: "~no-such-user-here/root"}
            with self.subTest(name=name):
                self.assertDefers(payload, environ)
                if os.name != "nt":  # Windows expands `~user` next to the current home instead of failing
                    # `Path.expanduser` fails there, and the full handler asks.
                    self.assertNotEqual(run_full("claude", payload, environ), (0, hook_fast.ALLOW))
                self.assertTrue(self.allows(payload, {**self.environ, name: str(self.layouts.home)}))

    def test_a_state_file_that_cannot_be_read_or_validated(self) -> None:
        for problem, root in {**self.layouts.broken, "a directory": self.layouts.state_dir}.items():
            with self.subTest(problem=problem):
                self.assertDefers(claude_call(str(root)))

    def test_git_entries_it_cannot_follow(self) -> None:
        layouts = self.layouts
        for where in (
            layouts.bad_dot_git,  # a `.git` file that is no `gitdir:` pointer
            layouts.link_mine,  # a symlink into an active gate: its real path is walked
            layouts.idle / ".git",  # a git directory itself: a work tree may be configured anywhere
            layouts.idle / ".git" / "refs",
            layouts.work_tree_config,  # `core.worktree`
            layouts.inner,  # inside a repository nested in an active gate
            layouts.blank_pointer,  # `gitdir:  x.git`: git reads ` x.git`
            layouts.header_key,  # `[core][core] worktree = ...`
        ):
            if where is None:
                continue  # no symlinks without the privilege to make them
            with self.subTest(where=where):
                self.assertDefers(claude_call(str(where)))

    def test_a_command_line_it_does_not_know(self) -> None:
        for argv in (
            ["pre-tool", "--platform=claude"],
            ["pre-tool", "--platform", "antigravity"],
            ["--platform", "claude", "pre-tool"],
            ["stop", "--platform", "claude"],
            ["pre-tool", "--platform", "claude", "--help"],
        ):
            with self.subTest(argv=argv):
                self.assertFalse(hook_fast.is_fast_command(argv))
        self.assertTrue(hook_fast.is_fast_command(["pre-tool", "--platform", "copilot"]))

    def test_a_gitdir_pointer_git_may_read_otherwise(self) -> None:
        where = self.layouts.tmp / "pointer"
        for pointer in (
            b"gitdir: x.git \n",  # a blank at either end, which git keeps
            b"gitdir:\tx.git\n",
            b"gitdir: x.git\xc2\xa0\n",
            b"gitdir: x\n.git\n",  # git reads every line
            b"gitdir: x\xff.git\n",  # not UTF-8
            b"gitdir: " + b"x/" * 2100 + b"\n",  # longer than the first line `named_git_dir` reads
            b"gitdir: \n",
        ):
            with self.subTest(pointer=pointer[:20]):
                self.layouts.write(where / ".git", pointer)
                self.assertDefers(claude_call(str(where)))
        self.layouts.write(where / ".git", b"gitdir: x.git\r\n")
        self.assertTrue(self.allows(claude_call(str(where))))


class HandOffTests(FastPathCase):
    def call(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """`craftsman hook pre-tool --platform claude` in-process, with `payload` on a byte stdin."""
        stdin = io.TextIOWrapper(io.BytesIO(json.dumps(payload).encode("utf-8")), encoding="utf-8")
        out = io.StringIO()
        with mock.patch.dict(os.environ, self.environ, clear=True), mock.patch("sys.stdin", stdin):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                code = cli_main(["hook", *ARGV["claude"]])
        return code, json.loads(out.getvalue())

    def test_a_deferred_call_reaches_the_full_handler_with_the_input_read_once(self) -> None:
        payload = claude_call(
            str(self.layouts.idle), "Bash", {"command": "rm .git/craftsman/ledger.json"}
        )
        code, out = self.call(payload)
        self.assertEqual(code, 0)
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(
            out["hookSpecificOutput"]["permissionDecisionReason"],
            command_policy.LEDGER_IS_WRITTEN_BY_THE_CLI,
        )

    def test_an_answered_call_never_loads_the_full_handler(self) -> None:
        with mock.patch.object(hooks, "main", side_effect=AssertionError("the full handler ran")):
            self.assertEqual(self.call(claude_call(str(self.layouts.idle))), (0, {}))

    def test_a_stdin_the_fast_path_cannot_read_is_left_to_the_full_handler(self) -> None:
        with mock.patch.object(hook_fast, "read_stdin", side_effect=OSError("stdin is gone")):
            with mock.patch.object(hooks, "main", wraps=hooks.main) as full:
                self.assertEqual(self.call(claude_call(str(self.layouts.idle))), (0, {}))
        full.assert_called_once_with(ARGV["claude"], stdin=None)


class DriftTests(unittest.TestCase):
    """What the fast module restates must stay equal to the code that decides."""

    def test_the_read_only_tools_are_the_command_policys(self) -> None:
        self.assertEqual(hook_fast.READ_ONLY_TOOL_NAMES, command_policy.READ_ONLY_TOOL_NAMES)
        for name in hook_fast.READ_ONLY_TOOL_NAMES:
            self.assertEqual(command_policy.tool_kind(name), "read", name)

    def test_the_gate_state_shape_is_the_gates(self) -> None:
        self.assertEqual(hook_fast.GATE_PLATFORMS, gate.GATE_PLATFORMS)
        self.assertEqual(hook_fast.STAGES, tuple(stage.value for stage in Stage))
        self.assertEqual(hook_fast.ISOLATION_EVIDENCES, records.ISOLATION_EVIDENCES)
        self.assertEqual(
            hook_fast.STATE_KEYS | hook_fast.OPTIONAL_STATE_KEYS,
            {item.name for item in fields(GateState)},
        )
        self.assertEqual(hook_fast.OWNER_KEYS, set(GateOwner("cli").to_json()))
        self.assertEqual(
            hook_fast.STATE_PATH, (gate.state_path_for(REPO_ROOT).parent.name, gate.STATE_FILENAME)
        )

    def test_the_hooks_predicates_are_the_scope_guards(self) -> None:
        for name in (
            "craftsman",
            "craftsman-reviewer",
            "craftsman:craftsman-verifier",
            " CRAFTSMAN ",
            "other:craftsman",
            "Plan",
            "",
            "craftsman-helper",
        ):
            self.assertEqual(hooks.is_craftsman_agent(name), scope.is_craftsman_agent(name), name)
        for tool in (
            "Bash",
            "Read",
            " grep ",
            "LS",
            "list_dir",
            "Write",
            "webfetch",
            "",
            "run_in_terminal",
        ):
            self.assertEqual(hooks.is_scope_checked_tool(tool), scope.is_scope_checked_tool(tool), tool)

    def test_a_state_the_fast_path_accepts_is_one_the_gate_reads_as_inactive(self) -> None:
        bad_values: list[object] = [
            None,
            "",
            " ",
            "x",
            "claude",
            "implement",
            0,
            -1,
            1,
            True,
            False,
            [],
            {},
            1.5,
        ]
        base = inactive_state()
        variants: list[dict[str, Any]] = [base, {**base, "extra": 1}]
        for key in base:
            variants.append({k: v for k, v in base.items() if k != key})
            variants.extend({**base, key: value} for value in bad_values)
        for owner_key in ("owner", "takeover_from"):
            variants.extend(
                {**base, owner_key: owner}
                for owner in (
                    {"platform": "copilot", "session_id": None},
                    {"platform": "cli"},
                    {"platform": "x", "session_id": None},
                    {"platform": "claude", "session_id": ""},
                    {"platform": "claude", "session_id": 5},
                    {"platform": "claude", "session_id": "s", "extra": 1},
                )
            )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "craftsman_state.json"
            accepted = 0
            for variant in variants:
                path.write_text(json.dumps(variant), encoding="utf-8")
                try:
                    loaded = load_state(path)
                    inactive = loaded is not None and not loaded.active
                except GateStateError:
                    inactive = False
                with self.subTest(variant=variant):
                    self.assertEqual(hook_fast.is_inactive_state(variant), inactive)
                accepted += inactive
        self.assertGreater(accepted, 10)

    def test_a_state_written_by_the_gate_is_accepted_once_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "craftsman_state.json"
            state = GateState.open(
                GateOwner("copilot", "s"), "b" * 40, True, "t", takeover_from=GateOwner("cli")
            )
            for current in (
                state,
                replace(state, active=False, stage=Stage.ABORTED, evidence_seen=3, question="q?"),
            ):
                save_state(path, current)
                raw = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(hook_fast.is_inactive_state(raw), not current.active)


def imported(code: str, *, cwd: Path, stdin: str = "") -> list[str]:
    """The modules a fresh interpreter holds after running `code`, which ends by printing them."""
    env = {**hook_environ(cwd), "PYTHONPATH": str(REPO_ROOT)}
    proc = subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            f"{code}\nimport sys, json\nsys.stderr.write(json.dumps(sorted(sys.modules)))",
        ],
        cwd=cwd,
        env=env,
        input=stdin,
        capture_output=True,
        encoding="utf-8",
        check=False,
        timeout=60,
    )
    modules: list[str] = json.loads(proc.stderr.splitlines()[-1])
    return modules


class ImportBudgetTests(unittest.TestCase):
    """Each command loads only what it needs (fresh interpreters, so nothing a test imported counts)."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = EvidenceRepo(Path(tmp.name).resolve() / "repo")

    def hook(self, payload: dict[str, Any]) -> list[str]:
        code = "from craftsman.cli import main\nmain(['hook', 'pre-tool', '--platform', 'claude'])"
        return imported(code, cwd=self.repo.root, stdin=json.dumps(payload))

    def test_a_fast_answer_loads_a_handful_of_craftsman_modules(self) -> None:
        modules = self.hook(claude_call(str(self.repo.root), "Read", {"file_path": "app.py"}))
        own = [name for name in modules if name.split(".")[0] == "craftsman"]
        self.assertNotIn("craftsman.hooks", own)
        self.assertLessEqual(len(own), 5, own)
        for heavy in ("argparse", "dataclasses", "subprocess"):
            self.assertNotIn(heavy, modules)

    def test_a_bash_call_does_not_load_the_commands_it_does_not_run(self) -> None:
        modules = self.hook(claude_call(str(self.repo.root), "Bash", {"command": "ls"}))
        self.assertIn("craftsman.hooks", modules)
        for unneeded in (
            "craftsman.doctor",
            "craftsman.install",
            "craftsman.review",
            "craftsman.packet",
            "craftsman.scope",
            "craftsman.antigravity",
            "craftsman.usage",
            "importlib.metadata",
        ):
            self.assertNotIn(unneeded, modules)

    def test_version_and_help_load_no_command(self) -> None:
        for args in (["--version"], ["--help"], []):
            with self.subTest(args=args):
                code = f"from craftsman.cli import main\ntry: main({args!r})\nexcept SystemExit: pass"
                own = [
                    name
                    for name in imported(code, cwd=self.repo.root)
                    if name.split(".")[0] == "craftsman"
                ]
                self.assertEqual(own, ["craftsman", "craftsman.cli", "craftsman.streams"])


if __name__ == "__main__":
    unittest.main()
