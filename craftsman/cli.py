#!/usr/bin/env python3
"""Unified CLI entrypoint for the Craftsman Agentic Engineering Framework.

A command's module is imported when the command runs (`COMMANDS`), so `--version`, the help and a hook
call load only what they need: hooks run on every tool call of every session with Craftsman installed.
"""

from __future__ import annotations

import importlib
import sys

from craftsman import __version__
from craftsman.streams import configure_std_streams

TYPE_CHECKING = False
if TYPE_CHECKING:
    import argparse

DOCTOR_HELP = "Audit skill manifests, core scripts, Git config, drift, and installed agent targets."
STATUS_HELP = "Show the work tree's quality gate, cumulative diff and review evidence."
SKILL_HELP = "Read bundled Craftsman skills and companion files without searching the disk."

# Command -> (module, function given the arguments after the command name, help); None is this module.
COMMANDS: dict[str, tuple[str | None, str, str]] = {
    "doctor": (None, "doctor_main", DOCTOR_HELP),
    "status": (None, "status_main", STATUS_HELP),
    "install": (
        "craftsman.install",
        "install_main",
        "Install Craftsman skills, rules, agents, and lifecycle hooks across platforms.",
    ),
    "uninstall": (
        "craftsman.uninstall",
        "uninstall_main",
        "Uninstall the Craftsman files recorded in install manifests.",
    ),
    "export": (
        "craftsman.export",
        "main",
        "Export skills and rules to Claude Code, GitHub Copilot, Codex, and Cursor.",
    ),
    "gate": (
        "craftsman.gate",
        "main",
        "Open, update and close the quality gate of the current work tree.",
    ),
    # `hook_fast` answers a read-only PreToolUse call itself and hands any other to `craftsman.hooks`.
    "hook": (
        "craftsman.hook_fast",
        "main",
        "Lifecycle hook for VS Code Copilot, the Copilot CLI and Claude Code.",
    ),
    "review": (
        "craftsman.review",
        "main",
        "Prepare review rounds and record findings, coverage and verdicts.",
    ),
    "run": (
        "craftsman.runner",
        "main",
        "Run a command and record its exit code in the evidence ledger.",
    ),
    "scope-guard": (
        "craftsman.scope",
        "main",
        "Enforce the search scope on Antigravity PreToolUse payloads.",
    ),
    "skill": (None, "skill_main", SKILL_HELP),
}


def doctor_main(argv: list[str]) -> int:
    import argparse
    from pathlib import Path

    from craftsman.doctor import run_doctor
    from craftsman.generated import ExportError
    from craftsman.manifest import InstallError
    from craftsman.paths import default_repo_dir

    parser = argparse.ArgumentParser(prog="craftsman doctor", description=DOCTOR_HELP)
    parser.add_argument("--repo-dir", type=Path, help="Path to the Craftsman repository root.")
    parser.add_argument("--json", action="store_true", help="Output diagnostic report as JSON.")
    parser.add_argument(
        "--workspace", type=Path, help="Also check the Copilot agents and skills of this project."
    )
    args = parser.parse_args(list(argv))
    try:
        return run_doctor(
            (args.repo_dir or default_repo_dir()).resolve(),
            as_json=args.json,
            workspace=args.workspace.resolve() if args.workspace else None,
        )
    except (ExportError, InstallError) as exc:
        sys.stderr.write(f"craftsman doctor: error: {exc}\n")
        return 1


def status_main(argv: list[str]) -> int:
    import argparse

    from craftsman.status import run_status

    parser = argparse.ArgumentParser(prog="craftsman status", description=STATUS_HELP)
    parser.add_argument("--cwd", help="A directory inside the work tree (default: the current one).")
    parser.add_argument("--json", action="store_true", help="Output status report as JSON.")
    args = parser.parse_args(list(argv))
    return run_status(args.cwd, as_json=args.json)


def _build_skill_parser() -> argparse.ArgumentParser:
    import argparse
    from pathlib import Path

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--repo-dir", type=Path, help="Path to the Craftsman repository or bundled assets root."
    )
    parser = argparse.ArgumentParser(
        prog="craftsman skill",
        description=SKILL_HELP,
    )
    sub = parser.add_subparsers(dest="subcommand", required=True)
    show_p = sub.add_parser(
        "show",
        parents=[common],
        help="Print a bundled SKILL.md or companion file (such as lenses.md) to stdout.",
    )
    show_p.add_argument("name", help="Skill name (e.g. craftsman, craft-review, simplify)")
    show_p.add_argument(
        "--file", dest="rel_file", help="Relative file inside the skill directory (e.g. lenses.md)"
    )
    show_p.add_argument(
        "--lenses",
        help=(
            "Comma-separated lens ids (craft-review only), e.g. intent,contracts"
        ),
    )
    list_p = sub.add_parser(
        "list",
        parents=[common],
        help="List bundled skill names and their one-sentence descriptions.",
    )
    list_p.add_argument("--json", action="store_true", help="Output skill list as JSON.")
    path_p = sub.add_parser(
        "path", parents=[common], help="Print the resolved directory path of a bundled skill."
    )
    path_p.add_argument("name", help="Skill name (e.g. craftsman, craft-review)")
    return parser


def skill_main(argv: list[str]) -> int:
    from craftsman.skill_reader import run_skill

    return run_skill(_build_skill_parser().parse_args(argv))


def main(argv: list[str] | None = None) -> int:
    configure_std_streams()
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in COMMANDS:
        module, function, _ = COMMANDS[args[0]]
        owner = sys.modules[__name__] if module is None else importlib.import_module(module)
        code: int = getattr(owner, function)(args[1:])
        return code
    import argparse

    parser = argparse.ArgumentParser(
        prog="craftsman", description="Make AI coding agents prove they reviewed and tested their change."
    )
    parser.add_argument("--version", action="version", version=f"craftsman {__version__}")
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")
    for name, (_, _, help_text) in COMMANDS.items():
        commands.add_parser(name, help=help_text)
    parser.parse_args(args)  # prints --version, help or the error for an unknown command
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
