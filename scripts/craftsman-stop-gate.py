#!/usr/bin/env python3
"""Stop lifecycle hook and CLI state helper for the `/craftsman` 3-Gear Hybrid Hard-Loop.

Supports both Google Antigravity and Claude Code lifecycle hooks:
- Gear 1 (`quick` / Soft-Loop): Activated via `--tier quick` (`/craftsman --quick`) or
  automatically (`--tier auto`) when the cumulative git diff (`base_sha..working_tree`)
  is <= 40 lines across <= 2 files. Allows inline verification without spawning a subagent.
- Gear 2 (`standard` / Hybrid Hard-Loop): Activated automatically when the cumulative diff
  exceeds 40 lines or 2 files (or when `--tier standard` is set). Enforces deterministic
  verification in `transcript.jsonl` that an isolated subagent (`Craftsman Quality Gate
  Auditor`) was invoked via `invoke_subagent`.
- Gear 3 (`deep` / Full-Audit Hard-Loop): Activated via `--tier deep` (`/craftsman --deep`
  or keyword `ultracode`). Always requires the isolated Quality Gate Auditor subagent
  regardless of diff size.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

REQUIRED_GATES = ("simplified", "reviewed", "verified")
DEFAULT_MAX_CONTINUATIONS = 3
SMALL_DIFF_MAX_LINES = 40
SMALL_DIFF_MAX_FILES = 2
VALID_TIERS = ("auto", "quick", "standard", "deep")

QUALITY_GATE_ROLE_MARKERS = (
    "quality gate",
    "auditor",
    "code reviewer",
)
QUALITY_GATE_SKILL_MARKERS = ("simplify", "code-review", "verify")
CRAFTSMAN_TRIGGER_RE = re.compile(
    r"(?i)(?:^|[\s\"'(\[`])/craftsman(?=$|[\s\"'\)\]`.,:;!?])|(?<![/\w-])ultracode(?![/\w.-])"
)


def get_head_sha(cwd: str | None = None) -> str | None:
    target = cwd if cwd and os.path.isdir(cwd) else None
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=target,
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        if proc.returncode == 0:
            return proc.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return None


def compute_cumulative_diff_stats(cwd: str | None, base_sha: str | None) -> tuple[int, int] | None:
    """Return (changed_files_count, total_changed_lines) for `git diff <base_sha>` plus untracked files in `cwd`."""
    if not cwd or not os.path.isdir(cwd):
        return None
    ref = base_sha if base_sha else "HEAD"
    try:
        proc = subprocess.run(
            ["git", "diff", "--numstat", ref],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        if proc.returncode != 0:
            return None
        files_count = 0
        total_lines = 0
        for line in proc.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            files_count += 1
            added = int(parts[0]) if parts[0].isdigit() else 0
            deleted = int(parts[1]) if parts[1].isdigit() else 0
            total_lines += added + deleted

        untracked_proc = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        if untracked_proc.returncode == 0:
            cwd_path = Path(cwd)
            for rel_path in untracked_proc.stdout.splitlines():
                rel_path = rel_path.strip()
                if not rel_path:
                    continue
                files_count += 1
                fpath = cwd_path / rel_path
                if fpath.is_file():
                    try:
                        with fpath.open("rb") as f:
                            total_lines += sum(1 for _ in f)
                    except OSError:
                        pass

        return files_count, total_lines
    except (OSError, subprocess.SubprocessError):
        return None


def requires_subagent_for_state(state: dict[str, Any], fallback_cwd: str | None = None) -> bool:
    """Determine whether the current task requires an isolated Quality Gate subagent."""
    tier = str(state.get("tier") or "auto").strip().lower()
    mode = str(state.get("mode") or "craftsman").strip().lower()

    if mode == "ultracode" or tier in ("deep", "standard"):
        return True
    if tier == "quick":
        return False

    # tier == "auto": measure cumulative diff in git if cwd is available
    cwd = state.get("cwd") or fallback_cwd
    base_sha = state.get("base_sha")
    stats = compute_cumulative_diff_stats(cwd if isinstance(cwd, str) else None, base_sha)
    if stats is not None:
        num_files, total_lines = stats
        if 1 <= num_files <= SMALL_DIFF_MAX_FILES and total_lines <= SMALL_DIFF_MAX_LINES:
            return False
    return True


def resolve_transcript_for_state_file(
    state_file: Path, payload: dict[str, Any] | None = None
) -> Path | None:
    candidates: list[Path] = []
    if payload:
        for key in ("transcriptPath", "transcript_path"):
            raw_t = payload.get(key)
            if isinstance(raw_t, str) and raw_t:
                candidates.append(Path(raw_t).expanduser())
        raw_a = payload.get("artifactDirectoryPath")
        if isinstance(raw_a, str) and raw_a:
            candidates.append(
                Path(raw_a).expanduser() / ".system_generated" / "logs" / "transcript.jsonl"
            )

    candidates.append(state_file.parent / ".system_generated" / "logs" / "transcript.jsonl")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def read_transcript_entries(transcript_path: Path | None) -> list[dict[str, Any]]:
    if transcript_path is None or not transcript_path.is_file():
        return []
    entries: list[dict[str, Any]] = []
    try:
        with transcript_path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    if isinstance(obj, dict):
                        entries.append(obj)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return entries


def _safe_int(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def get_latest_step_index(entries: list[dict[str, Any]]) -> int:
    for entry in reversed(entries):
        idx = entry.get("step_index")
        if isinstance(idx, int) and not isinstance(idx, bool):
            return idx
    return len(entries)


def default_state(
    mode: str = "craftsman",
    tier: str = "auto",
    cwd: str | None = None,
    base_sha: str | None = None,
    started_at_step: int = 0,
) -> dict[str, Any]:
    return {
        "active": True,
        "mode": mode,
        "tier": tier if tier in VALID_TIERS else "auto",
        "cwd": cwd,
        "stage": "implement",
        "base_sha": base_sha,
        "started_at_step": started_at_step,
        "completed_at_step": -1,
        "awaiting_user": False,
        "subagent_verified": False,
        "gates": {
            "simplified": False,
            "reviewed": False,
            "verified": False,
        },
        "forced_continuations": 0,
        "max_continuations": DEFAULT_MAX_CONTINUATIONS,
    }


def load_state(state_file: Path) -> dict[str, Any] | None:
    if not state_file.is_file():
        return None
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def save_state_atomic(state_file: Path, state: dict[str, Any]) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=".craftsman_state_", suffix=".tmp", dir=str(state_file.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp_path, state_file)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _try_save_state(state_file: Path, state: dict[str, Any]) -> None:
    try:
        save_state_atomic(state_file, state)
    except OSError as exc:
        sys.stderr.write(
            f"[craftsman-stop-gate] Warning: failed to persist state to {state_file}: {exc}\n"
        )


def emit_stop_decision(decision: str, reason: str | None = None) -> None:
    payload: dict[str, Any] = {"decision": decision}
    if reason:
        payload["reason"] = reason
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def is_waiting_on_ask_question_in_entries(entries: list[dict[str, Any]]) -> bool:
    for entry in reversed(entries):
        step_type = entry.get("type")
        if step_type == "USER_INPUT":
            return False
        if step_type == "PLANNER_RESPONSE":
            tool_calls = entry.get("tool_calls")
            if not isinstance(tool_calls, list) or not tool_calls:
                return False
            names = [tc.get("name") for tc in tool_calls if isinstance(tc, dict)]
            return any(n in ("ask_question", "AskUserQuestion") for n in names)
    return False


def detect_untracked_craftsman_trigger(
    entries: list[dict[str, Any]], after_step: int
) -> tuple[int, str] | None:
    """If user triggered `/craftsman` or `ultracode` after `after_step`, return (step_index, tier)."""
    found: tuple[int, str] | None = None
    for idx, entry in enumerate(entries):
        step_idx = _safe_int(entry.get("step_index"), idx)
        if step_idx <= after_step:
            continue
        if entry.get("type") == "USER_INPUT":
            content = str(entry.get("content") or "")
            if CRAFTSMAN_TRIGGER_RE.search(content):
                lower = content.lower()
                if "ultracode" in lower or "--deep" in lower:
                    tier = "deep"
                elif "--quick" in lower or "--soft" in lower:
                    tier = "quick"
                elif "--standard" in lower:
                    tier = "standard"
                else:
                    tier = "auto"
                found = (step_idx, tier)
    return found


def _is_quality_gate_subagent_spec(sub: dict[str, Any]) -> bool:
    sub_type = str(sub.get("TypeName") or "self").strip().lower()
    if sub_type != "self":
        return False
    role = str(sub.get("Role") or "").lower()
    prompt = str(sub.get("Prompt") or "").lower()
    if any(marker in role for marker in QUALITY_GATE_ROLE_MARKERS):
        return True
    return all(skill in prompt for skill in QUALITY_GATE_SKILL_MARKERS)


def has_quality_gate_subagent_in_entries(
    entries: list[dict[str, Any]], since_step: int
) -> bool:
    """Return True if an `invoke_subagent` or Claude Code `Task` call for the Quality Gate Auditor occurred at or after `since_step`."""
    for idx, entry in enumerate(entries):
        step_idx = _safe_int(entry.get("step_index"), idx)
        if step_idx < since_step:
            continue
        tool_calls = entry.get("tool_calls")
        if not isinstance(tool_calls, list):
            continue
        for tc in tool_calls:
            if not isinstance(tc, dict):
                continue
            tc_name = tc.get("name")
            if tc_name not in ("invoke_subagent", "Task"):
                continue
            args = tc.get("args") or tc.get("input")
            if not isinstance(args, dict):
                continue

            if tc_name == "Task":
                sub_type = str(args.get("subagent_type") or "general-purpose").strip().lower()
                if sub_type in ("explore", "research"):
                    continue
                combined = f"{args.get('description', '')} {args.get('prompt', '')}".lower()
                if any(marker in combined for marker in QUALITY_GATE_ROLE_MARKERS) or all(
                    skill in combined for skill in QUALITY_GATE_SKILL_MARKERS
                ):
                    return True
                continue

            raw_subagents = args.get("Subagents")
            parsed_subagents: list[Any] | None = None
            if isinstance(raw_subagents, list):
                parsed_subagents = raw_subagents
            elif isinstance(raw_subagents, str):
                try:
                    loaded = json.loads(raw_subagents)
                    if isinstance(loaded, list):
                        parsed_subagents = loaded
                except json.JSONDecodeError:
                    parsed_subagents = None

            if parsed_subagents is not None:
                if any(
                    isinstance(sub, dict) and _is_quality_gate_subagent_spec(sub)
                    for sub in parsed_subagents
                ):
                    return True
                continue

            # Fallback for truncated transcript lines where Subagents JSON string was cut off
            args_dump = json.dumps(args, ensure_ascii=False).lower()
            if '"typename": "research"' in args_dump or '"typename":"research"' in args_dump:
                continue
            if any(marker in args_dump for marker in QUALITY_GATE_ROLE_MARKERS) or all(
                skill in args_dump for skill in QUALITY_GATE_SKILL_MARKERS
            ):
                return True
    return False


def evaluate_stop(payload: dict[str, Any]) -> tuple[str, str | None]:
    termination_reason = payload.get("terminationReason", "NO_TOOL_CALL")
    if termination_reason != "NO_TOOL_CALL":
        return "allow", None

    if payload.get("fullyIdle") is False:
        return "allow", None

    artifact_dir = payload.get("artifactDirectoryPath")
    if not isinstance(artifact_dir, str) or not artifact_dir.strip():
        raw_tp = payload.get("transcript_path")
        if isinstance(raw_tp, str) and raw_tp.strip():
            artifact_dir = str(Path(raw_tp).expanduser().parent)
        else:
            return "allow", None

    workspaces = payload.get("workspacePaths")
    ws_cwd = (
        workspaces[0]
        if isinstance(workspaces, list) and workspaces
        else payload.get("cwd")
    )
    ws_cwd_str = ws_cwd if isinstance(ws_cwd, str) else None

    state_file = Path(artifact_dir).expanduser() / "craftsman_state.json"
    transcript_path = resolve_transcript_for_state_file(state_file, payload)
    entries = read_transcript_entries(transcript_path)

    state = load_state(state_file)
    completed_at = _safe_int(state.get("completed_at_step"), -1) if state else -1

    # Auto-detect `/craftsman` or `ultracode` from transcript even if agent forgot `--init`
    if (not state or not state.get("active", False)) and entries:
        trigger_info = detect_untracked_craftsman_trigger(entries, completed_at)
        if trigger_info is not None:
            trigger_step, detected_tier = trigger_info
            state = default_state(
                mode="ultracode" if detected_tier == "deep" else "craftsman",
                tier=detected_tier,
                cwd=ws_cwd_str,
                base_sha=get_head_sha(ws_cwd_str),
                started_at_step=trigger_step,
            )
            _try_save_state(state_file, state)

    if not state or not state.get("active", False):
        return "allow", None

    if state.get("awaiting_user", False) or is_waiting_on_ask_question_in_entries(entries):
        return "allow", None

    since_step = _safe_int(state.get("started_at_step"), 0)
    needs_subagent = requires_subagent_for_state(state, ws_cwd_str)
    subagent_ok = bool(state.get("subagent_verified", False))
    if entries and needs_subagent:
        subagent_ok = has_quality_gate_subagent_in_entries(entries, since_step)
        state["subagent_verified"] = subagent_ok

    gates = state.get("gates")
    if not isinstance(gates, dict):
        gates = {}

    missing = [gate for gate in REQUIRED_GATES if not bool(gates.get(gate, False))]
    if (
        entries
        and needs_subagent
        and not subagent_ok
        and "invoke_subagent(Craftsman Quality Gate Auditor)" not in missing
    ):
        missing.insert(0, "invoke_subagent(Craftsman Quality Gate Auditor)")

    if not missing:
        state["active"] = False
        state["stage"] = "completed"
        state["completed_at_step"] = get_latest_step_index(entries)
        _try_save_state(state_file, state)
        return "allow", None

    forced = _safe_int(state.get("forced_continuations"), 0)
    max_cont = max(1, _safe_int(state.get("max_continuations"), DEFAULT_MAX_CONTINUATIONS))

    if forced >= max_cont:
        state["active"] = False
        state["stage"] = "aborted_max_continuations"
        state["completed_at_step"] = get_latest_step_index(entries)
        _try_save_state(state_file, state)
        return "allow", None

    state["forced_continuations"] = forced + 1
    _try_save_state(state_file, state)

    base_sha = state.get("base_sha") or "HEAD~1"
    missing_str = ", ".join(missing)
    if needs_subagent:
        return (
            "continue",
            f"[CRAFTSMAN HARD-LOOP GATE ({forced + 1}/{max_cont})] "
            f"Task completion blocked until the Stage 1 Quality Gate is satisfied. "
            f"Missing requirements: [{missing_str}]. "
            f"Hybrid Hard-Loop Context Isolation: inline review in the main thread is insufficient. "
            f"You must invoke 'invoke_subagent' (TypeName: 'self', Role: 'Craftsman Quality Gate Auditor') "
            f"so an isolated subagent executes /simplify, /code-review, and /verify "
            f"across the full cumulative diff (git diff {base_sha}), and then run "
            f"craftsman-stop-gate.py --state-file {state_file} --complete.",
        )
    return (
        "continue",
        f"[CRAFTSMAN SOFT-LOOP GATE ({forced + 1}/{max_cont})] "
        f"Missing verification steps for Quick Mode: [{missing_str}]. "
        f"Execute /simplify, /code-review, and /verify, then close the gate via "
        f"craftsman-stop-gate.py --state-file {state_file} --complete.",
    )


def run_cli(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Manage craftsman_state.json for 3-Gear Hard-Loop gating.")
    parser.add_argument("--state-file", required=True, help="Path to craftsman_state.json")
    parser.add_argument("--init", action="store_true", help="Initialize a fresh active state")
    parser.add_argument("--cwd", help="Repository working directory to capture base_sha and diff stats")
    parser.add_argument("--base-sha", help="Explicit base commit SHA for cumulative diff")
    parser.add_argument("--mode", default="craftsman", help="Mode name (craftsman or ultracode)")
    parser.add_argument(
        "--tier",
        choices=VALID_TIERS,
        help="Gearbox tier: auto (default: <=40 lines & <=2 files -> quick, else standard), quick, standard, or deep",
    )
    parser.add_argument("--stage", help="Set current stage name")
    parser.add_argument("--await-user", action="store_true", help="Pause hard-loop while waiting for user input")
    parser.add_argument("--resume", action="store_true", help="Resume hard-loop after user input")
    parser.add_argument("--pass-simplify", action="store_true", help="Mark 'simplified' gate as true")
    parser.add_argument("--pass-review", action="store_true", help="Mark 'reviewed' gate as true")
    parser.add_argument("--pass-verify", action="store_true", help="Mark 'verified' gate as true")
    parser.add_argument("--complete", action="store_true", help="Verify quality gate execution and deactivate loop")
    parser.add_argument(
        "--allow-inline",
        action="store_true",
        help="Allow completion without transcript invoke_subagent check (Quick Soft-Loop / unit tests)",
    )
    args = parser.parse_args(argv)

    state_file = Path(args.state_file).expanduser()
    transcript_path = resolve_transcript_for_state_file(state_file)
    entries = read_transcript_entries(transcript_path)
    latest_step = get_latest_step_index(entries)

    state = load_state(state_file)
    if args.init or state is None:
        base_sha = args.base_sha or get_head_sha(args.cwd)
        if args.init:
            start_step = latest_step
            initial_tier = args.tier or ("deep" if args.mode == "ultracode" else "auto")
        else:
            detected = detect_untracked_craftsman_trigger(entries, -1)
            start_step = detected[0] if detected is not None else 0
            initial_tier = args.tier or (detected[1] if detected is not None else "auto")
        state = default_state(
            mode=args.mode,
            tier=initial_tier,
            cwd=args.cwd,
            base_sha=base_sha,
            started_at_step=start_step,
        )

    gates = state.setdefault("gates", {"simplified": False, "reviewed": False, "verified": False})
    if args.tier:
        state["tier"] = args.tier
    if args.cwd:
        state["cwd"] = args.cwd
    if args.stage:
        state["stage"] = args.stage
    if args.base_sha:
        state["base_sha"] = args.base_sha
    if args.await_user:
        state["awaiting_user"] = True
    if args.resume:
        state["awaiting_user"] = False
        state["active"] = True
    if args.pass_simplify:
        gates["simplified"] = True
    if args.pass_review:
        gates["reviewed"] = True
    if args.pass_verify:
        gates["verified"] = True

    if args.complete:
        since_step = _safe_int(state.get("started_at_step"), 0)
        needs_subagent = requires_subagent_for_state(state, args.cwd)
        if entries and needs_subagent and not args.allow_inline:
            if not has_quality_gate_subagent_in_entries(entries, since_step):
                sys.stderr.write(
                    "[CRAFTSMAN HARD-LOOP REJECTED] Cannot close quality gate (--complete): "
                    "transcript.jsonl does not record an isolated 'invoke_subagent' call "
                    "(TypeName: 'self', Role: 'Craftsman Quality Gate Auditor') since task start. "
                    "For changes exceeding Quick Mode thresholds (>40 lines or >2 files), spawn "
                    "the auditor subagent via invoke_subagent to execute /simplify, /code-review, and /verify.\n"
                )
                return 1
        state["subagent_verified"] = True
        gates["simplified"] = True
        gates["reviewed"] = True
        gates["verified"] = True
        state["awaiting_user"] = False
        state["active"] = False
        state["stage"] = "completed"
        state["completed_at_step"] = latest_step

    save_state_atomic(state_file, state)
    return 0


def main() -> int:
    if len(sys.argv) > 1:
        return run_cli(sys.argv[1:])

    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except (OSError, json.JSONDecodeError):
        emit_stop_decision("allow")
        return 0

    decision, reason = evaluate_stop(payload)
    emit_stop_decision(decision, reason)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
