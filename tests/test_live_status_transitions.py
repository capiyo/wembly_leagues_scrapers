"""Regression tests for fixture status transitions around kickoff."""

import unittest
from datetime import datetime, timedelta, timezone

from poller import MatchStateMachine


class MatchStateMachineStatusTests(unittest.TestCase):
    def setUp(self):
        self.machine = MatchStateMachine(store=None, forwarder=None)

    def test_fixture_within_one_hour_is_soon(self):
        kickoff = datetime.now(timezone.utc) + timedelta(minutes=59)
        match = {"kickoffUtc": kickoff.isoformat(), "status": "upcoming"}

        self.assertEqual(self.machine.determine_state(match), "soon")

    def test_passed_kickoff_does_not_promote_fixture_to_live(self):
        kickoff = datetime.now(timezone.utc) - timedelta(minutes=5)
        match = {"kickoffUtc": kickoff.isoformat(), "status": "soon"}

        state = self.machine.determine_state(match)
        match["minutes_to_kickoff"] = -5
        match["_state"] = state

        self.assertEqual(state, "soon")
        self.assertIsNone(self.machine.should_update_status(match))

    def test_confirmed_live_state_is_preserved(self):
        kickoff = datetime.now(timezone.utc) - timedelta(minutes=5)
        match = {"kickoffUtc": kickoff.isoformat(), "status": "live"}

        self.assertEqual(self.machine.determine_state(match), "live")
        self.assertIsNone(self.machine.should_update_status({
            **match,
            "minutes_to_kickoff": -5,
            "_state": "live",
        }))

    def test_completed_state_is_preserved(self):
        match = {"status": "completed"}

        self.assertEqual(self.machine.determine_state(match), "completed")
        self.assertIsNone(self.machine.should_update_status(match))


if __name__ == "__main__":
    unittest.main()
