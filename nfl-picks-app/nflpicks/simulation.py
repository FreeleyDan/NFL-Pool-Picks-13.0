"""
Season simulation (batch 3, item 2): Monte Carlo over the remaining
schedule, from CALIBRATED cover probabilities.

Why this exists: the standings rule (standings.py) suggests gambles,
but "cheap" is a claim about season odds, not just about one game's
expected wins. This module answers: given the remaining schedule,
what are my expected season wins, what is my probability of finishing
first, and how do this week's gambles move those numbers? It never
changes a pick -- compare_gambles() returns the odds delta and a
CHEAP / NEUTRAL / EXPENSIVE label for the caller to display next to
the GAMBLE labels from standings.py.

The one modelling choice that matters: games are simulated, not
picks. Each remaining game gets ONE draw (home covers / push / away
covers), and the user's and leader's results are both derived from
that same draw via their chosen sides. Simulating the two slates
independently would be wrong in both directions: when both pick the
same side their results are perfectly correlated (mirroring cannot
change the gap), and when they pick opposite sides their results are
perfectly anti-correlated (a flipped game is +1 to one slate and -1
to the other, or a push to both). Independent draws would invent
variance for mirrored slates and hide it for contrarian ones.

Inputs -- games: a list of dicts, one per remaining game:
  {"p_home": 0.58, "user_side": "home", "leader_side": "away"}
where p_home is the CALIBRATED P(home covers | decided) (e.g. from
calibration.cover_probability_home), user_side / leader_side are
"home"/"away", and leader_side may be None (leader has no pick on
that game / leader's slate is unknown -- then the leader's final
total stays at leader_wins and P(first) is vs that fixed total).
user_wins / leader_wins are season wins already banked.

Push handling: calibrated p is P(win | decided) -- the calibration
CDF treats a margin exactly equal to the line as neither a cover nor
a loss for the side (harness ATS% likewise excludes pushes). So each
game's draw is: push with probability config.SIM_PUSH_PROB (0.023,
the 43/1,871 harness push rate), otherwise home covers with
probability p_home. Effective win prob per game is therefore
p * (1 - push_prob), not p. If your pool scores a push as half a win,
this module overstates push cost slightly -- the push mass is 2.3%,
so the error is at most ~0.01 wins per game, and it is the same for
both slates in a comparison, so gamble deltas are barely affected.

Reproducibility: random.Random(seed) from the stdlib only, one
stream, draws in game order per trial. Same seed + same games (same
order) = byte-identical output; compare_gambles() deliberately uses
the SAME seed for baseline and gambled slates (common random numbers)
so the odds delta is a paired comparison, not two independent
Monte Carlo errors fighting each other. Default trials/seed are
config.SIM_TRIALS / config.SIM_SEED. Runtime is roughly linear in
trials x games (10,000 trials x 64 late-season games is well under
a second; a full 272-game schedule is a few seconds).
"""
from __future__ import annotations

import random

from . import config


def _params(trials: int | None, seed: int | None, push_prob: float | None) -> tuple[int, int, float]:
    trials = int(config.SIM_TRIALS) if trials is None else int(trials)
    seed = int(config.SIM_SEED) if seed is None else int(seed)
    push = float(config.SIM_PUSH_PROB) if push_prob is None else float(push_prob)
    if trials <= 0:
        raise ValueError("trials must be positive")
    if not 0.0 <= push < 1.0:
        raise ValueError(f"push_prob must be in [0, 1), got {push}")
    return trials, seed, push


def _validate_game(game: dict) -> tuple[float, str, str | None]:
    p = float(game["p_home"])
    if not 0.0 <= p <= 1.0:
        raise ValueError(f"p_home must be in [0, 1], got {p}")
    user_side = game.get("user_side")
    if user_side not in ("home", "away"):
        raise ValueError(f"user_side must be 'home'/'away', got {user_side!r}")
    leader_side = game.get("leader_side")
    if leader_side is not None and leader_side not in ("home", "away"):
        raise ValueError(f"leader_side must be 'home'/'away'/None, got {leader_side!r}")
    return p, user_side, leader_side


def _percentile(sorted_vals: list[float], pct: float) -> float:
    if not sorted_vals:
        return 0.0
    idx = min(len(sorted_vals) - 1, max(0, int(pct / 100.0 * len(sorted_vals))))
    return float(sorted_vals[idx])


def simulate_season(games: list[dict], user_wins: int, leader_wins: int,
                    trials: int | None = None, seed: int | None = None,
                    push_prob: float | None = None) -> dict:
    """Monte Carlo the rest of the season. Returns expected final wins
    for both slates, P(user finishes strictly first), P(tie),
    P(user finishes first-or-tied), and the user's final-wins
    distribution (p10 / p50 / p90). P(first) is strict: a tie is not
    a win, because the pool's tie-break / split rule is unknown --
    p_first_or_tied is reported alongside so a caller who knows ties
    split can read that instead.
    """
    if user_wins < 0 or leader_wins < 0:
        raise ValueError("user_wins and leader_wins must be non-negative")
    trials, seed, push = _params(trials, seed, push_prob)
    parsed = [_validate_game(g) for g in games]
    rng = random.Random(seed)
    user_finals: list[int] = []
    first = tied = 0
    user_total = leader_total = 0
    for _ in range(trials):
        uw, lw = int(user_wins), int(leader_wins)
        for p_home, user_side, leader_side in parsed:
            r = rng.random()
            if r < push:
                continue  # push: neither slate wins
            home_covers = r < push + (1.0 - push) * p_home
            if (home_covers and user_side == "home") or (not home_covers and user_side == "away"):
                uw += 1
            if leader_side is not None and (
                    (home_covers and leader_side == "home") or (not home_covers and leader_side == "away")):
                lw += 1
        user_finals.append(uw)
        user_total += uw
        leader_total += lw
        if uw > lw:
            first += 1
        elif uw == lw:
            tied += 1
    user_finals.sort()
    return {
        "trials": trials, "seed": seed, "push_prob": push,
        "expected_user_wins": round(user_total / trials, 3),
        "expected_leader_wins": round(leader_total / trials, 3),
        "p_first": round(first / trials, 4),
        "p_tied": round(tied / trials, 4),
        "p_first_or_tied": round((first + tied) / trials, 4),
        "user_wins_p10": _percentile(user_finals, 10),
        "user_wins_p50": _percentile(user_finals, 50),
        "user_wins_p90": _percentile(user_finals, 90),
    }


def classify_gamble(delta_p_first_pp: float, expected_cost_wins: float) -> str:
    """Label a week's gambles by season-odds effect. CHEAP: the gambles
    cost at most SIM_CHEAP_COST_MAX_WINS expected wins and do not
    reduce P(first). EXPENSIVE: they cost at least
    SIM_EXPENSIVE_COST_MIN_WINS expected wins, or they cut P(first) by
    at least SIM_EXPENSIVE_P_FIRST_DROP_PP points (a leading-slate
    gamble does the latter even when its win cost is small -- that is
    the point: when leading, odds are what you are spending, not
    wins). Anything between is NEUTRAL. expected_cost_wins is
    baseline_expected_wins - gambled_expected_wins (positive = the
    gambles cost wins); a negative cost (flips that gain expected
    wins, from sub-0.5 default picks) is cheap by construction.
    """
    cheap_max = float(config.SIM_CHEAP_COST_MAX_WINS)
    expensive_min = float(config.SIM_EXPENSIVE_COST_MIN_WINS)
    drop_pp = float(config.SIM_EXPENSIVE_P_FIRST_DROP_PP)
    if expected_cost_wins <= cheap_max and delta_p_first_pp >= 0.0:
        return "CHEAP"
    if expected_cost_wins >= expensive_min or delta_p_first_pp <= -drop_pp:
        return "EXPENSIVE"
    return "NEUTRAL"


def compare_gambles(baseline_games: list[dict], gambled_games: list[dict],
                    user_wins: int, leader_wins: int,
                    trials: int | None = None, seed: int | None = None,
                    push_prob: float | None = None) -> dict:
    """How a week's gambles move season odds. baseline_games and
    gambled_games are the SAME remaining schedule in the SAME order,
    differing only in user_side on the gambled games (leader_side
    unchanged -- the leader does not react to your gambles). Both
    simulations use the same seed, so the delta is paired: the same
    underlying game draws decide both slates, and only the flipped
    games can move the comparison. Returns both simulations, the
    deltas (p_first in percentage points, expected wins in wins),
    and the CHEAP / NEUTRAL / EXPENSIVE label from classify_gamble().
    """
    if len(baseline_games) != len(gambled_games):
        raise ValueError("baseline and gambled schedules must have the same games in the same order")
    base = simulate_season(baseline_games, user_wins, leader_wins, trials, seed, push_prob)
    gamb = simulate_season(gambled_games, user_wins, leader_wins, trials, seed, push_prob)
    delta_p_first_pp = round((gamb["p_first"] - base["p_first"]) * 100, 2)
    expected_cost = round(base["expected_user_wins"] - gamb["expected_user_wins"], 3)
    return {
        "baseline": base, "gambled": gamb,
        "delta_p_first_pp": delta_p_first_pp,
        "delta_expected_user_wins": round(gamb["expected_user_wins"] - base["expected_user_wins"], 3),
        "expected_cost_wins": expected_cost,
        "label": classify_gamble(delta_p_first_pp, expected_cost),
        "note": ("Same seed for both slates (paired draws): only flipped "
                 "games can move the delta. A label is not a pick change."),
    }
