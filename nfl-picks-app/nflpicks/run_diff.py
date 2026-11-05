"""
What changed since the last run (batch 3, item 6): a Thursday
10 AM final run is only useful if the review focuses on movers,
not on re-reading the whole slate. This module diffs the previous
saved run for the same season/week against a new run's results,
across ratings, picks, lines, injuries, totals, and weather.

Why this is computed in memory, not from two saved slates: the
picks table is keyed by (season, week, matchup) and every run
OVERWRITES it, so after the 10 AM run saves, the 7 AM slate no
longer exists in the picks table to compare against. weekly_run
therefore captures the old state BEFORE its first save and diffs
against the new predictions in memory, then persists only the
diff itself (db.run_diffs, keyed by run_time, append-only). The
diff of run N is never overwritten by run N+1 -- each run appends
its own rows -- so the dashboard can always show the latest
run's movers even after later runs land.

What counts as a change (thresholds in config.py):
  * PICK_FLIP -- the model side changed (home <-> away). Always
    reported, whatever the edge size: a flip is a different
    submission, not a rounding wobble.
  * LINE_MOVE -- the Vegas (closing/live) or pool line moved by
    >= RUN_DIFF_MIN_LINE_MOVE points. The pool line is still
    never auto-switched; a move here is a prompt to check news,
    and the diff says which line moved.
  * EDGE_MOVE -- the blended edge moved by >=
    RUN_DIFF_MIN_EDGE_MOVE points without flipping the pick.
    (A flip already carries its edge change in its detail, so it
    is not double-reported as an edge move.)
  * RATING_MOVE -- a team's rating moved by >=
    RUN_DIFF_MIN_RATING_MOVE points. Team-level, not per game:
    one rating move explains several games' edge moves.
  * INJURY_NEW / INJURY_CHANGED / INJURY_CLEARED -- a player
    appeared on, changed status on, or disappeared from the
    injury review for a team. "Cleared" is reported, not hidden:
    a starter coming OFF the list Thursday morning is exactly
    the kind of mover a final review needs (the returning-
    starter check in weekly_run covers the same idea from the
    other direction; this is the raw list diff).
  * WEATHER_CHANGED -- the stored weather note changed
    (including blank -> note and note -> blank). Weather only
    moves totals, and the note says so.
  * TOTAL_CHANGE -- the total pick changed, or the model total
    moved by >= RUN_DIFF_MIN_TOTAL_MOVE points.
  * NEW_GAME / REMOVED_GAME -- a matchup appeared or vanished
    between runs (should not happen midweek; a schedule or
    line-posting change if it does).

Sorting: flips first, then line moves, injury changes, edge
moves, rating moves, weather, totals, schedule changes; within
a category, largest magnitude first, then matchup. Stopping
early still covers the most consequential movers.

Inputs are plain dicts so the pure builder is testable without
a database. Pick dicts: {"pick", "edge", "model_independent",
"vegas_spread", "pool_spread", "cover_prob", "weather_note",
"total_pick", "model_total"} (all optional except as noted in
code). Rating dicts: {team: rating}. Injury rows: dicts with
team/player/status keys, or tuples whose first four elements
are (team, player, position, status) -- the shape
db.save_injury_review stores. Line dicts: {(home, away,
source): line} with source in ("pool", "closing").
"""
from __future__ import annotations

from . import config

CATEGORY_RANK = {
    "PICK_FLIP": 0,
    "LINE_MOVE": 1,
    "INJURY_NEW": 2,
    "INJURY_CHANGED": 2,
    "INJURY_CLEARED": 2,
    "EDGE_MOVE": 3,
    "RATING_MOVE": 4,
    "WEATHER_CHANGED": 5,
    "TOTAL_CHANGE": 6,
    "NEW_GAME": 7,
    "REMOVED_GAME": 7,
}


def _threshold(name: str, default: float) -> float:
    return float(getattr(config, name, default))


def pick_side(pick: str | None, home: str, away: str) -> str | None:
    """Which side a pick string backs: "home", "away", or None
    (no pick, or an unparseable string -- never guessed). Pick
    strings are "{team} {line}" (e.g. "KC -3.5", "KC PK"), so the
    team is the first whitespace-separated token."""
    if not pick:
        return None
    team = str(pick).strip().split()[0] if str(pick).strip() else ""
    if team == home:
        return "home"
    if team == away:
        return "away"
    return None


def _fmt(value: float | None) -> str:
    if value is None:
        return "none"
    return f"{float(value):+.1f}"


def _normalize_injuries(rows) -> dict[tuple[str, str], str]:
    """{(team, player): status} from dict rows or tuples. Rows
    without a team/player are skipped, never invented."""
    out: dict[tuple[str, str], str] = {}
    for r in rows or []:
        if isinstance(r, dict):
            team, player, status = r.get("team"), r.get("player"), r.get("status")
        else:
            try:
                team, player, status = r[0], r[1], r[3]
            except (IndexError, TypeError):
                continue
        if team and player:
            out[(str(team), str(player))] = str(status or "")
    return out


def build_diff(old_picks: dict, new_picks: dict,
               old_ratings: dict, new_ratings: dict,
               old_injuries, new_injuries,
               old_lines: dict, new_lines: dict) -> dict:
    """Pure diff. old_picks/new_picks are keyed by (home, away);
    ratings by team; lines by (home, away, source). Returns
    {"items", "n_changes", "summary", "note"} with items sorted
    by (category rank, -magnitude, matchup). An empty old state
    (first saved run for the week) yields zero items and a note
    saying so -- it is not an error, and it must not be read as
    "nothing changed".
    """
    items: list[dict] = []

    def add(matchup, category, detail, magnitude, home=None, away=None):
        items.append({
            "matchup": matchup, "category": category, "detail": detail,
            "magnitude": round(float(magnitude), 2),
            "home": home, "away": away,
        })

    if not old_picks and not old_ratings and not old_injuries and not old_lines:
        return {"items": [], "n_changes": 0, "summary": "First saved run for this week -- nothing to compare against.",
                "note": ("No previous run was saved for this season/week, so there is no "
                         "earlier slate to diff against. This run becomes the baseline "
                         "for the next run's diff.")}

    edge_min = _threshold("RUN_DIFF_MIN_EDGE_MOVE", 0.5)
    rating_min = _threshold("RUN_DIFF_MIN_RATING_MOVE", 0.5)
    line_min = _threshold("RUN_DIFF_MIN_LINE_MOVE", 0.5)
    total_min = _threshold("RUN_DIFF_MIN_TOTAL_MOVE", 0.5)

    # ---- Games: flips, lines, edges, totals, weather ----
    for key in sorted(set(old_picks) | set(new_picks)):
        home, away = key
        matchup = f"{away} @ {home}"
        old, new = old_picks.get(key), new_picks.get(key)
        if old is None:
            add(matchup, "NEW_GAME", f"New matchup in this run (pick: {new.get('pick')}).",
                0.0, home, away)
            continue
        if new is None:
            add(matchup, "REMOVED_GAME", "Matchup from the previous run is gone in this run.",
                0.0, home, away)
            continue
        old_side, new_side = pick_side(old.get("pick"), home, away), pick_side(new.get("pick"), home, away)
        if old_side is not None and new_side is not None and old_side != new_side:
            add(matchup, "PICK_FLIP",
                f"Pick flipped: {old.get('pick')} -> {new.get('pick')} "
                f"(edge {_fmt(old.get('edge'))} -> {_fmt(new.get('edge'))}).",
                abs((new.get("edge") or 0.0) - (old.get("edge") or 0.0)), home, away)
        else:
            old_edge, new_edge = old.get("edge"), new.get("edge")
            if old_edge is not None and new_edge is not None and abs(new_edge - old_edge) >= edge_min:
                add(matchup, "EDGE_MOVE",
                    f"Edge moved: {_fmt(old_edge)} -> {_fmt(new_edge)} "
                    f"(pick {new.get('pick')}, unchanged side).",
                    abs(new_edge - old_edge), home, away)
        for source, label in (("vegas_spread", "Vegas"), ("pool_spread", "Pool")):
            old_line, new_line = old.get(source), new.get(source)
            if old_line is not None and new_line is not None and abs(new_line - old_line) >= line_min:
                add(matchup, "LINE_MOVE",
                    f"{label} line moved: {_fmt(old_line)} -> {_fmt(new_line)} "
                    f"(nflverse convention; pool line is still the judged line).",
                    abs(new_line - old_line), home, away)
        old_total, new_total = old.get("model_total"), new.get("model_total")
        old_tpick, new_tpick = old.get("total_pick"), new.get("total_pick")
        if old_tpick != new_tpick or (
                old_total is not None and new_total is not None
                and abs(new_total - old_total) >= total_min):
            add(matchup, "TOTAL_CHANGE",
                f"Total: pick {old_tpick} -> {new_tpick}, model total "
                f"{_fmt(old_total)} -> {_fmt(new_total)}.",
                abs((new_total or 0.0) - (old_total or 0.0)) if old_total is not None and new_total is not None else 0.0,
                home, away)
        old_w, new_w = (old.get("weather_note") or "").strip(), (new.get("weather_note") or "").strip()
        if old_w != new_w:
            add(matchup, "WEATHER_CHANGED",
                f"Weather note: '{old_w or 'none'}' -> '{new_w or 'none'}' (totals only).",
                0.0, home, away)

    # ---- Line snapshots not reflected in the picks dicts ----
    # Picks carry the same Vegas/pool lines weekly_run snapshots,
    # so snapshot-only moves are already covered above; this loop
    # only catches a snapshot source the picks dicts never had
    # (defensive -- a snapshot with no pick row cannot flip a pick,
    # but a Thursday reviewer still wants to know the line moved).
    # It is intentionally NOT double-reporting pool/closing moves
    # already emitted from the picks loop: snapshot keys are
    # (home, away, source) with source "pool"/"closing", and the
    # picks loop covers both, so nothing is added here unless a
    # snapshot exists for a matchup with no pick row at all.
    for key in sorted(set(old_lines) | set(new_lines)):
        home, away, source = key
        if (home, away) in old_picks or (home, away) in new_picks:
            continue
        old_line, new_line = old_lines.get(key), new_lines.get(key)
        if old_line is not None and new_line is not None and abs(new_line - old_line) >= line_min:
            add(f"{away} @ {home}", "LINE_MOVE",
                f"{source} line snapshot moved: {_fmt(old_line)} -> {_fmt(new_line)} "
                f"(no pick row for this matchup).",
                abs(new_line - old_line), home, away)

    # ---- Ratings: team-level ----
    for team in sorted(set(old_ratings) | set(new_ratings)):
        old_r, new_r = old_ratings.get(team), new_ratings.get(team)
        if old_r is None or new_r is None:
            continue
        if abs(new_r - old_r) >= rating_min:
            add(team, "RATING_MOVE", f"{team} rating {_fmt(old_r)} -> {_fmt(new_r)} "
                f"({new_r - old_r:+.1f}).", abs(new_r - old_r))

    # ---- Injuries: player-level ----
    old_inj = _normalize_injuries(old_injuries)
    new_inj = _normalize_injuries(new_injuries)
    for (team, player) in sorted(set(old_inj) | set(new_inj)):
        old_s, new_s = old_inj.get((team, player)), new_inj.get((team, player))
        if old_s is None:
            add(f"{team} {player}", "INJURY_NEW",
                f"{team} {player} newly listed: {new_s or 'listed'}.", 0.0)
        elif new_s is None:
            add(f"{team} {player}", "INJURY_CLEARED",
                f"{team} {player} no longer listed (was {old_s}).", 0.0)
        elif old_s != new_s:
            add(f"{team} {player}", "INJURY_CHANGED",
                f"{team} {player} status: {old_s} -> {new_s}.", 0.0)

    items.sort(key=lambda i: (CATEGORY_RANK.get(i["category"], 99),
                              -i["magnitude"], i["matchup"]))
    by_cat: dict[str, int] = {}
    for i in items:
        by_cat[i["category"]] = by_cat.get(i["category"], 0) + 1
    summary = (", ".join(f"{n} {c.lower().replace('_', ' ')}" for c, n in
                         sorted(by_cat.items(), key=lambda kv: CATEGORY_RANK.get(kv[0], 99)))
               ) if by_cat else "No changes above the reporting thresholds."
    return {"items": items, "n_changes": len(items), "summary": summary,
            "note": ("Changes vs the previous saved run for this season/week. "
                     "A change is a prompt to re-check that game before submitting, "
                     "never an auto-switch: the pool line stays the judged line.")}
