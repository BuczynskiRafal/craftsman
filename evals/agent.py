"""Real end-to-end review runs: one headless Claude Code session per case.

``python3 -m evals.run agent`` prepares a case in a fresh temp directory, loads a Craftsman checkout
into a headless ``claude -p`` session, lets it review the change, scores the ledger with the same
scorer as ``run.py score`` and appends one JSON line per run to a results file. ``evals.compare``
summarizes such files. See ``evals/README.md`` for the A/B workflow, the permission model and the cost.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from collections import Counter
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

from evals import run

SCHEMA = 1
PLUGIN_DIRS = (".claude-plugin", "")  # where plugin.json can live, first match wins
# The review skill is craft-review since Craftsman 5; older checkouts have code-review. First found wins.
SKILL_CANDIDATES = ("craft-review", "code-review")
REVIEW_AGENTS = ("craftsman-reviewer", "craftsman-verifier")
BASE_REF = "HEAD"  # prepare_case commits the base, so the change is the uncommitted diff against HEAD
TASK_FILE = ".git/evals/task.md"
DEFAULT_PERMISSION_MODE = "dontAsk"
DEFAULT_MODEL = "sonnet"
DEFAULT_BUDGET_USD = 5.0
DEFAULT_TIMEOUT_MIN = 30.0
DEFAULT_JOBS = 2

# Tools a review needs, and nothing else. Read, Grep and Glob are not listed: Claude Code allows them
# inside the working directory (the temp case dir) by itself and refuses them elsewhere in dontAsk mode.
ALLOWED_TOOLS = (
    "Skill",
    "Task",  # the subagent tool is called Task or Agent depending on the Claude Code version
    "Agent",
    "TodoWrite",
    "Bash(craftsman:*)",
    "Bash(git status:*)",
    "Bash(git diff:*)",
    "Bash(git log:*)",
    "Bash(git show:*)",
    "Bash(git grep:*)",
    "Bash(git ls-files:*)",
    "Bash(git ls-tree:*)",
    "Bash(git cat-file:*)",
    "Bash(git rev-parse:*)",
    "Bash(git rev-list:*)",
    "Bash(git merge-base:*)",
    "Bash(git blame:*)",
    "Bash(git branch:*)",
    "Bash(python3:*)",
    "Bash(python:*)",
    "Bash(cd:*)",
    "Bash(pwd)",
    "Bash(ls:*)",
    "Bash(wc:*)",
)
# Never available, in any arm: nothing to edit in a review-only run, nothing to look up on the web.
DENIED_TOOLS = ("Edit", "Write", "NotebookEdit", "WebFetch", "WebSearch")

# Statuses that mean the run did not measure the protocol; ``compare`` leaves them out of its means.
INVALID_STATUSES = ("load-failed", "error", "no-result", "harness-error")

COPY_IGNORE = shutil.ignore_patterns(
    ".git",
    ".claude",
    "evals",
    "docs",
    "tests",
    "__pycache__",
    "*.pyc",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    "*.egg-info",
    "build",
    "dist",
    "bench",
)


@dataclass(frozen=True)
class PluginInfo:
    """What the harness expects to see loaded from a Craftsman checkout."""

    name: str
    skill: str

    @property
    def qualified_skill(self) -> str:
        return f"{self.name}:{self.skill}"

    @property
    def agents(self) -> tuple[str, ...]:
        return tuple(f"{self.name}:{a}" for a in REVIEW_AGENTS)


@dataclass(frozen=True)
class AgentConfig:
    cases: tuple[run.Case, ...]
    src: Path | None  # None: baseline arm, no Craftsman
    info: PluginInfo | None
    arm: str
    model: str
    repeat: int
    jobs: int
    budget_usd: float
    timeout_min: float
    out: Path
    logs_dir: Path
    prompt: str | None
    claude: str
    permission_mode: str
    extra_tools: tuple[str, ...]
    keep_workdirs: bool
    src_rev: str = ""


# --- the Craftsman checkout under test ---------------------------------------------------------


def detect_plugin(src: Path) -> PluginInfo:
    """Plugin name and review skill of a Craftsman checkout."""
    if not src.is_dir():
        raise run.EvalError(f"--craftsman-src {src} is not a directory")
    name = ""
    for sub in PLUGIN_DIRS:
        manifest = src / sub / "plugin.json" if sub else src / "plugin.json"
        if manifest.is_file():
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise run.EvalError(f"{manifest} is not valid JSON: {exc}") from exc
            if isinstance(data, dict) and isinstance(data.get("name"), str) and data["name"]:
                name = data["name"]
                break
    if not name:
        raise run.EvalError(f"{src} has no plugin.json with a name (looked in .claude-plugin/ and the root)")
    for skill in SKILL_CANDIDATES:
        if (src / "skills" / skill / "SKILL.md").is_file():
            return PluginInfo(name, skill)
    raise run.EvalError(f"{src} has none of the review skills: {', '.join(SKILL_CANDIDATES)}")


def source_revision(src: Path) -> str:
    """Short commit of the checkout, with '+dirty' when it has uncommitted changes; '' if unknown."""
    env = _clean_env()
    try:
        rev = subprocess.run(
            ["git", "-C", str(src), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            env=env,
        ).stdout.strip()
        dirty = subprocess.run(
            # only what the snapshot copies counts: edits to evals/, docs/ and tests/ do not change the arm
            [
                "git",
                "-C",
                str(src),
                "status",
                "--porcelain",
                "--untracked-files=no",
                "--",
                ".",
                ":!evals",
                ":!docs",
                ":!tests",
            ],
            capture_output=True,
            text=True,
            check=True,
            env=env,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return ""
    return f"{rev}+dirty" if dirty else rev


def snapshot_source(src: Path, dest: Path) -> Path:
    """Copy the plugin files of a checkout, so the arm is immune to edits during a long run and the
    reviewed session cannot find ``evals/cases/*/truth.json`` next to the plugin."""
    shutil.copytree(src, dest, ignore=COPY_IGNORE, symlinks=True)
    return dest


# --- command line of one run -------------------------------------------------------------------


def default_prompt(info: PluginInfo) -> str:
    return (
        f"/{info.qualified_skill} review the changes against {BASE_REF} (review only, do not fix). "
        f"The change description is in {TASK_FILE}."
    )


def render_prompt(template: str, values: dict[str, str]) -> str:
    """Substitute {skill}, {base}, {task_file}, {case} in a prompt template; other braces stay."""
    text = template
    for key, value in values.items():
        text = text.replace("{" + key + "}", value)
    return text


def load_prompt_template(value: str) -> str:
    """``--prompt`` is a template, or the path of a file that holds one."""
    path = Path(value)
    try:
        if len(value) < 4096 and "\n" not in value and path.is_file():
            return path.read_text(encoding="utf-8").rstrip("\n")
    except OSError:
        pass
    return value


def allowed_tools(cfg: AgentConfig, workdir: Path) -> list[str]:
    tools = [*ALLOWED_TOOLS, *cfg.extra_tools]
    if cfg.src is None:  # baseline: the prompt asks for .git/evals/findings.json, allow that one file
        tools.append(f"Write(/{run.findings_path(workdir).as_posix()})")
    return tools


def denied_tools(cfg: AgentConfig) -> list[str]:
    return [t for t in DENIED_TOOLS if not (cfg.src is None and t == "Write")]


def build_command(cfg: AgentConfig, prompt: str, workdir: Path, plugin_dir: Path | None) -> list[str]:
    """The ``claude`` command line of a run (cwd is the prepared case)."""
    command = [
        cfg.claude,
        "-p",
        prompt,
        "--model",
        cfg.model,
        "--output-format",
        "stream-json",
        "--verbose",
        "--max-budget-usd",
        f"{cfg.budget_usd:g}",
        "--permission-mode",
        cfg.permission_mode,
        "--allowedTools",
        ",".join(allowed_tools(cfg, workdir)),
        "--disallowedTools",
        ",".join(denied_tools(cfg)),
        # Only the project's own settings and nothing from the user's profile: same session in
        # every arm, no user plugins, skills, MCP servers or memory.
        "--setting-sources",
        "project,local",
        "--strict-mcp-config",
    ]
    if plugin_dir is not None:
        command += ["--plugin-dir", str(plugin_dir)]
    return command


def write_wrapper(bin_dir: Path, src: Path) -> Path:
    """A ``craftsman`` executable that runs the CLI of the checkout under test."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    wrapper = bin_dir / "craftsman"
    wrapper.write_text(
        f'#!/bin/sh\nPYTHONPATH={shlex.quote(str(src))} exec python3 -m craftsman.cli "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    return wrapper


def _clean_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def run_env(bin_dir: Path | None) -> dict[str, str]:
    env = _clean_env()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["DISABLE_AUTOUPDATER"] = "1"
    if bin_dir is not None:
        env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    return env


# --- reading the stream ------------------------------------------------------------------------


@dataclass
class Stream:
    """What a run's ``stream-json`` output says. Fed line by line."""

    init: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    tool_calls: Counter[str] = field(default_factory=Counter)
    hooks: dict[str, dict[str, int]] = field(default_factory=dict)

    def feed(self, event: Any) -> None:
        if not isinstance(event, dict):
            return
        kind, sub = event.get("type"), event.get("subtype")
        if kind == "system" and sub == "init":
            self.init = event
        elif kind == "result":
            self.result = event
        elif kind == "assistant":
            content = event.get("message", {}).get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        self.tool_calls[str(block.get("name"))] += 1
        elif kind == "system" and sub == "hook_response":
            entry = self.hooks.setdefault(str(event.get("hook_event")), {"runs": 0, "failed": 0})
            entry["runs"] += 1
            if event.get("outcome") != "success":
                entry["failed"] += 1


def parse_output(text: str) -> Stream:
    """A Stream from the full output of ``claude -p``: JSON lines, one JSON object or a JSON list."""
    stream = Stream()
    try:
        whole = json.loads(text)
    except json.JSONDecodeError:
        for line in text.splitlines():
            try:
                stream.feed(json.loads(line))
            except json.JSONDecodeError:
                continue
        return stream
    for event in whole if isinstance(whole, list) else [whole]:
        stream.feed(event)
    return stream


def check_init(init: dict[str, Any], cfg: AgentConfig) -> list[str]:
    """Problems with the loaded plugin: what is missing (Craftsman arm) or present (baseline arm)."""
    skills = [str(s) for s in init.get("skills", [])] or [str(s) for s in init.get("slash_commands", [])]
    agents = [str(a) for a in init.get("agents", [])]
    if cfg.info is None:
        return [f"unexpected {s}" for s in [*skills, *agents] if s.startswith("craftsman:")]
    missing = []
    if cfg.info.qualified_skill not in skills:
        missing.append(f"skill {cfg.info.qualified_skill}")
    missing += [f"agent {a}" for a in cfg.info.agents if a not in agents]
    return missing


# --- running claude ----------------------------------------------------------------------------


@dataclass
class Execution:
    exit_code: int | None
    timed_out: bool
    init_problems: list[str]
    stream: Stream
    wall_s: float


def _kill(proc: subprocess.Popen[str]) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        proc.kill()


def execute(
    command: Sequence[str],
    cwd: Path,
    env: dict[str, str],
    timeout_s: float,
    stream_log: Path,
    stderr_log: Path,
    check: Callable[[dict[str, Any]], list[str]],
) -> Execution:
    """Run ``command``, log its stream (without the token-level stream_event lines), stop it at
    the timeout, and stop it as soon as the init event shows the plugin did not load."""
    stream = Stream()
    problems: list[str] = []
    started = time.monotonic()
    with stderr_log.open("w", encoding="utf-8") as err, stream_log.open("w", encoding="utf-8") as log:
        try:
            proc = subprocess.Popen(
                list(command),
                cwd=cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=err,
                text=True,
                start_new_session=True,
            )
        except OSError as exc:
            raise run.EvalError(f"cannot start {command[0]}: {exc}") from exc
        timer = threading.Timer(timeout_s, _kill, args=(proc,))
        timer.start()
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    log.write(line)
                    continue
                if isinstance(event, dict) and event.get("type") == "stream_event":
                    continue
                log.write(line if line.endswith("\n") else line + "\n")
                stream.feed(event)
                if event.get("type") == "system" and event.get("subtype") == "init" and not problems:
                    problems = check(event)
                    if problems:
                        _kill(proc)
                        break
            code = proc.wait()
        finally:
            timed_out = not timer.is_alive() and time.monotonic() - started >= timeout_s
            timer.cancel()
            if proc.poll() is None:
                _kill(proc)
                proc.wait()
            if proc.stdout is not None:
                proc.stdout.close()
    return Execution(code, timed_out, problems, stream, time.monotonic() - started)


# --- scoring and the result record -------------------------------------------------------------


def recall_details(case: run.Case, workdir: Path) -> dict[str, Any] | None:
    """The scorer's verdict on the findings in the work directory; None when there are none."""
    try:
        findings, dismissed, unreadable = run.collect_findings(workdir)
    except run.EvalError:
        return None
    return recall_of(case, findings, dismissed, unreadable)


def recall_of(
    case: run.Case, findings: Sequence[run.Finding], dismissed: int = 0, unreadable: int = 0
) -> dict[str, Any]:
    """The ``recall`` field of a result record for these findings."""
    score = run.score_findings(case, findings, dismissed, unreadable)
    return {
        "found": score["found"],
        "total": score["total"],
        "recall": score["recall"],
        "found_ids": [d["id"] for d in score["defects"] if d["found"]],
        "missed_ids": [d["id"] for d in score["defects"] if not d["found"]],
        "decoy_hits": score["decoy_hits"],
        "unmatched": score["unmatched"],
        "duplicates": score["duplicates"],
        "findings_used": score["findings_used"],
        "precision": score["precision"],
        "dismissed_ignored": score["dismissed_ignored"],
        "unreadable_ignored": score["unreadable_ignored"],
    }


def _status(execution: Execution) -> str:
    result = execution.stream.result
    if execution.init_problems:
        return "load-failed"
    if execution.timed_out:
        return "timeout"
    if result is None:
        return "no-result"
    subtype = str(result.get("subtype", ""))
    if subtype == "error_max_budget_usd":
        return "budget"
    if subtype == "error_max_turns":
        return "max-turns"
    if result.get("is_error") or (subtype and subtype != "success"):
        return "error"
    return "ok"


def _slim_usage(usage: Any) -> dict[str, Any]:
    keys = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    return {k: usage[k] for k in keys if isinstance(usage, dict) and k in usage}


def make_record(
    cfg: AgentConfig,
    case: run.Case,
    index: int,
    prompt: str,
    execution: Execution,
    recall: dict[str, Any] | None,
    logs: dict[str, str],
    started_at: str,
) -> dict[str, Any]:
    result = execution.stream.result or {}
    init = execution.stream.init or {}
    hooks = execution.stream.hooks or {}
    warnings: list[str] = []
    if cfg.info is not None and execution.stream.init is not None and not hooks:
        warnings.append("no hook ran: the isolation evidence of the gate was not recorded")
    if recall is None:
        warnings.append("no findings: neither a ledger nor a findings file exists")
    tool_calls = execution.stream.tool_calls
    return {
        "schema": SCHEMA,
        "arm": cfg.arm,
        "case": case.id,
        "run": index,
        "defects_total": len(case.defects),
        "started_at": started_at,
        "model": cfg.model,
        "craftsman_src": str(cfg.src) if cfg.src is not None else None,
        "src_rev": cfg.src_rev,
        "skill": cfg.info.qualified_skill if cfg.info is not None else None,
        "prompt": prompt,
        "status": _status(execution),
        "exit_code": execution.exit_code,
        "stop_reason": result.get("stop_reason"),
        "terminal_reason": result.get("terminal_reason"),
        "is_error": bool(result.get("is_error", False)),
        "recall": recall,
        "cost_usd": float(result.get("total_cost_usd") or 0.0),
        "duration_ms": int(result.get("duration_ms") or 0),
        "duration_api_ms": int(result.get("duration_api_ms") or 0),
        "wall_s": round(execution.wall_s, 2),
        "turns": int(result.get("num_turns") or 0),
        "subagents": result.get("subagent_stats") or {},
        "usage": _slim_usage(result.get("usage")),
        "model_usage": result.get("modelUsage") or {},
        "tool_calls": dict(tool_calls) if tool_calls else {},
        "hooks": hooks,
        "permission_denials": len(result.get("permission_denials") or []),
        "init": {
            "problems": execution.init_problems,
            "plugins": [p.get("name") for p in init.get("plugins", []) if isinstance(p, dict)],
            "skill_loaded": cfg.info is not None and cfg.info.qualified_skill in init.get("skills", []),
            "agents_loaded": [a for a in init.get("agents", []) if str(a).startswith("craftsman:")],
            "permission_mode": init.get("permissionMode"),
        },
        "result_text": str(result.get("result") or "")[:4000],
        "warnings": warnings,
        "logs": logs,
    }


def run_one(cfg: AgentConfig, case: run.Case, index: int, plugin_dir: Path | None) -> dict[str, Any]:
    """One review run of one case; always returns a record, never raises for a failed run."""
    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    log_dir = cfg.logs_dir / cfg.arm / f"{case.id}-{index}"
    log_dir.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix=f"evals-{case.id}-"))
    workdir = root / "repo"
    logs = {"dir": str(log_dir), "stream": str(log_dir / "stream.jsonl"), "stderr": str(log_dir / "stderr.txt")}
    prompt = ""
    try:
        run.prepare_case(case, workdir)
        bin_dir = None
        if cfg.src is not None and plugin_dir is not None:
            bin_dir = root / "bin"
            write_wrapper(bin_dir, plugin_dir)
        template = cfg.prompt
        if template is None:
            template = default_prompt(cfg.info) if cfg.info is not None else run.BASELINE_PROMPT
        values = {"base": BASE_REF, "task_file": TASK_FILE, "case": case.id}
        if cfg.info is not None:
            values["skill"] = cfg.info.qualified_skill
        prompt = render_prompt(template, values)
        command = build_command(cfg, prompt, workdir, plugin_dir)
        execution = execute(
            command,
            workdir,
            run_env(bin_dir),
            cfg.timeout_min * 60,
            log_dir / "stream.jsonl",
            log_dir / "stderr.txt",
            lambda init: check_init(init, cfg),
        )
        recall = recall_details(case, workdir)
        ledger = run.ledger_path(workdir)
        if ledger.is_file():
            shutil.copyfile(ledger, log_dir / "ledger.json")
            logs["ledger"] = str(log_dir / "ledger.json")
        if cfg.keep_workdirs:
            logs["workdir"] = str(workdir)
        return make_record(cfg, case, index, prompt, execution, recall, logs, started_at)
    except Exception as exc:  # a broken run must not take the other runs down
        record = make_record(
            cfg, case, index, prompt, Execution(None, False, [], Stream(), 0.0), None, logs, started_at
        )
        record["status"] = "harness-error"
        record["error"] = str(exc)
        return record
    finally:
        if not cfg.keep_workdirs:
            shutil.rmtree(root, ignore_errors=True)


# --- command line ------------------------------------------------------------------------------


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--cases", default="all", help="comma separated case ids, or 'all' (default)")
    parser.add_argument(
        "--craftsman-src",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="the Craftsman checkout to test (default: this repository)",
    )
    parser.add_argument("--arm", default="default", help="label of this configuration in the results")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--repeat", type=int, default=1, help="runs per case (default 1)")
    parser.add_argument("--jobs", type=int, default=DEFAULT_JOBS, help="parallel runs (default 2)")
    parser.add_argument("--budget-usd", type=float, default=DEFAULT_BUDGET_USD, help="cap per run (default 5)")
    parser.add_argument("--timeout-min", type=float, default=DEFAULT_TIMEOUT_MIN, help="per run (default 30)")
    parser.add_argument("--out", type=Path, help="results file, appended (default bench/evals/<arm>.jsonl)")
    parser.add_argument("--prompt", help="prompt template or the path of a file with one; {skill} {base} {task_file}")
    parser.add_argument(
        "--baseline",
        action="store_true",
        help="no Craftsman: no plugin, no craftsman command, the baseline prompt of evals/README.md",
    )
    parser.add_argument("--claude", default=os.environ.get("EVALS_CLAUDE", "claude"), help="the claude executable")
    parser.add_argument("--permission-mode", default=DEFAULT_PERMISSION_MODE)
    parser.add_argument("--allow-tool", action="append", default=[], help="one more --allowedTools rule")
    parser.add_argument("--keep-workdirs", action="store_true", help="keep the temp repositories")
    parser.add_argument("--dry-run", action="store_true", help="print the command of the first run and stop")


def resolve_cases(spec: str, cases_dir: Path) -> tuple[run.Case, ...]:
    ids = run.case_ids(cases_dir) if spec.strip() == "all" else [c for c in spec.replace(",", " ").split() if c]
    if not ids:
        raise run.EvalError("no cases selected")
    return tuple(run.load_case(case_id, cases_dir) for case_id in ids)


def build_config(args: argparse.Namespace) -> AgentConfig:
    if args.repeat < 1 or args.jobs < 1:
        raise run.EvalError("--repeat and --jobs must be at least 1")
    if args.budget_usd <= 0 or args.timeout_min <= 0:
        raise run.EvalError("--budget-usd and --timeout-min must be positive")
    claude = shutil.which(args.claude) or args.claude
    if shutil.which(args.claude) is None and not Path(args.claude).is_file():
        raise run.EvalError(f"cannot find the claude executable '{args.claude}' (use --claude)")
    info = None if args.baseline else detect_plugin(args.craftsman_src.resolve())
    src = None if args.baseline else args.craftsman_src.resolve()
    arm = args.arm
    out = args.out if args.out is not None else Path("bench") / "evals" / f"{arm}.jsonl"
    return AgentConfig(
        cases=resolve_cases(args.cases, args.cases_dir),
        src=src,
        info=info,
        arm=arm,
        model=args.model,
        repeat=args.repeat,
        jobs=args.jobs,
        budget_usd=args.budget_usd,
        timeout_min=args.timeout_min,
        out=out.resolve(),
        logs_dir=out.resolve().with_suffix(".logs"),
        prompt=load_prompt_template(args.prompt) if args.prompt else None,
        claude=claude,
        permission_mode=args.permission_mode,
        extra_tools=tuple(args.allow_tool),
        keep_workdirs=args.keep_workdirs,
        src_rev=source_revision(src) if src is not None else "",
    )


def summary_line(record: dict[str, Any]) -> str:
    recall = record["recall"]
    found = f"{recall['found']}/{recall['total']}" if recall else "no findings"
    return (
        f"{record['arm']} {record['case']}#{record['run']}: {record['status']}, recall {found}, "
        f"${record['cost_usd']:.2f}, {record['wall_s'] / 60:.1f} min, {record['turns']} turns"
    )


def used_run_indexes(out: Path, logs_dir: Path, arm: str, case_id: str) -> int:
    """The highest run index of `arm` and `case_id` already in the results file `out` or its logs.

    Results, log folders and kept ledgers are keyed by (arm, case, index), so a repeat added later
    must continue after it instead of replacing an earlier run.
    """
    highest = 0
    try:
        lines = out.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        run_index = record.get("run") if isinstance(record, dict) else None
        if (
            isinstance(record, dict)
            and record.get("arm") == arm
            and record.get("case") == case_id
            and isinstance(run_index, int)
        ):
            highest = max(highest, run_index)
    prefix = f"{case_id}-"
    try:
        for folder in (logs_dir / arm).iterdir():
            suffix = folder.name[len(prefix) :]
            if folder.name.startswith(prefix) and suffix.isdecimal():
                highest = max(highest, int(suffix))
    except OSError:
        pass
    return highest


def cmd_agent(args: argparse.Namespace, out: TextIO) -> int:
    cfg = build_config(args)
    jobs: list[tuple[run.Case, int]] = []
    for case in cfg.cases:
        first = used_run_indexes(cfg.out, cfg.logs_dir, cfg.arm, case.id) + 1
        jobs.extend((case, index) for index in range(first, first + cfg.repeat))
    if args.dry_run:
        case = cfg.cases[0]
        plugin = cfg.src
        template = cfg.prompt or (default_prompt(cfg.info) if cfg.info else run.BASELINE_PROMPT)
        values = {"base": BASE_REF, "task_file": TASK_FILE, "case": case.id}
        if cfg.info is not None:
            values["skill"] = cfg.info.qualified_skill
        placeholder = Path("<temp case dir>")
        command = build_command(cfg, render_prompt(template, values), placeholder, plugin)
        print(f"{len(jobs)} runs; first run, in a fresh temp copy of '{case.id}':", file=out)
        print(shlex.join(command), file=out)
        return 0
    cfg.out.parent.mkdir(parents=True, exist_ok=True)
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    snapshot_root: Path | None = None
    plugin_dir: Path | None = None
    if cfg.src is not None:
        snapshot_root = Path(tempfile.mkdtemp(prefix="evals-plugin-"))
        plugin_dir = snapshot_source(cfg.src, snapshot_root / "craftsman")
    lock = threading.Lock()
    failures = 0
    print(f"arm '{cfg.arm}': {len(jobs)} runs, {cfg.jobs} at a time, results in {cfg.out}", file=out)
    try:
        with ThreadPoolExecutor(max_workers=cfg.jobs) as pool:
            futures = [pool.submit(run_one, cfg, case, index, plugin_dir) for case, index in jobs]
            for future in as_completed(futures):
                record = future.result()
                with lock:
                    with cfg.out.open("a", encoding="utf-8") as fh:
                        fh.write(json.dumps(record, sort_keys=True) + "\n")
                    print(summary_line(record), file=out, flush=True)
                if record["status"] != "ok":
                    failures += 1
    finally:
        if snapshot_root is not None:
            shutil.rmtree(snapshot_root, ignore_errors=True)
    if failures:
        print(f"{failures} of {len(jobs)} runs did not end ok; see status and logs in {cfg.out}", file=out)
    return 1 if failures else 0
