"""
Survivor/eliminator pool optimizer.

WHY THIS IS A DIFFERENT PROBLEM FROM ATS PICKS:
In a survivor pool you pick one team per week to win outright, you can
never reuse a team, and you're eliminated the moment a pick loses. The
greedy strategy ("pick the biggest favorite every week") is provably
suboptimal: if you burn your best team in Week 3 against a bad opponent,
you don't have them available in Week 14 when you might need them against
a tougher slate. The right question isn't "who's most likely to win this
week" -- it's "how do I sequence my 17 team-uses across the season to
maximize my probability of surviving to the end."

This is solved exactly as an assignment problem: rows = remaining weeks,
columns = teams not yet used, cost = -log(win probability). Minimizing
total cost is equivalent to maximizing the product of survival
probabilities across the picks, which is exactly your probability of
lasting the whole season (assuming week-to-week independence).
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
from scipy.optimize import linear_sum_assignment

from . import config


def win_probability(margin_for_team: float, stdev: float = config.SURVIVOR_SPREAD_STDEV) -> float:
    """Probability a team wins, given their model margin (positive = favored),
    via a normal approximation of the NFL's spread-to-win-rate relationship."""
    z = margin_for_team / stdev
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def build_win_prob_table(schedule: pd.DataFrame) -> pd.DataFrame:
    """
    `schedule` needs columns: week, home_team, away_team, model_margin
    (model_margin = predicted home-minus-away point margin).

    Returns a long-format table: week, team, opponent, is_home, win_prob.
    """
    rows = []
    for _, r in schedule.iterrows():
        rows.append({
            "week": r["week"], "team": r["home_team"], "opponent": r["away_team"],
            "is_home": True, "win_prob": win_probability(r["model_margin"]),
        })
        rows.append({
            "week": r["week"], "team": r["away_team"], "opponent": r["home_team"],
            "is_home": False, "win_prob": win_probability(-r["model_margin"]),
        })
    return pd.DataFrame(rows)


def optimize_survivor(
    win_prob_table: pd.DataFrame,
    used_teams: set[str] | None = None,
    weeks_ahead: int | None = None,
) -> pd.DataFrame:
    """
    Solves for the season-maximizing sequence of picks.

    used_teams: teams you've already burned in prior weeks (excluded).
    weeks_ahead: optionally limit the lookahead horizon (e.g. 6) if you'd
        rather not commit far-future picks to noisy long-range ratings --
        the optimizer will still reserve strong teams appropriately within
        that horizon.

    Returns a DataFrame: week, recommended_team, opponent, is_home,
    win_prob, survival_prob_to_here (cumulative product through this pick).
    """
    used_teams = used_teams or set()
    t = win_prob_table[~win_prob_table["team"].isin(used_teams)].copy()

    weeks = sorted(t["week"].unique())
    if weeks_ahead:
        weeks = weeks[:weeks_ahead]
        t = t[t["week"].isin(weeks)]

    teams = sorted(t["team"].unique())
    if not weeks or not teams:
        return pd.DataFrame(columns=["week", "recommended_team", "opponent", "is_home", "win_prob"])

    BIG_COST = 1e6
    cost = np.full((len(weeks), len(teams)), BIG_COST)
    meta = {}  # (week_idx, team_idx) -> (opponent, is_home, win_prob)

    week_idx = {w: i for i, w in enumerate(weeks)}
    team_idx = {tm: i for i, tm in enumerate(teams)}

    for _, r in t.iterrows():
        wi, ti = week_idx[r["week"]], team_idx[r["team"]]
        p = min(max(r["win_prob"], 1e-6), 1 - 1e-9)
        c = -math.log(p)
        if c < cost[wi, ti]:
            cost[wi, ti] = c
            meta[(wi, ti)] = (r["opponent"], r["is_home"], r["win_prob"])

    row_ind, col_ind = linear_sum_assignment(cost)

    results = []
    survival = 1.0
    for wi, ti in sorted(zip(row_ind, col_ind), key=lambda x: x[0]):
        if cost[wi, ti] >= BIG_COST:
            continue  # that week had no valid (bye) assignment for this team
        opp, is_home, p = meta[(wi, ti)]
        survival *= p
        results.append({
            "week": weeks[wi], "recommended_team": teams[ti], "opponent": opp,
            "is_home": is_home, "win_prob": round(p, 3),
            "survival_prob_to_here": round(survival, 4),
        })
    return pd.DataFrame(results)


def greedy_baseline(win_prob_table: pd.DataFrame, used_teams: set[str] | None = None) -> pd.DataFrame:
    """
    The naive "pick the biggest favorite available each week" strategy,
    computed purely for comparison against the optimizer -- run both and
    look at survival_prob_to_here in the final week to see the difference.
    """
    used_teams = set(used_teams or set())
    t = win_prob_table.copy()
    results = []
    survival = 1.0
    for wk in sorted(t["week"].unique()):
        candidates = t[(t["week"] == wk) & (~t["team"].isin(used_teams))]
        if candidates.empty:
            continue
        best = candidates.loc[candidates["win_prob"].idxmax()]
        used_teams.add(best["team"])
        survival *= best["win_prob"]
        results.append({
            "week": wk, "recommended_team": best["team"], "opponent": best["opponent"],
            "is_home": best["is_home"], "win_prob": round(best["win_prob"], 3),
            "survival_prob_to_here": round(survival, 4),
        })
    return pd.DataFrame(results)
