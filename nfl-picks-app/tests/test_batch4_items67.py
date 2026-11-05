"""Tests for batch 4, group C items 6-7 (play-by-play round 2,
rating uncertainty round 2). No network, no games.csv needed:
the full 2019-2025 numbers live in the README batch-4 group C
section. Run from nfl-picks-app/: python -m unittest discover
-s tests -v. No live behaviour changes: no flag is flipped,
no pick is changed, PBP_FEATURES_LIVE and UNCERTAINTY_PICK_USE
stay False.
"""
import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from nflpicks import backtest, config, pbp_round2 as pbp2, uncertainty


def _pbp_frame():
    """Two REG weeks for KC/DEN/BUF with known 3rd-down and ST plays.
    Week 1: KC converts 1/2 third downs vs DEN (DEN 0/1 allowed
    side is KC's own plays mirrored); ST: KC posteam EPA +1.0 on
    1 play, DEN posteam EPA -1.0 on 1 play."""
    rows = [
        # third down: KC 2 plays, 1 converts (first_down flag)
        {"season": 2024, "season_type": "REG", "week": 1, "posteam": "KC",
         "defteam": "DEN", "down": 3, "ydstogo": 10, "play_type": "pass",
         "yards_gained": 12, "first_down": 1, "touchdown": 0,
         "epa": 0.5, "special": 0},
        {"season": 2024, "season_type": "REG", "week": 1, "posteam": "KC",
         "defteam": "DEN", "down": 3, "ydstogo": 10, "play_type": "run",
         "yards_gained": 2, "first_down": 0, "touchdown": 0,
         "epa": -0.5, "special": 0},
        # third down: DEN 1 play, converts via yardage
        {"season": 2024, "season_type": "REG", "week": 1, "posteam": "DEN",
         "defteam": "KC", "down": 3, "ydstogo": 5, "play_type": "run",
         "yards_gained": 6, "first_down": 0, "touchdown": 0,
         "epa": 0.2, "special": 0},
        # ST plays (special == 1): KC +1.0, DEN -1.0 as posteam
        {"season": 2024, "season_type": "REG", "week": 1, "posteam": "KC",
         "defteam": "DEN", "down": 0, "ydstogo": 0, "play_type": "punt",
         "yards_gained": 0, "first_down": 0, "touchdown": 0,
         "epa": 1.0, "special": 1},
        {"season": 2024, "season_type": "REG", "week": 1, "posteam": "DEN",
         "defteam": "KC", "down": 0, "ydstogo": 0, "play_type": "punt",
         "yards_gained": 0, "first_down": 0, "touchdown": 0,
         "epa": -1.0, "special": 1},
        # POST game must not enter REG counts
        {"season": 2024, "season_type": "POST", "week": 19, "posteam": "KC",
         "defteam": "DEN", "down": 3, "ydstogo": 10, "play_type": "pass",
         "yards_gained": 20, "first_down": 1, "touchdown": 0,
         "epa": 9.0, "special": 0},
    ]
    return pd.DataFrame(rows)


class TestPbpRound2Counts(unittest.TestCase):
    def test_third_down_and_st_counts(self):
        counts = pbp2.weekly_team_counts2(_pbp_frame())
        by_team = {r["team"]: r for _, r in counts.iterrows()}
        # KC offense: 1/2 third downs; allowed: DEN converted 1/1 vs KC
        self.assertEqual(by_team["KC"]["third_num"], 1.0)
        self.assertEqual(by_team["KC"]["third_den"], 2.0)
        self.assertEqual(by_team["KC"]["third_allow_num"], 1.0)
        self.assertEqual(by_team["KC"]["third_allow_den"], 1.0)
        # ST: KC off mean +1.0, allowed = DEN's posteam EPA -1.0
        self.assertEqual(by_team["KC"]["st_epa_sum"], 1.0)
        self.assertEqual(by_team["KC"]["st_epa_n"], 1.0)
        self.assertEqual(by_team["KC"]["st_allow_epa_sum"], -1.0)

    def test_empty_or_weekless_frame_yields_nothing(self):
        self.assertTrue(pbp2.weekly_team_counts2(None).empty)
        self.assertTrue(pbp2.weekly_team_counts2(pd.DataFrame()).empty)
        no_week = _pbp_frame().drop(columns=["week"])
        self.assertTrue(pbp2.weekly_team_counts2(no_week).empty)

    def test_differential_signs(self):
        # KC off 0.5, allowed 1.0; DEN off 1.0, allowed 0.5.
        # Combined third = (0.5-1.0)-(1.0-0.5) = -1.0.
        # Off diff = 0.5-1.0 = -0.5; def diff = DEN allowed - KC
        # allowed = 0.5-1.0 = -0.5 (KC defense is worse here).
        rates = {"KC": {"third_off": 0.5, "third_allowed": 1.0,
                        "st_off": 1.0, "st_allowed": -1.0,
                        "early_off": 0.5, "early_allowed": 0.5,
                        "rz_off": 0.5, "rz_allowed": 0.5},
                 "DEN": {"third_off": 1.0, "third_allowed": 0.5,
                         "st_off": -1.0, "st_allowed": 1.0,
                         "early_off": 0.5, "early_allowed": 0.5,
                         "rz_off": 0.5, "rz_allowed": 0.5}}
        league = {k: 0.5 for k in rates["KC"]}
        league.update({"st_off": 0.0, "st_allowed": 0.0})
        d = pbp2.game_pbp2_differentials(rates, league, "KC", "DEN")
        self.assertAlmostEqual(d["third_down"], -1.0)
        self.assertAlmostEqual(d["third_down_off"], -0.5)
        self.assertAlmostEqual(d["third_down_def"], -0.5)
        # ST combined: KC strength 1-(-1)=2, DEN -1-1=-2, diff 4.
        self.assertAlmostEqual(d["st_epa"], 4.0)

    def test_missing_team_reads_as_league_average(self):
        rates = {"KC": {"third_off": 0.6, "third_allowed": 0.4,
                        "st_off": 0.1, "st_allowed": 0.0,
                        "early_off": 0.5, "early_allowed": 0.5,
                        "rz_off": 0.5, "rz_allowed": 0.5}}
        league = {"third_off": 0.4, "third_allowed": 0.4,
                  "st_off": 0.0, "st_allowed": 0.0,
                  "early_off": 0.5, "early_allowed": 0.5,
                  "rz_off": 0.5, "rz_allowed": 0.5}
        d = pbp2.game_pbp2_differentials(rates, league, "KC", "MIA")
        # MIA off/allowed both league: third off diff 0.2, def
        # diff = MIA allowed (0.4) - KC allowed (0.4) = 0.
        self.assertAlmostEqual(d["third_down_off"], 0.2)
        self.assertAlmostEqual(d["third_down_def"], 0.0)

    def test_fit_coeff_clips_and_zero_variation(self):
        rows = [{"third_down_diff": 0.1, "actual_margin": 10.0,
                 "model_margin": 0.0},
                {"third_down_diff": -0.1, "actual_margin": -10.0,
                 "model_margin": 0.0}]
        # LS coeff = (0.1*10 + -0.1*-10)/(0.01+0.01) = 100 -> at cap.
        self.assertAlmostEqual(pbp2.fit_pbp2_coeff(rows, "third_down"), 100.0)
        flat = [{"third_down_diff": 0.0, "actual_margin": 5.0,
                 "model_margin": 0.0}]
        self.assertEqual(pbp2.fit_pbp2_coeff(flat, "third_down"), 0.0)


class TestPlaceboGate(unittest.TestCase):
    def test_gate_requires_both_p95s(self):
        placebo = {"paired_delta_p95": 0.11, "ats_p95": 50.22}
        good = {"ats_pct": 51.0,
                "paired_vs_baseline": {"paired_delta_pp": 0.5}}
        self.assertTrue(pbp2.passes_placebo_gate(good, placebo))
        paired_only = {"ats_pct": 50.10,
                       "paired_vs_baseline": {"paired_delta_pp": 0.5}}
        self.assertFalse(pbp2.passes_placebo_gate(paired_only, placebo))
        ats_only = {"ats_pct": 51.0,
                    "paired_vs_baseline": {"paired_delta_pp": 0.05}}
        self.assertFalse(pbp2.passes_placebo_gate(ats_only, placebo))
        self.assertFalse(pbp2.passes_placebo_gate(
            {"ats_pct": 51.0, "paired_vs_baseline": {}}, placebo))


class TestUncertaintyRound2(unittest.TestCase):
    def test_graph_covariance_positive_and_narrows_margin(self):
        # One shared game: covariance must be positive (the
        # batch-3 docstring direction) and the margin SD with
        # it must be below the independent SD.
        res = uncertainty.rating_covariance_matrix(
            ["KC", "DEN"], [("KC", "DEN")])
        cov = uncertainty.covariance_between(res, "KC", "DEN")
        self.assertGreater(cov, 0.0)
        var_h, var_a = res["var"]["KC"], res["var"]["DEN"]
        with_cov = math.sqrt(var_h + var_a - 2 * cov)
        without = math.sqrt(var_h + var_a)
        self.assertLess(with_cov, without)
        # Correlation is modest for a single game, not ~1.
        self.assertLess(cov / math.sqrt(var_h * var_a), 0.5)

    def test_no_games_team_is_prior_only_uncorrelated(self):
        res = uncertainty.rating_covariance_matrix(
            ["KC", "MIA"], [("KC", "DEN")])
        self.assertAlmostEqual(res["var"]["MIA"], 25.0)
        self.assertEqual(uncertainty.covariance_between(res, "KC", "MIA"), 0.0)
        self.assertEqual(uncertainty.covariance_between(res, "KC", "XXX"), 0.0)

    def test_margin_sd_cov_reproduces_batch3_and_hfa_widens(self):
        self.assertAlmostEqual(
            uncertainty.margin_sd_cov(0, 0, 0.0, 0.0),
            uncertainty.margin_sd(0, 0))
        base = uncertainty.margin_sd_cov(4, 4, 0.0, 0.0)
        with_hfa = uncertainty.margin_sd_cov(4, 4, 0.0, 1.0)
        self.assertGreater(with_hfa, base)
        # HFA 1.0 pt adds exactly 1.0 in quadrature.
        self.assertAlmostEqual(with_hfa ** 2, base ** 2 + 1.0, places=6)
        # Neutral-site: hfa_sd=0 is honoured, not defaulted to 1.0.
        self.assertAlmostEqual(
            uncertainty.margin_sd_cov(4, 4, 0.0, 0.0), base)

    def test_margin_interval_and_cover_interval_cov(self):
        lo, hi = uncertainty.margin_interval(3.5, 4, 4, covariance=0.0,
                                             hfa_sd=0.0)
        self.assertLess(lo, 3.5)
        self.assertGreater(hi, 3.5)
        lo2, hi2 = uncertainty.cover_prob_interval_cov(3.5, 2.5, 4, 4)
        self.assertLessEqual(lo2, hi2)
        # Interval contains the point probability.
        from nflpicks import calibration
        pt = calibration.cover_probability_for_pick(3.5, 2.5)
        self.assertLessEqual(lo2, pt)
        self.assertGreaterEqual(hi2, pt)

    def test_window_pairs_is_point_in_time(self):
        games = pd.DataFrame([
            {"season": 2024, "week": 1, "game_type": "REG",
             "home_team": "KC", "away_team": "DEN"},
            {"season": 2024, "week": 5, "game_type": "REG",
             "home_team": "KC", "away_team": "BUF"},
            {"season": 2024, "week": 6, "game_type": "REG",
             "home_team": "BUF", "away_team": "KC"},
            {"season": 2023, "week": 17, "game_type": "REG",
             "home_team": "DEN", "away_team": "KC"},
            {"season": 2024, "week": 5, "game_type": "POST",
             "home_team": "KC", "away_team": "MIA"},
        ])
        pairs = uncertainty.window_pairs(games, 2024, 5)
        # Week 1 (before W) + full 2023; week 5/6 and POST excluded.
        self.assertIn(("KC", "DEN"), pairs)
        self.assertIn(("DEN", "KC"), pairs)
        self.assertNotIn(("KC", "BUF"), pairs)
        self.assertNotIn(("BUF", "KC"), pairs)
        self.assertNotIn(("KC", "MIA"), pairs)


class TestFlagsStayOffBatch4GroupC(unittest.TestCase):
    def test_flags_stay_off(self):
        for flag in ("EPA_LIVE", "ENSEMBLE_LIVE", "CALIBRATED_PICK",
                     "TRAVEL_ENABLED", "INJ_AUTO_APPLY", "TOTALS_SRS_LIVE",
                     "STALENESS_LIVE", "UNCERTAINTY_PICK_USE",
                     "PBP_FEATURES_LIVE", "TEAM_HFA_LIVE",
                     "DYNAMIC_BLEND_LIVE"):
            self.assertFalse(getattr(config, flag), flag)

    def test_no_pick_logic_reads_new_modules(self):
        model_src = Path(__file__).resolve().parent.parent.joinpath(
            "nflpicks", "model.py").read_text()
        self.assertNotIn("uncertainty", model_src)
        self.assertNotIn("pbp_round2", model_src)
        self.assertNotIn("pbp_features", model_src)


if __name__ == "__main__":
    unittest.main()
