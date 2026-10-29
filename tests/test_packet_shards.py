"""Acceptance tests for WP3: smaller packet budgets, trimming ladder and reviewer shards."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from craftsman import packet
from craftsman.diffscan import DiffSnapshot, Hunk, Reference, Scope
from craftsman.lenses import Tier, select_lenses
from craftsman.packet import (
    MAX_PACKET_CHARS,
    MAX_QUICK_PACKET_CHARS,
    MAX_VIEW_CHARS,
    PacketContext,
    parse_shard_spec,
    plan_shards,
    render_packet,
    render_packet_view,
    select_shard,
    suggested_shard_count,
)
from craftsman.records import FieldError, HunkRef, Round
from craftsman.review import view_slug

from tests.support import EvidenceRepo, assert_cli_error

PREPARED_AT = "2026-01-01T00:00:00.000Z"
BASE_SHA = "abcdef1234567890abcdef1234567890abcdef12"


def make_round(
    snap: DiffSnapshot,
    *,
    tier: Tier = "standard",
    carried: frozenset[str] = frozenset(),
) -> Round:
    return Round(
        number=1,
        fingerprint=snap.fingerprint,
        base_sha=snap.base_sha,
        tier=tier,
        prepared_at=PREPARED_AT,
        hunks=tuple(
            HunkRef(h.id, h.path, h.digest, h.new_start, h.new_len, h.id in carried)
            for h in snap.hunks
        ),
        lenses=("intent", "contracts", "design", "sweep") if tier != "quick" else ("intent", "craft"),
        packet_path="packet-1.md",
        signals=(),
    )


def build_ctx(
    hunks: tuple[Hunk, ...],
    *,
    tier: Tier = "standard",
    carried_from: dict[str, int] | None = None,
    **overrides: Any,
) -> PacketContext:
    carried = carried_from or {}
    added = sum(line.startswith("+") for h in hunks for line in h.lines)
    deleted = sum(line.startswith("-") for h in hunks for line in h.lines)
    files = tuple(dict.fromkeys(h.path for h in hunks))
    snap = DiffSnapshot(BASE_SHA, "f" * 64, hunks, files, (), added, deleted)
    round_ = make_round(snap, tier=tier, carried=frozenset(carried))
    picks = tuple(
        p for p in select_lenses(tier, {}, docs_only=False) if p.lens.id in round_.lenses
    )
    fields: dict[str, Any] = {
        "snap": snap,
        "round": round_,
        "picks": picks,
        "task": "Shrink review packets",
        "commits": (),
        "conventions": (),
        "scopes": {},
        "symbols": {},
        "references": {},
        "signals_by_hunk": {},
        "carried_from": carried,
        "reopened": {},
        "test_files_by_symbol": {},
        "last_runs": (),
        "facts_by_hunk": {},
    }
    return PacketContext(**{**fields, **overrides})


def numbered_hunk(index: int, path: str, *, line_count: int = 40, start: int = 1) -> Hunk:
    lines = tuple(f"+    value_{index}_{line} = {line} * {index} + 123456789" for line in range(line_count))
    return Hunk(
        id=f"H{index}",
        path=path,
        old_path=None,
        status="modified",
        old_start=start,
        old_len=0,
        new_start=start,
        new_len=line_count,
        header=f"def fn_{index}():",
        lines=lines,
        digest=f"digest-{index}",
    )


class BudgetConstantsTests(unittest.TestCase):
    def test_budget_constants_match_specification(self) -> None:
        self.assertEqual(MAX_PACKET_CHARS, 120_000)
        self.assertEqual(MAX_QUICK_PACKET_CHARS, 40_000)
        self.assertEqual(MAX_VIEW_CHARS, 60_000)

    def test_quick_packet_emits_scope_pointers_without_scope_bodies(self) -> None:
        h1 = numbered_hunk(1, "pkg/a.py", line_count=4, start=2)
        h2 = numbered_hunk(2, "pkg/a.py", line_count=4, start=12)
        scope = Scope(
            "pkg/a.py",
            "compute",
            "function",
            1,
            25,
            "\n".join(["def compute():", *(f"    step_{i} = {i}" for i in range(1, 25))]),
        )
        quick_text = render_packet(build_ctx((h1, h2), tier="quick", scopes={"H1": scope, "H2": scope}))
        std_text = render_packet(build_ctx((h1, h2), tier="standard", scopes={"H1": scope, "H2": scope}))

        self.assertEqual(quick_text.count("Scope: `compute` (function, L1–L25)"), 2)
        self.assertNotIn("Enclosing scope", quick_text)
        self.assertIn("Enclosing scope (L1–L25):", std_text)


class TrimmingLadderTests(unittest.TestCase):
    def ladder_context(self) -> PacketContext:
        h1 = numbered_hunk(1, "pkg/small.py", line_count=8, start=2)
        h2 = numbered_hunk(2, "pkg/huge.py", line_count=80, start=2)
        scope_body = "\n".join(["def helper():", *(f"    local_var_{i} = {i} * 99" for i in range(50))])
        scopes = {
            "H1": Scope("pkg/small.py", "small_fn", "function", 1, 51, scope_body),
            "H2": Scope("pkg/huge.py", "huge_fn", "function", 1, 51, scope_body),
        }
        refs = tuple(
            Reference(f"callers/mod_{i}.py", i + 10, f"result_{i} = target_symbol(arg_{i}, flag=True)")
            for i in range(8)
        )
        return build_ctx(
            (h1, h2),
            scopes=scopes,
            symbols={"H1": ("target_symbol",)},
            references={"target_symbol": refs},
            signals_by_hunk={"H1": ("errors",), "H2": ("filesystem",)},
        )

    def test_ladder_drops_scopes_before_touching_references_or_diffs(self) -> None:
        ctx = self.ladder_context()
        full_len = len(render_packet(ctx))

        with mock.patch.object(packet, "MAX_PACKET_CHARS", full_len - 400):
            trimmed = render_packet(ctx)

        self.assertLessEqual(len(trimmed), full_len - 400)
        self.assertIn("Enclosing scope omitted for size", trimmed)
        self.assertIn("callers/mod_7.py:17", trimmed)
        self.assertNotIn("more: git grep -n", trimmed)
        self.assertNotIn("## Omitted hunk bodies", trimmed)

    def test_ladder_trims_references_to_three_before_omitting_diffs(self) -> None:
        ctx = self.ladder_context()
        no_scopes = render_packet(
            build_ctx(
                ctx.snap.hunks,
                symbols=ctx.symbols,
                references=ctx.references,
                signals_by_hunk=ctx.signals_by_hunk,
            )
        )
        # Subtract 50 chars past the scope-omitted baseline so step (b) must run, but not step (c).
        budget = len(no_scopes) - 50

        with mock.patch.object(packet, "MAX_PACKET_CHARS", budget):
            trimmed = render_packet(ctx)

        self.assertLessEqual(len(trimmed), budget)
        self.assertIn("callers/mod_0.py:10", trimmed)
        self.assertIn("callers/mod_2.py:12", trimmed)
        self.assertNotIn("callers/mod_3.py:13", trimmed)
        self.assertIn("(+5 more: git grep -n target_symbol)", trimmed)
        self.assertNotIn("## Omitted hunk bodies", trimmed)
        self.assertIn("value_2_79", trimmed)

    def test_forced_overrun_omits_largest_diff_with_marker_keeping_headers(self) -> None:
        ctx = self.ladder_context()

        with mock.patch.object(packet, "MAX_PACKET_CHARS", 4300):
            trimmed = render_packet(ctx)

        self.assertLessEqual(len(trimmed), 4300)
        self.assertIn("## Omitted hunk bodies\nH2\n", trimmed)
        self.assertIn(f"diff omitted (80 lines): git diff {BASE_SHA[:7]} -- pkg/huge.py", trimmed)
        # Smaller hunk H1 keeps its diff body, and EVERY hunk keeps its full header with signals.
        self.assertIn("value_1_0", trimmed)
        self.assertIn("### H1 · `pkg/small.py` L2–L9 · modified · signals: errors", trimmed)
        self.assertIn("### H2 · `pkg/huge.py` L2–L81 · modified · signals: filesystem", trimmed)

    def test_omitted_diff_marker_uses_open_for_added_and_both_paths_for_renamed(self) -> None:
        added_lines = tuple(f"+    added_line_{i} = {i} * 123456789" for i in range(60))
        renamed_lines = tuple(f"+    renamed_line_{i} = {i} * 987654321" for i in range(60))
        h_added = Hunk(
            "H1", "pkg/new_untracked.py", None, "added", 0, 0, 1, 60, "", added_lines, "d-add"
        )
        h_renamed = Hunk(
            "H2", "pkg/new_name.py", "pkg/old_name.py", "renamed", 1, 0, 1, 60, "", renamed_lines, "d-ren"
        )
        ctx = build_ctx((h_added, h_renamed))

        with mock.patch.object(packet, "MAX_PACKET_CHARS", 2200):
            trimmed = render_packet(ctx)

        self.assertIn("diff omitted (60 lines): open pkg/new_untracked.py", trimmed)
        self.assertIn(
            f"diff omitted (60 lines): git diff {BASE_SHA[:7]} -- pkg/old_name.py pkg/new_name.py",
            trimmed,
        )


class ShardingInvariantsTests(unittest.TestCase):
    def multi_file_context(self) -> PacketContext:
        hunks = (
            numbered_hunk(1, "pkg/alpha.py", line_count=50, start=1),
            numbered_hunk(2, "pkg/alpha.py", line_count=45, start=80),
            numbered_hunk(3, "pkg/beta.py", line_count=30, start=1),
            numbered_hunk(4, "pkg/gamma.py", line_count=30, start=1),
            numbered_hunk(5, "pkg/delta.py", line_count=15, start=1),
            numbered_hunk(6, "pkg/alpha.py", line_count=10, start=200),
            numbered_hunk(7, "pkg/carried_only.py", line_count=20, start=1),
        )
        return build_ctx(hunks, carried_from={"H6": 1, "H7": 1})

    def test_shards_are_disjoint_cover_open_hunks_without_splitting_files(self) -> None:
        ctx = self.multi_file_context()

        shards = plan_shards(ctx, 3)

        self.assertEqual(len(shards), 3)
        seen_files: set[str] = set()
        seen_hunks: set[str] = set()
        for shard in shards:
            self.assertTrue(shard.files)
            self.assertFalse(seen_files & set(shard.files))
            self.assertFalse(seen_hunks & set(shard.hunk_ids))
            seen_files.update(shard.files)
            seen_hunks.update(shard.hunk_ids)
        self.assertEqual(seen_files, set(ctx.round.open_files))
        self.assertEqual(seen_hunks, set(ctx.round.open_hunk_ids))
        # Multi-hunk file pkg/alpha.py stays together in shard 1 without its carried H6.
        alpha_shards = [s for s in shards if "pkg/alpha.py" in s.files]
        self.assertEqual(len(alpha_shards), 1)
        self.assertEqual(alpha_shards[0].hunk_ids[:2], ("H1", "H2"))
        self.assertNotIn("H6", seen_hunks)

    def test_equal_sized_files_break_ties_deterministically_by_path(self) -> None:
        # Same exact rendered length per file ("pkg/b.py" vs "pkg/a.py" listed out of order).
        lines = ("+x = 12345",) * 10
        h1 = Hunk("H1", "pkg/b.py", None, "modified", 1, 0, 1, 10, "", lines, "d1")
        h2 = Hunk("H2", "pkg/a.py", None, "modified", 1, 0, 1, 10, "", lines, "d2")
        ctx = build_ctx((h1, h2))

        first, second = plan_shards(ctx, 2)

        self.assertEqual(first.files, ("pkg/a.py",))
        self.assertEqual(second.files, ("pkg/b.py",))

    def test_forty_eight_hunk_fixture_respects_tier_shard_budgets(self) -> None:
        hunks_list: list[Hunk] = []
        scopes: dict[str, Scope] = {}
        scope_body = "\n".join(["def work():", *(f"    item_{i} = {i} * 17" for i in range(55))])
        idx = 1
        for file_num in range(6):
            path = f"pkg/module_{file_num}.py"
            for slot in range(8):
                start = slot * 120 + 1
                hunk = numbered_hunk(idx, path, line_count=75, start=start)
                hunks_list.append(hunk)
                scopes[hunk.id] = Scope(path, f"work_{idx}", "function", start, start + 55, scope_body)
                idx += 1
        hunks = tuple(hunks_list)
        self.assertEqual(len(hunks), 48)

        std_ctx = build_ctx(hunks, tier="standard", scopes=scopes)
        std_packet = render_packet(std_ctx)
        self.assertLessEqual(len(std_packet), MAX_PACKET_CHARS)
        for hunk in hunks:
            self.assertIn(f"### {hunk.id} · `{hunk.path}`", std_packet)

        quick_ctx = build_ctx(hunks, tier="quick", scopes=scopes)
        quick_packet = render_packet(quick_ctx)
        self.assertLessEqual(len(quick_packet), MAX_QUICK_PACKET_CHARS)
        self.assertNotIn("Enclosing scope", quick_packet)

        for k in range(1, 5):
            shard = select_shard(std_ctx, f"{k}/4")
            view = render_packet_view(
                std_ctx, lenses=("intent", "contracts"), assigned_hunks=shard.hunk_ids, shard=shard
            )
            self.assertLessEqual(len(view), MAX_VIEW_CHARS)
            self.assertIn(f"Files in this shard: {', '.join(shard.files)}", view)

    def test_rendering_is_byte_for_byte_deterministic(self) -> None:
        ctx = self.multi_file_context()
        shard_a = select_shard(ctx, "2/3")
        shard_b = select_shard(ctx, "2/3")

        view_a = render_packet_view(ctx, lenses=("intent",), assigned_hunks=shard_a.hunk_ids, shard=shard_a)
        view_b = render_packet_view(ctx, lenses=("intent",), assigned_hunks=shard_b.hunk_ids, shard=shard_b)

        self.assertEqual(render_packet(ctx).encode("utf-8"), render_packet(ctx).encode("utf-8"))
        self.assertEqual(view_a.encode("utf-8"), view_b.encode("utf-8"))

    def test_shard_slug_format(self) -> None:
        self.assertEqual(
            view_slug(["intent", "contracts"], shard=(2, 3)),
            "intent-contracts.shard-2-of-3",
        )
        self.assertEqual(suggested_shard_count(3, 130_000), 3)
        self.assertEqual(suggested_shard_count(10, 300_000), 4)
        self.assertEqual(suggested_shard_count(1, 95_000), 1)
        with self.assertRaisesRegex(FieldError, "no open files"):
            parse_shard_spec("1/1", 0)


class CliShardWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = EvidenceRepo(Path(tmp.name).resolve() / "repo")

    def test_invalid_shard_arguments_fail_with_clear_messages(self) -> None:
        self.repo.write("a.py", "a = 1\n")
        self.repo.write("b.py", "b = 2\n")
        self.repo.cli("review", "prepare", "--tier", "standard")

        for bad_spec, fragment in (
            ("3/2", "--shard must satisfy 1 <= K <= N (got '3/2')"),
            ("0/2", "--shard must satisfy 1 <= K <= N (got '0/2')"),
            ("1/3", "--shard cannot split 2 open file(s) into 3 shards"),
            ("abc", "--shard must look like K/N"),
        ):
            with self.subTest(shard=bad_spec):
                proc = self.repo.cli("review", "view", "--lenses", "R1", "--shard", bad_spec, check=False)
                assert_cli_error(self, proc, 1, fragment)

        both = self.repo.cli(
            "review", "view", "--lenses", "R1", "--shard", "1/2", "--hunks", "H1", check=False
        )
        assert_cli_error(self, both, 1, "pass either --shard or --hunks, not both")

    def test_single_large_file_over_view_budget_suggests_one_shard(self) -> None:
        pad = "x" * 60
        lines = "\n".join(f"    item_{i} = payload + '_{i}_' + '{pad}'" for i in range(900))
        self.repo.write("single_big.py", f"def build_table(payload: str) -> None:\n{lines}\n")

        prepared = self.repo.cli("review", "prepare", "--tier", "standard")
        packet_file = self.repo.ledger_dir / "packet-1.md"
        packet_len = len(packet_file.read_text(encoding="utf-8"))

        self.assertGreater(packet_len, MAX_VIEW_CHARS)
        self.assertLessEqual(packet_len, MAX_PACKET_CHARS)
        self.assertIn("Suggested reviewer shards: 1 (no sharding needed)", prepared.stdout)

        # Re-running prepare when unchanged still reports the shard suggestion from packet-1.md.
        again = self.repo.cli("review", "prepare", "--tier", "standard")
        self.assertIn("Suggested reviewer shards: 1 (no sharding needed)", again.stdout)

        view_proc = self.repo.cli("review", "view", "--lenses", "intent,contracts", "--shard", "1/1")
        view_path = Path(view_proc.stdout.strip())
        view_text = view_path.read_text(encoding="utf-8")

        self.assertEqual(view_path.name, "packet-1.intent-contracts.shard-1-of-1.md")
        self.assertIn("Files in this shard: single_big.py", view_text)
        self.assertLessEqual(len(view_text), MAX_VIEW_CHARS)

    def test_multi_file_large_diff_suggests_shards_with_deterministic_views(self) -> None:
        lines = "\n".join(f"    row_{i} = val * {i + 1} + 9876543210123456789" for i in range(600))
        chunk = f"def compute_rows(val: int) -> None:\n{lines}\n"
        for name in ("mod_a.py", "mod_b.py", "mod_c.py"):
            self.repo.write(name, chunk)

        prepared = self.repo.cli("review", "prepare", "--tier", "standard")
        self.assertIn("Suggested reviewer shards: 2", prepared.stdout)

        p1 = Path(
            self.repo.cli("review", "view", "--lenses", "intent,contracts", "--shard", "1/2").stdout.strip()
        )
        p2 = Path(
            self.repo.cli("review", "view", "--lenses", "intent,contracts", "--shard", "2/2").stdout.strip()
        )
        first_bytes = p1.read_bytes()
        repeat_bytes = Path(
            self.repo.cli("review", "view", "--lenses", "intent,contracts", "--shard", "1/2").stdout.strip()
        ).read_bytes()

        self.assertEqual(p1.name, "packet-1.intent-contracts.shard-1-of-2.md")
        self.assertEqual(p2.name, "packet-1.intent-contracts.shard-2-of-2.md")
        self.assertEqual(first_bytes, repeat_bytes)
        self.assertLessEqual(len(p1.read_text(encoding="utf-8")), MAX_VIEW_CHARS)
        self.assertLessEqual(len(p2.read_text(encoding="utf-8")), MAX_VIEW_CHARS)


if __name__ == "__main__":
    unittest.main()
