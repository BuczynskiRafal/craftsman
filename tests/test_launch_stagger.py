"""Tests for holding back Craftsman agents that start together (`craftsman.launch_stagger`)."""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest import mock

from craftsman.diffscan import git_budget
from craftsman.launch_stagger import STAGGER_S, forget, hold_back, release, stagger_s
from craftsman.records import AgentEvent, format_timestamp

NOW = datetime(2026, 11, 10, 9, 0, tzinfo=timezone.utc)


def start(
    before_s: float, agent: str = "craftsman-reviewer", platform: str = "claude", agent_id: str = "a"
) -> AgentEvent:
    """A start of `agent` `before_s` seconds before `NOW`."""
    return AgentEvent(
        platform=platform,
        event="start",
        agent=agent,
        agent_id=agent_id,
        at=format_timestamp(NOW - timedelta(seconds=before_s)),
    )


def wait(
    agents: list[AgentEvent], agent: str = "craftsman-reviewer", platform: str = "claude", agent_id: str = "new"
) -> float:
    """`stagger_s` of a start of `agent` at `NOW` after `agents`."""
    return stagger_s(agents, start(0, agent, platform, agent_id))


class StaggerTests(unittest.TestCase):
    def test_the_stagger_is_five_seconds(self) -> None:
        self.assertEqual(STAGGER_S, 5.0)

    def test_the_first_start_of_a_burst_and_a_start_on_an_empty_ledger_do_not_wait(self) -> None:
        self.assertEqual(wait([]), 0.0)
        stopped = replace(start(1), event="stop")
        self.assertEqual(wait([stopped]), 0.0)

    def test_followers_wait_until_the_earliest_start_of_the_burst_plus_the_stagger(self) -> None:
        self.assertEqual(wait([start(1.5)]), 3.5)
        self.assertEqual(wait([start(1.5), start(0.5)]), 3.5)
        self.assertEqual(wait([start(0.5), start(1.5)]), 3.5)
        verifiers = [start(2, "craftsman-verifier"), start(1, "Craftsman Verifier")]
        self.assertEqual(wait(verifiers, "craftsman-verifier"), 3.0)

    def test_a_start_at_the_same_moment_waits_the_whole_stagger_and_never_longer(self) -> None:
        self.assertEqual(wait([start(0)]), STAGGER_S)
        self.assertEqual(wait([start(0), start(0)]), STAGGER_S)

    def test_a_start_just_outside_the_window_holds_nothing_back(self) -> None:
        self.assertEqual(wait([start(STAGGER_S)]), 0.0)
        self.assertEqual(wait([start(STAGGER_S + 0.001)]), 0.0)
        self.assertEqual(wait([start(-STAGGER_S)]), 0.0)
        self.assertAlmostEqual(wait([start(STAGGER_S - 0.001)]), 0.001)

    def test_a_start_recorded_earlier_but_stamped_a_little_later_holds_back_the_whole_stagger(self) -> None:
        # Hooks of one burst read the clock before they queue for the ledger lock.
        self.assertEqual(wait([start(-0.001)]), STAGGER_S)
        self.assertEqual(wait([start(-1), start(1)]), 4.0)
        self.assertEqual(wait([start(-(STAGGER_S - 0.001))]), STAGGER_S)

    def test_a_resumed_agent_does_not_wait(self) -> None:
        self.assertEqual(wait([start(1, agent_id="r-1")], agent_id="r-1"), 0.0)
        self.assertEqual(wait([start(1, agent_id="r-1")], agent_id="r-2"), 4.0)
        self.assertEqual(wait([start(1, agent_id="")], agent_id=""), 4.0)  # no id: never taken for a resume

    def test_an_agent_without_a_craftsman_role_does_not_wait(self) -> None:
        self.assertEqual(wait([start(1, "Explore")], "Explore"), 0.0)

    def test_another_role_or_agent_does_not_hold_a_start_back(self) -> None:
        self.assertEqual(wait([start(1, "craftsman-verifier")]), 0.0)
        self.assertEqual(wait([start(1)], "craftsman-verifier"), 0.0)
        self.assertEqual(wait([start(1, "Explore")]), 0.0)

    def test_only_claude_code_starts_are_held_back_and_only_by_starts_on_it(self) -> None:
        self.assertEqual(wait([start(1, platform="copilot")]), 0.0)
        self.assertEqual(wait([start(1, platform="copilot")], platform="copilot"), 0.0)
        self.assertEqual(wait([start(1)], platform="copilot"), 0.0)

    def test_starts_without_agent_ids_count(self) -> None:
        self.assertEqual(wait([start(2, agent_id="")]), 3.0)


class HoldBackTests(unittest.TestCase):
    def setUp(self) -> None:
        patch = mock.patch("craftsman.launch_stagger.sleep")
        self.sleep = patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(release)  # nothing held back leaks into another test

    def test_release_waits_what_the_start_was_held_back_for_once(self) -> None:
        hold_back([start(1)], start(0, agent_id="new"))
        release()
        release()

        self.sleep.assert_called_once_with(4.0)

    def test_a_forgotten_wait_is_not_waited(self) -> None:
        hold_back([start(1)], start(0, agent_id="new"))
        forget()
        release()

        self.sleep.assert_not_called()

    def test_nothing_held_back_means_no_wait(self) -> None:
        hold_back([], start(0, agent_id="new"))
        release()

        self.sleep.assert_not_called()

    def test_an_error_means_no_wait_and_is_not_raised(self) -> None:
        with mock.patch("craftsman.launch_stagger.parse_timestamp", side_effect=ValueError("not a time")):
            hold_back([start(1)], start(0, agent_id="new"))
        release()
        self.sleep.assert_not_called()

        hold_back([start(1)], start(0, agent_id="new"))
        self.sleep.side_effect = OSError("interrupted")
        release()
        self.sleep.assert_called_once_with(4.0)

    def test_the_wait_ends_within_the_hooks_git_budget(self) -> None:
        timeouts: list[str] = []
        with git_budget(2.0, on_timeout=timeouts.append):
            hold_back([start(0)], start(0, agent_id="new"))
            release()
        (waited,), _ = self.sleep.call_args
        self.assertLessEqual(waited, 2.0)
        self.assertGreater(waited, 1.0)

        with git_budget(0.0, on_timeout=timeouts.append):
            hold_back([start(0)], start(0, agent_id="new"))
            release()
        self.assertEqual(self.sleep.call_count, 1)  # a spent budget leaves no time to wait
        self.assertEqual(timeouts, [])  # and the wait is no git call that ran out of time


if __name__ == "__main__":
    unittest.main()
