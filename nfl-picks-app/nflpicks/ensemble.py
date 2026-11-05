"""
Walk-forward-weighted ensemble (batch 2, item 4).

Components (all point-in-time margins, home perspective):
  srs    -- ratings.blended_team_ratings margin (the live model)
  elo    -- elo.elo_margin
  epa    -- SOS-iterative EPA margin (epa.sos_epa_ratings, blended)
  market -- the spread line itself (the market is a component, not
            just the thing being beaten: a weight on market shrinks
            every other component toward the line)

Weights are NEVER hand-set. learn_weights() grid-searches the simplex
(grid step config.ENSEMBLE_GRID_STEP) on a TRAINING window only and
minimizes MAE, breaking ties by ATS%. The backtest applies weights
learned on strictly earlier seasons to the current season
(season-level walk-forward), so the reported ensemble numbers are
out-of-sample with respect to the weights. A single-season weight fit
would be overfitting on ~270 games; the harness therefore requires
ENSEMBLE_MIN_TRAIN_GAMES before learned weights replace equal weights.

combine_margin() renormalizes over AVAILABLE components: if EPA is
missing for a game (no team-stats feed), its weight is dropped and the
rest renormalized, rather than silently treating EPA as zero.
"""
from __future__ import annotations

import itertools

from . import config

COMPONENTS = ("srs", "elo", "epa", "market")


def _simplex_weights(step: float):
    """All 4-component weight tuples on the grid that sum to 1."""
    n = int(round(1.0 / step))
    out = []
    for a in range(n + 1):
        for b in range(n + 1 - a):
            for c in range(n + 1 - a - b):
                d = n - a - b - c
                out.append((a * step, b * step, c * step, d * step))
    return out


def combine_margin(components: dict, weights: dict) -> float | None:
    """Weighted margin over available components, renormalized.

    components: {name: margin or None}. weights: {name: weight}.
    Returns None only if no component is available.
    """
    avail = {k: v for k, v in components.items()
             if v is not None and k in weights}
    total = sum(weights[k] for k in avail)
    if not avail or total <= 0:
        return None
    return sum(weights[k] * v for k, v in avail.items()) / total


def evaluate_weights(rows: list[dict], weights: dict) -> dict:
    """MAE / ATS% of a weight vector on rows with component margins.

    A row needs actual_margin and spread_line plus at least one
    component; the pick is the combined margin vs the line (pushes
    excluded from ATS%, included in MAE, matching backtest.summarize).
    """
    errs, correct, decided = [], 0, 0
    for r in rows:
        m = combine_margin({c: r.get(f"{c}_margin") for c in COMPONENTS}, weights)
        if m is None:
            continue
        errs.append(abs(float(r["actual_margin"]) - m))
        line = float(r["spread_line"])
        actual = float(r["actual_margin"])
        if actual == line:
            continue
        decided += 1
        if (actual > line) == (m > line):
            correct += 1
    if not errs:
        return {"n": 0, "mae": None, "ats_pct": None}
    return {
        "n": len(errs),
        "mae": round(sum(errs) / len(errs), 3),
        "ats_pct": round(100 * correct / decided, 2) if decided else None,
    }


def learn_weights(rows: list[dict], step: float | None = None,
                  objective: str = "mae") -> dict:
    """Grid-search weights on TRAINING rows only.

    objective="mae" (default): minimize MAE, tie-break ATS%. This is
    the statistically stable choice -- but note what it does when the
    market is a component: the line's MAE (~9.8) beats every model
    component (~10.3), so MAE-optimal weights collapse to 100% market,
    i.e. "no pick". That collapse is reported honestly, not hidden.
    objective="ats": maximize ATS%, tie-break MAE. This is the pool's
    actual scoring rule, but ATS% on ~250-500 training games is a
    noisier objective (a 1 pp move is ~2-5 games), so it is reported
    as a sensitivity alongside MAE, not as the primary fit.
    Returns {"weights": {...}, "train": evaluate_weights(...)}.
    With too few training games, returns equal weights over the
    components present in the rows and flags it, rather than fitting
    noise on a tiny sample.
    """
    step = step if step is not None else float(config.ENSEMBLE_GRID_STEP)
    present = [c for c in COMPONENTS
               if any(r.get(f"{c}_margin") is not None for r in rows)]
    if len(rows) < int(config.ENSEMBLE_MIN_TRAIN_GAMES) or not present:
        w = {c: (1.0 / len(present) if c in present else 0.0) for c in COMPONENTS}
        return {"weights": w, "train": evaluate_weights(rows, w),
                "note": "equal weights (training sample below ENSEMBLE_MIN_TRAIN_GAMES)"}
    best = None
    for combo in _simplex_weights(step):
        w = dict(zip(COMPONENTS, combo))
        # Skip weight vectors that put weight only on components absent
        # from this training set (they are indistinguishable here).
        if any(w[c] > 0 and c not in present for c in COMPONENTS):
            continue
        ev = evaluate_weights(rows, w)
        if ev["mae"] is None:
            continue
        if objective == "ats":
            key = (-(ev["ats_pct"] or 0), ev["mae"])
        else:
            key = (ev["mae"], -(ev["ats_pct"] or 0))
        if best is None or key < best[0]:
            best = (key, w, ev)
    if best is None:
        w = {c: (1.0 / len(present) if c in present else 0.0) for c in COMPONENTS}
        return {"weights": w, "train": evaluate_weights(rows, w), "note": "equal weights (no grid point evaluable)"}
    return {"weights": best[1], "train": best[2], "note": "grid-searched on training window only"}
