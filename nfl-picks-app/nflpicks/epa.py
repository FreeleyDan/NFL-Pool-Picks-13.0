"""
Weighted-EPA shadow layer. Stays in shadow mode (logged, never drives the
official pick) until Model_Grades shows it beats the baseline -- same
discipline as the original script. Note: like the original, this layer's
"defense allowed" figure is a naive average, not SOS-iterative like
ratings.py. That's a reasonable next upgrade once you're validating this
layer for promotion to live.
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
    ratings = {}
    for t in teams:
        o = avg(off[t]) - league_off
        d = (avg(def_allowed[t]) - league_off) if t in def_allowed else 0.0
        ratings[t] = round(config.EPA_OFF_WEIGHT * o - config.EPA_DEF_WEIGHT * d, 1)

    note = (f"Weighted EPA loaded for {len(teams)} teams "
            f"(off x{config.EPA_OFF_WEIGHT}, def x{config.EPA_DEF_WEIGHT}).")
    return {"ratings": ratings, "note": note}
