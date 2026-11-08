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
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from craftsman.records import (
    AGENT_ROLES,
    PLUGIN_AGENT_PREFIX,
    AgentRole,
    agent_role,
    format_timestamp,
    parse_timestamp,
)
from craftsman.trigger import USER_REQUEST_BLOCK_RE, is_review_prompt
from craftsman.trigger import review_request as prompt_review_request

Entry = dict[str, Any]

SUBAGENT_ACK_MARKER = "Created the following subagent:"
LOG_DIR = Path(".system_generated") / "logs"
TRANSCRIPT_NAMES = ("transcript.jsonl", "transcript_full.jsonl")
KILLED_STATE = "SUBAGENT_STATE_KILLED"
EARLY_ENTRIES = 5  # a subagent's transcript opens with the message of its parent
SENDER_RE = re.compile(r"\bsender=(\S+)")
QUESTION_TOOLS = frozenset({"ask_question", "AskUserQuestion"})
NATIVE_TYPES = frozenset({"USER_INPUT", "PLANNER_RESPONSE", "GENERIC", "SYSTEM_MESSAGE"})


class TranscriptError(Exception):
    """A transcript exists but cannot be read."""


def read_transcript_entries(transcript_path: Path | None) -> list[Entry]:
    """The entries of a JSONL transcript.

    No path or a missing file yields no entries: the session has not written one yet. A last line
    without its newline is one the agent is still writing and is left out; any other line that is
    not a JSON object, or a file that cannot be read, raises `TranscriptError`, since a skipped
    entry could hide what the stop hook must see.
    """
    if transcript_path is None:
        return []
    try:
        text = transcript_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except (OSError, UnicodeDecodeError) as exc:
        raise TranscriptError(f"cannot read the transcript {transcript_path}: {exc}") from exc
    lines = text.split("\n")
    complete, partial = lines[:-1], lines[-1]
    entries = [
        _normalize(_json_object(line, transcript_path, index), index)
        for index, line in enumerate(complete, start=1)
        if line.strip()
    ]
    last = _json_object_or_none(partial) if partial.strip() else None
    if last is not None:  # a finished entry without its newline
        entries.append(_normalize(last, len(lines)))
    return entries


def _json_object_or_none(line: str) -> Entry | None:
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _json_object(line: str, path: Path, line_number: int) -> Entry:
    try:
        obj = json.loads(line)
    except json.JSONDecodeError as exc:
        raise TranscriptError(f"{path}:{line_number} is not JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise TranscriptError(f"{path}:{line_number} is not a JSON object")
    return obj


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


def decode_arg(value: Any) -> str:
    """A tool argument as text, whichever transcript wrote it.

    `transcript.jsonl` JSON-encodes the values of `tool_calls[].args` (`"\\"self\\""`), `transcript_full.jsonl` keeps
    native values (`"self"`); both read as `self`. Anything that is not text reads as its `str`, None as "".
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        return str(value)
    if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return value
        if isinstance(decoded, str):
            return decoded
    return value


def _spelled_role(name: str) -> AgentRole | None:
    """The Craftsman role a `Role` or `TypeName` text names: an agent name (plugin prefix, case and padding
    ignored), alone or followed by a space or punctuation (`craftsman-reviewer W1-1`)."""
    text = name.strip().lower().removeprefix(PLUGIN_AGENT_PREFIX)
    if (role := agent_role(text)) is not None:
        return role
    for agent, agent_role_ in AGENT_ROLES.items():
        if text.startswith(agent) and not text[len(agent)].isalnum():
            return agent_role_
    return None


def launch_role(role: Any, type_name: Any) -> AgentRole | None:
    """The Craftsman role of an `invoke_subagent` call, None for any other subagent.

    The agent must be writable (`TypeName` `self`, none, or itself a Craftsman agent name): a read-only type
    cannot record anything. `Role` names the role; a `TypeName` that is an agent name does as well.
    """
    kind = decode_arg(type_name).strip().lower()
    by_type = _spelled_role(kind)
    if kind not in ("", "self") and by_type is None:
        return None
    return _spelled_role(decode_arg(role)) or by_type


@dataclass(frozen=True)
class SubagentLaunch:
    """One subagent the conversation started: the conversation it runs in and when the platform acknowledged it."""

    conversation_id: str
    role: AgentRole
    launched_at: str  # the whole-second `created_at` of the acknowledgement
    log_path: Path | None


def subagent_launches(entries: Sequence[Entry]) -> tuple[SubagentLaunch, ...]:
    """The Craftsman subagents the transcript's conversation launched, in call order.

    A response that calls tools is followed by one `GENERIC` entry per call, in call order (the results of
    other tools interleave: the k-th result belongs to the k-th call, whatever the tool). An `invoke_subagent`
    call is paired with the k-th `GENERIC` entry after its response; its text names the new conversation. A
    call whose acknowledgement is missing (still running), unreadable or without a time, or whose role is not a
    Craftsman role, is skipped.
    """
    launches: list[SubagentLaunch] = []
    for position, entry in enumerate(entries):
        calls = entry.get("tool_calls") if entry.get("type") == "PLANNER_RESPONSE" else None
        if not isinstance(calls, list) or entry.get("status") == "ERROR":
            continue
        results = _results_after(entries, position)
        for index, call in enumerate(calls):
            if not isinstance(call, dict) or call.get("name") != "invoke_subagent" or index >= len(results):
                continue
            raw_args = call.get("args")
            args: Entry = raw_args if isinstance(raw_args, dict) else {}
            role = launch_role(args.get("Role"), args.get("TypeName"))
            created = _ack_created(results[index])
            if role is None or created is None:
                continue
            launches.append(SubagentLaunch(created[0], role, created[1], created[2]))
    return tuple(launches)


def _results_after(entries: Sequence[Entry], position: int) -> list[Entry]:
    """The `GENERIC` entries after the response at `position`, up to the next response: its tool results in call order."""
    results: list[Entry] = []
    for entry in entries[position + 1 :]:
        if entry.get("type") == "PLANNER_RESPONSE":
            break
        if entry.get("type") == "GENERIC":
            results.append(entry)
    return results


def _ack_created(entry: Entry) -> tuple[str, str, Path | None] | None:
    """`(conversation id, created_at, log path)` of a subagent acknowledgement, None when it is none or lacks a part."""
    content = entry.get("content")
    created_at = entry.get("created_at")
    if not isinstance(content, str) or not isinstance(created_at, str) or not _is_timestamp(created_at):
        return None
    marker = content.find(SUBAGENT_ACK_MARKER)
    brace = content.find("{", marker) if marker >= 0 else -1
    if brace < 0:
        return None
    try:
        info, _ = json.JSONDecoder().raw_decode(content[brace:])
    except json.JSONDecodeError:
        return None
    conversation = info.get("conversationId") if isinstance(info, dict) else None
    if not isinstance(conversation, str) or not conversation.strip():
        return None
    uri = info.get("logAbsoluteUri")
    path = Path(uri.removeprefix("file://")) if isinstance(uri, str) and uri.startswith("file://") else None
    return conversation.strip(), created_at.strip(), path


def _is_timestamp(text: str) -> bool:
    try:
        parse_timestamp(text.strip())
    except ValueError:
        return False
    return True


@dataclass(frozen=True)
class SubagentState:
    """Where a subagent stands, as far as its own transcript and its parent's registry tell.

    `finished_at` is the raw `created_at` of the end of its turn (a killed subagent: when the registry file was
    written); `last_step` is the `step_index` of the last entry of its transcript (None without one), which a
    subagent that was sent back compares with the step it was sent back at; `readable` is false when no transcript
    of it could be read.
    """

    finished_at: str | None = None
    killed: bool = False
    readable: bool = False
    last_step: int | None = None


def conversation_transcript(app_dir: Path, conversation_id: str) -> Path | None:
    """The transcript file of a conversation under `app_dir` (the compact one, else the full one), None if neither exists."""
    for name in TRANSCRIPT_NAMES:
        path = app_dir / "brain" / conversation_id / LOG_DIR / name
        if path.is_file():
            return path
    return None


def _registry_file(app_dir: Path, parent_id: str, conversation_id: str) -> Path:
    return app_dir / "brain" / parent_id / ".system_generated" / "subagents" / f"{conversation_id}.json"


def subagent_finish(app_dir: Path, conversation_id: str, parent_id: str) -> SubagentState:
    """The state of the subagent `conversation_id` launched by `parent_id`.

    Finished when the LAST entry of its transcript is a `PLANNER_RESPONSE` with status DONE and no tool calls
    (the registry keeps saying ALIVE after that). Killed when the parent's registry says
    `SUBAGENT_STATE_KILLED`, which is final. An unreadable or missing transcript leaves `readable` false.
    """
    killed_at = _killed_at(_registry_file(app_dir, parent_id, conversation_id))
    path = conversation_transcript(app_dir, conversation_id)
    try:
        entries = read_transcript_entries(path)
    except TranscriptError:
        entries = []
        path = None
    finished = _finished_at(entries[-1]) if entries else None
    last_step = step_of(entries[-1], len(entries)) if entries else None
    if killed_at is not None:
        return SubagentState(killed_at, True, path is not None, last_step)
    return SubagentState(finished, False, path is not None, last_step)


def _killed_at(registry: Path) -> str | None:
    try:
        info = json.loads(registry.read_text(encoding="utf-8"))
        if isinstance(info, dict) and info.get("state") == KILLED_STATE:
            return format_timestamp(datetime.fromtimestamp(registry.stat().st_mtime, timezone.utc))
    except (OSError, ValueError):
        pass
    return None


def _finished_at(entry: Entry) -> str | None:
    """When the turn that `entry` (the last of a transcript) ends was over, None when it does not end one."""
    created = entry.get("created_at")
    calls = entry.get("tool_calls")
    if (
        entry.get("type") == "PLANNER_RESPONSE"
        and entry.get("status") == "DONE"
        and not calls
        and isinstance(created, str)
        and _is_timestamp(created)
    ):
        return created.strip()
    return None


def parent_conversation(app_dir: Path, conversation_id: str, *, path: Path | None = None) -> str | None:
    """The conversation that launched `conversation_id`, None when it is a main conversation (or unreadable).

    A subagent's transcript has no `USER_INPUT` and one of its first entries is the message of its parent: a
    `SYSTEM_MESSAGE` that holds `[Message]` and `sender=<parent id>`. `path` is the transcript when the caller
    knows it (a hook payload's `transcriptPath`). The file is read line by line and the scan ends at the first
    `USER_INPUT` (a main conversation opens with one), so a long main transcript costs one line.
    """
    source = path or conversation_transcript(app_dir, conversation_id)
    if source is None:
        return None
    sender: str | None = None
    try:
        with source.open(encoding="utf-8") as lines:
            for position, line in enumerate(lines):
                entry = _json_object_or_none(line) if line.strip() else None
                if entry is None:
                    continue
                if entry.get("type") == "USER_INPUT":
                    return None
                content = entry.get("content")
                if sender is None and position < EARLY_ENTRIES and entry.get("type") == "SYSTEM_MESSAGE":
                    found = SENDER_RE.search(content) if isinstance(content, str) and "[Message]" in content else None
                    sender = found.group(1) if found else None
    except (OSError, UnicodeDecodeError):
        return None
    return sender


def subagent_role(app_dir: Path, conversation_id: str, parent_id: str) -> AgentRole | None:
    """The Craftsman role the parent launched `conversation_id` with: its registry file, else the parent's transcript."""
    try:
        info = json.loads(_registry_file(app_dir, parent_id, conversation_id).read_text(encoding="utf-8"))
        descriptor = info.get("subagentDescriptor") if isinstance(info, dict) else None
        if isinstance(descriptor, dict) and (role := launch_role(descriptor.get("role"), descriptor.get("typeName"))):
            return role
    except (OSError, ValueError):
        pass
    try:
        entries = read_transcript_entries(conversation_transcript(app_dir, parent_id))
    except TranscriptError:
        return None
    return next((item.role for item in subagent_launches(entries) if item.conversation_id == conversation_id), None)


def latest_user_input_text(entries: Sequence[Entry]) -> tuple[int, str | None, str] | None:
    """The step, `created_at` (None when the entry has none or an unreadable one) and text of the most recent user input."""
    for position in range(len(entries), 0, -1):
        entry = entries[position - 1]
        if entry.get("type") == "USER_INPUT":
            created = entry.get("created_at")
            stamp = created.strip() if isinstance(created, str) and _is_timestamp(created) else None
            return step_of(entry, position), stamp, str(entry.get("content") or "")
    return None


def request_body(text: str) -> str:
    """What the user typed in a `USER_INPUT`: the last `<USER_REQUEST>` block Antigravity wraps it in, else `text`."""
    blocks = USER_REQUEST_BLOCK_RE.findall(text)
    return blocks[-1] if blocks else text


def review_request(text: str) -> tuple[bool, str] | None:
    """`(deep, task)` when `text` is a `/craft-review` request, else None (the prompt hook's own reading)."""
    return prompt_review_request(text) if is_review_prompt(text) else None
