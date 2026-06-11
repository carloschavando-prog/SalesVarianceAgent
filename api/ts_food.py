#!/usr/bin/env python3
"""
ts_food.py — On Par Entertainment
Read-only endpoint: exact Tripleseat BEO amounts per day for a date range,
straight from ts_events. Used by the scheduling agent to get projected event
food (KIT) and non-food (FOH) revenue for forward weeks.

GET /api/ts_food?key=REPORT_KEY&start=YYYY-MM-DD&end=YYYY-MM-DD
Returns JSON: { range, totals, by_date: {date: {food, bev, games, karaoke, events, nonfood, total, events_list[]}} }

Status allowlist matches daily_report / labor pipeline: DEFINITE, CLOSED, TENTATIVE.
"""

import json
import os
import urllib.request
import urllib.parse
from collections import defaultdict
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
REPORT_KEY   = os.environ.get("REPORT_KEY", "")

_GAME_FIELDS = ("bowling_amount", "mini_golf_amount", "darts_amount",
                "shuffle_board_amount", "pool_amount")


def _supa_get(path: str) -> list:
    req = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1{path}",
        headers={"apikey": SUPABASE_KEY,
                 "Authorization": f"Bearer {SUPABASE_KEY}",
                 "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def _supa_paged(base: str) -> list:
    rows, offset = [], 0
    while True:
        sep = "&" if "?" in base else "?"
        batch = _supa_get(f"{base}{sep}limit=1000&offset={offset}")
        rows.extend(batch)
        if len(batch) < 1000:
            break
        offset += 1000
    return rows


def _f(row, key):
    return float(row.get(key) or 0)


def build(start: str, end: str) -> dict:
    sel = ("event_date,name,status,guest_count,food_amount,beverage_amount,"
           "events_amount,karaoke_amount,bowling_amount,mini_golf_amount,"
           "darts_amount,shuffle_board_amount,pool_amount")
    rows = _supa_paged(
        f"/ts_events?event_date=gte.{start}&event_date=lte.{end}"
        f"&deleted_at=is.null&status=in.(DEFINITE,CLOSED,TENTATIVE)"
        f"&select={urllib.parse.quote(sel)}"
    )
    by_date = defaultdict(lambda: {"food": 0.0, "bev": 0.0, "games": 0.0,
                                   "karaoke": 0.0, "events": 0.0,
                                   "events_list": []})
    for r in rows:
        d = (r.get("event_date") or "")[:10]
        if not d:
            continue
        food = _f(r, "food_amount")
        bev  = _f(r, "beverage_amount")
        kar  = _f(r, "karaoke_amount")
        evt  = _f(r, "events_amount")
        game = sum(_f(r, g) for g in _GAME_FIELDS)
        b = by_date[d]
        b["food"]    += food
        b["bev"]     += bev
        b["games"]   += game
        b["karaoke"] += kar
        b["events"]  += evt
        b["events_list"].append({
            "name": r.get("name", ""), "status": r.get("status", ""),
            "guests": r.get("guest_count") or 0, "food": round(food, 2),
            "nonfood": round(bev + game + kar + evt, 2),
        })

    out = {}
    tot = {"food": 0.0, "bev": 0.0, "games": 0.0, "karaoke": 0.0,
           "events": 0.0, "nonfood": 0.0, "total": 0.0}
    for d in sorted(by_date):
        b = by_date[d]
        nonfood = b["bev"] + b["games"] + b["karaoke"] + b["events"]
        total = nonfood + b["food"]
        out[d] = {
            "food":    round(b["food"], 2),
            "bev":     round(b["bev"], 2),
            "games":   round(b["games"], 2),
            "karaoke": round(b["karaoke"], 2),
            "events":  round(b["events"], 2),
            "nonfood": round(nonfood, 2),
            "total":   round(total, 2),
            "events_list": b["events_list"],
        }
        tot["food"]    += b["food"]
        tot["bev"]     += b["bev"]
        tot["games"]   += b["games"]
        tot["karaoke"] += b["karaoke"]
        tot["events"]  += b["events"]
        tot["nonfood"] += nonfood
        tot["total"]   += total
    return {"range": {"start": start, "end": end},
            "totals": {k: round(v, 2) for k, v in tot.items()},
            "by_date": out}


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        qs  = parse_qs(urlparse(self.path).query)
        key = qs.get("key", [""])[0]
        if REPORT_KEY and key != REPORT_KEY:
            self.send_response(401); self.end_headers()
            self.wfile.write(b"Unauthorized - add ?key=YOUR_KEY")
            return
        start = qs.get("start", [""])[0]
        end   = qs.get("end", [""])[0]
        if not start or not end:
            self.send_response(400); self.end_headers()
            self.wfile.write(b"Missing ?start=YYYY-MM-DD&end=YYYY-MM-DD")
            return
        try:
            data = build(start, end)
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
