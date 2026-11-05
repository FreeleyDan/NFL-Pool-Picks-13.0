"""Tests for batch 5, group A items 1-2 (situational spots; exact
rest / travel / time-zone math). No network, no games.csv needed:
the full 2019-2025 numbers live in the README batch-5 group A
section.

Run from nfl-picks-app/: python -m unittest discover -s tests -v
No live behaviour changes: no flag is flipped, no pick is changed.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from nflpicks import backtest, config, rest_travel as rt, situational as sit


def _game(season, week, home, away, hs, asc, line, div=0, hrest=7, arest=7,
          gameday="2024-09-08", location="Home", stadium=""):
    return {"season": season, "week": week, "game_type": "REG",
            "home_team": home, "away_team": away, "home_score": hs,
            "away_score": asc, "result": float(hs - asc),
            "spread_line": float(line), "div_game": div,
            "home_rest": hrest, "away_rest": arest, "gameday": gameday,
            "location": location, "stadium": stadium}


def _season_frame(games):
    return backtest.load_games_frame(pd.DataFrame(games))


class TestSituationalSpots(unittest.TestCase):
    def _ratings(self):
        # KC strong, DEN weak, BUF mid -- top-5/top-8 sets are
        # deterministic on this dict regardless of size.
        return {"KC": 8.0, "BUF": 2.0, "DEN": -6.0, "MIA": 0.0}

    def test_lookahead_div_and_strong(self):
        # KC plays DEN in week 5 (target), then BUF in week 6 in a
        # division game; BUF is top-5 here (only 4 teams rated).
        games = _season_frame([
            _game(2024, 5, "KC", "DEN", 24, 20, 3.0),
            _game(2024, 6, "BUF", "KC", 20, 17, 1.0, div=1),
            _game(2024, 4, "KC", "MIA", 30, 10, 3.0),
        ])
        row = games[(games["week"] == 5)].iloc[0]
        spots = sit.team_spots(games, "KC", 5, self._ratings(), row)
        self.assertIn("lookahead_div", spots)
        self.assertIn("lookahead_strong", spots)
        # DEN's next game is not in the frame: no lookahead spots.
        self.assertEqual(sit.team_spots(games, "DEN", 5, self._ratings(), row), set())

    def test_no_next_game_in_final_week(self):
        games = _season_frame([_game(2024, 5, "KC", "DEN", 24, 20, 3.0)])
        row = games.iloc[0]
        self.assertNotIn("lookahead_div", sit.team_spots(games, "KC", 5, self._ratings(), row))
        self.assertNotIn("lookahead_strong", sit.team_spots(games, "KC", 5, self._ratings(), row))

    def test_letdown_upset_and_blowout(self):
        # Week 4: KC won 30-10 as an underdog (line -3.5 means away
        # favoured... here KC is home, line -3.5 => home dog) and by
        # 20 points -- both letdown spots fire for week 5.
        games = _season_frame([
            _game(2024, 4, "KC", "MIA", 30, 10, -3.5),
            _game(2024, 5, "KC", "DEN", 24, 20, 3.0),
        ])
        row = games[(games["week"] == 5)].iloc[0]
        spots = sit.team_spots(games, "KC", 5, self._ratings(), row)
        self.assertIn("letdown_upset", spots)
        self.assertIn("letdown_blowout", spots)
        # A 10-point win as a favourite is neither.
        games2 = _season_frame([
            _game(2024, 4, "KC", "MIA", 27, 17, 3.5),
            _game(2024, 5, "KC", "DEN", 24, 20, 3.0),
        ])
        row2 = games2[(games2["week"] == 5)].iloc[0]
        spots2 = sit.team_spots(games2, "KC", 5, self._ratings(), row2)
        self.assertNotIn("letdown_upset", spots2)
        self.assertNotIn("letdown_blowout", spots2)

    def test_week1_has_no_previous_game_spot(self):
        games = _season_frame([_game(2024, 1, "KC", "DEN", 24, 20, 3.0)])
        row = games.iloc[0]
        spots = sit.team_spots(games, "KC", 1, self._ratings(), row)
        self.assertNotIn("letdown_upset", spots)
        self.assertNotIn("letdown_blowout", spots)

    def test_sandwich_needs_favourite_and_two_strong_neighbours(self):
        # KC is favourite (line 3.0) in week 5; previous opponent
        # BUF and next opponent BUF are both top-8 here.
        games = _season_frame([
            _game(2024, 4, "KC", "BUF", 24, 20, 3.0),
            _game(2024, 5, "KC", "DEN", 24, 20, 3.0),
            _game(2024, 6, "BUF", "KC", 20, 17, 1.0),
        ])
        row = games[(games["week"] == 5)].iloc[0]
        self.assertIn("sandwich", sit.team_spots(games, "KC", 5, self._ratings(), row))
        # Same setup, but KC is the underdog in week 5: no sandwich.
        games2 = _season_frame([
            _game(2024, 4, "KC", "BUF", 24, 20, 3.0),
            _game(2024, 5, "KC", "DEN", 24, 20, -3.0),
            _game(2024, 6, "BUF", "KC", 20, 17, 1.0),
        ])
        row2 = games2[(games2["week"] == 5)].iloc[0]
        self.assertNotIn("sandwich", sit.team_spots(games2, "KC", 5, self._ratings(), row2))

    def test_shortweek_fav(self):
        games = _season_frame([_game(2024, 5, "KC", "DEN", 24, 20, 3.0, hrest=4, arest=7)])
        row = games.iloc[0]
        self.assertIn("shortweek_fav", sit.team_spots(games, "KC", 5, self._ratings(), row))
        # Only 2 days short: below the 3-day cutoff.
        games2 = _season_frame([_game(2024, 5, "KC", "DEN", 24, 20, 3.0, hrest=5, arest=7)])
        self.assertNotIn("shortweek_fav",
                         sit.team_spots(games2, "KC", 5, self._ratings(), games2.iloc[0]))

    def test_learned_rule_fades_a_losing_spot_and_defaults_small_spots(self):
        def row(model, line, actual, spots_home, week=5, season=2023):
            return {"model_margin": model, "spread_line": line, "actual_margin": actual,
                    "week": week, "season": season, "home_team": "KC", "away_team": "DEN",
                    "spots_home": set(spots_home), "spots_away": set()}
        # lookahead_div loses 0/40 on train -> fade; sandwich has 1
        # decided train game -> follow (below the 30 floor).
        train = [row(8.0, 3.0, -3.0, {"lookahead_div"})] * 40 + [row(8.0, 3.0, 7.0, {"sandwich"})]
        rule = sit.learn_spot_rules(train)
        self.assertEqual(rule["lookahead_div"], "fade")
        self.assertEqual(rule["sandwich"], "follow")
        test = [row(8.0, 3.0, -3.0, {"lookahead_div"})] * 10
        applied = sit.apply_spot_rule(test, rule)
        self.assertEqual(applied["rule"]["ats_pct"], 100.0)
        self.assertEqual(applied["always_follow"]["ats_pct"], 0.0)
        self.assertEqual(applied["n_flipped"], 10)

    def test_additive_adjustment_uses_train_residual_and_clips(self):
        def row(model, line, actual, spots_home, week=5, season=2023):
            return {"model_margin": model, "spread_line": line, "actual_margin": actual,
                    "week": week, "season": season, "home_team": "KC", "away_team": "DEN",
                    "spots_home": set(spots_home), "spots_away": set()}
        # 40 train games, actual 6 below model every time: mean
        # residual -6.0, clipped to the -3.0 cap. (Actual must differ
        # from the line -- an actual == line is a push and is
        # excluded from the fit, like the harness grading.)
        train = [row(8.0, 3.0, 2.0, {"letdown_upset"})] * 40
        adj = sit.learn_spot_adjustments(train)
        self.assertEqual(adj["letdown_upset"], -3.0)
        self.assertEqual(adj["sandwich"], 0.0)  # no train games
        out = sit.apply_spot_adjustments([row(8.0, 3.0, 2.0, {"letdown_upset"})], adj)
        self.assertEqual(out["n_adjusted"], 1)

    def test_placebo_flip_distribution_is_degenerate_at_zero_flips(self):
        rows = [{"model_margin": 5.0, "spread_line": 3.0, "actual_margin": 7.0}]
        d = sit.placebo_flip_distribution(rows, 0)
        self.assertEqual(d["p95_pp"], 0.0)
        self.assertEqual(d["mean_pp"], 0.0)

    def test_evaluate_all_runs_on_tiny_frame(self):
        games = _season_frame([
            _game(2022, 1, "KC", "DEN", 24, 20, 3.0),
            _game(2022, 2, "BUF", "MIA", 20, 27, 2.0),
            _game(2023, 1, "KC", "BUF", 27, 24, 2.5),
            _game(2023, 2, "DEN", "MIA", 17, 20, 1.0),
            _game(2024, 1, "KC", "MIA", 24, 21, 3.0),
            _game(2024, 2, "BUF", "DEN", 30, 17, 4.0),
        ])
        out = sit.evaluate_all(games, seasons=[2023, 2024],
                               train_seasons=(2023,), test_seasons=(2024,))
        self.assertEqual(out["n"], 4)
        self.assertEqual(set(out["per_spot"]), set(sit.SPOTS))
        self.assertIsInstance(out["promoted_spots"], list)


class TestRestTravel(unittest.TestCase):
    def test_all_32_teams_have_a_tz_offset(self):
        self.assertEqual(len(rt._TEAM_TZ_STANDARD), 32)
        for team in ("KC", "BAL", "ARI", "SEA", "MIA"):
            self.assertIsNotNone(rt.team_tz_offset(team, "2024-10-13"))

    def test_haversine_known_distances(self):
        self.assertAlmostEqual(rt.haversine_miles(47.5952, -122.3316, 47.5952, -122.3316), 0.0)
        # Seattle -> Miami is ~2,720 mi great-circle.
        self.assertAlmostEqual(rt.haversine_miles(47.5952, -122.3316, 25.9580, -80.2389),
                               2720, delta=15)

    def test_dst_rules_arizona_never_shifts(self):
        self.assertEqual(rt.team_tz_offset("ARI", "2024-10-13"), -7)
        self.assertEqual(rt.team_tz_offset("ARI", "2024-12-15"), -7)
        self.assertEqual(rt.team_tz_offset("DEN", "2024-10-13"), -6)  # DST
        self.assertEqual(rt.team_tz_offset("DEN", "2024-12-15"), -7)  # standard

    def test_neutral_london_venue_coords_and_tz(self):
        row = pd.Series({"location": "Neutral", "stadium": "Tottenham Stadium",
                         "home_team": "MIN", "gameday": "2024-10-06"})
        coords = rt._stadium_coords(row)
        self.assertAlmostEqual(coords[0], 51.60, places=1)
        self.assertEqual(rt._stadium_tz_offset(row), 1)  # BST in October
        row_winter = pd.Series({"location": "Neutral", "stadium": "Wembley Stadium",
                                "home_team": "JAX", "gameday": "2024-12-01"})
        self.assertEqual(rt._stadium_tz_offset(row_winter), 0)  # GMT in December

    def test_unknown_neutral_venue_yields_no_travel(self):
        row = pd.Series({"location": "Neutral", "stadium": "Somewhere Bowl",
                         "home_team": "KC", "gameday": "2024-09-08"})
        self.assertIsNone(rt._stadium_coords(row))
        self.assertIsNone(rt._stadium_tz_offset(row))

    def test_week1_origin_is_home_stadium(self):
        games = _season_frame([_game(2024, 1, "KC", "BAL", 27, 20, 3.0)])
        row = games.iloc[0]
        away = rt.travel_for_team(games, "BAL", row)
        self.assertEqual(away["prev_game_week"], None)
        self.assertAlmostEqual(away["miles"], 955.5, delta=5)
        self.assertEqual(away["tz_crossed"], 1)
        home = rt.travel_for_team(games, "KC", row)
        self.assertEqual(home["miles"], 0.0)

    def test_road_trip_origin_is_previous_game_stadium(self):
        # BAL plays at KC in week 1, then at LV in week 2: the week-2
        # trip starts in Kansas City, not Baltimore.
        games = _season_frame([
            _game(2024, 1, "KC", "BAL", 27, 20, 3.0),
            _game(2024, 2, "LV", "BAL", 23, 20, -1.0),
        ])
        row2 = games[(games["week"] == 2)].iloc[0]
        away = rt.travel_for_team(games, "BAL", row2)
        kc = config.STADIUM_COORDS["KC"]
        lv = config.STADIUM_COORDS["LV"]
        self.assertAlmostEqual(away["miles"], rt.haversine_miles(*kc, *lv), delta=1)

    def test_exact_rest_uses_games_csv_columns(self):
        row = pd.Series({"home_rest": 4.0, "away_rest": 7.0})
        out = rt.exact_rest(row)
        self.assertEqual(out["rest_diff"], -3.0)
        self.assertEqual(out["home_rest_source"], "games_csv")

    def test_ols_single_recovers_a_known_slope_and_clips(self):
        xs = [1.0, 2.0, 3.0, 4.0]
        rs = [0.5, 1.0, 1.5, 2.0]  # slope exactly 0.5
        self.assertAlmostEqual(rt._ols_single(xs, rs, clip=1.0), 0.5)
        self.assertEqual(rt._ols_single(xs, [10.0, 20.0, 30.0, 40.0], clip=1.0), 1.0)
        self.assertEqual(rt._ols_single([], [], clip=1.0), 0.0)

    def test_adjusted_margin_rest_linear_replaces_live_term(self):
        # Baseline margin 5.0 with div 1.0, rest_diff +4: live term is
        # 0.25*4 = 1.0 capped at 2.0, so ex-rest margin is 4.0; a
        # learned coef of 0.5 gives 4.0 + 2.0 = 6.0.
        row = {"model_margin": 5.0, "spread_line": 3.0, "actual_margin": 7.0,
               "rest_diff": 4.0, "div_mult": 1.0, "travel_diff": 100.0,
               "tz_diff": 0, "east_diff": 0}
        coeffs = {"rest_linear": {"coef": 0.5}}
        self.assertAlmostEqual(rt.adjusted_margin(row, "rest_linear", coeffs), 6.0)
        # Travel linear: 5.0 + 0.5 * 0.1 = 5.05 (100 mi = 0.1k).
        self.assertAlmostEqual(
            rt.adjusted_margin(row, "travel_linear", {"travel_linear": {"coef_per_1000mi": 0.5}}),
            5.05)
        # Missing travel leg -> None (caller grades baseline).
        row2 = dict(row, travel_diff=None)
        self.assertIsNone(rt.adjusted_margin(row2, "travel_linear", coeffs))
        with self.assertRaises(ValueError):
            rt.adjusted_margin(row, "no_such_variant", coeffs)

    def test_evaluate_all_runs_on_tiny_frame(self):
        games = _season_frame([
            _game(2022, 1, "KC", "DEN", 24, 20, 3.0),
            _game(2022, 2, "BUF", "MIA", 20, 27, 2.0),
            _game(2023, 1, "KC", "BUF", 27, 24, 2.5),
            _game(2023, 2, "DEN", "MIA", 17, 20, 1.0),
            _game(2024, 1, "KC", "MIA", 24, 21, 3.0),
            _game(2024, 2, "BUF", "DEN", 30, 17, 4.0),
        ])
        out = rt.evaluate_all(games, seasons=[2023, 2024],
                              train_seasons=(2023,), test_seasons=(2024,))
        self.assertEqual(out["n"], 4)
        self.assertEqual(set(out["per_variant"]), set(rt.VARIANTS))
        self.assertEqual(out["n_travel_known"], 4)
        self.assertIsInstance(out["promoted_variants"], list)


class TestFlagsStayOffBatch5GroupA(unittest.TestCase):
    def test_flags_stay_off(self):
        for flag in ("EPA_LIVE", "ENSEMBLE_LIVE", "CALIBRATED_PICK", "TRAVEL_ENABLED",
                     "INJ_AUTO_APPLY", "TOTALS_SRS_LIVE", "STALENESS_LIVE",
                     "UNCERTAINTY_PICK_USE", "PBP_FEATURES_LIVE",
                     "TEAM_HFA_LIVE", "DYNAMIC_BLEND_LIVE",
                     "SITUATIONAL_LIVE", "REST_TRAVEL_EXACT_LIVE"):
            self.assertFalse(getattr(config, flag), flag)


if __name__ == "__main__":
    unittest.main()
