"""Tests for batch 4, group B items 3-5 (staleness auto-report, shrunk
team HFA, dynamic market blend). No network, no games.csv needed: the
full 2019-2025 numbers live in the README batch-4 group B section.

Run from nfl-picks-app/: python -m unittest discover -s tests -v
No live behaviour changes: no flag is flipped, no pick is changed.
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from nflpicks import backtest, config, db, dynamic_blend as dbl, ratings, staleness


def _played(rows):
    """rows: (home, away, home_score, away_score, location)."""
    return pd.DataFrame([{
        "home_team": h, "away_team": a, "home_score": hs, "away_score": asc,
        "location": loc,
    } for h, a, hs, asc, loc in rows])


class TestStalenessAutoReport(unittest.TestCase):
    def _rows(self, n_pairs, move=2.0):
        # Pool 3.0, closing 5.0 (move toward home): follow=home.
        # Alternate actual margins so follow wins exactly half.
        rows = []
        for i in range(n_pairs):
            actual = 7.0 if i % 2 == 0 else -1.0  # home covers, then not
            rows.append({"pool_line": 3.0, "closing_line": 3.0 + move,
                         "actual_margin": actual, "season": 2025})
        return rows

    def test_below_threshold_reports_not_enough_and_grades_nothing(self):
        rep = staleness.build_staleness_report(self._rows(99), threshold=100)
        self.assertEqual(rep["status"], "not_enough_data")
        self.assertEqual(rep["n_completed_pairs"], 99)
        self.assertEqual(rep["pairs_needed"], 1)
        self.assertNotIn("follow", rep)  # nothing graded sub-threshold

    def test_at_threshold_report_grades_follow_fade(self):
        rep = staleness.build_staleness_report(self._rows(100), threshold=100)
        self.assertEqual(rep["status"], "ready")
        self.assertEqual(rep["follow"]["decided"], 100)
        self.assertEqual(rep["follow"]["ats_pct"], 50.0)
        self.assertEqual(rep["fade"]["ats_pct"], 50.0)
        self.assertFalse(getattr(config, "STALENESS_LIVE"))

    def test_rows_missing_a_line_or_result_do_not_count(self):
        rows = self._rows(100) + [
            {"pool_line": 3.0, "closing_line": None, "actual_margin": 7.0},
            {"pool_line": 3.0, "closing_line": 5.0},  # no result
        ]
        rep = staleness.build_staleness_report(rows, threshold=100)
        self.assertEqual(rep["status"], "ready")
        self.assertEqual(rep["n_completed_pairs"], 100)

    def test_push_against_pool_line_excluded_from_ats(self):
        rows = [{"pool_line": 3.0, "closing_line": 5.0, "actual_margin": 3.0,
                 "season": 2025},
                {"pool_line": 3.0, "closing_line": 5.0, "actual_margin": 7.0,
                 "season": 2025}]
        rep = staleness.build_staleness_report(rows, threshold=2)
        self.assertEqual(rep["follow"]["n"], 2)
        self.assertEqual(rep["follow"]["decided"], 1)
        self.assertEqual(rep["follow"]["pushes"], 1)

    def test_auto_report_from_db_counts_completed_pairs_only(self):
        with tempfile.TemporaryDirectory() as td:
            path = str(Path(td) / "t.db")
            db.init_db(path)
            # Two snapshot pairs; only the first has a result.
            db.save_line_snapshot(2025, 1, "KC", "DEN", "pool", 3.0, path)
            db.save_line_snapshot(2025, 1, "KC", "DEN", "closing", 5.0, path)
            db.save_line_snapshot(2025, 1, "BUF", "MIA", "pool", -2.0, path)
            db.save_line_snapshot(2025, 1, "BUF", "MIA", "closing", -4.0, path)
            pairs = db.get_snapshot_pairs(path)
            self.assertEqual(len(pairs), 2)
            by_home = {p["home_team"]: p for p in pairs}
            self.assertEqual(by_home["KC"]["pool_line"], 3.0)
            self.assertEqual(by_home["KC"]["closing_line"], 5.0)
            rep = staleness.auto_report(
                path, results={(2025, 1, "KC", "DEN"): 7.0}, threshold=2)
            self.assertEqual(rep["n_snapshot_pairs"], 2)
            self.assertEqual(rep["n_completed_pairs"], 1)
            self.assertEqual(rep["status"], "not_enough_data")
            rep2 = staleness.auto_report(
                path, results={(2025, 1, "KC", "DEN"): 7.0,
                               (2025, 1, "BUF", "MIA"): 1.0}, threshold=2)
            self.assertEqual(rep2["status"], "ready")


class TestTeamHomeField(unittest.TestCase):
    def test_no_home_games_gets_league_number(self):
        played = _played([("KC", "DEN", 24, 20, "")])
        out = ratings.team_home_field_detail(
            played, pd.DataFrame(), {"KC": 5.0, "DEN": -5.0}, 2.0)
        self.assertEqual(out["DEN"]["n"], 0)
        self.assertIsNone(out["DEN"]["raw"])
        self.assertEqual(out["DEN"]["shrunk"], 2.0)

    def test_shrinkage_math_and_neutral_exclusion(self):
        # KC at home twice: residuals actual - (5 - (-5)): game margins
        # 14 and 24 give residuals 4 and 14, raw = 9.0; K=25, n=2 ->
        # w=2/27; shrunk = 2 + 7*2/27 = 2.52 (inside the clamp range).
        # A neutral-site KC home game must not enter the mean.
        played = _played([("KC", "DEN", 24, 10, ""),
                          ("KC", "BUF", 34, 10, ""),
                          ("KC", "MIA", 40, 10, "Neutral")])
        out = ratings.team_home_field_detail(
            played, pd.DataFrame(), {"KC": 5.0, "DEN": -5.0, "BUF": -5.0, "MIA": -5.0},
            2.0, shrinkage_games=25)
        self.assertEqual(out["KC"]["n"], 2)
        self.assertEqual(out["KC"]["raw"], 9.0)
        self.assertAlmostEqual(out["KC"]["shrunk"], 2.52, places=2)

    def test_large_sample_approaches_raw_and_clamp_binds(self):
        rows = [("KC", "DEN", 40, 10, "")] * 200  # residual 20 each -> raw 20
        played = _played(rows)
        out = ratings.team_home_field_detail(
            played, pd.DataFrame(), {"KC": 5.0, "DEN": -5.0}, 2.0,
            shrinkage_games=25)
        # w = 200/225 -> shrunk = 2 + 18*200/225 = 18.0, clamped to HFA_MAX 3.0
        self.assertEqual(out["KC"]["shrunk"], 3.0)

    def test_harness_accepts_team_hfa_variant(self):
        # Tiny two-season frame: prior season feeds ratings, target
        # season week 1 uses them. The assertion is only that the
        # variant runs and grades under the corrected push rule --
        # the real numbers come from the full 2019-2025 run.
        games = pd.DataFrame([
            {"season": 2023, "week": 1, "game_type": "REG", "home_team": "KC",
             "away_team": "DEN", "home_score": 24, "away_score": 20, "result": 4.0,
             "spread_line": 3.0},
            {"season": 2023, "week": 2, "game_type": "REG", "home_team": "BUF",
             "away_team": "MIA", "home_score": 20, "away_score": 27, "result": -7.0,
             "spread_line": 2.0},
            {"season": 2024, "week": 1, "game_type": "REG", "home_team": "KC",
             "away_team": "BUF", "home_score": 27, "away_score": 24, "result": 3.0,
             "spread_line": 2.5},
        ])
        frame = backtest.load_games_frame(games)
        summ = backtest.evaluate_variant(frame, [2024], "team_hfa")
        self.assertEqual(summ["n"], 1)
        rows = summ["_games"].to_dict("records")
        self.assertEqual(len(rows), 1)
        self.assertIn(rows[0]["ats"], ("correct", "wrong", "push"))


class TestDynamicBlend(unittest.TestCase):
    def _row(self, model, line, actual, week=5, season=2024):
        return {"model_margin": model, "spread_line": line,
                "actual_margin": actual, "week": week, "season": season}

    def test_positive_blend_weight_never_flips_a_pick(self):
        for model, line in ((5.0, 3.0), (1.0, 3.0), (-2.0, -3.5), (0.0, 3.0)):
            for w in (0.05, 0.135, 0.30, 1.0):
                blended = dbl.blend_margin(line, model, w)
                self.assertEqual((blended > line), (model > line))
                self.assertEqual((blended < line), (model < line))

    def test_buckets_and_description(self):
        rows = [self._row(8.0, 3.0, 7.0),   # 5+ dev, correct
                self._row(8.0, 3.0, -3.0),  # 5+ dev, wrong
                self._row(3.5, 3.0, 7.0)]   # <1 dev, correct
        desc = dbl.describe(rows)
        self.assertEqual(desc["by_deviation"]["5+"]["decided"], 2)
        self.assertEqual(desc["by_deviation"]["5+"]["ats_pct"], 50.0)
        self.assertEqual(desc["by_deviation"]["<1"]["ats_pct"], 100.0)
        self.assertEqual(dbl.deviation_bucket(8.0, 3.0), "5+")
        self.assertEqual(dbl.week_bucket(3), "1-4")
        self.assertEqual(dbl.week_bucket(12), "10+")

    def test_learned_rule_fades_a_losing_bucket_and_defaults_small_buckets(self):
        # Train: 5+ bucket loses badly (model side 0/40), <1 bucket tiny (1 game).
        train = [self._row(8.0, 3.0, -3.0)] * 40 + [self._row(3.5, 3.0, 7.0)]
        rule = dbl.learn_bucket_rule(train, key="deviation")
        self.assertEqual(rule["5+"], "fade")
        self.assertEqual(rule["<1"], "follow")  # 1 decided < 30 -> default
        self.assertEqual(rule["1-3"], "follow")  # empty -> default
        # Apply on test where the model side keeps losing in 5+: fading wins.
        test = [self._row(8.0, 3.0, -3.0)] * 10
        applied = dbl.apply_bucket_rule(test, rule, key="deviation")
        self.assertEqual(applied["rule"]["ats_pct"], 100.0)
        self.assertEqual(applied["always_follow"]["ats_pct"], 0.0)
        self.assertEqual(applied["paired_vs_follow"]["paired_delta_pp"], 100.0)

    def test_fade_large_rule_flips_only_large_deviations(self):
        rows = [self._row(8.0, 3.0, -3.0),  # 5 dev -> faded -> correct
                self._row(4.0, 3.0, -3.0)]  # 1 dev -> followed -> wrong
        out = dbl.fade_large_rule(rows, edge=5.0)
        self.assertEqual(out["rule"]["correct"], 1)
        self.assertEqual(out["always_follow"]["correct"], 0)

    def test_paired_comparison_excludes_pushes(self):
        # Model margin == line is a push for the baseline; the faded
        # pick on that game cannot manufacture a paired win.
        rows = [self._row(3.0, 3.0, 7.0), self._row(5.0, 3.0, 7.0)]
        rule = {"<1": "fade", "1-3": "follow", "3-5": "follow", "5+": "follow"}
        out = dbl.apply_bucket_rule(rows, rule, key="deviation")
        self.assertEqual(out["paired_vs_follow"]["n"], 1)


class TestFlagsStayOffBatch4GroupB(unittest.TestCase):
    def test_flags_stay_off(self):
        for flag in ("EPA_LIVE", "ENSEMBLE_LIVE", "CALIBRATED_PICK", "TRAVEL_ENABLED",
                     "INJ_AUTO_APPLY", "TOTALS_SRS_LIVE", "STALENESS_LIVE",
                     "UNCERTAINTY_PICK_USE", "PBP_FEATURES_LIVE",
                     "TEAM_HFA_LIVE", "DYNAMIC_BLEND_LIVE"):
            self.assertFalse(getattr(config, flag), flag)


if __name__ == "__main__":
    unittest.main()
