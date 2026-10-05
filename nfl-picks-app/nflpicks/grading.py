"""
Run this after games finish (e.g. Tuesday morning) to grade last week's
predictions against actual results. This is what tells you, with real
evidence, whether the EPA shadow layer is ready to go live -- don't flip
config.EPA_LIVE without checking this first.
"""
from __future__ import annotations

import statistics

from . import config, data, db


def grade_and_summarize(db_path: str | None = None):
    games = data.load_games(config.SEASON)
    results = {}
    for _, r in games[games["result"].notna()].iterrows():
        key = (int(r["season"]), int(r["week"]), r["away_team"], r["home_team"])
        results[key] = float(r["result"])

    with db.connect(db_path) as conn:
        rows = conn.execute("SELECT rowid, * FROM model_log").fetchall()
        stats = {"vegas": [], "margin": {"err": [], "ats": 0, "n": 0},
                 "epa": {"err": [], "ats": 0, "n": 0}, "combo": {"err": [], "ats": 0, "n": 0}}

        for row in rows:
            key = (row["season"], row["week"], row["away_team"], row["home_team"])
            actual = results.get(key)
            if actual is None:
                continue
            vegas = row["vegas_close"]
            stats["vegas"].append(abs(vegas - actual))

            updates = {"actual_margin": actual}
            for name, col, err_col in [("margin", "margin_model", "abs_err_margin"),
                                        ("epa", "epa_model", "abs_err_epa"),
                                        ("combo", "combo_model", "abs_err_combo")]:
                pred = row[col]
                if pred is None:
                    continue
                err = abs(pred - actual)
                updates[err_col] = err
                stats[name]["err"].append(err)
                if pred != vegas and actual != vegas:
                    stats[name]["n"] += 1
                    if (pred > vegas) == (actual > vegas):
                        stats[name]["ats"] += 1

            set_clause = ", ".join(f"{k}=?" for k in updates)
            conn.execute(f"UPDATE model_log SET {set_clause} WHERE rowid=?",
                         (*updates.values(), row["rowid"]))

        def mae(errs):
            return round(statistics.mean(errs), 2) if errs else None

        summary = [
            ("A", "Closing spread only (BASELINE -- beat this or cut the feature)",
             len(stats["vegas"]), mae(stats["vegas"]), "n/a", "n/a"),
        ]
        desc = {"margin": "Spread + SOS-adjusted margin model (current live model)",
                "epa": "Spread + weighted-EPA efficiency (shadow)",
                "combo": "Margin + EPA combined (shadow)"}
        for name in ("margin", "epa", "combo"):
            s = stats[name]
            ats_pct = f"{round(s['ats'] / s['n'] * 100, 1)}%" if s["n"] else "n/a"
            summary.append((name.upper(), desc[name], len(s["err"]), mae(s["err"]),
                             f"{s['ats']}-{s['n'] - s['ats']}", ats_pct))

        conn.execute("DELETE FROM model_grades")
        for variant, description, n, mae_val, ats_record, ats_pct in summary:
            conn.execute(
                "INSERT INTO model_grades (variant, description, games_graded, mae, ats_record, ats_pct) "
                "VALUES (?,?,?,?,?,?)",
                (variant, description, n, mae_val, ats_record, ats_pct),
            )

    return summary


if __name__ == "__main__":
    for row in grade_and_summarize():
        print(row)
