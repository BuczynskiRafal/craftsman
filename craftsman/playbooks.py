"""Slice `skills/code-review/lenses.md` down to the playbooks of the assigned review lenses."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

from craftsman.lenses import LENSES, REVIEWER_GROUPS, get_lens

CODE_REVIEW_SKILL = "code-review"
LENSES_FILENAME = "lenses.md"

_BACKTICK_FENCE_RE = re.compile(r"^ {0,3}(`{3,})[^`]*$")
_TILDE_FENCE_RE = re.compile(r"^ {0,3}(~{3,}).*$")
_LEVEL2_HEADING_RE = re.compile(r"^##(?!#)")
_SECTION_HEADING_RE = re.compile(r"^## `([a-z][a-z0-9_-]*)` — \S.*$")
_PLAYBOOK_TARGETS: dict[str, tuple[str, ...]] = {
    **REVIEWER_GROUPS,
    **{lens.id: (lens.id,) for lens in LENSES},
}


def _next_fence_state(
    line: str, fence: tuple[str, int] | None
) -> tuple[tuple[str, int] | None, bool]:
    """Update Markdown code-fence state and return `(new_fence, is_inside_fence)`."""
    if fence is not None:
        char, width = fence
        if re.match(rf"^ {{0,3}}{char}{{{width},}}[ \t]*$", line):
            return None, True
        return fence, True
    match = _BACKTICK_FENCE_RE.match(line) or _TILDE_FENCE_RE.match(line)
    if match is not None:
        token = match.group(1)
        return (token[0], len(token)), True
    return None, False


def _parse_section_heading(line: str) -> str:
    """Extract `<id>` from a level-2 `## `<id>` — <title>` heading, or raise `ValueError`."""
    match = _SECTION_HEADING_RE.match(line.rstrip())
    if match is None:
        raise ValueError(f"malformed lens heading {line!r} (expected '## `<id>` — <title>')")
    return match.group(1)


def parse_playbook_sections(lenses_md: str) -> tuple[str, Mapping[str, str]]:
    """Split `lenses.md` into `(preamble, {lens_id: section_markdown})` on `## `<id>` — ...` headings."""
    preamble_lines: list[str] = []
    sections: dict[str, str] = {}
    current_id: str | None = None
    current_lines: list[str] = []
    fence: tuple[str, int] | None = None
    for line in lenses_md.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        fence, in_fence = _next_fence_state(line, fence)
        if not in_fence and _LEVEL2_HEADING_RE.match(line):
            if current_id is not None:
                sections[current_id] = "\n".join(current_lines).strip()
            lens_id = _parse_section_heading(line)
            if lens_id in sections:
                raise ValueError(f"duplicate playbook section for lens {lens_id!r}")
            current_id, current_lines = lens_id, [line.rstrip()]
        elif current_id is None:
            preamble_lines.append(line)
        else:
            current_lines.append(line)
    if fence is not None:
        raise ValueError("unclosed code fence in playbooks markdown")
    if current_id is not None:
        sections[current_id] = "\n".join(current_lines).strip()
    if not sections:
        raise ValueError("playbooks markdown contains no lens sections")
    return "\n".join(preamble_lines).strip(), sections


def _valid_targets_hint() -> str:
    lenses = ", ".join(lens.id for lens in LENSES)
    groups = ", ".join(REVIEWER_GROUPS)
    return f"valid lenses: {lenses}; groups: {groups}"


def expand_playbook_lenses(raw: str) -> tuple[str, ...]:
    """Expand a CSV of lens ids and `R1`..`R4` groups into unique lens ids in `LENSES` order."""
    tokens = [item.strip() for item in raw.split(",") if item.strip()]
    if not tokens:
        raise ValueError(
            f"--lenses must name at least one lens or reviewer group ({_valid_targets_hint()})"
        )
    unknown = next((token for token in tokens if token not in _PLAYBOOK_TARGETS), None)
    if unknown is not None:
        raise ValueError(
            f"unknown lens or reviewer group {unknown!r} ({_valid_targets_hint()})"
        )
    chosen = {lens_id for token in tokens for lens_id in _PLAYBOOK_TARGETS[token]}
    return tuple(lens.id for lens in LENSES if lens.id in chosen)


def lens_playbooks(lenses_md: str, lens_ids: Sequence[str]) -> str:
    """Render the shared preamble once followed by the requested lens sections in `LENSES` order."""
    if isinstance(lens_ids, str):
        raise TypeError("lens_ids must be a sequence of lens ids, not str")
    if not lens_ids:
        raise ValueError("lens_ids must name at least one lens")
    requested = {get_lens(lens_id).id for lens_id in lens_ids}
    preamble, sections = parse_playbook_sections(lenses_md)
    ordered_ids = [lens.id for lens in LENSES if lens.id in requested]
    missing = [lens_id for lens_id in ordered_ids if lens_id not in sections]
    if missing:
        raise ValueError(f"missing playbook section(s) for: {', '.join(missing)}")
    parts = [sections[lens_id] for lens_id in ordered_ids]
    if preamble:
        parts.insert(0, preamble)
    return "\n\n".join(parts) + "\n"
