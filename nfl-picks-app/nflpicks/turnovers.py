"""
Turnover luck regression.

WHY THIS MATTERS: a team's SRS rating is built on actual point margin,
and turnovers directly swing point margin -- but turnover margin is one
of the least persistent stats in football. Fumble recovery rate in
particular is close to a 50/50 coin flip at the team level regardless of
skill (the ball just bounces where it bounces); interceptions have a bit
more skill in them (pass rush, coverage) but still carry real variance.
A team on a hot turnover-luck streak looks better than their underlying
team is; a team on a cold streak looks worse. This regresses that back
out as a transparent, capped correction to the rating -- not a shadow
layer, since the underlying finding (turnover margin doesn't persist) is
well-established, not something that needs its own live validation the
way the EPA layer does.
"""
from __future__ import annotations

import pandas as pd

from . import config


def compute_turnover_margins(team_stats: pd.DataFrame | None) -> dict[str, dict]:
    """
    Returns {team: {"games": n, "margin_per_game": float, "takeaways": int, "giveaways": int}}
    from the same team-week stats file used for the EPA layer.
    """
    if team_stats is None or team_stats.empty:
        return {}

    needed = {"passing_interceptions", "sack_fumbles_lost", "rushing_fumbles_lost",
              "receiving_fumbles_lost", "def_interceptions", "fumble_recovery_opp"}
    if not needed.issubset(team_stats.columns):
        return {}

    df = team_stats.copy()
    for c in needed:
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)

    df["giveaways"] = df["passing_interceptions"] + df["sack_fumbles_lost"] + \
        df["rushing_fumbles_lost"] + df["receiving_fumbles_lost"]
    df["takeaways"] = df["def_interceptions"] + df["fumble_recovery_opp"]
    df["margin"] = df["takeaways"] - df["giveaways"]

    out = {}
    for team, grp in df.groupby("team"):
        n = len(grp)
        out[team] = {
            "games": n,
            "margin_per_game": round(grp["margin"].sum() / n, 3) if n else 0.0,
            "takeaways": int(grp["takeaways"].sum()),
            "giveaways": int(grp["giveaways"].sum()),
        }
    return out


def luck_adjustment(margins: dict[str, dict]) -> dict[str, float]:
    """
    Returns {team: rating_points_adjustment}. NEGATIVE for a team whose
    turnover margin has been lucky (their rating gets pulled back down
    toward what their turnover-neutral performance suggests), POSITIVE
    for a team that's been turnover-unlucky.

    Regresses heavily toward zero (config.TURNOVER_LUCK_PERSISTENCE is
    the fraction treated as "real" skill, the rest as luck to regress
    out) and scales by a per-turnover point value consistent with the
    rest of the app's injury-value calibration. Confidence in the
    correction grows with sample size, same fade philosophy as the
    rating engine's prior-season blend.
    """
    adjustments = {}
    for team, m in margins.items():
        n = m["games"]
        if n == 0:
            adjustments[team] = 0.0
            continue
        real_fraction = config.TURNOVER_LUCK_PERSISTENCE
        excess_luck_per_game = m["margin_per_game"] * (1 - real_fraction)
        sample_conf = min(1.0, n / config.TURNOVER_LUCK_FULL_SAMPLE_GAMES)
        points = -excess_luck_per_game * config.POINTS_PER_TURNOVER * sample_conf
        points = max(-config.TURNOVER_LUCK_CAP, min(config.TURNOVER_LUCK_CAP, points))
        adjustments[team] = round(points, 2)
    return adjustments
