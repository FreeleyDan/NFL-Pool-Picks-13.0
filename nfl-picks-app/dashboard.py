"""
Run locally with:  streamlit run dashboard.py
Deploy free at:     https://share.streamlit.io  (point it at this repo)
"""
import json
import traceback

import pandas as pd
import streamlit as st

from nflpicks import adjustment_grading, config, db, error_decomposition, freshness, grading, manual_adjustments, pool_results, review_queue, run_diff, standings, submission, uncertainty
import weekly_run

st.set_page_config(page_title="NFL Pool Picks", layout="wide")
db.init_db()

header_col1, header_col2 = st.columns([4, 1])
with header_col1:
    st.title("🏈 NFL Pool Picks")
with header_col2:
    st.write("")  # small vertical spacer to align the button with the title
    if st.button("🔄 Refresh Picks Now", use_container_width=True):
        with st.spinner("Pulling latest data and recomputing picks... this takes 10-30 seconds"):
            try:
                weekly_run.main()
                st.success("Picks updated.")
            except Exception:
                st.error(
                    "Something went wrong while refreshing. Check the Data Health tab below for "
                    "details, or paste this error back to Claude:"
                )
                st.code(traceback.format_exc())
        st.rerun()

with db.connect() as conn:
    picks_df = pd.read_sql_query(
        "SELECT * FROM picks WHERE season=? ORDER BY week DESC, ABS(edge) DESC",
        conn, params=(config.SEASON,),
    )
    grades_df = pd.read_sql_query("SELECT * FROM model_grades", conn)
    injury_df = pd.read_sql_query(
        "SELECT * FROM injury_review WHERE season=? ORDER BY week DESC", conn, params=(config.SEASON,)
    )
    adj_df = pd.read_sql_query("SELECT * FROM manual_adjustments", conn)
    used_df = pd.read_sql_query(
        "SELECT team FROM survivor_used_teams WHERE season=?", conn, params=(config.SEASON,)
    )
    survivor_options_df = pd.read_sql_query(
        "SELECT * FROM survivor_week_options WHERE season=? ORDER BY week, win_prob DESC",
        conn, params=(config.SEASON,),
    )
    try:
        survivor_picks_df = pd.read_sql_query(
            "SELECT * FROM survivor_picks WHERE season=? ORDER BY strategy, week",
            conn, params=(config.SEASON,),
        )
    except Exception:
        survivor_picks_df = pd.DataFrame()
    diagnostics_df = pd.read_sql_query("SELECT * FROM run_diagnostics ORDER BY rowid", conn)
    ratings_df = pd.read_sql_query(
        "SELECT * FROM team_ratings_snapshot WHERE season=? ORDER BY week DESC, rating DESC",
        conn, params=(config.SEASON,),
    )
    returns_df = pd.read_sql_query(
        "SELECT * FROM injury_returns WHERE season=? ORDER BY week DESC", conn, params=(config.SEASON,)
    )
    pool_lines_df = pd.read_sql_query(
        "SELECT * FROM pool_line_overrides WHERE season=?", conn, params=(config.SEASON,)
    )

# ---- Freshness banner (batch-3 item 10) ----
# Shown on every load, before any slate is presented as current: if
# the saved picks are stale, missed a scheduled run, or the last run
# reported failures, say so HERE, not buried in a tab. This is the
# last run saved in this database, NOT live GitHub Actions status
# (the app has no Actions token) -- the banner says so.
try:
    _fresh = freshness.freshness_status(config.SEASON)
except Exception:
    _fresh = {"level": "stale", "message": "Could not check data freshness -- treat this slate as unverified.",
              "failed_steps": [], "latest_run": None}
if _fresh["level"] == "stale":
    st.error("⚠️ STALE PICKS -- " + _fresh["message"] +
             " Do not submit from this slate until you refresh it.")
elif _fresh["level"] == "warn":
    st.warning("⚠️ " + _fresh["message"])
    if _fresh.get("failed_steps"):
        st.warning("Failed step(s) on the last run: " + "; ".join(
            f"{s['step']}: {s['detail']}" for s in _fresh["failed_steps"]))

tab_names = ["ATS Picks", "Submit & Review", "Survivor Pool", "Injury Review", "Model Grades", "Manual Adjustments", "Data Health"]
if picks_df.empty:
    st.info("No picks yet -- click \"🔄 Refresh Picks Now\" above to pull data and generate this week's picks. "
            "If it stays empty after refreshing, check the Data Health tab below.")
    tabs = st.tabs(tab_names)
    with tabs[-1]:
        st.subheader("Data Health")
        if diagnostics_df.empty:
            st.write("No diagnostics recorded yet -- click Refresh above first.")
        else:
            for _, row in diagnostics_df.iterrows():
                icon = "✅" if row["ok"] else "❌"
                st.write(f"{icon} **{row['step']}**")
                st.caption(row["detail"])
    st.stop()

latest_week = int(picks_df["week"].max())
tab_ats, tab_submit, tab_survivor, tab_injuries, tab_grades, tab_adjust, tab_health = st.tabs(tab_names)

# ---- ATS Picks ----
with tab_ats:
    st.subheader(f"Week {latest_week} — Against the Spread")
    # Item 10: the slate's as-of date, right next to the picks it
    # describes, so a week-old slate can never read as today's.
    _run_times = picks_df[picks_df["week"] == latest_week]["run_time"].dropna() \
        if "run_time" in picks_df.columns else pd.Series([], dtype=str)
    if not _run_times.empty:
        st.caption(f"Picks as of: {_run_times.max()} (UTC, from the last saved run). "
                   "If that date is not this week, refresh before submitting.")
    wk_df = picks_df[picks_df["week"] == latest_week].copy()

    def _flag(row):
        if row.get("needs_review"):
            return "⚠️ REVIEW"
        if row.get("strong_edge"):
            return "🔥"
        return ""

    wk_df["flag"] = wk_df.apply(_flag, axis=1)
    # All spreads in this table stay in nflverse convention
    # (+ = home favored) so Vegas, Pool, and Model columns are
    # directly comparable. The pool-line FORM below is the exception:
    # it accepts sportsbook convention (- = home favored, as printed
    # by a pool sheet / sportsbook) and converts on save -- see
    # config.to_nflverse_spread. Do not mix conventions in this table.
    show_cols = ["away_team", "home_team", "vegas_spread", "pool_spread", "model_independent", "model_spread",
                  "edge", "pick", "confidence", "flag", "injury_flags",
                  "vegas_total", "pool_total", "model_total", "total_pick", "weather_note"]
    if "cover_prob" in wk_df.columns:
        show_cols.insert(10, "cover_prob")
    # Item 7 -- cover-probability range under rating uncertainty:
    # the same calibrated CDF evaluated at the model margin shifted
    # by +/-1 margin SD (rating bands from the latest ratings
    # snapshot's games-played counts). A range straddling 0.50
    # means rating uncertainty alone could flip which side is
    # favoured -- treat that pick as a coin flip. This column is
    # best-effort: any missing input skips it rather than breaking
    # the picks table.
    _cover_range = None
    try:
        _wk_ratings = ratings_df[ratings_df["week"] == latest_week] if not ratings_df.empty else ratings_df
        _games_played = {r.team: int(r.games_played_this_season)
                         for r in _wk_ratings.itertuples()
                         if "games_played_this_season" in _wk_ratings.columns}
        if _games_played and "model_independent" in wk_df.columns:
            def _range_for(row):
                _line = row["pool_spread"] if pd.notna(row.get("pool_spread")) else row.get("vegas_spread")
                if pd.isna(row.get("model_independent")) or _line is None or pd.isna(_line):
                    return ""
                _lo, _hi = uncertainty.cover_prob_interval(
                    float(row["model_independent"]), float(_line),
                    _games_played.get(row["home_team"], 0),
                    _games_played.get(row["away_team"], 0))
                return f"{_lo:.2f}-{_hi:.2f}"
            _cover_range = wk_df.apply(_range_for, axis=1)
    except Exception:
        _cover_range = None
    if _cover_range is not None:
        wk_df["cover_prob_range"] = _cover_range
        show_cols.insert(show_cols.index("cover_prob") + 1 if "cover_prob" in show_cols else 10,
                         "cover_prob_range")
    show = wk_df[show_cols]
    show.columns = ["Away", "Home", "Vegas Spread", "Pool Spread", "Model Spread (raw)", "Model Spread (official)",
                     "Edge", "Pick", "Confidence", "Flag"] + (["Cover Prob"] if "cover_prob" in wk_df.columns else []) + (["Cover Prob Range"] if "cover_prob_range" in show_cols else []) + [
                     "Injury Flags",
                     "Vegas Total", "Pool Total", "Model Total", "Total Pick", "Weather"]
    st.dataframe(show, use_container_width=True, hide_index=True)
    st.caption(
        "\"Model Spread (raw)\" is the rating engine's independent read, before it's blended toward the "
        "market line. \"Model Spread (official)\" is what actually drives the pick -- it's damped toward "
        "the line based on confidence, since an efficient market is hard to beat independently (see "
        "README). ⚠️ REVIEW means the two disagree by more than a market miss would realistically "
        "explain -- treat that as a signal to double-check inputs, not a signal to bet bigger. "
        "Confidence < 1.0 means one or both teams still have a small current-season sample. Weather "
        "only affects the Total (dome games are blank) -- it's not applied to the spread, since the "
        "research support for that is much weaker than for total scoring. \"Pool Spread\"/\"Pool Total\" "
        "are blank unless you set an override below -- when set, THAT line (not live Vegas) drives the "
        "Edge and Pick, since that's what you're actually judged against. "
        "All spreads in the table above are nflverse convention (+ = home favored). "
        "Cover Prob is the model's estimated probability the pick covers, "
        "from the raw model-vs-line gap -- near 50% means a coin flip, "
        "which matters when the same picks count for both a weekly winner "
        "and the season-long winner (see README). Cover Prob Range is that "
        "same probability under a +/-1 SD shift of the model margin from "
        "rating uncertainty (item 7): a range straddling 0.50 means the "
        "rating bands alone could flip the favoured side, so treat the "
        "pick as a coin flip even if the point probability looks strong. "
        "Bands are model uncertainty, not a guarantee."
    )

    # Line-movement review (backtest item 7, added 2026-11-04): games
    # where the pool line and the closing/live line moved >=1 pt or the
    # model edge flips between them. This is a manual Thursday-morning
    # check only -- the pool line is NEVER auto-switched, since it is
    # what the pool is judged against. Snapshots are written by
    # weekly_run.py (pool) and by picks saves (closing); until both
    # exist for a week this section is simply empty, not an error.
    try:
        from nflpicks import db as _db
        _moves = _db.get_line_moves(config.SEASON, latest_week)
    except Exception:
        _moves = []
    if _moves:
        st.divider()
        st.markdown("**⚠️ Line moves to review Thursday morning**")
        st.caption(
            "Pool line vs closing/live line moved >= 1 pt, or the model edge "
            "flips sign between them. Check for injury/news the market "
            "learned since the pool line locked -- do NOT auto-switch: "
            "the pool line is what you are judged against.")
        st.dataframe(_moves, use_container_width=True, hide_index=True)

    st.divider()
    st.markdown("**Set a pool line** (when your pool locked a different line than the live one above)")
    st.caption("Enter the spread EXACTLY as your pool sheet / sportsbook prints it: "
               "negative if the HOME team is favored (e.g. -3.5), positive if the AWAY team is favored. "
               "This form converts it to the app's internal nflverse convention on save -- "
               "entering it the other way used to silently flip the pick. "
               "Leave a field blank to use the live Vegas line for that number. "
               "Overrides saved before Nov 4, 2026 were stored unconverted, so re-enter any old override.")
    wk_games = wk_df[["away_team", "home_team"]].copy()
    wk_games["matchup"] = wk_games["away_team"] + " @ " + wk_games["home_team"]
    with st.form("pool_line_form"):
        matchup = st.selectbox("Game", wk_games["matchup"])
        c1, c2 = st.columns(2)
        # value=None renders an empty, fully-editable field and returns None
        # if left blank -- avoids the checkbox+disabled trick above, which
        # doesn't work inside a form (form widgets don't rerun live, so a
        # checkbox can never actually unlock another widget before submit).
        pool_spread_in = c1.number_input("Pool Spread, sportsbook style (home team: - = home favored)", value=None, step=0.5, format="%.1f")
        pool_total_in = c2.number_input("Pool Total", value=None, step=0.5, format="%.1f")
        col_save, col_clear = st.columns(2)
        save_clicked = col_save.form_submit_button("Save pool line")
        clear_clicked = col_clear.form_submit_button("Clear override (use live Vegas)")
        if save_clicked or clear_clicked:
            away, home = wk_games.loc[wk_games["matchup"] == matchup, ["away_team", "home_team"]].iloc[0]
            if clear_clicked:
                db.clear_pool_line_override(config.SEASON, latest_week, home, away)
                st.success(f"Cleared pool line for {matchup} -- back to live Vegas.")
                st.rerun()
            elif pool_spread_in is None and pool_total_in is None:
                st.warning("Enter a Pool Spread and/or Pool Total first, or use Clear instead.")
            else:
                # Convert sportsbook -> nflverse on save. DB stores nflverse.
                pool_spread_db = config.to_nflverse_spread(pool_spread_in) if pool_spread_in is not None else None
                db.set_pool_line_override(config.SEASON, latest_week, home, away, pool_spread_db, pool_total_in)
                st.success(f"Saved pool line for {matchup}. Click Refresh Picks Now to recompute the edge/pick with it.")
                st.rerun()
    if not pool_lines_df.empty:
        # Show overrides back in sportsbook style, as the user entered them.
        st.caption("Active pool line overrides (sportsbook style): " + ", ".join(
            f"{r.away_team}@{r.home_team} (spread {config.to_sportsbook_spread(r.pool_spread) if pd.notna(r.pool_spread) else r.pool_spread}, total {r.pool_total})"
            for r in pool_lines_df.itertuples()
        ))

# ---- Submit & Review (items 3 + 7b + standings wiring) ----
with tab_submit:
    st.subheader(f"Week {latest_week} — Submit & Thursday Review")
    wk_submit = picks_df[picks_df["week"] == latest_week].copy()

    # 7b -- workflow run status, from data the app can actually read.
    # This is the last run saved in THIS database (picks.run_time +
    # run_diagnostics), NOT a live GitHub Actions status: the Actions
    # API needs a token the app does not have, so no live status is
    # claimed. For live status, check the Actions tab on GitHub.
    st.markdown("**Last run (from this database, not live Actions status)**")
    run_times = wk_submit["run_time"].dropna() if "run_time" in wk_submit.columns else pd.Series([], dtype=str)
    if not run_times.empty:
        st.write(f"Latest week {latest_week} picks saved at: {run_times.max()} "
                 f"({len(wk_submit)} games). If this timestamp is older than the last "
                 "Tuesday/Thursday 15:00 UTC workflow run, the workflow may have failed -- "
                 "check the Actions tab on GitHub (a failing unit test now fails the job "
                 "before any pick is computed).")
    else:
        st.write("No run timestamp recorded for this week yet.")
    if not diagnostics_df.empty:
        failed = diagnostics_df[diagnostics_df["ok"] == 0]
        if not failed.empty:
            st.warning("Last run reported failures: " + "; ".join(
                f"{r.step}: {r.detail}" for r in failed.itertuples()))
        else:
            st.caption("Last run diagnostics: all steps OK (see Data Health for detail).")

    # 3a -- submission sheet.
    st.divider()
    st.markdown("**Submission sheet — copy-paste for Barbara's pool email**")
    st.caption("Picks are due Thursday 11:00 AM PT by replying to Barbara's email. "
               "Lines below are in sportsbook notation, exactly as a pool sheet prints them "
               "(home favorite = negative), derived from the stored pool line where set, "
               "else the live Vegas line -- and double-checked against the model's own pick "
               "string, so a flipped sign shows up as a MISMATCH instead of a silent error.")
    sub_inputs = []
    for r in wk_submit.itertuples():
        pool_sp = r.pool_spread if pd.notna(getattr(r, "pool_spread", None)) else None
        veg_sp = r.vegas_spread if pd.notna(getattr(r, "vegas_spread", None)) else None
        sub_inputs.append({"home": r.home_team, "away": r.away_team, "pick": r.pick,
                           "pool_spread": pool_sp, "vegas_spread": veg_sp})
    sheet = submission.build_submission_sheet(sub_inputs, latest_week)
    st.code(sheet["text"])
    st.markdown("**Checklist — do not send until every check passes (except the manual one)**")
    for c in sheet["checklist"]:
        icon = {"pass": "✅", "fail": "❌", "manual": "✏️"}.get(c["status"], "")
        st.write(f"{icon} **{c['check']}** — {c['detail']}")
    mismatches = [e for e in sheet["entries"] if e["sign_check"] == "MISMATCH"]
    if mismatches:
        st.error("Sign MISMATCH on: " + ", ".join(
            f"{e['matchup']} (derived {e['pick_text']}, model {e['model_pick']})" for e in mismatches)
        + " -- do not send until this is resolved.")

    # 3b -- Thursday review queue: flagged games only, P1 first.
    st.divider()
    st.markdown("**Thursday review queue — flagged games only**")
    try:
        queue = review_queue.build_review_queue(config.SEASON, latest_week)
    except Exception:
        queue = {"items": [], "note": "Could not build the review queue from this database."}
        st.error("Could not build the review queue -- see the error below, or check the ATS tab's line-moves section.")
        st.code(traceback.format_exc())
    st.caption(queue["note"])
    if queue["items"]:
        q_rows = [{"Priority": f"P{i['priority']}", "Matchup": i["matchup"], "Pick": i["pick"],
                   "Model vs line (pts)": i["raw_deviation"], "Line move (pts)": i["line_movement"],
                   "Why flagged": " | ".join(i["reasons"])} for i in queue["items"]]
        st.dataframe(q_rows, hide_index=True, use_container_width=True)
    else:
        st.write("No games flagged this week -- nothing in the queue means nothing met a flag "
                 "threshold, not that the data was checked and cleared.")

    # Item 6 -- what changed since the last run (Thursday 10 AM
    # final run vs 7 AM, or Thursday 7 AM vs Tuesday). The diff
    # survives because weekly_run persists it under its own
    # run_time before overwriting the picks; this section reads
    # only the latest run's rows, flips first. A change is a
    # prompt to re-check, never an auto-switch of the pool line.
    st.divider()
    st.markdown("**What changed since the last run — movers only**")
    try:
        _diff = db.get_latest_run_diff(config.SEASON, latest_week)
    except Exception:
        _diff = {"run_time": None, "items": []}
        st.error("Could not load the what-changed diff -- see the ATS tab's line-moves section instead.")
        st.code(traceback.format_exc())
    if _diff["run_time"] is None:
        st.write("No what-changed diff saved yet for this week -- the next weekly run "
                 "(including the Thursday 10 AM final run) saves one automatically. "
                 "The first run of a week has nothing to compare against by design.")
    elif not _diff["items"]:
        st.write(f"Latest diff ({_diff['run_time']}, UTC): no changes above the reporting "
                 "thresholds since the previous run -- the slate is unchanged where it matters.")
    else:
        st.caption(f"Latest diff run: {_diff['run_time']} (UTC). Flips first, then line moves, "
                   "injury changes, edge moves, rating moves, weather, and totals. "
                   "A row here means re-check that game before submitting -- it does not change a pick or a line.")
        _d_rows = [{"Category": i["category"], "Matchup": i["matchup"], "What changed": i["detail"]}
                   for i in _diff["items"]]
        st.dataframe(_d_rows, hide_index=True, use_container_width=True)

    # Standings wiring (batch 3 API, previously unwired): labels only.
    # The default picks are unchanged by anything here; a GAMBLE label
    # is a suggestion to flip, not a changed pick.
    st.divider()
    st.markdown("**Standings check — late-season gamble labels (label-only)**")
    st.caption("Enter your actual season ATS wins and the leader's, plus weeks left INCLUDING "
               "this week (final week = 1). Early in the season (5+ weeks left) the rule is "
               "off no matter the standings. Full-season simulation costing "
               "(simulation.compare_gambles) is NOT run here: it needs cover probabilities "
               "for the whole remaining schedule, and this database stores only this week's "
               "picks -- see README for where that wiring goes. The expected-win cost shown "
               "with each GAMBLE comes from standings.suggest_gambles itself.")
    with st.form("standings_form"):
        sc1, sc2, sc3 = st.columns(3)
        user_wins_in = sc1.number_input("Your season ATS wins", value=0, min_value=0, step=1)
        leader_wins_in = sc2.number_input("Leader's season ATS wins", value=0, min_value=0, step=1)
        weeks_left_in = sc3.number_input("Weeks left (incl. this week)", value=4, min_value=0, step=1)
        standings_clicked = st.form_submit_button("Check standings labels")
    if standings_clicked:
        s_games, skipped = [], []
        for r in wk_submit.itertuples():
            if pd.isna(getattr(r, "cover_prob", None)):
                skipped.append(f"{r.away_team}@{r.home_team} (no cover prob)")
                continue
            pick_team = submission.pick_team_from_pick_string(r.pick, r.home_team, r.away_team)
            if pick_team is None:
                skipped.append(f"{r.away_team}@{r.home_team} (no pick)")
                continue
            s_games.append({"home": r.home_team, "away": r.away_team,
                            "default_side": "home" if pick_team == r.home_team else "away",
                            "pick_prob": float(r.cover_prob)})
        try:
            s_out = standings.suggest_gambles(s_games, int(user_wins_in), int(leader_wins_in),
                                              int(weeks_left_in))
            st.write(s_out["note"])
            s_rows = [{"Matchup": f"{g['away']} @ {g['home']}", "Default": g["default_side"],
                       "Pick prob": g["pick_prob"], "Label": g["label"],
                       "Gamble side": g["gamble_side"], "Expected cost (wins)": g["expected_cost"],
                       "Reason": g["reason"]} for g in s_out["games"]]
            st.dataframe(s_rows, hide_index=True, use_container_width=True)
            if skipped:
                st.caption("Skipped (no cover prob or no pick): " + "; ".join(skipped))
        except Exception:
            st.error("Standings check failed -- check the inputs (all must be non-negative).")
            st.code(traceback.format_exc())

# ---- Survivor ----
with tab_survivor:
    st.subheader("Survivor Pool — This Week's Options")
    used_teams = set(used_df["team"]) if not used_df.empty else set()

    if survivor_options_df.empty:
        st.write("No survivor data yet -- click \"🔄 Refresh Picks Now\" above.")
    else:
        this_week = int(survivor_options_df["week"].min())
        wk_options = survivor_options_df[survivor_options_df["week"] == this_week].copy()
        available = wk_options[~wk_options["team"].isin(used_teams)].sort_values("win_prob", ascending=False)

        st.caption(
            f"Week {this_week} — every team playing this week, ranked by win probability. Teams you've "
            f"already used are hidden automatically. Derived from the same ratings as the ATS tab -- "
            f"check that tab for any ⚠️ REVIEW flags before picking."
        )
        show = available[["team", "opponent", "is_home", "win_prob"]].copy()
        show["is_home"] = show["is_home"].map({1: "home", 0: "away"})
        show["win_prob"] = (show["win_prob"] * 100).round(1).astype(str) + "%"
        show.columns = ["Team", "Opponent", "Home/Away", "Win Prob"]
        st.dataframe(show, hide_index=True, use_container_width=True)

        if used_teams:
            st.caption(f"Hidden (already used): {', '.join(sorted(used_teams))}")

        with st.expander("Why the top pick here might differ from a season-long optimizer"):
            st.write(
                "This list ranks purely by THIS week's win probability. A season-long optimizer might "
                "sometimes recommend a slightly lower-probability team this week in order to save a "
                "stronger team for a tougher matchup later -- this view intentionally doesn't do that "
                "trade-off for you, since you said you'd rather pick from the full list yourself."
            )

    # FIXED 2026-11-04: the optimizer's full-season plan was computed
    # and saved to survivor_picks every run, but this tab never read
    # that table -- the plan was invisible in the UI.
    if not survivor_picks_df.empty:
        st.divider()
        st.markdown("**Season-long optimizer plan** (maximizes survival probability across all remaining weeks)")
        for strategy in ["optimizer", "greedy"]:
            plan = survivor_picks_df[survivor_picks_df["strategy"] == strategy]
            if plan.empty:
                continue
            st.caption(f"Strategy: {strategy}")
            plan_show = plan[["week", "recommended_team", "opponent", "is_home", "win_prob", "survival_prob_to_here"]].copy()
            plan_show["is_home"] = plan_show["is_home"].map({1: "home", 0: "away"})
            plan_show.columns = ["Week", "Team", "Opponent", "Home/Away", "Win Prob", "Survival Prob To Here"]
            st.dataframe(plan_show, hide_index=True, use_container_width=True)

    st.divider()
    st.markdown("**Mark a team as used** (once you've actually made that week's pick)")
    new_used = st.text_input("Team abbreviation (e.g. KC)")
    if st.button("Mark used") and new_used:
        db.mark_survivor_team_used(config.SEASON, new_used.strip().upper())
        st.rerun()
    if used_teams:
        st.write("Already used this season:", ", ".join(sorted(used_teams)))
        remove_used = st.text_input("Remove a team from the used list (typo fix)")
        if st.button("Remove from used list") and remove_used:
            with db.connect() as conn:
                conn.execute("DELETE FROM survivor_used_teams WHERE season=? AND team=?",
                             (config.SEASON, remove_used.strip().upper()))
                conn.commit()
            st.rerun()
    else:
        st.write("Already used this season: none yet")

# ---- Injuries ----
with tab_injuries:
    st.subheader(f"Week {latest_week} — Injury Review (no automatic point shifts)")
    wk_inj = injury_df[injury_df["week"] == latest_week]
    if wk_inj.empty:
        st.write("No injury designations logged for this week yet.")
        st.caption("If you expect there to be one (e.g. a game is coming up and reports are usually "
                    "out by Wednesday-Friday), check the Data Health tab for the actual fetch result "
                    "rather than assuming this means \"no injuries.\"")
    else:
        st.caption(
            "\"Depth\" shows starter vs. backup from the depth chart -- a questionable backup barely "
            "moves the suggested impact, where a questionable starter does. Check \"Apply\" on any row "
            "you agree with and click the button below to send it straight to Manual Adjustments -- "
            "still nothing moves automatically until you do that."
        )
        edit_cols = ["team", "player", "position", "status", "depth_status", "impact", "flag_note"]
        edit_cols = [c for c in edit_cols if c in wk_inj.columns]
        editable = wk_inj[edit_cols].copy()
        editable.columns = [c.replace("depth_status", "Depth").replace("impact", "Adjustment (pts)")
                             .replace("flag_note", "Note").title()
                             for c in editable.columns]
        editable.insert(0, "Apply", False)
        edited = st.data_editor(
            editable, hide_index=True, use_container_width=True, key="injury_editor",
            column_config={
                "Apply": st.column_config.CheckboxColumn(default=False),
                "Adjustment (Pts)": st.column_config.NumberColumn(step=0.5),
            },
            disabled=["Team", "Player", "Position", "Status", "Depth", "Note"],
        )
        if st.button("Apply Checked Adjustments"):
            checked = edited[edited["Apply"]]
            if checked.empty:
                st.warning("No rows checked.")
            else:
                for team, grp in checked.groupby("Team"):
                    total_pts = round(grp["Adjustment (Pts)"].sum(), 1)
                    players = ", ".join(grp["Player"].astype(str))
                    db.set_manual_adjustment(team, total_pts, f"Injury review: {players}")
                st.success(f"Applied adjustments for {checked['Team'].nunique()} team(s) -- see Manual "
                           f"Adjustments tab. Note: this REPLACES any existing manual adjustment for "
                           f"those teams, it doesn't add on top of one.")
                st.rerun()

    st.caption(
        "QB values are grounded in real oddsmaker research (top starters run 6.5-7 points ATS vs. their "
        "backup; MAX_SUGGESTED_INJ_ADJ caps at 7 to match). Every other position is a much rougher "
        "estimate -- research suggests only genuinely elite skill players meaningfully move a spread, "
        "so these numbers are closer to an upper bound for a true WR1/RB1-caliber player than a "
        "reliable estimate for a role player. Use your own judgment on who the player actually is."
    )

    st.divider()
    st.subheader("Returning Starters")
    st.caption(
        "The positive mirror of the injury list above -- a starter who was OUT/DOUBTFUL recently and "
        "isn't listed at all this week. Their recent games (and the rating built from them) happened "
        "WITHOUT this player, so a return is a real signal the rating hasn't caught up to yet. Same "
        "rules as everything else here: review-only, capped, needs at least one prior week of saved "
        "data to detect anything."
    )
    wk_returns = returns_df[returns_df["week"] == latest_week] if not returns_df.empty else returns_df
    if wk_returns.empty:
        st.write("No returning starters detected for this week.")
    else:
        ret_cols = ["team", "player", "position", "note", "suggested_pts"]
        ret_cols = [c for c in ret_cols if c in wk_returns.columns]
        ret_editable = wk_returns[ret_cols].copy()
        ret_editable.columns = [c.replace("note", "Note").replace("suggested_pts", "Adjustment (pts)").title()
                                 for c in ret_editable.columns]
        ret_editable.insert(0, "Apply", False)
        ret_edited = st.data_editor(
            ret_editable, hide_index=True, use_container_width=True, key="returns_editor",
            column_config={
                "Apply": st.column_config.CheckboxColumn(default=False),
                "Adjustment (Pts)": st.column_config.NumberColumn(step=0.5),
            },
            disabled=["Team", "Player", "Position", "Note"],
        )
        if st.button("Apply Checked Returns"):
            checked = ret_edited[ret_edited["Apply"]]
            if checked.empty:
                st.warning("No rows checked.")
            else:
                for team, grp in checked.groupby("Team"):
                    total_pts = round(grp["Adjustment (Pts)"].sum(), 1)
                    players = ", ".join(grp["Player"].astype(str))
                    db.set_manual_adjustment(team, total_pts, f"Returning starter: {players}")
                st.success(f"Applied return bonus for {checked['Team'].nunique()} team(s) -- see Manual "
                           f"Adjustments tab. Note: this REPLACES any existing manual adjustment for "
                           f"those teams.")
                st.rerun()

# ---- Model grades ----
with tab_grades:
    st.subheader("Walk-Forward Model Grades")
    st.caption(
        "Only promote the EPA layer to live (config.EPA_LIVE) if its MAE beats variant B "
        "and/or its ATS% clears 52.4% (break-even at standard -110 juice) across a real sample."
    )
    if st.button("📊 Grade Completed Games Now"):
        with st.spinner("Pulling actual results and grading last week's predictions..."):
            try:
                grading.grade_and_summarize()
                st.success("Grading updated.")
            except Exception:
                st.error("Something went wrong while grading. Error detail:")
                st.code(traceback.format_exc())
        st.rerun()
    if grades_df.empty:
        st.write("No graded games yet -- click the button above once some of this week's games have finished.")
    else:
        st.dataframe(grades_df, hide_index=True, use_container_width=True)

    # 7c -- manual adjustments graded separately from model picks.
    st.divider()
    st.markdown("**Manual adjustments vs model picks**")
    st.caption(
        "\"Adjusted\" means a manual adjustment was active for that game at pick time -- "
        "it does NOT mean the adjustment flipped the pick (that would need a counterfactual "
        "re-run without adjustments). Pushes are excluded from ATS%, same as model grades. "
        "A small sample (one week) proves nothing; judge overrides over a season.")
    saved_adj = adjustment_grading.load_adjustment_grades(config.SEASON)
    if saved_adj:
        st.dataframe(saved_adj, hide_index=True, use_container_width=True)
    if st.button("📊 Grade Manual Adjustments Now"):
        with st.spinner("Pulling actual results and grading adjusted vs unadjusted games..."):
            try:
                from nflpicks import data as _data
                _games = _data.load_games(config.SEASON)
                _results = {(int(r["season"]), int(r["week"]), r["away_team"], r["home_team"]): float(r["result"])
                            for _, r in _games[_games["result"].notna()].iterrows()}
                _summary = adjustment_grading.grade_season_from_db(config.SEASON, _results)
                adjustment_grading.save_adjustment_grades(_summary)
                st.success(f"Adjusted: {_summary['adjusted']['record']} "
                           f"({_summary['adjusted']['ats_pct']}% ATS, {_summary['adjusted']['games']} games, "
                           f"{_summary['adjusted']['pushes']} pushes) | Unadjusted: "
                           f"{_summary['unadjusted']['record']} ({_summary['unadjusted']['ats_pct']}% ATS, "
                           f"{_summary['unadjusted']['games']} games, {_summary['unadjusted']['pushes']} pushes) | "
                           f"Ungraded (no result yet): {_summary['ungraded']}.")
            except Exception:
                st.error("Something went wrong while grading adjustments. Error detail:")
                st.code(traceback.format_exc())
        st.rerun()

    # Item 9 -- actual pool results vs model picks. DISTINCT from the
    # adjustment grading above: this compares the slate actually
    # submitted to Barbara's pool against the model slate, game by
    # game, and shows where differing picks helped or hurt. With no
    # imported results it shows an empty state, not a 0-0 record --
    # nothing has been submitted/graded yet, and no result is invented.
    st.divider()
    st.markdown("**Actual pool results vs model picks**")
    st.caption(
        "Import the picks you actually submitted (CSV, or one game at a time below) "
        "and grade them against the model picks on the same games and the same judged "
        "line (pool line where set, else live Vegas). 'Same' games count for both "
        "slates; only differing games move the net difference. Judge over a season, "
        "not one week.")
    _submitted = pool_results.load_pool_results(config.SEASON)
    if not _submitted:
        st.info("No actual pool results imported yet for this season -- nothing to grade. "
                "Import a CSV below, or add games manually, after you submit a week.")
    else:
        st.write(f"{len(_submitted)} submitted game(s) imported "
                 f"(weeks {min(r['week'] for r in _submitted)}-"
                 f"{max(r['week'] for r in _submitted)}).")
    with st.expander("CSV format for actual pool results"):
        st.code("season,week,away_team,home_team,submitted_team,note\n"
                "2026,1,DEN,KC,KC,flipped on Thursday review\n"
                "2026,1,BUF,MIA,MIA,\n",
                language="csv")
        st.caption("submitted_team is the team you actually submitted. You may give "
                   "submitted_pick instead (a pick string like 'KC -3.5'); a submitted "
                   "team that plays in neither team of its matchup is rejected, not guessed. "
                   "Re-importing a corrected CSV replaces that game's pick; it never duplicates it.")
        st.download_button("Download a sample CSV (SAMPLE ONLY -- not real results)",
                           data=pool_results.SAMPLE_CSV,
                           file_name="pool_results_sample.csv", mime="text/csv")
    _uploaded = st.file_uploader("Upload actual pool results CSV", type=["csv"], key="pool_results_csv")
    if _uploaded is not None and st.button("Import uploaded CSV"):
        try:
            _text = _uploaded.read().decode("utf-8")
            _imp = pool_results.import_pool_results_csv(_text)
            if _imp["errors"]:
                st.warning("Import notes / rejected rows: " + " | ".join(_imp["errors"]))
            st.success(f"Imported {_imp['imported']} submitted game(s).")
        except Exception:
            st.error("Could not import that CSV. Error detail:")
            st.code(traceback.format_exc())
        st.rerun()
    with st.form("pool_result_manual_form"):
        st.caption("Add one submitted game manually:")
        _m_week = st.number_input("Week", value=latest_week, min_value=1, step=1)
        _week_picks = picks_df[picks_df["week"] == int(_m_week)]
        if _week_picks.empty:
            st.caption("No model picks saved for that week yet -- add the game after a run saves them.")
            _m_submit = st.form_submit_button("Save submitted pick")
        else:
            _matchups = [f"{r.away_team} @ {r.home_team}" for r in _week_picks.itertuples()]
            _m_matchup = st.selectbox("Game", _matchups)
            _m_away, _m_home = _m_matchup.split(" @ ")
            _m_team = st.selectbox("Team actually submitted", [_m_away, _m_home])
            _m_note = st.text_input("Note (optional, e.g. flipped on Thursday review)")
            _m_submit = st.form_submit_button("Save submitted pick")
            if _m_submit:
                pool_results.save_pool_results([{"season": config.SEASON, "week": int(_m_week),
                                                 "away_team": _m_away, "home_team": _m_home,
                                                 "submitted_team": _m_team, "note": _m_note}])
                st.success(f"Saved submitted pick {_m_team} for {_m_matchup} (week {int(_m_week)}).")
                st.rerun()
    if _submitted and st.button("📊 Grade Submitted vs Model Now"):
        with st.spinner("Pulling actual results and grading submitted vs model picks..."):
            try:
                from nflpicks import data as _data2
                _games2 = _data2.load_games(config.SEASON)
                _results2 = {(int(r["season"]), int(r["week"]), r["away_team"], r["home_team"]): float(r["result"])
                             for _, r in _games2[_games2["result"].notna()].iterrows()}
                _pool = pool_results.grade_season_from_db(config.SEASON, _results2)
                if _pool["status"] == "empty":
                    st.info(_pool["note"])
                else:
                    st.success(f"Model: {_pool['model']['record']} ({_pool['model']['ats_pct']}% ATS) | "
                               f"Submitted: {_pool['submitted']['record']} ({_pool['submitted']['ats_pct']}% ATS) | "
                               f"{_pool['net_note']}")
                    _diff_rows = [g for g in _pool["per_game"] if g["outcome"] != "same"]
                    if _diff_rows:
                        st.markdown("**Games where the submitted pick differed from the model pick**")
                        st.dataframe(_diff_rows, hide_index=True, use_container_width=True)
                    else:
                        st.write("No differing games graded yet -- every graded submitted pick matched the model pick.")
                    if _pool["ungraded"]["no_result"] or _pool["ungraded"]["no_model_pick"]:
                        st.caption(f"Ungraded: {_pool['ungraded']} (games with no result or no model pick yet).")
            except Exception:
                st.error("Something went wrong while grading submitted picks. Error detail:")
                st.code(traceback.format_exc())

    # Item 3 -- error decomposition of this season's graded picks.
    # Same slice logic as the backtest harness (--decompose): ATS%/MAE
    # by spread, fav/dog, side, season phase, team, total, weather, and
    # division, always with sample sizes. Slices below 30 decided games
    # are flagged small -- a losing slice is a target for a future
    # walk-forward fix, not a reason to flip picks in that slice.
    st.divider()
    st.markdown("**Error decomposition — where this season's model loses**")
    st.caption(
        "Uses this season's saved picks against actual results (same judged line as the "
        "gradings above). MAE uses the model's raw spread where available. Total / "
        "division / wind context comes from the schedule file when it has it; games "
        "without it land in 'unknown' buckets, and the coverage counts say how many.")
    if st.button("📊 Decompose This Season's Errors"):
        with st.spinner("Pulling actual results and decomposing graded picks..."):
            try:
                from nflpicks import data as _data3
                _games3 = _data3.load_games(config.SEASON)
                _results3 = {(int(r["season"]), int(r["week"]), r["away_team"], r["home_team"]): float(r["result"])
                             for _, r in _games3[_games3["result"].notna()].iterrows()}
                with db.connect() as _conn:
                    _pick_rows = [dict(r) for r in _conn.execute(
                        "SELECT * FROM picks WHERE season=?", (config.SEASON,)).fetchall()]
                _rows = error_decomposition.normalize_pick_rows(_pick_rows, _results3)
                _rows = error_decomposition.attach_context(_rows, _games3)
                _dec = error_decomposition.decompose(_rows)
                if _dec["status"] == "empty":
                    st.info(_dec["note"])
                else:
                    _ov = _dec["overall"]
                    st.write(f"Overall: {_ov['wins']}-{_ov['losses']} ATS "
                             f"({_ov['ats_pct']}% on {_ov['decided']} decided games, "
                             f"{_ov['pushes']} pushes), MAE {_ov['mae']} vs line MAE {_ov['line_mae']} "
                             f"(gap {_ov['mae_gap']:+} pts). Coverage: total known "
                             f"{_dec['coverage']['total_known']}/{_dec['n']}, division known "
                             f"{_dec['coverage']['div_known']}/{_dec['n']}, wind known "
                             f"{_dec['coverage']['wind_known']}/{_dec['n']}.")
                    if _dec["worst_slices"]:
                        st.markdown("**Worst qualified slices (30+ decided games only)**")
                        st.dataframe(_dec["worst_slices"], hide_index=True, use_container_width=True)
                    for _dim, _table in _dec["slices"].items():
                        st.markdown(f"**By {_dim}**")
                        st.dataframe([{"slice": k, **v} for k, v in _table.items()],
                                     hide_index=True, use_container_width=True)
                    st.caption(_dec["note"])
            except Exception:
                st.error("Something went wrong while decomposing errors. Error detail:")
                st.code(traceback.format_exc())

# ---- Manual adjustments ----
with tab_adjust:
    st.subheader("Manual Adjustments")
    st.caption("Your call on injuries, weather, or anything else the model doesn't see. "
               "These feed directly into next run's ratings and are logged to Decision Log. "
               "Note: on Streamlit Cloud the table below still lives in that deployment's "
               "database only -- a redeploy can lose it. The durable copy is the versioned "
               "file manual_adjustments.json in the repo, which the weekly run reads "
               "(file wins per team; database-only teams still apply until exported, so "
               "nothing is silently dropped). A Cloud deployment cannot write that repo "
               "file -- use Export below, then commit the download to the repo, to make "
               "adjustments survive redeploys and reach the weekly run.")
    st.dataframe(adj_df, hide_index=True, use_container_width=True)

    st.markdown("**Repo file: export / import** (batch 3, item 5)")
    st.caption("Export downloads the merge of this dashboard's database and the repo file "
               "(file wins per team), with its audit history -- commit that download as "
               "manual_adjustments.json. Import reads an uploaded manual_adjustments.json "
               "into this deployment's database (merge, not wipe) so the adjustments work "
               "here; it does NOT write the repo file, so commit the file itself too if "
               "you want the weekly run to see the change.")
    try:
        _export_doc = manual_adjustments.build_export_doc()
        st.download_button("⬇️ Export manual_adjustments.json",
                           data=json.dumps(_export_doc, indent=2, sort_keys=True) + "\n",
                           file_name="manual_adjustments.json", mime="application/json")
        st.caption(f"Export covers {len(_export_doc['adjustments'])} team(s); "
                   f"repo file revision {_export_doc['revision']}.")
    except Exception:
        st.error("Could not build the export file. Error detail:")
        st.code(traceback.format_exc())
    _upload = st.file_uploader("Import manual_adjustments.json", type=["json"])
    if _upload is not None and st.button("Import into this dashboard's database"):
        try:
            # Database only, on purpose: parse_import_entries never
            # touches the repo file, which a Cloud deployment cannot
            # durably write. Commit the uploaded file itself to the
            # repo if you want the weekly run to see the change.
            _entries = manual_adjustments.parse_import_entries(
                _upload.read().decode("utf-8"))
            for _e in _entries:
                db.set_manual_adjustment(_e["team"], _e["points"], _e["reason"])
            st.success(f"Imported {len(_entries)} team(s) into this deployment's "
                       "database (merge, not wipe). This did NOT write the repo "
                       "file -- commit the file itself for the weekly run to see it.")
            st.rerun()
        except Exception:
            st.error("Import failed -- nothing was applied. Error detail:")
            st.code(traceback.format_exc())

    with st.form("add_adjustment"):
        c1, c2, c3 = st.columns([1, 1, 2])
        team = c1.text_input("Team (e.g. KC)")
        pts = c2.number_input("Points (+/-)", value=0.0, step=0.5)
        reason = c3.text_input("Reason (optional)")
        submitted = st.form_submit_button("Save adjustment")
        if submitted and team:
            db.set_manual_adjustment(team.strip().upper(), pts, reason)
            st.success(f"Saved: {team.upper()} {pts:+g}")
            st.rerun()

    remove_team = st.text_input("Remove adjustment for team")
    if st.button("Remove") and remove_team:
        db.delete_manual_adjustment(remove_team.strip().upper())
        st.rerun()

# ---- Data Health ----
with tab_health:
    st.subheader("Data Health")
    st.caption(
        "What actually happened on the last refresh -- use this to debug anything that looks off "
        "(missing injuries, suspicious ratings, zero confidence) instead of guessing."
    )
    if diagnostics_df.empty:
        st.write("No diagnostics recorded yet.")
    else:
        for _, row in diagnostics_df.iterrows():
            icon = "✅" if row["ok"] else "❌"
            st.write(f"{icon} **{row['step']}**")
            st.caption(row["detail"])

    st.divider()
    st.markdown(f"**Full team ratings, week {latest_week}** (sorted best to worst)")
    st.caption(
        "If a team's rating looks obviously wrong, this is where to spot it -- e.g. a team with a "
        "6-11 record showing a strongly positive rating, or a team missing entirely."
    )
    wk_ratings = ratings_df[ratings_df["week"] == latest_week] if not ratings_df.empty else ratings_df
    if wk_ratings.empty:
        st.write("No ratings snapshot yet.")
    else:
        display_cols = ["team", "rating", "raw_point_diff_prior_season", "games_played_this_season"]
        display_cols = [c for c in display_cols if c in wk_ratings.columns]
        _ratings_show = wk_ratings[display_cols].copy()
        # Item 7 -- rating bands: 90% interval from games played.
        # Best-effort, like the ATS cover-prob range: any missing
        # input skips the column rather than breaking the table.
        try:
            if "rating" in _ratings_show.columns and "games_played_this_season" in _ratings_show.columns:
                _ratings_show["rating_band_90"] = [
                    f"{lo:+.1f} to {hi:+.1f}"
                    for lo, hi in (uncertainty.rating_interval(r.rating, int(r.games_played_this_season))
                                   for r in _ratings_show.itertuples())
                ]
                display_cols = display_cols + ["rating_band_90"]
        except Exception:
            pass
        st.dataframe(
            _ratings_show[display_cols].rename(columns={
                "games_played_this_season": "games played (current season)",
                "raw_point_diff_prior_season": "raw avg point diff (prior season, unadjusted)",
                "rating_band_90": "rating 90% band (model uncertainty, not a guarantee)",
            }),
            hide_index=True, use_container_width=True,
        )
        st.caption(
            "The 90% band widens early in the season (few current-season games, "
            "prior-season rating doing most of the work) and narrows as games "
            "accumulate -- it stops narrowing late because recency weighting "
            "caps the effective sample size (see uncertainty.py). A band that "
            "crosses zero means the rating's sign itself is uncertain."
        )
        st.caption(
            "The raw point diff column is a simple, unadjusted sanity check -- a team that outscored "
            "opponents overall should essentially never show a negative rating, and vice versa. If you "
            "see them disagree in sign for a team, that's a strong, independent signal of a real bug -- "
            "screenshot this table and send it over."
        )
