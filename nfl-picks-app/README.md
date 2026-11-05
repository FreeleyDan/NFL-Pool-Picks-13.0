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
