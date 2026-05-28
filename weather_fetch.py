#!/usr/bin/env python3
"""
weather_fetch.py — On Par Entertainment
Fetches daily weather for Dayton, OH from Open-Meteo (free, no API key).
Stores in Supabase daily_weather table.
Provides a weather_multiplier for the forecast model.

Usage:
  python weather_fetch.py                    # yesterday + 16-day forecast preview
  python weather_fetch.py --backfill         # all history since venue open date
  python weather_fetch.py --forecast         # print upcoming 16-day weather table

Run daily_weather_schema.sql in Supabase first.
"""

import json
import os
import sys
import urllib.request
import urllib.parse
import urllib.error
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler

# ── Config ────────────────────────────────────────────────────────────────────

# Dayton, OH (Wright-Patterson AFB / downtown)
LATITUDE   = 39.7589
LONGITUDE  = -84.1916
TIMEZONE   = "America%2FAmerica_New_York"   # URL-encoded for query string

HISTORY_URL  = "https://archive-api.open-meteo.com/v1/archive"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

DAILY_VARS = "temperature_2m_max,temperature_2m_min,precipitation_sum,wind_speed_10m_max,weather_code"
UNITS      = "temperature_unit=fahrenheit&wind_speed_unit=mph&precipitation_unit=inch"

VENUE_OPEN_DATE = "2023-10-19"   # first day of sales history

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY", os.getenv("SUPABASE_KEY", ""))
CRON_SECRET  = os.getenv("CRON_SECRET", "")

# ── WMO weather code → human label + venue impact category ────────────────────
#
# Impact for an INDOOR entertainment venue (bowling, mini golf, bar):
#   "good"     → rain/snow keeps people indoors → revenue tends UP
#   "neutral"  → mild/cloudy, no strong effect
#   "negative" → extreme heat (outdoor competition) or dangerous weather

_WMO: dict = {
    0:  ("Clear sky",         "neutral"),
    1:  ("Mainly clear",      "neutral"),
    2:  ("Partly cloudy",     "neutral"),
    3:  ("Overcast",          "neutral"),
    45: ("Fog",               "neutral"),
    48: ("Freezing fog",      "slight_neg"),
    51: ("Light drizzle",     "good"),
    53: ("Moderate drizzle",  "good"),
    55: ("Dense drizzle",     "good"),
    56: ("Freezing drizzle",  "slight_neg"),
    57: ("Heavy freezing drizzle", "negative"),
    61: ("Slight rain",       "good"),
    63: ("Moderate rain",     "good"),
    65: ("Heavy rain",        "good"),
    66: ("Freezing rain",     "slight_neg"),
    67: ("Heavy freezing rain","negative"),
    71: ("Slight snow",       "good"),     # indoor alternative; driving OK
    73: ("Moderate snow",     "neutral"),  # mixed — some stay home
    75: ("Heavy snow",        "negative"), # people don't drive out
    77: ("Snow grains",       "neutral"),
    80: ("Slight rain showers","good"),
    81: ("Moderate rain showers","good"),
    82: ("Violent rain showers","slight_neg"),  # too intense → stay home
    85: ("Slight snow showers","good"),
    86: ("Heavy snow showers", "negative"),
    95: ("Thunderstorm",       "negative"),
    96: ("Thunderstorm+hail",  "negative"),
    99: ("Thunderstorm+hail",  "negative"),
}


def wmo_label(code: int) -> str:
    return _WMO.get(code, ("Unknown", "neutral"))[0]


def wmo_impact(code: int) -> str:
    return _WMO.get(code, ("Unknown", "neutral"))[1]


# ── Weather multiplier ────────────────────────────────────────────────────────
#
# Coefficients derived from domain knowledge for an indoor entertainment venue.
# Calibrate with calibrate_weather_coefficients() once historical data is loaded.
#
# Effect split by day type (weekends are more weather-sensitive because
# the outdoor-vs-indoor choice is stronger on leisure days):
#
#   Rain:
#     good codes → +8% weekdays, +12% weekends
#   Heavy/dangerous weather:
#     negative codes → -15% any day
#   Hot day (max_temp > 85°F):
#     → -5% weekday, -8% weekend (people go to outdoor venues / pools)
#   Cold snap (max_temp < 28°F):
#     → -8% any day (reluctance to leave home)
#   Perfect weather (65-78°F, clear):
#     → -4% weekend (competition from outdoor activities)

OPTIMAL_TEMP_LOW  = 65.0
OPTIMAL_TEMP_HIGH = 78.0
HOT_THRESHOLD     = 85.0
COLD_THRESHOLD    = 28.0


def weather_multiplier(max_temp_f: float, precipitation_in: float,
                       weather_code: int, is_weekend: bool) -> float:
    """
    Returns a multiplier to apply to the seasonal DOW baseline.
    1.0 = no effect. Typical range: 0.80–1.15.
    """
    mult = 1.0
    impact = wmo_impact(weather_code)

    # Precipitation / weather code effect
    if impact == "good":
        mult += 0.12 if is_weekend else 0.08
    elif impact == "negative":
        mult -= 0.15
    elif impact == "slight_neg":
        mult -= 0.05

    # Temperature effects (independent of precipitation)
    if max_temp_f is not None:
        if max_temp_f > HOT_THRESHOLD:
            penalty = min(0.15, (max_temp_f - HOT_THRESHOLD) * 0.008)
            mult -= (penalty * 1.5 if is_weekend else penalty)
        elif max_temp_f < COLD_THRESHOLD:
            mult -= 0.08
        elif OPTIMAL_TEMP_LOW <= max_temp_f <= OPTIMAL_TEMP_HIGH and impact == "neutral":
            # Beautiful weather → outdoor competition on weekends
            if is_weekend:
                mult -= 0.04

    return round(max(0.50, min(1.30, mult)), 4)


def calibrate_weather_coefficients(daily_sales: dict, weather: dict,
                                    dow_means: dict) -> dict:
    """
    Compute data-driven weather multipliers from historical residuals.

    daily_sales : { date_str: { cat: net_sales } }   (from forecast_agent)
    weather     : { date_str: daily_weather_row }
    dow_means   : { cat: { dow: mean_sales } }        (from build_forecasts)

    Returns { impact_category: { "weekend": float, "weekday": float } }
    """
    from collections import defaultdict
    from statistics import mean

    buckets: dict = defaultdict(lambda: {"weekend": [], "weekday": []})

    for d_str, day_cats in daily_sales.items():
        w = weather.get(d_str)
        if not w:
            continue
        d         = __import__("datetime").date.fromisoformat(d_str)
        is_wknd   = d.weekday() >= 4
        slot      = "weekend" if is_wknd else "weekday"
        impact    = wmo_impact(w.get("weather_code") or 0)

        # Compute actual vs. expected ratio
        total_actual   = sum(day_cats.values())
        total_baseline = sum(cm.get(d.weekday(), 0) for cm in dow_means.values())
        if total_baseline < 1:
            continue

        ratio = total_actual / total_baseline
        buckets[impact][slot].append(ratio)

    result = {}
    for impact, slots in buckets.items():
        result[impact] = {}
        for slot, ratios in slots.items():
            if len(ratios) >= 5:
                # Trim top/bottom 10%
                s = sorted(ratios)
                t = max(1, len(s) // 10)
                result[impact][slot] = round(mean(s[t:-t]), 4)
    return result


# ── Open-Meteo API helpers ────────────────────────────────────────────────────

def _fetch_open_meteo(base_url: str, start: str, end: str) -> list:
    """
    Returns a list of dicts, one per day, from start to end inclusive.
    """
    params = (
        f"latitude={LATITUDE}&longitude={LONGITUDE}"
        f"&daily={DAILY_VARS}&{UNITS}"
        f"&timezone=America%2FNew_York"
        f"&start_date={start}&end_date={end}"
    )
    url = f"{base_url}?{params}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Open-Meteo {e.code}: {e.read().decode()[:300]}") from e

    daily = data.get("daily", {})
    dates      = daily.get("time", [])
    max_temps  = daily.get("temperature_2m_max", [])
    min_temps  = daily.get("temperature_2m_min", [])
    precips    = daily.get("precipitation_sum", [])
    winds      = daily.get("wind_speed_10m_max", [])
    codes      = daily.get("weather_code", [])

    rows = []
    for i, d in enumerate(dates):
        code = int(codes[i]) if codes[i] is not None else 0
        rows.append({
            "report_date":      d,
            "max_temp_f":       round(float(max_temps[i]), 1) if max_temps[i] is not None else None,
            "min_temp_f":       round(float(min_temps[i]), 1) if min_temps[i] is not None else None,
            "precipitation_in": round(float(precips[i]),   2) if precips[i]   is not None else 0.0,
            "wind_speed_mph":   round(float(winds[i]),     1) if winds[i]     is not None else None,
            "weather_code":     code,
            "weather_desc":     wmo_label(code),
        })
    return rows


def fetch_historical(start: str, end: str) -> list:
    return _fetch_open_meteo(HISTORY_URL, start, end)


def fetch_forecast_weather(days: int = 16) -> list:
    today    = date.today().isoformat()
    end_date = (date.today() + timedelta(days=days - 1)).isoformat()
    return _fetch_open_meteo(FORECAST_URL, today, end_date)


# ── Supabase helpers ──────────────────────────────────────────────────────────

def _sb_headers():
    return {
        "apikey":        SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type":  "application/json",
    }


def upsert_weather(rows: list) -> int:
    if not rows:
        return 0
    url     = f"{SUPABASE_URL}/rest/v1/daily_weather?on_conflict=report_date"
    headers = {**_sb_headers(), "Prefer": "resolution=merge-duplicates,return=minimal"}
    data    = json.dumps(rows).encode()
    req     = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Supabase upsert daily_weather {e.code}: {e.read().decode()}") from e


def load_weather_from_supabase(start: str, end: str) -> dict:
    """Returns { date_str: weather_row_dict }."""
    url = (f"{SUPABASE_URL}/rest/v1/daily_weather?"
           f"report_date=gte.{start}&report_date=lte.{end}"
           f"&select=report_date,max_temp_f,min_temp_f,precipitation_in,wind_speed_mph,weather_code,weather_desc"
           f"&limit=500")
    req = urllib.request.Request(url, headers=_sb_headers())
    with urllib.request.urlopen(req, timeout=15) as r:
        rows = json.loads(r.read())
    return {row["report_date"]: row for row in rows}


# ── Main logic ────────────────────────────────────────────────────────────────

def run_backfill() -> str:
    today    = (date.today() - timedelta(days=1)).isoformat()
    rows     = fetch_historical(VENUE_OPEN_DATE, today)
    upsert_weather(rows)
    return f"ok: backfilled {len(rows)} days ({VENUE_OPEN_DATE} → {today})"


def run_yesterday() -> str:
    day  = (date.today() - timedelta(days=1)).isoformat()
    rows = fetch_historical(day, day)
    upsert_weather(rows)
    return f"ok: {day}  {rows[0]['weather_desc']}  max={rows[0]['max_temp_f']}°F  precip={rows[0]['precipitation_in']}\""


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Dayton OH weather fetcher")
    parser.add_argument("--backfill", action="store_true",
                        help="Fetch all history since venue open date")
    parser.add_argument("--forecast", action="store_true",
                        help="Print 16-day forecast (does not write to Supabase)")
    args = parser.parse_args()

    if args.backfill:
        print("Backfilling historical weather...")
        result = run_backfill()
        print(result)

    elif args.forecast:
        rows = fetch_forecast_weather(16)
        print(f"\n{'─'*70}")
        print(f"  Dayton OH — 16-day weather forecast")
        print(f"{'─'*70}")
        precip_hdr = 'Precip"'
        print(f"  {'Date':<12} {'DOW':<4} {'Max F':>6} {'Min F':>6} {precip_hdr:>8} "
              f"{'Wind':>6}  Description")
        print(f"{'─'*70}")
        dow_labels = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]
        for r in rows:
            d   = __import__("datetime").date.fromisoformat(r["report_date"])
            dow = dow_labels[d.weekday()]
            is_wknd = d.weekday() >= 4
            mult = weather_multiplier(r["max_temp_f"], r["precipitation_in"],
                                      r["weather_code"], is_wknd)
            flag = f"×{mult:.2f}"
            print(f"  {r['report_date']:<12} {dow:<4} "
                  f"{str(r['max_temp_f'] or '?'):>6} "
                  f"{str(r['min_temp_f'] or '?'):>6} "
                  f"{str(r['precipitation_in'] or '0.00'):>8} "
                  f"{str(r['wind_speed_mph'] or '?'):>6}  "
                  f"{r['weather_desc']:<25} {flag}")
        print(f"{'─'*70}\n")

    else:
        print(run_yesterday())


# ── Vercel cron handler ───────────────────────────────────────────────────────

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        auth = self.headers.get("authorization", "")
        if CRON_SECRET and auth != f"Bearer {CRON_SECRET}":
            self.send_response(401); self.end_headers()
            self.wfile.write(b"Unauthorized"); return
        try:
            result = run_yesterday()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": result}).encode())
        except Exception as e:
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    def log_message(self, *_):
        pass


if __name__ == "__main__":
    main()
