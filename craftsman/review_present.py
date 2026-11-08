"""Everything `craftsman review` prints for a person: the prepare summary and hunt plan, `show` and the reports."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from craftsman import hook_errors, huntplan, huntview
from craftsman.diffscan import (
    DiffSnapshot,
    files_without_signal_patterns,
)
from craftsman.gatecheck import (
    SELF_REVIEWED_BANNER,
    GatePolicy,
    GateReport,
    SlotStatus,
    evaluate,
    hunt_line,
    large_diff_warning,
    slot_statuses,
)
from craftsman.ledger import (
    uncovered_hunks,
)
from craftsman.packet import (
    bound_hints,
)
from craftsman.records import (
    SEVERITIES,
    Finding,
    Ledger,
    Round,
    Run,
)
from craftsman.review_scope import format_path_sample

if TYPE_CHECKING:
    from craftsman.review import _Prepared
from craftsman.text import defang_tokens, inline_md
from craftsman.usage import usage_lines
from craftsman.workspace import (
    Workspace,
)
from craftsman.worktree_index import WorktreeIndex

MAX_UNSCANNED_PATHS_SHOWN = 8


def print_empty_prepare(
    scope: tuple[str, ...] | None, snap: DiffSnapshot, full_snap: DiffSnapshot
) -> int:
    if scope is not None and not full_snap.is_empty:
        in_scope = set(scope)
        ignored = [path for path in full_snap.files if path not in in_scope]
        print(
            f"Nothing to review within the frozen scope (matches base {snap.base_sha[:12]}); "
            f"changed files outside scope: {format_path_sample(ignored)}."
        )
        print(
            "Run `craftsman review reset` to archive this review and start a new one "
            "(or pass `craftsman review prepare --add-paths <paths>` to add files touched by fixes)."
        )
        return 0
    print(f"Nothing to review: the work tree matches base {snap.base_sha[:12]}.")
    return 0


def print_scope_summary(
    ws: Workspace, ledger: Ledger, round_: Round, snap: DiffSnapshot, full_snap: DiffSnapshot
) -> None:
    if round_.scope is None:
        return
    rename_sources = {hunk.old_path for hunk in snap.hunks if hunk.old_path is not None}
    scoped_count = len(set(round_.scope) - rename_sources)
    print(f"Scope: {scoped_count} file(s), frozen for this review.")
    in_scope = set(round_.scope)
    ignored = [path for path in full_snap.files if path not in in_scope]
    if ignored:
        print(f"Ignored (outside the review scope): {format_path_sample(ignored)}")
        if ws.state is None and evaluate(
            ledger,
            snap,
            GatePolicy(deep=round_.deep, isolation_evidence=ws.isolation_evidence(ledger)),
            review_only=True,
        ).passed:
            print(
                f"Hint: round {round_.number} is already complete; run `craftsman review reset` "
                "to archive it before starting a new review (or `craftsman review prepare "
                "--add-paths <paths>` to include files added by a fix)."
            )


def print_unscanned_files(snap: DiffSnapshot) -> None:
    unscanned = files_without_signal_patterns(snap)
    if not unscanned:
        return
    shown = ", ".join(unscanned[:MAX_UNSCANNED_PATHS_SHOWN])
    more = len(unscanned) - MAX_UNSCANNED_PATHS_SHOWN
    if more > 0:
        shown += f" and {more} more"
    print(
        f"Not scanned for risk signals (no patterns for their language): {shown}. Read them for "
        "risks the suggested lenses miss and apply the matching lens yourself."
    )


def _production_lines(snap: DiffSnapshot) -> int:
    return sum(huntplan.changed_lines(hunk) for hunk in snap.hunks if huntplan.is_production_hunk(hunk))


def _depth_line(depth: huntplan.Depth, snap: DiffSnapshot) -> str:
    if depth.deep:
        return f"Depth: deep ({'; '.join(depth.reasons)})"
    lines = _production_lines(snap)
    return (
        f"Depth: normal (no deep signal; {lines} changed production lines <= "
        f"{huntplan.DEEP_MIN_PRODUCTION_LINES - 1})"
    )


def _enforcement_line(round_: Round) -> str:
    if round_.enforced:
        who = huntview.PLATFORM_LABELS.get(round_.platform, "Craftsman")
        return f"Enforced: {who} hooks serve this review; finishing requires every slot below."
    return (
        "Advisory: no Craftsman hook saw this session, so the review will be SELF-REVIEWED "
        "(run `craftsman doctor`)."
    )


def print_prepare_summary(
    result: _Prepared, packet: Path, snap: DiffSnapshot, depth: huntplan.Depth, now: datetime, *, check: str
) -> None:
    round_ = result.round
    carried = len(round_.hunks) - len(round_.open_hunk_ids)
    changed = snap.added + snap.deleted
    deep = " (deep)" if round_.deep and not result.raised else ""
    state = (
        "raised to deep (same diff)"
        if result.raised
        else "prepared"
        if result.created
        else "unchanged (same diff)"
    )
    print(
        f"Round {round_.number} {state}{deep}: {len(snap.files)} files, {len(snap.hunks)} hunks, "
        f"{changed} changed lines; {carried} hunks carried, {len(round_.open_hunk_ids)} to review."
    )
    print(f"Packet: {packet}")
    print(_depth_line(depth, snap))
    print(_enforcement_line(round_))
    if round_.signals:
        listed, summary = bound_hints(round_.signals)
        print(f"Quality hints: {summary}")
        for sig in listed:
            print(f"  {sig.id} {sig.kind.value} {sig.file}:{sig.line} — {sig.detail}")
    warning = large_diff_warning(round_)
    if warning is not None:
        print(f"Warning: {warning}")
    _print_hunt_plan(result, now, check=check)


def _print_hunt_plan(result: _Prepared, now: datetime, *, check: str) -> None:
    """The plan table and what to do next. A re-prepared round shows where each slot stands.

    An advisory round is judged by its hunk coverage, so its plan is a suggestion: once every hunk is covered
    nothing is left to launch, whatever state its slots are in.
    """
    round_ = result.round
    statuses = slot_statuses(result.ledger, round_, now)
    print(f"Hunt plan, round {round_.number}:")
    if not statuses:
        print("  (no slot: every hunk is carried over from earlier rounds)")
        print(f"Nothing is owed. Run `{check}`.")
        return
    covered = not round_.enforced and not result.created and not uncovered_hunks(result.ledger, round_)
    for line in huntview.plan_lines(statuses, with_state=not result.created and not covered):
        print(line)
    if not round_.enforced:
        print("This plan is a suggestion: an advisory review is complete when every hunk is covered.")
    if result.added_slots:
        print(f"New slots since the last prepare: {', '.join(result.added_slots)}")
    if covered:
        print(f"Every hunk is covered. Run `{check}`.")
        return
    todo = [status.slot.id for status in statuses if status.state in ("unlaunched", "dead")]
    running = [status.slot.id for status in statuses if status.state == "running"]
    if running:
        print(f"Running: {', '.join(running)} - do not launch these again; end your turn and wait for their reports.")
    if todo:
        for line in huntview.launch_lines(todo, check=check):
            print(line)
    elif not running:
        print(f"Every slot is done. Run `{check}`.")


def _hunk_span(new_start: int, new_len: int, old_start: int = 0, old_len: int = 0) -> tuple[int, int]:
    if new_len > 0:
        return new_start, new_start + new_len - 1
    if old_len > 0:
        return old_start, old_start + old_len - 1
    return 0, 0


def hunks_json(
    root: Path, round_: Round, snap: DiffSnapshot, missing: Sequence[str] | None = None
) -> list[dict[str, Any]]:
    """The round's hunks; for a current round each says whether it is `covered` (when `missing` is given).

    A current round lists its open hunks, and with `missing` also the carried hunks that are in it (`"carried": true`).
    """
    if round_.fingerprint != snap.fingerprint:
        return [
            {
                "id": ref.id,
                "path": ref.path,
                "new_start": ref.new_start,
                "new_len": ref.new_len,
                "end_line": ref.new_start + ref.new_len - 1 if ref.new_len > 0 else ref.new_start,
                "carried": ref.carried,
            }
            for ref in round_.hunks
        ]
    index = WorktreeIndex(root)
    by_id = {hunk.id: hunk for hunk in snap.hunks}
    items: list[dict[str, Any]] = []
    for ref in round_.hunks:
        # A carried hunk is listed only when a slot still misses it (a carried sweep or wave slot lists it), so
        # that `hunks` and `slots` never disagree about what is left.
        if (ref.carried and (missing is None or ref.id not in missing)) or (hunk := by_id.get(ref.id)) is None:
            continue
        scope = index.hunk_scope(hunk)
        start, end = _hunk_span(hunk.new_start, hunk.new_len, hunk.old_start, hunk.old_len)
        items.append(
            {
                "id": ref.id,
                "path": ref.path,
                "new_start": ref.new_start,
                "new_len": ref.new_len,
                "start_line": start,
                "end_line": end,
                "carried": ref.carried,
                "status": hunk.status,
                "scope": scope.name if scope is not None and scope.kind != "block" else None,
                **({} if missing is None else {"covered": ref.id not in missing}),
            }
        )
    return items


def _format_span(path: str, start: int, end: int, has_range: bool, *, bare_zero: bool = False) -> str:
    if has_range:
        return f"{path}:{start}-{end}"
    return path if bare_zero else f"{path}:{start}"


def hunk_map_lines(
    root: Path, round_: Round, snap: DiffSnapshot, missing: Sequence[str], slots: Sequence[SlotStatus] | None = None
) -> list[str]:
    """One compact `H#  path:start-end  scope|status` line per hunk still to cover, then `N left`.

    A stale round lists its HunkRef fields instead: its coverage no longer matches the code. A round judged
    by slots (`slots` is given) lists, per slot that is not done, the hunks that slot still misses.
    """
    entries = hunks_json(root, round_, snap)
    if round_.fingerprint != snap.fingerprint:
        # What the stale round recorded, not what is covered now: nothing here says "open".
        lines = [
            f"{e['id']}  {_format_span(e['path'], e['new_start'], e['end_line'], e['new_len'] > 0)}  "
            f"[{'carried' if e['carried'] else f'round {round_.number}'}]"
            for e in entries
        ]
        lines.append(huntview.STALE_NOTE)
        return lines
    by_id = {e["id"]: e for e in entries}

    def hunk_line(hunk_id: str) -> str:
        entry = by_id.get(hunk_id)
        if entry is None or entry["carried"]:  # a carried hunk that a slot lists: shown as the round recorded it
            ref = next(ref for ref in round_.hunks if ref.id == hunk_id)
            end = ref.new_start + ref.new_len - 1 if ref.new_len > 0 else ref.new_start
            return f"{hunk_id}  {_format_span(ref.path, ref.new_start, end, ref.new_len > 0)}  [carried]"
        span = _format_span(entry["path"], entry["start_line"], entry["end_line"], entry["end_line"] > 0, bare_zero=True)
        return f"{hunk_id}  {span}  {entry['scope'] or entry['status']}"

    if slots is not None:
        pending = [status for status in slots if status.state != "done"]
        lines = [
            line
            for status in pending
            for line in (
                f"{status.slot.id} [{huntview.STATE_LABELS[status.state]}] still misses "
                f"{huntview.hunk_ranges(status.missing)}:",
                *(f"  {hunk_line(hunk_id)}" for hunk_id in sorted(status.missing, key=lambda item: int(item[1:]))),
            )
        ]
        return [*lines, f"{len(pending)} slot(s) left to cover" if pending else "0 left to cover: every slot is done"]
    lines = [hunk_line(e["id"]) for e in entries if e["id"] in missing]
    if not entries:
        return ["(no open hunks)"]
    return [*lines, f"{len(lines)} left to cover" if lines else "0 left to cover: every hunk is covered"]


def round_line(round_: Round, snap: DiffSnapshot) -> str:
    carried = len(round_.hunks) - len(round_.open_hunk_ids)
    if round_.fingerprint == snap.fingerprint:
        freshness = "current"
    elif snap.is_empty:
        freshness = f"the work tree matches base {snap.base_sha[:12]}: nothing left to review"
    else:
        freshness = "stale: the code changed, run `craftsman review prepare`"
    return (
        f"Round {round_.number}{' (deep)' if round_.deep else ''} prepared {round_.prepared_at} · "
        f"{len(round_.hunks)} hunks ({carried} carried) · {freshness}"
    )


def round_json(round_: Round, snap: DiffSnapshot) -> dict[str, Any]:
    return {
        "number": round_.number,
        "deep": round_.deep,
        "prepared_at": round_.prepared_at,
        "current": round_.fingerprint == snap.fingerprint,
        "hunks": len(round_.hunks),
        "carried": len(round_.hunks) - len(round_.open_hunk_ids),
        "packet": round_.packet_path,
    }


def coverage_json(ledger: Ledger, round_: Round, slots: Sequence[SlotStatus] | None = None) -> dict[str, Any]:
    """Hunk coverage of `round_`; for a round judged by `slots`, the slots done and the hunks they still miss."""
    if slots is not None:
        return {
            "unit": "slots",
            "done": sum(status.state == "done" for status in slots),
            "total": len(slots),
            "missing": _slot_missing(slots),
        }
    missing = uncovered_hunks(ledger, round_)
    total = len(round_.open_hunk_ids)
    return {"covered": total - len(missing), "total": total, "missing": missing}


def _coverage_line(ledger: Ledger, round_: Round) -> str:
    entry = coverage_json(ledger, round_)
    missing = f" (missing {','.join(entry['missing'])})" if entry["missing"] else ""
    return f"  {entry['covered']}/{entry['total']} hunks{missing}"


def _slot_missing(slots: Sequence[SlotStatus]) -> list[str]:
    """The hunks that some slot still misses, in hunk order."""
    return sorted({hunk_id for status in slots for hunk_id in status.missing}, key=lambda hunk_id: int(hunk_id[1:]))


def not_covered(ledger: Ledger, slots: Sequence[SlotStatus] | None) -> list[str]:
    """The hunks of the latest round that are not covered, by the measure that round is judged by.

    `slots` is `hunt_slots(ledger)`. A round judged by slots (enforced, with a plan) lacks the hunks that some
    slot misses: its hunk coverage proves nothing, since only a slot's own vouched coverage counts. Any other
    round lacks the hunks that no coverage record names.
    """
    if not ledger.rounds:
        return []
    return uncovered_hunks(ledger, ledger.rounds[-1]) if slots is None else _slot_missing(slots)


def progress_lines(ledger: Ledger, round_: Round, slots: Sequence[SlotStatus] | None) -> list[str]:
    """How far the latest round is, as the gate counts it (see `not_covered`)."""
    if slots is None:
        return ["Coverage (covered/total non-carried hunks):", _coverage_line(ledger, round_)]
    missing = ", ".join(_slot_missing(slots)) or "none"
    return [hunt_line(slots), f"Hunks still missing from some slot: {missing}"]


def findings_table(findings: Sequence[Finding]) -> list[str]:
    if not findings:
        return ["Findings: none recorded."]
    lines = [
        "| ID | Severity | Verdict | Resolution | Location | Title |",
        "| :--- | :--- | :--- | :--- | :--- | :--- |",
    ]
    for finding in findings:
        run = f" ({finding.verdict_run})" if finding.verdict_run else ""
        verdict = f"{finding.verdict or '—'}{run}"
        lines.append(
            f"| {finding.id} | {finding.severity} | {verdict} | {finding.resolution or '—'} | "
            f"`{_cell(finding.path)}:{finding.line}` | {_cell(finding.title)} |"
        )
    return lines


def finding_details(findings: Sequence[Finding]) -> list[str]:
    """Each finding's scenario and verdict evidence, so a verifier needs no `--json` call."""
    lines: list[str] = []
    for finding in findings:
        lines.append(f"{finding.id} scenario (lens {finding.lens}): {_flat(finding.scenario)}")
        if finding.verdict is not None:
            run = f", {finding.verdict_run}" if finding.verdict_run else ""
            lines.append(f"{finding.id} evidence ({finding.verdict}{run}): {_flat(finding.verdict_evidence)}")
    return lines


def _flat(text: str) -> str:
    """Prose on one line, unable to pass for the output of a recording command (see `defang_tokens`)."""
    return " ".join(defang_tokens(text).split())


def _cell(text: str) -> str:
    """Text safe for one Markdown table cell: pipes escaped, line breaks flattened, record shapes defanged."""
    return defang_tokens(inline_md(text, table=True))


def prose(text: str) -> str:
    """Stored prose inline in Markdown, unable to pass for the output of a recording command."""
    return defang_tokens(inline_md(text))


def run_lines(runs: Sequence[Run], snap: DiffSnapshot) -> list[str]:
    if not runs:
        return ["Runs: none recorded."]
    lines = [f"Runs (last {len(runs)}):"]
    for run in runs:
        freshness = "current" if run.fingerprint == snap.fingerprint else "stale"
        lines.append(
            f"- {run.id} {run.kind} {run.exit_text} · {run.duration_s:.1f}s · {freshness} · "
            f"`{run.short_command}`"
        )
    return lines


ABANDONED_BANNER = "REVIEW ABANDONED - NOT COMPLETE"


ABANDONED_NOTE = (
    "the Stop hook gave up on this review after repeated reminders; do not present it as complete"
)


def print_hook_errors(ws: Workspace, ledger: Ledger) -> None:
    """The line that says the platform hooks failed since the latest round was prepared (nothing when none did)."""
    line = hook_errors.summary_for(ws.ledger_path, ledger)
    if line is not None:
        print(f"\n{line}")


def add_hook_errors(payload: dict[str, Any], ws: Workspace, ledger: Ledger) -> None:
    summary = hook_errors.summary_json(ws.ledger_path, ledger)
    if summary is not None:
        payload["hook_errors"] = summary


def slots_json(slots: Sequence[SlotStatus]) -> list[dict[str, Any]]:
    return [
        {
            "id": status.slot.id,
            "state": status.state,
            "lenses": list(status.slot.lenses),
            "hunks": list(status.slot.hunks),
            "missing": list(status.missing),
        }
        for status in slots
    ]


def report_json(report: GateReport, ledger: Ledger, *, abandoned: bool = False) -> dict[str, Any]:
    latest = ledger.rounds[-1] if ledger.rounds else None
    return {
        "passed": report.passed,
        "partial": report.partial,
        "self_reviewed": report.self_reviewed,
        "enforced": report.enforced,
        "platform": latest.platform if latest is not None else "cli",
        "abandoned": abandoned,
        "slots": slots_json(report.slots),
        "issues": [{"code": issue.code, "message": issue.message} for issue in report.issues],
        "warnings": list(report.warnings),
        "summary": report.summary,
    }


def finding_groups(findings: Sequence[Finding]) -> dict[str, list[Finding]]:
    groups: dict[str, list[Finding]] = {
        "fixed": [],
        "accepted": [],
        "open": [],
        "question": [],
        "dismissed": [],
    }
    for finding in sorted(findings, key=lambda item: SEVERITIES.index(item.severity)):
        if finding.verdict == "dismissed":
            groups["dismissed"].append(finding)
        elif finding.verdict == "question":
            groups["question"].append(finding)
        elif finding.resolution is not None:
            groups[finding.resolution].append(finding)
        else:
            groups["open"].append(finding)
    return groups


def _report_status_line(report: GateReport) -> str:
    subject = "Review" if report.partial else report.subject.capitalize()
    line = f"- {subject}: {report.status_label}"
    if report.passed:
        return line
    # The check that judges what the report just judged (a review-only report is never made under a gate).
    check = (
        huntview.REVIEW_ONLY_CHECK_COMMAND if report.review_only or report.partial else huntview.CHECK_COMMAND
    )
    return f"{line} — {report.issue_codes}; see `{check}`"


def _not_covered_lines(
    ledger: Ledger, snap: DiffSnapshot, report: GateReport, slots: Sequence[SlotStatus] | None
) -> list[str]:
    missing = not_covered(ledger, slots)
    if missing:
        what = "Hunks" if slots is None else "Hunks still missing from some slot"
        return ["", "## Not covered", f"- {what}: {', '.join(missing)}"]
    if report.partial and not ledger.rounds and not snap.is_empty:
        return ["", "## Not covered", "- No review round prepared yet."]
    return []


def _hunt_report_lines(report: GateReport) -> list[str]:
    if not report.slots:
        return []
    stale = [huntview.STALE_NOTE] if any(issue.code == "stale-round" for issue in report.issues) else []
    return [
        "",
        "## Hunt",
        hunt_line(report.slots),
        *stale,
        "```",
        *huntview.plan_lines(report.slots, with_state=True),
        "```",
    ]


def _report_finding_lines(ledger: Ledger, groups: dict[str, list[Finding]]) -> list[str]:
    if not ledger.findings:
        return ["No findings recorded."]
    detail: dict[str, Callable[[Finding], str]] = {
        "fixed": lambda f: f"Fix: {prose(f.resolution_note)}",
        "accepted": lambda f: f"Accepted because: {prose(f.resolution_note)}",
        "open": lambda f: (
            f"Verdict: {f.verdict}" if f.verdict is not None else "Verdict: none yet (unverified)"
        ),
        "question": lambda f: f"Question: {prose(f.verdict_evidence)}",
        "dismissed": lambda f: f"Dismissed: {prose(f.verdict_evidence)}",
    }
    lines: list[str] = []
    for name, items in groups.items():
        if items:
            lines.append(f"**{name.capitalize()} ({len(items)})**")
            lines.extend(
                f"- {f.id} · {f.severity} · `{f.path}:{f.line}` — {prose(f.title)}. {detail[name](f)}"
                for f in items
            )
    return lines


def _not_assessed_lines(report: GateReport) -> list[str]:
    if report.self_reviewed:
        isolation = f"Reviewer isolation status: {SELF_REVIEWED_BANNER}."
    elif report.passed:
        isolation = "Reviewer isolation status: verified via subagent stop events in the ledger."
    else:
        isolation = "Reviewer isolation: not established yet (review incomplete)."
    return [
        "",
        "## Not assessed by the gate",
        "- Business requirements fit and domain behavior not exercised by recorded runs.",
        "- Naming quality and domain clarity.",
        "- Architecture and invariants outside the changed files.",
        f"- {isolation}",
    ]


def _usage_section(ledger: Ledger) -> list[str]:
    lines = usage_lines(ledger)
    return ["", "## Agent usage", *lines] if lines else []


def _waived_run_lines(ledger: Ledger) -> list[str]:
    waived = [(run, run.waiver) for run in ledger.runs if run.waiver is not None]
    if not waived:
        return []
    return [
        "",
        "## Waived runs",
        *(
            f"- {run.id} `{run.command}` ({run.kind}, exit {run.exit_code}) — {prose(waiver.reason)}"
            for run, waiver in waived
        ),
    ]


def report_lines(
    ledger: Ledger,
    snap: DiffSnapshot,
    deep: bool,
    report: GateReport,
    groups: dict[str, list[Finding]],
    runs: Sequence[Run],
    slots: Sequence[SlotStatus] | None,
    *,
    abandoned: bool = False,
) -> list[str]:
    banner = [f"**{SELF_REVIEWED_BANNER}**"] if report.self_reviewed else []
    if abandoned:
        banner.append(f"**{ABANDONED_BANNER}**: {ABANDONED_NOTE}.")
    run_lines = [
        f"- {run.id} `{run.command}` ({run.kind}) → exit {run.exit_code} in {run.duration_s:.1f}s"
        for run in runs
    ] or ["No passing test or e2e run at the final code."]
    warning_lines = ["", "## Warnings", *(f"- {w}" for w in report.warnings)] if report.warnings else []
    return [
        "# Craftsman review report",
        *banner,
        f"- Base `{snap.base_sha[:7]}` · {'deep · ' if deep else ''}{len(ledger.rounds)} review round(s) · "
        f"{len(snap.files)} files, +{snap.added}/−{snap.deleted} lines",
        _report_status_line(report),
        *_hunt_report_lines(report),
        *_not_covered_lines(ledger, snap, report, slots),
        "",
        "## Findings",
        *_report_finding_lines(ledger, groups),
        "",
        "## Verification at the final code",
        *run_lines,
        *_waived_run_lines(ledger),
        *_usage_section(ledger),
        *warning_lines,
        *_not_assessed_lines(report),
    ]
