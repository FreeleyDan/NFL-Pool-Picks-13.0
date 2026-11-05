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


def expected_home_win_prob(elo_home: float, elo_away: float) -> float:
    """Logistic expected home win prob, with home field in Elo points."""
    diff = (elo_home + float(config.ELO_HFA_POINTS)) - elo_away
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


def regress_state(state: dict[str, float]) -> dict[str, float]:
    """Season rollover: pull every rating partway back to ELO_START."""
    f = float(config.ELO_PRIOR_REGRESSION)
    start = float(config.ELO_START)
    return {t: start + (r - start) * f for t, r in state.items()}


def apply_week(state: dict[str, float], week_games: pd.DataFrame) -> dict[str, float]:
    """Apply one week's results to `state`, returning a NEW dict.

    All updates use pre-week ratings (snapshot first, then update), so
    games within the same week never see each other. Rows need
    home_team, away_team, home_score, away_score; rows without scores
    are skipped. Teams not yet in state start at ELO_START.
    """
    if week_games is None or len(week_games) == 0:
        return dict(state)
    pre = dict(state)
    out = dict(state)
    k = float(config.ELO_K)
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
        exp_home = expected_home_win_prob(eh, ea)
        actual_home = 1.0 if hs > aws else 0.0 if hs < aws else 0.5
        delta = k * (actual_home - exp_home)
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
