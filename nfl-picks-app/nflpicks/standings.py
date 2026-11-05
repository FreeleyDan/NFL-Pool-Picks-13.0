"""
Standings-aware risk (batch 3, item 1): LABEL-ONLY gamble suggestions.

The pool pays a weekly winner AND a season-long winner on the same
picks, so late in the season the season-optimal slate (highest
calibrated cover probability on every game) is not always the
standings-optimal slate:

* Trailing late, you need variance: a slate that mirrors the leader
  can tie the leader, it can never pass them. Flipping a few coin-flip
  games to the other side creates the differentiation that can close
  a gap -- at a small expected-win cost, because a coin flip costs
  almost nothing to flip (cost = p_default - p_gamble, ~0.00-0.10 wins
  per game at p <= 0.55).
* Leading late, you want the opposite: mirror the default slate and
  protect strong leans. Any contrarian flip can only shrink a lead
  in expectation, so no gamble is suggested, and strong leans are
  explicitly labelled PROTECT.
* Early in the season (weeks_left > config.STANDINGS_LATE_WEEKS_LEFT)
  the rule is OFF / neutral no matter the standings: with 5+ weeks
  left, default variance over 80+ games usually closes a small gap,
  and gambling early just burns expected wins for a weekly prize that
  does not help the season race.

What this module NEVER does: it never changes a pick. It takes a
week's default picks (with their calibrated cover probabilities, as
produced by model.py / calibration.py) and returns the same picks
plus a label per game -- GAMBLE (suggested flip, with the gamble side
and its expected cost), PROTECT (strong lean while leading late), or
DEFAULT -- and a note stating the mode. The caller (dashboard, weekly
run, or the user) decides whether to act on a GAMBLE label. There is
no consensus input anywhere: "leader" here is just two numbers the
user enters (user wins, leader wins), not field data, and the
validation in evaluate_late_window() assumes the leader makes default
picks -- if the real leader picks contrarian, mirroring the default
does not guarantee holding a lead, and that limit is stated wherever
the rule is reported.

Inputs (small, user-entered -- there is no standings feed):
  user_wins, leader_wins: season ATS wins so far (pushes count as
    neither a win nor a loss, matching harness ATS% which excludes
    pushes; if your pool scores a push as half a win, enter wins
    accordingly and the rule still works, it just sees a smaller gap).
  weeks_left: weeks remaining INCLUDING the current week (so the
    final week is weeks_left == 1, and the last 4 weeks are 4, 3, 2, 1).

Games input to suggest_gambles(): a list of dicts, one per game:
  {"home": "KC", "away": "DEN", "default_side": "home",
   "pick_prob": 0.53}
where default_side is "home"/"away" (the default pick's side) and
pick_prob is the CALIBRATED probability that the default pick covers
(calibration.cover_probability_for_pick). pick_prob may be below 0.5
-- that honestly means the default side is not favoured after
shrinkage; such a game is still a coin flip (best side <= 0.55) and
flipping it has a NEGATIVE expected cost (it gains expected wins),
which the label shows rather than hiding.

All thresholds live in config.py (STANDINGS_*); this module reads
them at call time.
"""
from __future__ import annotations

from . import config


def _thresholds() -> dict:
    return {
        "late_weeks": int(config.STANDINGS_LATE_WEEKS_LEFT),
        "trail": int(config.STANDINGS_TRAIL_GAMES),
        "last_week_trail": int(config.STANDINGS_LAST_WEEK_TRAIL_GAMES),
        "lead": int(config.STANDINGS_LEAD_GAMES),
        "coin_flip_max": float(config.STANDINGS_COIN_FLIP_MAX_PROB),
        "strong_min": float(config.STANDINGS_STRONG_LEAN_MIN_PROB),
        "max_gambles": int(config.STANDINGS_MAX_GAMBLES_PER_WEEK),
    }


def standings_mode(user_wins: int, leader_wins: int, weeks_left: int) -> dict:
    """Classify the standings state. Modes:

    "early" -- weeks_left above the late threshold: rule off, no
      gambles, regardless of deficit/lead.
    "trailing" -- late and behind by >= TRAIL_GAMES, or behind by
      >= LAST_WEEK_TRAIL_GAMES in the final week: gambles allowed.
      (Final-week note: trailing by 1, mirroring loses outright, so
      differentiation is the only route even to a tie.)
    "leading" -- late and ahead by >= LEAD_GAMES: protect mode, no
      gambles, strong leans labelled PROTECT.
    "tied_late" -- late and tied: neutral, no gambles. (Tied is not
      trailing: the tie-break / split rule for a season tie is not
      known, so this module does not gamble to break a tie on an
      invented tie-break.)
    "done" -- weeks_left == 0: season over, nothing to suggest.
    """
    if weeks_left < 0 or user_wins < 0 or leader_wins < 0:
        raise ValueError("user_wins, leader_wins and weeks_left must be non-negative")
    t = _thresholds()
    deficit = int(leader_wins) - int(user_wins)  # > 0 means trailing
    if weeks_left == 0:
        mode, late = "done", False
    elif weeks_left > t["late_weeks"]:
        mode, late = "early", False
    elif deficit >= t["trail"] or (weeks_left == 1 and deficit >= t["last_week_trail"]):
        mode, late = "trailing", True
    elif deficit <= -t["lead"]:
        mode, late = "leading", True
    else:
        mode, late = "tied_late", True
    return {"mode": mode, "late": late, "deficit": deficit,
            "lead": -deficit, "weeks_left": int(weeks_left)}


def _validate_game(game: dict) -> tuple[str, float]:
    side = game.get("default_side")
    if side not in ("home", "away"):
        raise ValueError(f"default_side must be 'home'/'away', got {side!r}")
    p = float(game["pick_prob"])
    if not 0.0 <= p <= 1.0:
        raise ValueError(f"pick_prob must be in [0, 1], got {p}")
    return side, p


def suggest_gambles(games: list[dict], user_wins: int, leader_wins: int,
                    weeks_left: int) -> dict:
    """Label one week's default picks. Returns a dict with:

    "mode" (see standings_mode), "games" (one labelled dict per input
    game, in input order, each carrying its default_side UNCHANGED
    plus label / gamble_side / gamble_prob / expected_cost / reason),
    "gambles" (the GAMBLE-labelled games only, cheapest first),
    "total_expected_cost" (sum of expected-win costs of the gambles;
    negative means the flips gain expected wins -- that happens when
    a default pick's calibrated prob is below 0.5),
    "note" (one plain-language line stating what the rule did and
    that default picks are unchanged).
    """
    t = _thresholds()
    state = standings_mode(user_wins, leader_wins, weeks_left)
    mode = state["mode"]
    labelled: list[dict] = []
    for game in games:
        side, p = _validate_game(game)
        best = max(p, 1.0 - p)
        entry = {"home": game.get("home"), "away": game.get("away"),
                 "default_side": side, "pick_prob": p,
                 "gamble_side": None, "gamble_prob": None,
                 "expected_cost": None, "label": "DEFAULT", "reason": ""}
        if mode == "trailing" and best <= t["coin_flip_max"]:
            # Candidate gamble: cost is what flipping gives up in
            # expected wins (negative = flipping gains expected wins).
            entry["gamble_side"] = "away" if side == "home" else "home"
            entry["gamble_prob"] = round(1.0 - p, 3)
            entry["expected_cost"] = round(p - (1.0 - p), 3)
            entry["label"] = "GAMBLE_CANDIDATE"
            entry["reason"] = ("trailing late: coin-flip game, flipping "
                               "it creates differentiation at low cost")
        elif mode == "leading" and p >= t["strong_min"]:
            entry["label"] = "PROTECT"
            entry["reason"] = ("leading late: strong lean, keep the "
                               "default pick -- do not flip it")
        elif mode == "early":
            entry["reason"] = "early season: standings rule off, default pick stands"
        elif mode == "done":
            entry["reason"] = "season over: no suggestion"
        elif mode == "tied_late":
            entry["reason"] = "tied late: no gamble suggested (tie-break unknown)"
        elif mode == "trailing":
            entry["reason"] = "trailing late: not a coin flip, default pick stands"
        else:  # leading, non-strong game
            entry["reason"] = "leading late: default pick stands"
        labelled.append(entry)
    # Trailing mode: keep only the cheapest candidates, up to the cap.
    # Cheapest = lowest expected cost; ties broken by lowest best-side
    # prob (closest to a true 50/50) for determinism.
    gambles: list[dict] = []
    if mode == "trailing":
        candidates = [g for g in labelled if g["label"] == "GAMBLE_CANDIDATE"]
        candidates.sort(key=lambda g: (g["expected_cost"], max(g["pick_prob"], 1.0 - g["pick_prob"])))
        kept = candidates[:t["max_gambles"]]
        kept_ids = {id(g) for g in kept}
        for g in labelled:
            if g["label"] == "GAMBLE_CANDIDATE":
                if id(g) in kept_ids:
                    g["label"] = "GAMBLE"
                else:
                    g["label"] = "DEFAULT"
                    g["gamble_side"] = None
                    g["gamble_prob"] = None
                    g["expected_cost"] = None
                    g["reason"] = ("trailing late: coin flip, but over the "
                                   f"per-week gamble cap ({t['max_gambles']}) -- "
                                   "cheaper flips were suggested instead")
        gambles = kept
    total_cost = round(sum(g["expected_cost"] for g in gambles), 3) if gambles else 0.0
    if mode == "early":
        note = (f"Early season ({weeks_left} weeks left): standings rule off -- "
                "default picks unchanged, no gambles suggested.")
    elif mode == "trailing":
        note = (f"Trailing late (deficit {state['deficit']}, {weeks_left} weeks left): "
                f"{len(gambles)} gamble(s) suggested, total expected cost "
                f"{total_cost} wins. Default picks are unchanged -- a GAMBLE "
                "label is a suggestion to flip, not a changed pick.")
    elif mode == "leading":
        note = (f"Leading late (lead {state['lead']}, {weeks_left} weeks left): "
                "protect mode -- no gambles suggested, strong leans labelled PROTECT.")
    elif mode == "tied_late":
        note = ("Tied late: no gambles suggested (tie-break unknown); "
                "default picks unchanged.")
    else:
        note = "Season over: no suggestions."
    return {"mode": mode, "deficit": state["deficit"], "weeks_left": weeks_left,
            "games": labelled, "gambles": gambles,
            "total_expected_cost": total_cost, "note": note}


def evaluate_late_window(rows: list[dict], deficit: int) -> dict:
    """What the standings rule WOULD have done on historical late-season
    rows, vs the default slate. This is the validation harness for the
    rule, kept separate from the live suggestion path.

    rows: dicts with "season", "week", "pick_prob" (calibrated prob
      the default pick covers) and "ats" ("correct"/"wrong"/"push":
      the ACTUAL outcome of the default pick). Backtest rows from
      backtest.evaluate_variant() plus a pick_prob column qualify --
      see the README batch-3 section for the script shape.
    deficit: starting gap at the late window's first week, as
      leader_wins - user_wins (positive = trailing, negative = leading).
      The user's pre-window wins are their actual default-pick wins
      before the window; the leader starts at user + deficit and is
      assumed to make DEFAULT picks every late week (the key limit:
      a real leader who picks contrarian breaks this assumption).

    Returns per-season results plus an aggregate: gambles suggested,
    their summed expected cost, actual late-window wins for the
    gambled slate vs the default slate, and whether each slate finished
    ahead of the leader (overtake) or not. A single historical season
    is one realization -- 7 seasons is far too few to prove the rule
    helps, so the aggregate is reported with that warning, not as a
    win claim. See simulation.py for the smoother Monte Carlo version
    of the same question.
    """
    t = _thresholds()
    by_season: dict[int, list[dict]] = {}
    for r in rows:
        by_season.setdefault(int(r["season"]), []).append(r)
    seasons_out: dict[str, dict] = {}
    agg = {"seasons": 0, "gambles": 0, "expected_cost": 0.0,
           "gambled_wins": 0, "default_wins": 0,
           "overtake_gambled": 0, "overtake_default": 0,
           "late_games": 0}
    for season, srows in sorted(by_season.items()):
        max_week = max(int(r["week"]) for r in srows)
        pre = [r for r in srows if max_week - int(r["week"]) + 1 > t["late_weeks"]]
        late = [r for r in srows if max_week - int(r["week"]) + 1 <= t["late_weeks"]]
        user_pre = sum(1 for r in pre if r["ats"] == "correct")
        # Shift both starting totals up if a synthetic deficit would
        # otherwise make the leader's total negative (standings_mode
        # rejects negative wins, and only the GAP matters to the rule).
        shift = max(0, -(user_pre + int(deficit)), -user_pre)
        start_user = user_pre + shift
        user_g, leader, user_d = start_user, start_user + int(deficit), start_user
        gambles_n, cost = 0, 0.0
        weeks = sorted({int(r["week"]) for r in late})
        for week in weeks:
            wrows = [r for r in late if int(r["week"]) == week]
            weeks_left = max_week - week + 1
            sugg = suggest_gambles(
                [{"home": r.get("home_team"), "away": r.get("away_team"),
                  "default_side": "home", "pick_prob": float(r["pick_prob"])}
                 for r in wrows],
                user_g, leader, weeks_left)
            # A gamble flips this game's actual outcome (correct <->
            # wrong); pushes stay pushes for both slates. The leader
            # always takes the default outcome. Gamble identity is by
            # position within the week (suggest_gambles returns games
            # in input order, so index back into wrows).
            gamble_idx = {i for i, g in enumerate(sugg["games"]) if g["label"] == "GAMBLE"}
            gambles_n += len(gamble_idx)
            cost += sum(g["expected_cost"] for g in sugg["gambles"])
            for i, r in enumerate(wrows):
                default_win = 1 if r["ats"] == "correct" else 0
                leader += default_win
                user_d += default_win
                if i in gamble_idx:
                    user_g += 0 if r["ats"] == "correct" else (1 if r["ats"] == "wrong" else 0)
                else:
                    user_g += default_win
        seasons_out[str(season)] = {
            "late_games": len(late), "gambles": gambles_n,
            "expected_cost": round(cost, 3),
            "gambled_wins": user_g - start_user, "default_wins": user_d - start_user,
            "overtake_gambled": user_g > leader, "overtake_default": user_d > leader,
            "final_gap_gambled": user_g - leader, "final_gap_default": user_d - leader,
        }
        agg["seasons"] += 1
        agg["gambles"] += gambles_n
        agg["expected_cost"] = round(agg["expected_cost"] + cost, 3)
        agg["gambled_wins"] += user_g - start_user
        agg["default_wins"] += user_d - start_user
        agg["overtake_gambled"] += int(user_g > leader)
        agg["overtake_default"] += int(user_d > leader)
        agg["late_games"] += len(late)
    agg["actual_wins_delta"] = agg["gambled_wins"] - agg["default_wins"]
    return {"by_season": seasons_out, "aggregate": agg,
            "assumption": "leader makes default picks every late week; "
                          "pushes count as neither a win nor a loss"}
