"""Tests for the 2026-11-04 backtest items 1-7 (stdlib unittest, no network)."""
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pandas as pd

from nflpicks import backtest, calibration, config, db, injuries, model, ratings


def _games(season_games):
    """season_games: list of dicts with season/week/home/away/scores/line."""
    rows = []
    for g in season_games:
        rows.append({
            "season": g["season"], "week": g["week"], "game_type": "REG",
            "gameday": f"{g['season']}-09-01", "home_team": g["home"], "away_team": g["away"],
            "home_score": g["hs"], "away_score": g["as_"], "result": g["hs"] - g["as_"],
            "spread_line": g["line"], "total_line": 44.0,
            "home_rest": 7, "away_rest": 7, "div_game": 0, "location": "Home", "roof": "outdoors",
        })
    return pd.DataFrame(rows)


class TestBacktestHarness(unittest.TestCase):
    def test_exclude_backfill(self):
        rows = [{"is_backfill": 0}, {"is_backfill": 1}, {"is_backfill": 0}]
        self.assertEqual(len(backtest.exclude_backfill_rows(rows)), 2)

    def test_point_in_time_no_lookahead(self):
        # Prior season: HOME strong (+20 every game). Current week 1:
        # HOME vs AWAY, line 0. Later current-season games reverse
        # (AWAY blows out HOME) -- week 1 prediction must not change.
        prior = [{"season": 2024, "week": w, "home": "HOME", "away": "AWAY",
                  "hs": 30, "as_": 10, "line": 0.0} for w in range(1, 4)]
        wk1 = [{"season": 2025, "week": 1, "home": "HOME", "away": "AWAY",
                "hs": 20, "as_": 10, "line": 0.0}]
        later = [{"season": 2025, "week": 2, "home": "AWAY", "away": "HOME",
                  "hs": 40, "as_": 0, "line": 0.0}]
        games = backtest.load_games_frame(_games(prior + wk1))
        games_later = backtest.load_games_frame(_games(prior + wk1 + later))
        s1 = backtest.evaluate_variant(games, [2025], "baseline")
        s2 = backtest.evaluate_variant(games_later, [2025], "baseline")
        # Week 1 row is the only evaluated game in both (week 2 has no
        # later week to evaluate against in s1, and in s2 week 2 is
        # evaluated too -- so compare only the week-1 row).
        g1 = s1["_games"][s1["_games"]["week"] == 1].iloc[0]
        g2 = s2["_games"][s2["_games"]["week"] == 1].iloc[0]
        self.assertEqual(g1["model_margin"], g2["model_margin"])
        self.assertGreater(g1["model_margin"], 0)  # prior strength carries

    def test_srs_converges_disconnected_graph(self):
        # Two isolated pairs (week-2 style): undamped SRS did not converge.
        games = _games([
            {"season": 2025, "week": 1, "home": "A", "away": "B", "hs": 20, "as_": 10, "line": 0.0},
            {"season": 2025, "week": 1, "home": "C", "away": "D", "hs": 24, "as_": 14, "line": 0.0},
        ])
        ratings.srs_ratings(games, 2.0)
        self.assertTrue(ratings.LAST_SRS_CONVERGED)

    def test_offdef_diagnostic_runs(self):
        games = _games([
            {"season": 2025, "week": w, "home": "A", "away": "B", "hs": 30, "as_": 10, "line": 0.0}
            for w in range(1, 6)
        ] + [
            {"season": 2025, "week": w, "home": "B", "away": "A", "hs": 20, "as_": 17, "line": 0.0}
            for w in range(6, 9)
        ])
        off, deff = backtest._offdef_ratings(games, 2.0)
        self.assertIn("A", off)
        self.assertGreater(off["A"], off["B"])  # A scores more


class TestCalibration(unittest.TestCase):
    def test_cover_prob_monotonic_and_near_half_at_line(self):
        p0 = calibration.cover_probability_home(0.0, 0.0)
        self.assertAlmostEqual(p0, 0.5, delta=0.05)
        self.assertLess(calibration.cover_probability_home(-10.0, 0.0),
                        calibration.cover_probability_home(10.0, 0.0))
        # Shrunk: a raw 8-pt edge is worth ~5.8 pts, so prob < normal approx.
        self.assertLess(calibration.cover_probability_home(8.0, 0.0), 0.75)

    def test_shrunk_margin(self):
        self.assertAlmostEqual(calibration.shrunk_margin(10.0), 0.36 + 6.8, places=2)


class TestConfigAndModel(unittest.TestCase):
    def test_div_multiplier_kept_at_original(self):
        # Harness ablation div09 vs div1 was noise (paired z=1.13), so
        # the original 0.9 stays; see config.py comment.
        self.assertEqual(config.DIV_MULTIPLIER, 0.9)
        self.assertEqual(config.PRIOR_FADE_GAMES, 12)

    def test_travel_off_by_default(self):
        self.assertFalse(config.TRAVEL_ENABLED)
        self.assertEqual(model._travel_adjustment("KC", "DEN"), 0.0)

    def test_qb_change_review_only(self):
        self.assertEqual(injuries.qb_change_suggestion("A", "A")["suggested_pts"], 0.0)
        r = injuries.qb_change_suggestion("Starter", "Backup")
        self.assertEqual(r["suggested_pts"], config.QB_CHANGE_REVIEW_PTS)
        self.assertFalse(config.INJ_AUTO_APPLY)


class TestLineMoves(unittest.TestCase):
    def test_snapshot_and_edge_flip(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "t.db")
            db.init_db(path)
            pred = SimpleNamespace(
                week=1, home="KC", away="DEN", vegas_spread=3.0, model_spread=3.0,
                edge=0.0, pick="KC -3", confidence=1.0, strong_edge=False,
                epa_shadow=None, injury_flags="", manual_adj_home=0.0,
                manual_adj_away=0.0, vegas_total=None, model_total=None,
                total_pick=None, needs_review=False, model_independent=0.0,
                weather_note="", pool_spread=-3.0, pool_total=None, cover_prob=0.6,
            )
            db.save_picks(2026, [pred], db_path=path)
            # Model 0.0 vs pool line -3.0 gives edge +3.0; vs closing
            # +3.0 gives edge -3.0: the edge flips sign between lines.
            db.save_line_snapshot(2026, 1, "KC", "DEN", "pool", -3.0, db_path=path)
            db.save_line_snapshot(2026, 1, "KC", "DEN", "closing", 3.0, db_path=path)
            moves = db.get_line_moves(2026, 1, db_path=path)
            self.assertEqual(len(moves), 1)
            self.assertTrue(moves[0]["edge_flip"])
            self.assertEqual(moves[0]["movement"], 6.0)


class TestEpaCalibration(unittest.TestCase):
    def test_epa_scale_applied(self):
        # Two teams, one week each: A EPA +10, B EPA -10 (per-game totals).
        df = pd.DataFrame([
            {"team": "A", "passing_epa": 6.0, "rushing_epa": 4.0, "opponent_team": "B"},
            {"team": "B", "passing_epa": -6.0, "rushing_epa": -4.0, "opponent_team": "A"},
        ])
        out = __import__("nflpicks.epa", fromlist=["weighted_epa_ratings"]).weighted_epa_ratings(df)
        self.assertIsNotNone(out["ratings"])
        # App-scale rating for A would be 1.6*10 - 1.0*(-10) = 26 (before
        # league-centering); calibrated must be much smaller in magnitude.
        self.assertLess(abs(out["ratings"]["A"]), 10.0)


if __name__ == "__main__":
    unittest.main()
