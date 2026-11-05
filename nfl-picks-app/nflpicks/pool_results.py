"""
Actual pool results grading (batch 3, item 9): model picks vs the picks
actually submitted to Barbara's pool, and where the differences helped
or hurt.

This is DISTINCT from adjustment_grading.py: that module grades games
that carried a manual adjustment vs games that did not. This module
grades the SUBMITTED slate vs the MODEL slate, game by game. A game
only enters the comparison when all three facts exist: the model pick,
the submitted pick, and the actual result. Anything else is counted
ungraded, never invented.

Input format (CSV or manual entry -- documented in README):
  season, week, away_team, home_team, submitted_team [, note]
  - season/week: integers. Teams: normalized abbreviations, matched
    against the matchup -- a submitted team that plays in neither
    team is REJECTED with an error, not silently dropped or guessed.
  - submitted_team may instead be given as submitted_pick, a model
    pick string like "KC -3.5" or "KC PK" (parsed by
    submission.pick_team_from_pick_string); if both are present,
    submitted_team wins. A `pick` column is also accepted as that
    string, for copy-paste convenience.
  - note is free text (e.g. "flipped on Thursday review") and is
    stored but never graded.

Storage: this module owns its own table (pool_results) and creates
it on first use, so db.py -- which other batch-3 work also touches --
is not edited for this item. Rows are upserted on
(season, week, away_team, home_team): re-importing a corrected CSV
replaces the old submitted pick for that game, it does not duplicate
it. No row is ever deleted by an import; only an explicit
clear_pool_results() removes a season.

Empty state: with no imported rows for a season, grade functions
return status "empty" with no record at all -- not 0-0. A 0-0 record
would read as "submitted nothing and lost nothing"; the truth is
"nothing imported yet", and the dashboard says exactly that.

Difference outcomes (per game, decided games only):
  same          -- submitted pick == model pick (no difference to judge)
  diff_helped   -- picks differ, submitted correct, model wrong
  diff_hurt     -- picks differ, submitted wrong, model correct
  diff_neutral  -- picks differ but both push (actual == line), or --
                    by construction impossible otherwise, since ATS is
                    binary once the push case is excluded: differing
                    picks on a decided game always split correct/wrong.
  A game where either pick is missing or the result is unknown is
  ungraded, not neutral.
"""
from __future__ import annotations

import csv
import io
from datetime import datetime

from . import config, db
from .submission import pick_team_from_pick_string

TABLE_SQL = """
CREATE TABLE IF NOT EXISTS pool_results (
    season INTEGER, week INTEGER, away_team TEXT, home_team TEXT,
    submitted_team TEXT, note TEXT, updated_at TEXT,
    PRIMARY KEY (season, week, away_team, home_team)
);
"""

# Columns accepted in a CSV header (lowercased, stripped). submitted_team
# is the primary pick column; submitted_pick / pick are string fallbacks.
PICK_COLUMNS = ("submitted_team", "submitted_pick", "pick")


def ensure_table(db_path: str | None = None):
    with db.connect(db_path) as conn:
        conn.executescript(TABLE_SQL)


def _norm_team(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return config.normalize_team(text)


def parse_pool_results_csv(text: str) -> dict:
    """Parse CSV text into (rows, errors). A row is rejected -- with
    its line number and reason in errors -- when: season/week are not
    integers, a team is missing, or the submitted team plays in
    neither team of its matchup. Rejected rows never reach the DB.
    Valid rows: {season, week, away_team, home_team, submitted_team,
    note}. Duplicate matchups inside one file: the LAST row wins, and
    the earlier one is noted in errors as superseded (not silently
    overwritten)."""
    rows: list[dict] = []
    errors: list[str] = []
    seen: dict[tuple, int] = {}
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        return {"rows": [], "errors": ["Empty CSV: no header row found."]}
    headers = {str(h).strip().lower(): h for h in reader.fieldnames if h is not None}
    missing = [c for c in ("season", "week", "away_team", "home_team") if c not in headers]
    if missing:
        return {"rows": [], "errors": [f"Missing required column(s): {', '.join(missing)}."]}
    if not any(c in headers for c in PICK_COLUMNS):
        return {"rows": [], "errors": [
            "Missing pick column: need submitted_team, submitted_pick, or pick."]}
    for lineno, raw in enumerate(reader, start=2):
        def _get(col):
            key = headers.get(col)
            return raw.get(key) if key is not None else None

        try:
            season = int(str(_get("season")).strip())
            week = int(str(_get("week")).strip())
        except (TypeError, ValueError, AttributeError):
            errors.append(f"Line {lineno}: season/week must be integers -- skipped.")
            continue
        away = _norm_team(_get("away_team"))
        home = _norm_team(_get("home_team"))
        if away is None or home is None:
            errors.append(f"Line {lineno}: away_team/home_team missing -- skipped.")
            continue
        submitted = _norm_team(_get("submitted_team"))
        if submitted is None:
            pick_str = _get("submitted_pick") or _get("pick")
            submitted = pick_team_from_pick_string(pick_str, home, away)
        if submitted is None:
            errors.append(
                f"Line {lineno}: no usable submitted pick for {away}@{home} -- skipped.")
            continue
        if submitted not in (home, away):
            errors.append(
                f"Line {lineno}: submitted team {submitted} plays in neither team of "
                f"{away}@{home} -- skipped (not guessed).")
            continue
        note = _get("note")
        note = str(note).strip() if note is not None else ""
        key = (season, week, away, home)
        if key in seen:
            errors.append(
                f"Line {lineno}: duplicate matchup {away}@{home} week {week} -- "
                f"this row supersedes line {seen[key]}.")
        seen[key] = lineno
        rows.append({"season": season, "week": week, "away_team": away,
                     "home_team": home, "submitted_team": submitted, "note": note})
    # Last-row-wins for duplicates: keep only the last occurrence per key.
    deduped: dict[tuple, dict] = {}
    for r in rows:
        deduped[(r["season"], r["week"], r["away_team"], r["home_team"])] = r
    return {"rows": list(deduped.values()), "errors": errors}


def save_pool_results(rows: list[dict], db_path: str | None = None) -> int:
    """Upsert parsed rows. Returns the number of rows written."""
    ensure_table(db_path)
    now = datetime.utcnow().isoformat()
    with db.connect(db_path) as conn:
        for r in rows:
            conn.execute(
                "INSERT INTO pool_results (season, week, away_team, home_team, submitted_team, "
                "note, updated_at) VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(season, week, away_team, home_team) DO UPDATE SET "
                "submitted_team=excluded.submitted_team, note=excluded.note, "
                "updated_at=excluded.updated_at",
                (r["season"], r["week"], r["away_team"], r["home_team"],
                 r["submitted_team"], r.get("note", ""), now),
            )
    return len(rows)


def import_pool_results_csv(text: str, db_path: str | None = None) -> dict:
    """Parse + upsert in one call. Returns {imported, errors}."""
    parsed = parse_pool_results_csv(text)
    n = save_pool_results(parsed["rows"], db_path) if parsed["rows"] else 0
    return {"imported": n, "errors": parsed["errors"]}


def load_pool_results(season: int, db_path: str | None = None) -> list[dict]:
    ensure_table(db_path)
    with db.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM pool_results WHERE season=? ORDER BY week, away_team, home_team",
            (season,)).fetchall()
    return [dict(r) for r in rows]


def clear_pool_results(season: int, db_path: str | None = None) -> int:
    ensure_table(db_path)
    with db.connect(db_path) as conn:
        cur = conn.execute("DELETE FROM pool_results WHERE season=?", (season,))
        return cur.rowcount


def _ats(pick_team: str | None, home: str, away: str,
         line: float | None, actual: float | None) -> str | None:
    if pick_team not in (home, away) or line is None or actual is None:
        return None
    if float(actual) == float(line):
        return "push"
    home_covers = float(actual) > float(line)
    return "correct" if (pick_team == home) == home_covers else "wrong"


def grade_pool_results(season: int, model_picks_rows, submitted_rows, results: dict) -> dict:
    """Grade one season: model picks vs submitted picks vs results.

    model_picks_rows: dict-like picks rows (home_team/away_team, pick,
    pool_spread/vegas_spread, week, season). submitted_rows: pool_results
    rows. results: {(season, week, away_team, home_team):
    actual_margin}. The judged line is the pool line where set, else
    the live Vegas line -- the same line adjustment_grading uses, so
    the two gradings are comparable. Returns per-game rows, season
    totals for both slates, the difference ledger, per-week totals,
    and ungraded counts. With no submitted rows at all, returns the
    empty state (status "empty", no records)."""
    submitted_by_key = {(r["week"], r["away_team"], r["home_team"]): r for r in submitted_rows}
    if not submitted_by_key:
        return {
            "status": "empty", "season": int(season),
            "note": ("No actual pool results imported yet for this season -- "
                     "import the CSV (see README for the format) or add games "
                     "manually. Nothing below is a record; there is nothing to grade."),
        }
    per_game: list[dict] = []
    per_week: dict[int, dict] = {}
    totals = {"model": {"wins": 0, "losses": 0, "pushes": 0},
              "submitted": {"wins": 0, "losses": 0, "pushes": 0}}
    diffs = {"same": 0, "diff_helped": 0, "diff_hurt": 0, "diff_neutral": 0}
    ungraded = {"no_model_pick": 0, "no_result": 0}
    model_by_key = {}
    for p in model_picks_rows:
        d = p if isinstance(p, dict) else dict(p)
        model_by_key[(int(d["week"]), d["away_team"], d["home_team"])] = d
    for key, sub in sorted(submitted_by_key.items()):
        week, away, home = key
        model = model_by_key.get(key)
        if model is None:
            ungraded["no_model_pick"] += 1
            per_game.append({"week": week, "matchup": f"{away}@{home}",
                             "model_pick": None, "submitted_pick": sub["submitted_team"],
                             "outcome": "ungraded", "detail": "No model pick for this game."})
            continue
        line = model["pool_spread"] if model.get("pool_spread") is not None else model.get("vegas_spread")
        actual = results.get((int(season), week, away, home))
        if actual is None:
            ungraded["no_result"] += 1
            per_game.append({"week": week, "matchup": f"{away}@{home}",
                             "model_pick": pick_team_from_pick_string(model["pick"], home, away),
                             "submitted_pick": sub["submitted_team"],
                             "outcome": "ungraded", "detail": "No result yet for this game."})
            continue
        model_team = pick_team_from_pick_string(model["pick"], home, away)
        model_out = _ats(model_team, home, away, line, actual)
        sub_out = _ats(sub["submitted_team"], home, away, line, actual)
        if model_out is None or sub_out is None:
            ungraded["no_model_pick"] += 1
            per_game.append({"week": week, "matchup": f"{away}@{home}",
                             "model_pick": model_team, "submitted_pick": sub["submitted_team"],
                             "outcome": "ungraded", "detail": "Missing pick or judged line."})
            continue
        if model_team == sub["submitted_team"]:
            outcome = "same"
        elif model_out == "push" or sub_out == "push":
            outcome = "diff_neutral"
        elif sub_out == "correct" and model_out == "wrong":
            outcome = "diff_helped"
        elif sub_out == "wrong" and model_out == "correct":
            outcome = "diff_hurt"
        else:
            outcome = "diff_neutral"
        diffs[outcome] += 1
        for slate, out in (("model", model_out), ("submitted", sub_out)):
            if out == "correct":
                totals[slate]["wins"] += 1
            elif out == "wrong":
                totals[slate]["losses"] += 1
            else:
                totals[slate]["pushes"] += 1
        wk = per_week.setdefault(week, {"model": {"wins": 0, "losses": 0, "pushes": 0},
                                       "submitted": {"wins": 0, "losses": 0, "pushes": 0},
                                       "diff_helped": 0, "diff_hurt": 0, "same": 0})
        for slate, out in (("model", model_out), ("submitted", sub_out)):
            if out == "correct":
                wk[slate]["wins"] += 1
            elif out == "wrong":
                wk[slate]["losses"] += 1
            else:
                wk[slate]["pushes"] += 1
        if outcome in ("diff_helped", "diff_hurt", "same"):
            wk[outcome] += 1
        per_game.append({"week": week, "matchup": f"{away}@{home}",
                         "model_pick": model_team, "submitted_pick": sub["submitted_team"],
                         "model_outcome": model_out, "submitted_outcome": sub_out,
                         "outcome": outcome, "note": sub.get("note", "")})

    def _finish(t: dict) -> dict:
        decided = t["wins"] + t["losses"]
        return {**t, "decided": decided,
                "record": f"{t['wins']}-{t['losses']}",
                "ats_pct": round(100 * t["wins"] / decided, 1) if decided else None}

    net = diffs["diff_helped"] - diffs["diff_hurt"]
    return {
        "status": "ok", "season": int(season),
        "model": _finish(totals["model"]),
        "submitted": _finish(totals["submitted"]),
        "differences": diffs,
        "net_diff_wins": net,
        "net_note": (f"Submitted picks differ from model picks on "
                     f"{diffs['diff_helped'] + diffs['diff_hurt'] + diffs['diff_neutral']} game(s); "
                     f"the differences netted {'+' if net > 0 else ''}{net} win(s) for the submitted slate "
                     f"({diffs['diff_helped']} helped, {diffs['diff_hurt']} hurt). "
                     "Judge this over a season, not one week."),
        "per_week": {str(k): {"model": _finish(v["model"]), "submitted": _finish(v["submitted"]),
                              "diff_helped": v["diff_helped"], "diff_hurt": v["diff_hurt"],
                              "same": v["same"]} for k, v in sorted(per_week.items())},
        "per_game": per_game,
        "ungraded": ungraded,
        "note": ("Pushes excluded from ATS%. Judged line = pool line where set, else live Vegas line, "
                 "matching adjustment_grading. 'Same' games count for both slates' records; only "
                 "differing games can move the net difference."),
    }


def grade_season_from_db(season: int, results: dict, db_path: str | None = None) -> dict:
    """Load this season's model picks and submitted results from the DB and grade them."""
    submitted = load_pool_results(season, db_path)
    with db.connect(db_path) as conn:
        rows = conn.execute("SELECT * FROM picks WHERE season=?", (season,)).fetchall()
    return grade_pool_results(season, [dict(r) for r in rows], submitted, results)


SAMPLE_CSV = """season,week,away_team,home_team,submitted_team,note
2026,1,DEN,KC,KC,SAMPLE ONLY -- not real results
2026,1,BUF,MIA,BUF,SAMPLE ONLY -- not real results
"""
