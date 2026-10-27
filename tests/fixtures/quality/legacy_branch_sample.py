"""Reduced fixture copied from `fix/copilot-dedup-and-search-scope` before Part A."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any


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
    if ex_sid:
        return bool(in_sid) and in_sid == ex_sid and platform in (ex_plat, "cli")
    return ex_plat == platform


def check_and_claim_owner(
    state: dict[str, Any] | None, platform: str, session_id: str
) -> tuple[bool, bool]:
    """Check whether `(platform, session_id)` owns `state`, claiming an empty `session_id` if matching."""
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
    return (bool(in_sid) and in_sid == ex_sid), False


def _evaluate_shell_command_scope(
    command_line: str,
    cwd: Path,
    repo_root: Path,
    allowed_roots: tuple[Path, ...],
) -> tuple[str, str]:
    """Pre-Part-A shell parser function exceeding the 40-line function budget."""
    if not command_line or not command_line.strip():
        return "allow", ""
    segments = [seg.strip() for seg in command_line.split(";") if seg.strip()]
    for seg in segments:
        tokens = seg.split()
        if not tokens:
            continue
        exe = os.path.basename(tokens[0]).lower()
        if exe in ("rg", "ripgrep"):
            idx = 1
            positional: list[str] = []
            while idx < len(tokens):
                tok = tokens[idx]
                if tok == "--":
                    positional.extend(tokens[idx + 1 :])
                    break
                if tok in ("-e", "--regexp", "-f", "--file", "-g", "--glob"):
                    idx += 2
                    continue
                if tok.startswith("-"):
                    idx += 1
                    continue
                positional.append(tok)
                idx += 1
            targets = positional[1:] if len(positional) > 1 else ["."]
            for raw_target in targets:
                candidate = (cwd / raw_target).resolve()
                if not candidate.is_relative_to(repo_root):
                    return "deny", f"rg target {raw_target} escapes {repo_root}"
        elif exe in ("find", "fd", "fdfind"):
            for tok in tokens[1:]:
                if not tok.startswith("-"):
                    candidate = (cwd / tok).resolve()
                    if not candidate.is_relative_to(repo_root) and not any(
                        candidate.is_relative_to(root) for root in allowed_roots
                    ):
                        return "deny", f"{exe} target {tok} escapes {repo_root}"
    return "allow", ""
