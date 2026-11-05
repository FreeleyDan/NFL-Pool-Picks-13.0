"""
Play-by-play round 2 (batch 4, group C, item 6): third-down
efficiency, special-teams EPA, and defensive-only versions of
the round-1 early-down / red-zone features. Shadow-only.

SOURCE -- same confirmed source as round 1 (pbp_features.py):
nflverse-data `pbp` release, play_by_play_{season}.csv.gz
(config.PBP_URL_TMPL). Files for 2017-2025 were downloaded to
scratch (/tmp, NOT committed) for the full run in the README
batch-4 group C section; the module itself never invents a
play-by-play number -- with no frame for a season, that
season's games get None differentials and grade as the SRS
margin, exactly like round 1.

DEFINITIONS -- stated so they can be checked:
* Third-down conversion (offense): 3rd-down pass/run plays
  (no_play, kicks, field goals, kneels excluded, as in round 1).
  A play converts if first_down == 1 or touchdown == 1, or
  yards_gained >= ydstogo (ydstogo <= 0 falls back to the
  flags alone, so a 3rd-and-0 quirk cannot auto-convert).
  Rate = conversions / 3rd-down scrimmage plays. The file's
  third_down_converted column is NOT used: on 2024 it agrees
  with the flag rule to within ~0.3 pp, but the rule above is
  the definition, not a column that could silently change.
* Third-down allowed (defense): the same rate with this team
  as defteam.
* Special-teams EPA (offense): mean `epa` per special-teams
  play with this team as posteam, where special-teams play =
  nflfastR `special` == 1 (kickoff, punt, field goal, extra
  point -- 7,216 plays in 2024 REG; the narrower
  `special_teams_play` flag omits field goals and is used
  only as a fallback when `special` is absent). EPA is in
  posteam perspective, so a punt's EPA is usually negative
  (giving the ball away) -- the FEATURE is the relative
  mean, not the absolute level: a team whose punts lose
  less EPA than average still scores above average.
* Special-teams EPA allowed (defense): mean `epa` per
  special-teams play with this team as defteam, i.e. the
  opponent's ST EPA on plays against this team. Strength =
  off_mean - allowed_mean, parallel to round 1.
* Defensive-only early-down / red-zone: round 1 combined a
  team's offense and defense into one strength (off -
  allowed) and therefore could not say which side drove any
  result. Here they are split: off_diff = home_off -
  away_off; def_diff = away_allowed - home_allowed (a lower
  allowed rate is better, so the sign flips). The combined
  round-1 differentials are recomputed here only for the
  "all" variant (third + ST + early-combined + red-zone-
  combined); the split features are the new evidence.

PLACEBO GATE -- the promotion bar for every feature, not
just the baseline (see backtest.placebo_rows and the batch-4
group A section): a feature PASSES only if its paired delta
vs the corrected baseline EXCEEDS the placebo p95 paired
delta (+0.11 pp on 200 fresh seeds, re-verified for this
batch) AND its absolute ATS% EXCEEDS the placebo p95
absolute ATS (50.22%). A feature that beats the baseline
by less than a random +/-0.15 pt nudge beats on average is
dropped, however good its headline looks -- that is the
whole point of the gate, after round 1's red-zone headline
(z=4.02) turned out to be a tie-grading artifact. Features
are also Bonferroni-corrected across the 9 tested variants
(8 singles + all) in the README report; the module returns
the raw paired z and leaves the correction to the report,
so the module never hides a multiple-comparison penalty.

WALK-FORWARD DISCIPLINE -- identical to round 1: for season
S, week W, rates use season-S weeks before W plus the full
season S-1; coefficients for season S are fit ONLY on
scored + burn-in rows from seasons before S (burn-in
2017-2018, scored nowhere). In-sample numbers are reported
separately (coeffs fit on ALL scored rows, graded on the
same rows) and are NEVER the promotion test.

LIVE STATUS: PBP_FEATURES_LIVE stays False (config.py).
Nothing in model.py, weekly_run.py, or dashboard.py reads
this module; it is a harness/shadow module graded by
evaluate_pbp2_features() below.
"""
from __future__ import annotations

import pandas as pd

from . import backtest as backtest_mod
from . import config
from . import pbp_features as pbp1

PBP2_COLUMNS = ("game_id", "season", "season_type", "week", "posteam",
                "defteam", "down", "ydstogo", "play_type", "yards_gained",
                "first_down", "touchdown", "epa", "special",
                "special_teams_play")
THIRD_DOWN_TYPES = ("pass", "run")
# Single features, in report order. "all" is tested separately
# (it is not a fitted feature -- it sums the four combined
# differentials with their own season coefficients).
FEATURES2 = ("third_down", "third_down_off", "third_down_def", "st_epa",
             "early_down_off", "early_down_def", "red_zone_off",
             "red_zone_def")
COMBINED_FOR_ALL = ("third_down", "st_epa", "early_down", "red_zone")


def pbp2_url(season: int) -> str:
    """Same confirmed URL as round 1 (config.PBP_URL_TMPL)."""
    return config.PBP_URL_TMPL.format(season=int(season))


def load_pbp2_frame(season: int, path: str | None = None) -> pd.DataFrame:
    """Load one season's play-by-play, restricted to
    PBP2_COLUMNS actually present. With `path`, reads a local
    file (tests, offline reruns); otherwise downloads from
    pbp2_url(season). No week filter here -- callers aggregate
    weekly counts and filter there, so a full-season load can
    never silently become a leak."""
    if path is not None:
        header = list(pd.read_csv(path, nrows=0, low_memory=False).columns)
        usecols = [c for c in PBP2_COLUMNS if c in header]
        return pd.read_csv(path, usecols=usecols, low_memory=False)
    import requests
    resp = requests.get(pbp2_url(season), timeout=180)
    resp.raise_for_status()
    from io import BytesIO
    header = list(pd.read_csv(BytesIO(resp.content), nrows=0, low_memory=False).columns)
    usecols = [c for c in PBP2_COLUMNS if c in header]
    return pd.read_csv(BytesIO(resp.content), usecols=usecols,
                       low_memory=False)


def _third_converted(row) -> bool:
    """Conversion for one 3rd-down scrimmage play (see module
    docstring). Flags override the yardage test."""
    try:
        if float(row["first_down"]) == 1.0 or float(row["touchdown"]) == 1.0:
            return True
        ytg = float(row["ydstogo"])
        gained = float(row["yards_gained"])
    except (KeyError, TypeError, ValueError):
        return False
    if ytg <= 0:
        return False
    return gained >= ytg


def weekly_team_counts2(pbp: pd.DataFrame | None) -> pd.DataFrame:
    """Per (season, week, team) round-2 counts. Columns:
    third_num/third_den (offensive 3rd-down conversions/plays),
    third_allow_num/third_allow_den (same, allowed as defteam),
    st_epa_sum/st_epa_n (offensive ST EPA sum/plays as posteam),
    st_allow_epa_sum/st_allow_epa_n (same, allowed as defteam).
    Regular season only; week must be present or the frame
    yields nothing, since a week-less frame cannot be filtered
    point-in-time. A frame without an EPA column yields ST
    counts of zero (no ST feature), never an invented EPA."""
    cols = ("season", "week", "team", "third_num", "third_den",
            "third_allow_num", "third_allow_den",
            "st_epa_sum", "st_epa_n", "st_allow_epa_sum", "st_allow_epa_n")
    if pbp is None or pbp.empty or "week" not in pbp.columns \
            or "posteam" not in pbp.columns:
        return pd.DataFrame(columns=cols)
    df = pbp.copy()
    if "season_type" in df.columns:
        df = df[df["season_type"].astype(str).str.upper() == "REG"]
    df["week"] = pd.to_numeric(df["week"], errors="coerce")
    df = df[df["week"].notna()]
    if df.empty:
        return pd.DataFrame(columns=cols)
    df["posteam"] = df["posteam"].map(config.normalize_team)
    if "defteam" in df.columns:
        df["defteam"] = df["defteam"].map(config.normalize_team)
    rows: list[dict] = []

    def _blank(team, season, week):
        return {"season": season, "week": week, "team": team,
                "third_num": 0.0, "third_den": 0.0,
                "third_allow_num": 0.0, "third_allow_den": 0.0,
                "st_epa_sum": 0.0, "st_epa_n": 0.0,
                "st_allow_epa_sum": 0.0, "st_allow_epa_n": 0.0}

    # ---- Third-down conversion (offense + allowed) ----
    third = df[df["play_type"].isin(THIRD_DOWN_TYPES)
               & (pd.to_numeric(df["down"], errors="coerce") == 3)].copy()
    if not third.empty:
        third["is_conv"] = third.apply(_third_converted, axis=1)
        off = third.groupby(["season", "week", "posteam"])["is_conv"].agg(
            ["sum", "count"]).reset_index()
        for _, r in off.iterrows():
            d = _blank(r["posteam"], r["season"], r["week"])
            d["third_num"], d["third_den"] = float(r["sum"]), float(r["count"])
            rows.append(d)
        if "defteam" in third.columns:
            allowed = third.dropna(subset=["defteam"]).groupby(
                ["season", "week", "defteam"])["is_conv"].agg(
                ["sum", "count"]).reset_index()
            for _, r in allowed.iterrows():
                d = _blank(r["defteam"], r["season"], r["week"])
                d["third_allow_num"], d["third_allow_den"] = float(r["sum"]), float(r["count"])
                rows.append(d)
    # ---- Special-teams EPA (offense + allowed) ----
    st_flag = None
    if "special" in df.columns:
        st_flag = pd.to_numeric(df["special"], errors="coerce") == 1
    elif "special_teams_play" in df.columns:
        st_flag = pd.to_numeric(df["special_teams_play"], errors="coerce") == 1
    if st_flag is not None and "epa" in df.columns:
        st = df[st_flag].copy()
        st["epa"] = pd.to_numeric(st["epa"], errors="coerce")
        st = st[st["epa"].notna()]
        if not st.empty:
            off = st.groupby(["season", "week", "posteam"])["epa"].agg(
                ["sum", "count"]).reset_index()
            for _, r in off.iterrows():
                d = _blank(r["posteam"], r["season"], r["week"])
                d["st_epa_sum"], d["st_epa_n"] = float(r["sum"]), float(r["count"])
                rows.append(d)
            if "defteam" in st.columns:
                allowed = st.dropna(subset=["defteam"]).groupby(
                    ["season", "week", "defteam"])["epa"].agg(
                    ["sum", "count"]).reset_index()
                for _, r in allowed.iterrows():
                    d = _blank(r["defteam"], r["season"], r["week"])
                    d["st_allow_epa_sum"], d["st_allow_epa_n"] = float(r["sum"]), float(r["count"])
                    rows.append(d)
    counts = pd.DataFrame(rows)
    if counts.empty:
        return pd.DataFrame(columns=cols)
    return counts.groupby(["season", "week", "team"], as_index=False).sum(numeric_only=True)


def point_in_time_rates2(counts: pd.DataFrame, season: int, week: int) -> tuple[dict, dict]:
    """Cumulative round-2 rates before week `week` of `season`
    plus the full prior season, from a counts frame that
    already merges round-1 and round-2 weekly counts (see
    evaluate_pbp2_features). Returns ({team: rates}, league
    averages). A rate with no denominator is None (league
    average is then substituted by the differential function,
    not here). ST rates are means (sum/n), not ratios of sums
    across teams -- the league ST mean is the mean of team
    means, matching the round-1 league-average convention."""
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
        def _rate(num, den):
            return (float(r[num]) / float(r[den])) if float(r[den]) > 0 else None
        rates[str(r["team"])] = {
            "early_off": _rate("early_num", "early_den"),
            "early_allowed": _rate("early_allow_num", "early_allow_den"),
            "rz_off": _rate("rz_tds", "rz_trips"),
            "rz_allowed": _rate("rz_allow_tds", "rz_allow_trips"),
            "third_off": _rate("third_num", "third_den"),
            "third_allowed": _rate("third_allow_num", "third_allow_den"),
            "st_off": _rate("st_epa_sum", "st_epa_n"),
            "st_allowed": _rate("st_allow_epa_sum", "st_allow_epa_n"),
        }
    league = {}
    for key in ("early_off", "early_allowed", "rz_off", "rz_allowed",
                "third_off", "third_allowed", "st_off", "st_allowed"):
        vals = [v[key] for v in rates.values() if v[key] is not None]
        league[key] = sum(vals) / len(vals) if vals else None
    return rates, league


def game_pbp2_differentials(rates: dict, league: dict, home: str, away: str) -> dict:
    """Home-minus-away differentials for all round-2 features.
    Combined features mirror round 1 (strength = off -
    allowed, home strength minus away strength). Split
    features isolate one side: off_diff = home_off - away_off
    (missing side reads as league average); def_diff =
    away_allowed - home_allowed (a lower allowed rate is
    better, so the sign flips -- a positive def_diff means
    the HOME defense is better). A None on either side of a
    differential (no league average either) yields None, and
    that feature contributes nothing to that game's margin."""
    def _val(team, key):
        v = rates.get(team, {}).get(key)
        return league.get(key) if v is None else v

    def _off(off_key):
        h, a = _val(home, off_key), _val(away, off_key)
        return (h - a) if (h is not None and a is not None) else None

    def _def(allow_key):
        h, a = _val(home, allow_key), _val(away, allow_key)
        # Home defense better <=> home allowed lower.
        return (a - h) if (h is not None and a is not None) else None

    def _combined(off_key, allow_key):
        h_off, a_off = _val(home, off_key), _val(away, off_key)
        h_al, a_al = _val(home, allow_key), _val(away, allow_key)
        if None in (h_off, a_off, h_al, a_al):
            return None
        return (h_off - h_al) - (a_off - a_al)

    return {
        "third_down": _combined("third_off", "third_allowed"),
        "third_down_off": _off("third_off"),
        "third_down_def": _def("third_allowed"),
        "st_epa": _combined("st_off", "st_allowed"),
        "early_down": _combined("early_off", "early_allowed"),
        "early_down_off": _off("early_off"),
        "early_down_def": _def("early_allowed"),
        "red_zone": _combined("rz_off", "rz_allowed"),
        "red_zone_off": _off("rz_off"),
        "red_zone_def": _def("rz_allowed"),
    }


def fit_pbp2_coeff(train_rows: list[dict], feature: str) -> float:
    """Clipped least squares on TRAIN rows only (same form as
    round 1): coeff = sum(diff * residual) / sum(diff^2),
    clipped to config.PBP2_COEFF_CAPS, residual = actual margin
    - SRS model margin. 0.0 when the feature has no usable
    variation in training."""
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
    cap = float(config.PBP2_COEFF_CAPS.get(feature, 0.0))
    return max(-cap, min(cap, num / den))


def passes_placebo_gate(summary: dict, placebo: dict) -> bool:
    """True only if the feature clears BOTH placebo p95s:
    paired delta vs baseline > placebo paired p95, and
    absolute ATS% > placebo absolute p95. `summary` is an
    evaluate_pbp2_features feature entry (with ats_pct and
    paired_vs_baseline.paired_delta_pp); `placebo` is a
    placebo_distribution() result. A missing paired entry
    (empty variant) fails, never passes by default."""
    paired = (summary.get("paired_vs_baseline") or {}).get("paired_delta_pp")
    if paired is None or summary.get("ats_pct") is None:
        return False
    return (float(paired) > float(placebo["paired_delta_p95"])
            and float(summary["ats_pct"]) > float(placebo["ats_p95"]))


def placebo_distribution(baseline_games, seeds: int = 200) -> dict:
    """Placebo paired-delta / absolute-ATS distribution over
    `seeds` random +/-0.15 pt adjustments of the baseline's
    own margins (backtest.placebo_rows). Deterministic for a
    given seed range (seeds 0..seeds-1, matching the group A
    run). Returns means, SDs, and the p95s the gate uses."""
    import numpy as np
    deltas, atss = [], []
    for seed in range(int(seeds)):
        pr = backtest_mod.placebo_rows(baseline_games, seed)
        summ = backtest_mod.summarize(pr, "placebo")
        pc = backtest_mod.paired_comparison(baseline_games, pr)
        deltas.append(pc["paired_delta_pp"])
        atss.append(summ["ats_pct"])
    d, a = np.array(deltas), np.array(atss)
    return {"seeds": int(seeds),
            "paired_delta_mean": round(float(d.mean()), 3),
            "paired_delta_sd": round(float(d.std(ddof=1)), 3),
            "paired_delta_p95": round(float(np.percentile(d, 95)), 3),
            "ats_mean": round(float(a.mean()), 3),
            "ats_sd": round(float(a.std(ddof=1)), 3),
            "ats_p95": round(float(np.percentile(a, 95)), 3)}


def evaluate_pbp2_features(games: pd.DataFrame, seasons: list[int],
                           pbp_by_season: dict) -> dict:
    """Walk-forward grading of round-2 play-by-play features.

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
    under identical (corrected push) grading. Returns summaries
    for the baseline, each single feature, and "all" (third +
    ST + early-combined + red-zone-combined), plus
    coeffs_by_season, a placebo_distribution() over the
    baseline games, a per-feature passes_placebo_gate flag,
    and an in-sample section (coeffs fit on ALL scored rows,
    graded on the same rows -- reported separately, never the
    promotion test) under "in_sample". Scored rows under "_rows".
    """
    counts1_by_season: dict[int, pd.DataFrame] = {}
    counts2_by_season: dict[int, pd.DataFrame] = {}
    for season, frame in (pbp_by_season or {}).items():
        counts1_by_season[int(season)] = pbp1.weekly_team_counts(frame)
        counts2_by_season[int(season)] = weekly_team_counts2(frame)
    all_counts1 = pd.concat(list(counts1_by_season.values()), ignore_index=True) \
        if counts1_by_season else pd.DataFrame()
    all_counts2 = pd.concat(list(counts2_by_season.values()), ignore_index=True) \
        if counts2_by_season else pd.DataFrame()
    if not all_counts1.empty and not all_counts2.empty:
        all_counts = all_counts1.merge(
            all_counts2, on=["season", "week", "team"], how="outer").fillna(0.0)
    elif not all_counts1.empty:
        all_counts = all_counts1
    else:
        all_counts = all_counts2
    first = min(seasons)
    burn_in = [s for s in (first - 2, first - 1)
               if ((games["season"] == s) & games["result"].notna()).any()]
    ordered = burn_in + list(seasons)
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
            coeffs = {f: fit_pbp2_coeff(train_rows, f)
                      for f in FEATURES2 + COMBINED_FOR_ALL}
            coeffs_by_season[season] = coeffs
        else:
            coeffs = {}
        season_games = games[games["season"] == season]
        weeks = sorted(season_games[season_games["result"].notna()]["week"].unique().tolist())
        season_rows: list[dict] = []
        for week in weeks:
            rates, league = point_in_time_rates2(all_counts, season, week)
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
                diffs = game_pbp2_differentials(rates, league, row["home_team"], row["away_team"]) \
                    if rates else {f: None for f in
                                   FEATURES2 + COMBINED_FOR_ALL}
                for f, d in diffs.items():
                    rec[f"{f}_diff"] = d
                season_rows.append(rec)
        if is_eval:
            for rec in season_rows:
                for f in FEATURES2:
                    c = coeffs.get(f, 0.0)
                    d = rec.get(f"{f}_diff")
                    rec[f"feat_{f}_margin"] = (rec["model_margin"] + c * d) \
                        if (d is not None and c) else rec["model_margin"]
                all_margin = rec["model_margin"]
                for f in COMBINED_FOR_ALL:
                    c = coeffs.get(f, 0.0)
                    d = rec.get(f"{f}_diff")
                    if d is not None and c:
                        all_margin += c * d
                rec["feat_all_margin"] = all_margin
            scored_rows.extend(season_rows)
        train_rows.extend(season_rows)
    baseline_eval_rows = [r for r in base_rows if r["season"] in seasons]
    baseline = backtest_mod.summarize(baseline_eval_rows, "baseline")
    out: dict = {"coeffs_by_season": coeffs_by_season,
                 "baseline": {k: v for k, v in baseline.items() if k != "_games"}}
    base_games = baseline["_games"]
    placebo = placebo_distribution(base_games, seeds=200)
    out["placebo"] = placebo
    for feat in FEATURES2:
        key, label = f"feat_{feat}_margin", f"pbp2_{feat}"
        graded = pbp1._grade_rows(scored_rows, key)
        s = backtest_mod.summarize(graded, label)
        s["paired_vs_baseline"] = backtest_mod.paired_comparison(base_games, s["_games"])
        entry = {k: v for k, v in s.items() if k != "_games"}
        entry["passes_placebo_gate"] = passes_placebo_gate(entry, placebo)
        out[label] = entry
    graded_all = pbp1._grade_rows(scored_rows, "feat_all_margin")
    s_all = backtest_mod.summarize(graded_all, "pbp2_all")
    s_all["paired_vs_baseline"] = backtest_mod.paired_comparison(base_games, s_all["_games"])
    entry_all = {k: v for k, v in s_all.items() if k != "_games"}
    entry_all["passes_placebo_gate"] = passes_placebo_gate(entry_all, placebo)
    out["pbp2_all"] = entry_all
    # In-sample section (NOT the promotion test): coefficients
    # fit on all scored rows, graded on those same rows.
    in_sample: dict[str, dict] = {}
    coeffs_all = {f: fit_pbp2_coeff(scored_rows, f)
                  for f in FEATURES2 + COMBINED_FOR_ALL}
    in_sample["coeffs_fit_on_all_scored_rows"] = coeffs_all
    for feat in FEATURES2:
        rows_is = []
        for rec in scored_rows:
            r2 = dict(rec)
            c = coeffs_all.get(feat, 0.0)
            d = rec.get(f"{feat}_diff")
            r2[f"feat_{feat}_margin"] = (rec["model_margin"] + c * d) \
                if (d is not None and c) else rec["model_margin"]
            rows_is.append(r2)
        graded = pbp1._grade_rows(rows_is, f"feat_{feat}_margin")
        s = backtest_mod.summarize(graded, f"pbp2_{feat}_insample")
        in_sample[f"pbp2_{feat}"] = {k: v for k, v in s.items() if k != "_games"}
    out["in_sample"] = in_sample
    out["_rows"] = scored_rows
    return out
