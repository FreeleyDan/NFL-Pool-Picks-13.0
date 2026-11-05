"""
True play-by-play features: early-down success and red-zone
efficiency (batch 3, item 8).

SOURCE -- CONFIRMED, not guessed (2026-11-04 PT):
* The batch-2 attempt failed because it guessed release URLs and
  got 404s. The filename was never the mystery -- the release TAG
  was. nflreadr's own documentation (load_pbp: "Loads play by play
  seasons from the nflverse-data repository", file types rds/qs/csv/
  parquet) and its loader source (nflverse_download / nflverse_
  releases via piggyback on repo "nflverse/nflverse-data") name the
  repository; the nflverse-data README points at its releases page.
* The exact assets were then read off the GitHub REST API listing
  for that repository's `pbp` release (the directory listing
  piggyback's pb_list reads -- not a guessed file URL):
  release id 58152862, tag `pbp`, updated 2026-11-04. It lists,
  per season, `play_by_play_{season}.csv`, `.csv.gz`, `.parquet`,
  `.qs`, `.rds` -- e.g. play_by_play_2024.csv.gz, 19,362,351 bytes,
  sha256 23370d5d10f8104d80d46a1fc5e61f4f6f5a3263fe96fe2dd629913cfcb08,
  browser_download_url
  https://github.com/nflverse/nflverse-data/releases/download/pbp/play_by_play_2024.csv.gz
  (fetched live during this build: HTTP 200, 19,362,351 bytes --
  exactly the listed size, 372 columns). Seasons 2017-2025 were
  each fetched the same way (all HTTP 200, ~18-19 MB gz each).
  That is why this module's URL template uses tag `pbp`, not
  `play_by_play`: the batch-2 guesses used the wrong tag.
* Column names below (posteam, defteam, yardline_100, drive, down,
  ydstogo, play_type, yards_gained, first_down, touchdown, td_team,
  season_type, week, game_id) are the nflfastR field descriptions
  and were verified against the downloaded 2024 file itself
  (49,492 rows: 47,274 REG, 2,218 POST).

DEFINITIONS -- stated so they can be checked, not inherited:
* Early-down success (offense): a 1st- or 2nd-down play with
  play_type pass or run (sacks count as pass, scrambles as run,
  per the field descriptions; qb_kneel, qb_spike, no_play, kicks
  and field goals are excluded). A play succeeds if it gains at
  least 40% of ydstogo on 1st down or 60% on 2nd down (the
  Football Outsiders 40/60 convention), or records a first down
  or a touchdown. This is computed here from down/ydstogo/
  yards_gained/first_down/touchdown -- the file's own `success`
  column is NOT used: on 2024 early-down plays it gives 43.8%
  (an EPA>0-style definition, 90.9% agreement with the yardage
  rule), so substituting it would silently change the feature.
* Red-zone efficiency (offense): per possession segment
  (game_id, drive, posteam -- drive alone is not safe, since a
  drive number can be shared across a change of possession),
  a TRIP is a segment with at least one pass/run play starting
  at yardline_100 <= 20; the trip scores if any play in the
  segment has td_team == posteam. Rate = scoring trips / trips.
  Kneel-only segments are not trips (clock-killing in the red
  zone is not a scoring opportunity). Field-goal segments count
  as trips without a touchdown -- that is the point: efficiency
  is touchdowns per opportunity, not points per opportunity.
* Defensive rates are the mirror image, from the same plays:
  early-down success ALLOWED (opponent success on early-down
  plays with this team as defteam) and red-zone TD rate ALLOWED
  (opponent scoring trips / opponent trips with this team as
  defteam). A team's feature strength is off_rate -
  def_allowed_rate, and a game's differential is home strength
  minus away strength -- so a good defense helps exactly as
  much as a good offense, and a missing team reads as league
  average on both sides (the league-average strength, which is
  zero only when the league off and allowed averages happen
  to coincide), never as a 0.0 rate.
* Point-in-time discipline, exactly like the spread harness:
  for season S, week W, rates use season-S segments/weeks
  before W plus the FULL season S-1. Coefficients for season S
  are fit ONLY on completed seasons before S (plus burn-in
  seasons 2017-2018, scored nowhere), never on S itself.
  Play-by-play files are full-season, so an unfiltered load
  would leak future weeks -- every aggregation here is built
  from weekly counts, and the week filter is applied to the
  counts, not trusted to the caller.

LIVE STATUS: PBP_FEATURES_LIVE stays False (config.py). Nothing
in model.py, weekly_run.py, or dashboard.py reads this module;
it is a harness/shadow module graded by evaluate_pbp_features()
below. Flip it only on a walk-forward win -- see README.
"""
from __future__ import annotations

import math

import pandas as pd

from . import backtest as backtest_mod
from . import config

# Columns read from a play-by-play file. Extra columns in the file
# (372 in 2024) are never loaded: a full-season file is ~100 MB
# uncompressed, and only these are needed.
PBP_COLUMNS = ("game_id", "season", "season_type", "week", "posteam",
               "defteam", "drive", "down", "ydstogo", "yardline_100",
               "play_type", "yards_gained", "first_down", "touchdown",
               "td_team")
EARLY_DOWN_TYPES = ("pass", "run")
# Thresholds / red-zone line live in config.py (PBP_EARLY_DOWN_
# THRESHOLDS, PBP_RED_ZONE_YARDLINE) and are read at call time
# below, so a test or caller can patch config without reloading.
FEATURES = ("early_down", "red_zone")


def pbp_url(season: int) -> str:
    """Confirmed download URL for one season (csv.gz -- the
    smallest pandas-readable form listed for every season)."""
    return config.PBP_URL_TMPL.format(season=int(season))


def load_pbp_frame(season: int, path: str | None = None) -> pd.DataFrame:
    """Load one season's play-by-play, restricted to PBP_COLUMNS
    that are actually present (a missing column yields a frame
    without it; aggregators then fall back, never fabricate).
    With `path`, reads a local file (tests, offline reruns);
    otherwise downloads from pbp_url(season). No week filter
    here -- callers aggregate weekly counts and filter there,
    so a full-season load can never silently become a leak."""
    if path is not None:
        header = list(pd.read_csv(path, nrows=0, low_memory=False).columns)
        usecols = [c for c in PBP_COLUMNS if c in header]
        return pd.read_csv(path, usecols=usecols, low_memory=False)
    import requests
    resp = requests.get(pbp_url(season), timeout=180)
    resp.raise_for_status()
    from io import BytesIO
    header = list(pd.read_csv(BytesIO(resp.content), nrows=0, low_memory=False).columns)
    usecols = [c for c in PBP_COLUMNS if c in header]
    return pd.read_csv(BytesIO(resp.content), usecols=usecols,
                       low_memory=False)


def _is_success(row) -> bool:
    """Yardage-rule success for one early-down play (see module
    docstring). first_down/touchdown override the yardage test,
    so a conversion on a short gain still counts."""
    try:
        down = int(row["down"])
        thresh = float(config.PBP_EARLY_DOWN_THRESHOLDS[down])
        ytg = float(row["ydstogo"])
        gained = float(row["yards_gained"])
    except (KeyError, TypeError, ValueError):
        return False
    if gained >= thresh * ytg:
        return True
    for col in ("first_down", "touchdown"):
        try:
            if float(row[col]) == 1.0:
                return True
        except (KeyError, TypeError, ValueError):
            continue
    # Fallback when neither flag column exists: a gain of the
    # full distance is a conversion by definition.
    return gained >= ytg


def weekly_team_counts(pbp: pd.DataFrame | None) -> pd.DataFrame:
    """Per (season, week, team) play-by-play counts, the only
    aggregation the harness ever sees. Columns:
    early_num/early_den (offensive early-down successes/plays),
    early_allow_num/early_allow_den (same, allowed as defteam),
    rz_trips/rz_tds (offensive red-zone trips/scoring trips),
    rz_allow_trips/rz_allow_tds (same, allowed as defteam).
    Regular season only (season_type REG when present); week
    must be present or the frame yields nothing, since a
    week-less frame cannot be filtered point-in-time."""
    if pbp is None or pbp.empty or "week" not in pbp.columns \
            or "posteam" not in pbp.columns:
        return pd.DataFrame(columns=("season", "week", "team",
                                     "early_num", "early_den",
                                     "early_allow_num", "early_allow_den",
                                     "rz_trips", "rz_tds",
                                     "rz_allow_trips", "rz_allow_tds"))
    df = pbp.copy()
    if "season_type" in df.columns:
        df = df[df["season_type"].astype(str).str.upper() == "REG"]
    df["week"] = pd.to_numeric(df["week"], errors="coerce")
    df = df[df["week"].notna()]
    if df.empty:
        return pd.DataFrame(columns=("season", "week", "team",
                                     "early_num", "early_den",
                                     "early_allow_num", "early_allow_den",
                                     "rz_trips", "rz_tds",
                                     "rz_allow_trips", "rz_allow_tds"))
    df["posteam"] = df["posteam"].map(config.normalize_team)
    if "defteam" in df.columns:
        df["defteam"] = df["defteam"].map(config.normalize_team)
    # ---- Early-down success (offense + allowed) ----
    early = df[df["play_type"].isin(EARLY_DOWN_TYPES)
               & pd.to_numeric(df["down"], errors="coerce").isin([1, 2])].copy()
    early_rows = []
    if not early.empty:
        early["is_success"] = early.apply(_is_success, axis=1)
        off = early.groupby(["season", "week", "posteam"])["is_success"].agg(
            ["sum", "count"]).reset_index()
        for _, r in off.iterrows():
            early_rows.append({"season": r["season"], "week": r["week"],
                               "team": r["posteam"],
                               "early_num": float(r["sum"]),
                               "early_den": float(r["count"]),
                               "early_allow_num": 0.0, "early_allow_den": 0.0,
                               "rz_trips": 0.0, "rz_tds": 0.0,
                               "rz_allow_trips": 0.0, "rz_allow_tds": 0.0})
        if "defteam" in early.columns:
            allowed = early.dropna(subset=["defteam"]).groupby(
                ["season", "week", "defteam"])["is_success"].agg(
                ["sum", "count"]).reset_index()
            for _, r in allowed.iterrows():
                early_rows.append({"season": r["season"], "week": r["week"],
                                   "team": r["defteam"],
                                   "early_num": 0.0, "early_den": 0.0,
                                   "early_allow_num": float(r["sum"]),
                                   "early_allow_den": float(r["count"]),
                                   "rz_trips": 0.0, "rz_tds": 0.0,
                                   "rz_allow_trips": 0.0, "rz_allow_tds": 0.0})
    # ---- Red-zone trips (offense + allowed) ----
    rz_rows = []
    if "yardline_100" in df.columns and "drive" in df.columns:
        scrimmage = df[df["play_type"].isin(EARLY_DOWN_TYPES)].copy()
        scrimmage["in_rz"] = pd.to_numeric(
            scrimmage["yardline_100"], errors="coerce") <= float(config.PBP_RED_ZONE_YARDLINE)
        if "td_team" in scrimmage.columns:
            scrimmage["is_td"] = scrimmage.apply(
                lambda r: str(r.get("td_team") or "") ==
                str(r.get("posteam") or "") and str(r.get("posteam") or "") != "",
                axis=1)
        elif "touchdown" in scrimmage.columns:
            scrimmage["is_td"] = pd.to_numeric(
                scrimmage["touchdown"], errors="coerce") == 1.0
        else:
            scrimmage["is_td"] = False
        seg_keys = ["game_id", "drive", "posteam"] if "game_id" in scrimmage.columns \
            else ["drive", "posteam"]
        for _key, seg in scrimmage.groupby(seg_keys, dropna=False):
            if not bool(seg["in_rz"].any()):
                continue
            posteam = str(seg["posteam"].dropna().iloc[0]) if seg["posteam"].notna().any() else None
            if not posteam:
                continue
            season = seg["season"].iloc[0] if "season" in seg.columns else None
            week = seg["week"].iloc[0]
            trip, td = 1.0, 1.0 if bool(seg["is_td"].any()) else 0.0
            rz_rows.append({"season": season, "week": week, "team": posteam,
                            "early_num": 0.0, "early_den": 0.0,
                            "early_allow_num": 0.0, "early_allow_den": 0.0,
                            "rz_trips": trip, "rz_tds": td,
                            "rz_allow_trips": 0.0, "rz_allow_tds": 0.0})
            if "defteam" in seg.columns and seg["defteam"].notna().any():
                defteam = str(seg["defteam"].dropna().iloc[0])
                rz_rows.append({"season": season, "week": week, "team": defteam,
                                "early_num": 0.0, "early_den": 0.0,
                                "early_allow_num": 0.0, "early_allow_den": 0.0,
                                "rz_trips": 0.0, "rz_tds": 0.0,
                                "rz_allow_trips": trip, "rz_allow_tds": td})
    counts = pd.DataFrame(early_rows + rz_rows)
    if counts.empty:
        return pd.DataFrame(columns=("season", "week", "team",
                                     "early_num", "early_den",
                                     "early_allow_num", "early_allow_den",
                                     "rz_trips", "rz_tds",
                                     "rz_allow_trips", "rz_allow_tds"))
    return counts.groupby(["season", "week", "team"], as_index=False).sum(numeric_only=True)


def point_in_time_rates(counts: pd.DataFrame, season: int, week: int) -> tuple[dict, dict]:
    """Cumulative rates before week `week` of `season` plus the
    full prior season. Returns ({team: rates}, league averages).
    A rate with no denominator is None (league average is then
    substituted by the differential function, not here)."""
    if counts is None or counts.empty:
        return {}, {}
    cur = counts[(counts["season"] == season) & (counts["week"] < week)]
    prior = counts[counts["season"] == season - 1]
    tot = pd.concat([cur, prior], ignore_index=True) if len(prior) or len(cur) \
        else pd.DataFrame()
    if tot.empty:
        return {}, {}
    agg = tot.groupby("team", as_index=False).sum(numeric_only=True)
    rates: dict[str, dict] = {}
    for _, r in agg.iterrows():
        rates[str(r["team"])] = {
            "early_off": (float(r["early_num"]) / float(r["early_den"]))
            if float(r["early_den"]) > 0 else None,
            "early_allowed": (float(r["early_allow_num"]) / float(r["early_allow_den"]))
            if float(r["early_allow_den"]) > 0 else None,
            "rz_off": (float(r["rz_tds"]) / float(r["rz_trips"]))
            if float(r["rz_trips"]) > 0 else None,
            "rz_allowed": (float(r["rz_allow_tds"]) / float(r["rz_allow_trips"]))
            if float(r["rz_allow_trips"]) > 0 else None,
        }
    league = {}
    for key in ("early_off", "early_allowed", "rz_off", "rz_allowed"):
        vals = [v[key] for v in rates.values() if v[key] is not None]
        league[key] = sum(vals) / len(vals) if vals else None
    return rates, league


def game_pbp_differentials(rates: dict, league: dict, home: str, away: str) -> dict:
    """Home-minus-away strength differentials. Strength =
    off_rate - allowed_rate; a missing side reads as league
    average, so a missing team contributes the league-average
    strength, exactly like features.game_feature_differentials."""
    def _strength(team: str, off_key: str, allow_key: str):
        r = rates.get(team, {})
        off = r.get(off_key)
        off = league.get(off_key) if off is None else off
        allow = r.get(allow_key)
        allow = league.get(allow_key) if allow is None else allow
        if off is None or allow is None:
            return None
        return float(off) - float(allow)

    out = {}
    for feat, off_key, allow_key in (("early_down", "early_off", "early_allowed"),
                                     ("red_zone", "rz_off", "rz_allowed")):
        h = _strength(home, off_key, allow_key)
        a = _strength(away, off_key, allow_key)
        out[feat] = (h - a) if (h is not None and a is not None) else None
    return out


def fit_pbp_coeff(train_rows: list[dict], feature: str) -> float:
    """Clipped least squares on TRAIN rows only (same form as
    features.fit_feature_coeff): coeff = sum(diff * residual) /
    sum(diff^2), clipped to config.PBP_COEFF_CAPS, residual =
    actual margin - SRS model margin. 0.0 when the feature has
    no usable variation in training."""
    num, den = 0.0, 0.0
    for r in train_rows:
        d = r.get(f"{feature}_diff")
        if d is None:
            continue
        residual = float(r["actual_margin"]) - float(r["model_margin"])
        num += float(d) * residual
        den += float(d) * float(d)
    if den <= 0:
        return 0.0
    cap = float(config.PBP_COEFF_CAPS.get(feature, 0.0))
    return max(-cap, min(cap, num / den))


def adjusted_margin(model_margin: float, diffs: dict, coeffs: dict) -> float:
    """SRS margin + sum(coeff * diff). A None differential
    contributes nothing (league-average side)."""
    adj = float(model_margin)
    for f, c in coeffs.items():
        d = diffs.get(f)
        if d is not None and c:
            adj += float(c) * float(d)
    return adj


def _grade_rows(rows: list[dict], margin_key: str) -> list[dict]:
    """Re-grade rows on the margin in `margin_key` with the
    CANONICAL harness rule (backtest.evaluate_variant) as corrected
    in batch 4: margin == line or actual == line -> push (no pick);
    otherwise actual > line -> correct iff margin > line, actual <
    line -> correct iff margin < line. Before batch 4 this function
    deliberately used the old canonical rule (margin == line counted
    wrong when decided) so its numbers matched the old 49.12%
    baseline; after the correction the old rule no longer exists
    anywhere in the harness, and this function follows the corrected
    rule -- the batch-3 pbp numbers (baseline 49.12%, red-zone
    paired z=4.02) are superseded, see the README Batch 4 bridge."""
    out = []
    for r in rows:
        m = r.get(margin_key)
        if m is None:
            continue
        line, actual = float(r["spread_line"]), float(r["actual_margin"])
        if m == line or actual == line:
            ats = "push"
        elif actual > line:
            ats = "correct" if m > line else "wrong"
        else:
            ats = "correct" if m < line else "wrong"
        out.append({
            "season": r["season"], "week": r["week"],
            "home_team": r["home_team"], "away_team": r["away_team"],
            "model_margin": round(float(m), 3), "spread_line": line,
            "actual_margin": actual, "edge": round(float(m) - line, 3),
            "edge_bucket": ("0-1" if abs(float(m) - line) < 1 else "1-3" if abs(float(m) - line) < 3 else
                            "3-5" if abs(float(m) - line) < 5 else "5-8" if abs(float(m) - line) < 8 else "8+"),
            "pick_side": "home" if m > line else "away",
            "pick_favdog": ("fav" if (line > 0 and m > line) or (line < 0 and m < line) else "dog"),
            "week_bucket": "1-4" if r["week"] <= 4 else "5-9" if r["week"] <= 9 else "10+",
            "ats": ats, "abs_err": abs(actual - float(m)),
            "line_abs_err": abs(actual - line),
        })
    return out


def evaluate_pbp_features(games: pd.DataFrame, seasons: list[int],
                          pbp_by_season: dict) -> dict:
    """Walk-forward grading of true play-by-play features.

    games: backtest.load_games_frame output. seasons: evaluated
    seasons (e.g. 2019-2025). pbp_by_season: {season: play-by-play
    frame} -- must also cover two burn-in seasons before the
    first evaluated season (scored nowhere) so the first
    evaluated season has training coefficients; a season with
    no frame contributes no rates and its games get None
    differentials (they then grade as the SRS margin).

    For each evaluated season S: coefficients are fit on all
    scored + burn-in rows from seasons before S; each week W
    uses point-in-time rates (season S weeks < W + full S-1).
    The baseline is backtest.evaluate_variant "baseline" on the
    same games, so the paired comparison is on identical games
    under identical grading. Returns summaries for baseline,
    early_down alone, red_zone alone, and both together, plus
    coeffs_by_season and the scored rows under "_rows"."""
    counts_by_season: dict[int, pd.DataFrame] = {}
    for season, frame in (pbp_by_season or {}).items():
        counts_by_season[int(season)] = weekly_team_counts(frame)
    all_counts = pd.concat(list(counts_by_season.values()), ignore_index=True) \
        if counts_by_season else pd.DataFrame()
    first = min(seasons)
    burn_in = [s for s in (first - 2, first - 1)
               if ((games["season"] == s) & games["result"].notna()).any()]
    ordered = burn_in + list(seasons)
    # SRS margins for burn-in seasons too: without them the
    # burn-in rows would have no model margin, no diffs would
    # enter training, and the first evaluated season's
    # coefficients would be 0 by construction (that bug was
    # caught in the first run of this evaluation, where 2019
    # came out identical to baseline for exactly that reason).
    # The REPORTED baseline below is still evaluated seasons
    # only, so the reported baseline is the corrected canonical
    # number (batch 4: 50.03%, not the old 49.12%).
    baseline_full = backtest_mod.evaluate_variant(games, ordered, "baseline")
    base_rows = baseline_full["_games"].to_dict("records")
    srs_by_key = {(r["season"], r["week"], r["home_team"], r["away_team"]): r
                  for r in base_rows}
    train_rows: list[dict] = []
    scored_rows: list[dict] = []
    coeffs_by_season: dict[int, dict] = {}
    for season in ordered:
        is_eval = season in seasons
        if is_eval:
            coeffs = {f: fit_pbp_coeff(train_rows, f) for f in FEATURES}
            coeffs_by_season[season] = coeffs
        else:
            coeffs = {}
        season_games = games[games["season"] == season]
        weeks = sorted(season_games[season_games["result"].notna()]["week"].unique().tolist())
        season_rows: list[dict] = []
        for week in weeks:
            rates, league = point_in_time_rates(all_counts, season, week)
            targets = season_games[(season_games["week"] == week) & season_games["result"].notna()
                                   & season_games["spread_line"].notna()]
            for _, row in targets.iterrows():
                key = (int(season), int(week), row["home_team"], row["away_team"])
                srs = srs_by_key.get(key)
                if srs is None:
                    continue
                rec = {"season": int(season), "week": int(week),
                       "home_team": row["home_team"], "away_team": row["away_team"],
                       "spread_line": float(row["spread_line"]),
                       "actual_margin": float(row["home_score"]) - float(row["away_score"]),
                       "model_margin": float(srs["model_margin"])}
                diffs = game_pbp_differentials(rates, league, row["home_team"], row["away_team"]) \
                    if rates else {"early_down": None, "red_zone": None}
                for f, d in diffs.items():
                    rec[f"{f}_diff"] = d
                season_rows.append(rec)
        if is_eval:
            for rec in season_rows:
                for f in FEATURES:
                    c = coeffs.get(f, 0.0)
                    d = rec.get(f"{f}_diff")
                    rec[f"feat_{f}_margin"] = (rec["model_margin"] + c * d) \
                        if (d is not None and c) else rec["model_margin"]
                rec["feat_all_margin"] = adjusted_margin(
                    rec["model_margin"],
                    {f: rec.get(f"{f}_diff") for f in FEATURES}, coeffs)
            scored_rows.extend(season_rows)
        train_rows.extend(season_rows)
    # Reported baseline: evaluated seasons only (canonical
    # number); paired comparisons merge on game keys, so using
    # the evaluated-only frame here is also the correct pairing.
    baseline_eval_rows = [r for r in base_rows if r["season"] in seasons]
    baseline = backtest_mod.summarize(baseline_eval_rows, "baseline")
    out: dict = {"coeffs_by_season": coeffs_by_season,
                 "baseline": {k: v for k, v in baseline.items() if k != "_games"}}
    base_games = baseline["_games"]
    for key, label in (("feat_early_down_margin", "pbp_early_down"),
                       ("feat_red_zone_margin", "pbp_red_zone"),
                       ("feat_all_margin", "pbp_all")):
        graded = _grade_rows(scored_rows, key)
        s = backtest_mod.summarize(graded, label)
        s["paired_vs_baseline"] = backtest_mod.paired_comparison(base_games, s["_games"])
        out[label] = {k: v for k, v in s.items() if k != "_games"}
    out["_rows"] = scored_rows
    return out
