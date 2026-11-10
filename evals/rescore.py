"""Score saved ``evals.run agent`` results again, from the ledgers they kept.

Usage:
    python3 -m evals.run rescore <results.jsonl> [more.jsonl ...]

The truth and the matching rule change over time; the runs do not. A record whose ``logs.ledger``
file still exists is scored again with the current truth and its ``recall`` field is rewritten in
place. The session fields a record of an older harness read from the last turn only (``turns``,
``duration_ms``, ``permission_denials``) are read again from its kept stream, and ``hook_errors`` from its
kept copy of the hook error log or its kept case checkout (``agent.refresh_from_logs``). The original file
is kept as ``<file>.bak`` (``.bak.1``, ... when that exists). Records without a readable ledger, of an
unknown case, or of a baseline run (its findings file is not kept) keep their recall, with a warning.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, TextIO

from evals import agent, run


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("results", nargs="+", type=Path, help="JSON lines written by 'agent'")


def _relative_to_case(findings: list[run.Finding], case: run.Case) -> list[run.Finding]:
    """Findings with an absolute path mapped onto the case file the path ends with.

    The work directory of a run is temporary and not recorded, so an absolute path in a kept
    ledger cannot be made relative to it; the end of the path still names the file.
    """
    files = {loc.file for item in run.items(case) for loc in item.locations}
    result = []
    for finding in findings:
        file = finding.file
        if file.startswith("/") or run._DRIVE.match(file):
            matches = [f for f in files if file.endswith("/" + f)]
            if matches:
                file = max(matches, key=len)
        result.append(run.Finding(file, finding.line, finding.title))
    return result


def rescore_record(record: dict[str, Any], cases_dir: Path) -> str | None:
    """Rewrite ``record["recall"]`` from its ledger. Returns why the record was left alone, or None."""
    logs = record.get("logs")
    ledger = logs.get("ledger") if isinstance(logs, dict) else None
    if not isinstance(ledger, str) or not ledger:
        return "no kept ledger (logs.ledger)"
    try:
        case = run.load_case(str(record["case"]), cases_dir)
        workdir = logs.get("workdir") if isinstance(logs, dict) else None
        findings, dismissed, unreadable = run.read_ledger(
            Path(ledger), Path(workdir) if isinstance(workdir, str) else None
        )
    except run.EvalError as exc:
        return str(exc)
    findings = _relative_to_case(findings, case)
    record["recall"] = agent.recall_of(case, findings, dismissed, unreadable)
    record["defects_total"] = len(case.defects)
    return None


def _backup_path(path: Path) -> Path:
    candidate = path.with_name(path.name + ".bak")
    number = 0
    while candidate.exists():
        number += 1
        candidate = path.with_name(f"{path.name}.bak.{number}")
    return candidate


def rescore_file(path: Path, cases_dir: Path, out: TextIO, err: TextIO) -> tuple[int, int]:
    """Rescore every record of one file; returns (rescored, left alone)."""
    try:
        original = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise run.EvalError(f"cannot read {path}: {exc}") from exc
    lines = original.splitlines()
    output: list[str] = []
    rescored = skipped = 0
    for number, line in enumerate(lines, 1):
        if not line.strip():
            output.append(line)
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise run.EvalError(f"{path}:{number} is not valid JSON: {exc}") from exc
        if not isinstance(record, dict) or "arm" not in record or "case" not in record:
            raise run.EvalError(f"{path}:{number} is not a result record of 'evals.run agent'")
        before = record.get("recall")
        refreshed = agent.refresh_from_logs(record)
        reason = rescore_record(record, cases_dir)
        name = f"{path}:{number} {record['arm']} {record['case']}#{record.get('run', '?')}"
        if reason is not None:
            print(f"warning: {name} recall left as it is: {reason}", file=err)
            if not refreshed:
                skipped += 1
                output.append(line)
                continue
        rescored += 1
        output.append(json.dumps(record, sort_keys=True))
        after = record.get("recall")
        old = f"{before['found']}/{before['total']}" if isinstance(before, dict) else "none"
        new = f"{after['found']}/{after['total']}" if isinstance(after, dict) else "none"
        print(f"{name}: {old} -> {new}" + "".join(f"; {change}" for change in refreshed), file=out)
    if rescored:
        backup = _backup_path(path)
        backup.write_text(original, encoding="utf-8")
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text("\n".join(output) + ("\n" if original.endswith("\n") else ""), encoding="utf-8")
        os.replace(temporary, path)
        print(f"{path}: {rescored} rescored, {skipped} left as they are; original kept as {backup}", file=out)
    else:
        print(f"{path}: nothing to rescore, file untouched", file=out)
    return rescored, skipped


def cmd_rescore(args: argparse.Namespace, out: TextIO) -> int:
    for path in args.results:
        rescore_file(path, args.cases_dir, out, sys.stderr)
    return 0
