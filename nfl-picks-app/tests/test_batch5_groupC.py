"""Tests for batch 5, group C items 6-9 (tests-on-every-push
workflow; committed-DB sync setup; per-game explanations; flip
sensitivity). No network, no games.csv needed: the workflow file
is parsed from the repo, and explain/sensitivity run on plain
dicts. No live behaviour changes: no flag is flipped, no pick
is changed, no token value exists anywhere in this file.

Run from nfl-picks-app/: python -m unittest discover -s tests -v
"""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nflpicks import committed_db, config, explain, sensitivity

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# Flags that must stay off by default (every batch's constraint,
# restated here so a future batch cannot silently flip one while
# adding display code -- items 8/9 need no flag at all).
OFF_BY_DEFAULT = (
    "EPA_LIVE", "ENSEMBLE_LIVE", "CALIBRATED_PICK", "TRAVEL_ENABLED",
    "INJ_AUTO_APPLY", "TOTALS_SRS_LIVE", "STALENESS_LIVE",
    "UNCERTAINTY_PICK_USE", "PBP_FEATURES_LIVE", "TEAM_HFA_LIVE",
    "DYNAMIC_BLEND_LIVE", "SITUATIONAL_LIVE", "REST_TRAVEL_EXACT_LIVE",
    "WEATHER_KICKOFF_LIVE", "UNIT_DEPLETION_LIVE", "REF_COACH_LIVE",
)


def _game(**over):
    row = {"home_team": "KC", "away_team": "DEN", "pick": "KC -3.5",
           "model_independent": -3.5 + 6.0, "pool_spread": None,
           "vegas_spread": -3.5, "edge": 0.9, "cover_prob": 0.62,
           "confidence": 1.0, "strong_edge": True, "needs_review": False,
           "injury_flags": "", "weather_note": "", "model_total": None,
           "vegas_total": None, "pool_total": None, "total_pick": "no line",
           "cover_prob_range": None, "line_move": None}
    row.update(over)
    return row


class TestFlagsStillOff(unittest.TestCase):
    def test_all_off_by_default_flags_are_false(self):
        for flag in OFF_BY_DEFAULT:
            self.assertIs(getattr(config, flag), False, flag)

    def test_sensitivity_thresholds(self):
        # 0.5 = fragile (one half-point move flips it),
        # 3.0 = real lean (see sensitivity.py docstring).
        self.assertEqual(config.SENSITIVITY_FRAGILE_PTS, 0.5)
        self.assertEqual(config.SENSITIVITY_SOLID_PTS, 3.0)


class TestExplainPick(unittest.TestCase):
    def test_strong_edge_game(self):
        out = explain.explain_pick(_game())
        self.assertIn("KC", out["why"])
        self.assertIn("6.0 pts better than the Vegas line", out["why"])
        self.assertIn("strong model-vs-market gap", out["why"])
        self.assertIn("cover prob 0.62", out["why"])
        # One line by construction: no newlines, factors joined.
        self.assertNotIn("\n", out["why"])
        self.assertEqual(out["why"], "; ".join(out["factors"]))

    def test_pool_line_is_the_judged_line(self):
        out = explain.explain_pick(_game(pool_spread=-2.0, model_independent=2.0))
        self.assertIn("pool line", out["why"])
        self.assertIn("4.0 pts better", out["why"])

    def test_needs_review_names_the_disagreement(self):
        out = explain.explain_pick(_game(model_independent=5.0, vegas_spread=-3.5,
                                         needs_review=True, strong_edge=False))
        self.assertIn("market disagrees by 8.5 pts", out["why"])
        self.assertIn("verify the inputs", out["why"])

    def test_no_line_says_so_plainly(self):
        out = explain.explain_pick(_game(vegas_spread=None, pool_spread=None,
                                         pick=None, model_independent=1.0))
        self.assertIn("No line yet", out["why"])
        self.assertEqual(out["factors"], ["no line"])

    def test_no_model_says_so_plainly(self):
        out = explain.explain_pick(_game(model_independent=None))
        self.assertIn("No model read yet", out["why"])

    def test_coin_flip_cover_prob(self):
        out = explain.explain_pick(_game(cover_prob=0.51, model_independent=-3.0,
                                         strong_edge=False))
        self.assertIn("near a coin flip", out["why"])

    def test_uncertainty_range_straddling_half(self):
        out = explain.explain_pick(_game(cover_prob_range=(0.45, 0.68),
                                         strong_edge=False))
        self.assertIn("rating uncertainty alone could flip", out["why"])
        self.assertIn("0.45-0.68", out["why"])

    def test_uncertainty_range_not_straddling(self):
        out = explain.explain_pick(_game(cover_prob_range=(0.58, 0.71),
                                         strong_edge=False))
        self.assertNotIn("rating uncertainty alone could flip", out["why"])

    def test_injury_flag_is_review_only(self):
        out = explain.explain_pick(_game(
            injury_flags="KC: 2 listed (Mahomes, Kelce) | DEN: 1 listed (Sutton)",
            strong_edge=False))
        self.assertIn("injury report lists players for DEN, KC", out["why"])
        self.assertIn("review-only, not in the number", out["why"])

    def test_line_move_factor(self):
        out = explain.explain_pick(_game(line_move=1.5, strong_edge=False))
        self.assertIn("live line has moved 1.5 pts toward home", out["why"])

    def test_small_line_move_is_noise(self):
        out = explain.explain_pick(_game(line_move=0.5, strong_edge=False))
        self.assertNotIn("live line has moved", out["why"])

    def test_total_factor(self):
        out = explain.explain_pick(_game(model_total=48.0, vegas_total=44.5,
                                         total_pick="OVER 44.5", strong_edge=False))
        self.assertIn("total: model 48.0 vs line 44.5 (OVER 44.5)", out["why"])

    def test_weather_factor(self):
        out = explain.explain_pick(_game(weather_note="wind 20mph (high)",
                                         strong_edge=False))
        self.assertIn("weather: wind 20mph (high) (totals only)", out["why"])

    def test_low_confidence_noted(self):
        out = explain.explain_pick(_game(confidence=0.45, strong_edge=False))
        self.assertIn("early-season sample (confidence 0.45", out["why"])

    def test_stored_pick_disagreement_is_flagged(self):
        # Numbers favour KC (model above the line) but the stored
        # pick names DEN: a data problem, said out loud.
        out = explain.explain_pick(_game(pick="DEN +3.5", strong_edge=False))
        self.assertIn("stored pick (DEN +3.5) names the other side", out["why"])

    def test_nan_inputs_are_skipped_not_invented(self):
        import math
        out = explain.explain_pick(_game(cover_prob=math.nan, confidence=math.nan,
                                         injury_flags=float("nan"), strong_edge=False))
        self.assertNotIn("cover prob", out["why"])
        self.assertNotIn("confidence", out["why"])
        self.assertNotIn("injury report", out["why"])

    def test_explain_picks_batch_never_raises(self):
        rows = [_game(), _game(vegas_spread=None, pick=None), {"home_team": "KC"}]
        outs = explain.explain_picks(rows)
        self.assertEqual(len(outs), 3)
        self.assertIn("No line yet", outs[1]["why"])


class TestSensitivity(unittest.TestCase):
    def test_flip_line_is_the_model_read(self):
        out = sensitivity.flip_analysis(2.5, -3.5)
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["flip_line"], 2.5)
        self.assertEqual(out["line_move_to_flip"], 6.0)
        self.assertEqual(out["rating_move_to_flip"], -6.0)
        self.assertEqual(out["fragility"], "real lean")

    def test_fragile_half_point_move(self):
        out = sensitivity.flip_analysis(-3.0, -3.5)
        self.assertEqual(out["line_move_to_flip"], 0.5)
        self.assertEqual(out["fragility"], "fragile")
        self.assertIn("0.5 pts", out["summary"])
        self.assertIn("fragile", out["summary"])

    def test_thin_lean_between_thresholds(self):
        out = sensitivity.flip_analysis(-2.0, -3.5)
        self.assertEqual(out["line_move_to_flip"], 1.5)
        self.assertEqual(out["fragility"], "thin lean")

    def test_real_lean_at_three_points(self):
        out = sensitivity.flip_analysis(-0.5, -3.5)
        self.assertEqual(out["line_move_to_flip"], 3.0)
        self.assertEqual(out["fragility"], "real lean")

    def test_direction_home_pick(self):
        # Model above the line: pick is home, so the line must rise.
        out = sensitivity.flip_analysis(2.5, -3.5)
        self.assertIn("up (home more favoured)", out["line_direction"])
        self.assertEqual(out["rating_direction"], "down")

    def test_direction_away_pick(self):
        # Model below the line: pick is away, so the line must fall.
        out = sensitivity.flip_analysis(-5.0, -3.5)
        self.assertEqual(out["line_move_to_flip"], -1.5)
        self.assertIn("down (away more favoured)", out["line_direction"])
        self.assertEqual(out["rating_direction"], "up")

    def test_exact_tie_any_move_flips(self):
        out = sensitivity.flip_analysis(-3.5, -3.5)
        self.assertEqual(out["line_move_to_flip"], 0.0)
        self.assertEqual(out["fragility"], "fragile")
        self.assertIn("Already on the line", out["summary"])

    def test_no_line_status(self):
        out = sensitivity.flip_analysis(2.5, None)
        self.assertEqual(out["status"], "no_line")
        out = sensitivity.flip_analysis(None, -3.5)
        self.assertEqual(out["status"], "no_model")

    def test_line_and_rating_moves_are_equal_magnitude(self):
        for model, line in [(2.5, -3.5), (-5.0, -3.5), (-3.0, -3.5), (0.0, 2.5)]:
            out = sensitivity.flip_analysis(model, line)
            self.assertEqual(abs(out["line_move_to_flip"]),
                             abs(out["rating_move_to_flip"]))

    def test_total_flip_over(self):
        out = sensitivity.total_flip_analysis(48.0, 44.5)
        self.assertEqual(out["flip_line"], 48.0)
        self.assertEqual(out["fragility"], "real lean")
        self.assertIn("line rises 3.5 pts", out["summary"])

    def test_total_flip_missing_inputs_is_none(self):
        self.assertIsNone(sensitivity.total_flip_analysis(None, 44.5))
        self.assertIsNone(sensitivity.total_flip_analysis(48.0, None))

    def test_sensitivity_for_game_uses_pool_line(self):
        out = sensitivity.sensitivity_for_game(
            _game(model_independent=2.0, pool_spread=-2.0, vegas_spread=-3.5))
        self.assertEqual(out["spread"]["flip_line"], 2.0)
        self.assertEqual(out["spread"]["line_move_to_flip"], 4.0)
        self.assertIsNone(out["total"])  # no model_total in the row

    def test_sensitivities_for_games_batch_never_raises(self):
        rows = [_game(), _game(vegas_spread=None, pool_spread=None)]
        outs = sensitivity.sensitivities_for_games(rows)
        self.assertEqual(len(outs), 2)
        self.assertEqual(outs[1]["spread"]["status"], "no_line")


class TestCommittedDbSetup(unittest.TestCase):
    def test_setup_names_exact_vars_and_no_token_value(self):
        info = committed_db.setup_instructions()
        names = {v["name"] for v in info["env_vars"]}
        self.assertIn("PICKS_GITHUB_REPO", names)
        self.assertIn("PICKS_GITHUB_BRANCH", names)
        self.assertIn("PICKS_COMMITTED_DB_URL", names)
        self.assertIn("PICKS_COMMITTED_DB_FILE", names)
        self.assertEqual(info["token"]["name"], "PICKS_GITHUB_TOKEN")
        blob = str(info)
        # No token value exists to leak: the instructions name the
        # variable and say where to create one, and nothing here
        # looks like a pasted credential.
        self.assertNotIn("ghp_", blob)
        self.assertNotIn("github_pat_", blob)
        self.assertIn("Streamlit Cloud", " ".join(info["streamlit_cloud"]))
        self.assertIn("Secrets", " ".join(info["streamlit_cloud"]))
        self.assertTrue(info["verify"])  # verification steps exist

    def test_local_source_note_points_at_setup(self):
        src = committed_db.describe_source()
        # With no env set in the test process this is local-only;
        # the note must name the source vars, not just say "local".
        if src["mode"] == "local":
            self.assertIn("PICKS_GITHUB_REPO", src["note"])

    def test_apply_streamlit_secrets_sets_only_known_keys(self):
        for key in ("PICKS_GITHUB_REPO", "PICKS_GITHUB_TOKEN",
                    "PICKS_GITHUB_BRANCH", "PICKS_COMMITTED_DB_URL",
                    "PICKS_COMMITTED_DB_FILE"):
            os.environ.pop(key, None)
        applied = committed_db.apply_streamlit_secrets({
            "PICKS_GITHUB_REPO": "owner/repo",
            "PICKS_GITHUB_TOKEN": "token-value-that-stays-in-env-only",
            "SOME_OTHER_SECRET": "must be ignored",
            "PICKS_GITHUB_BRANCH": "main",
        })
        try:
            self.assertEqual(sorted(applied),
                             ["PICKS_GITHUB_BRANCH", "PICKS_GITHUB_REPO",
                              "PICKS_GITHUB_TOKEN"])
            self.assertEqual(os.environ["PICKS_GITHUB_REPO"], "owner/repo")
            self.assertNotIn("SOME_OTHER_SECRET", os.environ)
        finally:
            for key in ("PICKS_GITHUB_REPO", "PICKS_GITHUB_TOKEN",
                        "PICKS_GITHUB_BRANCH"):
                os.environ.pop(key, None)

    def test_apply_streamlit_secrets_never_overwrites_env(self):
        os.environ["PICKS_GITHUB_REPO"] = "env/wins"
        try:
            applied = committed_db.apply_streamlit_secrets(
                {"PICKS_GITHUB_REPO": "secret/loses"})
            self.assertEqual(applied, [])
            self.assertEqual(os.environ["PICKS_GITHUB_REPO"], "env/wins")
        finally:
            os.environ.pop("PICKS_GITHUB_REPO", None)

    def test_apply_streamlit_secrets_tolerates_empty_input(self):
        self.assertEqual(committed_db.apply_streamlit_secrets(None), [])
        self.assertEqual(committed_db.apply_streamlit_secrets({}), [])
        self.assertEqual(committed_db.apply_streamlit_secrets("not-a-mapping"), [])


class TestTestsWorkflow(unittest.TestCase):
    # NOTE: these tests deliberately avoid PyYAML. The CI image installs
    # only requirements.txt, which does not (and should not) include yaml
    # -- a test-only dependency broke every workflow run on 2026-11-05
    # (ModuleNotFoundError: No module named 'yaml'), and because the
    # weekly workflow runs tests fail-fast, no Week 9 picks were ever
    # generated. Plain-text checks are enough for this file.
    def test_workflow_file_exists_and_parses(self):
        path = REPO_ROOT / ".github" / "workflows" / "tests.yml"
        self.assertTrue(path.exists(), "tests.yml must exist at the repo root")
        text = path.read_text()
        self.assertIn("name: Tests", text)
        self.assertIn("push", text)
        self.assertIn("pull_request", text)

    def test_workflow_runs_compile_and_unittest(self):
        path = REPO_ROOT / ".github" / "workflows" / "tests.yml"
        text = path.read_text()
        self.assertIn("py_compile", text)
        self.assertIn("nfl-picks-app/nflpicks", text)
        self.assertIn("unittest discover", text)
        self.assertIn("nfl-picks-app", text)
        self.assertIn("actions/setup-python", text)
        self.assertIn('"3.12"', text)


if __name__ == "__main__":
    unittest.main()
