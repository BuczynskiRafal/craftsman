"""Summaries of ``evals.run agent`` results: recall, cost and time per arm and case.

``python3 -m evals.run compare a.jsonl [b.jsonl ...]`` reads the JSON lines written by ``agent``
(any number of files; arms are told apart by their ``arm`` label), prints one table per arm with a
row per case and a total, and, when there are several arms, the difference of every later arm to the
first one with a rough 90% interval.
"""

from __future__ import annotations

import argparse
import json
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

from evals import run
from evals.agent import INVALID_STATUSES

# One-sided 95% t quantiles (a two-sided 90% interval) for 1..30 degrees of freedom.
_T95 = (
    6.314, 2.920, 2.353, 2.132, 2.015, 1.943, 1.895, 1.860, 1.833, 1.812,
    1.796, 1.782, 1.771, 1.761, 1.753, 1.746, 1.740, 1.734, 1.729, 1.725,
    1.721, 1.717, 1.714, 1.711, 1.708, 1.706, 1.703, 1.701, 1.699, 1.697,
)  # fmt: skip


def mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def spread(values: Sequence[float]) -> float | None:
    """Sample standard deviation; None with fewer than two values."""
    if len(values) < 2:
        return None
    m = mean(values)
    return math.sqrt(sum((v - m) ** 2 for v in values) / (len(values) - 1))


def t_quantile(df: float) -> float:
    """t quantile for a two-sided 90% interval at ``df`` degrees of freedom (rounded down)."""
    if df < 1:
        return _T95[0]
    return _T95[int(df) - 1] if df <= len(_T95) else 1.67


def load_records(paths: Iterable[Path]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in paths:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise run.EvalError(f"cannot read {path}: {exc}") from exc
        for number, line in enumerate(lines, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise run.EvalError(f"{path}:{number} is not valid JSON: {exc}") from exc
            if not isinstance(record, dict) or "arm" not in record or "case" not in record:
                raise run.EvalError(f"{path}:{number} is not a result record of 'evals.run agent'")
            records.append(record)
    return records


@dataclass
class Group:
    """The valid runs of one case in one arm, plus how many runs were left out."""

    total: int = 0  # defects in the case
    found: list[int] = field(default_factory=list)
    decoys: list[int] = field(default_factory=list)
    unmatched: list[int] = field(default_factory=list)
    cost: list[float] = field(default_factory=list)
    minutes: list[float] = field(default_factory=list)
    turns: list[int] = field(default_factory=list)
    subagents: list[int] = field(default_factory=list)
    by_run: dict[int, int] = field(default_factory=dict)  # run index -> found
    bad: int = 0
    other: int = 0  # runs that ended early (timeout, budget) but are counted
    defect_hits: dict[str, int] = field(default_factory=dict)

    @property
    def runs(self) -> int:
        return len(self.found)


def _count(value: Any) -> int:
    return len(value) if isinstance(value, list) else 0


def group_records(records: Iterable[dict[str, Any]]) -> dict[str, dict[str, Group]]:
    """arm -> case -> Group. Runs with a status in INVALID_STATUSES are counted in ``bad`` only."""
    arms: dict[str, dict[str, Group]] = {}
    for record in records:
        group = arms.setdefault(str(record["arm"]), {}).setdefault(str(record["case"]), Group())
        recall = record.get("recall") or {}
        group.total = int(record.get("defects_total") or recall.get("total") or group.total)
        if record.get("status") in INVALID_STATUSES:
            group.bad += 1
            continue
        if record.get("status") != "ok":
            group.other += 1
        found = int(recall.get("found", 0))
        group.found.append(found)
        group.by_run[int(record.get("run", len(group.found)))] = found
        group.decoys.append(_count(recall.get("decoy_hits")))
        group.unmatched.append(_count(recall.get("unmatched")))
        group.cost.append(float(record.get("cost_usd") or 0.0))
        group.minutes.append(float(record.get("wall_s") or 0.0) / 60)
        group.turns.append(int(record.get("turns") or 0))
        group.subagents.append(int((record.get("subagents") or {}).get("spawned", 0)))
        for defect_id in recall.get("found_ids", []):
            group.defect_hits[defect_id] = group.defect_hits.get(defect_id, 0) + 1
    return arms


def suite_recalls(cases: dict[str, Group]) -> list[float]:
    """Recall of the whole suite for each repeat index that has a valid run of every case."""
    indexes = set.intersection(*(set(g.by_run) for g in cases.values())) if cases else set()
    total = sum(g.total for g in cases.values())
    if not indexes or total == 0:
        return []
    return [sum(g.by_run[i] for g in cases.values()) / total for i in sorted(indexes)]


def _pm(values: Sequence[float], digits: int = 2) -> str:
    if not values:
        return "-"
    sd = spread(values)
    text = f"{mean(values):.{digits}f}"
    return text if sd is None else f"{text} ±{sd:.{digits}f}"


def _range(values: Sequence[float], percent: bool = False) -> str:
    if len(values) < 2:
        return "-"
    lo, hi = min(values), max(values)
    if percent:
        return f"{lo:.0%}-{hi:.0%}"
    return f"{lo:g}-{hi:g}"


def arm_rows(cases: dict[str, Group]) -> list[dict[str, str]]:
    """Table rows of one arm: a row per case, then the total of a full pass over the cases."""
    rows = []
    for case_id in sorted(cases):
        g = cases[case_id]
        note = "; ".join(
            n for n in (f"{g.bad} invalid" if g.bad else "", f"{g.other} ended early" if g.other else "") if n
        )
        rows.append(
            {
                "case": case_id,
                "runs": str(g.runs),
                "recall": f"{mean(g.found):.2f}/{g.total} ({mean(g.found) / g.total:.0%})" if g.runs and g.total else "-",
                "range": _range(g.found),
                "decoys": f"{mean(g.decoys):.2f}" if g.runs else "-",
                "unmatched": f"{mean(g.unmatched):.2f}" if g.runs else "-",
                "cost $": _pm(g.cost),
                "minutes": _pm(g.minutes, 1),
                "turns": f"{mean(g.turns):.0f}" if g.runs else "-",
                "subagents": f"{mean(g.subagents):.1f}" if g.runs else "-",
                "note": note,
            }
        )
    live = [g for g in cases.values() if g.runs]
    total_defects = sum(g.total for g in live)
    suite = suite_recalls(cases)
    rows.append(
        {
            "case": "TOTAL",
            "runs": str(min((g.runs for g in live), default=0)),
            "recall": f"{sum(mean(g.found) for g in live):.2f}/{total_defects} "
            f"({sum(mean(g.found) for g in live) / total_defects:.0%})"
            if total_defects
            else "-",
            "range": _range(suite, percent=True),
            "decoys": f"{sum(mean(g.decoys) for g in live):.2f}" if live else "-",
            "unmatched": f"{sum(mean(g.unmatched) for g in live):.2f}" if live else "-",
            "cost $": f"{sum(mean(g.cost) for g in live):.2f}" if live else "-",
            "minutes": f"{sum(mean(g.minutes) for g in live):.1f}" if live else "-",
            "turns": f"{sum(mean(g.turns) for g in live):.0f}" if live else "-",
            "subagents": f"{sum(mean(g.subagents) for g in live):.1f}" if live else "-",
            "note": f"{sum(g.bad for g in cases.values())} invalid" if any(g.bad for g in cases.values()) else "",
        }
    )
    return rows


def render_table(rows: list[dict[str, str]]) -> str:
    columns = list(rows[0])
    widths = {c: max(len(c), *(len(r[c]) for r in rows)) for c in columns}
    head = "  ".join(c.ljust(widths[c]) if c in ("case", "note") else c.rjust(widths[c]) for c in columns)
    lines = [head.rstrip(), "  ".join("-" * widths[c] for c in columns)]
    for row in rows:
        cells = [row[c].ljust(widths[c]) if c in ("case", "note") else row[c].rjust(widths[c]) for c in columns]
        lines.append("  ".join(cells).rstrip())
    return "\n".join(lines)


def difference(a: Sequence[float], b: Sequence[float]) -> tuple[float, float, float] | None:
    """mean(b) - mean(a) with a Welch two-sided 90% interval; None when either side has < 2 values."""
    sa, sb = spread(a), spread(b)
    if sa is None or sb is None:
        return None
    va, vb = sa**2 / len(a), sb**2 / len(b)
    diff = mean(b) - mean(a)
    if va + vb == 0:
        return diff, diff, diff
    df = (va + vb) ** 2 / ((va**2 / (len(a) - 1) if va else 0) + (vb**2 / (len(b) - 1) if vb else 0))
    half = t_quantile(df) * math.sqrt(va + vb)
    return diff, diff - half, diff + half


def _ratio(new: Sequence[float], old: Sequence[float]) -> str:
    return f"x{mean(new) / mean(old):.2f}" if new and old and mean(old) else "-"


def render_versus(first: str, other: str, a: dict[str, Group], b: dict[str, Group]) -> str:
    """How arm ``other`` differs from arm ``first``: per case and for the suite."""
    rows = []
    for case_id in sorted(set(a) & set(b)):
        ga, gb = a[case_id], b[case_id]
        if not ga.runs or not gb.runs or not ga.total:
            continue
        rows.append(
            {
                "case": case_id,
                "recall": f"{(mean(gb.found) - mean(ga.found)) / ga.total * 100:+.0f} pts",
                "cost": _ratio(gb.cost, ga.cost),
                "minutes": _ratio(gb.minutes, ga.minutes),
                "turns": _ratio([float(t) for t in gb.turns], [float(t) for t in ga.turns]),
            }
        )
    lines = [f"{other} versus {first}"]
    if rows:
        lines += ["", render_table(rows)]
    shared = {c: a[c] for c in a if c in b}
    sa, sb = suite_recalls(shared), suite_recalls({c: b[c] for c in shared})
    diff = difference(sa, sb)
    if diff is None:
        lines.append(f"\nSuite recall: {len(sa)} vs {len(sb)} complete repeats; run at least 2 per arm for an interval.")
    else:
        d, lo, hi = diff
        verdict = "can not be told from no change" if lo <= 0 <= hi else ("higher" if d > 0 else "lower")
        lines.append(
            f"\nSuite recall, {other} minus {first}: {d * 100:+.1f} pts, 90% interval "
            f"{lo * 100:+.1f} to {hi * 100:+.1f} pts ({len(sa)} vs {len(sb)} repeats): {verdict}."
        )
    return "\n".join(lines)


def render_defects(arms: dict[str, dict[str, Group]]) -> str:
    """For each seeded defect, in how many valid runs of each arm it was found."""
    names = sorted(arms)
    ids = sorted({d for cases in arms.values() for g in cases.values() for d in g.defect_hits})
    ids += sorted(
        {
            f"{case_id}: (never found)"
            for cases in arms.values()
            for case_id, g in cases.items()
            if g.runs and not g.defect_hits
        }
    )
    rows = []
    for defect_id in ids:
        row = {"defect": defect_id}
        for name in names:
            runs = {c: g for c, g in arms[name].items() if defect_id in g.defect_hits}
            hits = sum(g.defect_hits[defect_id] for g in runs.values())
            case_runs = sum(g.runs for g in runs.values())
            row[name] = f"{hits}/{case_runs}" if case_runs else "-"
        rows.append(row)
    return render_table(rows) if rows else "no defect was found in any run"


def summarize(arms: dict[str, dict[str, Group]]) -> dict[str, Any]:
    """Machine readable form of the tables."""
    return {
        arm: {
            "rows": arm_rows(cases),
            "suite_recalls": suite_recalls(cases),
        }
        for arm, cases in arms.items()
    }


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("results", nargs="+", type=Path, help="JSON lines written by 'agent'")
    parser.add_argument("--defects", action="store_true", help="also show how often each defect was found")
    parser.add_argument("--json", action="store_true", help="machine-readable output")


def cmd_compare(args: argparse.Namespace, out: TextIO) -> int:
    records = load_records(args.results)
    if not records:
        raise run.EvalError("no results in the given files")
    arms = group_records(records)
    if args.json:
        print(json.dumps(summarize(arms), indent=2), file=out)
        return 0
    for arm in arms:
        runs = sum(g.runs for g in arms[arm].values())
        spent = sum(sum(g.cost) for g in arms[arm].values())
        print(f"arm {arm}: {runs} valid runs, ${spent:.2f} spent", file=out)
        print(render_table(arm_rows(arms[arm])), file=out)
        print(file=out)
    names = list(arms)
    for other in names[1:]:
        print(render_versus(names[0], other, arms[names[0]], arms[other]), file=out)
        print(file=out)
    if args.defects:
        print("Defects found / valid runs of the case", file=out)
        print(render_defects(arms), file=out)
        print(file=out)
    print(
        "Per case: mean over the valid runs (± sample standard deviation); recall is defects found / seeded,\n"
        "range the fewest-most found in one run. TOTAL: sums of the case means, i.e. one pass over all cases;\n"
        "its range is the suite recall per repeat. Unmatched findings are possible false positives. "
        "'invalid' runs\n(plugin did not load, harness error, no result) are left out of every number.",
        file=out,
    )
    return 0
