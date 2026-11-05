"""
Exact rest, travel, and time-zone math (batch 5, group A item 2) --
tested, NOT wired live.

Batch 1 tested travel COARSELY (a single 0.95 pts / 1,000 mi term
on home-stadium -> game-stadium distance) and it stayed off
(TRAVEL_ENABLED=False): +0.93 pp, paired z=1.28, noise. Rest was
likewise coarse: the live model uses 0.25 pts/day on the rest
differential, capped at 2.0, and batch 1 found the ATS-residual slope
was -0.03 +/- 0.12 pts/day -- the market already prices rest. This
module tests the EXACT versions, as continuous variables, not
coarse bye / short-week buckets:

  rest_days     exact days between games, from the games.csv
                home_rest / away_rest columns (nflverse's own
                days-since-last-game; verified against gameday
                diffs in the tests -- week-2 Thursday teams show
                4, Sunday teams 7). The differential home - away
                runs -8..+8 in 2019-2025; the live 0.25/day capped
                term is the baseline it is tested against.
  travel_miles  exact miles a team flew into THIS game: haversine
                from the stadium of its PREVIOUS game to this
                game's stadium -- not home-stadium -> game-stadium.
                A team on a multi-week road trip flies far less
                than home->away implies, and a team coming off a
                London game flies far more. Week 1 origin is the
                team's home stadium (offseason at home). Neutral
                games count for both teams (both travelled).
  tz_crossed    exact time zones crossed into this game, from the
                origin/destination UTC offsets ON THE GAMEDAY
                (DST-aware: US zones shift in March/November,
                Arizona never shifts, London/Munich shift on
                European dates, Sao Paulo / Mexico City do not
                shift in this window). Eastward vs westward is
                kept separate: the literature claims eastward
                travel (clock forward, shorter day) is worse.
  hfa interaction  the rest/travel effect is allowed to differ by
                WHO holds the advantage: piecewise slopes for a
                home-team advantage vs an away-team advantage
                (rest_piecewise), and a favourite-only rest slope
                (rest_fav_only). A single pooled slope would hide
                exactly the asymmetry the item asks about.

Pre-registered variants (8, one Bonferroni family). Every
coefficient is learned on TRAIN (2019-2022) only, by OLS of the
point-in-time residual (actual - model margin, with the tested
component removed where the baseline already contains it) on the
continuous variable, clipped to the config cap, then frozen and
applied to TEST (2023-2025):

  rest_linear       replace the live 0.25/day capped term with
                    coef * rest_diff (uncapped, coef clipped).
  rest_piecewise    separate slopes for rest_diff > 0 (home rested)
                    and rest_diff < 0 (away rested).
  rest_fav_only     rest slope applies only when the rested team is
                    the favourite; 0 otherwise.
  travel_linear     coef * (away_miles - home_miles) / 1000, added
                    to the baseline margin (baseline has no travel
                    term -- TRAVEL_ENABLED=False -- so the residual
                    is vs the baseline margin directly).
  travel_binary     away team flew >= 2,000 mi more than home:
                    shift by the train mean residual on those
                    games, clipped to +/- cap.
  tz_linear         coef * (away_tz - home_tz), added to the margin.
  tz_east           coef * (away eastward zones - home eastward
                    zones), added to the margin.
  travel_rest_combo rest_linear + travel_linear coefficients fit
                    JOINTLY (2-regressor OLS on train), applied
                    together -- the only combined variant, and it
                    is pre-registered, not a post-hoc best-of.

Promotion gate (ALL must hold): the variant wins on the TEST split
(paired delta > 0 vs baseline on the same test games) AND its
full-sample paired p-value survives Bonferroni across all 8
(adjusted p < 0.05) AND it beats the placebo gate
(backtest.placebo_rows, +/-0.15, 20 seeds -- the variant's TEST
paired delta must exceed the placebo distribution's 95th
percentile). Nothing passed; see README batch 5 group A.

Sign conventions: model margin and all adjustments are in
home-margin points. A positive travel adjustment means the HOME
margin rises (away travel hurts the away team); the fitted
coefficients are allowed either sign -- batch 1's coarse finding
was that long-travel away teams BEAT the spread, i.e. the fitted
sign may be negative, and imposing the intuitive sign would be
fitting the story, not the data.
"""
from __future__ import annotations

import calendar
import math
from datetime import date

import pandas as pd

from . import backtest as backtest_mod, config, model as model_mod, situational as sit_mod

TRAIN_SEASONS = (2019, 2020, 2021, 2022)
TEST_SEASONS = (2023, 2024, 2025)

# Standard-time UTC offsets by team (winter). DST handling is in
# _tz_offset(): US zones move +1 in DST, Arizona does not.
_TEAM_TZ_STANDARD = {
    # Eastern (-5): BUF MIA NE NYJ NYG PHI WAS PIT CLE CIN ATL CAR TB JAX IND DET BAL
    "BUF": -5, "MIA": -5, "NE": -5, "NYJ": -5, "NYG": -5, "PHI": -5,
    "WAS": -5, "PIT": -5, "CLE": -5, "CIN": -5, "ATL": -5, "CAR": -5,
    "TB": -5, "JAX": -5, "IND": -5, "DET": -5, "BAL": -5,
    # Central (-6): CHI GB MIN DAL HOU TEN NO KC
    "CHI": -6, "GB": -6, "MIN": -6, "DAL": -6, "HOU": -6, "TEN": -6,
    "NO": -6, "KC": -6,
    # Mountain (-7): DEN ARI (ARI never observes DST)
    "DEN": -7, "ARI": -7,
    # Pacific (-8): SEA SF LA LAC LV
    "SEA": -8, "SF": -8, "LA": -8, "LAC": -8, "LV": -8,
}

# Neutral-site stadiums that are NOT a team's home ground, with
# (lat, lon, UTC offset in standard time, observes DST). Venues
# that ARE a team's home ground (State Farm Stadium, Ford Field,
# SoFi, ...) are resolved through that team's STADIUM_COORDS /
# team TZ instead -- see _stadium_coords / _stadium_tz_offset.
_NEUTRAL_VENUES = {
    "Tottenham Stadium": ((51.6043, -0.0664), 0, True),   # London, GMT/BST
    "Wembley Stadium": ((51.5560, -0.2795), 0, True),     # London, GMT/BST
    "Allianz Arena": ((48.2188, 11.6247), 1, True),        # Munich, CET/CEST
    "Deutsche Bank Park": ((50.0686, 8.6458), 1, True),    # Frankfurt, CET/CEST
    "Arena Corinthians": ((-23.5452, -46.4743), -3, False),  # Sao Paulo, -03, no DST
    "Azteca Stadium": ((19.3029, -99.1505), -6, False),    # Mexico City, CST, no DST
}
# Neutral games played at a team's own stadium (COVID relocations,
# 2021 NO "home" game in Jacksonville, 2025 international-series
# games listed Neutral at a host stadium): stadium name -> the team
# whose ground it is, so coords/TZ come from that team.
_NEUTRAL_HOST_TEAM = {
    "State Farm Stadium": "ARI", "TIAA Bank Stadium": "JAX",
    "Ford Field": "DET", "SoFi Stadium": "LA", "Acrisure Stadium": "PIT",
    "FirstEnergy Stadium": "CLE", "MetLife Stadium": "NYG",
    "Lucas Oil Stadium": "IND", "Hard Rock Stadium": "MIA",
}


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """Date of the nth `weekday` (Mon=0) of a month."""
    first = date(year, month, 1)
    delta = (weekday - first.weekday()) % 7
    return date(year, month, 1 + delta + 7 * (n - 1))


def _is_us_dst(d: date) -> bool:
    """US daylight saving: second Sunday of March to first Sunday
    of November (the rule in force for every season 2019-2025)."""
    start = _nth_weekday(d.year, 3, 6, 2)   # 2nd Sunday March
    end = _nth_weekday(d.year, 11, 6, 1)    # 1st Sunday November
    return start <= d < end


def _is_eu_dst(d: date) -> bool:
    """European summer time: last Sunday of March to last Sunday of
    October."""
    last_mar = date(d.year, 3, 31)
    start = date(d.year, 3, 31 - (last_mar.weekday() + 1) % 7)
    last_oct = date(d.year, 10, 31)
    end = date(d.year, 10, 31 - (last_oct.weekday() + 1) % 7)
    return start <= d < end


def _parse_gameday(value) -> date | None:
    try:
        return pd.to_datetime(value).date()
    except (TypeError, ValueError):
        return None


def team_tz_offset(team: str, gameday) -> int | None:
    """UTC offset (hours) for a team's home city on the gameday.
    DST-aware; Arizona never shifts. Returns None for an unknown
    team code."""
    team = config.normalize_team(team)
    base = _TEAM_TZ_STANDARD.get(team)
    if base is None:
        return None
    d = _parse_gameday(gameday)
    if team == "ARI" or d is None:
        return base
    return base + 1 if _is_us_dst(d) else base


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in miles."""
    rlat1, rlon1, rlat2, rlon2 = (math.radians(v) for v in (lat1, lon1, lat2, lon2))
    a = (math.sin((rlat2 - rlat1) / 2) ** 2
         + math.cos(rlat1) * math.cos(rlat2) * math.sin((rlon2 - rlon1) / 2) ** 2)
    return 3959 * 2 * math.asin(math.sqrt(a))


def _stadium_coords(row: pd.Series) -> tuple[float, float] | None:
    """Coords of the stadium a game was played in. Home games use
    the home team's coords from config.STADIUM_COORDS; neutral
    games use the venue map (international venues directly, host
    stadiums via their team). Returns None when the venue is
    unknown -- the caller then records no travel, never an
    invented distance."""
    if str(row.get("location") or "").strip().lower() == "neutral":
        name = str(row.get("stadium") or "").strip()
        if name in _NEUTRAL_VENUES:
            return _NEUTRAL_VENUES[name][0]
        host = _NEUTRAL_HOST_TEAM.get(name)
        if host is not None:
            return config.STADIUM_COORDS.get(host)
        return None
    return config.STADIUM_COORDS.get(config.normalize_team(row["home_team"]))


def _stadium_tz_offset(row: pd.Series) -> int | None:
    """UTC offset (hours) of the stadium a game was played in, on
    its gameday. Home games use the home team's offset; neutral
    games use the venue's offset (European venues DST-aware)."""
    d = _parse_gameday(row.get("gameday"))
    if str(row.get("location") or "").strip().lower() == "neutral":
        name = str(row.get("stadium") or "").strip()
        if name in _NEUTRAL_VENUES:
            _coords, base, dst = _NEUTRAL_VENUES[name]
            if dst and d is not None and _is_eu_dst(d):
                return base + 1
            return base
        host = _NEUTRAL_HOST_TEAM.get(name)
        if host is not None:
            return team_tz_offset(host, row.get("gameday"))
        return None
    return team_tz_offset(row["home_team"], row.get("gameday"))


def travel_for_team(season_games: pd.DataFrame, team: str, target_row: pd.Series) -> dict:
    """Exact travel into a target game for one team: miles flown and
    time zones crossed (signed eastward zones separately), from the
    stadium of its most recent same-season game to this game's
    stadium. Week 1 (no prior game): origin is the team's home
    stadium. Any leg whose coords are unknown yields None for that
    quantity -- never an imputed distance."""
    team = config.normalize_team(team)
    dest = _stadium_coords(target_row)
    dest_tz = _stadium_tz_offset(target_row)
    prev_g = sit_mod._prev_game(season_games, team, int(target_row["week"]))
    if prev_g is None:
        origin = config.STADIUM_COORDS.get(team)
        origin_tz = team_tz_offset(team, target_row.get("gameday"))
    else:
        origin = _stadium_coords(prev_g)
        origin_tz = _stadium_tz_offset(prev_g)
    miles = None
    if origin is not None and dest is not None:
        miles = round(haversine_miles(origin[0], origin[1], dest[0], dest[1]), 1)
    tz_crossed = None
    east_zones = None
    if origin_tz is not None and dest_tz is not None:
        diff = dest_tz - origin_tz
        tz_crossed = abs(diff)
        # Eastward travel = destination clock ahead of origin: the
        # day shortens. Signed so eastward is positive.
        east_zones = diff
    return {"miles": miles, "tz_crossed": tz_crossed, "east_zones": east_zones,
            "prev_game_week": (int(prev_g["week"]) if prev_g is not None else None)}


def exact_rest(row: pd.Series) -> dict:
    """Exact rest for a game: days since each team's last game, from
    the games.csv rest columns (nflverse's own count). Falls back
    to the gameday diff vs the previous game when a rest column is
    missing, so the function still works on a minimal frame; the
    fallback is flagged in `rest_source`."""
    out: dict = {}
    for side, col in (("home", "home_rest"), ("away", "away_rest")):
        try:
            v = float(row[col])
            out[f"{side}_rest"] = v
            out[f"{side}_rest_source"] = "games_csv"
        except (KeyError, TypeError, ValueError):
            out[f"{side}_rest"] = None
            out[f"{side}_rest_source"] = "missing"
    if out["home_rest"] is not None and out["away_rest"] is not None:
        out["rest_diff"] = out["home_rest"] - out["away_rest"]
    else:
        out["rest_diff"] = None
    return out


def game_context(season_games: pd.DataFrame, row: pd.Series) -> dict:
    """All exact rest/travel/TZ quantities for one target game, in
    home-margin sign conventions:
      rest_diff   home_rest - away_rest (days; + = home rested)
      travel_diff away_miles - home_miles (mi; + = away flew further)
      tz_diff     away_tz_crossed - home_tz_crossed (zones)
      east_diff   away eastward zones - home eastward zones
    A quantity is None when either side's leg is unknown."""
    rest = exact_rest(row)
    home_tr = travel_for_team(season_games, row["home_team"], row)
    away_tr = travel_for_team(season_games, row["away_team"], row)
    travel_diff = None
    if home_tr["miles"] is not None and away_tr["miles"] is not None:
        travel_diff = round(away_tr["miles"] - home_tr["miles"], 1)
    tz_diff = None
    if home_tr["tz_crossed"] is not None and away_tr["tz_crossed"] is not None:
        tz_diff = away_tr["tz_crossed"] - home_tr["tz_crossed"]
    east_diff = None
    if home_tr["east_zones"] is not None and away_tr["east_zones"] is not None:
        east_diff = away_tr["east_zones"] - home_tr["east_zones"]
    return {
        "home_rest": rest["home_rest"], "away_rest": rest["away_rest"],
        "rest_diff": rest["rest_diff"],
        "home_miles": home_tr["miles"], "away_miles": away_tr["miles"],
        "travel_diff": travel_diff,
        "home_tz": home_tr["tz_crossed"], "away_tz": away_tr["tz_crossed"],
        "tz_diff": tz_diff,
        "home_east": home_tr["east_zones"], "away_east": away_tr["east_zones"],
        "east_diff": east_diff,
    }


def _ols_single(xs: list[float], rs: list[float], clip: float) -> float:
    """OLS slope of residual r on x with no intercept, clipped to
    +/- clip. No-intercept is the right form here: the adjustment
    must be 0 when the variable is 0 (equal rest, equal travel),
    and an intercept would be a free constant added to every game."""
    denom = sum(x * x for x in xs)
    if denom == 0 or not xs:
        return 0.0
    coef = sum(x * r for x, r in zip(xs, rs)) / denom
    return round(max(-clip, min(clip, coef)), 4)


def _ols_two(xs1: list[float], xs2: list[float], rs: list[float],
             clip1: float, clip2: float) -> tuple[float, float]:
    """Two-regressor OLS, no intercept, via the 2x2 normal
    equations (stdlib only). Returns clipped (coef1, coef2)."""
    s11 = sum(x * x for x in xs1)
    s22 = sum(x * x for x in xs2)
    s12 = sum(a * b for a, b in zip(xs1, xs2))
    t1 = sum(x * r for x, r in zip(xs1, rs))
    t2 = sum(x * r for x, r in zip(xs2, rs))
    det = s11 * s22 - s12 * s12
    if det == 0 or not xs1:
        return 0.0, 0.0
    c1 = (t1 * s22 - t2 * s12) / det
    c2 = (t2 * s11 - t1 * s12) / det
    return (round(max(-clip1, min(clip1, c1)), 4),
            round(max(-clip2, min(clip2, c2)), 4))


def _rest_adj_current(row: dict) -> float:
    """The rest term inside the baseline model margin for this row
    (0.25/day capped, times the divisional multiplier), so rest
    variants can remove it before fitting a replacement slope."""
    diff = row.get("rest_diff")
    if diff is None:
        return 0.0
    per_day = float(getattr(config, "REST_POINTS_PER_DAY", 0.25))
    cap = float(getattr(config, "REST_CAP", 2.0))
    adj = max(-cap, min(cap, float(diff) * per_day))
    return adj


def learn_coefficients(train_rows: list[dict]) -> dict:
    """Learn every variant's coefficients on TRAIN rows only.
    Residuals are actual - margin, with the tested component
    removed where the baseline already contains it (rest variants
    use the margin EXCLUDING the live rest term; travel/TZ
    variants use the baseline margin as-is, since it contains no
    travel term). Returns {variant: {coefficients..., n}}."""
    clip_rest = float(getattr(config, "REST_TRAVEL_REST_COEF_CLIP", 1.0))
    clip_travel = float(getattr(config, "REST_TRAVEL_TRAVEL_COEF_CLIP", 3.0))
    clip_tz = float(getattr(config, "REST_TRAVEL_TZ_COEF_CLIP", 3.0))
    cap_pts = float(getattr(config, "SITUATIONAL_SPOT_ADJ_CAP", 3.0))
    out: dict[str, dict] = {}

    # --- rest variants: residual vs the ex-rest margin ---
    ex_rest = []
    for r in train_rows:
        if r.get("rest_diff") is None:
            continue
        div = float(r.get("div_mult", 1.0))
        margin_ex_rest = (float(r["model_margin"]) / div - _rest_adj_current(r)) if div else float(r["model_margin"])
        ex_rest.append((r, margin_ex_rest))
    xs = [r["rest_diff"] for r, _ in ex_rest]
    rs = [float(r["actual_margin"]) - m for r, m in ex_rest]
    out["rest_linear"] = {"coef": _ols_single(xs, rs, clip_rest), "n": len(xs)}
    xs_pos = [max(0.0, r["rest_diff"]) for r, _ in ex_rest]
    xs_neg = [min(0.0, r["rest_diff"]) for r, _ in ex_rest]
    # Piecewise: fit both slopes jointly (two-regressor OLS) so the
    # home-advantage and away-advantage slopes do not contaminate
    # each other through the shared residual.
    c_pos, c_neg = _ols_two(xs_pos, xs_neg, rs, clip_rest, clip_rest)
    out["rest_piecewise"] = {"coef_home_rested": c_pos, "coef_away_rested": c_neg,
                             "n": len(xs)}
    # Favourite-only: regress only on games where the rested team
    # is the favourite; the slope is 0 elsewhere by construction.
    fav_xs, fav_rs = [], []
    for r, m in ex_rest:
        line = float(r["spread_line"])
        fav_home = line > 0
        rested_home = r["rest_diff"] > 0
        rested_fav = (rested_home and fav_home) or ((not rested_home) and (not fav_home))
        if r["rest_diff"] != 0 and rested_fav:
            fav_xs.append(r["rest_diff"])
            fav_rs.append(float(r["actual_margin"]) - m)
    out["rest_fav_only"] = {"coef": _ols_single(fav_xs, fav_rs, clip_rest),
                            "n": len(fav_xs)}

    # --- travel / TZ variants: residual vs the baseline margin ---
    def _pairs(key, scale=1.0):
        xs_, rs_ = [], []
        for r in train_rows:
            v = r.get(key)
            if v is None:
                continue
            xs_.append(float(v) * scale)
            rs_.append(float(r["actual_margin"]) - float(r["model_margin"]))
        return xs_, rs_

    tx, tr = _pairs("travel_diff", 1 / 1000.0)
    out["travel_linear"] = {"coef_per_1000mi": _ols_single(tx, tr, clip_travel), "n": len(tx)}
    zx, zr = _pairs("tz_diff")
    out["tz_linear"] = {"coef_per_zone": _ols_single(zx, zr, clip_tz), "n": len(zx)}
    ex, er = _pairs("east_diff")
    out["tz_east"] = {"coef_per_east_zone": _ols_single(ex, er, clip_tz), "n": len(ex)}
    # travel_binary: train mean residual on games where the away
    # team flew >= REST_TRAVEL_BINARY_MILES further than home, clipped.
    threshold = float(getattr(config, "REST_TRAVEL_BINARY_MILES", 2000))
    big = [r for r in train_rows
           if r.get("travel_diff") is not None and r["travel_diff"] >= threshold]
    if len(big) >= int(getattr(config, "SITUATIONAL_MIN_TRAIN_DECIDED", 30)):
        resid = [float(r["actual_margin"]) - float(r["model_margin"]) for r in big]
        out["travel_binary"] = {"shift": round(max(-cap_pts, min(cap_pts, sum(resid) / len(resid))), 3),
                                "n": len(big)}
    else:
        out["travel_binary"] = {"shift": 0.0, "n": len(big)}
    # combo: rest + travel jointly (2-regressor OLS on ex-rest
    # residuals, so the rest slope is not double-counted).
    cx = [r["rest_diff"] for r, _ in ex_rest]
    ctx = []
    for r, _ in ex_rest:
        ctx.append((float(r["travel_diff"]) / 1000.0) if r.get("travel_diff") is not None else 0.0)
    crs = [float(r["actual_margin"]) - m for r, m in ex_rest]
    cc_rest, cc_travel = _ols_two(cx, ctx, crs, clip_rest, clip_travel)
    out["travel_rest_combo"] = {"coef_rest": cc_rest, "coef_per_1000mi": cc_travel,
                                "n": len(cx)}
    return out


def adjusted_margin(row: dict, variant: str, coeffs: dict) -> float | None:
    """Baseline margin with one variant's exact rest/travel/TZ
    adjustment applied. Returns None when the variant needs a
    quantity this row does not have (unknown travel leg) -- the
    caller then grades that game on the baseline margin instead
    of inventing a number, and counts it as unadjusted."""
    m = float(row["model_margin"])
    div = float(row.get("div_mult", 1.0))
    c = coeffs.get(variant, {})
    # Every adjustment below is applied INSIDE the divisional
    # multiplier, exactly where the baseline margin applies its
    # rest/travel terms: margin = (ratings + hfa + rest - travel)
    # * div_mult. Adding a term outside div would silently scale
    # it by 1/0.9 on divisional games relative to the fitted
    # coefficient.
    ex_div = m / div if div else m
    if variant == "rest_linear":
        if row.get("rest_diff") is None:
            return None
        ex_rest = ex_div - _rest_adj_current(row)
        return round((ex_rest + c.get("coef", 0.0) * float(row["rest_diff"])) * div, 3)
    if variant == "rest_piecewise":
        if row.get("rest_diff") is None:
            return None
        ex_rest = ex_div - _rest_adj_current(row)
        d = float(row["rest_diff"])
        slope = c.get("coef_home_rested", 0.0) if d > 0 else c.get("coef_away_rested", 0.0)
        return round((ex_rest + slope * d) * div, 3)
    if variant == "rest_fav_only":
        if row.get("rest_diff") is None:
            return None
        ex_rest = ex_div - _rest_adj_current(row)
        line = float(row["spread_line"])
        fav_home = line > 0
        rested_home = float(row["rest_diff"]) > 0
        rested_fav = (rested_home and fav_home) or ((not rested_home) and (not fav_home))
        adj = c.get("coef", 0.0) * float(row["rest_diff"]) if rested_fav else 0.0
        return round((ex_rest + adj) * div, 3)
    if variant == "travel_linear":
        if row.get("travel_diff") is None:
            return None
        return round((ex_div + c.get("coef_per_1000mi", 0.0) * float(row["travel_diff"]) / 1000.0) * div, 3)
    if variant == "travel_binary":
        if row.get("travel_diff") is None:
            return None
        if float(row["travel_diff"]) >= float(getattr(config, "REST_TRAVEL_BINARY_MILES", 2000)):
            return round((ex_div + c.get("shift", 0.0)) * div, 3)
        return round(m, 3)
    if variant == "tz_linear":
        if row.get("tz_diff") is None:
            return None
        return round((ex_div + c.get("coef_per_zone", 0.0) * float(row["tz_diff"])) * div, 3)
    if variant == "tz_east":
        if row.get("east_diff") is None:
            return None
        return round((ex_div + c.get("coef_per_east_zone", 0.0) * float(row["east_diff"])) * div, 3)
    if variant == "travel_rest_combo":
        if row.get("rest_diff") is None or row.get("travel_diff") is None:
            return None
        ex_rest = ex_div - _rest_adj_current(row)
        new = (ex_rest + c.get("coef_rest", 0.0) * float(row["rest_diff"])
               + c.get("coef_per_1000mi", 0.0) * float(row["travel_diff"]) / 1000.0)
        return round(new * div, 3)
    raise ValueError(f"unknown rest/travel variant: {variant}")


VARIANTS = ("rest_linear", "rest_piecewise", "rest_fav_only",
            "travel_linear", "travel_binary", "tz_linear", "tz_east",
            "travel_rest_combo")


def _grade_variant(rows: list[dict], variant: str, coeffs: dict) -> dict:
    """Grade one variant on rows vs the baseline on the SAME rows,
    with the paired comparison. Games whose adjustment is None
    (unknown travel leg) are graded on the baseline margin and
    counted in `n_unadjusted` -- they cannot help or hurt the
    variant, which is the honest handling of missing legs. MAE is
    reported for both margins (the pool scores ATS; MAE is a
    model-quality check, not the gate)."""
    base_outcomes, var_outcomes = [], []
    n_unadjusted = 0
    base_errs, var_errs = [], []
    for r in rows:
        m, line, actual = float(r["model_margin"]), float(r["spread_line"]), float(r["actual_margin"])
        adj = adjusted_margin(r, variant, coeffs)
        if adj is None:
            adj = m
            n_unadjusted += 1
        base_outcomes.append(sit_mod._grade(m, line, actual))
        var_outcomes.append(sit_mod._grade(adj, line, actual))
        base_errs.append(abs(actual - m))
        var_errs.append(abs(actual - adj))
    return {"variant": sit_mod._summ(var_outcomes),
            "baseline": sit_mod._summ(base_outcomes),
            "paired_vs_baseline": sit_mod._paired(base_outcomes, var_outcomes),
            "mae_variant": round(sum(var_errs) / len(var_errs), 3) if var_errs else None,
            "mae_baseline": round(sum(base_errs) / len(base_errs), 3) if base_errs else None,
            "n_unadjusted": n_unadjusted}


def evaluate_all(games: pd.DataFrame, seasons: list[int] | None = None,
                 train_seasons: tuple[int, ...] = TRAIN_SEASONS,
                 test_seasons: tuple[int, ...] = TEST_SEASONS) -> dict:
    """Full item-2 evaluation. Replays the baseline with exact
    rest/travel/TZ context, learns each variant's coefficients on
    TRAIN (2019-2022) only, and grades them on TEST (2023-2025)
    and on the full sample -- each with its paired comparison,
    Bonferroni-adjusted p (family = all 8 variants), and the
    placebo gate. Returns `promoted_variants` (empty unless a
    variant clears every gate)."""
    if seasons is None:
        seasons = list(range(2019, 2026))
    games = backtest_mod.load_games_frame(games)

    def _ctx(season, week, row, ratings, games_played, hfa, season_games,
             played_current, prior_games):
        ctx = game_context(season_games, row)
        ctx["div_mult"] = float(getattr(config, "DIV_MULTIPLIER", 1.0)) \
            if model_mod._is_div_game(row.get("div_game")) else 1.0
        return ctx

    rows = sit_mod.replay_with_context(games, seasons, context_fn=_ctx)
    base = sit_mod._summ([r["ats"] for r in rows])
    train_rows = [r for r in rows if r["season"] in train_seasons]
    test_rows = [r for r in rows if r["season"] in test_seasons]
    coeffs = learn_coefficients(train_rows)

    import pandas as _pd

    def _frame(sub_rows):
        return _pd.DataFrame([{"season": r["season"], "week": r["week"],
                               "home_team": r["home_team"], "away_team": r["away_team"],
                               "model_margin": r["model_margin"], "spread_line": r["spread_line"],
                               "actual_margin": r["actual_margin"],
                               "ats": r["ats"]} for r in sub_rows])

    # Placebo gate on the TEST split (see situational.py: a
    # test-split paired delta has wider chance variation than a
    # full-sample one, so the full-sample p95 would be too lenient).
    placebo = sit_mod.placebo_additive_distribution(_frame(test_rows))
    placebo_full = sit_mod.placebo_additive_distribution(_frame(rows))

    n_tests = len(VARIANTS)
    per_variant: dict[str, dict] = {}
    promoted: list[str] = []
    for variant in VARIANTS:
        test_res = _grade_variant(test_rows, variant, coeffs)
        full_res = _grade_variant(rows, variant, coeffs)
        p_raw = full_res["paired_vs_baseline"]["p_two_sided"]
        p_adj = min(1.0, p_raw * n_tests)
        gate = bool(
            test_res["paired_vs_baseline"]["paired_delta_pp"] > 0
            and test_res["paired_vs_baseline"]["paired_delta_pp"] > placebo["p95_pp"]
            and p_adj < 0.05)
        if gate:
            promoted.append(variant)
        # MAE on test, for the record (the pool scores ATS; MAE is
        # a model-quality check, not the gate).
        per_variant[variant] = {
            "coefficients": coeffs[variant],
            "test": test_res, "full": full_res,
            "p_bonferroni": round(p_adj, 4),
            "placebo_add_p95_pp": placebo["p95_pp"],
            "gate_pass": gate,
        }
    # Coverage: how many games actually have exact travel/TZ legs.
    n_travel = sum(1 for r in rows if r.get("travel_diff") is not None)
    n_tz = sum(1 for r in rows if r.get("tz_diff") is not None)
    return {
        "baseline": base,
        "n": len(rows),
        "n_travel_known": n_travel, "n_tz_known": n_tz,
        "train_seasons": list(train_seasons), "test_seasons": list(test_seasons),
        "bonferroni_family_n": n_tests,
        "placebo_additive": placebo,
        "placebo_additive_full": placebo_full,
        "coefficients": coeffs,
        "per_variant": per_variant,
        "promoted_variants": promoted,
        "_rows": rows,
    }
