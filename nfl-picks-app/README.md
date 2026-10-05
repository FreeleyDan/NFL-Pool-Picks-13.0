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
weekly_run.py   the weekly job: pulls data, predicts, saves, emails
dashboard.py    Streamlit UI -- this replaces the spreadsheet as your daily view
.github/workflows/weekly.yml   free scheduled runs via GitHub Actions
```

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
2. **Add repo secrets** (Settings → Secrets and variables → Actions):
   - `PICKS_EMAIL` — where the weekly summary goes
   - `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`, `SMTP_FROM` — for
     email. Gmail: use an [app password](https://myaccount.google.com/apppasswords),
     host `smtp.gmail.com`, port `587`. If you skip these, the job still runs
     and updates the dashboard — it just won't email you.
3. The workflow in `.github/workflows/weekly.yml` runs every Tuesday
   (after lines post), grades last week, predicts next week, and commits
   the updated `nfl_picks.db` back to the repo. You can also trigger it
   manually from the Actions tab any time.

## Getting the dashboard on your phone

1. Go to [share.streamlit.io](https://share.streamlit.io), sign in with
   GitHub, and point it at this repo + `dashboard.py`. Free tier is plenty
   for personal use.
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
