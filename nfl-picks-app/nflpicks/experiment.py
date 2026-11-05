"""
Experiment tracking on every weekly run (batch 4, group D,
item 10).

The problem: when a week grades badly, there is no record of
WHICH model version, flag set, or blend produced that week's
slate -- so a bad week cannot be attributed to a specific
change (a flipped flag, a retuned blend, a new ratings
parameter), and a good week cannot be credited to one either.
This module closes that gap: every weekly run records one
row -- model version label, the full off-by-default flag
set, the blend/rating parameters that actually move picks,
a fingerprint of the whole record, and (best-effort) the
git commit the run came from -- into its own table
(run_experiments), created on first use so db.py is
untouched, exactly like pool_results.py's table.

What is recorded (all read from config.py AT CALL TIME,
so a test or a run that patched config records what it
actually used, not what the module imported):

* model_version: PICKS_MODEL_VERSION when set, else the
  label "nfl-picks-app (unlabelled build)". The label is
  NOT the attribution key -- a label can be reused across
  different flag sets, and this module says so. The key is
  the fingerprint.
* flags: all eleven off-by-default flags named in the task
  (EPA_LIVE, ENSEMBLE_LIVE, CALIBRATED_PICK, TRAVEL_ENABLED,
  INJ_AUTO_APPLY, TOTALS_SRS_LIVE, STALENESS_LIVE,
  UNCERTAINTY_PICK_USE, PBP_FEATURES_LIVE, TEAM_HFA_LIVE,
  DYNAMIC_BLEND_LIVE). A flag missing from config records
  as None (unknown), never as False -- a missing attribute
  is not evidence the feature was off.
* params: the parameters that move picks or their
  confidence if changed (prior fade/regression, divisional
  multiplier, market-trust ceiling, cover-probability
  shrinkage, EPA scale/blend, SRS damping, Elo K). These
  are judgment calls about what counts as "the model":
  adding a parameter here later is a schema-compatible
  change (it just changes future fingerprints), removing
  one would break comparability and must not be done
  silently.
* fingerprint: sha256 (first 16 hex chars) of the
  canonical JSON of {flags, params}. Any flag or parameter
  change changes the fingerprint; identical fingerprints
  mean identical recorded model settings. Two runs with
  the same fingerprint and different labels are the same
  model for attribution purposes.
* git_head: best-effort `git rev-parse --short HEAD`
  (5s timeout, failure -> None). In GitHub Actions and in
  a local checkout this identifies the code; in Streamlit
  Cloud the checkout may not carry git metadata, so None
  there is normal, not an error, and is never invented.
* picks_count: games in the run's slate (passed by the
  caller, weekly_run.py), so an experiment row can be
  joined to a week's slate size without re-reading picks.

Wiring (see weekly_run.py): record_run() is called once
per weekly run, AFTER picks are saved, inside a try/except
in the caller -- a tracking failure must never fail a run
whose picks are already saved, exactly like the
what-changed diff. It is NOT called from grading: grading
is a separate workflow step that re-reads the same DB, and
a second row per week would double-count the same slate.
Attribution reads the LATEST experiment row for a
season/week (latest_experiment) and joins it to whatever
grading the caller already computed -- see
attribution_for_week() and the dashboard's Model Grades
tab. A week graded before this batch has no experiment
row: attribution returns status "unknown", not a guessed
fingerprint.

The table is append-only by construction for history
(PRIMARY KEY run_time + season + week; a re-run in the
same second for the same week overwrites its own row,
which is the same run, not a new experiment). committed_db
merges this table on sync (local rows are never deleted
by a sync) for the same reason: history must survive.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime

from . import config, db

TABLE_SQL = """
CREATE TABLE IF NOT EXISTS run_experiments (
    run_time TEXT, season INTEGER, week INTEGER,
    model_version TEXT, git_head TEXT,
    flags_json TEXT, params_json TEXT, fingerprint TEXT,
    picks_count INTEGER,
    PRIMARY KEY (run_time, season, week)
);
"""

FLAG_NAMES = (
    "EPA_LIVE", "ENSEMBLE_LIVE", "CALIBRATED_PICK", "TRAVEL_ENABLED",
    "INJ_AUTO_APPLY", "TOTALS_SRS_LIVE", "STALENESS_LIVE",
    "UNCERTAINTY_PICK_USE", "PBP_FEATURES_LIVE", "TEAM_HFA_LIVE",
    "DYNAMIC_BLEND_LIVE",
)

PARAM_NAMES = (
    "PRIOR_FADE_GAMES", "PRIOR_REGRESSION", "DIV_MULTIPLIER",
    "MARKET_TRUST_COMPLEMENT", "COVER_SHRINK_SLOPE",
    "COVER_SHRINK_INTERCEPT", "EPA_POINTS_PER_UNIT", "EPA_BLEND",
    "SRS_DAMPING", "ELO_K",
)


def ensure_table(db_path: str | None = None):
    with db.connect(db_path) as conn:
        conn.executescript(TABLE_SQL)


def _model_version() -> str:
    import os
    return os.environ.get("PICKS_MODEL_VERSION", "") or "nfl-picks-app (unlabelled build)"


def _git_head() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5)
    except Exception:
        return None
    if out.returncode != 0:
        return None
    head = (out.stdout or "").strip()
    return head or None


def capture_model_settings() -> dict:
    """Snapshot the current model settings from config.py, with
    the fingerprint. Pure (no DB, no subprocess except none) --
    safe to call in tests with patched config values."""
    flags = {name: getattr(config, name, None) for name in FLAG_NAMES}
    params = {name: getattr(config, name, None) for name in PARAM_NAMES}
    canon = json.dumps({"flags": flags, "params": params}, sort_keys=True, default=str)
    fingerprint = hashlib.sha256(canon.encode()).hexdigest()[:16]
    return {"model_version": _model_version(), "flags": flags, "params": params,
            "fingerprint": fingerprint}


def record_run(season: int, week: int, picks_count: int | None = None,
               db_path: str | None = None, run_time: str | None = None) -> dict:
    """Record one weekly run's experiment row. Returns the row
    (including run_time and git_head). Never invents a git head:
    None when git metadata is unavailable."""
    ensure_table(db_path)
    settings = capture_model_settings()
    row = {"run_time": run_time or datetime.utcnow().isoformat(),
           "season": int(season), "week": int(week),
           "model_version": settings["model_version"],
           "git_head": _git_head(),
           "flags_json": json.dumps(settings["flags"], sort_keys=True),
           "params_json": json.dumps(settings["params"], sort_keys=True),
           "fingerprint": settings["fingerprint"],
           "picks_count": picks_count}
    with db.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO run_experiments (run_time, season, week, model_version, "
            "git_head, flags_json, params_json, fingerprint, picks_count) "
            "VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(run_time, season, week) DO UPDATE SET "
            "model_version=excluded.model_version, git_head=excluded.git_head, "
            "flags_json=excluded.flags_json, params_json=excluded.params_json, "
            "fingerprint=excluded.fingerprint, picks_count=excluded.picks_count",
            (row["run_time"], row["season"], row["week"], row["model_version"],
             row["git_head"], row["flags_json"], row["params_json"],
             row["fingerprint"], row["picks_count"]),
        )
    # Return the decoded flags/params alongside the stored JSON
    # forms, so callers need not re-parse to inspect a run.
    return {**row, "flags": settings["flags"], "params": settings["params"]}


def _row_to_dict(row) -> dict:
    d = dict(row)
    for key in ("flags_json", "params_json"):
        try:
            d[key.replace("_json", "")] = json.loads(d.pop(key))
        except (TypeError, ValueError):
            d[key.replace("_json", "")] = {}
    return d


def latest_experiment(season: int, week: int | None = None,
                      db_path: str | None = None) -> dict | None:
    """Latest experiment row for a season (optionally at/below a
    week). None when no row exists -- callers report 'unknown',
    never a guessed model."""
    ensure_table(db_path)
    with db.connect(db_path) as conn:
        if week is None:
            rows = conn.execute(
                "SELECT * FROM run_experiments WHERE season=? "
                "ORDER BY run_time DESC LIMIT 1", (season,)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM run_experiments WHERE season=? AND week<=? "
                "ORDER BY run_time DESC LIMIT 1", (season, week)).fetchall()
    return _row_to_dict(rows[0]) if rows else None


def experiments_for_season(season: int, db_path: str | None = None) -> list[dict]:
    """All experiment rows for a season, oldest first, so a
    fingerprint change over the season is visible as a timeline
    (a new fingerprint mid-season is a model change that splits
    any grading into before/after)."""
    ensure_table(db_path)
    with db.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM run_experiments WHERE season=? ORDER BY run_time",
            (season,)).fetchall()
    return [_row_to_dict(r) for r in rows]


def attribution_for_week(season: int, week: int, graded: dict | None = None,
                         db_path: str | None = None) -> dict:
    """Join a week's grading (any dict the caller computed, e.g.
    adjustment_grading or pool_results output, or None) to the
    experiment row that produced that week's slate. With no
    experiment row, status is "unknown" and the grading is
    returned un-attributed -- a bad week from before this batch
    cannot be blamed on (or credited to) a recorded change,
    and this function does not pretend otherwise. The flags_on
    list is derived from the stored flags_json, so a reader
    sees at a glance which shadow features were actually live
    that week (in normal operation: none)."""
    exp = latest_experiment(season, week, db_path)
    if exp is None:
        return {"status": "unknown", "season": int(season), "week": int(week),
                "experiment": None, "graded": graded,
                "note": ("No experiment record for this season/week (runs "
                         "before batch 4 group D item 10, or a run whose "
                         "tracking failed). This week's grade cannot be "
                         "attributed to a recorded model change.")}
    flags_on = sorted(k for k, v in exp["flags"].items() if v is True)
    return {"status": "ok", "season": int(season), "week": int(week),
            "experiment": exp, "graded": graded,
            "fingerprint": exp["fingerprint"],
            "flags_on": flags_on,
            "note": (f"Week {week} slate was produced by model fingerprint "
                     f"{exp['fingerprint']} (version label "
                     f"{exp['model_version']!r}, git {exp['git_head'] or 'unknown'}). "
                     f"Flags live that week: {', '.join(flags_on) if flags_on else 'none -- all shadow flags off'}."
                     " Compare fingerprints across weeks before blaming a "
                     "grade change on a model change: same fingerprint means "
                     "the model did not change, whatever the grade did.")}


def main(argv: list[str] | None = None) -> int:
    """CLI: python -m nflpicks.experiment [--db PATH] [--season N]
    [--week N] -- prints the season's experiment timeline as
    JSON (reporting, not a new record)."""
    import argparse
    parser = argparse.ArgumentParser(description="Report recorded weekly-run experiments.")
    parser.add_argument("--db", default=None, help="DB path (default: config.DB_PATH)")
    parser.add_argument("--season", type=int, default=None, help="Season (default: config.SEASON)")
    parser.add_argument("--week", type=int, default=None, help="Week for attribution (optional)")
    args = parser.parse_args(argv)
    season = args.season if args.season is not None else config.SEASON
    out: dict = {"season": season, "timeline": experiments_for_season(season, args.db)}
    if args.week is not None:
        out["attribution"] = attribution_for_week(season, args.week, None, args.db)
    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
