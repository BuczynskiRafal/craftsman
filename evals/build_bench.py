"""Build-and-verify coding benchmark harness (`evals.build_bench`).

Zero-dependency stdlib harness that manages realistic multi-module starter repositories,
grades finished implementations against hidden test suites without leaking hidden tests into
agent workspaces, and summarizes A/B JSONL results.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

BUILD_CASES_DIR = Path(__file__).resolve().parent / "build_cases"
STARTER_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache", ".mypy_cache", ".ruff_cache")
GRADE_TIMEOUT_S = 60


class BuildBenchError(Exception):
    """Raised when a build-benchmark case, workspace, or JSONL input is invalid."""


@dataclass(frozen=True)
class BuildCase:
    id: str
    path: Path
    starter_dir: Path
    task_file: Path
    hidden_tests_file: Path


def case_ids(cases_dir: Path = BUILD_CASES_DIR) -> list[str]:
    if not cases_dir.is_dir():
        return []
    return sorted(
        p.name
        for p in cases_dir.iterdir()
        if p.is_dir() and (p / "starter").is_dir() and (p / "TASK.md").is_file() and (p / "hidden_tests.py").is_file()
    )


def load_case(case_id: str, cases_dir: Path = BUILD_CASES_DIR) -> BuildCase:
    path = cases_dir / case_id
    starter_dir = path / "starter"
    task_file = path / "TASK.md"
    hidden_file = path / "hidden_tests.py"
    if not (starter_dir.is_dir() and task_file.is_file() and hidden_file.is_file()):
        known = ", ".join(case_ids(cases_dir)) or "none"
        raise BuildBenchError(f"unknown build case '{case_id}' (available: {known})")
    return BuildCase(case_id, path, starter_dir, task_file, hidden_file)


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        raise BuildBenchError(f"git {' '.join(args)} failed in {cwd}: {detail}")
    return proc.stdout


def prepare_case(case: BuildCase, dest: Path) -> Path:
    """Create a fresh git repository in ``dest`` containing starter code and ``TASK.md`` only.

    ``hidden_tests.py`` is intentionally never copied into ``dest``.
    """
    dest = dest.resolve()
    if dest.exists() and any(dest.iterdir()):
        raise BuildBenchError(f"destination already exists and is not empty: {dest}")
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copytree(case.starter_dir, dest, ignore=STARTER_IGNORE, dirs_exist_ok=True)
    shutil.copyfile(case.task_file, dest / "TASK.md")

    _git(dest, "init", "-q")
    _git(dest, "config", "user.name", "Benchmark Starter")
    _git(dest, "config", "user.email", "starter@example.invalid")
    exclude = dest / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    with exclude.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write("__pycache__/\n*.pyc\n.pytest_cache/\n")
    _git(dest, "add", "-A")
    _git(dest, "commit", "-q", "-m", f"chore: initial starter for {case.id}")
    return dest


_RUNNER_SCRIPT = r"""
import json, sys, unittest
target_dir, pattern = sys.argv[1], sys.argv[2]
loader = unittest.TestLoader()
suite = loader.discover(start_dir=target_dir, pattern=pattern, top_level_dir=target_dir if pattern != "test_*.py" else None)
runner = unittest.TextTestRunner(verbosity=0, stream=sys.stderr)
res = runner.run(suite)
failed = [t.id() for t, _ in res.failures] + [t.id() for t, _ in res.errors]
out = {
    "ran": res.testsRun,
    "failures": len(res.failures),
    "errors": len(res.errors),
    "skipped": len(res.skipped),
    "passed": max(0, res.testsRun - len(res.failures) - len(res.errors) - len(res.skipped)),
    "ok": res.wasSuccessful() and len(res.skipped) == 0 and res.testsRun > 0,
    "failed_tests": failed,
}
print("__BENCH_JSON__" + json.dumps(out))
sys.exit(0 if out["ok"] else 1)
"""


def _run_unittest_suite(workdir: Path, test_dir: Path, pattern: str, timeout_s: int = GRADE_TIMEOUT_S) -> dict[str, Any]:
    env = os.environ.copy()
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{workdir}{os.pathsep}{existing}" if existing else str(workdir)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        proc = subprocess.run(
            [sys.executable, "-c", _RUNNER_SCRIPT, str(test_dir), pattern],
            cwd=workdir,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "ran": 0,
            "failures": 0,
            "errors": 1,
            "skipped": 0,
            "passed": 0,
            "ok": False,
            "timed_out": True,
            "failed_tests": ["<timeout>"],
        }
    for line in reversed(proc.stdout.splitlines()):
        if line.startswith("__BENCH_JSON__"):
            payload = json.loads(line[len("__BENCH_JSON__") :])
            payload["timed_out"] = False
            return dict(payload)
    tail = "\n".join((proc.stderr or proc.stdout).splitlines()[-20:])
    return {
        "ran": 0,
        "failures": 0,
        "errors": 1,
        "skipped": 0,
        "passed": 0,
        "ok": False,
        "timed_out": False,
        "failed_tests": [f"<runner-error rc={proc.returncode}>"],
        "stderr_tail": tail,
    }


def grade_workdir(case: BuildCase, workdir: Path, *, timeout_s: int = GRADE_TIMEOUT_S) -> dict[str, Any]:
    """Run both ``tests/test_starter.py`` and the external ``hidden_tests.py`` against ``workdir``."""
    workdir = workdir.resolve()
    if not workdir.is_dir():
        raise BuildBenchError(f"workdir does not exist: {workdir}")

    tests_dir = workdir / "tests"
    starter = (
        _run_unittest_suite(workdir, tests_dir, "test_starter.py", timeout_s=timeout_s)
        if (tests_dir / "test_starter.py").is_file()
        else {"ran": 0, "passed": 0, "failures": 0, "errors": 1, "skipped": 0, "ok": False, "timed_out": False, "failed_tests": ["<missing tests/test_starter.py>"]}
    )
    all_repo_tests = (
        _run_unittest_suite(workdir, tests_dir, "test_*.py", timeout_s=timeout_s)
        if tests_dir.is_dir()
        else starter
    )

    with tempfile.TemporaryDirectory(prefix=f"grade-{case.id}-") as tmp:
        stage = Path(tmp)
        shutil.copyfile(case.hidden_tests_file, stage / "test_hidden.py")
        hidden = _run_unittest_suite(workdir, stage, "test_hidden.py", timeout_s=timeout_s)

    return {
        "case": case.id,
        "starter": starter,
        "repo_tests": all_repo_tests,
        "hidden": hidden,
        "all_passed": bool(starter["ok"] and hidden["ok"]),
    }


def _mean(vals: Sequence[float]) -> float:
    return sum(vals) / len(vals) if vals else 0.0


def _stdev(vals: Sequence[float]) -> float | None:
    if len(vals) < 2:
        return None
    m = _mean(vals)
    return math.sqrt(sum((v - m) ** 2 for v in vals) / (len(vals) - 1))


def _compact(count: float) -> str:
    size = abs(count)
    if size >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if size >= 10_000:
        return f"{count / 1_000:.0f}k"
    if size >= 1_000:
        return f"{count / 1_000:.1f}k"
    return f"{count:.0f}"


def load_jsonl(paths: Sequence[Path]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in paths:
        if not path.is_file():
            raise BuildBenchError(f"results file not found: {path}")
        for idx, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            raw = line.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise BuildBenchError(f"{path}:{idx}: invalid JSON: {exc}") from exc
            if not isinstance(obj, dict):
                raise BuildBenchError(f"{path}:{idx}: expected JSON object")
            records.append(obj)
    return records


def summarize_records(records: Sequence[dict[str, Any]]) -> dict[str, Any]:
    by_arm: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for rec in records:
        by_arm[str(rec.get("arm", "unknown"))].append(rec)

    arms_out: dict[str, Any] = {}
    for arm, items in sorted(by_arm.items()):
        ok_items = [r for r in items if r.get("status") == "ok"]
        by_case: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in ok_items:
            by_case[str(r.get("case"))].append(r)

        case_rows: dict[str, Any] = {}
        for cid, cruns in sorted(by_case.items()):
            h_rates = [
                (r["hidden"]["passed"] / r["hidden"]["ran"]) if r.get("hidden", {}).get("ran", 0) > 0 else 0.0
                for r in cruns
            ]
            s_pass = [1.0 if r.get("starter", {}).get("ok") else 0.0 for r in cruns]
            full_pass = [1.0 if r.get("all_passed") else 0.0 for r in cruns]
            toks = [float(r.get("tokens_total", 0)) for r in cruns]
            subs = [float(r.get("subagents", 0)) for r in cruns]
            durs = [float(r.get("duration_s", 0.0)) for r in cruns]
            case_rows[cid] = {
                "runs": len(cruns),
                "full_pass_rate": _mean(full_pass),
                "hidden_pass_rate": _mean(h_rates),
                "starter_pass_rate": _mean(s_pass),
                "mean_tokens": _mean(toks),
                "mean_subagents": _mean(subs),
                "mean_duration_s": _mean(durs),
                "enforced_runs": sum(1 for r in cruns if r.get("enforced")),
            }

        all_h_rates = [
            (r["hidden"]["passed"] / r["hidden"]["ran"]) if r.get("hidden", {}).get("ran", 0) > 0 else 0.0
            for r in ok_items
        ]
        all_full = [1.0 if r.get("all_passed") else 0.0 for r in ok_items]
        all_starter = [1.0 if r.get("starter", {}).get("ok") else 0.0 for r in ok_items]
        all_toks = [float(r.get("tokens_total", 0)) for r in ok_items]
        all_subs = [float(r.get("subagents", 0)) for r in ok_items]
        all_durs = [float(r.get("duration_s", 0.0)) for r in ok_items]

        arms_out[arm] = {
            "total_runs": len(items),
            "ok_runs": len(ok_items),
            "non_ok": {
                status: sum(1 for r in items if r.get("status") == status)
                for status in sorted({str(r.get("status")) for r in items if r.get("status") != "ok"})
            },
            "full_pass_rate": _mean(all_full),
            "hidden_pass_rate": _mean(all_h_rates),
            "hidden_pass_stdev": _stdev(all_h_rates),
            "starter_pass_rate": _mean(all_starter),
            "mean_tokens": _mean(all_toks),
            "mean_subagents": _mean(all_subs),
            "mean_duration_s": _mean(all_durs),
            "enforced_runs": sum(1 for r in ok_items if r.get("enforced")),
            "cases": case_rows,
        }
    return {"arms": arms_out}


def render_summary(summary: dict[str, Any]) -> str:
    lines: list[str] = []
    for arm, data in summary["arms"].items():
        non_ok = data["non_ok"]
        status_note = f", non-ok={non_ok}" if non_ok else ""
        lines.append(
            f"=== {arm} ({data['ok_runs']}/{data['total_runs']} ok{status_note}, enforced={data['enforced_runs']}) ==="
        )
        lines.append(
            f"{'case':<18} {'runs':>4} {'full':>7} {'hidden':>8} {'starter':>8} {'subag':>6} {'tokens':>8} {'time':>7}"
        )
        for cid, row in data["cases"].items():
            lines.append(
                f"{cid:<18} {row['runs']:>4} "
                f"{row['full_pass_rate'] * 100:>6.0f}% "
                f"{row['hidden_pass_rate'] * 100:>7.1f}% "
                f"{row['starter_pass_rate'] * 100:>7.0f}% "
                f"{row['mean_subagents']:>6.1f} "
                f"{_compact(row['mean_tokens']):>8} "
                f"{row['mean_duration_s']:>6.0f}s"
            )
        lines.append(
            f"{'TOTAL/MEAN':<18} {data['ok_runs']:>4} "
            f"{data['full_pass_rate'] * 100:>6.1f}% "
            f"{data['hidden_pass_rate'] * 100:>7.1f}% "
            f"{data['starter_pass_rate'] * 100:>7.1f}% "
            f"{data['mean_subagents']:>6.1f} "
            f"{_compact(data['mean_tokens']):>8} "
            f"{data['mean_duration_s']:>6.0f}s"
        )
        lines.append("")
    return "\n".join(lines).rstrip()


def _cmd_list(args: argparse.Namespace, out: TextIO) -> int:
    rows = [{"case": cid, "path": str(args.cases_dir / cid)} for cid in case_ids(args.cases_dir)]
    if args.json:
        print(json.dumps(rows, indent=2), file=out)
    else:
        for row in rows:
            print(row["case"], file=out)
    return 0


def _cmd_prepare(args: argparse.Namespace, out: TextIO) -> int:
    case = load_case(args.case, args.cases_dir)
    dest = prepare_case(case, args.dest)
    print(f"Prepared build case '{case.id}' in {dest}", file=out)
    return 0


def _cmd_grade(args: argparse.Namespace, out: TextIO) -> int:
    case = load_case(args.case, args.cases_dir)
    result = grade_workdir(case, args.workdir)
    if args.json:
        print(json.dumps(result, indent=2), file=out)
    else:
        print(
            f"{case.id}: all_passed={result['all_passed']} "
            f"starter={result['starter']['passed']}/{result['starter']['ran']} "
            f"hidden={result['hidden']['passed']}/{result['hidden']['ran']}",
            file=out,
        )
    return 0 if result["all_passed"] else 1


def _cmd_summary(args: argparse.Namespace, out: TextIO) -> int:
    records = load_jsonl(args.files)
    summary = summarize_records(records)
    print(json.dumps(summary, indent=2) if args.json else render_summary(summary), file=out)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="build_bench.py", description="Build-and-verify coding benchmark harness.")
    parser.add_argument("--cases-dir", type=Path, default=BUILD_CASES_DIR, help="directory holding build cases")
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="list available build-and-verify cases")
    p_list.add_argument("--json", action="store_true")

    p_prep = sub.add_parser("prepare", help="create a starter repository without hidden tests")
    p_prep.add_argument("case")
    p_prep.add_argument("dest", type=Path)

    p_grade = sub.add_parser("grade", help="run starter and hidden tests against a repository")
    p_grade.add_argument("case")
    p_grade.add_argument("workdir", type=Path)
    p_grade.add_argument("--json", action="store_true")

    p_sum = sub.add_parser("summary", help="summarize JSONL benchmark results")
    p_sum.add_argument("files", nargs="+", type=Path)
    p_sum.add_argument("--json", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None, out: TextIO | None = None) -> int:
    args = build_parser().parse_args(argv)
    stream = out if out is not None else sys.stdout
    handlers = {
        "list": _cmd_list,
        "prepare": _cmd_prepare,
        "grade": _cmd_grade,
        "summary": _cmd_summary,
    }
    try:
        return handlers[args.command](args, stream)
    except BuildBenchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from evals.build_bench import main as _main

    sys.exit(_main())
