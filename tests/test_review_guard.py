"""Tests for the review guard shared by the Stop hooks (`craftsman.review_guard`)."""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from craftsman.attribution import RUN_STALE_AFTER, agent_runs, evidence_counter
from craftsman.gate import MAX_CONTINUATION_REASON_CHARS, MAX_CONTINUATIONS, GateOwner
from craftsman.ledger import add_agent_event, load, mutate
from craftsman.records import AgentEvent, Ledger, SlotClaim, format_timestamp, parse_timestamp
from craftsman.review_guard import (
    ABANDONED_NOTICE,
    DEEP_REQUESTED,
    HOOKS_ENFORCE,
    REVIEW_NOT_STARTED,
    GuardDecision,
    decide_review_stop,
    gate_stop_inputs,
    review_commands,
    review_outcome_issues,
    running_work,
    serve_review_stop,
)
from craftsman.workspace import ReviewMarker, arm_marker, read_marker, write_marker

from tests.support import (
    T0,
    EvidenceRepo,
    agent_event,
    at,
    make_hunk,
    make_slot,
    make_snap,
    open_gate,
    planned,
    prepared,
)

CALLER = GateOwner("claude", "sess-1")
CART = "def total(items):\n    return sum(items)\n"


class ReviewCommandsTests(unittest.TestCase):
    def test_every_spelling_of_the_cli_is_found(self) -> None:
        commands = {
            "craftsman review prepare": ("prepare",),
            "craftsman.exe review check --review-only": ("check",),
            "/usr/local/bin/craftsman review show --json": ("show",),
            "python -m craftsman.cli review prepare": ("prepare",),
            "python3.12 -u -m craftsman.cli review cover --hunks H1": ("cover",),
            "python scripts/craftsman-cli.py review report": ("report",),
            "scripts/craftsman-cli.py review reset": ("reset",),
            "FOO=1 craftsman review brief W1-1": ("brief",),
            "cd /repo && craftsman review prepare && craftsman review check": ("prepare", "check"),
            "bash -c 'craftsman review prepare'": ("prepare",),
            "uv run craftsman review prepare": ("prepare",),
            "uv run --quiet craftsman review check": ("check",),
            "uvx craftsman review prepare": ("prepare",),
            "pipx run craftsman review reset": ("reset",),
            "poetry run craftsman review show": ("show",),
            "npx craftsman review prepare": ("prepare",),
            "python -m pipx run craftsman review prepare": ("prepare",),
            "uv run python -m craftsman.cli review prepare": ("prepare",),
        }
        for command, expected in commands.items():
            with self.subTest(command=command):
                self.assertEqual(review_commands(command), expected)

    def test_text_that_only_mentions_the_command_names_none(self) -> None:
        commands = (
            "echo craftsman review prepare",
            'git commit -m "craftsman review prepare"',
            "grep -r 'craftsman review' docs",
            "craftsman gate --init",
            "craftsman review",
            "craftsman review --help",
            "python other.py review prepare",
            "python -m pytest review prepare",
            "mycraftsman review prepare",
            "uv run pytest review prepare",
            "uv craftsman review prepare",
            "uv run",
            "uvx",
            "pipx run",
            "",
        )
        for command in commands:
            with self.subTest(command=command):
                self.assertEqual(review_commands(command), ())


class RepoCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = EvidenceRepo(Path(tmp.name) / "repo")
        self.ledger_path = self.repo.ledger_dir / "ledger.json"

    def load(self) -> Ledger:
        return load(self.ledger_path)


class RunningWorkTests(unittest.TestCase):
    """Only OPEN runs of this round exempt a stop; a claim alone never does."""

    def ledger(self, *slot_ids: str) -> Ledger:
        ledger, _ = prepared(make_snap(make_hunk("H1", "a.py"), make_hunk("H2", "b.py")))
        ledger = replace(ledger, rounds=(replace(ledger.rounds[0], prepared_at=T0, enforced=True),))
        return planned(ledger, *(make_slot(slot_id, hunks=("H1",)) for slot_id in slot_ids)) if slot_ids else ledger

    def work(self, ledger: Ledger, seconds: float = 100) -> tuple[str, ...]:
        return running_work(ledger, ledger.rounds[-1], parse_timestamp(at(seconds)))

    @staticmethod
    def with_events(ledger: Ledger, *events: AgentEvent) -> Ledger:
        for event in events:
            ledger = add_agent_event(ledger, event)
        return ledger

    def test_nothing_runs_in_a_round_nobody_launched(self) -> None:
        self.assertEqual(self.work(self.ledger("W1-1")), ())

    def test_a_hunter_that_started_and_fetched_no_brief_yet_is_named_by_its_role(self) -> None:
        ledger = self.with_events(self.ledger("W1-1"), agent_event("start", "craftsman-reviewer", "h1", 10))

        self.assertEqual(self.work(ledger), ("craftsman-reviewer",))

    def test_several_starting_hunters_are_counted(self) -> None:
        ledger = self.with_events(
            self.ledger("W1-1", "W1-2", "W1-3"),
            *(agent_event("start", "craftsman-reviewer", f"h{n}", 10 + n) for n in range(3)),
        )

        self.assertEqual(self.work(ledger), ("craftsman-reviewer x3",))

    def test_a_slot_whose_hunter_runs_is_named_by_its_id(self) -> None:
        ledger = self.ledger("W1-1", "W1-2")
        ledger = replace(ledger, claims=(SlotClaim(1, "W1-2", "B1", at(11)),))
        ledger = self.with_events(ledger, agent_event("start", "craftsman-reviewer", "h1", 10))

        self.assertEqual(self.work(ledger), ("W1-2",))

    def test_slots_and_unplaced_runs_are_listed_together(self) -> None:
        ledger = self.ledger("W1-1", "W1-2")
        ledger = replace(ledger, claims=(SlotClaim(1, "W1-1", "B1", at(11)),))
        ledger = self.with_events(
            ledger,
            agent_event("start", "craftsman-reviewer", "h1", 10),
            agent_event("start", "craftsman-reviewer", "h2", 12),
            agent_event("start", "craftsman-verifier", "v1", 13),
        )

        self.assertEqual(self.work(ledger), ("W1-1", "craftsman-reviewer", "craftsman-verifier"))

    def test_a_claim_without_an_open_run_exempts_nothing(self) -> None:
        ledger = replace(self.ledger("W1-1"), claims=(SlotClaim(1, "W1-1", "B1", at(11)),))

        self.assertEqual(self.work(ledger), ())

    def test_a_run_that_stopped_or_went_stale_or_is_of_an_earlier_round_is_not_work(self) -> None:
        stale_for = RUN_STALE_AFTER.total_seconds() + 1
        cases = {
            "stopped": (agent_event("start", "craftsman-reviewer", "h1", 10), agent_event("stop", "craftsman-reviewer", "h1", 20)),
            "stale": (agent_event("start", "craftsman-reviewer", "h1", 10),),
            "earlier round": (agent_event("start", "craftsman-reviewer", "h1", -10),),
        }
        for name, events in cases.items():
            with self.subTest(name):
                seconds = 10 + stale_for if name == "stale" else 100
                self.assertEqual(self.work(self.with_events(self.ledger("W1-1"), *events), seconds), ())

    def test_the_edge_of_staleness(self) -> None:
        ledger = self.with_events(self.ledger("W1-1"), agent_event("start", "craftsman-reviewer", "h1", 10))
        edge = 10 + RUN_STALE_AFTER.total_seconds()

        self.assertEqual(self.work(ledger, edge - 1), ("craftsman-reviewer",))
        self.assertEqual(self.work(ledger, edge), ())

    def test_a_nudge_does_not_end_the_run(self) -> None:
        ledger = self.with_events(
            self.ledger("W1-1"),
            agent_event("start", "craftsman-reviewer", "h1", 10),
            agent_event("nudge", "craftsman-reviewer", "h1", 50),
        )

        self.assertEqual(self.work(ledger), ("craftsman-reviewer",))

    def test_a_round_without_a_plan_counts_every_open_hunter(self) -> None:
        ledger = self.with_events(
            self.ledger(),
            agent_event("start", "craftsman-reviewer", "h1", 10),
            agent_event("start", "craftsman-reviewer", "h2", 11),
        )

        self.assertEqual(self.work(ledger), ("craftsman-reviewer x2",))

    def test_a_naive_time_is_taken_as_utc(self) -> None:
        ledger = self.with_events(self.ledger("W1-1"), agent_event("start", "craftsman-reviewer", "h1", 10))
        naive = parse_timestamp(at(100)).replace(tzinfo=None)

        self.assertEqual(running_work(ledger, ledger.rounds[-1], naive), ("craftsman-reviewer",))

    def test_agents_that_are_not_craftsman_roles_are_not_work(self) -> None:
        ledger = self.with_events(self.ledger("W1-1"), agent_event("start", "Explore", "x", 10))

        self.assertEqual(self.work(ledger), ())


class GuardCase(RepoCase):
    """A repository with a guarded review: the marker, the changed file, the hooked `prepare`."""

    def setUp(self) -> None:
        super().setUp()
        self.repo.write("cart.py", CART)

    def arm(self, **fields: object) -> ReviewMarker:
        now = datetime.now(timezone.utc)
        arm_marker(self.ledger_path, CALLER, now - timedelta(seconds=30), fresh=True)
        arm_marker(self.ledger_path, CALLER, now, fresh=False)  # a hooked `craftsman review` command
        stored = read_marker(self.ledger_path)
        assert stored is not None
        marker = replace(stored, **fields)  # type: ignore[arg-type]
        write_marker(self.ledger_path, marker)
        return marker

    def prepare(self) -> None:
        self.repo.cli("review", "prepare")

    def decide(self, marker: ReviewMarker | None = None, caller: GateOwner = CALLER) -> GuardDecision:
        stored = marker or read_marker(self.ledger_path)
        return decide_review_stop(self.repo.root, self.ledger_path, stored, caller, datetime.now(timezone.utc))

    def start(self, agent: str = "craftsman-reviewer", agent_id: str = "h1") -> None:
        event = AgentEvent("claude", "start", agent, agent_id, format_timestamp(datetime.now(timezone.utc)))
        mutate(self.ledger_path, lambda ledger: (add_agent_event(ledger, event), None))


class AllowTests(GuardCase):
    def test_no_marker_allows(self) -> None:
        self.assertEqual(self.decide(None), GuardDecision())

    def test_a_marker_that_is_not_guarding_allows(self) -> None:
        marker = self.arm(guarding=False)

        self.assertEqual(self.decide(marker), GuardDecision())

    def test_the_marker_of_another_session_allows(self) -> None:
        marker = self.arm()

        self.assertEqual(self.decide(marker, GateOwner("claude", "someone-else")), GuardDecision())


class IssueTests(GuardCase):
    def issues(self, marker: ReviewMarker) -> list[str]:
        return [
            f"{issue.code}: {issue.message}"
            for issue in review_outcome_issues(self.repo.root, self.ledger_path, marker, CALLER)
        ]

    def test_a_review_nobody_touched_has_no_round(self) -> None:
        marker = self.arm(touched_at=None)

        (only,) = review_outcome_issues(self.repo.root, self.ledger_path, marker, CALLER)

        self.assertEqual((only.code, only.message), ("no-round", REVIEW_NOT_STARTED))
        self.assertIn("launch the hunters it lists", REVIEW_NOT_STARTED)

    def test_a_hooked_command_since_the_marker_counts_as_touched_even_without_a_round_newer_than_it(self) -> None:
        self.prepare()
        marker = self.arm()  # the marker is newer than the round: an unchanged diff keeps its round

        issues = self.issues(marker)

        self.assertNotIn(REVIEW_NOT_STARTED, "".join(issues))

    def placed(self, seconds: float, **fields: object) -> ReviewMarker:
        """A marker written `seconds` after the latest round was prepared."""
        prepared_at = parse_timestamp(self.load().rounds[-1].prepared_at)
        stamp = format_timestamp(prepared_at + timedelta(seconds=seconds))
        return ReviewMarker("claude", "sess-1", stamp, **fields)  # type: ignore[arg-type]

    def stamp(self, seconds: float) -> str:
        return format_timestamp(parse_timestamp(self.load().rounds[-1].prepared_at) + timedelta(seconds=seconds))

    def restamp(self, seconds: float) -> None:
        """Every coverage record and finding was recorded `seconds` after the latest round was prepared."""
        stamp = self.stamp(seconds)
        mutate(
            self.ledger_path,
            lambda ledger: (
                replace(
                    ledger,
                    coverage=tuple(replace(item, recorded_at=stamp) for item in ledger.coverage),
                    findings=tuple(replace(item, recorded_at=stamp) for item in ledger.findings),
                ),
                None,
            ),
        )

    def add_finding(self) -> None:
        self.repo.cli(
            "review", "add", "--lens", "contracts", "--file", "cart.py", "--line", "2", "--severity", "major",
            "--title", "Strings are summed without a check", "--scenario", "total(['a']) raises TypeError",
        )  # fmt: skip

    def test_a_touch_at_the_very_moment_the_marker_was_written_counts(self) -> None:
        self.prepare()
        marker = self.placed(60)
        marker = replace(marker, touched_at=marker.at)  # the PreToolUse that armed the marker touched it at once

        self.assertNotIn(REVIEW_NOT_STARTED, "".join(self.issues(marker)))

    def test_a_touch_older_than_the_marker_does_not_count(self) -> None:
        self.prepare()
        marker = self.placed(60)
        marker = replace(marker, touched_at=self.stamp(59))

        self.assertEqual(self.issues(marker), [f"no-round: {REVIEW_NOT_STARTED}"])

    def test_coverage_recorded_since_the_marker_counts_and_older_coverage_does_not(self) -> None:
        self.prepare()
        self.repo.cover_all()
        self.restamp(120)

        self.assertNotIn(REVIEW_NOT_STARTED, "".join(self.issues(self.placed(120))))
        self.assertEqual(self.issues(self.placed(121)), [f"no-round: {REVIEW_NOT_STARTED}"])

    def test_a_finding_recorded_since_the_marker_counts_and_an_older_one_does_not(self) -> None:
        self.prepare()
        self.add_finding()
        self.restamp(120)

        self.assertNotIn(REVIEW_NOT_STARTED, "".join(self.issues(self.placed(120))))
        self.assertEqual(self.issues(self.placed(121)), [f"no-round: {REVIEW_NOT_STARTED}"])

    def test_a_requested_deep_review_needs_a_deep_round(self) -> None:
        self.prepare()
        marker = self.arm(deep=True)

        self.assertEqual(self.issues(marker), [f"no-round: {DEEP_REQUESTED}"])

    def test_the_latest_round_must_be_enforced_in_a_hooked_session(self) -> None:
        self.prepare()  # nothing hooked it: an advisory round
        marker = self.arm()

        self.assertEqual(self.issues(marker), [f"no-round: {HOOKS_ENFORCE}"])


class WaitingTests(GuardCase):
    def hooked_round(self) -> ReviewMarker:
        marker = self.arm()
        self.prepare()
        return marker

    def test_a_running_hunter_allows_with_a_notice_and_changes_nothing(self) -> None:
        marker = replace(self.hooked_round(), forced=3, evidence_seen=0)
        write_marker(self.ledger_path, marker)
        self.start()

        decision = self.decide(marker)

        self.assertIsNone(decision.block)
        self.assertIsNone(decision.marker)  # no counter change
        assert decision.notice is not None
        self.assertIn("waiting for craftsman-reviewer", decision.notice)
        self.assertIn("`craftsman review check --review-only` will say to relaunch them", decision.notice)

    def test_under_a_gate_the_notice_names_the_gate_check(self) -> None:
        marker = self.hooked_round()
        open_gate(self.repo.root, self.repo.base_sha, owner=GateOwner("claude", "gate-owner"))
        self.start()

        decision = self.decide(marker)

        assert decision.notice is not None
        self.assertIn("`craftsman review check` will say to relaunch them", decision.notice)
        self.assertNotIn("--review-only", decision.notice)


class ProgressTests(GuardCase):
    def hooked_round(self) -> ReviewMarker:
        self.arm()
        self.prepare()
        return self.arm()

    def evidence(self) -> int:
        ledger = self.load()
        return evidence_counter(ledger, agent_runs(ledger))

    def test_the_first_block_counts_one_and_remembers_the_evidence(self) -> None:
        marker = self.hooked_round()

        decision = self.decide(marker)

        assert decision.block is not None and decision.marker is not None
        self.assertTrue(decision.block.startswith(f"[CRAFTSMAN REVIEW 1/{MAX_CONTINUATIONS}] "))
        self.assertEqual((decision.marker.forced, decision.marker.evidence_seen), (1, self.evidence()))
        self.assertTrue(decision.marker.guarding)

    def test_the_same_evidence_counts_up_and_new_evidence_starts_over(self) -> None:
        marker = replace(self.hooked_round(), forced=4, evidence_seen=self.evidence())
        decision = self.decide(marker)
        assert decision.marker is not None
        self.assertEqual(decision.marker.forced, 5)

        grown = replace(marker, evidence_seen=self.evidence() - 1)
        decision = self.decide(grown)
        assert decision.marker is not None
        self.assertEqual(decision.marker.forced, 1)

    def test_a_lower_counter_is_no_progress(self) -> None:
        marker = replace(self.hooked_round(), forced=2, evidence_seen=self.evidence() + 5)

        decision = self.decide(marker)

        assert decision.marker is not None
        self.assertEqual((decision.marker.forced, decision.marker.evidence_seen), (3, self.evidence()))

    def test_the_cap_abandons_the_review_and_says_it_is_not_complete(self) -> None:
        marker = replace(self.hooked_round(), forced=MAX_CONTINUATIONS, evidence_seen=self.evidence())

        decision = self.decide(marker)

        self.assertIsNone(decision.block)
        self.assertEqual(decision.notice, ABANDONED_NOTICE.format(check="craftsman review check --review-only"))
        assert decision.notice is not None
        self.assertIn("NOT complete", decision.notice)
        assert decision.marker is not None
        self.assertFalse(decision.marker.guarding)
        self.assertIsNotNone(decision.marker.abandoned_at)

    def test_the_last_reminder_before_the_cap_still_blocks(self) -> None:
        marker = replace(self.hooked_round(), forced=MAX_CONTINUATIONS - 1, evidence_seen=self.evidence())

        decision = self.decide(marker)

        assert decision.block is not None
        self.assertIn(f"[CRAFTSMAN REVIEW {MAX_CONTINUATIONS}/{MAX_CONTINUATIONS}]", decision.block)

    def test_under_a_gate_the_abandon_notice_names_the_gate_check(self) -> None:
        marker = replace(self.hooked_round(), forced=MAX_CONTINUATIONS, evidence_seen=self.evidence())
        open_gate(self.repo.root, self.repo.base_sha, owner=GateOwner("claude", "gate-owner"))

        decision = self.decide(marker)

        assert decision.notice is not None
        self.assertIn("`craftsman review check` lists what is missing", decision.notice)
        self.assertNotIn("--review-only", decision.notice)

    def test_a_block_under_a_gate_finishes_with_the_gate_check(self) -> None:
        marker = self.hooked_round()
        open_gate(self.repo.root, self.repo.base_sha, owner=GateOwner("claude", "gate-owner"))

        decision = self.decide(marker)

        assert decision.block is not None
        self.assertTrue(decision.block.endswith("Finish with `craftsman review check` passing."))
        self.assertNotIn("--review-only", decision.block)

    def test_a_block_outside_a_gate_finishes_with_the_review_only_check(self) -> None:
        decision = self.decide(self.hooked_round())

        assert decision.block is not None
        self.assertTrue(decision.block.endswith("Finish with `craftsman review check --review-only` passing."))

    def test_an_unreadable_ledger_ends_the_guard_with_a_notice(self) -> None:
        marker = self.hooked_round()
        self.ledger_path.write_text("{", encoding="utf-8")

        decision = self.decide(marker)

        self.assertIsNone(decision.block)
        assert decision.notice is not None and decision.marker is not None
        self.assertIn("could not check the review", decision.notice)
        self.assertFalse(decision.marker.guarding)

    def test_a_block_and_a_notice_exclude_each_other(self) -> None:
        with self.assertRaises(ValueError):
            GuardDecision(block="a", notice="b")


class ServeTests(GuardCase):
    def test_the_new_marker_is_stored(self) -> None:
        self.arm()
        self.prepare()
        self.arm()

        decision = serve_review_stop(self.repo.root, self.ledger_path, CALLER, datetime.now(timezone.utc))

        self.assertIsNotNone(decision.block)
        stored = read_marker(self.ledger_path)
        assert stored is not None
        self.assertEqual(stored.forced, 1)

    def test_with_no_wave_due_the_ledger_is_not_rewritten(self) -> None:
        self.arm()
        self.prepare()
        before = self.load()

        serve_review_stop(self.repo.root, self.ledger_path, CALLER, datetime.now(timezone.utc))

        self.assertEqual(self.load(), before)  # nothing due: the ledger is not even rewritten


class BudgetTests(RepoCase):
    def test_the_block_of_a_big_plan_fits_the_budget_and_ends_on_a_whole_sentence(self) -> None:
        count = 19
        for index in range(count):
            self.repo.write(f"mod_{index}.py", "".join(f"value_{n} = {n}\n" for n in range(60)))
        now = datetime.now(timezone.utc)
        arm_marker(self.ledger_path, CALLER, now - timedelta(seconds=30), fresh=True)
        arm_marker(self.ledger_path, CALLER, now, fresh=False)
        self.repo.cli("review", "prepare")
        arm_marker(self.ledger_path, CALLER, datetime.now(timezone.utc), fresh=False)

        decision = decide_review_stop(
            self.repo.root, self.ledger_path, read_marker(self.ledger_path), CALLER, datetime.now(timezone.utc)
        )

        assert decision.block is not None
        self.assertLessEqual(len(decision.block), MAX_CONTINUATION_REASON_CHARS)
        self.assertTrue(decision.block.endswith("passing."))
        self.assertNotIn("review cover --hunks", decision.block)
        self.assertNotIn("review verdict", decision.block)


class GateStopInputsTests(GuardCase):
    def test_without_a_ledger_nothing_runs_and_the_evidence_is_zero(self) -> None:
        inputs = gate_stop_inputs(self.ledger_path, datetime.now(timezone.utc))

        self.assertEqual((inputs.running, inputs.evidence), ((), 0))

    def test_an_unreadable_ledger_tells_nothing(self) -> None:
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        self.ledger_path.write_text("{", encoding="utf-8")

        inputs = gate_stop_inputs(self.ledger_path, datetime.now(timezone.utc))

        self.assertEqual((inputs.running, inputs.evidence), ((), None))

    def test_a_running_hunter_and_the_evidence_are_reported(self) -> None:
        self.arm()
        self.prepare()
        self.start()

        inputs = gate_stop_inputs(self.ledger_path, datetime.now(timezone.utc))

        self.assertEqual(inputs.running, ("craftsman-reviewer",))
        self.assertEqual(inputs.evidence, 1)  # the round


class MarkerJsonTests(GuardCase):
    def test_the_stored_marker_has_no_leftover_best_counter(self) -> None:
        marker = self.arm()

        write_marker(self.ledger_path, marker)

        self.assertNotIn("best", json.loads((self.repo.ledger_dir / "review_hooks.json").read_text(encoding="utf-8")))


if __name__ == "__main__":
    unittest.main()
