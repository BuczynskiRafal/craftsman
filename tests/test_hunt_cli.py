"""End-to-end tests for the hunt commands of `craftsman review`: prepare's plan, briefs, tagged records."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest import mock

from craftsman import review
from craftsman.gate import GateOwner
from craftsman.huntplan import credited
from craftsman.ledger import add_agent_event, load, mutate, set_verdict
from craftsman.records import AgentEvent, Ledger, Round, format_timestamp, parse_timestamp
from craftsman.usage import _COVERAGE_TOKEN_RE, _FINDING_TOKEN_RE
from craftsman.workspace import HOOKS_MARKER, ReviewMarker, read_marker, write_marker

from tests.ambient import AMBIENT_PLATFORM_ENV_VARS
from tests.support import (
    EvidenceRepo,
    assert_cli_error,
    coverage_note,
    make_slot,
    open_gate,
    run_main,
)

CART = '''def total(items):
    """Sum of prices."""
    return sum(item["price"] for item in items) / len(items)
'''
CART_EDITED = CART.replace("/ len(items)", "/ max(len(items), 1)")
READER = "def load(path):\n    with open(path) as handle:\n        return handle.read()\n"
SHELL_OUT = "import subprocess\n\n\ndef go(cmd):\n    subprocess.run(cmd, shell=True)\n"
NOTE = "read the slice and tried an empty list and a single item"
FINDING_FLAGS = (
    "--file", "cart.py", "--line", "3", "--lens", "contracts", "--severity", "major",
    "--title", "Empty cart divides by zero",
    "--scenario", "total([]) evaluates sum([]) / len([]) and raises ZeroDivisionError",
)  # fmt: skip
STATIC = "static: cart.py:3 divides by len(items), which raises ZeroDivisionError on []"
LEFT = "hunk(s) left in your records (`craftsman review check` decides)."
CLAUDE = {"CLAUDECODE": "1"}
AGY = {"ANTIGRAVITY_CONVERSATION_ID": "conv-9"}


class HuntTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.repo = EvidenceRepo(self.tmp / "repo")

    @property
    def ledger_path(self) -> Path:
        return self.repo.ledger_dir / "ledger.json"

    def review(
        self,
        *args: str,
        check: bool = True,
        stdin: str | None = None,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return self.repo.cli("review", *args, check=check, stdin=stdin, env_extra=env)

    def ledger(self) -> Ledger:
        return load(self.ledger_path)

    def latest(self) -> Round:
        return self.ledger().rounds[-1]

    def slot_ids(self) -> list[str]:
        plan = self.latest().plan
        assert plan is not None
        return [slot.id for slot in plan.slots]

    def arm(
        self,
        *,
        platform: str = "claude",
        seconds_ago: float = 0,
        deep: bool = False,
        task: str = "",
        guarding: bool = True,
        abandoned_at: str | None = None,
    ) -> None:
        touched = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
        started = format_timestamp(datetime.now(timezone.utc) - timedelta(hours=1))
        marker = ReviewMarker(
            platform, "s1", started, guarding, touched_at=format_timestamp(touched), deep=deep, task=task,
            abandoned_at=abandoned_at,
        )  # fmt: skip
        write_marker(self.ledger_path, marker)

    def write_cart(self) -> None:
        self.repo.write("cart.py", CART)

    def write_cart_and_reader(self) -> None:
        self.repo.write("cart.py", CART)
        self.repo.write("reader.py", READER)

    def assert_error(self, proc: subprocess.CompletedProcess[str], code: int, fragment: str) -> None:
        assert_cli_error(self, proc, code, fragment)

    def assert_no_record_shapes(self, text: str) -> None:
        """Nothing a hunter's transcript reader would take for a recording command's output."""
        self.assertIsNone(_COVERAGE_TOKEN_RE.search(text), text)
        self.assertIsNone(_FINDING_TOKEN_RE.search(text), text)
        self.assertIsNone(re.search(r"verdict recorded \(V\d+\)", text), text)
        self.assertIsNone(re.search(r"Brief issued \(B\d+\)", text), text)

    def finish_hunter(self, slot: str, hunks: str | None = None, *, briefed: bool = True) -> None:
        """Run the hunter of `slot` as the hooks would see it: brief, cover, then a start and stop event.

        `briefed=False` skips the brief (which plans the waves that are due, as every judging command does).
        """
        # A hunter starts after its round was prepared (a run from before it is no hunter of it), so the start
        # is a second ago at the earliest, but never before the round.
        prepared = parse_timestamp(self.latest().prepared_at)
        started = format_timestamp(max(datetime.now(timezone.utc) - timedelta(seconds=1), prepared))
        claim = None
        if briefed:
            claim = re.search(r"Brief issued \((B\d+)\)", self.review("brief", slot).stdout)
            assert claim is not None
        ids = hunks or ",".join(next(s for s in self.latest().plan.slots if s.id == slot).hunks)  # type: ignore[union-attr]
        cover = self.review(
            "cover", "--hunter", slot, "--hunks", ids, "--note", coverage_note(ids.split(","))
        )
        recorded = re.search(r"Coverage recorded \((C\d+)\)", cover.stdout)
        assert recorded is not None
        stopped = format_timestamp(datetime.now(timezone.utc) + timedelta(seconds=1))
        agent_id = f"agent-{slot}"
        self.add_events(
            AgentEvent("claude", "start", "craftsman-reviewer", agent_id, started),
            AgentEvent(
                "claude", "stop", "craftsman-reviewer", agent_id, stopped,
                records=((claim.group(1),) if claim is not None else ()) + (recorded.group(1),),
            ),
        )  # fmt: skip

    def finish_generation_one(self, *, briefed: bool = True) -> None:
        """Every generation 1 slot of the latest round hunted to the end, so that its waves are due."""
        plan = self.latest().plan
        assert plan is not None
        for slot in plan.slots:
            if slot.generation == 1:
                self.finish_hunter(slot.id, briefed=briefed)

    def drop_planned_waves(self) -> None:
        """The latest round as a writer that does not plan waves leaves it: a wave may be due, none is stored.

        Every recording command stores the waves that are due, so the last `cover` of a hunt already does.
        """

        def drop(current: Ledger) -> tuple[Ledger, None]:
            latest = current.rounds[-1]
            assert latest.plan is not None
            plan = replace(
                latest.plan, slots=tuple(s for s in latest.plan.slots if s.generation == 1), waves=()
            )
            return replace(current, rounds=(*current.rounds[:-1], replace(latest, plan=plan))), None

        mutate(self.ledger_path, drop)

    def add_events(self, *events: AgentEvent) -> None:
        def record(current: Ledger) -> tuple[Ledger, None]:
            for event in events:
                current = add_agent_event(current, event)
            return current, None

        mutate(self.ledger_path, record)


class PrepareEnforcementTests(HuntTestCase):
    def test_without_a_hook_the_round_is_advisory_and_still_has_a_plan(self) -> None:
        self.write_cart()

        out = self.review("prepare").stdout

        self.assertIn(
            "Advisory: no Craftsman hook saw this session, so the review will be SELF-REVIEWED", out
        )
        self.assertIn("(run `craftsman doctor`)", out)
        round_ = self.latest()
        self.assertEqual((round_.enforced, round_.platform), (False, "cli"))
        self.assertEqual(self.slot_ids(), ["W1-1", "W1-2", "W1-3"])

    def test_a_marker_the_hooks_just_touched_makes_the_round_enforced(self) -> None:
        self.write_cart()
        self.arm()

        out = self.review("prepare").stdout

        self.assertIn(
            "Enforced: Claude Code hooks serve this review; finishing requires every slot below.", out
        )
        self.assertNotIn("Advisory", out)
        self.assertEqual((self.latest().enforced, self.latest().platform), (True, "claude"))

    def test_each_hooked_platform_names_itself(self) -> None:
        names = {"copilot": "Copilot", "antigravity": "Antigravity"}
        for platform, name in names.items():
            with self.subTest(platform=platform):
                self.review("reset")
                self.write_cart()
                self.arm(platform=platform)
                self.assertIn(f"Enforced: {name} hooks serve this review", self.review("prepare").stdout)
                self.assertEqual(self.latest().platform, platform)

    def test_a_stale_marker_leaves_the_round_advisory(self) -> None:
        self.write_cart()
        self.arm(seconds_ago=300)

        out = self.review("prepare").stdout

        self.assertIn("Advisory:", out)
        self.assertFalse(self.latest().enforced)

    def test_a_stale_marker_leaks_neither_its_depth_nor_its_task_into_any_round(self) -> None:
        self.write_cart()
        self.arm(seconds_ago=300, deep=True, task="OLD TASK")

        seen = []
        for command in (("prepare",), ("prepare", "--task", "X"), ("reset",), ("prepare",)):
            self.review(*command)
            if command[0] == "prepare":
                seen.append(self.latest())

        self.assertEqual(len(seen), 3)
        for round_ in seen:
            self.assertEqual((round_.enforced, round_.deep), (False, False))
            self.assertNotEqual(round_.task, "OLD TASK")
        self.assertEqual(seen[-1].task, "")

    def test_a_marker_the_hooks_just_touched_lends_its_depth_and_its_task_to_the_round(self) -> None:
        self.write_cart()
        self.arm(seconds_ago=0, deep=True, task="the cart totals")

        out = self.review("prepare").stdout

        self.assertIn("Depth: deep", out)
        self.assertEqual(
            (self.latest().enforced, self.latest().deep, self.latest().task), (True, True, "the cart totals")
        )

    def test_a_gate_whose_platform_proved_isolation_enforces_the_round(self) -> None:
        open_gate(
            self.repo.root,
            self.repo.base_sha,
            owner=GateOwner("claude", "sess-1"),
            isolation_evidence="hooks",
        )
        self.write_cart()

        out = self.review("prepare", env={**CLAUDE, "CLAUDE_CODE_SESSION_ID": "sess-1"}).stdout

        self.assertIn("Enforced: Claude Code hooks", out)
        self.assertTrue(self.latest().enforced)

    def test_the_upgrade_of_a_round_to_enforced_releases_a_self_reviewed_hunk_and_rewrites_the_packet(self) -> None:
        self.write_cart_and_reader()
        self.review("prepare")  # advisory round 1: H1 is cart.py
        self.review("cover", "--hunks", "H1", "--note", NOTE)  # the orchestrator reviews it itself
        self.repo.write("reader.py", READER + "\n\ndef close(handle):\n    handle.close()\n")
        self.review("prepare")  # advisory round 2: cart.py is carried
        packet = Path(self.latest().packet_path)
        self.assertIn("(1 carried)", packet.read_text(encoding="utf-8"))
        self.assertEqual({ref.path: ref.carried for ref in self.latest().hunks}, {"cart.py": True, "reader.py": False})
        self.arm()

        out = self.review("prepare").stdout

        self.assertIn("Enforced:", out)
        latest = self.latest()
        self.assertEqual((latest.number, latest.enforced), (2, True))
        self.assertEqual({ref.path: ref.carried for ref in latest.hunks}, {"cart.py": False, "reader.py": False})
        self.assertIn("(0 carried)", packet.read_text(encoding="utf-8"))
        assert latest.plan is not None
        cart = next(ref.id for ref in latest.hunks if ref.path == "cart.py")
        self.assertTrue(any(cart in slot.hunks and slot.kind == "bug" for slot in latest.plan.slots))

    def test_enforcement_is_sticky_across_rounds(self) -> None:
        self.write_cart()
        self.arm()
        self.review("prepare")
        (self.repo.ledger_dir / HOOKS_MARKER).unlink()
        self.repo.write("cart.py", CART_EDITED)

        out = self.review("prepare").stdout

        self.assertIn("Enforced: Claude Code hooks", out)
        self.assertEqual(
            (self.latest().number, self.latest().enforced, self.latest().platform), (2, True, "claude")
        )

    def test_an_advisory_round_becomes_enforced_when_the_hooks_now_see_the_review(self) -> None:
        self.write_cart()
        self.review("prepare")
        before = self.slot_ids()
        self.arm()

        out = self.review("prepare").stdout

        self.assertIn("Round 1 unchanged (same diff)", out)
        self.assertIn("Enforced:", out)
        self.assertTrue(self.latest().enforced)
        self.assertEqual(self.slot_ids(), before)

    def test_the_marker_keeps_guarding_after_prepare(self) -> None:
        self.write_cart()
        self.arm()

        self.review("prepare")

        marker = read_marker(self.ledger_path)
        assert marker is not None
        self.assertTrue(marker.guarding)


class PrepareTaskTests(HuntTestCase):
    def prepared_task(self) -> str:
        return self.latest().task

    def test_the_task_flag(self) -> None:
        self.write_cart()

        self.review("prepare", "--task", "Add cart totals")

        self.assertEqual(self.prepared_task(), "Add cart totals")
        self.assertIn(
            "**Task:** Add cart totals",
            (self.repo.ledger_dir / "packet-1.md").read_text(encoding="utf-8"),
        )

    def test_the_task_file_and_stdin(self) -> None:
        self.write_cart()
        path = self.tmp / "task.txt"
        path.write_text("  Add cart totals\n\nwith tax\n", encoding="utf-8")

        self.review("prepare", "--task-file", str(path))
        self.assertEqual(self.prepared_task(), "Add cart totals\n\nwith tax")

        self.review("reset")
        self.write_cart()
        self.review("prepare", "--task-file", "-", stdin="From stdin\n")
        self.assertEqual(self.prepared_task(), "From stdin")

    def test_a_long_task_file_is_cut_at_4000_characters(self) -> None:
        self.write_cart()
        path = self.tmp / "task.txt"
        path.write_text("x" * 5000, encoding="utf-8")

        self.review("prepare", "--task-file", str(path))

        self.assertEqual(len(self.prepared_task()), 4000)

    def test_both_flags_or_a_missing_file_are_errors(self) -> None:
        self.write_cart()

        both = self.review("prepare", "--task", "a", "--task-file", "b", check=False)
        missing = self.review("prepare", "--task-file", str(self.tmp / "nope.txt"), check=False)

        self.assert_error(both, 1, "pass either --task or --task-file")
        self.assert_error(missing, 1, "cannot read")
        self.assertFalse(self.ledger_path.exists())

    def test_the_marker_task_is_used_and_a_flag_beats_it(self) -> None:
        self.write_cart()
        self.arm(task="Task from the prompt")
        self.review("prepare")
        self.assertEqual(self.prepared_task(), "Task from the prompt")

        self.review("reset")
        self.write_cart()
        self.arm(task="Task from the prompt")
        self.review("prepare", "--task", "Explicit")
        self.assertEqual(self.prepared_task(), "Explicit")

    def test_the_gate_task_is_used(self) -> None:
        self.repo.cli("gate", "--init", "--task", "Gate task")
        self.write_cart()

        self.review("prepare")

        self.assertEqual(self.prepared_task(), "Gate task")

    def test_a_later_round_inherits_the_task_and_a_reprepare_keeps_it(self) -> None:
        self.write_cart()
        self.review("prepare", "--task", "Add cart totals")

        self.review("prepare")
        self.assertEqual(self.prepared_task(), "Add cart totals")

        self.repo.write("cart.py", CART_EDITED)
        self.review("prepare")
        self.assertEqual((self.latest().number, self.prepared_task()), (2, "Add cart totals"))

    def test_a_new_explicit_task_reaches_the_packet_of_an_unchanged_round(self) -> None:
        self.write_cart()
        self.review("prepare", "--task", "first task text")

        self.review("prepare", "--task", "second task text")

        packet = (self.repo.ledger_dir / "packet-1.md").read_text(encoding="utf-8")
        self.assertIn("second task text", packet)
        self.assertNotIn("first task text", packet)

    def test_a_new_explicit_task_replaces_the_one_of_an_unchanged_round(self) -> None:
        self.write_cart()
        self.review("prepare", "--task", "Old")

        self.review("prepare", "--task", "New")

        self.assertEqual((self.latest().number, self.prepared_task()), (1, "New"))


class PrepareDepthTests(HuntTestCase):
    def test_a_deep_prompt_makes_the_round_deep(self) -> None:
        self.write_cart()
        self.arm(deep=True)

        out = self.review("prepare").stdout

        self.assertIn("Depth: deep (a deep review was requested)\n", out)
        self.assertTrue(self.latest().deep)
        self.assertIn("W2-X1", self.slot_ids())

    def test_a_marker_that_no_longer_guards_asks_for_nothing(self) -> None:
        self.write_cart()
        self.arm(deep=True, guarding=False)

        self.assertIn("Depth: normal", self.review("prepare").stdout)

    def test_a_signal_raises_the_gate_the_caller_owns(self) -> None:
        self.repo.cli("gate", "--init")
        self.repo.write("run.py", SHELL_OUT)

        out = self.review("prepare").stdout

        self.assertIn("Gate raised to deep: a passing end-to-end run is now required.", out)
        self.assertIn("Depth: deep (process signal in H1)", out)
        self.assertTrue(self.repo.state()["deep"])

    def test_a_foreign_gate_is_never_raised_but_the_round_is_still_deep(self) -> None:
        open_gate(self.repo.root, self.repo.base_sha, owner=GateOwner("claude", "other"))
        self.repo.write("run.py", SHELL_OUT)

        out = self.review("prepare", "--deep", env={**CLAUDE, "CLAUDE_CODE_SESSION_ID": "mine"}).stdout

        self.assertIn("Note: --deep ignored: the active gate belongs to another session", out)
        self.assertNotIn("Gate raised", out)
        self.assertIn("Depth: deep (process signal in H1)", out)
        self.assertFalse(self.repo.state()["deep"])

    def test_raising_the_depth_of_an_unchanged_round_appends_slots_and_keeps_the_old_ones(self) -> None:
        self.write_cart_and_reader()
        self.review("prepare")
        before = self.latest().plan
        assert before is not None

        out = self.review("prepare", "--deep").stdout

        after = self.latest().plan
        assert after is not None
        self.assertIn("Round 1 raised to deep (same diff)", out)
        self.assertEqual(after.slots[: len(before.slots)], before.slots)
        added = [slot.id for slot in after.slots[len(before.slots) :]]
        self.assertTrue(added)
        self.assertIn("W2-X1", added)
        self.assertTrue(all(slot_id not in {slot.id for slot in before.slots} for slot_id in added))
        self.assertIn(f"New slots since the last prepare: {', '.join(added)}", out)


class PrepareReprepareTests(HuntTestCase):
    def test_an_unchanged_reprepare_changes_no_slot_and_launches_everything_not_started(self) -> None:
        self.write_cart()
        self.review("prepare")
        before = self.ledger_path.read_bytes()

        out = self.review("prepare").stdout

        self.assertIn("Round 1 unchanged (same diff)", out)
        self.assertEqual(out.count("[not launched]"), 3)
        self.assertIn("Launch all 3 hunters now", out)
        self.assertEqual(self.ledger_path.read_bytes(), before)

    def test_a_slot_with_a_fetched_brief_and_a_running_hunter_is_not_launched_again(self) -> None:
        self.write_cart()
        self.review("prepare")
        started = format_timestamp(datetime.now(timezone.utc))
        self.add_events(AgentEvent("claude", "start", "craftsman-reviewer", "a1", started))
        self.review("brief", "W1-1")

        out = self.review("prepare").stdout

        self.assertRegex(out, r"W1-1 .*\[running\]")
        self.assertIn("Running: W1-1 - do not launch these again", out)
        self.assertIn("Launch all 2 hunters now", out)

    def test_when_every_slot_is_done_nothing_is_launched(self) -> None:
        self.write_cart()
        self.arm()
        self.review("prepare")
        for slot in self.slot_ids():
            self.finish_hunter(slot)
        self.arm()

        out = self.review("prepare").stdout

        self.assertEqual(out.count("[done]"), 3)
        self.assertIn("Every slot is done. Run `craftsman review check --review-only`", out)
        self.assertNotIn("Launch", out)

    def test_prepare_output_never_looks_like_a_recording(self) -> None:
        self.write_cart_and_reader()
        self.assert_no_record_shapes(self.review("prepare").stdout)
        self.assert_no_record_shapes(self.review("prepare", "--deep").stdout)
        self.assert_no_record_shapes(self.review("prepare").stdout)


class CarryOverTests(HuntTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.repo.write("cart.py", CART)
        self.repo.write("notes.py", "LABEL = 'cart'\n")
        self.arm()
        self.review("prepare")

    def owed_by_slot_hunk(self, hunk_id: str) -> set[str]:
        """The lenses the notes hunk still owes: those of the plan's slots minus what round 1 credited."""
        digest = next(ref.digest for ref in self.latest().hunks if ref.id == hunk_id)
        return self.lenses_of(hunk_id) - credited(self.ledger())[digest]

    def lenses_of(self, hunk_id: str) -> set[str]:
        plan = self.latest().plan
        assert plan is not None
        return {lens for slot in plan.slots if hunk_id in slot.hunks for lens in slot.lenses}

    def test_after_a_partial_hunt_a_changed_diff_owes_the_lenses_not_yet_credited(self) -> None:
        self.finish_hunter("W1-1")
        self.repo.write("cart.py", CART_EDITED)
        self.arm()

        out = self.review("prepare").stdout

        notes = next(ref.id for ref in self.latest().hunks if ref.path == "notes.py")
        self.assertIn("0 hunks carried, 2 to review.", out)
        self.assertTrue({"contracts", "failure", "craft", "design"} <= self.lenses_of(notes))
        digest = next(ref.digest for ref in self.latest().hunks if ref.id == notes)
        self.assertEqual(credited(self.ledger())[digest], {"intent", "ripple", "tests"})
        self.assertEqual(
            self.owed_by_slot_hunk(notes), {"contracts", "failure", "craft", "design", "sweep"}
        )

    def test_without_that_hunt_nothing_is_credited_and_every_lens_is_owed(self) -> None:
        self.repo.write("cart.py", CART_EDITED)
        self.arm()

        self.review("prepare")

        notes = next(ref for ref in self.latest().hunks if ref.path == "notes.py")
        self.assertNotIn(notes.digest, credited(self.ledger()))
        self.assertTrue({"intent", "ripple", "tests"} <= self.lenses_of(notes.id))

    def test_after_a_full_hunt_the_unchanged_hunk_is_carried(self) -> None:
        for slot in self.slot_ids():
            self.finish_hunter(slot)
        self.repo.write("cart.py", CART_EDITED)
        self.arm()

        out = self.review("prepare").stdout

        self.assertIn("1 hunks carried, 1 to review.", out)
        notes = next(ref.id for ref in self.latest().hunks if ref.path == "notes.py")
        self.assertEqual(self.lenses_of(notes), set())


class AdvisoryCarryOverTests(HuntTestCase):
    """An advisory round keeps the 5.0 rules; an enforced round never takes its unvouched coverage as a hunt."""

    def setUp(self) -> None:
        super().setUp()
        self.repo.write("cart.py", CART)
        self.repo.write("notes.py", "LABEL = 'cart'\n")
        self.review("prepare")
        self.assertFalse(self.latest().enforced)

    def test_tagged_coverage_of_an_advisory_round_is_carried_over(self) -> None:
        self.review("cover", "--hunter", "W1-1", "--hunks", "H1-H2", "--note", NOTE)
        self.repo.write("cart.py", CART_EDITED)

        out = self.review("prepare").stdout

        self.assertIn("1 hunks carried, 1 to review.", out)

    def test_untagged_coverage_of_an_advisory_round_is_carried_over(self) -> None:
        self.review("cover", "--hunks", "H1-H2", "--note", NOTE)
        self.repo.write("cart.py", CART_EDITED)

        self.assertIn("1 hunks carried, 1 to review.", self.review("prepare").stdout)

    def test_an_enforced_round_does_not_take_the_self_review_of_an_advisory_one(self) -> None:
        self.review("cover", "--hunks", "H1-H2", "--note", NOTE)
        self.repo.write("cart.py", CART_EDITED)
        self.arm()

        out = self.review("prepare").stdout

        self.assertIn("0 hunks carried, 2 to review.", out)
        self.assertTrue(self.latest().enforced)
        notes = next(ref.id for ref in self.latest().hunks if ref.path == "notes.py")
        self.assertFalse(next(ref for ref in self.latest().hunks if ref.id == notes).carried)
        plan = self.latest().plan
        assert plan is not None
        self.assertTrue(any(notes in slot.hunks for slot in plan.slots))

    def test_tagged_coverage_of_an_advisory_round_is_no_hunt_for_an_enforced_one_either(self) -> None:
        self.review("cover", "--hunter", "W1-1", "--hunks", "H1-H2", "--note", NOTE)
        self.repo.write("cart.py", CART_EDITED)
        self.arm()

        self.assertIn("0 hunks carried, 2 to review.", self.review("prepare").stdout)

    def test_an_advisory_round_that_the_hooks_take_over_stops_crediting_its_self_review(self) -> None:
        self.review("cover", "--hunks", "H1-H2", "--note", NOTE)
        self.arm()

        self.review("prepare")  # unchanged diff: the round becomes enforced

        statuses = json.loads(self.review("check", "--review-only", "--json", check=False).stdout)["slots"]
        self.assertTrue(self.latest().enforced)
        self.assertEqual({slot["state"] for slot in statuses}, {"unlaunched"})


class BriefTests(HuntTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.write_cart_and_reader()
        self.review("prepare", "--task", "Add cart totals")

    def test_a_brief_records_a_claim_and_says_so_first(self) -> None:
        out = self.review("brief", "W1-1", env=CLAUDE).stdout
        again = self.review("brief", "W1-2", env=CLAUDE).stdout

        self.assertTrue(out.startswith("Brief issued (B1) for W1-1, round 1.\n"))
        self.assertTrue(again.startswith("Brief issued (B2) for W1-2, round 1.\n"))
        claims = self.ledger().claims
        self.assertEqual(
            [(c.slot, c.token, c.round, c.by) for c in claims],
            [("W1-1", "B1", 1, None), ("W1-2", "B2", 1, None)],
        )

    def test_the_brief_carries_the_assignment(self) -> None:
        out = self.review("brief", "W1-1", env=CLAUDE).stdout

        base = self.ledger().rounds[0].base_sha
        self.assertIn(
            "You are Craftsman hunter W1-1 (bug slot, generation 1): one of 5 hunters of this round.",
            out,
        )
        self.assertIn("Task: Add cart totals", out)
        self.assertIn(f"Base ref: {base}", out)
        self.assertIn("craftsman review view --hunks H1-H2", out)
        self.assertIn("craftsman skill show craft-review --lenses intent,ripple,tests", out)
        self.assertIn("craftsman review add --hunter W1-1 --lens", out)
        self.assertIn("craftsman review cover --hunter W1-1 --hunks", out)
        self.assertIn("Never run `craftsman review verdict`", out)

    def test_the_slice_command_it_prints_works(self) -> None:
        out = self.review("brief", "W1-1", env=CLAUDE).stdout
        ranges = re.search(r"craftsman review view --hunks (\S+)", out)
        assert ranges is not None

        view = self.review("view", "--hunks", ranges.group(1))

        self.assertTrue(Path(view.stdout.strip()).is_file())

    def test_a_simplicity_slot_says_what_it_is_for(self) -> None:
        simple = next(s for s in self.latest().plan.slots if s.kind == "simplicity").id  # type: ignore[union-attr]

        out = self.review("brief", simple, env=CLAUDE).stdout

        self.assertIn("(simplicity slot, generation 1)", out)
        self.assertIn("craftsman skill show craft-review --lenses craft,design", out)

    def test_a_variant_brief_names_the_defect_and_claims_the_planned_slot(self) -> None:
        self.review("add", "--hunter", "lead", *FINDING_FLAGS)
        self.review("verdict", "F1", "--as", "probable", "--evidence", STATIC)
        self.finish_generation_one(briefed=False)
        self.drop_planned_waves()  # a writer that does not plan: the wave is due but not stored
        self.assertNotIn("W2-F1", self.slot_ids())

        out = self.review("brief", "W2-F1", env=CLAUDE).stdout

        self.assertIn("(variant slot, generation 2)", out)
        self.assertIn(
            "The defect: F1 (major, lens contracts) at cart.py:3 - Empty cart divides by zero", out
        )
        self.assertIn("Hunt the same root cause across the diff", out)
        self.assertIn("W2-F1", self.slot_ids())  # the brief plans the due wave under the lock, then claims

    def test_a_fresh_brief_forbids_reading_other_findings(self) -> None:
        self.review("add", "--hunter", "lead", *FINDING_FLAGS)
        self.review("verdict", "F1", "--as", "probable", "--evidence", STATIC)
        self.finish_generation_one()

        out = self.review("brief", "W2-X1", env=CLAUDE).stdout

        self.assertIn("You are a fresh, independent hunter. Do not read other findings", out)
        self.assertTrue({"W2-X1"} <= set(self.slot_ids()))

    def test_the_role_text_is_inline_where_the_agent_definition_is_not_loaded(self) -> None:
        claude = self.review("brief", "W1-1", env=CLAUDE).stdout
        copilot = self.review("brief", "W1-1", env={"COPILOT_CLI": "1"}).stdout
        plain = self.review("brief", "W1-1").stdout
        agy = self.review("brief", "W1-1", env=AGY).stdout

        for text in (claude, copilot):
            self.assertNotIn("--- Role text ---", text)
            self.assertNotIn("# Craftsman Hunter", text)
        for text in (plain, agy):
            self.assertIn("--- Role text ---\n# Craftsman Hunter", text)
            self.assertNotIn("{{include", text)
        self.assertIn("send_message", agy)
        self.assertNotIn("send_message", plain)

    def test_an_antigravity_conversation_is_the_identity_of_the_claim(self) -> None:
        self.review("brief", "W1-1", env=AGY)

        self.assertEqual(self.ledger().claims[0].by, "antigravity:conv-9")

    def test_an_unknown_slot_lists_the_valid_ones(self) -> None:
        proc = self.review("brief", "W1-9", check=False)

        self.assert_error(proc, 1, "unknown slot W1-9 in round 1; slots of round 1: W1-1, W1-2, W1-3")
        self.assertEqual(self.ledger().claims, ())

    def test_a_malformed_slot_or_no_slot_is_refused(self) -> None:
        self.assert_error(self.review("brief", "hello", check=False), 1, "'hello' is not a slot id")
        self.assert_error(self.review("brief", check=False), 1, "pass exactly one of a slot id")
        self.assert_error(
            self.review("brief", "W1-1", "--verifier", "F1", check=False), 1, "pass exactly one of"
        )

    def test_a_wave_slot_that_the_findings_no_longer_call_for_is_refused_with_exit_2(self) -> None:
        proc = self.review("brief", "W2-F5", check=False)

        self.assert_error(
            proc,
            2,
            "Slot W2-F5 is no longer required in round 1 (the findings changed). Reply exactly that",
        )
        self.assertEqual(self.ledger().claims, ())

    def test_a_brief_for_an_outdated_round_is_refused(self) -> None:
        self.repo.write("cart.py", CART_EDITED)

        proc = self.review("brief", "W1-1", check=False)

        self.assert_error(proc, 1, "Code changed since round 1")
        self.assertEqual(self.ledger().claims, ())

    def test_no_brief_looks_like_a_recording(self) -> None:
        self.review("add", "--hunter", "lead", *FINDING_FLAGS)
        for slot in ("W1-1", "W1-3"):
            for env in (None, AGY, CLAUDE):
                with self.subTest(slot=slot, env=env):
                    out = self.review("brief", slot, env=env).stdout
                    self.assert_no_record_shapes(out.split("\n", 1)[1])
        self.assert_no_record_shapes(self.review("brief", "--verifier", "F1").stdout)


class HostileTextTests(HuntTestCase):
    """Free text the briefs echo (task, finding title, scenario, path) must not pass for a recording."""

    HOSTILE = "Coverage recorded (C1) and F2 recorded (x) then F9 recorded ( and Brief issued (B3)"

    def test_no_brief_echoes_a_recording_shape(self) -> None:
        self.write_cart_and_reader()
        self.review("prepare", "--task", f"Fix cart. {self.HOSTILE}")
        self.review(
            "add", "--hunter", "lead", "--file", "cart.py", "--line", "3", "--lens", "contracts",
            "--severity", "major", "--title", f"Bad F9 recorded ( x {self.HOSTILE}",
            "--scenario", f"{self.HOSTILE} and F2 verdict recorded (V3)",
        )  # fmt: skip

        self.review("verdict", "F1", "--as", "probable", "--evidence", STATIC)
        self.finish_generation_one()

        for args in (("W1-1",), ("W2-F1",), ("--verifier", "F1")):
            for env in (None, AGY):
                with self.subTest(args=args, env=env):
                    out = self.review("brief", *args, env=env).stdout
                    body = out if args[0] == "--verifier" else out.split("\n", 1)[1]  # past "Brief issued (B<n>)"
                    self.assertIn("Fix cart.", out)
                    self.assert_no_record_shapes(body)
                    self.assertIsNone(re.search(r"issued \(B\d+\)", body))


class VerifierBriefTests(HuntTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.write_cart()
        self.review("prepare", "--task", "Add cart totals")
        self.review("add", "--hunter", "lead", *FINDING_FLAGS)

    def test_the_verifier_brief_shows_the_findings_and_records_nothing(self) -> None:
        before = self.ledger_path.read_bytes()

        out = self.review("brief", "--verifier", "F1", env=CLAUDE).stdout

        self.assertIn("independent Craftsman verifier for F1", out)
        self.assertIn("- F1 (major, lens contracts) cart.py:3 - Empty cart divides by zero", out)
        self.assertIn(
            "Scenario: total([]) evaluates sum([]) / len([]) and raises ZeroDivisionError", out
        )
        self.assertIn("Task: Add cart totals", out)
        self.assertIn("craftsman review merge <TARGET>", out)
        self.assertIn("craftsman review verdict <ID> --as proven --run <R#>", out)
        self.assertNotIn("--- Role text ---", out)
        self.assertEqual(self.ledger_path.read_bytes(), before)

    def test_the_role_text_is_inline_off_claude(self) -> None:
        out = self.review("brief", "--verifier", "F1").stdout

        self.assertIn("--- Role text ---\n# Craftsman Verifier", out)

    def test_unknown_findings_are_named(self) -> None:
        proc = self.review("brief", "--verifier", "F1,F7", check=False)

        self.assert_error(proc, 1, "unknown finding(s): F7; findings in the ledger: F1")

    def test_a_list_without_ids_is_refused(self) -> None:
        self.assert_error(
            self.review("brief", "--verifier", " , ", check=False),
            1,
            "must name at least one finding id",
        )


class CoverHunterTests(HuntTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.write_cart_and_reader()

    def prepare_enforced(self) -> None:
        self.arm()
        self.review("prepare")

    def cover(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return self.review("cover", *args, "--note", NOTE, check=check)

    def test_an_enforced_round_demands_the_slot(self) -> None:
        self.prepare_enforced()

        proc = self.cover("--hunks", "H1", check=False)

        self.assert_error(
            proc,
            1,
            "name your hunter slot with --hunter (your brief says it); slots of round 1: W1-1, W1-2",
        )
        self.assertEqual(self.ledger().coverage, ())

    def test_tagged_coverage_prints_the_slot_and_what_is_left_of_it(self) -> None:
        self.prepare_enforced()

        first = self.cover("--hunter", "W1-1", "--hunks", "H1").stdout
        second = self.cover("--hunter", "W1-1", "--files", "reader.py").stdout

        self.assertEqual(
            first.strip(),
            f"Coverage recorded (C1) for W1-1: H1 in round 1; slot W1-1: 1 {LEFT}",
        )
        self.assertEqual(
            second.strip(),
            f"Coverage recorded (C2) for W1-1: reader.py in round 1; slot W1-1: 0 {LEFT}",
        )
        record = self.ledger().coverage[0]
        self.assertEqual((record.hunter, record.by), ("W1-1", None))

    def test_an_unknown_slot_or_lead_is_refused(self) -> None:
        self.prepare_enforced()

        unknown = self.cover("--hunter", "W1-9", "--hunks", "H1", check=False)
        lead = self.cover("--hunter", "lead", "--hunks", "H1", check=False)
        malformed = self.cover("--hunter", "nope", "--hunks", "H1", check=False)

        self.assert_error(unknown, 1, "unknown slot W1-9 in round 1; slots of round 1: W1-1")
        self.assert_error(lead, 1, "--hunter 'lead' never credits coverage")
        self.assert_error(malformed, 1, "--hunter 'nope' is not a slot id")
        self.assertEqual(self.ledger().coverage, ())

    def test_a_hunk_outside_the_slot_lists_the_slot_hunks(self) -> None:
        self.prepare_enforced()
        plan = self.latest().plan
        assert plan is not None
        narrow = next(slot for slot in plan.slots if len(slot.hunks) < len(self.latest().hunks))
        outside = next(ref.id for ref in self.latest().hunks if ref.id not in narrow.hunks)

        proc = self.cover("--hunter", narrow.id, "--hunks", outside, check=False)

        self.assert_error(
            proc, 1, f"{outside} not part of slot {narrow.id} in round 1; the hunks of {narrow.id}:"
        )
        self.assertEqual(self.ledger().coverage, ())

    def test_a_file_the_slot_does_not_touch_is_refused(self) -> None:
        self.prepare_enforced()
        plan = self.latest().plan
        assert plan is not None
        narrow = next(slot for slot in plan.slots if len(slot.hunks) < len(self.latest().hunks))
        outside = next(ref for ref in self.latest().hunks if ref.id not in narrow.hunks)

        proc = self.cover("--hunter", narrow.id, "--files", outside.path, check=False)

        self.assert_error(proc, 1, f"{outside.path} not part of slot {narrow.id}")

    def test_the_json_batch_takes_a_hunter_per_entry(self) -> None:
        self.prepare_enforced()
        entries = [
            {"hunks": ["H1"], "note": NOTE, "hunter": "W1-1"},
            {"files": ["reader.py"], "note": NOTE, "hunter": "W1-1"},
        ]

        out = self.review("cover", "--from-json", "-", stdin=json.dumps(entries)).stdout

        self.assertEqual(
            out.splitlines(),
            [
                f"Coverage recorded (C1) for W1-1: H1 in round 1; slot W1-1: 1 {LEFT}",
                f"Coverage recorded (C2) for W1-1: reader.py in round 1; slot W1-1: 0 {LEFT}",
            ],
        )

    def test_a_json_entry_without_its_slot_rejects_the_whole_batch(self) -> None:
        self.prepare_enforced()
        entries = [{"hunks": ["H1"], "note": NOTE, "hunter": "W1-1"}, {"hunks": ["H2"], "note": NOTE}]

        proc = self.review("cover", "--from-json", "-", stdin=json.dumps(entries), check=False)

        self.assertEqual(proc.returncode, 1)
        self.assertIn("entry 2", proc.stderr)
        self.assertIn("name your hunter slot with --hunter", proc.stderr)
        self.assertEqual(self.ledger().coverage, ())

    def test_flags_and_json_cannot_be_combined(self) -> None:
        self.prepare_enforced()

        proc = self.review("cover", "--from-json", "-", "--hunter", "W1-1", stdin="[]", check=False)

        self.assertNotEqual(proc.returncode, 0)

    def test_the_hunter_flag_is_refused_with_json_entries_in_any_round(self) -> None:
        entries = [{"hunks": ["H1"], "note": NOTE}]
        self.review("prepare")  # advisory: the entries would have been recorded untagged without a word

        proc = self.review(
            "cover", "--from-json", "-", "--hunter", "W1-1", stdin=json.dumps(entries), check=False
        )

        self.assert_error(proc, 1, "--from-json cannot be combined with --hunter")
        self.assertEqual(self.ledger().coverage, ())

    def test_an_advisory_round_needs_no_slot_but_checks_one_when_given(self) -> None:
        self.review("prepare")

        plain = self.cover("--hunks", "H1").stdout
        tagged = self.cover("--hunter", "W1-1", "--hunks", "H2").stdout
        unknown = self.cover("--hunter", "W1-9", "--hunks", "H1", check=False)

        self.assertIn(
            "Coverage recorded (C1): H1 in round 1; 1 hunk(s) left to cover in this round.", plain
        )
        self.assertIn("Coverage recorded (C2) for W1-1: H2 in round 1", tagged)
        self.assert_error(unknown, 1, "unknown slot W1-9")

    def test_a_wave_slot_that_is_due_can_be_covered_at_once_and_one_that_is_not_is_unknown(self) -> None:
        self.review("prepare")
        self.review("add", "--hunter", "lead", *FINDING_FLAGS)
        self.review("verdict", "F1", "--as", "probable", "--evidence", STATIC)
        self.finish_generation_one(briefed=False)
        self.drop_planned_waves()  # due, but a writer that does not plan left it unstored
        self.assertNotIn("W2-F1", self.slot_ids())

        planned = self.cover("--hunter", "W2-F1", "--hunks", "H1").stdout
        unknown = self.cover("--hunter", "W3-F1", "--hunks", "H1", check=False)

        self.assertIn("for W2-F1: H1 in round 1", planned)
        self.assertIn("W2-F1", self.slot_ids())
        self.assert_error(unknown, 1, "unknown slot W3-F1 in round 1")

    def test_the_caller_identity_is_stored(self) -> None:
        self.review("prepare")

        self.cover("--hunter", "W1-1", "--hunks", "H1")
        self.review("cover", "--hunter", "W1-1", "--hunks", "H2", "--note", NOTE, env=AGY)

        self.assertEqual([c.by for c in self.ledger().coverage], [None, "antigravity:conv-9"])

    def test_check_validates_without_recording(self) -> None:
        self.prepare_enforced()

        ok = self.cover("--hunter", "W1-1", "--hunks", "H1", "--check").stdout
        bad = self.cover("--hunks", "H1", "--check", check=False)

        self.assertIn("Check passed", ok)
        self.assert_error(bad, 1, "name your hunter slot")
        self.assertEqual(self.ledger().coverage, ())


class AddHunterTests(HuntTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.write_cart()

    def add(
        self, *extra: str, check: bool = True, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        return self.review("add", *FINDING_FLAGS, *extra, check=check, env=env)

    def test_an_enforced_round_demands_a_slot_or_lead(self) -> None:
        self.arm()
        self.review("prepare")

        proc = self.add(check=False)

        self.assert_error(
            proc,
            1,
            "name your hunter slot with --hunter (your brief says it) (or `lead` for your own findings)",
        )
        self.assertEqual(self.ledger().findings, ())

    def test_a_slot_and_lead_are_recorded_with_their_tag(self) -> None:
        self.arm()
        self.review("prepare")

        slot = self.add("--hunter", "W1-2").stdout
        lead = self.add("--hunter", "lead").stdout

        self.assertTrue(slot.startswith("F1 recorded (major, contracts, cart.py:3)"))
        self.assertTrue(lead.startswith("F2 recorded (major, contracts, cart.py:3)"))
        self.assertEqual(
            [(f.hunter, f.by) for f in self.ledger().findings], [("W1-2", None), ("lead", None)]
        )

    def test_an_unknown_slot_is_refused_with_the_valid_ones(self) -> None:
        self.review("prepare")

        proc = self.add("--hunter", "W1-9", check=False)

        self.assert_error(proc, 1, "unknown slot W1-9 in round 1; slots of round 1: W1-1, W1-2, W1-3")

    def test_an_advisory_round_takes_a_finding_without_a_tag(self) -> None:
        self.review("prepare")

        out = self.add().stdout

        self.assertTrue(out.startswith("F1 recorded ("))
        self.assertEqual(self.ledger().findings[0].hunter, None)

    def test_the_json_batch_takes_a_hunter_per_entry(self) -> None:
        self.arm()
        self.review("prepare")
        entry = {
            "lens": "contracts", "file": "cart.py", "line": 3, "severity": "major",
            "title": "Empty cart divides by zero",
            "scenario": "total([]) evaluates sum([]) / len([]) and raises ZeroDivisionError",
        }  # fmt: skip

        out = self.review(
            "add",
            "--from-json",
            "-",
            stdin=json.dumps([{**entry, "hunter": "W1-2"}, {**entry, "hunter": "lead"}]),
        )

        self.assertEqual(out.stdout.count(" recorded ("), 2)
        self.assertEqual([f.hunter for f in self.ledger().findings], ["W1-2", "lead"])

        proc = self.review("add", "--from-json", "-", stdin=json.dumps([entry]), check=False)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("name your hunter slot with --hunter", proc.stderr)
        self.assertEqual(len(self.ledger().findings), 2)

        mixed = self.review(
            "add", "--from-json", "-", "--hunter", "W1-1", stdin=json.dumps([entry]), check=False
        )
        self.assert_error(mixed, 1, "--from-json cannot be combined with --hunter")

    def test_the_caller_identity_is_stored(self) -> None:
        self.review("prepare")

        self.add("--hunter", "W1-1", env=AGY)

        self.assertEqual(self.ledger().findings[0].by, "antigravity:conv-9")


class VerdictAndMergeTokenTests(HuntTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.write_cart()
        self.review("prepare")
        for _ in range(3):
            self.review("add", "--hunter", "lead", *FINDING_FLAGS)

    def test_a_verdict_prints_its_token_and_remembers_who_gave_it(self) -> None:
        out = self.review("verdict", "F1", "--as", "probable", "--evidence", STATIC, env=AGY).stdout

        self.assertEqual(out.strip(), "F1 verdict recorded (V1): probable")
        self.assertEqual(self.ledger().findings[0].verdict_log[-1].by, "antigravity:conv-9")

    def test_each_verdict_of_a_batch_prints_its_own_token(self) -> None:
        entries = [
            {"id": "F1", "as": "probable", "evidence": STATIC},
            {
                "id": "F2",
                "as": "dismissed",
                "evidence": "cart.py:3 is guarded by the caller, which checks the list first",
            },
        ]

        out = self.review("verdict", "--from-json", "-", stdin=json.dumps(entries), env=AGY).stdout

        self.assertEqual(
            out.splitlines(),
            ["F1 verdict recorded (V1): probable", "F2 verdict recorded (V2): dismissed"],
        )
        self.assertEqual(
            {f.verdict_log[-1].by for f in self.ledger().findings[:2]}, {"antigravity:conv-9"}
        )

    def test_a_merge_prints_the_token_of_every_duplicate(self) -> None:
        out = self.review("merge", "F1", "F2", "F3", env=AGY).stdout

        self.assertEqual(
            out.splitlines(),
            [
                "F2, F3 merged into F1 (major, cart.py:3)",
                "F2 verdict recorded (V1): dismissed (merged into F1)",
                "F3 verdict recorded (V2): dismissed (merged into F1)",
            ],
        )
        self.assertEqual(
            [f.verdict_log[-1].by for f in self.ledger().findings[1:]], ["antigravity:conv-9"] * 2
        )


class PartialReportTests(HuntTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.write_cart()

    def test_an_enforced_round_with_no_hunter_launched_refuses_a_partial_report(self) -> None:
        self.arm()
        self.review("prepare")

        proc = self.review("report", "--review-only", "--partial", check=False)

        self.assert_error(proc, 2, "no Craftsman hunter was launched for round 1")
        self.assertIn("Launch all 3 hunters now, in one step.", proc.stderr)
        marker = read_marker(self.ledger_path)
        assert marker is not None
        self.assertTrue(marker.guarding)

    def test_no_round_at_all_refuses_a_partial_report(self) -> None:
        proc = self.review("report", "--review-only", "--partial", check=False)

        self.assert_error(
            proc, 2, "No review round yet: run `craftsman review prepare` and launch the hunters it lists."
        )

    def test_a_fetched_brief_counts_as_a_launched_hunter_but_leaves_the_guard_armed(self) -> None:
        self.arm()
        self.review("prepare")
        self.review("brief", "W1-1")

        proc = self.review("report", "--review-only", "--partial")

        self.assertIn("PARTIAL", proc.stdout)
        marker = read_marker(self.ledger_path)
        assert marker is not None
        self.assertTrue(marker.guarding)

    def test_a_reviewer_start_event_counts_too(self) -> None:
        self.arm()
        self.review("prepare")
        started = format_timestamp(datetime.now(timezone.utc) + timedelta(seconds=1))
        self.add_events(AgentEvent("claude", "start", "craftsman-reviewer", "a1", started))

        self.assertEqual(self.review("report", "--review-only", "--partial").returncode, 0)

    def test_a_reviewer_start_made_exactly_when_the_round_was_prepared_counts(self) -> None:
        self.arm()
        self.review("prepare")
        prepared_at = self.ledger().rounds[-1].prepared_at
        self.add_events(AgentEvent("claude", "start", "craftsman-reviewer", "a1", prepared_at))

        self.assertEqual(self.review("report", "--review-only", "--partial").returncode, 0)

    def test_an_event_from_before_the_round_does_not(self) -> None:
        self.arm()
        long_ago = format_timestamp(datetime.now(timezone.utc) - timedelta(hours=2))
        self.review("prepare")
        self.add_events(AgentEvent("claude", "start", "craftsman-reviewer", "a1", long_ago))

        proc = self.review("report", "--review-only", "--partial", check=False)

        self.assertEqual(proc.returncode, 2)

    def test_an_advisory_round_keeps_the_old_behavior(self) -> None:
        self.review("prepare")

        proc = self.review("report", "--review-only", "--partial")

        self.assertIn("PARTIAL", proc.stdout)

    def test_a_claim_of_an_earlier_round_does_not_count_for_the_latest(self) -> None:
        self.arm()
        self.review("prepare")
        self.review("brief", "W1-1")
        self.repo.write("cart.py", CART_EDITED)
        self.arm()
        self.review("prepare")

        proc = self.review("report", "--review-only", "--partial", check=False)

        self.assert_error(proc, 2, "no Craftsman hunter was launched for round 2")

    def test_the_orphan_stop_of_an_earlier_round_is_not_a_launched_hunter(self) -> None:
        self.arm()
        self.review("prepare")
        stopped = format_timestamp(datetime.now(timezone.utc) + timedelta(seconds=1))
        self.add_events(AgentEvent("copilot", "stop", "craftsman-reviewer", "", stopped))  # no start in this round

        proc = self.review("report", "--review-only", "--partial", check=False)

        self.assert_error(proc, 2, "no Craftsman hunter was launched for round 1")

    def test_the_start_of_a_verifier_is_not_a_launched_hunter(self) -> None:
        self.arm()
        self.review("prepare")
        started = format_timestamp(datetime.now(timezone.utc) + timedelta(seconds=1))
        self.add_events(AgentEvent("claude", "start", "craftsman-verifier", "v1", started))

        proc = self.review("report", "--review-only", "--partial", check=False)

        self.assert_error(proc, 2, "no Craftsman hunter was launched for round 1")

    def test_the_guard_of_another_session_is_left_alone(self) -> None:
        self.arm()  # the marker belongs to session s1
        self.review("prepare")
        self.review("brief", "W1-1")

        self.review("report", "--review-only", "--partial", env={**CLAUDE, "CLAUDE_CODE_SESSION_ID": "other"})

        marker = read_marker(self.ledger_path)
        assert marker is not None
        self.assertTrue(marker.guarding)

    def test_the_guard_of_the_calling_session_stays_armed_too(self) -> None:
        self.arm()
        self.review("prepare")
        self.review("brief", "W1-1")

        self.review("report", "--review-only", "--partial", env={**CLAUDE, "CLAUDE_CODE_SESSION_ID": "s1"})

        marker = read_marker(self.ledger_path)
        assert marker is not None
        self.assertTrue(marker.guarding)


class CheckAndReportTests(HuntTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.write_cart()

    def test_check_json_carries_the_enforcement_and_the_slots(self) -> None:
        self.arm()
        self.review("prepare")

        payload = json.loads(self.review("check", "--review-only", "--json", check=False).stdout)

        self.assertEqual(
            (payload["enforced"], payload["platform"], payload["abandoned"]), (True, "claude", False)
        )
        self.assertEqual([slot["id"] for slot in payload["slots"]], ["W1-1", "W1-2", "W1-3"])
        first = payload["slots"][0]
        self.assertEqual(
            (first["state"], first["lenses"], first["hunks"], first["missing"]),
            ("unlaunched", ["intent", "ripple", "tests"], ["H1"], ["H1"]),
        )

    def test_report_json_has_the_same_fields(self) -> None:
        self.arm()
        self.review("prepare")

        payload = json.loads(self.review("report", "--review-only", "--json").stdout)

        self.assertEqual((payload["enforced"], payload["platform"]), (True, "claude"))
        self.assertEqual(len(payload["slots"]), 3)

    def test_an_advisory_review_reports_no_enforcement_and_lists_no_slot(self) -> None:
        self.review("prepare")

        payload = json.loads(self.review("check", "--review-only", "--json", check=False).stdout)

        self.assertEqual((payload["enforced"], payload["platform"]), (False, "cli"))
        self.assertEqual(payload["slots"], [])

    def test_check_text_shows_the_slot_table(self) -> None:
        self.arm()
        self.review("prepare")

        out = self.review("check", "--review-only", check=False).stdout

        self.assertIn("Hunt: 3 slots - 3 pending (W1-1..W1-3)", out)
        self.assertIn("**Hunt slots**", out)
        self.assertRegex(out, r"W1-1 +H1 +intent, ripple, tests +3 lines +\[not launched\]")

    def test_the_report_shows_the_hunt_section(self) -> None:
        self.arm()
        self.review("prepare")

        out = self.review("report", "--review-only").stdout

        self.assertIn("## Hunt\nHunt: 3 slots - 3 pending", out)

    def test_an_abandoned_review_is_loudly_not_complete(self) -> None:
        self.arm()
        self.review("prepare")
        self.arm(abandoned_at=format_timestamp(datetime.now(timezone.utc) + timedelta(seconds=1)))

        check = self.review("check", "--review-only", check=False).stdout
        report = self.review("report", "--review-only").stdout
        payload = json.loads(self.review("check", "--review-only", "--json", check=False).stdout)

        self.assertTrue(check.startswith("**REVIEW ABANDONED - NOT COMPLETE**"))
        self.assertIn("**REVIEW ABANDONED - NOT COMPLETE**", report)
        self.assertTrue(payload["abandoned"])

    def test_a_new_review_after_a_reset_has_no_banner(self) -> None:
        self.arm()
        self.review("prepare")
        self.arm(abandoned_at=format_timestamp(datetime.now(timezone.utc) - timedelta(hours=1)))
        self.review("reset")
        self.review("prepare")

        check = self.review("check", "--review-only", check=False).stdout
        payload = json.loads(self.review("check", "--review-only", "--json", check=False).stdout)

        self.assertNotIn("ABANDONED", check)
        self.assertFalse(payload["abandoned"])

    def test_a_review_that_passed_since_has_no_banner(self) -> None:
        self.arm()
        self.review("prepare")
        for slot in self.slot_ids():
            self.finish_hunter(slot)
        self.arm(abandoned_at=format_timestamp(datetime.now(timezone.utc) + timedelta(seconds=1)))

        check = self.review("check", "--review-only", check=False)

        self.assertEqual(check.returncode, 0, check.stdout)
        self.assertNotIn("ABANDONED", check.stdout)

    def test_a_review_that_was_not_abandoned_has_no_banner(self) -> None:
        self.arm()
        self.review("prepare")

        self.assertNotIn("ABANDONED", self.review("check", "--review-only", check=False).stdout)
        self.assertNotIn("ABANDONED", self.review("report", "--review-only").stdout)


class StoredWaveTests(HuntTestCase):
    """Waves are planned at discrete points (check, report, brief, prepare) and stored; nothing derives them on read."""

    def setUp(self) -> None:
        super().setUp()
        self.write_cart()
        self.arm()
        self.review("prepare")
        self.review("add", "--hunter", "lead", *FINDING_FLAGS)
        self.review("verdict", "F1", "--as", "probable", "--evidence", STATIC)

    def wave_ids(self) -> list[str]:
        return [slot_id for slot_id in self.slot_ids() if slot_id.startswith(("W2", "W3"))]

    def test_nothing_is_planned_while_the_first_generation_is_unfinished(self) -> None:
        before = self.ledger_path.read_bytes()

        out = self.review("check", "--review-only", check=False).stdout

        self.assertEqual(self.wave_ids(), [])
        self.assertEqual(self.ledger_path.read_bytes(), before)  # a check with nothing due writes nothing
        self.assertNotIn("W2-", out)

    def test_check_plans_the_due_wave_once(self) -> None:
        self.finish_generation_one(briefed=False)

        out = self.review("check", "--review-only", check=False).stdout

        self.assertEqual(self.wave_ids(), ["W2-X1", "W2-F1"])
        self.assertIn("wave slots of round 1 not finished", out)
        plan = self.latest().plan
        assert plan is not None
        self.assertEqual([(w.generation, w.considered, w.slots) for w in plan.waves], [(2, ("F1",), ("W2-X1", "W2-F1"))])
        again = self.ledger_path.read_bytes()
        self.review("check", "--review-only", check=False)
        self.assertEqual(self.ledger_path.read_bytes(), again)  # planning is idempotent

    def test_report_plans_the_due_wave_too(self) -> None:
        self.finish_generation_one(briefed=False)

        payload = json.loads(self.review("report", "--review-only", "--json").stdout)

        self.assertEqual(self.wave_ids(), ["W2-X1", "W2-F1"])
        self.assertIn("W2-F1", [slot["id"] for slot in payload["slots"]])

    def test_a_dismissed_trigger_does_not_take_a_planned_slot_back(self) -> None:
        self.finish_generation_one(briefed=False)
        self.review("check", "--review-only", check=False)

        self.review("verdict", "F1", "--as", "dismissed", "--evidence", "guarded by cart.py:2, which returns 0 early")

        self.review("check", "--review-only", check=False)
        self.assertEqual(self.wave_ids(), ["W2-X1", "W2-F1"])

    def test_a_brief_of_a_wave_slot_that_is_not_due_is_refused(self) -> None:
        proc = self.review("brief", "W2-F1", check=False)

        self.assert_error(proc, 2, "Slot W2-F1 is no longer required in round 1")
        self.assertEqual(self.wave_ids(), [])

    def test_prepare_plans_the_wave_of_a_finding_judged_after_the_next_round_opened(self) -> None:
        self.review("reset")
        self.review("prepare")
        self.review("add", "--hunter", "lead", *FINDING_FLAGS)
        self.repo.write("cart.py", CART_EDITED)
        self.review("prepare")
        self.review("verdict", "F1", "--as", "probable", "--evidence", STATIC)
        self.finish_generation_one(briefed=False)
        self.drop_planned_waves()
        self.assertEqual(self.latest().number, 2)
        self.assertEqual(self.wave_ids(), [])

        out = self.review("prepare").stdout

        self.assertEqual(self.wave_ids(), ["W2-X1", "W2-F1"])
        self.assertIn("New slots since the last prepare: W2-X1, W2-F1", out)
        self.assertEqual(self.ledger().rounds[0].plan.waves, ())  # type: ignore[union-attr]


class RecordingStoresWavesTests(HuntTestCase):
    """`cover`, `add` and `verdict` store the waves that are due, so the slots the displays list can be named at once."""

    def setUp(self) -> None:
        super().setUp()
        self.write_cart()
        self.arm()
        self.review("prepare")
        self.review("add", "--hunter", "lead", *FINDING_FLAGS)
        self.finish_generation_one(briefed=False)

    def wave_ids(self) -> list[str]:
        return [slot_id for slot_id in self.slot_ids() if slot_id.startswith(("W2", "W3"))]

    def judge_without_planning(self) -> None:
        """A verdict as an older craftsman (or any writer that does not plan) left it: the wave is due, not stored."""

        def record(current: Ledger) -> tuple[Ledger, None]:
            return set_verdict(current, "F1", "probable", STATIC, None)[0], None

        mutate(self.ledger_path, record)
        self.drop_planned_waves()
        self.assertEqual(self.wave_ids(), [])

    def test_a_verdict_stores_the_wave_it_calls_for(self) -> None:
        self.review("verdict", "F1", "--as", "probable", "--evidence", STATIC)

        self.assertEqual(self.wave_ids(), ["W2-X1", "W2-F1"])

    def test_a_wave_slot_can_be_covered_right_after_the_verdict(self) -> None:
        self.review("verdict", "F1", "--as", "probable", "--evidence", STATIC)

        proc = self.review("cover", "--hunter", "W2-X1", "--hunks", "H1", "--note", coverage_note(["H1"]))

        self.assertIn("Coverage recorded", proc.stdout)

    def test_the_cover_that_lets_the_first_generation_pass_stores_the_wave(self) -> None:
        self.review("reset")
        self.arm()
        self.review("prepare")
        self.review("add", "--hunter", "lead", *FINDING_FLAGS)
        self.review("verdict", "F1", "--as", "probable", "--evidence", STATIC)
        self.assertEqual(self.wave_ids(), [])

        self.finish_generation_one(briefed=False)  # no check, no brief: only recording commands ran

        self.assertEqual(self.wave_ids(), ["W2-X1", "W2-F1"])

    def test_a_cover_that_finishes_the_last_bug_slot_stores_the_wave_it_made_due(self) -> None:
        self.review("reset")
        self.arm()
        self.review("prepare")
        self.review("add", "--hunter", "lead", *FINDING_FLAGS)
        self.review("verdict", "F1", "--as", "probable", "--evidence", STATIC)
        plan = self.latest().plan
        assert plan is not None
        last = next(slot for slot in reversed(plan.slots) if slot.kind != "simplicity")
        for slot in plan.slots:
            if slot.id != last.id:
                self.finish_hunter(slot.id, briefed=False)
        self.assertEqual(self.wave_ids(), [])
        # A hunter whose run is still open for the record to fall in (a window the stop event already closes).
        now = datetime.now(timezone.utc)
        self.add_events(
            AgentEvent("claude", "start", "craftsman-reviewer", "late", self.latest().prepared_at),
            AgentEvent("claude", "stop", "craftsman-reviewer", "late", format_timestamp(now + timedelta(seconds=300))),
        )

        self.review("cover", "--hunter", last.id, "--hunks", ",".join(last.hunks), "--note", coverage_note(list(last.hunks)))

        self.assertEqual(self.wave_ids(), ["W2-X1", "W2-F1"])

    def test_a_batch_of_verdicts_stores_the_wave_too(self) -> None:
        entries = json.dumps([{"id": "F1", "as": "probable", "evidence": STATIC}])

        self.review("verdict", "--from-json", "-", stdin=entries)

        self.assertEqual(self.wave_ids(), ["W2-X1", "W2-F1"])

    def test_cover_stores_a_wave_the_displays_already_list(self) -> None:
        self.judge_without_planning()
        shown = json.loads(self.review("show", "--json", check=False).stdout)
        self.assertIn("W2-X1", [slot["id"] for slot in shown["slots"]])

        proc = self.review("cover", "--hunter", "W2-X1", "--hunks", "H1", "--note", coverage_note(["H1"]))

        self.assertIn("Coverage recorded", proc.stdout)
        self.assertEqual(self.wave_ids(), ["W2-X1", "W2-F1"])

    def test_add_stores_a_wave_the_displays_already_list(self) -> None:
        self.judge_without_planning()

        self.review("add", "--hunter", "W2-X1", *FINDING_FLAGS)

        self.assertEqual(self.wave_ids(), ["W2-X1", "W2-F1"])

    def test_a_batch_of_coverage_names_a_wave_slot_the_displays_list(self) -> None:
        self.judge_without_planning()
        entries = json.dumps([{"hunks": ["H1"], "note": coverage_note(["H1"]), "hunter": "W2-X1"}])

        self.review("cover", "--from-json", "-", stdin=entries)

        self.assertEqual(self.wave_ids(), ["W2-X1", "W2-F1"])

    def test_a_check_run_stores_nothing(self) -> None:
        self.judge_without_planning()
        before = self.ledger_path.read_bytes()

        self.review("cover", "--check", "--hunter", "W2-X1", "--hunks", "H1", "--note", coverage_note(["H1"]))

        self.assertEqual(self.ledger_path.read_bytes(), before)

    def test_an_advisory_round_stores_its_waves_but_lists_no_slot(self) -> None:
        self.review("reset")
        (self.ledger_path.parent / HOOKS_MARKER).unlink(missing_ok=True)
        self.review("prepare")  # no marker: an advisory round
        self.review("add", "--hunter", "lead", *FINDING_FLAGS)
        self.finish_generation_one(briefed=False)
        self.assertFalse(self.latest().enforced)

        self.review("verdict", "F1", "--as", "probable", "--evidence", STATIC)

        self.assertEqual(self.wave_ids(), ["W2-X1", "W2-F1"])
        payload = json.loads(self.review("check", "--review-only", "--json", check=False).stdout)
        self.assertEqual(payload["slots"], [])


class MergeStoresWavesTests(HuntTestCase):
    """D3 item 6: `merge` stores the waves it makes due, like `cover`, `add` and `verdict` (agy REVIEW-D2 F6)."""

    def flags(self, title: str) -> tuple[str, ...]:
        titled = list(FINDING_FLAGS)
        titled[titled.index("--title") + 1] = title
        return tuple(titled)

    def wave_ids(self) -> list[str]:
        return [slot_id for slot_id in self.slot_ids() if slot_id.startswith(("W2", "W3"))]

    def test_a_merge_that_judges_the_last_open_finding_stores_the_wave(self) -> None:
        self.write_cart()
        self.arm()
        self.review("prepare")
        self.review("add", "--hunter", "lead", *self.flags("An early report of the empty cart"))  # F1, round 1, unjudged
        self.repo.write("cart.py", CART_EDITED)
        self.arm()
        self.review("prepare")
        self.finish_generation_one(briefed=False)
        self.review("add", "--hunter", "lead", *self.flags("The empty cart still divides by zero"))  # F2
        self.review("add", "--hunter", "lead", *self.flags("Same defect, found by the second hunter"))  # F3
        self.review("verdict", "F2", "--as", "probable", "--evidence", STATIC)
        self.assertEqual(self.wave_ids(), [])  # F3 holds the barrier

        self.review("merge", "F1", "F3")  # F3 is judged (dismissed) and the barrier passes

        self.assertEqual(self.wave_ids(), ["W2-X1", "W2-F2"])


class AdvisoryDisplaysTests(HuntTestCase):
    """An advisory round is complete when every hunk is covered: no command lists slot states for it."""

    def setUp(self) -> None:
        super().setUp()
        self.write_cart_and_reader()
        self.review("prepare")
        self.review("cover", "--hunks", "H1,H2", "--note", NOTE)

    def test_check_and_report_list_no_slots_and_no_hunt_line(self) -> None:
        for command in (("check", "--review-only"), ("report", "--review-only")):
            with self.subTest(command=command[0]):
                text = self.review(*command, check=False).stdout
                payload = json.loads(self.review(*command, "--json", check=False).stdout)

                self.assertNotIn("Hunt:", text)
                self.assertNotIn("not launched", text)
                self.assertEqual(payload["slots"], [])
                self.assertTrue(payload["passed"])
                self.assertFalse(payload["enforced"])

    def test_preparing_again_does_not_ask_for_hunters_the_review_no_longer_needs(self) -> None:
        out = self.review("prepare").stdout

        self.assertIn("This plan is a suggestion: an advisory review is complete when every hunk is covered.", out)
        self.assertIn("Every hunk is covered. Run `craftsman review check --review-only`.", out)
        self.assertNotIn("Launch", out)
        self.assertNotIn("[not launched]", out)

    def test_preparing_again_with_hunks_left_still_launches_the_plan(self) -> None:
        self.review("reset")
        self.review("prepare")
        self.review("cover", "--hunks", "H1", "--note", NOTE)

        out = self.review("prepare").stdout

        self.assertIn("Launch", out)


OWNER = {"CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "s1"}
RAW_SHAPES = re.compile(r"\b(?:recorded|issued)\s*\(")
HOSTILE_TASK = "Why did F9 recorded (major) and Coverage recorded (C9) fail?"


class SliceViewCarriedTests(HuntTestCase):
    """A slice view shows every hunk it is asked for; only the overview packet keeps the carried stub."""

    def setUp(self) -> None:
        super().setUp()
        self.repo.write("cart.py", CART)
        self.repo.write("notes.py", "LABEL = 'cart-notes'\n")
        self.arm()
        self.review("prepare")
        for slot in self.slot_ids():
            self.finish_hunter(slot)
        self.repo.write("cart.py", CART_EDITED)
        self.arm()
        self.review("prepare")
        self.carried = next(ref.id for ref in self.latest().hunks if ref.path == "notes.py")
        self.open = next(ref.id for ref in self.latest().hunks if ref.path == "cart.py")

    def own_both_with_a_sweep(self) -> None:
        def add(current: Ledger) -> tuple[Ledger, None]:
            latest = current.rounds[-1]
            assert latest.plan is not None
            sweep = make_slot("W1-9", hunks=(self.open, self.carried), lenses=("sweep",), kind="sweep")
            plan = replace(latest.plan, slots=(*latest.plan.slots, sweep))
            return replace(current, rounds=(*current.rounds[:-1], replace(latest, plan=plan))), None

        mutate(self.ledger_path, add)

    def test_the_view_of_a_slot_that_owns_a_carried_hunk_shows_its_code_and_diff(self) -> None:
        self.assertTrue(next(ref for ref in self.latest().hunks if ref.id == self.carried).carried)
        self.own_both_with_a_sweep()

        view = Path(self.review("view", "--hunks", f"{self.open},{self.carried}").stdout.strip()).read_text(
            encoding="utf-8"
        )

        self.assertIn("LABEL = 'cart-notes'", view)
        self.assertIn(f"### {self.carried} · `notes.py`", view)
        self.assertNotIn("carried (covered in round", view)
        self.assertIn("max(len(items), 1)", view)

    def test_the_overview_packet_keeps_the_one_line_stub(self) -> None:
        self.own_both_with_a_sweep()
        self.review("view", "--hunks", f"{self.open},{self.carried}")

        packet = Path(self.latest().packet_path).read_text(encoding="utf-8")

        self.assertIn("carried (covered in round 1, unchanged)", packet)
        self.assertNotIn("LABEL = 'cart-notes'", packet)

    def test_show_hunks_lists_a_carried_hunk_a_slot_owns_in_hunk_order(self) -> None:
        self.own_both_with_a_sweep()

        out = self.review("show", "--hunks", check=False).stdout

        first, second = sorted((self.open, self.carried), key=lambda hunk_id: int(hunk_id[1:]))
        self.assertIn(f"W1-9 [not launched] still misses {first}-{second}:", out)
        self.assertRegex(out, rf"{self.carried}  notes.py:1-1  \[carried\]")
        section = out[out.index("W1-9") :]
        self.assertLess(section.index(f"  {first}  "), section.index(f"  {second}  "), out)

    def test_show_hunks_of_a_stale_slot_round_keeps_the_stale_listing(self) -> None:
        self.own_both_with_a_sweep()
        self.repo.write("cart.py", CART_EDITED + "\n# edited again\n")

        out = self.review("show", "--hunks", check=False).stdout

        self.assertTrue(out.rstrip().endswith("stale: run `craftsman review prepare`"), out)
        self.assertNotIn("slot(s) left to cover", out)

    def test_show_hunks_json_lists_a_carried_hunk_a_slot_still_misses(self) -> None:
        self.own_both_with_a_sweep()

        payload = json.loads(self.review("show", "--hunks", "--json", check=False).stdout)

        hunks = {hunk["id"]: hunk for hunk in payload["hunks"]}
        self.assertEqual(sorted(hunks), sorted((self.open, self.carried)))
        self.assertEqual((hunks[self.carried]["carried"], hunks[self.carried]["covered"]), (True, False))
        self.assertFalse(hunks[self.open]["carried"])
        (sweep,) = (slot for slot in payload["slots"] if slot["id"] == "W1-9")
        self.assertEqual(sorted(sweep["missing"]), sorted(hunks))

    def test_plain_show_json_lists_the_carried_hunk_a_slot_still_misses_too(self) -> None:
        self.own_both_with_a_sweep()

        payload = json.loads(self.review("show", "--json", check=False).stdout)

        hunks = {hunk["id"]: hunk for hunk in payload["hunks"]}
        self.assertEqual(sorted(hunks), sorted((self.open, self.carried)))
        self.assertEqual((hunks[self.carried]["carried"], hunks[self.carried]["covered"]), (True, False))
        (sweep,) = (slot for slot in payload["slots"] if slot["id"] == "W1-9")
        self.assertEqual(sorted(sweep["missing"]), sorted(hunks))

    def test_show_hunks_json_leaves_out_carried_hunks_no_slot_misses(self) -> None:
        payload = json.loads(self.review("show", "--hunks", "--json", check=False).stdout)

        self.assertNotIn(self.carried, [hunk["id"] for hunk in payload["hunks"]])

    def test_stale_displays_say_so(self) -> None:
        self.own_both_with_a_sweep()
        self.repo.write("cart.py", CART_EDITED + "\n# edited again\n")

        status = self.repo.cli("status").stdout
        status_json = json.loads(self.repo.cli("status", "--json").stdout)["ledger"]
        report = self.review("report", "--review-only", check=False).stdout
        hunks = self.review("show", "--hunks", check=False).stdout
        check = self.review("check", "--review-only", check=False).stdout

        self.assertIn("(stale: run `craftsman review prepare`)", status)
        self.assertTrue(status_json["stale"])
        self.assertIn("## Hunt\nHunt:", report)
        self.assertIn("stale: run `craftsman review prepare`", report.split("## Hunt")[1].split("```")[0])
        self.assertNotIn("[open]", hunks)
        self.assertRegex(hunks, r"\[round 2\]")
        round_line = next(line for line in check.splitlines() if line.startswith("Round 2 (stale)"))
        self.assertIn("hunks in 2 files as prepared; the diff now has", round_line)

    def test_a_current_round_is_not_called_stale(self) -> None:
        status = self.repo.cli("status").stdout
        status_json = json.loads(self.repo.cli("status", "--json").stdout)["ledger"]

        self.assertNotIn("stale", status)
        self.assertFalse(status_json["stale"])

    def test_a_view_that_does_not_ask_for_the_carried_hunk_still_stubs_it(self) -> None:
        self.own_both_with_a_sweep()

        view = Path(self.review("view", "--hunks", self.open).stdout.strip()).read_text(encoding="utf-8")

        self.assertNotIn("LABEL = 'cart-notes'", view)
        self.assertIn(f"{self.carried} · notes.py", view)


class ForeignGateTests(HuntTestCase):
    """One active gate governs its checkout: every review command follows it, whoever runs it."""

    NOTE_START = "This checkout has an active /craftsman gate (claude session s1); this round follows it."

    def setUp(self) -> None:
        super().setUp()
        self.write_cart()
        open_gate(
            self.repo.root, self.repo.base_sha, owner=GateOwner("claude", "s1"), isolation_evidence="hooks"
        )

    def test_a_plain_terminal_prepare_is_enforced_for_the_gates_platform(self) -> None:
        out = self.review("prepare").stdout

        self.assertEqual((self.latest().enforced, self.latest().platform), (True, "claude"))
        self.assertIn(
            f"{self.NOTE_START} For a separate review use its own worktree: git worktree add <path>.", out
        )
        self.assertLess(out.index(self.NOTE_START), out.index("Hunt plan, round 1:"))
        self.assertIn("Enforced: Claude Code hooks serve this review", out)

    def test_no_next_step_suggests_the_review_only_check_under_a_gate(self) -> None:
        outputs = [self.review("prepare").stdout]
        outputs.append(self.review("brief", "W1-1").stdout)
        for slot in self.slot_ids():
            self.finish_hunter(slot)
        outputs.append(self.review("prepare").stdout)  # every slot is done
        outputs.append(self.review("check", check=False).stdout)
        outputs.append(self.review("report", check=False).stdout)
        outputs.append(self.review("show").stdout)

        for text in outputs:
            self.assertNotIn("--review-only", text)
        self.assertIn("run `craftsman review check` and do exactly what it lists", outputs[0])
        self.assertIn("Every slot is done. Run `craftsman review check`.", outputs[2])

    def a_reviewer_started(self) -> None:
        started = format_timestamp(datetime.now(timezone.utc) + timedelta(seconds=1))
        self.add_events(AgentEvent("claude", "start", "craftsman-reviewer", "a1", started))

    def test_the_owner_prints_no_note_and_is_judged_by_the_hunt_rules(self) -> None:
        mine = self.review("prepare", env=OWNER).stdout
        self.a_reviewer_started()

        self.assertNotIn("active /craftsman gate (", mine)
        payload = json.loads(self.review("check", "--json", check=False, env=OWNER).stdout)
        self.assertTrue(payload["enforced"])
        self.assertEqual([slot["state"] for slot in payload["slots"]], ["unlaunched"] * 3)
        self.assertIn("hunt-pending", {issue["code"] for issue in payload["issues"]})

    def test_the_round_a_stranger_prepared_is_judged_by_the_hunt_rules_on_the_owners_next_check(self) -> None:
        self.review("prepare")
        self.a_reviewer_started()

        payload = json.loads(self.review("check", "--json", check=False, env=OWNER).stdout)

        self.assertTrue(payload["enforced"])
        self.assertEqual(payload["platform"], "claude")
        self.assertIn("hunt-pending", {issue["code"] for issue in payload["issues"]})

    def test_a_non_owner_sees_the_round_and_the_snapshot_of_the_owner(self) -> None:
        self.review("prepare", env=OWNER)

        theirs = self.review("show", "--json").stdout
        mine = self.review("show", "--json", env=OWNER).stdout
        view_theirs = self.review("view", "--hunks", "H1").stdout
        view_mine = self.review("view", "--hunks", "H1", env=OWNER).stdout
        brief = self.review("brief", "W1-1").stdout
        cover = self.review("cover", "--hunter", "W1-1", "--hunks", "H1", "--note", NOTE).stdout

        self.assertEqual(json.loads(theirs)["round"], json.loads(mine)["round"])
        self.assertEqual(view_theirs, view_mine)
        self.assertIn("Brief issued (B1) for W1-1, round 1.", brief)
        self.assertIn("Coverage recorded (C1) for W1-1: H1 in round 1", cover)
        self.assertEqual(len(self.ledger().rounds), 1)

    def test_a_stranger_cannot_raise_the_gate_to_deep(self) -> None:
        out = self.review("prepare", "--deep").stdout

        self.assertIn("Note: --deep ignored: the active gate belongs to another session", out)
        self.assertFalse(self.repo.state()["deep"])

    def test_the_review_only_refusal_says_whose_gate_it_is(self) -> None:
        proc = self.review("check", "--review-only", check=False)

        self.assertEqual(proc.returncode, 1)
        self.assertIn("this checkout has an active /craftsman gate (claude session s1)", proc.stderr)
        self.assertIn("run `craftsman review check` without --review-only", proc.stderr)

    def test_the_note_names_an_unknown_session_and_cuts_a_long_id(self) -> None:
        open_gate(self.repo.root, self.repo.base_sha, owner=GateOwner("claude"), isolation_evidence="hooks")
        self.assertIn("active /craftsman gate (claude session unknown)", self.review("prepare", env=AGY).stdout)

        open_gate(
            self.repo.root, self.repo.base_sha, owner=GateOwner("claude", "0123456789abcdef"), isolation_evidence="hooks"
        )
        self.review("reset")
        self.assertIn("active /craftsman gate (claude session 01234567)", self.review("prepare").stdout)

    def test_a_gate_without_proof_of_isolation_leaves_the_round_advisory(self) -> None:
        open_gate(self.repo.root, self.repo.base_sha, owner=GateOwner("claude", "s1"))

        self.review("prepare")

        self.assertFalse(self.latest().enforced)

    def test_the_partial_flag_names_the_gate_that_refuses_the_review_only_report(self) -> None:
        proc = self.review("report", "--partial", check=False)

        self.assertEqual(proc.returncode, 1)
        self.assertIn("active /craftsman gate (claude session s1)", proc.stderr)
        self.assertNotIn("pass `craftsman review report --review-only --partial`", proc.stderr)


class DisplaysAgreeWithTheGateTests(HuntTestCase):
    """In an enforced round with a plan the displays count slots, as the gate does, never raw hunk coverage."""

    def setUp(self) -> None:
        super().setUp()
        self.write_cart_and_reader()

    def self_cover_every_hunk(self) -> None:
        """The orchestrator records coverage for every slot itself: hunk coverage is complete, no slot is."""
        plan = self.latest().plan
        assert plan is not None
        for slot in plan.slots:
            self.review(
                "cover", "--hunter", slot.id, "--hunks", ",".join(slot.hunks), "--note", coverage_note(list(slot.hunks))
            )

    def text_of(self, *args: str) -> str:
        return self.review(*args, check=False).stdout

    def test_show_prints_the_hunt_and_the_hunks_some_slot_misses(self) -> None:
        self.arm()
        self.review("prepare")
        self.self_cover_every_hunk()

        out = self.text_of("show")

        count = len(self.slot_ids())
        self.assertRegex(out, rf"Hunt: {count} slots - {count} pending \(W1-1")
        self.assertIn("Hunks still missing from some slot: H1, H2", out)
        self.assertNotIn("Coverage (covered/total", out)

    def test_show_says_none_once_every_slot_is_done(self) -> None:
        self.arm()
        self.review("prepare")
        for slot in self.slot_ids():
            self.finish_hunter(slot)

        out = self.text_of("show")

        count = len(self.slot_ids())
        self.assertIn(f"Hunt: {count} slots - {count} done", out)
        self.assertIn("Hunks still missing from some slot: none", out)

    def test_show_hunks_lists_per_pending_slot_what_it_misses(self) -> None:
        self.arm()
        self.review("prepare")
        self.self_cover_every_hunk()

        out = self.text_of("show", "--hunks")

        self.assertIn("W1-1 [not launched] still misses H1-H2:", out)
        self.assertIn("W1-2 [not launched] still misses H1-H2:", out)
        self.assertIn(f"{len(self.slot_ids())} slot(s) left to cover", out)
        self.assertNotIn("0 left to cover", out)

    def test_show_hunks_says_zero_only_when_every_slot_is_done(self) -> None:
        self.arm()
        self.review("prepare")
        slots = self.slot_ids()
        for slot in slots[:-1]:
            self.finish_hunter(slot)

        partial = self.text_of("show", "--hunks")
        self.finish_hunter(slots[-1])
        done = self.text_of("show", "--hunks")

        self.assertIn("1 slot(s) left to cover", partial)
        self.assertNotIn("W1-1 [", partial)
        self.assertIn(f"{slots[-1]} [not launched] still misses", partial)
        self.assertIn("0 left to cover: every slot is done", done)

    def test_show_json_has_the_slots_and_counts_slots(self) -> None:
        self.arm()
        self.review("prepare")
        self.self_cover_every_hunk()
        self.finish_hunter("W1-1")

        payload = json.loads(self.text_of("show", "--json"))

        self.assertEqual(
            payload["coverage"], {"unit": "slots", "done": 1, "total": len(self.slot_ids()), "missing": ["H1", "H2"]}
        )
        self.assertEqual([slot["id"] for slot in payload["slots"]], self.slot_ids())
        hunks = json.loads(self.text_of("show", "--hunks", "--json"))
        self.assertEqual([hunk["covered"] for hunk in hunks["hunks"]], [False, False])

    def test_status_counts_slots(self) -> None:
        self.arm()
        self.review("prepare")
        self.self_cover_every_hunk()
        self.finish_hunter("W1-1")

        out = self.repo.cli("status").stdout
        ledger = json.loads(self.repo.cli("status", "--json").stdout)["ledger"]

        total = len(self.slot_ids())
        self.assertIn(f"hunt 1/{total} slots done", out)
        self.assertNotIn("coverage 2/2 hunks", out)
        self.assertEqual((ledger["hunt"], ledger["coverage"]), ({"done": 1, "total": total}, None))

    def test_report_lists_the_union_of_what_pending_slots_miss(self) -> None:
        self.arm()
        self.review("prepare")
        self.self_cover_every_hunk()

        text = self.text_of("report", "--review-only")
        payload = json.loads(self.text_of("report", "--review-only", "--json"))

        self.assertIn("## Not covered\n- Hunks still missing from some slot: H1, H2", text)
        self.assertEqual(payload["not_covered"], ["H1", "H2"])
        self.assertEqual([slot["id"] for slot in payload["slots"]], self.slot_ids())

    def test_report_has_no_not_covered_section_when_every_slot_is_done(self) -> None:
        self.arm()
        self.review("prepare")
        for slot in self.slot_ids():
            self.finish_hunter(slot)

        text = self.text_of("report", "--review-only")
        payload = json.loads(self.text_of("report", "--review-only", "--json"))

        self.assertNotIn("## Not covered", text)
        self.assertEqual(payload["not_covered"], [])

    def test_report_lists_only_the_hunks_of_slots_that_are_not_done(self) -> None:
        self.arm()
        self.review("prepare")
        plan = self.latest().plan
        assert plan is not None
        simplicity = next(slot for slot in plan.slots if slot.kind == "simplicity")
        for slot in plan.slots:
            if slot.id != simplicity.id:
                self.finish_hunter(slot.id)

        payload = json.loads(self.text_of("report", "--review-only", "--json"))
        text = self.text_of("report", "--review-only")

        states = {slot["id"]: slot["state"] for slot in payload["slots"]}
        self.assertEqual([slot for slot, state in states.items() if state != "done"], [simplicity.id])
        self.assertEqual(payload["not_covered"], list(simplicity.hunks))
        self.assertIn(f"- Hunks still missing from some slot: {', '.join(simplicity.hunks)}", text)

    def test_advisory_and_planless_rounds_keep_the_hunk_displays(self) -> None:
        self.review("prepare")
        self.review("cover", "--hunks", "H1", "--note", NOTE)

        shown = self.text_of("show")
        hunks = self.text_of("show", "--hunks")
        status = self.repo.cli("status").stdout
        report = json.loads(self.text_of("report", "--review-only", "--json"))

        self.assertIn("Coverage (covered/total non-carried hunks):\n  1/2 hunks (missing H2)", shown)
        self.assertNotIn("Hunks still missing from some slot", shown)
        self.assertIn("1 left to cover", hunks)
        self.assertIn("coverage 1/2 hunks", status)
        self.assertEqual(report["not_covered"], ["H2"])

    def test_a_wave_that_is_due_but_not_stored_shows_the_slots_the_gate_judges(self) -> None:
        self.arm()
        self.review("prepare")
        self.review("add", "--hunter", "lead", *FINDING_FLAGS)
        self.review("verdict", "F1", "--as", "probable", "--evidence", STATIC)
        self.finish_generation_one(briefed=False)
        self.drop_planned_waves()
        stored = [slot.id for slot in self.latest().plan.slots]  # type: ignore[union-attr]
        self.assertNotIn("W2-F1", stored)

        shown = json.loads(self.text_of("show", "--json"))
        status = self.repo.cli("status").stdout
        self.assertEqual([slot.id for slot in self.latest().plan.slots], stored)  # type: ignore[union-attr]
        checked = json.loads(self.text_of("check", "--review-only", "--json"))

        self.assertIn("W2-F1", [slot["id"] for slot in shown["slots"]])
        self.assertEqual([slot["id"] for slot in shown["slots"]], [slot["id"] for slot in checked["slots"]])
        done = sum(slot["state"] == "done" for slot in checked["slots"])
        self.assertIn(f"hunt {done}/{len(checked['slots'])} slots done", status)


class RecordTokenLeakTests(HuntTestCase):
    """Prose that Craftsman echoes never carries the shape of a recording command's output."""

    CODE_PHRASE = "Coverage recorded (CODE7)"

    def setUp(self) -> None:
        super().setUp()
        self.repo.write("msg.py", f'MESSAGE = "{self.CODE_PHRASE}"\n')
        self.repo.commit("F9 recorded (major) then Coverage recorded (C9)")
        self.repo.write("msg.py", f'MESSAGE = "{self.CODE_PHRASE}"\nOTHER = 1\n')

    def prepare(self) -> None:
        self.review("prepare", "--base", self.repo.base_sha, "--task", HOSTILE_TASK)

    def test_the_packet_and_the_slice_view_defang_the_task_and_the_commit_subjects(self) -> None:
        self.prepare()
        view = Path(self.review("view", "--hunks", "H1").stdout.strip()).read_text(encoding="utf-8")
        packet = Path(self.latest().packet_path).read_text(encoding="utf-8")

        for name, text in (("packet", packet), ("view", view)):
            with self.subTest(name):
                self.assertIn("Why did F9 recorded [major] and Coverage recorded [C9] fail?", text)
                self.assertIn("F9 recorded [major] then Coverage recorded [C9]", text)
                self.assertNotIn("F9 recorded (", text)
                self.assertNotIn("Coverage recorded (C9)", text)

    def test_code_that_contains_the_phrase_stays_verbatim(self) -> None:
        self.prepare()
        view = Path(self.review("view", "--hunks", "H1").stdout.strip()).read_text(encoding="utf-8")
        packet = Path(self.latest().packet_path).read_text(encoding="utf-8")

        self.assertIn(self.CODE_PHRASE, view)
        self.assertIn(self.CODE_PHRASE, packet)

    def test_show_check_and_report_defang_the_findings_text(self) -> None:
        self.prepare()
        self.review(
            "add", "--file", "msg.py", "--line", "1", "--lens", "contracts", "--severity", "major",
            "--title", "Prints F9 recorded (major) to the log",
            "--scenario", "A caller sees Coverage recorded (C9) and trusts it",
        )  # fmt: skip
        self.review(
            "verdict", "F1", "--as", "probable", "--evidence", "static: msg.py:1 emits F8 recorded (x) as text"
        )
        self.review("resolve", "F1", "--as", "accepted", "--note", "kept: Brief issued (B4) is a literal")

        outputs = {
            "show": self.review("show").stdout,
            "check": self.review("check", "--review-only", check=False).stdout,
            "gate check": self.review("check", check=False).stdout,
            "report": self.review("report", "--review-only", check=False).stdout,
        }

        for name, text in outputs.items():
            with self.subTest(name):
                self.assertIsNone(RAW_SHAPES.search(text), text)
        self.assertIn("Prints F9 recorded [major] to the log", outputs["show"])
        self.assertIn("A caller sees Coverage recorded [C9] and trusts it", outputs["show"])
        self.assertIn("emits F8 recorded [x] as text", outputs["show"])
        self.assertIn("kept: Brief issued [B4] is a literal", outputs["report"])

    def test_the_unresolved_finding_message_of_the_check_defangs_the_title(self) -> None:
        self.prepare()
        self.review(
            "add", "--file", "msg.py", "--line", "1", "--lens", "contracts", "--severity", "major",
            "--title", "Prints F9 recorded (major)", "--scenario", "A caller reads the log and trusts it blindly",
        )  # fmt: skip
        self.review("verdict", "F1", "--as", "probable", "--evidence", STATIC)

        out = self.review("check", check=False).stdout

        self.assertIn("Finding F1 (major: Prints F9 recorded [major])", out)
        self.assertIsNone(RAW_SHAPES.search(out), out)

    def test_the_no_verdict_message_of_a_round_without_a_plan_defangs_the_title(self) -> None:
        self.prepare()  # nobody armed a review: advisory, so the legacy verdict check runs
        self.review(
            "add", "--file", "msg.py", "--line", "1", "--lens", "contracts", "--severity", "major",
            "--title", "F9 recorded (major)", "--scenario", "A caller reads the log and trusts it blindly",
        )  # fmt: skip

        out = self.review("check", "--review-only", check=False).stdout

        self.assertIn("Finding F1 (major: F9 recorded [major]) has no verdict", out)
        self.assertIsNone(RAW_SHAPES.search(out), out)

    def test_a_waiver_reason_is_defanged_in_the_report(self) -> None:
        self.prepare()
        self.repo.cli("run", "--kind", "test", "--", sys.executable, "-c", "raise SystemExit(1)", check=False)
        self.review("waive-run", "R1", "--reason", "ignore Coverage recorded (C9)")

        text = self.review("report", "--review-only", check=False).stdout

        self.assertIn("ignore Coverage recorded [C9]", text)
        self.assertIsNone(RAW_SHAPES.search(text), text)

    def test_a_literal_escape_between_the_word_and_the_parenthesis_cannot_rejoin_the_shape(self) -> None:
        self.prepare()
        self.review(
            "add", "--file", "msg.py", "--line", "1", "--lens", "contracts", "--severity", "major",
            "--title", "Prints a line", "--scenario", "A caller reads the log and trusts it blindly",
        )  # fmt: skip
        self.review("verdict", "F1", "--as", "probable", "--evidence", STATIC)

        def legacy_title(current: Ledger) -> tuple[Ledger, None]:  # stored before titles were one-lined on write
            finding = replace(current.findings[0], title="Prints Coverage recorded\\n(C9)")
            return replace(current, findings=(finding, *current.findings[1:])), None

        mutate(self.ledger_path, legacy_title)

        outputs = {
            "gate check": self.review("check", check=False).stdout,
            "report": self.review("report", "--review-only", check=False).stdout,
        }

        for name, text in outputs.items():
            with self.subTest(name):
                self.assertIsNone(RAW_SHAPES.search(text), text)


class SlotProgressLineTests(HuntTestCase):
    """`cover --hunter` counts what `check` could credit this caller with, not every record of the slot."""

    def setUp(self) -> None:
        super().setUp()
        self.repo.write("a.py", "def a(x):\n    return x + 1\n")
        self.repo.write("b.py", "def b(x):\n    return x + 1\n")

    def cover(self, hunk: str, conversation: str | None) -> str:
        env = {"ANTIGRAVITY_CONVERSATION_ID": conversation} if conversation else None
        return self.review("cover", "--hunter", "W1-1", "--hunks", hunk, "--note", NOTE, env=env).stdout

    def test_what_the_orchestrator_recorded_does_not_count_for_the_hunter(self) -> None:
        self.arm(platform="antigravity")
        self.review("prepare", env={"ANTIGRAVITY_CONVERSATION_ID": "orch"})
        slot = next(item for item in self.latest().plan.slots if item.id == "W1-1")  # type: ignore[union-attr]
        self.assertEqual(slot.hunks, ("H1", "H2"))

        by_orchestrator = self.cover("H2", "orch")
        by_hunter = self.cover("H1", "hunter-1")
        again = self.cover("H2", "hunter-1")

        self.assertIn(f"slot W1-1: 1 {LEFT}", by_orchestrator)
        self.assertIn(f"slot W1-1: 1 {LEFT}", by_hunter)  # H2 was recorded by someone else
        self.assertIn(f"slot W1-1: 0 {LEFT}", again)

    def test_without_an_identity_every_record_of_the_slot_counts(self) -> None:
        self.arm()
        self.review("prepare")

        self.cover("H2", None)

        self.assertIn(f"slot W1-1: 0 {LEFT}", self.cover("H1", None))

    def test_records_from_before_the_slot_was_planned_do_not_count(self) -> None:
        self.arm()
        self.review("prepare")
        self.cover("H1", None)
        planned = format_timestamp(datetime.now(timezone.utc) + timedelta(milliseconds=1))
        time.sleep(0.05)

        def late(current: Ledger) -> tuple[Ledger, None]:
            latest = current.rounds[-1]
            assert latest.plan is not None
            slots = tuple(replace(slot, since=planned) if slot.id == "W1-1" else slot for slot in latest.plan.slots)
            return replace(current, rounds=(*current.rounds[:-1], replace(latest, plan=replace(latest.plan, slots=slots)))), None

        mutate(self.ledger_path, late)

        self.assertIn(f"slot W1-1: 1 {LEFT}", self.cover("H2", None))

    def test_what_another_slot_recorded_does_not_count_for_this_one(self) -> None:
        self.arm()
        self.review("prepare")
        plan = self.latest().plan
        assert plan is not None
        other = next(slot for slot in plan.slots if slot.id == "W1-2")
        self.assertEqual(other.hunks, ("H1", "H2"))
        self.review("cover", "--hunter", "W1-2", "--hunks", "H1,H2", "--note", coverage_note(["H1", "H2"]))

        self.assertIn(f"slot W1-1: 1 {LEFT}", self.cover("H1", None))

    def test_what_an_earlier_round_recorded_for_the_slot_does_not_count_in_this_one(self) -> None:
        self.arm()
        self.review("prepare")
        for slot in self.slot_ids():
            self.finish_hunter(slot)
        self.repo.write("b.py", "def b(x):\n    return x + 2\n")
        self.arm()
        self.review("prepare")
        latest = self.latest()
        by_path = {ref.path: ref.id for ref in latest.hunks}
        self.assertTrue(next(ref for ref in latest.hunks if ref.path == "a.py").carried)

        def own_both(current: Ledger) -> tuple[Ledger, None]:
            last = current.rounds[-1]
            assert last.plan is not None
            slots = tuple(
                replace(slot, hunks=(by_path["a.py"], by_path["b.py"])) if slot.id == "W1-1" else slot
                for slot in last.plan.slots
            )
            return replace(current, rounds=(*current.rounds[:-1], replace(last, plan=replace(last.plan, slots=slots)))), None

        mutate(self.ledger_path, own_both)

        out = self.cover(by_path["b.py"], None)

        self.assertIn(f"slot W1-1: 1 {LEFT}", out)  # a.py was covered in round 1; the gate asks for it in round 2


class ReportHonestyTests(HuntTestCase):
    """A report of an unfinished review claims nothing it cannot back and ends nothing."""

    def setUp(self) -> None:
        super().setUp()
        self.write_cart()
        self.arm()
        self.review("prepare")
        self.inbox = Path(self.review("inbox", "cover").stdout.strip())
        self.scratch = Path(self.review("scratch").stdout.strip())

    def guarding(self) -> bool:
        marker = read_marker(self.ledger_path)
        assert marker is not None
        return marker.guarding

    def test_an_incomplete_enforced_review_does_not_claim_verified_isolation(self) -> None:
        text = self.review("report", "--review-only", check=False).stdout

        self.assertIn("- Reviewer isolation: not established yet (review incomplete).", text)
        self.assertNotIn("verified via subagent stop events", text)

    def test_an_unfinished_report_prunes_nothing_and_leaves_the_guard_armed(self) -> None:
        proc = self.review("report", "--review-only", check=False)

        self.assertEqual(proc.returncode, 0)
        self.assertTrue(self.inbox.exists())
        self.assertTrue(self.scratch.is_dir())
        self.assertTrue(self.guarding())

    def test_a_partial_report_prunes_nothing_and_leaves_the_guard_armed(self) -> None:
        self.review("brief", "W1-1")

        proc = self.review("report", "--review-only", "--partial")

        self.assertIn("PARTIAL", proc.stdout)
        self.assertTrue(self.inbox.exists())
        self.assertTrue(self.scratch.is_dir())
        self.assertTrue(self.guarding())

    def test_the_json_report_of_an_unfinished_review_changes_nothing_either(self) -> None:
        self.review("report", "--review-only", "--json", check=False)

        self.assertTrue(self.inbox.exists())
        self.assertTrue(self.guarding())

    def test_a_passed_review_is_verified_pruned_and_ends_the_guard(self) -> None:
        for slot in self.slot_ids():
            self.finish_hunter(slot)

        text = self.review("report", "--review-only").stdout

        self.assertIn("- Reviewer isolation status: verified via subagent stop events in the ledger.", text)
        self.assertFalse(self.inbox.exists())
        self.assertFalse(self.scratch.exists())
        self.assertFalse(self.guarding())

    def test_the_self_reviewed_text_is_unchanged(self) -> None:
        self.review("reset")
        self.write_cart()
        self.arm(guarding=False)
        self.review("prepare")  # no hook saw this session: advisory
        self.review("cover", "--hunks", "H1", "--note", NOTE)

        text = self.review("report", "--review-only").stdout

        self.assertIn("- Reviewer isolation status: SELF-REVIEWED", text)


class AntigravitySyncTests(HuntTestCase):
    """`prepare`, `brief`, `check` and `report` call the Antigravity sync before they judge anything."""

    def run_in_process(self, *args: str) -> tuple[int, list[tuple[Path, dict[str, str]]]]:
        """Run `review.main` without the ambient platform variables; what the sync was called with, copied at the call."""
        env = {k: v for k, v in os.environ.items() if k not in AMBIENT_PLATFORM_ENV_VARS}
        calls: list[tuple[Path, dict[str, str]]] = []

        def record(workspace: Any, environ: Mapping[str, str]) -> None:
            # A copy taken now: `environ` may be `os.environ` itself, which `mock.patch.dict` restores later.
            calls.append((Path(workspace.root), dict(environ)))

        with (
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch("craftsman.review.sync_antigravity_runs", side_effect=record),
        ):
            result = run_main(review.main, *args, "--cwd", str(self.repo.root))
        return result.code, calls

    def test_every_judging_command_syncs_first(self) -> None:
        self.write_cart()
        self.review("prepare")
        self.review("add", "--hunter", "lead", *FINDING_FLAGS)
        commands = (
            ("prepare",),
            ("brief", "W1-1"),
            ("brief", "--verifier", "F1"),
            ("check", "--review-only"),
            ("report", "--review-only"),
        )
        for args in commands:
            with self.subTest(args=args):
                code, calls = self.run_in_process(*args)
                self.assertIn(code, (0, 1))
                self.assertEqual(len(calls), 1)
                root, env = calls[0]
                self.assertEqual(root.resolve(), self.repo.root.resolve())
                self.assertTrue(AMBIENT_PLATFORM_ENV_VARS.isdisjoint(env), sorted(AMBIENT_PLATFORM_ENV_VARS & env.keys()))


if __name__ == "__main__":
    unittest.main()
