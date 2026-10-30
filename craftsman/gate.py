"""The Craftsman quality gate: the state of a `/craftsman` task and the decision whether it may end.

`craftsman gate --init` opens a gate for a task in the current git work tree; the prompt hooks
open one when the user types `/craftsman` or `ultracraft`. The state lives next to the evidence
ledger in `<git-dir>/craftsman/craftsman_state.json`, and every change to it happens under a file
lock. While a gate is active, the stop hooks keep the agent working until the ledger passes the
gate (`craftsman review check`), at most `MAX_CONTINUATIONS` times in a row without progress.
`craftsman gate --complete` closes the gate, which is the only enforcement on platforms without
stop hooks.

Each gate belongs to the agent session that opened it (`GateOwner`), so a parallel session in the
same checkout neither triggers nor changes it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Literal, TypeVar, get_args

from craftsman.diffscan import (
    DiffScanError,
    DiffSnapshot,
    git_dir,
    resolve_base_sha,
    snapshot,
    worktree_root,
)
from craftsman.evidence import (
    Evaluation,
    archive_ledger_if_rebased,
    evaluate_evidence,
    prune_inbox_files,
)
from craftsman.files import write_text_atomic
from craftsman.gatecheck import (
    QUICK_TIER_MAX_CHANGED_LINES,
    GateReport,
    has_unresolved_diff_size,
    render_next_actions,
    render_report,
)
from craftsman.ledger import locked
from craftsman.lenses import Tier
from craftsman.records import (
    ISOLATION_EVIDENCES,
    FieldError,
    Fields,
    IsolationEvidence,
    LedgerError,
    format_timestamp,
)

GateTier = Literal["auto", "quick", "standard", "deep"]
GatePlatform = Literal["copilot", "claude", "antigravity", "cli"]
GATE_TIERS: tuple[GateTier, ...] = get_args(GateTier)
GATE_PLATFORMS: tuple[GatePlatform, ...] = get_args(GatePlatform)
# `auto` ranks between `quick` and `standard`: it may still resolve to either.
TIER_RANK: dict[GateTier, int] = {"quick": 0, "auto": 1, "standard": 2, "deep": 3}

STATE_FILENAME = "craftsman_state.json"
MAX_CONTINUATIONS = 6
QUICK_TIER_MAX_FILES = 2
MAX_CONTINUATION_REASON_CHARS = 1800
MAX_ERROR_CHARS = 400
CONTINUATION_HEADER = "[CRAFTSMAN GATE"
TAKEOVER_HINT = "start a new gate with `craftsman gate --init --takeover`"
NO_ACTIVE_GATE = "no active gate in this work tree; open one with `craftsman gate --init`"
OVERSIZED_NOTICE = (
    "Craftsman gate: the change exceeds the diff-size budget. The decision belongs to the user; "
    "`craftsman review report` lists the top files by growth and a split suggestion."
)
ROUND_CAP_NOTICE = (
    "Craftsman gate: the review reached its round cap. The remaining issues need your decision; "
    "`craftsman review report` lists them. Start `/craftsman` again to restart."
)

P = TypeVar("P")
T = TypeVar("T")


class GateStateError(RuntimeError):
    """The gate state cannot be read or written, or it is not a valid gate state."""


class ForeignGateError(GateStateError):
    """The active gate belongs to another agent session than the caller's."""


class Stage(str, Enum):
    IMPLEMENT = "implement"
    NEEDS_HUMAN = "needs_human"
    COMPLETED = "completed"
    COMPLETE_UNISOLATED = "complete-unisolated"
    ABORTED = "aborted_max_continuations"


@dataclass(frozen=True)
class GateOwner:
    """The agent platform and session a gate belongs to; a None session is not known yet."""

    platform: GatePlatform
    session_id: str | None = None

    def __post_init__(self) -> None:
        if self.platform not in GATE_PLATFORMS:
            raise GateStateError(f"unknown gate owner platform {self.platform!r}")
        if self.session_id is not None and not self.session_id.strip():
            raise GateStateError("a gate owner's session id must not be blank")

    @classmethod
    def from_json(cls, raw: object) -> GateOwner:
        return _parsed(raw, cls.from_fields, "owner")

    @classmethod
    def from_fields(cls, raw: object) -> GateOwner:
        fields = Fields(raw)
        owner = cls(fields.choice("platform", GATE_PLATFORMS), fields.optional_text("session_id"))
        return fields.done(owner)

    def to_json(self) -> dict[str, str | None]:
        return {"platform": self.platform, "session_id": self.session_id}

    def admits(self, caller: GateOwner) -> bool:
        """Whether `caller` acts for the session that owns the gate.

        `cli` stands for a platform that could not be identified. When both sides name a session,
        the session ids decide, and `cli` on either side matches any platform. Otherwise only the
        owner's unknown parts match anything: a caller without a session id needs the owner's
        exact platform, since a command run from a terminal cannot always name its session.
        """
        if self.session_id is not None and caller.session_id is not None:
            platforms_match = self.platform == caller.platform or "cli" in (
                self.platform,
                caller.platform,
            )
            return platforms_match and self.session_id == caller.session_id
        if self.session_id is not None:
            return self.platform == caller.platform
        return self.platform in ("cli", caller.platform)

    def claimed_by(self, caller: GateOwner) -> GateOwner:
        """This owner, completed with what an admitted `caller` knows about the session."""
        return GateOwner(
            platform=caller.platform if self.platform == "cli" else self.platform,
            session_id=self.session_id or caller.session_id,
        )

    def describe(self) -> str:
        return f"{self.platform} (session {self.session_id})" if self.session_id else self.platform


@dataclass(frozen=True)
class GateState:
    """One task's gate in a work tree.

    `isolation_evidence` says where the proof of an isolated reviewer comes from: `hooks` once a
    platform hook served the session, `transcript` once the Antigravity stop hook did, `none`
    while neither did (the review then counts as self-reviewed). `request_step` is the step of
    the user's request in the Antigravity transcript.
    """

    owner: GateOwner
    base_sha: str
    tier: GateTier
    task: str
    started_at: str
    active: bool = True
    stage: Stage = Stage.IMPLEMENT
    awaiting_user: bool = False
    isolation_evidence: IsolationEvidence = "none"
    forced_continuations: int = 0
    best_issue_count: int | None = None
    takeover_from: GateOwner | None = None
    request_step: int | None = None

    def __post_init__(self) -> None:
        if not self.base_sha:
            raise GateStateError("base_sha must not be empty")
        if self.tier not in GATE_TIERS:
            raise GateStateError(f"unknown tier {self.tier!r}")
        if self.isolation_evidence not in ISOLATION_EVIDENCES:
            raise GateStateError(f"unknown isolation evidence {self.isolation_evidence!r}")
        if self.forced_continuations < 0 or (self.best_issue_count or 0) < 0:
            raise GateStateError("counters must not be negative")

    @classmethod
    def open(cls, owner: GateOwner, base_sha: str, tier: GateTier, task: str, **extra: Any) -> GateState:
        """A new active gate starting now."""
        return cls(owner, base_sha, tier, task, format_timestamp(datetime.now(timezone.utc)), **extra)

    @classmethod
    def from_json(cls, raw: object) -> GateState:
        return _parsed(raw, cls._from_fields, "state")

    @classmethod
    def _from_fields(cls, raw: object) -> GateState:
        fields = Fields(raw)
        state = cls(
            owner=fields.record("owner", GateOwner.from_fields),
            base_sha=fields.text("base_sha"),
            tier=fields.choice("tier", GATE_TIERS),
            task=fields.text("task"),
            started_at=fields.text("started_at"),
            active=fields.flag("active"),
            stage=fields.member("stage", Stage),
            awaiting_user=fields.flag("awaiting_user"),
            isolation_evidence=fields.choice("isolation_evidence", ISOLATION_EVIDENCES),
            forced_continuations=fields.integer("forced_continuations"),
            best_issue_count=fields.optional_integer("best_issue_count"),
            takeover_from=fields.optional_record("takeover_from", GateOwner.from_fields),
            request_step=fields.optional_integer("request_step"),
        )
        return fields.done(state)

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["owner"] = self.owner.to_json()
        data["takeover_from"] = None if self.takeover_from is None else self.takeover_from.to_json()
        data["stage"] = self.stage.value
        return data

    def closed(self, report: GateReport, tier: Tier) -> GateState:
        """The state after the ledger passed the gate on `tier`.

        A self-reviewed `deep` task stays active and waits for the user: the deep tier promises
        an independent review that did not happen.
        """
        if report.self_reviewed and tier == "deep":
            return replace(self, awaiting_user=False, stage=Stage.NEEDS_HUMAN)
        unisolated = report.self_reviewed and tier != "quick"
        stage = Stage.COMPLETE_UNISOLATED if unisolated else Stage.COMPLETED
        return replace(self, active=False, awaiting_user=False, stage=stage)


def _parsed(raw: object, parse: Callable[[object], P], where: str) -> P:
    try:
        return parse(raw)
    except FieldError as exc:
        raise GateStateError(f"{where}: {exc}") from None


def raised_tier(current: GateTier, requested: GateTier) -> GateTier:
    """The stricter of two tiers: an active gate's tier can rise, never drop."""
    return requested if TIER_RANK[requested] > TIER_RANK[current] else current


# --- persistence ----------------------------------------------------------------------------


def state_path_for(cwd: str | Path) -> Path:
    """The gate state file of the work tree containing `cwd`, next to its evidence ledger."""
    return git_dir(cwd) / "craftsman" / STATE_FILENAME


def load_state(path: Path) -> GateState | None:
    """The gate state stored at `path`, or None when no gate was ever opened there."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        raise GateStateError(f"cannot read {path}: {exc}") from exc
    try:
        return GateState.from_json(json.loads(text))
    except (json.JSONDecodeError, GateStateError) as exc:
        raise GateStateError(f"{path} is not a valid gate state ({exc}); {TAKEOVER_HINT}") from exc


def save_state(path: Path, state: GateState) -> None:
    try:
        write_text_atomic(path, json.dumps(state.to_json(), indent=2, ensure_ascii=False) + "\n")
    except OSError as exc:
        raise GateStateError(f"cannot write {path}: {exc}") from exc


@contextmanager
def state_lock(path: Path) -> Iterator[None]:
    """Hold the gate state lock. A holder may take the ledger lock inside it, never the reverse."""
    with locked(path):
        yield


def update_state(path: Path, change: Callable[[GateState | None], tuple[GateState | None, T]]) -> T:
    """Apply `change` to the stored state under the lock; save the new state when it differs."""
    with state_lock(path):
        before = load_state(path)
        after, result = change(before)
        if after is not None and after != before:
            save_state(path, after)
        return result


def require_active(state: GateState | None) -> GateState:
    if state is None or not state.active:
        raise GateStateError(NO_ACTIVE_GATE)
    return state


def require_owned(state: GateState | None, caller: GateOwner, root: Path, action: str) -> GateState:
    """The active gate of the work tree `root`, when `caller`'s session owns it."""
    active = require_active(state)
    if not active.owner.admits(caller):
        raise ForeignGateError(foreign_gate_refusal(root, active, action))
    return active


def foreign_gate_refusal(root: Path, active: GateState, action: str) -> str:
    """Why `action` is refused on another session's gate, and what the caller can do instead."""
    return (
        f"Refusing to {action}: the active gate in {root} is already owned by "
        f"{active.owner.describe()} (since {active.started_at}).\n"
        "If you are that session, repeat the command with your session ID from the prompt "
        "briefing: --session-id <session-id>\n"
        "To work in parallel without interfering with that session, create a separate worktree:\n"
        "  git worktree add ../<branch-dir> -b <branch>\n"
        "Or, to explicitly take over the existing gate, pass --takeover:\n"
        "  craftsman gate --init --takeover ..."
    )


# --- evaluation -----------------------------------------------------------------------------


def task_tier(configured: GateTier, snap: DiffSnapshot) -> Tier:
    """The concrete tier of a task; `auto` is `quick` while the diff stays within the quick limits.

    A binary or oversized file exceeds the limits: its size in lines is unknown.
    """
    if configured != "auto":
        return configured
    small = (
        len(snap.files) <= QUICK_TIER_MAX_FILES
        and snap.added + snap.deleted <= QUICK_TIER_MAX_CHANGED_LINES
        and not snap.skipped
    )
    return "quick" if small else "standard"


def current_tier(state: GateState, root: Path) -> Tier:
    """The concrete tier of `state`'s task for the work tree `root` as it is now."""
    if state.tier != "auto":
        return state.tier
    return task_tier(state.tier, snapshot(root, state.base_sha))


def evaluate_task(state: GateState, root: Path) -> Evaluation:
    """Evaluate the evidence ledger of the work tree `root` for the task of `state`.

    Raises `DiffScanError` or `LedgerError` when the work tree or the ledger cannot be read.
    """
    snap = snapshot(root, state.base_sha)
    return evaluate_evidence(root, snap, task_tier(state.tier, snap), state.isolation_evidence)


class Outcome(Enum):
    PASSED = "passed"
    OVERSIZED = "oversized"
    ROUND_CAP = "round-cap"
    UNSATISFIED = "unsatisfied"


def judge(evaluation: Evaluation) -> Outcome:
    """Whether an evaluated task passed, needs the user's decision, or needs more work."""
    if has_unresolved_diff_size(evaluation.ledger):
        return Outcome.OVERSIZED
    if any(issue.code == "round-cap" for issue in evaluation.report.issues):
        return Outcome.ROUND_CAP
    return Outcome.PASSED if evaluation.report.passed else Outcome.UNSATISFIED


HUMAN_NOTICES: dict[Outcome, str] = {
    Outcome.OVERSIZED: OVERSIZED_NOTICE,
    Outcome.ROUND_CAP: ROUND_CAP_NOTICE,
}


@dataclass(frozen=True)
class StopDecision:
    """What a stop hook does: block with `block_reason`, or let the agent stop with an optional
    `notice` for the user. `state` is the gate state afterwards."""

    state: GateState
    block_reason: str | None = None
    notice: str | None = None


def decide_stop(state: GateState, root: Path) -> StopDecision:
    """Whether the agent that owns `state` may stop now."""
    if not state.active or state.awaiting_user:
        return StopDecision(state)
    try:
        evaluation = evaluate_task(state, root)
    except (DiffScanError, LedgerError) as exc:
        return _continue(state, None, str(exc))
    outcome = judge(evaluation)
    if outcome in HUMAN_NOTICES:
        return StopDecision(replace(state, stage=Stage.NEEDS_HUMAN), notice=HUMAN_NOTICES[outcome])
    if outcome is Outcome.PASSED:
        return StopDecision(state.closed(evaluation.report, evaluation.tier))
    return _continue(state, evaluation.report, None)


def _continue(state: GateState, report: GateReport | None, error: str | None) -> StopDecision:
    """Block the stop, unless the streak of continuations without progress reached the cap.

    Progress means fewer open issues than ever before in this streak; an issue count that merely
    oscillates must not keep the agent looping. An unreadable ledger counts as one issue.
    """
    issue_count = len(report.issues) if report is not None else 1
    best = state.best_issue_count
    forced = 0 if best is not None and issue_count < best else state.forced_continuations
    best = issue_count if best is None else min(best, issue_count)
    if forced >= MAX_CONTINUATIONS:
        return StopDecision(
            replace(state, active=False, stage=Stage.ABORTED, best_issue_count=best),
            notice=(
                f"Craftsman gate not satisfied: the agent stopped after {forced} forced continuations "
                "without passing `craftsman review check`. Run it to see what is still missing."
            ),
        )
    header = f"{CONTINUATION_HEADER} {forced + 1}/{MAX_CONTINUATIONS}]"
    return StopDecision(
        replace(state, forced_continuations=forced + 1, best_issue_count=best),
        block_reason=continuation_reason(header, report, error),
    )


def continuation_reason(header: str, report: GateReport | None, evidence_error: str | None) -> str:
    """Tell the agent exactly what is missing, within a size the platforms display in full."""
    lines = [
        f"{header} Task completion is blocked until the evidence ledger proves the change is "
        "reviewed and verified."
    ]
    if evidence_error is not None:
        lines.append(f"Cannot evaluate the evidence ledger: {evidence_error[:MAX_ERROR_CHARS]}")
    closing = "Close the gate with `craftsman gate --complete` once `craftsman review check` passes."
    if report is not None and report.issues:
        lines.append("Next actions:")
        more = "… run `craftsman review check` for the remaining actions."
        budget = MAX_CONTINUATION_REASON_CHARS - sum(len(line) + 1 for line in (*lines, closing, more))
        for action in render_next_actions(report.issues):
            if len(action) + 1 > budget:
                lines.append(more)
                break
            lines.append(action)
            budget -= len(action) + 1
    lines.append(closing)
    return "\n".join(lines)


def is_continuation_prompt(prompt: str) -> bool:
    """True for a stop hook's block reason, which some platforms send back as a user prompt."""
    return prompt.lstrip().startswith(CONTINUATION_HEADER)


def archive_ledger_for_new_base(root: Path, base_sha: str) -> None:
    """A task with a different base commit must not inherit the previous review."""
    for path in archive_ledger_if_rebased(root, base_sha):
        sys.stderr.write(f"[craftsman gate] Archived previous review evidence to {path}\n")


# --- command line ---------------------------------------------------------------------------


def detect_cli_platform(env: Mapping[str, str]) -> GatePlatform:
    """The agent platform a `craftsman gate` command runs under, from its environment."""
    if env.get("CLAUDECODE") or env.get("CLAUDE_CODE_ENTRYPOINT"):
        return "claude"
    if env.get("ANTIGRAVITY_AGENT") or env.get("AGY_MANAGED_ENV"):
        return "antigravity"
    if (
        env.get("COPILOT_CLI")
        or env.get("GITHUB_COPILOT_CLI")
        or env.get("VSCODE_PID")
        or env.get("VSCODE_IPC_HOOK")
        or env.get("TERM_PROGRAM", "").strip().lower() == "vscode"
    ):
        return "copilot"
    return "cli"


def cli_caller(session_id: str | None, env: Mapping[str, str]) -> GateOwner:
    """The session a `craftsman gate` command acts for: `--session-id`, else the one Claude Code
    exports to the commands it runs."""
    platform = detect_cli_platform(env)
    exported = env.get("CLAUDE_CODE_SESSION_ID", "") if platform == "claude" else ""
    return GateOwner(platform, (session_id or "").strip() or exported.strip() or None)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="craftsman gate",
        description=(
            "Open, update and close the Craftsman quality gate of the current git work tree. "
            "Without an action, --tier and --task update the active gate."
        ),
    )
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--init", action="store_true", help="Open a gate for a new task.")
    action.add_argument(
        "--complete", action="store_true", help="Close the gate; refused until the ledger passes."
    )
    action.add_argument(
        "--await-user", action="store_true", help="Let the agent stop while it waits for the user."
    )
    action.add_argument(
        "--resume", action="store_true", help="Enforce the gate again after --await-user."
    )
    parser.add_argument("--cwd", help="A directory inside the work tree (default: the current one).")
    parser.add_argument(
        "--tier",
        choices=GATE_TIERS,
        help=f"auto (quick while the diff stays within {QUICK_TIER_MAX_FILES} files and "
        f"{QUICK_TIER_MAX_CHANGED_LINES} lines, else standard), quick, standard or deep. An active "
        "gate's tier can only rise.",
    )
    parser.add_argument("--task", help="One-line description of the task (shown in review packets).")
    parser.add_argument("--base-sha", help="With --init: the base commit (default HEAD).")
    parser.add_argument(
        "--session-id",
        help="The calling agent session's id (default: the one the platform exports, if any).",
    )
    parser.add_argument(
        "--takeover",
        action="store_true",
        help="With --init: replace another session's active gate or an invalid state file.",
    )
    return parser


def _check_combinations(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    init_only = [
        flag
        for flag, value in (
            ("--base-sha", args.base_sha),
            ("--takeover", args.takeover),
        )
        if value
    ]
    if init_only and not args.init:
        parser.error(f"{', '.join(init_only)}: only valid with --init")
    if (args.complete or args.await_user or args.resume) and (args.tier or args.task):
        parser.error("--tier and --task apply with --init or on their own")
    if not (args.init or args.complete or args.await_user or args.resume or args.tier or args.task):
        parser.error("choose --init, --complete, --await-user or --resume, or update --tier/--task")


def run_cli(argv: list[str], env: Mapping[str, str]) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    _check_combinations(parser, args)
    caller = cli_caller(args.session_id, env)
    try:
        root = worktree_root(args.cwd or os.getcwd())
        path = state_path_for(root)
        if args.init:
            _init(root, path, args, caller)
            return 0
        if args.complete:
            return _complete(root, path, caller)
        update_state(
            path,
            lambda current: (_updated(require_owned(current, caller, root, "update"), args), None),
        )
        return 0
    except (DiffScanError, LedgerError, GateStateError) as exc:
        sys.stderr.write(f"craftsman gate: error: {exc}\n")
        return 1


def _init(root: Path, path: Path, args: argparse.Namespace, caller: GateOwner) -> None:
    """Open the gate; the previous review is archived first, so a failure leaves no new gate."""
    base_sha = resolve_base_sha(root, args.base_sha)
    with state_lock(path):
        previous = _replaceable_state(path, takeover=args.takeover)
        owner = caller
        tier: GateTier = args.tier or "auto"
        isolation: IsolationEvidence = "none"
        takeover_from: GateOwner | None = None
        if previous is not None and previous.active and previous.owner.admits(caller):
            # Re-opening its own gate keeps what the hooks proved about the session, and its tier
            # can only rise, so a re-init cannot shed the isolated reviewer requirement.
            owner, isolation = previous.owner.claimed_by(caller), previous.isolation_evidence
            tier = raised_tier(previous.tier, args.tier or previous.tier)
        elif previous is not None and previous.active:
            if not args.takeover:
                raise ForeignGateError(foreign_gate_refusal(root, previous, "initialize"))
            takeover_from = previous.owner
        state = GateState.open(
            owner,
            base_sha,
            tier,
            args.task or "",
            isolation_evidence=isolation,
            takeover_from=takeover_from,
        )
        archive_ledger_for_new_base(root, base_sha)
        save_state(path, state)


def _replaceable_state(path: Path, *, takeover: bool) -> GateState | None:
    """The stored state; with `--takeover`, an invalid state file is replaced instead of refused."""
    try:
        return load_state(path)
    except GateStateError as exc:
        if not takeover:
            raise
        sys.stderr.write(f"[craftsman gate] Replacing an invalid gate state: {exc}\n")
        return None


def _updated(state: GateState, args: argparse.Namespace) -> GateState:
    if args.await_user or args.resume:
        return replace(state, awaiting_user=bool(args.await_user))
    return replace(
        state,
        tier=raised_tier(state.tier, args.tier) if args.tier else state.tier,
        task=args.task or state.task,
    )


def _close_gate(
    current: GateState | None, *, root: Path, caller: GateOwner
) -> tuple[GateState, tuple[int, str]]:
    state = require_owned(current, caller, root, "complete")
    evaluation = evaluate_task(state, root)
    outcome = judge(evaluation)
    if outcome in HUMAN_NOTICES:
        failure = f"[CRAFTSMAN GATE] Cannot complete. {HUMAN_NOTICES[outcome]}\n"
        return replace(state, stage=Stage.NEEDS_HUMAN), (1, failure)
    if outcome is Outcome.UNSATISFIED:
        return state, (1, f"[CRAFTSMAN GATE] Cannot complete:\n{render_report(evaluation.report)}\n")
    closed = state.closed(evaluation.report, evaluation.tier)
    return closed, (0, closed_gate_line(evaluation, closed))


def _complete(root: Path, path: Path, caller: GateOwner) -> int:
    code, message = update_state(
        path, lambda current: _close_gate(current, root=root, caller=caller)
    )
    (sys.stdout if code == 0 else sys.stderr).write(message)
    if code == 0:
        prune_inbox_files(path.parent)
    return code


def closed_gate_line(evaluation: Evaluation, state: GateState) -> str:
    if state.active:
        return (
            f"Craftsman gate passed without an isolated review on the {evaluation.tier} tier; it "
            "stays open for the user's decision. Run `craftsman review report` for them.\n"
        )
    findings = evaluation.ledger.findings
    fixed = sum(finding.resolution == "fixed" for finding in findings)
    accepted = sum(finding.resolution == "accepted" for finding in findings)
    return (
        f"Craftsman gate closed: round {len(evaluation.ledger.rounds)}, tier {evaluation.tier}, "
        f"{fixed} findings fixed, {accepted} accepted.\n"
    )


def main(argv: list[str] | None = None) -> int:
    return run_cli(list(sys.argv[1:] if argv is None else argv), os.environ)


if __name__ == "__main__":
    raise SystemExit(main())
