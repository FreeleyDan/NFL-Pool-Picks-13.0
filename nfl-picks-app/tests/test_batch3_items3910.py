"""Tests for batch-3 items 3, 9, and 10 (error decomposition, actual
pool results grading, data-freshness warnings).

Run from nfl-picks-app/: python -m unittest discover -s tests -v
No network, no Streamlit needed. No live behaviour changes: no flag
is flipped, no pick is changed, and the pool-results sample data below
is synthetic and labelled SAMPLE -- it is not real results.
"""
import contextlib
import io
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from nflpicks import backtest, config, db, error_decomposition as ed, freshness, pool_results


def _row(home, away, pick, line, actual, model=None, week=5, season=2024,
         total=None, div=None, wind=None):
    return {"season": season, "week": week, "home_team": home, "away_team": away,
            "pick_team": pick, "line": line, "actual_margin": actual,
            "model_margin": model if model is not None else line + 1.0,
            "total_line": total, "div_game": div, "wind": wind}


def _norm(rows):
    return [r for r in (ed.normalize_row(r) for r in rows) if r is not None]


class TestErrorDecomposition(unittest.TestCase):
    def test_slice_ats_and_mae_exact(self):
        # 4 games: 2 home-fav wins, 1 home-fav loss, 1 away-dog win.
        # Model margins chosen so MAE is exactly computable.
        rows = _norm([
            _row("KC", "DEN", "KC", 3.0, 7.0, model=5.0),
            _row("KC", "BUF", "KC", 3.0, 10.0, model=6.0),
            _row("DAL", "PHI", "DAL", 3.0, -3.0, model=5.0),  # home pick loses
            _row("MIA", "NE", "NE", 3.0, -7.0, model=-5.0),  # away-dog pick wins
        ])
        out = ed.decompose(rows)
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["n"], 4)
        self.assertEqual(out["overall"]["wins"], 3)
        self.assertEqual(out["overall"]["ats_pct"], 75.0)
        # MAE: |7-5| + |10-6| + |-3-5| + |-7+5| = 2+4+8+2 = 16/4 = 4.0
        self.assertEqual(out["overall"]["mae"], 4.0)
        fav = out["slices"]["favdog"]["favorite"]
        self.assertEqual((fav["wins"], fav["losses"]), (2, 1))
        dog = out["slices"]["favdog"]["underdog"]
        self.assertEqual((dog["wins"], dog["losses"]), (1, 0))
        home = out["slices"]["side"]["home"]
        self.assertEqual(home["n"], 3)
        self.assertEqual(out["slices"]["side"]["away"]["n"], 1)

    def test_push_excluded_from_ats_but_counted(self):
        rows = _norm([
            _row("KC", "DEN", "KC", 3.0, 3.0, model=5.0),  # push
            _row("BUF", "MIA", "BUF", 3.0, 7.0, model=5.0),
        ])
        out = ed.decompose(rows)
        self.assertEqual(out["overall"]["pushes"], 1)
        self.assertEqual(out["overall"]["decided"], 1)
        self.assertEqual(out["overall"]["ats_pct"], 100.0)

    def test_small_sample_flag_and_worst_ranking_excludes_small(self):
        # One big slice (40 decided, 50%) and one tiny slice (2 decided, 0%).
        rows = []
        for i in range(40):
            # Alternate win/loss on a home pick, line 3, actual +/-.
            rows.append(_row("KC", "DEN", "KC", 3.0, 7.0 if i % 2 == 0 else -3.0,
                             model=5.0, week=5))
        rows.append(_row("BUF", "MIA", "BUF", 3.0, -3.0, model=5.0, week=5))
        rows.append(_row("BUF", "NE", "BUF", 3.0, -3.0, model=5.0, week=5))
        out = ed.decompose(_norm(rows))
        team = out["slices"]["team"]
        self.assertTrue(team["BUF"]["small_sample"])
        self.assertFalse(team["KC"]["small_sample"])
        worst_teams = [w for w in out["worst_slices"] if w["dimension"] == "team"]
        self.assertNotIn("BUF", [w["slice"] for w in worst_teams])

    def test_unknown_context_buckets_and_coverage(self):
        rows = _norm([_row("KC", "DEN", "KC", 3.0, 7.0, model=5.0)])
        out = ed.decompose(rows)
        self.assertEqual(out["slices"]["total"]["unknown"]["n"], 1)
        self.assertEqual(out["slices"]["weather"]["unknown"]["n"], 1)
        self.assertEqual(out["slices"]["division"]["unknown"]["n"], 1)
        self.assertEqual(out["coverage"]["total_unknown"], 1)
        self.assertEqual(out["coverage"]["wind_unknown"], 1)

    def test_buckets_spread_total_weather_division_phase(self):
        rows = _norm([
            _row("KC", "DEN", "KC", 0.0, 3.0, model=1.0, week=2, total=38.0, div=1, wind=25.0),
            _row("BUF", "MIA", "BUF", -7.5, -10.0, model=-9.0, week=12, total=52.0, div=0, wind=5.0),
        ])
        out = ed.decompose(rows)
        self.assertEqual(out["slices"]["spread"]["pick'em"]["n"], 1)
        self.assertEqual(out["slices"]["spread"]["large (7.5+)"]["n"], 1)
        self.assertEqual(out["slices"]["total"]["low (<40)"]["n"], 1)
        self.assertEqual(out["slices"]["total"]["high (50+)"]["n"], 1)
        self.assertEqual(out["slices"]["weather"]["high wind (20+ mph)"]["n"], 1)
        self.assertEqual(out["slices"]["weather"]["calm (<15 mph)"]["n"], 1)
        self.assertEqual(out["slices"]["division"]["divisional"]["n"], 1)
        self.assertEqual(out["slices"]["division"]["non-divisional"]["n"], 1)
        self.assertEqual(out["slices"]["season_phase"]["early (weeks 1-4)"]["n"], 1)
        self.assertEqual(out["slices"]["season_phase"]["late (weeks 10+)"]["n"], 1)

    def test_empty_input_is_empty_state_not_zeros(self):
        out = ed.decompose([])
        self.assertEqual(out["status"], "empty")
        self.assertIsNone(out["overall"])

    def test_attach_context_joins_and_never_invents(self):
        rows = _norm([_row("KC", "DEN", "KC", 3.0, 7.0, model=5.0)])
        games = pd.DataFrame([{"season": 2024, "week": 5, "home_team": "KC",
                               "away_team": "DEN", "total_line": 44.5,
                               "div_game": 1, "wind": 18.0}])
        ed.attach_context(rows, games)
        out = ed.decompose(rows)
        self.assertEqual(out["slices"]["total"]["mid-low (40-45)"]["n"], 1)
        self.assertEqual(out["slices"]["weather"]["moderate wind (15-20 mph)"]["n"], 1)
        self.assertEqual(out["coverage"]["total_unknown"], 0)
        # A row with no matching source game stays unknown.
        rows2 = _norm([_row("DAL", "PHI", "DAL", 3.0, 7.0, model=5.0)])
        ed.attach_context(rows2, games)
        self.assertEqual(ed.decompose(rows2)["coverage"]["total_unknown"], 1)

    def test_backtest_rows_normalize_with_same_push_rule(self):
        # Synthetic backtest-shaped frame through the real harness path.
        games = pd.DataFrame([
            {"season": 2024, "week": 1, "game_type": "REG", "home_team": "KC",
             "away_team": "DEN", "home_score": 24, "away_score": 20,
             "result": 4.0, "spread_line": 3.0},
            {"season": 2024, "week": 1, "game_type": "REG", "home_team": "BUF",
             "away_team": "MIA", "home_score": 20, "away_score": 27,
             "result": -7.0, "spread_line": -3.0},
        ])
        frame = backtest.load_games_frame(games)
        # evaluate_variant needs a prior season for ratings; with none
        # it still runs (prior_games empty) -- but week-1 games have no
        # played_current, so ratings fall back to empty/zero. The point
        # here is normalization consistency, not a meaningful ATS%.
        summ = backtest.evaluate_variant(frame, [2024], "baseline")
        rows = ed.normalize_backtest_rows(summ["_games"])
        self.assertEqual(len(rows), 2)
        for r, g in zip(rows, summ["_games"].to_dict("records")):
            self.assertEqual(r["ats"], g["ats"])

    def test_flags_stay_off(self):
        for flag in ("EPA_LIVE", "ENSEMBLE_LIVE", "CALIBRATED_PICK", "TRAVEL_ENABLED",
                     "INJ_AUTO_APPLY", "TOTALS_SRS_LIVE", "STALENESS_LIVE"):
            self.assertFalse(getattr(config, flag), flag)


class TestPoolResults(unittest.TestCase):
    SAMPLE_CSV = """season,week,away_team,home_team,submitted_team,note
2026,1,DEN,KC,KC,flipped on Thursday review
2026,1,BUF,MIA,MIA,
2026,1,NE,NYJ,NYJ,SAMPLE ONLY -- not real results
2026,1,DAL,PHI,PHI,SAMPLE ONLY -- not real results
"""

    def _model_picks(self):
        # Model picks: KC (home fav), BUF (away fav), NE (away), DAL (away).
        return [
            {"season": 2026, "week": 1, "away_team": "DEN", "home_team": "KC",
             "pick": "KC -3.5", "pool_spread": 3.5, "vegas_spread": 3.0},
            {"season": 2026, "week": 1, "away_team": "BUF", "home_team": "MIA",
             "pick": "BUF -3.5", "pool_spread": -3.5, "vegas_spread": -3.0},
            {"season": 2026, "week": 1, "away_team": "NE", "home_team": "NYJ",
             "pick": "NE +3.5", "pool_spread": 3.5, "vegas_spread": 3.0},
            {"season": 2026, "week": 1, "away_team": "DAL", "home_team": "PHI",
             "pick": "DAL +3.5", "pool_spread": 3.5, "vegas_spread": 3.0},
        ]

    def _results(self):
        # KC wins by 7 (home covers), MIA wins by 10 (home covers),
        # NYJ wins by 3 (push vs 3.5? no: 3 < 3.5 so away covers),
        # PHI wins by 7 (home covers).
        return {(2026, 1, "DEN", "KC"): 7.0, (2026, 1, "BUF", "MIA"): 10.0,
                (2026, 1, "NE", "NYJ"): 3.0, (2026, 1, "DAL", "PHI"): 7.0}

    def test_parse_valid_and_reject_bad_team(self):
        parsed = pool_results.parse_pool_results_csv(
            self.SAMPLE_CSV + "2026,1,SEA,SF,GB,bad team not in game\n")
        self.assertEqual(len(parsed["rows"]), 4)
        self.assertEqual(len(parsed["errors"]), 1)
        self.assertIn("plays in neither team", parsed["errors"][0])

    def test_parse_pick_string_fallback(self):
        parsed = pool_results.parse_pool_results_csv(
            "season,week,away_team,home_team,submitted_pick\n2026,2,DEN,KC,KC -3.5\n")
        self.assertEqual(parsed["rows"][0]["submitted_team"], "KC")

    def test_grade_helped_hurt_same(self):
        parsed = pool_results.parse_pool_results_csv(self.SAMPLE_CSV)
        out = pool_results.grade_pool_results(
            2026, self._model_picks(), parsed["rows"], self._results())
        self.assertEqual(out["status"], "ok")
        # Game 1: model KC correct, submitted KC -> same.
        # Game 2: model BUF wrong (MIA covers), submitted MIA correct -> helped.
        # Game 3: model NE correct (3 < 3.5, away covers), submitted NYJ wrong -> hurt.
        # Game 4: model DAL wrong, submitted PHI correct -> helped.
        self.assertEqual(out["differences"], {"same": 1, "diff_helped": 2,
                                              "diff_hurt": 1, "diff_neutral": 0})
        self.assertEqual(out["net_diff_wins"], 1)
        self.assertEqual(out["model"]["record"], "2-2")
        self.assertEqual(out["submitted"]["record"], "3-1")

    def test_push_game_is_neutral_not_helped_or_hurt(self):
        picks = [{"season": 2026, "week": 1, "away_team": "DEN", "home_team": "KC",
                  "pick": "KC -3.5", "pool_spread": 3.0, "vegas_spread": 3.0}]
        sub = [{"season": 2026, "week": 1, "away_team": "DEN", "home_team": "KC",
                "submitted_team": "DEN", "note": ""}]
        out = pool_results.grade_pool_results(
            2026, picks, sub, {(2026, 1, "DEN", "KC"): 3.0})
        self.assertEqual(out["differences"]["diff_neutral"], 1)
        self.assertEqual(out["model"]["pushes"], 1)
        self.assertEqual(out["submitted"]["pushes"], 1)

    def test_empty_state_has_no_record(self):
        out = pool_results.grade_pool_results(2026, self._model_picks(), [], self._results())
        self.assertEqual(out["status"], "empty")
        self.assertNotIn("model", out)
        self.assertNotIn("submitted", out)

    def test_db_roundtrip_and_upsert(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "t.db")
            db.init_db(path)
            parsed = pool_results.parse_pool_results_csv(self.SAMPLE_CSV)
            self.assertEqual(pool_results.save_pool_results(parsed["rows"], path), 4)
            loaded = pool_results.load_pool_results(2026, path)
            self.assertEqual(len(loaded), 4)
            # Re-import with one corrected pick: upsert, not duplicate.
            fixed = pool_results.parse_pool_results_csv(
                "season,week,away_team,home_team,submitted_team\n2026,1,DEN,KC,DEN\n")
            pool_results.save_pool_results(fixed["rows"], path)
            loaded = pool_results.load_pool_results(2026, path)
            self.assertEqual(len(loaded), 4)
            kc = [r for r in loaded if r["home_team"] == "KC"][0]
            self.assertEqual(kc["submitted_team"], "DEN")

    def test_grade_season_from_db_sample(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "t.db")
            db.init_db(path)
            pool_results.import_pool_results_csv(self.SAMPLE_CSV, path)
            with db.connect(path) as conn:
                for p in self._model_picks():
                    conn.execute(
                        "INSERT INTO picks (season, week, home_team, away_team, pick, "
                        "pool_spread, vegas_spread) VALUES (?,?,?,?,?,?,?)",
                        (p["season"], p["week"], p["home_team"], p["away_team"],
                         p["pick"], p["pool_spread"], p["vegas_spread"]))
            out = pool_results.grade_season_from_db(2026, self._results(), path)
            self.assertEqual(out["net_diff_wins"], 1)


class TestFreshness(unittest.TestCase):
    # Reference week: Wed 2026-11-04 16:19 UTC. Last scheduled slot is
    # Tue 2026-11-03 15:00 UTC (Thu slot is in the future).
    NOW = datetime(2026, 11, 4, 16, 19, tzinfo=timezone.utc)

    def test_last_scheduled_run_tue_thu(self):
        self.assertEqual(freshness.last_scheduled_run(self.NOW),
                         datetime(2026, 11, 3, 15, 0, tzinfo=timezone.utc))
        # Friday: last slot is Thu. Monday: last slot is Thu (4 days back).
        self.assertEqual(freshness.last_scheduled_run(
            datetime(2026, 11, 6, 10, 0, tzinfo=timezone.utc)),
            datetime(2026, 11, 5, 15, 0, tzinfo=timezone.utc))
        self.assertEqual(freshness.last_scheduled_run(
            datetime(2026, 11, 9, 10, 0, tzinfo=timezone.utc)),
            datetime(2026, 11, 5, 15, 0, tzinfo=timezone.utc))

    def test_fresh_ok(self):
        v = freshness.check_run_freshness(
            datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc), self.NOW)
        self.assertEqual(v["level"], "ok")
        self.assertFalse(v["missed_scheduled_run"])

    def test_week_old_is_stale(self):
        v = freshness.check_run_freshness(
            datetime(2026, 10, 27, 15, 0, tzinfo=timezone.utc), self.NOW)
        self.assertEqual(v["level"], "stale")
        self.assertGreater(v["age_hours"], 168.0)

    def test_missed_scheduled_run_warns(self):
        # Latest run is Mon Nov 2 -- before Tue Nov 3's slot, and now is
        # past Tue 15:00 + grace, so the Tue run was missed.
        v = freshness.check_run_freshness(
            datetime(2026, 11, 2, 15, 0, tzinfo=timezone.utc), self.NOW)
        self.assertEqual(v["level"], "warn")
        self.assertTrue(v["missed_scheduled_run"])

    def test_grace_window_does_not_flag_missed(self):
        # Now is Tue 16:00 UTC, 1h after the slot: within the 3h grace,
        # so a run that predates the slot is not yet "missed".
        now = datetime(2026, 11, 3, 16, 0, tzinfo=timezone.utc)
        v = freshness.check_run_freshness(
            datetime(2026, 10, 29, 15, 0, tzinfo=timezone.utc), now)
        self.assertFalse(v["missed_scheduled_run"])

    def test_no_run_is_stale(self):
        v = freshness.check_run_freshness(None, self.NOW)
        self.assertEqual(v["level"], "stale")

    def test_db_status_fresh_and_stale_and_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = str(Path(tmp) / "nope.db")
            st = freshness.freshness_status(2026, missing, self.NOW)
            self.assertEqual(st["level"], "stale")
            self.assertFalse(Path(missing).exists())  # check must not create a DB
            path = str(Path(tmp) / "t.db")
            db.init_db(path)
            st = freshness.freshness_status(2026, path, self.NOW)
            self.assertEqual(st["level"], "stale")  # no picks yet
            with db.connect(path) as conn:
                conn.execute(
                    "INSERT INTO picks (season, week, home_team, away_team, run_time) "
                    "VALUES (2026, 10, 'KC', 'DEN', '2026-11-04T12:00:00')")
                conn.execute(
                    "INSERT INTO run_diagnostics (run_time, step, ok, detail) "
                    "VALUES ('2026-11-04T12:00:00', 'Weather fetch', 0, 'API down')")
            st = freshness.freshness_status(2026, path, self.NOW)
            self.assertEqual(st["level"], "warn")  # fresh run, but a failed step
            self.assertEqual(len(st["failed_steps"]), 1)
            self.assertIn("NOT live", st["note"])

    def test_cli_exit_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "t.db")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(freshness.main(["--db", path]), 1)  # missing -> stale
            db.init_db(path)
            with db.connect(path) as conn:
                conn.execute(
                    "INSERT INTO picks (season, week, home_team, away_team, run_time) "
                    "VALUES (2026, 10, 'KC', 'DEN', '2026-11-04T12:00:00')")
            # CLI uses real now; the assertion here is only that it runs
            # and returns an int exit code (0 ok/warn, 1 stale).
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertIn(freshness.main(["--db", path, "--season", "2026"]), (0, 1))
                # Report-only mode never fails, even on a stale/missing DB.
                self.assertEqual(freshness.main(["--db", str(Path(tmp) / "gone.db"),
                                                 "--no-fail"]), 0)


if __name__ == "__main__":
    unittest.main()
