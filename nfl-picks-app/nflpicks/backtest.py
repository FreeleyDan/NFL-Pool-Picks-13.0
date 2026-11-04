"""
Point-in-time backtest harness (backtest item 1).

Design, and why it is trustworthy where the old grading was not:

* For target season S, week W, ratings are computed ONLY from
  season-S games with week < W plus the full season S-1 -- exactly the
  information a live run in week W would have had. The target game's
  own result, later weeks, and later seasons never enter its rating.
  This is what makes the results out-of-sample, not a fit.
* Backfill exclusion is by construction, not by flag: there is no
  model_log read here at all, so a backfilled prediction (saved after
  the game, from today's ratings) cannot leak in. The helper
  `exclude_backfill_rows` is provided for any caller that DOES read
  model_log, and mirrors grading.py's is_backfill=0 filter.
* The recorded `spread_line` in nflverse games.csv is used as the line
  to beat. That is the single biggest external-validity caveat: it is
  NOT a verified Tuesday/Thursday pool-locked line, and whether it is
  an opener, a closer, or something in between is not established
  from the file. ATS against a real pool line will differ.
* HFA is computed point-in-time from played games only
  (compute_empirical_hfa on played_current + played_prior), unlike
  the earlier analysis scripts that used prior-season-only HFA.
  Results therefore differ slightly from those scripts (baseline
  ~47.7-48.0% depending on HFA choice) -- the harness number is the
  canonical one going forward, because it mirrors the live run.
* Variants are applied by temporarily patching config values
  (PRIOR_FADE_GAMES, RECENT_HALF_LIFE_GAMES, DIV_MULTIPLIER,
  CALIBRATED_PICK, TRAVEL_ENABLED) or by supplying an
  `epa_ratings_fn(season, week, games_played)` callback for EPA
  variants. Each variant is evaluated on the SAME games, so paired
  comparisons (discordant games) are reported alongside the naive
  independent standard error.

Usage:
    python -m nflpicks.backtest --games-csv /path/to/games.csv \
        --seasons 2019-2025 --variants baseline,fade12,div1 \
        --out /tmp/backtest_out.json
    python -m nflpicks.backtest --help

The games CSV is the nflverse games.csv format (see data.load_games
for the column contract). A local copy is enough -- no network fetch
happens inside this module.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import math
from collections.abc import Callable

import pandas as pd

from . import calibration, config, elo as elo_mod, ensemble as ensemble_mod, epa as epa_mod, \
    features as features_mod, market as market_mod, model as model_mod, ratings as ratings_mod, \
    scoring as scoring_mod

VARIANTS: dict[str, dict] = {
    # name -> config overrides (empty = current live defaults)
    "baseline": {},
    "fade12": {"PRIOR_FADE_GAMES": 12},
    "fade10": {"PRIOR_FADE_GAMES": 10},
    "fade6": {"PRIOR_FADE_GAMES": 6},
    "hl8": {"RECENT_HALF_LIFE_GAMES": 8},
    "div1": {"DIV_MULTIPLIER": 1.0},
    "div09": {"DIV_MULTIPLIER": 0.9},
    "calibrated_pick": {"CALIBRATED_PICK": True},
    "travel": {"TRAVEL_ENABLED": True, "TRAVEL_POINTS_PER_1000MI": 0.95},
    # epa_cal is not a config override: evaluate_variant passes
    # epa_live=True and grades pred.model_independent (the combo) for
    # this variant only, when an epa_ratings_fn callback is supplied.
    "epa_cal": {},
    # epa_sos: same grading rule as epa_cal, but the caller-supplied
    # epa_ratings_fn must return SOS-iterative EPA ratings
    # (epa.sos_epa_ratings, blended) rather than naive ones. Batch 2.
    "epa_sos": {},
}


def exclude_backfill_rows(rows: list[dict]) -> list[dict]:
    """Drop model_log rows flagged is_backfill=1. Backfill rows were
    predicted AFTER the game from today's ratings, so counting them
    inflates grades with look-ahead bias (fixed 2026-11-04 in
    grading.py; this helper is the same rule for any new reader)."""
    return [r for r in rows if not r.get("is_backfill")]


def load_games_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Normalize a games.csv frame to the harness contract: REG games
    only, team codes normalized, season/week numeric, sorted
    chronologically. Does not drop unplayed games -- callers filter
    by result/line when evaluating."""
    df = df.copy()
    df["season"] = pd.to_numeric(df["season"], errors="coerce")
    df["week"] = pd.to_numeric(df["week"], errors="coerce")
    df["home_team"] = df["home_team"].map(config.normalize_team)
    df["away_team"] = df["away_team"].map(config.normalize_team)
    if "game_type" in df.columns:
        df = df[df["game_type"] == "REG"]
    sort_cols = [c for c in ("season", "week", "gameday", "gametime") if c in df.columns]
    return df.sort_values(sort_cols, kind="stable").reset_index(drop=True)


@contextlib.contextmanager
def _patched_config(overrides: dict):
    old = {}
    for k, v in overrides.items():
        old[k] = getattr(config, k)
        setattr(config, k, v)
    try:
        yield
    finally:
        for k, v in old.items():
            setattr(config, k, v)


def _offdef_ratings(played: pd.DataFrame, hfa: float) -> tuple[dict, dict]:
    """Offense/defense split SRS, HARNESS-ONLY diagnostic (item 4).

    Iterative damped solve: a team's offensive rating is its scoring
    above league average after adjusting for opponent defense, and
    vice versa. Predicted margin = (off_home - def_away + hfa/2) -
    (off_away - def_home - hfa/2). This is NOT used live: the ablation
    study found it 98.4% correlated with single SRS, with significantly
    worse MAE, so it stays a diagnostic variant here.
    Returns (off, def) dicts, mean-centered."""
    if played.empty:
        return {}, {}
    teams = sorted(set(played["home_team"]) | set(played["away_team"]))
    league_avg = float((played["home_score"].astype(float) + played["away_score"].astype(float)).mean() / 2)
    # Per-team game lists, built once (the naive per-iteration full scan
    # is O(weeks * teams * games * iters) and far too slow for a harness).
    team_games: dict[str, list[tuple]] = {t: [] for t in teams}
    for _, row in played.iterrows():
        h, a = row["home_team"], row["away_team"]
        hs, as_ = float(row["home_score"]), float(row["away_score"])
        team_games[h].append((a, hs, as_, True))
        team_games[a].append((h, as_, hs, False))
    off = {t: 0.0 for t in teams}
    deff = {t: 0.0 for t in teams}
    for _ in range(200):
        new_off, new_def = {}, {}
        for t in teams:
            off_vals, def_vals = [], []
            for opp, team_score, opp_score, is_home in team_games[t]:
                hfa_term = hfa / 2 if is_home else -hfa / 2
                off_vals.append(team_score - league_avg + deff[opp] - hfa_term)
                # def from the opponent's score: S_opp = L + off_opp - def_t
                # +/- hfa/2, so def_t = L + off_opp - S_opp -/+ hfa/2,
                # i.e. minus the team's own hfa_term in both cases.
                def_vals.append(league_avg + off[opp] - opp_score - hfa_term)
            new_off[t] = sum(off_vals) / len(off_vals) if off_vals else 0.0
            new_def[t] = sum(def_vals) / len(def_vals) if def_vals else 0.0
        mo = sum(new_off.values()) / len(new_off)
        md = sum(new_def.values()) / len(new_def)
        new_off = {t: v - mo for t, v in new_off.items()}
        new_def = {t: v - md for t, v in new_def.items()}
        delta = max(max(abs(new_off[t] - off[t]), abs(new_def[t] - deff[t])) for t in teams)
        off, deff = {t: 0.5 * off[t] + 0.5 * new_off[t] for t in teams}, \
                    {t: 0.5 * deff[t] + 0.5 * new_def[t] for t in teams}
        if delta < 1e-4:
            break
    return off, deff


def evaluate_variant(games: pd.DataFrame, seasons: list[int], variant: str = "baseline",
                     epa_ratings_fn: Callable | None = None) -> dict:
    """Run one variant over the given seasons. Returns a summary dict
    plus a per-game DataFrame under key '_games' (stripped by the CLI
    before JSON output, kept for paired comparisons in-process)."""
    overrides = VARIANTS.get(variant, {})
    rows: list[dict] = []
    with _patched_config(overrides):
        for season in seasons:
            season_games = games[games["season"] == season]
            prior_games = games[(games["season"] == season - 1) & games["result"].notna()]
            weeks = sorted(season_games[season_games["result"].notna()]["week"].unique().tolist())
            for week in weeks:
                played_current = season_games[(season_games["week"] < week) & season_games["result"].notna()]
                if variant == "offdef":
                    hfa = ratings_mod.compute_empirical_hfa(played_current, prior_games)
                    off, deff = _offdef_ratings(pd.concat([prior_games, played_current]) if len(prior_games) else played_current, hfa)
                    # Off/def ratings are computed on the combined history
                    # for stability; the target week itself is excluded
                    # because played_current stops before it.
                    ratings_for_week = None
                else:
                    hfa = ratings_mod.compute_empirical_hfa(played_current, prior_games)
                    ratings_for_week, games_played = ratings_mod.blended_team_ratings(played_current, prior_games, hfa)
                targets = season_games[(season_games["week"] == week) & season_games["result"].notna()
                                       & season_games["spread_line"].notna()]
                for _, row in targets.iterrows():
                    if variant == "offdef":
                        home, away = row["home_team"], row["away_team"]
                        eff_hfa = 0.0 if str(row.get("location") or "").strip().lower() == "neutral" else hfa
                        model_margin = (off.get(home, 0.0) - deff.get(away, 0.0) + eff_hfa / 2) - \
                                       (off.get(away, 0.0) - deff.get(home, 0.0) - eff_hfa / 2)
                        # Rest/div adjustments match model.py for comparability.
                        rest_adj = model_mod._rest_adjustment(row.get("home_rest"), row.get("away_rest"))
                        div_mult = getattr(config, "DIV_MULTIPLIER", 1.0) if model_mod._is_div_game(row.get("div_game")) else 1.0
                        model_margin = (model_margin + rest_adj) * div_mult
                    else:
                        epa_ratings = epa_ratings_fn(season, week, games_played) if epa_ratings_fn else None
                        pred = model_mod.predict_game(
                            row, ratings_for_week, games_played, hfa,
                            epa_ratings=epa_ratings,
                            epa_live=(variant in ("epa_cal", "epa_sos")),
                        )
                        # epa_cal / epa_sos grade the combo
                        # (model_independent when epa_live=True); every
                        # other variant grades the SRS margin_model,
                        # matching live behaviour.
                        model_margin = pred.model_independent if variant in ("epa_cal", "epa_sos") else pred.margin_model
                    actual = float(row["home_score"]) - float(row["away_score"])
                    line = float(row["spread_line"])
                    # Calibrated-pick variant grades the SHRUNK pick, not
                    # the raw margin -- grading the raw margin made this
                    # variant a no-op in the first harness run (0 flips).
                    pick_margin = (calibration.shrunk_margin(model_margin)
                                   if variant == "calibrated_pick" else model_margin)
                    if actual > line:
                        ats = "correct" if pick_margin > line else "wrong"
                    elif actual < line:
                        ats = "correct" if pick_margin < line else "wrong"
                    else:
                        ats = "push"
                    edge = model_margin - line
                    rows.append({
                        "season": int(season), "week": int(week),
                        "home_team": row["home_team"], "away_team": row["away_team"],
                        "model_margin": round(model_margin, 3), "spread_line": line,
                        "actual_margin": actual, "edge": round(edge, 3),
                        "edge_bucket": ("0-1" if abs(edge) < 1 else "1-3" if abs(edge) < 3 else
                                        "3-5" if abs(edge) < 5 else "5-8" if abs(edge) < 8 else "8+"),
                        "pick_side": "home" if pick_margin > line else "away",
                        "pick_favdog": ("fav" if (line > 0 and pick_margin > line) or (line < 0 and pick_margin < line) else "dog"),
                        "week_bucket": "1-4" if week <= 4 else "5-9" if week <= 9 else "10+",
                        "ats": ats, "abs_err": abs(actual - model_margin),
                        "line_abs_err": abs(actual - line),
                    })
    return summarize(rows, variant)


def evaluate_totals(games: pd.DataFrame, seasons: list[int], in_sample: bool = False) -> dict:
    """Totals harness (item 8): old scoring-average blend vs SRS
    offensive/defensive splits, on the SAME games, totals-only. The
    spread harness is untouched -- this neither reads nor writes any
    spread result, and its MAE numbers are not comparable to spread
    MAE (different target: combined points, not margin).

    Out-of-sample (default, the promotion test): for season S, week W,
    both models use only season-S games before week W plus the full
    season S-1, with point-in-time empirical HFA -- exactly the
    information a live run in week W would have had. No parameter is
    fitted on the target seasons: the SRS solve uses fixed config
    constants (damping, tolerance, prior fade/regression), so there is
    no training window to leak, and no walk-forward weight learning
    is needed or claimed.

    In-sample (in_sample=True, reported separately, never the
    promotion test): both models use the FULL target season plus the
    prior season, then predict that same season's games. The gap
    between in-sample and out-of-sample MAE is the overfit/noise check:
    a model that only wins in-sample has memorised its own targets.

    Weather is excluded from BOTH models here: the live weather
    adjustment needs a forecast API call, which games.csv cannot
    supply retrospectively, and applying it to one model only would
    rig the comparison. The live weather step in model.py applies
    identically on either totals path, so it cannot change which
    model wins this harness.

    Returns MAEs (old, SRS, and the recorded total_line as a market
    reference), per-season MAEs, paired old-minus-SRS statistics, and
    the per-game frame under '_games' (stripped by the CLI).
    """
    rows: list[dict] = []
    for season in seasons:
        season_games = games[games["season"] == season]
        prior_games = games[(games["season"] == season - 1) & games["result"].notna()]
        weeks = sorted(season_games[season_games["result"].notna()]["week"].unique().tolist())
        for week in weeks:
            if in_sample:
                played_current = season_games[season_games["result"].notna()]
            else:
                played_current = season_games[(season_games["week"] < week) & season_games["result"].notna()]
            hfa = ratings_mod.compute_empirical_hfa(played_current, prior_games)
            pts_for, pts_against = scoring_mod.blended_scoring_averages(played_current, prior_games)
            off, deff, league_avg = scoring_mod.blended_srs_scoring(played_current, prior_games, hfa)
            targets = season_games[(season_games["week"] == week) & season_games["result"].notna()
                                   & season_games["total_line"].notna()]
            for _, row in targets.iterrows():
                home, away = row["home_team"], row["away_team"]
                old_total = scoring_mod.predict_total(home, away, pts_for, pts_against)
                srs_total = scoring_mod.predict_total_srs(home, away, off, deff, league_avg)
                actual = float(row["home_score"]) + float(row["away_score"])
                line = float(row["total_line"])
                rows.append({
                    "season": int(season), "week": int(week),
                    "home_team": home, "away_team": away,
                    "old_total": old_total, "srs_total": srs_total,
                    "line_total": line, "actual_total": actual,
                    "old_err": abs(actual - old_total), "srs_err": abs(actual - srs_total),
                    "line_err": abs(actual - line),
                })
    df = pd.DataFrame(rows)
    if df.empty:
        return {"in_sample": in_sample, "n": 0, "_games": df}
    paired = df["old_err"] - df["srs_err"]
    by_season = {}
    for season, sub in df.groupby("season"):
        by_season[str(int(season))] = {
            "n": int(len(sub)),
            "old_mae": round(float(sub["old_err"].mean()), 3),
            "srs_mae": round(float(sub["srs_err"].mean()), 3),
            "line_mae": round(float(sub["line_err"].mean()), 3),
        }
    return {
        "in_sample": in_sample,
        "n": int(len(df)),
        "old_mae": round(float(df["old_err"].mean()), 3),
        "srs_mae": round(float(df["srs_err"].mean()), 3),
        "line_mae": round(float(df["line_err"].mean()), 3),
        "paired_old_minus_srs_mean": round(float(paired.mean()), 3),
        "paired_se": round(float(paired.std(ddof=1) / math.sqrt(len(paired))), 3) if len(paired) > 1 else None,
        "srs_wins": int((df["srs_err"] < df["old_err"]).sum()),
        "old_wins": int((df["old_err"] < df["srs_err"]).sum()),
        "ties": int((df["old_err"] == df["srs_err"]).sum()),
        "by_season": by_season,
        "_games": df,
    }


def summarize(rows: list[dict], variant: str = "baseline") -> dict:
    df = pd.DataFrame(rows)
    if df.empty:
        return {"variant": variant, "n": 0, "_games": df}
    decided = df[df["ats"] != "push"]
    def _bucket(frame: pd.DataFrame, col: str) -> dict:
        out = {}
        for key, sub in frame.groupby(col):
            dec = sub[sub["ats"] != "push"]
            out[str(key)] = {
                "n": int(len(sub)), "decided": int(len(dec)),
                "ats_pct": round(100 * (dec["ats"] == "correct").mean(), 2) if len(dec) else None,
                "mae": round(float(sub["abs_err"].mean()), 2),
            }
        return out
    return {
        "variant": variant,
        "n": int(len(df)),
        "pushes": int((df["ats"] == "push").sum()),
        "decided": int(len(decided)),
        "correct": int((decided["ats"] == "correct").sum()),
        "ats_pct": round(100 * (decided["ats"] == "correct").mean(), 2) if len(decided) else None,
        "mae": round(float(df["abs_err"].mean()), 2),
        "line_mae": round(float(df["line_abs_err"].mean()), 2),
        "by_edge": _bucket(df, "edge_bucket"),
        "by_favdog": _bucket(df, "pick_favdog"),
        "by_side": _bucket(df, "pick_side"),
        "by_week": _bucket(df, "week_bucket"),
        "by_season": _bucket(df, "season"),
        "_games": df,
    }


def _ats_rows(rows: list[dict], margin_key: str) -> list[dict]:
    """Re-express component rows as summarize()-compatible rows graded
    on the margin in `margin_key` (None margins are dropped)."""
    out = []
    for r in rows:
        m = r.get(margin_key)
        if m is None:
            continue
        line, actual = float(r["spread_line"]), float(r["actual_margin"])
        # A combined margin exactly equal to the line is NO pick (this
        # happens for market-only weights, where margin == line by
        # construction), not a wrong pick -- grading it "wrong" produced
        # a spurious 0% ATS for the market-weight ensemble in the first
        # batch-2 run. This push rule applies to batch-2 rows only;
        # evaluate_variant's historical grading is left untouched so
        # the canonical baseline numbers stay comparable.
        if m == line:
            ats = "push"
        elif actual > line:
            ats = "correct" if m > line else "wrong"
        elif actual < line:
            ats = "correct" if m < line else "wrong"
        else:
            ats = "push"
        out.append({
            "season": r["season"], "week": r["week"],
            "home_team": r["home_team"], "away_team": r["away_team"],
            "model_margin": round(float(m), 3), "spread_line": line,
            "actual_margin": actual, "edge": round(float(m) - line, 3),
            "edge_bucket": ("0-1" if abs(float(m) - line) < 1 else "1-3" if abs(float(m) - line) < 3 else
                            "3-5" if abs(float(m) - line) < 5 else "5-8" if abs(float(m) - line) < 8 else "8+"),
            "pick_side": "home" if m > line else "away",
            "pick_favdog": ("fav" if (line > 0 and m > line) or (line < 0 and m < line) else "dog"),
            "week_bucket": "1-4" if r["week"] <= 4 else "5-9" if r["week"] <= 9 else "10+",
            "ats": ats, "abs_err": abs(actual - float(m)),
            "line_abs_err": abs(actual - line),
        })
    return out


def evaluate_ensemble_and_features(games: pd.DataFrame, seasons: list[int],
                                   team_stats_by_season: dict | None = None) -> dict:
    """Batch-2 evaluation: ensemble (SRS + Elo + SOS-EPA + market) and
    play-by-play-proxy features, all walk-forward at SEASON level.

    For each target week, SRS / Elo / EPA / feature inputs use only
    that season's games before the week plus the full prior season
    (team stats: weeks before the week plus the full prior season).
    Ensemble weights and feature coefficients for season S are learned
    ONLY from completed seasons before S (plus two burn-in seasons
    before the first evaluated season, scored nowhere), so every
    ensemble/feature number below is out-of-sample w.r.t. its weights.
    In-sample (training-window) fits are reported separately under
    "train" so a training win that evaporates out-of-sample is visible
    instead of hidden.

    team_stats_by_season: {season: full-season team-stats frame} as
    returned by data.load_team_stats(season) (week=None). Missing
    seasons -> EPA and feature components are None for those games
    and the ensemble renormalizes over the available components.
    Returns summaries for baseline (SRS), ensemble_wf, ensemble_equal,
    one per-feature variant, features_all (marginal-fit sum), plus
    market cross-check diagnostics and the learned weights/coeffs.
    """
    team_stats_by_season = team_stats_by_season or {}
    all_teams = sorted(set(games["home_team"]) | set(games["away_team"]))
    first = min(seasons)
    burn_in = [s for s in (first - 2, first - 1)
               if ((games["season"] == s) & games["result"].notna()).any()]
    ordered = burn_in + [s for s in seasons]
    elo_state = elo_mod.new_state(all_teams)
    train_rows: list[dict] = []
    scored_rows: list[dict] = []
    weights_by_season: dict[int, dict] = {}
    weights_ats_by_season: dict[int, dict] = {}
    coeffs_by_season: dict[int, dict] = {}
    train_fit_by_season: dict[int, dict] = {}
    prior_sos_cache: dict[int, dict] = {}
    for si, season in enumerate(ordered):
        if si > 0:
            elo_state = elo_mod.regress_state(elo_state)
        is_eval = season in seasons
        if is_eval:
            learned = ensemble_mod.learn_weights(train_rows)
            weights_by_season[season] = learned["weights"]
            train_fit_by_season[season] = learned["train"]
            learned_ats = ensemble_mod.learn_weights(train_rows, objective="ats")
            weights_ats_by_season[season] = learned_ats["weights"]
            coeffs = {f: features_mod.fit_feature_coeff(train_rows, f)
                      for f in list(features_mod.FEATURES) + list(features_mod.INTERACTIONS)}
            coeffs_by_season[season] = coeffs
        else:
            coeffs = {}
        season_games = games[games["season"] == season]
        prior_games = games[(games["season"] == season - 1) & games["result"].notna()]
        ts_cur_full = team_stats_by_season.get(season)
        ts_prior_full = team_stats_by_season.get(season - 1)
        if ts_prior_full is not None and season - 1 not in prior_sos_cache:
            sos_prior = epa_mod.sos_epa_ratings(ts_prior_full)
            prior_sos_cache[season - 1] = sos_prior.get("ratings") or {}
        sos_prior = prior_sos_cache.get(season - 1, {})
        weeks = sorted(season_games[season_games["result"].notna()]["week"].unique().tolist())
        season_rows: list[dict] = []
        for week in weeks:
            played_current = season_games[(season_games["week"] < week) & season_games["result"].notna()]
            hfa = ratings_mod.compute_empirical_hfa(played_current, prior_games)
            ratings_for_week, games_played = ratings_mod.blended_team_ratings(played_current, prior_games, hfa)
            # SOS-EPA for this week (current weeks < W, prior full).
            epa_ratings = None
            if ts_cur_full is not None and "week" in ts_cur_full.columns:
                cur_filtered = ts_cur_full[pd.to_numeric(ts_cur_full["week"], errors="coerce") < week]
                sos_cur = epa_mod.sos_epa_ratings(cur_filtered)
                epa_ratings = epa_mod.blend_epa_ratings(
                    sos_cur.get("ratings") or {}, sos_prior, games_played)
            elif ts_cur_full is not None:
                sos_cur = epa_mod.sos_epa_ratings(ts_cur_full)
                epa_ratings = epa_mod.blend_epa_ratings(
                    sos_cur.get("ratings") or {}, sos_prior, games_played)
            # Feature rates: prior full + current weeks < W.
            feat_rates, feat_league = {}, {}
            if ts_cur_full is not None or ts_prior_full is not None:
                frames = [f for f in (ts_prior_full,
                                      ts_cur_full[pd.to_numeric(ts_cur_full["week"], errors="coerce") < week]
                                      if (ts_cur_full is not None and "week" in ts_cur_full.columns)
                                      else ts_cur_full) if f is not None and len(f)]
                if frames:
                    feat_rates = features_mod.team_feature_rates(pd.concat(frames, ignore_index=True))
                    feat_league = features_mod.league_rates(feat_rates)
            targets = season_games[(season_games["week"] == week) & season_games["result"].notna()
                                   & season_games["spread_line"].notna()]
            for _, row in targets.iterrows():
                home, away = row["home_team"], row["away_team"]
                pred = model_mod.predict_game(row, ratings_for_week, games_played, hfa)
                srs_margin = pred.margin_model
                eff_hfa = 0.0 if str(row.get("location") or "").strip().lower() == "neutral" else hfa
                elo_margin = elo_mod.elo_margin(
                    elo_state.get(home, float(config.ELO_START)),
                    elo_state.get(away, float(config.ELO_START)), eff_hfa)
                epa_margin = None
                if epa_ratings and home in epa_ratings and away in epa_ratings:
                    rest_adj = model_mod._rest_adjustment(row.get("home_rest"), row.get("away_rest"))
                    div_mult = getattr(config, "DIV_MULTIPLIER", 1.0) if model_mod._is_div_game(row.get("div_game")) else 1.0
                    epa_margin = round((epa_ratings[home] - epa_ratings[away] + eff_hfa + rest_adj) * div_mult, 1)
                line = float(row["spread_line"])
                actual = float(row["home_score"]) - float(row["away_score"])
                rec = {
                    "season": int(season), "week": int(week),
                    "home_team": home, "away_team": away,
                    "srs_margin": srs_margin, "elo_margin": round(elo_margin, 3),
                    "epa_margin": epa_margin, "market_margin": line,
                    "spread_line": line, "actual_margin": actual,
                    "model_margin": srs_margin,
                }
                diffs = features_mod.game_feature_differentials(row, feat_rates, feat_league) if feat_rates else {}
                for f, d in diffs.items():
                    rec[f"{f}_diff"] = d
                cc = market_mod.crosscheck(
                    line, row.get("home_moneyline"), row.get("away_moneyline"),
                    row.get("total_line"), model_margin=srs_margin)
                rec["ml_margin"] = cc["ml_implied_margin"]
                rec["ml_flag"] = bool(any("spread/ML disagree" in fl for fl in cc["flags"]))
                season_rows.append(rec)
            # Elo advances on this week's results (pre-week snapshot).
            week_played = season_games[(season_games["week"] == week) & season_games["result"].notna()]
            elo_state = elo_mod.apply_week(elo_state, week_played)
        if is_eval:
            w = weights_by_season[season]
            w_ats = weights_ats_by_season[season]
            equal_w = {c: 0.25 for c in ensemble_mod.COMPONENTS}
            for rec in season_rows:
                comps = {c: rec.get(f"{c}_margin") for c in ensemble_mod.COMPONENTS}
                rec["ensemble_margin"] = ensemble_mod.combine_margin(comps, w)
                rec["ensemble_ats_margin"] = ensemble_mod.combine_margin(comps, w_ats)
                rec["ensemble_equal_margin"] = ensemble_mod.combine_margin(comps, equal_w)
                for f, coeff in coeffs.items():
                    d = rec.get(f"{f}_diff")
                    rec[f"feat_{f}_margin"] = (rec["srs_margin"] + coeff * d) if d is not None else None
                rec["feat_all_margin"] = features_mod.adjusted_margin(
                    rec["srs_margin"],
                    {f: rec.get(f"{f}_diff") for f in coeffs}, coeffs)
            scored_rows.extend(season_rows)
        train_rows.extend(season_rows)
    # Summaries (each graded on its own margin key).
    out: dict = {"weights_by_season": weights_by_season,
                 "weights_ats_by_season": weights_ats_by_season,
                 "coeffs_by_season": coeffs_by_season,
                 "train_fit_by_season": train_fit_by_season}
    base = summarize(_ats_rows(scored_rows, "srs_margin"), "baseline")
    out["baseline"] = {k: v for k, v in base.items() if k != "_games"}
    out["baseline"]["paired_vs_baseline"] = {"n": base["decided"], "paired_delta_pp": 0.0, "paired_z": 0.0}
    for key, label in (("ensemble_margin", "ensemble_wf"),
                       ("ensemble_ats_margin", "ensemble_wf_ats"),
                       ("ensemble_equal_margin", "ensemble_equal"),
                       ("elo_margin", "component_elo"),
                       ("epa_margin", "component_epa"),
                       ("feat_all_margin", "features_all")):
        s = summarize(_ats_rows(scored_rows, key), label)
        s["paired_vs_baseline"] = paired_comparison(base["_games"], s["_games"])
        out[label] = {k: v for k, v in s.items() if k != "_games"}
    for f in list(features_mod.FEATURES) + list(features_mod.INTERACTIONS):
        s = summarize(_ats_rows(scored_rows, f"feat_{f}_margin"), f"feature_{f}")
        s["paired_vs_baseline"] = paired_comparison(base["_games"], s["_games"])
        out[f"feature_{f}"] = {k: v for k, v in s.items() if k != "_games"}
    # Market diagnostics: SRS pick on ML-flagged vs unflagged games, and
    # the ML-implied pick itself (ml margin vs spread) -- a diagnostic
    # for whether spread/ML disagreement carries signal, not a pick rule.
    def _ml_stats(rows):
        dec = [r for r in rows if r.get("ml_margin") is not None
               and r["actual_margin"] != r["spread_line"]]
        if not dec:
            return {"n": 0, "ats_pct": None}
        corr = sum(1 for r in dec if (r["actual_margin"] > r["spread_line"]) == (r["ml_margin"] > r["spread_line"]))
        return {"n": len(dec), "ats_pct": round(100 * corr / len(dec), 2)}
    flagged = [r for r in scored_rows if r["ml_flag"]]
    unflagged = [r for r in scored_rows if not r["ml_flag"]]
    out["market"] = {
        "n": len(scored_rows),
        "ml_flagged_n": len(flagged),
        "ml_pick_overall": _ml_stats(scored_rows),
        "ml_flagged_srs_ats": (lambda d: (round(100 * sum(1 for r in d if (r["actual_margin"] > r["spread_line"]) == (r["srs_margin"] > r["spread_line"])) / len(d), 2) if d else None))(
            [r for r in flagged if r["actual_margin"] != r["spread_line"]]),
        "ml_unflagged_srs_ats": (lambda d: (round(100 * sum(1 for r in d if (r["actual_margin"] > r["spread_line"]) == (r["srs_margin"] > r["spread_line"])) / len(d), 2) if d else None))(
            [r for r in unflagged if r["actual_margin"] != r["spread_line"]]),
    }
    out["_rows"] = scored_rows
    return out


def paired_comparison(baseline_games: pd.DataFrame, variant_games: pd.DataFrame) -> dict:
    """Paired (McNemar-style) comparison on the same games, keyed by
    season/week/home/away. Returns discordant counts and a paired z."""
    key = ["season", "week", "home_team", "away_team"]
    if baseline_games is None or variant_games is None or baseline_games.empty \
            or variant_games.empty or not all(k in baseline_games.columns for k in key) \
            or not all(k in variant_games.columns for k in key):
        # Empty variant (e.g. a feature with no usable data): no paired
        # comparison is possible, and merging would raise a KeyError.
        return {"n": 0, "variant_right_base_wrong": 0, "base_right_variant_wrong": 0,
                "paired_delta_pp": 0.0, "paired_z": 0.0}
    merged = baseline_games.merge(variant_games, on=key, suffixes=("_base", "_var"))
    dec = merged[(merged["ats_base"] != "push") & (merged["ats_var"] != "push")]
    b = int(((dec["ats_var"] == "correct") & (dec["ats_base"] != "correct")).sum())
    c = int(((dec["ats_base"] == "correct") & (dec["ats_var"] != "correct")).sum())
    n = len(dec)
    delta = 100 * (b - c) / n if n else 0.0
    z = (b - c) / math.sqrt(b + c) if (b + c) else 0.0
    return {"n": int(n), "variant_right_base_wrong": b, "base_right_variant_wrong": c,
            "paired_delta_pp": round(delta, 2), "paired_z": round(z, 2)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Point-in-time NFL ATS backtest harness")
    parser.add_argument("--games-csv", required=True, help="nflverse games.csv path")
    parser.add_argument("--seasons", default="2019-2025",
                        help="e.g. 2019-2025 or 2024,2025")
    parser.add_argument("--variants", default="baseline",
                        help="comma-separated variant names from VARIANTS, plus offdef and "
                             "ensemble_wf (batch 2; fetches team stats)")
    parser.add_argument("--out", default=None, help="JSON output path (default: stdout)")
    parser.add_argument("--totals", action="store_true",
                        help="also run the totals harness (item 8): old blend vs SRS splits, "
                             "out-of-sample and in-sample, totals-only; does not affect spread variants")
    args = parser.parse_args(argv)
    if "-" in args.seasons:
        lo, hi = args.seasons.split("-", 1)
        seasons = list(range(int(lo), int(hi) + 1))
    else:
        seasons = [int(s) for s in args.seasons.split(",")]
    raw = pd.read_csv(args.games_csv, low_memory=False)
    games = load_games_frame(raw)
    names = [v.strip() for v in args.variants.split(",")]
    results: dict[str, dict] = {}
    baseline_games = None
    # Batch-2 variants need team stats (network fetch, cached per
    # season). epa_sos uses SOS-EPA ratings; ensemble_wf runs the
    # full ensemble + feature evaluation instead of evaluate_variant.
    team_stats_cache: dict[int, pd.DataFrame | None] = {}
    if any(n in ("epa_sos", "ensemble_wf") for n in names):
        from . import data as data_mod
        for s in range(min(seasons) - 2, max(seasons) + 1):
            try:
                team_stats_cache[s] = data_mod.load_team_stats(s)
            except Exception:
                team_stats_cache[s] = None

    def _sos_epa_fn(season: int, week: int, games_played: dict):
        cur_full = team_stats_cache.get(season)
        prior_full = team_stats_cache.get(season - 1)
        if cur_full is None and prior_full is None:
            return None
        sos_cur = epa_mod.sos_epa_ratings(
            cur_full[pd.to_numeric(cur_full["week"], errors="coerce") < week]
            if (cur_full is not None and "week" in cur_full.columns) else cur_full)
        sos_prior = epa_mod.sos_epa_ratings(prior_full)
        return epa_mod.blend_epa_ratings(
            sos_cur.get("ratings") or {}, sos_prior.get("ratings") or {}, games_played)

    if args.totals:
        for label, ins in (("totals_oos", False), ("totals_insample", True)):
            tot = evaluate_totals(games, seasons, in_sample=ins)
            results[label] = {k: v for k, v in tot.items() if k != "_games"}
    for name in names:
        if name == "ensemble_wf":
            ens = evaluate_ensemble_and_features(games, seasons, team_stats_cache)
            ens_rows = ens.pop("_rows", None)
            results["ensemble_wf"] = ens
            continue
        summary = evaluate_variant(games, seasons, name,
                                   epa_ratings_fn=_sos_epa_fn if name == "epa_sos" else None)
        if name == "baseline":
            baseline_games = summary["_games"]
        if baseline_games is not None and name != "baseline":
            summary["paired_vs_baseline"] = paired_comparison(baseline_games, summary["_games"])
        results[name] = {k: v for k, v in summary.items() if k != "_games"}
    payload = {"seasons": seasons, "variants": results,
               "caveat": "spread_line is the recorded nflverse line, not a verified "
                         "Tuesday/Thursday pool-locked line; see backtest.py docstring."}
    text = json.dumps(payload, indent=2)
    if args.out:
        with open(args.out, "w") as f:
            f.write(text)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
