"""
Per-game explanations in plain English (batch 5, group C, item 8).

One line per game -- "why this pick" -- generated from numbers
model.py already computed. No new model, no new data source, no
pick-logic change: this module only turns the stored pick row
into words. The dashboard adds the result as a "Why" column on
the ATS tab; nothing else reads this module.

Inputs are a plain mapping (a picks row as a dict, a pandas
Series converted with .to_dict(), or a hand-built dict in tests)
with the keys below. Everything is optional except home/away:
missing inputs are skipped, never invented -- a game with no
line says "no line yet" instead of guessing a reason.

    home_team, away_team       team abbreviations
    pick                       stored pick string, e.g. "KC -3.5"
    model_independent          model's raw read, nflverse (+ = home favoured)
    pool_spread, vegas_spread  judged line is pool_spread when set, else vegas_spread
    edge                       blended edge (display only here)
    cover_prob                 P(pick covers), calibrated
    cover_prob_range           optional (lo, hi) under rating uncertainty
                               (uncertainty.cover_prob_interval)
    confidence                 0-1, from rating sample size on both sides
    strong_edge, needs_review  flags from the raw model-vs-line gap
    injury_flags               dashboard injury summary string (may be "")
    weather_note               totals weather note (may be "")
    model_total                model's total, if computed
    pool_total, vegas_total    judged total line is pool_total when set, else vegas_total
    total_pick                 e.g. "OVER 44.5" / "no play" / "no line"
    line_move                  optional signed pool->live line move, nflverse
                               (closing - pool; + means the live line
                               favours home more than the pool line did)

The returned dict is {"why": <one line>, "factors": [<clauses>]}.
"why" is the factors joined with "; " -- one line by construction.
"""
from __future__ import annotations

import math


def _num(value):
    """float(value) or None -- NaN and unparseable values are None,
    so a pandas NaN in a row never becomes a bogus factor."""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(out) else out


def _text(value) -> str:
    if value is None:
        return ""
    try:
        if isinstance(value, float) and math.isnan(value):
            return ""
    except TypeError:
        pass
    return str(value).strip()


def judged_line(game) -> float | None:
    """The line the pick was judged against: the pool line when
    set, else the live Vegas line. Mirrors model.py's
    line_for_pick choice, read from the stored row."""
    pool = _num(game.get("pool_spread"))
    if pool is not None:
        return pool
    return _num(game.get("vegas_spread"))


def judged_total_line(game) -> float | None:
    """Same rule for the total: pool total when set, else Vegas."""
    pool = _num(game.get("pool_total"))
    if pool is not None:
        return pool
    return _num(game.get("vegas_total"))


def pick_side(game) -> str | None:
    """'home' / 'away' from the stored pick string's team, or
    None when the pick is missing or unparsable. The pick string
    is "<TEAM> <signed line>", so the first token is the team."""
    pick = _text(game.get("pick"))
    if not pick:
        return None
    team = pick.split()[0].upper()
    home = _text(game.get("home_team")).upper()
    away = _text(game.get("away_team")).upper()
    if team == home:
        return "home"
    if team == away:
        return "away"
    return None


def model_side(model_independent: float, line: float) -> str:
    """Which side the numbers favour: home when the model read
    sits above the line, away when below. At exact equality the
    pick is a coin flip and this reports 'home' only as the
    tie-break model.py uses (edge 0 -> away PK is the stored
    string; the explanation says "on the line" instead)."""
    return "home" if model_independent > line else "away"


def explain_pick(game) -> dict:
    """One-line plain-English 'why' for one game, plus the
    clauses it was built from. Display only -- it never changes
    a pick, an edge, or a line, and it invents nothing: every
    clause names a number already stored for that game."""
    home = _text(game.get("home_team")) or "home"
    away = _text(game.get("away_team")) or "away"
    matchup = f"{away} @ {home}"
    model = _num(game.get("model_independent"))
    line = judged_line(game)
    factors: list[str] = []

    if line is None:
        return {"why": (f"No line yet for {matchup} -- no pick can be made "
                        "until a Vegas or pool line exists."),
                "factors": ["no line"]}
    if model is None:
        return {"why": (f"No model read yet for {matchup} -- the pick cannot "
                        "be explained until the ratings run."),
                "factors": ["no model read"]}

    line_source = "pool line" if _num(game.get("pool_spread")) is not None else "Vegas line"
    gap = model - line
    side = model_side(model, line)
    side_team = home if side == "home" else away
    if gap == 0:
        factors.append(f"model sits exactly on the {line_source} ({line:+.1f}) -- a coin flip")
    else:
        factors.append(
            f"model rates {side_team} {abs(gap):.1f} pts better than the "
            f"{line_source} ({line:+.1f})")

    # The stored pick string should name the same side the numbers
    # favour. If it does not, that is a data problem worth saying
    # out loud, not a reason to rewrite the explanation to match it.
    stored = pick_side(game)
    if stored is not None and stored != side and gap != 0:
        factors.append(
            f"stored pick ({_text(game.get('pick'))}) names the other side "
            "-- check the row before submitting")
    elif stored is None and _text(game.get("pick")):
        factors.append(f"stored pick ({_text(game.get('pick'))}) could not be matched to a team")

    raw_gap = abs(gap)
    if bool(game.get("needs_review")):
        factors.append(
            f"but the market disagrees by {raw_gap:.1f} pts -- verify the "
            "inputs before trusting this, do not bet bigger on it")
    elif bool(game.get("strong_edge")):
        factors.append(f"a strong model-vs-market gap ({raw_gap:.1f} pts)")

    cover_prob = _num(game.get("cover_prob"))
    if cover_prob is not None:
        if cover_prob < 0.55:
            factors.append(f"cover prob {cover_prob:.2f} -- near a coin flip")
        else:
            factors.append(f"cover prob {cover_prob:.2f}")
    cover_range = game.get("cover_prob_range")
    if cover_range is not None:
        try:
            lo, hi = float(cover_range[0]), float(cover_range[1])
            if lo <= 0.5 <= hi:
                factors.append(
                    "rating uncertainty alone could flip the favoured side "
                    f"(cover-prob range {lo:.2f}-{hi:.2f})")
        except (TypeError, ValueError, IndexError):
            pass

    confidence = _num(game.get("confidence"))
    if confidence is not None and confidence < 1.0:
        factors.append(
            f"early-season sample (confidence {confidence:.2f} -- one or "
            "both teams have few current-season games)")

    line_move = _num(game.get("line_move"))
    if line_move is not None and abs(line_move) >= 1.0:
        direction = "toward home" if line_move > 0 else "toward away"
        factors.append(
            f"live line has moved {abs(line_move):.1f} pts {direction} "
            "since the pool line locked -- check Thursday's line-moves section")

    injury_flags = _text(game.get("injury_flags"))
    if injury_flags:
        teams = sorted({part.split(":")[0].strip()
                        for part in injury_flags.split("|") if ":" in part})
        who = ", ".join(teams) if teams else "both teams"
        factors.append(
            f"injury report lists players for {who} -- review-only, "
            "not in the number")

    weather_note = _text(game.get("weather_note"))
    if weather_note:
        factors.append(f"weather: {weather_note} (totals only)")

    model_total = _num(game.get("model_total"))
    total_line = judged_total_line(game)
    total_pick = _text(game.get("total_pick"))
    if model_total is not None and total_line is not None and total_pick not in ("", "no line"):
        factors.append(
            f"total: model {model_total:.1f} vs line {total_line:g} ({total_pick})")

    why = "; ".join(factors)
    return {"why": why, "factors": factors}


def explain_picks(rows) -> list[dict]:
    """explain_pick() over an iterable of game mappings, in order.
    A row that raises is reported with its matchup and the error
    instead of breaking the whole table -- the dashboard renders
    the rest."""
    out = []
    for game in rows:
        try:
            out.append(explain_pick(game))
        except Exception as exc:  # noqa: BLE001 -- display path, never fatal
            out.append({"why": (f"Could not explain "
                                f"{_text(game.get('away_team'))} @ "
                                f"{_text(game.get('home_team'))}: {exc}"),
                        "factors": []})
    return out
