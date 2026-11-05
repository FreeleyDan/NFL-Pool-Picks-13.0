"""Tests for batch-3 items 4 and 5 (pool-line importer, manual-
adjustment persistence).

Run from nfl-picks-app/: python -m unittest discover -s tests -v
No network, no Streamlit needed. No live behaviour changes: no flag
is flipped, no pick is changed, and every pool line and adjustment
below is synthetic sample data labelled as such -- no real Barbara
pool lines were supplied or invented for this build.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nflpicks import config, db, manual_adjustments as ma, pool_lines as pl


def _brow(season, week, home, away, margin, recorded, actual):
    return {"season": season, "week": week, "home_team": home, "away_team": away,
            "model_margin": margin, "spread_line": recorded, "actual_margin": actual}


class TestPoolLinesParsing(unittest.TestCase):
    def test_sportsbook_default_converts_to_nflverse(self):
        # SAMPLE ONLY: pool sheet prints KC -3.5 (home favorite);
        # internally that is +3.5 nflverse.
        parsed = pl.parse_pool_lines_csv(
            "season,week,away_team,home_team,pool_spread,pool_total,note\n"
            "2024,1,DEN,KC,-3.5,,SAMPLE ONLY\n"
            "2024,1,BUF,MIA,2.5,,SAMPLE ONLY\n")
        self.assertEqual(parsed["errors"], [])
        by_key = {(r["away_team"], r["home_team"]): r for r in parsed["rows"]}
        self.assertEqual(by_key[("DEN", "KC")]["pool_spread"], 3.5)
        self.assertEqual(by_key[("DEN", "KC")]["pool_spread_as_entered"], -3.5)
        self.assertEqual(by_key[("DEN", "KC")]["convention"], "sportsbook")
        # MIA +2.5 sportsbook (home dog) -> -2.5 nflverse.
        self.assertEqual(by_key[("BUF", "MIA")]["pool_spread"], -2.5)

    def test_nflverse_convention_column_passes_through(self):
        parsed = pl.parse_pool_lines_csv(
            "season,week,away_team,home_team,pool_spread,convention\n"
            "2024,1,DEN,KC,3.5,nflverse\n")
        self.assertEqual(parsed["errors"], [])
        self.assertEqual(parsed["rows"][0]["pool_spread"], 3.5)

    def test_rejections_and_duplicate_last_wins(self):
        parsed = pl.parse_pool_lines_csv(
            "season,week,away_team,home_team,pool_spread,convention\n"
            "2024,1,DEN,KC,-3.5,sportsbook\n"
            "notanint,1,DEN,KC,-3.5,sportsbook\n"
            "2024,1,DEN,KC,abc,sportsbook\n"
            "2024,1,DEN,KC,-4.5,sideways\n"
            "2024,1,DEN,KC,-6.5,sportsbook\n")
        # Only the first and last valid rows survive; last wins (6.5 nflverse).
        self.assertEqual(len(parsed["rows"]), 1)
        self.assertEqual(parsed["rows"][0]["pool_spread"], 6.5)
        self.assertEqual(len(parsed["errors"]), 4)  # bad season, bad spread, bad convention, duplicate note

    def test_missing_columns_and_empty(self):
        self.assertTrue(pl.parse_pool_lines_csv("season,week\n2024,1\n")["errors"])
        self.assertTrue(pl.parse_pool_lines_csv("")["errors"])
        loaded = pl.load_pool_lines_file("/nonexistent/pool_lines.csv")
        self.assertEqual(loaded["status"], "empty")
        self.assertEqual(loaded["rows"], [])


class TestPoolLinesGrading(unittest.TestCase):
    def test_empty_state_is_not_zero_zero(self):
        out = pl.grade_pool_lines([_brow(2024, 1, "KC", "DEN", 5.0, 3.0, 7.0)], [])
        self.assertEqual(out["status"], "empty")
        self.assertNotIn("pool", out)

    def test_recorded_vs_pool_delta_and_pick_flips(self):
        # SAMPLE ONLY games, model margin 4.0 throughout. All three
        # picks are correct on BOTH lines (g2: actual 4.0 < pool 5.0
        # and margin 4.0 < 5.0, so the away pick is correct on the
        # pool line too), so both ATS% are 3-0 and the delta is 0 --
        # but the pick itself flips on two games (g2: home on the
        # recorded line, away on the pool line; g3: away, then home),
        # which the flip count catches even when the record does
        # not move.
        rows = [_brow(2024, 1, "KC", "DEN", 4.0, 3.0, 7.0),
                _brow(2024, 1, "DAL", "PHI", 4.0, 3.0, 4.0),
                _brow(2024, 2, "MIA", "NE", 4.0, 5.0, 4.0)]
        pool_rows = [
            {"season": 2024, "week": 1, "away_team": "DEN", "home_team": "KC",
             "pool_spread": 3.5, "pool_total": None},
            {"season": 2024, "week": 1, "away_team": "PHI", "home_team": "DAL",
             "pool_spread": 5.0, "pool_total": None},
            {"season": 2024, "week": 2, "away_team": "NE", "home_team": "MIA",
             "pool_spread": 3.0, "pool_total": None},
        ]
        out = pl.grade_pool_lines(rows, pool_rows)
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["n_matched"], 3)
        self.assertEqual(out["recorded"], {"wins": 3, "losses": 0, "pushes": 0,
                                           "decided": 3, "record": "3-0", "ats_pct": 100.0})
        self.assertEqual(out["pool"], {"wins": 3, "losses": 0, "pushes": 0,
                                       "decided": 3, "record": "3-0", "ats_pct": 100.0})
        self.assertEqual(out["delta_pp"], 0.0)
        self.assertEqual(out["pick_flips"], 2)
        self.assertEqual(out["per_season"]["2024"]["pool"]["wins"], 3)

    def test_pool_line_harder_delta(self):
        # Same picks, pool line 1 pt worse for the model: recorded
        # 2-0 becomes pool 0-2, delta -100 pp on this tiny sample.
        # Batch 4 note: the old version of this test used a pool
        # line exactly equal to the model margin (4.0), which the
        # old rule graded as an away pick (wrong); the corrected
        # rule grades that a push, so the numbers now use a pool
        # line strictly between the actual margin and the model
        # margin instead -- same lesson, no tie involved.
        rows = [_brow(2024, 1, "KC", "DEN", 5.0, 3.5, 4.0),
                _brow(2024, 1, "DAL", "PHI", 5.0, 3.5, 4.0)]
        pool_rows = [
            {"season": 2024, "week": 1, "away_team": "DEN", "home_team": "KC",
             "pool_spread": 4.5, "pool_total": None},
            {"season": 2024, "week": 1, "away_team": "PHI", "home_team": "DAL",
             "pool_spread": 4.5, "pool_total": None},
        ]
        out = pl.grade_pool_lines(rows, pool_rows)
        # Recorded: model 5.0 > 3.5 -> home pick, actual 4.0 > 3.5
        # -> home covers, correct. Pool: model 5.0 > 4.5 -> home
        # pick, actual 4.0 < 4.5 -> away covers, wrong.
        self.assertEqual(out["recorded"]["record"], "2-0")
        self.assertEqual(out["pool"]["record"], "0-2")
        self.assertEqual(out["delta_pp"], -100.0)

    def test_pool_line_margin_equals_line_is_push(self):
        # Batch 4: a model margin exactly equal to the pool line is
        # a push against that line (and a pick against the recorded
        # line, where it differs) -- not an away pick.
        rows = [_brow(2024, 1, "KC", "DEN", 4.0, 3.0, 3.5)]
        pool_rows = [{"season": 2024, "week": 1, "away_team": "DEN", "home_team": "KC",
                      "pool_spread": 4.0, "pool_total": None}]
        out = pl.grade_pool_lines(rows, pool_rows)
        self.assertEqual(out["recorded"]["record"], "1-0")
        self.assertEqual(out["pool"]["pushes"], 1)
        self.assertEqual(out["pool"]["decided"], 0)

    def test_push_and_unmatched_handling(self):
        rows = [_brow(2024, 1, "KC", "DEN", 4.0, 3.0, 3.0),  # actual == recorded -> push
                _brow(2024, 1, "DAL", "PHI", 4.0, 3.0, 7.0)]  # no pool row -> excluded both sides
        pool_rows = [
            {"season": 2024, "week": 1, "away_team": "DEN", "home_team": "KC",
             "pool_spread": 3.0, "pool_total": None},
            {"season": 2024, "week": 9, "away_team": "XXX", "home_team": "YYY",
             "pool_spread": 3.0, "pool_total": None},
        ]
        out = pl.grade_pool_lines(rows, pool_rows)
        self.assertEqual(out["n_matched"], 1)
        self.assertEqual(out["n_unmatched_pool_rows"], 1)
        self.assertEqual(out["recorded"]["pushes"], 1)
        self.assertEqual(out["pool"]["pushes"], 1)
        self.assertIsNone(out["recorded"]["ats_pct"])  # 0 decided -> None, not 0.0
        attached = pl.attach_pool_lines(rows, pool_rows)
        self.assertEqual(len(attached), 1)
        self.assertEqual(attached[0]["pool_line"], 3.0)

    def test_sample_csv_parses(self):
        parsed = pl.parse_pool_lines_csv(pl.SAMPLE_CSV)
        self.assertEqual(len(parsed["rows"]), 2)
        self.assertIn("SAMPLE ONLY", parsed["rows"][0]["note"])


class TestManualAdjustmentsFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "manual_adjustments.json"
        self.db_path = str(Path(self.tmp.name) / "test.db")
        db.init_db(self.db_path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_set_delete_roundtrip_with_audit_trail(self):
        ma.set_adjustment("KC", 1.5, reason="SAMPLE ankle", actor="tester", path=self.path)
        ma.set_adjustment("KC", 2.0, reason="SAMPLE ankle worse", actor="tester", path=self.path)
        ma.set_adjustment("DEN", -1.0, reason="SAMPLE weather", actor="tester", path=self.path)
        self.assertEqual(ma.load_adjustments(self.path), {"KC": 2.0, "DEN": -1.0})
        doc = ma.load_doc(self.path)
        self.assertEqual(doc["schema_version"], 1)
        self.assertEqual(doc["revision"], 3)
        hist = ma.load_history(self.path)
        self.assertEqual(len(hist), 3)
        self.assertEqual(hist[0]["points_before"], None)
        self.assertEqual(hist[1]["points_before"], 1.5)
        self.assertEqual(hist[1]["points_after"], 2.0)
        self.assertEqual(hist[1]["actor"], "tester")
        self.assertTrue(ma.delete_adjustment("DEN", reason="SAMPLE resolved", path=self.path))
        self.assertEqual(ma.load_adjustments(self.path), {"KC": 2.0})
        self.assertEqual(ma.load_history(self.path)[-1]["action"], "delete")
        self.assertFalse(ma.delete_adjustment("DEN", path=self.path))  # no-op, no revision bump
        self.assertEqual(ma.load_doc(self.path)["revision"], 4)

    def test_corrupt_file_raises_not_empty(self):
        self.path.write_text("{not json")
        with self.assertRaises(ValueError):
            ma.load_doc(self.path)
        self.path.write_text(json.dumps({"schema_version": 99, "adjustments": [], "history": []}))
        with self.assertRaises(ValueError):
            ma.load_doc(self.path)
        self.assertEqual(ma.load_adjustments(Path(self.tmp.name) / "missing.json"), {})

    def test_import_merge_and_export_roundtrip(self):
        ma.set_adjustment("KC", 1.5, reason="SAMPLE", path=self.path)
        out = ma.import_text(json.dumps({
            "schema_version": 1,
            "adjustments": [{"team": "KC", "points": 2.5, "reason": "SAMPLE updated"},
                            {"team": "BUF", "points": -1.0, "reason": "SAMPLE new"}]}),
            actor="importer", path=self.path)
        self.assertEqual(sorted(out["changed"]), ["BUF", "KC"])
        self.assertEqual(ma.load_adjustments(self.path), {"KC": 2.5, "BUF": -1.0})
        # Re-importing identical values changes nothing and does not bump revision.
        rev = ma.load_doc(self.path)["revision"]
        out2 = ma.import_text(ma.export_text(self.path), path=self.path)
        self.assertEqual(out2["changed"], [])
        self.assertEqual(ma.load_doc(self.path)["revision"], rev)
        with self.assertRaises(ValueError):
            ma.import_text("{bad json", path=self.path)
        with self.assertRaises(ValueError):
            ma.import_text(json.dumps({"schema_version": 2, "adjustments": []}), path=self.path)

    def test_weekly_merge_file_wins_db_only_survives(self):
        db.set_manual_adjustment("KC", 1.0, "SAMPLE db", db_path=self.db_path)
        db.set_manual_adjustment("DEN", -1.0, "SAMPLE db", db_path=self.db_path)
        ma.set_adjustment("KC", 2.0, reason="SAMPLE file", path=self.path)
        weekly = ma.load_weekly_adjustments(db_path=self.db_path, file_path=self.path)
        self.assertEqual(weekly["adjustments"], {"KC": 2.0, "DEN": -1.0})
        self.assertEqual(weekly["db_only_teams"], ["DEN"])
        self.assertIn("source of truth", weekly["source_note"])
        self.assertEqual(weekly["reasons"]["KC"], "SAMPLE file")
        # No file at all -> database only, with the plain-language note.
        weekly2 = ma.load_weekly_adjustments(
            db_path=self.db_path, file_path=Path(self.tmp.name) / "missing.json")
        self.assertFalse(weekly2["file_exists"])
        self.assertEqual(weekly2["adjustments"], {"KC": 1.0, "DEN": -1.0})

    def test_parse_import_entries_does_not_write_file(self):
        entries = ma.parse_import_entries(json.dumps({
            "schema_version": 1,
            "adjustments": [{"team": "KC", "points": 1.0, "reason": "SAMPLE"}]}))
        self.assertEqual(entries[0]["team"], "KC")
        self.assertFalse(self.path.exists())

    def test_build_export_doc_and_sync(self):
        db.set_manual_adjustment("DEN", -1.0, "SAMPLE db", db_path=self.db_path)
        ma.set_adjustment("KC", 1.5, reason="SAMPLE file", path=self.path)
        doc = ma.build_export_doc(db_path=self.db_path, file_path=self.path)
        by_team = {a["team"]: a["points"] for a in doc["adjustments"]}
        self.assertEqual(by_team, {"KC": 1.5, "DEN": -1.0})
        self.assertEqual(doc["schema_version"], config.MANUAL_ADJUSTMENTS_SCHEMA_VERSION)
        written = ma.sync_db_from_file(db_path=self.db_path, file_path=self.path)
        self.assertEqual(written, ["KC"])
        self.assertEqual(db.get_manual_adjustments(self.db_path)["KC"], 1.5)

    def test_semantics_unchanged_ratings_still_use_adjustments(self):
        # The file changes storage, not maths: a file adjustment still
        # shifts the rating exactly as a DB adjustment did.
        from nflpicks import model
        import pandas as pd
        row = pd.Series({"week": 5, "home_team": "KC", "away_team": "DEN", "home_rest": 7,
                         "away_rest": 7, "div_game": False, "roof": "outdoors",
                         "surface": "grass", "temp": 60, "wind": 5,
                         "spread_line": 3.0, "total_line": 45.0})
        base = model.predict_game(row, {"KC": 1.0, "DEN": 0.0}, {"KC": 5, "DEN": 5}, 2.0)
        ma.set_adjustment("KC", 2.0, path=self.path)
        adj = ma.load_weekly_adjustments(db_path=self.db_path, file_path=self.path)["adjustments"]
        shifted = model.predict_game(row, {"KC": 1.0, "DEN": 0.0}, {"KC": 5, "DEN": 5}, 2.0,
                                     manual_adj=adj)
        self.assertAlmostEqual(shifted.margin_model - base.margin_model, 2.0, places=6)
