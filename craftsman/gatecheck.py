"""Gate evaluation: decide whether the ledger's evidence covers the current diff, and report why.

`evaluate` is a pure function of a ledger, a diff snapshot and a policy; `render_report` turns its
`GateReport` into the Markdown block shown to agents and users.
"""

from __future__ import annotations

from collections.abc import Sequence, Set
from dataclasses import dataclass
from typing import Literal

from craftsman.diffscan import DiffSnapshot
from craftsman.ledger import counting_sweeps, open_units, uncovered_by_lens
from craftsman.lenses import REVIEW_ONLY_QUICK_MAX_LINES, TIERS, Tier
from craftsman.records import (
    DESIGN_LENS,
    MAX_HUNKS_PER_COVERAGE,
    REVIEWER_AGENT,
    SWEEP_LENS,
    IsolationEvidence,
    Ledger,
    LedgerError,
    QualitySignal,
    Round,
    Run,
    RunKind,
    SignalAck,
    agent_role,
    parse_timestamp,
)
from craftsman.triviality import command_without_env, is_trivial_run

IssueCode = Literal[
    "no-round",
    "stale-round",
    "tier-too-low",
    "uncovered",
    "sweep-too-early",
    "unresolved-signal",
    "no-verdict",
    "unresolved",
    "no-passing-run",
    "no-e2e-run",
    "failing-run",
    "no-isolation",
    "round-cap",
]

SELF_REVIEWED_BANNER = (
    "SELF-REVIEWED: no evidence that anyone other than the author reviewed this change"
)
QUICK_TIER_MAX_CHANGED_LINES = 40
MAX_NEXT_ACTIONS = 12
SUMMARY_RECENT_RUNS = 3
# Review rounds of different diffs before the agent must stop iterating and ask the user.
MAX_ROUNDS = 5
# Process anomalies: patterns typical of a review that was recorded rather than performed.
ANOMALY_MIN_FINDINGS = 5
ANOMALY_FAST_COVERAGE_HUNKS = 20
ANOMALY_FAST_COVERAGE_SECONDS = 120
PASSING_KINDS: tuple[RunKind, ...] = ("test", "e2e")
_VERIFYING_KINDS: tuple[RunKind, ...] = ("test", "e2e", "lint", "build")


# --- gate evaluation ------------------------------------------------------------------------


@dataclass(frozen=True)
class GateIssue:
    code: IssueCode
    message: str


@dataclass(frozen=True)
class GateReport:
    issues: tuple[GateIssue, ...]
    warnings: tuple[str, ...]
    summary: str
    review_only: bool = False
    self_reviewed: bool = False
    partial: bool = False

    @property
    def passed(self) -> bool:
        return not self.issues

    @property
    def subject(self) -> str:
        """What was judged: the review alone, or the whole gate."""
        return "review" if self.review_only else "gate"

    @property
    def status_label(self) -> str:
        """PASSED / BLOCKED for the gate; COMPLETE / INCOMPLETE / PARTIAL for a review-only check."""
        if self.partial:
            if self.issues:
                return f"PARTIAL (review-only, {len(self.issues)} open items)"
            return "COMPLETE (review-only)"
        if self.passed:
            return "COMPLETE (review-only)" if self.review_only else "PASSED"
        blocked = "INCOMPLETE" if self.review_only else "BLOCKED"
        return f"{blocked} ({len(self.issues)} open items)"

    @property
    def issue_codes(self) -> str:
        """The distinct issue codes, in order of first appearance."""
        return ", ".join(dict.fromkeys(issue.code for issue in self.issues))


@dataclass(frozen=True)
class GatePolicy:
    """How strictly the gate judges a ledger.

    `isolation_evidence` names where proof of an isolated reviewer can come from; with `none`, a
    `standard` or `deep` review passes as self-reviewed.
    """

    tier: Tier
    isolation_evidence: IsolationEvidence = "none"

    @property
    def require_isolation(self) -> bool:
        return self.isolation_evidence != "none" and self.tier != "quick"


def evaluate(
    ledger: Ledger, snap: DiffSnapshot, policy: GatePolicy, *, review_only: bool = False
) -> GateReport:
    """Decide whether the evidence in `ledger` covers the current diff `snap`."""
    if snap.is_empty:
        summary = f"Nothing to review: the work tree matches base {snap.base_sha[:12]}."
        issues = () if review_only else tuple(_unresolved_issues(ledger, snap))
        return GateReport(issues, (), summary, review_only=review_only)
    latest = ledger.rounds[-1] if ledger.rounds else None
    fix_and_test = (
        ()
        if review_only
        else (
            *_unresolved_issues(ledger, snap),
            *_passing_run_issues(ledger, snap, policy),
            *_failing_run_issues(ledger),
        )
    )
    issues = (
        *_round_issues(ledger, snap),
        *(_tier_issues(latest, policy) if latest else ()),
        *(_coverage_issues(ledger, latest) if latest else ()),
        *(_signal_issues(ledger, latest) if latest else ()),
        *_verdict_issues(ledger),
        *fix_and_test,
        *(_isolation_issues(ledger, latest, policy) if latest else ()),
        *_round_cap_issues(ledger),
    )
    self_reviewed = bool(
        latest is not None
        and policy.tier != "quick"
        and not policy.require_isolation
        and not _has_isolated_reviewer_stop(ledger, latest)
    )
    return GateReport(
        issues,
        _warnings(ledger, snap, policy, review_only=review_only),
        _summary(ledger, snap),
        review_only=review_only,
        self_reviewed=self_reviewed,
    )


def acks_in_force(ledger: Ledger) -> dict[str, SignalAck]:
    """The latest acknowledgement of each signal of the latest round, by its id.

    Signal ids are renumbered every round, so an ack from an earlier round carries over only to a
    signal that flags the same thing (kind, file and detail), and only when that key names exactly
    one signal in both rounds: two equal-looking signals must each be acknowledged.
    """
    if not ledger.rounds:
        return {}
    latest = ledger.rounds[-1]
    unique_in_latest = _unique_by_key(latest)
    acks: dict[str, SignalAck] = {}
    for ack in ledger.signal_acks:
        if ack.round == latest.number:
            acks[ack.signal_id] = ack
            continue
        carried = unique_in_latest.get(
            _signal_key(ledger.rounds[ack.round - 1].signal(ack.signal_id))
        )
        if carried is not None and _signal_key(carried) in _unique_by_key(ledger.rounds[ack.round - 1]):
            acks[carried.id] = ack
    return {sig.id: acks[sig.id] for sig in latest.signals if sig.id in acks}


def _unique_by_key(round_: Round) -> dict[tuple[str, str, str], QualitySignal]:
    """The signals of `round_` whose key no other signal of the round shares."""
    by_key: dict[tuple[str, str, str], list[QualitySignal]] = {}
    for sig in round_.signals:
        by_key.setdefault(_signal_key(sig), []).append(sig)
    return {key: sigs[0] for key, sigs in by_key.items() if len(sigs) == 1}


def _signal_key(sig: QualitySignal) -> tuple[str, str, str]:
    return (sig.kind.value, sig.file, sig.detail)


def _is_signal_resolved(ledger: Ledger, latest: Round, sig: QualitySignal, acked: Set[str]) -> bool:
    if sig.needs_user:
        return False
    if sig.id in acked:
        return True
    # A dismissed finding argues the signal away instead of resolving it; that needs an ack.
    return any(
        finding.round == latest.number and finding.signal_id == sig.id and finding.verdict != "dismissed"
        for finding in ledger.findings
    )


def has_unresolved_diff_size(ledger: Ledger) -> bool:
    """True when the latest round has a signal only the user can resolve: the change is too big."""
    return bool(ledger.rounds) and any(sig.needs_user for sig in ledger.rounds[-1].signals)


def _signal_issues(ledger: Ledger, latest: Round) -> list[GateIssue]:
    acked = acks_in_force(ledger).keys()
    issues: list[GateIssue] = []
    for sig in latest.signals:
        if _is_signal_resolved(ledger, latest, sig, acked):
            continue
        action = (
            "shrink the change below the budget or hand the review to the user"
            if sig.needs_user
            else f"record a finding with `craftsman review add --signal {sig.id} ...` or acknowledge it "
            f'with `craftsman review ack {sig.id} --reason "<concrete reason, >= 40 chars>"`'
        )
        issues.append(
            GateIssue(
                "unresolved-signal",
                f"Quality signal {sig.id} ({sig.kind.value} at {sig.file}:{sig.line} — {sig.detail}) "
                f"is unresolved; {action}.",
            )
        )
    return issues


def require_current_round(ledger: Ledger, snap: DiffSnapshot) -> Round:
    """The latest round, provided it was prepared for exactly the diff `snap`."""
    issues = _round_issues(ledger, snap)
    if issues:
        raise LedgerError(issues[0].message)
    return ledger.rounds[-1]


def _round_issues(ledger: Ledger, snap: DiffSnapshot) -> list[GateIssue]:
    if not ledger.rounds:
        return [
            GateIssue(
                "no-round",
                "No review round yet; run `craftsman review prepare` to snapshot the diff "
                "and select lenses.",
            )
        ]
    base = ledger.rounds[0].base_sha
    latest = ledger.rounds[-1]
    if snap.base_sha != base:
        message = (
            f"The base commit changed from {base[:12]} to {snap.base_sha[:12]} since round 1; "
            "run `craftsman review reset` and start a new review."
        )
        return [GateIssue("stale-round", message)]
    if latest.fingerprint != snap.fingerprint:
        message = (
            f"Code changed since round {latest.number} was prepared; "
            "run `craftsman review prepare` to review the delta."
        )
        return [GateIssue("stale-round", message)]
    return []


def _tier_issues(latest: Round, policy: GatePolicy) -> list[GateIssue]:
    if TIERS.index(latest.tier) >= TIERS.index(policy.tier):
        return []
    return [
        GateIssue(
            "tier-too-low",
            f"Round {latest.number} was prepared for the {latest.tier} tier but this task needs "
            f"{policy.tier}; run `craftsman review prepare --tier {policy.tier}`.",
        )
    ]


def _coverage_issues(ledger: Ledger, latest: Round) -> list[GateIssue]:
    missing = uncovered_by_lens(ledger, latest)
    sweep_missing = missing.pop(SWEEP_LENS, [])
    design_missing = missing.pop(DESIGN_LENS, [])
    use_files = latest.tier == "quick"
    path_by_hunk = {hunk.id: hunk.path for hunk in latest.hunks}
    grouped: dict[tuple[str, ...], list[str]] = {}
    for lens, ids in missing.items():
        if not ids:
            continue
        targets = (
            tuple(dict.fromkeys(path_by_hunk[hid] for hid in ids)) if use_files else tuple(ids)
        )
        grouped.setdefault(targets, []).append(lens)
    issues = [
        _uncovered_issue(lenses, targets, latest, use_files=use_files)
        for targets, lenses in grouped.items()
    ]
    if design_missing:
        issues.append(_uncovered_issue((DESIGN_LENS,), design_missing, latest, use_files=True))
    premature = _premature_sweep_ids(ledger, latest, sweep_missing)
    if premature:
        batch = ",".join(premature[:MAX_HUNKS_PER_COVERAGE])
        issues.append(
            GateIssue(
                "sweep-too-early",
                f"The final sweep of {', '.join(premature)} was recorded before the other lenses "
                f"of round {latest.number} were complete, so it does not count; once every other "
                f"lens is covered, repeat it: "
                f'`craftsman review cover --lens {SWEEP_LENS} --hunks {batch} --note "..."`.',
            )
        )
    remaining = [hunk_id for hunk_id in sweep_missing if hunk_id not in premature]
    if remaining and not any(missing.values()) and not design_missing:
        issues.append(_uncovered_issue((SWEEP_LENS,), remaining, latest, use_files=False))
    return issues


def _uncovered_issue(
    lenses: Sequence[str], missing: Sequence[str], round_: Round, *, use_files: bool
) -> GateIssue:
    batch = ",".join(missing[:MAX_HUNKS_PER_COVERAGE])
    extra = len(missing) - MAX_HUNKS_PER_COVERAGE
    more = f" (+{extra} more)" if extra > 0 else ""
    unit = "file(s)" if use_files else "hunk(s)"
    flag = "--files" if use_files else "--hunks"
    lens_arg = ",".join(lenses)
    head = (
        f"Lens `{lenses[0]}` has"
        if len(lenses) == 1
        else f"Lenses {', '.join(f'`{lens}`' for lens in lenses)} have"
    )
    return GateIssue(
        "uncovered",
        f"{head} {len(missing)} uncovered {unit} in round {round_.number}: "
        f"{batch}{more}; review them and run "
        f'`craftsman review cover --lens {lens_arg} {flag} {batch} --note "..."`.',
    )


def _premature_sweep_ids(ledger: Ledger, round_: Round, sweep_missing: Sequence[str]) -> list[str]:
    """Still-uncovered hunk ids whose only sweep coverage in `round_` came too early."""
    counting = counting_sweeps(ledger)
    digests = {
        digest
        for record in ledger.coverage
        if record.round == round_.number and record.lens == SWEEP_LENS and record not in counting
        for digest in record.digests
    }
    return [hunk.id for hunk in round_.hunks if hunk.id in sweep_missing and hunk.digest in digests]


def _verdict_issues(ledger: Ledger) -> list[GateIssue]:
    return [
        GateIssue(
            "no-verdict",
            f"Finding {finding.id} ({finding.severity}: {finding.title}) has no verdict; verify it "
            f"independently and run `craftsman review verdict {finding.id} "
            '--as proven|probable|question|dismissed --evidence "..."`.',
        )
        for finding in ledger.findings
        if finding.verdict is None
    ]


def _unresolved_issues(ledger: Ledger, snap: DiffSnapshot) -> list[GateIssue]:
    issues: list[GateIssue] = []
    for finding in ledger.findings:
        label = f"Finding {finding.id} ({finding.severity}: {finding.title})"
        if finding.verdict in ("proven", "probable") and finding.resolution is None:
            alternative = "" if finding.severity == "blocker" else ' (or `--as accepted --note "why"`)'
            issues.append(
                GateIssue(
                    "unresolved",
                    f"{label} is {finding.verdict} but unresolved; fix the code, then run "
                    f'`craftsman review resolve {finding.id} --as fixed --note "..."`{alternative}.',
                )
            )
        elif finding.resolution == "fixed" and finding.fingerprint == snap.fingerprint:
            issues.append(
                GateIssue(
                    "unresolved",
                    f"{label} was resolved as fixed, but the code is back to the code it was "
                    "recorded at; the fix was reverted, apply it again.",
                )
            )
    return issues


def _counting_runs(ledger: Ledger, kinds: Sequence[RunKind]) -> list[Run]:
    return [run for run in ledger.runs if run.kind in kinds and not is_trivial_run(run.argv)]


def passing_runs(ledger: Ledger, fingerprint: str) -> list[Run]:
    """The non-trivial test and e2e runs that passed at `fingerprint`: what the gate counts."""
    return [
        run
        for run in _counting_runs(ledger, PASSING_KINDS)
        if run.counts_as_passing and run.fingerprint == fingerprint
    ]


def _passing_run_issues(ledger: Ledger, snap: DiffSnapshot, policy: GatePolicy) -> list[GateIssue]:
    passing = passing_runs(ledger, snap.fingerprint)
    issues: list[GateIssue] = []
    if not passing:
        issues.append(
            GateIssue(
                "no-passing-run",
                "No passing test or e2e run at the current code; "
                "run `craftsman run --kind test -- <your test command>`.",
            )
        )
    if policy.tier == "deep" and not any(run.kind == "e2e" for run in passing):
        issues.append(
            GateIssue(
                "no-e2e-run",
                "The deep tier needs a passing end-to-end run at the current code; run "
                "`craftsman run --kind e2e -- <command that exercises the real flow>`.",
            )
        )
    return issues


def _unwaived_failing_runs(ledger: Ledger) -> list[Run]:
    """Latest non-trivial verifying runs per (cwd, command) that failed without a waiver."""
    latest = {
        (run.cwd, run.argv): run
        for run in _counting_runs(ledger, _VERIFYING_KINDS)
        if run.exit_code != 0 or run.counts_as_passing
    }
    return [run for run in latest.values() if run.exit_code != 0 and run.waiver is None]


def _failing_run_issues(ledger: Ledger) -> list[GateIssue]:
    """The latest run of each command (per working directory) must not fail unless it was waived.

    A passing run that changed the work tree does not clear an earlier failure.
    """
    return [
        GateIssue(
            "failing-run",
            f"`{run.command}` ({run.kind}, {run.id}) last exited {run.exit_code}; fix the failure "
            f"and re-run `craftsman run --kind {run.kind} -- {run.command}`. If the command itself "
            f"is obsolete (a typo, a renamed test), retire it with "
            f'`craftsman review waive-run {run.id} --reason "..."`.',
        )
        for run in _unwaived_failing_runs(ledger)
    ]


def _has_isolated_reviewer_stop(ledger: Ledger, latest: Round) -> bool:
    if all(hunk.carried for hunk in latest.hunks):
        return True
    prepared_at = parse_timestamp(latest.prepared_at)
    return any(
        event.event == "stop"
        and agent_role(event.agent) == "reviewer"
        and parse_timestamp(event.at) >= prepared_at
        for event in ledger.agents
    )


def _isolation_issues(ledger: Ledger, latest: Round, policy: GatePolicy) -> list[GateIssue]:
    if not policy.require_isolation:
        return []
    if _has_isolated_reviewer_stop(ledger, latest):
        return []
    return [
        GateIssue(
            "no-isolation",
            f"No independent review finished since round {latest.number} was prepared; spawn the "
            f"{REVIEWER_AGENT} subagent for round {latest.number}.",
        )
    ]


def _round_cap_issues(ledger: Ledger) -> list[GateIssue]:
    # Re-preparing the same diff for a stricter tier is not another iteration.
    diffs = len({round_.fingerprint for round_ in ledger.rounds})
    if diffs <= MAX_ROUNDS:
        return []
    return [
        GateIssue(
            "round-cap",
            f"{diffs} review rounds of different diffs exceed the cap of {MAX_ROUNDS}; stop "
            "iterating and report the remaining issues to the user.",
        )
    ]


def _process_anomaly_warnings(ledger: Ledger) -> list[str]:
    return [
        *_all_confirmed_warnings(ledger),
        *_fast_coverage_warnings(ledger),
        *_retried_with_other_env_warnings(ledger),
    ]


def _all_confirmed_warnings(ledger: Ledger) -> list[str]:
    findings = ledger.findings
    if len(findings) < ANOMALY_MIN_FINDINGS:
        return []
    if any(finding.verdict is None or finding.verdict == "dismissed" for finding in findings):
        return []
    return [
        f"Process anomaly: {len(findings)} findings and 0 dismissed "
        "(all findings confirmed — typical of self-review)."
    ]


def _fast_coverage_warnings(ledger: Ledger) -> list[str]:
    if not ledger.rounds:
        return []
    latest = ledger.rounds[-1]
    warnings: list[str] = []
    for lens in latest.lenses:
        if lens == DESIGN_LENS:  # covers whole files, which takes fewer, larger steps
            continue
        records = [r for r in ledger.coverage if r.round == latest.number and r.lens == lens]
        covered = sum(len(record.digests) for record in records)
        if covered <= ANOMALY_FAST_COVERAGE_HUNKS:
            continue
        times = [parse_timestamp(record.recorded_at) for record in records]
        if (max(times) - min(times)).total_seconds() < ANOMALY_FAST_COVERAGE_SECONDS:
            warnings.append(
                f"Process anomaly: {covered} hunks covered in lens `{lens}` in under "
                f"{ANOMALY_FAST_COVERAGE_SECONDS // 60} minutes."
            )
    return warnings


def _retried_with_other_env_warnings(ledger: Ledger) -> list[str]:
    """A failing test rerun with the same code under another environment, e.g. `env CI= pytest`."""
    unretried: dict[tuple[str, tuple[str, ...]], list[Run]] = {}
    warnings: list[str] = []
    for run in ledger.runs:
        if run.kind not in PASSING_KINDS:
            continue
        failures = unretried.setdefault((run.fingerprint, tuple(command_without_env(run.argv))), [])
        retried = [failed for failed in failures if failed.argv != run.argv]
        warnings.extend(
            f"Process anomaly: {failed.kind} run {failed.id} failed and was repeated as "
            f"{run.id} with a different environment without a code change."
            for failed in retried
        )
        failures[:] = [failed for failed in failures if failed not in retried]
        if run.exit_code != 0:
            failures.append(run)
    return warnings


def _review_only_warnings(ledger: Ledger) -> list[str]:
    warnings: list[str] = []
    for_author = [
        finding.id
        for finding in ledger.findings
        if finding.verdict in ("proven", "probable") and finding.resolution is None
    ]
    if for_author:
        noun = "finding" if len(for_author) == 1 else "findings"
        warnings.append(f"{len(for_author)} {noun} for the author: {', '.join(for_author)}.")
    for run in _unwaived_failing_runs(ledger):
        warnings.append(
            f"Failing run {run.id} (`{run.command}`, {run.kind}) exited {run.exit_code}."
        )
    return warnings


def _warnings(
    ledger: Ledger, snap: DiffSnapshot, policy: GatePolicy, *, review_only: bool = False
) -> tuple[str, ...]:
    warnings = _review_only_warnings(ledger) if review_only else []
    accepted = [finding.id for finding in ledger.findings if finding.resolution == "accepted"]
    if accepted:
        warnings.append(f"Accepted without a fix: {', '.join(accepted)}; tell the user about them.")
    questions = [finding.id for finding in ledger.findings if finding.verdict == "question"]
    if questions:
        warnings.append(
            f"Design questions for the author: {', '.join(questions)}; highlight them in the report."
        )
    waived = [run.id for run in ledger.runs if run.waiver is not None]
    if waived:
        warnings.append(f"Waived failing runs: {', '.join(waived)}; tell the user about them.")
    changed_lines = snap.added + snap.deleted
    quick_max = REVIEW_ONLY_QUICK_MAX_LINES if review_only else QUICK_TIER_MAX_CHANGED_LINES
    if policy.tier == "quick" and changed_lines > quick_max and not (review_only and snap.docs_only):
        warnings.append(
            f"The quick tier reviews {changed_lines} changed lines "
            f"(> {quick_max}); consider the standard tier."
        )
    warnings.extend(
        f"{run.id} (`{run.command}`) is trivial and does not count as verification."
        for run in ledger.runs
        if is_trivial_run(run.argv)
    )
    warnings.extend(_process_anomaly_warnings(ledger))
    return tuple(warnings)


def _summary(ledger: Ledger, snap: DiffSnapshot) -> str:
    if not ledger.rounds:
        round_line = "Round: none prepared yet."
        coverage_line = "Coverage: n/a."
    else:
        latest = ledger.rounds[-1]
        freshness = "current" if latest.fingerprint == snap.fingerprint else "stale"
        round_line = (
            f"Round {latest.number} ({latest.tier}, {len(latest.lenses)} lenses, {freshness}): "
            f"{len(latest.hunks)} hunks in {len(snap.files)} files, +{snap.added}/-{snap.deleted} lines."
        )
        coverage_line = _coverage_summary(ledger, latest)
    return "\n".join((round_line, coverage_line, _findings_summary(ledger), _runs_summary(ledger, snap)))


def _coverage_summary(ledger: Ledger, latest: Round) -> str:
    missing = uncovered_by_lens(ledger, latest)
    total = sum(len(open_units(latest, lens)) for lens in latest.lenses)
    done = total - sum(len(ids) for ids in missing.values())
    carried = len(latest.hunks) - len(latest.open_hunk_ids)
    line = (
        f"Coverage: {done}/{total} lens items (hunks, or files for `{DESIGN_LENS}`); "
        f"{carried} hunk(s) carried from earlier rounds."
    )
    lenses_pending = any(ids for lens, ids in missing.items() if lens != SWEEP_LENS)
    if SWEEP_LENS in latest.lenses and lenses_pending:
        line += " Sweep pending (runs after every other lens)."
    return line


def _findings_summary(ledger: Ledger) -> str:
    if not ledger.findings:
        return "Findings: none."
    findings = ledger.findings
    awaiting = sum(finding.verdict is None for finding in findings)
    dismissed = sum(finding.verdict == "dismissed" for finding in findings)
    questions = [finding.id for finding in findings if finding.verdict == "question"]
    unresolved = sum(
        finding.verdict in ("proven", "probable") and finding.resolution is None for finding in findings
    )
    fixed = sum(finding.resolution == "fixed" for finding in findings)
    accepted = [finding.id for finding in findings if finding.resolution == "accepted"]
    accepted_detail = f" ({', '.join(accepted)})" if accepted else ""
    question_part = f", {len(questions)} question(s) ({', '.join(questions)})" if questions else ""
    return (
        f"Findings: {len(findings)} total; {awaiting} awaiting verdict, {unresolved} unresolved, "
        f"{fixed} fixed, {len(accepted)} accepted{accepted_detail}{question_part}, {dismissed} dismissed."
    )


def _runs_summary(ledger: Ledger, snap: DiffSnapshot) -> str:
    if not ledger.runs:
        return "Runs: none recorded."
    recent = ", ".join(
        f"{run.id} {run.kind} `{run.short_command}` exit {run.exit_code} "
        f"({'current' if run.fingerprint == snap.fingerprint else 'stale'})"
        for run in ledger.runs[-SUMMARY_RECENT_RUNS:]
    )
    return f"Runs: {len(ledger.runs)} recorded; latest: {recent}."


def render_next_actions(issues: Sequence[GateIssue], *, max_items: int = MAX_NEXT_ACTIONS) -> list[str]:
    """Numbered next-action lines, capped at `max_items` with a trailing count of the rest."""
    lines = [
        f"{index}. [{issue.code}] {issue.message}" for index, issue in enumerate(issues[:max_items], 1)
    ]
    if len(issues) > max_items:
        lines.append(f"… and {len(issues) - max_items} more")
    return lines


def render_report(report: GateReport) -> str:
    """Render a gate report as a compact Markdown block with numbered next actions."""
    lines = [f"**Craftsman {report.subject}: {report.status_label}**"]
    if report.self_reviewed:
        lines.append(f"**{SELF_REVIEWED_BANNER}**")
    lines += ["", report.summary]
    if report.issues:
        lines += ["", "**Next actions**", *render_next_actions(report.issues)]
    if report.warnings:
        lines += ["", "**Warnings**", *(f"- {warning}" for warning in report.warnings)]
    return "\n".join(lines)
