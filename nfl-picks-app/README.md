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

## Backtest results (2026-11-04, items 1-7) -- read before trusting any number

Run it yourself:

```bash
python -m nflpicks.backtest --games-csv /path/to/games.csv --seasons 2019-2025 --variants baseline,fade6,div1,calibrated_pick,offdef
```

The harness (`nflpicks/backtest.py`) is point-in-time: for season S,
week W, ratings use only season-S games before week W plus the full
season S-1. Backfill cannot leak in (no model_log read at all).
Caveat that applies to every number below: `spread_line` in
games.csv is the recorded line, NOT a verified Tuesday/Thursday
pool-locked line, so ATS against a real pool line will differ.

Harness, live-like HFA, 2019-2025 (n=1,871 games, 1,828 decided):

- **Current live defaults (fade-12, div 0.9): 49.12% ATS, MAE 10.34**
  (recorded line MAE 9.83 on the same games -- the model still trails
  the market, and 49.12% is below ~52.4% breakeven. This is a
  model-quality improvement, not a betting edge.)
- Fade-6 (old setting): 47.16% (-1.97 pp vs live, paired z=-2.5).
  Slower prior fade is the one clear win, hence
  `PRIOR_FADE_GAMES=12` in config.py.
- Div 1.0 vs 0.9: 48.69% (-0.44 pp, paired z=-1.13, noise). The
  divisional discount is therefore kept at the original 0.9 -- neither
  setting is proven, so no change is claimed.
- Calibrated pick (pick by shrunk margin vs line): 50.16% (+1.04 pp,
  paired z=0.99, 369 flips). Noise on this sample, and the shrinkage
  coefficients were fit on the same data, so `CALIBRATED_PICK` stays
  False. Cover Prob itself IS calibrated (see below) -- that is a
  confidence signal, not a pick change.
- Offense/defense split SRS (harness diagnostic only): 48.85%
  (+0.11 pp, paired z=0.08) with MAE 10.71 (worse). NOT promoted.
  The separate ablation study agreed: split is 98.4% correlated with
  single SRS and significantly worse on MAE. Do not promote it.
- Travel (0.95 pts/1,000 mi, harness only): 49.67% (+0.93 pp, paired
  z=1.28, noise). `TRAVEL_ENABLED` stays False.

Earlier analysis scripts (prior-season-only HFA, slightly different
from the harness) reported baseline 47.65% ATS, fade-12 49.84%, and
half-life 8 as a null (47.70%). Those numbers are superseded as the
canonical ones by the harness above, but they point the same way.

**Item 2 -- Cover Prob is now calibrated.** Old: normal curve, SD
13.5, around the raw model margin. New (`nflpicks/calibration.py`):
actual margin regresses on model margin with slope 0.68, intercept
0.36, residual SD 13.43, and cover probability interpolates the
empirical residual CDF from 2019-2025 (key-number clustering kept:
actual margin == 3 in 7.1% of games per side, == 7 in ~4.2%).
A Cover Prob near or below 50% for the official pick is honest:
after shrinkage that side is not actually favoured -- treat it as a
coin flip.

**Item 3 -- EPA, NOT promoted.** App-scale combo (1.6*off - 1.0*def
as points) was 48.9% ATS with significantly worse MAE -- do not flip
`EPA_LIVE` on that basis. Calibrated scale (0.38 pts per EPA unit,
`EPA_POINTS_PER_UNIT` in config.py, applied in `epa.py`) gave, in the
7-season ablation, a 50% combo of 49.9% ATS / MAE 10.35 vs SRS
48.0% / 10.66, MAE gains in all 7 seasons -- but ATS p=0.04 would not
survive Bonferroni across 9 variants, and my own 2024-2025 replication
was 50.46% vs 49.17% (paired z=0.73, noise). `EPA_LIVE` therefore stays
False. What WAS built: calibrated EPA ratings, prior-season blending
(`epa.blend_epa_ratings`), and week-filtered team-stats loading
(`data.load_team_stats(season, week)` -- the published file is
full-season, so unfiltered retrospective use leaks future weeks).

**Item 5 -- QB, review-only as before.** `injuries.qb_change_suggestion`
adds a 1.5 pt review-only signal when a team's QB name changes from
the previous game. That is a situational proxy (~1-1.7 pt penalty in
2019-2025, averaged over ALL changes including benchings/rest), NOT
a causal starter-injury value and NOT a substitute for the 5.0
starter-injury value when a starter is OUT/DOUBTFUL.
`INJ_AUTO_APPLY` stays False. Thursday practice participation is
already used in `review_injuries` (DNP=0.6, LP=0.25 when report_status
is empty); the weekly files carry no day-of-week, so no finer
Thursday signal is available from this data.

**Item 6 -- Rest / travel / HFA.** Rest stays 0.25 pts/day capped at
2.0 as a margin predictor (ATS residual slope was -0.03 +/- 0.12
pts/day, meaning the market already prices rest -- raising it would
create false edges). No bye or short-week extra: pooled bye residual
was -0.83 (wrong sign, n.s.) and one-sided short-week contrasts had
n=0. Travel stays off (see above). HFA stays league-wide empirical,
clamped 0.5-3.0, zero at neutral sites: team-specific HFA was mostly
noise (team ATS-residual SD 1.44 vs mean SE 1.73). Denver altitude:
no effect found (n=59, noise) -- not implemented.

**Item 7 -- Line movement.** `db.line_snapshots` records the pool line
(source 'pool') and the live/closing line (source 'closing') per game
per run (written by `weekly_run.py`); `db.get_line_moves` and a new
dashboard section flag games where movement is >= 1 pt or the model
edge flips between the two lines, for Thursday-morning manual review.
The pool line is NEVER auto-switched -- it is what you are judged
against. Until both snapshots exist for a week, the section is empty,
not an error.

**Still off by default, on purpose:** `EPA_LIVE=False`,
`CALIBRATED_PICK=False`, `TRAVEL_ENABLED=False`,
`INJ_AUTO_APPLY=False`. Each has a harness variant to re-test it;
flip one only on a walk-forward win, not on a single-season result.

**Known limits that remain:** early-season SRS damping cuts the
weeks 2-3 non-convergence from delta ~21 pts to ~0.006 pts (immaterial
to picks) but does not always reach the strict 1e-4 tolerance; the
live workflow run has not yet been observed end-to-end; team-stats
EPA is not SOS-iterative (naive defense average, as before).

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
