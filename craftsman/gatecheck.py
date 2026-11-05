"""Gate evaluation: decide whether the ledger's evidence covers the current diff, and report why.

`evaluate` is a pure function of a ledger, a diff snapshot and a policy; `render_report` turns its
`GateReport` into the Markdown block shown to agents and users.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from craftsman.diffscan import DiffSnapshot
from craftsman.ledger import uncovered_hunks
from craftsman.records import (
    REVIEWER_AGENT,
    IsolationEvidence,
    Ledger,
    LedgerError,
    Round,
    Run,
    RunKind,
    SignalKind,
    agent_role,
    parse_timestamp,
)
from craftsman.text import inline_md
from craftsman.triviality import command_without_env, is_trivial_run

IssueCode = Literal[
    "no-round",
    "stale-round",
    "uncovered",
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
MAX_NEXT_ACTIONS = 12
MAX_LISTED_HUNKS = 12
SUMMARY_RECENT_RUNS = 3
# Review rounds of different diffs before the agent must stop iterating and ask the user.
MAX_ROUNDS = 5
# Process anomalies: patterns typical of a review that was recorded rather than performed.
ANOMALY_MIN_FINDINGS = 5
PASSING_KINDS: tuple[RunKind, ...] = ("test", "e2e")
_VERIFYING_KINDS: tuple[RunKind, ...] = ("test", "e2e", "lint", "build")


# --- gate evaluation ------------------------------------------------------------------------


@dataclass(frozen=True)
class GateIssue:
    code: IssueCode
    message: str
    # Only for `uncovered`: how many hunks are still uncovered.
    missing: int = 0


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

    `isolation_evidence` names where proof of an isolated reviewer can come from; with `none`, the
    review passes as self-reviewed. `deep` additionally requires a passing end-to-end run.
    """

    deep: bool = False
    isolation_evidence: IsolationEvidence = "none"

    @property
    def require_isolation(self) -> bool:
        return self.isolation_evidence != "none"


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
        *(_coverage_issues(ledger, latest) if latest else ()),
        *_verdict_issues(ledger),
        *fix_and_test,
        *_isolation_issues(ledger, policy),
        *_round_cap_issues(ledger),
    )
    self_reviewed = bool(not policy.require_isolation and _review_gaps(ledger))
    return GateReport(
        issues,
        _warnings(ledger, review_only=review_only),
        _summary(ledger, snap),
        review_only=review_only,
        self_reviewed=self_reviewed,
    )


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
                "and see the suggested lenses.",
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


def _coverage_issues(ledger: Ledger, latest: Round) -> list[GateIssue]:
    missing = uncovered_hunks(ledger, latest)
    if not missing:
        return []
    batch = ",".join(missing[:MAX_LISTED_HUNKS])
    extra = len(missing) - MAX_LISTED_HUNKS
    more = f" (+{extra} more)" if extra > 0 else ""
    return [
        GateIssue(
            "uncovered",
            f"{len(missing)} hunk(s) uncovered in round {latest.number}: {batch}{more}; have them "
            f'reviewed and run `craftsman review cover --hunks {batch} --note "..."`.',
            missing=len(missing),
        )
    ]


def _verdict_issues(ledger: Ledger) -> list[GateIssue]:
    return [
        GateIssue(
            "no-verdict",
            f"Finding {finding.id} ({finding.severity}: {inline_md(finding.title)}) has no verdict; verify it "
            f"independently and run `craftsman review verdict {finding.id} "
            '--as proven|probable|question|dismissed --evidence "..."`.',
        )
        for finding in ledger.findings
        if finding.verdict is None
    ]


def _unresolved_issues(ledger: Ledger, snap: DiffSnapshot) -> list[GateIssue]:
    issues: list[GateIssue] = []
    for finding in ledger.findings:
        label = f"Finding {finding.id} ({finding.severity}: {inline_md(finding.title)})"
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
    elif policy.deep and not any(run.kind == "test" for run in passing):
        issues.append(
            GateIssue(
                "no-passing-run",
                "A deep gate needs a passing test run as well as the e2e run at the current "
                "code; run `craftsman run --kind test -- <your test command>`.",
            )
        )
    if policy.deep and not any(run.kind == "e2e" for run in passing):
        issues.append(
            GateIssue(
                "no-e2e-run",
                "A deep gate needs a passing end-to-end run at the current code; run "
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


@dataclass(frozen=True)
class _ReviewerRun:
    """A finished `craftsman-reviewer` run: when it ran, and the records its transcript shows."""

    started: datetime
    stopped: datetime
    records: tuple[str, ...] | None  # None: no transcript evidence, so records go by time

    def vouches_for(self, token: str, recorded_at: datetime) -> bool:
        """Whether this run wrote the record `token` names, made at `recorded_at`.

        By time always: nobody writes a record before it started or after it stopped, so a reviewer
        resumed across `review reset` (which restarts the numbering) cannot claim a new record with
        an old token. By identity as well where the transcript was read.
        """
        if not self.started <= recorded_at <= self.stopped:
            return False
        return self.records is None or token in self.records


def _reviewer_runs(ledger: Ledger) -> list[_ReviewerRun]:
    """Every finished `craftsman-reviewer` run.

    A stop with an agent id starts at the latest start of that id, which then leaves the queue of
    unpaired starts. Any other stop (no id, or an id no start carries, as Copilot starts by name
    only) is paired first-in, first-out with the unpaired starts of its name, so parallel runs of
    one name keep their union window. A stop without a start counts from the round that was current.
    """
    runs: list[_ReviewerRun] = []
    starts: list[tuple[str, str, datetime]] = []  # (agent name, agent id, when)
    taken: set[int] = set()  # indexes into `starts` that a stop has claimed
    for event in ledger.agents:
        if event.event == "start":
            starts.append((event.agent, event.agent_id, parse_timestamp(event.at)))
        elif agent_role(event.agent) == "reviewer":
            stopped = parse_timestamp(event.at)
            by_id = [i for i, (_, agent_id, _) in enumerate(starts) if event.agent_id and agent_id == event.agent_id]
            by_name = [i for i, (name, _, _) in enumerate(starts) if name == event.agent and i not in taken]
            chosen = by_id[-1] if by_id else by_name[0] if by_name else None
            if chosen is not None:
                taken.add(chosen)
                started = starts[chosen][2]
            else:
                current = [r for r in ledger.rounds if parse_timestamp(r.prepared_at) <= stopped]
                started = parse_timestamp(current[-1].prepared_at) if current else stopped
            runs.append(_ReviewerRun(started, stopped, event.records))
    return runs


def _record_times(ledger: Ledger) -> dict[str, datetime]:
    """When each record was made, by token: `C<n>` is `ledger.coverage[n - 1]`, `F<n>` the finding."""
    times = {f"C{index}": parse_timestamp(item.recorded_at) for index, item in enumerate(ledger.coverage, 1)}
    times.update({item.id: parse_timestamp(item.recorded_at) for item in ledger.findings})
    return times


def _latest_has_reviewer(ledger: Ledger, runs: Sequence[_ReviewerRun]) -> bool:
    """Whether a reviewer finished, having recorded something, while the latest round was current.

    Where a run's transcript was read, one of its own records must date from this round and from
    the run's window: a run whose tokens all belong to an earlier round does not count. Tokens no
    record carries are ignored.
    """
    start = parse_timestamp(ledger.rounds[-1].prepared_at)
    times = _record_times(ledger)
    for run in runs:
        if run.stopped < start:
            continue
        mine = times.values() if run.records is None else [times[t] for t in run.records if t in times]
        if any(max(start, run.started) <= at <= run.stopped for at in mine):
            return True
    return False


def _unexamined_hunks(ledger: Ledger, runs: Sequence[_ReviewerRun]) -> dict[int, list[str]]:
    """The hunks covered only outside every reviewer run, by round number.

    Coverage counts when a finished reviewer vouches for its record (`C<n>`, its position in the
    append-only coverage list): by identity where the run's transcript was read, else by time.
    Findings need no reviewer: verifiers judge every one. A round with no coverage holds nothing
    to examine, so one replaced before anyone reviewed it passes.
    """
    examined: dict[tuple[int, str], bool] = {}
    for index, record in enumerate(ledger.coverage, 1):
        recorded = parse_timestamp(record.recorded_at)
        vouched = any(run.vouches_for(f"C{index}", recorded) for run in runs)
        for digest in record.digests:
            key = (record.round, digest)
            examined[key] = examined.get(key, False) or vouched
    return {
        round_.number: ids
        for round_ in ledger.rounds
        if (ids := [h.id for h in round_.hunks if not h.carried and examined.get((round_.number, h.digest)) is False])
    }


@dataclass(frozen=True)
class _ReviewGaps:
    unexamined: dict[int, list[str]]  # round number -> hunks covered outside any reviewer run
    latest_lacks_reviewer: bool

    def __bool__(self) -> bool:
        return self.latest_lacks_reviewer or bool(self.unexamined)


def _review_gaps(ledger: Ledger) -> _ReviewGaps:
    """Where the isolated review is missing: the latest round needs a reviewer that recorded
    something; every round needs a reviewer behind each hunk it covered. A round whose hunks are
    all carried over needs no new reviewer."""
    if not ledger.rounds:
        return _ReviewGaps({}, False)
    runs = _reviewer_runs(ledger)
    return _ReviewGaps(
        _unexamined_hunks(ledger, runs),
        not all(hunk.carried for hunk in ledger.rounds[-1].hunks) and not _latest_has_reviewer(ledger, runs),
    )


def _isolation_issues(ledger: Ledger, policy: GatePolicy) -> list[GateIssue]:
    if not policy.require_isolation:
        return []
    latest_number = len(ledger.rounds)
    gaps = _review_gaps(ledger)
    latest: list[str] = []
    outside = gaps.unexamined.get(latest_number, [])
    if gaps.latest_lacks_reviewer:
        hint = (
            " Antigravity records finished reviewers from the transcript when your turn ends."
            if policy.isolation_evidence == "transcript"
            else ""
        )
        latest.append(
            f"No independent review finished since round {latest_number} was prepared; spawn "
            f"the {REVIEWER_AGENT} subagent for round {latest_number}.{hint}"
        )
    if outside:
        listed = _listed(outside)
        latest.append(
            f"Hunks {listed} of round {latest_number} were covered outside any finished "
            f"{REVIEWER_AGENT} run. Only a {REVIEWER_AGENT}'s own coverage counts, once it finishes: "
            f"if the reviewer covering these hunks is still running, wait for it; otherwise have a "
            f"{REVIEWER_AGENT} examine them and record its own coverage "
            f'(`craftsman review cover --hunks {",".join(outside[:MAX_LISTED_HUNKS])} --note "..."`).'
        )
    messages = [" ".join(latest)] if latest else []
    for number, ids in gaps.unexamined.items():
        if number != latest_number:
            messages.append(
                f"Round {number} has coverage that no {REVIEWER_AGENT} run examined (hunks "
                f"{_listed(ids)}), and a later round cannot make up for it. Run `craftsman "
                f"review reset`, prepare again and have the {REVIEWER_AGENT} examine the whole diff."
            )
    return [GateIssue("no-isolation", message) for message in messages]


def _listed(ids: Sequence[str]) -> str:
    more = f" (+{len(ids) - MAX_LISTED_HUNKS} more)" if len(ids) > MAX_LISTED_HUNKS else ""
    return ",".join(ids[:MAX_LISTED_HUNKS]) + more


def _round_cap_issues(ledger: Ledger) -> list[GateIssue]:
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


def _warnings(ledger: Ledger, *, review_only: bool = False) -> tuple[str, ...]:
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
    warnings.extend(_large_diff_warnings(ledger))
    warnings.extend(
        f"{run.id} (`{run.command}`) is trivial and does not count as verification."
        for run in ledger.runs
        if is_trivial_run(run.argv)
    )
    warnings.extend(_process_anomaly_warnings(ledger))
    return tuple(warnings)


def large_diff_warning(round_: Round) -> str | None:
    """A warning when the round's quality signals flag a diff too big for a thorough review."""
    for sig in round_.signals:
        if sig.kind is SignalKind.DIFF_SIZE:
            return f"Very large diff: {sig.detail}. Consider splitting the change; a review of this size misses things."
    return None


def _large_diff_warnings(ledger: Ledger) -> list[str]:
    warning = large_diff_warning(ledger.rounds[-1]) if ledger.rounds else None
    return [warning] if warning else []


def _summary(ledger: Ledger, snap: DiffSnapshot) -> str:
    if not ledger.rounds:
        round_line = "Round: none prepared yet."
        coverage_line = "Coverage: n/a."
    else:
        latest = ledger.rounds[-1]
        freshness = "current" if latest.fingerprint == snap.fingerprint else "stale"
        round_line = (
            f"Round {latest.number} ({'deep, ' if latest.deep else ''}{freshness}): "
            f"{len(latest.hunks)} hunks in {len(snap.files)} files, +{snap.added}/-{snap.deleted} lines."
        )
        coverage_line = _coverage_summary(ledger, latest)
    return "\n".join((round_line, coverage_line, _findings_summary(ledger), _runs_summary(ledger, snap)))


def _coverage_summary(ledger: Ledger, latest: Round) -> str:
    total = len(latest.open_hunk_ids)
    done = total - len(uncovered_hunks(ledger, latest))
    carried = len(latest.hunks) - total
    return f"Coverage: {done}/{total} hunks; {carried} hunk(s) carried from earlier rounds."


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


# One summary clause per issue code, in the order the agent should address them.
_ROUND_CLAUSES: dict[IssueCode, str] = {
    "no-round": "no review round prepared",
    "stale-round": "code changed since the review round",
    "round-cap": "maximum review rounds reached",
}
_COUNTED_CLAUSES: dict[IssueCode, tuple[str, str]] = {
    "no-verdict": ("finding without a verdict", "findings without a verdict"),
    "unresolved": ("unresolved finding", "unresolved findings"),
}
_RUN_CLAUSES: dict[IssueCode, str] = {
    "no-passing-run": "no passing test run",
    "no-e2e-run": "no passing e2e run",
}


def _counted(count: int, forms: tuple[str, str]) -> str:
    return f"{count} {forms[0] if count == 1 else forms[1]}"


def _coverage_clauses(issues: Sequence[GateIssue]) -> list[str]:
    total = sum(issue.missing for issue in issues if issue.code == "uncovered")
    return [f"{_counted(total, ('hunk', 'hunks'))} not covered"] if total else []


def format_blocker_summary(report: GateReport) -> str:
    """One line naming what still blocks the gate, e.g. `review incomplete: 3 hunks not covered;
    1 finding without a verdict`."""
    if not report.issues:
        return "review complete"
    codes = [issue.code for issue in report.issues]
    clauses = [clause for code, clause in _ROUND_CLAUSES.items() if code in codes]
    clauses += _coverage_clauses(report.issues)
    clauses += [_counted(codes.count(code), forms) for code, forms in _COUNTED_CLAUSES.items() if code in codes]
    clauses += [clause for code, clause in _RUN_CLAUSES.items() if code in codes]
    if "failing-run" in codes:
        clauses.append(_counted(codes.count("failing-run"), ("failing run", "failing runs")))
    if "no-isolation" in codes:
        clauses.append("no reviewer isolation evidence")
    return f"review incomplete: {'; '.join(clauses) or _counted(len(codes), ('open item', 'open items'))}"
