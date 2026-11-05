"""Tests for batch-4 group D, items 8-10 only (committed-DB
divergence, standings importer, experiment tracking).

Run from nfl-picks-app/: python -m unittest discover -s tests -v
No network, no Streamlit needed (dashboard.py is checked by
source inspection only, like prior batches' no-live-click
tests). No live behaviour changes: no flag is flipped, no
pick is changed, and every standings/DB fixture below is
synthetic and labelled SAMPLE where it resembles real data.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nflpicks import committed_db, config, db, experiment, pool_standings
from nflpicks.model import GamePrediction


def _pred(week, home, away, pick, edge=1.0):
    return GamePrediction(week=week, home=home, away=away, vegas_spread=3.0,
                          model_spread=4.0, model_independent=4.0, edge=edge,
                          pick=pick, confidence=1.0, strong_edge=False)


def _make_db(path, picks):
    db.init_db(path)
    if picks:
        db.save_picks(2026, picks, db_path=path)
    return path


def _env(**kw):
    """Context manager replacing env vars (restores afterwards)."""
    import contextlib

    @contextlib.contextmanager
    def _cm():
        old = {k: os.environ.get(k) for k in kw}
        try:
            for k, v in kw.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            yield
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
    return _cm()


class TestCommittedDb(unittest.TestCase):
    def test_describe_local_when_nothing_configured(self):
        with _env(PICKS_COMMITTED_DB_URL=None, PICKS_COMMITTED_DB_FILE=None,
                  PICKS_GITHUB_REPO=None):
            src = committed_db.describe_source()
            self.assertEqual(src["mode"], "local")
            self.assertNotIn("token", str(src).lower())

    def test_repo_url_and_bad_repo_rejected_without_network(self):
        with _env(PICKS_COMMITTED_DB_URL=None, PICKS_COMMITTED_DB_FILE=None,
                  PICKS_GITHUB_REPO="owner/repo", PICKS_GITHUB_BRANCH="main"):
            src = committed_db.describe_source()
            self.assertEqual(src["mode"], "repo")
            url = committed_db.committed_db_url(src)
            self.assertEqual(url, "https://raw.githubusercontent.com/owner/repo/main/nfl-picks-app/nfl_picks.db")
        with _env(PICKS_COMMITTED_DB_URL=None, PICKS_COMMITTED_DB_FILE=None,
                  PICKS_GITHUB_REPO="not-a-repo"):
            with self.assertRaises(ValueError):
                committed_db.committed_db_url(committed_db.describe_source())

    def test_compare_file_mode_match_and_diverge(self):
        with tempfile.TemporaryDirectory() as td:
            local = os.path.join(td, "local.db")
            committed = os.path.join(td, "committed.db")
            picks = [_pred(10, "KC", "DEN", "KC -3.0")]
            _make_db(local, picks)
            _make_db(committed, picks)
            with _env(PICKS_COMMITTED_DB_URL=None, PICKS_GITHUB_REPO=None,
                      PICKS_COMMITTED_DB_FILE=committed):
                cmp_ = committed_db.compare_to_local(local)
                self.assertEqual(cmp_["status"], "ok")
                self.assertFalse(cmp_["diverged"])
                # Committed slate changes one pick -> diverged.
                _make_db(committed, [_pred(10, "KC", "DEN", "DEN +3.0")])
                cmp2 = committed_db.compare_to_local(local)
                self.assertEqual(cmp2["status"], "diverged")
                self.assertTrue(cmp2["diverged"])
                if cmp2.get("snapshot_path"):
                    os.unlink(cmp2["snapshot_path"])
            if cmp_.get("snapshot_path"):
                os.unlink(cmp_["snapshot_path"])

    def test_compare_unreachable_falls_back_without_raising(self):
        with tempfile.TemporaryDirectory() as td:
            local = os.path.join(td, "local.db")
            _make_db(local, [_pred(10, "KC", "DEN", "KC -3.0")])
            with _env(PICKS_COMMITTED_DB_URL=None, PICKS_GITHUB_REPO=None,
                      PICKS_COMMITTED_DB_FILE=os.path.join(td, "missing.db")):
                cmp_ = committed_db.compare_to_local(local)
                self.assertEqual(cmp_["status"], "unreachable")
                self.assertIsNotNone(cmp_["local"])
                self.assertIsNone(cmp_["snapshot_path"])

    def test_sync_replaces_workflow_tables_preserves_user_tables(self):
        with tempfile.TemporaryDirectory() as td:
            local = os.path.join(td, "local.db")
            committed = os.path.join(td, "committed.db")
            _make_db(local, [_pred(10, "KC", "DEN", "KC -3.0")])
            _make_db(committed, [_pred(10, "KC", "DEN", "DEN +3.0"),
                                 _pred(10, "BUF", "MIA", "BUF -3.0")])
            # User-owned data in the local DB only.
            db.set_pool_line_override(2026, 10, "KC", "DEN", 2.5, None, db_path=local)
            db.set_manual_adjustment("KC", 1.5, "test", db_path=local)
            pool_standings.save_pool_standings(
                [{"season": 2026, "week": 10, "user_wins": 100, "leader_wins": 102,
                  "weeks_left": 4, "leader_name": "", "note": ""}], db_path=local)
            snap = committed_db._snapshot_path(open(committed, "rb").read())
            try:
                out = committed_db.sync_committed_to_local(local, snapshot_path=snap)
            finally:
                os.unlink(snap)
            self.assertEqual(out["status"], "ok")
            self.assertIn("picks", out["replaced"])
            self.assertIn("manual_adjustments", out["preserved"])
            self.assertIn("pool_line_overrides", out["preserved"])
            self.assertIn("pool_standings", out["preserved"])
            # Workflow slate now equals committed; user tables intact.
            with db.connect(local) as conn:
                rows = conn.execute(
                    "SELECT pick FROM picks WHERE season=2026 AND week=10 ORDER BY home_team").fetchall()
            self.assertEqual(sorted(r["pick"] for r in rows), ["BUF -3.0", "DEN +3.0"])
            self.assertEqual(db.get_pool_line_overrides(2026, 10, db_path=local)[("KC", "DEN")]["pool_spread"], 2.5)
            self.assertEqual(db.get_manual_adjustments(db_path=local)["KC"], 1.5)
            self.assertEqual(pool_standings.load_pool_standings(2026, db_path=local)[0]["user_wins"], 100)


class TestPoolStandings(unittest.TestCase):
    def test_parse_valid_and_rejects(self):
        text = ("season,week,user_wins,leader_wins,weeks_left,leader_name,note\n"
                "2026,10,118,120,4,,after week 9\n"
                "2026,10,119,120,4,,corrected\n"
                "2026,11,-1,120,3,,negative wins\n"
                "2026,11,119,abc,3,,non-integer\n"
                "2026,12,119,120,,,\n")
        out = pool_standings.parse_pool_standings_csv(text)
        # Last row wins for week 10; three bad rows rejected.
        self.assertEqual(len(out["rows"]), 1)
        self.assertEqual(out["rows"][0]["user_wins"], 119)
        self.assertEqual(len(out["errors"]), 4)  # 1 duplicate note + 3 rejections

    def test_empty_and_missing_columns(self):
        self.assertEqual(pool_standings.parse_pool_standings_csv("")["rows"], [])
        bad = pool_standings.parse_pool_standings_csv("season,week\n2026,10\n")
        self.assertEqual(bad["rows"], [])
        self.assertTrue(bad["errors"])

    def test_save_load_latest_and_empty_state(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "t.db")
            db.init_db(path)
            self.assertEqual(pool_standings.latest_standings(2026, db_path=path)["status"], "empty")
            pool_standings.import_pool_standings_csv(
                "season,week,user_wins,leader_wins,weeks_left,leader_name,note\n"
                "2026,10,118,120,4,,\n2026,11,124,125,3,,\n", db_path=path)
            latest = pool_standings.latest_standings(2026, db_path=path)
            self.assertEqual(latest["status"], "ok")
            self.assertEqual((latest["week"], latest["deficit"]), (11, 1))
            at10 = pool_standings.latest_standings(2026, week=10, db_path=path)
            self.assertEqual(at10["week"], 10)

    def test_price_week_gambles_uses_actual_snapshot_and_empty(self):
        games = [{"home": "KC", "away": "DEN", "default_side": "home", "pick_prob": 0.52},
                 {"home": "BUF", "away": "MIA", "default_side": "home", "pick_prob": 0.80}]
        snap = {"status": "ok", "season": 2026, "week": 10, "user_wins": 118,
                "leader_wins": 120, "weeks_left": 3}
        priced = pool_standings.price_week_gambles(games, snap, trials=200)
        self.assertEqual(priced["status"], "ok")
        self.assertEqual(priced["standings"]["mode"], "trailing")
        self.assertEqual(len(priced["standings"]["gambles"]), 1)
        self.assertIn("ONE-WEEK PRICING ONLY", priced["simulation"]["note"])
        self.assertIn("label", priced["simulation"])
        empty = pool_standings.price_week_gambles(games, {"status": "empty"})
        self.assertEqual(empty["status"], "empty")
        self.assertIsNone(empty["simulation"])

    def test_price_week_gambles_early_is_no_gamble(self):
        games = [{"home": "KC", "away": "DEN", "default_side": "home", "pick_prob": 0.52}]
        snap = {"status": "ok", "season": 2026, "week": 10, "user_wins": 118,
                "leader_wins": 120, "weeks_left": 10}
        priced = pool_standings.price_week_gambles(games, snap, trials=200)
        self.assertEqual(priced["standings"]["mode"], "early")
        self.assertEqual(priced["standings"]["gambles"], [])


class TestExperiment(unittest.TestCase):
    def test_flags_stay_off_by_default(self):
        for name in experiment.FLAG_NAMES:
            self.assertIs(getattr(config, name), False, name)

    def test_record_latest_and_fingerprint_changes_on_flag_flip(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "t.db")
            db.init_db(path)
            self.assertIsNone(experiment.latest_experiment(2026, db_path=path))
            row1 = experiment.record_run(2026, 10, picks_count=16, db_path=path)
            self.assertEqual(len(row1["fingerprint"]), 16)
            self.assertEqual(row1["flags"]["EPA_LIVE"], False)
            orig = config.EPA_LIVE
            try:
                config.EPA_LIVE = True
                row2 = experiment.record_run(2026, 11, picks_count=16, db_path=path,
                                             run_time="2099-01-01T00:00:00")
            finally:
                config.EPA_LIVE = orig
            self.assertNotEqual(row1["fingerprint"], row2["fingerprint"])
            latest = experiment.latest_experiment(2026, db_path=path)
            self.assertEqual(latest["fingerprint"], row2["fingerprint"])
            self.assertEqual(latest["flags"]["EPA_LIVE"], True)
            timeline = experiment.experiments_for_season(2026, db_path=path)
            self.assertEqual(len(timeline), 2)
            # Attribution: flags_on lists the flipped flag; unknown without a row.
            attr = experiment.attribution_for_week(2026, 11, db_path=path)
            self.assertEqual(attr["status"], "ok")
            self.assertEqual(attr["flags_on"], ["EPA_LIVE"])
            unknown = experiment.attribution_for_week(2025, 11, db_path=path)
            self.assertEqual(unknown["status"], "unknown")
            self.assertIsNone(unknown["experiment"])

    def test_capture_is_deterministic(self):
        a = experiment.capture_model_settings()
        b = experiment.capture_model_settings()
        self.assertEqual(a["fingerprint"], b["fingerprint"])
        self.assertEqual(set(a["flags"]), set(experiment.FLAG_NAMES))


class TestWiring(unittest.TestCase):
    def test_weekly_run_records_experiment(self):
        src = Path(__file__).resolve().parent.parent.joinpath("weekly_run.py").read_text()
        self.assertIn("experiment.record_run", src)

    def test_dashboard_wires_all_three_items(self):
        src = Path(__file__).resolve().parent.parent.joinpath("dashboard.py").read_text()
        for token in ("committed_db.compare_to_local", "sync_committed_to_local",
                      "pool_standings.latest_standings", "price_week_gambles",
                      "experiment.latest_experiment"):
            self.assertIn(token, src)

    def test_no_flag_flipped_in_new_modules(self):
        for mod in ("committed_db.py", "pool_standings.py", "experiment.py"):
            src = Path(__file__).resolve().parent.parent.joinpath("nflpicks", mod).read_text()
            for name in experiment.FLAG_NAMES:
                self.assertNotIn(f"{name} = True", src, f"{mod} flips {name}")


if __name__ == "__main__":
    unittest.main()
