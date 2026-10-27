#!/usr/bin/env python3
"""Behavioral tests for review packet context collection and Markdown rendering."""

from __future__ import annotations

import os
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path
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
from craftsman.packet import PacketContext, collect_context, render_packet
from craftsman.records import HunkRef, Ledger, Round

from tests.support import record_run
from tests.test_diffscan import GitRepo

PREPARED_AT = "2026-01-01T00:00:00.000Z"
TWO_FUNCTIONS = (
    "def foo():\n    a = 1\n    return a\ndef bar():\n    x = 1\n    y = 2\n    return x + y\n"
)


def round_for(snap: DiffSnapshot) -> Round:
    return Round(
        number=1,
        fingerprint=snap.fingerprint,
        base_sha=snap.base_sha,
        tier="standard",
        prepared_at=PREPARED_AT,
        hunks=tuple(
            HunkRef(hunk.id, hunk.path, hunk.digest, hunk.new_start, hunk.new_len, False)
            for hunk in snap.hunks
        ),
        lenses=("intent", "sweep"),
        packet_path="packet-1.md",
        signals=(),
    )


class CollectContextTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = GitRepo(Path(tmp.name).resolve())

    def context(self, base: str) -> PacketContext:
        snap = snapshot(self.repo.root, base)
        index = WorktreeIndex(self.repo.root)
        symbols = {hunk.id: index.symbols(hunk) for hunk in snap.hunks}
        picks = tuple(
            pick
            for pick in select_lenses("standard", {}, docs_only=False)
            if pick.lens.id in ("intent", "sweep")
        )
        return collect_context(
            self.repo.root,
            snap,
            round_for(snap),
            picks,
            "task",
            symbols=symbols,
            ledger=Ledger(),
            index=index,
        )

    def test_pure_deletion_shows_the_function_the_lines_were_removed_from(self) -> None:
        self.repo.write("mod.py", TWO_FUNCTIONS)
        base = self.repo.commit()
        self.repo.write("mod.py", TWO_FUNCTIONS.replace("    x = 1\n", ""))

        ctx = self.context(base)

        scope = ctx.scopes["H1"]
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

    def test_commits_before_the_first_commit_and_after_it(self) -> None:
        self.repo.write("app.py", "x = 1\n")
        base = resolve_base_sha(self.repo.root, None)

        self.assertEqual(self.context(base).commits, ())

        self.repo.commit("first")

        self.assertEqual([line.split(" ", 1)[1] for line in self.context(base).commits], ["first"])

    def test_orphan_branch_without_commits_lists_no_commits(self) -> None:
        self.repo.write("app.py", "x = 1\n")
        base = self.repo.commit("first")
        self.repo.git("checkout", "-q", "--orphan", "fresh")
        self.repo.write("app.py", "x = 2\n")

        ctx = self.context(base)

        self.assertEqual(ctx.commits, ())
        self.assertEqual(ctx.snap.files, ("app.py",))

    def test_references_prefer_files_other_than_the_definition(self) -> None:
        defining = "def compute_total(items):\n    return sum(items)\n" + "".join(
            f"compute_total([{index}])\n" for index in range(10)
        )
        self.repo.write("calc.py", defining)
        self.repo.write("use.py", "from calc import compute_total\n")
        base = self.repo.commit()
        self.repo.write("calc.py", defining.replace("sum(items)", "sum(items) * 2"))

        ctx = self.context(base)

        self.assertEqual(ctx.symbols["H1"], ("compute_total",))
        self.assertEqual(ctx.references["compute_total"][0].path, "use.py")

    def test_tests_referencing_changed_symbol_are_listed(self) -> None:
        self.repo.write("calc.py", "def compute_total(items):\n    return sum(items)\n")
        self.repo.write(
            "tests/test_calc.py",
            "from calc import compute_total\ndef test_sum():\n    assert compute_total([1]) == 1\n",
        )
        base = self.repo.commit()
        self.repo.write("calc.py", "def compute_total(items):\n    return sum(items) * 2\n")

        text = render_packet(self.context(base))

        self.assertIn("Tests referencing `compute_total`: `tests/test_calc.py`", text)

    def test_last_test_run_is_shown_with_staleness(self) -> None:
        self.repo.write("calc.py", "def compute_total(items):\n    return sum(items)\n")
        base = self.repo.commit()
        self.repo.write("calc.py", "def compute_total(items):\n    return sum(items) * 2\n")
        snap = snapshot(self.repo.root, base)
        index = WorktreeIndex(self.repo.root)
        symbols = {hunk.id: index.symbols(hunk) for hunk in snap.hunks}
        picks = tuple(
            pick
            for pick in select_lenses("standard", {}, docs_only=False)
            if pick.lens.id in ("intent", "sweep")
        )
        ledger = record_run(Ledger(), fingerprint="stale_fp")
        ctx = collect_context(
            self.repo.root,
            snap,
            round_for(snap),
            picks,
            "task",
            symbols=symbols,
            ledger=ledger,
            index=index,
        )

        text = render_packet(ctx)

        self.assertIn("Last runs: R1 `pytest -q` (test) exit 0 (stale)", text)

    def test_conventions_include_contributing_variants_and_changelog_readme(self) -> None:
        self.repo.write("CONTRIBUTING.rst", "How to contribute.\n")
        self.repo.write("CONTRIBUTING.txt", "Text guidelines.\n")
        self.repo.write(".github/CONTRIBUTING.md", "GitHub contributing guide.\n")
        self.repo.write("docs/CONTRIBUTING.rst", "Docs contributing guide.\n")
        self.repo.write("changelog/README.rst", "Towncrier instructions.\n")
        self.repo.write("app.py", "x = 1\n")
        base = self.repo.commit()
        self.repo.write("app.py", "x = 2\n")

        ctx = self.context(base)

        self.assertEqual(
            ctx.conventions,
            (
                "CONTRIBUTING.rst",
                "CONTRIBUTING.txt",
                ".github/CONTRIBUTING.md",
                "docs/CONTRIBUTING.rst",
                "changelog/README.rst",
            ),
        )


def rendered_context(
    *,
    path: str = "pkg/a.py",
    scope: Scope | None = None,
    references: Mapping[str, tuple[Reference, ...]] | None = None,
) -> PacketContext:
    hunk = Hunk("H1", path, None, "modified", 1, 1, 1, 1, "", ("-x = 0", "+x = 1"), "d1")
    snap = DiffSnapshot("b" * 40, "f" * 64, (hunk,), (path,), (), 1, 1)
    picks = tuple(p for p in select_lenses("standard", {}, docs_only=False) if p.lens.id == "intent")
    round_ = Round(
        number=1,
        fingerprint=snap.fingerprint,
        base_sha=snap.base_sha,
        tier="standard",
        prepared_at=PREPARED_AT,
        hunks=(HunkRef("H1", path, "d1", 1, 1, False),),
        lenses=("intent",),
        packet_path="packet-1.md",
        signals=(),
    )
    return PacketContext(
        root=Path("."),
        snap=snap,
        round=round_,
        picks=picks,
        task="",
        commits=(),
        conventions=(),
        scopes={"H1": scope},
        symbols={"H1": tuple(references or ())},
        references=references or {},
        signals_by_hunk={},
        carried_from={},
    )


class RenderTests(unittest.TestCase):
    def test_a_scope_whose_omission_saves_nothing_is_kept(self) -> None:
        path = "pkg/" + "deeply/nested/" * 6 + "a.py"
        scope = Scope(path, "f", "function", 1, 1, "x = 1")
        ctx = rendered_context(path=path, scope=scope)

        with mock.patch.object(packet, "MAX_PACKET_CHARS", 100):
            text = render_packet(ctx)

        self.assertIn("Enclosing scope (L1–L1):\n```python\nx = 1\n```", text)
        self.assertNotIn("Enclosing scope omitted", text)

    def test_reference_text_with_backticks_keeps_its_code_span(self) -> None:
        references = {
            "helper_fn": (
                Reference("web/app.js", 3, "msg = `hi ${helper_fn()}`"),
                Reference("web/b.js", 7, "x = helper_fn() ``y``"),
            )
        }

        text = render_packet(rendered_context(references=references))

        self.assertIn(
            "- `helper_fn`: `web/app.js:3` `` msg = `hi ${helper_fn()}` `` · "
            "`web/b.js:7` ``` x = helper_fn() ``y`` ```",
            text,
        )

    def test_scope_shared_by_two_hunks_is_rendered_once(self) -> None:
        h1 = Hunk("H1", "pkg/a.py", None, "modified", 2, 1, 2, 1, "", ("-a = 1", "+a = 2"), "d1")
        h2 = Hunk("H2", "pkg/a.py", None, "modified", 8, 1, 8, 1, "", ("-b = 1", "+b = 2"), "d2")
        snap = DiffSnapshot("b" * 40, "f" * 64, (h1, h2), ("pkg/a.py",), (), 2, 2)
        scope = Scope("pkg/a.py", "compute", "function", 1, 10, "def compute():\n    a = 2\n    b = 2")
        picks = tuple(p for p in select_lenses("standard", {}, docs_only=False) if p.lens.id == "intent")
        ctx = PacketContext(
            root=Path("."),
            snap=snap,
            round=round_for(snap),
            picks=picks,
            task="",
            commits=(),
            conventions=(),
            scopes={"H1": scope, "H2": scope},
            symbols={"H1": ("compute",), "H2": ("compute",)},
            references={"compute": (Reference("pkg/use.py", 4, "compute()"),)},
            signals_by_hunk={},
            carried_from={},
        )

        text = render_packet(ctx)

        self.assertEqual(text.count("Enclosing scope (L1–L10):"), 1)
        self.assertIn("Scope: `compute` — shown at H1", text)

    def test_long_scope_renders_signature_with_context_around_hunk(self) -> None:
        body_lines = ["def long_fn(x: int) -> int:"] + [f"    line_{i} = {i}" for i in range(2, 82)]
        scope = Scope("pkg/a.py", "long_fn", "function", 1, 81, "\n".join(body_lines))
        hunk = Hunk(
            "H1",
            "pkg/a.py",
            None,
            "modified",
            40,
            1,
            40,
            1,
            "",
            ("-    line_40 = 0", "+    line_40 = 40"),
            "d1",
        )
        snap = DiffSnapshot("b" * 40, "f" * 64, (hunk,), ("pkg/a.py",), (), 1, 1)
        picks = tuple(p for p in select_lenses("standard", {}, docs_only=False) if p.lens.id == "intent")
        ctx = PacketContext(
            root=Path("."),
            snap=snap,
            round=round_for(snap),
            picks=picks,
            task="",
            commits=(),
            conventions=(),
            scopes={"H1": scope},
            symbols={"H1": ("long_fn",)},
            references={},
            signals_by_hunk={},
            carried_from={},
        )

        text = render_packet(ctx)

        self.assertIn("Enclosing scope (pkg/a.py:1-81):", text)
        self.assertIn("def long_fn(x: int) -> int:", text)
        self.assertIn("line_40 = 40", text)
        self.assertNotIn("line_10 = 10", text)
        self.assertNotIn("line_75 = 75", text)

    def test_view_renders_assigned_hunks_in_full_with_one_line_index(self) -> None:
        h1 = Hunk("H1", "pkg/a.py", None, "modified", 2, 1, 2, 1, "", ("-a = 1", "+a = 2"), "d1")
        h2 = Hunk("H2", "pkg/b.py", None, "modified", 8, 1, 8, 1, "", ("-b = 1", "+b = 2"), "d2")
        snap = DiffSnapshot("b" * 40, "f" * 64, (h1, h2), ("pkg/a.py", "pkg/b.py"), (), 2, 2)
        scope_a = Scope("pkg/a.py", "alpha", "function", 1, 4, "def alpha():\n    a = 2")
        scope_b = Scope("pkg/b.py", "beta", "function", 7, 10, "def beta():\n    b = 2")
        picks = select_lenses("standard", {}, docs_only=False)
        ctx = PacketContext(
            root=Path("."),
            snap=snap,
            round=round_for(snap),
            picks=tuple(picks),
            task="test view",
            commits=(),
            conventions=(),
            scopes={"H1": scope_a, "H2": scope_b},
            symbols={"H1": ("alpha",), "H2": ("beta",)},
            references={},
            signals_by_hunk={},
            carried_from={},
        )

        view = packet.render_packet_view(ctx, lenses=("contracts",), assigned_hunks=("H1",))

        self.assertIn("### H1 · `pkg/a.py`", view)
        self.assertIn("def alpha():", view)
        self.assertNotIn("### H2 · `pkg/b.py`", view)
        self.assertNotIn("def beta():", view)
        self.assertIn("- H2 · pkg/b.py · beta", view)
        self.assertEqual(packet.expand_hunk_spec("H1-H2", ("H1", "H2")), ("H1", "H2"))


class PacketLayoutTests(unittest.TestCase):
    def context(self, *, scope_lines: int = 3) -> PacketContext:
        hunks = (
            Hunk(
                "H1", "pkg/a.py", None, "modified", 1, 1, 1, 2, "def f():", (" x = 1", "+y = ```"), "d1"
            ),
            Hunk("H2", "pkg/b.py", None, "modified", 4, 1, 4, 1, "", ("-a", "+b"), "d2"),
        )
        snap = DiffSnapshot("b" * 40, "f" * 64, hunks, ("pkg/a.py", "pkg/b.py"), (), 2, 1)
        round_ = Round(
            number=2,
            fingerprint=snap.fingerprint,
            base_sha=snap.base_sha,
            tier="standard",
            prepared_at=PREPARED_AT,
            hunks=(
                HunkRef("H1", "pkg/a.py", "d1", 1, 2, False),
                HunkRef("H2", "pkg/b.py", "d2", 4, 1, True),
            ),
            lenses=("intent", "sweep"),
            packet_path="packet-2.md",
            signals=(),
        )
        body = "\n".join(f"line {i}" for i in range(scope_lines))
        return PacketContext(
            root=Path("."),
            snap=snap,
            round=round_,
            picks=tuple(
                pick
                for pick in select_lenses("standard", {}, docs_only=False)
                if pick.lens.id in ("intent", "sweep")
            ),
            task="",
            commits=("abc1234 feat: add f",),
            conventions=("AGENTS.md",),
            scopes={"H1": Scope("pkg/a.py", "f", "function", 1, scope_lines, body)},
            symbols={"H1": ("helper_fn",)},
            references={"helper_fn": (Reference("pkg/c.py", 40, "x = helper_fn(y)"),)},
            signals_by_hunk={"H1": ("errors",)},
            carried_from={"H2": 1},
        )

    def test_sections_appear_in_order(self) -> None:
        text = render_packet(self.context())

        headings = [line for line in text.splitlines() if line.startswith("#")]
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

    def test_size_guard_drops_enclosing_scopes(self) -> None:
        ctx = self.context(scope_lines=500)
        with mock.patch.object(packet, "MAX_PACKET_CHARS", 3000):
            text = render_packet(ctx)

        self.assertIn("Enclosing scope omitted for size — open pkg/a.py:L1–L500", text)


if __name__ == "__main__":
    unittest.main()
