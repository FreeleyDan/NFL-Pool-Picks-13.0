"""
Data-freshness and failure warnings (batch 3, item 10): never silently
present a week-old slate as today's.

What this module checks, from data the app can actually read:
  * latest picks run_time in the database (the "data as-of" date),
  * the age of that run against FRESHNESS_WARN_HOURS / STALE_HOURS,
  * whether the last scheduled workflow run (Tue/Thu 15:00 UTC, the
    weekly.yml cron) was missed -- i.e. now is past a scheduled run
    (plus a grace window) but the latest saved run predates it,
  * failed steps in run_diagnostics from the last run.

What it does NOT claim: live GitHub Actions status. The Actions API
needs a token this app does not have, so -- exactly as the dashboard's
existing "Last run" section already states -- the live source is the
Actions tab on GitHub. freshness_status() says that in its note, and
returns status from the database only. A missed-run warning means
"the database has no run from the last scheduled slot", which is
consistent with a workflow failure but is not proof of one (a run
could also have been skipped manually, or its DB commit lost).

Severity levels: "ok" (fresh, no failures), "warn" (old but not yet
stale, or a missed scheduled run, or failed diagnostics -- show a
banner), "stale" (picks a week old or older, or no run at all --
do not present the slate as current). The CLI (python -m
nflpicks.freshness) prints the status and exits 1 only on "stale",
so the workflow's pre-run check step can run it with `|| true` and
never block a fresh run on a stale previous one.

All times: run_time values are stored as naive UTC ISO strings
(weekly_run/db use datetime.utcnow().isoformat()); "now" is UTC
unless the caller passes one. The scheduled-run computation is in
UTC, matching the cron.
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timedelta, timezone

from . import config, db


def parse_run_time(value: str | None) -> datetime | None:
    """Parse a stored run_time (naive UTC ISO) to an aware UTC datetime,
    or None when missing/unparseable. An unparseable timestamp is
    treated as no run -- it is never treated as fresh."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).strip())
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def last_scheduled_run(now: datetime | None = None) -> datetime:
    """Most recent Tue/Thu 15:00 UTC scheduled run at or before `now`.

    weekly.yml cron is "0 15 * * 2,4": Tuesdays and Thursdays at
    15:00 UTC. If `now` is exactly on a scheduled slot, that slot is
    the last scheduled run (the run may still be in its grace window
    -- see is_missed)."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    weekdays = tuple(int(d) for d in getattr(config, "FRESHNESS_SCHEDULE_WEEKDAYS", (1, 3)))
    hour = int(getattr(config, "FRESHNESS_SCHEDULE_HOUR_UTC", 15))
    candidate = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    # Walk back at most 7 days to the latest scheduled slot <= now.
    for back in range(8):
        day = candidate - timedelta(days=back)
        if day.weekday() in weekdays and day <= now:
            return day
    # Unreachable with Tue+Thu in the weekday set (max gap 4 days),
    # but never crash the dashboard on a config surprise.
    return candidate


def check_run_freshness(latest_run: datetime | None,
                        now: datetime | None = None) -> dict:
    """Pure freshness verdict for one run timestamp (no DB read).

    Returns level ("ok"/"warn"/"stale"), age_hours, the last scheduled
    run, whether that run was missed, and a plain-language message.
    Rules, in order:
      no run at all -> stale ("no picks saved yet").
      age > STALE_HOURS -> stale, regardless of schedule.
      latest run predates the last scheduled run AND now is past
        that scheduled run + grace -> warn "missed scheduled run".
      age > WARN_HOURS -> warn ("older than any normal Tue->Thu gap").
      otherwise ok."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    scheduled = last_scheduled_run(now)
    grace = timedelta(hours=float(getattr(config, "FRESHNESS_RUN_GRACE_HOURS", 3.0)))
    if latest_run is None:
        return {"level": "stale", "age_hours": None, "last_scheduled_run": scheduled.isoformat(),
                "missed_scheduled_run": True,
                "message": ("No picks run saved yet -- there is no slate to show. "
                            "Run the weekly job (or click Refresh Picks Now) first.")}
    if latest_run.tzinfo is None:
        latest_run = latest_run.replace(tzinfo=timezone.utc)
    age_hours = (now - latest_run).total_seconds() / 3600.0
    stale_hours = float(getattr(config, "FRESHNESS_STALE_HOURS", 168.0))
    warn_hours = float(getattr(config, "FRESHNESS_WARN_HOURS", 96.0))
    missed = latest_run < scheduled and now > scheduled + grace
    if age_hours > stale_hours:
        return {"level": "stale", "age_hours": round(age_hours, 1),
                "last_scheduled_run": scheduled.isoformat(), "missed_scheduled_run": missed,
                "message": (f"Picks are {age_hours / 24:.1f} days old (last saved "
                            f"{latest_run.isoformat()}) -- a week-old slate must not be "
                            "presented as today's. Refresh before submitting anything.")}
    if missed:
        return {"level": "warn", "age_hours": round(age_hours, 1),
                "last_scheduled_run": scheduled.isoformat(), "missed_scheduled_run": True,
                "message": (f"The last scheduled run ({scheduled.isoformat()}) has no saved "
                            f"picks -- the latest run is {latest_run.isoformat()}. The workflow "
                            "may have failed; check the Actions tab on GitHub (live status) "
                            "before trusting this slate.")}
    if age_hours > warn_hours:
        return {"level": "warn", "age_hours": round(age_hours, 1),
                "last_scheduled_run": scheduled.isoformat(), "missed_scheduled_run": False,
                "message": (f"Picks are {age_hours / 24:.1f} days old (last saved "
                            f"{latest_run.isoformat()}) -- older than a normal Tue->Thu gap. "
                            "Refresh before submitting anything.")}
    return {"level": "ok", "age_hours": round(age_hours, 1),
            "last_scheduled_run": scheduled.isoformat(), "missed_scheduled_run": False,
            "message": (f"Picks are fresh: last saved {latest_run.isoformat()} "
                        f"({age_hours:.1f} hours ago).")}


def freshness_status(season: int | None = None, db_path: str | None = None,
                     now: datetime | None = None) -> dict:
    """Database-backed freshness status for the dashboard banner and
    the workflow check step.

    Reads: the latest picks run_time (for `season` when given, else
    all seasons), the latest week's picks count, and any failed
    run_diagnostics steps. A failed diagnostic step escalates an
    otherwise-ok run to "warn" (the run landed, but something in it
    failed) -- it never escalates to "stale" on its own, since the
    picks themselves may still be usable. The note always states that
    live Actions status is NOT read here."""
    now = now or datetime.now(timezone.utc)
    resolved = db_path or config.DB_PATH
    if not os.path.exists(resolved):
        # Do not create an empty database just by checking: a missing
        # DB means no slate exists, which is stale by definition.
        verdict = check_run_freshness(None, now)
        return {**verdict, "latest_run": None, "latest_week": None,
                "games": 0, "failed_steps": [],
                "message": "No picks database found -- no slate to show.",
                "note": ("This is the last run saved in this database, NOT live GitHub "
                         "Actions status (the app has no Actions token). For live status, "
                         "check the Actions tab on GitHub.")}
    try:
        with db.connect(db_path) as conn:
            if season is not None:
                rows = conn.execute(
                    "SELECT MAX(run_time) AS latest, COUNT(*) AS n, MAX(week) AS week "
                    "FROM picks WHERE season=?", (season,)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT MAX(run_time) AS latest, COUNT(*) AS n, MAX(week) AS week "
                    "FROM picks").fetchall()
            diag = conn.execute(
                "SELECT step, detail FROM run_diagnostics WHERE ok=0").fetchall()
    except Exception as exc:  # no picks table yet (fresh DB) -> stale, not a crash
        verdict = check_run_freshness(None, now)
        return {**verdict, "latest_run": None, "latest_week": None,
                "games": 0, "failed_steps": [],
                "message": f"No picks database readable ({exc}) -- no slate to show.",
                "note": ("Live GitHub Actions status is not read here (no token); "
                         "check the Actions tab on GitHub for live status.")}
    row = rows[0] if rows else None
    latest = parse_run_time(row["latest"]) if row and row["latest"] else None
    failed = [{"step": r["step"], "detail": r["detail"]} for r in diag]
    verdict = check_run_freshness(latest, now)
    if failed and verdict["level"] == "ok":
        verdict = {**verdict, "level": "warn",
                   "message": verdict["message"] + " However, the last run reported "
                   "failed step(s) (see below) -- check Data Health before submitting."}
    return {"level": verdict["level"], "age_hours": verdict["age_hours"],
            "latest_run": latest.isoformat() if latest else None,
            "latest_week": int(row["week"]) if row and row["week"] is not None else None,
            "games": int(row["n"]) if row else 0,
            "missed_scheduled_run": verdict["missed_scheduled_run"],
            "last_scheduled_run": verdict["last_scheduled_run"],
            "failed_steps": failed,
            "message": verdict["message"],
            "note": ("This is the last run saved in this database, NOT live GitHub "
                     "Actions status (the app has no Actions token). For live status, "
                     "check the Actions tab on GitHub.")}


def main(argv: list[str] | None = None) -> int:
    """CLI for the workflow check step and manual checks:
    python -m nflpicks.freshness [--db PATH] [--season N]
    Prints JSON, exits 1 only on 'stale'. The workflow's pre-run
    check step passes --no-fail (report-only): a stale previous run
    is exactly what that run is about to fix, so the check must never
    block a fresh run -- and it must not use a shell swallow, so the
    fail-fast unit-test step's guard (no swallowed failures before
    the weekly run) stays meaningful."""
    parser = argparse.ArgumentParser(description="Check picks data freshness")
    parser.add_argument("--db", default=None, help="SQLite DB path (default: config.DB_PATH)")
    parser.add_argument("--season", type=int, default=None, help="Season to check (default: all)")
    parser.add_argument("--no-fail", action="store_true",
                        help="Report only: always exit 0, even on 'stale'. For the "
                             "workflow's pre-run check step.")
    args = parser.parse_args(argv)
    status = freshness_status(args.season, args.db)
    print(json.dumps(status, indent=2))
    if args.no_fail:
        return 0
    return 1 if status["level"] == "stale" else 0


if __name__ == "__main__":
    raise SystemExit(main())
