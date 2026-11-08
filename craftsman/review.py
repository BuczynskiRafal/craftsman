"""`craftsman review`: prepare review rounds and record or inspect evidence in the ledger.

This is the entry module: `main`, the commands that build or judge a round (`prepare`, `view`, `brief`, `check`,
`report`) and the error text. The rest is split by responsibility: `review_parser` (arguments), `review_scope`
(which files a review covers), `review_ingest` (`--from-json` batches), `review_records` (commands that write
evidence), `review_inspect` (commands that read or tidy), `review_present` (everything printed) and `review_common`.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from craftsman import huntplan, huntview
from craftsman.agy_sync import caller_parent, sync_antigravity_runs
from craftsman.assets import TemplateRole, load_agent_templates
from craftsman.attribution import agent_runs
from craftsman.diffscan import (
    DiffScanError,
    DiffSnapshot,
    snapshot,
)
from craftsman.evidence import (
    effective_deep,
    packet_path,
    prune_all_temp_dirs,
    prune_inbox_files,
    prune_round_files,
)
from craftsman.files import write_text_atomic
from craftsman.gate import (
    TAKEOVER_NOTE,
    GateState,
    GateStateError,
    cli_caller,
    state_path_for,
    update_state,
)
from craftsman.gatecheck import (
    GatePolicy,
    GateReport,
    evaluate,
    hunt_slots,
    passing_runs,
    render_report,
    require_current_round,
    slot_statuses,
)
from craftsman.generated import ExportError
from craftsman.ledger import (
    add_claim,
    load,
    mutate,
    open_round,
    round_unchanged,
    stable_hunk_ids,
    stamp_prepared,
)
from craftsman.packet import (
    PacketContext,
    collect_context,
    render_packet,
    render_packet_view,
)
from craftsman.paths import default_repo_dir
from craftsman.quality import collect_quality_signals
from craftsman.records import (
    BatchRejected,
    FieldError,
    Ledger,
    LedgerError,
    QualitySignal,
    Round,
    Slot,
    SlotClaim,
    parse_timestamp,
    slot_generation,
)
from craftsman.review_common import PROG, CommandRefused, comma_list, cwd_of, slot_list, store_due_waves
from craftsman.review_ingest import decode_input
from craftsman.review_inspect import (
    handle_inbox,
    handle_reset,
    handle_scout,
    handle_scratch,
    handle_show,
)
from craftsman.review_parser import Handler, build_parser
from craftsman.review_present import (
    ABANDONED_BANNER,
    ABANDONED_NOTE,
    add_hook_errors,
    finding_groups,
    not_covered,
    print_empty_prepare,
    print_hook_errors,
    print_prepare_summary,
    print_scope_summary,
    print_unscanned_files,
    report_json,
    report_lines,
)
from craftsman.review_records import (
    handle_add,
    handle_cover,
    handle_merge,
    handle_move,
    handle_resolve,
    handle_verdict,
    handle_waive_run,
)
from craftsman.review_scope import (
    normalize_round_files,
    prepare_scope_and_snapshot,
    view_hunk_ids,
    view_slug,
)
from craftsman.usage import usage_json
from craftsman.workspace import (
    Workspace,
    brief_platform,
    caller_identity,
    end_review_guard,
    live_marker,
    open_workspace,
    read_marker,
    review_enforcement,
)
from craftsman.worktree_index import WorktreeIndex

MAX_REPORTED_BATCH_PROBLEMS = 20


# Ledger record fields that come straight from a command-line flag, for error messages.
FIELD_FLAGS = {
    "lens": "--lens",
    "hunks": "--hunks",
    "files": "--files",
    "paths": "--paths",
    "add_paths": "--add-paths",
    "path": "--file",
    "line": "--line",
    "severity": "--severity",
    "title": "--title",
    "scenario": "--scenario",
    "note": "--note",
    "verdict": "--as",
    "verdict_evidence": "--evidence",
    "verdict_run": "--run",
    "resolution": "--as",
    "resolution_note": "--note",
    "reason": "--reason",
    "hunter": "--hunter",
    "slot": "slot",
    "verifier": "--verifier",
}


MAX_TASK_CHARS = 4000


GATE_SESSION_CHARS = 8


@dataclass(frozen=True)
class _PrepareContext:
    """Everything `prepare` decides before it takes the ledger lock."""

    ws: Workspace
    env: Mapping[str, str]
    snap: DiffSnapshot
    scope: tuple[str, ...] | None
    allow_scope_expansion: bool
    out_of_scope: tuple[str, ...]
    depth: huntplan.Depth
    symbols: Mapping[str, tuple[str, ...]]
    explicit_task: str
    marker_task: str
    now: datetime


@dataclass(frozen=True)
class _Prepared:
    """What `prepare` did to the ledger."""

    ledger: Ledger
    round: Round
    created: bool
    raised: bool
    added_slots: tuple[str, ...]  # slots a re-prepare of the same diff appended to the plan
    task_changed: bool = False  # a re-prepare of the same diff gave the round a different task text
    refreshed: bool = False  # a re-prepare of the same diff released carried hunks or made the round enforced


def _read_task_file(source: str) -> str:
    """The task text of `--task-file` (`-` is stdin): trimmed, at most `MAX_TASK_CHARS` characters."""
    label = "stdin" if source == "-" else source
    try:
        data = sys.stdin.buffer.read() if source == "-" else Path(source).read_bytes()
    except OSError as exc:
        raise LedgerError(f"cannot read {label}: {exc}") from exc
    return decode_input(data, label).strip()[:MAX_TASK_CHARS]


def _explicit_task(args: argparse.Namespace) -> str:
    if args.task is not None and args.task_file is not None:
        raise LedgerError("pass either --task or --task-file, not both")
    raw = args.task if args.task is not None else _read_task_file(args.task_file) if args.task_file else ""
    return raw.strip()[:MAX_TASK_CHARS]


def _depth_floor(
    args: argparse.Namespace, ws: Workspace, ledger: Ledger, marker_deep: bool, *, owned: bool
) -> tuple[bool, str | None]:
    """Whether something already asks for a deep round (never lowered inside a review), and what."""
    reasons = [
        reason
        for asked, reason in (
            (bool(args.deep) and owned, "requested with --deep"),
            (ws.state is not None and ws.state.deep, "the gate is deep"),
            (any(round_.deep for round_ in ledger.rounds), "an earlier round was deep"),
            (marker_deep, "a deep review was requested"),
        )
        if asked
    ]
    return bool(reasons), (reasons[0] if reasons else None)


def _prepare(args: argparse.Namespace) -> int:
    env = os.environ
    now = datetime.now(timezone.utc)
    ws = open_workspace(args.cwd)
    explicit_task = _explicit_task(args)
    sync_antigravity_runs(ws, env)
    ledger = load(ws.ledger_path)
    base_sha = ws.base_sha(ledger, args.base)
    full_snap = snapshot(ws.root, base_sha)
    scope, snap, allow_scope_expansion = prepare_scope_and_snapshot(
        ws, ledger, args, base_sha, full_snap
    )
    if snap.is_empty:
        end_review_guard(ws.ledger_path)  # nothing to review is a review outcome
        return print_empty_prepare(scope, snap, full_snap)
    snap = stable_hunk_ids(ledger, snap)
    owned = _owns_gate(ws, env)
    if not owned and args.deep:
        print(
            "Note: --deep ignored: the active gate belongs to another session, "
            "which alone can raise it."
        )
    marker = live_marker(ws.ledger_path, now)  # a stale marker's depth and task belong to an older review
    floor, floor_reason = _depth_floor(args, ws, ledger, marker is not None and marker.deep, owned=owned)
    depth = huntplan.decide_depth(snap, floor=floor, floor_reason=floor_reason)
    if ws.state is not None and owned and depth.deep and not ws.state.deep:
        update_state(state_path_for(ws.root), lambda current: (_raised_to_deep(current), None))
        print("Gate raised to deep: a passing end-to-end run is now required.")
    index = WorktreeIndex(ws.root)
    symbols = {hunk.id: index.symbols(hunk) for hunk in snap.hunks}
    ctx = _PrepareContext(
        ws=ws,
        env=env,
        snap=snap,
        scope=scope,
        allow_scope_expansion=allow_scope_expansion,
        out_of_scope=tuple(sorted(set(full_snap.files) - set(scope))) if scope is not None else (),
        depth=depth,
        symbols=symbols,
        explicit_task=explicit_task,
        marker_task=marker.task.strip() if marker is not None else "",
        now=now,
    )

    # The hints parse every changed Python file: compute them before the ledger lock is taken, for
    # the hunks this round will have to review (carried hunks were reviewed already).
    planned = _plan_round(ledger, ctx, lambda _ids: ())
    hint_ids = planned.round.open_hunk_ids if planned.created or planned.refreshed else None
    hints = collect_quality_signals(ws.root, snap.only(hint_ids)) if hint_ids is not None else ()

    def signals_for(open_ids: tuple[str, ...]) -> tuple[QualitySignal, ...]:
        if open_ids == hint_ids:
            return hints
        return collect_quality_signals(ws.root, snap.only(open_ids))  # coverage landed meanwhile: rare, slow path

    def open_next(current: Ledger) -> tuple[Ledger, _Prepared]:
        if stable_hunk_ids(current, snap) != snap:
            raise LedgerError("the ledger changed while the round was prepared; run prepare again")
        result = _plan_round(current, ctx, signals_for)
        stamped, round_ = stamp_prepared(result.ledger)  # the review guard counts a prepare that got this far
        return stamped, replace(result, ledger=stamped, round=round_)

    result = mutate(ws.ledger_path, open_next)
    ledger, round_ = result.ledger, result.round
    packet = Path(round_.packet_path)
    if result.created or result.raised or result.refreshed or result.task_changed or not packet.is_file():
        pctx = _packet_context(ws, snap, ledger, round_, index, symbols, task=round_.task)
        _write_text(packet, render_packet(pctx))
    if ws.state is not None and not owned:
        print(_foreign_gate_note(ws.state))
    print_prepare_summary(result, packet, snap, depth, now, check=_check_command(ws))
    print_unscanned_files(snap)
    print_scope_summary(ws, ledger, round_, snap, full_snap)
    if result.created and not result.raised:
        prune_round_files(ws.ledger_path.parent, round_.number)
        # Scratch directories outlive rounds: a repro written in round 1 is re-run after the fix.
        prune_inbox_files(ws.ledger_path.parent)
    return 0


def _plan_round(
    current: Ledger, ctx: _PrepareContext, signals_for: Callable[[tuple[str, ...]], tuple[QualitySignal, ...]]
) -> _Prepared:
    """Open the next round for the snapshot (or take the latest one as it is) and give it its hunt plan.

    The steps follow the planner's contract: the lenses each hunk requires and has been credited are
    computed on the ledger; `open_round` carries over the hunks that owe nothing; the plan is built for
    what the open hunks still owe, appending to the plan the round already has (so re-preparing an
    unchanged diff never changes an existing slot or forgets a wave); finally the waves that are due are
    stored in it. When this opens a NEW round, a wave that is already due is stored BEFORE anything else,
    in the round being replaced, so that a verdict recorded since the last `check` is carried into the new
    round's first plan instead of being lost to it. When the round is only refreshed (an unchanged
    diff, or an in-place raise to deep), no wave is planned until its plan is rebuilt: the barrier of a wave
    must see the generation 1 slots the refresh adds, and the hunks it releases. Carry-over is decided on
    the credit before the round opens; the plan is built on the credit as the round being planned sees it
    (hunks the new round reopened have none).
    """
    snap = ctx.snap
    changing = not round_unchanged(current, snap, ctx.scope)
    base = huntplan.plan_due_waves(current, ctx.now) if changing else current
    required = huntplan.required_by_hunk(snap.hunks, deep=ctx.depth.deep)
    enforced, platform = review_enforcement(ctx.ws, current, ctx.env, ctx.now)
    if not changing and enforced and not base.rounds[-1].enforced:
        # The round is upgraded in place: it is judged as the enforced round it is about to be, so the credit
        # it gets (and the hunks it keeps carried) never rests on what only an advisory round accepts.
        upgraded = replace(base.rounds[-1], enforced=True, platform=platform)
        base = replace(base, rounds=(*base.rounds[:-1], upgraded))
    have = huntplan.credited(base, enforced=enforced)
    carry_sweep = changing and huntplan.sweep_pending(base)
    packet = packet_path(ctx.ws.ledger_path, len(base.rounds) + 1)
    opened, round_ = open_round(
        base, snap, ctx.depth.deep, str(packet), ctx.symbols,
        scope=ctx.scope, allow_scope_expansion=ctx.allow_scope_expansion, out_of_scope=ctx.out_of_scope,
        required=required, credited=have,
    )  # fmt: skip
    created = len(opened.rounds) > len(base.rounds)
    previous = base.rounds[-1] if base.rounds and not created else None
    earlier = base.rounds if created else base.rounds[:-1]
    open_ids = set(round_.open_hunk_ids)
    open_hunks = [hunk for hunk in snap.hunks if hunk.id in open_ids]
    pending = huntplan.pending_wave_slots(base, open_hunks=open_hunks, round_hunks=snap.hunks) if created else ()
    plan = huntplan.build_plan(
        open_hunks,
        huntplan.owed_lenses(required, round_.hunks, have),
        depth=ctx.depth,
        round_number=round_.number,
        first_deep_round=ctx.depth.deep and not any(item.deep for item in earlier),
        existing=round_.plan,
        pending_wave_slots=pending,
        round_hunks=snap.hunks,
        carry_sweep=carry_sweep,
        credited=huntplan.credited(opened, enforced=enforced),
    )
    if round_.enforced:  # sticky: the round keeps the platform that first held it
        enforced, platform = True, round_.platform
    stored_before = current.rounds[-1].plan if current.rounds else None
    known = {slot.id for slot in stored_before.slots} if stored_before is not None and not created else set()
    task = _round_task(ctx, base, round_, created=created)
    refreshed = not created and (round_.hunks != current.rounds[-1].hunks or enforced != current.rounds[-1].enforced)
    prepared = replace(
        round_,
        plan=plan,
        enforced=enforced,
        platform=platform,
        task=task,
        signals=signals_for(round_.open_hunk_ids) if created or refreshed else round_.signals,
    )
    updated = huntplan.plan_due_waves(replace(opened, rounds=(*opened.rounds[:-1], prepared)), ctx.now)
    prepared = updated.rounds[-1]
    return _Prepared(
        ledger=updated,
        round=prepared,
        created=created,
        raised=previous is not None and not previous.deep and prepared.deep,
        added_slots=tuple(slot.id for slot in prepared.plan.slots if slot.id not in known)
        if not created and prepared.plan is not None
        else (),
        task_changed=not created and task != round_.task,
        refreshed=refreshed,
    )


def _round_task(ctx: _PrepareContext, base: Ledger, round_: Round, *, created: bool) -> str:
    """What the round says the change is for.

    The explicit text wins. A re-prepared round otherwise keeps its own; a new one falls back on the review
    marker's, the gate's and finally the previous round's.
    """
    if created:
        previous = base.rounds[-1].task if base.rounds else ""
        candidates = (ctx.explicit_task, ctx.marker_task, ctx.ws.task.strip(), previous)
    else:
        candidates = (ctx.explicit_task, round_.task, ctx.marker_task, ctx.ws.task.strip())
    return next((text for text in candidates if text), "")


def _gate_label(state: GateState) -> str:
    """`claude session 1a2b3c4d`: whose gate it is, as a person can tell sessions apart."""
    return f"{state.owner.platform} session {(state.owner.session_id or 'unknown')[:GATE_SESSION_CHARS]}"


def _foreign_gate_note(state: GateState) -> str:
    return (
        f"This checkout has an active /craftsman gate ({_gate_label(state)}); this round follows it. "
        "For a separate review use its own worktree: git worktree add <path>. "
        f"{TAKEOVER_NOTE}"
    )


def _check_command(ws: Workspace) -> str:
    """The command that judges this checkout's review now (see `huntview.check_command`)."""
    return huntview.check_command(gate_active=ws.state is not None)


def _owns_gate(ws: Workspace, env: Mapping[str, str]) -> bool:
    """Whether this command acts for the session that owns the active gate (true without one)."""
    return ws.state is None or ws.owns_gate(env)


def _raised_to_deep(state: GateState | None) -> GateState | None:
    return replace(state, deep=True) if state is not None and state.active else state


def _packet_context(
    ws: Workspace,
    snap: DiffSnapshot,
    ledger: Ledger,
    round_: Round,
    index: WorktreeIndex,
    symbols: Mapping[str, tuple[str, ...]],
    *,
    task: str,
    full_hunks: Sequence[str] = (),
) -> PacketContext:
    """What the packet of `round_` shows; `full_hunks` are shown in full even when carried (slice views)."""
    return collect_context(
        ws.root,
        snap,
        round_,
        task,
        symbols=symbols,
        ledger=ledger,
        index=index,
        full_hunks=full_hunks,
    )


def _write_text(path: Path, text: str) -> None:
    """Replace `path` atomically: a reader never sees a half-written packet or view."""
    try:
        write_text_atomic(path, text)
    except OSError as exc:
        raise LedgerError(f"cannot write {path}: {exc}") from exc


def _view(args: argparse.Namespace) -> int:
    if (args.files is None) == (args.hunks is None):
        raise LedgerError("pass exactly one of --files or --hunks")
    ws = open_workspace(args.cwd)
    ledger = load(ws.ledger_path)
    snap = ws.snapshot(ledger)
    round_ = require_current_round(ledger, snap)
    files = None if args.files is None else normalize_round_files(ws.root, cwd_of(args), args.files, round_, snap)
    assigned = view_hunk_ids(round_, args, files)
    index = WorktreeIndex(ws.root)
    symbols = {hunk.id: index.symbols(hunk) for hunk in snap.hunks}
    ctx = _packet_context(ws, snap, ledger, round_, index, symbols, task=round_.task or ws.task, full_hunks=assigned)
    slug = view_slug(hunks=assigned) if args.hunks is not None else view_slug(files=files or ())
    view_path = Path(round_.packet_path).with_name(f"packet-{round_.number}.{slug}.md")
    _write_text(view_path, render_packet_view(ctx, assigned_hunks=assigned))
    print(view_path)
    return 0


def _role_text(role: TemplateRole) -> str | None:
    """The body of the agent template for `role` (includes expanded), or None when the assets are not found."""
    try:
        return next((item.body for item in load_agent_templates(default_repo_dir()) if item.role == role), None)
    except (ExportError, OSError):
        return None


def _inline_role_text(env: Mapping[str, str]) -> bool:
    """Whether a reader on this platform needs the role text in its brief (Claude Code and Copilot load it)."""
    return brief_platform(env) in ("antigravity", "cli")


def _brief(args: argparse.Namespace) -> int:
    if (args.slot is None) == (args.verifier is None):
        raise LedgerError("pass exactly one of a slot id (your launch prompt names it) or --verifier F1,F2")
    env = os.environ
    ws = open_workspace(args.cwd)
    sync_antigravity_runs(ws, env)
    if args.verifier is not None:
        return _brief_verifier(ws, env, args.verifier)
    slot_id = str(args.slot).strip()
    if slot_generation(slot_id) is None:
        raise FieldError("slot", f"{slot_id!r} is not a slot id (like W1-1)")
    snap = ws.snapshot()
    by = caller_identity(env)

    def claim(current: Ledger) -> tuple[Ledger, tuple[SlotClaim, Round, Slot, int]]:
        round_ = require_current_round(current, snap)
        if round_.plan is None:
            raise LedgerError(
                f"round {round_.number} has no hunt plan (it was prepared by an older craftsman); "
                "run `craftsman review reset` and prepare again"
            )
        current = huntplan.plan_due_waves(current, datetime.now(timezone.utc))
        round_ = current.rounds[round_.number - 1]
        assert round_.plan is not None
        required = round_.plan.slots
        slot = round_.plan.slot(slot_id)
        if slot is None:
            if slot_generation(slot_id) == 1:
                raise LedgerError(f"unknown slot {slot_id} in round {round_.number}; slots of round {round_.number}: {slot_list(required)}")
            raise CommandRefused(
                f"Slot {slot_id} is no longer required in round {round_.number} (the findings changed). "
                "Reply exactly that to the agent that launched you and stop."
            )
        updated, issued = add_claim(current, slot_id, by=by)
        hunters = sum(item.generation == slot.generation for item in required)
        return updated, (issued, updated.rounds[-1], slot, hunters)

    issued, round_, slot, hunters = mutate(ws.ledger_path, claim)
    ledger = load(ws.ledger_path)
    trigger = next((item for item in ledger.findings if item.id == slot.trigger), None) if slot.kind == "variant" else None
    print(f"Brief issued ({issued.token}) for {slot.id}, round {round_.number}.\n")
    print(
        huntview.hunter_brief(
            slot=slot,
            round_=round_,
            hunters=hunters,
            trigger=trigger,
            role_text=_role_text("reviewer") if _inline_role_text(env) else None,
            antigravity=brief_platform(env) == "antigravity",
            parent=caller_parent(env),
        ),
        end="",
    )
    return 0


def _brief_verifier(ws: Workspace, env: Mapping[str, str], raw_ids: str) -> int:
    """The verifier's instruction for the named findings. Records nothing: a verifier's work is its verdicts."""
    ids = list(dict.fromkeys(comma_list(raw_ids)))
    if not ids:
        raise FieldError("verifier", "must name at least one finding id (F1,F2)")
    ledger = load(ws.ledger_path)
    by_id = {finding.id: finding for finding in ledger.findings}
    unknown = [finding_id for finding_id in ids if finding_id not in by_id]
    if unknown:
        known = ", ".join(by_id) or "none"
        raise LedgerError(f"unknown finding(s): {', '.join(unknown)}; findings in the ledger: {known}")
    latest = ledger.rounds[-1] if ledger.rounds else None
    print(
        huntview.verifier_brief(
            findings=[by_id[finding_id] for finding_id in ids],
            round_=latest,
            base_sha=latest.base_sha if latest is not None else ws.base_sha(ledger),
            task=(latest.task if latest is not None else "") or ws.task,
            role_text=_role_text("verifier") if _inline_role_text(env) else None,
            antigravity=brief_platform(env) == "antigravity",
            parent=caller_parent(env),
        ),
        end="",
    )
    return 0


def _evaluate(
    ws: Workspace, *, review_only: bool
) -> tuple[Ledger, DiffSnapshot, bool, GateReport]:
    if review_only and ws.state is not None:
        raise LedgerError(
            f"--review-only is not allowed: this checkout has an active /craftsman gate ({_gate_label(ws.state)}); "
            "the full gate applies to every review command here (run `craftsman review check` without --review-only)"
        )
    ledger = _with_due_waves(ws)
    deep = effective_deep(ledger, ws.state is not None and ws.state.deep)
    snap = ws.snapshot(ledger)
    policy = GatePolicy(deep=deep, isolation_evidence=ws.isolation_evidence(ledger))
    return ledger, snap, deep, evaluate(ledger, snap, policy, review_only=review_only)


def _with_due_waves(ws: Workspace) -> Ledger:
    """The ledger with the waves that are due stored in the plan of its latest round (under the lock, only if any are)."""
    now = datetime.now(timezone.utc)
    ledger = load(ws.ledger_path)
    if store_due_waves(ledger, now) is ledger:
        return ledger  # the usual case: take no lock

    def plan(current: Ledger) -> tuple[Ledger, Ledger]:
        planned = store_due_waves(current, now)
        return planned, planned

    return mutate(ws.ledger_path, plan)


def _abandoned(ws: Workspace, ledger: Ledger, report: GateReport) -> bool:
    """Whether the Stop hook gave up on this review and it is still not complete.

    The marker outlives the review it guarded, so its `abandoned_at` counts only when it is not older than the
    latest round (a `reset` or a newer round starts afresh) and the report has not passed since.
    """
    marker = read_marker(ws.ledger_path)
    if marker is None or marker.abandoned_at is None or not ledger.rounds or report.passed:
        return False
    try:
        return parse_timestamp(marker.abandoned_at) >= parse_timestamp(ledger.rounds[-1].prepared_at)
    except ValueError:
        return False  # a stamp that cannot be read proves nothing


def _check(args: argparse.Namespace) -> int:
    ws = open_workspace(args.cwd)
    sync_antigravity_runs(ws, os.environ)
    ledger, _, _, report = _evaluate(ws, review_only=args.review_only)
    abandoned = _abandoned(ws, ledger, report)
    if args.json:
        payload = report_json(report, ledger, abandoned=abandoned)
        add_hook_errors(payload, ws, ledger)
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        if abandoned:
            print(f"**{ABANDONED_BANNER}**: {ABANDONED_NOTE}.\n")
        print(render_report(report))
        if report.slots:
            print("\n**Hunt slots**")
            print("\n".join(huntview.plan_lines(report.slots, with_state=True)))
        print_hook_errors(ws, ledger)
        if args.review_only:
            _warn_new_out_of_scope(ws, ledger)
    return 0 if report.passed else 1


def _report(args: argparse.Namespace) -> int:
    ws = open_workspace(args.cwd)
    if args.partial and not args.review_only:
        if ws.state is not None:
            raise LedgerError(
                f"--partial is for a review-only report, which is refused here: this checkout has an active "
                f"/craftsman gate ({_gate_label(ws.state)}); run `craftsman review report`"
            )
        raise LedgerError(
            "--partial requires --review-only; pass `craftsman review report --review-only --partial`"
        )
    sync_antigravity_runs(ws, os.environ)
    ledger, snap, deep, report = _evaluate(ws, review_only=args.review_only)
    if args.partial:
        _require_hunters_launched(ws, ledger)
        report = replace(report, partial=True)
    abandoned = _abandoned(ws, ledger, report)
    groups = finding_groups(ledger.findings)
    runs = passing_runs(ledger, snap.fingerprint)
    slots = hunt_slots(ledger)
    if args.json:
        payload = {
            **report_json(report, ledger, abandoned=abandoned),
            "deep": deep,
            "rounds": len(ledger.rounds),
            "base_sha": snap.base_sha,
            "diff": {"files": len(snap.files), "added": snap.added, "deleted": snap.deleted},
            "not_covered": not_covered(ledger, slots),
            "findings": {name: [f.to_json() for f in items] for name, items in groups.items()},
            "runs": [run.to_json() for run in runs],
            "waived_runs": [run.to_json() for run in ledger.runs if run.waiver is not None],
            "agent_usage": usage_json(ledger),
        }
        add_hook_errors(payload, ws, ledger)
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print("\n".join(report_lines(ledger, snap, deep, report, groups, runs, slots, abandoned=abandoned)))
        print_hook_errors(ws, ledger)
        _warn_new_out_of_scope(ws, ledger)
    if args.review_only and report.passed:
        # Only a review that passed is over. A report of an unfinished one ends nothing: pruning would delete the
        # repro scripts and inbox files of work in progress, and ending the guard would let a session out of an
        # enforced review (`--partial` after one hunter is not a way out; the guard has its own exits).
        _end_guard_of_caller(ws, os.environ)
        prune_all_temp_dirs(ws.ledger_path.parent)
    return 0


def _end_guard_of_caller(ws: Workspace, env: Mapping[str, str]) -> None:
    """End the review guard if it holds the session that runs this command, not another live session's.

    Only Claude Code exports a session id to the commands it runs. A command that cannot tell which
    session it serves can only be the one the single guard of this ledger holds, so it ends it.
    """
    caller = cli_caller(None, env)
    end_review_guard(ws.ledger_path, caller if caller.session_id is not None else None)


def _require_hunters_launched(ws: Workspace, ledger: Ledger) -> None:
    """A partial report needs a round, and one that is enforced needs at least one hunter launched in it (a claim or a start/stop).

    Without a round nothing was prepared, let alone reviewed. Otherwise nothing was reviewed independently and
    `--partial` would only be a way out of the review.

    A bare claim counts although the orchestrator could fetch a brief itself: a session whose hooks recorded
    no start event must not be trapped, and the Phase F PreToolUse hook denies `brief` to the main session.
    """
    latest = ledger.rounds[-1] if ledger.rounds else None
    if latest is None:
        raise CommandRefused(
            "No review round yet: run `craftsman review prepare` and launch the hunters it lists."
        )
    if not latest.enforced or latest.plan is None:
        return
    prepared = parse_timestamp(latest.prepared_at)
    if any(claim.round == latest.number for claim in ledger.claims) or any(
        run.role == "reviewer" and run.started >= prepared for run in agent_runs(ledger)
    ):
        return
    pending = [
        status.slot.id
        for status in slot_statuses(ledger, latest, datetime.now(timezone.utc))
        if status.state != "done"
    ]
    launch = (
        "\n".join(huntview.launch_lines(pending, check=_check_command(ws)))
        if pending
        else "Run `craftsman review prepare`."
    )
    raise CommandRefused(
        f"no Craftsman hunter was launched for round {latest.number}, so a partial report would describe a diff "
        f"nobody reviewed. Launch the hunters first.\n{launch}"
    )


def _warn_new_out_of_scope(ws: Workspace, ledger: Ledger) -> None:
    """Warn about changed files outside the scope that were not already changed when it was frozen.

    The baseline is the first scoped round: a later round records whatever the session changed
    in between as its own baseline, which would hide exactly the files this warning is for.
    """
    scoped = [round_ for round_ in ledger.rounds if round_.scope is not None]
    latest = ledger.rounds[-1] if ledger.rounds else None
    if not scoped or latest is None or latest.scope is None:
        return
    full_now = snapshot(ws.root, latest.base_sha)
    current_out = set(full_now.files) - set(latest.scope)
    new_out = sorted(current_out - set(scoped[0].out_of_scope))
    if new_out:
        paths = ", ".join(new_out)
        print(
            f"Warning: Files created or changed during this review outside its scope: {paths}\n"
            "delete or revert them if this session made them"
        )


def _handlers() -> dict[str, Handler]:
    """The function that runs each `craftsman review` subcommand."""
    return {
        "prepare": _prepare,
        "view": _view,
        "scout": handle_scout,
        "cover": handle_cover,
        "brief": _brief,
        "add": handle_add,
        "move": handle_move,
        "merge": handle_merge,
        "verdict": handle_verdict,
        "resolve": handle_resolve,
        "waive-run": handle_waive_run,
        "show": handle_show,
        "check": _check,
        "report": _report,
        "reset": handle_reset,
        "inbox": handle_inbox,
        "scratch": handle_scratch,
    }


def _build_parser() -> argparse.ArgumentParser:
    return build_parser(_handlers())


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    try:
        result: int = args.handler(args)
    except BatchRejected as exc:
        sys.stderr.write(f"{format_batch_rejection(exc)}\n")
        return 1
    except CommandRefused as exc:
        sys.stderr.write(f"{PROG}: error: {exc}\n")
        return exc.exit_code
    except (LedgerError, DiffScanError, GateStateError) as exc:
        sys.stderr.write(f"{PROG}: error: {describe_error(exc)}\n")
        return 1
    return result


def format_batch_rejection(exc: BatchRejected) -> str:
    """Render every rejected batch entry without the `error:` prefix, capped at 20 lines."""
    lines = [
        str(exc),
        "Fix all of them, then send the corrected list once:",
    ]
    shown = exc.problems[:MAX_REPORTED_BATCH_PROBLEMS]
    for item in shown:
        head = f"entry {item.index} ({item.label})" if item.label else f"entry {item.index}"
        detail = f"{item.field}: {item.problem}" if item.field else item.problem
        lines.append(f"  {head}: {detail}")
    hidden = len(exc.problems) - len(shown)
    if hidden > 0:
        lines.append(f"  ... and {hidden} more")
    return "\n".join(lines)


def describe_error(exc: LedgerError | DiffScanError | GateStateError) -> str:
    """The error message, naming the command-line flag when a flag's value was rejected."""
    if isinstance(exc, FieldError) and exc.field in FIELD_FLAGS:
        return f"{FIELD_FLAGS[exc.field]} {exc.problem}"
    return str(exc)
