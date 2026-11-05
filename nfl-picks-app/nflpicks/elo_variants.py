"""
Elo variant research (batch 3, item 2) -- one-at-a-time grid, walk-forward gated.

Batch 2 left Elo as the most promising lead and the least proven one:
52.52% ATS alone, but 58.2% in 2020 vs 48.5% in 2023, and a paired z of
1.75 that does not survive correction across ~12 variants. This module
tests whether any simple Elo change fixes that, WITHOUT expanding the
search until something wins:

Grid (11 non-baseline variants, all one change from baseline):
  K: 10 / 32 / 40 (baseline 20)
  preseason rollover: 0.25 / 0.75 / 1.0 (baseline 0.5; 1.0 = no regression)
  home-field in Elo points: 0 / 110 (baseline 55)
  margin-based update: "log" / "capped" (baseline: win/loss only; see
    elo._margin_multiplier for the bounded formulas)
  recency: K x0.75 for weeks < 10, x1.25 for weeks >= 10 (baseline: flat K)

A full factorial (~100 combos) is deliberately NOT run: on 1,871 games
it would guarantee a spurious winner. No combined "best of OAT" variant
is tested either, for the same post-hoc reason -- if two OAT changes
both won, the combination would be a 12th variant needing its own
out-of-sample test, and neither did.

Point-in-time discipline (same as batch 2, so the baseline reproduces
it): for each target week, Elo ratings use only that season's games
before the week (pre-week snapshot per week) plus burn-in seasons
2017-2018, scored nowhere; the state regresses at each season start.
HFA in the Elo->margin conversion is the point-in-time empirical HFA
from played_current + prior season, with 0 at neutral sites. Grading
uses the batch-2 push rule (margin == line, or actual == line, is a
push, not a wrong pick).

Promotion gate -- ALL must hold, or nothing is promoted:
  (a) the variant beats baseline Elo on the 2019-2022 TRAINING split
      (this is where the variant is "chosen"),
  (b) the SAME variant also beats baseline Elo on the 2023-2025 TEST
      split (a training-only win is selection noise, not a finding),
  (c) its full-sample paired p-value survives Bonferroni correction
      across all 11 non-baseline variants (adjusted p < 0.05), and
  (d) its paired bootstrap 95% CI for the full-sample delta excludes 0.
A variant that wins on the full sample but was chosen on that same
sample fails (a) -- that is in-sample selection, reported separately
as "full-sample best" and never promoted on its own.
"""
from __future__ import annotations

import math
import random
from statistics import NormalDist

import pandas as pd

from . import config, elo as elo_mod, ratings as ratings_mod

_NORM = NormalDist()

BASELINE = {"name": "baseline", "k": None, "rollover": None, "hfa_points": None,
            "margin_mode": None, "recency": None}

DEFAULT_VARIANTS: list[dict] = [dict(BASELINE)]
for _k in getattr(config, "ELO_VARIANT_KS", (10.0, 32.0, 40.0)):
    DEFAULT_VARIANTS.append({"name": f"k{int(_k)}", "k": float(_k), "rollover": None,
                             "hfa_points": None, "margin_mode": None, "recency": None})
for _r in getattr(config, "ELO_VARIANT_ROLLOVERS", (0.25, 0.75, 1.0)):
    DEFAULT_VARIANTS.append({"name": f"roll{str(_r).replace('.', '')}", "k": None,
                             "rollover": float(_r), "hfa_points": None,
                             "margin_mode": None, "recency": None})
for _h in getattr(config, "ELO_VARIANT_HFA_POINTS", (0.0, 110.0)):
    DEFAULT_VARIANTS.append({"name": f"hfa{int(_h)}", "k": None, "rollover": None,
                             "hfa_points": float(_h), "margin_mode": None, "recency": None})
for _m in getattr(config, "ELO_VARIANT_MARGIN_MODES", ("log", "capped")):
    DEFAULT_VARIANTS.append({"name": f"margin_{_m}", "k": None, "rollover": None,
                             "hfa_points": None, "margin_mode": _m, "recency": None})
DEFAULT_VARIANTS.append({"name": "recency_late_boost", "k": None, "rollover": None,
                         "hfa_points": None, "margin_mode": None, "recency": "late_boost"})


def _grade(margin: float, line: float, actual: float) -> str:
    if margin == line or actual == line:
        return "push"
    return "correct" if (actual > line) == (margin > line) else "wrong"


def evaluate_variant(games: pd.DataFrame, seasons: list[int], variant: dict,
                     burn_in: tuple[int, ...] = (2017, 2018)) -> dict:
    """Replay one Elo variant over `seasons` (burn-in seasons unscored).
    Returns per-game rows plus a summary and per-season breakdown."""
    all_teams = sorted(set(games["home_team"]) | set(games["away_team"]))
    ordered = [s for s in burn_in if ((games["season"] == s) & games["result"].notna()).any()] \
        + [s for s in seasons]
    state = elo_mod.new_state(all_teams)
    rows: list[dict] = []
    for si, season in enumerate(ordered):
        if si > 0:
            state = elo_mod.regress_state(state, fraction=variant.get("rollover"))
        is_eval = season in seasons
        season_games = games[games["season"] == season]
        prior_games = games[(games["season"] == season - 1) & games["result"].notna()]
        weeks = sorted(season_games[season_games["result"].notna()]["week"].unique().tolist())
        for week in weeks:
            played_current = season_games[(season_games["week"] < week) & season_games["result"].notna()]
            hfa = ratings_mod.compute_empirical_hfa(played_current, prior_games)
            targets = season_games[(season_games["week"] == week) & season_games["result"].notna()
                                   & season_games["spread_line"].notna()]
            for _, row in targets.iterrows():
                home, away = row["home_team"], row["away_team"]
                eff_hfa = 0.0 if str(row.get("location") or "").strip().lower() == "neutral" else hfa
                margin = elo_mod.elo_margin(state.get(home, float(config.ELO_START)),
                                            state.get(away, float(config.ELO_START)), eff_hfa)
                if is_eval:
                    line = float(row["spread_line"])
                    actual = float(row["home_score"]) - float(row["away_score"])
                    rows.append({"season": int(season), "week": int(week), "home_team": home,
                                 "away_team": away, "elo_margin": round(margin, 3),
                                 "spread_line": line, "actual_margin": actual,
                                 "ats": _grade(margin, line, actual),
                                 "abs_err": abs(actual - margin)})
            mult = None
            if variant.get("recency") == "late_boost":
                mult = float(config.ELO_VARIANT_RECENCY_EARLY_MULT) if week < 10 \
                    else float(config.ELO_VARIANT_RECENCY_LATE_MULT)
            week_played = season_games[(season_games["week"] == week) & season_games["result"].notna()]
            state = elo_mod.apply_week(state, week_played, k=variant.get("k"),
                                       margin_mode=variant.get("margin_mode"),
                                       hfa_points=variant.get("hfa_points"),
                                       k_multiplier=mult)
    return {"variant": variant["name"], "rows": rows, "summary": summarize(rows)}


def summarize(rows: list[dict]) -> dict:
    """ATS%/MAE overall and per season for graded rows (pushes excluded
    from ATS%, included in MAE -- batch-2 convention)."""
    if not rows:
        return {"n": 0, "decided": 0, "ats_pct": None, "mae": None, "by_season": {}}
    dec = [r for r in rows if r["ats"] != "push"]
    by_season = {}
    for season in sorted({r["season"] for r in rows}):
        sub = [r for r in rows if r["season"] == season]
        dsub = [r for r in sub if r["ats"] != "push"]
        by_season[str(season)] = {
            "n": len(sub), "decided": len(dsub),
            "ats_pct": round(100 * sum(1 for r in dsub if r["ats"] == "correct") / len(dsub), 2) if dsub else None,
            "mae": round(sum(r["abs_err"] for r in sub) / len(sub), 2)}
    return {"n": len(rows), "decided": len(dec),
            "correct": sum(1 for r in dec if r["ats"] == "correct"),
            "pushes": len(rows) - len(dec),
            "ats_pct": round(100 * sum(1 for r in dec if r["ats"] == "correct") / len(dec), 2) if dec else None,
            "mae": round(sum(r["abs_err"] for r in rows) / len(rows), 2),
            "by_season": by_season}


def summarize_subset(rows: list[dict], seasons: list[int]) -> dict:
    return summarize([r for r in rows if r["season"] in seasons])


def paired_comparison(base_rows: list[dict], var_rows: list[dict]) -> dict:
    """Paired McNemar comparison on identical games (season/week/home/
    away). Returns discordants, paired delta, z, nominal two-sided p,
    and the Bonferroni-adjusted p across all non-baseline variants --
    the adjusted p is the one the promotion gate uses."""
    key = lambda r: (r["season"], r["week"], r["home_team"], r["away_team"])
    var_map = {key(r): r for r in var_rows}
    b = c = n = 0
    for r in base_rows:
        v = var_map.get(key(r))
        if v is None or r["ats"] == "push" or v["ats"] == "push":
            continue
        n += 1
        if v["ats"] == "correct" and r["ats"] != "correct":
            c += 1
        elif r["ats"] == "correct" and v["ats"] != "correct":
            b += 1
    z = (c - b) / math.sqrt(b + c) if (b + c) else 0.0
    p = 2 * (1 - _NORM.cdf(abs(z))) if (b + c) else 1.0
    m = max(1, len(DEFAULT_VARIANTS) - 1)
    return {"n": n, "variant_right_base_wrong": c, "base_right_variant_wrong": b,
            "paired_delta_pp": round(100 * (c - b) / n, 2) if n else 0.0,
            "paired_z": round(z, 2), "p_two_sided": round(p, 4),
            "p_bonferroni": round(min(1.0, p * m), 4), "n_variants_adjusted_for": m}


def bootstrap_paired_delta(base_rows: list[dict], var_rows: list[dict],
                           trials: int | None = None, seed: int | None = None) -> dict:
    """Bootstrap 95% CI for the paired ATS delta (variant minus baseline,
    percentage points), resampling decided-in-both games with
    replacement. Deterministic for a fixed seed; {} with no games."""
    key = lambda r: (r["season"], r["week"], r["home_team"], r["away_team"])
    var_map = {key(r): r for r in var_rows}
    pairs = []
    for r in base_rows:
        v = var_map.get(key(r))
        if v is None or r["ats"] == "push" or v["ats"] == "push":
            continue
        pairs.append(1 if v["ats"] == "correct" else 0)
        pairs[-1] = pairs[-1] - (1 if r["ats"] == "correct" else 0)
    if not pairs:
        return {}
    trials = int(trials if trials is not None else getattr(config, "ELO_BOOTSTRAP_TRIALS", 2000))
    rng = random.Random(int(seed if seed is not None else getattr(config, "ELO_BOOTSTRAP_SEED", 20261104)))
    n = len(pairs)
    deltas = sorted(100 * sum(rng.choice(pairs) for _ in range(n)) / n for _ in range(trials))
    point = 100 * sum(pairs) / n
    return {"paired_delta_pp": round(point, 2),
            "ci95": [round(deltas[int(0.025 * trials)], 2),
                     round(deltas[min(trials - 1, int(0.975 * trials))], 2)],
            "n": n}


def evaluate_all(games: pd.DataFrame, seasons: list[int],
                 variants: list[dict] | None = None) -> dict:
    """Run every variant, then the promotion gate. Train/test splits
    use config ELO_TRAIN_SEASONS / ELO_TEST_SEASONS (2019-2022 /
    2023-2025); a variant's train ATS is where it is 'chosen', its
    test ATS is the out-of-sample check. Full-sample 'best' is reported
    for transparency and explicitly NOT treated as a win."""
    variants = variants if variants is not None else DEFAULT_VARIANTS
    results: dict[str, dict] = {}
    base_rows = None
    for variant in variants:
        ev = evaluate_variant(games, seasons, variant)
        results[variant["name"]] = ev
        if variant["name"] == "baseline":
            base_rows = ev["rows"]
    train_seasons = list(getattr(config, "ELO_TRAIN_SEASONS", (2019, 2020, 2021, 2022)))
    test_seasons = list(getattr(config, "ELO_TEST_SEASONS", (2023, 2024, 2025)))
    base_train = summarize_subset(base_rows, train_seasons)
    base_test = summarize_subset(base_rows, test_seasons)
    gate: dict[str, dict] = {}
    for name, ev in results.items():
        if name == "baseline":
            continue
        train = summarize_subset(ev["rows"], train_seasons)
        test = summarize_subset(ev["rows"], test_seasons)
        paired = paired_comparison(base_rows, ev["rows"])
        boot = bootstrap_paired_delta(base_rows, ev["rows"])
        train_win = (train["ats_pct"] or 0) > (base_train["ats_pct"] or 0)
        test_win = (test["ats_pct"] or 0) > (base_test["ats_pct"] or 0)
        bonf = paired["p_bonferroni"] < 0.05
        ci_excl = bool(boot and (boot["ci95"][0] > 0 or boot["ci95"][1] < 0))
        gate[name] = {"train_ats_pct": train["ats_pct"], "base_train_ats_pct": base_train["ats_pct"],
                      "test_ats_pct": test["ats_pct"], "base_test_ats_pct": base_test["ats_pct"],
                      "full_ats_pct": ev["summary"]["ats_pct"],
                      "base_full_ats_pct": results["baseline"]["summary"]["ats_pct"],
                      "paired": paired, "bootstrap": boot,
                      "wins_train": train_win, "wins_test": test_win,
                      "bonferroni_significant": bonf, "bootstrap_ci_excludes_zero": ci_excl,
                      "PROMOTED": bool(train_win and test_win and bonf and ci_excl)}
    out = {name: {k: v for k, v in ev.items() if k != "rows"} for name, ev in results.items()}
    out["promotion_gate"] = gate
    out["promoted_variants"] = [n for n, g in gate.items() if g["PROMOTED"]]
    out["_rows"] = {name: ev["rows"] for name, ev in results.items()}
    return out
