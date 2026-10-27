"""Recognize a user prompt that starts a Craftsman session (`/craftsman` or `ultracraft`).

A prompt triggers a session when it invokes the command, not when it talks about it: questions,
explanations, negations and quoted mentions do not count.
"""

from __future__ import annotations

import re

from craftsman.gate import GateTier

MAX_TASK_CHARS = 300
FLAG_TIERS: dict[str, GateTier] = {"quick": "quick", "standard": "standard", "deep": "deep"}

LEADING_COMMAND_TRIGGER_RE = re.compile(
    r"(?i)^\s*(?:/[a-z0-9_-]+\s+)*(/craftsman|ultracraft)(?=$|[\s.,:;!])"
)
INLINE_TRIGGER_RE = re.compile(r"(?i)(?:^|\s)(/craftsman|ultracraft)(?=$|[\s.,!:])")
CRAFTSMAN_FLAG_TIER_RE = re.compile(r"(?i)/craftsman(?:\s+--(quick|standard|deep))\b")
CLAUDE_COMMAND_NAME_RE = re.compile(
    r"<command-name>\s*/?(craftsman|ultracraft)\s*</command-name>", re.IGNORECASE
)
CLAUDE_COMMAND_ARGS_RE = re.compile(r"<command-args>(.*?)</command-args>", re.DOTALL | re.IGNORECASE)
CONVERSATIONAL_OR_NEGATED_PREFIX_RE = re.compile(
    r"(?i)\b(explain|what\s+is|what\s+does|how\s+does|describe|tell\s+me\s+about|"
    r"wyja[sś]nij|opowiedz\s+o|co\s+to|jak\s+dzia[łl]a|"
    r"do\s+not|don't\s+want|don't\s+use|not\s+want|without|avoid|never\s+use|"
    r"nie\s+chc[eę]|nie\s+u[żz]ywaj|bez)\b"
)
NON_IMPERATIVE_ULTRACRAFT_RE = re.compile(r"(?i)\bultracraft\s+(?:does|is|works|keyword|mode|rule)\b")
SKILL_METADATA_TRIGGER_RE = re.compile(r"<SKILL>[^<]*skills/craftsman/SKILL\.md</SKILL>", re.IGNORECASE)
USER_REQUEST_BLOCK_RE = re.compile(
    r"<USER_REQUEST>((?:(?!</?USER_REQUEST>).)*?)</USER_REQUEST>", re.DOTALL | re.IGNORECASE
)
# Quoted or backticked mentions; English contractions (don't, it's) are not quotes.
QUOTED_SPAN_RE = re.compile(r"`[^`]*`|\"[^\"]*\"|(?<![A-Za-z0-9])'[^'\n]*'(?![A-Za-z0-9])")
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
    if leading and not _is_ultracraft_question(leading.group(1), _first_line(user_text)):
        return _requested_tier(user_text, leading.group(1))
    if SKILL_METADATA_TRIGGER_RE.search(content):
        return _requested_tier(user_text, "/craftsman")
    return next(
        (tier for line in user_text.splitlines() if (tier := _inline_trigger(line.strip())) is not None),
        None,
    )


def _inline_trigger(line: str) -> GateTier | None:
    """A trigger inside an imperative line ("do it with /craftsman"), not in a question."""
    if not line or "?" in line:
        return None
    match = INLINE_TRIGGER_RE.search(line)
    if match is None or CONVERSATIONAL_OR_NEGATED_PREFIX_RE.search(line[: match.start()]):
        return None
    token = match.group(1)
    if token.lower() == "ultracraft" and NON_IMPERATIVE_ULTRACRAFT_RE.search(line):
        return None
    return _requested_tier(line, token)


def _is_ultracraft_question(token: str, first_line: str) -> bool:
    return token.lower() == "ultracraft" and (
        NON_IMPERATIVE_ULTRACRAFT_RE.search(first_line) is not None or "?" in first_line
    )


def _first_line(text: str) -> str:
    lines = text.splitlines()
    return lines[0].strip() if lines else text


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
