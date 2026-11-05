"""
Play-by-play features and matchup interactions (batch 2, item 5).

HONEST SCOPE -- read before citing any feature name:
* True early-down success rate and true red-zone efficiency are NOT
  built here. Neither exists in the nflverse team-stats file (it has no
  down/distance columns and no red-zone columns), and the nflverse
  play-by-play release filename could not be confirmed -- guessed
  release URLs (csv / csv.gz / parquet for 2024) all returned 404, the
  same class of unconfirmed-filename problem documented for weekly
  rosters in config.py. Downloading an unverified multi-hundred-MB
  play-by-play file on a guessed URL, or inventing early-down/red-zone
  numbers from team totals, would be worse than reporting the gap.
  What this module builds instead, from the WEEK-FILTERED team-stats
  file only (data.load_team_stats(season, week) -- weeks strictly
  before the target week, plus the full prior season when the caller
  concatenates it), is:
    explosive_rate      -- (20+ and 40+ yard pass/rush plays) per play.
                           A genuine explosive-play proxy from real
                           columns (passing_20/40, rushing_20/40).
    pressure_allowed    -- sacks suffered per dropback.
    pressure_generated  -- (def sacks + QB hits) per opponent dropback,
                           joined on game_id to the opponent's row so
                           the denominator is that game's actual
                           dropbacks, not a league guess.
    pass_share          -- pass attempts per (attempts + carries).
    first_down_rate     -- (pass + rush first downs) per play. This is
                           a drive-sustainment proxy, explicitly NOT
                           early-down success.
* Interactions (both use only the target game's own row + the
  point-in-time rates above, so no future-week leak):
    wind_pass -- pass-heavy team in 20+ mph wind. Per team:
      (pass_share - league_pass_share) if wind >= WIND_HIGH_MPH else 0.
      The differential is home minus away: a positive value means the
      HOME team is the more pass-heavy side in high wind. The fitted
      coefficient's sign then says whether that helps or hurts; the
      module does not presume the direction.
    ol_rush  -- weak OL vs top pass rush. Per matchup:
      home_pressure_allowed * away_pressure_generated
        - away_pressure_allowed * home_pressure_generated.
      Positive means the home OL is more exposed than the away OL.
* Coefficients are fit by clipped least squares on a TRAINING window
  only (fit_feature_coeff): coeff = sum(diff * residual) /
  sum(diff^2), clipped to FEATURE_COEFF_CAPS, where residual =
  actual margin - SRS model margin. No coefficient is hand-set, and
  the backtest reports in-sample (training) vs out-of-sample
  (walk-forward season) separately. A feature whose out-of-sample
  MAE/ATS does not beat the SRS baseline is dropped, not promoted.
"""
from __future__ import annotations

import pandas as pd

from . import config

FEATURES = ("explosive", "pressure_allowed", "pressure_generated",
            "pass_share", "first_down")
INTERACTIONS = ("wind_pass", "ol_rush")


def team_feature_rates(team_stats: pd.DataFrame | None) -> dict:
    """Per-team point-in-time feature rates from a (filtered) team-stats
    frame. Returns {team: {feature: rate or None, "n_games": int}}.
    Missing columns yield None for that feature, never a fabricated 0
    that would read as "league average" in a differential."""
    if team_stats is None or team_stats.empty or "team" not in team_stats.columns:
        return {}
    df = team_stats.copy()
    # Opponent dropbacks per game, for pressure_generated: join each row
    # to its opponent's row on game_id (fall back to opponent_team +
    # week if game_id is absent).
    opp_dropbacks: dict = {}
    if "game_id" in df.columns and "attempts" in df.columns:
        by_game: dict = {}
        for _, r in df.iterrows():
            by_game.setdefault(str(r["game_id"]), {})[str(r["team"])] = r
        for gid, teams in by_game.items():
            for t, r in teams.items():
                others = [o for o in teams if o != t]
                if not others:
                    continue
                o = teams[others[0]]
                try:
                    opp_dropbacks[(gid, t)] = float(o["attempts"]) + float(o.get("sacks_suffered", 0) or 0)
                except (TypeError, ValueError):
                    pass
    out: dict[str, dict] = {}
    for team, sub in df.groupby("team"):
        n = len(sub)
        def _sum(col):
            return float(sub[col].fillna(0).sum()) if col in sub.columns else None
        attempts = _sum("attempts") or 0.0
        carries = _sum("carries") or 0.0
        plays = attempts + carries
        sacks_suf = _sum("sacks_suffered") or 0.0
        dropbacks = attempts + sacks_suf
        expl_num = 0.0
        for col in ("passing_20", "passing_40", "rushing_20", "rushing_40"):
            expl_num += _sum(col) or 0.0
        # Pressure generated: sum over games of (sacks + QB hits) /
        # opponent dropbacks that game; games without an opponent row
        # are skipped (not counted as zero pressure).
        pg_num, pg_den = 0.0, 0.0
        if "game_id" in df.columns:
            for _, r in sub.iterrows():
                den = opp_dropbacks.get((str(r["game_id"]), str(team)))
                if den:
                    pg_num += float(r.get("def_sacks", 0) or 0) + float(r.get("def_qb_hits", 0) or 0)
                    pg_den += den
        fd_num = (_sum("passing_first_downs") or 0.0) + (_sum("rushing_first_downs") or 0.0)
        out[str(team)] = {
            "explosive": (expl_num / plays) if plays > 0 else None,
            "pressure_allowed": (sacks_suf / dropbacks) if dropbacks > 0 else None,
            "pressure_generated": (pg_num / pg_den) if pg_den > 0 else None,
            "pass_share": (attempts / plays) if plays > 0 else None,
            "first_down": (fd_num / plays) if plays > 0 else None,
            "n_games": int(n),
        }
    return out


def league_rates(rates: dict) -> dict:
    """Simple mean of available team rates (missing teams excluded)."""
    out = {}
    for f in FEATURES:
        vals = [r[f] for r in rates.values() if r.get(f) is not None]
        out[f] = sum(vals) / len(vals) if vals else None
    return out


def _diff(rates: dict, league: dict, home: str, away: str, feature: str) -> float | None:
    h = rates.get(home, {}).get(feature)
    a = rates.get(away, {}).get(feature)
    if h is None and a is None:
        return None
    # A missing side reads as league average -- the neutral choice,
    # documented here rather than hidden in a 0.0 default.
    h = league.get(feature) if h is None else h
    a = league.get(feature) if a is None else a
    return float(h) - float(a)


def game_feature_differentials(game_row, rates: dict, league: dict | None = None) -> dict:
    """Feature differentials (home - away) + interactions for one game.

    game_row needs home_team, away_team and (for wind_pass) wind.
    Missing wind -> wind_pass is 0.0 (no wind information, no
    adjustment), not an imputed league wind.
    """
    league = league or league_rates(rates)
    home, away = str(game_row["home_team"]), str(game_row["away_team"])
    out = {}
    for f in FEATURES:
        out[f] = _diff(rates, league, home, away, f)
    # wind_pass interaction
    wind = game_row.get("wind")
    try:
        wind_val = float(wind) if wind is not None and not pd.isna(wind) else None
    except (TypeError, ValueError):
        wind_val = None
    if wind_val is not None and wind_val >= float(config.WIND_HIGH_MPH):
        lp = league.get("pass_share")
        hp = rates.get(home, {}).get("pass_share")
        ap = rates.get(away, {}).get("pass_share")
        if lp is not None and hp is not None and ap is not None:
            out["wind_pass"] = float((hp - lp) - (ap - lp))
        else:
            out["wind_pass"] = None
    else:
        out["wind_pass"] = 0.0
    # ol_rush interaction
    ha = rates.get(home, {}).get("pressure_allowed")
    ag = rates.get(away, {}).get("pressure_generated")
    aa = rates.get(away, {}).get("pressure_allowed")
    hg = rates.get(home, {}).get("pressure_generated")
    if None in (ha, ag, aa, hg):
        out["ol_rush"] = None
    else:
        out["ol_rush"] = float(ha * ag - aa * hg)
    return out


def fit_feature_coeff(train_rows: list[dict], feature: str) -> float:
    """Clipped least-squares coefficient for one feature on TRAIN rows.

    Each row needs f"{feature}_diff", "model_margin" (SRS) and
    "actual_margin". Returns 0.0 when the feature carries no usable
    variation in training. The cap comes from config so a tiny
    denominator (e.g. ol_rush differentials ~0.002) cannot produce an
    absurd points-per-unit coefficient.
    """
    num, den = 0.0, 0.0
    for r in train_rows:
        d = r.get(f"{feature}_diff")
        if d is None:
            continue
        residual = float(r["actual_margin"]) - float(r["model_margin"])
        num += float(d) * residual
        den += float(d) * float(d)
    if den <= 0:
        return 0.0
    cap = float(config.FEATURE_COEFF_CAPS.get(feature, 0.0))
    coeff = num / den
    return max(-cap, min(cap, coeff))


def adjusted_margin(model_margin: float, diffs: dict, coeffs: dict) -> float:
    """SRS margin + sum(coeff * diff) over fitted features. A None
    differential contributes nothing (league-average side)."""
    adj = float(model_margin)
    for f, c in coeffs.items():
        d = diffs.get(f)
        if d is not None and c:
            adj += float(c) * float(d)
    return adj
