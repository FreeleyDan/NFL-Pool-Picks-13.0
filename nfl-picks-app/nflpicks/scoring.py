"""
Totals (over/under) models. Totals-only: nothing here touches the spread
model, and the pool is ATS-only, so a totals result never changes a
spread pick.

LIVE (default): the simplified scoring-average blend below, carried
over from the original script -- `blended_scoring_averages` +
`predict_total`. It mirrors the original Apps Script's blendPts()
logic exactly: a plain, unopinionated blend of points-for and
points-against averages.

SHADOW (config.TOTALS_SRS_LIVE=False, do not flip without a
walk-forward win): the SRS-style offensive/defensive scoring split --
`blended_srs_scoring` + `predict_total_srs`. This is the same idea as
ratings.py's SRS, applied to scoring instead of margin: a team's
offensive rating is its scoring above league average after adjusting
for opponent defense, and vice versa, solved iteratively. It was built
for item 8 and graded point-in-time 2019-2025 in backtest.py
(`evaluate_totals`): SRS 11.04 MAE vs old blend 10.70 MAE
out-of-sample, worse in 6 of 7 seasons (tied in 2025), so it stays
shadow. The spread model's off/def split SRS showed the same pattern
(worse MAE than single SRS), which suggests opponent-adjusting scoring
adds estimation noise that the plain averages do not have -- not a
bug in either solve, just a model that does not earn promotion.

Both models feed model.py the same way (a totals number, then the
weather adjustment in model.py, which is preserved on either path).
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


# ---------------------------------------------------------------------------
# SRS-style offensive/defensive scoring splits (SHADOW -- see module docstring)
# ---------------------------------------------------------------------------

def _srs_offdef_solve(
    played: pd.DataFrame, hfa: float
) -> tuple[dict[str, float], dict[str, float], float]:
    """
    Opponent-adjusted scoring solve on ONE season's games (the caller
    blends seasons, exactly like ratings.blended_team_ratings does for
    the spread model). Returns (off, def, league_avg), mean-centered:
    off[t] is points above league average the team's offense produces
    after adjusting for opponent defense; def[t] is POSITIVE for a good
    defense (it holds opponents below what their offense would
    otherwise produce). The fixed point is:

        expected score for t vs opp
            = league_avg + off[t] - def[opp] +/- hfa/2
        off[t] = mean_g (score_g - league_avg + def[opp] -/+ hfa/2)
        def[t] = mean_g (league_avg + off[opp] - opp_score_g -/+ hfa/2)

    Home-field is split hfa/2 to each side in the solve (as in
    backtest._offdef_ratings), so it cancels in the predicted TOTAL
    (see predict_total_srs) -- HFA shifts points between the two teams,
    not the combined total, to first order. Neutral-site games get no
    HFA term. Damped Jacobi (config.TOTALS_SRS_*) for the same
    disconnected-graph reason as ratings.py. No recency weighting and
    no score cap: the spread model's off/def diagnostic used neither,
    and adding them here would be two more tuning knobs fitted on the
    same sample the model already loses on -- they are not smuggled in.
    Per-team game lists are built once, so the solve is O(teams * games
    * iters) on lists, not a full-frame scan per iteration.
    """
    if played.empty:
        return {}, {}, config.LEAGUE_AVG_POINTS
    teams = sorted(set(played["home_team"]) | set(played["away_team"]))
    league_avg = float(
        (played["home_score"].astype(float) + played["away_score"].astype(float)).mean() / 2
    )
    # (opponent, team_score, opp_score, hfa_term) from this team's view.
    team_games: dict[str, list[tuple[str, float, float, float]]] = {t: [] for t in teams}
    for _, row in played.iterrows():
        home, away = row["home_team"], row["away_team"]
        hs, as_ = float(row["home_score"]), float(row["away_score"])
        is_neutral = str(row.get("location") or "").strip().lower() == "neutral"
        hfa_term = 0.0 if is_neutral else hfa / 2
        team_games[home].append((away, hs, as_, hfa_term))
        team_games[away].append((home, as_, hs, -hfa_term))
    off = {t: 0.0 for t in teams}
    deff = {t: 0.0 for t in teams}
    damping = getattr(config, "TOTALS_SRS_DAMPING", 0.5)
    max_iters = getattr(config, "TOTALS_SRS_MAX_ITERS", 200)
    tol = getattr(config, "TOTALS_SRS_TOLERANCE", 1e-4)
    for _ in range(max_iters):
        new_off, new_def = {}, {}
        for t in teams:
            off_vals = [ts - league_avg + deff[opp] - ht for opp, ts, _os, ht in team_games[t]]
            def_vals = [league_avg + off[opp] - os_ - ht for opp, _ts, os_, ht in team_games[t]]
            new_off[t] = sum(off_vals) / len(off_vals) if off_vals else 0.0
            new_def[t] = sum(def_vals) / len(def_vals) if def_vals else 0.0
        mo = sum(new_off.values()) / len(new_off)
        md = sum(new_def.values()) / len(new_def)
        new_off = {t: v - mo for t, v in new_off.items()}
        new_def = {t: v - md for t, v in new_def.items()}
        delta = max(max(abs(new_off[t] - off[t]), abs(new_def[t] - deff[t])) for t in teams)
        off = {t: damping * off[t] + (1 - damping) * new_off[t] for t in teams}
        deff = {t: damping * deff[t] + (1 - damping) * new_def[t] for t in teams}
        if delta < tol:
            break
    return off, deff, league_avg


def blended_srs_scoring(
    played_current: pd.DataFrame, played_prior: pd.DataFrame, hfa: float
) -> tuple[dict[str, float], dict[str, float], float]:
    """
    Returns (off, def, league_avg): the SRS scoring ratings blended
    across seasons with the spread model's fade philosophy -- the
    prior season's ratings are regressed by PRIOR_REGRESSION toward
    league average (0), then faded out as PRIOR_FADE_GAMES of
    current-season games accumulate per team, exactly like
    ratings.blended_team_ratings. League average is the pooled mean
    team score over both frames (dominated by the full prior season
    early, shifting to the current season late) -- NOT the hardcoded
    LEAGUE_AVG_POINTS, which sits ~1 point below recent actual scoring
    and would bias every SRS total low by ~2 points if used as the base.
    A team with no current-season games gets its regressed prior
    rating; a team with no prior gets its current rating unregressed
    (there is nothing to regress from). Empty inputs fall back to the
    hardcoded league average with empty ratings, so predict_total_srs
    still returns a sane total.
    """
    cur_off, cur_def, _ = _srs_offdef_solve(played_current, hfa) if len(played_current) else ({}, {}, 0.0)
    prior_off, prior_def, _ = _srs_offdef_solve(played_prior, hfa) if len(played_prior) else ({}, {}, 0.0)
    frames = [df for df in (played_current, played_prior) if df is not None and len(df)]
    if frames:
        pooled = pd.concat(frames, ignore_index=True)
        league_avg = float(
            (pooled["home_score"].astype(float) + pooled["away_score"].astype(float)).mean() / 2
        )
    else:
        league_avg = config.LEAGUE_AVG_POINTS

    games_played: dict[str, int] = {}
    for t in set(cur_off) | set(prior_off):
        n_home = len(played_current[played_current["home_team"] == t]) if len(played_current) else 0
        n_away = len(played_current[played_current["away_team"] == t]) if len(played_current) else 0
        games_played[t] = n_home + n_away

    off: dict[str, float] = {}
    deff: dict[str, float] = {}
    for t in set(cur_off) | set(prior_off):
        prior_o = prior_off.get(t, 0.0) * config.PRIOR_REGRESSION
        prior_d = prior_def.get(t, 0.0) * config.PRIOR_REGRESSION
        n = games_played.get(t, 0)
        if n == 0:
            off[t], deff[t] = prior_o, prior_d
            continue
        w = min(1.0, n / config.PRIOR_FADE_GAMES)
        off[t] = w * cur_off.get(t, 0.0) + (1 - w) * prior_o
        deff[t] = w * cur_def.get(t, 0.0) + (1 - w) * prior_d
    return off, deff, league_avg


def predict_total_srs(
    home: str, away: str, off: dict, deff: dict, league_avg: float | None = None
) -> float:
    """
    Matchup-aware SRS total: predicted home score is league average
    plus the home offense's rating minus the away defense's rating,
    and symmetrically for the away team. The HFA terms cancel (see
    _srs_offdef_solve), so no HFA argument is needed here. Missing
    teams default to league-average offense/defense (rating 0).
    """
    lg = config.LEAGUE_AVG_POINTS if league_avg is None else float(league_avg)
    home_score = lg + off.get(home, 0.0) - deff.get(away, 0.0)
    away_score = lg + off.get(away, 0.0) - deff.get(home, 0.0)
    return round(home_score + away_score, 1)
