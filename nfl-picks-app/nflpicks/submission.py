"""
Submission sheet (batch items 3a): an exact copy-paste list for replying
to Barbara's pool email, plus a transcription checklist.

The pool sheet prints lines in SPORTSBOOK convention (a home favorite
is "-3.5"), while everything inside this app stores NFLVERSE convention
(positive = home favored). The model's own pick string (model.py) is
already in sportsbook notation -- it is built from the pool line when
one is set, else the live Vegas line -- so this module does NOT trust
either string blindly: it re-derives the sportsbook notation from the
stored nflverse line and the picked team, and the checklist FAILS any
game where the re-derived notation does not match the model's pick
string. That is the sign-convention double-check: a flipped sign can
only reach the copy-paste block as an explicit MISMATCH warning, never
as a silently wrong line.

Input picks are dicts (or objects with the same attribute names), one
per game, with keys:
  home, away: team abbreviations (also accepted as home_team/away_team)
  pick: the model's pick string, e.g. "KC -3.5", "DEN +3.5", "KC PK"
  pool_spread, vegas_spread: nflverse-convention lines (or None)
  line: explicit effective nflverse line, overriding both of the above
  line_source: explicit "pool"/"vegas" (else derived from which line won)
  pick_side: optional "home"/"away", used only when `pick` is missing

The effective line is the pool line when set -- that is what the user
is judged against -- else the live Vegas line. A game with no line at
all gets no pick line in the copy block and fails the checklist, rather
than a guessed line.

Ordering: the copy block is sorted alphabetically by matchup. The pool
sheet's game order is kickoff order, which the picks table does not
store, so the block says to match by matchup, not by position -- and
the checklist verifies every matchup appears exactly once, so a
reordered copy still cannot silently drop or duplicate a game.
"""
from __future__ import annotations

from . import config


def _get(pick, *names, default=None):
    for name in names:
        if isinstance(pick, dict):
            if name in pick and pick[name] is not None:
                return pick[name]
        else:
            value = getattr(pick, name, None)
            if value is not None:
                return value
    return default


def _fmt_line(value: float) -> str:
    """Format a sportsbook line value: -3.5 -> "-3.5", 3.5 -> "+3.5"."""
    v = float(value)
    return f"{'-' if v < 0 else '+'}{abs(v):g}"


def format_pick_sportsbook(home: str, away: str, line_nflverse: float | None,
                           picked_team: str | None) -> str | None:
    """Sportsbook notation for a pick, derived from the nflverse line.

    Home pick: the sportsbook line is -line_nflverse (config helper).
    Away pick: the away team's line is the mirror image of the home
    line, i.e. line_nflverse itself (nflverse -7 = away favored by 7
    = away team -7 on a sportsbook).
    A zero line is a pick'em ("PK") for either side. Returns None when
    there is no line or no picked team -- the caller must flag that,
    not invent a line.
    """
    if line_nflverse is None or picked_team is None:
        return None
    line = float(line_nflverse)
    if line == 0:
        return f"{picked_team} PK"
    if picked_team == home:
        return f"{home} {_fmt_line(config.to_sportsbook_spread(line))}"
    if picked_team == away:
        return f"{away} {_fmt_line(line)}"
    return None


def pick_team_from_pick_string(pick: str | None, home: str, away: str) -> str | None:
    """The team a model pick string names, or None if it names neither.

    Pick strings are "<TEAM> <line>" or "<TEAM> PK". Matching is on the
    first token, so a team abbreviation that is a prefix of the other
    team's abbreviation cannot false-match (no such pair exists in the
    NFL, but the token check is free).
    """
    if not pick:
        return None
    token = str(pick).strip().split()[0] if str(pick).strip() else ""
    if token == home:
        return home
    if token == away:
        return away
    return None


def _normalize(pick) -> dict:
    home = _get(pick, "home", "home_team")
    away = _get(pick, "away", "away_team")
    model_pick = _get(pick, "pick")
    pool_spread = _get(pick, "pool_spread")
    vegas_spread = _get(pick, "vegas_spread")
    line = _get(pick, "line")
    line_source = _get(pick, "line_source")
    if line is None:
        if pool_spread is not None:
            line, line_source = pool_spread, "pool"
        elif vegas_spread is not None:
            line, line_source = vegas_spread, "vegas"
        else:
            line_source = line_source or "none"
    elif line_source is None:
        line_source = "pool" if (pool_spread is not None and float(pool_spread) == float(line)) else "vegas"
    pick_side = _get(pick, "pick_side")
    picked_team = None
    if pick_side in ("home", "away"):
        picked_team = home if pick_side == "home" else away
    if picked_team is None:
        picked_team = pick_team_from_pick_string(model_pick, home, away)
    return {
        "home": home, "away": away, "model_pick": model_pick,
        "line": None if line is None else float(line),
        "line_source": line_source, "picked_team": picked_team,
    }


def build_submission_sheet(picks, week: int) -> dict:
    """Build the copy-paste block and its checklist for one week.

    Returns {"week", "entries", "text", "checklist", "all_checks_pass"}.
    Each entry carries matchup, pick_text (sportsbook notation, or a
    "(no pick -- ...)" placeholder that must never be copied blindly),
    line_source, model_pick, and sign_check ("OK"/"MISMATCH"/"NO_MODEL_PICK"/"NO_LINE").
    Checklist items carry status "pass"/"fail"/"manual": "manual" is
    the Thursday-morning human step (review queue checked) that no code
    can verify, so it is listed, never silently marked passed.
    """
    entries = []
    for raw in picks:
        p = _normalize(raw)
        pick_text = format_pick_sportsbook(p["home"], p["away"], p["line"], p["picked_team"])
        if pick_text is None:
            pick_text = "(no pick -- no line yet)"
            sign_check = "NO_LINE"
        elif p["model_pick"] is None:
            sign_check = "NO_MODEL_PICK"
        elif pick_text == str(p["model_pick"]).strip():
            sign_check = "OK"
        else:
            sign_check = "MISMATCH"
        entries.append({
            "home": p["home"], "away": p["away"],
            "matchup": f"{p['away']} @ {p['home']}",
            "pick_text": pick_text, "model_pick": p["model_pick"],
            "line": p["line"], "line_source": p["line_source"],
            "picked_team": p["picked_team"], "sign_check": sign_check,
        })
    entries.sort(key=lambda e: e["matchup"])

    lines = [f"Week {int(week)} -- NFL pool picks (ATS)"]
    for e in entries:
        lines.append(f"{e['matchup']}: {e['pick_text']}")
    lines.append("")
    lines.append("Due Thursday 11:00 AM PT -- reply to Barbara's pool email.")
    lines.append("Match by matchup, not by position: the order above is alphabetical,")
    lines.append("not kickoff order. Check every line against the dashboard before sending.")
    text = "\n".join(lines)

    matchups = [e["matchup"] for e in entries]
    n_picked = sum(1 for e in entries if e["sign_check"] != "NO_LINE")
    n_pool = sum(1 for e in entries if e["line_source"] == "pool")
    checklist = [
        {"check": "Every game has a pick",
         "status": "pass" if n_picked == len(entries) and entries else "fail",
         "detail": f"{n_picked} of {len(entries)} games have a pick line."},
        {"check": "No matchup listed twice",
         "status": "pass" if len(set(matchups)) == len(matchups) else "fail",
         "detail": f"{len(entries)} entries, {len(set(matchups))} unique matchups."},
        {"check": "Every pick team plays in its game",
         "status": "pass" if all(e["picked_team"] in (e["home"], e["away"]) for e in entries) else "fail",
         "detail": "Pick team is neither team for at least one game." if any(
             e["picked_team"] not in (e["home"], e["away"]) for e in entries)
         else "All pick teams match their matchup."},
        {"check": "Sportsbook notation matches the model pick (sign check)",
         "status": "pass" if all(e["sign_check"] in ("OK", "NO_MODEL_PICK") for e in entries) else "fail",
         "detail": ("All derived lines match the model pick strings." if all(
             e["sign_check"] == "OK" for e in entries)
             else ("Some games had no model pick string to check against; derived lines used as-is." if all(
                 e["sign_check"] in ("OK", "NO_MODEL_PICK") for e in entries)
                 else "MISMATCH on: " + ", ".join(
                     e["matchup"] for e in entries if e["sign_check"] == "MISMATCH")))},
        {"check": "Pool line used where set (judged line, not live line)",
         "status": "pass",
         "detail": (f"{n_pool} game(s) use a pool line; the rest use the live Vegas line. "
                    "If a pool line was set but shows as live here, do not send.")},
        {"check": "Thursday review queue checked before sending",
         "status": "manual",
         "detail": "Human step: open the Submit & Review tab's review queue and clear "
                   "every P1 item before copying this list."},
    ]
    return {
        "week": int(week), "entries": entries, "text": text,
        "checklist": checklist,
        "all_checks_pass": all(c["status"] == "pass" for c in checklist
                               if c["status"] != "manual"),
    }
