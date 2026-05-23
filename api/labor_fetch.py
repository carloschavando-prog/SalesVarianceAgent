"""
Nightly labor fetch from 7Shifts Hours & Wages report.

Pulls actual worked hours/cost and scheduled hours by department,
upserts one row per day into the labor_daily Supabase table.

GET /api/labor_fetch  (cron: 10:00 UTC = 6:00 AM ET)
Supports ?date=YYYY-MM-DD for manual backfill.

Required env vars:
  SUPABASE_URL, SUPABASE_SERVICE_KEY, CRON_SECRET, SEVEN_SHIFTS_TOKEN
"""

import json
import urllib.request
import urllib.error
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import os

SUPABASE_URL       = os.environ["SUPABASE_URL"]
SUPABASE_KEY       = os.environ["SUPABASE_SERVICE_KEY"]
CRON_SECRET        = os.environ.get("CRON_SECRET", "")
SEVEN_SHIFTS_TOKEN = os.environ["SEVEN_SHIFTS_TOKEN"]

COMPANY_ID  = "286488"
LOCATION_ID = "354876"

# role_id → "kit" or "foh"
# Manager (1760491) intentionally absent — salaried, $0 in 7Shifts
# CLEANER dept (545687) has no roles — ignored
ROLE_DEPT = {
    1760490: "foh",  # Beer wall ambassador
    1760495: "kit",  # Line Cook
    1760496: "kit",  # Dishwasher
    1761419: "foh",  # Security
    1780081: "kit",  # EXPO
    2045831: "foh",  # STAR BWA
    2103857: "kit",  # shift supervisor
    2215217: "foh",  # Marketing Assistant
    2332059: "kit",  # Content Creation
    2686259: "kit",  # Events
    2754779: "kit",  # Kitchen
}


# ---------------------------------------------------------------------------
# 7Shifts helpers
# ---------------------------------------------------------------------------

def _7shifts_get(path: str) -> dict:
    req = urllib.request.Request(
        f"https://api.7shifts.com{path}",
        headers={
            "Authorization": f"Bearer {SEVEN_SHIFTS_TOKEN}",
            "accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def _fetch_report(target_date: str, punches: bool) -> dict:
    flag = "true" if punches else "false"
    return _7shifts_get(
        f"/v2/reports/hours_and_wages"
        f"?company_id={COMPANY_ID}"
        f"&location_id={LOCATION_ID}"
        f"&from={target_date}"
        f"&to={target_date}"
        f"&punches={flag}"
    )


def _aggregate_actual(report: dict, target_date: str) -> dict:
    totals = {"kit_hours": 0.0, "kit_cost": 0.0, "foh_hours": 0.0, "foh_cost": 0.0}
    for user in report.get("users", []):
        for week in user.get("weeks", []):
            for shift in week.get("shifts", []):
                if (shift.get("date") or "")[:10] != target_date:
                    continue
                dept = ROLE_DEPT.get(shift.get("role_id"))
                if dept is None:
                    continue
                t = shift.get("total") or {}
                totals[f"{dept}_hours"] = round(totals[f"{dept}_hours"] + float(t.get("total_hours") or 0), 2)
                totals[f"{dept}_cost"]  = round(totals[f"{dept}_cost"]  + float(t.get("total_pay")   or 0), 2)
    return totals


def _aggregate_sched(report: dict, target_date: str) -> dict:
    totals = {"kit_sched": 0.0, "foh_sched": 0.0}
    for user in report.get("users", []):
        for week in user.get("weeks", []):
            for shift in week.get("shifts", []):
                if (shift.get("date") or "")[:10] != target_date:
                    continue
                dept = ROLE_DEPT.get(shift.get("role_id"))
                if dept is None:
                    continue
                t = shift.get("total") or {}
                totals[f"{dept}_sched"] = round(totals[f"{dept}_sched"] + float(t.get("total_hours") or 0), 2)
    return totals


# ---------------------------------------------------------------------------
# Supabase helpers
# ---------------------------------------------------------------------------

def _supa_upsert(rows: list) -> None:
    data = json.dumps(rows).encode()
    req = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1/labor_daily",
        data=data,
        headers={
            "apikey":        SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type":  "application/json",
            "Prefer":        "resolution=merge-duplicates",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        r.read()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(target_date: str) -> str:
    actual_report = _fetch_report(target_date, punches=True)
    sched_report  = _fetch_report(target_date, punches=False)

    actual = _aggregate_actual(actual_report, target_date)
    sched  = _aggregate_sched(sched_report,  target_date)

    row = {
        "date":        target_date,
        "kit_hours":   actual["kit_hours"],
        "kit_sched":   sched["kit_sched"],
        "kit_cost":    actual["kit_cost"],
        "foh_hours":   actual["foh_hours"],
        "foh_sched":   sched["foh_sched"],
        "foh_cost":    actual["foh_cost"],
        "total_hours": round(actual["kit_hours"] + actual["foh_hours"], 2),
        "total_sched": round(sched["kit_sched"]  + sched["foh_sched"],  2),
        "total_cost":  round(actual["kit_cost"]  + actual["foh_cost"],  2),
        "fetched_at":  "now()",
    }
    _supa_upsert([row])

    return (
        f"ok: {target_date} — "
        f"KIT {actual['kit_hours']:.1f}h ${actual['kit_cost']:,.2f}  "
        f"FOH {actual['foh_hours']:.1f}h ${actual['foh_cost']:,.2f}  "
        f"Total ${row['total_cost']:,.2f}"
    )


# ---------------------------------------------------------------------------
# Vercel handler
# ---------------------------------------------------------------------------

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        auth = self.headers.get("authorization", "")
        if CRON_SECRET and auth != f"Bearer {CRON_SECRET}":
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b"Unauthorized")
            return

        qs = parse_qs(urlparse(self.path).query)
        target_date = (
            qs["date"][0] if "date" in qs
            else (date.today() - timedelta(days=1)).isoformat()
        )

        try:
            result = run(target_date)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": result}).encode())
        except Exception as e:
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    def log_message(self, format, *args):
        pass
