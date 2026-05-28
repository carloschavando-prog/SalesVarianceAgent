#!/usr/bin/env python3
"""
forecast_push.py — On Par Entertainment
Daily Vercel cron: runs the forecast model and writes results to Supabase.
Runs at 6 AM ET (after the nightly GoTab sync).

Vercel cron: GET /api/forecast_push
Schedule: 0 11 * * *  (11:00 UTC = 7:00 AM ET)
"""

import json
import os
import sys
from datetime import date
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from forecast_agent import (
    fetch_daily_sales, fetch_events, load_weather,
    load_tab_hourly_profiles, build_forecasts,
    push_to_supabase, DEFAULT_FORECAST_DAYS, DEFAULT_HISTORY_WEEKS,
)

CRON_SECRET = os.getenv("CRON_SECRET", "")


def run_push(horizon_days: int = DEFAULT_FORECAST_DAYS) -> dict:
    daily    = fetch_daily_sales(history_weeks=DEFAULT_HISTORY_WEEKS)
    events   = fetch_events(horizon_days=horizon_days + 60)
    weather  = load_weather(horizon_days=horizon_days)

    results, last_date, _ = build_forecasts(daily, horizon_days, events, weather)
    push_to_supabase(results)

    total_rows = len(results)
    return {
        "status":        "ok",
        "generated_at":  date.today().isoformat(),
        "history_through": str(last_date),
        "rows_written":  total_rows,
        "horizon_days":  horizon_days,
    }


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        auth = self.headers.get("authorization", "")
        if CRON_SECRET and auth != f"Bearer {CRON_SECRET}":
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b"Unauthorized")
            return
        try:
            result = run_push()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(result).encode())
        except Exception as e:
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    def log_message(self, *_):
        pass
