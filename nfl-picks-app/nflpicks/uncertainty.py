"""
Rating uncertainty (batch 3, item 7): Bayesian-style bands around team
ratings, and what those bands imply for cover probabilities.

What this is NOT: a new rating, a new pick, or a guarantee. The point
estimates in picks.py / the dashboard are still the model. This module
only answers "how far off could that point estimate plausibly be?",
so a Thursday review can tell a genuinely strong lean from a lean
built on four games of evidence. No pick logic reads this module:
config.UNCERTAINTY_PICK_USE stays False, and there is currently no
code path that would change a pick from a band even if it were
flipped -- wiring one in would first need a walk-forward test
showing uncertainty-adjusted picks beat the current picks
out-of-sample, which has not been run. Labels and bands only.

The model, stated so it can be checked rather than trusted:
  * A team's rating is treated as Normal(mean = blended SRS rating,
    sd = rating_sd(games_played)). The mean is the existing rating;
    only the sd is new here.
  * Prior: before any current-season game, the rating is the
    regressed prior-season SRS, whose error is assigned sd
    config.UNCERTAINTY_PRIOR_SD (5.0 pts). That value is a judgment
    parameter, not a fitted one: a prior-season rating carried
    across an offseason (roster turnover, coaching change,
    regression itself) is genuinely less certain than a
    mid-season rating, and 5 pts is roughly the spread of
    year-over-year rating changes for a typical team. The tests
    pin the SHAPE (monotone narrowing, correct limits), not the
    wisdom of 5.0 -- if a future fit revises it, the shape stays.
  * Likelihood: each current-season game is one noisy observation
    of the rating with sd config.UNCERTAINTY_GAME_MARGIN_SD
    (13.43 pts, the residual SD of the 2019-2025 point-in-time
    backtest in calibration.py). That SD already includes
    opponent-rating error and game-to-game noise, so using it as
    the per-game noise is, if anything, conservative (wide).
  * Recency: the SRS solve weights games with half-life
    config.RECENT_HALF_LIFE_GAMES, so an old game is not a full
    observation. effective_sample_size() converts a game count
    into the recency-weighted effective count
    ESS = (sum w)^2 / (sum w^2), which saturates: even a full
    17-game season is only ~12 effective games under a 5-game
    half-life. That saturation is why late-season bands stop
    narrowing -- it is a property of the weighting, not a bug.
  * Posterior (precision form): 1/var = 1/prior_var +
    ESS/game_var. At 0 games this returns exactly the prior sd;
    as ESS grows the sd falls toward game_sd/sqrt(ESS).
    With the defaults: 0 games -> 5.00, 4 games -> ~3.9,
    12 games -> ~3.2, 17 games -> ~3.1.

What was deliberately left out in batch 3, and is added in
batch 4 group C item 7 (functions at the end of this module;
the batch-3 functions above are UNCHANGED, so old tests and
old bands still reproduce exactly):
  * Home-field uncertainty: the margin adds HFA as a constant.
    Ignoring its error understates margin variance slightly.
    Round 2 adds it as +UNCERTAINTY_HFA_SD^2 (1.0 pt, the
    year-to-year SD of season home-margin means 2019-2025,
    1.07 rounded -- see config.py), so a neutral-site game,
    where HFA is 0 by construction, should pass hfa_sd=0.0
    explicitly rather than taking the default.
  * Covariance between the two teams' ratings: SRS solves all
    teams jointly from shared games, so rating errors are not
    independent. Ignoring positive correlation OVERSTATES the
    margin variance (Var(a-b) < Var(a)+Var(b) when a,b are
    positively correlated). Round 2 computes that covariance
    from the game graph itself (rating_covariance_matrix):
    the SRS point estimate is the ridge solution to
    minimising sum_games (margin - (r_home - r_away))^2 /
    game_var + sum_teams (r - prior)^2 / prior_var, so the
    posterior covariance of the ratings is the inverse of
    the precision matrix P with P[t][t] = 1/prior_var +
    n_t/game_var and P[t][u] = -n_tu/game_var (n_tu = games
    between t and u in the rating window). A direct game
    between the pair AND shared opponents both enter P, so
    no separate shared/direct bookkeeping is needed and no
    correlation constant is fitted to picks. The diagonal
    of that inverse is close to rating_sd()^2 (it differs
    only by the coupling -- e.g. one shared game gives a
    marginal SD of 4.72 vs rating_sd(1) = 4.69), which is a
    check that the graph method agrees with the batch-3
    formula where it should. The two round-2 corrections
    push in opposite directions (covariance narrows the
    margin band, HFA widens it); at mid-season the net is
    a slightly narrower band than batch 3, which is the
    honest direction, not a claim of extra precision.
    The graph method ignores recency weighting (all games
    in the window count equally) and the prior-season fade,
    so its SDs are an approximation to the blended rating's
    uncertainty, not its exact posterior -- the same honesty
    caveat as batch 3, now with both known terms included.
  * The blended rating's own fade weight (w = n/12 in
    ratings.blended_team_ratings) is NOT reused here: that fade
    is a point-estimate shrinkage rule, while this is a
    precision-weighted uncertainty over the same evidence. The
    two agree in direction (more games -> more trust in the
    current season) but are not the same formula, and neither
    claims to be the exact posterior of the iterative SRS solve
    (which has no closed form). "Bayesian-style" is the honest
    label; "Bayesian posterior" would overclaim.

Cover-probability intervals: the calibrated point probability
from calibration.py is mapped through the empirical CDF at the
shrunk margin shifted by +/-1 margin SD
(sqrt(sd_home^2 + sd_away^2)). No new distribution is invented
for this step -- the same CDF, evaluated at two shifted margins.
The interval therefore inherits the calibration's fat tails and
key-number clustering, and it always contains the point
probability (the CDF is monotone). A range that straddles 0.5
means the rating uncertainty alone is enough to flip which side
is favoured: treat that pick as a coin flip for standings-gamble
purposes, whatever the point estimate says.
"""
from __future__ import annotations

import math

from . import calibration, config


def _prior_sd() -> float:
    return float(getattr(config, "UNCERTAINTY_PRIOR_SD", 5.0))


def _game_sd() -> float:
    return float(getattr(config, "UNCERTAINTY_GAME_MARGIN_SD", 13.43))


def effective_sample_size(games_played: int) -> float:
    """Recency-weighted effective game count.

    Weights are the SRS recency weights (ratings._recency_weight):
    the most recent game has weight 1, older games decay with
    half-life config.RECENT_HALF_LIFE_GAMES. ESS = (sum w)^2 /
    (sum w^2); it is 0 at 0 games, 1 at 1 game, and saturates
    below the raw game count as games accumulate.
    """
    n = int(games_played)
    if n <= 0:
        return 0.0
    half_life = float(config.RECENT_HALF_LIFE_GAMES)
    weights = [0.5 ** (k / half_life) for k in range(n)]
    sum_w = sum(weights)
    sum_w2 = sum(w * w for w in weights)
    if sum_w2 <= 0:
        return 0.0
    return sum_w * sum_w / sum_w2


def rating_sd(games_played: int) -> float:
    """SD (points) of a team's rating given its current-season
    game count. Precision-weighted prior + recency-weighted data:
    1/var = 1/prior_var + ESS/game_var. Returns exactly the prior
    sd at 0 games and decreases monotonically with games played.
    """
    prior_var = _prior_sd() ** 2
    game_var = _game_sd() ** 2
    ess = effective_sample_size(games_played)
    if ess <= 0:
        return _prior_sd()
    var = 1.0 / (1.0 / prior_var + ess / game_var)
    return math.sqrt(var)


def rating_interval(rating: float, games_played: int,
                    z: float | None = None) -> tuple[float, float]:
    """Two-sided rating band: rating +/- z * rating_sd.

    Default z is config.UNCERTAINTY_Z90 (1.645, a 90% band under
    the Normal model above). Pass z=1.0 for a +/-1 SD band. The
    band is model uncertainty, not a guarantee: the true rating
    can sit outside it, and the Normal shape itself is an
    approximation. Rounded to 1 decimal, matching rating display.
    """
    if z is None:
        z = float(getattr(config, "UNCERTAINTY_Z90", 1.645))
    sd = rating_sd(games_played)
    return (round(float(rating) - z * sd, 1), round(float(rating) + z * sd, 1))


def margin_sd(games_home: int, games_away: int) -> float:
    """SD (points) of a home-minus-away rating margin, ignoring
    rating covariance (see module docstring: that omission makes
    this wide, not tight) and HFA uncertainty."""
    return math.sqrt(rating_sd(games_home) ** 2 + rating_sd(games_away) ** 2)


def cover_prob_interval(model_margin: float, line: float,
                        games_home: int, games_away: int) -> tuple[float, float]:
    """(lo, hi) cover probability for the model side, under a
    +/-1 margin-SD shift of the model margin, through the same
    calibrated empirical CDF as the point probability.

    model_margin and line are in nflverse convention (positive =
    home favoured), matching calibration.py. The interval always
    contains calibration.cover_probability_for_pick(model_margin,
    line) and always has lo <= hi. At 0 games on both sides the
    band is at its widest; it narrows as the season progresses
    because rating_sd() narrows. This is a LABEL: it never
    changes the pick, the point probability, or any standings
    label on its own -- it tells the reviewer how much weight
    the point probability can bear.
    """
    sd = margin_sd(games_home, games_away)
    home_hi = calibration.cover_probability_home(float(model_margin) + sd, float(line))
    home_lo = calibration.cover_probability_home(float(model_margin) - sd, float(line))
    if float(model_margin) >= float(line):
        # Model side is home (at equality the point prob is 0.5
        # and the home mapping still straddles it honestly).
        lo, hi = home_lo, home_hi
    else:
        # Model side is away: pick prob = 1 - P(home covers),
        # which falls as the margin rises, so the endpoints swap.
        lo, hi = round(1.0 - home_hi, 3), round(1.0 - home_lo, 3)
    return (min(lo, hi), max(lo, hi))


def summarize_team(rating: float, games_played: int) -> dict:
    """One team's rating with its uncertainty, for dashboard
    display. `band_90` is the 90% interval; `sd` is the underlying
    standard deviation in points."""
    lo, hi = rating_interval(rating, games_played)
    return {
        "rating": round(float(rating), 1),
        "games_played": int(games_played),
        "sd": round(rating_sd(games_played), 2),
        "band_90": (lo, hi),
        "note": ("Model uncertainty only, not a guarantee: the "
                 "band reflects sample size, prior, and recency "
                 "weighting (see uncertainty.py), not a promise "
                 "that the true rating lies inside it."),
    }


# ---------------------------------------------------------------------------
# Batch 4 group C, item 7 -- round 2: covariance + HFA uncertainty.
# Batch-3 functions above are unchanged. Everything below is
# labels/display only: config.UNCERTAINTY_PICK_USE stays False
# and no pick logic imports this module (see test_batch3_items67).
# ---------------------------------------------------------------------------

def _hfa_sd() -> float:
    return float(getattr(config, "UNCERTAINTY_HFA_SD", 1.0))


def _invert_matrix(mat: list[list[float]]) -> list[list[float]] | None:
    """Gauss-Jordan inverse with partial pivoting (stdlib only;
    the matrix is at most 32x32 for an NFL season). Returns None
    if the matrix is singular to working precision, so callers
    fall back to the independent formula instead of crashing."""
    n = len(mat)
    if n == 0:
        return []
    aug = [list(map(float, mat[i])) + [1.0 if j == i else 0.0 for j in range(n)]
           for i in range(n)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(aug[r][col]))
        if abs(aug[piv][col]) < 1e-12:
            return None
        aug[col], aug[piv] = aug[piv], aug[col]
        div = aug[col][col]
        aug[col] = [v / div for v in aug[col]]
        for r in range(n):
            if r != col and aug[r][col] != 0.0:
                factor = aug[r][col]
                aug[r] = [v - factor * w for v, w in zip(aug[r], aug[col])]
    return [row[n:] for row in aug]


def rating_covariance_matrix(teams: list[str],
                             game_pairs: list[tuple[str, str]],
                             prior_sd: float | None = None,
                             game_sd: float | None = None) -> dict:
    """Posterior covariance of team ratings from a game graph.

    teams: node list (order defines the matrix rows/cols).
    game_pairs: (home, away) for every game in the rating window
    -- point-in-time discipline is the CALLER's job (season S
    weeks before W plus the full season S-1, as in
    window_pairs() below); a pair listed twice counts twice.
    Returns {"teams": teams, "cov": matrix, "var": {team: var},
    "sd": {team: sd}} where cov[i][j] is Cov(r_i, r_j) in pts^2.
    A team with no games gets exactly prior_var and zero
    covariance with every other team (its row/col is the prior
    alone, since P has no off-diagonal entries for it).
    If the precision matrix is singular (cannot happen with a
    positive prior precision, but guarded anyway), cov is None
    and callers fall back to rating_sd() / covariance 0.
    """
    if prior_sd is None:
        prior_sd = _prior_sd()
    if game_sd is None:
        game_sd = _game_sd()
    prior_var = float(prior_sd) ** 2
    game_var = float(game_sd) ** 2
    idx = {t: i for i, t in enumerate(teams)}
    n = len(teams)
    prec = [[0.0] * n for _ in range(n)]
    for i in range(n):
        prec[i][i] = 1.0 / prior_var
    for home, away in game_pairs:
        if home not in idx or away not in idx or home == away:
            continue
        ih, ia = idx[home], idx[away]
        prec[ih][ih] += 1.0 / game_var
        prec[ia][ia] += 1.0 / game_var
        prec[ih][ia] -= 1.0 / game_var
        prec[ia][ih] -= 1.0 / game_var
    cov = _invert_matrix(prec)
    if cov is None:
        return {"teams": list(teams), "cov": None, "var": {}, "sd": {}}
    return {"teams": list(teams), "cov": cov,
            "var": {t: cov[idx[t]][idx[t]] for t in teams},
            "sd": {t: math.sqrt(max(cov[idx[t]][idx[t]], 0.0)) for t in teams}}


def covariance_between(cov_result: dict, team_a: str, team_b: str) -> float:
    """Cov(r_a, r_b) from a rating_covariance_matrix() result;
    0.0 if either team is absent or the matrix is None (the
    independent fallback -- a missing team is prior-only, and
    a prior-only rating is uncorrelated with everything else)."""
    cov = cov_result.get("cov")
    teams = cov_result.get("teams", [])
    if cov is None or team_a not in teams or team_b not in teams:
        return 0.0
    idx = {t: i for i, t in enumerate(teams)}
    return float(cov[idx[team_a]][idx[team_b]])


def margin_sd_cov(games_home: int, games_away: int, covariance: float = 0.0,
                  hfa_sd: float | None = None) -> float:
    """Margin SD with both round-2 terms: sqrt(sd_h^2 + sd_a^2
    - 2*covariance + hfa_sd^2). With covariance=0 and
    hfa_sd=0 this reproduces margin_sd() exactly; with the
    defaults (covariance as computed from the graph, hfa_sd =
    config.UNCERTAINTY_HFA_SD) it is the honest band. The
    max(..., 0.0) guard only fires for an impossible covariance
    (cov > (var_h + var_a + hfa^2)/2, e.g. a caller-supplied
    value from a different scale) -- a graph covariance can
    never trip it, since the inverse precision matrix is
    positive definite. Pass hfa_sd=0.0 for neutral-site games.
    """
    if hfa_sd is None:
        hfa_sd = _hfa_sd()
    var = (rating_sd(games_home) ** 2 + rating_sd(games_away) ** 2
           - 2.0 * float(covariance) + float(hfa_sd) ** 2)
    return math.sqrt(max(var, 0.0))


def margin_interval(model_margin: float, games_home: int, games_away: int,
                    covariance: float = 0.0, hfa_sd: float | None = None,
                    z: float | None = None) -> tuple[float, float]:
    """Two-sided margin band: model_margin +/- z * margin_sd_cov.
    Default z is config.UNCERTAINTY_Z90 (1.645). This is a band
    for the RATING-implied margin, not for the actual game
    margin (which also carries ~13.43 pts of game noise) -- it
    answers "could rating error alone move this margin across
    the line?", which is the Thursday-review question. Rounded
    to 1 decimal, matching rating display."""
    if z is None:
        z = float(getattr(config, "UNCERTAINTY_Z90", 1.645))
    sd = margin_sd_cov(games_home, games_away, covariance, hfa_sd)
    return (round(float(model_margin) - z * sd, 1),
            round(float(model_margin) + z * sd, 1))


def cover_prob_interval_cov(model_margin: float, line: float,
                            games_home: int, games_away: int,
                            covariance: float = 0.0,
                            hfa_sd: float | None = None) -> tuple[float, float]:
    """cover_prob_interval() with the round-2 margin SD: the
    calibrated point probability mapped through the same
    empirical CDF at the shrunk margin shifted by +/-1
    margin_sd_cov. Same endpoint-swap logic as the batch-3
    function (the away side's interval inverts), same
    guarantees (contains the point probability, lo <= hi).
    A range straddling 0.5 means rating uncertainty alone
    (covariance + HFA included) is enough to flip which side
    is favoured: treat that pick as a coin flip, whatever the
    point estimate says. Label only -- it never changes a pick.
    """
    sd = margin_sd_cov(games_home, games_away, covariance, hfa_sd)
    home_hi = calibration.cover_probability_home(float(model_margin) + sd, float(line))
    home_lo = calibration.cover_probability_home(float(model_margin) - sd, float(line))
    if float(model_margin) >= float(line):
        lo, hi = home_lo, home_hi
    else:
        lo, hi = round(1.0 - home_hi, 3), round(1.0 - home_lo, 3)
    return (min(lo, hi), max(lo, hi))


def window_pairs(games, season: int, week: int) -> list[tuple[str, str]]:
    """(home, away) pairs in the rating window for season/week:
    season-S games with week < W plus the FULL season S-1.
    games is a backtest.load_games_frame-style frame (columns
    season/week/home_team/away_team, REG rows only when a
    game_type column is present). Point-in-time by
    construction: no game from week W or later enters, so a
    covariance computed from this window cannot leak the
    game being predicted. Returns [] for a frame without the
    needed columns, rather than guessing."""
    if games is None or getattr(games, "empty", True):
        return []
    needed = ("season", "week", "home_team", "away_team")
    if not all(c in games.columns for c in needed):
        return []
    df = games
    if "game_type" in df.columns:
        df = df[df["game_type"].astype(str).str.upper() == "REG"]
    cur = df[(df["season"] == season) & (df["week"] < week)]
    prior = df[df["season"] == season - 1]
    # itertuples() field access uses the column names; both
    # frames share them, so the two lists concatenate directly.
    return ([(str(r.home_team), str(r.away_team))
             for r in cur.itertuples(index=False)]
            + [(str(r.home_team), str(r.away_team))
               for r in prior.itertuples(index=False)])
