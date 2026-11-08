"""Tests for the 2026-11-08 turnover-adjustment removal (Week 9 test
follow-up). The walk-forward test that week (fade 6/10/12 x turnover
on/off, 2019-2026, point-in-time, recorded lines) found the
turnover-luck adjustment costs ATS at the live fade-12 -- 49.42%
2019-2025 with it on vs 50.03% off, 51.64% vs 54.47% in 2026 --
with a worse MAE in all three fades, and no demonstrated benefit
in any fade. So the live pick path now leaves it off
(config.TURNOVER_LUCK_LIVE=False), making live == backtest for
spread picks. These tests are offline: synthetic team-stats rows,
no games.csv, no network. Run from nfl-picks-app/:
    python -m unittest discover -s tests -v
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from nflpicks import config, data
import weekly_run


def _team_stats():
    """8 rows per team (full sample confidence = 8 games).
    KC: +2 turnover margin per game (very lucky) -> adjustment
    should be -1.5 * 4.5 = -6.75, capped at -3.0.
    BUF: -2 per game (very unlucky) -> +6.75, capped at +3.0.
    DAL: 0 margin -> 0.0 adjustment."""
    rows = []
    for _ in range(8):
        rows.append({"team": "KC", "passing_interceptions": 0,
                     "sack_fumbles_lost": 0, "rushing_fumbles_lost": 0,
                     "receiving_fumbles_lost": 0, "def_interceptions": 2,
                     "fumble_recovery_opp": 0})
        rows.append({"team": "BUF", "passing_interceptions": 2,
                     "sack_fumbles_lost": 0, "rushing_fumbles_lost": 0,
                     "receiving_fumbles_lost": 0, "def_interceptions": 0,
                     "fumble_recovery_opp": 0})
        rows.append({"team": "DAL", "passing_interceptions": 0,
                     "sack_fumbles_lost": 0, "rushing_fumbles_lost": 0,
                     "receiving_fumbles_lost": 0, "def_interceptions": 0,
                     "fumble_recovery_opp": 0})
    return pd.DataFrame(rows)


RATINGS = {"KC": 5.0, "BUF": -2.0, "DAL": 1.0, "MIA": 0.0}


class TestTurnoverGate(unittest.TestCase):
    def setUp(self):
        data.reset_diagnostics()

    def test_flag_defaults_off(self):
        self.assertIs(config.TURNOVER_LUCK_LIVE, False)

    def test_helper_off_applies_nothing(self):
        ratings = dict(RATINGS)
        with mock.patch.object(config, "TURNOVER_LUCK_LIVE", False):
            adj = weekly_run._apply_turnover_adjustment(ratings, _team_stats())
        self.assertEqual(adj, {})
        self.assertEqual(ratings, RATINGS)  # untouched, incl. MIA (no stats)
        note = " ".join(d["detail"] for d in data.DIAGNOSTICS
                        if d["step"] == "Turnover luck regression")
        self.assertIn("OFF", note)
        self.assertIn("live == backtest", note)

    def test_helper_on_applies_capped_adjustment(self):
        ratings = dict(RATINGS)
        with mock.patch.object(config, "TURNOVER_LUCK_LIVE", True):
            adj = weekly_run._apply_turnover_adjustment(ratings, _team_stats())
        self.assertEqual(adj, {"KC": -3.0, "BUF": 3.0, "DAL": 0.0})
        self.assertEqual(ratings["KC"], 2.0)   # 5.0 - 3.0 (cap, not -6.75)
        self.assertEqual(ratings["BUF"], 1.0)  # -2.0 + 3.0 (cap, not +6.75)
        self.assertEqual(ratings["DAL"], 1.0)  # zero margin -> unchanged
        self.assertEqual(ratings["MIA"], 0.0)  # no stats -> unchanged

    def test_helper_on_with_no_stats_applies_nothing(self):
        for stats in (None, pd.DataFrame()):
            ratings = dict(RATINGS)
            with mock.patch.object(config, "TURNOVER_LUCK_LIVE", True):
                adj = weekly_run._apply_turnover_adjustment(ratings, stats)
            self.assertEqual(adj, {})
            self.assertEqual(ratings, RATINGS)


if __name__ == "__main__":
    unittest.main()
