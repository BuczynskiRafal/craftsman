"""The hunt planner: depth, required and owed lenses, slicing, slots, waves and carry-over."""

from __future__ import annotations

import random
import unittest
from collections.abc import Sequence
from dataclasses import replace

from craftsman.attribution import agent_runs, bound_slots, slot_work
from craftsman.diffscan import Hunk, is_config_path
from craftsman.gatecheck import slot_statuses
from craftsman.huntplan import (
    BUG_LENSES,
    DEEP_MIN_PRODUCTION_LINES,
    DELTA_MAX_CHANGED_LINES,
    MAX_VARIANTS_PER_WAVE,
    SLICE_MAX_CHANGED_LINES,
    Depth,
    build_plan,
    changed_lines,
    credited,
    decide_depth,
    finding_generation,
    hunk_kind,
    is_production_hunk,
    is_trigger,
    materialize,
    owed_lenses,
    pending_wave_slots,
    required_by_hunk,
    required_lenses,
    required_slots,
    seal_round,
    signals_by_hunk,
    slice_hunks,
    slot_complete,
    slot_hunks,
    slot_lenses,
    slot_order,
    sweep_pending,
)
from craftsman.ledger import add_agent_event, add_finding, open_round, round_unchanged, stable_hunk_ids
from craftsman.records import (
    Coverage,
    Finding,
    HunkRef,
    HuntPlan,
    Ledger,
    LedgerError,
    Round,
    Severity,
    Slot,
    SlotClaim,
    Verdict,
    VerdictEvent,
    parse_timestamp,
)

from tests.support import (
    FINDING,
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

CORE = ("intent", "contracts", "failure", "ripple", "tests")
SIMPLICITY = ("craft", "design")
SQL_LINE = '+cursor.execute("DELETE FROM sessions WHERE expired = 1")'
FS_LINE = '+with open("config.toml", "rb") as handle:'
MODEL_LINE = "+type Config struct {"
STATIC_EVIDENCE = "static: app.py:3 divides by len(items), which raises ZeroDivisionError on []"
REVIEWER = "craftsman-reviewer"
AGY = "antigravity"


def lines(count: int, tag: str = "") -> tuple[str, ...]:
    return tuple(f"+value{tag}{index} = {index}" for index in range(count))


def code(hunk_id: str, path: str = "app.py", count: int = 5, extra: Sequence[str] = ()) -> Hunk:
    """A production hunk of `count` plain added lines (plus `extra`), distinct per hunk id."""
    return make_hunk(hunk_id, path, (*lines(count, hunk_id), *extra))


def plan_of(
    hunks: Sequence[Hunk],
    *,
    deep: bool = False,
    round_number: int = 1,
    existing: HuntPlan | None = None,
    first_deep: bool = False,
    have: dict[str, frozenset[str]] | None = None,
    **extra: object,
) -> HuntPlan:
    """The plan for `hunks` (all open) as the CLI would build it, with `have` credited per digest."""
    required = required_by_hunk(hunks, deep=deep)
    owed = owed_lenses(required, hunks, have or {})
    return build_plan(
        hunks,
        owed,
        depth=Depth(deep, ("requested with --deep",) if deep else ()),
        round_number=round_number,
        first_deep_round=first_deep,
        existing=existing,
        **extra,  # type: ignore[arg-type]
    )


def shape(plan: HuntPlan) -> list[tuple[str, tuple[str, ...], tuple[str, ...]]]:
    return [(slot.id, slot.lenses, slot.hunks) for slot in plan.slots]


class ClassificationTests(unittest.TestCase):
    def test_changed_lines_counts_added_and_removed_lines_only(self) -> None:
        hunk = make_hunk("H1", "a.py", ("+a", "-b", " context", "\\ No newline at end of file", "+c"))

        self.assertEqual(changed_lines(hunk), 3)

    def test_kinds_follow_the_path(self) -> None:
        expected = {
            "app.py": "production",
            "README.md": "docs",
            "tests/test_app.py": "test",
            "pyproject.toml": "config",
            "uv.lock": "config",
            "package-lock.json": "config",
            "go.sum": "config",
            "static/app.min.js": "config",
            "static/app.js.map": "config",
            "assets/logo.svg": "config",
        }
        for path, kind in expected.items():
            with self.subTest(path=path):
                self.assertEqual(hunk_kind(code("H1", path)), kind)

    def test_an_agent_prompt_is_production_code(self) -> None:
        hunk = code("H1", "skills/craft-review/SKILL.md")

        self.assertEqual(hunk_kind(hunk), "production")
        self.assertTrue(is_production_hunk(hunk))

    def test_a_rename_of_prose_into_code_is_not_docs(self) -> None:
        hunk = replace(code("H1", "app.py"), old_path="notes.md", status="renamed")

        self.assertEqual(hunk_kind(hunk), "production")


class DepthTests(unittest.TestCase):
    def test_a_quiet_small_change_is_normal(self) -> None:
        depth = decide_depth(make_snap(code("H1")), floor=False)

        self.assertEqual(depth, Depth(False, ()))

    def test_the_floor_makes_it_deep_and_gives_its_reason(self) -> None:
        depth = decide_depth(make_snap(code("H1")), floor=True, floor_reason="requested with --deep")

        self.assertEqual(depth, Depth(True, ("requested with --deep",)))

    def test_a_floor_without_a_reason_still_says_something(self) -> None:
        depth = decide_depth(make_snap(code("H1")), floor=True)

        self.assertEqual(depth, Depth(True, ("requested",)))

    def test_a_deep_signal_in_production_code_names_the_hunks(self) -> None:
        snap = make_snap(code("H1"), code("H3", "db.py", extra=(SQL_LINE,)), code("H5", "q.py", extra=(SQL_LINE,)))

        depth = decide_depth(snap, floor=False)

        self.assertEqual(depth, Depth(True, ("sql signal in H3, H5",)))

    def test_a_deep_signal_in_a_test_does_not_count(self) -> None:
        snap = make_snap(code("H1", "tests/test_db.py", extra=(SQL_LINE,)))

        self.assertFalse(decide_depth(snap, floor=False).deep)

    def test_250_production_lines_are_normal_and_251_are_deep(self) -> None:
        self.assertEqual(DEEP_MIN_PRODUCTION_LINES, 251)
        normal = decide_depth(make_snap(code("H1", count=250)), floor=False)
        deep = decide_depth(make_snap(code("H1", count=251)), floor=False)

        self.assertFalse(normal.deep)
        self.assertEqual(deep, Depth(True, ("251 changed production lines > 250",)))

    def test_added_and_deleted_lines_both_count_and_files_add_up(self) -> None:
        first = make_hunk("H1", "a.py", (*lines(130, "a"),))
        second = make_hunk("H2", "b.py", tuple(f"-old{index} = 1" for index in range(130)))

        depth = decide_depth(make_snap(first, second), floor=False)

        self.assertEqual(depth.reasons, ("260 changed production lines > 250",))

    def test_tests_docs_config_and_lockfiles_are_not_counted(self) -> None:
        snap = make_snap(
            code("H1", "tests/test_app.py", count=300),
            code("H2", "README.md", count=300),
            code("H3", "uv.lock", count=300),
            code("H4", "pyproject.toml", count=300),
            code("H5", "app.py", count=10),
        )

        self.assertEqual(decide_depth(snap, floor=False), Depth(False, ()))

    def test_every_reason_is_listed_and_the_floor_comes_first(self) -> None:
        snap = make_snap(code("H1", count=300, extra=(SQL_LINE,)))

        depth = decide_depth(snap, floor=True, floor_reason="an earlier round was deep")

        self.assertEqual(
            depth.reasons,
            ("an earlier round was deep", "sql signal in H1", "301 changed production lines > 250"),
        )


class RequiredLensesTests(unittest.TestCase):
    def required(self, hunk: Hunk, *, deep: bool = False) -> frozenset[str]:
        return required_by_hunk([hunk], deep=deep)[hunk.id]

    def test_production_code_without_signals_gets_core_and_simplicity(self) -> None:
        self.assertEqual(self.required(code("H1")), frozenset((*CORE, *SIMPLICITY)))

    def test_a_signal_adds_the_conditional_lenses_it_triggers_in_that_hunk(self) -> None:
        got = self.required(code("H1", extra=(FS_LINE,)))

        self.assertEqual(got, frozenset((*CORE, *SIMPLICITY, "state", "trust", "platform")))

    def test_a_data_model_adds_the_model_lens_only(self) -> None:
        got = self.required(code("H1", extra=(MODEL_LINE,)))

        self.assertEqual(got, frozenset((*CORE, *SIMPLICITY, "model")))

    def test_conditional_lenses_depend_on_the_hunk_not_the_diff(self) -> None:
        risky = code("H1", extra=(FS_LINE,))
        plain = code("H2", "b.py")

        required = required_by_hunk([risky, plain], deep=False)

        self.assertIn("state", required["H1"])
        self.assertNotIn("state", required["H2"])

    def test_a_test_hunk_with_a_filesystem_signal_gets_no_conditional_lens(self) -> None:
        got = self.required(code("H1", "tests/test_io.py", extra=(FS_LINE,)))

        self.assertEqual(got, frozenset((*CORE, *SIMPLICITY)))

    def test_config_gets_core_and_simplicity_only_even_when_deep(self) -> None:
        got = self.required(code("H1", "pyproject.toml"), deep=True)

        self.assertEqual(got, frozenset((*CORE, *SIMPLICITY)))

    def test_deep_production_code_gets_every_bug_lens(self) -> None:
        got = self.required(code("H1"), deep=True)

        self.assertEqual(got, frozenset((*BUG_LENSES, *SIMPLICITY)))
        self.assertEqual(len(BUG_LENSES), 9)

    def test_docs_get_intent_and_craft_whatever_the_depth(self) -> None:
        for deep in (False, True):
            with self.subTest(deep=deep):
                self.assertEqual(self.required(code("H1", "README.md"), deep=deep), frozenset({"intent", "craft"}))

    def test_signals_by_hunk_lists_every_hunk(self) -> None:
        signals = signals_by_hunk([code("H1"), code("H2", "b.py", extra=(FS_LINE,))])

        self.assertEqual(signals["H1"], frozenset())
        self.assertIn("filesystem", signals["H2"])

    def test_required_lenses_takes_the_signals_of_the_hunk(self) -> None:
        got = required_lenses(code("H1"), deep=False, signals=frozenset({"datamodel"}))

        self.assertIn("model", got)


class OwedLensesTests(unittest.TestCase):
    def test_owed_is_required_minus_credited_by_digest(self) -> None:
        hunk = code("H1")
        required = {"H1": frozenset({"intent", "tests", "craft"})}

        owed = owed_lenses(required, [hunk], {hunk.digest: frozenset({"intent", "ripple"})})

        self.assertEqual(owed, {"H1": frozenset({"tests", "craft"})})

    def test_a_hunk_that_owes_nothing_is_left_out(self) -> None:
        hunk = code("H1")

        owed = owed_lenses({"H1": frozenset({"intent"})}, [hunk], {hunk.digest: frozenset({"intent"})})

        self.assertEqual(owed, {})

    def test_a_carried_ref_owes_nothing_and_a_reopened_ref_gets_no_credit(self) -> None:
        carried = HunkRef("H1", "a.py", "d1", 1, 1, carried=True)
        reopened = HunkRef("H2", "b.py", "d2", 1, 1, carried=False, reopened=True)
        fresh = HunkRef("H3", "c.py", "d3", 1, 1, carried=False)
        required = {ref: frozenset({"intent", "tests"}) for ref in ("H1", "H2", "H3")}
        have = {"d1": frozenset(), "d2": frozenset({"intent", "tests"}), "d3": frozenset({"intent"})}

        owed = owed_lenses(required, [carried, reopened, fresh], have)

        self.assertEqual(owed, {"H2": frozenset({"intent", "tests"}), "H3": frozenset({"tests"})})


class SlicingTests(unittest.TestCase):
    def test_a_small_change_is_one_slice(self) -> None:
        self.assertEqual(slice_hunks([code("H1", "a.py"), code("H2", "b.py")]), (("H1", "H2"),))

    def test_whole_files_go_first_and_a_slice_holds_at_most_the_budget(self) -> None:
        self.assertEqual(SLICE_MAX_CHANGED_LINES, 400)
        hunks = [code("H1", "a.py", 300), code("H2", "b.py", 200), code("H3", "c.py", 50)]

        self.assertEqual(slice_hunks(hunks), (("H1",), ("H2", "H3")))

    def test_a_file_over_budget_is_split_between_its_hunks(self) -> None:
        hunks = [code("H1", "big.py", 150), code("H2", "big.py", 150), code("H3", "big.py", 150)]

        self.assertEqual(slice_hunks(hunks), (("H1", "H2"), ("H3",)))

    def test_a_split_file_fills_the_room_left_in_the_current_slice(self) -> None:
        hunks = [code("H1", "a.py", 100), code("H2", "big.py", 150), code("H3", "big.py", 150), code("H4", "big.py", 150)]

        self.assertEqual(slice_hunks(hunks), (("H1", "H2", "H3"), ("H4",)))

    def test_one_oversized_hunk_is_its_own_slice(self) -> None:
        hunks = [code("H1", "a.py", 100), code("H2", "big.py", 500), code("H3", "c.py", 100)]

        self.assertEqual(slice_hunks(hunks), (("H1",), ("H2",), ("H3",)))

    def test_a_hunk_without_changed_lines_joins_a_slice(self) -> None:
        hunks = [code("H1", "a.py", 300), make_hunk("H2", "mode.sh", ()), code("H3", "c.py", 200)]

        self.assertEqual(slice_hunks(hunks), (("H1", "H2"), ("H3",)))

    def test_reverse_packs_from_the_other_end_but_lists_hunks_in_order(self) -> None:
        hunks = [code("H1", "a.py", 300), code("H2", "b.py", 200)]

        self.assertEqual(slice_hunks(hunks, reverse=True), (("H2",), ("H1",)))

    def test_slicing_is_deterministic(self) -> None:
        hunks = [code(f"H{n}", f"f{n % 3}.py", 120) for n in range(1, 8)]

        self.assertEqual(slice_hunks(hunks), slice_hunks(list(hunks)))


class GenerationOnePlanTests(unittest.TestCase):
    def test_a_tiny_change_is_three_slots_with_the_sweep_on_the_second(self) -> None:
        plan = plan_of([code("H1")])

        self.assertEqual(
            shape(plan),
            [
                ("W1-1", ("intent", "ripple", "tests"), ("H1",)),
                ("W1-2", ("contracts", "failure", "sweep"), ("H1",)),
                ("W1-3", ("craft", "design"), ("H1",)),
            ],
        )
        self.assertEqual([slot.kind for slot in plan.slots], ["bug", "bug", "simplicity"])
        self.assertTrue(all(slot.generation == 1 and slot.trigger is None for slot in plan.slots))

    def test_changed_lines_of_a_slot_are_the_sum_over_its_hunks(self) -> None:
        plan = plan_of([code("H1", count=7), code("H2", "b.py", count=3)])

        self.assertEqual([slot.changed_lines for slot in plan.slots], [10, 10, 10])

    def test_a_risky_hunk_adds_conditional_slots_covering_only_that_hunk(self) -> None:
        plan = plan_of([code("H1"), code("H2", "b.py", extra=(FS_LINE,))])

        self.assertEqual(
            shape(plan),
            [
                ("W1-1", ("intent", "ripple", "tests"), ("H1", "H2")),
                ("W1-2", ("contracts", "failure", "sweep"), ("H1", "H2")),
                ("W1-3", ("state", "platform"), ("H2",)),
                ("W1-4", ("trust",), ("H2",)),
                ("W1-5", ("craft", "design"), ("H1", "H2")),
            ],
        )

    def test_a_deep_change_has_every_group_a_sweep_host_and_a_fresh_set(self) -> None:
        plan = plan_of([code("H1")], deep=True, first_deep=True)

        self.assertEqual(
            shape(plan),
            [
                ("W1-1", ("intent", "ripple", "tests"), ("H1",)),
                ("W1-2", ("contracts", "failure", "sweep"), ("H1",)),
                ("W1-3", ("state", "platform"), ("H1",)),
                ("W1-4", ("trust", "model"), ("H1",)),
                ("W1-5", ("craft", "design"), ("H1",)),
                ("W2-X1", ("intent", "contracts", "failure"), ("H1",)),
            ],
        )
        fresh = plan.slots[-1]
        self.assertEqual((fresh.kind, fresh.generation, fresh.trigger, fresh.since), ("fresh", 2, "deep", None))
        self.assertEqual(plan.reasons, ("requested with --deep",))

    def test_a_deep_plan_without_the_first_deep_round_has_no_fresh_set(self) -> None:
        plan = plan_of([code("H1")], deep=True, first_deep=False)

        self.assertFalse(any(slot.kind == "fresh" for slot in plan.slots))

    def test_several_slices_get_their_own_slots_and_one_separate_sweep(self) -> None:
        hunks = [code("H1", "a.py", 300), code("H2", "b.py", 300), code("H3", "c.py", 300)]

        plan = plan_of(hunks)

        self.assertEqual(len(plan.slots), 10)
        self.assertEqual(
            shape(plan)[:3],
            [
                ("W1-1", ("intent", "ripple", "tests"), ("H1",)),
                ("W1-2", ("contracts", "failure"), ("H1",)),
                ("W1-3", ("craft", "design"), ("H1",)),
            ],
        )
        self.assertEqual(shape(plan)[3][2], ("H2",))
        sweep = plan.slots[-1]
        self.assertEqual((sweep.id, sweep.kind, sweep.lenses, sweep.hunks), ("W1-10", "sweep", ("sweep",), ("H1", "H2", "H3")))
        self.assertEqual(sum("sweep" in slot.lenses for slot in plan.slots), 1)

    def test_a_docs_only_change_is_one_slot_and_nothing_else(self) -> None:
        plan = plan_of([code("H1", "README.md"), code("H2", "docs/guide.md")])

        self.assertEqual(shape(plan), [("W1-1", ("intent", "craft"), ("H1", "H2"))])
        self.assertEqual(plan.slots[0].kind, "bug")

    def test_docs_in_a_mixed_slice_ride_with_the_code_slots(self) -> None:
        plan = plan_of([code("H1"), code("H2", "README.md")])

        by_id = {slot.id: slot for slot in plan.slots}
        self.assertEqual(by_id["W1-1"].hunks, ("H1", "H2"))  # the docs hunk owes intent
        self.assertEqual(by_id["W1-2"].hunks, ("H1", "H2"))  # owes neither contracts nor failure, but the sweep reads it
        self.assertEqual(by_id["W1-3"].hunks, ("H1", "H2"))  # and owes craft

    def test_a_single_group_plan_gets_a_separate_sweep_when_two_bug_slots_exist(self) -> None:
        # intent, ripple, tests owed on H1 (group 1) and only `state` on H2: two bug slots, no core host.
        hunks = [code("H1"), code("H2", "b.py")]
        required = {"H1": frozenset({"intent", "ripple", "tests"}), "H2": frozenset({"state"})}
        owed = owed_lenses(required, hunks, {})

        plan = build_plan(
            hunks, owed, depth=Depth(False, ()), round_number=1, first_deep_round=False, existing=None
        )

        self.assertEqual(shape(plan)[-1], ("W1-3", ("sweep",), ("H1", "H2")))
        self.assertEqual(plan.slots[-1].kind, "sweep")

    def test_planning_is_deterministic(self) -> None:
        hunks = [code("H1"), code("H2", "b.py", extra=(FS_LINE,))]

        self.assertEqual(plan_of(hunks), plan_of(list(hunks)))

    def test_a_hunk_that_owes_nothing_takes_no_part(self) -> None:
        hunks = [code("H1"), code("H2", "b.py")]
        have = {hunks[0].digest: frozenset((*CORE, *SIMPLICITY))}

        plan = plan_of(hunks, have=have)

        self.assertEqual({hunk for slot in plan.slots for hunk in slot.hunks}, {"H2"})

    def test_nothing_owed_is_an_empty_plan(self) -> None:
        hunk = code("H1")

        plan = plan_of([hunk], have={hunk.digest: frozenset((*CORE, *SIMPLICITY))})

        self.assertEqual(plan.slots, ())


class DeltaRoundTests(unittest.TestCase):
    def test_a_small_later_round_is_one_bug_slot_with_every_owed_lens_and_a_simplicity_slot(self) -> None:
        plan = plan_of([code("H1"), code("H2", "b.py", extra=(MODEL_LINE,))], round_number=2)

        self.assertEqual(
            shape(plan),
            [
                ("W1-1", ("intent", "contracts", "failure", "ripple", "tests", "model"), ("H1", "H2")),
                ("W1-2", ("craft", "design"), ("H1", "H2")),
            ],
        )
        self.assertFalse(any("sweep" in slot.lenses for slot in plan.slots))

    def test_a_delta_round_has_no_simplicity_slot_when_simplicity_is_credited(self) -> None:
        hunk = code("H1")
        have = {hunk.digest: frozenset(SIMPLICITY)}

        plan = plan_of([hunk], round_number=2, have=have)

        self.assertEqual(shape(plan), [("W1-1", CORE, ("H1",))])
        self.assertEqual(plan.slots[0].kind, "bug")

    def test_a_delta_round_that_owes_only_simplicity_has_no_bug_slot(self) -> None:
        hunk = code("H1")
        have = {hunk.digest: frozenset(CORE)}

        plan = plan_of([hunk], round_number=2, have=have)

        self.assertEqual(shape(plan), [("W1-1", SIMPLICITY, ("H1",))])
        self.assertEqual(plan.slots[0].kind, "simplicity")

    def test_the_bug_slot_covers_only_hunks_that_owe_a_bug_lens(self) -> None:
        owes_all, owes_style = code("H1"), code("H2", "b.py")
        have = {owes_style.digest: frozenset(CORE)}

        plan = plan_of([owes_all, owes_style], round_number=2, have=have)

        self.assertEqual(shape(plan), [("W1-1", CORE, ("H1",)), ("W1-2", SIMPLICITY, ("H1", "H2"))])

    def test_the_first_round_is_never_a_delta_round(self) -> None:
        self.assertGreater(len(plan_of([code("H1")], round_number=1).slots), 2)

    def test_more_than_the_delta_budget_is_a_normal_round(self) -> None:
        self.assertEqual(DELTA_MAX_CHANGED_LINES, 40)
        small = plan_of([code("H1", count=40)], round_number=2)
        large = plan_of([code("H1", count=41)], round_number=2)

        self.assertEqual(len(small.slots), 2)
        self.assertEqual(len(large.slots), 3)

    def test_a_deep_signal_in_the_open_hunks_makes_it_a_normal_round(self) -> None:
        plan = plan_of([code("H1", count=3, extra=(SQL_LINE,))], round_number=2)

        self.assertGreater(len(plan.slots), 2)

    def test_a_deep_floor_alone_does_not_end_the_delta_round(self) -> None:
        plan = plan_of([code("H1")], round_number=2, deep=True)

        self.assertEqual(len(plan.slots), 2)
        self.assertEqual(plan.slots[0].lenses, BUG_LENSES)


class AppendOnlyTests(unittest.TestCase):
    def test_preparing_again_changes_nothing(self) -> None:
        hunks = [code("H1"), code("H2", "b.py", extra=(FS_LINE,))]
        first = plan_of(hunks)

        again = plan_of(hunks, existing=first)

        self.assertEqual(again, first)

    def test_raising_the_depth_adds_the_missing_groups_and_one_fresh_set(self) -> None:
        hunks = [code("H1")]
        first = plan_of(hunks)

        deep = plan_of(hunks, deep=True, first_deep=True, existing=first)

        self.assertEqual(deep.slots[: len(first.slots)], first.slots)
        self.assertEqual(
            shape(deep)[len(first.slots) :],
            [
                ("W1-4", ("state", "platform"), ("H1",)),
                ("W1-5", ("trust", "model"), ("H1",)),
                ("W2-X1", ("intent", "contracts", "failure"), ("H1",)),
            ],
        )
        self.assertEqual(plan_of(hunks, deep=True, first_deep=True, existing=deep), deep)
        self.assertEqual(deep.reasons, ("requested with --deep",))

    def test_a_pair_one_slot_assigns_is_not_assigned_again(self) -> None:
        hunks = [code("H1"), code("H2", "b.py", extra=(FS_LINE,))]
        deep = plan_of(hunks, deep=True, existing=plan_of(hunks))

        pairs = [
            (hunk, lens)
            for slot in deep.slots
            if slot.generation == 1
            for hunk in slot.hunks
            for lens in slot.lenses
            if lens != "sweep"
        ]
        # A slot is a rectangle of hunks and lenses, so a pair can repeat when its neighbours are new
        # ([trust, model] over H1 and H2 repeats H2's trust); a uniform hunk gets each lens exactly once.
        first = [lens for hunk, lens in pairs if hunk == "H1"]
        self.assertEqual(sorted(first), sorted((*BUG_LENSES, *SIMPLICITY)))
        self.assertEqual([lens for hunk, lens in pairs if hunk == "H2" and lens in ("state", "platform")], ["state", "platform"])

    def test_an_existing_sweep_is_not_added_again(self) -> None:
        hunks = [code("H1")]
        deep = plan_of(hunks, deep=True, existing=plan_of(hunks))

        self.assertEqual(sum("sweep" in slot.lenses for slot in deep.slots), 1)

    def test_a_first_plan_for_a_legacy_round_numbers_from_one(self) -> None:
        self.assertEqual(plan_of([code("H1")], existing=None).slots[0].id, "W1-1")

    def test_new_hunks_get_slots_that_continue_the_numbering(self) -> None:
        first = plan_of([code("H1")])
        both = [code("H1"), code("H2", "b.py")]
        owed = {"H2": required_by_hunk(both, deep=False)["H2"]}

        plan = build_plan(both, owed, depth=Depth(False, ()), round_number=1, first_deep_round=False, existing=first)

        self.assertEqual(plan.slots[: len(first.slots)], first.slots)
        self.assertEqual([slot.id for slot in plan.slots[len(first.slots) :]], ["W1-4", "W1-5", "W1-6"])
        self.assertTrue(all(slot.hunks == ("H2",) for slot in plan.slots[len(first.slots) :]))


# --- ledgers with a plan --------------------------------------------------------------------


def record_finding(
    ledger: Ledger,
    *,
    lens: str = "intent",
    path: str = "app.py",
    severity: Severity = "major",
    verdict: Verdict | None = "proven",
    hunter: str | None = None,
    by: str | None = None,
    seconds: float = 5,
    number: int | None = None,
) -> Ledger:
    """Append a finding of the latest round, recorded and judged at `seconds` after T0."""
    ledger, finding = add_finding(
        ledger, **{**FINDING, "lens": lens, "path": path, "severity": severity}, hunter=hunter, by=by
    )
    updated = replace(finding, recorded_at=at(seconds), updated_at=at(seconds))
    if verdict is not None:
        token = f"V{1 + sum(len(item.verdict_log) for item in ledger.findings)}"
        updated = replace(
            updated,
            verdict=verdict,
            verdict_evidence=STATIC_EVIDENCE if verdict != "proven" else "reproduced by tests/test_app.py::test_empty",
            verdict_run="R1" if verdict == "proven" else None,
            verdict_log=(VerdictEvent(token, verdict, at(seconds + 1)),),
        )
    findings = tuple(updated if item.id == finding.id else item for item in ledger.findings)
    return replace(ledger, findings=findings)


def hunt(hunks: Sequence[Hunk], *, deep: bool = False, first_deep: bool = False) -> Ledger:
    """A ledger whose round 1 is prepared for `hunks` with its hunt plan attached."""
    snap = make_snap(*hunks)
    ledger, round_ = prepared(snap, deep)
    ledger = record_run(ledger, kind="repro", exit_code=1)  # R1, which a "proven" verdict cites
    ledger = replace(ledger, rounds=(replace(round_, prepared_at=at(0), enforced=True),))  # vouched hunters
    plan = plan_of(hunks, deep=deep, first_deep=first_deep)
    return planned(ledger, *plan.slots, reasons=plan.reasons)


def run_slot(
    ledger: Ledger, slot_id: str, agent: str, seconds: float, *, number: int = 1, only: Sequence[str] | None = None
) -> Ledger:
    """A finished reviewer run `agent` that covered `slot_id` of round `number` (vouched by identity)."""
    round_ = ledger.rounds[number - 1]
    assert round_.plan is not None
    slot = next((item for item in required_slots(ledger, round_) if item.id == slot_id), None)
    ids = tuple(only if only is not None else (slot.hunks if slot is not None else ()))
    digests = tuple(ref.digest for ref in round_.hunks if ref.id in ids)
    record = Coverage(number, digests, coverage_note(ids), at(seconds), hunter=slot_id, by=f"{AGY}:{agent}")
    ledger = add_agent_event(ledger, agent_event("start", REVIEWER, agent, seconds - 1, platform=AGY))
    ledger = add_agent_event(ledger, agent_event("stop", REVIEWER, agent, seconds + 1, platform=AGY))
    return replace(ledger, coverage=(*ledger.coverage, record))


def run_all(ledger: Ledger, *, skip: Sequence[str] = (), number: int = 1) -> Ledger:
    """Every slot of round `number`, except `skip`, covered by its own run."""
    plan = ledger.rounds[number - 1].plan
    assert plan is not None
    for index, slot in enumerate(plan.slots):
        if slot.id not in skip:
            ledger = run_slot(ledger, slot.id, f"a{number}-{index}", 100 + 10 * index + 1000 * number, number=number)
    return ledger


def advance(
    ledger: Ledger, hunks: Sequence[Hunk], *, deep: bool = False, first_deep: bool = False
) -> tuple[Ledger, Round]:
    """Open the next round for `hunks` as the CLI will: lens-aware carry-over, then the plan (pending slots included)."""
    snap = stable_hunk_ids(ledger, make_snap(*hunks, fingerprint=f"fp{len(ledger.rounds) + 2}"))
    if not round_unchanged(ledger, snap, None):
        ledger = seal_round(ledger)  # what `prepare` does before it opens a new round
    required = required_by_hunk(snap.hunks, deep=deep)
    have = credited(ledger, enforced=True)
    carry_sweep = sweep_pending(ledger)
    opened, round_ = open_round(ledger, snap, deep, "p.md", {}, required=required, credited=have)
    round_ = replace(round_, prepared_at=at(5000 + 1000 * len(ledger.rounds)), enforced=True)
    open_hunks = [hunk for hunk in snap.hunks if hunk.id in round_.open_hunk_ids]
    pending = pending_wave_slots(ledger, open_hunks=open_hunks, round_hunks=snap.hunks)
    plan = build_plan(
        open_hunks,
        owed_lenses(required, round_.hunks, have),
        depth=Depth(deep, ()),
        round_number=round_.number,
        first_deep_round=first_deep,
        existing=None,
        pending_wave_slots=pending,
        round_hunks=snap.hunks,
        carry_sweep=carry_sweep,
    )
    round_ = replace(round_, plan=plan)
    return replace(opened, rounds=(*opened.rounds[:-1], round_)), round_


class TriggerTests(unittest.TestCase):
    def finding(self, **overrides: object) -> Finding:
        ledger = hunt([code("H1")])
        ledger = record_finding(ledger, **overrides)  # type: ignore[arg-type]
        return ledger.findings[0]

    def test_proven_and_probable_serious_findings_trigger(self) -> None:
        for severity in ("blocker", "major"):
            for verdict in ("proven", "probable"):
                with self.subTest(severity=severity, verdict=verdict):
                    self.assertTrue(is_trigger(self.finding(severity=severity, verdict=verdict), False))

    def test_a_minor_finding_triggers_only_when_proven(self) -> None:
        self.assertTrue(is_trigger(self.finding(severity="minor", verdict="proven"), False))
        self.assertFalse(is_trigger(self.finding(severity="minor", verdict="probable"), False))

    def test_other_verdicts_and_unjudged_findings_do_not_trigger(self) -> None:
        self.assertFalse(is_trigger(self.finding(verdict="dismissed"), False))
        self.assertFalse(is_trigger(self.finding(verdict=None), False))

    def test_simplicity_and_the_sweep_do_not_trigger(self) -> None:
        for lens in ("craft", "design", "sweep"):
            with self.subTest(lens=lens):
                self.assertFalse(is_trigger(self.finding(lens=lens), False))

    def test_a_finding_in_documentation_does_not_trigger(self) -> None:
        self.assertFalse(is_trigger(self.finding(), True))

    def test_a_merged_duplicate_does_not_trigger(self) -> None:
        merged = replace(self.finding(verdict="dismissed"), merged_into="F2")

        self.assertFalse(is_trigger(merged, False))


class RequiredSlotsTests(unittest.TestCase):
    def ledger(self, *hunks: Hunk) -> Ledger:
        return hunt(list(hunks) or [code("H1"), code("H2", "b.py")])

    def test_without_findings_the_stored_plan_is_the_answer(self) -> None:
        ledger = self.ledger()

        self.assertEqual(required_slots(ledger, ledger.rounds[0]), ledger.rounds[0].plan.slots)  # type: ignore[union-attr]

    def test_a_proven_finding_calls_for_a_fresh_set_and_a_variant(self) -> None:
        ledger = record_finding(self.ledger(), lens="failure", path="a.py", hunter="W1-2")
        ledger = replace(ledger, rounds=(replace(ledger.rounds[0]),))
        stored = ledger.rounds[0].plan.slots  # type: ignore[union-attr]
        since = ledger.findings[0].verdict_log[0].at

        slots = required_slots(ledger, ledger.rounds[0])

        self.assertEqual(slots[: len(stored)], stored)
        fresh, variant = slots[len(stored) :]
        self.assertEqual((fresh.id, fresh.kind, fresh.generation, fresh.trigger, fresh.since), ("W2-X1", "fresh", 2, "F1", since))
        self.assertEqual(fresh.lenses, ("state", "trust", "platform"))  # what the normal plan did not use
        self.assertEqual(fresh.hunks, ("H1", "H2"))
        self.assertEqual(
            (variant.id, variant.kind, variant.lenses, variant.trigger, variant.since),
            ("W2-F1", "variant", ("failure",), "F1", since),
        )
        self.assertEqual(variant.hunks, ("H1", "H2"))  # no hunk of a.py in the diff: all open hunks

    def test_the_fresh_set_falls_back_to_the_default_lenses_when_the_plan_used_them_all(self) -> None:
        ledger = record_finding(hunt([code("H1")], deep=True, first_deep=False))

        fresh = next(slot for slot in required_slots(ledger, ledger.rounds[0]) if slot.kind == "fresh")

        self.assertEqual(fresh.lenses, ("intent", "contracts", "failure"))

    def test_a_variant_covers_the_open_hunks_of_the_findings_file(self) -> None:
        ledger = record_finding(self.ledger(code("H1", "a.py"), code("H2", "b.py")), path="b.py")

        variant = required_slots(ledger, ledger.rounds[0])[-1]

        self.assertEqual((variant.id, variant.hunks), ("W2-F1", ("H2",)))

    def test_the_dynamic_fresh_set_is_cut_from_the_hunks_new_len(self) -> None:
        ledger = record_finding(self.ledger(code("H1", "a.py", 5)))

        fresh = next(slot for slot in required_slots(ledger, ledger.rounds[0]) if slot.kind == "fresh")

        self.assertEqual(fresh.changed_lines, ledger.rounds[0].hunks[0].new_len)

    def test_findings_that_do_not_trigger_add_nothing(self) -> None:
        ledger = self.ledger()
        for kwargs in ({"lens": "craft"}, {"verdict": "dismissed"}, {"severity": "minor", "verdict": "probable"}):
            ledger = record_finding(ledger, **kwargs)  # type: ignore[arg-type]

        self.assertEqual(required_slots(ledger, ledger.rounds[0]), ledger.rounds[0].plan.slots)  # type: ignore[union-attr]

    def test_a_finding_in_documentation_adds_nothing(self) -> None:
        ledger = record_finding(self.ledger(code("H1", "README.md")), path="README.md")

        self.assertEqual(required_slots(ledger, ledger.rounds[0]), ledger.rounds[0].plan.slots)  # type: ignore[union-attr]

    def test_findings_of_one_file_and_lens_share_a_variant_named_after_the_first(self) -> None:
        ledger = self.ledger(code("H1", "a.py"), code("H2", "b.py"))
        ledger = record_finding(ledger, path="a.py", severity="major")
        ledger = record_finding(ledger, path="a.py", severity="blocker")
        ledger = record_finding(ledger, path="b.py")

        variants = [slot for slot in required_slots(ledger, ledger.rounds[0]) if slot.kind == "variant"]

        self.assertEqual([(slot.id, slot.hunks, slot.trigger) for slot in variants], [
            ("W2-F1", ("H1",), "F1"),
            ("W2-F3", ("H2",), "F3"),
        ])

    def test_at_most_three_variants_highest_severity_first_then_finding_number(self) -> None:
        self.assertEqual(MAX_VARIANTS_PER_WAVE, 3)
        ledger = self.ledger()
        for lens, severity in (("intent", "major"), ("contracts", "major"), ("failure", "blocker"), ("ripple", "major"), ("tests", "blocker")):
            ledger = record_finding(ledger, lens=lens, severity=severity)  # type: ignore[arg-type]

        variants = [slot.id for slot in required_slots(ledger, ledger.rounds[0]) if slot.kind == "variant"]

        self.assertEqual(variants, ["W2-F3", "W2-F5", "W2-F1"])

    def test_a_stored_fresh_set_is_not_duplicated_and_stored_ids_are_skipped(self) -> None:
        ledger = hunt([code("H1")], deep=True, first_deep=True)
        ledger = record_finding(ledger)

        slots = required_slots(ledger, ledger.rounds[0])

        self.assertEqual([slot.id for slot in slots if slot.kind == "fresh"], ["W2-X1"])
        self.assertEqual(next(slot for slot in slots if slot.kind == "fresh").trigger, "deep")
        self.assertEqual([slot.id for slot in slots if slot.kind == "variant"], ["W2-F1"])
        ledger = planned(ledger, *slots)
        self.assertEqual(required_slots(ledger, ledger.rounds[0]), slots)

    def test_a_generation_two_finding_calls_for_generation_three_and_three_stops(self) -> None:
        ledger = self.ledger()
        ledger = record_finding(ledger, hunter="W1-1")
        ledger = record_finding(ledger, hunter="W2-F1", lens="failure", seconds=20)
        ledger = record_finding(ledger, hunter="W3-F2", lens="ripple", seconds=30)

        ids = [slot.id for slot in required_slots(ledger, ledger.rounds[0]) if slot.generation > 1]

        self.assertEqual(ids, ["W2-X1", "W2-F1", "W3-X1", "W3-F2"])

    def test_a_lead_or_untagged_finding_is_generation_one(self) -> None:
        for hunter in ("lead", None):
            with self.subTest(hunter=hunter):
                ledger = record_finding(self.ledger(), hunter=hunter)

                generations = {slot.generation for slot in required_slots(ledger, ledger.rounds[0]) if slot.generation > 1}

                self.assertEqual(generations, {2})

    def test_only_findings_of_the_round_itself_count(self) -> None:
        ledger = record_finding(self.ledger())
        ledger, _ = open_round(ledger, make_snap(code("H1", "z.py"), fingerprint="fp2"), False, "p.md", {})

        self.assertEqual(required_slots(ledger, ledger.rounds[1]), ())

    def test_a_round_without_a_plan_requires_no_slots(self) -> None:
        ledger = record_finding(record_run(prepared(make_snap(code("H1")))[0], kind="repro", exit_code=1))

        self.assertEqual(required_slots(ledger, ledger.rounds[0]), ())

    def test_without_open_hunks_nothing_is_derived(self) -> None:
        ledger = record_finding(self.ledger())
        carried = tuple(replace(ref, carried=True) for ref in ledger.rounds[0].hunks)
        ledger = replace(ledger, rounds=(replace(ledger.rounds[0], hunks=carried),))

        self.assertEqual(required_slots(ledger, ledger.rounds[0]), ledger.rounds[0].plan.slots)  # type: ignore[union-attr]


class FindingGenerationTests(unittest.TestCase):
    def test_the_slot_a_finding_is_tagged_with_decides(self) -> None:
        ledger = record_finding(hunt([code("H1")]), hunter="W2-F1")

        self.assertEqual(finding_generation(ledger, ledger.rounds[0], ledger.findings[0], agent_runs(ledger)), 2)

    def test_the_slot_bound_to_the_recording_run_decides_when_the_tag_is_missing(self) -> None:
        ledger = hunt([code("H1")], deep=True, first_deep=True)
        ledger = run_slot(ledger, "W2-X1", "a2", 20)
        ledger = record_finding(ledger, by=f"{AGY}:a2", seconds=20)

        generation = finding_generation(ledger, ledger.rounds[0], ledger.findings[0], agent_runs(ledger))

        self.assertEqual(generation, 2)

    def test_the_binding_outranks_a_higher_tag(self) -> None:
        ledger = hunt([code("H1")])
        ledger = run_slot(ledger, "W1-1", "a1", 20)
        ledger = record_finding(ledger, by=f"{AGY}:a1", seconds=20, hunter="W3-F1")

        generation = finding_generation(ledger, ledger.rounds[0], ledger.findings[0], agent_runs(ledger))

        self.assertEqual(generation, 1)  # a hunter cannot skip a generation of waves by tagging

    def test_the_binding_outranks_a_lower_tag(self) -> None:
        ledger = hunt([code("H1")], deep=True, first_deep=True)
        ledger = run_slot(ledger, "W2-X1", "a2", 20)
        ledger = record_finding(ledger, by=f"{AGY}:a2", seconds=20, hunter="W1-1")

        generation = finding_generation(ledger, ledger.rounds[0], ledger.findings[0], agent_runs(ledger))

        self.assertEqual(generation, 2)

    def test_an_unattributed_untagged_finding_is_generation_one(self) -> None:
        ledger = record_finding(hunt([code("H1")]))

        self.assertEqual(finding_generation(ledger, ledger.rounds[0], ledger.findings[0], agent_runs(ledger)), 1)

    def test_a_finding_of_another_round_is_refused(self) -> None:
        ledger = record_finding(hunt([code("H1")]))
        stranger = replace(ledger.findings[0], round=2)

        with self.assertRaises(ValueError):
            finding_generation(ledger, ledger.rounds[0], stranger, ())

    def test_a_generation_two_hunter_finding_through_its_run_spawns_generation_three(self) -> None:
        ledger = hunt([code("H1")], deep=True, first_deep=True)
        ledger = run_slot(ledger, "W2-X1", "a2", 20)
        ledger = record_finding(ledger, by=f"{AGY}:a2", seconds=20)

        ids = [slot.id for slot in required_slots(ledger, ledger.rounds[0]) if slot.generation == 3]

        self.assertEqual(ids, ["W3-X1", "W3-F1"])


class SlotCompleteTests(unittest.TestCase):
    def test_a_slot_is_complete_when_vouched_coverage_covers_all_its_hunks(self) -> None:
        ledger = run_slot(hunt([code("H1"), code("H2", "b.py")]), "W1-1", "a1", 20)
        work = slot_work(ledger, agent_runs(ledger), slot_order(ledger))
        round_ = ledger.rounds[0]
        slot = round_.plan.slots[0]  # type: ignore[union-attr]

        self.assertTrue(slot_complete(round_, slot, work))

    def test_partial_coverage_does_not_complete_a_slot(self) -> None:
        ledger = run_slot(hunt([code("H1"), code("H2", "b.py")]), "W1-1", "a1", 20, only=("H1",))
        work = slot_work(ledger, agent_runs(ledger), slot_order(ledger))
        round_ = ledger.rounds[0]

        self.assertFalse(slot_complete(round_, round_.plan.slots[0], work))  # type: ignore[union-attr]

    def test_an_uncovered_slot_is_not_complete(self) -> None:
        ledger = hunt([code("H1")])
        round_ = ledger.rounds[0]

        self.assertFalse(slot_complete(round_, round_.plan.slots[0], {}))  # type: ignore[union-attr]

    def test_work_that_began_before_the_trigger_does_not_complete_a_wave_slot(self) -> None:
        ledger = hunt([code("H1")], deep=True, first_deep=True)
        ledger = run_slot(ledger, "W2-X1", "a2", 20)
        work = slot_work(ledger, agent_runs(ledger), slot_order(ledger))
        round_ = ledger.rounds[0]
        slot = round_.plan.slot("W2-X1")  # type: ignore[union-attr]
        assert slot is not None

        self.assertTrue(slot_complete(round_, slot, work))
        self.assertTrue(slot_complete(round_, replace(slot, since=at(19)), work))
        self.assertFalse(slot_complete(round_, replace(slot, since=at(21)), work))


class CarryOverTests(unittest.TestCase):
    def two(self) -> list[Hunk]:
        return [code("H1"), code("H2", "b.py")]

    def test_an_edit_before_every_slot_ran_keeps_the_owed_pairs(self) -> None:
        hunks = self.two()
        ledger = run_slot(hunt(hunks), "W1-1", "a1", 20)  # intent, ripple, tests only
        edited = [hunks[0], code("H3", "c.py")]

        ledger, round_ = advance(ledger, edited)

        # H1 is not carried: contracts, failure, craft and design were never examined.
        self.assertEqual(round_.open_hunk_ids, ("H1", "H3"))
        owed = owed_lenses(required_by_hunk(edited, deep=False), round_.hunks, credited(ledger))
        self.assertEqual(owed["H1"], frozenset({"contracts", "failure", "craft", "design"}))
        self.assertEqual(owed["H3"], frozenset((*CORE, *SIMPLICITY)))

    def test_a_fully_examined_hunk_is_carried(self) -> None:
        hunks = self.two()
        ledger = run_all(hunt(hunks))

        ledger, round_ = advance(ledger, [hunks[0], code("H3", "c.py")])

        self.assertEqual(round_.open_hunk_ids, ("H3",))
        self.assertEqual([ref.carried for ref in round_.hunks], [True, False])

    def test_a_round_that_turns_deep_reopens_carried_hunks_for_the_lenses_they_lack(self) -> None:
        hunks = self.two()
        ledger = run_all(hunt(hunks))
        extended = [*hunks, code("H3", "c.py")]

        _, normal = advance(ledger, extended)
        _, deep = advance(ledger, extended, deep=True, first_deep=True)

        self.assertEqual(normal.open_hunk_ids, ("H3",))
        self.assertEqual(deep.open_hunk_ids, ("H1", "H2", "H3"))
        assert deep.plan is not None
        deep_lenses = {lens for slot in deep.plan.slots if slot.generation == 1 for lens in slot.lenses}
        self.assertTrue({"state", "trust", "platform", "model"} <= deep_lenses)

    def test_deep_owes_only_the_lenses_that_are_missing(self) -> None:
        hunks = self.two()
        ledger = run_all(hunt(hunks))
        extended = [*hunks, code("H3", "c.py")]

        ledger, round_ = advance(ledger, extended, deep=True)
        owed = owed_lenses(required_by_hunk(extended, deep=True), round_.hunks, credited(ledger))

        self.assertEqual(owed["H1"], frozenset({"state", "trust", "platform", "model"}))

    def test_legacy_coverage_keeps_the_five_oh_behavior(self) -> None:
        hunks = self.two()
        snap = make_snap(*hunks)
        legacy, _ = prepared(snap)
        legacy = replace(legacy, coverage=(Coverage(1, tuple(h.digest for h in hunks), coverage_note(["H1", "H2"]), at(1)),))

        _, normal = advance(legacy, [*hunks, code("H3", "c.py")])
        _, deep = advance(legacy, [*hunks, code("H3", "c.py")], deep=True)

        self.assertEqual(normal.open_hunk_ids, ("H3",))
        self.assertEqual(deep.open_hunk_ids, ("H3",))


class UnfinishedWorkSurvivesAnEditTests(unittest.TestCase):
    """An edit and a new `prepare` must never make pending work disappear (B5)."""

    def two(self) -> list[Hunk]:
        return [code("H1"), code("H2", "b.py")]

    def swept(self, ledger: Ledger, number: int = 1) -> list[Slot]:
        plan = ledger.rounds[number - 1].plan
        assert plan is not None
        return [slot for slot in plan.slots if "sweep" in slot.lenses]

    def test_an_unfinished_sweep_is_required_again_after_a_tiny_edit(self) -> None:
        hunks = self.two()
        ledger = run_all(hunt(hunks), skip=("W1-2",))  # the slot with the sweep never ran
        self.assertTrue(sweep_pending(ledger))

        ledger, round_ = advance(ledger, [hunks[0], code("H3", "c.py")])

        (sweep,) = self.swept(ledger, 2)
        self.assertEqual((sweep.kind, sweep.lenses), ("sweep", ("sweep",)))
        self.assertEqual(sweep.hunks, tuple(ref.id for ref in round_.hunks))
        self.assertIn("H3", sweep.hunks)

    def test_a_finished_sweep_is_not_carried(self) -> None:
        hunks = self.two()
        ledger = run_all(hunt(hunks))
        self.assertFalse(sweep_pending(ledger))

        ledger, _ = advance(ledger, [hunks[0], code("H3", "c.py")])

        self.assertEqual(self.swept(ledger, 2), [])

    def test_a_later_round_without_a_sweep_does_not_forgive_an_unfinished_one(self) -> None:
        hunks = self.two()
        ledger = run_all(hunt(hunks), skip=("W1-2",))
        ledger, _ = advance(ledger, [hunks[0], code("H3", "c.py")])
        ledger = replace(ledger, rounds=(*ledger.rounds[:1], replace(ledger.rounds[1], plan=HuntPlan((), ()))))

        self.assertTrue(sweep_pending(ledger))

    def test_a_sweep_finished_in_a_later_round_ends_the_obligation(self) -> None:
        hunks = self.two()
        ledger = run_all(hunt(hunks), skip=("W1-2",))
        edited = [hunks[0], code("H3", "c.py")]
        ledger, _ = advance(ledger, edited)
        ledger = run_all(ledger, number=2)

        self.assertFalse(sweep_pending(ledger))

    def test_no_plan_and_no_sweep_means_nothing_is_pending(self) -> None:
        self.assertFalse(sweep_pending(Ledger()))
        self.assertFalse(sweep_pending(prepared(make_snap(code("H1")))[0]))

    def test_an_unfinished_fresh_set_is_stored_again_over_all_hunks(self) -> None:
        hunks = self.two()
        ledger = run_all(hunt(hunks, deep=True, first_deep=True), skip=("W2-X1",))

        ledger, round_ = advance(ledger, [hunks[0], code("H3", "c.py")], deep=True)

        assert round_.plan is not None
        fresh = round_.plan.slot("W2-X1")
        assert fresh is not None
        self.assertEqual((fresh.kind, fresh.trigger, fresh.hunks), ("fresh", "deep", tuple(ref.id for ref in round_.hunks)))
        self.assertEqual(fresh.lenses, ("intent", "contracts", "failure"))

    def test_a_finished_fresh_set_is_not_carried(self) -> None:
        hunks = self.two()
        ledger = run_all(hunt(hunks, deep=True, first_deep=True))

        ledger, round_ = advance(ledger, [hunks[0], code("H3", "c.py")], deep=True)

        assert round_.plan is not None
        self.assertEqual([slot.id for slot in round_.plan.slots if slot.kind == "fresh"], [])

    def test_an_unfinished_variant_is_stored_again_over_the_open_hunks_of_its_file(self) -> None:
        hunks = [code("H1", "a.py"), code("H2", "b.py")]
        ledger = record_finding(run_all(hunt(hunks)), path="a.py", lens="failure")
        since = ledger.findings[0].verdict_log[0].at

        # a.py is edited: the new hunk is open, b.py is carried.
        ledger, round_ = advance(ledger, [code("H3", "a.py"), hunks[1]])

        assert round_.plan is not None
        variant = round_.plan.slot("W2-F1")
        assert variant is not None
        self.assertEqual((variant.kind, variant.lenses, variant.trigger, variant.since), ("variant", ("failure",), "F1", since))
        self.assertEqual(variant.hunks, ("H3",))
        fresh = round_.plan.slot("W2-X1")
        assert fresh is not None
        self.assertEqual(fresh.trigger, "F1")
        self.assertEqual(fresh.hunks, ("H3", "H2"))

    def test_a_completed_variant_is_not_carried(self) -> None:
        hunks = [code("H1", "a.py")]
        ledger = record_finding(run_all(hunt(hunks)), path="a.py")
        stored = required_slots(ledger, ledger.rounds[0])
        ledger = replace(ledger, rounds=(replace(ledger.rounds[0], plan=HuntPlan((), stored)),))
        ledger = run_all(ledger, number=1)

        self.assertEqual(pending_wave_slots(ledger, open_hunks=[code("H2", "a.py")], round_hunks=[code("H2", "a.py")]), ())

    def test_a_variant_without_open_hunks_is_dropped_but_a_fresh_set_follows_the_round(self) -> None:
        hunks = [code("H1", "a.py")]
        ledger = record_finding(run_all(hunt(hunks)), path="a.py")

        slots = pending_wave_slots(seal_round(ledger), open_hunks=[], round_hunks=[code("H1", "a.py")])

        self.assertEqual([slot.id for slot in slots], ["W2-X1"])

    def test_carried_slots_are_ordered_fresh_before_variants_by_generation(self) -> None:
        ledger = record_finding(run_all(hunt([code("H1", "a.py")])), path="a.py")
        ledger = record_finding(ledger, path="a.py", lens="failure", hunter="W2-F1", seconds=30)
        hunk = [code("H9", "a.py")]

        slots = pending_wave_slots(seal_round(ledger), open_hunks=hunk, round_hunks=hunk)

        self.assertEqual([slot.id for slot in slots], ["W2-X1", "W2-F1", "W3-X1", "W3-F2"])

    def test_delta_round_with_a_simplicity_slot_that_never_ran(self) -> None:
        hunks = self.two()
        ledger = run_all(hunt(hunks), skip=("W1-3",))  # [craft, design] never ran

        ledger, round_ = advance(ledger, [hunks[0], code("H3", "c.py")])

        assert round_.plan is not None
        simplicity = [slot for slot in round_.plan.slots if slot.kind == "simplicity"]
        self.assertEqual([(slot.lenses, slot.hunks) for slot in simplicity], [(SIMPLICITY, ("H1", "H3"))])

    def test_a_delta_round_that_owes_only_simplicity_has_no_bug_slot(self) -> None:
        hunks = self.two()
        ledger = run_all(hunt(hunks), skip=("W1-3",))

        ledger, round_ = advance(ledger, [hunks[0]])  # the edited hunk is deleted

        assert round_.plan is not None
        self.assertEqual([(slot.kind, slot.lenses) for slot in round_.plan.slots], [("simplicity", SIMPLICITY)])


class AdapterTests(unittest.TestCase):
    def test_lenses_and_order_cover_stored_and_dynamic_slots(self) -> None:
        ledger = record_finding(hunt([code("H1")]))
        lenses, order = slot_lenses(ledger), slot_order(ledger)

        self.assertEqual(lenses(1, "W1-1"), ("intent", "ripple", "tests"))
        self.assertEqual(lenses(1, "W2-F1"), ("intent",))
        self.assertIsNone(lenses(1, "W1-9"))
        self.assertIsNone(lenses(2, "W1-1"))
        self.assertEqual(list(order(1)), ["W1-1", "W1-2", "W1-3", "W2-X1", "W2-F1"])
        self.assertEqual(list(order(2)), [])

    def test_the_hunks_of_a_slot_cover_stored_and_dynamic_slots(self) -> None:
        ledger = record_finding(hunt([code("H1")]))
        hunks = slot_hunks(ledger)

        self.assertEqual(hunks(1, "W1-1"), frozenset({"H1"}))
        self.assertEqual(hunks(1, "W2-F1"), frozenset({"H1"}))
        self.assertIsNone(hunks(1, "W1-9"))
        self.assertIsNone(hunks(2, "W1-1"))

    def test_a_slot_credits_only_its_own_hunks(self) -> None:
        first, second = code("H1", count=300), code("H2", "b.py", count=300)
        ledger = hunt([first, second])
        slot = ledger.rounds[0].plan.slots[0]  # type: ignore[union-attr]
        self.assertEqual(slot.hunks, ("H1",))
        smuggled = run_slot(ledger, slot.id, "a1", 20, only=("H1", "H2"))

        self.assertEqual(slot_hunks(smuggled)(1, slot.id), frozenset({"H1"}))
        self.assertEqual(set(credited(smuggled)), {first.digest})

    def test_credited_lenses_come_from_the_slots_that_ran(self) -> None:
        hunk = code("H1")
        ledger = run_slot(hunt([hunk]), "W1-1", "a1", 20)

        self.assertEqual(credited(ledger), {hunk.digest: frozenset({"intent", "ripple", "tests"})})

    def test_a_dynamic_slot_that_ran_credits_its_lenses(self) -> None:
        hunk = code("H1")
        ledger = record_finding(hunt([hunk]), lens="failure")
        stored = ledger.rounds[0].plan
        assert stored is not None
        ledger = replace(ledger, coverage=(Coverage(1, (hunk.digest,), coverage_note(["H1"]), at(40), hunter="W2-F1", by=f"{AGY}:a9"),))
        ledger = add_agent_event(ledger, agent_event("start", REVIEWER, "a9", 39, platform=AGY))
        ledger = add_agent_event(ledger, agent_event("stop", REVIEWER, "a9", 41, platform=AGY))

        self.assertEqual(credited(ledger), {hunk.digest: frozenset({"failure"})})


class DataFilesAreNotProductionTests(unittest.TestCase):
    def sql(self, path: str) -> Hunk:
        return code("H1", path, extra=(SQL_LINE,))

    def test_a_deep_signal_in_a_data_or_generated_file_does_not_make_the_round_deep(self) -> None:
        for path in ("assets/logo.svg", "go.sum", "static/app.min.js", "uv.lock"):
            with self.subTest(path=path):
                depth = decide_depth(make_snap(self.sql(path)), floor=False)

                self.assertEqual(depth, Depth(False, ()))

    def test_a_deep_signal_in_production_code_still_does_(self) -> None:
        depth = decide_depth(make_snap(self.sql("app.py"), code("H2", "go.sum", extra=(SQL_LINE,))), floor=False)

        self.assertEqual(depth.reasons, ("sql signal in H1",))

    def test_such_a_hunk_does_not_end_a_delta_round(self) -> None:
        hunk = self.sql("go.sum")
        owed = owed_lenses(required_by_hunk([hunk], deep=False), [hunk], {})

        plan = build_plan(
            [hunk], owed, depth=Depth(False, ()), round_number=2, first_deep_round=False, existing=None
        )

        self.assertEqual([(slot.kind, len(slot.lenses)) for slot in plan.slots], [("bug", 5), ("simplicity", 2)])


class StaticFreshSetTests(unittest.TestCase):
    def test_raising_a_test_only_round_to_deep_adds_the_fresh_set(self) -> None:
        hunks = [code("H1", "tests/test_a.py")]
        first = plan_of(hunks)

        raised = plan_of(hunks, deep=True, first_deep=True, existing=first)

        self.assertEqual(raised.slots[: len(first.slots)], first.slots)
        self.assertEqual([(s.id, s.hunks) for s in raised.slots[len(first.slots) :]], [("W2-X1", ("H1",))])

    def test_a_round_whose_hunks_are_all_carried_still_gets_the_fresh_set_over_the_round(self) -> None:
        hunks = [code("H1"), code("H2", "b.py")]

        plan = build_plan(
            [],
            {},
            depth=Depth(True, ("requested with --deep",)),
            round_number=2,
            first_deep_round=True,
            existing=None,
            round_hunks=hunks,
        )

        self.assertEqual([(s.id, s.kind, s.hunks) for s in plan.slots], [("W2-X1", "fresh", ("H1", "H2"))])

    def test_a_documentation_only_deep_round_has_no_fresh_set(self) -> None:
        plan = plan_of([code("H1", "README.md")], deep=True, first_deep=True)

        self.assertEqual([s.id for s in plan.slots if s.kind == "fresh"], [])

    def test_documentation_is_left_out_of_the_fresh_set(self) -> None:
        plan = plan_of([code("H1"), code("H2", "README.md")], deep=True, first_deep=True)

        fresh = [s for s in plan.slots if s.kind == "fresh"]
        self.assertEqual([(s.id, s.hunks) for s in fresh], [("W2-X1", ("H1",))])

    def test_the_fresh_set_walks_the_files_from_the_other_end(self) -> None:
        hunks = [code("H1", "a.py", 300), code("H2", "b.py", 300)]

        plan = plan_of(hunks, deep=True, first_deep=True)

        fresh = [(s.id, s.hunks) for s in plan.slots if s.kind == "fresh"]
        self.assertEqual(fresh, [("W2-X1", ("H2",)), ("W2-X2", ("H1",))])


class SweepEdgeTests(unittest.TestCase):
    def carried(self, hunks: Sequence[Hunk]) -> HuntPlan:
        return build_plan(
            [],
            {},
            depth=Depth(False, ()),
            round_number=2,
            first_deep_round=False,
            existing=None,
            round_hunks=hunks,
            carry_sweep=True,
        )

    def test_a_carried_sweep_over_a_round_without_hunks_is_no_slot_and_no_error(self) -> None:
        self.assertEqual(self.carried([]).slots, ())

    def test_a_carried_sweep_reads_code_not_documentation(self) -> None:
        plan = self.carried([code("H1", "README.md"), code("H2"), code("H3", "b.py")])

        self.assertEqual([(s.kind, s.hunks) for s in plan.slots], [("sweep", ("H2", "H3"))])

    def test_a_carried_sweep_over_documentation_only_still_reads_it(self) -> None:
        plan = self.carried([code("H1", "README.md"), code("H2", "docs/guide.md")])

        self.assertEqual([(s.kind, s.hunks) for s in plan.slots], [("sweep", ("H1", "H2"))])

    def test_a_carried_sweep_covers_carried_hunks_too(self) -> None:
        hunks = [code("H1", "a.py", 300), code("H2", "b.py", 300)]
        ledger = hunt(hunks)
        assert ledger.rounds[0].plan is not None
        sweep_slot = ledger.rounds[0].plan.slots[-1]
        self.assertEqual(sweep_slot.kind, "sweep")  # a separate [sweep] slot, which never runs here
        ledger = run_all(ledger, skip=(sweep_slot.id,))

        ledger, round_ = advance(ledger, [hunks[0], code("H3", "c.py")])

        assert round_.plan is not None
        (sweep,) = [slot for slot in round_.plan.slots if "sweep" in slot.lenses]
        carried = [ref.id for ref in round_.hunks if ref.carried]
        self.assertTrue(carried)
        self.assertEqual(sweep.hunks, tuple(ref.id for ref in round_.hunks))

    def test_preparing_again_with_nothing_new_appends_no_sweep(self) -> None:
        existing = HuntPlan(
            (),
            (
                make_slot("W1-1", lenses=("intent",), kind="bug"),
                make_slot("W1-2", lenses=("contracts",), kind="bug"),
            ),
        )

        plan = build_plan(
            [code("H1")], {}, depth=Depth(False, ()), round_number=1, first_deep_round=False, existing=existing
        )

        self.assertEqual(plan, existing)

    def test_a_sweep_covering_only_some_hunks_is_still_pending(self) -> None:
        hunks = [code("H1", "a.py", 300), code("H2", "b.py", 300)]
        ledger = hunt(hunks)
        assert ledger.rounds[0].plan is not None
        sweep_id = ledger.rounds[0].plan.slots[-1].id
        ledger = run_all(ledger, skip=(sweep_id,))
        ledger = run_slot(ledger, sweep_id, "partial", 3000, only=("H1",))

        self.assertTrue(sweep_pending(ledger))

        ledger = run_slot(ledger, sweep_id, "rest", 3100, only=("H2",))

        self.assertFalse(sweep_pending(ledger))


class SliceBoundaryTests(unittest.TestCase):
    def test_a_file_that_exactly_fills_the_budget_shares_the_slice(self) -> None:
        both = [code("H1", "a.py", 200), code("H2", "b.py", 200)]
        over = [code("H1", "a.py", 200), code("H2", "b.py", 201)]

        self.assertEqual(slice_hunks(both), (("H1", "H2"),))
        self.assertEqual(slice_hunks(over), (("H1",), ("H2",)))


class VariantDetailTests(unittest.TestCase):
    def ledger(self) -> Ledger:
        return hunt([code("H1", "a.py"), code("H2", "b.py")])

    def test_a_cluster_ranks_by_its_best_severity(self) -> None:
        ledger = self.ledger()
        for lens, severity in (
            ("contracts", "major"),
            ("failure", "major"),
            ("ripple", "major"),
            ("intent", "major"),
            ("intent", "blocker"),
        ):
            ledger = record_finding(ledger, lens=lens, severity=severity)  # type: ignore[arg-type]

        variants = [slot.id for slot in required_slots(ledger, ledger.rounds[0]) if slot.kind == "variant"]

        self.assertEqual(variants, ["W2-F4", "W2-F1", "W2-F2"])

    def test_a_wave_is_due_since_its_earliest_verdict(self) -> None:
        ledger = record_finding(self.ledger(), path="a.py", seconds=5)
        ledger = record_finding(ledger, path="a.py", seconds=15)
        earliest = ledger.findings[0].verdict_log[0].at

        slots = required_slots(ledger, ledger.rounds[0])

        self.assertEqual({slot.since for slot in slots if slot.generation == 2}, {earliest})

    def test_work_that_began_exactly_at_the_trigger_completes_a_wave_slot(self) -> None:
        ledger = hunt([code("H1")], deep=True, first_deep=True)
        ledger = run_slot(ledger, "W2-X1", "a2", 20)
        work = slot_work(ledger, agent_runs(ledger), slot_order(ledger))
        round_ = ledger.rounds[0]
        slot = round_.plan.slot("W2-X1")  # type: ignore[union-attr]
        assert slot is not None
        first = work[(1, "W2-X1")].first_at
        assert first is not None

        self.assertTrue(slot_complete(round_, replace(slot, since=first.isoformat().replace("+00:00", "Z")), work))

    def test_a_pending_variant_with_no_open_hunk_of_its_file_covers_the_open_hunks(self) -> None:
        ledger = record_finding(run_all(hunt([code("H1", "a.py")])), path="a.py")
        elsewhere = [code("H5", "b.py"), code("H6", "c.py")]

        slots = pending_wave_slots(seal_round(ledger), open_hunks=elsewhere, round_hunks=elsewhere)

        variant = next(slot for slot in slots if slot.kind == "variant")
        self.assertEqual(variant.hunks, ("H5", "H6"))


class DocsSliceKindTests(unittest.TestCase):
    def docs_plan(self, credited_lenses_: frozenset[str]) -> HuntPlan:
        hunk = code("H1", "README.md")
        return plan_of([hunk], have={hunk.digest: credited_lenses_})

    def test_a_docs_slice_owing_only_craft_is_a_simplicity_slot(self) -> None:
        plan = self.docs_plan(frozenset({"intent"}))

        self.assertEqual([(s.kind, s.lenses) for s in plan.slots], [("simplicity", ("craft",))])

    def test_a_docs_slice_owing_intent_is_a_bug_slot(self) -> None:
        plan = self.docs_plan(frozenset({"craft"}))

        self.assertEqual([(s.kind, s.lenses) for s in plan.slots], [("bug", ("intent",))])


def judge(ledger: Ledger, finding_id: str, verdict: Verdict, seconds: float) -> Ledger:
    """`finding_id` judged `verdict` at `seconds` after T0, after any verdicts it already has."""
    found = next(item for item in ledger.findings if item.id == finding_id)
    token = f"V{1 + sum(len(item.verdict_log) for item in ledger.findings)}"
    evidence = "reproduced by tests/test_app.py::test_empty" if verdict == "proven" else STATIC_EVIDENCE
    updated = replace(
        found,
        verdict=verdict,
        verdict_evidence=evidence,
        verdict_run="R1" if verdict == "proven" else None,
        verdict_log=(*found.verdict_log, VerdictEvent(token, verdict, at(seconds))),
    )
    return replace(ledger, findings=tuple(updated if item.id == finding_id else item for item in ledger.findings))


def claim(ledger: Ledger, slot_id: str, seconds: float, *, number: int = 1) -> Ledger:
    token = f"B{len(ledger.claims) + 1}"
    return replace(ledger, claims=(*ledger.claims, SlotClaim(number, slot_id, token, at(seconds))))


class FreshSetIsAUnitTests(unittest.TestCase):
    def test_a_set_repartitioned_into_fewer_slots_leaves_no_orphan_behind(self) -> None:
        hunks = [code("H1", "a.py", 300), code("H2", "b.py", 300)]
        ledger = hunt(hunks, deep=True, first_deep=True)
        assert ledger.rounds[0].plan is not None
        self.assertEqual([s.id for s in ledger.rounds[0].plan.slots if s.kind == "fresh"], ["W2-X1", "W2-X2"])
        ledger = run_all(ledger, skip=("W2-X1", "W2-X2"))

        # H2 is replaced by a small hunk: the unfinished set is stored again, now as a single slot.
        ledger, second = advance(ledger, [hunks[0], code("H3", "c.py", 5)], deep=True)
        assert second.plan is not None
        self.assertEqual([s.id for s in second.plan.slots if s.kind == "fresh"], ["W2-X1"])
        ledger = run_all(ledger, number=2)

        ledger, third = advance(ledger, [hunks[0], code("H4", "d.py", 5)], deep=True)

        assert third.plan is not None
        self.assertEqual([s.id for s in third.plan.slots if s.kind == "fresh"], [])

    def test_the_rebuilt_lenses_come_from_the_latest_set_only(self) -> None:
        hunks = [code("H1", "a.py", 300), code("H2", "b.py", 300)]
        ledger = run_all(hunt(hunks, deep=True, first_deep=True), skip=("W2-X1", "W2-X2"))
        ledger, _ = advance(ledger, [hunks[0], code("H3", "c.py", 5)], deep=True)
        assert ledger.rounds[1].plan is not None
        only = ledger.rounds[1].plan.slot("W2-X1")
        assert only is not None

        pending = pending_wave_slots(ledger, open_hunks=[code("H3", "c.py", 5)], round_hunks=[code("H3", "c.py", 5)])

        self.assertEqual([(s.id, s.lenses) for s in pending], [("W2-X1", only.lenses)])


class MaterializeTests(unittest.TestCase):
    """What someone acted on is stored; the rest of the latest round stays provisional (B2 section 0)."""

    def four(self) -> Ledger:
        ledger = hunt([code("H1")])
        for lens, severity in (("intent", "major"), ("contracts", "major"), ("failure", "major")):
            ledger = record_finding(ledger, lens=lens, severity=severity)  # type: ignore[arg-type]
        return ledger

    def variants(self, ledger: Ledger) -> list[str]:
        return [s.id for s in required_slots(ledger, ledger.rounds[-1]) if s.kind == "variant"]

    def test_a_materialized_variant_survives_its_finding_being_dismissed(self) -> None:
        ledger = materialize(record_finding(hunt([code("H1")]), lens="failure"), 1, ["W2-F1"])

        ledger = judge(ledger, "F1", "dismissed", 40)

        slots = {s.id: s for s in required_slots(ledger, ledger.rounds[0])}
        self.assertEqual(slots["W2-F1"].lenses, ("failure",))
        self.assertEqual(slots["W2-F1"].since, ledger.findings[0].verdict_log[0].at)  # when it first was proven
        self.assertNotIn("W2-X1", slots)  # nobody acted on the fresh set: it went with the finding

    def test_a_slot_nobody_acted_on_goes_with_its_dismissed_finding(self) -> None:
        ledger = judge(record_finding(hunt([code("H1")])), "F1", "dismissed", 40)

        self.assertEqual(required_slots(ledger, ledger.rounds[0]), ledger.rounds[0].plan.slots)  # type: ignore[union-attr]

    def test_a_materialized_fresh_slot_takes_its_whole_set_along(self) -> None:
        hunks = [code("H1", "a.py", 300), code("H2", "b.py", 300)]
        ledger = record_finding(hunt(hunks), path="a.py", lens="failure")
        self.assertEqual([s.id for s in required_slots(ledger, ledger.rounds[0]) if s.kind == "fresh"], ["W2-X1", "W2-X2"])

        ledger = judge(materialize(ledger, 1, ["W2-X2"]), "F1", "dismissed", 40)

        fresh = [s for s in required_slots(ledger, ledger.rounds[0]) if s.kind == "fresh"]
        self.assertEqual([(s.id, s.trigger) for s in fresh], [("W2-X1", "F1"), ("W2-X2", "F1")])

    def test_a_materialized_variant_displaced_from_the_top_three_stays(self) -> None:
        ledger = materialize(self.four(), 1, ["W2-F3"])

        ledger = record_finding(ledger, lens="ripple", severity="blocker", seconds=30)

        # the stored variant counts toward the cap: one stored, two derived (the blocker first)
        self.assertEqual(self.variants(ledger), ["W2-F3", "W2-F4", "W2-F1"])

    def test_without_materializing_the_cap_applies_to_what_is_derived(self) -> None:
        ledger = record_finding(self.four(), lens="ripple", severity="blocker", seconds=30)

        self.assertEqual(self.variants(ledger), ["W2-F4", "W2-F1", "W2-F2"])

    def test_a_materialized_slot_credits_its_lenses_to_vouched_coverage(self) -> None:
        ledger = materialize(self.four(), 1, ["W2-F3"])
        ledger = run_slot(ledger, "W2-F3", "a1", 20)

        self.assertEqual(slot_lenses(ledger)(1, "W2-F3"), ("failure",))
        self.assertIn("failure", credited(ledger)[ledger.rounds[0].hunks[0].digest])

    def test_coverage_tagged_with_a_derived_slot_does_not_store_it(self) -> None:
        ledger = self.four()
        digest = ledger.rounds[0].hunks[0].digest
        tagged = Coverage(1, (digest,), coverage_note(["H1"]), at(20), hunter="W2-F3")
        ledger = replace(ledger, coverage=(*ledger.coverage, tagged))

        ledger = record_finding(ledger, lens="ripple", severity="blocker", seconds=30)

        self.assertNotIn("W2-F3", self.variants(ledger))

    def test_ids_already_stored_are_skipped_and_nothing_changes(self) -> None:
        ledger = record_finding(hunt([code("H1")]), lens="failure")
        once = materialize(ledger, 1, ["W2-F1"])

        self.assertIs(materialize(ledger, 1, ["W1-1"]), ledger)
        self.assertEqual(materialize(once, 1, ["W2-F1", "W1-1"]), once)
        self.assertEqual([s.id for s in once.rounds[0].plan.slots][-1], "W2-F1")  # type: ignore[union-attr]

    def test_an_id_that_cannot_be_derived_is_refused(self) -> None:
        ledger = hunt([code("H1")])
        for slot_id in ("W2-F9", "W2-X1", "W3-X1", "W1-9"):
            with self.subTest(slot_id=slot_id), self.assertRaises(LedgerError):
                materialize(ledger, 1, [slot_id])

    def test_a_slot_of_an_unjudged_finding_or_of_a_zero_finding_round_is_not_derivable(self) -> None:
        unjudged = record_finding(hunt([code("H1")]), lens="failure", verdict=None)
        for slot_id in ("W2-F1", "W3-F1", "W2-X1"):
            with self.subTest(slot_id=slot_id), self.assertRaises(LedgerError):
                materialize(claim(unjudged, slot_id, 10), 1, [slot_id])

    def test_a_past_round_derives_nothing(self) -> None:
        ledger = record_finding(hunt([code("H1", "a.py")]), path="a.py", lens="failure")
        ledger, _ = advance(ledger, [code("H2", "b.py")])

        with self.assertRaises(LedgerError):
            materialize(ledger, 1, ["W2-F9"])

    def test_a_missing_round_or_a_round_without_a_plan_is_refused(self) -> None:
        with self.assertRaises(LedgerError):
            materialize(hunt([code("H1")]), 2, ["W2-F1"])
        bare = record_finding(record_run(prepared(make_snap(code("H1")))[0], kind="repro", exit_code=1))
        with self.assertRaises(LedgerError):
            materialize(bare, 1, ["W2-F1"])


class SealRoundTests(unittest.TestCase):
    def test_sealing_stores_everything_the_latest_round_requires(self) -> None:
        ledger = record_finding(hunt([code("H1")]), lens="failure")
        wanted = required_slots(ledger, ledger.rounds[0])

        sealed = seal_round(ledger)

        self.assertEqual(sealed.rounds[0].plan.slots, wanted)  # type: ignore[union-attr]
        self.assertEqual(sealed.rounds[0].plan.reasons, ledger.rounds[0].plan.reasons)  # type: ignore[union-attr]
        self.assertEqual(required_slots(sealed, sealed.rounds[0]), wanted)
        self.assertEqual(seal_round(sealed), sealed)

    def test_a_sealed_round_no_longer_follows_its_findings(self) -> None:
        ledger = seal_round(record_finding(hunt([code("H1")]), lens="failure"))

        ledger = judge(ledger, "F1", "dismissed", 40)

        self.assertEqual([s.id for s in required_slots(ledger, ledger.rounds[0]) if s.generation == 2], ["W2-X1", "W2-F1"])

    def test_nothing_to_seal_returns_the_ledger_itself(self) -> None:
        ledger = hunt([code("H1")])
        bare = record_run(prepared(make_snap(code("H1")))[0], kind="repro", exit_code=1)

        self.assertIs(seal_round(ledger), ledger)
        self.assertIs(seal_round(bare), bare)
        self.assertEqual(seal_round(Ledger()), Ledger())

    def test_only_the_latest_round_is_sealed(self) -> None:
        ledger = record_finding(hunt([code("H1", "a.py")]), path="a.py", lens="failure")
        ledger, _ = advance(ledger, [code("H2", "b.py")])
        before = ledger.rounds[0]

        self.assertEqual(seal_round(ledger).rounds[0], before)

    def test_a_finished_wave_does_not_come_back_in_later_rounds(self) -> None:
        """agy finding 2: the waves a round inherited vanished when the next round opened and then returned as zombies."""
        hunks = [code("H1", "a.py", 300), code("H2", "b.py", 300)]
        ledger = record_finding(run_all(hunt(hunks)), path="a.py", lens="failure", verdict=None)
        ledger, _ = advance(ledger, [hunks[0], code("H3", "b.py", 200)])
        ledger = judge(ledger, "F1", "proven", 6500)
        wave = [s.id for s in required_slots(ledger, ledger.rounds[1]) if s.generation == 2]
        self.assertEqual(sorted(wave), ["W2-F1", "W2-X1", "W2-X2"])
        ledger = materialize(ledger, 2, wave)
        for index, slot_id in enumerate(wave):
            ledger = run_slot(ledger, slot_id, f"r2-{index}", 6600 + 10 * index, number=2)
        second = required_slots(ledger, ledger.rounds[1])
        lenses = slot_lenses(ledger)(2, "W2-X2")
        self.assertIsNotNone(lenses)

        ledger, third = advance(ledger, [hunks[0], code("H4", "b.py", 200)])

        self.assertEqual(required_slots(ledger, ledger.rounds[1]), second)  # an immutable snapshot
        self.assertEqual(slot_lenses(ledger)(2, "W2-X2"), lenses)  # its lens credit is not retracted
        self.assertEqual([s.id for s in required_slots(ledger, third) if s.generation == 2], [])
        ledger, fourth = advance(ledger, [hunks[0], code("H5", "b.py", 200)])
        self.assertEqual([s.id for s in required_slots(ledger, fourth) if s.generation == 2], [])

    def test_an_unfinished_wave_moves_to_the_next_round_once(self) -> None:
        hunks = [code("H1", "a.py"), code("H2", "b.py")]
        ledger = record_finding(run_all(hunt(hunks)), path="a.py", lens="failure")

        ledger, second = advance(ledger, [code("H3", "a.py"), hunks[1]])
        ledger, third = advance(ledger, [code("H4", "a.py"), hunks[1]])

        assert second.plan is not None and third.plan is not None
        self.assertEqual(sorted(s.id for s in second.plan.slots if s.generation == 2), ["W2-F1", "W2-X1"])
        self.assertEqual(sorted(s.id for s in third.plan.slots if s.generation == 2), ["W2-F1", "W2-X1"])
        self.assertEqual(sorted(s.id for s in required_slots(ledger, third) if s.generation == 2), ["W2-F1", "W2-X1"])


class AgyPlannerFindingTests(unittest.TestCase):
    """Regression tests for the eight findings of the independent review of Phase B (REVIEW-B)."""

    # finding 1
    def test_raising_a_round_to_deep_in_place_releases_carried_hunks_that_lack_deep_lenses(self) -> None:
        hunks = [code("H1", "a.py"), code("H2", "b.py")]
        ledger = run_all(hunt(hunks))
        ledger, normal = advance(ledger, [*hunks, code("H3", "c.py")])
        self.assertEqual(normal.open_hunk_ids, ("H3",))
        snap = stable_hunk_ids(ledger, make_snap(*hunks, code("H3", "c.py"), fingerprint=normal.fingerprint))
        required = required_by_hunk(snap.hunks, deep=True)
        have = credited(ledger)

        raised_ledger, raised = open_round(ledger, snap, True, "p.md", {}, required=required, credited=have)

        self.assertTrue(raised.deep)
        self.assertEqual(len(raised_ledger.rounds), 2)
        self.assertEqual(raised.open_hunk_ids, ("H1", "H2", "H3"))
        owed = owed_lenses(required, raised.hunks, have)
        self.assertTrue({"state", "trust", "platform", "model"} <= owed["H1"])

    def test_raising_in_place_without_lens_data_changes_only_the_depth(self) -> None:
        hunks = [code("H1", "a.py")]
        ledger = run_all(hunt(hunks))
        ledger, normal = advance(ledger, [*hunks, code("H3", "c.py")])
        snap = stable_hunk_ids(ledger, make_snap(*hunks, code("H3", "c.py"), fingerprint=normal.fingerprint))

        _, raised = open_round(ledger, snap, True, "p.md", {})

        self.assertEqual(raised, replace(normal, deep=True))

    def test_the_static_fresh_set_spans_the_round_not_just_the_hunks_taking_new_lenses(self) -> None:
        hunks = [code("H1", "a.py", 200, extra=(FS_LINE, MODEL_LINE)), code("H2", "b.py", 10)]
        direct = plan_of(hunks, deep=True, first_deep=True)
        raised = plan_of(hunks, deep=True, first_deep=True, existing=plan_of(hunks, deep=False))

        def fresh(plan: HuntPlan) -> list[tuple[str, tuple[str, ...]]]:
            return [(s.id, s.hunks) for s in plan.slots if s.kind == "fresh"]

        self.assertEqual(fresh(direct), [("W2-X1", ("H1", "H2"))])
        self.assertEqual(fresh(raised), fresh(direct))

    # finding 3
    def test_a_wave_slot_is_completed_by_covering_again_after_the_trigger(self) -> None:
        ledger = record_finding(hunt([code("H1")]), lens="failure", seconds=5)
        ledger = run_slot(ledger, "W2-F1", "a1", 15, only=("H1",))
        ledger = judge(ledger, "F1", "dismissed", 20)
        ledger = judge(ledger, "F1", "proven", 30)  # the trigger moved to t=30
        ledger = materialize(ledger, 1, ["W2-F1"])
        slot = next(s for s in required_slots(ledger, ledger.rounds[0]) if s.id == "W2-F1")
        self.assertEqual(slot.since, at(30))

        def complete(current: Ledger) -> bool:
            work = slot_work(current, agent_runs(current), slot_order(current))
            return slot_complete(current.rounds[0], slot, work)

        self.assertFalse(complete(ledger))  # only the early run so far
        ledger = run_slot(ledger, "W2-F1", "a2", 40, only=("H1",))
        self.assertTrue(complete(ledger))

    def test_a_wave_slot_needs_every_hunk_covered_at_or_after_the_trigger(self) -> None:
        hunks = [code("H1", "a.py"), code("H2", "a.py")]
        ledger = record_finding(hunt(hunks), path="a.py", lens="failure", seconds=5)
        ledger = materialize(ledger, 1, ["W2-F1"])
        slot = next(s for s in required_slots(ledger, ledger.rounds[0]) if s.id == "W2-F1")
        ledger = replace(ledger, findings=(replace(ledger.findings[0], verdict_log=(VerdictEvent("V1", "proven", at(30)),)),))
        slot = replace(slot, since=at(30))

        def complete(current: Ledger) -> bool:
            work = slot_work(current, agent_runs(current), slot_order(current))
            return slot_complete(current.rounds[0], slot, work)

        ledger = run_slot(ledger, "W2-F1", "a1", 20, only=("H1",))  # H1 before the trigger
        ledger = run_slot(ledger, "W2-F1", "a1b", 40, only=("H2",))  # H2 after it
        self.assertFalse(complete(ledger))
        ledger = run_slot(ledger, "W2-F1", "a1c", 50, only=("H1",))  # H1 again, after it
        self.assertTrue(complete(ledger))

    # finding 4
    def test_binding_uses_the_wave_aware_order(self) -> None:
        ledger = record_finding(hunt([code("H1")]), lens="failure")
        digest = ledger.rounds[0].hunks[0].digest
        for seconds, slot_id in ((20, "W2-X1"), (21, "W1-9")):
            ledger = replace(
                ledger,
                coverage=(*ledger.coverage, Coverage(1, (digest,), coverage_note(["H1"]), at(seconds), hunter=slot_id, by=f"{AGY}:a2")),
            )
        ledger = add_agent_event(ledger, agent_event("start", REVIEWER, "a2", 19, platform=AGY))
        ledger = add_agent_event(ledger, agent_event("stop", REVIEWER, "a2", 25, platform=AGY))
        ledger = record_finding(ledger, lens="contracts", by=f"{AGY}:a2", hunter="W2-X1", seconds=22)
        runs = agent_runs(ledger)
        work = slot_work(ledger, runs, slot_order(ledger))
        fresh = next(s for s in required_slots(ledger, ledger.rounds[0]) if s.id == "W2-X1")

        self.assertTrue(slot_complete(ledger.rounds[0], fresh, work))
        self.assertEqual(finding_generation(ledger, ledger.rounds[0], ledger.findings[1], runs), 2)
        ids = [s.id for s in required_slots(ledger, ledger.rounds[0])]
        self.assertIn("W3-X1", ids)
        self.assertIn("W3-F2", ids)
        self.assertNotIn("W2-F2", ids)  # F2 is a generation 2 finding: it calls for generation 3 only

    def test_two_derived_wave_slots_bind_in_plan_order_not_id_order(self) -> None:
        ledger = record_finding(hunt([code("H1")]), lens="failure")
        ledger = run_slot(ledger, "W2-F1", "a2", 20)
        ledger = run_slot(ledger, "W2-X1", "a2", 22)
        ledger = record_finding(ledger, lens="contracts", by=f"{AGY}:a2", seconds=23)

        runs = agent_runs(ledger)

        self.assertEqual(finding_generation(ledger, ledger.rounds[0], ledger.findings[1], runs), 2)
        self.assertEqual(
            list(bound_slots(ledger, runs, slot_order(ledger)).values()), ["W2-X1"]  # fresh sets come first in plan order
        )

    # finding 5
    def test_an_unjudged_or_wrong_generation_finding_never_fabricates_a_slot(self) -> None:
        zero = claim(hunt([code("H1")]), "W2-X1", 10)
        unjudged = claim(record_finding(hunt([code("H1")]), lens="failure", verdict=None), "W3-F1", 10)

        self.assertEqual(required_slots(zero, zero.rounds[0]), zero.rounds[0].plan.slots)  # type: ignore[union-attr]
        self.assertEqual(required_slots(unjudged, unjudged.rounds[0]), unjudged.rounds[0].plan.slots)  # type: ignore[union-attr]

    def test_a_generation_three_fresh_set_takes_its_trigger_and_since_from_a_generation_two_finding(self) -> None:
        ledger = hunt([code("H1")])
        ledger = record_finding(ledger, hunter="W1-1", lens="intent", seconds=5)  # F1, generation 1, proven at 6
        ledger = record_finding(ledger, hunter="W2-F1", lens="failure", seconds=50)  # F2, generation 2, proven at 51
        ledger = materialize(ledger, 1, ["W3-X1"])
        ledger = judge(ledger, "F2", "dismissed", 70)

        w3 = next(s for s in required_slots(ledger, ledger.rounds[0]) if s.id == "W3-X1")

        self.assertEqual((w3.trigger, w3.since), ("F2", at(51)))

    def test_required_slots_never_repeats_an_id(self) -> None:
        h1, h2 = code("H1", "a.py", 300), code("H2", "b.py", 300)
        first = plan_of([h1], deep=True, first_deep=True)
        extended = build_plan(
            [h1, h2],
            {"H2": required_by_hunk([h1, h2], deep=True)["H2"]},
            depth=Depth(True, ()),
            round_number=1,
            first_deep_round=True,
            existing=first,
        )
        ledger = claim(planned(hunt([h1, h2], deep=True, first_deep=True), *extended.slots), "W2-X2", 20)

        ids = [s.id for s in required_slots(ledger, ledger.rounds[0])]

        self.assertEqual(len(ids), len(set(ids)))
        HuntPlan(reasons=(), slots=required_slots(ledger, ledger.rounds[0]))

    # finding 6
    def test_a_carried_sweep_is_a_slot_of_its_own_and_never_grows_a_core_slot(self) -> None:
        hunks = [code("H1", "a.py", 300), code("H2", "b.py", 300)]
        ledger = hunt(hunks)
        assert ledger.rounds[0].plan is not None
        sweep_id = ledger.rounds[0].plan.slots[-1].id
        ledger = run_all(ledger, skip=(sweep_id,))

        _, second = advance(ledger, [*hunks, code("H3", "c.py", 50)])

        assert second.plan is not None
        swept = [s for s in second.plan.slots if "sweep" in s.lenses]
        self.assertEqual([(s.kind, s.lenses) for s in swept], [("sweep", ("sweep",))])
        for slot in second.plan.slots:
            if slot.kind != "sweep":
                self.assertLessEqual(slot.changed_lines, SLICE_MAX_CHANGED_LINES)

    def test_partial_sweeps_of_two_rounds_do_not_add_up(self) -> None:
        hunks = [code("H1", "a.py", 200), code("H2", "b.py", 200), code("H3", "z.py", 200)]
        ledger = hunt(hunks)
        assert ledger.rounds[0].plan is not None
        first_sweep = ledger.rounds[0].plan.slots[-1].id
        ledger = run_all(ledger, skip=(first_sweep,))
        ledger = run_slot(ledger, first_sweep, "partial-r1", 3000, only=("H1", "H2"))
        self.assertTrue(sweep_pending(ledger))

        ledger, second = advance(ledger, [hunks[0], hunks[1], code("H4", "z.py", 5)])
        assert second.plan is not None
        sweep = next(s for s in second.plan.slots if "sweep" in s.lenses)
        ledger = run_slot(ledger, sweep.id, "partial-r2", 6000, number=2, only=("H4",))

        work = slot_work(ledger, agent_runs(ledger), slot_order(ledger))
        self.assertFalse(slot_complete(ledger.rounds[1], sweep, work))
        self.assertTrue(sweep_pending(ledger))
        for ref in ledger.rounds[1].hunks:
            self.assertNotIn("sweep", credited(ledger).get(ref.digest, frozenset()))
        states = {item.slot.id: item.state for item in slot_statuses(ledger, ledger.rounds[1], parse_timestamp(at(7000)))}
        self.assertNotEqual(states[sweep.id], "done")  # the gate agrees

    def test_a_core_slot_hosting_the_sweep_credits_the_sweep_only_when_it_covered_every_hunk(self) -> None:
        hunks = [code("H1", "a.py"), code("H2", "b.py")]
        ledger = hunt(hunks)
        host = next(s for s in ledger.rounds[0].plan.slots if "sweep" in s.lenses and s.kind == "bug")  # type: ignore[union-attr]
        ledger = run_slot(ledger, host.id, "a1", 20, only=("H1",))
        have = credited(ledger)

        self.assertIn("contracts", have[ledger.rounds[0].hunks[0].digest])
        self.assertNotIn("sweep", have[ledger.rounds[0].hunks[0].digest])
        self.assertEqual(slot_lenses(ledger)(1, host.id), host.lenses)  # the stored lenses are untouched

        ledger = run_slot(ledger, host.id, "a2", 30, only=("H2",))
        self.assertIn("sweep", credited(ledger)[ledger.rounds[0].hunks[1].digest])
        self.assertFalse(sweep_pending(ledger))

    def test_a_sweep_over_a_single_hunk_gets_no_slot_of_its_own(self) -> None:
        plan = build_plan(
            [],
            {},
            depth=Depth(False, ()),
            round_number=2,
            first_deep_round=False,
            existing=None,
            round_hunks=[code("H1")],
            carry_sweep=True,
        )

        self.assertEqual(plan.slots, ())

    def test_raising_a_delta_round_to_deep_in_place_adds_no_sweep(self) -> None:
        small = [code("H3", "c.py", 5), code("H4", "d.py", 5)]
        normal = plan_of(small, round_number=2)
        self.assertFalse(any("sweep" in s.lenses for s in normal.slots))

        raised = plan_of(small, deep=True, round_number=2, existing=normal)

        self.assertGreater(len(raised.slots), len(normal.slots))
        self.assertFalse(any("sweep" in s.lenses for s in raised.slots))

    # finding 7
    def test_a_cluster_keeps_the_representative_a_hunter_already_works_on(self) -> None:
        ledger = hunt([code("H1", "a.py")])
        ledger = record_finding(ledger, path="a.py", lens="failure", verdict=None, seconds=5)  # F1
        ledger = record_finding(ledger, path="a.py", lens="failure", verdict="proven", seconds=6)  # F2
        ledger = materialize(ledger, 1, ["W2-F2"])
        ledger = run_slot(ledger, "W2-F2", "a2", 20)

        ledger = judge(ledger, "F1", "proven", 30)

        variants = [s.id for s in required_slots(ledger, ledger.rounds[0]) if s.kind == "variant"]
        self.assertEqual(variants, ["W2-F2"])

    def test_stored_variants_of_a_generation_count_toward_the_cap(self) -> None:
        ledger = hunt([code("H1")])
        for lens in ("intent", "contracts", "failure", "ripple"):
            ledger = record_finding(ledger, lens=lens)
        ledger = materialize(ledger, 1, ["W2-F1", "W2-F2", "W2-F3"])

        variants = [s.id for s in required_slots(ledger, ledger.rounds[0]) if s.kind == "variant"]

        self.assertEqual(variants, ["W2-F1", "W2-F2", "W2-F3"])  # F4 would be a fourth

    # finding 8
    def test_config_suffixes_are_case_insensitive(self) -> None:
        for path in ("ci.YAML", "settings.Yml", "x.TOML", "app.ENV"):
            with self.subTest(path=path):
                self.assertTrue(is_config_path(path))
                self.assertEqual(hunk_kind(code("H1", path)), "config")


class RandomSequenceTests(unittest.TestCase):
    """Plans stay constructible and stored slots stay put, whatever order prepare, claims and verdicts come in."""

    SEQUENCES = 220
    STEPS = 7
    PATHS = ("a.py", "b.py", "c.py")
    LENSES = ("failure", "contracts", "intent", "ripple")

    def run_sequence(self, seed: int) -> None:
        rng = random.Random(seed)
        counter = 0

        def new_hunk(path: str) -> Hunk:
            nonlocal counter
            counter += 1
            return code(f"H{counter}", path, rng.choice((5, 150, 300)))

        hunks = [new_hunk(path) for path in self.PATHS[: rng.randint(1, 3)]]
        ledger = hunt(hunks)
        clock = 10.0
        stored: dict[int, tuple[str, ...]] = {}
        sealed: dict[int, tuple[Slot, ...]] = {}
        for step in range(self.STEPS):
            clock += 100
            action = rng.choice(("finding", "finding", "judge", "claim", "advance", "run"))
            latest = ledger.rounds[-1]
            if action == "finding":
                ledger = record_finding(
                    ledger,
                    lens=rng.choice(self.LENSES),
                    path=rng.choice(self.PATHS),
                    severity=rng.choice(("minor", "major", "blocker")),
                    verdict=rng.choice((None, "proven", "probable", "dismissed")),
                    seconds=clock,
                )
            elif action == "judge" and ledger.findings:
                target = rng.choice(ledger.findings)
                ledger = judge(ledger, target.id, rng.choice(("proven", "probable", "dismissed")), clock)
            elif action in ("claim", "run"):
                slots = [s for s in required_slots(ledger, latest) if s.generation >= 2]
                if slots:
                    slot = rng.choice(slots)
                    ledger = claim(materialize(ledger, latest.number, [slot.id]), slot.id, clock, number=latest.number)
                    self.tally["materialized"] += 1
                    if action == "run":
                        ledger = run_slot(ledger, slot.id, f"s{seed}-{step}", clock + 10, number=latest.number)
            elif action == "advance":
                closing = seal_round(ledger)
                sealed[latest.number] = required_slots(closing, closing.rounds[-1])
                self.tally["sealed"] += len(sealed[latest.number]) - len(latest.plan.slots)  # type: ignore[union-attr]
                hunks = [hunk for hunk in hunks if rng.random() < 0.7] or [new_hunk(rng.choice(self.PATHS))]
                if rng.random() < 0.7:
                    hunks.append(new_hunk(rng.choice(self.PATHS)))
                ledger, _ = advance(ledger, hunks)
            self.check(ledger, stored, sealed)

    def check(self, ledger: Ledger, stored: dict[int, tuple[str, ...]], sealed: dict[int, tuple[Slot, ...]]) -> None:
        for round_ in ledger.rounds:
            slots = required_slots(ledger, round_)
            HuntPlan(reasons=(), slots=slots)  # constructs: no id twice
            ids = tuple(slot.id for slot in slots)
            self.assertEqual(ids[: len(round_.plan.slots)], tuple(s.id for s in round_.plan.slots))  # type: ignore[union-attr]
            before = stored.get(round_.number, ())
            self.assertEqual(ids[: len(before)] if before else (), before)  # the stored plan only grows
            stored[round_.number] = tuple(slot.id for slot in round_.plan.slots)  # type: ignore[union-attr]
            if round_.number != len(ledger.rounds):
                self.assertEqual(slots, round_.plan.slots)  # type: ignore[union-attr]
                if round_.number in sealed:
                    self.assertEqual(slots, sealed[round_.number])  # immutable since it was sealed
            else:
                plan_slots = round_.plan.slots  # type: ignore[union-attr]
                derived = slots[len(plan_slots) :]
                self.assertEqual(slots[: len(plan_slots)], plan_slots)  # the stored plan, untouched, first
                self.assertFalse({slot.id for slot in derived} & {slot.id for slot in plan_slots})  # derived adds only new ids

    def test_random_sequences_keep_the_invariants(self) -> None:
        self.tally = {"materialized": 0, "sealed": 0}
        for seed in range(self.SEQUENCES):
            with self.subTest(seed=seed):
                self.run_sequence(seed)
        # the sequences must really reach waves, or the invariants above prove nothing
        self.assertGreater(self.tally["materialized"], 100)
        self.assertGreater(self.tally["sealed"], 50)


class FindingJudgedInALaterRoundTests(unittest.TestCase):
    def setUp(self) -> None:
        self.hunks = [code("H1", "a.py"), code("H2", "b.py")]
        ledger = record_finding(run_all(hunt(self.hunks)), path="a.py", lens="failure", verdict=None)
        # a.py is edited and the next round prepared while F1 is still unjudged
        self.ledger, self.round = advance(ledger, [code("H3", "a.py"), self.hunks[1]])

    def test_a_finding_proven_after_the_next_round_opened_still_calls_for_its_wave(self) -> None:
        ledger = judge(self.ledger, "F1", "proven", 6500)
        since = ledger.findings[0].verdict_log[-1].at

        slots = required_slots(ledger, ledger.rounds[1])

        waves = {s.id: s for s in slots if s.generation == 2}
        self.assertEqual(sorted(waves), ["W2-F1", "W2-X1"])
        self.assertEqual((waves["W2-F1"].hunks, waves["W2-F1"].lenses, waves["W2-F1"].since), (("H3",), ("failure",), since))
        self.assertEqual(waves["W2-X1"].hunks, tuple(ref.id for ref in self.round.hunks))
        self.assertEqual(waves["W2-X1"].trigger, "F1")

    def test_it_is_not_required_before_it_is_proven(self) -> None:
        self.assertEqual(required_slots(self.ledger, self.ledger.rounds[1]), self.round.plan.slots)  # type: ignore[union-attr]

    def test_a_past_round_requires_its_stored_plan_only(self) -> None:
        ledger = judge(self.ledger, "F1", "proven", 6500)

        # round 1 is a past round: it no longer follows its findings, the latest round answers them
        self.assertEqual(required_slots(ledger, ledger.rounds[0]), ledger.rounds[0].plan.slots)  # type: ignore[union-attr]
        self.assertEqual(len([s for s in required_slots(ledger, ledger.rounds[1]) if s.generation == 2]), 2)

    def test_a_plan_that_stores_it_ends_the_derivation(self) -> None:
        ledger = judge(self.ledger, "F1", "proven", 6500)
        stored = required_slots(ledger, ledger.rounds[1])
        ledger = replace(ledger, rounds=(ledger.rounds[0], replace(ledger.rounds[1], plan=HuntPlan((), stored))))

        self.assertEqual(required_slots(ledger, ledger.rounds[1]), stored)

    def test_a_variant_stored_in_an_earlier_round_is_not_derived_again(self) -> None:
        ledger = judge(self.ledger, "F1", "proven", 6500)
        variant = next(s for s in required_slots(ledger, ledger.rounds[1]) if s.kind == "variant")
        stored = replace(variant, hunks=("H1",))
        earlier = replace(ledger.rounds[0], plan=HuntPlan((), (*ledger.rounds[0].plan.slots, stored)))  # type: ignore[union-attr]
        ledger = replace(ledger, rounds=(earlier, ledger.rounds[1]))

        self.assertEqual([s.id for s in required_slots(ledger, ledger.rounds[1]) if s.generation == 2], ["W2-X1"])

    def test_a_fresh_set_finished_after_the_finding_was_judged_is_not_derived_again(self) -> None:
        ledger = judge(self.ledger, "F1", "proven", 6500)
        wave = [s.id for s in required_slots(ledger, ledger.rounds[1]) if s.generation == 2]
        ledger = materialize(ledger, 2, wave)
        for index, slot_id in enumerate(wave):
            ledger = run_slot(ledger, slot_id, f"late{index}", 6600 + 10 * index, number=2)
        ledger, third = advance(ledger, [code("H4", "a.py"), self.hunks[1]])

        self.assertEqual([s.id for s in required_slots(ledger, third) if s.generation == 2], [])

    def test_a_fresh_set_finished_before_the_finding_was_judged_is_owed_again(self) -> None:
        # round 1 ran a (static) fresh set long ago; F1 of round 1 is judged only in round 2
        hunks = [code("H1", "a.py"), code("H2", "b.py")]
        ledger = run_all(hunt(hunks, deep=True, first_deep=True))
        ledger = record_finding(ledger, path="a.py", lens="failure", verdict=None)
        ledger, _ = advance(ledger, [code("H3", "a.py"), hunks[1]], deep=True)
        ledger = judge(ledger, "F1", "proven", 6500)

        ids = [s.id for s in required_slots(ledger, ledger.rounds[1]) if s.generation == 2]

        self.assertEqual(sorted(ids), ["W2-F1", "W2-X1"])

    def test_the_next_round_stores_the_wave_once(self) -> None:
        ledger = judge(self.ledger, "F1", "proven", 6500)

        ledger, third = advance(ledger, [code("H4", "a.py"), self.hunks[1]])

        assert third.plan is not None
        self.assertEqual(sorted(s.id for s in third.plan.slots if s.generation == 2), ["W2-F1", "W2-X1"])


class ReviewOfB2Tests(unittest.TestCase):
    """Gaps the review of Phase B2 found: carried waves, representatives across rounds, reasons, idempotence."""

    def test_a_carried_variant_finished_in_its_second_round_is_not_carried_again(self) -> None:
        hunks = [code("H1", "a.py"), code("H2", "b.py")]
        ledger = record_finding(run_all(hunt(hunks)), path="a.py", lens="failure")
        ledger, second = advance(ledger, [code("H3", "a.py"), hunks[1]])
        assert second.plan is not None
        carried = [s.id for s in second.plan.slots if s.generation == 2]
        self.assertIn("W2-F1", carried)
        for index, slot_id in enumerate(carried):
            ledger = run_slot(ledger, slot_id, f"r2-{index}", 7000 + 10 * index, number=2)

        _, third = advance(ledger, [code("H4", "a.py"), hunks[1]])

        assert third.plan is not None
        self.assertEqual([s.id for s in third.plan.slots if s.generation == 2], [])

    def test_a_fresh_set_with_one_slot_left_is_stored_again_whole(self) -> None:
        hunks = [code("H1", "a.py", 300), code("H2", "b.py", 300)]
        ledger = run_all(hunt(hunks, deep=True, first_deep=True), skip=("W2-X2",))

        _, second = advance(ledger, hunks, deep=True)

        assert second.plan is not None
        self.assertEqual([s.id for s in second.plan.slots if s.kind == "fresh"], ["W2-X1", "W2-X2"])

    def test_a_variant_stored_in_an_earlier_round_answers_its_cluster_under_another_id(self) -> None:
        ledger = hunt([code("H1", "a.py")])
        ledger = record_finding(ledger, path="a.py", lens="failure", verdict=None, seconds=5)  # F1
        ledger = record_finding(ledger, path="a.py", lens="failure", verdict="proven", seconds=6)  # F2
        ledger = run_slot(materialize(ledger, 1, ["W2-F2"]), "W2-F2", "a2", 20)
        ledger, _ = advance(ledger, [code("H2", "a.py")])

        ledger = judge(ledger, "F1", "proven", 6500)

        ids = [s.id for s in required_slots(ledger, ledger.rounds[1]) if s.kind == "variant"]
        self.assertEqual(ids, [])  # W2-F2 of round 1 is this cluster's variant: no second one for F1

    def test_a_derived_fresh_set_takes_its_trigger_and_since_from_the_findings_that_owe_it(self) -> None:
        hunks = [code("H1", "a.py"), code("H2", "b.py")]
        ledger = record_finding(hunt(hunks), path="a.py", lens="failure", seconds=5)  # F1
        fresh = [s.id for s in required_slots(ledger, ledger.rounds[0]) if s.kind == "fresh"]
        ledger = materialize(ledger, 1, fresh)
        for index, slot_id in enumerate(fresh):
            ledger = run_slot(ledger, slot_id, f"r1-{index}", 20 + 10 * index)
        ledger, _ = advance(ledger, [code("H3", "a.py"), hunks[1]])
        ledger = record_finding(ledger, path="a.py", lens="intent", seconds=6500)  # F2, owes its own set

        derived = [s for s in required_slots(ledger, ledger.rounds[1]) if s.kind == "fresh"]

        self.assertTrue(derived)
        second = next(item for item in ledger.findings if item.id == "F2")
        for slot in derived:
            self.assertEqual((slot.trigger, slot.since), ("F2", second.verdict_log[0].at))

    def test_a_fresh_set_for_a_finding_of_the_latest_round_spans_its_open_hunks_only(self) -> None:
        hunks = [code("H1", "a.py"), code("H2", "b.py")]
        ledger, round_ = advance(run_all(hunt(hunks)), [hunks[0], code("H3", "c.py")])
        self.assertEqual(round_.open_hunk_ids, ("H3",))

        ledger = record_finding(ledger, path="c.py", lens="failure", seconds=6500)

        fresh = [s for s in required_slots(ledger, ledger.rounds[1]) if s.kind == "fresh"]
        self.assertEqual([hunk for slot in fresh for hunk in slot.hunks], ["H3"])

    def test_sealing_and_materializing_keep_the_reasons_of_the_plan(self) -> None:
        ledger = record_finding(hunt([code("H1")], deep=True), lens="failure")
        reasons = ("requested with --deep",)
        self.assertEqual(ledger.rounds[0].plan.reasons, reasons)  # type: ignore[union-attr]

        self.assertEqual(seal_round(ledger).rounds[0].plan.reasons, reasons)  # type: ignore[union-attr]
        self.assertEqual(materialize(ledger, 1, ["W2-F1"]).rounds[0].plan.reasons, reasons)  # type: ignore[union-attr]

    def test_preparing_again_with_the_pending_slots_already_stored_changes_nothing(self) -> None:
        ledger = materialize(record_finding(hunt([code("H1")]), lens="failure"), 1, ["W2-F1"])
        plan = ledger.rounds[0].plan
        assert plan is not None
        variant = plan.slot("W2-F1")
        assert variant is not None

        again = build_plan(
            [code("H1")],
            {},
            depth=Depth(False, ()),
            round_number=1,
            first_deep_round=False,
            existing=plan,
            pending_wave_slots=[variant],
        )

        self.assertEqual(again.slots, plan.slots)

    def test_materializing_a_fresh_slot_takes_the_set_of_its_generation_only(self) -> None:
        ledger = hunt([code("H1")])
        ledger = record_finding(ledger, hunter="W1-1", lens="intent", seconds=5)  # F1, generation 1
        ledger = record_finding(ledger, hunter="W2-F1", lens="failure", seconds=50)  # F2, generation 2
        ids = {s.id for s in required_slots(ledger, ledger.rounds[0]) if s.kind == "fresh"}
        self.assertEqual(ids, {"W2-X1", "W3-X1"})

        stored = {s.id for s in materialize(ledger, 1, ["W2-X1"]).rounds[0].plan.slots}  # type: ignore[union-attr]

        self.assertIn("W2-X1", stored)
        self.assertNotIn("W3-X1", stored)


if __name__ == "__main__":
    unittest.main()
