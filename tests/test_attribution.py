"""Attribution: which subagent run vouches for which ledger record, and what that credits."""

from __future__ import annotations

import unittest
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import datetime

from craftsman.attribution import (
    AgentRun,
    SlotWork,
    agent_runs,
    bound_slots,
    credited_lenses,
    distinct_verdict_runs,
    evidence_counter,
    finding_slots,
    identity,
    slot_work,
    verdict_vouched,
    vouching_runs,
)
from craftsman.ledger import add_agent_event, add_finding, next_claim_token, resolve, set_verdict
from craftsman.lenses import LENS_IDS
from craftsman.records import (
    AgentEvent,
    AgentRole,
    Coverage,
    HunkRef,
    Ledger,
    SlotClaim,
    Verdict,
    VerdictEvent,
    parse_timestamp,
)

from tests.support import (
    FINDING,
    T0,
    agent_event,
    at,
    coverage_note,
    make_hunk,
    make_slot,
    make_snap,
    planned,
    prepared,
    record_run,
)

REVIEWER = "craftsman-reviewer"
VERIFIER = "craftsman-verifier"
AGY = "antigravity"

EVIDENCE = "guarded by cli.py:19, which rejects an empty list before total() runs"
EVIDENCE_OF = {
    "dismissed": EVIDENCE,
    "probable": "no-repro: needs a live database; cli.py:19 fails on an empty list",
    "question": "intent: the task asks for retries; cli.py:19 retries forever",
    "proven": "Reproduced via tests/test_app.py::test_empty (fails).",
}
SLOT_LENSES = {"W1-1": ("intent",), "W1-2": ("contracts",), "W1-3": ("failure",), "W1-4": ("ripple", "tests")}


FOREVER = 9_999_999_999  # seconds after T0: a run that is still open whenever the test records something


def moment(seconds: float) -> datetime:
    return parse_timestamp(at(seconds))


def hunt_ledger(slots: Sequence[str] = ("W1-1", "W1-2", "W1-3", "W1-4")) -> Ledger:
    """One round at `T0` with a hunk per slot (slot n owns Hn) and a plan of `slots`."""
    snap = make_snap(*(make_hunk(f"H{n}", f"f{n}.py") for n in range(1, len(slots) + 1)))
    ledger, _ = prepared(snap)
    ledger = replace(ledger, rounds=(replace(ledger.rounds[0], prepared_at=T0),))
    return planned(
        ledger,
        *(make_slot(slot, hunks=(f"H{n}",), lenses=SLOT_LENSES.get(slot, ("intent",))) for n, slot in enumerate(slots, 1)),
    )


def slot_hunks(ledger: Ledger, slot: str, number: int = 1) -> tuple[str, ...]:
    plan = ledger.rounds[number - 1].plan
    assert plan is not None
    found = plan.slot(slot)
    assert found is not None
    return found.hunks


def cover(
    ledger: Ledger, slot: str | None, seconds: float, *, by: str | None = None, number: int = 1
) -> tuple[Ledger, str]:
    """Append coverage of a slot's hunks (all open hunks of the round when untagged); returns the new token."""
    hunks = ledger.rounds[number - 1].hunks
    ids = slot_hunks(ledger, slot, number) if slot is not None else tuple(h.id for h in hunks)
    digests = tuple(h.digest for h in hunks if h.id in ids)
    record = Coverage(number, digests, coverage_note(ids), at(seconds), hunter=slot, by=by)
    return replace(ledger, coverage=(*ledger.coverage, record)), f"C{len(ledger.coverage) + 1}"


def claim(ledger: Ledger, slot: str, seconds: float, *, by: str | None = None, number: int = 1) -> tuple[Ledger, str]:
    token = next_claim_token(ledger)
    return replace(ledger, claims=(*ledger.claims, SlotClaim(number, slot, token, at(seconds), by))), token


def with_agents(ledger: Ledger, *events: AgentEvent) -> Ledger:
    for event in events:
        ledger = add_agent_event(ledger, event)
    return ledger


def run_events(
    agent_id: str, start: float, stop: float, *, agent: str = REVIEWER, platform: str = "claude",
    records: tuple[str, ...] | None = None,
) -> tuple[AgentEvent, AgentEvent]:
    return (
        agent_event("start", agent, agent_id, start, platform=platform),
        agent_event("stop", agent, agent_id, stop, platform=platform, records=records),
    )


def order_of(ledger: Ledger) -> Callable[[int], list[str]]:
    def order(number: int) -> list[str]:
        plan = ledger.rounds[number - 1].plan
        return [slot.id for slot in plan.slots] if plan else []

    return order


def work(ledger: Ledger) -> dict[tuple[int, str], SlotWork]:
    return slot_work(ledger, agent_runs(ledger), order_of(ledger))


def accepted_slots(ledger: Ledger, number: int = 1) -> set[str]:
    return {slot for (round_number, slot) in work(ledger) if round_number == number}


class AgentRunTests(unittest.TestCase):
    def test_runs_of_both_roles_pair_by_agent_id(self) -> None:
        ledger = with_agents(
            hunt_ledger(),
            *run_events("r1", 1, 5, records=("C1",)),
            *run_events("v1", 2, 6, agent=VERIFIER, platform=AGY),
        )

        runs = agent_runs(ledger)

        self.assertEqual(
            [(r.role, r.platform, r.agent_id, r.started, r.stopped, r.records) for r in runs],
            [
                ("reviewer", "claude", "r1", moment(1), moment(5), ("C1",)),
                ("verifier", AGY, "v1", moment(2), moment(6), None),
            ],
        )

    def test_a_stop_pairs_with_the_earliest_unpaired_start_of_its_id(self) -> None:
        ledger = with_agents(
            hunt_ledger(),
            agent_event("start", REVIEWER, "r1", 1),
            agent_event("start", REVIEWER, "r1", 10),
            agent_event("stop", REVIEWER, "r1", 12),
            agent_event("stop", REVIEWER, "r1", 20),
        )

        runs = agent_runs(ledger)

        self.assertEqual([(r.started, r.stopped) for r in runs], [(moment(1), moment(12)), (moment(10), moment(20))])

    def test_a_repeated_stop_extends_the_run_of_the_latest_start_of_its_id(self) -> None:
        ledger = with_agents(
            hunt_ledger(),
            agent_event("start", REVIEWER, "a", 0),
            agent_event("start", REVIEWER, "a", 1),
            agent_event("stop", REVIEWER, "a", 2),
            agent_event("stop", REVIEWER, "a", 3),
            agent_event("stop", REVIEWER, "a", 4),
        )

        self.assertEqual(
            [(r.started, r.stopped) for r in agent_runs(ledger)], [(moment(0), moment(2)), (moment(1), moment(4))]
        )

    def test_a_repeated_stop_prefers_its_id_over_an_untaken_start_without_one(self) -> None:
        ledger = with_agents(
            hunt_ledger(),
            agent_event("start", REVIEWER, "a", 0),
            agent_event("start", REVIEWER, "", 1),
            agent_event("stop", REVIEWER, "a", 5),
            agent_event("stop", REVIEWER, "a", 6),
        )

        self.assertEqual(
            [(r.agent_id, r.started, r.stopped) for r in agent_runs(ledger)],
            [("a", moment(0), moment(6)), ("", moment(1), None)],
        )

    def test_two_starts_and_two_stops_of_one_id_leave_no_run_unstopped(self) -> None:
        ledger = with_agents(
            hunt_ledger(),
            agent_event("start", REVIEWER, "a", 1),
            agent_event("start", REVIEWER, "a", 2),
            agent_event("stop", REVIEWER, "a", 5),
            agent_event("stop", REVIEWER, "a", 8),
        )

        self.assertEqual([r.stopped for r in agent_runs(ledger)], [moment(5), moment(8)])

    def test_a_stop_of_another_id_never_takes_a_running_start(self) -> None:
        ledger = with_agents(
            hunt_ledger(),
            agent_event("start", REVIEWER, "a", 1, platform="claude"),
            agent_event("stop", REVIEWER, "b", 5, platform="claude", records=("C1",)),
        )

        runs = agent_runs(ledger)

        self.assertEqual(
            [(r.agent_id, r.started, r.stopped, r.records) for r in runs],
            [("b", moment(0), moment(5), ("C1",)), ("a", moment(1), None, None)],
        )

    def test_a_stop_with_an_id_pairs_with_a_start_that_has_none(self) -> None:
        ledger = with_agents(
            hunt_ledger(),
            agent_event("start", REVIEWER, "", 1, platform="copilot"),
            agent_event("stop", REVIEWER, "x", 5, platform="copilot"),
        )

        (run,) = agent_runs(ledger)

        self.assertEqual((run.agent_id, run.started, run.stopped), ("x", moment(1), moment(5)))

    def test_roles_never_pair_across(self) -> None:
        ledger = with_agents(
            hunt_ledger(),
            agent_event("start", REVIEWER, "x", 1),
            agent_event("stop", VERIFIER, "x", 5),
        )

        runs = agent_runs(ledger)

        self.assertEqual(
            [(r.role, r.started, r.stopped) for r in runs],
            [("verifier", moment(0), moment(5)), ("reviewer", moment(1), None)],
        )

    def test_a_second_stop_of_a_started_id_extends_its_run_instead_of_making_another(self) -> None:
        ledger = with_agents(
            hunt_ledger(),
            agent_event("start", REVIEWER, "a", 1),
            agent_event("stop", REVIEWER, "a", 5, records=("B1", "C1")),
            agent_event("stop", REVIEWER, "a", 9, records=("C2",)),
        )

        (run,) = agent_runs(ledger)

        self.assertEqual((run.started, run.stopped, run.records), (moment(1), moment(9), ("B1", "C1", "C2")))

    def test_an_extended_run_has_unknown_records_when_either_stop_has_none(self) -> None:
        for first, second in ((("C1",), None), (None, ("C2",)), (None, None)):
            with self.subTest(first=first, second=second):
                ledger = with_agents(
                    hunt_ledger(),
                    agent_event("start", REVIEWER, "a", 1),
                    agent_event("stop", REVIEWER, "a", 5, records=first),
                    agent_event("stop", REVIEWER, "a", 9, records=second),
                )

                (run,) = agent_runs(ledger)

                self.assertEqual((run.stopped, run.records), (moment(9), None))

    def test_a_stop_that_comes_out_of_order_keeps_the_later_stop_time(self) -> None:
        ledger = with_agents(
            hunt_ledger(),
            agent_event("start", REVIEWER, "a", 1),
            agent_event("stop", REVIEWER, "a", 9),
            agent_event("stop", REVIEWER, "a", 5),
        )

        (run,) = agent_runs(ledger)

        self.assertEqual(run.stopped, moment(9))

    def test_one_start_and_two_stops_without_ids_make_two_runs_today(self) -> None:
        """Documents the pairing Phase F relies on: it stops recording the second stop of a subagent."""
        ledger = with_agents(
            hunt_ledger(),
            agent_event("start", REVIEWER, "", 1, platform="copilot"),
            agent_event("stop", REVIEWER, "", 5, platform="copilot"),
            agent_event("stop", REVIEWER, "", 9, platform="copilot"),
        )

        runs = agent_runs(ledger)

        self.assertEqual([(r.started, r.stopped) for r in runs], [(moment(0), moment(9)), (moment(1), moment(5))])

    def test_stops_without_an_id_pair_first_in_first_out_by_name(self) -> None:
        ledger = with_agents(
            hunt_ledger(),
            agent_event("start", REVIEWER, "", 1, platform="copilot"),
            agent_event("start", REVIEWER, "", 2, platform="copilot"),
            agent_event("stop", REVIEWER, "", 5, platform="copilot"),
            agent_event("stop", REVIEWER, "", 8, platform="copilot"),
        )

        runs = agent_runs(ledger)

        self.assertEqual([(r.started, r.stopped) for r in runs], [(moment(1), moment(5)), (moment(2), moment(8))])

    def test_a_stop_without_a_start_begins_when_the_current_round_was_prepared(self) -> None:
        ledger = with_agents(hunt_ledger(), agent_event("stop", VERIFIER, "v1", 7))

        (run,) = agent_runs(ledger)

        self.assertEqual((run.role, run.started, run.stopped), ("verifier", moment(0), moment(7)))

    def test_a_start_nobody_stopped_is_a_run_that_has_not_stopped(self) -> None:
        ledger = with_agents(hunt_ledger(), agent_event("start", REVIEWER, "r1", 3))

        (run,) = agent_runs(ledger)

        self.assertEqual((run.started, run.stopped, run.records), (moment(3), None, None))

    def test_agents_that_are_not_craftsman_roles_are_ignored(self) -> None:
        ledger = with_agents(hunt_ledger(), *run_events("x", 1, 2, agent="Explore"))

        self.assertEqual(agent_runs(ledger), ())

    def test_runs_come_oldest_start_first(self) -> None:
        ledger = with_agents(hunt_ledger(), *run_events("late", 20, 25), *run_events("early", 1, 5))

        self.assertEqual([r.agent_id for r in agent_runs(ledger)], ["early", "late"])


class IdentityTests(unittest.TestCase):
    def test_parses_platform_and_id(self) -> None:
        self.assertEqual(identity("antigravity:8bf67bba"), (AGY, "8bf67bba"))

    def test_keeps_colons_of_the_id(self) -> None:
        self.assertEqual(identity("antigravity:a:b"), (AGY, "a:b"))

    def test_no_identity_for_missing_or_malformed(self) -> None:
        for value in (None, "", "antigravity", "antigravity:", ":id"):
            with self.subTest(value=value):
                self.assertIsNone(identity(value))


def make_run(
    agent_id: str, start: float, stop: float | None, *, role: AgentRole = "reviewer", platform: str = "claude",
    records: tuple[str, ...] | None = None,
) -> AgentRun:
    return AgentRun(
        role,
        platform,
        agent_id,
        moment(start),
        None if stop is None else moment(stop),
        records,
    )


class VouchingTests(unittest.TestCase):
    def test_identity_is_decisive(self) -> None:
        mine = make_run("mine", 0, 10, platform=AGY)
        other = make_run("other", 0, 10, platform=AGY)

        vouchers = vouching_runs([mine, other], role="reviewer", token="C1", recorded_at=moment(5), by="antigravity:mine")

        self.assertEqual([(v.run, v.exact) for v in vouchers], [(mine, True)])

    def test_identity_outside_the_window_vouches_for_nobody_not_even_a_run_in_the_window(self) -> None:
        gone = make_run("gone", 0, 3, platform=AGY)
        other = make_run("other", 0, 10, platform=AGY)

        vouchers = vouching_runs([gone, other], role="reviewer", token="C1", recorded_at=moment(5), by="antigravity:gone")

        self.assertEqual(vouchers, ())

    def test_identity_must_match_the_platform_too(self) -> None:
        run = make_run("mine", 0, 10, platform="claude")

        self.assertEqual(vouching_runs([run], role="reviewer", token="C1", recorded_at=moment(5), by="antigravity:mine"), ())

    def test_a_transcript_vouches_for_the_tokens_it_shows(self) -> None:
        run = make_run("r", 0, 10, records=("C1",))

        shown = vouching_runs([run], role="reviewer", token="C1", recorded_at=moment(5), by=None)
        absent = vouching_runs([run], role="reviewer", token="C2", recorded_at=moment(5), by=None)

        self.assertEqual([(v.run, v.exact) for v in shown], [(run, True)])
        self.assertEqual(absent, ())

    def test_an_unread_transcript_falls_back_to_the_time_window(self) -> None:
        run = make_run("r", 0, 10)

        inside = vouching_runs([run], role="reviewer", token="C1", recorded_at=moment(5), by=None)
        outside = vouching_runs([run], role="reviewer", token="C1", recorded_at=moment(11), by=None)

        self.assertEqual([(v.run, v.exact) for v in inside], [(run, False)])
        self.assertEqual(outside, ())

    def test_a_record_is_only_vouched_inside_the_window_even_with_the_token(self) -> None:
        run = make_run("r", 0, 10, records=("C1",))

        self.assertEqual(vouching_runs([run], role="reviewer", token="C1", recorded_at=moment(11), by=None), ())

    def test_runs_that_have_not_stopped_vouch_for_nothing(self) -> None:
        run = make_run("r", 0, None)

        self.assertEqual(vouching_runs([run], role="reviewer", token="C1", recorded_at=moment(5), by=None), ())

    def test_only_runs_of_the_right_role_vouch(self) -> None:
        verifier = make_run("v", 0, 10, role="verifier")

        self.assertEqual(vouching_runs([verifier], role="reviewer", token="C1", recorded_at=moment(5), by=None), ())
        self.assertEqual(len(vouching_runs([verifier], role="verifier", token="V1", recorded_at=moment(5), by=None)), 1)

    def test_an_antigravity_run_never_vouches_by_window_for_a_record_without_an_identity(self) -> None:
        run = make_run("a", 0, 10, platform=AGY)

        self.assertEqual(vouching_runs([run], role="reviewer", token="C1", recorded_at=moment(5), by=None), ())

    def test_an_antigravity_run_whose_transcript_shows_the_token_still_vouches(self) -> None:
        run = make_run("a", 0, 10, platform=AGY, records=("C1",))

        (voucher,) = vouching_runs([run], role="reviewer", token="C1", recorded_at=moment(5), by=None)

        self.assertTrue(voucher.exact)

    def test_a_voucher_remembers_the_position_of_its_run(self) -> None:
        first = make_run("a", 0, 10)
        second = make_run("b", 0, 10)

        vouchers = vouching_runs([first, second], role="reviewer", token="C1", recorded_at=moment(5), by=None)

        self.assertEqual([v.index for v in vouchers], [0, 1])


class SlotWorkTests(unittest.TestCase):
    def test_a_claimed_slot_is_accepted_and_reports_its_work(self) -> None:
        ledger = hunt_ledger()
        ledger, _ = claim(ledger, "W1-1", 1, by="antigravity:a")
        ledger, _ = cover(ledger, "W1-1", 3, by="antigravity:a")
        ledger = with_agents(ledger, *run_events("a", 0, 5, platform=AGY))

        result = work(ledger)

        self.assertEqual(set(result), {(1, "W1-1")})
        self.assertEqual(
            result[(1, "W1-1")],
            SlotWork(1, "W1-1", frozenset(["d-f1.py-+x = 1"]), moment(3), ("a",)),
        )

    def test_the_orchestrators_own_coverage_outside_any_run_is_rejected(self) -> None:
        ledger = hunt_ledger()
        ledger, _ = cover(ledger, "W1-1", 20)
        ledger, _ = cover(ledger, "W1-2", 21, by="antigravity:orchestrator")
        ledger = with_agents(ledger, *run_events("hunter", 0, 5, platform=AGY))

        self.assertEqual(work(ledger), {})

    def test_a_run_that_claimed_two_slots_is_accepted_for_the_earliest_only(self) -> None:
        ledger = hunt_ledger()
        ledger, b1 = claim(ledger, "W1-1", 1)
        ledger, b2 = claim(ledger, "W1-2", 2)
        ledger, c1 = cover(ledger, "W1-1", 3)
        ledger, c2 = cover(ledger, "W1-2", 4)
        ledger = with_agents(ledger, *run_events("one", 0, 9, records=(b1, b2, c1, c2)))

        self.assertEqual(accepted_slots(ledger), {"W1-1"})

    def test_a_resumed_agent_cannot_unlock_a_second_slot(self) -> None:
        ledger = hunt_ledger()
        ledger, _ = claim(ledger, "W1-1", 1, by="antigravity:a")
        ledger, _ = cover(ledger, "W1-1", 2, by="antigravity:a")
        ledger, _ = claim(ledger, "W1-2", 11, by="antigravity:a")
        ledger, _ = cover(ledger, "W1-2", 12, by="antigravity:a")
        ledger = with_agents(ledger, *run_events("a", 0, 5, platform=AGY), *run_events("a", 10, 15, platform=AGY))

        self.assertEqual(accepted_slots(ledger), {"W1-1"})

    def test_a_claim_the_run_does_not_vouch_for_does_not_bind_it(self) -> None:
        ledger = hunt_ledger()
        ledger, _ = claim(ledger, "W1-3", 1, by="antigravity:someone-else")
        ledger, _ = cover(ledger, "W1-2", 3, by="antigravity:a")
        ledger = with_agents(ledger, *run_events("a", 0, 5, platform=AGY))

        self.assertEqual(accepted_slots(ledger), {"W1-2"})

    def test_a_run_that_claimed_other_slots_only_is_not_accepted_for_a_slot_it_never_claimed(self) -> None:
        ledger = hunt_ledger()
        ledger, b1 = claim(ledger, "W1-1", 1)
        ledger, c2 = cover(ledger, "W1-2", 3)
        ledger = with_agents(ledger, *run_events("a", 0, 5, records=(b1, c2)))

        self.assertEqual(accepted_slots(ledger), set())

    def test_a_run_without_claims_binds_the_first_slot_in_plan_order(self) -> None:
        ledger = hunt_ledger()
        ledger, c3 = cover(ledger, "W1-3", 1)
        ledger, c2 = cover(ledger, "W1-2", 2)
        ledger = with_agents(ledger, *run_events("a", 0, 5, records=(c3, c2)))

        self.assertEqual(accepted_slots(ledger), {"W1-2"})

    def test_two_runs_each_answer_for_their_own_slot(self) -> None:
        ledger = hunt_ledger()
        ledger, b1 = claim(ledger, "W1-1", 1)
        ledger, b2 = claim(ledger, "W1-2", 1)
        ledger, c1 = cover(ledger, "W1-1", 2)
        ledger, c2 = cover(ledger, "W1-2", 2)
        ledger = with_agents(ledger, *run_events("a", 0, 5, records=(b1, c1)), *run_events("b", 0, 5, records=(b2, c2)))

        self.assertEqual(accepted_slots(ledger), {"W1-1", "W1-2"})

    def test_untagged_coverage_is_not_slot_work(self) -> None:
        ledger = hunt_ledger()
        ledger, _ = cover(ledger, None, 2)
        ledger = with_agents(ledger, *run_events("a", 0, 5))

        self.assertEqual(work(ledger), {})

    def test_a_run_that_has_not_stopped_credits_nothing(self) -> None:
        ledger = hunt_ledger()
        ledger, _ = claim(ledger, "W1-1", 1, by="antigravity:a")
        ledger, _ = cover(ledger, "W1-1", 2, by="antigravity:a")
        ledger = with_agents(ledger, agent_event("start", REVIEWER, "a", 0, platform=AGY))

        self.assertEqual(work(ledger), {})

    def test_one_window_run_covering_three_slots_credits_the_first_only(self) -> None:
        ledger = hunt_ledger()
        for slot, seconds in (("W1-3", 1), ("W1-1", 2), ("W1-2", 3)):
            ledger, _ = cover(ledger, slot, seconds)
        ledger = with_agents(ledger, *run_events("w", 0, 9, platform="copilot"))

        self.assertEqual(accepted_slots(ledger), {"W1-1"})

    def test_parallel_window_runs_credit_one_slot_each(self) -> None:
        ledger = hunt_ledger()
        for slot, seconds in (("W1-1", 2), ("W1-2", 3), ("W1-3", 4)):
            ledger, _ = cover(ledger, slot, seconds)
        ledger = with_agents(
            ledger, *run_events("w1", 0, 9, platform="copilot"), *run_events("w2", 1, 8, platform="copilot")
        )

        self.assertEqual(accepted_slots(ledger), {"W1-1", "W1-2"})

    def test_window_runs_are_capped_also_in_a_round_with_an_exact_slot(self) -> None:
        ledger = hunt_ledger()
        ledger, b1 = claim(ledger, "W1-1", 1)
        ledger, c1 = cover(ledger, "W1-1", 2)
        ledger, _ = cover(ledger, "W1-2", 4)
        ledger, _ = cover(ledger, "W1-3", 5)
        ledger = with_agents(
            ledger,
            *run_events("exact", 0, 3, records=(b1, c1)),
            *run_events("window", 3.5, 9, platform="copilot"),
        )

        self.assertEqual(accepted_slots(ledger), {"W1-1", "W1-2"})

    def test_a_window_run_does_not_cap_a_slot_with_an_exact_voucher(self) -> None:
        ledger = hunt_ledger()
        ledger, b1 = claim(ledger, "W1-1", 1)
        ledger, c1 = cover(ledger, "W1-1", 2)
        ledger, c2 = cover(ledger, "W1-3", 2.5)
        ledger, b2 = claim(ledger, "W1-3", 1.5)
        ledger = with_agents(
            ledger,
            *run_events("a", 0, 5, records=(b1, c1)),
            *run_events("b", 0, 5, records=(b2, c2)),
            *run_events("window", 0, 5, platform="copilot"),
        )

        self.assertEqual(accepted_slots(ledger), {"W1-1", "W1-3"})

    def test_an_idle_window_run_does_not_give_an_exact_hunter_a_second_slot(self) -> None:
        ledger = hunt_ledger()
        ledger, b1 = claim(ledger, "W1-1", 1)
        ledger, c1 = cover(ledger, "W1-1", 2)
        ledger, c2 = cover(ledger, "W1-2", 3)
        ledger = with_agents(
            ledger,
            *run_events("a", 0, 5, records=(b1, c1, c2)),
            *run_events("idle", 0, 5, platform="copilot"),
        )

        self.assertEqual(accepted_slots(ledger), {"W1-1"})

    def test_a_window_record_adds_nothing_to_a_slot_an_exact_run_holds(self) -> None:
        ledger = hunt_ledger()
        ledger, b1 = claim(ledger, "W1-1", 1)
        ledger, c1 = cover(ledger, "W1-1", 2)
        ledger, _ = cover(ledger, "W1-1", 6)  # made in the unread run's window by somebody else
        ledger = with_agents(
            ledger,
            *run_events("a", 0, 3, records=(b1, c1)),
            *run_events("unread", 5, 9, platform="copilot"),
        )
        first_hunk = ledger.rounds[0].hunks[0].digest

        (accepted,) = work(ledger).values()
        self.assertEqual(accepted.digests, frozenset({first_hunk}))
        self.assertEqual(accepted.first_at, moment(2))
        self.assertEqual(accepted.runs, ("a",))

    def test_an_antigravity_record_without_an_identity_is_not_slot_work(self) -> None:
        ledger = hunt_ledger()
        ledger, _ = cover(ledger, "W1-2", 3, by=None)  # `env -u ANTIGRAVITY_CONVERSATION_ID craftsman review cover`
        ledger = with_agents(ledger, *run_events("a", 0, 5, platform=AGY))

        self.assertEqual(accepted_slots(ledger), set())

    def test_a_bound_agent_does_not_vouch_a_second_slot_by_window(self) -> None:
        ledger = hunt_ledger()
        ledger, b1 = claim(ledger, "W1-1", 1)
        ledger, c1 = cover(ledger, "W1-1", 2)
        ledger, _ = cover(ledger, "W1-2", 12)
        ledger = with_agents(
            ledger,
            *run_events("a", 0, 5, records=(b1, c1)),
            *run_events("a", 10, 15),  # the same agent resumed, its transcript unreadable this time
        )

        self.assertEqual(accepted_slots(ledger), {"W1-1"})

    def test_a_window_run_that_spans_two_slots_does_not_hide_the_slot_of_another_run(self) -> None:
        ledger = hunt_ledger(("W1-1", "W1-2", "W1-3"))
        for slot, seconds in (("W1-1", 1), ("W1-2", 2), ("W1-3", 12)):
            ledger, _ = cover(ledger, slot, seconds)
        ledger = with_agents(
            ledger, *run_events("w1", 0, 5, platform="copilot"), *run_events("w2", 10, 15, platform="copilot")
        )

        self.assertEqual(accepted_slots(ledger), {"W1-1", "W1-3"})

    def test_a_run_that_only_repeats_a_slot_does_not_unlock_the_slots_of_another(self) -> None:
        ledger = hunt_ledger(("W1-1", "W1-2", "W1-3"))
        for slot, seconds in (("W1-1", 1), ("W1-2", 2), ("W1-3", 3), ("W1-1", 12)):
            ledger, _ = cover(ledger, slot, seconds)
        ledger = with_agents(
            ledger, *run_events("w1", 0, 5, platform="copilot"), *run_events("w2", 10, 15, platform="copilot")
        )

        self.assertEqual(accepted_slots(ledger), {"W1-1", "W1-2"})

    def test_window_slots_are_matched_to_runs_by_plan_order(self) -> None:
        ledger = hunt_ledger(("W1-1", "W1-2", "W1-3"))
        for slot, seconds in (("W1-3", 1), ("W1-2", 2)):
            ledger, _ = cover(ledger, slot, seconds)
        ledger = with_agents(ledger, *run_events("w1", 0, 5, platform="copilot"))

        self.assertEqual(accepted_slots(ledger), {"W1-2"})

    def test_plan_order_is_not_slot_id_order(self) -> None:
        ledger = hunt_ledger(("W1-2", "W1-1"))
        for slot, seconds in (("W1-1", 1), ("W1-2", 2)):
            ledger, _ = cover(ledger, slot, seconds)
        ledger = with_agents(ledger, *run_events("w1", 0, 5, platform="copilot"))

        self.assertEqual(accepted_slots(ledger), {"W1-2"})

    def test_a_nudged_copilot_reviewer_is_one_run_with_one_slot_once_its_stops_pair(self) -> None:
        ledger = hunt_ledger()
        ledger, _ = cover(ledger, "W1-1", 3)
        ledger, _ = cover(ledger, "W1-2", 7)
        ledger = with_agents(
            ledger,
            agent_event("start", REVIEWER, "x", 1, platform="copilot"),
            agent_event("stop", REVIEWER, "x", 5, platform="copilot"),
            agent_event("stop", REVIEWER, "x", 9, platform="copilot"),
        )

        self.assertEqual(accepted_slots(ledger), {"W1-1"})

    def test_work_outside_the_slots_hunks_earns_nothing(self) -> None:
        ledger = hunt_ledger(("W1-1", "W1-2"))
        both = tuple(h.digest for h in ledger.rounds[0].hunks)
        record = Coverage(1, both, coverage_note(["H1", "H2"]), at(2), hunter="W1-1", by="antigravity:a")
        ledger = replace(ledger, coverage=(record,))
        ledger = with_agents(ledger, *run_events("a", 0, 5, platform=AGY))

        unfiltered = slot_work(ledger, agent_runs(ledger), order_of(ledger))
        owned = slot_work(
            ledger, agent_runs(ledger), order_of(ledger), slot_hunks=lambda n, s: frozenset({"H1"})
        )

        self.assertEqual(unfiltered[(1, "W1-1")].digests, frozenset(both))
        self.assertEqual(owned[(1, "W1-1")].digests, frozenset({ledger.rounds[0].hunks[0].digest}))

    def test_a_record_left_with_no_hunk_of_its_slot_adds_nothing(self) -> None:
        ledger = hunt_ledger(("W1-1", "W1-2"))
        other = ledger.rounds[0].hunks[1].digest
        ledger = replace(
            ledger,
            coverage=(Coverage(1, (other,), coverage_note(["H2"]), at(2), hunter="W1-1", by="antigravity:a"),),
        )
        ledger = with_agents(ledger, *run_events("a", 0, 5, platform=AGY))

        filtered = slot_work(
            ledger, agent_runs(ledger), order_of(ledger), slot_hunks=lambda n, s: frozenset({"H1"})
        )

        self.assertEqual(filtered, {})

    def test_a_slot_the_lookup_does_not_know_is_not_cut(self) -> None:
        ledger = hunt_ledger(("W1-1", "W1-2"))
        both = tuple(h.digest for h in ledger.rounds[0].hunks)
        ledger = replace(
            ledger,
            coverage=(Coverage(1, both, coverage_note(["H1", "H2"]), at(2), hunter="W1-1", by="antigravity:a"),),
        )
        ledger = with_agents(ledger, *run_events("a", 0, 5, platform=AGY))

        result = slot_work(ledger, agent_runs(ledger), order_of(ledger), slot_hunks=lambda n, s: None)

        self.assertEqual(result[(1, "W1-1")].digests, frozenset(both))

    def test_a_record_cut_to_nothing_does_not_spend_a_window_run(self) -> None:
        ledger = hunt_ledger(("W1-1", "W1-2"))
        stray = Coverage(
            1, (ledger.rounds[0].hunks[1].digest,), coverage_note(["H2"]), at(1), hunter="W1-1"
        )
        proper = Coverage(
            1, (ledger.rounds[0].hunks[1].digest,), coverage_note(["H2"]), at(2), hunter="W1-2"
        )
        ledger = replace(ledger, coverage=(stray, proper))
        ledger = with_agents(ledger, *run_events("w", 0, 5, platform="copilot"))

        result = slot_work(
            ledger,
            agent_runs(ledger),
            order_of(ledger),
            slot_hunks=lambda n, s: frozenset({"H1" if s == "W1-1" else "H2"}),
        )

        self.assertEqual(set(result), {(1, "W1-2")})

    def test_the_runs_of_a_slot_are_never_empty_for_a_platform_without_agent_ids(self) -> None:
        ledger = hunt_ledger(("W1-1",))
        ledger, _ = cover(ledger, "W1-1", 2)
        ledger = with_agents(ledger, *run_events("", 1, 5, platform="copilot"))

        self.assertEqual(work(ledger)[(1, "W1-1")].runs, ("run:0",))

    def test_work_of_a_later_round_is_kept_apart(self) -> None:
        first = hunt_ledger()
        second_round = replace(first.rounds[0], number=2, prepared_at=at(20))
        ledger = replace(first, rounds=(*first.rounds, second_round))
        ledger, b = claim(ledger, "W1-1", 21, number=2)
        ledger, c = cover(ledger, "W1-1", 22, number=2)
        ledger = with_agents(ledger, *run_events("a", 20, 25, records=(b, c)))

        self.assertEqual(set(work(ledger)), {(2, "W1-1")})


def two_hunk_slot_ledger() -> Ledger:
    """One slot, W1-1, over H1 and H2."""
    snap = make_snap(make_hunk("H1", "f1.py"), make_hunk("H2", "f2.py"))
    ledger, _ = prepared(snap)
    ledger = replace(ledger, rounds=(replace(ledger.rounds[0], prepared_at=T0),))
    return planned(ledger, make_slot("W1-1", hunks=("H1", "H2"), lenses=("intent",)))


def cover_hunks(ledger: Ledger, slot: str, hunk_ids: Sequence[str], seconds: float, *, by: str | None = None) -> tuple[Ledger, str]:
    """Coverage of just `hunk_ids`, tagged with `slot`; returns the new coverage token."""
    digests = tuple(h.digest for h in ledger.rounds[0].hunks if h.id in hunk_ids)
    record = Coverage(1, digests, coverage_note(hunk_ids), at(seconds), hunter=slot, by=by)
    return replace(ledger, coverage=(*ledger.coverage, record)), f"C{len(ledger.coverage) + 1}"


class RelaunchTests(unittest.TestCase):
    """A hunter that died with its slot half done can be replaced by one whose transcript is unreadable."""

    def half_done(self) -> Ledger:
        ledger = two_hunk_slot_ledger()
        ledger, _ = claim(ledger, "W1-1", 20)
        ledger, _ = cover_hunks(ledger, "W1-1", ("H1",), 30)
        return with_agents(ledger, *run_events("first", 10, 60, records=("B1", "C1")))

    def slot_digests(self, ledger: Ledger) -> frozenset[str]:
        return work(ledger)[(1, "W1-1")].digests

    def test_the_dead_hunter_alone_leaves_the_slot_half_done(self) -> None:
        ledger = self.half_done()

        self.assertEqual(len(self.slot_digests(ledger)), 1)

    def test_a_readable_relaunch_completes_the_slot(self) -> None:
        ledger = self.half_done()
        ledger, _ = claim(ledger, "W1-1", 120)
        ledger, _ = cover_hunks(ledger, "W1-1", ("H2",), 130)
        ledger = with_agents(ledger, *run_events("second", 100, 200, records=("B2", "C2")))

        self.assertEqual(len(self.slot_digests(ledger)), 2)

    def test_an_unreadable_relaunch_that_started_later_completes_the_slot(self) -> None:
        ledger = self.half_done()
        ledger, _ = cover_hunks(ledger, "W1-1", ("H2",), 130)
        ledger = with_agents(ledger, *run_events("second", 100, 200))

        self.assertEqual(len(self.slot_digests(ledger)), 2)
        self.assertEqual(work(ledger)[(1, "W1-1")].runs, ("first", "second"))

    def test_an_orchestrator_record_inside_a_concurrent_unread_run_adds_nothing(self) -> None:
        ledger = self.half_done()
        ledger, _ = cover_hunks(ledger, "W1-1", ("H2",), 130)  # the orchestrator's, in nobody's name
        ledger = with_agents(ledger, *run_events("other", 40, 300))  # started before the first hunter stopped

        self.assertEqual(len(self.slot_digests(ledger)), 1)

    def test_an_unreadable_run_that_started_exactly_when_the_hunter_stopped_adds_nothing(self) -> None:
        ledger = self.half_done()
        ledger, _ = cover_hunks(ledger, "W1-1", ("H2",), 130)
        ledger = with_agents(ledger, *run_events("other", 60, 300))

        self.assertEqual(len(self.slot_digests(ledger)), 1)

    def two_exact_holders(self) -> Ledger:
        """Two readable hunters of W1-1, both covering H1: the first ran 10..60, the second 50..100."""
        ledger = two_hunk_slot_ledger()
        ledger, _ = claim(ledger, "W1-1", 20)
        ledger, _ = cover_hunks(ledger, "W1-1", ("H1",), 30)
        ledger, _ = claim(ledger, "W1-1", 55)
        ledger, _ = cover_hunks(ledger, "W1-1", ("H1",), 70)
        ledger = with_agents(
            ledger, *run_events("first", 10, 60, records=("B1", "C1")), *run_events("second", 50, 100, records=("B2", "C2"))
        )
        ledger, _ = cover_hunks(ledger, "W1-1", ("H2",), 330)  # the orchestrator's, in nobody's name
        return ledger

    def test_an_unreadable_run_waits_for_every_exact_holder_to_stop(self) -> None:
        concurrent = with_agents(self.two_exact_holders(), *run_events("unread", 80, 400))  # the second still worked
        later = with_agents(self.two_exact_holders(), *run_events("unread", 110, 400))

        self.assertEqual(len(self.slot_digests(concurrent)), 1)
        self.assertEqual(len(self.slot_digests(later)), 2)

    def test_a_window_record_repeating_what_the_exact_hunter_covered_adds_nothing(self) -> None:
        ledger = self.half_done()
        ledger, _ = cover_hunks(ledger, "W1-1", ("H1",), 130)
        ledger = with_agents(ledger, *run_events("second", 100, 200))

        self.assertEqual(work(ledger)[(1, "W1-1")].runs, ("first",))

    def test_one_unreadable_run_cannot_finish_two_slots(self) -> None:
        snap = make_snap(make_hunk("H1", "f1.py"), make_hunk("H2", "f2.py"), make_hunk("H3", "f3.py"), make_hunk("H4", "f4.py"))
        ledger, _ = prepared(snap)
        ledger = replace(ledger, rounds=(replace(ledger.rounds[0], prepared_at=T0),))
        ledger = planned(
            ledger,
            make_slot("W1-1", hunks=("H1", "H2"), lenses=("intent",)),
            make_slot("W1-2", hunks=("H3", "H4"), lenses=("contracts",)),
        )
        ledger, _ = claim(ledger, "W1-1", 20)
        ledger, _ = cover_hunks(ledger, "W1-1", ("H1",), 30)
        ledger, _ = claim(ledger, "W1-2", 25)
        ledger, _ = cover_hunks(ledger, "W1-2", ("H3",), 35)
        ledger = with_agents(
            ledger, *run_events("one", 10, 60, records=("B1", "C1")), *run_events("two", 11, 61, records=("B2", "C2"))
        )
        ledger, _ = cover_hunks(ledger, "W1-1", ("H2",), 130)
        ledger, _ = cover_hunks(ledger, "W1-2", ("H4",), 131)
        ledger = with_agents(ledger, *run_events("late", 100, 200))

        result = work(ledger)

        self.assertEqual(sum(len(item.digests) for item in result.values()), 3)


class CreditedLensesTests(unittest.TestCase):
    def credited(self, ledger: Ledger, upto: int = 1) -> dict[str, frozenset[str]]:
        plans = {n: ledger.rounds[n - 1].plan for n in range(1, len(ledger.rounds) + 1)}

        def slot_lenses(number: int, slot: str) -> tuple[str, ...] | None:
            plan = plans.get(number)
            found = plan.slot(slot) if plan else None
            return found.lenses if found else None

        return credited_lenses(ledger, upto, slot_lenses, order_of(ledger))

    def test_an_accepted_slot_credits_its_lenses_to_its_hunks(self) -> None:
        ledger = hunt_ledger()
        ledger, b = claim(ledger, "W1-4", 1, by="antigravity:a")
        ledger, _ = cover(ledger, "W1-4", 2, by="antigravity:a")
        ledger = with_agents(ledger, *run_events("a", 0, 5, platform=AGY))

        self.assertEqual(self.credited(ledger), {"d-f4.py-+x = 1": frozenset(["ripple", "tests"])})

    def test_lenses_of_two_slots_add_up_on_a_shared_hunk(self) -> None:
        ledger = hunt_ledger(("W1-1", "W1-2"))
        plan = ledger.rounds[0].plan
        assert plan is not None
        ledger = planned(
            ledger, make_slot("W1-1", hunks=("H1", "H2"), lenses=("intent",)), make_slot("W1-2", hunks=("H1",), lenses=("contracts",))
        )
        ledger, _ = cover(ledger, "W1-1", 1, by="antigravity:a")
        ledger, _ = cover(ledger, "W1-2", 1, by="antigravity:b")
        ledger = with_agents(ledger, *run_events("a", 0, 5, platform=AGY), *run_events("b", 0, 5, platform=AGY))

        credit = self.credited(ledger)

        self.assertEqual(credit["d-f1.py-+x = 1"], frozenset(["intent", "contracts"]))
        self.assertEqual(credit["d-f2.py-+x = 1"], frozenset(["intent"]))

    def test_unvouched_coverage_credits_nothing(self) -> None:
        ledger = hunt_ledger()
        ledger, _ = cover(ledger, "W1-1", 2, by="antigravity:orchestrator")

        self.assertEqual(self.credited(ledger), {})

    def test_a_slot_the_lookup_does_not_know_credits_nothing(self) -> None:
        ledger = hunt_ledger()
        ledger, _ = cover(ledger, "W1-1", 2, by="antigravity:a")
        ledger = with_agents(ledger, *run_events("a", 0, 5, platform=AGY))

        self.assertEqual(credited_lenses(ledger, 1, lambda number, slot: None, order_of(ledger)), {})

    def test_coverage_from_before_a_reopening_does_not_count(self) -> None:
        first = hunt_ledger(("W1-1",))
        digest = first.rounds[0].hunks[0].digest
        second_round = replace(
            first.rounds[0],
            number=2,
            prepared_at=at(20),
            hunks=(HunkRef("H1", "f1.py", digest, 1, 1, carried=False, reopened=True),),
        )
        ledger = replace(first, rounds=(*first.rounds, second_round))
        ledger, _ = cover(ledger, "W1-1", 2, by="antigravity:a")
        ledger = with_agents(ledger, *run_events("a", 0, 5, platform=AGY))

        self.assertEqual(self.credited(ledger, upto=1), {digest: frozenset(["intent"])})
        self.assertEqual(self.credited(ledger, upto=2), {})

    def test_coverage_of_the_reopening_round_counts(self) -> None:
        first = hunt_ledger(("W1-1",))
        digest = first.rounds[0].hunks[0].digest
        second_round = replace(
            first.rounds[0],
            number=2,
            prepared_at=at(20),
            hunks=(HunkRef("H1", "f1.py", digest, 1, 1, carried=False, reopened=True),),
        )
        ledger = replace(first, rounds=(*first.rounds, second_round))
        ledger, _ = cover(ledger, "W1-1", 22, by="antigravity:a", number=2)
        ledger = with_agents(ledger, *run_events("a", 20, 25, platform=AGY))

        self.assertEqual(self.credited(ledger, upto=2), {digest: frozenset(["intent"])})

    def test_work_of_rounds_after_upto_is_ignored(self) -> None:
        first = hunt_ledger(("W1-1",))
        ledger = replace(first, rounds=(*first.rounds, replace(first.rounds[0], number=2, prepared_at=at(20))))
        ledger, _ = cover(ledger, "W1-1", 22, by="antigravity:a", number=2)
        ledger = with_agents(ledger, *run_events("a", 20, 25, platform=AGY))

        self.assertEqual(self.credited(ledger, upto=1), {})

    def test_untagged_coverage_of_a_round_without_a_plan_credits_every_lens(self) -> None:
        snap = make_snap(make_hunk("H1", "f1.py"))
        ledger, _ = prepared(snap)
        digest = ledger.rounds[0].hunks[0].digest
        ledger = replace(
            ledger, coverage=(Coverage(1, (digest,), coverage_note(["H1"]), at(1)),)
        )

        self.assertEqual(self.credited(ledger), {digest: frozenset(LENS_IDS)})

    def test_untagged_coverage_of_a_round_with_a_plan_credits_nothing(self) -> None:
        ledger = hunt_ledger(("W1-1",))
        ledger, _ = cover(ledger, None, 1)

        self.assertEqual(self.credited(ledger), {})

    def test_legacy_coverage_after_a_reopening_is_still_reopened_aware(self) -> None:
        snap = make_snap(make_hunk("H1", "f1.py"))
        first, _ = prepared(snap)
        digest = first.rounds[0].hunks[0].digest
        second_round = replace(
            first.rounds[0], number=2, hunks=(HunkRef("H1", "f1.py", digest, 1, 1, carried=False, reopened=True),)
        )
        ledger = replace(
            first,
            rounds=(*first.rounds, second_round),
            coverage=(Coverage(1, (digest,), coverage_note(["H1"]), at(1)),),
        )

        self.assertEqual(self.credited(ledger, upto=2), {})


class SlotHunkCreditTests(unittest.TestCase):
    def test_a_hunk_outside_the_slot_gets_no_lenses_from_it(self) -> None:
        ledger = hunt_ledger(("W1-1", "W1-2"))
        digests = {h.id: h.digest for h in ledger.rounds[0].hunks}
        record = Coverage(1, tuple(digests.values()), coverage_note(["H1", "H2"]), at(2), hunter="W1-1", by="antigravity:a")
        ledger = with_agents(replace(ledger, coverage=(record,)), *run_events("a", 0, 5, platform=AGY))

        def lenses(number: int, slot: str) -> tuple[str, ...] | None:
            return SLOT_LENSES.get(slot)

        smuggled = credited_lenses(ledger, 1, lenses, order_of(ledger))
        honest = credited_lenses(ledger, 1, lenses, order_of(ledger), slot_hunks=lambda n, s: frozenset({"H1"}))

        self.assertEqual(set(smuggled), {digests["H1"], digests["H2"]})
        self.assertEqual(honest, {digests["H1"]: frozenset({"intent"})})


def with_verdicts(ledger: Ledger, finding_id: str, *events: VerdictEvent) -> Ledger:
    """`finding_id` with exactly these verdict events (its current verdict is the last one's)."""
    findings = tuple(
        replace(
            f,
            verdict=events[-1].verdict,
            verdict_log=events,
            verdict_evidence=EVIDENCE,
            verdict_run="R1" if events[-1].verdict == "proven" else None,
        )
        if f.id == finding_id
        else f
        for f in ledger.findings
    )
    return replace(ledger, findings=findings)


def finding_ledger() -> Ledger:
    ledger = hunt_ledger(("W1-1",))
    ledger, _ = add_finding(ledger, **{**FINDING, "severity": "major"})
    return record_run(ledger, kind="repro", exit_code=1)  # R1, which a "proven" verdict cites


class VerdictVouchingTests(unittest.TestCase):
    def vouched(self, ledger: Ledger, index: int = 0, finding_id: str = "F1") -> bool:
        finding = next(f for f in ledger.findings if f.id == finding_id)
        return verdict_vouched(ledger, finding, finding.verdict_log[index], agent_runs(ledger))

    def test_a_verifier_transcript_vouches_for_its_token(self) -> None:
        ledger = with_verdicts(finding_ledger(), "F1", VerdictEvent("V1", "proven", at(3)))
        ledger = with_agents(ledger, *run_events("v", 0, 5, agent=VERIFIER, records=("V1",)))

        self.assertTrue(self.vouched(ledger))

    def test_an_orchestrator_verdict_outside_every_verifier_run_is_not_vouched(self) -> None:
        ledger = with_verdicts(finding_ledger(), "F1", VerdictEvent("V1", "proven", at(30)))
        ledger = with_agents(ledger, *run_events("v", 0, 5, agent=VERIFIER, records=("V1",)))

        self.assertFalse(self.vouched(ledger))

    def test_a_reviewer_run_is_not_a_verifier(self) -> None:
        ledger = with_verdicts(finding_ledger(), "F1", VerdictEvent("V1", "proven", at(3)))
        ledger = with_agents(ledger, *run_events("r", 0, 5, agent=REVIEWER, records=("V1",)))

        self.assertFalse(self.vouched(ledger))

    def test_identity_vouches_for_an_event_by_its_caller(self) -> None:
        ledger = with_verdicts(finding_ledger(), "F1", VerdictEvent("V1", "proven", at(3), "antigravity:v"))
        ledger = with_agents(ledger, *run_events("v", 0, 5, agent=VERIFIER, platform=AGY))

        self.assertTrue(self.vouched(ledger))

    def test_an_event_the_ledger_does_not_hold_is_never_vouched(self) -> None:
        ledger = with_verdicts(finding_ledger(), "F1", VerdictEvent("V1", "proven", at(3)))
        ledger = with_agents(ledger, *run_events("v", 0, 5, agent=VERIFIER))
        finding = ledger.findings[0]
        forged = VerdictEvent("V9", "proven", at(3))

        self.assertFalse(verdict_vouched(ledger, finding, forged, agent_runs(ledger)))

    def test_merge_events_follow_the_same_rule(self) -> None:
        ledger = finding_ledger()
        ledger, _ = add_finding(ledger, **{**FINDING, "severity": "major"})
        ledger = with_verdicts(ledger, "F2", VerdictEvent("V1", "dismissed", at(3)))
        ledger = replace(ledger, findings=(ledger.findings[0], replace(ledger.findings[1], merged_into="F1")))
        merged = ledger.findings[1]

        self.assertFalse(verdict_vouched(ledger, merged, merged.verdict_log[0], agent_runs(ledger)))
        ledger = with_agents(ledger, *run_events("v", 0, 5, agent=VERIFIER, records=("V1",)))
        self.assertTrue(verdict_vouched(ledger, merged, merged.verdict_log[0], agent_runs(ledger)))


class DistinctVerdictRunsTests(unittest.TestCase):
    def count(self, ledger: Ledger) -> int:
        return distinct_verdict_runs(ledger, ledger.findings[0], agent_runs(ledger))

    def test_one_event_vouched_by_two_overlapping_window_runs_counts_once(self) -> None:
        ledger = with_verdicts(finding_ledger(), "F1", VerdictEvent("V1", "dismissed", at(3)))
        ledger = with_agents(ledger, *run_events("a", 0, 5, agent=VERIFIER), *run_events("b", 1, 6, agent=VERIFIER))

        self.assertEqual(self.count(ledger), 1)

    def test_two_events_by_two_runs_count_twice(self) -> None:
        ledger = with_verdicts(
            finding_ledger(), "F1", VerdictEvent("V1", "dismissed", at(3)), VerdictEvent("V2", "dismissed", at(13))
        )
        ledger = with_agents(
            ledger,
            *run_events("a", 0, 5, agent=VERIFIER, records=("V1",)),
            *run_events("b", 10, 15, agent=VERIFIER, records=("V2",)),
        )

        self.assertEqual(self.count(ledger), 2)

    def test_two_events_by_one_run_count_once(self) -> None:
        ledger = with_verdicts(
            finding_ledger(), "F1", VerdictEvent("V1", "dismissed", at(3)), VerdictEvent("V2", "dismissed", at(4))
        )
        ledger = with_agents(ledger, *run_events("a", 0, 5, agent=VERIFIER, records=("V1", "V2")))

        self.assertEqual(self.count(ledger), 1)

    def test_a_run_is_not_wasted_on_an_event_an_exact_voucher_can_take(self) -> None:
        ledger = with_verdicts(
            finding_ledger(), "F1", VerdictEvent("V1", "dismissed", at(3)), VerdictEvent("V2", "dismissed", at(4))
        )
        ledger = with_agents(
            ledger,
            *run_events("window", 0, 6, agent=VERIFIER),
            *run_events("exact", 0, 6, agent=VERIFIER, records=("V1",)),
        )

        self.assertEqual(self.count(ledger), 2)

    def test_an_event_a_transcript_shows_is_not_also_credited_to_an_unread_run(self) -> None:
        ledger = with_verdicts(
            finding_ledger(), "F1", VerdictEvent("V1", "dismissed", at(3)), VerdictEvent("V2", "dismissed", at(4))
        )
        ledger = with_agents(
            ledger,
            *run_events("a", 0, 5, agent=VERIFIER, records=("V1", "V2")),
            *run_events("unread", 0, 5, agent=VERIFIER, platform="copilot"),
        )

        self.assertEqual(self.count(ledger), 1)

    def test_overlapping_windows_of_different_reach_still_count_both_runs(self) -> None:
        ledger = with_verdicts(
            finding_ledger(), "F1", VerdictEvent("V1", "dismissed", at(3)), VerdictEvent("V2", "dismissed", at(7))
        )
        ledger = with_agents(ledger, *run_events("wide", 0, 8, agent=VERIFIER), *run_events("narrow", 2, 4, agent=VERIFIER))

        self.assertEqual(self.count(ledger), 2)

    def test_a_late_event_does_not_starve_when_an_early_run_is_taken(self) -> None:
        ledger = with_verdicts(
            finding_ledger(), "F1", VerdictEvent("V1", "dismissed", at(10)), VerdictEvent("V2", "dismissed", at(50))
        )
        ledger = with_agents(
            ledger, *run_events("a", 1, 100, agent=VERIFIER), *run_events("b", 2, 20, agent=VERIFIER)
        )

        self.assertEqual(self.count(ledger), 2)

    def verifiers(self, ledger: Ledger, *ids: str) -> Ledger:
        for agent_id in ids:
            ledger = with_agents(ledger, *run_events(agent_id, 0, 50, agent=VERIFIER, platform=AGY))
        return ledger

    def test_a_verifier_who_judged_otherwise_is_not_agreement(self) -> None:
        ledger = with_verdicts(
            finding_ledger(),
            "F1",
            VerdictEvent("V1", "probable", at(2), "antigravity:v1"),
            VerdictEvent("V2", "dismissed", at(8), "antigravity:v2"),
        )
        ledger = self.verifiers(ledger, "v1", "v2")

        self.assertEqual(self.count(ledger), 1)

    def test_an_orchestrator_flip_after_two_confirming_verifiers_counts_nothing(self) -> None:
        ledger = with_verdicts(
            finding_ledger(),
            "F1",
            VerdictEvent("V1", "probable", at(2), "antigravity:v1"),
            VerdictEvent("V2", "probable", at(3), "antigravity:v2"),
            VerdictEvent("V3", "dismissed", at(60), "antigravity:orchestrator"),
        )
        ledger = self.verifiers(ledger, "v1", "v2")

        self.assertEqual(self.count(ledger), 0)

    def test_an_overturned_dismissal_is_not_reused_by_its_author(self) -> None:
        ledger = with_verdicts(
            finding_ledger(),
            "F1",
            VerdictEvent("V1", "dismissed", at(2), "antigravity:v1"),
            VerdictEvent("V2", "proven", at(3), "antigravity:v2"),
            VerdictEvent("V3", "dismissed", at(4), "antigravity:v1"),
        )
        ledger = self.verifiers(ledger, "v1", "v2")

        self.assertEqual(self.count(ledger), 1)

    def test_two_verifiers_agreeing_after_an_earlier_disagreement_count_twice(self) -> None:
        ledger = with_verdicts(
            finding_ledger(),
            "F1",
            VerdictEvent("V1", "probable", at(2), "antigravity:v1"),
            VerdictEvent("V2", "dismissed", at(3), "antigravity:v2"),
            VerdictEvent("V3", "dismissed", at(4), "antigravity:v3"),
        )
        ledger = self.verifiers(ledger, "v1", "v2", "v3")

        self.assertEqual(self.count(ledger), 2)

    def test_unvouched_events_count_nothing(self) -> None:
        ledger = with_verdicts(finding_ledger(), "F1", VerdictEvent("V1", "dismissed", at(30)))
        ledger = with_agents(ledger, *run_events("a", 0, 5, agent=VERIFIER))

        self.assertEqual(self.count(ledger), 0)

    def test_a_finding_without_events_has_none(self) -> None:
        self.assertEqual(self.count(finding_ledger()), 0)

    def test_a_finding_the_ledger_does_not_hold_has_none(self) -> None:
        ledger = finding_ledger()
        stranger = replace(ledger.findings[0], id="F9")

        self.assertEqual(distinct_verdict_runs(ledger, stranger, ()), 0)


class EvidenceCounterTests(unittest.TestCase):
    def counter(self, ledger: Ledger) -> int:
        return evidence_counter(ledger, agent_runs(ledger))

    def verified(self, *ids: str) -> Ledger:
        """A ledger with open verifier runs `ids` on Antigravity, so their verdicts (by identity) are vouched."""
        ledger = finding_ledger()
        for agent_id in ids:
            ledger = with_agents(ledger, *run_events(agent_id, 0, FOREVER, agent=VERIFIER, platform=AGY))
        return ledger

    def judge(self, ledger: Ledger, agent_id: str, verdict: Verdict = "dismissed") -> Ledger:
        run_id = "R1" if verdict == "proven" else None
        ledger, _ = set_verdict(ledger, "F1", verdict, EVIDENCE_OF[verdict], run_id, by=f"antigravity:{agent_id}")
        return ledger

    def test_a_fresh_round_counts_itself(self) -> None:
        self.assertEqual(self.counter(hunt_ledger()), 1)

    def test_vouched_coverage_counts_and_unvouched_does_not(self) -> None:
        ledger = hunt_ledger()
        ledger, c1 = cover(ledger, "W1-1", 2)
        ledger, _ = cover(ledger, "W1-2", 30)
        ledger = with_agents(ledger, *run_events("a", 0, 5, records=(c1,)))

        self.assertEqual(self.counter(ledger), 2)

    def test_an_orchestrator_cycling_verdicts_and_resolving_is_not_progress(self) -> None:
        ledger = finding_ledger()
        base = self.counter(ledger)
        for verdict in ("dismissed", "probable", "question", "proven"):
            ledger = self.judge(ledger, "orchestrator", verdict)
        ledger, _ = resolve(ledger, "F1", "fixed", "Guarded the empty list.", fingerprint="fp2")
        ledger = self.judge(ledger, "orchestrator", "proven")

        self.assertEqual(self.counter(ledger), base)

    def test_an_orchestrator_is_not_progress_even_beside_a_live_verifier(self) -> None:
        ledger = self.verified("v1")
        base = self.counter(ledger)
        ledger = self.judge(ledger, "orchestrator", "proven")

        self.assertEqual(self.counter(ledger), base)

    def test_a_verifier_repeating_one_verdict_counts_once(self) -> None:
        ledger = self.verified("v1")
        base = self.counter(ledger)
        for _ in range(5):
            ledger = self.judge(ledger, "v1")

        self.assertEqual(self.counter(ledger), base + 1)

    def test_a_second_verifier_agreeing_counts(self) -> None:
        ledger = self.verified("v1", "v2")
        base = self.counter(ledger)
        ledger = self.judge(ledger, "v1")
        ledger = self.judge(ledger, "v2")
        ledger = self.judge(ledger, "v2")

        self.assertEqual(self.counter(ledger), base + 2)

    def test_two_overlapping_window_verifiers_agreeing_count_twice(self) -> None:
        ledger = with_agents(
            finding_ledger(),
            *run_events("a", 0, 50, agent=VERIFIER, platform="copilot"),
            *run_events("b", 0, 50, agent=VERIFIER, platform="copilot"),
        )
        base = self.counter(ledger)
        ledger = with_verdicts(
            ledger, "F1", VerdictEvent("V1", "dismissed", at(3)), VerdictEvent("V2", "dismissed", at(4))
        )

        self.assertEqual(self.counter(ledger), base + 2)
        self.assertEqual(distinct_verdict_runs(ledger, ledger.findings[0], agent_runs(ledger)), 2)

    def test_one_window_verifier_judging_twice_counts_once(self) -> None:
        ledger = with_agents(finding_ledger(), *run_events("a", 0, 50, agent=VERIFIER, platform="copilot"))
        base = self.counter(ledger)
        ledger = with_verdicts(
            ledger, "F1", VerdictEvent("V1", "dismissed", at(3)), VerdictEvent("V2", "dismissed", at(4))
        )

        self.assertEqual(self.counter(ledger), base + 1)

    def test_an_identityless_verdict_beside_a_live_antigravity_verifier_is_not_progress(self) -> None:
        """`env -u ANTIGRAVITY_CONVERSATION_ID craftsman review verdict ...` cannot borrow the run."""
        ledger = self.verified("v1")
        base = self.counter(ledger)
        ledger, _ = set_verdict(ledger, "F1", "dismissed", EVIDENCE_OF["dismissed"], None, by=None)
        finding = ledger.findings[0]
        runs = agent_runs(ledger)

        self.assertFalse(verdict_vouched(ledger, finding, finding.verdict_log[0], runs))
        self.assertEqual(distinct_verdict_runs(ledger, finding, runs), 0)
        self.assertEqual(self.counter(ledger), base)

    def test_changing_a_verdict_is_progress(self) -> None:
        ledger = self.verified("v1")
        ledger = self.judge(ledger, "v1", "proven")
        once = self.counter(ledger)
        ledger = self.judge(ledger, "v1", "dismissed")

        self.assertEqual(self.counter(ledger), once + 1)

    def test_a_verifier_transcript_showing_two_verdicts_counts_both(self) -> None:
        ledger = with_verdicts(
            finding_ledger(), "F1", VerdictEvent("V1", "proven", at(3)), VerdictEvent("V2", "dismissed", at(4))
        )
        before = self.counter(ledger)
        ledger = with_agents(ledger, *run_events("v", 0, 5, agent=VERIFIER, records=("V1", "V2")))

        self.assertEqual(self.counter(ledger), before + 2)

    def test_a_resolution_counts_only_after_a_vouched_verdict(self) -> None:
        ledger = self.verified("v1")
        ledger = self.judge(ledger, "orchestrator", "proven")
        ledger, _ = resolve(ledger, "F1", "fixed", "Guarded the empty list.", fingerprint="fp2")
        unvouched = self.counter(ledger)
        ledger = self.judge(ledger, "v1", "proven")
        vouched_verdict = self.counter(ledger)

        self.assertEqual(vouched_verdict, unvouched + 2)  # the verdict, and the resolution it now backs

    def test_a_legacy_verdict_and_its_resolution_count(self) -> None:
        ledger = finding_ledger()
        base = self.counter(ledger)
        legacy = replace(ledger.findings[0], verdict="proven", verdict_evidence=EVIDENCE, verdict_run="R1")
        ledger = replace(ledger, findings=(legacy,))
        self.assertEqual(self.counter(ledger), base + 1)
        ledger, _ = resolve(ledger, "F1", "fixed", "Guarded the empty list.", fingerprint="fp2")

        self.assertEqual(self.counter(ledger), base + 2)

    def test_stop_events_and_claims_are_not_progress(self) -> None:
        before = hunt_ledger()
        after, _ = claim(before, "W1-1", 1)
        after = with_agents(after, *run_events("a", 0, 5), agent_event("stop", VERIFIER, "v", 6))

        self.assertEqual(self.counter(after), self.counter(before))

    def test_passing_runs_count_and_trivial_or_failing_ones_do_not(self) -> None:
        ledger = finding_ledger()
        base = self.counter(ledger)

        self.assertEqual(self.counter(record_run(ledger)), base + 1)
        self.assertEqual(self.counter(record_run(ledger, ("true",))), base)
        self.assertEqual(self.counter(record_run(ledger, exit_code=1)), base)

    def test_a_new_round_counts(self) -> None:
        ledger = hunt_ledger()
        later = replace(ledger, rounds=(*ledger.rounds, replace(ledger.rounds[0], number=2, prepared_at=at(40))))

        self.assertEqual(self.counter(later), self.counter(ledger) + 1)

    def test_it_can_drop_when_a_vouched_resolution_is_overturned(self) -> None:
        """The value is not monotone, and the docstring says so."""
        ledger = self.verified("v1")
        ledger = self.judge(ledger, "v1", "dismissed")
        ledger = self.judge(ledger, "v1", "proven")
        ledger, _ = resolve(ledger, "F1", "fixed", "Guarded the empty list.", fingerprint="fp2")
        resolved = self.counter(ledger)
        ledger = self.judge(ledger, "v1", "dismissed")  # nothing new to count; the resolution goes

        self.assertEqual(self.counter(ledger), resolved - 1)


class FindingSlotsTests(unittest.TestCase):
    """Which slot is bound to the run that recorded a finding (the generation of a finding rests on it)."""

    def ledger(self, *, by: str | None = None, seconds: float = 3) -> Ledger:
        ledger = hunt_ledger(("W1-1", "W1-2"))
        ledger, finding = add_finding(ledger, **{**FINDING, "severity": "major"}, by=by)
        return replace(ledger, findings=(replace(finding, recorded_at=at(seconds)),))

    def slots(self, ledger: Ledger) -> frozenset[str]:
        return finding_slots(ledger, ledger.findings[0], agent_runs(ledger), order_of(ledger))

    def test_the_slot_of_the_run_that_recorded_the_finding_by_identity(self) -> None:
        ledger = self.ledger(by="antigravity:a")
        ledger, _ = cover(ledger, "W1-2", 2, by="antigravity:a")
        ledger = with_agents(ledger, *run_events("a", 0, 5, platform=AGY))

        self.assertEqual(self.slots(ledger), {"W1-2"})

    def test_the_slot_of_the_run_whose_transcript_shows_the_finding(self) -> None:
        ledger = self.ledger()
        ledger, token = cover(ledger, "W1-1", 2)
        ledger = with_agents(ledger, *run_events("a", 0, 5, records=(token, "F1")))

        self.assertEqual(self.slots(ledger), {"W1-1"})

    def test_a_run_answers_for_one_slot_even_when_it_covered_two(self) -> None:
        ledger = self.ledger(by="antigravity:a")
        ledger, _ = cover(ledger, "W1-2", 2, by="antigravity:a")
        ledger, _ = cover(ledger, "W1-1", 3, by="antigravity:a")
        ledger = with_agents(ledger, *run_events("a", 0, 5, platform=AGY))

        self.assertEqual(self.slots(ledger), {"W1-1"})  # the first in plan order

    def test_a_claim_binds_before_any_coverage(self) -> None:
        ledger = self.ledger(by="antigravity:a")
        ledger, claim_token = claim(ledger, "W1-2", 1, by="antigravity:a")
        ledger, _ = cover(ledger, "W1-1", 2, by="antigravity:a")
        ledger = with_agents(ledger, *run_events("a", 0, 5, platform=AGY))

        self.assertEqual(claim_token, "B1")
        self.assertEqual(self.slots(ledger), {"W1-2"})

    def test_a_time_window_cannot_tell_which_slot_a_finding_belongs_to(self) -> None:
        ledger = self.ledger()
        ledger, _ = cover(ledger, "W1-1", 2)
        ledger = with_agents(ledger, *run_events("a", 0, 5))

        self.assertEqual(self.slots(ledger), frozenset())

    def test_the_orchestrators_finding_outside_every_run_has_no_slot(self) -> None:
        ledger = self.ledger(by="antigravity:a", seconds=30)
        ledger, _ = cover(ledger, "W1-1", 2, by="antigravity:a")
        ledger = with_agents(ledger, *run_events("a", 0, 5, platform=AGY))

        self.assertEqual(self.slots(ledger), frozenset())

    def test_a_verifier_run_does_not_count(self) -> None:
        ledger = self.ledger(by="antigravity:v")
        ledger = with_agents(ledger, *run_events("v", 0, 5, agent=VERIFIER, platform=AGY))

        self.assertEqual(self.slots(ledger), frozenset())

    def test_a_table_computed_once_gives_the_same_answer(self) -> None:
        ledger = self.ledger(by="antigravity:a")
        ledger, _ = cover(ledger, "W1-2", 2, by="antigravity:a")
        ledger = with_agents(ledger, *run_events("a", 0, 5, platform=AGY))
        runs = agent_runs(ledger)
        table = bound_slots(ledger, runs, order_of(ledger))

        self.assertEqual(finding_slots(ledger, ledger.findings[0], runs, order_of(ledger), bound=table), {"W1-2"})
        self.assertEqual(sorted(table.values()), ["W1-2"])


if __name__ == "__main__":
    unittest.main()
