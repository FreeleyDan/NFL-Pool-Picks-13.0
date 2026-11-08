"""
Weekly job. Run this manually (`python weekly_run.py`) or on a schedule via
the included GitHub Actions workflow. It:

  1. Pulls current data (schedule/results, injuries, team stats)
  2. Computes SOS-adjusted ratings + empirical home field
  3. Predicts next week's ATS/total picks
  4. Runs the survivor optimizer against the full remaining schedule
  5. Saves everything to SQLite
  6. Emails a summary (only if SMTP env vars are set -- safe to skip locally)
"""
from __future__ import annotations

import logging
import os
import smtplib
from email.mime.text import MIMEText

from nflpicks import config, data, ratings, injuries, epa, model, survivor, db, scoring, manual_adjustments, run_diff, experiment

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("weekly_run")


def send_email(subject: str, body: str):
    host = os.environ.get("SMTP_HOST")
    if not host:
        log.info("SMTP not configured (SMTP_HOST unset) -- skipping email. Body:\n%s", body)
        return
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = os.environ.get("SMTP_FROM", config.EMAIL)
    msg["To"] = config.EMAIL
    with smtplib.SMTP(host, int(os.environ.get("SMTP_PORT", "587"))) as server:
        server.starttls()
        server.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
        server.send_message(msg)
    log.info("Email sent to %s", config.EMAIL)


def _capture_previous_state(season: int, week: int, db_path: str | None = None) -> dict | None:
    """Snapshot the previously SAVED run for this season/week, for
    the what-changed diff (batch 3, item 6). MUST be called before
    this run's first save (save_ratings_snapshot), because every
    save overwrites the previous run's rows in place -- after the
    first save there is nothing left to diff against. Returns None
    (not an error) when no previous run exists for the week, or
    when the read itself fails: a diff must never break the run.
    Picks carry their run_time, so the diff can say which run it
    is diffing against (Tuesday's run, or Thursday 7 AM's)."""
    try:
        with db.connect(db_path) as conn:
            picks = conn.execute(
                "SELECT * FROM picks WHERE season=? AND week=?", (season, week)).fetchall()
            ratings_rows = conn.execute(
                "SELECT * FROM team_ratings_snapshot WHERE season=? AND week=?",
                (season, week)).fetchall()
            try:
                inj_rows = conn.execute(
                    "SELECT * FROM injury_review WHERE season=? AND week=?",
                    (season, week)).fetchall()
            except Exception:
                inj_rows = []
            try:
                line_rows = conn.execute(
                    "SELECT * FROM line_snapshots WHERE season=? AND week=?",
                    (season, week)).fetchall()
            except Exception:
                line_rows = []
    except Exception as exc:
        log.warning("Could not capture previous run state for diff (continuing without a diff): %s", exc)
        return None
    if not picks and not ratings_rows and not inj_rows and not line_rows:
        return None
    old_picks = {}
    run_times = []
    for p in picks:
        old_picks[(p["home_team"], p["away_team"])] = {
            "pick": p["pick"], "edge": p["edge"], "model_independent": p["model_independent"],
            "vegas_spread": p["vegas_spread"], "pool_spread": p["pool_spread"],
            "cover_prob": p["cover_prob"], "weather_note": p["weather_note"],
            "total_pick": p["total_pick"], "model_total": p["model_total"],
        }
        if p["run_time"]:
            run_times.append(p["run_time"])
    return {
        "old_run_time": max(run_times) if run_times else None,
        "old_picks": old_picks,
        "old_ratings": {r["team"]: r["rating"] for r in ratings_rows},
        "old_injuries": [dict(r) for r in inj_rows],
        "old_lines": {(r["home_team"], r["away_team"], r["source"]): r["line"] for r in line_rows},
    }


def _save_what_changed(season: int, week: int, previous: dict | None,
                       predictions, team_ratings: dict, inj_detail: list):
    """Build and persist the what-changed diff for this run, and
    record it as a diagnostic so the Actions log and Data Health
    show it even without opening the dashboard. New values come
    from this run's in-memory results (predictions, ratings,
    injury detail); old values come from _capture_previous_state.
    Never raises: a diff failure must not fail a run whose picks
    are already saved."""
    try:
        new_picks = {}
        new_lines = {}
        for p in predictions:
            new_picks[(p.home, p.away)] = {
                "pick": p.pick, "edge": p.edge, "model_independent": p.model_independent,
                "vegas_spread": p.vegas_spread, "pool_spread": p.pool_spread,
                "cover_prob": p.cover_prob, "weather_note": p.weather_note,
                "total_pick": p.total_pick, "model_total": p.model_total,
            }
            if p.pool_spread is not None:
                new_lines[(p.home, p.away, "pool")] = p.pool_spread
            if p.vegas_spread is not None:
                new_lines[(p.home, p.away, "closing")] = p.vegas_spread
        if previous is None:
            diff = run_diff.build_diff({}, {}, {}, {}, [], [], {}, {})
            data._record("What changed since last run", True, diff["summary"])
            log.info("What changed: %s", diff["summary"])
            return
        diff = run_diff.build_diff(
            previous["old_picks"], new_picks,
            previous["old_ratings"], team_ratings,
            previous["old_injuries"], inj_detail,
            previous["old_lines"], new_lines)
        db.save_run_diff(season, week, diff["items"])
        prev_label = f"previous run ({previous['old_run_time']})" if previous["old_run_time"] else "previous run"
        data._record("What changed since last run", True,
                     f"vs {prev_label}: {diff['summary']}")
        log.info("What changed vs %s: %s", prev_label, diff["summary"])
        for it in diff["items"][:20]:
            log.info("  CHANGED [%s] %s: %s", it["category"], it["matchup"], it["detail"])
    except Exception as exc:
        log.warning("What-changed diff failed (picks already saved, continuing): %s", exc)


def main():
    db.init_db()
    data.reset_diagnostics()
    try:
        _run()
    finally:
        # Always persist whatever diagnostics were collected, even on a
        # partial failure -- that's often the most useful evidence of what
        # went wrong.
        db.save_diagnostics(data.DIAGNOSTICS)


def _apply_turnover_adjustment(team_ratings: dict, team_stats) -> dict:
    """Apply the turnover-luck regression to `team_ratings` in place
    and return the {team: points} adjustment applied -- or apply
    nothing and return {} when config.TURNOVER_LUCK_LIVE is False
    (the default since 2026-11-08; see config.py for the
    walk-forward evidence). The team-stats load stays with the
    caller (it also feeds the EPA layer), so this helper only
    decides and applies. `team_stats` may be None (load failed):
    then no adjustment is possible either way. Kept as a separate
    function so the on/off behaviour is unit-testable offline
    without running a whole weekly run."""
    from nflpicks import turnovers as turnovers_mod

    if not getattr(config, "TURNOVER_LUCK_LIVE", False):
        data._record(
            "Turnover luck regression", True,
            "OFF (config.TURNOVER_LUCK_LIVE=False, 2026-11-08 walk-forward test: "
            "the adjustment cost ATS at fade-12 with no demonstrated benefit in any fade) "
            "-- ratings are the plain SRS ratings the backtest grades, so live == backtest.",
        )
        return {}
    turnover_margins = turnovers_mod.compute_turnover_margins(team_stats)
    turnover_luck_adj = turnovers_mod.luck_adjustment(turnover_margins)
    if turnover_luck_adj:
        for t, adj in turnover_luck_adj.items():
            if t in team_ratings:
                team_ratings[t] = round(team_ratings[t] + adj, 2)
        nonzero = {t: a for t, a in turnover_luck_adj.items() if a != 0}
        biggest = sorted(nonzero.items(), key=lambda x: abs(x[1]), reverse=True)[:5]
        data._record(
            "Turnover luck regression", True,
            (f"Applied to {len(nonzero)} teams. Largest adjustments: "
             + ", ".join(f"{t} {a:+.1f}" for t, a in biggest)) if nonzero
            else "Team stats loaded, but no team's turnover margin was large enough to trigger an adjustment.",
        )
    else:
        data._record("Turnover luck regression", False,
                     "Team stats unavailable or missing turnover columns (expected before the season "
                     "starts) -- no adjustment applied this run.")
    return turnover_luck_adj


def _run():
    log.info("Loading schedule/results for season %s...", config.SEASON)
    games = data.load_games(config.SEASON)
    played, played_prior, upcoming = data.split_games(games, config.SEASON)

    if upcoming.empty:
        log.info("No upcoming games found -- season may be over or lines not posted yet.")
        return
    week = int(upcoming["week"].iloc[0])
    log.info("Predicting week %s (%s games)", week, len(upcoming))

    # What-changed baseline (batch 3, item 6): capture BEFORE the
    # first save below overwrites the previous run's rows. The
    # diff itself is built and saved after predictions exist.
    previous_state = _capture_previous_state(config.SEASON, week)

    hfa = ratings.compute_empirical_hfa(played, played_prior)

    if "location" in games.columns:
        n_neutral_prior = int((played_prior.get("location") == "Neutral").sum()) if len(played_prior) else 0
        n_neutral_upcoming = int((upcoming.get("location") == "Neutral").sum())
        data._record(
            "Neutral-site game handling", True,
            f"{n_neutral_prior} neutral-site game(s) in prior-season data (excluded from HFA calc, no HFA "
            f"applied in their rating contribution), {n_neutral_upcoming} in this week's upcoming games "
            f"(will get 0 HFA in the prediction instead of a false home-field boost).",
        )
    else:
        data._record("Neutral-site game handling", False,
                     "'location' column not found in schedule data -- neutral-site games (international "
                     "games, Super Bowls) cannot be detected and will incorrectly get full home-field "
                     "advantage applied.")

    audit_warnings = ratings.audit_team_game_counts(played_prior)
    data._record(
        "Audit prior-season game counts", len(audit_warnings) == 0,
        "All 32 teams have a plausible game count." if not audit_warnings else " | ".join(audit_warnings),
    )

    team_ratings, games_played = ratings.blended_team_ratings(played, played_prior, hfa)
    data._record(
        "SRS convergence", bool(getattr(ratings, "LAST_SRS_CONVERGED", True)),
        f"Final SRS delta: {getattr(ratings, 'LAST_SRS_DELTA', 0.0):.5f} "
        f"(tolerance {config.SRS_TOLERANCE}) -- {'converged' if getattr(ratings, 'LAST_SRS_CONVERGED', True) else 'DID NOT CONVERGE, ratings may be unstable'}",
    )
    raw_diff = ratings.raw_point_differential(played_prior)

    # Turnover luck regression applies HERE, before the snapshot is saved
    # and before the trace/sign-check diagnostics run, so everything
    # downstream (dashboard, picks, survivor) sees the same final rating.
    # CHANGED 2026-11-08: gated behind config.TURNOVER_LUCK_LIVE
    # (False -- see config.py). _apply_turnover_adjustment decides,
    # applies, and records the diagnostic either way.
    team_stats = data.load_team_stats(config.SEASON)
    turnover_luck_adj = _apply_turnover_adjustment(team_ratings, team_stats)

    db.save_ratings_snapshot(config.SEASON, week, team_ratings, games_played, raw_diff)

    # Direct sign-mismatch check: a team that outscored opponents overall
    # should not end up with a negative SRS rating, and vice versa.
    # FIXED 2026-11-04: this used to compare the CURRENT blended rating
    # against LAST season's raw differential, so any team that flipped
    # year-over-year false-flagged mid-season. Now it uses the current
    # season's differential once a team has >=4 current games (the same
    # full-confidence threshold as the rating model), and falls back to
    # last season's differential only for teams with a thin current
    # sample. Compares against the rating BEFORE the turnover luck
    # adjustment, since that adjustment can legitimately flip a
    # borderline team's sign on purpose. (With TURNOVER_LUCK_LIVE=False
    # the adjustment is {} so this is just the live rating.)
    pre_turnover_rating = {t: r - turnover_luck_adj.get(t, 0.0) for t, r in team_ratings.items()}
    raw_diff_current = ratings.raw_point_differential(played) if len(played) else {}
    raw_diff_check = {}
    for t in team_ratings:
        if games_played.get(t, 0) >= config.MIN_GAMES_FOR_FULL_CONFIDENCE and t in raw_diff_current:
            raw_diff_check[t] = raw_diff_current[t]
        elif t in raw_diff:
            raw_diff_check[t] = raw_diff[t]
    mismatched_teams = [
        t for t in team_ratings
        if t in raw_diff_check and abs(raw_diff_check[t]) > 3 and (raw_diff_check[t] > 0) != (pre_turnover_rating[t] > 0)
    ]
    sign_mismatches = [
        f"{t}: raw point diff {raw_diff_check[t]:+.1f} but SRS rating (pre-turnover-adj) {pre_turnover_rating[t]:+.1f}"
        for t in mismatched_teams
    ]
    data._record(
        "Sign sanity check (raw point diff vs. SRS rating)",
        len(sign_mismatches) == 0,
        "No sign mismatches -- ratings agree in direction with raw scoring margin for every team."
        if not sign_mismatches else " | ".join(sign_mismatches),
    )

    # Full game-by-game trace for whichever teams rank highest/lowest, PLUS
    # every team flagged by the sign check above -- exactly the teams most
    # likely to be questioned, so the trace is there before anyone has to
    # ask for it.
    if team_ratings:
        ranked = sorted(team_ratings, key=team_ratings.get, reverse=True)
        teams_to_trace = list(dict.fromkeys(ranked[:2] + ranked[-2:] + mismatched_teams))
        # FIXED 2026-11-04: trace the CURRENT season's games when the
        # team has played this season -- tracing last season's games
        # for a rating that is now mostly current-season was misleading.
        trace_source = played if len(played) else played_prior
        trace_label = "current-season" if len(played) else "prior-season"
        for t in teams_to_trace:
            trace = ratings.debug_team_games(t, trace_source, hfa)
            lines_trace = [
                f"wk{g['week']} vs {g['opponent']} ({g['home_or_away']}"
                f"{', NEUTRAL SITE' if g['neutral_site'] else ''}): "
                f"{g['team_score']}-{g['opp_score']} (raw {g['raw_margin']:+.0f}) -> "
                f"HFA-adj {g['hfa_adjusted_margin']:+.1f}, recency weight {g['recency_weight']}"
                for g in trace
            ]
            data._record(
                f"Game trace ({trace_label}): {t} (rating {team_ratings[t]:+.1f}, raw point diff {raw_diff_check.get(t, raw_diff.get(t, 'n/a'))})",
                True,
                " || ".join(lines_trace) if lines_trace else f"No {trace_label} games found for this team.",
            )
    log.info("Empirical HFA: %.2f pts", hfa)
    if team_ratings:
        best = max(team_ratings, key=team_ratings.get)
        worst = min(team_ratings, key=team_ratings.get)
        data._record(
            "Compute team ratings", True,
            f"HFA={hfa:.2f} | {len(team_ratings)} teams rated | "
            f"best: {best} ({team_ratings[best]:+.1f}) | worst: {worst} ({team_ratings[worst]:+.1f}) | "
            f"games played this season -- min: {min(games_played.values()) if games_played else 'n/a'}, "
            f"max: {max(games_played.values()) if games_played else 'n/a'}",
        )

    inj_df = data.load_injuries(config.SEASON, week)
    depth_df = data.load_depth_charts(config.SEASON, week)
    roster_df = data.load_weekly_roster(config.SEASON, week)
    inj_review = injuries.review_injuries(inj_df, week, depth_df, roster_df)

    # Per-team reporting coverage for THIS week's games. NFL injury report
    # timing is genuinely uneven within a week: a Thursday-game team's
    # FINAL report is due the preceding Wednesday, while a Sunday-game
    # team's final report isn't due until Friday -- so refreshing Thursday
    # morning will legitimately show more for the Thursday game than for
    # Sunday games still on estimated practice participation, or nothing
    # yet. This shows exactly what's actually available right now instead
    # of leaving that as a guess.
    teams_this_week = sorted(set(upcoming["home_team"]) | set(upcoming["away_team"]))
    if inj_df is not None and not inj_df.empty and "team" in inj_df.columns:
        has_report_status = inj_df[inj_df["report_status"].astype(str).str.strip() != ""]["team"].unique()             if "report_status" in inj_df.columns else []
        practice_col = inj_df["practice_status"].astype(str).str.strip() if "practice_status" in inj_df.columns else None
        has_practice_only = inj_df[(practice_col != "") & (~inj_df["team"].isin(has_report_status))]["team"].unique()             if practice_col is not None else []
        teams_with_any_row = inj_df["team"].unique()
        no_data_teams = [t for t in teams_this_week if t not in teams_with_any_row]
        data._record(
            "Injury report coverage this week", True,
            f"{len(has_report_status)}/{len(teams_this_week)} teams have a final game-status designation "
            f"(OUT/DOUBTFUL/QUESTIONABLE) -- typically only Thursday-game teams this early in the week, "
            f"since Sunday teams' final report isn't due until Friday. "
            f"{len(has_practice_only)} more teams have practice-only data (DNP/Limited, used as an "
            f"early estimate via the fallback). {len(no_data_teams)} teams have nothing yet: "
            + (", ".join(no_data_teams) if no_data_teams else "none") + ".",
        )
    else:
        data._record(
            "Injury report coverage this week", False,
            f"No injury data at all for any of this week's {len(teams_this_week)} teams yet.",
        )

    epa_result = epa.weighted_epa_ratings(team_stats)  # team_stats already fetched above

    # Manual adjustments (batch 3, item 5): the versioned repo file
    # is the source of truth, merged over the database per team (file
    # wins where both have a team; database-only teams still apply
    # until exported, so nothing is silently dropped). Ratings
    # semantics are unchanged -- manual_adj still feeds into
    # predict_week exactly as before. A corrupt file is recorded as
    # a failed diagnostic and falls back to the database for this
    # run, rather than silently running with no adjustments.
    try:
        _adj = manual_adjustments.load_weekly_adjustments()
        manual_adj = _adj["adjustments"]
        _adj_reasons = _adj["reasons"]
        data._record("Manual adjustments source", True, _adj["source_note"])
    except ValueError as exc:
        manual_adj = db.get_manual_adjustments()
        _adj_reasons = {}
        data._record("Manual adjustments source", False,
                     f"Repo file unreadable ({exc}) -- fell back to the database "
                     f"({len(manual_adj)} team(s)) for this run. Fix the file; "
                     "do not assume the file's adjustments applied.")

    # Returning starters: the positive mirror of the injury review. Looks
    # at the last few weeks of saved injury history for starters who were
    # OUT/DOUBTFUL and aren't listed this week. Review-only, like the rest
    # of the injury system -- surfaced in the dashboard, never auto-applied.
    recent_history = db.get_recent_injury_history(config.SEASON, week)
    returns = injuries.detect_returning_players(inj_df, recent_history)
    db.save_injury_returns(config.SEASON, week, returns["detail"])
    data._record(
        "Returning starters check", True,
        f"{len(returns['detail'])} starter(s) look to have returned from a recent OUT/DOUBTFUL designation "
        f"(looked back over {len(recent_history)} injury records from prior weeks). "
        + ("; ".join(f"{d[0]} {d[1]} ({d[2]})" for d in returns["detail"]) if returns["detail"]
           else "Needs at least one prior week of saved injury data to detect anything."),
    )

    # Pool line overrides: your pool's locked lines, when they differ from
    # the live market line. These drive the edge/pick; live Vegas still shows.
    pool_lines = db.get_pool_line_overrides(config.SEASON, week)
    data._record(
        "Pool line overrides", True,
        f"{len(pool_lines)} game(s) this week use a pool line instead of the live Vegas line."
        if pool_lines else "No pool line overrides set -- all picks use the live Vegas line.",
    )
    # File adjustments carry their reason; database-only ones keep
    # the old behaviour (no per-run reason; decision_log dedupes on
    # value anyway).
    reasons = {t: _adj_reasons.get(t, "") for t in manual_adj}
    db.log_decisions(config.SEASON, week, manual_adj, reasons)

    pts_for, pts_against = scoring.blended_scoring_averages(played, played_prior)
    # SRS totals ratings (item 8, SHADOW): computed every run so flipping
    # config.TOTALS_SRS_LIVE is a one-line change, but the old blend
    # stays live until the SRS model wins walk-forward (it has not --
    # see README item 8). Totals-only; spread picks never read these.
    totals_off, totals_def, totals_league_avg = scoring.blended_srs_scoring(played, played_prior, hfa)

    predictions = model.predict_week(
        upcoming, team_ratings, games_played, hfa,
        manual_adj=manual_adj, injury_review=inj_review, epa=epa_result,
        pts_for=pts_for, pts_against=pts_against, pool_lines=pool_lines,
        totals_off=totals_off, totals_def=totals_def, totals_league_avg=totals_league_avg,
    )
    db.save_picks(config.SEASON, predictions)
    # Experiment tracking (batch 4, group D, item 10): log this
    # run's model version + flag set + blend/params + fingerprint
    # into run_experiments, so a later grade can be attributed to
    # a specific model change. Never fails the run: tracking is
    # bookkeeping, and picks are already saved at this point.
    try:
        _exp = experiment.record_run(config.SEASON, week, picks_count=len(predictions))
        data._record("Experiment tracking", True,
                     f"Recorded run fingerprint {_exp['fingerprint']} "
                     f"(version label {_exp['model_version']!r}, git {_exp['git_head'] or 'unknown'}).")
        log.info("Experiment recorded: %s", _exp["fingerprint"])
    except Exception as exc:
        log.warning("Experiment tracking failed (picks already saved, continuing): %s", exc)
        data._record("Experiment tracking", False,
                     f"Could not record this run's model settings ({exc}) -- this week's grade "
                     "cannot be attributed to a recorded model change.")
    # Line snapshots (item 7): pool line when set, closing/live line
    # always when present. The dashboard's line-moves section compares
    # these; a move never auto-switches the pool line.
    for p in predictions:
        if p.pool_spread is not None:
            db.save_line_snapshot(config.SEASON, week, p.home, p.away, "pool", p.pool_spread)
        if p.vegas_spread is not None:
            db.save_line_snapshot(config.SEASON, week, p.home, p.away, "closing", p.vegas_spread)
    db.save_injury_review(config.SEASON, week, inj_review["detail"])
    # What-changed diff vs the previous saved run for this week
    # (Tuesday's run, or Thursday 7 AM's when this is the 10 AM
    # final run). Computed from in-memory new values vs the
    # pre-save snapshot above -- the picks table no longer holds
    # the old slate once save_picks ran, so this ordering matters.
    _save_what_changed(config.SEASON, week, previous_state,
                       predictions, team_ratings, inj_review["detail"])
    db.save_model_log(config.SEASON, predictions)

    # ---- Backfill: log any already-completed games this season that
    # never got a prediction recorded (e.g. the logging feature was added
    # mid-week, or a refresh got skipped). Only fills in what's missing --
    # never overwrites a genuine pre-game prediction that's already there.
    existing_keys = db.get_model_log_keys(config.SEASON)
    already_played_this_season = played  # from split_games -- current season, has a result
    missing = already_played_this_season[
        ~already_played_this_season.apply(
            lambda r: (int(r["week"]), r["away_team"], r["home_team"]) in existing_keys, axis=1
        )
    ] if len(already_played_this_season) else already_played_this_season
    if len(missing):
        backfill_preds = model.predict_completed_games(
            missing, team_ratings, games_played, hfa, pts_for=pts_for, pts_against=pts_against,
            totals_off=totals_off, totals_def=totals_def, totals_league_avg=totals_league_avg,
        )
        db.save_model_log(config.SEASON, backfill_preds, is_backfill=True)
        data._record(
            "Backfill completed games into model_log", True,
            f"Logged {len(backfill_preds)} already-completed game(s) that were missing a prediction "
            f"(likely from before the logging feature was added). These use TODAY's ratings rather than "
            f"a true point-in-time prediction, so they are flagged is_backfill and EXCLUDED from grading, not a "
            f"rigorous backtest. Games: " + ", ".join(
                f"wk{p.week} {p.away}@{p.home}" for p in backfill_preds
            ),
        )
    else:
        data._record("Backfill completed games into model_log", True, "Nothing to backfill -- all completed games already logged.")

    from nflpicks import weather as weather_mod
    n_outdoor = sum(1 for _, r in upcoming.iterrows() if not weather_mod.is_indoor(r.get("roof")))
    n_weather_applied = sum(1 for p in predictions if p.weather_note and p.weather_note != "no significant weather effect")
    data._record(
        "Weather forecast fetch", True,
        f"{n_outdoor} outdoor games this week. Weather data changed the total for {n_weather_applied} of them. "
        f"If this is 0 across many outdoor games and you'd expect otherwise, the forecast API call may be "
        f"failing in this environment -- check for a 'weather fetch failed' warning in the logs.",
    )

    # ---- Survivor: needs the full remaining schedule, not just next week ----
    remaining = data.load_all_upcoming(games, config.SEASON)
    if len(remaining):
        near_term = remaining[remaining["week"] <= remaining["week"].min() + 2].sort_values(["week", "home_team"])
        by_week_lines = []
        for wk, grp in near_term.groupby("week"):
            matchups = ", ".join(f"{r.away_team}@{r.home_team}" for r in grp.itertuples())
            by_week_lines.append(f"Week {int(wk)} ({len(grp)} games): {matchups}")
        data._record(
            "Full schedule for next 3 weeks (survivor input)", True,
            " || ".join(by_week_lines),
        )
    data._record(
        "Load remaining schedule for survivor optimizer", True,
        f"{len(remaining)} remaining games found across weeks "
        f"{int(remaining['week'].min()) if len(remaining) else 'n/a'}-"
        f"{int(remaining['week'].max()) if len(remaining) else 'n/a'}.",
    )
    full_schedule_preds = model.predict_full_schedule(remaining, team_ratings, games_played, hfa)
    win_table = survivor.build_win_prob_table(full_schedule_preds)
    db.save_survivor_week_options(config.SEASON, win_table)
    used = db.get_survivor_used_teams(config.SEASON)

    survivor_opt = survivor.optimize_survivor(win_table, used_teams=used)
    survivor_greedy = survivor.greedy_baseline(win_table, used_teams=used)

    # Hard check: a team should never be recommended in more than one week
    # within the SAME strategy's plan -- that's the entire point of the
    # optimizer. If this ever fires, it's a real bug in survivor.py itself,
    # not a schedule data issue, and needs to be reported with this exact
    # message.
    for name, df in [("optimizer", survivor_opt), ("greedy", survivor_greedy)]:
        dupes = df["recommended_team"].value_counts()
        dupes = dupes[dupes > 1]
        if len(dupes):
            data._record(
                f"Survivor uniqueness check ({name})", False,
                f"BUG: these teams were recommended in more than one week: "
                f"{dict(dupes)} -- this should be mathematically impossible and needs to be reported.",
            )
        else:
            data._record(f"Survivor uniqueness check ({name})", True, "Every team used at most once. Correct.")

    db.save_survivor_picks(config.SEASON, survivor_opt, "optimizer")
    db.save_survivor_picks(config.SEASON, survivor_greedy, "greedy")

    # ---- Email summary ----
    lines = [f"Week {week} picks (sorted by edge):\n"]
    for p in predictions:
        flag = "*** " if p.strong_edge else ""
        conf_note = f" | conf: {p.confidence}" if p.confidence < 1.0 else ""
        inj = f" | \u26A0 {p.injury_flags}" if p.injury_flags else ""
        lines.append(f"{flag}{p.away} @ {p.home} | Pick: {p.pick} | Edge: {p.edge} pts{conf_note}{inj}")

    crit = [
        f"{team} -- {f}" for team, fl in inj_review["flags"].items() for f in fl if "CRITICAL" in f
    ]
    if crit:
        lines.append("\n=== INJURY REVIEW REQUIRED (no automatic adjustments applied) ===")
        lines.extend(crit)

    if not survivor_opt.empty:
        # FIXED 2026-11-04: iloc[0] was not necessarily this week's pick
        # if the optimizer skipped a week -- find this week's row first.
        this_week_rows = survivor_opt[survivor_opt["week"] == week]
        next_pick = this_week_rows.iloc[0] if len(this_week_rows) else survivor_opt.iloc[0]
        week_label = f"Week {int(next_pick['week'])}" if int(next_pick['week']) != week else "this week"
        lines.append(f"\nSurvivor pick {week_label}: {next_pick['recommended_team']} "
                      f"(win prob {next_pick['win_prob']}) -- see dashboard for full season plan.")

    lines.append(f"\nHFA (empirical): {hfa:.2f} pts | EPA layer: {epa_result['note']} "
                 f"{'[LIVE]' if config.EPA_LIVE else '[SHADOW]'}")
    lines.append(f"Injury data: {inj_review['note']}")

    # Email is opt-in only (SMTP_HOST unset = skipped inside send_email).
    # FIXED 2026-11-04: an SMTP failure used to abort the whole run
    # after picks were already saved, so a workflow commit step after
    # this would never run. Email must never block picks/DB.
    try:
        send_email(f"NFL Pool Picks -- Week {week}", "\n".join(lines))
    except Exception as e:
        log.warning("Email failed (picks already saved, continuing): %s", e)
    log.info("Done.")


if __name__ == "__main__":
    main()
