"""
Market cross-checks (batch 2, item 6).

Uses THREE market lines that games.csv already carries for every
2019-2025 game (100% coverage in the harness window): the spread, the
moneyline pair, and the total. The point is disagreement detection,
not a new pick:

* Moneyline-implied win prob: American odds -> raw prob, then
  vig-removed by normalizing home + away to 1 (the standard
  no-vig conversion; it assumes the vig is split proportionally,
  which is the honest simple choice, not a sharp-book model).
* Moneyline-implied margin: the vig-free home win prob converted
  with the same normal approximation used elsewhere (SD
  config.MARKET_MARGIN_SD): margin = SD * Phi^-1(p_home). This is
  an approximation -- moneyline and spread markets need not agree
  exactly even when both are efficient (key numbers, push risk) --
  so the flag threshold (MARKET_ML_SPREAD_FLAG) is 3 points, not a
  hair-trigger.
* Total-implied team scores: with spread S (home perspective) and
  total T, implied home points = (T + S) / 2, away = (T - S) / 2.
  These must sum to T and differ by S by construction; their use is
  as a consistency record and as a check of a MODEL total against
  the market total, not as a margin signal on their own.
* Flags are diagnostic only. crosscheck() returns the original lines
  unchanged alongside the flags -- there is deliberately no
  "recommended line" field, so no caller can auto-switch the pool
  line from this output. The pool line stays the judged line
  (see db.line_snapshots / get_line_moves for spread movement).

Log all three lines: db.save_market_crosscheck() persists
spread + both moneylines + total + the computed flags per game.
"""
from __future__ import annotations

import math
from statistics import NormalDist

from . import config

_NORM = NormalDist()


def american_to_prob(odds: float) -> float | None:
    """American odds -> raw implied probability (before vig removal)."""
    try:
        o = float(odds)
    except (TypeError, ValueError):
        return None
    if o == 0:
        return None
    if o > 0:
        return 100.0 / (o + 100.0)
    return (-o) / ((-o) + 100.0)


def ml_implied_probs(home_ml, away_ml) -> tuple[float | None, float | None]:
    """Vig-free (normalized) home/away win probabilities, or (None, None)."""
    ph_raw = american_to_prob(home_ml) if home_ml is not None else None
    pa_raw = american_to_prob(away_ml) if away_ml is not None else None
    if ph_raw is None or pa_raw is None:
        return None, None
    total = ph_raw + pa_raw
    if total <= 0:
        return None, None
    return ph_raw / total, pa_raw / total


def ml_implied_margin(home_ml, away_ml) -> float | None:
    """Moneyline-implied home margin in points (normal approx)."""
    ph, _ = ml_implied_probs(home_ml, away_ml)
    if ph is None:
        return None
    p = min(0.999, max(0.001, ph))
    return float(config.MARKET_MARGIN_SD) * _NORM.inv_cdf(p)


def spread_implied_home_win_prob(spread: float) -> float:
    """P(home wins outright) if the spread were the mean margin and
    margins were normal with SD MARKET_MARGIN_SD."""
    sd = float(config.MARKET_MARGIN_SD)
    return _NORM.cdf(float(spread) / sd)


def total_implied_scores(spread: float, total: float) -> tuple[float, float]:
    """(implied home points, implied away points) from spread + total."""
    s, t = float(spread), float(total)
    return (t + s) / 2.0, (t - s) / 2.0


def crosscheck(spread, home_ml, away_ml, total,
               model_margin=None, model_total=None) -> dict:
    """Cross-check one game. `spread` is nflverse convention (the same
    convention as every other line in this app). Returns the input
    lines verbatim plus computed probabilities, implied margin/scores,
    and a flags list. No field in the result is a substitute line."""
    ph, pa = ml_implied_probs(home_ml, away_ml)
    ml_margin = ml_implied_margin(home_ml, away_ml)
    flags: list[str] = []
    spread_prob = None
    if spread is not None:
        try:
            spread_prob = round(spread_implied_home_win_prob(spread), 3)
        except (TypeError, ValueError):
            spread_prob = None
    if ml_margin is not None and spread is not None:
        try:
            if abs(ml_margin - float(spread)) >= float(config.MARKET_ML_SPREAD_FLAG):
                flags.append(
                    f"spread/ML disagree: ML-implied margin {ml_margin:.1f} vs spread {float(spread):.1f}")
        except (TypeError, ValueError):
            pass
    if model_margin is not None and spread is not None:
        try:
            if abs(float(model_margin) - float(spread)) >= float(config.SANITY_EDGE_FLAG):
                flags.append("model/spread gap >= sanity flag (needs review)")
        except (TypeError, ValueError):
            pass
    if model_margin is not None and ml_margin is not None:
        try:
            if abs(float(model_margin) - ml_margin) >= float(config.MARKET_ML_SPREAD_FLAG):
                flags.append("model/ML disagree by >= 3 pts")
        except (TypeError, ValueError):
            pass
    imp_home, imp_away = None, None
    if spread is not None and total is not None:
        try:
            imp_home, imp_away = total_implied_scores(spread, total)
        except (TypeError, ValueError):
            pass
    if model_total is not None and total is not None:
        try:
            if abs(float(model_total) - float(total)) >= float(config.MARKET_MODEL_TOTAL_FLAG):
                flags.append("model total vs market total disagree by >= 5 pts")
        except (TypeError, ValueError):
            pass
    return {
        "spread_line": spread, "home_moneyline": home_ml, "away_moneyline": away_ml,
        "total_line": total,
        "ml_home_win_prob": round(ph, 3) if ph is not None else None,
        "ml_away_win_prob": round(pa, 3) if pa is not None else None,
        "ml_implied_margin": round(ml_margin, 2) if ml_margin is not None else None,
        "spread_implied_home_win_prob": spread_prob,
        "total_implied_home_pts": round(imp_home, 2) if imp_home is not None else None,
        "total_implied_away_pts": round(imp_away, 2) if imp_away is not None else None,
        "model_margin": model_margin, "model_total": model_total,
        "flags": flags,
    }
