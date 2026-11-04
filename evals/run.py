"""Seeded-defect review benchmark: prepare a case, let an agent review it, score the findings.

Usage:
    python3 evals/run.py list
    python3 evals/run.py prepare <case> <dest>
    python3 evals/run.py score <case> <workdir> [--findings findings.json] [--json]

Each case under ``evals/cases/<id>/`` holds ``base/`` and ``head/`` trees, ``task.md`` (the change
description a reviewer receives) and ``truth.json`` (the seeded defects, never copied to the work
directory). See ``evals/README.md``.
"""

from __future__ import annotations

import argparse
import json
import os
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

Find the real defects the change introduces or leaves behind: wrong behavior, boundary and edge
cases, errors that are lost, partial writes, security problems, places that should have been
updated with the change but were not, tests that cannot fail or that check less than they claim,
and help text or docs that contradict the code. Read the code around the change, not only the
diff, and run the tests if that helps. Do not modify any file in the repository.

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
        found.append(Location(normalize_path(_text(entry, "file", what)), start, end))
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
    return Case(case_id, path, tuple(defects), tuple(decoys))


# --- reading findings --------------------------------------------------------------------------


def normalize_path(raw: str, workdir: Path | None = None) -> str:
    """Repository-relative POSIX form of a path a reviewer reported."""
    text = raw.strip().replace("\\", "/")
    if workdir is not None and text.startswith("/"):
        root = workdir.resolve().as_posix().rstrip("/") + "/"
        for candidate in (text, Path(text).resolve().as_posix()):
            if candidate.startswith(root):
                text = candidate[len(root) :]
                break
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


def read_ledger(path: Path, workdir: Path | None = None) -> tuple[list[Finding], int, int]:
    """Findings of a Craftsman ledger as (findings, dismissed count, unreadable count).

    Reads only ``findings[].file`` (or ``.path``), ``.line``, ``.title`` and ``.verdict``, so it
    works across ledger schema versions. Findings with the verdict ``dismissed`` are ignored.
    """
    data = _read_json(path)
    raw = data.get("findings") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        raise EvalError(f"{path} has no 'findings' list; is it a Craftsman ledger?")
    findings: list[Finding] = []
    dismissed = unreadable = 0
    for entry in raw:
        if isinstance(entry, dict) and entry.get("verdict") == "dismissed":
            dismissed += 1
            continue
        finding = _finding(entry, workdir)
        if finding is None:
            unreadable += 1
        else:
            findings.append(finding)
    return findings, dismissed, unreadable


def read_findings_file(path: Path, workdir: Path | None = None) -> tuple[list[Finding], int, int]:
    """Findings from a baseline JSON list of {"file", "line", "title"} objects."""
    data = _read_json(path)
    if isinstance(data, dict) and isinstance(data.get("findings"), list):
        data = data["findings"]
    if not isinstance(data, list):
        raise EvalError(f'{path} must be a JSON list of {{"file", "line", "title"}} objects')
    findings: list[Finding] = []
    unreadable = 0
    for entry in data:
        finding = _finding(entry, workdir)
        if finding is None:
            unreadable += 1
        else:
            findings.append(finding)
    return findings, 0, unreadable


def ledger_path(workdir: Path) -> Path:
    return workdir.joinpath(".git", *LEDGER_PARTS)


def findings_path(workdir: Path) -> Path:
    return workdir / ".git" / EVALS_DIR / "findings.json"


# --- matching ----------------------------------------------------------------------------------


def distance(finding: Finding, location: Location) -> int | None:
    """0 inside [start, end]; the distance to the nearest edge up to WINDOW lines; else None."""
    if finding.file != location.file:
        return None
    if location.start <= finding.line <= location.end:
        return 0
    gap = location.start - finding.line if finding.line < location.start else finding.line - location.end
    return gap if gap <= WINDOW else None


def _nearest(finding: Finding, locations: Sequence[Location]) -> int | None:
    gaps = [g for g in (distance(finding, loc) for loc in locations) if g is not None]
    return min(gaps) if gaps else None


def match_findings(case: Case, findings: Sequence[Finding]) -> Matching:
    """Assign findings to defects: one finding per defect, one defect per finding, most defects.

    A finding that lands near a defect that another finding already found is a duplicate; one
    near a decoy is a decoy hit; the rest are unmatched (possible false positives).
    """
    edges: list[list[tuple[int, int]]] = []
    for finding in findings:
        near = [(_nearest(finding, d.locations), i) for i, d in enumerate(case.defects)]
        edges.append(sorted((gap, i) for gap, i in near if gap is not None))
    owner: dict[int, int] = {}

    def assign(finding_index: int, seen: set[int]) -> bool:
        for _, defect_index in edges[finding_index]:
            if defect_index in seen:
                continue
            seen.add(defect_index)
            if defect_index not in owner or assign(owner[defect_index], seen):
                owner[defect_index] = finding_index
                return True
        return False

    order = sorted(range(len(findings)), key=lambda i: (edges[i][0][0] if edges[i] else WINDOW + 1, i))
    for index in order:
        assign(index, set())
    assigned = set(owner.values())
    duplicates: list[int] = []
    decoy_hits: list[tuple[int, str]] = []
    unmatched: list[int] = []
    for index, finding in enumerate(findings):
        if index in assigned:
            continue
        if edges[index]:
            duplicates.append(index)
            continue
        decoy = next((d for d in case.decoys if _nearest(finding, d.locations) is not None), None)
        if decoy is not None:
            decoy_hits.append((index, decoy.id))
        else:
            unmatched.append(index)
    found = {case.defects[d].id: f for d, f in owner.items()}
    return Matching(found, tuple(duplicates), tuple(decoy_hits), tuple(unmatched))


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
        "dismissed_ignored": dismissed,
        "unreadable_ignored": unreadable,
    }


def render_score(result: dict[str, Any]) -> str:
    lines = [
        f"Case {result['case']}: recall {result['found']}/{result['total']} ({result['recall']:.0%})",
        f"  findings scored: {result['findings_used']}"
        f" (ignored: {result['dismissed_ignored']} dismissed, {result['unreadable_ignored']} unreadable)",
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


def _tree_files(root: Path) -> set[Path]:
    return {p.relative_to(root) for p in root.rglob("*") if p.is_file() and "__pycache__" not in p.parts}


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
        (dest / rel).unlink()
    shutil.copytree(head, dest, ignore=_IGNORE, dirs_exist_ok=True)
    meta = dest / ".git" / EVALS_DIR
    meta.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(case.path / "task.md", meta / "task.md")
    changed = [
        rel.as_posix()
        for rel in sorted(_tree_files(head) | _tree_files(base))
        if _read_bytes(base / rel) != _read_bytes(head / rel)
    ]
    return changed


def _read_bytes(path: Path) -> bytes | None:
    return path.read_bytes() if path.is_file() else None


def next_steps(case: Case, dest: Path, changed: Sequence[str]) -> str:
    here = "evals/run.py"
    findings = findings_path(dest)
    return "\n".join(
        [
            f"Prepared case '{case.id}' in {dest}",
            f"  base is committed; the change is left uncommitted ({len(changed)} files: {', '.join(changed)})",
            f"  change description: {dest / '.git' / EVALS_DIR / 'task.md'}",
            "",
            "Next, pick one:",
            f"  With Craftsman:  cd {dest}, start the agent (Craftsman installed), run /code-review,",
            "                   and give it .git/evals/task.md as the change description.",
            f"  Without it:      cd {dest}, start the agent with no Craftsman skills or hooks,",
            "                   and paste this prompt:",
            "",
            *("      " + line for line in BASELINE_PROMPT.splitlines()),
            "",
            "Then score the run:",
            f"  python3 {here} score {case.id} {dest}",
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
            f"{row['case']:<12} {row['defects']:>2} defects {row['decoys']:>2} decoys  {', '.join(row['kinds'])}",
            file=out,
        )
    print(
        f"{'total':<12} {sum(r['defects'] for r in rows):>2} defects {sum(r['decoys'] for r in rows):>2} decoys",
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
    if args.findings is not None:
        findings, dismissed, unreadable = read_findings_file(args.findings, workdir)
    elif ledger_path(workdir).is_file():
        findings, dismissed, unreadable = read_ledger(ledger_path(workdir), workdir)
    elif findings_path(workdir).is_file():
        findings, dismissed, unreadable = read_findings_file(findings_path(workdir), workdir)
    else:
        raise EvalError(
            f"no findings: {ledger_path(workdir)} does not exist (Craftsman run) and neither does "
            f"{findings_path(workdir)} (baseline run); pass --findings <file.json>"
        )
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
    return parser


def main(argv: Sequence[str] | None = None, out: TextIO | None = None) -> int:
    args = build_parser().parse_args(argv)
    stream = out if out is not None else sys.stdout
    handlers = {"list": _cmd_list, "prepare": _cmd_prepare, "score": _cmd_score}
    try:
        return handlers[args.command](args, stream)
    except EvalError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
