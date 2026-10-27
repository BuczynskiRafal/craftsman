"""Agent transcripts (Antigravity and Claude Code JSONL) read into one entry shape.

Every entry is a dict with a `step_index` and a `type`: `USER_INPUT` (with `content`),
`PLANNER_RESPONSE` (with `tool_calls`, each a dict with `name` and `args`), `GENERIC` (tool output
in `content`) or `SYSTEM_MESSAGE`. Antigravity writes this shape natively; Claude Code entries are
converted to it.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from craftsman.records import agent_role

Entry = dict[str, Any]

SUBAGENT_ACK_MARKER = "Created the following subagent"
SUBAGENT_ID_RE = re.compile(r'"conversationId"\s*:\s*"([^"]+)"')
SENDER_RE = re.compile(r"\bsender=(\S+)")
QUESTION_TOOLS = frozenset({"ask_question", "AskUserQuestion"})
NATIVE_TYPES = frozenset({"USER_INPUT", "PLANNER_RESPONSE", "GENERIC", "SYSTEM_MESSAGE"})


class TranscriptError(Exception):
    """A transcript exists but cannot be read."""


def read_transcript_entries(transcript_path: Path | None) -> list[Entry]:
    """The entries of a JSONL transcript; lines that are not JSON objects are skipped.

    No path or a missing file yields no entries: the session has not written one yet. Any other
    failure to read the file raises `TranscriptError`.
    """
    if transcript_path is None:
        return []
    entries: list[Entry] = []
    try:
        with transcript_path.open("r", encoding="utf-8", errors="replace") as handle:
            for index, line in enumerate(handle, start=1):
                obj = _json_object(line)
                if obj is not None:
                    entries.append(_normalize(obj, index))
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise TranscriptError(f"cannot read the transcript {transcript_path}: {exc}") from exc
    return entries


def _json_object(line: str) -> Entry | None:
    if not line.strip():
        return None
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _normalize(obj: Entry, line_number: int) -> Entry:
    """Convert a Claude Code entry to the Antigravity shape; native entries pass through."""
    if "step_index" in obj or obj.get("type") in NATIVE_TYPES:
        return obj
    message = obj.get("message")
    if not isinstance(message, dict):
        return obj
    if obj.get("type") == "user":
        return _claude_user_entry(message.get("content"), line_number)
    if obj.get("type") == "assistant":
        return _claude_assistant_entry(message.get("content"), line_number)
    return obj


def _claude_user_entry(content: Any, line_number: int) -> Entry:
    texts: list[str] = []
    results: list[str] = []
    if isinstance(content, str):
        texts.append(content)
    elif isinstance(content, list):
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text":
                texts.append(str(item.get("text") or ""))
            elif item.get("type") == "tool_result" and (result := _tool_result_text(item)):
                results.append(result)
    if results and not texts:
        return {"step_index": line_number, "type": "GENERIC", "content": "\n".join(results)}
    return {"step_index": line_number, "type": "USER_INPUT", "content": "\n".join(texts + results)}


def _tool_result_text(item: Entry) -> str:
    raw = item.get("content")
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):
        return "\n".join(
            str(sub.get("text") or "")
            for sub in raw
            if isinstance(sub, dict) and sub.get("type") == "text"
        )
    return ""


def _claude_assistant_entry(content: Any, line_number: int) -> Entry:
    calls: list[Entry] = []
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "tool_use":
                arguments = item.get("input") if isinstance(item.get("input"), dict) else {}
                calls.append({"name": item.get("name"), "input": arguments, "args": arguments})
    return {"step_index": line_number, "type": "PLANNER_RESPONSE", "tool_calls": calls}


def claude_project_slug(cwd: str) -> str:
    """The directory name Claude Code gives the project rooted at `cwd` under `~/.claude/projects`."""
    try:
        resolved = str(Path(cwd).expanduser().resolve())
    except OSError:
        resolved = cwd
    return re.sub(r"[^A-Za-z0-9-]", "-", resolved)


def step_of(entry: Entry, position: int) -> int:
    """The entry's `step_index`, or its 1-based position when it has none."""
    raw = entry.get("step_index")
    return raw if isinstance(raw, int) and not isinstance(raw, bool) else position


def latest_user_input(entries: Sequence[Entry]) -> tuple[int, str] | None:
    """The step and text of the most recent user input, or None when there is none."""
    for position in range(len(entries), 0, -1):
        entry = entries[position - 1]
        if entry.get("type") == "USER_INPUT":
            return step_of(entry, position), str(entry.get("content") or "")
    return None


def is_waiting_on_question(entries: Sequence[Entry]) -> bool:
    """True when the agent's latest action was a question to the user that is still unanswered."""
    for entry in reversed(entries):
        if entry.get("type") == "USER_INPUT":
            return False
        if entry.get("type") == "PLANNER_RESPONSE":
            calls = entry.get("tool_calls")
            if not isinstance(calls, list):
                return False
            return any(isinstance(call, dict) and call.get("name") in QUESTION_TOOLS for call in calls)
    return False


def finished_reviewers(entries: Sequence[Entry], after_step: int) -> tuple[str, ...]:
    """Conversation ids of `craftsman-reviewer` subagents invoked after `after_step` that finished.

    Antigravity records a subagent run in the parent transcript in three steps: the
    `invoke_subagent` call, an acknowledgement listing the new conversation ids in the order the
    subagents were requested, and a `SYSTEM_MESSAGE` from `sender=<conversation id>` when the
    subagent finishes. A subagent counts only once all three are present.
    """
    reviewer_ids: set[str] = set()
    finished: list[str] = []
    awaiting_ack: tuple[bool, ...] = ()
    for position, entry in enumerate(entries, start=1):
        kind = entry.get("type")
        raw_content = entry.get("content")
        content = raw_content if isinstance(raw_content, str) else ""
        if kind == "PLANNER_RESPONSE":
            awaiting_ack = _requested_reviewers(entry) if step_of(entry, position) > after_step else ()
        elif awaiting_ack and SUBAGENT_ACK_MARKER in content:
            ids = SUBAGENT_ID_RE.findall(content)
            if len(ids) == len(awaiting_ack):
                reviewer_ids.update(
                    sub_id for sub_id, is_reviewer in zip(ids, awaiting_ack, strict=True) if is_reviewer
                )
            awaiting_ack = ()
        elif kind == "SYSTEM_MESSAGE" and (sender := SENDER_RE.search(content)):
            sub_id = sender.group(1)
            if sub_id in reviewer_ids and sub_id not in finished:
                finished.append(sub_id)
    return tuple(finished)


def _requested_reviewers(entry: Entry) -> tuple[bool, ...]:
    """For each subagent an `invoke_subagent` call in `entry` requests: is it a Craftsman reviewer?"""
    if entry.get("status") == "ERROR":
        return ()
    specs: list[Any] = []
    for call in entry.get("tool_calls") or []:
        if isinstance(call, dict) and call.get("name") == "invoke_subagent":
            specs.extend(_subagent_specs(call.get("args")))
    return tuple(isinstance(spec, dict) and _is_reviewer_spec(spec) for spec in specs)


def _subagent_specs(args: Any) -> list[Any]:
    """The subagent specs of one call: a `Subagents` list (or its JSON text), or the args themselves."""
    if not isinstance(args, dict):
        return []
    if "Subagents" not in args:
        return [{key: _unquote(value) for key, value in args.items()}]
    raw = args["Subagents"]
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return []
    return raw if isinstance(raw, list) else []


def _unquote(value: Any) -> Any:
    """Antigravity may write a top-level spec field as JSON text (`'"self"'`)."""
    if isinstance(value, str) and len(value) >= 2 and value.startswith('"') and value.endswith('"'):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _is_reviewer_spec(spec: dict[str, Any]) -> bool:
    """A writable (`self`) subagent whose role names the Craftsman reviewer agent."""
    role = str(spec.get("Role") or "")
    return (
        str(spec.get("TypeName") or "self").strip().lower() == "self" and agent_role(role) == "reviewer"
    )
