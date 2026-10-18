#!/usr/bin/env python3
"""Stop lifecycle hook and CLI state helper for the `/craftsman` Hybrid Hard-Loop.

Enforces Zero-Trust Quality Gate verification via `transcript.jsonl`:
- Tracks `base_sha` (cumulative diff from start of task) and `started_at_step`.
- Auto-detects `/craftsman` or `ultracode` invocations in `transcript.jsonl` even if
  the main agent forgot to run `--init`.
- Physically blocks `--complete` and `Stop` (`{"decision": "continue"}`) unless the
  main agent actually spawned an independent Quality Gate subagent via `invoke_subagent`
  (`Role: "Craftsman Quality Gate Auditor"`) in `transcript.jsonl` to execute
  `/simplify`, `/code-review`, `/audit`, and `/verify` on the cumulative diff.
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
QUALITY_GATE_ROLE_MARKERS = (
    "quality gate",
    "auditor",
    "audytor",
    "bramka jakości",
    "bramki jakości",
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


def resolve_transcript_for_state_file(
    state_file: Path, payload: dict[str, Any] | None = None
) -> Path | None:
    candidates: list[Path] = []
    if payload:
        raw_t = payload.get("transcriptPath")
        if isinstance(raw_t, str) and raw_t:
            candidates.append(Path(raw_t))
        raw_a = payload.get("artifactDirectoryPath")
        if isinstance(raw_a, str) and raw_a:
            candidates.append(Path(raw_a) / ".system_generated" / "logs" / "transcript.jsonl")

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
    base_sha: str | None = None,
    started_at_step: int = 0,
) -> dict[str, Any]:
    return {
        "active": True,
        "mode": mode,
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
        sys.stderr.write(f"[craftsman-stop-gate] Warning: failed to persist state to {state_file}: {exc}\n")


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
            return "ask_question" in names
    return False


def detect_untracked_craftsman_trigger(
    entries: list[dict[str, Any]], after_step: int
) -> int | None:
    """If the user triggered `/craftsman` or `ultracode` in USER_INPUT after `after_step`, return its step_index."""
    found_step: int | None = None
    for idx, entry in enumerate(entries):
        step_idx = _safe_int(entry.get("step_index"), idx)
        if step_idx <= after_step:
            continue
        if entry.get("type") == "USER_INPUT":
            content = str(entry.get("content") or "")
            if CRAFTSMAN_TRIGGER_RE.search(content):
                found_step = step_idx
    return found_step


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
    """Return True if an `invoke_subagent` call for the Quality Gate Auditor occurred at or after `since_step`."""
    for idx, entry in enumerate(entries):
        step_idx = _safe_int(entry.get("step_index"), idx)
        if step_idx < since_step:
            continue
        tool_calls = entry.get("tool_calls")
        if not isinstance(tool_calls, list):
            continue
        for tc in tool_calls:
            if not isinstance(tc, dict) or tc.get("name") != "invoke_subagent":
                continue
            args = tc.get("args")
            if not isinstance(args, dict):
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
        return "allow", None

    state_file = Path(artifact_dir).expanduser() / "craftsman_state.json"
    transcript_path = resolve_transcript_for_state_file(state_file, payload)
    entries = read_transcript_entries(transcript_path)

    state = load_state(state_file)
    completed_at = _safe_int(state.get("completed_at_step"), -1) if state else -1

    # Auto-detect `/craftsman` or `ultracode` from transcript even if agent forgot `--init`
    if (not state or not state.get("active", False)) and entries:
        trigger_step = detect_untracked_craftsman_trigger(entries, completed_at)
        if trigger_step is not None:
            workspaces = payload.get("workspacePaths")
            ws_cwd = workspaces[0] if isinstance(workspaces, list) and workspaces else None
            state = default_state(
                mode="craftsman",
                base_sha=get_head_sha(ws_cwd),
                started_at_step=trigger_step,
            )
            _try_save_state(state_file, state)

    if not state or not state.get("active", False):
        return "allow", None

    if state.get("awaiting_user", False) or is_waiting_on_ask_question_in_entries(entries):
        return "allow", None

    since_step = _safe_int(state.get("started_at_step"), 0)
    subagent_ok = bool(state.get("subagent_verified", False))
    if entries:
        subagent_ok = has_quality_gate_subagent_in_entries(entries, since_step)
        state["subagent_verified"] = subagent_ok

    gates = state.get("gates")
    if not isinstance(gates, dict):
        gates = {}

    missing = [gate for gate in REQUIRED_GATES if not bool(gates.get(gate, False))]
    if entries and not subagent_ok and "invoke_subagent(Craftsman Quality Gate Auditor)" not in missing:
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
    return (
        "continue",
        f"[CRAFTSMAN HARD-LOOP GATE ({forced + 1}/{max_cont})] "
        f"Nie możesz zakończyć zadania przed domknięciem Bramki Jakości (Etap 1). "
        f"Brakujące wymagania: [{missing_str}]. "
        f"Zasada Twórca != Recenzent: NIE WOLNO robić przeglądu tylko inline w głównym wątku! "
        f"Musisz wywołać narzędzie 'invoke_subagent' (TypeName: 'self', Role: 'Craftsman Quality Gate Auditor'), "
        f"aby niezależny subagent wczytał przez view_file i wykonał /simplify, /code-review oraz /verify "
        f"na CAŁYM skumulowanym diffie od początku zadania (git diff {base_sha}), a następnie "
        f"uruchomić craftsman-stop-gate.py --state-file {state_file} --complete.",
    )


def run_cli(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Manage craftsman_state.json for Hard-Loop gating.")
    parser.add_argument("--state-file", required=True, help="Path to craftsman_state.json")
    parser.add_argument("--init", action="store_true", help="Initialize a fresh active state")
    parser.add_argument("--cwd", help="Repository working directory to capture base_sha")
    parser.add_argument("--base-sha", help="Explicit base commit SHA for cumulative diff")
    parser.add_argument("--mode", default="craftsman", help="Mode name (craftsman or ultracode)")
    parser.add_argument("--stage", help="Set current stage name")
    parser.add_argument("--await-user", action="store_true", help="Pause hard-loop while waiting for user input")
    parser.add_argument("--resume", action="store_true", help="Resume hard-loop after user input")
    parser.add_argument("--pass-simplify", action="store_true", help="Mark 'simplified' gate as true")
    parser.add_argument("--pass-review", action="store_true", help="Mark 'reviewed' gate as true")
    parser.add_argument("--pass-verify", action="store_true", help="Mark 'verified' gate as true")
    parser.add_argument("--complete", action="store_true", help="Verify subagent execution and deactivate loop")
    parser.add_argument(
        "--allow-inline",
        action="store_true",
        help="Allow completion without transcript invoke_subagent check (for isolated unit tests)",
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
        else:
            detected = detect_untracked_craftsman_trigger(entries, -1)
            start_step = detected if detected is not None else 0
        state = default_state(
            mode=args.mode,
            base_sha=base_sha,
            started_at_step=start_step,
        )

    gates = state.setdefault("gates", {"simplified": False, "reviewed": False, "verified": False})
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
        if entries and not args.allow_inline:
            if not has_quality_gate_subagent_in_entries(entries, since_step):
                sys.stderr.write(
                    "[CRAFTSMAN HARD-LOOP REJECTED] Nie można zamknąć bramki (--complete): "
                    "w transcript.jsonl brak wywołania niezależnego subagenta 'invoke_subagent' "
                    "(TypeName: 'self', Role: 'Craftsman Quality Gate Auditor') od kroku startowego! "
                    "Najpierw powołaj subagenta przez invoke_subagent, który wczyta i wykona "
                    "/simplify, /code-review oraz /verify na całym skumulowanym diffie.\n"
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

    if not isinstance(payload, dict):
        emit_stop_decision("allow")
        return 0

    decision, reason = evaluate_stop(payload)
    emit_stop_decision(decision, reason)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
