"""Summaries of ``evals.run agent`` results: recall, cost, tokens and time per arm and case.

``python3 -m evals.run compare a.jsonl [b.jsonl ...]`` reads the JSON lines written by ``agent``
(any number of files; arms are told apart by their ``arm`` label), prints one table per arm with a
row per case and a total, and, when there are several arms, the difference of every later arm to the
first one with a rough 90% interval.
"""

from __future__ import annotations

import argparse
import json
import math
from collections.abc import Callable, Iterable, Sequence
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


# The token counts of a session, by the names used here, in ``model_usage`` (per model).
TOKEN_FIELDS = ("input", "output", "cache_read", "cache_write")
_MODEL_USAGE_KEYS = ("inputTokens", "outputTokens", "cacheReadInputTokens", "cacheCreationInputTokens")
# Table columns of the token split, shown with --tokens and always carried in --json.
SPLIT_COLUMNS = ("input", "output", "cache read", "cache write")


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


def _is_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _tokens(value: Any) -> int:
    return value if _is_count(value) and value > 0 else 0


def _has_tokens(data: Any, keys: Sequence[str]) -> bool:
    return isinstance(data, dict) and any(_is_count(data.get(key)) for key in keys)


def session_tokens(record: dict[str, Any]) -> dict[str, int] | None:
    """The tokens of the whole session, subagents included, by ``TOKEN_FIELDS``; None when it has none.

    Read from ``model_usage`` summed over the models: the cost is computed from it, so it holds every
    agent of the session. ``usage`` is not a fallback: it is only the last turn of the main loop, so a
    record without token counts in ``model_usage`` has none.
    """
    models = record.get("model_usage")
    if isinstance(models, dict):
        entries = [entry for entry in models.values() if _has_tokens(entry, _MODEL_USAGE_KEYS)]
        if entries:
            return _summed(entries, _MODEL_USAGE_KEYS)
    return None


def _summed(sources: Sequence[dict[str, Any]], keys: Sequence[str]) -> dict[str, int]:
    return {
        name: sum(_tokens(source.get(key)) for source in sources)
        for name, key in zip(TOKEN_FIELDS, keys, strict=True)
    }


def compact(count: float) -> str:
    """A token count in a few characters: 950, 1.2k, 345k, 12.3M."""
    size = abs(count)
    if size >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if size >= 10_000:
        return f"{count / 1_000:.0f}k"
    if size >= 1_000:
        return f"{count / 1_000:.1f}k"
    return f"{count:.0f}"


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
    precision: list[float] = field(default_factory=list)  # runs that scored at least one finding
    cost: list[float] = field(default_factory=list)
    minutes: list[float] = field(default_factory=list)
    turns: list[int] = field(default_factory=list)
    subagents: list[int] = field(default_factory=list)
    tokens: list[dict[str, int]] = field(default_factory=list)  # of the runs that have token counts
    by_run: dict[int, int] = field(default_factory=dict)  # run index -> found
    bad: int = 0
    other: int = 0  # runs that ended early (timeout, budget) but are counted
    defect_hits: dict[str, int] = field(default_factory=dict)
    hook_errors: int = 0  # entries of the hook error logs of the valid runs (records that read them)

    @property
    def runs(self) -> int:
        return len(self.found)

    def mean_tokens(self) -> dict[str, float] | None:
        """Mean tokens per run by ``TOKEN_FIELDS``, plus ``total``, over the runs with token counts."""
        if not self.tokens:
            return None
        means = {name: mean([float(t[name]) for t in self.tokens]) for name in TOKEN_FIELDS}
        return {**means, "total": sum(means.values())}


def _count(value: Any) -> int:
    return len(value) if isinstance(value, list) else 0


def hook_error_count(record: dict[str, Any]) -> int:
    """The entries the run's hook error log held; 0 for a record written before the harness read the log."""
    errors = record.get("hook_errors")
    count = errors.get("count") if isinstance(errors, dict) else None
    return count if isinstance(count, int) and not isinstance(count, bool) else 0


def _hook_error_note(count: int) -> str:
    """A crashed hook still answers (and is asked to confirm or refused), so the run can look clean without this."""
    if not count:
        return ""
    return f"{count} hook error" + ("" if count == 1 else "s")


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
        if isinstance(recall.get("precision"), (int, float)):
            group.precision.append(float(recall["precision"]))
        group.cost.append(float(record.get("cost_usd") or 0.0))
        group.minutes.append(float(record.get("wall_s") or 0.0) / 60)
        group.turns.append(int(record.get("turns") or 0))
        group.hook_errors += hook_error_count(record)
        group.subagents.append(int((record.get("subagents") or {}).get("spawned", 0)))
        tokens = session_tokens(record)
        if tokens is not None:
            group.tokens.append(tokens)
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


def cost_per_defect(cases: Iterable[Group]) -> float | None:
    """The cost of one pass over the cases over the seeded defects it finds; None when it finds none.

    Both are sums of the case means, as in the TOTAL row: for one case, its cost over its defects found.
    """
    live = [g for g in cases if g.runs]
    found = sum(mean(g.found) for g in live)
    return sum(mean(g.cost) for g in live) / found if found else None


def pass_tokens(cases: Iterable[Group]) -> dict[str, float] | None:
    """The mean tokens of one pass over the cases (sums of the case means); None unless all have them."""
    totals: dict[str, float] = {}
    for g in cases:
        if not g.runs:
            continue
        tokens = g.mean_tokens()
        if tokens is None:
            return None
        for name, value in tokens.items():
            totals[name] = totals.get(name, 0.0) + value
    return totals or None


def _token_cells(tokens: dict[str, float] | None, per_defect: float | None) -> dict[str, str]:
    cells = {
        "tokens": compact(tokens["total"]) if tokens else "-",
        "$/defect": f"{per_defect:.2f}" if per_defect is not None else "-",
    }
    for column, name in zip(SPLIT_COLUMNS, TOKEN_FIELDS, strict=True):
        cells[column] = compact(tokens[name]) if tokens else "-"
    return cells


def arm_rows(cases: dict[str, Group]) -> list[dict[str, str]]:
    """Table rows of one arm: a row per case, then the total of a full pass over the cases."""
    rows = []
    for case_id in sorted(cases):
        g = cases[case_id]
        notes = (
            f"{g.bad} invalid" if g.bad else "",
            f"{g.other} ended early" if g.other else "",
            f"{g.runs - len(g.tokens)} without tokens" if len(g.tokens) < g.runs else "",
            _hook_error_note(g.hook_errors),
        )
        rows.append(
            {
                "case": case_id,
                "runs": str(g.runs),
                "recall": f"{mean(g.found):.2f}/{g.total} ({mean(g.found) / g.total:.0%})" if g.runs and g.total else "-",
                "range": _range(g.found),
                "decoys": f"{mean(g.decoys):.2f}" if g.runs else "-",
                "unmatched": f"{mean(g.unmatched):.2f}" if g.runs else "-",
                "precision": f"{mean(g.precision):.0%}" if g.precision else "-",
                "cost $": _pm(g.cost),
                "minutes": _pm(g.minutes, 1),
                "turns": f"{mean(g.turns):.0f}" if g.runs else "-",
                "subagents": f"{mean(g.subagents):.1f}" if g.runs else "-",
                **_token_cells(g.mean_tokens(), cost_per_defect([g])),
                "note": "; ".join(n for n in notes if n),
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
            "precision": f"{mean([mean(g.precision) for g in live if g.precision]):.0%}"
            if any(g.precision for g in live)
            else "-",
            "cost $": f"{sum(mean(g.cost) for g in live):.2f}" if live else "-",
            "minutes": f"{sum(mean(g.minutes) for g in live):.1f}" if live else "-",
            "turns": f"{sum(mean(g.turns) for g in live):.0f}" if live else "-",
            "subagents": f"{sum(mean(g.subagents) for g in live):.1f}" if live else "-",
            **_token_cells(pass_tokens(live), cost_per_defect(live)),
            "note": "; ".join(
                note
                for note in (
                    f"{sum(g.bad for g in cases.values())} invalid" if any(g.bad for g in cases.values()) else "",
                    _hook_error_note(sum(g.hook_errors for g in cases.values())),
                )
                if note
            ),
        }
    )
    return rows


def render_table(
    rows: list[dict[str, str]],
    columns: Sequence[str] | None = None,
    *,
    gap: int = 2,
    left: Sequence[str] = ("case", "note"),
) -> str:
    """``rows`` as aligned text: ``columns`` (default: every key of the first row) ``gap`` spaces apart,
    the ``left`` ones aligned left and the others right."""
    columns = list(rows[0]) if columns is None else list(columns)
    widths = {c: max(len(c), *(len(r[c]) for r in rows)) for c in columns}
    space = " " * gap
    head = space.join(c.ljust(widths[c]) if c in left else c.rjust(widths[c]) for c in columns)
    lines = [head.rstrip(), space.join("-" * widths[c] for c in columns)]
    for row in rows:
        cells = [row[c].ljust(widths[c]) if c in left else row[c].rjust(widths[c]) for c in columns]
        lines.append(space.join(cells).rstrip())
    return "\n".join(lines)


def arm_columns(rows: list[dict[str, str]], *, split: bool) -> list[str]:
    """The columns of an arm's text table: the token split only if ``split``, the note one if any."""
    return [
        c
        for c in rows[0]
        if (split or c not in SPLIT_COLUMNS) and (c != "note" or any(row["note"] for row in rows))
    ]


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


def _ratio(new: float | None, old: float | None) -> str:
    return f"x{new / old:.2f}" if new is not None and old else "-"


def _per_pass(groups: Sequence[Group], values: Callable[[Group], Sequence[float]]) -> float:
    return sum(mean(values(g)) for g in groups)


def _versus_row(label: str, a: Sequence[Group], b: Sequence[Group]) -> dict[str, str]:
    """How the groups ``b`` differ from ``a`` over one pass: recall in points, the rest as ratios."""
    tokens_a, tokens_b = pass_tokens(a), pass_tokens(b)
    found_a, found_b = _per_pass(a, lambda g: g.found), _per_pass(b, lambda g: g.found)
    return {
        "case": label,
        "recall": f"{(found_b - found_a) / sum(g.total for g in a) * 100:+.0f} pts",
        "cost": _ratio(_per_pass(b, lambda g: g.cost), _per_pass(a, lambda g: g.cost)),
        "minutes": _ratio(_per_pass(b, lambda g: g.minutes), _per_pass(a, lambda g: g.minutes)),
        "turns": _ratio(_per_pass(b, lambda g: g.turns), _per_pass(a, lambda g: g.turns)),
        "tokens": _ratio(tokens_b["total"], tokens_a["total"]) if tokens_a and tokens_b else "-",
        "$/defect": _ratio(cost_per_defect(b), cost_per_defect(a)),
    }


def versus_rows(a: dict[str, Group], b: dict[str, Group]) -> list[dict[str, str]]:
    """How arm ``b`` differs from arm ``a``: a row per case both ran, then the total over those cases."""
    shared = [c for c in sorted(set(a) & set(b)) if a[c].runs and b[c].runs and a[c].total]
    rows = [_versus_row(case_id, [a[case_id]], [b[case_id]]) for case_id in shared]
    if rows:
        rows.append(_versus_row("TOTAL", [a[c] for c in shared], [b[c] for c in shared]))
    return rows


def suite_difference(
    a: dict[str, Group], b: dict[str, Group]
) -> tuple[int, int, tuple[float, float, float] | None]:
    """The complete repeats of each arm over the cases both ran, and the difference of suite recall."""
    shared = {c: a[c] for c in a if c in b}
    sa, sb = suite_recalls(shared), suite_recalls({c: b[c] for c in shared})
    return len(sa), len(sb), difference(sa, sb)


def render_versus(first: str, other: str, a: dict[str, Group], b: dict[str, Group]) -> str:
    """How arm ``other`` differs from arm ``first``: per case and for the suite."""
    rows = versus_rows(a, b)
    lines = [f"{other} versus {first}"]
    if rows:
        lines += ["", render_table(rows)]
    count_a, count_b, diff = suite_difference(a, b)
    if diff is None:
        lines.append(
            f"\nSuite recall: {count_a} vs {count_b} complete repeats; "
            "run at least 2 per arm for an interval."
        )
    else:
        d, lo, hi = diff
        verdict = "can not be told from no change" if lo <= 0 <= hi else ("higher" if d > 0 else "lower")
        lines.append(
            f"\nSuite recall, {other} minus {first}: {d * 100:+.1f} pts, 90% interval "
            f"{lo * 100:+.1f} to {hi * 100:+.1f} pts ({count_a} vs {count_b} repeats): {verdict}."
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
    """Machine readable form of the tables, with the token means and the cost per defect as numbers."""
    names = list(arms)
    result: dict[str, Any] = {}
    for arm, cases in arms.items():
        entry: dict[str, Any] = {
            "rows": arm_rows(cases),
            "suite_recalls": suite_recalls(cases),
            "tokens": {
                **{case_id: cases[case_id].mean_tokens() for case_id in sorted(cases)},
                "TOTAL": pass_tokens(cases.values()),
            },
            "cost_per_defect": {
                **{case_id: cost_per_defect([cases[case_id]]) for case_id in sorted(cases)},
                "TOTAL": cost_per_defect(cases.values()),
            },
        }
        if arm != names[0]:
            _, _, diff = suite_difference(arms[names[0]], cases)
            entry["versus"] = {
                "against": names[0],
                "rows": versus_rows(arms[names[0]], cases),
                "suite_difference": list(diff) if diff is not None else None,
            }
        result[arm] = entry
    return result


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("results", nargs="+", type=Path, help="JSON lines written by 'agent'")
    parser.add_argument("--defects", action="store_true", help="also show how often each defect was found")
    parser.add_argument(
        "--tokens", action="store_true", help="also show input, output, cache read and cache write"
    )
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
        rows = arm_rows(arms[arm])
        print(f"arm {arm}: {runs} valid runs, ${spent:.2f} spent", file=out)
        # One space apart, so that the table of a full suite fits in 120 columns.
        print(render_table(rows, arm_columns(rows, split=args.tokens), gap=1), file=out)
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
        "its range is the suite recall per repeat. Unmatched findings are possible false positives;\n"
        "precision is the share of scored findings that point at a seeded defect. 'invalid' runs\n"
        "(plugin did not load, harness error, no result) are left out of every number. tokens: the\n"
        "whole session, subagents included (input, output, cache read and cache write; --tokens\n"
        "shows each); $/defect: the cost divided by the seeded defects found, '-' when none was found.",
        file=out,
    )
    return 0
