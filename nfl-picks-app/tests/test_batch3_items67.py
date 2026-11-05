"""Tests for batch-3 items 6 and 7 (Thursday 10 AM final run +
what-changed diff; rating uncertainty bands).

Run from nfl-picks-app/: python -m unittest discover -s tests -v
No network, no Streamlit needed. No live behaviour changes: no flag
is flipped, no pick is changed, and every game/team below is
synthetic sample data -- no real picks, lines, or injuries.
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nflpicks import calibration, config, db, run_diff, uncertainty
import weekly_run


def _pick(pick, edge, vegas=None, pool=None, weather="", total_pick=None, model_total=None):
    return {"pick": pick, "edge": edge, "model_independent": edge,
            "vegas_spread": vegas, "pool_spread": pool, "cover_prob": None,
            "weather_note": weather, "total_pick": total_pick, "model_total": model_total}


class TestUncertaintyShape(unittest.TestCase):
    def test_sd_at_zero_games_is_prior(self):
        self.assertAlmostEqual(uncertainty.rating_sd(0), config.UNCERTAINTY_PRIOR_SD)

    def test_sd_narrows_monotonically_but_saturates(self):
        sds = [uncertainty.rating_sd(n) for n in range(0, 18)]
        for a, b in zip(sds, sds[1:]):
            self.assertLess(b, a)
        for sd in sds:
            self.assertGreater(sd, 0)
        # Recency weighting caps the effective sample: a full season
        # is only ~12 effective games, so the late-season sd stays
        # well above zero instead of collapsing toward it.
        self.assertLess(uncertainty.effective_sample_size(17), 17)
        self.assertGreater(uncertainty.effective_sample_size(17), 10)
        self.assertAlmostEqual(uncertainty.effective_sample_size(1), 1.0)
        self.assertEqual(uncertainty.effective_sample_size(0), 0.0)

    def test_sd_reference_values(self):
        # Shape anchors with the shipped defaults (prior 5.0, game
        # SD 13.43): early ~5, mid-season ~4, late ~3.1. These pin
        # the curve so a future constant change shows up here.
        self.assertAlmostEqual(uncertainty.rating_sd(0), 5.0)
        self.assertTrue(3.5 < uncertainty.rating_sd(4) < 4.5)
        self.assertTrue(2.5 < uncertainty.rating_sd(17) < 3.5)

    def test_interval_contains_rating_and_narrows(self):
        lo0, hi0 = uncertainty.rating_interval(6.2, 0)
        lo17, hi17 = uncertainty.rating_interval(6.2, 17)
        self.assertLess(lo0, 6.2)
        self.assertGreater(hi0, 6.2)
        self.assertGreater(hi0 - lo0, hi17 - lo17)
        lo68, hi68 = uncertainty.rating_interval(6.2, 4, z=1.0)
        lo90, hi90 = uncertainty.rating_interval(6.2, 4)
        self.assertGreater(hi90 - lo90, hi68 - lo68)

    def test_margin_sd_combines_both_teams(self):
        import math
        self.assertAlmostEqual(
            uncertainty.margin_sd(0, 0), math.sqrt(2) * config.UNCERTAINTY_PRIOR_SD)
        self.assertLess(uncertainty.margin_sd(17, 17), uncertainty.margin_sd(0, 0))

    def test_cover_interval_contains_point_and_narrows(self):
        # Model margin +3.5 vs line +2.5 (home side favoured).
        point = calibration.cover_probability_for_pick(3.5, 2.5)
        lo0, hi0 = uncertainty.cover_prob_interval(3.5, 2.5, 0, 0)
        lo17, hi17 = uncertainty.cover_prob_interval(3.5, 2.5, 17, 17)
        self.assertLessEqual(lo0, point)
        self.assertGreaterEqual(hi0, point)
        self.assertLessEqual(lo17, point)
        self.assertGreaterEqual(hi17, point)
        self.assertLess(hi17 - lo17, hi0 - lo0)
        for v in (lo0, hi0, lo17, hi17):
            self.assertGreaterEqual(v, 0.01)
            self.assertLessEqual(v, 0.99)

    def test_cover_interval_away_side_mapping(self):
        # Model margin below the line: the model side is away, so
        # the interval must still be lo <= hi and contain the point.
        point = calibration.cover_probability_for_pick(-4.0, 2.5)
        lo, hi = uncertainty.cover_prob_interval(-4.0, 2.5, 4, 4)
        self.assertLessEqual(lo, point)
        self.assertGreaterEqual(hi, point)
        self.assertLessEqual(lo, hi)

    def test_no_pick_logic_uses_uncertainty(self):
        # Labels/bands only by default: the flag stays off, and the
        # model module never imports the uncertainty module.
        self.assertFalse(config.UNCERTAINTY_PICK_USE)
        import nflpicks.model as model_mod
        self.assertNotIn("uncertainty", dir(model_mod))


class TestRunDiff(unittest.TestCase):
    def test_pick_flip_beats_edge_move_and_sorts_first(self):
        old = {("KC", "DEN"): _pick("DEN +3.5", -1.0, vegas=3.5),
               ("MIA", "BUF"): _pick("BUF -6.5", 2.0, vegas=-6.5)}
        new = {("KC", "DEN"): _pick("KC -3.5", 1.5, vegas=3.5),
               ("MIA", "BUF"): _pick("BUF -6.5", 2.8, vegas=-6.5)}
        out = run_diff.build_diff(old, new, {}, {}, [], [], {}, {})
        cats = [i["category"] for i in out["items"]]
        self.assertEqual(cats[0], "PICK_FLIP")
        self.assertIn("EDGE_MOVE", cats)
        # The flipped game is NOT also reported as an edge move.
        self.assertEqual(cats.count("EDGE_MOVE"), 1)
        self.assertEqual(out["items"][0]["matchup"], "DEN @ KC")

    def test_thresholds_filter_small_moves(self):
        old = {("KC", "DEN"): _pick("KC -3.5", 1.0, vegas=3.5)}
        new_small = {("KC", "DEN"): _pick("KC -3.5", 1.4, vegas=3.5)}
        out = run_diff.build_diff(old, new_small, {"KC": 5.0}, {"KC": 5.4},
                                  [], [], {}, {})
        self.assertEqual(out["items"], [])
        new_big = {("KC", "DEN"): _pick("KC -3.5", 1.6, vegas=4.0)}
        out = run_diff.build_diff(old, new_big, {"KC": 5.0}, {"KC": 5.6},
                                  [], [], {}, {})
        cats = {i["category"] for i in out["items"]}
        self.assertEqual(cats, {"EDGE_MOVE", "LINE_MOVE", "RATING_MOVE"})

    def test_injury_new_changed_cleared(self):
        old_inj = [{"team": "KC", "player": "A", "status": "QUESTIONABLE"},
                   {"team": "KC", "player": "B", "status": "OUT"}]
        new_inj = [("KC", "A", "QB", "OUT"), ("KC", "C", "WR", "DOUBTFUL")]
        out = run_diff.build_diff({}, {}, {}, {}, old_inj, new_inj, {}, {})
        by_player = {i["matchup"]: i["category"] for i in out["items"]}
        self.assertEqual(by_player["KC A"], "INJURY_CHANGED")
        self.assertEqual(by_player["KC B"], "INJURY_CLEARED")
        self.assertEqual(by_player["KC C"], "INJURY_NEW")

    def test_weather_total_and_new_game(self):
        old = {("KC", "DEN"): _pick("KC -3.5", 1.0, vegas=3.5, weather="",
                                    total_pick="OVER 44.5", model_total=46.0)}
        new = {("KC", "DEN"): _pick("KC -3.5", 1.0, vegas=3.5, weather="wind 22mph (high)",
                                    total_pick="UNDER 44.5", model_total=43.0),
               ("MIA", "BUF"): _pick("BUF -6.5", 2.0, vegas=-6.5)}
        out = run_diff.build_diff(old, new, {}, {}, [], [], {}, {})
        cats = {i["category"] for i in out["items"]}
        self.assertEqual(cats, {"WEATHER_CHANGED", "TOTAL_CHANGE", "NEW_GAME"})

    def test_empty_old_state_is_first_run_not_no_change(self):
        out = run_diff.build_diff({}, {}, {}, {}, [], [], {}, {})
        self.assertEqual(out["items"], [])
        self.assertIn("First saved run", out["summary"])

    def test_pick_side_parsing(self):
        self.assertEqual(run_diff.pick_side("KC -3.5", "KC", "DEN"), "home")
        self.assertEqual(run_diff.pick_side("DEN +3.5", "KC", "DEN"), "away")
        self.assertEqual(run_diff.pick_side("KC PK", "KC", "DEN"), "home")
        self.assertIsNone(run_diff.pick_side(None, "KC", "DEN"))
        self.assertIsNone(run_diff.pick_side("", "KC", "DEN"))


class TestRunDiffPersistence(unittest.TestCase):
    def test_save_and_latest_round_trip(self):
        with tempfile.TemporaryDirectory() as td:
            path = str(Path(td) / "t.db")
            db.init_db(path)
            items = [{"matchup": "DEN @ KC", "category": "PICK_FLIP",
                      "detail": "Pick flipped: DEN +3.5 -> KC -3.5.",
                      "magnitude": 2.5, "home": "KC", "away": "DEN"}]
            db.save_run_diff(2026, 10, items, run_time="2026-11-05T18:00:00", db_path=path)
            got = db.get_latest_run_diff(2026, 10, db_path=path)
            self.assertEqual(got["run_time"], "2026-11-05T18:00:00")
            self.assertEqual(len(got["items"]), 1)
            self.assertEqual(got["items"][0]["category"], "PICK_FLIP")
            # A later run's diff does not overwrite the earlier one;
            # latest wins, and the earlier rows still exist.
            db.save_run_diff(2026, 10, [{"matchup": "BUF @ MIA", "category": "EDGE_MOVE",
                                         "detail": "Edge moved.", "magnitude": 0.6,
                                         "home": "MIA", "away": "BUF"}],
                             run_time="2026-11-05T19:00:00", db_path=path)
            got2 = db.get_latest_run_diff(2026, 10, db_path=path)
            self.assertEqual(got2["run_time"], "2026-11-05T19:00:00")
            self.assertEqual(len(got2["items"]), 1)
            self.assertEqual(got2["items"][0]["category"], "EDGE_MOVE")
            with db.connect(path) as conn:
                n = conn.execute("SELECT COUNT(*) AS n FROM run_diffs WHERE season=2026 AND week=10").fetchone()["n"]
            self.assertEqual(n, 2)  # both runs' rows survive
            empty = db.get_latest_run_diff(2026, 11, db_path=path)
            self.assertEqual(empty, {"run_time": None, "items": []})

    def test_capture_previous_state(self):
        with tempfile.TemporaryDirectory() as td:
            path = str(Path(td) / "t.db")
            db.init_db(path)
            self.assertIsNone(weekly_run._capture_previous_state(2026, 10, db_path=path))
            with db.connect(path) as conn:
                conn.execute(
                    "INSERT INTO picks (season, week, home_team, away_team, vegas_spread, "
                    "model_spread, edge, pick, confidence, strong_edge, run_time) "
                    "VALUES (2026, 10, 'KC', 'DEN', 3.5, 4.0, 0.5, 'KC -3.5', 1.0, 0, '2026-11-05T15:00:00')")
                conn.execute(
                    "INSERT INTO team_ratings_snapshot (season, week, team, rating, "
                    "games_played_this_season, run_time) VALUES (2026, 10, 'KC', 5.0, 8, '2026-11-05T15:00:00')")
                conn.execute(
                    "INSERT INTO injury_review (season, week, team, player, position, status) "
                    "VALUES (2026, 10, 'KC', 'A', 'QB', 'OUT')")
            prev = weekly_run._capture_previous_state(2026, 10, db_path=path)
            self.assertIsNotNone(prev)
            self.assertEqual(prev["old_run_time"], "2026-11-05T15:00:00")
            self.assertEqual(prev["old_picks"][("KC", "DEN")]["pick"], "KC -3.5")
            self.assertEqual(prev["old_ratings"]["KC"], 5.0)
            self.assertEqual(prev["old_injuries"][0]["player"], "A")


if __name__ == "__main__":
    unittest.main()
