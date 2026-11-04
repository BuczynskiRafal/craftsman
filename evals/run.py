"""Seeded-defect review benchmark: prepare a case, let an agent review it, score the findings.

Usage:
    python3 -m evals.run list
    python3 -m evals.run prepare <case> <dest>
    python3 -m evals.run score <case> <workdir> [--findings findings.json] [--json]
    python3 -m evals.run agent --cases all --arm <label> [--craftsman-src <dir>] [--repeat N] ...
    python3 -m evals.run compare results.jsonl [more.jsonl ...]

``agent`` runs a real headless Claude Code review per case and appends one JSON line per run to a
results file (see ``evals/agent.py``); ``compare`` summarizes such files (see ``evals/compare.py``).

Each case under ``evals/cases/<id>/`` holds ``base/`` and ``head/`` trees, ``task.md`` (the change
description a reviewer receives) and ``truth.json`` (the seeded defects, never copied to the work
directory). See ``evals/README.md``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

CASES_DIR = Path(__file__).resolve().parent / "cases"
WINDOW = 3
EVALS_DIR = "evals"  # inside <dest>/.git: holds task.md and, for baseline runs, findings.json
LEDGER_PARTS = ("craftsman", "ledger.json")
SIZES = ("small", "large")

KINDS = (
    "aliasing",
    "cache-key-missing-param",
    "empty-as-missing",
    "help-contradicts-code",
    "incomplete-propagation",
    "non-atomic-write",
    "off-by-one-boundary",
    "path-traversal",
    "platform-dependence",
    "qualifier-not-implemented",
    "swallowed-error",
    "weak-test",
)

BASELINE_PROMPT = """\
You are reviewing an uncommitted change in this git repository. The change description is in
`.git/evals/task.md`. See the change with `git status` and `git diff HEAD`; new files are
untracked, so read them too.

Find the real defects the change introduces or leaves behind. Read the code around the change,
not only the diff, and run the tests if that helps. Review only: do not modify any file in the
repository.

When you are done, write your findings to `.git/evals/findings.json` as a JSON list, one object
per defect, with exactly these keys:
  {"file": "<path relative to the repository root>", "line": <line number in the changed file>,
   "title": "<one sentence: what is wrong and when it shows>"}
Report each defect once, at the line where it is clearest. Also print the same list in your answer."""

EXIT_USAGE = 2


class EvalError(Exception):
    """A problem the user can fix: unknown case, bad file, missing ledger."""


@dataclass(frozen=True)
class Location:
    file: str
    start: int
    end: int
    anchor: str = ""  # text that must be on one of the lines start..end of the head file


@dataclass(frozen=True)
class Defect:
    id: str
    kind: str
    summary: str
    locations: tuple[Location, ...]


@dataclass(frozen=True)
class Decoy:
    id: str
    summary: str
    locations: tuple[Location, ...]


@dataclass(frozen=True)
class Case:
    id: str
    path: Path
    defects: tuple[Defect, ...]
    decoys: tuple[Decoy, ...]
    size: str = "small"  # "large" cases are 400-700 changed lines in 10-15 files, see README


@dataclass(frozen=True)
class Finding:
    file: str
    line: int
    title: str


@dataclass(frozen=True)
class Matching:
    """Which finding (index) found which defect, and what the other findings are."""

    found: dict[str, int]
    duplicates: tuple[int, ...]
    decoy_hits: tuple[tuple[int, str], ...]
    unmatched: tuple[int, ...]


# --- loading cases -----------------------------------------------------------------------------


def _object(value: Any, what: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise EvalError(f"{what} must be a JSON object")
    return value


def _text(data: dict[str, Any], key: str, what: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise EvalError(f"{what}: '{key}' must be a non-empty string")
    return value


def _locations(data: dict[str, Any], what: str) -> tuple[Location, ...]:
    raw = data.get("locations")
    if not isinstance(raw, list) or not raw:
        raise EvalError(f"{what}: 'locations' must be a non-empty list")
    found = []
    for item in raw:
        entry = _object(item, f"{what}: location")
        start, end = entry.get("start"), entry.get("end")
        if not isinstance(start, int) or not isinstance(end, int) or start < 1 or end < start:
            raise EvalError(f"{what}: location needs integer 'start' >= 1 and 'end' >= start")
        anchor = entry.get("anchor", "")
        if not isinstance(anchor, str):
            raise EvalError(f"{what}: 'anchor' must be a string")
        found.append(Location(normalize_path(_text(entry, "file", what)), start, end, anchor))
    return tuple(found)


def case_ids(cases_dir: Path = CASES_DIR) -> list[str]:
    if not cases_dir.is_dir():
        return []
    return sorted(p.name for p in cases_dir.iterdir() if (p / "truth.json").is_file())


def load_case(case_id: str, cases_dir: Path = CASES_DIR) -> Case:
    path = cases_dir / case_id
    truth_path = path / "truth.json"
    if not truth_path.is_file():
        known = ", ".join(case_ids(cases_dir)) or "none"
        raise EvalError(f"unknown case '{case_id}' (available: {known})")
    try:
        truth = _object(json.loads(truth_path.read_text(encoding="utf-8")), "truth.json")
    except json.JSONDecodeError as exc:
        raise EvalError(f"{truth_path}: invalid JSON: {exc}") from exc
    defects = []
    for item in truth.get("defects", []):
        entry = _object(item, "defect")
        what = f"defect {entry.get('id', '?')}"
        kind = _text(entry, "kind", what)
        if kind not in KINDS:
            raise EvalError(f"{what}: unknown kind '{kind}' (known: {', '.join(KINDS)})")
        defects.append(
            Defect(
                _text(entry, "id", what), kind, _text(entry, "summary", what), _locations(entry, what)
            )
        )
    decoys = []
    for item in truth.get("decoys", []):
        entry = _object(item, "decoy")
        what = f"decoy {entry.get('id', '?')}"
        decoys.append(
            Decoy(_text(entry, "id", what), _text(entry, "summary", what), _locations(entry, what))
        )
    ids = [d.id for d in defects] + [d.id for d in decoys]
    if len(ids) != len(set(ids)):
        raise EvalError(f"{truth_path}: ids must be unique")
    if not defects:
        raise EvalError(f"{truth_path}: no defects")
    size = truth.get("size", "small")
    if size not in SIZES:
        raise EvalError(f"{truth_path}: 'size' must be one of {', '.join(SIZES)}")
    return Case(case_id, path, tuple(defects), tuple(decoys), size)


# --- reading findings --------------------------------------------------------------------------


_DRIVE = re.compile(r"^[A-Za-z]:/")


def strip_root(text: str, root: str) -> str:
    """``text`` (POSIX form) relative to ``root`` when it lies inside it, else unchanged.

    Windows paths (a drive letter) are compared case-insensitively, as the file system does.
    """
    prefix = root.rstrip("/") + "/"
    fold = bool(_DRIVE.match(prefix))
    if (text.lower() if fold else text).startswith(prefix.lower() if fold else prefix):
        return text[len(prefix) :]
    return text


def normalize_path(raw: str, workdir: Path | None = None) -> str:
    """Repository-relative POSIX form of a path a reviewer reported."""
    text = raw.strip().replace("\\", "/")
    if workdir is not None and (text.startswith("/") or _DRIVE.match(text)):
        root = workdir.resolve().as_posix()
        stripped = strip_root(text, root)
        if stripped == text and text.startswith("/"):  # a symlinked spelling of the same directory
            stripped = strip_root(Path(text).resolve().as_posix(), root)
        text = stripped
    while text.startswith("./"):
        text = text[2:]
    return text


def _finding(entry: Any, workdir: Path | None) -> Finding | None:
    """A Finding from one JSON object, or None when it lacks a usable file or line."""
    if not isinstance(entry, dict):
        return None
    file = entry.get("file", entry.get("path"))
    line = entry.get("line")
    if isinstance(line, str) and line.strip().isdigit():
        line = int(line.strip())
    if (
        not isinstance(file, str)
        or not file.strip()
        or isinstance(line, bool)
        or not isinstance(line, int)
    ):
        return None
    title = entry.get("title")
    return Finding(normalize_path(file, workdir), line, title.strip() if isinstance(title, str) else "")


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise EvalError(f"cannot read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise EvalError(f"{path} is not valid JSON: {exc}") from exc


def _collect(
    entries: Sequence[Any], workdir: Path | None, ignore_dismissed: bool
) -> tuple[list[Finding], int, int]:
    """(findings, dismissed count, unreadable count) of a list of finding objects."""
    findings: list[Finding] = []
    dismissed = unreadable = 0
    for entry in entries:
        if ignore_dismissed and isinstance(entry, dict) and entry.get("verdict") == "dismissed":
            dismissed += 1
            continue
        finding = _finding(entry, workdir)
        if finding is None:
            unreadable += 1
        else:
            findings.append(finding)
    return findings, dismissed, unreadable


def read_ledger(path: Path, workdir: Path | None = None) -> tuple[list[Finding], int, int]:
    """Findings of a Craftsman ledger as (findings, dismissed count, unreadable count).

    Reads only ``findings[].file`` (or ``.path``), ``.line``, ``.title`` and ``.verdict``, so it
    works across ledger schema versions. Findings with the verdict ``dismissed`` are ignored.
    """
    data = _read_json(path)
    raw = data.get("findings") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        raise EvalError(f"{path} has no 'findings' list; is it a Craftsman ledger?")
    return _collect(raw, workdir, ignore_dismissed=True)


def read_findings_file(path: Path, workdir: Path | None = None) -> tuple[list[Finding], int, int]:
    """Findings from a baseline JSON list of {"file", "line", "title"} objects (nothing is dismissed)."""
    data = _read_json(path)
    if isinstance(data, dict) and isinstance(data.get("findings"), list):
        data = data["findings"]
    if not isinstance(data, list):
        raise EvalError(f'{path} must be a JSON list of {{"file", "line", "title"}} objects')
    return _collect(data, workdir, ignore_dismissed=False)


def ledger_path(workdir: Path) -> Path:
    return workdir.joinpath(".git", *LEDGER_PARTS)


def findings_path(workdir: Path) -> Path:
    return workdir / ".git" / EVALS_DIR / "findings.json"


def collect_findings(workdir: Path, findings_file: Path | None = None) -> tuple[list[Finding], int, int]:
    """Findings of a review as (findings, dismissed, unreadable).

    From ``findings_file`` when given, else the Craftsman ledger of the work directory, else its
    ``.git/evals/findings.json`` (baseline runs).
    """
    if findings_file is not None:
        return read_findings_file(findings_file, workdir)
    if ledger_path(workdir).is_file():
        return read_ledger(ledger_path(workdir), workdir)
    if findings_path(workdir).is_file():
        return read_findings_file(findings_path(workdir), workdir)
    raise EvalError(
        f"no findings: {ledger_path(workdir)} does not exist (Craftsman run) and neither does "
        f"{findings_path(workdir)} (baseline run); pass --findings <file.json>"
    )


# --- matching ----------------------------------------------------------------------------------


def distance(finding: Finding, location: Location, slack: int = WINDOW) -> int | None:
    """0 inside [start, end]; the distance to the nearest edge up to ``slack`` lines; else None."""
    if finding.file != location.file:
        return None
    if location.start <= finding.line <= location.end:
        return 0
    gap = location.start - finding.line if finding.line < location.start else finding.line - location.end
    return gap if gap <= slack else None


@dataclass(frozen=True)
class Zone:
    """The lines a finding may point at to count for one location of a defect or a decoy."""

    kind: str  # "defect" or "decoy"
    id: str
    location: Location
    slack: int


def _lines_between(a: Location, b: Location) -> int:
    """Lines strictly between two ranges of one file; 0 when they touch or overlap."""
    if b.start > a.end:
        return b.start - a.end - 1
    if a.start > b.end:
        return a.start - b.end - 1
    return 0


def zones(case: Case) -> list[Zone]:
    """Every location of every defect and decoy with its slack.

    The slack is WINDOW lines, but never more than it takes to leave a line of the gap to the next
    defect or decoy of the same file uncovered: the zones of two items never overlap, so a finding is
    near at most one of them and two findings on one defect cannot credit its neighbour.
    """
    items = [("defect", d.id, loc) for d in case.defects for loc in d.locations]
    items += [("decoy", d.id, loc) for d in case.decoys for loc in d.locations]
    result = []
    for kind, item_id, loc in items:
        slack = WINDOW
        for _, other_id, other in items:
            if other_id != item_id and other.file == loc.file:
                slack = min(slack, max(_lines_between(loc, other) - 1, 0) // 2)
        result.append(Zone(kind, item_id, loc, slack))
    return result


def match_findings(case: Case, findings: Sequence[Finding]) -> Matching:
    """Assign each finding to the defect or decoy it points at, or to none.

    A finding inside a zone belongs to that defect or decoy (the closest one when ranges overlap).
    Each defect is found once, by the finding closest to it; further findings on it are duplicates.
    A finding on a decoy is a decoy hit; the rest are unmatched (possible false positives).
    """
    all_zones = zones(case)
    credited: dict[str, tuple[int, int]] = {}  # defect id -> (gap, finding index)
    duplicates: list[int] = []
    decoy_hits: list[tuple[int, str]] = []
    unmatched: list[int] = []
    for index, finding in enumerate(findings):
        best: tuple[int, int, Zone] | None = None
        for order, zone in enumerate(all_zones):
            gap = distance(finding, zone.location, zone.slack)
            if gap is not None and (best is None or (gap, order) < best[:2]):
                best = (gap, order, zone)
        if best is None:
            unmatched.append(index)
        elif best[2].kind == "decoy":
            decoy_hits.append((index, best[2].id))
        else:
            gap, defect_id = best[0], best[2].id
            if defect_id not in credited:
                credited[defect_id] = (gap, index)
            elif gap < credited[defect_id][0]:
                duplicates.append(credited[defect_id][1])
                credited[defect_id] = (gap, index)
            else:
                duplicates.append(index)
    found = {defect_id: index for defect_id, (_, index) in credited.items()}
    return Matching(found, tuple(sorted(duplicates)), tuple(decoy_hits), tuple(unmatched))


def score_findings(
    case: Case, findings: Sequence[Finding], dismissed: int = 0, unreadable: int = 0
) -> dict[str, Any]:
    matching = match_findings(case, findings)

    def show(index: int) -> dict[str, Any]:
        f = findings[index]
        return {"file": f.file, "line": f.line, "title": f.title}

    defects = []
    for defect in case.defects:
        index = matching.found.get(defect.id)
        defects.append(
            {
                "id": defect.id,
                "kind": defect.kind,
                "summary": defect.summary,
                "found": index is not None,
                "finding": show(index) if index is not None else None,
            }
        )
    total = len(case.defects)
    return {
        "case": case.id,
        "found": len(matching.found),
        "total": total,
        "recall": len(matching.found) / total,
        "defects": defects,
        "unmatched": [show(i) for i in matching.unmatched],
        "duplicates": [show(i) for i in matching.duplicates],
        "decoy_hits": [{**show(i), "decoy": d} for i, d in matching.decoy_hits],
        "findings_used": len(findings),
        "precision": (len(matching.found) + len(matching.duplicates)) / len(findings) if findings else None,
        "dismissed_ignored": dismissed,
        "unreadable_ignored": unreadable,
    }


def render_score(result: dict[str, Any]) -> str:
    lines = [
        f"Case {result['case']}: recall {result['found']}/{result['total']} ({result['recall']:.0%})",
        f"  findings scored: {result['findings_used']}"
        f" (ignored: {result['dismissed_ignored']} dismissed, {result['unreadable_ignored']} unreadable)",
        "  precision (findings on a seeded defect / findings scored): "
        + (f"{result['precision']:.0%}" if result["precision"] is not None else "n/a")
        + "; an unmatched finding may still be a real problem",
        "",
        "Defects",
    ]
    for d in result["defects"]:
        if d["found"]:
            f = d["finding"]
            lines.append(f"  found   {d['id']:<8} {d['kind']}  <- {f['file']}:{f['line']} {f['title']}")
        else:
            lines.append(f"  MISSED  {d['id']:<8} {d['kind']}: {d['summary']}")

    def section(title: str, items: list[dict[str, Any]], suffix: str = "") -> None:
        lines.extend(["", f"{title} ({len(items)})"])
        for f in items:
            extra = f"  [decoy {f['decoy']}]" if "decoy" in f else ""
            lines.append(f"  {f['file']}:{f['line']} {f['title']}{extra}")
        if suffix and items:
            lines.append(f"  {suffix}")

    section(
        "Unmatched findings, possible false positives",
        result["unmatched"],
        "Read them: a real defect outside the seeded list also lands here.",
    )
    section("Decoy hits, code that looks wrong but is correct", result["decoy_hits"])
    section("Duplicates of a defect that was already found", result["duplicates"])
    return "\n".join(lines)


# --- prepare -----------------------------------------------------------------------------------

_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store")


def _git(dest: Path, *args: str) -> None:
    command = [
        "git",
        "-c",
        "user.name=Evals",
        "-c",
        "user.email=evals@example.invalid",
        "-c",
        "commit.gpgsign=false",
        "-c",
        "init.defaultBranch=main",
        *args,
    ]
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    try:
        subprocess.run(command, cwd=dest, check=True, capture_output=True, text=True, env=env)
    except FileNotFoundError as exc:
        raise EvalError("git is not installed or not on PATH") from exc
    except subprocess.CalledProcessError as exc:
        raise EvalError(f"git {' '.join(args)} failed: {exc.stderr.strip()}") from exc


_IGNORED_NAMES = {".DS_Store"}


def _tree_files(root: Path) -> set[Path]:
    """Files of a case tree that ``prepare`` copies (the ones _IGNORE does not skip)."""
    return {
        p.relative_to(root)
        for p in root.rglob("*")
        if p.is_file()
        and "__pycache__" not in p.parts
        and p.name not in _IGNORED_NAMES
        and p.suffix != ".pyc"
    }


def prepare_case(case: Case, dest: Path) -> list[str]:
    """Create the repository for a case in ``dest``; return the changed paths (head vs base)."""
    base, head = case.path / "base", case.path / "head"
    if not base.is_dir() or not head.is_dir():
        raise EvalError(f"case {case.id} needs base/ and head/ directories")
    if dest.exists() and (not dest.is_dir() or any(dest.iterdir())):
        raise EvalError(f"{dest} exists and is not an empty directory")
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copytree(base, dest, ignore=_IGNORE, dirs_exist_ok=True)
    _git(dest, "init", "-q")
    exclude = dest / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    with exclude.open("a", encoding="utf-8") as fh:
        fh.write("__pycache__/\n*.pyc\n")
    _git(dest, "add", "-A")
    _git(dest, "commit", "-q", "-m", f"Base of {case.id}")
    for rel in _tree_files(base) - _tree_files(head):
        (dest / rel).unlink(missing_ok=True)
    shutil.copytree(head, dest, ignore=_IGNORE, dirs_exist_ok=True)
    meta = dest / ".git" / EVALS_DIR
    meta.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(case.path / "task.md", meta / "task.md")
    return [
        rel.as_posix()
        for rel in sorted(_tree_files(head) | _tree_files(base))
        if _read_bytes(base / rel) != _read_bytes(head / rel)
    ]


def _read_bytes(path: Path) -> bytes | None:
    return path.read_bytes() if path.is_file() else None


def next_steps(case: Case, dest: Path, changed: Sequence[str]) -> str:
    here = shlex.quote(str(Path(__file__).resolve()))  # absolute: the next step starts in the case dir
    quoted = shlex.quote(str(dest))
    findings = findings_path(dest)
    return "\n".join(
        [
            f"Prepared case '{case.id}' in {dest}",
            f"  base is committed; the change is left uncommitted ({len(changed)} files: {', '.join(changed)})",
            f"  change description: {dest / '.git' / EVALS_DIR / 'task.md'}",
            "",
            "Next, pick one:",
            f"  With Craftsman:  cd {quoted}, start the agent (Craftsman installed), run /craft-review",
            "                   (/code-review in a checkout older than Craftsman 5), review only,",
            "                   and give it .git/evals/task.md as the change description.",
            f"  Without it:      cd {quoted}, start the agent with no Craftsman skills or hooks,",
            "                   and paste this prompt:",
            "",
            *("      " + line for line in BASELINE_PROMPT.splitlines()),
            "",
            "Then score the run:",
            f"  python3 {here} score {case.id} {quoted}",
            f"  (baseline findings are read from {findings} when no ledger exists;",
            "   or pass --findings <file.json>)",
        ]
    )


# --- command line ------------------------------------------------------------------------------


def _cmd_list(args: argparse.Namespace, out: TextIO) -> int:
    rows: list[dict[str, Any]] = []
    for case_id in case_ids(args.cases_dir):
        case = load_case(case_id, args.cases_dir)
        rows.append(
            {
                "case": case.id,
                "size": case.size,
                "defects": len(case.defects),
                "decoys": len(case.decoys),
                "kinds": sorted({d.kind for d in case.defects}),
            }
        )
    if args.json:
        print(json.dumps(rows, indent=2), file=out)
        return 0
    for row in rows:
        print(
            f"{row['case']:<12} {row['size']:<5} {row['defects']:>2} defects {row['decoys']:>2} decoys"
            f"  {', '.join(row['kinds'])}",
            file=out,
        )
    print(
        f"{'total':<12} {'':<5} {sum(r['defects'] for r in rows):>2} defects {sum(r['decoys'] for r in rows):>2} decoys",
        file=out,
    )
    return 0


def _cmd_prepare(args: argparse.Namespace, out: TextIO) -> int:
    case = load_case(args.case, args.cases_dir)
    dest = args.dest.resolve()
    changed = prepare_case(case, dest)
    print(next_steps(case, dest, changed), file=out)
    return 0


def _cmd_score(args: argparse.Namespace, out: TextIO) -> int:
    case = load_case(args.case, args.cases_dir)
    workdir = args.workdir.resolve()
    if not workdir.is_dir():
        raise EvalError(f"{workdir} is not a directory")
    findings, dismissed, unreadable = collect_findings(workdir, args.findings)
    result = score_findings(case, findings, dismissed, unreadable)
    print(json.dumps(result, indent=2) if args.json else render_score(result), file=out)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="run.py", description="Seeded-defect review benchmark.")
    parser.add_argument("--cases-dir", type=Path, default=CASES_DIR, help="directory holding the cases")
    sub = parser.add_subparsers(dest="command", required=True)
    listing = sub.add_parser("list", help="list the cases with their defect counts")
    listing.add_argument("--json", action="store_true", help="machine-readable output")
    prepare = sub.add_parser("prepare", help="create a repository to review in <dest>")
    prepare.add_argument("case")
    prepare.add_argument("dest", type=Path)
    score = sub.add_parser("score", help="score the findings of a review of a prepared case")
    score.add_argument("case")
    score.add_argument("workdir", type=Path)
    score.add_argument("--findings", type=Path, help='JSON list of {"file", "line", "title"}')
    score.add_argument("--json", action="store_true", help="machine-readable output")
    from evals import agent, compare

    agent.add_arguments(
        sub.add_parser("agent", help="run real headless Claude Code reviews and record recall, cost, time")
    )
    compare.add_arguments(sub.add_parser("compare", help="summarize results files written by 'agent'"))
    return parser


def _cmd_agent(args: argparse.Namespace, out: TextIO) -> int:
    from evals import agent

    return agent.cmd_agent(args, out)


def _cmd_compare(args: argparse.Namespace, out: TextIO) -> int:
    from evals import compare

    return compare.cmd_compare(args, out)


def main(argv: Sequence[str] | None = None, out: TextIO | None = None) -> int:
    args = build_parser().parse_args(argv)
    stream = out if out is not None else sys.stdout
    handlers = {
        "list": _cmd_list,
        "prepare": _cmd_prepare,
        "score": _cmd_score,
        "agent": _cmd_agent,
        "compare": _cmd_compare,
    }
    try:
        return handlers[args.command](args, stream)
    except EvalError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":
    # Run the importable copy of this module: agent.py and compare.py import ``evals.run``, and their
    # EvalError must be the one main() catches.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from evals.run import main as _main

    sys.exit(_main())
