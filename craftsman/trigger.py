"""Recognize a user prompt that starts a Craftsman session (`/craftsman` or `ultracraft`).

A prompt triggers a session when `/craftsman` or `ultracraft` appears as a standalone token
outside quotes and backticks (or via platform command/skill metadata tags).
"""

from __future__ import annotations

import re

MAX_TASK_CHARS = 300

COMMANDS_PATTERN = (
    r"/(?:craftsman:)?ultracraft|craftsman:ultracraft|ultracraft|"
    r"/(?:craftsman:)?craftsman|craftsman:craftsman"
)
# `/craftsman: add x` ends the command at the colon; `/craftsman:code-review` is another command.
COLON_END = r":(?![\w-])"
LEADING_COMMAND_TRIGGER_RE = re.compile(
    rf"(?i)^\s*(?:/[a-z0-9_-]+\s+)*({COMMANDS_PATTERN})(?=$|[\s.,;!?]|{COLON_END})"
)
INLINE_TRIGGER_RE = re.compile(rf"(?i)(?:^|[\s(])({COMMANDS_PATTERN})(?=$|[\s.,;!?)]|{COLON_END})")
# `--quick` and `--standard` are accepted and ignored; only `--deep` changes the gate.
CRAFTSMAN_DEEP_FLAG_RE = re.compile(
    r"(?i)(?:/(?:craftsman:)?craftsman|craftsman:craftsman)(?:\s+--(?:quick|standard))*\s+--deep\b"
)
CLAUDE_COMMAND_NAME_RE = re.compile(
    r"<command-name>\s*/?(?:craftsman:)?(craftsman|ultracraft)\s*</command-name>", re.IGNORECASE
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
# The ultracraft forms come first: `/craftsman` would otherwise match the start of
# `/craftsman:ultracraft`. A colon that ends the command (`/craftsman: add x`) goes with it.
TRIGGER_TOKEN_RE = re.compile(
    r"(?i)(?<![\w/-])(?:/(?:craftsman:)?ultracraft|craftsman:ultracraft|ultracraft"
    r"|(?:/(?:craftsman:)?craftsman|craftsman:craftsman)(?:\s+--(?:quick|standard|deep))*)"
    rf"(?:{COLON_END}|(?![\w:-]))"
)


def detect_trigger(content: str) -> bool | None:
    """Whether `content` asks for a deep gate when it invokes `/craftsman` or `ultracraft`.

    None when it starts no session; otherwise True for `ultracraft` and `/craftsman --deep`.
    """
    if not content.strip():
        return None
    claude_command = CLAUDE_COMMAND_NAME_RE.search(content)
    if claude_command:
        if claude_command.group(1).lower() == "ultracraft":
            return True
        args = CLAUDE_COMMAND_ARGS_RE.search(content)
        arg_str = args.group(1).strip() if args else ""
        return _requests_deep(f"/craftsman {arg_str}", "/craftsman")

    requests = USER_REQUEST_BLOCK_RE.findall(content)
    user_text = QUOTED_SPAN_RE.sub(" ", (requests[-1] if requests else content).strip())
    leading = LEADING_COMMAND_TRIGGER_RE.search(user_text)
    if leading:
        return _requests_deep(user_text, leading.group(1))
    inline = INLINE_TRIGGER_RE.search(user_text)
    if inline:
        return _requests_deep(user_text, inline.group(1))
    if SKILL_METADATA_TRIGGER_RE.search(content):
        return _requests_deep(user_text, "/craftsman")
    return None


def _requests_deep(text: str, token: str) -> bool:
    return token.lower().endswith("ultracraft") or CRAFTSMAN_DEEP_FLAG_RE.search(text) is not None


def task_from_prompt(prompt: str) -> str:
    """The user's request without the trigger, as a one-line task description."""
    command_args = CLAUDE_COMMAND_ARGS_RE.search(prompt)
    text = f"/craftsman {command_args.group(1)}" if command_args else prompt
    return " ".join(TRIGGER_TOKEN_RE.sub(" ", text).split())[:MAX_TASK_CHARS]
