"""
Manual-adjustment grading (batch item 7c): grade games that carried a
manual adjustment separately from pure model picks, so the user can
see over a season whether overrides help or hurt.

What "adjusted" means here, precisely: at pick time, manual_adj_home
or manual_adj_away for that game was non-zero (both are already
stored per game by db.save_picks -- no model change was needed to
record this). It does NOT mean the adjustment flipped the pick: a
small adjustment usually moves the margin without changing the side,
and deciding whether it flipped the pick would need a counterfactual
re-run without adjustments, which this module does not claim. The
comparison is therefore "games played with an adjustment active" vs
"games played without one", not "flipped vs unflipped".

ATS outcome uses the same convention as grading.py: actual_margin is
home-perspective (the games.csv `result` column, home score minus
away score) and the line is the effective judged line -- the pool
line when set, else the live Vegas line. Home covers iff
actual_margin > line, away covers iff actual_margin < line, equal is
a push. Pushes are reported but excluded from ATS% (same as the
harness), so ATS% = wins / (wins + losses).

Storage: grade_manual_adjustments() is pure over (picks rows, results
dict) and is the tested core. save_adjustment_grades() persists the
season summary to db.adjustment_grades (scopes: adjusted, unadjusted,
all) so the dashboard can show it without re-fetching results; the
per-game inputs (picks rows + actual results) remain the source of
truth and the summary is recomputed on demand, never edited by hand.
"""
from __future__ import annotations

from datetime import datetime

from . import db
from .submission import pick_team_from_pick_string


def ats_result_for_pick(pick_team: str | None, home: str, away: str,
                        line_nflverse: float | None, actual_margin: float | None) -> str | None:
    """ATS outcome of one pick: "correct"/"wrong"/"push", or None when
    the pick, the line, or the result is missing."""
    if pick_team not in (home, away) or line_nflverse is None or actual_margin is None:
        return None
    line, actual = float(line_nflverse), float(actual_margin)
    if actual == line:
        return "push"
    home_covers = actual > line
    picked_home = pick_team == home
    return "correct" if home_covers == picked_home else "wrong"


def _empty_bucket() -> dict:
    return {"games": 0, "wins": 0, "losses": 0, "pushes": 0, "ats_pct": None, "record": "0-0"}


def _finish(bucket: dict) -> dict:
    decided = bucket["wins"] + bucket["losses"]
    bucket["ats_pct"] = round(bucket["wins"] / decided * 100, 1) if decided else None
    bucket["record"] = f"{bucket['wins']}-{bucket['losses']}"
    return bucket


def grade_manual_adjustments(season: int, picks_rows, results: dict) -> dict:
    """Grade one season's picks rows against actual results.

    picks_rows: dict-like rows (or dicts) with home_team/away_team,
    pick, pool_spread, vegas_spread, manual_adj_home, manual_adj_away,
    week. results: {(season, week, away_team, home_team):
    actual_margin}. Only games with a pick, a line, and a known result
    are graded; the rest are counted in "ungraded" with no outcome
    invented for them.
    """
    buckets = {"adjusted": _empty_bucket(), "unadjusted": _empty_bucket(), "all": _empty_bucket()}
    per_week: dict[int, dict] = {}
    ungraded = 0
    for p in picks_rows:
        home = p["home_team"] if isinstance(p, dict) else p["home_team"]
        away = p["away_team"] if isinstance(p, dict) else p["away_team"]
        week = int(p["week"])
        pick_str = p["pick"]
        pool_spread = p["pool_spread"]
        vegas_spread = p["vegas_spread"]
        line = pool_spread if pool_spread is not None else vegas_spread
        adj_home = p["manual_adj_home"] or 0.0
        adj_away = p["manual_adj_away"] or 0.0
        adjusted = float(adj_home) != 0.0 or float(adj_away) != 0.0
        pick_team = pick_team_from_pick_string(pick_str, home, away)
        actual = results.get((int(season), week, away, home))
        outcome = ats_result_for_pick(pick_team, home, away, line, actual)
        if outcome is None:
            ungraded += 1
            continue
        scope = "adjusted" if adjusted else "unadjusted"
        wk = per_week.setdefault(week, {"adjusted": _empty_bucket(), "unadjusted": _empty_bucket()})
        for bucket in (buckets[scope], buckets["all"], wk[scope]):
            bucket["games"] += 1
            if outcome == "correct":
                bucket["wins"] += 1
            elif outcome == "wrong":
                bucket["losses"] += 1
            else:
                bucket["pushes"] += 1
    for bucket in buckets.values():
        _finish(bucket)
    for wk in per_week.values():
        for bucket in wk.values():
            _finish(bucket)
    return {
        "season": int(season), "adjusted": buckets["adjusted"],
        "unadjusted": buckets["unadjusted"], "all": buckets["all"],
        "per_week": {str(k): v for k, v in sorted(per_week.items())},
        "ungraded": ungraded,
        "note": ("Adjusted = a manual adjustment was active for that game at pick time; "
                 "it does not mean the adjustment flipped the pick. Pushes excluded from ATS%."),
    }


def grade_season_from_db(season: int, results: dict, db_path: str | None = None) -> dict:
    """Load this season's picks rows from the DB and grade them."""
    with db.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM picks WHERE season=?", (season,)).fetchall()
    return grade_manual_adjustments(season, [dict(r) for r in rows], results)


def save_adjustment_grades(summary: dict, db_path: str | None = None):
    """Persist the season summary (one row per scope) to adjustment_grades."""
    now = datetime.utcnow().isoformat()
    with db.connect(db_path) as conn:
        conn.execute("DELETE FROM adjustment_grades WHERE season=?", (summary["season"],))
        for scope in ("adjusted", "unadjusted", "all"):
            b = summary[scope]
            conn.execute(
                "INSERT INTO adjustment_grades (season, scope, games, wins, losses, pushes, ats_pct, run_time) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (summary["season"], scope, b["games"], b["wins"], b["losses"], b["pushes"], b["ats_pct"], now),
            )


def load_adjustment_grades(season: int, db_path: str | None = None) -> list[dict]:
    with db.connect(db_path) as conn:
        try:
            rows = conn.execute(
                "SELECT * FROM adjustment_grades WHERE season=? ORDER BY scope", (season,)).fetchall()
        except Exception:
            return []
    return [dict(r) for r in rows]
