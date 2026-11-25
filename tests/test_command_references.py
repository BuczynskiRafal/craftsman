#!/usr/bin/env python3
"""Every `craftsman <subcommand> --flag` the shipped prompts and docs tell a model to run must exist.

A removed flag that a skill, agent template, rule or doc still mentions stalls or confuses the
agent that follows it. This scans them against the real argparse parsers.
"""

from __future__ import annotations

import contextlib
import functools
import io
import re
import unittest
from pathlib import Path

from craftsman.cli import COMMANDS
from craftsman.cli import main as cli_main

from tests.support import REPO_ROOT

# Words that follow `craftsman` in a code span without naming a subcommand or flag.
IGNORED_SUBCOMMANDS: frozenset[str] = frozenset()
# `--flags` shown as placeholders or belonging to a wrapped command, not to craftsman.
IGNORED_FLAGS: frozenset[str] = frozenset()

SCANNED = ("skills", "templates", "rules")
SCANNED_FILES = ("README.md",)
SCANNED_GLOBS = (("docs", "*.md"),)

_INLINE_RE = re.compile(r"`([^`\n]+)`")
_CUT_RE = re.compile(r"\s(?:\|{1,2}|&&|;|>{1,2}|#)\s|\$\(|\s2>")
_FLAG_RE = re.compile(r"--[A-Za-z][\w-]*")
_GROUP_RE = re.compile(r"\{([\w,-]+)\}")
_FLAG_VALUE_RE = re.compile(r"--[\w-]+ \{[^}]*\}")  # `--platform {a,b}` is not a subcommand group
_WORD_RE = re.compile(r"^[a-z][a-z-]*$")


def scanned_files() -> list[Path]:
    paths: list[Path] = []
    for folder in SCANNED:
        paths.extend(sorted((REPO_ROOT / folder).rglob("*.md")))
    paths.extend(REPO_ROOT / name for name in SCANNED_FILES)
    for folder, pattern in SCANNED_GLOBS:
        paths.extend(sorted((REPO_ROOT / folder).glob(pattern)))
    return paths


def invocations(text: str) -> list[tuple[int, list[str]]]:
    """`(line, tokens after "craftsman")` for each command shown in a code span or code block."""
    found: list[tuple[int, list[str]]] = []
    in_fence = False
    for number, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        spans = [line] if in_fence else _INLINE_RE.findall(line)
        for span in spans:
            command = span.strip().removeprefix("$ ").strip()
            if not command.startswith("craftsman "):
                continue
            command = _CUT_RE.split(command, maxsplit=1)[0]
            tokens = command.split()[1:]
            found.append((number, tokens))
    return found


@functools.cache
def help_text(path: tuple[str, ...]) -> str:
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        try:
            cli_main([*path, "--help"])
        except SystemExit:
            pass
    return out.getvalue()


def subcommand_choices(path: tuple[str, ...]) -> list[str]:
    if not path:
        return list(COMMANDS)
    usage = " ".join(help_text(path).split("\n\n", 1)[0].split())
    match = _GROUP_RE.search(_FLAG_VALUE_RE.sub("", usage))
    return match.group(1).split(",") if match else []


def problems_in(tokens: list[str]) -> list[str]:
    """What is wrong with one `craftsman ...` invocation, empty when it matches the parsers."""
    path: tuple[str, ...] = ()
    rest = list(tokens)
    if rest and not _WORD_RE.match(rest[0]) and not rest[0].startswith("-"):
        return []  # a placeholder such as `…` or `<command>`, not a subcommand
    while rest:
        word = rest[0]
        choices = subcommand_choices(path)
        if not choices or not _WORD_RE.match(word) or word in IGNORED_SUBCOMMANDS:
            break
        if word not in choices:
            return [f"unknown subcommand {word!r} after `craftsman {' '.join(path)}`".replace("  ", " ")]
        path = (*path, word)
        rest = rest[1:]
    helps = [help_text(path[:depth]) for depth in range(len(path) + 1)]
    problems: list[str] = []
    for token in rest:
        if token == "--":
            break
        for flag in _FLAG_RE.findall(token):
            if flag in IGNORED_FLAGS or flag == "--help":
                continue
            if not any(re.search(rf"(?<![\w-]){re.escape(flag)}(?![\w-])", text) for text in helps):
                problems.append(f"unknown flag {flag} for `craftsman {' '.join(path)}`".replace("  ", " "))
    return problems


class InvocationScanTests(unittest.TestCase):
    def test_spans_and_blocks_are_found(self) -> None:
        text = "Run `craftsman gate --deep` then:\n```bash\n$ craftsman review show | head\n```\n"

        self.assertEqual(
            invocations(text), [(1, ["gate", "--deep"]), (3, ["review", "show"])]
        )

    def test_problems_name_unknown_subcommands_and_flags(self) -> None:
        self.assertEqual(problems_in(["review", "cover", "--hunks", "H1", "--note", "n"]), [])
        self.assertEqual(problems_in(["gate", "--init", "--deep"]), [])
        self.assertEqual(problems_in(["run", "--kind", "test", "--", "pytest", "--nope"]), [])
        self.assertEqual(problems_in(["skill", "show", "x", "--lenses", "a"]), [])
        self.assertEqual(
            problems_in(["gate", "--init", "--tier", "deep"]), ["unknown flag --tier for `craftsman gate`"]
        )
        self.assertEqual(
            problems_in(["review", "ack", "S1"]), ["unknown subcommand 'ack' after `craftsman review`"]
        )
        self.assertEqual(
            problems_in(["review", "add", "--signal", "S1"]),
            ["unknown flag --signal for `craftsman review add`"],
        )

    def test_placeholders_are_not_commands(self) -> None:
        self.assertEqual(problems_in(["…"]), [])
        self.assertEqual(problems_in(["<command>", "--flag"]), [])


class ShippedTextTests(unittest.TestCase):
    def test_prompts_and_docs_only_use_commands_and_flags_that_exist(self) -> None:
        files = scanned_files()
        self.assertGreater(len(files), 20)
        seen = 0
        failures: list[str] = []
        for path in files:
            for number, tokens in invocations(path.read_text(encoding="utf-8")):
                seen += 1
                failures.extend(
                    f"{path.relative_to(REPO_ROOT)}:{number}: {problem}: craftsman {' '.join(tokens)}"
                    for problem in problems_in(tokens)
                )
        self.assertGreater(seen, 50)
        self.assertEqual(failures, [], "\n" + "\n".join(failures))


if __name__ == "__main__":
    unittest.main()
