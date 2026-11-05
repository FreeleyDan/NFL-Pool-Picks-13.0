"""
Referee crews and coaching tendencies (batch 5, group B item 5) --
tested, NOT wired live.

The question: two people-effects the team ratings cannot see. Does
the REFEREE crew matter -- a crew that calls more penalties, or
whose games the home team covers more often than the line implies?
And does the HEAD COACH matter -- a coach whose teams cover more
often than their ratings imply, or who goes for it on 4th down?

What is actually testable from games.csv (the only file the
harness reads):

  referee  home-cover bias: the ref's PRIOR mean (actual - line)
           over games they officiated (home-margin units; positive
           = home covered more than the line in their games), and
           the ref's prior home ATS%. Penalty counts are NOT in
           games.csv -- a crew's penalty rate cannot be computed
           from it, so no penalty claim is made here. What IS
           tested is the outcome the pool scores: whether the
           ref's past home bias predicts the next game's ATS
           result. A totals version (prior mean actual total minus
           total_line) is reported as a secondary check.
  coach    prior mean residual in the coach's team's favour, and
           prior ATS%, both following the COACH (not the team)
           across teams and seasons. A coach's record conflates
           coach quality with team quality -- the SRS model already
           prices the team, so a coach signal would have to be
           orthogonal to ratings to survive walk-forward, and the
           gate is the arbiter.

What is NOT testable, stated plainly so it is not papered over:
4th-down aggressiveness. It needs play-by-play (4th-down go rate
by field position). The nflverse play-by-play release 404s on
both the csv.gz and parquet URLs (checked 2026-11-05), exactly
as batch 2 found when it could not confirm the filename -- so no
go-rate number is computed, approximated from team totals, or
invented. PBP_AVAILABLE is False in this module until a working
source is confirmed; the coach test below is the ATS-record half
of the item only.

Point-in-time discipline. Ref/coach stats for a game use ONLY
games before week W of season S (all earlier seasons included --
a ref's 2018 games were known before 2019 -- plus weeks < W of
season S), computed in a single chronological pass so no future
game leaks in. A ref/coach with fewer than
REF_COACH_MIN_PRIOR_GAMES prior games gets no feature (x = 0),
never a league-average fill that would pretend knowledge.

Pre-registered variants (additive only -- neither feature has a
natural fade form; 3 tests, one Bonferroni family). Each is a
single no-intercept OLS slope of the point-in-time residual
(actual - model, home-margin units) on a pre-registered x, fit
on TRAIN (2019-2022) only, frozen, applied to TEST (2023-2025),
shift clipped to +/- REF_COACH_ADJ_CAP:

  ref_home     x = ref's prior mean (actual - line), home-margin units
  coach_resid  x = home coach's prior mean team-favour residual
               minus away coach's
  coach_ats    x = home coach's prior ATS% minus away coach's
               (fraction, -1..1)

Promotion gate (ALL must hold): the variant wins on the TEST
split (paired delta > 0 vs always-follow on the same test games)
AND its full-sample paired p-value survives Bonferroni across
all 3 tests (adjusted p < 0.05) AND it beats the additive
placebo gate (+/-0.15 pt placebo p95 on the test split).
Nothing passed; see README batch 5 group B.
"""
from __future__ import annotations

import json
import math

import pandas as pd

from . import backtest as backtest_mod, config
from . import situational as sit_mod

log = __import__("logging").getLogger(__name__)

TRAIN_SEASONS = (2019, 2020, 2021, 2022)
TEST_SEASONS = (2023, 2024, 2025)

VARIANTS = ("ref_home", "coach_resid", "coach_ats")

# 4th-down aggressiveness needs play-by-play. The nflverse release
# 404s (play_by_play_{season}.csv.gz and .parquet, checked
# 2026-11-05); no go-rate is computed here. Flip this only when a
# working PBP source is confirmed AND the number is recomputed
# from it -- never from team totals.
PBP_AVAILABLE = False
FOURTH_DOWN_STATUS = (
    "unavailable: nflverse play-by-play release 404s "
    "(play_by_play_{season}.csv.gz and .parquet, checked 2026-11-05); "
    "no 4th-down go rate is computed or approximated"
)


def fourth_down_aggressiveness(*args, **kwargs):
    """Always None: there is no play-by-play source to compute a
    4th-down go rate from. Returning None (not a guessed number)
    is the honest behaviour; callers must treat it as missing."""
    return None


# ---------------------------------------------------------------------------
# Point-in-time referee / coach stats (single chronological pass)
# ---------------------------------------------------------------------------

def _team_favour_margin(row, team: str) -> float | None:
    """Actual margin in `team`'s favour minus the line in the team's
    favour: positive = the team covered. Home team: actual - line;
    away team: line - actual. None when the game has no result/line."""
    try:
        actual = float(row["result"])
        line = float(row["spread_line"])
    except (TypeError, ValueError, KeyError):
        return None
    if pd.isna(actual) or pd.isna(line):
        return None
    return (actual - line) if team == row["home_team"] else (line - actual)


def precompute_features(games: pd.DataFrame) -> dict:
    """Point-in-time ref/coach features for every played game in
    `games` (all seasons in the frame -- earlier seasons are valid
    priors for later ones). Returns {(season, week, home, away):
    {ref_home_resid, ref_home_ats, ref_total_resid, ref_n,
    coach_resid_diff, coach_ats_diff, coach_n_home, coach_n_away}}.
    A game whose ref/coach has too few priors gets None features
    (x = 0 downstream), never a filled-in league average."""
    df = backtest_mod.load_games_frame(games)
    df = df[df["result"].notna() & df["spread_line"].notna()].copy()
    df = df.sort_values(["season", "week", "gameday", "gametime"],
                        kind="stable").reset_index(drop=True)
    min_prior = int(getattr(config, "REF_COACH_MIN_PRIOR_GAMES", 10))
    ref_stats: dict[str, dict] = {}
    coach_stats: dict[str, dict] = {}
    out: dict = {}
    for _, row in df.iterrows():
        key = (int(row["season"]), int(row["week"]),
               row["home_team"], row["away_team"])
        feat: dict = {"ref_n": 0, "coach_n_home": 0, "coach_n_away": 0,
                      "ref_home_resid": None, "ref_home_ats": None,
                      "ref_total_resid": None, "coach_resid_diff": None,
                      "coach_ats_diff": None}
        ref = row.get("referee")
        if isinstance(ref, str) and ref.strip():
            st = ref_stats.get(ref)
            if st and st["n"] >= min_prior:
                feat["ref_n"] = st["n"]
                feat["ref_home_resid"] = st["home_resid_sum"] / st["n"]
                dec = st["home_decided"]
                feat["ref_home_ats"] = (st["home_wins"] / dec) if dec else None
                feat["ref_total_resid"] = (st["total_resid_sum"] / st["total_n"]
                                           if st["total_n"] else None)
        hc, ac = row.get("home_coach"), row.get("away_coach")
        ch = coach_stats.get(hc) if isinstance(hc, str) and hc.strip() else None
        ca = coach_stats.get(ac) if isinstance(ac, str) and ac.strip() else None
        if ch and ca and ch["n"] >= min_prior and ca["n"] >= min_prior:
            feat["coach_n_home"] = ch["n"]
            feat["coach_n_away"] = ca["n"]
            feat["coach_resid_diff"] = ch["resid_sum"] / ch["n"] - ca["resid_sum"] / ca["n"]
            dh = ch["decided"]
            da = ca["decided"]
            if dh and da:
                feat["coach_ats_diff"] = ch["wins"] / dh - ca["wins"] / da
        out[key] = feat
        # --- update accumulators with THIS game (after recording) ---
        actual = float(row["result"])
        line = float(row["spread_line"])
        if isinstance(ref, str) and ref.strip():
            st = ref_stats.setdefault(ref, {"n": 0, "home_resid_sum": 0.0,
                                            "home_wins": 0, "home_decided": 0,
                                            "total_resid_sum": 0.0, "total_n": 0})
            st["n"] += 1
            st["home_resid_sum"] += actual - line
            if actual != line:
                st["home_decided"] += 1
                if actual > line:
                    st["home_wins"] += 1
            total, total_line = row.get("total"), row.get("total_line")
            try:
                total, total_line = float(total), float(total_line)
                if not pd.isna(total) and not pd.isna(total_line):
                    st["total_resid_sum"] += total - total_line
                    st["total_n"] += 1
            except (TypeError, ValueError):
                pass
        for team, coach in ((row["home_team"], hc), (row["away_team"], ac)):
            if not (isinstance(coach, str) and coach.strip()):
                continue
            fav = _team_favour_margin(row, team)
            if fav is None:
                continue
            st = coach_stats.setdefault(coach, {"n": 0, "resid_sum": 0.0,
                                                "wins": 0, "decided": 0})
            st["n"] += 1
            st["resid_sum"] += fav
            if actual != line:
                st["decided"] += 1
                if fav > 0:
                    st["wins"] += 1
    return out


def describe_refs(games: pd.DataFrame, min_games: int = 30) -> dict:
    """Career (full-sample, exploratory -- NOT the walk-forward
    test) per-referee table: games, home ATS%, mean (actual -
    line), mean (actual total - total_line). Only refs with >=
    min_games are listed; a 10-game ref at 70% is noise, not a
    finding."""
    df = backtest_mod.load_games_frame(games)
    df = df[df["result"].notna() & df["spread_line"].notna()].copy()
    out = {}
    for ref, sub in df.groupby("referee"):
        if not isinstance(ref, str) or not ref.strip():
            continue
        dec = sub[sub["result"] != sub["spread_line"]]
        wins = (dec["result"] > dec["spread_line"]).sum()
        tot = sub[sub["total"].notna() & sub["total_line"].notna()]
        if len(sub) < min_games:
            continue
        out[ref] = {
            "n": int(len(sub)), "decided": int(len(dec)),
            "home_ats_pct": round(100 * wins / len(dec), 2) if len(dec) else None,
            "mean_home_resid": round(float((sub["result"] - sub["spread_line"]).mean()), 2),
            "mean_total_resid": (round(float((tot["total"] - tot["total_line"]).mean()), 2)
                                 if len(tot) else None),
            "total_n": int(len(tot)),
        }
    return out


def describe_coaches(games: pd.DataFrame, min_games: int = 30) -> dict:
    """Career (full-sample, exploratory) per-coach table: games as
    head coach, ATS% of their team, mean team-favour residual.
    Only coaches with >= min_games are listed."""
    df = backtest_mod.load_games_frame(games)
    df = df[df["result"].notna() & df["spread_line"].notna()].copy()
    stats: dict[str, dict] = {}
    for _, row in df.iterrows():
        for team, coach in ((row["home_team"], row.get("home_coach")),
                            (row["away_team"], row.get("away_coach"))):
            if not (isinstance(coach, str) and coach.strip()):
                continue
            fav = _team_favour_margin(row, team)
            if fav is None:
                continue
            st = stats.setdefault(coach, {"n": 0, "wins": 0, "decided": 0,
                                          "resid_sum": 0.0})
            st["n"] += 1
            st["resid_sum"] += fav
            if float(row["result"]) != float(row["spread_line"]):
                st["decided"] += 1
                if fav > 0:
                    st["wins"] += 1
    out = {}
    for coach, st in stats.items():
        if st["n"] < min_games:
            continue
        out[coach] = {
            "n": st["n"], "decided": st["decided"],
            "ats_pct": round(100 * st["wins"] / st["decided"], 2) if st["decided"] else None,
            "mean_team_resid": round(st["resid_sum"] / st["n"], 2),
        }
    return out


# ---------------------------------------------------------------------------
# Walk-forward evaluation (same gate shape as situational.py)
# ---------------------------------------------------------------------------

def _variant_x(row: dict, variant: str) -> float:
    if variant == "ref_home":
        v = row.get("ref_home_resid")
    elif variant == "coach_resid":
        v = row.get("coach_resid_diff")
    elif variant == "coach_ats":
        v = row.get("coach_ats_diff")
    else:
        return 0.0
    try:
        return float(v) if v is not None and not pd.isna(v) else 0.0
    except (TypeError, ValueError):
        return 0.0


def _ols_slope(xs: list[float], rs: list[float]) -> float:
    den = sum(x * x for x in xs)
    if den <= 0:
        return 0.0
    return sum(x * r for x, r in zip(xs, rs)) / den


def _grade_variant(rows: list[dict], variant: str, slope: float) -> dict:
    cap = float(getattr(config, "REF_COACH_ADJ_CAP", 3.0))
    base_outcomes, var_outcomes = [], []
    base_errs, var_errs = [], []
    n_adjusted = 0
    for r in rows:
        m, line, actual = (float(r["model_margin"]), float(r["spread_line"]),
                           float(r["actual_margin"]))
        shift = max(-cap, min(cap, slope * _variant_x(r, variant)))
        if shift != 0.0:
            n_adjusted += 1
        base_outcomes.append(sit_mod._grade(m, line, actual))
        var_outcomes.append(sit_mod._grade(m + shift, line, actual))
        base_errs.append(abs(actual - m))
        var_errs.append(abs(actual - (m + shift)))
    return {"adjusted": sit_mod._summ(var_outcomes),
            "always_follow": sit_mod._summ(base_outcomes),
            "paired_vs_follow": sit_mod._paired(base_outcomes, var_outcomes),
            "mae_adjusted": round(sum(var_errs) / len(var_errs), 3) if var_errs else None,
            "mae_follow": round(sum(base_errs) / len(base_errs), 3) if base_errs else None,
            "n_adjusted": n_adjusted, "slope": round(slope, 4)}


def evaluate_all(games: pd.DataFrame, seasons: list[int] | None = None,
                 train_seasons: tuple[int, ...] = TRAIN_SEASONS,
                 test_seasons: tuple[int, ...] = TEST_SEASONS,
                 precomputed: dict | None = None) -> dict:
    """Full item-5 evaluation. Replays the baseline with ref/coach
    context (point-in-time features from the FULL games frame --
    earlier seasons are valid priors), then tests the 3
    pre-registered additive variants with the group-A promotion
    gate. Returns the baseline summary (must reproduce 50.03% /
    898 / 1,795), the exploratory ref/coach tables, per-variant
    results, and `promoted_variants` (empty unless a variant
    clears every gate)."""
    if seasons is None:
        seasons = list(range(2019, 2026))
    games = backtest_mod.load_games_frame(games)
    if precomputed is None:
        precomputed = precompute_features(games)

    def _ctx(season, week, row, ratings, games_played, hfa, season_games,
             played_current, prior_games):
        return dict(precomputed.get((int(season), int(week),
                                     row["home_team"], row["away_team"]), {}))

    rows = sit_mod.replay_with_context(games, seasons, context_fn=_ctx)
    base = sit_mod._summ([r["ats"] for r in rows])
    train_rows = [r for r in rows if r["season"] in train_seasons]
    test_rows = [r for r in rows if r["season"] in test_seasons]
    min_decided = int(getattr(config, "REF_COACH_MIN_TRAIN_DECIDED", 30))

    import pandas as _pd

    def _frame(sub_rows):
        return _pd.DataFrame([{"season": r["season"], "week": r["week"],
                               "home_team": r["home_team"], "away_team": r["away_team"],
                               "model_margin": r["model_margin"], "spread_line": r["spread_line"],
                               "actual_margin": r["actual_margin"], "ats": r["ats"]}
                              for r in sub_rows])

    placebo_test = sit_mod.placebo_additive_distribution(_frame(test_rows))
    placebo_full = sit_mod.placebo_additive_distribution(_frame(rows))

    per_variant: dict[str, dict] = {}
    promoted: list[str] = []
    n_tests = len(VARIANTS)
    for variant in VARIANTS:
        train_decided = [r for r in train_rows
                         if _variant_x(r, variant) != 0.0
                         and float(r["actual_margin"]) != float(r["spread_line"])]
        if len(train_decided) < min_decided:
            slope = 0.0
            learned = False
        else:
            xs = [_variant_x(r, variant) for r in train_rows]
            rs = [float(r["actual_margin"]) - float(r["model_margin"]) for r in train_rows]
            slope = _ols_slope(xs, rs)
            learned = True
        test_res = _grade_variant(test_rows, variant, slope)
        full_res = _grade_variant(rows, variant, slope)
        p_raw = full_res["paired_vs_follow"]["p_two_sided"]
        p_adj = min(1.0, p_raw * n_tests)
        gate = bool(learned
                    and test_res["paired_vs_follow"]["paired_delta_pp"] > 0
                    and test_res["paired_vs_follow"]["paired_delta_pp"] > placebo_test["p95_pp"]
                    and p_adj < 0.05)
        if gate:
            promoted.append(variant)
        per_variant[variant] = {
            "learned": learned, "train_decided_nonzero_x": len(train_decided),
            "slope": round(slope, 4), "test": test_res, "full": full_res,
            "p_bonferroni": round(p_adj, 4),
            "placebo_add_p95_pp": placebo_test["p95_pp"],
            "gate_pass": gate,
        }
    return {"baseline": base, "n": len(rows),
            "train_seasons": list(train_seasons), "test_seasons": list(test_seasons),
            "bonferroni_family_n": n_tests,
            "placebo_additive": placebo_full,
            "placebo_additive_test": placebo_test,
            "refs": describe_refs(games), "coaches": describe_coaches(games),
            "fourth_down": FOURTH_DOWN_STATUS,
            "per_variant": per_variant, "promoted_variants": promoted,
            "_rows": rows}


if __name__ == "__main__":
    import sys
    _games_csv = sys.argv[1] if len(sys.argv) > 1 else "/tmp/games.csv"
    _games = pd.read_csv(_games_csv, low_memory=False)
    _out = evaluate_all(_games)
    _out.pop("_rows", None)
    print(json.dumps(_out, indent=2, default=str))
