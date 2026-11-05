"""
Pool-line staleness (batch 3, item 1) -- grading/descriptive framework only.

The question: when the closing line has moved away from the pool's locked
line, does following that move (taking the side the close favours more)
beat the pool line ATS, does fading it, or does neither -- and does the
answer depend on how far the line moved?

What this module is NOT:
* It is NOT a pick input. STALENESS_LIVE stays False and nothing in
  model.py, weekly_run.py or dashboard.py reads this module. A staleness
  score is only ever promoted on a walk-forward win, and no such win
  exists (see README batch-3 section).
* It does NOT duplicate the line-move flag. db.get_line_moves() and
  review_queue.py already flag pool-vs-closing moves >= 1 pt and edge
  flips for Thursday review; that flagging is untouched. This module
  grades outcomes against the pool line given both lines -- a different
  job, and one that can only be done once both snapshot lines exist
  for completed games.
* It does NOT invent a pool line. games.csv carries ONE undated
  near-close line per game (spread_line) and no opener, no timestamps,
  and no second line; a true pool-vs-closing backtest over 2019-2025
  is therefore impossible from this data, and this module refuses to
  fake one (e.g. by shifting the recorded line by an invented amount).
  The honest historical run is a labelled proxy -- moneyline-implied
  margin as a second market reading -- reported as a proxy, not as a
  staleness result. The real test this module is built for runs on
  future live data: join db.line_snapshots (pool + closing) to results
  for completed weeks and call grade_staleness() on those rows.

Conventions: all lines are nflverse convention (positive = home
favoured), matching spread_line, pool_line_overrides and
line_snapshots. Movement = closing - pool: positive movement means
the close favours the home team MORE than the pool line did, so the
"follow" side is home and the "fade" side is away (and vice versa).
ATS grading vs the pool line uses the batch-2 push rule: an actual
margin exactly equal to the pool line is a push, not a wrong pick.
"""
from __future__ import annotations

import math
import random
from statistics import NormalDist

from . import config

_NORM = NormalDist()


def staleness(pool_line: float | None, closing_line: float | None) -> dict | None:
    """Descriptive staleness of one game, or None if either line is missing.

    Returns movement, abs_movement, direction ("toward_home" /
    "toward_away" / "none"), bucket ("<1" / "1-2" / "2-3" / "3+"), and
    score (= abs_movement in points -- the score IS the movement size,
    no invented scaling). This is a measurement for review, not a pick
    adjustment: nothing here says which side to take."""
    if pool_line is None or closing_line is None:
        return None
    try:
        pool, close = float(pool_line), float(closing_line)
    except (TypeError, ValueError):
        return None
    movement = close - pool
    abs_move = abs(movement)
    direction = "none" if movement == 0 else ("toward_home" if movement > 0 else "toward_away")
    edges = tuple(float(e) for e in getattr(config, "STALENESS_BUCKETS", (1.0, 2.0, 3.0)))
    if abs_move < edges[0]:
        bucket = "<1"
    elif abs_move < edges[1]:
        bucket = "1-2"
    elif abs_move < edges[2]:
        bucket = "2-3"
    else:
        bucket = "3+"
    return {"pool_line": pool, "closing_line": close, "movement": round(movement, 2),
            "abs_movement": round(abs_move, 2), "direction": direction,
            "bucket": bucket, "score": round(abs_move, 2)}


def follow_side(pool_line: float, closing_line: float) -> str | None:
    """Side the closing move points toward: 'home' if the close favours
    home more than the pool line, 'away' if it favours away more, None
    if the lines are identical (no move to follow)."""
    movement = float(closing_line) - float(pool_line)
    if movement > 0:
        return "home"
    if movement < 0:
        return "away"
    return None


def fade_side(pool_line: float, closing_line: float) -> str | None:
    """Opposite of follow_side; None when there is no move."""
    f = follow_side(pool_line, closing_line)
    return None if f is None else ("away" if f == "home" else "home")


def _grade_side(side: str | None, actual_margin: float, pool_line: float) -> str | None:
    """'correct' / 'wrong' / 'push' for one side vs the pool line, or
    None if there is no side (no move) or the game pushed."""
    if side is None:
        return None
    if actual_margin == pool_line:
        return "push"
    home_covers = actual_margin > pool_line
    return "correct" if (side == "home") == home_covers else "wrong"


def grade_staleness(rows: list[dict]) -> dict:
    """Grade follow / fade / (optional) model picks against the POOL line.

    Row contract: pool_line, closing_line, actual_margin, and optionally
    season and model_margin (point-in-time model margin; its pick is
    graded vs the pool line too, as the "ignore the move" baseline).
    Rows missing a line or result are skipped and counted. Returns
    overall ATS% for follow and fade, follow/fade by movement bucket,
    per-season follow/fade, paired follow-vs-fade discordants, and the
    model baseline when model_margin is present. Pushes are excluded
    from ATS% (batch-2 convention) but counted."""
    follow_rows, fade_rows, model_rows = [], [], []
    skipped = 0
    by_bucket: dict[str, dict[str, list]] = {}
    by_season: dict[str, dict[str, list]] = {}
    for r in rows:
        try:
            pool, close, actual = float(r["pool_line"]), float(r["closing_line"]), float(r["actual_margin"])
        except (KeyError, TypeError, ValueError):
            skipped += 1
            continue
        s = staleness(pool, close)
        f = _grade_side(follow_side(pool, close), actual, pool)
        d = _grade_side(fade_side(pool, close), actual, pool)
        if f is not None:
            follow_rows.append(f)
        if d is not None:
            fade_rows.append(d)
        by_bucket.setdefault(s["bucket"], {"follow": [], "fade": []})
        if f is not None:
            by_bucket[s["bucket"]]["follow"].append(f)
        if d is not None:
            by_bucket[s["bucket"]]["fade"].append(d)
        if "season" in r and r["season"] is not None:
            key = str(int(r["season"]))
            by_season.setdefault(key, {"follow": [], "fade": []})
            if f is not None:
                by_season[key]["follow"].append(f)
            if d is not None:
                by_season[key]["fade"].append(d)
        if r.get("model_margin") is not None:
            try:
                mm = float(r["model_margin"])
            except (TypeError, ValueError):
                continue
            if mm == pool:
                model_rows.append("push")
            else:
                model_rows.append(_grade_side("home" if mm > pool else "away", actual, pool))

    def _summ(vals: list[str]) -> dict:
        dec = [v for v in vals if v != "push"]
        return {"n": len(vals), "decided": len(dec),
                "pushes": len(vals) - len(dec),
                "ats_pct": round(100 * sum(1 for v in dec if v == "correct") / len(dec), 2) if dec else None}

    # Paired follow-vs-fade on games where both exist (every moved game):
    # follow and fade are exact opposites, so discordants are just the
    # games one side got right -- the paired z is reported for symmetry
    # with backtest.paired_comparison, not as independent evidence.
    out = {"n_rows": len(rows), "skipped": skipped,
           "follow": _summ(follow_rows), "fade": _summ(fade_rows),
           "by_bucket": {b: {"follow": _summ(v["follow"]), "fade": _summ(v["fade"])}
                         for b, v in sorted(by_bucket.items())},
           "by_season": {s: {"follow": _summ(v["follow"]), "fade": _summ(v["fade"])}
                         for s, v in sorted(by_season.items())}}
    if model_rows:
        out["model_vs_pool"] = _summ(model_rows)
    return out


def learn_staleness_rule(train_rows: list[dict]) -> dict:
    """Learn a per-bucket follow/fade rule on TRAINING rows only: for
    each movement bucket, follow if training follow ATS% beats training
    fade ATS%, else fade. Buckets with no decided training games default
    to fade (the conservative choice: do not chase a move with no
    evidence). The rule is data, not a claim: apply_staleness_rule()
    must be run on LATER seasons before anything is promoted."""
    graded = grade_staleness(train_rows)
    rule = {}
    for bucket, vals in graded["by_bucket"].items():
        f, d = vals["follow"]["ats_pct"], vals["fade"]["ats_pct"]
        rule[bucket] = "follow" if (f is not None and d is not None and f > d) else "fade"
    return rule


def apply_staleness_rule(rows: list[dict], rule: dict) -> dict:
    """Grade a learned per-bucket rule on (later) rows: the rule's pick
    is follow or fade per the game's bucket. Returns overall ATS% plus
    the always-follow / always-fade comparators on the same rows, so a
    rule win cannot hide behind a different game set."""
    vals, follow_vals, fade_vals = [], [], []
    for r in rows:
        try:
            pool, close, actual = float(r["pool_line"]), float(r["closing_line"]), float(r["actual_margin"])
        except (KeyError, TypeError, ValueError):
            continue
        s = staleness(pool, close)
        pick = rule.get(s["bucket"], "fade")
        side = follow_side(pool, close) if pick == "follow" else fade_side(pool, close)
        g = _grade_side(side, actual, pool)
        if g is not None:
            vals.append(g)
        for lst, sd in ((follow_vals, follow_side(pool, close)), (fade_vals, fade_side(pool, close))):
            gg = _grade_side(sd, actual, pool)
            if gg is not None:
                lst.append(gg)

    def _summ(v: list[str]) -> dict:
        dec = [x for x in v if x != "push"]
        return {"n": len(v), "decided": len(dec),
                "ats_pct": round(100 * sum(1 for x in dec if x == "correct") / len(dec), 2) if dec else None}
    return {"rule": _summ(vals), "always_follow": _summ(follow_vals),
            "always_fade": _summ(fade_vals), "rule_map": dict(rule)}


def bootstrap_ci(values: list[str], trials: int | None = None,
                 seed: int | None = None) -> dict:
    """Bootstrap 95% CI for an ATS% (values: 'correct'/'wrong'/'push';
    pushes excluded, matching grade_staleness). Deterministic for a
    fixed seed. Returns {} when there are no decided games."""
    dec = [1 if v == "correct" else 0 for v in values if v != "push"]
    if not dec:
        return {}
    trials = int(trials if trials is not None else getattr(config, "STALENESS_BOOTSTRAP_TRIALS", 2000))
    rng = random.Random(int(seed if seed is not None else getattr(config, "STALENESS_BOOTSTRAP_SEED", 20261104)))
    n = len(dec)
    means = sorted(100 * sum(rng.choice(dec) for _ in range(n)) / n for _ in range(trials))
    lo = means[int(0.025 * trials)]
    hi = means[min(trials - 1, int(0.975 * trials))]
    return {"ats_pct": round(100 * sum(dec) / n, 2), "ci95": [round(lo, 2), round(hi, 2)],
            "decided": n}


def paired_z(b_correct: int, v_correct: int, both_n: int) -> dict:
    """McNemar-style paired z for two rules on the same decided games:
    b = games baseline right / variant wrong, v = variant right /
    baseline wrong. Mirrors backtest.paired_comparison."""
    b, v = int(b_correct), int(v_correct)
    z = (v - b) / math.sqrt(b + v) if (b + v) else 0.0
    p = 2 * (1 - _NORM.cdf(abs(z))) if (b + v) else 1.0
    return {"paired_delta_pp": round(100 * (v - b) / both_n, 2) if both_n else 0.0,
            "paired_z": round(z, 2), "p_two_sided": round(p, 4)}
