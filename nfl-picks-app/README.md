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
.github/workflows/tests.yml    compile + unit tests on every push/PR (at repo root, batch 5 group C)
nflpicks/explain.py + sensitivity.py   per-game "why" and flip sensitivity (labels only, batch 5 group C)
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

## Backtest batch 2 (2026-11-04, items 4-6) -- read before trusting any number

Run it yourself (team-stats fetch needs network; ~2 min):

```bash
python -m nflpicks.backtest --games-csv /path/to/games.csv --seasons 2019-2025 --variants baseline,epa_sos,ensemble_wf
```

Point-in-time throughout: for season S, week W, every component uses
only season-S games/weeks before W plus the full season S-1. Ensemble
weights and feature coefficients for season S are learned ONLY from
completed seasons before S (plus two burn-in seasons, 2017-2018, scored
nowhere), so ensemble/feature numbers are out-of-sample w.r.t. their
weights. Training-window fits are reported separately in the harness
output (`train_fit_by_season`); a training win that evaporates
out-of-sample is visible, not hidden.

**Grading-rule note that changes the baseline -- read first.** The
batch-2 harness (`backtest._ats_rows`) grades a combined margin exactly
equal to the line as a PUSH (no pick), not a wrong pick. The historical
harness (`evaluate_variant`, which produces the canonical 49.12%
baseline above) grades it wrong, because a margin can never be > or <
the line when it equals it. That single rule moves the batch-2
INTERNAL baseline to 50.03% ATS (same 898 correct picks; 34 tie games
move from "wrong" to "push", decided 1,828 -> 1,795). Paired
comparisons inside batch 2 are all against the internal baseline, so
they are valid; do NOT compare a batch-2 absolute ATS% directly against
the canonical 49.12% without accounting for the 34 ties. The first
batch-2 run used the old rule and produced spurious feature "wins"
(pass_share paired z=4.38, explosive 14-0 flips): those were tiny
adjustments breaking exact ties that the old rule had counted wrong.
Under push grading every feature below is null. That correction is
the reason this section reports internal-baseline numbers.

**Item 4 -- Ensemble + SOS-adjusted EPA: built, tested, NOT promoted.**
- `epa.sos_epa_ratings()` replaces the naive defense average with the
  same damped-Jacobi SOS solve as `ratings.py`, in EPA units
  (off[t] = mean(off_epa - league + def[opp]),
  def[t] = mean(league + off[opp] - opp_off_epa); def positive = good
  defense, so the rating is `0.38 * (1.6*off + 1.0*def)`). The naive
  `weighted_epa_ratings()` is kept untouched for comparison. This
  supersedes the Known-limits line above about naive EPA defense: the
  SOS version now exists, in shadow.
- New `nflpicks/elo.py`: sequential win/loss Elo (K=20, season rollover
  regressed 0.5 toward 1500, one update per week from pre-week ratings),
  converted to a margin with the normal quantile (SD 13.5) + HFA. No Elo
  existed before this batch.
- New `nflpicks/ensemble.py`: SRS + Elo + SOS-EPA + market (the line
  itself) combined with simplex grid weights (step 0.25) learned
  walk-forward, never hand-set.
- Results, 2019-2025 (n=1,871, internal baseline 50.03% / MAE 10.34):
  - MAE-objective learned weights collapse to 100% market in EVERY
    season (line MAE 9.83 beats every model component, ~10.3, so no
    blend beats the line on MAE). The ensemble then makes NO picks
    (margin == line -> push on all 1,871 games). That is the honest
    MAE answer: on MAE, the market already wins; it is not a pool
    model and is not promoted.
  - ATS-objective learned weights (sensitivity, pool's actual scoring
    rule): 50.89% ATS / MAE 9.87, paired +0.97 pp vs internal baseline,
    z=0.75 -- noise, below ~52.4% breakeven. Learned weights are mostly
    0.25 Elo + 0.75 market (2020: 100% market -> no picks that season).
  - Equal-weight (0.25 each, a prior, NOT learned): 51.18% / 10.07,
    paired +0.84 pp, z=0.81 -- noise, and it does not satisfy the
    learned-weights requirement anyway.
  - Components alone: Elo 52.52% / MAE 10.39 (paired +2.28 pp, z=1.75)
    -- nominally above breakeven but inconsistent by season (58.2% in
    2020, 48.5% in 2023, 49.1% in 2025), worse MAE than SRS, and
    z=1.75 does not survive correction across the ~12 variants tested.
    SOS-EPA alone: 51.14% / 10.30 (paired +1.02 pp, z=0.83; 46.8% in
    2025). Neither is promoted.
  - SOS-EPA 50% combo (`epa_sos` variant, canonical grading):
    50.66% ATS / MAE 10.25 vs canonical baseline 49.12% / 10.34
    (paired +1.53 pp, z=1.76). Better than the old naive-EPA combo
    (49.9% / 10.35) but still below breakeven and not significant
    after multiple-comparison correction. `EPA_LIVE` stays False and
    `ENSEMBLE_LIVE` (new, `config.py`) stays False. Nothing in
    `model.py`, `weekly_run.py`, or `dashboard.py` reads the ensemble
    live yet -- that wiring is deliberately not done until a
    walk-forward win exists.

**Item 5 -- Play-by-play features: proxies built, NONE kept.**
- True early-down success and true red-zone efficiency are NOT built:
  neither exists in the team-stats file (no down/distance or red-zone
  columns), and the nflverse play-by-play release filename could not
  be confirmed (guessed csv / csv.gz / parquet URLs for 2024 all 404,
  the same unconfirmed-filename problem as weekly rosters). No
  play-by-play file was downloaded and no early-down/red-zone numbers
  were invented from team totals.
- What `nflpicks/features.py` does build, from week-filtered team
  stats only: explosive-play rate (20+/40+ plays per play),
  pressure allowed (sacks per dropback), pressure generated
  (sacks + QB hits per opponent dropback, joined on game_id),
  pass share, and first-down rate (a drive-sustainment proxy,
  explicitly NOT early-down success). Interactions: pass-heavy team in
  20+ mph wind (games.csv `wind`; missing wind -> no adjustment, not
  an imputed wind) and weak-OL-vs-top-rush
  (`home_allowed*away_gen - away_allowed*home_gen`). Coefficients are
  clipped least squares fit on the training window only, per season.
- Walk-forward results (internal baseline, paired z in brackets):
  all-features sum 50.77% / MAE 10.37 [z=0.86, MAE worse than baseline];
  explosive 49.89% [0.0 -- its earlier flips were all tie artifacts];
  pressure allowed 50.16% [0.33]; pressure generated 49.95% [0.0];
  pass share 50.44% [1.0]; first-down 49.73% [-0.18];
  wind_pass 50.00% [-0.45, few windy games]; ol_rush 49.95% [-0.32].
  No feature beats the baseline out-of-sample, so no feature is wired
  live and there is no `FEATURES_LIVE` flag to flip. Fitted
  coefficients were also unstable across seasons (e.g. explosive
  -5.7 to +7.9, pass_share sign flips), which is itself evidence
  against promotion.

**Item 6 -- Market cross-checks: built as diagnostics only.**
- New `nflpicks/market.py`: moneyline -> vig-free win prob (American
  odds normalized to sum 1) -> moneyline-implied margin (normal
  quantile, SD 13.5); total-implied team scores ((total +/- spread)/2);
  spread-implied win prob. Flags (spread/ML margin gap >= 3 pts,
  model/ML gap >= 3 pts, model-total gap >= 5 pts, model/spread gap >=
  sanity flag) are diagnostic only -- `crosscheck()` returns the input
  lines verbatim and has no "recommended line" field, so no caller can
  auto-switch a line from it. `db.market_crosschecks` logs all three
  lines (spread, both moneylines, total) plus flags per game; no pick
  logic reads that table back.
- Results, 2019-2025: spread and moneyline markets agree closely, as
  efficient markets should -- only 8 of 1,871 games flag a >= 3 pt
  spread/ML disagreement. SRS ATS on flagged games is 25% (2/8) vs
  50.22% unflagged -- n=8, meaningless, no claim made. The
  moneyline-implied pick itself is 49.34% ATS: no edge there either.
  The pool line is never auto-switched (unchanged from item 7).

**Still off by default, on purpose (batch 2 additions):**
`ENSEMBLE_LIVE=False`, `EPA_LIVE=False` (unchanged). No feature or
market signal changes a pick.

## Backtest batch 3 (2026-11-04, items 1-2) -- standings risk + season simulation

Both modules are LABEL-ONLY. Neither changes a pick, switches a line,
or touches `model.py`, `weekly_run.py`, `dashboard.py`, or
`.github/workflows/weekly.yml`. They take a week's default picks (with
calibrated cover probabilities) and return labels and odds estimates
for the user to act on. All thresholds live in `config.py`
(`STANDINGS_*`, `SIM_*`); no magic numbers are hardcoded in the
modules themselves.

**Item 1 -- Standings-aware risk (`nflpicks/standings.py`).**
Input is deliberately small and user-entered -- there is no standings
feed and no consensus data: `user_wins`, `leader_wins`, and
`weeks_left` (including the current week, so the final week is 1).
Rules, with thresholds in `config.py`:

- Early (`weeks_left > 4`): rule OFF, no matter the standings.
  Default behaviour is unchanged early, as specified.
- Trailing late: deficit >= 2 games, or deficit >= 1 in the final
  week (mirroring loses outright then, so differentiation is the
  only route even to a tie). Up to 3 coin-flip games per week are
  labelled GAMBLE, cheapest first (lowest expected-win cost
  `p_default - p_gamble`); the gamble side is the opposite of the
  default pick, and the default pick itself is returned unchanged.
- Leading late (lead >= 1): protect mode -- no gambles; strong leans
  are labelled PROTECT.
- Tied late: neutral, no gambles (the season tie-break / split rule
  is unknown, so this module does not gamble to break a tie on an
  invented tie-break).
- Coin flip = even the best side's calibrated cover prob <= 0.55
  (39% of 2019-2025 harness games, ~6.8 per late week -- hence the
  cap of 3, cheapest first, rather than flipping all of them).
  Strong lean = calibrated pick prob >= 0.65 (20% of harness games);
  a strong lean is never a gamble candidate in any mode.

Example:

```python
from nflpicks import standings
week = [{"home": "KC", "away": "DEN", "default_side": "home",
         "pick_prob": 0.52}, ...]  # pick_prob from calibration.py
out = standings.suggest_gambles(week, user_wins=120, leader_wins=122,
                                weeks_left=3)
# out["games"] carries label GAMBLE / PROTECT / DEFAULT per game,
# out["gambles"] the suggested flips, out["note"] the plain-language
# statement that default picks are unchanged.
```

**Item 2 -- Season simulation (`nflpicks/simulation.py`).**
Monte Carlo over the remaining schedule from calibrated cover
probabilities: expected season wins, P(finishing first, strict),
P(tie), P(first-or-tied), and the user's final-wins distribution
(p10/p50/p90). Defaults: 10,000 trials, seed 20261104 (same seed +
same games in the same order = identical output).
`compare_gambles(baseline, gambled, ...)` uses the SAME seed for both
slates (common random numbers, paired delta) and labels the week's
gambles CHEAP (cost <= 0.10 expected wins and P(first) does not
fall), EXPENSIVE (cost >= 0.25 wins, or P(first) falls >= 1.0 pp),
or NEUTRAL in between.

Two modelling choices, stated so they can be checked:

- Games are simulated, not picks. Each game gets one draw (home
  covers / push / away covers); user and leader results derive from
  that same draw via their sides. Simulating slates independently
  would be wrong both ways: mirroring would invent variance, and
  contrarian flips would hide their perfect anti-correlation.
- Pushes: calibrated p is P(win | decided) -- the calibration CDF
  and harness ATS% both exclude pushes -- so each draw is a push
  with probability `SIM_PUSH_PROB` = 0.023 (43 pushes in 1,871
  harness games), otherwise home covers with probability p_home.
  Effective win prob per game is therefore p * (1 - 0.023), and a
  push counts for neither slate. This matches the batch-2 push
  grading convention (a margin exactly equal to the line is a push,
  not a wrong pick); the canonical `backtest.evaluate_variant`
  grading is left untouched, so the 49.12% canonical baseline and the
  50.03% batch-2 internal baseline keep their documented meanings.

Example:

```python
from nflpicks import simulation
remaining = [{"p_home": 0.58, "user_side": "home", "leader_side": "home"},
             ...]  # p_home from calibration.cover_probability_home
base = simulation.simulate_season(remaining, 120, 122)
gambled = [dict(g, user_side="away") if <flipped> else g for g in remaining]
cmp_ = simulation.compare_gambles(remaining, gambled, 120, 122)
# cmp_["delta_p_first_pp"], cmp_["expected_cost_wins"], cmp_["label"]
```

**Validation on historical seasons -- what it would have done vs
default, honestly reported.** Method: point-in-time backtest baseline
rows 2019-2025 (n=1,871) with calibrated pick probs attached;
`standings.evaluate_late_window()` replays the last 4 weeks of each
season week by week, starting the leader 2 games ahead (trailing
scenario), at 2 games behind (leading), or tied. The leader is
assumed to make default picks every late week -- that assumption
drives several numbers below, and a real leader who picks contrarian
breaks it.

- Trailing by 2, actual outcomes: 63 gambles over 7 seasons (447
  late games), gambled slate 219 wins vs default slate 215 (+4),
  summed expected cost -1.26 wins (negative -- see the sub-0.5 note
  below), overtake 2/7 seasons for the gambled slate vs 0/7 for
  default. The 0/7 is by construction: mirroring a default-picking
  leader with a 2-game deficit can never close the gap, so any
  differentiation looks good on that axis. The informative parts
  are the win delta (+4 on 447 games, i.e. noise) and that the
  gambles did not bleed wins: in 2020 they cost 3 wins, in 2022-2023
  they gained 3 each. Seven seasons is seven realizations -- this
  does NOT prove the rule helps.
- Leading by 2, actual outcomes: 0 gambles suggested in all 7
  seasons, slate identical to default, 2-game lead preserved in all
  7 -- again by construction under the mirroring-leader assumption,
  not evidence about a real field.
- Tied: 0 gambles, gap stays 0, by design.
- Monte Carlo on real late-season slates (one week's gambles only,
  rest of season default, trailing by 2, 5,000 trials, 28 late
  weeks): mean P(first) gain +10.87 pp, median +12.57 pp, 24 weeks
  positive, 4 zero, 0 negative; mean expected cost -0.065 wins;
  labels CHEAP 26/28, NEUTRAL 1, EXPENSIVE 1. The 4 zeros are not a
  bug: those weeks had only 1-2 coin-flip games, and with a
  mirroring leader 1-2 flips can at best tie a 2-game deficit, so
  strict P(first) cannot move (P(first-or-tied) does). The one-week
  MC also understates the full-window effect, since real use flips
  again each remaining week.
- Sub-0.5 note (read before trusting the negative costs): a default
  pick's calibrated prob can be below 0.5, meaning the default side
  is not actually favoured after shrinkage (CALIBRATED_PICK stays
  False, so defaults still follow the raw margin). Cheapest-first
  ordering picks those games first, so many "gambles" are partly
  calibration corrections that gain expected wins, not true
  sacrifices. The cost accounting shows this instead of hiding it,
  but it means the negative expected costs above are not pure
  "free variance" -- some of the gain is just fixing a sub-0.5 pick.

**Limits that remain.** No dashboard or weekly-run wiring yet: the
modules are a callable API, and the parent task wires the labels
into the UI. P(first) inherits the model's error (the model itself
is ~49-50% ATS, below breakeven), so treat it as a relative
gamble-comparison tool, not an absolute title probability. Game
draws are independent across games (no shared weather / injury
correlation). The leader never reacts to your gambles, and there is
no field-consensus input -- if the real leader is not a
default-picker, mirroring the default does not guarantee holding a
lead. Push handling assumes your pool scores a push as neither a win
nor a loss; if it scores half a win, P(first) shifts slightly.

## Submission sheet, Thursday review queue, and reliability (2026-11-04, items 3 and 7)

**Item 3a -- Submission sheet (`nflpicks/submission.py`, dashboard "Submit & Review" tab).**
`build_submission_sheet()` produces an exact copy-paste list for replying to
Barbara's email, due Thursday 11:00 AM PT. Every pick is shown in sportsbook
notation exactly as a pool sheet prints it (home favorite = negative), derived
from the stored nflverse line -- pool line where set (that is the judged line),
else the live Vegas line -- and double-checked against the model's own pick
string: a derived line that does not match the model string is flagged MISMATCH
in the checklist and must not be sent, so a flipped sign can never slip through
silently. The checklist also verifies every game has a pick, no matchup appears
twice, and every pick team plays in its game, plus one manual item (review queue
checked) that code cannot verify and therefore never marks passed. The copy
block is sorted alphabetically by matchup, not kickoff order (the picks table
stores no kickoff time), so match by matchup, not by position.

**Item 3b -- Thursday review queue (`nflpicks/review_queue.py`, same tab).**
Only flagged games appear, prioritised so Thursday-morning review is time-boxed:
P1 (check before submitting: MODEL_REVIEW, EDGE_FLIP, or a CRITICAL injury note)
first, then P2, with biggest |raw model-vs-line deviation| first inside a
priority, then biggest line move. A game is flagged only by: raw deviation >=
`EDGE_THRESHOLD` (strong lean) or >= `SANITY_EDGE_FLAG` (needs review); pool-vs-
closing line move >= `REVIEW_LINE_MOVE_THRESHOLD` (1.0 pt, now in `config.py`
and shared with `db.get_line_moves`, which previously hardcoded 1.0) or an edge
flip between those lines (db line snapshots, reused, never auto-switching);
a critical/long-term/OUT-or-DOUBTFUL-starter injury note or a returning starter
for either team; a weather note that actually changed the total; or a market
cross-check flag. Unflagged games are omitted on purpose -- an empty queue means
nothing met a flag threshold, not that games were checked and cleared.

**Standings wiring (batch-3 API, previously unwired).** The "Submit & Review"
tab now wires `standings.suggest_gambles()` into the dashboard: enter your and
the leader's season ATS wins plus weeks left (including this week) and it labels
the week's default picks GAMBLE / PROTECT / DEFAULT from their cover
probabilities. Picks are unchanged by it -- a GAMBLE label is a suggestion to
flip, not a changed pick. What is deliberately NOT wired there:
`simulation.compare_gambles()` over the full remaining schedule. It needs cover
probabilities for every remaining game, and this database stores only the
current week's picks, so running it on one week would misstate season odds.
Where that wiring goes: a future `weekly_run.py` step that saves remaining-
schedule ATS cover probabilities (the survivor optimizer's win probabilities
are straight-up win odds, not ATS cover probabilities, and must not be
substituted), after which the dashboard can build baseline vs gambled schedules
and call `compare_gambles()` on them.

**Item 7a -- Tests before the weekly run (`.github/workflows/weekly.yml`).**
A "Run unit tests (fail fast)" step (`python -m unittest discover -s tests -v`,
no `|| true`) now runs before the weekly-picks step, so a bad push fails the
job before any pick is computed, graded, or committed. Cron (`0 15 * * 2,4`),
`contents: write` (needed for the DB commit), and no-email behaviour are
unchanged.

**Item 7b -- Workflow run status in the dashboard (same tab, clearly labelled).**
The app cannot read live GitHub Actions status without a token it does not
have, so it does not invent one. What it shows is the last run saved in the
database it can actually read: the latest week's `picks.run_time` plus any
failed `run_diagnostics` steps, with an explicit note that this is not live
Actions status and that the Actions tab on GitHub is the live source. If the
timestamp is older than the last scheduled run, the workflow likely failed.

**Item 7c -- Manual adjustments graded separately (`nflpicks/adjustment_grading.py`,
Model Grades tab).** "Adjusted" means a manual adjustment was active for that
game at pick time (`manual_adj_home`/`manual_adj_away` non-zero, already stored
per game by `db.save_picks`); it does NOT mean the adjustment flipped the pick,
which would need a counterfactual re-run without adjustments and is not
claimed. ATS uses the judged line (pool line where set, else live Vegas) and
the games.csv `result` (home margin), pushes excluded from ATS% like the model
grades. Summaries are stored in the new `db.adjustment_grades` table (scopes
adjusted / unadjusted / all) and recomputed from picks + results on demand.
Judge overrides over a season, not one week -- a single week proves nothing.

**Limits that remain.** Streamlit is not installed in the build environment,
so `dashboard.py` was compile-checked only, never clicked through live; the
new tab needs a live run to confirm rendering. The review queue depends on
line snapshots existing for both sources: until a run has saved pool and
closing snapshots for a week, line-move/edge-flip reasons are simply absent.
The submission sheet's "pool line used where set" check is a count, not a
per-game proof against Barbara's sheet -- the final eyeball before sending is
still the user's. No GitHub push was made for this batch (local files only).

## Backtest batch 4 (2026-11-04, item 8) -- proper totals model: built, tested, NOT promoted

Run it yourself (local games.csv; no network, no team-stats fetch):

```bash
python -m nflpicks.backtest --games-csv /path/to/games.csv --seasons 2019-2025 --variants baseline --totals
```

**What was built (totals-only; spread untouched).** `nflpicks/scoring.py`
now has an SRS-style offensive/defensive scoring split alongside the
old blend: `_srs_offdef_solve` (damped-Jacobi solve, HFA split hfa/2
per side so it cancels in the total, neutral-site games get no HFA
term, no recency weighting and no score cap -- the spread model's
off/def diagnostic used neither, and adding them would be extra
knobs fitted on a sample the model already loses on) and
`blended_srs_scoring`, which blends seasons with the spread model's
fade philosophy (prior regressed by `PRIOR_REGRESSION`, faded out
over `PRIOR_FADE_GAMES` per team) over a pooled league average (NOT
the hardcoded `LEAGUE_AVG_POINTS`, which sits ~1 point below recent
actual scoring and would bias every SRS total low by ~2 points).
`predict_total_srs` is matchup-aware: home score = league + off[home]
- def[away], away score symmetrically. New config: `TOTALS_SRS_LIVE`
(False), `TOTALS_SRS_DAMPING` / `MAX_ITERS` / `TOLERANCE`. `model.py`
selects the totals model on that one flag and falls back to the old
blend if SRS ratings were never supplied; `weekly_run.py` computes
the SRS ratings every run so flipping the flag is a one-line change.
The weather adjustment in `model.py` applies identically on either
totals path, so it is preserved whichever model is live. The harness
(`backtest.evaluate_totals`, `--totals`) grades both models on the
same games and excludes weather from both (games.csv cannot supply a
retrospective forecast, and applying weather to one model only would
rig the comparison). The canonical spread baseline re-run inside the
same command is unchanged (49.12% ATS, MAE 10.34), confirming the
spread harness was not disturbed.

**Result -- do NOT flip `TOTALS_SRS_LIVE`.** Point-in-time
out-of-sample, 2019-2025 (n=1,871, same games as the spread harness):

- Old blend: **MAE 10.70**. SRS splits: **MAE 11.04**. Recorded
  total_line (market reference): MAE 10.36 -- both models trail the
  market on totals, as the spread model trails it on margins.
- SRS is worse in 6 of 7 seasons (2019: 11.44 vs 10.87; 2020: 10.89 vs
  10.75; 2021: 11.68 vs 11.17; 2022: 11.21 vs 10.87; 2023: 10.90 vs
  10.56; 2024: 10.47 vs 10.04) and effectively tied in 2025 (10.666 vs
  10.663, a 0.003 gap). Paired old-minus-SRS mean -0.334 pts, SE 0.072
  (z ~ -4.6): this is not a tie that a bigger sample would flip, it
  is a real out-of-sample loss. Game-level: old wins 1,008 games,
  SRS wins 834, 29 ties.
- Sensitivity checks (same harness, not promoted either): pooled
  prior+current solve 11.04, prior-regression 0.75 / 1.0 variants
  11.06 / 11.13, 50% shrinkage of the SRS deviation toward league
  average 10.76, 50/50 blend of old and SRS 10.79 -- none beats the
  old blend, and the shrinkage/blend weights were tried on the same
  data, so they cannot be claimed as walk-forward wins anyway.

**In-sample vs out-of-sample -- the reason this stays shadow.**
Fitting both models on the full target season and predicting that
same season's games (reported separately in the harness output,
never the promotion test): SRS MAE 9.72 vs old 9.97, with SRS winning
in all 7 seasons. The in-sample win (+0.25) reverses out-of-sample
(-0.33). That is the signature of estimation noise, not signal:
opponent-adjusting scoring splits each team's scoring into four
noisy ratings (two offenses, two defenses) where the plain averages
use two, and the extra variance costs more than the SOS correction
gains. The spread model's off/def split showed the same pattern
(worse MAE than single SRS), so this is a repeated result, not a
one-off. A future totals attempt should start from shrinkage of the
SRS deviation (the only sensitivity that got close) with its weight
chosen on earlier seasons and tested on later ones -- not from
fitting a weight on 2019-2025 and reporting it as a win.

**Limits that remain.** No convergence diagnostic is surfaced for
the totals solve (unlike ratings.py's `LAST_SRS_DELTA`); the solve
uses fixed config constants, so there is no fitted-parameter
walk-forward in the ensemble sense -- the "walk-forward" here is
strictly point-in-time ratings, which is the correct test for a
ratings model with no learned weights. Totals are not pool-scored
(the pool is ATS-only), so totals MAE is a model-quality metric,
not a pool edge, and no totals result changes any spread pick. No
GitHub push was made for this item (local files only).

## Backtest batch 3, second round (2026-11-04): pool-line staleness (item 1) + Elo variants (item 2) -- built, tested, NOTHING promoted

Note on naming: the earlier "Backtest batch 3 (items 1-2)" section above
is standings risk + season simulation. This section is a different
batch-3 workstream (staleness + Elo variants); the numbering collides
because the two workstreams were built in parallel. Nothing in the
earlier section was edited for this one.

Run it yourself (needs a local games.csv; no team-stats fetch, no network
inside the modules):

```bash
python - << 'PY'
import sys; sys.path.insert(0, ".")
import pandas as pd
from nflpicks import backtest, elo_variants, staleness, market
games = backtest.load_games_frame(pd.read_csv("/path/to/games.csv", low_memory=False))
out = elo_variants.evaluate_all(games, list(range(2019, 2026)))
print(out["promoted_variants"])  # [] -- see gate below
PY
```

**Item 1 -- Pool-line staleness: the true test cannot be run on
2019-2025, and the closest honest proxy is null. Flag only; no score
is wired into any pick.**

- The blocker, stated plainly so it is not papered over: games.csv
  carries ONE undated line per game (`spread_line`, a near-close
  consensus -- its vintage is undocumented). There is no pool line, no
  opener, and no line timestamp in the file, and no free multi-season
  open/close archive was found (the sources that exist are paid or
  academic-access-only, unverified here). A pool-vs-closing backtest
  over 2019-2025 is therefore impossible from available data, and no
  pool line was invented (e.g. by shifting the recorded line) to fake
  one. Any future claim about staleness must come from live data, not
  from this section's proxy numbers.
- What WAS built (`nflpicks/staleness.py`, thresholds in `config.py`
  as `STALENESS_*`, `STALENESS_LIVE=False`): a descriptive staleness
  measurement (movement = closing - pool, direction, bucket
  <1 / 1-2 / 2-3 / 3+, score = |movement| points -- no invented
  scaling), follow/fade sides, `grade_staleness()` (follow, fade, and
  optional model picks graded against the POOL line, pushes excluded
  from ATS% per the batch-2 convention), a per-bucket rule learner
  (`learn_staleness_rule` / `apply_staleness_rule`, train on earlier
  seasons, test on later ones), and a bootstrap CI. It does NOT
  reimplement the line-move flag: `db.get_line_moves()` and
  `review_queue.py` still own flagging (>= 1 pt or edge flip), and
  this module never returns a substitute line, so it cannot
  auto-switch the pool line. Nothing in `model.py`, `weekly_run.py`,
  or `dashboard.py` reads it.
- The proxy that WAS run, labelled as a proxy everywhere: the
  moneyline-implied margin (`market.ml_implied_margin`) as a second
  market reading, with the recorded spread as the "pool" line and the
  ML margin as the "closing" reading. This is cross-market
  disagreement at one snapshot, NOT movement over time -- it bounds
  the hypothesis (if even a second market reading carried no signal,
  a stale-line edge is unlikely to be large) but it cannot prove or
  disprove a true time-movement edge. Results, 2019-2025 (n=1,871,
  decided 1,828): follow-the-second-reading 49.34% ATS (bootstrap 95%
  CI [46.99, 51.75]), fade 50.66%. Follow and fade are exact opposites
  on decided games, so those two numbers are one result, not two --
  and neither clears ~52.4% breakeven. By bucket, follow is 49.10%
  (<1 pt, n=1,114 decided), 50.59% (1-2, n=593), 45.13% (2-3, n=113),
  50.00% (3+, n=8 -- meaningless). On games with a material proxy move
  (>= 1 pt, n=719): follow 49.72%, fade 50.28% -- a coin flip.
  Per season, follow ranges 45.98% (2022) to 51.87% (2024) with no
  season clearing breakeven. The walk-forward rule test fails cleanly:
  learning "fade in every bucket" on 2019-2022 (train fade 51.31% vs
  follow 48.69%) and applying it to 2023-2025 gives 49.81%, LOSING to
  always-follow on the same test games (50.19%) -- a training win that
  evaporates out-of-sample, exactly the pattern this gate exists to
  catch.
- The real test, for when live data exists: once `db.line_snapshots`
  holds pool + closing pairs for completed weeks, join them to results
  and call `grade_staleness()` on those rows (pool line = the judged
  line, closing line = the snapshot, actual margin = the result).
  Until that sample is at least a full season, do not promote any
  staleness score, and do not let a Thursday-morning move override a
  pick -- the existing flag is the review prompt, not a pick change.
- Dashboard hook (NOT wired -- `dashboard.py` was not edited, per the
  task constraints): a future "Line staleness" section could call
  `staleness.staleness(pool, closing)` per game for display and
  `grade_staleness()` over completed weeks for the running
  follow/fade record. That wiring waits for the live snapshot sample
  above; wiring it now would display an empty section, not evidence.

**Item 2 -- Elo variants: 11 one-at-a-time variants tested, none
promoted. Baseline Elo stays exactly as batch 2 built it.**

- Method (`nflpicks/elo_variants.py`; `elo.py` gained only optional
  overrides -- `k`, `margin_mode`, `hfa_points`, `k_multiplier` on
  `apply_week()`, `fraction` on `regress_state()` -- all defaulting to
  the existing config values, so live/default behaviour is unchanged
  and the old tests pass unmodified). Point-in-time throughout:
  pre-week Elo ratings, burn-in seasons 2017-2018 scored nowhere,
  state regressed at each season start, point-in-time empirical HFA
  in the Elo->margin conversion (0 at neutral sites), batch-2 push
  grading. The baseline reproduces batch 2 EXACTLY (52.52% ATS,
  MAE 10.39, n=1,871, decided 1,828; per-season 53.66 / 58.20 / 54.85 /
  51.72 / 48.45 / 51.87 / 49.08 for 2019-2025), which validates the
  harness before any variant is believed.
- Grid (baseline K=20, rollover 0.5, HFA-points 55, win/loss only,
  flat K): K 10 / 32 / 40; rollover 0.25 / 0.75 / 1.0; HFA-points
  0 / 110; margin "log" (1 + 0.3*log2(1+|margin|), capped 2.0x) /
  "capped" (1 to 1.5x linear); recency late-boost (K x0.75 weeks < 10,
  x1.25 weeks >= 10). No full factorial and no post-hoc "best of OAT"
  combination was tested -- on 1,871 games a ~100-variant search
  would guarantee a spurious winner.
- Full-sample results (paired vs baseline Elo, decided n=1,828;
  Bonferroni across all 11 variants; bootstrap 2,000 resamples,
  seed 20261104):

  | variant | ATS% | paired delta | paired z | p (Bonf.) | boot CI (pp) |
  |---|---|---|---|---|---|
  | baseline | 52.52 | -- | -- | -- | -- |
  | k10 | 51.91 | -0.60 | -0.93 | 1.0000 | [-1.91, 0.66] |
  | k32 | 50.66 | -1.86 | -2.62 | 0.0958 | [-3.28, -0.49] |
  | k40 | 50.27 | -2.24 | -2.40 | 0.1786 | [-4.10, -0.44] |
  | roll025 | 52.57 | +0.05 | +0.09 | 1.0000 | [-1.15, 1.26] |
  | roll075 | 50.27 | -2.24 | -3.01 | 0.0283 | [-3.67, -0.82] |
  | roll10 | 50.11 | -2.41 | -2.05 | 0.4472 | [-4.60, 0.00] |
  | hfa0 | 52.46 | -0.05 | -0.23 | 1.0000 | [-0.49, 0.44] |
  | hfa110 | 52.57 | +0.05 | +0.23 | 1.0000 | [-0.38, 0.49] |
  | margin_log | 50.55 | -1.97 | -2.25 | 0.2689 | [-3.72, -0.27] |
  | margin_capped | 51.37 | -1.15 | -2.15 | 0.3432 | [-2.19, -0.16] |
  | recency_late_boost | 51.70 | -0.82 | -1.89 | 0.6466 | [-1.75, 0.00] |

  Reading the table honestly: the two variants that edge baseline on
  the full sample (roll025 +0.05 pp, hfa110 +0.05 pp) are one game in
  1,828 -- noise, and both LOSE on the training split, so they fail
  the promotion gate at the first hurdle. Most variants are worse
  than baseline, and several are nominally significantly worse
  (k32, k40, margin_log, margin_capped) -- but after Bonferroni only
  one survives, and it is worse, not better: roll075 (raising the
  rollover from 0.5 to 0.75) at -2.24 pp, Bonferroni p=0.0283,
  bootstrap CI entirely below zero. That is a warning against raising
  the rollover, not a promotion. HFA-points 0 vs 110 barely matters
  (19 and 19 discordant games respectively) because the Elo->margin
  conversion uses the point HFA separately -- the Elo-points HFA only
  nudges expected win probabilities.
- Promotion gate (all four required: win on 2019-2022 train, win on
  2023-2025 test, Bonferroni p < 0.05, bootstrap CI excludes zero):
  baseline train 54.61% / test 49.81%. NO variant wins both splits --
  the best test split among variants is roll025 at 50.06% (+0.25 pp
  over baseline test) after losing train (54.51% vs 54.61%), and
  hfa110 at 49.94% (+0.13 pp) after tying train. The season
  inconsistency from batch 2 is unchanged: baseline Elo is 58.20% in
  2020 and 48.45% in 2023 in this harness, and no variant fixes the
  weak seasons (every variant is at or below 49.22% in 2023 -- none
  clears 50% there; 2025 is <= 49.82% for every variant).
  `ELO_K`, `ELO_PRIOR_REGRESSION`, and `ELO_HFA_POINTS` therefore stay
  at 20 / 0.5 / 55, and there is still no `ELO_LIVE` flag to flip --
  Elo remains a shadow component of the (also unpromoted) ensemble.
- MAE footnote, not hidden: larger K and margin variants slightly
  improve MAE (k40 10.26, margin_log 10.24, margin_capped 10.29 vs
  baseline 10.39) while worsening ATS. The pool scores ATS, so an MAE
  gain that costs ATS points is not a promotion case, and MAE was not
  part of the gate -- but a future Elo attempt that claims an MAE win
  must show it does not repeat this trade.
- In-sample vs out-of-sample, stated separately as required: the
  "full-sample best" variant is roll025/hfa110 tied at 52.57% (+0.05 pp)
  -- that is selection on the same sample it is scored on, worth
  exactly one game, and it is NOT an out-of-sample result. The
  out-of-sample result is the train/test gate above, and it is empty.
  A future attempt should pre-register ONE variant (the natural
  candidate from this sweep is none -- the sweep argues the baseline
  is already at its local optimum for these knobs) and test it on
  seasons after 2025, not re-search 2019-2025.

**Still off by default, on purpose (this round):** `STALENESS_LIVE=False`
(new), `ELO_K=20` / `ELO_PRIOR_REGRESSION=0.5` / `ELO_HFA_POINTS=55`
(unchanged), plus every flag from earlier batches
(`EPA_LIVE`, `ENSEMBLE_LIVE`, `CALIBRATED_PICK`, `TRAVEL_ENABLED`,
`INJ_AUTO_APPLY`, `TOTALS_SRS_LIVE` -- all False, unchanged).

**Limits that remain.** The staleness conclusion is a non-result with
a named cause (missing second line), not evidence that line movement
does not matter -- a real pool-vs-closing sample could still show an
effect, in either direction, and the proxy's 2-3 pt bucket (follow
45.13% / fade 54.87%, n=113) is too small and too proxy-dependent to
support fading moves either. The Elo sweep covers only simple knobs;
it says nothing about Elo blended with SRS (the ensemble, already
null), Elo with a different margin scale, or Elo trained on more
seasons. Canonical spread baseline re-run inside this work is
unchanged (49.12% ATS, MAE 10.34), confirming neither module disturbed
the spread harness. No GitHub push was made for this batch (local
files only).

## Backtest batch 3, third round (2026-11-04): error decomposition (item 3) + actual pool results (item 9) + freshness warnings (item 10) -- built, tested, NOTHING promoted

Note on naming: this is a third batch-3 workstream (decomposition /
pool results / freshness), parallel to the standings/simulation
workstream and the staleness/Elo workstream above. Nothing in those
sections was edited for this one. No flag is flipped, no pick is
changed, and no GitHub push was made (local files only).

Run the decomposition yourself (needs a local games.csv; no network
inside the modules):

```bash
python -m nflpicks.backtest --games-csv /path/to/games.csv --seasons 2019-2025 --variants baseline --decompose
```

**Item 3 -- Error decomposition (`nflpicks/error_decomposition.py`,
thresholds in `config.py` as `ERROR_*`).** Point-in-time backtest rows
(or graded live picks) are sliced by spread bucket (|line|: pick'em /
short / medium / large), favorite/dog, home/away pick side, season
phase (weeks 1-4 / 5-9 / 10+), picked team, total bucket, weather
(wind: calm / moderate 15-20 mph / high 20+ mph, the totals model's
thresholds), and division. Every slice reports n, decided games,
wins/losses/pushes, ATS% (pushes excluded, batch-2 convention), MAE,
line MAE, MAE gap, and mean signed error (which direction the model
misses). Two guards against cherry-picking, built in rather than
advised: a slice with fewer than `ERROR_MIN_DECIDED` = 30 decided
games is flagged `small_sample`, and the worst-slice ranking only
considers slices at/above that floor -- a 2-0 slice is noise, not a
finding. Total / division / wind context is joined from the games
frame by `attach_context()`; a game whose source row lacks one lands
in that dimension's "unknown" bucket, and the report states coverage
counts (known/unknown per dimension) instead of imputing a value.
The harness flag `--decompose` attaches the full decomposition to
each variant's JSON output; the dashboard's Model Grades tab runs
the same code over the current season's saved picks vs actual
results (MAE there uses the raw model spread where available).
A losing slice is a target for a future walk-forward fix, NOT a
reason to flip picks in that slice: the slice was found on the same
data it would be "fixed" on, so acting on it without an
out-of-sample test repeats the exact error this module exists to
prevent. No decomposition result changes any pick, and no
2019-2025 slice ranking is claimed here -- the module is the tool,
and its first real run on the full harness is still to be read,
not pre-announced.

**Item 9 -- Actual pool results grading (`nflpicks/pool_results.py`,
dashboard Model Grades tab).** Distinct from `adjustment_grading.py`
(adjusted vs unadjusted games): this grades the slate actually
submitted to Barbara's pool against the model slate, game by game,
on the same judged line (pool line where set, else live Vegas).
CSV format (header required; `note` optional, stored but never
graded):

```csv
season,week,away_team,home_team,submitted_team,note
2026,1,DEN,KC,KC,flipped on Thursday review
2026,1,BUF,MIA,MIA,
```

`submitted_pick` (a pick string like "KC -3.5") or `pick` may replace
`submitted_team`; if both are present, `submitted_team` wins. A
submitted team that plays in neither team of its matchup is
REJECTED with an error, not guessed; a duplicate matchup in one
file is superseded by its last row, noted in the import errors.
Manual entry (one game at a time, from the week's saved picks) is
in the same dashboard section, next to a downloadable sample CSV
that is labelled SAMPLE ONLY wherever it appears. Storage is the
module's own `pool_results` table (created on first use, upserted
on season/week/matchup), so `db.py` was not edited for this item.
Per-game outcomes: `same` (picks agree), `diff_helped` (submitted
correct, model wrong), `diff_hurt` (the reverse), `diff_neutral`
(differing picks on a push -- the only way differing picks avoid
splitting correct/wrong, since ATS is binary once pushes are
excluded). Games with no model pick or no result are ungraded,
never invented. With no imported rows the grade is an explicit
empty state -- no record at all, not 0-0 -- and the dashboard says
exactly that. Judge the net difference over a season, not one
week. No real results were imported or invented for this build:
all tests use synthetic sample data labelled as sample.

**Item 10 -- Data-freshness and failure warnings
(`nflpicks/freshness.py`, thresholds in `config.py` as
`FRESHNESS_*`, dashboard banner + ATS-tab as-of caption).** Three
checks, all from data the app can actually read: the latest picks
`run_time` in the database (the data as-of date), its age against
`FRESHNESS_WARN_HOURS` = 96 and `FRESHNESS_STALE_HOURS` = 168 (7
days -- a week-old slate is never presented as today's), and
whether the last scheduled workflow run (Tue/Thu 15:00 UTC, the
weekly.yml cron) was missed (latest saved run predates that slot
and now is past the slot plus `FRESHNESS_RUN_GRACE_HOURS` = 3).
Failed `run_diagnostics` steps escalate an otherwise fresh run to
"warn". Levels: ok (silent), warn (banner), stale (red banner:
do not submit from this slate). The dashboard banner sits above
every tab, and the ATS tab captions each week's picks with their
saved run_time, so staleness cannot hide inside a tab. The
workflow gains one step, before the weekly run, that reports the
PREVIOUS run's status (`python -m nflpicks.freshness --no-fail`,
report-only by flag, never blocking -- a stale previous run is
what the run is about to fix); cron (`0 15 * * 2,4`),
`contents: write`, and no-email behaviour are unchanged, and the
fail-fast unit-test step's guard (no swallowed failures before
the weekly run) still passes, since the step uses a flag rather
than a shell swallow. What is NOT claimed: live GitHub Actions
status. The app has no Actions token, so the banner, the CLI, and
the workflow step all state that the Actions tab on GitHub is the
live source, and a missed-run warning means "no saved run from
the last scheduled slot", not proof of a workflow failure. The
weekly_run.py file itself was deliberately not edited for this
item (another batch-3 workstream owns workflow-adjacent changes
there); the check lives in the dashboard, the CLI, and the
workflow step, as the task allowed.

**Still off / unchanged by this round:** every live flag from
earlier batches stays as it was (`EPA_LIVE`, `ENSEMBLE_LIVE`,
`CALIBRATED_PICK`, `TRAVEL_ENABLED`, `INJ_AUTO_APPLY`,
`TOTALS_SRS_LIVE`, `STALENESS_LIVE` -- all False; no new live flag
was added, because none of these three items is a pick input).
`staleness.py`, `elo_variants.py`, `elo.py`, `weekly_run.py`, and
`db.py` were not edited; the pool-line importer, manual-adjustment
persistence, Thursday workflow/diff, rating-uncertainty, and
play-by-play files belonging to other batch-3 items were not
touched either.

**Limits that remain.** Streamlit is not installed in the build
environment, so `dashboard.py` was compile-checked only, never
clicked through live; the new Model Grades sections (pool-results
import/grading, error decomposition) and the freshness banner need
a live run to confirm rendering. The dashboard decomposition covers
only the current season's saved picks, so early in a season its
slices will mostly be small-sample by design -- the harness
`--decompose` run over 2019-2025 is where slice sizes become
interpretable. Pool-results grading inherits the judged-line
caveat: it compares against the pool line where set, else the
live Vegas line saved with the picks, which is not a verified
Barbara-sheet line unless the override was entered. Freshness
checks the database the dashboard reads; if a deployment's
database differs from the workflow's committed database (the
known Streamlit Cloud caveat in the dashboard notes), the banner
reports that deployment's freshness, which is the correct scope
for what that deployment is showing, but it is not a check on the
other database.

## Backtest batch 3, fourth round (2026-11-04): pool-line importer (item 4) + manual-adjustment persistence (item 5) -- built, tested, NOTHING promoted

Note on naming: this is a fourth batch-3 workstream (pool lines /
adjustment file), parallel to the standings/simulation, staleness/
Elo, and decomposition/pool-results/freshness workstreams above.
Nothing in those sections was edited for this one. No flag is
flipped, no pick is changed, and no GitHub push was made (local
files only).

**Item 4 -- Backtest against actual Barbara pool lines
(`nflpicks/pool_lines.py`, thresholds in `config.py` as
`POOL_LINES_CSV_PATH`).** Distinct from item 9's grading: item 9
grades the slate actually SUBMITTED against the model slate; this
item re-grades the model's point-in-time picks against two
different LINES (recorded vs pool), so the pool line becomes the
judged line in the backtest/grading harness instead of the
nflverse recorded line.

CSV format (header required; `pool_total`, `note`, and
`convention` optional; `pool_total` is stored but never graded --
the pool is ATS-only):

```csv
season,week,away_team,home_team,pool_spread,pool_total,note,convention
2024,1,DEN,KC,-3.5,,flipped on Thursday review,sportsbook
2024,1,BUF,MIA,2.5,,,
```

Conventions -- the sign-flip bug lived here, so this is explicit:
`pool_spread` is SPORTSBOOK convention by default, exactly as a
pool sheet prints it (home favorite negative), matching the
dashboard's pool-line form. The parser converts to nflverse
convention (positive = home favored) on parse via
`config.to_nflverse_spread`, and parsed rows keep both the
as-entered value and the resolved convention, so any sign question
traces back to the source row. A per-row `convention` column may
say `sportsbook` or `nflverse`; anything else REJECTS the row
with an error, it is not guessed. Validation mirrors
pool_results.py: season/week must be integers, teams must be
present, the spread must be numeric, and a duplicate matchup in
one file is superseded by its last row, noted in the errors.

Grading (`grade_pool_lines()`): the SAME rule as the canonical
harness (`backtest.evaluate_variant`) is applied to both lines --
actual > line: correct iff model margin > line; actual < line:
correct iff model margin < line; actual == line: push. That is
deliberately NOT the batch-2 `_ats_rows` rule (margin == line is
a push there); using one rule for both lines means any
recorded-vs-pool difference is purely a line difference, never a
grading-rule difference. A game enters the comparison only when
it has a model margin, both lines, and a result; games with no
pool line are excluded from BOTH sides, so the two ATS% are on
identical game sets. Output: recorded vs pool ATS, the delta in
percentage points, pick-flip counts (games where the model side
differs between the two lines), per-season breakdowns, and
unmatched counts. `attach_pool_lines()` joins pool lines onto
backtest rows for callers that want the per-game table; it drops
unmatched rows rather than filling them with the recorded line,
which would silently report recorded-line results as pool-line
ones.

Run it yourself:

```bash
python -m nflpicks.pool_lines --pool-csv /path/to/pool_lines.csv \
    --games-csv /path/to/games.csv --seasons 2019-2025
python -m nflpicks.backtest --games-csv /path/to/games.csv \
    --seasons 2019-2025 --variants baseline --pool-lines-csv /path/to/pool_lines.csv
```

(The second form attaches `pool_line_comparison` to each
variant's summary; for the `calibrated_pick` variant it grades
the shrunk pick margin on both lines, matching how that variant
is graded in the harness. Set `POOL_LINES_CSV` to a slate path
to make `load_pool_lines_file()` find it without a flag.)

**No real pool-line result is reported here, because no real
pool lines were supplied -- and none were invented.** With no
slate, every entry point reports an explicit empty state (status
"empty", no record at all, not 0-0): the module, the CLI, and
the backtest flag. The only numbers ever run through this code
are synthetic sample data, labelled SAMPLE wherever they appear
(`pool_lines.SAMPLE_CSV`, and the tests in
`tests/test_batch3_items45.py`). As a mechanics illustration
only -- NOT a finding about real pool lines -- a 4-game synthetic
run (model margins from a toy schedule, recorded line 3.0
throughout, pool lines -3.5/+2.5 sportsbook) grades recorded
4-0 vs pool 2-2, delta -50.0 pp, 2 pick flips. That delta is an
artifact of four invented games; it says nothing about Barbara's
real lines, and must not be quoted as a pool-line effect. The
real test is still to come: once a genuine past slate is
imported, the same command reports the true pool-vs-recorded
difference on that sample, and only then can anyone say whether
the recorded line flattered or understated the model.

**Item 5 -- Manual-adjustment persistence fix
(`nflpicks/manual_adjustments.py`, `manual_adjustments.json` in
the repo, thresholds in `config.py` as
`MANUAL_ADJUSTMENTS_FILE` / `MANUAL_ADJUSTMENTS_SCHEMA_VERSION`).**
This supersedes the "Getting the dashboard on your phone"
caveat above that Cloud adjustments can be lost on redeploy --
for the weekly run's side, at least. The versioned repo file is
now the source of truth the weekly run reads:

```json
{"schema_version": 1, "revision": 3, "updated_at": "...",
 "adjustments": [{"team": "KC", "points": 1.5, "reason": "...",
                  "updated_at": "...", "updated_by": "..."}],
 "history": [{"timestamp": "...", "action": "set", "team": "KC",
              "points_before": null, "points_after": 1.5,
              "reason": "...", "actor": "..."}]}
```

`revision` increments on every mutation; `history` is
append-only (a delete appends a "delete" event instead of
erasing the past; an import appends events only for teams it
actually changes, and moves the revision only if something
changed). Every event records who/what/when/why where available
-- the dashboard passes an empty actor when it does not know
one, and the event is still recorded with timestamp, team,
before/after points, and reason, rather than dropped. Writes are
atomic (temp file in the same directory + os.replace), so a
crash mid-write cannot leave half-written JSON. A present-but-
corrupt file (bad JSON, wrong schema version, bad shape) raises
instead of silently reading as empty -- silently emptying would
silently drop adjustments.

Precedence, stated so it is not misread as "file wins outright":
the weekly run merges the file over the database, per team (file
value wins where both have the team; a database-only team still
applies until exported). The naive file-wins-outright rule would
silently discard Cloud-database adjustments that have not been
exported yet; the merge rule loses nothing, and once the file
covers every team (the normal state after one export + commit)
the merge and the file agree exactly. The run records which
source the counts came from as a diagnostic, so the merge is
visible. The file also supplies per-team reasons to the decision
log, where the old database-only path passed none.

Adjustment semantics are unchanged: adjustments still feed into
ratings exactly as before (`model.predict_week`'s `manual_adj`,
added to the team's rating). This item changes only where they
are stored and read from, never what they do. The existing
dashboard Manual Adjustments tab keeps working exactly as
before (add/remove forms write the deployment database); it
gains an export/import section, and that section is honest about
the one thing it cannot do: **a Streamlit Cloud deployment
cannot write the repo file** -- it has no repo write access, and
no code here pretends otherwise. What the tab does: Export
downloads the merge of the file and the database (file wins per
team) with its audit history, for committing as
`manual_adjustments.json`; Import parses an uploaded file and
writes it into that deployment's database only (merge, not wipe
-- `parse_import_entries` never touches the file), so the
adjustments work in that deployment, and the tab says to commit
the file itself for the weekly run to see the change. The
committed file in this batch is the empty-state document
(schema 1, revision 0, no adjustments) -- an empty file plus the
merge rule means existing database-only adjustments keep
applying, so committing it drops nothing. If the weekly run
ever finds the file corrupt, it records a failed diagnostic and
falls back to the database for that run rather than running
adjustment-free in silence.

**Still off / unchanged by this round:** every live flag from
earlier batches stays as it was (`EPA_LIVE`, `ENSEMBLE_LIVE`,
`CALIBRATED_PICK`, `TRAVEL_ENABLED`, `INJ_AUTO_APPLY`,
`TOTALS_SRS_LIVE`, `STALENESS_LIVE` -- all False; no new live
flag was added, because neither item is a pick input).
`staleness.py`, `elo_variants.py`, `error_decomposition.py`,
`pool_results.py`, `freshness.py`, `.github/workflows/weekly.yml`
(cron, `contents: write`, and no-email behaviour all untouched),
and the Thursday workflow/diff, rating-uncertainty, and
play-by-play files belonging to other batch-3 items were not
touched.

**Limits that remain.** Streamlit is not installed in the build
environment, so `dashboard.py` was compile-checked only, never
clicked through live; the new Manual Adjustments export/import
section needs a live run to confirm rendering (the import path
was unit-tested at the parse level, and the export builder at
the doc level, but not through the Streamlit widgets). The
pool-line comparison is only as good as the slate fed to it: a
pool line transcribed with the wrong convention column, or a
week mislabelled, grades the wrong games silently -- the parser
keeps the as-entered value per row precisely so a suspect week
can be re-checked against the sheet, but it cannot detect a
transcription error on its own. No real past pool slate exists
in the repo yet, so the pool-line ATS difference on real lines
remains unmeasured, and the Cloud-to-repo step for adjustments
remains manual (export + commit) until a repo-writing path
exists for the deployment.

## Backtest batch 3, fifth round (2026-11-04): Thursday 10 AM final run + diff (item 6) + rating uncertainty (item 7) -- built, tested, NOTHING promoted

Note on naming: this is a fifth batch-3 workstream (final run /
diff / uncertainty), parallel to the standings/simulation,
staleness/Elo, decomposition/pool-results/freshness, and pool-lines /
adjustment-file workstreams above. Nothing in those sections was
edited for this one, and none of their files was touched
(`staleness.py`, `elo_variants.py`, `error_decomposition.py`,
`pool_results.py`, `freshness.py`, `pool_lines.py`,
`manual_adjustments.py`, and all item-8 play-by-play files are
unchanged). No flag is flipped, no pick is changed, and no GitHub
push was made (local files only).

**Item 6 -- Thursday 10 AM final run + what-changed diff
(`.github/workflows/weekly.yml`, `nflpicks/run_diff.py`,
`weekly_run.py`, `nflpicks/db.py` run_diffs table, dashboard
Submit & Review tab).** The workflow gains a second Thursday
schedule, `0 18 * * 4`, alongside the existing `0 15 * * 2,4`.
Winter offset, verified rather than guessed: PST is UTC-8, so
18:00 UTC = 10:00 AM PT, one hour before Barbara's 11:00 AM PT
deadline. The existing Tue/Thu 7 AM runs are intact, and so are
the tests-first step and the freshness check step -- the final
run reuses the same job end to end (tests, freshness check,
weekly run, grading, commit), it is not a separate diff-only
job.

Why reusing the same job cannot corrupt the 7 AM run's commit:
every save in `weekly_run.py` is an upsert keyed by (season,
week, matchup), so the 10 AM run overwrites the 7 AM slate in
place exactly as the 7 AM run overwrote Tuesday's -- there is no
second database to reconcile and no merge to get wrong. The one
thing that would be lost by overwriting is the old slate itself,
and the diff is built to survive that: `weekly_run` captures the
previous saved run for the week in memory BEFORE its first save
(`_capture_previous_state`, called right after the upcoming
week is known and before `save_ratings_snapshot`), diffs the new
predictions against it in memory, and persists only the diff in
the new `run_diffs` table, keyed by run_time. Each run appends
its own rows; a later run never touches an earlier run's diff,
so the 10 AM diff (vs the 7 AM slate) and the 7 AM diff (vs
Tuesday's slate) both survive. A `concurrency` group
(`weekly-picks`, `cancel-in-progress: false`) serializes
overlapping runs, so a delayed 7 AM run finishes before the
10 AM run starts instead of two runners pushing competing DB
commits from different checkouts; a queued run waits rather
than killing a run mid-commit.

What the diff reports (`run_diff.build_diff`, thresholds in
`config.py` as `RUN_DIFF_MIN_*`): pick flips (always reported --
a flip is a different submission, not a rounding wobble); Vegas
or pool line moves >= 0.5 pts; blended edge moves >= 0.5 pts
without a flip (a flip already carries its edge change, so it
is not double-reported); team rating moves >= 0.5 pts
(team-level, since one rating move explains several games' edge
moves); injury list changes per player (newly listed, status
changed, no longer listed -- a Thursday-morning clearing is
reported, not hidden); weather-note changes (totals only); total
pick changes or model-total moves >= 0.5 pts; and matchups
appearing or vanishing (should not happen midweek). Items sort
flips first, then line moves, injury changes, edge moves,
rating moves, weather, totals, schedule changes; within a
category, largest magnitude first. The dashboard's Submit &
Review tab shows the latest run's diff rows (flips first) with
the run timestamp, and the weekly run logs the same summary to
the Actions log and as a Data Health diagnostic, so the movers
are visible without opening the dashboard. The first run of a
week reports "first saved run -- nothing to compare against"
rather than an empty diff pretending nothing changed, and a run
whose diff is empty above the thresholds says so explicitly.
A diff row is a prompt to re-check a game before submitting,
never an auto-switch: the pool line stays the judged line.

DST handling, stated plainly because the cron is fixed UTC:
GitHub cron does not follow daylight saving. In PST months
(Nov-Mar, most of the season including the playoff push),
18:00 UTC = 10:00 AM PT as intended. In PDT months (Mar-Nov,
including September-October early season), 18:00 UTC =
11:00 AM PT -- AT the deadline, not before it. The final run
still refreshes data in PDT, but a slate landing at deadline
time cannot be reviewed before submitting, so in PDT months
the 15:00 UTC run (8:00 AM PT) is the review slate and the
dashboard's Refresh Picks Now button is the pre-deadline
refresh. A timezone-aware schedule was deliberately not added:
it would need either two season-dependent crons that both fire
year-round or a wrapper script, and this item's brief specified
the winter offset. If the PDT landing becomes a real problem
in September, the honest fix is a second cron at 17:00 UTC
(10 AM PDT / 9 AM PST) -- NOT moving 18:00 UTC, which would
break the winter 10 AM landing. GitHub scheduled runs can also
start late, so every run time here is approximate.

**Item 7 -- Rating uncertainty (`nflpicks/uncertainty.py`,
thresholds in `config.py` as `UNCERTAINTY_*`, dashboard ATS
cover-prob range + Data Health rating bands).** Every rating
now carries a Bayesian-style band: rating treated as
Normal(mean = blended SRS rating, sd = `rating_sd(games)`),
where 1/var = 1/prior_var + ESS/game_var. The prior sd is 5.0
pts (a judgment parameter: a prior-season rating carried
across an offseason is genuinely less certain than a mid-season
one); the per-game noise sd is 13.43 pts (the 2019-2025
point-in-time backtest residual SD from `calibration.py`,
which already includes opponent-rating error and game noise,
so it is conservative -- wide -- by construction); ESS is the
recency-weighted effective game count, (sum w)^2 / (sum w^2)
under the SRS 5-game half-life, which saturates: a full
17-game season is only ~12 effective games, so late-season
bands stop narrowing. Resulting rating SDs with the shipped
defaults: 0 games 5.00, 1 game 4.69, 4 games 4.03, 8 games 3.53,
12 games 3.25, 17 games 3.07. A +6.2 rating's 90% band is
-2.0 to +14.4 at 0 games (the sign itself is uncertain) and
+1.2 to +11.2 at 17 games. The band is model uncertainty, not
a guarantee, and `uncertainty.py` says what it leaves out:
HFA uncertainty (ignored; understates margin variance
slightly) and covariance between the two teams' ratings
(ignored; if rating errors are positively correlated through
shared opponents, the true margin variance is smaller, so
these margin bands are wide, not tight). The blended rating's
fade weight (w = n/12) is deliberately NOT reused: that fade
is a point-estimate shrinkage rule, while this is a
precision-weighted uncertainty over the same evidence, and
neither claims to be the exact posterior of the iterative SRS
solve, which has no closed form -- "Bayesian-style" is the
honest label.

Feeding cover probabilities and standings gambles, labels
only: the dashboard's ATS tab gains a Cover Prob Range column
-- the calibrated cover probability evaluated through the same
empirical CDF at the model margin shifted by +/-1 margin SD
(sqrt of the two teams' rating variances) -- and the Data
Health ratings table gains a 90% band column. The point
probability and the pick never change; the range only says how
much weight the point can bear. Worked example (same numbers
the tests pin): model margin +3.5 vs line +2.5 has point cover
probability 0.498 -- already a coin flip -- with a range of
0.345-0.656 at 0 games and 0.402-0.601 at 17 games on both
sides. Even late in the season, a 1-point edge does not survive
rating uncertainty: a range straddling 0.50 means the rating
bands alone could flip which side is favoured, and for
standings-gamble purposes that game should be treated as a
coin flip whatever the point estimate says. The range always
contains the point probability (the CDF is monotone) and
always narrows with games played. Both dashboard additions are
best-effort: a missing input skips the column rather than
breaking the tab.

**Still off / unchanged by this round:** every live flag from
earlier batches stays as it was (`EPA_LIVE`, `ENSEMBLE_LIVE`,
`CALIBRATED_PICK`, `TRAVEL_ENABLED`, `INJ_AUTO_APPLY`,
`TOTALS_SRS_LIVE`, `STALENESS_LIVE` -- all False), and
`UNCERTAINTY_PICK_USE` is False with no code path that would
change a pick from a band even if it were flipped: wiring one
in would first need a walk-forward test showing
uncertainty-adjusted picks beat the current picks
out-of-sample, which has not been run. No pick logic reads
`uncertainty.py` (the tests assert the model module never
imports it). The pool line is never auto-switched by the diff
or by any band.

**Limits that remain.** Streamlit is not installed in the build
environment, so `dashboard.py` was compile-checked only, never
clicked through live; the new Submit & Review diff section,
the ATS cover-prob range column, and the Data Health band
column need a live run to confirm rendering (their pure
functions -- diff building, band math, DB round-trip -- are
unit-tested). The new 10 AM run has not been observed live:
the cron, concurrency group, and diff plumbing are in place,
but the first real Thursday will be the first proof that the
run fires, commits, and diffs against the 7 AM slate -- and
that its PDT landing (11 AM PT in September-October) behaves
as documented above. `freshness.py` was not edited (it belongs
to the item-10 workstream), so its missed-run logic still only
tracks the Tue/Thu 15:00 UTC slots: a missed 18:00 UTC final
run is NOT flagged by the freshness banner, because the 15:00
UTC run from the same morning still counts as fresh -- the
Actions tab remains the source for whether the final run
fired. The uncertainty constants are only partly fitted: the
13.43 game noise comes from the backtest, but the 5.0 prior sd
is judgment, and the whole band model assumes independent
game noise and a Normal rating shape -- if a future fit
revises either, the tests pin the shape (monotone narrowing,
correct limits, interval containing the point) so the change
shows up there, not silently in the dashboard.

## Backtest batch 3, sixth round (2026-11-04): play-by-play source confirmation + true early-down / red-zone features (item 8) -- built, tested, NOTHING promoted

Note on naming: this is a sixth batch-3 workstream (play-by-play
features), parallel to the standings/simulation, staleness/Elo,
decomposition/pool-results/freshness, pool-lines/adjustment-file,
and final-run/uncertainty workstreams above. Nothing in those
sections was edited for this one, and none of their files was
touched. No flag is flipped, no pick is changed, and no GitHub
push was made (local files only).

**Item 8a -- The source IS confirmed (this closes the batch-2
gap).** Batch 2 could not build true early-down success or
red-zone efficiency because guessed play-by-play URLs 404'd.
The guesses failed on the release TAG, not the filename:
nflreadr's documentation (`load_pbp`: "Loads play by play
seasons from the nflverse-data repository", file types rds/qs/
csv/parquet) and its loader source (`nflverse_download` /
`nflverse_releases` via piggyback on repo
"nflverse/nflverse-data") name the repository, and the GitHub
REST API listing for that repository's `pbp` release (id
58152862, updated 2026-11-04 -- the directory listing
piggyback's `pb_list` reads, not a guessed file URL) lists, per
season, `play_by_play_{season}.csv`, `.csv.gz`, `.parquet`,
`.qs`, `.rds`. Verified live during this build: the 2024 csv.gz
returned HTTP 200 at exactly the listed 19,362,351 bytes
(sha256 23370d5d10f8104d80d46a1fc5e61f4f6f5a3263fe96fe2dd629913cfcb08,
372 columns), and seasons 2017-2025 each returned HTTP 200 at
~18-19 MB. The template in `config.py`
(`PBP_URL_TMPL`, tag `pbp`, stem `play_by_play_`) is that
listed URL, and `pbp_features.pbp_url(2024)` is pinned to it
in the tests. The earlier "filename could not be confirmed"
lines in the batch-2 section and in `features.py` are now
superseded for the source question only -- the batch-2 proxy
features themselves are untouched.

**Item 8b -- True features, built (`nflpicks/pbp_features.py`,
thresholds in `config.py` as `PBP_*`, `PBP_FEATURES_LIVE`
False).** Definitions, stated so they can be checked:
early-down success = share of 1st/2nd-down pass/run plays
(sacks count as pass, scrambles as run; kneel, spike, no_play,
kicks excluded) gaining at least 40% of ydstogo on 1st down or
60% on 2nd (Football Outsiders 40/60 convention), or a first
down / touchdown -- computed from down/ydstogo/yards_gained/
first_down/touchdown, NOT from the file's own `success` column
(which gives 43.8% on 2024 early-down plays, an EPA-style
definition, 90.9% agreement with the yardage rule; using it
would silently change the feature). Sanity: the yardage rule
gives a 50.4% league early-down success rate on 2024.
Red-zone efficiency = scoring trips / trips, where a trip is
a possession segment (game_id, drive, posteam) with at least
one pass/run play starting at yardline_100 <= 20, and a trip
scores if any play in it has td_team == posteam; kneel-only
segments are not trips, field-goal segments are trips without
a touchdown. Defensive rates mirror both (success/TD rate
allowed as defteam); a team's strength is off - allowed, and
a game's differential is home strength minus away strength,
with a missing side reading as league average (never 0.0).
Only regular-season plays count; weekly counts are the unit,
so the point-in-time filter (season-S weeks before W plus
full season S-1) cannot leak future weeks. Coefficients are
clipped least squares fit on completed seasons before S only
(plus burn-in 2017-2018, scored nowhere), exactly like the
batch-2 feature fits.

Run it yourself (needs local play-by-play files -- the module
downloads them from the confirmed URL on demand -- plus a
local games.csv; no team-stats fetch, no network inside the
grading itself once the files exist):

```bash
python - << 'PY'
import sys; sys.path.insert(0, ".")
import pandas as pd
from nflpicks import backtest, pbp_features as pf
games = backtest.load_games_frame(pd.read_csv("/path/to/games.csv", low_memory=False))
pbp = {s: pf.load_pbp_frame(s) for s in range(2017, 2026)}
out = pf.evaluate_pbp_features(games, list(range(2019, 2026)), pbp)
print(out["pbp_all"]["ats_pct"], out["pbp_all"]["paired_vs_baseline"])
PY
```

**Results -- do NOT flip `PBP_FEATURES_LIVE`.**
Point-in-time out-of-sample, 2019-2025 (n=1,871, decided
1,828), canonical grading (margin == line counts wrong when
the game is decided -- the same rule as the 49.12% baseline;
see the placebo warning below before quoting any paired z):

- Baseline (re-run inside this evaluation, unchanged):
  **49.12% ATS, MAE 10.34** -- the canonical numbers reproduce
  exactly, confirming this work did not disturb the harness.
- Early-down alone: **49.67% ATS, MAE 10.35** (paired +0.55
  pp, z=1.12, discordant 45/35 -- noise; MAE worse than
  baseline). Per season: 49.19 / 52.34 / 46.64 / 52.11 / 48.45 /
  52.24 / 46.86 (2019-2025). Coefficients: -6.54 (2019, fit on
  burn-in only -- sign-flipped vs every later year), then
  4.12 / 7.81 / 9.61 / 9.65 / 11.03 / 11.75 -- positive and
  growing, but the ATS gain never clears noise.
- Red-zone alone: **50.11% ATS, MAE 10.34** (paired +0.98 pp,
  z=4.02 -- DO NOT QUOTE THIS z, see below). Per season:
  48.78 / 52.34 / 48.88 / 52.87 / 46.90 / 51.12 / 49.82.
  Coefficients: -0.32 / -0.90 / -0.62 / -0.28 / -0.85 / -0.74 /
  +0.39 -- tiny, and sign-flipped in 2025, the season most of
  the gain comes from.
- Both together: **50.11% ATS, MAE 10.35** (paired +0.98 pp,
  z=1.99, discordant 50/32).
- Nothing clears the ~52.4% breakeven, and no variant beats
  baseline MAE (10.34 vs 10.34/10.35). The features are
  therefore shadow-only, like every other batch-2/3 feature
  before them. `model.py`, `weekly_run.py`, and `dashboard.py`
  do not import this module (asserted in the tests).

**The paired-z warning -- read before citing the red-zone
number.** `model.py` rounds the SRS margin to 1 decimal, and
lines sit on .0/.5, so 34 baseline margins land exactly on
the line; under canonical grading those are guaranteed wrong
when the game is decided (33 decided). ANY tiny adjustment
moves them off the line, and the paired (McNemar) z then
counts only one direction: a tie game moved to the correct
side is discordant, a tie game moved to the wrong side is
both-wrong and invisible to the test. The red-zone variant
was right on 18 of those 33 tie games (54.5%) -- a coin
flip, not a signal -- yet the paired z reads 4.02. Proof by
placebo, run on the same baseline: random +/-0.15 pt
adjustments (no data at all) give 50.33% ATS, MAE 10.34, and
paired z=3.77 -- a "better" headline than the red-zone
feature. So: a tiny-adjustment variant must beat the
PLACEBO, not just the baseline, before anyone calls it a
win, and the canonical grading rule is left untouched (it
is what makes 49.12% comparable across batches; changing it
to rescue tie games would move the baseline too). The
red-zone +0.98 pp is fully explained by this artifact plus
noise, and the first (buggy) run of this evaluation -- where
burn-in seasons had no SRS margins, so 2019 coefficients
were 0 by construction and the numbers came out early-down
49.56% / red-zone 49.89% -- is itself evidence of how
fragile sub-1 pp results are to harness details. That bug
was fixed (burn-in SRS margins are now computed and the
reported baseline is still evaluated seasons only); the
numbers above are the fixed run, and the sensitivity is
reported rather than hidden.

**Still off by default, on purpose (this round):**
`PBP_FEATURES_LIVE=False` (new), plus every flag from earlier
batches (`EPA_LIVE`, `ENSEMBLE_LIVE`, `CALIBRATED_PICK`,
`TRAVEL_ENABLED`, `INJ_AUTO_APPLY`, `TOTALS_SRS_LIVE`,
`STALENESS_LIVE` -- all False, unchanged).

**Limits that remain.** The play-by-play files themselves
(~19 MB gz per season, 2017-2025, ~161 MB total) were
downloaded to a scratch directory for this run, not committed
to the repo -- a rerun re-downloads them from the confirmed
URL, which is the reproducible path. The harness run used the
pre-existing local games.csv (the same nflverse schedules
file the app itself fetches), so its recorded-line caveat
applies unchanged: ATS here is against the recorded line,
not a verified Barbara pool line. No live wiring exists: the
weekly run does not fetch play-by-play, and the dashboard
shows nothing from this module -- that wiring waits for a
walk-forward win that this run did not produce. No GitHub
push was made for this item (local files only).

## Backtest batch 4 (2026-11-04, group A items 1-2) -- tie-grading correction + first error-decomposition read

Naming note: an earlier section above is titled "Backtest batch 4
(item 8)" for the totals model; that was batch 3's numbering. THIS
section is the parent task's Batch 4, group A, items 1-2 only. Items
3+ of this batch are not covered here.

**The bridge -- read before quoting any canonical number.** Every
"canonical" number in earlier sections (49.12% ATS) was computed
under a grading bug, and those sections are left intact as the
historical record -- they are NOT silently rewritten. The bug: in
`backtest.evaluate_variant`, a model margin exactly equal to the
line could never be > or < the line, so it was counted WRONG
whenever the game was decided. Model margins are rounded to 1
decimal and lines sit on .0/.5, so 34 baseline margins land
exactly on the line in 2019-2025 (33 decided, 1 actual-push).
The correction (this batch): a margin exactly equal to the line
is a PUSH (no pick), excluded from ATS% but counted, matching
the batch-2 `_ats_rows` rule that the batch-2 internal baseline
already used. That is why the batch-2 internal baseline
(50.03%) and the corrected canonical baseline are the same
number -- batch 2 was right, the canonical harness was wrong.

| | Old rule | Corrected rule |
|---|---|---|
| Baseline ATS, 2019-2025 (n=1,871) | 49.12% (898/1,828) | **50.03% (898/1,795)** |
| Pushes | 43 (actual == line only) | 76 (43 actual + 34 margin - 1 both) |
| MAE | 10.34 | 10.34 (unchanged -- grading never touched MAE) |
| Recorded-line MAE | 9.83 | 9.83 (unchanged) |

Same 898 wins, 33 fewer decided games -- the entire move is the
33 tie games leaving the denominator. Per season, old -> new
ATS% (decided old -> new): 2019 47.97 -> 49.17 (246 -> 240);
2020 51.56 -> 52.80 (256 -> 250); 2021 47.01 -> 47.91
(268 -> 263); 2022 52.87 -> 53.08 (261 -> 260); 2023 46.12 ->
46.67 (258 -> 255); 2024 51.12 -> 51.50 (268 -> 266); 2025
47.23 -> 49.04 (271 -> 261). Still below ~52.4% breakeven, still
trailing the line on MAE (10.34 vs 9.83). This correction is a
bookkeeping fix, not an edge: the model did not get better, the
ruler did.

**What else changed, and what deliberately did not.** Changed
to the corrected rule, with tests: `backtest.evaluate_variant`
(the fix itself; `pick_margin` is now stored per row so the
shrunk-margin calibrated_pick tie is graded on the margin the
pick used, not the raw margin), `pbp_features._grade_rows`
(which had deliberately used the old rule to match 49.12%),
`pool_lines._grade_pick` (same rule on both lines, so a margin
equal to one line is now a push against that line and a pick
against the other), and `error_decomposition.normalize_row`
(which claimed the push rule in its docstring but only checked
actual == line, so it re-graded tie-margin rows wrong -- now
fixed, which is what makes the item-2 read below consistent
with the harness). Unchanged, on purpose, with reasons:
`grading.py` already excluded tie-margin picks from its counts
(pred != vegas gate), so its outcome already matched; `pool_results`
and `adjustment_grading` grade sides actually submitted, where a
margin tie cannot occur, and actual == line was already a push
there; `elo_variants._grade` and `staleness.py` already used
the push rule; `ensemble.evaluate_weights` is a training
objective, not a grading/reporting path (its reported summaries
go through the already-correct `_ats_rows`), so it was left
alone rather than conflated with reporting. New in the harness:
`backtest.placebo_rows(frame, seed, magnitude=0.15)` -- the
placebo gate as a reusable function, not a one-off script.

**Item 1 -- Fix tie grading, then re-test everything small:
built, tested, NOTHING promoted.** All runs below are
point-in-time 2019-2025, recorded-line caveat unchanged, paired
comparisons against the corrected baseline (1,795 decided)
unless stated. Absolute ATS% across variants is NOT comparable
when decided counts differ (a variant that picks tie games has
more decided games); the paired delta is the honest comparison.

- Placebo gate, 200 seeds of random +/-0.15 pt adjustments (no
  data at all) on the corrected baseline: absolute ATS mean
  49.83% (SD 0.23, p5 49.45, p95 50.22); paired delta mean
  -0.195 pp (SD 0.175, p95 +0.11). Compare batch 3, old rule:
  one placebo seed scored 50.33% with paired z=3.77 -- a
  "better" headline than the red-zone feature. Under corrected
  grading the placebo is what it should be: nothing. Two
  honesty notes: the paired mean is slightly negative because
  the baseline happens to win 61.3% on |edge| = 0.1 games
  (19/31 -- small sample, not a finding), and flipping half of
  those drags the placebo down; and a single placebo seed's
  paired z reached 3.30 in absolute value, because tiny
  adjustments have tiny discordant sets and z is volatile
  there -- so the gate is the paired-delta DISTRIBUTION and
  the absolute-ATS distribution, never one seed's z. A tiny
  feature must clear the placebo p95s (paired +0.11 pp,
  absolute 50.22%), not just the baseline.
- Play-by-play features (re-run, pbp files re-downloaded from
  the confirmed URL to scratch, not committed): early-down
  alone 49.67% absolute, paired -0.22 pp (z=-0.49);
  red-zone alone 50.11% absolute, paired **0.00 pp (z=0.00,
  discordant 1/1)**; both together 50.11% absolute, paired
  +0.11 pp (z=0.25). The red-zone "win" vanishes completely:
  its old paired z=4.02 was entirely the tie artifact. Audit:
  red-zone's absolute correct count rises 898 -> 916, and all
  18 of those extra correct picks are tie games the feature
  happens to pick (18/33 = 54.5%, a coin flip); on
  mutually-decided games it flips exactly 2 games, 1 right and
  1 wrong. MAE: early-down 10.35, red-zone 10.34, both 10.35
  vs baseline 10.34 -- no MAE win either. `PBP_FEATURES_LIVE`
  stays False.
- Batch-2 proxy features and ensemble (re-run with team stats
  fetched fresh): numbers reproduce EXACTLY, because
  `_ats_rows` was already push-correct -- this is independent
  confirmation that the corrected canonical baseline (50.03%)
  equals the batch-2 internal baseline. Features, paired:
  all-features +0.78 pp (z=0.86, MAE worse); explosive 0.00
  (z=0.00 -- its earlier flips were all tie artifacts, now
  literally zero discordants); pressure allowed +0.17
  (z=0.33); pressure generated 0.00; pass share +0.17
  (z=1.00, the best of the family -- nominal p ~0.32,
  Bonferroni across the 8 feature tests = 1.0); first-down
  -0.11 (z=-0.18); wind_pass -0.06 (z=-0.45); ol_rush -0.17
  (z=-0.32). Ensemble: MAE-objective still collapses to 100%
  market (no picks); ATS-objective 50.89%, paired +0.97
  (z=0.75); equal-weight 51.18%, paired +0.84 (z=0.81); Elo
  component 52.52%, paired +2.28 (z=1.75, unchanged conclusion:
  nominally above breakeven but season-inconsistent and not
  Bonferroni-significant across ~12 variants); SOS-EPA
  component 51.14%, paired +1.02 (z=0.83). Nothing promoted.
- SOS-EPA 50% combo (`epa_sos` variant, corrected grading):
  51.16% absolute (926/1,810), paired +0.73 pp (z=0.86) --
  DOWN from the old-rule paired +1.53 pp (z=1.76), because
  roughly half the old paired gain was tie games counted
  one-directionally. Still positive, still noise, still below
  breakeven. `EPA_LIVE` stays False.
- Other canonical variants (corrected grading, paired):
  fade-10 48.95% (-0.78, z=-1.78); fade-6 47.68% (-2.02,
  z=-2.58 -- the fade-12 live setting still wins, as before);
  div-1.0 49.44% (-0.45, z=-1.23, still noise, div stays 0.9);
  calibrated pick 50.16% absolute but paired -0.11
  (z=-0.11) -- the shrunk pick does NOT beat the baseline on
  mutually-decided games, and its coefficients were fit
  in-sample anyway; `CALIBRATED_PICK` stays False; travel
  (0.95 pts/1,000 mi) 50.88% absolute, paired +0.67 (z=0.97,
  noise); off/def splits 48.52% absolute, paired -1.62
  (z=-1.13, still worse). `TRAVEL_ENABLED` stays False.
- Elo variants (already push-graded, re-run): default Elo
  reproduces 52.52% / MAE 10.39 exactly (continuous margins,
  untouched by the fix); no variant passes the promotion gate
  (train win + test win + Bonferroni + bootstrap CI excluding
  zero). Closest to anything are two near-zero movers
  (rollover 0.25: paired z=0.09; HFA 110: z=0.23); the
  nominally Bonferroni-significant one (rollover 0.75,
  p_bonf=0.0283) is a LOSER, so it fails the gate on direction.
  Nothing promoted.

**Item 2 -- First full error-decomposition read: built, run,
NEGATIVE result, NOTHING implemented.** Decomposition of the
corrected baseline (via `backtest --decompose` path, verified
to match the harness exactly: n=1,871, decided 1,795, 898 wins,
ATS 50.03%, MAE 10.34, line MAE 9.83). Coverage: total and
division known for all 1,871 games; wind known for 1,133,
unknown for 738 (domes and unrecorded wind are "unknown", never
imputed). There are no pick'em-spread games in the window
(|line| < 0.5 never occurs), so that bucket is absent, not zero.

Worst slices (qualified = decided >= 30; 51 qualified slices
across 8 dimensions, so Bonferroni x51 applies):

- By lowest ATS: NYJ picks 35.85% (19/53, MAE gap +1.32 --
  the model's worst MAE gap too); NE picks 41.07% (23/56);
  PHI picks 41.86% (18/43); CHI picks 43.48% (30/69); LV picks
  43.75% (21/48). Every one is a TEAM slice with n ~50.
  NYJ's z vs 50% is -2.06 (nominal p ~0.039) -- Bonferroni
  x51 = 1.0. None survives correction. Team slices are also
  the least actionable target: a Jets-only fudge fit on 53
  games spanning 7 seasons and 3 regimes is chasing noise.
- Worst credible (non-team) slice: short spreads (|line| in
  [0.5, 3.5)) at 45.69% (297/650, MAE gap +0.49), z vs 50% =
  -2.20 (nominal p ~0.028, Bonferroni x51 = 1.0). Favorite
  picks 47.73% (305/639, z=-1.15); mid-high totals (45-50)
  47.46% (317/668, z=-1.32); moderate wind (15-20 mph) 46.53%
  (47/101, z=-0.70); home picks 48.76% (413/847). High wind
  (20+ mph) is flagged small_sample (29 decided) and excluded
  from the ranking, as designed. Divisional vs
  non-divisional is a dead heat (50.46% vs 49.78%); early
  weeks are not specially bad (49.19% weeks 1-4, 51.35%
  weeks 5-9, 49.72% weeks 10+).
- Global, not a slice: the model trails the line on MAE in
  essentially every slice (overall gap +0.51 pts); the few
  negative-gap slices are team slices (JAX -0.56, PIT -0.08,
  SF -0.04, ARI -0.03). There is no slice where the model
  beats the market on MAE to build on.

Walk-forward test of targeted fixes (fit on 2019-2022 only,
applied frozen to 2023-2025; win bar: test paired z > 1.96
after Bonferroni across the 3 candidates, with no harm to the
full test sample). Fix form, pre-stated: add the train slice's
mean signed error (actual - model) to slice margins only.

- NYJ bias (+2.26, fit on 35 games): test slice 38.9% ->
  50.0%, but on TWO flips (paired 2v0, z=1.41, n=18 decided)
  -- and the bias it was fit to REVERSED in test (train mean
  signed +2.26, test -2.61). A team fudge that "works" via 2
  coin-flip games while its own fitted bias flips sign is
  not a fix. NOT implemented.
- Short-spread bias (-0.21, fit on 367 games): test slice
  44.1% -> 44.0%, paired 3v1 (z=1.00). The slice's problem is
  not a level bias (train -0.21, test -0.54 -- both ~zero),
  so there is no bias fix to apply, and shrinking toward the
  line cannot change ATS at all (it never flips a pick).
  NOT implemented, and no flip-the-slice rule is proposed:
  short games are near coin flips and a contrarian rule on a
  45.7% slice with Bonferroni p = 1.0 would be guessing.
- Favorite-pick bias (-0.27, fit on 343 games): test slice
  51.3% -> 48.9%, paired 2v8 (z=-1.90) -- the fix HURTS
  out-of-sample. The train "finding" (44.34%, z=-2.05) was
  noise that reversed (test base 51.28%). NOT implemented,
  and this reversal is the reason slice fixes need the
  walk-forward bar in the first place.

Conclusion of item 2: no targeted fix wins walk-forward, so
nothing model-changing was implemented. The decomposition
module stays diagnostic-only, as designed. The honest summary
of where the model loses is global, not sliced: it is a ~50%
model that trails a sharp line on MAE everywhere, and its
worst slices are small-sample team noise plus one
moderately-sized short-spread dip with no correctable bias.

**Still off by default, on purpose (this round):**
`EPA_LIVE`, `ENSEMBLE_LIVE`, `CALIBRATED_PICK`,
`TRAVEL_ENABLED`, `INJ_AUTO_APPLY`, `TOTALS_SRS_LIVE`,
`STALENESS_LIVE`, `UNCERTAINTY_PICK_USE`,
`PBP_FEATURES_LIVE` -- all False, unchanged. No flag was
flipped, no pick logic was changed, no model parameter in
`config.py` was changed: the grading correction is a harness
rule, not a model parameter, and the item-2 result adds no
parameter at all.

**Files changed (local only, no GitHub push).**
- `nflpicks/backtest.py`: corrected push rule in
  `evaluate_variant`, `pick_margin` stored per row, new
  `placebo_rows()` helper, docstring bridge note.
- `nflpicks/error_decomposition.py`: `normalize_row` now
  grades a margin == line (pick_margin if present, else
  model_margin) as a push, matching its docstring and the
  harness.
- `nflpicks/pbp_features.py`: `_grade_rows` follows the
  corrected canonical rule (was deliberately old-rule).
- `nflpicks/pool_lines.py`: `_grade_pick` follows the
  corrected canonical rule on both lines (was deliberately
  old-rule).
- `tests/test_batch3_item8.py`: the old-rule grading test
  rewritten for the corrected rule.
- `tests/test_batch3_items45.py`: the old-rule pool-line
  test rewritten without a margin tie, plus a new
  margin-equals-pool-line-is-push test.
- `tests/test_batch4_items12.py` (new): tie-grading tests
  (including a non-vacuous harness tie test), placebo-gate
  tests, flags-stay-off test.
- This README section.
- NOT changed, deliberately: `grading.py`, `pool_results.py`,
  `adjustment_grading.py`, `elo_variants.py`, `staleness.py`,
  `ensemble.py`, `model.py`, `weekly_run.py`, `dashboard.py`,
  `config.py` (no new parameters; the fix adds none).

**Tests.** Full suite: 171/171 pass (164 prior + 7 new/changed
in effect: 6 new batch-4 tests + 1 new pool-line test, with 2
old-rule tests rewritten). `py_compile` clean on all changed
files. Baseline before this batch: 164/164.

**Limits that remain.** Recorded-line caveat unchanged: ATS
here is against the nflverse recorded line, not a verified
Barbara pool line, so pool ATS will differ. The games.csv
used was re-downloaded from the nflverse URL for this run
(same file the app fetches); the pbp files (~19 MB gz per
season, 2017-2025) were downloaded to scratch, not committed.
The walk-forward slice tests use a single 2019-2022 /
2023-2025 split (the same split as the Elo gate); a different
split could move the small NYJ numbers, which is itself a
reason not to implement on n=18. No GitHub push was made, no
email was sent, no current-week picks or leans are in this
section (historical backtest data only).

## Backtest batch 4 (2026-11-04, group B items 3-5) -- staleness auto-report + team HFA + dynamic blend: built, tested, NOTHING promoted

Naming note: this is the parent task's Batch 4, group B, items 3-5
only. Group A items 1-2 (tie-grading correction, canonical baseline
now 50.03% ATS, 898/1,795, MAE 10.34) are in the section above, and
nothing in that section was edited for this one. All runs below are
point-in-time 2019-2025 on the same games.csv, under the corrected
push grading, with the recorded-line caveat unchanged (ATS against
the nflverse recorded line, not a verified Barbara pool line).
Promotion bar throughout: a genuine walk-forward win above ~52.4%
breakeven, surviving Bonferroni correction across the
pre-registered tests for that item. No item clears it.

**Item 3 -- Staleness auto-report: built, currently reporting
not_enough_data, and that is the correct output.**
`db.get_snapshot_pairs()` collects every game with both a 'pool'
and a 'closing' snapshot in `line_snapshots`; `staleness.
build_staleness_report()` grades follow vs fade against the pool
line (existing `grade_staleness()`, pushes excluded) ONLY once
completed pairs with results reach `STALENESS_MIN_COMPLETED_PAIRS`
= 100 (config.py -- a judgment threshold, not a fitted value:
below ~100 pairs a 50/50 split still carries a ~10 pp wide 95%
CI, and 100 is roughly six full weeks of pool games, so one lucky
week cannot trip it). Below threshold the report returns status
`not_enough_data` with the counts and grades NOTHING -- no
follow/fade percentages exist for a sub-threshold sample, so a
handful of moved games can never be quoted as a finding.
`staleness.auto_report(db_path, results)` is the live entry
point: it joins snapshot pairs to a caller-supplied results map
(results are not in this database -- picks store no result), so
Thursday snapshots cannot inflate the pair count before games are
played. At/above threshold the report still does NOT promote
anything: reaching 100 makes the grading reportable, promotion
still needs a walk-forward win above breakeven decided manually,
and `STALENESS_LIVE` stays False. Two honesty notes carried in
the report itself: most pairs do not move, so even at 100 pairs
the moved-decided count may be under 30 (flagged
small_sample_warning when it is); and the 'closing' snapshot the
weekly run saves is the live line at run time, NOT a verified
kickoff close, so a ready report grades pool-vs-last-live-line
until a true close is ever recorded under that source name.
Current live state: this checkout contains no `nfl_picks.db`, so
a report run here returns 0 snapshot pairs -- not_enough_data,
100 pairs still needed. No pool line was invented, no dashboard
or weekly-run wiring was added (the report is a callable API;
wiring a display waits for a real pair sample, as with batch 3).

**Item 4 -- Team-specific HFA with shrinkage: built, tested at
three shrinkage strengths, LOSES at all three. Flat number kept.**
`ratings.team_home_field()` estimates each team's HFA from its
pre-week home games only (neutral-site games excluded): raw =
mean(actual home margin - ratings-implied margin without HFA),
shrunk = league + (raw - league) * n/(n+K), clamped to the same
0.5-3.0 range as the league number, teams with no home games
getting exactly the league number. K = 25 by default
(`TEAM_HFA_SHRINKAGE_GAMES`): a team with ~25 pre-week home games
gets weight 0.5 on its own estimate, the honest weighting when a
single team's home residual has SE ~13.4/sqrt(n) (batch-1 item 6
already found team ATS-residual SD 1.44 vs mean SE 1.73 -- mostly
noise). At a representative point (2025 week 10) the shrunk
deviations are material, not cosmetic: mean |shrunk - league|
0.81 pts, max 1.72, 7 of 32 teams at >= 1 pt, against a mean
|raw - league| of 3.44 -- shrinkage is doing real work, and the
variant still loses, so this is not a case of shrinkage hiding
a signal. Harness variants `team_hfa` (K=25), `team_hfa_k10`,
`team_hfa_k50` in `backtest.py` (same margin construction as
`model.py`, home team's shrunk HFA in place of the league number,
0 at neutral sites, 1-decimal rounding like `margin_model`):

| variant | ATS% | decided | MAE | paired delta | paired z |
|---|---|---|---|---|---|
| baseline (flat HFA) | 50.03 | 1,795 | 10.34 | -- | -- |
| team_hfa K=25 | 49.64 | 1,797 | 10.32 | -0.73 pp | -1.23 |
| team_hfa_k10 | 49.33 | 1,804 | 10.33 | -0.79 pp | -1.15 |
| team_hfa_k50 | 49.67 | 1,804 | 10.33 | -0.39 pp | -0.81 |

Baseline reproduces group A exactly (50.03%, 898/1,795, MAE
10.34), confirming the harness was not disturbed. The paired
comparison excludes games that are pushes under either rule, so
no tie artifact inflates these deltas, and the discordant sets
are large (111 for K=25: 49 variant-right vs 62 base-right), so
the negative paired z values are meaningful, not placebo-scale
wobble -- the placebo gate, which guards tiny +/-0.15 pt
adjustments, is not the binding check for ~0.8 pt HFA moves, and
is not claimed as one here. Per season, K=25 wins only 2021 (+1.33 pp: 49.24 vs 47.91) and
2023 (+1.57 pp: 48.24 vs 46.67), ties 2024 (51.50 vs 51.50), and
loses 2019, 2020, 2022, and 2025 -- there is no consistent season
pattern to build on, and the two wins do not offset the
full-sample paired loss. The 0.02 pt MAE improvement (10.32 vs
10.34) does not rescue it: the pool scores ATS, and an MAE gain
that costs ATS points is the same trade the Elo variants already
showed. `TEAM_HFA_LIVE` (new) stays False; nothing in `model.py`,
`weekly_run.py`, or `dashboard.py` reads team HFA. In-sample vs
out-of-sample, stated separately: the raw team estimates ARE
in-sample w.r.t. the ratings they are computed against (same
blended ratings as the pick), which is why shrinkage exists --
but the numbers above are all walk-forward (pre-week games only),
so the loss is an out-of-sample loss, not a fitting artifact.
A future attempt would need a genuinely different HFA signal
(stadium/altitude/dome splits were already null in batch 1), not
another K.

**Item 5 -- Dynamic market blend: tested three ways, NONE wins.
Fixed blend kept -- and rescaling alone could never have won.**
First the structural finding, proved in unit tests, not assumed:
in this app the pick follows the SIGN of (model - line), and
`model.py`'s blend (trust = confidence * 0.30) only rescales the
displayed edge -- any strictly positive blend weight preserves
the pick side exactly. A dynamic blend that only varies the
weight by week or by deviation bucket therefore cannot change a
single pick (except landing a margin exactly on the line, a push
under corrected grading, not a win). The only pick-changing form
a dynamic blend can take here is a per-bucket follow/fade flip,
and that is what `nflpicks/dynamic_blend.py` tests walk-forward:
learn follow/fade per bucket on 2019-2022, apply frozen to
2023-2025. Descriptive answer to "when the model disagrees with
the market by a lot, who wins?" -- the market, mildly, and
consistently: full-sample model-side ATS by |deviation| is
52.59% (<1, n=386 decided), 48.51% (1-3, n=707), 51.82% (3-5,
n=440), 47.33% (5+, n=262; 5-8: 46.08%, n=204; 8+: 51.72%, n=58).
The 5+ bucket loses on both splits too (train 47.89%, test
46.67% for the model side), so fading large disagreements is not
an obviously wrong idea -- it is just not a winning one once the
whole slate is scored, because 5+ games are only ~14% of the
slate and the fade gains there do not survive the games it also
touches. Walk-forward results (test split 2023-2025, baseline
test 49.10%, 384/782; Bonferroni across the 3 pre-registered
tests below):

- Learned deviation-bucket rule (train: follow <1, fade 1-3,
  follow 3-5, fade 5+): test 51.92% (406/782), paired +2.81 pp,
  z=1.07, p=0.2865, Bonferroni p=0.86 -- noise, and below 52.4%
  breakeven. Its train in-sample paired z was only 0.56, so even
  the training win was weak; fading the 1-3 bucket (the largest
  bucket, 707 games) on a sub-50% train reading is not a rule to
  trust on this evidence. Full-sample 51.98% is reported for
  completeness only -- it re-uses the training seasons and is NOT
  an out-of-sample result.
- Learned week-bucket rule (train: fade weeks 1-4, follow 5-9,
  fade 10+): test 46.68% (365/782), paired -2.43 pp, z=-0.79 --
  a clean out-of-sample REVERSAL. Train weeks 5-9 followed at
  58.39% and weeks 10+ faded from 47.03%; on test, weeks 5-9
  collapse to 42.03% and weeks 10+ rise to 53.05%. A week-based
  blend weight fit on 2019-2022 would have actively hurt
  2023-2025. This is the single clearest no-go in the batch.
- Pre-registered fade-if-|dev|>=5 rule: test 50.13% (392/782),
  paired +1.02 pp, z=0.73, p=0.4652, Bonferroni p=1.0 -- noise.
  (An abstain-on-large-deviation variant was considered and
  rejected without testing: a pool requires a pick every game,
  so an abstained game scores 0, strictly worse than a coin-flip
  pick, and grading abstentions as pushes would flatter the
  rule by deleting its losses.)

`DYNAMIC_BLEND_LIVE` (new) stays False; no blend weight in
`config.py` changes (the fixed 0.30 ceiling and confidence
scaling are untouched), and no week- or bucket-dependent weight
is wired into `model.py`. The placebo gate's role here is the
rescaling proof above (positive weights cannot flip picks),
verified in tests -- the flip rules themselves move whole
buckets (120-575 discordant games on test), so their paired z
values are not placebo-scale artifacts either; they are simply
not significant.

**Still off by default, on purpose (this round):**
`STALENESS_LIVE=False` (unchanged), `TEAM_HFA_LIVE=False` (new),
`DYNAMIC_BLEND_LIVE=False` (new), plus every flag from earlier
batches (`EPA_LIVE`, `ENSEMBLE_LIVE`, `CALIBRATED_PICK`,
`TRAVEL_ENABLED`, `INJ_AUTO_APPLY`, `TOTALS_SRS_LIVE`,
`UNCERTAINTY_PICK_USE`, `PBP_FEATURES_LIVE` -- all False,
unchanged). No flag was flipped, no pick logic was changed, no
live model parameter in `config.py` was changed: the new
parameters are harness/report thresholds and shadow flags only.

**Files changed (local only, no GitHub push).**
- `nflpicks/config.py`: `STALENESS_MIN_COMPLETED_PAIRS=100`,
  `TEAM_HFA_LIVE=False` / `TEAM_HFA_SHRINKAGE_GAMES=25`,
  `DYNAMIC_BLEND_LIVE=False` /
  `DYNAMIC_BLEND_LARGE_DEVIATION=5.0` /
  `DYNAMIC_BLEND_MIN_TRAIN_DECIDED=30`, each with its rationale
  comment. No live parameter changed.
- `nflpicks/ratings.py`: `team_home_field()` /
  `team_home_field_detail()` (shrunk per-team HFA; harness-only).
- `nflpicks/backtest.py`: `team_hfa` / `team_hfa_k10` /
  `team_hfa_k50` variants (pre-week HFA estimation, league
  fallback, neutral-site 0, 1-decimal rounding). Baseline path
  untouched -- it reproduces 50.03% exactly.
- `nflpicks/db.py`: `get_snapshot_pairs()` (pool+closing pairs
  with model margin when present; results joined by caller).
- `nflpicks/staleness.py`: `build_staleness_report()` /
  `auto_report()` (threshold-gated grading; `grade_staleness()`
  and flagging ownership unchanged).
- `nflpicks/dynamic_blend.py` (new): deviation/week bucketing,
  `blend_margin()`, `describe()`, `learn_bucket_rule()` /
  `apply_bucket_rule()`, `fade_large_rule()` -- shadow only.
- `tests/test_batch4_items345.py` (new): 15 tests -- threshold
  behaviour (below/at, missing lines/results, pushes, DB-backed
  auto-report), shrinkage math (no-home-games, neutral
  exclusion, clamp), harness variant acceptance, blend
  sign-preservation, bucket learning/application, paired
  push-exclusion, flags-stay-off.
- This README section.
- NOT changed, deliberately: `model.py`, `weekly_run.py`,
  `dashboard.py`, `grading.py`, `pool_lines.py`,
  `error_decomposition.py`, `ensemble.py`, group A files, and
  every earlier README section.

**Tests.** Full suite: 186/186 pass (171 prior + 15 new).
`py_compile` clean on all changed files. Baseline before this
batch: 171/171.

**Limits that remain.** Recorded-line caveat unchanged: items 4
and 5 grade against the nflverse recorded line, not a verified
Barbara pool line, so pool ATS will differ -- and item 5's
deviation buckets are deviations from that recorded line, not
from a pool-locked line. Item 3's true test cannot even start
until live snapshot pairs accumulate (0 in this checkout);
when it does, its 'closing' leg is last-live-line, not a
verified close, unless recording changes. Item 4's team-HFA
residuals are computed against ratings that already contain the
played games (shrinkage mitigates, walk-forward grading
adjudicates); a cleaner estimator would solve ratings and HFA
jointly, but a jointly-solved variant that still loses ATS
would not change the conclusion. Item 5 uses a single
2019-2022 / 2023-2025 split (the same split as the Elo gate); a
different split could move the deviation-rule test number, which
is itself a reason not to promote on z=1.07. No GitHub push was
made, no email was sent, no current-week picks or leans are in
this section (historical backtest data only).

## Backtest batch 4 (2026-11-04, group C items 6-7) -- play-by-play round 2 + rating uncertainty round 2: built, tested, NOTHING promoted

Naming note: this is the parent task's Batch 4, group C, items
6-7 only. Groups A (tie-grading correction, canonical baseline
now 50.03% ATS, 898/1,795, MAE 10.34) and B (staleness
auto-report, team HFA, dynamic blend -- nothing promoted) are
in the sections above, and nothing in those sections was edited
for this one. All runs below are point-in-time 2019-2025 on the
same games.csv, under the corrected push grading, with the
recorded-line caveat unchanged (ATS against the nflverse
recorded line, not a verified Barbara pool line). Promotion bar
throughout: a genuine walk-forward win above ~52.4% breakeven,
surviving Bonferroni correction across the pre-registered
tests for that item, with the placebo gate (below) as a floor,
not a substitute for that bar. No item clears it.

**Item 6 -- Play-by-play round 2: third-down, special-teams
EPA, and defensive-only splits. Built, graded, NOTHING kept.**
New module `nflpicks/pbp_round2.py` (round 1's
`pbp_features.py` untouched). Source: the same confirmed
source as round 1 -- nflverse-data `pbp` release,
`play_by_play_{season}.csv.gz` (config.PBP_URL_TMPL). Files
for 2017-2025 were used from scratch (/tmp, NOT committed);
the 2024 file was re-downloaded fresh for this batch and
matched the scratch copy byte-for-byte (19,362,351 bytes,
sha256 23370d5d10f8104d80d46a1fc5e61f4f6f5a3263fe96fe2dd6
29913cfcb08c06). One honesty note on that hash: the
round-1 docstring's sha256 string is only 61 characters,
so it cannot be a valid sha256 (64 required) -- the size
match and the fresh-download match are the verification
here, not that string, and the round-1 docstring was left
as-is rather than silently corrected. Season row counts
(REG): 2017: 45,268; 2018: 45,120; 2019: 45,339; 2020: 45,406;
2021: 47,651; 2022: 47,157; 2023: 47,399; 2024: 47,274;
2025: 46,452. No play-by-play number was invented: a season
with no frame would grade as the SRS margin.

Definitions (see the module docstring for the full text):
third-down conversion = 3rd-down pass/run plays converting
via first_down/touchdown flags or yards_gained >= ydstogo
(the file's third_down_converted column is not used);
special-teams EPA = mean `epa` per `special` == 1 play
(kickoff, punt, field goal, extra point -- 7,216 such plays
in 2024 REG; the narrower `special_teams_play` flag omits
field goals and is only a fallback); defensive-only splits
are off_diff = home_off - away_off and def_diff =
away_allowed - home_allowed (lower allowed is better, so
the sign flips), where round 1 only ever tested the
combined off-minus-allowed strength and could not say
which side drove a result. Walk-forward discipline is
identical to round 1: season S week W rates use season-S
weeks before W plus the full season S-1, coefficients for
season S are fit only on scored + burn-in (2017-2018)
rows from seasons before S, and in-sample numbers are
reported separately below, never as the promotion test.

Results, 2019-2025 (baseline 50.03% / 1,795 decided /
MAE 10.34; feature decided counts are 1,827-1,828 because
continuous feature margins turn baseline tie games into
picks -- the paired comparison below is on the 1,795
mutually-decided games, so no tie artifact inflates it;
placebo gate re-verified fresh for this batch over 200
seeds: paired-delta p95 +0.11 pp, absolute-ATS p95 50.22%,
identical to group A; a feature passes the gate only if
it clears BOTH p95s):

| feature | ATS% | paired delta | paired z | discordant (var/base) | placebo gate |
|---|---|---|---|---|---|
| third_down | 49.56 | -0.33 pp | -1.00 | 15/21 | FAIL |
| third_down_off | 49.56 | -0.39 pp | -0.64 | 56/63 | FAIL |
| third_down_def | 49.84 | -0.17 pp | -0.90 | 4/7 | FAIL |
| st_epa | 49.84 | 0.00 pp | 0.00 | 1/1 | FAIL |
| early_down_off | 49.51 | -0.28 pp | -0.48 | 51/56 | FAIL |
| early_down_def | 49.56 | -0.28 pp | -0.82 | 16/21 | FAIL |
| red_zone_off | 49.84 | -0.06 pp | -0.30 | 5/6 | FAIL |
| red_zone_def | 50.25 | +0.22 pp | +0.82 | 14/10 | pass (see below) |
| all (third+ST+early+rz combined) | 49.84 | -0.06 pp | -0.10 | 51/52 | FAIL |

MAE for every feature is 10.33-10.37 vs baseline 10.34 --
no MAE win either (early_down_off 10.33 is the only one
below baseline, by 0.01). Bonferroni across the 9 tested
variants: every nominal two-sided p is >= 0.31, so every
Bonferroni p = 1.0 -- there is no significant feature here
at all, before breakeven is even considered.

The one gate pass needs its context, because "passes the
placebo gate" is a floor, not a promotion, and this one
passes by less than a game: red_zone_def's absolute ATS
(50.25%) clears the placebo p95 (50.22%) by 0.03 pp, and
its paired delta (+0.22 pp) is 4 net games on 1,795
mutually-decided games (14 right / 10 wrong). Most of its
absolute gain is not mutually-decided flips at all: it
turns 32 baseline tie games into picks and wins 20 of
them (62.5%, n=32 -- a coin flip at that sample size).
And it does not hold up across splits of its own
walk-forward scored rows: 51.46% on 2019-2022 (1,030
decided) vs 48.68% on 2023-2025 (797 decided), below the
test-split baseline (~49.1%). A feature that passes the
gate by 0.03 pp, fails Bonferroni (p=1.0), sits 2.15 pp
below breakeven, and reverses on the test split is not
promoted, and the gate flag for it is reported as a pass
in the harness output (`passes_placebo_gate: true`) with
this paragraph as the reason it still stays shadow. Every
other feature fails the gate outright -- including
special-teams EPA, whose paired z is exactly 0.00 (1
discordant game each way) and whose fitted coefficients
are near zero in every season (-0.34 to +0.84, against a
natural scale near 13 for a mean-EPA-per-play feature),
i.e. the data say ST EPA per play carries no incremental
margin signal beyond SRS at all.

What the split features DO show, and it is worth stating
because it explains round 1: on scored rows, the
correlation of each differential with the SRS residual
(actual - model) is positive for every offensive split
(early_down_off +0.062, third_down_off +0.047, st_epa
+0.019, red_zone_off +0.013) and ~zero or negative for
every defensive split (third_down_def -0.021,
red_zone_def -0.010, early_down_def -0.003). The fitted
coefficients tell the same story (offensive coefficients
positive and growing across seasons, e.g. early_down_off
8.6 in 2019 to 20.0 in 2025; defensive coefficients mostly
negative). The SRS margin already prices defense through
points allowed, so a past defensive rate adds nothing
beyond it -- and in training windows it is slightly
anti-correlated, which is why combined features that mix
offense and defense (round 1's early_down, red_zone)
underperform their offensive halves. None of that rescues
a feature: the strongest offensive correlation (+0.062)
still grades at 49.51% walk-forward.

In-sample vs out-of-sample, stated separately: fitting
coefficients on ALL scored rows and grading those same
rows gives third_down 49.56%, third_down_off 49.12%,
third_down_def 50.16%, st_epa 50.49%, early_down_off
49.89%, early_down_def 49.84%, red_zone_off 49.45%,
red_zone_def 50.30% (in-sample coefficients: third_down
3.79, third_down_off 10.06, third_down_def -5.47, st_epa
1.81, early_down_off 19.21, early_down_def -0.97,
red_zone_off 1.87, red_zone_def -1.43, early_down 11.85,
red_zone 0.24). The in-sample numbers are no better than
the walk-forward ones -- there is no "true" in-sample win
being hidden by walk-forward noise here, just no signal.
`PBP_FEATURES_LIVE` stays False; round 2 shares that flag
(no separate live flag exists) and nothing in `model.py`,
`weekly_run.py`, or `dashboard.py` reads `pbp_round2.py`.

**Item 7 -- Rating uncertainty round 2: covariance + HFA
uncertainty built, bands barely move, pick-logic test FAILS.
Labels/display only, as before.**
Batch 3's bands ignored two terms (see the batch-3
docstring, now updated in place): rating covariance and
HFA uncertainty. Round 2 adds both in `uncertainty.py`;
the batch-3 functions are UNCHANGED, so old bands still
reproduce exactly. Covariance comes from the game graph
itself, not a fitted constant: the SRS point estimate is
the ridge solution minimising sum_games (margin -
(r_home - r_away))^2 / game_var + sum_teams (r -
prior)^2 / prior_var, so the rating covariance is the
inverse of the precision matrix P with P[t][t] =
1/prior_var + n_t/game_var and P[t][u] = -n_tu/game_var
(n_tu = games between t and u in the rating window,
built point-in-time by `window_pairs()` -- season-S weeks
before W plus the full season S-1). A direct game and
shared opponents both enter P, so no separate
shared/direct bookkeeping is needed. Sanity check: for
one shared game the graph gives marginal SD 4.72 vs
batch-3 rating_sd(1) = 4.69, and covariance positive, as
the batch-3 docstring predicted -- the graph method
agrees with the old formula where it should. HFA
uncertainty is `UNCERTAINTY_HFA_SD` = 1.0 (new in
config.py): the season mean home margin in games.csv
2019-2025 ranges -0.14 to +2.68, SD of the seven yearly
means 1.07 pts (the pooled SE of the overall mean, 0.33,
would understate the drift by assuming a fixed HFA), so
1.0 is that drift rounded -- a judgment value, not a
fitted pick parameter. It enters margin variance as
+HFA_SD^2, and neutral-site games (34 in the window)
pass hfa_sd=0.0 explicitly, since their HFA is 0 by
construction. New functions: `rating_covariance_matrix()`,
`covariance_between()`, `margin_sd_cov()`,
`margin_interval()`, `cover_prob_interval_cov()` -- all
labels/display, no pick path reads them.

How much do the bands actually move? On all 1,871
harness games (covariance from each game's point-in-time
week matrix; games_home/away = current-season games
before that week): mean graph covariance +0.67 pts^2
(median +0.55), mean batch-3 margin SD 5.26 pts, mean
round-2 margin SD 5.23 pts -- a net change of -0.03 pts.
The two corrections roughly cancel (covariance narrows,
HFA widens), and the round-2 band is narrower than batch 3
in only 60.7% of games. By week bucket (old -> new):
weeks 1-4, 6.48 -> 6.46; weeks 5-9, 5.36 -> 5.33;
weeks 10+, 4.62 -> 4.57 (covariance 0.63 / 0.61 / 0.71
respectively). That small net move is itself the finding:
rating covariance in this league is small because each
rating rests on many games and any one shared opponent
contributes little -- the batch-3 bands were already
close to honest, just slightly wide, and round 2
quantifies "slightly" at ~0.03 pts of SD. One related
number that must NOT be quoted as the band: the pure
graph margin SD (graph variances + covariance + HFA)
averages 3.54 pts -- much smaller, because the graph
counts prior-season games as full data while batch-3
rating_sd() treats the prior season as a 5.0-pt prior
and recency-weights current games. The reported round-2
band deliberately keeps batch-3 variances and adds only
the graph covariance + HFA, so it stays comparable to
batch 3; switching to pure graph variances would claim
precision the recency weighting and prior fade do not
support.

Does the new band sharpen the label? Barely, and no
better than batch 3: games whose round-2 cover interval
straddles 0.5 (1,262 games, 67% -- the SD is ~5 pts
against typical edges of a few points, so most games
straddle) score 48.88% ATS on the baseline pick (1,203
decided) vs 52.36% for non-straddling games (592
decided); the batch-3 intervals give 48.93% vs 52.33% on
essentially the same split. The straddle label separates
coin flips from leans directionally, but the
non-straddle 52.36% is still ~breakeven at n=592, and
round 2 adds nothing to that separation over batch 3.

Walk-forward pick-logic test (the test that would have
justified flipping `UNCERTAINTY_PICK_USE`): flip the pick
whenever the round-2 cover interval straddles 0.5, keep
it otherwise (baseline pushes stay pushes). Result:
51.53% ATS (925/1,795) vs baseline 50.03%, paired +1.50
pp, paired z=0.78 (615 variant-right / 588 base-right),
nominal p=0.44 -- noise, below 52.4% breakeven, and
wildly season-inconsistent: +1.66 pp (2019), -1.20
(2020), +2.28 (2021), -2.31 (2022), +8.62 (2023), -5.26
(2024), +6.90 (2025). A rule that flips 1,232 of 1,871
picks (every straddling game -- two-thirds of the slate)
and then wins big in two seasons and loses big in
another is not a pick rule, it is a near-contrarian
slate with a noisy headline. Its MAE (10.15) is NOT
reported as a win: the flipped "margin" in that test is
constructed as line +/- 1, so its MAE is pulled toward
the line (line MAE 9.83) by construction and means
nothing about margin accuracy. `UNCERTAINTY_PICK_USE`
therefore stays False, and the round-2 bands stay
labels/display only, exactly like batch 3. A future
pick-logic attempt should pre-register a rule that flips
far fewer games (e.g. only the largest straddling edges,
or a shrinkage rather than a flip -- noting that any
sign-preserving shrinkage cannot change a pick at all,
per the group B dynamic-blend proof) and test it on
seasons after 2025, not re-search 2019-2025.

**Still off by default, on purpose (this round):**
`PBP_FEATURES_LIVE=False` (unchanged, covers round 2),
`UNCERTAINTY_PICK_USE=False` (unchanged), plus every flag
from earlier batches (`EPA_LIVE`, `ENSEMBLE_LIVE`,
`CALIBRATED_PICK`, `TRAVEL_ENABLED`, `INJ_AUTO_APPLY`,
`TOTALS_SRS_LIVE`, `STALENESS_LIVE`, `TEAM_HFA_LIVE`,
`DYNAMIC_BLEND_LIVE` -- all False, unchanged). No flag was
flipped, no pick logic was changed; the new parameters
are a shadow coefficient-cap table and one HFA-SD
judgment value only.

**Files changed (local only, no GitHub push).**
- `nflpicks/config.py`: `PBP2_COEFF_CAPS` (rate caps 100,
  ST EPA cap 50 -- natural scale ~13, a team's ~13 ST
  plays/game times 1 pt per EPA), `UNCERTAINTY_HFA_SD=1.0`
  with the year-to-year-drift rationale. No live parameter
  changed.
- `nflpicks/pbp_round2.py` (new): third-down + ST EPA
  weekly counts, off/def/combined differentials, clipped
  walk-forward coefficient fits, `passes_placebo_gate()`,
  `placebo_distribution()`, `evaluate_pbp2_features()`
  (walk-forward + separate in-sample section). Shadow only.
- `nflpicks/uncertainty.py`: batch-3 functions unchanged;
  round-2 appendix -- `rating_covariance_matrix()`,
  `covariance_between()`, `margin_sd_cov()`,
  `margin_interval()`, `cover_prob_interval_cov()`,
  `window_pairs()` -- plus an updated docstring section
  explaining the two formerly-missing terms. No pick path
  reads this module (tested).
- `tests/test_batch4_items67.py` (new): 13 tests -- round-2
  counts (third-down, ST EPA, POST exclusion, week-less
  frame), differential signs, league-average fallback,
  coefficient clipping, placebo-gate both-p95 logic, graph
  covariance positivity/narrowing, prior-only team,
  margin_sd_cov reproduction + HFA quadrature, interval
  containment, window_pairs point-in-time, flags-stay-off,
  no-pick-logic-imports.
- This README section.
- NOT changed, deliberately: `model.py`, `weekly_run.py`,
  `dashboard.py`, `pbp_features.py`, `backtest.py`,
  `calibration.py`, `ratings.py`, group A/B files, and
  every earlier README section.

**Tests.** Full suite: 199/199 pass (186 prior + 13 new).
`py_compile` clean on all changed files. Baseline before
this batch: 186/186.

**Limits that remain.** Recorded-line caveat unchanged: both
items grade against the nflverse recorded line, not a
verified Barbara pool line, so pool ATS will differ. Item 6
uses a single walk-forward scheme (coefficients fit on
prior seasons, one 2019-2022 / 2023-2025 descriptive split
for the red_zone_def check); a different split could move
small numbers, which is itself a reason not to promote on
+0.22 pp. The ST EPA feature uses nflfastR's `epa` on
special-teams plays as recorded -- kickoff/punt EPA in
particular mixes returner and coverage effects that a
per-unit split (kicker vs returner vs coverage) could
separate, but that split was not built and no claim is
made for it. Item 7's covariance ignores recency
weighting in the graph (all window games count equally)
and the prior-season fade, so round-2 bands are an
approximation to the blended rating's uncertainty, not
its exact posterior -- and the pure-graph SD (3.54) is
reported only to warn against using it. The flip test's
MAE is construction-artifact and is not a finding (see
above). No GitHub push was made, no email was sent, no
current-week picks or leans are in this section
(historical backtest data only).

## Backtest batch 4 (2026-11-04, group D, items 8-10) -- committed-DB divergence, standings import, experiment tracking

Naming note: this is the parent task's Batch 4, group D, items
8-10 only. Groups A (tie-grading correction, canonical baseline
now 50.03% ATS, 898/1,795, MAE 10.34), B (staleness auto-report,
team HFA, dynamic blend -- nothing promoted), and C (pbp round 2,
uncertainty round 2 -- nothing promoted) are in the sections
above, and nothing in those sections was edited for this one.
No backtest numbers are re-run here: these three items are
plumbing (which database the dashboard shows, where standings
come from, what model produced a slate), not model variants,
so there is no ATS% to report and no promotion question --
the promotion question for each is whether the plumbing is
safe to rely on, answered by the tests below. No flag was
flipped, no pick logic was changed, no GitHub push was made,
no email was sent, and no current-week picks or leans are in
this section.

**Item 8 -- Streamlit Cloud DB divergence: detected on load,
resolved by an explicit sync. Built, tested, NOT live-clicked.**
The divergence is real and has three mechanisms, all stated
in `nflpicks/committed_db.py`: a Cloud redeploy lagging (or
failing) after a workflow push, so the dashboard shows last
week's slate while the repo holds the fresh one; a dashboard
"Refresh Picks Now" writing a slate into the deployment's own
database that the workflow can never commit back, so the
deployment slate becomes NEWER than the committed one and the
next redeploy silently overwrites it; and dashboard-entered
manual adjustments / pool overrides living in the deployment
database only (batch 3, item 5), which any sync must not
destroy. The fix is `committed_db.py` + a dashboard banner,
not a silent auto-overwrite (which would destroy a
just-clicked Refresh, and which Streamlit Cloud cannot
durably do to its own deployment database on load anyway):
- `describe_source()` resolves the committed source from
  env only: `PICKS_COMMITTED_DB_URL` (explicit raw URL,
  wins), `PICKS_COMMITTED_DB_FILE` (local shared-store
  file, no network, no token), or `PICKS_GITHUB_REPO`
  ("owner/repo", raw.githubusercontent.com URL derived
  from it, branch `PICKS_GITHUB_BRANCH`, default "main",
  path in repo `nfl-picks-app/nfl_picks.db`). There is
  deliberately NO default repo: the audit checkout has no
  git remote to derive one from, and hardcoding a repo name
  from memory would be inventing configuration. Until one
  source is set, the dashboard says plainly that it reads
  its local database only and cannot check itself -- it
  does not pretend a check happened.
- A token (`PICKS_GITHUB_TOKEN`) is OPTIONAL, sent only as
  an Authorization header, never in a URL, a log, or any
  returned dict. A public repo needs no token; a private
  repo without one returns 401/404, which this module
  reports as "committed source unreachable" with a local-DB
  fallback -- it does not guess at the contents.
- `compare_to_local()` fetches the committed DB to a temp
  file and compares its latest slate to the local one.
  Divergence is defined by the PICKS fingerprint (season,
  week, matchup, pick, edge, model margin, both lines),
  NOT by run_time: identical picks saved at different
  seconds are the same slate to submit, and a run_time-only
  difference must not cry divergence (this was caught by
  a failing test during this build and fixed in the module,
  not in the test). A newer-but-identical committed run is
  still reported, as `committed_newer`, not hidden.
- `sync_committed_to_local()` replaces the 13 workflow-owned
  tables (picks, ratings snapshot, injury review/returns,
  line snapshots, market cross-checks, model log/grades,
  run diagnostics/diffs, survivor picks/options,
  adjustment grades) from the snapshot, PRESERVES the 5
  user-owned tables (manual_adjustments,
  pool_line_overrides, survivor_used_teams, pool_results,
  pool_standings), and MERGES run_experiments (snapshot
  rows the local DB lacks are added; local rows are never
  deleted). Snapshot tables absent from either side are
  skipped, never treated as "delete everything". The sync
  result lists replaced / skipped / preserved / merged, so
  a lost user table would be visible in the result.
- Dashboard: a "Data source" section on every load (when a
  source is configured) shows match / diverged / unreachable
  with the committed and local run times, and a diverged
  slate gets a "Sync this dashboard to the committed
  slate" button that runs the sync above and reruns. A
  fetch failure warns and falls back to local, never
  crashes the page.
- CLI: `python -m nflpicks.committed_db [--db PATH]
  [--sync]` prints source + comparison as JSON, never a
  token.

**Item 8 limits, honestly stated.** (1) Raw GitHub serves
through a CDN cache: after a workflow push, a raw fetch
can return the PREVIOUS commit's database for some
minutes. The sync result therefore always reports the
committed slate's run_time, and the dashboard banner says
to check it against the expected Thursday run time instead
of trusting the word "synced". (2) The guarantee is
detect + explicit sync, not silent auto-sync on load, for
the reasons above -- a Thursday-morning user who ignores
a diverged banner and submits from the local slate is
still submitting from the local slate. (3) Sync is
one-way (committed -> local): dashboard-side changes that
should reach the workflow still need the
manual-adjustments export/commit route; this module does
not change that. (4) Dashboard live clicking was NOT
possible in this environment (Streamlit is not installed
here, same as prior batches): `dashboard.py` was
compile-checked only, and the banner/sync button needs a
live run to confirm rendering -- the module functions it
calls are unit-tested, the button itself is not.

**Item 9 -- Standings importer: built, tested, wired into
the dashboard's standings form.** `nflpicks/pool_standings.py`
mirrors `pool_results.py` exactly (its own table,
created on first use, so `db.py` is untouched; upsert on
(season, week); last-row-wins duplicates, noted in
errors; rejected rows never reach the DB). CSV format
(header required; leader_name and note optional):

    season,week,user_wins,leader_wins,weeks_left,leader_name,note
    2026,10,118,120,4,,after week 9 results posted

Rules that matter: `leader_wins` is the CURRENT leader's
wins, not second place -- if the user leads, leader_wins
== user_wins is the tied/leading input, and entering
second place as "leader" would falsely flip the rule into
trailing mode. `weeks_left` INCLUDES the current week
(final week = 1), the same definition `standings.py`
uses. Negative or non-integer wins/weeks_left reject the
row. With no imported rows, `latest_standings()` returns
status "empty" -- no wins, no deficit -- never 0-0 and
never a deficit of 0, either of which would misread as
"tied". `load_standings_file()` mirrors
`pool_lines.load_pool_lines_file`: a missing file (or no
`POOL_STANDINGS_CSV` path) is the empty state, not an
error. No standings were invented: the sample CSV rows
(118/120, 124/125) are labelled SAMPLE ONLY, and the only
numbers in the tests are synthetic fixtures.
`price_week_gambles(games, snapshot)` labels the week's
defaults with `standings.suggest_gambles` on the imported
snapshot and prices those labels with
`simulation.compare_gambles` -- both label-only, neither
changes a pick, and the leader is assumed to make default
picks (the same stated limit as
`standings.evaluate_late_window`). Dashboard wiring
(Submit & Review tab): a CSV import expander with format,
sample download, and upload (same pattern as the
pool-results import), the latest snapshot pre-filling the
standings form when one exists, and -- once labels are
checked -- a one-week simulation pricing line for the
form's standings (expected cost + delta P). That pricing
is explicitly ONE-WEEK ONLY: `compare_gambles` over this
week's games alone reports P(ahead after this week), NOT
season-end P(first). Full-season pricing still needs cover
probabilities for the whole remaining schedule, which this
database does not store (the batch-3 standings-wiring
limit, unchanged); presenting the one-week number as a
season-end number would be a misstatement, and the module
docstring, the function note, and the dashboard text all
carry the caveat with the numbers. What is deliberately
NOT built: a per-entrant field table -- standings.py and
simulation.py model a single leader, and a field model
would be a different module, not a reinterpretation of
these rows.

**Item 10 -- Experiment tracking on every weekly run:
built, tested, wired into weekly_run and reporting.**
`nflpicks/experiment.py` owns its own table
(`run_experiments`, created on first use, `db.py`
untouched), one row per weekly run: model version label,
git head (best-effort `git rev-parse --short HEAD`, 5s
timeout, None when git metadata is unavailable -- in
Streamlit Cloud that is normal, not an error, and is
never invented), the full flag set, the blend/rating
parameters, a fingerprint, and the slate's picks count.
The version label (`PICKS_MODEL_VERSION` when set, else
"nfl-picks-app (unlabelled build)") is cosmetic and the
module says so: the attribution key is the fingerprint,
sha256 (first 16 hex) of the canonical JSON of flags +
params, so any flag or parameter change changes the
fingerprint and identical fingerprints mean identical
recorded settings. Flags recorded (all eleven
off-by-default flags, read from `config.py` AT CALL TIME
so a patched config records what was actually used; a
flag missing from config records as None, unknown, never
as False): EPA_LIVE, ENSEMBLE_LIVE, CALIBRATED_PICK,
TRAVEL_ENABLED, INJ_AUTO_APPLY, TOTALS_SRS_LIVE,
STALENESS_LIVE, UNCERTAINTY_PICK_USE, PBP_FEATURES_LIVE,
TEAM_HFA_LIVE, DYNAMIC_BLEND_LIVE. Parameters recorded:
PRIOR_FADE_GAMES, PRIOR_REGRESSION, DIV_MULTIPLIER,
MARKET_TRUST_COMPLEMENT, COVER_SHRINK_SLOPE,
COVER_SHRINK_INTERCEPT, EPA_POINTS_PER_UNIT, EPA_BLEND,
SRS_DAMPING, ELO_K. Wiring: `weekly_run.py` calls
`experiment.record_run()` once per run, AFTER picks are
saved, inside try/except with a diagnostic -- a tracking
failure never fails a run whose picks are already saved,
exactly like the what-changed diff. It is NOT called from
grading (a separate workflow step re-reading the same DB):
a second row per week would double-count the same slate.
Reporting: the dashboard's Model Grades tab shows the
latest experiment row for the season/week (fingerprint,
version label, git head, run time, picks count, flags
live that week, full parameter list), with "no record"
reported as unknown, never guessed; `attribution_for_week()`
joins any caller-computed grading to that row (status
"unknown" for pre-batch weeks, with the grading returned
un-attributed); and `python -m nflpicks.experiment
[--db PATH] [--season N] [--week N]` prints the season's
experiment timeline as JSON. `grading.py` itself is
unchanged -- attribution is a join on top of grading, not
a change to grading -- and the table is append-only
history (PRIMARY KEY run_time + season + week; a re-run
in the same second overwrites its own row, which is the
same run), merged -- never wiped -- by a committed-DB
sync, for the same reason.

**Still off by default, on purpose (this round):** all
eleven flags above are False (verified by test, not just
asserted in prose), plus every flag from earlier batches.
No flag was flipped, no pick logic was changed; the new
config values are env-driven source settings, a standings
CSV path, and a version-label env var only.

**Files changed (local only, no GitHub push).**
- `nflpicks/committed_db.py` (new): source resolution,
  fetch (optional token header only), slate fingerprint +
  comparison, workflow-table sync with user-table
  preservation and run_experiments merge, CLI.
- `nflpicks/pool_standings.py` (new): standings CSV parse
  / upsert / latest / clear, one-week gamble pricing with
  the one-week caveat, file loader, CLI, sample CSV.
- `nflpicks/experiment.py` (new): settings capture +
  fingerprint, run recording, latest/timeline/attribution,
  CLI.
- `nflpicks/config.py`: `COMMITTED_DB_*` (URL / file /
  repo / branch / in-repo path / timeout / 64 MB cap, all
  env-driven, no default repo), `POOL_STANDINGS_CSV_PATH`,
  `EXPERIMENT_MODEL_VERSION`. No live parameter changed.
- `weekly_run.py`: one `experiment.record_run()` call
  after `save_picks`, try/except + diagnostic, nothing
  else changed.
- `dashboard.py`: committed-DB banner + sync button (top
  of page), standings CSV import + pre-filled form +
  one-week pricing (Submit & Review tab), experiment
  record display (Model Grades tab). Compile-checked
  only -- see item 8 limits.
- `tests/test_batch4_items8910.py` (new): 16 tests --
  committed source modes (local / repo URL / bad repo
  rejected without network), file-mode match + diverge,
  unreachable fallback, sync replaces workflow tables and
  preserves user tables, standings parse/reject/duplicate,
  empty state, save/load/latest, one-week pricing uses the
  actual snapshot and its caveat, early mode has no
  gambles, all 11 flags off, record/latest/fingerprint
  change on a flag flip, capture determinism, weekly_run
  and dashboard wiring by source inspection,
  no-flag-flipped in the new modules.
- This README section.
- NOT changed, deliberately: `db.py`, `grading.py`,
  `model.py`, `standings.py`, `simulation.py`,
  `pool_results.py`, `pool_lines.py`, `freshness.py`,
  `.github/workflows/weekly.yml`, and every earlier README
  section. In particular the workflow does NOT gain a
  committed-DB step: the check lives in the dashboard,
  where the divergence is visible, and the workflow's
  commit step is untouched.

**Tests.** Full suite: 215/215 pass (199 prior + 16 new).
`py_compile` clean on all changed files (including
`dashboard.py` and the new test file). CLI smoke checks
(no network, temp DB only): `experiment --season 2026
--week 10` prints the recorded fingerprint
(`d709a1eb4a8b148a` for the all-flags-off settings in
that smoke run -- a fingerprint of that run's settings,
not a canonical value to compare against, since any
config change changes it); `pool_standings --sample`
prints the sample CSV; `committed_db` with no source
configured prints status `local_only` and no token.
Baseline before this batch: 199/199.

**Limits that remain.** Dashboard live clicking was not
possible here (Streamlit not installed): the banner, the
sync button, the standings import UI, the pre-filled form,
the one-week pricing line, and the experiment display all
need a live run to confirm rendering -- only their
underlying module functions are unit-tested. The
committed-DB check does nothing at all until a source is
configured in the deployment's environment (Streamlit
Cloud secrets/env): with no source, Thursday morning
still shows the local slate with only a caption saying
so. The one-week simulation pricing is not season-end
pricing (see item 9 caveat, repeated wherever the number
appears). Experiment rows exist only for runs after this
batch: a bad week from before it cannot be attributed to
a recorded change, and `attribution_for_week` says
"unknown" for those weeks rather than reconstructing one.
No GitHub push was made, no email was sent, no
current-week picks or leans are in this section.

## Backtest batch 5, group A (2026-11-05, items 1-2) -- situational spots + exact rest/travel/TZ: built, tested, NOTHING promoted

Run it yourself (local games.csv; no network, no team-stats fetch):

```bash
python - << 'PY'
import pandas as pd
from nflpicks import backtest, situational, rest_travel
games = backtest.load_games_frame(pd.read_csv("/path/to/games.csv", low_memory=False))
print(situational.evaluate_all(games)["promoted_spots"])  # [] -- see gate below
print(rest_travel.evaluate_all(games)["promoted_variants"])  # [] -- see gate below
PY
```

**Shared harness.** Both modules replay the baseline through
`situational.replay_with_context()` -- the same loop as
`backtest.evaluate_variant("baseline")` (ratings from season-S
games before week W plus the full season S-1, point-in-time
empirical HFA, `model.predict_game` for the margin) -- and the
replay reproduces the canonical baseline EXACTLY (50.03% ATS,
898/1,795 decided, MAE 10.34 on 2019-2025, n=1,871). If it ever
does not, the numbers built on it are not to be trusted.
Grading uses the batch-4 corrected push rule throughout. The
promotion gate is the same for every test in this batch (ALL
must hold): the variant wins on the TEST split (2023-2025,
coefficients/rules learned on TRAIN 2019-2022 only and frozen),
its full-sample paired p-value survives Bonferroni across its
family, AND it beats the placebo gate. The placebo gate is
computed on the TEST split, not the full sample: a paired delta
on ~782 decided test games has wider chance variation than one
on 1,795, so a full-sample placebo p95 would be too lenient.
Additive placebo = `backtest.placebo_rows()` (+/-0.15 pt, 20
seeds): test-split distribution mean -0.08 pp, SD 0.18, p95
+0.26 pp. Fade placebo = random flips of the same number of
picks (200 draws, seeded): p95 +2.43 to +3.45 pp depending on
how many picks the rule flips -- randomly flipping 100-280
picks moves ATS by that much, and the gate prices that in.

**Item 1 -- Situational spots (`nflpicks/situational.py`):
built, tested, NOT promoted.** Six pre-registered spots, all
defined on the team the model PICKS, from schedule + prior
results + pre-week blended SRS ratings only (a future week's
OPPONENT identity is schedule info, known in advance; the
opponent's STRENGTH rank is always the pre-week rating, never
a full-season rating): lookahead_div (next game is a division
rival), lookahead_strong (next opponent top-5 pre-week),
letdown_upset (won the previous game outright as an underdog),
letdown_blowout (won the previous game by 14+), sandwich
(favourite whose previous AND next opponents are both top-8),
shortweek_fav (favourite on 3+ fewer rest days than its
opponent). Two forms per spot (12 tests, one Bonferroni
family): a fade rule (flip the pick on spot games, only if the
spot's training ATS% is below 50% with >= 30 decided training
games, else follow) and an additive adjustment (shift the
margin by the spot's training mean residual, clipped to
+/-3 pts). Descriptive ATS% on the model's side, full sample:

| spot | n (decided) | ATS% | train ATS% | test ATS% |
|---|---|---|---|---|
| lookahead_div | 656 (628) | 47.29 | 47.91 | 46.47 |
| lookahead_strong | 270 (260) | 48.08 | 45.39 | 51.85 |
| letdown_upset | 346 (333) | 49.25 | 46.35 | 53.19 |
| letdown_blowout | 317 (303) | 47.52 | 45.68 | 49.65 |
| sandwich | 43 (41) | 36.59 | 39.13 | 33.33 |
| shortweek_fav | 56 (54) | 40.74 | 44.44 | 37.04 |

Walk-forward results (fade form, test split, always-follow
49.10% ATS on the same 782 decided test games): lookahead_div
is the only fade with a positive test delta, +2.43 pp
(51.53% vs 49.10%, paired z=1.16, p=0.2467, Bonferroni 1.0,
279 flips) -- and it does NOT beat its random-flip placebo
p95 (+3.45 pp), so it fails the gate on two counts. The other
fades are zero or negative on test (lookahead_strong -0.51 pp,
letdown_upset -1.15 pp, letdown_blowout +0.13 pp). The
additive form is worse everywhere: best test delta is
+0.13 pp (letdown_upset, z=0.33), the rest are 0.00 to
-0.13 pp, all Bonferroni 1.0. Two spots look eye-catching in
the descriptive column and are NOT acted on, by design:
sandwich (36.59%) and shortweek_fav (40.74%) each have fewer
than 30 decided TRAINING games (23 and 27), so their rule
defaults to follow and their adjustment to 0.0 -- a 41-game
descriptive is not a finding, and their per-season ATS% is
unstable (sandwich: 14.29% in 2024, 57.14% in 2025, on 7
decided games each). The training wins that evaporate are
themselves the point of the gate: letdown_upset trained at
46.35% (fade) and tested at 53.19% on the model's side, so
fading it on test LOSES 1.15 pp; lookahead_strong is the same
story (45.39% train, 51.85% test). `SITUATIONAL_LIVE` stays
False; nothing in `model.py`, `weekly_run.py`, or
`dashboard.py` reads the module.

**Item 2 -- Exact rest, travel, time zones
(`nflpicks/rest_travel.py`): built, tested, NOT promoted.**
What "exact" means here, vs the coarse versions in batch 1:
rest is the games.csv `home_rest`/`away_rest` columns (exact
days since the last game -- verified: week-2 Thursday teams
show 4, Sunday teams 7; differential runs -8..+8, with 1,210
of 1,871 games at exactly 0); travel is haversine miles from
the stadium of a team's PREVIOUS game to this game's stadium
(a multi-week road trip flies far less than home->away
implies; a London game far more -- MIN flew 3,842.5 mi into
Tottenham in 2024 week 5, NYJ 3,460.2 mi; week-1 origin is
the home stadium); time zones are DST-aware UTC offsets on
the gameday (US zones shift in March/November, Arizona never
shifts -- ARI vs DEN is 1 zone apart in October, 0 in
December -- London/Munich on European dates, Sao Paulo /
Mexico City unshifted), with eastward travel kept separate
from zones crossed. Neutral venues resolve by stadium name
(international venues directly; host stadiums like State Farm
Stadium / Ford Field / SoFi via their team); an unknown venue
yields no travel rather than an invented distance. Coverage
is complete: travel and TZ are known for all 1,871 games
(0 unadjusted in every variant). Eight pre-registered
variants, coefficients learned on TRAIN (2019-2022) only by
no-intercept OLS of the point-in-time residual on the
continuous variable (rest variants fit against the margin
EXCLUDING the live 0.25/day term; travel/TZ variants against
the baseline margin, which contains no travel term), clipped,
then frozen for TEST:

| variant | train coef | test ATS% vs 49.10% base | test paired delta | full ATS% | p (Bonf.) |
|---|---|---|---|---|---|
| rest_linear | -0.0503 pts/day | 49.43 | +0.38 pp (z=0.63) | 49.45 | 1.0 |
| rest_piecewise | +0.28 home-rested / -0.39 away-rested | 48.80 | -0.13 pp | 49.47 | 1.0 |
| rest_fav_only | +0.2463 pts/day (n=192) | 49.30 | +0.26 pp | 49.81 | 1.0 |
| travel_linear | +0.163 pts/1,000 mi | 49.87 | +0.13 pp | 50.47 | 1.0 |
| travel_binary | +2.891 pts (n=55 train) | 48.53 | -0.64 pp | 49.78 | 1.0 |
| tz_linear | +0.160 pts/zone | 48.86 | -0.51 pp | 50.03 | 1.0 |
| tz_east | -0.634 pts/east zone | 49.68 | +0.38 pp | 50.33 | 1.0 |
| travel_rest_combo | -0.048 rest / +0.063 travel | 49.56 | +0.26 pp | 49.67 | 1.0 |

Reading the table honestly: the train-fitted rest slope is
-0.05 pts/day -- essentially zero, matching batch 1's ATS
residual slope of -0.03 +/- 0.12 and confirming the market
already prices rest; replacing the live 0.25/day term with it
LOSES 0.56 pp full-sample. The piecewise slopes look like an
HFA interaction (rest helps a home team, +0.28, but a rested
away team does worse than the model expects, -0.39) -- but
the variant loses on test (-0.13 pp) and full-sample
(-0.56 pp), so the pattern does not survive walk-forward and
is reported as coefficients, not a finding. The travel
binary is the cleanest train-win-evaporates case in this
batch: a +2.89 pt shift fit on 55 training games where the
away team flew 2,000+ mi further (92 such games in all of
2019-2025), which LOSES 0.64 pp on test -- and the model's
own side was already 58.33% ATS on those 37 test games, so
the shift's flips were the wrong direction. No variant's
test delta beats the additive placebo p95 (+0.26 pp) AND a
Bonferroni p < 0.05 -- rest_linear and tz_east clear the placebo threshold on the delta axis (+0.38 pp vs +0.26 pp) but fail
Bonferroni outright (1.0), and every variant's full-sample
MAE is within 0.08 pts of the baseline 10.34 (tz_east
10.309 is the only MAE improvement, on a variant that is
+0.06 pp ATS full-sample -- noise on both axes).
`TRAVEL_ENABLED` stays False (unchanged -- the exact travel
term does no better than the coarse one it replaces) and
`REST_TRAVEL_EXACT_LIVE` (new) stays False; the live rest
term (0.25/day, capped 2.0) is unchanged, because no exact
replacement beat it out-of-sample.

**Still off by default, on purpose (this batch):**
`SITUATIONAL_LIVE=False`, `REST_TRAVEL_EXACT_LIVE=False`
(new), plus every flag from earlier batches (`EPA_LIVE`,
`ENSEMBLE_LIVE`, `CALIBRATED_PICK`, `TRAVEL_ENABLED`,
`INJ_AUTO_APPLY`, `TOTALS_SRS_LIVE`, `STALENESS_LIVE`,
`UNCERTAINTY_PICK_USE`, `PBP_FEATURES_LIVE`,
`TEAM_HFA_LIVE`, `DYNAMIC_BLEND_LIVE` -- all False,
unchanged).

**Files changed.** New: `nflpicks/situational.py` (spot
definitions, fade/additive walk-forward tests, both placebo
gates, shared point-in-time replay), `nflpicks/rest_travel.py`
(exact rest/travel/TZ quantities, DST-aware offsets,
8-variant walk-forward test), `tests/test_batch5_groupA.py`
(22 tests). Changed: `nflpicks/config.py` (batch-5
thresholds + the two new off-by-default flags only). Not
changed, deliberately: `model.py`, `backtest.py`,
`weekly_run.py`, `dashboard.py`, `.github/workflows/weekly.yml`,
and every earlier README section.

**Tests.** Full suite: 237/237 pass (215 prior + 22 new).
`py_compile` clean on all changed files. Baseline before
this batch: 215/215.

**Limits that remain.** The lookahead/letdown spots use the
most recent previous game, which after a bye is two weeks
old -- a bye-week gap weakens a "coming off" story and is
not modelled separately. Opponent-strength ranks use the
same blended ratings as the pick, so they are in-sample
w.r.t. the ratings (the same shrinkage caveat as team HFA);
the walk-forward gate, not the rank, is the promotion test.
Travel is great-circle stadium-to-stadium: it ignores
actual flight routing, road-trip hotel stays, and the fact
that a team may fly home between road games (the schedule
cannot tell you that). TZ offsets are standard/DST rules,
not the teams' actual travel clocks. The sandwich and
shortweek_fav descriptives (36.59% / 40.74%) are the two
numbers in this batch most likely to be quoted out of
context -- both are sub-30-training-game descriptives that
the gate deliberately refused to act on, and neither is a
promotion candidate until a larger sample (seasons after
2025) says otherwise. No GitHub push was made, no email was
sent, no current-week picks or leans are in this section.

## Backtest batch 5, group B (2026-11-05, items 3-5) -- kickoff weather + unit depletion + referees/coaches: built, tested, NOTHING promoted

Run it yourself (local games.csv; team stats / snap counts / injuries
fetched once from nflverse for items 3-4 -- see the `__main__` blocks):

```bash
python -m nflpicks.kickoff_weather /path/to/games.csv /path/to/nflverse-csvs
python -m nflpicks.unit_depletion /path/to/games.csv /path/to/nflverse-csvs
python -m nflpicks.refs_coaches /path/to/games.csv
```

**Shared harness.** All three modules replay the baseline through
`situational.replay_with_context()` -- the same loop as group A --
and every replay reproduces the canonical baseline EXACTLY (50.03%
ATS, 898/1,795 decided, MAE 10.34 on 2019-2025, n=1,871). The
promotion gate is the same as group A (ALL must hold): the variant
wins on the TEST split (2023-2025, coefficients/rules learned on
TRAIN 2019-2022 only and frozen), its full-sample paired p-value
survives Bonferroni across its family, AND it beats the placebo
gate (additive: +/-0.15 pt placebo p95 +0.26 pp on the test split;
fade: random-flip placebo p95, computed per rule).

**Item 3 -- Weather at kickoff (`nflpicks/kickoff_weather.py`):
built, tested, NOT promoted.** Two halves: a live path and a
backtest, kept separate on purpose.

- Live path: hourly Open-Meteo values at the kickoff hour in the
  stadium's local time -- archive API for past dates, forecast API
  for upcoming games, both free with no key. `gametime` in
  games.csv is Eastern, so the kickoff hour is converted ET ->
  stadium-local with the DST-aware offsets from `rest_travel.py`
  (Arizona never shifts; London 09:30 ET is 14:30 local). Domes
  and closed roofs return None (weather does not apply indoors);
  unknown stadiums and fetch failures also return None, never a
  guessed value. Raw API JSON is cached on disk only when a
  cache_dir is passed (`WEATHER_KICKOFF_CACHE_DIR` is None by
  default); the test suite uses recorded fixtures
  (`tests/fixtures/open_meteo_*.json`, real archive responses)
  with requests monkeypatched, never live network.
- Live-path validation against the recorded values (70 outdoor
  games, all 30 wind-20+ games plus a 40-game random sample,
  fetched once and cached): 70/70 fetched. Temperature agrees
  well (MAE 2.51 F, corr 0.974). Wind agrees only moderately
  (MAE 3.89 mph, corr 0.664) -- and the disagreement is worst
  exactly where a wind adjustment would fire: PHI 2023-12-03
  recorded 44 mph vs 2.4 fetched; SEA 2020-10-11 recorded 10
  vs 23.3 fetched. Checking wind GUSTS does not resolve it
  (PHI gust 8.7 vs recorded 44; SEA gust 41.2 vs recorded 10):
  the game-book wind and the reanalysis wind are different
  measurements, and neither is obviously "truth". Consequence,
  stated plainly: even if the backtest had found a wind effect,
  the live fetch could not reproduce the backtest's wind input
  on the windiest games. The backtest below stands on the
  recorded values; the live path is not trusted to match them.
- Backtest (recorded kickoff wind/temp from games.csv -- kickoff
  values, not daily averages -- plus point-in-time pass/run
  shares from team stats). Descriptives on the model's side:
  wind <15 mph 49.74% ATS (n=967 decided), 15-20 mph 46.15%
  (n=104), 20+ mph 60.00% (n=30 -- noise, wrong direction);
  temp >32 F 50.05% (n=1,037), <=32 F 43.75% (n=64), <=20 F
  27.78% (n=18 -- noise). The totals-side check is also
  inconsistent with the published wind claim: mean (actual
  total - total_line) is +0.31 calm (n=996), -1.50 at 15-20 mph
  (n=106), but +0.42 at 20+ mph (n=30); slope -0.083 pts per
  mph above 15. The 15-20 bucket dips and the 20+ bucket does
  not -- that is not a dose response.
- Pre-registered additive variants (OLS slope on train, frozen):

  | variant | train slope | train decided (x != 0) | test delta | full delta | p (Bonf.) |
  |---|---|---|---|---|---|
  | wind_x_pass (wind>15 x pass-share diff) | +5.62 | 67 | +0.13 pp (z=0.58) | +0.17 pp | 1.0 |
  | wind20_x_pass (wind>20 x pass-share diff) | not learned | 20 | 0.00 pp | 0.00 pp | 1.0 |
  | cold_x_run (cold x run-share diff) | not learned | 27 | 0.00 pp | 0.00 pp | 1.0 |

  wind_x_pass is the only learned variant and it fails twice:
  its test delta (+0.13 pp) is below the placebo p95 (+0.26 pp)
  and its Bonferroni p is 1.0 -- and its train slope (+5.62) is
  the WRONG sign for the hypothesis (it says wind helps the
  pass-heavy team, relative to the model). The other two never
  clear the 30-decided-training-game floor, so their adjustment
  is 0.0 by design, not by fitting. Precipitation is not tested
  at all: games.csv carries no precipitation column, and the
  live fetch returns precip_mm at kickoff for future use, but
  no precip number was invented for the backtest.
  `WEATHER_KICKOFF_LIVE` stays False. The existing daily
  totals-model weather adjustment in `weather.py` is untouched
  (it was never the subject of this test).

**Item 4 -- Player-level matchups / unit depletion
(`nflpicks/unit_depletion.py`): built, tested, NOT promoted.**
The machinery works; the events are too rare to test.

- Starters come from SNAP COUNTS, not depth charts: the
  depth-chart release changed schema after 2024 and is one
  snapshot, while snap counts give a per-game point-in-time
  starter signal for every season. Top-N by snaps over the
  team's most recent 4 played games before week W: OL top 5
  (T/G/C), WR top 3, DL top 4 (DE/DT/NT), CB top 3. A starter
  is OUT only on an OUT/DOUBTFUL week-W injury report --
  Questionable does not count (those players suit up ~75-80%
  of the time). IR players never appear on the weekly report,
  so a starter who went on IR in an earlier week is MISSED --
  a stated limit, not a silent gap. QB is excluded on purpose:
  the injuries module already owns QB value.
- The starter definition was validated against the 2024 depth
  charts (weeks 5/10/15) and 2023 week 10: 92-96% of
  snap-count OL starters are depth-1 players, WR 83-89%, DL
  ~81%, CB 73-82%. (The reverse share is ~0.5 because depth
  charts list more depth-1 players than the top-N -- e.g. slot
  receivers and rotating linemen -- so the snap set is a subset
  of the real starter pool, not a different pool.) Injury joins
  are by normalised name within team, with a last-name +
  first-initial fallback for suffix/punctuation mismatches
  (71 fallback matches across 3,920 starter team-weeks).
- Descriptives on the model's side (picked team flagged):

  | flag | n (decided) | ATS% | train ATS% | test ATS% |
  |---|---|---|---|---|
  | ol_out2 | 33 (32) | 40.62 | 26.67 (15) | 52.94 (17) |
  | wr_out2 | 13 (13) | 53.85 | 42.86 (7) | 66.67 (6) |
  | dl_out2 | 5 (3) | 66.67 | 0.00 (1) | 100.00 (2) |
  | cb_out2 | 16 (15) | 26.67 | 33.33 (9) | 16.67 (6) |
  | any_out2 | 63 (59) | 40.68 | 33.33 (30) | 48.28 (29) |
  | ol_out2_vs_toprush | 7 (7) | 57.14 | 66.67 (3) | 50.00 (4) |

  Two starters out in the same unit in the same week is rare
  (~5 ol_out2 games per season) -- the counts above are the
  finding as much as the percentages. ol_out2's train 26.67%
  evaporates to 52.94% on test, the classic train-win pattern.
  cb_out2 holds its direction (33.33% train, 16.67% test) but
  on 6 test games -- far below any floor for action. Every
  flag except any_out2 defaults to follow / 0.0 adjustment
  because its training sample is below the 30-decided-game
  floor. any_out2 is the only tested rule: fade on test is
  exactly 0.00 pp (13 right / 13 wrong on 30 flips, flip
  placebo p95 +1.28 pp) and the additive form is -0.26 pp
  (adjustment +0.77, MAE 10.319 vs 10.305 follow). Both fail
  the gate. `UNIT_DEPLETION_LIVE` stays False. The flags are
  shadow-only: a Thursday review can look at them, but no
  number here justifies moving a pick.

**Item 5 -- Referee crews and coaching tendencies
(`nflpicks/refs_coaches.py`): built, tested, NOT promoted.**
As expected: noise.

- Referee: prior mean (actual - line) and prior home ATS% over
  the ref's earlier games (point-in-time, >= 10 priors). Career
  2019-2025 home ATS% across the 20 refs with >= 30 games runs
  38.89% (Bill Vinovich, n=111) to 60.38% (John Hussey, n=111)
  -- a wide spread, but with n~110 the SE is ~4.7 pp and 20
  refs guarantee extremes. Penalty rates are NOT tested:
  games.csv has no penalty counts, so no penalty claim is made.
  The totals version (prior mean actual total minus total_line)
  ranges -1.52 to +1.70 across refs (SD 0.75) -- no ref is a
  big over/under ref either. Data-quality note: referee names
  have variants (Ron Torbert n=80 and Ronald Torbert n=31 are
  presumably the same person, split across two spellings), so
  per-ref samples are slightly understated.
- Coach: prior mean residual in the coach's team's favour and
  prior ATS%, following the coach across teams. Career
  2019-2025 ATS% (n>=30) runs 27.27% (Brian Callahan, n=34) to
  65.06% (Dan Campbell, n=85), with Jim Harbaugh 64.71%
  (n=34) and Brian Flores 60.42% (n=49) near the top. Those
  career numbers conflate coach quality with team quality and
  era -- the SRS model already prices the team, so the
  walk-forward test is the arbiter, and it says the record does
  not persist: both coach variants fit NEGATIVE train slopes
  (coach_resid -0.26, coach_ats -3.65), i.e. the train data
  itself says to fade the coach effect, and both lose on test
  anyway.
- 4th-down aggressiveness is NOT tested: the nflverse
  play-by-play release 404s on both the csv.gz and parquet
  URLs (checked 2026-11-05), exactly as batch 2 found. No
  go-rate number is computed, approximated from team totals,
  or invented; `refs_coaches.PBP_AVAILABLE` is False and
  `fourth_down_aggressiveness()` returns None until a working
  PBP source is confirmed.
- Pre-registered additive variants (OLS slope on train, frozen):

  | variant | train slope | test delta | full delta | p (Bonf.) |
  |---|---|---|---|---|
  | ref_home | +0.06 | 0.00 pp | -0.17 pp | 0.95 |
  | coach_resid | -0.26 | -0.38 pp | -0.06 pp | 1.0 |
  | coach_ats | -3.65 | -0.26 pp | -0.11 pp | 1.0 |

  ref_home's train slope (+0.06) is essentially zero -- a
  ref's past home bias carries ~6% of itself forward, which is
  no persistence at all. All three test deltas are <= 0 and
  below the placebo p95 (+0.26 pp); none clears Bonferroni.
  `REF_COACH_LIVE` stays False.

**Still off by default, on purpose (this batch):**
`WEATHER_KICKOFF_LIVE=False`, `UNIT_DEPLETION_LIVE=False`,
`REF_COACH_LIVE=False` (all new), plus every flag from earlier
batches (`EPA_LIVE`, `ENSEMBLE_LIVE`, `CALIBRATED_PICK`,
`TRAVEL_ENABLED`, `INJ_AUTO_APPLY`, `TOTALS_SRS_LIVE`,
`STALENESS_LIVE`, `UNCERTAINTY_PICK_USE`, `PBP_FEATURES_LIVE`,
`TEAM_HFA_LIVE`, `DYNAMIC_BLEND_LIVE`, `SITUATIONAL_LIVE`,
`REST_TRAVEL_EXACT_LIVE` -- all False, unchanged).

**Files changed.** New: `nflpicks/kickoff_weather.py` (kickoff-hour
Open-Meteo fetch + cache + recorded-value validation + 3-variant
walk-forward test), `nflpicks/unit_depletion.py` (snap-count
starter sets, injury-report depletion flags, depth-chart
validation, 6-flag walk-forward test), `nflpicks/refs_coaches.py`
(point-in-time ref/coach features, 3-variant walk-forward test,
4th-down documented unavailable), `tests/test_batch5_groupB.py`
(34 tests), `tests/fixtures/open_meteo_buf_20240908.json` and
`tests/fixtures/open_meteo_den_20201219.json` (real recorded
Open-Meteo archive responses for the fixture tests). Changed:
`nflpicks/config.py` (batch-5 group B thresholds, stadium
timezones, and the three new off-by-default flags only). Not
changed, deliberately: `model.py`, `backtest.py`, `weather.py`
(the daily totals-model weather path is untouched), `weekly_run.py`,
`dashboard.py`, `.github/workflows/weekly.yml`, and every earlier
README section.

**Tests.** Full suite: 271/271 pass (237 prior + 34 new).
`py_compile` clean on all changed files. Baseline before this
batch: 237/237.

**Limits that remain.** The kickoff-weather backtest grades
realised (recorded) weather; a live forecast will sometimes be
wrong on top of that, and the live fetch demonstrably disagrees
with the recorded wind on the windiest games, so the live path
needs its own validation before it could ever feed a pick --
this batch validates the plumbing, not the forecast. The
unit-depletion flags miss IR starters (weekly report only) and
rest on name matching (71 fallback matches; a missed join
understates depletion, it never invents it). The referee test
cannot see penalty counts, only ATS/total outcomes, and ref
name variants split a few samples. The coach test cannot see
4th-down aggressiveness at all (no play-by-play source). All
three items' headline numbers are nulls on 2019-2025; a real
effect smaller than these gates can detect would need seasons
after 2025 to show up. No GitHub push was made, no email was
sent, no current-week picks or leans are in this section.

## Backtest batch 5, group C (2026-11-05, items 6-9) -- tests on every push + committed-DB setup + per-game explanations + flip sensitivity: built, tested, NOTHING promoted

No model change in this batch. Items 8-9 are labels/display only
(they read numbers model.py already computed), item 6 is CI, and
item 7 is setup/documentation for a feature built in batch 4.
The canonical baseline is untouched -- every replay still
reproduces 50.03% ATS (898/1,795 decided, MAE 10.34 on
2019-2025, n=1,871) exactly, and no backtest was re-run here
because no pick logic, rating, or blend changed. Nothing in
this batch can change a pick, an edge, a line, or a grade.

### Item 6 -- tests-on-every-push workflow: built, NOT pushed

New file `.github/workflows/tests.yml` (repo root, next to
`weekly.yml`), same pattern as the Stock Terminal repo's
25-line tests workflow: on every push and pull request it
checks out, sets up Python 3.12, installs
`nfl-picks-app/requirements.txt`, compiles all modules
(`python3 -m py_compile nfl-picks-app/nflpicks/*.py
nfl-picks-app/*.py`), and runs the full suite
(`python3 -m unittest discover -s tests -v` from
`nfl-picks-app/`). All tests are offline -- no network calls,
no secrets.

Why a second workflow when `weekly.yml` already runs the tests
first: `weekly.yml` only runs on its Tue/Thu schedule. A bad
push on Monday would otherwise sit uncaught until Tuesday's
run -- or worse, fail the Thursday run that matters for the
pool deadline. `tests.yml` catches it on the push itself.

NOT pushed by this batch: workflow files cannot be pushed
through the GitHub contents API (it returns 403 on
`.github/workflows/*`), exactly as established for the Stock
Terminal workflow and for `weekly.yml`. This file was built
locally and validated (YAML parses; the steps are the same
commands run locally below). It must be added via the GitHub
website or a git push from a local checkout -- see the note
at the top of the file itself.

### Item 7 -- committed-DB sync, configured: built, needs a user step

The sync itself was built in batch 4 (group D, item 8:
`committed_db.compare_to_local` /
`sync_committed_to_local`, user tables preserved, run-time
reported so CDN lag is visible). It was never turned on: the
dashboard's sync button does nothing until a source is
configured, and for a PRIVATE repo that source also needs a
token. This item is the configuration, not a code change to
the sync logic.

What was built:

* `committed_db.setup_instructions()` -- the exact setup, as
  data the dashboard and README both draw on: which env vars
  (`PICKS_GITHUB_REPO` -- required, `owner/repo` copied from
  the repo's GitHub URL, no default provided and none guessed;
  `PICKS_GITHUB_BRANCH` default `main`; alternatives
  `PICKS_COMMITTED_DB_URL` / `PICKS_COMMITTED_DB_FILE`),
  the token (`PICKS_GITHUB_TOKEN`, private repos only --
  created in GitHub Settings -> Developer settings ->
  Personal access tokens, fine-grained, this repo only,
  Contents read-only, or a classic token with `repo` scope),
  where to put them in Streamlit Cloud (App Settings ->
  Secrets -- Streamlit Cloud has no plain env-var UI, so
  Secrets is the only place), the local equivalent (export
  in the shell before running the dashboard), and how to
  verify (reload: the banner changes from "local only" to a
  match note or a divergence warning with a Sync button;
  check the committed run time against the expected
  Thursday run; click Sync once and confirm the success
  note lists replaced workflow tables and preserved user
  tables).
* `committed_db.apply_streamlit_secrets(secrets)` -- the
  bridge that makes the Cloud path actually work: called
  once on dashboard load with `st.secrets`, it copies only
  the five known committed-DB keys into `os.environ`, never
  overwrites an already-set env var, ignores unknown keys,
  returns only the names set (never a value), and returns []
  instead of raising when there are no Secrets. The dashboard
  call is best-effort (try/except), so a local run without
  Secrets is unchanged.
* Dashboard unconfigured state: the old generic "local
  database only" caption now names exactly what is missing
  and what to set (`PICKS_GITHUB_REPO` + `PICKS_GITHUB_TOKEN`
  for a private repo, App Settings -> Secrets in Streamlit
  Cloud, export locally), and points at this README section
  for the full steps and verification. When a repo-mode fetch
  fails and no token is set, the dashboard adds the hint that
  a missing token looks identical to a missing file (by
  design in `fetch_committed_db_bytes`), so the failure is
  diagnosable instead of opaque.

What was NOT done, on purpose: no token was invented,
created, stored, or written anywhere -- no token value
exists in code, docs, or tests (the tests assert the
instructions contain no `ghp_` / `github_pat_` string).
Creating the token is a user step in their own GitHub
account, and the repo name is theirs to copy from the URL.
Until they do it, the dashboard keeps saying plainly that
no committed source is configured; it never pretends a
check happened.

### Item 8 -- per-game explanations in plain English: built, display only

New module `nflpicks/explain.py`: `explain_pick(game)` turns
one stored pick row into a one-line "why" (factors joined
with "; "), and `explain_picks(rows)` maps it over a slate.
The dashboard shows it as a "Why" column on the ATS tab.
Generated from the same numbers already computed -- no new
model, no new data source, no pick-logic change. Every clause
names a number already stored for that game; missing inputs
are skipped, never invented (no line -> "No line yet ... no
pick can be made until a Vegas or pool line exists").

What a line can say, in order: the rating edge ("model rates
KC 6.0 pts better than the Vegas line (-3.5)" -- pool line
when one is set, since that is the judged line); market
disagreement (`needs_review` -> "the market disagrees by 8.5
pts -- verify the inputs before trusting this, do not bet
bigger on it"; `strong_edge` -> "a strong model-vs-market
gap"); cover prob, with <0.55 called "near a coin flip"; the
uncertainty band, when the cover-prob range straddles 0.50
("rating uncertainty alone could flip the favoured side");
early-season sample ("confidence 0.45"); a >=1 pt live line
move since the pool line locked (from `db.get_line_moves`,
which the tab now computes once and reuses for its review
section); injuries ("review-only, not in the number"); the
weather note (totals only); and the total ("model 48.0 vs
line 44.5 (OVER 44.5)"). If the stored pick string names the
other side from the numbers, that is flagged in the line
("stored pick names the other side -- check the row before
submitting") instead of being papered over -- the submission
sheet already does a sign check, and this is the same idea
at the game level.

### Item 9 -- sensitivity: what would change my pick?: built, display only

New module `nflpicks/sensitivity.py`: `flip_analysis(model,
line)` / `total_flip_analysis(model_total, total_line)` /
`sensitivity_for_game(game)`. The dashboard shows the spread
summary as a "What flips it" column on the ATS tab.

The answer is one number, and there is nothing to fit: the
pick follows the SIGN of (model_independent - line), and the
blended edge has the same sign (blend weight is strictly
positive), so the pick flips exactly when the line crosses
the model read -- or, equivalently, when the model read
crosses the line. The flip line IS the model read, the move
needed IS |model - line|, and the rating change needed is
the same magnitude in the opposite direction. The total pick
flips the same way, when the total line crosses the model
total. Labels (thresholds in `config.py`,
`SENSITIVITY_FRAGILE_PTS = 0.5`, `SENSITIVITY_SOLID_PTS = 3.0`):
<= 0.5 pts is "fragile" (the next half-point move -- the
smallest a line normally moves -- flips it), >= 3.0 pts is a
"real lean", between is a "thin lean". Example outputs:
"Flips if the line moves 0.5 pts up (home more favoured) (to
+2.5) or the model moves 0.5 pts down -- fragile." At an exact
tie the summary says "Already on the line -- any move flips
this pick (fragile)." With no line there is no pick to flip,
and the function says so instead of inventing a number.

A fragile pick is not a wrong pick -- it is one a Thursday
line move can take away from you, which is why it sits next
to the "Why" column rather than in a model table.

**Still off by default, on purpose (this batch):** no new
flag was added -- items 8-9 need none (display only) and
items 6-7 are CI/setup. Every flag from earlier batches
(`EPA_LIVE`, `ENSEMBLE_LIVE`, `CALIBRATED_PICK`,
`TRAVEL_ENABLED`, `INJ_AUTO_APPLY`, `TOTALS_SRS_LIVE`,
`STALENESS_LIVE`, `UNCERTAINTY_PICK_USE`, `PBP_FEATURES_LIVE`,
`TEAM_HFA_LIVE`, `DYNAMIC_BLEND_LIVE`, `SITUATIONAL_LIVE`,
`REST_TRAVEL_EXACT_LIVE`, `WEATHER_KICKOFF_LIVE`,
`UNIT_DEPLETION_LIVE`, `REF_COACH_LIVE` -- all False,
unchanged) is now asserted False by
`tests/test_batch5_groupC.py`, so a future batch cannot
silently flip one while adding display code.

**Files changed.** New: `nflpicks/explain.py` (item 8),
`nflpicks/sensitivity.py` (item 9),
`tests/test_batch5_groupC.py` (39 tests),
`.github/workflows/tests.yml` (item 6, not pushed -- see
above). Changed: `nflpicks/config.py` (two sensitivity
label thresholds only), `nflpicks/committed_db.py`
(`setup_instructions()` + `apply_streamlit_secrets()` +
token doc, item 7; sync logic untouched),
`dashboard.py` (Secrets bridge call, exact
unconfigured-state wording + token hint, Why / What-flips-it
columns, line-moves fetch moved up and reused; no pick, line,
or grade logic touched), `README.md` (this section + the
architecture list). Not changed, deliberately: `model.py`,
`backtest.py`, `weekly_run.py`, `.github/workflows/weekly.yml`,
`grading.py`, `db.py`, and every earlier README section. The
play-by-play source question from earlier batches is not
re-litigated here: this batch touches no pbp claim, and the
item-5 re-check discrepancy (a worker reporting the nflverse
pbp release 404ing again vs batch 3's confirmed source) is
still unresolved -- nothing in this section asserts the 404
as settled fact.

**Tests.** Full suite: 310/310 pass (271 prior + 39 new). `py_compile` clean on all changed files
(`nflpicks/*.py`, `dashboard.py`, `weekly_run.py`), and the
new workflow file parses as YAML with the expected triggers
(push + pull_request), compile step, and unittest step.
Baseline before this batch: 271/271.

**Limits that remain.** The dashboard was not clicked live:
Streamlit is not installed in this environment, so the Why /
What-flips-it columns, the Secrets bridge call, and the new
unconfigured-state wording are compile-checked and
unit-tested at the function level, not live-rendered -- the
column-count logic for all four cover-prob present/absent
cases was verified offline against fake pick rows (20/19/19/18
columns, matching headers in every case). The tests workflow
has not run on GitHub Actions (it cannot be pushed from here
-- see item 6), so its first real run is still to be
confirmed after the pusher adds it. The committed-DB sync
still needs the user's token + repo name before the Sync
button does anything; until then the dashboard's honest
state is "local only". No GitHub push was made, no email was
sent, no current-week picks or leans are in this section.
