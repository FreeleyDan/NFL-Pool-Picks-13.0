"""Tests for batch items 3 and 7: submission sheet, review queue,
workflow tests-first ordering, and manual-adjustment grading."""
import os
import re
import sqlite3
import tempfile
import unittest

from nflpicks import adjustment_grading, config, db, review_queue, submission


class TestSportsbookNotation(unittest.TestCase):
    def test_home_favorite(self):
        # nflverse +3.5 = home favored. Sportsbook: home -3.5, away +3.5.
        self.assertEqual(submission.format_pick_sportsbook("KC", "DEN", 3.5, "KC"), "KC -3.5")
        self.assertEqual(submission.format_pick_sportsbook("KC", "DEN", 3.5, "DEN"), "DEN +3.5")

    def test_away_favorite(self):
        # nflverse -7 = away favored. Sportsbook: home +7, away -7.
        self.assertEqual(submission.format_pick_sportsbook("KC", "DEN", -7.0, "KC"), "KC +7")
        self.assertEqual(submission.format_pick_sportsbook("KC", "DEN", -7.0, "DEN"), "DEN -7")

    def test_pickem_and_missing(self):
        self.assertEqual(submission.format_pick_sportsbook("KC", "DEN", 0, "KC"), "KC PK")
        self.assertEqual(submission.format_pick_sportsbook("KC", "DEN", 0, "DEN"), "DEN PK")
        self.assertIsNone(submission.format_pick_sportsbook("KC", "DEN", None, "KC"))
        self.assertIsNone(submission.format_pick_sportsbook("KC", "DEN", 3.5, None))
        self.assertIsNone(submission.format_pick_sportsbook("KC", "DEN", 3.5, "BUF"))

    def test_derived_matches_model_pick_strings(self):
        # model.py builds pick strings as: fav side + ("-" if line > 0
        # else "+") + |line|, where line is the pool line for a home
        # pick and its negation for an away pick. The submission
        # derivation must reproduce exactly those strings, or the
        # checklist's sign check would false-alarm on every game.
        cases = [
            ("KC", "DEN", 3.5, "KC", "KC -3.5"),
            ("KC", "DEN", 3.5, "DEN", "DEN +3.5"),
            ("KC", "DEN", -7.0, "KC", "KC +7"),
            ("KC", "DEN", -7.0, "DEN", "DEN -7"),
            ("KC", "DEN", -3.0, "DEN", "DEN -3"),
            ("KC", "DEN", 10.5, "KC", "KC -10.5"),
        ]
        for home, away, line, team, expected_model_pick in cases:
            with self.subTest(line=line, team=team):
                self.assertEqual(
                    submission.format_pick_sportsbook(home, away, line, team), expected_model_pick)

    def test_pick_team_parsing(self):
        self.assertEqual(submission.pick_team_from_pick_string("KC -3.5", "KC", "DEN"), "KC")
        self.assertEqual(submission.pick_team_from_pick_string("DEN PK", "KC", "DEN"), "DEN")
        self.assertIsNone(submission.pick_team_from_pick_string("BUF -3", "KC", "DEN"))
        self.assertIsNone(submission.pick_team_from_pick_string(None, "KC", "DEN"))


class TestSubmissionSheet(unittest.TestCase):
    def _sheet(self, picks):
        return submission.build_submission_sheet(picks, week=10)

    def test_clean_sheet_passes_and_copy_block(self):
        picks = [
            {"home": "KC", "away": "DEN", "pick": "KC -3.5", "pool_spread": 3.5},
            {"home": "BUF", "away": "MIA", "pick": "MIA -7", "vegas_spread": -7.0},
        ]
        sheet = self._sheet(picks)
        self.assertTrue(sheet["all_checks_pass"])
        self.assertIn("DEN @ KC: KC -3.5", sheet["text"])
        self.assertIn("MIA @ BUF: MIA -7", sheet["text"])
        self.assertIn("Thursday 11:00 AM PT", sheet["text"])
        # Alphabetical matchup order, not input order.
        self.assertLess(sheet["text"].index("DEN @ KC"), sheet["text"].index("MIA @ BUF"))
        by_matchup = {e["matchup"]: e for e in sheet["entries"]}
        self.assertEqual(by_matchup["DEN @ KC"]["line_source"], "pool")
        self.assertEqual(by_matchup["MIA @ BUF"]["line_source"], "vegas")

    def test_sign_mismatch_fails_checklist(self):
        picks = [{"home": "KC", "away": "DEN", "pick": "KC +3.5", "pool_spread": 3.5}]
        sheet = self._sheet(picks)
        self.assertFalse(sheet["all_checks_pass"])
        entry = sheet["entries"][0]
        self.assertEqual(entry["sign_check"], "MISMATCH")
        self.assertEqual(entry["pick_text"], "KC -3.5")
        sign_check = [c for c in sheet["checklist"] if "sign check" in c["check"]][0]
        self.assertEqual(sign_check["status"], "fail")

    def test_missing_line_fails_and_duplicate_fails(self):
        sheet = self._sheet([{"home": "KC", "away": "DEN", "pick": None}])
        self.assertFalse(sheet["all_checks_pass"])
        self.assertEqual(sheet["entries"][0]["sign_check"], "NO_LINE")
        dup = self._sheet([
            {"home": "KC", "away": "DEN", "pick": "KC -3.5", "pool_spread": 3.5},
            {"home": "KC", "away": "DEN", "pick": "KC -3.5", "pool_spread": 3.5},
        ])
        dup_check = [c for c in dup["checklist"] if "twice" in c["check"]][0]
        self.assertEqual(dup_check["status"], "fail")

    def test_manual_checklist_item_never_blocks_as_pass(self):
        sheet = self._sheet([{"home": "KC", "away": "DEN", "pick": "KC -3.5", "pool_spread": 3.5}])
        manual = [c for c in sheet["checklist"] if "review queue" in c["check"]][0]
        self.assertEqual(manual["status"], "manual")


class TestReviewQueue(unittest.TestCase):
    def _game(self, **kw):
        base = {"home": "KC", "away": "DEN", "pick": "KC -3.5",
                "model_independent": 4.0, "line": 3.5}
        base.update(kw)
        return base

    def test_unflagged_games_excluded(self):
        out = review_queue.build_review_queue_from_games(
            [self._game(model_independent=3.6, line=3.5)])  # dev 0.1, nothing else
        self.assertEqual(out["n_flagged"], 0)
        self.assertEqual(out["items"], [])

    def test_threshold_boundaries(self):
        # Exactly EDGE_THRESHOLD -> DISAGREEMENT; exactly SANITY -> MODEL_REVIEW.
        out = review_queue.build_review_queue_from_games([
            self._game(model_independent=6.5, line=3.5),   # dev 3.0
            self._game(home="BUF", away="MIA", model_independent=11.5, line=3.5),  # dev 8.0
        ])
        by_matchup = {i["matchup"]: i for i in out["items"]}
        self.assertTrue(any("DISAGREEMENT" in r for r in by_matchup["DEN @ KC"]["reasons"]))
        self.assertTrue(any("MODEL_REVIEW" in r for r in by_matchup["MIA @ BUF"]["reasons"]))
        # MODEL_REVIEW (P1) sorts before DISAGREEMENT (P2).
        self.assertEqual(out["items"][0]["priority"], 1)
        self.assertEqual(out["items"][0]["matchup"], "MIA @ BUF")

    def test_line_move_edge_flip_weather_injury_crosscheck(self):
        games = [
            self._game(line_move={"movement": 1.5, "edge_flip": False,
                                  "pool_line": 3.5, "closing_line": 2.0}),
            self._game(home="BUF", away="MIA",
                       line_move={"movement": 0.5, "edge_flip": True,
                                  "pool_line": 3.5, "closing_line": 3.0}),
            self._game(home="GB", away="CHI", weather_note="wind 22mph (high)"),
            self._game(home="DAL", away="PHI",
                       injury_notes=["DAL Dak (QB, OUT, REVIEW -- critical position)"]),
            self._game(home="SEA", away="SF", crosscheck_flags=["spread/ML gap 3.5 pts"]),
        ]
        out = review_queue.build_review_queue_from_games(games)
        self.assertEqual(out["n_flagged"], 5)
        reasons = {i["matchup"]: " ".join(i["reasons"]) for i in out["items"]}
        self.assertIn("LINE_MOVE", reasons["DEN @ KC"])
        self.assertIn("EDGE_FLIP", reasons["MIA @ BUF"])
        self.assertIn("WEATHER", reasons["CHI @ GB"])
        self.assertIn("INJURY", reasons["PHI @ DAL"])
        self.assertIn("CROSSCHECK", reasons["SF @ SEA"])
        # P1 = edge flip + critical injury; P2 = the rest.
        prio = {i["matchup"]: i["priority"] for i in out["items"]}
        self.assertEqual(prio["MIA @ BUF"], 1)
        self.assertEqual(prio["PHI @ DAL"], 1)
        self.assertEqual(prio["DEN @ KC"], 2)

    def test_priority_then_biggest_deviation_order(self):
        games = [
            self._game(home="A", away="B", model_independent=7.5, line=3.5,  # dev 4.0, P2
                       crosscheck_flags=["x"]),
            self._game(home="C", away="D", model_independent=9.5, line=3.5,  # dev 6.0, P2
                       crosscheck_flags=["x"]),
        ]
        out = review_queue.build_review_queue_from_games(games)
        self.assertEqual([i["matchup"] for i in out["items"]], ["D @ C", "B @ A"])

    def test_db_wrapper_assembles_sources(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        try:
            db.init_db(tmp.name)
            with db.connect(tmp.name) as conn:
                conn.execute(
                    "INSERT INTO picks (season, week, home_team, away_team, vegas_spread, model_spread, "
                    "edge, pick, confidence, strong_edge, model_independent, pool_spread, weather_note) "
                    "VALUES (2026, 10, 'KC', 'DEN', 2.0, 4.0, 1.0, 'KC -3.5', 1.0, 0, 4.0, 3.5, '')")
                conn.execute(
                    "INSERT INTO picks (season, week, home_team, away_team, vegas_spread, model_spread, "
                    "edge, pick, confidence, strong_edge, model_independent, pool_spread, weather_note) "
                    "VALUES (2026, 10, 'BUF', 'MIA', 3.0, 3.0, 0.0, 'BUF -3', 1.0, 0, 3.1, 3.0, '')")
                conn.execute(
                    "INSERT INTO injury_review (season, week, team, player, position, status, depth_status, "
                    "impact, flag_note) VALUES (2026, 10, 'KC', 'Mahomes', 'QB', 'OUT', 'Starter', -5.0, "
                    "'REVIEW -- critical position')")
            db.save_line_snapshot(2026, 10, "KC", "DEN", "pool", 3.5, db_path=tmp.name)
            db.save_line_snapshot(2026, 10, "KC", "DEN", "closing", 2.0, db_path=tmp.name)
            out = review_queue.build_review_queue(2026, 10, db_path=tmp.name)
            self.assertEqual(out["n_total"], 2)
            self.assertEqual(out["n_flagged"], 1)
            item = out["items"][0]
            self.assertEqual(item["matchup"], "DEN @ KC")
            joined = " ".join(item["reasons"])
            self.assertIn("LINE_MOVE", joined)
            self.assertIn("INJURY", joined)
        finally:
            os.unlink(tmp.name)


class TestAdjustmentGrading(unittest.TestCase):
    def _row(self, home, away, pick, pool, vegas, adj_h, adj_a, week=10):
        return {"season": 2026, "week": week, "home_team": home, "away_team": away,
                "pick": pick, "pool_spread": pool, "vegas_spread": vegas,
                "manual_adj_home": adj_h, "manual_adj_away": adj_a}

    def test_adjusted_vs_unadjusted_split_and_pool_line(self):
        rows = [
            # Adjusted, home covers the POOL line (+3.5) though not Vegas (+7): correct via pool.
            self._row("KC", "DEN", "KC -3.5", 3.5, 7.0, 1.0, 0.0),
            # Adjusted, away pick wrong: actual +6 > line +3.5, home covers.
            self._row("BUF", "MIA", "MIA +3.5", 3.5, 3.5, 0.0, -1.0),
            # Unadjusted, push: actual equals line.
            self._row("GB", "CHI", "GB -3", 3.0, 3.0, 0.0, 0.0),
            # Unadjusted, no result yet: ungraded, not invented.
            self._row("DAL", "PHI", "DAL -2.5", 2.5, 2.5, 0.0, 0.0),
        ]
        results = {(2026, 10, "DEN", "KC"): 5.0, (2026, 10, "MIA", "BUF"): 6.0,
                   (2026, 10, "CHI", "GB"): 3.0}
        out = adjustment_grading.grade_manual_adjustments(2026, rows, results)
        self.assertEqual(out["adjusted"], {"games": 2, "wins": 1, "losses": 1, "pushes": 0,
                                            "ats_pct": 50.0, "record": "1-1"})
        self.assertEqual(out["unadjusted"]["games"], 1)
        self.assertEqual(out["unadjusted"]["pushes"], 1)
        self.assertIsNone(out["unadjusted"]["ats_pct"])  # pushes excluded, no decided games
        self.assertEqual(out["ungraded"], 1)

    def test_ats_result_edges(self):
        self.assertEqual(adjustment_grading.ats_result_for_pick("KC", "KC", "DEN", 3.5, 3.5), "push")
        self.assertEqual(adjustment_grading.ats_result_for_pick("DEN", "KC", "DEN", 3.5, 3.0), "correct")
        self.assertEqual(adjustment_grading.ats_result_for_pick("DEN", "KC", "DEN", 3.5, 4.0), "wrong")
        self.assertIsNone(adjustment_grading.ats_result_for_pick("BUF", "KC", "DEN", 3.5, 4.0))
        self.assertIsNone(adjustment_grading.ats_result_for_pick("KC", "KC", "DEN", None, 4.0))

    def test_save_and_load_roundtrip(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        try:
            db.init_db(tmp.name)
            summary = {"season": 2026,
                       "adjusted": {"games": 2, "wins": 1, "losses": 1, "pushes": 0, "ats_pct": 50.0, "record": "1-1"},
                       "unadjusted": {"games": 1, "wins": 0, "losses": 0, "pushes": 1, "ats_pct": None, "record": "0-0"},
                       "all": {"games": 3, "wins": 1, "losses": 1, "pushes": 1, "ats_pct": 50.0, "record": "1-1"}}
            adjustment_grading.save_adjustment_grades(summary, db_path=tmp.name)
            loaded = {r["scope"]: r for r in adjustment_grading.load_adjustment_grades(2026, db_path=tmp.name)}
            self.assertEqual(loaded["adjusted"]["wins"], 1)
            self.assertEqual(loaded["unadjusted"]["pushes"], 1)
        finally:
            os.unlink(tmp.name)


class TestWorkflowTestsFirst(unittest.TestCase):
    def _workflow_path(self):
        here = os.path.dirname(os.path.abspath(__file__))
        candidate = os.path.normpath(os.path.join(here, "..", "..", ".github", "workflows", "weekly.yml"))
        if os.path.exists(candidate):
            return candidate
        # Walk up looking for the repo root (robust to a moved tests dir).
        cur = here
        for _ in range(5):
            candidate = os.path.join(cur, ".github", "workflows", "weekly.yml")
            if os.path.exists(candidate):
                return candidate
            cur = os.path.dirname(cur)
        self.fail("weekly.yml not found above tests dir")

    def test_tests_run_before_weekly_run_cron_and_permissions_kept(self):
        text = open(self._workflow_path()).read()
        self.assertIn('cron: "0 15 * * 2,4"', text)
        self.assertIn("contents: write", text)
        step_names = re.findall(r"-\s+name:\s+(.+)", text)
        self.assertFalse([n for n in step_names if re.search(r"email|smtp", n, re.I)],
                         f"workflow gained an email step: {step_names}")
        tests_idx = next(i for i, n in enumerate(step_names) if "unit test" in n.lower())
        weekly_idx = next(i for i, n in enumerate(step_names) if "weekly picks" in n.lower())
        self.assertLess(tests_idx, weekly_idx, "unit tests must run BEFORE the weekly picks step")
        tests_block = text[text.index("Run unit tests"):]
        self.assertNotIn("|| true", tests_block.split("Run weekly picks")[0],
                         "tests step must fail the job, not swallow failures")


if __name__ == "__main__":
    unittest.main()
