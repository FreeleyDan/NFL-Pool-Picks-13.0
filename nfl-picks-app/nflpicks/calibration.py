"""
Calibrated cover probabilities (backtest item 2).

Why the old number was overconfident: the live cover probability used a
normal approximation with stdev 13.5 around the raw model margin, i.e. it
treated the model margin as if it were calibrated 1:1 with the actual
margin. On point-in-time backtest data (2019-2025, n=1,871, see
backtest.py) the regression of actual margin on model margin is:

    actual ~= 0.19 + 0.89 * model_margin,  residual SD 13.27

A slope of 0.89, not 1.0, means the raw model is mildly over-dispersed:
a model edge of 8 points is worth only ~7.3 points of actual edge on
average. (Re-fit 2026-11-18, batch 7: the old 0.36/0.68 coefficients
did not reproduce from the current model's margins — OLS on the same
2019-2025 sample gives 0.19/0.89 — and over-shrank the displayed
probabilities. The old constants dated to an earlier margin
definition and were never re-fit after the model changed.)
Using the raw margin therefore overstated cover probabilities, most
noticeably for large edges -- exactly where the backtest also showed
no edge-bucket accuracy gain (8+ bucket: 49.3% ATS, MAE 13.70).

What this module does:
  * shrinks the model margin by the regression above (config
    COVER_SHRINK_SLOPE / COVER_SHRINK_INTERCEPT),
  * then turns the shrunk margin into P(home covers) by interpolating
    the empirical CDF of shrunk residuals from that same backtest
    (RESIDUAL_QUANTILES below, 2019-2025). The empirical CDF, unlike a
    normal curve, keeps the observed fat tails and the clustering of
    actual margins on key numbers (actual margin == 3 in 7.1% of games
    per side, == 7 in ~4.2% per side, vs ~2.9% per point under a normal
    curve with SD 13.5).

What it does NOT do:
  * It does not change the pick. A shrunk-margin pick rule (pick by
    shrunk margin vs line) was tested walk-forward 2000-2026 (batch 7,
    2026-11-18): 50.43% vs live 50.76% ATS, paired z=-1.13, 0 era wins,
    flips 47.53% vs live 52.47% on the same games. It loses, so it
    stays behind config.CALIBRATED_PICK=False. Cover Prob here is
    therefore a confidence signal: a value near or below 50% for the
    official pick means, honestly, that the model side is not actually
    favoured after shrinkage -- treat it as a coin flip, not as a
    confident lean.
  * The quantile table is a snapshot of 2019-2025 residuals, re-fit
    2026-11-18 (batch 7) alongside the constants. Re-fit it from
    backtest.py output when the model changes materially, or the
    calibration will drift with the model it was fit on -- which is
    exactly what happened to the 0.36/0.68 constants.
"""
from __future__ import annotations

from . import config

# (cumulative probability, shrunk residual in points), from the
# 2019-2025 point-in-time backtest (shrunk residual = actual margin -
# (0.19 + 0.89 * model margin), n=1,871, mean -0.01, SD 13.27).
# Re-fit 2026-11-18 (batch 7) with the new constants.
RESIDUAL_QUANTILES: list[tuple[float, float]] = [
    (0.010, -30.84), (0.025, -26.28), (0.050, -21.34), (0.100, -16.74),
    (0.200, -10.31), (0.300, -6.68), (0.400, -3.10), (0.500, -0.25),
    (0.600, 2.81), (0.700, 5.81), (0.800, 10.69), (0.900, 17.32),
    (0.950, 23.17), (0.975, 27.33), (0.990, 30.96),
]


def shrunk_margin(model_margin: float) -> float:
    """Calibrated margin: shrink the raw model margin toward the mean."""
    return config.COVER_SHRINK_INTERCEPT + config.COVER_SHRINK_SLOPE * float(model_margin)


def _empirical_cdf(x: float) -> float:
    """Interpolated empirical CDF of shrunk residuals at x (linear
    interpolation between quantile points; flat extrapolation outside
    the 1%/99% range, which is the honest choice rather than inventing
    a tail shape)."""
    pts = RESIDUAL_QUANTILES
    if x <= pts[0][1]:
        return pts[0][0]
    if x >= pts[-1][1]:
        return pts[-1][0]
    for (p0, v0), (p1, v1) in zip(pts, pts[1:]):
        if v0 <= x <= v1:
            if v1 == v0:
                return p1
            return p0 + (p1 - p0) * (x - v0) / (v1 - v0)
    return pts[-1][0]


def cover_probability_home(model_margin: float, line: float) -> float:
    """P(home team covers `line`) under the calibrated model.

    Home covers iff actual margin > line iff shrunk residual >
    line - shrunk margin, so this is 1 - CDF(line - shrunk_margin).
    Returns a value in [0.01, 0.99] (the quantile table's range).
    """
    threshold = float(line) - shrunk_margin(model_margin)
    return round(1.0 - _empirical_cdf(threshold), 3)


def cover_probability_for_pick(model_margin: float, line: float) -> float:
    """P(the model side covers). If the model margin is above the line
    the model side is home, otherwise away. This can be below 0.5 --
    that is the point: it flags picks that are not actually favoured
    after shrinkage, instead of dressing every pick up as >50%."""
    home_prob = cover_probability_home(model_margin, line)
    if model_margin == line:
        return 0.5
    return home_prob if model_margin > line else round(1.0 - home_prob, 3)
