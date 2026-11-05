"""
Past pool-slate importer + pool-line grading (batch 3, item 4).

The problem this solves: the backtest harness grades against the
nflverse recorded line in games.csv, but the user is judged against
Barbara's pool line, which is locked earlier (typically the Tuesday
sheet) and can differ. Until now there was no way to feed a real pool
line into the harness at all -- the staleness work (batch 3, item 1)
hit the same wall and had to settle for a moneyline proxy, because
games.csv carries ONE undated line per game and no pool line.

This module is the importer that wall was missing. It does NOT invent
past pool lines: if no pool slate has been supplied, every function
here reports an explicit empty state, not a 0-0 record and not a
shifted copy of the recorded line. What it does, once a real slate
is supplied, is re-grade point-in-time backtest rows (or any rows
with a model margin, a recorded line, and an actual margin) against
the pool line instead of -- and alongside -- the recorded line, so
the pool-line-vs-recorded-line ATS difference is measured on real
lines, not assumed.

CSV format (header required; `note` and `convention` optional):

    season,week,away_team,home_team,pool_spread,pool_total,note,convention
    2024,1,DEN,KC,-3.5,,flipped on Thursday review,sportsbook
    2024,1,BUF,MIA,2.5,,,

Conventions -- read this before writing a file, because this is
exactly where the old sign-flip bug lived:

* `pool_spread` is, BY DEFAULT, in SPORTSBOOK convention, exactly as
  a pool sheet prints it: a home favorite is negative ("KC -3.5"
  means KC favored by 3.5). That matches the dashboard's pool-line
  form, which also accepts sportsbook input and converts on save.
* Inside this module (and everywhere else in the app) the line is
  stored and graded in NFLVERSE convention (positive = home
  favored), via `config.to_nflverse_spread`. The parsed rows this
  module returns are always nflverse, whatever the input was.
* A per-row `convention` column may say `sportsbook` or `nflverse`
  to override the default for that row. An unrecognised convention
  value REJECTS the row with an error, it is not guessed.
* `pool_total` is convention-free (combined points) and passes
  through unchanged; it is stored but never graded (the pool is
  ATS-only), same as pool_results' `note`.

Row validation mirrors pool_results.py: season/week must be
integers, teams must be present, the spread must be numeric, and a
duplicate matchup inside one file is superseded by its LAST row,
noted in errors. A rejected row never reaches grading.

Grading rule (deliberate, so the comparison is clean): the SAME
rule as `backtest.evaluate_variant` (the canonical harness) is
applied to both lines -- actual > line: correct iff model margin >
line; actual < line: correct iff model margin < line; actual ==
line: push. That is NOT the batch-2 `_ats_rows` rule, which grades
a margin exactly equal to the line as a push. Using one rule for
both lines means any recorded-vs-pool difference is purely a line
difference, never a grading-rule difference; a model margin exactly
equal to a line is a pick (the away side, since it is not > line)
under this rule, on either line.

What this module is NOT:
* It is not a pick input and it never switches a line: grading
  against the pool line reports what WOULD have happened under the
  judged line; the live pick logic still takes its pool line from
  `pool_line_overrides`, never from a file here.
* It is not item 9's grading: pool_results.py grades the slate
  actually SUBMITTED against the model slate. This module grades the
  MODEL's point-in-time picks against two different LINES. A game
  needs a model margin, both lines, and a result to enter the
  comparison; anything else is counted unmatched/ungraded, never
  invented.
* It does not read games.csv itself (except through the CLI, which
  reuses backtest.load_games_frame/evaluate_variant). Callers pass
  rows; the pure functions take lists of dicts, so they are
  testable with synthetic data and need no pandas.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
from pathlib import Path

from . import config

REQUIRED_COLUMNS = ("season", "week", "away_team", "home_team", "pool_spread")
DEFAULT_CONVENTION = "sportsbook"

SAMPLE_CSV = """season,week,away_team,home_team,pool_spread,pool_total,note,convention
2024,1,DEN,KC,-3.5,,SAMPLE ONLY -- not a real pool line,sportsbook
2024,1,BUF,MIA,2.5,,SAMPLE ONLY -- not a real pool line,sportsbook
"""


def _norm_team(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return config.normalize_team(text)


def _to_float(value) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def parse_pool_lines_csv(text: str) -> dict:
    """Parse pool-slate CSV text into (rows, errors).

    Returned rows are in NFLVERSE convention (positive = home
    favored), with the as-entered value kept in
    `pool_spread_as_entered` and the resolved convention in
    `convention`, so a sign question can always be traced back to
    the source row. Rows: {season, week, away_team, home_team,
    pool_spread (nflverse), pool_total, note,
    pool_spread_as_entered, convention}."""
    rows: list[dict] = []
    errors: list[str] = []
    if text is None or not str(text).strip():
        return {"rows": [], "errors": ["Empty CSV: no header row found."]}
    reader = csv.DictReader(io.StringIO(str(text)))
    if reader.fieldnames is None:
        return {"rows": [], "errors": ["Empty CSV: no header row found."]}
    headers = {str(h).strip().lower(): h for h in reader.fieldnames if h is not None}
    missing = [c for c in REQUIRED_COLUMNS if c not in headers]
    if missing:
        return {"rows": [], "errors": [f"Missing required column(s): {', '.join(missing)}."]}
    seen: dict[tuple, int] = {}
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
        entered = _to_float(_get("pool_spread"))
        if entered is None:
            errors.append(f"Line {lineno}: pool_spread must be numeric -- skipped.")
            continue
        convention_raw = _get("convention")
        convention = str(convention_raw).strip().lower() if convention_raw is not None \
            and str(convention_raw).strip() else DEFAULT_CONVENTION
        if convention not in ("sportsbook", "nflverse"):
            errors.append(
                f"Line {lineno}: convention must be 'sportsbook' or 'nflverse', "
                f"got {convention!r} -- skipped (not guessed).")
            continue
        pool_spread = config.to_nflverse_spread(entered) if convention == "sportsbook" else entered
        pool_total = _to_float(_get("pool_total"))
        if _get("pool_total") is not None and str(_get("pool_total")).strip() \
                and pool_total is None:
            errors.append(f"Line {lineno}: pool_total must be numeric -- skipped.")
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
                     "home_team": home, "pool_spread": pool_spread,
                     "pool_total": pool_total, "note": note,
                     "pool_spread_as_entered": entered, "convention": convention})
    # Last-row-wins for duplicates.
    deduped: dict[tuple, dict] = {}
    for r in rows:
        deduped[(r["season"], r["week"], r["away_team"], r["home_team"])] = r
    return {"rows": list(deduped.values()), "errors": errors}


def load_pool_lines_file(path: str | Path | None = None) -> dict:
    """Load a pool-slate CSV from disk. A missing file (or no path
    at all) is the empty state -- status "empty", no rows, no
    invented lines -- not an error. A present-but-unparseable file
    returns its parse errors with whatever valid rows it had."""
    if path is None:
        path = getattr(config, "POOL_LINES_CSV_PATH", "") or None
    if not path:
        return {"rows": [], "errors": [], "status": "empty",
                "note": ("No past pool slate supplied -- set POOL_LINES_CSV to a "
                         "pool-lines CSV (see README batch-3 items 4-5) or pass "
                         "--pool-csv to the CLI. Nothing below is a pool-line "
                         "result; there is nothing to grade.")}
    p = Path(path)
    if not p.exists():
        return {"rows": [], "errors": [], "status": "empty",
                "note": f"No pool slate found at {p} -- nothing graded against a pool line."}
    parsed = parse_pool_lines_csv(p.read_text())
    return {"rows": parsed["rows"], "errors": parsed["errors"],
            "status": "ok" if parsed["rows"] else "empty",
            "note": (f"Loaded {len(parsed['rows'])} pool line(s) from {p}."
                     if parsed["rows"] else
                     f"No usable pool lines in {p} -- nothing graded against a pool line.")}


def _grade_pick(model_margin: float, line: float, actual_margin: float) -> str:
    """Canonical harness ATS outcome for one pick against one line:
    'correct' / 'wrong' / 'push' (actual == line). A model margin
    exactly equal to the line is a pick (away side), not a push --
    see the module docstring for why this rule, and only this rule,
    is used for both lines."""
    if actual_margin == line:
        return "push"
    if actual_margin > line:
        return "correct" if model_margin > line else "wrong"
    return "correct" if model_margin < line else "wrong"


def _pick_side(model_margin: float, line: float) -> str:
    return "home" if model_margin > line else "away"


def _summ(outcomes: list[str]) -> dict:
    wins = sum(1 for o in outcomes if o == "correct")
    losses = sum(1 for o in outcomes if o == "wrong")
    pushes = sum(1 for o in outcomes if o == "push")
    decided = wins + losses
    return {"wins": wins, "losses": losses, "pushes": pushes, "decided": decided,
            "record": f"{wins}-{losses}",
            "ats_pct": round(100 * wins / decided, 2) if decided else None}


def grade_pool_lines(backtest_rows, pool_rows) -> dict:
    """Re-grade point-in-time rows against pool lines vs recorded lines.

    backtest_rows: dicts with season, week, home_team, away_team,
    model_margin, spread_line (recorded line), actual_margin. A row
    may also carry `pick_margin` (e.g. the calibrated-pick variant):
    if present, that margin -- not model_margin -- decides the pick
    side on BOTH lines, so the comparison stays a pure line effect.
    pool_rows: parsed rows from parse_pool_lines_csv (nflverse).

    Returns overall recorded vs pool ATS, the delta in percentage
    points, pick-flip counts, per-season breakdowns, per-game rows,
    and unmatched counts. With no pool rows at all, returns the
    empty state (status "empty", no records) -- never 0-0."""
    pool_by_key = {(int(r["season"]), int(r["week"]), r["away_team"], r["home_team"]): r
                   for r in pool_rows}
    if not pool_by_key:
        return {
            "status": "empty",
            "note": ("No past pool lines supplied -- import a pool-slate CSV "
                     "(see README batch-3 items 4-5) to grade against the "
                     "judged line. Nothing below is a pool-line record; "
                     "there is nothing to grade."),
        }
    per_game: list[dict] = []
    recorded_outcomes: list[str] = []
    pool_outcomes: list[str] = []
    flips = 0
    unmatched_pool = 0
    skipped = 0
    matched_keys: set[tuple] = set()
    by_season: dict[str, dict[str, list]] = {}
    for raw in backtest_rows:
        try:
            season = int(raw["season"])
            week = int(raw["week"])
            home = config.normalize_team(raw["home_team"])
            away = config.normalize_team(raw["away_team"])
            margin = float(raw["pick_margin"] if raw.get("pick_margin") is not None
                           else raw["model_margin"])
            recorded = float(raw["spread_line"])
            actual = float(raw["actual_margin"])
        except (KeyError, TypeError, ValueError):
            skipped += 1
            continue
        pool_row = pool_by_key.get((season, week, away, home))
        if pool_row is None:
            continue
        matched_keys.add((season, week, away, home))
        pool_line = float(pool_row["pool_spread"])
        rec_out = _grade_pick(margin, recorded, actual)
        pool_out = _grade_pick(margin, pool_line, actual)
        recorded_outcomes.append(rec_out)
        pool_outcomes.append(pool_out)
        flipped = _pick_side(margin, recorded) != _pick_side(margin, pool_line)
        if flipped:
            flips += 1
        bucket = by_season.setdefault(str(season), {"recorded": [], "pool": []})
        bucket["recorded"].append(rec_out)
        bucket["pool"].append(pool_out)
        per_game.append({"season": season, "week": week, "matchup": f"{away}@{home}",
                         "model_margin": margin, "recorded_line": recorded,
                         "pool_line": pool_line, "actual_margin": actual,
                         "recorded_ats": rec_out, "pool_ats": pool_out,
                         "pick_flipped": flipped})
    unmatched_pool = len(pool_by_key) - len(matched_keys)
    rec = _summ(recorded_outcomes)
    pool = _summ(pool_outcomes)
    delta = None
    if rec["ats_pct"] is not None and pool["ats_pct"] is not None:
        delta = round(pool["ats_pct"] - rec["ats_pct"], 2)
    return {
        "status": "ok",
        "n_matched": len(per_game),
        "n_pool_rows": len(pool_by_key),
        "n_unmatched_pool_rows": unmatched_pool,
        "n_skipped_backtest_rows": skipped,
        "recorded": rec,
        "pool": pool,
        "delta_pp": delta,
        "delta_note": ("Pool-line ATS minus recorded-line ATS, in percentage "
                       "points, on the same games and the same model picks. "
                       "A negative delta means the pool line was harder to "
                       "beat than the recorded line on this sample -- it is "
                       "a property of the lines, not of the model."),
        "pick_flips": flips,
        "per_season": {s: {"recorded": _summ(v["recorded"]), "pool": _summ(v["pool"])}
                       for s, v in sorted(by_season.items())},
        "per_game": per_game,
        "note": ("Pushes excluded from ATS% (canonical harness rule: actual "
                 "== line is a push). Games with no pool line are excluded "
                 "from BOTH sides, so the two ATS% are on identical game sets."),
    }


def attach_pool_lines(backtest_rows, pool_rows) -> list[dict]:
    """Per-game join of pool lines onto backtest rows (matched games
    only): each returned dict is the input row plus `pool_line`,
    `recorded_ats`, `pool_ats`, and `pick_flipped`. Unmatched rows
    are dropped, not filled with the recorded line -- filling them
    would silently report recorded-line results as pool-line ones."""
    graded = grade_pool_lines(backtest_rows, pool_rows)
    if graded["status"] != "ok":
        return []
    by_key = {(g["season"], g["week"], g["matchup"]): g for g in graded["per_game"]}
    out = []
    for raw in backtest_rows:
        try:
            key = (int(raw["season"]), int(raw["week"]),
                   f"{config.normalize_team(raw['away_team'])}@{config.normalize_team(raw['home_team'])}")
        except (KeyError, TypeError, ValueError):
            continue
        g = by_key.get(key)
        if g is None:
            continue
        merged = dict(raw)
        merged.update({"pool_line": g["pool_line"], "recorded_ats": g["recorded_ats"],
                       "pool_ats": g["pool_ats"], "pick_flipped": g["pick_flipped"]})
        out.append(merged)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Grade point-in-time backtest picks against past pool lines.")
    parser.add_argument("--pool-csv", required=False, default=None,
                        help="pool-slate CSV in the format documented in pool_lines.py")
    parser.add_argument("--games-csv", required=False, default=None,
                        help="nflverse games.csv; runs the baseline harness, then re-grades it")
    parser.add_argument("--seasons", default="2019-2025",
                        help="e.g. 2019-2025 or 2024,2025 (only with --games-csv)")
    parser.add_argument("--sample", action="store_true",
                        help="print the SAMPLE pool-lines CSV and exit")
    args = parser.parse_args(argv)
    if args.sample:
        print(SAMPLE_CSV, end="")
        return 0
    loaded = load_pool_lines_file(args.pool_csv)
    if loaded["status"] != "ok":
        print(json.dumps({"status": "empty", "note": loaded["note"],
                          "errors": loaded["errors"]}, indent=2))
        return 0
    if args.games_csv is None:
        print(json.dumps({"status": "empty",
                          "note": "No --games-csv given: pool lines were parsed "
                                  f"({len(loaded['rows'])} row(s)) but there are no "
                                  "backtest rows to grade them against.",
                          "errors": loaded["errors"]}, indent=2))
        return 0
    import pandas as pd

    from . import backtest as backtest_mod
    if "-" in args.seasons:
        lo, hi = args.seasons.split("-", 1)
        seasons = list(range(int(lo), int(hi) + 1))
    else:
        seasons = [int(s) for s in args.seasons.split(",")]
    games = backtest_mod.load_games_frame(pd.read_csv(args.games_csv, low_memory=False))
    summary = backtest_mod.evaluate_variant(games, seasons, "baseline")
    rows = summary["_games"].to_dict("records")
    graded = grade_pool_lines(rows, loaded["rows"])
    graded["baseline_recorded"] = {k: v for k, v in summary.items() if k != "_games"}
    graded["pool_csv_errors"] = loaded["errors"]
    print(json.dumps(graded, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
