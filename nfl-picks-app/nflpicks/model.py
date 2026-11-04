"""
Turns team ratings into game-level predictions: spread pick, total pick,
edge, and a confidence score. This is the direct successor to the
runWeeklyPicks() scoring loop in the Apps Script version.
"""
from __future__ import annotations

from dataclasses import dataclass
import math

import pandas as pd

from . import config, ratings as ratings_mod


@dataclass
class GamePrediction:
    week: int
    home: str
    away: str
    vegas_spread: float | None
    model_spread: float          # the OFFICIAL number used for the pick (market-blended)
    model_independent: float     # the model's raw, unblended read -- for transparency/diagnostics only
    edge: float | None
    pick: str | None
    confidence: float          # 0-1, from rating sample size on both sides
    strong_edge: bool
    margin_model: float = 0.0    # variant B: pure SOS-adjusted margin model, pre-blend, pre-EPA
    combo_model: float | None = None  # variant D: margin + EPA combined (shadow)
    needs_review: bool = False  # model/Vegas gap is implausibly large -- verify before trusting
    epa_shadow: float | None = None
    injury_flags: str = ""
    manual_adj_home: float = 0.0
    manual_adj_away: float = 0.0
    vegas_total: float | None = None
    model_total: float | None = None
    weather_note: str = ""
    pool_spread: float | None = None   # your pool's locked spread, NFLVERSE convention (see config)
    pool_total: float | None = None    # your pool's locked total, if you set one (drives the total pick)
    total_pick: str | None = None
    cover_prob: float | None = None    # P(pick covers), normal approx. with SURVIVOR_SPREAD_STDEV


def _rest_adjustment(home_rest, away_rest) -> float:
    if pd.isna(home_rest) or pd.isna(away_rest):
        return 0.0
    diff = float(home_rest) - float(away_rest)
    return max(-2.0, min(2.0, diff * 0.25))


def _is_div_game(value) -> bool:
    # nflverse div_game arrives as int 1/0 in games.csv, but a CSV or
    # DataFrame round-trip can turn it into "1", 1.0, or True. The old
    # code only matched str(value) == "1", so True/1.0 silently lost the
    # divisional discount. Accept every truthy 1-form.
    if isinstance(value, bool):
        return value
    try:
        return float(value) == 1.0
    except (TypeError, ValueError):
        return str(value).strip() == "1"


def _is_neutral(location) -> bool:
    return str(location or "").strip().lower() == "neutral"


def _cover_probability(independent: float, line: float) -> float:
    # P(home covers) under a normal approx. for (actual margin - model
    # margin) with the same stdev the survivor model uses. The pick's
    # cover probability is max(p, 1-p) in the caller; this returns the
    # home-side probability so the direction is unambiguous.
    z = (independent - line) / config.SURVIVOR_SPREAD_STDEV
    return round(0.5 * (1 + math.erf(z / math.sqrt(2))), 3)


def predict_game(
    row: pd.Series,
    team_ratings: dict[str, float],
    games_played: dict[str, int],
    hfa: float,
    manual_adj: dict[str, float] | None = None,
    injury_suggested: dict[str, float] | None = None,
    injury_apply: bool = False,
    epa_ratings: dict | None = None,
    epa_live: bool = False,
    pts_for: dict | None = None,
    pts_against: dict | None = None,
    weather_data: dict | None = None,
    pool_spread: float | None = None,
    pool_total: float | None = None,
) -> GamePrediction:
    manual_adj = manual_adj or {}
    injury_suggested = injury_suggested or {}

    home, away = row["home_team"], row["away_team"]
    inj_h = injury_suggested.get(home, 0.0) if injury_apply else 0.0
    inj_a = injury_suggested.get(away, 0.0) if injury_apply else 0.0

    r_home = team_ratings.get(home, 0.0) + manual_adj.get(home, 0.0) + inj_h
    r_away = team_ratings.get(away, 0.0) + manual_adj.get(away, 0.0) + inj_a

    rest_adj = _rest_adjustment(row.get("home_rest"), row.get("away_rest"))
    div_mult = 0.9 if _is_div_game(row.get("div_game")) else 1.0
    effective_hfa = 0.0 if _is_neutral(row.get("location")) else hfa

    margin_model = (r_home - r_away + effective_hfa + rest_adj) * div_mult

    epa_model = None
    if epa_ratings and home in epa_ratings and away in epa_ratings:
        epa_model = round(
            (epa_ratings[home] - epa_ratings[away]
             + manual_adj.get(home, 0.0) - manual_adj.get(away, 0.0)
             + effective_hfa + rest_adj) * div_mult, 1)

    combo_model = None
    if epa_model is not None:
        combo_model = round((1 - config.EPA_BLEND) * margin_model + config.EPA_BLEND * epa_model, 1)

    independent = combo_model if (epa_live and combo_model is not None) else margin_model

    # The LIVE market line -- always tracked and shown for reference,
    # regardless of whether a pool line override is set.
    vegas = row.get("spread_line")
    vegas = float(vegas) if pd.notna(vegas) else None

    # The line that actually drives the edge/pick: your pool's locked
    # line when you've set one, since that's what you're judged against,
    # falling back to the live line otherwise.
    line_for_pick = pool_spread if pool_spread is not None else vegas

    conf_home = ratings_mod.rating_confidence(games_played.get(home, 0))
    conf_away = ratings_mod.rating_confidence(games_played.get(away, 0))
    confidence = round(min(conf_home, conf_away), 2)

    # ---- Market blending: the fix for implausible edges ----
    # The market line is the best single predictor available (see README /
    # config.py comment on MARKET_TRUST_COMPLEMENT for the research behind
    # this). The "official" number used for the pick is the market line
    # nudged toward the model's independent read, damped by confidence and
    # capped by MARKET_TRUST_COMPLEMENT -- so the model can only pull the
    # pick a bounded distance from the market, and less so early in the
    # season when there's little current-season evidence behind it.
    if line_for_pick is not None:
        raw_deviation = independent - line_for_pick
        trust = confidence * config.MARKET_TRUST_COMPLEMENT
        official = line_for_pick + raw_deviation * trust
    else:
        raw_deviation = None
        official = independent

    edge = (official - line_for_pick) if line_for_pick is not None else None
    # FIXED 2026-11-04: flags are computed from the RAW deviation, not the
    # blended edge. Blending only changes the displayed edge magnitude;
    # it does NOT change the pick (the pick follows the deviation's sign),
    # so a "damped" edge must not be used to decide whether a pick is
    # strong or implausible -- that made both flags effectively dead.
    # strong_edge / needs_review below now use raw_deviation.
    cover_prob = None
    pick = None
    if edge is not None:
        home_cover = _cover_probability(independent, line_for_pick)
        cover_prob = home_cover if edge > 0 else round(1 - home_cover, 3)
        if edge == 0:
            cover_prob = 0.5
        if line_for_pick == 0:
            pick = f"{home if edge > 0 else away} PK"
        else:
            fav_team = home if edge > 0 else away
            line = line_for_pick if edge > 0 else -line_for_pick
            sign = "-" if line > 0 else "+"
            pick = f"{fav_team} {sign}{abs(line):g}"

    # ---- Totals ----
    vegas_total = row.get("total_line")
    vegas_total = float(vegas_total) if pd.notna(vegas_total) else None
    total_for_pick = pool_total if pool_total is not None else vegas_total
    model_total = None
    total_pick = None
    weather_note = ""
    if pts_for is not None and pts_against is not None:
        from . import scoring, weather as weather_mod
        model_total = scoring.predict_total(home, away, pts_for, pts_against)

        if weather_data is not None and not weather_mod.is_indoor(row.get("roof")):
            flat_red, pct_red, reason = weather_mod.total_adjustment(weather_data)
            if flat_red or pct_red:
                model_total = round(model_total * (1 - pct_red) - flat_red, 1)
                weather_note = reason

        if total_for_pick is not None:
            t_edge = model_total - total_for_pick
            if abs(t_edge) >= config.EDGE_THRESHOLD:
                total_pick = f"{'OVER' if t_edge > 0 else 'UNDER'} {total_for_pick:g}"
            else:
                total_pick = "no play"
        else:
            total_pick = "no line"

    return GamePrediction(
        week=int(row["week"]), home=home, away=away,
        vegas_spread=vegas, model_spread=round(official, 1),
        model_independent=round(independent, 1),
        margin_model=round(margin_model, 1), combo_model=combo_model,
        edge=round(edge, 1) if edge is not None else None,
        pick=pick, confidence=confidence,
        strong_edge=bool(raw_deviation is not None
                          and config.EDGE_THRESHOLD <= abs(raw_deviation) < config.SANITY_EDGE_FLAG),
        needs_review=bool(raw_deviation is not None and abs(raw_deviation) >= config.SANITY_EDGE_FLAG),
        epa_shadow=epa_model,
        manual_adj_home=manual_adj.get(home, 0.0), manual_adj_away=manual_adj.get(away, 0.0),
        vegas_total=vegas_total, model_total=model_total, total_pick=total_pick,
        weather_note=weather_note,
        pool_spread=pool_spread, pool_total=pool_total,
        cover_prob=cover_prob,
    )


def predict_completed_games(
    completed: pd.DataFrame,
    team_ratings: dict[str, float],
    games_played: dict[str, int],
    hfa: float,
    pts_for: dict | None = None,
    pts_against: dict | None = None,
) -> list[GamePrediction]:
    """
    Predicts games that have ALREADY been played, using whatever ratings
    are available right now -- for backfilling model_log when a game got
    missed while it was still upcoming (e.g. the logging feature was
    added mid-week, or a refresh was skipped). This is NOT a true
    point-in-time prediction -- it uses today's ratings, which already
    reflect results from other games since then -- so it's a reasonable
    stand-in for grading purposes, not a rigorous backtest. No injuries/
    manual adjustments are applied (those were day-of context that's
    lost in hindsight), and no market-blending is needed since we're
    just recording the raw model read against the market line that
    already existed for that game. Callers must save these with
    db.save_model_log(..., is_backfill=True) so grading.py excludes
    them -- they use today's ratings, which already contain these
    games' results, and counting them would inflate grades.
    """
    preds = []
    for _, row in completed.iterrows():
        p = predict_game(row, team_ratings, games_played, hfa, pts_for=pts_for, pts_against=pts_against)
        preds.append(p)
    return preds


def predict_week(
    upcoming: pd.DataFrame,
    team_ratings: dict[str, float],
    games_played: dict[str, int],
    hfa: float,
    manual_adj: dict[str, float] | None = None,
    injury_review: dict | None = None,
    epa: dict | None = None,
    pts_for: dict | None = None,
    pts_against: dict | None = None,
    fetch_weather: bool = True,
    pool_lines: dict | None = None,
) -> list[GamePrediction]:
    injury_review = injury_review or {"suggested": {}, "flags": {}}
    epa = epa or {"ratings": None}
    pool_lines = pool_lines or {}

    preds = []
    for _, row in upcoming.iterrows():
        weather_data = None
        if fetch_weather and pts_for is not None:
            from . import weather as weather_mod
            if not weather_mod.is_indoor(row.get("roof")):
                weather_data = weather_mod.fetch_forecast(row["home_team"], row.get("gameday"))

        override = pool_lines.get((row["home_team"], row["away_team"]), {})

        p = predict_game(
            row, team_ratings, games_played, hfa,
            manual_adj=manual_adj,
            injury_suggested=injury_review.get("suggested", {}),
            injury_apply=config.INJ_AUTO_APPLY,
            epa_ratings=epa.get("ratings"),
            epa_live=config.EPA_LIVE,
            pts_for=pts_for, pts_against=pts_against,
            weather_data=weather_data,
            pool_spread=override.get("pool_spread"),
            pool_total=override.get("pool_total"),
        )
        # Show a summary of ALL injury designations for this matchup here
        # (not just the high-severity "flags"), so it's visible that the
        # Injury Review tab's data is the same data feeding this row --
        # even though, by design, none of it moves the number automatically
        # (see config.INJ_AUTO_APPLY).
        detail = injury_review.get("detail", [])
        home_names = [d[1] or d[2] for d in detail if d[0] == row["home_team"]]
        away_names = [d[1] or d[2] for d in detail if d[0] == row["away_team"]]
        parts = []
        if home_names:
            parts.append(f"{row['home_team']}: {len(home_names)} listed ({', '.join(home_names[:3])}"
                          f"{'...' if len(home_names) > 3 else ''})")
        if away_names:
            parts.append(f"{row['away_team']}: {len(away_names)} listed ({', '.join(away_names[:3])}"
                          f"{'...' if len(away_names) > 3 else ''})")
        p.injury_flags = " | ".join(parts)
        preds.append(p)

    preds.sort(key=lambda p: abs(p.edge or 0), reverse=True)
    return preds


def predict_full_schedule(
    remaining_games: pd.DataFrame,
    team_ratings: dict[str, float],
    games_played: dict[str, int],
    hfa: float,
) -> pd.DataFrame:
    """
    Predicts model spread for every remaining game this season (not just
    next week), with no Vegas line dependency -- used by the survivor
    optimizer, which needs a full-season view. Rest/div adjustments are
    applied where the schedule data has them, ignored otherwise.
    """
    rows = []
    for _, row in remaining_games.iterrows():
        home, away = row["home_team"], row["away_team"]
        r_home = team_ratings.get(home, 0.0)
        r_away = team_ratings.get(away, 0.0)
        rest_adj = _rest_adjustment(row.get("home_rest"), row.get("away_rest"))
        div_mult = 0.9 if _is_div_game(row.get("div_game")) else 1.0
        effective_hfa = 0.0 if _is_neutral(row.get("location")) else hfa
        model_margin = (r_home - r_away + effective_hfa + rest_adj) * div_mult
        rows.append({
            "week": int(row["week"]), "home_team": home, "away_team": away,
            "model_margin": round(model_margin, 2),
        })
    return pd.DataFrame(rows)
