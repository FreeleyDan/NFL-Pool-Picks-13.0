"""
Run locally with:  streamlit run dashboard.py
Deploy free at:     https://share.streamlit.io  (point it at this repo)
"""
import traceback

import pandas as pd
import streamlit as st

from nflpicks import adjustment_grading, config, db, grading, review_queue, standings, submission
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
    show = wk_df[show_cols]
    show.columns = ["Away", "Home", "Vegas Spread", "Pool Spread", "Model Spread (raw)", "Model Spread (official)",
                     "Edge", "Pick", "Confidence", "Flag"] + (["Cover Prob"] if "cover_prob" in wk_df.columns else []) + [
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
        "and the season-long winner (see README)."
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

# ---- Manual adjustments ----
with tab_adjust:
    st.subheader("Manual Adjustments")
    st.caption("Your call on injuries, weather, or anything else the model doesn't see. "
               "These feed directly into next run's ratings and are logged to Decision Log. "
               "Note: on Streamlit Cloud these live in that deployment's database only -- "
               "a redeploy can lose them. The GitHub Action workflow commits the "
               "runner's database back to the repo, but it cannot commit adjustments "
               "made inside a Cloud deployment. Keep a note of important adjustments.")
    st.dataframe(adj_df, hide_index=True, use_container_width=True)

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
        st.dataframe(
            wk_ratings[display_cols].rename(columns={
                "games_played_this_season": "games played (current season)",
                "raw_point_diff_prior_season": "raw avg point diff (prior season, unadjusted)",
            }),
            hide_index=True, use_container_width=True,
        )
        st.caption(
            "The raw point diff column is a simple, unadjusted sanity check -- a team that outscored "
            "opponents overall should essentially never show a negative rating, and vice versa. If you "
            "see them disagree in sign for a team, that's a strong, independent signal of a real bug -- "
            "screenshot this table and send it over."
        )
