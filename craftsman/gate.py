#!/usr/bin/env python3
"""Stop lifecycle hook and CLI state helper for the `/craftsman` 3-Gear Hybrid Hard-Loop.

Enforces the 3-Gear Adaptive Quality Gate:
- Gear 1 (`quick` / Soft-Loop): Activated via `--tier quick` (`/craftsman --quick`) or
  automatically (`--tier auto`) when the cumulative git diff (`base_sha..working_tree`)
  is <= 40 lines across <= 2 files (or 0 changed files). Allows inline verification
  without spawning a subagent.
- Gear 2 (`standard` / Hybrid Hard-Loop): Activated automatically when the cumulative diff
  exceeds 40 lines or 2 files (or when `--tier standard` is set). Enforces deterministic
  verification in `transcript.jsonl` that an isolated subagent (`Craftsman Quality Gate
  Auditor`) was invoked via `invoke_subagent` after task start and covers the final code state.
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
MAX_UNTRACKED_TEXT_FILE_BYTES = 1_048_576  # 1 MiB
BINARY_SNIFF_BYTES = 8192
VALID_TIERS = ("auto", "quick", "standard", "deep")
TIER_RANK = {"quick": 0, "auto": 1, "standard": 2, "deep": 3}

QUALITY_GATE_ROLE_MARKERS = (
    "quality gate",
    "auditor",
    "code reviewer",
    "audytor",
)
QUALITY_GATE_SKILL_MARKERS = ("simplify", "code-review", "verify")
QUALITY_GATE_PROMPT_KEYWORDS = (
    "simplify",
    "review",
    "verify",
    "audit",
    "diff",
    "quality",
    "gate",
    "check",
    "uprość",
    "przejrzyj",
    "zweryfikuj",
    "audyt",
    "weryfikacj",
    "sprawdź",
)
AUDIT_PASS_VERDICT_RE = re.compile(
    r"(?im)(?:^|[.\n])[ \t]*(?:[-*>][ \t]*)?\*{0,2}"
    r"(?:craftsman_verdict|overall[ \t]+verdict|verdict)[ \t*]*:[ \t*]*(?:✅[ \t*]*)?pass(?:ed)?\b"
)
AUDIT_FAIL_VERDICT_RE = re.compile(
    r"(?im)^[ \t]*(?:[-*>][ \t]*)?\*{0,2}"
    r"(?:craftsman_verdict|overall[ \t]+verdict|verdict|verify[ \t]+status|status)"
    r"[ \t*]*:[ \t*]*(?:❌[ \t*]*)?(?:fail|failed|reject|rejected)\b"
    r"|\bquality_gate_failed\b"
)

LEADING_COMMAND_TRIGGER_RE = re.compile(
    r"(?i)^\s*(?:/[a-z0-9_-]+\s+)*(/craftsman|ultracode)(?=$|[\s.,:;!])"
)
INLINE_TRIGGER_RE = re.compile(
    r"(?i)(?:^|\s)(/craftsman|ultracode)(?=$|[\s.,!:])"
)
CRAFTSMAN_FLAG_TIER_RE = re.compile(
    r"(?i)/craftsman(?:\s+--(quick|soft|standard|deep))\b"
)
CLAUDE_COMMAND_NAME_RE = re.compile(
    r"<command-name>\s*/?(craftsman|ultracode)\s*</command-name>", re.IGNORECASE
)
CLAUDE_COMMAND_ARGS_RE = re.compile(
    r"<command-args>(.*?)</command-args>", re.DOTALL | re.IGNORECASE
)
CONVERSATIONAL_OR_NEGATED_PREFIX_RE = re.compile(
    r"(?i)\b(explain|what\s+is|what\s+does|how\s+does|describe|tell\s+me\s+about|"
    r"wyja[sś]nij|opowiedz\s+o|co\s+to|jak\s+dzia[łl]a|"
    r"do\s+not|don't\s+want|don't\s+use|not\s+want|without|avoid|never\s+use|"
    r"nie\s+chc[eę]|nie\s+u[żz]ywaj|bez)\b"
)
NON_IMPERATIVE_ULTRACODE_RE = re.compile(
    r"(?i)\bultracode\s+(?:does|is|works|keyword|mode|rule)\b"
)
SKILL_METADATA_TRIGGER_RE = re.compile(
    r"<SKILL>[^<]*skills/craftsman/SKILL\.md</SKILL>", re.IGNORECASE
)
USER_REQUEST_BLOCK_RE = re.compile(
    r"<USER_REQUEST>(.*?)</USER_REQUEST>", re.DOTALL | re.IGNORECASE
)
QUOTED_SPAN_RE = re.compile(r"`[^`]*`|\"[^\"]*\"|(?<![A-Za-z0-9])'[^'\n]*'(?![A-Za-z0-9])")


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


def resolve_auto_base_sha(
    cwd: str | None,
    entries: list[dict[str, Any]],
    trigger_step: int,
    transcript_path: Path | None = None,
) -> str | None:
    """Resolve the baseline commit SHA prior to commits made after `trigger_step` when `--init` was omitted."""
    head_sha = get_head_sha(cwd)
    if not head_sha or not cwd or not os.path.isdir(cwd):
        return head_sha

    commit_calls = 0
    for idx, entry in enumerate(entries, start=1):
        step_idx = _safe_int(entry.get("step_index"), idx)
        if step_idx <= trigger_step:
            continue
        for tc in entry.get("tool_calls") or []:
            if not isinstance(tc, dict) or tc.get("name") not in ("run_command", "Bash"):
                continue
            args = tc.get("args") or tc.get("input") or {}
            if isinstance(args, dict):
                cmd = str(args.get("CommandLine") or args.get("command") or "")
                if re.search(r"\bgit\b.*\bcommit\b", cmd):
                    commit_calls += 1

    if commit_calls > 0:
        try:
            proc = subprocess.run(
                ["git", "rev-parse", f"HEAD~{commit_calls}"],
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                return proc.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass

    if transcript_path is not None and transcript_path.is_file():
        try:
            t_mtime = transcript_path.stat().st_mtime
            reflog_path = Path(cwd) / ".git" / "logs" / "HEAD"
            if reflog_path.is_file() and reflog_path.stat().st_mtime >= t_mtime:
                lines = [
                    ln.strip()
                    for ln in reflog_path.read_text(encoding="utf-8", errors="replace").splitlines()
                    if ln.strip()
                ]
                for ln in reversed(lines):
                    parts = ln.split()
                    if len(parts) >= 2 and parts[0] != "0" * 40 and "\tcommit:" in ln:
                        return parts[0]
        except OSError:
            pass

    return head_sha


def _count_text_file_lines_bounded(fpath: Path) -> int:
    """Count lines in an untracked file, skipping binary files and files larger than 1 MiB."""
    try:
        st = fpath.stat()
        if st.st_size == 0 or st.st_size > MAX_UNTRACKED_TEXT_FILE_BYTES:
            return 0
        with fpath.open("rb") as f:
            head = f.read(BINARY_SNIFF_BYTES)
            if b"\x00" in head:
                return 0
            lines = head.count(b"\n")
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                lines += chunk.count(b"\n")
            if st.st_size > 0 and not head.endswith(b"\n") and lines == 0:
                return 1
            return lines
    except OSError:
        return 0


def compute_cumulative_diff_summary(
    cwd: str | None, base_sha: str | None
) -> dict[str, int] | None:
    """Return diff metrics dict for `git diff <base_sha>` plus untracked files in `cwd`, or None if not a git repo."""
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
        added_total = 0
        deleted_total = 0
        for line in proc.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            files_count += 1
            # Binary files in git diff --numstat have '-' for added/deleted
            added = int(parts[0]) if parts[0].isdigit() else 0
            deleted = int(parts[1]) if parts[1].isdigit() else 0
            added_total += added
            deleted_total += deleted

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
                    added_total += _count_text_file_lines_bounded(fpath)

        return {
            "changed_files": files_count,
            "added_lines": added_total,
            "deleted_lines": deleted_total,
            "total_changed_lines": added_total + deleted_total,
        }
    except (OSError, subprocess.SubprocessError):
        return None


def compute_cumulative_diff_stats(cwd: str | None, base_sha: str | None) -> tuple[int, int] | None:
    """Return (changed_files_count, total_changed_lines) for `git diff <base_sha>` plus untracked files in `cwd`."""
    summary = compute_cumulative_diff_summary(cwd, base_sha)
    if summary is None:
        return None
    return summary["changed_files"], summary["total_changed_lines"]


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
    stats = compute_cumulative_diff_stats(
        cwd if isinstance(cwd, str) else None,
        base_sha if isinstance(base_sha, str) else None,
    )
    if stats is not None:
        num_files, total_lines = stats
        if num_files <= SMALL_DIFF_MAX_FILES and total_lines <= SMALL_DIFF_MAX_LINES:
            return False
    return True


def resolve_effective_gear_label(state: dict[str, Any], fallback_cwd: str | None = None) -> str:
    """Return human-readable 3-Gear label consistent with `requires_subagent_for_state`."""
    tier = str(state.get("tier") or "auto").strip().lower()
    mode = str(state.get("mode") or "craftsman").strip().lower()
    if mode == "ultracode" or tier == "deep":
        return "deep (Gear 3: Full-Audit Hard-Loop)"
    if not requires_subagent_for_state(state, fallback_cwd):
        return "quick (Gear 1: Soft-Loop)"
    return "standard (Gear 2: Hybrid Hard-Loop)"


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


def _extract_claude_tool_result_text(item: dict[str, Any]) -> str:
    raw = item.get("content")
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):
        parts: list[str] = []
        for sub in raw:
            if isinstance(sub, dict) and sub.get("type") == "text":
                parts.append(str(sub.get("text") or ""))
        return "\n".join(parts)
    return ""


def _normalize_transcript_entry(obj: dict[str, Any], fallback_idx: int) -> dict[str, Any]:
    """Normalize Antigravity and Claude Code JSONL entries into a common structure."""
    if "step_index" in obj or obj.get("type") in (
        "USER_INPUT",
        "PLANNER_RESPONSE",
        "GENERIC",
        "SYSTEM_MESSAGE",
    ):
        return obj

    entry_type = obj.get("type")
    msg = obj.get("message")
    if entry_type == "user" and isinstance(msg, dict):
        raw_content = msg.get("content")
        text_parts: list[str] = []
        tool_result_parts: list[str] = []
        if isinstance(raw_content, str):
            text_parts.append(raw_content)
        elif isinstance(raw_content, list):
            for item in raw_content:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "text":
                    text_parts.append(str(item.get("text") or ""))
                elif item.get("type") == "tool_result":
                    tr_text = _extract_claude_tool_result_text(item)
                    if tr_text:
                        tool_result_parts.append(tr_text)
        if tool_result_parts and not text_parts:
            return {
                "step_index": fallback_idx,
                "type": "GENERIC",
                "content": "\n".join(tool_result_parts),
            }
        return {
            "step_index": fallback_idx,
            "type": "USER_INPUT",
            "content": "\n".join(text_parts + tool_result_parts),
        }

    if entry_type == "assistant" and isinstance(msg, dict):
        raw_content = msg.get("content")
        tool_calls: list[dict[str, Any]] = []
        if isinstance(raw_content, list):
            for item in raw_content:
                if isinstance(item, dict) and item.get("type") == "tool_use":
                    tc_input = item.get("input")
                    tool_calls.append(
                        {
                            "name": item.get("name"),
                            "input": tc_input if isinstance(tc_input, dict) else {},
                            "args": tc_input if isinstance(tc_input, dict) else {},
                        }
                    )
        return {
            "step_index": fallback_idx,
            "type": "PLANNER_RESPONSE",
            "tool_calls": tool_calls,
        }

    return obj


def read_transcript_entries(transcript_path: Path | None) -> list[dict[str, Any]]:
    if transcript_path is None or not transcript_path.is_file():
        return []
    entries: list[dict[str, Any]] = []
    try:
        with transcript_path.open("r", encoding="utf-8", errors="replace") as f:
            for idx, line in enumerate(f, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    if isinstance(obj, dict):
                        entries.append(_normalize_transcript_entry(obj, idx))
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
    auto_detected: bool = False,
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
        "auto_detected": auto_detected,
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


def _is_claude_stop_payload(payload: dict[str, Any]) -> bool:
    return bool(
        payload.get("hook_event_name") == "Stop"
        or ("transcript_path" in payload and "artifactDirectoryPath" not in payload)
    )


def emit_stop_decision(
    decision: str, reason: str | None = None, claude_format: bool = False
) -> None:
    out_decision = "block" if (claude_format and decision == "continue") else decision
    payload: dict[str, Any] = {"decision": out_decision}
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


def _extract_craftsman_flag_tier(text: str, trigger_token: str) -> str:
    if trigger_token.lower() == "ultracode":
        return "deep"
    flag_match = CRAFTSMAN_FLAG_TIER_RE.search(text)
    if flag_match and flag_match.group(1):
        raw_flag = flag_match.group(1).lower()
        return "quick" if raw_flag == "soft" else raw_flag
    return "auto"


def is_craftsman_trigger_content(content: str) -> tuple[bool, str]:
    """Return (is_triggered, tier) if `content` is a command invocation rather than a discussion/question."""
    if not content or not content.strip():
        return False, "auto"

    claude_cmd = CLAUDE_COMMAND_NAME_RE.search(content)
    if claude_cmd:
        cmd_token = claude_cmd.group(1).lower()
        if cmd_token == "ultracode":
            return True, "deep"
        args_match = CLAUDE_COMMAND_ARGS_RE.search(content)
        args_text = args_match.group(1).strip() if args_match else ""
        return True, _extract_craftsman_flag_tier(f"/craftsman {args_text}", "/craftsman")

    req_match = USER_REQUEST_BLOCK_RE.search(content)
    user_text = req_match.group(1).strip() if req_match else content.strip()
    has_skill_tag = bool(SKILL_METADATA_TRIGGER_RE.search(content))

    # Strip quoted or backticked mentions (preserving English contractions like Don't / it's)
    unquoted = QUOTED_SPAN_RE.sub(" ", user_text)

    leading_match = LEADING_COMMAND_TRIGGER_RE.search(unquoted)
    if leading_match:
        tok = leading_match.group(1)
        return True, _extract_craftsman_flag_tier(unquoted, tok)

    if has_skill_tag:
        return True, _extract_craftsman_flag_tier(unquoted, "/craftsman")

    # Check inline imperative trigger (e.g., "zrób to przez /craftsman"), ignoring questions and discussions
    for line in unquoted.splitlines():
        line_stripped = line.strip()
        if not line_stripped or "?" in line_stripped:
            continue
        inline_match = INLINE_TRIGGER_RE.search(line_stripped)
        if not inline_match:
            continue
        prefix = line_stripped[: inline_match.start()]
        if CONVERSATIONAL_OR_NEGATED_PREFIX_RE.search(prefix):
            continue
        tok = inline_match.group(1)
        if tok.lower() == "ultracode" and NON_IMPERATIVE_ULTRACODE_RE.search(line_stripped):
            continue
        return True, _extract_craftsman_flag_tier(line_stripped, tok)

    return False, "auto"


def detect_untracked_craftsman_trigger(
    entries: list[dict[str, Any]], after_step: int
) -> tuple[int, str] | None:
    """If user triggered `/craftsman` or `ultracode` after `after_step`, return (step_index, tier)."""
    found: tuple[int, str] | None = None
    for idx, entry in enumerate(entries, start=1):
        step_idx = _safe_int(entry.get("step_index"), idx)
        if step_idx <= after_step:
            continue
        if entry.get("type") == "USER_INPUT":
            content = str(entry.get("content") or "")
            triggered, tier = is_craftsman_trigger_content(content)
            if triggered:
                found = (step_idx, tier)
    return found


def _is_quality_gate_subagent_spec(sub: dict[str, Any]) -> bool:
    sub_type = str(sub.get("TypeName") or "self").strip().lower()
    if sub_type != "self":
        return False
    role = str(sub.get("Role") or "").lower()
    prompt = str(sub.get("Prompt") or "").strip().lower()
    if len(prompt) < 8:
        return False
    if all(skill in prompt for skill in QUALITY_GATE_SKILL_MARKERS):
        return True
    if any(marker in role for marker in QUALITY_GATE_ROLE_MARKERS):
        return any(kw in prompt for kw in QUALITY_GATE_PROMPT_KEYWORDS)
    return False


def _entry_invokes_quality_gate_subagent(entry: dict[str, Any]) -> bool:
    tool_calls = entry.get("tool_calls")
    if not isinstance(tool_calls, list):
        return False
    for tc in tool_calls:
        if not isinstance(tc, dict):
            continue
        tc_name = tc.get("name")
        if tc_name not in ("invoke_subagent", "Task", "Agent"):
            continue
        args = tc.get("args") or tc.get("input")
        if not isinstance(args, dict):
            continue

        if tc_name in ("Task", "Agent"):
            sub_type = str(args.get("subagent_type") or "general-purpose").strip().lower()
            if sub_type in ("explore", "research"):
                continue
            desc = str(args.get("description") or "").lower()
            prompt = str(args.get("prompt") or "").strip().lower()
            if len(prompt) < 8:
                continue
            if all(skill in prompt for skill in QUALITY_GATE_SKILL_MARKERS):
                return True
            if any(marker in desc for marker in QUALITY_GATE_ROLE_MARKERS) and any(
                kw in prompt for kw in QUALITY_GATE_PROMPT_KEYWORDS
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
        args_dump = json.dumps(args, ensure_ascii=False).lower().replace('\\"', '"')
        if '"typename": "research"' in args_dump or '"typename":"research"' in args_dump:
            continue
        if all(skill in args_dump for skill in QUALITY_GATE_SKILL_MARKERS):
            return True
        if any(marker in args_dump for marker in QUALITY_GATE_ROLE_MARKERS) and any(
            kw in args_dump for kw in QUALITY_GATE_PROMPT_KEYWORDS
        ):
            return True
    return False


SHELL_FILE_MUTATION_RE = re.compile(
    r"(?:(?<!\d)>>?\s*(?!/dev/null\b)[A-Za-z0-9_./-]+"
    r"|\b(?:sed|perl)\s+-i\b"
    r"|\btee\s+(?:-a\s+)?(?!/dev/null\b)[A-Za-z0-9_./-]+"
    r"|\b(?:patch|git\s+apply)\b)"
)


def _shell_command_mutates_files(cmd: str) -> bool:
    if not cmd or "craftsman-stop-gate" in cmd or "craftsman gate" in cmd:
        return False
    # Strip descriptor redirections (2>&1, >&2) and quoted strings before checking for file redirection
    cleaned = re.sub(r"\d*>&\d+|\d*>>?\s*/dev/null\b", " ", cmd)
    cleaned = QUOTED_SPAN_RE.sub(" ", cleaned)
    return bool(SHELL_FILE_MUTATION_RE.search(cleaned))


def _count_post_audit_edit_stats(entries: list[dict[str, Any]], after_idx: int) -> tuple[int, int]:
    """Count (modified_files_count, added_or_replaced_lines) in tool calls after `entries[after_idx]`."""
    edited_files: set[str] = set()
    edited_lines = 0
    edit_tools = {
        "write_to_file",
        "replace_file_content",
        "multi_replace_file_content",
        "Write",
        "Edit",
        "MultiEdit",
    }
    for entry in entries[after_idx + 1 :]:
        tool_calls = entry.get("tool_calls")
        if not isinstance(tool_calls, list):
            continue
        for tc in tool_calls:
            if not isinstance(tc, dict):
                continue
            name = tc.get("name")
            args = tc.get("args") or tc.get("input")
            if not isinstance(args, dict):
                continue
            if name in ("run_command", "Bash"):
                cmd = str(args.get("CommandLine") or args.get("command") or "")
                if _shell_command_mutates_files(cmd):
                    edited_files.add("<shell_mutation>")
                    edited_lines += SMALL_DIFF_MAX_LINES + 1
                continue
            if name not in edit_tools:
                continue
            target_file = str(
                args.get("TargetFile") or args.get("file_path") or args.get("path") or ""
            )
            norm_target = target_file.replace("\\", "/")
            if "/.gemini/antigravity/brain/" in norm_target or norm_target.endswith(
                "craftsman_state.json"
            ):
                continue
            if target_file:
                edited_files.add(target_file)
            for content_key in ("CodeContent", "ReplacementContent", "content", "new_string"):
                raw_val = args.get(content_key)
                if isinstance(raw_val, str) and raw_val:
                    edited_lines += len(raw_val.splitlines())
            chunks = args.get("ReplacementChunks") or args.get("edits")
            if isinstance(chunks, list):
                for chunk in chunks:
                    if isinstance(chunk, dict):
                        rep = chunk.get("ReplacementContent") or chunk.get("new_string")
                        if isinstance(rep, str) and rep:
                            edited_lines += len(rep.splitlines())
    return len(edited_files), edited_lines


def _has_audit_failure_after(entries: list[dict[str, Any]], after_idx: int) -> bool:
    """Return True if the auditor response immediately following `entries[after_idx]` reports an explicit failure."""
    for entry in entries[after_idx + 1 :]:
        if entry.get("type") == "PLANNER_RESPONSE":
            # Stop scanning auditor responses once the main agent moves on to non-auditor tool calls
            break
        content = entry.get("content")
        if isinstance(content, str) and content:
            if AUDIT_FAIL_VERDICT_RE.search(content) and not AUDIT_PASS_VERDICT_RE.search(content):
                return True
    return False


def has_quality_gate_subagent_in_entries(
    entries: list[dict[str, Any]], since_step: int
) -> bool:
    """Return True if a valid Quality Gate Auditor subagent was invoked after `since_step` and not invalidated."""
    last_auditor_idx: int | None = None
    for pos, entry in enumerate(entries):
        step_idx = _safe_int(entry.get("step_index"), pos + 1)
        # If task was initialized at step > 0, the auditor must be invoked strictly after that step
        if since_step > 0 and step_idx <= since_step:
            continue
        if step_idx < since_step:
            continue
        if _entry_invokes_quality_gate_subagent(entry):
            last_auditor_idx = pos

    if last_auditor_idx is None:
        return False

    if _has_audit_failure_after(entries, last_auditor_idx):
        return False

    post_files, post_lines = _count_post_audit_edit_stats(entries, last_auditor_idx)
    if post_files > SMALL_DIFF_MAX_FILES or post_lines > SMALL_DIFF_MAX_LINES:
        return False

    return True


def _resolve_state_file_from_payload(payload: dict[str, Any], artifact_dir: str) -> Path:
    base_dir = Path(artifact_dir).expanduser()
    if "artifactDirectoryPath" not in payload:
        session_id = payload.get("session_id")
        if isinstance(session_id, str) and session_id.strip():
            safe_sid = re.sub(r"[^A-Za-z0-9_.-]", "_", session_id.strip())
            return base_dir / f"craftsman_state_{safe_sid}.json"
    return base_dir / "craftsman_state.json"


def evaluate_stop(payload: dict[str, Any]) -> tuple[str, str | None]:
    termination_reason = payload.get("terminationReason", "NO_TOOL_CALL")
    if termination_reason != "NO_TOOL_CALL":
        return "allow", None

    if payload.get("fullyIdle") is False or payload.get("stop_hook_active") is True:
        return "allow", None

    artifact_dir = payload.get("artifactDirectoryPath")
    if not isinstance(artifact_dir, str) or not artifact_dir.strip():
        raw_tp = payload.get("transcript_path") or payload.get("transcriptPath")
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

    state_file = _resolve_state_file_from_payload(payload, artifact_dir)
    transcript_path = resolve_transcript_for_state_file(state_file, payload)
    entries = read_transcript_entries(transcript_path)

    state = load_state(state_file)
    completed_at = _safe_int(state.get("completed_at_step"), -1) if state else -1

    # Auto-detect `/craftsman` or `ultracode` from transcript even if agent forgot `--init`
    if (not state or not state.get("active", False)) and entries:
        trigger_info = detect_untracked_craftsman_trigger(entries, completed_at)
        if trigger_info is not None:
            trigger_step, detected_tier = trigger_info
            auto_base_sha = resolve_auto_base_sha(
                ws_cwd_str, entries, trigger_step, transcript_path
            )
            diff_stats = compute_cumulative_diff_stats(ws_cwd_str, auto_base_sha)
            # If auto-detected on a clean repository with 0 changed files, do not block stop
            if diff_stats is not None and diff_stats[0] == 0:
                return "allow", None
            state = default_state(
                mode="ultracode" if detected_tier == "deep" else "craftsman",
                tier=detected_tier,
                cwd=ws_cwd_str,
                base_sha=auto_base_sha,
                started_at_step=trigger_step,
                auto_detected=True,
            )
            _try_save_state(state_file, state)

    if not state or not state.get("active", False):
        return "allow", None

    if state.get("awaiting_user", False) or is_waiting_on_ask_question_in_entries(entries):
        return "allow", None

    effective_cwd = state.get("cwd") if isinstance(state.get("cwd"), str) else ws_cwd_str
    base_sha_val = state.get("base_sha") if isinstance(state.get("base_sha"), str) else None
    diff_stats = compute_cumulative_diff_stats(effective_cwd, base_sha_val)
    if (
        diff_stats is not None
        and diff_stats[0] == 0
        and bool(state.get("auto_detected", False))
    ):
        state["active"] = False
        state["stage"] = "completed"
        state["completed_at_step"] = get_latest_step_index(entries)
        _try_save_state(state_file, state)
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
    parser = argparse.ArgumentParser(
        description="Manage craftsman_state.json for 3-Gear Hard-Loop gating."
    )
    parser.add_argument("--state-file", required=True, help="Path to craftsman_state.json")
    parser.add_argument("--init", action="store_true", help="Initialize a fresh active state")
    parser.add_argument(
        "--cwd", help="Repository working directory to capture base_sha and diff stats"
    )
    parser.add_argument("--base-sha", help="Explicit base commit SHA for cumulative diff")
    parser.add_argument("--mode", default="craftsman", help="Mode name (craftsman or ultracode)")
    parser.add_argument(
        "--tier",
        choices=VALID_TIERS,
        help="Gearbox tier: auto (default: <=40 lines & <=2 files -> quick, else standard), quick, standard, or deep",
    )
    parser.add_argument("--stage", help="Set current stage name")
    parser.add_argument(
        "--await-user", action="store_true", help="Pause hard-loop while waiting for user input"
    )
    parser.add_argument("--resume", action="store_true", help="Resume hard-loop after user input")
    parser.add_argument(
        "--pass-simplify", action="store_true", help="Mark 'simplified' gate as true"
    )
    parser.add_argument("--pass-review", action="store_true", help="Mark 'reviewed' gate as true")
    parser.add_argument("--pass-verify", action="store_true", help="Mark 'verified' gate as true")
    parser.add_argument(
        "--complete", action="store_true", help="Verify quality gate execution and deactivate loop"
    )
    args = parser.parse_args(argv)

    state_file = Path(args.state_file).expanduser()
    transcript_path = resolve_transcript_for_state_file(state_file)
    entries = read_transcript_entries(transcript_path)
    latest_step = get_latest_step_index(entries)

    state = load_state(state_file)
    if args.init or state is None:
        if args.init:
            start_step = latest_step
            initial_tier = args.tier or ("deep" if args.mode == "ultracode" else "auto")
            base_sha = args.base_sha or get_head_sha(args.cwd)
        else:
            detected = detect_untracked_craftsman_trigger(entries, -1)
            start_step = detected[0] if detected is not None else 0
            initial_tier = args.tier or (detected[1] if detected is not None else "auto")
            base_sha = args.base_sha or resolve_auto_base_sha(
                args.cwd, entries, start_step, transcript_path
            )
        state = default_state(
            mode=args.mode,
            tier=initial_tier,
            cwd=args.cwd,
            base_sha=base_sha,
            started_at_step=start_step,
            auto_detected=not bool(args.init),
        )
    elif args.tier:
        # On an already-initialized state file, do not allow --complete --tier quick to downgrade verification
        current_tier = str(state.get("tier") or "auto").strip().lower()
        if not args.complete or TIER_RANK.get(args.tier, 1) > TIER_RANK.get(current_tier, 1):
            state["tier"] = args.tier

    gates = state.setdefault("gates", {"simplified": False, "reviewed": False, "verified": False})
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
        effective_cwd = args.cwd or (
            state.get("cwd") if isinstance(state.get("cwd"), str) else None
        )
        needs_subagent = requires_subagent_for_state(state, effective_cwd)
        subagent_ran = (
            has_quality_gate_subagent_in_entries(entries, since_step) if entries else False
        )
        allow_inline_env = os.environ.get("CRAFTSMAN_ALLOW_INLINE") == "1"

        if entries and needs_subagent and not allow_inline_env:
            if not subagent_ran:
                sys.stderr.write(
                    "[CRAFTSMAN HARD-LOOP REJECTED] Cannot close quality gate (--complete): "
                    "transcript.jsonl does not record a valid, up-to-date 'invoke_subagent' call "
                    "(TypeName: 'self', Role: 'Craftsman Quality Gate Auditor') covering the final diff. "
                    "For changes exceeding Quick Mode thresholds (>40 lines or >2 files), spawn "
                    "the auditor subagent via invoke_subagent to execute /simplify, /code-review, and /verify.\n"
                )
                return 1

        state["subagent_verified"] = subagent_ran
        gates["simplified"] = True
        gates["reviewed"] = True
        gates["verified"] = True
        state["awaiting_user"] = False
        state["active"] = False
        state["stage"] = "completed"
        state["completed_at_step"] = latest_step

    save_state_atomic(state_file, state)
    return 0


def main(argv: list[str] | None = None) -> int:
    args_list = list(sys.argv[1:] if argv is None else argv)
    if args_list:
        return run_cli(args_list)

    try:
        raw = sys.stdin.read()
        loaded = json.loads(raw) if raw.strip() else {}
        payload = loaded if isinstance(loaded, dict) else {}
    except (OSError, json.JSONDecodeError):
        emit_stop_decision("allow")
        return 0

    decision, reason = evaluate_stop(payload)
    emit_stop_decision(decision, reason, claude_format=_is_claude_stop_payload(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
