"""Tests for the review packet: context collection and Markdown rendering."""

from __future__ import annotations

import os
import tempfile
import unittest
from dataclasses import replace
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
from craftsman.packet import (
    PacketContext,
    collect_context,
    expand_hunk_spec,
    render_packet,
    render_packet_view,
)
from craftsman.records import FieldError, HunkRef, Ledger, QualitySignal, Round, SignalKind

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
        deep=False,
        prepared_at=PREPARED_AT,
        hunks=tuple(
            HunkRef(hunk.id, hunk.path, hunk.digest, hunk.new_start, hunk.new_len, hunk.id in carried)
            for hunk in snap.hunks
        ),
        packet_path="packet-2.md",
        signals=(),
    )


def build_context(*hunks: Hunk, **overrides: Any) -> PacketContext:
    """A context for `hunks` (default: H1) with no scopes, symbols or references unless given."""
    shown = hunks or (H1,)
    snap = DiffSnapshot("b" * 40, "f" * 64, shown, tuple(dict.fromkeys(h.path for h in shown)), 1, 1)
    fields: dict[str, Any] = {
        "snap": snap,
        "round": round_for(snap, carried=frozenset(overrides.get("carried_from", {}))),
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
        self.repo.write("use.py", "from calc import compute_total\n\ncompute_total([0])\n")
        base = self.repo.commit()
        self.repo.write("calc.py", defining.replace("sum(items)", "sum(items) * 2"))

        ctx = self.context(base)

        self.assertEqual(ctx.references["compute_total"][0].path, "use.py")
        self.assertEqual(ctx.references["compute_total"][0].line, 3)

    def test_real_call_sites_rank_before_docs_and_imports_and_the_cap_comes_after(self) -> None:
        self.repo.write("docs_a.md", "compute_total is documented here\n")
        for index in range(10):
            self.repo.write(
                f"pkg/user_{index:02}.py",
                f"from calc import compute_total\n\nresult = compute_total([{index}])\n",
            )
        base = self.edit_compute_total()

        ctx = self.context(base)
        rendered = next(
            row for row in render_packet(ctx).splitlines() if row.startswith("- `compute_total`")
        )

        texts = [ref.text for ref in ctx.references["compute_total"]]
        self.assertTrue(all("import" not in text for text in texts[:10]))
        self.assertEqual(rendered.count("result = compute_total(["), 6)
        self.assertNotIn("import", rendered)
        self.assertNotIn("docs_a.md", rendered)
        self.assertIn("(+15 more: git grep -n compute_total)", rendered)

    def test_tests_referencing_changed_symbol_are_listed(self) -> None:
        self.repo.write("tests/test_calc.py", "from calc import compute_total\n")
        for index in range(7):  # more calls than the packet lists: the test file is cut from them
            self.repo.write(f"a{index}.py", "result = compute_total([1])\n")
        base = self.edit_compute_total()

        text = render_packet(self.context(base))

        self.assertIn("Tests referencing `compute_total`: `tests/test_calc.py`", text)

    def test_a_test_file_named_by_a_listed_reference_is_not_repeated(self) -> None:
        self.repo.write("tests/test_calc.py", "from calc import compute_total\n")
        base = self.edit_compute_total()

        text = render_packet(self.context(base))

        self.assertIn("`tests/test_calc.py:1`", text)
        self.assertNotIn("Tests referencing", text)

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

        self.assertIn("-        line_150 = 150\n+150     line_150 = -150\n 151     line_151 = 151", text)
        self.assertIn("   1 def big(x):\n[… L2–L138 omitted …]\n 139     line_139 = 139", text)
        self.assertIn("[… L162–L300 omitted …]", text)
        self.assertNotIn("line_2 = 2", text)
        self.assertEqual(text.count("line_150 = "), 2, "the hunk is not printed twice")


class LastRunsTests(unittest.TestCase):
    def test_a_run_that_ran_no_tests_does_not_look_green(self) -> None:
        snap = DiffSnapshot("b" * 40, "fp1", (), (), 0, 0)
        ledger = record_run(Ledger())
        ledger = replace(ledger, runs=(replace(ledger.runs[0], tests_ran=0),))

        (line,) = packet._last_runs(ledger, snap)

        self.assertEqual(line, "R1 `pytest -q` (test) exit 0, 0 tests - not counted (current)")


class RenderTests(unittest.TestCase):
    def test_a_scope_whose_omission_saves_nothing_is_kept(self) -> None:
        path = "pkg/" + "deeply/nested/" * 6 + "a.py"
        hunk = Hunk("H1", path, None, "modified", 1, 1, 1, 1, "", ("-x = 0", "+x = 1"), "d1")
        ctx = build_context(hunk, scopes={"H1": Scope(path, "f", "function", 1, 1, "x = 1")})

        with mock.patch.object(packet, "MAX_PACKET_CHARS", 100):
            text = render_packet(ctx)

        self.assertIn("```python\n[H1]\n-   x = 0\n+ 1 x = 1\n[/H1]\n```", text)
        self.assertNotIn("Enclosing scope omitted", text)

    def test_size_guard_drops_enclosing_scopes(self) -> None:
        scope = Scope("pkg/a.py", "f", "function", 1, 50, "\n".join(f"line {i}" for i in range(50)))
        ctx = build_context(scopes={"H1": scope})

        with mock.patch.object(packet, "MAX_PACKET_CHARS", 900):
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

        self.assertEqual(text.count("```python"), 1, "the shared scope has one code block")
        self.assertIn("Scope: `compute` (function, L1–L10) — also covers H2", text)
        self.assertIn("Scope: `compute` — shown at H1", text)
        self.assertEqual(text.count("[H1]"), 1)
        self.assertEqual(text.count("[H2]"), 1)

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

        self.assertIn(" 1 func long() {\n[… L2–L31 omitted …]\n 32     v32 := 32", text)
        self.assertNotIn("# ...", text)

    def test_view_renders_assigned_hunks_in_full(self) -> None:
        scopes = {
            "H1": Scope("pkg/a.py", "alpha", "function", 1, 4, "def alpha():\n    a = 2"),
            "H2": Scope("pkg/b.py", "beta", "function", 7, 10, "def beta():\n    b = 2"),
        }

        view = render_packet_view(build_context(H1, H2, scopes=scopes), assigned_hunks=("H1",))

        self.assertIn("def alpha():", view)
        self.assertNotIn("def beta():", view)
        self.assertIn("- H2 · pkg/b.py · beta", view)

    def test_view_has_its_own_size_budget(self) -> None:
        scope = Scope("pkg/a.py", "f", "function", 1, 50, "\n".join(f"line {i}" for i in range(50)))
        ctx = build_context(H1, H2, scopes={"H1": scope})
        full = len(render_packet(ctx))
        uncut = len(render_packet_view(ctx, assigned_hunks=("H1",)))

        with mock.patch.object(packet, "MAX_PACKET_CHARS", full), mock.patch.object(
            packet, "MAX_VIEW_CHARS", uncut - 100
        ):
            view = render_packet_view(ctx, assigned_hunks=("H1",))
            whole = render_packet(ctx)

        self.assertIn("Enclosing scope omitted for size", view)
        self.assertNotIn("Enclosing scope omitted for size", whole)


class ScopeRenderTests(unittest.TestCase):
    """Each hunk is drawn inside its enclosing scope once, with the change marked in place."""

    SCOPE = Scope(
        "pkg/a.py",
        "calc",
        "function",
        1,
        8,
        "def calc(x):\n    a = x + 1\n    b = a * 2\n    c = b - 3\n    d = c / 4\n"
        "    e = d % 5\n    f = e ** 2\n    return f",
    )

    def render(self, *hunks: Hunk, scope: Scope | None = None) -> str:
        found = scope or self.SCOPE
        return render_packet(build_context(*hunks, scopes={hunk.id: found for hunk in hunks}))

    def test_added_removed_and_context_lines_are_marked_in_place(self) -> None:
        hunk = Hunk(
            "H1", "pkg/a.py", None, "modified", 2, 3, 2, 3, "def calc(x):",
            ("     a = x + 1", "-    b = a * 3", "+    b = a * 2", "     c = b - 3"), "d1",
        )

        text = self.render(hunk)

        self.assertIn(
            "```python\n  1 def calc(x):\n[H1]\n  2     a = x + 1\n-       b = a * 3\n+ 3     b = a * 2\n"
            "  4     c = b - 3\n[/H1]\n  5     d = c / 4\n  6     e = d % 5\n  7     f = e ** 2\n"
            "  8     return f\n```",
            text,
        )
        self.assertNotIn("```diff", text)
        self.assertEqual(text.count("b = a * 2"), 1, "the scope and the hunk are one listing")

    def test_removed_lines_sit_before_the_line_that_follows_them(self) -> None:
        hunk = Hunk(
            "H1", "pkg/a.py", None, "modified", 2, 4, 2, 2, "",
            ("     a = x + 1", "-    b = a * 2", "-    c = b - 3", "     d = c / 4"), "d1",
        )
        scope = Scope(
            "pkg/a.py", "calc", "function", 1, 4,
            "def calc(x):\n    a = x + 1\n    d = c / 4\n    return d",
        )

        lines = self.render(hunk, scope=scope).splitlines()

        start = lines.index("[H1]")
        self.assertEqual(
            lines[start : start + 6],
            ["[H1]", "  2     a = x + 1", "-       b = a * 2", "-       c = b - 3", "  3     d = c / 4", "[/H1]"],
        )

    def test_a_hunk_reaching_past_its_scope_is_not_cut_off(self) -> None:
        hunk = Hunk(
            "H1", "pkg/a.py", None, "modified", 7, 4, 7, 5, "",
            ("     f = e ** 2", "     return f", "+", "+def other():", "+    return 1"), "d1",
        )

        text = self.render(hunk)

        self.assertIn(
            "  7     f = e ** 2\n  8     return f\n+ 9 \n+10 def other():\n+11     return 1\n[/H1]", text
        )

    def test_the_no_newline_note_follows_its_line(self) -> None:
        hunk = Hunk(
            "H1", "pkg/a.py", None, "modified", 8, 1, 8, 1, "",
            ("-    return f", "\\ No newline at end of file", "+    return f + 1", "\\ No newline at end of file"),
            "d1",
        )

        lines = self.render(hunk).splitlines()

        start = lines.index("[H1]")
        self.assertEqual(
            lines[start + 1 : start + 5],
            [
                "-       return f",
                "\\ No newline at end of file",
                "+ 8     return f + 1",
                "\\ No newline at end of file",
            ],
        )

    def test_hunks_of_one_scope_share_one_block_and_each_is_bracketed(self) -> None:
        first = Hunk("H1", "pkg/a.py", None, "modified", 2, 1, 2, 1, "", ("-    a = 1", "+    a = 2"), "d1")
        second = Hunk("H2", "pkg/a.py", None, "modified", 7, 1, 7, 1, "", ("-    f = 1", "+    f = 2"), "d2")

        lines = self.render(first, second).splitlines()

        self.assertEqual(lines.count("```python"), 1)
        self.assertEqual([line for line in lines if line.startswith("[")], ["[H1]", "[/H1]", "[H2]", "[/H2]"])

    def test_a_long_scope_shows_its_signature_and_a_window_around_each_hunk(self) -> None:
        body = "\n".join(f"    v{index} = {index}" for index in range(2, 201))
        scope = Scope("pkg/a.py", "long", "function", 1, 200, "def long():\n" + body)
        first = Hunk("H1", "pkg/a.py", None, "modified", 30, 1, 30, 1, "", ("-    v30 = 0", "+    v30 = 30"), "d1")
        second = Hunk("H2", "pkg/a.py", None, "modified", 150, 1, 150, 1, "", ("-    v150 = 0", "+    v150 = 150"), "d2")

        text = self.render(first, second, scope=scope)

        self.assertIn("def long():\n[… L2–L21 omitted …]", text)
        self.assertIn("[… L39–L141 omitted …]", text)
        self.assertIn("[… L159–L200 omitted …]", text)
        self.assertEqual(text.count("```python"), 1)

    def test_a_hunk_without_a_scope_keeps_its_plain_diff(self) -> None:
        text = render_packet(build_context(H1))

        self.assertIn("```diff\n@@ -2,1 +2,1 @@\n-a = 1\n+a = 2\n```", text)

    def test_size_guard_keeps_only_the_hunk_when_it_cuts_the_scope(self) -> None:
        hunk = Hunk(
            "H1", "pkg/a.py", None, "modified", 2, 3, 2, 3, "",
            ("     a = x + 1", "-    b = a * 3", "+    b = a * 2", "     c = b - 3"), "d1",
        )
        ctx = build_context(hunk, scopes={"H1": self.SCOPE})
        uncut = len(render_packet(ctx))

        with mock.patch.object(packet, "MAX_PACKET_CHARS", uncut - 20):
            lines = render_packet(ctx).splitlines()

        start = lines.index("[H1]")
        self.assertEqual(
            lines[start : start + 6],
            ["[H1]", "  2     a = x + 1", "-       b = a * 3", "+ 3     b = a * 2", "  4     c = b - 3", "[/H1]"],
        )
        self.assertNotIn("  5     d = c / 4", lines)
        self.assertIn("Enclosing scope omitted for size — open pkg/a.py:L1–L8", lines)

    def test_a_dropped_hunk_body_leaves_the_shared_block_of_the_others(self) -> None:
        first = Hunk("H1", "pkg/a.py", None, "modified", 2, 1, 2, 1, "", ("-    a = 1", "+    a = 2"), "d1")
        second = Hunk(
            "H2", "pkg/a.py", None, "modified", 7, 3, 7, 3, "",
            ("-    f = 1", "+    f = 2", "     return f", "     return f", "     return f"), "d2",
        )
        ctx = build_context(first, second, scopes={"H1": self.SCOPE, "H2": self.SCOPE})
        uncut = len(render_packet(ctx))

        with mock.patch.object(packet, "MAX_PACKET_CHARS", uncut - 450):
            text = render_packet(ctx)

        self.assertIn("## Omitted hunk bodies\nH2", text)
        self.assertIn("diff omitted (5 lines): git diff bbbbbbb -- pkg/a.py", text)
        self.assertIn("[H1]", text)
        self.assertNotIn("[H2]", text)


class TrimmingLadderTests(unittest.TestCase):
    """The size guard cuts scope bodies, then references, then hunk bodies, in that order."""

    def context(self) -> PacketContext:
        scope = Scope("pkg/a.py", "compute", "function", 1, 40, "\n".join(f"    line {i}" for i in range(1, 41)))
        lines = ("-    x = 0", *(f"+    x{index} = {index}" for index in range(25)))
        hunk = Hunk("H1", "pkg/a.py", None, "modified", 2, 25, 2, 25, "", lines, "d1")
        refs = tuple(Reference(f"pkg/use_{index}.py", index + 1, f"compute({index})") for index in range(8))
        return build_context(
            hunk, scopes={"H1": scope}, symbols={"H1": ("compute",)}, references={"compute": refs}
        )

    def render_within(self, ctx: PacketContext, budget: int) -> str:
        with mock.patch.object(packet, "MAX_PACKET_CHARS", budget):
            return render_packet(ctx)

    def stages(self) -> tuple[str, str, str, str]:
        ctx = self.context()
        full = render_packet(ctx)
        no_scope = self.render_within(ctx, len(full) - 1)
        short_refs = self.render_within(ctx, len(no_scope) - 1)
        no_diff = self.render_within(ctx, len(short_refs) - 1)
        return full, no_scope, short_refs, no_diff

    def test_the_budgets_are_one_120k_packet_and_60k_views(self) -> None:
        self.assertEqual(packet.MAX_PACKET_CHARS, 120_000)
        self.assertEqual(packet.MAX_VIEW_CHARS, 60_000)
        self.assertEqual(packet.MAX_REFERENCES_PER_SYMBOL, 6)

    def test_a_round_within_the_budget_keeps_scope_bodies_references_and_diffs(self) -> None:
        full, *_ = self.stages()

        self.assertIn("    line 30", full)
        self.assertNotIn("omitted for size", full)
        self.assertNotIn("## Omitted hunk bodies", full)
        self.assertIn("(+2 more: git grep -n compute)", full)
        self.assertEqual(full.count("`pkg/use_"), 6)

    def test_the_scope_body_goes_first_and_the_references_stay(self) -> None:
        _, no_scope, *_ = self.stages()

        self.assertIn("Enclosing scope omitted for size — open pkg/a.py:L1–L40", no_scope)
        self.assertNotIn("    line 30", no_scope)
        self.assertIn("(+2 more: git grep -n compute)", no_scope)
        self.assertIn("+26     x24 = 24", no_scope)

    def test_references_are_trimmed_to_three_before_any_diff_goes(self) -> None:
        _, _, short_refs, _ = self.stages()

        self.assertIn("(+5 more: git grep -n compute)", short_refs)
        self.assertEqual(short_refs.count("`pkg/use_"), 3)
        self.assertIn("+26     x24 = 24", short_refs)
        self.assertNotIn("## Omitted hunk bodies", short_refs)

    def test_the_largest_diff_goes_last_and_its_header_stays(self) -> None:
        *_, no_diff = self.stages()

        self.assertIn("## Omitted hunk bodies\nH1", no_diff)
        self.assertIn("### H1 · `pkg/a.py` L2–L26 · modified", no_diff)
        self.assertIn("diff omitted (26 lines): git diff bbbbbbb -- pkg/a.py", no_diff)
        self.assertNotIn("x24 = 24", no_diff)

    def test_an_omitted_added_file_points_at_the_file_and_a_rename_at_both_paths(self) -> None:
        added = Hunk("H1", "pkg/new.py", None, "added", 0, 0, 1, 30, "", tuple(f"+v{i} = {i}" for i in range(30)), "d1")
        renamed = Hunk(
            "H2", "pkg/new_name.py", "pkg/old_name.py", "renamed", 1, 30, 1, 30, "",
            tuple(line for i in range(15) for line in (f"-a{i} = {i}", f"+b{i} = {i}")), "d2",
        )
        ctx = build_context(added, renamed)

        text = self.render_within(ctx, 600)

        self.assertIn("diff omitted (30 lines): open pkg/new.py", text)
        self.assertIn("git diff bbbbbbb -- pkg/old_name.py pkg/new_name.py", text)

    def test_a_long_scope_body_is_present_in_a_big_round(self) -> None:
        hunks, scopes = [], {}
        for index in range(1, 21):
            path = f"pkg/m{index}.py"
            scopes[f"H{index}"] = Scope(
                path, f"f{index}", "function", 1, 90, "\n".join(f"    body {n}" for n in range(1, 91))
            )
            hunks.append(Hunk(f"H{index}", path, None, "modified", 45, 1, 45, 1, "", ("-    old", "+    new"), f"d{index}"))
        ctx = build_context(*hunks, scopes=scopes)

        text = render_packet(ctx)

        self.assertLess(len(text), packet.MAX_PACKET_CHARS)
        self.assertNotIn("omitted for size", text)
        for index in (1, 10, 20):
            self.assertIn(f"### H{index} · `pkg/m{index}.py`", text)
        self.assertEqual(text.count("    body 40\n"), 20, "each long scope shows its window around the hunk")


class HintAndReferenceBoundTests(unittest.TestCase):
    def many_hints(self, count: int = 300) -> PacketContext:
        base = build_context(H1, H2)
        signals = tuple(
            QualitySignal(f"S{index}", SignalKind.DUPLICATE_LOGIC, "pkg/a.py", 1, "`f` and `g` repeat " + "x" * 90)
            for index in range(1, count + 1)
        )
        return replace(base, round=replace(base.round, signals=signals))

    def test_quality_hints_are_listed_in_part_with_a_count_of_the_rest(self) -> None:
        text = render_packet(self.many_hints())

        self.assertLess(len(text), 10_000)
        self.assertIn("300 hints (duplicate-logic 300); +296 more not listed", text)
        self.assertEqual(text.count("**S"), 4)

    def test_a_view_of_a_small_file_stays_within_the_view_budget_whatever_the_hints(self) -> None:
        view = render_packet_view(self.many_hints(2000), assigned_hunks=("H1",))

        self.assertLess(len(view), packet.MAX_VIEW_CHARS)
        self.assertLess(len(view), 10_000)

    def test_the_preamble_counts_against_the_budget(self) -> None:
        ctx = self.many_hints(12)
        listed = render_packet(ctx)

        with mock.patch.object(packet, "MAX_PACKET_CHARS", len(listed) - 1):
            cut = render_packet(ctx)

        self.assertIn("**S1**", listed)
        self.assertNotIn("**S1**", cut)
        self.assertIn("12 hints (duplicate-logic 12)", cut)
        self.assertIn("### H1", cut)
        self.assertNotIn("Enclosing scope omitted", cut)

    def test_references_are_capped_at_six_when_rendered_with_a_count_of_the_rest(self) -> None:
        refs = tuple(Reference(f"pkg/user_{n}.py", 5, f"compute_total({n})") for n in range(12))

        text = render_packet(
            build_context(symbols={"H1": ("compute_total",)}, references={"compute_total": refs})
        )

        line = next(row for row in text.splitlines() if row.startswith("- `compute_total`"))
        self.assertEqual(line.count("`pkg/user_"), 6)
        self.assertIn("(+6 more: git grep -n compute_total)", line)


class ViewDietTests(unittest.TestCase):
    def view_and_packet(self) -> tuple[str, str]:
        signals = (
            QualitySignal("S1", SignalKind.LONG_FUNCTION, "pkg/a.py", 1, "`f` spans 50 lines"),
            QualitySignal("S2", SignalKind.TEST_NAME, "pkg/b.py", 8, "`t` has `_and_`"),
        )
        base = build_context(H1, H2, signals_by_hunk={"H1": ("errors",)})
        ctx = replace(base, round=replace(base.round, signals=signals), unscanned_files=("db/x.sql",))
        return render_packet_view(ctx, assigned_hunks=("H1",)), render_packet(ctx)

    def test_neither_a_view_nor_the_packet_has_the_lens_table_or_the_recording_cheat_sheet(self) -> None:
        view, whole = self.view_and_packet()

        for text in ("## Suggested lenses", "## Recording cheat sheet", "craftsman review cover"):
            self.assertNotIn(text, view)
            self.assertNotIn(text, whole)

    def test_a_view_keeps_the_risk_signals_of_its_hunks_with_their_lenses(self) -> None:
        view, _ = self.view_and_packet()

        self.assertIn("### H1 · `pkg/a.py` L2–L2 · modified · signals: errors (→ failure", view)

    def test_a_view_keeps_the_quality_hints_of_its_own_files_only(self) -> None:
        view, whole = self.view_and_packet()

        self.assertIn("**S1**", view)
        self.assertNotIn("**S2**", view)
        self.assertIn("**S2**", whole)

    def test_a_view_and_the_packet_keep_the_note_about_files_without_signal_patterns(self) -> None:
        view, whole = self.view_and_packet()

        for text in (view, whole):
            self.assertIn("Not scanned for risk signals (no patterns for their language): `db/x.sql`", text)

    def test_a_view_points_at_the_scout(self) -> None:
        view, _ = self.view_and_packet()

        self.assertIn("craftsman review scout H3", view)


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
            headings[:5],
            [
                "# Craftsman review packet — round 2",
                "## Intent",
                "## Conventions to read first",
                "## Hunks",
                "### H1 · `pkg/a.py` L1–L2 · modified · signals: errors (→ failure)",
            ],
        )

    def test_carried_hunk_is_listed_without_its_diff(self) -> None:
        text = render_packet(self.context())

        self.assertIn("### H2 · `pkg/b.py` L4–L4 · carried (covered in round 1, unchanged)", text)

    def test_diff_fence_is_longer_than_backticks_in_the_hunk(self) -> None:
        text = render_packet(self.context())

        self.assertIn("````diff\n@@ -1,1 +1,2 @@ def f():\n x = 1\n+y = ```\n````", text)


class OtherHunksIndexTests(unittest.TestCase):
    def index_of(self, ctx: PacketContext) -> str:
        view = render_packet_view(ctx, assigned_hunks=("H1",))
        return view.split("### Other hunks (index)")[1]

    def test_a_carried_hunk_inside_a_scope_keeps_both_its_scope_and_the_carried_label(self) -> None:
        scope = Scope("pkg/b.py", "moved", "function", 1, 9, "def moved():\n    return 1")
        ctx = build_context(H1, H2, scopes={"H2": scope}, carried_from={"H2": 1})

        self.assertIn("- H2 · pkg/b.py · moved · carried (round 1)", self.index_of(ctx))

    def test_a_carried_hunk_without_a_scope_is_labeled_carried(self) -> None:
        ctx = build_context(H1, H2, carried_from={"H2": 1})

        self.assertIn("- H2 · pkg/b.py · carried (round 1)", self.index_of(ctx))

    def test_an_open_hunk_keeps_its_scope_or_status(self) -> None:
        scope = Scope("pkg/b.py", "moved", "function", 1, 9, "def moved():\n    return 1")

        self.assertIn("- H2 · pkg/b.py · moved\n", self.index_of(build_context(H1, H2, scopes={"H2": scope})) + "\n")
        self.assertIn("- H2 · pkg/b.py · modified", self.index_of(build_context(H1, H2)))


class LeanPacketTests(unittest.TestCase):
    """A small diff costs a hunter its code context and little else."""

    SCOPE = Scope("pkg/a.py", "f", "function", 1, 3, "def f():\n    x = 1\n    return x")
    REFS = {"f": (Reference("pkg/use.py", 7, "f()"),)}

    def small(self, **overrides: Any) -> PacketContext:
        fields: dict[str, Any] = {
            "scopes": {"H1": self.SCOPE},
            "symbols": {"H1": ("f",)},
            "references": self.REFS,
            "signals_by_hunk": {"H1": ("errors",)},
        }
        return build_context(**{**fields, **overrides})

    def test_a_small_packet_opens_with_its_header_and_then_the_hunks(self) -> None:
        text = render_packet(self.small())

        self.assertRegex(text, r"\A# Craftsman review packet — round 2\nBase `b+` · [^\n]*\n\n## Hunks\n")

    def test_sections_with_nothing_to_say_are_left_out(self) -> None:
        text = render_packet(self.small())

        for placeholder in ("## Intent", "## Conventions", "(none", "(not recorded", "Commits since base"):
            self.assertNotIn(placeholder, text)

    def test_the_task_commits_and_conventions_appear_when_there_are_some(self) -> None:
        text = render_packet(self.small(task="Add f", commits=("abc1234 feat: f",), conventions=("AGENTS.md",)))

        self.assertIn("## Intent\n**Task:** Add f\n\n**Commits since base:**\n- abc1234 feat: f", text)
        self.assertIn("## Conventions to read first\n- AGENTS.md", text)
        self.assertNotIn("(not recorded", render_packet(self.small(commits=("abc1234 feat: f",))))
        self.assertNotIn("Commits since base", render_packet(self.small(task="Add f")))

    def test_a_small_packet_has_no_lens_table_or_cheat_sheet_but_all_the_code_context(self) -> None:
        text = render_packet(self.small(test_files_by_symbol={"f": ("tests/test_a.py",)}))

        for dropped in ("Suggested lenses", "cheat sheet", "craftsman review add", "craftsman review cover"):
            self.assertNotIn(dropped, text)
        self.assertIn("signals: errors (→ failure)", text)
        self.assertIn("Scope: `f` (function, L1–L3)", text)
        self.assertIn("def f():", text)
        self.assertIn("[H1]", text)
        self.assertIn("- `f`: `pkg/use.py:7` `f()`", text)
        self.assertIn("Tests referencing `f`: `tests/test_a.py`", text)

    def test_a_view_of_every_hunk_is_the_packet(self) -> None:
        ctx = self.small()

        self.assertEqual(render_packet_view(ctx, assigned_hunks=("H1",)), render_packet(ctx))


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
