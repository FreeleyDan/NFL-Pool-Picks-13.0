"""Tests for batch 5, group B items 3-5 (kickoff weather; unit
depletion; referees & coaches). No network, no games.csv needed:
all live-fetch tests use recorded Open-Meteo fixtures under
tests/fixtures/ with requests.get monkeypatched, and all harness
numbers live in the README batch-5 group B section.

Run from nfl-picks-app/: python -m unittest discover -s tests -v
No live behaviour changes: no flag is flipped, no pick is changed.
"""
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from nflpicks import backtest, config
from nflpicks import kickoff_weather as kw
from nflpicks import refs_coaches as rc
from nflpicks import unit_depletion as ud

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _load_fixture(name):
    return json.loads((FIXTURES / name).read_text())


def _game(season, week, home, away, hs, asc, line, **kw_):
    row = {"season": season, "week": week, "game_type": "REG",
           "home_team": home, "away_team": away, "home_score": hs,
           "away_score": asc, "result": float(hs - asc),
           "spread_line": float(line), "div_game": 0,
           "home_rest": 7, "away_rest": 7, "gameday": "2024-09-08",
           "location": "Home", "stadium": "", "roof": "outdoors",
           "wind": None, "temp": None, "gametime": "13:00",
           "referee": "Carl Cheffers", "home_coach": "Sean McDermott",
           "away_coach": "Andy Reid", "total": 45.0, "total_line": 44.0}
    row.update(kw_)
    return row


def _season_frame(games):
    return backtest.load_games_frame(pd.DataFrame(games))


class TestKickoffHour(unittest.TestCase):
    def test_eastern_kickoff_unchanged(self):
        # 13:00 ET in Buffalo (Eastern) is 13:00 local.
        self.assertEqual(kw.local_kickoff_hour("13:00", "2024-09-08", "BUF"), 13)

    def test_mountain_kickoff_converted(self):
        # 16:30 ET in Denver (Mountain, DST in December? No -- Dec
        # is standard time: ET -5, MT -7, so 16:30 ET is 14:30 MT).
        self.assertEqual(kw.local_kickoff_hour("16:30", "2020-12-19", "DEN"), 14)

    def test_pacific_prime_time(self):
        # 20:20 ET in Seattle is 17:20 PT (DST, September).
        self.assertEqual(kw.local_kickoff_hour("20:20", "2024-09-08", "SEA"), 17)

    def test_london_morning_game(self):
        # 09:30 ET in London is 14:30 local (BST in October).
        hour = kw.local_kickoff_hour("09:30", "2024-10-13", "JAX",
                                     stadium="Tottenham Stadium",
                                     location="Neutral")
        self.assertEqual(hour, 14)

    def test_unknown_gametime_is_none(self):
        self.assertIsNone(kw.local_kickoff_hour(None, "2024-09-08", "BUF"))
        self.assertIsNone(kw.local_kickoff_hour("not-a-time", "2024-09-08", "BUF"))

    def test_arizona_never_shifts(self):
        # ARI is -7 year-round: 16:25 ET in July (EDT, -4) is 13:25
        # local; in January (EST, -5) it is 14:25 local.
        self.assertEqual(kw.local_kickoff_hour("16:25", "2024-09-08", "ARI"), 13)
        self.assertEqual(kw.local_kickoff_hour("16:25", "2024-01-07", "ARI"), 14)


class TestKickoffFetchFromFixture(unittest.TestCase):
    def test_hour_extraction_from_real_archive_response(self):
        hourly = _load_fixture("open_meteo_buf_20240908.json")
        vals = kw.kickoff_values(hourly, 13)
        self.assertIsNotNone(vals)
        # Real recorded response for Buffalo 2024-09-08, 13:00 local.
        self.assertAlmostEqual(vals["wind_mph"], 20.6)
        self.assertAlmostEqual(vals["temp_f"], 59.4)
        self.assertAlmostEqual(vals["precip_mm"], 0.2)

    def test_missing_hour_is_none(self):
        hourly = {"hourly": {"time": ["2024-09-08T13:00"],
                             "wind_speed_10m": [10.0],
                             "temperature_2m": [60.0],
                             "precipitation": [0.0]}}
        self.assertIsNone(kw.kickoff_values(hourly, 14))

    def test_dome_returns_none_without_fetch(self):
        row = _game(2024, 1, "ATL", "TB", 24, 20, 3.0, roof="dome")
        with mock.patch.object(kw.requests, "get") as get:
            self.assertIsNone(kw.kickoff_weather_for_game(row))
            get.assert_not_called()

    def test_closed_roof_returns_none(self):
        row = _game(2024, 1, "ATL", "TB", 24, 20, 3.0, roof="closed")
        self.assertIsNone(kw.kickoff_weather_for_game(row))

    def test_fetch_uses_fixture_via_monkeypatched_requests(self):
        hourly = _load_fixture("open_meteo_buf_20240908.json")

        class Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return hourly

        row = _game(2024, 1, "BUF", "ARI", 34, 28, 6.5,
                    gameday="2024-09-08", gametime="13:00")
        with mock.patch.object(kw.requests, "get", return_value=Resp()) as get:
            got = kw.kickoff_weather_for_game(row)
            self.assertIsNotNone(got)
            self.assertAlmostEqual(got["wind_mph"], 20.6)
            self.assertAlmostEqual(got["temp_f"], 59.4)
            self.assertEqual(got["source"], "archive")
            url = get.call_args[0][0]
            self.assertIn("archive-api.open-meteo.com", url)

    def test_denver_hour_conversion_end_to_end(self):
        # 16:30 ET kickoff in Denver -> 14:00 local hour in the
        # fixture (recorded 2020-12-19 response).
        hourly = _load_fixture("open_meteo_den_20201219.json")

        class Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return hourly

        row = _game(2020, 15, "DEN", "BUF", 24, 20, 3.0,
                    gameday="2020-12-19", gametime="16:30")
        with mock.patch.object(kw.requests, "get", return_value=Resp()):
            got = kw.kickoff_weather_for_game(row)
            self.assertIsNotNone(got)
            self.assertEqual(got["hour"], 14)
            self.assertAlmostEqual(got["wind_mph"], 13.5)
            self.assertAlmostEqual(got["temp_f"], 44.2)

    def test_unknown_team_returns_none(self):
        row = _game(2024, 1, "XXX", "TB", 24, 20, 3.0)
        self.assertIsNone(kw.kickoff_weather_for_game(row))

    def test_cache_round_trip(self):
        import tempfile
        hourly = _load_fixture("open_meteo_buf_20240908.json")

        class Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return hourly

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(kw.requests, "get", return_value=Resp()) as get:
                first = kw.fetch_hourly(42.7738, -78.7870, "2024-09-08",
                                        cache_dir=tmp)
                self.assertIsNotNone(first)
                self.assertEqual(get.call_count, 1)
                # Second call reads the cache file: no network.
                second = kw.fetch_hourly(42.7738, -78.7870, "2024-09-08",
                                         cache_dir=tmp)
                self.assertEqual(get.call_count, 1)
                self.assertEqual(first, second)


class TestWeatherVariantMath(unittest.TestCase):
    def test_wind_x_pass_zero_when_calm_or_indoor(self):
        calm = {"wind": 8.0, "temp": 60.0, "roof": "outdoors",
                "pass_diff": 0.1, "run_diff": -0.1}
        self.assertEqual(kw._variant_x(calm, "wind_x_pass"), 0.0)
        dome = {"wind": 25.0, "temp": 70.0, "roof": "dome",
                "pass_diff": 0.1, "run_diff": -0.1}
        self.assertEqual(kw._variant_x(dome, "wind_x_pass"), 0.0)
        windy = {"wind": 22.0, "temp": 60.0, "roof": "outdoors",
                 "pass_diff": 0.1, "run_diff": -0.1}
        self.assertAlmostEqual(kw._variant_x(windy, "wind_x_pass"), 0.7)
        self.assertAlmostEqual(kw._variant_x(windy, "wind20_x_pass"), 0.2)

    def test_cold_x_run_zero_when_warm(self):
        warm = {"wind": 5.0, "temp": 55.0, "roof": "outdoors",
                "pass_diff": 0.1, "run_diff": 0.2}
        self.assertEqual(kw._variant_x(warm, "cold_x_run"), 0.0)
        cold = {"wind": 5.0, "temp": 20.0, "roof": "outdoors",
                "pass_diff": 0.1, "run_diff": 0.2}
        self.assertAlmostEqual(kw._variant_x(cold, "cold_x_run"), 2.4)

    def test_missing_wind_is_zero_not_imputed(self):
        row = {"wind": None, "temp": None, "roof": "outdoors",
               "pass_diff": 0.1, "run_diff": 0.2}
        self.assertEqual(kw._variant_x(row, "wind_x_pass"), 0.0)
        self.assertEqual(kw._variant_x(row, "cold_x_run"), 0.0)

    def test_flags_stay_off(self):
        self.assertFalse(config.WEATHER_KICKOFF_LIVE)
        self.assertFalse(config.UNIT_DEPLETION_LIVE)
        self.assertFalse(config.REF_COACH_LIVE)


class TestUnitDepletion(unittest.TestCase):
    def _snaps(self, rows):
        return pd.DataFrame(rows, columns=["season", "week", "team", "player",
                                           "position", "offense_snaps",
                                           "defense_snaps"])

    def test_starter_set_is_top_n_by_recent_snaps(self):
        # BUF week 1-4: 5 OL with full snaps, a 6th with few. Week 5
        # starters must be the top 5, not the part-timer.
        rows = []
        for wk in (1, 2, 3, 4):
            for i, name in enumerate(["Dawkins", "Edwards", "McGovern",
                                      "Torrence", "Brown"]):
                rows.append([2024, wk, "BUF", name, "T" if i in (0, 4) else "G",
                             60, 0])
            rows.append([2024, wk, "BUF", "Anderson", "G", 10, 0])
        # Week 5 rows must exist for a week-5 set to be built; the
        # set itself is computed from weeks 1-4 only.
        for name in ["Dawkins", "Edwards", "McGovern", "Torrence", "Brown",
                     "Anderson"]:
            rows.append([2024, 5, "BUF", name, "T", 60, 0])
        sets = ud.build_starter_sets(self._snaps(rows))
        self.assertEqual(sets[("BUF", 2024, 5)]["OL"],
                         frozenset(["dawkins", "edwards", "mcgovern",
                                    "torrence", "brown"]))

    def test_week1_has_no_starters(self):
        rows = [[2024, 1, "BUF", "Dawkins", "T", 60, 0]]
        sets = ud.build_starter_sets(self._snaps(rows))
        self.assertEqual(sets[("BUF", 2024, 1)]["OL"], frozenset())

    def test_lookback_spans_seasons(self):
        rows = []
        for wk in (16, 17, 18):
            for name in ["Dawkins", "Edwards", "McGovern", "Torrence", "Brown"]:
                rows.append([2023, wk, "BUF", name, "T", 60, 0])
        for name in ["Dawkins", "Edwards", "McGovern", "Torrence", "Brown"]:
            rows.append([2024, 1, "BUF", name, "T", 60, 0])
            rows.append([2024, 2, "BUF", name, "T", 60, 0])
        sets = ud.build_starter_sets(self._snaps(rows))
        # Week 2 of 2024 looks back at 2024 wk 1 + 2023 wks 16-18.
        self.assertEqual(len(sets[("BUF", 2024, 2)]["OL"]), 5)

    def test_out_counts_only_out_and_doubtful(self):
        inj = pd.DataFrame([
            [2024, 5, "BUF", "Out", "Dawkins", "T"],
            [2024, 5, "BUF", "Doubtful", "Edwards", "G"],
            [2024, 5, "BUF", "Questionable", "McGovern", "C"],
            [2024, 5, "BUF", "Out", "Somebody Else", "WR"],
        ], columns=["season", "week", "team", "report_status", "full_name",
                    "position"])
        out_sets = ud.build_out_sets(inj)
        starters = {"OL": frozenset(["dawkins", "edwards", "mcgovern",
                                      "torrence", "brown"])}
        n, n_fb = ud.count_out_starters(starters["OL"],
                                        out_sets[(2024, 5, "BUF")])
        self.assertEqual(n, 2)  # Out + Doubtful; Questionable does not count.
        self.assertEqual(n_fb, 0)

    def test_name_normalization_matches_punctuation(self):
        self.assertEqual(ud.normalize_name("O'Cyrus Torrence"),
                         "o cyrus torrence")
        self.assertEqual(ud.normalize_name("A.J. Brown"), "a j brown")

    def test_fallback_matches_suffix_mismatch(self):
        inj = pd.DataFrame([
            [2024, 5, "BUF", "Out", "Marvin Harrison Jr.", "WR"],
        ], columns=["season", "week", "team", "report_status", "full_name",
                    "position"])
        out_sets = ud.build_out_sets(inj)
        n, n_fb = ud.count_out_starters(frozenset(["marvin harrison"]),
                                        out_sets[(2024, 5, "BUF")])
        self.assertEqual((n, n_fb), (1, 1))

    def test_flags_for_game(self):
        snaps = self._snaps([
            [2024, wk, "BUF", name, "T", 60, 0]
            for wk in (1, 2, 3, 4)
            for name in ["Dawkins", "Edwards", "McGovern", "Torrence", "Brown"]
        ] + [
            [2024, 5, "BUF", name, "T", 60, 0]
            for name in ["Dawkins", "Edwards", "McGovern", "Torrence", "Brown"]
        ] + [
            [2024, wk, "KC", name, "WR", 55, 0]
            for wk in (1, 2, 3, 4)
            for name in ["Rice", "Worthy", "Brown"]
        ] + [
            [2024, 5, "KC", name, "WR", 55, 0]
            for name in ["Rice", "Worthy", "Brown"]
        ])
        inj = pd.DataFrame([
            [2024, 5, "BUF", "Out", "Dawkins", "T"],
            [2024, 5, "BUF", "Out", "Edwards", "G"],
            [2024, 5, "KC", "Out", "Rice", "WR"],
        ], columns=["season", "week", "team", "report_status", "full_name",
                    "position"])
        sets = ud.build_starter_sets(snaps)
        outs = ud.build_out_sets(inj)
        flags = ud.flags_for_game("BUF", "KC", 2024, 5, sets, outs)
        self.assertTrue(flags["ol_out2_home"])
        self.assertEqual(flags["ol_out_n_home"], 2)
        self.assertFalse(flags["wr_out2_away"])  # only 1 WR out
        self.assertTrue(flags["any_out2_home"])
        self.assertFalse(flags["any_out2_away"])
        self.assertFalse(flags["ol_out2_vs_toprush_home"])  # no pressure map

    def test_toprush_interaction_needs_pressure_map(self):
        snaps = self._snaps([
            [2024, wk, "BUF", name, "T", 60, 0]
            for wk in (1, 2, 3, 4)
            for name in ["Dawkins", "Edwards", "McGovern", "Torrence", "Brown"]
        ] + [
            [2024, 5, "BUF", name, "T", 60, 0]
            for name in ["Dawkins", "Edwards", "McGovern", "Torrence", "Brown"]
        ])
        inj = pd.DataFrame([
            [2024, 5, "BUF", "Out", "Dawkins", "T"],
            [2024, 5, "BUF", "Out", "Edwards", "G"],
        ], columns=["season", "week", "team", "report_status", "full_name",
                    "position"])
        sets = ud.build_starter_sets(snaps)
        outs = ud.build_out_sets(inj)
        flags = ud.flags_for_game("BUF", "KC", 2024, 5, sets, outs,
                                  opp_pressure_top={(2024, 5, "KC"): True})
        self.assertTrue(flags["ol_out2_vs_toprush_home"])
        flags2 = ud.flags_for_game("BUF", "KC", 2024, 5, sets, outs,
                                   opp_pressure_top={(2024, 5, "KC"): False})
        self.assertFalse(flags2["ol_out2_vs_toprush_home"])

    def test_fade_defaults_to_follow_below_min_train_games(self):
        rows = [{"model_margin": 3.0, "spread_line": 2.5, "actual_margin": 6.0,
                 "ol_out2_home": True, "home_team": "BUF", "away_team": "KC",
                 "season": 2023, "week": 5}]
        self.assertEqual(ud._learn_rules(rows), {f: "follow" for f in ud.FLAGS})
        self.assertEqual(ud._learn_adjustments(rows),
                         {f: 0.0 for f in ud.FLAGS})


class TestRefsCoaches(unittest.TestCase):
    def test_fourth_down_is_unavailable_not_invented(self):
        self.assertFalse(rc.PBP_AVAILABLE)
        self.assertIsNone(rc.fourth_down_aggressiveness())
        self.assertIn("404", rc.FOURTH_DOWN_STATUS)

    def test_ref_prior_uses_only_earlier_games(self):
        # Ref "R1": home covered by 10 in weeks 1-2, then week 3's
        # feature must be the mean of weeks 1-2 only (+10), and
        # week 1 must be None (no priors yet / below the minimum).
        games = _season_frame([
            _game(2024, 1, "BUF", "KC", 30, 20, 0.0, referee="R1"),
            _game(2024, 2, "BUF", "KC", 30, 20, 0.0, referee="R1"),
            _game(2024, 3, "BUF", "KC", 30, 20, 0.0, referee="R1"),
        ])
        with mock.patch.object(config, "REF_COACH_MIN_PRIOR_GAMES", 1):
            feats = rc.precompute_features(games)
        self.assertIsNone(feats[(2024, 1, "BUF", "KC")]["ref_home_resid"])
        self.assertAlmostEqual(
            feats[(2024, 2, "BUF", "KC")]["ref_home_resid"], 10.0)
        self.assertAlmostEqual(
            feats[(2024, 3, "BUF", "KC")]["ref_home_resid"], 10.0)

    def test_min_prior_games_gate(self):
        games = _season_frame([
            _game(2024, 1, "BUF", "KC", 30, 20, 0.0, referee="R1"),
            _game(2024, 2, "BUF", "KC", 30, 20, 0.0, referee="R1"),
        ])
        # Default minimum is 10: one prior game is not enough.
        feats = rc.precompute_features(games)
        self.assertIsNone(feats[(2024, 2, "BUF", "KC")]["ref_home_resid"])

    def test_coach_residual_follows_the_coach(self):
        # Coach "C1" coaches BUF in week 1 (BUF covers by 10) then
        # KC in week 2. Week 2's coach_resid_diff must credit C1's
        # week-1 game to KC's side (away), i.e. -10 for the away
        # coach, so the diff is 0 - (-10) = +10 for the home coach
        # only if the home coach also has a prior -- here the home
        # coach has none, so the diff is None (both need priors).
        games = _season_frame([
            _game(2024, 1, "BUF", "KC", 30, 20, 0.0, home_coach="C1",
                  away_coach="C2"),
            _game(2024, 2, "KC", "BUF", 20, 30, 0.0, home_coach="C2",
                  away_coach="C1"),
        ])
        with mock.patch.object(config, "REF_COACH_MIN_PRIOR_GAMES", 1):
            feats = rc.precompute_features(games)
        # Week 2: home coach C2 has 1 prior (week 1, away, lost ATS
        # by 10 -> team-favour margin -10); away coach C1 has 1
        # prior (week 1, home, +10). Diff = -10 - (+10) = -20.
        self.assertAlmostEqual(
            feats[(2024, 2, "KC", "BUF")]["coach_resid_diff"], -20.0)

    def test_ref_home_ats_excludes_pushes(self):
        games = _season_frame([
            _game(2024, 1, "BUF", "KC", 23, 20, 3.0, referee="R1"),
            _game(2024, 2, "BUF", "KC", 23, 20, 3.0, referee="R1"),
            _game(2024, 3, "BUF", "KC", 23, 20, 3.0, referee="R1"),
        ])
        with mock.patch.object(config, "REF_COACH_MIN_PRIOR_GAMES", 1):
            feats = rc.precompute_features(games)
        # Weeks 1-2: actual == line -> pushes, decided 0 -> ATS None.
        self.assertIsNone(feats[(2024, 3, "BUF", "KC")]["ref_home_ats"])
        # But the mean residual is still defined (0.0).
        self.assertAlmostEqual(
            feats[(2024, 3, "BUF", "KC")]["ref_home_resid"], 0.0)

    def test_variant_x_zero_when_no_prior(self):
        row = {"ref_home_resid": None, "coach_resid_diff": None,
               "coach_ats_diff": None}
        for variant in rc.VARIANTS:
            self.assertEqual(rc._variant_x(row, variant), 0.0)

    def test_describe_refs_respects_min_games(self):
        games = _season_frame([
            _game(2024, wk, "BUF", "KC", 30, 20, 0.0, referee="R1")
            for wk in (1, 2, 3)
        ])
        self.assertEqual(rc.describe_refs(games, min_games=30), {})
        self.assertEqual(len(rc.describe_refs(games, min_games=3)), 1)


if __name__ == "__main__":
    unittest.main()
