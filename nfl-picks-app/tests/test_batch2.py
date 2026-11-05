"""Tests for batch-2 items 4-6 (ensemble / features / market).
Run from nfl-picks-app/: python -m unittest discover -s tests -v
No network, no Streamlit needed.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from nflpicks import backtest, config, db, elo, ensemble, epa, features, market


def team_stats_row(team, opp, week, off_epa, sacks_suf=2, attempts=30,
                   carries=25, def_sacks=2, qb_hits=4, game_id=None):
    return {
        "season": 2024, "week": week, "team": team, "season_type": "REG",
        "game_id": game_id or f"2024_{week}_{team}_{opp}",
        "opponent_team": opp,
        "passing_epa": off_epa * 0.7, "rushing_epa": off_epa * 0.3,
        "attempts": attempts, "carries": carries, "sacks_suffered": sacks_suf,
        "passing_first_downs": 10, "rushing_first_downs": 5,
        "passing_20": 3, "passing_40": 1, "rushing_20": 1, "rushing_40": 0,
        "def_sacks": def_sacks, "def_qb_hits": qb_hits,
    }


class TestSosEpa(unittest.TestCase):
    def test_empty_returns_none(self):
        self.assertIsNone(epa.sos_epa_ratings(pd.DataFrame())["ratings"])
        self.assertIsNone(epa.sos_epa_ratings(None)["ratings"])

    def test_good_offense_and_defense_rank_high(self):
        # KC: strong offense vs everyone, strong defense (holds opponents
        # to negative EPA). DEN: weak on both. SOS solve must rank KC > DEN
        # on both components, and ratings must be ~mean-centered.
        rows = []
        for wk in (1, 2):
            rows.append(team_stats_row("KC", "DEN", wk, 8.0))
            rows.append(team_stats_row("DEN", "KC", wk, -8.0))
            rows.append(team_stats_row("BUF", "MIA", wk, 0.0))
            rows.append(team_stats_row("MIA", "BUF", wk, 0.0))
        out = epa.sos_epa_ratings(pd.DataFrame(rows))
        self.assertGreater(out["ratings"]["KC"], out["ratings"]["DEN"])
        self.assertGreater(out["off"]["KC"], out["off"]["DEN"])
        self.assertGreater(out["def"]["KC"], out["def"]["DEN"])
        mean_rating = sum(out["ratings"].values()) / len(out["ratings"])
        self.assertAlmostEqual(mean_rating, 0.0, delta=0.6)

    def test_sos_differs_from_naive_when_schedules_differ(self):
        # Same raw offensive EPA (5.0), but A faced a top defense and B
        # faced a bottom defense: SOS off rating must credit A more.
        # KC's strength / MIA's weakness is established through a
        # COMMON opponent Z (KC holds Z to -5, MIA allows Z +10), which
        # connects the graph -- with two disconnected pairs the SOS
        # system is underdetermined and cannot distinguish them, so a
        # connected graph is part of the test, not an accident.
        rows = [
            team_stats_row("A", "KC", 1, 5.0, game_id="g1"),
            team_stats_row("KC", "A", 1, 0.0, game_id="g1"),
            team_stats_row("Z", "KC", 2, -5.0, game_id="g2"),
            team_stats_row("KC", "Z", 2, 0.0, game_id="g2"),
            team_stats_row("Z", "MIA", 3, 10.0, game_id="g3"),
            team_stats_row("MIA", "Z", 3, 0.0, game_id="g3"),
            team_stats_row("B", "MIA", 4, 5.0, game_id="g4"),
            team_stats_row("MIA", "B", 4, 0.0, game_id="g4"),
        ]
        out = epa.sos_epa_ratings(pd.DataFrame(rows))
        self.assertGreater(out["def"]["KC"], out["def"]["MIA"])
        self.assertGreater(out["off"]["A"], out["off"]["B"])


class TestElo(unittest.TestCase):
    def test_win_raises_loser_lowers_and_margin_sign(self):
        state = elo.new_state(["KC", "DEN"])
        self.assertEqual(elo.elo_margin(1500, 1500, 2.0), 2.0)
        games = pd.DataFrame([{"home_team": "KC", "away_team": "DEN",
                               "home_score": 30, "away_score": 10}])
        new = elo.apply_week(state, games)
        self.assertGreater(new["KC"], 1500)
        self.assertLess(new["DEN"], 1500)
        self.assertGreater(elo.elo_margin(new["KC"], new["DEN"], 2.0), 2.0)

    def test_regression_pulls_toward_start(self):
        reg = elo.regress_state({"KC": 1700.0, "DEN": 1300.0})
        self.assertEqual(reg["KC"], 1500 + 200 * config.ELO_PRIOR_REGRESSION)
        self.assertEqual(reg["DEN"], 1500 - 200 * config.ELO_PRIOR_REGRESSION)

    def test_same_week_games_do_not_see_each_other(self):
        # Two games in one week involving KC: the second game's update
        # must still use the pre-week rating, so KC's total delta is
        # exactly 2 * K * (1 - expected) for two wins, not compounded.
        state = elo.new_state(["KC", "DEN", "BUF"])
        games = pd.DataFrame([
            {"home_team": "KC", "away_team": "DEN", "home_score": 20, "away_score": 10},
            {"home_team": "KC", "away_team": "BUF", "home_score": 20, "away_score": 10},
        ])
        new = elo.apply_week(state, games)
        exp = elo.expected_home_win_prob(1500, 1500)
        self.assertAlmostEqual(new["KC"], 1500 + 2 * config.ELO_K * (1 - exp), places=6)


class TestEnsemble(unittest.TestCase):
    def test_combine_renormalizes_over_available(self):
        w = {"srs": 0.5, "elo": 0.25, "epa": 0.25, "market": 0.0}
        # EPA missing: srs/elo renormalize to 2/3, 1/3.
        self.assertAlmostEqual(
            ensemble.combine_margin({"srs": 6.0, "elo": 0.0, "epa": None, "market": 3.0}, w),
            4.0)
        self.assertIsNone(ensemble.combine_margin({"srs": None, "elo": None}, w))

    def test_learn_weights_beats_equal_on_clear_signal(self):
        # SRS is perfect, market is noise: learned weights must put the
        # bulk on SRS and beat equal weights on MAE.
        rows = []
        for i, actual in enumerate([-10, -3, 4, 12, -7, 0, 8, -14, 3, 20]):
            rows.append({"actual_margin": float(actual), "spread_line": 0.0,
                         "srs_margin": float(actual), "elo_margin": 0.0,
                         "epa_margin": 0.0, "market_margin": 0.0})
        # Need >= MIN_TRAIN_GAMES rows: repeat the pattern.
        rows = rows * 25
        learned = ensemble.learn_weights(rows)
        self.assertGreaterEqual(learned["weights"]["srs"], 0.75)
        self.assertLess(learned["train"]["mae"],
                        ensemble.evaluate_weights(rows, {c: 0.25 for c in ensemble.COMPONENTS})["mae"])

    def test_learn_weights_equal_when_too_few_games(self):
        rows = [{"actual_margin": 3.0, "spread_line": 0.0, "srs_margin": 3.0,
                 "elo_margin": 0.0, "epa_margin": 0.0, "market_margin": 0.0}]
        learned = ensemble.learn_weights(rows)
        self.assertEqual(learned["weights"]["srs"], 0.25)


class TestFeatures(unittest.TestCase):
    def test_rates_and_pressure_join(self):
        rows = [team_stats_row("KC", "DEN", 1, 5.0, game_id="g1"),
                team_stats_row("DEN", "KC", 1, -2.0, game_id="g1")]
        rates = features.team_feature_rates(pd.DataFrame(rows))
        # KC: explosive (3+1+1+0)=5 plays / 55 = 0.0909; pressure allowed 2/32.
        self.assertAlmostEqual(rates["KC"]["explosive"], 5 / 55, places=4)
        self.assertAlmostEqual(rates["KC"]["pressure_allowed"], 2 / 32, places=4)
        # KC pressure generated: (2+4) / DEN dropbacks (30+2) = 6/32.
        self.assertAlmostEqual(rates["KC"]["pressure_generated"], 6 / 32, places=4)
        self.assertAlmostEqual(rates["KC"]["pass_share"], 30 / 55, places=4)

    def test_wind_interaction_zero_without_wind_or_below_threshold(self):
        rows = [team_stats_row("KC", "DEN", 1, 5.0, game_id="g1"),
                team_stats_row("DEN", "KC", 1, -2.0, game_id="g1")]
        rates = features.team_feature_rates(pd.DataFrame(rows))
        league = features.league_rates(rates)
        for wind in (None, 10.0):
            game = pd.Series({"home_team": "KC", "away_team": "DEN", "wind": wind})
            diffs = features.game_feature_differentials(game, rates, league)
            self.assertEqual(diffs["wind_pass"], 0.0)
        game = pd.Series({"home_team": "KC", "away_team": "DEN", "wind": 25.0})
        diffs = features.game_feature_differentials(game, rates, league)
        self.assertIsNotNone(diffs["wind_pass"])

    def test_fit_coeff_recovers_signal_and_zero_without_variation(self):
        rows = [{"actual_margin": 10.0, "model_margin": 0.0, "explosive_diff": 0.1},
                {"actual_margin": -10.0, "model_margin": 0.0, "explosive_diff": -0.1}]
        self.assertAlmostEqual(features.fit_feature_coeff(rows, "explosive"), 100.0)
        rows0 = [{"actual_margin": 10.0, "model_margin": 0.0, "explosive_diff": 0.0}]
        self.assertEqual(features.fit_feature_coeff(rows0, "explosive"), 0.0)


class TestMarket(unittest.TestCase):
    def test_american_probs_and_vig_removal(self):
        self.assertAlmostEqual(market.american_to_prob(-110), 110 / 210, places=4)
        self.assertAlmostEqual(market.american_to_prob(110), 100 / 210, places=4)
        ph, pa = market.ml_implied_probs(-110, -110)
        self.assertAlmostEqual(ph + pa, 1.0, places=6)
        self.assertAlmostEqual(ph, 0.5, places=6)

    def test_total_implied_scores_sum_and_diff(self):
        h, a = market.total_implied_scores(3.5, 47.0)
        self.assertAlmostEqual(h + a, 47.0)
        self.assertAlmostEqual(h - a, 3.5)

    def test_ml_margin_sign_and_flag(self):
        # Home favorite -200: ML-implied margin must be positive.
        self.assertGreater(market.ml_implied_margin(-200, 170), 0)
        # A spread/ML pair consistent with each other (real 2019-2025
        # row shape: spread 3.5, home -178 / away +160) must NOT flag.
        cc = market.crosscheck(3.5, -178, 160, 47.0, model_margin=3.0)
        self.assertEqual(cc["spread_line"], 3.5)  # original line preserved
        self.assertEqual(cc["flags"], [])
        cc2 = market.crosscheck(3.5, -1000, 700, 47.0, model_margin=3.0)
        self.assertTrue(any("spread/ML disagree" in f for f in cc2["flags"]))
        self.assertEqual(cc2["spread_line"], 3.5)  # flag never rewrites the line

    def test_db_roundtrip(self):
        import tempfile, os
        path = os.path.join(tempfile.mkdtemp(), "t.db")
        db.init_db(path)
        cc = market.crosscheck(3.5, -178, 160, 47.0, model_margin=3.0)
        db.save_market_crosscheck(2024, 5, "KC", "DEN", cc, db_path=path)
        rows = db.get_market_crosschecks(2024, 5, db_path=path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["spread_line"], 3.5)
        self.assertEqual(rows[0]["home_moneyline"], -178)
        self.assertEqual(rows[0]["total_line"], 47.0)


class TestBatch2GradingRules(unittest.TestCase):
    def test_margin_equal_to_line_is_push_not_wrong(self):
        # Market-only ensemble margins equal the line by construction;
        # the first batch-2 run graded those "wrong" (spurious 0% ATS).
        rows2 = [{"season": 2024, "week": 1, "home_team": "KC", "away_team": "DEN",
                  "spread_line": 3.0, "actual_margin": 7.0, "market_margin": 3.0}]
        self.assertEqual(backtest._ats_rows(rows2, "market_margin")[0]["ats"], "push")

    def test_ats_objective_differs_from_mae_objective(self):
        # Market is the MAE winner but a coin flip on ATS; SRS wins ATS.
        # MAE objective must pick market, ATS objective must pick SRS.
        rows = []
        # SRS is on the CORRECT side but hugely overshoots (ATS 100%,
        # MAE 99); market is on the WRONG side but close (ATS 0%,
        # MAE 2). MAE must therefore pick pure market, ATS must pick
        # a blend with SRS weight > 0 (grid step 0.25 -> 0.25 SRS).
        for actual, srs_m, mkt_m in ((1.0, 100.0, -1.0), (-1.0, -100.0, 1.0)):
            rows.append({"actual_margin": actual, "spread_line": 0.0,
                         "srs_margin": srs_m, "elo_margin": 20.0,
                         "epa_margin": 20.0, "market_margin": mkt_m})
        rows = rows * 120  # >= MIN_TRAIN_GAMES (200)
        mae_w = ensemble.learn_weights(rows, objective="mae")["weights"]
        ats_w = ensemble.learn_weights(rows, objective="ats")["weights"]
        self.assertEqual(mae_w["market"], 1.0)
        # ATS optimum is the smallest SRS weight on the grid that flips
        # the sign correctly (0.25), since ATS tie-breaks on MAE.
        self.assertEqual(ats_w["srs"], 0.25)


class TestEnsembleHarnessSmoke(unittest.TestCase):
    def test_evaluate_ensemble_runs_on_synthetic_games(self):
        # 2 tiny seasons, 4 teams, every pair plays: enough for SRS/Elo
        # to run end to end; asserts structure, not accuracy.
        games = []
        for season in (2023, 2024):
            for wk in (1, 2):
                for h, a, hs, aws in (("KC", "DEN", 24, 17), ("BUF", "MIA", 20, 27)):
                    games.append({"season": season, "game_type": "REG", "week": wk,
                                  "home_team": h, "away_team": a, "home_score": hs,
                                  "away_score": aws, "result": hs - aws,
                                  "spread_line": 3.0, "total_line": 45.0,
                                  "home_rest": 7, "away_rest": 7, "div_game": 0,
                                  "location": "Home", "home_moneyline": -150,
                                  "away_moneyline": 130})
        df = backtest.load_games_frame(pd.DataFrame(games))
        out = backtest.evaluate_ensemble_and_features(df, [2024], {})
        self.assertIn("ensemble_wf", out)
        self.assertIn("baseline", out)
        self.assertIn("market", out)
        self.assertEqual(out["baseline"]["n"], 4)


if __name__ == "__main__":
    unittest.main()
