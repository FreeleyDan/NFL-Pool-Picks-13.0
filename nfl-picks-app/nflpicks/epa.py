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


def sos_epa_ratings(team_stats: pd.DataFrame | None) -> dict:
    """SOS-iterative EPA ratings (batch 2, item 4).

    Fixes the naive-average defect documented at the top of this file:
    the old "defense allowed" was a plain average of opponents' offensive
    EPA, so a defense that faced weak offenses looked identical to one
    that faced strong ones. This solve mirrors ratings.py's SRS and
    backtest.py's _offdef_ratings, but in EPA units:

      expected off_epa for team t vs opp o
          = league_off + off[t] - def[o]
      so off[t] = mean_g (off_epa_g - league_off + def[opp])
         def[t] = mean_g (league_off + off[opp] - opp_off_epa_g)

    where def is POSITIVE for a good defense (it holds opponents below
    what their offense would otherwise produce). Both off and def are
    mean-centered, then combined with the same calibrated scale and
    weights as weighted_epa_ratings -- except the sign on defense flips
    to match the new definition: rating =
    EPA_POINTS_PER_UNIT * (EPA_OFF_WEIGHT * off + EPA_DEF_WEIGHT * def).
    (In the old parametrization def_allowed was league-relative allowed
    EPA, so it was subtracted; here def is goodness, so it is added.
    The two agree in magnitude when opponent offenses average to league.)

    Damped Jacobi (config.EPA_SOS_DAMPING) like SRS, because the same
    early-season disconnected-graph problem applies. Unweighted per-game
    average (the old layer was unweighted too); the caller must pass a
    week-filtered frame (data.load_team_stats(season, week)) or the
    ratings leak future weeks. Returns the same shape as
    weighted_epa_ratings plus "off"/"def" breakdowns for diagnostics.
    """
    if team_stats is None or team_stats.empty:
        return {"ratings": None, "note": "EPA feed unavailable -- shadow columns blank."}
    df = team_stats.copy()
    if "passing_epa" not in df.columns or "rushing_epa" not in df.columns:
        return {"ratings": None, "note": "EPA feed missing passing/rushing EPA columns."}
    df["off_epa"] = df["passing_epa"].fillna(0) + df["rushing_epa"].fillna(0)
    if "team" not in df.columns:
        return {"ratings": None, "note": "EPA feed missing team column."}
    teams = sorted(set(df["team"].dropna().astype(str)))
    if not teams:
        return {"ratings": None, "note": "No team-week rows yet this season."}
    league_off = float(df["off_epa"].mean())
    # Per-team game lists: offense games (rows where team == t) and
    # defense games (rows where opponent_team == t; the row's off_epa
    # is what that opponent's offense produced against t).
    off_games: dict[str, list[tuple[str, float]]] = {t: [] for t in teams}
    def_games: dict[str, list[tuple[str, float]]] = {t: [] for t in teams}
    for _, row in df.iterrows():
        t = str(row["team"])
        if t not in off_games:
            continue
        opp = row.get("opponent_team")
        opp = None if pd.isna(opp) or not opp else str(opp)
        if opp is None or opp not in off_games:
            # No usable opponent (bye-like row): offense still counts
            # against league average, defense cannot be attributed.
            off_games[t].append(("", float(row["off_epa"])))
            continue
        off_games[t].append((opp, float(row["off_epa"])))
        def_games[opp].append((t, float(row["off_epa"])))
    off = {t: 0.0 for t in teams}
    deff = {t: 0.0 for t in teams}
    damping = getattr(config, "EPA_SOS_DAMPING", 0.5)
    max_iters = getattr(config, "EPA_SOS_MAX_ITERS", 200)
    tol = getattr(config, "EPA_SOS_TOLERANCE", 1e-4)
    for _ in range(max_iters):
        new_off, new_def = {}, {}
        for t in teams:
            vals = [(v - league_off + deff[o]) if o else (v - league_off)
                    for o, v in off_games[t]]
            new_off[t] = sum(vals) / len(vals) if vals else 0.0
            dvals = [league_off + off[o] - v for o, v in def_games[t]]
            new_def[t] = sum(dvals) / len(dvals) if dvals else 0.0
        mo = sum(new_off.values()) / len(new_off)
        md = sum(new_def.values()) / len(new_def)
        new_off = {t: v - mo for t, v in new_off.items()}
        new_def = {t: v - md for t, v in new_def.items()}
        delta = max(max(abs(new_off[t] - off[t]), abs(new_def[t] - deff[t])) for t in teams)
        off = {t: damping * off[t] + (1 - damping) * new_off[t] for t in teams}
        deff = {t: damping * deff[t] + (1 - damping) * new_def[t] for t in teams}
        if delta < tol:
            break
    scale = getattr(config, "EPA_POINTS_PER_UNIT", 1.0)
    ratings = {
        t: round(scale * (config.EPA_OFF_WEIGHT * off[t] + config.EPA_DEF_WEIGHT * deff[t]), 1)
        for t in teams
    }
    return {
        "ratings": ratings,
        "off": {t: round(v, 3) for t, v in off.items()},
        "def": {t: round(v, 3) for t, v in deff.items()},
        "note": (f"SOS-iterative EPA loaded for {len(teams)} teams "
                 f"(off x{config.EPA_OFF_WEIGHT}, def x{config.EPA_DEF_WEIGHT}, "
                 f"calibrated x{scale} pts/EPA)."),
    }


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
