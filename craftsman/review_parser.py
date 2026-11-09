"""The `craftsman review` command line: one argparse parser; the handler of each subcommand is injected."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from typing import Any, TypeVar

from craftsman.diffscan import (
    IGNORE_FILE,
)
from craftsman.evidence import (
    INBOX_KINDS,
)
from craftsman.lenses import LENS_IDS
from craftsman.records import (
    RESOLUTIONS,
    SEVERITIES,
    VERDICTS,
    as_choice,
)
from craftsman.review_common import PROG

S = TypeVar("S", bound=str)
Handler = Callable[[argparse.Namespace], int]


def literal_choice(choices: Sequence[S]) -> Callable[[str], S]:
    """argparse `type=` converter that narrows a string to one of the `Literal` choices."""

    def parse(value: str) -> S:
        chosen = as_choice(value, choices)
        if chosen is not None:
            return chosen
        raise argparse.ArgumentTypeError(f"invalid choice {value!r} (choose from {', '.join(choices)})")

    return parse


def _add_prepare_parser(sub: Any, common: argparse.ArgumentParser, handlers: Mapping[str, Handler]) -> None:
    prepare = sub.add_parser(
        "prepare",
        parents=[common],
        help="Snapshot the diff and open a round",
        description="Snapshot the diff since the base commit and open a review round.",
        epilog=(
            "Tool caches (__pycache__, .pytest_cache, node_modules, ...) never enter the diff. "
            f"List further paths to leave out, in .gitignore syntax, in a {IGNORE_FILE} file at the "
            "repository root."
        ),
    )
    prepare.add_argument(
        "--deep",
        action="store_true",
        help="Make the round deep: every lens is suggested (inside a gate it raises the gate to deep)",
    )
    prepare.add_argument("--base", help="Base commit (default: the active task's base or HEAD)")
    prepare.add_argument("--task", help="One-line description of what the change is for")
    prepare.add_argument(
        "--task-file",
        metavar="PATH",
        help="Read the description of what the change is for from a file (use - for stdin; at most 4000 characters)",
    )
    prepare.add_argument(
        "--paths", help="Comma-separated file or directory paths to freeze as the review scope"
    )
    prepare.add_argument(
        "--add-paths",
        help="Comma-separated file or directory paths to add to an existing review scope (e.g. files touched in --fix)",
    )
    prepare.add_argument(
        "--staged",
        action="store_true",
        help="Freeze the review scope to paths staged in the index (content still comes from the work tree)",
    )
    prepare.add_argument(
        "--tracked",
        action="store_true",
        help="Freeze the review scope to tracked files changed since the base (omit untracked files)",
    )
    prepare.set_defaults(handler=handlers["prepare"])


def _add_view_and_cover_parsers(sub: Any, common: argparse.ArgumentParser, handlers: Mapping[str, Handler]) -> None:
    view = sub.add_parser("view", parents=[common], help="Build a packet view of some files or hunks")
    view.add_argument("--files", help="Comma-separated changed file paths, e.g. pkg/a.py,pkg/b.py")
    view.add_argument("--hunks", help="Comma-separated hunk ids or ranges, e.g. H3-H17")
    view.set_defaults(handler=handlers["view"])

    scout = sub.add_parser(
        "scout",
        parents=[common],
        help="Base version, callers and tests for a hunk, a location or a symbol",
        description=(
            "Grounded context in at most 12,000 characters, no opinions: for hunks (H3, H3-H5, H3,H7) "
            "and for path:line, the base version of the enclosing scope, up to 3 callers per symbol "
            "it defines and up to 3 tests; for a symbol name, its definitions, references and tests."
        ),
    )
    scout.add_argument("target", metavar="TARGET", help="H3, H3-H5, H3,H7, path:line or a symbol name")
    scout.add_argument(
        "--base", help="Revision the base version comes from (default: the round's base, else HEAD)"
    )
    scout.set_defaults(handler=handlers["scout"])

    cover = sub.add_parser("cover", parents=[common], help="Record that hunks or files were reviewed")
    cover.add_argument("--hunks", help="Comma-separated hunk ids or ranges, e.g. H1,H3-H7")
    cover.add_argument(
        "--files", help="Comma-separated file paths; covers every open hunk of those files"
    )
    cover.add_argument("--note", help="What you examined and tried (one line, >= 20 characters)")
    cover.add_argument(
        "--hunter",
        metavar="SLOT",
        help="The hunt-plan slot this coverage answers (your brief names it; required in an enforced review)",
    )
    cover.add_argument(
        "--from-json",
        metavar="PATH",
        help='Bulk JSON list of {"hunks"|"files", "note", "hunter"?} objects (use - for stdin)',
    )
    cover.set_defaults(handler=handlers["cover"], subparser=cover)

    brief = sub.add_parser(
        "brief",
        parents=[common],
        help="The whole instruction of one hunter slot, or of a verifier",
        description=(
            "`brief SLOT` records that you took the slot, then prints your slice, lenses and the commands that "
            "record your work. `brief --verifier F1,F2` prints a verifier's instruction and records nothing."
        ),
    )
    brief.add_argument("slot", metavar="SLOT", nargs="?", help="Slot id from the hunt plan, e.g. W1-2")
    brief.add_argument("--verifier", metavar="IDS", help="Comma-separated finding ids to verify, e.g. F1,F2")
    brief.set_defaults(handler=handlers["brief"])


def _add_finding_parsers(
    sub: Any, common: argparse.ArgumentParser, lenses: Sequence[str], handlers: Mapping[str, Handler]
) -> None:
    add = sub.add_parser("add", parents=[common], help="Record a finding")
    add.add_argument("--lens", choices=lenses)
    add.add_argument("--file")
    add.add_argument("--line", type=int)
    add.add_argument("--severity", type=literal_choice(SEVERITIES))
    add.add_argument("--title")
    add.add_argument("--scenario", help="Trigger -> wrong outcome")
    add.add_argument("--suggestion", default=None)
    add.add_argument(
        "--hunter",
        metavar="SLOT",
        help="The hunt-plan slot that found it, or `lead` for the orchestrator's own (required in an enforced review)",
    )
    add.add_argument(
        "--from-json",
        metavar="PATH",
        help='Bulk JSON list of {"lens", "file", "line", "severity", "title", "scenario", "suggestion"?, "hunter"?} objects (use - for stdin)',
    )
    add.set_defaults(handler=handlers["add"], subparser=add)

    move = sub.add_parser(
        "move",
        parents=[common],
        help="Correct the line (or file) of a finding",
        description="Point a finding at the right line before it has a verdict, instead of "
        "recording a duplicate.",
    )
    move.add_argument("finding_id", metavar="ID")
    move.add_argument("--line", required=True, type=int)
    move.add_argument("--file", help="New file (default: keep the finding's file)")
    move.set_defaults(handler=handlers["move"])


def _add_merge_and_verdict_parsers(sub: Any, common: argparse.ArgumentParser, handlers: Mapping[str, Handler]) -> None:
    merge = sub.add_parser(
        "merge",
        parents=[common],
        help="Group duplicate findings into one root-cause finding (only when one fix removes every consequence)",
        description=(
            "Combine unjudged findings that share a root cause into a single finding "
            "(listing all consequences in its scenario) and dismiss the duplicates. "
            "Merge only when ONE fix removes every consequence. Different fixes are separate "
            "findings, even on the same line, file, or category (for example, a missing `--json` "
            "flag in `craftsman status` and a stale README example are two separate findings, not "
            "one 'housekeeping' finding)."
        ),
    )
    merge.add_argument("target_id", metavar="TARGET_ID", help="Finding id to keep (e.g. F1)")
    merge.add_argument(
        "duplicate_ids", metavar="DUP_ID", nargs="+", help="Duplicate finding ids to fold into TARGET_ID"
    )
    merge.add_argument("--title", help="Updated title for the grouped finding")
    merge.add_argument("--scenario", help="Combined root-cause -> consequences scenario")
    merge.add_argument("--suggestion", help="Updated suggestion for the grouped finding")
    merge.set_defaults(handler=handlers["merge"])

    verdict = sub.add_parser("verdict", parents=[common], help="Record a verdict on a finding")
    verdict.add_argument("finding_id", metavar="ID", nargs="?")
    verdict.add_argument("--as", dest="verdict", type=literal_choice(VERDICTS))
    verdict.add_argument("--evidence")
    verdict.add_argument("--run", help="Id of a recorded run that supports the verdict")
    verdict.add_argument(
        "--from-json",
        metavar="PATH",
        help='Bulk JSON list of {"id", "as", "evidence", "run"?} objects (use - for stdin)',
    )
    verdict.set_defaults(handler=handlers["verdict"], subparser=verdict)


def _add_resolution_and_status_parsers(sub: Any, common: argparse.ArgumentParser, handlers: Mapping[str, Handler]) -> None:
    resolution = sub.add_parser("resolve", parents=[common], help="Mark a finding fixed/accepted")
    resolution.add_argument("finding_id", metavar="ID")
    resolution.add_argument("--as", dest="resolution", required=True, type=literal_choice(RESOLUTIONS))
    resolution.add_argument("--note", required=True)
    resolution.set_defaults(handler=handlers["resolve"])

    waiver = sub.add_parser(
        "waive-run",
        parents=[common],
        help="Retire an obsolete failing run",
        description=(
            "Stop a failing run from blocking the gate when its command can never pass again, "
            "e.g. a mistyped or renamed test. The reason appears in the final report."
        ),
    )
    waiver.add_argument("run_id", metavar="ID")
    waiver.add_argument("--reason", required=True, help="Why the command is obsolete")
    waiver.set_defaults(handler=handlers["waive-run"])


def _add_inspection_parsers(sub: Any, common: argparse.ArgumentParser, handlers: Mapping[str, Handler]) -> None:
    for name, help_text in (
        ("show", "Show coverage, findings and runs"),
        ("check", "Evaluate the gate; exit 0 only when it passes"),
        ("report", "Final Markdown report for the user"),
    ):
        command = sub.add_parser(name, parents=[common], help=help_text)
        command.add_argument("--json", action="store_true")
        command.set_defaults(handler=handlers[name])
    sub.choices["show"].add_argument(
        "--packet-path",
        action="store_true",
        help="Print only the resolved absolute path of the current review packet",
    )
    sub.choices["show"].add_argument(
        "--hunks",
        action="store_true",
        help="Print the hunks still to cover (H# -> file:lines and scope or status) and how many are left",
    )
    for name in ("check", "report"):
        sub.choices[name].add_argument(
            "--review-only",
            action="store_true",
            help=(
                "Check only review completeness (round, coverage, verdicts), without requiring fixes or test runs; "
                "refused while a /craftsman gate is active in the checkout"
            ),
        )
    sub.choices["report"].add_argument(
        "--partial",
        action="store_true",
        help=(
            "Print a partial review-only report when some hunks or verdicts could not be completed. It ends "
            "nothing: the review guard stays armed and scratch/inbox files are kept until the review passes"
        ),
    )
    reset = sub.add_parser("reset", parents=[common], help="Archive the ledger and packets")
    reset.set_defaults(handler=handlers["reset"])


def build_parser(handlers: Mapping[str, Handler]) -> argparse.ArgumentParser:
    """The parser of `craftsman review`; `handlers` maps every subcommand name to the function that runs it."""
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--cwd", help="Directory inside the repository (default: current)")
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Record and inspect code review evidence in the Craftsman ledger.",
        allow_abbrev=False,
    )
    sub = parser.add_subparsers(dest="command", required=True)
    lenses = sorted(LENS_IDS)
    _add_prepare_parser(sub, common, handlers)
    _add_view_and_cover_parsers(sub, common, handlers)
    _add_finding_parsers(sub, common, lenses, handlers)
    _add_merge_and_verdict_parsers(sub, common, handlers)
    _add_resolution_and_status_parsers(sub, common, handlers)
    _add_inspection_parsers(sub, common, handlers)
    for name in ("cover", "add", "verdict"):
        sub.choices[name].add_argument(
            "--check",
            action="store_true",
            help="Validate without recording to the ledger or consuming an inbox file",
        )
    inbox = sub.add_parser(
        "inbox",
        parents=[common],
        help="Create a unique managed JSON file in <git-dir>/craftsman/inbox/ for --from-json",
    )
    inbox.add_argument("kind", choices=INBOX_KINDS, help="Batch command the file is for")
    inbox.set_defaults(handler=handlers["inbox"])
    scratch = sub.add_parser(
        "scratch",
        parents=[common],
        help="Create a unique managed scratch directory in <git-dir>/craftsman/scratch/ for repro scripts",
    )
    scratch.set_defaults(handler=handlers["scratch"])
    # Prefix matching would let `--lens` stand in for `--lenses` and similar near-misses.
    for command in sub.choices.values():
        command.allow_abbrev = False
    return parser
