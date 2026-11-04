"""
All tunable settings live here. Nothing else in the codebase should
hardcode a magic number that a user might reasonably want to change.
"""
import os

# ---- Identity / notification ----
EMAIL = os.environ.get("PICKS_EMAIL", "jakobfreele@gmail.com")
SEASON = int(os.environ.get("PICKS_SEASON", "2026"))

# ---- Data source ----
# nflverse publishes a clean historical schedule/results file (same data
# your Apps Script used) and weekly injury reports.
GAMES_URL = "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv"
INJURIES_URL_TMPL = "https://github.com/nflverse/nflverse-data/releases/download/injuries/injuries_{season}.csv"
DEPTH_CHARTS_URL_TMPL = "https://github.com/nflverse/nflverse-data/releases/download/depth_charts/depth_charts_{season}.csv"
# The exact filename for this release could not be confirmed from docs
# (GitHub's asset list is JS-rendered); these are tried in order, first
# one that resolves wins. If both ever fail, check the "Fetch weekly
# roster" line in Data Health for the real error.
ROSTER_URL_TMPLS = [
    "https://github.com/nflverse/nflverse-data/releases/download/weekly_rosters/roster_weekly_{season}.csv",
    "https://github.com/nflverse/nflverse-data/releases/download/weekly_rosters/weekly_rosters_{season}.csv",
]
TEAM_STATS_URL_TMPL = "https://github.com/nflverse/nflverse-data/releases/download/stats_team/stats_team_week_{season}.csv"

# ---- Rating model ----
MARGIN_CAP = 21          # cap blowouts before they distort SRS ratings
RECENT_HALF_LIFE_GAMES = 5   # exponential recency weighting half-life, in games
PRIOR_REGRESSION = 0.55  # how much of a team's prior-season SRS rating carries into game 1
PRIOR_FADE_GAMES = 6     # current-season games needed before prior season is ignored
SRS_MAX_ITERS = 100
SRS_TOLERANCE = 1e-4

# ---- Home field ----
# Empirical HFA is computed from your own historical data rather than
# hardcoded (league-wide HFA has drifted down since ~2020). This is a
# floor/ceiling safety clamp on that empirical estimate, not the value itself.
HFA_MIN = 0.5
HFA_MAX = 3.0

# ---- Edge / confidence ----
# NOTE (fixed 2026-11-04): EDGE_THRESHOLD / SANITY_EDGE_FLAG are applied
# to the model's RAW deviation from the line (model_independent - line),
# NOT to the market-blended edge shown in the dashboard. The blended edge
# is only 13.5%-30% of the raw deviation, so applying these thresholds to
# it meant "strong edge" needed a 10-22 point deviation and "needs review"
# needed 27-59 points -- neither ever fired in practice. See model.py.
EDGE_THRESHOLD = 3.0     # flag games where model vs Vegas gap >= this (raw deviation)
MIN_GAMES_FOR_FULL_CONFIDENCE = 4  # below this many current-season games, confidence is down-weighted
# Confidence never floors at 0 even with zero current-season games -- last
# season's regressed rating is still real evidence. This is the floor value
# at 0 current-season games; it rises toward 1.0 as MIN_GAMES_FOR_FULL_CONFIDENCE
# is reached. See ratings.rating_confidence() for why this matters.
BASE_CONFIDENCE_WITH_PRIOR_DATA = 0.45

# Vegas is a sharp, liquid market -- it is far more likely that a huge gap
# between the model and the closing line means a bug or bad input than a
# genuine edge. Above this many points of disagreement, a pick is marked
# NEEDS REVIEW instead of STRONG EDGE, so it never gets treated as a
# confident play (in the dashboard, the email, or the survivor optimizer)
# without you first checking why the model and the market disagree this much.
SANITY_EDGE_FLAG = 8.0

# ---- Market blending (this is the real fix for implausible edges) ----
# Academic research on NFL closing lines (Gray & Gray 1997; Sauer et al.;
# Szalkowski & Nelson 2012) consistently finds the market is close to
# efficient -- independent power-rating models rarely beat it out of
# sample. Rather than let the model's own read stand on its own (which is
# what produced an unusable result like a double-digit disagreement with
# the market), the "official" spread used for picks is the market line
# nudged by the model's independent read, damped by two things: how much
# current-season evidence backs the ratings (confidence), and a hard cap
# on how much independent weight the model is ever given even at full
# confidence. This means early-season predictions (the least reliable
# part of any such model) stay close to the market by construction, and
# the model can only pull further from the market as it actually earns
# that trust via grade_and_summarize() showing it beats the baseline.
MARKET_TRUST_COMPLEMENT = 0.30   # ceiling on the model's independent weight, even at full confidence
LEAGUE_AVG_POINTS = 21.5         # league-average team scoring, used to seed the totals model

# ---- Turnover luck regression ----
# Fumble recovery rate is well-documented as close to a 50/50 coin flip
# at the team level regardless of skill; interceptions carry a bit more
# real skill (pass rush, coverage) but still real variance. This treats
# only a modest fraction of observed turnover margin as "real" and
# regresses the rest out of the rating -- capped so a small sample can't
# swing things too far.
TURNOVER_LUCK_PERSISTENCE = 0.25  # fraction of turnover margin treated as real skill, not luck
TURNOVER_LUCK_FULL_SAMPLE_GAMES = 8  # games needed before the correction applies at full confidence
POINTS_PER_TURNOVER = 4.5         # commonly-cited expected-points value of a turnover
TURNOVER_LUCK_CAP = 3.0           # max rating adjustment in either direction, points

# ---- Pool line overrides ----
# Your pool may lock a different line than the live market (e.g. the
# Tuesday opening line, while the app's "Vegas" column tracks the
# current/latest line). When set, the pool line -- not the live line --
# drives the edge/pick calculation, since that's what you're actually
# judged against; the live line still displays for reference.
POOL_LINE_ENABLED_DEFAULT = True

# ---- EPA shadow layer ----
EPA_LIVE = False         # flip only after grading shows the EPA/combo variant beats baseline
EPA_OFF_WEIGHT = 1.6
EPA_DEF_WEIGHT = 1.0
EPA_BLEND = 0.5

# ---- Injuries: review-first, never automatic ----
INJ_AUTO_APPLY = False   # hard default; only flip after walk-forward ablation says the heuristic helps
POS_VALUE = {
    # QB: the one position with real quantitative research behind it.
    # Yahoo Sports' 2025 poll of 11-12 NFL oddsmakers found the top QBs
    # (Josh Allen, Mahomes) worth ~6.5-7 points ATS relative to their own
    # backup; a UW-La Crosse academic study (2016 season) estimated Tom
    # Brady's point-spread value at 6.5. 5.0 targets a middling/average
    # starter within that range -- MAX_SUGGESTED_INJ_ADJ below still caps
    # any single suggestion at 7, matching the top of the real range.
    "QB": 5.0,
    # Every other position below has much weaker public research behind
    # it. One well-documented finding (Yahoo Sports, citing oddsmakers):
    # "outside of a handful of skill-position players like Justin
    # Jefferson, Ja'Marr Chase, and Saquon Barkley, only a starting QB
    # injury significantly moves the spread." That means these values are
    # closer to an UPPER BOUND for a true WR1/RB1-caliber player -- a
    # role player at any of these positions likely has close to zero
    # real spread impact, and this system has no way to tell the
    # difference. If you know the specific player is a genuine star,
    # consider a larger manual override than the suggestion here.
    "T": 1.0, "OT": 1.0, "G": 0.5, "OG": 0.5, "C": 0.5, "OL": 0.75,
    "WR": 1.25, "TE": 0.5, "RB": 1.0, "FB": 0.2,
    "CB": 0.75, "S": 0.4, "FS": 0.4, "SS": 0.4, "DB": 0.4,
    "DE": 0.6, "EDGE": 0.75, "OLB": 0.5, "DT": 0.4, "NT": 0.4,
    "LB": 0.4, "ILB": 0.4, "MLB": 0.4,
    "K": 0.2, "P": 0.1, "LS": 0.1,
}
# Expected fraction of the full position-value impact to apply, based on
# how often a player with that designation actually ends up missing the
# game (or playing hampered) under the CURRENT (post-2016) injury report
# rules. This corrects a real error: under the OLD rules "questionable"
# meant a 50/50 chance of playing, but ESPN's tracking after the 2016
# rule change found questionable players actually suit up 75-80% of the
# time -- meaning the expected impact should be LOW, not 50/50. "Doubtful"
# and "Out" were already reasonably close to their real-world miss rates.
STATUS_MULT = {"OUT": 1.0, "DOUBTFUL": 0.75, "QUESTIONABLE": 0.25}
MAX_SUGGESTED_INJ_ADJ = 7  # matches the top of the real QB point-spread-value range above
CRITICAL_POSITIONS = {"QB"}

# ---- Depth chart: a backup's injury matters far less than a starter's ----
# depth_team from nflverse: 1 = starter, 2 = first backup, 3+ = deeper.
# These multiply the position-value impact above -- a "questionable" QB2
# barely moves the suggestion, where a starting QB does.
DEPTH_MULTIPLIER = {1: 1.0, 2: 0.4, 3: 0.15}
DEPTH_MULTIPLIER_DEFAULT = 0.1  # depth_team 4+ or unmatched

# ---- Weather ----
# Grounded in published research (Advanced Football Analytics, The Spax,
# Sharp Football Analysis, covers.com): wind effects on passing/scoring
# become measurable around 15mph and significant past 20mph; games with
# 20+ mph winds average ~2.7 fewer combined points. Extreme temperatures
# (below 25F or above 85F) show roughly an 8% reduction in scoring, with
# a milder ~5% dip in the 25-50F range. These are applied to the TOTALS
# model only -- wind/cold's effect on which team wins (the spread) is far
# less established than its effect on how much scoring happens overall.
WIND_MODERATE_MPH = 15
WIND_HIGH_MPH = 20
WIND_MODERATE_TOTAL_ADJ = -1.3   # points off the total, 15-20mph
WIND_HIGH_TOTAL_ADJ = -2.7       # points off the total, 20+mph
TEMP_COLD_F = 25
TEMP_HOT_F = 85
TEMP_MILD_COLD_F = 50
EXTREME_TEMP_TOTAL_PCT = 0.08     # scoring reduction below TEMP_COLD_F or above TEMP_HOT_F
MILD_COLD_TOTAL_PCT = 0.05        # scoring reduction between TEMP_COLD_F and TEMP_MILD_COLD_F
# Roof values (from the schedule data) that mean weather doesn't apply
INDOOR_ROOF_VALUES = {"dome", "closed"}

# Approximate stadium coordinates (lat, lon) for outdoor weather lookups.
# City-level accuracy is plenty for wind/temperature purposes.
STADIUM_COORDS = {
    "ARI": (33.5276, -112.2626), "ATL": (33.7554, -84.4008), "BAL": (39.2780, -76.6227),
    "BUF": (42.7738, -78.7870), "CAR": (35.2258, -80.8528), "CHI": (41.8623, -87.6167),
    "CIN": (39.0955, -84.5161), "CLE": (41.5061, -81.6995), "DAL": (32.7473, -97.0945),
    "DEN": (39.7439, -105.0201), "DET": (42.3400, -83.0456), "GB": (44.5013, -88.0622),
    "HOU": (29.6847, -95.4107), "IND": (39.7601, -86.1639), "JAX": (30.3239, -81.6373),
    "KC": (39.0489, -94.4839), "LA": (33.9535, -118.3392), "LAC": (33.9535, -118.3392),
    "LV": (36.0909, -115.1833), "MIA": (25.9580, -80.2389), "MIN": (44.9735, -93.2575),
    "NE": (42.0909, -71.2643), "NO": (29.9511, -90.0812), "NYG": (40.8135, -74.0745),
    "NYJ": (40.8135, -74.0745), "PHI": (39.9008, -75.1675), "PIT": (40.4468, -80.0158),
    "SEA": (47.5952, -122.3316), "SF": (37.4032, -121.9698), "TB": (27.9759, -82.5033),
    "TEN": (36.1665, -86.7713), "WAS": (38.9078, -76.8645),
}

# ---- Survivor pool ----
# NFL win probability from a point spread, via a logistic curve fit to
# historical spread -> win% (the standard "13.5 point stdev" approximation).
SURVIVOR_SPREAD_STDEV = 13.5

# ---- Storage ----
DB_PATH = os.environ.get("PICKS_DB_PATH", "nfl_picks.db")

TEAM_MAP = {
    "LAR": "LA", "STL": "LA", "WSH": "WAS", "JAC": "JAX",
    "OAK": "LV", "SD": "LAC", "SDG": "LAC",
}


def normalize_team(team: str) -> str:
    t = str(team or "").strip().upper()
    return TEAM_MAP.get(t, t)


# ---- Spread sign conventions (fixed 2026-11-04) ----
# nflverse's spread_line is HOME-perspective: positive = home favored,
# negative = away favored. Sportsbooks print the opposite: a home
# favorite is "-3.5", an away favorite is "+3.5" for the home team.
# Everything inside this app (model, DB, grading) stores the NFLVERSE
# convention. The dashboard is the ONLY boundary that speaks sportsbook
# convention to the user, and it must convert both ways with these
# helpers -- entering a sportsbook line directly into the DB used to
# silently flip the pick.
def to_nflverse_spread(sportsbook_spread: float) -> float:
    """Sportsbook line (home team: -3.5 = home favored) -> nflverse line."""
    return -float(sportsbook_spread)


def to_sportsbook_spread(nflverse_spread: float) -> float:
    """nflverse line (positive = home favored) -> sportsbook line."""
    return -float(nflverse_spread)
