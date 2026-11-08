"""Unit tests for the shell command parser (`craftsman.shell`)."""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest import mock

from craftsman.shell import (
    expand_shell_path_token,
    iter_simple_commands,
    parse_git_invocation,
    redirection_targets,
    split_shell_events,
)


class RedirectionTokenizationTests(unittest.TestCase):
    def test_unquoted_redirections_are_stripped_without_hiding_arguments(self) -> None:
        for command in ("git push 2>&1 origin main", "git push &> /dev/null origin main"):
            with self.subTest(command=command):
                cmds = iter_simple_commands(command)
                self.assertEqual(len(cmds), 1)
                self.assertEqual(cmds[0].tokens, ["git", "push", "origin", "main"])

    def test_glued_redirection_ends_token_without_consuming_digit_suffix(self) -> None:
        cmds_digit = iter_simple_commands("git push origin main2>/dev/null")
        self.assertEqual(cmds_digit[0].tokens, ["git", "push", "origin", "main2"])

        cmds_plain = iter_simple_commands("git push origin main>/dev/null")
        self.assertEqual(cmds_plain[0].tokens, ["git", "push", "origin", "main"])


class RedirectionTargetTests(unittest.TestCase):
    def targets(self, command: str) -> list[list[str]]:
        return [redirection_targets(item.segment) for item in iter_simple_commands(command)]

    def test_a_redirection_on_either_side_of_a_heredoc_operator_is_found(self) -> None:
        for command in (
            "cat > out.txt <<EOF\nx\nEOF",
            "cat <<EOF > out.txt\nx\nEOF",
            "cat <<'EOF' >out.txt\nx\nEOF",
            "cat <<-EOF >>out.txt\n\tx\n\tEOF",
            'cat <<"EOF" > "out.txt"\nx\nEOF',
        ):
            with self.subTest(command=command):
                self.assertEqual(self.targets(command), [["out.txt"]])
        self.assertEqual(self.targets("cat <<A <<B > out.txt\nx\nA\ny\nB")[0], ["out.txt"])

    def test_the_heredoc_body_is_not_scanned(self) -> None:
        self.assertEqual(self.targets("cat <<EOF\necho x > no.txt\nEOF"), [[]])

    def test_the_arguments_after_a_heredoc_operator_stay_in_the_command(self) -> None:
        self.assertEqual(iter_simple_commands("tee <<EOF out.txt\nx\nEOF")[0].tokens, ["tee", "out.txt"])
        self.assertEqual(iter_simple_commands("cat <<EOF > out.txt\nx\nEOF")[0].tokens, ["cat"])

    def test_the_clobber_redirection_is_one_redirection_not_a_pipe(self) -> None:
        for command in ("echo x >| out.txt", "echo x >|out.txt", "echo x 2>| out.txt"):
            with self.subTest(command=command):
                self.assertEqual(self.targets(command), [["out.txt"]])
        self.assertEqual(split_shell_events("echo x >| out.txt | cat"), ["echo x >| out.txt", "cat"])
        self.assertEqual(split_shell_events("echo a | cat"), ["echo a", "cat"])
        self.assertEqual(split_shell_events("echo \\>|cat"), ["echo \\>", "cat"])  # an escaped `>` is no redirection

    def test_descriptor_duplications_and_input_redirections_write_nothing(self) -> None:
        self.assertEqual(self.targets("cmd 2>&1 < in.txt >&2"), [[]])
        self.assertEqual(self.targets("echo '> no.txt' \"> no.txt\""), [[]])


class WorkingDirectoryTrackingTests(unittest.TestCase):
    def test_isolated_cd_or_popd_restores_original_cwd_for_following_command(self) -> None:
        repo_dir = "/workspace/repo"
        other_dir = "/workspace/other"
        for cmd in (
            "(cd /tmp && ls) && git status",
            "(cd ~ && ls) && git status",
            f"pushd {other_dir}; popd; git status",
        ):
            with self.subTest(cmd=cmd):
                cmds = iter_simple_commands(cmd, cwd_str=repo_dir)
                git_cmds = [c for c in cmds if c.tokens and c.tokens[0] == "git"]
                self.assertEqual(len(git_cmds), 1)
                self.assertEqual(git_cmds[0].cwd, repo_dir)

    def test_compound_separators_split_commands_cleanly(self) -> None:
        for cmd in (
            "echo `cd /tmp`; git status",
            "cd /tmp | true; git status",
            "cd /tmp & git status",
            "f() { cd /tmp; }; git status",
            "(case x in a) cd /tmp;; esac); git status",
        ):
            with self.subTest(cmd=cmd):
                cmds = iter_simple_commands(cmd, cwd_str="/workspace/repo")
                self.assertTrue(any(c.tokens == ["git", "status"] for c in cmds))

    def test_wrapper_chdir_updates_only_the_wrapped_command_cwd(self) -> None:
        cmds = iter_simple_commands(
            "env -C /tmp/sub rg foo && sudo --chdir=/var/log ls && pwd",
            cwd_str="/workspace/repo",
        )
        self.assertEqual(cmds[0].tokens, ["rg", "foo"])
        self.assertEqual(cmds[0].cwd, os.path.normpath("/tmp/sub"))
        self.assertEqual(cmds[1].tokens, ["ls"])
        self.assertEqual(cmds[1].cwd, os.path.normpath("/var/log"))
        self.assertEqual(cmds[2].tokens, ["pwd"])
        self.assertEqual(cmds[2].cwd, "/workspace/repo")


class WrapperAndNestedShellTests(unittest.TestCase):
    def test_wrapped_or_nested_shells_expose_inner_git_commands(self) -> None:
        cases: tuple[tuple[str, tuple[str | None, list[str]]], ...] = (
            ("bash -c 'git push origin main'", ("push", ["origin", "main"])),
            ("sh -lc 'git push origin master'", ("push", ["origin", "master"])),
            ("timeout 30 git push origin main", ("push", ["origin", "main"])),
            ("timeout -k 5 30 git push origin main", ("push", ["origin", "main"])),
            ("xargs git push origin <<< main", ("push", ["origin", "main"])),
            ("eval git push origin main", ("push", ["origin", "main"])),
            ("git -c alias.p=push p origin main", ("push", ["origin", "main"])),
            ('git -c alias.p="!git push" p origin main', ("push", ["origin", "main"])),
            ("git subtree push --prefix=sub origin main", ("push", ["--prefix=sub", "origin", "main"])),
            (r"find . -name '*.py' -exec env A=1 git checkout -- {} \;", ("checkout", ["--", "{}"])),
            ("fd -e py -x git restore {}", ("restore", ["{}"])),
            ("bash <<'EOF'\ngit status --short\nEOF", ("status", ["--short"])),
        )
        for cmd, expected in cases:
            with self.subTest(cmd=cmd):
                parsed = [
                    (sub, args)
                    for item in iter_simple_commands(cmd, cwd_str="/repo")
                    for sub, args, _ in (parse_git_invocation(item.tokens, item.cwd),)
                    if sub is not None
                ]
                self.assertIn(expected, parsed)

    def test_parse_git_invocation_tracks_global_c_directory_forms(self) -> None:
        for tokens, expected_cwd in (
            (["git", "-C", "/other", "status"], "/other"),
            (["git", "-C/other", "status"], "/other"),
            (["git", "-C=/other", "status"], "/other"),
            (["git", "--git-dir=/g", "status"], "/default"),
        ):
            with self.subTest(tokens=tokens):
                sub, args, cwd = parse_git_invocation(tokens, "/default")
                self.assertEqual((sub, args, cwd), ("status", [], expected_cwd))


class HeredocAndPathExpansionTests(unittest.TestCase):
    def test_split_shell_events_preserves_multiline_here_constructs(self) -> None:
        events = split_shell_events("cat <<'EOF'\na; b | c\nEOF\necho done")
        self.assertEqual(len(events), 2)
        self.assertTrue(events[0].startswith("cat <<'EOF'\n"))
        self.assertEqual(events[1], "echo done")

        ps_events = split_shell_events("@'\nx; y\n'@ | Set-Content out.txt")
        self.assertEqual(len(ps_events), 2)
        self.assertEqual(ps_events[0], "'x; y'")
        self.assertEqual(ps_events[1], "Set-Content out.txt")

    def test_windows_backslash_paths_stay_intact(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"HOME": "/Users/alice", "USERPROFILE": "/Users/alice"},
            clear=False,
        ):
            notes = f"{Path('/Users/alice')}/notes"  # the home in this platform's separators
            self.assertEqual(expand_shell_path_token("~/notes"), notes)
            self.assertEqual(expand_shell_path_token("$HOME/notes"), notes)
            self.assertEqual(expand_shell_path_token("%USERPROFILE%/notes"), notes)
            cmds = iter_simple_commands(r"cat ~\notes\todo.txt C:\temp\file.txt")
            self.assertEqual(cmds[0].tokens, [r"cat", r"~\notes\todo.txt", r"C:\temp\file.txt"])


if __name__ == "__main__":
    unittest.main()
