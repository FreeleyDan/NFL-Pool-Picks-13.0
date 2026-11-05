"""Tests for item 8: SRS-style totals model (shadow, old blend stays live).
Run from nfl-picks-app/: python -m unittest discover -s tests -v
No network, no real games.csv needed -- all fixtures are synthetic.
"""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from nflpicks import backtest, config, model, scoring


def _frame(rows):
    """rows: (season, week, home, away, hs, as_)."""
    return pd.DataFrame([{
        "season": s, "week": w, "game_type": "REG", "gameday": f"{s}-09-01",
        "home_team": h, "away_team": a, "home_score": hs, "away_score": as_,
        "result": hs - as_, "spread_line": 0.0, "total_line": 44.0,
        "home_rest": 7, "away_rest": 7, "div_game": 0, "location": "Home",
        "roof": "outdoors",
    } for s, w, h, a, hs, as_ in rows])


def _row(home, away):
    return pd.Series({
        "week": 9, "home_team": home, "away_team": away,
        "spread_line": 3.0, "total_line": 45.0,
        "home_rest": 7, "away_rest": 7, "div_game": 0,
        "location": "Home", "gameday": "2026-11-08", "roof": "outdoors",
    })


class TestSrsSolve(unittest.TestCase):
    def test_strong_offense_and_good_defense_rank_first(self):
        # A scores 30 and concedes 10 every game; B is the mirror image.
        games = _frame([
            (2024, w, "A", "B", 30, 10, ) for w in range(1, 6)
        ] + [
            (2024, w, "B", "A", 10, 30, ) for w in range(6, 11)
        ])
        off, deff, lg = scoring._srs_offdef_solve(games, 2.0)
        self.assertGreater(off["A"], off["B"])
        self.assertGreater(deff["A"], deff["B"])  # def positive = good defense
        self.assertAlmostEqual(lg, 20.0)

    def test_predict_total_srs_formula(self):
        # league 22, off A +5 / B -5, def A +3 / B -3:
        # home A: 22+5-(-3)=30; away B: 22-5-3=14; total 44.
        total = scoring.predict_total_srs(
            "A", "B", {"A": 5.0, "B": -5.0}, {"A": 3.0, "B": -3.0}, 22.0)
        self.assertEqual(total, 44.0)

    def test_predict_total_srs_missing_team_defaults_to_league(self):
        self.assertEqual(
            scoring.predict_total_srs("ZZ", "YY", {}, {}, 22.0), 44.0)

    def test_empty_inputs_fall_back_to_league_average(self):
        off, deff, lg = scoring.blended_srs_scoring(
            pd.DataFrame(), pd.DataFrame(), 2.0)
        self.assertEqual(off, {})
        self.assertEqual(lg, config.LEAGUE_AVG_POINTS)


class TestSrsBlending(unittest.TestCase):
    def test_no_current_games_uses_regressed_prior(self):
        prior = _frame([(2024, w, "A", "B", 30, 10) for w in range(1, 18)])
        off_p, def_p, _ = scoring._srs_offdef_solve(prior, 2.0)
        off, deff, _ = scoring.blended_srs_scoring(pd.DataFrame(), prior, 2.0)
        self.assertAlmostEqual(off["A"], off_p["A"] * config.PRIOR_REGRESSION, places=6)
        self.assertAlmostEqual(deff["A"], def_p["A"] * config.PRIOR_REGRESSION, places=6)

    def test_full_current_season_ignores_prior(self):
        # 17 current games (fade saturates at 12) in one direction, prior
        # in the opposite direction: blended must equal the current solve.
        cur = _frame([(2025, w, "A", "B", 30, 10) for w in range(1, 18)])
        prior = _frame([(2024, w, "A", "B", 10, 30) for w in range(1, 18)])
        off_c, def_c, _ = scoring._srs_offdef_solve(cur, 2.0)
        off, deff, _ = scoring.blended_srs_scoring(cur, prior, 2.0)
        self.assertAlmostEqual(off["A"], off_c["A"], places=6)
        self.assertAlmostEqual(deff["A"], def_c["A"], places=6)


class TestModelWiring(unittest.TestCase):
    def test_srs_shadow_by_default_old_blend_live(self):
        # TOTALS_SRS_LIVE must stay False, and even with SRS ratings
        # supplied, predict_game must use the old blend by default.
        self.assertFalse(config.TOTALS_SRS_LIVE)
        pts_for = {"KC": 30.0, "DEN": 20.0}
        pts_against = {"KC": 20.0, "DEN": 30.0}
        p = model.predict_game(
            _row("KC", "DEN"), {"KC": 0.0, "DEN": 0.0}, {"KC": 8, "DEN": 8}, 2.0,
            pts_for=pts_for, pts_against=pts_against,
            totals_off={"KC": 100.0, "DEN": -100.0},
            totals_def={"KC": 100.0, "DEN": -100.0}, totals_league_avg=22.0)
        # Old blend: (30+30)/2 + (20+20)/2 = 50. SRS would be wildly different.
        self.assertEqual(p.model_total, 50.0)

    def test_flag_on_uses_srs_and_weather_still_applies(self):
        old_flag = config.TOTALS_SRS_LIVE
        config.TOTALS_SRS_LIVE = True
        try:
            pts_for = {"KC": 30.0, "DEN": 20.0}
            pts_against = {"KC": 20.0, "DEN": 30.0}
            # SRS total without weather: 22+5-(-3) + 22-5-3 = 44.
            # High wind (25mph) subtracts 2.7 (WIND_HIGH_TOTAL_ADJ).
            p = model.predict_game(
                _row("KC", "DEN"), {"KC": 0.0, "DEN": 0.0}, {"KC": 8, "DEN": 8}, 2.0,
                pts_for=pts_for, pts_against=pts_against,
                totals_off={"KC": 5.0, "DEN": -5.0},
                totals_def={"KC": 3.0, "DEN": -3.0}, totals_league_avg=22.0,
                weather_data={"wind_mph": 25.0, "temp_f": 60.0, "precip_mm": 0.0})
            self.assertEqual(p.model_total, 41.3)
            self.assertIn("wind", p.weather_note)
        finally:
            config.TOTALS_SRS_LIVE = old_flag

    def test_flag_on_without_srs_ratings_falls_back_to_old(self):
        old_flag = config.TOTALS_SRS_LIVE
        config.TOTALS_SRS_LIVE = True
        try:
            p = model.predict_game(
                _row("KC", "DEN"), {"KC": 0.0, "DEN": 0.0}, {"KC": 8, "DEN": 8}, 2.0,
                pts_for={"KC": 30.0, "DEN": 20.0},
                pts_against={"KC": 20.0, "DEN": 30.0})
            self.assertEqual(p.model_total, 50.0)
        finally:
            config.TOTALS_SRS_LIVE = old_flag


class TestTotalsHarness(unittest.TestCase):
    def test_point_in_time_no_lookahead(self):
        prior = [(2024, w, "HOME", "AWAY", 30, 10) for w in range(1, 4)]
        wk1 = [(2025, 1, "HOME", "AWAY", 20, 10)]
        later = [(2025, 2, "AWAY", "HOME", 40, 0)]
        g1 = backtest.load_games_frame(_frame(prior + wk1))
        g2 = backtest.load_games_frame(_frame(prior + wk1 + later))
        s1 = backtest.evaluate_totals(g1, [2025])
        s2 = backtest.evaluate_totals(g2, [2025])
        r1 = s1["_games"][s1["_games"]["week"] == 1].iloc[0]
        r2 = s2["_games"][s2["_games"]["week"] == 1].iloc[0]
        self.assertEqual(r1["srs_total"], r2["srs_total"])
        self.assertEqual(r1["old_total"], r2["old_total"])

    def test_harness_summary_shape_and_in_sample_flag(self):
        games = backtest.load_games_frame(_frame(
            [(2024, w, "A", "B", 24, 17) for w in range(1, 6)]
            + [(2025, w, "A", "B", 24, 17) for w in range(1, 6)]
            + [(2025, w, "B", "A", 20, 21) for w in range(6, 11)]))
        oos = backtest.evaluate_totals(games, [2025])
        ins = backtest.evaluate_totals(games, [2025], in_sample=True)
        self.assertFalse(oos["in_sample"])
        self.assertTrue(ins["in_sample"])
        self.assertGreater(oos["n"], 0)
        self.assertEqual(oos["n"], ins["n"])
        self.assertIn("2025", oos["by_season"])
        for key in ("old_mae", "srs_mae", "line_mae"):
            self.assertIn(key, oos)
            self.assertGreaterEqual(oos[key], 0)


if __name__ == "__main__":
    unittest.main()
