"""
Blended scoring averages, used only for the totals (over/under) model.
This mirrors the original Apps Script's blendPts() logic exactly -- it's a
simple, unopinionated blend, not SOS-adjusted like ratings.py. Improving
this the same way ratings.py was improved (opponent-adjusted offensive/
defensive scoring splits) is the natural next upgrade -- see README.
"""
from __future__ import annotations

import pandas as pd

from . import config


def _avg(a: list[float]) -> float:
    return sum(a) / len(a) if a else 0.0


def _team_points(played: pd.DataFrame) -> tuple[dict, dict]:
    """Returns (points_for, points_against) as {team: [scores...]}."""
    pts_for: dict[str, list[float]] = {}
    pts_against: dict[str, list[float]] = {}
    for _, row in played.iterrows():
        home, away = row["home_team"], row["away_team"]
        hs, as_ = float(row["home_score"]), float(row["away_score"])
        pts_for.setdefault(home, []).append(hs)
        pts_against.setdefault(home, []).append(as_)
        pts_for.setdefault(away, []).append(as_)
        pts_against.setdefault(away, []).append(hs)
    return pts_for, pts_against


def blended_scoring_averages(
    played_current: pd.DataFrame, played_prior: pd.DataFrame
) -> tuple[dict[str, float], dict[str, float]]:
    """
    Returns (pts_for, pts_against): each {team: blended average points},
    blending current-season averages with a lightly-regressed prior-season
    average, fading the prior out as PRIOR_FADE_GAMES of current-season
    data accumulate -- same fade philosophy as the rating model.
    """
    cur_for, cur_against = _team_points(played_current) if len(played_current) else ({}, {})
    prior_for, prior_against = _team_points(played_prior) if len(played_prior) else ({}, {})

    all_teams = set(cur_for) | set(prior_for) | set(cur_against) | set(prior_against)

    def blend(cur_dict, prior_dict, team):
        cur_arr = cur_dict.get(team, [])
        prior_arr = prior_dict.get(team, [])
        prior_avg = (0.75 * _avg(prior_arr) + 0.25 * config.LEAGUE_AVG_POINTS) if prior_arr else config.LEAGUE_AVG_POINTS
        if not cur_arr:
            return prior_avg
        w = min(1.0, len(cur_arr) / config.PRIOR_FADE_GAMES)
        return w * _avg(cur_arr) + (1 - w) * prior_avg

    pts_for = {t: blend(cur_for, prior_for, t) for t in all_teams}
    pts_against = {t: blend(cur_against, prior_against, t) for t in all_teams}
    return pts_for, pts_against


def predict_total(home: str, away: str, pts_for: dict, pts_against: dict) -> float:
    """Same formula as the original script's mTotal calculation."""
    home_side = (pts_for.get(home, config.LEAGUE_AVG_POINTS) + pts_against.get(away, config.LEAGUE_AVG_POINTS)) / 2
    away_side = (pts_for.get(away, config.LEAGUE_AVG_POINTS) + pts_against.get(home, config.LEAGUE_AVG_POINTS)) / 2
    return round(home_side + away_side, 1)
