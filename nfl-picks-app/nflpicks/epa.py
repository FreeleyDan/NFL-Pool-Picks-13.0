"""
Weighted-EPA shadow layer. Stays in shadow mode (logged, never drives the
official pick) until Model_Grades shows it beats the baseline -- same
discipline as the original script. Note: like the original, this layer's
"defense allowed" figure is a naive average, not SOS-iterative like
ratings.py. That's a reasonable next upgrade once you're validating this
layer for promotion to live.

CHANGED 2026-11-04 (backtest item 3): ratings are now CALIBRATED.
The old formula (1.6*off - 1.0*def, treated as points) overstates EPA
by ~2.6x: point-in-time calibration 2019-2025 gives ~0.38 points per
EPA unit (config.EPA_POINTS_PER_UNIT). The calibrated rating is
therefore EPA_POINTS_PER_UNIT * (1.6*off - 1.0*def). At app scale the
combo was 48.9% ATS with significantly worse MAE than SRS; at
calibrated scale the 50% combo was 49.9% ATS / MAE 10.35 vs SRS
48.0% / 10.66, MAE gains in all 7 seasons. That is promising, not
proven (ATS p=0.04 would not survive Bonferroni across 9 variants),
so EPA_LIVE stays False -- see config.py.
"""
from __future__ import annotations

import pandas as pd

from . import config


def weighted_epa_ratings(team_stats: pd.DataFrame | None) -> dict:
    if team_stats is None or team_stats.empty:
        return {"ratings": None, "note": "EPA feed unavailable -- shadow columns blank."}

    df = team_stats.copy()
    df["off_epa"] = df["passing_epa"].fillna(0) + df["rushing_epa"].fillna(0)

    off = df.groupby("team")["off_epa"].apply(list).to_dict()
    def_allowed = {}
    if "opponent_team" in df.columns:
        for _, row in df.iterrows():
            opp = row.get("opponent_team")
            if pd.isna(opp) or not opp:
                continue
            def_allowed.setdefault(opp, []).append(row["off_epa"])

    teams = list(off.keys())
    if not teams:
        return {"ratings": None, "note": "No team-week rows yet this season."}

    def avg(a):
        return sum(a) / len(a) if a else 0.0

    league_off = avg([avg(v) for v in off.values()])
    scale = getattr(config, "EPA_POINTS_PER_UNIT", 1.0)
    ratings = {}
    for t in teams:
        o = avg(off[t]) - league_off
        d = (avg(def_allowed[t]) - league_off) if t in def_allowed else 0.0
        ratings[t] = round(scale * (config.EPA_OFF_WEIGHT * o - config.EPA_DEF_WEIGHT * d), 1)

    note = (f"Weighted EPA loaded for {len(teams)} teams "
            f"(off x{config.EPA_OFF_WEIGHT}, def x{config.EPA_DEF_WEIGHT}, "
            f"calibrated x{scale} pts/EPA).")
    return {"ratings": ratings, "note": note}


def blend_epa_ratings(current: dict | None, prior: dict | None, games_played: dict) -> dict:
    """Blend current-season EPA ratings with regressed prior-season
    EPA ratings, using the same fade as ratings.blended_team_ratings
    (config.PRIOR_FADE_GAMES, PRIOR_REGRESSION). The live loader
    previously had NO prior blending -- Week 1 EPA was simply empty.
    This helper is used by backtest.py and by any future live promotion;
    it does not change EPA_LIVE by itself."""
    current = current or {}
    prior = prior or {}
    out: dict[str, float] = {}
    for t in set(current) | set(prior):
        prior_r = prior.get(t, 0.0) * config.PRIOR_REGRESSION
        n = games_played.get(t, 0)
        if n == 0:
            out[t] = round(prior_r, 1)
            continue
        w = min(1.0, n / config.PRIOR_FADE_GAMES)
        out[t] = round(w * current.get(t, 0.0) + (1 - w) * prior_r, 1)
    return out
