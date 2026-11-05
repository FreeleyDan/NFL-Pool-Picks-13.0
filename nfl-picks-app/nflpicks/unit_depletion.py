"""
Player-level matchups / unit depletion (batch 5, group B item 4) --
tested, NOT wired live.

The question: the model rates whole teams. Does a depleted UNIT --
two starting offensive linemen out against a top pass rush, two
starting receivers out, two starting defensive linemen or corners
out -- predict ATS, beyond what the team rating already knows?
Batch 2's coarse proxy (sacks allowed x sacks generated) was noise;
this is the player-level version.

How a starter is defined. NOT from depth charts: the depth-chart
release changed schema after 2024 (week -> dt timestamp, one
snapshot per season) and is a single snapshot, while snap counts
give a per-game, point-in-time starter signal for every season
2019-2025. A team's starters at a position group are the top-N
players by snaps in that group over the team's most recent 4 played
games before week W (spanning seasons when early in a season):

  OL  T/G/C, top 5 by offense snaps
  WR  top 3 by offense snaps
  DL  DE/DT/NT, top 4 by defense snaps
  CB  top 3 by defense snaps

A starter is OUT only when the week-W injury report lists them
OUT or DOUBTFUL (config.UNIT_OUT_STATUSES). Questionable does not
count -- batch-1 injury work: questionable players suit up
~75-80% of the time, so counting them would mostly flag players
who play. IR players never appear on the weekly report at all, so
a starter who went on IR in an earlier week is MISSED by this
definition -- a stated limit, not a silent gap (the weekly roster
file that could catch IR has an unconfirmed filename, same
problem as batch 2's roster note). QB is deliberately excluded:
the injuries module already owns QB changes / starter-out value,
and re-testing it here would double-count the same signal.

Matching injuries to starters is by normalised name within team
(lowercase, punctuation stripped). A last-name + first-initial
fallback covers suffix/punctuation mismatches ("Jr.", "O'Brien"
vs "OBrien"); the evaluation reports how many flags came from the
fallback, so a bad join would show up as a number, not a feeling.
Starter sets are validated against the 2024 depth charts once
(see validate_starters_vs_depth): snap-count starters should be
the depth-chart starters most of the time, or the whole flag is
built on the wrong players.

Pre-registered flags (per team, per game) and forms. Flags:
ol_out2, wr_out2, dl_out2, cb_out2 (>= 2 starters out in the
group), any_out2 (any group >= 2 out), ol_out2_vs_toprush
(ol_out2 AND the opponent's point-in-time pressure_generated is
in the top quartile that week -- the WR-vs-CB / OL-vs-DL matchup
version of the flag). Two forms per flag (12 tests, one
Bonferroni family): a fade rule (flip the pick when the PICKED
team is flagged, only if the flag's TRAINING ATS% on the model's
side is below 50% with >= 30 decided training games, else
follow) and an additive adjustment (shift the model margin by the
flag's TRAINING mean residual, actual - model in home-margin
units, clipped to +/- UNIT_ADJ_CAP -- the home-margin residual
already encodes direction for a home or an away flagged team,
so no re-orientation is needed). Rules/coefficients are learned
on TRAIN (2019-2022) only and frozen for TEST (2023-2025).

Promotion gate (ALL must hold): the flag wins on the TEST split
(paired delta > 0 vs always-follow on the same test games) AND its
full-sample paired p-value survives Bonferroni across all 12
tests (adjusted p < 0.05) AND it beats its placebo gate (fade:
random flips of the same count, 200 draws; additive:
+/-0.15 pt placebo p95 on the test split). Nothing passed; see
README batch 5 group B.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re

import pandas as pd

from . import backtest as backtest_mod, config, features as features_mod
from . import situational as sit_mod

log = logging.getLogger(__name__)

TRAIN_SEASONS = (2019, 2020, 2021, 2022)
TEST_SEASONS = (2023, 2024, 2025)

FLAGS = ("ol_out2", "wr_out2", "dl_out2", "cb_out2", "any_out2",
         "ol_out2_vs_toprush")
FORMS = ("fade", "add")


def normalize_name(name) -> str:
    """Lowercase, punctuation stripped, whitespace collapsed.
    'O'Cyrus Torrence' and \"O Cyrus Torrence\" normalise the same."""
    if name is None:
        return ""
    s = re.sub(r"[^a-z0-9 ]", " ", str(name).lower())
    return re.sub(r"\s+", " ", s).strip()


_NAME_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}


def _name_fallback_key(name: str) -> str:
    """Last name + first initial, for suffix/punctuation mismatches.
    Name suffixes (Jr./Sr./II/III) are stripped first, so
    'Marvin Harrison Jr.' falls back to 'harrison m' like the
    suffix-free 'Marvin Harrison' does."""
    parts = [p for p in normalize_name(name).split()
             if p not in _NAME_SUFFIXES]
    if len(parts) < 2:
        return normalize_name(name)
    return f"{parts[-1]} {parts[0][0]}"


# ---------------------------------------------------------------------------
# Starter sets from snap counts
# ---------------------------------------------------------------------------

def build_starter_sets(snap_counts: pd.DataFrame) -> dict:
    """Starter sets per (team, season, week).

    Input: nflverse snap_counts (season, week, team, player,
    position, offense_snaps, defense_snaps). Returns
    {(team, season, week): {group: frozenset(player_norm_names)}}
    for every team-week present in the frame, where the set is the
    top-N by snaps in the group over the team's most recent
    UNIT_LOOKBACK_GAMES played weeks before week W (weeks are
    taken in (season, week) order, so an early-season week looks
    back into the prior season). Weeks with no prior games get an
    empty set -- no starters invented from a future week.
    """
    if snap_counts is None or snap_counts.empty:
        return {}
    df = snap_counts.copy()
    df["week"] = pd.to_numeric(df["week"], errors="coerce")
    df["season"] = pd.to_numeric(df["season"], errors="coerce")
    df = df.dropna(subset=["week", "season", "team", "player", "position"])
    df["team"] = df["team"].map(config.normalize_team)
    df["player_norm"] = df["player"].map(normalize_name)
    df = df[df["player_norm"] != ""]
    lookback = int(getattr(config, "UNIT_LOOKBACK_GAMES", 4))
    groups = config.UNIT_GROUPS

    # Per (team, season, week): snap totals per (player, position).
    per_week: dict = {}
    for (team, season, week), sub in df.groupby(["team", "season", "week"]):
        agg: dict = {}
        for _, r in sub.iterrows():
            key = (r["player_norm"], str(r["position"]))
            off = float(r.get("offense_snaps") or 0)
            deff = float(r.get("defense_snaps") or 0)
            o, d = agg.get(key, (0.0, 0.0))
            agg[key] = (o + off, d + deff)
        per_week[(team, int(season), int(week))] = agg

    # Ordered week keys per team (across seasons).
    team_weeks: dict[str, list[tuple[int, int]]] = {}
    for (team, season, week) in per_week:
        team_weeks.setdefault(team, []).append((season, week))
    for team in team_weeks:
        team_weeks[team].sort()

    out: dict = {}
    for team, weeks in team_weeks.items():
        for idx, (season, week) in enumerate(weeks):
            prior = weeks[max(0, idx - lookback):idx]
            if not prior:
                out[(team, season, week)] = {g: frozenset() for g in groups}
                continue
            totals: dict[tuple[str, str], list[float]] = {}
            for key in prior:
                for (player, pos), (off, deff) in per_week[(team, key[0], key[1])].items():
                    cur = totals.setdefault((player, pos), [0.0, 0.0])
                    cur[0] += off
                    cur[1] += deff
            sets: dict[str, frozenset] = {}
            for gname, gdef in groups.items():
                snaps_col = 0 if gdef["snaps"] == "offense_snaps" else 1
                cands = [(tot[snaps_col], player)
                         for (player, pos), tot in totals.items()
                         if pos in gdef["positions"] and tot[snaps_col] > 0]
                cands.sort(key=lambda t: (-t[0], t[1]))
                sets[gname] = frozenset(p for _, p in cands[:gdef["n_starters"]])
            out[(team, season, week)] = sets
    return out


def validate_starters_vs_depth(starter_sets: dict, depth_charts: pd.DataFrame,
                               season: int, week: int) -> dict:
    """Sanity check: do snap-count starters match the depth chart's
    depth_team == 1 players? Returns per-group overlap (mean share
    of snap starters found among depth-1 players, and vice versa)
    for one season-week. A low overlap would mean the flags are
    built on the wrong players. Depth charts use gsis_id/full_name;
    matching is by normalised full_name within team. The 2025+
    depth-chart schema (dt timestamp, pos_rank) is handled by the
    caller passing the already-filtered snapshot; this function
    only needs full_name/team/position/depth_team (or pos_rank==1).
    """
    if depth_charts is None or depth_charts.empty:
        return {"checked": False, "reason": "no depth chart frame"}
    dc = depth_charts.copy()
    if "season" in dc.columns:
        dc = dc[pd.to_numeric(dc["season"], errors="coerce") == season]
    if dc.empty:
        return {"checked": False, "reason": "no rows for season"}
    if "depth_team" in dc.columns:
        dc = dc[pd.to_numeric(dc["depth_team"], errors="coerce") == 1]
    elif "pos_rank" in dc.columns:
        dc = dc[pd.to_numeric(dc["pos_rank"], errors="coerce") == 1]
    else:
        return {"checked": False, "reason": "no depth rank column"}
    team_col = "team" if "team" in dc.columns else "club_code"
    if team_col not in dc.columns:
        return {"checked": False, "reason": "no team column"}
    dc["team"] = dc[team_col].map(config.normalize_team)
    dc["player_norm"] = dc["full_name"].map(normalize_name)
    per_group: dict[str, dict] = {}
    for gname, gdef in config.UNIT_GROUPS.items():
        hits, total_snap, total_dc = 0, 0, 0
        for team in sorted(dc["team"].unique().tolist()):
            snap_set = starter_sets.get((team, season, week), {}).get(gname, frozenset())
            dc_set = set(dc[(dc["team"] == team)
                            & (dc["position"].isin(gdef["positions"]))]["player_norm"].tolist())
            total_snap += len(snap_set)
            total_dc += len(dc_set)
            hits += len(set(snap_set) & dc_set)
        per_group[gname] = {
            "snap_starters": total_snap, "depth1_players": total_dc,
            "overlap": hits,
            "share_of_snap_starters_in_depth1":
                round(hits / total_snap, 3) if total_snap else None,
            "share_of_depth1_in_snap_starters":
                round(hits / total_dc, 3) if total_dc else None,
        }
    return {"checked": True, "season": season, "week": week, "per_group": per_group}


# ---------------------------------------------------------------------------
# Depletion flags from the injury report
# ---------------------------------------------------------------------------

def build_out_sets(injuries: pd.DataFrame) -> dict:
    """{(season, week, team): {player_norm, ...}} of OUT/DOUBTFUL
    players, plus a fallback index {(season, week, team):
    {last+initial: player_norm}}. One row per player (deduped, last
    row wins) -- the source lists a player once per week, but a
    duplicate would otherwise double-count a depletion."""
    if injuries is None or injuries.empty:
        return {}
    df = injuries.copy()
    df["week"] = pd.to_numeric(df["week"], errors="coerce")
    df["season"] = pd.to_numeric(df["season"], errors="coerce")
    df = df.dropna(subset=["week", "season", "team", "full_name"])
    statuses = set(getattr(config, "UNIT_OUT_STATUSES", {"Out", "Doubtful"}))
    df = df[df["report_status"].isin(statuses)]
    df["team"] = df["team"].map(config.normalize_team)
    df["player_norm"] = df["full_name"].map(normalize_name)
    df = df[df["player_norm"] != ""]
    df = df.drop_duplicates(subset=["season", "week", "team", "player_norm"], keep="last")
    out: dict = {}
    for (season, week, team), sub in df.groupby(["season", "week", "team"]):
        names = set(sub["player_norm"].tolist())
        fb = {}
        for n in names:
            fb.setdefault(_name_fallback_key(n), n)
        out[(int(season), int(week), team)] = {"names": names, "fallback": fb}
    return out


def count_out_starters(starter_set, out_entry: dict | None) -> tuple[int, int]:
    """(n_out, n_fallback_matches) for one starter set against one
    week's out-set. Exact normalised-name match first; last-name +
    first-initial fallback second. A fallback match still counts
    as out -- but it is reported separately so a bad join shows up
    as a number."""
    if not starter_set or not out_entry:
        return 0, 0
    names, fb = out_entry["names"], out_entry["fallback"]
    n_out = n_fb = 0
    for player in starter_set:
        if player in names:
            n_out += 1
        elif _name_fallback_key(player) in fb:
            n_out += 1
            n_fb += 1
    return n_out, n_fb


def flags_for_game(home: str, away: str, season: int, week: int,
                   starter_sets: dict, out_sets: dict,
                   opp_pressure_top: dict | None = None) -> dict:
    """Depletion flags for both teams of one game. Returns
    {flag_home/flag_away: bool, *_n_home/*_n_away: int} for the
    four groups, any_out2, and ol_out2_vs_toprush (OL-depleted
    team facing a top-quartile pass rush -- the opponent's
    pressure comes from opp_pressure_top, a {(season, week, team):
    bool} map built from point-in-time team stats; when it is
    None the interaction flag is False, never guessed)."""
    result: dict = {"fallback_matches": 0}
    for side, team in (("home", home), ("away", away)):
        sets = starter_sets.get((team, season, week), {})
        out_entry = out_sets.get((season, week, team))
        counts = {}
        for gname in config.UNIT_GROUPS:
            n, n_fb = count_out_starters(sets.get(gname, frozenset()), out_entry)
            counts[gname] = n
            result["fallback_matches"] += n_fb
            result[f"{gname.lower()}_out_n_{side}"] = n
            result[f"{gname.lower()}_out2_{side}"] = n >= 2
        result[f"any_out2_{side}"] = any(counts[g] >= 2 for g in counts)
        opp = away if side == "home" else home
        top = bool(opp_pressure_top.get((season, week, opp))) if opp_pressure_top else False
        result[f"ol_out2_vs_toprush_{side}"] = bool(counts["OL"] >= 2 and top)
    return result


def precompute_flags(games: pd.DataFrame, snap_counts_by_season: dict,
                     injuries_by_season: dict,
                     team_stats_by_season: dict | None = None) -> dict:
    """Flags for every (season, week, home, away) in `games`, plus
    diagnostics (starter sets built, fallback-match count, top-rush
    coverage). Snap/injury/team-stats frames are passed per season
    so the caller controls the source; nothing here touches the
    network. Pressure quartiles are point-in-time (team stats for
    weeks < W only)."""
    all_snaps = pd.concat([df for df in snap_counts_by_season.values()
                           if df is not None and not df.empty], ignore_index=True) \
        if snap_counts_by_season else pd.DataFrame()
    all_inj = pd.concat([df for df in injuries_by_season.values()
                         if df is not None and not df.empty], ignore_index=True) \
        if injuries_by_season else pd.DataFrame()
    starter_sets = build_starter_sets(all_snaps)
    out_sets = build_out_sets(all_inj)

    # Top-quartile pass rush per (season, week, team), point-in-time.
    top_rush: dict = {}
    if team_stats_by_season:
        for season, ts in team_stats_by_season.items():
            if ts is None or ts.empty:
                continue
            ts = ts.copy()
            ts["week"] = pd.to_numeric(ts["week"], errors="coerce")
            weeks = sorted(games[games["season"] == season]["week"].unique().tolist())
            for week in weeks:
                rates = features_mod.team_feature_rates(ts[ts["week"] < week])
                vals = sorted(v for v in (r.get("pressure_generated")
                                          for r in rates.values()) if v is not None)
                if not vals:
                    continue
                q = vals[int(len(vals) * float(getattr(
                    config, "UNIT_TOP_RUSH_QUARTILE", 0.75)))]
                for team, r in rates.items():
                    pg = r.get("pressure_generated")
                    if pg is not None:
                        top_rush[(season, week, team)] = pg >= q
    flags: dict = {}
    total_fb = 0
    for _, row in games.iterrows():
        key = (int(row["season"]), int(row["week"]),
               row["home_team"], row["away_team"])
        f = flags_for_game(row["home_team"], row["away_team"],
                           int(row["season"]), int(row["week"]),
                           starter_sets, out_sets, top_rush)
        total_fb += f.pop("fallback_matches", 0)
        flags[key] = f
    return {"flags": flags, "starter_sets_built": len(starter_sets),
            "fallback_matches": total_fb,
            "top_rush_entries": len(top_rush),
            "diagnostics": {
                "starter_team_weeks": len(starter_sets),
                "out_team_weeks": len(out_sets),
                "fallback_matches": total_fb,
            }}


# ---------------------------------------------------------------------------
# Walk-forward evaluation (same gate shape as situational.py)
# ---------------------------------------------------------------------------

def _picked_flags(row: dict) -> set[str]:
    picked = sit_mod._picked_team(row)
    side = "home" if picked == row["home_team"] else "away"
    return {f for f in FLAGS if row.get(f"{f}_{side}")}


def describe_flags(rows: list[dict]) -> dict:
    """Descriptive ATS% on the model's side when the PICKED team is
    flagged, per flag (full sample). A flag the model already
    prices shows ~50% here; a real miss shows well below."""
    out = {}
    for flag in FLAGS:
        sub = [r for r in rows if flag in _picked_flags(r)]
        dec = [r for r in sub if float(r["actual_margin"]) != float(r["spread_line"])]
        wins = sum(1 for r in dec
                   if (float(r["actual_margin"]) > float(r["spread_line"]))
                   == (float(r["model_margin"]) > float(r["spread_line"])))
        out[flag] = {"n": len(sub), "decided": len(dec),
                     "ats_pct": round(100 * wins / len(dec), 2) if dec else None}
    return out


def _learn_rules(train_rows: list[dict]) -> dict:
    """Fade rule per flag: 'fade' if train ATS% on the model's side
    is below 50% with >= MIN_TRAIN_DECIDED decided games, else
    'follow'. A flag with too few training games defaults to
    follow -- the same small-sample guard as situational spots."""
    min_decided = int(getattr(config, "UNIT_MIN_TRAIN_DECIDED", 30))
    rules = {}
    for flag in FLAGS:
        sub = [r for r in train_rows if flag in _picked_flags(r)]
        dec = [r for r in sub if float(r["actual_margin"]) != float(r["spread_line"])]
        if len(dec) < min_decided:
            rules[flag] = "follow"
            continue
        wins = sum(1 for r in dec
                   if (float(r["actual_margin"]) > float(r["spread_line"]))
                   == (float(r["model_margin"]) > float(r["spread_line"])))
        rules[flag] = "fade" if (100 * wins / len(dec)) < 50 else "follow"
    return rules


def _learn_adjustments(train_rows: list[dict]) -> dict:
    """Additive adjustment per flag: train mean residual
    (actual - model, home-margin units) on games where the picked
    team is flagged, clipped to +/- UNIT_ADJ_CAP. Flags with too
    few decided training games get 0.0."""
    cap = float(getattr(config, "UNIT_ADJ_CAP", 3.0))
    min_decided = int(getattr(config, "UNIT_MIN_TRAIN_DECIDED", 30))
    adj = {}
    for flag in FLAGS:
        sub = [r for r in train_rows if flag in _picked_flags(r)
               and float(r["actual_margin"]) != float(r["spread_line"])]
        if len(sub) < min_decided:
            adj[flag] = 0.0
            continue
        resid = [float(r["actual_margin"]) - float(r["model_margin"]) for r in sub]
        adj[flag] = round(max(-cap, min(cap, sum(resid) / len(resid))), 3)
    return adj


def _apply_fade(rows: list[dict], rules: dict) -> dict:
    base_outcomes, var_outcomes = [], []
    n_flipped = 0
    for r in rows:
        m, line, actual = (float(r["model_margin"]), float(r["spread_line"]),
                           float(r["actual_margin"]))
        base_outcomes.append(sit_mod._grade(m, line, actual))
        flagged = _picked_flags(r)
        if any(rules.get(f) == "fade" for f in flagged):
            # Flip the pick: reflect the margin across the line.
            var_outcomes.append(sit_mod._grade(line - (m - line), line, actual))
            n_flipped += 1
        else:
            var_outcomes.append(base_outcomes[-1])
    return {"faded": sit_mod._summ(var_outcomes),
            "always_follow": sit_mod._summ(base_outcomes),
            "paired_vs_follow": sit_mod._paired(base_outcomes, var_outcomes),
            "n_flipped": n_flipped}


def _apply_add(rows: list[dict], adjustments: dict) -> dict:
    cap = float(getattr(config, "UNIT_ADJ_CAP", 3.0))
    base_outcomes, var_outcomes = [], []
    base_errs, var_errs = [], []
    n_adjusted = 0
    for r in rows:
        m, line, actual = (float(r["model_margin"]), float(r["spread_line"]),
                           float(r["actual_margin"]))
        shift = sum(float(adjustments.get(f, 0.0)) for f in _picked_flags(r))
        shift = max(-cap, min(cap, shift))
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
            "n_adjusted": n_adjusted, "adjustments": dict(adjustments)}


def evaluate_all(games: pd.DataFrame, seasons: list[int] | None = None,
                 train_seasons: tuple[int, ...] = TRAIN_SEASONS,
                 test_seasons: tuple[int, ...] = TEST_SEASONS,
                 snap_counts_by_season: dict | None = None,
                 injuries_by_season: dict | None = None,
                 team_stats_by_season: dict | None = None,
                 precomputed: dict | None = None) -> dict:
    """Full item-4 evaluation. Replays the baseline with depletion
    context, then for each of the 6 flags: descriptive ATS, a
    train-learned fade rule applied to test, and a train-learned
    additive adjustment applied to test -- each with its paired
    comparison, Bonferroni-adjusted p (family = all 12 flag tests),
    and its placebo gate. `precomputed` (from precompute_flags)
    skips the flag build when the caller already has it. Returns
    the baseline summary (must reproduce 50.03% / 898 / 1,795),
    per-flag results, and `promoted_flags` (empty unless a flag
    clears every gate)."""
    if seasons is None:
        seasons = list(range(2019, 2026))
    games = backtest_mod.load_games_frame(games)
    if precomputed is None:
        precomputed = precompute_flags(games, snap_counts_by_season or {},
                                       injuries_by_season or {},
                                       team_stats_by_season)
    flags = precomputed["flags"]

    def _ctx(season, week, row, ratings, games_played, hfa, season_games,
             played_current, prior_games):
        return dict(flags.get((int(season), int(week),
                               row["home_team"], row["away_team"]), {}))

    rows = sit_mod.replay_with_context(games, seasons, context_fn=_ctx)
    base = sit_mod._summ([r["ats"] for r in rows])
    desc = describe_flags(rows)
    train_rows = [r for r in rows if r["season"] in train_seasons]
    test_rows = [r for r in rows if r["season"] in test_seasons]
    rules = _learn_rules(train_rows)
    adjustments = _learn_adjustments(train_rows)

    import pandas as _pd

    def _frame(sub_rows):
        return _pd.DataFrame([{"season": r["season"], "week": r["week"],
                               "home_team": r["home_team"], "away_team": r["away_team"],
                               "model_margin": r["model_margin"], "spread_line": r["spread_line"],
                               "actual_margin": r["actual_margin"], "ats": r["ats"]}
                              for r in sub_rows])

    placebo_add_test = sit_mod.placebo_additive_distribution(_frame(test_rows))
    placebo_add_full = sit_mod.placebo_additive_distribution(_frame(rows))

    n_tests = len(FLAGS) * len(FORMS)
    per_flag: dict[str, dict] = {}
    promoted: list[str] = []
    for flag in FLAGS:
        test_rule = {f: (rules[f] if f == flag else "follow") for f in FLAGS}
        fade_test = _apply_fade(test_rows, test_rule)
        fade_full = _apply_fade(rows, test_rule)
        p_raw = fade_full["paired_vs_follow"]["p_two_sided"]
        p_adj = min(1.0, p_raw * n_tests)
        flip_placebo = sit_mod.placebo_flip_distribution(
            test_rows, fade_test["n_flipped"],
            trials=int(getattr(config, "SITUATIONAL_PLACEBO_FLIP_TRIALS", 200)))
        fade_gate = bool(
            fade_test["paired_vs_follow"]["paired_delta_pp"] > 0
            and fade_test["paired_vs_follow"]["paired_delta_pp"] > flip_placebo["p95_pp"]
            and p_adj < 0.05)
        test_adj = {f: (adjustments[f] if f == flag else 0.0) for f in FLAGS}
        add_test = _apply_add(test_rows, test_adj)
        add_full = _apply_add(rows, test_adj)
        p_raw_add = add_full["paired_vs_follow"]["p_two_sided"]
        p_adj_add = min(1.0, p_raw_add * n_tests)
        add_gate = bool(
            add_test["paired_vs_follow"]["paired_delta_pp"] > 0
            and add_test["paired_vs_follow"]["paired_delta_pp"] > placebo_add_test["p95_pp"]
            and p_adj_add < 0.05)
        if fade_gate:
            promoted.append(f"{flag}:fade")
        if add_gate:
            promoted.append(f"{flag}:add")
        per_flag[flag] = {
            "descriptive": desc[flag],
            "train_ats_pct": _train_ats(train_rows, flag),
            "test_ats_pct": _train_ats(test_rows, flag),
            "fade": {"rule": rules[flag], "test": fade_test, "full": fade_full,
                     "p_bonferroni": round(p_adj, 4),
                     "placebo_flip_p95_pp": flip_placebo["p95_pp"],
                     "gate_pass": fade_gate},
            "add": {"adjustment": adjustments[flag], "test": add_test, "full": add_full,
                    "p_bonferroni": round(p_adj_add, 4),
                    "placebo_add_p95_pp": placebo_add_test["p95_pp"],
                    "gate_pass": add_gate},
        }
    return {"baseline": base, "n": len(rows),
            "train_seasons": list(train_seasons), "test_seasons": list(test_seasons),
            "bonferroni_family_n": n_tests,
            "placebo_additive": placebo_add_full,
            "placebo_additive_test": placebo_add_test,
            "rule_map": rules, "adjustments": adjustments,
            "diagnostics": precomputed.get("diagnostics", {}),
            "per_flag": per_flag, "promoted_flags": promoted,
            "_rows": rows}


def _train_ats(sub_rows: list[dict], flag: str) -> dict:
    sub = [r for r in sub_rows if flag in _picked_flags(r)]
    dec = [r for r in sub if float(r["actual_margin"]) != float(r["spread_line"])]
    wins = sum(1 for r in dec
               if (float(r["actual_margin"]) > float(r["spread_line"]))
               == (float(r["model_margin"]) > float(r["spread_line"])))
    return {"n": len(sub), "decided": len(dec),
            "ats_pct": round(100 * wins / len(dec), 2) if dec else None}


if __name__ == "__main__":
    import sys
    _games_csv = sys.argv[1] if len(sys.argv) > 1 else "/tmp/games.csv"
    _data_dir = sys.argv[2] if len(sys.argv) > 2 else "/tmp/nfl-b5b"
    _games = pd.read_csv(_games_csv, low_memory=False)
    _sc, _inj, _ts = {}, {}, {}
    for _s in range(2019, 2026):
        for _prefix, _store in (("sc", _sc), ("inj", _inj), ("ts", _ts)):
            _p = os.path.join(_data_dir, f"{_prefix}_{_s}.csv")
            if os.path.exists(_p):
                _store[_s] = pd.read_csv(_p, low_memory=False)
    _pre = precompute_flags(_games, _sc, _inj, _ts)
    print("precompute diagnostics:", json.dumps(_pre["diagnostics"], indent=2))
    _out = evaluate_all(_games, snap_counts_by_season=_sc,
                        injuries_by_season=_inj, team_stats_by_season=_ts,
                        precomputed=_pre)
    _out.pop("_rows", None)
    print(json.dumps(_out, indent=2, default=str))
