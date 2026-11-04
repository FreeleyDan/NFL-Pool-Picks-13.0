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

from . import calibration, config, model as model_mod, ratings as ratings_mod

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
                            epa_live=(variant == "epa_cal"),
                        )
                        # epa_cal grades the combo (model_independent
                        # when epa_live=True); every other variant grades
                        # the SRS margin_model, matching live behaviour.
                        model_margin = pred.model_independent if variant == "epa_cal" else pred.margin_model
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


def paired_comparison(baseline_games: pd.DataFrame, variant_games: pd.DataFrame) -> dict:
    """Paired (McNemar-style) comparison on the same games, keyed by
    season/week/home/away. Returns discordant counts and a paired z."""
    key = ["season", "week", "home_team", "away_team"]
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
                        help="comma-separated variant names from VARIANTS, plus offdef")
    parser.add_argument("--out", default=None, help="JSON output path (default: stdout)")
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
    for name in names:
        summary = evaluate_variant(games, seasons, name)
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
