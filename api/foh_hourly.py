#!/usr/bin/env python3
"""
foh_hourly.py — On Par Entertainment
Average FOH (non-food) sales by hour, by day of week.

FOH = GoTab POS total sales - food sales (i.e. bar/entertainment/karaoke/other).
Hourly dollars aren't stored, so we distribute each day-of-week's average
non-food revenue across local (ET) hours using the real hourly tab-open profile
from tab_metrics.

GET /api/foh_hourly?key=REPORT_KEY[&weeks=16]
Returns JSON: { window, dow_avg_nonfood, profile_source, grid: {Mon:{"11":523,...},...} }
"""

import json
import os
import urllib.request
from datetime import date, timedelta
from collections import defaultdict
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
REPORT_KEY   = os.environ.get("REPORT_KEY", "")

_FOOD_CATS = frozenset({
    "chicken.", "dessert", "event food", "extra sauces and cheese dips",
    "fry platters", "half pound burgers", "legacy menu items",
    "pizza and flatbreads", "pretzels", "tacos", "tater kegs", "wraps",
})
_DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _supa_get(path):
    req = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1{path}",
        headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
                 "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read())


def _paged(base):
    rows, off = [], 0
    while True:
        sep = "&" if "?" in base else "?"
        b = _supa_get(f"{base}{sep}limit=1000&offset={off}")
        rows.extend(b)
        if len(b) < 1000:
            break
        off += 1000
    return rows


def _et_offset(d: date) -> int:
    """US Eastern UTC offset: -4 during DST (2nd Sun Mar .. 1st Sun Nov), else -5."""
    y = d.year
    # 2nd Sunday of March
    mar = date(y, 3, 1)
    dst_start = mar + timedelta(days=(6 - mar.weekday()) % 7 + 7)
    nov = date(y, 11, 1)
    dst_end = nov + timedelta(days=(6 - nov.weekday()) % 7)
    return -4 if dst_start <= d < dst_end else -5


def build(weeks: int) -> dict:
    end = date.today()
    start = end - timedelta(weeks=weeks)
    s_iso, e_iso = start.isoformat(), end.isoformat()

    # --- daily non-food from sales ---
    sales = _paged(f"/sales?report_date=gte.{s_iso}&report_date=lt.{e_iso}"
                   f"&select=report_date,category,net_sales")
    day = defaultdict(lambda: {"food": 0.0, "total": 0.0})
    for r in sales:
        d = (r.get("report_date") or "")[:10]
        if not d:
            continue
        amt = float(r.get("net_sales") or 0)
        day[d]["total"] += amt
        if (r.get("category") or "").lower().strip() in _FOOD_CATS:
            day[d]["food"] += amt
    dow_nf = defaultdict(list)
    for d, v in day.items():
        nf = v["total"] - v["food"]
        dow_nf[date.fromisoformat(d).weekday()].append(nf)
    dow_avg = {w: (sum(vals) / len(vals) if vals else 0.0) for w, vals in dow_nf.items()}

    # --- hourly tab-open profile from tab_metrics (UTC -> ET) ---
    prof_counts = defaultdict(lambda: defaultdict(float))  # dow -> et_hour -> count
    src = "tab_metrics"
    try:
        tm = _paged(f"/tab_metrics?report_date=gte.{s_iso}&report_date=lt.{e_iso}"
                    f"&select=report_date,hourly_opens")
    except Exception:
        tm = []
    n_tm = 0
    for r in tm:
        ho = r.get("hourly_opens")
        d = (r.get("report_date") or "")[:10]
        if not ho or not d:
            continue
        n_tm += 1
        dd = date.fromisoformat(d)
        off = _et_offset(dd)
        wd = dd.weekday()
        for hr_str, cnt in ho.items():
            try:
                et = (int(hr_str) + off) % 24
            except (ValueError, TypeError):
                continue
            prof_counts[wd][et] += float(cnt or 0)
    if n_tm == 0:
        src = "none (tab_metrics empty)"

    # --- grid: dow -> et_hour -> avg FOH $ ---
    grid = {}
    for wd in range(7):
        avg_nf = dow_avg.get(wd, 0.0)
        hours = prof_counts.get(wd, {})
        tot = sum(hours.values())
        row = {}
        if tot > 0:
            for h, c in hours.items():
                row[str(h)] = round(avg_nf * c / tot, 2)
        grid[_DOW[wd]] = dict(sorted(row.items(), key=lambda x: int(x[0])))

    return {
        "window": {"start": s_iso, "end": e_iso, "weeks": weeks},
        "profile_source": src,
        "tab_metrics_days": n_tm,
        "sales_days": len(day),
        "dow_avg_nonfood": {_DOW[w]: round(dow_avg.get(w, 0.0), 2) for w in range(7)},
        "grid": grid,
    }


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        qs = parse_qs(urlparse(self.path).query)
        if REPORT_KEY and qs.get("key", [""])[0] != REPORT_KEY:
            self.send_response(401); self.end_headers()
            self.wfile.write(b"Unauthorized - add ?key=YOUR_KEY"); return
        try:
            weeks = int(qs.get("weeks", ["16"])[0])
        except ValueError:
            weeks = 16
        try:
            data = build(weeks)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(data).encode())
        except Exception as e:
            import traceback
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e),
                "trace": traceback.format_exc()[-1500:]}).encode())

    def log_message(self, *_):
        pass
