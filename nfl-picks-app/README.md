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
