import unittest
from datetime import date
from unittest import mock

from hype_index import sources

SRC = {"type": "riftbound_official_events", "api_base": "https://api.example/api/v2",
       "referer": "https://locator.example/", "game_slug": "riftbound", "lag_days": 2}


def fake_response(payload):
    r = mock.Mock(status_code=200)
    r.json.return_value = payload
    return r


class OfficialLocatorTests(unittest.TestCase):
    def setUp(self):
        sources._official_cache.clear()

    def test_paginates_and_shares_one_crawl(self):
        pages = [
            {"count": 3, "next_page_number": 2,
             "results": [{"starting_player_count": 8}, {"starting_player_count": 16}]},
            {"count": 3, "next_page_number": None, "results": [{"starting_player_count": None}]},
        ]
        with mock.patch.object(sources.requests, "get", side_effect=[fake_response(p) for p in pages]) as get:
            events = sources.riftbound_official_events(SRC, date(2026, 9, 22), {})
            players = sources.riftbound_official_players(dict(SRC, type="riftbound_official_players"),
                                                         date(2026, 9, 22), {})
        self.assertEqual(events, [(date(2026, 9, 20), 3.0)])     # lag_days=2
        self.assertEqual(players, [(date(2026, 9, 20), 24.0)])
        self.assertEqual(get.call_count, 2)                       # players reused the crawl
        params = dict((k, v) for k, v in get.call_args_list[0].kwargs["params"] if k != "display_statuses")
        self.assertEqual(params["game_slug"], "riftbound")
        self.assertEqual(params["start_date_after"], "2026-09-19T16:00:00Z")   # 00:00 SGT
        self.assertEqual(params["start_date_before"], "2026-09-20T16:00:00Z")

    def test_partial_crawl_is_an_error_not_a_low_count(self):
        page = {"count": 5, "next_page_number": None, "results": [{}, {}]}
        with mock.patch.object(sources.requests, "get", return_value=fake_response(page)):
            with self.assertRaises(sources.SourceError):
                sources.riftbound_official_events(SRC, date(2026, 9, 22), {})


if __name__ == "__main__":
    unittest.main()
