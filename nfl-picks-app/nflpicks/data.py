"""
Data layer. Replaces the manual Utilities.parseCsv() calls from the Apps
Script version with pandas, which handles type coercion, missing columns,
and malformed rows far more gracefully.
"""
import logging

import pandas as pd
import requests

from . import config

log = logging.getLogger(__name__)

# Simple per-run diagnostics collector. Every fetch function records what it
# tried and what happened, successful or not, so the dashboard's Data Health
# tab can show real evidence instead of a generic "no data" message.
DIAGNOSTICS: list[dict] = []


def _record(step: str, ok: bool, detail: str):
    DIAGNOSTICS.append({"step": step, "ok": ok, "detail": detail})


def reset_diagnostics():
    DIAGNOSTICS.clear()


def _fetch_csv(url: str) -> pd.DataFrame:
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    from io import StringIO
    return pd.read_csv(StringIO(resp.text), low_memory=False)


def load_games(season: int) -> pd.DataFrame:
    """
    Returns every game for `season` and `season - 1` (regular season only
    for the prior year, matching the original script's prior-season logic).
    Columns are normalized to: season, week, game_type, home_team, away_team,
    home_score, away_score, result, spread_line, total_line, home_rest,
    away_rest, div_game.
    """
    try:
        df = _fetch_csv(config.GAMES_URL)
    except Exception as e:
        _record("Load schedule/results", False, f"Fetch failed: {e}")
        raise
    _record("Load schedule/results", True, f"Fetched {len(df)} total rows from {config.GAMES_URL}")

    # Defensive dtype fix: a 25+ year historical CSV can end up with 'week'
    # read as text (pandas infers one dtype per column; any stray
    # non-numeric value anywhere in the column forces the whole thing to
    # text). A text 'week' column sorts lexicographically -- week "10"
    # right after week "1" -- which would scramble the recency weighting
    # in ratings.py across the entire season for every team. Force both to
    # numeric explicitly rather than trust the inferred dtype.
    season_dtype_before = str(df["season"].dtype)
    week_dtype_before = str(df["week"].dtype)
    df["season"] = pd.to_numeric(df["season"], errors="coerce")
    df["week"] = pd.to_numeric(df["week"], errors="coerce")
    _record(
        "Coerce season/week to numeric", True,
        f"season column was {season_dtype_before} -> now {df['season'].dtype}; "
        f"week column was {week_dtype_before} -> now {df['week'].dtype}"
        + (" -- WAS TEXT, this was likely corrupting recency weighting" if week_dtype_before == "object" else ""),
    )

    df["home_team"] = df["home_team"].map(config.normalize_team)
    df["away_team"] = df["away_team"].map(config.normalize_team)

    keep = df[(df["season"] == season) | (df["season"] == season - 1)].copy()

    # Regular season only, for BOTH the current and prior season. This was
    # previously only applied to the prior season -- meaning preseason
    # games (game_type="PRE", played with backups in August, before the
    # real season starts) could get counted as "current season" results
    # and contaminate the ratings with noise. Excludes postseason too for
    # now (playoff-specific handling is a future improvement, see README).
    before = len(keep)
    keep = keep[keep["game_type"] == "REG"]
    n_excluded = before - len(keep)

    n_cur = int((keep["season"] == season).sum())
    n_prior = int((keep["season"] == season - 1).sum())
    n_cur_played = int(((keep["season"] == season) & keep["result"].notna()).sum())
    _record(
        f"Filter to season {season}", True,
        f"{n_cur} REG rows for {season} ({n_cur_played} with a result so far), {n_prior} REG rows for "
        f"{season - 1}. Excluded {n_excluded} non-REG rows (preseason/postseason).",
    )

    # IMPORTANT: recency weighting in ratings.py assumes rows are in true
    # chronological order. Don't trust the source file's row order -- sort
    # explicitly. gameday/gametime give exact ordering when present; week
    # is the fallback.
    sort_cols = [c for c in ("season", "week", "gameday", "gametime") if c in keep.columns]
    keep = keep.sort_values(sort_cols, kind="stable")

    # Defensive dedup: if the source ever has two rows for the same game
    # (e.g. a stale row left behind by a flex-schedule update), a team
    # would appear to play twice in one week downstream -- in the ATS
    # picks, and especially in the survivor optimizer where it would look
    # like a scheduling error. Keep the last (most recently listed) row
    # per matchup and record whether any were found.
    before_dedup = len(keep)
    keep = keep.drop_duplicates(subset=["season", "week", "home_team", "away_team"], keep="last")
    n_dupes = before_dedup - len(keep)
    if n_dupes:
        _record("Dedup schedule rows", False,
                f"Found and removed {n_dupes} duplicate game row(s) -- a team appearing to play twice "
                f"in the same week was likely caused by this.")
    else:
        _record("Dedup schedule rows", True, "No duplicate game rows found.")

    return keep.reset_index(drop=True)


def split_games(games: pd.DataFrame, season: int):
    """Split into (played_current, played_prior, upcoming_next_week).

    FIXED 2026-11-04: upcoming used to require spread_line.notna(), so a
    game whose line had not posted yet vanished from the slate entirely.
    This pool requires a pick on EVERY game, so an unlined game must
    still appear -- model.py then gives it a model-only read with
    pick=None / edge=None (and a pool override, if set, still drives a
    real pick), instead of silently dropping the game.
    """
    played_mask = games["result"].notna()
    cur = games["season"] == season
    prior = games["season"] == season - 1

    played = games[played_mask & cur].copy()
    played_prior = games[played_mask & prior].copy()

    upcoming = games[(~played_mask) & cur].copy()
    if len(upcoming):
        next_week = int(upcoming["week"].min())
        upcoming = upcoming[upcoming["week"] == next_week].copy()
    return played, played_prior, upcoming


def load_all_upcoming(games: pd.DataFrame, season: int) -> pd.DataFrame:
    """All not-yet-played current-season games (used by the survivor optimizer,
    which needs to see the whole remaining schedule, not just next week)."""
    played_mask = games["result"].notna()
    cur = games["season"] == season
    return games[(~played_mask) & cur].copy()


def load_weekly_roster(season: int, week: int) -> pd.DataFrame | None:
    """
    Roster status (ACT/RES/PUP/etc.) per player per week. Used to catch
    players on IR or another long-term-out list who won't appear in the
    day-to-day practice report at all -- that report only covers players
    who could conceivably play, so IR is invisible to it by design.

    The exact filename for this release isn't confirmed from docs, so
    this tries a couple of likely candidates in order.
    """
    df = None
    errors = []
    tried_url = None
    for tmpl in config.ROSTER_URL_TMPLS:
        url = tmpl.format(season=season)
        try:
            df = _fetch_csv(url)
            tried_url = url
            break
        except Exception as e:
            errors.append(f"{url} -- {e}")
    if df is None:
        log.warning("roster fetch failed for all candidate URLs: %s", errors)
        _record("Fetch weekly roster (for IR/long-term-out detection)", False,
                 "All candidate URLs failed: " + " | ".join(errors))
        return None
    url = tried_url

    needed = {"week", "team", "status", "gsis_id"}
    if not needed.issubset(df.columns):
        _record("Fetch weekly roster (for IR/long-term-out detection)", False,
                 f"URL: {url} -- fetched {len(df)} rows but missing columns from {needed}. "
                 f"Columns found: {list(df.columns)}")
        return None

    total_rows = len(df)
    df = df[df["week"] <= week].copy()
    if df.empty:
        _record("Fetch weekly roster (for IR/long-term-out detection)", True,
                 f"URL: {url} -- {total_rows} total rows, none at or before week {week} yet.")
        return None
    df["team"] = df["team"].map(config.normalize_team)
    latest_week = df["week"].max()
    df = df[df["week"] == latest_week]
    _record("Fetch weekly roster (for IR/long-term-out detection)", True,
             f"URL: {url} -- {total_rows} total rows this season, using week {int(latest_week)}'s "
             f"roster ({len(df)} players) as the most recent available for week {week}.")
    return df


def load_depth_charts(season: int, week: int) -> pd.DataFrame | None:
    """
    NOTE: nflverse changed the depth chart data source after the 2024
    season (NFL Data Exchange -> ESPN). From 2025 onwards there is no
    'week' column -- each record has a 'dt' (ISO8601 timestamp) instead,
    and depth rank is 'pos_rank' rather than 'depth_team'. This reads the
    current (2025+) schema and takes the single most recent snapshot
    available (by 'dt') as "this week's" depth chart.
    """
    url = config.DEPTH_CHARTS_URL_TMPL.format(season=season)
    try:
        df = _fetch_csv(url)
    except Exception as e:
        log.warning("depth chart fetch failed: %s", e)
        _record("Fetch depth charts", False, f"URL: {url} -- Error: {e}")
        return None

    needed = {"team", "pos_rank"}
    if not needed.issubset(df.columns):
        _record("Fetch depth charts", False,
                 f"URL: {url} -- fetched {len(df)} rows but missing columns from {needed}. "
                 f"Columns found: {list(df.columns)}")
        return None

    total_rows = len(df)
    df = df.copy()
    df["team"] = df["team"].map(config.normalize_team)

    if "dt" in df.columns:
        latest_dt = df["dt"].max()
        df = df[df["dt"] == latest_dt]
        _record("Fetch depth charts", True,
                 f"URL: {url} -- {total_rows} total rows this season, using the most recent snapshot "
                 f"({latest_dt}, {len(df)} rows) -- 2025+ depth charts don't have a week number, just a "
                 f"timestamp, so this is always \"current\" rather than tied to a specific week.")
    else:
        _record("Fetch depth charts", True, f"URL: {url} -- {total_rows} rows, no 'dt' column to filter by.")
    return df


def load_injuries(season: int, week: int) -> pd.DataFrame | None:
    url = config.INJURIES_URL_TMPL.format(season=season)
    try:
        df = _fetch_csv(url)
    except Exception as e:
        log.warning("injury fetch failed: %s", e)
        _record(
            "Fetch injury report", False,
            f"URL: {url} -- Error: {e}. If the {season} season hasn't started yet, this file may "
            f"simply not exist yet (nflverse publishes it once real injury reports start being filed "
            f"in-season) -- that's expected, not a bug.",
        )
        return None
    for col in ("week", "team", "position", "report_status"):
        if col not in df.columns:
            log.warning("injuries file missing column %s", col)
            _record("Fetch injury report", False,
                     f"URL: {url} -- fetched {len(df)} rows but column '{col}' is missing. "
                     f"Columns found: {list(df.columns)}")
            return None
    total_rows = len(df)
    week_dtype_before = str(df["week"].dtype)
    df["week"] = pd.to_numeric(df["week"], errors="coerce")  # same defensive coercion used for games.csv
    unique_weeks = sorted(df["week"].dropna().unique().tolist())
    max_week_in_file = max(unique_weeks) if unique_weeks else None
    rows_per_week = df["week"].value_counts().sort_index().to_dict()

    filtered = df[df["week"] == week].copy()
    # Dedup: if the source lists the same player more than once in a
    # week, injuries.review_injuries would otherwise sum that player's
    # impact twice. Keep the last row per player.
    if "full_name" in filtered.columns:
        before_dedup = len(filtered)
        filtered = filtered.drop_duplicates(subset=["team", "full_name"], keep="last")
        if len(filtered) != before_dedup:
            _record("Dedup injury rows", True,
                    f"Removed {before_dedup - len(filtered)} duplicate player row(s) for week {week}.")
    filtered["team"] = filtered["team"].map(config.normalize_team)

    detail = (
        f"URL: {url} -- {total_rows} total rows this season, {len(filtered)} rows for week {week} "
        f"(week column was {week_dtype_before} dtype). "
        f"Weeks actually present in the file: {unique_weeks} "
        f"(row counts: {rows_per_week}). "
    )
    if week not in unique_weeks and unique_weeks:
        detail += (
            f"*** This app is querying week {week}, but that week has NO rows in the file at all -- "
            f"the most recent week WITH data is {max_week_in_file}. If that's not the week you expect "
            f"to be current, this is likely why coverage looks incomplete. ***"
        )
    _record("Fetch injury report", True, detail)
    return filtered


def load_team_stats(season: int, week: int | None = None) -> pd.DataFrame | None:
    """Load team EPA stats. If `week` is given, only rows for weeks
    strictly before that week are returned (point-in-time, backtest
    item 3): the published file is full-season, so using it unfiltered
    in any retrospective grading leaks future weeks and playoff rows.
    The live weekly run should pass the target week here; mid-season
    the file then holds only completed weeks anyway, so the filter is
    harmless there and essential in backtests."""
    url = config.TEAM_STATS_URL_TMPL.format(season=season)
    try:
        df = _fetch_csv(url)
    except Exception as e:
        log.warning("team stats fetch failed: %s", e)
        _record("Fetch team EPA stats", False, f"URL: {url} -- Error: {e}")
        return None
    team_col = "team" if "team" in df.columns else ("recent_team" if "recent_team" in df.columns else None)
    if team_col is None or "passing_epa" not in df.columns or "rushing_epa" not in df.columns:
        log.warning("team stats file missing expected columns")
        _record("Fetch team EPA stats", False,
                 f"URL: {url} -- fetched {len(df)} rows but expected columns missing. "
                 f"Columns found: {list(df.columns)[:15]}...")
        return None
    df = df.rename(columns={team_col: "team"})
    df["team"] = df["team"].map(config.normalize_team)
    if "opponent_team" in df.columns:
        df["opponent_team"] = df["opponent_team"].map(config.normalize_team)
    if "season" in df.columns:
        df = df[df["season"] == season]
    if week is not None and "week" in df.columns:
        df = df[pd.to_numeric(df["week"], errors="coerce") < week]
    # Exclude postseason rows where the file carries a game/season type
    # marker; regular-season EPA only, matching the ratings input.
    for col in ("season_type", "game_type"):
        if col in df.columns:
            df = df[df[col].astype(str).str.upper().isin({"REG", "REGULAR"}) | df[col].isna()] \
                if df[col].notna().any() else df
            break
    _record("Fetch team EPA stats", True, f"URL: {url} -- {len(df)} rows for season {season}"
            + (f", weeks before {week}" if week is not None else ""))
    return df
