"""
Situational spots (batch 5, group A item 1) -- tested, NOT wired live.

The question: do schedule-only situations -- a big game next week, a
huge win last week, a favourite sandwiched between two strong
opponents, a favourite on short rest against a rested dog -- carry a
real ATS signal the SRS model misses? Everything here is testable
from the schedule + prior results alone: no new data source, no
injury file, no play-by-play.

Point-in-time discipline. A spot is computed for a target game from
only (a) the season's schedule (which opponent is next / was last --
the schedule is known in advance, so using a future week's OPPONENT
identity is not a leak), (b) results of games already played, and
(c) the pre-week blended SRS ratings (the same ratings the pick
uses). An opponent's STRENGTH rank is always the pre-week rating,
never a full-season rating, so a next week's opponent is ranked on
what was known before the target game.

Spots (all defined on the team the model PICKS -- the spot is a claim
about the picked side, so the fade test flips exactly those picks):

  lookahead_div     picked team's NEXT game is against a division
                    rival (div_game == 1 on the next scheduled game).
  lookahead_strong  picked team's NEXT opponent is top-N by pre-week
                    blended SRS rating (N = config, default 5).
  letdown_upset     picked team WON its previous game outright while
                    an underdog on the line (line against them, won
                    straight up). Week 1 has no previous game: no spot.
  letdown_blowout   picked team won its previous game by >= N points
                    (N = config, default 14).
  sandwich          picked team is the FAVOURITE, and BOTH its previous
                    opponent and its next opponent are top-N by
                    pre-week rating (N = config, default 8) -- a
                    favourite looking past a soft middle game.
  shortweek_fav     picked team is the FAVOURITE and has >= N fewer
                    rest days than its opponent (N = config, default
                    3; rest = the games.csv home_rest/away_rest
                    columns, exact days since the last game).

Two pre-registered forms are tested per spot (12 tests total, one
Bonferroni family):

  fade   flip the pick on spot games, but ONLY if the spot's TRAINING
         (2019-2022) ATS% on the model's side is below 50% with at
         least SITUATIONAL_MIN_TRAIN_DECIDED decided games; otherwise
         the spot is followed (no change). The rule is frozen on train
         and applied to TEST (2023-2025) -- a training-only fade that
         evaporates out-of-sample fails the gate, by design.
  add    shift the model margin by the spot's TRAINING mean residual
         (actual - model, home-margin units), clipped to +/- the
         config cap, and re-grade. Same train/test split.

Promotion gate (ALL must hold): the spot wins on the TEST split
(paired delta > 0 vs always-follow on the same test games) AND its
full-sample paired p-value survives Bonferroni across all 12 tests
(adjusted p < 0.05) AND it beats the placebo gate (see below).
Nothing passed; see README batch 5 group A.

Placebo gate. Two placebos, because the two forms have different
nulls:
  add-form: backtest.placebo_rows() -- the baseline's own margins
         re-graded with a random +/-0.15 pt adjustment per game, 20
         seeds. A real additive adjustment must beat the 95th
         percentile of that distribution, not just the baseline.
  fade-form: random flips -- the same NUMBER of picks the rule flips,
         chosen at random (200 draws, seeded), re-graded. A real fade
         must beat that distribution's 95th percentile. Randomly
         flipping ~50-150 picks moves ATS by chance alone; the gate
         prices that in.

Conventions: all lines/margins are nflverse (positive = home
favoured). Grading uses the batch-4 corrected push rule throughout
(margin == line, or actual == line, is a push, not a wrong pick).
The replay in this module reproduces the backtest baseline EXACTLY
(50.03% ATS, 898/1,795, MAE 10.34 on 2019-2025); if it ever does
not, the spot numbers built on it are not to be trusted.
"""
from __future__ import annotations

import math
import random
from statistics import NormalDist

import pandas as pd

from . import backtest as backtest_mod, config, model as model_mod, ratings as ratings_mod

_NORM = NormalDist()

SPOTS = ("lookahead_div", "lookahead_strong", "letdown_upset",
         "letdown_blowout", "sandwich", "shortweek_fav")

TRAIN_SEASONS = (2019, 2020, 2021, 2022)
TEST_SEASONS = (2023, 2024, 2025)


def _grade(margin: float, line: float, actual: float) -> str:
    """'correct' / 'wrong' / 'push' under the corrected push rule."""
    if margin == line or actual == line:
        return "push"
    return "correct" if (actual > line) == (margin > line) else "wrong"


def _summ(outcomes: list[str]) -> dict:
    dec = [o for o in outcomes if o != "push"]
    return {"n": len(outcomes), "decided": len(dec),
            "pushes": len(outcomes) - len(dec),
            "correct": sum(1 for o in dec if o == "correct"),
            "ats_pct": round(100 * sum(1 for o in dec if o == "correct") / len(dec), 2)
            if dec else None}


def _paired(base_outcomes: list[str], var_outcomes: list[str]) -> dict:
    """Paired comparison on mutually-decided games only (a game that
    is a push under either rule cannot manufacture a paired win --
    the tie-artifact guard from batch 4)."""
    pairs = [(b, v) for b, v in zip(base_outcomes, var_outcomes)
             if b != "push" and v != "push"]
    b = sum(1 for bb, vv in pairs if vv == "correct" and bb != "correct")
    c = sum(1 for bb, vv in pairs if bb == "correct" and vv != "correct")
    n = len(pairs)
    z = (b - c) / math.sqrt(b + c) if (b + c) else 0.0
    p = 2 * (1 - _NORM.cdf(abs(z))) if (b + c) else 1.0
    return {"n": n, "variant_right_base_wrong": b, "base_right_variant_wrong": c,
            "paired_delta_pp": round(100 * (b - c) / n, 2) if n else 0.0,
            "paired_z": round(z, 2), "p_two_sided": round(p, 4)}


def _is_div(value) -> bool:
    return model_mod._is_div_game(value)


def _team_won_outright(row: pd.Series, team: str) -> bool:
    """Did `team` win this (already played) game straight up?"""
    actual = float(row["home_score"]) - float(row["away_score"])
    return actual > 0 if team == row["home_team"] else actual < 0


def _team_was_underdog(row: pd.Series, team: str) -> bool:
    """Was `team` an underdog on the nflverse line? Line > 0 favours
    the home team, so the home team is the dog when line < 0 and the
    away team is the dog when line > 0."""
    line = float(row["spread_line"])
    if team == row["home_team"]:
        return line < 0
    return line > 0


def _team_margin(row: pd.Series, team: str) -> float:
    actual = float(row["home_score"]) - float(row["away_score"])
    return actual if team == row["home_team"] else -actual


def _top_set(ratings: dict[str, float], n: int) -> set[str]:
    """Top-n teams by pre-week blended rating (ties broken by team
    code, so the set is deterministic)."""
    ranked = sorted(ratings.items(), key=lambda kv: (-kv[1], kv[0]))
    return {t for t, _ in ranked[:max(0, int(n))]}


def _prev_game(season_games: pd.DataFrame, team: str, week: int) -> pd.Series | None:
    """The team's most recent SAME-SEASON game before `week` (played
    only). None in week 1. The bye-week case is included: a team
    coming off a bye last played two weeks ago, which is still its
    most recent game -- the spot definitions do not require the
    previous game to be last week, only that it is the latest one."""
    prior = season_games[(season_games["week"] < week) & season_games["result"].notna()]
    mine = prior[(prior["home_team"] == team) | (prior["away_team"] == team)]
    if mine.empty:
        return None
    return mine.sort_values(["week", "gameday"], kind="stable").iloc[-1]


def _next_game(season_games: pd.DataFrame, team: str, week: int) -> pd.Series | None:
    """The team's next SAME-SEASON scheduled game after `week`.
    Schedule identity only (opponent, div_game) -- the game need not
    have been played yet at pick time, because the schedule is known
    in advance. None in the final week."""
    later = season_games[season_games["week"] > week]
    mine = later[(later["home_team"] == team) | (later["away_team"] == team)]
    if mine.empty:
        return None
    return mine.sort_values(["week", "gameday"], kind="stable").iloc[0]


def _opponent(row: pd.Series, team: str) -> str:
    return row["away_team"] if team == row["home_team"] else row["home_team"]


def team_spots(season_games: pd.DataFrame, team: str, week: int,
               ratings: dict[str, float], target_row: pd.Series) -> set[str]:
    """Spot set for one team in one target game, from schedule +
    prior results + pre-week ratings only. `target_row` supplies the
    rest columns and the line (for favourite status)."""
    spots: set[str] = set()
    prev_g = _prev_game(season_games, team, week)
    next_g = _next_game(season_games, team, week)
    top_strong = _top_set(ratings, int(getattr(config, "SITUATIONAL_LOOKAHEAD_TOP_N", 5)))
    top_sandwich = _top_set(ratings, int(getattr(config, "SITUATIONAL_SANDWICH_TOP_N", 8)))
    if next_g is not None:
        if _is_div(next_g.get("div_game")):
            spots.add("lookahead_div")
        if _opponent(next_g, team) in top_strong:
            spots.add("lookahead_strong")
    if prev_g is not None:
        if _team_won_outright(prev_g, team) and _team_was_underdog(prev_g, team):
            spots.add("letdown_upset")
        if _team_margin(prev_g, team) >= float(getattr(config, "SITUATIONAL_LETDOWN_BLOWOUT_PTS", 14)):
            spots.add("letdown_blowout")
    # Favourite-only spots need the target line and rest.
    try:
        line = float(target_row["spread_line"])
    except (KeyError, TypeError, ValueError):
        return spots
    is_fav = (team == target_row["home_team"] and line > 0) or \
             (team == target_row["away_team"] and line < 0)
    if not is_fav:
        return spots
    if prev_g is not None and next_g is not None:
        if _opponent(prev_g, team) in top_sandwich and _opponent(next_g, team) in top_sandwich:
            spots.add("sandwich")
    try:
        home_rest, away_rest = float(target_row["home_rest"]), float(target_row["away_rest"])
    except (KeyError, TypeError, ValueError):
        return spots
    own_rest = home_rest if team == target_row["home_team"] else away_rest
    opp_rest = away_rest if team == target_row["home_team"] else home_rest
    if own_rest - opp_rest <= -float(getattr(config, "SITUATIONAL_SHORTWEEK_DAYS", 3)):
        spots.add("shortweek_fav")
    return spots


def replay_with_context(games: pd.DataFrame, seasons: list[int],
                        context_fn=None) -> list[dict]:
    """Point-in-time replay of the baseline model over `seasons`,
    one row per played game with a recorded line, plus whatever extra
    columns `context_fn` returns per game.

    This is the same loop as backtest.evaluate_variant("baseline")
    (ratings from season-S games before week W plus the full season
    S-1; point-in-time empirical HFA; model.predict_game for the
    margin), so its baseline summary must reproduce the canonical
    numbers. `context_fn(season, week, row, ratings, games_played,
    hfa, season_games, played_current, prior_games)` is called per
    target game BEFORE that week's games are scored, so it sees
    exactly the pre-week information -- it must not read the target
    game's result, and for lookahead it may read the schedule (not
    results) of later weeks. Returns rows with the baseline fields
    (season, week, home_team, away_team, model_margin, spread_line,
    actual_margin, ats, abs_err) plus the context dict's keys.
    """
    rows: list[dict] = []
    for season in seasons:
        season_games = games[games["season"] == season]
        prior_games = games[(games["season"] == season - 1) & games["result"].notna()]
        weeks = sorted(season_games[season_games["result"].notna()]["week"].unique().tolist())
        for week in weeks:
            played_current = season_games[(season_games["week"] < week) & season_games["result"].notna()]
            hfa = ratings_mod.compute_empirical_hfa(played_current, prior_games)
            ratings_for_week, games_played = ratings_mod.blended_team_ratings(played_current, prior_games, hfa)
            targets = season_games[(season_games["week"] == week) & season_games["result"].notna()
                                   & season_games["spread_line"].notna()]
            for _, row in targets.iterrows():
                pred = model_mod.predict_game(row, ratings_for_week, games_played, hfa)
                model_margin = pred.margin_model
                actual = float(row["home_score"]) - float(row["away_score"])
                line = float(row["spread_line"])
                rec = {
                    "season": int(season), "week": int(week),
                    "home_team": row["home_team"], "away_team": row["away_team"],
                    "model_margin": round(model_margin, 3),
                    "spread_line": line, "actual_margin": actual,
                    "ats": _grade(model_margin, line, actual),
                    "abs_err": abs(actual - model_margin),
                }
                if context_fn is not None:
                    rec.update(context_fn(season, week, row, ratings_for_week,
                                          games_played, hfa, season_games,
                                          played_current, prior_games) or {})
                rows.append(rec)
    return rows


def _picked_team(row: dict) -> str:
    """The team the model picks (margin vs line, nflverse)."""
    return row["home_team"] if float(row["model_margin"]) > float(row["spread_line"]) else row["away_team"]


def _picked_spots(row: dict) -> set[str]:
    """Spot set of the picked team, from the context columns
    `spots_home` / `spots_away` (stored as frozensets by the
    evaluator)."""
    picked = _picked_team(row)
    key = "spots_home" if picked == row["home_team"] else "spots_away"
    return set(row.get(key) or ())


def describe_spots(rows: list[dict]) -> dict:
    """Descriptive ATS% on the model's side for each spot (picked
    team in the spot). Description only -- a spot that looks bad
    here must still win the walk-forward gate to matter."""
    out: dict[str, dict] = {}
    for spot in SPOTS:
        outcomes = [_grade(r["model_margin"], r["spread_line"], r["actual_margin"])
                    for r in rows if spot in _picked_spots(r)]
        s = _summ(outcomes)
        s["spot"] = spot
        out[spot] = s
    base = _summ([_grade(r["model_margin"], r["spread_line"], r["actual_margin"]) for r in rows])
    return {"baseline": base, "by_spot": out}


def learn_spot_rules(train_rows: list[dict], min_decided: int | None = None) -> dict:
    """Fade/follow rule per spot, learned on TRAIN rows only: fade a
    spot when the model's side went below 50% ATS there with at
    least min_decided decided games, else follow. Spots that do not
    clear the floor default to follow -- flipping a pick on a
    handful of games is chasing noise."""
    if min_decided is None:
        min_decided = int(getattr(config, "SITUATIONAL_MIN_TRAIN_DECIDED", 30))
    rule: dict[str, str] = {}
    for spot in SPOTS:
        outcomes = [_grade(r["model_margin"], r["spread_line"], r["actual_margin"])
                    for r in train_rows if spot in _picked_spots(r)]
        s = _summ(outcomes)
        rule[spot] = "fade" if (s["decided"] >= min_decided and s["ats_pct"] is not None
                                and s["ats_pct"] < 50) else "follow"
    return rule


def apply_spot_rule(rows: list[dict], rule: dict) -> dict:
    """Grade a learned fade/follow rule on rows (usually the TEST
    split), with the always-follow baseline on the SAME rows and the
    paired comparison. A faded pick is the opposite side: pick margin
    = line - (model - line), preserving |deviation| and flipping
    only the side. A game is faded if the picked team is in ANY
    faded spot (spots overlap; the rule map is reported so the
    overlap is visible)."""
    base_outcomes, var_outcomes = [], []
    n_flipped = 0
    base_errs, var_errs = [], []
    for r in rows:
        m, line, actual = float(r["model_margin"]), float(r["spread_line"]), float(r["actual_margin"])
        spots = _picked_spots(r)
        fade = any(rule.get(s) == "fade" for s in spots)
        var_margin = line - (m - line) if fade else m
        if fade:
            n_flipped += 1
        base_outcomes.append(_grade(m, line, actual))
        var_outcomes.append(_grade(var_margin, line, actual))
        base_errs.append(abs(actual - m))
        var_errs.append(abs(actual - var_margin))
    return {"rule": _summ(var_outcomes), "always_follow": _summ(base_outcomes),
            "paired_vs_follow": _paired(base_outcomes, var_outcomes),
            "mae_rule": round(sum(var_errs) / len(var_errs), 3) if var_errs else None,
            "mae_follow": round(sum(base_errs) / len(base_errs), 3) if base_errs else None,
            "n_flipped": n_flipped, "rule_map": dict(rule)}


def learn_spot_adjustments(train_rows: list[dict]) -> dict:
    """Per-spot additive adjustment, learned on TRAIN rows only: the
    mean residual (actual - model, home-margin units) on spot games,
    clipped to +/- SITUATIONAL_SPOT_ADJ_CAP. A spot whose games the
    model systematically overrates gets a negative shift, and vice
    versa. The adjustment is a single number per spot, fit on train
    -- applying it to test is the out-of-sample check. Spots with
    fewer than MIN_TRAIN_DECIDED decided train games get 0.0 (no
    adjustment on a handful of games)."""
    cap = float(getattr(config, "SITUATIONAL_SPOT_ADJ_CAP", 3.0))
    min_decided = int(getattr(config, "SITUATIONAL_MIN_TRAIN_DECIDED", 30))
    adj: dict[str, float] = {}
    for spot in SPOTS:
        sub = [r for r in train_rows if spot in _picked_spots(r)
               and r["actual_margin"] != r["spread_line"]]
        if len(sub) < min_decided:
            adj[spot] = 0.0
            continue
        resid = [float(r["actual_margin"]) - float(r["model_margin"]) for r in sub]
        adj[spot] = round(max(-cap, min(cap, sum(resid) / len(resid))), 3)
    return adj


def apply_spot_adjustments(rows: list[dict], adjustments: dict) -> dict:
    """Grade per-spot additive adjustments on rows (usually TEST):
    a game whose picked team is in a spot gets its model margin
    shifted by that spot's adjustment (if several spots apply, the
    adjustments SUM -- spots overlap, and the sum is the honest
    reading of the additive form, not a cherry-picked single spot).
    Returns the adjusted summary, the always-follow baseline on the
    same rows, and the paired comparison."""
    base_outcomes, var_outcomes = [], []
    n_adjusted = 0
    base_errs, var_errs = [], []
    for r in rows:
        m, line, actual = float(r["model_margin"]), float(r["spread_line"]), float(r["actual_margin"])
        shift = sum(float(adjustments.get(s, 0.0)) for s in _picked_spots(r))
        if shift != 0.0:
            n_adjusted += 1
        base_outcomes.append(_grade(m, line, actual))
        var_outcomes.append(_grade(m + shift, line, actual))
        base_errs.append(abs(actual - m))
        var_errs.append(abs(actual - (m + shift)))
    return {"adjusted": _summ(var_outcomes), "always_follow": _summ(base_outcomes),
            "paired_vs_follow": _paired(base_outcomes, var_outcomes),
            "mae_adjusted": round(sum(var_errs) / len(var_errs), 3) if var_errs else None,
            "mae_follow": round(sum(base_errs) / len(base_errs), 3) if base_errs else None,
            "n_adjusted": n_adjusted, "adjustments": dict(adjustments)}


def placebo_flip_distribution(rows: list[dict], n_flips: int, trials: int = 200,
                              seed: int = 20261105) -> dict:
    """Null distribution for a fade rule that flips `n_flips` picks:
    choose that many rows at random (seeded), flip them, grade, and
    record the paired delta vs always-follow. A real fade must beat
    this distribution's 95th percentile -- randomly flipping picks
    moves ATS by chance alone, and the gate prices that in. Returns
    mean / sd / p95 / max of the paired delta (pp), plus the trial
    count. With n_flips == 0 the distribution is degenerate at 0."""
    if n_flips <= 0 or not rows:
        return {"trials": int(trials), "n_flips": int(n_flips),
                "mean_pp": 0.0, "sd_pp": 0.0, "p95_pp": 0.0, "max_pp": 0.0}
    rng = random.Random(seed)
    base = [_grade(r["model_margin"], r["spread_line"], r["actual_margin"]) for r in rows]
    deltas = []
    idx = list(range(len(rows)))
    for _ in range(trials):
        chosen = set(rng.sample(idx, min(n_flips, len(idx))))
        var = []
        for i, r in enumerate(rows):
            m, line, actual = float(r["model_margin"]), float(r["spread_line"]), float(r["actual_margin"])
            var.append(_grade(line - (m - line), line, actual) if i in chosen else base[i])
        p = _paired(base, var)
        deltas.append(p["paired_delta_pp"])
    deltas.sort()
    mean = sum(deltas) / len(deltas)
    sd = math.sqrt(sum((d - mean) ** 2 for d in deltas) / len(deltas))
    p95 = deltas[min(len(deltas) - 1, int(0.95 * len(deltas)))]
    return {"trials": int(trials), "n_flips": int(n_flips),
            "mean_pp": round(mean, 2), "sd_pp": round(sd, 2),
            "p95_pp": round(p95, 2), "max_pp": round(deltas[-1], 2)}


def placebo_additive_distribution(baseline_games_df, trials_seeds=None,
                                  magnitude: float = 0.15) -> dict:
    """Null distribution for an additive adjustment: reuse
    backtest.placebo_rows() (random +/-magnitude per game) over the
    given seeds and record each seed's paired delta vs the baseline.
    A real additive adjustment must beat this distribution's 95th
    percentile. `baseline_games_df` is the baseline _games frame
    (model_margin / spread_line / actual_margin / season / week /
    home_team / away_team)."""
    if trials_seeds is None:
        trials_seeds = list(range(int(getattr(config, "SITUATIONAL_PLACEBO_SEEDS", 20))))
    base_df = baseline_games_df
    deltas = []
    for seed in trials_seeds:
        placebo = backtest_mod.placebo_rows(base_df, seed, magnitude=magnitude)
        p = backtest_mod.paired_comparison(base_df, placebo)
        deltas.append(p["paired_delta_pp"])
    deltas.sort()
    mean = sum(deltas) / len(deltas) if deltas else 0.0
    sd = math.sqrt(sum((d - mean) ** 2 for d in deltas) / len(deltas)) if len(deltas) > 1 else 0.0
    p95 = deltas[min(len(deltas) - 1, int(0.95 * len(deltas)))] if deltas else 0.0
    return {"trials": len(deltas), "magnitude": magnitude,
            "mean_pp": round(mean, 2), "sd_pp": round(sd, 2),
            "p95_pp": round(p95, 2), "max_pp": round(deltas[-1], 2) if deltas else 0.0}


def evaluate_all(games: pd.DataFrame, seasons: list[int] | None = None,
                 train_seasons: tuple[int, ...] = TRAIN_SEASONS,
                 test_seasons: tuple[int, ...] = TEST_SEASONS) -> dict:
    """Full item-1 evaluation. Replays the baseline with spot
    context, then for each of the 6 spots: descriptive ATS, a
    train-learned fade rule applied to test, and a train-learned
    additive adjustment applied to test -- each with its paired
    comparison, Bonferroni-adjusted p (family = all 12 spot tests),
    and its placebo gate. Returns the baseline summary (which must
    reproduce 50.03% / 898 / 1,795 on 2019-2025), per-spot results,
    and `promoted_spots` (empty unless a spot clears every gate)."""
    if seasons is None:
        seasons = list(range(2019, 2026))
    games = backtest_mod.load_games_frame(games)

    def _ctx(season, week, row, ratings, games_played, hfa, season_games,
             played_current, prior_games):
        return {
            "spots_home": frozenset(team_spots(season_games, row["home_team"], week, ratings, row)),
            "spots_away": frozenset(team_spots(season_games, row["away_team"], week, ratings, row)),
        }

    rows = replay_with_context(games, seasons, context_fn=_ctx)
    base = _summ([r["ats"] for r in rows])
    desc = describe_spots(rows)
    train_rows = [r for r in rows if r["season"] in train_seasons]
    test_rows = [r for r in rows if r["season"] in test_seasons]
    rule = learn_spot_rules(train_rows)
    adjustments = learn_spot_adjustments(train_rows)

    # Placebo gates. The gate distribution is computed on the TEST
    # split, not the full sample: a paired delta on ~800 test games
    # has wider chance variation than one on 1,871 games, so a
    # full-sample placebo p95 would be too lenient a gate for a
    # test-split win. The full-sample placebo is reported alongside
    # for reference.
    import pandas as _pd

    def _frame(sub_rows):
        return _pd.DataFrame([{"season": r["season"], "week": r["week"],
                               "home_team": r["home_team"], "away_team": r["away_team"],
                               "model_margin": r["model_margin"], "spread_line": r["spread_line"],
                               "actual_margin": r["actual_margin"],
                               "ats": r["ats"]} for r in sub_rows])

    placebo_add = placebo_additive_distribution(_frame(rows))
    placebo_add_test = placebo_additive_distribution(_frame(test_rows))

    n_tests = 2 * len(SPOTS)  # 6 spots x 2 forms
    per_spot: dict[str, dict] = {}
    promoted: list[str] = []
    for spot in SPOTS:
        # --- fade form: rule learned on train, applied to test ---
        test_rule = {s: (rule[s] if s == spot else "follow") for s in SPOTS}
        fade_test = apply_spot_rule(test_rows, test_rule)
        fade_full = apply_spot_rule(rows, test_rule)
        p_raw = fade_full["paired_vs_follow"]["p_two_sided"]
        p_adj = min(1.0, p_raw * n_tests)
        flip_placebo = placebo_flip_distribution(
            test_rows, fade_test["n_flipped"],
            trials=int(getattr(config, "SITUATIONAL_PLACEBO_FLIP_TRIALS", 200)))
        fade_gate = bool(
            fade_test["paired_vs_follow"]["paired_delta_pp"] > 0
            and fade_test["paired_vs_follow"]["paired_delta_pp"] > flip_placebo["p95_pp"]
            and p_adj < 0.05)
        # --- additive form: adjustment learned on train, applied to test ---
        test_adj = {s: (adjustments[s] if s == spot else 0.0) for s in SPOTS}
        add_test = apply_spot_adjustments(test_rows, test_adj)
        add_full = apply_spot_adjustments(rows, test_adj)
        p_raw_add = add_full["paired_vs_follow"]["p_two_sided"]
        p_adj_add = min(1.0, p_raw_add * n_tests)
        add_gate = bool(
            add_test["paired_vs_follow"]["paired_delta_pp"] > 0
            and add_test["paired_vs_follow"]["paired_delta_pp"] > placebo_add_test["p95_pp"]
            and p_adj_add < 0.05)
        if fade_gate:
            promoted.append(f"{spot}:fade")
        if add_gate:
            promoted.append(f"{spot}:add")
        per_spot[spot] = {
            "descriptive": desc["by_spot"][spot],
            "train_ats_pct": _summ([_grade(r["model_margin"], r["spread_line"], r["actual_margin"])
                                    for r in train_rows if spot in _picked_spots(r)])["ats_pct"],
            "train_decided": _summ([_grade(r["model_margin"], r["spread_line"], r["actual_margin"])
                                    for r in train_rows if spot in _picked_spots(r)])["decided"],
            "test_ats_pct": _summ([_grade(r["model_margin"], r["spread_line"], r["actual_margin"])
                                   for r in test_rows if spot in _picked_spots(r)])["ats_pct"],
            "test_decided": _summ([_grade(r["model_margin"], r["spread_line"], r["actual_margin"])
                                   for r in test_rows if spot in _picked_spots(r)])["decided"],
            "fade": {"rule": rule[spot], "test": fade_test, "full": fade_full,
                     "p_bonferroni": round(p_adj, 4),
                     "placebo_flip_p95_pp": flip_placebo["p95_pp"],
                     "gate_pass": fade_gate},
            "add": {"adjustment": adjustments[spot], "test": add_test, "full": add_full,
                    "p_bonferroni": round(p_adj_add, 4),
                    "placebo_add_p95_pp": placebo_add_test["p95_pp"],
                    "gate_pass": add_gate},
        }
    return {
        "baseline": base,
        "n": len(rows),
        "train_seasons": list(train_seasons), "test_seasons": list(test_seasons),
        "bonferroni_family_n": n_tests,
        "placebo_additive": placebo_add,
        "placebo_additive_test": placebo_add_test,
        "rule_map": rule, "adjustments": adjustments,
        "per_spot": per_spot,
        "promoted_spots": promoted,
        "_rows": rows,
    }
