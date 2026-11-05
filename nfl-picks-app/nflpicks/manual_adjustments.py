"""
Manual-adjustment persistence (batch 3, item 5): the repo file as
source of truth.

The problem: manual adjustments entered in the dashboard live in
that deployment's SQLite database only. On Streamlit Cloud a
redeploy can wipe them, and the GitHub Action workflow -- which
commits its own runner database back to the repo -- cannot commit
adjustments made inside a Cloud deployment back. The dashboard
already warns about this; this module is the fix for the weekly
run's side of it.

The file: `nfl-picks-app/manual_adjustments.json` in the repo
(overridable with the PICKS_ADJUSTMENTS_FILE env var, which is
what the tests use). Schema version 1:

    {
      "schema_version": 1,
      "revision": 3,                 // increments on every mutation
      "updated_at": "2026-11-04T...",
      "adjustments": [
        {"team": "KC", "points": 1.5, "reason": "Mahomes ankle",
         "updated_at": "...", "updated_by": "jakob"}
      ],
      "history": [                    // append-only audit trail
        {"timestamp": "...", "action": "set", "team": "KC",
         "points_before": null, "points_after": 1.5,
         "reason": "Mahomes ankle", "actor": "jakob"}
      ]
    }

Audit trail: every set/delete appends one history event with
who/what/when/why where available (actor is whoever the caller
passes -- the dashboard passes "" when it does not know, and the
event still records the timestamp, team, before/after points, and
reason, rather than dropping the event for a missing actor).
History is never rewritten by a later mutation: importing a file
appends events for the teams it actually changes, and deleting a
team appends a "delete" event instead of erasing its past. The
file is written atomically (temp file in the same directory +
os.replace), so a crash mid-write cannot leave a half-written
JSON file behind.

Precedence -- read this before reasoning about a run, because it
is the one place a naive "file wins outright" rule would silently
lose data: the weekly run merges the file over the database,
per team (file value wins where both have the team; a
database-only team still applies). Rationale: until a user has
exported their Cloud-database adjustments into the file and
committed it, dropping database-only teams would silently discard
adjustments they can still see in the dashboard. Once the file
covers every team (the normal state after one export + commit),
the merge and the file agree exactly, and the file is the source
of truth in practice. The weekly run's diagnostic note states
which source each count came from, so the merge is visible, not
silent.

Semantics are unchanged: adjustments still feed into ratings
exactly as before (model.predict_week's manual_adj argument, added
to the team's rating -- see model.py). This module only changes
WHERE they are stored and read from, never what they do. No flag
is added and none is flipped.

What this module cannot do, stated honestly: the dashboard
running on Streamlit Cloud CANNOT write this repo file -- it has
no repo write access, and no code here pretends otherwise. The
dashboard's role is import/export (see below): it can download
the file's contents, and it can import an uploaded file into its
own database so the adjustments work in that deployment. Getting
an adjustment into the weekly run still requires the file to be
committed to the repo (edit it in GitHub, or export from the
dashboard and commit the download). The dashboard tab says
exactly that; it does not claim a Cloud save reaches the repo.
"""
from __future__ import annotations

import json
import math
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from . import config, db

SCHEMA_VERSION = 1


def default_path() -> Path:
    override = getattr(config, "MANUAL_ADJUSTMENTS_FILE", "") or os.environ.get(
        "PICKS_ADJUSTMENTS_FILE", "")
    if override:
        return Path(override)
    return Path(__file__).resolve().parent.parent / "manual_adjustments.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def empty_doc() -> dict:
    return {"schema_version": SCHEMA_VERSION, "revision": 0, "updated_at": None,
            "adjustments": [], "history": [],
            "note": ("Manual adjustments, versioned file (batch 3, item 5). "
                     "This file is the source of truth the weekly run reads; "
                     "the dashboard database is a deployment-local copy. "
                     "Empty means no adjustments -- not an error.")}


def _validate_team_points(team, points) -> tuple[str, float]:
    norm = config.normalize_team(team)
    if not norm:
        raise ValueError("Adjustment team must be a non-empty team abbreviation.")
    try:
        pts = float(points)
    except (TypeError, ValueError):
        raise ValueError(f"Adjustment points for {norm} must be numeric, got {points!r}.")
    if not math.isfinite(pts):
        raise ValueError(f"Adjustment points for {norm} must be finite, got {points!r}.")
    return norm, pts


def load_doc(path: str | Path | None = None) -> dict:
    """Load the file doc, or an empty doc if the file does not exist.
    A present-but-corrupt file (bad JSON, wrong schema version, bad
    shape) raises ValueError -- it is never silently treated as
    empty, because that would silently drop adjustments."""
    p = Path(path) if path is not None else default_path()
    if not p.exists():
        return empty_doc()
    try:
        doc = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        raise ValueError(f"Manual-adjustments file {p} is not readable JSON: {exc}")
    if not isinstance(doc, dict) or doc.get("schema_version") != SCHEMA_VERSION \
            or not isinstance(doc.get("adjustments"), list) \
            or not isinstance(doc.get("history"), list):
        raise ValueError(
            f"Manual-adjustments file {p} has an unsupported shape or schema_version "
            f"(expected schema_version {SCHEMA_VERSION}); refusing to read it rather "
            "than guessing at its contents.")
    for adj in doc["adjustments"]:
        if not isinstance(adj, dict) or "team" not in adj or "points" not in adj:
            raise ValueError(f"Manual-adjustments file {p} has a malformed adjustment entry: {adj!r}.")
    return doc


def load_adjustments(path: str | Path | None = None) -> dict[str, float]:
    """Current adjustments as {team: points}. Empty dict when the
    file does not exist -- that is the empty state, not an error."""
    return {config.normalize_team(a["team"]): float(a["points"])
            for a in load_doc(path)["adjustments"]}


def load_details(path: str | Path | None = None) -> list[dict]:
    """Current adjustments with reason/updated_at/updated_by per team."""
    return [dict(a) for a in load_doc(path)["adjustments"]]


def load_history(path: str | Path | None = None) -> list[dict]:
    return [dict(h) for h in load_doc(path)["history"]]


def _write_doc(doc: dict, path: str | Path | None = None) -> Path:
    p = Path(path) if path is not None else default_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=p.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(doc, f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp, p)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return p


def set_adjustment(team: str, points: float, reason: str = "", actor: str = "",
                   path: str | Path | None = None, timestamp: str | None = None) -> dict:
    """Upsert one adjustment in the file and append its audit event.
    Returns the stored adjustment dict. Semantics identical to
    db.set_manual_adjustment, plus the audit trail."""
    norm, pts = _validate_team_points(team, points)
    p = Path(path) if path is not None else default_path()
    doc = load_doc(p)
    ts = timestamp or _now()
    before = None
    for adj in doc["adjustments"]:
        if config.normalize_team(adj["team"]) == norm:
            before = float(adj["points"])
            adj.update({"team": norm, "points": pts, "reason": reason or "",
                        "updated_at": ts, "updated_by": actor or ""})
            break
    else:
        doc["adjustments"].append({"team": norm, "points": pts, "reason": reason or "",
                                   "updated_at": ts, "updated_by": actor or ""})
    doc["history"].append({"timestamp": ts, "action": "set", "team": norm,
                           "points_before": before, "points_after": pts,
                           "reason": reason or "", "actor": actor or ""})
    doc["revision"] = int(doc.get("revision", 0)) + 1
    doc["updated_at"] = ts
    _write_doc(doc, p)
    return {"team": norm, "points": pts, "reason": reason or "",
            "updated_at": ts, "updated_by": actor or ""}


def delete_adjustment(team: str, reason: str = "", actor: str = "",
                      path: str | Path | None = None, timestamp: str | None = None) -> bool:
    """Remove one adjustment from the file, with a 'delete' audit
    event. Returns False (and writes nothing, so the revision does
    not move) when the team had no adjustment."""
    norm, _ = _validate_team_points(team, 0.0)
    p = Path(path) if path is not None else default_path()
    doc = load_doc(p)
    before = None
    kept = []
    for adj in doc["adjustments"]:
        if config.normalize_team(adj["team"]) == norm:
            before = float(adj["points"])
        else:
            kept.append(adj)
    if before is None:
        return False
    ts = timestamp or _now()
    doc["adjustments"] = kept
    doc["history"].append({"timestamp": ts, "action": "delete", "team": norm,
                           "points_before": before, "points_after": None,
                           "reason": reason or "", "actor": actor or ""})
    doc["revision"] = int(doc.get("revision", 0)) + 1
    doc["updated_at"] = ts
    _write_doc(doc, p)
    return True


def _validate_import_doc(doc) -> list[dict]:
    """Validate an imported doc (dict) and return its normalized
    adjustment entries. Raises ValueError on any bad shape, so a
    bad upload never half-applies."""
    if not isinstance(doc, dict):
        raise ValueError("Imported manual-adjustments content must be a JSON object.")
    if "schema_version" in doc and doc["schema_version"] != SCHEMA_VERSION:
        raise ValueError(
            f"Imported schema_version {doc['schema_version']!r} is not supported "
            f"(expected {SCHEMA_VERSION}).")
    entries = doc.get("adjustments")
    if not isinstance(entries, list):
        raise ValueError("Imported content needs an 'adjustments' list.")
    out = []
    seen: dict[str, dict] = {}
    for entry in entries:
        if not isinstance(entry, dict) or "team" not in entry or "points" not in entry:
            raise ValueError(f"Imported adjustment entry is malformed: {entry!r}.")
        norm, pts = _validate_team_points(entry["team"], entry["points"])
        # Last entry wins on duplicates, matching the CSV importers.
        seen[norm] = {"team": norm, "points": pts,
                      "reason": str(entry.get("reason") or ""),
                      "updated_at": str(entry.get("updated_at") or ""),
                      "updated_by": str(entry.get("updated_by") or "")}
    return list(seen.values())


def parse_import_entries(text: str) -> list[dict]:
    """Parse (only) an uploaded manual_adjustments.json document and
    return its normalized adjustment entries. Raises ValueError on
    bad JSON or shape. The dashboard import path uses this directly
    and writes to its database only -- it deliberately never calls
    import_text, because import_text writes the repo file, which a
    Cloud deployment cannot durably do."""
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Imported content is not valid JSON: {exc}")
    return _validate_import_doc(doc)


def import_text(text: str, actor: str = "", path: str | Path | None = None,
                timestamp: str | None = None) -> dict:
    """Import a manual_adjustments.json document (text) into the
    file. Imported values win per team; teams absent from the import
    are left untouched (an import is a merge, not a wipe -- a wipe
    would silently delete adjustments the file never mentioned).
    Only teams whose value actually changes get an audit event, and
    the revision moves only if something changed. Returns
    {changed: [teams], unchanged: [teams], revision}."""
    entries = parse_import_entries(text)
    p = Path(path) if path is not None else default_path()
    current = load_doc(p)
    current_by_team = {config.normalize_team(a["team"]): a for a in current["adjustments"]}
    ts = timestamp or _now()
    changed, unchanged = [], []
    for entry in entries:
        existing = current_by_team.get(entry["team"])
        before = float(existing["points"]) if existing is not None else None
        if existing is not None and before == entry["points"] \
                and (existing.get("reason") or "") == entry["reason"]:
            unchanged.append(entry["team"])
            continue
        if existing is not None:
            existing.update({"team": entry["team"], "points": entry["points"],
                             "reason": entry["reason"], "updated_at": ts,
                             "updated_by": actor or entry["updated_by"]})
        else:
            current["adjustments"].append({"team": entry["team"], "points": entry["points"],
                                           "reason": entry["reason"], "updated_at": ts,
                                           "updated_by": actor or entry["updated_by"]})
        current["history"].append({"timestamp": ts, "action": "import", "team": entry["team"],
                                   "points_before": before, "points_after": entry["points"],
                                   "reason": entry["reason"], "actor": actor or ""})
        changed.append(entry["team"])
    if changed:
        current["revision"] = int(current.get("revision", 0)) + 1
        current["updated_at"] = ts
        _write_doc(current, p)
    return {"changed": changed, "unchanged": unchanged,
            "revision": int(current.get("revision", 0))}


def export_text(path: str | Path | None = None) -> str:
    """The file's current contents as JSON text, for the dashboard
    download button. Missing file -> the empty doc, labelled as
    such by its note field, not an error."""
    return json.dumps(load_doc(path), indent=2, sort_keys=True) + "\n"


def build_export_doc(db_path: str | None = None, file_path: str | Path | None = None) -> dict:
    """Build the doc a user should commit to the repo: the merge of
    the file and the dashboard database (file wins per team), with
    the file's history preserved. This is what the dashboard's
    Export button downloads -- committing it makes the weekly run
    see exactly what the dashboard sees."""
    weekly = load_weekly_adjustments(db_path=db_path, file_path=file_path)
    file_doc = weekly["file_doc"]
    merged = []
    for team, pts in sorted(weekly["adjustments"].items()):
        reason = weekly["reasons"].get(team, "")
        merged.append({"team": team, "points": pts, "reason": reason,
                       "updated_at": file_doc.get("updated_at") or _now(),
                       "updated_by": ""})
    return {"schema_version": SCHEMA_VERSION,
            "revision": int(file_doc.get("revision", 0)),
            "updated_at": file_doc.get("updated_at"),
            "adjustments": merged,
            "history": file_doc.get("history", []),
            "note": ("Exported merge of the repo file and the dashboard database "
                     "(file wins per team). Commit this as manual_adjustments.json "
                     "so the weekly run reads exactly these values.")}


def _load_db_adjustments(db_path: str | None = None) -> tuple[dict[str, float], dict[str, str]]:
    """Database adjustments with reasons, or ({}, {}) when the DB
    or table does not exist yet. Reasons are best-effort: the DB
    table carries them, but a missing table must not break a run."""
    try:
        with db.connect(db_path) as conn:
            rows = conn.execute(
                "SELECT team, points, reason FROM manual_adjustments").fetchall()
    except Exception:
        return {}, {}
    points = {config.normalize_team(r["team"]): float(r["points"]) for r in rows}
    reasons = {config.normalize_team(r["team"]): (r["reason"] or "") for r in rows}
    return points, reasons


def load_weekly_adjustments(db_path: str | None = None,
                            file_path: str | Path | None = None) -> dict:
    """What the weekly run should use: file merged over database,
    per team, with reasons and a plain-language source note.

    Returns {adjustments, reasons, file_teams, db_only_teams,
    file_exists, file_doc, source_note}. A corrupt file raises
    ValueError (via load_doc) -- the caller (weekly_run) records
    that as a failed diagnostic and falls back to the database,
    rather than this function silently swallowing the error."""
    p = Path(file_path) if file_path is not None else default_path()
    file_exists = p.exists()
    file_doc = load_doc(p)
    file_adj = {config.normalize_team(a["team"]): float(a["points"])
                for a in file_doc["adjustments"]}
    file_reasons = {config.normalize_team(a["team"]): (a.get("reason") or "")
                    for a in file_doc["adjustments"]}
    db_adj, db_reasons = _load_db_adjustments(db_path)
    merged = {**db_adj, **file_adj}
    reasons = {**db_reasons, **file_reasons}
    db_only = sorted(set(db_adj) - set(file_adj))
    if not file_exists:
        note = ("No manual_adjustments.json file found -- using the database "
                f"({len(db_adj)} team(s)). Create/commit the file to make "
                "adjustments survive redeploys.")
    elif not file_adj and not db_adj:
        note = "Manual-adjustments file exists and is empty; no adjustments active."
    elif db_only:
        note = (f"File is source of truth ({len(file_adj)} team(s)); "
                f"{len(db_only)} database-only team(s) also applied "
                f"({', '.join(db_only)}) until exported to the file -- "
                "nothing was silently dropped.")
    else:
        note = (f"Manual adjustments from the repo file ({len(file_adj)} team(s)); "
                "file and database agree.")
    return {"adjustments": merged, "reasons": reasons,
            "file_teams": sorted(file_adj), "db_only_teams": db_only,
            "file_exists": file_exists, "file_doc": file_doc, "source_note": note}


def sync_db_from_file(db_path: str | None = None,
                      file_path: str | Path | None = None) -> list[str]:
    """Write every file adjustment into a database (local use, e.g.
    tests and local runs: a Cloud deployment cannot write the repo
    file, so the dashboard import path instead parses the upload
    with parse_import_entries and writes the database directly).
    Returns the teams written. Database-only teams are left alone
    (merge, not wipe)."""
    weekly = load_weekly_adjustments(db_path=db_path, file_path=file_path)
    written = []
    for team in weekly["file_teams"]:
        db.set_manual_adjustment(team, weekly["adjustments"][team],
                                 weekly["reasons"].get(team, ""), db_path=db_path)
        written.append(team)
    return written
