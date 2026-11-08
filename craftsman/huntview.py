"""What the review commands print about the hunt: the plan table, the launch block and the briefs.

Pure text. `prepare` shows the plan and tells the orchestrator how to launch the hunters; `brief` is the
whole instruction of one hunter (or of a verifier) and is the only thing its launch prompt has to carry.
"""

from __future__ import annotations

from collections.abc import Sequence

from craftsman.gatecheck import SlotState, SlotStatus
from craftsman.records import Finding, Round, Slot
from craftsman.text import defang_tokens

LAUNCH_PROMPT = "You are Craftsman hunter <ID>. Run `craftsman review brief <ID>` and follow it exactly."
LAUNCH_MECHANICS = (
    "Claude Code: Agent tool, subagent_type craftsman-reviewer. Copilot: custom agent craftsman-reviewer.",
    "Antigravity: invoke_subagent with TypeName self and Role craftsman-reviewer.",
)
CHECK_COMMAND = "craftsman review check"
STALE_NOTE = "stale: run `craftsman review prepare`"  # the latest round no longer matches the diff
REVIEW_ONLY_CHECK_COMMAND = "craftsman review check --review-only"
PLATFORM_LABELS = {
    "claude": "Claude Code",
    "copilot": "Copilot",
    "antigravity": "Antigravity",
    "cli": "Craftsman",
}
SIMPLICITY_NOTE = "[Claude Code: model haiku]"
STATE_LABELS: dict[SlotState, str] = {
    "done": "done",
    "running": "running",
    "dead": "stopped without finishing",
    "unlaunched": "not launched",
}
KIND_LINES = {
    "bug": "Hunt for real defects with your lenses; a candidate needs a nameable trigger and a wrong outcome.",
    "simplicity": (
        "Report only code that is harder to read, longer or more complex than it needs to be, each with the"
        " concrete simpler alternative; leave correctness to the bug hunters."
    ),
    "sweep": (
        "You are the final sweep: besides your other lenses, look for hunks that are each correct but disagree"
        " with each other, with the docs or with the config."
    ),
    "fresh": "You are a fresh, independent hunter: look at the code without anyone's earlier conclusions.",
    "variant": "You hunt the sibling bugs of one confirmed defect, described below.",
}
NO_TASK_TEXT = "(no task text recorded)"
MAX_BRIEF_SCENARIO_CHARS = 1200


def check_command(*, gate_active: bool) -> str:
    """The command that judges the review the state of this checkout accepts.

    Inside a `/craftsman` gate the full gate applies and `--review-only` is refused; outside one, the
    review-only check is the one that asks for nothing but the review. Every line that tells the reader what
    to run next asks this function.
    """
    return CHECK_COMMAND if gate_active else REVIEW_ONLY_CHECK_COMMAND


def waiting_notice(running: Sequence[str], *, gate_active: bool) -> str:
    """What a Stop hook tells the user when it lets the agent stop because craftsman subagents are still running.

    One text for the gate and the review guard: their reports wake the session; if they died, the check
    command (`check_command`) says which to launch again.
    """
    return (
        f"Craftsman: waiting for {', '.join(running)}. Their reports will wake you; "
        f"if they died, `{check_command(gate_active=gate_active)}` will say to relaunch them."
    )


def wait_and_check(check: str) -> tuple[str, ...]:
    """The last lines of the launch block: end the turn, then run `check` and do what it lists."""
    return (
        "Then end your turn while they run. When they report back, run "
        f"`{check}` and do exactly what it lists.",
    )


# --- hunks and slots -------------------------------------------------------------------------


def hunk_ranges(hunk_ids: Sequence[str]) -> str:
    """`H1-H3,H5` for the hunk ids: runs of consecutive numbers as ranges, the way `--hunks` reads them."""
    numbers = sorted({int(hunk_id[1:]) for hunk_id in hunk_ids})
    runs: list[tuple[int, int]] = []
    for number in numbers:
        if runs and runs[-1][1] + 1 == number:
            runs[-1] = (runs[-1][0], number)
        else:
            runs.append((number, number))
    return ",".join(f"H{first}" if first == last else f"H{first}-H{last}" for first, last in runs)


def lens_text(slot: Slot) -> str:
    """The lenses of `slot`, with a note on what kind of slot it is when the lenses do not say."""
    lenses = ", ".join(slot.lenses)
    if slot.kind == "simplicity":
        return f"{lenses} (simplicity)"
    if slot.kind == "fresh":
        return f"{lenses} (fresh look)"
    if slot.kind == "variant":
        return f"{lenses} (variant of {slot.trigger})"
    return lenses


def plan_lines(statuses: Sequence[SlotStatus], *, with_state: bool) -> list[str]:
    """The plan table: one line per slot with its id, hunks, lenses and changed lines (and where it stands)."""
    rows = [
        (
            status.slot.id,
            hunk_ranges(status.slot.hunks),
            lens_text(status.slot),
            f"{status.slot.changed_lines} line{'' if status.slot.changed_lines == 1 else 's'}",
            _row_note(status, with_state=with_state),
        )
        for status in statuses
    ]
    widths = [max(len(row[column]) for row in rows) for column in range(4)] if rows else [0] * 4
    return [
        "  " + "  ".join(row[column].ljust(widths[column]) for column in range(4)) + (f"  {row[4]}" if row[4] else "")
        for row in rows
    ]


def _row_note(status: SlotStatus, *, with_state: bool) -> str:
    notes = [f"[{STATE_LABELS[status.state]}]"] if with_state else []
    if status.slot.kind == "simplicity":
        notes.append(SIMPLICITY_NOTE)
    return " ".join(notes)


def launch_lines(slot_ids: Sequence[str], *, check: str) -> list[str]:
    """The block that tells the orchestrator to launch `slot_ids` now, all in one step, then run `check`."""
    noun = "hunter" if len(slot_ids) == 1 else "hunters"
    return [
        f"Launch {'all ' if len(slot_ids) > 1 else ''}{len(slot_ids)} {noun} now, in one step. "
        "Each is a craftsman-reviewer subagent whose whole prompt is:",
        f"  {LAUNCH_PROMPT}",
        *(f"  {line}" for line in LAUNCH_MECHANICS),
        *wait_and_check(check),
    ]


def slot_kind_title(slot: Slot) -> str:
    return {
        "bug": "bug slot",
        "simplicity": "simplicity slot",
        "sweep": "sweep slot",
        "fresh": "fresh slot",
        "variant": "variant slot",
    }[slot.kind]


# --- the hunter brief ------------------------------------------------------------------------


def hunter_brief(
    *,
    slot: Slot,
    round_: Round,
    hunters: int,
    trigger: Finding | None,
    role_text: str | None,
    antigravity: bool,
) -> str:
    """The whole instruction of the hunter of `slot`.

    `hunters` is how many hunters the slot's generation has. `trigger` is the finding a variant slot
    hunts the siblings of. `role_text` is the reviewer role text, for readers that did not load the agent
    definition themselves. Nothing here resembles what a recording command prints, so a transcript reader
    never mistakes this text for a record.
    """
    ranges = hunk_ranges(slot.hunks)
    lenses = ",".join(slot.lenses)
    lines = [
        f"You are Craftsman hunter {slot.id} ({slot_kind_title(slot)}, generation {slot.generation}): one of "
        f"{hunters} hunter{'s' if hunters != 1 else ''} of this round. Cover only your slot; the other slots belong"
        " to other hunters.",
        KIND_LINES[slot.kind],
        "",
        f"Task: {defang_tokens(round_.task.strip()) or NO_TASK_TEXT}",
        f"Base ref: {round_.base_sha}",
        "",
        f"Your slice is {len(slot.hunks)} hunk(s), {slot.changed_lines} changed lines: {ranges}. Read only your slice:",
        f"  craftsman review view --hunks {ranges}",
        f"Your lenses: {', '.join(slot.lenses)}. Load their playbooks:",
        f"  craftsman skill show craft-review --lenses {lenses}",
    ]
    lines += _wave_lines(slot, trigger)
    lines += [
        "",
        "Recording (as you go, not in one batch at the end):",
        f'- A candidate defect: craftsman review add --hunter {slot.id} --lens <one of your lenses> --file <path>'
        ' --line <n> --severity <blocker|major|minor|nit> --title "<one line>"'
        ' --scenario "<trigger -> wrong outcome>" --suggestion "<fix>"',
        f'  Several at once: craftsman review add --from-json - (JSON entries with "hunter": "{slot.id}").',
        f'- What you examined: craftsman review cover --hunter {slot.id} --hunks <ids you examined> --note "<what you'
        ' examined and which values you tried, one line>"',
        f'  Several at once: craftsman review cover --from-json - (entries with "hunter": "{slot.id}").',
        f"- Every hunk of your slot ({ranges}) must be covered before you finish.",
        "- Never run `craftsman review verdict`, `merge`, `prepare` or `reset`: verifiers judge, the orchestrator"
        " plans. Never change the work tree or the index.",
        "",
        "Finish: reply with a 2-3 line summary (lenses, findings by id and title, anything you could not examine).",
    ]
    if antigravity:
        lines.append(
            "On Antigravity, send that summary to your parent with send_message (Recipient: the conversation"
            " that launched you); a plain reply is not seen."
        )
    return _with_role(lines, role_text)


def _wave_lines(slot: Slot, trigger: Finding | None) -> list[str]:
    """The extra instruction of a variant or fresh slot."""
    if slot.kind == "fresh":
        return [
            "",
            "You are a fresh, independent hunter. Do not read other findings (skip `craftsman review show`): an"
            " unanchored second look is the point of this slot.",
        ]
    if slot.kind != "variant":
        return []
    lines = [""]
    if trigger is not None:
        scenario = defang_tokens(" ".join(trigger.scenario.split())[:MAX_BRIEF_SCENARIO_CHARS])
        lines.append(
            f"The defect: {trigger.id} ({trigger.severity}, lens {trigger.lens}) at {defang_tokens(trigger.path)}:"
            f"{trigger.line} - {defang_tokens(trigger.title)}. Scenario: {scenario}"
        )
    else:
        lines.append(f"The defect: {defang_tokens(str(slot.trigger))}; `craftsman review show` has its details.")
    lines.append(
        "Hunt the same root cause across the diff and the code it touches: the same mistake in other places,"
        " callers that depend on it, and the same pattern in sibling functions."
    )
    return lines


# --- the verifier brief ----------------------------------------------------------------------


def verifier_brief(
    *, findings: Sequence[Finding], round_: Round | None, base_sha: str, task: str, role_text: str | None
) -> str:
    """The whole instruction of a verifier for `findings` (their text comes from the ledger)."""
    ids = ",".join(finding.id for finding in findings)
    lines = [
        f"You are an independent Craftsman verifier for {ids}. You did not write this code and you did not hunt"
        " these findings: be skeptical in both directions.",
        "",
        f"Task: {defang_tokens(task.strip()) or NO_TASK_TEXT}",
        f"Base ref: {base_sha}" + (f" (round {round_.number})" if round_ is not None else ""),
        "",
        "Findings to judge:",
    ]
    for finding in findings:
        scenario = defang_tokens(" ".join(finding.scenario.split())[:MAX_BRIEF_SCENARIO_CHARS])
        lines += [
            f"- {finding.id} ({finding.severity}, lens {finding.lens}) {defang_tokens(finding.path)}:{finding.line}"
            f" - {defang_tokens(finding.title)}",
            f"  Scenario: {scenario}",
        ]
    lines += [
        "",
        "How to judge:",
        "- First merge duplicates, only when one fix removes every consequence, and before any verdict:"
        ' craftsman review merge <TARGET> <DUPLICATES...> --scenario "<root cause -> effects>"',
        "- Re-read the code around each cited file:line yourself, with its callers; do not trust the description."
        " Then try to disprove it: a guard, unreachability, a misreading, or intent.",
        "- Reproduce blocker and major findings whenever the code can run:"
        " craftsman run --kind repro -- <command>. Never dismiss flakiness or platform dependence on one green run.",
        "- Record each verdict (the only way your judgment counts):",
        '  craftsman review verdict <ID> --as proven --run <R#> --evidence "<what the run printed and why it is wrong>"',
        '  craftsman review verdict <ID> --as probable --evidence "static: <file:line chain>" (or "no-repro: <why>; <chain>")',
        '  craftsman review verdict <ID> --as question --evidence "intent: <source says X>; <file:line>"',
        '  craftsman review verdict <ID> --as dismissed --evidence "<guard quoted, file:line>" [--run <R#>]',
        '  Several at once: craftsman review verdict --from-json - (entries {"id","as","evidence","run"}).',
        "- If the severity is wrong, start the evidence with `[severity: <new>]` and say why.",
        "- Never edit the work tree or the index; reproductions live in a `craftsman review scratch` directory."
        " Never run `prepare`, `reset`, `add` or `cover`.",
        "",
        "Finish with a table: finding id, verdict, one-line reason.",
    ]
    return _with_role(lines, role_text)


def _with_role(lines: list[str], role_text: str | None) -> str:
    if role_text:
        lines += ["", "--- Role text ---", role_text.strip()]
    return "\n".join(lines) + "\n"
