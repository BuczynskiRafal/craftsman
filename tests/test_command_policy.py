"""Tests for the command policy (`craftsman.command_policy`): one decision per tool call, for every platform."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from craftsman.command_policy import (
    BRIEFS_ARE_FOR_SUBAGENTS,
    HOOKS_ARE_THE_PLATFORMS,
    IDENTITY_IS_THE_PLATFORMS,
    LEAD_ONLY,
    LEDGER_IS_WRITTEN_BY_THE_CLI,
    NO_RESTART,
    OWNER_CANNOT_TAKE_OVER,
    TRANSCRIPTS_ARE_THE_PLATFORMS,
    Decision,
    PolicyContext,
    ToolCall,
    arming_action,
    candidate_paths,
    craftsman_arguments,
    decide,
    default_transcript_roots,
    invocations,
    is_main_session,
    review_commands,
    tool_call,
    tool_kind,
)
from craftsman.diffscan import DiffScanError
from craftsman.shell import iter_simple_commands
from craftsman.worktree_guard import find_worktree_mutation

from tests.support import REPO_ROOT


class PolicyCase(unittest.TestCase):
    """A repository with a ledger directory and a transcript root; `shell` and `write` ask the policy."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        self.ledger = self.repo / ".git" / "craftsman"
        (self.ledger / "inbox").mkdir(parents=True)
        (self.ledger / "scratch").mkdir()
        self.transcripts = self.tmp / "projects"
        (self.transcripts / "p").mkdir(parents=True)
        self.git_calls = 0
        self.owns = False
        self.in_review: int | None = None

    def context(self) -> PolicyContext:
        def ledger_dir() -> Path | None:
            self.git_calls += 1
            return self.ledger

        return PolicyContext(
            ledger_dir=ledger_dir,
            owns_gate=lambda: self.owns,
            review_in_progress=lambda: self.in_review,
            transcript_roots=(self.transcripts,),
        )

    def shell(
        self, command: str, *, main: bool = True, role: str | None = None, session: str | None = "S1", cwd: Path | None = None
    ) -> Decision:
        call = ToolCall(
            "shell",
            str(cwd or self.repo),
            command=command,
            role=role,  # type: ignore[arg-type]
            main_session=main and role is None,
            session_id=session,
        )
        return decide(call, self.context())

    def write(self, *paths: str, tool: str = "Write", key: str = "file_path") -> Decision:
        call = tool_call(
            tool, {key: paths[0]} if len(paths) == 1 else {"files": list(paths)}, str(self.repo),
            role=None, main_session=True, session_id="S1",
        )  # fmt: skip
        return decide(call, self.context())

    def assertDenied(self, decision: Decision, message: str) -> None:
        self.assertEqual((decision.action, decision.reason), ("deny", message))

    def assertAllowed(self, decision: Decision) -> None:
        self.assertEqual(decision.action, "allow", decision.reason)


class CommandMatcherTests(unittest.TestCase):
    def test_the_craftsman_cli_is_recognized_in_every_spelling(self) -> None:
        for command in (
            "craftsman review cover",
            "craftsman.exe review cover",
            "/opt/bin/craftsman review cover",
            "python -m craftsman.cli review cover",
            "python3.12 -m craftsman.cli review cover",
            "scripts/craftsman-cli.py review cover",
            "python /x/scripts/craftsman-cli.py review cover",
            '"/usr/bin/python3" "/site-packages/craftsman/craftsman-cli.py" review cover',
            "env FOO=1 craftsman review cover",
            "uv run craftsman review cover",
            "uvx craftsman review cover",
            "bash -c 'craftsman review cover'",
        ):
            with self.subTest(command=command):
                self.assertEqual(review_commands(command), ("cover",))

    def test_text_that_merely_mentions_a_command_is_no_command(self) -> None:
        for command in (
            "echo craftsman review cover",
            'grep -n "craftsman review cover" docs/a.md',
            "git commit -m 'craftsman review prepare'",
            "cat craftsman",
            "python -c 'print(1)'",
        ):
            with self.subTest(command=command):
                self.assertEqual(review_commands(command), ())

    def test_craftsman_arguments_name_what_follows_the_cli(self) -> None:
        self.assertEqual(craftsman_arguments(["craftsman", "gate", "--init"]), ("gate", "--init"))
        self.assertIsNone(craftsman_arguments(["echo", "craftsman", "gate"]))
        self.assertIsNone(craftsman_arguments(["python", "-m", "craftsman.hooks", "stop"]))

    def test_arming_follows_the_subcommands(self) -> None:
        self.assertEqual(arming_action(("prepare",)), "arm")
        self.assertEqual(arming_action(("report", "check")), "arm")
        self.assertEqual(arming_action(("report",)), "touch")
        self.assertIsNone(arming_action(()))

    def test_the_main_session_is_a_call_without_a_role_an_agent_id_or_a_foreign_agent_name(self) -> None:
        self.assertTrue(is_main_session("", ""))
        self.assertTrue(is_main_session("craftsman", ""))
        self.assertTrue(is_main_session("craftsman:craftsman", ""))
        self.assertFalse(is_main_session("craftsman-reviewer", ""))
        self.assertFalse(is_main_session("", "a1"))
        self.assertFalse(is_main_session("Explore", ""))


class ToolCallTests(unittest.TestCase):
    def test_tools_are_told_apart_by_what_they_do(self) -> None:
        for name in ("Bash", "run_in_terminal", "powershell", "run_command"):
            self.assertEqual(tool_kind(name), "shell", name)
        for name in ("Read", "Grep", "Glob", "LS", "read_file", "grep_search", "list_dir", "view_file", "WebFetch"):
            self.assertEqual(tool_kind(name), "read", name)
        for name in ("Write", "Edit", "MultiEdit", "NotebookEdit", "editFiles", "replace_string_in_file",
                     "insert_edit_into_file", "create_file", "write_to_file", "apply_patch", "mcp__fs__save"):  # fmt: skip
            self.assertEqual(tool_kind(name), "write", name)

    def test_a_shell_calls_own_directory_is_applied(self) -> None:
        call = tool_call("Bash", {"command": "ls", "cwd": "sub"}, "/work", role=None, main_session=True, session_id="s")
        copilot = tool_call("run_command", {"CommandLine": "ls", "Cwd": "/other"}, "/work", role=None, main_session=True, session_id=None)

        self.assertEqual((call.command, call.cwd), ("ls", os.path.join("/work", "sub")))
        self.assertEqual((copilot.command, copilot.cwd), ("ls", "/other"))

    def test_path_arguments_are_found_whatever_their_spelling(self) -> None:
        self.assertEqual(candidate_paths({"file_path": "a.py", "content": "b.py"}), ("a.py",))
        self.assertEqual(candidate_paths({"notebook_path": "n.ipynb"}), ("n.ipynb",))
        self.assertEqual(candidate_paths({"filePath": "a", "path": "b", "Path": "c", "TargetFile": "d"}), ("a", "b", "c", "d"))
        self.assertEqual(candidate_paths({"files": ["a", "b"]}), ("a", "b"))
        self.assertEqual(candidate_paths({"files": [{"filePath": "a"}, {"path": "b"}]}), ("a", "b"))
        self.assertEqual(candidate_paths({"replacements": [{"filePath": "a", "oldString": "x"}]}), ("a",))
        self.assertEqual(candidate_paths({"edits": [{"file_path": "a"}], "file_path": "a"}), ("a",))
        self.assertEqual(candidate_paths({"path": "file:///tmp/x"}), ("/tmp/x",))
        self.assertEqual(candidate_paths({"query": "x", "url": "https://x"}), ())
        patch = "*** Begin Patch\n*** Update File: src/a.py\n@@\n*** Add File: b.py\n*** End Patch"
        self.assertEqual(candidate_paths({"input": patch}), ("src/a.py", "b.py"))


class HookForgeryTests(PolicyCase):
    def test_every_spelling_of_craftsman_hook_is_denied(self) -> None:
        for command in (
            "craftsman hook stop --platform claude",
            "craftsman.exe hook pre-tool --platform copilot",
            "python -m craftsman.cli hook stop --platform claude",
            "python3 -m craftsman hook stop",
            "python scripts/craftsman-cli.py hook prompt --platform claude",
            # The commands `craftsman install` and the plugin write, copied into the agent's shell.
            '"/usr/bin/python3" "/usr/lib/python3/site-packages/craftsman/craftsman-cli.py" hook stop --platform claude',
            "'/opt/my venv/bin/python' '/opt/my venv/lib/craftsman/craftsman-cli.py' hook pre-tool --platform copilot",
            "python3 ~/.local/lib/python3.12/site-packages/craftsman/craftsman-cli.py hook stop",
            '"$CLAUDE_PLUGIN_ROOT/bin/craftsman-cli.py" hook stop --platform claude',
            "python3 /plugin/bin/craftsman-cli.py hook stop --platform claude",
            "/usr/local/bin/craftsman hook stop",
            "env FOO=1 craftsman hook stop",
            "env -u HOME craftsman hook stop",
            "uv run craftsman hook stop",
            "uv run python -m craftsman.cli hook stop",
            "cd /tmp && craftsman hook stop",
            "echo '{}' | craftsman hook stop --platform claude",
            "bash -c 'craftsman hook stop'",
            "sudo craftsman hook stop",
        ):
            with self.subTest(command=command):
                self.assertDenied(self.shell(command), HOOKS_ARE_THE_PLATFORMS)

    def test_the_entry_modules_and_guard_scripts_are_denied(self) -> None:
        for command in (
            "python -m craftsman.hooks stop --platform claude",
            "python -m craftsman.antigravity",
            "python craftsman/hooks.py stop",
            "python /repo/craftsman/antigravity.py",
            "scripts/craftsman-stop-gate.py",
            "python scripts/craftsman-stop-gate.py",
            "./scripts/craftsman-scope-guard.py",
            "python3 scripts/craftsman-scope-guard.py < payload.json",
        ):
            with self.subTest(command=command):
                self.assertDenied(self.shell(command), HOOKS_ARE_THE_PLATFORMS)

    def test_an_inline_program_that_imports_craftsman_is_denied(self) -> None:
        for command in (
            "python -c 'import craftsman'",
            "python3 -c 'from craftsman.hooks import main; main()'",
            "python -c 'import os, craftsman.ledger as l'",
            "python -c \"__import__('craftsman.hooks')\"",
            "python - <<'EOF'\nimport craftsman.cli\nEOF",
            "python3 <<EOF\nfrom craftsman import hooks\nEOF",
            "echo 'import craftsman' | python",
            "bash -c \"python -c 'import craftsman'\"",
        ):
            with self.subTest(command=command):
                self.assertDenied(self.shell(command), HOOKS_ARE_THE_PLATFORMS)

    def test_quoted_echoed_and_unrelated_text_is_not_a_hook_command(self) -> None:
        for command in (
            "echo craftsman hook stop",
            'echo "run craftsman hook stop yourself"',
            "grep -rn 'craftsman hook' README.md",
            "git commit -m 'document craftsman hook'",
            "cat craftsman/hooks.py",
            "python -c 'import os; print(os.getcwd())'",
            "python -m unittest tests.test_hooks",
            "python -m pytest tests/test_hooks.py",
            "python -c \"print('import craftsman')\"",
            "craftsman review check",
            "craftsman gate --complete",
            "ls hook",
            "python script.py hook",
        ):
            with self.subTest(command=command):
                self.assertAllowed(self.shell(command))

    def test_subagents_cannot_run_hooks_either(self) -> None:
        for role in ("reviewer", "verifier"):
            self.assertDenied(self.shell("craftsman hook stop --platform claude", role=role), HOOKS_ARE_THE_PLATFORMS)


class LedgerWriteTests(PolicyCase):
    def test_every_write_verb_and_redirection_into_the_ledger_directory_is_denied(self) -> None:
        ledger = self.ledger / "ledger.json"
        for command in (
            f"echo x > {ledger}",
            f"echo x >> {ledger}",
            f"echo x>{ledger}",
            f'echo x > "{ledger}"',
            f"echo x &> {ledger}",
            f"cat in.json > {ledger}",
            f"cat >> {ledger} <<EOF\nbody\nEOF",
            f"printf x | tee {ledger}",
            f"tee -a {ledger} < in",
            f"cp new.json {ledger}",
            f"cp -r new {self.ledger}/",
            f"cp -t {self.ledger} new.json",
            f"install -m 644 new.json {ledger}",
            f"mv new.json {ledger}",
            f"mv {ledger} /tmp/old.json",
            f"rm {ledger}",
            f"rm -rf {self.ledger}",
            f"ln -sf /tmp/x {ledger}",
            f"truncate -s 0 {ledger}",
            f"sed -i 's/a/b/' {ledger}",
            f"sed -i.bak 's/a/b/' {ledger}",
            f"sed --in-place -e 's/a/b/' {ledger}",
            f"dd if=/dev/zero of={ledger}",
            f"touch {self.ledger}/craftsman_state.json",
            f"echo x > {self.ledger}/review_hooks.json",
            f"echo x > {self.ledger}/hook-errors.log",
            f"echo x > {self.ledger}/ledger.v3.20261108T100000Z.bak.json",
            f"find {self.ledger} -name '*.json' -delete",
            f"Set-Content -Path {ledger} -Value x",
            f"Remove-Item {ledger}",
            f"bash -c 'echo x > {ledger}'",
            f"cd /tmp && echo x > {ledger}",
            f"cd {self.repo} && echo x > .git/craftsman/ledger.json",
            f"echo x > {self.repo}/./.git/../.git/craftsman/ledger.json",
            f"echo x > {self.repo}/x/../.git/craftsman/ledger.json",
        ):
            with self.subTest(command=command):
                self.assertDenied(self.shell(command), LEDGER_IS_WRITTEN_BY_THE_CLI)

    def test_a_cd_into_the_ledger_directory_makes_a_relative_write_a_ledger_write(self) -> None:
        for command in (
            "cd .git/craftsman && echo x > ledger.json",
            "cd .git/craftsman; sed -i s/a/b/ ledger.json",
            f"pushd {self.ledger} && tee ledger.json",
            "cd .git && cd craftsman && rm craftsman_state.json",
        ):
            with self.subTest(command=command):
                self.assertDenied(self.shell(command), LEDGER_IS_WRITTEN_BY_THE_CLI)

    def test_a_cd_that_cannot_be_followed_makes_the_ledger_file_names_decide(self) -> None:
        for name in ("ledger.json", "review_hooks.json", "craftsman_state.json", "hook-errors.log"):
            with self.subTest(name=name):
                command = f'cd "$(git rev-parse --absolute-git-dir)/craftsman" && echo x > {name}'
                self.assertDenied(self.shell(command), LEDGER_IS_WRITTEN_BY_THE_CLI)
        self.assertAllowed(self.shell('cd "$(git rev-parse --show-toplevel)" && echo x > notes.txt'))

    def test_the_same_file_names_elsewhere_are_ordinary_files(self) -> None:
        for command in ("echo x > ledger.json", "cp a.json ledger.json", f"echo x > {self.tmp}/hook-errors.log"):
            with self.subTest(command=command):
                self.assertAllowed(self.shell(command))

    def test_reading_the_ledger_stays_allowed(self) -> None:
        ledger = self.ledger / "ledger.json"
        for command in (
            f"cat {ledger}",
            f"head -c 100 {ledger}",
            f"grep -n round {ledger}",
            f"jq .rounds {ledger}",
            f"cp {ledger} /tmp/ledger-copy.json",
            f"ln -s {ledger} /tmp/link-to-ledger",
            f"sed -n 1,5p {ledger}",
            f"tail {self.ledger}/hook-errors.log",
            f"ls -la {self.ledger}",
            f"cat {ledger} | wc -l",
            f"cat {ledger} > /tmp/out.json",
            f"python -c \"import json; print(len(json.load(open('{ledger}'))['rounds']) > 1)\"",
            f"git -C {self.repo} status",
        ):
            with self.subTest(command=command):
                self.assertAllowed(self.shell(command))

    def test_the_inbox_and_scratch_folders_are_the_agents_to_write(self) -> None:
        for command in (
            f"echo '[]' > {self.ledger}/inbox/cover-1.json",
            f"Set-Content -LiteralPath {self.ledger}/inbox/add-1.json -Value x",
            f"cp batch.json {self.ledger}/inbox/",
            f"echo 'print(1)' > {self.ledger}/scratch/abc/repro.py",
            f"rm -rf {self.ledger}/scratch/abc",
        ):
            with self.subTest(command=command):
                self.assertAllowed(self.shell(command))

    def test_a_symlink_into_the_ledger_does_not_hide_the_write(self) -> None:
        link = self.repo / "notes"
        link.symlink_to(self.ledger)

        self.assertDenied(self.shell(f"echo x > {link}/ledger.json"), LEDGER_IS_WRITTEN_BY_THE_CLI)
        self.assertDenied(self.shell("echo x > notes/ledger.json"), LEDGER_IS_WRITTEN_BY_THE_CLI)
        self.assertAllowed(self.shell("cat notes/ledger.json"))

    def test_an_inline_program_that_names_the_ledger_and_can_write_is_denied(self) -> None:
        ledger = self.ledger / "ledger.json"
        for command in (
            f"python3 -c \"open('{ledger}', 'w').write('{{}}')\"",
            f"python3 -c \"open('{ledger}', mode='w')\"",
            f"python3 -c \"from pathlib import Path; Path('{ledger}').write_text('x')\"",
            f"python3 -c \"import os; os.remove('{ledger}')\"",
            f"python3 -c \"import shutil; shutil.copy('a', '{ledger}')\"",
            "python3 -c \"import json; json.dump({}, open('.git/craftsman/ledger.json', 'w'))\"",
            f"python - <<'EOF'\nopen('{ledger}', 'w').write('x')\nEOF",
            f"node -e \"require('fs').writeFileSync('{ledger}', 'x')\"",
            f"perl -e 'open(F, \">{ledger}\"); print F 1' ; echo | tee {ledger}",
        ):
            with self.subTest(command=command):
                self.assertDenied(self.shell(command), LEDGER_IS_WRITTEN_BY_THE_CLI)

    def test_an_inline_program_that_only_reads_the_ledger_is_allowed(self) -> None:
        ledger = self.ledger / "ledger.json"
        self.assertAllowed(self.shell(f"python3 -c \"import json; print(json.load(open('{ledger}')))\""))
        self.assertAllowed(self.shell(f"python3 -c \"print(open('{ledger}', 'r').read())\""))
        self.assertAllowed(self.shell(f"python3 - <<'EOF'\nprint(open('{ledger}').read())\nEOF"))

    def test_the_ledger_directory_is_asked_for_only_when_a_path_mentions_it(self) -> None:
        for command in ("echo x > notes.txt", "cp a b", "rm -rf build", "python -c 'print(1)'", "tee out.txt"):
            self.assertAllowed(self.shell(command))
        self.assertAllowed(self.write("a.py"))
        self.assertAllowed(self.write("src/deep/b.py", tool="Edit"))
        self.assertEqual(self.git_calls, 0)

        self.assertAllowed(self.shell(f"echo x > {self.repo}/craftsman/new.py"))  # mentions it: git is asked once
        self.assertEqual(self.git_calls, 1)

    def test_a_ledger_directory_that_cannot_be_found_leaves_the_lexical_check(self) -> None:
        def broken() -> Path | None:
            raise DiffScanError("git is gone")

        context = PolicyContext(ledger_dir=broken, transcript_roots=())
        denied = decide(ToolCall("shell", str(self.repo), command=f"echo x > {self.ledger}/ledger.json"), context)
        other = decide(ToolCall("shell", str(self.repo), command=f"echo x > {self.repo}/craftsman/a.py"), context)

        self.assertDenied(denied, LEDGER_IS_WRITTEN_BY_THE_CLI)
        self.assertEqual(other.action, "allow")

    def test_a_linked_work_trees_ledger_is_protected_by_its_path_alone(self) -> None:
        path = self.repo / ".git" / "worktrees" / "side" / "craftsman" / "ledger.json"

        self.assertDenied(self.shell(f"echo x > {path}"), LEDGER_IS_WRITTEN_BY_THE_CLI)

    def test_the_transcripts_of_the_platforms_are_protected_too(self) -> None:
        transcript = self.transcripts / "p" / "agent-1.jsonl"
        for command in (
            f"echo '{{}}' >> {transcript}",
            f"sed -i 's/C1/C9/' {transcript}",
            f"cp forged.jsonl {transcript}",
            f"rm {transcript}",
            f"python3 -c \"open('{transcript}', 'a').write('x')\"",
        ):
            with self.subTest(command=command):
                self.assertDenied(self.shell(command), TRANSCRIPTS_ARE_THE_PLATFORMS)
        self.assertAllowed(self.shell(f"cat {transcript}"))
        self.assertAllowed(self.shell(f"cp {transcript} /tmp/copy.jsonl"))

    def test_the_default_transcript_roots_cover_the_three_platforms(self) -> None:
        roots = default_transcript_roots({"CLAUDE_CONFIG_DIR": "/cfg", "ANTIGRAVITY_APP_DATA_DIR": "/agy"})
        text = {root.as_posix() for root in roots}

        self.assertTrue({"/cfg/projects", "/agy/brain"} <= text)
        self.assertTrue(any(item.endswith(".claude/projects") for item in text))
        self.assertTrue(any(item.endswith(".copilot/session-state") for item in text))
        self.assertTrue(any(item.endswith(".gemini/antigravity/brain") for item in text))


class WriteToolTests(PolicyCase):
    def test_file_writing_tools_cannot_reach_the_ledger_or_the_transcripts(self) -> None:
        ledger = str(self.ledger / "ledger.json")
        transcript = str(self.transcripts / "p" / "agent-1.jsonl")
        for tool, key in (
            ("Write", "file_path"),
            ("Edit", "file_path"),
            ("MultiEdit", "file_path"),
            ("NotebookEdit", "notebook_path"),
            ("replace_string_in_file", "filePath"),
            ("insert_edit_into_file", "filePath"),
            ("create_file", "filePath"),
            ("write_to_file", "TargetFile"),
            ("mcp__server__save", "path"),
        ):
            for path, message in ((ledger, LEDGER_IS_WRITTEN_BY_THE_CLI), (transcript, TRANSCRIPTS_ARE_THE_PLATFORMS)):
                with self.subTest(tool=tool, path=path):
                    self.assertDenied(self.write(path, tool=tool, key=key), message)

    def test_list_arguments_and_nested_replacements_are_checked(self) -> None:
        ledger = str(self.ledger / "ledger.json")
        edit_files = tool_call("editFiles", {"files": ["a.py", ledger]}, str(self.repo), role=None, main_session=True, session_id="S1")
        nested = tool_call("multi_replace", {"replacements": [{"filePath": ledger}]}, str(self.repo), role=None, main_session=True, session_id="S1")
        patch = tool_call(
            "apply_patch", {"input": f"*** Begin Patch\n*** Update File: {ledger}\n*** End Patch"}, str(self.repo),
            role=None, main_session=True, session_id="S1",
        )  # fmt: skip

        for call in (edit_files, nested, patch):
            self.assertDenied(decide(call, self.context()), LEDGER_IS_WRITTEN_BY_THE_CLI)

    def test_dot_dot_and_symlinks_are_resolved(self) -> None:
        link = self.repo / "docs"
        link.symlink_to(self.ledger)

        self.assertDenied(self.write("../repo/.git/craftsman/ledger.json"), LEDGER_IS_WRITTEN_BY_THE_CLI)
        self.assertDenied(self.write("a/../.git/craftsman/ledger.json"), LEDGER_IS_WRITTEN_BY_THE_CLI)
        self.assertDenied(self.write("docs/ledger.json"), LEDGER_IS_WRITTEN_BY_THE_CLI)
        self.assertDenied(self.write(str(link / "hook-errors.log")), LEDGER_IS_WRITTEN_BY_THE_CLI)

    def test_other_writes_reading_tools_and_the_agents_folders_are_allowed_without_asking_git(self) -> None:
        self.assertAllowed(self.write("a.py"))
        self.assertAllowed(self.write(str(self.repo / "src" / "b.py"), tool="Edit"))
        self.assertAllowed(self.write(str(self.tmp / "notes.md"), tool="NotebookEdit", key="notebook_path"))
        self.assertEqual(self.git_calls, 0)
        for tool in ("Read", "Grep", "Glob", "LS", "read_file", "grep_search"):
            call = tool_call(tool, {"file_path": str(self.ledger / "ledger.json"), "path": str(self.ledger)}, str(self.repo),
                             role=None, main_session=True, session_id="S1")  # fmt: skip
            self.assertAllowed(decide(call, self.context()))
        self.assertAllowed(self.write(str(self.ledger / "inbox" / "cover-1.json")))
        self.assertAllowed(self.write(str(self.ledger / "scratch" / "x" / "repro.py")))

    def test_the_protection_holds_for_subagents_and_without_any_gate(self) -> None:
        call = tool_call("Write", {"file_path": str(self.ledger / "ledger.json")}, str(self.repo), role="reviewer", main_session=False, session_id="S1")

        self.assertDenied(decide(call, PolicyContext(transcript_roots=())), LEDGER_IS_WRITTEN_BY_THE_CLI)


class IdentityTests(PolicyCase):
    def test_an_identity_variable_set_or_unset_for_a_craftsman_command_is_denied(self) -> None:
        for command in (
            "CLAUDE_CODE_SESSION_ID=other craftsman review check",
            "ANTIGRAVITY_CONVERSATION_ID=x ANTIGRAVITY_APP_DATA_DIR=/tmp craftsman review check",
            "CRAFTSMAN_SESSION_ID=x craftsman review check",
            "FOO=1 CRAFTSMAN_ANYTHING=1 craftsman gate --init",
            "env CLAUDE_CODE_SESSION_ID=other craftsman review check",
            "env -u CLAUDE_CODE_SESSION_ID craftsman review check",
            "env --unset=CLAUDE_CODE_SESSION_ID craftsman review check",
            "env -uCLAUDE_CODE_SESSION_ID craftsman review check",
            "export CLAUDE_CODE_SESSION_ID=other; craftsman review check",
            "export ANTIGRAVITY_CONVERSATION_ID=x && craftsman review check",
            "declare -x CRAFTSMAN_FOO=1; craftsman review check",
            "unset CLAUDE_CODE_SESSION_ID && craftsman review check",
            "unset ANTIGRAVITY_CONVERSATION_ID; craftsman gate --complete",
            "CLAUDE_CODE_SESSION_ID=other python -m craftsman.cli review check",
            "CLAUDE_CODE_SESSION_ID=other uv run craftsman review check",
            "$env:CLAUDE_CODE_SESSION_ID = 'other'; craftsman review check",
            "bash -c 'CLAUDE_CODE_SESSION_ID=other craftsman review check'",
        ):
            with self.subTest(command=command):
                self.assertDenied(self.shell(command), IDENTITY_IS_THE_PLATFORMS)

    def test_other_variables_and_other_programs_are_unaffected(self) -> None:
        for command in (
            "FOO=1 craftsman review check",
            "export FOO=1; craftsman review check",
            "env -u HOME craftsman review check",
            "CLAUDE_CODE_SESSION_ID=other pytest",
            "export CLAUDE_CODE_SESSION_ID=other",
            "echo CLAUDE_CODE_SESSION_ID=other craftsman review check",
            "echo $CLAUDE_CODE_SESSION_ID; craftsman review check",
            "craftsman review check --json",
        ):
            with self.subTest(command=command):
                self.assertAllowed(self.shell(command))


class GateCommandTests(PolicyCase):
    def test_the_owner_cannot_take_over_its_own_gate(self) -> None:
        self.owns = True

        for command in (
            "craftsman gate --init --takeover",
            "craftsman gate --init --takeover --task x --session-id S1",
            "craftsman gate --init --take",
            "python -m craftsman.cli gate --init --takeover",
        ):
            with self.subTest(command=command):
                self.assertDenied(self.shell(command), OWNER_CANNOT_TAKE_OVER)

    def test_a_session_that_does_not_own_the_gate_may_take_it_over(self) -> None:
        self.owns = False

        self.assertAllowed(self.shell("craftsman gate --init --takeover --task x"))

    def test_the_gate_is_asked_about_only_for_a_takeover(self) -> None:
        def explode() -> bool:
            raise AssertionError("the gate was asked about")

        context = PolicyContext(owns_gate=explode, transcript_roots=())
        call = ToolCall("shell", str(self.repo), command="craftsman gate --init --task x --deep", main_session=True, session_id="S1")

        self.assertEqual(decide(call, context).action, "allow")

    def test_another_sessions_id_is_denied_the_own_id_is_not(self) -> None:
        for command in (
            "craftsman gate --init --session-id other-session",
            "craftsman gate --init --session-id=other-session",
            "craftsman gate --session other-session --complete",
            "craftsman gate --await-user --session-id ''",
            "env FOO=1 craftsman gate --init --session-id X",
        ):
            with self.subTest(command=command):
                self.assertDenied(self.shell(command), IDENTITY_IS_THE_PLATFORMS)
        for command in (
            "craftsman gate --init --session-id S1 --deep --task \"fix the cart\"",
            "craftsman gate --init --session-id=S1",
            "craftsman gate --complete --session-id S1",
            "craftsman gate --await-user",
        ):
            with self.subTest(command=command):
                self.assertAllowed(self.shell(command))

    def test_a_payload_without_a_session_id_may_not_pass_one(self) -> None:
        self.assertDenied(self.shell("craftsman gate --init --session-id S1", session=None), IDENTITY_IS_THE_PLATFORMS)
        self.assertAllowed(self.shell("craftsman gate --init --task x", session=None))

    def test_the_stage_zero_command_of_the_prompt_hook_passes(self) -> None:
        from craftsman.gate import GateOwner, GateState
        from craftsman.hooks import init_command

        state = GateState.open(GateOwner("claude", "S1"), "a" * 40, True, 'fix "the" cart $HOME')

        self.assertAllowed(self.shell(init_command(state)))

    def test_text_that_mentions_the_options_is_not_a_gate_command(self) -> None:
        self.owns = True

        self.assertAllowed(self.shell("echo craftsman gate --init --takeover"))
        self.assertAllowed(self.shell("craftsman review check --takeover"))


class ReviewCommandTests(PolicyCase):
    def test_hunters_and_verifiers_cannot_re_plan_the_review(self) -> None:
        for role in ("reviewer", "verifier"):
            for command in ("craftsman review prepare", "craftsman review reset", "cd x && craftsman review prepare --deep"):
                with self.subTest(role=role, command=command):
                    self.assertDenied(self.shell(command, role=role), NO_RESTART)
            self.assertAllowed(self.shell("craftsman review cover --hunter W1-1 --hunks H1", role=role))
            self.assertAllowed(self.shell("craftsman review brief W1-1", role=role))

    def test_the_main_session_cannot_fetch_a_brief(self) -> None:
        self.assertDenied(self.shell("craftsman review brief W1-1"), BRIEFS_ARE_FOR_SUBAGENTS)
        self.assertAllowed(self.shell("craftsman review prepare"))

    def test_the_decision_names_the_review_commands_it_saw(self) -> None:
        self.assertEqual(self.shell("craftsman review prepare && craftsman review check").review, ("prepare", "check"))
        self.assertEqual(self.shell("ls").review, ())
        self.assertEqual(self.shell("craftsman review brief W1-1").review, ("brief",))  # a denial still reports it


class LeadOnlyTests(PolicyCase):
    def test_the_main_session_cannot_tag_a_record_with_a_slot(self) -> None:
        for command in (
            "craftsman review cover --hunter W1-1 --hunks H1 --note x",
            "craftsman review cover --hunter=W3-2 --files a.py",
            "craftsman review add --hunter W1-1 --lens intent --file a.py",
            "craftsman review add --lens intent --hunter=W2-1",
            "craftsman review add --hunt W1-1",
            "python -m craftsman.cli review cover --hunter W1-1 --hunks H1",
            "cd sub && craftsman review add --hunter W1-1",
            "craftsman review cover --hunter Lead --hunks H1",
        ):
            with self.subTest(command=command):
                self.assertDenied(self.shell(command), LEAD_ONLY)

    def test_lead_and_untagged_records_pass(self) -> None:
        for command in (
            "craftsman review cover --hunter lead --hunks H1 --note x",
            "craftsman review add --hunter=lead --lens intent",
            "craftsman review add --lens intent --file a.py",
            "craftsman review cover --hunks H1",
            "craftsman review verdict F1 --as proven --hunter W1-1",
            "echo craftsman review cover --hunter W1-1",
        ):
            with self.subTest(command=command):
                self.assertAllowed(self.shell(command))

    def test_subagents_are_unaffected(self) -> None:
        for role in ("reviewer", "verifier"):
            self.assertAllowed(self.shell("craftsman review cover --hunter W1-1 --hunks H1", role=role))
            self.assertAllowed(self.shell("craftsman review add --hunter W2-1 --lens intent", role=role))

    def test_a_batch_file_with_a_slot_entry_is_denied(self) -> None:
        batch = self.tmp / "batch.json"
        batch.write_text(json.dumps([{"hunks": "H1", "note": "x", "hunter": "lead"}, {"hunks": "H2", "hunter": "W1-1"}]))

        self.assertDenied(self.shell(f"craftsman review cover --from-json {batch}"), LEAD_ONLY)
        self.assertDenied(self.shell(f"craftsman review cover --from-json={batch}"), LEAD_ONLY)
        self.assertDenied(self.shell(f"craftsman review add --from {batch}"), LEAD_ONLY)

    def test_a_batch_file_of_lead_entries_or_none_passes(self) -> None:
        batch = self.tmp / "batch.json"
        batch.write_text(json.dumps([{"hunks": "H1", "hunter": "lead"}, {"hunks": "H2"}]))

        self.assertAllowed(self.shell(f"craftsman review cover --from-json {batch}"))

    def test_a_relative_batch_path_is_read_from_the_commands_directory(self) -> None:
        (self.repo / "batch.json").write_text(json.dumps([{"hunks": "H1", "hunter": "W1-1"}]))

        self.assertDenied(self.shell("craftsman review cover --from-json batch.json"), LEAD_ONLY)
        self.assertDenied(self.shell(f"cd {self.repo} && craftsman review cover --from-json batch.json", cwd=self.tmp), LEAD_ONLY)

    def test_a_batch_that_cannot_be_read_is_left_to_the_cli(self) -> None:
        broken = self.tmp / "broken.json"
        broken.write_text("{not json")

        for source in (str(self.tmp / "missing.json"), str(broken), "-", str(self.tmp)):
            with self.subTest(source=source):
                self.assertAllowed(self.shell(f"craftsman review cover --from-json {source}"))

    def test_an_oversized_batch_is_not_read(self) -> None:
        batch = self.tmp / "big.json"
        batch.write_text(json.dumps([{"hunks": "H1", "hunter": "W1-1"}]) + " " * (5 * 1024 * 1024))

        self.assertAllowed(self.shell(f"craftsman review cover --from-json {batch}"))


class EnvSplitStringTests(PolicyCase):
    """`env -S "<command line>"` runs the command line: every rule judges it exactly like the plain spelling."""

    def both(self, plain: str, *, role: str | None = None, main: bool = True) -> list[tuple[str, Decision]]:
        spellings = [
            plain,
            f"env -S '{plain}'",
            f'env -S "{plain}"',
            f"env -S'{plain}'",
            f"env --split-string='{plain}'",
            f"env --split-string '{plain}'",
            f"env -i -S '{plain}'",
            f"/usr/bin/env -S '{plain}'",
            f"/usr/bin/env --split-string='{plain}'",
            f"./env -S '{plain}'",
            f"env --s '{plain}'",  # GNU env takes the shortest unique prefix of a long option
            f"env --sp '{plain}'",
            f"env -a x -S '{plain}'",
        ]
        return [(spelling, self.shell(spelling, role=role, main=main)) for spelling in spellings]

    def assertSame(self, plain: str, message: str, *, role: str | None = None, main: bool = True) -> None:
        for spelling, decision in self.both(plain, role=role, main=main):
            with self.subTest(command=spelling):
                self.assertDenied(decision, message)

    def test_a_hook_command_is_denied(self) -> None:
        self.assertSame("craftsman hook stop --platform claude", HOOKS_ARE_THE_PLATFORMS)

    def test_a_ledger_write_is_denied(self) -> None:
        self.assertSame("rm -f .git/craftsman/ledger.json", LEDGER_IS_WRITTEN_BY_THE_CLI)

    def test_a_restarting_command_is_denied_to_a_role(self) -> None:
        self.assertSame("craftsman review prepare", NO_RESTART, role="reviewer")

    def test_a_work_tree_change_is_denied_to_a_subagent(self) -> None:
        for spelling, decision in self.both("git reset --hard", role="reviewer", main=False):
            with self.subTest(command=spelling):
                self.assertEqual(decision.action, "deny")
                self.assertIn("`git reset`", decision.reason)
                self.assertEqual(find_worktree_mutation(spelling, str(self.repo), subagent=True), "`git reset`")

    def test_an_identity_variable_inside_the_string_is_denied(self) -> None:
        for command in (
            'env -S "CLAUDE_CODE_SESSION_ID=other craftsman review check"',
            "env -S 'FOO=1 CLAUDE_CODE_SESSION_ID=other craftsman review check'",
            'env --split-string="ANTIGRAVITY_CONVERSATION_ID=x craftsman review check"',
            'env -S "env -S \'CRAFTSMAN_X=1 craftsman gate --complete\'"',
        ):
            with self.subTest(command=command):
                self.assertDenied(self.shell(command), IDENTITY_IS_THE_PLATFORMS)

    def test_an_harmless_string_is_allowed(self) -> None:
        for command in ("env -S 'FOO=1 craftsman review check'", 'env -S "git status"', "env -S 'echo hi'"):
            with self.subTest(command=command):
                self.assertAllowed(self.shell(command))

    def test_the_main_session_cannot_tag_a_slot_through_the_string(self) -> None:
        self.assertSame("craftsman review cover --hunter W1-1 --hunks H1 --note x", LEAD_ONLY)


def _runs_a_program(source: str) -> bool:
    """The module ends in a `__main__` block that does work (not the one that only says it is no program)."""
    block = source.partition('if __name__ == "__main__":')[2]
    return bool(block) and "is not a program" not in block


class EnvOptionSpellingTests(PolicyCase):
    """Every spelling of an `env` option that takes a value hides nothing: the policy judges the command behind it."""

    LEDGER = "craftsman/ledger.json"  # inside `.git`

    def assertLedgerDenied(self, *commands: str) -> None:
        for command in commands:
            with self.subTest(command=command):
                self.assertDenied(self.shell(command), LEDGER_IS_WRITTEN_BY_THE_CLI)

    def test_the_directory_of_env_decides_where_a_relative_path_points(self) -> None:
        self.assertLedgerDenied(
            f"env -C .git rm {self.LEDGER}",
            f"env -S '-C .git rm {self.LEDGER}'",
            f"env -S '-C.git rm {self.LEDGER}'",
            f"env -S '--chdir=.git rm {self.LEDGER}'",
            f"env -C.git rm {self.LEDGER}",
            f"env -iC .git rm {self.LEDGER}",
            f"env --chd .git rm {self.LEDGER}",
            f"env --chdir=.git rm {self.LEDGER}",
            f"timeout 5 env -C .git rm {self.LEDGER}",
            f"/usr/bin/env -S '-C .git rm {self.LEDGER}'",
        )

    def test_the_value_of_the_argv0_option_is_not_the_program(self) -> None:
        self.assertLedgerDenied(
            "env -a x rm -f .git/craftsman/ledger.json",
            "env -ax rm -f .git/craftsman/ledger.json",
            "env --argv0 x rm -f .git/craftsman/ledger.json",
            "env --argv0=x rm -f .git/craftsman/ledger.json",
            "env --a x rm -f .git/craftsman/ledger.json",
            "env -a x -S 'rm -f .git/craftsman/ledger.json'",
            "env -P /bin rm -f .git/craftsman/ledger.json",  # the BSD option for the search path
        )
        self.assertDenied(self.shell("env -a x craftsman hook stop --platform claude"), HOOKS_ARE_THE_PLATFORMS)
        self.assertDenied(self.shell("env --argv0 x craftsman hook stop --platform claude"), HOOKS_ARE_THE_PLATFORMS)

    def test_an_escaped_underscore_separates_the_words_of_the_string(self) -> None:
        self.assertLedgerDenied(
            "env -S 'rm\\_-f\\_.git/craftsman/ledger.json'",
            "env -S 'FOO=1\\_rm\\_-f\\_.git/craftsman/ledger.json'",
        )

    def test_the_unique_prefixes_of_the_long_options_are_the_options(self) -> None:
        for option in ("--s", "--sp", "--split", "--split-string"):
            self.assertLedgerDenied(f"env {option} 'rm -f .git/craftsman/ledger.json'")
        self.assertLedgerDenied("env --u HOME rm -f .git/craftsman/ledger.json", "env --un=HOME rm -f .git/craftsman/ledger.json")


class EntryModuleTests(PolicyCase):
    """Every module the package can run as a program is a command the policy reads (REVIEW-FSE 3)."""

    def test_every_module_with_a_main_block_is_a_recognized_entry_point(self) -> None:
        modules = sorted(
            path
            for path in (REPO_ROOT / "craftsman").glob("*.py")
            if _runs_a_program(path.read_text(encoding="utf-8"))
        )
        self.assertGreaterEqual({path.stem for path in modules}, {"cli", "hooks", "antigravity", "scope"})

        for path in modules:
            for command in (
                f"python3 -m craftsman.{path.stem} --init --takeover",
                f"python3 {path} --init --takeover",
                f"python3 craftsman/{path.name}",
            ):
                with self.subTest(command=command):
                    self.assertTrue(
                        invocations(iter_simple_commands(command)),
                        f"`{command}` runs craftsman/{path.name}, which the command policy does not read: "
                        "teach `command_policy` the module or drop its `__main__` block",
                    )

    def test_the_cli_module_run_as_a_script_is_the_cli(self) -> None:
        self.owns = True

        for command in ("python3 craftsman/cli.py gate --init --takeover", f"python3 {REPO_ROOT}/craftsman/cli.py gate --init --takeover"):
            with self.subTest(command=command):
                self.assertDenied(self.shell(command), OWNER_CANNOT_TAKE_OVER)

    def test_the_gate_and_export_modules_are_no_programs_and_say_so(self) -> None:
        # `craftsman/gate.py` used to end in a `__main__` block that ran the gate CLI around the command policy;
        # the policy reads one CLI (`craftsman gate ...`), so running the module fails loud and does nothing.
        for name in ("gate", "export"):
            for argv in (["-m", f"craftsman.{name}", "--init", "--takeover"], [str(REPO_ROOT / "craftsman" / f"{name}.py"), "--init"]):
                with self.subTest(argv=argv):
                    done = subprocess.run(
                        [sys.executable, *argv], cwd=self.repo, capture_output=True, text=True, env={**os.environ, "PYTHONPATH": str(REPO_ROOT)}
                    )
                    self.assertEqual((done.returncode, done.stdout), (2, ""))
                    self.assertIn(f"craftsman.{name} is not a program: run `craftsman {name} ...`", done.stderr)
                    self.assertFalse((self.ledger / "craftsman_state.json").exists())


class WorktreeGuardTests(PolicyCase):
    def test_a_subagent_never_changes_the_work_tree(self) -> None:
        decision = self.shell("git checkout main", role="reviewer")

        self.assertEqual(decision.action, "deny")
        self.assertIn("must not change the work tree", decision.reason)

    def test_the_main_session_is_asked_while_a_review_is_in_progress(self) -> None:
        self.assertAllowed(self.shell("git checkout main"))
        self.in_review = 2

        decision = self.shell("git checkout main")

        self.assertEqual(decision.action, "ask")
        self.assertIn("round 2", decision.reason)

    def test_a_denial_wins_over_a_question(self) -> None:
        self.in_review = 2

        self.assertDenied(self.shell("git checkout main && craftsman hook stop"), HOOKS_ARE_THE_PLATFORMS)


if __name__ == "__main__":
    unittest.main()


class PythonOptionTests(PolicyCase):
    """The interpreter's options must not hide the module or the script it runs (clusters, options with a value)."""

    FORGERIES = (
        "python -um craftsman.cli hook stop",
        "python -Im craftsman.hooks pre-tool",
        "python -Bm craftsman hook stop",
        "python -mcraftsman.hooks stop",
        "python -W ignore -m craftsman.hooks pre-tool",
        "python -W ignore -m craftsman.cli hook stop",
        "python -Wignore -m craftsman hook stop",
        "python3 -X utf8 -m craftsman.hooks stop",
        "python3 -X utf8 -m craftsman hook stop",
        "python -X dev -W error -B -m craftsman.cli hook stop",
        "python --check-hash-based-pycs always -m craftsman hook stop",
        "python -W ignore scripts/craftsman-cli.py hook stop",
        "python -X utf8 craftsman/hooks.py stop",
        "python -W ignore scripts/craftsman-stop-gate.py",
        "python -W ignore -c 'import craftsman'",
        "python -Ic 'import craftsman'",
        "python -- craftsman/hooks.py stop",
    )

    def test_a_hook_run_through_python_options_is_still_a_hook_run(self) -> None:
        for command in self.FORGERIES:
            with self.subTest(command=command):
                self.assertDenied(self.shell(command), HOOKS_ARE_THE_PLATFORMS)

    def test_the_review_commands_behind_python_options_are_seen(self) -> None:
        for command in (
            "python -W error -m craftsman.cli review prepare",
            "python -X utf8 -um craftsman review prepare",
            "python -I -m craftsman.cli review prepare",
        ):
            with self.subTest(command=command):
                self.assertEqual(review_commands(command), ("prepare",))
                self.assertDenied(self.shell(command, role="reviewer"), NO_RESTART)

    def test_other_modules_and_scripts_behind_python_options_stay_allowed(self) -> None:
        for command in (
            "python -W ignore -m unittest tests.test_hooks",
            "python -X utf8 -m pytest -q",
            "python -um pytest",
            "python -W ignore script.py hook",
            "python -W ignore -c 'print(1)'",
            "python -B -m craftsman.cli review check",
        ):
            with self.subTest(command=command):
                self.assertAllowed(self.shell(command))


class HeredocRedirectionTests(PolicyCase):
    """A redirection written after the heredoc operator is a write all the same."""

    def test_a_redirection_after_the_heredoc_operator_is_a_write(self) -> None:
        ledger = self.ledger / "ledger.json"
        for command in (
            f"cat <<EOF > {ledger}\nx\nEOF",
            f"cat <<'EOF' >{ledger}\nx\nEOF",
            f'cat <<"EOF" >> {ledger}\nx\nEOF',
            f"cat <<-EOF >{ledger}\n\tx\n\tEOF",
            "cat <<EOF >> .git/craftsman/ledger.json\nx\nEOF",
            f"cat <<A <<B > {ledger}\nx\nA\ny\nB",
            f"tee <<EOF {ledger}\nx\nEOF",
            f"cat <<EOF &> {ledger}\nx\nEOF",
        ):
            with self.subTest(command=command):
                self.assertDenied(self.shell(command), LEDGER_IS_WRITTEN_BY_THE_CLI)

    def test_the_transcripts_are_protected_the_same_way(self) -> None:
        transcript = self.transcripts / "p" / "agent-1.jsonl"

        self.assertDenied(self.shell(f"cat <<EOF >> {transcript}\n{{}}\nEOF"), TRANSCRIPTS_ARE_THE_PLATFORMS)

    def test_a_heredoc_that_writes_somewhere_else_or_only_names_the_ledger_in_its_body_is_allowed(self) -> None:
        ledger = self.ledger / "ledger.json"
        for command in (
            f"cat <<EOF > {self.tmp}/notes.txt\nx\nEOF",
            f"cat <<EOF\necho x > {ledger}\nEOF",
            f"cat <<'EOF' | wc -l\n{ledger}\nEOF",
            f"cat <<EOF > /dev/null\n{ledger}\nEOF",
        ):
            with self.subTest(command=command):
                self.assertAllowed(self.shell(command))

    def test_the_clobber_redirection_is_a_write(self) -> None:
        ledger = self.ledger / "ledger.json"
        transcript = self.transcripts / "p" / "agent-1.jsonl"
        for command in (f"echo x >| {ledger}", f"echo x >|{ledger}", f"echo x 2>| {ledger}", f"echo x >| {ledger} | cat"):
            with self.subTest(command=command):
                self.assertDenied(self.shell(command), LEDGER_IS_WRITTEN_BY_THE_CLI)
        self.assertDenied(self.shell(f"echo x >|{transcript}"), TRANSCRIPTS_ARE_THE_PLATFORMS)
        self.assertAllowed(self.shell(f"echo x >| {self.tmp}/out.txt"))
        self.assertAllowed(self.shell(f"cat {self.ledger}/ledger.json | wc -l"))


class TranscriptFileTests(PolicyCase):
    """Only the transcripts of a platform are protected, not everything under its transcript root."""

    def test_the_agents_own_files_below_a_transcript_root_are_writable(self) -> None:
        root = self.transcripts
        (root / "p" / "memory").mkdir()
        for path in (
            root / "p" / "memory" / "MEMORY.md",
            root / "p" / "memory" / "notes.md",
            root / "session-1" / "plan.md",
            root / "brain-1" / "task.md",
            root / "brain-1" / "implementation_plan.md",
            root / "brain-1" / "screenshot.png",
        ):
            with self.subTest(path=str(path)):
                for tool, key in (("Write", "file_path"), ("Edit", "file_path"), ("create_file", "filePath")):
                    self.assertAllowed(self.write(str(path), tool=tool, key=key))
                self.assertAllowed(self.shell(f"echo hi >> {path}"))
                self.assertAllowed(self.shell(f"sed -i 's/a/b/' {path}"))
                self.assertAllowed(self.shell(f"python3 -c \"open('{path}', 'w').write('x')\""))

    def test_transcript_files_and_the_folders_platforms_fill_are_protected(self) -> None:
        root = self.transcripts
        for path in (
            root / "p" / "session-1.jsonl",
            root / "p" / "AGENT-1.JSONL",
            root / "p" / "session-1" / "subagents" / "agent-x.jsonl",
            root / "p" / "session-1" / "subagents" / "agent-x.meta",
            root / "session-1" / "events.jsonl",
            root / "brain-1" / ".system_generated" / "steps" / "3" / "output.txt",
            root / "brain-1" / "transcript.jsonl",
            root / "p" / "new-forged.jsonl",
        ):
            with self.subTest(path=str(path)):
                self.assertDenied(self.write(str(path)), TRANSCRIPTS_ARE_THE_PLATFORMS)
                self.assertDenied(self.shell(f"echo x > {path}"), TRANSCRIPTS_ARE_THE_PLATFORMS)
        self.assertDenied(self.shell(f"rm -rf {root}"), TRANSCRIPTS_ARE_THE_PLATFORMS)

    def test_the_default_roots_leave_claudes_memory_to_the_agent(self) -> None:
        roots = default_transcript_roots({})
        context = PolicyContext(transcript_roots=roots)
        claude = next(root for root in roots if root.as_posix().endswith(".claude/projects"))

        memory = decide(tool_call("Write", {"file_path": str(claude / "-Users-x-Git-y" / "memory" / "MEMORY.md")}, str(self.repo),
                                  role=None, main_session=True, session_id="S1"), context)  # fmt: skip
        transcript = decide(tool_call("Write", {"file_path": str(claude / "-Users-x-Git-y" / "abc.jsonl")}, str(self.repo),
                                      role=None, main_session=True, session_id="S1"), context)  # fmt: skip

        self.assertAllowed(memory)
        self.assertDenied(transcript, TRANSCRIPTS_ARE_THE_PLATFORMS)


class OptionAbbreviationTests(PolicyCase):
    """argparse resolves an abbreviation among the options of the command it parses."""

    def test_the_tables_are_the_options_of_the_live_parsers(self) -> None:
        from craftsman import gate, review
        from craftsman.command_policy import ADD_OPTIONS, BRIEF_OPTIONS, COVER_OPTIONS, GATE_OPTIONS

        def long_options(parser: argparse.ArgumentParser) -> set[str]:
            return {option for action in parser._actions for option in action.option_strings if option.startswith("--")}

        sub = next(action for action in review._build_parser()._actions if isinstance(action, argparse._SubParsersAction))

        self.assertEqual(set(GATE_OPTIONS), long_options(gate._build_parser()))
        self.assertEqual(set(COVER_OPTIONS), long_options(sub.choices["cover"]))
        self.assertEqual(set(ADD_OPTIONS), long_options(sub.choices["add"]))
        self.assertEqual(set(BRIEF_OPTIONS), long_options(sub.choices["brief"]))

    def test_every_abbreviation_of_session_id_that_gate_accepts_names_a_session(self) -> None:
        for flag in ("--s", "--se", "--ses", "--sess", "--session", "--session-id"):
            with self.subTest(flag=flag):
                self.assertDenied(self.shell(f"craftsman gate --init {flag} FAKE"), IDENTITY_IS_THE_PLATFORMS)
                self.assertDenied(self.shell(f"craftsman gate --init {flag}=FAKE"), IDENTITY_IS_THE_PLATFORMS)
                self.assertAllowed(self.shell(f"craftsman gate --init {flag} S1"))

    def test_an_abbreviation_that_is_ambiguous_for_the_command_is_no_option(self) -> None:
        self.assertAllowed(self.shell("craftsman gate --init --t FAKE"))  # --takeover, --task
        self.assertDenied(self.shell("craftsman review cover --hunter W1-1 --h x"), LEAD_ONLY)
        self.assertAllowed(self.shell("craftsman review add --h W1-1"))  # ambiguous (--help, --hunter): the CLI rejects it
        self.assertDenied(self.shell("craftsman review add --hu W1-1"), LEAD_ONLY)

    def test_the_own_options_of_each_command_decide(self) -> None:
        self.assertAllowed(self.shell("craftsman review cover --hun W1-1 --hunks H1"))  # ambiguous for cover (--hunks, --hunter)
        self.assertDenied(self.shell("craftsman review cover --hunt W1-1"), LEAD_ONLY)
        self.assertDenied(self.shell("craftsman review add --hunt W1-1"), LEAD_ONLY)


class BatchOnTheCommandLineTests(PolicyCase):
    """The lead-only rule reads a batch wherever the command line puts it, with the CLI's own decoding."""

    ENTRY = [{"hunks": "H1", "note": "x", "hunter": "W1-1"}]

    def test_a_batch_on_stdin_is_read_from_the_command_line(self) -> None:
        body = json.dumps(self.ENTRY)
        for command in (
            f"craftsman review cover --from-json - <<'EOF'\n{body}\nEOF",
            f"craftsman review cover --from-json - <<EOF\n{body}\nEOF",
            f"echo '{body}' | craftsman review cover --from-json -",
            f"printf '%s' '{body}' | craftsman review add --from-json -",
            f'echo "{body.replace(chr(34), chr(92) + chr(34))}" | craftsman review cover --from-json -',
            f"printf '%s' '{body}' > /tmp/b.json && craftsman review cover --from-json /tmp/b.json",
        ):
            with self.subTest(command=command):
                self.assertDenied(self.shell(command), LEAD_ONLY)

    def test_a_batch_of_lead_entries_on_stdin_passes(self) -> None:
        body = json.dumps([{"hunks": "H1", "note": "x", "hunter": "lead"}, {"hunks": "H2", "note": "y"}])

        self.assertAllowed(self.shell(f"craftsman review cover --from-json - <<'EOF'\n{body}\nEOF"))
        self.assertAllowed(self.shell(f"echo '{body}' | craftsman review cover --from-json -"))
        self.assertAllowed(self.shell("cat batch.json | craftsman review cover --from-json -"))

    def test_slot_entries_in_a_command_that_is_no_batch_command_are_nothing(self) -> None:
        body = json.dumps(self.ENTRY)

        self.assertAllowed(self.shell(f"echo '{body}' > notes.json"))
        self.assertAllowed(self.shell(f"echo '{body}' | craftsman review verdict F1 --as proven"))
        self.assertAllowed(self.shell(f"craftsman review cover --from-json - <<'EOF'\n{body}\nEOF", role="reviewer"))

    def test_a_batch_file_is_decoded_like_the_cli_decodes_it(self) -> None:
        text = json.dumps([{"hunks": "H1", "hunter": "W1-1"}])
        for name, data in (
            ("bom.json", b"\xef\xbb\xbf" + text.encode("utf-8")),
            ("utf16.json", text.encode("utf-16")),
            ("utf16be.json", b"\xfe\xff" + text.encode("utf-16-be")),
        ):
            with self.subTest(name=name):
                (self.tmp / name).write_bytes(data)

                self.assertDenied(self.shell(f"craftsman review cover --from-json {self.tmp / name}"), LEAD_ONLY)
        (self.tmp / "lead.json").write_bytes(json.dumps([{"hunks": "H1", "hunter": "lead"}]).encode("utf-16"))
        self.assertAllowed(self.shell(f"craftsman review cover --from-json {self.tmp / 'lead.json'}"))


class CaseInsensitivePathTests(PolicyCase):
    def test_a_path_that_differs_only_in_case_is_the_same_place(self) -> None:
        for command in (
            "echo x > .git/CRAFTSMAN/ledger.json",
            "echo x > .GIT/craftsman/ledger.json",
            "echo x > .Git/Craftsman/Ledger.json",
            f"echo x > {str(self.ledger).upper()}/ledger.json",
            f"rm {str(self.ledger).replace('craftsman', 'Craftsman')}/ledger.json",
        ):
            with self.subTest(command=command):
                self.assertDenied(self.shell(command), LEDGER_IS_WRITTEN_BY_THE_CLI)
        for path in (".git/CRAFTSMAN/ledger.json", ".GIT/craftsman/ledger.json", str(self.ledger).upper() + "/ledger.json"):
            with self.subTest(path=path):
                self.assertDenied(self.write(path), LEDGER_IS_WRITTEN_BY_THE_CLI)
                self.assertDenied(self.write(path, tool="Edit"), LEDGER_IS_WRITTEN_BY_THE_CLI)
        self.assertDenied(self.write(str(self.transcripts / "P" / "Agent-1.JSONL")), TRANSCRIPTS_ARE_THE_PLATFORMS)
        self.assertDenied(self.shell(f"echo x > {str(self.transcripts).upper()}/p/agent-1.jsonl"), TRANSCRIPTS_ARE_THE_PLATFORMS)

    def test_the_agents_folders_are_the_same_in_any_case(self) -> None:
        self.assertAllowed(self.write(".git/CRAFTSMAN/INBOX/cover-1.json"))
        self.assertAllowed(self.shell("echo x > .git/craftsman/Scratch/a/repro.py"))


class LinkAndDownloadTests(PolicyCase):
    def test_a_hard_link_to_the_ledger_is_denied(self) -> None:
        ledger = self.ledger / "ledger.json"
        for command in (
            f"ln {ledger} /tmp/h",
            f"ln -f {ledger} /tmp/h",
            f"cp -l {ledger} /tmp/h2",
            f"cp -al {self.ledger} /tmp/h3",
            f"cp --link {ledger} /tmp/h2",
        ):
            with self.subTest(command=command):
                self.assertDenied(self.shell(command), LEDGER_IS_WRITTEN_BY_THE_CLI)

    def test_a_symbolic_link_or_a_copy_of_the_ledger_is_a_read(self) -> None:
        ledger = self.ledger / "ledger.json"
        for command in (f"ln -s {ledger} /tmp/l", f"ln -sf {ledger} /tmp/l", f"ln --symbolic {ledger} /tmp/l", f"cp {ledger} /tmp/c"):
            with self.subTest(command=command):
                self.assertAllowed(self.shell(command))

    def test_downloads_and_other_writers_into_the_ledger_are_denied(self) -> None:
        ledger = self.ledger / "ledger.json"
        for command in (
            f"curl -o {ledger} https://example.com/x",
            f"curl --output {ledger} https://example.com/x",
            f"curl -o{ledger} https://example.com/x",
            f"wget -O {ledger} https://example.com/x",
            f"wget --output-document={ledger} https://example.com/x",
            f"cat in | sponge {ledger}",
            f"gawk -i inplace '{{print}}' {ledger}",
            f"find {self.ledger} -exec rm {{}} +",
            f"find {self.ledger} -name '*.json' -exec mv {{}} /tmp \\;",
            f"find {self.ledger} -execdir truncate -s 0 {{}} \\;",
        ):
            with self.subTest(command=command):
                self.assertDenied(self.shell(command), LEDGER_IS_WRITTEN_BY_THE_CLI)
        for command in (
            f"curl -o /tmp/x https://example.com/x && cat {ledger}",
            f"find {self.ledger} -name '*.json' -exec cat {{}} \\;",
            f"find {self.ledger} -name '*.json'",
            "wget -O - https://example.com/x | cat > /tmp/y",
        ):
            with self.subTest(command=command):
                self.assertAllowed(self.shell(command))


class ReadingFalsePositiveTests(PolicyCase):
    def test_reading_programs_and_look_alike_names_are_allowed(self) -> None:
        ledger = self.ledger / "ledger.json"
        for command in (
            f"python -c \"import json; print(json.dumps(json.load(open('{ledger}'))))\"",
            "python3 -c \"import json; print(json.dumps(json.load(open('.git/craftsman/ledger.json')), indent=2))\"",
            "sed -i 's/a/b/' \"$REPO/craftsman/ledger.py\"",
            "echo hi > $D/ledger.py",
            "echo hi > $D/ledger_helpers.md",
            'cd "$(git rev-parse --show-toplevel)/craftsman" && sed -i s/a/b/ ledger.py',
            'cd "$(git rev-parse --show-toplevel)" && echo x > review_hooks.py',
        ):
            with self.subTest(command=command):
                self.assertAllowed(self.shell(command))

    def test_the_exact_ledger_file_names_still_decide_when_the_directory_is_unknown(self) -> None:
        for name in ("ledger.json", "review_hooks.json", "craftsman_state.json", "hook-errors.log", "hook-errors.log.1",
                     "ledger.v3.20261108T100000Z.bak.json"):  # fmt: skip
            with self.subTest(name=name):
                self.assertDenied(self.shell(f"echo x > $D/{name}"), LEDGER_IS_WRITTEN_BY_THE_CLI)

    def test_an_mcp_tool_is_classified_by_its_own_name(self) -> None:
        ledger = str(self.ledger / "ledger.json")
        for tool in ("mcp__fs__read_file", "mcp__fs__list_directory", "mcp__github__get_file_contents", "mcp__fs__search_files"):
            with self.subTest(tool=tool):
                self.assertEqual(tool_kind(tool), "read")
                self.assertAllowed(self.write(ledger, tool=tool, key="path"))
        for tool in ("mcp__fs__write_file", "mcp__fs__edit_file", "mcp__server__save"):
            with self.subTest(tool=tool):
                self.assertEqual(tool_kind(tool), "write")
                self.assertDenied(self.write(ledger, tool=tool, key="path"), LEDGER_IS_WRITTEN_BY_THE_CLI)
