#!/usr/bin/env python3
"""
tab_metrics_fetch.py — On Par Entertainment
Fetches GoTab tab/guest/hourly data per day and upserts into Supabase tab_metrics.

Usage:
  python tab_metrics_fetch.py                # yesterday only (daily cron mode)
  python tab_metrics_fetch.py --backfill 52  # last 52 weeks (one-time backfill)
  python tab_metrics_fetch.py --date 2026-05-20  # specific date

Rate limit: 4 req/sec → 0.4s sleep between GoTab API calls.

Run tab_metrics_schema.sql in Supabase first.
"""

import json
import os
import sys
import time
import urllib.request
import urllib.parse
import urllib.error
from collections import defaultdict
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler

# ── Config ────────────────────────────────────────────────────────────────────

GOTAB_AUTH_URL    = "https://gotab.io/api/oauth/token"
GOTAB_GRAPH_URL   = "https://gotab.io/api/graph"
LOCATION_ID       = int(os.environ.get("GOTAB_LOCATION_ID", "112479"))
API_ACCESS_ID     = os.environ.get("GOTAB_API_ACCESS_ID", "")
API_ACCESS_SECRET = os.environ.get("GOTAB_API_ACCESS_SECRET", "")
SUPABASE_URL      = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY      = os.environ.get("SUPABASE_SERVICE_KEY", os.environ.get("SUPABASE_KEY", ""))
CRON_SECRET = os.environ.get("CRON_SECRET", "")

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

# System zones that are not real physical spaces — excluded from zone_tab_counts
SYSTEM_ZONES = {
    "PMB INTEGRATION - DO NOT TOUCH",
    "Reservation and Deposit Zone DONT ARCHIVE",
    "3rd Party Order Zone",
    "Tripleseat Event Zone",
    "E-Commerce",
    "Default",
    "Kiosk",
    "Counter",
    "Ordering Station Bowling",
}

# ── GoTab helpers ─────────────────────────────────────────────────────────────

def get_token():
    data = json.dumps({
        "api_access_id":     API_ACCESS_ID,
        "api_access_secret": API_ACCESS_SECRET,
        "grant_type":        "client_credentials",
    }).encode()
    req = urllib.request.Request(
        GOTAB_AUTH_URL, data=data, method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json",
                 "User-Agent": UA},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())["token"]


def gql(token, query, variables=None, retries=4):
    data = json.dumps({"query": query, "variables": variables or {}}).encode()
    req = urllib.request.Request(
        GOTAB_GRAPH_URL, data=data, method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                 "Accept": "application/json", "User-Agent": UA},
    )
    last_err = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=45) as r:
                result = json.loads(r.read())
            if result.get("errors"):
                raise RuntimeError(f"GraphQL errors: {result['errors']}")
            return result
        except Exception as e:
            last_err = e
            wait = 2 ** attempt
            print(f"    retry {attempt+1}/{retries} after {wait}s ({e})")
            time.sleep(wait)
    raise RuntimeError(f"gql failed after {retries} retries: {last_err}") from last_err


_TABS_QUERY = """
query TabMetrics($loc: BigInt!, $day: Date!, $offset: Int!) {
  tabsList(
    filter: { locationId: { equalTo: $loc }, fiscalDay: { equalTo: $day } }
    first: 500
    offset: $offset
  ) {
    total
    numGuests
    opened
    zonesList { name }
  }
}
"""


def fetch_tabs_for_date(token: str, day: str) -> dict:
    """
    Returns:
      { tab_count, revenue_tab_count, guest_count, hourly_opens, zone_tab_counts }
    """
    all_tabs = []
    offset   = 0

    while True:
        time.sleep(0.4)
        result = gql(token, _TABS_QUERY, {"loc": LOCATION_ID, "day": day, "offset": offset})
        batch  = result["data"]["tabsList"]
        all_tabs.extend(batch)
        if len(batch) < 500:
            break
        offset += 500

    hourly:  dict = defaultdict(int)
    zones:   dict = defaultdict(int)
    guests        = 0
    rev_count     = 0

    for tab in all_tabs:
        guests    += tab.get("numGuests") or 0
        if (tab.get("total") or 0) > 0:
            rev_count += 1
        # hour from opened timestamp (UTC)
        opened = tab.get("opened")
        if opened:
            hr = opened[11:13]   # "2026-05-23T21:30:00..." → "21"
            hourly[hr] += 1
        # physical zones only
        for z in (tab.get("zonesList") or []):
            zname = z.get("name") or ""
            if zname and zname not in SYSTEM_ZONES:
                zones[zname] += 1

    return {
        "tab_count":         len(all_tabs),
        "revenue_tab_count": rev_count,
        "guest_count":       guests,
        "hourly_opens":      dict(sorted(hourly.items())),
        "zone_tab_counts":   dict(sorted(zones.items(), key=lambda x: -x[1])),
    }


# ── Supabase helpers ──────────────────────────────────────────────────────────

def _sb_headers():
    return {
        "apikey":        SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type":  "application/json",
        "Accept":        "application/json",
    }


def already_loaded(day: str) -> bool:
    url = (f"{SUPABASE_URL}/rest/v1/tab_metrics?"
           f"report_date=eq.{day}&select=report_date&limit=1")
    req = urllib.request.Request(url, headers=_sb_headers())
    rows = json.loads(urllib.request.urlopen(req, timeout=10).read())
    return len(rows) > 0


def upsert_metrics(day: str, metrics: dict):
    row = {
        "report_date":       day,
        "tab_count":         metrics["tab_count"],
        "revenue_tab_count": metrics["revenue_tab_count"],
        "guest_count":       metrics["guest_count"],
        "hourly_opens":      metrics["hourly_opens"],
        "zone_tab_counts":   metrics["zone_tab_counts"],
    }
    url     = f"{SUPABASE_URL}/rest/v1/tab_metrics?on_conflict=report_date"
    headers = {**_sb_headers(), "Prefer": "resolution=merge-duplicates,return=minimal"}
    data    = json.dumps([row]).encode()
    req     = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.status


# ── Main logic ────────────────────────────────────────────────────────────────

def run_single_date(token: str, day: str, force: bool = False) -> str:
    if not force and already_loaded(day):
        return f"skip: {day} already loaded"
    metrics = fetch_tabs_for_date(token, day)
    if metrics["tab_count"] == 0:
        return f"empty: no tabs found for {day}"
    upsert_metrics(day, metrics)
    return (f"ok: {day}  tabs={metrics['tab_count']}  "
            f"rev_tabs={metrics['revenue_tab_count']}  "
            f"guests={metrics['guest_count']}")


def run_backfill(weeks: int = 4) -> None:
    token      = get_token()
    token_age  = 0          # refresh every 50 dates (~20 min at 0.4s/req)
    end        = date.today() - timedelta(days=1)
    start      = end - timedelta(weeks=weeks)
    day        = start
    total      = (end - start).days + 1
    done       = 0

    print(f"Backfilling {start} → {end} ({total} days, {weeks} weeks)...")
    while day <= end:
        # Refresh token periodically so it doesn't expire mid-run
        if token_age >= 50:
            try:
                token     = get_token()
                token_age = 0
            except Exception as e:
                print(f"  Warning: token refresh failed ({e}), continuing with old token")

        day_str = day.isoformat()
        try:
            result = run_single_date(token, day_str)
        except Exception as e:
            result = f"error: {day_str} — {e}"
        print(f"  [{done+1:>4}/{total}] {result}")
        done      += 1
        token_age += 1
        day       += timedelta(days=1)
    print(f"\nDone. {done} dates processed.")


def run_yesterday() -> str:
    token = get_token()
    day   = (date.today() - timedelta(days=1)).isoformat()
    return run_single_date(token, day)


# ── Vercel cron handler ───────────────────────────────────────────────────────

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        auth = self.headers.get("authorization", "")
        if CRON_SECRET and auth != f"Bearer {CRON_SECRET}":
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b"Unauthorized")
            return
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

    def log_message(self, format, *args):
        pass


# ── CLI entry point ───────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="On Par — GoTab tab metrics fetcher")
    parser.add_argument("--backfill", type=int, metavar="WEEKS",
                        help="Backfill last N weeks of tab metrics")
    parser.add_argument("--date", type=str,
                        help="Fetch a specific date (YYYY-MM-DD)")
    parser.add_argument("--force", action="store_true",
                        help="Re-fetch even if date already loaded")
    args = parser.parse_args()

    if args.backfill:
        run_backfill(weeks=args.backfill)
    elif args.date:
        token  = get_token()
        result = run_single_date(token, args.date, force=args.force)
        print(result)
    else:
        result = run_yesterday()
        print(result)


if __name__ == "__main__":
    main()
