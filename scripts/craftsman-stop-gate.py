#!/usr/bin/env python3
"""Stop lifecycle hook and CLI state helper for the `/craftsman` Hybrid Hard-Loop.

When invoked by Antigravity's `Stop` hook (JSON on stdin, no CLI arguments):
- Checks if `<artifactDirectoryPath>/craftsman_state.json` exists and has `"active": true`.
- Allows stopping immediately if:
  * `terminationReason != "NO_TOOL_CALL"` (e.g. error, user cancel, max invocations)
  * `fullyIdle is False` (background tasks/subagents are still running)
  * `craftsman_state.json` does not exist or `"active"` is `false`
  * `"awaiting_user"` is `true` (or the latest tool call in the transcript was `ask_question`)
  * All quality gates (`simplified`, `reviewed`, `verified`) are `true` (marks state completed)
  * Circuit breaker (`forced_continuations >= max_continuations`) is reached
- Otherwise blocks stopping (`{"decision": "continue", "reason": "..."}`) and increments
  `forced_continuations` so the agent is forced to complete `/simplify -> /code-review -> /verify`.

Can also be invoked as a CLI helper by the agent to initialize or update state:
  ./scripts/craftsman-stop-gate.py --state-file <path> --init [--mode craftsman|ultracode]
  ./scripts/craftsman-stop-gate.py --state-file <path> --pass-simplify --pass-review --pass-verify
  ./scripts/craftsman-stop-gate.py --state-file <path> --await-user | --resume | --complete
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

REQUIRED_GATES = ("simplified", "reviewed", "verified")
DEFAULT_MAX_CONTINUATIONS = 3


def default_state(mode: str = "craftsman") -> dict[str, Any]:
    return {
        "active": True,
        "mode": mode,
        "stage": "implement",
        "awaiting_user": False,
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
    except Exception:
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
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def emit_stop_decision(decision: str, reason: str | None = None) -> None:
    payload: dict[str, Any] = {"decision": decision}
    if reason:
        payload["reason"] = reason
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def is_waiting_on_ask_question_in_transcript(payload: dict[str, Any]) -> bool:
    """Return True if the last tool-calling turn in transcript invoked `ask_question` with no subsequent user input."""
    candidates: list[Path] = []
    raw_transcript = payload.get("transcriptPath")
    if isinstance(raw_transcript, str) and raw_transcript:
        candidates.append(Path(raw_transcript))
    raw_artifact = payload.get("artifactDirectoryPath")
    if isinstance(raw_artifact, str) and raw_artifact:
        candidates.append(Path(raw_artifact) / ".system_generated" / "logs" / "transcript.jsonl")

    transcript_path = next((p for p in candidates if p.is_file()), None)
    if transcript_path is None:
        return False

    try:
        lines = transcript_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return False

    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue

        step_type = entry.get("type")
        if step_type == "USER_INPUT":
            return False

        tool_calls = entry.get("tool_calls")
        if isinstance(tool_calls, list) and tool_calls:
            names = [tc.get("name") for tc in tool_calls if isinstance(tc, dict)]
            return "ask_question" in names

    return False


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def evaluate_stop(payload: dict[str, Any]) -> tuple[str, str | None]:
    termination_reason = payload.get("terminationReason", "NO_TOOL_CALL")
    if termination_reason != "NO_TOOL_CALL":
        return "allow", None

    if payload.get("fullyIdle") is False:
        return "allow", None

    artifact_dir = payload.get("artifactDirectoryPath")
    if not isinstance(artifact_dir, str) or not artifact_dir.strip():
        return "allow", None

    state_file = Path(artifact_dir) / "craftsman_state.json"
    state = load_state(state_file)
    if not state or not state.get("active", False):
        return "allow", None

    if state.get("awaiting_user", False) or is_waiting_on_ask_question_in_transcript(payload):
        return "allow", None

    gates = state.get("gates")
    if not isinstance(gates, dict):
        gates = {}

    missing = [gate for gate in REQUIRED_GATES if not bool(gates.get(gate, False))]
    if not missing:
        state["active"] = False
        state["stage"] = "completed"
        try:
            save_state_atomic(state_file, state)
        except Exception:
            pass
        return "allow", None

    forced = _safe_int(state.get("forced_continuations"), 0)
    max_cont = max(1, _safe_int(state.get("max_continuations"), DEFAULT_MAX_CONTINUATIONS))

    if forced >= max_cont:
        state["active"] = False
        state["stage"] = "aborted_max_continuations"
        try:
            save_state_atomic(state_file, state)
        except Exception:
            pass
        return "allow", None

    state["forced_continuations"] = forced + 1
    try:
        save_state_atomic(state_file, state)
    except Exception:
        pass

    missing_str = ", ".join(missing)
    return (
        "continue",
        f"[CRAFTSMAN HARD-LOOP GATE ({forced + 1}/{max_cont})] "
        f"Nie możesz zakończyć zadania przed domknięciem Bramki Jakości (Etap 1). "
        f"Brakujące kroki w {state_file}: [{missing_str}]. "
        f"Wykonaj brakujące etapy (/simplify -> /code-review -> /verify), zaktualizuj "
        f"odpowiednie flagi w 'gates' na true w pliku {state_file} (lub ustaw "
        f"'awaiting_user': true, jeśli czekasz na odpowiedź użytkownika) i dopiero "
        f"wtedy przedstaw Kartę Odbioru.",
    )


def run_cli(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Manage craftsman_state.json for Hard-Loop gating.")
    parser.add_argument("--state-file", required=True, help="Path to craftsman_state.json")
    parser.add_argument("--init", action="store_true", help="Initialize a fresh active state")
    parser.add_argument("--mode", default="craftsman", help="Mode name (craftsman or ultracode)")
    parser.add_argument("--stage", help="Set current stage name")
    parser.add_argument("--await-user", action="store_true", help="Pause hard-loop while waiting for user input")
    parser.add_argument("--resume", action="store_true", help="Resume hard-loop after user input")
    parser.add_argument("--pass-simplify", action="store_true", help="Mark 'simplified' gate as true")
    parser.add_argument("--pass-review", action="store_true", help="Mark 'reviewed' gate as true")
    parser.add_argument("--pass-verify", action="store_true", help="Mark 'verified' gate as true")
    parser.add_argument("--complete", action="store_true", help="Mark all gates passed and deactivate loop")
    args = parser.parse_args(argv)

    state_file = Path(args.state_file)
    state = load_state(state_file)
    if args.init or state is None:
        state = default_state(mode=args.mode)

    gates = state.setdefault("gates", {"simplified": False, "reviewed": False, "verified": False})
    if args.stage:
        state["stage"] = args.stage
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
        gates["simplified"] = True
        gates["reviewed"] = True
        gates["verified"] = True
        state["awaiting_user"] = False
        state["active"] = False
        state["stage"] = "completed"

    save_state_atomic(state_file, state)
    return 0


def main() -> int:
    if len(sys.argv) > 1:
        return run_cli(sys.argv[1:])

    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        emit_stop_decision("allow")
        return 0

    decision, reason = evaluate_stop(payload)
    emit_stop_decision(decision, reason)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
