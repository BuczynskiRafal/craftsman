"""The hunt planner: depth, required and owed lenses, slicing, slots, waves and carry-over."""

from __future__ import annotations

import random
import unittest
from collections import Counter, defaultdict
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
    _generation_of,
    barrier_passed,
    build_plan,
    changed_lines,
    credited,
    decide_depth,
    finding_generation,
    hunk_kind,
    is_production_hunk,
    is_trigger,
    owed_lenses,
    pending_wave_slots,
    plan_due_waves,
    required_by_hunk,
    required_lenses,
    required_slots,
    signals_by_hunk,
    slice_hunks,
    slot_complete,
    slot_hunks,
    slot_lenses,
    slot_order,
    slots_missing,
    sweep_pending,
    unanswered_triggers,
)
from craftsman.ledger import add_agent_event, add_finding, open_round, stable_hunk_ids
from craftsman.records import (
    Coverage,
    FieldError,
    Finding,
    HunkRef,
    HuntPlan,
    Ledger,
    Round,
    Severity,
    Slot,
    SlotClaim,
    Verdict,
    VerdictEvent,
    Wave,
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
        self.assertEqual(by_id["W1-2"].hunks, ("H1",))  # the sweep rides on it but never grows it onto the prose
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


def run_all(ledger: Ledger, *, skip: Sequence[str] = (), number: int = 1, start: float | None = None) -> Ledger:
    """Every slot of round `number`, except `skip`, covered by its own run (from `start` seconds on)."""
    plan = ledger.rounds[number - 1].plan
    assert plan is not None
    first = 100 + 1000 * number if start is None else start
    for index, slot in enumerate(plan.slots):
        if slot.id not in skip:
            ledger = run_slot(ledger, slot.id, f"a{number}-{index}-{int(first)}", first + 10 * index, number=number)
    return ledger


def run_waves(ledger: Ledger, *, number: int = 1, start: float = 5000, only: Sequence[str] | None = None) -> Ledger:
    """Every wave slot (generation 2 and up) of round `number` covered by its own run, from `start` seconds on."""
    plan = ledger.rounds[number - 1].plan
    assert plan is not None
    for index, slot in enumerate(plan.slots):
        if slot.generation >= 2 and (only is None or slot.id in only):
            ledger = run_slot(ledger, slot.id, f"w{number}-{slot.id}-{int(start)}", start + 10 * index, number=number)
    return ledger


def advance(
    ledger: Ledger,
    hunks: Sequence[Hunk],
    *,
    deep: bool = False,
    first_deep: bool = False,
    seconds: float | None = None,
    waves: bool = True,
) -> tuple[Ledger, Round]:
    """Open the next round for `hunks` as the CLI will: lens-aware carry-over, the plan (pending slots
    included), then the waves that are due (`waves=False` leaves the last step out)."""
    snap = stable_hunk_ids(ledger, make_snap(*hunks, fingerprint=f"fp{len(ledger.rounds) + 2}"))
    required = required_by_hunk(snap.hunks, deep=deep)
    have = credited(ledger, enforced=True)
    carry_sweep = sweep_pending(ledger)
    opened, round_ = open_round(ledger, snap, deep, "p.md", {}, required=required, credited=have)
    prepared_at = 5000 + 1000 * len(ledger.rounds) if seconds is None else seconds
    round_ = replace(round_, prepared_at=at(prepared_at), enforced=True)
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
    opened = replace(opened, rounds=(*opened.rounds[:-1], round_))
    if waves:
        opened = plan_waves(opened, prepared_at + 1)
    return opened, opened.rounds[-1]


def plan_waves(ledger: Ledger, seconds: float = 4000) -> Ledger:
    """The waves that are due at `seconds` after T0 stored in the latest round."""
    return plan_due_waves(ledger, parse_timestamp(at(seconds)))


def wave_ids(ledger: Ledger, number: int = 1, *, generation: int | None = None) -> list[str]:
    """The ids of the stored wave slots (generation 2 and up) of round `number`."""
    plan = ledger.rounds[number - 1].plan
    assert plan is not None
    return [s.id for s in plan.slots if s.generation >= 2 and (generation is None or s.generation == generation)]


def with_slots(ledger: Ledger, *slots: Slot, number: int = 1) -> Ledger:
    """`ledger` with `slots` appended to the stored plan of round `number` (a wave someone planned earlier)."""
    round_ = ledger.rounds[number - 1]
    assert round_.plan is not None
    plan = replace(round_.plan, slots=(*round_.plan.slots, *slots))
    return replace(ledger, rounds=tuple(replace(r, plan=plan) if r.number == number else r for r in ledger.rounds))


def stored_is_required(test: unittest.TestCase, ledger: Ledger) -> None:
    """THE PROPERTY of phase B3: what a round requires is exactly its stored plan, for every round."""
    for round_ in ledger.rounds:
        expected = round_.plan.slots if round_.plan is not None else ()
        test.assertEqual(required_slots(ledger, round_), expected)


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


class FindingGenerationTests(unittest.TestCase):
    def generation(self, ledger: Ledger, index: int = 0) -> int:
        return finding_generation(ledger, ledger.rounds[0], ledger.findings[index], agent_runs(ledger))

    def test_the_stored_slot_a_finding_is_tagged_with_decides(self) -> None:
        ledger = hunt([code("H1")], deep=True, first_deep=True)  # stores W2-X1
        ledger = record_finding(ledger, hunter="W2-X1")

        self.assertEqual(self.generation(ledger), 2)

    def test_a_tag_that_names_no_stored_slot_counts_for_nothing(self) -> None:
        for tag in ("W2-F1", "W3-X1", "W1-9", "lead"):
            with self.subTest(tag=tag):
                ledger = record_finding(hunt([code("H1")]), hunter=tag)

                self.assertEqual(self.generation(ledger), 1)

    def test_the_stored_slot_bound_to_the_recording_run_decides_when_the_tag_is_missing(self) -> None:
        ledger = hunt([code("H1")], deep=True, first_deep=True)
        ledger = run_slot(ledger, "W2-X1", "a2", 20)
        ledger = record_finding(ledger, by=f"{AGY}:a2", seconds=20)

        self.assertEqual(self.generation(ledger), 2)

    def test_the_binding_outranks_a_higher_tag(self) -> None:
        ledger = with_slots(hunt([code("H1")]), make_slot("W3-X1", lenses=("intent",), trigger="F9"))
        ledger = run_slot(ledger, "W1-1", "a1", 20)
        ledger = record_finding(ledger, by=f"{AGY}:a1", seconds=20, hunter="W3-X1")

        self.assertEqual(self.generation(ledger), 1)  # a hunter cannot skip a generation of waves by tagging

    def test_the_binding_outranks_a_lower_tag(self) -> None:
        ledger = hunt([code("H1")], deep=True, first_deep=True)
        ledger = run_slot(ledger, "W2-X1", "a2", 20)
        ledger = record_finding(ledger, by=f"{AGY}:a2", seconds=20, hunter="W1-1")

        self.assertEqual(self.generation(ledger), 2)

    def test_a_binding_to_a_slot_the_plan_does_not_store_is_ignored(self) -> None:
        ledger = run_slot(hunt([code("H1")]), "W2-F1", "a1", 20, only=("H1",))  # tagged with an unstored slot
        ledger = record_finding(ledger, by=f"{AGY}:a1", seconds=20)

        self.assertEqual(bound_slots(ledger, agent_runs(ledger), slot_order(ledger)).get((1, "id:a1")), "W2-F1")
        self.assertEqual(self.generation(ledger), 1)

    def test_an_unattributed_untagged_finding_is_generation_one(self) -> None:
        self.assertEqual(self.generation(record_finding(hunt([code("H1")]))), 1)

    def test_a_finding_of_another_round_is_refused(self) -> None:
        ledger = record_finding(hunt([code("H1")]))
        stranger = replace(ledger.findings[0], round=2)

        with self.assertRaises(ValueError):
            finding_generation(ledger, ledger.rounds[0], stranger, ())

    def test_the_generation_never_exceeds_the_last_one(self) -> None:
        ledger = with_slots(hunt([code("H1")]), make_slot("W3-X1", lenses=("intent",), trigger="F9"))
        ledger = record_finding(ledger, hunter="W3-X1")

        self.assertEqual(self.generation(ledger), 3)


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
        ledger = plan_waves(record_finding(run_all(hunt(hunks)), path="a.py", lens="failure"), 2000)
        since = at(2000)

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
        ledger = plan_waves(record_finding(run_all(hunt(hunks)), path="a.py"), 2000)
        ledger = run_waves(ledger, start=3000)

        self.assertEqual(pending_wave_slots(ledger, open_hunks=[code("H2", "a.py")], round_hunks=[code("H2", "a.py")]), ())

    def test_a_variant_without_open_hunks_follows_the_round_like_the_fresh_set(self) -> None:
        hunks = [code("H1", "a.py")]
        ledger = plan_waves(record_finding(run_all(hunt(hunks)), path="a.py"))

        slots = pending_wave_slots(ledger, open_hunks=[], round_hunks=[code("H1", "a.py")])

        self.assertEqual([(slot.id, slot.hunks) for slot in slots], [("W2-X1", ("H1",)), ("W2-F1", ("H1",))])

    def test_a_variant_is_dropped_only_when_the_round_has_nothing_but_documentation(self) -> None:
        ledger = plan_waves(record_finding(run_all(hunt([code("H1", "a.py")])), path="a.py"))
        readme = make_hunk("H2", "README.md", lines(3))

        slots = pending_wave_slots(ledger, open_hunks=[readme], round_hunks=[readme])

        self.assertEqual(slots, ())

    def test_carried_slots_are_ordered_fresh_before_variants_by_generation(self) -> None:
        ledger = plan_waves(record_finding(run_all(hunt([code("H1", "a.py")])), path="a.py"), 2000)
        ledger = run_waves(ledger, start=3000)
        ledger = record_finding(ledger, path="a.py", lens="failure", hunter="W2-F1", seconds=3500)
        ledger = plan_waves(ledger, 4000)  # the generation 2 finding calls for generation 3
        self.assertEqual(wave_ids(ledger), ["W2-X1", "W2-F1", "W3-X1", "W3-F2"])
        hunk = [code("H9", "a.py")]

        slots = pending_wave_slots(replace(ledger, coverage=()), open_hunks=hunk, round_hunks=hunk)

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
    def test_lenses_and_order_cover_the_stored_slots_only(self) -> None:
        ledger = plan_waves(run_all(record_finding(hunt([code("H1")]))), 2000)
        lenses, order = slot_lenses(ledger), slot_order(ledger)

        self.assertEqual(lenses(1, "W1-1"), ("intent", "ripple", "tests"))
        self.assertEqual(lenses(1, "W2-F1"), ("intent",))
        self.assertIsNone(lenses(1, "W1-9"))
        self.assertIsNone(lenses(2, "W1-1"))
        self.assertEqual(list(order(1)), ["W1-1", "W1-2", "W1-3", "W2-X1", "W2-F1"])
        self.assertEqual(list(order(2)), [])

    def test_nothing_is_adapted_before_the_wave_is_planned(self) -> None:
        ledger = run_all(record_finding(hunt([code("H1")])))

        self.assertIsNone(slot_lenses(ledger)(1, "W2-F1"))
        self.assertEqual(list(slot_order(ledger)(1)), ["W1-1", "W1-2", "W1-3"])
        self.assertIsNone(slot_hunks(ledger)(1, "W2-F1"))

    def test_the_hunks_of_a_slot_cover_the_stored_slots(self) -> None:
        ledger = plan_waves(run_all(record_finding(hunt([code("H1")]))), 2000)
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

    def test_a_wave_slot_that_ran_credits_its_lenses(self) -> None:
        hunk = code("H1")
        ledger = plan_waves(run_all(record_finding(hunt([hunk]), lens="failure")), 2000)
        ledger = run_waves(ledger, start=3000, only=("W2-F1",))

        self.assertIn("failure", credited(ledger)[hunk.digest])

    def test_the_sweep_is_never_credited(self) -> None:
        hunks = [code("H1", "a.py"), code("H2", "b.py")]
        ledger = run_all(hunt(hunks))

        self.assertFalse(sweep_pending(ledger))  # the sweep slot did finish ...
        for digest in credited(ledger).values():
            self.assertNotIn("sweep", digest)  # ... and has no carry-over role


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
        ledger = run_all(self.ledger())
        for lens, severity in (
            ("contracts", "major"),
            ("failure", "major"),
            ("ripple", "major"),
            ("intent", "major"),
            ("intent", "blocker"),
        ):
            ledger = record_finding(ledger, lens=lens, severity=severity)  # type: ignore[arg-type]

        variants = [s.id for s in plan_waves(ledger).rounds[0].plan.slots if s.kind == "variant"]  # type: ignore[union-attr]

        self.assertEqual(variants, ["W2-F4", "W2-F1", "W2-F2"])

    def test_a_wave_is_due_since_the_moment_it_was_planned(self) -> None:
        ledger = record_finding(run_all(self.ledger()), path="a.py", seconds=5)
        ledger = record_finding(ledger, path="a.py", seconds=15)

        slots = plan_waves(ledger, 2500).rounds[0].plan.slots  # type: ignore[union-attr]

        self.assertEqual({slot.since for slot in slots if slot.generation == 2}, {at(2500)})

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
        ledger = plan_waves(record_finding(run_all(hunt([code("H1", "a.py")])), path="a.py"))
        elsewhere = [code("H5", "b.py"), code("H6", "c.py")]

        slots = pending_wave_slots(ledger, open_hunks=elsewhere, round_hunks=elsewhere)

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
        merged_into=found.merged_into if verdict == "dismissed" else None,  # as `set_verdict` un-merges
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
        ledger = run_all(ledger, number=2, start=6100)  # after round 2 was prepared

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
    def test_a_wave_slot_is_completed_by_covering_after_it_was_planned(self) -> None:
        ledger = record_finding(run_all(hunt([code("H1")])), lens="failure", seconds=5)
        ledger = plan_waves(ledger, 100)
        slot = next(s for s in ledger.rounds[0].plan.slots if s.id == "W2-F1")  # type: ignore[union-attr]
        self.assertEqual(slot.since, at(100))
        ledger = run_slot(ledger, "W2-F1", "a1", 50, only=("H1",))  # covered before it was planned

        def complete(current: Ledger) -> bool:
            work = slot_work(current, agent_runs(current), slot_order(current))
            return slot_complete(current.rounds[0], slot, work)

        self.assertFalse(complete(ledger))  # only the early run so far
        ledger = run_slot(ledger, "W2-F1", "a2", 140, only=("H1",))
        self.assertTrue(complete(ledger))

    def test_a_wave_slot_needs_every_hunk_covered_at_or_after_it_was_planned(self) -> None:
        hunks = [code("H1", "a.py"), code("H2", "a.py")]
        ledger = plan_waves(record_finding(run_all(hunt(hunks)), path="a.py", lens="failure", seconds=5), 100)
        slot = next(s for s in ledger.rounds[0].plan.slots if s.id == "W2-F1")  # type: ignore[union-attr]

        def complete(current: Ledger) -> bool:
            work = slot_work(current, agent_runs(current), slot_order(current))
            return slot_complete(current.rounds[0], slot, work)

        ledger = run_slot(ledger, "W2-F1", "a1", 20, only=("H1",))  # H1 before it was planned
        ledger = run_slot(ledger, "W2-F1", "a1b", 140, only=("H2",))  # H2 after it
        self.assertFalse(complete(ledger))
        ledger = run_slot(ledger, "W2-F1", "a1c", 150, only=("H1",))  # H1 again, after it
        self.assertTrue(complete(ledger))

    # finding 4
    def test_binding_uses_the_stored_order(self) -> None:
        ledger = plan_waves(run_all(record_finding(hunt([code("H1")]), lens="failure")), 2000)
        digest = ledger.rounds[0].hunks[0].digest
        for seconds, slot_id in ((2020, "W2-X1"), (2021, "W1-9")):
            ledger = replace(
                ledger,
                coverage=(*ledger.coverage, Coverage(1, (digest,), coverage_note(["H1"]), at(seconds), hunter=slot_id, by=f"{AGY}:a2")),
            )
        ledger = add_agent_event(ledger, agent_event("start", REVIEWER, "a2", 2019, platform=AGY))
        ledger = add_agent_event(ledger, agent_event("stop", REVIEWER, "a2", 2025, platform=AGY))
        ledger = record_finding(ledger, lens="contracts", by=f"{AGY}:a2", hunter="W2-X1", seconds=2022)
        ledger = run_slot(ledger, "W2-F1", "a3", 2040)  # the rest of generation 2 is finished too
        runs = agent_runs(ledger)
        work = slot_work(ledger, runs, slot_order(ledger))
        fresh = ledger.rounds[0].plan.slot("W2-X1")  # type: ignore[union-attr]
        assert fresh is not None

        self.assertTrue(slot_complete(ledger.rounds[0], fresh, work))
        self.assertEqual(finding_generation(ledger, ledger.rounds[0], ledger.findings[1], runs), 2)
        ledger = plan_waves(ledger, 2100)
        ids = wave_ids(ledger)
        self.assertIn("W3-X1", ids)
        self.assertIn("W3-F2", ids)
        self.assertNotIn("W2-F2", ids)  # F2 is a generation 2 finding: it calls for generation 3 only

    def test_two_wave_slots_bind_in_stored_order_not_id_order(self) -> None:
        ledger = plan_waves(run_all(record_finding(hunt([code("H1")]), lens="failure")), 2000)
        ledger = run_slot(ledger, "W2-F1", "a2", 2020)
        ledger = run_slot(ledger, "W2-X1", "a2", 2022)
        ledger = record_finding(ledger, lens="contracts", by=f"{AGY}:a2", seconds=2023)

        runs = agent_runs(ledger)

        self.assertEqual(finding_generation(ledger, ledger.rounds[0], ledger.findings[1], runs), 2)
        self.assertEqual(
            bound_slots(ledger, runs, slot_order(ledger))[(1, "id:a2")], "W2-X1"  # fresh sets come first in plan order
        )

    # finding 5
    def test_an_unjudged_or_wrong_generation_finding_never_plans_a_slot(self) -> None:
        zero = plan_waves(claim(run_all(hunt([code("H1")])), "W2-X1", 10), 2000)
        unjudged = plan_waves(claim(record_finding(run_all(hunt([code("H1")])), lens="failure", verdict=None), "W3-F1", 10), 2000)

        self.assertEqual(wave_ids(zero), [])
        self.assertEqual(wave_ids(unjudged), [])
        for ledger in (zero, unjudged):
            stored_is_required(self, ledger)

    def test_a_generation_three_fresh_set_takes_its_trigger_and_since_from_a_generation_two_finding(self) -> None:
        ledger = record_finding(run_all(hunt([code("H1")])), hunter="W1-1", lens="intent", seconds=5)  # F1: generation 1
        ledger = plan_waves(ledger, 2000)
        ledger = run_waves(ledger, start=3000)
        ledger = record_finding(ledger, hunter="W2-F1", lens="failure", seconds=3500)  # F2: generation 2
        ledger = plan_waves(ledger, 4000)

        w3 = ledger.rounds[0].plan.slot("W3-X1")  # type: ignore[union-attr]
        assert w3 is not None
        self.assertEqual((w3.trigger, w3.since), ("F2", at(4000)))
        ledger = judge(ledger, "F2", "dismissed", 4100)
        self.assertIsNotNone(ledger.rounds[0].plan.slot("W3-X1"))  # type: ignore[union-attr]

    def test_a_plan_never_repeats_an_id(self) -> None:
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

    def test_a_core_slot_hosting_the_sweep_is_done_only_when_it_covered_every_hunk(self) -> None:
        hunks = [code("H1", "a.py"), code("H2", "b.py")]
        ledger = hunt(hunks)
        host = next(s for s in ledger.rounds[0].plan.slots if "sweep" in s.lenses and s.kind == "bug")  # type: ignore[union-attr]
        ledger = run_slot(ledger, host.id, "a1", 20, only=("H1",))
        have = credited(ledger)

        self.assertIn("contracts", have[ledger.rounds[0].hunks[0].digest])
        self.assertNotIn("sweep", have[ledger.rounds[0].hunks[0].digest])
        self.assertEqual(slot_lenses(ledger)(1, host.id), host.lenses)  # the stored lenses are untouched
        self.assertEqual(slots_missing(ledger, ledger.rounds[0])[host.id], ("H2",))
        self.assertTrue(sweep_pending(ledger))

        ledger = run_slot(ledger, host.id, "a2", 30, only=("H2",))

        self.assertNotIn("sweep", credited(ledger)[ledger.rounds[0].hunks[1].digest])  # it has no carry-over role
        self.assertEqual(slots_missing(ledger, ledger.rounds[0])[host.id], ())
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
        ledger = run_all(hunt([code("H1", "a.py")]))
        ledger = record_finding(ledger, path="a.py", lens="failure", verdict="dismissed", seconds=5)  # F1
        ledger = record_finding(ledger, path="a.py", lens="failure", verdict="proven", seconds=6)  # F2
        ledger = plan_waves(ledger, 2000)
        ledger = run_slot(ledger, "W2-F2", "a2", 2020)

        ledger = plan_waves(judge(ledger, "F1", "proven", 2030), 2100)  # F1 joins the cluster F2 already answers

        self.assertEqual([s.id for s in ledger.rounds[0].plan.slots if s.kind == "variant"], ["W2-F2"])  # type: ignore[union-attr]
        self.assertEqual(unanswered_triggers(ledger), ())

    def test_the_cap_applies_to_each_wave_and_the_findings_it_leaves_out_are_decided(self) -> None:
        ledger = run_all(hunt([code("H1")]))
        for lens in ("intent", "contracts", "failure", "ripple"):
            ledger = record_finding(ledger, lens=lens)

        ledger = plan_waves(ledger, 2000)

        self.assertEqual([s.id for s in ledger.rounds[0].plan.slots if s.kind == "variant"], ["W2-F1", "W2-F2", "W2-F3"])  # type: ignore[union-attr]
        self.assertEqual(ledger.rounds[0].plan.waves[0].considered, ("F1", "F2", "F3", "F4"))  # type: ignore[union-attr]
        self.assertEqual(unanswered_triggers(ledger), ())

    # finding 8
    def test_config_suffixes_are_case_insensitive(self) -> None:
        for path in ("ci.YAML", "settings.Yml", "x.TOML", "app.ENV"):
            with self.subTest(path=path):
                self.assertTrue(is_config_path(path))
                self.assertEqual(hunk_kind(code("H1", path)), "config")


# --- waves are planned and stored (phase B3) -------------------------------------------------


def finished(*hunks: Hunk, deep: bool = False, first_deep: bool = False) -> Ledger:
    """Round 1 over `hunks` (default: a.py and b.py) with every slot hunted to the end."""
    chosen = list(hunks) or [code("H1", "a.py"), code("H2", "b.py")]
    return run_all(hunt(chosen, deep=deep, first_deep=first_deep))


class WavePlanningTests(unittest.TestCase):
    def test_with_nothing_due_the_ledger_itself_comes_back(self) -> None:
        ledger = finished()
        bare = record_finding(record_run(prepared(make_snap(code("H1")))[0], kind="repro", exit_code=1))

        self.assertIs(plan_waves(ledger), ledger)
        self.assertIs(plan_waves(bare), bare)
        self.assertEqual(plan_waves(Ledger()), Ledger())

    def test_a_proven_finding_plans_a_fresh_set_and_a_variant_and_records_the_wave(self) -> None:
        ledger = record_finding(finished(), lens="failure", path="a.py")
        before = ledger.rounds[0].plan
        assert before is not None

        planned_ledger = plan_waves(ledger, 2000)

        plan = planned_ledger.rounds[0].plan
        assert plan is not None
        self.assertEqual(plan.slots[: len(before.slots)], before.slots)  # append-only
        fresh, variant = plan.slots[len(before.slots) :]
        self.assertEqual((fresh.id, fresh.kind, fresh.generation, fresh.trigger, fresh.since), ("W2-X1", "fresh", 2, "F1", at(2000)))
        self.assertEqual(fresh.lenses, ("state", "trust", "platform"))  # what the normal plan did not use
        self.assertEqual(fresh.hunks, ("H1", "H2"))
        self.assertEqual(
            (variant.id, variant.kind, variant.lenses, variant.trigger, variant.since, variant.hunks),
            ("W2-F1", "variant", ("failure",), "F1", at(2000), ("H1",)),
        )
        self.assertEqual(plan.waves, (Wave(2, at(2000), ("F1",), ("W2-X1", "W2-F1")),))
        stored_is_required(self, planned_ledger)

    def test_a_variant_of_a_file_without_hunks_covers_all_production_hunks(self) -> None:
        ledger = plan_waves(record_finding(finished(), path="gone.py"), 2000)

        variant = ledger.rounds[0].plan.slot("W2-F1")  # type: ignore[union-attr]

        assert variant is not None
        self.assertEqual(variant.hunks, ("H1", "H2"))

    def test_planning_twice_changes_nothing_the_second_time(self) -> None:
        once = plan_waves(record_finding(finished()), 2000)

        self.assertIs(plan_waves(once, 3000), once)

    def test_a_naive_time_is_taken_as_utc(self) -> None:
        ledger = record_finding(finished())

        naive = plan_due_waves(ledger, parse_timestamp(at(2000)).replace(tzinfo=None))

        self.assertEqual(naive.rounds[0].plan.waves[0].planned_at, at(2000))  # type: ignore[union-attr]

    def test_the_fresh_set_falls_back_to_the_default_lenses_when_the_plan_used_them_all(self) -> None:
        ledger = plan_waves(record_finding(run_all(hunt([code("H1")], deep=True, first_deep=False))), 2000)

        fresh = ledger.rounds[0].plan.slot("W2-X1")  # type: ignore[union-attr]

        assert fresh is not None
        self.assertEqual(fresh.lenses, ("intent", "contracts", "failure"))

    def test_the_fresh_set_is_cut_from_the_hunks_new_len(self) -> None:
        ledger = plan_waves(record_finding(finished(code("H1", "a.py", 5))), 2000)

        fresh = ledger.rounds[0].plan.slot("W2-X1")  # type: ignore[union-attr]

        assert fresh is not None
        self.assertEqual(fresh.changed_lines, ledger.rounds[0].hunks[0].new_len)

    def test_findings_that_do_not_trigger_plan_nothing(self) -> None:
        ledger = finished()
        for kwargs in ({"lens": "craft"}, {"verdict": "dismissed"}, {"severity": "minor", "verdict": "probable"}):
            ledger = record_finding(ledger, **kwargs)  # type: ignore[arg-type]

        self.assertIs(plan_waves(ledger), ledger)

    def test_a_finding_in_documentation_plans_nothing(self) -> None:
        ledger = record_finding(finished(code("H1", "README.md")), path="README.md")

        self.assertIs(plan_waves(ledger), ledger)

    def test_a_merged_duplicate_plans_nothing(self) -> None:
        ledger = record_finding(finished(), verdict="dismissed")
        ledger = record_finding(ledger, path="b.py")
        merged = replace(ledger.findings[0], merged_into="F2")
        ledger = replace(ledger, findings=(merged, ledger.findings[1]))

        planned_ledger = plan_waves(ledger, 2000)

        self.assertEqual(planned_ledger.rounds[0].plan.waves[0].considered, ("F2",))  # type: ignore[union-attr]

    def test_findings_of_one_file_and_lens_share_a_variant_named_after_the_first(self) -> None:
        ledger = record_finding(finished(), path="a.py", severity="major")
        ledger = record_finding(ledger, path="a.py", severity="blocker")
        ledger = record_finding(ledger, path="b.py")

        plan = plan_waves(ledger, 2000).rounds[0].plan
        assert plan is not None

        self.assertEqual(
            [(slot.id, slot.hunks, slot.trigger) for slot in plan.slots if slot.kind == "variant"],
            [("W2-F1", ("H1",), "F1"), ("W2-F3", ("H2",), "F3")],
        )
        self.assertEqual(plan.waves[0].considered, ("F1", "F2", "F3"))

    def test_at_most_three_variants_highest_severity_first_then_finding_number(self) -> None:
        self.assertEqual(MAX_VARIANTS_PER_WAVE, 3)
        ledger = finished()
        for lens, severity in (("intent", "major"), ("contracts", "major"), ("failure", "blocker"), ("ripple", "major"), ("tests", "blocker")):
            ledger = record_finding(ledger, lens=lens, severity=severity)  # type: ignore[arg-type]

        planned_ledger = plan_waves(ledger, 2000)

        plan = planned_ledger.rounds[0].plan
        assert plan is not None
        self.assertEqual([slot.id for slot in plan.slots if slot.kind == "variant"], ["W2-F3", "W2-F5", "W2-F1"])
        self.assertEqual(plan.waves[0].considered, ("F1", "F2", "F3", "F4", "F5"))  # the capped ones are decided too
        self.assertEqual(unanswered_triggers(planned_ledger), ())

    def test_the_static_fresh_set_of_a_deep_round_is_not_planned_again(self) -> None:
        ledger = record_finding(run_all(hunt([code("H1")], deep=True, first_deep=True)))

        plan = plan_waves(ledger, 2000).rounds[0].plan
        assert plan is not None

        self.assertEqual([slot.id for slot in plan.slots if slot.kind == "fresh"], ["W2-X1"])
        self.assertEqual(plan.slot("W2-X1").trigger, "deep")  # type: ignore[union-attr]
        self.assertEqual(plan.waves, (Wave(2, at(2000), ("F1",), ("W2-F1",)),))

    def test_a_lead_or_untagged_finding_is_generation_one(self) -> None:
        for hunter in ("lead", None):
            with self.subTest(hunter=hunter):
                ledger = plan_waves(record_finding(finished(), hunter=hunter), 2000)

                self.assertEqual({slot.generation for slot in ledger.rounds[0].plan.slots if slot.generation > 1}, {2})  # type: ignore[union-attr]

    def test_a_generation_two_finding_calls_for_generation_three_and_the_third_stops(self) -> None:
        ledger = plan_waves(record_finding(finished(), hunter="W1-1"), 2000)
        ledger = run_waves(ledger, start=3000)
        ledger = plan_waves(record_finding(ledger, hunter="W2-F1", lens="failure", seconds=3500), 4000)
        self.assertEqual(wave_ids(ledger), ["W2-X1", "W2-F1", "W3-X1", "W3-F2"])
        ledger = run_waves(ledger, start=5000, only=("W3-X1", "W3-F2"))

        ledger = plan_waves(record_finding(ledger, hunter="W3-F2", lens="ripple", seconds=5500), 6000)

        plan = ledger.rounds[0].plan
        assert plan is not None
        self.assertEqual(wave_ids(ledger), ["W2-X1", "W2-F1", "W3-X1", "W3-F2"])  # nothing of generation 4
        self.assertEqual([(w.generation, w.considered, w.slots) for w in plan.waves][-1], (3, ("F3",), ()))
        self.assertEqual(unanswered_triggers(ledger), ())
        self.assertIs(plan_waves(ledger, 7000), ledger)

    def test_a_round_without_a_plan_requires_no_slots_and_plans_nothing(self) -> None:
        ledger = record_finding(record_run(prepared(make_snap(code("H1")))[0], kind="repro", exit_code=1))

        self.assertEqual(required_slots(ledger, ledger.rounds[0]), ())
        self.assertIs(plan_waves(ledger), ledger)

    def test_findings_of_an_earlier_round_do_not_plan_into_it(self) -> None:
        ledger = record_finding(finished(code("H1", "a.py")), path="a.py")
        later, _ = advance(ledger, [code("H2", "a.py")], waves=False)

        planned_ledger = plan_waves(later, 9000)

        self.assertEqual(planned_ledger.rounds[0], later.rounds[0])


class BarrierTests(unittest.TestCase):
    """A wave of generation g+1 waits until generation g is finished and judged in the latest round."""

    def test_nothing_is_planned_while_a_bug_slot_is_unfinished(self) -> None:
        ledger = record_finding(run_all(hunt([code("H1", "a.py"), code("H2", "b.py")]), skip=("W1-2",)))

        self.assertIs(plan_waves(ledger), ledger)
        self.assertFalse(barrier_passed(ledger, 1))
        self.assertEqual([f.id for f in unanswered_triggers(ledger)], ["F1"])

        ledger = run_slot(ledger, "W1-2", "late", 3000)

        self.assertTrue(barrier_passed(ledger, 1))
        self.assertEqual(wave_ids(plan_waves(ledger, 3500)), ["W2-X1", "W2-F1"])

    def test_a_simplicity_slot_that_never_ran_does_not_hold_the_wave(self) -> None:
        ledger = record_finding(run_all(hunt([code("H1", "a.py"), code("H2", "b.py")]), skip=("W1-3",)))

        self.assertEqual(wave_ids(plan_waves(ledger, 3500)), ["W2-X1", "W2-F1"])

    def test_a_sweep_only_slot_that_never_ran_does_not_hold_the_wave(self) -> None:
        hunks = [code("H1", "a.py", 300), code("H2", "b.py", 300)]
        ledger = hunt(hunks)
        sweep = next(slot for slot in ledger.rounds[0].plan.slots if slot.kind == "sweep")  # type: ignore[union-attr]
        ledger = record_finding(run_all(ledger, skip=(sweep.id,)))

        self.assertTrue(wave_ids(plan_waves(ledger, 3500)))

    def test_an_unjudged_finding_of_the_latest_round_holds_the_wave(self) -> None:
        ledger = record_finding(finished(), lens="failure", seconds=5)
        ledger = record_finding(ledger, lens="intent", verdict=None, seconds=6)

        self.assertIs(plan_waves(ledger), ledger)

        ledger = judge(ledger, "F2", "dismissed", 3000)

        self.assertEqual(wave_ids(plan_waves(ledger, 3500)), ["W2-X1", "W2-F1"])

    def test_the_next_generation_waits_for_the_wave_before_it(self) -> None:
        ledger = plan_waves(record_finding(finished(), hunter="W1-1"), 2000)
        ledger = record_finding(ledger, hunter="W2-F1", lens="failure", seconds=3500)  # the wave has not run

        self.assertIs(plan_waves(ledger, 4000), ledger)
        self.assertTrue(barrier_passed(ledger, 1))
        self.assertFalse(barrier_passed(ledger, 2))

    def test_a_late_trigger_passes_the_barrier_of_its_generation_in_the_latest_round(self) -> None:
        hunks = [code("H1", "a.py"), code("H2", "b.py")]
        ledger = record_finding(finished(*hunks), path="a.py", lens="failure", verdict=None)
        ledger, _ = advance(ledger, [code("H3", "a.py"), hunks[1]])
        ledger = judge(ledger, "F1", "proven", 6500)

        self.assertIs(plan_waves(ledger, 6600), ledger)  # round 2 has not finished its own generation 1

        ledger = plan_waves(run_all(ledger, number=2, start=6700), 7000)

        self.assertEqual(sorted(wave_ids(ledger, 2)), ["W2-F1", "W2-X1"])
        self.assertEqual(wave_ids(ledger, 1), [])
        stored_is_required(self, ledger)

    def test_an_edit_before_the_barrier_moves_the_pending_trigger_to_the_new_round(self) -> None:
        ledger = record_finding(finished(code("H1", "a.py")), path="a.py", lens="failure", verdict=None)
        ledger, _ = advance(ledger, [code("H2", "a.py")])
        ledger = judge(ledger, "F1", "proven", 6500)
        ledger, _ = advance(ledger, [code("H3", "a.py")], waves=False)

        self.assertIs(plan_waves(ledger, 7000), ledger)
        ledger = plan_waves(run_all(ledger, number=3, start=7100), 8000)

        self.assertEqual(wave_ids(ledger, 3), ["W2-X1", "W2-F1"])
        self.assertEqual(wave_ids(ledger, 2), [])


class CarriedAndLateWaveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.hunks = [code("H1", "a.py"), code("H2", "b.py")]

    def test_an_unfinished_wave_moves_to_the_next_round_once_and_keeps_its_identity(self) -> None:
        ledger = plan_waves(record_finding(finished(*self.hunks), path="a.py", lens="failure"), 2000)
        original = {slot.id: slot for slot in ledger.rounds[0].plan.slots if slot.generation == 2}  # type: ignore[union-attr]

        ledger, second = advance(ledger, [code("H3", "a.py"), self.hunks[1]])
        ledger, third = advance(ledger, [code("H4", "a.py"), self.hunks[1]])

        for round_ in (second, third):
            assert round_.plan is not None
            carried = {slot.id: slot for slot in round_.plan.slots if slot.generation == 2}
            self.assertEqual(sorted(carried), ["W2-F1", "W2-X1"])
            self.assertEqual((carried["W2-F1"].trigger, carried["W2-F1"].since), ("F1", at(2000)))
            self.assertEqual((carried["W2-X1"].trigger, carried["W2-X1"].since), (original["W2-X1"].trigger, at(2000)))
            self.assertEqual(round_.plan.waves, ())
        self.assertEqual(unanswered_triggers(ledger), ())  # no round plans F1 a second time
        stored_is_required(self, ledger)

    def test_a_finished_wave_does_not_come_back_in_later_rounds(self) -> None:
        hunks = [code("H1", "a.py", 300), code("H2", "b.py", 300)]
        ledger = record_finding(finished(*hunks), path="a.py", lens="failure", verdict=None)
        ledger, _ = advance(ledger, [hunks[0], code("H3", "b.py", 200)])
        ledger = judge(ledger, "F1", "proven", 6500)
        ledger = plan_waves(run_all(ledger, number=2, start=6600), 7000)
        wave = wave_ids(ledger, 2)
        self.assertEqual(sorted(wave), ["W2-F1", "W2-X1", "W2-X2"])
        ledger = run_waves(ledger, number=2, start=7100)
        second = ledger.rounds[1].plan

        ledger, third = advance(ledger, [hunks[0], code("H4", "b.py", 200)])

        self.assertEqual(ledger.rounds[1].plan, second)  # a past round is an immutable snapshot
        self.assertEqual(wave_ids(ledger, 3), [])
        ledger, fourth = advance(ledger, [hunks[0], code("H5", "b.py", 200)])
        self.assertEqual(wave_ids(ledger, 4), [])

    def test_a_fresh_set_with_one_slot_left_is_stored_again_whole(self) -> None:
        hunks = [code("H1", "a.py", 300), code("H2", "b.py", 300)]
        ledger = run_all(hunt(hunks, deep=True, first_deep=True), skip=("W2-X2",))

        _, second = advance(ledger, hunks, deep=True)

        assert second.plan is not None
        self.assertEqual([s.id for s in second.plan.slots if s.kind == "fresh"], ["W2-X1", "W2-X2"])

    def test_a_planned_slot_stays_required_after_its_trigger_is_dismissed_and_is_carried(self) -> None:
        ledger = plan_waves(record_finding(finished(*self.hunks), path="a.py", lens="failure"), 2000)

        ledger = judge(ledger, "F1", "dismissed", 2100)

        self.assertEqual(wave_ids(plan_waves(ledger, 2200)), ["W2-X1", "W2-F1"])
        ledger, second = advance(ledger, [code("H3", "a.py"), self.hunks[1]])
        self.assertEqual(wave_ids(ledger, 2), ["W2-X1", "W2-F1"])  # unfinished work is not forgiven
        stored_is_required(self, ledger)

    def test_a_flip_to_proven_after_planning_is_a_new_trigger_for_the_next_pass(self) -> None:
        ledger = record_finding(finished(*self.hunks), path="a.py", lens="failure", verdict="probable", severity="minor")
        self.assertIs(plan_waves(ledger), ledger)  # a minor probable finding is no trigger

        ledger = plan_waves(judge(ledger, "F1", "proven", 3000), 3500)

        self.assertEqual(wave_ids(ledger), ["W2-X1", "W2-F1"])

    def test_a_later_trigger_plans_variants_only_when_the_round_stores_the_fresh_set(self) -> None:
        ledger = plan_waves(record_finding(finished(*self.hunks), path="a.py", lens="failure", seconds=5), 2000)
        ledger = record_finding(ledger, path="b.py", lens="contracts", seconds=2100)

        ledger = plan_waves(ledger, 2200)

        plan = ledger.rounds[0].plan
        assert plan is not None
        self.assertEqual(wave_ids(ledger), ["W2-X1", "W2-F1", "W2-F2"])
        self.assertEqual([w.slots for w in plan.waves], [("W2-X1", "W2-F1"), ("W2-F2",)])

    def test_all_carried_hunks_still_get_the_wave_over_the_round(self) -> None:
        ledger = record_finding(finished(*self.hunks), path="a.py", lens="failure", verdict=None)
        ledger, round_ = advance(ledger, [self.hunks[0]])
        self.assertEqual(round_.open_hunk_ids, ())

        ledger = plan_waves(judge(ledger, "F1", "proven", 6500), 7000)

        plan = ledger.rounds[1].plan
        assert plan is not None
        self.assertEqual([(s.id, s.hunks) for s in plan.slots], [("W2-X1", ("H1",)), ("W2-F1", ("H1",))])


class ReviewOfB2Probes(unittest.TestCase):
    """The eight findings of the agy review of Phase B2 (REVIEW-B2), each as a regression test of the stored design."""

    def odd_coverage(self, ledger: Ledger, *slot_ids: str, agent: str = "a1", start: float = 3000) -> Ledger:
        digest = ledger.rounds[0].hunks[0].digest
        for index, slot_id in enumerate(slot_ids):
            record = Coverage(1, (digest,), coverage_note(["H1"]), at(start + index), hunter=slot_id, by=f"{AGY}:{agent}")
            ledger = replace(ledger, coverage=(*ledger.coverage, record))
        ledger = add_agent_event(ledger, agent_event("start", REVIEWER, agent, start - 1, platform=AGY))
        return add_agent_event(ledger, agent_event("stop", REVIEWER, agent, start + 10, platform=AGY))

    # finding 1: oscillation and slots that were never planned
    def test_coverage_tagged_with_slots_that_are_not_stored_cannot_skip_a_generation(self) -> None:
        ledger = self.odd_coverage(finished(code("H1")), "W1-9", "W2-X1")
        ledger = record_finding(ledger, lens="failure", by=f"{AGY}:a1", hunter="W2-X1", seconds=3005)

        self.assertEqual(finding_generation(ledger, ledger.rounds[0], ledger.findings[0], agent_runs(ledger)), 1)

        once = plan_waves(ledger, 4000)

        self.assertEqual(wave_ids(once), ["W2-X1", "W2-F1"])  # generation 2 only: no W3-*
        self.assertIs(plan_waves(once, 5000), once)  # and no oscillation: the answer is stable
        stored_is_required(self, once)

    def test_a_tag_for_a_slot_that_is_not_stored_yet_plans_one_wave_not_two(self) -> None:
        ledger = claim(finished(code("H1", "a.py")), "W1-1", 10)
        ledger = record_finding(ledger, path="a.py", lens="failure", hunter="W2-F1", by=f"{AGY}:a1", seconds=15)

        ledger = plan_waves(ledger, 4000)
        ledger = add_agent_event(ledger, agent_event("start", REVIEWER, "a1", 9, platform=AGY))
        ledger = add_agent_event(ledger, agent_event("stop", REVIEWER, "a1", 20, platform=AGY))
        ledger = plan_waves(ledger, 5000)

        self.assertEqual(wave_ids(ledger), ["W2-X1", "W2-F1"])
        variants = [s for s in ledger.rounds[0].plan.slots if s.kind == "variant"]  # type: ignore[union-attr]
        self.assertEqual([(s.id, s.trigger) for s in variants], [("W2-F1", "F1")])

    # finding 2: the fresh set shrinks or vanishes
    def test_a_finding_of_the_latest_round_does_not_shrink_the_fresh_set_a_late_one_planned(self) -> None:
        h1, h2 = code("H1", "a.py", 300), code("H2", "b.py", 300)
        ledger = record_finding(finished(h1, h2), path="a.py", lens="failure", verdict=None)
        ledger, _ = advance(ledger, [h1, code("H3", "b.py", 300)])
        ledger = judge(ledger, "F1", "proven", 6500)
        ledger = plan_waves(run_all(ledger, number=2, start=6600), 7000)
        fresh = [(s.id, s.hunks, s.trigger) for s in ledger.rounds[1].plan.slots if s.kind == "fresh"]  # type: ignore[union-attr]
        self.assertEqual(fresh, [("W2-X1", ("H3",), "F1"), ("W2-X2", ("H1",), "F1")])

        ledger = record_finding(ledger, path="b.py", lens="contracts", seconds=7100)
        before = ledger.rounds[1].plan
        ledger = plan_waves(ledger, 7500)

        after = ledger.rounds[1].plan
        assert before is not None and after is not None
        self.assertEqual(after.slots[: len(before.slots)], before.slots)
        self.assertEqual([(s.id, s.hunks, s.trigger) for s in after.slots if s.kind == "fresh"], fresh)

    def test_a_generation_two_finding_in_an_all_carried_round_still_plans_generation_three(self) -> None:
        hunks = [code("H1", "a.py"), code("H2", "b.py")]
        ledger = record_finding(finished(*hunks), path="a.py", lens="failure", verdict=None)
        ledger, _ = advance(ledger, [hunks[0]])
        ledger = plan_waves(judge(ledger, "F1", "proven", 6500), 7000)
        self.assertEqual(wave_ids(ledger, 2), ["W2-X1", "W2-F1"])
        ledger = run_waves(ledger, number=2, start=7100)

        ledger = plan_waves(record_finding(ledger, path="a.py", lens="contracts", hunter="W2-X1", seconds=7500), 8000)

        self.assertEqual(wave_ids(ledger, 2), ["W2-X1", "W2-F1", "W3-X1", "W3-F2"])

    # finding 3: a sweep from an earlier round marks the new round's sweep slot done
    def test_the_sweep_of_an_earlier_round_does_not_finish_the_sweep_slot_of_this_one(self) -> None:
        h1, h2 = code("H1", "a.py", 25), code("H2", "b.py", 25)
        ledger, round_ = advance(run_all(hunt([h1, h2])), [h1, h2], deep=True, first_deep=True)
        sweep = next(slot for slot in round_.plan.slots if "sweep" in slot.lenses)  # type: ignore[union-attr]

        statuses = {item.slot.id: item for item in slot_statuses(ledger, round_, parse_timestamp(at(9000)))}

        self.assertTrue(sweep_pending(ledger))
        self.assertEqual(slots_missing(ledger, round_)[sweep.id], sweep.hunks)
        self.assertEqual((statuses[sweep.id].state, statuses[sweep.id].missing), ("unlaunched", sweep.hunks))
        for digest in credited(ledger).values():
            self.assertNotIn("sweep", digest)

    # finding 4: variants cut by the cap leak into later rounds
    def test_triggers_the_cap_left_out_do_not_come_back_in_a_later_round(self) -> None:
        h1, h2 = code("H1", "a.py"), code("H2", "b.py")
        ledger = finished(h1, h2)
        for lens in ("intent", "contracts", "failure", "ripple"):
            ledger = record_finding(ledger, path="a.py", lens=lens)
        ledger = plan_waves(ledger, 2000)
        self.assertEqual([s.id for s in ledger.rounds[0].plan.slots if s.kind == "variant"], ["W2-F1", "W2-F2", "W2-F3"])  # type: ignore[union-attr]
        ledger = run_waves(ledger, start=3000)

        ledger, second = advance(ledger, [h1, code("H3", "b.py")])
        ledger = plan_waves(run_all(ledger, number=2, start=6100), 7000)

        self.assertEqual(wave_ids(ledger, 2), [])
        self.assertNotIn("W2-F4", [s.id for s in ledger.rounds[1].plan.slots])  # type: ignore[union-attr]

    # finding 5: a second variant for the cluster a carried variant already answers
    def test_a_carried_variant_answers_its_cluster_in_the_new_round(self) -> None:
        ledger = plan_waves(record_finding(finished(code("H1", "a.py")), path="a.py", lens="failure"), 2000)
        ledger, second = advance(ledger, [code("H2", "a.py")])
        assert second.plan is not None
        self.assertEqual([s.id for s in second.plan.slots if s.kind == "variant"], ["W2-F1"])

        ledger = record_finding(run_all(ledger, number=2, start=6100), path="a.py", lens="failure", seconds=6500)
        ledger = plan_waves(ledger, 7000)

        variants = [s for s in ledger.rounds[1].plan.slots if s.kind == "variant"]  # type: ignore[union-attr]
        self.assertEqual([(s.id, s.hunks) for s in variants], [("W2-F1", ("H2",))])
        self.assertEqual(ledger.rounds[1].plan.waves[-1].considered, ("F2",))  # type: ignore[union-attr]

    # finding 6: the sweep host grows onto documentation, and a lone carried sweep never gets a slot
    def test_the_sweep_never_grows_a_host_slot_onto_documentation(self) -> None:
        plan = plan_of([code("H1", "a.py", 50), code("H2", "README.md", 10)])

        self.assertEqual(
            shape(plan),
            [
                ("W1-1", ("intent", "ripple", "tests"), ("H1", "H2")),
                ("W1-2", ("contracts", "failure", "sweep"), ("H1",)),
                ("W1-3", ("craft", "design"), ("H1", "H2")),
            ],
        )

    def test_a_carried_sweep_over_a_single_code_hunk_rides_on_the_slot_that_already_lists_it(self) -> None:
        plan = plan_of([code("H3", "a.py", 55), code("H2", "README.md", 10)], round_number=2, carry_sweep=True)

        host = next(slot for slot in plan.slots if "sweep" in slot.lenses)

        self.assertEqual((host.kind, host.lenses, host.hunks), ("bug", ("contracts", "failure", "sweep"), ("H3",)))

    # finding 7: wave slots in the order they were stored
    def test_wave_slots_are_stored_in_generation_order_fresh_before_variants(self) -> None:
        ledger = plan_waves(record_finding(finished(), hunter="W1-1"), 2000)
        ledger = run_waves(ledger, start=3000)
        ledger = plan_waves(record_finding(ledger, hunter="W2-F1", lens="failure", seconds=3500), 4000)

        order = list(slot_order(ledger)(1))

        self.assertEqual(order[3:], ["W2-X1", "W2-F1", "W3-X1", "W3-F2"])

    # finding 8: a dismissed trigger's planned slot is not forgotten
    def test_a_planned_slot_of_a_dismissed_trigger_stays_and_is_carried(self) -> None:
        ledger = plan_waves(record_finding(finished(code("H1", "a.py")), path="a.py", lens="failure"), 2000)
        ledger = judge(ledger, "F1", "dismissed", 2100)

        ledger, second = advance(ledger, [code("H2", "a.py")])
        ledger, third = advance(ledger, [code("H3", "a.py")])

        self.assertIn("W2-F1", [s.id for s in second.plan.slots])  # type: ignore[union-attr]
        self.assertIn("W2-F1", [s.id for s in third.plan.slots])  # type: ignore[union-attr]


class SlotMissingTests(unittest.TestCase):
    def test_a_slot_is_done_when_nothing_is_missing(self) -> None:
        ledger = finished()

        self.assertEqual(set(slots_missing(ledger, ledger.rounds[0]).values()), {()})

    def test_a_round_without_a_plan_misses_nothing(self) -> None:
        ledger = record_run(prepared(make_snap(code("H1")))[0], kind="repro", exit_code=1)

        self.assertEqual(slots_missing(ledger, ledger.rounds[0]), {})

    def test_a_sweep_bearing_slot_names_the_hunks_its_own_coverage_has_not_reached(self) -> None:
        ledger = hunt([code("H1", "a.py"), code("H2", "b.py")])
        host = next(s for s in ledger.rounds[0].plan.slots if "sweep" in s.lenses)  # type: ignore[union-attr]
        ledger = run_slot(ledger, host.id, "a1", 20, only=("H1",))

        self.assertEqual(slots_missing(ledger, ledger.rounds[0])[host.id], ("H2",))

        ledger = run_slot(ledger, host.id, "a2", 30, only=("H2",))

        self.assertEqual(slots_missing(ledger, ledger.rounds[0])[host.id], ())

    def test_lenses_credited_by_other_slots_do_not_finish_the_sweep(self) -> None:
        ledger = hunt([code("H1", "a.py"), code("H2", "b.py")])
        host = next(s for s in ledger.rounds[0].plan.slots if "sweep" in s.lenses)  # type: ignore[union-attr]
        ledger = run_slot(ledger, "W1-1", "other", 20)
        ledger = run_slot(ledger, "W1-3", "third", 30)

        self.assertEqual(slots_missing(ledger, ledger.rounds[0])[host.id], host.hunks)

    def test_a_wave_slot_without_a_trigger_time_counts_from_the_start_of_the_round(self) -> None:
        ledger = run_slot(hunt([code("H1")], deep=True, first_deep=True), "W2-X1", "early", 20)
        ledger = replace(ledger, rounds=(replace(ledger.rounds[0], prepared_at=at(100)),))

        self.assertEqual(slots_missing(ledger, ledger.rounds[0])["W2-X1"], ("H1",))


class StoredPlanPropertyTests(unittest.TestCase):
    def test_every_round_requires_exactly_its_stored_plan(self) -> None:
        ledger = plan_waves(record_finding(finished(), path="a.py"), 2000)
        ledger, _ = advance(ledger, [code("H3", "a.py"), code("H2", "b.py")])
        ledger = judge(ledger, "F1", "dismissed", 6500)
        bare = record_finding(record_run(prepared(make_snap(code("H1")))[0], kind="repro", exit_code=1))

        for current in (ledger, bare, Ledger()):
            stored_is_required(self, current)

    def test_a_wave_must_name_slots_of_its_own_plan(self) -> None:
        with self.assertRaises(FieldError):
            HuntPlan((), (make_slot("W1-1"),), (Wave(2, at(1), ("F1",), ("W2-X1",)),))


class ReviewOfB3Tests(unittest.TestCase):
    """Probes of the B3 review: which hunks a wave reads, what holds the barrier, which trigger wins."""

    def test_an_unjudged_finding_of_an_earlier_round_does_not_hold_the_barrier(self) -> None:
        ledger = record_finding(finished(code("H1", "a.py")), lens="craft", path="a.py", severity="minor", verdict=None)
        ledger, _ = advance(ledger, [code("H2", "a.py")], waves=False)
        ledger = run_all(ledger, number=2, start=6700)
        ledger = record_finding(ledger, lens="failure", path="a.py", seconds=6900)

        ledger = plan_waves(ledger, 7000)

        self.assertEqual(sorted(wave_ids(ledger, 2)), ["W2-F2", "W2-X1"])
        stored_is_required(self, ledger)

    def test_the_fresh_set_of_a_wave_reads_only_the_open_hunks_when_the_trigger_is_new(self) -> None:
        hunks = [code("H1", "a.py"), code("H2", "b.py")]
        ledger, _ = advance(finished(*hunks), [hunks[0], code("H3", "b.py")], waves=False)  # H1 is carried
        ledger = run_all(ledger, number=2, start=6700)
        ledger = record_finding(ledger, lens="failure", path="b.py", seconds=6900)

        plan = plan_waves(ledger, 7000).rounds[1].plan

        assert plan is not None
        fresh = plan.slot("W2-X1")
        assert fresh is not None
        self.assertEqual(fresh.hunks, ("H3",))

    def test_a_variant_reads_only_the_open_hunks_of_its_file(self) -> None:
        hunks = [code("H1", "a.py"), code("H2", "a.py"), code("H3", "b.py")]
        ledger, _ = advance(finished(*hunks), [hunks[0], code("H4", "a.py"), code("H5", "b.py")], waves=False)
        ledger = run_all(ledger, number=2, start=6700)
        ledger = record_finding(ledger, lens="failure", path="a.py", seconds=6900)

        plan = plan_waves(ledger, 7000).rounds[1].plan

        assert plan is not None
        variant = plan.slot("W2-F1")
        assert variant is not None
        self.assertEqual(variant.hunks, ("H4",))

    def test_a_wave_leaves_the_documentation_hunks_out(self) -> None:
        readme = make_hunk("H2", "README.md", lines(3))
        ledger = plan_waves(record_finding(finished(code("H1", "a.py"), readme), path="a.py"), 2000)

        plan = ledger.rounds[0].plan

        assert plan is not None
        self.assertEqual([(s.id, s.hunks) for s in plan.slots if s.generation == 2], [("W2-X1", ("H1",)), ("W2-F1", ("H1",))])

    def test_a_carried_wave_leaves_the_documentation_hunks_out(self) -> None:
        readme = make_hunk("H4", "README.md", lines(3))
        ledger = plan_waves(record_finding(finished(code("H1", "a.py")), path="a.py"), 2000)
        open_hunks = [code("H3", "a.py"), readme]

        slots = pending_wave_slots(ledger, open_hunks=open_hunks, round_hunks=open_hunks)

        self.assertEqual([(s.id, s.hunks) for s in slots], [("W2-X1", ("H3",)), ("W2-F1", ("H3",))])

    def test_a_carried_variant_keeps_the_code_when_the_new_round_changes_only_documentation(self) -> None:
        readme = make_hunk("H2", "README.md", lines(3))
        ledger = plan_waves(record_finding(finished(code("H1", "a.py")), path="a.py", lens="failure"), 2000)

        ledger, round_ = advance(ledger, [code("H1", "a.py"), readme], waves=False)

        assert round_.plan is not None
        variant = round_.plan.slot("W2-F1")
        assert variant is not None
        self.assertEqual(variant.hunks, ("H1",))
        self.assertTrue(all("H2" not in s.hunks for s in round_.plan.slots if s.generation >= 2))

    def test_a_carried_variant_is_rebuilt_over_the_open_hunks_of_its_own_file(self) -> None:
        ledger = plan_waves(record_finding(finished(code("H1", "a.py")), path="a.py"), 2000)
        elsewhere = [code("H3", "a.py"), code("H4", "b.py")]

        slots = pending_wave_slots(ledger, open_hunks=elsewhere, round_hunks=elsewhere)

        variant = next(slot for slot in slots if slot.id == "W2-F1")
        self.assertEqual(variant.hunks, ("H3",))

    def test_the_fresh_set_is_triggered_by_the_lowest_finding(self) -> None:
        ledger = record_finding(finished(), lens="failure", path="a.py")
        ledger = record_finding(ledger, lens="contracts", path="b.py")

        plan = plan_waves(ledger, 2000).rounds[0].plan

        assert plan is not None
        fresh = plan.slot("W2-X1")
        assert fresh is not None
        self.assertEqual(fresh.trigger, "F1")
        self.assertEqual(plan.waves[0].considered, ("F1", "F2"))

    def test_the_lowest_due_generation_is_planned_first(self) -> None:
        ledger = plan_waves(record_finding(finished(), lens="failure", path="a.py"), 2000)
        ledger = run_waves(ledger, start=3000)
        ledger = record_finding(ledger, lens="failure", path="a.py", hunter="W2-F1", seconds=3500)  # generation 2
        ledger = record_finding(ledger, lens="contracts", path="b.py", hunter="W1-1", seconds=3500)  # late generation 1

        plan = plan_waves(ledger, 4000).rounds[0].plan

        assert plan is not None
        self.assertEqual([wave.generation for wave in plan.waves], [2, 2])  # W3 waits for the new W2 variant
        self.assertEqual(wave_ids(plan_waves(ledger, 4000), generation=3), [])

    def test_a_finding_bound_to_several_stored_slots_takes_the_highest_generation(self) -> None:
        item = record_finding(hunt([code("H1")]), hunter=None).findings[0]
        stored = frozenset({"W1-1", "W2-F1"})

        self.assertEqual(_generation_of(item, frozenset({"W1-1", "W2-F1"}), stored), 2)
        self.assertEqual(_generation_of(item, frozenset({"W2-F1", "W1-1", "W9-9"}), stored), 2)


class RandomSequenceTests(unittest.TestCase):
    """Whatever order prepare, hunters, verdicts and edits come in, the stored plan is the whole truth."""

    SEQUENCES = 320
    STEPS = 18
    PATHS = ("a.py", "b.py", "c.py")
    LENSES = ("failure", "contracts", "intent", "ripple", "craft")

    def run_sequence(self, seed: int) -> None:
        rng = random.Random(seed)
        counter = 0

        def new_hunk(path: str) -> Hunk:
            nonlocal counter
            counter += 1
            return code(f"H{counter}", path, rng.choice((5, 40, 150, 300)))

        hunks = [new_hunk(path) for path in self.PATHS[: rng.randint(1, 3)]]
        ledger = hunt(hunks)
        clock = 10.0
        seen: dict[int, tuple[str, ...]] = {}
        for step in range(self.STEPS):
            clock += 600
            action = rng.choice(
                ("finish", "finish", "finding", "finding", "tagged", "judge", "claim", "merge", "edit", "deep", "late",
                 "waves", "waves", "wave finding", "wave finding", "last wave finding", "last wave finding")
            )  # fmt: skip
            latest = ledger.rounds[-1]
            plan = latest.plan
            assert plan is not None
            if action == "finish":
                for index, slot in enumerate(plan.slots):
                    if rng.random() < 0.85:
                        ledger = run_slot(ledger, slot.id, f"s{seed}-{step}-{index}", clock + 10 * index, number=latest.number)
            elif action == "waves":
                ledger = run_waves(ledger, number=latest.number, start=clock + 1, only=[s.id for s in plan.slots if rng.random() < 0.9])
            elif action in ("wave finding", "last wave finding") and any(s.generation >= 2 for s in plan.slots):
                floor = 3 if action == "last wave finding" else 2
                tagged = [s.id for s in plan.slots if s.generation >= floor] or [s.id for s in plan.slots if s.generation >= 2]
                ledger = record_finding(
                    ledger,
                    lens=rng.choice(self.LENSES),
                    path=rng.choice(self.PATHS),
                    severity=rng.choice(("major", "blocker")),
                    verdict=rng.choice(("proven", "probable")),
                    hunter=rng.choice(tagged),
                    seconds=clock,
                )
            elif action in ("finding", "tagged"):
                hunter = rng.choice(plan.slots).id if action == "tagged" and plan.slots else None
                ledger = record_finding(
                    ledger,
                    lens=rng.choice(self.LENSES),
                    path=rng.choice(self.PATHS),
                    severity=rng.choice(("minor", "major", "blocker")),
                    verdict=rng.choice((None, "proven", "probable", "dismissed")),
                    hunter=hunter,
                    seconds=clock,
                )
            elif action in ("judge", "late") and ledger.findings:
                pool = [f for f in ledger.findings if action == "judge" or f.round < latest.number] or list(ledger.findings)
                ledger = judge(ledger, rng.choice(pool).id, rng.choice(("proven", "probable", "dismissed")), clock)
            elif action == "claim" and plan.slots:
                ledger = claim(ledger, rng.choice(plan.slots).id, clock, number=latest.number)
            elif action == "merge" and len(ledger.findings) >= 2:
                target, duplicate = rng.sample(ledger.findings, 2)
                if target.merged_into is None and duplicate.merged_into is None and not any(
                    f.merged_into == duplicate.id for f in ledger.findings
                ):
                    token = f"V{1 + sum(len(f.verdict_log) for f in ledger.findings)}"
                    merged = replace(
                        duplicate, merged_into=target.id, verdict="dismissed", verdict_run=None,
                        verdict_evidence=STATIC_EVIDENCE,
                        verdict_log=(*duplicate.verdict_log, VerdictEvent(token, "dismissed", at(clock))),
                    )  # fmt: skip
                    ledger = replace(ledger, findings=tuple(merged if f.id == duplicate.id else f for f in ledger.findings))
            elif action in ("edit", "deep"):
                hunks = [hunk for hunk in hunks if rng.random() < 0.7] or [new_hunk(rng.choice(self.PATHS))]
                if rng.random() < 0.7 or action == "deep":
                    hunks.append(new_hunk(rng.choice(self.PATHS)))
                deep = action == "deep" or any(r.deep for r in ledger.rounds)
                first = deep and not any(r.deep for r in ledger.rounds)
                ledger, _ = advance(ledger, hunks, deep=deep, first_deep=first, seconds=clock + 300, waves=False)
            before = ledger
            ledger = plan_waves(ledger, clock + 400)
            self.check(before, ledger, seen)

    def check(self, before: Ledger, ledger: Ledger, seen: dict[int, tuple[str, ...]]) -> None:
        stored_is_required(self, ledger)
        self.assertIs(plan_waves(ledger, 99999), ledger)  # planning twice is planning once
        for round_ in ledger.rounds:
            assert round_.plan is not None
            ids = tuple(slot.id for slot in round_.plan.slots)
            HuntPlan(round_.plan.reasons, round_.plan.slots, round_.plan.waves)  # constructs: unique ids, waves resolve
            known = seen.get(round_.number, ())
            self.assertEqual(ids[: len(known)], known)  # plans only grow
            seen[round_.number] = ids
            old = before.rounds[round_.number - 1].plan if round_.number <= len(before.rounds) else None
            self.tally["waves"] += len(round_.plan.waves) - (len(old.waves) if old is not None else 0)
            for slot in round_.plan.slots:
                self.reached[f"generation {slot.generation}"].add(self.seed)
            if any(w.generation == 3 and not w.slots for w in round_.plan.waves):
                self.reached["last generation decided"].add(self.seed)
        runs = agent_runs(ledger)
        for finding in unanswered_triggers(ledger):
            generation = finding_generation(ledger, ledger.rounds[finding.round - 1], finding, runs)
            self.assertFalse(barrier_passed(ledger, generation), f"{finding.id} is due but unanswered")
        considered = [fid for r in ledger.rounds for w in r.plan.waves for fid in w.considered]  # type: ignore[union-attr]
        self.assertEqual(len(considered), len(set(considered)))  # a finding is answered once

    def test_random_sequences_keep_the_invariants(self) -> None:
        self.tally: Counter[str] = Counter()
        self.reached: defaultdict[str, set[int]] = defaultdict(set)
        for seed in range(self.SEQUENCES):
            self.seed = seed
            with self.subTest(seed=seed):
                self.run_sequence(seed)
        # the sequences must really reach waves of every generation, or the invariants above prove nothing
        self.assertGreater(self.tally["waves"], 100)
        self.assertGreater(len(self.reached["generation 2"]), 100)
        self.assertGreater(len(self.reached["generation 3"]), 30)
        self.assertGreater(len(self.reached["last generation decided"]), 5)


if __name__ == "__main__":
    unittest.main()
