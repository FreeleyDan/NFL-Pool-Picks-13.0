"""Tests for batch 4, items 1-2 (tie-grading correction, placebo gate,
error-decomposition read). No network, no games.csv needed: the
grading-rule tests are synthetic, and the full 2019-2025 numbers live
in the README Batch 4 section (reproduced from /tmp/games.csv runs).

Run from nfl-picks-app/: python -m unittest discover -s tests -v
No live behaviour changes: no flag is flipped, no pick is changed.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from nflpicks import backtest, config, error_decomposition as ed


def _games_frame(rows):
    """Minimal evaluate_variant-shaped frame for placebo/summarize."""
    return pd.DataFrame([{
        "season": r[0], "week": r[1], "home_team": r[2], "away_team": r[3],
        "model_margin": r[4], "spread_line": r[5], "actual_margin": r[6],
        "edge": round(r[4] - r[5], 3),
        "edge_bucket": "0-1",
        "pick_side": "home" if r[4] > r[5] else "away",
        "pick_favdog": "fav",
        "week_bucket": "5-9",
        "ats": r[7], "abs_err": abs(r[6] - r[4]),
        "line_abs_err": abs(r[6] - r[5]),
    } for r in rows])


class TestTieGradingCorrection(unittest.TestCase):
    def test_evaluate_variant_grades_margin_equals_line_as_push(self):
        # Week-1 games with no prior season: ratings fall back to
        # empty/zero, so the model margin is just the no-data HFA
        # margin (2.0 in this harness). A 2.0 line is therefore an
        # exact tie; with a decided result the old rule graded it
        # wrong, the corrected rule grades push. The margin==line
        # assertion is deliberate: if the no-data margin ever
        # changes, this test must fail loudly, not pass vacuously.
        games = pd.DataFrame([
            {"season": 2024, "week": 1, "game_type": "REG", "home_team": "KC",
             "away_team": "DEN", "home_score": 24, "away_score": 20,
             "result": 4.0, "spread_line": 2.0},
            {"season": 2024, "week": 1, "game_type": "REG", "home_team": "BUF",
             "away_team": "MIA", "home_score": 20, "away_score": 27,
             "result": -7.0, "spread_line": 2.0},
        ])
        frame = backtest.load_games_frame(games)
        summ = backtest.evaluate_variant(frame, [2024], "baseline")
        rows = summ["_games"].to_dict("records")
        self.assertEqual(len(rows), 2)
        for r in rows:
            self.assertEqual(r["model_margin"], r["spread_line"])
            self.assertEqual(r["ats"], "push")
        # And the decomposition path must agree with the harness
        # (batch-3 test asserted this for actual==line pushes; batch
        # 4 extends it to margin==line pushes).
        norm = ed.normalize_backtest_rows(summ["_games"])
        self.assertEqual(len(norm), 2)
        for r, g in zip(norm, rows):
            self.assertEqual(r["ats"], g["ats"])

    def test_normalize_row_margin_tie_and_pick_margin_tie_are_push(self):
        base = {"season": 2024, "week": 5, "home_team": "KC", "away_team": "DEN",
                "pick_team": "KC", "line": 3.0, "actual_margin": 7.0}
        # Raw margin tie: no lean, push even though the home side
        # would have covered.
        row = ed.normalize_row({**base, "model_margin": 3.0})
        self.assertEqual(row["ats"], "push")
        # Shrunk-pick tie (calibrated_pick): raw margin leans home
        # but the graded pick margin does not -- push, not a win.
        row2 = ed.normalize_row({**base, "model_margin": 5.0, "pick_margin": 3.0})
        self.assertEqual(row2["ats"], "push")
        # Non-tie control still grades normally.
        row3 = ed.normalize_row({**base, "model_margin": 5.0})
        self.assertEqual(row3["ats"], "correct")

    def test_summarize_excludes_pushes_from_ats_but_counts_them(self):
        frame = _games_frame([
            (2024, 5, "KC", "DEN", 3.0, 3.0, 7.0, "push"),
            (2024, 5, "BUF", "MIA", 5.0, 3.0, 7.0, "correct"),
            (2024, 5, "DAL", "PHI", 1.0, 3.0, -3.0, "wrong"),
        ])
        out = backtest.summarize(frame.to_dict("records"), "baseline")
        self.assertEqual(out["n"], 3)
        self.assertEqual(out["pushes"], 1)
        self.assertEqual(out["decided"], 2)
        self.assertEqual(out["correct"], 1)
        self.assertEqual(out["ats_pct"], 50.0)


class TestPlaceboGate(unittest.TestCase):
    def test_placebo_cannot_flip_large_edge_games(self):
        # All |edge| >= 1.0: a +/-0.15 adjustment can flip nothing,
        # so placebo ATS must equal baseline ATS exactly, whatever
        # the seed. This is the property the old rule violated on
        # tie games (edge exactly 0, counted wrong, flipped free).
        frame = _games_frame([
            (2024, 5, "KC", "DEN", 5.0, 3.0, 7.0, "correct"),
            (2024, 5, "BUF", "MIA", 5.0, 3.0, -3.0, "wrong"),
            (2024, 5, "DAL", "PHI", 1.0, 3.0, 7.0, "wrong"),
            (2024, 5, "MIA", "NE", -5.0, -3.0, -7.0, "correct"),
        ])
        base = backtest.summarize(frame.to_dict("records"), "baseline")
        for seed in (1, 20261104, 999):
            plac = backtest.summarize(
                backtest.placebo_rows(frame, seed).to_dict("records"), "placebo")
            self.assertEqual(plac["ats_pct"], base["ats_pct"])
            self.assertEqual(plac["decided"], base["decided"])
            pc = backtest.paired_comparison(frame, backtest.placebo_rows(frame, seed))
            self.assertEqual(pc["paired_delta_pp"], 0.0)

    def test_placebo_turns_tie_pushes_into_picks_not_free_wins(self):
        # A tie game is a push in the baseline; the placebo moves it
        # off the line, so it becomes decided -- but under corrected
        # grading it is also EXCLUDED from the paired comparison
        # (baseline push), so it cannot manufacture a paired z.
        frame = _games_frame([
            (2024, 5, "KC", "DEN", 3.0, 3.0, 7.0, "push"),
            (2024, 5, "BUF", "MIA", 5.0, 3.0, 7.0, "correct"),
        ])
        plac_df = backtest.placebo_rows(frame, 20261104)
        plac = backtest.summarize(plac_df.to_dict("records"), "placebo")
        self.assertEqual(plac["decided"], 2)  # tie game now decided
        pc = backtest.paired_comparison(frame, plac_df)
        self.assertEqual(pc["n"], 1)  # only the mutually-decided game
        self.assertEqual(pc["paired_delta_pp"], 0.0)


class TestFlagsStayOffBatch4(unittest.TestCase):
    def test_flags_stay_off(self):
        for flag in ("EPA_LIVE", "ENSEMBLE_LIVE", "CALIBRATED_PICK", "TRAVEL_ENABLED",
                     "INJ_AUTO_APPLY", "TOTALS_SRS_LIVE", "STALENESS_LIVE",
                     "UNCERTAINTY_PICK_USE", "PBP_FEATURES_LIVE"):
            self.assertFalse(getattr(config, flag), flag)
