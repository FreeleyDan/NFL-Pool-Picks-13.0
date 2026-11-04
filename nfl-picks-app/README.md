# NFL Pool Picks

A standalone replacement for the Google Sheets / Apps Script picks engine.
Same core philosophy (injuries are review-only, EPA stays in shadow mode
until proven, everything is walk-forward graded) with two real upgrades:

1. **Opponent-adjusted (SOS) power ratings** instead of raw margin averages
   — a win only counts as much as the opponent it came against.
2. **A survivor-pool optimizer** that sequences your picks across the whole
   remaining season instead of just picking the biggest favorite each week.

## Architecture

```
nflpicks/
  config.py     all tunable settings
  data.py       pulls schedule/results, injuries, team stats (nflverse)
  ratings.py    SOS-adjusted SRS ratings + empirical home-field advantage
  injuries.py   review-first injury flags/suggestions (no auto point shifts)
  epa.py        weighted-EPA shadow layer
  model.py      turns ratings into spread/total picks with a confidence score
  survivor.py   season-long survivor pool optimizer (assignment problem)
  grading.py    walk-forward grading of each model variant vs. actual results
  db.py         SQLite storage (one table per old spreadsheet tab)
weekly_run.py   the weekly job: pulls data, predicts, saves (email is opt-in only)
dashboard.py    Streamlit UI -- this replaces the spreadsheet as your daily view
.github/workflows/weekly.yml   free scheduled runs via GitHub Actions (at repo root)
tests/          unit tests for the bug fixes -- python -m unittest discover -s tests
```

## Spread conventions -- read this before entering a pool line

Everything inside the app uses nflverse convention: spread_line is
home-perspective, positive = home favored, negative = away favored.
Sportsbooks and pool sheets print the opposite: a home favorite is
"-3.5". The dashboard's pool-line form accepts sportsbook convention
and converts it on save (`config.to_nflverse_spread`); the ATS table
itself stays in nflverse convention so all its spread columns are
directly comparable. Entering a sportsbook line directly into the
database used to silently flip the pick -- that was fixed 2026-11-04.
Overrides saved before that date were stored unconverted: re-enter them.

## Weekly winner AND season-long winner

This pool pays both, and the same picks count for both, so there is
no separate weekly pick set to generate. The default is the
season-maximizing choice: pick the side with the highest estimated
cover probability on every game. The dashboard's Cover Prob column
shows how close each pick is to a coin flip. For the weekly prize
alone, variance helps -- a contrarian pick on a coin-flip game can
separate you from the field for that week -- but it costs expected
season points on average. Use Cover Prob to decide where a weekly
gamble is cheap (near 50%) vs. expensive (a strong lean you'd be
throwing away).

## Local setup

```bash
pip install -r requirements.txt
python weekly_run.py          # pulls data, computes picks, saves to nfl_picks.db
streamlit run dashboard.py    # opens the dashboard at localhost:8501
```

Set `PICKS_SEASON` as an env var if you're not on the current default season
(see `config.py`).

## Automating it (no server required)

1. **Push this repo to GitHub** (private repo is fine, free).
2. **No email setup needed.** The workflow does not send email, and
   `weekly_run.py` skips email entirely when `SMTP_HOST` is unset.
   Email is opt-in only: if you ever want it back, set `SMTP_HOST`,
   `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`, and `SMTP_FROM` (Gmail: use an
   [app password](https://myaccount.google.com/apppasswords), host
   `smtp.gmail.com`, port `587`). An email failure can no longer abort
   the run or block the database commit.
3. The workflow in `.github/workflows/weekly.yml` (at the repo root,
   not inside `nfl-picks-app/`) runs Tuesday and Thursday at 15:00 UTC
   (7 AM PT in winter). Thursday's run is the one that matters: picks
   are due Thursday at 11:00 AM PT, so Thursday morning catches the
   latest lines and practice reports. It grades completed games,
   predicts the next week, and commits the updated `nfl_picks.db`
   back to the repo (force-added, since `*.db` is gitignored for
   local dev). You can also trigger it manually from the Actions tab
   any time.

## Getting the dashboard on your phone

1. Go to [share.streamlit.io](https://share.streamlit.io), sign in with
   GitHub, and point it at this repo + `nfl-picks-app/dashboard.py`
   (not the repo root -- the app lives in that subfolder). Free tier is
   plenty for personal use. Note: manual adjustments made inside a
   Streamlit Cloud deployment live in that deployment's database only
   and can be lost on redeploy -- the GitHub Action cannot commit them
   back. Keep a note of important adjustments.
2. Since the GitHub Action commits `nfl_picks.db` back to the repo each
   week, Streamlit Cloud will pick up fresh data automatically on its next
   redeploy (it redeploys on every push to the repo).
3. Bookmark the resulting URL on your phone — that's your new spreadsheet.

## Making manual adjustments

Use the **Manual Adjustments** tab in the dashboard instead of the old
Adjustments sheet tab — same concept, same effect (feeds into next run's
ratings), now editable from your phone instead of requiring Sheets.

## Checking whether the EPA layer is ready to go live

After a few weeks of games, run:

```bash
python -m nflpicks.grading
```

then check the **Model Grades** tab in the dashboard. Only flip
`config.EPA_LIVE = True` if the EPA or combo variant's MAE beats the
margin model's, and/or its ATS% clears ~52.4% (break-even against
standard -110 vig) across a real sample — not a lucky week or two.
Backfilled games (predicted after they were played, from today's
ratings) are flagged `is_backfill` in `model_log` and excluded from
grades, so they can't inflate those numbers with look-ahead bias.

## Roadmap (as you said: start simple, then get more sophisticated)

Already better than the original Sheets version:
- [x] Opponent-adjusted (SRS) ratings instead of raw margin averages
- [x] Empirical home-field advantage instead of a hardcoded constant
- [x] Confidence score based on current-season sample size
- [x] Survivor pool optimizer (season-long assignment, not greedy)

Natural next upgrades, roughly in order of value-per-effort:
1. **SOS-adjust the EPA shadow layer too** (currently naive averages, like
   the original) — reuse the same iterative approach as `ratings.py`.
2. **Team-specific home-field advantage** (travel distance, altitude,
   dome vs. outdoor) instead of one league-wide number.
3. **Backtest across multiple historical seasons** before the season
   starts, not just walk-forward within the current one — lets you tune
   `MARGIN_CAP`, `PRIOR_REGRESSION`, `RECENT_HALF_LIFE_GAMES` etc. against
   real history instead of guessing.
4. **Proper totals model** — the current one is a simplified scoring-average
   blend carried over from the original script; an SRS-style split of
   offensive/defensive scoring ratings would sharpen it the same way the
   spread model improved.
5. **Closing line value tracking** — log the line at the time you'd
   actually bet vs. the true close, to see if your timing costs you edge.
6. **Survivor pool equity** — if your pool is large, weight picks partly by
   how many entries you'd expect to share your pick (thinning the field
   late is worth something beyond raw win probability).

None of these require touching the architecture — just the model math in
`ratings.py`, `epa.py`, or `survivor.py`.
