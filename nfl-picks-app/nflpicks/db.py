"""
SQLite storage. Table-for-tab mapping from the old spreadsheet:

  Picks          -> picks            (rewritten each week)
  Adjustments    -> manual_adjustments (edited live from the dashboard)
  Injury_Review  -> injury_review     (rewritten each week)
  Model_Log      -> model_log         (upserted per week, for grading)
  Model_Grades   -> model_grades      (rewritten by grade_model_log)
  Decision_Log   -> decision_log      (append-only, deduped)
  Survivor       -> survivor_picks    (new -- optimizer output per week)
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS picks (
    season INTEGER, week INTEGER, home_team TEXT, away_team TEXT,
    vegas_spread REAL, model_spread REAL, edge REAL, pick TEXT,
    confidence REAL, strong_edge INTEGER, epa_shadow REAL,
    injury_flags TEXT, manual_adj_home REAL, manual_adj_away REAL,
    vegas_total REAL, model_total REAL, total_pick TEXT,
    run_time TEXT, needs_review INTEGER DEFAULT 0, model_independent REAL, weather_note TEXT,
    pool_spread REAL, pool_total REAL, cover_prob REAL,
    PRIMARY KEY (season, week, home_team, away_team)
);

CREATE TABLE IF NOT EXISTS manual_adjustments (
    team TEXT PRIMARY KEY,
    points REAL,
    reason TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS injury_review (
    season INTEGER, week INTEGER, team TEXT, player TEXT, position TEXT,
    status TEXT, depth_status TEXT, impact REAL, flag_note TEXT
);

CREATE TABLE IF NOT EXISTS model_log (
    run_time TEXT, season INTEGER, week INTEGER, away_team TEXT, home_team TEXT,
    vegas_close REAL, margin_model REAL, epa_model REAL, combo_model REAL,
    live_variant TEXT, actual_margin REAL,
    abs_err_margin REAL, abs_err_epa REAL, abs_err_combo REAL, ats_result TEXT,
    is_backfill INTEGER DEFAULT 0,
    PRIMARY KEY (season, week, away_team, home_team)
);

CREATE TABLE IF NOT EXISTS model_grades (
    variant TEXT PRIMARY KEY, description TEXT, games_graded INTEGER,
    mae REAL, ats_record TEXT, ats_pct TEXT
);

CREATE TABLE IF NOT EXISTS decision_log (
    logged_at TEXT, week INTEGER, team TEXT, points REAL, reason TEXT
);

CREATE TABLE IF NOT EXISTS survivor_picks (
    season INTEGER, week INTEGER, recommended_team TEXT, opponent TEXT,
    is_home INTEGER, win_prob REAL, survival_prob_to_here REAL,
    strategy TEXT,  -- 'optimizer' or 'greedy'
    run_time TEXT,
    PRIMARY KEY (season, week, strategy)
);

CREATE TABLE IF NOT EXISTS survivor_used_teams (
    season INTEGER, team TEXT,
    PRIMARY KEY (season, team)
);

CREATE TABLE IF NOT EXISTS run_diagnostics (
    run_time TEXT, step TEXT, ok INTEGER, detail TEXT
);

CREATE TABLE IF NOT EXISTS team_ratings_snapshot (
    season INTEGER, week INTEGER, team TEXT, rating REAL, games_played_this_season INTEGER,
    raw_point_diff_prior_season REAL, run_time TEXT,
    PRIMARY KEY (season, week, team)
);

CREATE TABLE IF NOT EXISTS survivor_week_options (
    season INTEGER, week INTEGER, team TEXT, opponent TEXT, is_home INTEGER, win_prob REAL,
    run_time TEXT,
    PRIMARY KEY (season, week, team)
);

CREATE TABLE IF NOT EXISTS injury_returns (
    season INTEGER, week INTEGER, team TEXT, player TEXT, position TEXT,
    note TEXT, depth_status TEXT, suggested_pts REAL, flag_note TEXT
);

CREATE TABLE IF NOT EXISTS pool_line_overrides (
    season INTEGER, week INTEGER, home_team TEXT, away_team TEXT,
    pool_spread REAL, pool_total REAL, updated_at TEXT,
    PRIMARY KEY (season, week, home_team, away_team)
);

-- Line movement (backtest item 7, added 2026-11-04): one snapshot row
-- per game per source per run. Sources: 'pool' = the pool's locked line
-- (pool_line_overrides at run time), 'closing' = the recorded/live
-- vegas_spread saved with that week's picks. Comparing the two is how
-- the dashboard flags edge flips and >1 pt moves for Thursday review.
-- The pool line is NEVER auto-switched: it is what the user is judged
-- against, so a move only ever produces a review flag.
-- Market cross-checks (batch 2, item 6, added 2026-11-04): all three
-- market lines for a game (spread, both moneylines, total) plus the
-- computed cross-check flags, one row per game. This is a LOG, not a
-- line source: nothing in the app reads a line back out of this table
-- to drive a pick, so it can never auto-switch the pool line.
CREATE TABLE IF NOT EXISTS market_crosschecks (
    season INTEGER, week INTEGER, home_team TEXT, away_team TEXT,
    spread_line REAL, home_moneyline REAL, away_moneyline REAL, total_line REAL,
    ml_home_win_prob REAL, ml_implied_margin REAL, flags TEXT, run_time TEXT,
    PRIMARY KEY (season, week, home_team, away_team)
);

CREATE TABLE IF NOT EXISTS line_snapshots (
    season INTEGER, week INTEGER, home_team TEXT, away_team TEXT,
    source TEXT, line REAL, run_time TEXT,
    PRIMARY KEY (season, week, home_team, away_team, source)
);

-- Manual-adjustment grading summary (item 7c, added 2026-11-04):
-- one row per scope (adjusted / unadjusted / all) per season, written
-- by adjustment_grading.save_adjustment_grades(). This is a SUMMARY
-- only: the per-game source of truth stays in picks (which already
-- stores manual_adj_home/manual_adj_away per game) plus actual
-- results, and the summary is recomputed from those on demand.
CREATE TABLE IF NOT EXISTS adjustment_grades (
    season INTEGER, scope TEXT, games INTEGER, wins INTEGER,
    losses INTEGER, pushes INTEGER, ats_pct REAL, run_time TEXT,
    PRIMARY KEY (season, scope)
);

-- What-changed diff (batch 3, item 6, added 2026-11-04): one row per
-- reported change from one run, keyed by run_time so a later run
-- never overwrites an earlier run's diff. The picks table itself is
-- overwritten every run, so the diff is the only surviving record
-- of what the previous run's slate looked like. Append-only by
-- construction: save_run_diff inserts under its own run_time.
CREATE TABLE IF NOT EXISTS run_diffs (
    season INTEGER, week INTEGER, run_time TEXT, matchup TEXT,
    home_team TEXT, away_team TEXT, category TEXT, detail TEXT,
    magnitude REAL,
    PRIMARY KEY (season, week, run_time, matchup, category)
);
"""


@contextmanager
def connect(db_path: str | None = None):
    conn = sqlite3.connect(db_path or config.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(db_path: str | None = None):
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)
        # Lightweight migration: add columns introduced after a user's first
        # run without requiring them to delete their existing database.
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(picks)").fetchall()}
        if "needs_review" not in cols:
            conn.execute("ALTER TABLE picks ADD COLUMN needs_review INTEGER DEFAULT 0")
        if "model_independent" not in cols:
            conn.execute("ALTER TABLE picks ADD COLUMN model_independent REAL")
        if "weather_note" not in cols:
            conn.execute("ALTER TABLE picks ADD COLUMN weather_note TEXT")
        if "pool_spread" not in cols:
            conn.execute("ALTER TABLE picks ADD COLUMN pool_spread REAL")
        if "pool_total" not in cols:
            conn.execute("ALTER TABLE picks ADD COLUMN pool_total REAL")
        if "cover_prob" not in cols:
            conn.execute("ALTER TABLE picks ADD COLUMN cover_prob REAL")
        mcols = {r["name"] for r in conn.execute("PRAGMA table_info(model_log)").fetchall()}
        if mcols and "is_backfill" not in mcols:
            conn.execute("ALTER TABLE model_log ADD COLUMN is_backfill INTEGER DEFAULT 0")
        rcols = {r["name"] for r in conn.execute("PRAGMA table_info(team_ratings_snapshot)").fetchall()}
        if rcols and "raw_point_diff_prior_season" not in rcols:
            conn.execute("ALTER TABLE team_ratings_snapshot ADD COLUMN raw_point_diff_prior_season REAL")


def get_manual_adjustments(db_path: str | None = None) -> dict[str, float]:
    with connect(db_path) as conn:
        rows = conn.execute("SELECT team, points FROM manual_adjustments").fetchall()
    return {r["team"]: r["points"] for r in rows}


def set_manual_adjustment(team: str, points: float, reason: str = "", db_path: str | None = None):
    with connect(db_path) as conn:
        conn.execute(
            "INSERT INTO manual_adjustments (team, points, reason, updated_at) VALUES (?,?,?,?) "
            "ON CONFLICT(team) DO UPDATE SET points=excluded.points, reason=excluded.reason, "
            "updated_at=excluded.updated_at",
            (team, points, reason, datetime.utcnow().isoformat()),
        )


def delete_manual_adjustment(team: str, db_path: str | None = None):
    with connect(db_path) as conn:
        conn.execute("DELETE FROM manual_adjustments WHERE team=?", (team,))


def log_decisions(season: int, week: int, adj: dict[str, float], reasons: dict[str, str], db_path: str | None = None):
    if not adj:
        return
    with connect(db_path) as conn:
        seen = {
            (r["week"], r["team"], r["points"], r["reason"] or "")
            for r in conn.execute("SELECT week, team, points, reason FROM decision_log").fetchall()
        }
        now = datetime.utcnow().isoformat()
        for team, pts in adj.items():
            key = (week, team, pts, reasons.get(team, ""))
            if key not in seen:
                conn.execute(
                    "INSERT INTO decision_log (logged_at, week, team, points, reason) VALUES (?,?,?,?,?)",
                    (now, week, team, pts, reasons.get(team, "")),
                )


def save_picks(season: int, predictions, db_path: str | None = None):
    now = datetime.utcnow().isoformat()
    with connect(db_path) as conn:
        for p in predictions:
            conn.execute(
                "INSERT INTO picks (season, week, home_team, away_team, vegas_spread, model_spread, "
                "edge, pick, confidence, strong_edge, epa_shadow, injury_flags, manual_adj_home, "
                "manual_adj_away, vegas_total, model_total, total_pick, run_time, needs_review, "
                "model_independent, weather_note, pool_spread, pool_total, cover_prob) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(season, week, home_team, away_team) DO UPDATE SET "
                "vegas_spread=excluded.vegas_spread, model_spread=excluded.model_spread, "
                "edge=excluded.edge, pick=excluded.pick, confidence=excluded.confidence, "
                "strong_edge=excluded.strong_edge, epa_shadow=excluded.epa_shadow, "
                "injury_flags=excluded.injury_flags, manual_adj_home=excluded.manual_adj_home, "
                "manual_adj_away=excluded.manual_adj_away, vegas_total=excluded.vegas_total, "
                "model_total=excluded.model_total, total_pick=excluded.total_pick, "
                "run_time=excluded.run_time, needs_review=excluded.needs_review, "
                "model_independent=excluded.model_independent, weather_note=excluded.weather_note, "
                "pool_spread=excluded.pool_spread, pool_total=excluded.pool_total, "
                "cover_prob=excluded.cover_prob",
                (season, p.week, p.home, p.away, p.vegas_spread, p.model_spread, p.edge, p.pick,
                 p.confidence, int(p.strong_edge), p.epa_shadow, p.injury_flags, p.manual_adj_home,
                 p.manual_adj_away, p.vegas_total, p.model_total, p.total_pick, now, int(p.needs_review),
                 p.model_independent, p.weather_note, p.pool_spread, p.pool_total,
                 getattr(p, "cover_prob", None)),
            )


def save_injury_review(season: int, week: int, detail: list[tuple], db_path: str | None = None):
    with connect(db_path) as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(injury_review)").fetchall()}
        if cols and "depth_status" not in cols:
            conn.execute("ALTER TABLE injury_review ADD COLUMN depth_status TEXT")
        conn.execute("DELETE FROM injury_review WHERE season=? AND week=?", (season, week))
        for team, player, pos, status, depth_status, impact, flag_note in detail:
            conn.execute(
                "INSERT INTO injury_review (season, week, team, player, position, status, depth_status, "
                "impact, flag_note) VALUES (?,?,?,?,?,?,?,?,?)",
                (season, week, team, player, pos, status, depth_status, impact, flag_note),
            )


def get_recent_injury_history(season: int, current_week: int, lookback_weeks: int = 3, db_path: str | None = None):
    """Returns injury_review rows from the last `lookback_weeks` weeks before current_week
    (as a list of sqlite3.Row), for detecting players who were out and have now returned."""
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM injury_review WHERE season=? AND week < ? AND week >= ? ORDER BY week DESC",
            (season, current_week, max(1, current_week - lookback_weeks)),
        ).fetchall()
    return rows


def save_line_snapshot(season: int, week: int, home_team: str, away_team: str,
                       source: str, line: float | None, db_path: str | None = None):
    """Save (or replace) one line snapshot. `line` is in nflverse
    convention (+ = home favored), matching picks.vegas_spread and
    pool_line_overrides as stored. A None line is a no-op."""
    if line is None:
        return
    with connect(db_path) as conn:
        conn.execute(
            "INSERT INTO line_snapshots (season, week, home_team, away_team, source, line, run_time) "
            "VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(season, week, home_team, away_team, source) DO UPDATE SET "
            "line=excluded.line, run_time=excluded.run_time",
            (season, week, home_team, away_team, source, float(line), datetime.utcnow().isoformat()),
        )


def get_line_moves(season: int, week: int, db_path: str | None = None,
                   move_threshold: float | None = None) -> list[dict]:
    if move_threshold is None:
        move_threshold = float(config.REVIEW_LINE_MOVE_THRESHOLD)
    """Games where the pool line and the closing/live line disagree
    enough to warrant Thursday-morning review: movement >=
    `move_threshold` points, OR the model edge flips sign between the
    two lines (model_independent from picks is used for the edge).
    Returns rows sorted by movement descending. A flagged game is a
    manual check (injury/news the market learned), never an
    auto-switch: the pool line stays the judged line."""
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT p.home_team, p.away_team, p.model_independent, "
            "pool.line AS pool_line, close.line AS closing_line "
            "FROM picks p "
            "LEFT JOIN line_snapshots pool ON pool.season=p.season AND pool.week=p.week "
            "  AND pool.home_team=p.home_team AND pool.away_team=p.away_team AND pool.source='pool' "
            "LEFT JOIN line_snapshots close ON close.season=p.season AND close.week=p.week "
            "  AND close.home_team=p.home_team AND close.away_team=p.away_team AND close.source='closing' "
            "WHERE p.season=? AND p.week=?",
            (season, week)).fetchall()
    out = []
    for r in rows:
        pool_line, closing_line, model = r["pool_line"], r["closing_line"], r["model_independent"]
        if pool_line is None or closing_line is None or model is None:
            continue
        movement = abs(closing_line - pool_line)
        edge_pool = model - pool_line
        edge_close = model - closing_line
        edge_flip = (edge_pool == 0 or edge_close == 0) or (edge_pool > 0) != (edge_close > 0)
        if movement >= move_threshold or edge_flip:
            out.append({
                "home_team": r["home_team"], "away_team": r["away_team"],
                "pool_line": pool_line, "closing_line": closing_line,
                "movement": round(movement, 2), "model_independent": model,
                "edge_pool": round(edge_pool, 2), "edge_close": round(edge_close, 2),
                "edge_flip": bool(edge_flip),
            })
    return sorted(out, key=lambda d: -d["movement"])


def save_market_crosscheck(season: int, week: int, home_team: str, away_team: str,
                           crosscheck_result: dict, db_path: str | None = None):
    """Persist one market cross-check (batch 2, item 6). Flags are
    stored as a semicolon-joined string. Log-only: no pick logic reads
    this table back, so a stored flag can never switch a line."""
    with connect(db_path) as conn:
        conn.execute(
            "INSERT INTO market_crosschecks (season, week, home_team, away_team, spread_line, "
            "home_moneyline, away_moneyline, total_line, ml_home_win_prob, ml_implied_margin, "
            "flags, run_time) VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(season, week, home_team, away_team) DO UPDATE SET "
            "spread_line=excluded.spread_line, home_moneyline=excluded.home_moneyline, "
            "away_moneyline=excluded.away_moneyline, total_line=excluded.total_line, "
            "ml_home_win_prob=excluded.ml_home_win_prob, ml_implied_margin=excluded.ml_implied_margin, "
            "flags=excluded.flags, run_time=excluded.run_time",
            (season, week, home_team, away_team, crosscheck_result.get("spread_line"),
             crosscheck_result.get("home_moneyline"), crosscheck_result.get("away_moneyline"),
             crosscheck_result.get("total_line"), crosscheck_result.get("ml_home_win_prob"),
             crosscheck_result.get("ml_implied_margin"),
             "; ".join(crosscheck_result.get("flags", [])),
             datetime.utcnow().isoformat()),
        )


def get_market_crosschecks(season: int, week: int, db_path: str | None = None) -> list[dict]:
    """Market cross-check rows for a week, flagged games first."""
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM market_crosschecks WHERE season=? AND week=? ORDER BY rowid",
            (season, week)).fetchall()
    out = [dict(r) for r in rows]
    out.sort(key=lambda d: (0 if d.get("flags") else 1))
    return out


def save_injury_returns(season: int, week: int, detail: list[tuple], db_path: str | None = None):
    with connect(db_path) as conn:
        conn.execute("DELETE FROM injury_returns WHERE season=? AND week=?", (season, week))
        for team, player, pos, note, depth_status, pts, flag_note in detail:
            conn.execute(
                "INSERT INTO injury_returns (season, week, team, player, position, note, depth_status, "
                "suggested_pts, flag_note) VALUES (?,?,?,?,?,?,?,?,?)",
                (season, week, team, player, pos, note, depth_status, pts, flag_note),
            )


def get_pool_line_overrides(season: int, week: int, db_path: str | None = None) -> dict:
    """Returns {(home_team, away_team): {"pool_spread": float|None, "pool_total": float|None}}."""
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM pool_line_overrides WHERE season=? AND week=?", (season, week)
        ).fetchall()
    return {(r["home_team"], r["away_team"]): {"pool_spread": r["pool_spread"], "pool_total": r["pool_total"]}
            for r in rows}


def set_pool_line_override(season: int, week: int, home_team: str, away_team: str,
                            pool_spread: float | None, pool_total: float | None, db_path: str | None = None):
    with connect(db_path) as conn:
        conn.execute(
            "INSERT INTO pool_line_overrides (season, week, home_team, away_team, pool_spread, pool_total, "
            "updated_at) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(season, week, home_team, away_team) DO UPDATE SET "
            "pool_spread=excluded.pool_spread, pool_total=excluded.pool_total, updated_at=excluded.updated_at",
            (season, week, home_team, away_team, pool_spread, pool_total, datetime.utcnow().isoformat()),
        )


def clear_pool_line_override(season: int, week: int, home_team: str, away_team: str, db_path: str | None = None):
    with connect(db_path) as conn:
        conn.execute(
            "DELETE FROM pool_line_overrides WHERE season=? AND week=? AND home_team=? AND away_team=?",
            (season, week, home_team, away_team),
        )


def save_survivor_picks(season: int, df, strategy: str, db_path: str | None = None):
    now = datetime.utcnow().isoformat()
    with connect(db_path) as conn:
        conn.execute("DELETE FROM survivor_picks WHERE season=? AND strategy=?", (season, strategy))
        for _, r in df.iterrows():
            conn.execute(
                "INSERT INTO survivor_picks (season, week, recommended_team, opponent, is_home, "
                "win_prob, survival_prob_to_here, strategy, run_time) VALUES (?,?,?,?,?,?,?,?,?)",
                (season, int(r["week"]), r["recommended_team"], r["opponent"], int(r["is_home"]),
                 r["win_prob"], r["survival_prob_to_here"], strategy, now),
            )


def get_survivor_used_teams(season: int, db_path: str | None = None) -> set[str]:
    with connect(db_path) as conn:
        rows = conn.execute("SELECT team FROM survivor_used_teams WHERE season=?", (season,)).fetchall()
    return {r["team"] for r in rows}


def mark_survivor_team_used(season: int, team: str, db_path: str | None = None):
    with connect(db_path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO survivor_used_teams (season, team) VALUES (?,?)", (season, team)
        )


def get_model_log_keys(season: int, db_path: str | None = None) -> set[tuple[int, str, str]]:
    """Returns {(week, away_team, home_team)} already present in model_log for this season."""
    with connect(db_path) as conn:
        rows = conn.execute("SELECT week, away_team, home_team FROM model_log WHERE season=?", (season,)).fetchall()
    return {(r["week"], r["away_team"], r["home_team"]) for r in rows}


def save_model_log(season: int, predictions, db_path: str | None = None, is_backfill: bool = False):
    """
    Logs each game's prediction (all three model variants, plus which one
    was live) so grading.grade_and_summarize() has something to grade
    once real results come in.

    FIXED 2026-11-04: backfill rows (predictions made AFTER the game,
    from today's ratings) are flagged is_backfill=1 and are excluded
    from grading -- counting them inflated grades with look-ahead bias.
    A backfill row also never overwrites a genuine pre-game prediction.
    """
    now = datetime.utcnow().isoformat()
    live_variant = "D-live" if config.EPA_LIVE and any(p.combo_model is not None for p in predictions) else "B-live"
    with connect(db_path) as conn:
        for p in predictions:
            if p.vegas_spread is None:
                continue  # nothing to grade without a market line to compare against
            if is_backfill:
                existing = conn.execute(
                    "SELECT is_backfill FROM model_log WHERE season=? AND week=? AND away_team=? AND home_team=?",
                    (season, p.week, p.away, p.home)).fetchone()
                if existing is not None and not existing["is_backfill"]:
                    continue  # genuine pre-game prediction already there -- never overwrite it
            conn.execute(
                "INSERT INTO model_log (run_time, season, week, away_team, home_team, vegas_close, "
                "margin_model, epa_model, combo_model, live_variant, is_backfill) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(season, week, away_team, home_team) DO UPDATE SET "
                "run_time=excluded.run_time, vegas_close=excluded.vegas_close, "
                "margin_model=excluded.margin_model, epa_model=excluded.epa_model, "
                "combo_model=excluded.combo_model, live_variant=excluded.live_variant, "
                "is_backfill=excluded.is_backfill",
                (now, season, p.week, p.away, p.home, p.vegas_spread,
                 p.margin_model, p.epa_shadow, p.combo_model, live_variant, int(is_backfill)),
            )


def save_survivor_week_options(season: int, win_prob_table, db_path: str | None = None):
    """
    Saves every team's win probability for every remaining week -- not
    just the optimizer's chosen picks -- so the dashboard can show a full,
    browsable list of this week's matchups instead of only one
    auto-selected recommendation.
    """
    now = datetime.utcnow().isoformat()
    with connect(db_path) as conn:
        conn.execute("DELETE FROM survivor_week_options WHERE season=?", (season,))
        for _, r in win_prob_table.iterrows():
            conn.execute(
                "INSERT INTO survivor_week_options (season, week, team, opponent, is_home, win_prob, run_time) "
                "VALUES (?,?,?,?,?,?,?)",
                (season, int(r["week"]), r["team"], r["opponent"], int(r["is_home"]), r["win_prob"], now),
            )


def save_diagnostics(steps: list[dict], db_path: str | None = None):
    now = datetime.utcnow().isoformat()
    with connect(db_path) as conn:
        conn.execute("DELETE FROM run_diagnostics")  # only keep the most recent run
        for s in steps:
            conn.execute(
                "INSERT INTO run_diagnostics (run_time, step, ok, detail) VALUES (?,?,?,?)",
                (now, s["step"], int(s["ok"]), s["detail"]),
            )


def get_diagnostics(db_path: str | None = None):
    with connect(db_path) as conn:
        return conn.execute("SELECT * FROM run_diagnostics ORDER BY rowid").fetchall()


def save_run_diff(season: int, week: int, items: list[dict], run_time: str | None = None,
                  db_path: str | None = None) -> str:
    """Persist one run's what-changed items (run_diff.build_diff
    output). run_time defaults to now (naive UTC ISO, matching
    picks.run_time). Rows are keyed by run_time, so saving a new
    run's diff never touches a previous run's rows. An empty items
    list still records nothing -- callers that want an explicit
    "no changes" record should check get_latest_run_diff's empty
    return instead of inventing rows."""
    run_time = run_time or datetime.utcnow().isoformat()
    with connect(db_path) as conn:
        for it in items:
            conn.execute(
                "INSERT INTO run_diffs (season, week, run_time, matchup, home_team, away_team, "
                "category, detail, magnitude) VALUES (?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(season, week, run_time, matchup, category) DO UPDATE SET "
                "home_team=excluded.home_team, away_team=excluded.away_team, "
                "detail=excluded.detail, magnitude=excluded.magnitude",
                (season, week, run_time, it["matchup"], it.get("home"), it.get("away"),
                 it["category"], it["detail"], it.get("magnitude", 0.0)),
            )
    return run_time


def get_latest_run_diff(season: int, week: int, db_path: str | None = None) -> dict:
    """Latest saved run_diffs rows for one season/week, in
    run_diff category order (flips first). Empty state (no rows)
    returns items=[] and run_time=None -- never an invented diff.
    Sorting needs run_diff.CATEGORY_RANK; to keep db.py free of
    that import cycle risk (run_diff imports config only, so the
    import is actually safe), the rank map is duplicated here in
    miniature via a local import."""
    from . import run_diff as _run_diff
    with connect(db_path) as conn:
        try:
            rows = conn.execute(
                "SELECT MAX(run_time) AS latest FROM run_diffs WHERE season=? AND week=?",
                (season, week)).fetchall()
        except Exception:
            return {"run_time": None, "items": []}
        latest = rows[0]["latest"] if rows else None
        if not latest:
            return {"run_time": None, "items": []}
        data = conn.execute(
            "SELECT * FROM run_diffs WHERE season=? AND week=? AND run_time=?",
            (season, week, latest)).fetchall()
    items = [dict(r) for r in data]
    items.sort(key=lambda i: (_run_diff.CATEGORY_RANK.get(i["category"], 99),
                              -(i["magnitude"] or 0.0), i["matchup"]))
    return {"run_time": latest, "items": items}


def save_ratings_snapshot(season: int, week: int, team_ratings: dict, games_played: dict,
                           raw_point_diff: dict | None = None, db_path: str | None = None):
    raw_point_diff = raw_point_diff or {}
    now = datetime.utcnow().isoformat()
    with connect(db_path) as conn:
        conn.execute("DELETE FROM team_ratings_snapshot WHERE season=? AND week=?", (season, week))
        for team, rating in team_ratings.items():
            conn.execute(
                "INSERT INTO team_ratings_snapshot (season, week, team, rating, games_played_this_season, "
                "raw_point_diff_prior_season, run_time) VALUES (?,?,?,?,?,?,?)",
                (season, week, team, rating, games_played.get(team, 0), raw_point_diff.get(team), now),
            )
