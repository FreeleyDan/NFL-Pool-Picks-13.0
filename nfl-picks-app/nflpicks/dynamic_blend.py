"""
Dynamic market blend (batch 4, group B item 5) -- tested, NOT wired live.

The question the parent task asked: should the market-blend weight
vary by week of season, or by how far the model disagrees with the
market (especially 5+ points)? Historically, when the model disagrees
with the market by a lot, who wins?

The structural fact that decides most of this before any number is
run, and that the tests below prove rather than assume: in this app
the pick follows the SIGN of (model_margin - line). model.py blends
the line toward the model with trust = confidence * 0.30, but the
pick side is the deviation's sign, and any strictly positive blend
weight preserves that sign. So rescaling the blend weight -- by week,
by deviation bucket, by anything -- CANNOT change a single pick
(except pushing a margin exactly onto the line, a tie artifact under
corrected grading, which is a push, not a win). A "dynamic blend"
that only rescales is therefore a display parameter, not a pick
parameter, in this app; the live fixed blend is kept for exactly
that reason, and DYNAMIC_BLEND_LIVE stays False.

What CAN change picks is a per-bucket follow/fade rule: in bucket B,
either take the model's side (follow, weight +1 in sign terms) or
the opposite side (fade, weight -1). That is the only pick-changing
form a dynamic blend can take here, so it is what this module tests
walk-forward: learn follow/fade per deviation bucket (and, separately,
per week bucket) on 2019-2022, apply frozen to 2023-2025, and require
a win on the test split above ~52.4% breakeven with Bonferroni
correction across the pre-registered tests. It did not win (see
README batch-4 group B), so no rule is wired into model.py and no
blend weight in config.py changes.

Conventions: all lines and margins are nflverse (positive = home
favoured). Rows are harness-row dicts with model_margin, spread_line,
actual_margin, season, week (the backtest _games frame's records, or
any dicts with those keys). Grading uses the batch-4 corrected push
rule throughout: a pick margin exactly equal to the line, or an
actual margin exactly equal to the line, is a push, excluded from
ATS% but counted. Deviation buckets: "<1" / "1-3" / "3-5" / "5+"
(the 5+ bucket splits into "5-8" / "8+" for description only).
Week buckets match the harness: "1-4" / "5-9" / "10+".
"""
from __future__ import annotations

import math
from statistics import NormalDist

from . import config

_NORM = NormalDist()

DEVIATION_BUCKETS = ("<1", "1-3", "3-5", "5+")
WEEK_BUCKETS = ("1-4", "5-9", "10+")


def deviation_bucket(model_margin: float, line: float) -> str:
    """Deviation bucket for one game, from |model - line|."""
    d = abs(float(model_margin) - float(line))
    if d < 1:
        return "<1"
    if d < 3:
        return "1-3"
    if d < 5:
        return "3-5"
    return "5+"


def deviation_bucket_fine(model_margin: float, line: float) -> str:
    """Descriptive split of the 5+ bucket into 5-8 / 8+."""
    d = abs(float(model_margin) - float(line))
    if d < 1:
        return "<1"
    if d < 3:
        return "1-3"
    if d < 5:
        return "3-5"
    if d < 8:
        return "5-8"
    return "8+"


def week_bucket(week: int) -> str:
    """Harness week bucket for one game."""
    w = int(week)
    if w <= 4:
        return "1-4"
    if w <= 9:
        return "5-9"
    return "10+"


def blend_margin(line: float, model_margin: float, weight: float) -> float:
    """Blended margin: line + weight * (model - line). A positive
    weight moves the number toward the model without ever changing
    which side of the line it sits on -- that is the structural fact
    behind this module's conclusion, and test_blend_margin below pins
    it down: for weight > 0 and model != line, sign(blend - line)
    == sign(model - line), always."""
    return float(line) + float(weight) * (float(model_margin) - float(line))


def _grade_pick(pick_margin: float, line: float, actual: float) -> str:
    """'correct' / 'wrong' / 'push' under the corrected push rule."""
    if pick_margin == line or actual == line:
        return "push"
    home_covers = actual > line
    return "correct" if (pick_margin > line) == home_covers else "wrong"


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
    the tie-artifact guard from batch 4 group A)."""
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


def describe(rows: list[dict]) -> dict:
    """Descriptive ATS by deviation bucket, fine deviation bucket,
    and week bucket, on the model's own side (follow). This is
    description, not a promotion test: a bucket's ATS% here is the
    training signal the walk-forward rule learner uses, and a bucket
    that looks bad in-sample must still win out-of-sample to matter.
    Rows missing a margin, line, or result are skipped and counted."""
    by_dev: dict[str, list[str]] = {}
    by_dev_fine: dict[str, list[str]] = {}
    by_week: dict[str, list[str]] = {}
    skipped = 0
    for r in rows:
        try:
            m, line, actual = float(r["model_margin"]), float(r["spread_line"]), float(r["actual_margin"])
            week = int(r["week"])
        except (KeyError, TypeError, ValueError):
            skipped += 1
            continue
        outcome = _grade_pick(m, line, actual)
        by_dev.setdefault(deviation_bucket(m, line), []).append(outcome)
        by_dev_fine.setdefault(deviation_bucket_fine(m, line), []).append(outcome)
        by_week.setdefault(week_bucket(week), []).append(outcome)
    return {"n_rows": len(rows), "skipped": skipped,
            "by_deviation": {b: _summ(by_dev.get(b, [])) for b in DEVIATION_BUCKETS},
            "by_deviation_fine": {b: _summ(by_dev_fine.get(b, []))
                                  for b in ("<1", "1-3", "3-5", "5-8", "8+")},
            "by_week": {b: _summ(by_week.get(b, [])) for b in WEEK_BUCKETS}}


def learn_bucket_rule(train_rows: list[dict], key: str = "deviation",
                      min_decided: int | None = None) -> dict:
    """Learn a per-bucket follow/fade rule on TRAINING rows only:
    follow a bucket when its training ATS% is above 50, else fade it.
    Buckets with fewer than min_decided decided training games
    (default config.DYNAMIC_BLEND_MIN_TRAIN_DECIDED = 30) default to
    follow -- flipping a bucket on a handful of games is chasing
    noise, and the default must be the model's own side. The rule is
    data, not a claim: apply_bucket_rule() on LATER seasons is the
    test. `key` is "deviation" or "week"."""
    if min_decided is None:
        min_decided = int(getattr(config, "DYNAMIC_BLEND_MIN_TRAIN_DECIDED", 30))
    buckets: dict[str, list[str]] = {}
    for r in train_rows:
        try:
            m, line, actual = float(r["model_margin"]), float(r["spread_line"]), float(r["actual_margin"])
            week = int(r["week"])
        except (KeyError, TypeError, ValueError):
            continue
        b = deviation_bucket(m, line) if key == "deviation" else week_bucket(week)
        buckets.setdefault(b, []).append(_grade_pick(m, line, actual))
    keys = DEVIATION_BUCKETS if key == "deviation" else WEEK_BUCKETS
    rule = {}
    for b in keys:
        s = _summ(buckets.get(b, []))
        if s["decided"] < min_decided or s["ats_pct"] is None:
            rule[b] = "follow"
        else:
            rule[b] = "follow" if s["ats_pct"] > 50 else "fade"
    return rule


def apply_bucket_rule(rows: list[dict], rule: dict, key: str = "deviation") -> dict:
    """Grade a learned per-bucket rule on (later) rows, with the
    always-follow baseline on the SAME rows and the paired comparison
    between them. A fade pick is the opposite side of the model's
    margin: pick margin = line - (model - line), which preserves
    |deviation| and flips only the side. Returns overall ATS% for the
    rule and the baseline, the paired stats, and the rule map -- so a
    rule win cannot hide behind a different game set, and an abstain
    reading is impossible: every game still gets a pick, as a pool
    requires. (A no-pick/abstain variant would score 0 on abstained
    pool games, strictly worse than a coin-flip pick, so it is not
    offered as a promotion path here.)"""
    base_outcomes, var_outcomes = [], []
    for r in rows:
        try:
            m, line, actual = float(r["model_margin"]), float(r["spread_line"]), float(r["actual_margin"])
            week = int(r["week"])
        except (KeyError, TypeError, ValueError):
            continue
        b = deviation_bucket(m, line) if key == "deviation" else week_bucket(week)
        pick = rule.get(b, "follow")
        var_margin = m if pick == "follow" else line - (m - line)
        base_outcomes.append(_grade_pick(m, line, actual))
        var_outcomes.append(_grade_pick(var_margin, line, actual))
    return {"rule": _summ(var_outcomes), "always_follow": _summ(base_outcomes),
            "paired_vs_follow": _paired(base_outcomes, var_outcomes),
            "rule_map": dict(rule)}


def fade_large_rule(rows: list[dict], edge: float | None = None) -> dict:
    """Single pre-registered rule, not a learned one: fade the model
    whenever |model - line| >= edge (default
    config.DYNAMIC_BLEND_LARGE_DEVIATION = 5.0), follow otherwise.
    Reported with the same paired comparison as apply_bucket_rule,
    so it competes on equal footing with the learned bucket rule."""
    if edge is None:
        edge = float(getattr(config, "DYNAMIC_BLEND_LARGE_DEVIATION", 5.0))
    base_outcomes, var_outcomes = [], []
    for r in rows:
        try:
            m, line, actual = float(r["model_margin"]), float(r["spread_line"]), float(r["actual_margin"])
        except (KeyError, TypeError, ValueError):
            continue
        var_margin = line - (m - line) if abs(m - line) >= edge else m
        base_outcomes.append(_grade_pick(m, line, actual))
        var_outcomes.append(_grade_pick(var_margin, line, actual))
    return {"rule": _summ(var_outcomes), "always_follow": _summ(base_outcomes),
            "paired_vs_follow": _paired(base_outcomes, var_outcomes),
            "edge": edge}
