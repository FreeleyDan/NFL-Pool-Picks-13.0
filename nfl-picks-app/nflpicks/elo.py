"""
Sequential Elo ratings (batch 2, item 4 -- no Elo existed before).

Design choices, kept deliberately plain so the ensemble cannot hide
overfitting behind Elo complexity:

* Win/loss only, no margin multiplier. Blowout information already
  lives in SRS; a margin-weighted Elo would double-count it and let a
  few blowouts swing the ensemble weight search.
* One update per week from PRE-week ratings: every game in a week is
  scored against ratings that do not yet contain that week's results,
  so no within-week result leaks into a same-week pick. This matches
  the backtest harness's point-in-time rule.
* Season rollover regresses ratings toward ELO_START by
  ELO_PRIOR_REGRESSION (same fade philosophy as SRS prior blending).
* Elo -> margin conversion uses the normal approximation already used
  elsewhere in the app (SURVIVOR_SPREAD_STDEV / MARKET_MARGIN_SD,
  13.5 pts): win prob from the Elo logistic curve, then margin =
  SD * Phi^-1(win prob), plus HFA. The conversion is an approximation
  -- Elo is a win-probability system, not a margin system -- and the
  ensemble weight search is where a mis-scaled Elo gets down-weighted
  instead of trusted.

This module is shadow/harness only: ENSEMBLE_LIVE stays False unless
the walk-forward ensemble beats live defaults (see README batch 2).
"""
from __future__ import annotations

import math
from statistics import NormalDist

import pandas as pd

from . import config

_NORM = NormalDist()


def new_state(teams) -> dict[str, float]:
    return {str(t): float(config.ELO_START) for t in teams}


def expected_home_win_prob(elo_home: float, elo_away: float, hfa_points: float | None = None) -> float:
    """Logistic expected home win prob, with home field in Elo points."""
    if hfa_points is None:
        hfa_points = float(config.ELO_HFA_POINTS)
    diff = (elo_home + float(hfa_points)) - elo_away
    return 1.0 / (1.0 + 10.0 ** (-diff / 400.0))


def elo_margin(elo_home: float, elo_away: float, hfa: float) -> float:
    """Elo ratings -> predicted home margin in points.

    Win prob from the logistic curve WITHOUT the Elo-points HFA (HFA
    enters here in points, like everywhere else in the app, so the
    harness HFA is the one used), converted with the normal quantile.
    Clipped to the win-prob range [0.001, 0.999] so the margin stays
    finite for extreme Elo gaps.
    """
    diff = elo_home - elo_away
    p = 1.0 / (1.0 + 10.0 ** (-diff / 400.0))
    p = min(0.999, max(0.001, p))
    sd = float(getattr(config, "MARKET_MARGIN_SD", 13.5))
    return sd * _NORM.inv_cdf(p) + float(hfa)


def regress_state(state: dict[str, float], fraction: float | None = None) -> dict[str, float]:
    """Season rollover: pull every rating partway back to ELO_START."""
    f = float(config.ELO_PRIOR_REGRESSION) if fraction is None else float(fraction)
    start = float(config.ELO_START)
    return {t: start + (r - start) * f for t, r in state.items()}


def _margin_multiplier(margin: float, mode: str | None) -> float:
    """Multiplier on the Elo update for a game decided by `margin` points
    (home perspective, absolute value used). None = win/loss only (the
    live/batch-2 rule). "log" = 1 + 0.3*log2(1+|margin|), capped at 2.0
    (a 1-pt win counts 1.3x, a 7-pt win 1.9x, 14+ pts 2.0x).
    "capped" = 1 + min(|margin|, 14)/28, i.e. 1.0x to 1.5x, linear.
    Both are bounded so a single blowout cannot swing a rating the way
    an uncapped margin multiplier would; SRS already carries blowout
    information, which is why the default stays win/loss only."""
    if mode is None:
        return 1.0
    m = abs(float(margin))
    if mode == "log":
        return min(2.0, 1.0 + 0.3 * math.log2(1.0 + m))
    if mode == "capped":
        return 1.0 + min(m, 14.0) / 28.0
    raise ValueError(f"unknown margin_mode: {mode!r}")


def apply_week(state: dict[str, float], week_games: pd.DataFrame, k: float | None = None,
               margin_mode: str | None = None, hfa_points: float | None = None,
               k_multiplier: float | None = None) -> dict[str, float]:
    """Apply one week's results to `state`, returning a NEW dict.

    All updates use pre-week ratings (snapshot first, then update), so
    games within the same week never see each other. Rows need
    home_team, away_team, home_score, away_score; rows without scores
    are skipped. Teams not yet in state start at ELO_START.
    Optional overrides exist ONLY for the batch-3 variant harness
    (elo_variants.py): k, margin_mode and hfa_points default to the
    config values, and k_multiplier (1.0 = no change) lets a caller
    scale K by week -- e.g. a recency variant that counts late-season
    games more than early ones. Live behaviour is unchanged when all
    overrides are left at their defaults.
    """
    if week_games is None or len(week_games) == 0:
        return dict(state)
    pre = dict(state)
    out = dict(state)
    k = float(config.ELO_K) if k is None else float(k)
    if k_multiplier is not None:
        k = k * float(k_multiplier)
    for _, row in week_games.iterrows():
        try:
            hs, aws = float(row["home_score"]), float(row["away_score"])
        except (TypeError, ValueError):
            continue
        if pd.isna(hs) or pd.isna(aws):
            continue
        home, away = str(row["home_team"]), str(row["away_team"])
        eh = pre.get(home, float(config.ELO_START))
        ea = pre.get(away, float(config.ELO_START))
        exp_home = expected_home_win_prob(eh, ea, hfa_points=hfa_points)
        actual_home = 1.0 if hs > aws else 0.0 if hs < aws else 0.5
        delta = k * (actual_home - exp_home) * _margin_multiplier(hs - aws, margin_mode)
        # Accumulate onto `out` (not `pre`): a team cannot play twice
        # in a real NFL week, but overwriting here would silently drop
        # the first game's update if it ever did.
        out[home] = out.get(home, eh) + delta
        out[away] = out.get(away, ea) - delta
    return out


def replay_season(state: dict[str, float], season_games: pd.DataFrame) -> dict[str, float]:
    """Replay a full season's played games week by week (week order,
    pre-week snapshot per week). Returns the end-of-season state."""
    if season_games is None or len(season_games) == 0:
        return dict(state)
    played = season_games[season_games["result"].notna()] if "result" in season_games.columns \
        else season_games[season_games["home_score"].notna()]
    cur = dict(state)
    for week in sorted(pd.to_numeric(played["week"], errors="coerce").dropna().unique().tolist()):
        wk = played[pd.to_numeric(played["week"], errors="coerce") == week]
        cur = apply_week(cur, wk)
    return cur
