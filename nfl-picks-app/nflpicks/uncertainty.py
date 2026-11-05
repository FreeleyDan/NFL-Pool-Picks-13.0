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

What is deliberately left out, and the direction of the error:
  * Home-field uncertainty: the margin adds HFA as a constant.
    Ignoring its error understates margin variance slightly.
  * Covariance between the two teams' ratings: SRS solves all
    teams jointly from shared games, so rating errors are not
    independent. Ignoring positive correlation OVERSTATES the
    margin variance (Var(a-b) < Var(a)+Var(b) when a,b are
    positively correlated). The two omissions push in opposite
    directions; the covariance one is the larger effect, so the
    margin bands here are best read as wide, not tight.
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
