import unittest
from datetime import datetime, timedelta, timezone

from poller import MatchStateMachine
from sources.threesixtyfive import (
    build_athlete_image_url,
    build_competitor_image_url,
    extract_statistics_from_game,
)


class MatchAssetsAndStatisticsTests(unittest.TestCase):
    def test_365scores_image_urls_use_source_ids_and_versions(self):
        self.assertIn("/v14/Competitors/5071", build_competitor_image_url(5071, 14))
        self.assertIn("/v25/Athletes/276", build_athlete_image_url(276, 25))
        self.assertIsNone(build_athlete_image_url(None, 1))

    def test_statistics_are_joined_to_home_and_away_competitors(self):
        game = {
            "id": 4627864,
            "gameTime": 67,
            "homeCompetitor": {"id": 5071},
            "awayCompetitor": {"id": 5028},
        }
        payload = {
            "competitors": [{"id": 5071}, {"id": 5028}],
            "statistics": [
                {"competitorId": 5071, "name": "Ball Possession", "value": "56%"},
                {"competitorId": 5028, "name": "Ball Possession", "value": "44%"},
                {"competitorId": 5071, "name": "Total Shots", "value": "12"},
                {"competitorId": 5028, "name": "Total Shots", "value": "7"},
                {"competitorId": 5071, "name": "Shots on Goal", "value": "5"},
                {"competitorId": 5028, "name": "Shots on Goal", "value": "2"},
            ],
        }

        result = extract_statistics_from_game(game, payload)

        self.assertEqual(result["minute"], 67)
        self.assertEqual(result["home"]["possession"], 56)
        self.assertEqual(result["away"]["possession"], 44)
        self.assertEqual(result["home"]["shots"], 12)
        self.assertEqual(result["away"]["shots_on_target"], 2)

    def test_missing_statistics_are_not_fabricated_as_zero(self):
        game = {
            "id": 4627864,
            "homeCompetitor": {"id": 5071},
            "awayCompetitor": {"id": 5028},
        }
        self.assertIsNone(extract_statistics_from_game(game, {"statistics": []}))

    def test_live_statistics_refresh_every_thirty_seconds(self):
        state = MatchStateMachine(store=object(), forwarder=object())
        self.assertTrue(state.should_forward_statistics("fixture-1", "live"))
        state.mark_stats_forwarded("fixture-1", "live")
        self.assertFalse(state.should_forward_statistics("fixture-1", "live"))
        state.last_statistics_fetch["fixture-1"] = datetime.now(timezone.utc) - timedelta(seconds=31)
        self.assertTrue(state.should_forward_statistics("fixture-1", "live"))
        self.assertTrue(state.should_forward_statistics("fixture-1", "halftime"))


if __name__ == "__main__":
    unittest.main()
