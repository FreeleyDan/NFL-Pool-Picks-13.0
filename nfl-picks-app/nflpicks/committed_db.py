"""
Committed-DB source of truth (batch 4, group D, item 8).

The divergence this fixes: the GitHub Actions workflow commits
`nfl_picks.db` back to the repo every run, but the Streamlit Cloud
dashboard reads whatever database its own deployment happens to
hold. Those two can differ, in three concrete ways:

1. A Cloud redeploy lags (or fails) after a workflow push, so the
   dashboard shows the previous week's slate while the repo already
   holds the fresh one.
2. Clicking "Refresh Picks Now" in the dashboard recomputes picks
   into the deployment's own database, which the workflow can never
   commit back -- the deployment slate is then NEWER than the
   committed slate, and the next redeploy silently overwrites it.
3. Manual adjustments / pool overrides entered in the dashboard
   live in the deployment database only (see README, batch 3
   item 5) -- they are user data, not workflow output, and must
   survive any sync in either direction.

What this module does, and does not do:

* It can FETCH the committed database -- from an explicit raw URL,
  from a local shared-store file, or from a raw.githubusercontent.com
  URL derived from a repo name -- and compare its latest slate
  against the local deployment database (latest run_time + a
  fingerprint of the latest week's picks). A difference is
  reported, never hidden, and never presented as "the dashboard
  is up to date".
* `sync_committed_to_local()` copies the WORKFLOW-OWNED tables
  from the committed snapshot into the local database, so a
  Thursday-morning dashboard can be made to show exactly what
  the workflow produced. User-owned tables are PRESERVED, not
  overwritten: manual_adjustments, pool_line_overrides,
  survivor_used_teams, pool_results, pool_standings. The sync
  result lists both sets, so a lost user table would be visible.
  run_experiments rows are MERGED (snapshot rows the local DB
  lacks are added; local rows are never deleted), because that
  table is append-only history, not a weekly slate.
* It never writes to the repo, never commits, and never invents
  a credential. A token is OPTIONAL and only ever sent as an
  Authorization header when the caller set PICKS_GITHUB_TOKEN;
  it is never logged, never put in a URL, and never echoed in
  any returned dict or error message. For a PUBLIC repo no
  token is needed at all; for a PRIVATE repo the raw URL
  returns 401/404 without one, and this module reports that
  as "committed source unreachable" with a local-DB fallback --
  it does not guess at the contents.

Configuration (all in config.py, all env-driven, all optional):

* PICKS_COMMITTED_DB_URL -- full raw URL to the committed DB.
  Wins over everything else when set.
* PICKS_COMMITTED_DB_FILE -- local path to a committed-DB copy
  (a shared store / mounted file / downloaded copy). No network,
  no token. Wins over the repo-derived URL.
* PICKS_GITHUB_REPO -- "owner/repo" for the repo-derived URL.
  There is NO default here on purpose: the audit checkout has
  no git remote to derive one from, and hardcoding a repo name
  from memory would be inventing configuration. Until one of
  the three sources above is set, describe_source() reports
  mode "local" and the dashboard says plainly that it cannot
  check the committed slate -- it does not pretend otherwise.
* PICKS_GITHUB_BRANCH (default "main"), PICKS_GITHUB_DB_PATH
  (default "nfl-picks-app/nfl_picks.db" -- the path inside the
  repo), PICKS_COMMITTED_DB_TIMEOUT_SECONDS, PICKS_COMMITTED_DB_MAX_BYTES.

Honest limitations (also in the README item-8 section):

* raw.githubusercontent.com serves through a CDN cache: after a
  workflow push, a raw fetch can return the PREVIOUS commit's
  database for some minutes. The sync result therefore always
  reports the committed slate's run_time, so the user can check
  it against the expected Thursday run time instead of trusting
  the word "synced". If the committed run_time is older than
  the last scheduled run, freshness.py already warns about it.
* This module does not make the dashboard read the committed
  DB on every render: Streamlit Cloud cannot durably replace
  its own deployment database on load (a load-time overwrite
  would also destroy a just-clicked Refresh), and dashboard
  writes (pool overrides, adjustments) must go to the local
  DB. The guarantee is therefore: divergence is DETECTED on
  load (when a source is configured) and RESOLVED by an
  explicit sync click that preserves user data -- not silent
  auto-overwrite, and not silent staleness.
* A sync is one-way (committed -> local). Dashboard-side
  changes that should reach the workflow still need the
  manual-adjustments export/commit route documented in the
  README; this module does not change that.
"""
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import tempfile
import urllib.error
import urllib.request
from datetime import datetime

from . import config, db

# Tables the weekly workflow owns (rewritten every run). A sync
# replaces these wholesale from the committed snapshot.
WORKFLOW_TABLES = (
    "picks", "team_ratings_snapshot", "injury_review", "injury_returns",
    "line_snapshots", "market_crosschecks", "model_log", "model_grades",
    "run_diagnostics", "run_diffs", "survivor_picks",
    "survivor_week_options", "adjustment_grades",
)
# Tables the user owns in the dashboard deployment. A sync must
# never delete or overwrite these.
PRESERVED_TABLES = (
    "manual_adjustments", "pool_line_overrides", "survivor_used_teams",
    "pool_results", "pool_standings",
)
# Append-only history: merged, never wiped (see module docstring).
MERGED_TABLES = ("run_experiments",)

_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, "") or default


def describe_source() -> dict:
    """How the committed DB would be reached, without fetching it.

    Returns {mode, url, file, repo, branch, db_path_in_repo,
    note}. mode is "url" | "file" | "repo" | "local". "local"
    means no committed source is configured at all: the caller
    must fall back to the local DB and say so, not imply a
    committed check happened. The returned URL never contains
    a token (the token, when set, travels only as a header)."""
    url = _env("PICKS_COMMITTED_DB_URL") or getattr(config, "COMMITTED_DB_URL", "")
    if url:
        return {"mode": "url", "url": url, "file": None, "repo": None,
                "branch": None, "db_path_in_repo": None,
                "note": "Committed DB fetched from the explicit raw URL."}
    path = _env("PICKS_COMMITTED_DB_FILE") or getattr(config, "COMMITTED_DB_FILE", "")
    if path:
        return {"mode": "file", "url": None, "file": path, "repo": None,
                "branch": None, "db_path_in_repo": None,
                "note": "Committed DB read from a local shared-store file (no network, no token)."}
    repo = _env("PICKS_GITHUB_REPO") or getattr(config, "COMMITTED_DB_REPO", "")
    if repo:
        branch = _env("PICKS_GITHUB_BRANCH") or getattr(config, "COMMITTED_DB_BRANCH", "main")
        in_repo = getattr(config, "COMMITTED_DB_PATH_IN_REPO", "nfl-picks-app/nfl_picks.db")
        return {"mode": "repo", "url": None, "file": None, "repo": repo,
                "branch": branch, "db_path_in_repo": in_repo,
                "note": ("Committed DB fetched from raw.githubusercontent.com for "
                         f"{repo}@{branch}. A private repo needs PICKS_GITHUB_TOKEN; "
                         "a public repo does not.")}
    return {"mode": "local", "url": None, "file": None, "repo": None,
            "branch": None, "db_path_in_repo": None,
            "note": ("No committed-DB source configured (set PICKS_COMMITTED_DB_URL, "
                     "PICKS_COMMITTED_DB_FILE, or PICKS_GITHUB_REPO). The dashboard "
                     "reads its local deployment database only, which can diverge "
                     "from the workflow-committed database -- see README batch-4 "
                     "group D item 8.")}


def committed_db_url(source: dict | None = None) -> str | None:
    """Resolve the fetch URL for a described source, or None for
    file/local modes. Raises ValueError on a malformed repo name
    instead of building a bogus URL from it."""
    source = source or describe_source()
    if source["mode"] == "url":
        return source["url"]
    if source["mode"] == "repo":
        repo = source["repo"]
        if not _REPO_RE.match(repo or ""):
            raise ValueError(
                f"PICKS_GITHUB_REPO must be 'owner/repo', got {repo!r} -- not fetched.")
        return (f"https://raw.githubusercontent.com/{repo}/{source['branch']}/"
                f"{source['db_path_in_repo']}")
    return None


def fetch_committed_db_bytes(source: dict | None = None) -> bytes:
    """Fetch the committed database bytes. File mode reads the
    file; URL/repo modes use urllib with an optional token header.
    Raises on network/HTTP errors and on an oversize response --
    callers treat any raise as 'committed source unreachable' and
    fall back to the local DB. The token (if set) is never part
    of any exception message raised here."""
    source = source or describe_source()
    if source["mode"] == "local":
        raise ValueError("No committed-DB source configured -- nothing to fetch.")
    if source["mode"] == "file":
        path = source["file"]
        if not os.path.exists(path):
            raise FileNotFoundError(f"Committed-DB file not found: {path}")
        with open(path, "rb") as fh:
            data = fh.read()
        _check_size(data)
        return data
    url = committed_db_url(source)
    timeout = float(getattr(config, "COMMITTED_DB_TIMEOUT_SECONDS", 30))
    request = urllib.request.Request(url)
    token = _env("PICKS_GITHUB_TOKEN")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    request.add_header("Accept", "application/octet-stream")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            data = resp.read(int(getattr(config, "COMMITTED_DB_MAX_BYTES", 64 * 1024 * 1024)) + 1)
    except urllib.error.HTTPError as exc:
        # Deliberately generic: do not leak the URL's auth state or
        # the token into a message that could be displayed. A 404 on
        # a private repo without a token looks identical to a
        # missing file -- both mean "unreachable", and the caller
        # says exactly that.
        raise RuntimeError(f"Committed DB unreachable (HTTP {exc.code}).") from None
    except urllib.error.URLError as exc:
        raise RuntimeError("Committed DB unreachable (network error).") from exc
    _check_size(data)
    return data


def _check_size(data: bytes) -> None:
    limit = int(getattr(config, "COMMITTED_DB_MAX_BYTES", 64 * 1024 * 1024))
    if len(data) > limit:
        raise RuntimeError(
            f"Committed DB larger than the {limit}-byte cap -- not loaded.")


def _snapshot_path(data: bytes) -> str:
    fd, path = tempfile.mkstemp(prefix="committed_picks_", suffix=".db")
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    return path


def _latest_slate(db_path: str) -> dict:
    """Latest slate in one database file: latest run_time, latest
    week, game count, and a fingerprint of the latest week's
    picks. A missing/unreadable picks table is an empty slate,
    not an error -- the comparison reports it as such."""
    if not os.path.exists(db_path):
        return {"status": "empty", "run_time": None, "week": None,
                "games": 0, "fingerprint": None,
                "note": "No database file at this path."}
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            if "picks" not in tables:
                return {"status": "empty", "run_time": None, "week": None,
                        "games": 0, "fingerprint": None,
                        "note": "Database has no picks table."}
            rows = conn.execute(
                "SELECT season, week, home_team, away_team, pick, edge, "
                "model_independent, pool_spread, vegas_spread, run_time "
                "FROM picks ORDER BY season, week, home_team, away_team").fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return {"status": "error", "run_time": None, "week": None,
                "games": 0, "fingerprint": None, "note": f"Unreadable database: {exc}"}
    if not rows:
        return {"status": "empty", "run_time": None, "week": None,
                "games": 0, "fingerprint": None, "note": "Picks table is empty."}
    latest_week = max(int(r["week"]) for r in rows)
    latest_season = max(int(r["season"]) for r in rows if int(r["week"]) == latest_week)
    week_rows = [r for r in rows
                 if int(r["week"]) == latest_week and int(r["season"]) == latest_season]
    run_times = [r["run_time"] for r in week_rows if r["run_time"]]
    canon = "|".join(
        f"{r['season']}:{r['week']}:{r['away_team']}@{r['home_team']}:"
        f"{r['pick']}:{r['edge']}:{r['model_independent']}:"
        f"{r['pool_spread']}:{r['vegas_spread']}"
        for r in week_rows)
    return {"status": "ok", "run_time": max(run_times) if run_times else None,
            "week": latest_week, "season": latest_season,
            "games": len(week_rows),
            "fingerprint": hashlib.sha256(canon.encode()).hexdigest()[:16],
            "note": f"{len(week_rows)} game(s), week {latest_week}."}


def compare_to_local(local_path: str | None = None,
                     source: dict | None = None) -> dict:
    """Fetch the committed DB (when configured) and compare its
    latest slate to the local DB's. Never raises for a fetch or
    parse failure: that case returns status "unreachable" with
    the local slate attached, so the caller falls back to local
    and says the committed check did not happen. Returns the
    temp snapshot path on success so the caller can sync from
    it without re-fetching; the caller owns deleting it."""
    local_path = local_path or config.DB_PATH
    local = _latest_slate(local_path)
    source = source or describe_source()
    if source["mode"] == "local":
        return {"status": "local_only", "source": source, "local": local,
                "committed": None, "diverged": None, "committed_newer": None,
                "snapshot_path": None,
                "note": source["note"]}
    try:
        data = fetch_committed_db_bytes(source)
        snap = _snapshot_path(data)
    except Exception as exc:
        return {"status": "unreachable", "source": source, "local": local,
                "committed": None, "diverged": None, "committed_newer": None,
                "snapshot_path": None, "error": str(exc),
                "note": ("Committed DB could not be fetched -- the dashboard "
                         "is showing its local database, which may differ "
                         "from the workflow-committed slate.")}
    committed = _latest_slate(snap)
    if committed["status"] != "ok":
        try:
            os.unlink(snap)
        except OSError:
            pass
        return {"status": "unreachable", "source": source, "local": local,
                "committed": committed, "diverged": None, "committed_newer": None,
                "snapshot_path": None,
                "note": f"Committed DB fetched but has no usable slate: {committed['note']}"}
    # Divergence is defined by the PICKS fingerprint, not by
    # run_time: two databases holding identical picks saved at
    # different seconds are the same slate to submit, and a
    # run_time-only difference must not cry divergence. run_time
    # is still reported (and committed_newer flagged) so a
    # newer-but-identical committed run is visible, not hidden.
    diverged = (local["status"] != "ok"
                or local["fingerprint"] != committed["fingerprint"])
    committed_newer = bool(
        committed["run_time"] and local["run_time"]
        and committed["run_time"] > local["run_time"])
    if diverged and committed_newer:
        note = ("Committed slate is NEWER than this deployment's slate "
                f"(committed {committed['run_time']}, local {local['run_time']}) -- "
                "sync before submitting.")
    elif diverged:
        note = ("Local and committed slates DIFFER (different picks) -- "
                "the local slate may be a dashboard Refresh "
                "the workflow never committed. Sync to the committed slate "
                "before submitting, or keep the local slate deliberately.")
    elif committed_newer:
        note = ("Local slate matches the committed slate's picks; the "
                "committed run is newer but produced identical picks.")
    else:
        note = "Local slate matches the committed slate."
    return {"status": "ok" if not diverged else "diverged", "source": source,
            "local": local, "committed": committed, "diverged": diverged,
            "committed_newer": committed_newer, "snapshot_path": snap,
            "note": note}


def sync_committed_to_local(local_path: str | None = None,
                            source: dict | None = None,
                            snapshot_path: str | None = None) -> dict:
    """Replace the workflow-owned tables in the local DB with the
    committed snapshot's, preserving user-owned tables and merging
    run_experiments. Returns {status, replaced, skipped, preserved,
    merged, committed_run_time, local_run_time_before, note}.

    A snapshot table that does not exist in the snapshot is
    skipped (an older committed DB may predate a table), never
    treated as "delete everything". A snapshot table missing
    from the local DB is created by the copy only if the local
    schema already has it via db.init_db -- callers should run
    init_db first (the dashboard does at import)."""
    local_path = local_path or config.DB_PATH
    db.init_db(local_path)
    if snapshot_path is None:
        source = source or describe_source()
        if source["mode"] == "local":
            return {"status": "empty", "replaced": [], "skipped": [],
                    "preserved": list(PRESERVED_TABLES), "merged": [],
                    "committed_run_time": None, "local_run_time_before": None,
                    "note": "No committed-DB source configured -- nothing synced."}
        try:
            data = fetch_committed_db_bytes(source)
            snapshot_path = _snapshot_path(data)
            _own_snapshot = True
        except Exception as exc:
            return {"status": "unreachable", "replaced": [], "skipped": [],
                    "preserved": list(PRESERVED_TABLES), "merged": [],
                    "committed_run_time": None, "local_run_time_before": None,
                    "note": f"Committed DB could not be fetched -- nothing synced ({exc})."}
    else:
        _own_snapshot = False
    try:
        before = _latest_slate(local_path)
        committed = _latest_slate(snapshot_path)
        conn = sqlite3.connect(local_path)
        try:
            snap_tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            conn.execute("ATTACH DATABASE ? AS snap", (snapshot_path,))
            try:
                snap_names = {r[0] for r in conn.execute(
                    "SELECT name FROM snap.sqlite_master WHERE type='table'").fetchall()}
                replaced, skipped = [], []
                for table in WORKFLOW_TABLES:
                    if table not in snap_names or table not in snap_tables:
                        skipped.append(table)
                        continue
                    conn.execute(f"DELETE FROM \"{table}\"")
                    cols = [r[1] for r in conn.execute(
                        f"PRAGMA table_info(\"{table}\")").fetchall()]
                    col_list = ", ".join(f'"{c}"' for c in cols)
                    conn.execute(
                        f"INSERT INTO \"{table}\" ({col_list}) "
                        f"SELECT {col_list} FROM snap.\"{table}\"")
                    replaced.append(table)
                merged = []
                for table in MERGED_TABLES:
                    if table not in snap_names or table not in snap_tables:
                        skipped.append(table)
                        continue
                    cols = [r[1] for r in conn.execute(
                        f"PRAGMA table_info(\"{table}\")").fetchall()]
                    col_list = ", ".join(f'"{c}"' for c in cols)
                    conn.execute(
                        f"INSERT OR IGNORE INTO \"{table}\" ({col_list}) "
                        f"SELECT {col_list} FROM snap.\"{table}\"")
                    merged.append(table)
            finally:
                # Commit BEFORE detach: an open transaction locks
                # the attached database and DETACH fails with
                # "database snap is locked".
                conn.commit()
                conn.execute("DETACH DATABASE snap")
        finally:
            conn.close()
        return {"status": "ok", "replaced": replaced, "skipped": skipped,
                "preserved": list(PRESERVED_TABLES), "merged": merged,
                "committed_run_time": committed["run_time"],
                "local_run_time_before": before["run_time"],
                "note": (f"Synced {len(replaced)} workflow table(s) from the committed "
                         f"slate (run {committed['run_time']}); user tables preserved: "
                         f"{', '.join(PRESERVED_TABLES)}."
                         + (f" Skipped (absent in snapshot or local DB): {', '.join(skipped)}."
                            if skipped else ""))}
    finally:
        if _own_snapshot:
            try:
                os.unlink(snapshot_path)
            except OSError:
                pass


def main(argv: list[str] | None = None) -> int:
    """CLI: python -m nflpicks.committed_db [--db PATH] [--sync]
    Prints the source + comparison as JSON. --sync performs the
    sync described above. Never prints a token."""
    import argparse
    import json
    parser = argparse.ArgumentParser(description="Compare/sync the committed picks database.")
    parser.add_argument("--db", default=None, help="Local DB path (default: config.DB_PATH)")
    parser.add_argument("--sync", action="store_true", help="Sync committed -> local (preserves user tables)")
    args = parser.parse_args(argv)
    cmp_ = compare_to_local(args.db)
    out = {k: v for k, v in cmp_.items() if k != "snapshot_path"}
    if args.sync and cmp_.get("snapshot_path"):
        out["sync"] = sync_committed_to_local(args.db, snapshot_path=cmp_["snapshot_path"])
    if cmp_.get("snapshot_path"):
        try:
            os.unlink(cmp_["snapshot_path"])
        except OSError:
            pass
    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
