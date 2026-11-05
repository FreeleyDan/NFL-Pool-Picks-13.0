"""Tests for batch-3 items 1-2 (pool-line staleness framework, Elo variants).
Run from nfl-picks-app/: python -m unittest discover -s tests -v
No network, no Streamlit needed. No live behaviour changes: elo defaults
and the line-move flag are asserted unchanged.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from nflpicks import backtest, config, db, elo, elo_variants, staleness


def _week_games(rows):
    return pd.DataFrame(rows)


class TestEloParamOverrides(unittest.TestCase):
    def test_defaults_unchanged(self):
        # A plain apply_week call must equal the old behaviour exactly.
        state = elo.new_state(["KC", "DEN"])
        games = _week_games([{"home_team": "KC", "away_team": "DEN",
                              "home_score": 30, "away_score": 10}])
        new = elo.apply_week(state, games)
        exp = elo.expected_home_win_prob(1500, 1500)
        self.assertAlmostEqual(new["KC"], 1500 + config.ELO_K * (1 - exp), places=6)

    def test_k_override_scales_delta(self):
        state = elo.new_state(["KC", "DEN"])
        games = _week_games([{"home_team": "KC", "away_team": "DEN",
                              "home_score": 30, "away_score": 10}])
        new = elo.apply_week(state, games, k=40.0)
        exp = elo.expected_home_win_prob(1500, 1500)
        self.assertAlmostEqual(new["KC"], 1500 + 40.0 * (1 - exp), places=6)

    def test_margin_modes_bounded_and_ordered(self):
        # Blowout must move more than a 1-pt win under both modes, and
        # never more than 2x the win/loss delta under "log".
        for mode, cap in (("log", 2.0), ("capped", 1.5)):
            self.assertEqual(elo._margin_multiplier(0, mode), 1.0)
            self.assertGreater(elo._margin_multiplier(21, mode),
                               elo._margin_multiplier(1, mode))
            self.assertLessEqual(elo._margin_multiplier(100, mode), cap)
        with self.assertRaises(ValueError):
            elo._margin_multiplier(7, "bogus")

    def test_margin_mode_changes_update(self):
        state = elo.new_state(["KC", "DEN"])
        blow = _week_games([{"home_team": "KC", "away_team": "DEN",
                             "home_score": 40, "away_score": 10}])
        plain = elo.apply_week(state, blow)
        marg = elo.apply_week(state, blow, margin_mode="capped")
        self.assertGreater(marg["KC"] - 1500, plain["KC"] - 1500)

    def test_regress_override_and_hfa_points_override(self):
        self.assertEqual(elo.regress_state({"KC": 1700.0}, fraction=0.25)["KC"], 1550.0)
        self.assertGreater(elo.expected_home_win_prob(1500, 1500, hfa_points=110.0),
                           elo.expected_home_win_prob(1500, 1500, hfa_points=0.0))

    def test_k_multiplier_and_preweek_snapshot_preserved(self):
        # Two KC games in one week, k_multiplier=2: KC delta must be
        # exactly 2 * (2*K*(1-exp)) from pre-week ratings, not compounded.
        state = elo.new_state(["KC", "DEN", "BUF"])
        games = _week_games([
            {"home_team": "KC", "away_team": "DEN", "home_score": 20, "away_score": 10},
            {"home_team": "KC", "away_team": "BUF", "home_score": 20, "away_score": 10},
        ])
        new = elo.apply_week(state, games, k_multiplier=2.0)
        exp = elo.expected_home_win_prob(1500, 1500)
        self.assertAlmostEqual(new["KC"], 1500 + 2 * (2 * config.ELO_K * (1 - exp)), places=6)


class TestEloVariantsHarness(unittest.TestCase):
    def _synthetic(self):
        games = []
        for season in (2023, 2024):
            for wk in (1, 2, 3):
                for h, a, hs, aws in (("KC", "DEN", 24, 17), ("BUF", "MIA", 20, 27),
                                      ("KC", "BUF", 30, 20), ("DEN", "MIA", 17, 24)):
                    games.append({"season": season, "game_type": "REG", "week": wk,
                                  "home_team": h, "away_team": a, "home_score": hs,
                                  "away_score": aws, "result": hs - aws,
                                  "spread_line": 3.0, "total_line": 45.0,
                                  "home_rest": 7, "away_rest": 7, "div_game": 0,
                                  "location": "Home"})
        return backtest.load_games_frame(pd.DataFrame(games))

    def test_evaluate_variant_structure_and_smoke(self):
        df = self._synthetic()
        ev = elo_variants.evaluate_variant(df, [2024], dict(elo_variants.BASELINE), burn_in=())
        # 2024 has 3 weeks x 4 games = 12 rows, all graded (no pushes:
        # margins are continuous, lines are 3.0, actuals differ).
        self.assertEqual(len(ev["rows"]), 12)
        self.assertIn("ats_pct", ev["summary"])
        self.assertIn("2024", ev["summary"]["by_season"])

    def test_paired_and_bootstrap_determinism(self):
        base = [{"season": 2024, "week": 1, "home_team": "KC", "away_team": "DEN", "ats": "correct"},
                {"season": 2024, "week": 1, "home_team": "BUF", "away_team": "MIA", "ats": "wrong"},
                {"season": 2024, "week": 2, "home_team": "KC", "away_team": "BUF", "ats": "correct"}]
        var = [dict(r, ats="correct") for r in base]
        pc = elo_variants.paired_comparison(base, var)
        self.assertEqual(pc["variant_right_base_wrong"], 1)
        self.assertEqual(pc["base_right_variant_wrong"], 0)
        b1 = elo_variants.bootstrap_paired_delta(base, var, trials=200, seed=7)
        b2 = elo_variants.bootstrap_paired_delta(base, var, trials=200, seed=7)
        self.assertEqual(b1, b2)
        self.assertGreater(b1["paired_delta_pp"], 0)

    def test_promotion_gate_needs_all_four_conditions(self):
        # Gate logic: a variant winning train but losing test must NOT
        # be promoted, even if its full-sample paired p looks good.
        gate = {"v": {"wins_train": True, "wins_test": False,
                      "bonferroni_significant": True, "bootstrap_ci_excludes_zero": True,
                      "PROMOTED": False}}
        self.assertFalse(gate["v"]["PROMOTED"])
        self.assertEqual(elo_variants.DEFAULT_VARIANTS[0]["name"], "baseline")
        self.assertEqual(len(elo_variants.DEFAULT_VARIANTS), 12)  # baseline + 11


class TestStaleness(unittest.TestCase):
    def test_movement_direction_bucket_score(self):
        s = staleness.staleness(3.0, 5.5)
        self.assertEqual(s["movement"], 2.5)
        self.assertEqual(s["direction"], "toward_home")
        self.assertEqual(s["bucket"], "2-3")
        self.assertEqual(s["score"], 2.5)
        self.assertEqual(staleness.staleness(3.0, 3.0)["direction"], "none")
        self.assertEqual(staleness.staleness(-2.0, -5.0)["bucket"], "3+")
        self.assertIsNone(staleness.staleness(None, 3.0))
        self.assertIsNone(staleness.staleness(3.0, None))

    def test_follow_fade_sides(self):
        self.assertEqual(staleness.follow_side(3.0, 5.0), "home")
        self.assertEqual(staleness.fade_side(3.0, 5.0), "away")
        self.assertEqual(staleness.follow_side(3.0, 1.0), "away")
        self.assertIsNone(staleness.follow_side(3.0, 3.0))
        self.assertIsNone(staleness.fade_side(3.0, 3.0))

    def test_grade_follow_beats_fade_when_close_moves_toward_winner(self):
        # Pool 3.0, close 5.0 (toward home), home wins by 7: follow=home
        # correct, fade wrong. Push game (actual == pool) excluded.
        rows = [
            {"season": 2024, "pool_line": 3.0, "closing_line": 5.0, "actual_margin": 7.0},
            {"season": 2024, "pool_line": 3.0, "closing_line": 5.0, "actual_margin": 3.0},
            {"season": 2024, "pool_line": 3.0, "closing_line": 1.0, "actual_margin": 0.0},
        ]
        g = staleness.grade_staleness(rows)
        self.assertEqual(g["follow"]["ats_pct"], 100.0)  # both decided games follow-correct
        self.assertEqual(g["fade"]["ats_pct"], 0.0)
        self.assertEqual(g["follow"]["pushes"], 1)
        self.assertEqual(g["by_bucket"]["2-3"]["follow"]["decided"], 2)

    def test_learn_and_apply_rule(self):
        train = [
            {"pool_line": 0.0, "closing_line": 2.0, "actual_margin": 5.0},
            {"pool_line": 0.0, "closing_line": 2.0, "actual_margin": 6.0},
            {"pool_line": 0.0, "closing_line": 0.5, "actual_margin": -4.0},
        ]
        rule = staleness.learn_staleness_rule(train)
        self.assertEqual(rule["2-3"], "follow")  # follow won the 2-pt bucket in train
        applied = staleness.apply_staleness_rule(
            [{"pool_line": 0.0, "closing_line": 2.5, "actual_margin": 4.0}], rule)
        self.assertEqual(applied["rule"]["ats_pct"], 100.0)

    def test_bootstrap_ci_deterministic(self):
        vals = ["correct"] * 60 + ["wrong"] * 40
        c1 = staleness.bootstrap_ci(vals, trials=200, seed=11)
        c2 = staleness.bootstrap_ci(vals, trials=200, seed=11)
        self.assertEqual(c1, c2)
        self.assertEqual(c1["ats_pct"], 60.0)
        self.assertLessEqual(c1["ci95"][0], 60.0)
        self.assertGreaterEqual(c1["ci95"][1], 60.0)
        self.assertEqual(staleness.bootstrap_ci([]), {})

    def test_snapshot_pairs_feed_grading_without_new_flag(self):
        # Live path: pool + closing snapshots already exist in db;
        # staleness grades from them, while get_line_moves still owns
        # the flag (>=1 pt move here must flag, unchanged behaviour).
        import os, tempfile
        path = os.path.join(tempfile.mkdtemp(), "t.db")
        db.init_db(path)
        from nflpicks.model import GamePrediction
        # Minimal picks row so get_line_moves can join: use save_picks.
        p = GamePrediction(week=5, home="KC", away="DEN", vegas_spread=3.0,
                           model_spread=3.0, model_independent=4.0, edge=1.0,
                           pick="KC -3", confidence=1.0, strong_edge=False)
        db.save_picks(2024, [p], db_path=path)
        db.save_line_snapshot(2024, 5, "KC", "DEN", "pool", 3.0, db_path=path)
        db.save_line_snapshot(2024, 5, "KC", "DEN", "closing", 5.0, db_path=path)
        moves = db.get_line_moves(2024, 5, db_path=path)
        self.assertEqual(len(moves), 1)
        self.assertEqual(moves[0]["movement"], 2.0)
        s = staleness.staleness(moves[0]["pool_line"], moves[0]["closing_line"])
        self.assertEqual(s["bucket"], "2-3")


if __name__ == "__main__":
    unittest.main()
