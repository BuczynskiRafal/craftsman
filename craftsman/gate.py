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
  or keyword `ultracraft`). Always requires the isolated Quality Gate Auditor subagent
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
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from craftsman.diffscan import DiffScanError
from craftsman.evidence import Evaluation, archive_ledger_if_rebased, evaluate_evidence
from craftsman.ledger import (
    GatePolicy,
    GateReport,
    LedgerError,
    render_next_actions,
    render_report,
)
from craftsman.lenses import Tier

DEFAULT_MAX_CONTINUATIONS = 3
MAX_CONTINUATION_REASON_CHARS = 1800
MAX_ERROR_CHARS = 400
DEPRECATED_PASS_FLAGS = ("--pass-simplify", "--pass-review", "--pass-verify")
SMALL_DIFF_MAX_LINES = 40
SMALL_DIFF_MAX_FILES = 2
MAX_UNTRACKED_TEXT_FILE_BYTES = 1_048_576  # 1 MiB
BINARY_SNIFF_BYTES = 8192
VALID_TIERS = ("auto", "quick", "standard", "deep")
TIER_RANK = {"quick": 0, "auto": 1, "standard": 2, "deep": 3}

QUALITY_GATE_ROLE_MARKERS = (
    "quality gate",
    "auditor",
    "reviewer",  # `craftsman-reviewer`, "Craftsman Reviewer", "code reviewer"
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
CRAFTSMAN_FINAL_VERDICT_RE = re.compile(
    r"(?im)(?:^[ \t]*|\bcontent=[ \t]*)(?:(?:[-*>]+|#{1,6})[ \t]*)?[*`]{0,3}"
    r"(?:craftsman_verdict|overall[ \t]+verdict)"
    r"[ \t*`]*:[ \t*`]*(?:✅[ \t*`]*|❌[ \t*`]*)?[*`]{0,3}(pass(?:ed)?|fail(?:ed)?|reject(?:ed)?)\b"
)
AUDIT_SECTION_FAIL_RE = re.compile(
    r"(?im)(?:^[ \t]*|\bcontent=[ \t]*)(?:(?:[-*>]+|#{1,6})[ \t]*)?[*`]{0,3}"
    r"(?:craftsman_verdict|overall[ \t]+verdict|verdict|verify[ \t]+status|status)"
    r"[ \t*`]*:[ \t*`]*(?:❌[ \t*`]*)?[*`]{0,3}(?:fail|failed|reject|rejected)\b(?![ \t]+initially\b)"
    r"|\bquality_gate_failed\b"
)
SUBAGENT_ACK_CONV_ID_RE = re.compile(r'"conversationId"\s*:\s*"([^"]+)"')

LEADING_COMMAND_TRIGGER_RE = re.compile(
    r"(?i)^\s*(?:/[a-z0-9_-]+\s+)*(/craftsman|ultracraft)(?=$|[\s.,:;!])"
)
INLINE_TRIGGER_RE = re.compile(
    r"(?i)(?:^|\s)(/craftsman|ultracraft)(?=$|[\s.,!:])"
)
CRAFTSMAN_FLAG_TIER_RE = re.compile(
    r"(?i)/craftsman(?:\s+--(quick|soft|standard|deep))\b"
)
CLAUDE_COMMAND_NAME_RE = re.compile(
    r"<command-name>\s*/?(craftsman|ultracraft)\s*</command-name>", re.IGNORECASE
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
NON_IMPERATIVE_ULTRACRAFT_RE = re.compile(
    r"(?i)\bultracraft\s+(?:does|is|works|keyword|mode|rule)\b"
)
SKILL_METADATA_TRIGGER_RE = re.compile(
    r"<SKILL>[^<]*skills/craftsman/SKILL\.md</SKILL>", re.IGNORECASE
)
USER_REQUEST_BLOCK_RE = re.compile(
    r"<USER_REQUEST>((?:(?!</?USER_REQUEST>).)*?)</USER_REQUEST>", re.DOTALL | re.IGNORECASE
)
QUOTED_SPAN_RE = re.compile(r"`[^`]*`|\"[^\"]*\"|(?<![A-Za-z0-9])'[^'\n]*'(?![A-Za-z0-9])")
GIT_NEW_COMMIT_SEGMENT_RE = re.compile(
    r"\bgit\b(?:\s+(?:-[Cc]\s+\S+|--[a-z-]+(?:=\S+)?|-[A-Za-z0-9]+))*\s+"
    r"(?:commit|merge|cherry-pick)\b(?![-\w])"
)
GIT_NON_NEW_COMMIT_FLAG_RE = re.compile(
    r"\s(?:--abort|--quit|--dry-run|--no-commit|--amend|--help|-h)\b"
    r"|\bcherry-pick\b[^\n;&|]*\s-n\b"
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


def _count_successful_commits_after_trigger(
    entries: list[dict[str, Any]], trigger_step: int
) -> tuple[int, bool]:
    has_generic = any(e.get("type") == "GENERIC" for e in entries)
    commit_count = 0
    saw_command_tool = False
    for idx, entry in enumerate(entries):
        step_idx = _safe_int(entry.get("step_index"), idx + 1)
        if step_idx <= trigger_step:
            continue
        if entry.get("status") == "ERROR":
            continue
        if has_generic and idx + 1 < len(entries) and entries[idx + 1].get("type") == "GENERIC":
            next_g = entries[idx + 1]
            g_content = str(next_g.get("content") or "")
            if next_g.get("status") == "ERROR" or (
                "The command exited with code " in g_content
                and "The command exited with code 0." not in g_content
            ):
                continue

        for tc in entry.get("tool_calls") or []:
            if not isinstance(tc, dict) or tc.get("name") not in ("run_command", "Bash"):
                continue
            saw_command_tool = True
            args = tc.get("args") or tc.get("input") or {}
            if isinstance(args, dict):
                cmd = str(args.get("CommandLine") or args.get("command") or "")
                unquoted_cmd = QUOTED_SPAN_RE.sub(" ", cmd)
                for seg in re.split(r"[;\n|&]+", unquoted_cmd):
                    if GIT_NEW_COMMIT_SEGMENT_RE.search(
                        seg
                    ) and not GIT_NON_NEW_COMMIT_FLAG_RE.search(seg):
                        commit_count += 1
    return commit_count, saw_command_tool


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

    commit_calls, saw_command_tool = _count_successful_commits_after_trigger(entries, trigger_step)
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

    if not saw_command_tool and transcript_path is not None and transcript_path.is_file():
        try:
            st = transcript_path.stat()
            t_start = getattr(st, "st_birthtime", st.st_ctime)
            reflog_path = Path(cwd) / ".git" / "logs" / "HEAD"
            if reflog_path.is_file():
                lines = [
                    ln.strip()
                    for ln in reflog_path.read_text(encoding="utf-8", errors="replace").splitlines()
                    if ln.strip()
                ]
                earliest_old_sha: str | None = None
                for ln in reversed(lines):
                    tab_parts = ln.split("\t", 1)
                    header = tab_parts[0]
                    action = tab_parts[1] if len(tab_parts) > 1 else ""
                    if not action.startswith(("commit:", "merge ", "cherry-pick:")):
                        continue
                    parts = header.split()
                    if len(parts) < 5 or parts[0] == "0" * 40:
                        continue
                    try:
                        ts = float(parts[-2])
                    except ValueError:
                        continue
                    if ts >= t_start - 2.0:
                        earliest_old_sha = parts[0]
                    else:
                        break
                if earliest_old_sha:
                    return earliest_old_sha
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

    if mode == "ultracraft" or tier in ("deep", "standard"):
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


def resolve_effective_tier(state: dict[str, Any], fallback_cwd: str | None = None) -> Tier:
    """Concrete review tier of a state; `auto` resolves by the size of the cumulative diff."""
    tier = str(state.get("tier") or "auto").strip().lower()
    mode = str(state.get("mode") or "craftsman").strip().lower()
    if mode == "ultracraft" or tier == "deep":
        return "deep"
    return "standard" if requires_subagent_for_state(state, fallback_cwd) else "quick"


def resolve_effective_gear_label(state: dict[str, Any], fallback_cwd: str | None = None) -> str:
    """Return human-readable 3-Gear label consistent with `requires_subagent_for_state`."""
    return {
        "deep": "deep (Gear 3: Full-Audit Hard-Loop)",
        "standard": "standard (Gear 2: Hybrid Hard-Loop)",
        "quick": "quick (Gear 1: Soft-Loop)",
    }[resolve_effective_tier(state, fallback_cwd)]


def gate_policy(state: dict[str, Any], tier: Tier) -> GatePolicy:
    """The gate policy for a task; the single place that decides whether isolation is required.

    Subagent isolation is only demanded where platform hooks record subagent events, which they
    announce by setting `hooks_seen` on the active state.
    """
    return GatePolicy(tier=tier, require_isolation=bool(state.get("hooks_seen")) and tier != "quick")


def evaluate_task(state: dict[str, Any], fallback_cwd: str | None) -> Evaluation:
    """Evaluate the evidence ledger for the task described by `state`.

    Shared by `gate --complete`, the stop hooks and `craftsman review check` semantics. Raises
    `DiffScanError` or `LedgerError` when the work tree or the ledger cannot be read.
    """
    state_cwd = state.get("cwd")
    cwd = state_cwd if isinstance(state_cwd, str) and state_cwd else fallback_cwd or os.getcwd()
    base = state.get("base_sha")
    tier = resolve_effective_tier(state, cwd)
    base_ref = base if isinstance(base, str) and base else None
    return evaluate_evidence(cwd, base_ref, tier, lambda strictest: gate_policy(state, strictest))


def needs_transcript_auditor(
    state: dict[str, Any], entries: list[dict[str, Any]], fallback_cwd: str | None
) -> bool:
    """Whether the legacy transcript check for an auditor subagent applies.

    Platform hooks record subagent runs in the ledger, which `gate_policy` then enforces, so the
    transcript heuristic only applies to sessions without them.
    """
    if not entries or state.get("hooks_seen"):
        return False
    return requires_subagent_for_state(state, fallback_cwd)


def _encode_claude_project_slug(cwd: str) -> str:
    try:
        resolved = str(Path(cwd).expanduser().resolve())
    except OSError:
        resolved = cwd
    return re.sub(r"[^A-Za-z0-9-]", "-", resolved)


def resolve_transcript_for_state_file(
    state_file: Path, payload: dict[str, Any] | None = None, cwd: str | None = None
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

    # Fallback: check for recent JSONL transcripts in the same directory or Claude project directory
    search_dirs: list[Path] = [state_file.parent]
    if cwd:
        claude_proj = Path.home() / ".claude" / "projects" / _encode_claude_project_slug(cwd)
        if claude_proj.is_dir():
            search_dirs.append(claude_proj)
    for sdir in search_dirs:
        if not sdir.is_dir():
            continue
        jsonls = sorted(
            (p for p in sdir.glob("*.jsonl") if p.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if jsonls:
            return jsonls[0]
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


def detect_cli_platform(env: Mapping[str, str] | None = None) -> str:
    """Infer the active agent platform from environment variables when `craftsman gate --init` runs."""
    environ = os.environ if env is None else env
    if environ.get("CLAUDECODE") or environ.get("CLAUDE_CODE_ENTRYPOINT"):
        return "claude"
    if environ.get("ANTIGRAVITY_AGENT") or environ.get("AGY_MANAGED_ENV"):
        return "antigravity"
    if (
        environ.get("COPILOT_CLI")
        or environ.get("GITHUB_COPILOT_CLI")
        or environ.get("VSCODE_PID")
        or environ.get("VSCODE_IPC_HOOK")
        or environ.get("TERM_PROGRAM", "").strip().lower() == "vscode"
    ):
        return "copilot"
    return "cli"


def extract_cli_session_id(platform: str, env: Mapping[str, str] | None = None) -> str:
    """Return an explicit session ID from the environment if provided, else empty string."""
    environ = os.environ if env is None else env
    explicit = environ.get("CRAFTSMAN_SESSION_ID", "").strip()
    if explicit:
        return explicit
    if platform == "claude":
        return (
            environ.get("CLAUDE_SESSION_ID") or environ.get("CLAUDE_CODE_SESSION_ID") or ""
        ).strip()
    if platform == "copilot":
        return (environ.get("COPILOT_SESSION_ID") or "").strip()
    return ""


def format_gate_owner(owner: Any) -> str:
    """Human-readable representation of `state['owner']`."""
    if not isinstance(owner, dict):
        return "unknown"
    platform = str(owner.get("platform") or "").strip()
    if not platform:
        return "unknown"
    session_id = str(owner.get("session_id") or "").strip()
    if session_id:
        return f"{platform} (session {session_id})"
    return platform


def is_same_or_unclaimed_owner(
    existing_owner: Any, platform: str, session_id: str = ""
) -> bool:
    """Return True if `existing_owner` is unset (v3.0.0), unclaimed `cli`, or matches `(platform, session_id)`."""
    if not isinstance(existing_owner, dict):
        return True
    ex_plat = str(existing_owner.get("platform") or "").strip()
    if not ex_plat:
        return True
    ex_sid = str(existing_owner.get("session_id") or "").strip()
    in_sid = session_id.strip()
    if ex_plat == "cli" and not ex_sid:
        return True
    if ex_plat != platform:
        return False
    if ex_sid and in_sid and ex_sid != in_sid:
        return False
    return True


def check_and_claim_owner(
    state: dict[str, Any] | None, platform: str, session_id: str
) -> tuple[bool, bool]:
    """Check whether `(platform, session_id)` owns `state`, claiming an empty `session_id` if matching.

    Returns `(is_owner, claimed_modified)`. States without `owner` (v3.0.0) treat every session as owner.
    """
    if state is None:
        return True, False
    owner = state.get("owner")
    if not isinstance(owner, dict):
        return True, False
    ex_plat = str(owner.get("platform") or "").strip()
    if not ex_plat:
        return True, False
    ex_sid = str(owner.get("session_id") or "").strip()
    in_sid = session_id.strip()
    if ex_plat == "cli" and not ex_sid:
        owner["platform"] = platform
        if in_sid:
            owner["session_id"] = in_sid
        return True, True
    if ex_plat != platform:
        return False, False
    if not ex_sid:
        if in_sid:
            owner["session_id"] = in_sid
            return True, True
        return True, False
    if not in_sid or in_sid == ex_sid:
        return True, False
    return False, False


def _extract_antigravity_session_id(payload: dict[str, Any]) -> str:
    for key in ("conversation_id", "conversationId", "session_id", "sessionId"):
        val = payload.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    artifact_dir = payload.get("artifactDirectoryPath")
    if isinstance(artifact_dir, str) and artifact_dir.strip():
        p = Path(artifact_dir.strip())
        if p.parent.name == "brain" and p.name:
            return p.name
    return ""


def default_state(
    mode: str = "craftsman",
    tier: str = "auto",
    cwd: str | None = None,
    base_sha: str | None = None,
    started_at_step: int = 0,
    auto_detected: bool = False,
    owner: dict[str, str] | None = None,
) -> dict[str, Any]:
    state: dict[str, Any] = {
        "active": True,
        "mode": mode,
        "tier": tier if tier in VALID_TIERS else "auto",
        "cwd": cwd,
        "stage": "implement",
        "base_sha": base_sha,
        "started_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "started_at_step": started_at_step,
        "completed_at_step": -1,
        "awaiting_user": False,
        "subagent_verified": False,
        "auto_detected": auto_detected,
        "forced_continuations": 0,
    }
    if owner is not None:
        state["owner"] = {
            "platform": str(owner.get("platform") or "cli"),
            "session_id": str(owner.get("session_id") or ""),
        }
    return state


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
    if trigger_token.lower() == "ultracraft":
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
        if cmd_token == "ultracraft":
            return True, "deep"
        args_match = CLAUDE_COMMAND_ARGS_RE.search(content)
        args_text = args_match.group(1).strip() if args_match else ""
        return True, _extract_craftsman_flag_tier(f"/craftsman {args_text}", "/craftsman")

    req_matches = list(USER_REQUEST_BLOCK_RE.finditer(content))
    user_text = req_matches[-1].group(1).strip() if req_matches else content.strip()
    has_skill_tag = bool(SKILL_METADATA_TRIGGER_RE.search(content))

    # Strip quoted or backticked mentions (preserving English contractions like Don't / it's)
    unquoted = QUOTED_SPAN_RE.sub(" ", user_text)

    leading_match = LEADING_COMMAND_TRIGGER_RE.search(unquoted)
    if leading_match:
        tok = leading_match.group(1)
        first_line = unquoted.splitlines()[0].strip() if unquoted.splitlines() else unquoted
        if tok.lower() == "ultracraft" and (
            NON_IMPERATIVE_ULTRACRAFT_RE.search(first_line) or "?" in first_line
        ):
            pass
        else:
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
        if tok.lower() == "ultracraft" and NON_IMPERATIVE_ULTRACRAFT_RE.search(line_stripped):
            continue
        return True, _extract_craftsman_flag_tier(line_stripped, tok)

    return False, "auto"


def detect_untracked_craftsman_trigger(
    entries: list[dict[str, Any]], after_step: int
) -> tuple[int, str] | None:
    """If user triggered `/craftsman` or `ultracraft` after `after_step`, return (step_index, tier)."""
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
    if entry.get("status") == "ERROR":
        return False
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

        if parsed_subagents is None and any(k in args for k in ("TypeName", "Role", "Prompt")):
            unquoted_sub: dict[str, Any] = {}
            for k, v in args.items():
                if isinstance(v, str) and v.startswith('"') and v.endswith('"'):
                    try:
                        unquoted_sub[k] = json.loads(v)
                    except json.JSONDecodeError:
                        unquoted_sub[k] = v.strip('"')
                else:
                    unquoted_sub[k] = v
            parsed_subagents = [unquoted_sub]

        if parsed_subagents is not None:
            if any(
                isinstance(sub, dict) and _is_quality_gate_subagent_spec(sub)
                for sub in parsed_subagents
            ):
                return True
            continue

        # Fallback for truncated transcript lines where Subagents JSON string was cut off
        args_dump = json.dumps(args, ensure_ascii=False).lower().replace('\\"', '"')
        if re.search(r'"typename"\s*:\s*"+research"+', args_dump):
            continue
        if all(skill in args_dump for skill in QUALITY_GATE_SKILL_MARKERS):
            return True
        if any(marker in args_dump for marker in QUALITY_GATE_ROLE_MARKERS) and any(
            kw in args_dump for kw in QUALITY_GATE_PROMPT_KEYWORDS
        ):
            return True
    return False


SHELL_FILE_MUTATION_RE = re.compile(
    r"(?:(?<!\d)>>?\s*(?!/dev/null\b|/tmp/|/var/folders/)[A-Za-z0-9_./-]+"
    r"|\b(?:sed|perl)\s+-i\b"
    r"|\btee\s+(?:-a\s+)?(?!/dev/null\b|/tmp/|/var/folders/)[A-Za-z0-9_./-]+"
    r"|\b(?:patch|git\s+apply)\b"
    r"|\b(?:cp|mv)\s+(?:-[A-Za-z]+\s+)*\S+\s+(?!/tmp/|/var/folders/)\S+"
    r"|\b(?:ruff\s+(?:check\s+)?[^\n;&|]*--fix\b"
    r"|(?:black|isort|autopep8|rustfmt)\s+(?!--check\b|--diff\b|--version\b|--help\b|-h\b)\S+"
    r"|gofmt\s+-w\b"
    r"|prettier\s+[^\n;&|]*-w\b))"
)
PYTHON_INLINE_WRITE_RE = re.compile(
    r"\bpython3?(?:\.\d+)?\s+-c\b[^\n|&]*\b(?:write|write_text|write_bytes|replace|unlink|rename|shutil)\b"
)


def _shell_segment_mutates_files(seg: str) -> bool:
    if not seg or "craftsman-stop-gate" in seg or "craftsman gate" in seg:
        return False
    # Strip descriptor redirections (2>&1, >&2) before checking for file redirection
    cleaned = re.sub(r"\d*>&\d+|\d*>>?\s*/dev/null\b", " ", seg)
    return bool(SHELL_FILE_MUTATION_RE.search(cleaned))


def _shell_command_mutates_files(cmd: str) -> bool:
    if not cmd:
        return False
    if PYTHON_INLINE_WRITE_RE.search(cmd):
        return True
    unquoted = QUOTED_SPAN_RE.sub(" ", cmd)
    parts = re.split(r"[;\n]|&&|\|\|", unquoted)
    return any(_shell_segment_mutates_files(part.strip()) for part in parts if part.strip())


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


def _evaluate_auditor_report_text(text: str) -> bool:
    """Return True if auditor report text represents a failed audit verdict."""
    final_matches = list(CRAFTSMAN_FINAL_VERDICT_RE.finditer(text))
    if final_matches:
        last_verdict = final_matches[-1].group(1).lower()
        return not last_verdict.startswith("pass")

    if AUDIT_SECTION_FAIL_RE.search(text):
        return True
    return False


def _has_audit_failure_after(entries: list[dict[str, Any]], after_idx: int) -> bool:
    """Return True if the auditor response following `entries[after_idx]` is still pending or reports a failure."""
    subagent_conv_ids: set[str] = set()
    ack_seen = False
    passed_first_planner = False
    system_replies: list[str] = []
    generic_replies: list[str] = []
    generic_error_seen = False

    for entry in entries[after_idx + 1 :]:
        etype = entry.get("type")
        content = entry.get("content")
        content_str = content if isinstance(content, str) else ""

        if (
            not passed_first_planner
            and etype in ("INVOKE_SUBAGENT", "GENERIC")
            and "Created the following subagent" in content_str
        ):
            ack_seen = True
            for cid in SUBAGENT_ACK_CONV_ID_RE.findall(content_str):
                if cid:
                    subagent_conv_ids.add(cid)
            continue

        if etype == "SYSTEM_MESSAGE" and content_str:
            if subagent_conv_ids:
                if any(f"sender={cid}" in content_str for cid in subagent_conv_ids):
                    system_replies.append(content_str)
            elif not passed_first_planner:
                system_replies.append(content_str)
            continue

        if etype == "PLANNER_RESPONSE":
            passed_first_planner = True
            continue

        if etype == "GENERIC" and content_str and not passed_first_planner and not ack_seen:
            if entry.get("status") == "ERROR":
                generic_error_seen = True
            generic_replies.append(content_str)

    if not ack_seen and generic_error_seen:
        return True

    auditor_replies = system_replies if ack_seen else (system_replies + generic_replies)
    if not auditor_replies:
        # If Antigravity recorded the subagent creation ACK, the subagent is still running (no reply yet)
        if ack_seen:
            return True
        return False

    return any(_evaluate_auditor_report_text(reply) for reply in auditor_replies)


def has_quality_gate_subagent_in_entries(
    entries: list[dict[str, Any]], since_step: int
) -> bool:
    """Return True if a valid Quality Gate Auditor subagent was invoked after `since_step` and not invalidated."""
    last_auditor_idx: int | None = None
    for pos, entry in enumerate(entries):
        step_idx = _safe_int(entry.get("step_index"), pos + 1)
        # If task was initialized at step > 0, the auditor must be invoked strictly after that step
        if step_idx <= since_step if since_step > 0 else step_idx < 0:
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


def _resolve_git_worktree_state_file(cwd: str | None) -> Path | None:
    if not cwd or not os.path.isdir(cwd):
        return None
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            git_dir = Path(proc.stdout.strip())
            if not git_dir.is_absolute():
                git_dir = (Path(cwd) / git_dir).resolve()
            return git_dir / "craftsman" / "craftsman_state.json"
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def resolve_cli_state_file(
    raw_state_file: str | Path | None, cwd: str | None = None
) -> Path:
    raw_sf = str(raw_state_file or "").strip()
    if (
        not raw_sf
        or "$(git rev-parse" in raw_sf
        or raw_sf.replace("\\", "/") == "/craftsman/craftsman_state.json"
    ):
        fallback_dir = cwd or os.getcwd()
        return _resolve_git_worktree_state_file(fallback_dir) or (
            Path(fallback_dir).resolve() / ".craftsman" / "craftsman" / "craftsman_state.json"
        )
    return Path(raw_sf).expanduser()


def _resolve_state_file_from_payload(
    payload: dict[str, Any], artifact_dir: str, ws_cwd: str | None = None
) -> Path:
    base_dir = Path(artifact_dir).expanduser()
    if "artifactDirectoryPath" not in payload:
        wt_state = _resolve_git_worktree_state_file(ws_cwd)
        if wt_state is not None and wt_state.is_file():
            return wt_state
        plain_state = base_dir / "craftsman_state.json"
        if plain_state.is_file():
            return plain_state
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

    state_file = _resolve_state_file_from_payload(payload, artifact_dir, ws_cwd_str)
    transcript_path = resolve_transcript_for_state_file(state_file, payload, ws_cwd_str)
    entries = read_transcript_entries(transcript_path)

    latest_step = get_latest_step_index(entries)
    state = load_state(state_file)
    stop_platform = "claude" if _is_claude_stop_payload(payload) else "antigravity"
    stop_sid = (
        str(payload.get("session_id") or payload.get("sessionId") or "").strip()
        if stop_platform == "claude"
        else _extract_antigravity_session_id(payload)
    )
    if state and state.get("active", False):
        is_owner, _ = check_and_claim_owner(state, stop_platform, stop_sid)
        if not is_owner:
            return "allow", None

    completed_at = _safe_int(state.get("completed_at_step"), -1) if state else -1
    if completed_at > latest_step:
        completed_at = -1

    # Auto-detect `/craftsman` or `ultracraft` from transcript even if agent forgot `--init`
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
                mode="ultracraft" if detected_tier == "deep" else "craftsman",
                tier=detected_tier,
                cwd=ws_cwd_str,
                base_sha=auto_base_sha,
                started_at_step=trigger_step,
                auto_detected=True,
                owner={"platform": stop_platform, "session_id": stop_sid},
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
    missing_subagent = False
    if needs_transcript_auditor(state, entries, ws_cwd_str):
        state["subagent_verified"] = has_quality_gate_subagent_in_entries(entries, since_step)
        missing_subagent = not state["subagent_verified"]
    report: GateReport | None = None
    evidence_error: str | None = None
    try:
        report = evaluate_task(state, ws_cwd_str).report
    except (DiffScanError, LedgerError) as exc:
        evidence_error = str(exc)

    if report is not None and any(issue.code == "round-cap" for issue in report.issues):
        state["stage"] = "needs_human"
        _try_save_state(state_file, state)
        return "allow", None

    if report is not None and report.passed and not missing_subagent:
        mark_completed(state, get_latest_step_index(entries))
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
    header = f"[CRAFTSMAN GATE {forced + 1}/{max_cont}]"
    return "continue", continuation_reason(header, report, evidence_error, missing_subagent)


def continuation_reason(
    header: str, report: GateReport | None, evidence_error: str | None, missing_subagent: bool
) -> str:
    """Tell the agent exactly what is missing, within a size the platforms display in full."""
    lines = [
        f"{header} Task completion is blocked until the evidence ledger proves the change is "
        "reviewed and verified."
    ]
    if missing_subagent:
        lines.append(
            "Missing: an isolated reviewer — run the review lenses in a `craftsman-reviewer` "
            "subagent (invoke_subagent with Role 'Craftsman Reviewer'); inline review in the main "
            "thread does not count."
        )
    if evidence_error is not None:
        lines.append(f"Cannot evaluate the evidence ledger: {evidence_error[:MAX_ERROR_CHARS]}")
    closing = "Close the gate with `craftsman gate --complete` once `craftsman review check` passes."
    if report is not None and report.issues:
        lines.append("Next actions:")
        more = "… run `craftsman review check` for the remaining actions."
        budget = MAX_CONTINUATION_REASON_CHARS - sum(len(line) + 1 for line in (*lines, closing, more))
        for action in render_next_actions(report.issues):
            if len(action) + 1 > budget:
                lines.append(more)
                break
            lines.append(action)
            budget -= len(action) + 1
    lines.append(closing)
    return "\n".join(lines)


def run_cli(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="craftsman gate",
        description="Manage craftsman_state.json for 3-Gear Hard-Loop gating.",
    )
    parser.add_argument(
        "--state-file",
        default=None,
        help="Path to craftsman_state.json (defaults to per-worktree .git/craftsman/craftsman_state.json)",
    )
    parser.add_argument("--init", action="store_true", help="Initialize a fresh active state")
    parser.add_argument(
        "--takeover",
        action="store_true",
        help="Take over an active gate owned by another agent session",
    )
    parser.add_argument(
        "--cwd", help="Repository working directory to capture base_sha and diff stats"
    )
    parser.add_argument("--base-sha", help="Explicit base commit SHA for cumulative diff")
    parser.add_argument("--mode", default="craftsman", help="Mode name (craftsman or ultracraft)")
    parser.add_argument(
        "--tier",
        choices=VALID_TIERS,
        help="Gearbox tier: auto (default: <=40 lines & <=2 files -> quick, else standard), quick, standard, or deep",
    )
    parser.add_argument("--task", help="One-line description of the task (shown in review packets)")
    parser.add_argument("--stage", help="Set current stage name")
    parser.add_argument(
        "--await-user", action="store_true", help="Pause hard-loop while waiting for user input"
    )
    parser.add_argument("--resume", action="store_true", help="Resume hard-loop after user input")
    for flag in DEPRECATED_PASS_FLAGS:
        parser.add_argument(
            flag, action="store_true", help="Deprecated: evidence now comes from the ledger"
        )
    parser.add_argument(
        "--complete", action="store_true", help="Verify quality gate execution and deactivate loop"
    )
    args = parser.parse_args(argv)

    state_file = resolve_cli_state_file(args.state_file, args.cwd)
    existing_state = load_state(state_file)
    hint_cwd = args.cwd or (
        existing_state.get("cwd")
        if isinstance(existing_state, dict) and isinstance(existing_state.get("cwd"), str)
        else None
    )
    transcript_path = resolve_transcript_for_state_file(state_file, cwd=hint_cwd)
    entries = read_transcript_entries(transcript_path)
    latest_step = get_latest_step_index(entries)

    state = existing_state
    existing_was_active = bool(state and state.get("active", False))
    completed_at = _safe_int(state.get("completed_at_step"), -1) if state else -1
    eff_completed_at = completed_at if completed_at <= latest_step else -1
    reinit_inactive = (
        state is not None
        and not existing_was_active
        and not args.resume
        and (
            detect_untracked_craftsman_trigger(entries, eff_completed_at) is not None
            or (args.complete and latest_step > eff_completed_at)
        )
    )

    if (args.init and not args.complete) or state is None or reinit_inactive:
        caller_platform = detect_cli_platform()
        caller_sid = extract_cli_session_id(caller_platform)
        takeover_from: Any = None
        if args.init and not args.complete:
            if existing_was_active and existing_state is not None:
                existing_owner = existing_state.get("owner")
                same_owner = is_same_or_unclaimed_owner(
                    existing_owner, caller_platform, caller_sid
                )
                if not same_owner and not args.takeover:
                    owner_desc = format_gate_owner(existing_owner)
                    since_desc = existing_state.get("started_at") or "unknown time"
                    sys.stderr.write(
                        f"[CRAFTSMAN GATE] Refusing to initialize: an active gate in this work tree "
                        f"is already owned by {owner_desc} (since {since_desc}).\n"
                        "To work in parallel without interfering with that session, create a separate worktree:\n"
                        "  git worktree add ../<branch-dir> -b <branch>\n"
                        "Or, to explicitly take over the existing gate, pass --takeover:\n"
                        "  craftsman gate --init --takeover ...\n"
                    )
                    return 1
                if args.takeover:
                    takeover_from = existing_owner
            start_step = latest_step
            initial_tier = args.tier or ("deep" if args.mode == "ultracraft" else "auto")
            base_sha = args.base_sha or get_head_sha(hint_cwd)
            if existing_was_active and existing_state is not None and not args.takeover:
                prev_owner = existing_state.get("owner")
                prev_sid = (
                    str(prev_owner.get("session_id") or "")
                    if isinstance(prev_owner, dict)
                    and prev_owner.get("platform") == caller_platform
                    else ""
                )
                new_owner = {"platform": caller_platform, "session_id": caller_sid or prev_sid}
            else:
                new_owner = {"platform": caller_platform, "session_id": caller_sid}
        else:
            detected = detect_untracked_craftsman_trigger(entries, eff_completed_at)
            start_step = detected[0] if detected is not None else max(0, eff_completed_at)
            detected_tier = detected[1] if detected is not None else "auto"
            if args.complete and args.tier == "quick" and detected_tier != "quick":
                initial_tier = detected_tier
            else:
                initial_tier = args.tier or detected_tier
            base_sha = args.base_sha or resolve_auto_base_sha(
                hint_cwd, entries, start_step, transcript_path
            )
            new_owner = {"platform": caller_platform, "session_id": caller_sid}
        state = default_state(
            mode=args.mode,
            tier=initial_tier,
            cwd=hint_cwd,
            base_sha=base_sha,
            started_at_step=start_step,
            auto_detected=not bool(args.init and not args.complete),
            owner=new_owner,
        )
        if takeover_from is not None:
            state["takeover_from"] = takeover_from
            state["takeover_at"] = state["started_at"]
    elif args.tier:
        # On an already-active state file, tier is monotonically non-decreasing (cannot downgrade standard/deep -> quick)
        current_tier = str(state.get("tier") or "auto").strip().lower()
        if not existing_was_active or TIER_RANK.get(args.tier, 1) >= TIER_RANK.get(current_tier, 1):
            state["tier"] = args.tier

    for flag in DEPRECATED_PASS_FLAGS:
        if getattr(args, flag.removeprefix("--").replace("-", "_")):
            sys.stderr.write(
                f"[craftsman gate] Warning: {flag} no longer satisfies the gate; "
                "evidence comes from the ledger (`craftsman review check`).\n"
            )
    if args.task:
        state["task"] = args.task
    if args.init and not args.complete and isinstance(state.get("base_sha"), str):
        archive_ledger_for_new_base(hint_cwd, state["base_sha"])
    if args.cwd:
        state["cwd"] = str(Path(args.cwd).resolve())
    if args.stage:
        state["stage"] = args.stage
    if args.base_sha and (
        (args.init and not args.complete)
        or not existing_was_active
        or not state.get("base_sha")
    ):
        state["base_sha"] = args.base_sha
    if args.await_user:
        state["awaiting_user"] = True
    if args.resume:
        state["awaiting_user"] = False
        state["active"] = True

    closed_line: str | None = None
    if args.complete:
        since_step = _safe_int(state.get("started_at_step"), 0)
        effective_cwd = args.cwd or (
            state.get("cwd") if isinstance(state.get("cwd"), str) else None
        )
        subagent_ran = (
            has_quality_gate_subagent_in_entries(entries, since_step) if entries else False
        )

        if needs_transcript_auditor(state, entries, effective_cwd):
            if not subagent_ran:
                sys.stderr.write(
                    "[CRAFTSMAN HARD-LOOP REJECTED] Cannot close quality gate (--complete): "
                    "transcript.jsonl does not record an up-to-date 'invoke_subagent' call for an "
                    "isolated reviewer (Role: 'Craftsman Reviewer') covering the final diff. For "
                    "changes beyond the quick limits (>40 lines or >2 files), run the review lenses "
                    "in reviewer subagents as /code-review describes.\n"
                )
                return 1

        state["subagent_verified"] = subagent_ran
        try:
            evaluation = evaluate_task(state, effective_cwd)
        except (DiffScanError, LedgerError) as exc:
            sys.stderr.write(
                f"[CRAFTSMAN GATE] Cannot complete: cannot evaluate the evidence ledger: {exc}\n"
            )
            return 1
        if not evaluation.report.passed:
            sys.stderr.write(f"[CRAFTSMAN GATE] Cannot complete:\n{render_report(evaluation.report)}\n")
            return 1
        mark_completed(state, latest_step)
        closed_line = closed_gate_line(evaluation, resolve_effective_tier(state, effective_cwd))

    save_state_atomic(state_file, state)
    if closed_line is not None:
        print(closed_line)
    return 0


def mark_completed(state: dict[str, Any], completed_at_step: int) -> None:
    state["awaiting_user"] = False
    state["active"] = False
    state["stage"] = "completed"
    state["completed_at_step"] = completed_at_step


def closed_gate_line(evaluation: Evaluation, tier: Tier) -> str:
    findings = evaluation.ledger.findings
    fixed = sum(finding.resolution == "fixed" for finding in findings)
    accepted = sum(finding.resolution == "accepted" for finding in findings)
    return (
        f"Craftsman gate closed: round {len(evaluation.ledger.rounds)}, tier {tier}, "
        f"{fixed} findings fixed, {accepted} accepted."
    )


def archive_ledger_for_new_base(cwd: str | None, base_sha: str) -> None:
    """A new task with a different base commit must not inherit the previous review."""
    try:
        archived = archive_ledger_if_rebased(cwd or os.getcwd(), base_sha)
    except (DiffScanError, LedgerError) as exc:
        sys.stderr.write(f"[craftsman gate] Warning: evidence ledger not checked: {exc}\n")
        return
    for path in archived:
        sys.stderr.write(f"[craftsman gate] Archived previous review evidence to {path}\n")


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
