"""
Injury review layer. Preserves the original design choice: injuries never
move a rating automatically. They produce flags and *suggested* manual
adjustments only. Turn config.INJ_AUTO_APPLY on only if you've validated
the position-value heuristics with real backtesting.

Depth chart status (starter vs. backup) now multiplies the impact -- a
"questionable" backup guard barely moves the suggestion, where a
questionable starting QB does. This is still a heuristic, not calibrated
against real outcomes, but it's a meaningfully better heuristic than
treating every player at a position identically regardless of role.
"""
from __future__ import annotations

import pandas as pd

from . import config


def _build_depth_lookup(depth_df: pd.DataFrame | None) -> tuple[dict, dict]:
    """
    Returns (by_gsis, by_name):
      by_gsis: {gsis_id: depth_rank} -- preferred, reliable ID-based match
      by_name: {(team, PLAYER_NAME_UPPER): depth_rank} -- fallback

    Handles both the current (2025+, ESPN-sourced) schema, which uses
    'player_name'/'pos_rank'/'gsis_id', and the older (pre-2025, NFL Data
    Exchange) schema, which used 'full_name'/'depth_team'. Lowest rank
    (most starter-like) wins if a player appears more than once (e.g.
    also listed on special teams).
    """
    by_gsis: dict[str, int] = {}
    by_name: dict[tuple[str, str], int] = {}
    if depth_df is None or depth_df.empty:
        return by_gsis, by_name

    name_col = next((c for c in ("player_name", "full_name", "name") if c in depth_df.columns), None)
    rank_col = "pos_rank" if "pos_rank" in depth_df.columns else (
        "depth_team" if "depth_team" in depth_df.columns else None)
    if rank_col is None:
        return by_gsis, by_name
    has_gsis = "gsis_id" in depth_df.columns

    for _, row in depth_df.iterrows():
        try:
            rank = int(row.get(rank_col))
        except (TypeError, ValueError):
            continue
        team = config.normalize_team(row.get("team"))

        if has_gsis:
            gid = str(row.get("gsis_id") or "").strip()
            if gid:
                if gid not in by_gsis or rank < by_gsis[gid]:
                    by_gsis[gid] = rank

        if name_col:
            name = str(row.get(name_col) or "").strip().upper()
            if name:
                key = (team, name)
                if key not in by_name or rank < by_name[key]:
                    by_name[key] = rank

    return by_gsis, by_name


def detect_returning_players(current_injury_df: pd.DataFrame | None, recent_history_rows: list) -> dict:
    """
    The positive mirror of the injury system: detects a STARTER who was
    OUT or DOUBTFUL in a recent week but isn't listed at all this week --
    a likely healthy return. The team's recent point-differential-based
    rating reflects games played WITHOUT that player, so their return is
    a real signal the rating hasn't caught up to yet.

    Returns {"suggested": {team: +points}, "detail": [(team, player, pos,
    note, depth_status, points, flag_note), ...]} -- same shape as
    review_injuries()'s output, review-only just like the rest of the
    injury system (never auto-applied).
    """
    if not recent_history_rows:
        return {"suggested": {}, "detail": []}

    # An ABSENT report means "unknown," not "healthy." Reports usually post
    # Wednesday-Friday, so on a Monday/Tuesday refresh the current week's
    # data is empty and every previously-injured player would falsely look
    # like a return. Only draw conclusions once this week's report has
    # actually been published.
    if current_injury_df is None or current_injury_df.empty or \
            "full_name" not in current_injury_df.columns or "team" not in current_injury_df.columns:
        return {"suggested": {}, "detail": []}

    current_players = set()
    teams_reporting = set()
    for _, row in current_injury_df.iterrows():
        team = config.normalize_team(row.get("team"))
        teams_reporting.add(team)
        name = str(row.get("full_name") or "").strip().upper()
        if name:
            current_players.add((team, name))

    # Find starters who were OUT/DOUBTFUL at some point in the lookback
    # window; keep the most recent sighting (and its status) per player.
    candidates: dict[tuple[str, str], tuple] = {}
    for row in recent_history_rows:
        status = str(row["status"] or "").upper()
        depth_status = str(row["depth_status"] or "")
        if status not in ("OUT", "DOUBTFUL") or depth_status != "Starter":
            continue
        team = row["team"]
        player = row["player"] or ""
        key = (team, player.strip().upper())
        if key not in candidates or row["week"] > candidates[key][3]:
            candidates[key] = (team, player, row["position"], row["week"], status)

    suggested: dict[str, float] = {}
    detail = []
    for (team, name_upper), (team_disp, player, pos, seen_week, status) in candidates.items():
        if (team, name_upper) in current_players:
            continue  # still listed this week -- not a clean return, don't guess
        if team not in teams_reporting:
            continue  # this team has no entries in the current report at all -- can't tell if it's posted
        val = min(config.POS_VALUE.get(str(pos).upper(), 0.25), config.MAX_SUGGESTED_INJ_ADJ)
        suggested[team_disp] = suggested.get(team_disp, 0.0) + val
        detail.append((
            team_disp, player, pos, f"Returning (was {status} wk {seen_week})",
            "Starter", round(val, 2), "RETURN -- starter back from injury",
        ))

    for t in suggested:
        suggested[t] = min(config.MAX_SUGGESTED_INJ_ADJ, round(suggested[t], 1))

    return {"suggested": suggested, "detail": detail}


def _long_term_out_from_roster(depth_df, roster_df, already_seen: set) -> list[tuple]:
    """
    Catches players who are on IR/PUP/another reserve list and therefore
    never appear in the week's day-to-day practice report at all -- that
    report only covers players who could conceivably suit up, so a
    season-ending or multi-week injury is invisible to it by design (real
    NFL mechanics, not a data gap). Cross-references the depth chart's
    STARTERS against roster status; skips anyone already captured from
    the weekly report (already_seen: a set of (team, PLAYER_NAME_UPPER))
    to avoid double-counting the same player from two sources.
    """
    if depth_df is None or depth_df.empty or roster_df is None or roster_df.empty:
        return []
    if "status" not in roster_df.columns or "gsis_id" not in roster_df.columns:
        return []
    rank_col = "pos_rank" if "pos_rank" in depth_df.columns else (
        "depth_team" if "depth_team" in depth_df.columns else None)
    name_col = next((c for c in ("player_name", "full_name", "name") if c in depth_df.columns), None)
    if rank_col is None or name_col is None or "gsis_id" not in depth_df.columns:
        return []

    roster_status = {}
    for _, row in roster_df.iterrows():
        gid = str(row.get("gsis_id") or "").strip()
        if gid:
            roster_status[gid] = str(row.get("status") or "").strip().upper()

    out = []
    for _, row in depth_df.iterrows():
        try:
            rank = int(row.get(rank_col))
        except (TypeError, ValueError):
            continue
        if rank != 1:
            continue  # only starters -- backups' roster churn isn't worth flagging
        gid = str(row.get("gsis_id") or "").strip()
        status = roster_status.get(gid)
        if not status or status == "ACT":
            continue  # active (or unknown) -- no signal
        team = config.normalize_team(row.get("team"))
        player = str(row.get(name_col) or "")
        if (team, player.strip().upper()) in already_seen:
            continue  # already captured from the weekly practice report
        pos = str(row.get("pos_abb") or row.get("pos_name") or row.get("position") or "").strip().upper()
        val = min(config.POS_VALUE.get(pos, 0.25), config.MAX_SUGGESTED_INJ_ADJ)
        out.append((team, player, pos, f"Roster status: {status}", "Starter", val,
                     "LONG-TERM OUT -- not on active roster, won't appear in the weekly practice report"))
    return out


def review_injuries(injuries_df: pd.DataFrame | None, week: int, depth_df: pd.DataFrame | None = None,
                     roster_df: pd.DataFrame | None = None) -> dict:
    """
    Returns {
      "flags": {team: [flag_str, ...]},
      "suggested": {team: -points},   # negative = suggested penalty
      "detail": [ (team, player, pos, status, depth_status, impact, flag_note), ... ],
      "note": str,
      "depth_match_rate": float | None,  # fraction of designations matched to a depth chart row
    }

    Combines two independent signals: the week's day-to-day practice
    report (injuries_df) AND roster status (roster_df) for anyone on
    IR/PUP/etc. who wouldn't appear in that report at all. Either can be
    missing/empty without preventing the other from working -- a quiet
    injury-report week (e.g. no games until Thursday) shouldn't hide a
    long-term injury that roster status already knows about.
    """
    flags: dict[str, list[str]] = {}
    suggested: dict[str, float] = {}
    detail = []
    counted = 0
    depth_matched = 0
    already_seen: set[tuple[str, str]] = set()

    by_gsis, by_name = _build_depth_lookup(depth_df)
    has_injury_gsis = injuries_df is not None and "gsis_id" in injuries_df.columns

    if injuries_df is not None and not injuries_df.empty:
        has_practice = "practice_status" in injuries_df.columns
        has_name = "full_name" in injuries_df.columns

        for _, row in injuries_df.iterrows():
            report_status = str(row.get("report_status") or "").strip().upper()
            practice_status = str(row.get("practice_status") or "").strip().upper() if has_practice else ""
            mult = config.STATUS_MULT.get(report_status)

            if mult is None and report_status == "":
                if "DID NOT PARTICIPATE" in practice_status or practice_status == "DNP":
                    mult = 0.6
                elif "LIMITED" in practice_status or practice_status == "LP":
                    mult = 0.25

            if not mult:
                continue

            pos = str(row.get("position") or "").strip().upper()
            val = config.POS_VALUE.get(pos, 0.25)
            team = config.normalize_team(row.get("team"))
            player = str(row.get("full_name") or "") if has_name else ""
            status_label = report_status or f"PRACTICE: {practice_status}"
            already_seen.add((team, player.strip().upper()))

            depth_team = None
            if has_injury_gsis:
                gid = str(row.get("gsis_id") or "").strip()
                if gid and gid in by_gsis:
                    depth_team = by_gsis[gid]
            if depth_team is None:
                depth_team = by_name.get((team, player.strip().upper()))

            if depth_team is not None:
                depth_mult = config.DEPTH_MULTIPLIER.get(depth_team, config.DEPTH_MULTIPLIER_DEFAULT)
                depth_status = "Starter" if depth_team == 1 else f"Backup (depth {depth_team})"
                depth_matched += 1
            else:
                depth_mult = 1.0  # unknown role -- don't guess, apply full weight as before
                depth_status = "Unknown"

            counted += 1
            weighted_val = val * mult * depth_mult
            suggested[team] = suggested.get(team, 0.0) + weighted_val

            is_critical_pos = pos in config.CRITICAL_POSITIONS
            # Critical-position flags only fire for starters (or unknown depth,
            # to stay cautious) -- a backup QB being out isn't a CRITICAL flag.
            is_starter_or_unknown = depth_team is None or depth_team == 1
            if is_critical_pos and is_starter_or_unknown and (
                report_status in ("OUT", "DOUBTFUL") or (report_status == "" and mult >= 0.6)
            ):
                flags.setdefault(team, []).append(
                    f"{pos} STATUS CRITICAL ({(player + ', ') if player else ''}{status_label})"
                )
            elif report_status == "OUT" and val >= 1.0 and is_starter_or_unknown:
                flags.setdefault(team, []).append(f"KEY {pos} OUT" + (f" ({player})" if player else ""))

            detail.append((
                team, player, pos, status_label, depth_status,
                -round(weighted_val, 2),
                "REVIEW -- critical position" if is_critical_pos else "",
            ))

    # Long-term-out starters (IR/PUP/etc.) who never appear in the weekly
    # practice report at all -- merged in regardless of whether the weekly
    # report had anything, and never double-counting a player already
    # captured above.
    long_term_out = _long_term_out_from_roster(depth_df, roster_df, already_seen)
    for team, player, pos, status_label, depth_status, val, flag_note in long_term_out:
        suggested[team] = suggested.get(team, 0.0) + val
        if pos in config.CRITICAL_POSITIONS:
            flags.setdefault(team, []).append(f"{pos} STATUS CRITICAL ({player}, {status_label})")
        detail.append((team, player, pos, status_label, depth_status, -round(val, 2), flag_note))

    for t in suggested:
        suggested[t] = -min(config.MAX_SUGGESTED_INJ_ADJ, round(suggested[t], 1))

    depth_match_rate = round(depth_matched / counted, 2) if counted else None

    note_parts = []
    if counted:
        note_parts.append(
            f"{counted} designations found for week {week} across "
            f"{len({d[0] for d in detail if 'LONG-TERM' not in d[6]})} teams."
        )
        if depth_df is not None and not depth_df.empty:
            note_parts.append(f"Depth chart matched {depth_matched}/{counted} players ({depth_match_rate:.0%}).")
        else:
            note_parts.append("Depth chart data unavailable this run -- impacts use full weight (no starter/backup discount).")
    else:
        note_parts.append(f"No week-{week} day-to-day designations posted yet (reports usually post Wed-Fri).")
    if long_term_out:
        note_parts.append(
            f"{len(long_term_out)} additional starter(s) found via roster status (IR/PUP/etc.) -- these "
            f"wouldn't show up in the day-to-day report at all since they're not on the active roster."
        )
    elif roster_df is None or roster_df.empty:
        note_parts.append("Roster status data unavailable this run -- long-term-out starters not on the "
                           "active roster (IR/PUP) won't be caught unless they also appear in the weekly report.")
    note = " NO automatic adjustments applied -- review below and use manual overrides if you agree. " + \
        " ".join(note_parts)

    return {"flags": flags, "suggested": suggested, "detail": detail, "note": note,
            "depth_match_rate": depth_match_rate}
