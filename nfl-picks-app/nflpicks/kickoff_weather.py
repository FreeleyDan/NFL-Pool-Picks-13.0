"""
Kickoff weather (batch 5, group B item 3) -- tested, NOT wired live.

The question: batch 2's coarse wind proxy (daily max wind from
games.csv, pass-heavy team in 20+ mph wind) was noise. Was that
because wind does not matter, or because a DAILY wind number is the
wrong measurement? This module tests kickoff-level weather properly:

  * the LIVE path fetches HOURLY Open-Meteo values at the kickoff
    hour in the stadium's local time (archive API for past dates,
    forecast API for upcoming games -- both free, no key), never a
    daily max/average;
  * the BACKTEST path uses the wind/temp already recorded in
    games.csv -- nflverse records those at kickoff, not as daily
    averages -- so the retrospective test does not depend on any
    live fetch. The live path is validated against those recorded
    values on a sample (see validate_against_recorded), not assumed
    to agree.

Point-in-time discipline. The backtest replay is the same
situational.replay_with_context loop as group A (ratings from
season-S games before week W plus the full season S-1, corrected
push grading). Pass/run shares are point-in-time team-stats rates
(weeks < W only, via features.team_feature_rates). Weather itself
is known before kickoff, so using the target game's recorded
wind/temp is not a leak -- it is the information a Thursday
forecast would have given, with forecast error removed. (Forecast
error is a real caveat for the live path: the backtest grades
realised weather, a live forecast will sometimes be wrong.)

Pre-registered variants (additive only -- weather has no natural
fade form; 3 tests, one Bonferroni family). Each variant is a
single no-intercept OLS slope of the point-in-time residual
(actual - model, home-margin units) on a pre-registered x,
fit on TRAIN (2019-2022) only, frozen, applied to TEST
(2023-2025). The shift is clipped to +/- WEATHER_KICKOFF_ADJ_CAP:

  wind_x_pass   x = max(0, wind - 15) * (pass_share_home - pass_share_away)
                -- wind hurts the more pass-heavy team. Outdoor games
                with known wind only; domes / unknown wind get x = 0.
  wind20_x_pass x = max(0, wind - 20) * (pass_share_home - pass_share_away)
                -- the stricter cutoff, in case 15 mph is too low.
  cold_x_run    x = max(0, 32 - temp) * (run_share_home - run_share_away)
                -- cold helps the more run-heavy team (run_share =
                1 - pass_share). Outdoor games with known temp only.

Precipitation is NOT tested: games.csv carries no precipitation
column, and inventing a precip value from a daily forecast would
repeat the daily-average error this item exists to avoid. The live
fetch DOES return precip_mm at kickoff (the API has it), so a
future test can use recorded precip once a source exists.

Promotion gate (ALL must hold): the variant wins on the TEST split
(paired delta > 0 vs always-follow on the same test games) AND its
full-sample paired p-value survives Bonferroni across all 3 tests
(adjusted p < 0.05) AND it beats the additive placebo gate
(situational.placebo_additive_distribution on the test split,
+/-0.15 pt, 20 seeds). Nothing passed; see README batch 5 group B.

Cache / fixtures. The test suite must not depend on live network
calls: tests use recorded Open-Meteo JSON fixtures under
tests/fixtures/ (real archive responses, saved verbatim) plus a
monkeypatched requests.get. The live fetch caches raw API JSON on
disk only when a cache_dir is passed (WEATHER_KICKOFF_CACHE_DIR in
config.py is None by default -- no surprise disk writes); the
cache key is (latitude, longitude, date) rounded to 4 decimals.
"""
from __future__ import annotations

import json
import logging
import math
import os
from datetime import date
from pathlib import Path

import pandas as pd
import requests

from . import backtest as backtest_mod, config, features as features_mod
from . import rest_travel as rt_mod
from . import situational as sit_mod

log = logging.getLogger(__name__)

TRAIN_SEASONS = (2019, 2020, 2021, 2022)
TEST_SEASONS = (2023, 2024, 2025)

VARIANTS = ("wind_x_pass", "wind20_x_pass", "cold_x_run")

HOURLY_VARS = "temperature_2m,precipitation,wind_speed_10m"


# ---------------------------------------------------------------------------
# Kickoff hour + live fetch
# ---------------------------------------------------------------------------

def _parse_gametime(value) -> tuple[int, int] | None:
    """nflverse gametime is Eastern Time, 'HH:MM'. Returns (hour, minute)."""
    if value is None:
        return None
    try:
        parts = str(value).strip().split(":")
        return int(parts[0]), int(parts[1])
    except (ValueError, IndexError):
        return None


def _et_offset_hours(gameday) -> int | None:
    """UTC offset of US Eastern Time on the gameday (-4 DST, -5 standard)."""
    d = rt_mod._parse_gameday(gameday)
    if d is None:
        return None
    return -4 if rt_mod._is_us_dst(d) else -5


def local_kickoff_hour(gametime, gameday, home_team: str,
                       stadium: str | None = None,
                       location: str | None = None) -> int | None:
    """Kickoff hour (0-23) in the STADIUM's local time.

    nflverse gametime is Eastern; the stadium offset comes from the
    DST-aware rest_travel helpers (neutral venues resolved by stadium
    name, same as travel). Returns None when the time or either
    offset is unknown -- the caller then records no weather, never
    a guessed hour.
    """
    parsed = _parse_gametime(gametime)
    if parsed is None:
        return None
    et_hour, _minute = parsed
    et_off = _et_offset_hours(gameday)
    row = {"home_team": home_team, "gameday": gameday,
           "stadium": stadium, "location": location}
    st_off = rt_mod._stadium_tz_offset(row)  # type: ignore[arg-type]
    if et_off is None or st_off is None:
        return None
    return (et_hour + (st_off - et_off)) % 24


def _stadium_coords_and_tz(home_team: str, stadium: str | None,
                           location: str | None):
    """(lat, lon, iana_tz) for the stadium a game is played in, or
    (None, None, None) when the venue is unknown. Mirrors the
    rest_travel venue resolution (home team coords, neutral venues
    by stadium name, host stadiums by their team)."""
    row = {"home_team": home_team, "stadium": stadium, "location": location}
    coords = rt_mod._stadium_coords(row)  # type: ignore[arg-type]
    if coords is None:
        return None, None, None
    if str(location or "").strip().lower() == "neutral":
        name = str(stadium or "").strip()
        tz = config.NEUTRAL_VENUE_TIMEZONES.get(name)
        if tz is not None:
            return coords[0], coords[1], tz
        host = rt_mod._NEUTRAL_HOST_TEAM.get(name)
        if host is not None:
            return coords[0], coords[1], config.STADIUM_TIMEZONES.get(host)
        return coords[0], coords[1], None
    return coords[0], coords[1], config.STADIUM_TIMEZONES.get(
        config.normalize_team(home_team))


def _cache_path(cache_dir, lat: float, lon: float, date_str: str) -> Path:
    name = f"openmeteo_{lat:.4f}_{lon:.4f}_{date_str}.json"
    return Path(cache_dir) / name


def fetch_hourly(lat: float, lon: float, date_str: str,
                 cache_dir=None) -> dict | None:
    """Raw Open-Meteo hourly JSON for one stadium-day.

    Past dates use the archive API, dates within the forecast
    horizon use the forecast API (the archive API rejects future
    dates and the forecast API rejects dates older than ~16 days,
    so the choice is by date, not by preference). A cached file
    wins over the network whenever it exists. Returns None on any
    failure -- the caller records no weather, never a fabricated
    value. No test may call this without a monkeypatched requests
    (see tests/fixtures for recorded responses).
    """
    if cache_dir is not None:
        path = _cache_path(cache_dir, lat, lon, date_str)
        if path.exists():
            try:
                return json.loads(path.read_text())
            except (OSError, ValueError):
                pass  # corrupt cache entry: fall through to the network
    try:
        target = date.fromisoformat(str(date_str)[:10])
    except ValueError:
        return None
    is_past = target < date.today()
    url = config.WEATHER_ARCHIVE_URL if is_past else config.WEATHER_FORECAST_URL
    params = {
        "latitude": lat, "longitude": lon,
        "hourly": HOURLY_VARS,
        "temperature_unit": "fahrenheit", "wind_speed_unit": "mph",
        "start_date": date_str, "end_date": date_str,
        "timezone": "auto",
    }
    try:
        resp = requests.get(url, params=params, timeout=20)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.warning("Open-Meteo fetch failed for %s %s: %s", lat, lon, e)
        return None
    if not data.get("hourly", {}).get("time"):
        return None
    if cache_dir is not None:
        try:
            path = _cache_path(cache_dir, lat, lon, date_str)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data))
        except OSError:
            pass
    return data


def kickoff_values(hourly: dict, hour: int) -> dict | None:
    """Wind/temp/precip at one local hour from an hourly response.

    The response's own timezone field is checked against the hour
    index: hours are matched by the ISO time string's hour, not by
    position, so a short/missing array cannot silently shift the
    hour. Returns None when the hour is absent.
    """
    times = (hourly or {}).get("hourly", {}).get("time") or []
    hourly_block = (hourly or {}).get("hourly", {})
    for i, t in enumerate(times):
        try:
            h = int(str(t)[11:13])
        except (ValueError, IndexError):
            continue
        if h != hour:
            continue
        def _at(key):
            vals = hourly_block.get(key) or []
            return vals[i] if i < len(vals) else None
        wind = _at("wind_speed_10m")
        temp = _at("temperature_2m")
        if wind is None or temp is None:
            return None
        precip = _at("precipitation")
        return {"wind_mph": float(wind), "temp_f": float(temp),
                "precip_mm": float(precip) if precip is not None else 0.0,
                "hour": hour}
    return None


def kickoff_weather_for_game(row, cache_dir=None) -> dict | None:
    """Kickoff-hour weather for one game row (or None).

    Row needs home_team, gameday, gametime, roof, stadium, location.
    Indoor roofs (dome/closed, config.INDOOR_ROOF_VALUES) return
    None -- weather does not apply indoors, and a fetched outdoor
    value would be meaningless for the game. Unknown stadium,
    unknown kickoff hour, or any fetch failure also returns None.
    The returned dict carries its source ('archive'/'forecast') so
    a caller can tell realised weather from a forecast.
    """
    from .weather import is_indoor  # existing totals-model helper

    if is_indoor(row.get("roof")):
        return None
    gameday = row.get("gameday")
    if gameday is None:
        return None
    date_str = str(gameday)[:10]
    lat, lon, _tz = _stadium_coords_and_tz(
        str(row.get("home_team")), row.get("stadium"), row.get("location"))
    if lat is None:
        return None
    hour = local_kickoff_hour(row.get("gametime"), gameday,
                              str(row.get("home_team")),
                              row.get("stadium"), row.get("location"))
    if hour is None:
        return None
    hourly = fetch_hourly(lat, lon, date_str, cache_dir=cache_dir)
    if hourly is None:
        return None
    vals = kickoff_values(hourly, hour)
    if vals is None:
        return None
    try:
        target = date.fromisoformat(date_str)
    except ValueError:
        return None
    vals["source"] = "archive" if target < date.today() else "forecast"
    vals["date"] = date_str
    return vals


def validate_against_recorded(games: pd.DataFrame, fetch_fn) -> dict:
    """Compare fetched kickoff weather against games.csv recorded
    wind/temp on outdoor games that have both.

    `fetch_fn(row_dict)` returns a kickoff dict like
    kickoff_weather_for_game (the caller supplies the fetch so this
    stays testable with fixtures and so a bulk run can reuse one
    cached fetch per stadium-day). Returns n, coverage, MAE and
    correlation for wind and temp, plus the per-game table. A large
    disagreement here would mean the live path and the backtest
    path are measuring different things -- the backtest numbers
    would still stand on the recorded values, but the live fetch
    could not be trusted to reproduce them.
    """
    rows = []
    for _, r in games.iterrows():
        if str(r.get("roof") or "").strip().lower() in config.INDOOR_ROOF_VALUES:
            continue
        wind = r.get("wind")
        temp = r.get("temp")
        try:
            wind = float(wind) if wind is not None and not pd.isna(wind) else None
            temp = float(temp) if temp is not None and not pd.isna(temp) else None
        except (TypeError, ValueError):
            wind, temp = None, None
        if wind is None or temp is None:
            continue
        got = fetch_fn(r.to_dict())
        if not got:
            continue
        rows.append({"season": int(r["season"]), "week": int(r["week"]),
                     "home_team": r["home_team"], "away_team": r["away_team"],
                     "gameday": str(r.get("gameday")),
                     "wind_recorded": wind, "wind_fetched": float(got["wind_mph"]),
                     "temp_recorded": temp, "temp_fetched": float(got["temp_f"])})
    out: dict = {"n_compared": len(rows), "rows": rows}
    if not rows:
        out.update({"wind_mae": None, "temp_mae": None,
                    "wind_corr": None, "temp_corr": None})
        return out
    import statistics

    def _mae(a, b):
        return round(sum(abs(x - y) for x, y in zip(a, b)) / len(a), 2)

    def _corr(a, b):
        if len(a) < 3:
            return None
        ma, mb = sum(a) / len(a), sum(b) / len(b)
        num = sum((x - ma) * (y - mb) for x, y in zip(a, b))
        den = math.sqrt(sum((x - ma) ** 2 for x in a) * sum((y - mb) ** 2 for y in b))
        return round(num / den, 3) if den else None

    out["wind_mae"] = _mae([r["wind_recorded"] for r in rows],
                           [r["wind_fetched"] for r in rows])
    out["temp_mae"] = _mae([r["temp_recorded"] for r in rows],
                           [r["temp_fetched"] for r in rows])
    out["wind_corr"] = _corr([r["wind_recorded"] for r in rows],
                             [r["wind_fetched"] for r in rows])
    out["temp_corr"] = _corr([r["temp_recorded"] for r in rows],
                             [r["temp_fetched"] for r in rows])
    return out


# ---------------------------------------------------------------------------
# Backtest: recorded kickoff wind/temp + point-in-time pass/run shares
# ---------------------------------------------------------------------------

def _variant_x(row: dict, variant: str) -> float:
    """Pre-registered interaction variable for one replay row (0.0
    when the game is indoor, the weather is unknown, or the feature
    rates are missing -- no imputed weather, no imputed share)."""
    if str(row.get("roof") or "").strip().lower() in config.INDOOR_ROOF_VALUES:
        return 0.0
    wind = row.get("wind")
    temp = row.get("temp")
    try:
        wind = float(wind) if wind is not None and not pd.isna(wind) else None
        temp = float(temp) if temp is not None and not pd.isna(temp) else None
    except (TypeError, ValueError):
        wind, temp = None, None
    if variant in ("wind_x_pass", "wind20_x_pass"):
        if wind is None or row.get("pass_diff") is None:
            return 0.0
        cutoff = float(getattr(config, "WEATHER_KICKOFF_WIND_PASS_MPH", 15)) \
            if variant == "wind_x_pass" else float(getattr(config, "WEATHER_KICKOFF_WIND_HIGH_MPH", 20))
        return max(0.0, wind - cutoff) * float(row["pass_diff"])
    if variant == "cold_x_run":
        if temp is None or row.get("run_diff") is None:
            return 0.0
        return max(0.0, float(getattr(config, "WEATHER_KICKOFF_COLD_F", 32)) - temp) \
            * float(row["run_diff"])
    return 0.0


def _ols_slope(xs: list[float], rs: list[float]) -> float:
    """No-intercept OLS slope of residuals on x (zeros contribute
    nothing to numerator or denominator, so games with x == 0 do
    not dilute the fit). Returns 0.0 when sum(x^2) is 0."""
    den = sum(x * x for x in xs)
    if den <= 0:
        return 0.0
    return sum(x * r for x, r in zip(xs, rs)) / den


def _grade_variant(rows: list[dict], variant: str, slope: float) -> dict:
    """Grade an additive variant on rows: shift = clip(slope * x).
    Returns the adjusted summary, the always-follow baseline on the
    same rows, and the paired comparison."""
    cap = float(getattr(config, "WEATHER_KICKOFF_ADJ_CAP", 3.0))
    base_outcomes, var_outcomes = [], []
    base_errs, var_errs = [], []
    n_adjusted = 0
    for r in rows:
        m, line, actual = (float(r["model_margin"]), float(r["spread_line"]),
                           float(r["actual_margin"]))
        shift = max(-cap, min(cap, slope * _variant_x(r, variant)))
        if shift != 0.0:
            n_adjusted += 1
        base_outcomes.append(sit_mod._grade(m, line, actual))
        var_outcomes.append(sit_mod._grade(m + shift, line, actual))
        base_errs.append(abs(actual - m))
        var_errs.append(abs(actual - (m + shift)))
    return {"adjusted": sit_mod._summ(var_outcomes),
            "always_follow": sit_mod._summ(base_outcomes),
            "paired_vs_follow": sit_mod._paired(base_outcomes, var_outcomes),
            "mae_adjusted": round(sum(var_errs) / len(var_errs), 3) if var_errs else None,
            "mae_follow": round(sum(base_errs) / len(base_errs), 3) if base_errs else None,
            "n_adjusted": n_adjusted, "slope": round(slope, 4)}


def _bucket_summary(rows: list[dict]) -> dict:
    """Descriptive ATS% / mean residual by recorded-weather bucket
    (outdoor games with known wind/temp only). Buckets are the
    totals model's own thresholds, so the spread read and the
    totals read use the same language."""
    def _sub(pred):
        return [r for r in rows if pred(r)]

    def _ats(sub):
        dec = [r for r in sub if float(r["actual_margin"]) != float(r["spread_line"])]
        if not dec:
            return {"n": len(sub), "decided": 0, "ats_pct": None, "mean_resid": None}
        wins = sum(1 for r in dec
                   if (float(r["actual_margin"]) > float(r["spread_line"]))
                   == (float(r["model_margin"]) > float(r["spread_line"])))
        resid = [float(r["actual_margin"]) - float(r["model_margin"]) for r in sub]
        return {"n": len(sub), "decided": len(dec),
                "ats_pct": round(100 * wins / len(dec), 2),
                "mean_resid": round(sum(resid) / len(resid), 2)}

    outdoor = [r for r in rows
               if str(r.get("roof") or "").strip().lower()
               not in config.INDOOR_ROOF_VALUES]
    known_wind = [r for r in outdoor if r.get("wind") is not None
                  and not pd.isna(r.get("wind"))]
    known_temp = [r for r in outdoor if r.get("temp") is not None
                  and not pd.isna(r.get("temp"))]
    return {
        "outdoor_n": len(outdoor), "known_wind_n": len(known_wind),
        "known_temp_n": len(known_temp),
        "wind_calm_lt15": _ats([r for r in known_wind if float(r["wind"]) < 15]),
        "wind_15_20": _ats([r for r in known_wind
                            if 15 <= float(r["wind"]) < 20]),
        "wind_20plus": _ats([r for r in known_wind if float(r["wind"]) >= 20]),
        "temp_above32": _ats([r for r in known_temp if float(r["temp"]) > 32]),
        "temp_32_or_below": _ats([r for r in known_temp if float(r["temp"]) <= 32]),
        "temp_20_or_below": _ats([r for r in known_temp if float(r["temp"]) <= 20]),
    }


def _totals_weather_check(games: pd.DataFrame, rows: list[dict]) -> dict:
    """Secondary, totals-only check: does wind suppress scoring
    relative to the total line? Joins the replay rows to the games
    frame for actual totals and total_line. No pick depends on this
    (the pool is ATS-only); it is the kickoff-level version of the
    totals model's published wind claim."""
    keyed = {(int(r["season"]), int(r["week"]), r["home_team"], r["away_team"]): r
             for r in rows}
    buckets: dict[str, list[float]] = {"wind_lt15": [], "wind_15_20": [],
                                       "wind_20plus": []}
    slopes_x, slopes_y = [], []
    for _, g in games.iterrows():
        k = (int(g["season"]), int(g["week"]), g["home_team"], g["away_team"])
        if k not in keyed:
            continue
        if str(g.get("roof") or "").strip().lower() in config.INDOOR_ROOF_VALUES:
            continue
        wind, total, line = g.get("wind"), g.get("total"), g.get("total_line")
        try:
            wind = float(wind) if wind is not None and not pd.isna(wind) else None
            total = float(total) if total is not None and not pd.isna(total) else None
            line = float(line) if line is not None and not pd.isna(line) else None
        except (TypeError, ValueError):
            continue
        if wind is None or total is None or line is None:
            continue
        resid = total - line
        b = "wind_lt15" if wind < 15 else "wind_15_20" if wind < 20 else "wind_20plus"
        buckets[b].append(resid)
        slopes_x.append(max(0.0, wind - 15))
        slopes_y.append(resid)
    out = {}
    for b, vals in buckets.items():
        out[b] = {"n": len(vals),
                  "mean_total_minus_line": round(sum(vals) / len(vals), 2) if vals else None}
    out["wind_slope_pts_per_mph_above15"] = round(_ols_slope(slopes_x, slopes_y), 3) \
        if slopes_x else None
    out["n"] = sum(len(v) for v in buckets.values())
    return out


def evaluate_all(games: pd.DataFrame, seasons: list[int] | None = None,
                 train_seasons: tuple[int, ...] = TRAIN_SEASONS,
                 test_seasons: tuple[int, ...] = TEST_SEASONS,
                 team_stats_by_season: dict | None = None) -> dict:
    """Full item-3 evaluation. Replays the baseline with weather
    context (recorded kickoff wind/temp/roof from games.csv +
    point-in-time pass/run-share differentials from team stats),
    then tests the 3 pre-registered additive variants with the
    group-A promotion gate. `team_stats_by_season` maps season ->
    team-stats frame (stats_team_week); when omitted, pass/run
    differentials are None and the interaction variants degrade to
    x == 0 (reported as such, not silently skipped). Returns the
    baseline summary (must reproduce 50.03% / 898 / 1,795), bucket
    descriptives, the totals-side check, per-variant results, and
    `promoted_variants` (empty unless a variant clears every gate).
    """
    if seasons is None:
        seasons = list(range(2019, 2026))
    games = backtest_mod.load_games_frame(games)

    # Point-in-time feature rates per (season, week): team stats for
    # weeks < W only, computed once per (season, week) pair.
    rates_by_sw: dict = {}
    if team_stats_by_season:
        for season in seasons:
            ts = team_stats_by_season.get(season)
            if ts is None or ts.empty:
                continue
            ts = ts.copy()
            ts["week"] = pd.to_numeric(ts["week"], errors="coerce")
            for week in sorted(games[games["season"] == season]["week"].unique().tolist()):
                sub = ts[ts["week"] < week]
                rates_by_sw[(season, week)] = features_mod.team_feature_rates(sub)

    def _ctx(season, week, row, ratings, games_played, hfa, season_games,
             played_current, prior_games):
        rates = rates_by_sw.get((season, week), {})
        league = features_mod.league_rates(rates) if rates else {}
        home, away = str(row["home_team"]), str(row["away_team"])
        hp = rates.get(home, {}).get("pass_share")
        ap = rates.get(away, {}).get("pass_share")
        pass_diff = (hp - ap) if (hp is not None and ap is not None) else None
        return {"wind": row.get("wind"), "temp": row.get("temp"),
                "roof": row.get("roof"),
                "pass_diff": pass_diff,
                "run_diff": (-pass_diff) if pass_diff is not None else None}

    rows = sit_mod.replay_with_context(games, seasons, context_fn=_ctx)
    base = sit_mod._summ([r["ats"] for r in rows])
    buckets = _bucket_summary(rows)
    totals_check = _totals_weather_check(games, rows)
    train_rows = [r for r in rows if r["season"] in train_seasons]
    test_rows = [r for r in rows if r["season"] in test_seasons]
    min_decided = int(getattr(config, "WEATHER_KICKOFF_MIN_TRAIN_DECIDED", 30))

    import pandas as _pd

    def _frame(sub_rows):
        return _pd.DataFrame([{"season": r["season"], "week": r["week"],
                               "home_team": r["home_team"], "away_team": r["away_team"],
                               "model_margin": r["model_margin"], "spread_line": r["spread_line"],
                               "actual_margin": r["actual_margin"], "ats": r["ats"]}
                              for r in sub_rows])

    placebo_test = sit_mod.placebo_additive_distribution(_frame(test_rows))
    placebo_full = sit_mod.placebo_additive_distribution(_frame(rows))

    per_variant: dict[str, dict] = {}
    promoted: list[str] = []
    n_tests = len(VARIANTS)
    for variant in VARIANTS:
        train_decided = [r for r in train_rows
                         if _variant_x(r, variant) != 0.0
                         and float(r["actual_margin"]) != float(r["spread_line"])]
        if len(train_decided) < min_decided:
            slope = 0.0
            learned = False
        else:
            xs = [_variant_x(r, variant) for r in train_rows]
            rs = [float(r["actual_margin"]) - float(r["model_margin"]) for r in train_rows]
            slope = _ols_slope(xs, rs)
            learned = True
        test_res = _grade_variant(test_rows, variant, slope)
        full_res = _grade_variant(rows, variant, slope)
        p_raw = full_res["paired_vs_follow"]["p_two_sided"]
        p_adj = min(1.0, p_raw * n_tests)
        gate = bool(learned
                    and test_res["paired_vs_follow"]["paired_delta_pp"] > 0
                    and test_res["paired_vs_follow"]["paired_delta_pp"] > placebo_test["p95_pp"]
                    and p_adj < 0.05)
        if gate:
            promoted.append(variant)
        per_variant[variant] = {
            "learned": learned, "train_decided_nonzero_x": len(train_decided),
            "slope": round(slope, 4), "test": test_res, "full": full_res,
            "p_bonferroni": round(p_adj, 4),
            "placebo_add_p95_pp": placebo_test["p95_pp"],
            "gate_pass": gate,
        }
    return {"baseline": base, "n": len(rows),
            "train_seasons": list(train_seasons), "test_seasons": list(test_seasons),
            "bonferroni_family_n": n_tests,
            "placebo_additive": placebo_full,
            "placebo_additive_test": placebo_test,
            "buckets": buckets, "totals_check": totals_check,
            "team_stats_loaded": bool(rates_by_sw),
            "per_variant": per_variant, "promoted_variants": promoted,
            "_rows": rows}


if __name__ == "__main__":
    import sys
    _games_csv = sys.argv[1] if len(sys.argv) > 1 else "/tmp/games.csv"
    _ts_dir = sys.argv[2] if len(sys.argv) > 2 else "/tmp/nfl-b5b"
    _games = pd.read_csv(_games_csv, low_memory=False)
    _ts = {}
    for _s in range(2019, 2026):
        _p = os.path.join(_ts_dir, f"ts_{_s}.csv")
        if os.path.exists(_p):
            _ts[_s] = pd.read_csv(_p, low_memory=False)
    _out = evaluate_all(_games, team_stats_by_season=_ts)
    _out.pop("_rows", None)
    print(json.dumps(_out, indent=2, default=str))
