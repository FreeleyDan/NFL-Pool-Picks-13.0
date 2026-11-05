"""
Flip sensitivity (batch 5, group C, item 9): what would change my pick?

For each game, the line move -- or the rating change -- that would
flip the pick. Computed from numbers model.py already computed;
display only, no pick-logic change. Nothing here feeds back into
model.py, weekly_run.py, or the survivor optimizer.

Why the answer is one number: the pick follows the SIGN of
(model_independent - line). The blended edge model.py displays is
line + (model - line) * trust with trust > 0, so the blended edge
has the same sign as the raw deviation -- blending changes the
edge's size, never its sign. The pick therefore flips exactly
when the line crosses the model read (or, equivalently, when the
model read crosses the line). The flip line IS the model read,
the move needed IS |model - line|, and the rating change needed
is the same magnitude in the opposite direction. There is nothing
else to fit, and no separate threshold to tune -- the two
fragility labels in config.py (SENSITIVITY_FRAGILE_PTS = 0.5,
SENSITIVITY_SOLID_PTS = 3.0) only name how big that number is:

  * <= 0.5 pts: "fragile" -- the next half-point line move (the
    smallest a line normally moves) flips the pick.
  * >= 3.0 pts: "real lean" -- it takes a 3-point move to flip.
  * between: "thin lean".

Lines are nflverse convention throughout (+ = home favoured),
matching picks.vegas_spread / picks.pool_spread as stored.
The judged line is the pool line when set, else the Vegas line
-- the same choice model.py makes, so the flip is measured
against the line the pick is actually judged on.

Totals get the same treatment in total_flip_analysis(): the
total pick flips when the total line crosses the model total.
"""
from __future__ import annotations

import math

from . import config, explain


def _num(value):
    """float(value) or None -- NaN and unparseable values are None."""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(out) else out


def fragility_label(abs_move: float) -> str:
    """'fragile' / 'thin lean' / 'real lean' for a flip distance
    in points. Thresholds are config.SENSITIVITY_FRAGILE_PTS
    (0.5) and config.SENSITIVITY_SOLID_PTS (3.0) -- see the
    module docstring for why those values."""
    fragile = float(getattr(config, "SENSITIVITY_FRAGILE_PTS", 0.5))
    solid = float(getattr(config, "SENSITIVITY_SOLID_PTS", 3.0))
    if abs_move <= fragile:
        return "fragile"
    if abs_move >= solid:
        return "real lean"
    return "thin lean"


def flip_analysis(model_independent, line) -> dict:
    """Spread-pick sensitivity for one game.

    Returns {"status": "ok", "flip_line", "line_move_to_flip",
    "line_direction", "rating_move_to_flip", "rating_direction",
    "fragility", "summary"} on success, or {"status": "no_line"}
    / {"status": "no_model"} with a plain-English note when an
    input is missing (nothing to flip, nothing invented).

    flip_line is the nflverse line at which the pick flips -- the
    model read itself. line_move_to_flip is signed (flip_line -
    line): positive means the line must rise (home more favoured)
    to flip the pick. rating_move_to_flip is the same magnitude
    with the opposite sign: the model read must move toward the
    line by that much. At an exact tie (move 0.0) any move flips
    the pick, and the label is "fragile".
    """
    model = _num(model_independent)
    judged = _num(line)
    if judged is None:
        return {"status": "no_line",
                "note": "No line yet -- there is no pick to flip."}
    if model is None:
        return {"status": "no_model",
                "note": "No model read yet -- there is no pick to flip."}
    move = model - judged
    abs_move = abs(move)
    label = fragility_label(abs_move)
    if abs_move == 0:
        summary = ("Already on the line -- any move flips this pick "
                   "(fragile).")
        return {"status": "ok", "flip_line": round(model, 1),
                "line_move_to_flip": 0.0, "line_direction": "either way",
                "rating_move_to_flip": 0.0, "rating_direction": "either way",
                "fragility": label, "summary": summary}
    # Pick is home when model > line: the line must RISE past the
    # model to flip it to away, and the model must FALL to the line.
    if move > 0:
        line_dir = "up (home more favoured)"
        rating_dir = "down"
    else:
        line_dir = "down (away more favoured)"
        rating_dir = "up"
    summary = (f"Flips if the line moves {abs_move:.1f} pts {line_dir} "
               f"(to {model:+.1f}) or the model moves {abs_move:.1f} pts "
               f"{rating_dir} -- {label}.")
    return {"status": "ok", "flip_line": round(model, 1),
            "line_move_to_flip": round(move, 1),
            "line_direction": line_dir,
            "rating_move_to_flip": round(-move, 1),
            "rating_direction": rating_dir,
            "fragility": label, "summary": summary}


def total_flip_analysis(model_total, total_line) -> dict | None:
    """Total-pick sensitivity: the total pick flips when the total
    line crosses the model total. Returns None when either input
    is missing (no total to flip -- not an error). The over/under
    direction follows the same sign rule as the spread: the pick
    is OVER when model_total > total_line."""
    model = _num(model_total)
    line = _num(total_line)
    if model is None or line is None:
        return None
    move = model - line
    abs_move = abs(move)
    label = fragility_label(abs_move)
    if abs_move == 0:
        return {"status": "ok", "flip_line": round(model, 1),
                "line_move_to_flip": 0.0, "fragility": label,
                "summary": "Total sits exactly on the line -- any move flips it (fragile)."}
    if move > 0:
        summary = (f"Total flips if the line rises {abs_move:.1f} pts "
                   f"(to {model:.1f}) or the model total falls {abs_move:.1f} pts -- {label}.")
    else:
        summary = (f"Total flips if the line falls {abs_move:.1f} pts "
                   f"(to {model:.1f}) or the model total rises {abs_move:.1f} pts -- {label}.")
    return {"status": "ok", "flip_line": round(model, 1),
            "line_move_to_flip": round(move, 1),
            "fragility": label, "summary": summary}


def sensitivity_for_game(game) -> dict:
    """Spread + total sensitivity for one game mapping (same input
    shape as explain.explain_pick). Returns {"spread": <flip
    dict>, "total": <total flip dict or None>, "summary": <the
    spread summary, or the no-line note>}. Display only."""
    line = explain.judged_line(game)
    model = _num(game.get("model_independent"))
    spread = flip_analysis(model, line)
    total = total_flip_analysis(_num(game.get("model_total")),
                                explain.judged_total_line(game))
    summary = spread.get("summary") or spread.get("note", "")
    return {"spread": spread, "total": total, "summary": summary}


def sensitivities_for_games(rows) -> list[dict]:
    """sensitivity_for_game() over an iterable of game mappings,
    in order. A row that raises is reported with its matchup and
    the error instead of breaking the whole table."""
    out = []
    for game in rows:
        try:
            out.append(sensitivity_for_game(game))
        except Exception as exc:  # noqa: BLE001 -- display path, never fatal
            out.append({"spread": {"status": "error", "note": str(exc)},
                        "total": None,
                        "summary": f"Could not compute sensitivity: {exc}"})
    return out
