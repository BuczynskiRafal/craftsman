"""Tests for the review packet: context collection and Markdown rendering."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from craftsman import packet
from craftsman.diffscan import (
    DiffSnapshot,
    Hunk,
    Reference,
    Scope,
    WorktreeIndex,
    resolve_base_sha,
    snapshot,
)
from craftsman.lenses import select_lenses
from craftsman.packet import (
    PacketContext,
    collect_context,
    expand_hunk_spec,
    render_packet,
    render_packet_view,
)
from craftsman.records import FieldError, HunkRef, Ledger, Round

from tests.support import GitRepo, record_run

PREPARED_AT = "2026-01-01T00:00:00.000Z"
TWO_FUNCTIONS = (
    "def foo():\n    a = 1\n    return a\ndef bar():\n    x = 1\n    y = 2\n    return x + y\n"
)
H1 = Hunk("H1", "pkg/a.py", None, "modified", 2, 1, 2, 1, "", ("-a = 1", "+a = 2"), "d1")
H2 = Hunk("H2", "pkg/b.py", None, "modified", 8, 1, 8, 1, "", ("-b = 1", "+b = 2"), "d2")


def round_for(snap: DiffSnapshot, *, carried: frozenset[str] = frozenset()) -> Round:
    return Round(
        number=2,
        fingerprint=snap.fingerprint,
        base_sha=snap.base_sha,
        tier="standard",
        prepared_at=PREPARED_AT,
        hunks=tuple(
            HunkRef(hunk.id, hunk.path, hunk.digest, hunk.new_start, hunk.new_len, hunk.id in carried)
            for hunk in snap.hunks
        ),
        lenses=("intent", "sweep"),
        packet_path="packet-2.md",
        signals=(),
    )


def picks() -> tuple[Any, ...]:
    return tuple(
        pick
        for pick in select_lenses("standard", {}, docs_only=False)
        if pick.lens.id in ("intent", "sweep")
    )


def build_context(*hunks: Hunk, **overrides: Any) -> PacketContext:
    """A context for `hunks` (default: H1) with no scopes, symbols or references unless given."""
    shown = hunks or (H1,)
    snap = DiffSnapshot("b" * 40, "f" * 64, shown, tuple(dict.fromkeys(h.path for h in shown)), (), 1, 1)
    fields: dict[str, Any] = {
        "snap": snap,
        "round": round_for(snap, carried=frozenset(overrides.get("carried_from", {}))),
        "picks": picks(),
        "task": "",
        "commits": (),
        "conventions": (),
        "scopes": {},
        "symbols": {},
        "references": {},
        "signals_by_hunk": {},
        "carried_from": {},
        "reopened": {},
        "test_files_by_symbol": {},
        "last_runs": (),
        "facts_by_hunk": {},
    }
    return PacketContext(**{**fields, **overrides})


class CollectContextTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = GitRepo(Path(tmp.name).resolve())

    def context(self, base: str, ledger: Ledger | None = None) -> PacketContext:
        snap = snapshot(self.repo.root, base)
        index = WorktreeIndex(self.repo.root)
        symbols = {hunk.id: index.symbols(hunk) for hunk in snap.hunks}
        return collect_context(
            self.repo.root,
            snap,
            round_for(snap),
            picks(),
            "task",
            symbols=symbols,
            ledger=ledger or Ledger(),
            index=index,
        )

    def edit_compute_total(self) -> str:
        self.repo.write("calc.py", "def compute_total(items):\n    return sum(items)\n")
        base = self.repo.commit()
        self.repo.write("calc.py", "def compute_total(items):\n    return sum(items) * 2\n")
        return base

    def test_pure_deletion_shows_the_function_the_lines_were_removed_from(self) -> None:
        self.repo.write("mod.py", TWO_FUNCTIONS)
        base = self.repo.commit()
        self.repo.write("mod.py", TWO_FUNCTIONS.replace("    x = 1\n", ""))

        scope = self.context(base).scopes["H1"]

        assert scope is not None
        self.assertEqual((scope.name, scope.start, scope.end), ("bar", 4, 6))

    def test_rename_without_content_changes_has_no_scope(self) -> None:
        self.repo.write("mod.py", TWO_FUNCTIONS)
        base = self.repo.commit()
        self.repo.git("mv", "mod.py", "moved.py")

        ctx = self.context(base)

        self.assertIsNone(ctx.scopes["H1"])
        self.assertNotIn("Enclosing scope", render_packet(ctx))

    @unittest.skipIf(os.name == "nt", "the executable bit is not tracked on Windows checkouts")
    def test_mode_change_next_to_content_edits_is_rendered(self) -> None:
        script = self.repo.write("run.sh", "echo one\n")
        base = self.repo.commit()
        self.repo.write("run.sh", "echo two\n")
        script.chmod(0o755)

        text = render_packet(self.context(base))

        self.assertIn("@@ -1,1 +1,1 @@\n~ mode 100644 -> 100755\n-echo one\n+echo two\n", text)

    def test_no_commits_are_listed_before_the_first_commit(self) -> None:
        self.repo.write("app.py", "x = 1\n")

        self.assertEqual(self.context(resolve_base_sha(self.repo.root, None)).commits, ())

    def test_commits_since_the_base_are_listed(self) -> None:
        self.repo.write("app.py", "x = 1\n")
        base = resolve_base_sha(self.repo.root, None)
        self.repo.commit("first")

        self.assertEqual([line.split(" ", 1)[1] for line in self.context(base).commits], ["first"])

    def test_orphan_branch_without_commits_lists_no_commits(self) -> None:
        self.repo.write("app.py", "x = 1\n")
        base = self.repo.commit("first")
        self.repo.git("checkout", "-q", "--orphan", "fresh")
        self.repo.write("app.py", "x = 2\n")

        self.assertEqual(self.context(base).commits, ())

    def test_references_prefer_files_other_than_the_definition(self) -> None:
        defining = "def compute_total(items):\n    return sum(items)\n" + "".join(
            f"compute_total([{index}])\n" for index in range(10)
        )
        self.repo.write("calc.py", defining)
        self.repo.write("use.py", "from calc import compute_total\n")
        base = self.repo.commit()
        self.repo.write("calc.py", defining.replace("sum(items)", "sum(items) * 2"))

        ctx = self.context(base)

        self.assertEqual(ctx.references["compute_total"][0].path, "use.py")

    def test_tests_referencing_changed_symbol_are_listed(self) -> None:
        self.repo.write("tests/test_calc.py", "from calc import compute_total\n")
        base = self.edit_compute_total()

        text = render_packet(self.context(base))

        self.assertIn("Tests referencing `compute_total`: `tests/test_calc.py`", text)

    def test_tests_are_found_behind_many_production_callers(self) -> None:
        for index in range(12):
            self.repo.write(f"a{index:02}.py", "from calc import compute_total\n" * 6)
        self.repo.write("tests/test_calc.py", "from calc import compute_total\n")
        base = self.edit_compute_total()

        self.assertEqual(
            self.context(base).test_files_by_symbol["compute_total"], ("tests/test_calc.py",)
        )

    def test_last_test_run_is_shown_with_staleness(self) -> None:
        base = self.edit_compute_total()

        text = render_packet(self.context(base, record_run(Ledger(), fingerprint="stale_fp")))

        self.assertIn("Last runs: R1 `pytest -q` (test) exit 0 (stale)", text)

    def test_contributing_guides_count_as_conventions(self) -> None:
        for path in (
            "CONTRIBUTING.rst",
            "CONTRIBUTING.txt",
            ".github/CONTRIBUTING.md",
            "docs/CONTRIBUTING.rst",
            "changelog/README.rst",
        ):
            self.repo.write(path, "How to contribute.\n")
        self.repo.write("app.py", "x = 1\n")
        base = self.repo.commit()
        self.repo.write("app.py", "x = 2\n")

        self.assertEqual(
            self.context(base).conventions,
            (
                "CONTRIBUTING.rst",
                "CONTRIBUTING.txt",
                ".github/CONTRIBUTING.md",
                "docs/CONTRIBUTING.rst",
                "changelog/README.rst",
            ),
        )

    def test_long_function_is_shown_around_a_hunk_in_its_middle(self) -> None:
        body = "".join(f"    line_{index} = {index}\n" for index in range(2, 301))
        self.repo.write("big.py", "def big(x):\n" + body)
        base = self.repo.commit()
        self.repo.write("big.py", "def big(x):\n" + body.replace("line_150 = 150", "line_150 = -150"))

        text = render_packet(self.context(base))

        self.assertIn("    line_150 = -150\n    line_151 = 151", text)
        self.assertIn("def big(x):\n[… L2–L131 omitted …]\n    line_132 = 132", text)
        self.assertIn("[… L163–L300 omitted …]", text)


class RenderTests(unittest.TestCase):
    def test_a_scope_whose_omission_saves_nothing_is_kept(self) -> None:
        path = "pkg/" + "deeply/nested/" * 6 + "a.py"
        hunk = Hunk("H1", path, None, "modified", 1, 1, 1, 1, "", ("-x = 0", "+x = 1"), "d1")
        ctx = build_context(hunk, scopes={"H1": Scope(path, "f", "function", 1, 1, "x = 1")})

        with mock.patch.object(packet, "MAX_PACKET_CHARS", 100):
            text = render_packet(ctx)

        self.assertIn("Enclosing scope (L1–L1):\n```python\nx = 1\n```", text)

    def test_size_guard_drops_enclosing_scopes(self) -> None:
        scope = Scope("pkg/a.py", "f", "function", 1, 50, "\n".join(f"line {i}" for i in range(50)))
        ctx = build_context(scopes={"H1": scope})

        with mock.patch.object(packet, "MAX_PACKET_CHARS", 3000):
            text = render_packet(ctx)

        self.assertIn("Enclosing scope omitted for size — open pkg/a.py:L1–L50", text)

    def test_reference_text_with_backticks_keeps_its_code_span(self) -> None:
        references = {
            "helper_fn": (
                Reference("web/app.js", 3, "msg = `hi ${helper_fn()}`"),
                Reference("web/b.js", 7, "x = helper_fn() ``y``"),
            )
        }

        text = render_packet(build_context(symbols={"H1": ("helper_fn",)}, references=references))

        self.assertIn(
            "- `helper_fn`: `web/app.js:3` `` msg = `hi ${helper_fn()}` `` · "
            "`web/b.js:7` ``` x = helper_fn() ``y`` ```",
            text,
        )

    def test_scope_shared_by_two_hunks_is_rendered_once(self) -> None:
        second = Hunk("H2", "pkg/a.py", None, "modified", 8, 1, 8, 1, "", ("-b = 1", "+b = 2"), "d2")
        scope = Scope("pkg/a.py", "compute", "function", 1, 10, "def compute():\n    a = 2\n    b = 2")

        text = render_packet(build_context(H1, second, scopes={"H1": scope, "H2": scope}))

        self.assertEqual(text.count("Enclosing scope (L1–L10):"), 1)
        self.assertIn("Scope: `compute` — shown at H1", text)

    def test_long_scope_omits_lines_with_a_language_neutral_marker(self) -> None:
        lines = ["func long() {"] + [f"    v{i} := {i}" for i in range(2, 82)]
        hunk = Hunk(
            "H1",
            "pkg/a.go",
            None,
            "modified",
            40,
            1,
            40,
            1,
            "",
            ("-    v40 := 0", "+    v40 := 40"),
            "d1",
        )
        scope = Scope("pkg/a.go", "long", "function", 1, 81, "\n".join(lines))

        text = render_packet(build_context(hunk, scopes={"H1": scope}))

        self.assertIn("func long() {\n[… L2–L24 omitted …]\n    v25 := 25", text)
        self.assertNotIn("# ...", text)

    def test_view_renders_assigned_hunks_in_full(self) -> None:
        scopes = {
            "H1": Scope("pkg/a.py", "alpha", "function", 1, 4, "def alpha():\n    a = 2"),
            "H2": Scope("pkg/b.py", "beta", "function", 7, 10, "def beta():\n    b = 2"),
        }

        view = render_packet_view(
            build_context(H1, H2, scopes=scopes), lenses=("intent",), assigned_hunks=("H1",)
        )

        self.assertIn("def alpha():", view)
        self.assertNotIn("def beta():", view)
        self.assertIn("- H2 · pkg/b.py · beta", view)

    def test_view_gets_its_hunks_share_of_the_size_budget(self) -> None:
        scope = Scope("pkg/a.py", "f", "function", 1, 50, "\n".join(f"line {i}" for i in range(50)))
        ctx = build_context(H1, H2, scopes={"H1": scope})
        full = len(render_packet(ctx))

        with mock.patch.object(packet, "MAX_PACKET_CHARS", full):
            view = render_packet_view(ctx, lenses=("intent",), assigned_hunks=("H1",))
            whole = render_packet(ctx)

        self.assertIn("Enclosing scope omitted for size", view)
        self.assertNotIn("Enclosing scope omitted for size", whole)

    def test_view_lists_only_its_lenses(self) -> None:
        view = render_packet_view(build_context(), lenses=("intent",), assigned_hunks=("H1",))

        self.assertNotIn("`sweep`", view.split("## Recording cheat sheet")[0])


class LayoutTests(unittest.TestCase):
    def context(self) -> PacketContext:
        first = Hunk(
            "H1", "pkg/a.py", None, "modified", 1, 1, 1, 2, "def f():", (" x = 1", "+y = ```"), "d1"
        )
        carried = Hunk("H2", "pkg/b.py", None, "modified", 4, 1, 4, 1, "", ("-a", "+b"), "d2")
        return build_context(
            first,
            carried,
            commits=("abc1234 feat: add f",),
            conventions=("AGENTS.md",),
            signals_by_hunk={"H1": ("errors",)},
            carried_from={"H2": 1},
        )

    def test_sections_appear_in_order(self) -> None:
        headings = [line for line in render_packet(self.context()).splitlines() if line.startswith("#")]

        self.assertEqual(
            headings[:7],
            [
                "# Craftsman review packet — round 2",
                "## Intent",
                "## Conventions to read first",
                "## Lenses in this round",
                "## Recording cheat sheet",
                "## Hunks",
                "### H1 · `pkg/a.py` L1–L2 · modified · signals: errors",
            ],
        )

    def test_carried_hunk_is_listed_without_its_diff(self) -> None:
        text = render_packet(self.context())

        self.assertIn("### H2 · `pkg/b.py` L4–L4 · carried (covered in round 1, unchanged)", text)

    def test_diff_fence_is_longer_than_backticks_in_the_hunk(self) -> None:
        text = render_packet(self.context())

        self.assertIn("````diff\n@@ -1,1 +1,2 @@ def f():\n x = 1\n+y = ```\n````", text)


class HunkSpecTests(unittest.TestCase):
    IDS = ("H1", "H2", "H3", "H5")

    def test_hunk_spec_expands_in_order_without_repeats(self) -> None:
        self.assertEqual(expand_hunk_spec("H3, H1-H3", self.IDS), ("H3", "H1", "H2"))

    def test_range_skips_ids_missing_from_the_round(self) -> None:
        self.assertEqual(expand_hunk_spec("H3-H5", self.IDS), ("H3", "H5"))

    def test_invalid_specs_are_rejected(self) -> None:
        for raw, message in (
            ("H3-H1", "invalid hunk range 'H3-H1'"),
            ("H6-H9", "range 'H6-H9' matches no hunks"),
            ("H4", "unknown hunk 'H4'"),
            (" , ", "must name at least one hunk"),
        ):
            with self.subTest(raw=raw), self.assertRaisesRegex(FieldError, message):
                expand_hunk_spec(raw, self.IDS)


if __name__ == "__main__":
    unittest.main()
