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
# CHANGED 2026-11-04 (backtest items 1/6): was 6. Point-in-time backtest
# 2019-2025 (n=1,871, paired test) found fade-12 beats fade-6 by +2.19 pp
# ATS (49.84% vs 47.65%, paired z=2.77) and MAE -0.31 pts; fade-10 was
# +1.42 pp. Still below 50% and below ~52.4% breakeven, and 3 variants were
# tried, so this is a model-quality improvement, not a betting edge.
PRIOR_FADE_GAMES = 12    # current-season games needed before prior season is ignored
SRS_MAX_ITERS = 100
SRS_TOLERANCE = 1e-4
# Damped SRS (see ratings.py) needs more iterations than undamped to
# reach the same tolerance, so ratings.py runs 3*SRS_MAX_ITERS when
# damping is on; the warning message reports that total, not this value.
SRS_DAMPING = 0.5        # damped Jacobi for SRS: new = damping*old + (1-damping)*computed.
                          # The undamped solve does NOT converge in weeks 2-3 (disconnected
                          # game graph, final delta ~21 pts); damping cuts that to
                          # ~0.006 pts in the worst harness weeks (immaterial to picks),
                          # and matches the undamped fixed point to <=0.005 pts from week 4 on.
DIV_MULTIPLIER = 0.9     # Kept at the original 0.9 (was hardcoded in model.py).
                          # Situational backtest found divisional margin~spread slopes
                          # identical to non-divisional (1.051 vs 1.048), which argued
                          # for removing it, but the harness ablation the other way
                          # (div09 49.18% vs div1 48.74%, paired z=1.13, 50 flips)
                          # is noise in the opposite direction. Neither side is
                          # proven, so the original value stays -- do not claim
                          # a win for either setting.
REST_POINTS_PER_DAY = 0.25  # rest adjustment per day of rest differential (see model.py)
REST_CAP = 2.0           # max rest adjustment in either direction, points

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
EPA_LIVE = False         # STAYS False 2026-11-04: app-scale combo (48.9% ATS, worse MAE)
                          # does NOT beat baseline. The CALIBRATED combo below does in the
                          # ablation study (see epa.py), but its scale was fit on the same
                          # sample and the loader changes (prior blending, week filtering)
                          # are new -- flip only after a walk-forward re-run confirms it.
EPA_OFF_WEIGHT = 1.6
EPA_DEF_WEIGHT = 1.0
EPA_BLEND = 0.5
# Calibrated EPA scale (backtest item 3): the app formula (1.6*off - 1.0*def)
# treated as points overstates EPA by ~2.6x. Point-in-time calibration
# 2019-2025 gives ~0.38 points per EPA unit (0.27 in 2019, 0.38-0.40 from
# 2021 on). epa.py multiplies its rating by this factor; the resulting
# calibrated combo (50% weight) was 49.9% ATS / MAE 10.35 vs SRS 48.0% /
# 10.66 in the ablation, with MAE gains in all 7 seasons -- promising,
# not proven, hence EPA_LIVE stays False above.
EPA_POINTS_PER_UNIT = 0.38
# ---- Cover-probability calibration (backtest item 2) ----
# Actual margin ~ model margin regresses with slope ~0.68, intercept ~0.36
# on 2019-2025 point-in-time data (residual SD 13.43): the raw model is
# over-dispersed. calibration.py shrinks the model margin by these
# coefficients before turning it into a cover probability. This does NOT
# change the pick: a shrunk-margin pick test was fit in-sample only
# (49.4% vs 47.7%, paired z=1.69, 368 flips) and is kept behind
# CALIBRATED_PICK=False until a walk-forward test confirms it.
COVER_SHRINK_SLOPE = 0.68
COVER_SHRINK_INTERCEPT = 0.36
CALIBRATED_PICK = False  # if True, model.py picks by shrunk margin vs line; OFF by default
# ---- QB change review signal (backtest item 5) ----
# Situational proxy 2019-2025: a QB change / non-season-mode QB carries a
# ~1-1.7 pt penalty the spread does not fully price. This averages ALL
# changes (benchings, rest, backup-to-backup), NOT just starter injuries,
# so it is review-only, never auto-applied, and well below the 5.0 starter
# injury value in POS_VALUE. See injuries.qb_change_suggestion().
QB_CHANGE_REVIEW_PTS = 1.5
# ---- Travel (backtest item 6) ----
# Long-travel away teams beat the spread in 2019-2025 (-2.17 pts residual
# at >=2,000 mi, slope -0.95 pts/1,000 mi) but the effect is driven by
# 2019-2020 and inconsistent by season. Kept OFF by default; the
# function exists in model.py for harness testing only.
TRAVEL_ENABLED = False
TRAVEL_POINTS_PER_1000MI = 0.0

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

# ---- Batch 2 (2026-11-04, items 4-6): ensemble / features / market ----
# All three stay OFF / shadow by default. Nothing in model.py, weekly_run.py
# or dashboard.py reads these live yet: ensemble.py, features.py and
# market.py are harness/shadow modules, graded in backtest.py. Flip
# ENSEMBLE_LIVE only on a walk-forward win (see README batch-2 section);
# there is no FEATURES_LIVE flag because no feature won out-of-sample,
# and market cross-checks never switch the pool line by design.
ENSEMBLE_LIVE = False
ENSEMBLE_GRID_STEP = 0.25  # simplex grid step for walk-forward weight learning (0.25 -> 35 combos for 4 components)
ENSEMBLE_MIN_TRAIN_GAMES = 200  # games needed before learned weights replace equal weights
# Elo (new in batch 2; no Elo existed before). Sequential, win/loss only
# (no margin multiplier -- blowouts are already in SRS), updated once per
# week from pre-week ratings so no within-week result leaks into a pick.
ELO_START = 1500.0
ELO_K = 20.0
ELO_PRIOR_REGRESSION = 0.5  # fraction of (rating - 1500) carried into the next season
ELO_HFA_POINTS = 55.0  # home-field edge in Elo points used in the expected-score formula (approx: ~2 pts of margin)
# SOS-iterative EPA (batch 2, item 4). Same damped-Jacobi idea as
# ratings.SRS_DAMPING, applied to offensive/defensive EPA instead of
# points. The naive weighted_epa_ratings() in epa.py is kept untouched
# for comparison; sos_epa_ratings() is the new one.
EPA_SOS_DAMPING = 0.5
EPA_SOS_MAX_ITERS = 200
EPA_SOS_TOLERANCE = 1e-4
# Play-by-play proxies (batch 2, item 5). True early-down success and
# red-zone efficiency are NOT in the team-stats file (no down/distance
# or red-zone columns) and the nflverse play-by-play release filename
# could not be confirmed (guessed URLs 404), so they are NOT built here
# -- see features.py. What IS built from week-filtered team stats:
# explosive-play rate, pressure allowed/generated, pass share,
# first-down rate (a drive-sustainment proxy, NOT early-down success).
# Feature coefficients are fit by clipped least squares on the training
# window only (features.fit_feature_coeff), never hand-set per game.
FEATURE_COEFF_CAPS = {
    # max |points| a feature differential may move a margin, via its cap
    # on the fitted coefficient (units differ per feature, see features.py).
    "explosive": 300.0, "pressure_allowed": 300.0, "pressure_generated": 300.0,
    "pass_share": 50.0, "first_down": 300.0,
    "wind_pass": 30.0, "ol_rush": 2000.0,
}
# Market cross-checks (batch 2, item 6). Thresholds for FLAGs only --
# a flag never changes a pick or a line.
MARKET_MARGIN_SD = 13.5  # normal SD (pts) converting win prob <-> margin; same as SURVIVOR_SPREAD_STDEV
MARKET_ML_SPREAD_FLAG = 3.0  # |moneyline-implied margin - spread| at/above this flags spread/ML disagreement
MARKET_MODEL_TOTAL_FLAG = 5.0  # |model total - total line| at/above this flags a totals disagreement (diagnostic only)

# ---- Batch 3 (2026-11-04, items 1-2): standings risk / season simulation ----
# Both modules are LABEL-ONLY: they suggest gambles and estimate their
# cost in season odds, they never change a pick. standings.py reads
# these thresholds at call time (not import time) so a test or caller
# can patch config and re-run without reloading the module.
# Thresholds, and why these values:
#   LATE_WEEKS_LEFT = 4: the rule is silent for the first ~14 weeks.
#     With 5+ weeks (80+ games) left, default variance alone usually
#     closes a small gap, and gambling early just burns expected wins.
#   TRAIL_GAMES = 2: trailing by 1 with 4 weeks left is still
#     recoverable by default variance over ~64 games, so it does not
#     trigger; trailing by 2+ does. LAST_WEEK_TRAIL_GAMES = 1 is the
#     exception: with one week left, mirroring the leader loses
#     outright, so a 1-game deficit can ONLY be closed (to a tie or,
#     with several flips, a win) by differentiation.
#   LEAD_GAMES = 1: any late lead is "protect" mode -- keep defaults,
#     never flip a strong lean to chase a weekly prize. Protect mode
#     and neutral mode both yield the default slate; they differ only
#     in the label (and in what they warn against).
#   COIN_FLIP_MAX_PROB = 0.55: a game is a coin flip when even the
#     BEST side's calibrated cover prob is <= 0.55 (39% of 2019-2025
#     harness games, ~6.8 per late week). Flipping all of them would
#     be ~7 gambles a week, so MAX_GAMBLES_PER_WEEK = 3 caps it and
#     cheapest-first ordering (lowest expected-win cost) picks which.
#   STRONG_LEAN_MIN_PROB = 0.65: a calibrated pick prob >= 0.65
#     (20% of harness games) is a strong lean -- never a gamble
#     candidate, and labelled PROTECT when leading late.
STANDINGS_LATE_WEEKS_LEFT = 4
STANDINGS_TRAIL_GAMES = 2
STANDINGS_LAST_WEEK_TRAIL_GAMES = 1
STANDINGS_LEAD_GAMES = 1
STANDINGS_COIN_FLIP_MAX_PROB = 0.55
STANDINGS_STRONG_LEAN_MIN_PROB = 0.65
STANDINGS_MAX_GAMBLES_PER_WEEK = 3
# Season simulation (simulation.py). Calibrated cover probs are
# treated as P(win | decided); SIM_PUSH_PROB is the push mass applied
# on top (43 pushes in 1,871 harness games = 2.3%). Trials/seed are
# the reproducibility contract: same seed + same games = same output.
SIM_TRIALS = 10000
SIM_SEED = 20261104
SIM_PUSH_PROB = 0.023
# Cheap vs expensive weekly gambles (simulation.classify_gamble):
# CHEAP = expected cost <= 0.10 wins AND season odds do not fall;
# EXPENSIVE = expected cost >= 0.25 wins OR season odds fall by
# >= 1.0 pp; anything between is NEUTRAL. The cost axis is expected
# season wins, the odds axis is P(finishing first) in percentage points.
SIM_CHEAP_COST_MAX_WINS = 0.10
SIM_EXPENSIVE_COST_MIN_WINS = 0.25
SIM_EXPENSIVE_P_FIRST_DROP_PP = 1.0

# ---- Batch 3 (2026-11-04, items 1-2, second batch): pool-line staleness / Elo variants ----
# STALENESS_LIVE stays False: no staleness score is wired into any pick.
# The 2019-2025 backtest cannot run the true pool-vs-closing test at all
# (games.csv carries ONE undated near-close line per game -- no pool
# line, no opener, no line timestamps; no free multi-season open/close
# archive was found, only paid/academic ones), and the closest honest
# proxy (moneyline-implied margin as a second market reading) shows no
# edge (see README batch-3 section). staleness.py is therefore a
# grading/descriptive framework for future live snapshot pairs
# (db.line_snapshots pool + closing), NOT a pick input. The existing
# line-move FLAG (db.get_line_moves, review_queue) is untouched and
# remains the only live staleness signal; staleness.py deliberately
# does not reimplement flagging. The pool line is never auto-switched.
STALENESS_LIVE = False
STALENESS_BUCKETS = (1.0, 2.0, 3.0)  # bucket edges in points: <1, 1-2, 2-3, 3+
STALENESS_MIN_MOVE = 1.0  # mirrors REVIEW_LINE_MOVE_THRESHOLD; a smaller move is noise for grading
STALENESS_BOOTSTRAP_TRIALS = 2000
STALENESS_BOOTSTRAP_SEED = 20261104
# Elo variants (batch 3, item 2). One-at-a-time grid only -- a full
# factorial over K x rollover x HFA x margin would be ~100 variants and
# would guarantee a spurious "winner". The 11 non-baseline variants in
# elo_variants.DEFAULT_VARIANTS are the whole grid; any variant promoted
# would have to (a) win on the 2019-2022 training split, (b) also win
# on the 2023-2025 test split, and (c) survive Bonferroni correction
# across all 11 -- none did (see README). ELO_* live values are unchanged.
ELO_VARIANT_KS = (10.0, 32.0, 40.0)
ELO_VARIANT_ROLLOVERS = (0.25, 0.75, 1.0)
ELO_VARIANT_HFA_POINTS = (0.0, 110.0)
ELO_VARIANT_MARGIN_MODES = ("log", "capped")
ELO_VARIANT_RECENCY_EARLY_MULT = 0.75  # weeks < 10: K scaled by this
ELO_VARIANT_RECENCY_LATE_MULT = 1.25   # weeks >= 10: K scaled by this
ELO_BOOTSTRAP_TRIALS = 2000
ELO_BOOTSTRAP_SEED = 20261104
ELO_TRAIN_SEASONS = (2019, 2020, 2021, 2022)
ELO_TEST_SEASONS = (2023, 2024, 2025)

# ---- Totals model: SRS-style scoring splits (2026-11-04, item 8) ----
# TOTALS_SRS_LIVE stays False: the SRS totals model (scoring.py) was
# built and graded point-in-time 2019-2025, and it does NOT beat the old
# simplified scoring-average blend out-of-sample (SRS 11.04 MAE vs old
# 10.70 MAE, worse in 6 of 7 seasons, tied in 2025; market line 10.36).
# The old blend therefore stays live in model.py. Flip this only on a
# walk-forward win, not on an in-sample fit -- see README item 8.
# When True, model.py uses scoring.predict_total_srs with the ratings
# weekly_run.py passes through; when False (default) it uses the old
# scoring.predict_total, even if SRS ratings were computed. The SRS
# solve itself reuses the spread model's fade philosophy
# (PRIOR_FADE_GAMES, PRIOR_REGRESSION) rather than inventing new
# blending constants; only the solver's own constants live here.
TOTALS_SRS_LIVE = False
TOTALS_SRS_DAMPING = 0.5  # damped Jacobi, same idea as SRS_DAMPING in ratings.py
TOTALS_SRS_MAX_ITERS = 200
TOTALS_SRS_TOLERANCE = 1e-4

# ---- Submission sheet + Thursday review queue + reliability (2026-11-04, items 3 and 7) ----
# REVIEW_LINE_MOVE_THRESHOLD: pool-vs-closing movement (points) that
# puts a game on the Thursday review queue. Same 1 pt value the
# dashboard's line-moves section used before; it now lives here so
# db.get_line_moves and review_queue.py share one threshold instead
# of each hardcoding 1.0. The pool line is still never auto-switched.
REVIEW_LINE_MOVE_THRESHOLD = 1.0

# ---- Batch 3 (2026-11-04, items 3, 9, 10): error decomposition / pool results / freshness ----
# Item 3: error_decomposition.py slices point-in-time backtest rows (or
# graded live picks) and reports ATS% + MAE per slice, always with its
# sample size. ERROR_MIN_DECIDED is the "do not act on this" floor: a
# slice with fewer decided games is flagged small_sample, and the
# worst-slice ranking only considers slices at/above it -- a 2-0 slice
# is not a finding, it is noise. No slice changes a pick.
ERROR_MIN_DECIDED = 30
# Spread-bucket edges on |line| (nflverse, points): pick'em, short,
# medium, large. Total-bucket edges on total_line (points).
ERROR_SPREAD_EDGES = (0.5, 3.5, 7.5)
ERROR_TOTAL_EDGES = (40.0, 45.0, 50.0)
# Item 9: pool_results.py grades the picks actually submitted to
# Barbara's pool against the model picks. Grading only -- it never
# changes a pick, a line, or a model grade, and it invents no results:
# with no imported rows it reports an empty state, not a 0-0 record.
# Item 10: freshness.py warns when the dashboard would otherwise
# present an old slate as current. The workflow runs Tue/Thu 15:00 UTC
# (weekly.yml cron "0 15 * * 2,4"); a run is STALE when its picks are
# a week old, WARN when older than 4 days or when the last scheduled
# run was missed, and OK otherwise. MISSED means: now is past a
# scheduled run time (plus a grace window for the run itself) but the
# latest saved run predates that scheduled run. The app cannot read
# live GitHub Actions status without a token it does not have, so the
# live source remains the Actions tab -- freshness.py says so, and
# never invents a live status.
FRESHNESS_STALE_HOURS = 168.0  # 7 days: a week-old slate is never presented as today's
FRESHNESS_WARN_HOURS = 96.0  # 4 days: older than any normal Tue->Thu->Tue gap
FRESHNESS_RUN_GRACE_HOURS = 3.0  # a scheduled run gets this long to land before it counts as missed
FRESHNESS_SCHEDULE_WEEKDAYS = (1, 3)  # Tue, Thu (Python weekday(): Mon=0)
FRESHNESS_SCHEDULE_HOUR_UTC = 15

# ---- Batch 3 (2026-11-04, items 4-5): pool-line slates / manual-adjustment file ----
# Item 4: past pool slates for the backtest harness. A CSV in the
# format documented in pool_lines.py (sportsbook convention by
# default, converted to nflverse on parse). Empty = no slate
# supplied, and pool_lines.py reports that empty state plainly --
# it never invents lines. Set via env so a local harness run can
# point at a slate without editing code.
POOL_LINES_CSV_PATH = os.environ.get("POOL_LINES_CSV", "")
# Item 5: the versioned repo file that is the manual-adjustments
# source of truth (manual_adjustments.py). Empty = the default
# repo file nfl-picks-app/manual_adjustments.json; the env var is
# for tests and for runs pointed at a different checkout.
MANUAL_ADJUSTMENTS_FILE = os.environ.get("PICKS_ADJUSTMENTS_FILE", "")
MANUAL_ADJUSTMENTS_SCHEMA_VERSION = 1

# ---- Batch 3 (2026-11-04, items 6-7): Thursday final-run diff / rating uncertainty ----
# Item 7: uncertainty.py bands. UNCERTAINTY_PICK_USE stays False and
# no code path changes a pick from a band: labels/bands only until a
# walk-forward test justifies a pick-logic use. PRIOR_SD is a judgment
# parameter (see uncertainty.py); GAME_MARGIN_SD is the 2019-2025
# point-in-time backtest residual SD from calibration.py.
UNCERTAINTY_PRIOR_SD = 5.0
UNCERTAINTY_GAME_MARGIN_SD = 13.43
UNCERTAINTY_Z90 = 1.645  # 90% band multiplier for rating_interval()
UNCERTAINTY_PICK_USE = False
# ---- Batch 4 (2026-11-04, group C item 7): rating uncertainty round 2 ----
# UNCERTAINTY_PICK_USE stays False: the walk-forward flip test in the
# README batch-4 group C section did NOT win, so no pick logic reads
# the new covariance/HFA-aware bands either. The new functions in
# uncertainty.py are labels/display only, like the batch-3 bands.
# UNCERTAINTY_HFA_SD: the league HFA is not a known constant -- the
# season mean home margin in games.csv 2019-2025 ranges -0.14 to
# +2.68, SD of the seven yearly means 1.07 pts (pooled SE of the
# overall mean is only 0.33, which would understate the drift by
# assuming a fixed HFA). 1.0 is that drift rounded, a judgment
# value, not a fitted pick parameter: it enters margin variance
# as +HFA_SD^2, i.e. ~+0.07 pts on a ~4.5 pt margin SD at mid-season.
# Covariance needs no new constant: it comes from the game graph
# itself (uncertainty.rating_covariance_matrix), using the same
# prior/game SDs above, so there is nothing to fit to picks.
UNCERTAINTY_HFA_SD = 1.0
# Item 6: run_diff.py reporting thresholds (points). A pick flip is
# always reported regardless of edge size; everything else must move
# at least this far to earn a row, so rounding wobbles do not flood
# the Thursday review.
RUN_DIFF_MIN_EDGE_MOVE = 0.5
RUN_DIFF_MIN_RATING_MOVE = 0.5
RUN_DIFF_MIN_LINE_MOVE = 0.5
RUN_DIFF_MIN_TOTAL_MOVE = 0.5

# ---- Batch 4 (2026-11-04, group B items 3-5): staleness auto-report / team HFA / dynamic blend ----
# Item 3: STALENESS_LIVE stays False. The auto-report (staleness.py)
# fires -- i.e. produces a follow/fade grading -- only once
# STALENESS_MIN_COMPLETED_PAIRS completed pool+closing snapshot pairs
# with results have accumulated in db.line_snapshots. 100 is a
# judgment threshold, not a fitted value: below ~100 pairs a
# follow/fade split is dominated by noise (a 50/50 split on 100
# decided games still has a ~10 pp wide 95% CI), and 100 is roughly
# six full weeks of pool games, so it cannot be reached by one lucky
# week. Until then the report states not_enough_data explicitly and
# grades nothing. Note the snapshot caveat: weekly_run.py saves the
# 'closing' snapshot from the live line at run time, NOT a verified
# kickoff close, so pairs are pool-vs-last-live-line until a true
# close is ever recorded under that source name.
STALENESS_MIN_COMPLETED_PAIRS = 100
# Item 4: TEAM_HFA_LIVE stays False -- no team-specific HFA is wired
# into model.py, weekly_run.py, or dashboard.py. The harness variant
# team_hfa (ratings.team_home_field) is the only consumer. Shrinkage:
# a team's shrunk HFA = league + (raw_team - league) * n/(n+K), where
# n is that team's pre-week home games in the played history and K is
# below. K=25 means a team with a full season-plus of home games
# (~25) gets weight 0.5 on its own estimate -- the prior (league mean)
# and the team's own data count equally at that sample size, which is
# the honest weighting when a single team's home residual has SE
# ~13.4/sqrt(n) points (batch-1 item 6: team ATS-residual SD 1.44 vs
# mean SE 1.73 -- mostly noise). Shrunk values are clamped to the
# same HFA_MIN/HFA_MAX range as the league number, and neutral-site
# games still get 0.
TEAM_HFA_LIVE = False
TEAM_HFA_SHRINKAGE_GAMES = 25
# Item 5: DYNAMIC_BLEND_LIVE stays False -- no dynamic blend is wired
# into model.py. The finding that forces this: in this app the pick
# follows the SIGN of (model - line), and any strictly positive blend
# weight preserves that sign, so rescaling the blend by week or by
# deviation bucket cannot change a single pick (except a margin
# landing exactly on the line, i.e. a tie artifact). The only
# pick-changing dynamic rule is a per-bucket follow/fade flip, which
# dynamic_blend.py tests walk-forward and which did NOT win (see
# README batch-4 group B). Thresholds below are the test's
# definitions, not live parameters: LARGE_DEVIATION is the "model
# disagrees a lot" bucket edge, MIN_TRAIN_DECIDED is the floor below
# which a bucket's training record defaults to follow (do not flip
# a bucket on a handful of games).
DYNAMIC_BLEND_LIVE = False
DYNAMIC_BLEND_LARGE_DEVIATION = 5.0
DYNAMIC_BLEND_MIN_TRAIN_DECIDED = 30

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


# ---- Batch 3 (2026-11-04, item 8): true play-by-play features ----
# PBP_FEATURES_LIVE stays False: the features in pbp_features.py
# (early-down success, red-zone efficiency) are graded
# out-of-sample 2019-2025 in the harness and do NOT beat the
# baseline there (see README), so nothing in model.py,
# weekly_run.py, or dashboard.py reads them. The URL template
# below is CONFIRMED, not guessed: the GitHub REST API listing
# for the nflverse-data `pbp` release (id 58152862, updated
# 2026-11-04) lists play_by_play_{season}.csv/.csv.gz/.parquet/
# .qs/.rds per season, and the 2024 csv.gz fetched during this
# build returned HTTP 200 at exactly the listed 19,362,351 bytes.
# Batch 2's guesses 404'd because they used the wrong release
# tag; the tag is `pbp`, the filename stem is `play_by_play_`.
# csv.gz is the default because it is the smallest
# pandas-readable form listed for every season.
PBP_FEATURES_LIVE = False
PBP_URL_TMPL = "https://github.com/nflverse/nflverse-data/releases/download/pbp/play_by_play_{season}.csv.gz"
# Coefficient caps (points per unit rate differential, 0-1 scale)
# for pbp_features.fit_pbp_coeff -- same clipping role as
# FEATURE_COEFF_CAPS for the team-stats proxies. A cap of 100
# cannot bind on any plausible fit (a 0.05 rate differential
# moving a margin by 5 pts is already a coefficient of 100),
# but it stops a tiny-denominator fit from producing an absurd
# points-per-unit value.
PBP_COEFF_CAPS = {"early_down": 100.0, "red_zone": 100.0}
# Early-down success thresholds (Football Outsiders 40/60 rule;
# see pbp_features.py -- a convention, not an nflverse column).
PBP_EARLY_DOWN_THRESHOLDS = {1: 0.4, 2: 0.6}
# Red zone = plays starting at yardline_100 <= 20.
PBP_RED_ZONE_YARDLINE = 20.0

# ---- Batch 4 (2026-11-04, group D items 8-10) ----
# Item 8: committed-DB source (committed_db.py). ALL optional and
# all env-driven; there is deliberately NO default repo name --
# the audit checkout has no git remote to derive one from, and
# hardcoding one from memory would be inventing configuration.
# Until one source is set, the dashboard reads its local DB
# only and says so (mode "local"). A token is optional, sent
# only as an Authorization header, never in a URL or a log.
COMMITTED_DB_URL = os.environ.get("PICKS_COMMITTED_DB_URL", "")
COMMITTED_DB_FILE = os.environ.get("PICKS_COMMITTED_DB_FILE", "")
COMMITTED_DB_REPO = os.environ.get("PICKS_GITHUB_REPO", "")
COMMITTED_DB_BRANCH = os.environ.get("PICKS_GITHUB_BRANCH", "main")
COMMITTED_DB_PATH_IN_REPO = "nfl-picks-app/nfl_picks.db"
COMMITTED_DB_TIMEOUT_SECONDS = 30
COMMITTED_DB_MAX_BYTES = 64 * 1024 * 1024
# Item 9: standings CSV path (pool_standings.py), mirroring
# POOL_LINES_CSV_PATH. Empty = no standings file supplied, and
# pool_standings reports that empty state, never invented wins.
POOL_STANDINGS_CSV_PATH = os.environ.get("POOL_STANDINGS_CSV", "")
# Item 10: experiment tracking (experiment.py). The version
# label is cosmetic -- the fingerprint of flags+params is the
# attribution key. Empty label env = "nfl-picks-app (unlabelled
# build)" in the record, never a guessed release number.
EXPERIMENT_MODEL_VERSION = os.environ.get("PICKS_MODEL_VERSION", "")

# ---- Batch 4 (2026-11-04, group C item 6): play-by-play round 2 ----
# Round 2 shares PBP_FEATURES_LIVE (False, above): nothing in
# model.py, weekly_run.py, or dashboard.py reads pbp_round2.py
# either, and no separate live flag exists to flip. Coefficient
# caps (points per unit differential, same clipping role as
# PBP_COEFF_CAPS): rate features (0-1 scale) cap 100, as in
# round 1. ST EPA is a mean EPA per play, not a rate: a team
# averages ~13 special-teams plays per game, so a 1-pt-per-EPA
# translation implies a natural coefficient near 13; cap 50
# allows ~4x that and only stops a tiny-denominator fit from
# producing an absurd value. The "early_down"/"red_zone"
# combined keys here are the round-1 combined differentials,
# reused only for the round-2 "all" variant.
PBP2_COEFF_CAPS = {"third_down": 100.0, "third_down_off": 100.0,
                   "third_down_def": 100.0, "st_epa": 50.0,
                   "early_down": 100.0, "early_down_off": 100.0,
                   "early_down_def": 100.0, "red_zone": 100.0,
                   "red_zone_off": 100.0, "red_zone_def": 100.0}

