"""Tests for the token and cost figures of `evals.run compare` and for `evals.run breakdown`.

The inputs come from the real code paths: ledgers written by the real `craftsman review` CLI in a
temporary repository, with the agent events the hooks would append, and result records made by
`evals.agent.make_record`.
"""

from __future__ import annotations

import contextlib
import io
import json
import re
import shutil
import sys
import tempfile
import unittest
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from craftsman.attribution import agent_runs
from craftsman.ledger import add_agent_event, load, mutate
from craftsman.records import AgentEvent, AgentUsage, Ledger, format_timestamp
from craftsman.workspace import ReviewMarker, write_marker
from evals import agent as run_agent
from evals import breakdown as run_breakdown
from evals import compare as run_compare
from evals import run

from tests.support import EvidenceRepo, coverage_note

if sys.platform == "win32":
    raise unittest.SkipTest(
        "the seeded benchmark runs on Linux and macOS only (POSIX shell wrapper, POSIX-path cases)"
    )

REPO_ROOT = Path(__file__).resolve().parent.parent
CLAUDE = {"CLAUDECODE": "1"}
# Twenty small functions: `f<i>` returns on line 3 * i + 2.
CART = "".join(f"def f{i}(x):\n    return x + {i}\n\n" for i in range(20))
TRUTH = {
    "defects": [
        {"id": "cart-1", "kind": "off-by-one-boundary", "summary": "f1 is one off",
         "locations": [{"file": "cart.py", "start": 5, "end": 5, "anchor": "return x + 1"}]},
        {"id": "cart-2", "kind": "swallowed-error", "summary": "f9 hides an error",
         "locations": [{"file": "cart.py", "start": 29, "end": 29, "anchor": "return x + 9"}]},
        {"id": "cart-3", "kind": "aliasing", "summary": "f5 shares a list",
         "locations": [{"file": "cart.py", "start": 17, "end": 17, "anchor": "return x + 5"}]},
    ],
    "decoys": [
        {"id": "cart-d1", "summary": "f14 is right",
         "locations": [{"file": "cart.py", "start": 44, "end": 44, "anchor": "return x + 14"}]},
    ],
}  # fmt: skip
STATIC = "static: cart.py:{line} returns x plus a constant, so f(0) is not 0 as the task asks"
GUARD = "guarded by cart.py:1, which only ever receives numbers from the caller"
# What a recording command prints: the tokens a subagent's transcript would show (see `craftsman.usage`).
RECORD_TOKENS = re.compile(
    r"Brief issued \((B\d+)\)|Coverage recorded \((C\d+)\)"
    r"|\b(F\d+) recorded \(|verdict recorded \((V\d+)\)"
)


def usage(cache_write: int, *, tokens: int = 1000) -> AgentUsage:
    """What one subagent used: `tokens` of input, output and cache read each, and `cache_write`."""
    return AgentUsage(tokens, tokens, tokens, cache_write, 60.0)


class Review:
    """A review of `CART` driven through the real CLI in a temporary repository, in an enforced round."""

    def __init__(self, root: Path) -> None:
        self.repo = EvidenceRepo(root)
        self.repo.write("cart.py", CART)
        now = datetime.now(timezone.utc)
        started, touched = format_timestamp(now - timedelta(hours=1)), format_timestamp(now)
        write_marker(self.ledger_path, ReviewMarker("claude", "s1", started, True, touched_at=touched))
        self.repo.cli("review", "prepare")  # the hooks just touched the marker: an enforced round

    @property
    def ledger_path(self) -> Path:
        return self.repo.ledger_dir / "ledger.json"

    def ledger(self) -> Ledger:
        return load(self.ledger_path)

    def subagent(
        self, agent: str, agent_id: str, *commands: Sequence[str], used: AgentUsage | None
    ) -> None:
        """One subagent run as the hooks record it: a start, its `craftsman review` commands, and a stop
        with the record tokens its transcript shows and, unless `used` is None, its usage."""
        started = format_timestamp(datetime.now(timezone.utc))
        tokens: list[str] = []
        for command in commands:
            out = self.repo.cli("review", *command, env_extra=CLAUDE).stdout
            tokens += [token for groups in RECORD_TOKENS.findall(out) for token in groups if token]
        stopped = format_timestamp(datetime.now(timezone.utc) + timedelta(seconds=1))
        events = (
            AgentEvent("claude", "start", agent, agent_id, started),
            AgentEvent("claude", "stop", agent, agent_id, stopped, usage=used, records=tuple(tokens)),
        )

        def record(current: Ledger) -> tuple[Ledger, None]:
            for event in events:
                current = add_agent_event(current, event)
            return current, None

        mutate(self.ledger_path, record)

    def hunter(
        self, agent_id: str, slot: str, *findings: int, cover: bool = True, used: AgentUsage | None
    ) -> None:
        """A hunter that claims `slot`, covers its hunks unless it dies first, and adds the findings."""
        commands: list[Sequence[str]] = [("brief", slot)]
        if cover:
            plan = self.ledger().rounds[-1].plan
            assert plan is not None
            hunks = list(next(s for s in plan.slots if s.id == slot).hunks)
            commands.append(
                ("cover", "--hunter", slot, "--hunks", ",".join(hunks), "--note", coverage_note(hunks))
            )
        for line in findings:
            commands.append((
                "add", "--hunter", slot, "--file", "cart.py", "--line", str(line), "--lens", "contracts",
                "--severity", "major", "--title", f"Wrong value on line {line}",
                "--scenario", f"calling the function of line {line} with 0 does not return 0",
            ))  # fmt: skip
        self.subagent("craftsman-reviewer", agent_id, *commands, used=used)

    def verifier(self, agent_id: str, *commands: Sequence[str], used: AgentUsage | None) -> None:
        self.subagent("craftsman-verifier", agent_id, *commands, used=used)


def judged(finding: str, verdict: str, line: int = 1) -> tuple[str, ...]:
    evidence = STATIC.format(line=line) if verdict == "probable" else GUARD
    return ("verdict", finding, "--as", verdict, "--evidence", evidence)


def full_review(root: Path) -> Review:
    """Every shape the breakdown has to tell apart, in one enforced round.

    W1-1 (bug) is launched twice: its first hunter dies after the brief. F1 (W1-1, bug) and F4 (W1-3,
    simplicity) both point at cart-1, F1 first. F2 and F3 (W1-2) match no seeded defect and F3 is merged
    into F2; F5 (W1-3) is on the decoy. The wave's variant slot W2-F1 finds cart-2 (F6). Verifier v2
    only judges F1 again: a second opinion; v3 judges F6 first and F4 again, which is none. Hunter
    cache writes are 900..1300, verifier ones 500..700.
    """
    review = Review(root)
    review.hunter("h1", "W1-1", cover=False, used=usage(900))
    review.hunter("h2", "W1-1", 5, used=usage(1000))
    review.hunter("h3", "W1-2", 59, 58, used=usage(1100))
    review.hunter("h4", "W1-3", 6, 44, used=usage(1200))
    review.verifier(
        "v1",
        ("merge", "F2", "F3"),
        judged("F1", "probable", 5),
        judged("F2", "dismissed"),
        judged("F4", "probable", 6),
        judged("F5", "dismissed"),
        used=usage(500),
    )
    review.hunter("h5", "W2-F1", 29, used=usage(1300))
    review.verifier("v2", judged("F1", "probable", 5), used=usage(600))
    review.verifier("v3", judged("F6", "probable", 29), judged("F4", "probable", 6), used=usage(700))
    return review


def short_review(root: Path) -> Review:
    """One hunter that finds cart-1 and reports its usage, then is resumed and reports it again (one
    agent), and one that finds nothing and does not report its usage."""
    review = Review(root)
    review.hunter("h1", "W1-1", 5, used=usage(2000))
    review.subagent("craftsman-reviewer", "h1", used=usage(2500))
    review.hunter("h2", "W1-2", used=None)
    return review


SESSION = {"input": 50_000, "output": 20_000, "cache_read": 900_000, "cache_write": 60_000}


class Results:
    """A results file of `evals.run agent`, its logs folder, and a cases folder with the `cart` case."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.cases_dir = root / "cases"
        (self.cases_dir / "cart").mkdir(parents=True)
        (self.cases_dir / "cart" / "truth.json").write_text(json.dumps(TRUTH), encoding="utf-8")
        self.case = run.load_case("cart", self.cases_dir)
        self.path = root / "results" / "runs.jsonl"
        self.path.parent.mkdir(parents=True)

    def record(
        self,
        arm: str,
        index: int,
        *,
        ledger: Path | None = None,
        cost: float = 2.0,
        tokens: dict[str, int] | None = None,
        loaded: bool = True,
        case: run.Case | None = None,
    ) -> dict[str, Any]:
        """A record as `agent.run_one` makes it, with the ledger kept in the logs folder."""
        case = case or self.case
        plugin = run_agent.PluginInfo("craftsman", "craft-review")
        cfg = run_agent.AgentConfig(
            (case,), REPO_ROOT, plugin, arm, "sonnet", 1, 1, 5.0, 30.0,
            self.path, self.path.with_suffix(".logs"), None, "claude", "dontAsk", (), False,
        )  # fmt: skip
        stream = run_agent.Stream()
        result: dict[str, Any] = {
            "type": "result", "subtype": "success", "total_cost_usd": cost, "duration_ms": 600_000,
            "num_turns": 40, "subagent_stats": {"spawned": 8, "completed": 8, "failed": 0},
        }  # fmt: skip
        if tokens is not None:
            # `usage` differs from `modelUsage`, so the tests show which of the two is read.
            result["usage"] = {"input_tokens": 1, "output_tokens": 1}
            result["modelUsage"] = {
                "claude-sonnet-5-5": {
                    "inputTokens": tokens["input"],
                    "outputTokens": tokens["output"],
                    "cacheReadInputTokens": tokens["cache_read"],
                    "cacheCreationInputTokens": tokens["cache_write"],
                    "costUSD": cost,
                },
            }
        stream.feed(result)
        log_dir = cfg.logs_dir / arm / f"{case.id}-{index}"
        log_dir.mkdir(parents=True, exist_ok=True)
        logs = {"dir": str(log_dir)}
        recall = None
        if ledger is not None:
            shutil.copyfile(ledger, log_dir / "ledger.json")
            logs["ledger"] = str(log_dir / "ledger.json")
            recall = run_agent.recall_of(case, *run.read_ledger(log_dir / "ledger.json"))
        execution = run_agent.Execution(
            0, False, [] if loaded else ["skill craftsman:craft-review"], stream, 720.0
        )
        return run_agent.make_record(
            cfg, case, index, "prompt", execution, recall, logs, "2026-11-10T00:00:00+00:00"
        )

    def write(self, *records: dict[str, Any]) -> Path:
        self.path.write_text(
            "".join(json.dumps(r, sort_keys=True) + "\n" for r in records), encoding="utf-8"
        )
        return self.path

    def main(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(err):
            code = run.main(["--cases-dir", str(self.cases_dir), *argv], out)
        return code, out.getvalue(), err.getvalue()


class ReviewFixture(unittest.TestCase):
    """The two reviews are slow to drive through the CLI, so every test shares them."""

    full: Path
    short: Path
    _tmp: tempfile.TemporaryDirectory[str]

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        root = Path(cls._tmp.name)
        cls.full = full_review(root / "full").ledger_path
        cls.short = short_review(root / "short").ledger_path

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.results = Results(Path(tmp.name))

    def standard(self) -> Path:
        """Arm `a`: the full review and a run without a ledger; `b`: the short one and an invalid run."""
        results = self.results
        return results.write(
            results.record("a", 1, ledger=self.full, cost=6.0, tokens=SESSION),
            results.record("a", 2, cost=1.0, tokens=SESSION),
            results.record("b", 1, ledger=self.short, cost=2.0, tokens=SESSION),
            results.record("b", 2, ledger=self.short, loaded=False),
        )

    def breakdown(self, *extra: str) -> dict[str, Any]:
        code, out, err = self.results.main("breakdown", "--out", str(self.standard()), "--json", *extra)
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        assert isinstance(data, dict)
        return data


class FixtureTests(ReviewFixture):
    """The reviews hold what the breakdown tests rely on, so a CLI change cannot make them vacuous."""

    def test_the_full_review_has_a_relaunch_a_wave_a_merge_and_a_second_verdict(self) -> None:
        ledger = load(self.full)
        plan = ledger.rounds[-1].plan
        assert plan is not None
        self.assertEqual([c.slot for c in ledger.claims], ["W1-1", "W1-1", "W1-2", "W1-3", "W2-F1"])
        self.assertEqual(next(s.kind for s in plan.slots if s.id == "W2-F1"), "variant")
        self.assertEqual(
            [f.hunter for f in ledger.findings], ["W1-1", "W1-2", "W1-2", "W1-3", "W1-3", "W2-F1"]
        )
        self.assertEqual(next(f.merged_into for f in ledger.findings if f.id == "F3"), "F2")
        self.assertEqual(len(next(f for f in ledger.findings if f.id == "F1").verdict_log), 2)

    def test_the_short_review_has_a_resumed_hunter(self) -> None:
        runs = agent_runs(load(self.short))
        self.assertEqual([r.agent_id for r in runs], ["h1", "h1", "h2"])

    def test_the_kept_ledger_is_scored_like_any_run(self) -> None:
        record = self.results.record("a", 1, ledger=self.full)
        self.assertEqual(record["recall"]["found_ids"], ["cart-1", "cart-2"])
        self.assertEqual(len(record["recall"]["duplicates"]), 1)  # F4 on cart-1 again


class VerifierCountTests(unittest.TestCase):
    def test_a_verifier_after_a_merge_is_a_second_opinion_and_one_that_vouched_nothing_is_idle(self) -> None:
        # The shape of an end-to-end run: a verifier merges a major duplicate (a dismissal the gate wants a second
        # verifier for), one verifier is denied its commands and records nothing, and the second opinion judges
        # only the merged duplicate.
        with tempfile.TemporaryDirectory() as tmp:
            review = Review(Path(tmp))
            review.hunter("h1", "W1-1", 5, 6, used=usage(900))
            review.verifier("v1", ("merge", "F1", "F2"), judged("F1", "probable", 5), used=usage(500))
            review.verifier("v2", used=usage(500))
            review.verifier("v3", judged("F2", "dismissed"), used=usage(500))
            ledger = review.ledger()
        self.assertTrue(next(f for f in ledger.findings if f.id == "F2").verdict_log[0].merge)
        runs = agent_runs(ledger)
        self.assertEqual(run_breakdown.second_opinions(ledger, runs), 1)  # v3
        self.assertEqual(run_breakdown.idle_verifiers(ledger, runs), 1)  # v2


class BreakdownTests(ReviewFixture):
    def group(
        self, data: dict[str, Any], arm: str, case: str = run_breakdown.ALL_CASES
    ) -> dict[str, Any]:
        groups = data[arm]["groups"]
        found = next(g for g in groups if g["case"] == case)
        assert isinstance(found, dict)
        return found

    def test_runs_without_a_ledger_or_invalid_are_counted_and_named(self) -> None:
        data = self.breakdown()
        a, b = self.group(data, "a"), self.group(data, "b")
        self.assertEqual((a["runs"], a["with_ledger"], a["with_tokens"]), (2, 1, 1))
        self.assertEqual((b["runs"], b["with_ledger"], b["with_tokens"]), (1, 1, 0))
        self.assertEqual(
            data["a"]["without_ledger"], [{"run": "cart#2", "reason": "no kept ledger (logs.ledger)"}]
        )
        self.assertEqual(data["b"]["invalid"], [{"run": "cart#2", "reason": "load-failed"}])
        self.assertEqual(
            data["b"]["without_tokens"],
            [{"run": "cart#1", "reason": "1 of 2 subagents have no usage in the ledger"}],
        )
        self.assertAlmostEqual(a["cost_usd"], 3.5)  # both valid runs, with or without a ledger
        self.assertAlmostEqual(a["minutes"], 12.0)

    def test_subagents_by_role_slot_kind_and_generation(self) -> None:
        agents = self.group(self.breakdown(), "a")["subagents"]
        self.assertEqual((agents["hunters"], agents["verifiers"]), (5, 3))
        self.assertEqual(
            (agents["bug"], agents["simplicity"], agents["variant"], agents["fresh"]), (3, 1, 1, 0)
        )
        self.assertEqual((agents["gen 1"], agents["gen 2"], agents["none"]), (4, 1, 0))
        self.assertEqual(agents["relaunched slots"], 1)  # W1-1, whose first hunter died after its brief
        self.assertEqual(agents["second opinions"], 1)  # v2 only judged F1 again; v3 also judged F6 first
        self.assertEqual(agents["idle verifiers"], 0)  # every verifier vouches for a verdict

    def test_tokens_per_role_and_the_main_sessions_share(self) -> None:
        tokens = self.group(self.breakdown(), "a")["tokens"]
        hunters, verifiers = tokens["hunters"], tokens["verifiers"]
        self.assertEqual((hunters["agents"], hunters["input"], hunters["cache_write"]), (5, 5000, 5500))
        self.assertEqual(
            (verifiers["agents"], verifiers["output"], verifiers["cache_write"]), (3, 3000, 1800)
        )
        self.assertEqual(hunters["minutes"], 5.0)
        whole = sum(SESSION.values())
        self.assertEqual(tokens["whole session"]["total"], whole)
        main = tokens["main session"]
        self.assertEqual(main["cache_read"], 900_000 - 8000)
        self.assertEqual(main["cache_write"], 60_000 - 5500 - 1800)
        self.assertEqual(main["total"], whole - hunters["total"] - verifiers["total"])
        self.assertAlmostEqual(main["share"] + hunters["share"] + verifiers["share"], 1.0)

    def test_a_session_smaller_than_its_subagents_has_no_main_share(self) -> None:
        results = self.results
        small = {**SESSION, "output": 2000}  # the hunters alone report 5000 output tokens
        path = results.write(
            results.record("a", 1, ledger=self.full, tokens=SESSION),
            results.record("a", 2, ledger=self.full, tokens=small),
        )
        code, out, err = results.main("breakdown", "--out", str(path), "--json")
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual(
            data["a"]["without_tokens"],
            [{"run": "cart#2", "reason": "its subagents report more output tokens than the record"}],
        )
        a = self.group(data, "a")
        self.assertEqual(a["with_tokens"], 1)
        self.assertEqual(a["tokens"]["main session"]["output"], 20_000 - 5000 - 3000)

    def test_cache_write_per_subagent_mean_and_minimum(self) -> None:
        writes = self.group(self.breakdown(), "a")["cache_write_per_subagent"]
        self.assertEqual(writes["hunters"], {"subagents": 5, "mean": 1100.0, "min": 900})
        self.assertEqual(writes["verifiers"], {"subagents": 3, "mean": 600.0, "min": 500})

    def test_a_run_without_full_usage_is_left_out_of_the_tokens_only(self) -> None:
        b = self.group(self.breakdown(), "b")
        self.assertIsNone(b["tokens"])
        self.assertEqual(b["cache_write_per_subagent"], {"hunters": None, "verifiers": None})
        self.assertEqual(b["subagents"]["hunters"], 2)  # h1 was resumed: one agent
        self.assertEqual(b["subagents"]["relaunched slots"], 0)
        self.assertEqual(b["found_first_by"]["found"], 1)

    def test_the_findings_funnel(self) -> None:
        funnel = self.group(self.breakdown(), "a")["funnel"]
        self.assertEqual(
            funnel,
            {
                "recorded": 6,
                "merged": 1,
                "proven": 0,
                "probable": 3,
                "question": 0,
                "dismissed": 2,
                "open": 0,
            },
        )

    def test_each_defect_found_goes_to_the_slot_that_recorded_it_first(self) -> None:
        a = self.group(self.breakdown(), "a")
        found = a["found_first_by"]
        # cart-1 twice, by F1 of W1-1 (bug) and then F4 of W1-3 (simplicity); cart-2 by F6 of W2-F1
        self.assertEqual(found["found"], 2)
        self.assertEqual((found["bug"], found["simplicity"], found["variant"]), (1, 0, 1))
        self.assertEqual((found["gen 1"], found["gen 2"]), (1, 1))
        noise = a["noise_by"]
        # F2 (unmatched, W1-2) and F5 (the decoy, W1-3); F3 was merged into F2
        self.assertEqual((noise["findings"], noise["bug"], noise["simplicity"]), (2, 1, 1))

    def test_cost_per_found_defect_per_arm(self) -> None:
        data = self.breakdown()
        # arm a: a mean cost of (6 + 1) / 2 over a mean of (2 + 0) / 2 defects found
        self.assertAlmostEqual(data["a"]["groups"][-1]["cost_per_defect"], 3.5)
        self.assertAlmostEqual(data["b"]["groups"][-1]["cost_per_defect"], 2.0)

    def test_cases_adds_a_group_per_case_and_arm_picks_one_arm(self) -> None:
        other = run.Case("other", self.results.cases_dir / "other", self.results.case.defects, ())
        results = self.results
        path = results.write(
            results.record("a", 1, ledger=self.full, tokens=SESSION),
            results.record("a", 1, ledger=self.short, tokens=SESSION, case=other),
            results.record("b", 1, ledger=self.short, tokens=SESSION),
        )
        code, out, err = results.main("breakdown", "--out", str(path), "--json", "--cases", "--arm", "a")
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual(list(data), ["a"])
        self.assertEqual([g["case"] for g in data["a"]["groups"]], ["cart", "other", "all"])
        self.assertEqual(data["a"]["unknown_case"][0]["run"], "other#1")
        self.assertEqual(self.group(data, "a", "other")["yield_runs"], 0)  # its findings match no truth
        self.assertEqual(self.group(data, "a", "other")["funnel"]["recorded"], 1)

    def test_text_tables_name_what_is_left_out(self) -> None:
        code, out, err = self.results.main("breakdown", "--out", str(self.standard()))
        self.assertEqual(code, 0, err)
        self.assertIn("arm a: cost per found defect $3.50", out)
        self.assertIn("1 without a ledger: cart#2 (no kept ledger (logs.ledger))", out)
        self.assertIn("1 invalid, left out: cart#2 (load-failed)", out)
        self.assertIn(
            "1 without full token counts: cart#1 (1 of 2 subagents have no usage in the ledger)", out
        )
        self.assertRegex(out, r"\na    main session +- +42k +12k +892k +53k +999k +- +97%\n")
        # cache write per subagent: mean, minimum
        self.assertRegex(out, r"\na    hunters +5 +1\.1k +900\n")
        self.assertLessEqual(
            max(len(line) for line in out.splitlines() if not line.startswith("  ")), 120
        )

    def test_an_unknown_arm_and_a_missing_file_are_usage_errors(self) -> None:
        path = self.standard()
        code, _, err = self.results.main("breakdown", "--out", str(path), "--arm", "c")
        self.assertEqual(code, run.EXIT_USAGE)
        self.assertIn("arms: a, b", err)
        code, _, _ = self.results.main("breakdown", "--out", str(path.with_name("missing.jsonl")))
        self.assertEqual(code, run.EXIT_USAGE)

    def test_nothing_is_written(self) -> None:
        path = self.standard()
        before = {p: p.read_bytes() for p in self.results.root.rglob("*") if p.is_file()}
        self.results.main("breakdown", "--out", str(path), "--cases")
        after = {p: p.read_bytes() for p in self.results.root.rglob("*") if p.is_file()}
        self.assertEqual(after, before)


class CompareTokenTests(ReviewFixture):
    def compare(self, *extra: str) -> str:
        results = self.results
        path = results.write(
            results.record("a", 1, ledger=self.full, cost=6.0, tokens=SESSION),
            results.record("a", 2, ledger=self.short, cost=2.0, tokens=SESSION),
            results.record(
                "b", 1, ledger=self.short, cost=1.0, tokens={k: v // 2 for k, v in SESSION.items()}
            ),
            results.record("b", 2, ledger=self.short, cost=1.0),
        )
        code, out, err = results.main("compare", str(path), *extra)
        self.assertEqual(code, 0, err)
        return out

    def test_tokens_and_cost_per_found_defect_per_case_and_in_the_total(self) -> None:
        text = self.compare()
        rows = [line for line in text.splitlines() if line.startswith(("cart", "TOTAL"))]
        # arm a: 1.5 defects found on average for $4.00; arm b: 1 for $1.00, one run without token counts
        self.assertRegex(rows[0], r" 1\.0M +2\.67$")
        self.assertRegex(rows[1], r" 1\.0M +2\.67$")
        self.assertRegex(rows[2], r" 515k +1\.00 1 without tokens$")
        self.assertNotIn(
            "cache read", next(line for line in text.splitlines() if line.startswith("case"))
        )

    def test_later_arms_get_the_ratio_to_the_first(self) -> None:
        text = self.compare()
        versus = text[text.index("b versus a") :]
        for label in ("cart", "TOTAL"):
            row = next(line for line in versus.splitlines() if line.startswith(label))
            self.assertRegex(row, r"x0\.50 +x0\.38$")  # tokens, then $/defect: 1.00 / 2.67

    def test_the_split_and_the_json(self) -> None:
        split = self.compare("--tokens")
        self.assertIn("cache read", next(line for line in split.splitlines() if line.startswith("case")))
        self.assertRegex(
            next(line for line in split.splitlines() if line.startswith("cart")),
            r" 50k +20k +900k +60k$",
        )
        data = json.loads(self.compare("--json"))
        total = data["a"]["rows"][-1]
        self.assertEqual(
            (total["tokens"], total["$/defect"], total["cache read"]), ("1.0M", "2.67", "900k")
        )
        self.assertEqual(data["a"]["tokens"]["TOTAL"]["cache_write"], 60_000)
        self.assertAlmostEqual(data["b"]["cost_per_defect"]["cart"], 1.0)
        self.assertEqual(data["b"]["versus"]["against"], "a")
        self.assertEqual(data["b"]["versus"]["rows"][-1]["tokens"], "x0.50")
        self.assertNotIn("versus", data["a"])

    def test_a_case_with_no_defect_found_has_no_cost_per_defect(self) -> None:
        results = self.results
        path = results.write(results.record("a", 1, tokens=SESSION))
        code, out, _ = results.main("compare", str(path))
        self.assertEqual(code, 0)
        row = next(line for line in out.splitlines() if line.startswith("cart"))
        self.assertRegex(row, r" 1\.0M +- *$")

    def test_session_tokens_come_from_the_model_usage_alone(self) -> None:
        record = self.results.record("a", 1, tokens=SESSION)
        self.assertEqual(run_compare.session_tokens(record), SESSION)
        # `usage` is only the last turn of the main loop: without model usage the record has no tokens.
        self.assertEqual(record["usage"], {"input_tokens": 1, "output_tokens": 1})
        for empty in ({}, None, {"claude-sonnet-5-5": {"costUSD": 1.0}}):
            with self.subTest(model_usage=empty):
                record["model_usage"] = empty
                self.assertIsNone(run_compare.session_tokens(record))
        del record["model_usage"]
        self.assertIsNone(run_compare.session_tokens(record))

    def test_a_record_with_only_usage_is_counted_without_tokens(self) -> None:
        results = self.results
        record = results.record("a", 1, tokens=SESSION)
        record["model_usage"] = {}
        path = results.write(record)
        code, out, _ = results.main("compare", str(path))
        self.assertEqual(code, 0)
        self.assertIn("1 without tokens", out)


if __name__ == "__main__":
    unittest.main()
