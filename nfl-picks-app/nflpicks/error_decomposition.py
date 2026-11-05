"""
Error decomposition (batch 3, item 3): where does the current model
actually lose?

This module slices graded games -- point-in-time backtest rows or
graded live picks -- by spread bucket, favorite/dog, home/away,
early/late season, picked team, total bucket, weather (wind), and
division, and reports ATS% and MAE per slice. Every slice carries its
sample size, and slices below ERROR_MIN_DECIDED decided games are
flagged small_sample and excluded from the worst-slice ranking, so a
2-0 or 0-3 slice can never be presented as a finding.

What this module is NOT:
* It is NOT a pick input. Nothing in model.py, weekly_run.py, or
  dashboard.py changes a pick from a slice result. A losing slice is
  a target for a future walk-forward fix, not a reason to flip picks
  in that slice -- the slice itself was found on the same data it
  would be "fixed" on, so acting on it without an out-of-sample test
  is exactly the cherry-picking this module is built to prevent.
* It does NOT invent context. Total, division, and wind come from the
  source games frame via attach_context(); a game whose source row
  lacks a total, a div_game flag, or wind lands in that slice's
  "unknown" bucket, and the report states how many games are unknown
  per dimension. Unknown is a coverage fact, not a zero.
* It does NOT re-grade with a different push rule. A margin exactly
  equal to the line is a push (batch-2 convention), excluded from
  ATS% but counted, matching backtest._ats_rows and staleness.py.
  Canonical evaluate_variant rows already carry "ats"; this module
  re-derives it from pick/line/actual when normalizing, so a caller
  passing raw picks gets the same rule.

Row contract (normalized dicts, see normalize_backtest_rows /
normalize_pick_rows):
  required: season, week, home_team, away_team, line (nflverse),
    actual_margin, pick_team (or pick_side "home"/"away").
  optional: model_margin (enables MAE / signed bias; without it a
    slice reports ATS only and mae is None), total_line, div_game,
    wind (mph, games.csv `wind`; missing -> unknown, never imputed).
"""
from __future__ import annotations

import math

from . import config

# Slice dimensions, in the order the report presents them.
DIMENSIONS = ("spread", "favdog", "side", "season_phase", "team",
              "total", "weather", "division")


def _is_div(value) -> bool | None:
    """div_game truthiness, or None when the value is missing/unknown.

    nflverse div_game arrives as int 1/0, but a CSV round-trip can
    turn it into "1", 1.0, or True (see model._is_div_game). None /
    empty / NaN stays unknown -- it is not assumed non-divisional."""
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    if isinstance(value, str) and value.strip() == "":
        return None
    if isinstance(value, bool):
        return value
    try:
        return float(value) == 1.0
    except (TypeError, ValueError):
        return str(value).strip() == "1"


def _bucket(value: float | None, edges: tuple[float, ...], labels: tuple[str, ...]) -> str:
    if value is None:
        return "unknown"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "unknown"
    if isinstance(v, float) and math.isnan(v):
        return "unknown"
    for edge, label in zip(edges, labels):
        if v < edge:
            return label
    return labels[-1]


def spread_bucket(line: float | None) -> str:
    """Bucket on |line| (nflverse points)."""
    if line is None:
        return "unknown"
    try:
        a = abs(float(line))
    except (TypeError, ValueError):
        return "unknown"
    edges = tuple(float(e) for e in getattr(config, "ERROR_SPREAD_EDGES", (0.5, 3.5, 7.5)))
    if a < edges[0]:
        return "pick'em"
    if a < edges[1]:
        return "short (<=3)"
    if a < edges[2]:
        return "medium (3.5-7)"
    return "large (7.5+)"


def total_bucket(total_line: float | None) -> str:
    edges = tuple(float(e) for e in getattr(config, "ERROR_TOTAL_EDGES", (40.0, 45.0, 50.0)))
    return _bucket(total_line, edges, ("low (<40)", "mid-low (40-45)", "mid-high (45-50)", "high (50+)"))


def weather_bucket(wind: float | None) -> str:
    """Wind buckets from the same thresholds the totals model uses
    (config WIND_MODERATE_MPH / WIND_HIGH_MPH). Missing wind is
    unknown, never a calm-game assumption: a dome game and a game
    with no recorded wind are different facts, and only the recorded
    value is bucketed here."""
    if wind is None:
        return "unknown"
    try:
        w = float(wind)
    except (TypeError, ValueError):
        return "unknown"
    if math.isnan(w):
        return "unknown"
    if w >= float(config.WIND_HIGH_MPH):
        return "high wind (20+ mph)"
    if w >= float(config.WIND_MODERATE_MPH):
        return "moderate wind (15-20 mph)"
    return "calm (<15 mph)"


def season_phase(week: int | None) -> str:
    if week is None:
        return "unknown"
    try:
        w = int(week)
    except (TypeError, ValueError):
        return "unknown"
    if w <= 4:
        return "early (weeks 1-4)"
    if w <= 9:
        return "mid (weeks 5-9)"
    return "late (weeks 10+)"


def _pick_side(row: dict) -> str | None:
    team = row.get("pick_team")
    if team is not None and row.get("home_team") is not None:
        if team == row["home_team"]:
            return "home"
        if team == row["away_team"]:
            return "away"
        return None
    side = row.get("pick_side")
    return side if side in ("home", "away") else None


def _favdog(row: dict) -> str:
    """Favorite/dog from the PICK's side vs the line: the pick is a
    favorite pick when it takes the team the line favours (home pick
    with line > 0, away pick with line < 0); a zero line is neither,
    so it lands in pick'em (which matches spread_bucket "pick'em")."""
    line, side = row.get("line"), _pick_side(row)
    if line is None or side is None:
        return "unknown"
    try:
        ln = float(line)
    except (TypeError, ValueError):
        return "unknown"
    if ln == 0:
        return "pick'em"
    fav_is_home = ln > 0
    return "favorite" if (side == "home") == fav_is_home else "underdog"


def normalize_row(row: dict) -> dict | None:
    """Normalize one raw row to the decomposition contract, or None
    when it cannot be graded (no pick, no line, or no result).

    Accepts both backtest-style rows (pick_side, spread_line,
    model_margin) and picks-table-style rows (pick string team,
    pool_spread/vegas_spread, model_independent/model_spread). The
    caller should prefer normalize_backtest_rows /
    normalize_pick_rows, which set pick_team explicitly; this is the
    shared core they both funnel through."""
    home = row.get("home_team") or row.get("home")
    away = row.get("away_team") or row.get("away")
    line = row.get("line")
    if line is None:
        line = row.get("pool_spread") if row.get("pool_spread") is not None else row.get("vegas_spread")
    if line is None:
        line = row.get("spread_line")
    actual = row.get("actual_margin")
    if actual is None:
        actual = row.get("result")
    pick_team = row.get("pick_team")
    if pick_team is None and row.get("pick_side") in ("home", "away"):
        pick_team = home if row["pick_side"] == "home" else away
    if home is None or away is None or line is None or actual is None or pick_team is None:
        return None
    if pick_team not in (home, away):
        return None
    try:
        line_f, actual_f = float(line), float(actual)
    except (TypeError, ValueError):
        return None
    model_margin = row.get("model_margin")
    if model_margin is None:
        model_margin = row.get("model_independent")
    if model_margin is None:
        model_margin = row.get("model_spread")
    try:
        model_f = float(model_margin) if model_margin is not None else None
    except (TypeError, ValueError):
        model_f = None
    if actual_f == line_f:
        ats = "push"
    else:
        home_covers = actual_f > line_f
        ats = "correct" if (pick_team == home) == home_covers else "wrong"
    div = _is_div(row.get("div_game"))
    return {
        "season": row.get("season"), "week": row.get("week"),
        "home_team": home, "away_team": away, "pick_team": pick_team,
        "line": line_f, "actual_margin": actual_f, "model_margin": model_f,
        "ats": ats,
        "abs_err": abs(actual_f - model_f) if model_f is not None else None,
        "line_abs_err": abs(actual_f - line_f),
        "signed_err": (actual_f - model_f) if model_f is not None else None,
        "total_line": row.get("total_line"),
        "div_game": div,
        "wind": row.get("wind"),
    }


def normalize_backtest_rows(frame) -> list[dict]:
    """Backtest _games frame (evaluate_variant output) -> normalized
    rows. The frame already carries pick_side, spread_line,
    model_margin, and ats under the canonical grading; normalization
    re-derives ats with the same push rule as a consistency check
    (a mismatch would surface as a test failure, not a silent rule
    change -- see tests)."""
    rows = []
    records = frame.to_dict("records") if hasattr(frame, "to_dict") else list(frame)
    for r in records:
        norm = normalize_row(r)
        if norm is not None:
            rows.append(norm)
    return rows


def normalize_pick_rows(picks_rows, results: dict) -> list[dict]:
    """DB picks rows + results dict -> normalized rows.

    picks_rows: dict-like rows with home_team/away_team, pick (model
    pick string, parsed by submission.pick_team_from_pick_string),
    pool_spread/vegas_spread, model_independent/model_spread, week,
    season, and optionally total_line/div_game/wind. results:
    {(season, week, away_team, home_team): actual_margin} (same key
    shape as adjustment_grading). Games with no result are skipped
    and counted by the caller via len() difference, never invented."""
    from .submission import pick_team_from_pick_string

    rows = []
    for p in picks_rows:
        d = p if isinstance(p, dict) else dict(p)
        home = d["home_team"]
        away = d["away_team"]
        week = int(d["week"])
        season = int(d["season"]) if d.get("season") is not None else None
        pick_team = pick_team_from_pick_string(d["pick"], home, away)
        actual = results.get((season, week, away, home)) if season is not None else None
        row = dict(d)
        row.update({"home_team": home, "away_team": away, "pick_team": pick_team,
                    "actual_margin": actual})
        norm = normalize_row(row)
        if norm is not None:
            norm["season"] = season
            norm["week"] = week
            rows.append(norm)
    return rows


def attach_context(rows: list[dict], games_frame) -> list[dict]:
    """Fill total_line / div_game / wind on normalized rows from a
    games frame (nflverse games.csv, normalized by
    backtest.load_games_frame), joined on season/week/home/away.
    Columns absent from the frame stay absent (unknown buckets);
    rows with no matching source game keep whatever context they
    already had. Point-in-time note: this only ATTACHES context
    columns for slicing already-graded rows -- it never feeds a
    result back into a rating, so it cannot leak into a pick."""
    if games_frame is None or len(rows) == 0:
        return rows
    try:
        records = games_frame.to_dict("records")
    except Exception:
        return rows
    lookup = {}
    for g in records:
        try:
            key = (int(g["season"]), int(g["week"]), g["home_team"], g["away_team"])
        except (KeyError, TypeError, ValueError):
            continue
        lookup[key] = g
    for r in rows:
        try:
            key = (int(r["season"]), int(r["week"]), r["home_team"], r["away_team"])
        except (KeyError, TypeError, ValueError):
            continue
        src = lookup.get(key)
        if src is None:
            continue
        for col in ("total_line", "div_game", "wind"):
            if col in src and r.get(col) is None:
                val = src[col]
                # pandas NaN is not None -- treat it as missing.
                if isinstance(val, float) and math.isnan(val):
                    continue
                r[col] = val
        # Re-derive the div flag from the attached raw value.
        if "div_game" in src:
            r["div_game"] = _is_div(src["div_game"])
    return rows


def _slice_key(row: dict, dimension: str) -> str:
    if dimension == "spread":
        return spread_bucket(row["line"])
    if dimension == "favdog":
        return _favdog(row)
    if dimension == "side":
        return _pick_side(row) or "unknown"
    if dimension == "season_phase":
        return season_phase(row.get("week"))
    if dimension == "team":
        return row["pick_team"]
    if dimension == "total":
        return total_bucket(row.get("total_line"))
    if dimension == "weather":
        return weather_bucket(row.get("wind"))
    if dimension == "division":
        div = row.get("div_game")
        return "divisional" if div is True else ("non-divisional" if div is False else "unknown")
    raise ValueError(f"unknown dimension: {dimension}")


def _summarize(rows: list[dict]) -> dict:
    decided = [r for r in rows if r["ats"] != "push"]
    wins = sum(1 for r in decided if r["ats"] == "correct")
    losses = sum(1 for r in decided if r["ats"] == "wrong")
    errs = [r["abs_err"] for r in rows if r.get("abs_err") is not None]
    line_errs = [r["line_abs_err"] for r in rows if r.get("line_abs_err") is not None]
    signed = [r["signed_err"] for r in rows if r.get("signed_err") is not None]
    min_decided = int(getattr(config, "ERROR_MIN_DECIDED", 30))
    return {
        "n": len(rows), "decided": len(decided),
        "wins": wins, "losses": losses,
        "pushes": len(rows) - len(decided),
        "ats_pct": round(100 * wins / len(decided), 2) if decided else None,
        "mae": round(sum(errs) / len(errs), 2) if errs else None,
        "line_mae": round(sum(line_errs) / len(line_errs), 2) if line_errs else None,
        "mae_gap": (round(sum(errs) / len(errs) - sum(line_errs) / len(line_errs), 2)
                    if errs and line_errs else None),
        "mean_signed_err": round(sum(signed) / len(signed), 2) if signed else None,
        "small_sample": len(decided) < min_decided,
    }


def decompose(rows: list[dict]) -> dict:
    """Full decomposition of normalized rows.

    Returns overall, one table per dimension (each slice with n /
    decided / ATS% / MAE / line MAE / MAE gap / signed bias /
    small_sample), a coverage block (how many rows lack total / div /
    wind context), and worst_slices: the lowest-ATS and largest-MAE-gap
    slices among slices with decided >= ERROR_MIN_DECIDED, presented
    as ranked targets for future investigation -- NOT as claims, and
    never including a small slice. An empty input returns an explicit
    empty state, not zero-filled tables."""
    rows = [r for r in rows if r is not None]
    if not rows:
        return {"status": "empty", "n": 0, "overall": None, "slices": {},
                "coverage": {}, "worst_slices": [],
                "note": "No graded games to decompose -- run the backtest first."}
    slices: dict[str, dict[str, dict]] = {}
    for dim in DIMENSIONS:
        buckets: dict[str, list[dict]] = {}
        for r in rows:
            buckets.setdefault(_slice_key(r, dim), []).append(r)
        slices[dim] = {k: _summarize(v) for k, v in sorted(buckets.items())}
    coverage = {
        "total_known": sum(1 for r in rows if r.get("total_line") is not None),
        "total_unknown": sum(1 for r in rows if r.get("total_line") is None),
        "div_known": sum(1 for r in rows if r.get("div_game") is not None),
        "div_unknown": sum(1 for r in rows if r.get("div_game") is None),
        "wind_known": sum(1 for r in rows if r.get("wind") is not None),
        "wind_unknown": sum(1 for r in rows if r.get("wind") is None),
    }
    min_decided = int(getattr(config, "ERROR_MIN_DECIDED", 30))
    qualified = [(dim, name, s) for dim, table in slices.items()
                 for name, s in table.items()
                 if not s["small_sample"] and s["ats_pct"] is not None]
    by_ats = sorted(qualified, key=lambda t: t[2]["ats_pct"])[:5]
    by_gap = sorted([t for t in qualified if t[2]["mae_gap"] is not None],
                    key=lambda t: -t[2]["mae_gap"])[:5]
    worst = [{"dimension": d, "slice": n, "ats_pct": s["ats_pct"],
              "decided": s["decided"], "mae_gap": s["mae_gap"], "rank_by": "lowest ATS"}
             for d, n, s in by_ats]
    worst += [{"dimension": d, "slice": n, "ats_pct": s["ats_pct"],
               "decided": s["decided"], "mae_gap": s["mae_gap"], "rank_by": "largest MAE gap"}
              for d, n, s in by_gap]
    return {
        "status": "ok", "n": len(rows),
        "overall": _summarize(rows),
        "slices": slices, "coverage": coverage,
        "worst_slices": worst,
        "min_decided": min_decided,
        "note": (f"Slices with fewer than {min_decided} decided games are flagged "
                 "small_sample and excluded from worst_slices. A losing slice is a "
                 "target for a future walk-forward fix, not a reason to flip picks."),
    }
