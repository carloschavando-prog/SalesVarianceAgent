"""
Daily revenue aggregation agent.

Runs after both GoTab (9 UTC) and Tripleseat (11 UTC) syncs.
Reads yesterday's data from `sales` and `ts_events`, maps every line item to
one of 15 revenue columns, and upserts two rows into `daily_revenue`
(source='gotab' and source='tripleseat').
"""

import json
import urllib.request
import urllib.error
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler
import os

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
CRON_SECRET  = os.environ.get("CRON_SECRET", "")

# ---------------------------------------------------------------------------
# Column definitions (must match daily_revenue table and revenue_export.py)
# ---------------------------------------------------------------------------
COLS = [
    "food", "beverage", "mini_golf", "bowling", "karaoke", "darts",
    "shuffle_board", "pool", "merchandise", "events", "open_item",
    "bottle_svc", "reservations", "gift_card", "redeemed",
]

def _empty() -> dict:
    return {c: 0.0 for c in COLS}


# ---------------------------------------------------------------------------
# GoTab category → column mapping
# ---------------------------------------------------------------------------
_FOOD_CATS = frozenset({
    "chicken.", "dessert", "event food", "extra sauces and cheese dips",
    "fry platters", "half pound burgers", "legacy menu items",
    "pizza and flatbreads", "pretzels", "tacos", "tater kegs", "wraps",
})
_BEV_CATS = frozenset({"beverage", "soda pop", "wine"})


def _gotab_col(category: str, product: str) -> str:
    cat  = (category or "").lower().strip()
    prod = (product  or "").lower().strip()
    if cat in _FOOD_CATS:
        return "food"
    if cat in _BEV_CATS:
        return "beverage"
    if cat == "karaoke":
        return "karaoke"
    if cat == "merchandise":
        return "merchandise"
    if cat == "reservations":
        return "reservations"
    if cat == "bottle service":
        return "bottle_svc"
    if cat == "entertainment":
        if "mini golf" in prod:  return "mini_golf"
        if "bowling"   in prod:  return "bowling"
        if "darts"     in prod:  return "darts"
        if "shuffle"   in prod:  return "shuffle_board"
        if "pool"      in prod:  return "pool"
    return "open_item"


# ---------------------------------------------------------------------------
# Supabase helpers
# ---------------------------------------------------------------------------
def _supa_get(path: str) -> list:
    req = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1{path}",
        headers={
            "apikey":        SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Accept":        "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def _supa_upsert(table: str, rows: list) -> None:
    data = json.dumps(rows).encode()
    req  = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1/{table}",
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
# Aggregation logic
# ---------------------------------------------------------------------------
def _aggregate_gotab(target_date: str) -> dict:
    rows = _supa_get(
        f"/sales?report_date=eq.{target_date}"
        f"&select=category,product,net_sales"
    )
    totals = _empty()
    for row in rows:
        col = _gotab_col(row.get("category") or "", row.get("product") or "")
        totals[col] = round(totals[col] + float(row.get("net_sales") or 0), 2)
    return totals


_TS_GAME_COLS = (
    "bowling_amount,mini_golf_amount,darts_amount,shuffle_board_amount,pool_amount"
)


def _aggregate_tripleseat(target_date: str) -> dict:
    # Try to fetch per-game columns (available after revenue_schema.sql is applied).
    # Fall back to base columns only if the schema migration hasn't run yet.
    try:
        rows = _supa_get(
            f"/ts_events?event_date=eq.{target_date}"
            f"&deleted_at=is.null"
            f"&select=food_amount,beverage_amount,events_amount,{_TS_GAME_COLS}"
        )
        has_game_cols = True
    except Exception:
        rows = _supa_get(
            f"/ts_events?event_date=eq.{target_date}"
            f"&deleted_at=is.null"
            f"&select=food_amount,beverage_amount,events_amount"
        )
        has_game_cols = False

    totals = _empty()
    for row in rows:
        totals["food"]     = round(totals["food"]     + float(row.get("food_amount")     or 0), 2)
        totals["beverage"] = round(totals["beverage"] + float(row.get("beverage_amount") or 0), 2)
        totals["events"]   = round(totals["events"]   + float(row.get("events_amount")   or 0), 2)
        if has_game_cols:
            totals["bowling"]       = round(totals["bowling"]       + float(row.get("bowling_amount")       or 0), 2)
            totals["mini_golf"]     = round(totals["mini_golf"]     + float(row.get("mini_golf_amount")     or 0), 2)
            totals["darts"]         = round(totals["darts"]         + float(row.get("darts_amount")         or 0), 2)
            totals["shuffle_board"] = round(totals["shuffle_board"] + float(row.get("shuffle_board_amount") or 0), 2)
            totals["pool"]          = round(totals["pool"]          + float(row.get("pool_amount")          or 0), 2)
    return totals


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def run(target_date: str) -> str:
    gotab_totals = _aggregate_gotab(target_date)
    ts_totals    = _aggregate_tripleseat(target_date)

    rows = [
        {"report_date": target_date, "source": "gotab",       **gotab_totals},
        {"report_date": target_date, "source": "tripleseat",  **ts_totals},
    ]
    _supa_upsert("daily_revenue", rows)

    gt_total = sum(gotab_totals.values())
    ts_total = sum(ts_totals.values())
    return (
        f"ok: {target_date} — "
        f"GoTab ${gt_total:,.2f}  Tripleseat ${ts_total:,.2f}"
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

        # Support ?date=YYYY-MM-DD for manual backfill; default to yesterday
        from urllib.parse import urlparse, parse_qs
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
