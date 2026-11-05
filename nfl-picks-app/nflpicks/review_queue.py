"""
Thursday review queue (batch item 3b): ONLY flagged games, prioritised
so a Thursday-morning review is time-boxed.

A game enters the queue only if at least one of these fires:
  MODEL_REVIEW   -- |raw model-vs-line deviation| >= SANITY_EDGE_FLAG
                     (the model's read is implausibly far from the market)
  DISAGREEMENT   -- |raw deviation| >= EDGE_THRESHOLD but below sanity
                     (a strong lean, worth a second look, not an alarm)
  LINE_MOVE      -- pool line vs closing/live line moved >=
                     REVIEW_LINE_MOVE_THRESHOLD (db line snapshots)
  EDGE_FLIP      -- the model edge changes sign between the pool line
                     and the closing/live line (same snapshots)
  INJURY         -- a critical/long-term/OUT-or-DOUBTFUL-starter injury
                     note, or a returning starter, for either team
  WEATHER        -- a weather note that actually changed the total
                     (model.py only stores a note when it applied one)
  CROSSCHECK     -- a market cross-check flag for the game (diagnostic)

Unflagged games are deliberately absent: reviewing all games equally
is what makes Thursday morning unbounded. Priority is P1 (check before
submitting: MODEL_REVIEW, EDGE_FLIP, or a CRITICAL injury note) then
P2 (everything else flagged). Within a priority, biggest |raw
deviation| first, then biggest line move, then matchup -- so the top
of the queue is always the most consequential check, and stopping
early still covers the worst items.

Two entry points: build_review_queue_from_games() is the pure,
testable core over per-game dicts; build_review_queue() is the DB
wrapper that assembles those dicts from picks, line_snapshots (via
db.get_line_moves), market_crosschecks, injury_review and
injury_returns for one season/week. Neither changes a pick or a line:
a queued game is a manual check, never an auto-switch.
"""
from __future__ import annotations

from . import config, db

CRITICAL_MARKER = "CRITICAL"


def _raw_deviation(game: dict) -> float | None:
    model = game.get("model_independent")
    line = game.get("line")
    if line is None:
        line = game.get("pool_spread")
    if line is None:
        line = game.get("vegas_spread")
    if model is None or line is None:
        return None
    return float(model) - float(line)


def build_review_queue_from_games(games: list[dict]) -> dict:
    """Pure queue builder. Per-game dict keys (all optional except
    home/away): pick, model_independent, line (effective nflverse
    line; else pool_spread/vegas_spread), line_move {movement,
    edge_flip}, injury_notes [str], weather_note str,
    crosscheck_flags [str]. Returns {"items", "n_flagged",
    "n_total", "note"} with items sorted by (priority, -|deviation|,
    -movement, matchup)."""
    items = []
    for game in games:
        home, away = game.get("home"), game.get("away")
        reasons: list[str] = []
        dev = _raw_deviation(game)
        abs_dev = abs(dev) if dev is not None else 0.0
        if dev is not None and abs_dev >= float(config.SANITY_EDGE_FLAG):
            reasons.append(f"MODEL_REVIEW: model vs line gap {dev:+.1f} pts "
                           f"(>= sanity flag {config.SANITY_EDGE_FLAG:g}) -- verify inputs, do not trust blindly")
        elif dev is not None and abs_dev >= float(config.EDGE_THRESHOLD):
            reasons.append(f"DISAGREEMENT: model vs line gap {dev:+.1f} pts (strong lean)")
        move = game.get("line_move") or {}
        movement = move.get("movement")
        edge_flip = bool(move.get("edge_flip"))
        if movement is not None and float(movement) >= float(config.REVIEW_LINE_MOVE_THRESHOLD):
            reasons.append(f"LINE_MOVE: pool vs closing line moved {float(movement):.1f} pts "
                           f"(pool {move.get('pool_line')}, closing {move.get('closing_line')}) -- "
                           "check for news the market learned; pool line stays the judged line")
        if edge_flip:
            reasons.append("EDGE_FLIP: model edge changes sign between pool line and closing line -- "
                           "the pick is line-dependent, re-check before submitting")
        injury_notes = [str(n) for n in (game.get("injury_notes") or []) if str(n).strip()]
        for note in injury_notes:
            reasons.append(f"INJURY: {note}")
        weather_note = (game.get("weather_note") or "").strip()
        if weather_note:
            reasons.append(f"WEATHER: {weather_note} (total only -- spread unaffected)")
        for flag in (game.get("crosscheck_flags") or []):
            if str(flag).strip():
                reasons.append(f"CROSSCHECK: {flag}")
        if not reasons:
            continue
        # Case-insensitive: DB flag_note spells it "REVIEW -- critical
        # position" (lowercase), weekly-run flag strings use "CRITICAL".
        priority = 1 if (
            any(r.startswith("MODEL_REVIEW") for r in reasons)
            or any(r.startswith("EDGE_FLIP") for r in reasons)
            or any(CRITICAL_MARKER.lower() in r.lower() for r in reasons)
        ) else 2
        items.append({
            "home": home, "away": away, "matchup": f"{away} @ {home}",
            "pick": game.get("pick"), "priority": priority,
            "reasons": reasons, "raw_deviation": None if dev is None else round(dev, 2),
            "line_movement": None if movement is None else round(float(movement), 2),
        })
    items.sort(key=lambda i: (i["priority"], -(abs(i["raw_deviation"]) if i["raw_deviation"] is not None else 0.0),
                            -(i["line_movement"] or 0.0), i["matchup"]))
    return {
        "items": items, "n_flagged": len(items), "n_total": len(games),
        "note": (f"{len(items)} of {len(games)} games flagged for Thursday review "
                 "(unflagged games are omitted on purpose). P1 = check before "
                 "submitting; P2 = check if time. Nothing here changes a pick or a line."),
    }


def _injury_notes_for_game(injury_rows, return_rows, home: str, away: str) -> list[str]:
    notes = []
    for r in injury_rows:
        if r["team"] not in (home, away):
            continue
        flag_note = (r["flag_note"] or "").strip()
        status = (r["status"] or "").strip().upper()
        depth = (r["depth_status"] or "").strip()
        is_starter = depth == "Starter"
        if flag_note or (status in ("OUT", "DOUBTFUL") and is_starter):
            notes.append(f"{r['team']} {r['player']} ({r['position']}, {status}"
                         f"{', ' + flag_note if flag_note else ''})")
    for r in return_rows:
        if r["team"] in (home, away):
            notes.append(f"Returning starter: {r['team']} {r['player']} ({r['position']})")
    return notes


def build_review_queue(season: int, week: int, db_path: str | None = None) -> dict:
    """DB wrapper: assemble per-game dicts for one season/week and
    run the pure builder. Missing tables/rows degrade to fewer reasons,
    never to an invented flag: a game with no snapshots, cross-checks
    or injury rows simply contributes no reason from that source."""
    with db.connect(db_path) as conn:
        picks = conn.execute(
            "SELECT * FROM picks WHERE season=? AND week=?", (season, week)).fetchall()
        try:
            injuries = conn.execute(
                "SELECT * FROM injury_review WHERE season=? AND week=?", (season, week)).fetchall()
        except Exception:
            injuries = []
        try:
            returns = conn.execute(
                "SELECT * FROM injury_returns WHERE season=? AND week=?", (season, week)).fetchall()
        except Exception:
            returns = []
        try:
            crosschecks = conn.execute(
                "SELECT * FROM market_crosschecks WHERE season=? AND week=?", (season, week)).fetchall()
        except Exception:
            crosschecks = []
    moves = {}
    try:
        for m in db.get_line_moves(season, week, db_path=db_path,
                                   move_threshold=float(config.REVIEW_LINE_MOVE_THRESHOLD)):
            moves[(m["home_team"], m["away_team"])] = m
    except Exception:
        moves = {}
    # Edge flips with a sub-threshold move do not appear in
    # get_line_moves' output only if neither condition fires -- but
    # get_line_moves DOES include edge flips regardless of movement
    # (its condition is movement OR flip), so the map above already
    # covers flips. A game whose move is below threshold and has no
    # flip correctly contributes nothing here.
    cc_flags = {}
    for r in crosschecks:
        flags = [f for f in (r["flags"] or "").split(";") if f.strip()]
        if flags:
            cc_flags[(r["home_team"], r["away_team"])] = flags
    games = []
    for p in picks:
        home, away = p["home_team"], p["away_team"]
        pool_spread, vegas_spread = p["pool_spread"], p["vegas_spread"]
        games.append({
            "home": home, "away": away, "pick": p["pick"],
            "model_independent": p["model_independent"],
            "line": pool_spread if pool_spread is not None else vegas_spread,
            "pool_spread": pool_spread, "vegas_spread": vegas_spread,
            "line_move": moves.get((home, away)),
            "injury_notes": _injury_notes_for_game(injuries, returns, home, away),
            "weather_note": (p["weather_note"] or "").strip(),
            "crosscheck_flags": cc_flags.get((home, away), []),
        })
    out = build_review_queue_from_games(games)
    out.update({"season": int(season), "week": int(week)})
    return out
