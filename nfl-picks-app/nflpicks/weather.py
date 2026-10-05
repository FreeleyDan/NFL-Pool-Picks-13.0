"""
Weather effects on the TOTALS model only. Grounded in published research
(see config.py comments for sources) -- wind past 15-20mph and extreme
temperatures both suppress scoring; wind is the more reliable signal.
Uses Open-Meteo's free forecast API (no key required, ~16-day horizon,
plenty for a week-ahead pick).

Deliberately NOT applied to the spread model: weather's effect on which
team wins is far less established in the research than its effect on
total points, and misapplying it there risks adding noise, not signal.
"""
from __future__ import annotations

import logging

import requests

from . import config

log = logging.getLogger(__name__)

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"


def is_indoor(roof: str | None) -> bool:
    if not roof:
        return False
    return str(roof).strip().lower() in config.INDOOR_ROOF_VALUES


def fetch_forecast(home_team: str, game_date: str | None) -> dict | None:
    """
    Returns {"wind_mph": float, "temp_f": float, "precip_mm": float} for
    the home team's stadium on game day, or None if unavailable (dome,
    fetch failure, date outside forecast horizon, or unknown stadium).
    """
    coords = config.STADIUM_COORDS.get(home_team)
    if coords is None or not game_date:
        return None
    lat, lon = coords
    try:
        resp = requests.get(FORECAST_URL, params={
            "latitude": lat, "longitude": lon,
            "daily": "temperature_2m_max,temperature_2m_min,windspeed_10m_max,precipitation_sum",
            "temperature_unit": "fahrenheit", "windspeed_unit": "mph",
            "start_date": game_date, "end_date": game_date,
            "timezone": "auto",
        }, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        daily = data.get("daily", {})
        if not daily.get("time"):
            return None
        temp_max = daily["temperature_2m_max"][0]
        temp_min = daily["temperature_2m_min"][0]
        return {
            "wind_mph": daily["windspeed_10m_max"][0],
            "temp_f": (temp_max + temp_min) / 2,
            "precip_mm": daily.get("precipitation_sum", [0])[0],
        }
    except Exception as e:
        log.warning("weather fetch failed for %s on %s: %s", home_team, game_date, e)
        return None


def total_adjustment(weather: dict | None) -> tuple[float, float, str]:
    """
    Returns (flat_points_to_subtract, pct_of_total_to_subtract, reason).
    Both reductions apply on top of the model's total: adjusted_total =
    raw_total * (1 - pct) - flat.
    """
    if weather is None:
        return 0.0, 0.0, ""

    reasons = []
    pct_reduction = 0.0
    flat_reduction = 0.0

    wind = weather["wind_mph"]
    if wind >= config.WIND_HIGH_MPH:
        flat_reduction += abs(config.WIND_HIGH_TOTAL_ADJ)
        reasons.append(f"wind {wind:.0f}mph (high)")
    elif wind >= config.WIND_MODERATE_MPH:
        flat_reduction += abs(config.WIND_MODERATE_TOTAL_ADJ)
        reasons.append(f"wind {wind:.0f}mph (moderate)")

    temp = weather["temp_f"]
    if temp <= config.TEMP_COLD_F or temp >= config.TEMP_HOT_F:
        pct_reduction += config.EXTREME_TEMP_TOTAL_PCT
        reasons.append(f"extreme temp {temp:.0f}F")
    elif temp <= config.TEMP_MILD_COLD_F:
        pct_reduction += config.MILD_COLD_TOTAL_PCT
        reasons.append(f"cold {temp:.0f}F")

    reason = ", ".join(reasons) if reasons else "no significant weather effect"
    return flat_reduction, pct_reduction, reason
