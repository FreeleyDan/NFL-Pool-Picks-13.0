"""Tests for batch-3 item 8 (true play-by-play features).

Run from nfl-picks-app/: python -m unittest discover -s tests -v
No network, no Streamlit needed. Every play-by-play row below is
synthetic sample data labelled as such -- no real nflverse file
is read by these tests, and no pick is changed: PBP_FEATURES_LIVE
stays False and no live module imports pbp_features.
"""
import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nflpicks import backtest, config, pbp_features as pf

HERE = Path(__file__).resolve().parent.parent


def _play(game, week, team, opp, drive, down, ytg, yl, ptype, gained,
          fd=0, td=0, td_team="", season=2024, stype="REG"):
    return {"game_id": game, "season": season, "season_type": stype,
            "week": week, "posteam": team, "defteam": opp, "drive": drive,
            "down": down, "ydstogo": ytg, "yardline_100": yl,
            "play_type": ptype, "yards_gained": gained, "first_down": fd,
            "touchdown": td, "td_team": td_team}


def _sample_pbp():
    # SAMPLE ONLY: one week-1 game, KC (home) vs DEN, plus one
    # prior-season week for the point-in-time test.
    rows = [
        # KC early downs: 4/10 success, 3/10 fail, 6/10 on 2nd success,
        # 5/10 on 2nd fail, 2/10 with a first down = success.
        _play("2024_01_KC_DEN", 1, "KC", "DEN", 1, 1, 10, 70, "run", 4),
        _play("2024_01_KC_DEN", 1, "KC", "DEN", 1, 1, 10, 66, "pass", 3),
        _play("2024_01_KC_DEN", 1, "KC", "DEN", 2, 2, 10, 60, "run", 6),
        _play("2024_01_KC_DEN", 1, "KC", "DEN", 2, 2, 10, 54, "pass", 5),
        _play("2024_01_KC_DEN", 1, "KC", "DEN", 3, 1, 10, 50, "pass", 2, fd=1),
        # Excluded from early downs: 3rd down, kneel, no_play.
        _play("2024_01_KC_DEN", 1, "KC", "DEN", 4, 3, 5, 40, "pass", 5, fd=1),
        _play("2024_01_KC_DEN", 1, "KC", "DEN", 4, 1, 10, 30, "qb_kneel", -1),
        _play("2024_01_KC_DEN", 1, "KC", "DEN", 4, 1, 10, 30, "no_play", 0),
        # DEN early downs: 10/10 success, 0/10 fail.
        _play("2024_01_KC_DEN", 1, "DEN", "KC", 1, 1, 10, 70, "run", 10, fd=1),
        _play("2024_01_KC_DEN", 1, "DEN", "KC", 1, 1, 10, 60, "pass", 0),
        # KC red zone: drive 5 reaches RZ and scores; drive 6
        # reaches RZ and does not (run + field goal attempt is a
        # trip without a TD -- the FG play itself is not a
        # scrimmage play, so the segment is just the run);
        # drive 7 is kneel-only in the RZ and is NOT a trip.
        _play("2024_01_KC_DEN", 1, "KC", "DEN", 5, 1, 10, 25, "run", 10),
        _play("2024_01_KC_DEN", 1, "KC", "DEN", 5, 1, 10, 15, "pass", 10, td=1, td_team="KC"),
        _play("2024_01_KC_DEN", 1, "KC", "DEN", 6, 1, 10, 18, "run", 2),
        _play("2024_01_KC_DEN", 1, "KC", "DEN", 6, 2, 8, 16, "field_goal", 0),
        _play("2024_01_KC_DEN", 1, "KC", "DEN", 7, 1, 10, 10, "qb_kneel", -1),
        # DEN red zone: one trip, no TD.
        _play("2024_01_KC_DEN", 1, "DEN", "KC", 1, 1, 10, 12, "pass", 3),
        # POSTSEASON play that must be excluded from REG counts.
        _play("2024_01_KC_DEN", 1, "KC", "DEN", 9, 1, 10, 70, "run", 10, fd=1, stype="POST"),
        # Prior season: one KC early-down success in 2023 week 5.
        _play("2023_05_KC_BUF", 5, "KC", "BUF", 1, 1, 10, 70, "run", 8, season=2023),
    ]
    return pd.DataFrame(rows)


class TestPbpSource(unittest.TestCase):
    def test_confirmed_url_template(self):
        # The exact asset name from the nflverse-data `pbp`
        # release listing (id 58152862), not a guessed URL.
        self.assertEqual(
            pf.pbp_url(2024),
            "https://github.com/nflverse/nflverse-data/releases/download/pbp/play_by_play_2024.csv.gz")
        self.assertIn("/pbp/play_by_play_", config.PBP_URL_TMPL)
        self.assertFalse(config.PBP_FEATURES_LIVE)

    def test_no_live_module_reads_pbp_features(self):
        paths = {"model.py": HERE / "nflpicks" / "model.py",
                 "weekly_run.py": HERE / "weekly_run.py",
                 "dashboard.py": HERE / "dashboard.py"}
        for name, path in paths.items():
            self.assertNotIn("pbp_features", path.read_text(), name)


class TestPbpAggregation(unittest.TestCase):
    def setUp(self):
        self.counts = pf.weekly_team_counts(_sample_pbp())

    def _row(self, season, week, team):
        sub = self.counts[(self.counts["season"] == season)
                          & (self.counts["week"] == week)
                          & (self.counts["team"] == team)]
        self.assertEqual(len(sub), 1, (season, week, team))
        return sub.iloc[0]

    def test_early_down_success_thresholds_and_exclusions(self):
        kc = self._row(2024, 1, "KC")
        # 8 plays: the 5 listed early downs plus the 3 red-zone
        # scrimmage plays (drive 5 run/pass, drive 6 run). 3rd
        # down, kneel, no_play, field goal, and POST excluded.
        # Successes: 4/10, 6/10-on-2nd, first-down override,
        # and both drive-5 plays = 5.
        self.assertEqual(kc["early_den"], 8)
        self.assertEqual(kc["early_num"], 5)
        den = self._row(2024, 1, "DEN")
        # 10/10 success, 0/10 fail, plus the red-zone 3/10 fail.
        self.assertEqual(den["early_den"], 3)
        self.assertEqual(den["early_num"], 1)
        # Defensive allowed mirrors the opponent's plays.
        self.assertEqual(kc["early_allow_den"], 3)
        self.assertEqual(kc["early_allow_num"], 1)
        self.assertEqual(den["early_allow_den"], 8)
        self.assertEqual(den["early_allow_num"], 5)

    def test_red_zone_trips_and_kneel_only_exclusion(self):
        kc = self._row(2024, 1, "KC")
        self.assertEqual(kc["rz_trips"], 2)
        self.assertEqual(kc["rz_tds"], 1)
        den = self._row(2024, 1, "DEN")
        self.assertEqual(den["rz_trips"], 1)
        self.assertEqual(den["rz_tds"], 0)
        self.assertEqual(kc["rz_allow_trips"], 1)
        self.assertEqual(kc["rz_allow_tds"], 0)
        self.assertEqual(den["rz_allow_trips"], 2)
        self.assertEqual(den["rz_allow_tds"], 1)

    def test_point_in_time_prior_season_and_week_filter(self):
        # Week 1 of 2024: only the 2023 prior season counts.
        rates, _ = pf.point_in_time_rates(self.counts, 2024, 1)
        self.assertAlmostEqual(rates["KC"]["early_off"], 1.0)
        self.assertNotIn("DEN", rates)
        # Week 2 of 2024: prior season + week 1.
        rates, league = pf.point_in_time_rates(self.counts, 2024, 2)
        # KC: (5 + 1) / (8 + 1) = 6/9; DEN: 1/3.
        self.assertAlmostEqual(rates["KC"]["early_off"], 6 / 9)
        self.assertAlmostEqual(rates["DEN"]["early_off"], 1 / 3)
        diffs = pf.game_pbp_differentials(rates, league, "KC", "DEN")
        # Early: KC strength 6/9-1/3=1/3; DEN 1/3-5/8=-7/24
        # (DEN's allowed rate is week-1 KC only, 5/8 -- the
        # prior-season KC play was allowed by BUF, not DEN);
        # diff = 1/3 + 7/24 = 0.625.
        self.assertAlmostEqual(diffs["early_down"], 0.625, places=4)
        # Red zone: KC 0.5-0.0=0.5; DEN 0.0-0.5=-0.5; diff 1.0.
        self.assertAlmostEqual(diffs["red_zone"], 1.0)

    def test_missing_team_reads_as_league_average(self):
        rates, league = pf.point_in_time_rates(self.counts, 2024, 2)
        diffs = pf.game_pbp_differentials(rates, league, "KC", "MIA")
        # MIA has no rates, so it reads as league average on
        # both sides: league off (6/9 + 1/3)/2 = 0.5, league
        # allowed (1/3 + 5/8 + 1)/3 = 0.6528, strength -0.1528;
        # KC strength 1/3, so the diff is 0.4861.
        self.assertAlmostEqual(diffs["early_down"], 0.4861, places=4)
        self.assertAlmostEqual(diffs["red_zone"], 0.5, places=4)

    def test_empty_and_weekless_frames_yield_nothing(self):
        self.assertTrue(pf.weekly_team_counts(None).empty)
        self.assertTrue(pf.weekly_team_counts(pd.DataFrame()).empty)
        no_week = _sample_pbp().drop(columns=["week"])
        self.assertTrue(pf.weekly_team_counts(no_week).empty)


class TestPbpFittingAndGrading(unittest.TestCase):
    def test_fit_pbp_coeff_and_cap(self):
        rows = [{"early_down_diff": 0.1, "model_margin": 0.0, "actual_margin": 2.0},
                {"early_down_diff": -0.1, "model_margin": 0.0, "actual_margin": -2.0},
                {"early_down_diff": None, "model_margin": 0.0, "actual_margin": 99.0}]
        self.assertAlmostEqual(pf.fit_pbp_coeff(rows, "early_down"), 20.0)
        old = dict(config.PBP_COEFF_CAPS)
        try:
            config.PBP_COEFF_CAPS["early_down"] = 5.0
            self.assertAlmostEqual(pf.fit_pbp_coeff(rows, "early_down"), 5.0)
        finally:
            config.PBP_COEFF_CAPS.clear()
            config.PBP_COEFF_CAPS.update(old)
        self.assertEqual(pf.fit_pbp_coeff([], "early_down"), 0.0)

    def test_adjusted_margin_ignores_none(self):
        self.assertAlmostEqual(
            pf.adjusted_margin(3.0, {"early_down": 0.2, "red_zone": None},
                               {"early_down": 10.0, "red_zone": 10.0}), 5.0)

    def test_corrected_grading_rule_margin_equals_line_is_push(self):
        # Batch 4: the old canonical rule (margin == line with a
        # decided game counted WRONG) was the bug; the corrected
        # canonical rule grades it a push, matching batch-2
        # _ats_rows. Actual == line is also a push.
        rows = [{"season": 2024, "week": 1, "home_team": "KC", "away_team": "DEN",
                 "spread_line": 3.0, "actual_margin": 7.0, "feat_margin": 3.0},
                {"season": 2024, "week": 1, "home_team": "BUF", "away_team": "MIA",
                 "spread_line": 3.0, "actual_margin": 3.0, "feat_margin": 10.0}]
        graded = pf._grade_rows(rows, "feat_margin")
        self.assertEqual(graded[0]["ats"], "push")
        self.assertEqual(graded[1]["ats"], "push")

    def test_loader_reads_local_file_subset(self):
        frame = _sample_pbp()
        with tempfile.NamedTemporaryFile(suffix=".csv", mode="w", delete=False) as f:
            frame.to_csv(f.name, index=False)
            loaded = pf.load_pbp_frame(2024, path=f.name)
        self.assertEqual(len(loaded), len(frame))
        self.assertIn("play_type", loaded.columns)
        self.assertNotIn("nonexistent_column", loaded.columns)


class TestPbpEvaluateSmoke(unittest.TestCase):
    def test_evaluate_on_toy_schedule(self):
        # SAMPLE ONLY: 4 teams, 2023 (burn-in/training + prior
        # season) and 2024 (evaluated). Scores are invented.
        games_rows = []
        for season, base in ((2023, 0), (2024, 100)):
            for week in (1, 2):
                games_rows.append({"season": season, "week": week, "game_type": "REG",
                                   "home_team": "KC", "away_team": "DEN",
                                   "home_score": 24 + base % 7, "away_score": 17,
                                   "result": 7.0, "spread_line": 3.0})
                games_rows.append({"season": season, "week": week, "game_type": "REG",
                                   "home_team": "BUF", "away_team": "MIA",
                                   "home_score": 20, "away_score": 21,
                                   "result": -1.0, "spread_line": -2.5})
        games = backtest.load_games_frame(pd.DataFrame(games_rows))
        sample = _sample_pbp()
        out = pf.evaluate_pbp_features(
            games, [2024],
            {2024: sample[sample["season"] == 2024],
             2023: sample[sample["season"] == 2023]})
        self.assertIn(2024, out["coeffs_by_season"])
        self.assertEqual(out["baseline"]["n"], 4)
        for label in ("pbp_early_down", "pbp_red_zone", "pbp_all"):
            self.assertIn(label, out)
            self.assertEqual(out[label]["n"], 4)
            self.assertIn("paired_vs_baseline", out[label])
