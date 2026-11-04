"""Tests for the 2026-11-04 bug fixes. Run from nfl-picks-app/:
    python -m unittest discover -s tests -v
No network, no Streamlit needed.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from nflpicks import config, model


def row(home, away, spread, div="0"):
    return pd.Series({
        "week": 9, "home_team": home, "away_team": away,
        "spread_line": spread, "total_line": 45.0,
        "home_rest": 7, "away_rest": 7, "div_game": div,
        "location": "Home", "gameday": "2026-11-08", "roof": "outdoors",
    })


RATINGS = {"KC": 5.0, "DEN": -5.0}
GP = {"KC": 8, "DEN": 8}


class TestSpreadSign(unittest.TestCase):
    def test_sportsbook_to_nflverse_roundtrip(self):
        # Home favorite -3.5 sportsbook == +3.5 nflverse, and back.
        self.assertEqual(config.to_nflverse_spread(-3.5), 3.5)
        self.assertEqual(config.to_sportsbook_spread(3.5), -3.5)

    def test_pool_override_in_sportsbook_notation(self):
        # The old bug: entering -6.5 (home favorite, sportsbook style)
        # directly into the DB flipped the pick to KC +6.5. The dashboard
        # now converts on save, so the DB value is +6.5 nflverse and the
        # pick must be KC -6.5 (home favorite), not KC +6.5.
        p = model.predict_game(row("KC", "DEN", 3.0), RATINGS, GP, 2.0,
                               pool_spread=config.to_nflverse_spread(-6.5))
        self.assertTrue(p.pick.startswith("KC -"), p.pick)


class TestEdgeFlags(unittest.TestCase):
    def test_strong_edge_from_raw_deviation(self):
        # Independent margin 6, line 3: raw deviation 3, blended edge
        # only 0.9. Strong-edge must fire on the raw gap (>=3, <8),
        # even though the blended edge is below 3.
        p = model.predict_game(row("KC", "DEN", 3.0),
                               {"KC": 2.0, "DEN": -2.0}, GP, 2.0)
        self.assertEqual(p.model_independent, 6.0)
        self.assertTrue(p.strong_edge)
        self.assertFalse(p.needs_review)

    def test_needs_review_from_raw_deviation(self):
        p = model.predict_game(row("KC", "DEN", 3.0),
                               {"KC": 20.0, "DEN": -20.0}, GP, 2.0)
        self.assertTrue(p.needs_review)


class TestDivGame(unittest.TestCase):
    def test_div_game_forms(self):
        # int, str, float, and bool 1 must all get the divisional multiplier.
        # DIV_MULTIPLIER stays 0.9 (see config.py): harness ablation
        # div09 vs div1 was noise (paired z=1.13), so the original value
        # is kept rather than changed on weak evidence.
        for div in (1, "1", 1.0, True):
            p = model.predict_game(row("KC", "DEN", 3.0, div=div), RATINGS, GP, 2.0)
            self.assertEqual(p.margin_model, 10.8, div)  # (5 - -5 + 2) * 0.9
        p = model.predict_game(row("KC", "DEN", 3.0, div=0), RATINGS, GP, 2.0)
        self.assertEqual(p.margin_model, 12.0)


class TestCoverProb(unittest.TestCase):
    def test_cover_prob_above_half_for_pick(self):
        p = model.predict_game(row("KC", "DEN", 3.0), RATINGS, GP, 2.0)
        self.assertIsNotNone(p.cover_prob)
        self.assertGreater(p.cover_prob, 0.5)
        self.assertLessEqual(p.cover_prob, 1.0)


if __name__ == "__main__":
    unittest.main()
