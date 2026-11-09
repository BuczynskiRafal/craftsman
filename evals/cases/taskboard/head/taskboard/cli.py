"""Command line interface of the task board."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import TextIO

from taskboard.board import Board
from taskboard.model import DEFAULT_PRIORITY, PRIORITIES, Task


def format_task(task: Task) -> str:
    return f"{task.id:>3} [{'x' if task.done else ' '}] P{task.priority} {task.title}"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="taskboard")
    parser.add_argument("--file", type=Path, default=Path("tasks.json"))
    sub = parser.add_subparsers(dest="command", required=True)
    add = sub.add_parser("add", help="add a task")
    add.add_argument("title")
    add.add_argument("--priority", type=int, choices=PRIORITIES, default=DEFAULT_PRIORITY)
    done = sub.add_parser("done", help="mark a task as done")
    done.add_argument("id", type=int)
    show = sub.add_parser("list", help="show the tasks")
    show.add_argument("--sort", choices=["id", "priority"], default="id")
    sub.add_parser("export", help="print the tasks as CSV")
    return parser


def main(argv: list[str] | None = None, out: TextIO = sys.stdout) -> int:
    args = build_parser().parse_args(argv)
    board = Board.load(args.file)
    if args.command == "add":
        task = board.add(args.title, args.priority)
        board.save(args.file)
        print(format_task(task), file=out)
    elif args.command == "done":
        try:
            board.complete(args.id)
        except KeyError:
            print(f"no task {args.id}", file=sys.stderr)
            return 1
        board.save(args.file)
    elif args.command == "export":
        board.export_csv(out)
    else:
        tasks = board.by_priority() if args.sort == "priority" else board.tasks
        for task in tasks:
            print(format_task(task), file=out)
    return 0
