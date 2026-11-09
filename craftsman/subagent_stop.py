"""What a stopping Craftsman subagent still owes, shared by the Claude/Copilot SubagentStop hook and the
Antigravity Stop hook (a platform without subagent hooks reads the same rules from the ledger).

- A verifier owes the verdicts the gate waits for (`gatecheck.verification_debt`): it may stop once it recorded a
  real verdict of its own; a merge is no verdict, and a finding judged by someone else does not excuse it.
- A reviewer that holds no claim and recorded nothing owes the coverage and findings of its slice
  (`REVIEWER_STOP_REASON`).

The hooks decide when to send an agent back (at most once per run); this module only says what it owes.
"""

from __future__ import annotations

from datetime import datetime

from craftsman.gatecheck import verification_debt
from craftsman.records import GROUPED_EVIDENCE_PREFIX, Ledger, parse_timestamp

REVIEWER_STOP_REASON = (
    "Before finishing, record coverage (`craftsman review cover ...`) for the slice you "
    "examined, and a finding (`craftsman review add ...`) for every defect you found. If your prompt "
    "names a slot <ID>, run `craftsman review brief <ID>` first and add `--hunter <ID>` to both commands."
)


def verifier_debt(after: Ledger, records: tuple[str, ...] | None, since: datetime, *, by: str | None = None) -> str | None:
    """The reminder for a verifier that judged nothing of what the gate still waits for, None when it may finish.

    What a verifier owes is the gate's own verification debt (`gatecheck.verification_debt`: findings without a
    verdict, with a verdict no verifier vouches for, or dismissed serious ones that lack a second opinion).
    Per verifier: see `judged_since`.
    """
    owed = verification_debt(after, after.rounds[-1])
    if not owed or judged_since(after, records, since, by=by):
        return None
    unjudged = {finding.id for finding in after.findings if finding.verdict is None}
    ids = ", ".join(owed)
    if unjudged.issuperset(owed):
        subject = f"Findings still without a verdict: {ids}."
    else:
        subject = f"Findings that still need your independent verdict: {ids}."
    return (
        f"{subject} Record one for each with "
        '`craftsman review verdict <id> --as proven|probable|question|dismissed --evidence "..."` '
        "before finishing."
    )


def judged_since(ledger: Ledger, records: tuple[str, ...] | None, since: datetime, *, by: str | None = None) -> bool:
    """Whether the verifier that started at `since` recorded a verdict (a merge is none).

    With its transcript's `records`, one of them is the token of a verdict event that is no merge. Without
    (no transcript was readable), a verdict event that is no merge was recorded at or after `since`; a chained
    or repeated merge leaves several merge events, and every one of them is skipped. `by` is the verifier's
    identity (`antigravity:<conversation id>`, the platform's records name their caller): a verdict event that
    names it counts, and a verdict event that names someone else never does.
    """
    if by is not None and any(
        event.by == by and not event.merge for finding in ledger.findings for event in finding.verdict_log
    ):
        return True
    if records is not None:
        judging = {event.token for finding in ledger.findings for event in finding.verdict_log if not event.merge}
        return any(token in judging for token in records)
    for finding in ledger.findings:
        if finding.verdict is None:
            continue
        if any(
            not event.merge and (by is None or event.by is None) and parse_timestamp(event.at) >= since
            for event in finding.verdict_log
        ):
            return True
        merged = finding.verdict_evidence.startswith(GROUPED_EVIDENCE_PREFIX)
        if not finding.verdict_log and not merged and parse_timestamp(finding.updated_at) >= since:
            return True  # a verdict from before verdict events existed
    return False


def records_since(ledger: Ledger, since: datetime, by: str) -> bool:
    """Whether the caller `by` recorded coverage or a finding at or after `since`."""
    return any(parse_timestamp(item.recorded_at) >= since and item.by == by for item in ledger.coverage) or any(
        parse_timestamp(item.recorded_at) >= since and item.by == by for item in ledger.findings
    )
