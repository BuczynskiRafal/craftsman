"""Recognize a user prompt that starts a Craftsman session (`/craftsman` or `ultracraft`).

A prompt triggers a session when `/craftsman` or `ultracraft` appears as a standalone token
outside quotes and backticks (or via platform command/skill metadata tags).
"""

from __future__ import annotations

import re

from craftsman.gate import GateTier

MAX_TASK_CHARS = 300
FLAG_TIERS: dict[str, GateTier] = {"quick": "quick", "standard": "standard", "deep": "deep"}

LEADING_COMMAND_TRIGGER_RE = re.compile(
    r"(?i)^\s*(?:/[a-z0-9_-]+\s+)*(/craftsman|ultracraft)(?=$|[\s.,:;!])"
)
INLINE_TRIGGER_RE = re.compile(r"(?i)(?:^|[\s(])(/craftsman|ultracraft)(?=$|[\s.,:;!?)])")
CRAFTSMAN_FLAG_TIER_RE = re.compile(r"(?i)/craftsman(?:\s+--(quick|standard|deep))\b")
CLAUDE_COMMAND_NAME_RE = re.compile(
    r"<command-name>\s*/?(craftsman|ultracraft)\s*</command-name>", re.IGNORECASE
)
CLAUDE_COMMAND_ARGS_RE = re.compile(r"<command-args>(.*?)</command-args>", re.DOTALL | re.IGNORECASE)
SKILL_METADATA_TRIGGER_RE = re.compile(r"<SKILL>[^<]*skills/craftsman/SKILL\.md</SKILL>", re.IGNORECASE)
USER_REQUEST_BLOCK_RE = re.compile(
    r"<USER_REQUEST>((?:(?!</?USER_REQUEST>).)*?)</USER_REQUEST>", re.DOTALL | re.IGNORECASE
)
# Quoted or backticked mentions; English contractions (don't, it's) are not quotes.
QUOTED_SPAN_RE = re.compile(
    r"(?<!`)(`+)(?!`)[\s\S]*?(?<!`)\1(?!`)|\"[^\"]*\"|(?<![A-Za-z0-9])'[^'\n]*'(?![A-Za-z0-9])"
)
TRIGGER_TOKEN_RE = re.compile(
    r"(?i)(?<![\w/-])(?:/craftsman(?:\s+--(?:quick|standard|deep))?|ultracraft)(?![\w-])"
)


def detect_trigger(content: str) -> GateTier | None:
    """The tier `content` asks for when it invokes `/craftsman` or `ultracraft`, else None."""
    if not content.strip():
        return None
    claude_command = CLAUDE_COMMAND_NAME_RE.search(content)
    if claude_command:
        if claude_command.group(1).lower() == "ultracraft":
            return "deep"
        args = CLAUDE_COMMAND_ARGS_RE.search(content)
        return _requested_tier(f"/craftsman {args.group(1).strip() if args else ''}", "/craftsman")

    requests = USER_REQUEST_BLOCK_RE.findall(content)
    user_text = QUOTED_SPAN_RE.sub(" ", (requests[-1] if requests else content).strip())
    leading = LEADING_COMMAND_TRIGGER_RE.search(user_text)
    if leading:
        return _requested_tier(user_text, leading.group(1))
    inline = INLINE_TRIGGER_RE.search(user_text)
    if inline:
        return _requested_tier(user_text, inline.group(1))
    if SKILL_METADATA_TRIGGER_RE.search(content):
        return _requested_tier(user_text, "/craftsman")
    return None


def _requested_tier(text: str, token: str) -> GateTier:
    if token.lower() == "ultracraft":
        return "deep"
    flag = CRAFTSMAN_FLAG_TIER_RE.search(text)
    if flag is None:
        return "auto"
    return FLAG_TIERS[flag.group(1).lower()]


def task_from_prompt(prompt: str) -> str:
    """The user's request without the trigger, as a one-line task description."""
    command_args = CLAUDE_COMMAND_ARGS_RE.search(prompt)
    text = f"/craftsman {command_args.group(1)}" if command_args else prompt
    return " ".join(TRIGGER_TOKEN_RE.sub(" ", text).split())[:MAX_TASK_CHARS]
