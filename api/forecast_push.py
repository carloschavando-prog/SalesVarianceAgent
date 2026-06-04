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

from collections import defaultdict

from forecast_agent import (
    fetch_daily_sales, fetch_events, load_weather,
    load_tab_hourly_profiles, build_forecasts,
    push_to_supabase, supabase_get,
    DEFAULT_FORECAST_DAYS, DEFAULT_HISTORY_WEEKS, MODEL_VERSION,
)

# Best-effort status reporting to the Agent Control Board (never breaks this agent).
try:
    from control_board import report
except Exception:
    def report(*_a, **_k):
        return

CRON_SECRET = os.getenv("CRON_SECRET", "")


def _uplift_summary(results: list) -> dict:
    """Summarize the event uplift the model computed for this run (pre-write)."""
    by_day = defaultdict(float)
    for r in results:
        u = float(r.get("event_uplift") or 0)
        if u:
            by_day[r["forecast_date"]] += u
    return {
        "event_days":   len(by_day),
        "total_uplift": round(sum(by_day.values()), 2),
        "days": {d: round(v, 2) for d, v in sorted(by_day.items())},
    }


def _verify_stored_uplift(from_date: str) -> dict:
    """
    Read back the rows just written (forecast_date >= from_date, current model
    version) and confirm event_uplift actually persisted to Supabase. This is the
    round-trip proof that the cron records uplift going forward.
    """
    try:
        rows = supabase_get("forecasts", [
            ("select",        "forecast_date,event_uplift"),
            ("forecast_date", f"gte.{from_date}"),
            ("model_version", f"eq.{MODEL_VERSION}"),
            ("limit",         5000),
        ])
    except Exception as e:
        return {"ok": False, "error": str(e)}
    by_day = defaultdict(float)
    for r in rows:
        u = float(r.get("event_uplift") or 0)
        if u:
            by_day[r["forecast_date"]] += u
    return {
        "ok":                  True,
        "rows_read":           len(rows),
        "stored_event_days":   len(by_day),
        "stored_total_uplift": round(sum(by_day.values()), 2),
    }


def run_push(horizon_days: int = DEFAULT_FORECAST_DAYS) -> dict:
    daily    = fetch_daily_sales(history_weeks=DEFAULT_HISTORY_WEEKS)
    events   = fetch_events(horizon_days=horizon_days + 60)
    weather  = load_weather(horizon_days=horizon_days)

    results, last_date, _ = build_forecasts(daily, horizon_days, events, weather)
    push_to_supabase(results)

    today_iso = date.today().isoformat()
    return {
        "status":          "ok",
        "generated_at":    today_iso,
        "history_through": str(last_date),
        "rows_written":    len(results),
        "horizon_days":    horizon_days,
        "uplift_computed": _uplift_summary(results),
        "uplift_stored":   _verify_stored_uplift(today_iso),
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
            report("forecast", "started", current_task="Scheduled forecast push")
            result = run_push()
            report("forecast", "finished",
                   output=f"Forecast push — {result.get('rows_written')} rows through {result.get('history_through')}")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(result).encode())
        except Exception as e:
            report("forecast", "failed", message=f"Forecast push: {e}")
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    def log_message(self, *_):
        pass
