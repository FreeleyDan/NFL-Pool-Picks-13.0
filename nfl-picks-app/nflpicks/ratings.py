"""
Opponent-adjusted power ratings (Simple Rating System / SRS).

WHY THIS REPLACES THE OLD MODEL:
The original Apps Script rated a team by averaging its own game margins.
That means a team that beat three bad teams by 10 looks identical to one
that beat three good teams by 10 -- there's no strength-of-schedule
adjustment. SRS fixes this with one extra idea: a team's rating is its
average margin *plus* the average rating of the teams it played, solved
iteratively until it converges. A blowout win over a good team now counts
for more than the same margin over a bad one.

Everything else (margin capping, recency weighting, prior-season blending
with fade) is preserved from the original model -- just applied on top of
SOS-adjusted numbers instead of raw ones.
"""
from __future__ import annotations

import logging
import math

import pandas as pd

from . import config

log = logging.getLogger(__name__)

# Last SRS solve diagnostics, for weekly_run to surface in Data Health.
# SRS used to silently return after SRS_MAX_ITERS even if it never
# converged -- now weekly_run records a failed convergence instead of
# letting a non-converged rating pass as normal.
LAST_SRS_CONVERGED: bool = True
LAST_SRS_DELTA: float = 0.0


def _adjusted_margin(home_score: float, away_score: float, hfa: float, is_neutral: bool = False) -> float:
    """Home-field-neutral margin, capped so one blowout doesn't dominate.
    For neutral-site games (international games, Super Bowls), no HFA is
    subtracted -- there's no real home-field advantage to remove."""
    effective_hfa = 0.0 if is_neutral else hfa
    raw = (home_score - away_score) - effective_hfa
    return max(-config.MARGIN_CAP, min(config.MARGIN_CAP, raw))


def compute_empirical_hfa(played_current: pd.DataFrame, played_prior: pd.DataFrame) -> float:
    """
    Home-field advantage estimated from your own data (current season,
    backstopped by prior season while the sample is thin) instead of a
    hardcoded constant. League-average HFA has drifted down since ~2020,
    so a fixed value goes stale.

    Neutral-site games (international games, Super Bowls) are excluded --
    there's no true home field to measure in those, and including them
    would dilute the estimate. This matches standard practice in public
    HFA research.
    """
    frames = [df for df in (played_current, played_prior) if df is not None and len(df)]
    if not frames:
        return 2.0  # sane fallback if there's simply no data yet
    all_games = pd.concat(frames, ignore_index=True)
    if "location" in all_games.columns:
        all_games = all_games[all_games["location"] != "Neutral"]
    if all_games.empty:
        return 2.0
    margins = all_games["home_score"].astype(float) - all_games["away_score"].astype(float)
    hfa = float(margins.mean())
    return max(config.HFA_MIN, min(config.HFA_MAX, hfa))


def _recency_weight(games_ago: int) -> float:
    """Exponential decay: most recent game = weight 1, older games decay
    with a half-life of RECENT_HALF_LIFE_GAMES games."""
    return 0.5 ** (games_ago / config.RECENT_HALF_LIFE_GAMES)


def srs_ratings(games: pd.DataFrame, hfa: float) -> dict[str, float]:
    """
    Iterative SRS solve. `games` must have home_team, away_team, home_score,
    away_score, and be ordered oldest-to-newest (so recency weighting lines
    up with real chronology).

    Returns {team: rating}, mean-centered at 0, in points-per-game.
    """
    if games.empty:
        return {}

    games = games.reset_index(drop=True)
    teams = sorted(set(games["home_team"]) | set(games["away_team"]))
    ratings = {t: 0.0 for t in teams}

    # IMPORTANT: recency weight must be computed from each team's OWN game
    # sequence, not from that game's row position in the full multi-team
    # league-wide list. A team's Week 1 game is one row near the start of
    # the ~272-row full-season file; if "games_ago" were measured against
    # the full file's length, a Week 1 game would look like it happened
    # ~250+ games ago and get a weight of effectively zero, when for THAT
    # team it's actually their oldest game out of only ~17. Getting this
    # wrong meant only the last week or so of the ENTIRE LEAGUE'S season
    # carried any real weight, so a team's rating could end up dominated
    # almost entirely by a single late-season result.
    team_total_games: dict[str, int] = {t: 0 for t in teams}
    for _, row in games.iterrows():
        team_total_games[row["home_team"]] += 1
        team_total_games[row["away_team"]] += 1

    team_games_seen: dict[str, int] = {t: 0 for t in teams}
    edges: dict[str, list[tuple[str, float, float]]] = {t: [] for t in teams}
    for _, row in games.iterrows():
        home, away = row["home_team"], row["away_team"]
        team_games_seen[home] += 1
        team_games_seen[away] += 1
        games_ago_home = team_total_games[home] - team_games_seen[home]  # 0 = this team's most recent game
        games_ago_away = team_total_games[away] - team_games_seen[away]
        w_home = _recency_weight(games_ago_home)
        w_away = _recency_weight(games_ago_away)
        is_neutral = row.get("location") == "Neutral"
        m = _adjusted_margin(row["home_score"], row["away_score"], hfa, is_neutral)
        edges[home].append((away, m, w_home))
        edges[away].append((home, -m, w_away))

    global LAST_SRS_CONVERGED, LAST_SRS_DELTA
    LAST_SRS_CONVERGED = False
    LAST_SRS_DELTA = float("inf")
    for _ in range(config.SRS_MAX_ITERS):
        new_ratings = {}
        for t in teams:
            num, den = 0.0, 0.0
            for opp, margin, w in edges[t]:
                num += w * (margin + ratings[opp])
                den += w
            new_ratings[t] = (num / den) if den > 0 else 0.0
        # mean-center so the league average rating is always 0
        mean = sum(new_ratings.values()) / len(new_ratings)
        new_ratings = {t: r - mean for t, r in new_ratings.items()}

        delta = max(abs(new_ratings[t] - ratings[t]) for t in teams)
        ratings = new_ratings
        LAST_SRS_DELTA = delta
        if delta < config.SRS_TOLERANCE:
            LAST_SRS_CONVERGED = True
            break
    if not LAST_SRS_CONVERGED:
        log.warning("SRS did not converge after %s iterations (final delta %.4f) -- "
                    "ratings may be unstable; check Data Health.",
                    config.SRS_MAX_ITERS, LAST_SRS_DELTA)

    return ratings


def blended_team_ratings(
    played_current: pd.DataFrame,
    played_prior: pd.DataFrame,
    hfa: float,
) -> tuple[dict[str, float], dict[str, int]]:
    """
    Final per-team rating = current-season SRS, faded in from regressed
    prior-season SRS as current-season sample size grows -- same fade
    philosophy as the original script, now applied to SOS-adjusted ratings.

    Returns (ratings, games_played_this_season).
    """
    prior_ratings = srs_ratings(played_prior, hfa) if len(played_prior) else {}
    current_ratings = srs_ratings(played_current, hfa) if len(played_current) else {}

    games_played: dict[str, int] = {}
    all_teams = set(prior_ratings) | set(current_ratings)
    for t in all_teams:
        n_home = len(played_current[played_current["home_team"] == t]) if len(played_current) else 0
        n_away = len(played_current[played_current["away_team"] == t]) if len(played_current) else 0
        games_played[t] = n_home + n_away

    final: dict[str, float] = {}
    for t in all_teams:
        prior_r = prior_ratings.get(t, 0.0) * config.PRIOR_REGRESSION
        n = games_played.get(t, 0)
        if n == 0:
            final[t] = prior_r
            continue
        cur_r = current_ratings.get(t, 0.0)
        w = min(1.0, n / config.PRIOR_FADE_GAMES)
        final[t] = w * cur_r + (1 - w) * prior_r

    return final, games_played


def raw_point_differential(played: pd.DataFrame) -> dict[str, float]:
    """
    Simple, unadjusted average point differential per team (no SOS, no
    recency weighting, no HFA, no capping) -- purely a sanity-check number.
    A team's SRS rating and its raw point differential should almost
    always have the same SIGN (SOS-adjustment changes magnitude and can
    reorder teams that are close, but a team that outscored opponents
    overall should essentially never end up with a negative SRS rating,
    and vice versa). If the dashboard ever shows these disagreeing in
    sign for a team, that's strong evidence of a real bug, independent of
    whatever the SRS math is doing.
    """
    diffs: dict[str, list[float]] = {}
    for _, row in played.iterrows():
        h, a = row["home_team"], row["away_team"]
        hs, as_ = float(row["home_score"]), float(row["away_score"])
        diffs.setdefault(h, []).append(hs - as_)
        diffs.setdefault(a, []).append(as_ - hs)
    return {t: round(sum(v) / len(v), 2) for t, v in diffs.items()}


def debug_team_games(team: str, played: pd.DataFrame, hfa: float) -> list[dict]:
    """
    Every game a team played, with the exact intermediate values the
    rating engine computed from it: raw margin, HFA-adjusted margin, and
    the recency weight applied. This is the ground-truth trace for
    diagnosing a rating that looks wrong -- it shows precisely which
    games are pulling the number up or down, instead of trusting the
    aggregate.
    """
    if played.empty:
        return []
    played = played.reset_index(drop=True)
    team_games = played[(played["home_team"] == team) | (played["away_team"] == team)].reset_index(drop=True)
    n_team_games = len(team_games)
    rows = []
    for i, row in team_games.iterrows():
        h, a = row["home_team"], row["away_team"]
        is_home = (h == team)
        opp = a if is_home else h
        hs, as_ = float(row["home_score"]), float(row["away_score"])
        team_score = hs if is_home else as_
        opp_score = as_ if is_home else hs
        is_neutral = row.get("location") == "Neutral"
        m = _adjusted_margin(hs, as_, hfa, is_neutral)  # from the HOME team's perspective
        team_adj_margin = m if is_home else -m
        games_ago = n_team_games - 1 - i  # 0 = this team's most recent game
        w = _recency_weight(games_ago)
        rows.append({
            "week": row.get("week"), "opponent": opp, "home_or_away": "home" if is_home else "away",
            "neutral_site": is_neutral,
            "team_score": team_score, "opp_score": opp_score, "raw_margin": team_score - opp_score,
            "hfa_adjusted_margin": round(team_adj_margin, 2), "recency_weight": round(w, 3),
        })
    return rows


def rating_confidence(games_played: int) -> float:
    """
    0-1 scale: how much evidence backs this team's rating, used to set how
    much independent weight the market-blending in model.py gives the
    model (see config.MARKET_TRUST_COMPLEMENT).

    IMPORTANT: this does NOT floor at 0 with zero current-season games.
    Even in Week 1, before any current-season game has been played, we
    still have a real signal -- last season's SOS-adjusted rating,
    regressed. A hard 0 here previously meant the market-blending step
    (confidence * MARKET_TRUST_COMPLEMENT) zeroed out entirely at Week 1,
    silently making every "official" pick exactly equal to the Vegas
    line with 0 edge -- confidence is deliberately bounded away from 0 to
    avoid that trap. It still rises toward 1.0 as real current-season
    evidence accumulates, which is when the model should be trusted most.
    """
    base = config.BASE_CONFIDENCE_WITH_PRIOR_DATA
    return base + (1 - base) * min(1.0, games_played / config.MIN_GAMES_FOR_FULL_CONFIDENCE)


def audit_team_game_counts(played_prior: pd.DataFrame) -> list[str]:
    """
    Sanity check on the prior-season data that feeds every Week 1-3
    rating: a real, healthy NFL team plays 17 regular-season games. If any
    team's count here is far from that, it's strong evidence of a data or
    team-code bug (a team's games split across two different codes,
    duplicate rows, or a bad filter) rather than the rating math itself --
    exactly the kind of issue that could produce a rating that looks
    backwards (a team with a bad record showing as a top-rated team).
    Returns a list of human-readable warning strings, empty if all clear.
    """
    if played_prior.empty:
        return ["No prior-season data loaded at all -- every current rating is meaningless until this is fixed."]
    counts = {}
    for _, row in played_prior.iterrows():
        counts[row["home_team"]] = counts.get(row["home_team"], 0) + 1
        counts[row["away_team"]] = counts.get(row["away_team"], 0) + 1
    warnings = []
    for team, n in sorted(counts.items()):
        if n < 15 or n > 19:
            warnings.append(f"{team}: {n} prior-season games found (expected ~17) -- possible data/team-code issue")
    if len(counts) < 32:
        warnings.append(f"Only {len(counts)} teams found in prior-season data (expected 32)")
    return warnings
