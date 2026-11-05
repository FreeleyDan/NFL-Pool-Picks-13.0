"""
Pool-standings importer (batch 4, group D, item 9).

The problem: standings.py and simulation.py already exist, but
they run on numbers the user types into a dashboard form each
time -- there is no stored record of the user's ACTUAL pool
standings (his wins, the leader's wins, weeks remaining), so a
gamble can never be priced against his real position from saved
data, and a typed number is never checked against a previous
week's import. This module is the importer that gap was missing.
It follows the pool_results.py pattern exactly (its own table,
created on first use, so db.py is untouched; upsert on
(season, week); last-row-wins duplicates; rejected rows never
reach the DB; empty state, never an invented record).

CSV format (header required; leader_name and note optional):

    season,week,user_wins,leader_wins,weeks_left,leader_name,note
    2026,10,118,120,4,,after week 9 results posted
    2026,11,124,125,3,,

Semantics -- read before writing a file:

* season/week: integers. week is the week the snapshot is FOR
  (the upcoming week whose gambles it prices), so a snapshot
  imported on Tuesday prices that week's slate.
* user_wins / leader_wins: season ATS wins so far (pushes count
  as neither, matching standings.py). leader_wins is the CURRENT
  leader's wins, not the second-place total and not a field
  average -- standings.py and simulation.py only model one
  leader, and inventing a field from a single leader number
  would be a different model. If the user IS the leader,
  leader_wins == user_wins is the tied/leading input; do not
  enter the second-place total as "leader" -- that would flip
  the rule into trailing mode on false data.
* weeks_left: weeks remaining INCLUDING the current week, so
  the final week is 1 and the last 4 weeks are 4, 3, 2, 1 --
  the same definition standings.py uses. A weeks_left of 0
  means the season is over (standings_mode "done").
* leader_name / note: stored, never graded. A snapshot with a
  note like "leader flipped on Thursday review" is still just
  one snapshot for that week.

What this module is NOT:

* It does not invent standings. With no imported rows for a
  season, every function here returns the empty state
  (status "empty", no wins, no deficit) -- not 0-0 and not a
  deficit of 0, either of which would misread as "tied".
* It is not a field-consensus input: there is no per-entrant
  table here, because standings.py / simulation.py model a
  single leader. If a future module models the whole field,
  it should extend this table, not reinterpret these rows.
* It does not change a pick. `price_week_gambles()` labels
  the week's default picks (standings.suggest_gambles) and
  prices those labels with simulation.compare_gambles -- both
  label-only, exactly like their inputs. The simulation part
  is explicitly ONE-WEEK pricing: compare_gambles over only
  this week's games reports P(ahead after this week) and the
  expected-win cost of this week's flips, NOT season-end
  P(first). Full-season pricing still needs cover
  probabilities for the whole remaining schedule (see README
  batch-3, standings wiring), which this database does not
  store; presenting a one-week number as a season-end number
  would be the misstatement this paragraph exists to prevent.

Games input to price_week_gambles(): one dict per game in
the week's slate, in the standings.py shape PLUS the
simulation shape -- {"home", "away", "default_side",
"pick_prob", "p_home"} where p_home is the calibrated
probability the HOME side covers (from
calibration.cover_probability_home). If p_home is missing
it is derived from default_side + pick_prob (home pick ->
p_home = pick_prob, away pick -> p_home = 1 - pick_prob).
leader_side is the default side throughout: the leader is
assumed to make default picks, the same assumption
standings.evaluate_late_window() states and the same limit
applies (a real leader who picks contrarian breaks it).
"""
from __future__ import annotations

import csv
import io
from datetime import datetime

from . import config, db, simulation, standings

TABLE_SQL = """
CREATE TABLE IF NOT EXISTS pool_standings (
    season INTEGER, week INTEGER, user_wins INTEGER, leader_wins INTEGER,
    weeks_left INTEGER, leader_name TEXT, note TEXT, updated_at TEXT,
    PRIMARY KEY (season, week)
);
"""

SAMPLE_CSV = """season,week,user_wins,leader_wins,weeks_left,leader_name,note
2026,10,118,120,4,,SAMPLE ONLY -- not real standings
2026,11,124,125,3,,SAMPLE ONLY -- not real standings
"""


def ensure_table(db_path: str | None = None):
    with db.connect(db_path) as conn:
        conn.executescript(TABLE_SQL)


def _to_int(value) -> int | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(text)
    except (TypeError, ValueError):
        return None


def parse_pool_standings_csv(text: str) -> dict:
    """Parse standings CSV text into (rows, errors). A row is
    rejected -- with line number and reason -- when: season/week
    are not integers, any wins/weeks_left value is missing,
    non-integer, or negative. Rejected rows never reach the DB.
    Valid rows: {season, week, user_wins, leader_wins,
    weeks_left, leader_name, note}. Duplicate (season, week)
    inside one file: the LAST row wins, noted in errors."""
    rows: list[dict] = []
    errors: list[str] = []
    if text is None or not str(text).strip():
        return {"rows": [], "errors": ["Empty CSV: no header row found."]}
    reader = csv.DictReader(io.StringIO(str(text)))
    if reader.fieldnames is None:
        return {"rows": [], "errors": ["Empty CSV: no header row found."]}
    headers = {str(h).strip().lower(): h for h in reader.fieldnames if h is not None}
    missing = [c for c in ("season", "week", "user_wins", "leader_wins", "weeks_left")
               if c not in headers]
    if missing:
        return {"rows": [], "errors": [f"Missing required column(s): {', '.join(missing)}."]}
    seen: dict[tuple, int] = {}
    for lineno, raw in enumerate(reader, start=2):
        def _get(col):
            key = headers.get(col)
            return raw.get(key) if key is not None else None

        season = _to_int(_get("season"))
        week = _to_int(_get("week"))
        if season is None or week is None:
            errors.append(f"Line {lineno}: season/week must be integers -- skipped.")
            continue
        user_wins = _to_int(_get("user_wins"))
        leader_wins = _to_int(_get("leader_wins"))
        weeks_left = _to_int(_get("weeks_left"))
        if user_wins is None or leader_wins is None or weeks_left is None:
            errors.append(f"Line {lineno}: user_wins/leader_wins/weeks_left must be integers -- skipped.")
            continue
        if user_wins < 0 or leader_wins < 0 or weeks_left < 0:
            errors.append(f"Line {lineno}: wins and weeks_left must be non-negative -- skipped.")
            continue
        leader_name = _get("leader_name")
        leader_name = str(leader_name).strip() if leader_name is not None else ""
        note = _get("note")
        note = str(note).strip() if note is not None else ""
        key = (season, week)
        if key in seen:
            errors.append(
                f"Line {lineno}: duplicate standings for season {season} week {week} -- "
                f"this row supersedes line {seen[key]}.")
        seen[key] = lineno
        rows.append({"season": season, "week": week, "user_wins": user_wins,
                     "leader_wins": leader_wins, "weeks_left": weeks_left,
                     "leader_name": leader_name, "note": note})
    deduped: dict[tuple, dict] = {}
    for r in rows:
        deduped[(r["season"], r["week"])] = r
    return {"rows": list(deduped.values()), "errors": errors}


def save_pool_standings(rows: list[dict], db_path: str | None = None) -> int:
    """Upsert parsed rows. Returns the number of rows written."""
    ensure_table(db_path)
    now = datetime.utcnow().isoformat()
    with db.connect(db_path) as conn:
        for r in rows:
            conn.execute(
                "INSERT INTO pool_standings (season, week, user_wins, leader_wins, "
                "weeks_left, leader_name, note, updated_at) VALUES (?,?,?,?,?,?,?,?) "
                "ON CONFLICT(season, week) DO UPDATE SET "
                "user_wins=excluded.user_wins, leader_wins=excluded.leader_wins, "
                "weeks_left=excluded.weeks_left, leader_name=excluded.leader_name, "
                "note=excluded.note, updated_at=excluded.updated_at",
                (r["season"], r["week"], r["user_wins"], r["leader_wins"],
                 r["weeks_left"], r.get("leader_name", ""), r.get("note", ""), now),
            )
    return len(rows)


def import_pool_standings_csv(text: str, db_path: str | None = None) -> dict:
    """Parse + upsert in one call. Returns {imported, errors}."""
    parsed = parse_pool_standings_csv(text)
    n = save_pool_standings(parsed["rows"], db_path) if parsed["rows"] else 0
    return {"imported": n, "errors": parsed["errors"]}


def load_pool_standings(season: int, db_path: str | None = None) -> list[dict]:
    ensure_table(db_path)
    with db.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM pool_standings WHERE season=? ORDER BY week",
            (season,)).fetchall()
    return [dict(r) for r in rows]


def latest_standings(season: int, week: int | None = None,
                     db_path: str | None = None) -> dict:
    """The standings snapshot for one week (or the latest week
    at/below `week`, or the latest overall when week is None).
    Empty state when no snapshot exists: status "empty", no
    wins, no deficit -- never 0-0."""
    rows = load_pool_standings(season, db_path)
    if week is not None:
        rows = [r for r in rows if r["week"] <= week]
    if not rows:
        return {"status": "empty", "season": int(season), "week": week,
                "note": ("No pool standings imported yet for this season -- "
                         "import a standings CSV (see README batch-4 group D "
                         "item 9) or enter wins manually. Nothing below is a "
                         "standing; there is nothing to price gambles against.")}
    row = rows[-1]
    deficit = int(row["leader_wins"]) - int(row["user_wins"])
    return {"status": "ok", **row, "deficit": deficit,
            "note": (f"Week {row['week']} snapshot: you {row['user_wins']}, "
                     f"leader {row['leader_wins']} (deficit {deficit}), "
                     f"{row['weeks_left']} week(s) left.")}


def clear_pool_standings(season: int, db_path: str | None = None) -> int:
    ensure_table(db_path)
    with db.connect(db_path) as conn:
        cur = conn.execute("DELETE FROM pool_standings WHERE season=?", (season,))
        return cur.rowcount


def _sim_games(games: list[dict]) -> tuple[list[dict], list[dict]]:
    """Build the baseline/gambled simulation game lists from a
    week's standings-shaped games. Returns (baseline, gambled)
    in the simulation.py shape (p_home, user_side, leader_side).
    A game with no GAMBLE label keeps its default side in both
    lists; a GAMBLE game flips user_side only, leader unchanged.
    Raises ValueError on a malformed game, like standings.py."""
    sugg_games = []
    for g in games:
        side = g.get("default_side")
        if side not in ("home", "away"):
            raise ValueError(f"default_side must be 'home'/'away', got {side!r}")
        p = float(g["pick_prob"])
        if not 0.0 <= p <= 1.0:
            raise ValueError(f"pick_prob must be in [0, 1], got {p}")
        if g.get("p_home") is not None:
            p_home = float(g["p_home"])
        else:
            p_home = p if side == "home" else 1.0 - p
        if not 0.0 <= p_home <= 1.0:
            raise ValueError(f"p_home must be in [0, 1], got {p_home}")
        sugg_games.append({"home": g.get("home"), "away": g.get("away"),
                           "default_side": side, "pick_prob": p, "p_home": p_home})
    return sugg_games


def price_week_gambles(games: list[dict], snapshot: dict,
                       trials: int | None = None, seed: int | None = None) -> dict:
    """Label one week's gambles from an imported standings
    snapshot and price them with the simulation -- ONE WEEK
    ONLY (see module docstring). snapshot is a
    latest_standings() result; with status "empty" this
    returns the empty state, never a priced gamble against
    invented standings. With no GAMBLE labels, the simulation
    is still run (baseline == gambled) so the caller gets a
    CHEAP/zero-delta record, not a missing one -- a week with
    no gambles costs nothing by construction, and that is
    stated, not hidden.

    Returns {status, snapshot, standings (suggest_gambles
    output), simulation (compare_gambles output or None),
    note}. The simulation note always carries the one-week
    caveat; callers must surface it with the numbers."""
    if snapshot.get("status") != "ok":
        return {"status": "empty", "snapshot": snapshot, "standings": None,
                "simulation": None,
                "note": ("No standings snapshot supplied -- gambles cannot be "
                         "priced against an actual pool position. Import a "
                         "standings CSV first.")}
    sugg_games = _sim_games(games)
    sugg = standings.suggest_gambles(
        [{"home": g["home"], "away": g["away"], "default_side": g["default_side"],
          "pick_prob": g["pick_prob"]} for g in sugg_games],
        int(snapshot["user_wins"]), int(snapshot["leader_wins"]),
        int(snapshot["weeks_left"]))
    gamble_idx = {i for i, g in enumerate(sugg["games"]) if g["label"] == "GAMBLE"}
    baseline, gambled = [], []
    for i, g in enumerate(sugg_games):
        side = g["default_side"]
        baseline.append({"p_home": g["p_home"], "user_side": side, "leader_side": side})
        gambled.append({"p_home": g["p_home"],
                        "user_side": ("away" if side == "home" else "home")
                        if i in gamble_idx else side,
                        "leader_side": side})
    cmp_ = simulation.compare_gambles(
        baseline, gambled, int(snapshot["user_wins"]), int(snapshot["leader_wins"]),
        trials=trials, seed=seed)
    cmp_ = {**cmp_, "note": (cmp_["note"] + " ONE-WEEK PRICING ONLY: these games "
                             "are this week alone, so P(first) here is P(ahead "
                             "after this week), NOT season-end P(first). "
                             "Full-season pricing needs the remaining schedule's "
                             "cover probabilities, which this database does not store.")}
    return {"status": "ok", "snapshot": snapshot, "standings": sugg,
            "simulation": cmp_,
            "note": (f"{sugg['note']} Simulation (one week only): "
                     f"{cmp_['label']}, delta P(ahead after this week) "
                     f"{cmp_['delta_p_first_pp']:+} pp, expected cost "
                     f"{cmp_['expected_cost_wins']:+} wins.")}


def load_standings_file(path: str | None = None) -> dict:
    """Load a standings CSV from disk, mirroring
    pool_lines.load_pool_lines_file: a missing file (or no path)
    is the empty state, not an error. Path defaults to the
    POOL_STANDINGS_CSV_PATH env var in config.py."""
    from pathlib import Path
    if path is None:
        path = getattr(config, "POOL_STANDINGS_CSV_PATH", "") or None
    if not path:
        return {"rows": [], "errors": [], "status": "empty",
                "note": ("No standings CSV supplied -- set POOL_STANDINGS_CSV to a "
                         "standings CSV (see README batch-4 group D item 9).")}
    p = Path(path)
    if not p.exists():
        return {"rows": [], "errors": [], "status": "empty",
                "note": f"No standings file found at {p} -- nothing imported."}
    parsed = parse_pool_standings_csv(p.read_text())
    return {"rows": parsed["rows"], "errors": parsed["errors"],
            "status": "ok" if parsed["rows"] else "empty",
            "note": (f"Loaded {len(parsed['rows'])} standings snapshot(s) from {p}."
                     if parsed["rows"] else
                     f"No usable standings rows in {p}.")}


def main(argv: list[str] | None = None) -> int:
    """CLI: python -m nflpicks.pool_standings [--csv PATH] [--sample]
    Prints the parsed rows / empty state as JSON. --sample prints
    the sample CSV and exits."""
    import argparse
    import json
    parser = argparse.ArgumentParser(description="Import pool standings snapshots.")
    parser.add_argument("--csv", default=None, help="Standings CSV (see module docstring)")
    parser.add_argument("--sample", action="store_true", help="Print the sample CSV and exit")
    args = parser.parse_args(argv)
    if args.sample:
        print(SAMPLE_CSV, end="")
        return 0
    loaded = load_standings_file(args.csv)
    print(json.dumps(loaded, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
