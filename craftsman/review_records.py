"""The `craftsman review` commands that write evidence: cover, add, move, merge, verdict, resolve, waive-run."""

from __future__ import annotations

import argparse
import os
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from craftsman import huntview
from craftsman.diffscan import (
    DiffSnapshot,
)
from craftsman.gatecheck import (
    require_current_round,
)
from craftsman.ledger import (
    TargetKind,
    add_coverage,
    add_finding,
    merge_findings,
    mutate,
    next_finding_id,
    plan_coverage,
    relocate_finding,
    resolve,
    set_verdict,
    uncovered_hunks,
    waive_run,
)
from craftsman.lenses import LENS_IDS
from craftsman.packet import (
    expand_hunk_spec,
)
from craftsman.records import (
    LEAD_HUNTER,
    SEVERITIES,
    VERDICTS,
    FieldError,
    Finding,
    Ledger,
    LedgerError,
    Round,
    Severity,
    Slot,
    Verdict,
    check_coverage_note,
    parse_timestamp,
    slot_generation,
)
from craftsman.review_common import cwd_of, slot_list, store_due_waves
from craftsman.review_ingest import (
    EntryReader,
    attempt,
    entry_label,
    load_json_entries,
    record_entries,
    reject_flags_with_from_json,
    require_flags,
    run_batch,
    run_single,
)
from craftsman.review_scope import finding_path, normalize_round_files
from craftsman.text import defang_tokens, one_line
from craftsman.workspace import (
    Workspace,
    caller_identity,
    open_workspace,
)

CITED_LINE_CHARS = 120


def handle_cover(args: argparse.Namespace) -> int:
    if args.from_json is not None:
        return _cover_from_json(args)
    return _cover_one(args)


def _cover_one(args: argparse.Namespace) -> int:
    if (args.hunks is None) == (args.files is None):
        raise LedgerError("pass exactly one of --hunks or --files")
    require_flags(args, {"--note": args.note})
    target_kind: TargetKind = "files" if args.files is not None else "hunks"
    ws = open_workspace(args.cwd)
    snap = ws.snapshot()

    def files_of(round_: Round) -> Callable[[str], list[str]]:
        return lambda raw: normalize_round_files(ws.root, cwd_of(args), raw, round_, snap)

    by = caller_identity(os.environ)

    def record(current: Ledger) -> tuple[Ledger, str]:
        round_ = require_current_round(current, snap)
        current = store_due_waves(current)  # a wave slot of the displays can be named at once
        raw = args.files if args.files is not None else args.hunks
        targets = _cover_targets(round_, target_kind, raw, files_of(round_))
        updated, message = record_coverage(
            current, round_, targets, args.note, target_kind=target_kind, hunter=args.hunter, by=by
        )
        return store_due_waves(updated), message

    return run_single(ws, bool(args.check), record, print)


def _cover_targets(
    round_: Round, kind: TargetKind, raw: str, files: Callable[[str], list[str]]
) -> list[str]:
    """The files, or the hunk ids with ranges like `H3-H7` expanded, named by one cover target list."""
    if kind == "files":
        return files(raw)
    return list(expand_hunk_spec(raw, [hunk.id for hunk in round_.hunks]))


def _cover_entry_label(raw: Any) -> str | None:
    if not isinstance(raw, dict):
        return None
    for key in ("hunks", "files"):
        items = raw.get(key)
        if isinstance(items, list) and all(isinstance(item, str) for item in items):
            cleaned = [item.strip() for item in items if item.strip()]
            if cleaned:
                return ",".join(cleaned)
    return None


def _cover_one_from_json(
    state: Ledger, round_: Round, raw: Any, files: Callable[[str], list[str]], by: str | None
) -> tuple[Ledger, str]:
    reader = EntryReader(raw)
    present = [key for key in ("hunks", "files") if key in raw]
    if len(present) == 2:
        raise LedgerError("pass either 'hunks' or 'files', not both")
    kind: TargetKind = "files" if present == ["files"] else "hunks"
    listed: list[str] = []
    if present:
        listed = reader.read(
            kind, lambda f: [item.strip() for item in f.texts(kind) if item.strip()], listed
        )
    else:
        reader.errors.append(FieldError("hunks", "is missing; pass 'hunks' or 'files'"))
    note = reader.read("note", lambda f: f.text("note"), "")
    hunter = reader.read("hunter", lambda f: f.text_if_present("hunter"), None)
    reader.reject_unknown()

    def record(sink: list[LedgerError]) -> tuple[Ledger, str] | None:
        if reader.failed(kind, "hunter") or not present:
            return None
        targets = attempt(
            sink, lambda: _cover_targets(round_, kind, ",".join(listed), files), None
        )
        if targets is None:  # the note is still checked, so one resend fixes both
            if not reader.failed("note"):
                check_coverage_note(one_line(note, warn=False))
            return None
        return attempt(
            sink,
            lambda: record_coverage(state, round_, targets, note, target_kind=kind, hunter=hunter, by=by),
            None,
        )

    return reader.finish(record)


def _cover_from_json(args: argparse.Namespace) -> int:
    reject_flags_with_from_json(
        {"--hunks": args.hunks, "--files": args.files, "--note": args.note, "--hunter": args.hunter}
    )
    raw_entries = load_json_entries(args.from_json)
    ws = open_workspace(args.cwd)
    snap = ws.snapshot()

    by = caller_identity(os.environ)

    def record_batch(current: Ledger) -> tuple[Ledger, list[str]]:
        round_ = require_current_round(current, snap)
        updated, messages = record_entries(
            "cover",
            raw_entries,
            store_due_waves(current),
            lambda _index, raw: _cover_entry_label(raw),
            lambda _index, state, raw: _cover_one_from_json(
                state,
                round_,
                raw,
                lambda value: normalize_round_files(ws.root, cwd_of(args), value, round_, snap),
                by,
            ),
        )
        return store_due_waves(updated), messages

    return run_batch(ws, args, record_batch, print)


def record_coverage(
    current: Ledger,
    round_: Round,
    targets: Sequence[str],
    note: str,
    *,
    target_kind: TargetKind,
    hunter: str | None = None,
    by: str | None = None,
) -> tuple[Ledger, str]:
    slot = _resolve_hunter(current, round_, hunter, allow_lead=False)
    if slot is not None:
        return _record_slot_coverage(current, slot, targets, note, target_kind=target_kind, by=by)
    open_ids, carried = plan_coverage(round_, targets, target_kind=target_kind)
    if not open_ids:
        left = len(uncovered_hunks(current, round_))
        return current, (
            f"Nothing to record: {', '.join(carried)} already covered in an earlier round "
            f"(carried over); {left} hunk(s) left to cover in this round."
        )
    skipped = f" ({len(carried)} carried hunk(s) skipped)" if carried else ""
    updated, _ = add_coverage(current, open_ids, note)
    left = len(uncovered_hunks(updated, round_))
    token = f"C{len(updated.coverage)}"  # the gate matches a reviewer's transcript to its records
    label = ", ".join(targets if target_kind == "files" else open_ids)
    return updated, (
        f"Coverage recorded ({token}): {label} in round {round_.number}{skipped}; "
        f"{left} hunk(s) left to cover in this round."
    )


def _record_slot_coverage(
    current: Ledger, slot: Slot, targets: Sequence[str], note: str, *, target_kind: TargetKind, by: str | None
) -> tuple[Ledger, str]:
    """Coverage tagged with `slot`: it may name only the slot's hunks, carried ones included."""
    round_ = current.rounds[-1]
    plan_coverage(round_, targets, target_kind=target_kind)  # unknown ids and files, empty lists
    owned = set(slot.hunks)
    if target_kind == "files":
        outside = [path for path in targets if not any(ref.path == path and ref.id in owned for ref in round_.hunks)]
        chosen = [ref.id for ref in round_.hunks if ref.path in set(targets) and ref.id in owned]
        field = "files"
    else:
        outside = [hunk_id for hunk_id in targets if hunk_id not in owned]
        chosen = list(dict.fromkeys(targets))
        field = "hunks"
    if outside:
        raise LedgerError(
            f"{', '.join(outside)} not part of slot {slot.id} in round {round_.number}; "
            f"the hunks of {slot.id}: {huntview.hunk_ranges(slot.hunks)}",
            field=field,
        )
    updated, _ = add_coverage(current, chosen, note, hunter=slot.id, by=by)
    digest_of = {ref.id: ref.digest for ref in round_.hunks}
    # Only what `check` could credit this caller with: records of the slot made since it was planned (a wave
    # slot ignores coverage recorded before its trigger) and, where the caller can prove who it is, by it.
    since = parse_timestamp(slot.since) if slot.since is not None else None
    seen = {
        digest
        for item in updated.coverage
        if item.round == round_.number
        and item.hunter == slot.id
        and (since is None or parse_timestamp(item.recorded_at) >= since)
        and (by is None or item.by == by)
        for digest in item.digests
    }
    left = sum(digest_of[hunk_id] not in seen for hunk_id in slot.hunks)
    token = f"C{len(updated.coverage)}"  # the gate matches a reviewer's transcript to its records
    label = ", ".join(targets if target_kind == "files" else chosen)
    return updated, (
        f"Coverage recorded ({token}) for {slot.id}: {label} in round {round_.number}; "
        f"slot {slot.id}: {left} hunk(s) left in your records (`craftsman review check` decides)."
    )


def _resolve_hunter(
    current: Ledger, round_: Round, hunter: str | None, *, allow_lead: bool
) -> Slot | None:
    """The slot a `--hunter` names, from the round's stored plan.

    No `--hunter` is an error in an enforced round that has a plan, and fine elsewhere. `lead` (the
    orchestrator, or extra work) names no slot. A slot the plan does not store is an error that lists
    the ones it does.
    """
    round_ = current.rounds[round_.number - 1]  # the caller's copy may predate an entry of the same batch
    number = round_.number
    if hunter is None:
        if round_.enforced and round_.plan is not None:
            lead = f" (or `{LEAD_HUNTER}` for your own findings)" if allow_lead else ""
            raise LedgerError(
                f"name your hunter slot with --hunter (your brief says it){lead}; "
                f"slots of round {number}: {slot_list(round_.plan.slots) if round_.plan else 'none'}",
                field="hunter",
            )
        return None
    if hunter == LEAD_HUNTER:
        if not allow_lead:
            raise FieldError(
                "hunter", f"'{LEAD_HUNTER}' never credits coverage; name the slot that was examined"
            )
        return None
    if slot_generation(hunter) is None:
        raise FieldError("hunter", f"{hunter!r} is not a slot id (like W1-1)")
    if round_.plan is None:
        raise LedgerError(f"round {number} has no hunt plan, so there is no slot to name; leave out --hunter", field="hunter")
    slot = round_.plan.slot(hunter)
    if slot is None:
        raise LedgerError(
            f"unknown slot {hunter} in round {number}; slots of round {number}: {slot_list(round_.plan.slots)}",
            field="hunter",
        )
    return slot


def handle_add(args: argparse.Namespace) -> int:
    if args.from_json is not None:
        return _add_from_json(args)
    return _add_one(args)


def _required_finding_flags(args: argparse.Namespace) -> dict[str, object]:
    return {
        "--lens": args.lens,
        "--file": args.file,
        "--line": args.line,
        "--severity": args.severity,
        "--title": args.title,
        "--scenario": args.scenario,
    }


def _add_one(args: argparse.Namespace) -> int:
    require_flags(args, _required_finding_flags(args))
    ws = open_workspace(args.cwd)
    snap = ws.snapshot()
    path = finding_path(ws.root, cwd_of(args), args.file, snap)
    lines = source_lines(ws.root, path, args.line)

    by = caller_identity(os.environ)

    def record(current: Ledger) -> tuple[Ledger, Finding]:
        round_ = require_current_round(current, snap)
        current = store_due_waves(current)  # a wave slot of the displays can be named at once
        hunter = _finding_hunter(current, round_, args.hunter)
        updated, finding = add_finding(
            current,
            lens=args.lens,
            path=path,
            line=args.line,
            severity=args.severity,
            title=args.title,
            scenario=args.scenario,
            suggestion=args.suggestion or "",
            hunter=hunter,
            by=by,
        )
        return store_due_waves(updated), finding

    return run_single(ws, bool(args.check), record, lambda finding: print_recorded(finding, lines))


def _add_from_json(args: argparse.Namespace) -> int:
    reject_flags_with_from_json(
        {**_required_finding_flags(args), "--suggestion": args.suggestion}
    )
    reject_flags_with_from_json({"--hunter": args.hunter})
    raw_entries = load_json_entries(args.from_json)
    ws = open_workspace(args.cwd)
    snap = ws.snapshot()
    cwd = cwd_of(args)
    by = caller_identity(os.environ)

    def record_batch(current: Ledger) -> tuple[Ledger, list[tuple[Finding, list[str] | None]]]:
        require_current_round(current, snap)
        base_number = int(next_finding_id(current)[1:])
        updated, recorded = record_entries(
            "add",
            raw_entries,
            store_due_waves(current),
            lambda index, _raw: f"F{base_number + index - 1}",
            lambda _index, state, raw: _add_one_from_json(ws, cwd, snap, state, raw, by),
        )
        return store_due_waves(updated), recorded

    return run_batch(ws, args, record_batch, lambda pair: print_recorded(pair[0], pair[1]))


def _add_one_from_json(
    ws: Workspace, cwd: Path, snap: DiffSnapshot, current: Ledger, raw: Any, by: str | None
) -> tuple[Ledger, tuple[Finding, list[str] | None]]:
    reader = EntryReader(raw)
    lens = reader.read("lens", lambda f: f.choice("lens", tuple(sorted(LENS_IDS))), "intent")
    raw_file = reader.read("file", lambda f: f.text("file"), "")
    line = reader.read("line", lambda f: f.integer("line"), 1)
    severity: Severity = reader.read("severity", lambda f: f.choice("severity", SEVERITIES), "minor")
    title = reader.read("title", lambda f: f.text("title"), "")
    scenario = reader.read("scenario", lambda f: f.text("scenario"), "")
    suggestion = reader.read("suggestion", lambda f: f.text_or("suggestion", ""), "")
    named_hunter = reader.read("hunter", lambda f: f.text_if_present("hunter"), None)
    reader.reject_unknown()

    def record(sink: list[LedgerError]) -> tuple[Ledger, tuple[Finding, list[str] | None]] | None:
        path = raw_file
        lines: list[str] | None = None
        if not reader.failed("file"):
            found = attempt(sink, lambda: finding_path(ws.root, cwd, raw_file, snap), None)
            if found is not None:
                path = found
                if not reader.failed("line"):
                    lines = attempt(sink, lambda: source_lines(ws.root, found, line), None)
        hunter: str | None = None
        if not reader.failed("hunter"):
            hunter = attempt(sink, lambda: _finding_hunter(current, current.rounds[-1], named_hunter), None)
        updated, finding = add_finding(
            current,
            lens=lens,
            path=path,
            line=line,
            severity=severity,
            title=title,
            scenario=scenario,
            suggestion=suggestion,
            hunter=hunter,
            by=by,
        )
        return updated, (finding, lines)

    return reader.finish(record)


def _finding_hunter(current: Ledger, round_: Round, hunter: str | None) -> str | None:
    """The tag for a new finding: a slot of the round's stored plan, `lead` or none.

    The tag is required in an enforced round that has a plan.
    """
    if hunter == LEAD_HUNTER:
        return LEAD_HUNTER
    slot = _resolve_hunter(current, round_, hunter, allow_lead=True)
    return slot.id if slot is not None else None


def print_recorded(finding: Finding, lines: Sequence[str] | None) -> None:
    print(f"{finding.id} recorded ({finding.severity}, {finding.lens}, {finding.path}:{finding.line})")
    print(cited_line(finding, lines))


def source_lines(root: Path, path: str, line: int) -> list[str] | None:
    """The cited file's lines (None when the diff deletes it), refusing a line past its end."""
    source = root / path
    lines = _git_lines(source) if source.is_file() else None
    if lines is not None and line > len(lines):
        raise FieldError("line", f"{line} is past the end of {path}, which has {len(lines)} lines")
    return lines


def handle_move(args: argparse.Namespace) -> int:
    ws = open_workspace(args.cwd)
    snap = ws.snapshot()
    path = None if args.file is None else finding_path(ws.root, cwd_of(args), args.file, snap)

    def record(current: Ledger) -> tuple[Ledger, tuple[Finding, list[str] | None]]:
        updated, finding = relocate_finding(
            current, args.finding_id, path=path, line=args.line, fingerprint=snap.fingerprint
        )
        return updated, (finding, source_lines(ws.root, finding.path, finding.line))

    finding, lines = mutate(ws.ledger_path, record)
    print(f"{finding.id} now points at {finding.path}:{finding.line}")
    print(cited_line(finding, lines))
    return 0


def handle_merge(args: argparse.Namespace) -> int:
    ws = open_workspace(args.cwd)
    by = caller_identity(os.environ)

    def record(current: Ledger) -> tuple[Ledger, tuple[Finding, tuple[Finding, ...]]]:
        updated, target, duplicates = merge_findings(
            current,
            args.target_id,
            args.duplicate_ids,
            title=args.title,
            scenario=args.scenario,
            suggestion=args.suggestion,
            by=by,
        )
        return store_due_waves(updated), (target, duplicates)  # the merge may judge the last finding a wave waits for

    target, duplicates = mutate(ws.ledger_path, record)
    dup_ids = ", ".join(item.id for item in duplicates)
    print(f"{dup_ids} merged into {target.id} ({target.severity}, {target.path}:{target.line})")
    for item in duplicates:
        if item.verdict_log:  # the token a verifier's transcript shows it printed
            print(f"{item.id} verdict recorded ({item.verdict_log[-1].token}): dismissed (merged into {target.id})")
    return 0


def cited_line(finding: Finding, lines: Sequence[str] | None) -> str:
    """Echo the cited source line so a wrong `--line` is noticed while the finding is fresh.

    The line is repository text, so a comment or a test fixture on it may spell a record
    (`Brief issued (B9)`): it is printed defanged, like every other prose echoed by a recording command.
    """
    if lines is None:
        return (
            f"  ({finding.path} is deleted in the diff; line {finding.line} refers to the base version)"
        )
    text = lines[finding.line - 1].strip()
    return f"  L{finding.line}: {defang_tokens(text[:CITED_LINE_CHARS])}{'…' if len(text) > CITED_LINE_CHARS else ''}"


def _git_lines(path: Path) -> list[str]:
    """The file's lines as git counts them: split on newlines only."""
    try:
        text = path.read_bytes().decode("utf-8", errors="replace")
    except OSError as exc:
        raise LedgerError(f"cannot read {path}: {exc}") from exc
    return text.removesuffix("\n").split("\n") if text else []


def handle_verdict(args: argparse.Namespace) -> int:
    if args.from_json is not None:
        return _verdict_from_json(args)
    return _verdict_one(args)


def _verdict_one(args: argparse.Namespace) -> int:
    require_flags(args, {"ID": args.finding_id, "--as": args.verdict, "--evidence": args.evidence})
    ws = open_workspace(args.cwd)
    by = caller_identity(os.environ)

    def record(current: Ledger) -> tuple[Ledger, Finding]:
        updated, finding = set_verdict(current, args.finding_id, args.verdict, args.evidence, args.run, by=by)
        return store_due_waves(updated), finding  # the verdict may be the one that calls for a wave

    return run_single(ws, bool(args.check), record, print_verdict)


def _verdict_one_from_json(state: Ledger, raw: Any, by: str | None) -> tuple[Ledger, Finding]:
    reader = EntryReader(raw)
    finding_id = reader.read("id", lambda f: f.text("id"), "")
    verdict: Verdict = reader.read("as", lambda f: f.choice("as", VERDICTS), "dismissed")
    evidence = reader.read("evidence", lambda f: f.text("evidence"), "")
    run_id = reader.read("run", lambda f: f.text_if_present("run"), None)
    reader.reject_unknown()

    def record(sink: list[LedgerError]) -> tuple[Ledger, Finding] | None:
        if reader.failed("id", "as", "evidence", "run"):
            return None
        return set_verdict(state, finding_id, verdict, evidence, run_id, by=by)

    return reader.finish(record)


def _verdict_from_json(args: argparse.Namespace) -> int:
    reject_flags_with_from_json(
        {"ID": args.finding_id, "--as": args.verdict, "--evidence": args.evidence, "--run": args.run}
    )
    raw_entries = load_json_entries(args.from_json)
    ws = open_workspace(args.cwd)
    by = caller_identity(os.environ)

    def record_batch(current: Ledger) -> tuple[Ledger, list[Finding]]:
        updated, findings = record_entries(
            "verdict",
            raw_entries,
            current,
            lambda _index, raw: entry_label(raw, "id"),
            lambda _index, state, raw: _verdict_one_from_json(state, raw, by),
        )
        return store_due_waves(updated), findings

    return run_batch(ws, args, record_batch, print_verdict)


def print_verdict(finding: Finding) -> None:
    """`F3 verdict recorded (V7): proven (R2)`: the token is what a verifier's transcript shows it printed."""
    token = f" ({finding.verdict_log[-1].token})" if finding.verdict_log else ""
    run = f" ({finding.verdict_run})" if finding.verdict_run else ""
    print(f"{finding.id} verdict recorded{token}: {finding.verdict}{run}")


def handle_resolve(args: argparse.Namespace) -> int:
    ws = open_workspace(args.cwd)
    snap = ws.snapshot()
    finding = mutate(
        ws.ledger_path,
        lambda current: resolve(
            current, args.finding_id, args.resolution, args.note, fingerprint=snap.fingerprint
        ),
    )
    print(f"{finding.id} resolved as {finding.resolution}")
    return 0


def handle_waive_run(args: argparse.Namespace) -> int:
    ws = open_workspace(args.cwd)
    run = mutate(ws.ledger_path, lambda current: waive_run(current, args.run_id, args.reason))
    print(f"{run.id} waived; it no longer blocks the gate")
    return 0
