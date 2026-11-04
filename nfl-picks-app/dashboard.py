"""
Run locally with:  streamlit run dashboard.py
Deploy free at:     https://share.streamlit.io  (point it at this repo)
"""
import traceback

import pandas as pd
import streamlit as st

from nflpicks import config, db, grading
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

tab_names = ["ATS Picks", "Survivor Pool", "Injury Review", "Model Grades", "Manual Adjustments", "Data Health"]
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
tab_ats, tab_survivor, tab_injuries, tab_grades, tab_adjust, tab_health = st.tabs(tab_names)

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
