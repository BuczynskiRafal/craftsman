"""Where a review spends its subagents and tokens, and which slots find the seeded defects.

Usage:
    python3 -m evals.run breakdown --out RUNS.jsonl [--arm NAME] [--cases] [--json]

Reads the result records ``evals.run agent`` wrote and the ledgers the runs kept (``logs.ledger``),
and prints per arm (and per case with ``--cases``): the subagent runs by role, slot kind and
generation; the tokens per role and the main session's share; the cache writes per subagent; the
findings funnel; which slots found the seeded defects first and which recorded the findings that
matched none; and the cost per found defect.

It only reads: no agent is started, and neither the records nor the ledgers are changed. Runs without
a kept ledger or without token counts are counted and named, never estimated. Runs are tied to slots,
and verdicts to verifier runs, by the product's own attribution (``craftsman.attribution``), findings
to seeded defects by the scorer's own rule (``run.nearest_item``). See ``evals/README.md``.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO

from craftsman.attribution import (
    AgentRun,
    SlotOrder,
    agent_runs,
    bound_slots,
    finding_slots,
    round_vouchers,
    run_key,
)
from craftsman.huntplan import slot_order, window_slots
from craftsman.records import LEAD_HUNTER, AgentRole, Finding, Ledger, LedgerError, parse_timestamp
from craftsman.usage import ROLE_LABELS, agent_usage

from evals import compare, rescore, run
from evals.agent import INVALID_STATUSES

SLOT_KINDS = ("bug", "simplicity", "sweep", "variant", "fresh")
NO_SLOT = "none"  # a hunter or a finding that no stored slot of its round accounts for
GENERATIONS = ("gen 1", "gen 2", "gen 3")
ROLES: tuple[AgentRole, ...] = ("reviewer", "verifier")
VERDICT_COLUMNS = ("proven", "probable", "question", "dismissed", "open")
TOKEN_COLUMNS = (*compare.TOKEN_FIELDS, "total")
AGENT_COLUMNS = ("hunters", "verifiers", "relaunched", "second opinions")
HUNTER_COLUMNS = (*SLOT_KINDS, NO_SLOT, *GENERATIONS)
FINDER_COLUMNS = (*SLOT_KINDS, "lead", NO_SLOT)
ALL_CASES = "all"


@dataclass
class Session:
    """What the record of one valid run and the ledger it kept say."""

    record: dict[str, Any]
    whole: dict[str, int] | None  # tokens of the whole session (`compare.session_tokens`)
    ledger_problem: str | None = None  # why no ledger was read; None when one was
    usage_problem: str | None = None  # why the main session's share is unknown; None when it is known
    case_problem: str | None = None  # why the findings could not be matched to the seeded defects
    agents: Counter[str] = field(default_factory=Counter)  # by role label, slot kind and generation
    tokens: dict[str, dict[str, float]] = field(default_factory=dict)  # role label -> token figures
    cache_writes: dict[str, list[int]] = field(default_factory=dict)  # role label -> one per subagent
    main: dict[str, int] | None = None  # the whole session minus its subagents
    funnel: Counter[str] = field(default_factory=Counter)
    found_by: Counter[str] = field(default_factory=Counter)  # slot kind and generation of first finders
    noise_by: Counter[str] = field(default_factory=Counter)  # slot kind of findings on no seeded defect

    @property
    def label(self) -> str:
        return f"{self.record['case']}#{self.record.get('run', '?')}"


# --- one run ---------------------------------------------------------------------------------


def kept_ledger(record: Mapping[str, Any]) -> tuple[Ledger | None, str | None]:
    """The ledger a run kept, or None and why there is none. Never writes, never sets a ledger aside."""
    logs = record.get("logs")
    path = logs.get("ledger") if isinstance(logs, dict) else None
    if not isinstance(path, str) or not path:
        return None, "no kept ledger (logs.ledger)"
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        return None, f"cannot read {path}: {exc.strerror or exc}"
    except json.JSONDecodeError as exc:
        return None, f"{path} is not valid JSON: {exc}"
    try:
        return Ledger.from_json(raw), None
    except LedgerError as exc:
        return None, f"{path}: {exc}"


def subagents(
    ledger: Ledger, runs: Sequence[AgentRun]
) -> dict[str, tuple[AgentRole, tuple[int, str] | None]]:
    """Every subagent by `run_key` (a resumed agent is one), with its role and the (round, slot) it took.

    The slot is the one the product binds the agent to (`bound_slots`), else the one a run without a
    readable transcript serves (`window_slots`), of the latest round; None for a verifier and for a
    hunter that no slot accounts for.
    """
    bound = bound_slots(ledger, runs, slot_order(ledger))
    window = window_slots(ledger, runs)
    agents: dict[str, tuple[AgentRole, tuple[int, str] | None]] = {}
    for index, item in enumerate(runs):
        key = run_key(item, index)
        if key in agents:
            continue  # a resumed agent: one key, one slot
        places = [(n, s) for (n, k), s in bound.items() if k == key] or [
            (n, s) for (n, k), s in window.items() if k == key
        ]
        agents[key] = (item.role, max(places) if places else None)
    return agents


def slot_columns(ledger: Ledger, place: tuple[int, str] | None) -> tuple[str, str | None]:
    """The kind and the generation column of a (round, slot); (`NO_SLOT`, None) for a slot not stored."""
    if place is None:
        return NO_SLOT, None
    number, slot_id = place
    plan = ledger.rounds[number - 1].plan if number <= len(ledger.rounds) else None
    slot = plan.slot(slot_id) if plan is not None else None
    if slot is None:
        return NO_SLOT, None
    return slot.kind, GENERATIONS[slot.generation - 1]


def second_opinions(ledger: Ledger, runs: Sequence[AgentRun]) -> int:
    """The verifier runs that only judged findings another verifier run had judged before them.

    A verdict counts for the runs that vouch for it (`round_vouchers`, the rule of the gate); a merge
    judges nothing and is left out.
    """
    first: set[str] = set()
    second: set[str] = set()
    for finding in ledger.findings:
        judged: set[str] = set()
        for event in finding.verdict_log:
            if event.merge:
                continue
            vouchers = round_vouchers(
                ledger,
                runs,
                finding.round,
                role="verifier",
                token=event.token,
                recorded_at=parse_timestamp(event.at),
                by=event.by,
            )
            keys = {run_key(voucher.run, voucher.index) for voucher in vouchers}
            (second if judged else first).update(keys - judged)
            judged |= keys
    return len(second - first)


def _usage(session: Session, ledger: Ledger, agents: Mapping[str, tuple[AgentRole, Any]]) -> None:
    """Tokens per role (`agent_usage`: the rule of `review report`) and, when every subagent reported its
    usage and the record has token counts that hold them, the main session's.

    Subagents that report more tokens of a kind than the whole session mean the two sources disagree;
    the share is then unknown, never negative."""
    used = agent_usage(ledger)
    missing = 0
    for role in ROLES:
        label = ROLE_LABELS[role]
        usages = [usage for (owner, _), usage in used.items() if owner == role]
        figures = {
            "input": sum(u.input_tokens for u in usages),
            "output": sum(u.output_tokens for u in usages),
            "cache_read": sum(u.cache_read_tokens for u in usages),
            "cache_write": sum(u.cache_write_tokens for u in usages),
        }
        session.tokens[label] = {
            **figures,
            "total": sum(figures.values()),
            "agents": len(usages),
            "minutes": sum(u.duration_s for u in usages) / 60,
        }
        session.cache_writes[label] = [u.cache_write_tokens for u in usages]
        missing += max(sum(owner == role for owner, _ in agents.values()) - len(usages), 0)
    if missing:
        session.usage_problem = f"{missing} of {len(agents)} subagents have no usage in the ledger"
    elif session.whole is None:
        session.usage_problem = "the record has no token counts"
    else:
        main = {
            name: session.whole[name] - int(sum(session.tokens[ROLE_LABELS[r]][name] for r in ROLES))
            for name in compare.TOKEN_FIELDS
        }
        over = [name.replace("_", " ") for name, count in main.items() if count < 0]
        if over:
            session.usage_problem = f"its subagents report more {', '.join(over)} tokens than the record"
        else:
            session.main = main


def finding_slot(
    ledger: Ledger,
    finding: Finding,
    runs: Sequence[AgentRun],
    order: SlotOrder,
    bound: Mapping[tuple[int, str], str],
) -> str | None:
    """The stored slot a finding is credited to; None for the lead's own finding or one of no slot.

    As the hunt plan decides a finding's generation: the stored slot bound to the run that recorded it
    (`finding_slots`) outranks the slot the record names (`hunter`), which counts only when no
    recording run is bound to a stored slot.
    """
    stored = list(order(finding.round))
    bound_to = finding_slots(ledger, finding, runs, order, bound=bound)
    chosen = sorted((slot for slot in bound_to if slot in stored), key=stored.index)
    if chosen:
        return chosen[0]
    return finding.hunter if finding.hunter in stored else None


def _recorded(finding: Finding) -> tuple[datetime, int]:
    return parse_timestamp(finding.recorded_at), int(finding.id[1:])


def _yield(
    session: Session, ledger: Ledger, runs: Sequence[AgentRun], case: run.Case, workdir: Path | None
) -> None:
    """Who found each seeded defect first, and who recorded the findings that point at none.

    A finding points at the item the scorer assigns it to (`run.nearest_item`). As in scoring, a
    dismissed finding finds no defect; a merged duplicate counts nowhere, its target stands for it.
    """
    order = slot_order(ledger)
    bound = bound_slots(ledger, runs, order)

    def columns(finding: Finding) -> tuple[str, str | None]:
        slot = finding_slot(ledger, finding, runs, order, bound)
        if slot is None:
            return ("lead" if finding.hunter == LEAD_HUNTER else NO_SLOT), None
        return slot_columns(ledger, (finding.round, slot))

    first: dict[str, Finding] = {}  # seeded defect -> the finding that recorded it first
    for finding in ledger.findings:
        if finding.merged_into is not None:
            continue
        located = run.Finding(run.normalize_path(finding.path, workdir), finding.line, finding.title)
        near = run.nearest_item(case, rescore._relative_to_case([located], case)[0])
        if near is None or near[1].kind == "decoy":
            session.noise_by[columns(finding)[0]] += 1
        elif finding.verdict != "dismissed":
            earliest = first.get(near[1].id)
            if earliest is None or _recorded(finding) < _recorded(earliest):
                first[near[1].id] = finding
    for finding in first.values():
        kind, generation = columns(finding)
        session.found_by[kind] += 1
        if generation is not None:
            session.found_by[generation] += 1


def analyze(record: dict[str, Any], cases_dir: Path) -> Session:
    """Everything the breakdown needs from one valid run."""
    session = Session(record, compare.session_tokens(record))
    ledger, problem = kept_ledger(record)
    if ledger is None:
        session.ledger_problem = problem
        return session
    runs = agent_runs(ledger)
    agents = subagents(ledger, runs)
    hunters_of: dict[tuple[int, str], set[str]] = {}
    for key, (role, place) in agents.items():
        session.agents[ROLE_LABELS[role]] += 1
        if role == "reviewer":
            kind, generation = slot_columns(ledger, place)
            session.agents[kind] += 1
            if generation is not None:
                session.agents[generation] += 1
            if place is not None:
                hunters_of.setdefault(place, set()).add(key)
    session.agents["relaunched"] = sum(len(keys) > 1 for keys in hunters_of.values())
    session.agents["second opinions"] = second_opinions(ledger, runs)
    _usage(session, ledger, agents)
    for finding in ledger.findings:
        session.funnel["recorded"] += 1
        session.funnel["merged" if finding.merged_into is not None else finding.verdict or "open"] += 1
    try:
        case = run.load_case(str(record["case"]), cases_dir)
    except run.EvalError as exc:
        session.case_problem = str(exc)
        return session
    logs = record.get("logs")
    workdir = logs.get("workdir") if isinstance(logs, dict) else None
    _yield(session, ledger, runs, case, Path(workdir) if isinstance(workdir, str) else None)
    return session


# --- groups of runs --------------------------------------------------------------------------


def _mean(sessions: Sequence[Session], value: Callable[[Session], float]) -> float | None:
    return compare.mean([value(s) for s in sessions]) if sessions else None


def _column_means(
    sessions: Sequence[Session], counts: Callable[[Session], Counter[str]], columns: Sequence[str]
) -> dict[str, float | None]:
    return {c: compare.mean([counts(s)[c] for s in sessions]) if sessions else None for c in columns}


def _token_rows(sessions: Sequence[Session]) -> dict[str, dict[str, float]] | None:
    """Mean tokens per run of each role, of the main and of the whole session, with each one's share."""
    if not sessions:
        return None
    rows: dict[str, dict[str, float]] = {}
    for role in ROLES:
        label = ROLE_LABELS[role]
        rows[label] = {
            name: compare.mean([s.tokens[label][name] for s in sessions])
            for name in (*TOKEN_COLUMNS, "agents", "minutes")
        }
    parts = {"main session": [s.main for s in sessions], "whole session": [s.whole for s in sessions]}
    for label, figures in parts.items():
        known = [f for f in figures if f is not None]
        means = {name: compare.mean([float(f[name]) for f in known]) for name in compare.TOKEN_FIELDS}
        rows[label] = {**means, "total": sum(means.values())}
    whole = rows["whole session"]["total"]
    for row in rows.values():
        row["share"] = row["total"] / whole if whole else 0.0
    return rows


def _cache_writes(writes: Sequence[int]) -> dict[str, float] | None:
    if not writes:
        return None
    mean = compare.mean([float(write) for write in writes])
    return {"subagents": len(writes), "mean": mean, "min": min(writes)}


def summarize(sessions: Sequence[Session]) -> dict[str, Any]:
    """The figures of a group of valid runs: means per run, except the yield, which is summed."""
    ledgered = [s for s in sessions if s.ledger_problem is None]
    counted = [s for s in ledgered if s.main is not None]
    matched = [s for s in ledgered if s.case_problem is None]
    return {
        "runs": len(sessions),
        "with_ledger": len(ledgered),
        "with_tokens": len(counted),
        "cost_usd": _mean(sessions, lambda s: float(s.record.get("cost_usd") or 0.0)),
        "minutes": _mean(sessions, lambda s: float(s.record.get("wall_s") or 0.0) / 60),
        "subagents": _column_means(ledgered, lambda s: s.agents, (*AGENT_COLUMNS, *HUNTER_COLUMNS)),
        "tokens": _token_rows(counted),
        "cache_write_per_subagent": {
            label: _cache_writes([write for s in counted for write in s.cache_writes[label]])
            for label in ROLE_LABELS.values()
        },
        "funnel": _column_means(ledgered, lambda s: s.funnel, ("recorded", "merged", *VERDICT_COLUMNS)),
        "yield_runs": len(matched),
        "found_first_by": {
            "found": sum(s.found_by[c] for s in matched for c in FINDER_COLUMNS),
            **{c: sum(s.found_by[c] for s in matched) for c in (*FINDER_COLUMNS, *GENERATIONS)},
        },
        "noise_by": {
            "findings": sum(s.noise_by[c] for s in matched for c in FINDER_COLUMNS),
            **{c: sum(s.noise_by[c] for s in matched) for c in FINDER_COLUMNS},
        },
    }


def _named(
    sessions: Iterable[Session], problem: Callable[[Session], str | None]
) -> list[dict[str, str]]:
    """The runs that have a ``problem``, each with it."""
    return [{"run": s.label, "reason": reason} for s in sessions if (reason := problem(s)) is not None]


def breakdown(records: Iterable[dict[str, Any]], cases_dir: Path, *, by_case: bool) -> dict[str, Any]:
    """Per arm: the figures per case (with ``by_case``) and of all cases, and the runs they leave out."""
    arms: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        arms.setdefault(str(record["arm"]), []).append(record)
    result: dict[str, Any] = {}
    for arm, items in arms.items():
        invalid = [
            {"run": f"{r['case']}#{r.get('run', '?')}", "reason": str(r["status"])}
            for r in items
            if r.get("status") in INVALID_STATUSES
        ]
        sessions = [analyze(r, cases_dir) for r in items if r.get("status") not in INVALID_STATUSES]
        groups = compare.group_records(items)[arm]
        summaries = [
            {
                "case": case_id,
                **summarize([s for s in sessions if str(s.record["case"]) == case_id]),
                "cost_per_defect": compare.cost_per_defect([groups[case_id]]),
            }
            for case_id in (sorted(groups) if by_case else [])
        ]
        summaries.append(
            {
                "case": ALL_CASES,
                **summarize(sessions),
                "cost_per_defect": compare.cost_per_defect(groups.values()),
            }
        )
        result[arm] = {
            "groups": summaries,
            "invalid": invalid,
            "without_ledger": _named(sessions, lambda s: s.ledger_problem),
            "without_tokens": _named(sessions, lambda s: s.usage_problem),
            "unknown_case": _named(sessions, lambda s: s.case_problem),
        }
    return result


# --- text ------------------------------------------------------------------------------------

Cells = Callable[[Mapping[str, Any]], list[dict[str, str]]]


def _number(value: float | None, digits: int = 1) -> str:
    return "-" if value is None else f"{value:.{digits}f}"


def _tokens(value: float | None) -> str:
    return "-" if value is None else compare.compact(value)


def _table(result: Mapping[str, Any], by_case: bool, cells: Cells) -> str:
    """The rows ``cells`` makes of each group, after its arm (and case), in the order of ``result``."""
    rows = [
        {"arm": arm, **({"case": group["case"]} if by_case else {}), **row}
        for arm, entry in result.items()
        for group in entry["groups"]
        for row in cells(group)
    ]
    return compare.render_table(rows, left=("arm", "case", "of")) if rows else "(no runs)"


def _runs(group: Mapping[str, Any]) -> list[dict[str, str]]:
    return [
        {
            "runs": str(group["runs"]),
            "with ledger": str(group["with_ledger"]),
            "with tokens": str(group["with_tokens"]),
            "cost $": _number(group["cost_usd"], 2),
            "minutes": _number(group["minutes"]),
            "$/defect": _number(group["cost_per_defect"], 2),
        }
    ]


def _subagents(columns: Sequence[str]) -> Cells:
    return lambda group: [{c: _number(group["subagents"][c]) for c in columns}]


def _token_cells(group: Mapping[str, Any]) -> list[dict[str, str]]:
    return [
        {
            "of": label,
            "agents": _number(row.get("agents")),
            **{name.replace("_", " "): _tokens(row[name]) for name in TOKEN_COLUMNS},
            "agent min": _number(row.get("minutes")),
            "share": f"{row['share']:.0%}",
        }
        for label, row in (group["tokens"] or {}).items()
    ]


def _cache_write_cells(group: Mapping[str, Any]) -> list[dict[str, str]]:
    return [
        {
            "of": label,
            "subagents": str(row["subagents"]),
            "mean": _tokens(row["mean"]),
            "min": _tokens(row["min"]),
        }
        for label, row in group["cache_write_per_subagent"].items()
        if row is not None
    ]


def _counts(key: str, per_run: bool) -> Cells:
    def cells(group: Mapping[str, Any]) -> list[dict[str, str]]:
        counts = {c: _number(v) if per_run else str(v) for c, v in group[key].items()}
        return [counts if per_run else {"runs": str(group["yield_runs"]), **counts}]

    return cells


SECTIONS: tuple[tuple[str, Cells], ...] = (
    ("Runs: valid runs, how many kept a ledger and full token counts; means per run", _runs),
    ("Subagents per run with a ledger (a resumed agent counts once)", _subagents(AGENT_COLUMNS)),
    (
        "Hunters per run with a ledger, by the kind and generation of their slot",
        _subagents(HUNTER_COLUMNS),
    ),
    ("Tokens per run with full token counts; main session: the whole minus its subagents", _token_cells),
    (
        "Cache write per subagent in those runs; the minimum is about the fixed starting context",
        _cache_write_cells,
    ),
    ("Findings per run with a ledger: recorded, merged, by current verdict", _counts("funnel", True)),
    (
        "Seeded defects found, by the slot that recorded them first (sums)",
        _counts("found_first_by", False),
    ),
    (
        "Findings on no seeded defect (unmatched, decoy; dismissed too), by slot (sums)",
        _counts("noise_by", False),
    ),
)
LEFT_OUT = (
    ("invalid", "invalid, left out"),
    ("without_ledger", "without a ledger"),
    ("without_tokens", "without full token counts"),
    ("unknown_case", "of an unknown case"),
)


def render(result: Mapping[str, Any], by_case: bool) -> str:
    blocks = [f"{title}\n{_table(result, by_case, cells)}" for title, cells in SECTIONS]
    lines = []
    for arm, entry in result.items():
        per_defect = entry["groups"][-1]["cost_per_defect"]
        cost = "-" if per_defect is None else f"${per_defect:.2f}"
        lines.append(f"arm {arm}: cost per found defect {cost}")
        for key, what in LEFT_OUT:
            if entry[key]:
                named = "; ".join(f"{item['run']} ({item['reason']})" for item in entry[key])
                lines.append(f"  {len(entry[key])} {what}: {named}")
    return "\n\n".join([*blocks, "\n".join(lines)])


# --- command line ----------------------------------------------------------------------------


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--out", type=Path, required=True, help="the results file 'agent' wrote")
    parser.add_argument("--arm", help="only this arm")
    parser.add_argument("--cases", action="store_true", help="also a row per case")
    parser.add_argument("--json", action="store_true", help="machine-readable output")


def cmd_breakdown(args: argparse.Namespace, out: TextIO) -> int:
    records = compare.load_records([args.out])
    if args.arm is not None:
        arms = sorted({str(r["arm"]) for r in records})
        records = [r for r in records if str(r["arm"]) == args.arm]
        if not records:
            raise run.EvalError(f"no runs of arm '{args.arm}' in {args.out} (arms: {', '.join(arms)})")
    if not records:
        raise run.EvalError(f"no results in {args.out}")
    result = breakdown(records, args.cases_dir, by_case=args.cases)
    print(json.dumps(result, indent=2) if args.json else render(result, args.cases), file=out)
    return 0
