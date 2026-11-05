"""Tests for batch-3 items 1-2 (standings risk / season simulation).
Run from nfl-picks-app/: python -m unittest discover -s tests -v
No network, no Streamlit needed. All tests use synthetic slates;
the historical validation numbers in the README come from running
standings.evaluate_late_window() on real backtest rows (see README),
not from these unit tests.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nflpicks import config, simulation, standings


def game(home, away, side, prob):
    return {"home": home, "away": away, "default_side": side, "pick_prob": prob}


def sim_game(p_home, user_side, leader_side=None):
    return {"p_home": p_home, "user_side": user_side, "leader_side": leader_side}


class TestStandingsMode(unittest.TestCase):
    def test_early_rule_off_no_matter_the_deficit(self):
        self.assertEqual(standings.standings_mode(50, 60, 10)["mode"], "early")
        out = standings.suggest_gambles(
            [game("KC", "DEN", "home", 0.51)], 50, 60, 10)
        self.assertEqual(out["mode"], "early")
        self.assertEqual(out["gambles"], [])
        self.assertEqual(out["games"][0]["label"], "DEFAULT")

    def test_trailing_threshold_and_last_week_exception(self):
        # Trailing by 1 with 4 weeks left: NOT trailing (recoverable).
        self.assertEqual(standings.standings_mode(50, 51, 4)["mode"], "tied_late")
        # Trailing by 2 with 4 weeks left: trailing.
        self.assertEqual(standings.standings_mode(50, 52, 4)["mode"], "trailing")
        # Trailing by 1 in the final week: trailing (only differentiation wins).
        self.assertEqual(standings.standings_mode(50, 51, 1)["mode"], "trailing")
        # Tied late is its own neutral mode, not trailing.
        self.assertEqual(standings.standings_mode(50, 50, 2)["mode"], "tied_late")
        self.assertEqual(standings.standings_mode(50, 49, 2)["mode"], "leading")
        self.assertEqual(standings.standings_mode(50, 50, 0)["mode"], "done")

    def test_invalid_inputs_rejected(self):
        with self.assertRaises(ValueError):
            standings.standings_mode(-1, 50, 4)
        with self.assertRaises(ValueError):
            standings.standings_mode(50, 50, -1)
        with self.assertRaises(ValueError):
            standings.suggest_gambles(
                [game("KC", "DEN", "KC", 0.5)], 50, 52, 4)


class TestSuggestGambles(unittest.TestCase):
    def test_trailing_flips_cheapest_coin_flips_only_up_to_cap(self):
        # 5 coin flips (costs 0.10, 0.06, 0.02, -0.04, 0.00) + 1 strong lean.
        games = [game("A", "B", "home", 0.55), game("C", "D", "home", 0.53),
                 game("E", "F", "home", 0.51), game("G", "H", "home", 0.48),
                 game("I", "J", "home", 0.50), game("K", "L", "home", 0.70)]
        out = standings.suggest_gambles(games, 50, 52, 4)
        self.assertEqual(len(out["gambles"]), config.STANDINGS_MAX_GAMBLES_PER_WEEK)
        # Cheapest first: 0.48 (cost -0.04), 0.50 (0.00), 0.51 (0.02).
        self.assertEqual([g["pick_prob"] for g in out["gambles"]], [0.48, 0.50, 0.51])
        self.assertAlmostEqual(out["total_expected_cost"], -0.02, places=3)
        # Strong lean never gambled, and the over-cap coin flips stay DEFAULT.
        labels = [g["label"] for g in out["games"]]
        self.assertEqual(labels.count("GAMBLE"), 3)
        self.assertEqual(out["games"][5]["label"], "DEFAULT")
        # Labels never change the default pick.
        for g_in, g_out in zip(games, out["games"]):
            self.assertEqual(g_out["default_side"], g_in["default_side"])
        for g in out["gambles"]:
            self.assertEqual(g["gamble_side"], "away")
            self.assertAlmostEqual(g["gamble_prob"], 1.0 - g["pick_prob"], places=3)

    def test_leading_protects_strong_leans_and_suggests_nothing(self):
        games = [game("A", "B", "home", 0.70), game("C", "D", "home", 0.51)]
        out = standings.suggest_gambles(games, 52, 50, 3)
        self.assertEqual(out["mode"], "leading")
        self.assertEqual(out["gambles"], [])
        self.assertEqual(out["games"][0]["label"], "PROTECT")
        self.assertEqual(out["games"][1]["label"], "DEFAULT")

    def test_tied_late_suggests_nothing(self):
        out = standings.suggest_gambles(
            [game("A", "B", "home", 0.51)], 50, 50, 2)
        self.assertEqual(out["mode"], "tied_late")
        self.assertEqual(out["gambles"], [])


class TestSimulation(unittest.TestCase):
    def test_seeded_reproducibility(self):
        games = [sim_game(0.6, "home", "away") for _ in range(16)]
        a = simulation.simulate_season(games, 100, 102, trials=2000, seed=7)
        b = simulation.simulate_season(games, 100, 102, trials=2000, seed=7)
        self.assertEqual(a, b)

    def test_expected_wins_match_calibrated_probs(self):
        # 20 games, p_home=0.6, user takes home: expected wins per game
        # = 0.6 * (1 - 0.023) = 0.5862, over 20 games = 11.72.
        games = [sim_game(0.6, "home") for _ in range(20)]
        out = simulation.simulate_season(games, 0, 0, trials=20000, seed=1)
        self.assertAlmostEqual(out["expected_user_wins"], 20 * 0.6 * 0.977, delta=0.15)

    def test_mirroring_cannot_change_the_gap(self):
        # User ahead by 1, leader mirrors every pick: gap is preserved
        # on every trial, so P(first) is exactly 1. Behind by 1: exactly 0.
        games = [sim_game(0.6, "home", "home") for _ in range(16)]
        ahead = simulation.simulate_season(games, 101, 100, trials=2000, seed=3)
        self.assertEqual(ahead["p_first"], 1.0)
        behind = simulation.simulate_season(games, 100, 101, trials=2000, seed=3)
        self.assertEqual(behind["p_first"], 0.0)
        self.assertEqual(behind["p_first_or_tied"], 0.0)

    def test_contrarian_flip_probability_when_trailing_by_one(self):
        # One game left, trailing by 1, user flips to away (leader home).
        # Away covers with prob (1 - 0.023) * (1 - 0.6) = 0.3908 -- and
        # that only TIES (101-101), it cannot win outright, because one
        # flipped game moves the gap by at most 1: from -1 to 0 on a
        # flip win, to -2 on a flip loss. So strict P(first) stays 0
        # for both slates, while P(first-or-tied) jumps from 0 to
        # 0.3908. That strict-vs-tied distinction is exactly why
        # simulation.py reports both.
        base = [sim_game(0.6, "home", "home")]
        gamb = [sim_game(0.6, "away", "home")]
        out = simulation.compare_gambles(base, gamb, 100, 101, trials=20000, seed=5)
        self.assertEqual(out["baseline"]["p_first"], 0.0)
        self.assertEqual(out["gambled"]["p_first"], 0.0)
        self.assertAlmostEqual(out["gambled"]["p_first_or_tied"], 0.977 * 0.4, delta=0.02)
        self.assertEqual(out["delta_p_first_pp"], 0.0)
        # Cost of this single flip: home win prob 0.5862 vs away
        # 0.3908, cost 0.1954 -- above the CHEAP cap (0.10), below the
        # EXPENSIVE floor (0.25): NEUTRAL on wins, effective on ties.
        self.assertEqual(out["label"], "NEUTRAL")
        self.assertAlmostEqual(out["expected_cost_wins"], 0.195, delta=0.02)

    def test_classify_thresholds(self):
        self.assertEqual(simulation.classify_gamble(5.0, 0.05), "CHEAP")
        self.assertEqual(simulation.classify_gamble(5.0, 0.30), "EXPENSIVE")
        self.assertEqual(simulation.classify_gamble(-2.0, 0.05), "EXPENSIVE")
        self.assertEqual(simulation.classify_gamble(0.5, 0.15), "NEUTRAL")
        self.assertEqual(simulation.classify_gamble(0.0, -0.04), "CHEAP")

    def test_invalid_sim_inputs_rejected(self):
        with self.assertRaises(ValueError):
            simulation.simulate_season([sim_game(1.5, "home")], 0, 0, trials=10)
        with self.assertRaises(ValueError):
            simulation.simulate_season([sim_game(0.5, "KC")], 0, 0, trials=10)
        with self.assertRaises(ValueError):
            simulation.compare_gambles([sim_game(0.5, "home")],
                                       [sim_game(0.5, "home"), sim_game(0.5, "home")],
                                       0, 0, trials=10)

    def test_pushes_count_for_neither_slate(self):
        # p_home = 0.5, 1 game, both pick home: user wins iff home covers
        # and not a push. With push_prob=1.0 in effect... use push_prob=0.5
        # override: expected wins = 0.5 * 0.5 = 0.25.
        out = simulation.simulate_season(
            [sim_game(0.5, "home", "home")], 0, 0, trials=20000, seed=2, push_prob=0.5)
        self.assertAlmostEqual(out["expected_user_wins"], 0.25, delta=0.02)


class TestEvaluateLateWindow(unittest.TestCase):
    def _rows(self, season, weeks_outcomes):
        # weeks_outcomes: {week: [ats, ...]}; pick_prob 0.51 (coin flip)
        # for every game, so trailing mode flips up to the cap each week.
        rows = []
        for week, outcomes in weeks_outcomes.items():
            for ats in outcomes:
                rows.append({"season": season, "week": week,
                             "home_team": "A", "away_team": "B",
                             "pick_prob": 0.51, "ats": ats})
        return rows

    def test_trailing_flips_and_leading_does_not(self):
        # One late week only (week 18, weeks_left == 1): trailing by 2
        # flips the 3 cheapest coin flips (cap) -- flipping two wrongs
        # to wins and one correct to a loss nets +1 vs default that week.
        rows = self._rows(2024, {18: ["correct", "wrong", "wrong", "correct"]})
        out = standings.evaluate_late_window(rows, deficit=2)
        s = out["by_season"]["2024"]
        self.assertEqual(s["gambles"], 3)
        self.assertEqual(s["gambled_wins"] - s["default_wins"], 1)
        self.assertAlmostEqual(s["expected_cost"], 3 * 0.02, places=3)
        out_lead = standings.evaluate_late_window(rows, deficit=-2)
        self.assertEqual(out_lead["by_season"]["2024"]["gambles"], 0)
        self.assertEqual(out_lead["by_season"]["2024"]["gambled_wins"],
                         out_lead["by_season"]["2024"]["default_wins"])


if __name__ == "__main__":
    unittest.main()
