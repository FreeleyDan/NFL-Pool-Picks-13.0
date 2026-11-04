"""Tests for split_games (line-less games) and backfill grading exclusion."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from nflpicks import data, db, grading, model


def games_df():
    return pd.DataFrame([
        {"season": 2026, "week": 9, "game_type": "REG", "home_team": "KC",
         "away_team": "DEN", "home_score": None, "away_score": None,
         "result": None, "spread_line": 3.0, "total_line": 45.0,
         "home_rest": 7, "away_rest": 7, "div_game": 0, "location": "Home"},
        {"season": 2026, "week": 9, "game_type": "REG", "home_team": "BUF",
         "away_team": "MIA", "home_score": None, "away_score": None,
         "result": None, "spread_line": None, "total_line": None,
         "home_rest": 7, "away_rest": 7, "div_game": 0, "location": "Home"},
        {"season": 2026, "week": 8, "game_type": "REG", "home_team": "KC",
         "away_team": "DEN", "home_score": 24, "away_score": 17,
         "result": 7.0, "spread_line": 3.0, "total_line": 45.0,
         "home_rest": 7, "away_rest": 7, "div_game": 0, "location": "Home"},
    ])


class TestSplitGames(unittest.TestCase):
    def test_line_less_game_stays_in_slate(self):
        played, prior, upcoming = data.split_games(games_df(), 2026)
        self.assertEqual(len(upcoming), 2)  # was 1 before the fix
        self.assertIn("BUF", set(upcoming["home_team"]))


class TestBackfillGrading(unittest.TestCase):
    def test_backfill_row_never_overwrites_genuine_prediction(self):
        with tempfile.TemporaryDirectory() as td:
            path = str(Path(td) / "test.db")
            db.init_db(path)
            row = pd.Series({"week": 8, "home_team": "KC", "away_team": "DEN",
                             "spread_line": 3.0, "total_line": 45.0,
                             "home_rest": 7, "away_rest": 7, "div_game": "0",
                             "location": "Home", "gameday": "2026-11-01",
                             "roof": "outdoors"})
            genuine = model.predict_game(row, {"KC": 5.0, "DEN": -5.0},
                                         {"KC": 8, "DEN": 8}, 2.0)
            db.save_model_log(2026, [genuine], db_path=path)
            backfill = model.predict_game(row, {"KC": 20.0, "DEN": -20.0},
                                          {"KC": 8, "DEN": 8}, 2.0)
            db.save_model_log(2026, [backfill], db_path=path, is_backfill=True)
            with db.connect(path) as conn:
                saved = conn.execute(
                    "SELECT margin_model, is_backfill FROM model_log").fetchone()
            self.assertEqual(saved["margin_model"], genuine.margin_model)
            self.assertEqual(saved["is_backfill"], 0)


if __name__ == "__main__":
    unittest.main()
